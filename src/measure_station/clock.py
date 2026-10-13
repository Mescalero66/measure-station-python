"""Timekeeping for a device without a real-time clock.

The Pi has no battery-backed RTC. After a boot without NTP, the wall clock is only
a guess (restored from the last shutdown), and it jumps when NTP finally syncs.

Every reading therefore also records the *monotonic boot clock*, which counts
seconds since boot and never jumps. Once the wall clock is synchronised we know
``offset = wall_time - boot_clock`` for this boot, and any reading captured earlier
in the same boot can be given an accurate timestamp: ``boot_clock_at_capture + offset``.
The boot clock restarts at every boot, so readings are tagged with the kernel's
``boot_id`` to make sure an offset is only ever applied to readings from its own boot.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger(__name__)

_BOOT_ID_FILE = Path("/proc/sys/kernel/random/boot_id")
# Created by systemd-timesyncd on its first successful synchronisation after boot.
_TIMESYNC_FLAG = Path("/run/systemd/timesync/synchronized")


class TimeQuality(StrEnum):
    """How a reading's timestamp was obtained."""

    NTP = "ntp"  # captured while the wall clock was synchronised
    CORRECTED = "corrected"  # captured before sync, back-dated from the boot clock
    PENDING = "pending"  # captured before sync, not (yet) corrected


def boot_clock() -> float:
    """Seconds since boot; immune to wall-clock steps.

    CLOCK_BOOTTIME (Linux) also counts time spent suspended. It is system-wide, so
    values stay comparable across service restarts within the same boot.
    """
    clock_id = getattr(time, "CLOCK_BOOTTIME", None)
    if clock_id is not None:
        return time.clock_gettime(clock_id)
    return time.monotonic()


def read_boot_id() -> str:
    """Kernel-assigned identifier, unique to this boot."""
    try:
        return _BOOT_ID_FILE.read_text().strip()
    except OSError:
        # Not Linux (development): treat each process as its own "boot".
        return f"process-{uuid.uuid4()}"


def system_clock_synchronised() -> bool:
    """True once the kernel clock has been synchronised by NTP since boot."""
    if sys.platform != "linux":
        return True  # development machines are assumed to have a good clock
    if _TIMESYNC_FLAG.exists():
        return True
    # Fallback for other NTP daemons (e.g. chrony): ask systemd, which checks the
    # kernel's synchronisation status.
    if shutil.which("timedatectl"):
        try:
            result = subprocess.run(
                ["timedatectl", "show", "--property=NTPSynchronized", "--value"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return result.stdout.strip() == "yes"
        except (OSError, subprocess.SubprocessError):
            pass
    return False


class _SyncAnchor(NamedTuple):
    offset_s: float  # wall_time - boot_clock, measured just after sync was detected
    synced_at_boot_s: float  # boot clock value when sync was detected


class ClockState:
    """Tracks clock synchronisation for this boot and resolves reading timestamps.

    Thread-safe: the anchor is set once, as a single immutable tuple.
    """

    def __init__(
        self,
        *,
        boot_id: str | None = None,
        sync_check: Callable[[], bool] = system_clock_synchronised,
        boot_clock_fn: Callable[[], float] = boot_clock,
        wall_clock_fn: Callable[[], float] = time.time,
    ) -> None:
        self.boot_id = boot_id or read_boot_id()
        self._sync_check = sync_check
        self._boot_clock = boot_clock_fn
        self._wall_clock = wall_clock_fn
        self._anchor: _SyncAnchor | None = None
        self._lock = threading.Lock()

    @property
    def synced(self) -> bool:
        return self._anchor is not None

    @property
    def offset_s(self) -> float | None:
        anchor = self._anchor
        return anchor.offset_s if anchor else None

    def now_boot_s(self) -> float:
        return self._boot_clock()

    def poll(self) -> bool:
        """Check synchronisation (blocking). Returns True only on the transition to synced."""
        if self._anchor is not None or not self._sync_check():
            return False
        with self._lock:
            if self._anchor is not None:
                return False
            boot_now = self._boot_clock()
            self._anchor = _SyncAnchor(self._wall_clock() - boot_now, boot_now)
        log.info("System clock is synchronised (boot clock offset %.3f s)", self._anchor.offset_s)
        return True

    def resolve(self, wall_ts: float, boot_ts: float) -> tuple[float, TimeQuality]:
        """Best timestamp for a reading captured in *this* boot at the given clock values."""
        anchor = self._anchor
        if anchor is None:
            return wall_ts, TimeQuality.PENDING
        if boot_ts >= anchor.synced_at_boot_s:
            return wall_ts, TimeQuality.NTP
        # Captured before we knew the clock was good (possibly before NTP stepped it).
        return boot_ts + anchor.offset_s, TimeQuality.CORRECTED
