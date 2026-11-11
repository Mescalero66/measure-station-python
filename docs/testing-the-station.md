# Testing the Station

These checks test the station's hardware: that the Pi can see every sensor, that the fans
start, and that the readings are plausible. Do them once the station is installed (Step 6
of [Deploying the Station](deploying-the-station.md#step-6-check-that-it-works)), after
changing any wiring, or when a sensor's readings look wrong.

Run every command on the Pi, connected with `ssh` (see
[Step 2](deploying-the-station.md#step-2-start-the-pi-and-connect-to-it)), unless it says
otherwise. The code's automated tests run on a PC instead: see
[Development](development.md).

Contents:

- [Before You Start](#before-you-start)
- [Can the Pi See Every Sensor?](#can-the-pi-see-every-sensor)
- [Do the Fans Work?](#do-the-fans-work)
- [One Sensor at a Time](#one-sensor-at-a-time)
- [All Sensors at Once, Sent to a Test PC](#all-sensors-at-once-sent-to-a-test-pc)
- [Matching the Probe to the Humidity Sensor](#matching-the-probe-to-the-humidity-sensor)
- [When You're Done](#when-youre-done)

## Before You Start

Stop the station, so the tests have the sensors and fans to themselves:

```bash
sudo systemctl stop measure-station
```

It stays stopped until you start it again (see [When You're Done](#when-youre-done)), or
until the Pi restarts.

## Can the Pi See Every Sensor?

List the sensors on the I2C bus:

```bash
sudo i2cdetect -y 1
```

With everything wired, it shows these eight addresses in a grid:

```text
     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f
00:                         -- -- -- -- -- -- -- --
10: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- 1f
20: -- -- -- 23 -- -- -- -- -- -- -- -- -- -- -- --
30: -- -- -- -- -- -- -- -- 38 -- -- -- -- -- -- --
40: 40 -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
50: -- -- -- 53 -- -- -- -- -- -- -- -- -- -- -- --
60: -- -- 62 -- -- -- -- -- -- -- -- 6b -- -- -- --
70: -- -- -- -- -- -- 76 --
```

| Address | Sensor |
|---|---|
| `1f` | [Temperature sensor](sensors/temperature-sensor.md) (MCP9808) |
| `23` | [Light sensor](sensors/light-sensor.md) (BH1750) |
| `38` and `53` | [Air quality board](sensors/air-quality-board.md) (AHT21 and ENS160) |
| `40` | [Humidity sensor](sensors/humidity-sensor.md) (CHT8305) |
| `62` | [CO2 sensor](sensors/co2-sensor.md) (SCD41) |
| `6b` | [Particulate sensor](sensors/particulate-sensor.md) (SEN62 or SEN65) |
| `76` | [Pressure sensor](sensors/pressure-sensor.md) (BMP280) |

- **A number is missing:** that sensor isn't answering. Shut down
  (`sudo shutdown -h now`), unplug, and check its four wires against the table in
  [Step 3](deploying-the-station.md#step-3-wire-the-sensors). A loose jumper wire is the
  usual cause.
- **None appear, or it shows errors:** check the shared wires to pins 1, 3, 5 and 6. If
  it says the bus doesn't exist, run the installer again (Step 4) and reboot.

The DS18B20 probe on pin 7 isn't on the I2C bus. Check it separately:

```bash
ls /sys/bus/w1/devices/
```

It should list one `28-...` entry (its ID). If there's none, check the probe's three
wires and the 4.7 kΩ resistor on pin 7.

## Do the Fans Work?

The PWM pins for the fans (pins 12 and 35) are ready if this lists `pwmchip0`:

```bash
ls /sys/class/pwm/
```

Then switch the fans by hand. Channel 0 is the exhaust fans; for the intake fans, use `1`
and `pwm1` instead:

```bash
cd /sys/class/pwm/pwmchip0
echo 0 | sudo tee export                               # creates pwm0 (skip if it's there)
echo 10000000 | sudo tee pwm0/period                   # 100 Hz, in nanoseconds
echo 10000000 | sudo tee pwm0/duty_cycle               # 100 %: fans on
echo 1 | sudo tee pwm0/enable
echo 5000000 | sudo tee pwm0/duty_cycle                # 50 %: do they keep turning?
echo 0 | sudo tee pwm0/duty_cycle                      # 0 %: fans off
```

Switch each set on and off a few times: every fan should start from standstill every
time. For `FAN_MODE = "analog"`, also check that the exhaust fans keep turning at
`FAN_ANALOG_MIN_DUTY_PCT` (50 % by default) once started at 100 %; if not, raise it.
Starting the station again takes over the same channels. See
[Fan Control](fan-control.md) for how the station runs them.

## One Sensor at a Time

This tool reads one sensor every 10 seconds and prints the result. With no name, it lists
the sensors:

```bash
/opt/measure-station/.venv/bin/python -m measure_station.read_sensor
```

Then read each one by name, for example:

```bash
/opt/measure-station/.venv/bin/python -m measure_station.read_sensor temperature
```

Whichever sensor it reads, it runs the fans too, so the sensor gets the airflow it will
have in service: the exhaust fans at 100 %, and the intake fans at 100 % for 3 seconds
(`FAN_KICKSTART_S`), then at 50 % (`FAN_ANALOG_MIN_DUTY_PCT`), whatever `FAN_MODE` is.
So it also shows whether the intake fans keep turning at that speed. If the fans can't be
started (e.g. the PWM overlay isn't loaded), it says so and reads the sensor anyway.

Press **Ctrl+C** to stop it. The fans are then left as the stopped station leaves them:
exhaust at 100 %, intake off. Check that each sensor's values look plausible (the room
temperature, a humidity between 20 and 90 %, and so on). Things that are normal:

- `particulates` takes about 35 seconds to show its first reading.
- `air-quality` shows `validity=warmup` for its first 3 minutes, and `startup` for the
  first hour of a brand-new sensor.

## All Sensors at Once, Sent to a Test PC

To compare the sensors with each other, or watch them over hours, run the whole station
for a while and send its readings to another computer on the network. It reads every
enabled sensor every 10 seconds (not every minute), runs the fans exactly as the station
does, and reads the MQ gas board. Each reading is printed and sent straight to the test
PC's MQTT broker, on the station's usual topics. Nothing is stored on the SD card: while
the test PC can't be reached, readings are only printed.

On the test PC, install an MQTT broker, for example
[Mosquitto](https://mosquitto.org/download/). Mosquitto only accepts connections from
other computers if its configuration file (`mosquitto.conf`) has these two lines:

```text
listener 1883
allow_anonymous true
```

Restart Mosquitto, and let port 1883 through the test PC's firewall. Then watch, on the
test PC, everything the station sends:

```bash
mosquitto_sub -h localhost -t 'measure-station/#' -v
```

On the Pi, give the test PC's IP address (and a port, if it isn't 1883):

```bash
/opt/measure-station/.venv/bin/python -m measure_station.live_test 192.168.1.50
```

Differences from the running station:

- The DS18B20 (`temperature-probe`) is sent too; normally it's only used for the fans.
- The `health` report is sent every 10 seconds too (not every minute), so the fan state
  can be followed. Its `outbox_backlog` and `awaiting_time_sync` are always 0, since
  nothing is stored.
- It connects as `measure-station-<station ID>-test`, so it can't knock the station off a
  broker they share.
- Readings taken before the Pi's clock has synchronised are sent with `"ts_src":
  "estimated"` and aren't corrected later.

Press **Ctrl+C** to stop it. The fans are left as the stopped station leaves them.

## Matching the Probe to the Humidity Sensor

Fan control compares two sensors: the DS18B20 probe at the top of the enclosure minus the
CHT8305 (`humidity`) at the bottom. Each is accurate only to within a few tenths of a
degree (±0.5 °C and ±0.3 °C typical), so even in the same air they can disagree by up to
about 0.8 °C. The probe's offset, `PROBE_TEMPERATURE_OFFSET_C`, removes that
disagreement, so the
difference fan control sees is the real one. It matches the probe to the CHT8305; it
doesn't make either one more accurate.

1. **Put the two side by side,** with `PROBE_TEMPERATURE_OFFSET_C` at `0.0`: tape
   the DS18B20 probe to the CHT8305 probe, in the same air, out of the fans' airflow, and
   away from anything warm (the Pi, the power supply, the MQ board).
2. **Run [`live_test`](#all-sensors-at-once-sent-to-a-test-pc),** and wait 15 to 30
   minutes for both to settle.
3. **Compare** `temperature_c` of `temperature-probe` with `temperature_c` of `humidity`
   over about ten readings. The offset is the probe's average minus the CHT8305's. For
   example, if the probe reads 21.4 °C and the CHT8305 22.0 °C, the offset is `-0.6`
   (the probe reads 0.6 °C low).
4. **Set it** as `PROBE_TEMPERATURE_OFFSET_C`, in the **Calibration** section near the
   top of `sysconfig.py` (for example `PROBE_TEMPERATURE_OFFSET_C: float = -0.6`). Put
   the probe back at the top of the enclosure, and restart the station.

The offset is subtracted from every reading, the same way as the
[CO2 sensor's](sensors/co2-sensor.md). If the two disagree by different amounts at
different times (for example, only while the fans are off), it isn't a calibration error:
look at where the sensors are placed instead.

## When You're Done

Start the station again:

```bash
sudo systemctl start measure-station
```
