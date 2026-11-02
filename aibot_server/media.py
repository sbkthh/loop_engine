"""Inbound media for the aibot long connection: download, decrypt, save,
and turn the result into a prompt fragment the agent can consume.

WeCom encrypts every attachment URL with a per-message AES key
(`image.aeskey` / `file.aeskey`): AES-256-CBC, IV = first 16 bytes of the
key, PKCS#7 padded to 32 bytes. The key does not depend on bot credentials.
"""
import base64
import os
import re
import subprocess
import time
import zipfile
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree as ET

import requests

from constants import DATA_DIR

MEDIA_DIR = os.path.join(DATA_DIR, "files", "aibot")
MAX_BYTES = 20 * 1024 * 1024
MAX_ENTRY_BYTES = 20 * 1024 * 1024
MAX_TEXT = 50000
TTL = 24 * 3600

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
TEXT_EXT = {
    ".txt", ".log", ".md", ".markdown", ".json", ".yaml", ".yml",
    ".csv", ".tsv", ".xml", ".html", ".htm", ".ini", ".conf", ".cfg",
    ".sql", ".py", ".js", ".ts", ".java", ".go", ".sh", ".properties",
}
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def download(url, max_bytes=MAX_BYTES):
    """GET an attachment URL. Returns (bytes, headers)."""
    if not re.match(r"^https?://", url or ""):
        raise ValueError("附件下载地址无效")
    r = requests.get(url, timeout=(10, 60), stream=True)
    try:
        r.raise_for_status()
        chunks = []
        total = 0
        for chunk in r.iter_content(65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"附件超过 {max_bytes // 1024 // 1024}MB 限制")
            chunks.append(chunk)
        return b"".join(chunks), r.headers
    finally:
        close = getattr(r, "close", None)
        if close:
            close()


def _decode_aeskey(aeskey):
    """Decode the per-attachment key. Accepts standard/URL-safe base64,
    missing padding, and percent-encoded values (Node's Buffer.from tolerates
    all of these; strict Python base64 does not)."""
    value = unquote((aeskey or "").strip())
    if not value:
        raise ValueError("附件缺少解密密钥")
    if len(value) == 64 and re.fullmatch(r"[0-9a-fA-F]{64}", value):
        return bytes.fromhex(value)
    std = value.replace("-", "+").replace("_", "/")
    std += "=" * (-len(std) % 4)
    try:
        key = base64.b64decode(std)
    except Exception as exc:
        raise ValueError("附件解密密钥无效") from exc
    if len(key) != 32:
        raise ValueError("附件解密密钥长度不是 32 字节")
    return key


