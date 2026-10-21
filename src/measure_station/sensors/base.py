"""Sensor driver interface and the async polling loop that drives it."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from measure_station.models import Reading
from measure_station.supervisor import Heartbeat

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Sample:
    """What ``Sensor.read()`` returns: one measurement.

    ``values``: what the sensor is there to measure. ``meta``: how the reading was made
    or how far to trust it (compensation inputs, status flags, calibration
    parameters). Both are ``{key: value}`` with ``<quantity>_<unit>`` keys.
    """

    values: Mapping[str, Any]
    meta: Mapping[str, Any] = field(default_factory=dict)


class LatestValues:
    """The latest values of every sensor, in memory, so one sensor can use another's
    readings (e.g. the SCD41 compensating with the barometer's pressure).

    Recorded on the event loop and read from worker threads. Each entry is replaced
    whole, never modified, so readers always see one complete reading.
    """

    def __init__(self) -> None:
        self._latest: dict[str, tuple[float, Mapping[str, Any]]] = {}

    def record(self, source: str, values: Mapping[str, Any], *, valid_for_s: float) -> None:
        self._latest[source] = (time.monotonic() + valid_for_s, values)

    def get(self, source: str, key: str) -> Any:
        """``source``'s latest value for ``key``, or None if missing or out of date."""
        entry = self._latest.get(source)
        if entry is None or time.monotonic() > entry[0]:
            return None
        return entry[1].get(key)


class Sensor(ABC):
    """A polled sensor.

    Drivers implement blocking methods; :func:`poll_sensor` runs them in worker
    threads so a slow bus never blocks the event loop. Extra keys in a sensor's
    ``sysconfig.SENSORS`` entry are passed to the subclass constructor as kwargs.

    If ``open()`` or ``read()`` raises, the supervisor calls ``close()`` and later
    ``open()`` again, so drivers should (re)acquire all hardware handles in ``open()``.
    """

    # The SENSOR_TYPES key, published with every reading. Set by create_sensor().
    sensor_type: str

    def __init__(self, name: str, interval_s: float) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        self.name = name
        self.interval_s = float(interval_s)
        # Shared by every sensor in the station (set by main), None when run alone.
        self.latest_values: LatestValues | None = None

    def uses_sources(self) -> list[str]:
        """Other sensors whose values this one reads, checked at startup."""
        return []

    def open(self) -> None:  # noqa: B027 - optional hook
        """Acquire hardware resources."""

    @abstractmethod
    def read(self) -> Sample:
        """Take one measurement."""

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release hardware resources. Must be safe to call more than once, and after a
        failed or missing ``open()``."""

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
    try:
        # Inside the try: a failed open() may already hold handles (e.g. an open bus).
        async with asyncio.timeout(read_timeout_s):
            await asyncio.to_thread(sensor.open)
        log.info("Sensor %r started", sensor)
        next_due = loop.time()
        while True:
            heartbeat.beat()
            async with asyncio.timeout(read_timeout_s):
                sample = await asyncio.to_thread(sensor.read)
            await queue.put(
                Reading(
                    source=sensor.name,
                    sensor_type=sensor.sensor_type,
                    values=sample.values,
                    meta=sample.meta,
                )
            )
            if sensor.latest_values is not None:
                # Valid until it has missed two of its own readings.
                valid_for_s = 2 * sensor.interval_s + read_timeout_s
                sensor.latest_values.record(sensor.name, sample.values, valid_for_s=valid_for_s)

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
