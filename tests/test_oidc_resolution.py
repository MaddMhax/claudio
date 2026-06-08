"""OIDC identity → local user mapping (app.auth._resolve_oidc_user)."""
from app.auth import _resolve_oidc_user
from app.models import User


def _cfg(**over):
    base = {
        "username_claim": "preferred_username",
        "name_claim": "name",
        "auto_create": False,
        "default_roles": [],
    }
    base.update(over)
    return base


def test_resolve_by_existing_sub(session, user_factory):
    user = user_factory(username="bob", roles=())
    user.oidc_sub = "SUB-1"
    session.commit()
    got = _resolve_oidc_user("SUB-1", {}, _cfg())
    assert got is not None and got.id == user.id


def test_resolve_binds_sub_by_username(session, user_factory):
    user = user_factory(username="bob", roles=())
    got = _resolve_oidc_user("SUB-2", {"preferred_username": "bob"}, _cfg())
    assert got is not None and got.id == user.id
    assert got.oidc_sub == "SUB-2"  # bound on first SSO login


def test_resolve_refuses_unknown_without_autocreate(session):
    got = _resolve_oidc_user(
        "SUB-3", {"preferred_username": "ghost"}, _cfg(auto_create=False)
    )
    assert got is None


def test_resolve_autocreates_when_enabled(session):
    got = _resolve_oidc_user(
        "SUB-4",
        {"preferred_username": "newbie", "name": "New Bie"},
        _cfg(auto_create=True, default_roles=["audit_web"]),
    )
    assert got is not None
    assert got.username == "newbie"
    assert got.oidc_sub == "SUB-4"
    assert session.get(User, got.id) is not None
