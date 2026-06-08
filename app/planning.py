import calendar as _calendar
from datetime import date, datetime, time, timedelta

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    DateField,
    SelectField,
    SelectMultipleField,
    StringField,
    TextAreaField,
    TimeField,
    URLField,
)
from wtforms.validators import DataRequired, Length, Optional, URL

from .extensions import db
from .holidays import add_working_days, effective_holidays, is_french_holiday


def _holidays_iso_for_form(anchor: date | None = None) -> list[str]:
    """ISO date strings for French holidays spanning a window wide enough to
    cover any reasonable mission length. Used by the event form's JS to skip
    holidays when computing end_date from JH / pentester count."""
    anchor = anchor or date.today()
    out: list[str] = []
    for y in range(anchor.year - 1, anchor.year + 3):
        out.extend(d.isoformat() for d in effective_holidays(y))
    return sorted(out)
from .models import (
    PROVENANCE_ROLE_KEYS,
    ROLE_ADMIN,
    SYSTEM_ROLES,
    Client,
    EVENT_STATUS_PLANIFIE,
    EVENT_STATUS_PREPLANIFIE,
    EVENT_STATUSES,
    Event,
    EventDateHistory,
    Meeting,
    MeetingType,
    MissionSubtype,
    Project,
    Role,
    Task,
    TaskStatus,
    User,
    UserRole,
)


def _team_user_query():
    """User query for everyone with at least one non-admin role.

    Replaces the old hardcoded TEAM_ROLES tuple (planificateur + auditeurs).
    A user is on the planning if any of their roles is not the bare admin
    role — distinct() collapses duplicates from the UserRole join."""
    return (
        db.select(User)
        .join(UserRole)
        .where(UserRole.role != ROLE_ADMIN)
        .distinct()
    )


def _internal_auditor_ids(members) -> set[int]:
    """Ids of the members that drive the calendar's availability/overcharge
    colouring: *internal* collaborators holding at least one real specialty role
    (a non-system, non-provenance Role).

    Pure planificateurs / admins don't count, and external providers are
    excluded — they can carry technical missions but must never tint the
    availability cells (green) or the overcharge cells (red)."""
    return {
        m.id for m in members
        if not m.is_external
        and any(
            r not in SYSTEM_ROLES and r not in PROVENANCE_ROLE_KEYS
            for r in m.roles
        )
    }


bp = Blueprint("planning", __name__, url_prefix="/planning")


MONTH_NAMES_FR = [
    "", "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
    "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre",
]


_TIME_STEP = {"step": 900}  # HTML <input type="time" step="900"> → 15-minute granularity


