"""Project CRUD + the cross-project synthesis & CSV export.

A Project is the top-level container that groups, under a single client:
the audit missions (technical Events), the accompanying meetings (cadrage,
restitution, standalones) and the follow-up tasks. Internal absences
(formation, congé) are out-of-project: they carry no project_id and surface
on the planning rather than under any project."""
from __future__ import annotations

import calendar as _calendar
import csv
from collections import defaultdict
from datetime import date, timedelta
from io import StringIO

from flask import (
    Blueprint, Response, abort, flash, jsonify, redirect, render_template, request, url_for,
)
from sqlalchemy import update
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from wtforms import SelectField, StringField, TextAreaField
from wtforms.validators import DataRequired, Length, Optional

from .extensions import db
from .holidays import is_french_holiday
from .models import (
    Client,
    Event,
    Meeting,
    PROJECT_STATUS_ACTIVE,
    PROJECT_STATUS_CLOSED,
    PROJECT_STATUSES,
    Project,
    Task,
)


bp = Blueprint("projects", __name__, url_prefix="/projects")


# === Mission status derivation (re-used from the old mission view) ===

STATUS_PREPLANIFIE = "preplanifie"
STATUS_PLANIFIE = "planifie"
STATUS_EN_COURS = "en_cours"
STATUS_TERMINE = "termine"

STATUS_LABELS: dict[str, str] = {
    STATUS_PREPLANIFIE: "Préplanifié",
    STATUS_PLANIFIE:    "Planifié",
    STATUS_EN_COURS:    "En cours",
    STATUS_TERMINE:     "Terminé",
}

STATUS_ORDER = (STATUS_PREPLANIFIE, STATUS_PLANIFIE, STATUS_EN_COURS, STATUS_TERMINE)


MONTH_NAMES_FR = [
    "", "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
    "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre",
]


def _require_manage() -> None:
    if not current_user.can_manage_events:
        abort(403)


def _effective_status(event: Event, today: date) -> str:
    if event.status == STATUS_PREPLANIFIE:
        return STATUS_PREPLANIFIE
    if event.end_date < today:
        return STATUS_TERMINE
    if event.start_date <= today <= event.end_date:
        return STATUS_EN_COURS
    return STATUS_PLANIFIE


def _workdays(start: date, end: date) -> int:
    if end < start:
        return 0
    count = 0
    d = start
    while d <= end:
        if d.weekday() < 5 and not is_french_holiday(d):
            count += 1
        d += timedelta(days=1)
    return count


def _person_days(event: Event) -> int:
    return _workdays(event.start_date, event.end_date) * len(event.participants)


# === Project CRUD ===

class ProjectForm(FlaskForm):
    name = StringField("Nom", validators=[DataRequired(), Length(min=2, max=200)])
    code = StringField(
        "Référence interne", validators=[Optional(), Length(max=40)],
    )
    client_id = SelectField("Client", coerce=int, validators=[DataRequired()])
    status = SelectField(
        "Statut", validators=[DataRequired()],
        choices=[(k, v) for k, v in PROJECT_STATUSES.items()],
        default=PROJECT_STATUS_ACTIVE,
    )
    description = TextAreaField(
        "Description", validators=[Optional(), Length(max=4000)],
    )

    def populate_choices(self) -> None:
        clients = db.session.execute(
            db.select(Client).order_by(Client.name)
        ).scalars().all()
        self.client_id.choices = [(0, "— choisir un client —")] + [
            (c.id, c.name) for c in clients
        ]


def _name_taken(client_id: int, name: str, exclude_id: int | None = None) -> bool:
    q = db.select(Project.id).where(
        Project.client_id == client_id, Project.name == name,
    )
    if exclude_id is not None:
        q = q.where(Project.id != exclude_id)
    return db.session.execute(q).scalar_one_or_none() is not None


