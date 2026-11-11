# 1-Wire Temperature Probe (DS18B20)

A Maxim DS18B20 temperature sensor on the Pi's 1-wire bus, usually as a waterproof probe
on a cable. It measures temperature to ±0.5 °C (from -10 to 85 °C), in steps of
0.0625 °C. Its reading is an input of [fan control](../fan-control.md).

- **Driver:** none needed. The kernel's `w1-gpio` and `w1-therm` drivers read the chip,
  and the station reads their sysfs file.
- **Station adapter:** `src/measure_station/sensors/ds18b20_1wire.py`.

**Wiring**

| DS18B20 (probe wire) | Pi header pin |
|---|---|
| VDD (red) | 1 (3.3 V) |
| GND (black) | 9 (GND) |
| DQ, data (yellow) | 7 (GPIO4) |

- **Fit a 4.7 kΩ pull-up resistor** between DQ and 3.3 V. Without it, reads fail
  their CRC check, or return nothing. Some breakout boards already have one.
- **Power it from 3.3 V, not 5 V,** so the data line stays at the Pi's logic level.
  Don't use parasite power (VDD tied to GND): give it its own supply wire.
- **The 1-wire bus is enabled by `deploy/install.sh`** (`raspi-config nonint do_onewire 0`,
  which adds `dtoverlay=w1-gpio` to config.txt, on GPIO4). Reboot after installing.
- **Check it's connected:** `ls /sys/bus/w1/devices/` should list a `28-...` entry, and
  `cat /sys/bus/w1/devices/28-*/temperature` prints millidegrees. Then
  `python -m measure_station.read_sensor temperature-probe` prints the station's reading
  (see [Testing the Station](../testing-the-station.md#one-sensor-at-a-time)).

**What Gets Published**

Nothing on its own: its `SENSORS` entry has `"publish": False`, so it's read only for
[fan control](../fan-control.md), whose state (including the temperature difference) is
in the `health` report. Each reading is `temperature_c`: the temperature at the probe,
-55 to 125 °C, in steps of 0.0625 °C, ±0.5 °C from -10 to 85 °C. To publish it too, as
source `temperature-probe` (`sensor_type` `ds18b20`, about 160 bytes), set
`"publish": True`.

**Bad Reads**

Each read is checked. A failed check is retried twice, and then the read fails: the
station logs it, and re-opens the sensor with a growing delay.

- **CRC check failed:** usually a missing pull-up, a loose wire, or a very long cable.
- **All zeros:** the data line is shorted to ground. This passes the CRC check, so it's
  checked separately.
- **85 °C exactly:** the chip's power-on value, read before it has measured. It usually
  means it browned out (a poor supply connection).

**Settings**

The `SENSORS` entry in `sysconfig.py`:

| Option | Default | Meaning |
|---|---|---|
| `name` | `"temperature-probe"` | Its name, which fan control refers to as `FAN_TEMPERATURE_SOURCE` (and the MQTT source name, if published) |
| `interval_s` | `60` | Seconds between readings |
| `publish` | `False` | Whether its readings are also sent to MQTT |
| `device_id` | `None` | The probe's 1-wire ID, e.g. `"28-0000075a1b2c"` (see `/sys/bus/w1/devices/`). `None` uses the only DS18B20 on the bus, and fails if there's more than one |

To add a second probe, add a second entry with its own `name` and `device_id`. It needs
no Python dependencies, so there's no install extra.
