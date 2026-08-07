"""Tests for WeCom API module."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch, MagicMock

from wecom_server.wecom_api import get_access_token, send_text

CONFIG = {"corp_id": "test_corp", "secret": "test_secret", "agent_id": "1000002"}


def test_get_access_token_caches():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"access_token": "tok_abc", "expires_in": 7200, "errcode": 0}
    with patch("wecom_server.wecom_api.requests.get", return_value=mock_resp) as mock_get:
        t1 = get_access_token(CONFIG)
        t2 = get_access_token(CONFIG)
        assert t1 == "tok_abc"
        assert t2 == "tok_abc"
        assert mock_get.call_count == 1


def test_get_access_token_refreshes_when_expired():
    import wecom_server.wecom_api as api
    api._token_cache = {"token": "old_tok", "expires_at": 0.0}
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"access_token": "new_tok", "expires_in": 7200, "errcode": 0}
    with patch("wecom_server.wecom_api.requests.get", return_value=mock_resp):
        token = get_access_token(CONFIG)
        assert token == "new_tok"


def test_send_text_success():
    mock_token_resp = MagicMock()
    mock_token_resp.json.return_value = {"access_token": "tok", "expires_in": 7200, "errcode": 0}
    mock_send_resp = MagicMock()
    mock_send_resp.json.return_value = {"errcode": 0, "errmsg": "ok"}
    with patch("wecom_server.wecom_api.requests.get", return_value=mock_token_resp), \
         patch("wecom_server.wecom_api.requests.post", return_value=mock_send_resp) as mock_post:
        ok = send_text("user123", "hello", CONFIG)
        assert ok is True
        body = mock_post.call_args[1]["json"]
        assert body["touser"] == "user123"
        assert body["text"]["content"] == "hello"
        assert body["agentid"] == 1000002


def test_send_text_failure():
    mock_token_resp = MagicMock()
    mock_token_resp.json.return_value = {"access_token": "tok", "expires_in": 7200, "errcode": 0}
    mock_send_resp = MagicMock()
    mock_send_resp.json.return_value = {"errcode": 40014, "errmsg": "invalid token"}
    with patch("wecom_server.wecom_api.requests.get", return_value=mock_token_resp), \
         patch("wecom_server.wecom_api.requests.post", return_value=mock_send_resp):
        ok = send_text("user123", "hello", CONFIG)
        assert ok is False
