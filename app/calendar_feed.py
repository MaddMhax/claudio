"""Personal iCal (ICS) subscription feed.

Each user can subscribe their own calendar app to a stable, secret URL
(``/calendar/feed/<token>.ics``) authenticated by an unguessable per-user token
— calendar clients can't carry a login session. The token is mintable and
rotatable from the in-app subscription page, so a leaked URL can be revoked.

The feed exposes the missions/events the user participates in over a rolling
window. ICS is hand-built (no extra dependency); times are emitted in UTC.
"""
from datetime import datetime, time, timedelta, timezone

from flask import Blueprint, Response, abort, flash, redirect, render_template, url_for
from flask_login import current_user, login_required

from . import DISPLAY_TZ
from .extensions import db
from .models import EVENT_STATUSES, User


bp = Blueprint("calendar_feed", __name__, url_prefix="/calendar")

# Rolling window exported to the calendar client (keeps the feed bounded).
_WINDOW_PAST = timedelta(days=60)
_WINDOW_FUTURE = timedelta(days=365)


def _ics_escape(text: str) -> str:
    """Escape a value per RFC 5545 (commas, semicolons, backslashes, newlines)."""
    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """Fold a content line to <=75 octets per RFC 5545 (continuation = CRLF + SP)."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    chunks, start = [], 0
    limit = 75
    while start < len(raw):
        # Don't split a multibyte char: back off to a UTF-8 boundary.
        end = min(start + limit, len(raw))
        while end < len(raw) and (raw[end] & 0xC0) == 0x80:
            end -= 1
        chunks.append(raw[start:end].decode("utf-8"))
        start = end
        limit = 74  # continuation lines lose one octet to the leading space
    return "\r\n ".join(chunks)


def _utc_stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _local_to_utc_stamp(d, t) -> str:
    """Combine a date + time as Europe/Paris wall-clock, emit a UTC ICS stamp."""
    local = datetime.combine(d, t or time(0, 0)).replace(tzinfo=DISPLAY_TZ)
    return _utc_stamp(local)


def _build_ics(user: User) -> str:
    now = datetime.now(timezone.utc)
    lo = (now - _WINDOW_PAST).date()
    hi = (now + _WINDOW_FUTURE).date()

    events = [
        e for e in user.events
        if e.start_date <= hi and e.end_date >= lo
    ]
    events.sort(key=lambda e: (e.start_date, e.start_time or time(0, 0)))

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Claudio//Planning//FR",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:Claudio — {_ics_escape(user.full_name)}",
    ]
    stamp = _utc_stamp(now)
    for e in events:
        status = "TENTATIVE" if e.status == "preplanifie" else "CONFIRMED"
        bits = []
        if e.client:
            bits.append(f"Client : {e.client.name}")
        if e.project:
            bits.append(f"Projet : {e.project.name}")
        bits.append(f"Statut : {EVENT_STATUSES.get(e.status, e.status)}")
        if e.meeting_type:
            bits.append(f"Type : {e.meeting_type.name}")
        lines += [
            "BEGIN:VEVENT",
            f"UID:event-{e.id}@claudio",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{_local_to_utc_stamp(e.start_date, e.start_time)}",
            f"DTEND:{_local_to_utc_stamp(e.end_date, e.end_time)}",
            f"SUMMARY:{_ics_escape(e.title)}",
            f"DESCRIPTION:{_ics_escape(' — '.join(bits))}",
            f"STATUS:{status}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


@bp.route("/feed/<token>.ics")
def feed(token: str):
    """Token-authenticated ICS feed (no session). 404 on any bad token so we
    don't reveal whether a token exists."""
    if not token or len(token) < 16:
        abort(404)
    user = db.session.execute(
        db.select(User).where(User.ical_token == token)
    ).scalar_one_or_none()
    if user is None:
        abort(404)
    return Response(_build_ics(user), mimetype="text/calendar; charset=utf-8")


@bp.route("/subscribe")
@login_required
def subscribe():
    """Show the user's personal feed URL (minting a token on first visit)."""
    token = current_user.ensure_ical_token()
    db.session.commit()
    feed_url = url_for("calendar_feed.feed", token=token, _external=True)
    return render_template("calendar_subscribe.html", feed_url=feed_url)


@bp.route("/rotate", methods=["POST"])
@login_required
def rotate():
    """Revoke the current feed URL by issuing a fresh token."""
    current_user.ical_token = None
    current_user.ensure_ical_token()
    db.session.commit()
    flash("Lien iCal régénéré — l'ancien lien ne fonctionne plus.", "success")
    return redirect(url_for("calendar_feed.subscribe"))
