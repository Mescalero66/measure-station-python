"""The SEN6x adapter (SEN62 or SEN65), driving the real driver against a fake I2C bus.

Each open() waits through the driver's real stop (1.4 s) and reset (1.2 s) delays, and a
fan cleaning through its 10 s, so the tests open as few times as they can.
"""

import time

import pytest

from conftest import FakeMsg, install_fake_smbus2
from measure_station.sensors import Sample, create_sensor, sen6x_i2c
from measure_station.sensors.sen6x_i2c import FAN_SPEED, WARMUP, Sen6xSensor

START, STOP, DATA_READY = 0x0021, 0x0104, 0x0202  # Table 27
READ_VALUES_SEN62, READ_VALUES_SEN65 = 0x04A3, 0x0446
STATUS, RESET, FAN_CLEANING = 0xD206, 0xD304, 0x5607
PRODUCT_TYPE, PRODUCT_NAME, VOC_STATE = 0xD002, 0xD014, 0x6181
FAN_SPEED_BIT, PM_ERROR, GAS_ERROR = 1 << 21, 1 << 11, 1 << 7  # Figure 4
RHT_ERROR, FAN_ERROR = 1 << 6, 1 << 4
STICKY = PM_ERROR | GAS_ERROR | RHT_ERROR | FAN_ERROR
SEN62, SEN65 = "0x00085800", "0x00085200"  # Get Product Type (§4.8.16)


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


def _string(text: str) -> bytes:
    """A string<32> reply (§4.8.16): null-padded ASCII, as 16 words."""
    data = text.encode("ascii").ljust(32, b"\x00")
    return b"".join(_word(int.from_bytes(data[i : i + 2], "big")) for i in range(0, 32, 2))


def _args(data: bytes) -> list[int]:
    """The data words written after a command, each followed by its CRC (§4.6)."""
    assert len(data) % 3 == 0
    for i in range(0, len(data), 3):
        assert _crc8(data[i : i + 2]) == data[i + 2], "bad CRC"
    return [int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data), 3)]


def _values(pm1, pm25, pm4, pm10, rh_pct, t_c, *indices) -> list[int]:
    """Read Measured Values words (Tables 31 and 33): PM in tenths (uint16), RH in
    hundredths and T in two-hundredths (int16, so _word sends a negative as two's
    complement), then (SEN65) the VOC and NOx indices in tenths (int16)."""
    return [
        *(round(pm * 10) for pm in (pm1, pm25, pm4, pm10)),
        round(rh_pct * 100),
        round(t_c * 200),
        *(round(index * 10) for index in indices),
    ]


TYPICAL = _values(3.2, 4.1, 4.6, 4.9, 61.3, 18.42)
TYPICAL_SEN65 = _values(3.2, 4.1, 4.6, 4.9, 61.3, 18.42, 112.3, 3.6)
TYPICAL_VALUES = {
    "pm1_ugm3": 3.2,
    "pm25_ugm3": 4.1,
    "pm4_ugm3": 4.6,
    "pm10_ugm3": 4.9,
    "temperature_c": 18.42,
    "humidity_pct": 61.3,
}
TYPICAL_VALUES_SEN65 = TYPICAL_VALUES | {"voc_index": 112, "nox_index": 4}  # whole numbers
LEARNED = bytes(range(1, 9))  # a VOC algorithm state (8 bytes, opaque)


