"""Fan control task: sets the fans' PWM duty cycle from other sensors' latest values.

Two controls run together, each a :class:`FanInput` with its own :class:`FanCurve`:

- ``temp_diff``: the temperature of one sensor minus another's (by default
  the DS18B20 probe minus the CHT8305).
- ``humidity``: the relative humidity of one sensor (by default the CHT8305).

The fans run at the higher of the two duty cycles. If an input is missing or out of
date (its sensor has missed two readings), the fans run at least at the failsafe duty
cycle until it returns, or higher if the other control asks for more. They're left at
the failsafe duty cycle when the task stops.

The latest state is kept in a :class:`FanState`, which the health report publishes.
See docs/fan-control.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

from measure_station.fan.pwm import PwmOutput
from measure_station.sensors.base import LatestValues
from measure_station.supervisor import Heartbeat

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FanCurve:
    """Maps an input value to a duty cycle (in whole percent).

    The fans turn on at ``min_duty_pct`` when the input reaches ``start``, and rise in
    a straight line to ``max_duty_pct`` at ``full``. They turn off (``off_duty_pct``)
    only once the input is back past ``start`` by ``hysteresis``, so an input hovering
    near ``start`` doesn't switch them on and off. ``full`` may be below ``start``, for
    a fan that should run as the input falls.
    """

    start: float
    full: float
    hysteresis: float
    min_duty_pct: float
    max_duty_pct: float
    off_duty_pct: float

    def __post_init__(self) -> None:
        if self.start == self.full:
            raise ValueError("the curve's start and full values must differ")
        if self.hysteresis < 0:
            raise ValueError("the curve's hysteresis must be >= 0")
        for name in ("min_duty_pct", "max_duty_pct", "off_duty_pct"):
            if not 0 <= getattr(self, name) <= 100:
                raise ValueError(f"{name} must be 0-100")
        if self.min_duty_pct > self.max_duty_pct:
            raise ValueError("min_duty_pct must not exceed max_duty_pct")

    def duty(self, value: float, *, running: bool) -> tuple[int, bool]:
        """Return ``(duty_pct, running)`` for ``value``; ``running`` is whether the fans
        were on (past ``start``) at the previous value."""
        span = self.full - self.start
        # How far value is from start towards full: 0 at start, 1 at full.
        progress = (value - self.start) / span
        turn_off_below = -self.hysteresis / abs(span) if running else 0.0
        if progress < turn_off_below:
            return round(self.off_duty_pct), False
        progress = min(max(progress, 0.0), 1.0)
        return round(self.min_duty_pct + progress * (self.max_duty_pct - self.min_duty_pct)), True


@dataclass(frozen=True)
class FanInput:
    """One control: a value read from the sensors' latest readings, and its curve.

    With one source, the value is that source's ``field``. With two, it's the first
    source's ``field`` minus the second's.
    """

    name: str  # e.g. "humidity"; its duty cycle is reported as fan_<name>_duty_pct
    key: str  # the value's name, e.g. "temp_diff_c"
    sources: tuple[str, ...]
    field: str  # the key read from each source's values, e.g. "humidity_pct"
    curve: FanCurve
    # Whether the health report includes the value itself (as fan_<key>), not only the
    # duty cycle it asks for. Not needed when a sensor already publishes it.
    report_value: bool = False

    def __post_init__(self) -> None:
        if len(self.sources) not in (1, 2):
            raise ValueError(f"{self.name} needs one or two sources")
        if len(set(self.sources)) != len(self.sources):
            raise ValueError(f"{self.name}'s two sources must differ")

    def measure(self, latest: LatestValues) -> float | None:
        """The input's value, or None if a source is missing or out of date."""
        values = [latest.get(source, self.field) for source in self.sources]
        if None in values:
            return None
        value = values[0] - values[1] if len(values) == 2 else values[0]
        return round(value, 2)

    def missing(self, latest: LatestValues) -> list[str]:
        return [s for s in self.sources if latest.get(s, self.field) is None]


