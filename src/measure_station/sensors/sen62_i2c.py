"""Sensirion SEN62 particulate matter, temperature and humidity sensor on I2C (SEN62-SIN-T).

Station adapter for the verbatim driver in ``measure_station.drivers.sen62``. See
docs/sensors/particulate-sensor.md for wiring and settings.

Values:
    pm1_ugm3:       PM1.0 mass concentration (particles 0.3-1.0 µm), in µg/m³, in steps
                    of 0.1. Specified for 0-1000 µg/m³.
    pm25_ugm3:      PM2.5 (0.3-2.5 µm), likewise. Precision ±(5 µg/m³ + 5 %) up to
                    100 µg/m³, ±10 % above.
    pm4_ugm3:       PM4.0 (0.3-4.0 µm), likewise. Precision ±25 µg/m³ up to 100 µg/m³.
    pm10_ugm3:      PM10 (0.3-10 µm), likewise.
    temperature_c:  air temperature, in °C, compensated for the module's own heat;
                    ±0.45 °C typical.
    humidity_pct:   relative humidity, 0-100 %, compensated likewise; ±4.5 %RH typical.
    Each is None (null) if the sensor reports it as unknown.

Meta (only when the PM values may be wrong; otherwise no meta):
    pm:  "fan_speed" = the fan is more than 10 % off its target speed.

Settling: the PM values need 30 s after the measurement starts (its start-up time), so
the first read after open() waits until then: about 30 s instead of about 0.1 s. Fan
cleaning: every fan_cleaning_interval_h, one read first blows the dust out of the fan
(full speed for 10 s) and restarts it, then waits likewise: about 42 s. Both still
return a normal, settled reading.

Requires: pip install -e ".[sen62]"   (smbus2)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from measure_station.sensors.base import Sample, Sensor

log = logging.getLogger(__name__)

# A new result every second (the first about 1.1 s after starting); wait a little longer.
_READY_TIMEOUT_S = 3.0
_READY_POLL_S = 0.2
_WARMUP_S = 30.0  # PM start-up time (datasheet Table 2); no reading before it
# The extra time cleaning takes, before the warm-up: stop (1.4 s) + cleaning (10 s) + start.
_CLEANING_S = 1.4 + 10 + 0.1 + 3

FAN_SPEED = "fan_speed"  # meta "pm" code


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


class Sen62Sensor(Sensor):
    """The driver is asyncio-based, but its I2C transfers block. The station runs
    ``open()`` and ``read()`` in worker threads, so each one runs the driver's coroutines
    to completion on its own short-lived event loop there, off the station's loop."""

    def __init__(
        self,
        name: str,
        interval_s: float,
        i2c_bus: int = 1,
        fan_cleaning_interval_h: float | None = 24,
    ) -> None:
        super().__init__(name, interval_s)
        if fan_cleaning_interval_h is not None and fan_cleaning_interval_h <= 0:
            raise ValueError("fan_cleaning_interval_h must be more than 0, or None")
        self._i2c_bus = i2c_bus
        self._cleaning_interval_s = (
            None if fan_cleaning_interval_h is None else fan_cleaning_interval_h * 3600
        )
        self._sen: Any = None
        self._started = 0.0  # time.monotonic() when the measurement started
        self._cleaned = 0.0  # time.monotonic() of the last cleaning (or the start)
        self._clean_next = False  # whether the next read cleans the fan

    def read_timeout_s(self, default_s: float) -> float:
        # The read waits out the warm-up after a start: open()'s, or the cleaning's own.
        if self._clean_next:
            return default_s + _CLEANING_S + _WARMUP_S
        return default_s + max(0.0, self._started + _WARMUP_S - time.monotonic())

    def open(self) -> None:
        # Imported here: the driver imports smbus2 at the top of the module.
        from measure_station.drivers.sen62 import SEN62

        self._sen = SEN62(self._i2c_bus)
        asyncio.run(self._start())

    async def _start(self) -> None:
        sen = self._sen
        await sen.stop_measurement()  # in case it's still measuring; raises if it doesn't answer
        await sen.device_reset()  # like a power cycle: clears its sticky error flags
        # Continuous: it has no single-shot mode, and its PM values need 30 s to settle.
        await sen.start_continuous_measurement()
        self._started = self._cleaned = time.monotonic()  # cleaning is counted from here
        self._clean_next = False

    def read(self) -> Sample:
        if self._clean_next:
            log.info("%s: cleaning the SEN62's fan (this read takes about 42 s)", self.name)
            asyncio.run(self._clean_fan())
        m, status = asyncio.run(self._read())
        # Decided now, so the station knows the next read's timeout before it starts.
        self._clean_next = (
            self._cleaning_interval_s is not None
            and time.monotonic() - self._cleaned >= self._cleaning_interval_s
        )
        errors = [
            name
            for name, flag in (
                ("fan", status.fan_error),
                ("PM", status.pm_error),
                ("RH&T", status.rht_error),
            )
            if flag
        ]
        if errors:
            # Its values are likely wrong. Raising re-opens it, and the reset clears the
            # flags: a glitch recovers, a real fault keeps failing (and is logged).
            raise OSError(
                f"SEN62 reports a {' and '.join(errors)} error (status 0x{status.raw:08X})"
            )
        meta = {"pm": FAN_SPEED} if status.fan_speed_warning else {}
        return Sample(
            values={
                "pm1_ugm3": _round(m.pm1_0, 1),
                "pm25_ugm3": _round(m.pm2_5, 1),
                "pm4_ugm3": _round(m.pm4_0, 1),
                "pm10_ugm3": _round(m.pm10_0, 1),
                "temperature_c": _round(m.temperature, 2),
                "humidity_pct": _round(m.humidity, 1),
            },
            meta=meta,
        )

    async def _clean_fan(self) -> None:
        """Blow the dust out, then restart (the read then waits out the warm-up)."""
        sen = self._sen
        await sen.stop_measurement()  # cleaning is idle mode only
        await sen.start_fan_cleaning()  # full speed for 10 s; returns when it's done
        await sen.start_continuous_measurement()
        self._started = self._cleaned = time.monotonic()

    async def _read(self) -> tuple[Any, Any]:
        """The newest settled result, and the device status after it."""
        settling_s = self._started + _WARMUP_S - time.monotonic()
        if settling_s > 0:  # only after a start: the first read, or a cleaning
            await asyncio.sleep(settling_s)
        deadline = time.monotonic() + _READY_TIMEOUT_S
        while not await self._sen.get_data_ready():
            if time.monotonic() > deadline:
                # Not measuring, e.g. reset to idle by a brown-out. Raising re-opens it.
                raise OSError(f"SEN62: no measurement ready within {_READY_TIMEOUT_S:g} s")
            await asyncio.sleep(_READY_POLL_S)
        return await self._sen.read_measured_values(), await self._sen.read_device_status()

    def close(self) -> None:
        # Runs on the station's event loop thread, where asyncio.run() isn't allowed, so
        # it doesn't stop the measurement (the fan keeps running). open() stops it first.
        if self._sen is not None:
            self._sen.close()
            self._sen = None
