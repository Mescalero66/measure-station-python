"""Core data types: a measurement reading and its MQTT wire encoding."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, NamedTuple

from measure_station.clock import clock_is_synchronised


@dataclass(frozen=True, slots=True)
class Reading:
    """A set of values captured from one source at one instant."""

    source: str
    values: Mapping[str, Any]
    timestamp: float = field(default_factory=time.time)
    # False if the system clock had not yet been NTP-synchronised when this was taken
    # (the Pi has no RTC, so after an offline boot timestamps may be wrong).
    clock_synced: bool = field(default_factory=clock_is_synchronised)


class OutboxMessage(NamedTuple):
    """An encoded message ready to be stored and published."""

    topic: str
    payload: str
    created_at: float


class MessageEncoder:
    """Maps readings to MQTT topics and JSON payloads."""

    def __init__(self, base_topic: str, station_id: str) -> None:
        self._station_id = station_id
        self._prefix = f"{base_topic.strip('/')}/{station_id}"

    @property
    def status_topic(self) -> str:
        return f"{self._prefix}/status"

    def topic_for(self, source: str) -> str:
        return f"{self._prefix}/{source}"

    def encode(self, reading: Reading) -> OutboxMessage:
        payload = {
            "station": self._station_id,
            "source": reading.source,
            "ts": datetime.fromtimestamp(reading.timestamp, UTC).isoformat(timespec="milliseconds"),
            "clock_synced": reading.clock_synced,
            "values": _json_safe(reading.values),
        }
        return OutboxMessage(
            topic=self.topic_for(reading.source),
            payload=json.dumps(payload, separators=(",", ":"), allow_nan=False, default=str),
            created_at=reading.timestamp,
        )


def _json_safe(value: Any) -> Any:
    """Replace NaN/inf (not valid JSON) with None, recursively."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    return value
