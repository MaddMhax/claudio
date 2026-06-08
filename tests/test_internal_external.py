"""Internal vs external (« prestataire ») collaborators and cell colouring.

External providers can be assigned to technical missions but must never tint the
planning's availability (green) / overcharge (red) cells — only internal
specialists do. These are pure tests (SimpleNamespace fakes, no DB): the holiday
helpers degrade to the hardcoded French list outside an app context, and the two
functions under test only read plain attributes.
"""
from datetime import date
from types import SimpleNamespace

from app.planning import _build_month_panel, _internal_auditor_ids


def _member(uid, *, roles, is_external=False):
    return SimpleNamespace(id=uid, roles=list(roles), is_external=is_external)


def _blocking_event(start, end, participants):
    """A blocking mission (counts toward availability/overcharge)."""
    return SimpleNamespace(
        start_date=start, end_date=end, participants=participants,
        meeting_type=SimpleNamespace(blocks_assignments=True), audit_kind=None,
    )


# --- _internal_auditor_ids -------------------------------------------------
def test_internal_auditor_ids_keeps_only_internal_specialists():
    internal = _member(1, roles=["audit_web"])
    external = _member(2, roles=["audit_web", "externe_prestataire"], is_external=True)
    planner = _member(3, roles=["planificateur"])           # system role only
    tag_only = _member(4, roles=["interne"])                # provenance, no specialty

    assert _internal_auditor_ids([internal, external, planner, tag_only]) == {1}


# --- _build_month_panel colouring ------------------------------------------
# Tue 4 Jun 2024 — a plain working day (no French public holiday that week).
DAY = date(2024, 6, 4)


def test_external_double_booking_overcharges_but_stays_off_green():
    ext = _member(2, roles=["audit_web", "externe_prestataire"], is_external=True)
    members = [ext]
    events = [
        _blocking_event(DAY, DAY, [ext]),
        _blocking_event(DAY, DAY, [ext]),  # double-booked external
    ]
    panel = _build_month_panel(
        2024, 6, members, events, _internal_auditor_ids(members),
    )
    # Red DOES surface an overcharged external (informational), but they never
    # create internal availability (green) — there's no internal auditor here.
    assert DAY.day in panel["days_overcharged"]
    assert panel["days_available"] == set()


def test_internal_double_booking_overcharges():
    internal = _member(1, roles=["audit_web"])
    members = [internal]
    events = [
        _blocking_event(DAY, DAY, [internal]),
        _blocking_event(DAY, DAY, [internal]),
    ]
    panel = _build_month_panel(
        2024, 6, members, events, _internal_auditor_ids(members),
    )
    assert DAY.day in panel["days_overcharged"]


def test_external_assignment_does_not_create_availability():
    # An internal auditor exists and is free; an external is busy that day.
    internal = _member(1, roles=["audit_web"])
    ext = _member(2, roles=["audit_web", "externe_prestataire"], is_external=True)
    members = [internal, ext]
    events = [_blocking_event(DAY, DAY, [ext])]  # only the external is blocked
    panel = _build_month_panel(
        2024, 6, members, events, _internal_auditor_ids(members),
    )
    # The internal auditor is free → day is available regardless of the external.
    assert DAY.day in panel["days_available"]


def test_external_works_public_holiday_internal_does_not():
    # 1 May 2024 (Fête du Travail) — a French public holiday.
    holiday = date(2024, 5, 1)
    internal = _member(1, roles=["audit_web"])
    ext = _member(2, roles=["audit_web", "externe_prestataire"], is_external=True)
    members = [internal, ext]
    # One mission spanning the holiday, both assigned.
    events = [_blocking_event(date(2024, 4, 30), date(2024, 5, 2), [internal, ext])]
    panel = _build_month_panel(
        2024, 5, members, events, _internal_auditor_ids(members),
    )
    by_member = panel["by_member"]
    # Holiday: the external's segment shows, the internal's doesn't.
    assert holiday.day in by_member[ext.id]
    assert holiday.day not in by_member[internal.id]
    # The following worked day (2 May) still shows the internal.
    assert 2 in by_member[internal.id]
    # And a holiday is never tinted green/red.
    assert holiday.day not in panel["days_available"]


# --- assignment conflicts never block an external (DB-backed) --------------
def test_assignment_conflict_skips_externals(session, user_factory):
    """An external in an overlapping absence must not block a mission assignment;
    an internal in the same absence still does."""
    from datetime import datetime, time

    from app.models import Event, MeetingType
    from app.planning import _find_assignment_conflicts

    absence = MeetingType(
        name="Congé (test)", blocks_assignments=True,
        is_technical=False, allows_client=False,
    )
    session.add(absence)
    session.flush()

    internal = user_factory(username="int1", roles=("audit_web",))
    external = user_factory(
        username="ext1", roles=("audit_web", "externe_prestataire"),
    )

    ev = Event(
        title="Congé", start_date=DAY, end_date=DAY,
        start_time=time(9, 0), end_time=time(17, 0),
        meeting_type_id=absence.id,
    )
    ev.participants = [internal, external]
    session.add(ev)
    session.commit()

    conflicts = _find_assignment_conflicts(
        datetime.combine(DAY, time(9, 0)),
        datetime.combine(DAY, time(17, 0)),
        {internal.id, external.id},
    )
    blocked_ids = {u.id for u, _ in conflicts}
    assert internal.id in blocked_ids        # internal still blocked
    assert external.id not in blocked_ids     # external exempt


# --- availability search pool excludes externals (DB-backed) ---------------
def test_availability_search_pool_excludes_externals(session, user_factory):
    from app.search import _all_pentesters

    internal = user_factory(username="int2", roles=("audit_web",))
    external = user_factory(
        username="ext2", roles=("audit_web", "externe_prestataire"),
    )
    planner = user_factory(username="pl2", roles=("planificateur",))

    ids = {u.id for u in _all_pentesters()}
    assert internal.id in ids
    assert external.id not in ids   # external left out of availability search
    assert planner.id not in ids    # system-only role isn't a pentester
