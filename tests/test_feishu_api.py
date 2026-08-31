"""Tests for Feishu API helpers: token cache, sanitize, send."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from feishu_server import feishu_api


class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


@pytest.fixture(autouse=True)
def _fresh_token_cache(monkeypatch):
    monkeypatch.setattr(feishu_api, "_token_cache",
                        {"token": None, "expires_at": 0.0})


def test_sanitize_text_strips_wecom_markdown():
    src = '**加粗** <font color="info">绿色</font> > 引用'
    assert feishu_api.sanitize_text(src) == "加粗 绿色 > 引用"


def test_token_cached_across_calls(monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(url)
        return _Resp({"code": 0, "app_access_token": "tk", "expire": 7200})
    monkeypatch.setattr(feishu_api.requests, "post", fake_post)
    config = {"app_id": "a", "app_secret": "s"}
    assert feishu_api.get_app_access_token(config) == "tk"
    assert feishu_api.get_app_access_token(config) == "tk"
    assert len(calls) == 1


def test_send_text_markdown_goes_card(monkeypatch):
    monkeypatch.setattr(feishu_api, "_token_cache",
                        {"token": "tk", "expires_at": time.time() + 3600})
    bodies = []

    def fake_post(url, params=None, headers=None, json=None, timeout=None):
        bodies.append(json)
        return _Resp({"code": 0})
    monkeypatch.setattr(feishu_api.requests, "post", fake_post)
    assert feishu_api.send_text("ou_1", '**hi** [看](url)',
                                {"app_id": "a", "app_secret": "s"}) is True
    assert bodies[0]["msg_type"] == "interactive"
    card = json.loads(bodies[0]["content"])
    assert card["elements"][0]["tag"] == "markdown"
    assert card["elements"][0]["content"] == '**hi** [看](url)'


def test_send_text_font_only_goes_card_stripped(monkeypatch):
    monkeypatch.setattr(feishu_api, "_token_cache",
                        {"token": "tk", "expires_at": time.time() + 3600})
    bodies = []

    def fake_post(url, params=None, headers=None, json=None, timeout=None):
        bodies.append(json)
        return _Resp({"code": 0})
    monkeypatch.setattr(feishu_api.requests, "post", fake_post)
    assert feishu_api.send_text("ou_1", '<font color="info">绿</font>字',
                                {"app_id": "a", "app_secret": "s"}) is True
    assert bodies[0]["msg_type"] == "interactive"
    card = json.loads(bodies[0]["content"])
    assert card["elements"][0]["content"] == "绿字"


def test_send_text_plain_stays_text(monkeypatch):
    monkeypatch.setattr(feishu_api, "_token_cache",
                        {"token": "tk", "expires_at": time.time() + 3600})
    bodies = []

    def fake_post(url, params=None, headers=None, json=None, timeout=None):
        bodies.append(json)
        return _Resp({"code": 0})
    monkeypatch.setattr(feishu_api.requests, "post", fake_post)
    assert feishu_api.send_text("ou_1", "纯文本内容",
                                {"app_id": "a", "app_secret": "s"}) is True
    assert bodies[0]["msg_type"] == "text"
    assert json.loads(bodies[0]["content"]) == {"text": "纯文本内容"}


def test_send_text_failure_returns_false(monkeypatch):
    monkeypatch.setattr(feishu_api, "_token_cache",
                        {"token": "tk", "expires_at": time.time() + 3600})
    monkeypatch.setattr(feishu_api.requests, "post",
                        lambda url, **kw: _Resp({"code": 99991663,
                                                 "msg": "bad token"}))
    assert feishu_api.send_text("ou_1", "x",
                                {"app_id": "a", "app_secret": "s"}) is False
