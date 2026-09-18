from __future__ import annotations

import codecs
import html
import re
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

try:
    from selectolax.lexbor import LexborHTMLParser
except Exception:  # pragma: no cover - fallback retained for source-only environments
    LexborHTMLParser = None

from .constants import BINARY_EXTENSIONS, TEXT_EXTENSIONS
from .utils import clean_space

URL_PATTERN = re.compile(r'''(?ix)\b(?:https?://|ftp://|www\.)[^\s<>"'()\[\]{}]+''')
TITLE_PATTERN = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
TAG_PATTERN = re.compile(r"(?is)<[^>]+>")
CHARSET_PATTERN = re.compile(r"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", re.IGNORECASE)
REPLAY_ERROR_MARKERS = (
    ("this url has been excluded from the wayback machine", "wayback_excluded"),
    ("blocked site error", "wayback_excluded"),
    ("page cannot be displayed due to robots.txt", "robots_blocked"),
    ("not saved because of robots.txt", "robots_blocked"),
    ("wayback machine doesn't have that page archived", "missing_capture"),
    ("wayback machine has not archived that url", "missing_capture"),
    ("not in archive", "missing_capture"),
    ("the machine that serves this file is down", "origin_unavailable"),
)


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.text: list[str] = []
        self.ignore_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered in {"script", "style", "noscript", "svg"}:
            self.ignore_depth += 1
        for key, value in attrs:
            if value and key.lower() in {"href", "src", "data", "poster", "action", "movie"}:
                self.links.append(value.strip())

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in {"script", "style", "noscript", "svg"} and self.ignore_depth:
            self.ignore_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.ignore_depth and data:
            self.text.append(data)


def safe_urlsplit(url: str):
    try:
        return urllib.parse.urlsplit(url)
    except (TypeError, ValueError, UnicodeError):
        return None


def normalize_link(raw: str, base: str) -> str:
    raw = html.unescape(raw or "").strip().strip("'\"").rstrip(".,;:!?)]]}")
    if not raw:
        return ""
    if raw.lower().startswith("www."):
        raw = "http://" + raw
    try:
        return urllib.parse.urljoin(base, raw)
    except ValueError:
        return raw


def title_from_html(raw: str) -> str:
    match = TITLE_PATTERN.search(raw)
    if not match:
        return ""
    return clean_space(html.unescape(TAG_PATTERN.sub(" ", match.group(1))))[:500]


def _encoding_candidates(data: bytes, content_type: str = "") -> list[str]:
    if data.startswith(codecs.BOM_UTF8):
        return ["utf-8-sig", "utf-8"]
    if data.startswith(codecs.BOM_UTF32_LE):
        return ["utf-32-le"]
    if data.startswith(codecs.BOM_UTF32_BE):
        return ["utf-32-be"]
    if data.startswith(codecs.BOM_UTF16_LE):
        return ["utf-16-le"]
    if data.startswith(codecs.BOM_UTF16_BE):
        return ["utf-16-be"]
    candidates: list[str] = []
    charset_match = CHARSET_PATTERN.search(content_type or "")
    if charset_match:
        candidates.append(charset_match.group(1))
    head = data[:16384].decode("ascii", "ignore")
    meta_match = CHARSET_PATTERN.search(head)
    if meta_match:
        candidates.append(meta_match.group(1))
    xml_match = re.search(r"(?i)<\?xml[^>]+encoding\s*=\s*[\"']([^\"']+)[\"']", head)
    if xml_match:
        candidates.append(xml_match.group(1))
    sample = data[:4096]
    if len(sample) >= 8:
        even_nuls = sum(1 for i in range(0, len(sample), 2) if sample[i] == 0)
        odd_nuls = sum(1 for i in range(1, len(sample), 2) if sample[i] == 0)
        halves = max(1, len(sample) // 2)
        if odd_nuls / halves > 0.25 and even_nuls / halves < 0.05:
            candidates.append("utf-16-le")
        elif even_nuls / halves > 0.25 and odd_nuls / halves < 0.05:
            candidates.append("utf-16-be")
    candidates.extend(["utf-8", "windows-1252", "latin-1"])
    return list(dict.fromkeys(value.casefold() for value in candidates if value))


def decode_bytes_with_encoding(data: bytes, content_type: str = "") -> tuple[str, str]:
    """Decode once, retaining both the source and its actual encoding label."""
    candidates = _encoding_candidates(data, content_type)
    for encoding in candidates:
        try:
            return data.decode(encoding), encoding
        except (LookupError, UnicodeDecodeError):
            continue
    encoding = candidates[0] if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE, codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)) else "utf-8"
    return data.decode(encoding, "replace"), encoding


def detect_encoding(data: bytes, content_type: str = "") -> str:
    return decode_bytes_with_encoding(data, content_type)[1]


def decode_bytes(data: bytes, content_type: str = "") -> str:
    return decode_bytes_with_encoding(data, content_type)[0]


