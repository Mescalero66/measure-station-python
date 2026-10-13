"""systemd integration: readiness notification and software watchdog.

The service runs with ``Type=notify`` and ``WatchdogSec=``. We ping systemd only
while every supervised task is alive; if a task hangs (or the event loop itself
blocks), pings stop and systemd kills and restarts the service. Below that, the
BCM2835 hardware watchdog reboots the Pi if systemd itself stops responding.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket

from measure_station.supervisor import Supervisor

log = logging.getLogger(__name__)


class SystemdNotifier:
    """Minimal ``sd_notify(3)`` implementation (avoids a libsystemd dependency)."""

    def __init__(self, address: str | None = None) -> None:
        address = address if address is not None else os.environ.get("NOTIFY_SOCKET")
        if address and address.startswith("@"):  # abstract namespace socket
            address = "\0" + address[1:]
        self._address = address or None

    @property
    def enabled(self) -> bool:
        return self._address is not None

    def notify(self, message: str) -> bool:
        if self._address is None:
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.connect(self._address)
                sock.sendall(message.encode())
        except OSError as exc:
            log.warning("sd_notify(%r) failed: %s", message, exc)
            return False
        return True


def watchdog_interval_s(fallback_s: float) -> float:
    """Ping at half the systemd watchdog timeout, as recommended by sd_watchdog_enabled(3)."""
    usec = os.environ.get("WATCHDOG_USEC", "")
    if usec.isdigit() and int(usec) > 0:
        return int(usec) / 1_000_000 / 2
    return fallback_s


async def run_watchdog(
    notifier: SystemdNotifier, supervisor: Supervisor, interval_s: float
) -> None:
    if not notifier.enabled:
        log.info("Not running under systemd; watchdog pings disabled (hang detection still logs)")
    while True:
        stale = supervisor.stale_tasks()
        if stale:
            log.error("Hung task(s) detected, withholding watchdog ping: %s", ", ".join(stale))
        else:
            notifier.notify("WATCHDOG=1")
        await asyncio.sleep(interval_s)
