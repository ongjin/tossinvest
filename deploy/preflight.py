"""Checks a pytossinvest-mcp container from the inside, over its HTTP endpoint.

Run with `docker exec -i <container> python - < deploy/preflight.py`. It needs no network
beyond the container itself and never calls the brokerage API, so it works in a
`--network none` container with a dummy bearer as well as in the real one. It verifies that

- the server answers,
- a request without the bearer, or with a wrong one, is refused,
- MCP initialize succeeds,
- tools are registered, and in read_only mode none of them can place, change or cancel orders.

Nothing secret is printed.
"""
import json
import os
import sys
import time

import httpx

URL = f"http://127.0.0.1:{os.environ.get('TOSSINVEST_HTTP_PORT', '8000')}/mcp"
TOKEN = os.environ["TOSSINVEST_AUTH_TOKEN"]
MODE = os.environ.get("TOSSINVEST_MODE", "paper")
READ_ONLY = "read_only"

STARTUP_TRIES = 100
STARTUP_PAUSE_SEC = 0.2
REQUEST_TIMEOUT_SEC = 10

BASE_HEADERS = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
WRITE_TOOLS = {
    "get_order_readiness", "preview_order", "place_order", "preview_modify", "modify_order",
    "cancel_order", "preview_conditional_order", "preview_conditional_modify",
    "cancel_conditional_order",
}
INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "preflight", "version": "0"}},
}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


def post(headers: dict, payload: dict) -> httpx.Response:
    return httpx.post(URL, headers={**BASE_HEADERS, **headers}, json=payload,
                      timeout=REQUEST_TIMEOUT_SEC)


def message_of(resp: httpx.Response) -> dict:
    """The JSON-RPC message of a response that is either plain JSON or a single SSE event."""
    if "text/event-stream" not in resp.headers.get("content-type", ""):
        return resp.json()
    data = [line[len("data:"):].strip() for line in resp.text.splitlines() if line.startswith("data:")]
    return json.loads(data[-1])


def wait_until_up() -> None:
    for _ in range(STARTUP_TRIES):
        try:
            post({}, INITIALIZE)
            return
        except httpx.TransportError:
            time.sleep(STARTUP_PAUSE_SEC)
    sys.exit("FAIL: the server did not come up")


def main() -> None:
    wait_until_up()
    failures = []

    refusals = (post({}, INITIALIZE).status_code,
                post({"authorization": "Bearer not-the-token"}, INITIALIZE).status_code)
    print(f"without bearer -> {refusals[0]}, wrong bearer -> {refusals[1]}")
    if refusals != (401, 401):
        failures.append("unauthenticated requests were not refused with 401")

    auth = {"authorization": f"Bearer {TOKEN}"}
    init = post(auth, INITIALIZE)
    if init.status_code != 200:
        sys.exit(f"FAIL: initialize answered {init.status_code}")
    info = message_of(init)["result"]
    server, protocol = info["serverInfo"], info["protocolVersion"]
    print(f"{server['name']} {server['version']}, protocol {protocol}, mode {MODE}")

    listed = post({**auth, "mcp-protocol-version": protocol}, LIST_TOOLS)
    names = sorted(tool["name"] for tool in message_of(listed)["result"]["tools"])
    print(f"{len(names)} tools: {', '.join(names)}")
    if not names:
        failures.append("no tools are registered")
    order_tools = WRITE_TOOLS & set(names)
    if MODE == READ_ONLY and order_tools:
        failures.append(f"order tools registered in read_only: {sorted(order_tools)}")

    if failures:
        sys.exit("FAIL: " + "; ".join(failures))
    print("PREFLIGHT OK")


main()
