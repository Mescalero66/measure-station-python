"""Periodic station self-report, published like any other reading."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import time
from pathlib import Path
from typing import Any

from measure_station.clock import ClockState, boot_clock
from measure_station.fan import FanState
from measure_station.models import Reading
from measure_station.storage import Outbox
from measure_station.supervisor import Heartbeat, Supervisor
from measure_station.uart import MqBoardDecoder

# The health report describes the station itself, not a sensor.
SENSOR_TYPE = "station"


async def report_health(
    queue: asyncio.Queue[Reading],
    outbox: Outbox,
    supervisor: Supervisor,
    clock: ClockState,
    mqboard: MqBoardDecoder | None,
    fan: FanState | None,
    *,
    source: str,
    interval_s: float,
    data_dir: Path,
    heartbeat: Heartbeat,
) -> None:
    started = time.monotonic()
    cpu = CpuUsage()
    while True:
        heartbeat.beat()
        values: dict[str, Any] = {
            "uptime_s": round(time.monotonic() - started),
            "outbox_backlog": await asyncio.to_thread(outbox.count),
            "queue_depth": queue.qsize(),
            "task_restarts": supervisor.total_restarts(),
            "clock_synced": clock.synced,
            "awaiting_time_sync": await asyncio.to_thread(outbox.count_awaiting_time),
        }
        values["cpu_usage_pct"] = await asyncio.to_thread(cpu.sample)
        values.update(await asyncio.to_thread(system_stats, data_dir))
        if mqboard is not None:
            for key, value in mqboard.status(boot_clock()).items():
                values[f"mqboard_{key}"] = value
        if fan is not None:
            values.update(fan.health_values())
        await queue.put(Reading(source=source, sensor_type=SENSOR_TYPE, values=values))
        await asyncio.sleep(interval_s)


class CpuUsage:
    """CPU usage (all cores) between one ``sample()`` and the next, from /proc/stat."""

    def __init__(self, stat_file: Path = Path("/proc/stat")) -> None:
        self._stat_file = stat_file
        self._last: tuple[int, int] | None = None  # (busy, total) jiffies

    def sample(self) -> float | None:
        """Percent busy since the previous call: None the first time, or if unavailable."""
        try:
            # "cpu  user nice system idle iowait irq softirq steal guest guest_nice"
            first_line = self._stat_file.read_text().splitlines()[0]
            fields = [int(f) for f in first_line.split()[1:9]]
        except (OSError, ValueError, IndexError):
            return None
        total = sum(fields)
        busy = total - fields[3] - fields[4]  # less idle and iowait
        last, self._last = self._last, (busy, total)
        if last is None or total <= last[1]:
            return None
        return round(100 * (busy - last[0]) / (total - last[1]), 1)


def system_stats(
    data_dir: Path,
    *,
    meminfo: Path = Path("/proc/meminfo"),
    thermal: Path = Path("/sys/class/thermal/thermal_zone0/temp"),
) -> dict[str, float]:
    """Host metrics; each is omitted if unavailable on this platform."""
    stats: dict[str, float] = {}
    with contextlib.suppress(OSError, ValueError):
        stats["cpu_temperature_c"] = round(int(thermal.read_text().strip()) / 1000, 2)
    with contextlib.suppress(OSError, AttributeError):
        stats["load_1m"] = round(os.getloadavg()[0], 2)
    with contextlib.suppress(OSError):
        stats["disk_free_mb"] = round(shutil.disk_usage(data_dir).free / 1_000_000, 1)
    with contextlib.suppress(OSError, ValueError, IndexError, KeyError, ZeroDivisionError):
        kib = {}
        for line in meminfo.read_text().splitlines():
            name, _, rest = line.partition(":")
            if name in ("MemTotal", "MemAvailable"):
                kib[name] = int(rest.split()[0])
        stats["mem_available_mb"] = round(kib["MemAvailable"] / 1024, 1)
        used = kib["MemTotal"] - kib["MemAvailable"]
        stats["ram_usage_pct"] = round(100 * used / kib["MemTotal"], 1)
    return stats
