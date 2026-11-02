"""WeCom-flavoured markdown helpers shared by all chat transports.

The self-built-app callback transport (access_token + message/send) is gone;
aibot and Feishu still reuse the formatting/splitting rules defined here.
"""
import re

_MAX_BYTES = 1800  # keep well under WeCom's 2048-byte markdown limit


def sanitize_markdown(content):
    """Convert standard markdown to WeCom's supported subset (#, **, >, <font>).

    Tables collapse to pipe-joined text rows; fenced code blocks become indented text.
    """
    lines = []
    in_code = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            lines.append("    " + line)
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                continue  # table separator row
            lines.append(" | ".join(cells))
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def split_segments(content, max_bytes=_MAX_BYTES):
    """Split content into <=max_bytes segments on character boundaries (never mid-CJK)."""
    segments = []
    current = ""
    for ch in content:
        trial = current + ch
        if len(trial.encode("utf-8")) > max_bytes:
            segments.append(current)
            current = ch
        else:
            current = trial
    if current:
        segments.append(current)
    return segments


def md_bold(text):
    """WeCom markdown bold."""
    return f"**{text}**"


def md_color(text, color="comment"):
    """WeCom markdown font color: info (green), comment (gray), warning (orange-red)."""
    return f'<font color="{color}">{text}</font>'
