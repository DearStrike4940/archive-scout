from __future__ import annotations

import calendar
import json
import random
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from ..config import ProjectConfig
from ..database.repositories import get_or_create_target, record_error, record_recovery_event, record_site_issue, upsert_captures
from ..downloads.rate_limit import SharedFixedRateLimiter, shared_host_gate
from ..events import ConnectivityPaused, ProgressEvent, Stopped
from ..site_status import host_from_url, site_issue_message
from ..utils import utc_now
from .client import (
    HttpClient,
    PermanentRequestError,
    RateLimitDeferred,
    TransientRequestError,
    is_timeout_error,
    request_cdx_json_payload,
    request_cdx_rows,
)
from .parallel import PageFetchResult, effective_page_workers, iter_cdx_pages
from .parameters import (
    build_cdx_params,
    build_num_pages_params,
    build_paged_cdx_params,
    cdx_endpoints,
    cdx_paged_endpoints,
    cdx_query_signature,
    cdx_query_signatures,
    cdx_year_window,
    parse_num_pages,
    preferred_index_strategy,
)


PAGED_PIPELINE_PAGES = 1000
PAGED_REQUEST_ATTEMPTS = 5
PAGED_PAGE_FAILURE_LIMIT = 5


@dataclass(slots=True)
class PendingWindow:
    start: str
    end: str
    resume_key: str | None = None
    failures: int = 0
    page_size: int = 0
    strategy: str = "auto"
    page: int = 0
    page_count: int = -1
    page_blocks: int = 0
    pagination_supported: bool = True
    retry_pages: list[int] = field(default_factory=list)
    page_failures: dict[int, int] = field(default_factory=dict)


@dataclass(slots=True)
class IndexPlan:
    pending: list[PendingWindow]
    completed: int
    planned: int


@dataclass(slots=True)
class PagedBatch:
    results: list[PageFetchResult]
    requested_pages: list[int]
    finished: bool

    @property
    def successful(self) -> list[PageFetchResult]:
        return [item for item in self.results if item.succeeded]

    @property
    def failed(self) -> list[PageFetchResult]:
        return [item for item in self.results if not item.succeeded]


def emit(callback: Callable[[ProgressEvent], None] | None, event: ProgressEvent) -> None:
    if callback:
        callback(event)


def month_windows(config: ProjectConfig, year: int) -> list[tuple[str, str]]:
    windows: list[tuple[str, str]] = []
    for month in range(1, 13):
        last_day = calendar.monthrange(year, month)[1]
        start = max(config.from_date, f"{year:04d}{month:02d}01000000")
        end = min(config.to_date, f"{year:04d}{month:02d}{last_day:02d}235959")
        if start <= end:
            windows.append((start, end))
    return windows


def index_windows(config: ProjectConfig, target: str, year: int) -> list[tuple[str, str]]:
    """Start each target/year as one large resumable window.

    The timeout recovery engine already subdivides only the ranges that actually
    fail. Starting with twelve unconditional monthly windows multiplied request
    overhead on healthy archives, especially when resume-key batches are large.
    """
    window = cdx_year_window(config, year)
    return [window] if window else []


def encode_resume(start: str, end: str, resume: str | None) -> str:
    return json.dumps(
        {"version": 1, "window_start": start, "window_end": end, "resume_key": resume},
        separators=(",", ":"),
        sort_keys=True,
    )


def decode_resume(value: str | None) -> tuple[str, str, str | None] | None:
    if not value or not value.lstrip().startswith("{"):
        return None
    try:
        payload = json.loads(value)
        if int(payload.get("version", 1)) != 1:
            return None
        start = str(payload["window_start"])
        end = str(payload["window_end"])
        resume = payload.get("resume_key")
        return start, end, str(resume) if resume else None
    except Exception:
        return None


