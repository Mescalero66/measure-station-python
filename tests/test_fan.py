import asyncio
from pathlib import Path

import pytest

from measure_station.fan import FanCurve, FanInput, FanSettings, FanState, SysfsPwm, control_fan
from measure_station.sensors import LatestValues
from measure_station.supervisor import Heartbeat


def _curve(**overrides) -> FanCurve:
    options = {
        "start": 60.0,
        "full": 80.0,
        "hysteresis": 3.0,
        "min_duty_pct": 30.0,
        "max_duty_pct": 100.0,
        "off_duty_pct": 0.0,
    }
    return FanCurve(**(options | overrides))


def _temperature_difference() -> FanInput:
    return FanInput(
        name="temp_diff",
        key="temp_diff_c",
        sources=("temperature-probe", "humidity"),
        field="temperature_c",
        curve=_curve(start=2.0, full=8.0, hysteresis=0.5),
        report_value=True,
    )


def _humidity() -> FanInput:
    return FanInput(
        name="humidity",
        key="humidity_pct",
        sources=("humidity",),
        field="humidity_pct",
        curve=_curve(),
    )


def _settings(**overrides) -> FanSettings:
    options = {
        "inputs": (_temperature_difference(), _humidity()),
        "failsafe_duty_pct": 50.0,
        "interval_s": 0.01,
    }
    return FanSettings(**(options | overrides))


# --------------------------------------------------------------------------- the curve


def test_curve_ramps_from_start_to_full() -> None:
    curve = _curve()
    assert curve.duty(50, running=False) == (0, False)
    assert curve.duty(60, running=False) == (30, True)
    assert curve.duty(70, running=False) == (65, True)
    assert curve.duty(80, running=False) == (100, True)
    assert curve.duty(95, running=False) == (100, True)


def test_curve_turns_off_only_past_the_hysteresis() -> None:
    curve = _curve()
    assert curve.duty(58, running=True) == (30, True)  # within 3 of start: stays on
    assert curve.duty(56.9, running=True) == (0, False)
    assert curve.duty(58, running=False) == (0, False)  # and doesn't turn on there


def test_curve_can_run_as_the_input_falls() -> None:
    curve = _curve(start=0, full=-5, hysteresis=0.5)
    assert curve.duty(1, running=False) == (0, False)
    assert curve.duty(-2.5, running=False) == (65, True)
    assert curve.duty(0.4, running=True) == (30, True)
    assert curve.duty(0.6, running=True) == (0, False)


@pytest.mark.parametrize(
    "bad",
    [
        {"full": 60.0},
        {"hysteresis": -1},
        {"min_duty_pct": 101},
        {"min_duty_pct": 80, "max_duty_pct": 50},
    ],
)
def test_invalid_curves_rejected(bad: dict) -> None:
    with pytest.raises(ValueError):
        _curve(**bad)


# --------------------------------------------------------------------------- inputs


def test_temperature_difference_is_probe_minus_reference() -> None:
    latest = LatestValues()
    fan_input = _temperature_difference()
    latest.record("temperature-probe", {"temperature_c": 25.5}, valid_for_s=60)
    assert fan_input.measure(latest) is None
    assert fan_input.missing(latest) == ["humidity"]
    latest.record("humidity", {"temperature_c": 21.25, "humidity_pct": 70}, valid_for_s=60)
    assert fan_input.measure(latest) == 4.25
    assert fan_input.missing(latest) == []


def test_humidity_input() -> None:
    latest = LatestValues()
    latest.record("humidity", {"temperature_c": 21.25, "humidity_pct": 70.1}, valid_for_s=60)
    assert _humidity().measure(latest) == 70.1


def test_input_sources_are_listed_once() -> None:
    assert _settings().input_sources() == ["temperature-probe", "humidity"]


def test_temperature_sources_must_differ() -> None:
    with pytest.raises(ValueError, match="differ"):
        FanInput("t", "t_c", ("humidity", "humidity"), "temperature_c", _curve())


# --------------------------------------------------------------------------- the task


class FakePwm:
    def __init__(self, fail_open: bool = False) -> None:
        self.duties: list[float] = []
        self.fail_open = fail_open
        self.closed = False

    def open(self) -> None:
        if self.fail_open:
            raise OSError("no pwmchip0")

    def set_duty(self, duty_pct: float) -> None:
        self.duties.append(duty_pct)

    def close(self) -> None:
        self.closed = True


async def _run(pwm: FakePwm, settings: FanSettings, steps) -> tuple[list[dict], dict]:
    """Run the task, calling each of ``steps`` (on its LatestValues) a few intervals apart.

    Returns the health values before each step and after the last, then after it stops.
    """
    latest = LatestValues()
    state = FanState(settings.inputs)
    task = asyncio.create_task(
        control_fan(pwm, latest, state, settings=settings, heartbeat=Heartbeat("fan", None))
    )
    reports = []
    for step in [*steps, None]:
        await asyncio.sleep(0.05)
        reports.append(state.health_values())
        if step is not None:
            step(latest)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return reports, state.health_values()


