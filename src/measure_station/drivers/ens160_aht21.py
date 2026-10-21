# Python driver for ENS160 + AHT21 combo boards on a Raspberry Pi: a ScioSense ENS160 air-quality
# sensor and an ASAIR (Aosong) AHT21 temperature/humidity sensor sharing one I2C bus.
#
# Written from the datasheets. Section numbers in comments refer to them:
#   "ENS §"  ScioSense ENS160 datasheet v1.3 (2023-03-29)
#   "AHT §"  ASAIR AHT21 datasheet v1.0 (May 2021)
#
# Requires smbus2. Blocking methods work everywhere; methods ending in _async are for asyncio.

import asyncio
import time
from collections import namedtuple

from smbus2 import SMBus, i2c_msg


# DEVICE_STATUS bits (ENS §16.2.7). validity is 0-3, see ENS160.VALIDITY
Status = namedtuple("Status", ("running", "error", "validity", "new_data", "new_gpr"))
# aqi: UBA index 1-5, tvoc in ppb, eco2 in ppm (ENS §5)
AirQuality = namedtuple("AirQuality", ("aqi", "tvoc", "eco2"))
# Air quality, the ENS160 validity flag (0-3, see ENS160.VALIDITY), and the AHT21 values sent to the
# ENS160 as compensation: pcb_temp in °C, pcb_humi in %RH. These are the conditions on the board,
# warmed by the ENS160 heater, not room conditions.
Reading = namedtuple("Reading", ("aqi", "tvoc", "eco2", "validity", "pcb_temp", "pcb_humi"))


def _timed_out(start, timeout_ms):
    return (time.monotonic() - start) * 1000 > timeout_ms


def _sleep_ms(ms):
    time.sleep(ms / 1000)


# =====================================================================================================
# I2C bus
# =====================================================================================================

class I2C:
    """A Linux I2C bus (/dev/i2c-N) with the same methods as MicroPython's machine.I2C.

    Every transfer goes through i2c_rdwr, because the AHT21 needs plain reads and writes with no
    register byte, which the SMBus block calls can't do. Register reads are a write of the register
    address, a repeated START, then the read, as in ENS §14.1.3.
    The bus speed is set by the kernel (dtparam=i2c_arm_baudrate in config.txt), not here."""

    def __init__(self, bus=1):
        """bus: a bus number (1 is GPIO2/GPIO3 on the Pi header) or an open smbus2.SMBus."""
        self._own = isinstance(bus, int)
        self.bus = SMBus(bus) if self._own else bus

    def close(self):
        if self._own:
            self.bus.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def readfrom(self, addr, n):
        msg = i2c_msg.read(addr, n)
        self.bus.i2c_rdwr(msg)
        return bytes(msg)

    def writeto(self, addr, data):
        self.bus.i2c_rdwr(i2c_msg.write(addr, bytes(data)))

    def readfrom_mem(self, addr, reg, n):
        reg_msg = i2c_msg.write(addr, bytes((reg,)))
        data_msg = i2c_msg.read(addr, n)
        self.bus.i2c_rdwr(reg_msg, data_msg)
        return bytes(data_msg)

    def writeto_mem(self, addr, reg, data):
        self.bus.i2c_rdwr(i2c_msg.write(addr, bytes((reg,)) + bytes(data)))

    def probe(self, addr):
        """True if a device acknowledges a 1-byte read at addr."""
        try:
            self.readfrom(addr, 1)
            return True
        except OSError:
            return False


# =====================================================================================================
# AHT21 temperature and humidity sensor
# =====================================================================================================

