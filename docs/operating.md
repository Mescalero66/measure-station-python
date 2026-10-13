# Operating

```bash
systemctl status measure-station           # is it running?
journalctl -u measure-station -f           # follow the logs
sudo systemctl restart measure-station     # apply sysconfig.py changes (including NTP_SERVERS)
timedatectl timesync-status                # which NTP server is in use, and sync state
sqlite3 /var/lib/measure-station/outbox.sqlite3 'select count(*) from outbox'   # backlog
```

If the configuration is invalid, the service logs `Configuration error: ...` and stops
instead of restart-looping (exit code 78). Fix the config, then restart the service.

To update the code: `cd /opt/measure-station && sudo git pull && sudo systemctl restart measure-station`.
Local edits to `sysconfig.py` will conflict with upstream changes to that file. Keep
per-device values (host, credentials, station ID) in `/etc/measure-station/env` where possible.
