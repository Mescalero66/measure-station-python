# Broker and Consumer Requirements

This document is for whoever runs the MQTT broker and the system that stores or processes
station data. The station's guarantee ends at the broker, so whether data actually reaches
storage depends on how the broker and consumer are configured.

## What the Station Guarantees

The station keeps every reading in its outbox until the broker sends a **PUBACK** (QoS 1).
A PUBACK confirms only that *the broker* received the message, not that any subscriber
did. MQTT has no end-to-end acknowledgement. Once PUBACK arrives the station deletes its
copy, so from then on the broker is responsible for the message. A broker can acknowledge
a message and still lose it if:

- no subscriber is connected and none has a persistent session, so the broker discards
  the message; or
- the broker restarts before writing the message to disk.

## Broker

- **Enable persistence**, and save often enough that a broker crash loses little.
  Mosquitto's default `autosave_interval` is 1800 s, so it can lose 30 minutes of data.
- **Raise the per-client queue limit.** It must be able to hold a whole station backlog
  while the consumer is offline. Mosquitto's default `max_queued_messages` is 1000, and
  messages beyond that are dropped.
- **Grant each station publish rights** (including retained messages) on
  `<base>/<station>/#`.
- **Give the broker host the same NTP source as the stations**, so its clock agrees with
  their `ts` (e.g. for "no health message for N minutes" checks). See
  [Using a Local NTP Server](timekeeping.md#using-a-local-ntp-server).

Example Mosquitto settings:

```conf
persistence true
persistence_location /var/lib/mosquitto/
autosave_interval 60
max_queued_messages 0        # unlimited; or size it to your longest expected consumer outage
max_queued_bytes 0
```

Other brokers have equivalents. Check limits on session expiry and queued messages,
especially on managed cloud brokers.

## Consumer (Storage or Processing)

- **Use a persistent session**, so the broker queues messages while the consumer is down.
  Give it a fixed client ID, and connect with `clean_session=False` (MQTT 3.1.1), or with
  `clean_start=False` and a non-zero session expiry interval (MQTT 5).
- **Subscribe at QoS 1.** A message is delivered at the lower of the publish and
  subscription QoS. A QoS 0 subscription downgrades delivery to at-most-once, and brokers
  generally don't queue QoS 0 messages for offline clients.
- **Acknowledge only after the data is stored.** If the consumer acknowledges first and
  then crashes before writing, the message is lost. With paho-mqtt 2.x, use
  `manual_ack=True` and call `client.ack(mid, qos)` after the write commits.
- **De-duplicate on `(station, source, ts)`**, for example with a unique constraint.
  Delivery is at-least-once, so duplicates occur around reconnects.
- **Order by `ts`, not arrival order.** The station sends messages in concurrent batches
  and resends after reconnects, so arrival order isn't guaranteed.
- **Expect late data.** After an outage, readings arrive hours or days late with their
  original `ts`. Aggregations and alerts must handle data arriving after its time window.
  Readings taken before the station's clock synced are deliberately held until they can
  be corrected, so they can arrive late even when the network was up.
- **Check `ts_src`.** Treat `estimated` timestamps as approximate: flag them, or
  exclude them from time-sensitive analysis.
- **Check the quality flags in `meta`** before trusting `values`, e.g. the air quality
  board's `validity` during warm-up. Each sensor's page lists its flags.

## Monitoring

- `<base>/<station>/status` is retained. It changes to `offline` on a clean stop, or via
  the last-will when the connection drops.
- On `<base>/<station>/health`, a rising `outbox_backlog` means data is waiting to be sent.
  If no health message arrives for several `HEALTH_INTERVAL_S` periods, the station is
  unreachable or stalled.

## End-to-End Acknowledgement (Not Implemented)

If the station must keep data until the *storage system* confirms it, the storage system
would publish acknowledgements to a topic the station subscribes to, and the station would
delete outbox rows only on that acknowledgement. Getting the broker and consumer settings
above right covers most of the same risk with configuration alone.
