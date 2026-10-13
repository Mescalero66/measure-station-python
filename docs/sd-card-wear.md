# SD Card Wear

SQLite rewrites whole 4 KiB pages on every
transaction, so writing each ~200-byte reading on its own costs about 8 KiB of writes.
The recorder therefore groups readings into one transaction per
`RECORDER_FLUSH_INTERVAL_S`, which brings the cost down to about 0.8 KiB per reading (measured).

| `RECORDER_FLUSH_INTERVAL_S` | Writes per reading | Lost on power cut | Extra MQTT latency |
|---|---|---|---|
| `0` | ~8 KiB | nothing already recorded | none |
| `10` (default) | ~0.8 KiB if ≥10 readings arrive per interval | up to 10 s of readings | up to 10 s |

The grouping only pays off when several readings arrive within each interval. A station
with just a few slow sensors writes very little either way. Beyond the software:

- Use a high-endurance card.
- Unclean power loss can damage a card's internal bookkeeping. If mains power is
  unreliable, add a UPS or supercapacitor HAT.
- To keep system logs in RAM only, set `Storage=volatile` in `deploy/systemd/journald.conf`.
  You lose the logs across reboots.
