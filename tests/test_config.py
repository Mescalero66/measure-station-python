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
        SENSORS=[{"type": "nope", "name": "x", "interval_s": 1, "enabled": False}],
    )
    assert validate_config(cfg) == []


def test_stale_threshold_must_exceed_backoff() -> None:
    cfg = _config(STATION_ID="pi-01", TASK_STALE_AFTER_S=30, TASK_RESTART_MAX_S=60)
    assert any("TASK_STALE_AFTER_S" in e for e in validate_config(cfg))


def test_topic_unsafe_station_id_rejected() -> None:
    assert validate_config(_config(STATION_ID="bad/id"))
