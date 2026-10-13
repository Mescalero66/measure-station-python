# Raspberry Pi Setup (Fresh Install)

1. **Flash the OS.** In Raspberry Pi Imager, choose **Raspberry Pi OS Lite (32-bit)**,
   which the Pi 2B needs. In the advanced options, set the hostname (this becomes the
   default `STATION_ID`), enable SSH and create your user.

2. **Clone and install.** Over SSH:

   ```bash
   sudo apt-get update && sudo apt-get install -y git
   sudo git clone <repo-url> /opt/measure-station
   sudo bash /opt/measure-station/deploy/install.sh
   ```

   The installer:
   - installs system packages
   - enables I2C and the hardware UART, and turns off the serial login console
   - enables NTP
   - creates a `measure-station` system user with `i2c`, `dialout` and `gpio` access
   - builds a virtualenv in `/opt/measure-station/.venv`
   - installs the systemd service, hardware watchdog and journald limits

   It is safe to re-run.

3. **Configure.**

   ```bash
   sudo nano /opt/measure-station/src/measure_station/sysconfig.py   # broker, NTP, sensors, MQ board
   sudo nano /etc/measure-station/env                                # MQTT credentials
   ```

   If the station can't reach the internet, set `NTP_SERVERS` to a local time server.
   See [Using a Local NTP Server](timekeeping.md#using-a-local-ntp-server).

4. **Reboot** to apply the I2C, UART and watchdog changes: `sudo reboot`.
   The service starts automatically on boot.