class FakeSen6xBus:
    """A SEN62 or SEN65 (``product_type``) at 0x6B: commands are written, then their
    reply is read."""

    present = True
    product_type, product_name = SEN62, "SEN62"
    latest: "FakeSen6xBus"

    def __init__(self, bus: int) -> None:
        self.gas = self.product_type == SEN65
        self.commands: list[int] = []
        self.measuring = False
        self.status = 0
        self.words = TYPICAL_SEN65 if self.gas else TYPICAL
        self.voc_state = bytes(8)  # learning from scratch
        self._voc_restore: bytes | None = None
        self.closed = False
        self._reply = b""
        FakeSen6xBus.latest = self

    def i2c_rdwr(self, *messages: FakeMsg) -> None:
        for m in messages:
            if not self.present or m.addr != 0x6B:
                raise OSError(121, "Remote I/O error")
            if not m.is_read:
                self._command(int.from_bytes(m.data[:2], "big"), _args(bytes(m.data[2:])))
            elif not self._reply:
                raise OSError(121, "Remote I/O error")  # NACK: nothing to read
            else:
                m.data[:] = self._reply[: len(m.data)]
                self._reply = b""

    def _command(self, command: int, args: list[int]) -> None:
        self.commands.append(command)
        assert not args or command == VOC_STATE, "only Set VOC Algorithm State takes data"
        read_values = READ_VALUES_SEN65 if self.gas else READ_VALUES_SEN62
        if command == STOP:
            self.measuring = False
            self.status &= ~FAN_SPEED_BIT  # cleared when leaving measurement mode
        elif command == RESET:
            assert not self.measuring, "Device Reset is idle mode only"
            self.status = 0  # same as a power cycle
            self.voc_state, self._voc_restore = bytes(8), None  # learning starts again
        elif command == START:
            assert not self.measuring, "Start is idle mode only"
            self.measuring = True
            if self._voc_restore is not None:  # applied once, at the next start (§4.8.29)
                self.voc_state, self._voc_restore = self._voc_restore, None
        elif command == FAN_CLEANING:
            assert not self.measuring, "Fan cleaning is idle mode only"
        elif command == DATA_READY:
            self._reply = _word(1 if self.measuring else 0)  # False when not measuring
        elif command == read_values and self.measuring:
            self._reply = b"".join(_word(w) for w in self.words)
        elif command == STATUS:
            self._reply = _word(self.status >> 16) + _word(self.status)
        elif command == PRODUCT_TYPE:
            self._reply = _string(self.product_type)
        elif command == PRODUCT_NAME:
            self._reply = _string(self.product_name)
        elif command == VOC_STATE and self.gas:
            if args:  # Set
                assert not self.measuring, "Set VOC Algorithm State is idle mode only"
                self._voc_restore = b"".join(w.to_bytes(2, "big") for w in args)
            else:  # Get
                state = self.voc_state
                self._reply = b"".join(_word(state[i] << 8 | state[i + 1]) for i in (0, 2, 4, 6))

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FakeSen6xBus, "present", True)
    monkeypatch.setattr(FakeSen6xBus, "product_type", SEN62)
    monkeypatch.setattr(FakeSen6xBus, "product_name", "SEN62")
    install_fake_smbus2(monkeypatch, FakeSen6xBus, "sen6x")


@pytest.fixture
def sen65(fake_bus: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FakeSen6xBus, "product_type", SEN65)
    monkeypatch.setattr(FakeSen6xBus, "product_name", "SEN65")


@pytest.fixture
def settled(monkeypatch: pytest.MonkeyPatch) -> None:
    """No warm-up to wait out after a start."""
    monkeypatch.setattr(sen6x_i2c, "_WARMUP_S", 0)


def test_meta_codes() -> None:
    assert (FAN_SPEED, WARMUP) == ("fan_speed", "warmup")


def test_fake_crc_matches_the_datasheet_example() -> None:
    assert _crc8(b"\xbe\xef") == 0x92  # §4.9


def test_first_reading_waits_until_settled(fake_bus: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sen6x_i2c, "_WARMUP_S", 0.5)  # 30 s on the real sensor
    sensor = create_sensor({"type": "sen6x", "name": "particulates", "interval_s": 60})
    assert sensor.sensor_type == "sen6x"
    sensor.open()
    opened = time.monotonic()
    assert sensor.sensor_type == "sen62"  # the detected model, published from now on
    bus = FakeSen6xBus.latest
    assert bus.commands == [STOP, RESET, PRODUCT_TYPE, START]
    assert 10 < sensor.read_timeout_s(10) <= 10.5  # the station allows for the wait

    bus.commands.clear()
    assert sensor.read() == Sample(values=TYPICAL_VALUES)  # a normal reading, no indices
    assert time.monotonic() - opened >= 0.45
    assert bus.commands == [DATA_READY, READ_VALUES_SEN62, STATUS]
    assert sensor.read_timeout_s(10) == 10  # later reads don't wait

    sensor.close()
    assert bus.closed
    sensor.close()  # safe twice


def test_decodes_values_and_flags(fake_bus: None, settled: None) -> None:
    sensor = Sen6xSensor("particulates", 60)
    sensor.open()
    bus = FakeSen6xBus.latest

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


