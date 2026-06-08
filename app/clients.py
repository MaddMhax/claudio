"""CRUD for the customer referential. Clients can be attached to any event.

Gated on ``can_manage_events`` (admin + planificateur), not admin-only, because
planificateurs need to register a new client as soon as a deal lands."""
from functools import wraps

from flask import Blueprint, abort, flash, redirect, render_template, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField
from wtforms.validators import DataRequired, Length, Optional

from .extensions import db
from .models import Client, Project


bp = Blueprint("clients", __name__, url_prefix="/clients")


def manager_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.can_manage_events:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


class ClientForm(FlaskForm):
    name = StringField("Nom", validators=[DataRequired(), Length(min=2, max=120)])
    notes = TextAreaField("Notes", validators=[Optional(), Length(max=1000)])


def _name_taken(name: str, exclude_id: int | None = None) -> bool:
    q = db.select(Client.id).where(Client.name == name)
    if exclude_id is not None:
        q = q.where(Client.id != exclude_id)
    return db.session.execute(q).scalar_one_or_none() is not None


@bp.route("/")
@manager_required
def list_clients():
    clients = db.session.execute(
        db.select(Client).order_by(Client.name)
    ).scalars().all()
    # Per-client project count so the table flags which clients are active.
    counts = {
        c.id: db.session.execute(
            db.select(db.func.count(Project.id)).where(Project.client_id == c.id)
        ).scalar_one()
        for c in clients
    }
    return render_template("clients_list.html", clients=clients, counts=counts)


@bp.route("/new", methods=["GET", "POST"])
@manager_required
def create_client():
    form = ClientForm()
    if form.validate_on_submit():
        name = form.name.data.strip()
        if _name_taken(name):
            form.name.errors.append("Ce nom existe déjà.")
        else:
            c = Client(
                name=name,
                notes=(form.notes.data or "").strip() or None,
            )
            db.session.add(c)
            db.session.commit()
            flash(f"Client « {c.name} » créé.", "success")
            return redirect(url_for("clients.list_clients"))
    return render_template("client_form.html", form=form, mode="new")


@bp.route("/<int:client_id>/edit", methods=["GET", "POST"])
@manager_required
def edit_client(client_id: int):
    c = db.session.get(Client, client_id)
    if c is None:
        abort(404)
    form = ClientForm(obj=c)
    if form.validate_on_submit():
        new_name = form.name.data.strip()
        if new_name != c.name and _name_taken(new_name, exclude_id=c.id):
            form.name.errors.append("Ce nom existe déjà.")
        else:
            c.name = new_name
            c.notes = (form.notes.data or "").strip() or None
            db.session.commit()
            flash(f"Client « {c.name} » mis à jour.", "success")
            return redirect(url_for("clients.list_clients"))
    return render_template("client_form.html", form=form, mode="edit", client=c)


@bp.route("/<int:client_id>/delete", methods=["POST"])
@manager_required
def delete_client(client_id: int):
    c = db.session.get(Client, client_id)
    if c is None:
        abort(404)
    name = c.name
    # Cascade: deleting a client deletes its projects (and through them the
    # missions, meetings and tasks attached to those projects).
    db.session.delete(c)
    db.session.commit()
    flash(
        f"Client « {name} » supprimé "
        "(projets associés et leur contenu supprimés également).",
        "info",
    )
    return redirect(url_for("clients.list_clients"))
