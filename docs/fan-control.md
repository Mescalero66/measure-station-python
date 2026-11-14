# Fan Control

The enclosure has two sets of 2-wire fans, each switched by its own MOSFET from its own
Pi hardware PWM pin: the **exhaust** fans at the top and the **intake** fans at the
bottom. The station decides from the sensors' latest readings which sets run, and how
fast. The code is in `src/measure_station/fan/`, and every setting is a `FAN_*` value in
`sysconfig.py`.

## How the Fans Work Together

Most of the sensors sit just above an open vent in the middle of the enclosure's floor.
They measure outside air only while air flows **in** through that vent, so normally the
exhaust fans run alone: they lower the pressure inside a little, which draws outside air
in through every bottom opening, past the sensors and before any electronics warm it.
The heat of the power supply, the Pi and the MQ heaters rises and leaves at the top,
downstream of the sensors.

Which sets run, in priority order:

| Mode | When | Exhaust (top) | Intake (bottom) |
|---|---|---|---|
| **Heat** | The temperature difference has reached ON, until it reaches OFF | On | On |
| **Failsafe** | A reading fan control needs is missing or out of date | On (100 %) | Off |
| **Fog** | The humidity has reached FOG ON, until it reaches FOG OFF | Slow (50 %) | Off |
| **Normal** | Otherwise | On | Off |

When the station stops, it leaves the fans in the failsafe state: exhaust at 100 %,
intake off. The PWM controller keeps its outputs without the station, so the fans stay
that way until it starts again. The same happens at every start until the sensors' first
readings arrive.

## Heat

The temperature difference is `FAN_TEMPERATURE_SOURCE`'s `temperature_c` minus
`FAN_REFERENCE_SOURCE`'s, in °C: by default the DS18B20 probe at the top
(`temperature-probe`) minus the CHT8305 at the bottom (`humidity`). It shows how much the
electronics are warming the enclosure. Ventilation can only reduce the rise above the air
drawn in; it can't cool the enclosure below outside air temperature. So the difference,
not any absolute temperature, decides when more air is needed.

