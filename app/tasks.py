from datetime import datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    DateField,
    IntegerField,
    SelectField,
    StringField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Length, NumberRange, Optional, Regexp

from .extensions import db
from .models import Client, Project, Task, TaskStatus
from .users import manage_required


bp = Blueprint("tasks", __name__, url_prefix="/admin/tasks")


class TaskForm(FlaskForm):
    name = StringField("Nom", validators=[DataRequired(), Length(min=2, max=200)])
    description = TextAreaField(
        "Description", validators=[Optional(), Length(max=4000)]
    )
    due_date = DateField("Date", validators=[Optional()])
    project_id = SelectField("Projet", coerce=int, validators=[Optional()])
    status_id = SelectField("Statut", coerce=int, validators=[Optional()])
    is_template = BooleanField(
        "Modèle — créé automatiquement après chaque réunion (générique)"
    )
    auto_after_mission = BooleanField(
        "Créer automatiquement après chaque mission technique"
    )
    auto_offset_days = IntegerField(
        "Délai (jours ouvrés) après la fin de la mission",
        default=5,
        validators=[Optional(), NumberRange(min=0, max=365)],
    )

    def populate_choices(self) -> None:
        self.status_id.choices = [(0, "— aucun —")] + [
            (s.id, s.name)
            for s in db.session.execute(
                db.select(TaskStatus).order_by(TaskStatus.name)
            ).scalars()
        ]
        projects = db.session.execute(
            db.select(Project)
            .join(Client, Project.client_id == Client.id)
            .order_by(Client.name, Project.name)
        ).scalars().all()
        self.project_id.choices = [(0, "— choisir un projet —")] + [
            (p.id, f"{p.client.name} — {p.name}") for p in projects
        ]

    def validate(self, extra_validators=None) -> bool:  # type: ignore[override]
        ok = super().validate(extra_validators=extra_validators)
        if not ok:
            return False
        # Templates are reusable patterns — they sit outside any project.
        # Every other task must belong to a project.
        if not self.is_template.data and not self.project_id.data:
            self.project_id.errors.append(
                "Un projet est obligatoire (sauf pour un modèle de tâche)."
            )
            ok = False
        if self.is_template.data:
            self.project_id.data = 0
        else:
            # Auto-spawn is a template-only behavior; silently drop the flag on
            # non-template tasks instead of erroring out.
            self.auto_after_mission.data = False
        if self.auto_offset_days.data is None:
            self.auto_offset_days.data = 5
        return ok


class TaskStatusForm(FlaskForm):
    name = StringField("Nom", validators=[DataRequired(), Length(min=2, max=80)])
    color = StringField(
        "Couleur",
        validators=[
            DataRequired(),
            Regexp(r"^#[0-9a-fA-F]{6}$", message="Format attendu : #RRGGBB."),
        ],
        default="#64748b",
    )
    emoji = StringField(
        "Emoji",
        validators=[Optional(), Length(max=16)],
    )


@bp.route("/", methods=["GET"])
@manage_required
def list_tasks():
    task_form = TaskForm(prefix="task")
    task_form.populate_choices()
    # Pre-fill the date when arriving from the calendar's "+T" button.
    raw_date = request.args.get("date", "")
    try:
        task_form.due_date.data = datetime.strptime(raw_date, "%Y-%m-%d").date()
    except ValueError:
        pass
    # Pre-select a project when arriving from the project detail page.
    try:
        task_form.project_id.data = int(request.args.get("project_id", "")) or 0
    except ValueError:
        pass
    status_form = TaskStatusForm(prefix="status")

    tasks = db.session.execute(
        db.select(Task).order_by(Task.created_at.desc())
    ).scalars().all()
    statuses = db.session.execute(
        db.select(TaskStatus).order_by(TaskStatus.name)
    ).scalars().all()
    return render_template(
        "tasks_list.html",
        tasks=tasks,
        statuses=statuses,
        task_form=task_form,
        status_form=status_form,
    )


