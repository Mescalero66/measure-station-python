import json

from measure_station.models import MessageEncoder, StoredReading, encode_values


def _stored(quality: str, values: dict | None = None, meta: dict | None = None) -> StoredReading:
    return StoredReading(
        id=1,
        source="ambient",
        sensor_type="ens160_aht21",
        values_json=encode_values(values or {"t": 21.5}),
        meta_json=encode_values(meta or {}),
        ts=0.0,
        ts_quality=quality,
        boot_id="boot-A",
        boot_ts=12.5,
    )


def test_encode_topic_and_payload() -> None:
    encoder = MessageEncoder("site/", "pi-01")

    topic, payload = encoder.encode(_stored("ntp", meta={"comp_src": "hpa"}))

    assert topic == "site/pi-01/ambient"
    assert encoder.status_topic == "site/pi-01/status"
    assert json.loads(payload) == {
        "station": "pi-01",
        "source": "ambient",
        "sensor_type": "ens160_aht21",
        "ts": "1970-01-01T00:00:00.000+00:00",
        "ts_src": "ntp",
        "values": {"t": 21.5},
        "meta": {"comp_src": "hpa"},
    }


def test_empty_meta_is_left_out() -> None:
    _, payload = MessageEncoder("base", "s").encode(_stored("ntp"))
    assert "meta" not in json.loads(payload)


def test_uncorrected_reading_is_published_as_estimated_with_boot_reference() -> None:
    _, payload = MessageEncoder("base", "s").encode(_stored("pending"))
    decoded = json.loads(payload)
    assert decoded["ts_src"] == "estimated"
    assert decoded["boot_id"] == "boot-A"
    assert decoded["boot_ts"] == 12.5


def test_non_finite_values_become_null() -> None:
    values = json.loads(encode_values({"a": float("nan"), "b": [float("inf"), 1.0]}))
    assert values == {"a": None, "b": [None, 1.0]}
