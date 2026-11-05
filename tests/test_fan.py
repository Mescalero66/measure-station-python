import asyncio
from pathlib import Path

import pytest

from measure_station.fan import FanSettings, FanState, FanSwitch, SysfsPwm, control_fan
from measure_station.sensors import LatestValues
from measure_station.supervisor import Heartbeat


def _settings(**overrides) -> FanSettings:
    options = {
        "source": "temperature-probe",
        "reference": "humidity",
        "switch": FanSwitch(on=8.0, off=4.0),
        "failsafe_on": True,
        "interval_s": 0.01,
        "min_on_s": 0,
    }
    return FanSettings(**(options | overrides))


# --------------------------------------------------------------------------- the switch


def test_switch_turns_on_at_on_and_off_at_off() -> None:
    switch = FanSwitch(on=80, off=70)
    assert switch.is_on(69, was_on=False) is False
    assert switch.is_on(75, was_on=False) is False  # between: stays off
    assert switch.is_on(80, was_on=False) is True
    assert switch.is_on(75, was_on=True) is True  # between: stays on
    assert switch.is_on(70, was_on=True) is False
    assert switch.is_on(95, was_on=True) is True


def test_switch_can_run_as_the_input_falls() -> None:
    switch = FanSwitch(on=-5, off=0)
    assert switch.is_on(1, was_on=False) is False
    assert switch.is_on(-3, was_on=False) is False
    assert switch.is_on(-5, was_on=False) is True
    assert switch.is_on(-1, was_on=True) is True
    assert switch.is_on(0, was_on=True) is False


def test_switch_on_and_off_must_differ() -> None:
    with pytest.raises(ValueError, match="differ"):
        FanSwitch(on=70, off=70)


# --------------------------------------------------------------------------- settings


def test_temperature_difference_is_probe_minus_reference() -> None:
    latest = LatestValues()
    settings = _settings()
    latest.record("temperature-probe", {"temperature_c": 25.5}, valid_for_s=60)
    assert settings.temperature_difference(latest) is None
    assert settings.missing(latest) == ["humidity"]
    latest.record("humidity", {"temperature_c": 21.25, "humidity_pct": 70}, valid_for_s=60)
    assert settings.temperature_difference(latest) == 4.25
    assert settings.missing(latest) == []


def test_sources_must_differ() -> None:
    with pytest.raises(ValueError, match="differ"):
        _settings(reference="temperature-probe")


@pytest.mark.parametrize("bad", [{"interval_s": 0}, {"min_on_s": -1}])
def test_invalid_settings_rejected(bad: dict) -> None:
    with pytest.raises(ValueError):
        _settings(**bad)


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
    state = FanState()
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


def _readings(probe_c: float | None, air_c: float, valid_for_s: float = 60):
    def step(latest: LatestValues) -> None:
        if probe_c is not None:
            latest.record("temperature-probe", {"temperature_c": probe_c}, valid_for_s=valid_for_s)
        latest.record("humidity", {"temperature_c": air_c}, valid_for_s=valid_for_s)

    return step


def _report(failsafe, on, difference_c, difference_on) -> dict:
    return {
        "fan_failsafe": failsafe,
        "fan_on": on,
        "fan_temp_diff_c": difference_c,
        "fan_temp_diff_on": difference_on,
    }


def test_the_temperature_difference_switches_the_fans() -> None:
    pwm = FakePwm()
    reports, stopped = asyncio.run(
        _run(
            pwm,
            _settings(),
            [
                _readings(30, 20),  # 10 °C: on
                _readings(26, 20),  # 6 °C, between off and on: stays on
                _readings(23, 20),  # 3 °C: off
                _readings(26, 20),  # 6 °C: stays off
                _readings(29, 20),  # 9 °C: on
            ],
        )
    )
    # Failsafe (on) until the first readings; failsafe again on stop.
    assert pwm.duties == [100, 0, 100, 100]
    assert pwm.closed
    assert reports == [
        _report(True, True, None, None),
        _report(False, True, 10, True),
        _report(False, True, 6, True),
        _report(False, False, 3, False),
        _report(False, False, 6, False),
        _report(False, True, 9, True),
    ]
    # Unknown once fan control has stopped.
    assert stopped == _report(None, None, None, None)


def test_fans_stay_on_for_the_minimum_on_time() -> None:
    pwm = FakePwm()
    off = _readings(21, 20)  # 1 °C: off
    # On at the start (failsafe), then off at 0.05 s: held on until 0.3 s.
    reports, _ = asyncio.run(_run(pwm, _settings(min_on_s=0.3), [off] * 8))
    assert pwm.duties == [100, 0, 100]
    assert reports[2] == _report(False, True, 1, False)  # at 0.1 s: held on
    assert reports[-1] == _report(False, False, 1, False)  # at 0.45 s: off


def test_a_missing_input_turns_the_fans_on() -> None:
    pwm = FakePwm()
    reports, _ = asyncio.run(_run(pwm, _settings(), [_readings(None, 20)]))
    assert pwm.duties == [100, 100]
    assert reports[1:] == [_report(True, True, None, None)]


def test_without_failsafe_a_missing_input_leaves_the_fans_off() -> None:
    pwm = FakePwm()
    reports, _ = asyncio.run(
        _run(pwm, _settings(failsafe_on=False), [_readings(None, 20), _readings(30, 20)])
    )
    assert pwm.duties == [0, 100, 0]
    assert reports[1:] == [_report(True, False, None, None), _report(False, True, 10, True)]


def test_fans_go_to_failsafe_when_inputs_go_stale() -> None:
    pwm = FakePwm()
    asyncio.run(_run(pwm, _settings(), [_readings(21, 20), _readings(21, 20, -1)]))
    assert pwm.duties == [100, 0, 100, 100]


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
