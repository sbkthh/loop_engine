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


def test_split_strips_font_tags():
    segments = api.split('<font color="info">成功</font> 与 <font color="warning">失败</font>')
    assert segments == ["成功 与 失败"]


def test_extract_media_image_and_file():
    assert api.extract_media({
        "msgtype": "image",
        "image": {"url": "https://x/a.png", "aeskey": "k"},
    }) == [{"kind": "image", "url": "https://x/a.png", "aeskey": "k"}]
    assert api.extract_media({
        "msgtype": "file",
        "file": {"url": "https://x/a.pdf"},
    }) == [{"kind": "file", "url": "https://x/a.pdf", "aeskey": ""}]


def test_extract_media_mixed_images_only():
    body = {"msgtype": "mixed", "mixed": {"msg_item": [
        {"msgtype": "text", "text": {"content": "说明"}},
        {"msgtype": "image", "image": {"url": "https://x/a.png"}},
        {"msgtype": "image", "image": {}},
    ]}}
    assert api.extract_media(body) == [
        {"kind": "image", "url": "https://x/a.png", "aeskey": ""}]


def test_extract_media_reads_aeskey_from_url_query():
    body = {"msgtype": "image", "image": {
        "url": "https://x/a.png?aeskey=abc%2Bdef%3D"}}
    assert api.extract_media(body) == [
        {"kind": "image", "url": "https://x/a.png?aeskey=abc%2Bdef%3D",
         "aeskey": "abc+def="}]


def test_extract_media_unsupported_or_absent():
    assert api.extract_media({"msgtype": "text"}) == []
    assert api.extract_media({"msgtype": "voice"}) == []
    assert api.extract_media(None) == []


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


def test_extract_text_mixed_accepts_official_msgtype_key():
    body = {"msgtype": "mixed", "mixed": {"msg_item": [
        {"msgtype": "text", "text": {"content": "官方字段"}},
        {"msgtype": "image", "image": {"url": "u"}},
    ]}}
    assert api.extract_text(body) == "官方字段"


def test_extract_quote_text():
    body = {"quote": {"msgtype": "text", "text": {"content": "上一句"}}}
    assert api.extract_quote(body) == "上一句"


def test_extract_quote_mixed():
    body = {"quote": {"msgtype": "mixed", "mixed": {"msg_item": [
        {"msgtype": "text", "text": {"content": "引用一"}},
        {"msgtype": "text", "text": {"content": "引用二"}},
    ]}}}
    assert api.extract_quote(body) == "引用一\n引用二"


def test_extract_quote_voice_image_file():
    assert api.extract_quote(
        {"quote": {"msgtype": "voice", "voice": {"content": "语音转写"}}}
    ) == "语音转写"
    assert api.extract_quote({"quote": {"msgtype": "image"}}) == "[引用图片]"
    assert api.extract_quote(
        {"quote": {"msgtype": "file", "file": {"name": "a.pdf"}}}
    ) == "[引用文件: a.pdf]"
    assert api.extract_quote({"quote": {"msgtype": "file"}}) == "[引用文件]"


def test_extract_quote_absent():
    assert api.extract_quote({}) == ""
    assert api.extract_quote(None) == ""


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
