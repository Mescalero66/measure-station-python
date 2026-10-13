# Measure Station

An always-on environmental measurement station for the Raspberry Pi 2B. It polls
sensors and reads data from external microcontrollers over UART, then publishes
everything as MQTT messages to an external broker over Ethernet.

It is built to run unattended:

- **No data loss during network outages.** Every reading is stored in a local SQLite
  outbox first, and removed only after the broker acknowledges it.
- **Self-recovering.** Crashed tasks restart on their own. Hung tasks trigger a
  service restart, and a hung OS triggers a hardware reboot.
- **Concurrent.** Each sensor, UART link and the publisher run independently.

## Architecture

```
 sensors (I2C, sysfs...) ─┐                                       ┌─> MQTT broker
 UART links (MCUs) ───────┼─> asyncio.Queue ─> recorder ─> SQLite ─> publisher
 health self-report ──────┘     (memory)     (groups writes) outbox (deletes only on PUBACK)
```

| Layer | Mechanism | Recovers from |
|---|---|---|
| Task | `Supervisor` restarts a crashed task with exponential backoff | Sensor unplugged, I2C glitch, USB-serial adapter re-plugged |
| Link | Publisher reconnects with backoff; data waits in the outbox | Ethernet down, broker restart |
| Process | Tasks send heartbeats; systemd `WatchdogSec` restarts the service if any task hangs | Deadlocked hardware read, frozen event loop |
| OS | systemd keeps the BCM2835 hardware watchdog alive (`RuntimeWatchdogSec`) | Kernel or systemd hang, which triggers a reboot |
| Storage | WAL-mode SQLite; corrupt DB files are quarantined and replaced | Power loss, SD-card corruption |

