# Server Pi Interface

This is the interface between a Measure Station and the server Pi that will receive,
store and display its data (MQTT broker, database and web UI). It's written for the server
Pi's developer, and is self-contained: you don't need the station's code to build against
it. For the reasons behind each rule, see [MQTT Messages](mqtt-messages.md),
[Broker and Consumer Requirements](broker-and-consumer.md) and
[Timestamps Without a Real-Time Clock](timekeeping.md).

## At a Glance

```text
 Station Pi (Pi 2B)                          Server Pi
 sensors -> SQLite outbox -> MQTT client ==> MQTT broker -> consumer -> database -> web UI
                (SD card)    QoS 1, publish     (persistent)   (persistent session,
                             only                              QoS 1, de-duplicates)
        \                                         /
         `---- both take NTP from one LAN time source
```

| Item | Value |
|---|---|
| Transport | MQTT 3.1.1 over TCP, over the LAN (Ethernet) |
| Direction | Station to broker only. The station connects out, publishes, and subscribes to nothing; it accepts no commands |
| Topics | `measure-station/<station>/<source>`, plus `measure-station/<station>/status` |
| Payload | Compact UTF-8 JSON, 150 to 490 bytes (the status topic is plain text) |
| Delivery | QoS 1, at-least-once: expect duplicates |
| Rate per station | About 16,400 messages a day (about 11 a minute) |
| Timestamps | UTC capture time in the payload (`ts`), never the arrival time |
| De-duplication key | `(station, source, ts)` |
| Backlog | Up to 1,500,000 messages buffered on the station during an outage, sent oldest first when it reconnects |

## Best Source for Each Quantity

Several sensors report the same quantity. Each is listed in one of three columns:

- **Primary:** the most accurate measurement of the atmosphere. Use it for display and
  analysis.
- **Secondary:** also a correct measurement of the atmosphere, but less accurate. Use it
  for cross-checks, or as a fallback while the primary is down.
- **Meta:** a figure for the sensor's own use, such as a compensation input or the
  temperature of a chip or board. It isn't the atmosphere: never chart it as one.

Each entry is `source` · `section.key` (part, typical accuracy).

| Quantity | Primary | Secondary | Meta |
|---|---|---|---|
| Air temperature | `temperature` · `values.temperature_c` (MCP9808, ±0.25 °C, steps of 0.0625 °C) | `humidity` · `values.temperature_c` (CHT8305, ±0.3 °C, ±0.5 °C below 0 °C); `particulates` · `values.temperature_c` (SEN62, ±0.45 °C); `co2` · `values.temperature_c` (SCD41, after a hand-set offset for its self-heating) | `barometer` · `meta.temperature_c` (BMP280 chip); `air-quality` · `meta.temperature_c` (AHT21, beside the ENS160's heater); `mqboard-*` · `meta.temperature_c` (the MQ board's compensation input); `cpu` · `values.temperature_c` (the Pi's processor) |
| Relative humidity | `humidity` · `values.humidity_pct` (CHT8305, ±2 %RH) | `particulates` · `values.humidity_pct` (SEN62, ±4.5 %RH); `co2` · `values.humidity_pct` (SCD41) | `air-quality` · `meta.humidity_pct` (AHT21, beside the heater, so it reads dry) |
| Pressure | `barometer` · `values.pressure_hpa` (BMP280, ±1 hPa absolute, ±0.12 hPa relative) | | `co2` · `meta.pressure_hpa` (the pressure the SCD41 was compensated with) |
| CO2 | `co2` · `values.co2_ppm` (SCD41, ±(50 ppm + 2.5 %) up to 1000 ppm) | | |
| Particulates | `particulates` · `values.pm25_ugm3` (SEN62, ±(5 µg/m³ + 5 %) up to 100 µg/m³), and `pm1_ugm3`, `pm4_ugm3`, `pm10_ugm3` (PM4 and PM10: ±25 µg/m³) | | |
| Illuminance | `light` · `values.illuminance_lux` (BH1750, ±20 %; the cover's transmittance is estimated) | | |
| VOCs, air quality index | `air-quality` · `values.tvoc_ppb`, `values.aqi` (ENS160, indicative; only while `meta.validity` is `"normal"`) | | |
| Methane, CO, H2, LPG, NH3, smoke | `mqboard-mq4`, `-mq7`, `-mq8`, `-mq5`, `-mq135`, `-mq2` · `values.concentration_ppm` (indicative, as MQ sensors are cross-sensitive; absolute only while `meta.baseline_tracking` is `true`) | | |

- **`air-quality` · `values.eco2_ppm` isn't CO2.** The ENS160 estimates it from VOCs. Use
  `co2` for CO2.
- **Dew point and absolute humidity:** calculate them from `humidity`'s own
  `temperature_c` and `humidity_pct`, never from one sensor's humidity and another's
  temperature.
- **`cpu` and `health`** describe the station itself, not the environment.

## Network and Time

- **Broker port:** the station connects to the broker's host and port (1883 by default, or
  8883 with TLS). Nothing connects to the station.
- **Time:** the station and the server Pi must both take NTP from **the same time source
  on the LAN**, so their clocks agree. That source must not serve time before it has real
  time itself. In particular, the server Pi must not serve its own clock to the station:
  a Pi has no battery-backed clock, so after a power cut its time can be wrong. See
  [Choosing the Time Source](timekeeping.md#choosing-the-time-source).

## Broker Requirements

**How the station connects**

| Setting | Value |
|---|---|
| Protocol | MQTT 3.1.1 |
| Client ID | `measure-station-<station>`, e.g. `measure-station-ms-2b` |
| Clean session | Yes. The station keeps its own outbox, so it needs no session state on the broker |
| Keepalive | 30 s |
| Authentication | Username and password, optional (set on the station) |
| TLS | Optional: CA certificate, and a client certificate if the broker requires one |
| Last will | `measure-station/<station>/status` = `offline`, QoS 1, retained |
| Publish QoS | 1, with up to 50 messages in flight at once |
| PUBACK timeout | 10 s. If the broker hasn't acknowledged a message by then, the station drops the connection, reconnects and re-sends |

**What the broker must do**

- **Acknowledge only what it can keep.** The station deletes each reading as soon as the
  broker's PUBACK arrives. From then on the broker is responsible for it. So:
  - enable persistence, and save often (Mosquitto's default `autosave_interval` of
    1800 s can lose 30 minutes of data in a crash; use 60 or less);
  - raise the per-client queue limit so it holds the whole backlog while the consumer is
    offline (Mosquitto's default `max_queued_messages` of 1000 drops the rest).
- **Allow each station to publish, including retained messages,** on
  `measure-station/<station>/#`.
