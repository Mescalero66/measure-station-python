# Light Sensor (BH1750)

A ROHM BH1750 ambient light sensor on I2C, e.g. the Adafruit 4681 or GY-302 board. It
measures illuminance in lux, with a spectral response close to the human eye.

- **Driver:** `src/measure_station/drivers/bh1750.py`, a verbatim copy of
  `Lux-BH1750-python/bh1750.py` from drivers-repo. Its README covers the chip in detail.
- **Station adapter:** `src/measure_station/sensors/bh1750_i2c.py`.

**Wiring**

| BH1750 board | Pi header pin |
|---|---|
| VIN / VCC | 1 (3.3 V) |
| GND | 6 (GND) |
| SCL | 5 (GPIO3, SCL1) |
| SDA | 3 (GPIO2, SDA1) |
| ADDR | Open or GND for `0x23`, 3.3 V for `0x5C` |

- **A STEMMA QT / Qwiic cable** works instead on the Adafruit board.
- **Check it's on the bus:** `i2cdetect -y 1` should show `23` (or `5c`).
- **It can share the bus** with the other I2C sensors: none of their addresses overlap.
- **Mount it facing the light you want to measure.** Shade, reflections or a dirty cover
  change the reading.

**What Gets Published**
One reading every `interval_s` (60 s by default), on `measure-station/<station>/light`:

```json
{
  "station": "pi-01",
  "source": "light",
  "sensor_type": "bh1750",
  "ts": "2026-10-21T09:40:00.000+00:00",
  "ts_src": "ntp",
  "values": {
    "illuminance_lux": 166.64
  },
  "meta": {
    "range": "lo"
  }
}
```

(Formatted here for reading; the station sends it as compact JSON, about 170 bytes.)

| Key | Section | Meaning |
|---|---|---|
| `illuminance_lux` | `values` | Illuminance outside the cover, in lux: about 0.1 to 121000 lx (more with a cover, see `transmittance`). Brighter light reads as the maximum |
| `range` | `meta` | Which measurement range the reading came from: see below |

| `range` | Step | Used | Meaning |
|---|---|---|---|
| `"hi"` | about 1.9 lx | 5000 lx and above | Daylight |
| `"lo"` | about 0.11 lx | below 5000 lx | Overcast days, dusk, dawn, night |

(Steps and maximums are without a cover. Behind one they're divided by `transmittance`:
at 0.9, about 2.1 lx and 0.13 lx steps, up to about 135000 lx. The 5000 lx switch point
applies to the published value.)

**How Auto-Ranging Works**

The chip counts light up to 65535, so it can't have fine steps and a high maximum at
the same time. Each reading therefore takes one or two measurements:

1. **Hi range first:** the shortest exposure (MTreg 31). Enough for full sun.
2. **If that reads below 5000 lx, the lo range:** the longest exposure (MTreg 254) and
   finer resolution, for steps of about 0.11 lx. Its maximum is about 7400 lx, so 5000 lx
   leaves a margin.
3. **If the light jumped during the 0.7 s in between** (e.g. the sun came out) and the
   lo measurement is full scale, the hi reading is kept.

A hi reading takes about 0.1 s; a lo one about 0.75 s. After each measurement the
chip powers itself down until the next.

**Interpreting the Values**

- **The lowest it can see is about 0.1 lx.** Full moonlight (0.1–0.3 lx) is visible; a
  moonless night reads 0. Darker skies need a different sensor (e.g. a TSL2591).
- **Accuracy is ±20 % between parts** unless calibrated: the chip's sensitivity varies
  from 0.96 to 1.44 counts per lux, and the driver uses the typical 1.2.
- **The cover is estimated.** `transmittance` 0.9 is typical for clear plastic, but
  isn't measured, and dirt or UV yellowing lowers it over time. Readings are only as
  accurate as that estimate (and the ±20 % above).

**Faults**
The chip has no ID register, so the station can only check that it acknowledges
commands. If it doesn't answer, the station retries with backoff, as for any sensor.
A power glitch resets the chip's exposure setting, so the station sends it again before
every measurement.

**Settings**
The `light` entry in `SENSORS` in `sysconfig.py`:

| Option | Default | Meaning |
|---|---|---|
| `interval_s` | 60 | Seconds between readings |
| `i2c_bus` | 1 | I2C bus number (1 is GPIO2/GPIO3 on the header) |
| `address` | `0x23` | `0x23` (ADDR open or low) or `0x5C` (ADDR high) |
| `transmittance` | 0.9 | The fraction of light its cover lets through (0–1). Readings are divided by it. 1.0 means no cover |

**Calibrating `transmittance`**, if you get access to a lux meter: on an overcast day,
hold the meter next to the sensor and set `transmittance` to (`illuminance_lux` × the
current `transmittance` ÷ the meter's reading). This also corrects the chip's own ±20 %.

It needs `smbus2`, installed as the `bh1750` extra. The installer includes it by default.

**Updating the Driver**
Make changes in drivers-repo, then copy `bh1750.py` into `src/measure_station/drivers/`
unchanged.
