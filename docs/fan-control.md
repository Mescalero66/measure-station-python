# Fan Control

The station switches the 2-wire exhaust fans fully on or off from one of the Pi's
hardware PWM pins (through an L298N driver board), from the temperature difference
between the top and the bottom of the enclosure. The code is in
`src/measure_station/fan/`, and every setting is a `FAN_*` value in `sysconfig.py`.

## What Switches the Fans

The input is `FAN_TEMPERATURE_SOURCE`'s `temperature_c` minus `FAN_REFERENCE_SOURCE`'s,
in °C. By default, that's the DS18B20 probe at the top (`temperature-probe`) minus the
CHT8305 at the bottom (`humidity`). Both are the `name`s of enabled entries in
`SENSORS`; the station refuses to start if either isn't an enabled sensor.

The fans turn on, at full speed, when the difference reaches ON, and off when it reaches
OFF. In between, they stay as they were, so a difference hovering near one of them
doesn't switch them on and off. With the defaults:

| Temperature difference | Fans |
|---|---|
| Rising, below 8 °C | Off |
| 8 °C and above | On |
| Falling, above 4 °C | Stay on |
| 4 °C and below | Off |

| Setting | Default |
|---|---|
| ON | `FAN_TEMPERATURE_DIFFERENCE_ON_C = 8.0` |
| OFF | `FAN_TEMPERATURE_DIFFERENCE_OFF_C = 4.0` |
| Minimum on time | `FAN_MIN_ON_S = 300.0` (5 minutes) |

- **Once on, the fans stay on for at least `FAN_MIN_ON_S`,** whatever turned them on,
  including the failsafe at every start. Without it, they could switch with every
  1-minute reading. The time also lets the parts that hold the heat (the power supply,
  the Pi, the regulators) cool, not only the air, so the difference doesn't spring
  straight back once they stop.
- **The gap between ON and OFF also sets how often the fans switch.** Running fans pull
  the difference back down, so a narrow gap switches them more often. The two sensors
  are accurate to ±0.5 °C (DS18B20) and ±0.3 °C (CHT8305), so the difference can be off
  by up to 0.8 °C: keep OFF above about 1.5 °C and the gap at 2 °C or more.
- **For fans that run as the difference falls,** set ON below OFF. For example, ON -5
  and OFF 0 runs them when the probe is 5 °C colder than the CHT8305, until it's no
  longer colder.

### Choosing ON and OFF

`fan_temp_diff_c` is in every `health` report, so the difference can be measured on the
station itself:

1. Run a day with the fans forced off (`FAN_TEMPERATURE_DIFFERENCE_ON_C = 99.0`) and
   note the usual difference.
2. Run a day with them forced on (`ON_C = -50.0`, `OFF_C = -60.0`) and note it again.
3. Set **OFF about 0.5 °C above the "forced on" difference.** Below it, the fans never
   switch off.
4. Set **ON about 1 °C above the usual "forced off" difference,** so the fans run only
   when heat builds up beyond normal.

### Why Humidity Doesn't Switch the Fans

The exhaust fans deliberately ignore humidity. The intake fans always run, so the air in
the enclosure is outside air warmed by the electronics. Each 1 °C of warming lowers its
relative humidity by about 6 % of its value: outside air at 95 % is about 74 % at the
top of the enclosure with a 4 °C difference. That warming is what keeps condensation
away, and running the exhaust fans in humid weather would remove it without removing any
moisture, since the outside air is the moisture's source.

## When an Input Is Missing

If either sensor fails, or misses two readings in a row, the fans run until its readings
return (`FALLBACK_FANS_ON = True`), or stay off (`False`). The log says which source is
missing. Right after the station starts, the fans run until the sensors' first readings
arrive, and then for the rest of their minimum on time.

`FALLBACK_FANS_ON` is in the **Fallback values** section at the top of `sysconfig.py`,
with the station's other fallbacks. The fan control section's `FAN_FAILSAFE_ON` takes its
value from it, so change it there, not in the fan control section.

When the station stops, it leaves the fans in that failsafe state (on, by default). The
PWM controller keeps its output without the station, so the fans stay that way until it
starts again.

## What Gets Published

Fan control has no MQTT source of its own. Its state is in the station's `health`
report, once a minute:

| Key | Meaning |
|---|---|
| `fan_failsafe` | `true` while a sensor's reading is missing or out of date |
| `fan_on` | Whether the fans are running (may be `true` with `fan_temp_diff_on` `false`: the failsafe or the minimum on time) |
| `fan_temp_diff_c` | The DS18B20 probe's temperature minus the CHT8305's, in °C |
| `fan_temp_diff_on` | Whether the temperature difference has turned the fans on |

Each is `null` while it isn't known: all of them while fan control isn't running, and
the last two while a sensor's reading is missing. The DS18B20 itself isn't published
(`"publish": False` in its `SENSORS` entry); the CHT8305 is, as the `humidity` source.
See [`health`](server-pi-interface.md#health) for every key.

The difference is re-evaluated every `FAN_CONTROL_INTERVAL_S` (10 s). The sensors read
once a minute, so a new reading takes effect within 10 s of arriving.

## The PWM Output

The station uses the Pi's hardware PWM through the kernel's sysfs interface
(`/sys/class/pwm`), at a duty cycle of 0 % (off) or 100 % (on). The hardware holds the
output while the station restarts.

- **The pin is set in config.txt.** `deploy/install.sh` adds
  `dtoverlay=pwm,pin=18,func=2`: GPIO18 (header pin 12) as channel 0 of `pwmchip0`.
  `FAN_PWM_CHIP` and `FAN_PWM_CHANNEL` must match it. To use GPIO12 (header pin 32)
  instead, use `dtoverlay=pwm,pin=12,func=4`. Reboot after changing it.
- **The Pi 2B has two PWM channels.** Channel 0 is on GPIO12 or GPIO18, channel 1 on
  GPIO13 or GPIO19. The analogue audio output uses both, so don't use it at the same time.
- **`FAN_PWM_INVERTED = True`** turns the fans on with the pin low and off with it high,
  for a driver stage that inverts the signal (e.g. a P-channel MOSFET on the fan's
  positive supply). An L298N wired as in
  [Deploying the Station](deploying-the-station.md#step-3-wire-the-sensors) (pin 12 to
  IN1, IN2 to ground) needs no inversion, nor does an N-channel MOSFET on the fan's
  negative side.
- **Permissions:** Raspberry Pi OS gives the `gpio` group access to `/sys/class/pwm`, and
  the installer puts the service user in that group.

**Check it works,** with the service stopped (`sudo systemctl stop measure-station`):

```bash
ls /sys/class/pwm/                                     # pwmchip0: the overlay is loaded
cd /sys/class/pwm/pwmchip0
echo 0 | sudo tee export                               # creates pwm0 (skip if it's there)
echo 10000000 | sudo tee pwm0/period                   # 100 Hz, in nanoseconds
echo 10000000 | sudo tee pwm0/duty_cycle               # 100 %: fans on
echo 1 | sudo tee pwm0/enable
echo 0 | sudo tee pwm0/duty_cycle                      # 0 %: fans off
```

Starting the service again takes over the same channel.

## Turning It Off

Set `FAN_ENABLED = False` and restart the service. The PWM output then stays wherever it
was last set, until the next reboot.
