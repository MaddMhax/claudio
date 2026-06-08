"""French metropolitan public holidays.

Combines fixed-date holidays with the three Easter-derived ones (Lundi de
Pâques, Ascension, Lundi de Pentecôte). Used to mark non-workable days in
the planning and to exclude them from person-day calculations."""
from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache


# (day, month, label) — these never move.
_FIXED: tuple[tuple[int, int, str], ...] = (
    (1, 1,  "Jour de l'an"),
    (1, 5,  "Fête du Travail"),
    (8, 5,  "Victoire 1945"),
    (14, 7, "Fête nationale"),
    (15, 8, "Assomption"),
    (1, 11, "Toussaint"),
    (11, 11, "Armistice 1918"),
    (25, 12, "Noël"),
)


def _easter_sunday(year: int) -> date:
    """Anonymous Gregorian (Meeus / Jones / Butcher) algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    L = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * L) // 451
    month, day_minus_one = divmod(h + L - 7 * m + 114, 31)
    return date(year, month, day_minus_one + 1)


@lru_cache(maxsize=128)
def french_holidays(year: int) -> dict[date, str]:
    """All metropolitan French public holidays for ``year`` → name."""
    easter = _easter_sunday(year)
    out: dict[date, str] = {date(year, mo, d): name for (d, mo, name) in _FIXED}
    out[easter + timedelta(days=1)]  = "Lundi de Pâques"
    out[easter + timedelta(days=39)] = "Ascension"
    out[easter + timedelta(days=50)] = "Lundi de Pentecôte"
    return out


def _overrides() -> tuple[set[date], dict[date, str]]:
    """Admin ``HolidayOverride`` rows split into (worked_dates, custom_off→label).

    ``worked_dates``  — public holidays the company exceptionally works.
    custom-off map     — extra non-working days that aren't national holidays.

    Cached per request via ``flask.g`` so the tight loops in ``add_working_days``
    and the availability scan don't re-query. Degrades to empty when there's no
    app context or the table doesn't exist yet (fresh boot, before create_all),
    so the pure French list keeps working."""
    try:
        from flask import g, has_app_context
    except Exception:
        return set(), {}

    if has_app_context() and hasattr(g, "_holiday_overrides"):
        return g._holiday_overrides

    worked: set[date] = set()
    custom_off: dict[date, str] = {}
    try:
        from .extensions import db
        from .models import HolidayOverride
        rows = db.session.execute(db.select(HolidayOverride)).scalars().all()
        for r in rows:
            if r.worked:
                worked.add(r.holiday_date)
            else:
                custom_off[r.holiday_date] = r.label or "Jour chômé"
    except Exception:
        worked, custom_off = set(), {}

    result = (worked, custom_off)
    if has_app_context():
        g._holiday_overrides = result
    return result


def is_french_holiday(d: date) -> bool:
    """True when ``d`` is non-workable: a French public holiday or a custom
    company day off, minus any holiday flagged 'exceptionally worked'."""
    worked, custom_off = _overrides()
    if d in worked:
        return False
    if d in custom_off:
        return True
    return d in french_holidays(d.year)


def french_holiday_name(d: date) -> str | None:
    worked, custom_off = _overrides()
    if d in worked:
        return None
    if d in custom_off:
        return custom_off[d]
    return french_holidays(d.year).get(d)


def effective_holidays(year: int) -> dict[date, str]:
    """French public holidays for ``year`` with admin overrides applied:
    worked holidays removed, custom days off added.

    Use this for display / availability. ``french_holidays`` stays the canonical
    hardcoded national list (e.g. for the admin toggle screen)."""
    worked, custom_off = _overrides()
    out = {d: n for d, n in french_holidays(year).items() if d not in worked}
    for d, name in custom_off.items():
        if d.year == year:
            out[d] = name
    return out


def french_holidays_in(start: date, end: date) -> dict[date, str]:
    """Effective holidays within [start, end] inclusive (overrides applied)."""
    out: dict[date, str] = {}
    for y in range(start.year, end.year + 1):
        for d, name in effective_holidays(y).items():
            if start <= d <= end:
                out[d] = name
    return out


def add_working_days(start: date, days: int) -> date:
    """Return ``start`` shifted forward by ``days`` working days.

    Working days = Mon–Fri minus French public holidays. ``start`` itself
    is not counted; we walk day-by-day from ``start + 1``."""
    if days <= 0:
        return start
    d = start
    counted = 0
    while counted < days:
        d += timedelta(days=1)
        if d.weekday() < 5 and not is_french_holiday(d):
            counted += 1
    return d
