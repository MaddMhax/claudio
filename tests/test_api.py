"""Read-only integration API: bearer-token auth + project endpoints."""
from datetime import date

from app.api import generate_token, hash_token
from app.extensions import db
from app.models import ApiToken, Client, Event, Project
from tests.conftest import login


def _make_token(session, scope="read_only", active=True, label="test"):
    """Persist a token and return its plaintext (only the hash is stored)."""
    plaintext = generate_token()
    session.add(ApiToken(
        label=label, token_hash=hash_token(plaintext), scope=scope, active=active,
    ))
    session.commit()
    return plaintext


def _make_project(session, name, code):
    client = Client(name=f"Client {name}")
    session.add(client)
    session.flush()
    project = Project(name=name, code=code, client_id=client.id)
    session.add(project)
    session.commit()
    return project


def _make_mission(session, project, start, end, participants=()):
    mission = Event(
        title=f"Mission {project.name}",
        start_date=start, end_date=end, project_id=project.id,
    )
    mission.participants = list(participants)
    session.add(mission)
    session.commit()
    return mission


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_missing_token_is_401_json(client):
    resp = client.get("/api/v1/projects")
    assert resp.status_code == 401
    assert resp.is_json
    assert resp.get_json()["error"]["code"] == "unauthorized"


def test_invalid_token_rejected(client):
    resp = client.get("/api/v1/projects", headers=_auth("cld_bogus"))
    assert resp.status_code == 401


def test_revoked_token_rejected(client, session):
    token = _make_token(session, active=False)
    resp = client.get("/api/v1/projects", headers=_auth(token))
    assert resp.status_code == 401


def test_list_projects_exposes_name_and_reference(client, session):
    token = _make_token(session)
    _make_project(session, name="Alpha", code="REF-1")
    _make_project(session, name="Beta", code=None)  # référence interne optional

    resp = client.get("/api/v1/projects", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] == 2
    by_name = {p["name"]: p for p in data["projects"]}
    assert by_name["Alpha"]["reference_interne"] == "REF-1"
    assert by_name["Beta"]["reference_interne"] is None


def test_get_single_project(client, session):
    token = _make_token(session)
    project = _make_project(session, name="Gamma", code="REF-9")
    resp = client.get(f"/api/v1/projects/{project.id}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.get_json() == {
        "id": project.id, "name": "Gamma", "reference_interne": "REF-9",
        "missions": [],
    }


def test_project_payload_lists_mission_ids(client, session):
    token = _make_token(session)
    project = _make_project(session, name="Zeta", code="REF-Z")
    m1 = _make_mission(session, project, date(2026, 6, 8), date(2026, 6, 12))
    m2 = _make_mission(session, project, date(2026, 6, 15), date(2026, 6, 19))

    resp = client.get(f"/api/v1/projects/{project.id}", headers=_auth(token))
    assert resp.status_code == 200
    missions = resp.get_json()["missions"]
    # Ordered by start_date; ids are what a client uses to query /missions/{id}.
    assert [m["id"] for m in missions] == [m1.id, m2.id]
    assert all("title" in m for m in missions)


def test_get_unknown_project_is_404_json(client, session):
    token = _make_token(session)
    resp = client.get("/api/v1/projects/999999", headers=_auth(token))
    assert resp.status_code == 404
    assert resp.get_json()["error"]["code"] == "not_found"


def test_get_mission_reports_nombre_jh(client, session, user_factory):
    token = _make_token(session)
    project = _make_project(session, name="Delta", code="REF-D")
    # Mon 8 → Fri 12 June 2026 = 5 working days (no holiday that week), with two
    # pentesters → 10 JH.
    a = user_factory(username="p1", roles=("web",))
    b = user_factory(username="p2", roles=("web",))
    mission = _make_mission(
        session, project, date(2026, 6, 8), date(2026, 6, 12), participants=(a, b),
    )
    resp = client.get(f"/api/v1/missions/{mission.id}", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["id"] == mission.id
    assert data["project_id"] == project.id
    assert data["start_date"] == "2026-06-08"
    assert data["end_date"] == "2026-06-12"
    assert data["nombre_jh"] == 10


def test_get_unknown_mission_is_404_json(client, session):
    token = _make_token(session)
    resp = client.get("/api/v1/missions/999999", headers=_auth(token))
    assert resp.status_code == 404
    assert resp.get_json()["error"]["code"] == "not_found"


def test_get_mission_requires_token(client, session):
    project = _make_project(session, name="Epsilon", code=None)
    mission = _make_mission(session, project, date(2026, 6, 8), date(2026, 6, 12))
    resp = client.get(f"/api/v1/missions/{mission.id}")
    assert resp.status_code == 401


def test_wrong_method_is_405_json(client):
    resp = client.post("/api/v1/projects")
    assert resp.status_code == 405
    assert resp.is_json


def test_successful_call_stamps_last_used(client, session):
    token = _make_token(session)
    assert client.get("/api/v1/projects", headers=_auth(token)).status_code == 200
    session.expire_all()
    row = session.execute(db.select(ApiToken)).scalars().one()
    assert row.last_used_at is not None


# --- OpenAPI document is admin-gated ---------------------------------------
def test_openapi_requires_login(client):
    # Anonymous → bounced to login (the swagger gate / admin_required), never 200.
    assert client.get("/admin/api/openapi.json").status_code in (302, 401)


def test_openapi_served_to_admin(client, user_factory):
    user_factory(username="admin", password="Adminpass1", roles=("admin",))
    login(client, "admin", "Adminpass1")
    resp = client.get("/admin/api/openapi.json")
    assert resp.status_code == 200
    spec = resp.get_json()
    assert spec["openapi"].startswith("3.")
    assert "/projects" in spec["paths"]
    assert "/projects/{project_id}" in spec["paths"]
    assert "/missions/{mission_id}" in spec["paths"]
