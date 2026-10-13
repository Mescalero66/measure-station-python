"""Decoder for the MQ gas sensor board's ``$PMQB`` sentences (see the board's INTERFACE.md).

The board broadcasts the latest reading of each of its six sensors every ~10 s, as
NMEA-style sentences::

    $PMQB,<name>,<t_s>,<gas>,<ppm>,<temp_c>,<tracking>,<r0>*<CS>

A reading is identified by its sensor ``name`` and ``t_s`` (board uptime in seconds
when it was taken), and is repeated ~15 times until that sensor is read again. This
decoder:

* validates and parses sentences (checksum, address, fields; empty field = null);
* emits each reading once, remembering the last ``t_s`` per sensor across station
  restarts (via :class:`SeenMarker`, saved in the outbox with the reading itself);
* detects board restarts (``t_s`` goes backwards, or a sensor's ``t_s`` goes empty);
* timestamps readings by estimating when the board booted (see
  :class:`BoardBootEstimator`), holding readings until a trustworthy estimate exists;
* watches for stale sensors and a silent link.
"""

from __future__ import annotations

import logging
import math
import re
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from functools import reduce
from operator import xor
from typing import Any, NamedTuple

from measure_station.models import Reading, SeenMarker

log = logging.getLogger(__name__)

ADDRESS = "PMQB"
# In broadcast order. Sentences with any other name are ignored, as the spec requires.
KNOWN_SENSORS = ("MQ2", "MQ4", "MQ5", "MQ8", "MQ135", "MQ7")

_CHECKSUM = re.compile(r"[0-9A-Fa-f]{2}")


class ParseError(ValueError):
    """A line is corrupt (bad framing, checksum or field)."""


class Received(NamedTuple):
    """When a line arrived, on both station clocks."""

    boot_ts: float
    wall_ts: float


class PmqbSentence(NamedTuple):
    name: str
    t_s: int | None
    gas: str | None
    ppm: float | None
    temp_c: float | None
    tracking: bool | None
    r0: float | None


def nmea_checksum(body: str) -> int:
    """XOR of every character between ``$`` and ``*``."""
    return reduce(xor, body.encode("ascii"), 0)


def parse_sentence(line: bytes) -> PmqbSentence | None:
    """Parse one line.

    Returns None for a valid sentence with another address. Raises ParseError if the
    line is corrupt.
    """
    # From the LAST '$': no field ever contains one, so this recovers a sentence that
    # follows line noise which happens to include a '$'.
    start = line.rfind(b"$")
    if start < 0:
        raise ParseError("no '$' start marker")
    try:
        text = line[start:].decode("ascii").strip()
    except UnicodeDecodeError:
        raise ParseError("non-ASCII bytes") from None
    star = text.find("*")
    if star < 0 or not _CHECKSUM.fullmatch(text[star + 1 :]):
        raise ParseError("missing or malformed checksum")
    body = text[1:star]
    if nmea_checksum(body) != int(text[star + 1 :], 16):
        raise ParseError("checksum mismatch")

    fields = body.split(",")
    if fields[0] != ADDRESS:
        return None
    if len(fields) < 8:
        raise ParseError(f"expected 7 fields, got {len(fields) - 1}")
    name, t_s, gas, ppm, temp_c, tracking, r0 = fields[1:8]  # extra fields are ignored
    if not name:
        raise ParseError("empty sensor name")
    if tracking not in ("", "0", "1"):
        raise ParseError(f"tracking must be 0 or 1, got {tracking!r}")
    return PmqbSentence(
        name=name,
        t_s=_optional_int(t_s),
        gas=gas or None,
        ppm=_optional_float(ppm),
        temp_c=_optional_float(temp_c),
        tracking=None if tracking == "" else tracking == "1",
        r0=_optional_float(r0),
    )


def _optional_int(field: str) -> int | None:
    if not field:
        return None
    try:
        return int(field)
    except ValueError:
        raise ParseError(f"not an integer: {field!r}") from None


def _optional_float(field: str) -> float | None:
    if not field:
        return None
    try:
        value = float(field)
    except ValueError:
        raise ParseError(f"not a number: {field!r}") from None
    # The spec: reject non-numeric values such as inf or nan (as "no value").
    return value if math.isfinite(value) else None


