from __future__ import annotations

import importlib
import sys
import types
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
            source=source,
            sensor_type="simulated",
            values=values or {"v": 1.0},
            timestamp=self.wall,
            boot_ts=self.boot,
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


# --------------------------------------------------------------------------- fake I2C
# Drivers from drivers-repo talk to the bus through smbus2's i2c_rdwr. These replace
# smbus2 so the real driver can run against a fake chip, without hardware or smbus2.


class FakeMsg:
    """An smbus2 i2c_msg: the bus fills in ``data`` for a read."""

    def __init__(self, addr: int, data: bytes, is_read: bool) -> None:
        self.addr, self.data, self.is_read = addr, bytearray(data), is_read

    def __iter__(self):
        return iter(self.data)

    def __bytes__(self) -> bytes:
        return bytes(self.data)


class FakeI2cMsg:
    @staticmethod
    def read(addr: int, n: int) -> FakeMsg:
        return FakeMsg(addr, bytes(n), True)

    @staticmethod
    def write(addr: int, data) -> FakeMsg:
        return FakeMsg(addr, bytes(data), False)


def install_fake_smbus2(monkeypatch: pytest.MonkeyPatch, bus_class: type, driver: str) -> None:
    """Make ``measure_station.drivers.<driver>`` use ``bus_class`` as its SMBus."""
    fake = types.ModuleType("smbus2")
    fake.SMBus = bus_class
    fake.i2c_msg = FakeI2cMsg
    monkeypatch.setitem(sys.modules, "smbus2", fake)
    module = importlib.import_module(f"measure_station.drivers.{driver}")
    # Also when the driver was already imported, e.g. with the real smbus2 on a Pi.
    monkeypatch.setattr(module, "SMBus", bus_class)
    monkeypatch.setattr(module, "i2c_msg", FakeI2cMsg)
