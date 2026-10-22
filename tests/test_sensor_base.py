import asyncio
import time

import pytest

from measure_station.models import Reading
from measure_station.sensors import LatestValues, Sample, create_sensor
from measure_station.sensors.base import Sensor, poll_sensor
from measure_station.supervisor import Heartbeat


class FailsToOpen(Sensor):
    """Acquires a handle, then fails, like a bus that opens but finds no device."""

    def __init__(self) -> None:
        super().__init__("x", 1)
        self.handle_open = False

    def open(self) -> None:
        self.handle_open = True
        raise OSError("device not found")

    def read(self) -> dict[str, float]:
        return {}

    def close(self) -> None:
        self.handle_open = False


def test_failed_open_is_closed() -> None:
    sensor = FailsToOpen()

    async def scenario() -> None:
        await poll_sensor(sensor, asyncio.Queue(), read_timeout_s=1, heartbeat=Heartbeat("x", None))

    with pytest.raises(OSError):
        asyncio.run(scenario())
    assert not sensor.handle_open


def test_readings_carry_source_and_sensor_type() -> None:
    sensor = create_sensor(
        {"type": "simulated", "name": "bench", "interval_s": 60, "channels": ["temperature_c"]}
    )

    async def scenario() -> Reading:
        queue: asyncio.Queue[Reading] = asyncio.Queue()
        task = asyncio.create_task(
            poll_sensor(sensor, queue, read_timeout_s=1, heartbeat=Heartbeat("x", None))
        )
        reading = await asyncio.wait_for(queue.get(), 1)
        task.cancel()
        return reading

    reading = asyncio.run(scenario())
    assert (reading.source, reading.sensor_type) == ("bench", "simulated")
    assert list(reading.values) == ["temperature_c"]


def test_polling_records_latest_values_for_other_sensors() -> None:
    sensor = create_sensor(
        {"type": "simulated", "name": "bench", "interval_s": 60, "channels": ["pressure_hpa"]}
    )
    sensor.latest_values = LatestValues()

    async def scenario() -> Reading:
        queue: asyncio.Queue[Reading] = asyncio.Queue()
        task = asyncio.create_task(
            poll_sensor(sensor, queue, read_timeout_s=1, heartbeat=Heartbeat("x", None))
        )
        reading = await asyncio.wait_for(queue.get(), 1)
        await asyncio.sleep(0)  # let it record
        task.cancel()
        return reading

    reading = asyncio.run(scenario())
    assert sensor.latest_values.get("bench", "pressure_hpa") == reading.values["pressure_hpa"]
    assert sensor.latest_values.get("bench", "no_such_key") is None
    assert sensor.latest_values.get("other", "pressure_hpa") is None


class SlowRead(Sensor):
    """A 0.2 s read, with ``extra_time_s`` added to the read timeout."""

    sensor_type = "slow"

    def __init__(self, extra_time_s: float) -> None:
        super().__init__("x", 60)
        self.extra_time_s = extra_time_s

    def read_timeout_s(self, default_s: float) -> float:
        return default_s + self.extra_time_s

    def read(self) -> Sample:
        time.sleep(0.2)
        return Sample({"v": 1})


def test_a_slow_read_times_out() -> None:
    async def scenario() -> None:
        await poll_sensor(
            SlowRead(0), asyncio.Queue(), read_timeout_s=0.1, heartbeat=Heartbeat("x", None)
        )

    with pytest.raises(TimeoutError):
        asyncio.run(scenario())


def test_a_sensor_can_extend_its_read_timeout() -> None:
    async def scenario() -> Reading:
        queue: asyncio.Queue[Reading] = asyncio.Queue()
        task = asyncio.create_task(
            poll_sensor(SlowRead(1), queue, read_timeout_s=0.1, heartbeat=Heartbeat("x", None))
        )
        reading = await asyncio.wait_for(queue.get(), 2)
        task.cancel()
        return reading

    assert asyncio.run(scenario()).values == {"v": 1}


def test_latest_values_expire() -> None:
    latest = LatestValues()
    latest.record("barometer", {"pressure_hpa": 1006.5}, valid_for_s=-1)
    assert latest.get("barometer", "pressure_hpa") is None
