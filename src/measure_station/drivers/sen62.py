# Asynchronous Python driver for:
# Sensirion SEN62 particulate matter, temperature & humidity sensor module (SEN62-SIN-T)
# <https://sensirion.com/products/catalog/SEN62>
#
# Based on the Sensirion SEN6x datasheet v1.0 (October 2026). Section numbers in comments refer to it.

import asyncio
from dataclasses import dataclass

from smbus2 import SMBus, i2c_msg


@dataclass
class Measurement:
    pm1_0: float | None         # mass concentration, µg/m³
    pm2_5: float | None
    pm4_0: float | None
    pm10_0: float | None
    humidity: float | None      # %RH (compensated)
    temperature: float | None   # °C (compensated)


@dataclass
class NumberConcentration:
    nc0_5: float | None         # number concentration, particles/cm³
    nc1_0: float | None
    nc2_5: float | None
    nc4_0: float | None
    nc10_0: float | None


@dataclass
class DeviceStatus:
    raw: int                    # whole 32-bit status register (§4.3)
    fan_speed_warning: bool     # bit 21, clears itself
    pm_error: bool              # bit 11, sticky
    rht_error: bool             # bit 6, sticky
    fan_error: bool             # bit 4, sticky


def _uint(word, scale):
    """Scale an unsigned value. 0xFFFF means unknown."""
    return None if word == 0xFFFF else word / scale


def _int(word, scale):
    """Scale a signed (two's complement) value. 0x7FFF means unknown."""
    if word == 0x7FFF:
        return None
    return (word - 0x10000 if word & 0x8000 else word) / scale


