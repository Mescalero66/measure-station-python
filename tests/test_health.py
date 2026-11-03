from pathlib import Path

from measure_station.health import CpuUsage, system_stats

MEMINFO = "MemTotal:         948304 kB\nMemFree:          100000 kB\nMemAvailable:     711228 kB\n"


def _stat(user: int, idle: int) -> str:
    # user nice system idle iowait irq softirq steal guest guest_nice
    return f"cpu  {user} 0 0 {idle} 0 0 0 0 0 0\ncpu0 1 0 0 1 0 0 0 0 0 0\n"


def test_cpu_usage_is_measured_between_samples(tmp_path: Path) -> None:
    stat = tmp_path / "stat"
    usage = CpuUsage(stat)
    stat.write_text(_stat(user=1000, idle=9000))
    assert usage.sample() is None  # nothing to compare with yet
    stat.write_text(_stat(user=1250, idle=9750))  # 250 busy of 1000
    assert usage.sample() == 25.0
    stat.write_text(_stat(user=1250, idle=9750))  # no time passed
    assert usage.sample() is None


def test_cpu_usage_unavailable(tmp_path: Path) -> None:
    assert CpuUsage(tmp_path / "missing").sample() is None


def test_system_stats_memory_and_cpu_temperature(tmp_path: Path) -> None:
    (tmp_path / "meminfo").write_text(MEMINFO)
    (tmp_path / "temp").write_text("48312\n")
    stats = system_stats(tmp_path, meminfo=tmp_path / "meminfo", thermal=tmp_path / "temp")
    assert stats["cpu_temperature_c"] == 48.31
    assert stats["mem_available_mb"] == 694.6
    assert stats["ram_usage_pct"] == 25.0
    assert "disk_free_mb" in stats


def test_system_stats_leave_out_what_is_unavailable(tmp_path: Path) -> None:
    stats = system_stats(tmp_path, meminfo=tmp_path / "x", thermal=tmp_path / "y")
    assert not {"cpu_temperature_c", "mem_available_mb", "ram_usage_pct"} & set(stats)
