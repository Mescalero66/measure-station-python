"""ENS160 air-quality sensor and AHT21 temperature/humidity sensor on one I2C board.

Station adapter for the verbatim driver in ``measure_station.drivers.ens160_aht21``. See
docs/sensors/air-quality-board.md for wiring and how to interpret the values.

Values:
    aqi:            air quality index, UBA scale: 1 excellent, 2 good, 3 moderate,
                    4 poor, 5 unhealthy.
    tvoc_ppb:       total volatile organic compounds, 0-65000 ppb.
    eco2_ppm:       equivalent CO2, estimated from the VOCs (not measured CO2),
                    400-65000 ppm.

Meta:
    validity:       the ENS160's own data quality flag: "normal"; "warmup" (3 min after
                    every start); "startup" (first hour of a new sensor); "invalid".
                    Only trust the values when it is "normal".
    temperature_c:  temperature on the board, in °C, from the AHT21.
    humidity_pct:   relative humidity on the board, 0-100 %, from the AHT21.

temperature_c and humidity_pct are the compensation inputs given to the ENS160. They
are measured next to its heater, so they read warmer and drier than the room.

Requires: pip install -e ".[ens160_aht21]"   (smbus2)
"""

from __future__ import annotations

import logging
import time
from typing import Any

from measure_station.sensors.base import Sample, Sensor

log = logging.getLogger(__name__)

_ENS160_ADDRESSES = (0x52, 0x53)  # set by the board's ADD pin (ENS §14.1.1)
# The ENS160's validity codes 0-3 (ENS §11, Table 10), published as text.
VALIDITY = ("normal", "warmup", "startup", "invalid")
# Longest wait for the first sample after starting STANDARD mode; on hardware it came
# about 2 s after. Twice this, plus a reset, stays within SENSOR_READ_TIMEOUT_S (10 s).
_FIRST_SAMPLE_S = 3.0
_POLL_S = 0.1


class Ens160Aht21Sensor(Sensor):
    def __init__(
        self,
        name: str,
        interval_s: float,
        i2c_bus: int = 1,
        ens160_address: int | None = None,
    ) -> None:
        super().__init__(name, interval_s)
        if ens160_address is not None and ens160_address not in _ENS160_ADDRESSES:
            raise ValueError("ens160_address must be 0x52, 0x53 or None (find it on the bus)")
        self._i2c_bus = i2c_bus
        self._ens160_address = ens160_address
        self._i2c: Any = None
        self._board: Any = None

    def open(self) -> None:
        # Imported here: the driver imports smbus2 at the top of the module.
        from measure_station.drivers.ens160_aht21 import ENS160_AHT21, I2C

        self._i2c = I2C(self._i2c_bus)
        self._board = ENS160_AHT21(self._i2c, self._ens160_address)
        # Starts STANDARD mode, which restarts the ENS160's 3 minute warm-up (validity 1).
        self._board.begin()
        ens160 = self._board.ens160
        # Wait for its first sample, so the first read can't return the empty (all 0)
        # data registers.
        if self._first_sample(ens160):
            return
        # Seen on hardware: OPMODE still read STANDARD, but it had stopped measuring
        # (DEVICE_STATUS 0x03, data all 0), and stayed so when its power was removed with
        # the I2C lines still connected (which may have kept it powered). A software
        # reset restarted it. Reset only when needed: it came back with validity 2, so a
        # reset may restart its 1 hour start-up phase.
        log.warning("%s: the ENS160 isn't measuring; resetting it", self.name)
        ens160.reset()
        ens160.set_mode(ens160.MODE_STANDARD)
        if not self._first_sample(ens160):
            raise OSError("ENS160 isn't measuring, even after a reset")

    @staticmethod
    def _first_sample(ens160: Any) -> bool:
        """Whether it's measuring, with a sample ready, within _FIRST_SAMPLE_S."""
        deadline = time.monotonic() + _FIRST_SAMPLE_S
        while True:
            status = ens160.status()  # reading it doesn't clear NEWDAT (only a data read does)
            if status.running and status.new_data:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_POLL_S)

    def read(self) -> Sample:
        ens160 = self._board.ens160
        # A brown-out resets the ENS160 to deep sleep, where its data stops updating.
        # Raising makes the station close and re-open the sensor, which restarts it.
        mode = ens160.mode()
        if mode != ens160.MODE_STANDARD:
            raise OSError(f"ENS160 is not in STANDARD mode (OPMODE 0x{mode:02X})")
        status = ens160.status()
        if status.error:
            raise OSError("ENS160 reports an error (DEVICE_STATUS STATER)")
        # It can also stop measuring while OPMODE still reads STANDARD. Its data is then
        # all 0, and validity 0 ("normal"), so this bit is the only sign.
        if not status.running:
            raise OSError("ENS160 has stopped measuring (DEVICE_STATUS STATAS low)")

        reading = self._board.read()
        if reading.aqi == 0:  # the UBA index is 1-5: 0 means no result
            raise OSError("ENS160 returned AQI 0 (no result)")
        return Sample(
            values={"aqi": reading.aqi, "tvoc_ppb": reading.tvoc, "eco2_ppm": reading.eco2},
            meta={
                "validity": VALIDITY[reading.validity],
                "temperature_c": round(reading.pcb_temp, 2),
                "humidity_pct": round(reading.pcb_humi, 1),
            },
        )

    def close(self) -> None:
        if self._i2c is not None:
            self._i2c.close()
            self._i2c = None
        self._board = None
