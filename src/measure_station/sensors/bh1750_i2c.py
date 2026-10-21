"""ROHM BH1750 ambient light sensor on I2C (e.g. Adafruit 4681, GY-302).

Station adapter for the verbatim driver in ``measure_station.drivers.bh1750``. See
docs/sensors/light-sensor.md for wiring and settings.

Values:
    illuminance_lux:  illuminance, with a spectral response close to the human eye,
                      about 0.1 to 121000 lx (divided by transmittance).

Meta:
    range:  which measurement range the reading came from (auto-ranging):
            "hi" = steps of about 1.9 lx, up to about 121000 lx (brighter light reads
                   as that maximum);
            "lo" = steps of about 0.11 lx, used below 5000 lx.

Requires: pip install -e ".[bh1750]"   (smbus2)
"""

from __future__ import annotations

import asyncio
from typing import Any

from measure_station.sensors.base import Sample, Sensor

_ADDRESSES = (0x23, 0x5C)  # set by the ADDR pin: low or high (p10)

# Auto-ranging: the chip counts up to 65535, so a range's step sets its maximum. Each
# range is (resolution code, MTreg): MTreg is the exposure time (p11), 31-254.
_HI = (0x00, 31)  # H-resolution, shortest exposure: 1.86 lx steps, up to ~121500 lx
_LO = (0x01, 254)  # H-resolution mode 2, longest exposure: 0.11 lx steps, up to ~7400 lx
_LO_BELOW_LUX = 5000  # re-measure in the lo range below this; well under its maximum
_FULL_SCALE = 0xFFFF


class Bh1750Sensor(Sensor):
    """The driver is asyncio-based, but its I2C transfers block. The station runs
    ``open()`` and ``read()`` in worker threads, so each one runs the driver's coroutines
    to completion on its own short-lived event loop there, off the station's loop."""

    def __init__(
        self,
        name: str,
        interval_s: float,
        i2c_bus: int = 1,
        address: int = 0x23,
        transmittance: float = 1.0,
    ) -> None:
        super().__init__(name, interval_s)
        if address not in _ADDRESSES:
            raise ValueError("address must be 0x23 (ADDR low) or 0x5C (ADDR high)")
        if not 0 < transmittance <= 1:
            raise ValueError("transmittance must be more than 0 and at most 1")
        self._i2c_bus = i2c_bus
        self._address = address
        self._transmittance = transmittance
        self._bh: Any = None

    def open(self) -> None:
        # Imported here: the driver imports smbus2 at the top of the module.
        from measure_station.drivers.bh1750 import BH1750

        self._bh = BH1750(self._i2c_bus, self._address, self._transmittance)
        # The chip has no ID register: this raises if nothing acknowledges the address.
        asyncio.run(self._bh.power_on())

    def read(self) -> Sample:
        lux, range_ = asyncio.run(self._auto_range())
        digits = 2 if range_ == "lo" else 1  # the range's step size
        return Sample({"illuminance_lux": round(lux, digits)}, meta={"range": range_})

    async def _auto_range(self) -> tuple[float, str]:
        lux, _ = await self._measure(*_HI)
        if lux >= _LO_BELOW_LUX:
            return lux, "hi"
        lo_lux, lo_full_scale = await self._measure(*_LO)
        if lo_full_scale:
            # It brightened during the ~0.7 s between the two measurements.
            return lux, "hi"
        return lo_lux, "lo"

    async def _measure(self, resolution: int, mtreg: int) -> tuple[float, bool]:
        """One-time measurement (up to 663 ms at MTreg 254); the chip then powers itself
        down. Returns (lux, whether the count was at full scale)."""
        bh = self._bh
        await bh.power_on()
        # Sent every time, which also undoes a brown-out's reset of MTreg (the driver
        # would otherwise keep scaling with the value it last set).
        await bh.set_mtreg(mtreg)
        lux = await bh.measure(resolution)
        return lux, round(lux / bh.lux_per_count(resolution)) >= _FULL_SCALE

    def close(self) -> None:
        # Runs on the station's event loop thread, where asyncio.run() isn't allowed. The
        # chip already powers itself down after each measurement.
        if self._bh is not None:
            self._bh.close()
            self._bh = None
