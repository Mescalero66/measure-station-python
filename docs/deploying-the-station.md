# Deploying the Station

This guide takes a Raspberry Pi 2B from an empty SD card to a running Measure Station. It
takes about two hours. You don't need to know Linux: every command is given in full, and
each step says what you should see.

The station reads its sensors every minute and stores every reading on its SD card. It
sends them to an MQTT broker (a message server on the second Pi) whenever one is
reachable. Until that server exists, the station keeps the readings, for up to about
3 months.

**This is the first deployment on real hardware.** The installer and the sensor code have
been tested on a PC with simulated sensors, but never yet on a Pi. If a step fails, stop,
copy the last 20 or so lines from the terminal, and send them to the station's developer.

Contents:

- [What You Need](#what-you-need)
- [Linux Basics for This Guide](#linux-basics-for-this-guide)
- [Step 1: Prepare the SD Card](#step-1-prepare-the-sd-card)
- [Step 2: Start the Pi and Connect to It](#step-2-start-the-pi-and-connect-to-it)
- [Step 3: Wire the Sensors](#step-3-wire-the-sensors)
- [Step 4: Install the Station Software](#step-4-install-the-station-software)
- [Step 5: Enter the Station's Settings](#step-5-enter-the-stations-settings)
- [Step 6: Check That It Works](#step-6-check-that-it-works)
- [Step 7: Connect to the Server Pi (Later)](#step-7-connect-to-the-server-pi-later)
- [Everyday Tasks](#everyday-tasks)
- [Troubleshooting](#troubleshooting)

## What You Need

**Hardware**

| Item | Notes |
|---|---|
| Raspberry Pi 2 Model B | The station is built for this model. |
| Power supply | 5 V micro-USB, good quality, 2.5 A recommended. The sensors draw extra current, and a weak supply causes random faults. |
| microSD card | 16 GB or larger. An "endurance" card lasts longest, because the station writes to it all day. |
| Ethernet cable | The Pi 2B has no Wi-Fi. Plug it into your router or a network switch. |
| Sensors, MQ gas board and wires | Listed in [Step 3](#step-3-wire-the-sensors). |
| A computer with an SD card reader | Windows, Mac or Linux. You use it to prepare the card, and then to type commands on the Pi. |

**Information to collect before you start**

| What | Example | Used in |
|---|---|---|
| A name for the Pi on the network (its hostname) | `station1` | Step 1 |
| A username and password for logging in to the Pi | `admin` | Step 1 |
| The station ID: a short name that labels all its data | `mstn-2b` | Step 5 |
| The IP address of a time server on your network (often the router) | `192.168.1.1` | Step 5 |
| The MQTT broker's address, port, username and password | `192.168.1.20`, `1883` | Step 5. Leave these out until the server Pi exists. |

The station ID may use only letters, digits, `.`, `_` and `-`. Choose it once and keep it:
it's how the server tells this station's data apart from any other.

## Linux Basics for This Guide

You control the Pi by typing commands into a **terminal**: a text window on your computer
that is connected to the Pi. These few things are all you need.

- **Typing a command.** Type it (or paste it) and press **Enter**. Linux is
  case-sensitive and spaces matter, so copy commands exactly. In this guide, commands are
  shown in grey boxes.
- **The prompt.** When the Pi is ready for a command it shows a line such as
  `admin@station1:~ $`. The part before `@` is your user; the part after is the Pi's name.
  While a command is still running, the prompt doesn't appear.
- **Pasting.** In Windows Terminal or PowerShell, paste with **Ctrl+V** or a right-click.
  On a Mac, use **Cmd+V**.
- **Ctrl+C stops a command.** In a terminal, Ctrl+C means "stop", not "copy". Use it to end
  a command that keeps running, such as a log viewer.
- **`sudo` means "as administrator".** Commands that change the system start with `sudo`.
  The first time, it asks for your Pi password. **Nothing appears while you type it**, not
  even dots. Type it and press Enter.
- **Pagers.** Some commands show their output one screen at a time. Use the arrow keys to
  scroll, and press **q** to quit.
- **Lines starting with `#`** in a settings file are comments: the system ignores them. To
  turn a setting on, delete the `#` at the start of its line.

**Editing a file with nano**

`nano` is a simple text editor that runs inside the terminal. For example,
`sudo nano /etc/measure-station/env` opens that file for editing.

| To | Press |
|---|---|
| Move around | The arrow keys (the mouse doesn't work) |
| Save | **Ctrl+O**, then **Enter** to keep the file name |
| Exit | **Ctrl+X**. If you haven't saved, it asks: press **Y**, then **Enter** |
| Search | **Ctrl+W**, type the word, then **Enter** |

The keys are also listed along the bottom of the nano screen, where `^` means Ctrl.

## Step 1: Prepare the SD Card

You write the Pi's operating system, Raspberry Pi OS, onto the SD card with a free tool
called Raspberry Pi Imager.

1. On your computer, download and install **Raspberry Pi Imager** from
   [raspberrypi.com/software](https://www.raspberrypi.com/software/).
2. Put the microSD card in your computer's card reader and start Imager.
3. Choose:
   - **Device:** Raspberry Pi 2.
   - **Operating system:** *Raspberry Pi OS (other)*, then **Raspberry Pi OS Lite
     (32-bit)**. "Lite" has no desktop, which the station doesn't need. Don't choose a
     "Legacy" version.
   - **Storage:** your SD card. Check it's the right one: everything on it is erased.
4. Imager offers to **customise the OS settings**. Accept, and set the following. (Older
   versions of Imager show these in an "Edit settings" window with tabs; newer ones show
   them as pages, one after another.)

   | Setting | Value |
   |---|---|
   | Hostname | Your chosen name, e.g. `station1` |
   | Username and password | Your chosen login. Write the password down |
   | Wireless LAN | Leave off (the Pi 2B has no Wi-Fi) |
   | Locale | Your time zone and keyboard layout |
   | SSH (on the Services or Remote access page) | **Enable**, with password authentication |

5. Write the card. It takes 5 to 15 minutes. When Imager says it's finished, remove the
   card.

## Step 2: Start the Pi and Connect to It

**SSH** lets you type commands on the Pi from your own computer, over the network.

1. Put the SD card in the Pi (the slot is on the underside, at the short edge).
2. Connect the Ethernet cable from the Pi to your router or switch.
3. Plug in the power supply. The red light comes on and the green light flickers.
4. **Wait 3 minutes.** On its first start, the Pi sets itself up and restarts once.
5. On your computer, open a terminal:
   - **Windows:** press the Windows key, type `PowerShell` and press Enter.
   - **Mac:** open **Terminal** (in Applications, then Utilities).
6. Connect, using your username and hostname from Step 1:

   ```bash
   ssh admin@station1.local
   ```

7. The first time, it asks `Are you sure you want to continue connecting?`. Type `yes`
   and press Enter.
8. Enter your Pi password (it doesn't show as you type). You should now see the Pi's
   prompt, e.g. `admin@station1:~ $`. Every command from here on is typed at this prompt.

If `ssh` says it can't find the host, see [Troubleshooting](#troubleshooting).

Now bring the operating system up to date. On a Pi 2B this can take 15 to 30 minutes:

```bash
sudo apt update && sudo apt full-upgrade -y
```

When the prompt comes back, restart the Pi:

```bash
sudo reboot
```

Your SSH connection closes. Wait 2 minutes, then connect again with the same `ssh`
command.

## Step 3: Wire the Sensors

**Always shut the Pi down and unplug it before wiring.** To shut it down safely:

```bash
sudo shutdown -h now
```

Wait until the green light has stopped flashing (about 20 seconds), then unplug the power.

**Finding the pins.** The Pi has a double row of 40 pins along one edge, numbered 1 to 40.
Pin 1 is at the end furthest from the USB ports, in the row further from the board's edge;
its solder pad on the underside is square. Odd-numbered pins are in that inner row, and
even-numbered pins along the board's edge. Only these pins are used:

```text
          inner row     edge row
 3.3 V        [ 1]       [ 2]   5 V: never connect a sensor here
 SDA          [ 3]       [ 4]   5 V: never connect a sensor here
 SCL          [ 5]       [ 6]   GND (ground)
              [ 7]       [ 8]
 GND          [ 9]       [10]   RX: the MQ gas board's TX wire
              ...        ...
```

**The I2C sensors all share four wires.** Every one of the seven I2C sensor boards
connects to the same four pins: **3.3 V (pin 1), GND (pin 6), SDA (pin 3) and SCL
(pin 5)**. Join them on a small proto-board or breadboard with four rails, one per
signal, and run four wires from the rails to the Pi.

- **Never use the 5 V pins (2 and 4) for a sensor.** Several of these boards have no
  voltage regulator, and 5 V can damage the sensor or the Pi.
- **Check the pull-up resistors before connecting the rails to the Pi.** The Pi 2B has
  1.8 kΩ pull-up resistors on SDA and SCL, and most sensor boards add their own. With
  seven boards they add up, and too strong a pull-up (too low a resistance) causes bus
  errors. With all the boards on the rails, nothing powered and the rails not yet
  connected to the Pi, set a multimeter to resistance and measure:
  - between the **SDA** rail and the **3.3 V** rail, and
  - between the **SCL** rail and the **3.3 V** rail.

  Each should read **2.2 kΩ or more**. Together with the Pi's own 1.8 kΩ, that keeps the
  total at about 1 kΩ or more. If either reads lower, some boards' pull-ups must be
  removed before you go on: ask the station's builder.
- Each sensor's page in [`sensors/`](sensors/) has its own wiring table, which names the
  board's pins.

| Sensor | Board | Its address (Step 6) | Notes |
|---|---|---|---|
| Temperature | MCP9808 (DFRobot SEN0435) | `1f` | Leave the A0, A1, A2 and ALT pins unconnected. [Wiring](sensors/temperature-sensor.md) |
| Light | BH1750 | `23` | Leave ADDR unconnected. Face it towards the sky. [Wiring](sensors/light-sensor.md) |
| Air quality | ENS160 + AHT21 | `38` and `53` | Leave ADD, CS and INT unconnected. [Wiring](sensors/air-quality-board.md) |
| Humidity | CHT8305 probe (DFRobot SEN0546) | `40` | Red 3.3 V, black GND, white SCL, yellow SDA. **The SCL wire must be white**: a green one means a different chip that the station can't read. [Wiring](sensors/humidity-sensor.md) |
| CO2 | SCD41 | `62` | [Wiring](sensors/co2-sensor.md) |
| Particulates | SEN62 or SEN65 | `6b` | Either model works; the station detects which. Keep its cable under 10 cm if you can (50 cm at most). [Wiring](sensors/particulate-sensor.md) |
| Pressure | BMP280 | `76` | Leave CSB and SDO unconnected. [Wiring](sensors/pressure-sensor.md) |

**The MQ gas board** connects with two wires, not to the four shared rails:

- the board's **TX** to the Pi's **pin 10** (RX);
- the board's **GND** to a Pi GND pin, e.g. **pin 9**.

The board has its own power supply. See [MQ Gas Board](sensors/mq-gas-board.md).

**Placement.** The temperature and humidity sensors need shade and moving air, away from
the Pi's own heat. The particulate sensor needs its air inlet and outlet clear, and must
never get wet. Each sensor's page has more detail.

When the wiring is done, plug the power back in, wait 2 minutes, and connect with `ssh`
again. You check the wiring in Step 6, once the software has switched the sensor bus on.

## Step 4: Install the Station Software

The station's software is downloaded from GitHub into `/opt/measure-station`, and its
installer sets up everything else.

1. Install `git`, the tool that downloads the software:

   ```bash
   sudo apt-get install -y git
   ```

2. Download the station software:

   ```bash
   sudo git clone https://github.com/Mescalero66/measure-station-python.git /opt/measure-station
   ```

3. Run the installer:

   ```bash
   sudo bash /opt/measure-station/deploy/install.sh
   ```

   It prints a line starting with `==>` for each stage, and can take 20 minutes or more.
   It:
   - installs the system packages the station needs;
   - switches on the I2C sensor bus and the serial port for the MQ board, and turns off
     the serial login console that would otherwise use that port;
   - turns on network time (NTP);
   - creates a `measure-station` user for the service, with access to the sensors;
   - installs the station's Python software and sensor drivers;
   - sets the station up to start automatically at every boot, with a hardware watchdog
     that restarts the Pi if it ever freezes.

4. It's finished when it prints **`Installation complete.`** and a list of next steps. If
   it stops with an error instead, see [Troubleshooting](#troubleshooting).

The installer is safe to run again: it skips or repeats each stage without harm.

## Step 5: Enter the Station's Settings

Settings are in two places:

- **`/etc/measure-station/env`** holds the settings that belong to this particular Pi:
  its station ID, the broker's address and password, and the time server.
- **`/opt/measure-station/src/measure_station/sysconfig.py`** holds everything else, such
  as the sensors' settings. Most of it is already right.

### The Pi's Own Settings

Open the file:

```bash
sudo nano /etc/measure-station/env
```

It contains lines like these, all switched off with `#`:

```text
#MEASURE_STATION_STATION_ID=mstn-2b
#MEASURE_STATION_MQTT_HOST=broker.example.com
#MEASURE_STATION_MQTT_PORT=1883
#MEASURE_STATION_MQTT_USERNAME=
#MEASURE_STATION_MQTT_PASSWORD=
#MEASURE_STATION_NTP_SERVERS=192.168.1.10,pool.ntp.org
```

For each setting you use, delete its `#` and put your value after the `=`, with no
spaces around it:

| Setting | What to enter |
|---|---|
| `MEASURE_STATION_STATION_ID` | The station ID, e.g. `mstn-2b`. If left off, it's `mstn-2b` |
| `MEASURE_STATION_NTP_SERVERS` | Your local time server first, then a public one, separated by a comma, e.g. `192.168.1.1,pool.ntp.org` |
| `MEASURE_STATION_MQTT_HOST` | The server Pi's IP address. Leave it off until the server exists ([Step 7](#step-7-connect-to-the-server-pi-later)) |
| `MEASURE_STATION_MQTT_PORT` | Usually `1883`. Leave it off until the server exists |
| `MEASURE_STATION_MQTT_USERNAME`, `MEASURE_STATION_MQTT_PASSWORD` | Only if the broker requires a login |

For example, before the server Pi exists:

```text
MEASURE_STATION_STATION_ID=mstn-2b
MEASURE_STATION_NTP_SERVERS=192.168.1.1,pool.ntp.org
```

Save with **Ctrl+O** and **Enter**, then exit with **Ctrl+X**.

**About the time server.** The Pi has no clock that keeps time while it's off, so it sets
its clock from the network each time it starts. Readings taken before that are held back
and re-timed once it has. Use a time source on your own network so this works even
without internet. It must be one that keeps real time itself (one that gets its time from
the internet or GPS, or has a battery-backed clock). Its address must never change: the
router's own address doesn't, but any other device needs a fixed one (a "DHCP
reservation" in the router's settings). See
[Choosing the Time Source](timekeeping.md#choosing-the-time-source).

### Sensor Settings

Open the main settings file:

```bash
sudo nano /opt/measure-station/src/measure_station/sysconfig.py
```

Change only these, if they apply. Use **Ctrl+W** to find each one.

| Find | Change it when | How |
|---|---|---|
| `"altitude_m": 0,` | Always | Replace `0` with the station's height above sea level, in metres. The CO2 sensor uses it when the pressure sensor is unavailable |
| `"enabled": True,` | A sensor isn't fitted | Change `True` to `False` in that sensor's section, or the log shows an error for it every minute |
| `MQBOARD_ENABLED: bool = True` | The MQ gas board isn't connected | Change `True` to `False` |

Keep the capital letters, quotes and commas exactly as they are. Save and exit as before.

**Later, once the station is mounted in its final place:** the CO2 sensor's
`temperature_offset_c` (4.0 by default) may need adjusting. See the
[CO2 Sensor](sensors/co2-sensor.md) page.

### Restart to Apply Everything

The installer's changes to the sensor bus, serial port and watchdog take effect only
after a restart:

```bash
sudo reboot
```

Wait 2 minutes, then connect with `ssh` again. The station starts by itself at every
boot.

## Step 6: Check That It Works

Work through these checks in order.

### 1. Is the Station Running?

```bash
systemctl status measure-station
```

Look for **`Active: active (running)`** in green. Press **q** to quit.

### 2. Can the Pi See Every Sensor?

Stop the station first, so the checks below have the sensors to themselves:

```bash
sudo systemctl stop measure-station
```

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

A missing number means that sensor isn't answering: check its four wires against the
table in [Step 3](#step-3-wire-the-sensors). If none appear at all, check the shared
wires to pins 1, 3, 5 and 6.

### 3. Do the Sensors Give Sensible Readings?

This tool reads one sensor every 2 seconds and prints the result. With no name, it lists
the sensors:

```bash
/opt/measure-station/.venv/bin/python -m measure_station.read_sensor
```

Then read each one by name, for example:

```bash
/opt/measure-station/.venv/bin/python -m measure_station.read_sensor temperature
```

Press **Ctrl+C** to stop it. Check that each sensor's values look plausible (the room
temperature, a humidity between 20 and 90 %, and so on). Things that are normal:

- `particulates` takes about 35 seconds to show its first reading.
- `air-quality` shows `validity=warmup` for its first 3 minutes, and `startup` for the
  first hour of a brand-new sensor.

When you're done, start the station again:

```bash
sudo systemctl start measure-station
```

### 4. What Does the Log Say?

```bash
sudo journalctl -u measure-station -f
```

This shows the station's log and keeps adding new lines. Press **Ctrl+C** to stop.

| You see | Meaning |
|---|---|
| `Running tasks: recorder, publisher, timesync, sensor:cpu, ...` | The station started. Every sensor should be in the list |
| `System clock not yet synchronised` | The Pi hasn't got the time from the network yet. Readings are kept and re-timed later. It should stop within a minute or two |
| `MQTT link down` and `Reconnecting to MQTT broker` | The broker isn't reachable. **Expected until the server Pi exists**: readings wait on the SD card |
| `Connected to MQTT broker` | Readings are being sent |
| `Task 'sensor:...' crashed`, with an error | That sensor failed. The station keeps retrying it. Check its wiring |
| `Configuration error: ...` | A setting is wrong. The station won't start until it's fixed. See [Troubleshooting](#troubleshooting) |

### 5. Has the Clock Synchronised?

```bash
timedatectl
```

Look for **`System clock synchronized: yes`**. If it says `no` for more than a few
minutes, check the time server address in Step 5.

### 6. Are Readings Being Stored?

```bash
sudo sqlite3 /var/lib/measure-station/outbox.sqlite3 'select count(*) from outbox'
```

This prints the number of readings waiting to be sent. Without a broker it rises by about
11 a minute. Once a broker is connected, it stays near zero.

**The station is deployed.** It now runs by itself, and restarts itself after a power cut
or a fault.

## Step 7: Connect to the Server Pi (Later)

Do this once the server Pi and its MQTT broker are running. Until then, the station keeps
every reading, and sends them all when it connects.

1. **Get the broker's details** from the server Pi's developer: its IP address, its port
   (usually `1883`), and a username and password if it requires a login.

   - **The address must never change.** The station finds the broker only by this
     address. Ask for the server Pi to have a fixed one, usually a "DHCP reservation" in
     the router's settings, or the router may give it a new address after a restart and
     the station will stop sending.
   - **The password should use only letters, digits, `-` and `_`.** The env file treats
     quote marks, `\` and spaces specially, so a password containing them can reach the
     broker changed, and be rejected.

2. **Add them to the station's settings.** Connect with `ssh` (as in Step 2), then open
   the file from Step 5:

   ```bash
   sudo nano /etc/measure-station/env
   ```

   Delete the `#` from the start of the `MQTT` lines and put in your values, as in Step 5.
   The lines without a `#` should then read like this (with your values):

   ```text
   MEASURE_STATION_STATION_ID=mstn-2b
   MEASURE_STATION_NTP_SERVERS=192.168.1.1,pool.ntp.org
   MEASURE_STATION_MQTT_HOST=192.168.1.20
   MEASURE_STATION_MQTT_PORT=1883
   MEASURE_STATION_MQTT_USERNAME=mstn-2b
   MEASURE_STATION_MQTT_PASSWORD=the-password
   ```

   Leave out the username and password lines if the broker has no login. As before: no
   `#` at the start, and no spaces around the `=`. Save with **Ctrl+O** and **Enter**,
   then exit with **Ctrl+X**.

3. **Restart the station** so it reads the new settings:

   ```bash
   sudo systemctl restart measure-station
   ```

4. **Check that it connected:**

   ```bash
   sudo journalctl -u measure-station -n 20
   ```

   Within a few seconds you should see `Connected to MQTT broker 192.168.1.20:1883`.
   Repeated `MQTT link down` lines mean it can't reach the broker: see
   [Troubleshooting](#troubleshooting).

5. **Watch the stored readings go out:**

   ```bash
   sudo sqlite3 /var/lib/measure-station/outbox.sqlite3 'select count(*) from outbox'
   ```

   Run it a few times. The number falls as the station sends its backlog, oldest first,
   and then stays near zero. Each reading keeps the time it was taken, so the server
   shows the whole period the station ran without it.

## Everyday Tasks

Connect with `ssh` first, as in Step 2.

| Task | Command |
|---|---|
| Is it running? | `systemctl status measure-station` (press **q** to quit) |
| Watch the log | `sudo journalctl -u measure-station -f` (**Ctrl+C** to stop) |
| The log since this morning | `sudo journalctl -u measure-station --since today` |
| Apply a changed setting | `sudo systemctl restart measure-station` |
| Stop it (e.g. to test a sensor) | `sudo systemctl stop measure-station` |
| Start it again | `sudo systemctl start measure-station` |
| Readings waiting to be sent | `sudo sqlite3 /var/lib/measure-station/outbox.sqlite3 'select count(*) from outbox'` |
| Free space on the SD card | `df -h /` (look at `Avail`) |
| Shut down safely | `sudo shutdown -h now`, then wait for the green light to stop before unplugging |

**Always shut down before unplugging** if you can. A clean stop writes the last few
seconds of readings to the SD card first; pulling the plug loses up to 10 seconds of
readings (and nothing more).

**Changing a setting:** edit the file as in Step 5, then restart the station with
`sudo systemctl restart measure-station`. Check the log afterwards for a
`Configuration error`.

**Updating the software** when the developer releases a new version:

```bash
cd /opt/measure-station
sudo git pull
sudo bash deploy/install.sh
sudo systemctl restart measure-station
```

Running the installer again after an update installs any new drivers, and prepares the
new code so the station starts quickly.

## Troubleshooting

| Problem | What to do |
|---|---|
| `ssh` says `Could not resolve hostname` | Your computer can't find the Pi by name. Find its IP address in your router's list of connected devices (look for your hostname), then use it: `ssh admin@192.168.1.57` |
| `ssh` says `Connection refused` or times out | The Pi is still starting (wait 2 more minutes), or SSH wasn't enabled in Step 1 (write the card again) |
| `ssh` warns `REMOTE HOST IDENTIFICATION HAS CHANGED` | Normal after re-writing the SD card. Clear the old entry, then connect again: `ssh-keygen -R station1.local` |
| The installer stopped with an error | Copy the last 20 lines it printed and send them to the developer. It's safe to run it again once the problem is fixed |
| `Active: failed` in `systemctl status` | Look at the log: `sudo journalctl -u measure-station -n 50`. A `Configuration error` names the setting to fix |
| A sensor is missing from `i2cdetect` | Shut down, unplug, and check that sensor's four wires. A loose jumper wire is the usual cause |
| `i2cdetect` shows nothing at all, or errors | Check the shared wires to pins 1, 3, 5 and 6. If it says the bus doesn't exist, run the installer again and reboot |
| A sensor keeps logging `crashed` | Check its wiring. If it isn't fitted, set its `"enabled"` to `False` (Step 5) |
| The humidity sensor logs a version error | The probe has the newer CHT832X chip (green SCL wire), which needs a different driver. Tell the developer |
| `sudo git pull` says `Your local changes would be overwritten` | Your edits to `sysconfig.py` clash with the update. Run `sudo git stash`, then `sudo git pull`, then `sudo git stash pop`. If that last command reports a conflict, ask the developer |
| The log keeps showing `MQTT link down` | Check `MEASURE_STATION_MQTT_HOST` and `MEASURE_STATION_MQTT_PORT` in the env file for typos, and that the server Pi is on: `ping -c 3 192.168.1.20` (its address) should get replies. No replies, but the server Pi is on: its address has probably changed. Find its new one in the router's list of connected devices, give it a fixed address (Step 7), and correct the env file. Replies: the broker may not be running, or may reject the username or password: ask the server Pi's developer. Readings wait on the station meanwhile |
| `System clock synchronized: no` for a long time | Check `MEASURE_STATION_NTP_SERVERS` in the env file (if the time server isn't the router, its address may have changed), and that the time server is reachable: `timedatectl timesync-status` shows which server it's trying |
| Random sensor errors or restarts | Often a weak power supply. Try a better one (2.5 A) |
