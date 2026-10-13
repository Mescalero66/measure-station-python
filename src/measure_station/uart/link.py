"""Serial reader task for the MQ gas board on the Pi's GPIO UART."""

from __future__ import annotations

import asyncio
import logging
import time

import serial

from measure_station.clock import boot_clock
from measure_station.models import Reading
from measure_station.supervisor import Heartbeat
from measure_station.uart.mq_board import MqBoardDecoder, ParseError, Received

log = logging.getLogger(__name__)

# Fixed by the board's interface spec: 8N1, lines of at most 128 bytes.
MAX_LINE_BYTES = 128
READ_TIMEOUT_S = 1.0  # also how often the decoder's housekeeping runs when the line is idle


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


async def read_mqboard(
    decoder: MqBoardDecoder,
    queue: asyncio.Queue[Reading],
    *,
    port: str,
    baudrate: int,
    malformed_log_interval_s: float,
    heartbeat: Heartbeat,
) -> None:
    """Read and decode the board's sentences until cancelled or the port fails.

    If the port fails, the read raises and the supervisor re-opens it with backoff.
    ``decoder`` outlives this task, so its state survives the re-open.
    """
    loop = asyncio.get_running_loop()
    serial_port = await asyncio.to_thread(_open_port, port, baudrate)
    log.info("MQ board: reading %s at %d baud", port, baudrate)

    buffer = LineBuffer(MAX_LINE_BYTES)
    malformed = 0
    latest_problem = ""
    last_report = loop.time()
    try:
        while True:
            heartbeat.beat()
            # Returns at a newline, after MAX_LINE_BYTES, or on timeout (possibly with a
            # partial line, which LineBuffer holds until the rest arrives).
            chunk = await asyncio.to_thread(serial_port.read_until, b"\n", MAX_LINE_BYTES)
            received = Received(boot_ts=boot_clock(), wall_ts=time.time())
            for raw in buffer.feed(chunk):
                try:
                    readings = decoder.feed(raw, received)
                except ParseError as exc:
                    decoder.bad_lines += 1
                    malformed += 1
                    latest_problem = f"{raw[:80]!r}: {exc}"
                    continue
                for reading in readings:
                    await queue.put(reading)
            # Runs at least once per read timeout, even when the line is silent.
            for reading in decoder.check(received.boot_ts):
                await queue.put(reading)

            if buffer.overflows:
                decoder.bad_lines += buffer.overflows
                malformed += buffer.overflows
                latest_problem = f"line exceeded {MAX_LINE_BYTES} bytes"
                buffer.overflows = 0
            if malformed and loop.time() - last_report >= malformed_log_interval_s:
                log.warning(
                    "MQ board: discarded %d corrupt line(s) in the last %.0f s (latest %s)",
                    malformed,
                    loop.time() - last_report,
                    latest_problem,
                )
                malformed = 0
                last_report = loop.time()
    finally:
        serial_port.close()


def _open_port(port: str, baudrate: int) -> serial.SerialBase:
    # serial_for_url() takes a device path as-is, and also socket://host:port, which
    # lets the station be tested against a simulated board without hardware.
    serial_port = serial.serial_for_url(
        port,
        baudrate=baudrate,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=READ_TIMEOUT_S,
        exclusive=True,  # refuse to share the port with another process (POSIX)
    )
    serial_port.reset_input_buffer()  # drop any stale partial line from before we opened
    return serial_port
