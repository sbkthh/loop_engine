"""aibot media: decrypt, filename, extraction and prompt fragments."""
import base64
import os
import time
import zipfile

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from aibot_server import media


def _key():
    return bytes(range(32))


def _encrypt(plain, key=None):
    key = key or _key()
    pad = 32 - len(plain) % 32
    padded = plain + bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return enc.update(padded) + enc.finalize()


def _aeskey(key=None):
    return base64.b64encode(key or _key()).decode()


def test_decrypt_roundtrip_cjk_and_binary():
    plain = "执行结果 ✅".encode() + bytes(range(20))
    assert media.decrypt(_encrypt(plain), _aeskey()) == plain


def test_decrypt_rejects_bad_key_and_padding():
    with pytest.raises(ValueError, match="长度"):
        media.decrypt(_encrypt(b"x"), base64.b64encode(b"short").decode())
    with pytest.raises(ValueError, match="填充"):
        media.decrypt(b"\x00" * 16, _aeskey())


def test_decrypt_accepts_urlsafe_unpadded_and_encoded_keys():
    plain = b"media-key-variants"
    key = bytes([0xFB, 0xFF] * 16)
    cipher = _encrypt(plain, key)

    std = _aeskey(key)
    assert "+" in std and "/" in std
    urlsafe = std.replace("+", "-").replace("/", "_")
    assert media.decrypt(cipher, urlsafe) == plain

    assert media.decrypt(cipher, std.rstrip("=")) == plain
    assert media.decrypt(cipher, std.replace("=", "%3D")) == plain
    assert media.decrypt(cipher, key.hex()) == plain


def test_decrypt_rejects_missing_key():
    with pytest.raises(ValueError, match="缺少"):
        media.decrypt(_encrypt(b"x"), "")


def test_filename_prefers_utf8_disposition_and_strips_paths():
    headers = {"Content-Disposition":
               "attachment; filename*=UTF-8''%E6%89%A7%E8%A1%8C.txt"}
    assert media.filename(headers, "https://x/a", "m1", "file", b"") == "执行.txt"

    headers = {"Content-Disposition": 'attachment; filename="../../a.txt"'}
    assert media.filename(headers, "https://x/a", "m1", "file", b"") == "a.txt"


def test_filename_repairs_latin1_mojibake():
    mojibake = "执行结果.txt".encode("utf-8").decode("latin-1")
    headers = {"Content-Disposition": f'filename="{mojibake}"'}
    assert media.filename(headers, "https://x/a", "m1", "file", b"") == "执行结果.txt"


def test_filename_falls_back_to_msgid_and_url_ext():
    assert media.filename({}, "https://x/report.pdf", "m1", "file", b"") == "m1.pdf"
    assert media.filename({}, "https://x/download", "m1", "file",
                          b"%PDF-1.4") == "m1.pdf"
    assert media.filename({}, "https://x/a", "m1", "image",
                          b"\x89PNG\r\n\x1a\n") == "m1.png"


def test_save_unique_names_and_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "MEDIA_DIR", str(tmp_path))
    old = tmp_path / "old.txt"
    old.write_text("old")
    old_time = time.time() - media.TTL - 10
    os.utime(old, (old_time, old_time))

    first = media.save(b"a", "a.txt", "m1")
    second = media.save(b"b", "a.txt", "m1")
    assert first != second
    assert not old.exists()


def test_prepare_media_image(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "MEDIA_DIR", str(tmp_path))
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    monkeypatch.setattr(
        media, "download",
        lambda url, max_bytes=media.MAX_BYTES:
        (_encrypt(png), {"Content-Disposition": 'filename="shot.png"'}))

    fragment = media.prepare_media(
        {"kind": "image", "url": "https://x/a", "aeskey": _aeskey()}, "m1")

    assert "[用户发送了图片]" in fragment
    assert "Read" in fragment
    path = fragment.split("图片已保存到：", 1)[1].splitlines()[0]
    assert os.path.exists(path)
    assert path.endswith(".png")


