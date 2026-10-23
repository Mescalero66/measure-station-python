import asyncio
import sqlite3
from pathlib import Path

import pytest

from measure_station.clock import ClockState
from measure_station.models import Reading
from measure_station.recorder import record_readings
from measure_station.storage import Outbox
from measure_station.supervisor import Heartbeat


class CountingOutbox(Outbox):
    """Records how many write transactions the recorder issues."""

    def __init__(self, path: Path, clock: ClockState) -> None:
        super().__init__(path, clock=clock, max_rows=10_000, max_time_hold_s=0)
        self.writes = 0

    def add_many(self, messages):  # type: ignore[no-untyped-def]
        self.writes += 1
        return super().add_many(messages)


@pytest.fixture
def outbox(tmp_path: Path, synced_clock: ClockState):
    box = CountingOutbox(tmp_path / "outbox.sqlite3", synced_clock)
    box.open()
    yield box
    box.close()


def _start(outbox: Outbox, queue: asyncio.Queue, *, flush_interval_s: float, batch_size: int = 100):
    return asyncio.create_task(
        record_readings(
            queue,
            outbox,
            asyncio.Event(),
            flush_interval_s=flush_interval_s,
            batch_size=batch_size,
            idle_timeout_s=0.05,
            prune_interval_s=3600,
            heartbeat=Heartbeat("recorder", None),
        )
    )


async def _stop(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_readings_are_grouped_into_one_write_per_interval(outbox: CountingOutbox) -> None:
    async def scenario() -> None:
        queue: asyncio.Queue[Reading] = asyncio.Queue()
        task = _start(outbox, queue, flush_interval_s=0.3)
        for i in range(5):
            await queue.put(Reading(source="s", sensor_type="simulated", values={"i": i}))
            await asyncio.sleep(0.02)
        assert outbox.count() == 0  # still held in memory
        await asyncio.sleep(0.4)
        assert outbox.count() == 5
        assert outbox.writes == 1
        await _stop(task)

    asyncio.run(scenario())


def test_batch_size_forces_early_flush(outbox: CountingOutbox) -> None:
    async def scenario() -> None:
        queue: asyncio.Queue[Reading] = asyncio.Queue()
        task = _start(outbox, queue, flush_interval_s=60, batch_size=3)
        for i in range(3):
            await queue.put(Reading(source="s", sensor_type="simulated", values={"i": i}))
        await asyncio.sleep(0.1)
        assert outbox.count() == 3
        await _stop(task)

    asyncio.run(scenario())


def test_zero_interval_writes_immediately(outbox: CountingOutbox) -> None:
    async def scenario() -> None:
        queue: asyncio.Queue[Reading] = asyncio.Queue()
        task = _start(outbox, queue, flush_interval_s=0)
        await queue.put(Reading(source="s", sensor_type="simulated", values={"i": 0}))
        await asyncio.sleep(0.05)
        assert outbox.count() == 1
        await _stop(task)

    asyncio.run(scenario())


def test_clean_shutdown_flushes_pending_and_queued(outbox: CountingOutbox) -> None:
    async def scenario() -> None:
        queue: asyncio.Queue[Reading] = asyncio.Queue()
        task = _start(outbox, queue, flush_interval_s=60)
        await queue.put(Reading(source="s", sensor_type="simulated", values={"i": 0}))
        await asyncio.sleep(0.05)  # picked up into the pending group
        # Possibly still queued:
        await queue.put(Reading(source="s", sensor_type="simulated", values={"i": 1}))
        await _stop(task)

    asyncio.run(scenario())
    assert outbox.count() == 2


class FailingOnceOutbox(CountingOutbox):
    """The first write fails, as when the outbox is found corrupt: Outbox quarantines it,
    opens a fresh one and re-raises, so the caller can retry."""

    def add_many(self, messages):  # type: ignore[no-untyped-def]
        if self.writes == 0:
            self.writes += 1
            raise sqlite3.DatabaseError("database disk image is malformed")
        return super().add_many(messages)


def test_failed_write_is_retried_not_lost(tmp_path: Path, synced_clock: ClockState) -> None:
    outbox = FailingOnceOutbox(tmp_path / "outbox.sqlite3", synced_clock)
    outbox.open()

    async def scenario() -> None:
        queue: asyncio.Queue[Reading] = asyncio.Queue()
        task = _start(outbox, queue, flush_interval_s=0)
        await queue.put(Reading(source="s", sensor_type="simulated", values={"i": 0}))
        # The task still crashes (the supervisor restarts it), but only after retrying.
        with pytest.raises(sqlite3.DatabaseError):
            await task

    asyncio.run(scenario())
    assert outbox.count() == 1
    assert outbox.writes == 2
    outbox.close()
