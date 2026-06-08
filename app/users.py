import re
from functools import wraps

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from wtforms import PasswordField, SelectMultipleField, StringField
from wtforms.validators import DataRequired, Length, Optional, Regexp

from .audit import record as audit_record
from .extensions import db
from .models import (
    ROLE_ADMIN,
    SYSTEM_ROLES,
    Role,
    User,
    role_label_lookup,
)


bp = Blueprint("users", __name__, url_prefix="/admin/users")


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def manage_required(view):
    """Allow admins *and* planificateurs (anyone with ``can_manage_events``).

    Used for screens a planificateur legitimately drives — tasks, the worked
    calendar — without granting full admin (users, roles, mission types)."""
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.can_manage_events:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


class UserForm(FlaskForm):
    username = StringField(
        "Nom d'utilisateur",
        validators=[
            DataRequired(),
            Length(min=3, max=64),
            Regexp(r"^[a-zA-Z0-9._-]+$", message="Caractères autorisés : a-z, 0-9, point, tiret, underscore."),
        ],
    )
    full_name = StringField("Nom complet", validators=[DataRequired(), Length(max=120)])
    roles = SelectMultipleField(
        "Rôles",
        validators=[DataRequired(message="Sélectionnez au moins un rôle.")],
    )
    color = StringField(
        "Couleur",
        validators=[
            DataRequired(),
            Regexp(r"^#[0-9a-fA-F]{6}$", message="Format attendu : #RRGGBB."),
        ],
        default="#3b82f6",
    )
    password = PasswordField(
        "Mot de passe",
        validators=[Optional(), Length(min=8, max=256)],
    )

    def populate_choices(self) -> None:
        """System roles first (immutable), then admin-managed roles by label."""
        system = [(key, label) for key, label in SYSTEM_ROLES.items()]
        dynamic = [
            (r.key, r.label) for r in db.session.execute(
                db.select(Role).order_by(Role.label)
            ).scalars().all()
        ]
        self.roles.choices = system + dynamic


class RoleForm(FlaskForm):
    label = StringField(
        "Libellé",
        validators=[DataRequired(), Length(min=2, max=80)],
    )
    color = StringField(
        "Couleur",
        validators=[
            DataRequired(),
            Regexp(r"^#[0-9a-fA-F]{6}$", message="Format attendu : #RRGGBB."),
        ],
        default="#3b82f6",
    )


USER_TECH_FILTERS = ("all", "technical", "non_technical")


@bp.route("/")
@admin_required
def list_users():
    tech = request.args.get("tech", "all")
    if tech not in USER_TECH_FILTERS:
        tech = "all"

    users = db.session.execute(
        db.select(User).order_by(User.full_name)
    ).scalars().all()

    counts = {
        "all": len(users),
        "technical": sum(1 for u in users if u.is_technical),
    }
    counts["non_technical"] = counts["all"] - counts["technical"]

    if tech == "technical":
        users = [u for u in users if u.is_technical]
    elif tech == "non_technical":
        users = [u for u in users if not u.is_technical]

    roles = db.session.execute(
        db.select(Role).order_by(Role.label)
    ).scalars().all()
    # Count user assignments per role so admins see which ones are in use
    # before they reach for delete.
    role_usage = {
        r.id: db.session.execute(
            db.select(db.func.count())
            .select_from(db.text("user_roles"))
            .where(db.text("user_roles.role = :key")).params(key=r.key)
        ).scalar_one()
        for r in roles
    }

    return render_template(
        "users_list.html",
        users=users,
        tech_filter=tech,
        tech_counts=counts,
        roles=roles,
        role_usage=role_usage,
        role_form=RoleForm(),
        role_labels=role_label_lookup(),
    )


@bp.route("/new", methods=["GET", "POST"])
@admin_required
def create_user():
    form = UserForm()
    form.populate_choices()

    if form.validate_on_submit():
        if not form.password.data:
            form.password.errors.append("Le mot de passe est requis à la création.")
        elif _username_taken(form.username.data.strip()):
            form.username.errors.append("Ce nom d'utilisateur existe déjà.")
        elif _invalid_roles(form.roles.data):
            form.roles.errors.append("Rôle inconnu.")
        else:
            user = User(
                username=form.username.data.strip(),
                full_name=form.full_name.data.strip(),
                color=form.color.data,
            )
            user.set_password(form.password.data)
            db.session.add(user)
            db.session.flush()  # need user.id before linking roles
            user.roles = list(form.roles.data)
            audit_record("user.create", target=user.username,
                         detail="rôles: " + ", ".join(sorted(form.roles.data)))
            db.session.commit()
            flash(f"Utilisateur « {user.username} » créé.", "success")
            return redirect(url_for("users.list_users"))

    return render_template("user_form.html", form=form, mode="new")


