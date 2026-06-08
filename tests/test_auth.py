"""Authentication: login, lockout, forced change, session invalidation."""
from app.models import User
from tests.conftest import login


def test_login_success_redirects(client, user_factory):
    user_factory(username="bob", password="Secretpass1", roles=())
    resp = login(client, "bob", "Secretpass1")
    assert resp.status_code == 302
    assert "/auth/login" not in resp.headers["Location"]


def test_login_wrong_password_no_redirect(client, user_factory):
    user_factory(username="bob", password="Secretpass1", roles=())
    resp = login(client, "bob", "nope")
    assert resp.status_code == 200  # re-renders the form, no redirect


def test_lockout_after_repeated_failures(client, session, user_factory):
    user = user_factory(username="bob", password="Secretpass1", roles=())
    for _ in range(5):
        login(client, "bob", "wrong")
    session.expire_all()  # ensure we read committed state, not a cached instance
    refreshed = session.get(User, user.id)
    assert refreshed.failed_login_count >= 5
    assert refreshed.is_locked
    # Even the correct password is refused while locked.
    resp = login(client, "bob", "Secretpass1")
    assert resp.status_code == 200


def test_successful_login_resets_failures(client, session, user_factory):
    user = user_factory(username="bob", password="Secretpass1", roles=())
    login(client, "bob", "wrong")
    login(client, "bob", "Secretpass1")
    session.expire_all()
    refreshed = session.get(User, user.id)
    assert refreshed.failed_login_count == 0
    assert refreshed.locked_until is None


def test_must_change_password_redirects(client, user_factory):
    user_factory(username="bob", password="Secretpass1", roles=(),
                 must_change_password=True)
    login(client, "bob", "Secretpass1")
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/auth/password" in resp.headers["Location"]


def test_password_change_invalidates_other_sessions(client, session, user_factory):
    user = user_factory(username="bob", password="Secretpass1", roles=())
    login(client, "bob", "Secretpass1")
    # Simulate a password change from another session: rotate the token.
    session.get(User, user.id).rotate_session_token()
    session.commit()
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]
