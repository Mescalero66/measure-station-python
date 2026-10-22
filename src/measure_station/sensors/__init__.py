"""Sensor drivers and the registry mapping config ``type`` names to driver classes.

To add a sensor: subclass :class:`Sensor` in a new module, register it in
``SENSOR_TYPES`` below, and add an entry to ``sysconfig.SENSORS``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from measure_station.sensors.base import LatestValues, Sample, Sensor, poll_sensor
from measure_station.sensors.bh1750_i2c import Bh1750Sensor
from measure_station.sensors.bmp280_i2c import Bmp280Sensor
from measure_station.sensors.cht8305_i2c import Cht8305Sensor
from measure_station.sensors.cpu_temperature import CpuTemperatureSensor
from measure_station.sensors.ens160_aht21_i2c import Ens160Aht21Sensor
from measure_station.sensors.mcp9808_i2c import Mcp9808Sensor
from measure_station.sensors.scd41_i2c import Scd41Sensor
from measure_station.sensors.sen62_i2c import Sen62Sensor
from measure_station.sensors.simulated import SimulatedSensor

SENSOR_TYPES: dict[str, type[Sensor]] = {
    "bh1750": Bh1750Sensor,
    "bmp280": Bmp280Sensor,
    "cht8305": Cht8305Sensor,
    "cpu_temperature": CpuTemperatureSensor,
    "ens160_aht21": Ens160Aht21Sensor,
    "mcp9808": Mcp9808Sensor,
    "scd41": Scd41Sensor,
    "sen62": Sen62Sensor,
    "simulated": SimulatedSensor,
}


def create_sensor(config: Mapping[str, Any]) -> Sensor:
    """Build a sensor from one ``sysconfig.SENSORS`` entry."""
    options = dict(config)
    options.pop("enabled", None)
    type_name = options.pop("type", None)
    try:
        cls = SENSOR_TYPES[type_name]
    except KeyError:
        raise ValueError(
            f"unknown sensor type {type_name!r}; expected one of {sorted(SENSOR_TYPES)}"
        ) from None
    sensor = cls(**options)
    sensor.sensor_type = type_name
    return sensor


__all__ = ["SENSOR_TYPES", "LatestValues", "Sample", "Sensor", "create_sensor", "poll_sensor"]
