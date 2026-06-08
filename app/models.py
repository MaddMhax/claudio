import secrets
from datetime import datetime, timezone

import bcrypt
from flask_login import UserMixin
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.ext.associationproxy import association_proxy
from sqlalchemy.orm import relationship

from .extensions import db


# System roles — hardcoded, can never be deleted/renamed in the UI. They drive
# permission checks (is_admin, can_manage_events). Every other role lives in
# the Role table and is fully admin-managed.
ROLE_ADMIN = "admin"
ROLE_PLANIFICATEUR = "planificateur"

SYSTEM_ROLES: dict[str, str] = {
    ROLE_ADMIN: "Administrateur",
    ROLE_PLANIFICATEUR: "Planificateur",
}

SYSTEM_ROLE_COLORS: dict[str, str] = {
    ROLE_ADMIN: "#a855f7",
    ROLE_PLANIFICATEUR: "#ec4899",
}


# Provenance roles — admin-managed (live in the Role table, seeded by init_db)
# but semantically a *who-employs-them* axis rather than a pentest specialty.
# They tag a collaborator as internal staff vs an external provider. Their keys
# are stable so the planning can identify externals; the labels/colours stay
# editable. They are deliberately excluded from the calendar's auditor/overcharge
# colouring so an external pentester can carry technical missions without tinting
# the availability cells. Holding the external role makes ``User.is_external`` true.
ROLE_INTERNE = "interne"
ROLE_EXTERNE = "externe_prestataire"
PROVENANCE_ROLE_KEYS: set[str] = {ROLE_INTERNE, ROLE_EXTERNE}


def dynamic_role_keys() -> list[str]:
    """All admin-managed role keys (everything outside SYSTEM_ROLES)."""
    return [
        row[0] for row in db.session.execute(
            db.select(Role.key).order_by(Role.label)
        ).all()
    ]


def role_label_lookup() -> dict[str, str]:
    """Display labels keyed by role key, system + dynamic roles combined.

    Cached on ``flask.g`` for the request: this is hit once per ``User`` while
    rendering lists (users table, participant pickers, …), so without the cache
    a page with N users fires N identical ``SELECT * FROM roles`` queries."""
    from flask import g, has_request_context

    if has_request_context():
        cached = g.get("_role_label_lookup")
        if cached is not None:
            return cached

    out = dict(SYSTEM_ROLES)
    for r in db.session.execute(db.select(Role)).scalars().all():
        out[r.key] = r.label

    if has_request_context():
        g._role_label_lookup = out
    return out


# Event workflow status
EVENT_STATUS_PREPLANIFIE = "preplanifie"
EVENT_STATUS_PLANIFIE = "planifie"

