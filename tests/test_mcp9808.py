"""The MCP9808 adapter, driving the real driver against a fake I2C bus."""

import time

import pytest

from conftest import FakeMsg, install_fake_smbus2
from measure_station.sensors import create_sensor
from measure_station.sensors.mcp9808_i2c import Mcp9808Sensor

CONFIG, T_UPPER, T_LOWER, T_CRIT, T_A, MANUFACTURER_ID, DEVICE_ID, RESOLUTION = range(1, 9)
SHDN = 0x0100
T_CONV_S = (0.030, 0.065, 0.130, 0.250)  # typical, per resolution code (Table 5-2)

# Register accesses, as ("r" or "w", register).
OPEN = [("r", MANUFACTURER_ID), ("r", DEVICE_ID), ("w", RESOLUTION)]
SET_CONFIG = [("r", CONFIG), ("w", CONFIG), ("r", CONFIG)]  # read-modify-write, checked
READ = [*SET_CONFIG, ("r", T_A), *SET_CONFIG]  # wake, (wait), read, shut down


def encode(celsius: float) -> int:
    """TA bits 12-0 (Register 5-4): 13-bit two's complement, 1/16 °C per LSB."""
    return round(celsius * 16) & 0x1FFF


def signed(bits: int) -> int:
    bits &= 0x1FFF
    return bits - 0x2000 if bits & 0x1000 else bits


class FakeMcp9808Bus:
    """An MCP9808 at 0x1F, starting with the power-on defaults (Table 5-3). While it
    converts, TA takes the temperature once tCONV has passed since it woke (Table 5-2);
    in shutdown TA keeps its last value (§5.2.1). TA's bits 15-13 compare it with the
    limit registers (Register 5-4)."""

    present = True
    manufacturer = 0x0054
    resolution = 0x03  # at the start: the power-on default unless something changed it
    temperature = 21.0  # the air at the chip
    latest: "FakeMcp9808Bus"

    def __init__(self, bus: int) -> None:
        self.accesses: list[tuple[str, int]] = []
        self.closed = False
        self.power_on()
        self.regs[RESOLUTION] = self.resolution
        FakeMcp9808Bus.latest = self

    def power_on(self) -> None:
        self.regs = {CONFIG: 0, T_UPPER: 0, T_LOWER: 0, T_CRIT: 0, T_A: 0}
        self.regs |= {MANUFACTURER_ID: self.manufacturer, DEVICE_ID: 0x0400, RESOLUTION: 0x03}
        self.pointer = 0
        self.woke = time.monotonic()

    def i2c_rdwr(self, *messages: FakeMsg) -> None:
        for m in messages:
            if not self.present or m.addr != 0x1F:
                raise OSError(121, "Remote I/O error")
            if m.is_read:
                self.accesses.append(("r", self.pointer))
                value = self._ta() if self.pointer == T_A else self.regs[self.pointer]
                m.data[:] = value.to_bytes(len(m.data), "big")
            else:
                self.pointer, *data = m.data  # the pointer alone selects a register to read
                if data:
                    self.accesses.append(("w", self.pointer))
                    self._write(int.from_bytes(bytes(data), "big"))

    def _write(self, value: int) -> None:
        if self.pointer == CONFIG:
            if self.regs[CONFIG] & SHDN and not value & SHDN:
                self.woke = time.monotonic()  # conversions start again
            value &= ~0x0030  # Int Clear reads as 0; Alert Stat is read-only
        elif self.pointer == RESOLUTION:
            value &= 0x03
        elif self.pointer in (T_UPPER, T_LOWER, T_CRIT):
            value &= 0x1FFC
        else:
            return  # read-only
        self.regs[self.pointer] = value

    def _ta(self) -> int:
        regs = self.regs
        converting = not regs[CONFIG] & SHDN
        if converting and time.monotonic() - self.woke >= T_CONV_S[regs[RESOLUTION]]:
            regs[T_A] = encode(self.temperature)
        ta = signed(regs[T_A])
        return (
            regs[T_A]
            | (0x8000 if ta >= signed(regs[T_CRIT]) else 0)
            | (0x4000 if ta > signed(regs[T_UPPER]) else 0)
            | (0x2000 if ta < signed(regs[T_LOWER]) else 0)
        )

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FakeMcp9808Bus, "present", True)
    monkeypatch.setattr(FakeMcp9808Bus, "temperature", 21.0)
    install_fake_smbus2(monkeypatch, FakeMcp9808Bus, "mcp9808")


def _opened(**options) -> tuple[Mcp9808Sensor, FakeMcp9808Bus]:
    sensor = Mcp9808Sensor("temperature", 60, **options)
    sensor.open()
    return sensor, FakeMcp9808Bus.latest


