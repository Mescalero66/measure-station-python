#!/usr/bin/env bash
# Provision a fresh Raspberry Pi OS (Lite) install to run Measure Station.
#
# Usage (from a checkout, ideally at /opt/measure-station):
#     sudo ./deploy/install.sh
#
# Safe to re-run: every step is idempotent. EXTRAS lists the optional sensor driver
# dependencies to install (comma-separated); it defaults to those of the sensors enabled
# in sysconfig.py, e.g.  sudo EXTRAS="ens160_aht21,bh1750" ./deploy/install.sh
set -euo pipefail

APP_NAME=measure-station
APP_USER=measure-station
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$APP_DIR/.venv"
ETC_DIR="/etc/$APP_NAME"
EXTRAS="${EXTRAS-ens160_aht21,bmp280,scd41}"

log() { printf '\n==> %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root: sudo $0" >&2
    exit 1
fi

log "Installing system packages"
apt-get update
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip git sqlite3 i2c-tools

log "Enabling I2C and the hardware UART (disabling the serial login console)"
if command -v raspi-config >/dev/null; then
    raspi-config nonint do_i2c 0
    # Newer raspi-config splits UART hardware and console; older uses do_serial 2.
    if raspi-config nonint do_serial_hw 0 2>/dev/null; then
        raspi-config nonint do_serial_cons 1
    else
        raspi-config nonint do_serial 2
    fi
else
    echo "raspi-config not found; enable I2C and UART manually." >&2
fi

log "Enabling NTP time synchronisation"
timedatectl set-ntp true || true

log "Creating service user '$APP_USER'"
if ! id "$APP_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$APP_USER"
fi
for group in dialout i2c gpio spi; do
    if getent group "$group" >/dev/null; then
        usermod -aG "$group" "$APP_USER"
    fi
done

log "Creating Python virtual environment at $VENV_DIR"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip
# Editable install: edits to src/measure_station/sysconfig.py apply on service restart.
"$VENV_DIR/bin/pip" install -e "$APP_DIR${EXTRAS:+[$EXTRAS]}"
# Pre-compile bytecode: the service can't write __pycache__ (ProtectSystem=strict),
# and compiling on every start is slow on a Pi 2B.
"$VENV_DIR/bin/python" -m compileall -q "$APP_DIR/src"

log "Installing configuration in $ETC_DIR"
install -d -m 0750 -o root -g "$APP_USER" "$ETC_DIR"
if [[ ! -f "$ETC_DIR/env" ]]; then
    install -m 0640 -o root -g "$APP_USER" "$APP_DIR/deploy/env.example" "$ETC_DIR/env"
fi

log "Configuring hardware watchdog and journald limits"
install -D -m 0644 "$APP_DIR/deploy/systemd/watchdog.conf" \
    /etc/systemd/system.conf.d/$APP_NAME-watchdog.conf
install -D -m 0644 "$APP_DIR/deploy/systemd/journald.conf" \
    /etc/systemd/journald.conf.d/$APP_NAME.conf

log "Installing systemd service"
sed -e "s#@APP_DIR@#$APP_DIR#g" -e "s#@APP_USER@#$APP_USER#g" \
    "$APP_DIR/deploy/systemd/$APP_NAME.service" > "/etc/systemd/system/$APP_NAME.service"
systemctl daemon-reload
systemctl enable "$APP_NAME.service"

cat <<EOF

Installation complete.

Next steps:
  1. Edit settings:   sudo nano $APP_DIR/src/measure_station/sysconfig.py
  2. Edit secrets:    sudo nano $ETC_DIR/env
  3. Reboot to apply I2C/UART/watchdog changes:   sudo reboot
  4. Check status:    systemctl status $APP_NAME
     Follow logs:     journalctl -u $APP_NAME -f
EOF
