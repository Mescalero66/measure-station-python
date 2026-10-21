"""Sensirion SCD41 CO2, temperature and humidity sensor on I2C (e.g. Adafruit SCD-41).

Station adapter for the verbatim driver in ``measure_station.drivers.scd41``. See
docs/sensors/co2-sensor.md for wiring and calibration.

Values:
    co2_ppm:        CO2 concentration, measured (photoacoustic NDIR). Specified for
                    400-5000 ppm; accuracy ±(50 ppm + 2.5 %) up to 1000 ppm.
    temperature_c:  temperature at the sensor, in °C, after its temperature offset
                    (temperature_offset_c) for self-heating.
    humidity_pct:   relative humidity at the sensor, 0-100 %.

Meta:
    pressure_hpa:   the ambient pressure the sensor used to compensate this CO2
                    reading, in whole hPa (the sensor's resolution), 700-1200.
    comp_src:       where that pressure came from:
                    "hpa" = live, the latest pressure_hpa of the pressure_source sensor;
                    "alt" = altitude, the standard-atmosphere pressure at altitude_m
                        (no pressure_source, or its value is missing or out of date).

The settings (temperature offset, self-calibration, pressure) are applied in the
sensor's RAM every time it starts, never written to its EEPROM, so sysconfig.py stays
the only source of truth and the EEPROM (about 2000 writes) never wears.

Requires: pip install -e ".[scd41]"   (smbus2)
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from measure_station.sensors.base import Sample, Sensor

# Periodic mode gives a new result every 5 s; wait a little longer than that for one.
_READY_TIMEOUT_S = 7.0
_READY_POLL_S = 0.5

LIVE, ALTITUDE = "hpa", "alt"  # comp_src codes
_PRESSURE_RANGE_HPA = (700, 1200)  # what the sensor accepts (§3.7)
_ALTITUDE_RANGE_M = (0, 3000)


def altitude_pressure_hpa(altitude_m: float) -> float:
    """Standard-atmosphere pressure at ``altitude_m`` (ISA, from sea-level 1013.25 hPa)."""
    return 1013.25 * (1 - 2.25577e-5 * altitude_m) ** 5.25588


class Scd41Sensor(Sensor):
    """The driver is asyncio-based, but its I2C transfers block. The station runs
    ``open()`` and ``read()`` in worker threads, so each one runs the driver's coroutines
    to completion on its own short-lived event loop there, off the station's loop."""

    def __init__(
        self,
        name: str,
        interval_s: float,
        i2c_bus: int = 1,
        altitude_m: float = 0,
        pressure_source: str | None = None,
        temperature_offset_c: float = 4.0,
        asc_enabled: bool = True,
    ) -> None:
        super().__init__(name, interval_s)
        low, high = _ALTITUDE_RANGE_M
        if not low <= altitude_m <= high:
            raise ValueError(f"altitude_m must be {low} to {high}")
        if temperature_offset_c < 0:
            raise ValueError("temperature_offset_c must be >= 0")
        self._i2c_bus = i2c_bus
        self._altitude_hpa = altitude_pressure_hpa(altitude_m)
        self._pressure_source = pressure_source
        self._temperature_offset_c = temperature_offset_c
        self._asc_enabled = bool(asc_enabled)
        self._scd: Any = None
        self._applied: tuple[int, str] | None = None  # (hPa, comp_src) the sensor is using

    def uses_sources(self) -> list[str]:
        return [self._pressure_source] if self._pressure_source else []

    def open(self) -> None:
        # Imported here: the driver imports smbus2 at the top of the module.
        from measure_station.drivers.scd41 import SCD41

        self._scd = SCD41(self._i2c_bus)
        asyncio.run(self._start())

    async def _start(self) -> None:
        scd = self._scd
        await scd.wake_up()  # in case it was powered down
        await scd.stop_periodic_measurement()  # raises if the sensor doesn't answer
        await scd.reinit()  # discard any unsaved settings, from the EEPROM
        # These are only accepted while idle. Written only if they differ (RAM only).
        if abs(await scd.get_temperature_offset() - self._temperature_offset_c) > 0.005:
            await scd.set_temperature_offset(self._temperature_offset_c)
        if await scd.get_automatic_self_calibration_enabled() != self._asc_enabled:
            await scd.set_automatic_self_calibration_enabled(self._asc_enabled)
        await self._apply_pressure()
        # Periodic mode: the mode the accuracy and self-calibration are specified for.
        await scd.start_periodic_measurement()

    def read(self) -> Sample:
        measurement, (pressure_hpa, source) = asyncio.run(self._read())
        return Sample(
            values={
                "co2_ppm": measurement.co2,
                "temperature_c": round(measurement.temperature, 2),
                "humidity_pct": round(measurement.humidity, 1),
            },
            meta={"pressure_hpa": pressure_hpa, "comp_src": source},
        )

    async def _read(self) -> tuple[Any, tuple[int, str]]:
        """The newest result, with the compensation it was measured with. Then updates
        the pressure: a new pressure only affects the sensor's following measurements."""
        deadline = time.monotonic() + _READY_TIMEOUT_S
        while not await self._scd.get_data_ready_status():
            if time.monotonic() > deadline:
                # Not measuring, e.g. reset by a brown-out. Raising re-opens it.
                raise OSError(f"SCD41: no measurement ready within {_READY_TIMEOUT_S:g} s")
            await asyncio.sleep(_READY_POLL_S)
        measurement = await self._scd.read_measurement()
        used = self._applied
        await self._apply_pressure()
        return measurement, used

    async def _apply_pressure(self) -> None:
        """Send the live pressure if there is one, else the altitude's (allowed while
        measuring; overrides the sensor's own altitude setting)."""
        pressure_hpa, source = self._altitude_hpa, ALTITUDE
        if self._pressure_source and self.latest_values is not None:
            live = self.latest_values.get(self._pressure_source, "pressure_hpa")
            low, high = _PRESSURE_RANGE_HPA
            if isinstance(live, int | float) and low <= live <= high:
                pressure_hpa, source = live, LIVE
        applied = (round(pressure_hpa), source)  # the sensor takes whole hPa
        await self._scd.set_ambient_pressure(applied[0] * 100)
        self._applied = applied

    def close(self) -> None:
        # Runs on the station's event loop thread, where asyncio.run() isn't allowed,
        # so this doesn't stop the measurement. open() does that first anyway.
        if self._scd is not None:
            self._scd.close()
            self._scd = None
