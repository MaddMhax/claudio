from datetime import date, timedelta

from flask import Blueprint, abort, render_template, request
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from wtforms import IntegerField, SelectField
from wtforms.validators import DataRequired, NumberRange, Optional

from .extensions import db
from .holidays import is_french_holiday
from .models import (
    PROVENANCE_ROLE_KEYS,
    SYSTEM_ROLES,
    Event,
    Meeting,
    Project,
    Role,
    Task,
    User,
    UserRole,
)
from .planning import _collect_blocking_for_users


bp = Blueprint("search", __name__, url_prefix="/search")


HORIZON_DAYS = 365
MAX_RESULTS = 20


class AvailabilitySearchForm(FlaskForm):
    jh_count = IntegerField(
        "Nombre de JH (jours-homme)",
        default=5,
        validators=[DataRequired(), NumberRange(min=1, max=120)],
    )
    pentester_count = IntegerField(
        "Nombre de pentesters",
        default=1,
        validators=[DataRequired(), NumberRange(min=1, max=20)],
    )
    role_key = SelectField(
        "Spécialité (optionnelle)",
        default="",
        validators=[Optional()],
    )

    def populate_role_choices(self) -> None:
        roles = db.session.execute(
            db.select(Role).order_by(Role.label)
        ).scalars().all()
        self.role_key.choices = [("", "— toutes spécialités —")] + [
            (r.key, r.label) for r in roles
        ]


def _is_workable(d: date) -> bool:
    return d.weekday() < 5 and not is_french_holiday(d)


