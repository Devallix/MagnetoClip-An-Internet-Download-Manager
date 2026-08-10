from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from .migrations import run_migrations


class Database:
    """Owns the SQLAlchemy engine and session factory for the app database."""

    def __init__(self, path: Path, *, echo: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(
            f"sqlite:///{self.path.as_posix()}",
            echo=echo,
            connect_args={"check_same_thread": False},
        )
        event.listen(self.engine, "connect", _enable_wal)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def initialize(self) -> None:
        run_migrations(self.engine)

    def close(self) -> None:
        self.engine.dispose()


def _enable_wal(dbapi_connection, connection_record) -> None:  # noqa: ANN001
    """Use WAL journaling and a busy timeout so the separate browser-host
    process can safely share this database with the main app."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=wal")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()
