"""Tests for shared WeCom-flavoured markdown helpers."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wecom_server.wecom_api import (
    sanitize_markdown, split_segments, md_bold, md_color,
)


def test_sanitize_markdown_removes_tables_and_code_blocks():
    raw = (
        "# 标题\n"
        "| 模块 | 状态 |\n"
        "| --- | --- |\n"
        "| A | SYNCED |\n"
        "```\n"
        "code line\n"
        "```\n"
        "**加粗**\n"
    )
    cleaned = sanitize_markdown(raw)
    assert "```" not in cleaned
    assert "| --- | --- |" not in cleaned
    assert "模块 | 状态" in cleaned
    assert "A | SYNCED" in cleaned
    assert "    code line" in cleaned
    assert "**加粗**" in cleaned


def test_split_segments_char_boundary_no_cjk_cut():
    content = "字" * 10000  # 30000 bytes
    segments = split_segments(content)
    assert len(segments) > 1
    for seg in segments:
        assert len(seg.encode("utf-8")) <= 1800
        assert all("\u4e00" <= c <= "\u9fff" for c in seg)  # no partial chars
    assert "".join(segments) == content


def test_split_segments_single_when_short():
    assert split_segments("hello") == ["hello"]
    assert split_segments("") == []


def test_md_helpers_wrap_wecom_markdown():
    assert md_bold("req-a") == "**req-a**"
    assert md_color("执行完成（成功）", "info") == \
        '<font color="info">执行完成（成功）</font>'
    assert md_color("执行暂停") == '<font color="comment">执行暂停</font>'


def test_sanitize_keeps_font_color_tags():
    raw = ('[调度] **req-a** <font color="warning">执行失败</font>'
           '：detail')
    cleaned = sanitize_markdown(raw)
    assert cleaned == raw
