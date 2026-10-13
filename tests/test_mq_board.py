"""MQ gas board ($PMQB) decoder, tested against docs/uart-interface.md."""

from __future__ import annotations

import math
import re
from functools import reduce
from operator import xor
from pathlib import Path

import pytest

from measure_station.uart import ParseError, Received
from measure_station.uart.mq_board import KNOWN_SENSORS, MqBoardDecoder, parse_sentence

# Every example sentence in docs/uart-interface.md, verbatim.
SPEC_EXAMPLES = [
    "$PMQB,MQ4,86451,CH4,17.85,23.19,1,5.401877*2E",
    "$PMQB,MQ2,26,SMK,12.97,22.44,0,0.606521*78",
    "$PMQB,MQ2,,SMK,,,,*59",
    "$PMQB,MQ4,,CH4,,,,*35",
    "$PMQB,MQ5,,LPG,,,,*50",
    "$PMQB,MQ8,,H2,,,,*7C",
    "$PMQB,MQ135,,NH3,,,,*3C",
    "$PMQB,MQ7,,CO,,,,*05",
    "$PMQB,MQ4,51,CH4,16.32,22.44,0,5.383251*12",
    "$PMQB,MQ5,76,LPG,0.85,22.44,0,4.912730*48",
    "$PMQB,MQ8,101,H2,52.54,22.50,0,1.274406*63",
    "$PMQB,MQ135,126,NH3,4.30,22.50,0,8.031977*16",
    "$PMQB,MQ7,151,CO,0.65,22.50,0,2.150834*21",
    "$PMQB,MQ2,176,SMK,12.81,22.50,0,0.606521*4E",
    "$PMQB,MQ2,86426,SMK,14.02,23.19,1,0.611204*46",
    "$PMQB,MQ5,86326,LPG,0.91,23.13,1,4.928110*7A",
    "$PMQB,MQ8,86351,H2,55.10,23.13,1,1.280533*66",
    "$PMQB,MQ135,86376,NH3,4.62,23.13,1,8.064209*1D",
    "$PMQB,MQ7,86401,CO,0.71,23.19,1,2.163390*22",
    "$PMQB,MQ4,86601,CH4,,23.19,1,5.401877*0C",
    "$PMQB,MQ2,175,SMK,12.97,,0,0.612840*6E",
    "$PMQB,MQ4,51,CH4,2.87,22.44,0,10.000000*13",
    "$PMQB,MQ2,259226,SMK,14.10,23.44,1,0.611204*7B",
    "$PMQB,MQ2,26,SMK,14.08,23.50,1,0.611204*7B",
]

GAS = {"MQ2": "SMK", "MQ4": "CH4", "MQ5": "LPG", "MQ7": "CO", "MQ8": "H2", "MQ135": "NH3"}


def sentence(
    name: str, uptime_s: int | None = None, concentration_ppm: float = 1.0, address: str = "PMQB"
) -> bytes:
    """Build a valid sentence (empty fields when uptime_s is None, as at board boot)."""
    if uptime_s is None:
        body = f"{address},{name},,{GAS.get(name, 'X')},,,,"
    else:
        gas = GAS.get(name, "X")
        body = f"{address},{name},{uptime_s},{gas},{concentration_ppm:.2f},22.50,1,1.234567"
    cs = reduce(xor, body.encode(), 0)
    return f"${body}*{cs:02X}\r\n".encode()


def at(boot_ts: float) -> Received:
    return Received(boot_ts=boot_ts, wall_ts=1_700_000_000 + boot_ts)


# --------------------------------------------------------------------------- parsing


INTERFACE_DOC = Path(__file__).parents[1] / "docs" / "uart-interface.md"


def test_interface_doc_examples_are_valid_and_match_the_fixtures() -> None:
    documented = re.findall(r"\$PMQB\S*\*[0-9A-F]{2}", INTERFACE_DOC.read_text(encoding="utf-8"))
    assert set(documented) == set(SPEC_EXAMPLES)
    for line in documented:
        assert parse_sentence(line.encode()) is not None, line


