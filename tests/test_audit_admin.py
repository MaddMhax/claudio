"""Audit viewer (admin), CSV export, and retention pruning."""
from datetime import datetime, timedelta, timezone

from app import audit
from app.extensions import db
from app.models import AuditLog
from tests.conftest import login


def test_prune_removes_only_old_entries(app, session):
    now = datetime.now(timezone.utc)
    session.add_all([
        AuditLog(action="old.event", created_at=now - timedelta(days=400)),
        AuditLog(action="recent.event", created_at=now - timedelta(days=10)),
    ])
    session.commit()

    removed = audit.prune(365)
    assert removed == 1

    session.expire_all()
    remaining = session.execute(db.select(AuditLog)).scalars().all()
    assert [e.action for e in remaining] == ["recent.event"]


def test_audit_view_requires_admin(client, user_factory):
    user_factory(username="bob", password="Secretpass1", roles=())
    login(client, "bob", "Secretpass1")
    assert client.get("/admin/audit").status_code == 403


def test_audit_view_loads_for_admin(client, user_factory):
    user_factory(username="admin", password="Adminpass1", roles=("admin",))
    login(client, "admin", "Adminpass1")  # writes a login.success row
    resp = client.get("/admin/audit")
    assert resp.status_code == 200
    assert b"login.success" in resp.data


def test_audit_csv_export(client, user_factory):
    user_factory(username="admin", password="Adminpass1", roles=("admin",))
    login(client, "admin", "Adminpass1")
    resp = client.get("/admin/audit?format=csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.content_type
    assert "attachment" in resp.headers.get("Content-Disposition", "")
    assert "action" in resp.get_data(as_text=True)


def test_audit_csv_neutralises_formula_injection(client, user_factory):
    # A failed login records the (attacker-controlled) username verbatim as the
    # audit ``target``. Exporting must not hand Excel/LibreOffice a live formula.
    payload = "=cmd|'/c calc'!A1"
    client.post("/auth/login", data={"username": payload, "password": "x"})

    user_factory(username="admin", password="Adminpass1", roles=("admin",))
    login(client, "admin", "Adminpass1")
    body = client.get("/admin/audit?format=csv").get_data(as_text=True)

    # The dangerous value is present but defanged with a leading quote, so no
    # cell begins with a formula trigger.
    assert "'" + payload in body
    assert ";" + payload not in body