@bp.route("/new", methods=["POST"])
@manage_required
def create_task():
    form = TaskForm(prefix="task")
    form.populate_choices()
    if form.validate_on_submit():
        t = Task(
            name=form.name.data.strip(),
            description=(form.description.data or "").strip() or None,
            due_date=form.due_date.data,
            status_id=form.status_id.data or None,
            is_template=bool(form.is_template.data),
            auto_after_mission=bool(form.auto_after_mission.data),
            auto_offset_days=int(form.auto_offset_days.data or 5),
            project_id=form.project_id.data or None,
            created_by_id=current_user.id,
        )
        db.session.add(t)
        db.session.commit()
        flash(f"Tâche « {t.name} » créée.", "success")
    else:
        flash("Impossible de créer la tâche : vérifiez les champs.", "danger")
    return redirect(url_for("tasks.list_tasks"))


@bp.route("/<int:task_id>/edit", methods=["GET", "POST"])
@manage_required
def edit_task(task_id: int):
    t = db.session.get(Task, task_id)
    if t is None:
        abort(404)
    form = TaskForm(obj=t, prefix="task")
    form.populate_choices()
    if request.method == "GET":
        form.status_id.data = t.status_id or 0
        form.project_id.data = t.project_id or 0

    if form.validate_on_submit():
        t.name = form.name.data.strip()
        t.description = (form.description.data or "").strip() or None
        t.due_date = form.due_date.data
        t.status_id = form.status_id.data or None
        t.is_template = bool(form.is_template.data)
        t.auto_after_mission = bool(form.auto_after_mission.data)
        t.auto_offset_days = int(form.auto_offset_days.data or 5)
        t.project_id = form.project_id.data or None
        db.session.commit()
        flash(f"Tâche « {t.name} » mise à jour.", "success")
        return redirect(url_for("planning.calendar_default"))

    return render_template("task_form.html", form=form, task=t)


@bp.route("/<int:task_id>/delete", methods=["POST"])
@manage_required
def delete_task(task_id: int):
    t = db.session.get(Task, task_id)
    if t is None:
        abort(404)
    name = t.name
    db.session.delete(t)
    db.session.commit()
    flash(f"Tâche « {name} » supprimée.", "info")
    return redirect(url_for("tasks.list_tasks"))


@bp.route("/statuses/new", methods=["POST"])
@manage_required
def create_status():
    form = TaskStatusForm(prefix="status")
    if not form.validate_on_submit():
        flash("Impossible de créer le statut : vérifiez les champs.", "danger")
        return redirect(url_for("tasks.list_tasks"))

    name = form.name.data.strip()
    if _status_name_taken(name):
        flash(f"Le statut « {name} » existe déjà.", "danger")
        return redirect(url_for("tasks.list_tasks"))

    s = TaskStatus(
        name=name,
        color=form.color.data,
        emoji=(form.emoji.data or "").strip() or None,
    )
    db.session.add(s)
    db.session.commit()
    flash(f"Statut « {s.name} » créé.", "success")
    return redirect(url_for("tasks.list_tasks"))


@bp.route("/statuses/<int:status_id>/edit", methods=["GET", "POST"])
@manage_required
def edit_status(status_id: int):
    s = db.session.get(TaskStatus, status_id)
    if s is None:
        abort(404)
    form = TaskStatusForm(obj=s, prefix="status")

    if form.validate_on_submit():
        new_name = form.name.data.strip()
        if new_name != s.name and _status_name_taken(new_name):
            form.name.errors.append("Ce nom existe déjà.")
        else:
            s.name = new_name
            s.color = form.color.data
            s.emoji = (form.emoji.data or "").strip() or None
            db.session.commit()
            flash(f"Statut « {s.name} » mis à jour.", "success")
            return redirect(url_for("tasks.list_tasks"))

    return render_template("task_status_form.html", form=form, status=s)


@bp.route("/statuses/<int:status_id>/delete", methods=["POST"])
@manage_required
def delete_status(status_id: int):
    s = db.session.get(TaskStatus, status_id)
    if s is None:
        abort(404)
    name = s.name
    # FK on tasks.status_id is SET NULL on delete — tasks survive but lose
    # their status label.
    db.session.delete(s)
    db.session.commit()
    flash(
        f"Statut « {name} » supprimé. Les tâches associées sont désormais sans statut.",
        "info",
    )
    return redirect(url_for("tasks.list_tasks"))


def _status_name_taken(name: str) -> bool:
    return db.session.execute(
        db.select(TaskStatus.id).where(TaskStatus.name == name)
    ).scalar_one_or_none() is not None