def test_sen65_adds_the_gas_indices(
    sen65: None, settled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    sensor = create_sensor({"type": "sen6x", "name": "particulates", "interval_s": 60})
    sensor.open()
    assert sensor.sensor_type == "sen65"
    bus = FakeSen6xBus.latest
    assert bus.commands == [STOP, RESET, PRODUCT_TYPE, START]  # nothing learned to restore

    bus.commands.clear()
    # Just reset, so neither index meets its specification yet (Table 4).
    assert sensor.read() == Sample(values=TYPICAL_VALUES_SEN65, meta={"voc": WARMUP, "nox": WARMUP})
    assert bus.commands == [DATA_READY, READ_VALUES_SEN65, STATUS, VOC_STATE]

    bus.words = [0xFFFF] * 4 + [0x7FFF] * 4  # unknown (Table 33)
    assert set(sensor.read().values.values()) == {None}

    bus.words = TYPICAL_SEN65
    monkeypatch.setattr(sen6x_i2c, "_VOC_WARMUP_S", 0)  # 1 h on the real sensor
    assert sensor.read().meta == {"nox": WARMUP}
    monkeypatch.setattr(sen6x_i2c, "_NOX_WARMUP_S", 0)  # 6 h
    assert sensor.read() == Sample(values=TYPICAL_VALUES_SEN65)  # no meta when all is well
    bus.status = FAN_SPEED_BIT
    assert sensor.read().meta == {"pm": FAN_SPEED}


def test_voc_learning_survives_a_reopen(sen65: None, settled: None) -> None:
    sensor = Sen6xSensor("particulates", 60)
    sensor.open()
    bus = FakeSen6xBus.latest
    bus.voc_state = LEARNED
    sensor.read()  # saves the state
    bus.voc_state = bytes(8)
    bus.status = GAS_ERROR
    with pytest.raises(OSError, match="SEN65 reports a gas error"):
        sensor.read()  # doesn't save the state from a failed reading

    # What the station does next: close, then open again. The reset clears the learning,
    # and the saved state is put back before the measurement starts.
    sensor.close()
    sensor.open()
    bus = FakeSen6xBus.latest
    assert bus.commands == [STOP, RESET, PRODUCT_TYPE, VOC_STATE, START]
    assert bus.voc_state == LEARNED
    assert sensor.read().meta == {"nox": WARMUP}  # the VOC index needn't learn again


def test_sticky_errors_raise_and_a_reopen_clears_them(fake_bus: None, settled: None) -> None:
    sensor = Sen6xSensor("particulates", 60)
    sensor.open()
    bus = FakeSen6xBus.latest
    errors = [(FAN_ERROR, "fan"), (PM_ERROR, "PM"), (GAS_ERROR, "gas"), (RHT_ERROR, "RH&T")]
    for flag, name in errors:
        bus.status = flag | FAN_SPEED_BIT
        with pytest.raises(OSError, match=f"SEN62 reports a {name} error"):
            sensor.read()
    bus.status = STICKY
    with pytest.raises(OSError, match="fan and PM and gas and RH&T error"):
        sensor.read()

    # What the station does next: close, then open again, which resets the sensor.
    sensor.close()
    sensor.open()
    assert FakeSen6xBus.latest.commands == [STOP, RESET, PRODUCT_TYPE, START]
    assert sensor.read() == Sample(values=TYPICAL_VALUES)


def test_fan_cleaning(fake_bus: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sen6x_i2c, "_WARMUP_S", 0.3)
    sensor = Sen6xSensor("particulates", 60, fan_cleaning_interval_h=1e-9)  # due at once
    sensor.open()
    bus = FakeSen6xBus.latest

    bus.commands.clear()
    assert sensor.read() == Sample(values=TYPICAL_VALUES)  # the first read; cleaning now due
    assert bus.commands == [DATA_READY, READ_VALUES_SEN62, STATUS]
    assert sensor.read_timeout_s(10) == 10 + sen6x_i2c._CLEANING_S + 0.3

    bus.commands.clear()
    # Cleans, restarts, and waits out the warm-up, so this reading is a settled one.
    assert sensor.read() == Sample(values=TYPICAL_VALUES)
    assert bus.commands == [STOP, FAN_CLEANING, START, DATA_READY, READ_VALUES_SEN62, STATUS]


def test_stopped_sensor_raises_so_it_is_restarted(
    fake_bus: None, settled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sen6x_i2c, "_READY_TIMEOUT_S", 0.1)
    monkeypatch.setattr(sen6x_i2c, "_READY_POLL_S", 0.02)
    sensor = Sen6xSensor("particulates", 60)
    sensor.open()
    FakeSen6xBus.latest.measuring = False  # e.g. back to idle after a brown-out
    with pytest.raises(OSError, match="no measurement ready"):
        sensor.read()


def test_other_models_fail_to_open(fake_bus: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FakeSen6xBus, "product_type", "0x00085300")  # §4.8.16
    monkeypatch.setattr(FakeSen6xBus, "product_name", "SEN66")
    sensor = Sen6xSensor("particulates", 60)
    with pytest.raises(RuntimeError, match="SEN66"):
        sensor.open()
    sensor.close()
    assert FakeSen6xBus.latest.closed


def test_missing_sensor_fails_to_open(fake_bus: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FakeSen6xBus, "present", False)
    sensor = Sen6xSensor("particulates", 60)
    with pytest.raises(OSError):
        sensor.open()
    sensor.close()
    assert FakeSen6xBus.latest.closed
    sensor.close()  # safe twice


@pytest.mark.parametrize("hours", [0, -1])
def test_bad_options_rejected(hours: float) -> None:
    with pytest.raises(ValueError):
        Sen6xSensor("particulates", 60, fan_cleaning_interval_h=hours)