class TestParseSentence:
    @pytest.mark.parametrize("line", SPEC_EXAMPLES)
    def test_every_spec_example_parses(self, line: str) -> None:
        assert parse_sentence(line.encode()) is not None

    def test_fields(self) -> None:
        s = parse_sentence(b"$PMQB,MQ4,86451,CH4,17.85,23.19,1,5.401877*2E\r\n")
        assert s == ("MQ4", 86451, "CH4", 17.85, 23.19, True, 5.401877)

    def test_empty_fields_are_null_not_zero(self) -> None:
        s = parse_sentence(b"$PMQB,MQ2,,SMK,,,,*59")
        assert (
            s.uptime_s,
            s.gas,
            s.concentration_ppm,
            s.temperature_c,
            s.baseline_tracking,
            s.r0_kohm,
        ) == (
            None,
            "SMK",
            None,
            None,
            None,
            None,
        )

    def test_read_fault_has_new_t_s_and_null_ppm(self) -> None:
        s = parse_sentence(b"$PMQB,MQ4,86601,CH4,,23.19,1,5.401877*0C")
        assert (s.uptime_s, s.concentration_ppm, s.temperature_c) == (86601, None, 23.19)

    def test_bad_checksum_rejected(self) -> None:
        with pytest.raises(ParseError, match="checksum mismatch"):
            parse_sentence(b"$PMQB,MQ4,86451,CH4,17.86,23.19,1,5.401877*2E")

    @pytest.mark.parametrize(
        "line",
        [
            b"PMQB,MQ2,26,SMK,12.97,22.44,0,0.606521*78",  # no '$'
            b"$PMQB,MQ2,26,SMK,12.97,22.44,0,0.606521",  # no checksum
            b"$PMQB,MQ2,26,SMK,12.97,22.44,0,0.606521*7",  # one hex digit
            b"$PMQB,MQ2,26,SMK,12.97,22.44,0,0.606521*+8",
            "$PMQB,MQ2,26,SMK,12.97,22.44,0,0.60652°*78".encode(),  # non-ASCII
        ],
    )
    def test_malformed_framing_rejected(self, line: bytes) -> None:
        with pytest.raises(ParseError):
            parse_sentence(line)

    def test_leading_noise_including_dollar_is_skipped(self) -> None:
        line = b"\x00\xff$garb$PMQB,MQ2,26,SMK,12.97,22.44,0,0.606521*78"
        assert parse_sentence(line).uptime_s == 26

    def test_other_address_ignored(self) -> None:
        assert parse_sentence(sentence("MQ2", 26, address="GPGGA")) is None

    def test_too_few_fields_rejected(self) -> None:
        body = "PMQB,MQ2,26,SMK"
        line = f"${body}*{reduce(xor, body.encode(), 0):02X}".encode()
        with pytest.raises(ParseError):
            parse_sentence(line)

    def test_extra_trailing_fields_ignored(self) -> None:
        body = "PMQB,MQ2,26,SMK,12.97,22.44,0,0.606521,future,field"
        line = f"${body}*{reduce(xor, body.encode(), 0):02X}".encode()
        assert parse_sentence(line).r0_kohm == 0.606521

    @pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
    def test_non_finite_numbers_become_null(self, value: str) -> None:
        body = f"PMQB,MQ2,26,SMK,{value},22.44,0,0.606521"
        line = f"${body}*{reduce(xor, body.encode(), 0):02X}".encode()
        assert parse_sentence(line).concentration_ppm is None


# --------------------------------------------------------------------------- decoding


def broadcast(decoder: MqBoardDecoder, when: float, uptime_s: dict[str, int | None]) -> list:
    """Feed one broadcast (all six sentences) received at station time ``when``."""
    out = []
    for name in KNOWN_SENSORS:
        out += decoder.feed(sentence(name, uptime_s.get(name)), at(when))
    return out


EMPTY: dict[str, int | None] = {}


