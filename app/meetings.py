from datetime import date as _date, datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    DateField,
    IntegerField,
    SelectField,
    StringField,
    TimeField,
)
from wtforms.validators import DataRequired, Length, NumberRange, Optional, Regexp

from .extensions import db
from .holidays import add_working_days, is_french_holiday  # noqa: F401  (is_french_holiday kept for backwards-compat imports)
from .models import (
    Client,
    Meeting,
    MeetingCategory,
    MeetingDateHistory,
    Project,
    Task,
    TaskStatus,
)
from .users import admin_required


FOLLOWUP_DAYS = 5
TASK_NAME_MAX = 200  # mirrors Task.name column length


bp = Blueprint("meetings", __name__, url_prefix="/meetings")


class MeetingForm(FlaskForm):
    name = StringField("Nom", validators=[DataRequired(), Length(min=2, max=200)])
    date = DateField("Date", validators=[DataRequired()])
    # Optional time window — when both are empty the meeting renders as a
    # plain date label on the calendar (legacy behavior).
    start_time = TimeField("Heure de début", validators=[Optional()])
    end_time = TimeField("Heure de fin", validators=[Optional()])
    project_id = SelectField("Projet", coerce=int, validators=[DataRequired()])
    category_id = SelectField("Type", coerce=int, validators=[Optional()])
    create_followup_task = BooleanField("Créer une tâche de suivi")
    followup_template_id = SelectField(
        "Tâche de suivi",
        coerce=int,
        validators=[Optional()],
    )
    followup_offset_days = IntegerField(
        "Délai (jours ouvrés)",
        default=FOLLOWUP_DAYS,
        validators=[Optional(), NumberRange(min=0, max=365)],
    )

    def populate_choices(self) -> None:
        self.category_id.choices = [(0, "— aucun —")] + [
            (c.id, c.name)
            for c in db.session.execute(
                db.select(MeetingCategory).order_by(MeetingCategory.name)
            ).scalars()
        ]
        projects = db.session.execute(
            db.select(Project)
            .join(Client, Project.client_id == Client.id)
            .order_by(Client.name, Project.name)
        ).scalars().all()
        self.project_id.choices = [(0, "— choisir un projet —")] + [
            (p.id, p.name) for p in projects
        ]
        self.followup_template_id.choices = [(0, "— choisir un modèle —")] + [
            (t.id, t.name)
            for t in db.session.execute(
                db.select(Task)
                .where(Task.is_template.is_(True))
                .order_by(Task.name)
            ).scalars()
        ]

    def validate(self, extra_validators=None) -> bool:  # type: ignore[override]
        ok = super().validate(extra_validators=extra_validators)
        if not ok:
            return False
        if self.create_followup_task.data and not self.followup_template_id.data:
            self.followup_template_id.errors.append(
                "Choisissez un modèle de tâche à instancier."
            )
            ok = False
        # Times come as a pair: either both set or both empty. A start without
        # an end (or vice versa) is rejected so the calendar render stays
        # consistent (always shows HH:MM-HH:MM when any time is present).
        s, e = self.start_time.data, self.end_time.data
        if (s is None) != (e is None):
            self.end_time.errors.append(
                "Renseignez les deux heures ou aucune."
            )
            ok = False
        elif s is not None and e is not None and e <= s:
            self.end_time.errors.append("L'heure de fin doit être après le début.")
            ok = False
        return ok


class MeetingCategoryForm(FlaskForm):
    name = StringField("Nom", validators=[DataRequired(), Length(min=2, max=80)])
    color = StringField(
        "Couleur",
        validators=[
            DataRequired(),
            Regexp(r"^#[0-9a-fA-F]{6}$", message="Format attendu : #RRGGBB."),
        ],
        default="#a855f7",
    )


class FollowupTemplateForm(FlaskForm):
    """Inline create form for a Task template surfaced on the meetings page.

    Only the name is exposed here — descriptions, auto-after-mission, offset
    days etc. are tuned via the full /admin/tasks/<id>/edit page reached via
    the panel's 'Modifier' button."""
    name = StringField("Nom du modèle", validators=[DataRequired(), Length(min=2, max=TASK_NAME_MAX)])


def _require_manage() -> None:
    if not current_user.can_manage_events:
        abort(403)


