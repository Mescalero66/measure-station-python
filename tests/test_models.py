import json

from measure_station.models import MessageEncoder, StoredReading, encode_values


def _stored(quality: str, values: dict | None = None) -> StoredReading:
    return StoredReading(
        id=1,
        source="ambient",
        values_json=encode_values(values or {"t": 21.5}),
        ts=0.0,
        ts_quality=quality,
        boot_id="boot-A",
        boot_ts=12.5,
    )


def test_encode_topic_and_payload() -> None:
    encoder = MessageEncoder("site/", "pi-01")

    topic, payload = encoder.encode(_stored("ntp"))

    assert topic == "site/pi-01/ambient"
    assert encoder.status_topic == "site/pi-01/status"
    assert json.loads(payload) == {
        "station": "pi-01",
        "source": "ambient",
        "ts": "1970-01-01T00:00:00.000+00:00",
        "ts_quality": "ntp",
        "values": {"t": 21.5},
    }


def test_uncorrected_reading_is_published_as_estimated_with_boot_reference() -> None:
    _, payload = MessageEncoder("base", "s").encode(_stored("pending"))
    decoded = json.loads(payload)
    assert decoded["ts_quality"] == "estimated"
    assert decoded["boot_id"] == "boot-A"
    assert decoded["boot_ts"] == 12.5


def test_non_finite_values_become_null() -> None:
    values = json.loads(encode_values({"a": float("nan"), "b": [float("inf"), 1.0]}))
    assert values == {"a": None, "b": [None, 1.0]}
