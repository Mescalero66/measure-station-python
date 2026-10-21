from types import SimpleNamespace

import pytest

from measure_station.sensors import Sample, create_sensor
from measure_station.sensors.ens160_aht21_i2c import Ens160Aht21Sensor

STANDARD = 0x02
DEEP_SLEEP = 0x00


class FakeEns160:
    MODE_STANDARD = STANDARD

    def __init__(self) -> None:
        self.opmode = STANDARD
        self.error = False

    def mode(self) -> int:
        return self.opmode

    def status(self) -> SimpleNamespace:
        return SimpleNamespace(error=self.error)


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