def _window_days(jh: int, k: int) -> int:
    """Consecutive working days needed to burn ``jh`` man-days with ``k``
    pentesters working in parallel: ``ceil(jh / k)``. E.g. 10 JH / 2 = 5 days,
    15 JH / 3 = 5 days, 20 JH / 2 = 10 days."""
    return -(-jh // k)


def _find_slots(
    auditors: list[User], jh: int, k: int
) -> list[dict]:
    """Return up to ``MAX_RESULTS`` consecutive-working-day windows starting
    today, where at least ``k`` auditors are free for the entire window. The
    window length is ``ceil(jh / k)`` working days (the ``jh`` man-days spread
    across ``k`` parallel pentesters). The same set-of-free-auditors only
    surfaces once (the earliest start)."""
    if not auditors or k > len(auditors):
        return []

    days = _window_days(jh, k)

    start = date.today()
    horizon = start + timedelta(days=HORIZON_DAYS)
    auditor_ids = {a.id for a in auditors}

    by_user = _collect_blocking_for_users(auditor_ids, start, horizon)

    # Per-auditor set of blocked dates within the horizon.
    blocked_dates: dict[int, set[date]] = {}
    for uid in auditor_ids:
        blocked: set[date] = set()
        for ev in by_user.get(uid, []):
            d = max(ev.start_date, start)
            end = min(ev.end_date, horizon)
            while d <= end:
                blocked.add(d)
                d += timedelta(days=1)
        blocked_dates[uid] = blocked

    # All workable days in the horizon, in chronological order.
    working_days: list[date] = []
    d = start
    while d <= horizon:
        if _is_workable(d):
            working_days.append(d)
        d += timedelta(days=1)

    # Per-day set of free auditors.
    free_by_day: dict[date, set[int]] = {
        wd: {uid for uid in auditor_ids if wd not in blocked_dates[uid]}
        for wd in working_days
    }

    results: list[dict] = []
    prev_set: frozenset[int] | None = None
    by_uid = {a.id: a for a in auditors}

    for i in range(len(working_days) - days + 1):
        window = working_days[i:i + days]
        intersection = set(free_by_day[window[0]])
        for wd in window[1:]:
            intersection &= free_by_day[wd]
            if not intersection:
                break
        if len(intersection) < k:
            prev_set = None
            continue
        # Never propose an audit that starts on a Friday (weekday 4). Skip
        # before touching prev_set so the same free set can still surface at
        # its next non-Friday start.
        if window[0].weekday() == 4:
            continue
        # Suppress sliding duplicates: only emit when the free set changes.
        current = frozenset(intersection)
        if current == prev_set:
            continue
        prev_set = current
        results.append({
            "start": window[0],
            "end": window[-1],
            "days": days,
            "free_auditors": sorted(
                (by_uid[uid] for uid in current),
                key=lambda u: u.full_name.lower(),
            ),
            "free_count": len(current),
        })
        if len(results) >= MAX_RESULTS:
            break

    return results


def _all_pentesters() -> list[User]:
    """Internal pentesters: users holding at least one real specialty role (a
    non-system, non-provenance Role), excluding external providers.

    Replaces the legacy hardcoded AUDITOR_ROLES filter: any user assigned a
    specialty surfaced in the /admin/users Rôles panel counts — but externals
    (« prestataire ») are left out of the availability search, just like they're
    left out of the planning's green availability cells."""
    users = db.session.execute(
        db.select(User).join(UserRole).distinct().order_by(User.full_name)
    ).scalars().all()
    return [
        u for u in users
        if not u.is_external
        and any(
            r not in SYSTEM_ROLES and r not in PROVENANCE_ROLE_KEYS
            for r in u.roles
        )
    ]


def _text_search(query: str) -> dict:
    """Substring (ILIKE) lookup against project / mission / meeting / task names.

    Returns one bucket per entity type with the matching rows, capped at 50 per
    bucket to keep the page bounded. Case- and accent-tolerance is what the
    underlying collation gives us — ``ILIKE`` is case-insensitive everywhere,
    accent folding depends on the PostgreSQL collation."""
    pattern = f"%{query}%"
    projects = db.session.execute(
        db.select(Project)
        .where(Project.name.ilike(pattern))
        .order_by(Project.name)
        .limit(50)
    ).scalars().all()
    missions = db.session.execute(
        db.select(Event)
        .where(Event.title.ilike(pattern))
        .order_by(Event.start_date.desc(), Event.start_time.desc())
        .limit(50)
    ).scalars().all()
    meetings = db.session.execute(
        db.select(Meeting)
        .where(Meeting.name.ilike(pattern))
        .order_by(Meeting.date.desc())
        .limit(50)
    ).scalars().all()
    tasks = db.session.execute(
        db.select(Task)
        .where(Task.name.ilike(pattern))
        .order_by(Task.due_date.is_(None), Task.due_date.desc(), Task.name)
        .limit(50)
    ).scalars().all()
    return {
        "projects": projects,
        "missions": missions,
        "meetings": meetings,
        "tasks": tasks,
        "total": len(projects) + len(missions) + len(meetings) + len(tasks),
    }


@bp.route("/", methods=["GET", "POST"])
@login_required
def availability_search():
    if not current_user.can_manage_events:
        abort(403)

    form = AvailabilitySearchForm()
    form.populate_role_choices()
    results: list[dict] | None = None
    pentesters = _all_pentesters()

    if form.validate_on_submit():
        jh = form.jh_count.data
        k = form.pentester_count.data
        # Optional specialty filter — only auditors holding the chosen Role.key
        # are considered. Empty string = no filter (all pentesters).
        wanted_role = (form.role_key.data or "").strip() or None
        pool = (
            [u for u in pentesters if wanted_role in u.roles]
            if wanted_role else pentesters
        )
        if k > len(pool):
            if wanted_role:
                form.pentester_count.errors.append(
                    f"Seuls {len(pool)} pentester(s) ont cette spécialité."
                )
            else:
                form.pentester_count.errors.append(
                    f"L'équipe ne compte que {len(pool)} pentester(s)."
                )
        else:
            results = _find_slots(pool, jh, k)

    # Textual search runs in parallel on every GET that carries a ?q=… param
    # so the page can host both forms without juggling separate endpoints.
    raw_q = (request.args.get("q") or "").strip()
    text_results = _text_search(raw_q) if raw_q else None

    return render_template(
        "search_availability.html",
        form=form,
        results=results,
        auditor_total=len(pentesters),
        horizon_days=HORIZON_DAYS,
        text_query=raw_q,
        text_results=text_results,
    )