class EventForm(FlaskForm):
    title = StringField("Titre", validators=[DataRequired(), Length(max=200)])
    description = TextAreaField("Description", validators=[Optional(), Length(max=2000)])
    start_date = DateField("Date de début", validators=[DataRequired()])
    start_time = TimeField("Heure de début", validators=[DataRequired()], render_kw=_TIME_STEP)
    end_date = DateField("Date de fin", validators=[DataRequired()])
    end_time = TimeField("Heure de fin", validators=[DataRequired()], render_kw=_TIME_STEP)
    meeting_type_id = SelectField("Type de mission", coerce=int, validators=[Optional()])
    meeting_subtype_id = SelectField("Sous-type", coerce=int, validators=[Optional()])
    project_id = SelectField("Projet", coerce=int, validators=[Optional()])
    # Every event must have at least one pentester assigned, regardless of type.
    participants = SelectMultipleField(
        "Participants",
        coerce=int,
        validators=[DataRequired(message="Au moins un pentester doit être sélectionné.")],
    )
    fpr_received = BooleanField("FPR reçue ?")
    fpr_url = URLField(
        "Lien vers la FPR",
        validators=[Optional(), URL(message="URL invalide."), Length(max=500)],
    )
    difficulties = TextAreaField(
        "Difficultés rencontrées",
        validators=[Optional(), Length(max=4000)],
    )

    def populate_choices(
        self,
        audit_kind: str | None = None,
        kind: str = "mission",
        include_type_id: int | None = None,
    ) -> None:
        # ``kind`` scopes which MeetingTypes the form offers:
        #   - "mission": technical missions (Audit, Retest, …) — blocking,
        #     client-bound, subtype/spécialité required.
        #   - "absence": Congé / Formation … — blocking, no client, no subtype,
        #     assignable to anyone (technical or not).
        # ``include_type_id`` force-keeps a type in the list even if it falls
        # outside the category (so an existing event with a legacy type stays
        # editable).
        self.kind = kind
        if kind == "absence":
            type_filter = (
                MeetingType.is_technical.is_(False),
                MeetingType.blocks_assignments.is_(True),
                MeetingType.allows_client.is_(False),
            )
        else:
            type_filter = (MeetingType.is_technical.is_(True),)
        mission_types = db.session.execute(
            db.select(MeetingType).where(*type_filter).order_by(MeetingType.name)
        ).scalars().all()
        if include_type_id and not any(mt.id == include_type_id for mt in mission_types):
            extra = db.session.get(MeetingType, include_type_id)
            if extra is not None:
                mission_types = sorted(
                    mission_types + [extra], key=lambda mt: mt.name
                )
        self.meeting_type_id.choices = [(0, "— choisir —")] + [
            (mt.id, mt.name) for mt in mission_types
        ]
        # Subtypes: WTForms needs the union of all subtype IDs so coercion +
        # validation accept any of them. Client-side JS filters the dropdown
        # down to the subtypes belonging to the currently chosen meeting type.
        all_subtypes = db.session.execute(
            db.select(MissionSubtype).order_by(MissionSubtype.name)
        ).scalars().all()
        self.meeting_subtype_id.choices = [(0, "— aucun —")] + [
            (s.id, s.name) for s in all_subtypes
        ]
        self.mission_subtypes = all_subtypes
        # Projects appear as "<Client> — <Project>" so the planificateur sees
        # which customer each project belongs to without having to drill in.
        projects = db.session.execute(
            db.select(Project)
            .join(Client, Project.client_id == Client.id)
            .order_by(Client.name, Project.name)
        ).scalars().all()
        self.project_id.choices = [(0, "— hors projet (Divers) —")] + [
            (p.id, p.name) for p in projects
        ]

        team = db.session.execute(_team_user_query()).scalars().all()

        # Sort: pentesters whose specialty matches the mission come first.
        # ``audit_kind`` is now a Role.key passed straight through — a user
        # matches when the same key sits in their roles. The browser re-applies
        # this rule live when the user changes the subtype select.
        def _sort_key(u: User):
            is_match = bool(audit_kind and audit_kind in u.roles)
            return (0 if is_match else 1, u.full_name.lower())

        team.sort(key=_sort_key)

        # Stored separately so the template can render <option data-roles="..."> for the JS.
        self.team_members = team

        # Mission types: expose the category-filtered list for manual
        # <option data-technical="..."> render in the template.
        self.mission_types = mission_types

        # WTForms still needs `choices` for validation/coercion. Plain labels —
        # the ★ marker is added client-side so it stays in sync with audit_kind.
        self.participants.choices = [
            (u.id, f"{u.full_name} ({u.role_label})") for u in team
        ]

    def validate(self, extra_validators=None) -> bool:  # type: ignore[override]
        ok = super().validate(extra_validators=extra_validators)
        if not ok:
            return False
        if self.start_time.data.minute % 15 != 0 or self.start_time.data.second != 0:
            self.start_time.errors.append("L'heure doit être un multiple de 15 minutes.")
            ok = False
        if self.end_time.data.minute % 15 != 0 or self.end_time.data.second != 0:
            self.end_time.errors.append("L'heure doit être un multiple de 15 minutes.")
            ok = False
        start = datetime.combine(self.start_date.data, self.start_time.data)
        end = datetime.combine(self.end_date.data, self.end_time.data)
        if end <= start:
            self.end_time.errors.append("La fin doit être postérieure au début.")
            ok = False
        # Type-driven rules. Participants are enforced by the field-level
        # DataRequired validator. The spécialité (audit_kind) is now derived
        # from the chosen subtype.
        mt = (
            db.session.get(MeetingType, self.meeting_type_id.data)
            if self.meeting_type_id.data else None
        )
        sub = (
            db.session.get(MissionSubtype, self.meeting_subtype_id.data)
            if self.meeting_subtype_id.data else None
        )
        # Drop a subtype that doesn't belong to the chosen type — e.g. user
        # picked one and then switched type without the JS resetting it.
        if sub and (mt is None or sub.meeting_type_id != mt.id):
            sub = None
            self.meeting_subtype_id.data = 0
        # A type is now mandatory in both flows (mission / absence). The filtered
        # choices already restrict which types are offered; this rejects the
        # empty "— choisir —" placeholder.
        kind = getattr(self, "kind", "mission")
        if mt is None:
            self.meeting_type_id.errors.append(
                "Choisissez un type de mission." if kind == "mission"
                else "Choisissez un type d'absence."
            )
            ok = False
        if mt:
            if mt.is_technical and sub is None:
                self.meeting_subtype_id.errors.append(
                    f"Un sous-type est obligatoire pour un événement de type « {mt.name} »."
                )
                ok = False
            # Technical missions are reserved for technical users (auditeurs).
            # The UI filters them out client-side; this is the defensive check.
            if mt.is_technical and self.participants.data:
                users = db.session.execute(
                    db.select(User).where(User.id.in_(self.participants.data))
                ).scalars().all()
                non_tech = [u for u in users if not u.is_technical]
                if non_tech:
                    names = ", ".join(u.full_name for u in non_tech)
                    self.participants.errors.append(
                        "Les missions techniques ne peuvent inclure que des auditeurs. "
                        f"Non auditeur(s) sélectionné(s) : {names}."
                    )
                    ok = False

        # Project membership rules:
        # - Internal absences (mt.allows_client == False, i.e. Congé/Formation)
        #   never carry a project — they're Divers events.
        # - Untyped events also default to Divers so the form never blocks a
        #   user who didn't pick a type.
        # - Every other event (typed, allows_client=True) must belong to a project.
        is_divers = (mt is None) or (not mt.allows_client)
        if is_divers:
            self.project_id.data = 0
        elif not self.project_id.data:
            self.project_id.errors.append(
                "Un projet est obligatoire pour ce type d'événement."
            )
            ok = False
        return ok


def _find_assignment_conflicts(
    new_start: datetime,
    new_end: datetime,
    participant_ids: set[int],
    *,
    exclude_event_id: int | None = None,
) -> list[tuple[User, Event]]:
    """Return (user, blocking_event) pairs where a participant is unavailable.

    A participant is unavailable if they sit in an *absence* (Congé, Formation —
    a blocking, non-technical type) whose [start, end] window overlaps the new
    event's window. Technical missions are also flagged blocking, but an
    audit-vs-audit overlap is only a soft warning (see _find_audit_overlaps),
    not a hard conflict — kickoff/handover days are legitimately shared.

    External providers (« prestataire ») are never returned: they may be
    overcharged / double-booked, but managing their availability isn't ours, so
    a conflict must not block assigning them to a mission.
    """
    if not participant_ids:
        return []

    candidates = db.session.execute(
        db.select(Event)
        .join(MeetingType, Event.meeting_type_id == MeetingType.id)
        .where(
            MeetingType.blocks_assignments.is_(True),
            MeetingType.is_technical.is_(False),
        )
    ).scalars().all()

    conflicts: list[tuple[User, Event]] = []
    for ev in candidates:
        if exclude_event_id is not None and ev.id == exclude_event_id:
            continue
        ev_start = datetime.combine(ev.start_date, ev.start_time)
        ev_end = datetime.combine(ev.end_date, ev.end_time)
        if not (ev_start < new_end and new_start < ev_end):
            continue
        for p in ev.participants:
            if p.id in participant_ids and not p.is_external:
                conflicts.append((p, ev))
    return conflicts


def _find_audit_overlaps(
    new_start: datetime,
    new_end: datetime,
    participant_ids: set[int],
    *,
    exclude_event_id: int | None = None,
) -> list[tuple[User, Event]]:
    """Soft-warning sibling of _find_assignment_conflicts.

    Returns (participant, existing_audit) pairs where ``participant`` is
    already on another audit mission (Event with ``role_id`` set) whose
    [start, end] window overlaps the new event's window. Used to flash a
    yellow banner on save — it does *not* block the form, since deliberate
    overlaps (kickoff/handover days) are sometimes valid."""
    if not participant_ids:
        return []
    candidates = db.session.execute(
        db.select(Event).where(Event.role_id.isnot(None))
    ).scalars().all()
    overlaps: list[tuple[User, Event]] = []
    for ev in candidates:
        if exclude_event_id is not None and ev.id == exclude_event_id:
            continue
        ev_start = datetime.combine(ev.start_date, ev.start_time)
        ev_end = datetime.combine(ev.end_date, ev.end_time)
        if not (ev_start < new_end and new_start < ev_end):
            continue
        for p in ev.participants:
            if p.id in participant_ids:
                overlaps.append((p, ev))
    return overlaps