@bp.route("/", methods=["GET"])
@login_required
def list_meetings():
    _require_manage()
    form = MeetingForm()
    form.populate_choices()
    category_form = MeetingCategoryForm(prefix="category")

    raw_date = request.args.get("date", "")
    try:
        form.date.data = datetime.strptime(raw_date, "%Y-%m-%d").date()
    except ValueError:
        form.date.data = _date.today()

    # Pre-select project when coming from project detail (?project_id=...).
    try:
        form.project_id.data = int(request.args.get("project_id", "")) or 0
    except ValueError:
        pass

    meetings = db.session.execute(
        db.select(Meeting).order_by(Meeting.date.desc(), Meeting.name)
    ).scalars().all()
    categories = db.session.execute(
        db.select(MeetingCategory).order_by(MeetingCategory.name)
    ).scalars().all()
    followup_templates = db.session.execute(
        db.select(Task).where(Task.is_template.is_(True)).order_by(Task.name)
    ).scalars().all()
    return render_template(
        "meetings_list.html",
        meetings=meetings,
        categories=categories,
        form=form,
        category_form=category_form,
        followup_days=FOLLOWUP_DAYS,
        followup_templates=followup_templates,
        followup_form=FollowupTemplateForm(prefix="ftpl"),
    )


@bp.route("/new", methods=["POST"])
@login_required
def create_meeting():
    _require_manage()
    form = MeetingForm()
    form.populate_choices()
    if not form.validate_on_submit():
        flash("Impossible de créer la réunion : vérifiez les champs.", "danger")
        return redirect(url_for("meetings.list_meetings"))

    m = Meeting(
        name=form.name.data.strip(),
        date=form.date.data,
        start_time=form.start_time.data,
        end_time=form.end_time.data,
        category_id=form.category_id.data or None,
        project_id=form.project_id.data,
        created_by_id=current_user.id,
    )
    db.session.add(m)

    # Offset comes from the form (per-meeting setting) — falls back to the
    # FOLLOWUP_DAYS default when the user didn't type anything.
    offset_days = form.followup_offset_days.data
    if offset_days is None or offset_days < 0:
        offset_days = FOLLOWUP_DAYS
    followup_date = add_working_days(form.date.data, offset_days)
    default_status = db.session.execute(
        db.select(TaskStatus).where(TaskStatus.name == "À faire")
    ).scalar_one_or_none()
    default_status_id = default_status.id if default_status else None

    extra_flash = None

    if form.create_followup_task.data and form.followup_template_id.data:
        tpl = db.session.get(Task, form.followup_template_id.data)
        if tpl is not None and tpl.is_template:
            spawned_name = f"{tpl.name} — {m.name}"[:TASK_NAME_MAX]
            # Spawned follow-up tasks inherit the meeting's project, so they
            # surface on the project detail page alongside the meeting itself.
            db.session.add(Task(
                name=spawned_name,
                description=tpl.description,
                due_date=followup_date,
                status_id=default_status_id,
                is_template=False,
                project_id=m.project_id,
                created_by_id=current_user.id,
            ))
            extra_flash = (
                f"Tâche de suivi « {spawned_name} » créée pour le "
                f"{followup_date.strftime('%d/%m/%Y')} "
                f"(+{offset_days} j ouvrés)."
            )

    db.session.commit()
    flash(f"Réunion « {m.name} » créée.", "success")
    if extra_flash:
        flash(extra_flash, "info")
    return redirect(url_for("planning.calendar_default"))


@bp.route("/<int:meeting_id>/edit", methods=["GET", "POST"])
@login_required
def edit_meeting(meeting_id: int):
    """Edit a meeting. A change to ``date`` / ``start_time`` / ``end_time``
    snapshots the previous values into ``meeting_date_history`` so the
    'Précédemment planifié le …' trail stays intact across reschedules."""
    _require_manage()
    m = db.session.get(Meeting, meeting_id)
    if m is None:
        abort(404)

    form = MeetingForm(obj=m)
    form.populate_choices()
    if request.method == "GET":
        form.project_id.data = m.project_id
        form.category_id.data = m.category_id or 0
        # Strip the follow-up toggle on edit — it only makes sense at creation.
        form.create_followup_task.data = False
        form.followup_template_id.data = 0

    if form.validate_on_submit():
        prev_date = m.date
        prev_start = m.start_time
        prev_end = m.end_time
        new_date = form.date.data
        new_start = form.start_time.data
        new_end = form.end_time.data
        moved = (
            new_date != prev_date
            or new_start != prev_start
            or new_end != prev_end
        )
        if moved:
            db.session.add(MeetingDateHistory(
                meeting_id=m.id,
                previous_date=prev_date,
                previous_start_time=prev_start,
                previous_end_time=prev_end,
                changed_by_id=current_user.id,
            ))
        m.name = form.name.data.strip()
        m.date = new_date
        m.start_time = new_start
        m.end_time = new_end
        m.project_id = form.project_id.data
        m.category_id = form.category_id.data or None
        db.session.commit()
        flash(f"Réunion « {m.name} » mise à jour.", "success")
        return redirect(url_for("meetings.list_meetings"))

    return render_template(
        "meeting_form.html",
        form=form,
        meeting=m,
        followup_days=FOLLOWUP_DAYS,
    )


