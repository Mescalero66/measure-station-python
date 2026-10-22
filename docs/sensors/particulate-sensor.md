# Particulate Sensor (SEN62)

A Sensirion SEN62 (SEN62-SIN-T) on I2C. It measures particulate matter (dust, smoke,
pollen, haze) by laser scattering, with a fan drawing air through it, plus temperature
and humidity from a built-in SHT4x.

- **Driver:** `src/measure_station/drivers/sen62.py`, a verbatim copy of
  `Particulates-SEN62-python/sen62.py` from drivers-repo. Its README covers the sensor in
  detail; the datasheet is the Sensirion SEN6x datasheet (v1.0, October 2026).
- **Station adapter:** `src/measure_station/sensors/sen62_i2c.py`.

**Wiring**
The sensor has a 6-pin 1.25 mm connector (mating plug JST GHR-06V-S).

| SEN62 pin | Pi header pin |
|---|---|
| 1 VDD | 1 (3.3 V) |
| 2 GND | 6 (GND) |
| 3 SDA | 3 (GPIO2, SDA1) |
| 4 SCL | 5 (GPIO3, SCL1) |
| 5 GND, 6 VDD | Leave unconnected (joined to pins 2 and 1 inside the sensor) |

- **Keep the cable short:** under 10 cm is recommended, 50 cm at most.
- **Check it's on the bus:** `i2cdetect -y 1` should show `6b`. The address is fixed.
- **It can share the bus** with the [Air Quality Board](air-quality-board.md), the
  [CO2 Sensor](co2-sensor.md), the [Light Sensor](light-sensor.md) and the
  [Pressure Sensor](pressure-sensor.md): none of their addresses overlap.
- **Leave the bus at 100 kHz** (the Pi's default). The SEN62 supports standard mode only,
  so don't raise `i2c_arm_baudrate`: that would affect every sensor on the bus.
- **Power:** about 75 mA while measuring, with peaks of 130 mA, which is more than the
  other sensors combined.
- **Air must reach it.** Keep its inlet and outlet clear, and out of direct rain and
  sun. It must never see condensation (see the operating limits below).

**What Gets Published**
One reading every `interval_s` (60 s by default), on
`measure-station/<station>/particulates`:

```json
{
  "station": "pi-01",
  "source": "particulates",
  "sensor_type": "sen62",
  "ts": "2026-10-22T10:30:03.580+00:00",
  "ts_src": "ntp",
  "values": {
    "pm1_ugm3": 3.2,
    "pm25_ugm3": 4.1,
    "pm4_ugm3": 4.6,
    "pm10_ugm3": 4.9,
    "temperature_c": 18.42,
    "humidity_pct": 61.3
  }
}
```

(Formatted here for reading; the station sends it as compact JSON, about 230 bytes.)

Normally there is no `meta`. It's added only when the PM values may be wrong (about
260 bytes):

```json
  "meta": {
    "pm": "fan_speed"
  }
```

| Key | Section | Meaning |
|---|---|---|
| `pm1_ugm3` | `values` | PM1.0 mass concentration: particles 0.3–1.0 µm, in µg/m³, in steps of 0.1 |
| `pm25_ugm3` | `values` | PM2.5 (0.3–2.5 µm), likewise. Precision ±(5 µg/m³ + 5 %) up to 100 µg/m³, ±10 % above |
| `pm4_ugm3` | `values` | PM4.0 (0.3–4.0 µm), likewise. Precision ±25 µg/m³ up to 100 µg/m³ |
| `pm10_ugm3` | `values` | PM10 (0.3–10 µm), likewise. Precision ±25 µg/m³ up to 100 µg/m³ |
| `temperature_c` | `values` | Air temperature, in °C, compensated for the module's own heat. ±0.45 °C typical |
| `humidity_pct` | `values` | Relative humidity, in %, compensated likewise. ±4.5 %RH typical |
| `pm` | `meta` | Only when the PM values may be wrong: see below |

Any value the sensor reports as unknown is published as `null`.

| `pm` | Meaning |
|---|---|
| (absent) | Normal |
| `"fan_speed"` | The fan has been more than 10 % off its target speed for several seconds. PM values may be wrong. Very low or high temperatures can cause it briefly; if it persists, suspect the power supply or the fan |

