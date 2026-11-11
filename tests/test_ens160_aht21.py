import logging
from types import SimpleNamespace

import pytest

from conftest import FakeMsg, install_fake_smbus2
from measure_station.sensors import Sample, create_sensor, ens160_aht21_i2c
from measure_station.sensors.ens160_aht21_i2c import Ens160Aht21Sensor

STANDARD = 0x02
DEEP_SLEEP = 0x00
IDLE = 0x01
RESET = 0xF0


class FakeEns160:
    MODE_STANDARD = STANDARD

    def __init__(self) -> None:
        self.opmode = STANDARD
        self.error = False
        self.running = True

    def mode(self) -> int:
        return self.opmode

    def status(self) -> SimpleNamespace:
        return SimpleNamespace(error=self.error, running=self.running)


class FakeBoard:
    """Stands in for drivers.ens160_aht21.ENS160_AHT21 after begin()."""

    def __init__(self) -> None:
        self.ens160 = FakeEns160()

    def read(self) -> SimpleNamespace:
        return SimpleNamespace(
            aqi=2, tvoc=145, eco2=612, validity=0, pcb_temp=27.84123, pcb_humi=38.2167
        )


def _opened(board: FakeBoard) -> Ens160Aht21Sensor:
    sensor = Ens160Aht21Sensor("air-quality", 60)
    sensor._board = board
    return sensor


def test_config_creates_sensor_without_hardware() -> None:
    sensor = create_sensor({"type": "ens160_aht21", "name": "air-quality", "interval_s": 60})
    assert isinstance(sensor, Ens160Aht21Sensor)
    assert sensor.sensor_type == "ens160_aht21"  # published with every reading
    sensor.close()  # safe before open()
    sensor.close()


def test_bad_ens160_address_rejected() -> None:
    with pytest.raises(ValueError, match="ens160_address"):
        Ens160Aht21Sensor("air-quality", 60, ens160_address=0x38)


def test_read_splits_driver_reading_into_values_and_meta() -> None:
    assert _opened(FakeBoard()).read() == Sample(
        values={"aqi": 2, "tvoc_ppb": 145, "eco2_ppm": 612},
        meta={"validity": "normal", "temperature_c": 27.84, "humidity_pct": 38.2},
    )


def test_validity_is_published_as_text() -> None:
    board = FakeBoard()
    board.read = lambda: SimpleNamespace(
        aqi=1, tvoc=0, eco2=400, validity=1, pcb_temp=25.0, pcb_humi=40.0
    )
    assert _opened(board).read().meta["validity"] == "warmup"


def test_read_raises_when_ens160_left_standard_mode() -> None:
    board = FakeBoard()
    board.ens160.opmode = DEEP_SLEEP  # e.g. reset by a brown-out
    with pytest.raises(OSError, match="STANDARD"):
        _opened(board).read()


def test_read_raises_on_ens160_error() -> None:
    board = FakeBoard()
    board.ens160.error = True
    with pytest.raises(OSError, match="error"):
        _opened(board).read()


def test_read_raises_when_ens160_stopped_measuring_in_standard_mode() -> None:
    board = FakeBoard()
    board.ens160.running = False  # seen on hardware: OPMODE 0x02, DEVICE_STATUS 0x03
    with pytest.raises(OSError, match="stopped measuring"):
        _opened(board).read()


def test_read_raises_on_aqi_0() -> None:
    board = FakeBoard()
    board.read = lambda: SimpleNamespace(
        aqi=0, tvoc=0, eco2=0, validity=0, pcb_temp=20.46, pcb_humi=50.4
    )
    with pytest.raises(OSError, match="AQI 0"):
        _opened(board).read()


# ---- open(), through the real driver ----