class BoardBootEstimator:
    """Estimates when the board booted, in station boot-clock seconds.

    Every new reading bounds it: the board booted no later than ``received - t_s``,
    because a reading is always broadcast after it was taken. The minimum over recent
    samples is the estimate; with a few dozen samples whose broadcast delays are
    spread over 0-10 s, it lands within a fraction of a second.

    Only *fresh* samples count: a ``t_s`` change seen within one broadcast of the
    previous sentence for that sensor, so its delay is known to be small. A reading
    that was already on the line when the station started may be up to 150 s old.

    The minimum is taken over a sliding window, so the two clocks drifting apart over
    weeks of uptime can't accumulate into a stale estimate.
    """

    def __init__(self, window_s: float) -> None:
        self._window_s = window_s
        self._samples: deque[tuple[float, float]] = deque()  # (received, received - t_s)

    @property
    def ready(self) -> bool:
        return bool(self._samples)

    @property
    def estimate(self) -> float | None:
        return min(bound for _, bound in self._samples) if self._samples else None

    def add(self, received: float, t_s: int) -> None:
        self._samples.append((received, received - t_s))
        # Keep at least the newest sample, so a long link outage doesn't discard the
        # estimate and hold readings again.
        while len(self._samples) > 1 and self._samples[0][0] < received - self._window_s:
            self._samples.popleft()

    def reset(self) -> None:
        self._samples.clear()


@dataclass
class _SensorState:
    last_t_s: int | None = None  # None: not read since (our knowledge of) the board's boot
    last_sentence_at: float | None = None  # any sentence for this sensor
    last_new_at: float | None = None  # last t_s change, for stale detection
    stale: bool = False


@dataclass(frozen=True)
class _Held:
    sentence: PmqbSentence
    received: Received
    board_epoch: int  # board boots seen since the station started