def _format_audit_overlap_msgs(overlaps: list[tuple[User, Event]]) -> list[str]:
    msgs: list[str] = []
    seen: set[tuple[int, int]] = set()
    for user, ev in overlaps:
        key = (user.id, ev.id)
        if key in seen:
            continue
        seen.add(key)
        when = ev.start_date.strftime("%d/%m/%Y")
        if ev.end_date != ev.start_date:
            when = f"du {when} au {ev.end_date.strftime('%d/%m/%Y')}"
        title = ev.title
        msgs.append(f"{user.full_name} est déjà sur « {title} » {when}.")
    return msgs


def _format_conflict_msg(conflicts: list[tuple[User, Event]]) -> list[str]:
    msgs: list[str] = []
    seen: set[tuple[int, int]] = set()
    for user, ev in conflicts:
        key = (user.id, ev.id)
        if key in seen:
            continue
        seen.add(key)
        when = ev.start_date.strftime("%d/%m/%Y")
        if ev.end_date != ev.start_date:
            when = f"du {when} au {ev.end_date.strftime('%d/%m/%Y')}"
        type_name = ev.meeting_type.name if ev.meeting_type else "indisponibilité"
        msgs.append(f"{user.full_name} est en « {type_name} » {when}.")
    return msgs


def _collect_blocking_for_users(
    user_ids: set[int],
    from_date: date,
    horizon: date,
    exclude_event_id: int | None = None,
) -> dict[int, list[Event]]:
    """All blocking events touching [from_date, horizon], bucketed per
    participant. An event blocks an auditor iff:
      - it carries an ``audit_kind`` (technical mission, preplanified included), OR
      - its meeting type has ``blocks_assignments`` (Congé, Formation, ...).
    Meetings / restitutions never block, regardless of duration."""
    events = db.session.execute(
        db.select(Event)
        .where(Event.end_date >= from_date, Event.start_date <= horizon)
    ).scalars().all()

    by_user: dict[int, list[Event]] = {uid: [] for uid in user_ids}
    for ev in events:
        if exclude_event_id is not None and ev.id == exclude_event_id:
            continue
        is_blocker = (
            ev.audit_kind is not None
            or (ev.meeting_type and ev.meeting_type.blocks_assignments)
        )
        if not is_blocker:
            continue
        for p in ev.participants:
            if p.id in by_user:
                by_user[p.id].append(ev)
    return by_user


def _conflicts_in_range(
    blocking: list[Event], range_start: date, range_end: date
) -> list[Event]:
    """Subset of ``blocking`` that overlaps the [range_start, range_end] window."""
    return [
        ev for ev in blocking
        if ev.start_date <= range_end and ev.end_date >= range_start
    ]


def _next_free_slot(
    blocking: list[Event],
    from_date: date,
    horizon_days: int = 365,
) -> dict | None:
    """First contiguous free Mon–Fri slot starting on or after ``from_date``.

    Returns ``{start, end, days}`` (ISO strings + workday count) or ``None``
    if the pentester has no free workday within the horizon. Weekends and
    French public holidays pass through the streak without breaking it
    but aren't counted as j/h."""
    horizon = from_date + timedelta(days=horizon_days)
    blocked: set[date] = set()
    for ev in blocking:
        d = max(ev.start_date, from_date)
        end = min(ev.end_date, horizon)
        while d <= end:
            blocked.add(d)
            d += timedelta(days=1)

    def is_workable(d: date) -> bool:
        return d.weekday() < 5 and not is_french_holiday(d)

    cursor = from_date
    while cursor <= horizon and (not is_workable(cursor) or cursor in blocked):
        cursor += timedelta(days=1)
    if cursor > horizon:
        return None

    slot_start = cursor
    last_free = cursor
    days = 0
    while cursor <= horizon and cursor not in blocked:
        if is_workable(cursor):
            days += 1
            last_free = cursor
        cursor += timedelta(days=1)

    return {
        "start": slot_start.isoformat(),
        "end": last_free.isoformat(),
        "days": days,
    }


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    first = date(year, month, 1)
    last_day = _calendar.monthrange(year, month)[1]
    last = date(year, month, last_day)
    return first, last


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = (year * 12 + (month - 1)) + delta
    return idx // 12, idx % 12 + 1


CALENDAR_VIEWS = ("month", "quarter", "year")


