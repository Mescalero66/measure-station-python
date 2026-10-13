from measure_station.uart import LineBuffer


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
