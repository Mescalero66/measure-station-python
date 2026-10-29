# Asynchronous Python driver for the Sensirion SEN6x environmental sensor modules:
#   SEN62 (SEN62-SIN-T): particulate matter, temperature & humidity  <https://sensirion.com/products/catalog/SEN62>
#   SEN65 (SEN65-SIN-T): as SEN62, plus VOC & NOx indices             <https://sensirion.com/products/catalog/SEN65>
# The model is detected automatically from the sensor itself.
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
    voc_index: float | None     # 1–500, always None on the SEN62
    nox_index: float | None     # 1–500, always None on the SEN62


@dataclass
class RawValues:
    humidity: float | None      # %RH (uncompensated)
    temperature: float | None   # °C (uncompensated)
    voc: int | None             # raw VOC ticks, always None on the SEN62
    nox: int | None             # raw NOx ticks, always None on the SEN62


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
    gas_error: bool             # bit 7, sticky (VOC/NOx sensor, SEN65 only)
    rht_error: bool             # bit 6, sticky
    fan_error: bool             # bit 4, sticky


@dataclass
class AlgorithmTuning:          # VOC / NOx index algorithm parameters (§4.8.26 – §4.8.31)
    index_offset: int
    learning_time_offset_hours: int
    learning_time_gain_hours: int
    gating_max_duration_minutes: int
    std_initial: int
    gain_factor: int


@dataclass(frozen=True)
class Model:                    # what differs between the supported models (§4.8, Table 27)
    name: str
    read_values: int            # Read Measured Values command
    read_raw_values: int        # Read Measured Raw Values command
    gas: bool                   # has the VOC/NOx sensor
    heater_readout_fw: tuple    # first firmware with Get SHT Heater Measurements (§4.8.25)


def _uint(word, scale):
    """Scale an unsigned value. 0xFFFF means unknown."""
    return None if word == 0xFFFF else word / scale


def _int(word, scale):
    """Scale a signed (two's complement) value. 0x7FFF means unknown."""
    if word == 0x7FFF:
        return None
    return (word - 0x10000 if word & 0x8000 else word) / scale


def _ticks(word):
    """Raw VOC/NOx ticks, unscaled. 0xFFFF means unknown."""
    return None if word == 0xFFFF else word


def _scaled(value, scale, signed, name):
    """Scale an argument to a 16-bit word. Out-of-range values raise instead of silently wrapping in _write."""
    word = round(value * scale)
    lo, hi = (-0x8000, 0x7FFF) if signed else (0, 0xFFFF)
    if not lo <= word <= hi:
        raise ValueError(f"{name} must be {lo / scale:g} to {hi / scale:g}")
    return word


def _in_range(value, lo, hi, name):
    """Check an algorithm tuning argument. The sensor silently ignores the whole command if any is out of range."""
    value = int(value)
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be {lo} to {hi}")
    return value


