"""Fan control task: switches the exhaust (top) and intake (bottom) fans on or off.

Each fan set has its own PWM output. A fan set that's on runs at 100 % in binary mode;
in analog mode, at a speed set by the temperature difference (:meth:`FanSettings.speed`).
A fan set that turns on runs at 100 % for ``kickstart_s`` first, so that a low speed
can't leave it stalled.
Which fan sets are on, in priority order:

1. Heat building up: the temperature difference (by default the DS18B20 probe at the
   top minus the CHT8305 at the bottom) has turned the ``heat`` :class:`FanSwitch` on.
   Both fan sets run. Once on, the intake fans stay on for at least
   ``intake_min_on_s``.
2. Failsafe: a source is missing or out of date (its sensor has missed two readings).
   Exhaust on, intake off.
3. Fog: the humidity (by default the CHT8305's) has turned the ``fog`` switch on, which
   then holds each state for at least ``fog_min_s``. Both fan sets off.
4. Normal: exhaust on, intake off, drawing outside air in through the bottom vents.

The fans are left in the failsafe state (exhaust at 100 %, intake off) when the task
stops.
The latest state is kept in a :class:`FanState`, which the health report publishes.
See docs/fan-control.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from dataclasses import dataclass
from typing import Any

from measure_station.fan.pwm import PwmOutput
from measure_station.sensors.base import LatestValues
from measure_station.supervisor import Heartbeat

log = logging.getLogger(__name__)

_FULL_PCT, _OFF_PCT = 100, 0
_TEMPERATURE, _HUMIDITY = "temperature_c", "humidity_pct"  # the keys read from values


@dataclass(frozen=True)
class FanSwitch:
    """Turns on when its input reaches ``on``, and off when it reaches ``off``.

    Between the two, it stays as it was, so an input hovering near one of them doesn't
    switch the fans on and off. ``on`` above ``off`` turns on as the input rises;
    ``on`` below ``off`` turns on as it falls.
    """

    on: float
    off: float

    def __post_init__(self) -> None:
        if self.on == self.off:
            raise ValueError("the switch's on and off values must differ")

    def is_on(self, value: float, *, was_on: bool) -> bool:
        """Whether the switch is on at ``value``, given whether it was on before."""
        sign = 1 if self.on > self.off else -1  # compare as if it turns on as it rises
        if sign * value >= sign * self.on:
            return True
        if sign * value <= sign * self.off:
            return False
        return was_on


@dataclass(frozen=True)
class FanSettings:
    source: str  # the sensor whose temperature_c is measured...
    reference: str  # ...minus this one's
    heat: FanSwitch  # on the temperature difference: both fan sets on
    humidity_source: str  # the sensor whose humidity_pct is read
    fog: FanSwitch  # on the humidity: both fan sets off
    interval_s: float
    intake_min_on_s: float  # once on, the intake fans stay on at least this long
    fog_min_s: float  # fog mode stays on, or off, at least this long
    analog: bool = False  # False: fans that are on run at 100 %
    min_duty_pct: float = 0  # analog mode: the speed at or past heat.off
    kickstart_s: float = 0  # a fan set that turns on runs at 100 % for this long first

    def __post_init__(self) -> None:
        if self.source == self.reference:
            raise ValueError("the temperature source and reference must differ")
        if self.interval_s <= 0:
            raise ValueError("interval_s must be > 0")
        if min(self.intake_min_on_s, self.fog_min_s, self.kickstart_s) < 0:
            raise ValueError("intake_min_on_s, fog_min_s and kickstart_s must be >= 0")
        if not 0 <= self.min_duty_pct <= 100:
            raise ValueError("min_duty_pct must be 0-100")

    def speed(self, difference: float | None) -> int:
        """The duty cycle, in whole percent, for a fan set that's on.

        100 in binary mode, and while the difference is unknown. In analog mode,
        ``min_duty_pct`` at ``heat.off`` (or further from ``heat.on``), rising in a
        straight line to 100 at ``heat.on`` (or beyond).
        """
        if not self.analog or difference is None:
            return _FULL_PCT
        progress = (difference - self.heat.off) / (self.heat.on - self.heat.off)
        progress = min(max(progress, 0.0), 1.0)
        return round(self.min_duty_pct + progress * (_FULL_PCT - self.min_duty_pct))

    def sources(self) -> list[str]:
        """The sensors fan control reads, which must be enabled."""
        return list(dict.fromkeys([self.source, self.reference, self.humidity_source]))

    def temperature_difference(self, latest: LatestValues) -> float | None:
        """Source minus reference, in °C, or None if either is missing or out of date."""
        source = latest.get(self.source, _TEMPERATURE)
        reference = latest.get(self.reference, _TEMPERATURE)
        if source is None or reference is None:
            return None
        return round(source - reference, 2)

    def humidity(self, latest: LatestValues) -> float | None:
        return latest.get(self.humidity_source, _HUMIDITY)

    def missing(self, latest: LatestValues) -> list[str]:
        needed = [(self.source, _TEMPERATURE), (self.reference, _TEMPERATURE)]
        needed.append((self.humidity_source, _HUMIDITY))
        return list(dict.fromkeys(s for s, key in needed if latest.get(s, key) is None))


@dataclass
class FanState:
    """Fan control's latest evaluation, for the health report.

    Written on the event loop by :func:`control_fan`, read there by the health report.
    Everything is None while fan control isn't running (e.g. a PWM output failed), and
    ``temp_diff_c`` and ``fog`` while their inputs are missing.
    """

    failsafe: bool | None = None
    exhaust_duty_pct: int | None = None
    intake_duty_pct: int | None = None
    temp_diff_c: float | None = None
    fog: bool | None = None

    def clear(self) -> None:
        self.failsafe = self.exhaust_duty_pct = self.intake_duty_pct = None
        self.temp_diff_c = self.fog = None

    def health_values(self) -> dict[str, Any]:
        """``fan_*`` keys for the health report; a value is None when it isn't known."""
        return {
            "fan_failsafe": self.failsafe,
            "fan_exhaust_duty_pct": self.exhaust_duty_pct,
            "fan_intake_duty_pct": self.intake_duty_pct,
            "fan_temp_diff_c": self.temp_diff_c,
            "fan_fog": self.fog,
        }


