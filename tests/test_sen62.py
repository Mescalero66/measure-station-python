"""The SEN62 adapter, driving the real driver against a fake I2C bus.

Each open() waits through the driver's real stop (1.4 s) and reset (1.2 s) delays, and a
fan cleaning through its 10 s, so the tests open as few times as they can.
"""

import time

import pytest

from conftest import FakeMsg, install_fake_smbus2
from measure_station.sensors import Sample, create_sensor, sen62_i2c
from measure_station.sensors.sen62_i2c import FAN_SPEED, Sen62Sensor

START, STOP, DATA_READY, READ_VALUES = 0x0021, 0x0104, 0x0202, 0x04A3  # Table 27
STATUS, RESET, FAN_CLEANING = 0xD206, 0xD304, 0x5607
FAN_SPEED_BIT, PM_ERROR, RHT_ERROR, FAN_ERROR = 1 << 21, 1 << 11, 1 << 6, 1 << 4  # Figure 4
STICKY = PM_ERROR | RHT_ERROR | FAN_ERROR


def _crc8(data: bytes) -> int:
    crc = 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _word(value: int) -> bytes:
    data = (value & 0xFFFF).to_bytes(2, "big")
    return data + bytes((_crc8(data),))


def _values(pm1, pm25, pm4, pm10, rh_pct, t_c) -> list[int]:
    """Read Measured Values words (Table 31): PM in tenths (uint16), RH in hundredths and T
    in two-hundredths (int16, so _word sends a negative as two's complement)."""
    return [
        *(round(pm * 10) for pm in (pm1, pm25, pm4, pm10)),
        round(rh_pct * 100),
        round(t_c * 200),
    ]


TYPICAL = _values(3.2, 4.1, 4.6, 4.9, 61.3, 18.42)
TYPICAL_VALUES = {
    "pm1_ugm3": 3.2,
    "pm25_ugm3": 4.1,
    "pm4_ugm3": 4.6,
    "pm10_ugm3": 4.9,
    "temperature_c": 18.42,
    "humidity_pct": 61.3,
}


class FakeSen62Bus:
    """A SEN62 at 0x6B: commands are written, then their reply is read."""

    present = True
    latest: "FakeSen62Bus"

    def __init__(self, bus: int) -> None:
        self.commands: list[int] = []
        self.measuring = False
        self.status = 0
        self.words = TYPICAL
        self.closed = False
        self._reply = b""
        FakeSen62Bus.latest = self

    def i2c_rdwr(self, *messages: FakeMsg) -> None:
        for m in messages:
            if not self.present or m.addr != 0x6B:
                raise OSError(121, "Remote I/O error")
            if not m.is_read:
                assert len(m.data) == 2, "these commands take no data"
                self._command(int.from_bytes(m.data, "big"))
            elif not self._reply:
                raise OSError(121, "Remote I/O error")  # NACK: nothing to read
            else:
                m.data[:] = self._reply[: len(m.data)]
                self._reply = b""

    def _command(self, command: int) -> None:
        self.commands.append(command)
        if command == STOP:
            self.measuring = False
            self.status &= ~FAN_SPEED_BIT  # cleared when leaving measurement mode
        elif command == RESET:
            assert not self.measuring, "Device Reset is idle mode only"
            self.status = 0  # same as a power cycle
        elif command == START:
            assert not self.measuring, "Start is idle mode only"
            self.measuring = True
        elif command == FAN_CLEANING:
            assert not self.measuring, "Fan cleaning is idle mode only"
        elif command == DATA_READY:
            self._reply = _word(1 if self.measuring else 0)  # False when not measuring
        elif command == READ_VALUES and self.measuring:
            self._reply = b"".join(_word(w) for w in self.words)
        elif command == STATUS:
            self._reply = _word(self.status >> 16) + _word(self.status)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FakeSen62Bus, "present", True)
    install_fake_smbus2(monkeypatch, FakeSen62Bus, "sen62")


@pytest.fixture
def settled(monkeypatch: pytest.MonkeyPatch) -> None:
    """No warm-up to wait out after a start."""
    monkeypatch.setattr(sen62_i2c, "_WARMUP_S", 0)


def test_meta_codes() -> None:
    assert FAN_SPEED == "fan_speed"


def test_fake_crc_matches_the_datasheet_example() -> None:
    assert _crc8(b"\xbe\xef") == 0x92  # §4.9


