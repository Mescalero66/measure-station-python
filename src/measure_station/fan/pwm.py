"""Hardware PWM output through the Linux sysfs PWM interface (``/sys/class/pwm``).

The Pi's PWM controller is enabled with a device tree overlay, which also chooses the
pins, e.g. in config.txt ``dtoverlay=pwm-2chan,pin=18,func=2,pin2=19,func2=2`` (GPIO18,
header pin 12, as PWM channel 0 of pwmchip0; GPIO19, header pin 35, as channel 1). See
docs/fan-control.md.

On Raspberry Pi OS, a udev rule gives the gpio group write access to an exported
channel, a moment after it's exported. The service user is in that group.

All methods block briefly; call them via ``asyncio.to_thread``, except ``close()``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Protocol

# After exporting a channel, how long to wait for udev to create and chown its files.
_EXPORT_WAIT_S = 5.0
_EXPORT_POLL_S = 0.05


class PwmOutput(Protocol):
    def open(self) -> None: ...
    def set_duty(self, duty_pct: float) -> None: ...
    def close(self) -> None: ...


class SysfsPwm:
    """One PWM channel. ``inverted`` outputs 100 - duty, for a driver stage that inverts
    the signal (e.g. a P-channel MOSFET switching the fan's positive supply)."""

    def __init__(
        self,
        *,
        chip: int = 0,
        channel: int = 0,
        frequency_hz: float = 100,
        inverted: bool = False,
        root: Path = Path("/sys/class/pwm"),
    ) -> None:
        if frequency_hz <= 0:
            raise ValueError("frequency_hz must be positive")
        self._chip_dir = root / f"pwmchip{chip}"
        self._channel = channel
        self._dir = self._chip_dir / f"pwm{channel}"
        self._period_ns = round(1e9 / frequency_hz)
        self._inverted = inverted

    def __repr__(self) -> str:
        return f"SysfsPwm({self._dir}, {1e9 / self._period_ns:.0f} Hz)"

    def open(self) -> None:
        if not self._chip_dir.is_dir():
            raise OSError(
                f"{self._chip_dir} not found: is the PWM overlay enabled in config.txt "
                "(e.g. dtoverlay=pwm-2chan,pin=18,func=2,pin2=19,func2=2)?"
            )
        if not self._dir.is_dir():
            (self._chip_dir / "export").write_text(str(self._channel))
        self._wait_writable()
        # Already set up (e.g. a restart of the task): leave the fans running meanwhile.
        if (self._dir / "period").read_text().strip() != str(self._period_ns):
            # The duty cycle may never exceed the period, so clear it before changing it.
            self._write("duty_cycle", 0)
            self._write("period", self._period_ns)
        self._write("enable", 1)

    def set_duty(self, duty_pct: float) -> None:
        if not 0 <= duty_pct <= 100:
            raise ValueError(f"duty_pct {duty_pct} is outside 0-100")
        if self._inverted:
            duty_pct = 100 - duty_pct
        self._write("duty_cycle", round(self._period_ns * duty_pct / 100))

    def close(self) -> None:
        # Nothing to release. The channel stays exported and enabled at its last duty
        # cycle, so the fans keep the speed they were last given.
        pass

    def _write(self, name: str, value: int) -> None:
        (self._dir / name).write_text(str(value))

    def _wait_writable(self) -> None:
        deadline = time.monotonic() + _EXPORT_WAIT_S
        while True:
            try:
                # Rewriting the current value checks access without changing anything.
                enable = self._dir / "enable"
                enable.write_text(enable.read_text().strip())
                return
            except (FileNotFoundError, PermissionError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(_EXPORT_POLL_S)
