from pathlib import Path

import pytest

from conftest import FakeTime, make_outbox
from measure_station.clock import ClockState
from measure_station.models import Reading, SeenMarker
from measure_station.storage import Outbox


@pytest.fixture
def outbox(tmp_path: Path, synced_clock: ClockState):
    box = make_outbox(tmp_path / "outbox.sqlite3", synced_clock, max_rows=5)
    yield box
    box.close()


def _add(box: Outbox, fake_time: FakeTime, n: int, start: int = 0) -> None:
    box.add_many(fake_time.reading(source=f"s{i}") for i in range(start, start + n))


def test_fetch_returns_oldest_first_without_removing(outbox: Outbox, fake_time: FakeTime) -> None:
    _add(outbox, fake_time, 3)
    batch = outbox.fetch_batch(2)
    assert [m.source for m in batch] == ["s0", "s1"]
    assert outbox.count() == 3


def test_delete_removes_only_acknowledged(outbox: Outbox, fake_time: FakeTime) -> None:
    _add(outbox, fake_time, 3)
    first, _, third = outbox.fetch_batch(3)
    outbox.delete([first.id, third.id])
    assert [m.source for m in outbox.fetch_batch(10)] == ["s1"]


def test_order_preserved_when_newest_row_deleted_and_id_reused(
    outbox: Outbox, fake_time: FakeTime
) -> None:
    _add(outbox, fake_time, 3)
    newest = outbox.fetch_batch(3)[-1]
    outbox.delete([newest.id])
    _add(outbox, fake_time, 1, start=9)
    assert [m.source for m in outbox.fetch_batch(10)] == ["s0", "s1", "s9"]


def test_prune_discards_oldest_beyond_capacity(outbox: Outbox, fake_time: FakeTime) -> None:
    _add(outbox, fake_time, 8)
    assert outbox.prune() == 3
    assert [m.source for m in outbox.fetch_batch(10)] == ["s3", "s4", "s5", "s6", "s7"]
    assert outbox.prune() == 0


def test_survives_reopen(tmp_path: Path, synced_clock: ClockState, fake_time: FakeTime) -> None:
    path = tmp_path / "outbox.sqlite3"
    box = make_outbox(path, synced_clock)
    _add(box, fake_time, 2)
    box.close()

    reopened = make_outbox(path, synced_clock)
    assert reopened.count() == 2
    reopened.close()


def test_corrupt_file_is_quarantined_and_replaced(
    tmp_path: Path, synced_clock: ClockState, fake_time: FakeTime
) -> None:
    path = tmp_path / "outbox.sqlite3"
    path.write_bytes(b"this is not a sqlite database" * 100)

    box = make_outbox(path, synced_clock)
    assert box.count() == 0
    _add(box, fake_time, 1)
    assert box.count() == 1
    box.close()

    assert list(tmp_path.glob("outbox.sqlite3.corrupt-*"))


def test_rejects_invalid_synchronous_mode(tmp_path: Path, synced_clock: ClockState) -> None:
    with pytest.raises(ValueError):
        Outbox(
            tmp_path / "x.sqlite3",
            clock=synced_clock,
            max_rows=1,
            max_time_hold_s=0,
            synchronous="SOMETIMES",
        )


def test_seen_markers_saved_with_readings(
    tmp_path: Path, synced_clock: ClockState, fake_time: FakeTime
) -> None:
    path = tmp_path / "outbox.sqlite3"
    box = make_outbox(path, synced_clock)
    box.add_many(
        [
            Reading(
                "mqboard-mq2",
                {"ppm": 1},
                fake_time.wall,
                fake_time.boot,
                SeenMarker("MQ2", 26),
            ),
            Reading(
                "mqboard-mq2",
                {"ppm": 2},
                fake_time.wall,
                fake_time.boot,
                SeenMarker("MQ2", 176),
            ),
            Reading(
                "mqboard-mq4",
                {"ppm": 3},
                fake_time.wall,
                fake_time.boot,
                SeenMarker("MQ4", 51),
            ),
            Reading("cpu", {"t": 40}, fake_time.wall, fake_time.boot),
        ]
    )
    box.close()

    reopened = make_outbox(path, synced_clock)
    assert reopened.load_mqboard_last_seen() == {"MQ2": 176, "MQ4": 51}
    assert reopened.count() == 4
    reopened.close()
