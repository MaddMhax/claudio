from flask import Blueprint, abort, flash, redirect, render_template, url_for
from flask_wtf import FlaskForm
from wtforms import SelectField, StringField, TextAreaField
from wtforms.validators import DataRequired, Length, Optional, Regexp

from .extensions import db
from .models import Event, MeetingType, MissionSubtype, Role
from .users import admin_required


bp = Blueprint("mission_types", __name__, url_prefix="/admin/mission-types")


class MissionSubtypeForm(FlaskForm):
    name = StringField("Nom", validators=[DataRequired(), Length(min=1, max=80)])
    color = StringField(
        "Couleur",
        validators=[
            DataRequired(),
            Regexp(r"^#[0-9a-fA-F]{6}$", message="Format attendu : #RRGGBB."),
        ],
        default="#64748b",
    )
    # Optional spécialité — points at a row in the Role table managed via
    # /admin/users. 0 = no spécialité (the subtype doesn't imply one).
    role_id = SelectField("Spécialité", coerce=int, validators=[Optional()])

    def populate_choices(self) -> None:
        roles = db.session.execute(
            db.select(Role).order_by(Role.label)
        ).scalars().all()
        self.role_id.choices = [(0, "— aucune —")] + [(r.id, r.label) for r in roles]


class MissionTypeForm(FlaskForm):
    """Shared form for both categories (technical missions & absences). The
    behavioural flags (blocking / technical / client) are no longer tickable —
    they're forced from the category the type is created under."""
    name = StringField("Nom", validators=[DataRequired(), Length(min=2, max=80)])
    color = StringField(
        "Couleur",
        validators=[
            DataRequired(),
            Regexp(r"^#[0-9a-fA-F]{6}$", message="Format attendu : #RRGGBB."),
        ],
        default="#3b82f6",
    )
    description = TextAreaField("Description", validators=[Optional(), Length(max=500)])


def _event_counts(types) -> dict[int, int]:
    return {
        mt.id: db.session.execute(
            db.select(db.func.count(Event.id)).where(Event.meeting_type_id == mt.id)
        ).scalar_one()
        for mt in types
    }


# ===========================================================================
# Technical mission types — always technical, blocking and client-bound.
# All managed in Administration → « Types de mission ». A subtype (Audit,
# Retest, …) carries the spécialité.
# ===========================================================================
@bp.route("/")
@admin_required
def list_types():
    types = db.session.execute(
        db.select(MeetingType)
        .where(MeetingType.is_technical.is_(True))
        .order_by(MeetingType.name)
    ).scalars().all()
    return render_template(
        "mission_types_list.html",
        category="technical", types=types, counts=_event_counts(types),
    )


@bp.route("/new", methods=["GET", "POST"])
@admin_required
def create_type():
    form = MissionTypeForm()
    if form.validate_on_submit():
        if _name_taken(form.name.data.strip()):
            form.name.errors.append("Ce nom existe déjà.")
        else:
            # Technical missions are always blocking, always client-bound and
            # always carry a spécialité (via their mandatory subtype).
            mt = MeetingType(
                name=form.name.data.strip(),
                color=form.color.data,
                description=(form.description.data or "").strip() or None,
                blocks_assignments=True,
                is_technical=True,
                allows_client=True,
            )
            db.session.add(mt)
            db.session.commit()
            flash(f"Type de mission « {mt.name} » créé.", "success")
            return redirect(url_for("mission_types.list_types"))
    return render_template(
        "mission_type_form.html", form=form, mode="new", category="technical",
    )