async def control_fan(
    exhaust: PwmOutput,
    intake: PwmOutput,
    latest: LatestValues,
    state: FanState,
    *,
    settings: FanSettings,
    heartbeat: Heartbeat,
) -> None:
    """Switch both fan sets every ``settings.interval_s`` until cancelled or an output
    fails, keeping ``state`` up to date."""
    try:
        await asyncio.to_thread(exhaust.open)
        await asyncio.to_thread(intake.open)
        log.info("Fan control started: exhaust on %r, intake on %r", exhaust, intake)
        heat = fog = False  # the switches' states
        fog_since = -math.inf  # time.monotonic() when fog mode last changed
        intake_since = -math.inf  # ...and when the intake fans last turned on
        fans: tuple[bool, bool] | None = None  # (exhaust, intake): which are on
        duties: tuple[int, ...] | None = None  # (exhaust, intake) as last set
        kick_until = [-math.inf, -math.inf]  # (exhaust, intake): 100 % until then
        missing: list[str] | None = None  # None until the first evaluation
        while True:
            heartbeat.beat()
            now = time.monotonic()
            difference = settings.temperature_difference(latest)
            heat = difference is not None and settings.heat.is_on(difference, was_on=heat)
            humidity = settings.humidity(latest)
            if humidity is None:
                fog, fog_since = False, -math.inf
            else:
                now_fog = settings.fog.is_on(humidity, was_on=fog)
                if now_fog != fog and now - fog_since >= settings.fog_min_s:
                    fog, fog_since = now_fog, now
            now_missing = settings.missing(latest)
            _log_missing_change(missing, now_missing)
            missing = now_missing

            intake_was_on = fans is not None and fans[1]
            held = intake_was_on and now - intake_since < settings.intake_min_on_s
            if heat or held:
                mode, now_fans = "heat", (True, True)
            elif missing:
                mode, now_fans = "failsafe", (True, False)
            elif fog:
                mode, now_fans = "fog", (False, False)
            else:
                mode, now_fans = "normal", (True, False)
            if now_fans[1] and not intake_was_on:
                intake_since = now
            if now_fans != fans:
                # Only on/off changes are logged: an analog speed changes every reading.
                log.info(
                    "Fans: exhaust %s, intake %s (%s; temp_diff_c=%s, humidity_pct=%s)",
                    *("on" if on else "off" for on in now_fans),
                    mode,
                    difference,
                    humidity,
                )
                for i, on in enumerate(now_fans):
                    if on and not (fans is not None and fans[i]):
                        # From standstill: full power first, so a low speed can't stall it.
                        kick_until[i] = now + settings.kickstart_s
                fans = now_fans

            speed = settings.speed(difference)
            now_duties = tuple(
                (_FULL_PCT if now < kick_until[i] else speed) if on else _OFF_PCT
                for i, on in enumerate(fans)
            )
            for i, output in enumerate((exhaust, intake)):
                if duties is None or now_duties[i] != duties[i]:
                    await asyncio.to_thread(output.set_duty, now_duties[i])
            duties = now_duties
            state.failsafe = bool(missing)
            state.exhaust_duty_pct, state.intake_duty_pct = duties
            state.temp_diff_c = difference
            state.fog = None if humidity is None else fog
            # Wake when a kick-start ends, if that's sooner than the next evaluation.
            kicks = [t - now for t in kick_until if t > now]
            await asyncio.sleep(min([settings.interval_s, *kicks]))
    finally:
        state.clear()
        # Runs on the event loop thread; a sysfs write is a few microseconds.
        for output, duty in ((exhaust, _FULL_PCT), (intake, _OFF_PCT)):
            with contextlib.suppress(Exception):
                output.set_duty(duty)
            with contextlib.suppress(Exception):
                output.close()


def _log_missing_change(was: list[str] | None, now: list[str]) -> None:
    if now == was:
        return
    if now:
        # Expected right after a start, before the sensors' first readings.
        level = logging.INFO if was is None else logging.WARNING
        log.log(level, "No up-to-date reading from %s: fans on failsafe", ", ".join(now))
    elif was:
        log.info("Fan inputs available again; fans under control")
