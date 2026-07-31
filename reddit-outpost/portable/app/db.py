"""Database wiring — the standalone replacement for Letaido's `src.db_cross`.

One engine, one session factory, one declarative base. PostgreSQL only: the app
stores JSON columns and relies on `ON CONFLICT`, and it runs background threads
that would trip over SQLite's locking.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://outpost:outpost@localhost:5432/outpost",
)

engine = create_engine(DB_URL, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False,
                            future=True)


class Base(DeclarativeBase):
    """Declarative base for every Outpost model."""


@contextmanager
def session_scope():
    """Transactional scope. Commits on success, rolls back on error.

    Used by both request handlers and background threads, so it must not depend
    on any request context.
    """
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def init_db() -> None:
    """Create tables that don't exist yet. Never drops or alters."""
    from app import models  # noqa: F401 — registers the models on Base
    Base.metadata.create_all(engine)