@bp.route("/<int:type_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_type(type_id: int):
    mt = db.session.get(MeetingType, type_id)
    if mt is None or not mt.is_technical:
        abort(404)
    form = MissionTypeForm(obj=mt)
    if form.validate_on_submit():
        new_name = form.name.data.strip()
        if new_name != mt.name and _name_taken(new_name):
            form.name.errors.append("Ce nom existe déjà.")
        else:
            mt.name = new_name
            mt.color = form.color.data
            mt.description = (form.description.data or "").strip() or None
            # Flags are immutable for this category — keep them forced on.
            mt.blocks_assignments = True
            mt.is_technical = True
            mt.allows_client = True
            db.session.commit()
            flash(f"Type de mission « {mt.name} » mis à jour.", "success")
            return redirect(url_for("mission_types.list_types"))
    subtype_form = MissionSubtypeForm()
    subtype_form.populate_choices()
    return render_template(
        "mission_type_form.html",
        form=form, mode="edit", category="technical", mission_type=mt,
        subtypes=mt.subtypes, subtype_form=subtype_form,
    )


# ===========================================================================
# Absence types — Congé, Formation, … Always blocking, never client-bound,
# never technical (no subtype, no spécialité). Can be assigned to anyone,
# technical or not. Managed in Administration → « Types d'absence ».
# ===========================================================================
def _is_absence(mt: MeetingType) -> bool:
    return (not mt.is_technical) and mt.blocks_assignments and (not mt.allows_client)


@bp.route("/absences/")
@admin_required
def list_absence_types():
    types = db.session.execute(
        db.select(MeetingType)
        .where(
            MeetingType.is_technical.is_(False),
            MeetingType.blocks_assignments.is_(True),
            MeetingType.allows_client.is_(False),
        )
        .order_by(MeetingType.name)
    ).scalars().all()
    return render_template(
        "mission_types_list.html",
        category="absence", types=types, counts=_event_counts(types),
    )


@bp.route("/absences/new", methods=["GET", "POST"])
@admin_required
def create_absence_type():
    form = MissionTypeForm()
    if form.validate_on_submit():
        if _name_taken(form.name.data.strip()):
            form.name.errors.append("Ce nom existe déjà.")
        else:
            # Absences block assignment, carry no client and no spécialité.
            mt = MeetingType(
                name=form.name.data.strip(),
                color=form.color.data,
                description=(form.description.data or "").strip() or None,
                blocks_assignments=True,
                is_technical=False,
                allows_client=False,
            )
            db.session.add(mt)
            db.session.commit()
            flash(f"Type d'absence « {mt.name} » créé.", "success")
            return redirect(url_for("mission_types.list_absence_types"))
    return render_template(
        "mission_type_form.html", form=form, mode="new", category="absence",
    )


@bp.route("/absences/<int:type_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_absence_type(type_id: int):
    mt = db.session.get(MeetingType, type_id)
    if mt is None or not _is_absence(mt):
        abort(404)
    form = MissionTypeForm(obj=mt)
    if form.validate_on_submit():
        new_name = form.name.data.strip()
        if new_name != mt.name and _name_taken(new_name):
            form.name.errors.append("Ce nom existe déjà.")
        else:
            mt.name = new_name
            mt.color = form.color.data
            mt.description = (form.description.data or "").strip() or None
            mt.blocks_assignments = True
            mt.is_technical = False
            mt.allows_client = False
            db.session.commit()
            flash(f"Type d'absence « {mt.name} » mis à jour.", "success")
            return redirect(url_for("mission_types.list_absence_types"))
    return render_template(
        "mission_type_form.html",
        form=form, mode="edit", category="absence", mission_type=mt,
    )


@bp.route("/absences/<int:type_id>/delete", methods=["POST"])
@admin_required
def delete_absence_type(type_id: int):
    mt = db.session.get(MeetingType, type_id)
    if mt is None or not _is_absence(mt):
        abort(404)
    name = mt.name
    db.session.delete(mt)
    db.session.commit()
    flash(f"Type d'absence « {name} » supprimé.", "info")
    return redirect(url_for("mission_types.list_absence_types"))


@bp.route("/<int:type_id>/subtypes/new", methods=["POST"])
@admin_required
def create_subtype(type_id: int):
    mt = db.session.get(MeetingType, type_id)
    if mt is None:
        abort(404)
    form = MissionSubtypeForm()
    form.populate_choices()
    if not form.validate_on_submit():
        for field, errors in form.errors.items():
            for e in errors:
                flash(f"Sous-type — {field}: {e}", "danger")
        return redirect(url_for("mission_types.edit_type", type_id=mt.id))
    name = form.name.data.strip()
    if _subtype_name_taken(mt.id, name):
        flash(f"Un sous-type « {name} » existe déjà pour ce type.", "danger")
        return redirect(url_for("mission_types.edit_type", type_id=mt.id))
    sub = MissionSubtype(
        name=name,
        color=form.color.data,
        role_id=form.role_id.data or None,
        meeting_type_id=mt.id,
    )
    db.session.add(sub)
    db.session.commit()
    flash(f"Sous-type « {sub.name } » ajouté.", "success")
    return redirect(url_for("mission_types.edit_type", type_id=mt.id))


@bp.route("/<int:type_id>/subtypes/<int:sub_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_subtype(type_id: int, sub_id: int):
    mt = db.session.get(MeetingType, type_id)
    sub = db.session.get(MissionSubtype, sub_id)
    if mt is None or sub is None or sub.meeting_type_id != mt.id:
        abort(404)
    form = MissionSubtypeForm(obj=sub)
    form.populate_choices()
    if form.validate_on_submit():
        new_name = form.name.data.strip()
        if new_name != sub.name and _subtype_name_taken(mt.id, new_name):
            form.name.errors.append("Un sous-type avec ce nom existe déjà pour ce type.")
        else:
            sub.name = new_name
            sub.color = form.color.data
            sub.role_id = form.role_id.data or None
            db.session.commit()
            flash(f"Sous-type « {sub.name} » mis à jour.", "success")
            return redirect(url_for("mission_types.edit_type", type_id=mt.id))
    return render_template(
        "mission_subtype_form.html",
        form=form, mission_type=mt, subtype=sub,
    )


@bp.route("/<int:type_id>/subtypes/<int:sub_id>/delete", methods=["POST"])
@admin_required
def delete_subtype(type_id: int, sub_id: int):
    mt = db.session.get(MeetingType, type_id)
    sub = db.session.get(MissionSubtype, sub_id)
    if mt is None or sub is None or sub.meeting_type_id != mt.id:
        abort(404)
    name = sub.name
    db.session.delete(sub)
    db.session.commit()
    flash(f"Sous-type « {name} » supprimé.", "info")
    return redirect(url_for("mission_types.edit_type", type_id=mt.id))


@bp.route("/<int:type_id>/delete", methods=["POST"])
@admin_required
def delete_type(type_id: int):
    mt = db.session.get(MeetingType, type_id)
    if mt is None or not mt.is_technical:
        abort(404)
    name = mt.name
    # FK on events.meeting_type_id is SET NULL on delete, so events are preserved
    # but lose their type label.
    db.session.delete(mt)
    db.session.commit()
    flash(f"Type de mission « {name} » supprimé.", "info")
    return redirect(url_for("mission_types.list_types"))


def _name_taken(name: str) -> bool:
    return db.session.execute(
        db.select(MeetingType.id).where(MeetingType.name == name)
    ).scalar_one_or_none() is not None


def _subtype_name_taken(meeting_type_id: int, name: str) -> bool:
    return db.session.execute(
        db.select(MissionSubtype.id).where(
            MissionSubtype.meeting_type_id == meeting_type_id,
            MissionSubtype.name == name,
        )
    ).scalar_one_or_none() is not None
