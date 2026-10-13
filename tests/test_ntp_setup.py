from pathlib import Path

import pytest

from measure_station.ntp_setup import apply, invalid_servers, render_dropin


def test_render_lists_servers_in_order() -> None:
    assert render_dropin(["192.168.1.10", "pool.ntp.org"]).splitlines()[1:] == [
        "[Time]",
        "NTP=192.168.1.10 pool.ntp.org",
    ]


def test_empty_means_os_defaults() -> None:
    assert render_dropin([]) is None


@pytest.mark.parametrize(
    "good", ["ntp.local", "192.168.1.10", "fe80::1%eth0", "time-1.example.com"]
)
def test_accepts_hosts_and_addresses(good: str) -> None:
    assert invalid_servers([good]) == []


@pytest.mark.parametrize("bad", ["", "a b", "host\nNTP=evil", "trailing.newline\n", "x;rm", 123])
def test_rejects_entries_that_could_corrupt_the_config(bad: object) -> None:
    assert invalid_servers([bad]) == [bad]


def test_apply_writes_only_on_change_and_removes_when_cleared(tmp_path: Path) -> None:
    path = tmp_path / "timesyncd.conf.d" / "measure-station.conf"

    assert apply(["192.168.1.10"], path=path) is True
    assert "NTP=192.168.1.10" in path.read_text()
    assert apply(["192.168.1.10"], path=path) is False  # unchanged: no timesyncd restart

    assert apply([], path=path) is True
    assert not path.exists()
    assert apply([], path=path) is False


def test_apply_refuses_invalid_entries(tmp_path: Path) -> None:
    path = tmp_path / "x.conf"
    with pytest.raises(ValueError):
        apply(["good.host", "bad host"], path=path)
    assert not path.exists()
