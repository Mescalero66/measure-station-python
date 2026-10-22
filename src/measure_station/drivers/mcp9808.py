# Asynchronous Python driver for:
# DFRobot SEN0435 Fermion: MCP9808 High Accuracy I2C Temperature Sensor (Microchip MCP9808)
# <https://wiki.dfrobot.com/sen0435/>
#
# Based on the Microchip MCP9808 datasheet DS20005095B (May 2018). Section numbers in comments refer to it.

import asyncio
from dataclasses import dataclass

from smbus2 import SMBus, i2c_msg


@dataclass
class Measurement:
    temperature: float      # °C
    above_critical: bool    # TA ≥ TCRIT  (flag bits are set whatever the alert configuration, §5.1.3)
    above_upper: bool       # TA > TUPPER
    below_lower: bool       # TA < TLOWER


class MCP9808:
    ADDRESS = 0x1F                                  # DFRobot board: A2 A1 A0 pulled high. Range 0x18–0x1F (§4.1.4)

    # Register pointers (§5.1, Register 5-1)
    CONFIG                  = 0x01
    T_UPPER                 = 0x02
    T_LOWER                 = 0x03
    T_CRIT                  = 0x04
    T_A                     = 0x05
    MANUFACTURER_ID         = 0x06
    DEVICE_ID               = 0x07
    RESOLUTION              = 0x08

    # CONFIG bits (§5.1.1, Register 5-2)
    HYST_SHIFT              = 9                     # bits 10–9
    SHDN                    = 0x0100
    CRIT_LOCK               = 0x0080
    WIN_LOCK                = 0x0040
    INT_CLEAR               = 0x0020
    ALERT_STAT              = 0x0010                # read-only
    ALERT_CNT               = 0x0008                # alert output enabled
    ALERT_SEL               = 0x0004                # TCRIT only
    ALERT_POL               = 0x0002                # active-high
    ALERT_MOD               = 0x0001                # interrupt mode
    _CHECK = 0x07CF                                 # bits that read back as written (not INT_CLEAR / ALERT_STAT)

    MANUFACTURER = 0x0054
    DEVICE = 0x04

    HYSTERESIS = (0.0, 1.5, 3.0, 6.0)               # °C, THYST codes 00–11
    RESOLUTIONS = (0.5, 0.25, 0.125, 0.0625)        # °C, resolution codes 00–11 (Register 5-7)
    CONVERSION_MS = (30, 65, 130, 250)              # typical tCONV per resolution (Table 5-2)

    def __init__(self, bus=1, address=ADDRESS):
        self.address = address
        self._bus = SMBus(bus)
        self._lock = asyncio.Lock()                 # one register sequence at a time
        self._res = 3                               # power-up default, re-read in __aenter__

    # ---- Context manager: check the ID, start converting; shut down on exit ----

    async def __aenter__(self):
        if await self.get_manufacturer_id() != self.MANUFACTURER or (await self.get_device_id())[0] != self.DEVICE:
            raise RuntimeError(f"no MCP9808 at 0x{self.address:02X}")
        await self.get_resolution()
        await self.wake_up()
        return self

    async def __aexit__(self, *exc):
        try:
            await self.shutdown()                   # 0.1 µA
        except RuntimeError:
            pass                                    # locked: SHDN can't be set until a power cycle
        finally:
            self.close()

    def close(self):
        self._bus.close()

    # ---- Low-level I2C helpers (§4.1, Figures 5-2 to 5-8) ----

    def _read(self, reg, n=2):
        """Write the register pointer, repeated START, read n bytes MSB first."""
        rd = i2c_msg.read(self.address, n)
        self._bus.i2c_rdwr(i2c_msg.write(self.address, [reg]), rd)
        b = list(rd)
        return b[0] << 8 | b[1] if n == 2 else b[0]

    def _write(self, reg, value, n=2):
        data = [reg, value >> 8 & 0xFF, value & 0xFF] if n == 2 else [reg, value & 0xFF]
        self._bus.i2c_rdwr(i2c_msg.write(self.address, data))

    async def _get(self, reg, n=2):
        async with self._lock:
            return self._read(reg, n)

    async def _set_checked(self, reg, value, mask=0xFFFF):
        """Write a register and read it back. Locked registers ignore writes, so a mismatch raises."""
        async with self._lock:
            self._write(reg, value)
            got = self._read(reg)
        if (got ^ value) & mask:
            raise RuntimeError(f"MCP9808 ignored write to register 0x{reg:02X} (locked until power cycle?)")

    async def _update_config(self, clear=0, set_=0):
        """Read-modify-write CONFIG, then check the change took effect."""
        async with self._lock:
            cfg = self._read(self.CONFIG) & ~(self.INT_CLEAR | self.ALERT_STAT)
            new = (cfg & ~clear) | set_
            self._write(self.CONFIG, new)
            got = self._read(self.CONFIG)
        if (got ^ new) & self._CHECK:
            raise RuntimeError("MCP9808 ignored CONFIG write (locked until power cycle?)")

    @staticmethod
    def _to_celsius(raw):
        """13-bit two's complement, 1/16 °C per LSB (§5.1.3.1). Also used for the limit registers."""
        raw &= 0x1FFF
        return (raw - 0x2000 if raw & 0x1000 else raw) / 16

    @staticmethod
    def _from_celsius(celsius):
        """Limit registers: 11-bit two's complement in bits 12–2, 0.25 °C per LSB (§5.1.2)."""
        if not -256 <= celsius <= 255.75:
            raise ValueError(f"MCP9808 limit {celsius} °C out of range -256 – 255.75")
        return (round(celsius * 4) << 2) & 0x1FFC

    def conversion_time(self):
        """Seconds per conversion at the current resolution, with a 25 % margin (the datasheet gives typical values only)."""
        return self.CONVERSION_MS[self._res] * 1.25 / 1000

    # ---- Temperature (§5.1.3) ----

    async def read_measurement(self):
        """Latest temperature plus the TA vs TCRIT / TUPPER / TLOWER flags. The register updates every tCONV."""
        raw = await self._get(self.T_A)
        return Measurement(self._to_celsius(raw), bool(raw & 0x8000), bool(raw & 0x4000), bool(raw & 0x2000))

    async def read_temperature(self):
        return (await self.read_measurement()).temperature

    async def measurements(self):
        """Async generator that yields a new measurement every conversion time."""
        while True:
            await asyncio.sleep(self.conversion_time())
            yield await self.read_measurement()

    async def measure_single_shot(self):
        """Wake, wait one conversion, read, shut down again: about 0.1 µA between readings (§5.2.1)."""
        await self.wake_up()
        m = await self.read_measurement()
        await self.shutdown()
        return m

    # ---- Power (§5.2.1) ----

    async def shutdown(self):
        """Stop converting. Registers stay readable and writable; TA keeps the last value. Not allowed while locked."""
        await self._update_config(set_=self.SHDN)

    async def wake_up(self):
        """Return to continuous conversion, then wait until TA holds a fresh value."""
        await self._update_config(clear=self.SHDN)
        await asyncio.sleep(self.conversion_time())

    # ---- Resolution (§5.1.6, §5.2.4) ----

    async def get_resolution(self):
        """°C per LSB: 0.5, 0.25, 0.125 or 0.0625."""
        self._res = await self._get(self.RESOLUTION, n=1) & 0x03
        return self.RESOLUTIONS[self._res]

    async def set_resolution(self, step):
        """0.5 (30 ms), 0.25 (65 ms), 0.125 (130 ms) or 0.0625 °C (250 ms, power-up default)."""
        if step not in self.RESOLUTIONS:
            raise ValueError(f"MCP9808 resolution must be one of {self.RESOLUTIONS}")
        code = self.RESOLUTIONS.index(step)
        async with self._lock:
            self._write(self.RESOLUTION, code, n=1)
        self._res = code

    # ---- Alert limits (§5.1.2) ----

    async def get_upper_limit(self):
        return self._to_celsius(await self._get(self.T_UPPER))

    async def set_upper_limit(self, celsius):
        await self._set_checked(self.T_UPPER, self._from_celsius(celsius))

    async def get_lower_limit(self):
        return self._to_celsius(await self._get(self.T_LOWER))

    async def set_lower_limit(self, celsius):
        await self._set_checked(self.T_LOWER, self._from_celsius(celsius))

    async def get_critical_limit(self):
        return self._to_celsius(await self._get(self.T_CRIT))

    async def set_critical_limit(self, celsius):
        await self._set_checked(self.T_CRIT, self._from_celsius(celsius))

    # ---- Configuration and alert output (§5.1.1, §5.2.2, §5.2.3) ----

    async def get_config(self):
        """Raw 16-bit CONFIG register."""
        return await self._get(self.CONFIG)

    async def get_hysteresis(self):
        return self.HYSTERESIS[(await self.get_config()) >> self.HYST_SHIFT & 0x03]

    async def set_hysteresis(self, celsius):
        """0, 1.5, 3 or 6 °C, applied to all three limits as the temperature falls."""
        if celsius not in self.HYSTERESIS:
            raise ValueError(f"MCP9808 hysteresis must be one of {self.HYSTERESIS}")
        await self._update_config(clear=0x03 << self.HYST_SHIFT, set_=self.HYSTERESIS.index(celsius) << self.HYST_SHIFT)

    async def configure_alert(self, enabled=True, interrupt=False, active_high=False, critical_only=False):
        """Set up the open-drain Alert (ALT) pin.
        interrupt=False: comparator mode, asserted while a limit is exceeded (thermostat use).
        interrupt=True: asserted at each limit crossing until clear_interrupt().
        critical_only=True: alert only when TA ≥ TCRIT (always comparator mode)."""
        if interrupt and critical_only:
            raise ValueError("MCP9808 interrupt mode can't be used with critical_only")
        bits = ((self.ALERT_CNT if enabled else 0) | (self.ALERT_SEL if critical_only else 0)
                | (self.ALERT_POL if active_high else 0) | (self.ALERT_MOD if interrupt else 0))
        await self._update_config(clear=0x000F, set_=bits)

    async def get_alert_status(self):
        """True while the Alert output is asserted."""
        return bool(await self.get_config() & self.ALERT_STAT)

    async def clear_interrupt(self):
        """Release the Alert output in interrupt mode (not possible in shutdown)."""
        await self._update_config(set_=self.INT_CLEAR)

    async def wait_alert(self, poll=0.1):
        """Poll the Alert Stat bit until the alert is asserted, then return the measurement that caused it.
        In interrupt mode the interrupt is cleared afterwards, so the next call waits for the next crossing."""
        while True:
            cfg = await self.get_config()
            if cfg & self.ALERT_STAT:
                break
            await asyncio.sleep(poll)
        m = await self.read_measurement()
        if cfg & self.ALERT_MOD and not cfg & self.ALERT_SEL:
            await self.clear_interrupt()
        return m

    async def lock_limits(self, critical=True, window=True):
        """Lock TCRIT and/or TUPPER/TLOWER and the alert settings against writes. Only a power cycle unlocks them."""
        await self._update_config(set_=(self.CRIT_LOCK if critical else 0) | (self.WIN_LOCK if window else 0))

    async def reset(self):
        """Write the power-on defaults (§5.3, Table 5-3). The chip has no reset command, so this fails if locked."""
        await self._set_checked(self.CONFIG, 0x0000, self._CHECK)
        for reg in (self.T_UPPER, self.T_LOWER, self.T_CRIT):
            await self._set_checked(reg, 0x0000)
        await self.set_resolution(0.0625)

    # ---- Identification (§5.1.4, §5.1.5) ----

    async def get_manufacturer_id(self):
        """0x0054 for Microchip."""
        return await self._get(self.MANUFACTURER_ID)

    async def get_device_id(self):
        """(device ID, revision). The MCP9808 device ID is 0x04."""
        word = await self._get(self.DEVICE_ID)
        return word >> 8, word & 0xFF