class FakeEns160Chip:
    """The ENS160's registers (ENS §16), as seen on hardware. Starting STANDARD mode gives
    the first sample a couple of status reads later. A stuck chip reads STANDARD in
    OPMODE, but DEVICE_STATUS 0x03 (not measuring) and data all 0, and ignores writes to
    STANDARD; a reset (OPMODE 0xF0) clears it, unless ``stuck_after_reset``."""

    SAMPLE = bytes((0x01, 0x18, 0x00, 0x90, 0x01))  # AQI 1, TVOC 24 ppb, eCO2 400 ppm

    def __init__(self, *, stuck: bool = False, stuck_after_reset: bool = False) -> None:
        self.opmode = STANDARD if stuck else DEEP_SLEEP
        self.stuck, self.stuck_after_reset = stuck, stuck_after_reset
        self.starting = 0  # status reads left before the first sample
        self.new_data = False
        self.mode_writes: list[int] = []

    @property
    def measuring(self) -> bool:
        return self.opmode == STANDARD and not self.stuck and self.starting == 0

    def write(self, reg: int, data: bytes) -> None:
        if reg != 0x10:  # e.g. the compensation registers
            return
        self.opmode = data[0]
        self.mode_writes.append(data[0])
        if data[0] == RESET:
            self.opmode, self.stuck = IDLE, self.stuck_after_reset
        elif data[0] == STANDARD and not self.stuck:
            self.starting, self.new_data = 2, True

    def read(self, reg: int, n: int) -> bytes:
        if reg == 0x00:
            return bytes((0x60, 0x01))  # PART_ID 0x0160
        if reg == 0x10:
            return bytes((self.opmode,))
        if reg == 0x20:
            if self.stuck:
                return bytes((0x03,))
            if self.opmode == STANDARD and self.starting:
                self.starting -= 1
                return bytes((0x01,))  # not measuring yet
            # Measuring, validity 1 (warm-up), NEWDAT.
            return bytes((0x84 | (0x02 if self.new_data else 0),)) if self.measuring else b"\0"
        if reg == 0x21:
            self.new_data = False
            return self.SAMPLE[:n] if self.measuring else bytes(n)
        return bytes(n)


class FakeEns160Bus:
    """The board's I2C bus: the ENS160 at 0x53, and an AHT21 at 0x38 that's calibrated."""

    chip: FakeEns160Chip

    def __init__(self, bus: int) -> None:
        pass

    def close(self) -> None:
        pass

    def i2c_rdwr(self, *msgs: FakeMsg) -> None:
        addr = msgs[0].addr
        if addr == 0x38:
            for m in msgs:
                if m.is_read:
                    m.data[:] = bytes((0x18,)) * len(m.data)  # status: initialised
            return
        if addr != 0x53:
            raise OSError(121, "Remote I/O error")
        if len(msgs) == 2:  # register read
            msgs[1].data[:] = self.chip.read(msgs[0].data[0], len(msgs[1].data))
        elif msgs[0].is_read:  # probe
            msgs[0].data[:] = bytes(len(msgs[0].data))
        else:
            self.chip.write(msgs[0].data[0], bytes(msgs[0].data[1:]))


@pytest.fixture
def fake_board(monkeypatch: pytest.MonkeyPatch) -> type[FakeEns160Bus]:
    install_fake_smbus2(monkeypatch, FakeEns160Bus, "ens160_aht21")
    monkeypatch.setattr(ens160_aht21_i2c, "_FIRST_SAMPLE_S", 0.5)  # keep the tests quick
    return FakeEns160Bus


def _open(bus: type[FakeEns160Bus], chip: FakeEns160Chip) -> Ens160Aht21Sensor:
    bus.chip = chip
    sensor = create_sensor({"type": "ens160_aht21", "name": "air-quality", "interval_s": 60})
    sensor.open()
    return sensor


def test_open_waits_for_the_first_sample(fake_board: type[FakeEns160Bus]) -> None:
    chip = FakeEns160Chip()
    _open(fake_board, chip).close()
    assert chip.mode_writes == [STANDARD]  # a healthy chip isn't reset
    assert chip.measuring and chip.new_data  # so the first read gets a real sample


def test_open_resets_a_stuck_ens160(
    fake_board: type[FakeEns160Bus], caplog: pytest.LogCaptureFixture
) -> None:
    chip = FakeEns160Chip(stuck=True)
    with caplog.at_level(logging.WARNING):
        _open(fake_board, chip).close()
    assert chip.mode_writes == [STANDARD, RESET, IDLE, STANDARD]  # STANDARD alone didn't help
    assert chip.measuring and chip.new_data
    assert "resetting it" in caplog.text


def test_open_fails_if_a_reset_doesnt_help(fake_board: type[FakeEns160Bus]) -> None:
    chip = FakeEns160Chip(stuck=True, stuck_after_reset=True)
    with pytest.raises(OSError, match="even after a reset"):
        _open(fake_board, chip)