The two sensors can disagree by up to about 0.8 °C in the same air, within their
accuracy, so the difference can read slightly negative when the box isn't warm. To
remove that, set `PROBE_TEMPERATURE_OFFSET_C` in the Calibration section of
`sysconfig.py` (see
[Matching the Probe to the Humidity Sensor](testing-the-station.md#matching-the-probe-to-the-humidity-sensor)).

| Setting | Default | Meaning |
|---|---|---|
| `FAN_TEMPERATURE_DIFFERENCE_ON_C` | 8.0 | Heat mode starts at this difference |
| `FAN_TEMPERATURE_DIFFERENCE_OFF_C` | 3.0 | ...and ends at this one; in between, it stays as it was |
| `FAN_INTAKE_MIN_ON_S` | 120 (2 minutes) | Once on, the intake fans stay on at least this long (also the gap after fog mode, below) |

- **The minimum on time** stops the intake fans switching with every 1-minute reading,
  and lets the parts that hold the heat (the power supply, the Pi, the regulators) cool
  too, not only the air, so the difference doesn't spring straight back.
- **Heat mode should be rare** under cover: it's for a very hot day, a failed or blocked
  exhaust fan, or a blocked vent. 2-wire fans can't report their speed, so a rising
  difference is the only sign of a dead exhaust fan, and heat mode covers for it.
- **While the intake fans run, the enclosure is pushing air out** rather than drawing it
  in, so readings from the sensors at the bottom vent may be slightly warm. The `health`
  report's `fan_intake_duty_pct` above 0 marks those minutes.
- **Accuracy:** the DS18B20 is accurate to ±0.5 °C and the CHT8305 to ±0.3 °C, so the
  difference can be off by up to 0.8 °C. Keep OFF above about 1.5 °C, and the gap
  between ON and OFF at 2 °C or more.

### Choosing ON and OFF

`fan_temp_diff_c` is in every `health` report, so the difference can be measured on the
station itself:

1. Run a day normally and note the usual difference, and the highest.
2. Run a day in heat mode throughout (`FAN_TEMPERATURE_DIFFERENCE_ON_C = -50.0`,
   `OFF_C = -60.0`) and note the difference with both sets running.
3. Set **OFF about 0.5 °C above the difference from step 2.** Below it, heat mode never
   ends.
4. Set **ON 1 to 2 °C above the usual difference from step 1,** so heat mode starts only
   when heat builds up beyond normal.

## Fog

`FAN_HUMIDITY_SOURCE`'s `humidity_pct` (the CHT8305, in the air drawn in at the bottom)
turns fog mode on. Fog mode **slows the exhaust fans** to `FAN_ANALOG_MIN_DUTY_PCT`
(50 %, in either `FAN_MODE`) and keeps the intake fans off:

| Setting | Default | Meaning |
|---|---|---|
| `FAN_FOG_ON_PCT` | 95.0 | Fog mode starts at this humidity... |
| `FAN_FOG_ON_READINGS` | 2 | ...once this many readings in a row reach it |
| `FAN_FOG_MIN_S` | 900 (15 minutes) | Fog mode lasts at least this long... |
| `FAN_FOG_OFF_PCT` | 90.0 | ...then ends at this humidity |
| `FAN_INTAKE_MIN_ON_S` | 120 (2 minutes) | Once over, fog mode can't start again for this long |

For example: readings of 96 % at 06:00 and 06:01 start fog mode at 06:01. It lasts until
at least 06:16, then ends at the first reading of 90 % or less. After that, the exhaust
fans run at full speed for at least 2 minutes, even if the humidity is back at 95 %.

- **Why slower, not faster:** the enclosure's air is outside air warmed by the
  electronics. Each 1 °C of warming lowers its relative humidity by about 6 % of its
  value, so the less air flows through, the drier the enclosure stays. More airflow
  wouldn't remove any moisture, since the outside air is where the moisture comes from,
  but it would draw in more mist droplets.
- **Why not off:** with no airflow, the electronics' heat builds up around the sensors,
  so their readings come out warm for as long as fog mode lasts. The CHT8305 also
  warms and reads drier, which could end fog mode early. A slow exhaust keeps outside
  air moving past the sensors.
- **Why two readings in a row:** so one spiked reading doesn't slow the fans for
  15 minutes. Real fog starts fog mode about a minute later than it would on one reading.
- **Why the minimum times:** they stop fog mode switching on and off with every
  1-minute reading, together with the 5 % gap between ON and OFF.
- **Heat mode overrides fog mode,** and so does a missing reading. A missing humidity
  reading ends fog mode, and the count of readings in a row starts again.
- **`FAN_ANALOG_MIN_DUTY_PCT` must keep the exhaust fans turning,** since fog mode uses it
  in binary mode too. The fans are already turning when fog mode slows them, so they
  get no kick-start.

## Binary or Analog

`FAN_MODE` sets how fast a fan set that's on runs:

- **`"binary"`** (default): 100 %.
- **`"analog"`**: by the temperature difference, from `FAN_ANALOG_MIN_DUTY_PCT` (50 %)
  at or below OFF, rising in a straight line to 100 % at ON. With the defaults:

| Temperature difference | Exhaust | Intake |
|---|---|---|
| 3 °C (OFF) and below | 50 % | Off |
| 5.5 °C, rising | 75 % | Off |
| 8 °C (ON) and above | 100 % | On, at 100 % |
| 5.5 °C, falling | 75 % | 75 % |
| 3 °C (OFF) and below, after at least 2 minutes | 50 % | Off |

In analog mode, the exhaust fans idle at the minimum in normal running, which is quieter
and draws in less dust, and speed up as heat builds. The intake fans only come on at ON,
where the speed is already 100 %, so they always start at full power. While a reading is
missing, the exhaust fans run at 100 %.

- **Kick-start:** a fan set that starts from standstill (0 %) runs at 100 % for
  `FAN_KICKSTART_S` (3 s) first, then at its speed. A fan that starts reliably at full
  power can stall from standstill at a low duty cycle. With the defaults, the intake
  fans always start at 100 % anyway; it matters if `FAN_ANALOG_MIN_DUTY_PCT` is 0, which
  stops the exhaust fans at or below OFF (and in fog).
- **`FAN_ANALOG_MIN_DUTY_PCT` must keep the exhaust fans turning once started.** Above
  that, it's a choice between airflow and noise (and dust drawn in). Test it by hand
  (see [Testing the Station](testing-the-station.md#do-the-fans-work)).
- **`FAN_PWM_FREQUENCY_HZ`** (100 Hz) matters only at speeds between 0 and 100 % (analog
  mode, and fog mode); at 0 % and 100 % the output is steady. Switching a 2-wire fan's whole supply works best at a low frequency,
  which its motor electronics can follow. If the fans hum, tick or run unevenly, try 25
  to 50 Hz.
- **Speed changes aren't logged:** the speed can change with every reading. The log
  records each change of `fan_status` (below) and which sets are on.

## When a Reading Is Missing

If the DS18B20 or the CHT8305 fails, or misses two readings in a row, fan control can't
tell whether heat is building up or whether it's foggy, so it runs the failsafe: exhaust
at 100 %, intake off. That keeps the enclosure ventilated and the sensor vent an inlet.
Under cover, with about 10 to 12 W of heat inside, the intake fans aren't needed for
safety. The log says which source is missing. A missing reading doesn't stop heat mode
if the difference is still known to be high.

## What Gets Published

Fan control has no MQTT source of its own. Its state is in the station's `health`
report, once a minute:

| Key | Meaning |
|---|---|
| `fan_status` | Why the fans are as they are (below) |
| `fan_exhaust_duty_pct` | The exhaust fans' duty cycle, 0 to 100 %; `FAN_ANALOG_MIN_DUTY_PCT` (50) in fog |
| `fan_intake_duty_pct` | The intake fans' duty cycle, 0 to 100 %; above 0, the sensors' air may be slightly warm |
| `fan_temp_diff_c` | The DS18B20 probe's temperature minus the CHT8305's, in °C |

`fan_status` is the mode, or a hold within it while a minimum time is what decides it:

| `fan_status` | Meaning |
|---|---|
| `normal` | Normal mode |
| `heat` | Heat mode: the difference has reached ON and not yet fallen to OFF |
| `heat_hold` | The difference is back at OFF, but the intake fans haven't run `FAN_INTAKE_MIN_ON_S` yet |
| `fog` | Fog mode |
| `fog_hold` | The humidity is back at FOG OFF, but fog mode hasn't lasted `FAN_FOG_MIN_S` yet |
| `fog_lockout` | The humidity would start fog mode, but it ended less than `FAN_INTAKE_MIN_ON_S` ago (fans as in normal mode) |
| `failsafe` | A reading fan control needs is missing or out of date |
| `starting` | Fan control hasn't set the fans yet (at most the first report after a start) |
| `error` | Fan control has stopped or failed (e.g. a PWM output): the fans are in an unknown state |

Only the main reason shows: while heat mode or the failsafe overrides fog mode, the
status is `heat` or `failsafe`. The other keys are `null` while they aren't known: all
three while fan control isn't running (`starting` or `error`), and `fan_temp_diff_c`
while its readings are missing. The DS18B20 itself isn't published (`"publish": False`
in its `SENSORS` entry); the CHT8305 is, as the `humidity` source. See
[`health`](server-pi-interface.md#health) for every key.

The readings are re-evaluated every `FAN_CONTROL_INTERVAL_S` (10 s). The sensors read
once a minute, so a new reading takes effect within 10 s of arriving.

## The PWM Outputs

The station uses the Pi's hardware PWM through the kernel's sysfs interface
(`/sys/class/pwm`). The hardware holds the outputs while the station restarts.

- **The pins are set in config.txt.** `deploy/install.sh` adds
  `dtoverlay=pwm-2chan,pin=18,func=2,pin2=19,func2=2`: GPIO18 (header pin 12) as channel
  0 of `pwmchip0` for the exhaust fans, and GPIO19 (header pin 35) as channel 1 for the
  intake fans. `FAN_PWM_CHIP`, `FAN_EXHAUST_PWM_CHANNEL` and `FAN_INTAKE_PWM_CHANNEL`
  must match it. Channel 0 can be GPIO12 (`pin=12,func=4`) instead, and channel 1
  GPIO13 (`pin2=13,func2=4`). Reboot after changing it.
- **The analogue audio output uses both channels,** so don't use it at the same time.
- **`FAN_PWM_INVERTED = True`** runs the fans while the pins are low, for driver stages
  that invert the signal (e.g. a P-channel MOSFET on the fans' positive supply). The
  N-channel MOSFETs in
  [Deploying the Station](deploying-the-station.md#step-3-wire-the-sensors) need no
  inversion.
- **Permissions:** Raspberry Pi OS gives the `gpio` group access to `/sys/class/pwm`, and
  the installer puts the service user in that group.

**To check the outputs and the fans by hand,** see
[Testing the Station](testing-the-station.md#do-the-fans-work). Its `read_sensor` and
`live_test` tools run the fans too.

## Turning It Off

Set `FAN_ENABLED = False` and restart the service. The PWM outputs then stay wherever
they were last set, until the next reboot.
