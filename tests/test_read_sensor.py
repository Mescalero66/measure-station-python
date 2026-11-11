import pytest

from measure_station import read_sensor, sysconfig


class FakePwm:
    def __init__(self, name: str, log: list[tuple[str, float]], fail: bool = False) -> None:
        self.name, self.log, self.fail = name, log, fail

    def open(self) -> None:
        if self.fail:
            raise OSError("/sys/class/pwm/pwmchip0 not found")

    def set_duty(self, duty_pct: float) -> None:
        self.log.append((self.name, duty_pct))

    def close(self) -> None:
        pass


@pytest.fixture
def duties(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, float]]:
    """The fans' duty cycles as set, in order, as (exhaust or intake, percent)."""
    log: list[tuple[str, float]] = []
    outputs = (FakePwm("exhaust", log), FakePwm("intake", log))
    monkeypatch.setattr(read_sensor, "fan_outputs", lambda cfg: outputs)
    return log


def _ctrl_c_on_sleep(monkeypatch: pytest.MonkeyPatch, *, after: int) -> list[float]:
    """Let ``after`` sleeps pass, then press Ctrl+C; returns the sleeps asked for."""
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) > after:
            raise KeyboardInterrupt

    monkeypatch.setattr(read_sensor.time, "sleep", sleep)
    return sleeps


def test_unknown_name_lists_sensors(capsys: pytest.CaptureFixture[str]) -> None:
    assert read_sensor.main(["no-such-sensor"]) == 2
    assert "air-quality" in capsys.readouterr().out


def test_prints_readings_every_10_s_with_the_fans_on(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    duties: list[tuple[str, float]],
) -> None:
    sleeps = _ctrl_c_on_sleep(monkeypatch, after=1)  # the kick-start, then Ctrl+C
    assert read_sensor.main(["simulated"]) == 0  # listed even though it's disabled
    assert "temperature_c=" in capsys.readouterr().out
    assert sleeps == [sysconfig.FAN_KICKSTART_S, 10.0]
    assert duties == [
        ("exhaust", 100),
        ("intake", 100),  # the kick-start...
        ("intake", sysconfig.FAN_ANALOG_MIN_DUTY_PCT),  # ...then the minimum (50 %)
        ("exhaust", 100),  # on exit, as the stopped service leaves them
        ("intake", 0),
    ]


def test_ctrl_c_during_the_kick_start_still_leaves_the_fans_in_failsafe(
    monkeypatch: pytest.MonkeyPatch, duties: list[tuple[str, float]]
) -> None:
    _ctrl_c_on_sleep(monkeypatch, after=0)
    assert read_sensor.main(["simulated"]) == 0
    assert duties[-2:] == [("exhaust", 100), ("intake", 0)]
    assert ("intake", sysconfig.FAN_ANALOG_MIN_DUTY_PCT) not in duties


def test_reads_anyway_if_the_fans_cant_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    log: list[tuple[str, float]] = []
    outputs = (FakePwm("exhaust", log, fail=True), FakePwm("intake", log))
    monkeypatch.setattr(read_sensor, "fan_outputs", lambda cfg: outputs)
    _ctrl_c_on_sleep(monkeypatch, after=0)
    assert read_sensor.main(["simulated"]) == 0
    out = capsys.readouterr().out
    assert "couldn't start them" in out
    assert "temperature_c=" in out
    assert log == []


def test_fans_left_alone_when_fan_control_is_disabled(
    monkeypatch: pytest.MonkeyPatch, duties: list[tuple[str, float]]
) -> None:
    monkeypatch.setattr(sysconfig, "FAN_ENABLED", False)
    _ctrl_c_on_sleep(monkeypatch, after=0)
    assert read_sensor.main(["simulated"]) == 0
    assert duties == []
