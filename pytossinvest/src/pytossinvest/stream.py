"""Realtime WebSocket stream: trades, orderbooks and your own order events.

Optional dependency: install the ``ws`` extra (``pip install "pytossinvest[ws]"``).
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

WS_URL = "wss://openapi-ws.tossinvest.com/ws/v1"

# The server drops a connection that has *received* nothing for 180s (its own pushes don't
# count) and answers a plain-text PING with {"type": "pong"}.
PING_INTERVAL_SEC = 60
PING = "PING"
PONG_TYPE = "pong"


class TossInvestStream:
    """Async client for the realtime stream. Subscriptions are declarative: every
    ``subscribe()`` call replaces the whole set, and ``[]`` unsubscribes everything.

        async with TossInvestStream(client) as stream:
            await stream.subscribe([{"type": "trade:kr", "codes": ["005930"]}])
            async for frame in stream:
                ...  # {"type": "message", "topic": "trade:kr:005930", "data": {...}}

    Frame types are ``subscriptions`` (ack with ``subscribed``/``rejected``), ``message`` and
    ``error``. ``personal:order`` codes are account sequence strings (``Account.account_seq``);
    that channel is lossless only within one connection, so resync with ``list_orders`` after a
    reconnect. Market data frames may be dropped under backpressure.
    """

    def __init__(self, client: Any, *, url: str = WS_URL,
                 ping_interval: float = PING_INTERVAL_SEC) -> None:
        self._client = client  # anything with access_token(), normally a TossInvestClient
        self._url = url
        self._ping_interval = ping_interval
        self._ws: Any = None
        self._pinger: asyncio.Task | None = None

    async def __aenter__(self) -> "TossInvestStream":
        from websockets.asyncio.client import connect  # optional dependency ([ws] extra)

        token = await asyncio.to_thread(self._client.access_token)
        self._ws = await connect(self._url, additional_headers={"Authorization": f"Bearer {token}"})
        self._pinger = asyncio.create_task(self._keepalive())
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._pinger.cancel()
        await self._ws.close()

    async def subscribe(self, subscriptions: list[dict]) -> None:
        """Send the full subscription set. Items are {"type": ..., "codes": [...]}; an item
        {"id": "..."} is echoed back in the ack."""
        await self._ws.send(json.dumps(subscriptions))

    async def __aiter__(self) -> AsyncIterator[dict]:
        async for raw in self._ws:
            frame = json.loads(raw)
            if frame.get("type") == PONG_TYPE:
                continue
            yield frame

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(self._ping_interval)
            await self._ws.send(PING)