class AHT21:
    ADDRESS = 0x38                                          # fixed (AHT §7.3)

    CMD_TRIGGER = b"\xAC\x33\x00"                           # trigger measurement (AHT §7.4 step 2)

    # Status byte (AHT §7.3, Table 9)
    STATUS_BUSY = 0x80                                      # bit 7: measurement in progress
    STATUS_CAL  = 0x08                                      # bit 3: calibrated
    STATUS_INIT = 0x18                                      # bits 4 and 3 must both be set (AHT §7.4 step 1)

    CALIBRATION_REGS = (0x1B, 0x1C, 0x1E)                   # re-initialised if STATUS_INIT is not set

    POWER_ON_MS = 100                                       # idle >= 100 ms after power-on (AHT §7.1)
    MEASURE_MS  = 80                                        # conversion time (AHT §7.4 step 3)
    POLL_MS     = 10
    TIMEOUT_MS  = 1000

    def __init__(self, i2c, address=ADDRESS, check_crc=True):
        """i2c: an I2C object from this module. Max bus speed 400 kHz (AHT §4.4, Table 8)."""
        self.i2c = i2c
        self.address = address
        self.check_crc = check_crc

    def begin(self):
        """Check the calibration status and initialise the sensor if needed (AHT §7.4 step 1).
        Only needed once after power-on. Raises OSError if the sensor stays uninitialised."""
        _sleep_ms(self.POWER_ON_MS)
        if self.status() & self.STATUS_INIT != self.STATUS_INIT:
            for reg in self.CALIBRATION_REGS:
                self._init_register(reg)
            _sleep_ms(10)
            if self.status() & self.STATUS_INIT != self.STATUS_INIT:
                raise OSError(f"AHT21: not initialised, status 0x{self.status():02X}")
        _sleep_ms(10)                                       # wait 10 ms before the first trigger (AHT §7.4 step 2)

    def _init_register(self, reg):
        # AHT §7.4 refers to the manufacturer's example code for this step and doesn't describe it.
        # The ASAIR example reads the register's two bytes, then writes them back with 0xB0 OR'd into
        # the register address.
        self.i2c.writeto(self.address, bytes((reg, 0x00, 0x00)))
        _sleep_ms(5)
        d = self.i2c.readfrom(self.address, 3)
        _sleep_ms(10)
        self.i2c.writeto(self.address, bytes((0xB0 | reg, d[1], d[2])))

    def status(self):
        """The status byte (AHT §7.3, Table 9). AHT §7.4's "send 0x71" is just the read address
        byte, (0x38 << 1) | 1, so this is a plain 1-byte read."""
        return self.i2c.readfrom(self.address, 1)[0]

    # ---- Split measurement: trigger(), wait >= 80 ms, then result() ----

    def trigger(self):
        """Start a measurement (AHT §7.4 step 2)."""
        self.i2c.writeto(self.address, self.CMD_TRIGGER)

    def result(self):
        """Read a triggered measurement. Returns (temperature °C, humidity %RH), or None while the
        sensor is still busy. Raises OSError on a CRC mismatch."""
        # One read returns status, 20-bit humidity, 20-bit temperature, then CRC (AHT §7.4, p.13)
        d = self.i2c.readfrom(self.address, 7)
        if d[0] & self.STATUS_BUSY:
            return None
        if self.check_crc and self.crc8(d[:6]) != d[6]:
            raise OSError("AHT21: CRC mismatch")
        s_rh = (d[1] << 12) | (d[2] << 4) | (d[3] >> 4)
        s_t = ((d[3] & 0x0F) << 16) | (d[4] << 8) | d[5]
        # AHT §8.1 and §8.2, 2^20 = 1048576
        return s_t * 200 / 1048576 - 50, s_rh * 100 / 1048576

    # ---- Complete measurement ----

    def measure(self):
        """Trigger a measurement, wait for it and return (temperature °C, humidity %RH).
        To limit self-heating, measure no more than once a second (AHT §4.4)."""
        self.trigger()
        _sleep_ms(self.MEASURE_MS)
        start = time.monotonic()
        while True:
            r = self.result()
            if r is not None:
                return r
            if _timed_out(start, self.TIMEOUT_MS):
                raise OSError("AHT21: measurement timed out")
            _sleep_ms(self.POLL_MS)

    async def measure_async(self):
        """Like measure(), but other tasks run during the 80 ms conversion."""
        self.trigger()
        await asyncio.sleep(self.MEASURE_MS / 1000)
        start = time.monotonic()
        while True:
            r = self.result()
            if r is not None:
                return r
            if _timed_out(start, self.TIMEOUT_MS):
                raise OSError("AHT21: measurement timed out")
            await asyncio.sleep(self.POLL_MS / 1000)

    @staticmethod
    def crc8(data):
        """CRC-8, polynomial x^8 + x^5 + x^4 + 1 (0x31), initial value 0xFF (AHT §7.4 step 4)."""
        crc = 0xFF
        for b in data:
            crc ^= b
            for _ in range(8):
                crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
        return crc


