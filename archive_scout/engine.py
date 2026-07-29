from __future__ import annotations

import calendar
import concurrent.futures
import gzip
import hashlib
import html
import json
import os
import random
import re
import sqlite3
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable

VERSION = "1.1.0"
CDX_URL = "https://web.archive.org/cdx/search/cdx"
REPLAY_URL = "https://web.archive.org/web"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
TEXT_EXTENSIONS = {
    ".asp", ".aspx", ".cfm", ".cgi", ".css", ".htm", ".html", ".inc",
    ".js", ".json", ".jsp", ".php", ".shtml", ".text", ".txt", ".xhtml", ".xml"
}
BINARY_EXTENSIONS = {
    ".3gp", ".7z", ".ace", ".aiff", ".asf", ".avi", ".bin", ".bmp", ".bz2",
    ".cab", ".class", ".dmg", ".doc", ".docx", ".exe", ".f4v", ".flac", ".flv",
    ".gif", ".gz", ".ico", ".iso", ".jar", ".jpeg", ".jpg", ".m4a", ".m4v",
    ".mid", ".mkv", ".mov", ".mp3", ".mp4", ".mpeg", ".mpg", ".ogg", ".ogm",
    ".ogv", ".pdf", ".png", ".ppt", ".pptx", ".qt", ".rar", ".rm", ".rmvb",
    ".swf", ".tar", ".tif", ".tiff", ".torrent", ".ts", ".vob", ".wav", ".webm",
    ".webp", ".wmv", ".xls", ".xlsx", ".zip"
}
MEDIA_EXTENSIONS = {
    ".3gp", ".asf", ".avi", ".f4v", ".flv", ".m4v", ".mkv", ".mov", ".mp4",
    ".mpeg", ".mpg", ".ogm", ".ogv", ".qt", ".rm", ".rmvb", ".swf", ".ts",
    ".vob", ".webm", ".wmv"
}
ARCHIVE_EXTENSIONS = {".7z", ".ace", ".cab", ".gz", ".rar", ".tar", ".tgz", ".zip"}
URL_PATTERN = re.compile(r'''(?ix)\b(?:https?://|ftp://|www\.)[^\s<>"'()\[\]{}]+''')
TITLE_PATTERN = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
TAG_PATTERN = re.compile(r"(?is)<[^>]+>")
SPACE_PATTERN = re.compile(r"\s+")


class Stopped(RuntimeError):
    pass


class RateLimited(RuntimeError):
    pass


@dataclass(slots=True)
class ProjectConfig:
    output_dir: Path
    targets: list[str]
    keywords: list[str]
    from_year: int = 2000
    to_year: int = datetime.now().year
    from_date: str = ""
    to_date: str = ""
    cdx_filters: list[str] = field(default_factory=lambda: ["statuscode:200"])
    cdx_collapses: list[str] = field(default_factory=lambda: ["urlkey"])
    cdx_match_type: str = ""
    cdx_extra_params: list[str] = field(default_factory=list)
    workers: int = 6
    download_scope: str = "all_text"
    minimum_score: int = 1
    max_file_mb: float = 25.0
    page_size: int = 5000
    cdx_delay: float = 0.8
    download_delay: float = 0.25
    retries: int = 6
    connect_timeout: float = 30.0
    read_timeout: float = 180.0
    max_attempts: int = 4
    user_agent: str = "ArchiveScout/1.1 public web archive research client"

    def normalized(self) -> "ProjectConfig":
        targets = list(dict.fromkeys(normalize_target(value) for value in self.targets if value.strip()))
        keywords = list(dict.fromkeys(value.strip() for value in self.keywords if value.strip()))
        output_dir = self.output_dir.expanduser().resolve()
        from_date = normalize_cdx_date(self.from_date or str(self.from_year), end=False)
        to_date = normalize_cdx_date(self.to_date or str(self.to_year), end=True)
        filters = list(dict.fromkeys(value.strip() for value in self.cdx_filters if value.strip()))
        collapses = list(dict.fromkeys(value.strip() for value in self.cdx_collapses if value.strip()))
        match_type = self.cdx_match_type.strip()
        if match_type not in {"", "exact", "prefix", "host", "domain"}:
            raise ValueError("matchType must be exact, prefix, host, domain, or blank")
        extra_params = [f"{key}={value}" for key, value in parse_cdx_parameter_lines(self.cdx_extra_params)]
        return ProjectConfig(
            output_dir=output_dir,
            targets=targets,
            keywords=keywords,
            from_year=int(from_date[:4]),
            to_year=int(to_date[:4]),
            from_date=from_date,
            to_date=to_date,
            cdx_filters=filters,
            cdx_collapses=collapses,
            cdx_match_type=match_type,
            cdx_extra_params=extra_params,
            workers=min(12, max(1, int(self.workers))),
            download_scope=self.download_scope if self.download_scope in {"all_text", "keyword_urls", "index_only"} else "all_text",
            minimum_score=max(1, int(self.minimum_score)),
            max_file_mb=max(0.1, float(self.max_file_mb)),
            page_size=min(10000, max(100, int(self.page_size))),
            cdx_delay=max(0.0, float(self.cdx_delay)),
            download_delay=max(0.0, float(self.download_delay)),
            retries=min(12, max(1, int(self.retries))),
            connect_timeout=max(1.0, float(self.connect_timeout)),
            read_timeout=max(1.0, float(self.read_timeout)),
            max_attempts=min(20, max(1, int(self.max_attempts))),
            user_agent=self.user_agent.strip() or "ArchiveScout/1.1 public web archive research client",
        )

    @property
    def max_file_bytes(self) -> int:
        return int(self.max_file_mb * 1024 * 1024)


@dataclass(slots=True)
class ProgressEvent:
    stage: str
    message: str
    current: int | None = None
    total: int | None = None
    detail: dict = field(default_factory=dict)


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
        if tag.lower() in {"script", "style", "noscript", "svg"} and self.ignore_depth:
            self.ignore_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.ignore_depth and data:
            self.text.append(data)


