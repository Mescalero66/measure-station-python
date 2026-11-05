import copy
from types import SimpleNamespace

from measure_station import sysconfig
from measure_station.main import validate_config


def _config(**overrides) -> SimpleNamespace:
    values = {k: copy.deepcopy(v) for k, v in vars(sysconfig).items() if k.isupper()}
    values.update(overrides)
    return SimpleNamespace(**values)


def test_shipped_sysconfig_is_valid() -> None:
    assert validate_config(_config(STATION_ID="pi-01")) == []


def test_duplicate_source_names_rejected() -> None:
    cfg = _config(
        STATION_ID="pi-01",
        SENSORS=[
            {"type": "simulated", "name": "dup", "interval_s": 1},
            {"type": "simulated", "name": "dup", "interval_s": 1},
        ],
    )
    assert any("duplicate" in e for e in validate_config(cfg))


def test_unknown_sensor_type_rejected() -> None:
    cfg = _config(STATION_ID="pi-01", SENSORS=[{"type": "nope", "name": "x", "interval_s": 1}])
    assert any("unknown sensor type" in e for e in validate_config(cfg))


def test_disabled_entries_are_not_validated() -> None:
    cfg = _config(
        STATION_ID="pi-01",
        FAN_ENABLED=False,  # its inputs aren't in SENSORS
        SENSORS=[{"type": "nope", "name": "x", "interval_s": 1, "enabled": False}],
    )
    assert validate_config(cfg) == []


def test_stale_threshold_must_exceed_backoff() -> None:
    cfg = _config(STATION_ID="pi-01", TASK_STALE_AFTER_S=30, TASK_RESTART_MAX_S=60)
    assert any("TASK_STALE_AFTER_S" in e for e in validate_config(cfg))


def test_topic_unsafe_station_id_rejected() -> None:
    assert validate_config(_config(STATION_ID="bad/id"))


def test_mqboard_sources_must_not_collide_with_sensors() -> None:
    cfg = _config(
        STATION_ID="pi-01",
        MQBOARD_ENABLED=True,
        SENSORS=[{"type": "simulated", "name": "mqboard-mq2", "interval_s": 1}],
    )
    assert any("duplicate source name 'mqboard-mq2'" in e for e in validate_config(cfg))


def test_mqboard_prefix_must_be_topic_safe() -> None:
    cfg = _config(STATION_ID="pi-01", MQBOARD_ENABLED=True, MQBOARD_SOURCE_PREFIX="mq/board-")
    assert any("must match" in e for e in validate_config(cfg))


def test_pressure_source_must_be_another_enabled_sensor() -> None:
    co2 = {"type": "scd41", "name": "co2", "interval_s": 60}
    barometer = {"type": "bmp280", "name": "barometer", "interval_s": 60}
    ok = _config(
        STATION_ID="pi-01",
        FAN_ENABLED=False,  # its inputs aren't in SENSORS
        SENSORS=[co2 | {"pressure_source": "barometer"}, barometer],
    )
    assert validate_config(ok) == []
    for sensors in (
        [co2 | {"pressure_source": "barometr"}, barometer],  # misspelt
        [co2 | {"pressure_source": "barometer"}, barometer | {"enabled": False}],
        [co2 | {"pressure_source": "co2"}],  # itself
    ):
        errors = validate_config(_config(STATION_ID="pi-01", FAN_ENABLED=False, SENSORS=sensors))
        assert any("isn't another enabled sensor" in e for e in errors), sensors


def test_fan_inputs_must_be_enabled_sensors() -> None:
    for overrides in (
        {"FAN_TEMPERATURE_SOURCE": "humidty"},  # misspelt
        {"FAN_REFERENCE_SOURCE": "humidty"},
    ):
        errors = validate_config(_config(STATION_ID="pi-01", **overrides))
        assert any("'humidty', which isn't an enabled sensor" in e for e in errors), overrides


def test_invalid_fan_settings_rejected() -> None:
    for overrides in (
        {"FAN_TEMPERATURE_DIFFERENCE_OFF_C": 8.0},  # on == off
        {"FAN_CONTROL_INTERVAL_S": 0},
        {"FAN_MIN_ON_S": -1},
        {"FAN_TEMPERATURE_SOURCE": "humidity"},  # the same as the reference
    ):
        errors = validate_config(_config(STATION_ID="pi-01", **overrides))
        assert errors, overrides


def test_disabled_fan_isnt_validated() -> None:
    cfg = _config(STATION_ID="pi-01", FAN_ENABLED=False, FAN_TEMPERATURE_DIFFERENCE_OFF_C=8.0)
    assert validate_config(cfg) == []
