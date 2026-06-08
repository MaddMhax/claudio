"""Pytest fixtures.

Requires a reachable PostgreSQL via ``DATABASE_URL`` (the CI ``test`` job
provides one). Each test runs against a freshly ``create_all``-ed schema that
is dropped on teardown — note this exercises the models directly, not the
``init_db`` legacy-migration path.
"""
import os
import secrets

# Must be set before the app package is imported (create_app reads them).
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("ALLOW_INSECURE_COOKIE", "1")

import pytest  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db as _db  # noqa: E402
from app.models import User  # noqa: E402


@pytest.fixture
def app():
    application = create_app()
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with application.app_context():
        _db.create_all()
        try:
            yield application
        finally:
            _db.session.remove()
            _db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def session(app):
    return _db.session


@pytest.fixture
def user_factory(session):
    """Create and persist a User. Returns the factory callable."""
    def _make(username="alice", password="Password123",
              roles=("admin",), must_change_password=False):
        user = User(
            username=username,
            full_name=username.title(),
            must_change_password=must_change_password,
        )
        if password:
            user.set_password(password)
        session.add(user)
        session.flush()
        user.roles = list(roles)
        session.commit()
        return user
    return _make


def login(client, username, password):
    return client.post(
        "/auth/login",
        data={"username": username, "password": password},
    )