def _build_month_panel(
    year: int, month: int, members, events, auditor_ids: set[int],
    tasks: list[Task] | None = None,
    meetings: list[Meeting] | None = None,
) -> dict:
    """Render a single month into the structure the template consumes.

    Filters ``events`` (already fetched for a wider range) down to those that
    touch this month, then buckets occurrences per (member, day_of_month).
    Also computes ``days_available``: the set of days where at least one
    auditor (web/mobile/code) is neither on leave/formation nor on an audit
    mission. Meetings do *not* count as unavailability.

    ``tasks`` / ``meetings`` are bucketed per day_of_month so the template can
    render them above the events on each cell."""
    first, last = _month_bounds(year, month)
    by_member: dict[int, dict[int, list[Event]]] = {m.id: {} for m in members}
    # Per-day set of auditor IDs that are blocked (vacation, formation, audit).
    blocked_by_day: dict[int, set[int]] = {}
    # Per-day, per-member count of blocking events (audit mission or Congé /
    # Formation type). A member with 2+ on the same day is double-booked, which
    # makes the day "overcharged" (rendered red in the cell).
    blockers_by_day_member: dict[int, dict[int, int]] = {}
    tasks_by_day: dict[int, list[Task]] = {}
    if tasks:
        for t in tasks:
            if t.due_date and t.due_date.year == year and t.due_date.month == month:
                tasks_by_day.setdefault(t.due_date.day, []).append(t)
    meetings_by_day: dict[int, list[Meeting]] = {}
    if meetings:
        for m in meetings:
            if m.date.year == year and m.date.month == month:
                meetings_by_day.setdefault(m.date.day, []).append(m)

    for ev in events:
        if ev.end_date < first or ev.start_date > last:
            continue
        # "Blocks an auditor" = explicit blocker (e.g. Congé, Formation, atelier)
        # OR the auditor is themselves running an audit mission.
        is_blocker = bool(
            (ev.meeting_type and ev.meeting_type.blocks_assignments)
            or ev.audit_kind
        )
        d = max(ev.start_date, first)
        end = min(ev.end_date, last)
        while d <= end:
            # Public holidays (and the chômé/travaillé overrides) are non-worked
            # days for the *internal* team only: their mission segments are
            # hidden and the availability/overcharge maths ignores the day (JH
            # counting already excludes them too). External providers
            # (« prestataire ») are unaffected — they may work holidays, so their
            # segments still render. Weekends are dropped later by week[:5].
            holiday = is_french_holiday(d)
            for p in ev.participants:
                if holiday and not p.is_external:
                    continue
                if p.id in by_member:
                    by_member[p.id].setdefault(d.day, []).append(ev)
                if is_blocker and p.id in auditor_ids:
                    blocked_by_day.setdefault(d.day, set()).add(p.id)
                # Overcharge (red) tracks everyone, externals included — a
                # double-booked provider should still surface as overloaded.
                # (Availability/green stays internal-only via auditor_ids above.)
                if is_blocker:
                    per_member = blockers_by_day_member.setdefault(d.day, {})
                    per_member[p.id] = per_member.get(p.id, 0) + 1
            d += timedelta(days=1)

    # Public holidays in this month → {day_of_month: name}, overrides applied.
    year_holidays = effective_holidays(year)
    holidays_by_day: dict[int, str] = {
        d.day: name for d, name in year_holidays.items() if d.month == month
    }

    # A day is "overcharged" when some collaborator carries 2+ blocking events
    # on it (e.g. a single-day mission stacked on an existing mission/absence).
    days_overcharged: set[int] = {
        day
        for day, per_member in blockers_by_day_member.items()
        if any(count > 1 for count in per_member.values())
    }

    days_available: set[int] = set()
    if auditor_ids:
        for day in range(1, _calendar.monthrange(year, month)[1] + 1):
            # Holidays are never bookable, regardless of who's free.
            if day in holidays_by_day:
                continue
            # If not every auditor is blocked, at least one is free.
            if len(blocked_by_day.get(day, set())) < len(auditor_ids):
                days_available.add(day)

    full_weeks = _calendar.Calendar(firstweekday=0).monthdayscalendar(year, month)
    weeks = [week[:5] for week in full_weeks if any(week[:5])]
    return {
        "year": year,
        "month": month,
        "month_name": MONTH_NAMES_FR[month],
        "weeks": weeks,
        "by_member": by_member,
        "days_available": days_available,
        "days_overcharged": days_overcharged,
        "holidays_by_day": holidays_by_day,
        "tasks_by_day": tasks_by_day,
        "meetings_by_day": meetings_by_day,
    }


@bp.route("/")
@login_required
def calendar_default():
    today = date.today()
    view = session.get("calendar_view", "month")
    if view not in CALENDAR_VIEWS:
        view = "month"
    return redirect(url_for("planning.calendar", year=today.year, month=today.month, view=view))


@bp.route("/<int:year>/<int:month>")
@login_required
def calendar(year: int, month: int):
    if not (1 <= month <= 12) or not (1970 <= year <= 2999):
        abort(404)

    view = request.args.get("view", session.get("calendar_view", "month"))
    if view not in CALENDAR_VIEWS:
        view = "month"
    session["calendar_view"] = view

    # Build the list of months to display, and the date range to query.
    if view == "year":
        panels_spec = [(year, m) for m in range(1, 13)]
        range_start = date(year, 1, 1)
        range_end = date(year, 12, 31)
        prev_y, prev_m = year - 1, month
        next_y, next_m = year + 1, month
        prev_label = str(year - 1)
        next_label = str(year + 1)
        view_title = str(year)
    elif view == "quarter":
        panels_spec = []
        cy, cm = year, month
        for _ in range(3):
            panels_spec.append((cy, cm))
            cy, cm = _shift_month(cy, cm, +1)
        range_start, _ = _month_bounds(*panels_spec[0])
        _, range_end = _month_bounds(*panels_spec[-1])
        prev_y, prev_m = _shift_month(year, month, -3)
        next_y, next_m = _shift_month(year, month, +3)
        prev_label = f"{MONTH_NAMES_FR[prev_m]} {prev_y}"
        next_label = f"{MONTH_NAMES_FR[next_m]} {next_y}"
        s_y, s_m = panels_spec[0]
        e_y, e_m = panels_spec[-1]
        if s_y == e_y:
            view_title = f"{MONTH_NAMES_FR[s_m]} – {MONTH_NAMES_FR[e_m]} {e_y}"
        else:
            view_title = f"{MONTH_NAMES_FR[s_m]} {s_y} – {MONTH_NAMES_FR[e_m]} {e_y}"
    else:  # month
        panels_spec = [(year, month)]
        range_start, range_end = _month_bounds(year, month)
        prev_y, prev_m = _shift_month(year, month, -1)
        next_y, next_m = _shift_month(year, month, +1)
        prev_label = f"{MONTH_NAMES_FR[prev_m]} {prev_y}"
        next_label = f"{MONTH_NAMES_FR[next_m]} {next_y}"
        view_title = f"{MONTH_NAMES_FR[month]} {year}"

    members = db.session.execute(
        _team_user_query().order_by(User.full_name)
    ).scalars().all()

    events = db.session.execute(
        db.select(Event)
        .where(Event.start_date <= range_end, Event.end_date >= range_start)
        .order_by(Event.start_date, Event.start_time)
    ).scalars().all()

    auditor_ids = _internal_auditor_ids(members)

    tasks = db.session.execute(
        db.select(Task)
        .where(Task.due_date.isnot(None))
        .where(Task.is_template.is_(False))
        .where(Task.due_date >= range_start, Task.due_date <= range_end)
        .order_by(Task.due_date, Task.name)
    ).scalars().all()

    meetings = db.session.execute(
        db.select(Meeting)
        .where(Meeting.date >= range_start, Meeting.date <= range_end)
        .order_by(Meeting.date, Meeting.start_time.is_(None), Meeting.start_time, Meeting.name)
    ).scalars().all()

    panels = [
        _build_month_panel(
            y, m, members, events, auditor_ids,
            tasks=tasks, meetings=meetings,
        )
        for (y, m) in panels_spec
    ]

    # === Synthèse "Cette semaine" — always anchored on today, not the displayed range ===
    today_real = date.today()
    week_start = today_real - timedelta(days=today_real.weekday())  # Monday
    week_end = week_start + timedelta(days=4)                        # Friday
    week_events_q = db.session.execute(
        db.select(Event)
        .where(Event.start_date <= week_end, Event.end_date >= week_start)
        .order_by(Event.start_date, Event.start_time)
    ).scalars().all()
    week_by_type: dict[str, dict] = {}
    for ev in week_events_q:
        if ev.meeting_type:
            key = ev.meeting_type.name
            color = ev.meeting_type.color
        else:
            key, color = "Sans type", "#64748b"
        bucket = week_by_type.setdefault(key, {"color": color, "events": []})
        bucket["events"].append(ev)

    week_tasks = db.session.execute(
        db.select(Task)
        .where(Task.is_template.is_(False))
        .where(Task.due_date >= week_start, Task.due_date <= week_end)
        .order_by(Task.due_date, Task.name)
    ).scalars().all()
    week_tasks_by_status: dict[str, dict] = {}
    for t in week_tasks:
        if t.status:
            key = t.status.name
            color = t.status.color
            emoji = t.status.emoji
        else:
            key, color, emoji = "Sans statut", "#64748b", None
        bucket = week_tasks_by_status.setdefault(
            key, {"color": color, "emoji": emoji, "tasks": []}
        )
        bucket["tasks"].append(t)

    week_meetings = db.session.execute(
        db.select(Meeting)
        .where(Meeting.date >= week_start, Meeting.date <= week_end)
        .order_by(Meeting.date, Meeting.start_time.is_(None), Meeting.start_time, Meeting.name)
    ).scalars().all()
    week_meetings_by_category: dict[str, dict] = {}
    for mt in week_meetings:
        if mt.category:
            key = mt.category.name
            color = mt.category.color
        else:
            key, color = "Sans type", "#a855f7"
        bucket = week_meetings_by_category.setdefault(
            key, {"color": color, "meetings": []}
        )
        bucket["meetings"].append(mt)

    week_summary = {
        "start": week_start,
        "end": week_end,
        "total": len(week_events_q),
        "type_count": len(week_by_type),
        "by_type": week_by_type,
        "tasks_total": len(week_tasks),
        "tasks_by_status": week_tasks_by_status,
        "meetings_total": len(week_meetings),
        "meetings_by_category": week_meetings_by_category,
    }

    return render_template(
        "calendar.html",
        year=year,
        month=month,
        view=view,
        view_title=view_title,
        panels=panels,
        members=members,
        prev_year=prev_y,
        prev_month=prev_m,
        prev_label=prev_label,
        next_year=next_y,
        next_month=next_m,
        next_label=next_label,
        today=date.today(),
        week_summary=week_summary,
    )


