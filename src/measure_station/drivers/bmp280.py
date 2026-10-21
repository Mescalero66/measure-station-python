# Asynchronous Python driver for:
# Generic BMP280 barometric pressure sensor breakout (Bosch Sensortec BMP280), e.g. the 6-pin GY-BMP280 board
#
# Based on the Bosch BMP280 datasheet BST-BMP280-DS001-26, rev 1.26 (October 2021). Section numbers in comments refer to it.

import asyncio
import struct
import time
from dataclasses import dataclass
from typing import Optional

from smbus2 import SMBus, i2c_msg


@dataclass
class Measurement:
    temperature: Optional[float]    # °C, None if skipped (oversampling 0)
    pressure: Optional[float]       # Pa, None if skipped


def _div(a, b):
    """C integer division, which truncates toward zero (Python's // floors)."""
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


class BMP280:
    ADDRESS = 0x76                                  # SDO to GND (generic boards). SDO to VDDIO gives 0x77 (§5.2)

    # Registers (§4.2, Table 18)
    CALIB                   = 0x88                  # calib00…calib23: dig_T1…dig_P9 (§3.11.2, Table 17)
    ID                      = 0xD0
    RESET                   = 0xE0
    STATUS                  = 0xF3
    CTRL_MEAS               = 0xF4
    CONFIG                  = 0xF5
    PRESS_MSB               = 0xF7                  # burst read 0xF7…0xFC: pressure then temperature (§3.9)

    RESET_WORD              = 0xB6                  # §4.3.2
    MEASURING               = 0x08                  # status bit 3 (Table 19)
    IM_UPDATE               = 0x01                  # status bit 0
    SKIPPED                 = 0x80000               # raw output of a skipped measurement (§3.3.1)

    CHIP_IDS = {0x58: "BMP280", 0x60: "BME280"}     # §4.3.1. A BME280 has the same T/P registers

    # Power modes, mode[1:0] (§3.6, Table 10)
    SLEEP                   = 0b00
    FORCED                  = 0b01
    NORMAL                  = 0b11

    OVERSAMPLING = (0, 1, 2, 4, 8, 16)              # ×, osrs codes 000–101 (Tables 21, 22). 0 = skipped
    FILTERS = (0, 2, 4, 8, 16)                      # IIR coefficient, filter codes 000–100 (§3.3.3). 0 = off
    STANDBY_MS = (0.5, 62.5, 125, 250, 500, 1000, 2000, 4000)  # t_sb codes 000–111 (Table 11)

    # Recommended settings per use case (§3.4, Tables 7 and 15): mode, osrs_p, osrs_t, filter, t_standby (ms)
    PRESETS = {
        "handheld_low_power":   (NORMAL, 16, 2, 4, 62.5),     # 10 Hz
        "handheld_dynamic":     (NORMAL, 4, 1, 16, 0.5),      # 83 Hz
        "weather":              (FORCED, 1, 1, 0, 0.5),       # one measure_single_shot() a minute
        "elevator":             (NORMAL, 4, 1, 4, 125),       # 7.3 Hz
        "drop":                 (NORMAL, 2, 1, 0, 0.5),       # 125 Hz
        "indoor_navigation":    (NORMAL, 16, 2, 16, 0.5),     # 26 Hz
    }

    def __init__(self, bus=1, address=ADDRESS):
        self.address = address
        self._bus = SMBus(bus)
        self._lock = asyncio.Lock()                 # one register sequence at a time
        self._cal = None                            # dig_T1…dig_P9, read in init()
        self._osrs_p = self._osrs_t = 0             # copies of the register settings, for timing
        self._t_sb = 0.5
        self._due = time.monotonic()                # when measurements() expects the next sample
        self._last_raw = None

    # ---- Context manager: reset, check the ID, read calibration; sleep on exit ----

    async def __aenter__(self):
        await self.init()
        return self

    async def __aexit__(self, *exc):
        try:
            await self.set_mode(self.SLEEP)         # 0.1 µA
        finally:
            self.close()

    def close(self):
        self._bus.close()

    async def init(self):
        """Soft reset, check the chip ID, read the calibration, then select ×16 / ×2 oversampling. Leaves the sensor asleep."""
        await self.reset()
        chip = await self.get_chip_id()
        if chip not in self.CHIP_IDS:
            raise RuntimeError(f"no BMP280 at 0x{self.address:02X} (chip ID 0x{chip:02X})")
        self._cal = struct.unpack("<HhhHhhhhhhhh", await self._read(self.CALIB, 24))
        await self.set_oversampling(16, 2)          # ultra high resolution (Table 4)
        return self

    # ---- Low-level I2C helpers (§5.2) ----

    def _write_reg(self, reg, value):
        self._bus.i2c_rdwr(i2c_msg.write(self.address, [reg, value]))

    def _read_regs(self, reg, n=1):
        """Register address, repeated start, then an auto-incremented burst read (§5.2.2)."""
        rd = i2c_msg.read(self.address, n)
        self._bus.i2c_rdwr(i2c_msg.write(self.address, [reg]), rd)
        return bytes(rd)

    async def _read(self, reg, n=1):
        async with self._lock:
            return self._read_regs(reg, n)

    async def _wait_idle(self):
        """Called with the lock held. Waits until no measurement is running."""
        while self._read_regs(self.STATUS)[0] & self.MEASURING:
            await asyncio.sleep(0.001)

    async def _enter_sleep(self, ctrl):
        """Called with the lock held. A mode change waits for the running measurement to finish (§3.6.4)."""
        self._write_reg(self.CTRL_MEAS, ctrl & 0xFC)
        await self._wait_idle()

    async def _set_config(self, mask, bits):
        """'config' writes may be ignored in normal mode (§4.3.5), so drop to sleep for the write, then restore."""
        async with self._lock:
            ctrl = self._read_regs(self.CTRL_MEAS)[0]
            normal = (ctrl & 0x03) == self.NORMAL
            if normal:
                await self._enter_sleep(ctrl)
            config = self._read_regs(self.CONFIG)[0]
            self._write_reg(self.CONFIG, (config & ~mask & 0xFF) | bits)     # keeps spi3w_en
            if normal:
                self._write_reg(self.CTRL_MEAS, ctrl)

    @staticmethod
    def _code(table, value, what):
        if value not in table:
            raise ValueError(f"{what} must be one of {table}")
        return table.index(value)

    # ---- Identification, reset and status (§4.3.1–4.3.3) ----

    async def get_chip_id(self):
        """0x58 for a BMP280 (0x60 for a BME280)."""
        return (await self._read(self.ID))[0]

    async def reset(self):
        """Soft reset (the full power-on-reset). Settings return to sleep, measurements skipped, filter off."""
        async with self._lock:
            self._write_reg(self.RESET, self.RESET_WORD)
            await asyncio.sleep(0.002)              # t_startup (Table 2)
            while self._read_regs(self.STATUS)[0] & self.IM_UPDATE:     # NVM still being copied
                await asyncio.sleep(0.001)
        self._osrs_p = self._osrs_t = 0
        self._t_sb = 0.5

    async def get_status(self):
        """Returns (measuring, im_update)."""
        status = (await self._read(self.STATUS))[0]
        return bool(status & self.MEASURING), bool(status & self.IM_UPDATE)

    # ---- Measurement settings: register 0xF4 "ctrl_meas" (§4.3.4) ----

    async def get_mode(self):
        """SLEEP, FORCED or NORMAL. Forced mode reads back as SLEEP once its measurement is done."""
        mode = (await self._read(self.CTRL_MEAS))[0] & 0x03
        return self.FORCED if mode == 0b10 else mode   # 01 and 10 both mean forced

    async def set_mode(self, mode):
        """SLEEP or NORMAL (use measure_single_shot() for forced mode). Sleep waits for any running measurement."""
        async with self._lock:
            ctrl = self._read_regs(self.CTRL_MEAS)[0]
            if mode == self.SLEEP:
                await self._enter_sleep(ctrl)
            else:
                self._write_reg(self.CTRL_MEAS, (ctrl & 0xFC) | mode)
        if mode == self.NORMAL:                     # first sample lands one measurement time from now
            self._last_raw = None
            self._due = time.monotonic() + self.measurement_time()

    async def get_oversampling(self):
        """Returns (pressure, temperature) oversampling. 0 = skipped."""
        ctrl = (await self._read(self.CTRL_MEAS))[0]
        return self.OVERSAMPLING[min((ctrl >> 2) & 7, 5)], self.OVERSAMPLING[min(ctrl >> 5, 5)]

    async def set_oversampling(self, pressure=16, temperature=2):
        """0 (skip), 1, 2, 4, 8 or 16 each. Table 4 recommends temperature ×2 with pressure ×16, otherwise ×1."""
        p = self._code(self.OVERSAMPLING, pressure, "pressure oversampling")
        t = self._code(self.OVERSAMPLING, temperature, "temperature oversampling")
        async with self._lock:
            ctrl = self._read_regs(self.CTRL_MEAS)[0]
            self._write_reg(self.CTRL_MEAS, (t << 5) | (p << 2) | (ctrl & 0x03))
        self._osrs_p, self._osrs_t = pressure, temperature

    # ---- Rate and filter settings: register 0xF5 "config" (§4.3.5) ----

    async def get_filter(self):
        """IIR filter coefficient. 0 = off."""
        return self.FILTERS[min(((await self._read(self.CONFIG))[0] >> 2) & 7, 4)]

    async def set_filter(self, coefficient):
        """0 (off), 2, 4, 8 or 16. Writing it resets the filter (§3.3.3)."""
        await self._set_config(0x1C, self._code(self.FILTERS, coefficient, "filter") << 2)

    async def get_standby(self):
        """Normal-mode standby time between measurements, in ms."""
        return self.STANDBY_MS[(await self._read(self.CONFIG))[0] >> 5]

    async def set_standby(self, ms):
        """0.5, 62.5, 125, 250, 500, 1000, 2000 or 4000 ms (§3.6.3)."""
        await self._set_config(0xE0, self._code(self.STANDBY_MS, ms, "standby") << 5)
        self._t_sb = ms

    async def apply_preset(self, name):
        """Settings for one of the use cases in PRESETS (Table 7). Normal-mode presets start measuring,
        'weather' leaves the sensor asleep for measure_single_shot()."""
        mode, osrs_p, osrs_t, coefficient, t_sb = self.PRESETS[name]
        await self.set_mode(self.SLEEP)
        await self.set_oversampling(osrs_p, osrs_t)
        await self.set_filter(coefficient)
        await self.set_standby(t_sb)
        if mode == self.NORMAL:
            await self.set_mode(self.NORMAL)

    # ---- Timing (§3.8) ----

    def measurement_time(self, typical=False):
        """Measurement time in seconds for the current oversampling: maximum (default) or typical.
        These formulas reproduce every row of Table 13."""
        t, p = self._osrs_t, self._osrs_p
        if typical:
            ms = 1 + 2 * t + (2 * p + 0.5 if p else 0)
        else:
            ms = 1.25 + 2.3 * t + (2.3 * p + 0.575 if p else 0)
        return ms / 1000

    def output_data_rate(self):
        """Typical normal-mode samples per second, 1 / (t_measure + t_standby) (§3.8.2, Table 14)."""
        return 1 / (self.measurement_time(typical=True) + self._t_sb / 1000)

    # ---- Readout and compensation (§3.9–3.11) ----

    async def _read_raw(self):
        """One burst read, so shadowing keeps pressure and temperature from the same cycle (§3.10)."""
        d = await self._read(self.PRESS_MSB, 6)
        return d[0] << 12 | d[1] << 4 | d[2] >> 4, d[3] << 12 | d[4] << 4 | d[5] >> 4

    def _compensate(self, adc_p, adc_t):
        if self._cal is None:
            raise RuntimeError("BMP280 not initialised, use 'async with' or init()")
        if adc_t == self.SKIPPED:
            return Measurement(None, None)          # pressure compensation needs t_fine
        T1, T2, T3, P1, P2, P3, P4, P5, P6, P7, P8, P9 = self._cal

        # Temperature: 32-bit integer formula (§3.11.3). Python's >> is arithmetic, like C on signed ints.
        var1 = (((adc_t >> 3) - (T1 << 1)) * T2) >> 11
        var2 = (((((adc_t >> 4) - T1) * ((adc_t >> 4) - T1)) >> 12) * T3) >> 14
        t_fine = var1 + var2
        temperature = ((t_fine * 5 + 128) >> 8) / 100
        if adc_p == self.SKIPPED:
            return Measurement(temperature, None)

        # Pressure: 64-bit integer formula (§3.11.3), result in Pa as Q24.8. Kept identical to the
        # MicroPython driver, where integers stay exact and single-precision floats would not.
        var1 = t_fine - 128000
        var2 = var1 * var1 * P6
        var2 += (var1 * P5) << 17
        var2 += P4 << 35
        var1 = ((var1 * var1 * P3) >> 8) + ((var1 * P2) << 12)
        var1 = (((1 << 47) + var1) * P1) >> 33
        if var1 == 0:
            return Measurement(temperature, None)   # avoid division by zero
        p = 1048576 - adc_p
        p = _div(((p << 31) - var2) * 3125, var1)
        var1 = (P9 * (p >> 13) * (p >> 13)) >> 25
        var2 = (P8 * p) >> 19
        p = ((p + var1 + var2) >> 8) + (P7 << 4)
        return Measurement(temperature, p / 256)

    async def read_measurement(self):
        """Compensate the latest result in the data registers, without waiting."""
        return self._compensate(*await self._read_raw())

    async def measure_single_shot(self):
        """Forced mode (§3.6.2): one measurement at the current settings, then the sensor goes back to sleep."""
        async with self._lock:
            ctrl = self._read_regs(self.CTRL_MEAS)[0]
            self._write_reg(self.CTRL_MEAS, (ctrl & 0xFC) | self.FORCED)
            await asyncio.sleep(self.measurement_time())    # maximum measurement time (§3.8.1)
            await self._wait_idle()
        return await self.read_measurement()

    async def measurements(self):
        """Async generator for normal mode: yields each new sample (one per output-data period, §3.8.2)."""
        while True:
            period = 1 / self.output_data_rate()
            wait = self._due - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            for _ in range(8):                      # the standby timer is only ±5 % accurate (±25 % max, Table 2)
                raw = await self._read_raw()
                if raw != self._last_raw:
                    break
                await asyncio.sleep(period / 8)     # still the previous sample, so check again shortly
            self._last_raw = raw
            self._due = time.monotonic() + period
            yield self._compensate(*raw)

    # ---- Altitude helpers (international barometric formula) ----

    @staticmethod
    def altitude(pressure, sea_level=101325.0):
        """Metres above the level where the pressure is sea_level (Pa). Use the local QNH for absolute height."""
        return 44330.0 * (1.0 - (pressure / sea_level) ** 0.1903)

    @staticmethod
    def sea_level_pressure(pressure, altitude):
        """QNH (Pa) from a pressure measured at a known altitude (m)."""
        return pressure / (1.0 - altitude / 44330.0) ** 5.255
