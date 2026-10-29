# Adding a Sensor

A sensor is a small driver class that knows how to talk to one piece of hardware and
return its measurements. Everything else (scheduling, timestamps, buffering, retries,
MQTT) is handled by the station. Adding one takes five steps:

1. [Write the driver](#1-write-the-driver) in `src/measure_station/sensors/`.
2. [Register it](#2-register-the-driver) in `SENSOR_TYPES`.
3. [Configure it](#3-configure-the-sensor) in `SENSORS` in `sysconfig.py`.
4. [Install its dependencies](#4-install-dependencies), if it needs any.
5. [Document it](#5-document-the-sensor) in `docs/sensors/`.

There's a complete [worked example](#worked-example-bh1750-light-sensor) at the end.

## 1. Write the Driver

### Where It Goes

Create one module per sensor type in `src/measure_station/sensors/`, alongside the
existing drivers:

```
src/measure_station/sensors/
├── __init__.py          # SENSOR_TYPES registry
├── base.py              # the Sensor base class and the polling loop
├── bh1750_i2c.py        # BH1750 light sensor (the worked example below)
├── bmp280_i2c.py        # BMP280 pressure sensor (adapter for an asyncio driver)
├── cht8305_i2c.py       # CHT8305 temperature and humidity probe (adapter for an asyncio driver)
├── cpu_temperature.py   # Pi SoC temperature (no hardware needed)
├── ens160_aht21_i2c.py  # ENS160 + AHT21 air quality board (adapter, see below)
├── mcp9808_i2c.py       # MCP9808 temperature sensor (adapter for an asyncio driver)
├── scd41_i2c.py         # SCD41 CO2 sensor (adapter for an asyncio driver)
├── sen6x_i2c.py         # SEN62 or SEN65 particulate matter sensor (adapter for an asyncio driver)
├── simulated.py         # fake data for development
└── sht31_i2c.py         # <- your new driver
```

- **Module name:** describe the part and bus, e.g. `bh1750_i2c.py`, `sht31_i2c.py`,
  `ds18b20_1wire.py`.
- **Don't reuse a library's name.** Never give the module the same name as the Python
  library it imports (`bme280.py` importing `bme280`): the module would shadow the
  library. Adding the bus avoids this (`bme280_i2c.py`).
- **Class name:** the part name plus `Sensor`, e.g. `Bh1750Sensor`.

### Using a Driver From drivers-repo

If the chip already has a driver in drivers-repo, don't rewrite it. Use it unchanged,
behind a small adapter:

1. **Copy the driver verbatim** into `src/measure_station/drivers/`. Don't edit or
   reformat it there: make changes in drivers-repo and copy them over, so the two stay
   identical. Ruff skips this folder for that reason.
2. **Write the adapter** in `src/measure_station/sensors/`, as described below. Its
   `open()` imports the driver (`from measure_station.drivers.<module> import ...`) and
   starts the chip. Its `read()` calls the driver and renames the results to the
   station's [value keys](#measurement-names-and-units).

The driver must be imported inside `open()`, never at the top of the adapter. Drivers
import their hardware libraries (e.g. `smbus2`) at the top, and the adapter is imported
on every machine, including during config validation and tests.

If the driver is `asyncio`-based (its methods are coroutines), the adapter's `open()` and
`read()` still block: run each coroutine with `asyncio.run()`. They run in a worker
thread, so this gives each call its own short-lived event loop there, and the driver's
blocking bus transfers stay off the station's event loop. `bmp280_i2c.py` is the
example. Two things to watch, both handled in `scd41_i2c.py`:

- **`close()` runs on the station's event loop thread**, where `asyncio.run()` raises.
  Keep it synchronous: just close the bus.
- **If the first result takes time** after `open()` starts the sensor, wait for it in
  `read()`, within `SENSOR_READ_TIMEOUT_S`. Raising instead would re-open the sensor,
  which restarts the wait, and so on forever.

`ens160_aht21_i2c.py` is the reference example. Its tests (`tests/test_ens160_aht21.py`)
replace the driver with a fake board, so they run without hardware.

### The `Sensor` Class

Subclass `measure_station.sensors.base.Sensor` and implement these methods:

| Method | Required | When it runs | What it must do |
|---|---|---|---|
| `__init__(self, name, interval_s, **options)` | yes | at startup, **and during config validation on any machine** | Call `super().__init__(name, interval_s)` and store the options. **Never touch hardware here.** |
| `open(self)` | no | before the first read, and again after any failure | Acquire hardware: open the bus, configure the chip, load calibration. |
| `read(self)` | yes | every `interval_s` seconds | Take one measurement and return a `Sample(values, meta)` (see [Values and Meta](#values-and-meta)). |
| `close(self)` | no | after a failure (including a failed `open()`), and at shutdown | Release hardware. Must be safe to call more than once, or before `open()`. |

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
  abandoned and treated as failures if they take longer. A sensor whose next `read()`
  does slow maintenance can allow itself more time for that read by overriding
  `read_timeout_s()` (the SEN6x's daily fan cleaning, in `sen6x_i2c.py`).
- **Import hardware libraries inside `open()`**, not at the top of the module, so the
  station (and its tests) still run on machines without them.

### Using Another Sensor's Values

A sensor can use another sensor's latest readings, e.g. for compensation. The SCD41
uses the barometer's pressure this way (`scd41_i2c.py`):

- **Read them** with `self.latest_values.get(source, key)`, for a key in that sensor's
  `values`. It returns `None` if that sensor hasn't produced the key yet, or has missed two of its own readings. Always
  have a fallback. `latest_values` is also `None` when the sensor runs on its own (e.g.
  in `read_sensor`).
- **Declare the source** by overriding `uses_sources()` to return its name. Startup then
  checks that it's another enabled sensor, so a misspelt name is a configuration error.
- **Publish what you used** in the reading's `meta`, so consumers can tell how each
  reading was made.

## Values and Meta

`read()` returns a `Sample` (from `measure_station.sensors`) with two dictionaries,
published as-is as the reading's `values` and `meta`:

```python
return Sample(
    values={"pressure_hpa": 1006.53},  # what the sensor is there to measure
    meta={"temperature_c": 25.08},  # how it was measured, or how far to trust it
)
```

- **`values`:** the readings consumers chart and store.
- **`meta`:** compensation inputs, status and quality flags, calibration parameters.
  Optional: `Sample({...})` has none, and an empty `meta` isn't published.
- **Flags are text or booleans, never numeric codes**, e.g. `"warmup"` rather than
  `1`, so a message can be read without a code table.

## Measurement Names and Units

The keys in `values` and `meta` are what consumers use to tell measurements apart, so
they carry both the **type of measurement** and the **unit**:

```
<quantity>_<unit>        e.g.  temperature_c, humidity_pct, pressure_hpa
```

- **Lowercase snake_case.** The quantity first, the unit as a suffix.
- **No suffix when there's no unit.** Text codes, status codes, indexes and true/false
  values have no unit, so they're just named for what they are, e.g. the MQ board's
  `gas` and `baseline_tracking`, and the air quality board's `aqi` and `validity`. A
  bare count takes `_count`.
- **Use the units in the table below** where they fit, so the same quantity has the
  same key whichever sensor measures it.
- **Never change a key once data has been published.** Consumers store and query by
  these names. A different quantity or unit needs a new key.
- **Round to the sensor's real precision** (e.g. `round(value, 2)`). Extra digits are
  noise, and make every message and database row larger.
- **Values must be JSON types:** `int`, `float`, `bool`, `str` or `None`. `NaN` and
  infinity are published as `null`. Anything else (e.g. `Decimal`) is converted to a
  string, so convert it yourself first.
- **Document each key** in the driver's module docstring, under `Values:` and `Meta:`,
  with its unit and range.

| Quantity | Unit | Suffix | Example key |
|---|---|---|---|
| Temperature | °C | `_c` | `temperature_c` |
| Relative humidity | % | `_pct` | `humidity_pct` |
| Pressure | hPa | `_hpa` | `pressure_hpa` |
| Gas concentration | ppm | `_ppm` | `co2_ppm` |
| Gas concentration (trace) | ppb | `_ppb` | `tvoc_ppb` |
| Mass concentration | µg/m³ | `_ugm3` | `pm25_ugm3` |
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

A sensor that measures several things returns several keys in the same reading. A
temperature, humidity and pressure sensor returns `temperature_c`, `humidity_pct` and
`pressure_hpa` together.

Every source in the station follows this convention, so the same quantity has the same
key everywhere. The CPU sensor, the air quality board and the MQ board all publish
temperature as `temperature_c` (see [Air Quality Board](sensors/air-quality-board.md) and the
MQ board's fields in [MQ Gas Board](sensors/mq-gas-board.md)). The source tells consumers
where it was measured.

## 2. Register the Driver

Add the class to `SENSOR_TYPES` in `src/measure_station/sensors/__init__.py`:

```python
from measure_station.sensors.bh1750_i2c import Bh1750Sensor

SENSOR_TYPES: dict[str, type[Sensor]] = {
    "bh1750": Bh1750Sensor,
    "cpu_temperature": CpuTemperatureSensor,
    "ens160_aht21": Ens160Aht21Sensor,
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
        "type": "bh1750",  # the SENSOR_TYPES key, published as sensor_type
        "name": "light",  # the MQTT source name (see below)
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
  `enclosure`, `light`. Then swapping the part for a different model doesn't
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

Then install it on the Pi. Either re-run the installer with the extras listed,
comma-separated (it's safe to re-run):

```bash
sudo EXTRAS="ens160_aht21,bh1750" bash /opt/measure-station/deploy/install.sh
```

or install it into the station's virtualenv directly:

```bash
sudo /opt/measure-station/.venv/bin/pip install -e "/opt/measure-station[bh1750]"
```

If the sensor is enabled in the shipped `sysconfig.py`, also add its extra to the
default `EXTRAS` in `deploy/install.sh`, so a fresh install includes it.

- **Prefer pure-Python libraries.** Packages that compile C code can take a very long
  time to build on a Pi 2B. Raspberry Pi OS fetches prebuilt wheels from piwheels where
  they exist.
- **Avoid heavy frameworks** (e.g. Adafruit Blinka) for a single sensor. `smbus2` for
  I2C, `pyserial` for serial and `spidev` for SPI are usually all you need.
- **Hardware access:** the installer adds the service user to the `i2c`, `dialout`,
  `gpio` and `spi` groups, so I2C buses, serial ports, GPIO and SPI devices are readable
  without root. Enable the bus itself with `raspi-config` if it isn't already (the
  installer enables I2C and the UART).

## 5. Document the Sensor

Give each sensor its own page in `docs/sensors/`, named for what it measures, e.g.
`docs/sensors/pressure-sensor.md`. Use an existing page, such as
[Pressure Sensor](sensors/pressure-sensor.md), as the template:

- **Wiring:** a table from the board's pins to the Pi's header pins, its supply voltage,
  and how to check it's connected (e.g. `i2cdetect -y 1`).
- **What gets published:** an example message, and a table of its value keys with their
  meanings, units and ranges.
- **How to interpret the values:** anything a consumer could get wrong, such as warm-up
  periods or a temperature measured on the board rather than in the room.
- **Settings:** its `SENSORS` entry's options and its dependency extra.

Then add it to the Hardware section of the Documentation index in `README.md`, and point
the adapter's module docstring and its `SENSORS` comment in `sysconfig.py` at the page.

## How Readings Reach MQTT

**You don't need to do anything** beyond returning values from `read()`. For every
enabled sensor, the station runs this loop:

```
read() returns Sample({"illuminance_lux": 412.5})
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
measure-station/pi-01/light
{"station": "pi-01", "source": "light", "sensor_type": "bh1750",
 "ts": "2026-10-13T09:40:01.000+00:00", "ts_src": "ntp",
 "values": {"illuminance_lux": 412.5}}
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

The BH1750 is an I2C ambient light sensor, with an `asyncio` driver from drivers-repo
(`Lux-BH1750-python/bh1750.py`). Adding it took these files; see
[Light Sensor](sensors/light-sensor.md) for the sensor itself.

| File | Change |
|---|---|
| `src/measure_station/drivers/bh1750.py` | The driver, copied verbatim |
| `src/measure_station/sensors/bh1750_i2c.py` | The adapter, below |
| `src/measure_station/sensors/__init__.py` | `"bh1750": Bh1750Sensor` in `SENSOR_TYPES` |
| `src/measure_station/sysconfig.py` | The `light` entry in `SENSORS` |
| `pyproject.toml`, `deploy/install.sh` | The `bh1750` extra, and in the installer's default `EXTRAS` |
| `tests/test_bh1750.py` | Tests against a fake chip (see [Testing](#testing)) |
| `docs/sensors/light-sensor.md` | Its page, listed in the README |

The heart of the adapter (option checks and comments left out; see the file). It
auto-ranges: it measures again, more finely, when the first reading is low:

```python
class Bh1750Sensor(Sensor):
    def __init__(self, name, interval_s, i2c_bus=1, address=0x23, transmittance=1.0):
        super().__init__(name, interval_s)  # no hardware access here
        ...

    def open(self) -> None:
        # Imported here: the driver imports smbus2 at the top of the module.
        from measure_station.drivers.bh1750 import BH1750

        self._bh = BH1750(self._i2c_bus, self._address, self._transmittance)
        asyncio.run(self._bh.power_on())  # raises if nothing answers at the address

    def read(self) -> Sample:
        lux, range_ = asyncio.run(self._auto_range())
        digits = 2 if range_ == "lo" else 1
        return Sample({"illuminance_lux": round(lux, digits)}, meta={"range": range_})

    async def _auto_range(self) -> tuple[float, str]:
        lux, _ = await self._measure(*_HI)
        if lux >= _LO_BELOW_LUX:
            return lux, "hi"
        lo_lux, lo_full_scale = await self._measure(*_LO)
        return (lux, "hi") if lo_full_scale else (lo_lux, "lo")

    async def _measure(self, resolution: int, mtreg: int) -> tuple[float, bool]:
        bh = self._bh
        await bh.power_on()
        await bh.set_mtreg(mtreg)  # every time: a brown-out would have reset it
        lux = await bh.measure(resolution)  # one-time: then the chip sleeps
        return lux, round(lux / bh.lux_per_count(resolution)) >= _FULL_SCALE

    def close(self) -> None:
        if self._bh is not None:  # synchronous: runs on the event loop thread
            self._bh.close()
            self._bh = None
```

## Testing

**Unit tests**, in `tests/test_<module>.py`, run on any machine: no hardware or
`smbus2` needed. Test the adapter with your **real driver** against a fake chip: the
helpers in `tests/conftest.py` replace `smbus2`, so each test file only describes the
chip. Model the chip from its datasheet, and check the model against the datasheet's
worked example where there is one, so expected results come from the manufacturer:

```python
from conftest import FakeMsg, install_fake_smbus2


class FakeBh1750Bus:
    """A BH1750 at 0x23: lux * 1.2 * MTreg / 69 counts (p11), clipped at 65535."""

    lux = 30000.0  # the light the test shows the chip

    def __init__(self, bus: int) -> None:
        self.mtreg, self.raw = 69, 0

    def i2c_rdwr(self, *messages: FakeMsg) -> None:
        for m in messages:
            if m.addr != 0x23:
                raise OSError(121, "Remote I/O error")  # nothing at that address
            if m.is_read:
                m.data[:] = self.raw.to_bytes(2, "big")
            else:
                ...  # track the MTreg and measurement opcodes, and set self.raw


def test_bright_light(monkeypatch) -> None:
    install_fake_smbus2(monkeypatch, FakeBh1750Bus, "bh1750")
    sensor = create_sensor({"type": "bh1750", "name": "light", "interval_s": 60})
    sensor.open()
    sample = sensor.read()
    assert sample.meta == {"range": "hi"}
    assert sample.values["illuminance_lux"] == pytest.approx(30000, abs=1.9)
```

Also test the failure paths: a missing chip, bad options, and recovery from a reset.
Run the tests with `.venv/Scripts/python -m pytest` (`.venv/bin/pytest` on Linux).

**On the Pi:**

1. Check the device is visible on the bus: `i2cdetect -y 1` should show its address.
2. Restart the service and watch the log:

   ```bash
   sudo systemctl restart measure-station
   journalctl -u measure-station -f
   ```

   Look for `Sensor Bh1750Sensor(name='light', ...) started`. A
   `Task 'sensor:light' crashed` traceback means `open()` or `read()` raised; the
   message says why, and the station retries on its own.
3. Watch the messages arrive at the broker:
   `mosquitto_sub -h <broker> -t 'measure-station/+/light' -v`.

## Checklist

- [ ] Driver module in `src/measure_station/sensors/`, not named after its library.
- [ ] A driver from drivers-repo is copied verbatim into `drivers/`, and imported only
      inside `open()`.
- [ ] `__init__` only stores settings. Hardware access is in `open()`, library imports
      too.
- [ ] `read()` takes one measurement, returns a `Sample` with `<quantity>_<unit>` keys
      and rounded values (flags and compensation inputs in `meta`), and raises on
      failure.
- [ ] `close()` is safe to call twice.
- [ ] Value keys, units and ranges documented in the module docstring.
- [ ] Registered in `SENSOR_TYPES`.
- [ ] `SENSORS` entry with a unique, descriptive `name` and a sensible `interval_s`.
- [ ] Optional dependency group in `pyproject.toml`, if it needs a library.
- [ ] Unit tests for the conversion logic and the config entry.
- [ ] A page in `docs/sensors/`, listed in the README's Documentation index.
- [ ] Seen working on the Pi: log line, then messages at the broker.
