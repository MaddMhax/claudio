"""Bootstrap the database: create tables, migrate legacy schemas, seed users.

Run inside the container: ``python -m app.init_db``
"""
from __future__ import annotations

import os
import sys

from sqlalchemy import inspect, text, update

from . import create_app
from .extensions import db
from .models import (
    Event,
    MeetingCategory,
    MeetingType,
    MissionSubtype,
    ROLE_ADMIN,
    ROLE_PLANIFICATEUR,
    Role,
    TaskStatus,
    User,
)


# Default spécialité for the "Audit" subtype auto-seeded under each technical
# mission type. Keyed by the parent type's name so existing installs get a
# sensible default after the role refactor wiped audit_kind. Admins can rewire
# any of these from /admin/mission-types/<id>/edit.
DEFAULT_SUBTYPE_ROLE_BY_TYPE_NAME = {
    "Audit de code": "audit_code",
    "MEEXT":         "audit_web",
}


# Default seeded roles (admin-managed via /admin/users). Keys are stable slugs
# referenced by UserRole.role and MissionSubtype.role / Event.role. New
# installations can rename or delete these from the UI.
DEFAULT_ROLES = [
    {"key": "audit_web",    "label": "Auditeur web",      "color": "#3b82f6"},
    {"key": "audit_mobile", "label": "Auditeur mobile",   "color": "#22c55e"},
    {"key": "audit_code",   "label": "Auditeur de code",  "color": "#f59e0b"},
    # Provenance roles (internal staff vs external provider). Stable keys —
    # ROLE_INTERNE / ROLE_EXTERNE in models — so the planning can spot externals
    # and keep them out of the availability/overcharge cell colouring.
    {"key": "interne",              "label": "Interne",               "color": "#6366f1"},
    {"key": "externe_prestataire",  "label": "Externe - prestataire", "color": "#f97316"},
]


# Default accounts. Change the passwords after first login.
# Each user has a *list* of roles (a user can hold several).
DEFAULT_USERS = [
    {
        "username": "admin",
        "full_name": "Administrateur",
        "color": "#a855f7",
        "password": "Admin!Planning2026",
        "roles": [ROLE_ADMIN],
    },
    {
        "username": "bob",
        "full_name": "Bob Durand",
        "color": "#3b82f6",
        "password": "Bob!Planning2026",
        "roles": ["audit_web"],
    },
    {
        "username": "carol",
        "full_name": "Carole Lefevre",
        "color": "#22c55e",
        "password": "Carol!Planning2026",
        "roles": ["audit_mobile"],
    },
    {
        "username": "david",
        "full_name": "David Garcia",
        "color": "#f59e0b",
        "password": "David!Planning2026",
        "roles": ["audit_code"],
    },
    {
        "username": "eve",
        "full_name": "Eve Moreau",
        "color": "#ec4899",
        "password": "Eve!Planning2026",
        "roles": [ROLE_PLANIFICATEUR],
    },
]

# Minimal seed — admins can add more from the UI (/admin/mission-types).
# Seeded once on a fresh install. Admins can rename / delete / add more.
DEFAULT_TASK_STATUSES = [
    {"name": "À faire",  "color": "#64748b", "emoji": "📝"},
    {"name": "En cours", "color": "#3aa5c9", "emoji": "⚙️"},
    {"name": "Terminé",  "color": "#1fa878", "emoji": "✅"},
]

# Standalone meeting categories — admins can rework them in the UI.
DEFAULT_MEETING_CATEGORIES = [
    {"name": "Synchro équipe", "color": "#a855f7"},
    {"name": "Atelier",         "color": "#3aa5c9"},
    {"name": "Point client",    "color": "#e0a23a"},
]