TASK_NAME_MAX = 200  # mirrors Task.name column length


def _spawn_auto_mission_tasks(ev: Event) -> int:
    """For every Task template flagged ``auto_after_mission``, create a copy on
    ``ev``'s project with due_date = ev.end_date + ``auto_offset_days`` working
    days. Returns the number of spawned tasks (0 if the event has no project
    or no template is configured)."""
    if ev.project_id is None:
        return 0
    templates = db.session.execute(
        db.select(Task)
        .where(Task.is_template.is_(True), Task.auto_after_mission.is_(True))
    ).scalars().all()
    if not templates:
        return 0
    default_status = db.session.execute(
        db.select(TaskStatus).where(TaskStatus.name == "À faire")
    ).scalar_one_or_none()
    default_status_id = default_status.id if default_status else None
    for tpl in templates:
        spawned_name = _mission_task_name(tpl.name, ev.title)
        db.session.add(Task(
            name=spawned_name,
            description=tpl.description,
            due_date=add_working_days(ev.end_date, tpl.auto_offset_days),
            status_id=default_status_id,
            is_template=False,
            project_id=ev.project_id,
            source_event_id=ev.id,
            created_by_id=ev.created_by_id,
        ))
    return len(templates)


def _mission_task_name(prefix: str, title: str) -> str:
    """Compose a spawned-task name from its template prefix and the mission
    title. The `` — <title>`` suffix is what `_rename_linked_tasks` rewrites
    when the mission is renamed."""
    return f"{prefix} — {title}"[:TASK_NAME_MAX]


def _rename_linked_tasks(event_id: int, prev_title: str, new_title: str) -> int:
    """Propagate a mission rename to its auto-spawned tasks.

    Only tasks still ending in `` — <prev_title>`` are touched, so a task the
    user has manually renamed is left alone. Returns the number updated."""
    if prev_title == new_title:
        return 0
    suffix = f" — {prev_title}"
    linked = db.session.execute(
        db.select(Task).where(Task.source_event_id == event_id)
    ).scalars().all()
    updated = 0
    for t in linked:
        if t.name.endswith(suffix):
            prefix = t.name[: -len(suffix)]
            t.name = _mission_task_name(prefix, new_title)
            updated += 1
    return updated


def _subtype_kind_from_form(form: "EventForm") -> str | None:
    """Look up the spécialité role-key of the subtype currently selected on
    ``form``, used as a hint to pre-sort the participant list by spécialité."""
    if not form.meeting_subtype_id.data:
        return None
    sub = db.session.get(MissionSubtype, form.meeting_subtype_id.data)
    return sub.role.key if (sub and sub.role) else None


def _working_days_inclusive(start: date, end: date) -> int:
    """Count Mon–Fri non-holiday days in [start, end] inclusive.

    This is the worked-day span of an event; multiplied by the pentester count
    it gives the JH (jours-homme) total. Excluding holidays keeps it the inverse
    of ``add_working_days`` so the form's JH ⇄ end_date round-trip is stable."""
    if not start or not end or end < start:
        return 0
    total = 0
    d = start
    while d <= end:
        if d.weekday() < 5 and not is_french_holiday(d):
            total += 1
        d += timedelta(days=1)
    return total


def _computed_jh(ev: Event) -> int:
    """Auto-computed JH for an existing event: worked days × pentester count.

    Mon–Fri over one week with 3 pentesters → 15 JH; a single pentester over
    4 full weeks → 20 JH."""
    return _working_days_inclusive(ev.start_date, ev.end_date) * len(ev.participants)