**Interpreting the Values**

- **The PM classes overlap.** Each includes all the smaller particles, so
  `pm1_ugm3` ≤ `pm25_ugm3` ≤ `pm4_ugm3` ≤ `pm10_ugm3`. Particles smaller than 0.3 µm
  aren't seen at all.
- **PM2.5 is the one to compare** with air quality standards and other stations. It's
  calibrated against a TSI DustTrak DRX 8533 (ambient mode), and is the most precise.
  PM4 and PM10 are much less precise (±25 µg/m³).
- **The specified range is 0–1000 µg/m³.** It reports up to 6000 µg/m³, unspecified.
- **Humid air reads high.** Like any optical PM sensor, it counts water-swollen
  particles and fog droplets as particulates, so PM readings above about 80 %RH
  overstate the dry mass. Check `humidity_pct` before trusting a spike.
- **Operating limits.** It's specified for 10–40 °C and 20–80 %RH, and may only briefly
  go outside that, to −10–60 °C and 0–95 %RH. **It must never see condensation.**
  Outside the recommended range, temperature, humidity and PM precision drop (reversibly);
  beyond the short-term limits it can be damaged. An outdoor station needs a sheltered,
  ventilated spot for it.
- **`temperature_c` and `humidity_pct` are its best temperature and humidity.** The
  module compensates for its own fan, laser and electronics, so no offset is applied.
  If its enclosure warms it (e.g. near the Pi), a temperature offset can be added later.

**How Readings Are Handled**

- **The sensor measures continuously**, with a new result every second. It has no
  single-shot mode, and its PM values take 30 s to settle after starting, so it isn't
  switched on and off between readings. The station takes the latest result every
  `interval_s`. The module's rated lifetime is 10 years of 24-hour operation (for
  indoor use).
- **Start-up.** The station stops any running measurement and resets the sensor (the
  same as a power cycle, which clears any error flags), then starts measuring. This
  takes about 2.7 s.
- **No unsettled readings.** PM values need 30 s after the measurement starts to settle,
  so the first reading after a start waits until then: it's published about 33 s after
  the station starts the sensor, instead of about 4 s. The same applies after every
  restart (e.g. after a fault) and after fan cleaning. The status register is still
  checked at the end of that wait, so errors are still caught.
- **Each reading:** the station waits for a new result (up to 3 s), reads it, then reads
  the status register for the `pm` flag and the error flags below.
- **Errors.** If the sensor reports a fan, PM or RH&T error, its values are likely
  wrong, so nothing is published: the reading fails and the station restarts the sensor,
  which clears the flags. A one-off glitch recovers; a real fault (e.g. a blocked fan)
  keeps failing, and the log says which error it is.
- **Faults.** If the sensor doesn't answer, the station retries with backoff, as for
  any sensor. If no new result arrives within 3 s (e.g. it was power-cycled and has
  stopped measuring), the station restarts it.
- **Fan cleaning, once a day** (`fan_cleaning_interval_h`). Dust slowly builds up on the
  fan and in the airflow path, and the sensor never cleans itself. So, 24 hours after
  each start, one reading first stops the measurement, runs the fan at full speed for
  10 s to blow the dust out, restarts, and waits 30 s for the PM values to settle.
  That reading takes about 42 s instead of about 1 s, but it's still taken on schedule
  and is a normal, settled reading: no reading is missed and none is marked. The log
  notes each cleaning. The 24 hours count from the last start, so a restart postpones
  the next cleaning.
- **When the station stops**, the sensor keeps measuring (fan on) until the station
  starts again and stops it.

**Settings**
The `particulates` entry in `SENSORS` in `sysconfig.py`. The sensor stores nothing
permanently: everything is reset at each start.

| Option | Default | Meaning |
|---|---|---|
| `interval_s` | 60 | Seconds between readings |
| `i2c_bus` | 1 | I2C bus number (1 is GPIO2/GPIO3 on the header) |
| `fan_cleaning_interval_h` | 24 | Hours between fan cleanings, or `None` for never |

It needs `smbus2`, installed as the `sen62` extra. The installer includes it by default.

**Updating the Driver**
Make changes in drivers-repo, then copy `sen62.py` into `src/measure_station/drivers/`
unchanged.