def test_fake_matches_the_datasheet() -> None:
    # §5.1.2: "Writing +90°C to the TUPPER Register <0000 0101 1010 0000>b". The limit
    # registers use TA's bit layout (Registers 5-3, 5-4).
    assert encode(90.0) == 0b0000_0101_1010_0000
    # Equation 5-1, from the upper and lower bytes. Below 0 °C this revision prints
    # 256 - (...), which has the wrong sign: it's (...) - 256, as two's complement.
    upper, lower = divmod(encode(25.0625), 256)
    assert upper * 2**4 + lower * 2**-4 == 25.0625
    upper, lower = divmod(encode(-10.5), 256)
    assert upper & 0x10  # the SIGN bit
    assert (upper & 0x0F) * 2**4 + lower * 2**-4 - 256 == -10.5


def test_config_entry_reads_the_temperature(
    fake_bus: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FakeMcp9808Bus, "temperature", 23.4375)
    sensor = create_sensor({"type": "mcp9808", "name": "temperature", "interval_s": 60})
    assert sensor.sensor_type == "mcp9808"
    sensor.open()
    sample = sensor.read()
    # Not 'above critical' etc: TA is above the 0 °C limits, but the flags aren't used.
    assert sample.values == {"temperature_c": 23.4375}
    assert sample.meta == {}


def test_open_checks_the_id_and_leaves_it_asleep(fake_bus: None) -> None:
    _, chip = _opened()
    assert chip.accesses == [*OPEN, *SET_CONFIG]
    assert chip.regs[CONFIG] & SHDN
    assert chip.regs[RESOLUTION] == 0x03  # 0.0625 °C


def test_each_read_wakes_it_for_a_new_conversion(
    fake_bus: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    sensor, chip = _opened()
    assert sensor.read().values["temperature_c"] == 21.0
    # TA keeps 21.0 through shutdown, so this needs the conversion after waking.
    monkeypatch.setattr(FakeMcp9808Bus, "temperature", 21.5)
    assert sensor.read().values["temperature_c"] == 21.5
    assert chip.accesses == [*OPEN, *SET_CONFIG, *READ, *READ]
    assert chip.regs[CONFIG] & SHDN  # asleep between readings


def test_full_range_and_negative_temperatures(
    fake_bus: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    sensor, _ = _opened()
    for celsius in (-40.0, -10.5, -0.0625, 0.0, 125.0):
        monkeypatch.setattr(FakeMcp9808Bus, "temperature", celsius)
        assert sensor.read().values["temperature_c"] == celsius


def test_a_leftover_resolution_is_reset(fake_bus: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # e.g. left at 0.5 °C by the driver's demo, which sweeps the resolutions.
    monkeypatch.setattr(FakeMcp9808Bus, "resolution", 0x00)
    monkeypatch.setattr(FakeMcp9808Bus, "temperature", 21.0625)
    sensor, chip = _opened()
    assert chip.regs[RESOLUTION] == 0x03
    assert sensor.read().values["temperature_c"] == 21.0625


def test_brown_out_is_put_back_to_sleep(fake_bus: None, monkeypatch: pytest.MonkeyPatch) -> None:
    sensor, chip = _opened()
    sensor.read()
    chip.power_on()  # converting continuously, limits 0 °C, TA 0x0000
    monkeypatch.setattr(FakeMcp9808Bus, "temperature", 30.25)
    assert sensor.read().values["temperature_c"] == 30.25
    assert chip.regs[CONFIG] & SHDN


def test_wrong_chip_fails_to_open(fake_bus: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FakeMcp9808Bus, "manufacturer", 0x1234)
    sensor = Mcp9808Sensor("temperature", 60)
    with pytest.raises(RuntimeError, match="no MCP9808 at 0x1F"):
        sensor.open()
    sensor.close()
    assert FakeMcp9808Bus.latest.closed


def test_missing_sensor_fails_to_open(fake_bus: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FakeMcp9808Bus, "present", False)
    sensor = Mcp9808Sensor("temperature", 60)
    with pytest.raises(OSError):
        sensor.open()
    sensor.close()
    assert FakeMcp9808Bus.latest.closed
    sensor.close()  # safe twice


@pytest.mark.parametrize("options", [{"address": 0x17}, {"address": 0x20}, {"resolution": 0.5}])
def test_bad_options_rejected(options: dict) -> None:
    with pytest.raises((ValueError, TypeError)):  # resolution: fixed at 0.0625 °C
        Mcp9808Sensor("temperature", 60, **options)
