import asyncio
import contextlib
import json
from typing import Any

import aiomqtt
import pytest

from measure_station import live_test, sysconfig
from measure_station.clock import ClockState
from measure_station.models import MessageEncoder, Reading
from measure_station.sensors import Sensor


class FakeClient:
    """Stands in for aiomqtt.Client: refuses the first ``fail`` connections."""

    def __init__(self, sent: list[tuple[str, dict[str, Any], int]], fail: int = 0) -> None:
        self.sent, self.fail, self.connects = sent, fail, 0

    def __call__(self, **kwargs: Any) -> "FakeClient":
        assert kwargs["identifier"] == f"{sysconfig.MQTT_CLIENT_ID}-test"
        return self

    async def __aenter__(self) -> "FakeClient":
        self.connects += 1
        if self.connects <= self.fail:
            raise aiomqtt.MqttError("Connection refused")
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    async def publish(self, topic: str, payload: str, qos: int) -> None:
        self.sent.append((topic, json.loads(payload), qos))


class FakePwm:
    def __init__(self, name: str, log: list[tuple[str, float]]) -> None:
        self.name, self.log, self.closed = name, log, False

    def open(self) -> None:
        pass

    def set_duty(self, duty_pct: float) -> None:
        self.log.append((self.name, duty_pct))

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any], int]]:
    """What the fake broker received, as (topic, payload, qos)."""
    received: list[tuple[str, dict[str, Any], int]] = []
    monkeypatch.setattr(aiomqtt, "Client", FakeClient(received))
    return received


@pytest.fixture
def only_simulated(monkeypatch: pytest.MonkeyPatch) -> None:
    """One sensor, not normally published, and no MQ board or fans."""
    entry = {
        "type": "simulated",
        "name": "simulated",
        "interval_s": 60,
        "channels": ["temperature_c", "humidity_pct"],
        "publish": False,
    }
    monkeypatch.setattr(sysconfig, "SENSORS", [entry, {**entry, "name": "off", "enabled": False}])
    monkeypatch.setattr(sysconfig, "MQBOARD_ENABLED", False)
    monkeypatch.setattr(sysconfig, "FAN_ENABLED", False)


async def _run_for(seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(live_test.run(sysconfig, "test-pc", 1883), seconds)


def test_bad_arguments_print_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert live_test.main([]) == 2
    assert live_test.main(["test-pc", "port"]) == 2
    assert "Usage" in capsys.readouterr().out


@pytest.mark.usefixtures("only_simulated")
def test_reads_every_enabled_sensor_every_10_s_and_sends_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sent: list[tuple[str, dict[str, Any], int]],
) -> None:
    polled: list[Sensor] = []
    real_poll = live_test.poll_sensor

    def spy(sensor: Sensor, *args: Any, **kwargs: Any) -> Any:
        polled.append(sensor)
        return real_poll(sensor, *args, **kwargs)

    monkeypatch.setattr(live_test, "poll_sensor", spy)
    asyncio.run(_run_for(0.5))

    assert [(s.name, s.interval_s, s.publish) for s in polled] == [("simulated", 10.0, True)]
    by_source = {payload["source"]: (topic, payload, qos) for topic, payload, qos in sent}
    assert sorted(by_source) == ["health", "simulated"]
    topic, payload, qos = by_source["simulated"]  # sent though its entry says "publish": False
    assert topic == f"{sysconfig.MQTT_BASE_TOPIC}/{sysconfig.STATION_ID}/simulated"
    assert qos == 1
    assert payload["ts_src"] == "ntp"  # a dev machine's clock counts as synchronised
    assert set(payload["values"]) == {"temperature_c", "humidity_pct"}
    assert "simulated" in capsys.readouterr().out


@pytest.mark.usefixtures("only_simulated")
def test_sends_the_services_health_report(sent: list[tuple[str, dict[str, Any], int]]) -> None:
    asyncio.run(_run_for(0.5))
    [(topic, payload, _)] = [s for s in sent if s[1]["source"] == "health"]
    assert topic == f"{sysconfig.MQTT_BASE_TOPIC}/{sysconfig.STATION_ID}/health"
    assert payload["sensor_type"] == "station"
    values = payload["values"]
    assert values["outbox_backlog"] == values["awaiting_time_sync"] == 0  # nothing stored
    assert values["clock_synced"] is True
    assert "disk_free_mb" in values  # of DATA_DIR, or of the current directory without one
    assert not any(key.startswith(("fan_", "mqboard_")) for key in values)  # both disabled


def test_readings_are_only_printed_while_the_broker_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    received: list[tuple[str, dict[str, Any], int]] = []
    client = FakeClient(received, fail=1)
    monkeypatch.setattr(aiomqtt, "Client", client)

    async def go() -> None:
        queue: asyncio.Queue[Reading] = asyncio.Queue()
        task = asyncio.create_task(
            live_test.broadcast(
                queue,
                ClockState(sync_check=lambda: True),
                MessageEncoder("base", "pi-01"),
                host="test-pc",
                port=1883,
                client_id=f"{sysconfig.MQTT_CLIENT_ID}-test",
                reconnect_max_s=1.0,
                heartbeat=live_test.Heartbeat("broadcast", None),
            )
        )
        await queue.put(Reading("lost", "simulated", {"temperature_c": 1.0}))
        await asyncio.sleep(0.2)
        assert queue.empty()  # taken and printed while waiting to reconnect: never blocks
        async with asyncio.timeout(3):  # the reconnect is ~1 s later
            while client.connects < 2:
                await asyncio.sleep(0.05)
        await queue.put(Reading("sent", "simulated", {"temperature_c": 2.0}, meta={"x": "y"}))
        await asyncio.sleep(0.1)
        task.cancel()

    asyncio.run(go())
    assert [(topic, p["values"], p.get("meta")) for topic, p, _ in received] == [
        ("base/pi-01/sent", {"temperature_c": 2.0}, {"x": "y"})
    ]
    out = capsys.readouterr().out
    assert "lost" in out
    assert "| meta: x=y" in out


@pytest.mark.usefixtures("only_simulated")
def test_fan_control_runs_and_is_left_in_failsafe(
    monkeypatch: pytest.MonkeyPatch, sent: list[tuple[str, dict[str, Any], int]]
) -> None:
    log: list[tuple[str, float]] = []
    outputs = (FakePwm("exhaust", log), FakePwm("intake", log))
    monkeypatch.setattr(live_test, "fan_outputs", lambda cfg: outputs)
    monkeypatch.setattr(sysconfig, "FAN_ENABLED", True)
    asyncio.run(_run_for(0.5))
    [health] = [payload["values"] for _, payload, _ in sent if payload["source"] == "health"]
    assert "fan_failsafe" in health  # the health report shares fan control's state
    # Its inputs (the DS18B20 and CHT8305) aren't enabled here, so it runs on failsafe.
    assert log[:2] == [("exhaust", 100), ("intake", 0)]
    assert log[-2:] == [("exhaust", 100), ("intake", 0)]  # on stop, as the service leaves them
    assert all(output.closed for output in outputs)
