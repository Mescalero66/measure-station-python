from pathlib import Path

import pytest

from measure_station.models import OutboxMessage
from measure_station.storage import Outbox


@pytest.fixture
def outbox(tmp_path: Path):
    box = Outbox(tmp_path / "outbox.sqlite3", max_rows=5)
    box.open()
    yield box
    box.close()


def _messages(n: int, start: int = 0) -> list[OutboxMessage]:
    return [OutboxMessage(f"t/{i}", f'{{"i":{i}}}', float(i)) for i in range(start, start + n)]


def test_fetch_returns_oldest_first_without_removing(outbox: Outbox) -> None:
    outbox.add_many(_messages(3))
    batch = outbox.fetch_batch(2)
    assert [m.topic for m in batch] == ["t/0", "t/1"]
    assert outbox.count() == 3


def test_delete_removes_only_acknowledged(outbox: Outbox) -> None:
    outbox.add_many(_messages(3))
    first, _, third = outbox.fetch_batch(3)
    outbox.delete([first.id, third.id])
    assert [m.topic for m in outbox.fetch_batch(10)] == ["t/1"]


def test_order_preserved_when_newest_row_deleted_and_id_reused(outbox: Outbox) -> None:
    outbox.add_many(_messages(3))
    newest = outbox.fetch_batch(3)[-1]
    outbox.delete([newest.id])
    outbox.add_many(_messages(1, start=9))
    assert [m.topic for m in outbox.fetch_batch(10)] == ["t/0", "t/1", "t/9"]


def test_prune_discards_oldest_beyond_capacity(outbox: Outbox) -> None:
    outbox.add_many(_messages(8))
    assert outbox.prune() == 3
    assert [m.topic for m in outbox.fetch_batch(10)] == ["t/3", "t/4", "t/5", "t/6", "t/7"]
    assert outbox.prune() == 0


def test_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "outbox.sqlite3"
    box = Outbox(path, max_rows=100)
    box.open()
    box.add_many(_messages(2))
    box.close()

    reopened = Outbox(path, max_rows=100)
    reopened.open()
    assert reopened.count() == 2
    reopened.close()


def test_corrupt_file_is_quarantined_and_replaced(tmp_path: Path) -> None:
    path = tmp_path / "outbox.sqlite3"
    path.write_bytes(b"this is not a sqlite database" * 100)

    box = Outbox(path, max_rows=100)
    box.open()
    assert box.count() == 0
    box.add_many(_messages(1))
    assert box.count() == 1
    box.close()

    assert list(tmp_path.glob("outbox.sqlite3.corrupt-*"))


def test_rejects_invalid_synchronous_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        Outbox(tmp_path / "x.sqlite3", max_rows=1, synchronous="SOMETIMES")