- **Keep up with a backlog.** After an outage the station sends its whole backlog in
  batches of 50. The broker must acknowledge each within 10 s even then.

Example Mosquitto settings:

```conf
persistence true
persistence_location /var/lib/mosquitto/
autosave_interval 60
max_queued_messages 0        # unlimited; or size it for the longest consumer outage
max_queued_bytes 0
```

## Topics

| Topic | Payload | Retained |
|---|---|---|
| `measure-station/<station>/<source>` | A reading, as JSON | No |
| `measure-station/<station>/status` | The plain text `online` or `offline` (not JSON) | Yes |

- **`measure-station`** is the station's base topic. It's a setting (`MQTT_BASE_TOPIC`),
  but every station uses this default unless told otherwise.
- **`<station>`** is the station ID (e.g. `ms-2b`) and **`<source>`** names the data
  series. Both match `^[A-Za-z0-9_.-]+$`, so they contain no `/`, `+` or `#`.
- **`status` is reserved:** no reading uses it as a source. Parse it separately, since its
  payload isn't JSON.
- **Subscribe to `measure-station/+/+`** to receive everything from every station. The
  source in the topic always equals the payload's `source`.

**The sources:**

| `source` | `sensor_type` | What it measures | Rate |
|---|---|---|---|
| `cpu` | `cpu_temperature` | The Pi's processor temperature | 1/min |
| `air-quality` | `ens160_aht21` | VOCs, air quality index, estimated CO2 | 1/min |
| `barometer` | `bmp280` | Air pressure | 1/min |
| `co2` | `scd41` | CO2, temperature, humidity | 1/min |
| `light` | `bh1750` | Illuminance | 1/min |
| `particulates` | `sen62` | Particulate matter, temperature, humidity | 1/min |
| `temperature` | `mcp9808` | Air temperature | 1/min |
| `humidity` | `cht8305` | Humidity and temperature | 1/min |
| `mqboard-mq2`, `-mq4`, `-mq5`, `-mq7`, `-mq8`, `-mq135` | `mq_board` | One gas each (MQ gas board) | 1 per 150 s each |
| `health` | `station` | The station's own condition | 1/min |

The sensor set is final. A source can stop (a sensor disabled or failed), but its name
won't change.

## Message Format

