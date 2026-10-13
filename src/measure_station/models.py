"""Core data types: a measurement reading, its stored form, and its MQTT encoding."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, NamedTuple

from measure_station.clock import TimeQuality, boot_clock


@dataclass(frozen=True, slots=True)
class Reading:
    """A set of values captured from one source at one instant.

    Both clocks are captured: ``timestamp`` (wall clock, may be wrong before NTP sync)
    and ``boot_ts`` (seconds since boot, never jumps), so the timestamp can be
    corrected later. See :mod:`measure_station.clock`.
    """

    source: str
    values: Mapping[str, Any]
    timestamp: float = field(default_factory=time.time)
    boot_ts: float = field(default_factory=boot_clock)


class StoredReading(NamedTuple):
    """A reading as held in the outbox."""

    id: int
    source: str
    values_json: str
    ts: float
    ts_quality: str
    boot_id: str
    boot_ts: float


def encode_values(values: Mapping[str, Any]) -> str:
    return json.dumps(_json_safe(values), separators=(",", ":"), allow_nan=False, default=str)


class MessageEncoder:
    """Maps stored readings to MQTT topics and JSON payloads."""

    def __init__(self, base_topic: str, station_id: str) -> None:
        self._station_id = station_id
        self._prefix = f"{base_topic.strip('/')}/{station_id}"

    @property
    def status_topic(self) -> str:
        return f"{self._prefix}/status"

    def topic_for(self, source: str) -> str:
        return f"{self._prefix}/{source}"

    def encode(self, reading: StoredReading) -> tuple[str, str]:
        """Return ``(topic, payload)``."""
        payload: dict[str, Any] = {
            "station": self._station_id,
            "source": reading.source,
            "ts": datetime.fromtimestamp(reading.ts, UTC).isoformat(timespec="milliseconds"),
            "ts_quality": _published_quality(reading.ts_quality),
            "values": json.loads(reading.values_json),
        }
        if reading.ts_quality == TimeQuality.PENDING:
            # Uncorrectable on the station; include what a consumer would need to fix
            # it later from another reading of the same boot with a known time.
            payload["boot_id"] = reading.boot_id
            payload["boot_ts"] = round(reading.boot_ts, 3)
        return (
            self.topic_for(reading.source),
            json.dumps(payload, separators=(",", ":"), allow_nan=False, default=str),
        )


def _published_quality(stored: str) -> str:
    # A reading released while still uncorrected carries the unsynchronised wall clock.
    return "estimated" if stored == TimeQuality.PENDING else stored


def _json_safe(value: Any) -> Any:
    """Replace NaN/inf (not valid JSON) with None, recursively."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    return value
