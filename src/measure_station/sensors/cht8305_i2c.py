"""Sensylink CHT8305C temperature and humidity sensor on I2C (DFRobot SEN0546 probe).

Station adapter for the verbatim driver in ``measure_station.drivers.cht8305``. See
docs/sensors/humidity-sensor.md for wiring and settings.

Values:
    temperature_c:  air temperature at the probe, -40 to 125 °C. ±0.3 °C typical
                    (±0.5 °C maximum) from 0 to 50 °C.
    humidity_pct:   relative humidity, 0-100 %. ±2 %RH typical (±3 %RH maximum) at
                    50 %RH; ±3 %RH typical (±5 %RH maximum) from 20 to 80 %RH.

Heater cycle: every heater_interval_h, the chip's heater runs from one reading to the
next, to drive off moisture (it reduces the offset humidity sensors build up in damp
air). The reading that ends it waits until the probe has cooled back to the temperature
it read before heating, so no reading is published warm. Each cycle is logged.

Requires: pip install -e ".[cht8305]"   (smbus2)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from measure_station.sensors.base import Sample, Sensor

log = logging.getLogger(__name__)

# After the heater: measure every _POLL_S (the datasheet allows at most one a second,
# §2) until within _COOLED_C of the temperature before heating, or _COOL_MAX_S passes.
_POLL_S = 2.0
_COOLED_C = 0.1
_COOL_MAX_S = 40.0


class Cht8305Sensor(Sensor):
    """The driver is asyncio-based, but its I2C transfers block. The station runs
    ``open()`` and ``read()`` in worker threads, so each one runs the driver's coroutines
    to completion on its own short-lived event loop there, off the station's loop."""

    def __init__(
        self,
        name: str,
        interval_s: float,
        i2c_bus: int = 1,
        heater_interval_h: float | None = 24,
    ) -> None:
        super().__init__(name, interval_s)
        if heater_interval_h is not None and heater_interval_h <= 0:
            raise ValueError("heater_interval_h must be more than 0, or None")
        self._i2c_bus = i2c_bus
        self._heater_interval_s = None if heater_interval_h is None else heater_interval_h * 3600
        self._cht: Any = None
        self._heat_due = 0.0  # time.monotonic() when the next heater cycle is due
        self._heated_from: float | None = None  # °C before heating, while the heater is on

    def read_timeout_s(self, default_s: float) -> float:
        # The read that ends a heater cycle waits for the probe to cool.
        return default_s + _COOL_MAX_S if self._heated_from is not None else default_s

    def open(self) -> None:
        # Imported here: the driver imports smbus2 at the top of the module.
        from measure_station.drivers.cht8305 import CHT8305

        # Address 0x40: the probe doesn't wire out the address pin (AD0).
        self._cht = CHT8305(self._i2c_bus)
        asyncio.run(self._start())

    async def _start(self) -> None:
        cht = self._cht
        manufacturer = await cht.get_manufacturer_id()
        version = await cht.get_version_id()
        if (manufacturer, version) != (cht.MANUFACTURER, cht.VERSION):
            raise RuntimeError(
                f"no CHT8305 at 0x40 (manufacturer 0x{manufacturer:04X}, version "
                f"0x{version:04X}); a CHT832X probe (green SCL wire) needs a different driver"
            )
        # The defaults are the settings the station uses (temperature and humidity in one
        # conversion, 14-bit, heater off, no clock stretching), and a brown-out restores
        # them too. The reset also ends a heater cycle that a failure interrupted.
        await cht.soft_reset()
        self._heated_from = None
        if self._heater_interval_s is not None:
            self._heat_due = time.monotonic() + self._heater_interval_s

    def read(self) -> Sample:
        return asyncio.run(self._read())

    async def _read(self) -> Sample:
        cht = self._cht
        if self._heated_from is not None:
            measurement = await self._end_heater_cycle(self._heated_from)
        else:
            # One conversion (about 11 ms); the sensor then idles until the next.
            measurement = await cht.read_measurement()
        if self._heater_interval_s is not None and time.monotonic() >= self._heat_due:
            # On until the next reading, which turns it off.
            await cht.set_heater(True)
            self._heated_from = measurement.temperature
            self._heat_due = time.monotonic() + self._heater_interval_s
        return Sample(
            {
                "temperature_c": round(measurement.temperature, 2),
                "humidity_pct": round(measurement.humidity, 1),
            }
        )

    async def _end_heater_cycle(self, before_c: float) -> Any:
        """Turn the heater off and return the first measurement taken once the probe has
        cooled to within _COOLED_C of ``before_c``, or after _COOL_MAX_S anyway (the air
        itself may have warmed). Logs how much the heater warmed the probe."""
        cht = self._cht
        heated = await cht.read_measurement()
        await cht.set_heater(False)
        self._heated_from = None
        rise_c = heated.temperature - before_c
        off = time.monotonic()
        while True:
            await asyncio.sleep(_POLL_S)
            measurement = await cht.read_measurement()
            cooled = measurement.temperature - before_c <= _COOLED_C
            if cooled or time.monotonic() - off >= _COOL_MAX_S:
                break
        waited_s = time.monotonic() - off
        if rise_c < _COOLED_C:
            log.warning(
                "%s: the heater warmed the probe by only %.2f °C; it may only heat "
                "during conversions",
                self.name,
                rise_c,
            )
        elif cooled:
            log.info(
                "%s: heater cycle warmed the probe %.2f °C; cooled after %.0f s",
                self.name,
                rise_c,
                waited_s,
            )
        else:
            log.warning(
                "%s: heater cycle warmed the probe %.2f °C; still %.2f °C above the start "
                "after %.0f s, published anyway",
                self.name,
                rise_c,
                measurement.temperature - before_c,
                waited_s,
            )
        return measurement

    def close(self) -> None:
        # Runs on the station's event loop thread, where asyncio.run() isn't allowed. The
        # sensor already idles between readings. A heater cycle in progress stays on
        # until the next open(), whose reset turns it off.
        if self._cht is not None:
            self._cht.close()
            self._cht = None
