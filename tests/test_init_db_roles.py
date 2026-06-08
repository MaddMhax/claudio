"""Regression tests for the user_role cleanup on (re)deploy.

Guards the bug where a redeploy wiped admin-created users' specialty roles,
emptying ``auditor_ids`` so every "auditeur disponible" green calendar cell
disappeared. The cleanup must now drop *only* genuinely-orphaned role strings
and preserve every valid assignment.
"""
from app.init_db import _prune_orphan_user_roles
from app.models import Role, User


def test_prune_keeps_valid_specialty_drops_orphan(app, session, user_factory):
    # A live specialty role + an admin-created user holding it AND a stale
    # legacy string left over from the pre-roles-table era.
    session.add(Role(key="audit_web", label="Auditeur web", color="#3b82f6"))
    session.commit()
    user = user_factory(username="zoe", roles=("audit_web", "auditeur_legacy"))
    assert set(user.roles) == {"audit_web", "auditeur_legacy"}

    _prune_orphan_user_roles()
    session.expire_all()

    refreshed = session.get(User, user.id)
    # The valid specialty survives the redeploy; only the orphan is pruned.
    assert set(refreshed.roles) == {"audit_web"}


def test_prune_keeps_system_roles_without_role_rows(app, session, user_factory):
    # admin / planificateur live as constants, not Role rows — they must never
    # be treated as orphans.
    user = user_factory(username="mgr", roles=("planificateur",))

    _prune_orphan_user_roles()
    session.expire_all()

    assert set(session.get(User, user.id).roles) == {"planificateur"}


def test_prune_is_idempotent_and_noop_on_clean_data(app, session, user_factory):
    session.add(Role(key="audit_code", label="Auditeur de code", color="#f59e0b"))
    session.commit()
    user = user_factory(username="iris", roles=("admin", "audit_code"))

    _prune_orphan_user_roles()
    _prune_orphan_user_roles()  # second pass must change nothing
    session.expire_all()

    assert set(session.get(User, user.id).roles) == {"admin", "audit_code"}
