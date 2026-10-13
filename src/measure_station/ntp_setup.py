"""Apply ``sysconfig.NTP_SERVERS`` to systemd-timesyncd.

Runs as root before the station starts, via ``ExecStartPre=-+`` in the systemd unit
("+" = full privileges for this step only; "-" = a failure is logged but never stops
the station). So ``sudo systemctl restart measure-station`` applies NTP changes just
like any other sysconfig change.

This is the only code that runs as root: keep it small, and validate everything that
goes into the config file.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from measure_station import sysconfig

log = logging.getLogger("measure_station.ntp_setup")

DROPIN_PATH = Path("/etc/systemd/timesyncd.conf.d/measure-station.conf")

# Hostname, IPv4 or IPv6 address. Crucially rejects whitespace/newlines, which could
# otherwise inject extra lines into a root-owned config file. Used with fullmatch():
# "$" would also match before a trailing newline.
_SERVER = re.compile(r"[A-Za-z0-9.:%\[\]-]{1,253}")


def invalid_servers(servers: Sequence[object]) -> list[object]:
    return [s for s in servers if not isinstance(s, str) or not _SERVER.fullmatch(s)]


def render_dropin(servers: Sequence[str]) -> str | None:
    """timesyncd drop-in content, or None to fall back to the OS default servers."""
    if not servers:
        return None
    return (
        "# Managed by measure-station from sysconfig.NTP_SERVERS; edits will be overwritten.\n"
        "[Time]\n"
        f"NTP={' '.join(servers)}\n"
    )


def apply(servers: Sequence[str], *, path: Path = DROPIN_PATH) -> bool:
    """Write (or remove) the drop-in. Returns True if the file changed."""
    bad = invalid_servers(servers)
    if bad:
        raise ValueError(f"invalid NTP server entries: {bad!r}")
    desired = render_dropin(servers)
    current = path.read_text() if path.exists() else None
    if desired == current:
        return False
    if desired is None:
        path.unlink()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(desired)
        os.replace(tmp, path)  # atomic: timesyncd never sees a half-written file
    return True


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", *args], capture_output=True, text=True, timeout=30, check=False
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s", stream=sys.stdout
    )
    if sys.platform != "linux" or shutil.which("systemctl") is None:
        log.info("Not a systemd host; skipping NTP configuration")
        return 0

    servers = list(sysconfig.NTP_SERVERS)
    try:
        changed = apply(servers)
    except (ValueError, OSError) as exc:
        log.error("Could not apply NTP_SERVERS: %s", exc)
        return 1

    if not changed:
        return 0
    log.info("NTP servers set to %s", " ".join(servers) if servers else "the OS defaults")
    if _systemctl("is-active", "systemd-timesyncd.service").stdout.strip() != "active":
        log.warning(
            "systemd-timesyncd is not active, so NTP_SERVERS has no effect; "
            "configure your NTP daemon (e.g. chrony) manually"
        )
        return 0
    # --no-block: don't wait on another unit's job from inside our own start job.
    _systemctl("--no-block", "restart", "systemd-timesyncd.service")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