EVENT_STATUSES: dict[str, str] = {
    EVENT_STATUS_PREPLANIFIE: "Préplanifié",
    EVENT_STATUS_PLANIFIE: "Planifié",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


event_participants = Table(
    "event_participants",
    db.metadata,
    Column("event_id", Integer, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
)


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    full_name = Column(String(120), nullable=False)
    color = Column(String(7), nullable=False, default="#3b82f6")  # CSS hex
    # Nullable: SSO-only accounts (provisioned via OIDC) have no local password.
    password_hash = Column(String(255), nullable=True)
    # Stable OpenID Connect subject identifier, set once a user signs in via
    # SSO. Unique so a federated identity maps to exactly one local account.
    oidc_sub = Column(String(255), unique=True, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    # --- Brute-force throttling (DB-backed so it holds across gunicorn workers).
    failed_login_count = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime(timezone=True), nullable=True)

    # Force a password change on next login (seeded/temporary credentials).
    must_change_password = Column(Boolean, nullable=False, default=False)

    # Opaque per-user session token. Stored in the Flask session at login and
    # rotated on password change so changing a password invalidates every other
    # active session. NULL until the user first authenticates with a password.
    session_token = Column(String(64), nullable=True)

    # Opaque, unguessable token authenticating the personal iCal feed (calendar
    # apps can't carry a login session). Rotatable to revoke a leaked URL.
    ical_token = Column(String(64), unique=True, nullable=True, index=True)

    _role_assocs = relationship(
        "UserRole",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    # Flat list-of-strings view; assigning a list creates/removes UserRole rows.
    roles = association_proxy(
        "_role_assocs", "role",
        creator=lambda role: UserRole(role=role),
    )

    @property
    def is_admin(self) -> bool:
        return ROLE_ADMIN in self.roles

    @property
    def can_manage_events(self) -> bool:
        return any(r in (ROLE_ADMIN, ROLE_PLANIFICATEUR) for r in self.roles)

    @property
    def is_technical(self) -> bool:
        """A user is technical iff they hold at least one non-system role —
        i.e. a role admin-managed via the Roles table (web/mobile/code/…)."""
        return any(r not in SYSTEM_ROLES for r in self.roles)

    @property
    def is_external(self) -> bool:
        """True iff this collaborator is an external provider (« prestataire »).

        Externals can be assigned to technical missions like anyone else, but
        the planning deliberately leaves them out of the availability /
        overcharge cell colouring — see ``app.planning._build_month_panel``."""
        return ROLE_EXTERNE in self.roles

    @property
    def role_labels(self) -> list[str]:
        lookup = role_label_lookup()
        return [lookup.get(r, r) for r in sorted(self.roles)]

    @property
    def role_label(self) -> str:
        """Single comma-joined label, for legacy template uses."""
        return ", ".join(self.role_labels) if self.roles else "—"

    events = relationship(
        "Event",
        secondary=event_participants,
        back_populates="participants",
        lazy="selectin",
    )

    @property
    def is_locked(self) -> bool:
        """True while a brute-force lockout window is still in effect."""
        return self.locked_until is not None and self.locked_until > _utcnow()

    def rotate_session_token(self) -> None:
        """Issue a fresh session token, invalidating any other live sessions."""
        self.session_token = secrets.token_urlsafe(32)

    def ensure_ical_token(self) -> str:
        """Return the iCal feed token, minting one on first use."""
        if not self.ical_token:
            self.ical_token = secrets.token_urlsafe(32)
        return self.ical_token

    def set_password(self, plaintext: str) -> None:
        if len(plaintext) < 8:
            raise ValueError("Password must be at least 8 characters long.")
        self.password_hash = bcrypt.hashpw(
            plaintext.encode("utf-8"), bcrypt.gensalt(rounds=12)
        ).decode("utf-8")
        # A password change invalidates other sessions and clears any lockout.
        self.rotate_session_token()
        self.failed_login_count = 0
        self.locked_until = None

    def check_password(self, plaintext: str) -> bool:
        # SSO-only accounts carry no hash — password login can never match.
        if not self.password_hash:
            return False
        try:
            return bcrypt.checkpw(
                plaintext.encode("utf-8"), self.password_hash.encode("utf-8")
            )
        except (ValueError, TypeError):
            return False


class UserRole(db.Model):
    __tablename__ = "user_roles"

    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Free-form string that matches either a SYSTEM_ROLES key (admin /
    # planificateur) or a Role.key (admin-managed). No FK because the two
    # universes coexist: system roles never have a Role row.
    role = Column(String(40), primary_key=True)

    user = relationship("User", back_populates="_role_assocs")


class Role(db.Model):
    """Admin-managed role / spécialité (web, mobile, code, …).

    Each row is both a role a user can hold (stored as ``UserRole.role`` =
    ``Role.key``) AND a spécialité a MissionSubtype can target. The system
    roles ``admin`` / ``planificateur`` live as constants outside this table."""
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True)
    # Stable slug used as the value in UserRole.role and as the join key on
    # MissionSubtype/Event. Immutable in the UI to keep historical references
    # valid through label renames.
    key = Column(String(40), unique=True, nullable=False)
    label = Column(String(80), nullable=False)
    color = Column(String(7), nullable=False, default="#3b82f6")
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class AppSetting(db.Model):
    """Generic key/value store for runtime-editable settings.

    One row per setting key. Values are plain text; sensitive values (like the
    OIDC client secret) are stored already-encrypted by the caller — see
    ``settings_store``. Used today for UI-managed OIDC SSO configuration."""
    __tablename__ = "app_settings"

    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=True)


class AuditLog(db.Model):
    """Append-only security/audit trail.

    Records authentication events (login success/failure, logout, password
    change, lockout) and privileged mutations (user/role CRUD, SSO link
    changes, SSO config edits). ``actor_username`` is denormalised so the line
    survives deletion of the acting user; ``actor_id`` keeps the live link
    while the user exists."""
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)
    actor_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    actor = relationship("User", foreign_keys=[actor_id])
    # Denormalised actor label (username) — kept even after the user is deleted.
    actor_username = Column(String(64), nullable=True)
    # Short machine-ish verb, e.g. "login.success", "user.delete".
    action = Column(String(64), nullable=False, index=True)
    # Human-readable subject of the action (e.g. the affected username).
    target = Column(String(255), nullable=True)
    ip = Column(String(64), nullable=True)
    detail = Column(Text, nullable=True)


# Coarse-grained API access scopes. Today the integration API (Pwndoc import)
# is strictly read-only; the mapping leaves room to add e.g. "read_write" later
# without a schema change. The label is what the admin UI shows.
API_SCOPE_READ_ONLY = "read_only"

API_SCOPES: dict[str, str] = {
    API_SCOPE_READ_ONLY: "Lecture seule (read-only)",
}


class ApiToken(db.Model):
    """Bearer token authenticating a machine client of the read-only API
    (e.g. the Pwndoc report generator pulling project data).

    Deliberately decoupled from the ``User`` table: an API client is not a team
    member, must never appear on the planning, and carries its own scope rather
    than a human role. The secret is stored **hashed** (SHA-256 hex) — the
    plaintext is shown exactly once at creation, like a GitHub PAT — so a DB
    leak never yields a usable token. Deactivating a row revokes it instantly."""
    __tablename__ = "api_tokens"

    id = Column(Integer, primary_key=True)
    # Human label so an admin can tell tokens apart ("Pwndoc prod", …).
    label = Column(String(80), nullable=False)
    # SHA-256 hex of the plaintext token. Unique + indexed for O(1) lookup.
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    # Access scope — see API_SCOPES. Defaults to read-only.
    scope = Column(String(20), nullable=False, default=API_SCOPE_READ_ONLY)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    created_by = relationship("User", foreign_keys=[created_by_id])
    # Stamped on each successful authenticated call (last-seen heartbeat).
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    @property
    def scope_label(self) -> str:
        return API_SCOPES.get(self.scope, self.scope)


class Client(db.Model):
    __tablename__ = "clients"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    projects = relationship(
        "Project", back_populates="client",
        cascade="all, delete-orphan", lazy="selectin",
    )


# Project lifecycle status.
PROJECT_STATUS_ACTIVE = "actif"
PROJECT_STATUS_CLOSED = "clos"

PROJECT_STATUSES: dict[str, str] = {
    PROJECT_STATUS_ACTIVE: "Actif",
    PROJECT_STATUS_CLOSED: "Clos",
}


class Project(db.Model):
    """Top-level container for all customer-facing work.

    A project belongs to one client and aggregates the missions (technical
    audit events), accompanying meetings (cadrage, restitution, standalones),
    and follow-up tasks performed for that client. Internal absences
    (formation, congé) are out-of-project and live in the "Divers" view."""
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    code = Column(String(40), nullable=True)
    description = Column(Text, nullable=True)
    status = Column(
        String(20), nullable=False, default=PROJECT_STATUS_ACTIVE, index=True,
    )
    client_id = Column(
        Integer, ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    client = relationship("Client", back_populates="projects", lazy="joined")

    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    created_by = relationship("User", foreign_keys=[created_by_id])
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    missions = relationship(
        "Event", back_populates="project",
        cascade="all, delete-orphan", lazy="selectin",
        order_by="Event.start_date",
    )
    meetings = relationship(
        "Meeting", back_populates="project",
        cascade="all, delete-orphan", lazy="selectin",
        order_by="Meeting.date",
    )
    tasks = relationship(
        "Task", back_populates="project",
        cascade="all, delete-orphan", lazy="selectin",
        order_by="Task.due_date",
    )

    __table_args__ = (
        UniqueConstraint("client_id", "name", name="uq_project_client_name"),
    )


class MeetingType(db.Model):
    __tablename__ = "meeting_types"

    id = Column(Integer, primary_key=True)
    name = Column(String(80), unique=True, nullable=False)
    color = Column(String(7), nullable=False, default="#64748b")
    description = Column(Text, nullable=True)
    # When True, a participant of any event of this type is considered unavailable
    # for mission assignment over the same period (e.g. Congé, Formation).
    blocks_assignments = Column(Boolean, nullable=False, default=False)
    # When True, the event is a technical pentest mission — the planificateur must
    # pick a subtype (Audit, Retest, …). The chosen subtype carries the optional
    # audit specialty (web / mobile / code).
    is_technical = Column(Boolean, nullable=False, default=False)
    # When True, the event form exposes the "Client" dropdown for events of this
    # type. Internal absences (Congé, Formation) keep this False.
    allows_client = Column(Boolean, nullable=False, default=True)

    subtypes = relationship(
        "MissionSubtype",
        back_populates="meeting_type",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="MissionSubtype.name",
    )


class MissionSubtype(db.Model):
    """Refinement of a MeetingType (e.g. Mission technique → Audit / Retest).

    A subtype optionally targets a Role — the spécialité events of this subtype
    inherit. When the user picks a subtype on the event form, the event copies
    that ``role_id`` so per-event specialty filtering keeps working even if the
    subtype is later edited."""
    __tablename__ = "mission_subtypes"

    id = Column(Integer, primary_key=True)
    name = Column(String(80), nullable=False)
    color = Column(String(7), nullable=False, default="#64748b")
    # Optional spécialité inherited by events of this subtype.
    role_id = Column(
        Integer, ForeignKey("roles.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    role = relationship("Role", lazy="joined")
    meeting_type_id = Column(
        Integer, ForeignKey("meeting_types.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    meeting_type = relationship("MeetingType", back_populates="subtypes")
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("meeting_type_id", "name", name="uq_subtype_type_name"),
    )


class Event(db.Model):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    start_date = Column(Date, nullable=False, index=True)
    end_date = Column(Date, nullable=False, index=True)
    start_time = Column(Time, nullable=False, default="09:00")
    end_time = Column(Time, nullable=False, default="17:00")
    status = Column(String(20), nullable=False, default=EVENT_STATUS_PREPLANIFIE, index=True)
    # Optional spécialité — FK to the Role table. SET NULL on role deletion so
    # historical events survive a role being removed.
    role_id = Column(
        Integer, ForeignKey("roles.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    role = relationship("Role", lazy="joined", foreign_keys=[role_id])

    # FPR (Fiche Pré-Requis) — only meaningful when role_id is set (audit mission).
    fpr_received = Column(Boolean, nullable=False, default=False)
    fpr_url = Column(String(500), nullable=True)

    # Free-form post-mortem notes ("difficultés rencontrées").
    difficulties = Column(Text, nullable=True)

    @property
    def audit_kind(self):
        """Back-compat shim — the role key of the event's spécialité, if any."""
        return self.role.key if self.role else None

    @property
    def needs_fpr(self) -> bool:
        """An audit mission must have its FPR received before being planifié."""
        return self.role_id is not None

    @property
    def fpr_missing(self) -> bool:
        return self.needs_fpr and not self.fpr_received

    meeting_type_id = Column(
        Integer, ForeignKey("meeting_types.id", ondelete="SET NULL"), nullable=True
    )
    meeting_type = relationship("MeetingType", lazy="joined")

    meeting_subtype_id = Column(
        Integer, ForeignKey("mission_subtypes.id", ondelete="SET NULL"), nullable=True
    )
    meeting_subtype = relationship("MissionSubtype", lazy="joined")

    # Nullable: project events carry a project (whose client is the customer);
    # internal absences (formation/congé) sit in the Divers view and have no
    # project. There is no direct client_id anymore — client is derived from
    # the parent project.
    project_id = Column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    project = relationship("Project", back_populates="missions", lazy="joined")

    @property
    def client(self):
        return self.project.client if self.project else None

    @property
    def client_id(self):
        return self.project.client_id if self.project else None

    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    created_by = relationship("User", foreign_keys=[created_by_id])

    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    # Last-modification audit. ``updated_at`` is left NULL on creation (onupdate
    # only fires on UPDATE) so a never-edited event has no "modifié" line; the
    # editing routes set ``updated_by_id`` explicitly.
    updated_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    updated_by = relationship("User", foreign_keys=[updated_by_id])
    updated_at = Column(DateTime(timezone=True), onupdate=_utcnow, nullable=True)

    participants = relationship(
        "User",
        secondary=event_participants,
        back_populates="events",
        lazy="selectin",
    )

    date_history = relationship(
        "EventDateHistory",
        back_populates="event",
        cascade="all, delete-orphan",
        order_by="EventDateHistory.changed_at.desc()",
        lazy="selectin",
    )

    __table_args__ = (
        UniqueConstraint("title", "start_date", "end_date", name="uq_event_title_dates"),
    )


class TaskStatus(db.Model):
    """User-defined status bucket for the team's task board (admin-only).

    ``emoji`` is an optional pictogram prepended to the task name when the
    task is rendered on the planning."""
    __tablename__ = "task_statuses"

    id = Column(Integer, primary_key=True)
    name = Column(String(80), unique=True, nullable=False)
    color = Column(String(7), nullable=False, default="#64748b")
    emoji = Column(String(16), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    tasks = relationship("Task", back_populates="status", lazy="selectin")


class Task(db.Model):
    """Free-form task tracked on the admin task board.

    ``due_date`` is optional; when set, the task is rendered on the planning
    view at the matching day cell.

    ``is_template`` marks the row as a reusable pattern: it never appears on
    the planning itself, but each meeting creation spawns a fresh copy
    (status « À faire », due_date = meeting + 5 days). Templates can be
    edited like any other task; their generated copies are independent.

    ``project_id`` is the parent project. Templates carry no project
    (project_id NULL); every other task belongs to exactly one project."""
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    due_date = Column(Date, nullable=True, index=True)
    is_template = Column(Boolean, nullable=False, default=False, index=True)
    # When True on a template, every newly-created technical mission auto-spawns
    # a copy of this task on the mission's project, due ``auto_offset_days``
    # working days after the mission's end_date.
    auto_after_mission = Column(Boolean, nullable=False, default=False)
    auto_offset_days = Column(Integer, nullable=False, default=5)
    status_id = Column(
        Integer,
        ForeignKey("task_statuses.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status = relationship("TaskStatus", back_populates="tasks", lazy="joined")

    project_id = Column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    project = relationship("Project", back_populates="tasks", lazy="joined")

    # When a task is auto-spawned from a technical mission, this points back at
    # that Event so renaming the mission can propagate to the task name. SET
    # NULL on mission deletion (the task survives on its project).
    source_event_id = Column(
        Integer, ForeignKey("events.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    created_by_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_by = relationship("User", foreign_keys=[created_by_id])
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class MeetingCategory(db.Model):
    """Admin-managed category for standalone meetings (the "type" surfaced
    on /meetings). Kept separate from MeetingType — which actually labels
    *mission* types on events — so the two concepts don't collide."""
    __tablename__ = "meeting_categories"

    id = Column(Integer, primary_key=True)
    name = Column(String(80), unique=True, nullable=False)
    color = Column(String(7), nullable=False, default="#a855f7")
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    meetings = relationship("Meeting", back_populates="category", lazy="selectin")


class Meeting(db.Model):
    """Standalone meeting added to the planning view — no participants,
    no mission type. Just a name and a date that everyone can see.

    Always attached to a project: meetings exist to serve a customer
    engagement; out-of-project meetings have no business meaning."""
    __tablename__ = "meetings"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    date = Column(Date, nullable=False, index=True)
    # Optional time window. Both columns nullable so legacy meetings without
    # times keep displaying as plain date labels on the calendar.
    start_time = Column(Time, nullable=True)
    end_time = Column(Time, nullable=True)
    category_id = Column(
        Integer,
        ForeignKey("meeting_categories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    category = relationship("MeetingCategory", back_populates="meetings", lazy="joined")

    project_id = Column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    project = relationship("Project", back_populates="meetings", lazy="joined")

    created_by_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_by = relationship("User", foreign_keys=[created_by_id])
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    date_history = relationship(
        "MeetingDateHistory",
        back_populates="meeting",
        cascade="all, delete-orphan",
        order_by="MeetingDateHistory.changed_at.desc()",
        lazy="selectin",
    )


class MeetingDateHistory(db.Model):
    """One row per date / time change on a meeting. Same shape as
    EventDateHistory minus the workflow-status column (meetings have none)."""
    __tablename__ = "meeting_date_history"

    id = Column(Integer, primary_key=True)
    meeting_id = Column(
        Integer, ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    previous_date = Column(Date, nullable=False)
    previous_start_time = Column(Time, nullable=True)
    previous_end_time = Column(Time, nullable=True)
    changed_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    changed_by_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )

    meeting = relationship("Meeting", back_populates="date_history")
    changed_by = relationship("User", foreign_keys=[changed_by_id])


class EventDateHistory(db.Model):
    """One row per date change on an event. Lets the planificateur trace how
    a mission's planning evolved before reaching its current dates."""
    __tablename__ = "event_date_history"

    id = Column(Integer, primary_key=True)
    event_id = Column(
        Integer, ForeignKey("events.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Snapshot of the dates / status that were overwritten.
    previous_start_date = Column(Date, nullable=False)
    previous_end_date = Column(Date, nullable=False)
    previous_start_time = Column(Time, nullable=True)
    previous_end_time = Column(Time, nullable=True)
    previous_status = Column(String(20), nullable=True)
    changed_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    changed_by_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )

    event = relationship("Event", back_populates="date_history")
    changed_by = relationship("User", foreign_keys=[changed_by_id])


class HolidayOverride(db.Model):
    """Per-deployment override of the computed public-holiday calendar.

    ``worked = True``  → this date is normally a French public holiday but is
                         exceptionally worked at this company. It is then treated
                         as a normal working day everywhere: planning availability,
                         the JH (jours-homme) maths, and ``add_working_days``.
    ``worked = False`` → a custom company day off (pont, fermeture…) that isn't a
                         national public holiday. Treated as non-workable, like a
                         holiday.

    One row per date. This is what makes the worked calendar fully customisable
    without touching the hardcoded French list in ``holidays.py``."""
    __tablename__ = "holiday_overrides"

    id = Column(Integer, primary_key=True)
    holiday_date = Column(Date, nullable=False, unique=True, index=True)
    worked = Column(Boolean, nullable=False, default=True)
    label = Column(String(120), nullable=True)
    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    created_by = relationship("User", foreign_keys=[created_by_id])
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
