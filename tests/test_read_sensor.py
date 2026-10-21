import pytest

from measure_station import read_sensor


def test_unknown_name_lists_sensors(capsys: pytest.CaptureFixture[str]) -> None:
    assert read_sensor.main(["no-such-sensor"]) == 2
    assert "air-quality" in capsys.readouterr().out


def test_prints_readings_until_ctrl_c(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def ctrl_c(seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(read_sensor.time, "sleep", ctrl_c)
    assert read_sensor.main(["simulated"]) == 0  # listed even though it's disabled
    assert "temperature_c=" in capsys.readouterr().out
