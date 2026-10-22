# Humidity Sensor (CHT8305)

A Sensylink CHT8305C temperature and humidity sensor in a stainless steel probe on a
1.5 m cable: the DFRobot SEN0546. It measures relative humidity to ±2 %RH and
temperature to ±0.3 °C (typical), and is the station's most accurate humidity sensor.

- **Driver:** `src/measure_station/drivers/cht8305.py`, a verbatim copy of
  `Temp-Humidity-CHT8305-python/cht8305_async.py` from drivers-repo, renamed. Its README
  covers the chip in detail.
- **Station adapter:** `src/measure_station/sensors/cht8305_i2c.py`.

**Check the probe version first.** DFRobot have shipped the SEN0546 with a CHT832X chip
since July 2025. This driver is for the CHT8305, which has a **white** SCL wire; the
CHT832X has a green one and needs a different driver. The station refuses to start a
probe whose chip reports a different version.

**Wiring**

| Probe wire | Pi header pin |
|---|---|
| Red, VCC | 1 (3.3 V) |
| Black, GND | 6 (GND) |
| White, SCL | 5 (GPIO3, SCL1) |
| Yellow, SDA | 3 (GPIO2, SDA1) |

- **Check it's on the bus:** `i2cdetect -y 1` should show `40`. The address is fixed:
  the probe doesn't wire out the address pin.
- **It can share the bus** with the other I2C sensors: none of their addresses overlap.
- **The probe adds 4.7 kΩ pull-ups and 1.5 m of cable** to the bus. If I2C errors appear
  after connecting it, the bus is the first suspect: shorten the cable, or take the
  pull-ups off another board.
- **Mount it in the shade, sheltered from rain**, with air moving past it: a radiation
  shield (a louvred screen) outdoors. Away from the Pi and the other sensors, whose heat
  would make it read warmer and drier.

**Making It Last Outdoors**

- **Point the tip down or sideways, with a drip loop in the cable**, so water runs off
  rather than into the probe or along the cable into the enclosure. Seal the cable joint
  at the Pi end (heat-shrink, not bare jumper pins).
- **Keep chemical vapours away.** Solvents, fresh paint, ammonia, chlorine cleaners and
  acetic-cure ("vinegar smell") silicone permanently shift polymer humidity sensors. Seal
  anything near it with neutral-cure silicone, cured before the probe goes in.
- **Keep it off cold surfaces that drip**, and out of the enclosure's warm exhaust.
- **Check it once a year with the salt test:** seal the probe in a container over a damp
  slurry of table salt for 12–24 hours at room temperature. It should read about
  75 %RH; a bigger difference is drift.

**What Gets Published**
One reading every `interval_s` (60 s by default), on
`measure-station/<station>/humidity`:

```json
{
  "station": "pi-01",
  "source": "humidity",
  "sensor_type": "cht8305",
  "ts": "2026-10-22T09:40:00.000+00:00",
  "ts_src": "ntp",
  "values": {
    "temperature_c": 18.41,
    "humidity_pct": 61.3
  }
}
```

(Formatted here for reading; the station sends it as compact JSON, about 170 bytes.)

| Key | Section | Meaning |
|---|---|---|
| `temperature_c` | `values` | Air temperature at the probe, -40 to 125 °C, to 0.01 °C. ±0.3 °C typical (±0.5 °C maximum) from 0 to 50 °C; ±0.5 °C typical (±1 °C maximum) from -20 to 85 °C |
| `humidity_pct` | `values` | Relative humidity, 0–100 %, to 0.1 %. ±2 %RH typical (±3 %RH maximum) at 50 %RH; ±3 %RH typical (±5 %RH maximum) from 20 to 80 %RH |

There is no `meta`.

**Interpreting the Values**

- **The two values are a pair.** Relative humidity depends on the temperature it's
  measured at, so calculate dew point or absolute humidity from this sensor's own
  `temperature_c`, not another sensor's.
- **For temperature alone, the [Temperature Sensor](temperature-sensor.md) is more
  accurate**, especially below 0 °C, where this one is specified to ±0.5 °C typical.
- **The humidity figures leave out hysteresis and drift.** Readings can differ by up to
  ±1 %RH depending on whether the humidity was rising or falling, and humidity sensors
  drift over time, more so after long spells near 100 %.
- **In fog or after rain it can stick near 100 %** until the probe dries out. The daily
  heater cycle (below) helps it dry.

**How Readings Are Handled**

- **Each reading** starts one temperature and humidity conversion, about 11 ms. The
  sensor then idles at 0.05 µA until the next one, so it doesn't warm itself (the
  datasheet asks for no more than one reading a second).
- **There is no data-ready flag.** The sensor ignores reads until the conversion has
  finished, so the driver waits 12 ms and then retries every 2 ms. If it still hasn't
  finished after about 0.1 s, the reading fails.
- **At start** the station checks the manufacturer and version IDs, then soft-resets the
  sensor. The defaults are the settings the station uses: temperature and humidity in
  one conversion, 14-bit, heater off (it's turned on only for the heater cycle below).
- **Faults.** If the sensor doesn't answer, the station retries with backoff, as for any
  sensor. A power glitch resets it to those same defaults, so nothing needs sending
  again.

**The Heater Cycle**

Humidity sensors slowly build up an offset in damp air, and warming them drives the
moisture out (datasheet §1.3). Every `heater_interval_h` (24 h by default, counted from
when the station starts the sensor):

1. **A reading is published as usual**, then the heater turns on (about 7 mA).
2. **It runs until the next reading**, one `interval_s` later (60 s by default).
3. **That reading measures once more** (to see how much the heater warmed the probe),
   turns the heater off, and then measures every 2 s until the temperature is back
   within 0.1 °C of the reading before heating. Only that measurement is published, so
   no reading is published warm. If it takes more than 40 s, the last one is published
   anyway: the air itself may have warmed meanwhile.

No reading is missed or marked; the one after heating just takes longer (up to about
42 s). Each cycle logs one line to the journal, e.g.
`humidity: heater cycle warmed the probe 2.31 °C; cooled after 14 s`.

**Check that line on the Pi after the first day.** The datasheet doesn't say how much
the heater warms the chip. If the log reports almost no warming, the heater may only
run during measurements (as on TI's HDC1080, whose registers this chip copies); say so
and the cycle can be changed to measure while heating. If it often says "published
anyway", the probe cools slowly and the 40 s limit needs raising.

If the station is stopped during a cycle, the heater stays on until the next start,
whose reset turns it off.

**Settings**
The `humidity` entry in `SENSORS` in `sysconfig.py`:

| Option | Default | Meaning |
|---|---|---|
| `interval_s` | 60 | Seconds between readings |
| `i2c_bus` | 1 | I2C bus number (1 is GPIO2/GPIO3 on the header) |
| `heater_interval_h` | 24 | Hours between heater cycles; `None` turns them off |

It needs `smbus2`, installed as the `cht8305` extra. The installer includes it by
default.

**Updating the Driver**
Make changes in drivers-repo, then copy `cht8305_async.py` into
`src/measure_station/drivers/` as `cht8305.py`, otherwise unchanged.
