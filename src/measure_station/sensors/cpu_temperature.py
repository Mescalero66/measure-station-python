"""SoC temperature from the Linux thermal subsystem (no extra hardware needed)."""

from __future__ import annotations

from pathlib import Path

from measure_station.sensors.base import Sample, Sensor


class CpuTemperatureSensor(Sensor):
    def __init__(
        self,
        name: str,
        interval_s: float,
        path: str = "/sys/class/thermal/thermal_zone0/temp",
    ) -> None:
        super().__init__(name, interval_s)
        self._path = Path(path)

    def read(self) -> Sample:
        millidegrees = int(self._path.read_text().strip())
        return Sample({"temperature_c": round(millidegrees / 1000, 2)})
