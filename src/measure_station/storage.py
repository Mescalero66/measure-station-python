"""Durable local outbox (SQLite) for store-and-forward MQTT delivery.

Every reading is written here before any publish attempt; a row is deleted only once
the broker has acknowledged it. The outbox therefore survives network outages,
process crashes and reboots.

All methods are blocking and thread-safe; call them via ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import NamedTuple, TypeVar

from measure_station.models import OutboxMessage

log = logging.getLogger(__name__)

_T = TypeVar("_T")

# No AUTOINCREMENT: it writes an extra page (sqlite_sequence) on every insert, ~1/3 of
# all outbox writes. A plain rowid is always max(id)+1, so new rows still sort after
# every pending row; ids may be reused once deleted, which nothing here relies on.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id         INTEGER PRIMARY KEY,
    created_at REAL    NOT NULL,
    topic      TEXT    NOT NULL,
    payload    TEXT    NOT NULL
)
"""

# Error codes meaning the file itself is damaged (as opposed to e.g. a full disk,
# where we must NOT throw the data away).
_CORRUPTION_CODES = frozenset({sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB})

_VALID_SYNCHRONOUS = frozenset({"OFF", "NORMAL", "FULL", "EXTRA"})


class StoredMessage(NamedTuple):
    id: int
    topic: str
    payload: str


class OutboxCorruptError(sqlite3.DatabaseError):
    """Raised when the outbox fails its integrity check."""


class Outbox:
    def __init__(self, path: Path, *, max_rows: int, synchronous: str = "NORMAL") -> None:
        synchronous = synchronous.upper()
        if synchronous not in _VALID_SYNCHRONOUS:
            raise ValueError(f"synchronous must be one of {sorted(_VALID_SYNCHRONOUS)}")
        self._path = path
        self._max_rows = max_rows
        self._synchronous = synchronous
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------ lifecycle

    def open(self) -> None:
        """Open (creating if needed) the database, quarantining it if corrupt."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            try:
                self._conn = self._connect()
            except sqlite3.DatabaseError as exc:
                if not _is_corruption(exc):
                    raise
                log.error(
                    "Outbox %s is corrupt (%s); quarantining and starting fresh", self._path, exc
                )
                self._quarantine()
                self._conn = self._connect()
        log.info("Outbox opened at %s (%d messages pending)", self._path, self.count())

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------------ operations

    def add_many(self, messages: Iterable[OutboxMessage]) -> int:
        rows = [(m.created_at, m.topic, m.payload) for m in messages]
        if rows:
            self._write("INSERT INTO outbox (created_at, topic, payload) VALUES (?, ?, ?)", rows)
        return len(rows)

    def fetch_batch(self, limit: int) -> list[StoredMessage]:
        """Return up to ``limit`` of the oldest pending messages (not removed)."""
        with self._lock:
            cursor = self._guarded(
                lambda conn: conn.execute(
                    "SELECT id, topic, payload FROM outbox ORDER BY id LIMIT ?", (limit,)
                )
            )
            return [StoredMessage(*row) for row in cursor.fetchall()]

    def delete(self, ids: Sequence[int]) -> None:
        if ids:
            self._write("DELETE FROM outbox WHERE id = ?", [(i,) for i in ids])

    def count(self) -> int:
        with self._lock:
            row = self._guarded(lambda conn: conn.execute("SELECT COUNT(*) FROM outbox")).fetchone()
            return int(row[0])

    def prune(self) -> int:
        """Discard the oldest messages beyond ``max_rows``. Returns the number removed."""
        with self._lock:

            def _prune(conn: sqlite3.Connection) -> int:
                with conn:
                    cursor = conn.execute(
                        "DELETE FROM outbox WHERE id < "
                        "(SELECT id FROM outbox ORDER BY id DESC LIMIT 1 OFFSET ?)",
                        (self._max_rows - 1,),
                    )
                    return cursor.rowcount

            return self._guarded(_prune)

    # ------------------------------------------------------------------ internals

    def _write(self, sql: str, rows: list[tuple]) -> None:
        with self._lock:

            def _execute(conn: sqlite3.Connection) -> None:
                with conn:  # one transaction (one fsync) per batch
                    conn.executemany(sql, rows)

            self._guarded(_execute)

    def _guarded(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        """Run ``operation(conn)``; on file corruption, quarantine and reopen, then re-raise.

        Re-raising lets the caller's supervisor retry against the fresh database.
        Caller must hold ``self._lock``.
        """
        if self._conn is None:
            raise RuntimeError("Outbox is not open")
        try:
            return operation(self._conn)
        except sqlite3.DatabaseError as exc:
            if _is_corruption(exc):
                log.error("Outbox corruption detected (%s); quarantining and reopening", exc)
                self._conn.close()
                self._quarantine()
                self._conn = self._connect()
            raise

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30, check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f"PRAGMA synchronous={self._synchronous}")
            conn.execute(_SCHEMA)
            conn.commit()
            result = conn.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok":
                raise OutboxCorruptError(f"quick_check failed: {result}")
        except Exception:
            conn.close()
            raise
        return conn

    def _quarantine(self) -> None:
        stamp = time.strftime("%Y%m%dT%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            src = self._path.with_name(self._path.name + suffix)
            if src.exists():
                dest = src.with_name(f"{src.name}.corrupt-{stamp}")
                src.replace(dest)
                log.warning("Moved %s -> %s", src, dest)


def _is_corruption(exc: sqlite3.DatabaseError) -> bool:
    return isinstance(exc, OutboxCorruptError) or (
        getattr(exc, "sqlite_errorcode", None) in _CORRUPTION_CODES
    )