def _event_title_dates_taken(
    title: str,
    start_date: date,
    end_date: date,
    *,
    exclude_event_id: int | None = None,
) -> bool:
    """True if another event already uses this (title, start_date, end_date).

    Mirrors the uq_event_title_dates constraint as a pre-check (same pattern as
    projects' ``_name_taken``) so a duplicate surfaces as a friendly form error
    instead of an uncaught IntegrityError → 500."""
    q = db.select(Event.id).where(
        Event.title == title,
        Event.start_date == start_date,
        Event.end_date == end_date,
    )
    if exclude_event_id is not None:
        q = q.where(Event.id != exclude_event_id)
    return db.session.execute(q).first() is not None


def _enforce_full_working_days(form: "EventForm") -> None:
    """Push ``form.end_date`` out so the mission covers its full count of worked
    days, skipping French public holidays.

    A public holiday in the middle of an N-day mission must not silently eat a
    day: 5 days stays 5 worked days, the mission simply ends one (working) day
    later. The intended count is ``ceil(JH / nb pentesters)`` when a JH value
    was submitted, otherwise the number of week-day slots the planner laid out
    between start and end. With no holiday in the span this leaves a weekday
    range unchanged. Mirrors the form's client-side JH helper so the result is
    identical whether or not the JS ran."""
    start = form.start_date.data
    end = form.end_date.data
    if not start or not end or end < start:
        return
    target_days: int | None = None
    raw_jh = (request.form.get("jh_count") or "").strip()
    n = len(form.participants.data or [])
    if raw_jh.isdigit() and int(raw_jh) >= 1 and n >= 1:
        target_days = -(-int(raw_jh) // n)  # ceil(JH / pentesters)
    else:
        # Manual span: count the Mon–Fri days the planner selected (holidays
        # included — they represent days the planner expected to be worked).
        target_days = sum(
            1 for i in range((end - start).days + 1)
            if (start + timedelta(days=i)).weekday() < 5
        )
    if target_days < 1:
        return
    form.end_date.data = add_working_days(start, target_days - 1)


@bp.route("/events/new", methods=["GET", "POST"])
@login_required
def event_new():
    if not current_user.can_manage_events:
        abort(403)
    # Two creation flows share this view: "mission" (technical) and "absence"
    # (Congé / Formation). The kind rides a query param on GET and a hidden
    # field on POST so the right type list and rules apply throughout.
    kind = (request.values.get("kind") or "mission").strip()
    if kind not in ("mission", "absence"):
        kind = "mission"
    form = EventForm()
    form.populate_choices(audit_kind=_subtype_kind_from_form(form), kind=kind)

    if request.method == "GET":
        # Single-day default from ?date=YYYY-MM-DD; a search prefill can override
        # both ends via ?start=…&end=… (typically from the availability search).
        try:
            d = datetime.strptime(request.args.get("date", ""), "%Y-%m-%d").date()
        except ValueError:
            d = date.today()
        try:
            start_q = datetime.strptime(request.args.get("start", ""), "%Y-%m-%d").date()
        except ValueError:
            start_q = None
        try:
            end_q = datetime.strptime(request.args.get("end", ""), "%Y-%m-%d").date()
        except ValueError:
            end_q = None
        form.start_date.data = start_q or d
        form.end_date.data = end_q or start_q or d
        form.start_time.data = time(9, 0)
        form.end_time.data = time(17, 0)
        # Pre-select a project when coming from the project detail page.
        try:
            form.project_id.data = int(request.args.get("project_id", "")) or 0
        except ValueError:
            pass
        # Pre-select participants from ?participants=1&participants=2 (used by
        # the availability search to spawn an event from a found slot).
        participant_ids: list[int] = []
        for raw in request.args.getlist("participants"):
            for token in raw.split(","):
                if token.isdigit():
                    participant_ids.append(int(token))
        if participant_ids:
            form.participants.data = participant_ids
        try:
            prefill_jh = int(request.args.get("jh_count", ""))
            if prefill_jh < 1 or prefill_jh > 365:
                prefill_jh = None
        except ValueError:
            prefill_jh = None
    else:
        prefill_jh = None

    if form.validate_on_submit():
        sub = (
            db.session.get(MissionSubtype, form.meeting_subtype_id.data)
            if form.meeting_subtype_id.data else None
        )
        role_id = sub.role_id if sub else None
        mt = (
            db.session.get(MeetingType, form.meeting_type_id.data)
            if form.meeting_type_id.data else None
        )
        # Holidays never eat a worked day on a mission: extend end_date to cover
        # the full count, skipping public holidays. Leave / formation keep their
        # literal span (a holiday during a Congé isn't a day owed back).
        if mt and mt.is_technical:
            _enforce_full_working_days(form)
        # Pre-check the (title, dates) uniqueness so a duplicate is a clean form
        # error rather than an IntegrityError on commit. Run after the working-day
        # adjustment so we test the dates that will actually be persisted.
        if _event_title_dates_taken(
            form.title.data.strip(), form.start_date.data, form.end_date.data
        ):
            form.title.errors.append(
                "Un événement portant ce titre existe déjà sur ces dates."
            )
            return render_template(
                "event_form.html", form=form, mode="new", kind=kind,
                holidays_iso=_holidays_iso_for_form(form.start_date.data),
            )
        if role_id:
            conflicts = _find_assignment_conflicts(
                datetime.combine(form.start_date.data, form.start_time.data),
                datetime.combine(form.end_date.data, form.end_time.data),
                set(form.participants.data),
            )
            if conflicts:
                for msg in _format_conflict_msg(conflicts):
                    form.participants.errors.append(msg)
                return render_template(
                    "event_form.html", form=form, mode="new", kind=kind,
                    holidays_iso=_holidays_iso_for_form(form.start_date.data),
                )
        # Only technical missions enter the preplanifié → planifié devis workflow.
        initial_status = (
            EVENT_STATUS_PREPLANIFIE if (mt and mt.is_technical)
            else EVENT_STATUS_PLANIFIE
        )
        # Types that don't allow a client (Congé / Formation) are Divers events
        # and never carry a project — the form validator has already enforced this.
        project_id_value = (
            None if (mt and not mt.allows_client)
            else (form.project_id.data or None)
        )
        # Audit-related fields only meaningful if the chosen subtype carries a
        # spécialité — that's what tells us this is an audit-flavoured mission.
        has_audit = bool(role_id)
        ev = Event(
            title=form.title.data.strip(),
            description=(form.description.data or "").strip() or None,
            start_date=form.start_date.data,
            start_time=form.start_time.data,
            end_date=form.end_date.data,
            end_time=form.end_time.data,
            meeting_type_id=form.meeting_type_id.data or None,
            meeting_subtype_id=sub.id if sub else None,
            role_id=role_id,
            project_id=project_id_value,
            status=initial_status,
            fpr_received=bool(form.fpr_received.data) if has_audit else False,
            fpr_url=(form.fpr_url.data or "").strip() or None if has_audit else None,
            difficulties=(form.difficulties.data or "").strip() or None,
            created_by_id=current_user.id,
        )
        ev.participants = db.session.execute(
            db.select(User).where(User.id.in_(form.participants.data))
        ).scalars().all()
        db.session.add(ev)
        db.session.commit()
        if initial_status == EVENT_STATUS_PREPLANIFIE:
            flash(
                "Mission préplanifiée. Tenez la réunion de cadrage, puis validez le devis.",
                "success",
            )
        else:
            flash("Événement créé.", "success")
        # Soft audit-overlap warning — only relevant for audit missions (role_id set).
        # Same person on two overlapping audits isn't a hard error (kickoff/handover
        # days are legitimate) but the planificateur should know.
        if role_id:
            overlaps = _find_audit_overlaps(
                datetime.combine(form.start_date.data, form.start_time.data),
                datetime.combine(form.end_date.data, form.end_time.data),
                set(form.participants.data),
                exclude_event_id=ev.id,
            )
            for msg in _format_audit_overlap_msgs(overlaps):
                flash(f"⚠ Chevauchement d'audit — {msg}", "warning")
        # Auto-spawn project tasks from any "auto_after_mission" template, but
        # only for technical missions — meetings, holidays etc. don't trigger it.
        if mt and mt.is_technical and ev.project_id:
            spawned = _spawn_auto_mission_tasks(ev)
            if spawned:
                db.session.commit()
                flash(
                    f"{spawned} tâche(s) automatique(s) créée(s) sur le projet "
                    "(modèle « auto-mission »).",
                    "info",
                )
        if request.form.get("action") == "validate":
            return redirect(url_for("planning.calendar_default"))
        return redirect(url_for("planning.event_edit", event_id=ev.id))

    return render_template(
        "event_form.html", form=form, mode="new", kind=kind,
        holidays_iso=_holidays_iso_for_form(form.start_date.data),
        prefill_jh=prefill_jh,
    )


@bp.route("/events/<int:event_id>/edit", methods=["GET", "POST"])
@login_required
def event_edit(event_id: int):
    ev = db.session.get(Event, event_id)
    if ev is None:
        abort(404)
    if not current_user.can_manage_events:
        abort(403)

    form = EventForm(obj=ev)
    if request.method == "GET":
        form.participants.data = [p.id for p in ev.participants]
        form.meeting_type_id.data = ev.meeting_type_id or 0
        form.meeting_subtype_id.data = ev.meeting_subtype_id or 0
        form.project_id.data = ev.project_id or 0
        form.fpr_received.data = ev.fpr_received
        form.fpr_url.data = ev.fpr_url or ""
        # Always pre-fill the JH field from the stored event: worked days
        # (Mon–Fri, holidays excluded) × number of pentesters.
        prefill_jh = _computed_jh(ev) or None
    else:
        # On a failed submit, keep whatever the user typed rather than reverting.
        raw_jh = (request.form.get("jh_count") or "").strip()
        prefill_jh = int(raw_jh) if raw_jh.isdigit() else None

    # The flow (mission / absence) follows the event's own type so the form
    # renders and validates with the right rules. The current type is force-kept
    # in the dropdown even if it predates the technical/absence split.
    emt = ev.meeting_type
    if emt is not None and not emt.is_technical and emt.blocks_assignments and not emt.allows_client:
        kind = "absence"
    else:
        kind = "mission"
    form.populate_choices(
        audit_kind=_subtype_kind_from_form(form) or ev.audit_kind,
        kind=kind,
        include_type_id=ev.meeting_type_id,
    )

    if form.validate_on_submit():
        sub = (
            db.session.get(MissionSubtype, form.meeting_subtype_id.data)
            if form.meeting_subtype_id.data else None
        )
        new_role_id = sub.role_id if sub else None
        new_mt = (
            db.session.get(MeetingType, form.meeting_type_id.data)
            if form.meeting_type_id.data else None
        )
        # Same rule as event_new: a holiday never shortens a mission's worked
        # days — extend the end across it. Leave / formation are left untouched.
        if new_mt and new_mt.is_technical:
            _enforce_full_working_days(form)
        # Same uniqueness pre-check as event_new, excluding this event itself.
        if _event_title_dates_taken(
            form.title.data.strip(), form.start_date.data, form.end_date.data,
            exclude_event_id=ev.id,
        ):
            form.title.errors.append(
                "Un événement portant ce titre existe déjà sur ces dates."
            )
            return render_template(
                "event_form.html", form=form, mode="edit", event=ev, kind=kind,
                holidays_iso=_holidays_iso_for_form(form.start_date.data),
                prefill_jh=prefill_jh,
            )
        if new_role_id:
            conflicts = _find_assignment_conflicts(
                datetime.combine(form.start_date.data, form.start_time.data),
                datetime.combine(form.end_date.data, form.end_time.data),
                set(form.participants.data),
                exclude_event_id=ev.id,
            )
            if conflicts:
                for msg in _format_conflict_msg(conflicts):
                    form.participants.errors.append(msg)
                return render_template(
                    "event_form.html", form=form, mode="edit", event=ev, kind=kind,
                    holidays_iso=_holidays_iso_for_form(form.start_date.data),
                    prefill_jh=prefill_jh,
                )
        # Snapshot the current date / status BEFORE applying the form data.
        # If the dates end up changing, we'll log the old values as history.
        prev_start_date = ev.start_date
        prev_end_date = ev.end_date
        prev_start_time = ev.start_time
        prev_end_time = ev.end_time
        prev_status = ev.status
        prev_title = ev.title

        ev.title = form.title.data.strip()
        # Auto-spawned tasks inherit the mission title — keep them in sync.
        _rename_linked_tasks(ev.id, prev_title, ev.title)
        ev.description = (form.description.data or "").strip() or None
        ev.start_date = form.start_date.data
        ev.start_time = form.start_time.data
        ev.end_date = form.end_date.data
        ev.end_time = form.end_time.data
        ev.meeting_type_id = form.meeting_type_id.data or None
        ev.meeting_subtype_id = sub.id if sub else None
        ev.role_id = new_role_id
        # Non-technical events skip the devis workflow — keep them in 'planifié'.
        # (new_mt was resolved above, before the working-day enforcement.)
        # Divers types (Congé, Formation) carry no project.
        ev.project_id = (
            None if (new_mt and not new_mt.allows_client)
            else (form.project_id.data or None)
        )
        if not (new_mt and new_mt.is_technical):
            ev.status = EVENT_STATUS_PLANIFIE
        if ev.role_id:
            ev.fpr_received = bool(form.fpr_received.data)
            ev.fpr_url = (form.fpr_url.data or "").strip() or None
        else:
            # Non-audit events never carry an FPR.
            ev.fpr_received = False
            ev.fpr_url = None
        ev.difficulties = (form.difficulties.data or "").strip() or None
        ev.participants = db.session.execute(
            db.select(User).where(User.id.in_(form.participants.data))
        ).scalars().all()
        # Stamp the modifier; updated_at is set automatically via onupdate.
        ev.updated_by_id = current_user.id

        # Date trail: any change in start_date or end_date is logged, with the
        # status that was in effect at the time of the change. Times-only edits
        # don't create an entry (the user explicitly asked for "date history").
        if prev_start_date != ev.start_date or prev_end_date != ev.end_date:
            db.session.add(EventDateHistory(
                event_id=ev.id,
                previous_start_date=prev_start_date,
                previous_end_date=prev_end_date,
                previous_start_time=prev_start_time,
                previous_end_time=prev_end_time,
                previous_status=prev_status,
                changed_by_id=current_user.id,
            ))

        db.session.commit()
        flash("Événement modifié.", "success")
        # Soft audit-overlap warning, same rule as event_new — exclude self.
        if new_role_id:
            overlaps = _find_audit_overlaps(
                datetime.combine(form.start_date.data, form.start_time.data),
                datetime.combine(form.end_date.data, form.end_time.data),
                set(form.participants.data),
                exclude_event_id=ev.id,
            )
            for msg in _format_audit_overlap_msgs(overlaps):
                flash(f"⚠ Chevauchement d'audit — {msg}", "warning")
        if request.form.get("action") == "validate":
            return redirect(url_for("planning.calendar_default"))
        return redirect(url_for("planning.event_edit", event_id=ev.id))

    return render_template(
        "event_form.html", form=form, mode="edit", event=ev, kind=kind,
        holidays_iso=_holidays_iso_for_form(form.start_date.data),
        prefill_jh=prefill_jh,
    )


@bp.route("/events/<int:event_id>/validate-devis", methods=["POST"])
@login_required
def event_validate_devis(event_id: int):
    """Mark a preplanned event as planned (the client has validated the quote)."""
    if not current_user.can_manage_events:
        abort(403)
    ev = db.session.get(Event, event_id)
    if ev is None:
        abort(404)
    if ev.status == EVENT_STATUS_PLANIFIE:
        flash("Cet événement est déjà planifié.", "info")
        return redirect(url_for("planning.event_edit", event_id=ev.id))
    if not (ev.meeting_type and ev.meeting_type.is_technical):
        # Defensive: only technical missions use the devis workflow.
        ev.status = EVENT_STATUS_PLANIFIE
        db.session.commit()
        flash("Événement marqué comme planifié.", "info")
        return redirect(url_for("planning.event_edit", event_id=ev.id))

    # Pre-flight checks for audit missions before they become planifié.
    if ev.audit_kind:
        if len(ev.participants) < 1:
            flash(
                "Une mission planifiée doit comporter au moins un pentester. "
                "Ajoutez un participant avant de valider le devis.",
                "danger",
            )
            return redirect(url_for("planning.event_edit", event_id=ev.id))
        if not ev.fpr_received:
            flash(
                "Impossible de planifier la mission : la FPR n'a pas encore été reçue.",
                "danger",
            )
            return redirect(url_for("planning.event_edit", event_id=ev.id))

    ev.status = EVENT_STATUS_PLANIFIE
    ev.updated_by_id = current_user.id
    db.session.commit()
    flash("Devis validé. La mission est maintenant planifiée.", "success")
    return redirect(url_for("planning.event_edit", event_id=ev.id))


@bp.route("/events/<int:event_id>/delete", methods=["POST"])
@login_required
def event_delete(event_id: int):
    ev = db.session.get(Event, event_id)
    if ev is None:
        abort(404)
    if not current_user.can_manage_events:
        abort(403)
    year, month = ev.start_date.year, ev.start_date.month
    db.session.delete(ev)
    db.session.commit()
    flash("Événement supprimé.", "info")
    return redirect(url_for("planning.calendar", year=year, month=month))


@bp.route("/availability")
@login_required
def availability():
    """JSON: per-pentester availability for [start, end], plus the next free
    slot for occupied ones. Drives the live preview on the event form when
    creating a technical or blocking mission."""
    if not current_user.can_manage_events:
        abort(403)
    try:
        start = datetime.strptime(request.args.get("start", ""), "%Y-%m-%d").date()
        end = datetime.strptime(request.args.get("end", ""), "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "invalid_date"}), 400
    if end < start:
        return jsonify({"error": "end_before_start"}), 400

    exclude_raw = request.args.get("exclude_event_id", "")
    exclude_id = int(exclude_raw) if exclude_raw.isdigit() else None

    members = db.session.execute(
        _team_user_query().order_by(User.full_name)
    ).scalars().all()
    user_ids = {m.id for m in members}

    horizon = start + timedelta(days=365)
    by_user = _collect_blocking_for_users(
        user_ids, start, horizon, exclude_event_id=exclude_id
    )

    users_out = []
    for u in members:
        blocking = by_user.get(u.id, [])
        conflicts_evs = _conflicts_in_range(blocking, start, end)
        is_available = not conflicts_evs
        next_free = None if is_available else _next_free_slot(blocking, start)
        users_out.append({
            "id": u.id,
            "name": u.full_name,
            "color": u.color,
            "available": is_available,
            "conflicts": [
                {
                    "title": ev.title,
                    "start": ev.start_date.isoformat(),
                    "end": ev.end_date.isoformat(),
                    "type": ev.meeting_type.name if ev.meeting_type else None,
                    "kind": ev.role.label if ev.role else None,
                }
                for ev in conflicts_evs
            ],
            "next_free": next_free,
        })

    return jsonify({"users": users_out})
