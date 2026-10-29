# Measure Station

An always-on environmental measurement station for the Raspberry Pi 2B. It polls
sensors and reads an MQ gas sensor board over the Pi's UART, then publishes
everything as MQTT messages to an external broker over Ethernet.

It is built to run unattended:

- **No data loss during network outages.** Every reading is stored in a local SQLite
  outbox first, and removed only after the broker acknowledges it.
- **Self-recovering.** Crashed tasks restart on their own. Hung tasks trigger a
  service restart, and a hung OS triggers a hardware reboot.
- **Concurrent.** Each sensor, the MQ board reader and the publisher run independently.

## Architecture

```
 sensors (I2C, sysfs...) ─┐                                       ┌─> MQTT broker
 MQ gas board (UART) ─────┼─> asyncio.Queue ─> recorder ─> SQLite ─> publisher
 health self-report ──────┘     (memory)     (groups writes) outbox (deletes only on PUBACK)
```

| Layer | Mechanism | Recovers from |
|---|---|---|
| Task | `Supervisor` restarts a crashed task with exponential backoff | Sensor unplugged, I2C glitch, serial port error |
| Link | Publisher reconnects with backoff; data waits in the outbox | Ethernet down, broker restart |
| Process | Tasks send heartbeats; systemd `WatchdogSec` restarts the service if any task hangs | Deadlocked hardware read, frozen event loop |
| OS | systemd keeps the BCM2835 hardware watchdog alive (`RuntimeWatchdogSec`) | Kernel or systemd hang, which triggers a reboot |
| Storage | WAL-mode SQLite; corrupt DB files are quarantined and replaced | Power loss, SD-card corruption |

Readings that have not yet been written to the outbox are held in memory for up to
`RECORDER_FLUSH_INTERVAL_S` (10 s by default; see [SD Card Wear](docs/sd-card-wear.md)). A clean
stop or reboot writes them first. A power cut or hard kill loses them.

Concurrency uses `asyncio`. Blocking hardware I/O (sensor reads, serial reads, SQLite)
runs in a bounded worker thread pool via `asyncio.to_thread`, so a slow device never
stalls the rest of the station.


## Project Layout

```
├── README.md                       # overview, architecture, documentation index
├── docs/                           # these documents
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
│   ├── clock.py                    # boot clock, NTP sync detection, timestamp correction
│   ├── timesync.py                 # task: detects sync, back-dates held readings
│   ├── ntp_setup.py                # applies NTP_SERVERS to timesyncd (runs as root at start)
│   ├── storage.py                  # SQLite outbox (store-and-forward)
│   ├── recorder.py                 # queue -> outbox task
│   ├── publisher.py                # outbox -> MQTT task
│   ├── supervisor.py               # restart and heartbeat supervision
│   ├── watchdog.py                 # systemd sd_notify / watchdog
│   ├── health.py                   # periodic station self-report
│   ├── read_sensor.py              # prints one sensor's readings, for checking wiring
│   ├── drivers/                    # hardware drivers, verbatim from drivers-repo
│   ├── sensors/                    # sensor classes (adapters for drivers/) + registry
│   └── uart/                       # MQ gas board: serial reader + $PMQB decoder
└── tests/
```

## Documentation

**Setting Up and Running a Station**

- [Deploying the Station](docs/deploying-the-station.md): step by step from an empty SD
  card to a checked, running station, for readers new to Linux.
- [Operating](docs/operating.md): status, logs, applying configuration changes, updating.
- [SD Card Wear](docs/sd-card-wear.md): what the station writes, and how to make the card
  last.

**Data and Integration**

- [MQTT Messages](docs/mqtt-messages.md): topics, payloads and timestamp quality.
- [Timestamps Without a Real-Time Clock](docs/timekeeping.md): how readings get accurate
  times before NTP sync, and using a local NTP server.
- [Broker and Consumer Requirements](docs/broker-and-consumer.md): what the MQTT broker and
  the storage/processing system must do so no data is lost.
- [Server Pi Interface](docs/server-pi-interface.md): everything the server Pi's developer
  needs: topics, payload fields and types per source, the best source for each quantity,
  delivery rules and a suggested database design.

**Hardware**

- [Air Quality Board](docs/sensors/air-quality-board.md): the ENS160 + AHT21 board's wiring,
  published values and warm-up.
- [CO2 Sensor](docs/sensors/co2-sensor.md): the SCD41's wiring, published values and
  calibration.
- [Humidity Sensor](docs/sensors/humidity-sensor.md): the CHT8305 probe's wiring, accuracy
  and probe version.
- [Light Sensor](docs/sensors/light-sensor.md): the BH1750's wiring, range and settings.
- [Particulate Sensor](docs/sensors/particulate-sensor.md): the SEN62 or SEN65's wiring,
  published values (with the SEN65's VOC and NOx indices), status flags and operating limits.
- [Pressure Sensor](docs/sensors/pressure-sensor.md): the BMP280's wiring and published values.
- [Temperature Sensor](docs/sensors/temperature-sensor.md): the MCP9808's wiring, accuracy and
  why it sleeps between readings.
- [MQ Gas Board](docs/sensors/mq-gas-board.md): wiring, what's published, and how readings are
  handled.
- [MQ Gas Board UART Interface](docs/uart-interface.md): the board's `$PMQB` sentence
  specification.
- [Adding a Sensor](docs/adding-a-sensor.md): writing and registering a new sensor driver.

**Development**

- [Development](docs/development.md): running tests and the station on a dev machine.
