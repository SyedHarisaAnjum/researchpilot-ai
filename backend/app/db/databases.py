"""Database engine and sessions. SQLite for local use; set DATABASE_URL for PostgreSQL."""

import logging
from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.config import BACKEND_DIR, get_settings


class Base(DeclarativeBase):
    pass


def database_url() -> str:
    """The configured URL, with a relative SQLite path resolved against backend/ so it
    doesn't depend on the directory the server was started from."""
    url = make_url(get_settings().database_url)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        path = Path(url.database)
        if not path.is_absolute():
            url = url.set(database=str((BACKEND_DIR / path).resolve()))
    return url.render_as_string(hide_password=False)


def is_sqlite(url: str) -> bool:
    return make_url(url).get_backend_name() == "sqlite"


# Tests open several event loops; pooled SQLite connections must not be shared between them.
engine = create_async_engine(
    database_url(),
    poolclass=NullPool if get_settings().environment == "test" else None,
)

if is_sqlite(database_url()):

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")  # SQLite ignores foreign keys unless asked
        cursor.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request."""
    async with SessionLocal() as session:
        yield session


def run_migrations() -> None:
    """Bring the database schema up to date (alembic upgrade head)."""
    logging.getLogger("alembic.runtime.plugins").setLevel(logging.WARNING)
    from alembic import command
    from alembic.config import Config

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")
