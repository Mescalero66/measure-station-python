# Adding a Sensor

A sensor is a small driver class that knows how to talk to one piece of hardware and
return its measurements. Everything else (scheduling, timestamps, buffering, retries,
MQTT) is handled by the station. Adding one takes four steps:

1. [Write the driver](#1-write-the-driver) in `src/measure_station/sensors/`.
2. [Register it](#2-register-the-driver) in `SENSOR_TYPES`.
3. [Configure it](#3-configure-the-sensor) in `SENSORS` in `sysconfig.py`.
4. [Install its dependencies](#4-install-dependencies), if it needs any.

There's a complete [worked example](#worked-example-bh1750-light-sensor) at the end.

## 1. Write the Driver

### Where It Goes

Create one module per sensor type in `src/measure_station/sensors/`, alongside the
existing drivers:

```
src/measure_station/sensors/
├── __init__.py          # SENSOR_TYPES registry
├── base.py              # the Sensor base class and the polling loop
├── bme280_i2c.py        # BME280 temperature / humidity / pressure (I2C)
├── cpu_temperature.py   # Pi SoC temperature (no hardware needed)
├── simulated.py         # fake data for development
└── bh1750_i2c.py        # <- your new driver
```

- **Module name:** describe the part and bus, e.g. `bh1750_i2c.py`, `sht31_i2c.py`,
  `ds18b20_1wire.py`.
- **Don't reuse a library's name.** Never give the module the same name as the Python
  library it imports (`bme280.py` importing `bme280`). That's why the existing driver
  is `bme280_i2c.py`.
- **Class name:** the part name plus `Sensor`, e.g. `Bh1750Sensor`.

### The `Sensor` Class

Subclass `measure_station.sensors.base.Sensor` and implement these methods:

| Method | Required | When it runs | What it must do |
|---|---|---|---|
| `__init__(self, name, interval_s, **options)` | yes | at startup, **and during config validation on any machine** | Call `super().__init__(name, interval_s)` and store the options. **Never touch hardware here.** |
| `open(self)` | no | before the first read, and again after any failure | Acquire hardware: open the bus, configure the chip, load calibration. |
| `read(self)` | yes | every `interval_s` seconds | Take one measurement and return `{field_name: value}`. |
| `close(self)` | no | after a failure, and at shutdown | Release hardware. Must be safe to call more than once, or before `open()`. |

Your options are the extra keys in the sensor's `SENSORS` entry, passed to `__init__` as
keyword arguments. Give each one a sensible default, e.g. `i2c_bus=1, address=0x23`.

### Rules for Drivers

- **Blocking code is fine.** `open()`, `read()` and `close()` run in a worker thread,
  so `time.sleep()`, waiting on a bus, or a slow library call won't stall the station.
- **Don't loop or sleep between readings in `read()`.** Take one measurement and
  return. The station calls `read()` every `interval_s` on a fixed schedule.
- **Raise on failure; never return made-up values.** If the hardware doesn't respond
  or a value is implausible, raise an exception. The station logs it, calls `close()`,
  waits (backing off from 1 s up to 60 s), then calls `open()` and carries on. This
  recovers from loose wires and bus glitches without a restart.
- **Return `None` for a single missing value** when the rest of the reading is still
  good. It's published as `null`.
- **Stay within `SENSOR_READ_TIMEOUT_S`** (10 s). `open()` and each `read()` are
  abandoned and treated as failures if they take longer.
- **Import hardware libraries inside `open()`**, not at the top of the module, so the
  station (and its tests) still run on machines without them.

## Measurement Names and Units

`read()` returns a dictionary, and it is published as-is as the reading's `values`. The
keys are what consumers use to tell measurements apart, so they carry both the **type
of measurement** and the **unit**:

```
<quantity>_<unit>        e.g.  temperature_c, humidity_pct, pressure_hpa
```

- **Lowercase snake_case.** The quantity first, the unit as a suffix.
- **No suffix when there's no unit.** Text codes and true/false values have no unit, so
  they're just named for what they are, e.g. the MQ board's `gas` and
  `baseline_tracking`. A bare count takes `_count`.
- **Use the units in the table below** where they fit, so the same quantity has the
  same key whichever sensor measures it.
- **Never change a key once data has been published.** Consumers store and query by
  these names. A different quantity or unit needs a new key.
- **Round to the sensor's real precision** (e.g. `round(value, 2)`). Extra digits are
  noise, and make every message and database row larger.
- **Values must be JSON types:** `int`, `float`, `bool`, `str` or `None`. `NaN` and
  infinity are published as `null`. Anything else (e.g. `Decimal`) is converted to a
  string, so convert it yourself first.
- **Document each key** in the driver's module docstring, with its unit and range.

| Quantity | Unit | Suffix | Example key |
|---|---|---|---|
| Temperature | °C | `_c` | `temperature_c` |
| Relative humidity | % | `_pct` | `humidity_pct` |
| Pressure | hPa | `_hpa` | `pressure_hpa` |
| Gas concentration | ppm | `_ppm` | `co2_ppm` |
| Illuminance | lux | `_lux` | `illuminance_lux` |
| Voltage | V | `_v` | `supply_v` |
| Current | A | `_a` | `load_a` |
| Power | W | `_w` | `load_w` |
| Resistance | kΩ | `_kohm` | `r0_kohm` |
| Speed | m/s | `_m_s` | `wind_speed_m_s` |
| Direction | degrees | `_deg` | `wind_direction_deg` |
| Length / rainfall | mm | `_mm` | `rainfall_mm` |
| Duration | s | `_s` | `uptime_s` |
| Count | (none) | `_count` | `pulse_count` |

A sensor that measures several things returns several keys in the same reading. The
BME280 returns `temperature_c`, `humidity_pct` and `pressure_hpa` together.

Every source in the station follows this convention, so the same quantity has the same
key everywhere. The CPU sensor, the BME280 and the MQ board all publish temperature as
`temperature_c` (see the MQ board's fields in [MQ Gas Board](mq-gas-board.md)).

## 2. Register the Driver

Add the class to `SENSOR_TYPES` in `src/measure_station/sensors/__init__.py`:

```python
from measure_station.sensors.bh1750_i2c import Bh1750Sensor

SENSOR_TYPES: dict[str, type[Sensor]] = {
    "bh1750": Bh1750Sensor,
    "bme280": Bme280Sensor,
    "cpu_temperature": CpuTemperatureSensor,
    "simulated": SimulatedSensor,
}
```

The key (`"bh1750"`) is the `type` you'll use in `sysconfig.py`. Use the part name, in
lowercase.

## 3. Configure the Sensor

Add an entry to `SENSORS` in `src/measure_station/sysconfig.py`:

```python
SENSORS: list[dict[str, Any]] = [
    # ...existing sensors...
    {
        "type": "bh1750",  # the SENSOR_TYPES key
        "name": "ambient-light",  # the MQTT source name (see below)
        "interval_s": 30,  # seconds between readings
        "address": 0x23,  # driver option, passed to __init__
        "enabled": True,  # False keeps the entry but doesn't start it
    },
]
```

Then restart the service: `sudo systemctl restart measure-station`.

### Naming the Sensor

`name` identifies this sensor everywhere: in the MQTT topic
(`<MQTT_BASE_TOPIC>/<STATION_ID>/<name>`), in the payload's `source` field, and in logs.

- **Allowed characters:** letters, digits, `_`, `.` and `-`. No spaces or `/`.
- **Unique:** it must differ from every other sensor, from the health source (`health`),
  and from the MQ board's sources (`mqboard-mq2`, `mqboard-mq4`...).
- **Not `status`**, which is reserved for the station's online/offline topic.
- **Name it for what it measures or where it is**, not the part number: `ambient`,
  `enclosure`, `ambient-light`. Then swapping the part for a different model doesn't
  change the topic.
- **Keep it stable.** Renaming a sensor starts a new data series for consumers.

### What's Checked at Startup

When the service starts, it builds every enabled sensor to check its configuration. If
the `type` is unknown, an option is misspelt (an unexpected keyword argument),
`interval_s` isn't positive, or the name breaks the rules above, it logs
`Configuration error: ...` and stops. It doesn't restart in a loop. Check with
`journalctl -u measure-station`.

## 4. Install Dependencies

If the driver needs a Python library, add an optional dependency group for it in
`pyproject.toml`, so the station still installs on machines without that hardware:

```toml
[project.optional-dependencies]
bh1750 = ["smbus2>=0.4"]
```

Then install it on the Pi. Either re-run the installer with the extra listed (it's safe
to re-run):

```bash
sudo EXTRAS="bme280,bh1750" bash /opt/measure-station/deploy/install.sh
```

or install it into the station's virtualenv directly:

```bash
sudo /opt/measure-station/.venv/bin/pip install -e "/opt/measure-station[bh1750]"
```

- **Prefer pure-Python libraries.** Packages that compile C code can take a very long
  time to build on a Pi 2B. Raspberry Pi OS fetches prebuilt wheels from piwheels where
  they exist.
- **Avoid heavy frameworks** (e.g. Adafruit Blinka) for a single sensor. `smbus2` for
  I2C, `pyserial` for serial and `spidev` for SPI are usually all you need.
- **Hardware access:** the installer adds the service user to the `i2c`, `dialout`,
  `gpio` and `spi` groups, so I2C buses, serial ports, GPIO and SPI devices are readable
  without root. Enable the bus itself with `raspi-config` if it isn't already (the
  installer enables I2C and the UART).

## How Readings Reach MQTT

**You don't need to do anything** beyond returning values from `read()`. For every
enabled sensor, the station runs this loop:

```
read() returns {"illuminance_lux": 412.5}
   │
   ▼
poll_sensor()          wraps it in a Reading: source = name, timestamped with
   │                   both the wall clock and the boot clock
   ▼
asyncio.Queue          in-memory hand-off, shared by all sensors
   │
   ▼
recorder               writes to the SQLite outbox (grouped, see SD card wear)
   │
   ▼
publisher              sends to the broker; deletes from the outbox only on PUBACK
   │
   ▼
measure-station/pi-01/ambient-light
{"station": "pi-01", "source": "ambient-light", "ts": "2026-10-13T09:40:01.000+00:00",
 "ts_quality": "ntp", "values": {"illuminance_lux": 412.5}}
```

Because every reading goes through the outbox, the new sensor automatically gets
everything the rest of the station has:

- its data survives network outages and station restarts;
- it's timestamped correctly even before the clock has synced (see
  [Timestamps Without a Real-Time Clock](timekeeping.md));
- failures are retried with backoff;
- a hung read is detected by the watchdog.

**Don't** publish to MQTT or write to the database from a driver. Doing so would bypass
all of that.

`ts` is the time `read()` returned. If your sensor takes a long time to measure, and the
exact moment matters, start the conversion in `open()` (as continuous-mode sensors
allow) so `read()` returns quickly with the latest value.

## Worked Example: BH1750 Light Sensor

The BH1750 is an I2C ambient light sensor (address `0x23`, or `0x5C` with its ADDR pin
high). This driver puts it in continuous high-resolution mode and reads illuminance in
lux.

`src/measure_station/sensors/bh1750_i2c.py`:

```python
"""ROHM BH1750 ambient light sensor on I2C.

Values:
    illuminance_lux: illuminance in lux, 0-65535 (1 lux resolution in this mode).

Requires: pip install -e ".[bh1750]"   (smbus2)
Wiring:   SDA -> GPIO2 (pin 3), SCL -> GPIO3 (pin 5), VCC -> 3.3 V, GND.
"""

from __future__ import annotations

import time
from typing import Any

from measure_station.sensors.base import Sensor

_POWER_ON = 0x01
_CONTINUOUS_HIGH_RES = 0x10  # 1 lux resolution, ~120 ms per measurement


def lux_from_raw(high: int, low: int) -> float:
    """Convert the sensor's two result bytes to lux (datasheet: count / 1.2)."""
    return round(((high << 8) | low) / 1.2, 1)


class Bh1750Sensor(Sensor):
    def __init__(self, name: str, interval_s: float, i2c_bus: int = 1, address: int = 0x23):
        super().__init__(name, interval_s)  # no hardware access here
        self._i2c_bus = i2c_bus
        self._address = address
        self._bus: Any = None

    def open(self) -> None:
        import smbus2  # imported here so the station runs without it installed

        self._bus = smbus2.SMBus(self._i2c_bus)
        self._bus.write_byte(self._address, _POWER_ON)
        self._bus.write_byte(self._address, _CONTINUOUS_HIGH_RES)
        time.sleep(0.2)  # let the first measurement complete

    def read(self) -> dict[str, float]:
        import smbus2

        message = smbus2.i2c_msg.read(self._address, 2)
        self._bus.i2c_rdwr(message)  # raises OSError if the sensor doesn't answer
        high, low = list(message)
        return {"illuminance_lux": lux_from_raw(high, low)}

    def close(self) -> None:
        if self._bus is not None:
            self._bus.close()
            self._bus = None
```

Then:
- **Register it:** add `"bh1750": Bh1750Sensor` to `SENSOR_TYPES`.
- **Configure it:** add the `SENSORS` entry shown in
  [Configure the Sensor](#3-configure-the-sensor).
- **Dependency:** add the `bh1750` extra to `pyproject.toml`.

## Testing

**Unit tests**, in `tests/test_<module>.py`, run on any machine: no hardware needed.
Test the conversion logic directly, and check that the configuration is accepted:

```python
from measure_station.sensors import create_sensor
from measure_station.sensors.bh1750_i2c import lux_from_raw


def test_lux_conversion() -> None:
    assert lux_from_raw(0x01, 0x90) == 333.3  # 400 counts / 1.2


def test_config_creates_sensor() -> None:
    sensor = create_sensor({"type": "bh1750", "name": "ambient-light", "interval_s": 30})
    assert sensor.name == "ambient-light"
```

Run them with `.venv/bin/pytest`. Keeping hardware access in `open()`/`read()`, and the
maths in plain functions, is what makes this possible.

**On the Pi:**

1. Check the device is visible on the bus: `i2cdetect -y 1` should show its address.
2. Restart the service and watch the log:

   ```bash
   sudo systemctl restart measure-station
   journalctl -u measure-station -f
   ```

   Look for `Sensor Bh1750Sensor(name='ambient-light', ...) started`. A
   `Task 'sensor:ambient-light' crashed` traceback means `open()` or `read()` raised; the
   message says why, and the station retries on its own.
3. Watch the messages arrive at the broker:
   `mosquitto_sub -h <broker> -t 'measure-station/+/ambient-light' -v`.

## Checklist

- [ ] Driver module in `src/measure_station/sensors/`, not named after its library.
- [ ] `__init__` only stores settings. Hardware access is in `open()`, library imports
      too.
- [ ] `read()` takes one measurement, returns `<quantity>_<unit>` keys with rounded
      values, and raises on failure.
- [ ] `close()` is safe to call twice.
- [ ] Value keys, units and ranges documented in the module docstring.
- [ ] Registered in `SENSOR_TYPES`.
- [ ] `SENSORS` entry with a unique, descriptive `name` and a sensible `interval_s`.
- [ ] Optional dependency group in `pyproject.toml`, if it needs a library.
- [ ] Unit tests for the conversion logic and the config entry.
- [ ] Seen working on the Pi: log line, then messages at the broker.
