"""Fake sensor producing a random walk; for development and pipeline testing."""

from __future__ import annotations

import random
from collections.abc import Sequence

from measure_station.sensors.base import Sample, Sensor


class SimulatedSensor(Sensor):
    def __init__(
        self,
        name: str,
        interval_s: float,
        channels: Sequence[str] = ("value",),
        start: float = 20.0,
        step: float = 0.2,
    ) -> None:
        super().__init__(name, interval_s)
        self._state = dict.fromkeys(channels, float(start))
        self._step = step

    def read(self) -> Sample:
        for channel in self._state:
            self._state[channel] += random.uniform(-self._step, self._step)
        return Sample({channel: round(value, 3) for channel, value in self._state.items()})