@dataclass
class FanState:
    """Fan control's latest evaluation, for the health report.

    Written on the event loop by :func:`control_fan`, read there by the health report.
    Everything is None while fan control isn't running (e.g. the PWM output failed).
    """

    inputs: tuple[FanInput, ...]
    failsafe: bool | None = None
    duty_pct: int | None = None
    values: dict[str, float | None] = field(default_factory=dict)  # by input name
    demands: dict[str, int | None] = field(default_factory=dict)  # by input name

    def clear(self) -> None:
        self.failsafe = self.duty_pct = None
        self.values.clear()
        self.demands.clear()

    def health_values(self) -> dict[str, Any]:
        """``fan_*`` keys for the health report; a value is None when it isn't known."""
        report: dict[str, Any] = {"fan_failsafe": self.failsafe, "fan_duty_pct": self.duty_pct}
        for fan_input in self.inputs:
            if fan_input.report_value:
                report[f"fan_{fan_input.key}"] = self.values.get(fan_input.name)
            report[f"fan_{fan_input.name}_duty_pct"] = self.demands.get(fan_input.name)
        return report


@dataclass(frozen=True)
class FanSettings:
    inputs: tuple[FanInput, ...]
    failsafe_duty_pct: float
    interval_s: float

    def __post_init__(self) -> None:
        if not self.inputs:
            raise ValueError("fan control needs at least one input")
        if not 0 <= self.failsafe_duty_pct <= 100:
            raise ValueError("failsafe_duty_pct must be 0-100")
        if self.interval_s <= 0:
            raise ValueError("interval_s must be > 0")

    def input_sources(self) -> list[str]:
        """Every sensor the inputs read, which must be enabled."""
        return list(dict.fromkeys(s for fan_input in self.inputs for s in fan_input.sources))


async def control_fan(
    output: PwmOutput,
    latest: LatestValues,
    state: FanState,
    *,
    settings: FanSettings,
    heartbeat: Heartbeat,
) -> None:
    """Set the fans' duty cycle every ``settings.interval_s`` until cancelled or the
    output fails, keeping ``state`` up to date."""
    failsafe_pct = round(settings.failsafe_duty_pct)
    try:
        await asyncio.to_thread(output.open)
        log.info(
            "Fan control started on %r: %s",
            output,
            ", ".join(fan_input.name for fan_input in settings.inputs),
        )
        duty: int | None = None  # the duty cycle last set
        running = {fan_input.name: False for fan_input in settings.inputs}
        missing: list[str] | None = None  # None until the first evaluation
        while True:
            heartbeat.beat()
            values: dict[str, float | None] = {}
            demands: dict[str, int | None] = {}
            for fan_input in settings.inputs:
                value = values[fan_input.name] = fan_input.measure(latest)
                if value is None:
                    running[fan_input.name] = False
                    demands[fan_input.name] = None
                    continue
                demand, now_running = fan_input.curve.duty(value, running=running[fan_input.name])
                if now_running != running[fan_input.name]:
                    log.info(
                        "Fan control %s %s (%s=%s)",
                        fan_input.name,
                        "on" if now_running else "off",
                        fan_input.key,
                        value,
                    )
                running[fan_input.name] = now_running
                demands[fan_input.name] = demand

            now_missing = list(
                dict.fromkeys(s for fan_input in settings.inputs for s in fan_input.missing(latest))
            )
            _log_missing_change(missing, now_missing, failsafe_pct)
            missing = now_missing
            # The higher demand wins; a missing input raises it to at least the failsafe.
            new_duty = max((d for d in demands.values() if d is not None), default=0)
            if missing:
                new_duty = max(new_duty, failsafe_pct)

            if new_duty != duty:
                await asyncio.to_thread(output.set_duty, new_duty)
                duty = new_duty
            state.failsafe, state.duty_pct = bool(missing), duty
            state.values, state.demands = values, demands
            await asyncio.sleep(settings.interval_s)
    finally:
        state.clear()
        # Runs on the event loop thread; a sysfs write is a few microseconds.
        with contextlib.suppress(Exception):
            output.set_duty(failsafe_pct)
        with contextlib.suppress(Exception):
            output.close()


def _log_missing_change(was: list[str] | None, now: list[str], failsafe_pct: int) -> None:
    if now == was:
        return
    if now:
        # Expected right after a start, before the sensors' first readings.
        level = logging.INFO if was is None else logging.WARNING
        log.log(
            level,
            "Fans at least at failsafe %s%%: no up-to-date reading from %s",
            failsafe_pct,
            ", ".join(now),
        )
    elif was:
        log.info("Fan inputs available again; fans under control")
