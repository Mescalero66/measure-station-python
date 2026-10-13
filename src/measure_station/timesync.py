"""Clock-sync monitor task: detects NTP sync and back-dates readings taken before it."""

from __future__ import annotations

import asyncio
import logging

from measure_station.clock import ClockState
from measure_station.storage import Outbox
from measure_station.supervisor import Heartbeat

log = logging.getLogger(__name__)


async def monitor_clock_sync(
    clock: ClockState,
    outbox: Outbox,
    new_data: asyncio.Event,
    *,
    poll_interval_s: float,
    heartbeat: Heartbeat,
) -> None:
    """Poll for clock synchronisation; once synced, correct held readings and release them.

    The correction runs once per task start, so it also covers readings left pending by
    an earlier run of the service within the same boot.
    """
    corrected = False
    while True:
        heartbeat.beat()
        if not clock.synced:
            await asyncio.to_thread(clock.poll)
        if clock.synced and not corrected:
            count = await asyncio.to_thread(outbox.apply_time_correction)
            corrected = True
            if count:
                log.info("Corrected timestamps of %d reading(s) captured before time sync", count)
                new_data.set()  # wake the publisher: these are now publishable
        await asyncio.sleep(poll_interval_s)