class SEN62:
    ADDRESS = 0x6B                                  # fixed I2C address (§4.4)

    # Command codes (§4.8, Table 27), only those that apply to the SEN62
    START_MEASUREMENT       = 0x0021
    STOP_MEASUREMENT        = 0x0104
    DATA_READY              = 0x0202
    READ_VALUES             = 0x04A3
    READ_RAW_VALUES         = 0x0492
    READ_NUMBER_CONC        = 0x0316
    TEMP_OFFSET_PARAMS      = 0x60B2
    TEMP_ACCEL_PARAMS       = 0x6100
    PRODUCT_TYPE            = 0xD002
    PRODUCT_NAME            = 0xD014
    SERIAL_NUMBER           = 0xD033
    DEVICE_STATUS           = 0xD206
    DEVICE_STATUS_CLEAR     = 0xD210
    VERSION                 = 0xD100
    DEVICE_RESET            = 0xD304
    FAN_CLEANING            = 0x5607
    SHT_HEATER              = 0x6765
    SHT_HEATER_RESULT       = 0x6790

    # Device status register bits (§4.3)
    STATUS_FAN_SPEED        = 1 << 21
    STATUS_PM               = 1 << 11
    STATUS_RHT              = 1 << 6
    STATUS_FAN              = 1 << 4

    def __init__(self, bus=1, address=ADDRESS):
        """bus: I2C bus number. The SEN62 supports standard mode only (≤ 100 kHz)."""
        self.address = address
        self._bus = SMBus(bus)
        self._lock = asyncio.Lock()                 # one command at a time (§4.7)

    # ---- Context manager: stop any running measurement, reset to defaults ----

    async def __aenter__(self):
        await self.stop_measurement()               # in case a previous run left it measuring
        await self.device_reset()
        return self

    async def __aexit__(self, *exc):
        try:
            await self.stop_measurement()           # leave the sensor idle (fan and laser off)
        finally:
            self.close()

    def close(self):
        self._bus.close()

    # ---- Low-level I2C helpers ----

    @staticmethod
    def _crc8(data):
        """Sensirion CRC-8: poly 0x31, init 0xFF (§4.9)."""
        crc = 0xFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                crc = ((crc << 1) ^ 0x31) if crc & 0x80 else (crc << 1)
                crc &= 0xFF
        return crc

    def _write(self, cmd, args=()):
        """Send a 16-bit command, followed by each data word + CRC (§4.6)."""
        buf = [cmd >> 8, cmd & 0xFF]
        for arg in args:
            word = [(arg >> 8) & 0xFF, arg & 0xFF]  # masking also encodes negative int16 values
            buf += word + [self._crc8(word)]
        self._bus.i2c_rdwr(i2c_msg.write(self.address, buf))

    async def _send(self, cmd, args=(), delay=0.02):
        """'write' sequence, then wait out the execution time (the sensor NACKs while busy)."""
        async with self._lock:
            self._write(cmd, args)
            await asyncio.sleep(delay)

    async def _read(self, cmd, words=1, delay=0.02):
        """'read' sequence (§4.7). Returns a list of CRC-checked words."""
        async with self._lock:
            self._write(cmd)
            await asyncio.sleep(delay)              # must wait before issuing the read header
            msg = i2c_msg.read(self.address, words * 3)
            self._bus.i2c_rdwr(msg)
        raw = list(msg)
        result = []
        for i in range(0, len(raw), 3):
            word, crc = raw[i:i + 2], raw[i + 2]
            if self._crc8(word) != crc:
                raise ValueError(f"SEN62 CRC mismatch on command 0x{cmd:04X}")
            result.append(word[0] << 8 | word[1])
        return result

    async def _read_string(self, cmd):
        """Null-terminated ASCII string of up to 32 characters (16 words)."""
        words = await self._read(cmd, words=16)
        data = bytes(b for w in words for b in (w >> 8, w & 0xFF))
        return data.split(b"\x00")[0].decode("ascii")

    # ---- Measurement (§4.1, §4.8.1 – §4.8.13) ----

    async def start_continuous_measurement(self):
        """Idle -> measurement mode. Fan and laser start, and a new reading arrives every second."""
        await self._send(self.START_MEASUREMENT, delay=0.05)

    async def stop_measurement(self):
        """Measurement -> idle mode. Takes 1.4 s."""
        await self._send(self.STOP_MEASUREMENT, delay=1.4)

    async def get_data_ready(self):
        (word,) = await self._read(self.DATA_READY)
        return bool(word & 0xFF)                    # high byte is padding

    async def read_measured_values(self):
        """Latest PM mass concentrations and compensated RH/T. Unknown values are None.
        Returns the previous values again if no new data is ready."""
        w = await self._read(self.READ_VALUES, words=6)
        return Measurement(*(_uint(x, 10) for x in w[:4]), _int(w[4], 100), _int(w[5], 200))

    async def measurements(self, poll=0.2):
        """Async generator that yields each new measurement (one per second) as it becomes ready."""
        while True:
            if await self.get_data_ready():
                yield await self.read_measured_values()
            else:
                await asyncio.sleep(poll)

    async def read_measured_raw_values(self):
        """Uncompensated (humidity %RH, temperature °C) straight from the RH/T sensor."""
        rh, t = await self._read(self.READ_RAW_VALUES, words=2)
        return _int(rh, 100), _int(t, 200)

    async def read_number_concentration_values(self):
        """Particle counts for PM0.5 / 1.0 / 2.5 / 4.0 / 10, in particles/cm³."""
        w = await self._read(self.READ_NUMBER_CONC, words=5)
        return NumberConcentration(*(_uint(x, 10) for x in w))

    # ---- Temperature compensation (§4.2, §4.8.14, §4.8.15) ----

    async def set_temperature_offset_parameters(self, offset, slope=0.0, time_constant=0, slot=0):
        """T_out = T + slope * T + offset, summed over 5 slots (0–4). offset in °C, slope as a factor,
        time_constant in s (0 = apply now). Allowed while measuring. Volatile: lost on reset."""
        if not 0 <= slot <= 4:
            raise ValueError("slot must be 0–4")
        await self._send(self.TEMP_OFFSET_PARAMS,
                         (round(offset * 200), round(slope * 10000), int(time_constant), int(slot)))

    async def set_temperature_acceleration_parameters(self, k, p, t1, t2):
        """Override the RH/T engine's acceleration filter (t1, t2 in s). See the app note
        "SEN6x – Temperature Acceleration and Compensation Instructions". Idle mode only. Volatile."""
        await self._send(self.TEMP_ACCEL_PARAMS, (round(k * 10), round(p * 10), round(t1 * 10), round(t2 * 10)))

    # ---- Device information and status (§4.3, §4.8.16 – §4.8.21) ----

    async def get_product_type(self):
        """Returns "0x00085800" on the SEN62."""
        return await self._read_string(self.PRODUCT_TYPE)

    async def get_product_name(self):
        return await self._read_string(self.PRODUCT_NAME)

    async def get_serial_number(self):
        return await self._read_string(self.SERIAL_NUMBER)

    async def get_version(self):
        """Firmware version as (major, minor)."""
        (word,) = await self._read(self.VERSION)
        return word >> 8, word & 0xFF

    async def read_device_status(self):
        return self._status(await self._read(self.DEVICE_STATUS, words=2))

    async def read_and_clear_device_status(self):
        """Returns the status as it was before the flags were cleared."""
        return self._status(await self._read(self.DEVICE_STATUS_CLEAR, words=2))

    def _status(self, words):
        raw = words[0] << 16 | words[1]
        return DeviceStatus(raw, bool(raw & self.STATUS_FAN_SPEED), bool(raw & self.STATUS_PM),
                            bool(raw & self.STATUS_RHT), bool(raw & self.STATUS_FAN))

    # ---- Maintenance (§4.8.22 – §4.8.25) ----

    async def device_reset(self):
        """Same as a power cycle: back to idle, volatile settings and sticky errors cleared. Idle mode only."""
        await self._send(self.DEVICE_RESET, delay=1.2)

    async def start_fan_cleaning(self):
        """Run the fan at full speed for 10 s to blow out dust. Idle mode only. Returns once the 10 s are up."""
        await self._send(self.FAN_CLEANING, delay=10.0)

    async def activate_sht_heater(self):
        """Heat the RH/T sensor (200 mW for 1 s) to undo humidity-induced creep. Idle mode only.
        Waits 1.3 s, the longest execution time across firmware versions. Wait 20 s before measuring."""
        await self._send(self.SHT_HEATER, delay=1.3)

    async def get_sht_heater_measurements(self):
        """(humidity %RH, temperature °C) taken after heating, or (None, None) if not ready.
        Needs firmware ≥ 6.0. Idle mode only."""
        rh, t = await self._read(self.SHT_HEATER_RESULT, words=2)
        return _int(rh, 100), _int(t, 200)
