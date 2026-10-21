"""The SCD41 adapter, driving the real driver against a fake I2C bus."""

import pytest

from conftest import FakeMsg, install_fake_smbus2
from measure_station.sensors import LatestValues, Sample, create_sensor, scd41_i2c
from measure_station.sensors.scd41_i2c import ALTITUDE, LIVE, Scd41Sensor, altitude_pressure_hpa

WAKE_UP, STOP, REINIT, START = 0x36F6, 0x3F86, 0x3646, 0x21B1
DATA_READY, READ_MEASUREMENT = 0xE4B8, 0xEC05
GET_TEMP_OFFSET, SET_TEMP_OFFSET = 0x2318, 0x241D
GET_ASC, SET_ASC = 0x2313, 0x2416
AMBIENT_PRESSURE = 0xE000


def _crc8(data: bytes) -> int:
    crc = 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _word(value: int) -> bytes:
    data = value.to_bytes(2, "big")
    return data + bytes((_crc8(data),))


# The datasheet's read_measurement example words (§3.6.2): 500 ppm, 25 °C, 37 %RH.
MEASUREMENT = _word(0x01F4) + _word(0x6667) + _word(0x5EB9)
FOUR_DEGREES = round(4 * 65535 / 175)  # the factory temperature offset


class FakeScd41Bus:
    """An SCD41 at 0x62: commands are written, then their reply is read."""

    present = True
    latest: "FakeScd41Bus"

    def __init__(self, bus: int) -> None:
        self.commands: list[int] = []
        self.measuring = False
        self.temp_offset = FOUR_DEGREES
        self.asc = 1
        self.pressures_hpa: list[int] = []  # every set_ambient_pressure
        self.closed = False
        self._reply = b""
        FakeScd41Bus.latest = self

    def i2c_rdwr(self, *messages: FakeMsg) -> None:
        for m in messages:
            if not self.present or m.addr != 0x62:
                raise OSError(121, "Remote I/O error")
            if not m.is_read:
                arg = None
                if len(m.data) == 5:  # command, data word, CRC
                    assert _crc8(m.data[2:4]) == m.data[4], "bad CRC from the driver"
                    arg = int.from_bytes(m.data[2:4], "big")
                self._command(int.from_bytes(m.data[:2], "big"), arg)
            elif not self._reply:
                raise OSError(121, "Remote I/O error")  # NACK: nothing to read
            else:
                m.data[:] = self._reply[: len(m.data)]
                self._reply = b""

    def _command(self, command: int, arg: int | None) -> None:
        self.commands.append(command)
        if command == WAKE_UP:
            raise OSError(121, "Remote I/O error")  # the real sensor doesn't ACK it
        if command in (STOP, START):
            self.measuring = command == START
        elif command == DATA_READY:
            self._reply = _word(0x8006 if self.measuring else 0x8000)
        elif command == READ_MEASUREMENT and self.measuring:
            self._reply = MEASUREMENT
        elif command == GET_TEMP_OFFSET:
            self._reply = _word(self.temp_offset)
        elif command == SET_TEMP_OFFSET:
            self.temp_offset = arg
        elif command == GET_ASC:
            self._reply = _word(self.asc)
        elif command == SET_ASC:
            self.asc = arg
        elif command == AMBIENT_PRESSURE and arg is not None:
            self.pressures_hpa.append(arg)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FakeScd41Bus, "present", True)
    install_fake_smbus2(monkeypatch, FakeScd41Bus, "scd41")


def _opened(**options) -> Scd41Sensor:
    sensor = Scd41Sensor("co2", 60, **options)
    sensor.latest_values = LatestValues()
    sensor.open()
    return sensor


def test_comp_src_codes() -> None:
    assert (LIVE, ALTITUDE) == ("hpa", "alt")


def test_fake_crc_matches_the_datasheet_example() -> None:
    assert _crc8(b"\xbe\xef") == 0x92  # §3.12


def test_reads_the_datasheet_example(fake_bus: None) -> None:
    sensor = create_sensor({"type": "scd41", "name": "co2", "interval_s": 60})
    assert sensor.sensor_type == "scd41"
    sensor.open()
    assert sensor.read() == Sample(
        values={"co2_ppm": 500, "temperature_c": 25.0, "humidity_pct": 37.0},
        meta={"pressure_hpa": 1013, "comp_src": "alt"},
    )
    sensor.close()
    assert FakeScd41Bus.latest.closed
    sensor.close()  # safe twice


