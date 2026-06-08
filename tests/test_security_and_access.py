"""Security headers, access control, audit logging, iCal feed, healthz."""
from app.extensions import db
from app.models import AuditLog
from tests.conftest import login


def test_healthz_ok(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_security_headers_present(client):
    resp = client.get("/healthz")
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("Referrer-Policy")
    # CSP defaults to report-only until explicitly enforced.
    assert "Content-Security-Policy-Report-Only" in resp.headers


def test_non_admin_forbidden_on_users_admin(client, user_factory):
    user_factory(username="bob", password="Secretpass1", roles=())
    login(client, "bob", "Secretpass1")
    resp = client.get("/admin/users/")
    assert resp.status_code == 403


def test_login_writes_audit_entry(client, session, user_factory):
    user_factory(username="bob", password="Secretpass1", roles=())
    login(client, "bob", "Secretpass1")
    rows = session.execute(
        db.select(AuditLog).where(AuditLog.action == "login.success")
    ).scalars().all()
    assert any(r.target == "bob" for r in rows)


def test_failed_login_is_audited(client, session, user_factory):
    user_factory(username="bob", password="Secretpass1", roles=())
    login(client, "bob", "wrong")
    rows = session.execute(
        db.select(AuditLog).where(AuditLog.action == "login.failure")
    ).scalars().all()
    assert any(r.target == "bob" for r in rows)


def test_ical_feed_serves_with_valid_token(client, session, user_factory):
    user = user_factory(username="bob", roles=())
    token = user.ensure_ical_token()
    session.commit()
    resp = client.get(f"/calendar/feed/{token}.ics")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert body.startswith("BEGIN:VCALENDAR")
    assert "text/calendar" in resp.headers["Content-Type"]


def test_ical_feed_rejects_bad_token(client):
    resp = client.get("/calendar/feed/" + "z" * 24 + ".ics")
    assert resp.status_code == 404