def _project_summary(project: Project, today: date) -> dict:
    """Aggregate stats for the project list table."""
    missions = project.missions
    person_days = sum(_person_days(m) for m in missions)
    open_missions = sum(
        1 for m in missions if _effective_status(m, today) != STATUS_TERMINE
    )
    return {
        "project": project,
        "mission_count": len(missions),
        "meeting_count": len(project.meetings),
        "task_count": len(project.tasks),
        "person_days": person_days,
        "open_missions": open_missions,
    }


@bp.route("/")
@login_required
def projects_list():
    today = date.today()
    projects = db.session.execute(
        db.select(Project)
        .join(Client, Project.client_id == Client.id)
        .order_by(Project.status, Client.name, Project.name)
    ).scalars().all()
    rows = [_project_summary(p, today) for p in projects]
    return render_template(
        "projects_list.html",
        rows=rows,
        status_labels=PROJECT_STATUSES,
    )


@bp.route("/options.json")
@login_required
def options_json():
    """Project choices as JSON — lets the event form's project dropdown refresh
    its list (e.g. after a project was just created in another tab) without a
    full page reload. Mirrors EventForm.populate_choices' project labels."""
    projects = db.session.execute(
        db.select(Project)
        .join(Client, Project.client_id == Client.id)
        .order_by(Client.name, Project.name)
    ).scalars().all()
    options = [{"value": 0, "label": "— hors projet (Divers) —"}] + [
        {"value": p.id, "label": p.name} for p in projects
    ]
    return jsonify(options=options)