# =====================================================================================================
# ENS160 air quality sensor
# =====================================================================================================

class ENS160:
    ADDRESS     = 0x53                                      # ADDR pin high; 0x52 with ADDR low (ENS §14.1.1)
    ADDRESS_ALT = 0x52
    PART_ID     = 0x0160                                    # ENS §16.2.1

    # Registers (ENS §16.1, Table 16). Multi-byte registers are little endian.
    REG_PART_ID       = 0x00
    REG_OPMODE        = 0x10
    REG_CONFIG        = 0x11
    REG_COMMAND       = 0x12
    REG_TEMP_IN       = 0x13                                # followed by RH_IN at 0x15
    REG_DEVICE_STATUS = 0x20
    REG_DATA_AQI      = 0x21                                # followed by DATA_TVOC 0x22, DATA_ECO2 0x24
    REG_DATA_T        = 0x30                                # followed by DATA_RH 0x32
    REG_DATA_MISR     = 0x38
    REG_GPR_READ      = 0x48

    # Operating modes (ENS §16.2.2)
    MODE_DEEP_SLEEP = 0x00
    MODE_IDLE       = 0x01
    MODE_STANDARD   = 0x02
    MODE_RESET      = 0xF0

    # Commands, only actioned in IDLE mode (ENS §16.2.4)
    CMD_NOP        = 0x00
    CMD_GET_APPVER = 0x0E
    CMD_CLRGPR     = 0xCC

    MISR_POLY = 0x1D                                        # x^8 + x^4 + x^3 + x^2 + 1 (ENS §16.2.14)

    VALIDITY = ("Normal operation", "Warm-up", "Initial start-up", "Invalid output")    # ENS §11, Table 10
    AQI_RATING = (None, "Excellent", "Good", "Moderate", "Poor", "Unhealthy")          # ENS §5.3, Table 6

    MODE_CHANGE_MS = 10                                     # not specified in the datasheet
    TIMEOUT_MS = 1000

    def __init__(self, i2c, address=ADDRESS):
        """i2c: an I2C object from this module. Max bus speed 1 MHz (ENS §14.1.1)."""
        self.i2c = i2c
        self.address = address

    # ---- Low-level helpers ----

    def _read(self, reg, length):
        return self.i2c.readfrom_mem(self.address, reg, length)

    def _write(self, reg, data):
        self.i2c.writeto_mem(self.address, reg, bytes(data))

    @staticmethod
    def _u16(d, i=0):
        return d[i] | (d[i + 1] << 8)

    # ---- Identity and operating mode (ENS §15, §16.2.1-2) ----

    def begin(self):
        """Check the part ID, then start STANDARD gas sensing. Raises OSError on a wrong ID."""
        part_id = self.part_id()
        if part_id != self.PART_ID:
            raise OSError(f"ENS160: unexpected PART_ID 0x{part_id:04X} (expected 0x{self.PART_ID:04X})")
        self.set_mode(self.MODE_STANDARD)

    def part_id(self):
        return self._u16(self._read(self.REG_PART_ID, 2))

    def set_mode(self, mode):
        """Leaving STANDARD and coming back restarts the 3 minute warm-up (ENS §10.2)."""
        self._write(self.REG_OPMODE, (mode,))
        _sleep_ms(self.MODE_CHANGE_MS)

    def mode(self):
        return self._read(self.REG_OPMODE, 1)[0]

    def reset(self):
        """Software reset. The sensor comes back in IDLE mode (ENS §15)."""
        self.set_mode(self.MODE_RESET)
        self.set_mode(self.MODE_IDLE)

    def firmware_version(self):
        """Returns 'major.minor.release' (ENS §16.2.4.1). Commands only run in IDLE, so this leaves the
        current mode briefly and restores it. Coming back to STANDARD restarts the warm-up."""
        mode = self.mode()
        if mode != self.MODE_IDLE:
            self.set_mode(self.MODE_IDLE)
        try:
            self._read(self.REG_GPR_READ, 1)                # clears any stale NEWGPR flag
            self._write(self.REG_COMMAND, (self.CMD_NOP,))
            self._write(self.REG_COMMAND, (self.CMD_GET_APPVER,))
            start = time.monotonic()
            while not self.status().new_gpr:
                if _timed_out(start, self.TIMEOUT_MS):
                    raise OSError("ENS160: no reply to GET_APPVER")
                _sleep_ms(5)
            d = self._read(self.REG_GPR_READ, 8)
        finally:
            if mode != self.MODE_IDLE:
                self.set_mode(mode)
        return f"{d[4]}.{d[5]}.{d[6]}"

    def configure_interrupt(self, enable=True, on_data=True, on_gpr=False, push_pull=True, active_high=False):
        """Configure the INTn pin (ENS §16.2.3, Table 19). Defaults give 0x23: active low, push-pull,
        asserted when new data is in DATA_x."""
        cfg = ((0x40 if active_high else 0) | (0x20 if push_pull else 0) |
               (0x08 if on_gpr else 0) | (0x02 if on_data else 0) | (0x01 if enable else 0))
        self._write(self.REG_CONFIG, (cfg,))

    # ---- Status and measurements (ENS §16.2.7-10) ----

    def status(self):
        s = self._read(self.REG_DEVICE_STATUS, 1)[0]
        return Status(bool(s & 0x80), bool(s & 0x40), (s >> 2) & 0x03, bool(s & 0x02), bool(s & 0x01))

    def read(self, verify=False):
        """AQI, TVOC and eCO2 in one 5-byte read, so all three come from the same sample.
        This read clears NEWDAT. verify=True checks the transfer against DATA_MISR (see _misr)."""
        if verify:
            seed = self._read(self.REG_DATA_MISR, 1)[0]
        d = self._read(self.REG_DATA_AQI, 5)
        if verify and self._misr(seed, d) != self._read(self.REG_DATA_MISR, 1)[0]:
            raise OSError("ENS160: DATA_MISR mismatch")
        return AirQuality(d[0] & 0x07, self._u16(d, 1), self._u16(d, 3))

    def wait_data(self, poll_ms=50, timeout_ms=5000):
        """Wait for the next sample (NEWDAT), then read it."""
        start = time.monotonic()
        while not self.status().new_data:
            if _timed_out(start, timeout_ms):
                raise OSError(f"ENS160: no new data within {timeout_ms} ms")
            _sleep_ms(poll_ms)
        return self.read()

    async def wait_data_async(self, poll_ms=50, timeout_ms=5000):
        """Like wait_data(), but other tasks run while it waits."""
        start = time.monotonic()
        while not self.status().new_data:
            if _timed_out(start, timeout_ms):
                raise OSError(f"ENS160: no new data within {timeout_ms} ms")
            await asyncio.sleep(poll_ms / 1000)
        return self.read()

    def raw_resistance(self):
        """Raw resistance in ohms of sensor elements 1 and 4 (ENS §7, Table 8), for custom processing.
        Raw values are logarithmic: R = 2^(raw / 2048). These aren't humidity-compensated (ENS §8.2)."""
        d = self._read(self.REG_GPR_READ, 8)
        return 2 ** (self._u16(d, 0) / 2048), 2 ** (self._u16(d, 6) / 2048)

    # ---- Temperature / humidity compensation (ENS §8.2, §16.2.5-6, §16.2.12-13) ----

    def set_compensation(self, temp_c, humidity):
        """Give the ENS160 the temperature (°C) and relative humidity (%) around it.
        Encoding: Kelvin * 64 and %RH * 512. Written in one auto-increment transfer, 0x13 to 0x16,
        so each LSB goes before its MSB as the datasheet requires."""
        t = round((temp_c + 273.15) * 64)
        rh = round(min(max(humidity, 0), 100) * 512)
        self._write(self.REG_TEMP_IN, (t & 0xFF, t >> 8, rh & 0xFF, rh >> 8))

    def compensation(self):
        """The (°C, %RH) the ENS160 is currently using, from DATA_T and DATA_RH.
        Defaults are 25 °C and 50 %RH."""
        d = self._read(self.REG_DATA_T, 4)
        return self._u16(d, 0) / 64 - 273.15, self._u16(d, 2) / 512

    # ---- Ratings (no I/O) ----

    @staticmethod
    def eco2_rating(eco2):
        """ENS §5.2, Table 5."""
        if eco2 > 1500:
            return "Bad"
        if eco2 > 1000:
            return "Poor"
        if eco2 > 800:
            return "Fair"
        if eco2 > 600:
            return "Good"
        return "Excellent"

    # ---- Data integrity (ENS §16.2.14) ----

    @classmethod
    def _misr(cls, misr, data):
        """Update the DATA_MISR checksum for each byte read from 0x20-0x37.
        The datasheet's example C code has two bugs, fixed here:
          - `misr&0x80==0` parses as `misr & (0x80==0)`, which is always 0
          - misr_set() is defined twice with different signatures
        It also contradicts itself on whether DATA_MISR covers only the previous transaction or keeps
        running. Reading DATA_MISR (0x38, outside the range) just before the data read as the
        starting value works for the running case. Untested on hardware."""
        for b in data:
            x = ((misr << 1) ^ b) & 0xFF
            misr = x ^ cls.MISR_POLY if misr & 0x80 else x
        return misr


