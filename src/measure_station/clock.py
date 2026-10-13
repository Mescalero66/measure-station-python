"""System clock trust check.

The Raspberry Pi has no battery-backed RTC. After a boot without network access the
clock is restored from the last shutdown (fake-hwclock) and can be far behind until
NTP syncs. Readings carry a flag so downstream consumers can treat them with care.
"""

from __future__ import annotations

from pathlib import Path

# Created by systemd-timesyncd on first successful synchronisation.
_TIMESYNC_FLAG = Path("/run/systemd/timesync/synchronized")

_synced = False


def clock_is_synchronised() -> bool:
    """Return True once the system clock has been NTP-synchronised since boot."""
    global _synced
    if not _synced:
        _synced = _TIMESYNC_FLAG.exists()
    return _synced
