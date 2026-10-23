"""Recorder task: moves readings from the in-memory queue into the durable outbox."""

from __future__ import annotations

import asyncio
import logging

from measure_station.models import Reading
from measure_station.storage import Outbox
from measure_station.supervisor import Heartbeat

log = logging.getLogger(__name__)


async def record_readings(
    queue: asyncio.Queue[Reading],
    outbox: Outbox,
    new_data: asyncio.Event,
    *,
    flush_interval_s: float,
    batch_size: int,
    idle_timeout_s: float,
    prune_interval_s: float,
    heartbeat: Heartbeat,
) -> None:
    """Group readings into one SQLite transaction per flush, then signal the publisher.

    A flush happens ``flush_interval_s`` after the first reading of a group arrives,
    or sooner once ``batch_size`` readings are pending. Every transaction rewrites whole
    4 KiB pages, so writing many readings at once cuts SD-card wear dramatically; the
    cost is that a power cut (or hard kill) loses readings not yet flushed. A clean
    shutdown flushes everything pending.
    """
    loop = asyncio.get_running_loop()
    pending: list[Reading] = []
    flush_due: float | None = None
    next_prune = loop.time()
    try:
        while True:
            heartbeat.beat()
            wait_s = idle_timeout_s
            if flush_due is not None:
                wait_s = min(wait_s, max(0.0, flush_due - loop.time()))
            try:
                async with asyncio.timeout(wait_s):
                    reading = await queue.get()
            except TimeoutError:
                pass
            else:
                pending.append(reading)
                if flush_due is None:
                    flush_due = loop.time() + flush_interval_s
                while len(pending) < batch_size:
                    try:
                        pending.append(queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

            if pending and (len(pending) >= batch_size or loop.time() >= flush_due):
                group = list(pending)
                # Clear before writing: if we're cancelled mid-write, the worker thread
                # still commits these, so the shutdown flush must not write them again.
                pending.clear()
                flush_due = None
                try:
                    await asyncio.to_thread(outbox.add_many, group)
                except Exception:
                    # Not written: keep them for the flush below, which tries once more
                    # (e.g. into the fresh outbox that replaced a corrupt one).
                    pending.extend(group)
                    raise
                new_data.set()

            if loop.time() >= next_prune:
                dropped = await asyncio.to_thread(outbox.prune)
                if dropped:
                    log.warning("Outbox at capacity: discarded %d oldest message(s)", dropped)
                next_prune = loop.time() + prune_interval_s
    finally:
        _flush_on_exit(pending, queue, outbox)


def _flush_on_exit(
    pending: list[Reading],
    queue: asyncio.Queue[Reading],
    outbox: Outbox,
) -> None:
    """On shutdown or a failed write, persist the unflushed group and anything queued."""
    while True:
        try:
            pending.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    if not pending:
        return
    try:
        outbox.add_many(pending)
        log.info("Flushed %d pending reading(s) to the outbox", len(pending))
    except Exception:
        log.exception("Failed to flush %d pending reading(s) on shutdown", len(pending))