@bp.route("/<int:meeting_id>/delete", methods=["POST"])
@login_required
def delete_meeting(meeting_id: int):
    _require_manage()
    m = db.session.get(Meeting, meeting_id)
    if m is None:
        abort(404)
    name = m.name
    db.session.delete(m)
    db.session.commit()
    flash(f"Réunion « {name} » supprimée.", "info")
    return redirect(url_for("meetings.list_meetings"))


# ===== Meeting categories (admin-only management) =====

@bp.route("/categories/new", methods=["POST"])
@admin_required
def create_category():
    form = MeetingCategoryForm(prefix="category")
    if not form.validate_on_submit():
        flash("Impossible de créer le type : vérifiez les champs.", "danger")
        return redirect(url_for("meetings.list_meetings"))
    name = form.name.data.strip()
    if _category_name_taken(name):
        flash(f"Le type « {name} » existe déjà.", "danger")
        return redirect(url_for("meetings.list_meetings"))
    c = MeetingCategory(name=name, color=form.color.data)
    db.session.add(c)
    db.session.commit()
    flash(f"Type « {c.name} » créé.", "success")
    return redirect(url_for("meetings.list_meetings"))


@bp.route("/categories/<int:category_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_category(category_id: int):
    c = db.session.get(MeetingCategory, category_id)
    if c is None:
        abort(404)
    form = MeetingCategoryForm(obj=c, prefix="category")

    if form.validate_on_submit():
        new_name = form.name.data.strip()
        if new_name != c.name and _category_name_taken(new_name):
            form.name.errors.append("Ce nom existe déjà.")
        else:
            c.name = new_name
            c.color = form.color.data
            db.session.commit()
            flash(f"Type « {c.name} » mis à jour.", "success")
            return redirect(url_for("meetings.list_meetings"))

    return render_template("meeting_category_form.html", form=form, category=c)


@bp.route("/categories/<int:category_id>/delete", methods=["POST"])
@admin_required
def delete_category(category_id: int):
    c = db.session.get(MeetingCategory, category_id)
    if c is None:
        abort(404)
    name = c.name
    # FK on meetings.category_id is SET NULL on delete — meetings survive
    # but lose their type label.
    db.session.delete(c)
    db.session.commit()
    flash(
        f"Type « {name} » supprimé. Les réunions associées sont sans type.",
        "info",
    )
    return redirect(url_for("meetings.list_meetings"))


def _category_name_taken(name: str) -> bool:
    return db.session.execute(
        db.select(MeetingCategory.id).where(MeetingCategory.name == name)
    ).scalar_one_or_none() is not None


# ===== Follow-up task templates (inline create — edit/delete via tasks blueprint) =====

@bp.route("/followup-templates/new", methods=["POST"])
@admin_required
def create_followup_template():
    """Inline-create a Task template from the meetings page. Fine-tuning
    (description, auto-after-mission, offset days) is done via the
    tasks.edit_task page reached from the 'Modifier' button."""
    form = FollowupTemplateForm(prefix="ftpl")
    if not form.validate_on_submit():
        flash("Impossible de créer le modèle : nom invalide.", "danger")
        return redirect(url_for("meetings.list_meetings"))
    name = form.name.data.strip()
    exists = db.session.execute(
        db.select(Task.id).where(Task.is_template.is_(True), Task.name == name)
    ).scalar_one_or_none()
    if exists:
        flash(f"Un modèle « {name} » existe déjà.", "danger")
        return redirect(url_for("meetings.list_meetings"))
    db.session.add(Task(
        name=name,
        is_template=True,
        created_by_id=current_user.id,
    ))
    db.session.commit()
    flash(f"Modèle « {name} » créé.", "success")
    return redirect(url_for("meetings.list_meetings"))