def test_prepare_media_text_and_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr(
        media, "download",
        lambda url, max_bytes=media.MAX_BYTES:
        (_encrypt(b"LOG-TOKEN"), {"Content-Disposition": 'filename="a.log"'}))

    fragment = media.prepare_media(
        {"kind": "file", "url": "https://x/a", "aeskey": _aeskey()}, "m1")
    assert "文本文件已保存到" in fragment
    assert "Read" in fragment

    monkeypatch.setattr(
        media, "download",
        lambda url, max_bytes=media.MAX_BYTES:
        (_encrypt(b"%PDF-1.4"), {"Content-Disposition": 'filename="a.pdf"'}))
    fragment = media.prepare_media(
        {"kind": "file", "url": "https://x/a", "aeskey": _aeskey()}, "m2")
    assert "PDF已保存到" in fragment


def _write_docx(path, token):
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/'
           'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
           f'{token}</w:t></w:r></w:p></w:body></w:document>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", doc)


def test_prepare_media_docx_extracts_text(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "MEDIA_DIR", str(tmp_path))
    src = tmp_path / "src.docx"
    _write_docx(src, "DOCX-TOKEN-456")
    data = src.read_bytes()
    monkeypatch.setattr(
        media, "download",
        lambda url, max_bytes=media.MAX_BYTES:
        (_encrypt(data), {"Content-Disposition": 'filename="需求.docx"'}))

    fragment = media.prepare_media(
        {"kind": "file", "url": "https://x/a", "aeskey": _aeskey()}, "m1")
    assert "DOCX-TOKEN-456" in fragment
    assert "已抽取文本内容" in fragment


def test_prepare_media_xlsx_extracts_text(tmp_path, monkeypatch):
    openpyxl = pytest.importorskip("openpyxl")
    monkeypatch.setattr(media, "MEDIA_DIR", str(tmp_path))
    src = tmp_path / "src.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1"] = "XLSX-TOKEN-123"
    wb.save(src)
    data = src.read_bytes()
    monkeypatch.setattr(
        media, "download",
        lambda url, max_bytes=media.MAX_BYTES:
        (_encrypt(data), {"Content-Disposition": 'filename="数据.xlsx"'}))

    fragment = media.prepare_media(
        {"kind": "file", "url": "https://x/a", "aeskey": _aeskey()}, "m1")
    assert "XLSX-TOKEN-123" in fragment


def test_prepare_media_unknown_binary_keeps_path(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr(
        media, "download",
        lambda url, max_bytes=media.MAX_BYTES:
        (_encrypt(b"\x00\x01\x02"), {"Content-Disposition": 'filename="a.bin"'}))

    fragment = media.prepare_media(
        {"kind": "file", "url": "https://x/a", "aeskey": _aeskey()}, "m1")
    assert "无法直接解析" in fragment
    assert "a.bin" in fragment


def test_docx_rejects_dtd(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "MEDIA_DIR", str(tmp_path))
    src = tmp_path / "bad.docx"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("word/document.xml",
                   '<!DOCTYPE x [<!ENTITY e "boom">]><x>&e;</x>')
    monkeypatch.setattr(
        media, "download",
        lambda url, max_bytes=media.MAX_BYTES:
        (_encrypt(src.read_bytes()),
         {"Content-Disposition": 'filename="bad.docx"'}))

    with pytest.raises(ValueError, match="DTD"):
        media.prepare_media(
            {"kind": "file", "url": "https://x/a", "aeskey": _aeskey()}, "m1")


def test_download_rejects_bad_url_and_oversize(monkeypatch):
    with pytest.raises(ValueError, match="下载地址"):
        media.download("file:///etc/passwd")

    class Resp:
        headers = {}

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            yield b"x" * 10

        def close(self):
            pass

    monkeypatch.setattr(media.requests, "get", lambda *a, **k: Resp())
    with pytest.raises(ValueError, match="20MB|超过"):
        media.download("https://x/a", max_bytes=5)
