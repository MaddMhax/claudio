"""Lightweight security/audit logging.

``record()`` appends an :class:`~app.models.AuditLog` row describing a
security-relevant event. It is deliberately defensive: a failure to write the
audit trail must never break the operation being audited, so all errors are
swallowed (after a rollback of just the audit insert when committing).
"""
from flask import has_request_context, request
from flask_login import current_user

from .extensions import db
from .models import AuditLog


def client_ip() -> str | None:
    """Best-effort client IP. Relies on ProxyFix (BEHIND_PROXY=1) having already
    rewritten ``remote_addr`` to the real client when behind a reverse proxy."""
    if not has_request_context():
        return None
    return request.remote_addr or None


def record(action: str, *, target=None, detail=None, actor=None, commit=False) -> None:
    """Append an audit entry.

    ``actor`` defaults to the authenticated user for the current request.
    Pass it explicitly for events where there is no logged-in user yet (e.g. a
    failed login). Set ``commit=True`` to flush immediately (use for events not
    already wrapped in a committing route)."""
    try:
        if actor is None and has_request_context():
            actor = current_user if getattr(current_user, "is_authenticated", False) else None
        db.session.add(AuditLog(
            actor_id=getattr(actor, "id", None),
            actor_username=getattr(actor, "username", None),
            action=action,
            target=target,
            ip=client_ip(),
            detail=detail,
        ))
        if commit:
            db.session.commit()
    except Exception:  # noqa: BLE001 — auditing must never break the operation
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


def prune(retention_days: int) -> int:
    """Delete audit entries older than ``retention_days``. Returns the count."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import delete

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    result = db.session.execute(delete(AuditLog).where(AuditLog.created_at < cutoff))
    db.session.commit()
    return result.rowcount or 0


def _main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m app.audit --days 365`` (run from cron for retention)."""
    import argparse

    from . import create_app

    parser = argparse.ArgumentParser(description="Prune the Claudio audit log.")
    parser.add_argument("--days", type=int, default=365,
                        help="retention window in days (default 365)")
    args = parser.parse_args(argv)

    app = create_app()
    with app.app_context():
        removed = prune(args.days)
    print(f"[audit] pruned {removed} entries older than {args.days} days", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