def encode_plan(plan: IndexPlan) -> str | None:
    if not plan.pending:
        return None
    payload = {
        "version": 5,
        "completed": int(plan.completed),
        "planned": int(plan.planned),
        "pending": [
            {
                "start": item.start,
                "end": item.end,
                "resume_key": item.resume_key,
                "failures": int(item.failures),
                "page_size": int(item.page_size),
                "strategy": item.strategy,
                "page": int(item.page),
                "page_count": int(item.page_count),
                "page_blocks": int(item.page_blocks),
                "pagination_supported": bool(item.pagination_supported),
                "retry_pages": sorted({int(page) for page in item.retry_pages if int(page) >= 0}),
                "page_failures": {
                    str(int(page)): max(0, int(count))
                    for page, count in item.page_failures.items()
                    if int(page) >= 0 and int(count) > 0
                },
            }
            for item in plan.pending
        ],
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def decode_plan(value: str | None, default_windows: list[tuple[str, str]]) -> IndexPlan:
    if value and value.lstrip().startswith("{"):
        try:
            payload = json.loads(value)
            version = int(payload.get("version", 1))
            if version in {2, 3, 4, 5}:
                pending: list[PendingWindow] = []
                for raw in payload.get("pending") or []:
                    start = str(raw["start"])
                    end = str(raw["end"])
                    if start > end:
                        continue
                    resume = raw.get("resume_key")
                    retry_pages = sorted({max(0, int(page)) for page in raw.get("retry_pages") or []})
                    raw_failures = raw.get("page_failures") or {}
                    page_failures = {
                        max(0, int(page)): max(0, int(count))
                        for page, count in raw_failures.items()
                        if int(count) > 0
                    }
                    pending.append(
                        PendingWindow(
                            start=start,
                            end=end,
                            resume_key=str(resume) if resume else None,
                            failures=max(0, int(raw.get("failures", 0))),
                            page_size=max(0, int(raw.get("page_size", 0))),
                            strategy=str(raw.get("strategy") or "auto"),
                            page=max(0, int(raw.get("page", 0))),
                            page_count=int(raw.get("page_count", -1)),
                            page_blocks=max(0, int(raw.get("page_blocks", 0))),
                            pagination_supported=bool(raw.get("pagination_supported", True)),
                            retry_pages=retry_pages,
                            page_failures=page_failures,
                        )
                    )
                completed = max(0, int(payload.get("completed", 0)))
                planned = max(completed + len(pending), int(payload.get("planned", 0)))
                if pending:
                    return IndexPlan(pending, completed, planned)
        except Exception:
            pass

    legacy = decode_resume(value)
    if legacy:
        saved_start, saved_end, saved_resume = legacy
        later = [(start, end) for start, end in default_windows if start > saved_end]
        completed = sum(1 for _, end in default_windows if end < saved_start)
        pending = [PendingWindow(saved_start, saved_end, saved_resume)]
        pending.extend(PendingWindow(start, end) for start, end in later)
        return IndexPlan(pending, completed, completed + len(pending))

    pending = [PendingWindow(start, end) for start, end in default_windows]
    return IndexPlan(pending, 0, len(pending))


def split_window(window: PendingWindow) -> list[PendingWindow]:
    start_dt = datetime.strptime(window.start, "%Y%m%d%H%M%S")
    end_dt = datetime.strptime(window.end, "%Y%m%d%H%M%S")
    duration = end_dt - start_dt

    if duration >= timedelta(days=60):
        chunk = timedelta(days=30)
    elif duration >= timedelta(days=8):
        chunk = timedelta(days=7)
    elif duration >= timedelta(days=2):
        chunk = timedelta(days=1)
    elif duration >= timedelta(hours=12):
        chunk = timedelta(hours=6)
    elif duration >= timedelta(hours=2):
        chunk = timedelta(hours=1)
    elif duration >= timedelta(minutes=30):
        chunk = timedelta(minutes=15)
    elif duration >= timedelta(minutes=10):
        chunk = timedelta(minutes=5)
    elif duration >= timedelta(minutes=2):
        chunk = timedelta(minutes=1)
    elif duration >= timedelta(seconds=30):
        chunk = timedelta(seconds=15)
    elif duration >= timedelta(seconds=10):
        chunk = timedelta(seconds=5)
    elif duration >= timedelta(seconds=2):
        chunk = timedelta(seconds=1)
    else:
        return []

    parts: list[PendingWindow] = []
    cursor = start_dt
    while cursor <= end_dt:
        part_end = min(end_dt, cursor + chunk - timedelta(seconds=1))
        parts.append(
            PendingWindow(
                start=cursor.strftime("%Y%m%d%H%M%S"),
                end=part_end.strftime("%Y%m%d%H%M%S"),
                page_size=max(100, window.page_size // 2) if window.page_size else 0,
                strategy=window.strategy,
                page_blocks=max(0, window.page_blocks),
                pagination_supported=window.pagination_supported,
            )
        )
        cursor = part_end + timedelta(seconds=1)
    return parts if len(parts) > 1 else []


def window_label(start: str, end: str) -> str:
    start_date = datetime.strptime(start, "%Y%m%d%H%M%S")
    end_date = datetime.strptime(end, "%Y%m%d%H%M%S")
    if start_date.date() == end_date.date():
        if start_date.hour == 0 and start_date.minute == 0 and end_date.hour == 23 and end_date.minute == 59:
            return start_date.strftime("%Y-%m-%d")
        if start_date.hour == end_date.hour and start_date.minute == end_date.minute:
            return f"{start_date:%Y-%m-%d %H:%M:%S}–{end_date:%H:%M:%S}"
        return f"{start_date:%Y-%m-%d %H:%M}–{end_date:%H:%M}"
    if start_date.day == 1 and end_date.month == start_date.month:
        last_day = calendar.monthrange(start_date.year, start_date.month)[1]
        if end_date.day == last_day:
            return start_date.strftime("%Y-%m")
    if start_date.month == 1 and start_date.day == 1 and end_date.month == 12 and end_date.day == 31:
        return start_date.strftime("%Y")
    return f"{start_date:%Y-%m-%d}–{end_date:%Y-%m-%d}"


def transient_backoff(config: ProjectConfig, failures: int) -> float:
    network = config.network.normalized()
    base = min(network.retry_max_seconds, network.retry_base_seconds * (2 ** min(max(0, failures - 1), 6)))
    return base * random.uniform(0.85, 1.15)


def save_state(
    database: sqlite3.Connection,
    target_id: int,
    year: int,
    signature: str,
    resume_key: str | None,
    complete: bool,
    seen: int,
    error_id: int | None,
) -> None:
    database.execute(
        """
        INSERT INTO index_state(target_id,year,query_signature,resume_key,complete,seen,error_id,updated_at)
        VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(target_id,year,query_signature) DO UPDATE SET
            resume_key=excluded.resume_key,
            complete=excluded.complete,
            seen=excluded.seen,
            error_id=excluded.error_id,
            updated_at=excluded.updated_at
        """,
        (target_id, year, signature, resume_key, int(complete), seen, error_id, utc_now()),
    )


def _adopt_compatible_index_state(
    database: sqlite3.Connection,
    target_id: int,
    year: int,
    config: ProjectConfig,
    signature: str,
) -> None:
    """Adopt earlier state whose only signature difference is page size."""
    existing = database.execute(
        "SELECT 1 FROM index_state WHERE target_id=? AND year=? AND query_signature=?",
        (target_id, year, signature),
    ).fetchone()
    if existing:
        return
    start, end = cdx_year_window(config, year) or (f"{year:04d}0101000000", f"{year:04d}1231235959")
    for candidate in cdx_query_signatures(config):
        if candidate == signature:
            continue
        state = database.execute(
            "SELECT resume_key,complete,seen,error_id,updated_at FROM index_state "
            "WHERE target_id=? AND year=? AND query_signature=?",
            (target_id, year, candidate),
        ).fetchone()
        if not state:
            continue
        # There is no current state row, so most updates are conflict-free.
        # UPDATE OR IGNORE protects projects that already contain a handful of
        # rows under both signatures after an interrupted upgrade.
        database.execute(
            "UPDATE OR IGNORE captures SET query_signature=? "
            "WHERE target_id=? AND query_signature=? AND timestamp BETWEEN ? AND ?",
            (signature, target_id, candidate, start, end),
        )
        database.execute(
            "DELETE FROM captures WHERE target_id=? AND query_signature=? AND timestamp BETWEEN ? AND ?",
            (target_id, candidate, start, end),
        )
        database.execute(
            "INSERT INTO index_state(target_id,year,query_signature,resume_key,complete,seen,error_id,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (target_id, year, signature, state["resume_key"], state["complete"], state["seen"], state["error_id"], state["updated_at"]),
        )
        database.execute(
            "DELETE FROM index_state WHERE target_id=? AND year=? AND query_signature=?",
            (target_id, year, candidate),
        )
        return


def _record_network_event(database: sqlite3.Connection, stage: str, message: str, details: dict | None = None) -> None:
    try:
        database.execute(
            "INSERT INTO network_events(stage,message,details_json,created_at) VALUES(?,?,?,?)",
            (stage, message, json.dumps(details or {}, ensure_ascii=False, sort_keys=True), utc_now()),
        )
    except sqlite3.OperationalError:
        pass


def _defer_transient_window(
    config: ProjectConfig,
    database: sqlite3.Connection,
    plan: IndexPlan,
    current: PendingWindow,
    target_id: int,
    year: int,
    signature: str,
    seen: int,
    error_id: int | None,
    exc: BaseException,
    callback: Callable[[ProgressEvent], None] | None,
    completed_windows: int,
    total_windows: int,
    stop_event: threading.Event,
) -> int:
    current.failures += 1
    current.page_size = max(100, (current.page_size or config.page_size) // 2)
    if current.strategy != "paged":
        current.page_blocks = max(1, current.page_blocks // 2)
    message = f"{type(exc).__name__}: {exc}"
    network = config.network.normalized()
    with database:
        record_recovery_event(
            database, "index", "transient_index_delay",
            f"{window_label(current.start, current.end)}: {message}",
            details={"failures": current.failures, "strategy": current.strategy},
        )
        _record_network_event(
            database,
            "index",
            message,
            {
                "window_start": current.start,
                "window_end": current.end,
                "strategy": current.strategy,
                "failures": current.failures,
                "page": current.page,
                "page_count": current.page_count,
                "retry_pages": current.retry_pages[:100],
            },
        )
        if len(plan.pending) > 1:
            plan.pending.append(plan.pending.pop(0))
        save_state(database, target_id, year, signature, encode_plan(plan), False, seen, error_id)

    if not network.persistent_retries and current.failures >= max(3, config.retries):
        raise TransientRequestError(
            f"transient retry limit reached for {window_label(current.start, current.end)}: {exc}",
            timed_out=is_timeout_error(exc),
            splittable=False,
        ) from exc

    threshold = network.failure_pause_threshold
    if plan.pending and all(item.failures >= threshold for item in plan.pending):
        raise ConnectivityPaused(
            "Wayback could not answer any remaining CDX work after multiple independent connection methods. "
            "Archive Scout saved the exact queue and paused cleanly; Resume will continue from this point."
        ) from exc

    if len(plan.pending) > 1:
        emit(
            callback,
            ProgressEvent(
                "index",
                f"Wayback did not answer {window_label(current.start, current.end)}. Progress was saved and this work moved behind the remaining queue.",
                completed_windows,
                total_windows,
            ),
        )
        return error_id

    if current.failures >= threshold:
        raise ConnectivityPaused(
            f"Wayback remained unreachable for {window_label(current.start, current.end)} after {current.failures} recovery cycles. "
            "The queue was saved without marking the project failed."
        ) from exc

    wait_seconds = transient_backoff(config, current.failures)
    emit(
        callback,
        ProgressEvent(
            "index",
            f"Wayback is temporarily unavailable. Archive Scout remains active and will retry in {wait_seconds:.1f}s (attempt {current.failures + 1}).",
            completed_windows,
            total_windows,
        ),
    )
    stop_event.wait(wait_seconds)
    if stop_event.is_set():
        raise Stopped
    return error_id


def _client_for_config(
    config: ProjectConfig,
    stop_event: threading.Event,
    callback: Callable[[ProgressEvent], None] | None,
) -> HttpClient:
    network = config.network.normalized()
    limiter = SharedFixedRateLimiter(config.cdx_delay)
    host_gate = shared_host_gate(config.rate_limit_base_pause, config.rate_limit_max_pause)

    def on_retry(attempt: int, total: int, reason: str, wait_seconds: float) -> None:
        if "all Wayback requests paused" in reason:
            limit = f"/{total}" if total else ""
            message = f"{reason}. Shared pause {attempt}{limit} for {wait_seconds:.1f}s; one recovery probe will run next…"
            stage = "rate_limit"
        elif wait_seconds <= 0:
            message = reason
            stage = "network"
        else:
            message = f"CDX request failed ({reason}). Retrying attempt {attempt}/{total} in {wait_seconds:.1f}s…"
            stage = "index"
        emit(callback, ProgressEvent(stage, message))

    def on_network(message: str) -> None:
        emit(callback, ProgressEvent("network", message))

    return HttpClient(
        limiter,
        1,
        min(max(config.read_timeout, 30.0), 120.0),
        config.user_agent,
        stop_event,
        retry_callback=on_retry,
        connect_timeout=min(max(config.connect_timeout, 5.0), 15.0),
        read_timeout=min(max(config.read_timeout, 30.0), 120.0),
        pool_size=network.cdx_workers,
        host_gate=host_gate,
        rate_limit_attempts=config.rate_limit_attempts,
        rate_limit_max_wait=config.rate_limit_max_wait,
        network_backend=network.backend,
        trust_environment=network.trust_environment,
        network_callback=on_network,
    )


def _resolve_strategy(current: PendingWindow, config: ProjectConfig, target: str) -> None:
    desired = preferred_index_strategy(config, target)
    if current.strategy not in {"resume", "paged"}:
        current.strategy = desired
    elif desired == "resume" and current.strategy == "paged":
        # Safely abandon an unfinished v1.0.2 numbered-page queue. Previously
        # indexed rows remain in SQLite and no-op upserts make replaying the
        # beginning of this date window cheap; continuing thousands of page
        # numbers can be orders of magnitude slower.
        current.strategy = "resume"
        # Resume-key traversal still supports adaptive date-window splitting on
        # transient oversized/timeout responses; only the old numbered queue is
        # being discarded here.
        current.pagination_supported = True
        current.page = 0
        current.page_count = -1
        current.retry_pages.clear()
        current.page_failures.clear()
        current.resume_key = None
    if not current.pagination_supported and current.strategy == "paged":
        current.strategy = "resume"
    network = config.network.normalized()
    if network.index_strategy == "auto" and current.strategy == "paged":
        # Auto mode is the fixed reference-downloader profile.  Do not let a
        # legacy/custom numbered-page block value silently turn the ten-worker
        # Timemap pipeline back into large, low-concurrency page bodies.
        current.page_blocks = 9
    elif current.page_blocks <= 0:
        current.page_blocks = network.page_blocks


def cdx_response_budget(page_size: int) -> int:
    """Bound a large CDX response without penalizing normal 100k batches."""
    size = max(100, int(page_size))
    return min(256 * 1024 * 1024, max(64 * 1024 * 1024, size * 1536))


def _select_page_batch(current: PendingWindow, workers: int) -> tuple[list[int], int]:
    workers = max(1, int(workers))
    retry_pages = sorted({page for page in current.retry_pages if 0 <= page < current.page_count})
    pages: list[int] = []
    next_page = max(0, current.page)
    new_pages_remain = next_page < current.page_count
    retry_quota = workers if not new_pages_remain else min(len(retry_pages), max(1, workers // 2))
    pages.extend(retry_pages[:retry_quota])
    while len(pages) < workers and next_page < current.page_count:
        if next_page not in retry_pages:
            pages.append(next_page)
        next_page += 1
    if len(pages) < workers:
        for page in retry_pages[retry_quota:]:
            if page not in pages:
                pages.append(page)
            if len(pages) >= workers:
                break
    return pages, next_page


def _request_paged_count(
    client: HttpClient,
    endpoints: tuple[str, ...],
    params: list[tuple[str, str]],
    config: ProjectConfig,
    stop_event: threading.Event,
) -> int:
    """Retry the small native-JSON page count before abandoning Timemap.

    The reference downloader gives every Timemap request five attempts.  A
    single count timeout previously pushed Archive Scout into its much slower
    resume-window fallback immediately.
    """
    attempts = max(PAGED_REQUEST_ATTEMPTS, int(config.retries))
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        if stop_event.is_set():
            raise Stopped
        try:
            payload = request_cdx_json_payload(
                client,
                endpoints,
                params,
                max_bytes=1024 * 1024,
            )
            return parse_num_pages(payload)
        except (RateLimitDeferred, PermanentRequestError, Stopped):
            raise
        except TransientRequestError as exc:
            last_error = exc
            # One connection_failed result already represents a complete pass
            # through the configured independent transports. Hand it back to
            # the operation-wide connection circuit instead of multiplying a
            # DNS/proxy/TLS outage by five more multi-backend passes.
            if exc.connection_failed or exc.timed_out:
                raise
            if attempt >= attempts:
                raise
            retry_callback = getattr(client, "retry_callback", None)
            if retry_callback:
                retry_callback(
                    attempt + 1,
                    attempts,
                    "Timemap page-count request failed; retrying native JSON",
                    0.0,
                )
    if last_error is not None:
        raise last_error
    raise TransientRequestError("Timemap page-count request failed", splittable=True)


def _request_paged_batch(
    client: HttpClient,
    config: ProjectConfig,
    target: str,
    current: PendingWindow,
    stop_event: threading.Event,
    consume_success: Callable[[PageFetchResult], None] | None = None,
    completed_pages: set[int] | None = None,
) -> PagedBatch:
    endpoints = cdx_paged_endpoints(config)
    network = config.network.normalized()
    if current.page_count < 0:
        current.page_count = _request_paged_count(
            client,
            endpoints,
            build_num_pages_params(config, target, current.start, current.end, current.page_blocks),
            config,
            stop_event,
        )
        current.page = min(current.page, current.page_count)
        current.retry_pages = [page for page in current.retry_pages if page < current.page_count]
    if current.page >= current.page_count and not current.retry_pages:
        return PagedBatch([], [], True)

    page_workers = effective_page_workers(network.cdx_workers, current.page_blocks)
    # Keep a long rolling queue behind the worker pool, just like the reference
    # downloader's 1,000-page task chunks. A single slow Timemap page no longer
    # creates a barrier that leaves the other nine workers idle.
    pages, next_page = _select_page_batch(current, max(page_workers, PAGED_PIPELINE_PAGES))
    completed_pages = completed_pages or set()
    requested_pages = [page for page in pages if page not in completed_pages]
    if not requested_pages:
        current.page = next_page
        current.retry_pages = [page for page in current.retry_pages if page not in completed_pages]
        finished = current.page >= current.page_count and not current.retry_pages
        return PagedBatch([], pages, finished)
    results: list[PageFetchResult] = []
    for result in iter_cdx_pages(
        client,
        endpoints,
        requested_pages,
        lambda page: build_paged_cdx_params(config, target, current.start, current.end, page, current.page_blocks),
        stop_event,
        workers=page_workers,
        # Server-sized pages can contain far more ZipNum blocks than the old
        # pageSize=9 transport. Keep a generous per-response safety ceiling while
        # concurrency is independently capped by effective_page_workers().
        max_bytes=(192 * 1024 * 1024 if current.page_blocks <= 0 else max(64 * 1024 * 1024, current.page_blocks * 12 * 1024 * 1024)),
        prefer_text=False,
        json_only=True,
    ):
        if result.succeeded and consume_success is not None:
            consume_success(result)
        results.append(result)
    current.page = next_page
    retry_set = set(current.retry_pages)
    for result in results:
        if result.succeeded:
            retry_set.discard(result.page)
            current.page_failures.pop(result.page, None)
        else:
            retry_set.add(result.page)
            current.page_failures[result.page] = current.page_failures.get(result.page, 0) + 1
    current.retry_pages = sorted(retry_set)
    finished = current.page >= current.page_count and not current.retry_pages
    return PagedBatch(results, pages, finished)


def _request_resume(
    client: HttpClient,
    config: ProjectConfig,
    target: str,
    current: PendingWindow,
) -> tuple[list[tuple[str, str, str, str, str, str]], bool]:
    page_size = current.page_size or config.page_size
    result = request_cdx_rows(
        client,
        cdx_endpoints(config),
        build_cdx_params(config, target, current.start, current.end, current.resume_key, page_size=page_size),
        max_bytes=cdx_response_budget(page_size),
        prefer_text=True,
    )
    rows, next_resume = result.rows, result.resume_key
    if next_resume:
        if next_resume == current.resume_key:
            raise TransientRequestError("CDX returned the same resume key twice", splittable=True)
        current.resume_key = next_resume
        return rows, False
    return rows, True


def _paged_failure_error(failures: list[PageFetchResult]) -> BaseException:
    if not failures:
        return TransientRequestError("unknown paged CDX failure", splittable=False)
    return failures[0].error or TransientRequestError("unknown paged CDX failure", splittable=False)


def _is_pagination_unavailable(exc: BaseException) -> bool:
    message = str(exc)
    return isinstance(exc, RuntimeError) and ("HTTP 400" in message or "page-count" in message)


def _is_permanent_page_error(exc: BaseException) -> bool:
    if isinstance(exc, (TransientRequestError, RateLimitDeferred)):
        return False
    if _is_pagination_unavailable(exc):
        return False
    return isinstance(exc, RuntimeError)


def index_archive(
    config: ProjectConfig,
    database: sqlite3.Connection,
    stop_event: threading.Event,
    callback: Callable[[ProgressEvent], None] | None = None,
) -> None:
    config = config.normalized()
    client = _client_for_config(config, stop_event, callback)
    if not config.cdx_collapses:
        emit(callback, ProgressEvent("index", "Warning: no CDX collapse is selected. Every archived snapshot may be returned."))

    tasks: list[tuple[str, ProjectConfig, int, list[tuple[str, str]], str]] = []
    for target in config.targets:
        target_config = config.for_target(target)
        signature = cdx_query_signature(target_config)
        for year in range(target_config.from_year, target_config.to_year + 1):
            windows = index_windows(target_config, target, year)
            if windows:
                tasks.append((target, target_config, year, windows, signature))

    total_windows = sum(len(windows) for _, _, _, windows, _ in tasks)
    completed_windows = 0
    connection_failure_streak = 0
    transient_failure_streak = 0
    excluded_targets: set[str] = set()
    try:
        for target, target_config, year, default_windows, signature in tasks:
            if stop_event.is_set():
                raise Stopped
            target_id = get_or_create_target(database, target, target_config.settings_for_target(target))
            if target in excluded_targets:
                with database:
                    save_state(database, target_id, year, signature, None, True, 0, None)
                completed_windows += len(default_windows)
                emit(
                    callback,
                    ProgressEvent(
                        "site_issue",
                        f"Skipping {target} for {year}: Wayback already reported this target as excluded.",
                        completed_windows,
                        total_windows,
                    ),
                )
                continue
            with database:
                _adopt_compatible_index_state(database, target_id, year, target_config, signature)
            state = database.execute(
                "SELECT resume_key,complete,seen,error_id FROM index_state WHERE target_id=? AND year=? AND query_signature=?",
                (target_id, year, signature),
            ).fetchone()
            if state and state["complete"]:
                completed_windows += len(default_windows)
                emit(callback, ProgressEvent("index", f"Already indexed {target} for {year}", completed_windows, total_windows))
                continue

            seen = int(state["seen"] or 0) if state else 0
            error_id = int(state["error_id"]) if state and state["error_id"] else None
            plan = decode_plan(state["resume_key"] if state else None, default_windows)
            completed_windows += plan.completed
            total_windows += max(0, plan.planned - len(default_windows))

            while plan.pending:
                if stop_event.is_set():
                    with database:
                        save_state(database, target_id, year, signature, encode_plan(plan), False, seen, error_id)
                    raise Stopped
                current = plan.pending[0]
                _resolve_strategy(current, target_config, target)
                label = window_label(current.start, current.end)
                if current.strategy == "paged" and current.page_count >= 0:
                    detail = f"pages {current.page:,}/{current.page_count:,}; {len(current.retry_pages)} retry"
                else:
                    detail = current.strategy
                emit(callback, ProgressEvent("index", f"Indexing {target} for {label} ({detail})…", completed_windows, total_windows))
                request_started = time.monotonic()
                try:
                    if current.strategy == "paged":
                        received = 0
                        changed = 0
                        write_seconds = 0.0
                        batch_pages_done = 0
                        last_page_progress = time.monotonic()

                        completed_pages = {
                            int(row[0]) for row in database.execute(
                                """SELECT page FROM index_pages WHERE query_signature=? AND target_id=?
                                   AND window_start=? AND window_end=? AND status='complete'""",
                                (signature, target_id, current.start, current.end),
                            )
                        }

                        def store_completed_page(result: PageFetchResult) -> None:
                            nonlocal received, changed, write_seconds, batch_pages_done, last_page_progress
                            page_received = len(result.rows)
                            write_started = time.monotonic()
                            # Capture rows and their page checkpoint are one
                            # transaction: after a power loss the page is either
                            # wholly eligible for retry or already known complete.
                            with database:
                                changed += upsert_captures(database, result.rows, target_id, signature)
                                database.execute(
                                    """INSERT INTO index_pages(query_signature,target_id,window_start,window_end,page,row_count,status,updated_at)
                                       VALUES(?,?,?,?,?,?,'complete',?)
                                       ON CONFLICT(query_signature,target_id,window_start,window_end,page) DO UPDATE SET
                                       row_count=excluded.row_count,status='complete',updated_at=excluded.updated_at""",
                                    (signature, target_id, current.start, current.end, int(result.page), page_received, utc_now()),
                                )
                            completed_pages.add(int(result.page))
                            write_seconds += time.monotonic() - write_started
                            received += page_received
                            batch_pages_done += 1
                            now = time.monotonic()
                            if (
                                now - last_page_progress >= 1.0
                                or len(completed_pages) >= current.page_count
                            ):
                                emit(
                                    callback,
                                    ProgressEvent(
                                        "index",
                                        f"{target} {label}: completed {len(completed_pages):,}/{current.page_count:,} Timemap pages; "
                                        f"this block finished {batch_pages_done:,} pages and received {received:,} captures",
                                        completed_windows,
                                        total_windows,
                                    ),
                                )
                                last_page_progress = now
                            # Release the largest object while sibling requests
                            # are still in flight instead of retaining a full
                            # worker batch in memory.
                            result.rows.clear()

                        batch = _request_paged_batch(
                            client, target_config, target, current, stop_event, store_completed_page, completed_pages
                        )
                        request_seconds = max(0.0, time.monotonic() - request_started - write_seconds)
                        successes = batch.successful
                        failures = batch.failed
                        with database:
                            seen += received
                            if successes:
                                current.failures = 0
                                connection_failure_streak = 0
                                transient_failure_streak = 0
                            if batch.finished:
                                plan.pending.pop(0)
                                plan.completed += 1
                                completed_windows += 1
                            complete = not plan.pending
                            save_state(database, target_id, year, signature, encode_plan(plan), complete, seen, None if complete else error_id)
                            if error_id and not failures:
                                database.execute("UPDATE errors SET resolved=1,last_seen=? WHERE id=?", (utc_now(), error_id))
                                error_id = None
                        pages_done = len(successes)
                        emit(
                            callback,
                            ProgressEvent(
                                "index",
                                f"{target} {label}: {pages_done}/{len(batch.requested_pages)} pages, received {received:,}, stored {changed:,}, seen {seen:,} — network {request_seconds:.1f}s, database {write_seconds:.2f}s",
                                completed_windows,
                                total_windows,
                            ),
                        )
                        if not failures:
                            continue

                        failure_exc = _paged_failure_error(failures)
                        if not successes and all(
                            isinstance(item.error, TransientRequestError) and item.error.connection_failed
                            for item in failures
                        ):
                            # Page workers return failures as results so successful
                            # siblings can still be committed. A complete connection
                            # failure must nevertheless enter the operation-wide
                            # connection circuit instead of being mistaken for one
                            # repeatedly slow CDX page.
                            raise failure_exc
                        if not successes and _is_pagination_unavailable(failure_exc):
                            current.pagination_supported = False
                            current.strategy = "resume"
                            current.page = 0
                            current.page_count = -1
                            current.retry_pages.clear()
                            current.page_failures.clear()
                            with database:
                                save_state(database, target_id, year, signature, encode_plan(plan), False, seen, error_id)
                            emit(callback, ProgressEvent("index", f"Paged CDX is unavailable for {target} {label}; continuing with resume keys.", completed_windows, total_windows))
                            continue
                        permanent = next((item.error for item in failures if item.error and _is_permanent_page_error(item.error)), None)
                        if permanent is not None:
                            raise permanent
                        highest_page_failures = max(current.page_failures.values(), default=0)
                        new_pages_remain = current.page < current.page_count
                        if highest_page_failures >= PAGED_PAGE_FAILURE_LIMIT and (
                            not successes or not new_pages_remain
                        ):
                            # Keep the exact failed page numbers. Converting a
                            # nearly completed Timemap year into broad resume-key
                            # windows repeated successful work and was the main
                            # source of post-v1.0.5 indexing stalls.
                            with database:
                                error_id = record_error(
                                    database,
                                    "index",
                                    "timemap_pages_unavailable",
                                    f"{target} {label}: {len(current.retry_pages)} Timemap page(s) remained unavailable "
                                    f"after {highest_page_failures} attempts: {failure_exc}",
                                    retryable=True,
                                )
                                record_recovery_event(
                                    database,
                                    "index",
                                    "timemap_page_queue_saved",
                                    f"{target} {label}: saved only the failed Timemap pages for Resume.",
                                    details={"pages": current.retry_pages[:100], "attempts": highest_page_failures},
                                )
                                save_state(database, target_id, year, signature, encode_plan(plan), False, seen, error_id)
                            raise ConnectivityPaused(
                                f"{len(current.retry_pages)} Timemap page(s) remained unavailable after "
                                f"{highest_page_failures} attempts. Successful pages were preserved and only the exact "
                                "failed page queue was saved for Resume."
                            ) from failure_exc
                        with database:
                            record_recovery_event(
                                database, "index", "transient_page_retry",
                                f"{target} {label}: {len(failures)} CDX page(s) requeued: {failure_exc}",
                                details={"pages": [item.page for item in failures]},
                            )
                            _record_network_event(
                                database,
                                "index_page",
                                str(failure_exc),
                                {"pages": [item.page for item in failures], "retry_pages": current.retry_pages[:100]},
                            )
                            save_state(database, target_id, year, signature, encode_plan(plan), False, seen, error_id)
                        if successes:
                            emit(callback, ProgressEvent("index", f"Requeued {len(failures)} slow page(s) while continuing with untouched pages.", completed_windows, total_windows))
                            continue
                        if completed_pages and not new_pages_remain:
                            emit(
                                callback,
                                ProgressEvent(
                                    "index",
                                    f"Retrying {len(current.retry_pages)} isolated Timemap page(s); all successful pages remain checkpointed.",
                                    completed_windows,
                                    total_windows,
                                ),
                            )
                            continue
                        error_id = _defer_transient_window(
                            target_config, database, plan, current, target_id, year, signature, seen, error_id,
                            failure_exc, callback, completed_windows, total_windows, stop_event,
                        )
                        continue

                    rows, finished = _request_resume(client, target_config, target, current)
                    connection_failure_streak = 0
                    transient_failure_streak = 0
                    request_seconds = time.monotonic() - request_started
                    received = len(rows)
                    write_started = time.monotonic()
                    with database:
                        changed = upsert_captures(database, rows, target_id, signature)
                        seen += received
                        current.failures = 0
                        if finished:
                            plan.pending.pop(0)
                            plan.completed += 1
                            completed_windows += 1
                        complete = not plan.pending
                        save_state(database, target_id, year, signature, encode_plan(plan), complete, seen, None if complete else error_id)
                        if error_id:
                            database.execute("UPDATE errors SET resolved=1,last_seen=? WHERE id=?", (utc_now(), error_id))
                            error_id = None
                    write_seconds = time.monotonic() - write_started
                    emit(
                        callback,
                        ProgressEvent(
                            "index",
                            f"{target} {label}: received {received:,}, stored {changed:,}, seen {seen:,} — network {request_seconds:.1f}s, database {write_seconds:.2f}s",
                            completed_windows,
                            total_windows,
                        ),
                    )
                    # Do not retain the previous 50k-row page while the next
                    # response is being downloaded and parsed. Python evaluates
                    # the next assignment's right-hand side before releasing the
                    # old local value, which otherwise briefly doubles peak memory.
                    rows.clear()
                except Stopped:
                    with database:
                        save_state(database, target_id, year, signature, encode_plan(plan), False, seen, error_id)
                    raise
                except RateLimitDeferred as exc:
                    error_id = _defer_transient_window(
                        target_config, database, plan, current, target_id, year, signature, seen, error_id,
                        exc, callback, completed_windows, total_windows, stop_event,
                    )
                except TransientRequestError as exc:
                    if exc.connection_failed:
                        connection_failure_streak += 1
                        current.failures += 1
                        network = target_config.network.normalized()
                        with database:
                            if connection_failure_streak >= network.connection_failure_pause_threshold:
                                error_id = record_error(
                                    database, "index", "wayback_connection_unavailable",
                                    f"{target} {label}: {exc}", retryable=True,
                                )
                            else:
                                record_recovery_event(
                                    database, "index", "connection_retry", f"{target} {label}: {exc}",
                                    details={"streak": connection_failure_streak},
                                )
                            _record_network_event(
                                database,
                                "connection",
                                str(exc),
                                {"streak": connection_failure_streak, "target": target, "window": label},
                            )
                            save_state(database, target_id, year, signature, encode_plan(plan), False, seen, error_id)
                        if connection_failure_streak >= network.connection_failure_pause_threshold:
                            raise ConnectivityPaused(
                                f"Archive Scout could not establish a Wayback connection after "
                                f"{connection_failure_streak} complete multi-backend attempts. "
                                "The exact index queue was saved; Resume will continue without repeating completed pages."
                            ) from exc
                        wait_seconds = min(
                            15.0,
                            network.connection_retry_seconds * (2 ** max(0, connection_failure_streak - 1)),
                        )
                        emit(
                            callback,
                            ProgressEvent(
                                "network",
                                f"Wayback connection setup failed. Retrying the same saved request in {wait_seconds:.1f}s "
                                f"({connection_failure_streak}/{network.connection_failure_pause_threshold})…",
                                completed_windows,
                                total_windows,
                            ),
                        )
                        stop_event.wait(wait_seconds)
                        if stop_event.is_set():
                            raise Stopped
                        continue
                    transient_failure_streak += 1
                    network = target_config.network.normalized()
                    no_progress_limit = min(
                        network.failure_pause_threshold,
                        4 if exc.timed_out else 6,
                    )
                    if transient_failure_streak >= no_progress_limit:
                        current.failures += 1
                        with database:
                            error_id = record_error(
                                database,
                                "index",
                                "transient_index_delay",
                                f"{target} {label}: {transient_failure_streak} consecutive transient CDX failures without a successful response: {exc}",
                                retryable=True,
                            )
                            _record_network_event(
                                database,
                                "no_progress",
                                str(exc),
                                {"streak": transient_failure_streak, "target": target, "window": label},
                            )
                            save_state(database, target_id, year, signature, encode_plan(plan), False, seen, error_id)
                        raise ConnectivityPaused(
                            f"Wayback returned no usable CDX response after {transient_failure_streak} consecutive recovery attempts. "
                            "Archive Scout saved the exact queue and paused instead of looping indefinitely."
                        ) from exc
                    if current.strategy == "paged" and current.page_count < 0:
                        # A page-count request should be cheap. If it cannot be
                        # obtained, do not loop on it indefinitely: switch this
                        # window to resume-key retrieval and let ordinary window
                        # subdivision take over if the broad request is still too
                        # expensive for Wayback.
                        current.strategy = "resume"
                        current.pagination_supported = False
                        current.page = 0
                        current.page_count = -1
                        current.retry_pages.clear()
                        current.page_failures.clear()
                        parts = split_window(current) if exc.splittable else []
                        if parts:
                            for part in parts:
                                part.strategy = "resume"
                                part.pagination_supported = False
                            plan.pending[0:1] = parts
                            added = len(parts) - 1
                            plan.planned += added
                            total_windows += added
                        with database:
                            save_state(database, target_id, year, signature, encode_plan(plan), False, seen, error_id)
                        emit(callback, ProgressEvent("index", f"Paged CDX could not count {target} {label}; continuing with resumable smaller windows.", completed_windows, total_windows))
                        continue
                    if current.strategy == "resume" and current.pagination_supported:
                        parts = split_window(current) if exc.splittable else []
                        if parts:
                            plan.pending[0:1] = parts
                            added = len(parts) - 1
                            plan.planned += added
                            total_windows += added
                            with database:
                                save_state(database, target_id, year, signature, encode_plan(plan), False, seen, error_id)
                            emit(callback, ProgressEvent("index", f"CDX did not answer {target} {label}; split into {len(parts)} smaller windows.", completed_windows, total_windows))
                            continue
                        if preferred_index_strategy(target_config, target) == "paged":
                            current.strategy = "paged"
                            current.page = 0
                            current.page_count = -1
                            current.resume_key = None
                            current.retry_pages.clear()
                            current.page_failures.clear()
                            with database:
                                save_state(database, target_id, year, signature, encode_plan(plan), False, seen, error_id)
                            emit(callback, ProgressEvent("index", f"Resume-key indexing remained slow for {target} {label}; switching this window to paged CDX indexing.", completed_windows, total_windows))
                            continue
                    error_id = _defer_transient_window(
                        target_config, database, plan, current, target_id, year, signature, seen, error_id,
                        exc, callback, completed_windows, total_windows, stop_event,
                    )
                except RuntimeError as exc:
                    if isinstance(exc, PermanentRequestError) and exc.category in {"wayback_excluded", "wayback_forbidden"}:
                        category = exc.category
                        message = site_issue_message(category, target, "CDX indexing", exc.status)
                        remaining = len(plan.pending)
                        plan.pending.clear()
                        plan.completed += remaining
                        completed_windows += remaining
                        if category == "wayback_excluded":
                            excluded_targets.add(target)
                        with database:
                            error_id = record_error(
                                database,
                                "index",
                                category,
                                f"{target} {label}: {exc}",
                                http_status=exc.status,
                                retryable=False,
                            )
                            record_site_issue(
                                database,
                                host_from_url(target),
                                "cdx_index",
                                category,
                                message,
                                target=target,
                                http_status=exc.status,
                            )
                            save_state(database, target_id, year, signature, None, True, seen, error_id)
                        emit(callback, ProgressEvent("site_issue", message, completed_windows, total_windows))
                        continue
                    if current.strategy == "paged" and _is_pagination_unavailable(exc):
                        current.pagination_supported = False
                        current.strategy = "resume"
                        current.page = 0
                        current.page_count = -1
                        current.retry_pages.clear()
                        current.page_failures.clear()
                        with database:
                            save_state(database, target_id, year, signature, encode_plan(plan), False, seen, error_id)
                        emit(callback, ProgressEvent("index", f"Paged CDX is unavailable for {target} {label}; continuing with resume keys.", completed_windows, total_windows))
                        continue
                    message = f"{target} {label}: {type(exc).__name__}: {exc}"
                    with database:
                        error_id = record_error(database, "index", "index_failure", message, retryable=False)
                        save_state(database, target_id, year, signature, encode_plan(plan), False, seen, error_id)
                    emit(callback, ProgressEvent("index", f"Indexing stopped on a permanent configuration or local-data error for {target} {label}. Progress was saved.", completed_windows, total_windows))
                    raise
                except Exception as exc:
                    # Programming, parsing, SQLite, and local filesystem errors
                    # are not network retries. Requeueing them forever hides the
                    # real defect and can make the interface appear stuck.
                    with database:
                        error_id = record_error(
                            database,
                            "index",
                            "unexpected_index_error",
                            f"{target} {label}: {type(exc).__name__}: {exc}",
                            retryable=False,
                        )
                        save_state(database, target_id, year, signature, encode_plan(plan), False, seen, error_id)
                    raise
            emit(callback, ProgressEvent("index", f"Finished {target} for {year}", completed_windows, total_windows))
    finally:
        client.close()
