"""Run the whole station, reading every sensor every 10 s, and send each reading straight
to a test broker, for checking that the readings are plausible. See
docs/testing-the-station.md.

    python -m measure_station.live_test 192.168.1.50         # the test PC's broker
    python -m measure_station.live_test 192.168.1.50 1884    # ...on another port

Everything runs as in the service, with the same drivers, settings and topics, except:

- every sensor enabled in SENSORS is read every 10 s, whatever its interval_s, and all
  are sent, including those with "publish": False (the DS18B20 probe);
- nothing is stored: each reading is printed, then sent once to the broker given (QoS
  1). While the broker can't be reached, readings are only printed;
- the health report is sent every 10 s too, with outbox_backlog and awaiting_time_sync
  always 0, since nothing is stored;
- no clock-sync corrections or systemd watchdog.

The MQ gas board's readings are sent as they arrive (each MQ sensor every ~150 s), and
fan control runs exactly as in the service. Press Ctrl+C to stop: the fans are then left
as the stopped service leaves them (exhaust at 100 %, intake off).

Stop the service first, since it uses the same buses and fan outputs:

    sudo systemctl stop measure-station
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType

import aiomqtt

from measure_station import sysconfig
from measure_station.backoff import ExponentialBackoff
from measure_station.clock import ClockState
from measure_station.fan import FanState, control_fan
from measure_station.health import report_health
from measure_station.logging_setup import configure_logging
from measure_station.main import (
    EXIT_CONFIG_ERROR,
    fan_outputs,
    fan_settings,
    mqboard_decoder,
    validate_config,
)
from measure_station.models import MessageEncoder, Reading, StoredReading, encode_values
from measure_station.sensors import LatestValues, create_sensor, poll_sensor
from measure_station.supervisor import Heartbeat, Supervisor
from measure_station.uart import read_mqboard

log = logging.getLogger("measure_station")

INTERVAL_S = 10.0
DEFAULT_PORT = 1883
PUBLISH_TIMEOUT_S = 5.0


def main(argv: list[str]) -> int:
    if not 1 <= len(argv) <= 2 or (len(argv) == 2 and not argv[1].isdigit()):
        print("Usage: python -m measure_station.live_test <broker host> [port]")
        return 2
    host, port = argv[0], int(argv[1]) if len(argv) == 2 else DEFAULT_PORT

    cfg = sysconfig
    configure_logging(cfg.LOG_LEVEL)
    errors = validate_config(cfg)
    if errors:
        for error in errors:
            log.critical("Configuration error: %s", error)
        return EXIT_CONFIG_ERROR

    # aiomqtt (via paho) needs a selector event loop; Windows defaults to proactor.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    # Ctrl+C cancels run(), so every task's cleanup runs: sensors close, fans to failsafe.
    with contextlib.suppress(KeyboardInterrupt), asyncio.Runner(loop_factory=loop_factory) as r:
        r.run(run(cfg, host, port))
    return 0


async def run(cfg: ModuleType, host: str, port: int) -> None:
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=cfg.THREAD_POOL_WORKERS, thread_name_prefix="io")
    )
    queue: asyncio.Queue[Reading] = asyncio.Queue(maxsize=cfg.READING_QUEUE_MAXSIZE)
    clock = ClockState()
    await asyncio.to_thread(clock.poll)
    # Crashed tasks restart with backoff, as in the service. No watchdog, so no stale limit.
    supervisor = Supervisor(
        restart_min_s=cfg.TASK_RESTART_MIN_S,
        restart_max_s=cfg.TASK_RESTART_MAX_S,
        stable_after_s=cfg.TASK_STABLE_AFTER_S,
    )
    supervisor.add(
        "broadcast",
        functools.partial(
            broadcast,
            queue,
            clock,
            MessageEncoder(cfg.MQTT_BASE_TOPIC, cfg.STATION_ID),
            host=host,
            port=port,
            # Its own ID, so it can't knock the station off a broker they share.
            client_id=f"{cfg.MQTT_CLIENT_ID}-test",
            reconnect_max_s=cfg.MQTT_RECONNECT_MAX_S,
        ),
        stale_after_s=None,
    )
    if not clock.synced:
        log.warning("System clock not yet synchronised; readings are sent as 'estimated'")
        supervisor.add(
            "clock",
            functools.partial(watch_clock, clock, poll_interval_s=cfg.TIME_SYNC_POLL_S),
            stale_after_s=None,
        )

    latest = LatestValues()  # as in the service: the SCD41 and fan control read it
    for entry in cfg.SENSORS:
        if not entry.get("enabled", True):
            continue
        sensor = create_sensor({**entry, "interval_s": INTERVAL_S, "publish": True})
        sensor.latest_values = latest
        supervisor.add(
            f"sensor:{sensor.name}",
            functools.partial(poll_sensor, sensor, queue, read_timeout_s=cfg.SENSOR_READ_TIMEOUT_S),
            stale_after_s=None,
        )
    fan_state = None
    if cfg.FAN_ENABLED:
        fan_state = FanState()
        exhaust, intake = fan_outputs(cfg)
        supervisor.add(
            "fan",
            functools.partial(
                control_fan, exhaust, intake, latest, fan_state, settings=fan_settings(cfg)
            ),
            stale_after_s=None,
        )
    mqboard = None
    if cfg.MQBOARD_ENABLED:
        mqboard = mqboard_decoder(cfg, {})
        supervisor.add(
            "mqboard",
            functools.partial(
                read_mqboard,
                mqboard,
                queue,
                port=cfg.MQBOARD_PORT,
                baudrate=cfg.MQBOARD_BAUDRATE,
                malformed_log_interval_s=cfg.MQBOARD_CORRUPT_LOG_INTERVAL_S,
            ),
            stale_after_s=None,
        )
    supervisor.add(
        "health",
        functools.partial(
            report_health,
            queue,
            NoOutbox(),
            supervisor,
            clock,
            mqboard,
            fan_state,
            source=cfg.HEALTH_SOURCE_NAME,
            interval_s=INTERVAL_S,  # like the sensors, so the fan state is seen every 10 s
            # disk_free_mb: the service's data directory, or, when that isn't there (it's
            # created by systemd), the current one, which is on the same SD card.
            data_dir=cfg.DATA_DIR if cfg.DATA_DIR.is_dir() else Path.cwd(),
        ),
        stale_after_s=None,
    )

    log.info(
        "Live test: sending to %s:%d, sensors every %g s. Press Ctrl+C to stop. Tasks: %s",
        host,
        port,
        INTERVAL_S,
        ", ".join(supervisor.task_names),
    )
    await supervisor.run()


async def broadcast(
    queue: asyncio.Queue[Reading],
    clock: ClockState,
    encoder: MessageEncoder,
    *,
    host: str,
    port: int,
    client_id: str,
    reconnect_max_s: float,
    heartbeat: Heartbeat,
) -> None:
    """Print each reading, and send it while connected; between attempts, only print."""
    backoff = ExponentialBackoff(1.0, reconnect_max_s)
    while True:
        heartbeat.beat()
        try:
            async with aiomqtt.Client(
                hostname=host, port=port, identifier=client_id, timeout=PUBLISH_TIMEOUT_S
            ) as client:
                log.info("Connected to MQTT broker %s:%d", host, port)
                backoff.reset()
                while True:
                    topic, payload = await _next_message(queue, clock, encoder)
                    async with asyncio.timeout(PUBLISH_TIMEOUT_S):
                        await client.publish(topic, payload, qos=1)
        except (aiomqtt.MqttError, TimeoutError, OSError) as exc:
            log.warning("MQTT link down: %s", exc or type(exc).__name__)

        delay = backoff.next_delay()
        log.info("Reconnecting to MQTT broker in %.1f s; until then, readings aren't sent", delay)
        # Keep taking readings meanwhile, so the sensors never wait on a full queue.
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(delay):
                while True:
                    await _next_message(queue, clock, encoder)


async def _next_message(
    queue: asyncio.Queue[Reading], clock: ClockState, encoder: MessageEncoder
) -> tuple[str, str]:
    """Take the next reading, print it, and return its (topic, payload)."""
    r = await queue.get()
    line = "  ".join(f"{k}={v}" for k, v in r.values.items())
    if r.meta:
        line += "  | meta: " + "  ".join(f"{k}={v}" for k, v in r.meta.items())
    print(time.strftime("%H:%M:%S"), f"{r.source:<18}", line, flush=True)
    # As the outbox would store it: "ntp", or "estimated" before the clock has synced.
    ts, quality = clock.resolve(r.timestamp, r.boot_ts)
    values, meta = encode_values(r.values), encode_values(r.meta)
    stored = StoredReading(
        0, r.source, r.sensor_type, values, meta, ts, quality, clock.boot_id, r.boot_ts
    )
    return encoder.encode(stored)


class NoOutbox:
    """Stands in for the outbox in the health report. Nothing is stored or held for a
    time correction, so its outbox_backlog and awaiting_time_sync are truly 0."""

    def count(self) -> int:
        return 0

    def count_awaiting_time(self) -> int:
        return 0


async def watch_clock(clock: ClockState, *, poll_interval_s: float, heartbeat: Heartbeat) -> None:
    """Notice the clock synchronising, so readings from then on are sent as "ntp"."""
    while not clock.synced:
        heartbeat.beat()
        await asyncio.sleep(poll_interval_s)
        await asyncio.to_thread(clock.poll)
    await asyncio.Event().wait()  # done; a supervised task mustn't return


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
