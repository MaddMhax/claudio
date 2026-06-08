"""Operational reminders: FPR-not-received missions and tasks coming due.

Run on a schedule (cron / systemd timer) inside the container:

    python -m app.reminders            # print the digest
    python -m app.reminders --days 5   # change the look-ahead window

If SMTP is configured via environment variables the digest is emailed; either
way it's printed to stdout (so a cron MAILTO or container log captures it).

    SMTP_HOST, SMTP_PORT (default 25), SMTP_USER, SMTP_PASSWORD,
    SMTP_STARTTLS (1/0, default 1 when user set), SMTP_FROM, REMINDER_TO

The digest is read-only — it never mutates the database.
"""
from __future__ import annotations

import argparse
import os
import smtplib
import sys
from datetime import date, timedelta
from email.message import EmailMessage

from . import create_app
from .extensions import db
from .models import Event, Task


DEFAULT_WINDOW_DAYS = 7


def _missions_missing_fpr(today: date, horizon: date) -> list[Event]:
    """Technical missions starting within the window whose FPR isn't received."""
    events = db.session.execute(
        db.select(Event)
        .where(
            Event.role_id.is_not(None),       # audit mission → needs an FPR
            Event.fpr_received.is_(False),
            Event.start_date >= today,
            Event.start_date <= horizon,
        )
        .order_by(Event.start_date)
    ).scalars().all()
    return events


def _tasks_due(today: date, horizon: date) -> list[Task]:
    """Non-template tasks with a due date inside the window."""
    return db.session.execute(
        db.select(Task)
        .where(
            Task.is_template.is_(False),
            Task.due_date.is_not(None),
            Task.due_date >= today,
            Task.due_date <= horizon,
        )
        .order_by(Task.due_date)
    ).scalars().all()


def build_digest(days: int) -> tuple[str, bool]:
    """Return (text, has_items). ``has_items`` is False when nothing is due."""
    today = date.today()
    horizon = today + timedelta(days=days)

    missions = _missions_missing_fpr(today, horizon)
    tasks = _tasks_due(today, horizon)

    lines = [f"Rappels Claudio — échéances d'ici le {horizon.isoformat()} :", ""]

    lines.append(f"FPR non reçues ({len(missions)}) :")
    if missions:
        for e in missions:
            client = e.client.name if e.client else "—"
            lines.append(f"  • {e.start_date.isoformat()} — {e.title} ({client})")
    else:
        lines.append("  (aucune)")
    lines.append("")

    lines.append(f"Tâches à échéance ({len(tasks)}) :")
    if tasks:
        for t in tasks:
            proj = t.project.name if t.project else "—"
            status = t.status.name if t.status else "—"
            lines.append(
                f"  • {t.due_date.isoformat()} — {t.name} [{status}] ({proj})"
            )
    else:
        lines.append("  (aucune)")

    return "\n".join(lines), bool(missions or tasks)


def _send_email(subject: str, body: str) -> bool:
    """Send the digest by SMTP when configured. Returns True if an email was sent."""
    host = os.environ.get("SMTP_HOST")
    to_addr = os.environ.get("REMINDER_TO")
    if not host or not to_addr:
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", "claudio@localhost"))
    msg["To"] = to_addr
    msg.set_content(body)

    port = int(os.environ.get("SMTP_PORT", "25"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    use_tls = os.environ.get("SMTP_STARTTLS", "1" if user else "0") == "1"

    with smtplib.SMTP(host, port, timeout=15) as smtp:
        if use_tls:
            smtp.starttls()
        if user and password:
            smtp.login(user, password)
        smtp.send_message(msg)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send/print Claudio reminders.")
    parser.add_argument(
        "--days", type=int, default=DEFAULT_WINDOW_DAYS,
        help=f"look-ahead window in days (default {DEFAULT_WINDOW_DAYS})",
    )
    args = parser.parse_args(argv)

    app = create_app()
    with app.app_context():
        body, has_items = build_digest(args.days)

    print(body, flush=True)
    try:
        if _send_email("Claudio — rappels d'échéances", body):
            print("\n[reminders] digest envoyé par e-mail.", flush=True)
    except Exception as exc:  # noqa: BLE001 — emailing must not crash the job
        print(f"\n[reminders] échec de l'envoi e-mail : {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
