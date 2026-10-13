"""Measure Station system configuration.

Every tunable setting for the station lives in this file; the rest of the package
reads from here and never hard-codes values. After editing, restart the service::

    sudo systemctl restart measure-station

Secrets (e.g. the MQTT password) are read from environment variables so they stay
out of version control. On the Pi, set them in ``/etc/measure-station/env``
(created by ``deploy/install.sh``); any ``MEASURE_STATION_*`` variable used below
can be overridden there.

Note: always import this as ``from measure_station import sysconfig``. Never put
this file's directory on ``sys.path`` directly, or it will shadow the standard
library's ``sysconfig`` module.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Any


def _env(name: str, default: str | None = None) -> str | None:
    """Read an optional ``MEASURE_STATION_<name>`` environment override."""
    return os.environ.get(f"MEASURE_STATION_{name}", default)


# --------------------------------------------------------------------------- #
# Station identity
# --------------------------------------------------------------------------- #
# Used in MQTT topics and payloads. Must not contain '/', '+' or '#'.
STATION_ID: str = _env("STATION_ID") or socket.gethostname()

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
LOG_LEVEL: str = "INFO"
# Logs go to stdout, which systemd captures in the journal (journalctl -u measure-station).
# Set a path to additionally write a size-rotated log file.
LOG_FILE: Path | None = None
LOG_FILE_MAX_BYTES: int = 1_000_000
LOG_FILE_BACKUP_COUNT: int = 3

# --------------------------------------------------------------------------- #
# MQTT broker
# --------------------------------------------------------------------------- #
MQTT_HOST: str = _env("MQTT_HOST") or "mqtt.example.com"
MQTT_PORT: int = int(_env("MQTT_PORT") or 1883)
MQTT_USERNAME: str | None = _env("MQTT_USERNAME")
MQTT_PASSWORD: str | None = _env("MQTT_PASSWORD")
MQTT_CLIENT_ID: str = f"measure-station-{STATION_ID}"
MQTT_KEEPALIVE_S: int = 30

# QoS 1 (at-least-once) is strongly recommended: a message is only removed from the
# local outbox once the broker has acknowledged it. Consumers should de-duplicate on
# (station, source, ts).
MQTT_QOS: int = 1

MQTT_TLS_ENABLED: bool = False
MQTT_TLS_CA_CERTS: str | None = None  # e.g. "/etc/measure-station/ca.crt"
MQTT_TLS_CERTFILE: str | None = None  # client certificate, if the broker requires one
MQTT_TLS_KEYFILE: str | None = None

# Topics are <MQTT_BASE_TOPIC>/<STATION_ID>/<source>; station status (retained
# "online"/"offline", with a last-will) is published to <MQTT_BASE_TOPIC>/<STATION_ID>/status.
MQTT_BASE_TOPIC: str = "measure-station"

MQTT_PUBLISH_TIMEOUT_S: float = 10.0  # waiting for PUBACK; on timeout we reconnect
MQTT_PUBLISH_BATCH_SIZE: int = 50  # messages taken from the outbox and sent concurrently
MQTT_IDLE_POLL_S: float = 5.0  # how often an idle publisher re-checks the outbox
MQTT_RECONNECT_MIN_S: float = 1.0  # reconnect backoff (exponential, with jitter)
MQTT_RECONNECT_MAX_S: float = 60.0

# --------------------------------------------------------------------------- #
# Local store-and-forward buffer (SQLite outbox)
# --------------------------------------------------------------------------- #
# systemd's StateDirectory= sets $STATE_DIRECTORY (/var/lib/measure-station). When
# running by hand during development, data goes to ./data instead.
DATA_DIR: Path = Path(os.environ.get("STATE_DIRECTORY") or Path.cwd() / "data")
OUTBOX_FILENAME: str = "outbox.sqlite3"

# Upper bound on buffered messages during a long outage. When exceeded, the OLDEST
# messages are discarded. ~300 bytes/row => 500k rows is roughly 150 MB on disk.
OUTBOX_MAX_ROWS: int = 500_000
OUTBOX_PRUNE_INTERVAL_S: float = 300.0

# "NORMAL" (with WAL) never corrupts the database but may lose the last few seconds
# of writes on sudden power loss. "FULL" fsyncs every commit: safer, more SD-card wear.
OUTBOX_SQLITE_SYNCHRONOUS: str = "NORMAL"

# In-memory hand-off between producers and the outbox writer. If it fills (e.g. the
# SD card stalls), producers wait rather than silently dropping readings.
READING_QUEUE_MAXSIZE: int = 1000

# SD-card wear vs. power-cut exposure. Readings are held in memory and written to the
# outbox together, at most this many seconds after the first one arrives. SQLite
# rewrites whole 4 KiB pages per transaction, so one write per ~200-byte reading is
# ~8 KiB of flash wear; grouping 10+ readings brings that to <1 KiB each.
# Trade-off: a power cut, hard crash or watchdog kill loses up to this many seconds
# of readings (a clean stop/reboot flushes them), and MQTT delivery is delayed by up
# to the same amount. 0 = write every reading immediately (maximum wear).
RECORDER_FLUSH_INTERVAL_S: float = 10.0
# Flush early once this many readings are pending, whatever the interval.
RECORDER_BATCH_SIZE: int = 200

# --------------------------------------------------------------------------- #
# Concurrency and self-recovery
# --------------------------------------------------------------------------- #
# Worker threads for blocking I/O (sensor reads, serial reads, SQLite). Each enabled
# UART link permanently occupies one worker, so keep this >= len(UART_LINKS) + 4.
THREAD_POOL_WORKERS: int = 8

# A crashed task (sensor, UART link, publisher...) is restarted with exponential backoff.
TASK_RESTART_MIN_S: float = 1.0
TASK_RESTART_MAX_S: float = 60.0
# If a task ran at least this long before failing, its backoff resets to the minimum.
TASK_STABLE_AFTER_S: float = 300.0

# Every task reports a heartbeat. If any task is silent for longer than this it is
# considered hung: the systemd watchdog is no longer pinged and systemd restarts the
# whole service. Must exceed TASK_RESTART_MAX_S and MQTT_RECONNECT_MAX_S. Sensors with
# long intervals automatically get a proportionally longer allowance.
TASK_STALE_AFTER_S: float = 180.0

# Ping interval when systemd doesn't supply WATCHDOG_USEC (i.e. outside systemd).
WATCHDOG_FALLBACK_INTERVAL_S: float = 30.0

# --------------------------------------------------------------------------- #
# Station health reporting
# --------------------------------------------------------------------------- #
# Periodic self-report (uptime, outbox backlog, load, memory, disk) published as
# an ordinary reading under this source name.
HEALTH_SOURCE_NAME: str = "health"
HEALTH_INTERVAL_S: float = 60.0

# --------------------------------------------------------------------------- #
# Sensors
# --------------------------------------------------------------------------- #
# Upper bound on a single sensor open()/read(). A read that exceeds it is treated as
# a failure and the sensor is re-opened.
SENSOR_READ_TIMEOUT_S: float = 10.0

# Each entry: "type" selects the driver (see measure_station.sensors.SENSOR_TYPES),
# "name" is the unique MQTT source name, "interval_s" the polling period. Any other
# keys are passed to the driver's constructor.
SENSORS: list[dict[str, Any]] = [
    {
        "type": "cpu_temperature",
        "name": "cpu",
        "interval_s": 60,
        "enabled": True,
    },
    {
        "type": "bme280",
        "name": "ambient",
        "interval_s": 30,
        "i2c_bus": 1,
        "address": 0x76,
        "enabled": False,
    },
    {
        # Generates fake data. Useful for exercising the pipeline on a dev machine.
        "type": "simulated",
        "name": "simulated",
        "interval_s": 5,
        "channels": ["temperature_c", "humidity_pct"],
        "enabled": False,
    },
]

# --------------------------------------------------------------------------- #
# UART links to external microcontrollers
# --------------------------------------------------------------------------- #
# Each link reads newline-terminated messages. Protocols:
#   "json": one JSON object per line, e.g.  {"temp_c": 21.4, "rh_pct": 40.1}
#   "kv":   comma-separated key=value pairs, e.g.  temp_c=21.4,rh_pct=40.1
# Prefer stable device paths: /dev/serial0 for the GPIO UART, or
# /dev/serial/by-id/... for USB adapters (survives re-plugging and re-enumeration).
UART_LINKS: list[dict[str, Any]] = [
    {
        "name": "mcu1",
        "port": "/dev/serial0",
        "baudrate": 115200,
        "protocol": "json",
        "enabled": False,
    },
]

# Malformed UART lines are counted and summarised at most once per interval, so a
# baud-rate mismatch can't flood the journal and wear out the SD card.
UART_MALFORMED_LOG_INTERVAL_S: float = 60.0
