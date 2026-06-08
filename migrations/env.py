"""Alembic environment.

Targets the SQLAlchemy metadata defined by the app's models, so
``alembic revision --autogenerate`` produces real, reviewable migrations going
forward. The database URL comes from ``DATABASE_URL`` (same as the app).

Cutover note: the legacy hand-rolled migrations in ``app/init_db.py`` remain
authoritative for the *current* schema. To adopt Alembic on an existing
database, bring it to the current schema as usual, then baseline it once with
``alembic stamp head`` and use ``alembic revision``/``upgrade`` thereafter.
"""
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Import the app's metadata (registers all models on db.metadata).
from app.extensions import db
import app.models  # noqa: F401 — side-effect import: populates db.metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

_db_url = os.environ.get("DATABASE_URL")
if _db_url:
    config.set_main_option("sqlalchemy.url", _db_url)

target_metadata = db.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
