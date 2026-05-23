from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from app.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Migrations should always go to the unpooled connection (Supabase recommends
# DIRECT_URL on port 5432). Fall back to DATABASE_URL for local Postgres.
# Note: we deliberately do NOT call config.set_main_option("sqlalchemy.url", _url)
# because configparser interprets `%` as an interpolation character, which breaks
# URL-encoded passwords like `%40`.
_url = os.getenv("DIRECT_URL") or os.getenv("DATABASE_URL")
if not _url:
    raise RuntimeError("DIRECT_URL or DATABASE_URL must be set to run alembic")

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_url, poolclass=pool.NullPool, future=True)
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