Every reading is one JSON object, sent compact (no spaces or line breaks). Formatted for
reading:

```json
{
  "station": "ms-2b",
  "source": "co2",
  "sensor_type": "scd41",
  "ts": "2026-10-21T09:40:00.000+00:00",
  "ts_src": "ntp",
  "values": {
    "co2_ppm": 500,
    "temperature_c": 25.0,
    "humidity_pct": 37.0
  },
  "meta": {
    "pressure_hpa": 1007,
    "comp_src": "hpa"
  }
}
```

| Field | JSON type | Always present | Meaning |
|---|---|---|---|
| `station` | string | yes | The station ID; equals the topic's second level |
| `source` | string | yes | The data series; equals the topic's last level |
| `sensor_type` | string | yes | The hardware that produced the reading. If a part is ever replaced by another model, `source` stays and `sensor_type` changes |
| `ts` | string | yes | Capture time, UTC, ISO 8601 with milliseconds and a `+00:00` offset (never `Z`): `2026-10-21T09:40:00.000+00:00` |
| `ts_src` | string | yes | Where `ts` came from: `ntp`, `corrected` or `estimated`. See [Timestamps](#timestamps) |
| `values` | object | yes | What the sensor is there to measure |
| `meta` | object | no | How the reading was made or how far to trust it: compensation inputs, status flags, calibration parameters. Left out when empty |
| `boot_id` | string (UUID) | only with `ts_src: "estimated"` | Identifies the Pi boot the reading was taken in. Informational |
| `boot_ts` | number | only with `ts_src: "estimated"` | Seconds since that boot, to 3 decimals. Informational |

**Rules for `values` and `meta`:**

- **Values are JSON numbers, booleans, strings or `null`.** The only array is `health`'s
  `mqboard_stale_sensors`. `null` means "unknown" (never 0); NaN and infinity are sent as
  `null`.
- **Store every number as a double.** A few keys are sent as JSON integers
  (`co2_ppm`, `aqi`, the counts in `health`); the rest have a decimal point (`25.0`).
- **Key names are `<quantity>_<unit>`**: `_c` °C, `_pct` % relative humidity, `_hpa` hPa,
  `_ppm`, `_ppb`, `_ugm3` µg/m³, `_lux`, `_kohm` kΩ, `_s` seconds, `_mb` megabytes. Keys
  with no unit suffix are text codes, booleans or counts.
- **The same key can appear in both sections with different meanings.** `pressure_hpa` in
  `barometer`'s `values` is a measurement; in `co2`'s `meta` it's the pressure its reading
  was compensated with. **Key series by `(station, source, section, key)`.**
- **Flags are text or booleans**, never numeric codes.
- **Ignore unknown keys** rather than rejecting the message. Keys are never renamed or
  repurposed once deployed, but a flag key can appear only when it's set (e.g.
  `particulates`' `meta.pm`).

## Timestamps

The station's Pi has no battery-backed clock. Readings taken before its clock syncs at
boot are held on the station and re-timed once it syncs, so the server doesn't need to
correct anything. `ts_src` says how a timestamp was obtained:

| `ts_src` | Meaning | Treat as |
|---|---|---|
| `ntp` | Taken while the clock was synchronised | Accurate |
| `corrected` | Taken before sync, re-timed by the station once it synced | Accurate (within seconds, even over a day) |
| `estimated` | Couldn't be corrected; `ts` is the Pi's unsynchronised clock | Approximate: flag it, or exclude it from time-sensitive analysis. `boot_id` and `boot_ts` are included for reference |

When to expect `estimated`:

- **`health` readings before sync.** Health is never held, so a clock problem stays
  visible: while the station is unsynchronised, its health readings arrive as
  `estimated`, with `clock_synced: false`.
- **Readings held longer than 7 days** waiting for a sync.
- **Readings from a boot that never synced** (the Pi restarted before it got the time).

Consequences for the server:

- **Data arrives late.** Readings taken before sync arrive when the station syncs, which
  can be minutes to days after they were taken. After a network outage, the backlog
  arrives with its original timestamps. Aggregates, charts and alerts must accept data
  for past time windows.
- **Order by `ts`, never by arrival.** The station sends in batches of 50 at once, and
  re-sends after reconnects.
- **A rare duplicate has a different `ts`.** A `health` reading (or one released after
  the 7-day hold) whose acknowledgement was lost just before the clock synced is re-sent
  as `corrected`, with a new `ts`. The de-duplication key doesn't catch it, so the
  database keeps both: one `estimated`, one `corrected`.

## Readings by Source

Precision is the number of decimals sent. "Number or null" means the sensor can report a
value as unknown.

### `cpu`

| Section | Key | Type | Unit | Precision | Meaning |
|---|---|---|---|---|---|
| `values` | `temperature_c` | number | °C | 2 | The Pi's processor temperature |

### `temperature` (MCP9808)

| Section | Key | Type | Unit | Precision | Meaning |
|---|---|---|---|---|---|
| `values` | `temperature_c` | number | °C | steps of 0.0625 (up to 4 decimals) | Air temperature, -40 to 125 °C |

No `meta`. About 155 bytes.

### `humidity` (CHT8305)

| Section | Key | Type | Unit | Precision | Meaning |
|---|---|---|---|---|---|
| `values` | `temperature_c` | number | °C | 2 | Air temperature at the probe, -40 to 125 °C |
| `values` | `humidity_pct` | number | % | 1 | Relative humidity, 0 to 100 % |

No `meta`. About 170 bytes.

### `barometer` (BMP280)

| Section | Key | Type | Unit | Precision | Meaning |
|---|---|---|---|---|---|
| `values` | `pressure_hpa` | number | hPa | 2 | Station (absolute) pressure, 300 to 1100 hPa. Not sea-level pressure: about 1.2 hPa lower per 10 m of altitude |
| `meta` | `temperature_c` | number | °C | 2 | The chip's own temperature, used in its pressure calculation |

About 180 bytes.

### `co2` (SCD41)

| Section | Key | Type | Unit | Precision | Meaning |
|---|---|---|---|---|---|
| `values` | `co2_ppm` | integer | ppm | 0 | CO2 concentration, measured. Specified for 400 to 5000 ppm |
| `values` | `temperature_c` | number | °C | 2 | Temperature at the sensor, after a hand-set offset for its self-heating |
| `values` | `humidity_pct` | number | % | 1 | Relative humidity at the sensor |
| `meta` | `pressure_hpa` | integer | hPa | 0 | The pressure this CO2 reading was compensated with, 700 to 1200 |
| `meta` | `comp_src` | text | | | `"hpa"`: live pressure from `barometer`. `"alt"`: the standard pressure at the station's altitude (fallback; slightly less accurate CO2) |

About 220 bytes.

### `particulates` (SEN62)

| Section | Key | Type | Unit | Precision | Meaning |
|---|---|---|---|---|---|
| `values` | `pm1_ugm3` | number or null | µg/m³ | 1 | PM1.0 (particles 0.3 to 1.0 µm) |
| `values` | `pm25_ugm3` | number or null | µg/m³ | 1 | PM2.5 (0.3 to 2.5 µm): the one to compare with standards |
| `values` | `pm4_ugm3` | number or null | µg/m³ | 1 | PM4.0 (0.3 to 4.0 µm) |
| `values` | `pm10_ugm3` | number or null | µg/m³ | 1 | PM10 (0.3 to 10 µm) |
| `values` | `temperature_c` | number or null | °C | 2 | Air temperature, compensated for the module's heat |
| `values` | `humidity_pct` | number or null | % | 1 | Relative humidity, compensated likewise |
| `meta` | `pm` | text | | | Present only when the PM values may be wrong: `"fan_speed"` (the fan is off its target speed) |

Normally no `meta`. About 230 bytes, or 260 with `meta`.

- The classes are cumulative: PM1 ≤ PM2.5 ≤ PM4 ≤ PM10.
- Above about 80 %RH, PM reads high (water droplets count as particles).

### `light` (BH1750)

| Section | Key | Type | Unit | Precision | Meaning |
|---|---|---|---|---|---|
| `values` | `illuminance_lux` | number | lx | up to 2 | Illuminance, about 0.1 to 135,000 lx. Brighter light reads as the maximum |
| `meta` | `range` | text | | | `"hi"` (daylight, steps of about 2 lx) or `"lo"` (below 5000 lx, steps of about 0.13 lx) |

About 170 bytes.

### `air-quality` (ENS160 + AHT21)

| Section | Key | Type | Unit | Precision | Meaning |
|---|---|---|---|---|---|
| `values` | `aqi` | integer | | | Air quality index, UBA scale: 1 excellent, 2 good, 3 moderate, 4 poor, 5 unhealthy |
| `values` | `tvoc_ppb` | integer | ppb | 0 | Total volatile organic compounds, 0 to 65,000 |
| `values` | `eco2_ppm` | integer | ppm | 0 | Equivalent CO2, 400 to 65,000: estimated from VOCs, not measured |
| `meta` | `validity` | text | | | `"normal"`; `"warmup"` (first 3 minutes after each start); `"startup"` (first hour of a new sensor); `"invalid"`. **Trust `values` only when `"normal"`** |
| `meta` | `temperature_c` | number | °C | 2 | Temperature on the board: a compensation input, not room temperature |
| `meta` | `humidity_pct` | number | % | 1 | Humidity on the board: a compensation input, not room humidity |

About 245 bytes.

### `mqboard-<sensor>` (MQ Gas Board)

Six sources: `mqboard-mq2`, `mqboard-mq4`, `mqboard-mq5`, `mqboard-mq7`, `mqboard-mq8` and
`mqboard-mq135`, each one new reading every 150 s.

| Section | Key | Type | Unit | Precision | Meaning |
|---|---|---|---|---|---|
| `values` | `concentration_ppm` | number or null | ppm | 2 | Concentration of the target gas. `null` = a failed read (published so faults are visible) |
| `meta` | `gas` | text | | | The target gas: see below |
| `meta` | `temperature_c` | number or null | °C | 2 | Temperature used to compensate the reading; `null` = uncompensated |
| `meta` | `baseline_tracking` | boolean | | | `true`: an established clean-air baseline. `false`: a provisional boot calibration, so the ppm shows changes but not reliable levels |
| `meta` | `r0_kohm` | number | kΩ | 6 | The clean-air reference resistance the ppm was calculated with |
| `meta` | `ts_basis` | text | | | `"board"`: `ts` derived from the board's uptime (normal). `"receipt"`: timestamped on arrival (fallback, up to about 150 s late) |

About 260 bytes.

| Source | `gas` | Target gas | Clean-air reading (about) | Curve exponent `b` |
|---|---|---|---|---|
| `mqboard-mq2` | `SMK` | Smoke | 13 ppm | -2.440 |
| `mqboard-mq4` | `CH4` | Methane | 16 ppm | -2.786 |
| `mqboard-mq5` | `LPG` | Liquefied petroleum gas | 0.85 ppm | -2.431 |
| `mqboard-mq7` | `CO` | Carbon monoxide | 0.65 ppm | -1.518 |
| `mqboard-mq8` | `H2` | Hydrogen | 53 ppm | -0.688 |
| `mqboard-mq135` | `NH3` | Ammonia | 4.3 ppm | -2.473 |

- **The readings are indicative.** MQ sensors respond to several gases, so the ppm is only
  accurate when the target gas is the one present. Clean air reads near the floor above,
  not zero.
- **`r0_kohm` = 10.0 with `baseline_tracking: false`** means the boot calibration failed:
  that ppm isn't meaningful.
- **To compare readings on one baseline**, recompute against a fixed R0:
  `ppm_new = concentration_ppm × (r0_kohm / r0_new) ^ b`. See
  [Recomputing ppm](uart-interface.md#recomputing-ppm).

### `health`

The station's self-report, once a minute. `sensor_type` is `station`; there's no `meta`.
About 414 bytes (488 when `estimated`).

```json
{
  "station": "ms-2b",
  "source": "health",
  "sensor_type": "station",
  "ts": "2026-10-22T08:00:00.000+00:00",
  "ts_src": "ntp",
  "values": {
    "uptime_s": 86400,
    "outbox_backlog": 3,
    "queue_depth": 0,
    "task_restarts": 0,
    "clock_synced": true,
    "awaiting_time_sync": 0,
    "load_1m": 0.42,
    "disk_free_mb": 11234.5,
    "mem_available_mb": 702.3,
    "mqboard_up": true,
    "mqboard_last_line_age_s": 1.8,
    "mqboard_bad_lines": 0,
    "mqboard_stale_sensors": [],
    "mqboard_held": 0
  }
}
```

| Key | Type | Meaning | Worth an alert when |
|---|---|---|---|
| `uptime_s` | integer | Seconds since the station service started (not the Pi) | It drops: the service restarted |
| `outbox_backlog` | integer | Readings stored on the station, not yet acknowledged by the broker | It keeps rising: data isn't reaching the broker |
| `queue_depth` | integer | Readings in memory, not yet written to the SD card | It stays high: the SD card is struggling |
| `task_restarts` | integer | Task restarts since the service started (a sensor fault, a dropped connection) | It keeps rising |
| `clock_synced` | boolean | Whether the station's clock is NTP-synchronised | `false` for more than a few minutes |
| `awaiting_time_sync` | integer | Readings held on the station until its clock syncs | Above 0 for long: NTP is unreachable |
| `load_1m` | number | CPU load average over 1 minute (the Pi 2B has 4 cores) | |
| `disk_free_mb` | number | Free space on the SD card, in MB | It falls below a few hundred MB |
| `mem_available_mb` | number | Available memory, in MB | |
| `mqboard_up` | boolean | Whether the MQ board's data link is up | `false` |
| `mqboard_last_line_age_s` | number or null | Seconds since the last valid line from the board (`null`: none yet) | Above 30 |
| `mqboard_bad_lines` | integer | Corrupt lines from the board since the service started | It rises steadily: a wiring fault |
| `mqboard_stale_sensors` | array of strings | Board sensors whose reading hasn't changed for 5 minutes, by board name (e.g. `["MQ4"]`) | Not empty |
| `mqboard_held` | integer | Board readings held until they can be timestamped | Above 0 for more than 5 minutes |

- `load_1m`, `disk_free_mb` and `mem_available_mb` are left out if the system can't
  report them.
- The `mqboard_*` keys are left out if the MQ board is disabled on the station.
- **Missing health is the main alarm.** If no `health` reading arrives for several
  minutes (by the server's own clock), the station is offline or stalled. Check the
  retained `status` topic too: `offline` after a clean stop or a dropped connection.

## What the Consumer Must Do

1. **Use a persistent session**, so the broker queues messages while the consumer is
   down: a fixed client ID with `clean_session=False` (MQTT 3.1.1), or `clean_start=False`
   with a non-zero session expiry (MQTT 5).
2. **Subscribe at QoS 1.** A QoS 0 subscription downgrades delivery to at-most-once.
3. **Acknowledge a message only after it's committed to the database.** With paho-mqtt
   2.x, use `manual_ack=True` and call `client.ack(mid, qos)` after the commit.
4. **De-duplicate on `(station, source, ts)`**, e.g. a unique key with "insert, ignore
   if it exists".
5. **Treat the `status` topic separately** (plain text, retained).
6. **Ignore unknown keys and sources** instead of rejecting the message. Log a
   message that isn't valid JSON, and acknowledge it, so it can't block the queue.
7. **Check the quality flags** before using values: `ts_src`, `air-quality`'s `validity`,
   `particulates`' `pm`, the MQ board's `baseline_tracking`.

## Suggested Storage Design

A simple design that loses nothing: store each message as received, with the
de-duplication key as the primary key, and derive charts from it.

```sql
CREATE TABLE reading (
    station     TEXT        NOT NULL,
    source      TEXT        NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,   -- from the payload, UTC
    sensor_type TEXT        NOT NULL,
    ts_src      TEXT        NOT NULL,   -- ntp | corrected | estimated
    values_json JSONB       NOT NULL,
    meta_json   JSONB,                  -- NULL when the message has no meta
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (station, source, ts)
);

INSERT INTO reading (station, source, ts, sensor_type, ts_src, values_json, meta_json)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT DO NOTHING;
```

(PostgreSQL shown. SQLite works the same way with `TEXT` timestamps and `INSERT OR
IGNORE`.)

- **For charts**, a narrow table or view with one row per value is easier to query:
  `(station, source, section, key, ts, num_value, text_value)`.
- **Volume:** about 16,400 messages a day per station, roughly 4 MB of JSON a day, or
  1.4 GB a year, before indexes or compression. An SD card on the server Pi faces the
  same wear as the station's; consider a USB SSD for the database.
- **Keep `received_at`.** Its difference from `ts` shows outages and late data.

## Checklist for the Server Pi

- [ ] The server Pi takes NTP from the same LAN time source as the station, and doesn't
      serve time itself.
- [ ] The broker listens on the agreed port, with persistence on, `autosave_interval` of
      60 s or less, and a queue limit that holds a full backlog.
- [ ] Each station's login (if any) may publish and retain on
      `measure-station/<station>/#`.
- [ ] The consumer uses a fixed client ID, a persistent session and QoS 1, and
      acknowledges after committing.
- [ ] The database rejects duplicates on `(station, source, ts)`.
- [ ] Charts and aggregates are by `ts`, and accept late data.
- [ ] Alerts: no `health` for several minutes; `status` = `offline`; `clock_synced` false;
      a rising `outbox_backlog`.
- [ ] Then set `MEASURE_STATION_MQTT_HOST` (and port, username and password) on the
      station, restart it, and watch its `outbox_backlog` drain to zero.