# Two categories only:
#   - Technical missions: always blocking, client-bound, spécialité-bearing.
#   - Absences (Congé, Formation): always blocking, no client, no spécialité.
# Réunion de cadrage / Restitution are no longer event types — réunions live in
# the Meetings module.
DEFAULT_MEETING_TYPES = [
    {"name": "Audit de code", "color": "#f59e0b", "description": "Audit de code source",                                    "blocks_assignments": True, "is_technical": True,  "allows_client": True},
    {"name": "MEEXT",         "color": "#ef4444", "description": "Mission d'évaluation externe (test d'intrusion externe)", "blocks_assignments": True, "is_technical": True,  "allows_client": True},
    {"name": "Revue SSI",     "color": "#3b82f6", "description": "Revue de la sécurité du système d'information",           "blocks_assignments": True, "is_technical": True,  "allows_client": True},
    {"name": "Formation",     "color": "#84cc16", "description": "Formation / montée en compétence",                        "blocks_assignments": True, "is_technical": False, "allows_client": False},
    {"name": "Congé",         "color": "#64748b", "description": "Congé / absence",                                          "blocks_assignments": True, "is_technical": False, "allows_client": False},
]


def _wipe_legacy_for_project_refactor(table_names: set[str]) -> bool:
    """One-shot pre-create_all() hook for the Project hierarchy refactor.

    Fires whenever the ``projects`` table is absent — that's our marker for
    "schema predates the refactor". Drops any leftover event/meeting/task
    tables (some or all may already be gone from a previous half-finished
    boot) so ``db.create_all()`` can rebuild them clean with ``project_id``.
    Users, clients, mission types, task statuses and meeting categories are
    preserved.

    Returns True to tell the caller to short-circuit the rest of the
    legacy column-by-column migration block, which targets the now-gone /
    soon-to-be-fresh tables and would otherwise crash."""
    if "projects" in table_names:
        return False

    # Order matters: child / dependent tables before parents.
    drop_order = [
        "event_participants",
        "event_date_history",
        "events",
        "meetings",
        "tasks",
    ]
    dropped: list[str] = []
    with db.engine.begin() as conn:
        for t in drop_order:
            if t in table_names:
                conn.execute(text(f"DROP TABLE IF EXISTS {t} CASCADE"))
                dropped.append(t)
    if dropped:
        print(
            "[init_db] refactor Projet : tables vidées "
            f"({', '.join(dropped)}) — reconstruction avec project_id.",
            flush=True,
        )
    else:
        print(
            "[init_db] refactor Projet : aucune table legacy à vider, "
            "schéma reconstruit avec project_id.",
            flush=True,
        )
    return True