class MqBoardDecoder:
    """Turns received lines into readings: one per new sensor reading, accurately timed.

    Lives for the whole station run (across serial port re-opens), so its state
    survives a reconnect.
    """

    def __init__(
        self,
        *,
        last_seen: Mapping[str, int] | None = None,
        source_prefix: str = "mqboard-",
        fresh_gap_s: float = 15.0,
        estimate_window_s: float = 3600.0,
        max_hold_s: float = 300.0,
        stale_sensor_s: float = 300.0,
        link_down_s: float = 30.0,
        started_at: float | None = None,
    ) -> None:
        last_seen = last_seen or {}
        self.bad_lines = 0  # incremented by the reader for every discarded line
        self.last_valid_at: float | None = None  # station boot clock
        self._sensors = {name: _SensorState(last_t_s=last_seen.get(name)) for name in KNOWN_SENSORS}
        self._source_prefix = source_prefix
        self._fresh_gap_s = fresh_gap_s
        self._max_hold_s = max_hold_s
        self._stale_sensor_s = stale_sensor_s
        self._link_down_s = link_down_s
        self._estimator = BoardBootEstimator(estimate_window_s)
        self._held: list[_Held] = []
        self._board_epoch = 0
        self._started_at = started_at
        self._link_down = False
        self._unknown_names: set[str] = set()

    def source_for(self, name: str) -> str:
        return f"{self._source_prefix}{name.lower()}"

    def sources(self) -> list[str]:
        return [self.source_for(name) for name in KNOWN_SENSORS]

    # ------------------------------------------------------------------ decoding

    def feed(self, line: bytes, received: Received) -> list[Reading]:
        """Decode one line. Raises :class:`ParseError` if it is corrupt."""
        sentence = parse_sentence(line)
        if sentence is None:
            return []
        now = received.boot_ts
        self.last_valid_at = now
        state = self._sensors.get(sentence.name)
        if state is None:
            if sentence.name not in self._unknown_names:
                self._unknown_names.add(sentence.name)
                log.info("MQ board: ignoring unknown sensor %r", sentence.name)
            return []

        previous_sentence_at, state.last_sentence_at = state.last_sentence_at, now
        if state.last_new_at is None:
            state.last_new_at = now  # start the stale timer at first sight

        t_s = sentence.t_s
        if t_s is None:  # sensor not read since the board booted
            if state.last_t_s is not None:
                self._board_restarted(f"{sentence.name} has no reading any more", now)
            return []
        if state.last_t_s is not None:
            if t_s == state.last_t_s:
                return []  # repeat of a reading already recorded
            if t_s < state.last_t_s:
                self._board_restarted(
                    f"{sentence.name} t_s went from {state.last_t_s} to {t_s}", now
                )

        state.last_t_s = t_s
        state.last_new_at = now
        if state.stale:
            state.stale = False
            log.info("MQ board: sensor %s is updating again", sentence.name)

        if previous_sentence_at is not None and now - previous_sentence_at <= self._fresh_gap_s:
            self._estimator.add(now, t_s)
        self._held.append(_Held(sentence, received, self._board_epoch))
        return self._release(now)

    def check(self, now_boot: float) -> list[Reading]:
        """Periodic housekeeping, called about once a second. May release readings."""
        self._check_link(now_boot)
        if not self._link_down:
            self._check_sensors(now_boot)
        return self._release(now_boot)

    def status(self, now_boot: float) -> dict[str, Any]:
        """Values for the station health report."""
        last = self.last_valid_at
        return {
            "up": not self._link_down,
            "last_line_age_s": None if last is None else round(now_boot - last, 1),
            "bad_lines": self.bad_lines,
            "stale_sensors": [n for n, s in self._sensors.items() if s.stale],
            "held": len(self._held),
        }

    # ------------------------------------------------------------------ internals

    def _board_restarted(self, reason: str, now: float) -> None:
        log.warning("MQ board: restart detected (%s)", reason)
        self._board_epoch += 1
        self._estimator.reset()
        # Every sensor restarts with the board: old t_s values no longer apply, and
        # each sensor gets a full cycle to deliver its first reading before it's stale.
        for state in self._sensors.values():
            state.last_t_s = None
            state.last_new_at = now

    def _release(self, now: float) -> list[Reading]:
        """Emit held readings that can now be timestamped."""
        released: list[Reading] = []
        still_held: list[_Held] = []
        estimate = self._estimator.estimate
        for held in self._held:
            if held.board_epoch != self._board_epoch:
                # The board restarted before its boot time could be estimated.
                log.warning(
                    "MQ board: %s reading from before a board restart timestamped on receipt",
                    held.sentence.name,
                )
                released.append(self._reading(held, captured_at=None))
            elif estimate is not None:
                released.append(self._reading(held, captured_at=estimate + held.sentence.t_s))
            elif now - held.received.boot_ts > self._max_hold_s:
                log.warning(
                    "MQ board: no clock estimate after %.0f s; %s reading timestamped on receipt",
                    self._max_hold_s,
                    held.sentence.name,
                )
                released.append(self._reading(held, captured_at=None))
            else:
                still_held.append(held)
        self._held = still_held
        return released

    def _reading(self, held: _Held, *, captured_at: float | None) -> Reading:
        received = held.received
        basis = "board" if captured_at is not None else "receipt"
        # Never after it was received (guards against a loose estimate).
        captured = min(
            captured_at if captured_at is not None else received.boot_ts, received.boot_ts
        )
        s = held.sentence
        return Reading(
            source=self.source_for(s.name),
            values={
                "gas": s.gas,
                "ppm": s.ppm,
                "temp_c": s.temp_c,
                "tracking": s.tracking,
                "r0": s.r0,
                "board_t_s": s.t_s,
                "ts_basis": basis,
            },
            timestamp=received.wall_ts - (received.boot_ts - captured),
            boot_ts=captured,
            seen=SeenMarker(s.name, s.t_s),
        )

    def _check_link(self, now: float) -> None:
        if self._started_at is None:
            self._started_at = now
        last = self.last_valid_at if self.last_valid_at is not None else self._started_at
        down = now - last > self._link_down_s
        if down and not self._link_down:
            log.warning("MQ board: no valid sentences for %.0f s; board or link down", now - last)
        elif self._link_down and not down:
            log.info("MQ board: receiving sentences again")
            # Sensors couldn't update while the link was down; restart their timers.
            for state in self._sensors.values():
                if state.last_new_at is not None:
                    state.last_new_at = now
        self._link_down = down

    def _check_sensors(self, now: float) -> None:
        for name, state in self._sensors.items():
            if state.stale or state.last_new_at is None:
                continue
            if now - state.last_new_at > self._stale_sensor_s:
                state.stale = True
                log.warning(
                    "MQ board: sensor %s has not updated for %.0f s",
                    name,
                    now - state.last_new_at,
                )