@bp.route("/<int:user_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_user(user_id: int):
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)

    form = UserForm(obj=user)
    form.populate_choices()
    if request.method == "GET":
        form.roles.data = list(user.roles)

    if form.validate_on_submit():
        new_username = form.username.data.strip()
        new_roles = set(form.roles.data)
        if new_username != user.username and _username_taken(new_username):
            form.username.errors.append("Ce nom d'utilisateur existe déjà.")
        elif _invalid_roles(form.roles.data):
            form.roles.errors.append("Rôle inconnu.")
        elif user.id == current_user.id and ROLE_ADMIN not in new_roles:
            form.roles.errors.append(
                "Vous ne pouvez pas retirer le rôle administrateur de votre propre compte."
            )
        else:
            user.username = new_username
            user.full_name = form.full_name.data.strip()
            user.color = form.color.data
            user.roles = list(new_roles)
            if form.password.data:
                user.set_password(form.password.data)
            audit_record("user.update", target=user.username,
                         detail="rôles: " + ", ".join(sorted(new_roles)))
            db.session.commit()
            flash(f"Utilisateur « {user.username} » mis à jour.", "success")
            return redirect(url_for("users.list_users"))

    return render_template("user_form.html", form=form, mode="edit", user=user)


@bp.route("/<int:user_id>/unlink-sso", methods=["POST"])
@admin_required
def unlink_sso(user_id: int):
    """Sever the federated (OIDC) link on an account.

    Clearing ``oidc_sub`` means the next SSO login re-links by username (or is
    refused). If the account has no password either, it's left with no way in —
    warn the admin so they can set one."""
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    if user.oidc_sub is None:
        flash(f"« {user.username} » n'est pas lié à un compte SSO.", "info")
        return redirect(url_for("users.list_users"))

    user.oidc_sub = None
    audit_record("user.sso_unlink", target=user.username)
    db.session.commit()
    if not user.password_hash:
        flash(
            f"Lien SSO retiré pour « {user.username} ». Attention : ce compte "
            "n'a aucun mot de passe et ne peut donc plus se connecter — "
            "définissez-en un.",
            "warning",
        )
    else:
        flash(f"Lien SSO retiré pour « {user.username} ».", "info")
    return redirect(url_for("users.list_users"))


@bp.route("/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id: int):
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    if user.id == current_user.id:
        flash("Vous ne pouvez pas supprimer votre propre compte.", "danger")
        return redirect(url_for("users.list_users"))

    username = user.username
    db.session.delete(user)
    audit_record("user.delete", target=username)
    db.session.commit()
    flash(f"Utilisateur « {username} » supprimé.", "info")
    return redirect(url_for("users.list_users"))


# ===== Rôles (admin-managed specialties) =====

@bp.route("/roles/new", methods=["POST"])
@admin_required
def create_role():
    form = RoleForm()
    if not form.validate_on_submit():
        for field, errors in form.errors.items():
            for e in errors:
                flash(f"Rôle — {field} : {e}", "danger")
        return redirect(url_for("users.list_users"))
    label = form.label.data.strip()
    key = _slugify(label)
    if not key:
        flash("Libellé invalide — impossible d'en dériver un identifiant.", "danger")
        return redirect(url_for("users.list_users"))
    if key in SYSTEM_ROLES:
        flash(f"« {label} » est un rôle système réservé.", "danger")
        return redirect(url_for("users.list_users"))
    if _role_key_taken(key):
        flash(f"Un rôle dérive déjà l'identifiant « {key} ».", "danger")
        return redirect(url_for("users.list_users"))
    role = Role(key=key, label=label, color=form.color.data)
    db.session.add(role)
    audit_record("role.create", target=key, detail=label)
    db.session.commit()
    flash(f"Rôle « {role.label} » créé.", "success")
    return redirect(url_for("users.list_users"))


@bp.route("/roles/<int:role_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_role(role_id: int):
    role = db.session.get(Role, role_id)
    if role is None:
        abort(404)
    form = RoleForm(obj=role)
    if form.validate_on_submit():
        role.label = form.label.data.strip()
        role.color = form.color.data
        db.session.commit()
        flash(f"Rôle « {role.label} » mis à jour.", "success")
        return redirect(url_for("users.list_users"))
    return render_template("role_form.html", form=form, role=role)


@bp.route("/roles/<int:role_id>/delete", methods=["POST"])
@admin_required
def delete_role(role_id: int):
    role = db.session.get(Role, role_id)
    if role is None:
        abort(404)
    label = role.label
    key = role.key
    # Drop UserRole rows pointing to this key — the FKs on MissionSubtype /
    # Event use ON DELETE SET NULL so they self-clean.
    db.session.execute(
        db.text("DELETE FROM user_roles WHERE role = :key"), {"key": key}
    )
    db.session.delete(role)
    audit_record("role.delete", target=key, detail=label)
    db.session.commit()
    flash(f"Rôle « {label} » supprimé.", "info")
    return redirect(url_for("users.list_users"))


def _username_taken(username: str) -> bool:
    return db.session.execute(
        db.select(User.id).where(User.username == username)
    ).scalar_one_or_none() is not None


def _invalid_roles(roles) -> bool:
    """Reject any role key that's neither a system role nor a Role row."""
    valid = set(SYSTEM_ROLES) | {
        r.key for r in db.session.execute(db.select(Role)).scalars().all()
    }
    return any(r not in valid for r in roles)


def _slugify(label: str) -> str:
    """Lowercase ASCII slug used as the immutable Role.key."""
    s = label.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")[:40]


def _role_key_taken(key: str) -> bool:
    return db.session.execute(
        db.select(Role.id).where(Role.key == key)
    ).scalar_one_or_none() is not None
