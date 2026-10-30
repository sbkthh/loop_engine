"""Pure protocol helpers for the WeCom aibot long connection."""
from aibot_server import aibot_api as api


def test_subscribe_frame():
    frame = api.subscribe_frame("bot-1", "sec")
    assert frame["cmd"] == "aibot_subscribe"
    assert frame["headers"]["req_id"].startswith("aibot_subscribe_")
    assert frame["body"] == {"bot_id": "bot-1", "secret": "sec"}


def test_ping_frame():
    frame = api.ping_frame()
    assert frame["cmd"] == "ping"
    assert frame["headers"]["req_id"].startswith("ping_")


def test_respond_frame_echoes_callback_req_id():
    frame = api.respond_frame("req-9", "ack_1", "处理中")
    assert frame["cmd"] == "aibot_respond_msg"
    assert frame["headers"]["req_id"] == "req-9"
    stream = frame["body"]["stream"]
    assert stream == {"id": "ack_1", "content": "处理中", "finish": True}


def test_send_msg_frame():
    frame = api.send_msg_frame("wr123", "**done**")
    assert frame["cmd"] == "aibot_send_msg"
    assert frame["body"]["chatid"] == "wr123"
    assert frame["body"]["msgtype"] == "markdown"
    assert frame["body"]["markdown"]["content"] == "**done**"


def test_split_never_exceeds_limit_or_breaks_cjk():
    content = "中" * 2000
    segments = api.split(content)
    assert len(segments) > 1
    assert all(len(s.encode("utf-8")) <= api.MAX_BYTES for s in segments)
    assert "".join(segments) == content


def test_extract_text_text():
    assert api.extract_text(
        {"msgtype": "text", "text": {"content": " 你好 "}}) == "你好"


def test_extract_text_mixed_joins_text_items():
    body = {
        "msgtype": "mixed",
        "mixed": {"msg_item": [
            {"type": "text", "text": {"content": "第一行"}},
            {"type": "image", "image": {"url": "u"}},
            {"type": "text", "text": {"content": "第二行"}},
        ]},
    }
    assert api.extract_text(body) == "第一行\n第二行"


def test_extract_text_unsupported():
    assert api.extract_text({"msgtype": "image"}) == ""
    assert api.extract_text({}) == ""
    assert api.extract_text(None) == ""


def test_strip_mentions():
    assert api.strip_mentions("@机器人\u2005查看状态") == "查看状态"
    assert api.strip_mentions("@机器人 查看状态") == "查看状态"
    assert api.strip_mentions("@机器人\u2005@机器人\u2005查看状态") == "查看状态"
    assert api.strip_mentions("@机器人\u2005") == ""
    assert api.strip_mentions("正文里的@符号保留") == "正文里的@符号保留"
