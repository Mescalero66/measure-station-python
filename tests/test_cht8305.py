"""The CHT8305 adapter, driving the real driver against a fake I2C bus."""

import logging
import math
import time

import pytest

from conftest import FakeMsg, install_fake_smbus2
from measure_station.sensors import cht8305_i2c, create_sensor
from measure_station.sensors.cht8305_i2c import Cht8305Sensor

TEMPERATURE, HUMIDITY, CONFIG, ALERT_SETUP, MANUFACTURER_ID, VERSION_ID = 0, 1, 2, 3, 0xFE, 0xFF
DEFAULT_CONFIG = 0x1000  # Table 1: T and RH in one conversion, 14-bit, heater off
SRST, HEATER, T_RES = 0x8000, 0x2000, 0x0400


def encode_c(celsius: float) -> int:
    """Formula (1) inverted, truncated as in the datasheet's examples (§1.1.4)."""
    return int((celsius + 40) * 65535 / 165)


def encode_rh(percent: float) -> int:
    """Formula (2) inverted, likewise."""
    return int(percent * 65535 / 100)


class FakeCht8305Bus:
    """A CHT8305C at 0x40. Writing the pointer 0x00 starts a temperature and humidity
    conversion (2 x 5.5 ms, §1.4.3); reads are NACKed until it finishes. Results are
    14-bit: bits 1-0 always 0 (§1.1.1, §1.1.2). A soft reset restores Table 1's defaults.
    The heater (§1.3) warms the chip by heat_c, which decays with cool_tau_s once it's
    off; the warmer chip reads drier, by about 6 % of the reading per °C."""

    present = True
    version = 0x8305
    config = DEFAULT_CONFIG  # at the start: the default unless something changed it
    conversion_s = 0.011
    temperature, humidity = 18.42, 61.3  # the air at the probe
    heat_c, cool_tau_s = 2.0, 0.3  # the datasheet gives neither
    latest: "FakeCht8305Bus"

    def __init__(self, bus: int) -> None:
        self.accesses: list[tuple] = []
        self.nacks = 0
        self.closed = False
        self.power_on()
        self.regs[CONFIG] = self.config
        FakeCht8305Bus.latest = self

    def power_on(self) -> None:
        self.regs = {TEMPERATURE: 0, HUMIDITY: 0, CONFIG: DEFAULT_CONFIG, ALERT_SETUP: 0xCD36}
        self.regs |= {MANUFACTURER_ID: 0x5959, VERSION_ID: self.version}
        self.pointer = 0
        self.done_at = 0.0
        self.heater_off_at: float | None = None

    def i2c_rdwr(self, *messages: FakeMsg) -> None:
        for m in messages:
            if not self.present or m.addr != 0x40:
                raise OSError(121, "Remote I/O error")
            if m.is_read:
                self._read(m)
            elif len(m.data) == 1:
                self.pointer = m.data[0]
                self.accesses.append(("pointer", self.pointer))
                if self.pointer == TEMPERATURE:
                    self._convert()
            else:
                self.pointer, msb, lsb = m.data
                self.accesses.append(("write", self.pointer, msb << 8 | lsb))
                self._write(msb << 8 | lsb)

    def _convert(self) -> None:
        self.done_at = time.monotonic() + self.conversion_s
        warmer = self._warmer_c()
        self.regs[TEMPERATURE] = encode_c(self.temperature + warmer) & 0xFFFC
        self.regs[HUMIDITY] = encode_rh(self.humidity * (1 - 0.06 * warmer)) & 0xFFFC

    def _warmer_c(self) -> float:
        if self.regs[CONFIG] & HEATER:
            return self.heat_c
        if self.heater_off_at is None:
            return 0.0
        return self.heat_c * math.exp(-(time.monotonic() - self.heater_off_at) / self.cool_tau_s)

    def _read(self, m: FakeMsg) -> None:
        if time.monotonic() < self.done_at:
            self.nacks += 1
            raise OSError(121, "Remote I/O error")  # still converting
        self.accesses.append(("read", len(m.data)))
        words = [self.regs[self.pointer]]
        if self.pointer == TEMPERATURE:
            words.append(self.regs[HUMIDITY])  # 4-byte read: T then RH (§1.4.5, method 2)
        m.data[:] = b"".join(w.to_bytes(2, "big") for w in words)[: len(m.data)]

    def _write(self, value: int) -> None:
        if self.pointer == CONFIG:
            if self.regs[CONFIG] & HEATER and not value & HEATER:
                self.heater_off_at = time.monotonic()
            if value & SRST:
                self.power_on()  # §1.2: every register back to its default
            else:
                read_only = self.regs[CONFIG] & 0x0838  # VCCS and the alert status bits
                self.regs[CONFIG] = value & 0x77C0 | read_only
        elif self.pointer == ALERT_SETUP:
            self.regs[ALERT_SETUP] = value

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FakeCht8305Bus, "present", True)
    install_fake_smbus2(monkeypatch, FakeCht8305Bus, "cht8305")