def test_factory_settings_are_not_rewritten(fake_bus: None) -> None:
    _opened()
    commands = FakeScd41Bus.latest.commands
    assert commands[:3] == [WAKE_UP, STOP, REINIT]
    assert commands[-1] == START
    assert SET_TEMP_OFFSET not in commands and SET_ASC not in commands


def test_configured_settings_are_applied_before_measuring(fake_bus: None) -> None:
    _opened(temperature_offset_c=2.5, asc_enabled=False)
    bus = FakeScd41Bus.latest
    assert bus.temp_offset == round(2.5 * 65535 / 175)
    assert bus.asc == 0
    assert bus.commands.index(SET_ASC) < bus.commands.index(START)


def test_altitude_fallback_pressure(fake_bus: None) -> None:
    assert round(altitude_pressure_hpa(0), 2) == 1013.25
    assert round(altitude_pressure_hpa(1000)) == 899  # ISA: 898.75 hPa
    sensor = _opened(altitude_m=1000)
    assert FakeScd41Bus.latest.pressures_hpa == [899]
    assert sensor.read().meta == {"pressure_hpa": 899, "comp_src": ALTITUDE}


def test_live_pressure_is_used_and_reported_for_the_next_reading(fake_bus: None) -> None:
    sensor = _opened(pressure_source="barometer", altitude_m=1000)
    sensor.latest_values.record("barometer", {"pressure_hpa": 1006.53}, valid_for_s=60)

    # Measured with the pressure applied at open (no barometer value yet); the live
    # pressure is sent afterwards, for the following measurements.
    assert sensor.read().meta == {"pressure_hpa": 899, "comp_src": ALTITUDE}
    assert FakeScd41Bus.latest.pressures_hpa == [899, 1007]

    assert sensor.read().meta == {"pressure_hpa": 1007, "comp_src": LIVE}


@pytest.mark.parametrize(
    "barometer",
    [
        None,  # never read
        {"pressure_hpa": None},  # missing value
        {"pressure_hpa": 1500.0},  # outside what the sensor accepts
    ],
)
def test_unusable_live_pressure_falls_back_to_altitude(
    fake_bus: None, barometer: dict | None
) -> None:
    sensor = _opened(pressure_source="barometer")
    if barometer is not None:
        sensor.latest_values.record("barometer", barometer, valid_for_s=60)
    sensor.read()
    assert sensor.read().meta["comp_src"] == ALTITUDE


def test_out_of_date_live_pressure_falls_back_to_altitude(fake_bus: None) -> None:
    sensor = _opened(pressure_source="barometer")
    sensor.latest_values.record("barometer", {"pressure_hpa": 1006.5}, valid_for_s=-1)
    sensor.read()
    assert sensor.read().meta["comp_src"] == ALTITUDE


def test_stopped_sensor_raises_so_it_is_restarted(
    fake_bus: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scd41_i2c, "_READY_TIMEOUT_S", 0.1)
    monkeypatch.setattr(scd41_i2c, "_READY_POLL_S", 0.02)
    sensor = _opened()
    FakeScd41Bus.latest.measuring = False  # e.g. power-cycled by a brown-out
    with pytest.raises(OSError, match="no measurement ready"):
        sensor.read()


def test_missing_sensor_fails_to_open(fake_bus: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FakeScd41Bus, "present", False)
    sensor = Scd41Sensor("co2", 60)
    with pytest.raises(OSError):
        sensor.open()
    sensor.close()
    assert FakeScd41Bus.latest.closed


@pytest.mark.parametrize("options", [{"altitude_m": -10}, {"altitude_m": 3500}])
def test_bad_options_rejected(options: dict) -> None:
    with pytest.raises(ValueError):
        Scd41Sensor("co2", 60, **options)


def test_uses_its_pressure_source() -> None:
    assert Scd41Sensor("co2", 60, pressure_source="barometer").uses_sources() == ["barometer"]
    assert Scd41Sensor("co2", 60).uses_sources() == []
