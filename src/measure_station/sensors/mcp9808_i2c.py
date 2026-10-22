"""Microchip MCP9808 precision temperature sensor on I2C (e.g. DFRobot SEN0435).

Station adapter for the verbatim driver in ``measure_station.drivers.mcp9808``. See
docs/sensors/temperature-sensor.md for wiring and settings.

Values:
    temperature_c:  air temperature at the chip, -40 to 125 °C, in steps of 0.0625 °C.
                    ±0.25 °C typical, ±0.5 °C maximum from -20 to 100 °C.

Requires: pip install -e ".[mcp9808]"   (smbus2)
"""

from __future__ import annotations

import asyncio
from typing import Any

from measure_station.sensors.base import Sample, Sensor

_ADDRESSES = range(0x18, 0x20)  # 0x18 + 4·A2 + 2·A1 + A0 (§4.1.4)
_RESOLUTION_C = 0.0625  # the power-on default, so a brown-out can't change it


class Mcp9808Sensor(Sensor):
    """The driver is asyncio-based, but its I2C transfers block. The station runs
    ``open()`` and ``read()`` in worker threads, so each one runs the driver's coroutines
    to completion on its own short-lived event loop there, off the station's loop."""

    def __init__(
        self,
        name: str,
        interval_s: float,
        i2c_bus: int = 1,
        address: int = 0x1F,
    ) -> None:
        super().__init__(name, interval_s)
        if address not in _ADDRESSES:
            raise ValueError("address must be 0x18-0x1F (0x1F with A0-A2 open)")
        self._i2c_bus = i2c_bus
        self._address = address
        self._mcp: Any = None

    def open(self) -> None:
        # Imported here: the driver imports smbus2 at the top of the module.
        from measure_station.drivers.mcp9808 import MCP9808

        self._mcp = MCP9808(self._i2c_bus, self._address)
        asyncio.run(self._start())

    async def _start(self) -> None:
        mcp = self._mcp
        manufacturer = await mcp.get_manufacturer_id()
        device, _ = await mcp.get_device_id()
        if manufacturer != mcp.MANUFACTURER or device != mcp.DEVICE:
            raise RuntimeError(
                f"no MCP9808 at 0x{self._address:02X} "
                f"(manufacturer 0x{manufacturer:04X}, device 0x{device:02X})"
            )
        await mcp.set_resolution(_RESOLUTION_C)
        # Asleep between readings: converting continuously warms the chip about 0.14 °C.
        await mcp.shutdown()

    def read(self) -> Sample:
        # Wake, wait one conversion (about 0.3 s), read, shut down again. After a
        # brown-out the chip converts continuously; this puts it back to sleep.
        measurement = asyncio.run(self._mcp.measure_single_shot())
        # Not rounded: always a whole number of 1/16 °C, exact in 4 decimals. The
        # measurement's limit flags aren't used (the limits stay at 0 °C).
        return Sample({"temperature_c": measurement.temperature})

    def close(self) -> None:
        # Runs on the station's event loop thread, where asyncio.run() isn't allowed. The
        # chip is already asleep between readings.
        if self._mcp is not None:
            self._mcp.close()
            self._mcp = None
