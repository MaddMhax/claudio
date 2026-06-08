"""Read-only admin viewer for the security audit trail.

Supports filtering by action verb and UTC date range, paginated browsing, and
CSV export of the current filter. Retention pruning lives in
``app.audit.prune`` (run ``python -m app.audit --days N`` from cron)."""
import csv
import io
from datetime import datetime, time, timedelta, timezone
from functools import wraps

from flask import Blueprint, Response, abort, render_template, request
from flask_login import current_user, login_required

from .extensions import db
from .models import AuditLog


bp = Blueprint("audit_admin", __name__, url_prefix="/admin/audit")


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


PAGE_SIZE = 100
EXPORT_CAP = 10000  # hard ceiling on a single CSV export


def _parse_date(value: str | None):
    """Parse an ISO date (YYYY-MM-DD) or return None."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _filtered_query(action: str, date_from, date_to):
    """Build the base query honouring the active filters (UTC boundaries)."""
    query = db.select(AuditLog)
    if action:
        query = query.where(AuditLog.action == action)
    if date_from:
        start = datetime.combine(date_from, time.min, tzinfo=timezone.utc)
        query = query.where(AuditLog.created_at >= start)
    if date_to:
        # Inclusive of the whole 'to' day.
        end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=timezone.utc)
        query = query.where(AuditLog.created_at < end)
    return query


@bp.route("")
@admin_required
def view_log():
    action = (request.args.get("action") or "").strip()
    date_from = _parse_date(request.args.get("from"))
    date_to = _parse_date(request.args.get("to"))
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1

    base = _filtered_query(action, date_from, date_to)

    if request.args.get("format") == "csv":
        return _export_csv(base)

    total = db.session.execute(
        db.select(db.func.count()).select_from(base.subquery())
    ).scalar_one()
    entries = db.session.execute(
        base.order_by(AuditLog.created_at.desc())
        .limit(PAGE_SIZE)
        .offset((page - 1) * PAGE_SIZE)
    ).scalars().all()

    actions = [
        row[0] for row in db.session.execute(
            db.select(AuditLog.action).distinct().order_by(AuditLog.action)
        ).all()
    ]
    return render_template(
        "audit_log.html",
        entries=entries,
        actions=actions,
        active_action=action,
        date_from=request.args.get("from", ""),
        date_to=request.args.get("to", ""),
        page=page,
        page_size=PAGE_SIZE,
        total=total,
        has_prev=page > 1,
        has_next=page * PAGE_SIZE < total,
    )


# Cells starting with one of these are interpreted as formulas by Excel /
# LibreOffice. Audit fields like ``target`` carry attacker-controllable text
# (e.g. a failed-login username is logged verbatim), so neutralise them before
# export by prefixing a single quote — the classic CSV-injection guard.
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: str) -> str:
    return "'" + value if value and value[0] in _FORMULA_TRIGGERS else value


def _export_csv(base) -> Response:
    rows = db.session.execute(
        base.order_by(AuditLog.created_at.desc()).limit(EXPORT_CAP)
    ).scalars().all()

    buf = io.StringIO()
    buf.write("﻿")  # BOM so Excel detects UTF-8
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    writer.writerow(["date_utc", "action", "actor", "target", "ip", "detail"])
    for e in rows:
        writer.writerow([
            e.created_at.strftime("%Y-%m-%d %H:%M:%S") if e.created_at else "",
            _csv_safe(e.action),
            _csv_safe(e.actor_username or ""),
            _csv_safe(e.target or ""),
            _csv_safe(e.ip or ""),
            _csv_safe(e.detail or ""),
        ])
    return Response(
        buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=audit_log.csv"},
    )
