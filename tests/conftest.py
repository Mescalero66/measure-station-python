from __future__ import annotations

from pathlib import Path

import pytest

from measure_station.clock import ClockState
from measure_station.models import Reading
from measure_station.storage import Outbox


class FakeTime:
    """Controllable wall and boot clocks plus NTP sync flag."""

    def __init__(self, wall: float = 1_000_000.0, boot: float = 100.0) -> None:
        self.wall = wall
        self.boot = boot
        self.ntp_synced = False

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.boot += seconds

    def clock_state(self, boot_id: str = "boot-A") -> ClockState:
        return ClockState(
            boot_id=boot_id,
            sync_check=lambda: self.ntp_synced,
            boot_clock_fn=lambda: self.boot,
            wall_clock_fn=lambda: self.wall,
        )

    def reading(self, source: str = "s", **values: float) -> Reading:
        return Reading(
            source=source, values=values or {"v": 1.0}, timestamp=self.wall, boot_ts=self.boot
        )


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


@pytest.fixture
def synced_clock(fake_time: FakeTime) -> ClockState:
    fake_time.ntp_synced = True
    clock = fake_time.clock_state()
    clock.poll()
    return clock


def make_outbox(path: Path, clock: ClockState, **kwargs) -> Outbox:
    options = {"max_rows": 10_000, "max_time_hold_s": 3600} | kwargs
    box = Outbox(path, clock=clock, **options)
    box.open()
    return box
