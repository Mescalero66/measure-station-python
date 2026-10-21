# CO2 Sensor (SCD41)

A Sensirion SCD41 on I2C, e.g. the Adafruit SCD-41 board. It measures CO2 directly
(photoacoustic NDIR), plus temperature and humidity.

- **Driver:** `src/measure_station/drivers/scd41.py`, a verbatim copy of
  `CO2-SCD41-python/scd41.py` from drivers-repo. Its README covers the sensor in
  detail.
- **Station adapter:** `src/measure_station/sensors/scd41_i2c.py`.

**Wiring**

| SCD-41 board | Pi header pin |
|---|---|
| VIN | 1 (3.3 V) |
| GND | 6 (GND) |
| SCL | 5 (GPIO3, SCL1) |
| SDA | 3 (GPIO2, SDA1) |

- **A STEMMA QT / Qwiic cable** works instead, to the same pins.
- **Check it's on the bus:** `i2cdetect -y 1` should show `62`. The address is fixed.
- **It can share the bus** with the [Air Quality Board](air-quality-board.md) and the
  [Pressure Sensor](pressure-sensor.md): none of their addresses overlap.

**What Gets Published**
One reading every `interval_s` (60 s by default), on `measure-station/<station>/co2`:

```json
{
  "station": "pi-01",
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

(Formatted here for reading; the station sends it as compact JSON, about 220 bytes.)

| Key | Section | Meaning |
|---|---|---|
| `co2_ppm` | `values` | CO2 concentration, measured. Specified for 400–5000 ppm; accuracy ±(50 ppm + 2.5 %) up to 1000 ppm |
| `temperature_c` | `values` | Temperature at the sensor, in °C, after its temperature offset |
| `humidity_pct` | `values` | Relative humidity at the sensor, in % |
| `pressure_hpa` | `meta` | The ambient pressure the sensor used to compensate this CO2 reading, in whole hPa (the sensor's resolution), 700–1200. Not a pressure measurement |
| `comp_src` | `meta` | Where that pressure came from: see below |

| `comp_src` | Meaning |
|---|---|
| `"hpa"` | **Live:** the latest `pressure_hpa` from the `pressure_source` sensor (the [Pressure Sensor](pressure-sensor.md)) |
| `"alt"` | **Altitude:** the standard-atmosphere pressure at `altitude_m`. The fallback, used when there's no `pressure_source`, or its value is missing, out of range or out of date |

**Interpreting the Values**

- **`co2_ppm` is a real CO2 measurement.** The [Air Quality Board](air-quality-board.md)'s
  `eco2_ppm` is an estimate from VOCs. The two are different quantities, so they have
  different keys.
- **Pressure compensation.** The sensor's CO2 reading depends on air pressure:
  uncompensated, it reads low by roughly 1 % per 100 m of altitude, and weather moves
  it by a few percent more. Live pressure (`"hpa"`) corrects both. The altitude fallback
  (`"alt"`) corrects the altitude only, so treat those readings as slightly less accurate.
- **The pressure takes effect one reading later.** A new pressure only affects the
  sensor's following measurements, so each reading reports the pressure that was in
  effect when it was measured: the one sent at the previous reading, up to
  `interval_s` earlier.
- **Self-calibration (ASC)** is on by default. It assumes the sensor sees fresh air
  (about 400 ppm) at least once a week. In a space that never gets fresh air, readings
  drift: set `asc_enabled` to `False`.
- **`temperature_c` includes an offset** (`temperature_offset_c`, 4 °C by default)
  for the sensor's own heating. It only changes the temperature and humidity readings,
  not CO2. Set it once the sensor is mounted in its final enclosure.

**How Readings Are Handled**

- **The sensor measures every 5 s** in periodic mode, the mode its accuracy and
  self-calibration are specified for. The station takes the latest result every
  `interval_s`.
- **Start-up.** The station stops any running measurement and reloads the sensor's
  stored settings. Then it applies the settings from `sysconfig.py`, sends the
  compensation pressure, and starts periodic mode. The first result takes 5 s.
- **Each reading:** the station takes the latest result, then sends the current
  compensation pressure for the next ones: live if available, otherwise the altitude's.
- **Live pressure is out of date** once the pressure sensor has missed two of its own
  readings. Until it recovers, readings use the altitude fallback.
- **Faults.** If the sensor doesn't answer, the station retries with backoff, as for
  any sensor. If no new result arrives within 7 s (e.g. it was power-cycled and has
  stopped measuring), the station restarts it.

**Settings**
The `co2` entry in `SENSORS` in `sysconfig.py`. The sensor's settings are applied in its
RAM at every start, and only when they differ from what it already has. Its EEPROM
(rated for about 2000 writes) is never written, so `sysconfig.py` is the only place to
change them, and a replacement sensor picks them up automatically.

| Option | Default | Meaning |
|---|---|---|
| `interval_s` | 60 | Seconds between readings |
| `i2c_bus` | 1 | I2C bus number (1 is GPIO2/GPIO3 on the header) |
| `pressure_source` | `"barometer"` | The sensor whose `pressure_hpa` compensates CO2 live, or `None` to use only the altitude. Checked at startup: it must be another enabled sensor |
| `altitude_m` | 0 | The station's altitude above sea level, 0–3000 m: the fallback for pressure compensation |
| `temperature_offset_c` | 4.0 | Subtracted from the temperature for the sensor's self-heating; affects temperature and humidity, not CO2 |
| `asc_enabled` | `True` | Automatic self-calibration. Needs fresh air (about 400 ppm) at least weekly |

It needs `smbus2`, installed as the `scd41` extra. The installer includes it by default.

**Updating the Driver**
Make changes in drivers-repo, then copy `scd41.py` into `src/measure_station/drivers/`
unchanged.
