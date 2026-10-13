"""Sensor drivers and the registry mapping config ``type`` names to driver classes.

To add a sensor: subclass :class:`Sensor` in a new module, register it in
``SENSOR_TYPES`` below, and add an entry to ``sysconfig.SENSORS``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from measure_station.sensors.base import Sensor, poll_sensor
from measure_station.sensors.bme280_i2c import Bme280Sensor
from measure_station.sensors.cpu_temperature import CpuTemperatureSensor
from measure_station.sensors.simulated import SimulatedSensor

SENSOR_TYPES: dict[str, type[Sensor]] = {
    "bme280": Bme280Sensor,
    "cpu_temperature": CpuTemperatureSensor,
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
    return cls(**options)


__all__ = ["SENSOR_TYPES", "Sensor", "create_sensor", "poll_sensor"]
