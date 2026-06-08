"""Admin UI to configure OIDC single sign-on at runtime.

Writes the OIDC settings to the ``app_settings`` table (client secret encrypted
at rest, see ``settings_store``). When the ``OIDC_*`` environment variables are
set they take precedence and this page becomes read-only — the env config is
authoritative for IaC / immutable deployments.
"""
from functools import wraps
from urllib.parse import urlparse

import requests
from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from wtforms import BooleanField, PasswordField, SelectField, StringField
from wtforms.validators import Length, Optional

from .audit import record as audit_record
from .extensions import db
from .settings_store import (
    encrypt_secret,
    get_oidc_config,
    get_setting,
    oidc_env_mode,
    set_setting,
)


bp = Blueprint("sso_admin", __name__, url_prefix="/admin/sso")


# Known provider presets surfaced in the admin dropdown. Each entry pre-fills
# the discovery URL pattern, scopes and the username claim that provider
# actually emits — most misconfigurations are a typo'd URL or wrong claim.
# Keys double as the identity used to pick the login-button logo.
PROVIDER_CHOICES = [
    ("custom", "Personnalisé / autre"),
    ("gitlab", "GitLab"),
    ("google", "Google"),
    ("keycloak", "Keycloak"),
    ("entra", "Microsoft Entra ID"),
]


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


class SSOConfigForm(FlaskForm):
    enabled = BooleanField("Activer la connexion SSO (OIDC)")
    provider = SelectField("Fournisseur", choices=PROVIDER_CHOICES, default="custom")
    client_id = StringField("Client ID", validators=[Optional(), Length(max=255)])
    # Write-only: never rendered back. Left blank on save = keep current secret.
    client_secret = PasswordField(
        "Client secret", validators=[Optional(), Length(max=512)]
    )
    discovery_url = StringField(
        "URL de découverte (.well-known/openid-configuration)",
        validators=[Optional(), Length(max=500)],
    )
    redirect_uri = StringField(
        "URL de redirection (override)", validators=[Optional(), Length(max=500)]
    )
    scopes = StringField("Scopes", validators=[Optional(), Length(max=255)])
    username_claim = StringField(
        "Claim → nom d'utilisateur", validators=[Optional(), Length(max=64)]
    )
    name_claim = StringField(
        "Claim → nom complet", validators=[Optional(), Length(max=64)]
    )
    button_label = StringField(
        "Libellé du bouton", validators=[Optional(), Length(max=64)]
    )
    auto_create = BooleanField("Créer automatiquement les comptes inconnus")
    default_roles = StringField(
        "Rôles par défaut (clés, séparées par des virgules)",
        validators=[Optional(), Length(max=255)],
    )


_TEXT_FIELDS = {
    "provider": "oidc_provider",
    "client_id": "oidc_client_id",
    "discovery_url": "oidc_discovery_url",
    "redirect_uri": "oidc_redirect_uri",
    "scopes": "oidc_scopes",
    "username_claim": "oidc_username_claim",
    "name_claim": "oidc_name_claim",
    "button_label": "oidc_button_label",
    "default_roles": "oidc_default_roles",
}


def _sso_status() -> dict:
    """Resolve the live state shown by the status badge.

    ``active`` when a usable config resolves, ``incomplete`` when SSO is turned
    on but the required trio is missing, ``disabled`` otherwise. ``source`` is
    where the config comes from (env vars vs the admin UI)."""
    cfg = get_oidc_config()
    if cfg is not None:
        return {
            "key": "active",
            "label": "SSO actif",
            "source": "env" if cfg["source"] == "env" else "ui",
            "discovery_url": cfg["discovery_url"],
        }
    enabled = oidc_env_mode() or get_setting("oidc_enabled") == "1"
    if enabled:
        return {
            "key": "incomplete",
            "label": "SSO incomplet",
            "source": "ui",
            "discovery_url": get_setting("oidc_discovery_url", ""),
        }
    return {"key": "disabled", "label": "SSO désactivé", "source": None, "discovery_url": ""}


@bp.route("", methods=["GET", "POST"])
@admin_required
def edit():
    env_mode = oidc_env_mode()
    form = SSOConfigForm()

    if env_mode:
        # Authoritative env config — show it, don't let the UI overwrite it.
        if form.is_submitted():
            flash(
                "La configuration SSO est verrouillée par les variables "
                "d'environnement et ne peut pas être modifiée ici.",
                "warning",
            )
            return redirect(url_for("sso_admin.edit"))
        return render_template(
            "sso_admin.html",
            form=form,
            env_mode=True,
            cfg=get_oidc_config(),
            secret_set=True,
            status=_sso_status(),
        )

    if form.validate_on_submit():
        set_setting("oidc_enabled", "1" if form.enabled.data else "0")
        set_setting("oidc_auto_create", "1" if form.auto_create.data else "0")
        for field, key in _TEXT_FIELDS.items():
            set_setting(key, (getattr(form, field).data or "").strip())
        # Write-only secret: only overwrite when a new value was typed.
        if form.client_secret.data:
            set_setting(
                "oidc_client_secret_enc", encrypt_secret(form.client_secret.data)
            )
        audit_record(
            "sso.config_update",
            detail=f"enabled={'1' if form.enabled.data else '0'}, "
                   f"provider={form.provider.data}, "
                   f"secret_changed={'yes' if form.client_secret.data else 'no'}",
        )
        db.session.commit()

        if form.enabled.data and get_oidc_config() is None:
            flash(
                "Réglages enregistrés, mais le SSO reste inactif : renseignez "
                "Client ID, secret et URL de découverte.",
                "warning",
            )
        else:
            flash("Configuration SSO enregistrée.", "success")
        return redirect(url_for("sso_admin.edit"))

    if not form.is_submitted():
        # Pre-fill from stored settings (never the secret — it's write-only).
        form.enabled.data = get_setting("oidc_enabled") == "1"
        form.auto_create.data = get_setting("oidc_auto_create") == "1"
        for field, key in _TEXT_FIELDS.items():
            getattr(form, field).data = get_setting(key, "")
        # SelectField needs a valid choice — blank isn't one.
        form.provider.data = get_setting("oidc_provider", "custom")

    return render_template(
        "sso_admin.html",
        form=form,
        env_mode=False,
        cfg=None,
        secret_set=bool(get_setting("oidc_client_secret_enc")),
        status=_sso_status(),
    )


@bp.route("/test", methods=["POST"])
@admin_required
def test_connection():
    """Fetch a discovery document server-side and report the resolved endpoints.

    Lets the admin validate an OIDC discovery URL before attempting a real
    login — a wrong URL or unreachable IdP surfaces here instantly instead of
    mid-flow. Reads the URL from the request body so the *unsaved* field value
    can be tested; falls back to the stored setting."""
    payload = request.get_json(silent=True) or {}
    url = (payload.get("discovery_url") or get_setting("oidc_discovery_url", "")).strip()

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return jsonify(ok=False, error="URL de découverte invalide (http/https attendu)."), 400

    try:
        resp = requests.get(url, timeout=6, headers={"Accept": "application/json"})
        resp.raise_for_status()
        doc = resp.json()
    except requests.exceptions.JSONDecodeError:
        return jsonify(ok=False, error="La réponse n'est pas un document JSON OIDC valide."), 502
    except requests.RequestException as exc:
        return jsonify(ok=False, error=f"Échec de la requête : {exc}"), 502

    return jsonify(
        ok=True,
        issuer=doc.get("issuer"),
        authorization_endpoint=doc.get("authorization_endpoint"),
        token_endpoint=doc.get("token_endpoint"),
        jwks_uri=doc.get("jwks_uri"),
    )
