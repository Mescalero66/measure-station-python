import pytest

from measure_station.uart import LineBuffer, ParseError, UartConfig
from measure_station.uart.parsers import parse_json, parse_key_value


class TestParsers:
    def test_json_object(self) -> None:
        assert parse_json('{"temp_c": 21.5, "ok": true}') == {"temp_c": 21.5, "ok": True}

    @pytest.mark.parametrize("line", ["not json", "[1, 2]", "{}", '"str"'])
    def test_json_rejects_non_objects(self, line: str) -> None:
        with pytest.raises(ParseError):
            parse_json(line)

    def test_key_value_coerces_numbers(self) -> None:
        assert parse_key_value("temp_c=21.5, count=3,state=ok") == {
            "temp_c": 21.5,
            "count": 3,
            "state": "ok",
        }

    @pytest.mark.parametrize("line", ["temp_c", "=3", "a=1,,b=2"])
    def test_key_value_rejects_malformed(self, line: str) -> None:
        with pytest.raises(ParseError):
            parse_key_value(line)


class TestLineBuffer:
    def test_reassembles_lines_across_chunks(self) -> None:
        buf = LineBuffer(max_line_bytes=100)
        assert buf.feed(b"abc") == []
        assert buf.feed(b"def\r\nghi\n") == [b"abcdef", b"ghi"]
        assert buf.feed(b"\n\n") == []  # blank lines ignored

    def test_overlong_complete_line_is_dropped(self) -> None:
        buf = LineBuffer(max_line_bytes=4)
        assert buf.feed(b"toolong\nok\n") == [b"ok"]
        assert buf.overflows == 1

    def test_overlong_partial_line_is_skipped_until_newline(self) -> None:
        buf = LineBuffer(max_line_bytes=4)
        assert buf.feed(b"garbage") == []
        assert buf.feed(b"more-garbage") == []
        assert buf.feed(b"tail\nok\n") == [b"ok"]
        assert buf.overflows == 1


def test_uart_config_rejects_unknown_protocol() -> None:
    with pytest.raises(ValueError):
        UartConfig(name="x", port="/dev/null", protocol="morse")
