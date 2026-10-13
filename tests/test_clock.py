"""Time correction: readings captured before NTP sync get accurate timestamps afterwards."""

from pathlib import Path

import pytest

from conftest import FakeTime, make_outbox
from measure_station.clock import TimeQuality, boot_clock


class TestResolve:
    def test_pending_until_synced(self, fake_time: FakeTime) -> None:
        clock = fake_time.clock_state()
        assert clock.resolve(fake_time.wall, fake_time.boot) == (
            fake_time.wall,
            TimeQuality.PENDING,
        )

    def test_poll_reports_transition_once(self, fake_time: FakeTime) -> None:
        clock = fake_time.clock_state()
        assert clock.poll() is False
        fake_time.ntp_synced = True
        assert clock.poll() is True
        assert clock.poll() is False
        assert clock.synced

    def test_readings_before_sync_are_back_dated_from_boot_clock(self, fake_time: FakeTime) -> None:
        clock = fake_time.clock_state()
        reading = fake_time.reading()  # wall clock wrong at this point

        fake_time.advance(300)
        fake_time.wall += 86_400  # NTP steps the clock forward a day
        fake_time.ntp_synced = True
        clock.poll()

        ts, quality = clock.resolve(reading.timestamp, reading.boot_ts)
        assert quality is TimeQuality.CORRECTED
        assert ts == pytest.approx(fake_time.wall - 300)

    def test_readings_after_sync_keep_wall_time(self, synced_clock, fake_time: FakeTime) -> None:
        fake_time.advance(5)
        assert synced_clock.resolve(fake_time.wall, fake_time.boot) == (
            fake_time.wall,
            TimeQuality.NTP,
        )


class TestOutboxCorrection:
    def test_held_until_sync_then_corrected_and_released(
        self, tmp_path: Path, fake_time: FakeTime
    ) -> None:
        clock = fake_time.clock_state()
        box = make_outbox(tmp_path / "o.sqlite3", clock)
        captured_boot = fake_time.boot
        box.add_many([fake_time.reading()])

        assert box.fetch_batch(10) == []  # held: time unknown
        assert box.count_awaiting_time() == 1

        fake_time.advance(600)
        fake_time.wall += 3600  # clock was an hour behind
        fake_time.ntp_synced = True
        clock.poll()
        assert box.apply_time_correction() == 1
        assert box.apply_time_correction() == 0  # idempotent

        (row,) = box.fetch_batch(10)
        assert row.ts_quality == TimeQuality.CORRECTED
        assert row.ts == pytest.approx(captured_boot + clock.offset_s)
        assert row.ts == pytest.approx(fake_time.wall - 600)
        box.close()

    def test_readings_added_after_sync_are_resolved_on_insert(
        self, tmp_path: Path, fake_time: FakeTime
    ) -> None:
        clock = fake_time.clock_state()
        box = make_outbox(tmp_path / "o.sqlite3", clock)
        early = fake_time.reading()  # captured pre-sync, still in the recorder's buffer
        fake_time.advance(10)
        fake_time.ntp_synced = True
        clock.poll()
        box.apply_time_correction()  # runs before the buffered reading is written

        box.add_many([early])

        (row,) = box.fetch_batch(10)
        assert row.ts_quality == TimeQuality.CORRECTED
        box.close()

    def test_previous_boot_readings_released_as_estimated(
        self, tmp_path: Path, fake_time: FakeTime
    ) -> None:
        path = tmp_path / "o.sqlite3"
        first_boot = make_outbox(path, fake_time.clock_state(boot_id="boot-A"))
        first_boot.add_many([fake_time.reading()])
        first_boot.close()

        # Rebooted before NTP ever synced: boot clock restarts, offset is unknowable.
        fake_time.boot = 5.0
        second_boot_clock = fake_time.clock_state(boot_id="boot-B")
        second_boot = make_outbox(path, second_boot_clock)

        (row,) = second_boot.fetch_batch(10)
        assert row.boot_id == "boot-A"
        assert row.ts_quality == TimeQuality.PENDING  # published as "estimated"

        fake_time.ntp_synced = True
        second_boot_clock.poll()
        assert second_boot.apply_time_correction() == 0  # never applies another boot's offset
        second_boot.close()

    def test_released_after_max_hold(self, tmp_path: Path, fake_time: FakeTime) -> None:
        box = make_outbox(tmp_path / "o.sqlite3", fake_time.clock_state(), max_time_hold_s=60)
        box.add_many([fake_time.reading()])
        assert box.fetch_batch(10) == []
        fake_time.advance(61)
        assert len(box.fetch_batch(10)) == 1
        box.close()


def test_boot_clock_is_monotonic() -> None:
    assert boot_clock() <= boot_clock()
