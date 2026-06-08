"""Read-only integration API (``/api/v1``).

Exposes a small, stable JSON surface for external tooling — today the Pwndoc
report generator, which pulls project metadata to seed an audit report. The API
is intentionally minimal and read-only:

  * Authentication is a bearer token (``Authorization: Bearer <token>``) that
    maps to an :class:`~app.models.ApiToken` row. Tokens are stored hashed; a
    revoked/unknown token is indistinguishable from a missing one (401).
  * It never touches the login session or the human role system, so a leaked
    integration token can't be used to drive the web UI.
  * Errors are JSON (``{"error": {"code", "message"}}``) with the right status,
    never the HTML error pages the browser app serves.

The matching admin UI (mint/revoke tokens, Swagger docs) lives in
``app.api_admin``; the OpenAPI document describing these routes is built by
:func:`build_openapi_spec` and served from there.
"""
import hashlib
import secrets
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, g, jsonify, request

from .extensions import db
from .models import API_SCOPE_READ_ONLY, ApiToken, Event, Project


bp = Blueprint("api", __name__, url_prefix="/api/v1")

# Plaintext tokens are prefixed so they're recognisable in logs / config files
# and obviously ours. The random part is 256 bits of urlsafe entropy.
TOKEN_PREFIX = "cld_"


def generate_token() -> str:
    """Mint a fresh plaintext token (shown to the admin once, never stored)."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(plaintext: str) -> str:
    """Stable SHA-256 hex digest — what we persist and look up by.

    A token is high-entropy random, so a plain (unsalted) cryptographic hash is
    sufficient: there's nothing to brute-force the way there is with passwords,
    and a deterministic digest lets us look the token up with an indexed query."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _json_error(status: int, code: str, message: str):
    resp = jsonify(error={"code": code, "message": message})
    resp.status_code = status
    return resp


def _authenticate() -> ApiToken | None:
    """Resolve the bearer token on the current request to an active ApiToken,
    or ``None`` if absent/invalid. Never raises."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    presented = auth[len("Bearer "):].strip()
    if not presented:
        return None
    return db.session.execute(
        db.select(ApiToken).where(
            ApiToken.token_hash == hash_token(presented),
            ApiToken.active.is_(True),
        )
    ).scalar_one_or_none()


def api_token_required(scope: str = API_SCOPE_READ_ONLY):
    """Guard an API view with bearer-token auth and a minimum scope.

    Today every route needs only ``read_only`` and every token has at least
    that, so the scope check is a forward-looking no-op; it's wired now so a
    future write route can demand a stronger scope without reworking the
    middleware. On success the token is stashed on ``flask.g.api_token`` and its
    ``last_used_at`` heartbeat is bumped."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            token = _authenticate()
            if token is None:
                return _json_error(
                    401, "unauthorized",
                    "Missing or invalid API token. Send 'Authorization: "
                    "Bearer <token>'.",
                )
            # Scope ladder: read_only < read_write. A read_only token may only
            # reach read_only routes.
            if scope != API_SCOPE_READ_ONLY and token.scope == API_SCOPE_READ_ONLY:
                return _json_error(
                    403, "forbidden",
                    "This token is read-only and cannot perform this action.",
                )
            token.last_used_at = datetime.now(timezone.utc)
            db.session.commit()
            g.api_token = token
            return view(*args, **kwargs)
        return wrapped
    return decorator


def _project_payload(p: Project) -> dict:
    """Serialise the public, read-only view of a project.

    ``reference_interne`` is the project's internal reference / quote number —
    stored on the model as ``code`` (the field labelled « Référence interne »
    in the project form). Exposed under its functional name so the Pwndoc side
    reads cleanly."""
    return {
        "id": p.id,
        "name": p.name,
        "reference_interne": p.code,
        # Mission references (id + title) so a client can discover the mission
        # ids of a project and then pull each one's detail (incl. nombre_jh)
        # via GET /missions/{id}. Ordered by start_date (the relationship's
        # order_by). Title only — the full detail lives on the mission route.
        "missions": [
            {"id": m.id, "title": m.title} for m in p.missions
        ],
    }


def _mission_payload(ev: Event) -> dict:
    """Serialise the public, read-only view of a mission (an :class:`Event`).

    ``nombre_jh`` is the jours-homme total — worked days in the date span times
    the number of assigned pentesters. It's computed by the canonical
    :func:`app.planning._computed_jh` (imported lazily to avoid a heavy import
    cycle through the planning blueprint) so the API and the web form never
    disagree on the maths."""
    from .planning import _computed_jh

    return {
        "id": ev.id,
        "title": ev.title,
        "project_id": ev.project_id,
        "start_date": ev.start_date.isoformat() if ev.start_date else None,
        "end_date": ev.end_date.isoformat() if ev.end_date else None,
        "nombre_jh": _computed_jh(ev),
    }


