"""Exponential backoff with jitter, used for task restarts and MQTT reconnects."""

from __future__ import annotations

import random


class ExponentialBackoff:
    """Produces delays of initial, initial*factor, ... capped at ``maximum_s``.

    Jitter (+/- ``jitter`` fraction) avoids synchronised retry storms when several
    tasks fail at the same moment (e.g. a shared I2C bus glitch).
    """

    def __init__(
        self,
        initial_s: float,
        maximum_s: float,
        *,
        factor: float = 2.0,
        jitter: float = 0.1,
    ) -> None:
        if initial_s <= 0 or maximum_s < initial_s:
            raise ValueError("require 0 < initial_s <= maximum_s")
        self._initial_s = initial_s
        self._maximum_s = maximum_s
        self._factor = factor
        self._jitter = jitter
        self._attempt = 0

    def next_delay(self) -> float:
        base = min(self._maximum_s, self._initial_s * self._factor**self._attempt)
        self._attempt += 1
        jittered = base * (1 + random.uniform(-self._jitter, self._jitter))
        return min(self._maximum_s, max(0.0, jittered))

    def reset(self) -> None:
        self._attempt = 0
