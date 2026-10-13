# MQTT Messages

| Topic | Payload |
|---|---|
| `<base>/<station>/<source>` | `{"station", "source", "ts", "ts_quality", "values": {...}}` |
| `<base>/<station>/status` | `online` / `offline` (retained; `offline` is also the last-will) |
| `<base>/<station>/health` | uptime, outbox backlog, queue depth, task restarts, clock sync, load, memory, disk |

`ts` is the UTC capture time, not the send time, so buffered data keeps its true
timestamp. Delivery is **at-least-once** (QoS 1), which means a reading can arrive twice
around a reconnect. Consumers should de-duplicate on `(station, source, ts)`.

`ts_quality` says how far `ts` can be trusted (see [Timestamps Without a Real-Time
Clock](timekeeping.md)):

| `ts_quality` | Meaning | Accuracy |
|---|---|---|
| `ntp` | Captured while the clock was NTP-synchronised | NTP accuracy |
| `corrected` | Captured before sync and back-dated once sync was reached | Within seconds over a day offline (crystal drift) |
| `estimated` | Could not be corrected; `ts` is the unsynchronised system clock | Unreliable. The payload also carries `boot_id` and `boot_ts` |
