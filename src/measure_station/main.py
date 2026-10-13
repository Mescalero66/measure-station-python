"""Entry point: validate configuration, wire up components and run until stopped.

This module contains no tunable values; everything comes from ``sysconfig``.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import re
import signal
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from types import ModuleType
from typing import Any

from measure_station import __version__, sysconfig
from measure_station.clock import ClockState
from measure_station.health import report_health
from measure_station.logging_setup import configure_logging
from measure_station.models import MessageEncoder, Reading
from measure_station.ntp_setup import invalid_servers
from measure_station.publisher import MqttPublisher, MqttSettings
from measure_station.recorder import record_readings
from measure_station.sensors import create_sensor, poll_sensor
from measure_station.storage import Outbox
from measure_station.supervisor import Supervisor
from measure_station.timesync import monitor_clock_sync
from measure_station.uart import MqBoardDecoder, read_mqboard
from measure_station.watchdog import SystemdNotifier, run_watchdog, watchdog_interval_s

log = logging.getLogger("measure_station")

# Matches EX_CONFIG; the systemd unit's RestartPreventExitStatus stops a restart
# loop on a bad config (it can't fix itself) while still restarting on crashes.
EXIT_CONFIG_ERROR = 78

_TOPIC_SAFE = re.compile(r"^[A-Za-z0-9_.-]+$")
_RESERVED_SOURCES = frozenset({"status"})


def main() -> int:
    cfg = sysconfig
    configure_logging(
        cfg.LOG_LEVEL,
        cfg.LOG_FILE,
        max_bytes=cfg.LOG_FILE_MAX_BYTES,
        backup_count=cfg.LOG_FILE_BACKUP_COUNT,
    )
    log.info("Measure Station %s starting (station_id=%s)", __version__, cfg.STATION_ID)

    errors = validate_config(cfg)
    if errors:
        for error in errors:
            log.critical("Configuration error: %s", error)
        return EXIT_CONFIG_ERROR

    # aiomqtt (via paho) needs a selector event loop; Windows defaults to proactor.
    # This only matters when developing on Windows.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    try:
        with asyncio.Runner(loop_factory=loop_factory) as runner:
            runner.run(run(cfg))
    except KeyboardInterrupt:
        pass
    log.info("Measure Station stopped")
    return 0


async def run(cfg: ModuleType) -> None:
    loop = asyncio.get_running_loop()
    loop.set_default_executor(
        ThreadPoolExecutor(max_workers=cfg.THREAD_POOL_WORKERS, thread_name_prefix="io")
    )
    stop = asyncio.Event()
    _install_signal_handlers(loop, stop.set)

    clock = ClockState()
    await asyncio.to_thread(clock.poll)  # already synced if this is a mid-boot restart
    if not clock.synced:
        log.warning("System clock not yet synchronised; readings will be held for correction")

    outbox = Outbox(
        cfg.DATA_DIR / cfg.OUTBOX_FILENAME,
        clock=clock,
        max_rows=cfg.OUTBOX_MAX_ROWS,
        max_time_hold_s=cfg.TIME_SYNC_MAX_HOLD_S,
        synchronous=cfg.OUTBOX_SQLITE_SYNCHRONOUS,
    )
    await asyncio.to_thread(outbox.open)

    queue: asyncio.Queue[Reading] = asyncio.Queue(maxsize=cfg.READING_QUEUE_MAXSIZE)
    new_data = asyncio.Event()
    encoder = MessageEncoder(cfg.MQTT_BASE_TOPIC, cfg.STATION_ID)
    # The decoder lives for the whole run (across serial re-opens), seeded with what the
    # outbox already recorded, so the board's repeated readings aren't recorded twice.
    mqboard = None
    if cfg.MQBOARD_ENABLED:
        mqboard = _mqboard_decoder(cfg, await asyncio.to_thread(outbox.load_mqboard_last_seen))
    supervisor = build_supervisor(cfg, clock, queue, outbox, encoder, new_data, mqboard)
    notifier = SystemdNotifier()

    try:
        async with asyncio.TaskGroup() as group:
            tasks = [
                group.create_task(supervisor.run(), name="supervisor"),
                group.create_task(
                    run_watchdog(
                        notifier,
                        supervisor,
                        watchdog_interval_s(cfg.WATCHDOG_FALLBACK_INTERVAL_S),
                    ),
                    name="watchdog",
                ),
            ]
            notifier.notify("READY=1")
            log.info("Running tasks: %s", ", ".join(supervisor.task_names))

            await stop.wait()
            log.info("Shutdown requested")
            notifier.notify("STOPPING=1")
            for task in tasks:
                task.cancel()
    finally:
        await asyncio.to_thread(outbox.close)


def build_supervisor(
    cfg: ModuleType,
    clock: ClockState,
    queue: asyncio.Queue[Reading],
    outbox: Outbox,
    encoder: MessageEncoder,
    new_data: asyncio.Event,
    mqboard: MqBoardDecoder | None,
) -> Supervisor:
    supervisor = Supervisor(
        restart_min_s=cfg.TASK_RESTART_MIN_S,
        restart_max_s=cfg.TASK_RESTART_MAX_S,
        stable_after_s=cfg.TASK_STABLE_AFTER_S,
    )
    stale_s = cfg.TASK_STALE_AFTER_S

    supervisor.add(
        "recorder",
        functools.partial(
            record_readings,
            queue,
            outbox,
            new_data,
            flush_interval_s=cfg.RECORDER_FLUSH_INTERVAL_S,
            batch_size=cfg.RECORDER_BATCH_SIZE,
            idle_timeout_s=min(5.0, stale_s / 4),
            prune_interval_s=cfg.OUTBOX_PRUNE_INTERVAL_S,
        ),
        stale_after_s=stale_s,
    )

    publisher = MqttPublisher(_mqtt_settings(cfg, encoder), outbox, encoder, new_data)
    supervisor.add("publisher", publisher.run, stale_after_s=stale_s)

    supervisor.add(
        "timesync",
        functools.partial(
            monitor_clock_sync, clock, outbox, new_data, poll_interval_s=cfg.TIME_SYNC_POLL_S
        ),
        stale_after_s=stale_s,
    )

    for sensor_cfg in _enabled(cfg.SENSORS):
        sensor = create_sensor(sensor_cfg)
        supervisor.add(
            f"sensor:{sensor.name}",
            functools.partial(poll_sensor, sensor, queue, read_timeout_s=cfg.SENSOR_READ_TIMEOUT_S),
            # A slow sensor legitimately beats only once per interval.
            stale_after_s=max(stale_s, 2 * sensor.interval_s + cfg.SENSOR_READ_TIMEOUT_S),
        )

    if mqboard is not None:
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
            stale_after_s=stale_s,
        )

    supervisor.add(
        "health",
        functools.partial(
            report_health,
            queue,
            outbox,
            supervisor,
            clock,
            mqboard,
            source=cfg.HEALTH_SOURCE_NAME,
            interval_s=cfg.HEALTH_INTERVAL_S,
            data_dir=cfg.DATA_DIR,
        ),
        stale_after_s=max(stale_s, 2 * cfg.HEALTH_INTERVAL_S),
    )
    return supervisor


def validate_config(cfg: Any) -> list[str]:
    """Return human-readable problems with the configuration (empty if valid)."""
    errors: list[str] = []

    if not _TOPIC_SAFE.match(str(cfg.STATION_ID)):
        errors.append(f"STATION_ID {cfg.STATION_ID!r} must match {_TOPIC_SAFE.pattern}")
    if any(ch in cfg.MQTT_BASE_TOPIC for ch in "+#") or not cfg.MQTT_BASE_TOPIC.strip("/"):
        errors.append("MQTT_BASE_TOPIC must be non-empty and contain no MQTT wildcards")
    if cfg.MQTT_QOS not in (0, 1, 2):
        errors.append("MQTT_QOS must be 0, 1 or 2")
    if cfg.RECORDER_FLUSH_INTERVAL_S < 0:
        errors.append("RECORDER_FLUSH_INTERVAL_S must be >= 0")
    if cfg.RECORDER_BATCH_SIZE < 1:
        errors.append("RECORDER_BATCH_SIZE must be >= 1")
    if cfg.TIME_SYNC_POLL_S <= 0:
        errors.append("TIME_SYNC_POLL_S must be > 0")
    if cfg.TIME_SYNC_MAX_HOLD_S < 0:
        errors.append("TIME_SYNC_MAX_HOLD_S must be >= 0")
    if bad_ntp := invalid_servers(cfg.NTP_SERVERS):
        errors.append(f"NTP_SERVERS contains invalid host names or addresses: {bad_ntp!r}")

    sources = [cfg.HEALTH_SOURCE_NAME]
    for entry in _enabled(cfg.SENSORS):
        try:
            sources.append(create_sensor(entry).name)
        except (TypeError, ValueError) as exc:
            errors.append(f"SENSORS entry {entry!r}: {exc}")
    if cfg.MQBOARD_ENABLED:
        sources.extend(_mqboard_decoder(cfg, {}).sources())

    seen: set[str] = set()
    for name in sources:
        if not _TOPIC_SAFE.match(name):
            errors.append(f"source name {name!r} must match {_TOPIC_SAFE.pattern}")
        if name in _RESERVED_SOURCES:
            errors.append(f"source name {name!r} is reserved")
        if name in seen:
            errors.append(f"duplicate source name {name!r}")
        seen.add(name)

    longest_wait = max(
        cfg.TASK_RESTART_MAX_S, cfg.MQTT_RECONNECT_MAX_S, cfg.MQTT_IDLE_POLL_S, cfg.TIME_SYNC_POLL_S
    )
    if longest_wait >= cfg.TASK_STALE_AFTER_S:
        errors.append(
            f"TASK_STALE_AFTER_S ({cfg.TASK_STALE_AFTER_S}) must exceed TASK_RESTART_MAX_S, "
            f"MQTT_RECONNECT_MAX_S, MQTT_IDLE_POLL_S and TIME_SYNC_POLL_S ({longest_wait})"
        )
    min_workers = 5 if cfg.MQBOARD_ENABLED else 4  # the board reader holds one permanently
    if min_workers > cfg.THREAD_POOL_WORKERS:
        errors.append(f"THREAD_POOL_WORKERS ({cfg.THREAD_POOL_WORKERS}) must be >= {min_workers}")
    return errors


def _enabled(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in entries if e.get("enabled", True)]


def _mqboard_decoder(cfg: Any, last_seen: dict[str, int]) -> MqBoardDecoder:
    return MqBoardDecoder(
        last_seen=last_seen,
        source_prefix=cfg.MQBOARD_SOURCE_PREFIX,
        max_hold_s=cfg.MQBOARD_MAX_HOLD_S,
        stale_sensor_s=cfg.MQBOARD_STALE_SENSOR_S,
        link_down_s=cfg.MQBOARD_LINK_DOWN_S,
    )


def _mqtt_settings(cfg: ModuleType, encoder: MessageEncoder) -> MqttSettings:
    return MqttSettings(
        host=cfg.MQTT_HOST,
        port=cfg.MQTT_PORT,
        client_id=cfg.MQTT_CLIENT_ID,
        status_topic=encoder.status_topic,
        username=cfg.MQTT_USERNAME,
        password=cfg.MQTT_PASSWORD,
        keepalive_s=cfg.MQTT_KEEPALIVE_S,
        qos=cfg.MQTT_QOS,
        tls_enabled=cfg.MQTT_TLS_ENABLED,
        tls_ca_certs=cfg.MQTT_TLS_CA_CERTS,
        tls_certfile=cfg.MQTT_TLS_CERTFILE,
        tls_keyfile=cfg.MQTT_TLS_KEYFILE,
        publish_timeout_s=cfg.MQTT_PUBLISH_TIMEOUT_S,
        batch_size=cfg.MQTT_PUBLISH_BATCH_SIZE,
        idle_poll_s=cfg.MQTT_IDLE_POLL_S,
        reconnect_min_s=cfg.MQTT_RECONNECT_MIN_S,
        reconnect_max_s=cfg.MQTT_RECONNECT_MAX_S,
    )


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, callback: Callable[[], None]) -> None:
    """SIGTERM (systemctl stop) and SIGINT (Ctrl+C) trigger a graceful shutdown."""
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Windows has no loop signal handlers; Ctrl+C raises KeyboardInterrupt instead.
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, callback)