@bp.route("/<int:project_id>")
@login_required
def project_detail(project_id: int):
    p = db.session.get(Project, project_id)
    if p is None:
        abort(404)
    today = date.today()
    missions_decorated = [
        {
            "event": m,
            "status": _effective_status(m, today),
            "status_label": STATUS_LABELS[_effective_status(m, today)],
            "audit_kind_label": m.role.label if m.role else "—",
            "person_days": _person_days(m),
            "auditors": sorted(m.participants, key=lambda u: u.full_name),
        }
        for m in sorted(p.missions, key=lambda e: (e.start_date, e.start_time))
    ]
    total_pd = sum(r["person_days"] for r in missions_decorated)
    meetings_sorted = sorted(p.meetings, key=lambda m: m.date)
    tasks_sorted = sorted(
        p.tasks, key=lambda t: (t.due_date or date.max, t.name),
    )
    return render_template(
        "project_detail.html",
        project=p,
        missions=missions_decorated,
        meetings=meetings_sorted,
        tasks=tasks_sorted,
        total_person_days=total_pd,
        status_labels=PROJECT_STATUSES,
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
def project_new():
    _require_manage()
    form = ProjectForm()
    form.populate_choices()

    # Pre-select a client when arriving from /clients (?client_id=...).
    if request.method == "GET":
        try:
            cid = int(request.args.get("client_id", ""))
            form.client_id.data = cid
        except ValueError:
            pass

    if form.validate_on_submit():
        cid = form.client_id.data
        client = db.session.get(Client, cid) if cid else None
        if client is None:
            form.client_id.errors.append("Choisissez un client valide.")
        else:
            name = form.name.data.strip()
            if _name_taken(cid, name):
                form.name.errors.append(
                    f"Un projet « {name} » existe déjà chez ce client.",
                )
            else:
                p = Project(
                    name=name,
                    code=(form.code.data or "").strip() or None,
                    description=(form.description.data or "").strip() or None,
                    status=form.status.data,
                    client_id=cid,
                    created_by_id=current_user.id,
                )
                db.session.add(p)
                db.session.commit()
                flash(f"Projet « {p.name} » créé.", "success")
                return redirect(url_for("projects.project_detail", project_id=p.id))

    return render_template("project_form.html", form=form, mode="new")


@bp.route("/<int:project_id>/edit", methods=["GET", "POST"])
@login_required
def project_edit(project_id: int):
    _require_manage()
    p = db.session.get(Project, project_id)
    if p is None:
        abort(404)
    form = ProjectForm(obj=p)
    form.populate_choices()
    if request.method == "GET":
        form.client_id.data = p.client_id
        form.status.data = p.status

    if form.validate_on_submit():
        cid = form.client_id.data
        client = db.session.get(Client, cid) if cid else None
        if client is None:
            form.client_id.errors.append("Choisissez un client valide.")
        else:
            new_name = form.name.data.strip()
            if _name_taken(cid, new_name, exclude_id=p.id):
                form.name.errors.append(
                    f"Un projet « {new_name} » existe déjà chez ce client.",
                )
            else:
                p.name = new_name
                p.code = (form.code.data or "").strip() or None
                p.description = (form.description.data or "").strip() or None
                p.status = form.status.data
                p.client_id = cid
                db.session.commit()
                flash(f"Projet « {p.name} » mis à jour.", "success")
                return redirect(url_for("projects.project_detail", project_id=p.id))

    return render_template("project_form.html", form=form, mode="edit", project=p)


@bp.route("/<int:project_id>/delete", methods=["POST"])
@login_required
def project_delete(project_id: int):
    _require_manage()
    p = db.session.get(Project, project_id)
    if p is None:
        abort(404)
    name = p.name
    # Detach any task that points back at one of this project's missions via
    # source_event_id (that FK has no ORM relationship, so the cascade below
    # would otherwise rely solely on the DB constraint). Nulling them first
    # guarantees the wipe can't trip an integrity error and abort.
    mission_ids = [m.id for m in p.missions]
    if mission_ids:
        db.session.execute(
            update(Task)
            .where(Task.source_event_id.in_(mission_ids))
            .values(source_event_id=None)
        )
    db.session.delete(p)  # ORM cascade wipes missions / meetings / tasks
    db.session.commit()
    flash(
        f"Projet « {name} » supprimé "
        "(missions, réunions et tâches associées supprimées également).",
        "info",
    )
    return redirect(url_for("projects.projects_list"))


# === Synthesis (kept; now genuinely cross-project) ===

def _monthly_person_days(missions: list[Event]) -> list[dict]:
    bucket: dict[tuple[int, int], int] = defaultdict(int)
    for ev in missions:
        n_part = len(ev.participants)
        if n_part == 0:
            continue
        d = ev.start_date
        while d <= ev.end_date:
            if d.weekday() < 5 and not is_french_holiday(d):
                bucket[(d.year, d.month)] += n_part
            d += timedelta(days=1)
    return [
        {
            "year": y,
            "month": m,
            "label": f"{MONTH_NAMES_FR[m]} {y}",
            "person_days": count,
        }
        for (y, m), count in sorted(bucket.items(), reverse=True)
    ]


def _client_breakdown(missions: list[Event]) -> dict:
    NO_CLIENT_KEY = (None, "— Sans projet —")
    buckets: dict[tuple[int | None, str], dict] = {}

    for ev in missions:
        c = ev.client
        key = (c.id, c.name) if c else NO_CLIENT_KEY
        bucket = buckets.setdefault(key, {
            "client_id": key[0],
            "name": key[1],
            "mission_count": 0,
            "person_days": 0,
            "kinds": set(),
        })
        bucket["mission_count"] += 1
        bucket["person_days"] += _person_days(ev)
        if ev.role:
            bucket["kinds"].add(ev.role.label)

    rows = sorted(
        buckets.values(),
        key=lambda b: (b["client_id"] is None, -b["person_days"], b["name"].lower()),
    )
    for r in rows:
        r["kinds"] = sorted(r["kinds"])

    total_pd = sum(r["person_days"] for r in rows)
    max_pd = max((r["person_days"] for r in rows), default=0)
    return {
        "rows": rows,
        "total_person_days": total_pd,
        "max_person_days": max_pd,
        "named_person_days": sum(
            r["person_days"] for r in rows if r["client_id"] is not None
        ),
        "client_count": sum(1 for r in rows if r["client_id"] is not None),
    }


def _pentester_breakdown(missions: list[Event]) -> dict:
    matrix: dict[int, dict[tuple[int, int], int]] = defaultdict(lambda: defaultdict(int))
    users_by_id: dict[int, "object"] = {}
    months_set: set[tuple[int, int]] = set()

    for ev in missions:
        if not ev.participants:
            continue
        d = ev.start_date
        while d <= ev.end_date:
            if d.weekday() < 5 and not is_french_holiday(d):
                key = (d.year, d.month)
                months_set.add(key)
                for p in ev.participants:
                    matrix[p.id][key] += 1
                    users_by_id[p.id] = p
            d += timedelta(days=1)

    months = sorted(months_set, reverse=True)
    months_labeled = [
        {"year": y, "month": m, "label": f"{MONTH_NAMES_FR[m]} {y}"}
        for (y, m) in months
    ]
    pentesters = sorted(users_by_id.values(), key=lambda u: u.full_name.lower())

    rows = []
    for p in pentesters:
        cells = [matrix[p.id].get((y, m), 0) for (y, m) in months]
        rows.append({
            "user": p,
            "cells": cells,
            "total": sum(cells),
        })

    totals_by_month = [
        sum(matrix[p.id].get((y, m), 0) for p in pentesters)
        for (y, m) in months
    ]
    grand_total = sum(r["total"] for r in rows)
    max_cell = max((c for r in rows for c in r["cells"]), default=0)
    max_total = max((r["total"] for r in rows), default=0)

    return {
        "months": months_labeled,
        "rows": rows,
        "totals_by_month": totals_by_month,
        "grand_total": grand_total,
        "max_cell": max_cell,
        "max_total": max_total,
    }


def _load_missions(
    year: int | None = None, client_id: int | None = None,
) -> list[Event]:
    """Audit missions (Event with a spécialité role), newest first.

    Optional filters narrow the dataset before the per-table breakdowns run.
    A mission is kept when it overlaps the requested calendar year and (when
    ``client_id`` is set) when its parent project belongs to that client."""
    q = db.select(Event).where(Event.role_id.isnot(None))
    if year is not None:
        first = date(year, 1, 1)
        last = date(year, 12, 31)
        q = q.where(Event.start_date <= last, Event.end_date >= first)
    if client_id is not None:
        q = (
            q.join(Project, Event.project_id == Project.id)
            .where(Project.client_id == client_id)
        )
    return db.session.execute(
        q.order_by(Event.start_date.desc(), Event.start_time.desc())
    ).scalars().all()


def _parse_synthesis_filters() -> tuple[int | None, int | None]:
    """(year, client_id) from query, both optional, both validated."""
    year_raw = (request.args.get("year") or "").strip()
    client_raw = (request.args.get("client_id") or "").strip()
    year: int | None = None
    if year_raw:
        try:
            y = int(year_raw)
            if 1970 <= y <= 2999:
                year = y
        except ValueError:
            pass
    client_id: int | None = None
    if client_raw:
        try:
            cid = int(client_raw)
            client_id = cid if cid > 0 else None
        except ValueError:
            pass
    return year, client_id


def _summary(missions: list[Event], today: date) -> dict:
    counts = {s: 0 for s in STATUS_ORDER}
    total_pd = 0
    for ev in missions:
        counts[_effective_status(ev, today)] += 1
        total_pd += _person_days(ev)
    return {
        "total": len(missions),
        "total_person_days": total_pd,
        "by_status": [(s, STATUS_LABELS[s], counts[s]) for s in STATUS_ORDER],
    }


def _available_months(missions: list[Event]) -> list[dict]:
    months: set[tuple[int, int]] = set()
    for ev in missions:
        d = ev.start_date
        while d <= ev.end_date:
            if d.weekday() < 5 and not is_french_holiday(d):
                months.add((d.year, d.month))
            d += timedelta(days=1)
    return [
        {"key": f"{y:04d}-{m:02d}", "label": f"{MONTH_NAMES_FR[m]} {y}"}
        for (y, m) in sorted(months, reverse=True)
    ]


@bp.route("/synthesis")
@login_required
def projects_synthesis():
    today = date.today()
    year, client_id = _parse_synthesis_filters()
    missions = _load_missions(year=year, client_id=client_id)
    summary = _summary(missions, today)
    monthly = _monthly_person_days(missions)
    monthly_max = max((m["person_days"] for m in monthly), default=0)
    pentester = _pentester_breakdown(missions)
    client = _client_breakdown(missions)
    months_available = _available_months(missions)

    years_available = sorted(
        # Years pulled from every recorded mission (so filtering options stay
        # stable even when the current filter narrowed the result to one year).
        {ev.start_date.year for ev in db.session.execute(
            db.select(Event).where(Event.role_id.isnot(None))
        ).scalars().all()},
        reverse=True,
    )
    clients_available = db.session.execute(
        db.select(Client).order_by(Client.name)
    ).scalars().all()

    return render_template(
        "projects_synthesis.html",
        summary=summary,
        monthly=monthly,
        monthly_max=monthly_max,
        pentester=pentester,
        client=client,
        months_available=months_available,
        years_available=years_available,
        clients_available=clients_available,
        selected_year=year,
        selected_client_id=client_id,
        current_year=today.year,
    )


@bp.route("/export.csv")
@login_required
def projects_export_csv():
    """CSV of every audit mission. With ?month=YYYY-MM, restrict to that month
    and report person-days actually worked that month."""
    month_str = (request.args.get("month") or "").strip()
    target_year: int | None = None
    target_month: int | None = None
    if month_str:
        try:
            year_part, month_part = month_str.split("-", 1)
            target_year, target_month = int(year_part), int(month_part)
            if not (1 <= target_month <= 12 and 1970 <= target_year <= 2999):
                raise ValueError
        except ValueError:
            abort(400)

    missions = db.session.execute(
        db.select(Event)
        .where(Event.role_id.isnot(None))
        .order_by(Event.start_date, Event.start_time)
    ).scalars().all()

    buf = StringIO()
    buf.write("﻿")
    writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(["Projet", "Client", "Mission", "Date de début", "Date de fin", "Jours/homme"])

    if target_year is None:
        for ev in missions:
            writer.writerow([
                ev.project.name if ev.project else "",
                ev.client.name if ev.client else "",
                ev.title,
                ev.start_date.isoformat(),
                ev.end_date.isoformat(),
                _person_days(ev),
            ])
        filename = f"projets-tous-{date.today().isoformat()}.csv"
    else:
        first = date(target_year, target_month, 1)
        last = date(
            target_year, target_month,
            _calendar.monthrange(target_year, target_month)[1],
        )
        for ev in missions:
            slice_start = max(ev.start_date, first)
            slice_end = min(ev.end_date, last)
            days_in_month = _workdays(slice_start, slice_end)
            pd = days_in_month * len(ev.participants)
            if pd == 0:
                continue
            writer.writerow([
                ev.project.name if ev.project else "",
                ev.client.name if ev.client else "",
                ev.title,
                ev.start_date.isoformat(),
                ev.end_date.isoformat(),
                pd,
            ])
        filename = f"projets-{target_year:04d}-{target_month:02d}.csv"

    response = Response(buf.getvalue(), mimetype="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# ===== Advanced export (multi-section CSV) =====

def _parse_year_month(year_raw: str, month_raw: str) -> tuple[int | None, int | None]:
    """Validate optional year / month query params. Returns (year, month) where
    each is None when unset. Year must be 4-digit; month requires year."""
    year: int | None = None
    month: int | None = None
    if year_raw:
        try:
            year = int(year_raw)
            if not (1970 <= year <= 2999):
                year = None
        except ValueError:
            year = None
    if month_raw and year is not None:
        try:
            month = int(month_raw)
            if not (1 <= month <= 12):
                month = None
        except ValueError:
            month = None
    return year, month


def _period_bounds(year: int | None, month: int | None) -> tuple[date | None, date | None]:
    """Resolve the (year, month) filter into (period_start, period_end) inclusive.
    Returns (None, None) when no filter — caller treats it as 'all dates'."""
    if year is None:
        return None, None
    if month is None:
        return date(year, 1, 1), date(year, 12, 31)
    last_day = _calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def _project_touches_period(p: Project, start: date, end: date) -> bool:
    """A project is in scope when ANY of its missions/meetings/tasks overlap
    the [start, end] window (inclusive)."""
    for ev in p.missions:
        if ev.start_date <= end and ev.end_date >= start:
            return True
    for mt in p.meetings:
        if start <= mt.date <= end:
            return True
    for t in p.tasks:
        if t.due_date and start <= t.due_date <= end:
            return True
    return False


def _filter_projects_for_export(
    year: int | None, month: int | None, client_id: int | None
) -> list[Project]:
    q = (
        db.select(Project)
        .join(Client, Project.client_id == Client.id)
        .order_by(Client.name, Project.name)
    )
    if client_id:
        q = q.where(Project.client_id == client_id)
    projects = db.session.execute(q).scalars().all()
    period_start, period_end = _period_bounds(year, month)
    if period_start is not None and period_end is not None:
        projects = [
            p for p in projects
            if _project_touches_period(p, period_start, period_end)
        ]
    return projects


def _years_with_activity() -> list[int]:
    """Distinct years that have at least one mission, meeting or task. Newest
    first so the dropdown defaults to recent periods."""
    years: set[int] = set()
    for d in db.session.execute(db.select(Event.start_date)).scalars().all():
        years.add(d.year)
    for d in db.session.execute(db.select(Event.end_date)).scalars().all():
        years.add(d.year)
    for d in db.session.execute(db.select(Meeting.date)).scalars().all():
        years.add(d.year)
    for d in db.session.execute(db.select(Task.due_date)).scalars().all():
        if d is not None:
            years.add(d.year)
    return sorted(years, reverse=True)


@bp.route("/export")
@login_required
def export_form():
    """Render the filter + project-picker page that feeds /projects/export-all.csv."""
    _require_manage()
    year_raw = (request.args.get("year") or "").strip()
    month_raw = (request.args.get("month") or "").strip()
    client_raw = (request.args.get("client_id") or "").strip()
    year, month = _parse_year_month(year_raw, month_raw)
    client_id: int | None = None
    if client_raw:
        try:
            client_id = int(client_raw) or None
        except ValueError:
            client_id = None

    projects = _filter_projects_for_export(year, month, client_id)
    clients = db.session.execute(
        db.select(Client).order_by(Client.name)
    ).scalars().all()

    return render_template(
        "projects_export.html",
        projects=projects,
        clients=clients,
        years=_years_with_activity(),
        months=[(i, MONTH_NAMES_FR[i]) for i in range(1, 13)],
        selected_year=year,
        selected_month=month,
        selected_client_id=client_id,
    )


@bp.route("/export-all.csv")
@login_required
def export_csv_all():
    """Multi-section CSV: missions, meetings, tasks for the chosen projects,
    filtered to the requested period when one is provided."""
    _require_manage()
    year, month = _parse_year_month(
        (request.args.get("year") or "").strip(),
        (request.args.get("month") or "").strip(),
    )
    period_start, period_end = _period_bounds(year, month)
    raw_ids = request.args.getlist("project_ids")
    project_ids: list[int] = []
    for raw in raw_ids:
        for token in raw.split(","):
            if token.isdigit():
                project_ids.append(int(token))
    if not project_ids:
        flash("Sélectionnez au moins un projet à exporter.", "danger")
        return redirect(url_for(
            "projects.export_form",
            year=year or "", month=month or "",
        ))

    projects = db.session.execute(
        db.select(Project)
        .where(Project.id.in_(project_ids))
        .order_by(Project.name)
    ).scalars().all()

    def in_period_dates(start: date, end: date) -> bool:
        if period_start is None:
            return True
        return start <= period_end and end >= period_start

    def in_period_single(d: date | None) -> bool:
        if period_start is None:
            return True
        return d is not None and period_start <= d <= period_end

    today = date.today()
    buf = StringIO()
    buf.write("﻿")  # BOM so Excel opens UTF-8 cleanly
    writer = csv.writer(
        buf, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n",
    )

    # === Missions ===
    writer.writerow(["=== MISSIONS ==="])
    writer.writerow([
        "Projet", "Client", "Mission", "Type", "Sous-type", "Spécialité",
        "Date début", "Date fin", "JH", "Statut", "Participants",
    ])
    for p in projects:
        for ev in sorted(p.missions, key=lambda e: (e.start_date, e.start_time)):
            if not in_period_dates(ev.start_date, ev.end_date):
                continue
            writer.writerow([
                p.name,
                p.client.name if p.client else "",
                ev.title,
                ev.meeting_type.name if ev.meeting_type else "",
                ev.meeting_subtype.name if ev.meeting_subtype else "",
                ev.role.label if ev.role else "",
                ev.start_date.isoformat(),
                ev.end_date.isoformat(),
                _person_days(ev),
                STATUS_LABELS.get(_effective_status(ev, today), ev.status),
                ", ".join(sorted(u.full_name for u in ev.participants)),
            ])

    writer.writerow([])
    # === Meetings ===
    writer.writerow(["=== RÉUNIONS ==="])
    writer.writerow(["Projet", "Client", "Réunion", "Catégorie", "Date"])
    for p in projects:
        for mt in sorted(p.meetings, key=lambda m: m.date):
            if not in_period_single(mt.date):
                continue
            writer.writerow([
                p.name,
                p.client.name if p.client else "",
                mt.name,
                mt.category.name if mt.category else "",
                mt.date.isoformat(),
            ])

    writer.writerow([])
    # === Tasks ===
    writer.writerow(["=== TÂCHES ==="])
    writer.writerow(["Projet", "Client", "Tâche", "Statut", "Date d'échéance"])
    for p in projects:
        for t in sorted(p.tasks, key=lambda x: (x.due_date or date.max, x.name)):
            # Tasks without a due date are out of any period filter — only
            # surface them on an unfiltered export so they're not silently
            # dropped from a monthly run.
            if period_start is not None and not in_period_single(t.due_date):
                continue
            writer.writerow([
                p.name,
                p.client.name if p.client else "",
                t.name,
                t.status.name if t.status else "",
                t.due_date.isoformat() if t.due_date else "",
            ])

    suffix_parts: list[str] = []
    if year is not None:
        suffix_parts.append(f"{year:04d}")
    if month is not None:
        suffix_parts.append(f"{month:02d}")
    if not suffix_parts:
        suffix_parts.append(date.today().isoformat())
    filename = f"projets-export-{'-'.join(suffix_parts)}.csv"

    response = Response(buf.getvalue(), mimetype="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# Status constants re-exported for templates/blueprints that need them.
__all__ = [
    "bp",
    "STATUS_LABELS",
    "STATUS_PREPLANIFIE",
    "STATUS_PLANIFIE",
    "STATUS_EN_COURS",
    "STATUS_TERMINE",
    "PROJECT_STATUS_ACTIVE",
    "PROJECT_STATUS_CLOSED",
    "MONTH_NAMES_FR",
]