def decrypt(data, aeskey):
    """AES-256-CBC decrypt with WeCom's 32-byte PKCS#7 padding."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if not data:
        raise ValueError("附件内容为空")
    key = _decode_aeskey(aeskey)
    if len(data) % 16:
        raise ValueError("附件密文长度无效")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    padded = decryptor.update(data) + decryptor.finalize()
    pad = padded[-1] if padded else 0
    if not 1 <= pad <= 32 or pad > len(padded) or \
            padded[-pad:] != bytes([pad]) * pad:
        raise ValueError("附件解密失败（填充无效）")
    return padded[:-pad]


def _sniff(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"%PDF"):
        return ".pdf"
    if data.startswith(b"PK\x03\x04"):
        return ".zip"
    return ".bin"


def _url_ext(url):
    ext = os.path.splitext(os.path.basename(urlparse(url).path))[1].lower()
    return ext if re.fullmatch(r"\.[a-z0-9]{1,8}", ext) else ""


def _fix_header_text(value):
    # urllib3 decodes headers as latin-1; non-ASCII filenames arrive mojibaked.
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def filename(headers, url, msgid, kind, data):
    """Pick a safe local filename: Content-Disposition first, then URL."""
    cd = ""
    for key, value in (headers or {}).items():
        if key.lower() == "content-disposition":
            cd = value or ""
            break
    name = ""
    m = re.search(r"filename\*=UTF-8''([^;\s]+)", cd, re.I)
    if m:
        name = unquote(m.group(1))
    else:
        m = re.search(r'filename="?([^";]+)"?', cd, re.I)
        if m:
            name = unquote(m.group(1).strip())
    name = _fix_header_text(os.path.basename(name.replace("\\", "/")))
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    if not name or name in (".", ".."):
        ext = _sniff(data) if kind == "image" else _url_ext(url)
        name = f"{msgid}{ext}"
    if not os.path.splitext(name)[1]:
        ext = _url_ext(url) if kind != "image" else ""
        name += ext or _sniff(data)
    return name


def _cleanup(ttl=TTL):
    now = time.time()
    try:
        entries = list(os.scandir(MEDIA_DIR))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_file() and now - entry.stat().st_mtime > ttl:
                os.unlink(entry.path)
        except OSError:
            pass


def save(data, name, msgid):
    os.makedirs(MEDIA_DIR, exist_ok=True)
    _cleanup()
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", msgid)[:40] or "media"
    stem, ext = os.path.splitext(name)
    path = os.path.join(MEDIA_DIR, f"{safe_id}-{stem}{ext}")
    index = 1
    while os.path.exists(path):
        path = os.path.join(MEDIA_DIR, f"{safe_id}-{stem}-{index}{ext}")
        index += 1
    with open(path, "wb") as f:
        f.write(data)
    return path


def _cap(text, limit=MAX_TEXT):
    text = (text or "").strip()
    if len(text) > limit:
        return text[:limit] + "\n…（内容过长，已截断）"
    return text


def _xml(data):
    # Office files are untrusted input: refuse DTD/entity declarations
    # outright rather than relying on parser-specific expansion limits.
    head = data[:4096]
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
        raise ValueError("文档 XML 含不安全的 DTD/实体声明")
    return ET.fromstring(data)


def _zip_read(z, name):
    info = z.getinfo(name)
    if info.file_size > MAX_ENTRY_BYTES:
        raise ValueError("文档内容过大")
    return z.read(info)


def _docx_text(path):
    with zipfile.ZipFile(path) as z:
        root = _xml(_zip_read(z, "word/document.xml"))
    paragraphs = []
    for para in root.iter(_W_NS + "p"):
        text = "".join(t.text or "" for t in para.iter(_W_NS + "t"))
        if text.strip():
            paragraphs.append(text)
    return "\n".join(paragraphs)


def _xlsx_openpyxl(path):
    import openpyxl  # optional; stdlib fallback below

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        out = []
        for ws in wb.worksheets:
            out.append(f"## {ws.title}")
            for row in ws.iter_rows(values_only=True):
                vals = ["" if v is None else str(v) for v in row]
                if any(v.strip() for v in vals):
                    out.append("\t".join(vals).rstrip())
        return "\n".join(out)
    finally:
        wb.close()


def _xlsx_stdlib(path):
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = _xml(_zip_read(z, "xl/sharedStrings.xml"))
            for si in root:
                shared.append("".join(t.text or "" for t in si.iter()
                                      if t.tag.endswith("}t")))
        out = []
        sheets = sorted(n for n in z.namelist()
                        if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        for sheet in sheets:
            out.append(f"## {os.path.basename(sheet)}")
            root = _xml(_zip_read(z, sheet))
            for row in root.iter():
                if not row.tag.endswith("}row"):
                    continue
                vals = []
                for cell in row:
                    if not cell.tag.endswith("}c"):
                        continue
                    ctype = cell.get("t")
                    value = next((x for x in cell if x.tag.endswith("}v")), None)
                    if ctype == "s" and value is not None:
                        try:
                            vals.append(shared[int(value.text)])
                        except (ValueError, IndexError):
                            vals.append(value.text or "")
                    elif ctype == "inlineStr":
                        vals.append("".join(x.text or "" for x in cell.iter()
                                            if x.tag.endswith("}t")))
                    else:
                        vals.append(value.text if value is not None and value.text else "")
                if any(v.strip() for v in vals):
                    out.append("\t".join(vals).rstrip())
        return "\n".join(out)


def _xlsx_text(path):
    try:
        return _xlsx_openpyxl(path)
    except ImportError:
        return _xlsx_stdlib(path)


def _doc_text(path):
    r = subprocess.run(["textutil", "-convert", "txt", "-stdout", path],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise ValueError("doc 解析失败")
    return r.stdout


def _file_fragment(name, path, extracted):
    head = f"[用户发送了文件：{name}]\n文件已保存到：{path}\n"
    if not extracted.strip():
        return head + "未能抽取到文本内容。"
    return head + f"已抽取文本内容如下（可能截断）：\n```text\n{extracted}\n```"


def prepare_media(item, msgid):
    """Download + decrypt one image/file and return a prompt fragment.

    Raises ValueError with a user-safe message when the attachment cannot be
    fetched or decoded; the caller answers generically and logs the detail.
    """
    data, headers = download(item.get("url") or "")
    data = decrypt(data, item.get("aeskey") or "")
    kind = item.get("kind")
    name = filename(headers, item.get("url") or "", msgid, kind, data)
    path = save(data, name, msgid)
    ext = os.path.splitext(name)[1].lower()

    if kind == "image" or ext in IMAGE_EXT:
        return (f"[用户发送了图片]\n图片已保存到：{path}\n"
                "请用 Read 工具查看图片内容后再回答。")
    if ext in TEXT_EXT or ext == ".pdf":
        label = "PDF" if ext == ".pdf" else "文本文件"
        return (f"[用户发送了文件：{name}]\n{label}已保存到：{path}\n"
                "请用 Read 工具读取内容后再回答。")
    if ext == ".docx":
        return _file_fragment(name, path, _cap(_docx_text(path)))
    if ext == ".xlsx":
        return _file_fragment(name, path, _cap(_xlsx_text(path)))
    if ext == ".doc":
        return _file_fragment(name, path, _cap(_doc_text(path)))
    return (f"[用户发送了文件：{name}]\n文件已保存到：{path}\n"
            "该格式无法直接解析，请勿臆测内容；需要时可用 Bash 查看"
            "（如 unzip -l / file）。")
