"""The BH1750 adapter, driving the real driver against a fake I2C bus."""

import pytest

from conftest import FakeMsg, install_fake_smbus2
from measure_station.sensors import create_sensor
from measure_station.sensors.bh1750_i2c import Bh1750Sensor

POWER_ON, ONE_TIME_HIGH, ONE_TIME_HIGH2 = 0x01, 0x20, 0x21


def _mtreg_opcodes(mtreg: int) -> list[int]:
    return [0x40 | (mtreg >> 5), 0x60 | (mtreg & 0x1F)]  # sent as two opcodes (p11)


class FakeBh1750Bus:
    """A BH1750 at 0x23, converting light to counts as the datasheet describes (p11):
    lux * 1.2 * MTreg / 69, doubled in H-resolution mode 2, clipped at 65535."""

    present = True
    lux: tuple[float, ...] = (100.0,)  # light at the chip, per measurement (last repeats)
    latest: "FakeBh1750Bus"

    def __init__(self, bus: int) -> None:
        self.opcodes: list[int] = []
        self.mtreg = 69
        self.raw = 0
        self.measurements = 0
        self.closed = False
        FakeBh1750Bus.latest = self

    def i2c_rdwr(self, *messages: FakeMsg) -> None:
        for m in messages:
            if not self.present or m.addr != 0x23:
                raise OSError(121, "Remote I/O error")
            if m.is_read:
                m.data[:] = self.raw.to_bytes(2, "big")
            else:
                self._opcode(m.data[0])

    def _opcode(self, op: int) -> None:
        self.opcodes.append(op)
        if 0x40 <= op <= 0x47:  # MTreg bits 7-5
            self.mtreg = (self.mtreg & 0x1F) | (op & 0x07) << 5
        elif 0x60 <= op <= 0x7F:  # MTreg bits 4-0
            self.mtreg = (self.mtreg & 0xE0) | (op & 0x1F)
        elif op in (ONE_TIME_HIGH, ONE_TIME_HIGH2):
            lux = self.lux[min(self.measurements, len(self.lux) - 1)]
            self.measurements += 1
            counts = lux * 1.2 * self.mtreg / 69 * (2 if op == ONE_TIME_HIGH2 else 1)
            self.raw = min(0xFFFF, round(counts))

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FakeBh1750Bus, "present", True)
    install_fake_smbus2(monkeypatch, FakeBh1750Bus, "bh1750")


def _read_at(monkeypatch: pytest.MonkeyPatch, *lux: float, **options) -> tuple[float, str]:
    monkeypatch.setattr(FakeBh1750Bus, "lux", lux)
    sensor = Bh1750Sensor("light", 60, **options)
    sensor.open()
    sample = sensor.read()
    return sample.values["illuminance_lux"], sample.meta["range"]


def test_fake_matches_the_datasheet_example(monkeypatch: pytest.MonkeyPatch) -> None:
    # p7: at the default MTreg (69), H-resolution, 28066.7 lx gives 0x8390.
    monkeypatch.setattr(FakeBh1750Bus, "lux", (33680 / 1.2,))
    chip = FakeBh1750Bus(1)
    chip._opcode(ONE_TIME_HIGH)
    assert chip.raw == 0x8390


def test_bright_light_takes_one_hi_measurement(
    fake_bus: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    lux, range_ = _read_at(monkeypatch, 30000.0)
    assert range_ == "hi"
    assert lux == pytest.approx(30000, abs=1.9)  # one hi-range step
    assert FakeBh1750Bus.latest.opcodes[-4:] == [POWER_ON, *_mtreg_opcodes(31), ONE_TIME_HIGH]
    assert FakeBh1750Bus.latest.measurements == 1


@pytest.mark.parametrize("true_lux", [4000.0, 12.3, 0.25, 0.0])
def test_dim_light_is_measured_again_in_lo(
    fake_bus: None, monkeypatch: pytest.MonkeyPatch, true_lux: float
) -> None:
    lux, range_ = _read_at(monkeypatch, true_lux)
    assert range_ == "lo"
    assert lux == pytest.approx(true_lux, abs=0.12)  # one lo-range step
    assert FakeBh1750Bus.latest.opcodes[-4:] == [POWER_ON, *_mtreg_opcodes(254), ONE_TIME_HIGH2]


def test_brightening_between_measurements_keeps_the_hi_reading(
    fake_bus: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 4000 lx at the hi measurement, then the sun comes out before the lo one.
    lux, range_ = _read_at(monkeypatch, 4000.0, 20000.0)
    assert range_ == "hi"
    assert lux == pytest.approx(4000, abs=1.9)


def test_above_the_hi_range_reads_its_maximum(
    fake_bus: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    lux, range_ = _read_at(monkeypatch, 150000.0)
    assert range_ == "hi"
    assert lux == pytest.approx(65535 * 69 / (1.2 * 31), abs=0.1)  # the maximum, ~121550


def test_transmittance_scales_readings(fake_bus: None, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _read_at(monkeypatch, 100.0, transmittance=0.5)[0] == pytest.approx(200, abs=0.25)


def test_config_entry_creates_the_sensor(fake_bus: None) -> None:
    sensor = create_sensor({"type": "bh1750", "name": "light", "interval_s": 60})
    assert sensor.sensor_type == "bh1750"
    sensor.open()
    sensor.close()
    assert FakeBh1750Bus.latest.closed
    sensor.close()  # safe twice


def test_missing_sensor_fails_to_open(fake_bus: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FakeBh1750Bus, "present", False)
    sensor = Bh1750Sensor("light", 60)
    with pytest.raises(OSError):
        sensor.open()
    sensor.close()
    assert FakeBh1750Bus.latest.closed


@pytest.mark.parametrize("options", [{"address": 0x76}, {"transmittance": 0}, {"mtreg": 69}])
def test_bad_options_rejected(options: dict) -> None:
    with pytest.raises((ValueError, TypeError)):  # mtreg: auto-ranging sets it
        Bh1750Sensor("light", 60, **options)
