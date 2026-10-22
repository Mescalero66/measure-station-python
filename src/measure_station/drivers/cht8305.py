# Asynchronous Python driver for:
# DFRobot SEN0546 I2C Temperature & Humidity Sensor, Stainless Steel Shell (Sensylink CHT8305C)
# <https://wiki.dfrobot.com/SKU_SEN0546_I2C_Temperature_and_Humidity_Sensor_Stainless_Steel_Shell>
#
# Based on the Sensylink CHT8305C datasheet Rev 2.2 (Jul 2020). Section numbers in comments refer to it.
# Probes made after 10/7/2025 use a CHT832X instead (green SCL wire); this driver is for the CHT8305 (white SCL wire).

import asyncio
from dataclasses import dataclass

from smbus2 import SMBus, i2c_msg


@dataclass
class Measurement:
    temperature: float      # °C
    humidity: float         # %RH


@dataclass
class AlertStatus:
    pending: bool           # APS: at least one alert pending
    humidity: bool          # HALT: humidity ≥ its high limit at the last measurement
    temperature: bool       # TALT: temperature ≥ its high limit at the last measurement


class CHT8305:
    ADDRESS = 0x40                                  # AD0 to GND or open (the SEN0546 probe has no AD0 wire)
    ADDRESSES = (0x40, 0x44, 0x48, 0x4C)            # AD0 to GND / VCC / SDA / SCL (§1.5.1, Table 7)

    # Registers (§1.1, Table 1)
    TEMPERATURE             = 0x00                  # writing this pointer starts a T (+ RH) conversion
    HUMIDITY                = 0x01                  # writing this pointer starts an RH conversion
    CONFIG                  = 0x02
    ALERT_SETUP             = 0x03
    MANUFACTURER_ID         = 0xFE
    VERSION_ID              = 0xFF

    # CONFIG bits (§1.1.3, Table 4)
    SRST                    = 0x8000                # soft reset, reads back 0
    CLKSTR                  = 0x4000                # clock stretching during conversion
    HEATER                  = 0x2000
    MODE                    = 0x1000                # 1 = T then RH in one conversion (default)
    VCCS                    = 0x0800                # read-only: supply below 2.8 V
    T_RES                   = 0x0400                # 1 = 11-bit, 0 = 14-bit
    H_RES_SHIFT             = 8                     # bits 9–8: 00 = 14-bit, 01 = 11-bit, 10 = 8-bit
    ALTM_SHIFT              = 6                     # bits 7–6: alert trigger mode
    APS                     = 0x0020                # read-only status bits
    HALT                    = 0x0010
    TALT                    = 0x0008
    _WRITABLE = 0xF7C3                              # leaves out VCCS, the status bits and bit 2 (must be written 0)

    # Alert trigger modes, ALTM (§1.1.3)
    ALERT_EITHER            = 0                     # T or RH over its limit (default)
    ALERT_TEMPERATURE       = 1                     # T only
    ALERT_HUMIDITY          = 2                     # RH only
    ALERT_BOTH              = 3                     # T and RH both over their limits

    MANUFACTURER = 0x5959
    VERSION = 0x8305
    H_BITS = {14: 0, 11: 1, 8: 2}

    CONVERSION_TIME = 0.012                         # T + RH at 14-bit: 2 × 5.5 ms typical (§1.4.3)
    POLL_INTERVAL = 0.002                           # the sensor NACKs reads until the conversion is done (§1.5.2)
    POLL_TRIES = 50

    def __init__(self, bus=1, address=ADDRESS):
        self.address = address
        self._bus = SMBus(bus)
        self._lock = asyncio.Lock()                 # one register sequence at a time (pointer + read)

    # ---- Context manager: check the ID, T + RH mode without clock stretching; heater off on exit ----

    async def __aenter__(self):
        if await self.get_manufacturer_id() != self.MANUFACTURER:
            raise RuntimeError(f"no CHT8305 at 0x{self.address:02X} (a CHT832X probe needs a different driver)")
        await self._update_config(clear=self.CLKSTR, set_=self.MODE)
        return self

    async def __aexit__(self, *exc):
        try:
            await self.set_heater(False)
        finally:
            self.close()

    def close(self):
        self._bus.close()

    # ---- Low-level I2C helpers (§1.5.2) ----

    def _write(self, data):
        self._bus.i2c_rdwr(i2c_msg.write(self.address, data))

    def _read(self, n):
        msg = i2c_msg.read(self.address, n)
        self._bus.i2c_rdwr(msg)
        return list(msg)

    async def _get(self, reg):
        """Write the register pointer, STOP, then read 16 bits MSB first."""
        async with self._lock:
            self._write([reg])
            b = self._read(2)
        return b[0] << 8 | b[1]

    async def _set(self, reg, value):
        async with self._lock:
            self._write([reg, value >> 8 & 0xFF, value & 0xFF])

    async def _update_config(self, clear=0, set_=0):
        async with self._lock:
            self._write([self.CONFIG])
            b = self._read(2)
            cfg = ((b[0] << 8 | b[1]) & ~clear | set_) & self._WRITABLE
            self._write([self.CONFIG, cfg >> 8, cfg & 0xFF])

    async def _measure(self, reg, n):
        """Start a conversion by writing the pointer, then read once the sensor ACKs again."""
        async with self._lock:
            self._write([reg])
            await asyncio.sleep(self.CONVERSION_TIME)
            for _ in range(self.POLL_TRIES):
                try:
                    return self._read(n)
                except OSError:                     # NACK: still converting
                    await asyncio.sleep(self.POLL_INTERVAL)
        raise OSError("CHT8305 conversion did not finish")

    @staticmethod
    def _to_celsius(raw):
        return raw * 165 / 65535 - 40               # formula (1)

    @staticmethod
    def _to_rh(raw):
        return raw * 100 / 65535                    # formula (2)

    # ---- Measurement (§1.4) ----

    async def read_measurement(self):
        """One T + RH conversion (about 11 ms), read as 4 bytes (§1.4.5, method 2)."""
        b = await self._measure(self.TEMPERATURE, 4)
        return Measurement(self._to_celsius(b[0] << 8 | b[1]), self._to_rh(b[2] << 8 | b[3]))

    async def read_temperature(self):
        b = await self._measure(self.TEMPERATURE, 2)
        return self._to_celsius(b[0] << 8 | b[1])

    async def read_humidity(self):
        b = await self._measure(self.HUMIDITY, 2)
        return self._to_rh(b[0] << 8 | b[1])

    async def measurements(self, interval=2.0):
        """Async generator that yields a measurement every `interval` seconds. Keep it at 1 s or more to avoid
        self-heating (§2); the sensor sits in standby between conversions."""
        while True:
            yield await self.read_measurement()
            await asyncio.sleep(interval)

    # ---- Configuration (§1.1.3, §1.2, §1.3) ----

    async def get_config(self):
        """Raw 16-bit CONFIG register."""
        return await self._get(self.CONFIG)

    async def get_resolution(self):
        """(temperature bits, humidity bits)."""
        cfg = await self.get_config()
        h = {v: k for k, v in self.H_BITS.items()}.get(cfg >> self.H_RES_SHIFT & 0x03, 14)
        return (11 if cfg & self.T_RES else 14), h

    async def set_resolution(self, temperature_bits=14, humidity_bits=14):
        """Temperature 14 or 11 bits; humidity 14, 11 or 8 bits."""
        if temperature_bits not in (14, 11) or humidity_bits not in self.H_BITS:
            raise ValueError("CHT8305 resolution: temperature 14 or 11 bits, humidity 14, 11 or 8 bits")
        bits = (self.T_RES if temperature_bits == 11 else 0) | self.H_BITS[humidity_bits] << self.H_RES_SHIFT
        await self._update_config(clear=self.T_RES | 0x03 << self.H_RES_SHIFT, set_=bits)

    async def get_heater(self):
        return bool(await self.get_config() & self.HEATER)

    async def set_heater(self, on):
        """On-chip heater (about 7 mA) to drive off condensation. Readings are warmer and drier while it runs."""
        await self._update_config(clear=self.HEATER, set_=self.HEATER if on else 0)

    async def get_supply_low(self):
        """True if VCC is below 2.8 V (VCCS)."""
        return bool(await self.get_config() & self.VCCS)

    async def soft_reset(self):
        """Return every register to its default and reload the calibration (§1.2)."""
        await self._set(self.CONFIG, self.SRST | self.MODE)
        await asyncio.sleep(0.05)

    # ---- Alert high limits (§1.1.4, §1.6) ----

    async def get_alert_limits(self):
        """(temperature °C, humidity %RH). Default 59.9 °C / 79.7 %RH."""
        word = await self._get(self.ALERT_SETUP)
        return self._to_celsius((word & 0x01FF) << 7), self._to_rh((word >> 9) << 9)

    async def set_alert_limits(self, temperature, humidity):
        """High limits, kept as the top 9 bits of T (≈0.32 °C steps) and top 7 bits of RH (≈0.78 %RH steps),
        rounded down as in the datasheet example. Compared after every measurement."""
        if not -40 <= temperature <= 125 or not 0 <= humidity <= 100:
            raise ValueError("CHT8305 alert limits: -40 – 125 °C, 0 – 100 %RH")
        t9 = int((temperature + 40) * 65535 / 165) >> 7
        h7 = int(humidity * 65535 / 100) >> 9
        await self._set(self.ALERT_SETUP, h7 << 9 | t9)

    async def get_alert_mode(self):
        return (await self.get_config()) >> self.ALTM_SHIFT & 0x03

    async def set_alert_mode(self, mode):
        """ALERT_EITHER (default), ALERT_TEMPERATURE, ALERT_HUMIDITY or ALERT_BOTH."""
        if mode not in (0, 1, 2, 3):
            raise ValueError("CHT8305 alert mode must be 0–3")
        await self._update_config(clear=0x03 << self.ALTM_SHIFT, set_=mode << self.ALTM_SHIFT)

    async def get_alert_status(self):
        """Alert flags from the last measurement. The ALERT pin (active low) follows them, but the probe doesn't wire it out."""
        cfg = await self.get_config()
        return AlertStatus(bool(cfg & self.APS), bool(cfg & self.HALT), bool(cfg & self.TALT))

    # ---- Identification (§1.1.5, §1.1.6) ----

    async def get_manufacturer_id(self):
        """0x5959."""
        return await self._get(self.MANUFACTURER_ID)

    async def get_version_id(self):
        """0x8305."""
        return await self._get(self.VERSION_ID)