class Clock:
    """The adapter's time.monotonic(), which tests can move on by hours."""

    offset_s = 0.0

    @classmethod
    def monotonic(cls) -> float:
        return time.monotonic() + cls.offset_s

    @classmethod
    def hours_pass(cls, hours: float) -> None:
        cls.offset_s += hours * 3600


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> type[Clock]:
    monkeypatch.setattr(Clock, "offset_s", 0.0)
    monkeypatch.setattr(cht8305_i2c, "time", Clock)
    monkeypatch.setattr(cht8305_i2c, "_POLL_S", 0.05)  # cooling checks, sped up
    return Clock


def _opened(**options) -> tuple[Cht8305Sensor, FakeCht8305Bus]:
    sensor = Cht8305Sensor("humidity", 60, **options)
    sensor.open()
    return sensor, FakeCht8305Bus.latest


def test_fake_matches_the_datasheet_example() -> None:
    # §1.1.4: a 90 %RH, 80 °C threshold "convert[s] into binary according to above formula".
    assert encode_rh(90) == 0b1110_0110_0110_0101
    assert encode_c(80) == 0b1011_1010_0010_1101


def test_config_entry_reads_temperature_and_humidity(fake_bus: None) -> None:
    sensor = create_sensor({"type": "cht8305", "name": "humidity", "interval_s": 60})
    assert sensor.sensor_type == "cht8305"
    sensor.open()
    sample = sensor.read()
    assert sample.values == {"temperature_c": pytest.approx(18.42, abs=0.015), "humidity_pct": 61.3}
    assert round(sample.values["temperature_c"], 2) == sample.values["temperature_c"]
    assert sample.meta == {}


