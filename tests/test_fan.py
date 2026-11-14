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
        "heat": FanSwitch(on=8.0, off=3.0),
        "humidity_source": "humidity",
        "fog": FanSwitch(on=95.0, off=90.0),
        "interval_s": 0.01,
        "intake_min_on_s": 0,
        "fog_min_s": 0,
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


def test_switch_can_turn_on_as_the_input_falls() -> None:
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
    assert settings.humidity(latest) == 70
    assert settings.missing(latest) == []


def test_a_missing_humidity_is_missing() -> None:
    latest = LatestValues()
    latest.record("temperature-probe", {"temperature_c": 25.5}, valid_for_s=60)
    latest.record("humidity", {"temperature_c": 21.25}, valid_for_s=60)
    assert _settings().missing(latest) == ["humidity"]


def test_sources_are_listed_once() -> None:
    assert _settings().sources() == ["temperature-probe", "humidity"]


def test_temperature_sources_must_differ() -> None:
    with pytest.raises(ValueError, match="differ"):
        _settings(reference="temperature-probe")


@pytest.mark.parametrize(
    "bad",
    [
        {"interval_s": 0},
        {"intake_min_on_s": -1},
        {"fog_min_s": -1},
        {"fog_on_readings": 0},
        {"kickstart_s": -1},
        {"min_duty_pct": 101},
    ],
)
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