class SharedRateLimiter:
    def __init__(self, delay: float) -> None:
        self.delay = max(0.0, delay)
        self.lock = threading.Lock()
        self.next_request = 0.0

    def wait(self, stop_event: threading.Event) -> None:
        with self.lock:
            if stop_event.is_set():
                raise Stopped
            wait = self.next_request - time.monotonic()
            if wait > 0:
                stop_event.wait(wait)
            if stop_event.is_set():
                raise Stopped
            self.next_request = time.monotonic() + self.delay


class HttpClient:
    def __init__(
        self,
        limiter: SharedRateLimiter,
        retries: int,
        timeout: float,
        user_agent: str,
        stop_event: threading.Event,
    ) -> None:
        self.limiter = limiter
        self.retries = retries
        self.timeout = timeout
        self.user_agent = user_agent
        self.stop_event = stop_event

    def get(self, url: str, max_bytes: int, accept: str = "*/*") -> dict:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": accept,
            "Connection": "close",
            "Accept-Encoding": "gzip",
        }
        last_error: Exception | None = None
        for attempt in range(self.retries):
            self.limiter.wait(self.stop_event)
            request = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    announced = response.headers.get("Content-Length")
                    if announced and announced.isdigit() and int(announced) > max_bytes:
                        raise RuntimeError(f"response exceeds {max_bytes:,} bytes")
                    chunks: list[bytes] = []
                    size = 0
                    while True:
                        if self.stop_event.is_set():
                            raise Stopped
                        chunk = response.read(min(1024 * 1024, max_bytes - size + 1))
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > max_bytes:
                            raise RuntimeError(f"response exceeds {max_bytes:,} bytes")
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    if response.headers.get("Content-Encoding", "").lower() == "gzip":
                        try:
                            data = gzip.decompress(data)
                        except OSError:
                            pass
                    return {
                        "data": data,
                        "status": int(getattr(response, "status", 200)),
                        "headers": dict(response.headers.items()),
                        "final_url": response.geturl(),
                    }
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in RETRYABLE_STATUS:
                    raise RuntimeError(f"HTTP {exc.code}: {url}") from exc
                retry_after = parse_retry_after(exc.headers.get("Retry-After"))
                if attempt + 1 == self.retries:
                    if exc.code == 429:
                        raise RateLimited(f"repeated HTTP 429 for {url}") from exc
                    raise RuntimeError(f"HTTP {exc.code} after {self.retries} attempts: {url}") from exc
                self.retry_wait(attempt, retry_after)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt + 1 == self.retries:
                    raise RuntimeError(f"network failure for {url}: {exc}") from exc
                self.retry_wait(attempt)
        raise RuntimeError(f"request failed for {url}: {last_error}")

    def get_json(self, url: str, params: list[tuple[str, str]], max_bytes: int = 64 * 1024 * 1024) -> object:
        full_url = url + "?" + urllib.parse.urlencode(params, doseq=True)
        response = self.get(full_url, max_bytes, "application/json,text/plain,*/*")
        raw = response["data"].decode("utf-8", "replace").strip()
        if not raw:
            return []
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            preview = clean_space(raw[:500])
            raise RuntimeError(f"CDX returned non-JSON content: {preview}") from exc

    def retry_wait(self, attempt: int, retry_after: float | None = None) -> None:
        base = max(float(retry_after or 0), min(120.0, 2**attempt))
        self.stop_event.wait(base * random.uniform(0.85, 1.2))
        if self.stop_event.is_set():
            raise Stopped


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_space(value: str) -> str:
    return SPACE_PATTERN.sub(" ", value or "").strip()


def normalize_search(value: str) -> str:
    value = html.unescape(urllib.parse.unquote(value or ""))
    value = unicodedata.normalize("NFKC", value).casefold()
    value = value.replace("_", " ")
    return clean_space(value)


