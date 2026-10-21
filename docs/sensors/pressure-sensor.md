# Pressure Sensor (BMP280)

A Bosch BMP280 barometric pressure sensor on I2C, e.g. the generic 6-pin "GY-BMP280"
breakout. It measures absolute air pressure, and its own temperature for compensation.

- **Driver:** `src/measure_station/drivers/bmp280.py`, a verbatim copy of
  `Barometer-BMP280-python/bmp280.py` from drivers-repo. Its README covers the chip in
  detail.
- **Station adapter:** `src/measure_station/sensors/bmp280_i2c.py`.

**Wiring**

| BMP280 board | Pi header pin |
|---|---|
| VCC | 1 (3.3 V) |
| GND | 6 (GND) |
| SCL | 5 (GPIO3, SCL1) |
| SDA | 3 (GPIO2, SDA1) |
| CSB | Not connected (pulled up on the board, which selects I2C) |
| SDO | Not connected for `0x76`, or 3.3 V for `0x77` |

- **Power it from 3.3 V, not 5 V.** The common 6-pin board has no regulator, so 5 V
  would go straight to the chip.
- **Check it's on the bus:** `i2cdetect -y 1` should show `76` (or `77`).
- **It can share the bus** with the [Air Quality Board](air-quality-board.md): their
  addresses don't overlap.

**What Gets Published**
One reading every `interval_s` (60 s by default), on
`measure-station/<station>/barometer`:

```json
{
  "station": "pi-01",
  "source": "barometer",
  "sensor_type": "bmp280",
  "ts": "2026-10-21T09:40:00.000+00:00",
  "ts_src": "ntp",
  "values": {
    "pressure_hpa": 1006.53
  },
  "meta": {
    "temperature_c": 25.08
  }
}
```

(Formatted here for reading; the station sends it as compact JSON, about 180 bytes.)

| Key | Section | Meaning |
|---|---|---|
| `pressure_hpa` | `values` | Absolute air pressure at the station, 300–1100 hPa. Relative accuracy ±0.12 hPa, absolute ±1 hPa |
| `temperature_c` | `meta` | The chip's own temperature, -40 to 85 °C. The chip measures it itself, and the pressure calculation uses it to correct for temperature |

- **`pressure_hpa` is station pressure, not sea-level pressure (QNH).** It's lower
  than weather reports by about 1.2 hPa per 10 m of altitude. A consumer that needs
  QNH can convert it using the station's altitude.
- **The [CO2 Sensor](co2-sensor.md) uses this pressure** to compensate its readings
  (its `pressure_source`). While this sensor is down, it falls back to its altitude.
- **`meta`'s `temperature_c` reads a little above the air temperature**, because of
  self-heating on the board. Use a dedicated sensor for air temperature.

**How Readings Are Handled**

- **Each reading** triggers one measurement in forced mode, with x16 pressure and x2
  temperature oversampling (Bosch's "ultra high resolution"), which takes up to 43 ms.
  The chip sleeps between readings.
- **Faults.** If the chip doesn't answer, the station retries with backoff, as for any
  sensor. If it has been reset since the station started it (e.g. by a brown-out), it
  returns no measurement. The station then re-initialises it.
- **A BME280 also works.** Boards sold as BMP280 sometimes carry one. The driver reads
  its temperature and pressure, but not its humidity.

**Settings**
The `barometer` entry in `SENSORS` in `sysconfig.py`:

| Option | Default | Meaning |
|---|---|---|
| `interval_s` | 60 | Seconds between readings |
| `i2c_bus` | 1 | I2C bus number (1 is GPIO2/GPIO3 on the header) |
| `address` | `0x76` | `0x76` (SDO low, most boards) or `0x77` (SDO high) |

It needs `smbus2`, installed as the `bmp280` extra. The installer includes it by
default.

**Updating the Driver**
Make changes in drivers-repo, then copy `bmp280.py` into `src/measure_station/drivers/`
unchanged.