class TestDecoder:
    def test_boot_broadcast_produces_nothing(self) -> None:
        decoder = MqBoardDecoder()
        assert broadcast(decoder, 100, EMPTY) == []

    def test_each_reading_emitted_once_and_timestamped(self) -> None:
        decoder = MqBoardDecoder()
        # Board booted at station time 1000; MQ2 read at uptime_s=26 (station 1026).
        broadcast(decoder, 1020, EMPTY)
        (reading,) = broadcast(decoder, 1030, {"MQ2": 26})
        assert broadcast(decoder, 1040, {"MQ2": 26}) == []  # repeats dropped
        assert broadcast(decoder, 1050, {"MQ2": 26}) == []

        assert reading.source == "mqboard-mq2"
        assert reading.values["uptime_s"] == 26
        assert reading.values["gas"] == "SMK"
        assert reading.values["baseline_tracking"] is True
        assert reading.values["ts_basis"] == "board"
        assert reading.seen == ("MQ2", 26)
        # One fresh sample with a 4 s broadcast delay: estimate is within 4 s.
        assert 1026 <= reading.boot_ts <= 1030
        assert reading.timestamp == pytest.approx(1_700_000_000 + reading.boot_ts)

    def test_readings_already_on_the_line_are_held_until_an_estimate_exists(self) -> None:
        decoder = MqBoardDecoder()
        # Station starts mid-run: these readings may be up to 150 s old.
        state = {"MQ2": 826, "MQ4": 851, "MQ5": 726, "MQ8": 751, "MQ135": 776, "MQ7": 801}
        for when in (2000, 2010, 2020):  # nothing changes for a couple of broadcasts
            assert broadcast(decoder, when, state) == []
        assert decoder.status(2020)["held"] == 6

        # Board booted at station 1150, so MQ4's next reading (uptime_s=876) was taken at 2026.
        released = broadcast(decoder, 2030, state | {"MQ4": 876})
        assert len(released) == 7
        assert decoder.status(2030)["held"] == 0
        by_source = {r.source: r for r in released}
        assert by_source["mqboard-mq4"].boot_ts == pytest.approx(2026, abs=4)
        # Held readings get their true capture times, not their arrival time (2000).
        assert by_source["mqboard-mq5"].boot_ts == pytest.approx(1150 + 726, abs=4)

    def test_restart_detected_when_t_s_goes_backwards(self) -> None:
        decoder = MqBoardDecoder()
        broadcast(decoder, 990, {"MQ2": 259201})
        broadcast(decoder, 1000, {"MQ2": 259226})
        (reading,) = broadcast(decoder, 1010, {"MQ2": 26})
        assert reading.values["uptime_s"] == 26

    def test_restart_detected_from_empty_sentences(self) -> None:
        decoder = MqBoardDecoder()
        broadcast(decoder, 990, {"MQ2": 259201})
        broadcast(decoder, 1000, {"MQ2": 259226})
        for when in (1010, 1020, 1030):  # board rebooted: all empty until first read
            assert broadcast(decoder, when, EMPTY) == []
        (reading,) = broadcast(decoder, 1040, {"MQ2": 26})
        # Board rebooted at ~1010; fresh estimate from this sample.
        assert reading.boot_ts <= 1040

    def test_persisted_last_seen_suppresses_repeats_after_station_restart(self) -> None:
        decoder = MqBoardDecoder(last_seen={"MQ2": 26, "MQ4": 51})
        assert broadcast(decoder, 100, {"MQ2": 26, "MQ4": 51}) == []
        assert decoder.status(100)["held"] == 0

    def test_unknown_sensor_ignored(self) -> None:
        decoder = MqBoardDecoder()
        assert decoder.feed(sentence("MQ999", 26), at(100)) == []

    def test_read_fault_is_emitted_with_null_ppm(self) -> None:
        decoder = MqBoardDecoder()
        decoder.feed(sentence("MQ4", 86451), at(100))  # held: first sight
        line = b"$PMQB,MQ4,86601,CH4,,23.19,1,5.401877*0C"
        held, fault = decoder.feed(line, at(108))  # fresh: estimate exists, both released
        assert held.values["concentration_ppm"] == 1.0
        assert fault.values["uptime_s"] == 86601
        assert fault.values["concentration_ppm"] is None

    def test_falls_back_to_receipt_time_when_no_estimate_arrives(self) -> None:
        decoder = MqBoardDecoder(max_hold_s=300)
        decoder.feed(sentence("MQ2", 826), at(1000))  # first sight: not fresh
        assert decoder.check(1200) == []
        (reading,) = decoder.check(1301)
        assert reading.values["ts_basis"] == "receipt"
        assert reading.boot_ts == 1000

    def test_link_down_and_stale_sensor_reporting(self) -> None:
        decoder = MqBoardDecoder(started_at=0, link_down_s=30, stale_sensor_s=300)
        assert decoder.status(10)["up"] is True
        decoder.check(31)
        assert decoder.status(31)["up"] is False

        # Broadcasts resume, but MQ4's uptime_s never changes.
        for i in range(40):
            now = 40 + 10 * i
            broadcast(decoder, now, {"MQ2": 26 + 25 * (i // 3), "MQ4": 51})
            decoder.check(now)
        status = decoder.status(now)
        assert status["up"] is True
        assert "MQ4" in status["stale_sensors"]
        assert "MQ2" not in status["stale_sensors"]


# --------------------------------------------------------------------------- simulation


class BoardSim:
    """The board as docs/uart-interface.md describes it.

    Boots at station time ``boot_at``; reads one sensor every 25 s from uptime 26 in a
    fixed order; broadcasts the latest reading of each sensor every ``period`` s.
    ``drift`` scales the board's clock (1.00005 = 50 concentration_ppm fast).
    """

    ORDER = ("MQ2", "MQ4", "MQ5", "MQ8", "MQ135", "MQ7")

    def __init__(self, boot_at: float, period: float = 10.07, drift: float = 1.0) -> None:
        self.boot_at, self.period, self.drift = boot_at, period, drift

    def uptime(self, station_t: float) -> float:
        return (station_t - self.boot_at) * self.drift

    def readings_until(self, station_t: float) -> dict[str, list[int]]:
        """uptime_s of every reading each sensor has taken by ``station_t``."""
        out: dict[str, list[int]] = {n: [] for n in self.ORDER}
        j = 0
        while 26 + 25 * j <= self.uptime(station_t):
            out[self.ORDER[j % 6]].append(26 + 25 * j)
            j += 1
        return out

    def capture_time(self, uptime_s: int) -> float:
        return self.boot_at + uptime_s / self.drift

    def broadcasts(self, start: float, end: float):
        k = math.ceil((start - self.boot_at) / self.period)
        while (t := self.boot_at + k * self.period) < end:
            latest = {n: (v[-1] if v else None) for n, v in self.readings_until(t).items()}
            yield t, latest
            k += 1


@pytest.mark.parametrize("drift", [1.0, 1.00005, 0.99995])
def test_simulated_hours_every_reading_once_and_accurately_timed(drift: float) -> None:
    board = BoardSim(boot_at=500.0, drift=drift)
    decoder = MqBoardDecoder()
    emitted = []
    end = 500.0 + 6 * 3600
    for t, latest in board.broadcasts(500.0, end):
        emitted += broadcast(decoder, t, latest)
        emitted += decoder.check(t)

    expected = board.readings_until(end - board.period)
    got: dict[str, list[int]] = {n: [] for n in KNOWN_SENSORS}
    for r in emitted:
        got[r.seen.sensor].append(r.seen.uptime_s)
    for name in KNOWN_SENSORS:
        assert got[name] == expected[name], name  # every reading, once, in order

    # After the estimate has settled, timestamps are within ~1 s (uptime_s is whole seconds).
    errors = [
        abs(r.boot_ts - board.capture_time(r.values["uptime_s"]))
        for r in emitted
        if r.boot_ts > 500.0 + 900
    ]
    assert max(errors) < 1.5


def test_simulated_station_restart_mid_run() -> None:
    board = BoardSim(boot_at=0.0)
    # Station restarts at t=5000, having already recorded readings up to t=4990.
    recorded = {n: v[-1] for n, v in board.readings_until(4990).items() if v}
    decoder = MqBoardDecoder(last_seen=recorded)

    emitted = []
    for t, latest in board.broadcasts(5000, 5400):
        emitted += broadcast(decoder, t, latest)

    for r in emitted:  # nothing recorded before the restart is emitted again
        assert r.seen.uptime_s > recorded[r.seen.sensor]
        assert abs(r.boot_ts - board.capture_time(r.values["uptime_s"])) < 11
