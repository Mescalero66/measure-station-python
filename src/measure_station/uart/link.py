"""UART reader task for one external microcontroller link."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import serial

from measure_station.models import Reading
from measure_station.supervisor import Heartbeat
from measure_station.uart.parsers import PARSERS, ParseError

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UartConfig:
    """One ``sysconfig.UART_LINKS`` entry."""

    name: str
    port: str
    baudrate: int = 115200
    protocol: str = "json"
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1
    read_timeout_s: float = 1.0
    max_line_bytes: int = 1024
    encoding: str = "utf-8"

    def __post_init__(self) -> None:
        if self.protocol not in PARSERS:
            raise ValueError(
                f"UART link {self.name!r}: unknown protocol {self.protocol!r}; "
                f"expected one of {sorted(PARSERS)}"
            )


class LineBuffer:
    """Reassembles newline-terminated lines from arbitrary byte chunks.

    Lines longer than ``max_line_bytes`` (e.g. noise or a baud-rate mismatch with no
    newlines) are discarded up to the next newline rather than growing without bound.
    """

    def __init__(self, max_line_bytes: int) -> None:
        self._max = max_line_bytes
        self._buf = bytearray()
        self._skipping = False
        self.overflows = 0

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        lines: list[bytes] = []
        while (newline := self._buf.find(b"\n")) != -1:
            line = bytes(self._buf[:newline]).rstrip(b"\r")
            del self._buf[: newline + 1]
            if self._skipping:
                self._skipping = False
            elif len(line) > self._max:
                self.overflows += 1
            elif line.strip():
                lines.append(line)
        if len(self._buf) > self._max:
            self._buf.clear()
            if not self._skipping:
                self.overflows += 1
            self._skipping = True
        return lines


async def read_uart(
    config: UartConfig,
    queue: asyncio.Queue[Reading],
    *,
    malformed_log_interval_s: float,
    heartbeat: Heartbeat,
) -> None:
    """Read and parse lines from a serial port until cancelled or the port fails.

    If the device disappears (e.g. a USB adapter is unplugged) the read raises,
    and the supervisor re-opens the port with backoff until it returns.
    """
    loop = asyncio.get_running_loop()
    parse = PARSERS[config.protocol]
    port = await asyncio.to_thread(_open_port, config)
    log.info("UART link %r open on %s @ %d baud", config.name, config.port, config.baudrate)

    buffer = LineBuffer(config.max_line_bytes)
    malformed = 0
    latest_problem = ""
    last_report = loop.time()
    try:
        while True:
            heartbeat.beat()
            # Returns at a newline, after max_line_bytes, or on read timeout (possibly
            # with a partial line, which LineBuffer holds until the rest arrives).
            chunk = await asyncio.to_thread(port.read_until, b"\n", config.max_line_bytes)
            for raw in buffer.feed(chunk):
                try:
                    values = parse(raw.decode(config.encoding))
                except (UnicodeDecodeError, ParseError) as exc:
                    malformed += 1
                    latest_problem = f"{raw[:80]!r}: {exc}"
                    continue
                await queue.put(Reading(source=config.name, values=values))

            if buffer.overflows:
                malformed += buffer.overflows
                latest_problem = f"line exceeded {config.max_line_bytes} bytes"
                buffer.overflows = 0
            if malformed and loop.time() - last_report >= malformed_log_interval_s:
                log.warning(
                    "UART link %r: discarded %d malformed line(s) in the last %.0f s (latest %s)",
                    config.name,
                    malformed,
                    loop.time() - last_report,
                    latest_problem,
                )
                malformed = 0
                last_report = loop.time()
    finally:
        port.close()


def _open_port(config: UartConfig) -> serial.Serial:
    port = serial.Serial(
        port=config.port,
        baudrate=config.baudrate,
        bytesize=config.bytesize,
        parity=config.parity,
        stopbits=config.stopbits,
        timeout=config.read_timeout_s,
        exclusive=True,  # refuse to share the port with another process (POSIX)
    )
    port.reset_input_buffer()  # drop any stale partial line from before we opened
    return port
