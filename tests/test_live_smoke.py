"""Opt-in live connectivity smoke: real WeCom WebSocket, no mocks.

    LOOP_ENGINE_LIVE_SMOKE=1 python3 -m pytest -s tests/test_live_smoke.py

Reads the real bot credentials from ~/.qoder/loop_engine/aibot.json and must
receive errcode 0 for ``aibot_subscribe``. Skipped by default (and in CI).

Stop the aibot daemon first: one bot id allows a single active long
connection, so this probe would kick the daemon offline. The test refuses to
run while the daemon's control port is answering.
"""
import asyncio
import json
import os
import socket

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("LOOP_ENGINE_LIVE_SMOKE") != "1",
    reason="opt-in: set LOOP_ENGINE_LIVE_SMOKE=1")


def _daemon_responding(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def test_aibot_subscribe_live():
    from aibot_server.aibot_api import WS_URL, subscribe_frame

    try:
        import websockets
    except ImportError:
        pytest.skip("websockets not installed")
    path = os.path.expanduser("~/.qoder/loop_engine/aibot.json")
    if not os.path.exists(path):
        pytest.skip("aibot.json not configured")
    with open(path) as f:
        cfg = json.load(f)
    port = int(cfg.get("port", 5099))
    if _daemon_responding(port):
        pytest.fail("aibot daemon is running; stop it before the live smoke "
                    "(one bot id allows a single active connection)")

    async def probe():
        async with websockets.connect(WS_URL, open_timeout=10) as ws:
            await ws.send(json.dumps(
                subscribe_frame(cfg["bot_id"], cfg["secret"])))
            while True:
                frame = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if frame.get("errcode") is not None:
                    return frame

    frame = asyncio.run(probe())
    assert frame.get("errcode") == 0, frame
