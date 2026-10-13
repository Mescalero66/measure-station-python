import asyncio

import pytest

from measure_station.backoff import ExponentialBackoff
from measure_station.supervisor import Heartbeat, Supervisor


def test_backoff_grows_caps_and_resets() -> None:
    backoff = ExponentialBackoff(1.0, 5.0, jitter=0)
    assert [backoff.next_delay() for _ in range(5)] == [1.0, 2.0, 4.0, 5.0, 5.0]
    backoff.reset()
    assert backoff.next_delay() == 1.0


def test_backoff_jitter_stays_in_bounds() -> None:
    backoff = ExponentialBackoff(10.0, 10.0, jitter=0.1)
    assert all(9.0 <= backoff.next_delay() <= 10.0 for _ in range(100))


def test_heartbeat_staleness() -> None:
    assert not Heartbeat("never", None).is_stale
    hb = Heartbeat("x", stale_after_s=-1)  # immediately stale
    assert hb.is_stale


def test_crashing_task_is_restarted() -> None:
    attempts = 0

    async def flaky(*, heartbeat: Heartbeat) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("boom")
        await asyncio.sleep(3600)

    async def scenario() -> Supervisor:
        supervisor = Supervisor(restart_min_s=0.01, restart_max_s=0.02, stable_after_s=60)
        supervisor.add("flaky", flaky, stale_after_s=None)
        task = asyncio.create_task(supervisor.run())
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return supervisor

    supervisor = asyncio.run(scenario())
    assert attempts == 3
    assert supervisor.total_restarts() == 2


def test_duplicate_task_names_rejected() -> None:
    async def noop(*, heartbeat: Heartbeat) -> None:
        pass

    supervisor = Supervisor(restart_min_s=1, restart_max_s=1, stable_after_s=1)
    supervisor.add("a", noop, stale_after_s=None)
    with pytest.raises(ValueError):
        supervisor.add("a", noop, stale_after_s=None)
