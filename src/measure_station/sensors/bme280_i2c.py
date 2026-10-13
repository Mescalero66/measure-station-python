"""Bosch BME280 temperature / humidity / pressure sensor on I2C.

Requires the optional dependencies: ``pip install -e ".[bme280]"``.
Wiring: SDA -> GPIO2 (pin 3), SCL -> GPIO3 (pin 5); I2C must be enabled.
Check the address with ``i2cdetect -y 1`` (usually 0x76 or 0x77).
"""

from __future__ import annotations

from typing import Any

from measure_station.sensors.base import Sensor


class Bme280Sensor(Sensor):
    def __init__(
        self,
        name: str,
        interval_s: float,
        i2c_bus: int = 1,
        address: int = 0x76,
    ) -> None:
        super().__init__(name, interval_s)
        self._i2c_bus = i2c_bus
        self._address = address
        self._bus: Any = None
        self._calibration: Any = None
        self._driver: Any = None

    def open(self) -> None:
        # Imported lazily so the package works on machines without these extras.
        import bme280
        import smbus2

        self._driver = bme280
        self._bus = smbus2.SMBus(self._i2c_bus)
        self._calibration = bme280.load_calibration_params(self._bus, self._address)

    def read(self) -> dict[str, float]:
        sample = self._driver.sample(self._bus, self._address, self._calibration)
        return {
            "temperature_c": round(sample.temperature, 2),
            "humidity_pct": round(sample.humidity, 2),
            "pressure_hpa": round(sample.pressure, 2),
        }

    def close(self) -> None:
        if self._bus is not None:
            self._bus.close()
            self._bus = None
