"""Bosch BMP280 barometric pressure sensor on I2C (e.g. the 6-pin GY-BMP280 board).

Station adapter for the verbatim driver in ``measure_station.drivers.bmp280``. See
docs/sensors/pressure-sensor.md for wiring.

Values:
    pressure_hpa:   absolute air pressure at the station, 300-1100 hPa. Not corrected
                    to sea level.

Meta:
    temperature_c:  the chip's own temperature, which the pressure compensation uses,
                    -40 to 85 °C. It reads slightly above the air temperature
                    because of self-heating.

Requires: pip install -e ".[bmp280]"   (smbus2)
"""

from __future__ import annotations

import asyncio
from typing import Any

from measure_station.sensors.base import Sample, Sensor

_ADDRESSES = (0x76, 0x77)  # set by the SDO pin: low or high (§5.2)


class Bmp280Sensor(Sensor):
    """The driver is asyncio-based, but its I2C transfers block. The station runs
    ``open()`` and ``read()`` in worker threads, so each one runs the driver's coroutines
    to completion on its own short-lived event loop there, off the station's loop."""

    def __init__(
        self,
        name: str,
        interval_s: float,
        i2c_bus: int = 1,
        address: int = 0x76,
    ) -> None:
        super().__init__(name, interval_s)
        if address not in _ADDRESSES:
            raise ValueError("address must be 0x76 (SDO low) or 0x77 (SDO high)")
        self._i2c_bus = i2c_bus
        self._address = address
        self._bmp: Any = None

    def open(self) -> None:
        # Imported here: the driver imports smbus2 at the top of the module.
        from measure_station.drivers.bmp280 import BMP280

        self._bmp = BMP280(self._i2c_bus, self._address)
        # Soft reset, check the chip ID, load the calibration, and select x16 pressure /
        # x2 temperature oversampling ("ultra high resolution"). Leaves it asleep.
        asyncio.run(self._bmp.init())

    def read(self) -> Sample:
        # Forced mode: one measurement (up to 43 ms), then the chip sleeps again.
        measurement = asyncio.run(self._bmp.measure_single_shot())
        if measurement.temperature is None or measurement.pressure is None:
            # Never skipped at the settings from init(), so the chip has been reset (e.g.
            # by a brown-out) or its calibration is bad. Raising re-initialises it.
            raise OSError("BMP280 returned no measurement")
        return Sample(
            values={"pressure_hpa": round(measurement.pressure / 100, 2)},
            # The chip's own temperature, which the pressure compensation uses.
            meta={"temperature_c": round(measurement.temperature, 2)},
        )

    def close(self) -> None:
        if self._bmp is not None:
            self._bmp.close()
            self._bmp = None
