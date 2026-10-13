"""In-process task supervision: restart crashed tasks, detect hung ones.

Each supervised task is an async callable accepting a ``heartbeat`` keyword argument.
The task calls ``heartbeat.beat()`` on every loop iteration. If it raises, it is
restarted with exponential backoff. If it stops beating (e.g. stuck in a hardware
call), :meth:`Supervisor.stale_tasks` reports it, and the watchdog escalates to a
full service restart via systemd.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from measure_station.backoff import ExponentialBackoff

log = logging.getLogger(__name__)


class Heartbeat:
    """Liveness marker updated by a running task."""

    __slots__ = ("_last", "name", "stale_after_s")

    def __init__(self, name: str, stale_after_s: float | None) -> None:
        self.name = name
        self.stale_after_s = stale_after_s
        self._last = time.monotonic()

    def beat(self) -> None:
        self._last = time.monotonic()

    @property
    def age_s(self) -> float:
        return time.monotonic() - self._last

    @property
    def is_stale(self) -> bool:
        return self.stale_after_s is not None and self.age_s > self.stale_after_s


TaskFactory = Callable[..., Awaitable[None]]
"""An async callable invoked as ``factory(heartbeat=hb)``."""


@dataclass
class _SupervisedTask:
    name: str
    factory: TaskFactory
    heartbeat: Heartbeat
    restarts: int = 0


class Supervisor:
    def __init__(
        self,
        *,
        restart_min_s: float,
        restart_max_s: float,
        stable_after_s: float,
    ) -> None:
        self._restart_min_s = restart_min_s
        self._restart_max_s = restart_max_s
        self._stable_after_s = stable_after_s
        self._tasks: dict[str, _SupervisedTask] = {}

    def add(self, name: str, factory: TaskFactory, *, stale_after_s: float | None) -> None:
        """Register a task. ``stale_after_s=None`` disables hang detection for it."""
        if name in self._tasks:
            raise ValueError(f"duplicate task name {name!r}")
        self._tasks[name] = _SupervisedTask(name, factory, Heartbeat(name, stale_after_s))

    @property
    def task_names(self) -> list[str]:
        return list(self._tasks)

    def stale_tasks(self) -> list[str]:
        return [t.name for t in self._tasks.values() if t.heartbeat.is_stale]

    def total_restarts(self) -> int:
        return sum(t.restarts for t in self._tasks.values())

    async def run(self) -> None:
        """Run all registered tasks until cancelled."""
        async with asyncio.TaskGroup() as group:
            for task in self._tasks.values():
                group.create_task(self._supervise(task), name=task.name)

    async def _supervise(self, task: _SupervisedTask) -> None:
        loop = asyncio.get_running_loop()
        backoff = ExponentialBackoff(self._restart_min_s, self._restart_max_s)
        while True:
            task.heartbeat.beat()
            started = loop.time()
            try:
                await task.factory(heartbeat=task.heartbeat)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Task %r crashed", task.name)
            else:
                log.warning("Task %r exited unexpectedly", task.name)

            task.restarts += 1
            if loop.time() - started >= self._stable_after_s:
                backoff.reset()
            delay = backoff.next_delay()
            log.info("Restarting task %r in %.1f s (restart #%d)", task.name, delay, task.restarts)
            # Waiting to restart is not "hung": keep the heartbeat fresh.
            task.heartbeat.beat()
            await asyncio.sleep(delay)
