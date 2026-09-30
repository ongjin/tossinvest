import asyncio
import json

import httpx
import pytest
import respx

websockets = pytest.importorskip("websockets")
from websockets.asyncio.server import serve  # noqa: E402

from pytossinvest.client import TossInvestClient  # noqa: E402
from pytossinvest.stream import TossInvestStream  # noqa: E402

TRADE = [{"type": "trade:kr", "codes": ["005930"]}]


class _TokenOnly:
    def access_token(self):
        return "tok"


def test_stream_authenticates_subscribes_keeps_alive_and_yields_frames():
    seen = {"auth": None, "subs": None, "pings": 0}

    async def handler(ws):
        seen["auth"] = ws.request.headers["Authorization"]
        async for raw in ws:
            if raw == "PING":
                seen["pings"] += 1
                await ws.send(json.dumps({"type": "pong"}))
                continue
            seen["subs"] = json.loads(raw)
            await ws.send(json.dumps({"type": "subscriptions", "subscribed": ["trade:kr:005930"],
                                      "rejected": []}))
            await ws.send(json.dumps({"type": "message", "topic": "trade:kr:005930",
                                      "data": {"price": "70000"}}))

    async def run():
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            stream = TossInvestStream(_TokenOnly(), url=f"ws://127.0.0.1:{port}", ping_interval=0.01)
            async with stream:
                await asyncio.sleep(0.05)  # several keepalive PINGs go out before subscribing
                await stream.subscribe(TRADE)
                frames = []
                async for frame in stream:
                    frames.append(frame)
                    if frame["type"] == "message":
                        break
        return frames

    frames = asyncio.run(run())

    assert seen["auth"] == "Bearer tok"
    assert seen["subs"] == TRADE
    assert seen["pings"] >= 1
    assert [f["type"] for f in frames] == ["subscriptions", "message"]  # pongs are filtered out


@respx.mock
def test_client_exposes_its_access_token():
    base = "https://openapi.example.test"
    respx.post(f"{base}/oauth2/token").mock(return_value=httpx.Response(
        200, json={"access_token": "tok-1", "token_type": "Bearer", "expires_in": 3600}))
    assert TossInvestClient("cid", "secret", base_url=base).access_token() == "tok-1"