def _migrate_schema() -> None:
    """Apply any idempotent schema changes for databases created by older versions."""
    inspector = inspect(db.engine)
    table_names = set(inspector.get_table_names())
    if "users" not in table_names:
        return  # Fresh DB — create_all() will build everything from scratch.

    if _wipe_legacy_for_project_refactor(table_names):
        # We just dropped events/meetings/tasks (+ their join tables).
        # Everything will be rebuilt with the current schema by
        # db.create_all() once we return. The rest of this function is
        # column-by-column legacy migration of those very tables, so it
        # has nothing left to do and would in fact crash trying to
        # recreate event_date_history with a FK to the just-dropped
        # events table. Short-circuit out.
        return

    user_cols = {c["name"] for c in inspector.get_columns("users")}
    event_cols = (
        {c["name"] for c in inspector.get_columns("events")}
        if "events" in table_names else set()
    )

    with db.engine.begin() as conn:
        # SSO (OIDC): add the federated-subject link and relax the password
        # NOT NULL so SSO-only accounts (no local password) are valid. Both
        # statements are idempotent — safe to run on every boot.
        if "oidc_sub" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN oidc_sub VARCHAR(255)"))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_oidc_sub "
                "ON users (oidc_sub)"
            ))
        conn.execute(text(
            "ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL"
        ))

        # Security hardening columns (brute-force lockout, forced password
        # change, session invalidation token, iCal feed token). All idempotent.
        if "failed_login_count" not in user_cols:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN failed_login_count "
                "INTEGER NOT NULL DEFAULT 0"
            ))
        if "locked_until" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN locked_until TIMESTAMPTZ"))
        if "must_change_password" not in user_cols:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN must_change_password "
                "BOOLEAN NOT NULL DEFAULT FALSE"
            ))
        if "session_token" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN session_token VARCHAR(64)"))
        if "ical_token" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN ical_token VARCHAR(64)"))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_ical_token "
                "ON users (ical_token)"
            ))

        # Add start_time / end_time to legacy events tables.
        if "events" in table_names and "start_time" not in event_cols:
            conn.execute(text(
                "ALTER TABLE events ADD COLUMN start_time TIME NOT NULL DEFAULT '09:00'"
            ))
        if "events" in table_names and "end_time" not in event_cols:
            conn.execute(text(
                "ALTER TABLE events ADD COLUMN end_time TIME NOT NULL DEFAULT '17:00'"
            ))
        if "events" in table_names and "status" not in event_cols:
            conn.execute(text(
                "ALTER TABLE events ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'preplanifie'"
            ))
        if "events" in table_names and "audit_kind" not in event_cols:
            conn.execute(text(
                "ALTER TABLE events ADD COLUMN audit_kind VARCHAR(20)"
            ))
        if "events" in table_names and "fpr_received" not in event_cols:
            conn.execute(text(
                "ALTER TABLE events ADD COLUMN fpr_received BOOLEAN NOT NULL DEFAULT FALSE"
            ))
        if "events" in table_names and "fpr_url" not in event_cols:
            conn.execute(text(
                "ALTER TABLE events ADD COLUMN fpr_url VARCHAR(500)"
            ))
        if "events" in table_names and "difficulties" not in event_cols:
            conn.execute(text(
                "ALTER TABLE events ADD COLUMN difficulties TEXT"
            ))
        # Last-modification audit columns (who edited the event last, and when).
        if "events" in table_names and "updated_by_id" not in event_cols:
            conn.execute(text(
                "ALTER TABLE events ADD COLUMN updated_by_id INTEGER "
                "REFERENCES users(id) ON DELETE SET NULL"
            ))
        if "events" in table_names and "updated_at" not in event_cols:
            conn.execute(text(
                "ALTER TABLE events ADD COLUMN updated_at TIMESTAMPTZ"
            ))
        # "Lieu" field has been retired — drop the column on older databases.
        if "events" in table_names and "location" in event_cols:
            conn.execute(text("ALTER TABLE events DROP COLUMN location"))
        # Date history: idempotently create the per-event date trail table.
        if "event_date_history" not in table_names:
            conn.execute(text("""
                CREATE TABLE event_date_history (
                    id                   SERIAL PRIMARY KEY,
                    event_id             INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    previous_start_date  DATE NOT NULL,
                    previous_end_date    DATE NOT NULL,
                    previous_start_time  TIME,
                    previous_end_time    TIME,
                    previous_status      VARCHAR(20),
                    changed_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    changed_by_id        INTEGER REFERENCES users(id) ON DELETE SET NULL
                )
            """))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_event_date_history_event_id "
                "ON event_date_history (event_id)"
            ))
        # Clients: idempotently create the referential and the FK column on events.
        if "clients" not in table_names:
            conn.execute(text("""
                CREATE TABLE clients (
                    id         SERIAL PRIMARY KEY,
                    name       VARCHAR(120) UNIQUE NOT NULL,
                    notes      TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
        # NOTE: events.client_id was the legacy direct-client link, retired
        # by the Project refactor. The wipe block above drops the events
        # table for legacy installs, so we no longer add this column.
        if "meeting_types" in table_names:
            mt_cols = {c["name"] for c in inspector.get_columns("meeting_types")}
            if "blocks_assignments" not in mt_cols:
                conn.execute(text(
                    "ALTER TABLE meeting_types "
                    "ADD COLUMN blocks_assignments BOOLEAN NOT NULL DEFAULT FALSE"
                ))
                # Existing Congé / Formation rows are blocking by convention.
                conn.execute(text(
                    "UPDATE meeting_types SET blocks_assignments = TRUE "
                    "WHERE name IN ('Congé', 'Formation')"
                ))
            if "requires_participants" in mt_cols:
                # The per-type flag is gone: every event now requires at least
                # one participant. Drop the column on older databases.
                conn.execute(text(
                    "ALTER TABLE meeting_types DROP COLUMN requires_participants"
                ))
            if "is_technical" not in mt_cols:
                conn.execute(text(
                    "ALTER TABLE meeting_types "
                    "ADD COLUMN is_technical BOOLEAN NOT NULL DEFAULT FALSE"
                ))
                # The three technical mission types.
                conn.execute(text(
                    "UPDATE meeting_types SET is_technical = TRUE "
                    "WHERE name IN ('Audit de code', 'MEEXT', 'Revue SSI')"
                ))
            if "allows_client" not in mt_cols:
                # Default TRUE preserves prior behaviour (every type accepted a
                # client); then disable it on internal absences.
                conn.execute(text(
                    "ALTER TABLE meeting_types "
                    "ADD COLUMN allows_client BOOLEAN NOT NULL DEFAULT TRUE"
                ))
                conn.execute(text(
                    "UPDATE meeting_types SET allows_client = FALSE "
                    "WHERE name IN ('Congé', 'Formation')"
                ))

            # Technical missions are now *always* blocking (a pentester on an
            # audit is unavailable). Backfill the flag on existing technical
            # types — idempotent, runs every boot.
            conn.execute(text(
                "UPDATE meeting_types SET blocks_assignments = TRUE "
                "WHERE is_technical = TRUE AND blocks_assignments = FALSE"
            ))

        # Mission subtypes (Audit / Retest / …) — refinement of a MeetingType.
        # Created idempotently before any data migration that references it.
        if "mission_subtypes" not in table_names:
            conn.execute(text("""
                CREATE TABLE mission_subtypes (
                    id              SERIAL PRIMARY KEY,
                    name            VARCHAR(80) NOT NULL,
                    color           VARCHAR(7) NOT NULL DEFAULT '#64748b',
                    audit_kind      VARCHAR(20),
                    meeting_type_id INTEGER NOT NULL REFERENCES meeting_types(id) ON DELETE CASCADE,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_subtype_type_name UNIQUE (meeting_type_id, name)
                )
            """))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_mission_subtypes_meeting_type_id "
                "ON mission_subtypes (meeting_type_id)"
            ))
            table_names.add("mission_subtypes")
        if "events" in table_names and "meeting_subtype_id" not in event_cols:
            conn.execute(text(
                "ALTER TABLE events ADD COLUMN meeting_subtype_id INTEGER "
                "REFERENCES mission_subtypes(id) ON DELETE SET NULL"
            ))

        # Idempotent data-cleanup: only technical missions can sit in 'preplanifie'.
        # Any non-technical event previously left in that state is bumped to 'planifie'.
        if "events" in table_names and "meeting_types" in table_names:
            conn.execute(text(
                "UPDATE events SET status = 'planifie' "
                "WHERE status = 'preplanifie' "
                "AND (meeting_type_id IS NULL "
                "     OR meeting_type_id NOT IN ("
                "         SELECT id FROM meeting_types WHERE is_technical = TRUE"
                "     ))"
            ))
        # Tasks: add the optional due_date column on legacy tables.
        if "tasks" in table_names:
            task_cols = {c["name"] for c in inspector.get_columns("tasks")}
            if "due_date" not in task_cols:
                conn.execute(text(
                    "ALTER TABLE tasks ADD COLUMN due_date DATE"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_tasks_due_date ON tasks (due_date)"
                ))
            if "is_template" not in task_cols:
                conn.execute(text(
                    "ALTER TABLE tasks ADD COLUMN is_template "
                    "BOOLEAN NOT NULL DEFAULT FALSE"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_tasks_is_template "
                    "ON tasks (is_template)"
                ))
            if "auto_after_mission" not in task_cols:
                conn.execute(text(
                    "ALTER TABLE tasks ADD COLUMN auto_after_mission "
                    "BOOLEAN NOT NULL DEFAULT FALSE"
                ))
            if "auto_offset_days" not in task_cols:
                conn.execute(text(
                    "ALTER TABLE tasks ADD COLUMN auto_offset_days "
                    "INTEGER NOT NULL DEFAULT 5"
                ))
            if "source_event_id" not in task_cols:
                conn.execute(text(
                    "ALTER TABLE tasks ADD COLUMN source_event_id INTEGER "
                    "REFERENCES events(id) ON DELETE SET NULL"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_tasks_source_event_id "
                    "ON tasks (source_event_id)"
                ))
        if "task_statuses" in table_names:
            ts_cols = {c["name"] for c in inspector.get_columns("task_statuses")}
            if "emoji" not in ts_cols:
                conn.execute(text(
                    "ALTER TABLE task_statuses ADD COLUMN emoji VARCHAR(16)"
                ))
        # Meetings: add the category FK on legacy tables. The
        # meeting_categories table must exist *before* the ALTER references
        # it, so create it idempotently here rather than waiting on
        # db.create_all() (which runs after this migration block).
        if "meetings" in table_names:
            if "meeting_categories" not in table_names:
                conn.execute(text("""
                    CREATE TABLE meeting_categories (
                        id         SERIAL PRIMARY KEY,
                        name       VARCHAR(80) UNIQUE NOT NULL,
                        color      VARCHAR(7) NOT NULL DEFAULT '#a855f7',
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """))
                table_names.add("meeting_categories")
            meeting_cols = {c["name"] for c in inspector.get_columns("meetings")}
            if "category_id" not in meeting_cols:
                conn.execute(text(
                    "ALTER TABLE meetings ADD COLUMN category_id INTEGER "
                    "REFERENCES meeting_categories(id) ON DELETE SET NULL"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_meetings_category_id "
                    "ON meetings (category_id)"
                ))
            if "start_time" not in meeting_cols:
                conn.execute(text("ALTER TABLE meetings ADD COLUMN start_time TIME"))
            if "end_time" not in meeting_cols:
                conn.execute(text("ALTER TABLE meetings ADD COLUMN end_time TIME"))
            if "meeting_date_history" not in table_names:
                conn.execute(text("""
                    CREATE TABLE meeting_date_history (
                        id                  SERIAL PRIMARY KEY,
                        meeting_id          INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                        previous_date       DATE NOT NULL,
                        previous_start_time TIME,
                        previous_end_time   TIME,
                        changed_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        changed_by_id       INTEGER REFERENCES users(id) ON DELETE SET NULL
                    )
                """))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_meeting_date_history_meeting_id "
                    "ON meeting_date_history (meeting_id)"
                ))
                table_names.add("meeting_date_history")

        # Ensure the user_roles table exists before migrating data into it.
        if "user_roles" not in table_names:
            conn.execute(text("""
                CREATE TABLE user_roles (
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    role    VARCHAR(40) NOT NULL,
                    PRIMARY KEY (user_id, role)
                )
            """))

        # Migration path A: very-old schema with users.is_admin
        if "is_admin" in user_cols:
            conn.execute(text("""
                INSERT INTO user_roles (user_id, role)
                SELECT id, CASE WHEN is_admin THEN 'admin' ELSE 'audit_web' END
                FROM users
                ON CONFLICT DO NOTHING
            """))
            conn.execute(text("ALTER TABLE users DROP COLUMN is_admin"))

        # Migration path B: single users.role column → user_roles rows
        if "role" in user_cols:
            conn.execute(text("""
                INSERT INTO user_roles (user_id, role)
                SELECT id, role FROM users
                ON CONFLICT DO NOTHING
            """))
            conn.execute(text("ALTER TABLE users DROP COLUMN role"))

        # ===== Role / specialty refactor =====
        # The legacy schema stored audit_kind as a free-form string on both
        # events and mission_subtypes, and treated 'auditeur_*' as hardcoded
        # role keys. Drop those columns + stale role rows so create_all() and
        # the seeding block below can rebuild against the new Role table.

        # Ensure the roles target table exists *before* any FK that references
        # it. db.create_all() builds it later but the ALTERs below fire now.
        if "roles" not in table_names:
            conn.execute(text("""
                CREATE TABLE roles (
                    id         SERIAL PRIMARY KEY,
                    key        VARCHAR(40) UNIQUE NOT NULL,
                    label      VARCHAR(80) NOT NULL,
                    color      VARCHAR(7) NOT NULL DEFAULT '#3b82f6',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
            table_names.add("roles")
        # Drop the retired free-form audit_kind columns; the spécialité now
        # lives in the roles table, referenced by role_id (added just below).
        if "events" in table_names and "audit_kind" in event_cols:
            conn.execute(text("ALTER TABLE events DROP COLUMN audit_kind"))
        if "mission_subtypes" in table_names:
            sub_cols = {c["name"] for c in inspector.get_columns("mission_subtypes")}
            if "audit_kind" in sub_cols:
                conn.execute(text("ALTER TABLE mission_subtypes DROP COLUMN audit_kind"))
            if "role_id" not in sub_cols:
                conn.execute(text(
                    "ALTER TABLE mission_subtypes ADD COLUMN role_id INTEGER "
                    "REFERENCES roles(id) ON DELETE SET NULL"
                ))
        if "events" in table_names and "role_id" not in event_cols:
            conn.execute(text(
                "ALTER TABLE events ADD COLUMN role_id INTEGER "
                "REFERENCES roles(id) ON DELETE SET NULL"
            ))
        # NOTE: stale user_role cleanup is deliberately NOT done here anymore.
        # The old blanket "DELETE every non-system role" wiped valid specialty
        # assignments for admin-created users whenever this legacy path re-ran
        # (e.g. a preprod DB restored from an older snapshot still carrying the
        # audit_kind columns) — which emptied auditor_ids and made every
        # "auditeur disponible" green cell disappear. Orphan strings are now
        # pruned surgically, after roles are seeded/renamed, by
        # _prune_orphan_user_roles() — see seed().

        # Simplify the seeded spécialité keys: auditeur_* → audit_*. Idempotent
        # and runs before seeding, so the renamed rows are in place before
        # _seed_default_roles re-adds any missing default. Both the roles table
        # and the user_roles strings (which store the key, not a FK) are kept in
        # sync. The NOT EXISTS guard avoids a unique clash if the new key was
        # somehow already created.
        if "roles" in table_names:
            for old_key, new_key in (
                ("auditeur_web", "audit_web"),
                ("auditeur_mobile", "audit_mobile"),
                ("auditeur_code", "audit_code"),
            ):
                conn.execute(text(
                    "UPDATE roles SET key = :new WHERE key = :old "
                    "AND NOT EXISTS (SELECT 1 FROM roles r WHERE r.key = :new)"
                ), {"old": old_key, "new": new_key})
                conn.execute(text(
                    "UPDATE user_roles SET role = :new WHERE role = :old"
                ), {"old": old_key, "new": new_key})


def _purge_alice() -> None:
    """Per requirements: the legacy 'alice' user must not exist anymore."""
    alice = db.session.execute(
        db.select(User).where(User.username == "alice")
    ).scalar_one_or_none()
    if alice is None:
        return
    db.session.execute(
        update(Event).where(Event.created_by_id == alice.id).values(created_by_id=None)
    )
    db.session.delete(alice)
    db.session.commit()
    print("[init_db] removed legacy user 'alice'.", flush=True)


def _seed_default_subtypes() -> None:
    """Bootstrap an 'Audit' subtype on technical mission types that have *no*
    subtypes at all, and backfill existing events onto it. Once a type has
    any subtype (Audit, Retest, Pentest, …) we treat it as curated and never
    re-seed — deleting 'Audit' keeps it deleted across boots.

    When the default subtype gets created, we attach the role suggested by
    ``DEFAULT_SUBTYPE_ROLE_BY_TYPE_NAME`` so the participant-sort ★ chip
    works out-of-the-box after the role refactor."""
    roles_by_key = {
        r.key: r for r in db.session.execute(db.select(Role)).scalars().all()
    }
    tech_types = db.session.execute(
        db.select(MeetingType).where(MeetingType.is_technical.is_(True))
    ).scalars().all()
    for mt in tech_types:
        # Curation guard: any existing subtype (including a user-renamed one)
        # means the admin has touched this type. Leave it alone. limit(1) so
        # the query stays scalar even when several subtypes exist.
        existing = db.session.execute(
            db.select(MissionSubtype.id)
            .where(MissionSubtype.meeting_type_id == mt.id)
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            continue
        default = MissionSubtype(
            name="Audit", color=mt.color, meeting_type_id=mt.id,
        )
        db.session.add(default)
        db.session.flush()  # populate default.id
        wanted = DEFAULT_SUBTYPE_ROLE_BY_TYPE_NAME.get(mt.name)
        role = roles_by_key.get(wanted) if wanted else None
        if role is not None:
            default.role_id = role.id
        # Backfill: every event of this type without a subtype gets the default.
        db.session.execute(
            update(Event)
            .where(
                Event.meeting_type_id == mt.id,
                Event.meeting_subtype_id.is_(None),
            )
            .values(meeting_subtype_id=default.id)
        )
        # And backfill role_id on those events too — the subtype's spécialité
        # is what the per-event spécialité column mirrors.
        if default.role_id is not None:
            db.session.execute(
                update(Event)
                .where(
                    Event.meeting_subtype_id == default.id,
                    Event.role_id.is_(None),
                )
                .values(role_id=default.role_id)
            )
    db.session.commit()


def _seed_default_roles() -> None:
    """Seed the three baseline pentest specialties (idempotent).

    Admins can rename their label/color or delete them altogether from the UI
    once the team has settled on its own taxonomy."""
    existing_keys = {
        r.key for r in db.session.execute(db.select(Role)).scalars().all()
    }
    for spec in DEFAULT_ROLES:
        if spec["key"] in existing_keys:
            continue
        db.session.add(Role(
            key=spec["key"], label=spec["label"], color=spec["color"],
        ))
    db.session.commit()


def _prune_orphan_user_roles() -> None:
    """Delete only genuinely-orphaned user_role rows: those whose key is
    neither a system role nor a live ``Role.key``.

    This is the idempotent replacement for the old blanket wipe. It runs on
    every boot but only ever touches strings that map to no usable specialty —
    the same "valid" definition the UI enforces in ``users._invalid_roles``.
    Every valid assignment (seeded *or* admin-created) always survives, so the
    calendar's availability (green) cells can't silently disappear after a
    redeploy. Normal role deletion already cleans its own user_role rows
    (see ``users.delete_role``), so a healthy install has no orphans to prune.

    Set-based so it's cheap; ``rowcount`` is logged when anything was removed.
    Runs after ``_seed_default_roles`` (and after the auditeur_* → audit_*
    rename in ``_migrate_schema``) so renamed/default keys count as valid."""
    result = db.session.execute(
        text(
            "DELETE FROM user_roles "
            "WHERE role NOT IN (SELECT key FROM roles) "
            "AND role NOT IN (:sys_admin, :sys_planif)"
        ),
        {"sys_admin": ROLE_ADMIN, "sys_planif": ROLE_PLANIFICATEUR},
    )
    db.session.commit()
    if result.rowcount:
        print(
            f"[init_db] pruned {result.rowcount} orphan user_role row(s) "
            "(role string with no matching specialty).",
            flush=True,
        )


def _restore_seeded_user_roles() -> None:
    """Re-attach the seeded users' default roles if they have none.

    Valid assignments now survive every redeploy (the destructive wipe is gone
    and _prune_orphan_user_roles only removes orphans), so this is a belt-and-
    suspenders restore for the seeded team (bob/carol/david/eve) on a DB where
    they genuinely ended up with no specialty. We only re-apply roles to users
    whose username AND role key match the original seed — never to admin-created
    users — so it stays idempotent and never silently grants permissions an
    operator may have removed on purpose."""
    seed_by_username = {spec["username"]: spec["roles"] for spec in DEFAULT_USERS}
    valid_keys = {r.key for r in db.session.execute(db.select(Role)).scalars().all()}
    valid_keys.update({ROLE_ADMIN, ROLE_PLANIFICATEUR})
    for u in db.session.execute(db.select(User)).scalars().all():
        wanted = seed_by_username.get(u.username)
        if not wanted:
            continue
        if u.roles:
            continue  # admin/planificateur survived the wipe — don't double up
        u.roles = [r for r in wanted if r in valid_keys]
    db.session.commit()


def seed() -> None:
    app = create_app()
    with app.app_context():
        _migrate_schema()
        db.create_all()
        _purge_alice()
        _seed_default_roles()
        _prune_orphan_user_roles()
        _restore_seeded_user_roles()
        _seed_default_subtypes()

        # Seed defaults only on a fresh install. Once the table has any row,
        # we assume the admin has curated it (possibly deleting some seed
        # entries) and we must NOT recreate them on every boot.
        mt_count = db.session.execute(
            db.select(db.func.count(MeetingType.id))
        ).scalar_one()
        if mt_count == 0:
            for mt in DEFAULT_MEETING_TYPES:
                db.session.add(MeetingType(**mt))

        ts_count = db.session.execute(
            db.select(db.func.count(TaskStatus.id))
        ).scalar_one()
        if ts_count == 0:
            for status in DEFAULT_TASK_STATUSES:
                db.session.add(TaskStatus(**status))

        mc_count = db.session.execute(
            db.select(db.func.count(MeetingCategory.id))
        ).scalar_one()
        if mc_count == 0:
            for category in DEFAULT_MEETING_CATEGORIES:
                db.session.add(MeetingCategory(**category))

        user_count = db.session.execute(
            db.select(db.func.count(User.id))
        ).scalar_one()
        if user_count > 0:
            # Skip the default-users loop entirely — admin curated.
            db.session.commit()
            print("[init_db] schéma prêt (utilisateurs/types conservés).", flush=True)
            return

        # Seeding the default team (admin / bob / carol / david / eve) installs
        # *publicly documented* passwords. Fine for `docker compose up` on a
        # laptop, lethal on a public-facing first boot — so a real production
        # deploy must explicitly opt out with DISABLE_DEFAULT_SEED=1 and
        # bootstrap users another way (SQL, manage CLI, etc.).
        if os.environ.get("DISABLE_DEFAULT_SEED") == "1":
            db.session.commit()
            print(
                "[init_db] DISABLE_DEFAULT_SEED=1 — aucune équipe par défaut injectée. "
                "Créez l'administrateur initial manuellement (psql ou outil dédié) "
                "avant la première connexion.",
                flush=True,
            )
            return

        for spec in DEFAULT_USERS:
            u = User(
                username=spec["username"],
                full_name=spec["full_name"],
                color=spec["color"],
                # Public seed passwords — force a change at first login.
                must_change_password=True,
            )
            u.set_password(spec["password"])
            u.roles = list(spec["roles"])
            db.session.add(u)

        db.session.commit()
        print(
            "[init_db] schéma prêt, équipe pentest et types de mission initialisés.",
            flush=True,
        )
        print(
            "[security] WARNING: default seeded passwords are public (see init_db.DEFAULT_USERS). "
            "Each seeded account must change its password at first login; do so before "
            "exposing the app.",
            file=sys.stderr, flush=True,
        )


if __name__ == "__main__":
    try:
        seed()
    except Exception as exc:  # noqa: BLE001
        print(f"[init_db] FAILED: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
