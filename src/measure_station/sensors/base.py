"""Sensor driver interface and the async polling loop that drives it."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from abc import ABC, abstractmethod
from typing import Any

from measure_station.models import Reading
from measure_station.supervisor import Heartbeat

log = logging.getLogger(__name__)


class Sensor(ABC):
    """A polled sensor.

    Drivers implement blocking methods; :func:`poll_sensor` runs them in worker
    threads so a slow bus never blocks the event loop. Extra keys in a sensor's
    ``sysconfig.SENSORS`` entry are passed to the subclass constructor as kwargs.

    If ``read()`` raises, the supervisor calls ``close()`` and later ``open()`` again,
    so drivers should (re)acquire all hardware handles in ``open()``.
    """

    def __init__(self, name: str, interval_s: float) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        self.name = name
        self.interval_s = float(interval_s)

    def open(self) -> None:  # noqa: B027 - optional hook
        """Acquire hardware resources."""

    @abstractmethod
    def read(self) -> dict[str, Any]:
        """Take one measurement and return ``{field_name: value}``."""

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release hardware resources. Must be safe to call more than once."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r}, interval_s={self.interval_s})"


async def poll_sensor(
    sensor: Sensor,
    queue: asyncio.Queue[Reading],
    *,
    read_timeout_s: float,
    heartbeat: Heartbeat,
) -> None:
    """Open ``sensor`` and read it every ``interval_s`` until cancelled or it fails.

    Note: a timed-out read cannot interrupt the worker thread; the thread is abandoned
    and finishes on its own. If threads keep leaking, the pool eventually starves,
    heartbeats go stale and the watchdog restarts the whole service.
    """
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(read_timeout_s):
        await asyncio.to_thread(sensor.open)
    log.info("Sensor %r started", sensor)
    try:
        next_due = loop.time()
        while True:
            heartbeat.beat()
            async with asyncio.timeout(read_timeout_s):
                values = await asyncio.to_thread(sensor.read)
            await queue.put(Reading(source=sensor.name, values=values))

            # Schedule against a fixed cadence rather than "sleep(interval)" so read
            # time doesn't accumulate as drift; if we fall behind, re-align instead of
            # bursting to catch up.
            next_due += sensor.interval_s
            delay = next_due - loop.time()
            if delay < 0:
                next_due = loop.time()
                delay = 0
            await asyncio.sleep(delay)
    finally:
        with contextlib.suppress(Exception):
            sensor.close()
