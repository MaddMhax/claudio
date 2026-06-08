"""Admin screen to customise the worked calendar.

Two levers, both per-deployment so the app stays usable by any company:

- Toggle a French public holiday as « travaillé exceptionnellement » — it then
  counts as a normal working day everywhere (planning, dispo, JH maths).
- Add a custom company day off (pont, fermeture…) that isn't a national
  holiday — treated as non-workable.

Both are stored as ``HolidayOverride`` rows and resolved in ``holidays.py``."""
from __future__ import annotations

from datetime import date, datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from .extensions import db
from .holidays import french_holidays
from .models import HolidayOverride
from .users import manage_required


bp = Blueprint("holidays_admin", __name__, url_prefix="/admin/holidays")


def _parse_date(raw: str) -> date | None:
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


@bp.route("/")
@manage_required
def list_overrides():
    try:
        year = int(request.args.get("year", ""))
    except ValueError:
        year = date.today().year
    if not (1970 <= year <= 2999):
        year = date.today().year

    overrides = {
        ov.holiday_date: ov
        for ov in db.session.execute(db.select(HolidayOverride)).scalars().all()
    }

    fh = french_holidays(year)
    rows = [
        {"date": d, "name": fh[d], "worked": bool(overrides.get(d) and overrides[d].worked)}
        for d in sorted(fh)
    ]
    customs = sorted(
        (ov for ov in overrides.values() if not ov.worked),
        key=lambda o: o.holiday_date,
    )
    return render_template(
        "holidays_admin.html",
        year=year, rows=rows, customs=customs, today=date.today(),
    )


@bp.route("/toggle", methods=["POST"])
@manage_required
def toggle_holiday():
    """Flip a French public holiday between chômé and travaillé."""
    d = _parse_date(request.form.get("date", ""))
    if d is None:
        abort(400)
    name = french_holidays(d.year).get(d)
    if name is None:
        # Only genuine national holidays are toggleable here.
        flash("Cette date n'est pas un jour férié national.", "danger")
        return redirect(url_for("holidays_admin.list_overrides", year=d.year))

    existing = db.session.execute(
        db.select(HolidayOverride).where(HolidayOverride.holiday_date == d)
    ).scalar_one_or_none()

    if existing and existing.worked:
        # Currently worked → restore the holiday (drop the override).
        db.session.delete(existing)
        db.session.commit()
        flash(f"« {name} » est de nouveau chômé.", "info")
    else:
        if existing:
            existing.worked = True
            existing.label = name
        else:
            db.session.add(HolidayOverride(
                holiday_date=d, worked=True, label=name,
                created_by_id=current_user.id,
            ))
        db.session.commit()
        flash(f"« {name} » est désormais travaillé (exception).", "success")
    return redirect(url_for("holidays_admin.list_overrides", year=d.year))


@bp.route("/custom", methods=["POST"])
@manage_required
def add_custom():
    """Register a custom non-working day that isn't a national holiday."""
    d = _parse_date(request.form.get("date", ""))
    if d is None:
        flash("Date invalide.", "danger")
        return redirect(url_for("holidays_admin.list_overrides"))
    if d in french_holidays(d.year):
        flash(
            "Cette date est déjà un jour férié national — inutile de l'ajouter.",
            "info",
        )
        return redirect(url_for("holidays_admin.list_overrides", year=d.year))

    existing = db.session.execute(
        db.select(HolidayOverride).where(HolidayOverride.holiday_date == d)
    ).scalar_one_or_none()
    if existing is not None:
        flash("Un réglage existe déjà pour cette date.", "info")
        return redirect(url_for("holidays_admin.list_overrides", year=d.year))

    label = (request.form.get("label") or "").strip()[:120] or None
    db.session.add(HolidayOverride(
        holiday_date=d, worked=False, label=label,
        created_by_id=current_user.id,
    ))
    db.session.commit()
    flash("Jour chômé personnalisé ajouté.", "success")
    return redirect(url_for("holidays_admin.list_overrides", year=d.year))


@bp.route("/<int:override_id>/delete", methods=["POST"])
@manage_required
def delete_override(override_id: int):
    ov = db.session.get(HolidayOverride, override_id)
    if ov is None:
        abort(404)
    year = ov.holiday_date.year
    db.session.delete(ov)
    db.session.commit()
    flash("Réglage supprimé.", "info")
    return redirect(url_for("holidays_admin.list_overrides", year=year))