@bp.route("/missions/<int:mission_id>", methods=["GET"])
@api_token_required()
def get_mission(mission_id: int):
    """Fetch a single mission with its computed JH (jours-homme) total."""
    mission = db.session.get(Event, mission_id)
    if mission is None:
        return _json_error(404, "not_found", f"No mission with id {mission_id}.")
    return jsonify(_mission_payload(mission))


@bp.route("/projects", methods=["GET"])
@api_token_required()
def list_projects():
    """List every project with its name and internal reference."""
    projects = db.session.execute(
        db.select(Project).order_by(Project.name)
    ).scalars().all()
    return jsonify(
        projects=[_project_payload(p) for p in projects],
        count=len(projects),
    )


@bp.route("/projects/<int:project_id>", methods=["GET"])
@api_token_required()
def get_project(project_id: int):
    """Fetch a single project's name and internal reference by id."""
    project = db.session.get(Project, project_id)
    if project is None:
        return _json_error(404, "not_found", f"No project with id {project_id}.")
    return jsonify(_project_payload(project))


# --- OpenAPI 3.0 document -------------------------------------------------
# Hand-written (the surface is tiny) so we carry no spec-generation dependency.
# Served — and rendered via Swagger UI — from app.api_admin, admin-only.
def build_openapi_spec(server_url: str) -> dict:
    """Return the OpenAPI 3.0 description of this blueprint.

    ``server_url`` is resolved per-request by the admin route so the "Try it
    out" button targets the host the docs are actually served from."""
    project_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "integer", "example": 1},
            "name": {"type": "string", "example": "Audit applicatif ACME"},
            "reference_interne": {
                "type": "string",
                "nullable": True,
                "example": "REF-2026-042",
                "description": "Référence interne / numéro de devis du projet.",
            },
            "missions": {
                "type": "array",
                "description": (
                    "Références des missions du projet (id + intitulé). "
                    "Utiliser l'id avec GET /missions/{mission_id} pour le "
                    "détail, dont le nombre de JH."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "example": 7},
                        "title": {
                            "type": "string",
                            "example": "Audit applicatif ACME — phase 1",
                        },
                    },
                },
            },
        },
    }
    mission_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "integer", "example": 7},
            "title": {"type": "string", "example": "Audit applicatif ACME — phase 1"},
            "project_id": {
                "type": "integer",
                "nullable": True,
                "example": 1,
                "description": "Projet parent, ou null pour un événement hors-projet.",
            },
            "start_date": {
                "type": "string", "format": "date", "nullable": True, "example": "2026-06-08",
            },
            "end_date": {
                "type": "string", "format": "date", "nullable": True, "example": "2026-06-12",
            },
            "nombre_jh": {
                "type": "integer",
                "example": 10,
                "description": (
                    "Nombre de jours-homme (JH) : jours ouvrés de la mission × "
                    "nombre de pentesters affectés."
                ),
            },
        },
    }
    error_schema = {
        "type": "object",
        "properties": {
            "error": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "example": "not_found"},
                    "message": {"type": "string"},
                },
            }
        },
    }
    unauthorized = {
        "description": "Token manquant, invalide ou révoqué.",
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}},
    }
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Claudio — API d'intégration",
            "version": "1.0.0",
            "description": (
                "API en lecture seule exposant les données du planning Claudio "
                "pour des outils externes (ex. import Pwndoc). "
                "Authentification par jeton porteur."
            ),
        },
        "servers": [{"url": server_url}],
        "security": [{"bearerAuth": []}],
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "Jeton d'API Claudio (préfixe « cld_ »).",
                }
            },
            "schemas": {
                "Project": project_schema,
                "Mission": mission_schema,
                "Error": error_schema,
            },
        },
        "paths": {
            "/projects": {
                "get": {
                    "summary": "Lister les projets",
                    "operationId": "listProjects",
                    "responses": {
                        "200": {
                            "description": "Liste des projets.",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "count": {"type": "integer"},
                                            "projects": {
                                                "type": "array",
                                                "items": {"$ref": "#/components/schemas/Project"},
                                            },
                                        },
                                    }
                                }
                            },
                        },
                        "401": unauthorized,
                    },
                }
            },
            "/projects/{project_id}": {
                "get": {
                    "summary": "Récupérer un projet (nom + référence interne)",
                    "operationId": "getProject",
                    "parameters": [
                        {
                            "name": "project_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Le projet demandé.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Project"}
                                }
                            },
                        },
                        "401": unauthorized,
                        "404": {
                            "description": "Projet introuvable.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Error"}
                                }
                            },
                        },
                    },
                }
            },
            "/missions/{mission_id}": {
                "get": {
                    "summary": "Récupérer une mission (avec son nombre de JH)",
                    "operationId": "getMission",
                    "parameters": [
                        {
                            "name": "mission_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "La mission demandée.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Mission"}
                                }
                            },
                        },
                        "401": unauthorized,
                        "404": {
                            "description": "Mission introuvable.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Error"}
                                }
                            },
                        },
                    },
                }
            },
        },
    }
