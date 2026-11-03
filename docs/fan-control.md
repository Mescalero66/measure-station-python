# Fan Control

The station switches 2-wire fans on and off from one of the Pi's hardware PWM pins
(through a MOSFET), setting their speed (the PWM duty cycle) from the sensors' latest
readings. The code is in `src/measure_station/fan/`, and every setting is a `FAN_*` value
in `sysconfig.py`.

## What Sets the Speed

Two controls run together. Each turns its input into a duty cycle, and **the fans run at
the higher of the two**:

| Control | Input | Default sources |
|---|---|---|
| Temperature difference | `FAN_TEMPERATURE_SOURCE`'s `temperature_c` minus `FAN_REFERENCE_SOURCE`'s, in °C | the DS18B20 probe (`temperature-probe`) minus the CHT8305 (`humidity`) |
| Humidity | `FAN_HUMIDITY_SOURCE`'s `humidity_pct`, in % | the CHT8305 (`humidity`) |

The sources are the `name`s of enabled entries in `SENSORS`. The station refuses to start
if any of them isn't an enabled sensor.

Each control maps its input to a duty cycle with a straight-line curve and hysteresis.
The fans turn on at the minimum duty cycle when the input reaches START, and rise in a
straight line to the maximum at FULL. They turn off only once the input is back below
START by HYSTERESIS, so an input hovering around START doesn't switch them on and off.
With the humidity defaults:

| Humidity | Duty cycle |
|---|---|
| Below 60 % | 0 % (off) |
| 60 % | 30 % (on) |
| 70 % | 65 % |
| 80 % and above | 100 % |
| Falling from 60 % | Stays at 30 % down to 57 %, then off |

| Setting | Temperature difference | Humidity |
|---|---|---|
| START | `FAN_TEMPERATURE_DIFFERENCE_START_C = 2.0` | `FAN_HUMIDITY_START_PCT = 60.0` |
| FULL | `FAN_TEMPERATURE_DIFFERENCE_FULL_C = 8.0` | `FAN_HUMIDITY_FULL_PCT = 80.0` |
| HYSTERESIS | `FAN_TEMPERATURE_DIFFERENCE_HYSTERESIS_C = 0.5` | `FAN_HUMIDITY_HYSTERESIS_PCT = 3.0` |

Both controls share the duty cycles: `FAN_MIN_DUTY_PCT` (30), `FAN_MAX_DUTY_PCT` (100)
and `FAN_OFF_DUTY_PCT` (0). Duty cycles are whole percentages.

- **The minimum is there because fans stall at low duty cycles.** Set it to the lowest
  duty cycle that reliably starts yours from standstill.
- **For a control that runs the fans as its input falls,** set FULL below START. For
  example, START 0 and FULL -5 runs them when the probe is colder than the CHT8305.
- **To leave a control out,** set its START beyond any value it will see. For example,
  `FAN_HUMIDITY_START_PCT = 99.0` and `FAN_HUMIDITY_FULL_PCT = 100.0` leave only the
  temperature difference in charge.

## When an Input Is Missing

If an input sensor fails, or misses two readings in a row, the fans run at least at
`FALLBACK_FAN_DUTY_PCT` (50 %) until its readings return. If the other control asks for
more, they run at that instead. The log says which source is missing. Right after the
station starts, the fans run at the failsafe duty cycle for a few seconds, until the
sensors' first readings arrive.

`FALLBACK_FAN_DUTY_PCT` is in the **Fallback values** section at the top of
`sysconfig.py`, with the station's other fallbacks. The fan control section's
`FAN_FAILSAFE_DUTY_PCT` takes its value from it, so change it there, not in the fan
control section.

When the station stops, it leaves the fans at the failsafe duty cycle. The PWM controller
keeps running without the station, so the fans keep that speed until it starts again.

## What Gets Published

Fan control has no MQTT source of its own. Its state is in the station's `health`
report, once a minute:

| Key | Meaning |
|---|---|
| `fan_failsafe` | `true` while an input is missing or out of date |
| `fan_duty_pct` | The duty cycle the fans are switched at |
| `fan_temp_diff_c` | The DS18B20 probe's temperature minus the CHT8305's, in °C |
| `fan_temp_diff_duty_pct` | The duty cycle the temperature difference asks for |
| `fan_humidity_duty_pct` | The duty cycle the humidity asks for |

Each is `null` while it isn't known: all of them while fan control isn't running, and a
control's values while its input is missing. The DS18B20 itself isn't published
(`"publish": False` in its `SENSORS` entry); the humidity is, by the `humidity` source.
See [`health`](server-pi-interface.md#health) for every key.

The duty cycle is recalculated every `FAN_CONTROL_INTERVAL_S` (10 s). The sensors read
once a minute, so a new reading takes effect within 10 s of arriving.

## The PWM Output

The station uses the Pi's hardware PWM through the kernel's sysfs interface
(`/sys/class/pwm`). The hardware keeps the fans running while the station restarts.

- **The pin is set in config.txt.** `deploy/install.sh` adds
  `dtoverlay=pwm,pin=18,func=2`: GPIO18 (header pin 12) as channel 0 of `pwmchip0`.
  `FAN_PWM_CHIP` and `FAN_PWM_CHANNEL` must match it. To use GPIO12 (header pin 32)
  instead, use `dtoverlay=pwm,pin=12,func=4`. Reboot after changing it.
- **The Pi 2B has two PWM channels.** Channel 0 is on GPIO12 or GPIO18, channel 1 on
  GPIO13 or GPIO19. The analogue audio output uses both, so don't use it at the same time.
- **Frequency:** `FAN_PWM_FREQUENCY_HZ`, 100 Hz by default. Switching a 2-wire fan's
  whole supply works best at a low frequency, which its motor electronics can follow. If
  the fan hums, ticks or runs unevenly, try 25 to 50 Hz.
- **`FAN_PWM_INVERTED = True`** outputs 100 % minus the duty cycle, for a driver stage
  where the fan runs while the pin is low (e.g. a P-channel MOSFET on the fan's positive
  supply). An N-channel MOSFET on the fan's negative side needs no inversion.
- **Permissions:** Raspberry Pi OS gives the `gpio` group access to `/sys/class/pwm`, and
  the installer puts the service user in that group.

**Check it works,** with the service stopped (`sudo systemctl stop measure-station`):

```bash
ls /sys/class/pwm/                                     # pwmchip0: the overlay is loaded
cd /sys/class/pwm/pwmchip0
echo 0 | sudo tee export                               # creates pwm0 (skip if it's there)
echo 10000000 | sudo tee pwm0/period                   # 100 Hz, in nanoseconds
echo 5000000 | sudo tee pwm0/duty_cycle                # 50 %
echo 1 | sudo tee pwm0/enable
```

Starting the service again takes over the same channel.

## Turning It Off

Set `FAN_ENABLED = False` and restart the service. The PWM output then stays wherever it
was last set, until the next reboot.