class SEN6x:
    ADDRESS = 0x6B                                  # fixed I2C address (§4.4)

    # Supported models, keyed by the Get Product Type value (§4.8.16)
    MODELS = {
        0x00085800: Model("SEN62", read_values=0x04A3, read_raw_values=0x0492, gas=False, heater_readout_fw=(6, 0)),
        0x00085200: Model("SEN65", read_values=0x0446, read_raw_values=0x0455, gas=True, heater_readout_fw=(5, 0)),
    }

    # Command codes common to the SEN62 and SEN65 (§4.8, Table 27)
    START_MEASUREMENT       = 0x0021
    STOP_MEASUREMENT        = 0x0104
    DATA_READY              = 0x0202
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
    # SEN65 only (VOC/NOx)
    VOC_TUNING              = 0x60D0
    VOC_STATE               = 0x6181
    NOX_TUNING              = 0x60E1

    # Device status register bits (§4.3)
    STATUS_FAN_SPEED        = 1 << 21
    STATUS_PM               = 1 << 11
    STATUS_GAS              = 1 << 7
    STATUS_RHT              = 1 << 6
    STATUS_FAN              = 1 << 4

    def __init__(self, bus=1, address=ADDRESS):
        """bus: I2C bus number. The SEN6x supports standard mode only (≤ 100 kHz)."""
        self.address = address
        self._bus = SMBus(bus)
        self._lock = asyncio.Lock()                 # one command at a time (§4.7)
        self._model = None

    # ---- Context manager: stop any running measurement, reset to defaults, identify the model ----

    async def __aenter__(self):
        await self.stop_measurement()               # in case a previous run left it measuring
        await self.device_reset()
        await self.get_model()
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
                raise ValueError(f"SEN6x CRC mismatch on command 0x{cmd:04X}")
            result.append(word[0] << 8 | word[1])
        return result

    async def _read_bytes(self, cmd, words):
        """Read `words` words and return them as a byte string (MSB first)."""
        return bytes(b for w in await self._read(cmd, words) for b in (w >> 8, w & 0xFF))

    async def _read_string(self, cmd):
        """Null-terminated ASCII string of up to 32 characters (16 words)."""
        return (await self._read_bytes(cmd, 16)).split(b"\x00")[0].decode("ascii")

    # ---- Model detection ----

    async def get_model(self):
        """Identify the connected sensor and return its Model (name "SEN62" or "SEN65"). Read once, then cached.
        Uses Get Product Type, falling back to the product name on firmware that predates that command."""
        if self._model is None:
            self._model = await self._detect_model()
        return self._model

    async def _detect_model(self):
        try:
            model = self.MODELS.get(int(await self.get_product_type(), 16))   # "0x00085200" or "00085200"
            if model:
                return model
        except (OSError, ValueError):               # command not supported, or not a hex number
            pass
        name = await self.get_product_name()
        for model in self.MODELS.values():
            if model.name in name:
                return model
        raise RuntimeError(f"unsupported SEN6x model {name!r}, this driver handles the SEN62 and SEN65")

    async def _require_gas(self):
        model = await self.get_model()
        if not model.gas:
            raise RuntimeError(f"VOC/NOx commands need a SEN65, the connected sensor is a {model.name}")

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
        """Latest PM mass concentrations, compensated RH/T and (SEN65) VOC/NOx indices. Unknown values are None.
        Returns the previous values again if no new data is ready."""
        model = await self.get_model()
        w = await self._read(model.read_values, words=8 if model.gas else 6)
        voc = nox = None
        if model.gas:
            voc, nox = _int(w[6], 10), _int(w[7], 10)
        return Measurement(*(_uint(x, 10) for x in w[:4]), _int(w[4], 100), _int(w[5], 200), voc, nox)

    async def measurements(self, poll=0.2):
        """Async generator that yields each new measurement (one per second) as it becomes ready."""
        while True:
            if await self.get_data_ready():
                yield await self.read_measured_values()
            else:
                await asyncio.sleep(poll)

    async def read_measured_raw_values(self):
        """Uncompensated RH/T straight from the RH/T sensor, plus (SEN65) raw VOC/NOx ticks."""
        model = await self.get_model()
        w = await self._read(model.read_raw_values, words=4 if model.gas else 2)
        voc = nox = None
        if model.gas:
            voc, nox = _ticks(w[2]), _ticks(w[3])
        return RawValues(_int(w[0], 100), _int(w[1], 200), voc, nox)

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
                         (_scaled(offset, 200, True, "offset"), _scaled(slope, 10000, True, "slope"),
                          _scaled(time_constant, 1, False, "time_constant"), int(slot)))

    async def set_temperature_acceleration_parameters(self, k, p, t1, t2):
        """Override the RH/T engine's acceleration filter (t1, t2 in s). See the app note
        "SEN6x – Temperature Acceleration and Compensation Instructions". Idle mode only. Volatile."""
        await self._send(self.TEMP_ACCEL_PARAMS,
                         (_scaled(k, 10, False, "k"), _scaled(p, 10, False, "p"),
                          _scaled(t1, 10, False, "t1"), _scaled(t2, 10, False, "t2")))

    # ---- VOC / NOx index algorithms, SEN65 only (§4.8.26 – §4.8.31) ----

    async def get_voc_algorithm_tuning_parameters(self):
        """Current VOC index algorithm parameters as AlgorithmTuning. Idle mode only."""
        await self._require_gas()
        return AlgorithmTuning(*await self._read(self.VOC_TUNING, words=6))

    async def set_voc_algorithm_tuning_parameters(self, index_offset=100, learning_time_offset_hours=12,
                                                  learning_time_gain_hours=12, gating_max_duration_minutes=180,
                                                  std_initial=50, gain_factor=230):
        """Customise the VOC index algorithm. The defaults are the sensor's own. Idle mode only. Volatile."""
        await self._require_gas()
        await self._send(self.VOC_TUNING,
                         (_in_range(index_offset, 1, 250, "index_offset"),
                          _in_range(learning_time_offset_hours, 1, 1000, "learning_time_offset_hours"),
                          _in_range(learning_time_gain_hours, 1, 1000, "learning_time_gain_hours"),
                          _in_range(gating_max_duration_minutes, 0, 3000, "gating_max_duration_minutes"),
                          _in_range(std_initial, 10, 5000, "std_initial"),
                          _in_range(gain_factor, 1, 1000, "gain_factor")))

    async def get_voc_algorithm_state(self):
        """8-byte VOC algorithm state. Save it and restore it with set_voc_algorithm_state() after a reset or
        power cycle to skip the algorithm's learning phase. Works while measuring (current state) and in idle
        mode (state when the measurement stopped)."""
        await self._require_gas()
        return await self._read_bytes(self.VOC_STATE, 4)

    async def set_voc_algorithm_state(self, state):
        """Restore a state from get_voc_algorithm_state(). Applied when the next measurement starts. Idle mode only."""
        await self._require_gas()
        if len(state) != 8:
            raise ValueError("state must be 8 bytes")
        await self._send(self.VOC_STATE, [state[i] << 8 | state[i + 1] for i in range(0, 8, 2)])

    async def get_nox_algorithm_tuning_parameters(self):
        """Current NOx index algorithm parameters as AlgorithmTuning. Idle mode only."""
        await self._require_gas()
        return AlgorithmTuning(*await self._read(self.NOX_TUNING, words=6))

    async def set_nox_algorithm_tuning_parameters(self, index_offset=1, learning_time_offset_hours=12,
                                                  gating_max_duration_minutes=720, gain_factor=230):
        """Customise the NOx index algorithm. The defaults are the sensor's own. Learning time gain and std initial
        have no effect for NOx and are always sent as the required 12 and 50. Idle mode only. Volatile."""
        await self._require_gas()
        await self._send(self.NOX_TUNING,
                         (_in_range(index_offset, 1, 250, "index_offset"),
                          _in_range(learning_time_offset_hours, 1, 1000, "learning_time_offset_hours"),
                          12,
                          _in_range(gating_max_duration_minutes, 0, 3000, "gating_max_duration_minutes"),
                          50,
                          _in_range(gain_factor, 1, 1000, "gain_factor")))

    # ---- Device information and status (§4.3, §4.8.16 – §4.8.21) ----

    async def get_product_type(self):
        """Returns "0x00085800" on the SEN62 and "0x00085200" on the SEN65."""
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
                            bool(raw & self.STATUS_GAS), bool(raw & self.STATUS_RHT), bool(raw & self.STATUS_FAN))

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
        """(humidity %RH, temperature °C) taken after heating, or (None, None) if not ready. Idle mode only.
        Needs firmware ≥ 6.0 on the SEN62 or ≥ 5.0 on the SEN65 (Model.heater_readout_fw)."""
        rh, t = await self._read(self.SHT_HEATER_RESULT, words=2)
        return _int(rh, 100), _int(t, 200)

    async def wait_sht_heater_measurements(self, timeout=1.0):
        """Poll get_sht_heater_measurements every 50 ms (§4.8.25) until heating has finished, then return
        (humidity %RH, temperature °C). Raises TimeoutError after about `timeout` s."""
        for _ in range(max(1, round(timeout / 0.05))):
            rh, t = await self.get_sht_heater_measurements()
            if rh is not None:
                return rh, t
            await asyncio.sleep(0.05)
        raise TimeoutError("SEN6x SHT heater measurements not ready")