def test_open_checks_the_ids_and_resets_to_the_defaults(
    fake_bus: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # e.g. left with the heater on and 11-bit temperature by the driver's demo.
    monkeypatch.setattr(FakeCht8305Bus, "config", DEFAULT_CONFIG | HEATER | T_RES)
    _, chip = _opened()
    assert chip.accesses == [
        ("pointer", MANUFACTURER_ID),
        ("read", 2),
        ("pointer", VERSION_ID),
        ("read", 2),
        ("write", CONFIG, SRST | DEFAULT_CONFIG),
    ]
    assert chip.regs[CONFIG] == DEFAULT_CONFIG


def test_each_read_is_one_conversion(fake_bus: None, monkeypatch: pytest.MonkeyPatch) -> None:
    sensor, chip = _opened()
    chip.accesses.clear()
    monkeypatch.setattr(FakeCht8305Bus, "humidity", 75.0)
    assert sensor.read().values["humidity_pct"] == 75.0
    assert chip.accesses == [("pointer", TEMPERATURE), ("read", 4)]


@pytest.mark.parametrize(
    ("celsius", "percent"), [(-40.0, 0.0), (-10.5, 5.5), (40.0, 95.0), (124.98, 100.0)]
)
def test_full_range(
    fake_bus: None, monkeypatch: pytest.MonkeyPatch, celsius: float, percent: float
) -> None:
    monkeypatch.setattr(FakeCht8305Bus, "temperature", celsius)
    monkeypatch.setattr(FakeCht8305Bus, "humidity", percent)
    sensor, _ = _opened()
    values = sensor.read().values
    assert values["temperature_c"] == pytest.approx(celsius, abs=0.015)  # one 14-bit step
    assert values["humidity_pct"] == pytest.approx(percent, abs=0.1)


def test_slow_conversion_is_waited_for(fake_bus: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FakeCht8305Bus, "conversion_s", 0.030)
    sensor, chip = _opened()
    assert sensor.read().values["humidity_pct"] == 61.3
    assert chip.nacks > 0


def test_conversion_that_never_finishes_raises(
    fake_bus: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FakeCht8305Bus, "conversion_s", 3600)
    sensor, _ = _opened()
    with pytest.raises(OSError, match="did not finish"):
        sensor.read()


def test_brown_out_needs_nothing_resent(fake_bus: None, monkeypatch: pytest.MonkeyPatch) -> None:
    sensor, chip = _opened()
    sensor.read()
    chip.power_on()  # the same defaults open() reset it to
    chip.accesses.clear()
    monkeypatch.setattr(FakeCht8305Bus, "temperature", 3.0)
    assert sensor.read().values["temperature_c"] == pytest.approx(3.0, abs=0.015)
    assert chip.accesses == [("pointer", TEMPERATURE), ("read", 4)]


def test_cht832x_probe_fails_to_open(fake_bus: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FakeCht8305Bus, "version", 0x8320)
    sensor = Cht8305Sensor("humidity", 60)
    with pytest.raises(RuntimeError, match="CHT832X"):
        sensor.open()
    sensor.close()
    assert FakeCht8305Bus.latest.closed


def test_missing_sensor_fails_to_open(fake_bus: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FakeCht8305Bus, "present", False)
    sensor = Cht8305Sensor("humidity", 60)
    with pytest.raises(OSError):
        sensor.open()
    sensor.close()
    assert FakeCht8305Bus.latest.closed
    sensor.close()  # safe twice


def _heater_on(chip: FakeCht8305Bus) -> bool:
    return bool(chip.regs[CONFIG] & HEATER)


def test_heater_cycle_every_24_hours(
    fake_bus: None, clock: type[Clock], caplog: pytest.LogCaptureFixture
) -> None:
    sensor, chip = _opened()
    sensor.read()
    clock.hours_pass(23.9)
    sensor.read()
    assert not _heater_on(chip)
    assert sensor.read_timeout_s(10) == 10

    clock.hours_pass(0.1)
    # Due: published as usual, then the heater runs until the next reading.
    assert sensor.read().values["humidity_pct"] == 61.3
    assert _heater_on(chip)
    assert sensor.read_timeout_s(10) == 10 + cht8305_i2c._COOL_MAX_S

    with caplog.at_level(logging.INFO):
        values = sensor.read().values  # off, and only published once cooled
    assert not _heater_on(chip)
    assert values["temperature_c"] == pytest.approx(18.42, abs=cht8305_i2c._COOLED_C + 0.015)
    assert values["humidity_pct"] == pytest.approx(61.3, abs=1)
    assert "warmed the probe 2.00 °C; cooled after" in caplog.text
    assert sensor.read_timeout_s(10) == 10

    clock.hours_pass(23.9)  # counted from when the heater went on
    sensor.read()
    assert not _heater_on(chip)
    clock.hours_pass(0.1)
    sensor.read()
    assert _heater_on(chip)


def test_heater_never_with_none(fake_bus: None, clock: type[Clock]) -> None:
    sensor, chip = _opened(heater_interval_h=None)
    clock.hours_pass(1000)
    sensor.read()
    assert not _heater_on(chip)


def test_heater_cycle_gives_up_cooling_after_its_limit(
    fake_bus: None,
    clock: type[Clock],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(FakeCht8305Bus, "cool_tau_s", 3600)
    monkeypatch.setattr(cht8305_i2c, "_COOL_MAX_S", 0.2)
    sensor, chip = _opened()
    clock.hours_pass(24)
    sensor.read()
    sensor.read()
    assert not _heater_on(chip)
    assert "still 2.00 °C above the start" in caplog.text


def test_heater_without_effect_is_reported(
    fake_bus: None,
    clock: type[Clock],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # e.g. if, like TI's HDC1080, the heater only runs during conversions.
    monkeypatch.setattr(FakeCht8305Bus, "heat_c", 0.0)
    sensor, _ = _opened()
    clock.hours_pass(24)
    sensor.read()
    sensor.read()
    assert "only 0.00 °C" in caplog.text


def test_reopening_ends_a_heater_cycle(fake_bus: None, clock: type[Clock]) -> None:
    sensor, chip = _opened()
    clock.hours_pass(24)
    sensor.read()
    sensor.close()  # e.g. a failure: the station closes and reopens the sensor
    sensor.open()
    chip = FakeCht8305Bus.latest
    assert not _heater_on(chip)
    assert sensor.read_timeout_s(10) == 10
    clock.hours_pass(23.9)  # counted again from the restart
    sensor.read()
    assert not _heater_on(chip)


@pytest.mark.parametrize(
    "options", [{"address": 0x44}, {"heater_interval_h": 0}, {"heater_interval_h": -24}]
)
def test_bad_options_rejected(options: dict) -> None:
    with pytest.raises((ValueError, TypeError)):  # address: fixed on the probe
        Cht8305Sensor("humidity", 60, **options)
