# Temperature Sensor (MCP9808)

A Microchip MCP9808 precision temperature sensor on I2C, e.g. the DFRobot SEN0435 board.
It measures air temperature to ±0.25 °C (typical), in steps of 0.0625 °C.

- **Driver:** `src/measure_station/drivers/mcp9808.py`, a verbatim copy of
  `Temp-MCP9808-python/mcp9808.py` from drivers-repo. Its README covers the chip in
  detail.
- **Station adapter:** `src/measure_station/sensors/mcp9808_i2c.py`.

**Wiring**

| SEN0435 board | Pi header pin |
|---|---|
| VCC | 1 (3.3 V) |
| GND | 6 (GND) |
| SCL | 5 (GPIO3, SCL1) |
| SDA | 3 (GPIO2, SDA1) |
| ALT | Not connected (the station doesn't use the alert output) |
| A0 / A1 / A2 | Open for `0x1F`, or tie to GND to clear that bit |

- **Power it from 3.3 V, not 5 V.** The chip runs straight from VCC, so 5 V would put
  5 V logic on the I2C lines.
- **The address** is `0x18` + 4·A2 + 2·A1 + A0, with each pin 1 when left open: `0x1F`
  with all three open, `0x18` with all three tied to GND.
- **Check it's on the bus:** `i2cdetect -y 1` should show `1f` (or the address you set).
- **It can share the bus** with the other I2C sensors: none of their addresses overlap.
- **Mount it in the shade, with air moving past it.** It measures its own temperature,
  so sun, a warm enclosure or the Pi's own heat show up in the reading. A radiation
  shield (a louvred screen) is the usual answer outdoors.

**What Gets Published**
One reading every `interval_s` (60 s by default), on
`measure-station/<station>/temperature`:

```json
{
  "station": "pi-01",
  "source": "temperature",
  "sensor_type": "mcp9808",
  "ts": "2026-10-22T09:40:00.000+00:00",
  "ts_src": "ntp",
  "values": {
    "temperature_c": 22.4375
  }
}
```

(Formatted here for reading; the station sends it as compact JSON, about 155 bytes.)

| Key | Section | Meaning |
|---|---|---|
| `temperature_c` | `values` | Air temperature at the chip, -40 to 125 °C, in steps of 0.0625 °C. ±0.25 °C typical; ±0.5 °C maximum from -20 to 100 °C, ±1 °C outside that |

There is no `meta`.

**Interpreting the Values**

- **The steps are finer than the accuracy.** Each reading is a whole number of
  sixteenths of a degree (e.g. `22.4375`), published unrounded. Changes of 0.0625 °C are
  real, which is useful for trends; the absolute value is good to about ±0.25 °C.
- **It's the most accurate air temperature on the station.** The others are the
  [Humidity Sensor](humidity-sensor.md) (±0.3 °C typical, wider below 0 °C), the
  [CO2 Sensor](co2-sensor.md) and [Particulate Sensor](particulate-sensor.md) (both
  corrected for their own heat), and the chip temperatures in the `meta` of the
  [Pressure Sensor](pressure-sensor.md) and [Air Quality Board](air-quality-board.md).

**How Readings Are Handled**

- **Each reading** wakes the chip, waits for one conversion (250 ms typical; the driver
  waits 312 ms), reads it, and shuts the chip down again. It draws 0.1 µA in between.
- **Why it sleeps:** converting continuously uses about 0.66 mW, which would warm the
  chip about 0.14 °C above the air (the datasheet's 211 °C/W for its package). Asleep
  for all but 0.3 s a minute, the warming is negligible.
- **The chip has no data-ready flag.** Its result register keeps the last value through
  shutdown, so the wait after waking is what makes each reading new.
- **Faults.** At start the station checks the chip's manufacturer and device IDs, so a
  different chip at that address is reported rather than read. If it doesn't answer,
  the station retries with backoff, as for any sensor. A power glitch resets the chip
  to its defaults: converting continuously, at the resolution the station uses anyway.
  The next reading puts it back to sleep.
- **The alert output and limits aren't used.** They're left at their power-on defaults
  (0 °C), and the driver's limit flags aren't published.

**Settings**
The `temperature` entry in `SENSORS` in `sysconfig.py`:

| Option | Default | Meaning |
|---|---|---|
| `interval_s` | 60 | Seconds between readings |
| `i2c_bus` | 1 | I2C bus number (1 is GPIO2/GPIO3 on the header) |
| `address` | `0x1F` | `0x18` to `0x1F`, set by A0–A2 (`0x1F` with all three open) |

The resolution is fixed at 0.0625 °C: at one reading a minute, its 250 ms conversion
costs nothing.

It needs `smbus2`, installed as the `mcp9808` extra. The installer includes it by
default.

**Updating the Driver**
Make changes in drivers-repo, then copy `mcp9808.py` into `src/measure_station/drivers/`
unchanged.
