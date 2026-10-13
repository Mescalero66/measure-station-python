"""Publisher task: drains the outbox to the MQTT broker, reconnecting as needed."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

import aiomqtt

from measure_station.backoff import ExponentialBackoff
from measure_station.storage import Outbox, StoredMessage
from measure_station.supervisor import Heartbeat

log = logging.getLogger(__name__)

STATUS_ONLINE = "online"
STATUS_OFFLINE = "offline"


@dataclass(frozen=True, slots=True)
class MqttSettings:
    host: str
    port: int
    client_id: str
    status_topic: str
    username: str | None = None
    password: str | None = None
    keepalive_s: int = 30
    qos: int = 1
    tls_enabled: bool = False
    tls_ca_certs: str | None = None
    tls_certfile: str | None = None
    tls_keyfile: str | None = None
    publish_timeout_s: float = 10.0
    batch_size: int = 50
    idle_poll_s: float = 5.0
    reconnect_min_s: float = 1.0
    reconnect_max_s: float = 60.0


class MqttPublisher:
    """Publishes outbox messages, deleting each only after the broker acknowledges it.

    Connection loss is an expected condition here, not a crash: it is handled inside
    :meth:`run` with its own reconnect backoff, while messages keep accumulating in the
    outbox until the link returns.
    """

    def __init__(self, settings: MqttSettings, outbox: Outbox, new_data: asyncio.Event) -> None:
        self._settings = settings
        self._outbox = outbox
        self._new_data = new_data

    async def run(self, *, heartbeat: Heartbeat) -> None:
        s = self._settings
        backoff = ExponentialBackoff(s.reconnect_min_s, s.reconnect_max_s)
        while True:
            heartbeat.beat()
            try:
                async with self._make_client() as client:
                    log.info("Connected to MQTT broker %s:%d", s.host, s.port)
                    backoff.reset()
                    await self._publish(client, s.status_topic, STATUS_ONLINE, retain=True)
                    try:
                        await self._drain(client, heartbeat)
                    except asyncio.CancelledError:
                        await self._announce_offline(client)
                        raise
            except (aiomqtt.MqttError, TimeoutError, OSError) as exc:
                log.warning("MQTT link down: %s", exc or type(exc).__name__)

            delay = backoff.next_delay()
            log.info("Reconnecting to MQTT broker in %.1f s", delay)
            heartbeat.beat()
            await asyncio.sleep(delay)

    async def _drain(self, client: aiomqtt.Client, heartbeat: Heartbeat) -> None:
        s = self._settings
        while True:
            heartbeat.beat()
            # Clear *before* fetching so a write that lands after the fetch still wakes us.
            self._new_data.clear()
            batch = await asyncio.to_thread(self._outbox.fetch_batch, s.batch_size)
            if not batch:
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(s.idle_poll_s):
                        await self._new_data.wait()
                continue
            await self._publish_batch(client, batch)

    async def _publish_batch(self, client: aiomqtt.Client, batch: list[StoredMessage]) -> None:
        results = await asyncio.gather(
            *(self._publish(client, m.topic, m.payload) for m in batch),
            return_exceptions=True,
        )
        delivered = [m.id for m, r in zip(batch, results, strict=True) if r is None]
        if delivered:
            await asyncio.to_thread(self._outbox.delete, delivered)
        failures = [r for r in results if isinstance(r, BaseException)]
        if failures:
            log.warning(
                "%d of %d message(s) not acknowledged; they remain queued",
                len(failures),
                len(batch),
            )
            raise failures[0]
        log.debug("Published %d message(s)", len(batch))

    async def _publish(
        self, client: aiomqtt.Client, topic: str, payload: str, *, retain: bool = False
    ) -> None:
        # With QoS >= 1, aiomqtt's publish() completes only once the broker has acked.
        async with asyncio.timeout(self._settings.publish_timeout_s):
            await client.publish(topic, payload, qos=self._settings.qos, retain=retain)

    async def _announce_offline(self, client: aiomqtt.Client) -> None:
        """Best effort on clean shutdown (the last-will only fires on *unclean* disconnects)."""
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                client.publish(self._settings.status_topic, STATUS_OFFLINE, qos=1, retain=True),
                timeout=2,
            )

    def _make_client(self) -> aiomqtt.Client:
        s = self._settings
        tls_params = (
            aiomqtt.TLSParameters(
                ca_certs=s.tls_ca_certs, certfile=s.tls_certfile, keyfile=s.tls_keyfile
            )
            if s.tls_enabled
            else None
        )
        client = aiomqtt.Client(
            hostname=s.host,
            port=s.port,
            username=s.username,
            password=s.password,
            identifier=s.client_id,
            keepalive=s.keepalive_s,
            timeout=s.publish_timeout_s,
            tls_params=tls_params,
            will=aiomqtt.Will(s.status_topic, STATUS_OFFLINE, qos=1, retain=True),
            # Let a whole batch be in flight at once (paho defaults to 20).
            max_inflight_messages=s.batch_size,
        )
        # A full batch of pending publishes is by design, not a warning sign.
        client.pending_calls_threshold = s.batch_size + 1
        return client
