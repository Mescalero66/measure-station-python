"""The BMP280 adapter, driving the real driver against a fake I2C bus."""

import struct

import pytest

from conftest import FakeMsg, install_fake_smbus2
from measure_station.sensors import Sample, create_sensor
from measure_station.sensors.bmp280_i2c import Bmp280Sensor

# The datasheet's worked example (§3.12): these give 25.08 °C and 100653.27 Pa.
CALIBRATION = struct.pack(
    "<HhhHhhhhhhhh", 27504, 26435, -1000, 36477, -10685, 3024, 2855, 140, -7, 15500, -14600, 6000
)
ADC_P, ADC_T = 415148, 519888
SKIPPED = 0x80000


def _raw20(value: int) -> bytes:
    return bytes((value >> 12 & 0xFF, value >> 4 & 0xFF, (value & 0x0F) << 4))


class FakeBmp280Bus:
    """A BMP280 at 0x76, as seen through smbus2's i2c_rdwr."""

    latest: "FakeBmp280Bus"

    def __init__(self, bus: int) -> None:
        self.regs = bytearray(256)
        self.regs[0xD0] = 0x58  # chip ID
        self.regs[0x88 : 0x88 + len(CALIBRATION)] = CALIBRATION
        self.regs[0xF7:0xFD] = _raw20(ADC_P) + _raw20(ADC_T)
        self.pointer = 0
        self.closed = False
        FakeBmp280Bus.latest = self

    def i2c_rdwr(self, *messages: FakeMsg) -> None:
        for m in messages:
            if m.addr != 0x76:
                raise OSError(121, "Remote I/O error")
            if not m.is_read:
                self.pointer = m.data[0]
                if len(m.data) > 1 and self.pointer == 0xE0:  # soft reset
                    self.regs[0xF4] = self.regs[0xF5] = 0
                elif len(m.data) > 1:
                    self.regs[self.pointer] = m.data[1]
            elif self.pointer == 0xF7 and self.regs[0xF4] >> 5 == 0:  # temperature skipped
                m.data[:] = _raw20(SKIPPED) * 2
            else:
                m.data[:] = self.regs[self.pointer : self.pointer + len(m.data)]

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_smbus2(monkeypatch, FakeBmp280Bus, "bmp280")


def test_reads_the_datasheet_example(fake_bus: None) -> None:
    sensor = create_sensor({"type": "bmp280", "name": "barometer", "interval_s": 60})
    assert sensor.sensor_type == "bmp280"
    sensor.open()
    assert sensor.read() == Sample(values={"pressure_hpa": 1006.53}, meta={"temperature_c": 25.08})
    sensor.close()
    assert FakeBmp280Bus.latest.closed
    sensor.close()  # safe twice


def test_chip_reset_since_open_raises_so_it_is_reinitialised(fake_bus: None) -> None:
    sensor = Bmp280Sensor("barometer", 60)
    sensor.open()
    FakeBmp280Bus.latest.regs[0xF4] = 0  # power-on defaults after a brown-out: all skipped
    with pytest.raises(OSError, match="no measurement"):
        sensor.read()


def test_missing_chip_fails_to_open(fake_bus: None) -> None:
    sensor = Bmp280Sensor("barometer", 60, address=0x77)  # nothing answers there
    with pytest.raises(OSError):
        sensor.open()
    sensor.close()
    assert FakeBmp280Bus.latest.closed


def test_bad_address_rejected() -> None:
    with pytest.raises(ValueError, match="address"):
        Bmp280Sensor("barometer", 60, address=0x38)
