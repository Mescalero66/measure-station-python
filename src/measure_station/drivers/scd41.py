# Asynchronous Python driver for:
# Adafruit SCD-41 True CO2, Temperature & Humidity Sensor (Sensirion SCD41)
# <https://www.adafruit.com/product/5190>
#
# Based on the Sensirion SCD4x datasheet v1.7 (April 2025). Section numbers in comments refer to it.

import asyncio
from dataclasses import dataclass

from smbus2 import SMBus, i2c_msg


@dataclass
class Measurement:
    co2: int                # ppm (0 for RH/T-only single shots)
    temperature: float      # °C
    humidity: float         # %RH


class SCD41:
    ADDRESS = 0x62                                  # fixed I2C address (§3.2)

    # Command codes (§3.5, Table 9)
    START_PERIODIC          = 0x21B1
    READ_MEASUREMENT        = 0xEC05
    STOP_PERIODIC           = 0x3F86
    SET_TEMP_OFFSET         = 0x241D
    GET_TEMP_OFFSET         = 0x2318
    SET_ALTITUDE            = 0x2427
    GET_ALTITUDE            = 0x2322
    AMBIENT_PRESSURE        = 0xE000                # same code for set and get
    FORCED_RECAL            = 0x362F
    SET_ASC_ENABLED         = 0x2416
    GET_ASC_ENABLED         = 0x2313
    SET_ASC_TARGET          = 0x243A
    GET_ASC_TARGET          = 0x233F
    START_LOW_POWER         = 0x21AC
    DATA_READY              = 0xE4B8
    PERSIST_SETTINGS        = 0x3615
    SERIAL_NUMBER           = 0x3682
    SELF_TEST               = 0x3639
    FACTORY_RESET           = 0x3632
    REINIT                  = 0x3646
    SENSOR_VARIANT          = 0x202F
    SINGLE_SHOT             = 0x219D
    SINGLE_SHOT_RHT         = 0x2196
    POWER_DOWN              = 0x36E0
    WAKE_UP                 = 0x36F6

    VARIANTS = {0b0000: "SCD40", 0b0001: "SCD41", 0b0101: "SCD43"}

    def __init__(self, bus=1, address=ADDRESS):
        self.address = address
        self._bus = SMBus(bus)
        self._lock = asyncio.Lock()                 # one command at a time (§3.4)

    # ---- Context manager: wake, stop any running measurement, reload settings ----

    async def __aenter__(self):
        await self.wake_up()
        await self.stop_periodic_measurement()
        await self.reinit()
        return self

    async def __aexit__(self, *exc):
        try:
            await self.stop_periodic_measurement()  # leave the sensor idle
        finally:
            self.close()

    def close(self):
        self._bus.close()

    # ---- Low-level I2C helpers ----

    @staticmethod
    def _crc8(data):
        """Sensirion CRC-8: poly 0x31, init 0xFF (§3.12)."""
        crc = 0xFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                crc = ((crc << 1) ^ 0x31) if crc & 0x80 else (crc << 1)
                crc &= 0xFF
        return crc

    def _write(self, cmd, arg=None):
        """Send a 16-bit command, optionally followed by one data word + CRC."""
        buf = list(cmd.to_bytes(2, "big"))
        if arg is not None:
            word = list(arg.to_bytes(2, "big"))
            buf += word + [self._crc8(word)]
        self._bus.i2c_rdwr(i2c_msg.write(self.address, buf))

    async def _send(self, cmd, arg=None, delay=0.001):
        """'send command' / 'write' sequence, then wait out the execution time."""
        async with self._lock:
            self._write(cmd, arg)
            await asyncio.sleep(delay)

    async def _read(self, cmd, words=1, delay=0.001, arg=None):
        """'read' / 'send command and fetch result' sequence. Returns a list of CRC-checked words."""
        async with self._lock:
            self._write(cmd, arg)
            await asyncio.sleep(delay)              # must wait before issuing the read header
            msg = i2c_msg.read(self.address, words * 3)
            self._bus.i2c_rdwr(msg)
        raw = list(msg)
        result = []
        for i in range(0, len(raw), 3):
            word, crc = raw[i:i + 2], raw[i + 2]
            if self._crc8(word) != crc:
                raise ValueError(f"SCD41 CRC mismatch on command 0x{cmd:04X}")
            result.append(word[0] << 8 | word[1])
        return result

    # ---- Measurement (§3.6, §3.9, §3.11) ----

    async def start_periodic_measurement(self):
        """New reading every 5 s."""
        await self._send(self.START_PERIODIC)

    async def start_low_power_periodic_measurement(self):
        """New reading every ~30 s."""
        await self._send(self.START_LOW_POWER)

    async def stop_periodic_measurement(self):
        await self._send(self.STOP_PERIODIC, delay=0.5)

    async def get_data_ready_status(self):
        (word,) = await self._read(self.DATA_READY)
        return (word & 0x07FF) != 0                 # lowest 11 bits non-zero = ready

    async def read_measurement(self):
        """Read the latest result. Raises OSError (NACK) if no new data is available."""
        co2, t, rh = await self._read(self.READ_MEASUREMENT, words=3)
        return Measurement(co2, -45 + 175 * t / 65535, 100 * rh / 65535)

    async def measurements(self, poll=1.0):
        """Async generator that yields each new periodic measurement as it becomes ready."""
        while True:
            if await self.get_data_ready_status():
                yield await self.read_measurement()
            else:
                await asyncio.sleep(poll)

    async def measure_single_shot(self):
        """On-demand CO2 + RH/T measurement (takes 5 s). Sensor must be idle."""
        await self._send(self.SINGLE_SHOT, delay=5.0)
        return await self.read_measurement()

    async def measure_single_shot_rht_only(self):
        """On-demand RH/T measurement (50 ms). CO2 is returned as 0."""
        await self._send(self.SINGLE_SHOT_RHT, delay=0.05)
        return await self.read_measurement()

    async def power_down(self):
        await self._send(self.POWER_DOWN)

    async def wake_up(self):
        """The sensor does not ACK wake_up, so the resulting I/O error is ignored."""
        async with self._lock:
            try:
                self._write(self.WAKE_UP)
            except OSError:
                pass
            await asyncio.sleep(0.03)

    # ---- Signal compensation (§3.7) ----

    async def get_temperature_offset(self):
        (word,) = await self._read(self.GET_TEMP_OFFSET)
        return word * 175 / 65535

    async def set_temperature_offset(self, offset_c):
        """Default 4 °C. Affects RH/T output only, not CO2."""
        await self._send(self.SET_TEMP_OFFSET, round(offset_c * 65535 / 175))

    async def get_sensor_altitude(self):
        (word,) = await self._read(self.GET_ALTITUDE)
        return word

    async def set_sensor_altitude(self, metres):
        """0 – 3000 m above sea level."""
        await self._send(self.SET_ALTITUDE, int(metres))

    async def get_ambient_pressure(self):
        (word,) = await self._read(self.AMBIENT_PRESSURE)
        return word * 100

    async def set_ambient_pressure(self, pascals):
        """70 000 – 120 000 Pa. Overrides altitude compensation. Allowed during measurement."""
        await self._send(self.AMBIENT_PRESSURE, round(pascals / 100))

    # ---- Field calibration (§3.8) ----

    async def perform_forced_recalibration(self, target_ppm):
        """Run after >3 min of measuring in a stable, known CO2 level, then stop. Returns correction in ppm."""
        (word,) = await self._read(self.FORCED_RECAL, delay=0.4, arg=int(target_ppm))
        if word == 0xFFFF:
            raise RuntimeError("SCD41 forced recalibration failed")
        return word - 0x8000

    async def get_automatic_self_calibration_enabled(self):
        (word,) = await self._read(self.GET_ASC_ENABLED)
        return bool(word)

    async def set_automatic_self_calibration_enabled(self, enabled):
        await self._send(self.SET_ASC_ENABLED, int(bool(enabled)))

    async def get_automatic_self_calibration_target(self):
        (word,) = await self._read(self.GET_ASC_TARGET)
        return word

    async def set_automatic_self_calibration_target(self, ppm):
        await self._send(self.SET_ASC_TARGET, int(ppm))

    # ---- Advanced (§3.10) ----

    async def persist_settings(self):
        """Save config to EEPROM (rated for about 2000 writes, so use sparingly)."""
        await self._send(self.PERSIST_SETTINGS, delay=0.8)

    async def get_serial_number(self):
        w0, w1, w2 = await self._read(self.SERIAL_NUMBER, words=3)
        return w0 << 32 | w1 << 16 | w2

    async def perform_self_test(self):
        """Takes 10 s. Returns True if no malfunction was detected."""
        (word,) = await self._read(self.SELF_TEST, delay=10.0)
        return word == 0

    async def perform_factory_reset(self):
        """Erases EEPROM settings and FRC/ASC history."""
        await self._send(self.FACTORY_RESET, delay=1.2)

    async def reinit(self):
        """Reload settings from EEPROM."""
        await self._send(self.REINIT, delay=0.03)

    async def get_sensor_variant(self):
        (word,) = await self._read(self.SENSOR_VARIANT)
        return self.VARIANTS.get(word >> 12, "unknown")
