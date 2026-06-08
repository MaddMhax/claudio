import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from authlib.integrations.base_client import OAuthError
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from flask_wtf import FlaskForm
from wtforms import PasswordField, StringField
from wtforms.validators import DataRequired, EqualTo, Length

from .audit import record as audit_record
from .extensions import db
from .models import User
from .oidc import get_oidc_client


bp = Blueprint("auth", __name__, url_prefix="/auth")


# After this many consecutive failures an account is temporarily locked, with
# the window growing on each further failure (capped at 1h). DB-backed so the
# lockout is consistent across gunicorn workers.
MAX_FAILED_BEFORE_LOCK = 5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _register_failed_login(user: User) -> None:
    """Bump the failure counter and (re)arm the lockout with exponential backoff."""
    user.failed_login_count = (user.failed_login_count or 0) + 1
    if user.failed_login_count >= MAX_FAILED_BEFORE_LOCK:
        over = user.failed_login_count - MAX_FAILED_BEFORE_LOCK
        minutes = min(60, 2 ** over)  # 1, 2, 4, 8, 16, 32, 60, …
        user.locked_until = _utcnow() + timedelta(minutes=minutes)
    db.session.commit()


def _establish_session(user: User) -> None:
    """Log the user in and bind this session to their current session token."""
    if user.session_token is None:
        user.rotate_session_token()
    user.failed_login_count = 0
    user.locked_until = None
    db.session.commit()
    login_user(user, remember=False)
    session["_user_token"] = user.session_token


def _safe_next(target: str | None) -> str | None:
    """Validate the ``next`` query param before redirecting through it.

    Accepts only same-origin **relative** paths (``/something``). Anything that
    carries a scheme, a host, a backslash (Windows-style scheme separator), or
    a protocol-relative ``//host`` is rejected so we can never bounce a
    freshly-authed user onto an attacker-controlled domain."""
    if not target:
        return None
    if not target.startswith("/"):
        return None
    if target.startswith("//") or target.startswith("/\\"):
        return None
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return None
    return target


class LoginForm(FlaskForm):
    username = StringField("Nom d'utilisateur", validators=[DataRequired(), Length(max=64)])
    password = PasswordField("Mot de passe", validators=[DataRequired(), Length(max=256)])


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("planning.calendar_default"))

    form = LoginForm()
    if form.validate_on_submit():
        # constant-ish lookup time to mitigate user-enumeration timing
        start = time.perf_counter()
        username = form.username.data.strip()
        user = db.session.execute(
            db.select(User).where(User.username == username)
        ).scalar_one_or_none()

        locked = user is not None and user.is_locked
        valid = (
            user is not None and not locked and user.check_password(form.password.data)
        )

        # ensure at least 250ms per attempt to slow brute-forcing
        elapsed = time.perf_counter() - start
        if elapsed < 0.25:
            time.sleep(0.25 - elapsed)

        if locked:
            audit_record("login.locked", target=username, actor=user, commit=True)
            flash(
                "Trop de tentatives échouées : ce compte est temporairement "
                "verrouillé. Réessayez dans quelques minutes.",
                "danger",
            )
        elif valid:
            _establish_session(user)
            audit_record("login.success", target=username, actor=user, commit=True)
            target = _safe_next(request.args.get("next")) or url_for(
                "planning.calendar_default"
            )
            return redirect(target)
        else:
            if user is not None:
                _register_failed_login(user)
            audit_record("login.failure", target=username, actor=user, commit=True)
            flash("Nom d'utilisateur ou mot de passe invalide.", "danger")

    return render_template("login.html", form=form)


@bp.route("/oidc/login")
def oidc_login():
    """Kick off the OIDC authorization-code flow (redirect to the IdP)."""
    client, cfg = get_oidc_client()
    if client is None:
        abort(404)
    if current_user.is_authenticated:
        return redirect(url_for("planning.calendar_default"))

    # Carry a validated post-login target across the round-trip via the
    # session (the IdP redirect can't be trusted to preserve our query string).
    nxt = _safe_next(request.args.get("next"))
    if nxt:
        session["oidc_next"] = nxt

    # Use the explicit redirect URI when configured (deterministic, and the
    # only reliable option behind a TLS-terminating proxy where url_for can
    # otherwise emit the wrong scheme/host). Must match the URI registered at
    # the provider exactly.
    redirect_uri = cfg["redirect_uri"] or url_for(
        "auth.oidc_callback", _external=True
    )
    return client.authorize_redirect(redirect_uri)


