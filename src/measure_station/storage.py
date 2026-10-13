"""Durable local outbox (SQLite) for store-and-forward MQTT delivery.

Every reading is written here before any publish attempt; a row is deleted only once
the broker has acknowledged it. The outbox therefore survives network outages,
process crashes and reboots.

Readings captured before the system clock was NTP-synchronised are stored with
``ts_quality='pending'`` and held back from publishing until
:meth:`Outbox.apply_time_correction` back-dates them (see :mod:`measure_station.clock`),
or until they can no longer be corrected.

All methods are blocking and thread-safe; call them via ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import TypeVar

from measure_station.clock import ClockState, TimeQuality
from measure_station.models import Reading, StoredReading, encode_values

log = logging.getLogger(__name__)

_T = TypeVar("_T")

# No AUTOINCREMENT: it writes an extra page (sqlite_sequence) on every insert, ~1/3 of
# all outbox writes. A plain rowid is always max(id)+1, so new rows still sort after
# every pending row; ids may be reused once deleted, which nothing here relies on.
_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS outbox (
        id          INTEGER PRIMARY KEY,
        source      TEXT NOT NULL,
        values_json TEXT NOT NULL,
        ts          REAL NOT NULL,  -- best known wall-clock time (unix seconds)
        ts_quality  TEXT NOT NULL,  -- 'ntp' | 'corrected' | 'pending'
        boot_id     TEXT NOT NULL,  -- kernel boot the reading was captured in
        boot_ts     REAL NOT NULL   -- seconds since that boot at capture
    )
    """,
    # Small partial index: only rows awaiting a time correction are indexed.
    """
    CREATE INDEX IF NOT EXISTS outbox_awaiting_time
        ON outbox (boot_id) WHERE ts_quality = 'pending'
    """,
    # uptime_s of the last recorded reading per MQ board sensor (see SeenMarker).
    """
    CREATE TABLE IF NOT EXISTS mqboard_last_seen (
        sensor   TEXT    PRIMARY KEY,
        uptime_s INTEGER NOT NULL
    ) WITHOUT ROWID
    """,
)

# Error codes meaning the file itself is damaged (as opposed to e.g. a full disk,
# where we must NOT throw the data away).
_CORRUPTION_CODES = frozenset({sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB})

_VALID_SYNCHRONOUS = frozenset({"OFF", "NORMAL", "FULL", "EXTRA"})


class OutboxCorruptError(sqlite3.DatabaseError):
    """Raised when the outbox fails its integrity check."""


class Outbox:
    def __init__(
        self,
        path: Path,
        *,
        clock: ClockState,
        max_rows: int,
        max_time_hold_s: float,
        synchronous: str = "NORMAL",
    ) -> None:
        synchronous = synchronous.upper()
        if synchronous not in _VALID_SYNCHRONOUS:
            raise ValueError(f"synchronous must be one of {sorted(_VALID_SYNCHRONOUS)}")
        self._path = path
        self._clock = clock
        self._max_rows = max_rows
        self._max_time_hold_s = max_time_hold_s
        self._synchronous = synchronous
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------ lifecycle

    def open(self) -> None:
        """Open (creating if needed) the database; set it aside if corrupt."""
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
        log.info("Outbox opened at %s (%d readings pending)", self._path, self.count())

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------------ operations

    def add_many(self, readings: Iterable[Reading]) -> int:
        """Store readings, and their :class:`SeenMarker`s, in one transaction."""
        with self._lock:
            # Resolve timestamps under the lock, so this can't interleave with
            # apply_time_correction and leave a pre-sync reading uncorrected.
            rows = []
            seen: dict[str, int] = {}  # latest marker per sensor wins
            for r in readings:
                ts, quality = self._clock.resolve(r.timestamp, r.boot_ts)
                rows.append(
                    (r.source, encode_values(r.values), ts, quality, self._clock.boot_id, r.boot_ts)
                )
                if r.seen is not None:
                    seen[r.seen.sensor] = r.seen.uptime_s
            if not rows:
                return 0

            def _insert(conn: sqlite3.Connection) -> None:
                with conn:
                    conn.executemany(
                        "INSERT INTO outbox "
                        "(source, values_json, ts, ts_quality, boot_id, boot_ts) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        rows,
                    )
                    conn.executemany(
                        "INSERT INTO mqboard_last_seen (sensor, uptime_s) VALUES (?, ?) "
                        "ON CONFLICT (sensor) DO UPDATE SET uptime_s = excluded.uptime_s",
                        seen.items(),
                    )

            self._guarded(_insert)
            return len(rows)

    def load_mqboard_last_seen(self) -> dict[str, int]:
        """``{sensor: uptime_s}`` of the last recorded reading per MQ board sensor."""
        with self._lock:
            cursor = self._guarded(
                lambda conn: conn.execute("SELECT sensor, uptime_s FROM mqboard_last_seen")
            )
            return dict(cursor.fetchall())

    def fetch_batch(self, limit: int) -> list[StoredReading]:
        """Return up to ``limit`` of the oldest *publishable* readings (not removed).

        Held back: readings from this boot still awaiting a time correction, unless
        they have waited longer than ``max_time_hold_s``. Readings from earlier boots
        can never be corrected, so they are released immediately.
        """
        release_before = self._clock.now_boot_s() - self._max_time_hold_s
        with self._lock:
            cursor = self._guarded(
                lambda conn: conn.execute(
                    "SELECT id, source, values_json, ts, ts_quality, boot_id, boot_ts "
                    "FROM outbox "
                    "WHERE ts_quality != 'pending' OR boot_id != ? OR boot_ts <= ? "
                    "ORDER BY id LIMIT ?",
                    (self._clock.boot_id, release_before, limit),
                )
            )
            return [StoredReading(*row) for row in cursor.fetchall()]

    def apply_time_correction(self) -> int:
        """Back-date this boot's pending readings now that the clock is synchronised.

        Returns the number of readings corrected. Idempotent.
        """
        offset = self._clock.offset_s
        if offset is None:
            return 0
        with self._lock:

            def _correct(conn: sqlite3.Connection) -> int:
                with conn:
                    return conn.execute(
                        "UPDATE outbox SET ts = boot_ts + ?, ts_quality = ? "
                        "WHERE ts_quality = 'pending' AND boot_id = ?",
                        (offset, TimeQuality.CORRECTED.value, self._clock.boot_id),
                    ).rowcount

            return self._guarded(_correct)

    def delete(self, ids: Sequence[int]) -> None:
        if ids:
            with self._lock:
                self._execute_many("DELETE FROM outbox WHERE id = ?", [(i,) for i in ids])

    def count(self) -> int:
        return self._scalar("SELECT COUNT(*) FROM outbox")

    def count_awaiting_time(self) -> int:
        """Readings still held for a time correction (any boot)."""
        return self._scalar("SELECT COUNT(*) FROM outbox WHERE ts_quality = 'pending'")

    def prune(self) -> int:
        """Discard the oldest readings beyond ``max_rows``. Returns the number removed."""
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

    def _scalar(self, sql: str) -> int:
        with self._lock:
            return int(self._guarded(lambda conn: conn.execute(sql)).fetchone()[0])

    def _execute_many(self, sql: str, rows: list[tuple]) -> None:
        """Run one statement for many rows in a single transaction. Caller holds the lock."""

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
            result = conn.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok":
                raise OutboxCorruptError(f"quick_check failed: {result}")
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.commit()
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
