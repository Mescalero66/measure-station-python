"""Maxim DS18B20 temperature sensor on the Pi's 1-wire bus (the kernel's w1-gpio driver).

Reads the kernel's sysfs file for the probe, so it needs no Python library. See
docs/sensors/onewire-temperature-sensor.md for wiring and settings.

Values:
    temperature_c:  temperature at the probe, -55 to 125 °C, in steps of 0.0625 °C,
                    after its temperature offset (temperature_offset_c). ±0.5 °C from
                    -10 to 85 °C.

Each read starts a conversion and waits for it (about 0.75 s at 12-bit resolution).
"""

from __future__ import annotations

import logging
from pathlib import Path

from measure_station.sensors.base import Sample, Sensor

log = logging.getLogger(__name__)

_DEVICES = Path("/sys/bus/w1/devices")
_FAMILY = "28-"  # the DS18B20's 1-wire family code
# A read with a bad CRC is retried this many times before the read fails.
_ATTEMPTS = 3
# The value the chip holds after power-on, before its first conversion. A reading of
# exactly 85 °C usually means the probe browned out and reset.
_POWER_ON_MILLI_C = 85_000
# temperature_offset_c's limits: well beyond the ±0.5 °C accuracy, so a larger value is
# most likely a typo (e.g. 6 for 0.6).
_MAX_OFFSET_C = 10.0


class Ds18b20Sensor(Sensor):
    def __init__(
        self,
        name: str,
        interval_s: float,
        device_id: str | None = None,
        temperature_offset_c: float = 0.0,
        devices_dir: str = str(_DEVICES),
    ) -> None:
        super().__init__(name, interval_s)
        if device_id is not None and not device_id.startswith(_FAMILY):
            raise ValueError(f"device_id must start with {_FAMILY!r} (e.g. '28-0000075a1b2c')")
        if not -_MAX_OFFSET_C <= temperature_offset_c <= _MAX_OFFSET_C:
            raise ValueError(
                f"temperature_offset_c must be -{_MAX_OFFSET_C:g} to {_MAX_OFFSET_C:g}"
            )
        self._device_id = device_id
        # Subtracted, like the SCD41's: how much the probe reads too high (negative if low).
        self._offset_c = temperature_offset_c
        self._devices_dir = Path(devices_dir)
        self._file: Path | None = None

    def open(self) -> None:
        if self._device_id is not None:
            device = self._devices_dir / self._device_id
            if not device.is_dir():
                raise OSError(f"no 1-wire device {self._device_id} in {self._devices_dir}")
        else:
            found = sorted(self._devices_dir.glob(f"{_FAMILY}*"))
            if len(found) != 1:
                names = ", ".join(d.name for d in found) or "none"
                raise OSError(
                    f"expected exactly one DS18B20 in {self._devices_dir}, found {names}; "
                    "set device_id to choose one"
                )
            device = found[0]
            log.info("%s: using DS18B20 %s", self.name, device.name)
        self._file = device / "w1_slave"

    def read(self) -> Sample:
        assert self._file is not None, "read() before open()"
        problem = ""
        for _ in range(_ATTEMPTS):
            milli_c, problem = _parse(self._file.read_text())
            if milli_c is not None:
                # 4 places hold the 0.0625 °C steps exactly; they also drop the float noise
                # an offset like 0.6 would add.
                return Sample({"temperature_c": round(milli_c / 1000 - self._offset_c, 4)})
        raise OSError(f"DS18B20 {self._file.parent.name}: {problem}")

    def close(self) -> None:
        self._file = None


def _parse(text: str) -> tuple[int | None, str]:
    """Parse the kernel's ``w1_slave`` text into millidegrees, or ``(None, problem)``.

    The file holds two lines, the 9-byte scratchpad each time::

        72 01 4b 46 7f ff 0e 10 57 : crc=57 YES
        72 01 4b 46 7f ff 0e 10 57 t=23125
    """
    lines = text.strip().splitlines()
    if len(lines) != 2 or "t=" not in lines[1]:
        return None, f"unexpected w1_slave contents {text!r}"
    if not lines[0].endswith("YES"):
        return None, "CRC check failed (check the wiring and the pull-up resistor)"
    scratchpad = lines[0].split(":")[0].split()
    if all(byte == "00" for byte in scratchpad):
        # All zeros passes the CRC check: the data line is shorted to ground.
        return None, "read all zeros (the data line may be shorted to ground)"
    milli_c = int(lines[1].rsplit("t=", 1)[1])
    if milli_c == _POWER_ON_MILLI_C:
        return None, "read its power-on value of 85 °C (the probe may have browned out)"
    return milli_c, ""