async def _run(
    exhaust: FakePwm, intake: FakePwm, settings: FanSettings, steps
) -> tuple[list[dict], dict]:
    """Run the task, calling each of ``steps`` (on its LatestValues) a few intervals apart.

    Returns the health values before each step and after the last, then after it stops.
    """
    latest = LatestValues()
    state = FanState()
    task = asyncio.create_task(
        control_fan(
            exhaust, intake, latest, state, settings=settings, heartbeat=Heartbeat("fan", None)
        )
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


def _report(status, exhaust_pct, intake_pct, difference_c) -> dict:
    return {
        "fan_status": status,
        "fan_exhaust_duty_pct": exhaust_pct,
        "fan_intake_duty_pct": intake_pct,
        "fan_temp_diff_c": difference_c,
    }


def test_status_before_and_after_fan_control_runs() -> None:
    state = FanState()
    assert state.health_values() == _report("starting", None, None, None)
    state.status, state.exhaust_duty_pct = "normal", 100
    state.clear()
    assert state.health_values() == _report("error", None, None, None)


def test_normally_the_exhaust_fans_draw_air_through() -> None:
    exhaust, intake = FakePwm(), FakePwm()
    reports, stopped = asyncio.run(_run(exhaust, intake, _settings(), [_readings(21, 20, 50)]))
    # Failsafe until the first readings, then normal: the same fans, so nothing more is
    # written until the stop sets the failsafe again.
    assert exhaust.duties == [100, 100]
    assert intake.duties == [0, 0]
    assert exhaust.closed and intake.closed
    assert reports == [
        _report("failsafe", 100, 0, None),
        _report("normal", 100, 0, 1),
    ]
    # Unknown once fan control has stopped.
    assert stopped == _report("error", None, None, None)


def test_heat_turns_the_intake_fans_on_too() -> None:
    exhaust, intake = FakePwm(), FakePwm()
    reports, _ = asyncio.run(
        _run(
            exhaust,
            intake,
            _settings(),
            [
                _readings(30, 20, 50),  # 10 °C: on
                _readings(25, 20, 50),  # 5 °C, between off and on: stays on
                _readings(22, 20, 50),  # 2 °C: off
            ],
        )
    )
    assert exhaust.duties == [100, 100]  # on throughout
    assert intake.duties == [0, 100, 0, 0]
    assert reports[1:] == [
        _report("heat", 100, 100, 10),
        _report("heat", 100, 100, 5),
        _report("normal", 100, 0, 2),
    ]


def test_fog_slows_the_exhaust_fans() -> None:
    exhaust, intake = FakePwm(), FakePwm()
    reports, _ = asyncio.run(
        _run(
            exhaust,
            intake,
            # Binary, so the minimum is only fog's speed. The kick at the start is over
            # by the first readings.
            _settings(min_duty_pct=40, kickstart_s=0.02),
            [
                _readings(21, 20, 96),  # fog
                _readings(21, 20, 92),  # between off and on: still fog
                _readings(21, 20, 89),  # fog over
            ],
        )
    )
    # Still turning in fog, so no kick-start either way.
    assert exhaust.duties == [100, 40, 100, 100]
    assert intake.duties == [0, 0]
    assert reports[1:] == [
        _report("fog", 40, 0, 1),
        _report("fog", 40, 0, 1),
        _report("normal", 100, 0, 1),
    ]


def test_fog_needs_humid_readings_in_a_row() -> None:
    exhaust, intake = FakePwm(), FakePwm()
    foggy, dry = _readings(21, 20, 96), _readings(21, 20, 50)
    settings = _settings(fog_on_readings=2, min_duty_pct=40)
    reports, _ = asyncio.run(_run(exhaust, intake, settings, [foggy, dry, foggy, foggy]))
    # Each reading is evaluated about 5 times, but counts once: the first foggy reading
    # alone doesn't start fog, and a dry one starts the count again.
    assert [r["fan_status"] for r in reports] == ["failsafe", "normal", "normal", "normal", "fog"]
    assert reports[-1] == _report("fog", 40, 0, 1)


def test_heat_overrides_fog() -> None:
    exhaust, intake = FakePwm(), FakePwm()
    reports, _ = asyncio.run(_run(exhaust, intake, _settings(), [_readings(30, 20, 97)]))
    assert reports[1] == _report("heat", 100, 100, 10)


def test_a_missing_reading_overrides_fog() -> None:
    exhaust, intake = FakePwm(), FakePwm()
    reports, _ = asyncio.run(_run(exhaust, intake, _settings(), [_readings(None, 20, 97)]))
    assert reports[1] == _report("failsafe", 100, 0, None)
    assert exhaust.duties == [100, 100]


def test_the_intake_fans_stay_on_for_their_minimum_time() -> None:
    exhaust, intake = FakePwm(), FakePwm()
    hot, cool = _readings(30, 20, 50), _readings(21, 20, 50)
    # On at 0.05 s, then cool from 0.1 s: held on until 0.35 s.
    settings = _settings(intake_min_on_s=0.3)
    reports, _ = asyncio.run(_run(exhaust, intake, settings, [hot] + [cool] * 7))
    assert intake.duties == [0, 100, 0, 0]
    assert reports[2] == _report("heat_hold", 100, 100, 1)  # at 0.15 s: held on
    assert reports[-1] == _report("normal", 100, 0, 1)  # at 0.45 s: off


def test_fog_mode_lasts_its_minimum_time() -> None:
    exhaust, intake = FakePwm(), FakePwm()
    foggy, dry = _readings(21, 20, 96), _readings(21, 20, 50)
    # Fog from 0.05 s, dry from 0.1 s: held until 0.35 s.
    settings = _settings(fog_min_s=0.3, min_duty_pct=40)
    reports, _ = asyncio.run(_run(exhaust, intake, settings, [foggy] + [dry] * 7))
    assert exhaust.duties == [100, 40, 100, 100]
    assert reports[2] == _report("fog_hold", 40, 0, 1)  # at 0.15 s: held
    assert reports[-1] == _report("normal", 100, 0, 1)  # at 0.45 s: over


def test_fog_mode_cant_start_again_until_the_minimum_on_time() -> None:
    exhaust, intake = FakePwm(), FakePwm()
    foggy, dry = _readings(21, 20, 96), _readings(21, 20, 50)
    # Fog from 0.05 s, over at 0.1 s, foggy again from 0.15 s: can't start until 0.4 s.
    settings = _settings(intake_min_on_s=0.3, min_duty_pct=40)
    reports, _ = asyncio.run(_run(exhaust, intake, settings, [foggy, dry] + [foggy] * 6))
    assert reports[1]["fan_status"] == "fog"  # at 0.1 s
    assert reports[3] == _report("fog_lockout", 100, 0, 1)  # at 0.2 s: held off
    assert reports[-1] == _report("fog", 40, 0, 1)  # at 0.45 s: fog again


def test_fans_go_to_failsafe_when_readings_go_stale() -> None:
    exhaust, intake = FakePwm(), FakePwm()
    steps = [_readings(21, 20, 96), _readings(21, 20, 96, -1)]  # fog, then stale
    asyncio.run(_run(exhaust, intake, _settings(), steps))
    assert exhaust.duties == [100, 0, 100, 100]
    assert intake.duties == [0, 0]


def test_analog_speed_follows_the_temperature_difference() -> None:
    exhaust, intake = FakePwm(), FakePwm()
    settings = _settings(analog=True, min_duty_pct=40)
    reports, _ = asyncio.run(
        _run(
            exhaust,
            intake,
            settings,
            [
                _readings(21, 20, 50),  # 1 °C, below off (3): the minimum
                _readings(25.5, 20, 50),  # 5.5 °C, half way from off to on: 70 %
                _readings(29, 20, 50),  # 9 °C, past on: 100 %, and the intake fans on
                _readings(25.5, 20, 50),  # 5.5 °C: both at 70 %, intake still on
                _readings(22, 20, 50),  # 2 °C: intake off, exhaust at the minimum
            ],
        )
    )
    # 100 % (failsafe) until the first readings, and again on stop.
    assert exhaust.duties == [100, 40, 70, 100, 70, 40, 100]
    assert intake.duties == [0, 100, 70, 0, 0]
    assert [r["fan_exhaust_duty_pct"] for r in reports] == [100, 40, 70, 100, 70, 40]
    assert [r["fan_intake_duty_pct"] for r in reports] == [0, 0, 0, 100, 70, 0]


def test_a_fan_set_starting_from_standstill_is_kick_started() -> None:
    exhaust, intake = FakePwm(), FakePwm()
    cool, warmer = _readings(21, 20, 50), _readings(25.5, 20, 50)
    # A minimum of 0 stops the exhaust fans at or below OFF.
    settings = _settings(analog=True, min_duty_pct=0, kickstart_s=0.1)
    reports, _ = asyncio.run(_run(exhaust, intake, settings, [cool, warmer, warmer, warmer]))
    # 100 % (failsafe) at the start; stopped at 1 °C; at 5.5 °C, 100 % for 0.1 s (from
    # 0.1 s), then its speed; 100 % again on stop.
    assert exhaust.duties == [100, 0, 100, 50, 100]
    assert reports[2]["fan_exhaust_duty_pct"] == 100  # at 0.15 s: kick-starting
    assert reports[4]["fan_exhaust_duty_pct"] == 50  # at 0.25 s: at its speed


def test_analog_runs_at_full_speed_while_a_reading_is_missing() -> None:
    settings = _settings(analog=True, min_duty_pct=40)
    assert settings.speed(None) == 100
    assert settings.speed(-10) == 40
    assert settings.speed(20) == 100
    assert _settings().speed(1) == 100  # binary


def test_a_failed_open_raises_for_the_supervisor() -> None:
    exhaust, intake = FakePwm(), FakePwm(fail_open=True)
    with pytest.raises(OSError):
        asyncio.run(_run(exhaust, intake, _settings(), []))
    # Both left in the failsafe state, and closed.
    assert exhaust.duties == [100] and intake.duties == [0]
    assert exhaust.closed and intake.closed


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