def test_first_reading_waits_until_settled(fake_bus: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sen62_i2c, "_WARMUP_S", 0.5)  # 30 s on the real sensor
    sensor = create_sensor({"type": "sen62", "name": "particulates", "interval_s": 60})
    assert sensor.sensor_type == "sen62"
    sensor.open()
    opened = time.monotonic()
    bus = FakeSen62Bus.latest
    assert bus.commands == [STOP, RESET, START]
    assert 10 < sensor.read_timeout_s(10) <= 10.5  # the station allows for the wait

    bus.commands.clear()
    assert sensor.read() == Sample(values=TYPICAL_VALUES)  # a normal reading
    assert time.monotonic() - opened >= 0.45
    assert bus.commands == [DATA_READY, READ_VALUES, STATUS]
    assert sensor.read_timeout_s(10) == 10  # later reads don't wait

    sensor.close()
    assert bus.closed
    sensor.close()  # safe twice


def test_decodes_values_and_flags(fake_bus: None, settled: None) -> None:
    sensor = Sen62Sensor("particulates", 60)
    sensor.open()
    bus = FakeSen62Bus.latest

    assert sensor.read() == Sample(values=TYPICAL_VALUES)  # no meta when all is well

    bus.words = _values(152.3, 310.0, 402.6, 455.1, 95.0, -5.5)  # smoky, cold, damp
    assert sensor.read().values == {
        "pm1_ugm3": 152.3,
        "pm25_ugm3": 310.0,
        "pm4_ugm3": 402.6,
        "pm10_ugm3": 455.1,
        "temperature_c": -5.5,  # int16, two's complement
        "humidity_pct": 95.0,
    }

    bus.words = [0xFFFF] * 4 + [0x7FFF] * 2  # unknown (Table 31)
    assert set(sensor.read().values.values()) == {None}

    bus.words = TYPICAL
    bus.status = FAN_SPEED_BIT
    assert sensor.read().meta == {"pm": FAN_SPEED}


def test_sticky_errors_raise_and_a_reopen_clears_them(fake_bus: None, settled: None) -> None:
    sensor = Sen62Sensor("particulates", 60)
    sensor.open()
    bus = FakeSen62Bus.latest
    for flag, name in [(FAN_ERROR, "fan"), (PM_ERROR, "PM"), (RHT_ERROR, "RH&T")]:
        bus.status = flag | FAN_SPEED_BIT
        with pytest.raises(OSError, match=f"a {name} error"):
            sensor.read()
    bus.status = STICKY
    with pytest.raises(OSError, match="fan and PM and RH&T error"):
        sensor.read()

    # What the station does next: close, then open again, which resets the sensor.
    sensor.close()
    sensor.open()
    assert FakeSen62Bus.latest.commands == [STOP, RESET, START]
    assert sensor.read() == Sample(values=TYPICAL_VALUES)


def test_fan_cleaning(fake_bus: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sen62_i2c, "_WARMUP_S", 0.3)
    sensor = Sen62Sensor("particulates", 60, fan_cleaning_interval_h=1e-9)  # due at once
    sensor.open()
    bus = FakeSen62Bus.latest

    bus.commands.clear()
    assert sensor.read() == Sample(values=TYPICAL_VALUES)  # the first read; cleaning now due
    assert bus.commands == [DATA_READY, READ_VALUES, STATUS]
    assert sensor.read_timeout_s(10) == 10 + sen62_i2c._CLEANING_S + 0.3

    bus.commands.clear()
    # Cleans, restarts, and waits out the warm-up, so this reading is a settled one.
    assert sensor.read() == Sample(values=TYPICAL_VALUES)
    assert bus.commands == [STOP, FAN_CLEANING, START, DATA_READY, READ_VALUES, STATUS]


def test_stopped_sensor_raises_so_it_is_restarted(
    fake_bus: None, settled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sen62_i2c, "_READY_TIMEOUT_S", 0.1)
    monkeypatch.setattr(sen62_i2c, "_READY_POLL_S", 0.02)
    sensor = Sen62Sensor("particulates", 60)
    sensor.open()
    FakeSen62Bus.latest.measuring = False  # e.g. back to idle after a brown-out
    with pytest.raises(OSError, match="no measurement ready"):
        sensor.read()


def test_missing_sensor_fails_to_open(fake_bus: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FakeSen62Bus, "present", False)
    sensor = Sen62Sensor("particulates", 60)
    with pytest.raises(OSError):
        sensor.open()
    sensor.close()
    assert FakeSen62Bus.latest.closed
    sensor.close()  # safe twice


@pytest.mark.parametrize("hours", [0, -1])
def test_bad_options_rejected(hours: float) -> None:
    with pytest.raises(ValueError):
        Sen62Sensor("particulates", 60, fan_cleaning_interval_h=hours)
