"""Personal dashboard ('Ma semaine'): a per-user landing page that surfaces
the missions current_user is on this week, their upcoming tasks, and any
audit they're a participant on whose FPR is still pending.

Available to every logged-in user. The root route ``/`` redirects users
without ``can_manage_events`` here instead of dropping them on the global
calendar — pentesters get a focused view of their own work first.
"""
from __future__ import annotations

from datetime import date, timedelta

from flask import Blueprint, render_template
from flask_login import current_user, login_required

from .extensions import db
from .models import Event, Task


bp = Blueprint("dashboard", __name__, url_prefix="/me")


UPCOMING_DAYS = 30


def _week_bounds(today: date) -> tuple[date, date]:
    """ISO week (Monday → Sunday) containing ``today``."""
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=6)


@bp.route("/")
@login_required
def home():
    today = date.today()
    week_start, week_end = _week_bounds(today)
    horizon = today + timedelta(days=UPCOMING_DAYS)

    # Missions where the current user is a participant, overlapping this week
    # or the next 30 days — split so the template can render two sections.
    my_events = db.session.execute(
        db.select(Event)
        .where(Event.participants.any(id=current_user.id))
        .order_by(Event.start_date, Event.start_time)
    ).scalars().all()

    this_week: list[Event] = []
    upcoming: list[Event] = []
    pending_fpr: list[Event] = []
    for ev in my_events:
        if ev.end_date < today:
            continue  # past — out of scope for a 'what's next' view
        if ev.start_date <= week_end and ev.end_date >= week_start:
            this_week.append(ev)
        elif ev.start_date <= horizon:
            upcoming.append(ev)
        # FPR still pending — only meaningful on audit missions.
        if ev.fpr_missing:
            pending_fpr.append(ev)

    # Tasks the user can act on: any task due in the next 30 days, on a project
    # they've participated in (= has at least one of their events) OR the task
    # has no project (orphan task). Past-due tasks surface too.
    my_project_ids = {ev.project_id for ev in my_events if ev.project_id}
    tasks_all = db.session.execute(
        db.select(Task)
        .where(Task.is_template.is_(False))
        .where(Task.due_date.isnot(None))
        .where(Task.due_date <= horizon)
        .order_by(Task.due_date)
    ).scalars().all()
    tasks_relevant = [
        t for t in tasks_all
        if t.project_id is None or t.project_id in my_project_ids
    ]

    return render_template(
        "dashboard.html",
        today=today,
        week_start=week_start,
        week_end=week_end,
        this_week=this_week,
        upcoming=upcoming,
        pending_fpr=pending_fpr,
        tasks=tasks_relevant,
        horizon_days=UPCOMING_DAYS,
    )
