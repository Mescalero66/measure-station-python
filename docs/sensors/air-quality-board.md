# Air Quality Board (ENS160 + AHT21)

A combo board with a ScioSense ENS160 air-quality sensor and an ASAIR AHT21
temperature/humidity sensor on one I2C bus. The ENS160 does the air-quality sensing.
The AHT21 measures the temperature and humidity at the ENS160, which it needs to
compensate its readings.

- **Driver:** `src/measure_station/drivers/ens160_aht21.py`, a verbatim copy of
  `Air-Quality-ENS160-AHT21-python/ens160_aht21.py` from drivers-repo. Its README covers
  the chips in detail.
- **Station adapter:** `src/measure_station/sensors/ens160_aht21_i2c.py`.

**Wiring**

| Board | Pi header pin |
|---|---|
| VIN | 1 (3.3 V) |
| GND | 6 (GND) |
| SDA | 3 (GPIO2, SDA1) |
| SCL | 5 (GPIO3, SCL1) |
| ADD, CS, INT | Not connected |

- **Power it from 3.3 V, not 5 V.** If the board's I2C pull-ups go to VIN, 5 V on VIN
  would put 5 V on the Pi's GPIO pins.
- **Leave CS unconnected (high).** Pulling it low switches the ENS160 to SPI until the
  next power cycle.
- **Check both chips are on the bus:** `i2cdetect -y 1` should show `38` (AHT21) and
  `53` (ENS160; `52` if ADD is low).
- **Bus speed:** the Pi's default 100 kHz suits both chips. Never set it above 400 kHz,
  the AHT21's limit.

**What Gets Published**
One reading every `interval_s` (60 s by default), on
`measure-station/<station>/air-quality`:

```json
{
  "station": "pi-01",
  "source": "air-quality",
  "sensor_type": "ens160_aht21",
  "ts": "2026-10-21T09:40:00.000+00:00",
  "ts_src": "ntp",
  "values": {
    "aqi": 2,
    "tvoc_ppb": 145,
    "eco2_ppm": 612
  },
  "meta": {
    "validity": "normal",
    "temperature_c": 27.84,
    "humidity_pct": 38.2
  }
}
```

(Formatted here for reading; the station sends it as compact JSON, about 245 bytes.)

| Key | Section | From | Meaning |
|---|---|---|---|
| `aqi` | `values` | ENS160 | Air quality index, UBA scale: 1 excellent, 2 good, 3 moderate, 4 poor, 5 unhealthy |
| `tvoc_ppb` | `values` | ENS160 | Total volatile organic compounds, 0–65000 ppb |
| `eco2_ppm` | `values` | ENS160 | Equivalent CO2, 400–65000 ppm. Estimated from the VOCs, not measured CO2 |
| `validity` | `meta` | ENS160 | The ENS160's own data quality flag: see below |
| `temperature_c` | `meta` | AHT21 | Temperature on the board, in °C: the ENS160's compensation input |
| `humidity_pct` | `meta` | AHT21 | Relative humidity on the board, in %: the ENS160's compensation input |

**Interpreting the Values**

- **Check `validity` first.** Trust the `values` only when it is `"normal"`. Readings
  are published whatever their validity, so consumers can see the warm-up and filter it
  out themselves.

  | `validity` | Meaning |
  |---|---|
  | `"normal"` | Normal operation |
  | `"warmup"` | The first 3 minutes after every start of the ENS160: every service start, Pi reboot and recovery from an error (see below) |
  | `"startup"` | Initial start-up: the first hour of operation of a brand-new sensor |
  | `"invalid"` | The ENS160 reports its output as invalid |

- **`eco2_ppm` is not a CO2 measurement.** The ENS160 estimates it from VOCs, so it
  won't match a true (NDIR) CO2 sensor.
- **`meta`'s `temperature_c` and `humidity_pct` aren't room conditions.** They are
  measured next to the ENS160's heater, so they read warmer and drier than the room.
  They're published because they're the compensation the ENS160 used for this reading.
  For the air's humidity, use the [Humidity Sensor](humidity-sensor.md).

**How Readings Are Handled**

- **Each reading:** the AHT21 measures, and its values are written to the ENS160 as
  compensation. Then AQI, TVOC and eCO2 are read together, so all three come from the
  same sample.
- **Self-check.** Before each reading, the station checks that the ENS160 is still in
  STANDARD (sensing) mode and isn't reporting an error. A brown-out, for example, resets
  it to deep sleep, where its data stops updating. If the check fails, the reading fails
  instead of publishing stale values. The station then closes and re-opens the sensor,
  which restarts it.
- **Faults.** If the board doesn't answer (unplugged, loose wire), the log shows
  `Task 'sensor:air-quality' crashed` with the reason, and the station retries with
  backoff, from 1 s up to 60 s. No readings are published while it's down, so the data
  has a gap rather than made-up values.
- **Every (re)start restarts the warm-up**, so expect `validity: "warmup"` for 3 minutes after
  any recovery.

**Settings**
The `air-quality` entry in `SENSORS` in `sysconfig.py`:

| Option | Default | Meaning |
|---|---|---|
| `interval_s` | 60 | Seconds between readings |
| `i2c_bus` | 1 | I2C bus number (1 is GPIO2/GPIO3 on the header) |
| `ens160_address` | `None` | `0x53` (ADD high) or `0x52` (ADD low). `None` finds it on the bus |

It needs `smbus2`, installed as the `ens160_aht21` extra. The installer includes it by
default.

**Updating the Driver**
Make changes in drivers-repo, then copy `ens160_aht21.py` into
`src/measure_station/drivers/` unchanged.
