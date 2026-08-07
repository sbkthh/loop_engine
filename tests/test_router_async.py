"""Tests for router async dispatch (all messages go through LLM)."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wecom_server.router import dispatch


def test_dispatch_always_returns_callable():
    """All messages return callable (async LLM path)."""
    assert callable(dispatch("查状态", [], "/tmp"))
    assert callable(dispatch("批准", [], "/tmp"))
    assert callable(dispatch("随便说点什么", [], "/tmp"))
    assert callable(dispatch("", [], "/tmp"))