def normalize_target(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("target cannot be empty")
    value = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", value)
    value = value.lstrip("/")
    if not value:
        raise ValueError("target cannot be empty")
    if "*" in value:
        return value
    if "/" not in value:
        return value.rstrip("/") + "/*"
    if value.endswith("/"):
        return value + "*"
    return value + "*"


def normalize_cdx_date(value: str, end: bool = False) -> str:
    digits = re.sub(r"[^0-9]", "", str(value or ""))
    if len(digits) == 4:
        return digits + ("1231235959" if end else "0101000000")
    if len(digits) == 6:
        year = int(digits[:4])
        month = int(digits[4:6])
        if not 1 <= month <= 12:
            raise ValueError(f"invalid CDX month: {value}")
        day = calendar.monthrange(year, month)[1] if end else 1
        return f"{year:04d}{month:02d}{day:02d}" + ("235959" if end else "000000")
    if len(digits) == 8:
        year = int(digits[:4])
        month = int(digits[4:6])
        day = int(digits[6:8])
        datetime(year, month, day)
        return digits + ("235959" if end else "000000")
    if len(digits) == 14:
        datetime.strptime(digits, "%Y%m%d%H%M%S")
        return digits
    raise ValueError("CDX dates must be YYYY, YYYYMM, YYYYMMDD, or YYYYMMDDhhmmss")


CDX_RESERVED_PARAMETERS = {
    "url", "from", "to", "output", "fl", "showresumekey", "resumekey", "limit", "matchtype"
}


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


def cdx_query_signature(config: ProjectConfig) -> str:
    payload = {
        "from": config.from_date,
        "to": config.to_date,
        "filters": config.cdx_filters,
        "collapses": config.cdx_collapses,
        "match_type": config.cdx_match_type,
        "extra": config.cdx_extra_params,
        "page_size": config.page_size,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def cdx_year_window(config: ProjectConfig, year: int) -> tuple[str, str] | None:
    start = max(config.from_date, f"{year:04d}0101000000")
    end = min(config.to_date, f"{year:04d}1231235959")
    if start > end:
        return None
    return start, end


def cdx_target_value(target: str, match_type: str) -> str:
    if match_type in {"exact", "prefix", "host", "domain"}:
        target = target.rstrip("*")
    if match_type in {"host", "domain"}:
        target = target.rstrip("/")
    return target


def build_cdx_params(
    config: ProjectConfig,
    target: str,
    start: str,
    end: str,
    resume: str | None = None,
) -> list[tuple[str, str]]:
    params = [
        ("url", cdx_target_value(target, config.cdx_match_type)),
        ("from", start),
        ("to", end),
        ("output", "json"),
        ("fl", "timestamp,original,mimetype,statuscode,digest,length"),
    ]
    if config.cdx_match_type:
        params.append(("matchType", config.cdx_match_type))
    params.extend(("filter", value) for value in config.cdx_filters)
    params.extend(("collapse", value) for value in config.cdx_collapses)
    params.extend(parse_cdx_parameter_lines(config.cdx_extra_params))
    params.extend([
        ("limit", str(config.page_size)),
        ("showResumeKey", "true"),
    ])
    if resume:
        params.append(("resumeKey", resume))
    return params


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


def replay_url(timestamp: str, original: str) -> str:
    encoded = urllib.parse.quote(original, safe=":/?&=#%+;,[]@!$'()*")
    return f"{REPLAY_URL}/{timestamp}id_/{encoded}"


def title_from_html(raw: str) -> str:
    match = TITLE_PATTERN.search(raw)
    if not match:
        return ""
    return clean_space(html.unescape(TAG_PATTERN.sub(" ", match.group(1))))[:500]


def decode_bytes(data: bytes, content_type: str = "") -> str:
    candidates: list[str] = []
    charset_match = re.search(r"charset\s*=\s*['\"]?([a-zA-Z0-9._-]+)", content_type or "", re.IGNORECASE)
    if charset_match:
        candidates.append(charset_match.group(1))
    head = data[:8192].decode("ascii", "ignore")
    meta_match = re.search(r"charset\s*=\s*['\"]?([a-zA-Z0-9._-]+)", head, re.IGNORECASE)
    if meta_match:
        candidates.append(meta_match.group(1))
    candidates.extend(("utf-8", "cp1252", "latin-1"))
    seen: set[str] = set()
    for encoding in candidates:
        lowered = encoding.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def looks_textual_bytes(data: bytes, content_type: str = "") -> bool:
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime.startswith("text/") or any(token in mime for token in ("html", "xml", "json", "javascript")):
        return True
    sample = data[:16384]
    if not sample:
        return True
    if b"\x00" in sample:
        return False
    stripped = sample.lstrip().lower()
    if stripped.startswith((b"<!doctype", b"<html", b"<?xml")) or b"<body" in stripped[:8192]:
        return True
    printable = sum(1 for byte in sample if byte in b"\t\n\r" or 32 <= byte <= 126 or byte >= 128)
    return printable / len(sample) >= 0.88


def is_text_candidate(original: str, mimetype: str) -> bool:
    parsed = safe_urlsplit(original)
    extension = Path(parsed.path).suffix.lower() if parsed else ""
    mime = (mimetype or "").split(";", 1)[0].lower()
    if extension in TEXT_EXTENSIONS:
        return True
    if extension in BINARY_EXTENSIONS:
        return False
    if mime.startswith("text/") or any(token in mime for token in ("html", "xml", "json", "javascript")):
        return True
    if mime.startswith(("image/", "audio/", "video/", "font/")):
        return False
    if any(token in mime for token in ("zip", "rar", "gzip", "pdf", "octet-stream", "shockwave", "msword")):
        return False
    return True


def parse_page(raw: str, original: str) -> tuple[str, str, list[str]]:
    parser = PageParser()
    try:
        parser.feed(raw)
    except Exception:
        pass
    visible = clean_space(" ".join(parser.text))
    links: set[str] = set()
    for value in parser.links:
        normalized = normalize_link(value, original)
        if normalized:
            links.add(normalized)
    for value in URL_PATTERN.findall(raw):
        normalized = normalize_link(value, original)
        if normalized:
            links.add(normalized)
    return title_from_html(raw), visible, sorted(links)


def compile_keywords(keywords: Iterable[str]) -> list[tuple[str, re.Pattern[str]]]:
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for raw in keywords:
        value = raw.strip()
        if not value:
            continue
        if value.lower().startswith("re:"):
            label = value[3:].strip()
            if not label:
                continue
            compiled.append((value, re.compile(label, re.IGNORECASE)))
        else:
            normalized = normalize_search(value)
            pattern = re.escape(normalized).replace(r"\ ", r"\s+")
            compiled.append((value, re.compile(pattern, re.IGNORECASE)))
    return compiled


def make_snippets(text: str, patterns: list[tuple[str, re.Pattern[str]]], limit: int = 5, radius: int = 220) -> list[str]:
    normalized = normalize_search(text)
    snippets: list[str] = []
    starts: list[int] = []
    for _, pattern in patterns:
        for match in pattern.finditer(normalized):
            start = max(0, match.start() - radius)
            end = min(len(normalized), match.end() + radius)
            if any(abs(start - previous) < radius for previous in starts):
                continue
            snippet = clean_space(normalized[start:end])
            if start:
                snippet = "…" + snippet
            if end < len(normalized):
                snippet += "…"
            snippets.append(snippet)
            starts.append(start)
            if len(snippets) >= limit:
                return snippets
    return snippets


def keyword_url_match(url: str, patterns: list[tuple[str, re.Pattern[str]]]) -> bool:
    normalized = normalize_search(url)
    return any(pattern.search(normalized) for _, pattern in patterns)


def link_is_interesting(link: str, patterns: list[tuple[str, re.Pattern[str]]]) -> bool:
    parsed = safe_urlsplit(link)
    extension = Path(parsed.path).suffix.lower() if parsed else ""
    if extension in MEDIA_EXTENSIONS or extension in ARCHIVE_EXTENSIONS:
        return True
    return keyword_url_match(link, patterns)


def analyze_content(
    original: str,
    title: str,
    visible: str,
    raw: str,
    links: list[str],
    patterns: list[tuple[str, re.Pattern[str]]],
) -> dict:
    fields = {
        "url": original,
        "title": title,
        "body": visible,
        "source": raw[:500000],
        "links": "\n".join(links),
    }
    multipliers = {"url": 5, "title": 4, "body": 1, "source": 1, "links": 2}
    hits: Counter[str] = Counter()
    hit_fields: dict[str, set[str]] = {}
    score = 0
    matched_patterns: list[tuple[str, re.Pattern[str]]] = []
    for field_name, value in fields.items():
        normalized = normalize_search(value)
        for label, pattern in patterns:
            count = sum(1 for _ in pattern.finditer(normalized))
            if not count:
                continue
            hits[label] += count
            hit_fields.setdefault(label, set()).add(field_name)
            score += min(count, 10) * multipliers[field_name]
            matched_patterns.append((label, pattern))
    interesting_links = sorted({link for link in links if link_is_interesting(link, patterns)})
    snippets = make_snippets(visible or raw, list(dict.fromkeys(matched_patterns))) if hits else []
    return {
        "score": score,
        "hits": dict(sorted(hits.items())),
        "hit_fields": {key: sorted(value) for key, value in hit_fields.items()},
        "snippets": snippets,
        "interesting_links": interesting_links,
    }


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace", newline="\n") as handle:
            handle.write(text)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def capture_path(root: Path, timestamp: str, original: str) -> Path:
    digest = hashlib.sha1(original.encode("utf-8", "surrogatepass")).hexdigest()
    return root / "captures" / timestamp[:4] / timestamp[4:6] / f"{digest}.txt"


def open_database(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(root / "archive_scout.sqlite3", timeout=60)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA journal_mode=WAL")
    database.execute("PRAGMA synchronous=NORMAL")
    database.executescript(
        """
        CREATE TABLE IF NOT EXISTS captures(
            original TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            source_target TEXT NOT NULL,
            query_signature TEXT NOT NULL DEFAULT '',
            mimetype TEXT,
            statuscode TEXT,
            digest TEXT,
            length INTEGER DEFAULT 0,
            state TEXT DEFAULT 'pending',
            attempts INTEGER DEFAULT 0,
            path TEXT,
            title TEXT,
            score INTEGER DEFAULT 0,
            keyword_hits TEXT,
            hit_fields TEXT,
            snippets TEXT,
            interesting_links TEXT,
            bytes_saved INTEGER DEFAULT 0,
            http_status INTEGER,
            final_url TEXT,
            error TEXT,
            updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS capture_state ON captures(state,attempts,timestamp);
        CREATE INDEX IF NOT EXISTS capture_score ON captures(score DESC);
        CREATE TABLE IF NOT EXISTS index_state(
            target TEXT,
            year INTEGER,
            query_signature TEXT NOT NULL,
            resume_key TEXT,
            complete INTEGER DEFAULT 0,
            seen INTEGER DEFAULT 0,
            error TEXT,
            updated_at TEXT,
            PRIMARY KEY(target,year,query_signature)
        );
        CREATE TABLE IF NOT EXISTS project_meta(
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    capture_columns = {row[1] for row in database.execute("PRAGMA table_info(captures)")}
    if "query_signature" not in capture_columns:
        database.execute("ALTER TABLE captures ADD COLUMN query_signature TEXT NOT NULL DEFAULT ''")
    columns = {row[1] for row in database.execute("PRAGMA table_info(index_state)")}
    if "query_signature" not in columns:
        database.executescript(
            """
            ALTER TABLE index_state RENAME TO index_state_legacy;
            CREATE TABLE index_state(
                target TEXT,
                year INTEGER,
                query_signature TEXT NOT NULL,
                resume_key TEXT,
                complete INTEGER DEFAULT 0,
                seen INTEGER DEFAULT 0,
                error TEXT,
                updated_at TEXT,
                PRIMARY KEY(target,year,query_signature)
            );
            INSERT INTO index_state(target,year,query_signature,resume_key,complete,seen,error,updated_at)
            SELECT target,year,'legacy',resume_key,complete,seen,error,updated_at FROM index_state_legacy;
            DROP TABLE index_state_legacy;
            """
        )
    return database


def parse_cdx(payload: object) -> tuple[list[dict[str, str]], str | None]:
    if payload in (None, []):
        return [], None
    if isinstance(payload, dict):
        message = str(payload.get("message") or payload.get("error") or payload)
        lowered = message.lower()
        if "no capture" in lowered or "no result" in lowered or "not found" in lowered:
            return [], None
        if "429" in lowered or "rate limit" in lowered or "too many requests" in lowered:
            raise RateLimited(message)
        raise RuntimeError(message)
    if not isinstance(payload, list) or not payload:
        return [], None
    header = payload[0]
    if not isinstance(header, list):
        raise RuntimeError("unexpected CDX response header")
    body = payload[1:]
    resume = None
    if len(body) >= 2 and body[-2] == [] and isinstance(body[-1], list) and len(body[-1]) == 1:
        resume = str(body[-1][0])
        body = body[:-2]
    rows: list[dict[str, str]] = []
    for item in body:
        if not item or not isinstance(item, list) or len(item) != len(header):
            continue
        row = dict(zip(header, item))
        if row.get("timestamp") and row.get("original"):
            rows.append(row)
    return rows, resume


def upsert_capture(
    database: sqlite3.Connection,
    row: dict[str, str],
    target: str,
    query_signature: str = "",
) -> bool:
    existing = database.execute(
        "SELECT timestamp,query_signature FROM captures WHERE original=?",
        (row["original"],),
    ).fetchone()
    timestamp = row["timestamp"]
    if existing and str(existing["query_signature"] or "") == query_signature and str(existing["timestamp"]) <= timestamp:
        database.execute(
            "UPDATE captures SET source_target=?,query_signature=?,updated_at=? WHERE original=?",
            (target, query_signature, utc_now(), row["original"]),
        )
        return False
    database.execute(
        """
        INSERT INTO captures(original,timestamp,source_target,query_signature,mimetype,statuscode,digest,length,state,updated_at)
        VALUES(?,?,?,?,?,?,?,?,'pending',?)
        ON CONFLICT(original) DO UPDATE SET
            timestamp=excluded.timestamp,
            source_target=excluded.source_target,
            query_signature=excluded.query_signature,
            mimetype=excluded.mimetype,
            statuscode=excluded.statuscode,
            digest=excluded.digest,
            length=excluded.length,
            state=CASE WHEN captures.timestamp<>excluded.timestamp THEN 'pending' ELSE captures.state END,
            path=CASE WHEN captures.timestamp<>excluded.timestamp THEN NULL ELSE captures.path END,
            updated_at=excluded.updated_at
        """,
        (
            row["original"],
            timestamp,
            target,
            query_signature,
            row.get("mimetype", ""),
            row.get("statuscode", ""),
            row.get("digest", ""),
            int(row.get("length") or 0),
            utc_now(),
        ),
    )
    return True


def emit(callback: Callable[[ProgressEvent], None] | None, event: ProgressEvent) -> None:
    if callback:
        callback(event)


def index_archive(
    config: ProjectConfig,
    database: sqlite3.Connection,
    stop_event: threading.Event,
    callback: Callable[[ProgressEvent], None] | None,
) -> None:
    limiter = SharedRateLimiter(config.cdx_delay)
    client = HttpClient(
        limiter,
        config.retries,
        max(config.connect_timeout, config.read_timeout),
        config.user_agent,
        stop_event,
    )
    signature = cdx_query_signature(config)
    windows = [
        (target, year, cdx_year_window(config, year))
        for target in config.targets
        for year in range(config.from_year, config.to_year + 1)
    ]
    windows = [(target, year, window) for target, year, window in windows if window is not None]
    total_windows = len(windows)
    completed_windows = 0
    for target, year, window in windows:
        if stop_event.is_set():
            raise Stopped
        start, end = window
        state = database.execute(
            "SELECT resume_key,complete,seen FROM index_state WHERE target=? AND year=? AND query_signature=?",
            (target, year, signature),
        ).fetchone()
        if state and state["complete"]:
            completed_windows += 1
            emit(callback, ProgressEvent("index", f"Already indexed {target} for {year}", completed_windows, total_windows))
            continue
        resume = state["resume_key"] if state else None
        seen = int(state["seen"] or 0) if state else 0
        while True:
            params = build_cdx_params(config, target, start, end, resume)
            emit(callback, ProgressEvent("index", f"Indexing {target} for {year}…", completed_windows, total_windows))
            try:
                payload = client.get_json(CDX_URL, params)
                rows, next_resume = parse_cdx(payload)
                inserted = 0
                with database:
                    for row in rows:
                        inserted += int(upsert_capture(database, row, target, signature))
                    seen += len(rows)
                    database.execute(
                        """
                        INSERT INTO index_state(target,year,query_signature,resume_key,complete,seen,error,updated_at)
                        VALUES(?,?,?,?,?,?,NULL,?)
                        ON CONFLICT(target,year,query_signature) DO UPDATE SET
                            resume_key=excluded.resume_key,
                            complete=excluded.complete,
                            seen=excluded.seen,
                            error=NULL,
                            updated_at=excluded.updated_at
                        """,
                        (target, year, signature, next_resume, 0 if next_resume else 1, seen, utc_now()),
                    )
                emit(
                    callback,
                    ProgressEvent(
                        "index",
                        f"{target} {year}: received {len(rows):,}, added {inserted:,}, seen {seen:,}",
                        completed_windows,
                        total_windows,
                    ),
                )
            except Exception as exc:
                with database:
                    database.execute(
                        """
                        INSERT INTO index_state(target,year,query_signature,resume_key,complete,seen,error,updated_at)
                        VALUES(?,?,?,?,0,?,?,?)
                        ON CONFLICT(target,year,query_signature) DO UPDATE SET
                            resume_key=excluded.resume_key,
                            complete=0,
                            seen=excluded.seen,
                            error=excluded.error,
                            updated_at=excluded.updated_at
                        """,
                        (target, year, signature, resume, seen, repr(exc), utc_now()),
                    )
                raise
            if not next_resume:
                break
            if next_resume == resume:
                raise RuntimeError("CDX returned the same resume key twice")
            resume = next_resume
        completed_windows += 1
        emit(callback, ProgressEvent("index", f"Finished {target} for {year}", completed_windows, total_windows))


def select_download_rows(database: sqlite3.Connection, config: ProjectConfig, patterns: list[tuple[str, re.Pattern[str]]]) -> list[sqlite3.Row]:
    rows = database.execute(
        """
        SELECT * FROM captures
        WHERE query_signature=? AND state IN ('pending','error') AND attempts<?
        ORDER BY timestamp,original
        """,
        (cdx_query_signature(config), config.max_attempts),
    ).fetchall()
    selected: list[sqlite3.Row] = []
    for row in rows:
        if not is_text_candidate(row["original"], row["mimetype"] or ""):
            with database:
                database.execute(
                    "UPDATE captures SET state='skipped',error='non-text capture',updated_at=? WHERE original=?",
                    (utc_now(), row["original"]),
                )
            continue
        if config.download_scope == "keyword_urls" and not keyword_url_match(row["original"], patterns):
            with database:
                database.execute(
                    "UPDATE captures SET state='skipped',error='URL did not match keyword filter',updated_at=? WHERE original=?",
                    (utc_now(), row["original"]),
                )
            continue
        selected.append(row)
    return selected


def fetch_and_scan(
    row: sqlite3.Row,
    config: ProjectConfig,
    patterns: list[tuple[str, re.Pattern[str]]],
    client: HttpClient,
) -> dict:
    original = row["original"]
    timestamp = row["timestamp"]
    response = client.get(replay_url(timestamp, original), config.max_file_bytes)
    content_type = response["headers"].get("Content-Type", row["mimetype"] or "")
    data = response["data"]
    if not looks_textual_bytes(data, content_type):
        return {
            "original": original,
            "state": "skipped",
            "error": "downloaded response was not textual",
            "http_status": response["status"],
            "final_url": response["final_url"],
        }
    raw = decode_bytes(data, content_type)
    title, visible, links = parse_page(raw, original)
    analysis = analyze_content(original, title, visible, raw, links, patterns)
    path = capture_path(config.output_dir, timestamp, original)
    atomic_write_text(path, raw)
    return {
        "original": original,
        "state": "done",
        "path": str(path),
        "title": title,
        "score": analysis["score"],
        "keyword_hits": json.dumps(analysis["hits"], ensure_ascii=False, sort_keys=True),
        "hit_fields": json.dumps(analysis["hit_fields"], ensure_ascii=False, sort_keys=True),
        "snippets": json.dumps(analysis["snippets"], ensure_ascii=False),
        "interesting_links": json.dumps(analysis["interesting_links"], ensure_ascii=False),
        "bytes_saved": len(data),
        "http_status": response["status"],
        "final_url": response["final_url"],
        "error": None,
    }


def save_fetch_result(database: sqlite3.Connection, result: dict) -> None:
    with database:
        database.execute(
            """
            UPDATE captures SET
                state=?,path=?,title=?,score=?,keyword_hits=?,hit_fields=?,snippets=?,interesting_links=?,
                bytes_saved=?,http_status=?,final_url=?,error=?,updated_at=?
            WHERE original=?
            """,
            (
                result.get("state", "error"),
                result.get("path"),
                result.get("title"),
                int(result.get("score") or 0),
                result.get("keyword_hits"),
                result.get("hit_fields"),
                result.get("snippets"),
                result.get("interesting_links"),
                int(result.get("bytes_saved") or 0),
                result.get("http_status"),
                result.get("final_url"),
                result.get("error"),
                utc_now(),
                result["original"],
            ),
        )


def download_archive(
    config: ProjectConfig,
    database: sqlite3.Connection,
    stop_event: threading.Event,
    callback: Callable[[ProgressEvent], None] | None,
) -> None:
    if config.download_scope == "index_only":
        emit(callback, ProgressEvent("download", "Index-only mode selected; downloads skipped."))
        return
    patterns = compile_keywords(config.keywords)
    if not patterns:
        raise ValueError("at least one keyword is required")
    with database:
        database.execute("UPDATE captures SET state='pending' WHERE state='downloading'")
    rows = select_download_rows(database, config, patterns)
    total = len(rows)
    if not total:
        emit(callback, ProgressEvent("download", "No pending text captures to download.", 0, 0))
        return
    limiter = SharedRateLimiter(config.download_delay)
    client = HttpClient(
        limiter,
        config.retries,
        max(config.connect_timeout, config.read_timeout),
        config.user_agent,
        stop_event,
    )
    completed = 0
    matched = 0
    failures = 0
    started = time.monotonic()
    max_inflight = max(config.workers, config.workers * 3)
    row_iter = iter(rows)
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.workers, thread_name_prefix="archive-scout") as pool:
        futures: dict[concurrent.futures.Future, sqlite3.Row] = {}

        def submit_next() -> bool:
            try:
                row = next(row_iter)
            except StopIteration:
                return False
            if stop_event.is_set():
                raise Stopped
            with database:
                database.execute(
                    "UPDATE captures SET state='downloading',attempts=attempts+1,error=NULL,updated_at=? WHERE original=?",
                    (utc_now(), row["original"]),
                )
            futures[pool.submit(fetch_and_scan, row, config, patterns, client)] = row
            return True

        while len(futures) < max_inflight and submit_next():
            pass

        while futures:
            if stop_event.is_set():
                for pending in futures:
                    pending.cancel()
                raise Stopped
            done, _ = concurrent.futures.wait(
                futures,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                row = futures.pop(future)
                try:
                    result = future.result()
                except RateLimited:
                    result = {"original": row["original"], "state": "pending", "error": "HTTP 429; retry later"}
                    save_fetch_result(database, result)
                    for pending in futures:
                        pending.cancel()
                    raise
                except Stopped:
                    result = {"original": row["original"], "state": "pending", "error": "stopped"}
                except Exception as exc:
                    failures += 1
                    result = {"original": row["original"], "state": "error", "error": repr(exc)}
                save_fetch_result(database, result)
                completed += 1
                matched += int(int(result.get("score") or 0) >= config.minimum_score)
                elapsed = max(0.001, time.monotonic() - started)
                rate = completed / elapsed
                emit(
                    callback,
                    ProgressEvent(
                        "download",
                        f"Downloaded and scanned {completed:,}/{total:,}; matches {matched:,}; errors {failures:,}; {rate:.1f}/s",
                        completed,
                        total,
                        {"matched": matched, "failures": failures, "rate": rate},
                    ),
                )
                while len(futures) < max_inflight and submit_next():
                    pass

def rescan_archive(
    config: ProjectConfig,
    database: sqlite3.Connection,
    stop_event: threading.Event,
    callback: Callable[[ProgressEvent], None] | None,
) -> None:
    patterns = compile_keywords(config.keywords)
    if not patterns:
        raise ValueError("at least one keyword is required")
    rows = database.execute(
        "SELECT * FROM captures WHERE query_signature=? AND state='done' AND path IS NOT NULL ORDER BY timestamp,original",
        (cdx_query_signature(config),),
    ).fetchall()
    total = len(rows)
    for index, row in enumerate(rows, 1):
        if stop_event.is_set():
            raise Stopped
        path = Path(row["path"])
        if not path.exists():
            with database:
                database.execute(
                    "UPDATE captures SET state='pending',path=NULL,error='saved file missing',updated_at=? WHERE original=?",
                    (utc_now(), row["original"]),
                )
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
            title, visible, links = parse_page(raw, row["original"])
            analysis = analyze_content(row["original"], title, visible, raw, links, patterns)
            with database:
                database.execute(
                    """
                    UPDATE captures SET title=?,score=?,keyword_hits=?,hit_fields=?,snippets=?,interesting_links=?,error=NULL,updated_at=?
                    WHERE original=?
                    """,
                    (
                        title,
                        analysis["score"],
                        json.dumps(analysis["hits"], ensure_ascii=False, sort_keys=True),
                        json.dumps(analysis["hit_fields"], ensure_ascii=False, sort_keys=True),
                        json.dumps(analysis["snippets"], ensure_ascii=False),
                        json.dumps(analysis["interesting_links"], ensure_ascii=False),
                        utc_now(),
                        row["original"],
                    ),
                )
        except OSError as exc:
            with database:
                database.execute(
                    "UPDATE captures SET error=?,updated_at=? WHERE original=?",
                    (repr(exc), utc_now(), row["original"]),
                )
        emit(callback, ProgressEvent("rescan", f"Rescanned {index:,}/{total:,}", index, total))


def json_value(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def generate_reports(config: ProjectConfig, database: sqlite3.Connection) -> dict[str, Path]:
    output = config.output_dir / "reports"
    output.mkdir(parents=True, exist_ok=True)
    signature = cdx_query_signature(config)
    rows = database.execute(
        "SELECT * FROM captures WHERE query_signature=? AND score>=? ORDER BY score DESC,timestamp,original",
        (signature, config.minimum_score),
    ).fetchall()
    all_rows = database.execute(
        "SELECT * FROM captures WHERE query_signature=? ORDER BY original",
        (signature,),
    ).fetchall()
    state_counts = dict(
        database.execute(
            "SELECT state,COUNT(*) FROM captures WHERE query_signature=? GROUP BY state",
            (signature,),
        ).fetchall()
    )
    keyword_counts: Counter[str] = Counter()
    ranked_blocks: list[str] = []
    matched_urls: list[str] = []
    wayback_urls: list[str] = []
    link_rows: list[tuple[str, str]] = []
    for rank, row in enumerate(rows, 1):
        hits = json_value(row["keyword_hits"], {})
        fields = json_value(row["hit_fields"], {})
        snippets = json_value(row["snippets"], [])
        links = json_value(row["interesting_links"], [])
        keyword_counts.update(hits)
        matched_urls.append(row["original"])
        wayback_urls.append(replay_url(row["timestamp"], row["original"]))
        hit_lines = [
            f"{label}={count} [{','.join(fields.get(label, []))}]"
            for label, count in sorted(hits.items(), key=lambda item: (-item[1], item[0].casefold()))
        ]
        snippet_lines = [f"  {index}. {snippet}" for index, snippet in enumerate(snippets, 1)] or ["  None"]
        link_lines = [f"  {link}" for link in links] or ["  None"]
        link_rows.extend((row["original"], link) for link in links)
        ranked_blocks.append(
            "\n".join(
                [
                    "=" * 100,
                    f"RANK: {rank}",
                    f"SCORE: {row['score']}",
                    f"TIMESTAMP: {row['timestamp']}",
                    f"TITLE: {row['title'] or '(untitled)'}",
                    f"ORIGINAL URL: {row['original']}",
                    f"WAYBACK URL: {replay_url(row['timestamp'], row['original'])}",
                    f"LOCAL FILE: {row['path'] or '(not downloaded)'}",
                    f"MIME TYPE: {row['mimetype'] or '(unknown)'}",
                    f"KEYWORD HITS: {'; '.join(hit_lines) if hit_lines else 'None'}",
                    "SNIPPETS:",
                    *snippet_lines,
                    "INTERESTING LINKS:",
                    *link_lines,
                ]
            )
        )
    all_indexed_lines = [
        f"{row['timestamp']}\t{row['mimetype'] or ''}\t{row['state']}\t{row['original']}"
        for row in all_rows
    ]
    error_rows = database.execute(
        """
        SELECT timestamp,original,attempts,error FROM captures
        WHERE query_signature=? AND (state='error' OR error IS NOT NULL)
        ORDER BY timestamp,original
        """,
        (signature,),
    ).fetchall()
    error_lines = [
        f"{row['timestamp']}\tattempts={row['attempts']}\t{row['original']}\t{row['error'] or ''}"
        for row in error_rows
    ]
    summary_lines = [
        f"Archive Scout {VERSION}",
        f"Generated: {utc_now()}",
        f"Output directory: {config.output_dir}",
        f"Targets: {', '.join(config.targets)}",
        f"Date range: {config.from_date}-{config.to_date}",
        f"CDX filters: {', '.join(config.cdx_filters) or '(none)'}",
        f"CDX collapses: {', '.join(config.cdx_collapses) or '(none)'}",
        f"CDX matchType: {config.cdx_match_type or '(automatic)'}",
        f"CDX additional parameters: {', '.join(config.cdx_extra_params) or '(none)'}",
        f"CDX query signature: {signature}",
        f"Download scope: {config.download_scope}",
        f"Indexed unique earliest URLs: {len(all_rows):,}",
        f"Ranked matches at score >= {config.minimum_score}: {len(rows):,}",
        f"Total bytes saved: {sum(int(row['bytes_saved'] or 0) for row in all_rows):,}",
        "States: " + ", ".join(f"{key}={value:,}" for key, value in sorted(state_counts.items())),
    ]
    paths = {
        "matches": output / "matches_ranked.txt",
        "matched_urls": output / "matched_urls.txt",
        "wayback_urls": output / "wayback_urls.txt",
        "interesting_links": output / "interesting_links.txt",
        "keyword_counts": output / "keyword_counts.txt",
        "all_urls": output / "all_indexed_urls.txt",
        "errors": output / "errors.txt",
        "summary": output / "summary.txt",
    }
    atomic_write_text(paths["matches"], "\n\n".join(ranked_blocks) + ("\n" if ranked_blocks else ""))
    atomic_write_text(paths["matched_urls"], "\n".join(dict.fromkeys(matched_urls)) + ("\n" if matched_urls else ""))
    atomic_write_text(paths["wayback_urls"], "\n".join(dict.fromkeys(wayback_urls)) + ("\n" if wayback_urls else ""))
    atomic_write_text(
        paths["interesting_links"],
        "\n".join(f"{source}\t{link}" for source, link in sorted(set(link_rows))) + ("\n" if link_rows else ""),
    )
    atomic_write_text(
        paths["keyword_counts"],
        "\n".join(f"{count}\t{label}" for label, count in keyword_counts.most_common()) + ("\n" if keyword_counts else ""),
    )
    atomic_write_text(paths["all_urls"], "\n".join(all_indexed_lines) + ("\n" if all_indexed_lines else ""))
    atomic_write_text(paths["errors"], "\n".join(error_lines) + ("\n" if error_lines else ""))
    atomic_write_text(paths["summary"], "\n".join(summary_lines) + "\n")
    return paths


def save_project_config(config: ProjectConfig) -> Path:
    config = config.normalized()
    path = config.output_dir / "project.json"
    payload = {
        "version": VERSION,
        "output_dir": str(config.output_dir),
        "targets": config.targets,
        "keywords": config.keywords,
        "from_year": config.from_year,
        "to_year": config.to_year,
        "from_date": config.from_date,
        "to_date": config.to_date,
        "cdx_filters": config.cdx_filters,
        "cdx_collapses": config.cdx_collapses,
        "cdx_match_type": config.cdx_match_type,
        "cdx_extra_params": config.cdx_extra_params,
        "workers": config.workers,
        "download_scope": config.download_scope,
        "minimum_score": config.minimum_score,
        "max_file_mb": config.max_file_mb,
        "page_size": config.page_size,
        "cdx_delay": config.cdx_delay,
        "download_delay": config.download_delay,
        "retries": config.retries,
        "max_attempts": config.max_attempts,
        "user_agent": config.user_agent,
    }
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return path


def load_project_config(path: Path) -> ProjectConfig:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return ProjectConfig(
        output_dir=Path(payload.get("output_dir") or path.parent),
        targets=list(payload.get("targets") or []),
        keywords=list(payload.get("keywords") or []),
        from_year=int(payload.get("from_year", 2000)),
        to_year=int(payload.get("to_year", datetime.now().year)),
        from_date=str(payload.get("from_date") or payload.get("from_year", 2000)),
        to_date=str(payload.get("to_date") or payload.get("to_year", datetime.now().year)),
        cdx_filters=list(payload["cdx_filters"]) if "cdx_filters" in payload else ["statuscode:200"],
        cdx_collapses=list(payload["cdx_collapses"]) if "cdx_collapses" in payload else ["urlkey"],
        cdx_match_type=str(payload.get("cdx_match_type", "")),
        cdx_extra_params=list(payload.get("cdx_extra_params") or []),
        workers=int(payload.get("workers", 6)),
        download_scope=str(payload.get("download_scope", "all_text")),
        minimum_score=int(payload.get("minimum_score", 1)),
        max_file_mb=float(payload.get("max_file_mb", 25.0)),
        page_size=int(payload.get("page_size", 5000)),
        cdx_delay=float(payload.get("cdx_delay", 0.8)),
        download_delay=float(payload.get("download_delay", 0.25)),
        retries=int(payload.get("retries", 6)),
        max_attempts=int(payload.get("max_attempts", 4)),
        user_agent=str(payload.get("user_agent", "ArchiveScout/1.1 public web archive research client")),
    ).normalized()


def run_project(
    config: ProjectConfig,
    mode: str = "all",
    stop_event: threading.Event | None = None,
    callback: Callable[[ProgressEvent], None] | None = None,
) -> dict[str, Path]:
    config = config.normalized()
    if config.from_date > config.to_date:
        raise ValueError("start date must not be later than end date")
    if not config.targets:
        raise ValueError("at least one target is required")
    if not config.keywords and mode not in {"index", "report"}:
        raise ValueError("at least one keyword is required")
    if mode not in {"all", "index", "download", "rescan", "report"}:
        raise ValueError(f"unsupported mode: {mode}")
    stop_event = stop_event or threading.Event()
    config.output_dir.mkdir(parents=True, exist_ok=True)
    (config.output_dir / "captures").mkdir(exist_ok=True)
    (config.output_dir / "reports").mkdir(exist_ok=True)
    save_project_config(config)
    database = open_database(config.output_dir)
    try:
        if mode in {"all", "index"}:
            index_archive(config, database, stop_event, callback)
        if mode in {"all", "download"}:
            download_archive(config, database, stop_event, callback)
        if mode == "rescan":
            rescan_archive(config, database, stop_event, callback)
        paths = generate_reports(config, database)
        emit(callback, ProgressEvent("report", f"Reports written to {config.output_dir / 'reports'}"))
        return paths
    except Stopped:
        with database:
            database.execute("UPDATE captures SET state='pending' WHERE state='downloading'")
        emit(callback, ProgressEvent("stopped", "Stopped. Progress was saved and can be resumed."))
        raise
    finally:
        database.close()
