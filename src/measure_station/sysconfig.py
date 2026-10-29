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
from pathlib import Path
from typing import Any


def _env(name: str, default: str | None = None) -> str | None:
    """Read an optional ``MEASURE_STATION_<name>`` environment override."""
    return os.environ.get(f"MEASURE_STATION_{name}", default)


def _env_list(name: str) -> list[str]:
    """Read an optional comma/space-separated ``MEASURE_STATION_<name>`` override."""
    return (_env(name) or "").replace(",", " ").split()


# --------------------------------------------------------------------------- #
# Station identity
# --------------------------------------------------------------------------- #
# Used in MQTT topics and payloads. Must not contain '/', '+' or '#'.
STATION_ID: str = _env("STATION_ID") or "mstn-2b"

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
# messages are discarded. ~16,400 readings/day => 1.5M rows is about 3 months;
# ~300 bytes/row => roughly 450 MB on disk.
OUTBOX_MAX_ROWS: int = 1_500_000
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
# Timekeeping (the Pi has no real-time clock)
# --------------------------------------------------------------------------- #
# NTP servers for the Pi's clock (systemd-timesyncd), as hostnames or IP addresses, in
# order of preference. NTP needs a reachable server, not internet access: point this
# at a time source on the local network (e.g. the router) so the clock syncs even when
# the internet is unreachable; it must not serve time before it has real time itself
# (see docs/timekeeping.md). Add a public server after it as a backup, e.g.
#     NTP_SERVERS = ["192.168.1.10", "pool.ntp.org"]
# Empty = the OS defaults (Debian's public pool, which needs internet access).
# Applied as root each time the service starts (see measure_station/ntp_setup.py).
NTP_SERVERS: list[str] = _env_list("NTP_SERVERS") or []

# Until NTP has synchronised the clock after a boot, readings are stored with their
# seconds-since-boot and held back from MQTT. On sync they are back-dated to accurate
# timestamps (published with "ts_src": "corrected") and released.
#
# How often to check for NTP synchronisation while unsynchronised.
TIME_SYNC_POLL_S: float = 10.0
# Longest a reading is held waiting for a time correction. After this it is published
# anyway, with its unsynchronised timestamp ("ts_src": "estimated"). This bounds
# the delivery delay at sites where NTP is unreachable even though the broker isn't.
# 0 = never hold (publish immediately; pre-sync readings are always "estimated").
TIME_SYNC_MAX_HOLD_S: float = 7 * 24 * 3600

# --------------------------------------------------------------------------- #
# Concurrency and self-recovery
# --------------------------------------------------------------------------- #
# Worker threads for blocking I/O (sensor reads, serial reads, SQLite). Size it at the
# number of enabled sensors + 4: one for the MQ board reader, which holds one
# permanently, and three for the recorder, publisher and health tasks. Then every sensor
# can be mid-read (e.g. the ~42 s daily maintenance reads) without delaying the rest.
# Below 5 (4 without the MQ board) the station refuses to start.
THREAD_POOL_WORKERS: int = 12

