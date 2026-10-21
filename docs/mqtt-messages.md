# MQTT Messages

| Topic | Payload |
|---|---|
| `<base>/<station>/<source>` | `{"station", "source", "sensor_type", "ts", "ts_src", "values": {...}, "meta": {...}}` |
| `<base>/<station>/status` | `online` / `offline` (retained; `offline` is also the last-will) |
| `<base>/<station>/health` | uptime, outbox backlog, queue depth, task restarts, clock sync, load, memory, disk |

For example:

```json
{
  "station": "pi-01",
  "source": "co2",
  "sensor_type": "scd41",
  "ts": "2026-10-21T09:40:00.000+00:00",
  "ts_src": "ntp",
  "values": {"co2_ppm": 500, "temperature_c": 25.0, "humidity_pct": 37.0},
  "meta": {"pressure_hpa": 1007, "comp_src": "hpa"}
}
```

(Formatted here for reading; the station sends each message as compact JSON.) Each
sensor's page in [`sensors/`](sensors/) lists its keys.

**Values and meta.** A reading's keys are in two sections:

| Section | Holds | Examples |
|---|---|---|
| `values` | What the sensor is there to measure: the readings to chart and store | `co2_ppm`, `pressure_hpa`, `aqi` |
| `meta` | How the reading was made, or how far to trust it: compensation inputs, status flags, calibration parameters | the SCD41's compensation pressure, the ENS160's `validity` |

- **The same key can appear in both**, with different meanings. `pressure_hpa` in the
  barometer's `values` is a measured pressure; in the CO2 sensor's `meta` it's the
  pressure its reading was compensated with. Keep the section as part of the key.
- **`meta` is left out** when a reading has none (e.g. `cpu`, `health`).
- **Keys follow the same rules in both:** `<quantity>_<unit>`, or no suffix for text
  codes and flags. Flags are text (e.g. `"normal"`, `"alt"`) or JSON booleans, never
  numeric codes.

**Telling sensors apart.** Several sensors can report the same value key, such as
`temperature_c`. Each reading says where it came from in two fields:

| Field | Identifies | Examples |
|---|---|---|
| `source` | The data series: the sensor's configured name, unique per station. Also the last topic level | `air-quality`, `outdoor`, `mqboard-mq4`, `health` |
| `sensor_type` | The hardware that produced the reading: the sensor's driver type | `ens160_aht21`, `cpu_temperature`, `mq_board` (MQ gas board), `station` (health report) |

- **Key data by `(station, source)` plus the section and key.** `pi-01 / co2 / values /
  temperature_c` and `pi-01 / outdoor / values / temperature_c` are separate series.
- **Use `sensor_type` for provenance.** It tells you which hardware, and so which
  documentation, a reading comes from.
- **A swapped part keeps its series.** If a sensor is replaced by a different model,
  `source` stays the same and `sensor_type` changes from that reading on.

**Timestamps.** `ts` is the UTC capture time, not the send time, so buffered data keeps
its true timestamp. Delivery is **at-least-once** (QoS 1), which means a reading can
arrive twice around a reconnect. Consumers should de-duplicate on `(station, source, ts)`.

`ts_src` says where `ts` came from, and so how far it can be trusted (see
[Timestamps Without a Real-Time Clock](timekeeping.md)):

| `ts_src` | Meaning | Accuracy |
|---|---|---|
| `ntp` | Captured while the clock was NTP-synchronised | NTP accuracy |
| `corrected` | Captured before sync and back-dated once sync was reached | Within seconds over a day offline (crystal drift) |
| `estimated` | Could not be corrected; `ts` is the unsynchronised system clock | Unreliable. The payload also carries `boot_id` and `boot_ts` |
