"""Periodic station self-report, published like any other reading."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import time
from pathlib import Path

from measure_station.models import Reading
from measure_station.storage import Outbox
from measure_station.supervisor import Heartbeat, Supervisor


async def report_health(
    queue: asyncio.Queue[Reading],
    outbox: Outbox,
    supervisor: Supervisor,
    *,
    source: str,
    interval_s: float,
    data_dir: Path,
    heartbeat: Heartbeat,
) -> None:
    started = time.monotonic()
    while True:
        heartbeat.beat()
        values: dict[str, float | int] = {
            "uptime_s": round(time.monotonic() - started),
            "outbox_backlog": await asyncio.to_thread(outbox.count),
            "queue_depth": queue.qsize(),
            "task_restarts": supervisor.total_restarts(),
        }
        values.update(await asyncio.to_thread(system_stats, data_dir))
        await queue.put(Reading(source=source, values=values))
        await asyncio.sleep(interval_s)


def system_stats(data_dir: Path) -> dict[str, float]:
    """Host metrics; each is omitted if unavailable on this platform."""
    stats: dict[str, float] = {}
    with contextlib.suppress(OSError, AttributeError):
        stats["load_1m"] = round(os.getloadavg()[0], 2)
    with contextlib.suppress(OSError):
        stats["disk_free_mb"] = round(shutil.disk_usage(data_dir).free / 1_000_000, 1)
    with contextlib.suppress(OSError, ValueError, IndexError):
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                stats["mem_available_mb"] = round(int(line.split()[1]) / 1024, 1)
                break
    return stats
