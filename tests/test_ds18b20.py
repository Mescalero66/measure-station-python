from pathlib import Path

import pytest

from measure_station.sensors import create_sensor

GOOD = "72 01 4b 46 7f ff 0e 10 57 : crc=57 YES\n72 01 4b 46 7f ff 0e 10 57 t=23125\n"
BAD_CRC = "72 01 4b 46 7f ff 0e 10 57 : crc=12 NO\n72 01 4b 46 7f ff 0e 10 57 t=23125\n"
ZEROS = "00 00 00 00 00 00 00 00 00 : crc=00 YES\n00 00 00 00 00 00 00 00 00 t=0\n"
POWER_ON = "50 05 4b 46 7f ff 0c 10 1c : crc=1c YES\n50 05 4b 46 7f ff 0c 10 1c t=85000\n"
NEGATIVE = "5e ff 4b 46 7f ff 02 10 61 : crc=61 YES\n5e ff 4b 46 7f ff 02 10 61 t=-10125\n"


def _device(root: Path, device_id: str, text: str) -> Path:
    device = root / device_id
    device.mkdir(parents=True)
    (device / "w1_slave").write_text(text)
    return device / "w1_slave"


def _sensor(root: Path, **options):
    entry = {"type": "ds18b20", "name": "probe", "interval_s": 60, "devices_dir": str(root)}
    return create_sensor(entry | options)


def test_reads_temperature_from_the_only_probe(tmp_path: Path) -> None:
    _device(tmp_path, "28-0000075a1b2c", GOOD)
    (tmp_path / "w1_bus_master1").mkdir()  # the bus master is not a probe
    sensor = _sensor(tmp_path)
    sensor.open()
    assert sensor.read().values == {"temperature_c": 23.125}


def test_negative_temperatures(tmp_path: Path) -> None:
    _device(tmp_path, "28-a", NEGATIVE)
    sensor = _sensor(tmp_path)
    sensor.open()
    assert sensor.read().values == {"temperature_c": -10.125}


def test_device_id_chooses_between_probes(tmp_path: Path) -> None:
    _device(tmp_path, "28-a", GOOD)
    _device(tmp_path, "28-b", NEGATIVE)
    with pytest.raises(OSError, match="exactly one"):
        _sensor(tmp_path).open()
    sensor = _sensor(tmp_path, device_id="28-b")
    sensor.open()
    assert sensor.read().values == {"temperature_c": -10.125}


def test_missing_probe_fails_to_open(tmp_path: Path) -> None:
    with pytest.raises(OSError, match="found none"):
        _sensor(tmp_path).open()
    with pytest.raises(OSError, match="no 1-wire device"):
        _sensor(tmp_path, device_id="28-x").open()


def test_device_id_must_be_a_ds18b20() -> None:
    with pytest.raises(ValueError, match="28-"):
        create_sensor({"type": "ds18b20", "name": "p", "interval_s": 60, "device_id": "10-a"})


@pytest.mark.parametrize(
    ("text", "problem"),
    [(BAD_CRC, "CRC"), (ZEROS, "all zeros"), (POWER_ON, "power-on"), ("", "unexpected")],
)
def test_bad_reads_fail(tmp_path: Path, text: str, problem: str) -> None:
    _device(tmp_path, "28-a", text)
    sensor = _sensor(tmp_path)
    sensor.open()
    with pytest.raises(OSError, match=problem):
        sensor.read()


def test_a_bad_crc_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _device(tmp_path, "28-a", GOOD)
    sensor = _sensor(tmp_path)
    sensor.open()
    reads = iter([BAD_CRC, GOOD])
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: next(reads))
    assert sensor.read().values == {"temperature_c": 23.125}
