"""Fan control task: switches the fans on or off from a temperature difference.

The difference is one sensor's temperature minus another's (by default the DS18B20 probe
at the top of the enclosure minus the CHT8305 at the bottom), switched by a
:class:`FanSwitch`. The fans run at 100 % while it's on, and are off (0 %) otherwise.

If a source is missing or out of date (its sensor has missed two readings), the fans are
on or off as ``failsafe_on`` says until it returns. Once on, for whatever reason, they
stay on for at least ``min_on_s``. They're left in the failsafe state when the task
stops.

The latest state is kept in a :class:`FanState`, which the health report publishes.
See docs/fan-control.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Any

from measure_station.fan.pwm import PwmOutput
from measure_station.sensors.base import LatestValues
from measure_station.supervisor import Heartbeat

log = logging.getLogger(__name__)

_ON_PCT, _OFF_PCT = 100, 0  # the PWM duty cycles for fans on and off
_FIELD = "temperature_c"  # the key read from each source's values


@dataclass(frozen=True)
class FanSwitch:
    """Turns on when its input reaches ``on``, and off when it reaches ``off``.

    Between the two, it stays as it was, so an input hovering near one of them doesn't
    switch the fans on and off. ``on`` above ``off`` runs the fans as the input rises;
    ``on`` below ``off`` runs them as it falls.
    """

    on: float
    off: float

    def __post_init__(self) -> None:
        if self.on == self.off:
            raise ValueError("the switch's on and off values must differ")

    def is_on(self, value: float, *, was_on: bool) -> bool:
        """Whether the switch is on at ``value``, given whether it was on before."""
        sign = 1 if self.on > self.off else -1  # compare as if the fans run as it rises
        if sign * value >= sign * self.on:
            return True
        if sign * value <= sign * self.off:
            return False
        return was_on


@dataclass(frozen=True)
class FanSettings:
    source: str  # the sensor whose temperature_c is measured...
    reference: str  # ...minus this one's
    switch: FanSwitch
    failsafe_on: bool
    interval_s: float
    min_on_s: float  # once on, the fans stay on at least this long

    def __post_init__(self) -> None:
        if self.source == self.reference:
            raise ValueError("the temperature source and reference must differ")
        if self.interval_s <= 0:
            raise ValueError("interval_s must be > 0")
        if self.min_on_s < 0:
            raise ValueError("min_on_s must be >= 0")

    def sources(self) -> list[str]:
        """The sensors fan control reads, which must be enabled."""
        return [self.source, self.reference]

    def temperature_difference(self, latest: LatestValues) -> float | None:
        """Source minus reference, in °C, or None if either is missing or out of date."""
        source, reference = (latest.get(name, _FIELD) for name in self.sources())
        if source is None or reference is None:
            return None
        return round(source - reference, 2)

    def missing(self, latest: LatestValues) -> list[str]:
        return [name for name in self.sources() if latest.get(name, _FIELD) is None]


@dataclass
class FanState:
    """Fan control's latest evaluation, for the health report.

    Written on the event loop by :func:`control_fan`, read there by the health report.
    Everything is None while fan control isn't running (e.g. the PWM output failed), and
    the difference and its switch while a source is missing.
    """

    failsafe: bool | None = None
    on: bool | None = None
    temp_diff_c: float | None = None
    temp_diff_on: bool | None = None

    def clear(self) -> None:
        self.failsafe = self.on = self.temp_diff_c = self.temp_diff_on = None

    def health_values(self) -> dict[str, Any]:
        """``fan_*`` keys for the health report; a value is None when it isn't known."""
        return {
            "fan_failsafe": self.failsafe,
            "fan_on": self.on,
            "fan_temp_diff_c": self.temp_diff_c,
            "fan_temp_diff_on": self.temp_diff_on,
        }


async def control_fan(
    output: PwmOutput,
    latest: LatestValues,
    state: FanState,
    *,
    settings: FanSettings,
    heartbeat: Heartbeat,
) -> None:
    """Switch the fans every ``settings.interval_s`` until cancelled or the output
    fails, keeping ``state`` up to date."""
    failsafe_pct = _ON_PCT if settings.failsafe_on else _OFF_PCT
    try:
        await asyncio.to_thread(output.open)
        log.info(
            "Fan control started on %r: %s minus %s",
            output,
            settings.source,
            settings.reference,
        )
        fans_on: bool | None = None  # as last set
        on_since = 0.0  # time.monotonic() when the fans last turned on
        switch_on = False
        missing: list[str] | None = None  # None until the first evaluation
        while True:
            heartbeat.beat()
            difference = settings.temperature_difference(latest)
            if difference is None:
                switch_on = False
            else:
                now_on = settings.switch.is_on(difference, was_on=switch_on)
                if now_on != switch_on:
                    log.info(
                        "Fan control %s (temp_diff_c=%s)", "on" if now_on else "off", difference
                    )
                switch_on = now_on

            now_missing = settings.missing(latest)
            _log_missing_change(missing, now_missing, settings.failsafe_on)
            missing = now_missing
            new_on = switch_on or (bool(missing) and settings.failsafe_on)
            if fans_on and not new_on and time.monotonic() - on_since < settings.min_on_s:
                new_on = True  # not on for long enough yet

            if new_on != fans_on:
                await asyncio.to_thread(output.set_duty, _ON_PCT if new_on else _OFF_PCT)
                if new_on:
                    on_since = time.monotonic()
                fans_on = new_on
            state.failsafe, state.on = bool(missing), fans_on
            state.temp_diff_c = difference
            state.temp_diff_on = None if difference is None else switch_on
            await asyncio.sleep(settings.interval_s)
    finally:
        state.clear()
        # Runs on the event loop thread; a sysfs write is a few microseconds.
        with contextlib.suppress(Exception):
            output.set_duty(failsafe_pct)
        with contextlib.suppress(Exception):
            output.close()


def _log_missing_change(was: list[str] | None, now: list[str], failsafe_on: bool) -> None:
    if now == was:
        return
    if now:
        # Expected right after a start, before the sensors' first readings.
        level = logging.INFO if was is None else logging.WARNING
        log.log(
            level,
            "No up-to-date reading from %s: fans %s (failsafe)",
            ", ".join(now),
            "on" if failsafe_on else "off",
        )
    elif was:
        log.info("Fan inputs available again; fans under control")