# A crashed task (sensor, MQ board reader, publisher...) is restarted with exponential backoff.
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
        # ENS160 + AHT21 air quality board (docs/sensors/air-quality-board.md).
        "type": "ens160_aht21",
        "name": "air-quality",
        "interval_s": 60,
        "i2c_bus": 1,
        "ens160_address": None,  # 0x53 (ADD high) or 0x52 (ADD low); None finds it
        "enabled": True,
    },
    {
        # BMP280 barometric pressure sensor (docs/sensors/pressure-sensor.md).
        "type": "bmp280",
        "name": "barometer",
        "interval_s": 60,
        "i2c_bus": 1,
        "address": 0x76,  # 0x76 (SDO low, most boards) or 0x77 (SDO high)
        "enabled": True,
    },
    {
        # SCD41 CO2 sensor (docs/sensors/co2-sensor.md). Fixed I2C address 0x62.
        "type": "scd41",
        "name": "co2",
        "interval_s": 60,
        "i2c_bus": 1,
        # Pressure compensation: the live pressure_hpa of pressure_source (a sensor name,
        # or None), falling back to the standard pressure at altitude_m (0-3000 m above
        # sea level) when that's missing or out of date.
        "pressure_source": "barometer",
        "altitude_m": 0,
        # Applied at every start, in the sensor's RAM (its EEPROM is never written).
        "temperature_offset_c": 4.0,  # its self-heating; set once mounted in its enclosure
        "asc_enabled": True,  # automatic self-calibration: needs fresh air weekly
        "enabled": True,
    },
    {
        # BH1750 ambient light sensor (docs/sensors/light-sensor.md).
        "type": "bh1750",
        "name": "light",
        "interval_s": 60,
        "i2c_bus": 1,
        "address": 0x23,  # 0x23 (ADDR low or open) or 0x5C (ADDR high)
        # The fraction of light its cover lets through (0-1); readings are divided by it.
        # 0.9 is an estimate for clear plastic (acrylic ~0.92, polycarbonate ~0.88);
        # measure it against a lux meter if one is available. The range is automatic.
        "transmittance": 0.9,
        "enabled": True,
    },
    {
        # SEN62 or SEN65 particulate matter sensor (docs/sensors/particulate-sensor.md),
        # detected at start-up; a SEN65 adds the VOC and NOx indices. Fixed I2C address
        # 0x6B. It measures continuously (fan and laser always on); each reading is its
        # latest 1-second result.
        "type": "sen6x",
        "name": "particulates",
        "interval_s": 60,
        "i2c_bus": 1,
        # Blow the dust out of its fan this often (hours; None = never). That reading
        # takes about 42 s instead of 1 s, but is still on time and settled.
        "fan_cleaning_interval_h": 24,
        "enabled": True,
    },
    {
        # MCP9808 precision temperature sensor (docs/sensors/temperature-sensor.md). It
        # sleeps between readings, waking for one 0.3 s measurement each time.
        "type": "mcp9808",
        "name": "temperature",
        "interval_s": 60,
        "i2c_bus": 1,
        "address": 0x1F,  # 0x18-0x1F: 0x1F with A0-A2 open (DFRobot board); see the docs
        "enabled": True,
    },
    {
        # CHT8305 temperature and humidity probe (docs/sensors/humidity-sensor.md). Fixed
        # I2C address 0x40. Each reading is one 11 ms conversion; it idles in between.
        "type": "cht8305",
        "name": "humidity",
        "interval_s": 60,
        "i2c_bus": 1,
        # Run its heater from one reading to the next this often (hours; None = never),
        # to drive off moisture. The reading after waits up to 40 s for it to cool.
        "heater_interval_h": 24,
        "enabled": True,
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
# MQ gas sensor board (UART)
# --------------------------------------------------------------------------- #
# The board broadcasts $PMQB sentences on its UART TX, wired to the Pi's GPIO UART
# RX (GPIO15, pin 10) plus ground. Framing is fixed by the board's spec (8N1).
MQBOARD_ENABLED: bool = True
MQBOARD_PORT: str = "/dev/serial0"
MQBOARD_BAUDRATE: int = 115200

# One MQTT source per sensor: <prefix><sensor>, e.g. mqboard-mq4, mqboard-mq135.
MQBOARD_SOURCE_PREFIX: str = "mqboard-"

# Warn when a sensor's reading hasn't changed for this long (the board reads each
# sensor every 150 s), or when no valid sentence has arrived for this long.
MQBOARD_STALE_SENSOR_S: float = 300.0
MQBOARD_LINK_DOWN_S: float = 30.0

# Readings already on the line when the station starts can be up to 150 s old, so
# they're held until the board's boot time can be estimated (normally within ~25 s).
# After this long they're sent anyway, timestamped on arrival ("ts_basis": "receipt").
MQBOARD_MAX_HOLD_S: float = 300.0

# Corrupt lines are counted and summarised at most once per interval, so a wiring or
# baud-rate problem can't flood the journal and wear out the SD card.
MQBOARD_CORRUPT_LOG_INTERVAL_S: float = 60.0
