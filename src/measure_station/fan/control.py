"""Fan control task: switches the exhaust (top) and intake (bottom) fans on or off.

Each fan set has its own PWM output. A fan set that's on runs at 100 % in binary mode;
in analog mode, at a speed set by the temperature difference (:meth:`FanSettings.speed`).
A fan set that starts from standstill runs at 100 % for ``kickstart_s`` first, so that a
low speed can't leave it stalled.
Which fan sets are on, in priority order:

1. Heat building up: the temperature difference (by default the DS18B20 probe at the
   top minus the CHT8305 at the bottom) has turned the ``heat`` :class:`FanSwitch` on.
   Both fan sets run. Once on, the intake fans stay on for at least
   ``intake_min_on_s``.
2. Failsafe: a source is missing or out of date (its sensor has missed two readings).
   Exhaust on, intake off.
3. Fog: the humidity (by default the CHT8305's) has reached ``fog.on`` on
   ``fog_on_readings`` readings in a row. It lasts at least ``fog_min_s``, then until
   the humidity reaches ``fog.off``; once over, it can't start again for
   ``intake_min_on_s``. Exhaust slowed to ``min_duty_pct``, intake off.
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
    fog: FanSwitch  # on the humidity: exhaust slowed to min_duty_pct, intake off
    interval_s: float
    # Once on, the intake fans stay on at least this long; and once fog mode is over, it
    # can't start again for this long.
    intake_min_on_s: float
    fog_min_s: float  # fog mode lasts at least this long
    fog_on_readings: int = 1  # fog mode starts after this many readings in a row at fog.on
    analog: bool = False  # False: fans that are on run at 100 %
    # Analog mode: the speed at or past heat.off. In both modes: the exhaust's speed in fog.
    min_duty_pct: float = 0
    kickstart_s: float = 0  # a fan set starting from standstill runs at 100 % this long first

    def __post_init__(self) -> None:
        if self.source == self.reference:
            raise ValueError("the temperature source and reference must differ")
        if self.interval_s <= 0:
            raise ValueError("interval_s must be > 0")
        if min(self.intake_min_on_s, self.fog_min_s, self.kickstart_s) < 0:
            raise ValueError("intake_min_on_s, fog_min_s and kickstart_s must be >= 0")
        if self.fog_on_readings < 1:
            raise ValueError("fog_on_readings must be >= 1")
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

    ``status`` is why the fans are as they are: a mode (``normal``, ``heat``, ``fog``,
    ``failsafe``), or ``heat_hold``, ``fog_hold`` or ``fog_lockout`` while a minimum time
    is what decides it (see :func:`control_fan`). Before fan control's first evaluation
    it's ``starting``; once fan control has stopped or failed (e.g. a PWM output), it's
    ``error``, and everything else is None. ``temp_diff_c`` is also None while its
    inputs are missing.
    """

    status: str = "starting"
    exhaust_duty_pct: int | None = None
    intake_duty_pct: int | None = None
    temp_diff_c: float | None = None

    def clear(self) -> None:
        """Fan control has stopped: nothing is known except that it isn't running."""
        self.status = "error"
        self.exhaust_duty_pct = self.intake_duty_pct = self.temp_diff_c = None

    def health_values(self) -> dict[str, Any]:
        """``fan_*`` keys for the health report; a value is None when it isn't known."""
        return {
            "fan_status": self.status,
            "fan_exhaust_duty_pct": self.exhaust_duty_pct,
            "fan_intake_duty_pct": self.intake_duty_pct,
            "fan_temp_diff_c": self.temp_diff_c,
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
        heat = fog = False  # the modes' states
        fog_since = fog_ended = -math.inf  # time.monotonic() when fog mode started, ended
        humid_readings = 0  # humidity readings in a row at fog.on
        humidity_reading: int | None = None  # the last one counted (reading_number)
        intake_since = -math.inf  # when the intake fans last turned on
        fans: tuple[bool, bool] | None = None  # (exhaust, intake): which are on
        status: str | None = None  # the mode, or a hold within it (FanState.status)
        duties: tuple[int, ...] | None = None  # (exhaust, intake) as last set
        kick_until = [-math.inf, -math.inf]  # (exhaust, intake): 100 % until then
        missing: list[str] | None = None  # None until the first evaluation
        while True:
            heartbeat.beat()
            now = time.monotonic()
            difference = settings.temperature_difference(latest)
            heat = difference is not None and settings.heat.is_on(difference, was_on=heat)
            humidity = settings.humidity(latest)
            fog_held = fog_locked_out = False  # a minimum time is what decides fog mode
            if humidity is None:
                fog, humid_readings, humidity_reading = False, 0, None
            else:
                # Count readings, not evaluations: the sensor reads less often than this.
                reading = latest.reading_number(settings.humidity_source)
                if reading != humidity_reading:
                    humidity_reading = reading
                    humid = settings.fog.is_on(humidity, was_on=False)  # at or past fog.on
                    humid_readings = humid_readings + 1 if humid else 0
                if fog:
                    over = not settings.fog.is_on(humidity, was_on=True)  # at or past fog.off
                    fog_held = over and now - fog_since < settings.fog_min_s
                    if over and not fog_held:
                        fog, fog_ended = False, now
                elif humid_readings >= settings.fog_on_readings:
                    fog_locked_out = now - fog_ended < settings.intake_min_on_s
                    if not fog_locked_out:
                        fog, fog_since = True, now
            now_missing = settings.missing(latest)
            _log_missing_change(missing, now_missing)
            missing = now_missing

            intake_was_on = fans is not None and fans[1]
            held = intake_was_on and now - intake_since < settings.intake_min_on_s
            if heat or held:
                now_status, now_fans = "heat" if heat else "heat_hold", (True, True)
            elif missing:
                now_status, now_fans = "failsafe", (True, False)
            elif fog:
                now_status, now_fans = "fog_hold" if fog_held else "fog", (True, False)
            else:
                now_status = "fog_lockout" if fog_locked_out else "normal"
                now_fans = (True, False)
            if now_fans[1] and not intake_was_on:
                intake_since = now
            if now_status != status:
                # Only status changes are logged: an analog speed changes every reading.
                log.info(
                    "Fans: %s, exhaust %s, intake %s (temp_diff_c=%s, humidity_pct=%s)",
                    now_status,
                    *("on" if on else "off" for on in now_fans),
                    difference,
                    humidity,
                )
                status = now_status
            fans = now_fans

            speed = settings.speed(difference)
            in_fog = status in ("fog", "fog_hold")
            speeds = (round(settings.min_duty_pct) if in_fog else speed, speed)
            targets = [speeds[i] if on else _OFF_PCT for i, on in enumerate(fans)]
            for i, target in enumerate(targets):
                if target > _OFF_PCT and (duties is None or duties[i] == _OFF_PCT):
                    # From standstill: full power first, so a low speed can't stall it.
                    kick_until[i] = now + settings.kickstart_s
            now_duties = tuple(
                _FULL_PCT if target > _OFF_PCT and now < kick_until[i] else target
                for i, target in enumerate(targets)
            )
            for i, output in enumerate((exhaust, intake)):
                if duties is None or now_duties[i] != duties[i]:
                    await asyncio.to_thread(output.set_duty, now_duties[i])
            duties = now_duties
            state.status = status
            state.exhaust_duty_pct, state.intake_duty_pct = duties
            state.temp_diff_c = difference
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
