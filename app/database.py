"""Async SQLAlchemy database engine and session factory."""
import asyncio
import importlib
import logging
import pkgutil
import socket
import sys
from pathlib import Path
from typing import AsyncGenerator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

logger = logging.getLogger(__name__)

DATABASE_UNAVAILABLE_DETAIL = (
    "Pakalon could not reach its configured database. Start Docker Desktop or point "
    "DATABASE_URL at a reachable database, then retry."
)

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""
    pass


def is_sqlite_database_url(database_url: str) -> bool:
    return database_url.startswith("sqlite+")


def _is_local_database_host(hostname: str | None) -> bool:
    return hostname in {"localhost", "127.0.0.1", "::1"}


def _tcp_endpoint_is_reachable(hostname: str, port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


def resolve_effective_database_url() -> str:
    settings = get_settings()
    database_url = settings.database_url

    if (
        not settings.is_development
        or not settings.development_allow_sqlite_fallback
        or is_sqlite_database_url(database_url)
    ):
        return database_url

    parsed = urlsplit(database_url)
    hostname = parsed.hostname
    port = parsed.port
    if not hostname or not port or not _is_local_database_host(hostname):
        return database_url

    if _tcp_endpoint_is_reachable(hostname, port):
        return database_url

    fallback_url = settings.development_database_fallback_url
    if is_sqlite_database_url(fallback_url):
        sqlite_path = fallback_url.replace("sqlite+aiosqlite:///", "", 1)
        Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)

    logger.warning(
        "Database at %s:%s is unreachable; using development SQLite fallback at %s",
        hostname,
        port,
        fallback_url,
    )
    return fallback_url


def normalize_async_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql+psycopg://"):
        database_url = database_url.replace("postgresql+psycopg://", "postgresql+asyncpg://", 1)

        split_url = urlsplit(database_url)
        query = dict(parse_qsl(split_url.query, keep_blank_values=True))
        sslmode = query.pop("sslmode", None)
        if sslmode and "ssl" not in query:
            query["ssl"] = sslmode
        return urlunsplit(
            (
                split_url.scheme,
                split_url.netloc,
                split_url.path,
                urlencode(query),
                split_url.fragment,
            )
        )

    return database_url


def is_database_unavailable_error(exc: BaseException) -> bool:
    connection_markers = (
        "connect call failed",
        "connection refused",
        "could not connect",
        "connection is closed",
        "failed to establish a new connection",
        "timeout expired",
        "network is unreachable",
    )

    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        text = str(current).lower()
        if isinstance(current, OSError) and any(marker in text for marker in connection_markers):
            return True
        if any(marker in text for marker in connection_markers):
            return True
        current = current.__cause__ or current.__context__

    return False


def _import_all_model_modules() -> None:
    import app.models as models_package  # noqa: PLC0415

    for module_info in pkgutil.iter_modules(models_package.__path__):
        if module_info.name in {"contribution_day"}:
            continue
        if not module_info.name.startswith("_"):
            importlib.import_module(f"{models_package.__name__}.{module_info.name}")


async def initialize_database_if_needed() -> None:
    if not is_sqlite_database_url(ACTIVE_DATABASE_URL):
        return

    _import_all_model_modules()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _make_engine():
    settings = get_settings()
    return make_async_engine(echo=settings.is_development)


def make_async_engine(*, echo: bool = False):
    database_url = normalize_async_database_url(resolve_effective_database_url())

    if is_sqlite_database_url(database_url):
        return create_async_engine(
            database_url,
            echo=echo,
            connect_args={"check_same_thread": False},
        )

    return create_async_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )


ACTIVE_DATABASE_URL = normalize_async_database_url(resolve_effective_database_url())


engine = _make_engine()

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
