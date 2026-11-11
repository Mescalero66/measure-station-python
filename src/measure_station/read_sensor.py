"""Print one sensor's readings to the terminal, for checking its wiring on the Pi.
See docs/testing-the-station.md.

    python -m measure_station.read_sensor               # list the sensors in sysconfig.py
    python -m measure_station.read_sensor air-quality   # read it every 10 s until Ctrl+C

It uses the sensor's entry in SENSORS (even if it isn't enabled) and the same driver as
the service. While it runs, the fans run too, whichever sensor is read, so the sensor
sees the airflow it will have in service: the exhaust fans at 100 %, and the intake fans
at 100 % for FAN_KICKSTART_S, then at FAN_ANALOG_MIN_DUTY_PCT. On exit, the fans are left
as the stopped service leaves them: exhaust at 100 %, intake off. If FAN_ENABLED is
False, the fans are left alone; if they can't be started, it says so and reads anyway.

Stop the service first, since it uses the same buses and fan outputs:

    sudo systemctl stop measure-station
"""

from __future__ import annotations

import contextlib
import sys
import time

from measure_station import sysconfig
from measure_station.fan import PwmOutput
from measure_station.main import fan_outputs
from measure_station.sensors import create_sensor

INTERVAL_S = 10.0


def main(argv: list[str]) -> int:
    entries = {entry["name"]: entry for entry in sysconfig.SENSORS}
    if len(argv) != 1 or argv[0] not in entries:
        print("Usage: python -m measure_station.read_sensor <name>\n\nSensors in sysconfig.py:")
        for name, entry in entries.items():
            print(f"  {name:<20} {entry['type']}")
        return 2

    sensor = create_sensor(entries[argv[0]])
    fans: tuple[PwmOutput, PwmOutput] | None = None
    try:
        fans = _start_fans()
        print(f"Opening {sensor!r}. Press Ctrl+C to stop.")
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
        if fans is not None:
            _stop_fans(*fans)


def _start_fans() -> tuple[PwmOutput, PwmOutput] | None:
    """Exhaust at 100 %; intake kick-started at 100 %, then at the minimum speed.

    Returns the outputs, or None if fans are disabled or couldn't be started.
    """
    if not sysconfig.FAN_ENABLED:
        print("Fans: FAN_ENABLED is False, so they're left as they are.")
        return None
    exhaust, intake = fan_outputs(sysconfig)
    try:
        for output in (exhaust, intake):
            output.open()
            output.set_duty(100)
    except OSError as exc:
        print(f"Fans: couldn't start them ({exc}). Reading without them.")
        return None
    try:
        print(f"Fans: exhaust 100 %, intake 100 % for {sysconfig.FAN_KICKSTART_S:g} s...")
        time.sleep(sysconfig.FAN_KICKSTART_S)
        intake.set_duty(sysconfig.FAN_ANALOG_MIN_DUTY_PCT)
    except BaseException:  # e.g. Ctrl+C during the kick-start
        _stop_fans(exhaust, intake)
        raise
    print(f"Fans: exhaust 100 %, intake {sysconfig.FAN_ANALOG_MIN_DUTY_PCT:g} %.")
    return exhaust, intake


def _stop_fans(exhaust: PwmOutput, intake: PwmOutput) -> None:
    """Leave the fans as the stopped service does: exhaust at 100 %, intake off."""
    for output, duty in ((exhaust, 100), (intake, 0)):
        with contextlib.suppress(OSError):
            output.set_duty(duty)
            output.close()
    print("Fans: exhaust 100 %, intake off (as the stopped service leaves them).")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