def has_binary_signature(data: bytes) -> bool:
    """Recognize containers before believing a server's often incorrect MIME.

    SVG is deliberately absent: it is XML and remains searchable as text, while
    the media workflow may independently retain it as an image.
    """
    return data.startswith((
        b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a",
        b"II*\x00", b"MM\x00*", b"\x00\x00\x01\x00", b"\x00\x00\x02\x00",
        b"%PDF-", b"PK\x03\x04", b"PK\x05\x06", b"\x1f\x8b", b"Rar!",
        b"7z\xbc\xaf\x27\x1c", b"\xd0\xcf\x11\xe0", b"\x7fELF", b"SQLite format 3\x00",
        b"OggS", b"fLaC", b"\x1a\x45\xdf\xa3", b"\x30\x26\xb2\x75\x8e\x66\xcf\x11",
    )) or (
        len(data) >= 12 and (
            (data[:4] == b"RIFF" and data[8:12] in {b"WEBP", b"WAVE", b"AVI "})
            or data[4:8] == b"ftyp"
        )
    ) or (data.startswith(b"ID3") and len(data) >= 10 and data[3] in {2, 3, 4})


def looks_textual_bytes(data: bytes, content_type: str = "") -> bool:
    if has_binary_signature(data):
        return False
    if not data:
        return True
    for encoding in _encoding_candidates(data[:16384], content_type):
        if not encoding.startswith(("utf-16", "utf-32")):
            continue
        try:
            # A bounded preview may end in half a character/surrogate pair.
            decoded = codecs.getincrementaldecoder(encoding)("strict").decode(data[:16384], final=False)
            if decoded and sum(ch.isprintable() or ch.isspace() for ch in decoded) / max(1, len(decoded)) > 0.85:
                return True
        except (LookupError, UnicodeDecodeError):
            pass
    # Once metadata was ambiguous enough to fetch, printable source wins over
    # misleading image/video headers too (for example a PHP page ending .jpg).
    sample = data[:8192]
    if b"\x00" in sample:
        return False
    control = sum(byte < 9 or 13 < byte < 32 for byte in sample)
    return control / len(sample) < 0.05


def classify_text_candidate(url: str, mimetype: str = "") -> str:
    """Return text, binary, or ambiguous from archive metadata."""
    parsed = safe_urlsplit(url)
    if parsed:
        filename = parsed.path.rsplit("/", 1)[-1]
        dot = filename.rfind(".")
        extension = filename[dot:].casefold() if dot > 0 else ""
    else:
        extension = ""
    mime = (mimetype or "").split(";", 1)[0].strip().casefold()
    ext_text = extension in TEXT_EXTENSIONS or extension == ".svg"
    ext_binary = extension in BINARY_EXTENSIONS and extension != ".svg"
    mime_text = mime.startswith("text/") or any(token in mime for token in ("html", "xml", "json", "javascript", "svg"))
    mime_binary = (
        mime.startswith(("image/", "audio/", "video/", "font/"))
        or any(token in mime for token in ("zip", "rar", "gzip", "pdf", "shockwave", "msword"))
    ) and "svg" not in mime
    weak_mime = not mime or "octet-stream" in mime or mime in {"application/binary", "binary/octet-stream"}
    if ext_text and not mime_binary:
        return "text"
    if mime_text and not ext_binary:
        return "text"
    if (ext_text and mime_binary) or (ext_binary and mime_text):
        return "ambiguous"
    if ext_binary and mime_binary:
        return "binary"
    if ext_binary and weak_mime:
        return "ambiguous"
    if mime_binary and not extension:
        return "ambiguous"
    if mime_binary and extension and not ext_text:
        return "binary"
    if weak_mime or not extension:
        return "ambiguous"
    return "text"


def is_text_candidate(url: str, mimetype: str = "") -> bool:
    return classify_text_candidate(url, mimetype) != "binary"

def parse_page(raw: str, original: str) -> tuple[str, str, list[str]]:
    links: set[str] = set()
    title = ""
    visible = ""
    if LexborHTMLParser is not None:
        try:
            tree = LexborHTMLParser(raw)
            title_node = tree.css_first("title")
            if title_node is not None:
                title = clean_space(title_node.text(deep=True, separator=" ", strip=True))[:500]
            root = tree.root
            if root is not None:
                for node in root.traverse():
                    attrs = node.attributes
                    for key in ("href", "src", "data", "poster", "action", "movie"):
                        value = attrs.get(key)
                        if value:
                            normalized = normalize_link(value, original)
                            if normalized:
                                links.add(normalized)
                for tag in ("script", "style", "noscript", "svg"):
                    for node in tree.css(tag):
                        node.decompose()
                text_root = tree.body or tree.root
                if text_root is not None:
                    visible = clean_space(text_root.text(deep=True, separator=" ", strip=True))
        except Exception:
            title = ""
            visible = ""
            links.clear()
    if not visible and not links:
        parser = PageParser()
        try:
            parser.feed(raw)
        except Exception:
            pass
        visible = clean_space(" ".join(parser.text))
        for value in parser.links:
            normalized = normalize_link(value, original)
            if normalized:
                links.add(normalized)
    if not title:
        title = title_from_html(raw)
    for value in URL_PATTERN.findall(raw):
        normalized = normalize_link(value, original)
        if normalized:
            links.add(normalized)
    return title, visible, sorted(links)


def classify_replay_content(raw: str, final_url: str) -> str | None:
    """Return a specific Wayback replay problem when an error shell was saved as HTTP 200.

    Wayback sometimes serves explanatory HTML instead of the requested capture. Keeping
    these reasons separate makes the UI useful: an exclusion or robots policy should not
    look like a transient network failure, while an unavailable origin can be retried later.
    """
    lowered = raw[:20000].casefold()
    for marker, category in REPLAY_ERROR_MARKERS:
        if marker in lowered:
            return category
    if "/web/" not in final_url and "web.archive.org" in final_url:
        return "invalid_wayback_replay"
    return None