@bp.route("/oidc/callback")
def oidc_callback():
    """Handle the IdP redirect: exchange the code, then log the user in."""
    client, cfg = get_oidc_client()
    if client is None:
        abort(404)

    try:
        token = client.authorize_access_token()
    except OAuthError as exc:
        current_app.logger.warning("OIDC callback failed: %s", exc)
        flash(
            "La connexion SSO a échoué. Réessayez ou utilisez vos identifiants.",
            "danger",
        )
        return redirect(url_for("auth.login"))

    claims = token.get("userinfo") or {}
    sub = claims.get("sub")
    if not sub:
        flash("Réponse SSO invalide (identifiant manquant).", "danger")
        return redirect(url_for("auth.login"))

    user = _resolve_oidc_user(sub, claims, cfg)
    if user is None:
        flash(
            "Aucun compte ne correspond à cette identité SSO. "
            "Contactez un administrateur.",
            "danger",
        )
        return redirect(url_for("auth.login"))

    _establish_session(user)
    audit_record("login.success", target=user.username, actor=user,
                 detail="SSO/OIDC", commit=True)
    target = _safe_next(session.pop("oidc_next", None)) or url_for(
        "planning.calendar_default"
    )
    return redirect(target)


def _resolve_oidc_user(sub: str, claims: dict, cfg: dict) -> User | None:
    """Map an OIDC identity onto a local ``User``.

    Linking precedence:
      1. a user already bound to this ``sub``;
      2. an existing user whose username matches the configured claim — bound
         to ``sub`` on this first SSO login;
      3. a freshly provisioned user, only when auto-create is enabled.

    Returns ``None`` when nothing matches and auto-create is disabled, so the
    caller can refuse the login."""
    user = db.session.execute(
        db.select(User).where(User.oidc_sub == sub)
    ).scalar_one_or_none()
    if user is not None:
        return user

    username = (claims.get(cfg["username_claim"]) or "").strip()
    if username:
        user = db.session.execute(
            db.select(User).where(User.username == username)
        ).scalar_one_or_none()
        if user is not None:
            user.oidc_sub = sub  # bind the federated identity on first login
            db.session.commit()
            return user

    if not cfg["auto_create"] or not username:
        return None

    full_name = (claims.get(cfg["name_claim"]) or username).strip()
    user = User(username=username, full_name=full_name, oidc_sub=sub)
    user.roles = list(cfg["default_roles"])
    db.session.add(user)
    db.session.commit()
    return user


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    audit_record("logout", target=current_user.username, commit=True)
    session.pop("_user_token", None)
    logout_user()
    flash("Vous avez été déconnecté.", "info")
    return redirect(url_for("auth.login"))


class PasswordChangeForm(FlaskForm):
    current_password = PasswordField(
        "Mot de passe actuel", validators=[DataRequired(), Length(max=256)]
    )
    new_password = PasswordField(
        "Nouveau mot de passe",
        validators=[
            DataRequired(),
            Length(min=8, max=256, message="Au moins 8 caractères."),
        ],
    )
    confirm_password = PasswordField(
        "Confirmer le nouveau mot de passe",
        validators=[
            DataRequired(),
            EqualTo("new_password", message="Les mots de passe ne correspondent pas."),
        ],
    )


@bp.route("/password", methods=["GET", "POST"])
@login_required
def change_password():
    """Let a logged-in user change their own password."""
    form = PasswordChangeForm()
    if form.validate_on_submit():
        if not current_user.check_password(form.current_password.data):
            form.current_password.errors.append("Mot de passe actuel incorrect.")
        elif current_user.check_password(form.new_password.data):
            form.new_password.errors.append(
                "Le nouveau mot de passe doit être différent de l'actuel."
            )
        else:
            current_user.set_password(form.new_password.data)
            current_user.must_change_password = False
            db.session.commit()
            # set_password rotated the session token (logging out other
            # sessions); keep *this* session valid by re-syncing it.
            session["_user_token"] = current_user.session_token
            audit_record("password.change", target=current_user.username, commit=True)
            flash("Votre mot de passe a été modifié.", "success")
            return redirect(url_for("planning.calendar_default"))
    return render_template("change_password.html", form=form)
