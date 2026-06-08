"""Working-day / holiday / person-day arithmetic.

The holiday + working-day helpers are pure (no DB) when there's no app context —
``_overrides()`` degrades to the hardcoded French list — so most of these run
without fixtures. Override behaviour is tested separately with the app fixture.
"""
from datetime import date
from types import SimpleNamespace

from app.holidays import add_working_days, french_holidays, is_french_holiday
from app.projects import _person_days, _workdays


# --- French public holidays (incl. Easter-derived) -------------------------
def test_fixed_holidays_2024():
    h = french_holidays(2024)
    assert h[date(2024, 1, 1)] == "Jour de l'an"
    assert h[date(2024, 7, 14)] == "Fête nationale"
    assert h[date(2024, 12, 25)] == "Noël"


def test_easter_derived_holidays_2024():
    # Easter Sunday 2024 = 31 Mar → Monday 1 Apr, Ascension 9 May, Pentecôte 20 May.
    h = french_holidays(2024)
    assert h[date(2024, 4, 1)] == "Lundi de Pâques"
    assert h[date(2024, 5, 9)] == "Ascension"
    assert h[date(2024, 5, 20)] == "Lundi de Pentecôte"


def test_is_french_holiday_pure():
    assert is_french_holiday(date(2024, 12, 25)) is True
    assert is_french_holiday(date(2024, 12, 24)) is False  # normal Tuesday


# --- add_working_days (start excluded; Mon-Fri minus holidays) -------------
def test_add_working_days_skips_weekend():
    # Fri 7 Jun 2024 + 1 working day → Mon 10 Jun.
    assert add_working_days(date(2024, 6, 7), 1) == date(2024, 6, 10)


def test_add_working_days_skips_holiday():
    # Tue 24 Dec 2024 + 1 → skip Wed 25 (Noël) → Thu 26 Dec.
    assert add_working_days(date(2024, 12, 24), 1) == date(2024, 12, 26)


def test_add_working_days_zero_is_noop():
    assert add_working_days(date(2024, 6, 7), 0) == date(2024, 6, 7)


# --- _workdays (inclusive count) -------------------------------------------
def test_workdays_full_week():
    assert _workdays(date(2024, 6, 3), date(2024, 6, 7)) == 5  # Mon–Fri


def test_workdays_ignores_trailing_weekend():
    assert _workdays(date(2024, 6, 3), date(2024, 6, 9)) == 5  # +Sat/Sun


def test_workdays_excludes_holiday_in_range():
    # Mon 23 → Fri 27 Dec 2024, minus Wed 25 (Noël) = 4.
    assert _workdays(date(2024, 12, 23), date(2024, 12, 27)) == 4


def test_workdays_reversed_range_is_zero():
    assert _workdays(date(2024, 6, 7), date(2024, 6, 3)) == 0


# --- _person_days = workdays × participant count ---------------------------
def test_person_days_multiplies_by_participants():
    event = SimpleNamespace(
        start_date=date(2024, 6, 3),  # Mon
        end_date=date(2024, 6, 7),    # Fri → 5 working days
        participants=[object(), object()],  # 2 people
    )
    assert _person_days(event) == 10


def test_person_days_no_participants_is_zero():
    event = SimpleNamespace(
        start_date=date(2024, 6, 3), end_date=date(2024, 6, 7), participants=[]
    )
    assert _person_days(event) == 0


# --- Admin overrides (need an app/DB context) ------------------------------
def test_worked_override_makes_holiday_workable(app, session):
    from app.models import HolidayOverride
    session.add(HolidayOverride(holiday_date=date(2024, 12, 25), worked=True))
    session.commit()
    # First call in this app-context caches overrides *with* the row present.
    assert is_french_holiday(date(2024, 12, 25)) is False


def test_custom_day_off_override_blocks(app, session):
    from app.models import HolidayOverride
    # A normal Monday flagged as a company day off.
    session.add(HolidayOverride(holiday_date=date(2024, 8, 19), worked=False,
                                label="Pont"))
    session.commit()
    assert is_french_holiday(date(2024, 8, 19)) is True