def _readings(probe_c: float | None, air_c: float, humidity_pct: float, valid_for_s: float = 60):
    def step(latest: LatestValues) -> None:
        if probe_c is not None:
            latest.record("temperature-probe", {"temperature_c": probe_c}, valid_for_s=valid_for_s)
        latest.record(
            "humidity",
            {"temperature_c": air_c, "humidity_pct": humidity_pct},
            valid_for_s=valid_for_s,
        )

    return step


def _report(failsafe, duty, difference_c, difference_duty, humidity_duty) -> dict:
    return {
        "fan_failsafe": failsafe,
        "fan_duty_pct": duty,
        "fan_temp_diff_c": difference_c,
        "fan_temp_diff_duty_pct": difference_duty,
        "fan_humidity_duty_pct": humidity_duty,
    }


def test_the_higher_demand_wins() -> None:
    pwm = FakePwm()
    reports, stopped = asyncio.run(
        _run(
            pwm,
            _settings(),
            [
                _readings(25, 20, 50),  # difference 5 °C: 65 %; humidity off
                _readings(21, 20, 75),  # difference off; humidity 75 %: 82.5, rounded to 82 %
                _readings(21, 20, 50),  # both off
            ],
        )
    )
    # Failsafe until the first readings; then the controls; failsafe again on stop.
    assert pwm.duties == [50, 65, 82, 0, 50]
    assert pwm.closed
    assert reports == [
        _report(True, 50, None, None, None),
        _report(False, 65, 5, 65, 0),
        _report(False, 82, 1, 0, 82),
        _report(False, 0, 1, 0, 0),
    ]
    # Unknown once fan control has stopped.
    assert stopped == _report(None, None, None, None, None)


def test_a_missing_input_raises_the_duty_to_at_least_the_failsafe() -> None:
    pwm = FakePwm()
    reports, _ = asyncio.run(
        _run(
            pwm,
            _settings(),
            [
                _readings(None, 20, 50),  # no probe; humidity off: failsafe
                _readings(None, 20, 80),  # no probe; humidity 80 %: above the failsafe
            ],
        )
    )
    assert pwm.duties == [50, 100, 50]
    assert reports[1:] == [_report(True, 50, None, None, 0), _report(True, 100, None, None, 100)]


def test_fans_go_to_failsafe_when_inputs_go_stale() -> None:
    pwm = FakePwm()
    asyncio.run(_run(pwm, _settings(), [_readings(21, 20, 50), _readings(21, 20, 50, -1)]))
    assert pwm.duties == [50, 0, 50, 50]


def test_a_failed_open_raises_for_the_supervisor() -> None:
    pwm = FakePwm(fail_open=True)
    with pytest.raises(OSError):
        asyncio.run(_run(pwm, _settings(), []))
    assert pwm.closed


# --------------------------------------------------------------------------- sysfs PWM


def _chip(root: Path, *, exported: bool, period: str = "0") -> Path:
    chip = root / "pwmchip0"
    chip.mkdir()
    (chip / "export").write_text("")
    if exported:
        channel = chip / "pwm0"
        channel.mkdir()
        for name, value in (("period", period), ("duty_cycle", "0"), ("enable", "0")):
            (channel / name).write_text(value)
    return chip / "pwm0"


def test_sysfs_pwm_sets_period_and_duty(tmp_path: Path) -> None:
    channel = _chip(tmp_path, exported=True)
    pwm = SysfsPwm(frequency_hz=100, root=tmp_path)
    pwm.open()
    assert (channel / "period").read_text() == "10000000"
    assert (channel / "enable").read_text() == "1"
    pwm.set_duty(65)
    assert (channel / "duty_cycle").read_text() == "6500000"
    with pytest.raises(ValueError):
        pwm.set_duty(101)


def test_sysfs_pwm_inverted(tmp_path: Path) -> None:
    channel = _chip(tmp_path, exported=True)
    pwm = SysfsPwm(frequency_hz=100, inverted=True, root=tmp_path)
    pwm.open()
    pwm.set_duty(25)
    assert (channel / "duty_cycle").read_text() == "7500000"


def test_sysfs_pwm_reopen_keeps_the_duty_cycle(tmp_path: Path) -> None:
    channel = _chip(tmp_path, exported=True, period="10000000")
    (channel / "duty_cycle").write_text("6500000")
    SysfsPwm(frequency_hz=100, root=tmp_path).open()
    assert (channel / "duty_cycle").read_text() == "6500000"


def test_sysfs_pwm_exports_the_channel(tmp_path: Path, monkeypatch) -> None:
    _chip(tmp_path, exported=False)
    monkeypatch.setattr("measure_station.fan.pwm._EXPORT_WAIT_S", 0.1)
    with pytest.raises(FileNotFoundError):  # nothing here creates pwm0, as the kernel would
        SysfsPwm(root=tmp_path).open()
    assert (tmp_path / "pwmchip0" / "export").read_text() == "0"


def test_sysfs_pwm_without_the_overlay(tmp_path: Path) -> None:
    with pytest.raises(OSError, match="dtoverlay"):
        SysfsPwm(root=tmp_path).open()
