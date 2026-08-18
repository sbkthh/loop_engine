"""Tests for WeCom server per-requirement serial queues."""
import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wecom_server import server


def _queued_fn(name, gate=None, order=None):
    """A task that records start/end; optionally blocks on gate."""
    def fn():
        if order is not None:
            order.append(name + ":start")
        if gate is not None:
            gate.wait(timeout=2)
        if order is not None:
            order.append(name + ":end")
        return name
    fn.requirement = name
    return fn


def test_submit_async_bounded_per_requirement(monkeypatch):
    """Full queue gets an immediate reply; accepted messages stay queued."""
    q = queue.Queue(maxsize=2)
    monkeypatch.setattr(server, "_queue_for", lambda req: q)
    calls = []

    def fn():
        calls.append("ran")
        return "ok"

    assert server._submit_async(fn, "u", {}) is None
    assert server._submit_async(fn, "u", {}) is None
    reply = server._submit_async(fn, "u", {})
    assert "上一条还在处理中" in reply
    assert calls == []  # no consumer running: nothing executed yet


def test_same_requirement_serial_across_requirements_parallel(monkeypatch):
    """Same requirement executes strictly in order; a different requirement
    runs concurrently instead of waiting behind it."""
    monkeypatch.setattr("wecom_server.wecom_api.send_text", lambda *a, **k: True)
    order = []
    gate = threading.Event()

    threads = []
    for req, count in (("reqA", 2), ("reqB", 1)):
        q = queue.Queue(maxsize=3)
        monkeypatch.setattr(server, "_queue_for", lambda req=req, q=q: q)
        t = threading.Thread(target=server._drain_pending, args=(q,), daemon=True)
        t.start()
        threads.append(t)
        for i in range(count):
            fn = _queued_fn(f"{req}-{i}", gate=gate if (req, i) == ("reqA", 0) else None,
                            order=order)
            assert server._submit_async(fn, "u", {}) is None

    time.sleep(0.2)
    # reqA-0 is running; reqA-1 must wait behind it, but reqB-0 runs now
    assert "reqA-0:start" in order
    assert "reqA-1:start" not in order
    assert "reqB-0:start" in order

    gate.set()
    time.sleep(0.3)
    i_a0s = order.index("reqA-0:start")
    i_a0e = order.index("reqA-0:end")
    i_a1s = order.index("reqA-1:start")
    i_b0s = order.index("reqB-0:start")
    assert i_a1s > i_a0e  # same requirement: serial, never overlaps
    assert i_b0s < i_a0e  # other requirement ran before reqA finished
