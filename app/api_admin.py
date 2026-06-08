"""Admin UI for the integration API: token management + Swagger docs.

Two pieces, both admin-only:

  * ``/admin/api`` — mint and revoke :class:`~app.models.ApiToken` rows. A freshly
    minted token's plaintext is shown **once** (we only persist its hash), then
    never again.
  * ``/admin/api/docs`` — interactive Swagger UI (served by ``flask-swagger-ui``
    from vendored assets, no CDN) pointed at ``/admin/api/openapi.json``.

The Swagger UI blueprint is created here and gated by its own ``before_request``
so its page *and* its bundled JS/CSS require an admin session.
"""
from functools import wraps

from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required
from flask_swagger_ui import get_swaggerui_blueprint
from flask_wtf import FlaskForm
from wtforms import StringField
from wtforms.validators import DataRequired, Length

from .api import build_openapi_spec, generate_token, hash_token
from .audit import record as audit_record
from .extensions import db
from .models import API_SCOPE_READ_ONLY, ApiToken


bp = Blueprint("api_admin", __name__, url_prefix="/admin/api")

# Mounted in create_app(). Kept in sync with the Swagger UI mount point so the
# CSP relaxation in app.__init__ can scope itself to exactly this prefix.
SWAGGER_URL = "/admin/api/docs"
OPENAPI_JSON_PATH = "/admin/api/openapi.json"


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


class CreateTokenForm(FlaskForm):
    label = StringField(
        "Libellé", validators=[DataRequired(), Length(min=2, max=80)]
    )


@bp.route("", methods=["GET", "POST"])
@admin_required
def list_tokens():
    form = CreateTokenForm()
    # The one-time plaintext is carried across the POST→GET redirect in the
    # signed, HttpOnly session (popped on display) — never in the URL, so it
    # can't leak into browser history or access logs.
    new_token = session.pop("_new_api_token", None)

    if form.validate_on_submit():
        plaintext = generate_token()
        token = ApiToken(
            label=form.label.data.strip(),
            token_hash=hash_token(plaintext),
            scope=API_SCOPE_READ_ONLY,
            created_by_id=current_user.id,
        )
        db.session.add(token)
        audit_record("api.token.create", target=token.label,
                     detail=f"scope={API_SCOPE_READ_ONLY}")
        db.session.commit()
        session["_new_api_token"] = plaintext
        flash(
            "Jeton créé. Copiez-le maintenant : il ne sera plus jamais affiché.",
            "success",
        )
        return redirect(url_for("api_admin.list_tokens"))

    tokens = db.session.execute(
        db.select(ApiToken).order_by(ApiToken.created_at.desc())
    ).scalars().all()
    return render_template(
        "api_tokens.html",
        form=form,
        tokens=tokens,
        new_token=new_token,
        swagger_url=SWAGGER_URL,
    )


@bp.route("/tokens/<int:token_id>/revoke", methods=["POST"])
@admin_required
def revoke_token(token_id: int):
    token = db.session.get(ApiToken, token_id)
    if token is None:
        abort(404)
    if token.active:
        token.active = False
        audit_record("api.token.revoke", target=token.label)
        db.session.commit()
        flash(f"Jeton « {token.label} » révoqué.", "info")
    return redirect(url_for("api_admin.list_tokens"))


@bp.route("/openapi.json", methods=["GET"])
@admin_required
def openapi_json():
    """Serve the OpenAPI document, with the server URL resolved to this host so
    Swagger UI's "Try it out" targets the right origin."""
    server_url = request.url_root.rstrip("/") + "/api/v1"
    return jsonify(build_openapi_spec(server_url))


# --- Swagger UI blueprint (admin-gated) -----------------------------------
swagger_bp = get_swaggerui_blueprint(
    SWAGGER_URL,
    OPENAPI_JSON_PATH,
    config={"app_name": "Claudio — API d'intégration"},
)


@swagger_bp.before_request
def _gate_swagger():
    """Require an admin session for the docs page and its bundled assets."""
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login", next=request.path))
    if not current_user.is_admin:
        abort(403)
    return None
