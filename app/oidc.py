"""Generic OpenID Connect (OIDC) single sign-on.

Builds an Authlib OAuth client for *any* spec-compliant OIDC provider
(Keycloak, Authentik, Google, Entra ID, Okta, GitLab, …) from its discovery
document. SSO is optional and coexists with username/password login.

Configuration is resolved at request time by ``settings_store.get_oidc_config``
(environment variables, else the admin-managed DB settings), so an admin can
change it from the UI without restarting — and every gunicorn worker picks up
the change. The constructed client is cached per worker, keyed by a signature
of the connection settings, and rebuilt automatically when they change.
"""
import hashlib
import json

from authlib.integrations.flask_client import OAuth
from flask import current_app

from .settings_store import get_oidc_config


# Per-worker cache: signature -> registered Authlib client. Only ever holds the
# client for the current settings; a settings change clears it and rebuilds.
_client_cache: dict[str, object] = {}


def _signature(cfg: dict) -> str:
    """Stable hash of the settings that define the connection. A change here
    (id / secret / discovery / scopes) invalidates the cached client."""
    payload = json.dumps(
        [cfg["client_id"], cfg["client_secret"], cfg["discovery_url"], cfg["scopes"]],
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_oidc_client():
    """Return ``(client, cfg)`` for the active OIDC config, or ``(None, None)``
    when SSO is disabled."""
    cfg = get_oidc_config()
    if cfg is None:
        return None, None

    sig = _signature(cfg)
    client = _client_cache.get(sig)
    if client is None:
        # Settings are new or changed — drop any stale client and rebuild.
        _client_cache.clear()
        registry = OAuth()
        # Authlib's Flask client needs a concrete app reference before a
        # registered client can be resolved; bind the real app (not the proxy).
        registry.init_app(current_app._get_current_object())
        client = registry.register(
            name="oidc",
            client_id=cfg["client_id"],
            client_secret=cfg["client_secret"],
            # Discovery: Authlib pulls all endpoints + JWKS from this document.
            server_metadata_url=cfg["discovery_url"],
            client_kwargs={"scope": cfg["scopes"]},
        )
        _client_cache[sig] = client
    return client, cfg
