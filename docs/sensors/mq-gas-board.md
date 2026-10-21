# MQ Gas Board

The MQ board (six MQ gas sensors on an ESP32-S3) broadcasts NMEA-style `$PMQB`
sentences, as specified in [MQ Gas Board UART Interface](../uart-interface.md). It re-sends
the latest reading of every sensor about every 10s and reads each sensor every 150s, so
most sentences are repeats.

**Wiring**
- Board TX → Pi RX (GPIO15, pin 10).
- Common ground.
- Both sides use 3.3 V logic, so no level shifter is needed.
- The board only transmits, so Pi TX is unused.
- The station reads it on `/dev/serial0` at 115200 8N1 (`MQBOARD_*` in `sysconfig.py`).
  The installer enables the UART and turns off the serial login console that would
  otherwise use it.

**What Gets Published**
One MQTT source per sensor: `<MQBOARD_SOURCE_PREFIX><sensor>`, e.g.
`measure-station/pi-01/mqboard-mq4`. Each new reading is sent once:

```json
{
  "station": "pi-01",
  "source": "mqboard-mq4",
  "sensor_type": "mq_board",
  "ts": "2026-10-13T09:40:01.000+00:00",
  "ts_src": "ntp",
  "values": {
    "concentration_ppm": 17.85
  },
  "meta": {
    "gas": "CH4",
    "temperature_c": 23.19,
    "baseline_tracking": true,
    "r0_kohm": 5.401877,
    "ts_basis": "board"
  }
}
```

(Formatted here for reading; the station sends it as compact JSON, about 260 bytes.)

The station passes the board's sentence fields through under the board's own names
(see [MQ Gas Board UART Interface](../uart-interface.md#fields) for the full
definitions). It decides only which section each field goes in, adds `ts_basis`, and
leaves out `uptime_s`:

| Key | Section | From | Meaning |
|---|---|---|---|
| `concentration_ppm` | `values` | board | Concentration of the target gas, in ppm |
| `gas` | `meta` | board | Target gas the ppm is calibrated for |
| `temperature_c` | `meta` | board | Temperature used to compensate the reading, in °C |
| `baseline_tracking` | `meta` | board | `true`: R0 is an established clean-air baseline. `false`: provisional boot calibration |
| `r0_kohm` | `meta` | board | Clean-air reference resistance this ppm was calculated with, in kΩ |
| `ts_basis` | `meta` | station | `board`: `ts` was derived from the board's uptime. `receipt`: fallback, timestamped on arrival |
| `uptime_s` | not published | board | Board uptime when the reading was taken. The station uses it internally (see below) |

`ts` (top level) is the reading's UTC time, worked out by the station from the board's
`uptime_s`.

- **Values are as the board sent them.** Empty fields become `null` (never 0).
  `nan`/`inf` also become `null`.
- **Read faults are published.** A failed read (`concentration_ppm: null`) is sent, not
  dropped, so consumers can see sensor faults.
- **The station doesn't interpret the data.** Deciding whether a reading with
  `baseline_tracking: false` is provisional, or whether `r0_kohm` = 10.0 is a placeholder, is
  left to consumers. So is recomputing ppm against another R0.
- **De-duplication key:** `(station, source, ts)`, as for every source. Each reading's
  `ts` is fixed when it's stored, so a re-delivered message has the same `ts`.

**How Readings Are Handled**

- **Repeats** are dropped in memory and never written to the SD card. Only new readings
  are stored: 6 per 150s.
- **Station restarts.** The last `uptime_s` recorded per sensor is saved in the outbox, in
  the same transaction as the reading itself. After a station restart, readings already
  recorded aren't sent again. If the station dies with readings still in memory, the
  board is still repeating them, so they're picked up again on restart (if it's back
  within about 150s).
- **Board restarts** are detected when a sensor's `uptime_s` goes backwards, or when its
  sentence turns empty.
- **Timestamps.** The board has no clock; `uptime_s` is its uptime. The station estimates
  when the board booted, as the minimum of `arrival time − uptime_s` over the last hour of
  newly seen readings, and timestamps each reading as `board boot + uptime_s`. That's within
  about a second of when the reading was actually taken. These timestamps then go
  through the station's own clock-sync correction like any other reading.
- **Held readings.** A reading already on the line when the station starts can be up
  to 150s old. It's held until a reading is seen arriving fresh (within about 25s),
  then sent with its true capture time. If no estimate arrives within `max_hold_s`, it's
  sent anyway, timestamped on arrival, with `ts_basis: "receipt"`.
- **Corrupt lines** (bad checksum, framing or field) are discarded. The board repeats
  every reading, so nothing is lost. They're counted, and summarised in the log at most
  once per minute.

**Health and Alerts**
- The health topic includes `mqboard_up`, `mqboard_stale_sensors`,
  `mqboard_bad_lines`, `mqboard_last_line_age_s` and `mqboard_held`.
- The log warns if no valid sentence arrives for 30s, if a sensor's reading stops
  changing for 300s, and when a board restart is detected.

**Settings:** `MQBOARD_*` in `sysconfig.py`. Defaults follow the board's spec.
