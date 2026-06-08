"""Persisted application settings (key/value) + OIDC SSO config resolution.

SSO can be configured two ways, with a deliberate precedence:

  * **Environment variables** (``OIDC_*``) — when the required trio is present
    they are authoritative and the admin UI shows the values read-only. Ideal
    for IaC / immutable deployments.
  * **Admin UI** — otherwise values come from the ``app_settings`` table and
    are editable live (no restart). The client secret is encrypted at rest
    with a key derived from ``SECRET_KEY``.

``get_oidc_config()`` returns the resolved config dict (or ``None`` when SSO is
off), caching the result on ``flask.g`` so a request hits the DB at most once.
"""
import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app, g

from .extensions import db
from .models import AppSetting


# Env vars that, when all present, lock SSO config to environment mode.
OIDC_REQUIRED_ENV = ("OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_DISCOVERY_URL")

_DEFAULTS = {
    "scopes": "openid email profile",
    "username_claim": "preferred_username",
    "name_claim": "name",
    "button_label": "Connexion SSO",
    "provider": "custom",
}


# --- raw key/value store ---------------------------------------------------
def get_setting(key: str, default=None):
    row = db.session.get(AppSetting, key)
    # Treat an empty/blank stored value as "unset" so callers fall back to
    # their default (e.g. a blank button label → "Connexion SSO").
    if row is None or not (row.value or "").strip():
        return default
    return row.value


def set_setting(key: str, value) -> None:
    row = db.session.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key)
        db.session.add(row)
    row.value = value


# --- client-secret encryption (Fernet key derived from SECRET_KEY) ---------
def _fernet() -> Fernet:
    secret = current_app.config["SECRET_KEY"].encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(key)


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    """Decrypt a stored secret. Returns "" if empty or undecryptable (e.g. the
    SECRET_KEY was rotated — the admin must re-enter the secret)."""
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


# --- OIDC config resolution ------------------------------------------------
def oidc_env_mode() -> bool:
    """True when SSO is pinned by environment variables (UI becomes read-only)."""
    return all(os.environ.get(k) for k in OIDC_REQUIRED_ENV)


def _split_roles(raw: str | None) -> list[str]:
    return [r.strip() for r in (raw or "").split(",") if r.strip()]


def get_oidc_config() -> dict | None:
    """Resolve the active OIDC config, or ``None`` when SSO is disabled.

    Env mode wins outright; otherwise the DB-stored config is used (and must
    have client id + secret + discovery URL to count as enabled). Cached on
    ``flask.g`` for the duration of the request."""
    if "oidc_config" in g:
        return g.oidc_config

    cfg = _resolve_oidc_config()
    g.oidc_config = cfg
    return cfg


def _resolve_oidc_config() -> dict | None:
    if oidc_env_mode():
        return {
            "source": "env",
            "client_id": os.environ["OIDC_CLIENT_ID"],
            "client_secret": os.environ["OIDC_CLIENT_SECRET"],
            "discovery_url": os.environ["OIDC_DISCOVERY_URL"],
            "scopes": os.environ.get("OIDC_SCOPES", _DEFAULTS["scopes"]),
            "username_claim": os.environ.get(
                "OIDC_USERNAME_CLAIM", _DEFAULTS["username_claim"]
            ),
            "name_claim": os.environ.get("OIDC_NAME_CLAIM", _DEFAULTS["name_claim"]),
            "button_label": os.environ.get(
                "OIDC_BUTTON_LABEL", _DEFAULTS["button_label"]
            ),
            "redirect_uri": os.environ.get("OIDC_REDIRECT_URI") or None,
            "auto_create": os.environ.get("OIDC_AUTO_CREATE") == "1",
            "default_roles": _split_roles(os.environ.get("OIDC_DEFAULT_ROLES")),
            "provider": os.environ.get("OIDC_PROVIDER", _DEFAULTS["provider"]),
        }

    if get_setting("oidc_enabled") != "1":
        return None
    client_id = get_setting("oidc_client_id", "")
    discovery_url = get_setting("oidc_discovery_url", "")
    client_secret = decrypt_secret(get_setting("oidc_client_secret_enc", ""))
    if not (client_id and discovery_url and client_secret):
        return None

    return {
        "source": "db",
        "client_id": client_id,
        "client_secret": client_secret,
        "discovery_url": discovery_url,
        "scopes": get_setting("oidc_scopes", _DEFAULTS["scopes"]),
        "username_claim": get_setting("oidc_username_claim", _DEFAULTS["username_claim"]),
        "name_claim": get_setting("oidc_name_claim", _DEFAULTS["name_claim"]),
        "button_label": get_setting("oidc_button_label", _DEFAULTS["button_label"]),
        "redirect_uri": get_setting("oidc_redirect_uri"),
        "auto_create": get_setting("oidc_auto_create") == "1",
        "default_roles": _split_roles(get_setting("oidc_default_roles")),
        "provider": get_setting("oidc_provider", _DEFAULTS["provider"]),
    }
