# Timestamps Without a Real-Time Clock

The Pi has no battery-backed clock. After a boot without NTP, the system clock resumes
from roughly the last shutdown time and then jumps when NTP syncs. To keep timestamps
accurate anyway:

1. **Every reading records two clocks:** the wall clock, and the *boot clock*
   (`CLOCK_BOOTTIME`, seconds since boot). NTP never changes the boot clock.
2. **Until NTP sync is detected**, readings are stored in the outbox as `pending`, tagged
   with the kernel's `boot_id`, and are **not published**.
3. **When sync is detected**, the station computes `offset = wall clock − boot clock`.
   One `UPDATE` then sets `ts = boot_ts + offset` for every pending reading from this
   boot, and those readings are released as `corrected`.
4. **Some readings can't be corrected.** If the Pi rebooted before ever syncing, that
   boot's offset is unknowable. Those readings are released immediately as `estimated`.
   So are readings held longer than `TIME_SYNC_MAX_HOLD_S` (24 h by default).

Settings: `TIME_SYNC_POLL_S` and `TIME_SYNC_MAX_HOLD_S` in `sysconfig.py`. While held,
readings show up in the health topic as `awaiting_time_sync`.

The fewer readings that need correcting, the better. Two ways to get there:

- **Use a local NTP server** (below), so the clock syncs whenever the local network is
  up, even without internet access.
- **Add an RTC module** (e.g. DS3231) for accurate time through outages *and* reboots.

## Using a Local NTP Server

NTP needs a *reachable* NTP server, not internet access. By default the Pi uses Debian's
public pool, so a station without internet never syncs. To use a server on the local
network instead (typically the broker host), list it in `sysconfig.py`, in order of
preference:

```python
NTP_SERVERS: list[str] = _env_list("NTP_SERVERS") or ["192.168.1.10", "pool.ntp.org"]
```

Or set it per device in `/etc/measure-station/env`:
`MEASURE_STATION_NTP_SERVERS=192.168.1.10,pool.ntp.org`. Then run
`sudo systemctl restart measure-station`.

How it works:

- On every service start, a small step (`measure_station.ntp_setup`) runs as root before
  the station itself. It writes the list to
  `/etc/systemd/timesyncd.conf.d/measure-station.conf`, and restarts `systemd-timesyncd`
  only if the list changed.
- An empty list removes that file, so the OS defaults return.
- Entries must be plain host names or IP addresses. Anything else is a configuration
  error and the station won't start.
- If this step fails, the error is logged and the station still starts, keeping the
  previous NTP settings.
- It only configures `systemd-timesyncd`, the Raspberry Pi OS default. If you've switched
  to another NTP daemon (e.g. chrony), it logs a warning and you configure that daemon
  yourself.

To check it's working, run `timedatectl timesync-status`. The `Server:` line should show
your local server. The health topic's `clock_synced` should become `true` shortly after
boot.

**Setting up the server** (on the broker host or any always-on machine on the LAN), with
chrony as an example:

```bash
sudo apt-get install -y chrony
echo "allow 192.168.1.0/24" | sudo tee /etc/chrony/conf.d/stations.conf   # your station subnet
# Only if this host itself has no internet/upstream NTP: serve its own clock anyway,
# so stations at least agree with the server (and with each other).
echo "local stratum 10" | sudo tee -a /etc/chrony/conf.d/stations.conf
sudo systemctl restart chrony
```

Allow UDP port 123 through any firewall between the stations and the server. If the server
has no upstream time source, all timestamps are only as accurate as its clock. That's
still consistent across stations, and still far better than a Pi that never syncs.
