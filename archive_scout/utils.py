from __future__ import annotations

import calendar
import hashlib
import html
import json
import os
import re
import tempfile
import unicodedata
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

SPACE_PATTERN = re.compile(r"\s+")

JS_ESCAPE_PATTERN = re.compile(r"\\u([0-9A-Fa-f]{4})|\\x([0-9A-Fa-f]{2})")


def decode_common_escapes(value: str) -> str:
    """Decode conservative JavaScript/JSON hex escapes without interpreting backslashes generally."""
    def repl(match: re.Match[str]) -> str:
        token = match.group(1) or match.group(2)
        try:
            return chr(int(token, 16))
        except (TypeError, ValueError, OverflowError):
            return match.group(0)
    return JS_ESCAPE_PATTERN.sub(repl, value or "")

CDX_RESERVED_PARAMETERS = {
    "url", "from", "to", "output", "fl", "showresumekey", "resumekey", "limit", "matchtype"
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_space(value: str) -> str:
    return SPACE_PATTERN.sub(" ", value or "").strip()


def normalize_search(value: str) -> str:
    value = decode_common_escapes(html.unescape(urllib.parse.unquote(value or "")))
    value = unicodedata.normalize("NFKC", value).casefold().replace("_", " ")
    return clean_space(value)


def normalize_target(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("target cannot be empty")
    # Accept a copied, wholly encoded URL without unquoting real path escapes
    # in ordinary targets (a%2Fb and a/b can identify different archive URLs).
    for _ in range(3):
        encoded_scheme = re.match(r"(?i)^https?%(?:25)*3a", value)
        encoded_host_path = "/" not in value and re.match(r"(?i)^[\w.*-]+(?:\.[\w*-]+)+%(?:25)*2f", value)
        if not (encoded_scheme or encoded_host_path):
            break
        value = urllib.parse.unquote(value)
    parsed = urllib.parse.urlsplit(value if "://" in value else "https://" + value)
    if parsed.hostname == "web.archive.org" and parsed.path.rstrip("/") in {
        "/cdx/search/cdx", "/web/timemap/json", "/web/timemap/cdx",
    }:
        # A CDX request is a wrapper around a target, never a site to index.
        targets = urllib.parse.parse_qs(html.unescape(parsed.query)).get("url", [])
        if len(targets) != 1 or not targets[0].strip():
            raise ValueError("CDX request must contain exactly one url parameter; enter the original site/path")
        nested = targets[0]
        if "web.archive.org/cdx/search/cdx" in nested or "web.archive.org/web/timemap/" in nested:
            raise ValueError("Nested CDX requests are not targets; enter the original site/path")
        return normalize_target(nested)
    value = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", value).lstrip("/")
    if not value:
        raise ValueError("target cannot be empty")
    if "*" in value:
        return value
    if "/" not in value:
        return value.rstrip("/") + "/*"
    if value.endswith("/"):
        return value + "*"
    return value + "*"


def cdx_request_url(endpoint: str, params: Iterable[tuple[str, str]]) -> str:
    """Encode the *parameter values*, keeping readable CDX path/wildcard syntax.

    Ampersands, plus signs, fragments and existing percent escapes inside a
    nested target/resume key must still be quoted. Do not unquote the final URL.
    """
    query = urllib.parse.urlencode(list(params), doseq=True, safe=":/*,", quote_via=urllib.parse.quote)
    return endpoint + ("&" if "?" in endpoint else "?") + query


def normalize_cdx_date(value: str, end: bool = False) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(
            "CDX dates must be YYYY, YYYYMM, YYYYMMDD, YYYYMMDDhhmmss, "
            "MM/DD/YYYY, or YYYY-MM-DD"
        )

    # Accept the common human-readable formats users naturally enter while
    # preserving every compact CDX format supported by earlier releases.
    for date_format in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            parsed = datetime.strptime(raw, date_format)
        except ValueError:
            continue
        return parsed.strftime("%Y%m%d") + ("235959" if end else "000000")

    digits = re.sub(r"[^0-9]", "", raw)
    try:
        if len(digits) == 4:
            year = int(digits)
            datetime(year, 1, 1)
            return digits + ("1231235959" if end else "0101000000")
        if len(digits) == 6:
            year = int(digits[:4])
            month = int(digits[4:6])
            day = calendar.monthrange(year, month)[1] if end else 1
            return f"{year:04d}{month:02d}{day:02d}" + ("235959" if end else "000000")
        if len(digits) == 8:
            parsed = datetime.strptime(digits, "%Y%m%d")
            return parsed.strftime("%Y%m%d") + ("235959" if end else "000000")
        if len(digits) == 14:
            datetime.strptime(digits, "%Y%m%d%H%M%S")
            return digits
    except (ValueError, OverflowError) as exc:
        raise ValueError(
            f"Invalid CDX date {value!r}. Use YYYY, YYYYMM, YYYYMMDD, "
            "YYYYMMDDhhmmss, MM/DD/YYYY, or YYYY-MM-DD."
        ) from exc

    raise ValueError(
        f"Invalid CDX date {value!r}. Use YYYY, YYYYMM, YYYYMMDD, "
        "YYYYMMDDhhmmss, MM/DD/YYYY, or YYYY-MM-DD."
    )


def parse_cdx_parameter_lines(lines: Iterable[str]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if "=" not in line:
            raise ValueError(f"CDX parameter must use key=value: {line}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", key):
            raise ValueError(f"invalid CDX parameter name: {key}")
        if key.casefold() in CDX_RESERVED_PARAMETERS:
            raise ValueError(f"{key} is controlled by the app and cannot be added as an advanced parameter")
        pairs.append((key, value))
    return pairs


def hash_text(value: str, algorithm: str = "sha256") -> str:
    return hashlib.new(algorithm, value.encode("utf-8", "replace")).hexdigest()


@contextmanager
def atomic_text_writer(path: Path):
    """Yield a text handle that replaces *path* only after a successful write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace", newline="\n") as handle:
            yield handle
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def atomic_write_text(path: Path, text: str) -> None:
    with atomic_text_writer(path) as handle:
        handle.write(text)


def atomic_write_lines(path: Path, lines: Iterable[str]) -> None:
    """Atomically stream text lines without building one project-sized string."""
    with atomic_text_writer(path) as handle:
        for line in lines:
            text = str(line)
            handle.write(text)
            if text and not text.endswith("\n"):
                handle.write("\n")


def json_value(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default
