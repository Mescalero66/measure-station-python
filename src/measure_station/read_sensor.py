"""Print one sensor's readings to the terminal, for checking its wiring on the Pi.

    python -m measure_station.read_sensor               # list the sensors in sysconfig.py
    python -m measure_station.read_sensor air-quality   # read it every 2 s until Ctrl+C

It uses the sensor's entry in SENSORS (even if it isn't enabled) and the same driver as
the service. Stop the service first, since it uses the same buses:

    sudo systemctl stop measure-station
"""

from __future__ import annotations

import sys
import time

from measure_station import sysconfig
from measure_station.sensors import create_sensor

INTERVAL_S = 2.0


def main(argv: list[str]) -> int:
    entries = {entry["name"]: entry for entry in sysconfig.SENSORS}
    if len(argv) != 1 or argv[0] not in entries:
        print("Usage: python -m measure_station.read_sensor <name>\n\nSensors in sysconfig.py:")
        for name, entry in entries.items():
            print(f"  {name:<20} {entry['type']}")
        return 2

    sensor = create_sensor(entries[argv[0]])
    print(f"Opening {sensor!r}. Press Ctrl+C to stop.")
    try:
        sensor.open()
        while True:
            sample = sensor.read()
            line = "  ".join(f"{k}={v}" for k, v in sample.values.items())
            if sample.meta:
                line += "  | meta: " + "  ".join(f"{k}={v}" for k, v in sample.meta.items())
            print(time.strftime("%H:%M:%S"), line)
            time.sleep(INTERVAL_S)
    except KeyboardInterrupt:
        return 0
    finally:
        sensor.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