Readings that have not yet been written to the outbox are held in memory for up to
`RECORDER_FLUSH_INTERVAL_S` (10 s by default; see [SD card wear](#sd-card-wear)). A clean
stop or reboot writes them first. A power cut or hard kill loses them.

Concurrency uses `asyncio`. Blocking hardware I/O (sensor reads, serial reads, SQLite)
runs in a bounded worker thread pool via `asyncio.to_thread`, so a slow device never
stalls the rest of the station.

## Project layout

```
├── deploy/
│   ├── install.sh                  # provisions a fresh Raspberry Pi OS install
│   ├── env.example                 # template for /etc/measure-station/env (secrets)
│   └── systemd/
│       ├── measure-station.service # the service unit (Type=notify, watchdog, hardening)
│       ├── watchdog.conf           # hardware watchdog for systemd
│       └── journald.conf           # caps journal size on the SD card
├── src/measure_station/
│   ├── sysconfig.py                # ALL configurable settings
│   ├── main.py                     # entry point: validates config, wires tasks
│   ├── models.py                   # Reading, MQTT topic/payload encoding
│   ├── storage.py                  # SQLite outbox (store-and-forward)
│   ├── recorder.py                 # queue -> outbox task
│   ├── publisher.py                # outbox -> MQTT task
│   ├── supervisor.py               # restart and heartbeat supervision
│   ├── watchdog.py                 # systemd sd_notify / watchdog
│   ├── health.py                   # periodic station self-report
│   ├── sensors/                    # sensor drivers + registry
│   └── uart/                       # serial reader + line protocols
└── tests/
```

## Raspberry Pi setup (fresh install)

1. **Flash the OS.** In Raspberry Pi Imager, choose **Raspberry Pi OS Lite (32-bit)**,
   which the Pi 2B needs. In the advanced options, set the hostname (this becomes the
   default `STATION_ID`), enable SSH and create your user.

2. **Clone and install.** Over SSH:

   ```bash
   sudo apt-get update && sudo apt-get install -y git
   sudo git clone <repo-url> /opt/measure-station
   sudo bash /opt/measure-station/deploy/install.sh
   ```

   The installer:
   - installs system packages
   - enables I2C and the hardware UART, and turns off the serial login console
   - enables NTP
   - creates a `measure-station` system user with `i2c`, `dialout` and `gpio` access
   - builds a virtualenv in `/opt/measure-station/.venv`
   - installs the systemd service, hardware watchdog and journald limits

   It is safe to re-run.

3. **Configure.**

   ```bash
   sudo nano /opt/measure-station/src/measure_station/sysconfig.py   # broker, sensors, UARTs
   sudo nano /etc/measure-station/env                                # MQTT credentials
   ```

4. **Reboot** to apply the I2C, UART and watchdog changes: `sudo reboot`.
   The service starts automatically on boot.

## Operating

```bash
systemctl status measure-station           # is it running?
journalctl -u measure-station -f           # follow the logs
sudo systemctl restart measure-station     # apply sysconfig.py changes
sqlite3 /var/lib/measure-station/outbox.sqlite3 'select count(*) from outbox'   # backlog
```

If the configuration is invalid, the service logs `Configuration error: ...` and stops
instead of restart-looping (exit code 78). Fix the config, then restart the service.

To update the code: `cd /opt/measure-station && sudo git pull && sudo systemctl restart measure-station`.
Local edits to `sysconfig.py` will conflict with upstream changes to that file. Keep
per-device values (host, credentials, station ID) in `/etc/measure-station/env` where possible.

## SD card wear

Flash wears out from writes, not reads. SQLite rewrites whole 4 KiB pages on every
transaction, so writing each ~200-byte reading on its own costs about 8 KiB of writes.
The recorder therefore groups readings into one transaction per
`RECORDER_FLUSH_INTERVAL_S`, which brings the cost down to about 0.8 KiB per reading (measured).

| `RECORDER_FLUSH_INTERVAL_S` | Writes per reading | Lost on power cut | Extra MQTT latency |
|---|---|---|---|
| `0` | ~8 KiB | nothing already recorded | none |
| `10` (default) | ~0.8 KiB if ≥10 readings arrive per interval | up to 10 s of readings | up to 10 s |

The grouping only pays off when several readings arrive within each interval. A station
with just a few slow sensors writes very little either way. Beyond the software:

- Use a high-endurance card (e.g. SanDisk High Endurance or Samsung PRO Endurance).
- Unclean power loss can damage a card's internal bookkeeping. If mains power is
  unreliable, add a UPS or supercapacitor HAT.
- To keep system logs in RAM only, set `Storage=volatile` in `deploy/systemd/journald.conf`.
  You lose the logs across reboots.

## MQTT messages

| Topic | Payload |
|---|---|
| `<base>/<station>/<source>` | `{"station", "source", "ts", "clock_synced", "values": {...}}` |
| `<base>/<station>/status` | `online` / `offline` (retained; `offline` is also the last-will) |
| `<base>/<station>/health` | uptime, outbox backlog, queue depth, task restarts, load, memory, disk |

`ts` is the UTC capture time, not the send time, so buffered data keeps its true
timestamp. Delivery is **at-least-once** (QoS 1), which means a reading can arrive twice
around a reconnect. Consumers should de-duplicate on `(station, source, ts)`.

`clock_synced` is `false` for readings taken before NTP has synchronised since boot.
The Pi has no real-time clock, so after an offline boot its time can be wrong. If
accurate timestamps during long outages matter, add an RTC module (e.g. DS3231).

## Broker and consumer requirements

This section is for whoever runs the MQTT broker and the system that stores or processes
station data. The station's guarantee ends at the broker, so whether data actually reaches
storage depends on how the broker and consumer are configured.

### What the station guarantees

The station keeps every reading in its outbox until the broker sends a **PUBACK** (QoS 1).
A PUBACK confirms only that *the broker* received the message, not that any subscriber
did. MQTT has no end-to-end acknowledgement. Once PUBACK arrives the station deletes its
copy, so from then on the broker is responsible for the message. A broker can acknowledge
a message and still lose it if:

- no subscriber is connected and none has a persistent session, so the broker discards
  the message; or
- the broker restarts before writing the message to disk.

### Broker

- **Enable persistence**, and save often enough that a broker crash loses little.
  Mosquitto's default `autosave_interval` is 1800 s, so it can lose 30 minutes of data.
- **Raise the per-client queue limit.** It must be able to hold a whole station backlog
  while the consumer is offline. Mosquitto's default `max_queued_messages` is 1000, and
  messages beyond that are dropped.
- **Grant each station publish rights** (including retained messages) on
  `<base>/<station>/#`.

Example Mosquitto settings:

```conf
persistence true
persistence_location /var/lib/mosquitto/
autosave_interval 60
max_queued_messages 0        # unlimited; or size it to your longest expected consumer outage
max_queued_bytes 0
```

Other brokers have equivalents. Check limits on session expiry and queued messages,
especially on managed cloud brokers.

### Consumer (storage or processing)

- **Use a persistent session**, so the broker queues messages while the consumer is down.
  Give it a fixed client ID, and connect with `clean_session=False` (MQTT 3.1.1), or with
  `clean_start=False` and a non-zero session expiry interval (MQTT 5).
- **Subscribe at QoS 1.** A message is delivered at the lower of the publish and
  subscription QoS. A QoS 0 subscription downgrades delivery to at-most-once, and brokers
  generally don't queue QoS 0 messages for offline clients.
- **Acknowledge only after the data is stored.** If the consumer acknowledges first and
  then crashes before writing, the message is lost. With paho-mqtt 2.x, use
  `manual_ack=True` and call `client.ack(mid, qos)` after the write commits.
- **De-duplicate on `(station, source, ts)`**, for example with a unique constraint.
  Delivery is at-least-once, so duplicates occur around reconnects.
- **Order by `ts`, not arrival order.** The station sends messages in concurrent batches
  and resends after reconnects, so arrival order isn't guaranteed.
- **Expect late data.** After an outage, readings arrive hours or days late with their
  original `ts`. Aggregations and alerts must handle data arriving after its time window.

### Monitoring

- `<base>/<station>/status` is retained. It changes to `offline` on a clean stop, or via
  the last-will when the connection drops.
- On `<base>/<station>/health`, a rising `outbox_backlog` means data is waiting to be sent.
  If no health message arrives for several `HEALTH_INTERVAL_S` periods, the station is
  unreachable or stalled.

### End-to-end acknowledgement (not implemented)

If the station must keep data until the *storage system* confirms it, the storage system
would publish acknowledgements to a topic the station subscribes to, and the station would
delete outbox rows only on that acknowledgement. Getting the broker and consumer settings
above right covers most of the same risk with configuration alone.

## Adding a sensor

1. Create `src/measure_station/sensors/my_sensor.py`:

   ```python
   from measure_station.sensors.base import Sensor


   class MySensor(Sensor):
       def __init__(self, name, interval_s, pin=4):
           super().__init__(name, interval_s)
           self._pin = pin

       def open(self):  # acquire hardware (called again after any failure)
           ...

       def read(self):  # blocking; runs in a worker thread
           return {"lux": 123.4}

       def close(self): ...
   ```

2. Register it in `SENSOR_TYPES` in `sensors/__init__.py`.
3. Add an entry to `SENSORS` in `sysconfig.py`, e.g.
   `{"type": "my_sensor", "name": "light", "interval_s": 10, "pin": 4}`.

## UART protocol

Microcontrollers send one newline-terminated message per reading. There are two formats:

- `json`: `{"temp_c": 21.4, "rh_pct": 40.1}`
- `kv`: `temp_c=21.4,rh_pct=40.1`

Each link publishes under its configured `name`. Malformed or overlong lines are
discarded and reported in a summary at most once per minute. For USB-serial adapters,
use the stable `/dev/serial/by-id/...` path rather than `/dev/ttyUSB0`.

## Development

The package runs on any OS. Hardware drivers are optional, and the `simulated`
sensor generates data so you can exercise the pipeline.

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"     # Windows: .venv\Scripts\pip
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/python -m measure_station   # data goes to ./data when not under systemd
```

Always run the station with `python -m measure_station`, never by file path. Running
a file inside the package would put that directory on `sys.path`, and then
`sysconfig.py` would shadow the standard library's `sysconfig` module.
