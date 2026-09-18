from __future__ import annotations

import hashlib
import json
from datetime import datetime

from ..config import ProjectConfig
from ..utils import parse_cdx_parameter_lines


def cdx_query_signature(config: ProjectConfig, page_size: int | None = None) -> str:
    payload = {
        "from": config.from_date,
        "to": config.to_date,
        "filters": config.cdx_filters,
        "collapses": config.cdx_collapses,
        "match_type": config.cdx_match_type,
        "extra": config.cdx_extra_params,
        "page_size": int(config.page_size if page_size is None else page_size),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]

def cdx_query_signatures(config: ProjectConfig) -> tuple[str, ...]:
    """Return the current signature plus compatible earlier page-size variants.

    Page size affects how the same result set is transported, not its meaning.
    Earlier Archive Scout builds included it in the signature, so the current engine can
    adopt completed work created with the earlier defaults instead of forcing
    an expensive re-index.
    """
    sizes = [config.page_size, 5000, 25000, 1000, 10000, 50000, 100000, 150000]
    return tuple(dict.fromkeys(cdx_query_signature(config, size) for size in sizes))


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
    page_size: int | None = None,
) -> list[tuple[str, str]]:
    params = [
        ("url", cdx_target_value(target, config.cdx_match_type)),
        ("from", start),
        ("to", end),
        ("output", "json"),
        ("fl", "urlkey,timestamp,original,mimetype,statuscode,digest,length"),
    ]
    if config.cdx_match_type:
        params.append(("matchType", config.cdx_match_type))
    params.extend(("filter", value) for value in config.cdx_filters)
    params.extend(("collapse", value) for value in config.cdx_collapses)
    params.extend(parse_cdx_parameter_lines(config.cdx_extra_params))
    params.extend([("limit", str(page_size or config.page_size)), ("showResumeKey", "true")])
    if resume:
        params.append(("resumeKey", resume))
    return params


def parse_cdx(payload: object) -> tuple[list[dict[str, str]], str | None]:
    # The extension/legacy API must enforce the same completeness contract as
    # the compact hot-path parser; otherwise malformed rows silently vanish.
    from .client import parse_cdx_rows_payload
    validated = parse_cdx_rows_payload(payload)
    if payload == [] or isinstance(payload, dict):
        return [], None
    header = payload[0]
    rows = [dict(zip(header, item)) for item in payload[1:1 + len(validated.rows)]]
    return rows, validated.resume_key


def cdx_endpoints(config: ProjectConfig) -> tuple[str, ...]:
    from ..constants import CDX_URL, CDX_TIMEMAP_JSON_URL, CDX_TIMEMAP_URL
    mode = config.network.normalized().endpoint_mode
    if mode == "cdx":
        return (CDX_URL,)
    if mode == "timemap":
        return (CDX_TIMEMAP_JSON_URL, CDX_TIMEMAP_URL)
    # Resume-key traversal is a CDX operation. Keep CDX first here and use the
    # Timemap-first endpoint order only for numbered-page acquisition below.
    return (CDX_URL, CDX_TIMEMAP_JSON_URL, CDX_TIMEMAP_URL)


def cdx_paged_endpoints(config: ProjectConfig) -> tuple[str, ...]:
    from ..constants import CDX_URL, CDX_TIMEMAP_JSON_URL
    mode = config.network.normalized().endpoint_mode
    if mode == "cdx":
        return (CDX_URL,)
    # Numbered automatic paging follows the reference downloader exactly: one
    # native Timemap JSON service. A failed page remains one durable page retry;
    # it is not silently reissued against different endpoint semantics.
    return (CDX_TIMEMAP_JSON_URL,)


def is_broad_cdx_query(config: ProjectConfig, target: str) -> bool:
    if config.cdx_match_type in {"prefix", "host", "domain"}:
        return True
    value = target.strip()
    return value.endswith("/*") or value.startswith("*.") or value.endswith("*")


def preferred_index_strategy(config: ProjectConfig, target: str) -> str:
    strategy = config.network.normalized().index_strategy
    if strategy != "auto":
        return strategy
    # v1.0.5 follows the fast downloader's acquisition model: ask Timemap for
    # the page count once, then keep a bounded pool of numbered page requests
    # continuously occupied. Resume-key traversal remains the automatic fallback
    # when page counting/pagination is unavailable or one page stays pathological.
    return "paged"


def build_num_pages_params(
    config: ProjectConfig,
    target: str,
    start: str,
    end: str,
    page_blocks: int | None = None,
) -> list[tuple[str, str]]:
    params = build_cdx_params(config, target, start, end, page_size=config.page_size)
    params = [
        (key, value)
        for key, value in params
        if key not in {"limit", "showResumeKey", "resumeKey", "fl"}
    ]
    params.append(("showNumPages", "true"))
    blocks = config.network.normalized().page_blocks if page_blocks is None else int(page_blocks)
    # The Settings-tab default is 0, meaning "automatic".  Archive Scout's
    # high-throughput automatic profile uses the proven pageSize=9 grouping
    # with ten parallel Timemap workers; explicit positive values remain custom.
    if blocks <= 0:
        blocks = 9
    params.append(("pageSize", str(blocks)))
    return params


def build_paged_cdx_params(
    config: ProjectConfig,
    target: str,
    start: str,
    end: str,
    page: int,
    page_blocks: int | None = None,
) -> list[tuple[str, str]]:
    params = build_cdx_params(config, target, start, end, page_size=config.page_size)
    params = [(key, value) for key, value in params if key not in {"limit", "showResumeKey", "resumeKey"}]
    params = [
        (key, "timestamp,original,mimetype,statuscode,digest,length") if key == "fl" else (key, value)
        for key, value in params
    ]
    params.append(("page", str(max(0, int(page)))))
    blocks = config.network.normalized().page_blocks if page_blocks is None else int(page_blocks)
    if blocks <= 0:
        blocks = 9
    params.append(("pageSize", str(blocks)))
    return params


def parse_num_pages(payload: object) -> int:
    if isinstance(payload, int):
        return max(0, payload)
    if isinstance(payload, str) and payload.strip().isdigit():
        return max(0, int(payload.strip()))
    if isinstance(payload, list):
        # Timemap JSON's showNumPages response is normally a tiny two-row table
        # and the reference downloader reads payload[1][0].  Accept that shape
        # explicitly, while retaining the older scalar/nested compatibility.
        if len(payload) >= 2 and isinstance(payload[1], list) and payload[1]:
            value = payload[1][0]
            if str(value).strip().isdigit():
                return max(0, int(str(value).strip()))
        candidates = payload
        while isinstance(candidates, list) and len(candidates) == 1:
            candidates = candidates[0]
        if isinstance(candidates, int):
            return max(0, candidates)
        if isinstance(candidates, str) and candidates.strip().isdigit():
            return max(0, int(candidates.strip()))
    if isinstance(payload, dict):
        for key in ("pages", "numPages", "num_pages"):
            value = payload.get(key)
            if str(value).strip().isdigit():
                return max(0, int(str(value).strip()))
    raise RuntimeError(f"unexpected CDX page-count response: {payload!r}")
