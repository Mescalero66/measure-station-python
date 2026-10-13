import json

from measure_station.models import MessageEncoder, Reading


def test_encode_topic_and_payload() -> None:
    encoder = MessageEncoder("site/", "pi-01")
    reading = Reading(source="ambient", values={"t": 21.5}, timestamp=0.0, clock_synced=True)

    message = encoder.encode(reading)

    assert message.topic == "site/pi-01/ambient"
    assert encoder.status_topic == "site/pi-01/status"
    assert json.loads(message.payload) == {
        "station": "pi-01",
        "source": "ambient",
        "ts": "1970-01-01T00:00:00.000+00:00",
        "clock_synced": True,
        "values": {"t": 21.5},
    }


def test_non_finite_values_become_null() -> None:
    encoder = MessageEncoder("base", "s")
    reading = Reading(source="x", values={"a": float("nan"), "b": [float("inf"), 1.0]})

    payload = json.loads(encoder.encode(reading).payload)

    assert payload["values"] == {"a": None, "b": [None, 1.0]}
