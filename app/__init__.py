import os
import secrets
import sys
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

from flask import Flask, g, redirect, request, session, url_for
from flask_login import LoginManager, current_user, logout_user

from .extensions import db
from .models import User


# Release identity. Build names follow the chameleon genus *Furcifer* — the
# colour-shifting lineage that inspired Claudio's theme-switching identity.
__version__ = "1.0.0"
__build_name__ = "Furcifer pardalis"


# The team plans from France — timestamps are stored UTC (TIMESTAMPTZ) but
# must be shown in local wall-clock time.
DISPLAY_TZ = ZoneInfo("Europe/Paris")


def _format_local_dt(value, fmt: str = "%d/%m/%Y à %H:%M") -> str:
    """Render a stored datetime in Europe/Paris local time.

    Naive values are assumed UTC (that's how ``_utcnow`` writes them); aware
    values are converted from whatever offset they carry. Returns "" for None
    so templates can pipe optional timestamps straight through."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(DISPLAY_TZ).strftime(fmt)


login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Veuillez vous connecter pour accéder au planning."
login_manager.login_message_category = "warning"


# Literal value shipped in docker-compose.yml as a placeholder. Refusing to
# boot with this string in production stops the most common deploy footgun.
_PLACEHOLDER_SECRET = "change-me-in-prod-please-use-a-long-random-string"


@login_manager.user_loader
def load_user(user_id: str):
    return db.session.get(User, int(user_id))


def create_app() -> Flask:
    app = Flask(__name__, instance_relative_config=False)

    secret = os.environ.get("SECRET_KEY")
    if not secret or len(secret) < 32:
        raise RuntimeError(
            "SECRET_KEY env var is missing or too short (must be >= 32 chars)."
        )
    # Session cookies default to Secure (HTTPS-only). Local dev over plain
    # HTTP must opt out with ALLOW_INSECURE_COOKIE=1 (the example
    # docker-compose sets this). A real HTTPS deployment leaves it unset.
    # This same flag marks an explicit "local dev" intent, so it also gates the
    # placeholder-secret escape hatch below.
    insecure_cookie_optout = os.environ.get("ALLOW_INSECURE_COOKIE") == "1"

    if secret == _PLACEHOLDER_SECRET:
        # The docker-compose default key is fine for `docker compose up` on a
        # laptop but lethal on a public-facing deploy. Fail closed unless the
        # operator has explicitly opted into local-dev mode.
        if insecure_cookie_optout:
            print(
                "[security] WARNING: SECRET_KEY is the docker-compose placeholder "
                "(allowed because ALLOW_INSECURE_COOKIE=1 marks local dev). "
                "Set a real random value (>= 32 chars) before going to production.",
                file=sys.stderr, flush=True,
            )
        else:
            raise RuntimeError(
                "Refusing to boot with the placeholder SECRET_KEY. Set a real "
                "random value (>= 32 chars). For local HTTP dev only, set "
                "ALLOW_INSECURE_COOKIE=1 to allow it."
            )

    app.config.update(
        SECRET_KEY=secret,
        SQLALCHEMY_DATABASE_URI=os.environ["DATABASE_URL"],
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=not insecure_cookie_optout,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        WTF_CSRF_TIME_LIMIT=None,
        # Let browsers cache static assets (CSS/logo) instead of revalidating
        # every navigation. The stylesheet link is version-stamped (?v=) so a
        # release busts the cache immediately despite the long lifetime.
        SEND_FILE_MAX_AGE_DEFAULT=timedelta(days=30),
        # CSP starts in report-only so a missed inline script can't break the
        # app on rollout; set CSP_REPORT_ONLY=0 to enforce after validation.
        CSP_REPORT_ONLY=os.environ.get("CSP_REPORT_ONLY", "1") != "0",
    )

    # Behind a TLS-terminating reverse proxy, honor X-Forwarded-* so
    # request.is_secure / remote_addr / scheme reflect the real client.
    # Gated on BEHIND_PROXY=1 — never enable blindly, the headers are
    # client-controlled when there's no proxy actually rewriting them.
    if os.environ.get("BEHIND_PROXY") == "1":
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(
            app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1,
        )

    # Local-time display filter: {{ some_dt | localdt }} → "30/05/2026 à 14:05"
    # in Europe/Paris regardless of the UTC storage offset.
    app.jinja_env.filters["localdt"] = _format_local_dt

    db.init_app(app)
    login_manager.init_app(app)

    from flask_wtf.csrf import CSRFProtect

    csrf = CSRFProtect(app)

    from .api import bp as api_bp
    from .api_admin import bp as api_admin_bp, swagger_bp as api_swagger_bp
    from .audit_admin import bp as audit_admin_bp
    from .auth import bp as auth_bp
    from .calendar_feed import bp as calendar_feed_bp
    from .clients import bp as clients_bp
    from .dashboard import bp as dashboard_bp
    from .holiday_admin import bp as holidays_admin_bp
    from .meetings import bp as meetings_bp
    from .mission_types import bp as mission_types_bp
    from .planning import bp as planning_bp
    from .projects import bp as projects_bp
    from .search import bp as search_bp
    from .sso_admin import bp as sso_admin_bp
    from .tasks import bp as tasks_bp
    from .users import bp as users_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(audit_admin_bp)
    app.register_blueprint(calendar_feed_bp)
    app.register_blueprint(planning_bp)
    app.register_blueprint(projects_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(mission_types_bp)
    app.register_blueprint(clients_bp)
    app.register_blueprint(tasks_bp)
    app.register_blueprint(meetings_bp)
    app.register_blueprint(search_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(holidays_admin_bp)
    app.register_blueprint(sso_admin_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(api_admin_bp)
    app.register_blueprint(api_swagger_bp)

    # The integration API authenticates with a bearer token, not the session
    # cookie, so the form-oriented CSRF protection doesn't apply (and would
    # reject legitimate machine clients). The admin token-management pages stay
    # CSRF-protected — only the /api/v1 surface is exempt.
    csrf.exempt(api_bp)

    _register_security(app)

    @app.context_processor
    def inject_version():
        return {"app_version": __version__, "app_build_name": __build_name__}

    @app.context_processor
    def inject_csp_nonce():
        # Per-request nonce so inline <script nonce="..."> tags satisfy CSP.
        return {"csp_nonce": g.get("csp_nonce", "")}

    @app.context_processor
    def inject_oidc():
        # Resolve once per request (cached on flask.g) so templates can show the
        # SSO login button + label. Defensive: never let a DB hiccup (or a
        # not-yet-migrated app_settings table) break page rendering — fall back
        # to "SSO off". Env-mode resolution never touches the DB.
        from .settings_store import get_oidc_config

        try:
            cfg = get_oidc_config()
        except Exception:  # noqa: BLE001 — rendering must not depend on the DB
            cfg = None
        return {
            "oidc_enabled": cfg is not None,
            "oidc_button_label": cfg["button_label"] if cfg else None,
            "oidc_provider": cfg["provider"] if cfg else None,
        }

    @app.route("/")
    def index():
        # Pure pentesters (no admin / planificateur role) land on their
        # personal dashboard; managers keep the global calendar default.
        if current_user.is_authenticated and not current_user.can_manage_events:
            return redirect(url_for("dashboard.home"))
        return redirect(url_for("planning.calendar_default"))

    @app.route("/healthz")
    def healthz():
        # Liveness + DB connectivity probe for load balancers / compose. No
        # auth (it leaks nothing) and never raises — returns 503 on DB trouble.
        from sqlalchemy import text
        try:
            db.session.execute(text("SELECT 1"))
            return {"status": "ok"}, 200
        except Exception:  # noqa: BLE001
            db.session.rollback()
            return {"status": "error"}, 503

    _register_error_handlers(app)

    return app


# Endpoints an authenticated user may still reach while forced to change their
# password (or being logged out for a stale session) — avoids a redirect loop.
_ACCOUNT_STATE_EXEMPT = {"auth.change_password", "auth.logout", "healthz", "static"}


def _register_security(app: Flask) -> None:
    """Per-request security wiring: CSP nonce, session/account-state checks,
    and hardened response headers."""

    @app.before_request
    def _csp_nonce():
        g.csp_nonce = secrets.token_urlsafe(16)

    @app.before_request
    def _enforce_account_state():
        if not current_user.is_authenticated:
            return None

        # Session invalidation: a password change rotates the user's
        # session_token, orphaning every other session. Only enforced for
        # sessions that actually carry a token (pre-feature sessions don't, and
        # are left alone until they re-login).
        tok = session.get("_user_token")
        if (
            tok is not None
            and current_user.session_token is not None
            and tok != current_user.session_token
        ):
            session.pop("_user_token", None)
            logout_user()
            return redirect(url_for("auth.login"))

        # Forced password change (seeded / temporary credentials).
        if current_user.must_change_password and request.endpoint not in _ACCOUNT_STATE_EXEMPT:
            return redirect(url_for("auth.change_password"))
        return None

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
        )
        resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        # HSTS only over HTTPS so plain-HTTP local dev is unaffected.
        if request.is_secure:
            resp.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )

        nonce = g.get("csp_nonce", "")
        if request.path.startswith("/admin/api/docs"):
            # Swagger UI (vendored by flask-swagger-ui, served from 'self')
            # bootstraps via an inline <script> we don't control, so it needs
            # 'unsafe-inline' for scripts. Browsers ignore 'unsafe-inline' when a
            # nonce is present, so we drop the nonce on this path. Scoped to the
            # admin-only docs page; the rest of the app keeps the strict policy.
            csp = (
                "default-src 'self'; "
                "base-uri 'self'; "
                "object-src 'none'; "
                "frame-ancestors 'none'; "
                "img-src 'self' data:; "
                "font-src 'self'; "
                "style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'; "
                "connect-src 'self'; "
                "form-action 'self'"
            )
        else:
            csp = (
                "default-src 'self'; "
                "base-uri 'self'; "
                "object-src 'none'; "
                "frame-ancestors 'none'; "
                "img-src 'self' data:; "
                "font-src 'self'; "
                "style-src 'self' 'unsafe-inline'; "
                f"script-src 'self' 'nonce-{nonce}'; "
                # Existing inline on* handlers (confirm dialogs) stay allowed; the
                # primary XSS vector (injected <script>) is blocked by the nonce.
                "script-src-attr 'unsafe-inline'; "
                "connect-src 'self'; "
                "form-action 'self'"
            )
        header = (
            "Content-Security-Policy-Report-Only"
            if app.config.get("CSP_REPORT_ONLY", True)
            else "Content-Security-Policy"
        )
        resp.headers.setdefault(header, csp)
        return resp


def _register_error_handlers(app: Flask) -> None:
    from flask import jsonify, render_template

    # Machine clients hit /api/* and expect JSON, never the HTML error page —
    # even for routing misses (404) and unexpected failures (500) that the
    # blueprint itself can't intercept.
    _API_ERROR_CODES = {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        500: "internal_error",
    }

    def _error_page(code: int, title: str, message: str):
        if request.path.startswith("/api/"):
            resp = jsonify(error={"code": _API_ERROR_CODES.get(code, "error"),
                                  "message": message})
            resp.status_code = code
            return resp
        return (
            render_template("error.html", code=code, title=title, message=message),
            code,
        )

    @app.errorhandler(403)
    def _forbidden(_exc):
        return _error_page(
            403,
            "Accès refusé",
            "Vous n'avez pas les droits nécessaires pour effectuer cette action. "
            "Si vous pensez que c'est une erreur, contactez un administrateur.",
        )

    @app.errorhandler(404)
    def _not_found(_exc):
        return _error_page(
            404,
            "Page introuvable",
            "La page demandée n'existe pas ou a été déplacée.",
        )

    @app.errorhandler(405)
    def _method_not_allowed(_exc):
        return _error_page(
            405,
            "Méthode non autorisée",
            "Cette action n'est pas permise sur cette ressource.",
        )

    @app.errorhandler(500)
    def _server_error(_exc):
        return _error_page(
            500,
            "Erreur interne",
            "Une erreur inattendue s'est produite. L'équipe technique a été notifiée.",
        )
