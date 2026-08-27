"""SQLAlchemy engine/session setup for the dashboard package.

New component -- no database exists anywhere else in this repo today (the
existing pipeline's only persistence is a Zoho-hosted master.json blob, see
docs/architecture.md). Sync engine (psycopg 3), plain session-per-request via
get_db(); get_session() is the equivalent context-manager form for use from
background threads (the upload queue worker, translation job runner) that
aren't inside a FastAPI request.
"""
from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from dashboard.config import DATABASE_URL


class Base(DeclarativeBase):
    pass


# Engine/sessionmaker are constructed lazily on first real use, not at
# import time -- DATABASE_URL may not be set yet (see config.py's comment),
# and pop_server.py imports this module chain unconditionally at startup, so
# eagerly calling create_engine(None) here would crash the already-working
# pipeline server before a single request is served.
_engine = None
_SessionLocal: sessionmaker | None = None


def _get_engine_and_sessionmaker():
    global _engine, _SessionLocal
    if _engine is None:
        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not set -- the dashboard database is unavailable. "
                "Set it in .env (see docs/dashboard_backend_plan.md §7) and restart."
            )
        _engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, expire_on_commit=False)
    return _engine, _SessionLocal


def init_db() -> None:
    """Create the pgvector extension (if missing) and all tables. Idempotent
    -- safe to call on every startup, matching the existing lifespan()'s
    idempotent Zoho-singleton init in pop_server.py. Raises RuntimeError
    (caught and logged, not fatal) if DATABASE_URL isn't configured yet."""
    engine, _ = _get_engine_and_sessionmaker()
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    import dashboard.models  # noqa: F401  (registers all tables on Base.metadata)

    Base.metadata.create_all(engine)


@contextmanager
def get_session():
    _, SessionLocal = _get_engine_and_sessionmaker()
    session: Session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db():
    """FastAPI dependency form of get_session()."""
    _, SessionLocal = _get_engine_and_sessionmaker()
    session: Session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