# =====================================================================================================
# Both sensors on one board
# =====================================================================================================

class ENS160_AHT21:
    """The ENS160 does the air-quality sensing. The AHT21 is only there to measure the temperature and
    humidity at the ENS160 and feed them to it as compensation (ENS §8.2)."""

    AHT21_MIN_INTERVAL_MS = 1000                            # limit self-heating (AHT §4.4)

    def __init__(self, i2c, ens160_address=None):
        """i2c: an I2C object from this module (bus speed <= 400 kHz, the AHT21's limit).
        ens160_address: None finds 0x53 or 0x52 in begin()."""
        self.i2c = i2c
        self.aht21 = AHT21(i2c)
        self.ens160 = ENS160(i2c, ens160_address or ENS160.ADDRESS)
        self._find_ens160 = ens160_address is None
        self._compensated_at = None                         # time.monotonic() of the last AHT21 measurement
        self._pcb = None                                    # (°C, %RH) of the last AHT21 measurement

    def begin(self):
        """Check both sensors are on the bus, initialise the AHT21 and start ENS160 gas sensing."""
        if not self.i2c.probe(self.aht21.address):
            raise OSError(f"AHT21 not found at 0x{self.aht21.address:02X}")
        candidates = (ENS160.ADDRESS, ENS160.ADDRESS_ALT) if self._find_ens160 else (self.ens160.address,)
        for addr in candidates:
            if self.i2c.probe(addr):
                self.ens160.address = addr
                break
        else:
            raise OSError("ENS160 not found at " + " / ".join(f"0x{a:02X}" for a in candidates))
        self.aht21.begin()
        self.ens160.begin()

    def _compensation_due(self):
        return self._compensated_at is None or _timed_out(self._compensated_at, self.AHT21_MIN_INTERVAL_MS - 1)

    def _compensate(self, t, rh):
        self._compensated_at = time.monotonic()
        self._pcb = (t, rh)
        self.ens160.set_compensation(t, rh)

    def _air_quality(self):
        status = self.ens160.status()
        aq = self.ens160.read()
        return Reading(aq.aqi, aq.tvoc, aq.eco2, status.validity, self._pcb[0], self._pcb[1])

    def read(self):
        """Update the ENS160's compensation from the AHT21 (at most once a second), then read the
        latest air quality. pcb_temp and pcb_humi are the AHT21 values the ENS160 is using, so between
        AHT21 measurements they repeat."""
        if self._compensation_due():
            self._compensate(*self.aht21.measure())
        return self._air_quality()

    async def read_async(self):
        """Like read(), but other tasks run during the AHT21 conversion."""
        if self._compensation_due():
            self._compensate(*(await self.aht21.measure_async()))
        return self._air_quality()
