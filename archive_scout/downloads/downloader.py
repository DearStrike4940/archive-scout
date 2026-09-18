from __future__ import annotations

import concurrent.futures
import hashlib
import os
import sqlite3
import threading
import time
import urllib.parse
from collections import deque
from pathlib import Path
from typing import Callable, Iterator

from ..cdx.client import HttpClient, RateLimitDeferred
from ..cdx.parameters import cdx_query_signature
from ..config import ProjectConfig
from ..constants import REPLAY_URL
from ..content import (
    CHARSET_PATTERN,
    classify_replay_content,
    classify_text_candidate,
    decode_bytes,
    decode_bytes_with_encoding,
    looks_textual_bytes,
    parse_page,
)
from ..database.repositories import (
    mark_capture_skipped,
    record_error,
    record_site_issue,
    requeue_reclassifiable_skips,
    resolve_errors,
    save_match,
    upsert_document,
)
from ..events import ProgressEvent, Stopped
from ..parsing.embeds import extract_embed_candidates_fast
from ..site_status import host_from_url, should_surface_site_issue, site_issue_message
from ..scanning.jobs import ScanJob
from ..scanning.keywords import compile_prefilter
from ..scanning.scoring import analyze_content, prepare_analysis_fields
from ..storage import capture_path as url_capture_path, sha256_file
from ..utils import hash_text, normalize_search, utc_now
from .rate_limit import SharedFixedRateLimiter, shared_host_gate
from .validation import classify_exception

CLASSIFIER_REVISION = 2


def replay_url(timestamp: str, original: str, modifier: str = "id_") -> str:
    encoded = urllib.parse.quote(original, safe=":/?&=#%+;,[]@!$'()*")
    clean_modifier = modifier if modifier in {"id_", "if_", "oe_"} else "id_"
    return f"{REPLAY_URL}/{timestamp}{clean_modifier}/{encoded}"


def capture_path(root: Path, capture_id: int, timestamp: str, original: str) -> Path:
    """Compatibility wrapper using the v1.0.6 URL-derived filename policy."""
    del capture_id
    return url_capture_path(root, timestamp, original)


def _allocate_capture_path(database: sqlite3.Connection, root: Path, row: sqlite3.Row, reserved: set[str] | None = None) -> Path:
    existing = str(row["local_path"] or "") if "local_path" in row.keys() else ""
    if existing:
        return Path(existing)
    candidate = url_capture_path(root, str(row["timestamp"]), str(row["original_url"]))
    def occupied(path: Path) -> bool:
        return str(path) in (reserved or ()) or path.exists() or database.execute(
            "SELECT id FROM captures WHERE id<>? AND local_path=? LIMIT 1",
            (int(row["id"]), str(path)),
        ).fetchone() is not None

    if occupied(candidate):
        candidate = url_capture_path(root, str(row["timestamp"]), str(row["original_url"]), disambiguate=True)
        if occupied(candidate):
            # Escaping URL characters can collide with an already-percent-
            # escaped URL, even at the same timestamp. Never adopt another
            # capture's file just because the portable spelling is identical.
            base = url_capture_path(root, str(row["timestamp"]), str(row["original_url"]))
            counter = 0
            while True:
                suffix = f"~c{int(row['id'])}" + (f"-{counter}" if counter else "")
                candidate = base.with_name(base.stem + suffix + ".txt")
                if not occupied(candidate):
                    break
                counter += 1
    return candidate


def cumulative_download_progress(
    database: sqlite3.Connection,
    config: ProjectConfig,
    queued_total: int,
    capture_ids: list[int] | None = None,
) -> tuple[int, int]:
    if capture_ids:
        return 0, max(0, int(queued_total))
    signature = cdx_query_signature(config)
    total = int(database.execute(
        "SELECT COUNT(*) FROM captures WHERE query_signature=?", (signature,)
    ).fetchone()[0])
    unfinished = int(database.execute(
        """SELECT COUNT(*) FROM captures
           WHERE query_signature=? AND state IN ('pending','downloading','downloaded_unscanned','scanning')""",
        (signature,),
    ).fetchone()[0])
    return max(0, total - unfinished), total


def prepare_download_rows(
    database: sqlite3.Connection,
    config: ProjectConfig,
    patterns,
    states: tuple[str, ...] = ("pending",),
    capture_ids: list[int] | None = None,
) -> tuple[int, Iterator[sqlite3.Row]]:
    """Classify indexed captures and create a bounded SQLite-backed replay queue.

    Intentional non-text/URL-filter decisions are auditable skip reasons, never
    Open Errors. Ambiguous metadata is downloaded and sniffed rather than lost.
    """
    requeue_reclassifiable_skips(database, config.download_scope, CLASSIFIER_REVISION)
    database.execute("DROP TABLE IF EXISTS temp.archive_scout_download_queue")
    database.execute(
        """CREATE TEMP TABLE archive_scout_download_queue(
               id INTEGER PRIMARY KEY,
               priority INTEGER NOT NULL,
               length INTEGER NOT NULL
           ) WITHOUT ROWID"""
    )
    database.execute(
        "CREATE INDEX archive_scout_download_queue_order ON archive_scout_download_queue(priority,length,id)"
    )
    database.execute("DROP TABLE IF EXISTS temp.archive_scout_capture_selection")

    source = "captures c"
    clauses: list[str] = []
    params: list[object] = []
    if capture_ids:
        database.execute(
            "CREATE TEMP TABLE archive_scout_capture_selection(id INTEGER PRIMARY KEY) WITHOUT ROWID"
        )
        database.executemany(
            "INSERT OR IGNORE INTO archive_scout_capture_selection(id) VALUES(?)",
            ((int(value),) for value in capture_ids),
        )
        source += " JOIN archive_scout_capture_selection s ON s.id=c.id"
    else:
        clauses.extend(["c.query_signature=?", "c.download_attempts<?"])
        params.extend([cdx_query_signature(config), config.max_attempts])
    if states:
        clauses.append("c.state IN (" + ",".join("?" for _ in states) + ")")
        params.extend(states)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    cursor = database.execute(
        "SELECT c.id,c.original_url,c.mimetype,c.length FROM " + source + where + " ORDER BY c.id",
        params,
    )
    url_prefilter = compile_prefilter(patterns) if patterns else None
    while True:
        chunk = cursor.fetchmany(2000)
        if not chunk:
            break
        selected_ids: list[tuple[int, int, int]] = []
        with database:
            for row in chunk:
                capture_id = int(row["id"])
                classification = classify_text_candidate(
                    str(row["original_url"]), str(row["mimetype"] or "")
                )
                if classification == "binary":
                    mark_capture_skipped(database, capture_id, "known_non_text", CLASSIFIER_REVISION)
                    continue
                if config.download_scope == "keyword_urls" and url_prefilter is not None:
                    original_url = str(row["original_url"])
                    normalized_url = normalize_search(original_url)
                    if (
                        not url_prefilter.has_positive_rules
                        or not url_prefilter.matches(
                            {"url": original_url}, {"url": normalized_url}
                        )
                    ):
                        mark_capture_skipped(
                            database, capture_id, "url_keyword_filter", CLASSIFIER_REVISION
                        )
                        continue
                length = max(0, int(row["length"] or 0))
                selected_ids.append((capture_id, 1 if length <= 0 else 0, length))
            if selected_ids:
                database.executemany(
                    "INSERT OR IGNORE INTO archive_scout_download_queue(id,priority,length) VALUES(?,?,?)",
                    selected_ids,
                )
    total = int(database.execute(
        "SELECT COUNT(*) FROM archive_scout_download_queue"
    ).fetchone()[0])

    def iter_rows() -> Iterator[sqlite3.Row]:
        last_priority = -1
        last_length = -1
        last_id = 0
        while True:
            batch = database.execute(
                """
                SELECT c.* FROM captures c
                JOIN archive_scout_download_queue q ON q.id=c.id
                WHERE (q.priority,q.length,q.id)>(?,?,?)
                ORDER BY q.priority,q.length,q.id LIMIT 1000
                """,
                (last_priority, last_length, last_id),
            ).fetchall()
            if not batch:
                return
            for row in batch:
                length = max(0, int(row["length"] or 0))
                last_priority = 1 if length <= 0 else 0
                last_length = length
                last_id = int(row["id"])
                yield row

    return total, iter_rows()


def prepare_download_only_rows(
    database: sqlite3.Connection,
    config: ProjectConfig,
    states: tuple[str, ...] = ("pending",),
    capture_ids: list[int] | None = None,
) -> tuple[int, Iterator[sqlite3.Row], dict[str, int]]:
    """Stream download-only candidates without materializing a temporary queue.

    The normal scan pipeline keeps its priority queue because it coordinates two
    local stages. Acquisition-only can use the existing composite capture index
    directly: known-size captures first, then unknown-size captures. Metadata
    classification is performed in bounded 2,000-row pages and intentional
    binary skips are persisted in batches. This starts replay work with no
    project-sized INSERT/DELETE churn in a temporary SQLite table.
    """
    requeue_reclassifiable_skips(database, "all_text", CLASSIFIER_REVISION)
    database.execute("DROP TABLE IF EXISTS temp.archive_scout_capture_selection")
    source = "captures c"
    clauses: list[str] = []
    params: list[object] = []
    if capture_ids:
        database.execute(
            "CREATE TEMP TABLE archive_scout_capture_selection(id INTEGER PRIMARY KEY) WITHOUT ROWID"
        )
        database.executemany(
            "INSERT OR IGNORE INTO archive_scout_capture_selection(id) VALUES(?)",
            ((int(value),) for value in capture_ids),
        )
        source += " JOIN archive_scout_capture_selection s ON s.id=c.id"
    else:
        clauses.extend(["c.query_signature=?", "c.download_attempts<?"])
        params.extend([cdx_query_signature(config), config.max_attempts])
    if states:
        clauses.append("c.state IN (" + ",".join("?" for _ in states) + ")")
        params.extend(states)
    base_where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    total = int(database.execute(
        "SELECT COUNT(*) FROM " + source + base_where, params
    ).fetchone()[0])
    stats = {"metadata_skipped": 0}

    def classify_batch(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
        selected: list[sqlite3.Row] = []
        skipped_ids: list[int] = []
        for row in rows:
            if classify_text_candidate(
                str(row["original_url"]), str(row["mimetype"] or "")
            ) == "binary":
                skipped_ids.append(int(row["id"]))
            else:
                selected.append(row)
        if skipped_ids:
            now = utc_now()
            with database:
                database.executemany(
                    """UPDATE captures SET state='skipped',skip_reason='known_non_text',
                       classifier_revision=?,updated_at=? WHERE id=?""",
                    ((CLASSIFIER_REVISION, now, capture_id) for capture_id in skipped_ids),
                )
            stats["metadata_skipped"] += len(skipped_ids)
        return selected

    def iter_rows() -> Iterator[sqlite3.Row]:
        if capture_ids:
            last_id = 0
            while True:
                where = base_where + (" AND " if base_where else " WHERE ") + "c.id>?"
                rows = database.execute(
                    "SELECT c.* FROM " + source + where + " ORDER BY c.id LIMIT 2000",
                    [*params, last_id],
                ).fetchall()
                if not rows:
                    return
                last_id = int(rows[-1]["id"])
                yield from classify_batch(rows)
            return

        # The captures_download_length_idx index supports this directly without
        # constructing a second project-sized queue table.
        last_length = -1
        last_id = 0
        while True:
            where = base_where + (" AND " if base_where else " WHERE ")
            where += "COALESCE(c.length,0)>0 AND (c.length,c.id)>(?,?)"
            rows = database.execute(
                "SELECT c.* FROM " + source + where + " ORDER BY c.length,c.id LIMIT 2000",
                [*params, last_length, last_id],
            ).fetchall()
            if not rows:
                break
            last_length = max(0, int(rows[-1]["length"] or 0))
            last_id = int(rows[-1]["id"])
            yield from classify_batch(rows)

        last_id = 0
        while True:
            where = base_where + (" AND " if base_where else " WHERE ")
            where += "COALESCE(c.length,0)<=0 AND c.id>?"
            rows = database.execute(
                "SELECT c.* FROM " + source + where + " ORDER BY c.id LIMIT 2000",
                [*params, last_id],
            ).fetchall()
            if not rows:
                break
            last_id = int(rows[-1]["id"])
            yield from classify_batch(rows)

    return total, iter_rows(), stats


def select_download_rows(
    database: sqlite3.Connection,
    config: ProjectConfig,
    patterns,
    states: tuple[str, ...] = ("pending",),
    capture_ids: list[int] | None = None,
) -> list[sqlite3.Row]:
    _total, rows = prepare_download_rows(
        database, config, patterns, states=states, capture_ids=capture_ids
    )
    return list(rows)


def _download_capture(
    row: dict[str, object],
    path: Path,
    config: ProjectConfig,
    client: HttpClient,
    *,
    verify_existing_hash: bool = True,
    compute_hash: bool = True,
) -> dict:
    original = str(row["original_url"])
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        # Existing final paths have already crossed the atomic .part -> final
        # boundary, so they are complete captures. Read only the small sniff
        # prefix here instead of pulling the entire file into RAM merely to
        # slice the first 16 KiB. The hash is still verified in the normal
        # scan path when it is missing from the manifest.
        size = path.stat().st_size
        digest = str(row.get("content_hash") or "")
        if not digest and verify_existing_hash:
            _size, digest = sha256_file(path)
            size = _size
        with path.open("rb") as handle:
            preview = handle.read(16384)
        if not looks_textual_bytes(preview, str(row.get("mimetype") or "")):
            return {"kind": "non_text", "capture_id": int(row["id"])}
        return {
            "kind": "downloaded",
            "capture_id": int(row["id"]), "path": path, "bytes_saved": size,
            "content_hash": digest, "http_status": 200,
            "final_url": replay_url(str(row["timestamp"]), original),
            "content_type": str(row.get("mimetype") or ""), "preview": preview,
        }
    temp = path.with_name(path.name + ".part")
    # The old 25 MB setting must not silently discard a known larger text page.
    # For known CDX lengths, allow the advertised payload plus headroom while
    # retaining the configured budget for unknown-length responses.
    known_length = max(0, int(row.get("length") or 0))
    stream_limit = max(config.max_file_bytes, known_length + 1024 * 1024)
    replay = replay_url(str(row["timestamp"]), original)
    if compute_hash:
        response = client.download_to_path(replay, temp, stream_limit)
    else:
        response = client.download_to_path(
            replay, temp, stream_limit, compute_hash=False
        )
    content_type = (
        response["headers"].get("content-type")
        or response["headers"].get("Content-Type")
        or row.get("mimetype")
        or ""
    )
    preview = bytes(response.get("preview") or b"")
    if not looks_textual_bytes(preview, str(content_type)):
        temp.unlink(missing_ok=True)
        return {
            "kind": "non_text", "capture_id": int(row["id"]),
            "content_type": str(content_type), "http_status": response["status"],
            "final_url": response["final_url"],
        }
    preview_text = decode_bytes(preview, str(content_type))
    replay_problem = classify_replay_content(preview_text, str(response["final_url"]))
    if replay_problem:
        temp.unlink(missing_ok=True)
        raise RuntimeError(replay_problem)
    os.replace(temp, path)
    charset = CHARSET_PATTERN.search(str(content_type))
    return {
        "kind": "downloaded",
        "capture_id": int(row["id"]), "path": path,
        "bytes_saved": int(response["bytes"]),
        "content_hash": str(response["content_hash"]),
        "http_status": response["status"], "final_url": response["final_url"],
        "content_type": str(content_type), "preview": preview,
        "encoding": charset.group(1) if charset else "",
    }


def _scan_saved_capture(
    row: dict[str, object], path: Path, config: ProjectConfig, jobs: list[ScanJob]
) -> dict:
    data = path.read_bytes()
    content_type = str(row.get("mimetype") or "")
    if row.get("detected_encoding"):
        content_type += "; charset=" + str(row["detected_encoding"])
    if not looks_textual_bytes(data[:16384], content_type):
        return {"kind": "non_text", "capture_id": int(row["id"]), "path": path}
    content_hash = str(row.get("content_hash") or "") or hashlib.sha256(data).hexdigest()
    raw, encoding = decode_bytes_with_encoding(data, content_type)
    # The decoded source is the canonical scan input from this point onward.
    # Releasing the byte buffer before DOM/normalization work avoids keeping
    # both a potentially huge bytes object and several Unicode views alive.
    del data
    replay_problem = classify_replay_content(raw, str(row.get("final_url") or replay_url(str(row["timestamp"]), str(row["original_url"]))))
    if replay_problem:
        raise RuntimeError(replay_problem)
    original = str(row["original_url"])
    title, visible, links = parse_page(raw, original)
    if config.media.enabled and config.media.discover_embedded:
        embed_urls = {candidate.url for candidate in extract_embed_candidates_fast(raw, original)}
        if embed_urls:
            links = sorted(set(links).union(embed_urls))
    prepared_fields, prepared_normalized_fields = prepare_analysis_fields(
        original, title, visible, raw, links
    )
    analyses = {
        job.scan_run_id: analyze_content(
            original, title, visible, raw, links, job.patterns, job.prefilter,
            prepared_fields, prepared_normalized_fields,
            include_hit_fields=config.report.store_keyword_fields,
            include_snippets=config.report.store_snippets,
            include_interesting_links=config.report.store_interesting_links,
        )
        for job in jobs
    }
    return {
        "kind": "scanned", "capture_id": int(row["id"]), "path": path,
        "title": title, "visible": visible, "links": links,
        "analyses": analyses, "content_hash": content_hash,
        "normalized_hash": hash_text(prepared_normalized_fields["body"]),
        "bytes_saved": path.stat().st_size, "encoding": encoding,
    }


def fetch_parse_scan(row: sqlite3.Row, config: ProjectConfig, jobs: list[ScanJob], client: HttpClient) -> dict:
    """Legacy extension API; v1.0.6's main pipeline calls download and scan separately."""
    row_dict = dict(row)
    path = url_capture_path(config.output_dir, str(row["timestamp"]), str(row["original_url"]))
    downloaded = _download_capture(row_dict, path, config, client)
    if downloaded["kind"] != "downloaded":
        raise RuntimeError("downloaded response was not textual")
    row_dict.update(downloaded)
    return _scan_saved_capture(row_dict, path, config, jobs)


def save_success(database: sqlite3.Connection, result: dict, report_config=None) -> None:
    document_id = upsert_document(
        database, result["capture_id"], result["path"], result["title"],
        result["visible"], result["links"], result["content_hash"],
        result["normalized_hash"], result["bytes_saved"],
    )
    database.execute(
        "UPDATE captures SET state='downloaded',detected_encoding=?,updated_at=? WHERE id=?",
        (result.get("encoding") or "", utc_now(), result["capture_id"]),
    )
    for scan_run_id, analysis in result["analyses"].items():
        save_match(database, int(scan_run_id), document_id, analysis, report_config)
    resolve_errors(database, capture_id=result["capture_id"], document_id=document_id)


def _pending_scan_rows(
    database: sqlite3.Connection, config: ProjectConfig, capture_ids: list[int] | None = None
) -> Iterator[sqlite3.Row]:
    clauses = ["state='downloaded_unscanned'", "local_path IS NOT NULL"]
    params: list[object] = []
    if capture_ids:
        placeholders = ",".join("?" for _ in capture_ids)
        clauses.append(f"id IN ({placeholders})")
        params.extend(int(value) for value in capture_ids)
    else:
        clauses.append("query_signature=?")
        params.append(cdx_query_signature(config))
    last = 0
    while True:
        rows = database.execute(
            "SELECT * FROM captures WHERE " + " AND ".join(clauses) + " AND id>? ORDER BY id LIMIT 1000",
            [*params, last],
        ).fetchall()
        if not rows:
            return
        for row in rows:
            last = int(row["id"])
            yield row


def download_archive(
    config: ProjectConfig,
    database: sqlite3.Connection,
    scan_run_id: int,
    stop_event: threading.Event,
    callback: Callable[[ProgressEvent], None] | None,
    states: tuple[str, ...] = ("pending",),
    capture_ids: list[int] | None = None,
    scan_jobs: list[ScanJob] | None = None,
) -> None:
    if config.download_scope == "index_only":
        if callback:
            callback(ProgressEvent("download", "Index-only mode selected; downloads skipped."))
        return
    jobs = scan_jobs or [ScanJob.create(scan_run_id, config.keyword_set_name, config.keywords)]
    if not jobs or any(not job.patterns for job in jobs):
        raise ValueError("at least one keyword rule is required")
    combined_patterns = [item for job in jobs for item in job.patterns]
    with database:
        database.execute("UPDATE captures SET state='pending' WHERE state='downloading'")
        database.execute("UPDATE captures SET state='downloaded_unscanned' WHERE state='scanning'")
    total, row_iter = prepare_download_rows(
        database, config, combined_patterns, states=states, capture_ids=capture_ids
    )
    completed_before, cumulative_total = cumulative_download_progress(database, config, total, capture_ids)

    limiter = SharedFixedRateLimiter(config.download_delay)
    host_gate = shared_host_gate(config.rate_limit_base_pause, config.rate_limit_max_pause)

    def on_retry(attempt: int, total_attempts: int, reason: str, wait_seconds: float) -> None:
        if callback:
            stage = "rate_limit" if "all Wayback requests paused" in reason else "download_retry"
            callback(ProgressEvent(stage, f"{reason}. Retry {attempt}/{total_attempts} in {wait_seconds:.1f}s…"))

    client = HttpClient(
        limiter, config.retries, max(config.connect_timeout, config.read_timeout),
        config.user_agent, stop_event, retry_callback=on_retry,
        connect_timeout=config.connect_timeout, read_timeout=config.read_timeout,
        pool_size=config.workers, host_gate=host_gate,
        rate_limit_attempts=config.rate_limit_attempts,
        rate_limit_max_wait=config.rate_limit_max_wait,
        network_backend=config.network.normalized().backend,
        trust_environment=config.network.normalized().trust_environment,
        network_callback=(lambda message: callback(ProgressEvent("network", message)) if callback else None),
    )

    scan_workers = config.scan_workers or min(8, max(1, (os.cpu_count() or 4) - 1))
    scan_workers = max(1, min(32, scan_workers))
    download_limit = max(config.workers, config.workers * 2)
    scan_limit = max(scan_workers, scan_workers * 3)
    download_futures: dict[concurrent.futures.Future, dict[str, object]] = {}
    scan_futures: dict[concurrent.futures.Future, dict[str, object]] = {}
    waiting_scan: deque[dict[str, object]] = deque()
    rows_exhausted = False
    submitted_downloads = completed_downloads = downloaded_for_scan = completed_scans = matched = failures = 0
    resolved_scan_items = 0
    initial_scan_backlog = int(database.execute(
        "SELECT COUNT(*) FROM captures WHERE state='downloaded_unscanned' AND local_path IS NOT NULL"
        + (" AND query_signature=?" if not capture_ids else ""),
        (() if capture_ids else (cdx_query_signature(config),)),
    ).fetchone()[0]) if not capture_ids else 0
    started = time.monotonic()

    def emit_progress() -> None:
        if not callback:
            return
        elapsed = max(0.001, time.monotonic() - started)
        done = completed_scans + failures
        cumulative = min(cumulative_total, completed_before + done)
        backlog = max(0, initial_scan_backlog + downloaded_for_scan - resolved_scan_items)
        callback(ProgressEvent(
            "download",
            f"Replay starts {submitted_downloads:,} ({submitted_downloads/elapsed:.1f}/s); "
            f"downloads {completed_downloads:,} ({completed_downloads/elapsed:.1f}/s); "
            f"scanned {completed_scans:,} ({completed_scans/elapsed:.1f}/s); backlog {backlog:,}; "
            f"matches {matched:,}; errors {failures:,}; project {cumulative:,}/{cumulative_total:,}",
            cumulative, cumulative_total,
            {"replay_started": submitted_downloads, "replay_start_rate": submitted_downloads / elapsed,
             "downloaded": completed_downloads, "download_rate": completed_downloads / elapsed,
             "scanned": completed_scans, "scan_rate": completed_scans / elapsed,
             "scan_backlog": backlog, "matched": matched, "failures": failures,
             "download_workers": config.workers, "scan_workers": scan_workers},
        ))

    queued_scan_ids: set[int] = set()
    failed_scan_ids: set[int] = set()

    def fill_waiting_from_database() -> None:
        capacity = max(0, scan_limit - len(waiting_scan) - len(scan_futures))
        if capacity <= 0:
            return
        for pending_row in _pending_scan_rows(database, config, capture_ids):
            capture_id = int(pending_row["id"])
            if capture_id in queued_scan_ids or capture_id in failed_scan_ids:
                continue
            queued_scan_ids.add(capture_id)
            waiting_scan.append(dict(pending_row))
            capacity -= 1
            if capacity <= 0:
                break

    def schedule_waiting(scan_pool: concurrent.futures.ThreadPoolExecutor) -> None:
        # Drain replay first: local parsing/matching should not compete with
        # acquisition for CPU, memory or SQLite writes. The backlog is durable.
        if not rows_exhausted or download_futures:
            return
        if len(waiting_scan) + len(scan_futures) < scan_limit:
            fill_waiting_from_database()
        slots = max(0, scan_limit - len(scan_futures))
        items: list[dict[str, object]] = []
        while waiting_scan and len(items) < slots:
            item = waiting_scan.popleft()
            queued_scan_ids.discard(int(item["id"]))
            items.append(item)
        if not items:
            return
        now = utc_now()
        with database:
            database.executemany(
                "UPDATE captures SET state='scanning',updated_at=? WHERE id=?",
                ((now, int(item["id"])) for item in items),
            )
        for item in items:
            path = Path(str(item["local_path"]))
            future = scan_pool.submit(_scan_saved_capture, item, path, config, jobs)
            scan_futures[future] = item

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=config.workers, thread_name_prefix="archive-download") as download_pool, concurrent.futures.ThreadPoolExecutor(max_workers=scan_workers, thread_name_prefix="archive-scan") as scan_pool:
            # Scanner backlog is durable in SQLite; keep only a bounded local window.
            fill_waiting_from_database()
            schedule_waiting(scan_pool)

            while True:
                if stop_event.is_set():
                    raise Stopped

                # Keep the v1.0.5 acquisition envelope independent of local scanner speed.
                # Scanner backlog lives durably in SQLite and is allowed to grow; only
                # disk/network failures or Wayback's shared host gate may slow replay.
                slots = download_limit - len(download_futures)
                if not rows_exhausted and slots > 0:
                    batch: list[tuple[dict[str, object], Path]] = []
                    reserved_paths: set[str] = set()
                    while len(batch) < slots:
                        try:
                            row = next(row_iter)
                        except StopIteration:
                            rows_exhausted = True
                            break
                        row_dict = dict(row)
                        path = _allocate_capture_path(database, config.output_dir, row, reserved_paths)
                        reserved_paths.add(str(path))
                        row_dict["assigned_path"] = str(path)
                        batch.append((row_dict, path))
                    if batch:
                        now = utc_now()
                        with database:
                            database.executemany(
                                "UPDATE captures SET state='downloading',local_path=?,download_attempts=download_attempts+1,updated_at=? WHERE id=?",
                                ((str(path), now, int(item["id"])) for item, path in batch),
                            )
                        for item, path in batch:
                            download_futures[download_pool.submit(_download_capture, item, path, config, client)] = item
                            submitted_downloads += 1

                if download_futures:
                    done_downloads, _ = concurrent.futures.wait(
                        tuple(download_futures), timeout=0.05, return_when=concurrent.futures.FIRST_COMPLETED
                    )
                else:
                    done_downloads = set()
                for future in done_downloads:
                    row = download_futures.pop(future)
                    capture_id = int(row["id"])
                    try:
                        result = future.result()
                        if result["kind"] == "non_text":
                            with database:
                                mark_capture_skipped(database, capture_id, "sniffed_non_text", CLASSIFIER_REVISION)
                            completed_downloads += 1
                            continue
                        path = Path(result["path"])
                        content_hash = str(result["content_hash"])
                        # Acquisition first: physical exact-byte CoW dedupe is intentionally
                        # deferred to Compact Project / idle maintenance, never the replay hot path.
                        with database:
                            database.execute(
                                """UPDATE captures SET state='downloaded_unscanned',local_path=?,content_hash=?,http_status=?,final_url=?,bytes_saved=?,skip_reason=NULL,classifier_revision=?,detected_encoding=COALESCE(NULLIF(?,''),detected_encoding),updated_at=? WHERE id=?""",
                                (str(path), content_hash, result["http_status"], result["final_url"], result["bytes_saved"], CLASSIFIER_REVISION, str(result.get("encoding") or ""), utc_now(), capture_id),
                            )
                        row.update(result)
                        row["detected_encoding"] = result.get("encoding") or row.get("detected_encoding")
                        row["local_path"] = str(path)
                        # Keep local scan memory bounded. If full, SQLite remains the queue.
                        if len(waiting_scan) + len(scan_futures) < scan_limit:
                            queued_scan_ids.add(capture_id)
                            waiting_scan.append(row)
                        completed_downloads += 1
                        downloaded_for_scan += 1
                    except RateLimitDeferred:
                        with database:
                            database.execute("UPDATE captures SET state='pending',updated_at=? WHERE id=?", (utc_now(), capture_id))
                        raise
                    except Stopped:
                        raise
                    except Exception as exc:
                        failures += 1
                        category, status, retryable = classify_exception(exc)
                        issue_message = site_issue_message(category, str(row["original_url"]), "text download", status)
                        with database:
                            database.execute("UPDATE captures SET state='error',http_status=?,updated_at=? WHERE id=?", (status, utc_now(), capture_id))
                            record_error(database, "download", category, repr(exc), capture_id=capture_id, http_status=status, retryable=retryable)
                            if should_surface_site_issue(category):
                                record_site_issue(database, host_from_url(str(row["original_url"])), "text_download", category, issue_message, target=str(row["original_url"]), http_status=status)

                schedule_waiting(scan_pool)

                if scan_futures:
                    scan_timeout = 0.0 if download_futures or not rows_exhausted else 0.05
                    done_scans, _ = concurrent.futures.wait(
                        tuple(scan_futures), timeout=scan_timeout, return_when=concurrent.futures.FIRST_COMPLETED
                    )
                else:
                    done_scans = set()
                scan_results: list[tuple[dict[str, object], dict | BaseException]] = []
                for future in done_scans:
                    row = scan_futures.pop(future)
                    try:
                        scan_results.append((row, future.result()))
                    except Exception as exc:
                        scan_results.append((row, exc))
                if scan_results:
                    # One transaction for a whole completed scanner group.
                    with database:
                        for row, outcome in scan_results:
                            capture_id = int(row["id"])
                            if isinstance(outcome, BaseException):
                                failed_scan_ids.add(capture_id)
                                failures += 1
                                record_error(database, "scan", "scan_failure", repr(outcome), capture_id=capture_id, retryable=True)
                                database.execute("UPDATE captures SET state='downloaded_unscanned',updated_at=? WHERE id=?", (utc_now(), capture_id))
                                continue
                            if outcome.get("kind") == "non_text":
                                mark_capture_skipped(database, capture_id, "sniffed_non_text", CLASSIFIER_REVISION)
                                resolved_scan_items += 1
                                continue
                            save_success(database, outcome, config.report)
                            completed_scans += 1
                            resolved_scan_items += 1
                            matched += int(any(
                                int(analysis.get("score") or 0) >= config.minimum_score
                                and not analysis.get("excluded") and not analysis.get("required_missing")
                                for analysis in outcome["analyses"].values()
                            ))
                    emit_progress()

                schedule_waiting(scan_pool)

                if rows_exhausted and not download_futures and not waiting_scan and not scan_futures:
                    # Drain any durable scanner backlog after acquisition has already finished.
                    fill_waiting_from_database()
                    if waiting_scan:
                        schedule_waiting(scan_pool)
                        continue
                    break
    except Stopped:
        for future in download_futures:
            future.cancel()
        for future in scan_futures:
            future.cancel()
        with database:
            database.execute("UPDATE captures SET state='pending' WHERE state='downloading'")
            database.execute("UPDATE captures SET state='downloaded_unscanned' WHERE state='scanning'")
        raise
    finally:
        client.close()

def download_archive_only(
    config: ProjectConfig,
    database: sqlite3.Connection,
    stop_event: threading.Event,
    callback: Callable[[ProgressEvent], None] | None,
    states: tuple[str, ...] = ("pending",),
    capture_ids: list[int] | None = None,
) -> dict[str, int | float]:
    """Acquire indexed text captures without creating any scan work.

    This is the deliberately lean acquisition path used by the download-only
    operation. SQLite remains the durable capture manifest/resume queue because
    Hitlist, exact resume, retry state, and URL-to-file mapping all depend on it,
    but no documents, scan runs, matches, research indexes, media jobs, or scan
    reports are created here.
    """
    if config.download_scope == "index_only":
        if callback:
            callback(ProgressEvent("download_only", "Index-only scope selected; downloads skipped."))
        return {"queued": 0, "downloaded": 0, "skipped": 0, "errors": 0, "elapsed": 0.0}

    with database:
        database.execute("UPDATE captures SET state='pending' WHERE state='downloading'")

    # Download-only is intentionally keyword-free. The operation orchestrator
    # forces all_text so this empty pattern set cannot become a URL keyword gate.
    total, row_iter, selection_stats = prepare_download_only_rows(
        database, config, states=states, capture_ids=capture_ids
    )

    limiter = SharedFixedRateLimiter(config.download_delay)
    host_gate = shared_host_gate(config.rate_limit_base_pause, config.rate_limit_max_pause)

    def on_retry(attempt: int, total_attempts: int, reason: str, wait_seconds: float) -> None:
        if callback:
            stage = "rate_limit" if "all Wayback requests paused" in reason else "download_retry"
            callback(ProgressEvent(stage, f"{reason}. Retry {attempt}/{total_attempts} in {wait_seconds:.1f}s…"))

    client = HttpClient(
        limiter, config.retries, max(config.connect_timeout, config.read_timeout),
        config.user_agent, stop_event, retry_callback=on_retry,
        connect_timeout=config.connect_timeout, read_timeout=config.read_timeout,
        pool_size=config.workers, host_gate=host_gate,
        rate_limit_attempts=config.rate_limit_attempts,
        rate_limit_max_wait=config.rate_limit_max_wait,
        network_backend=config.network.normalized().backend,
        trust_environment=config.network.normalized().trust_environment,
        network_callback=(lambda message: callback(ProgressEvent("network", message)) if callback else None),
    )

    # Keep a deeper producer window than the normal scan pipeline because no
    # local CPU stage exists to consume executor time or memory. The fixed rate
    # limiter still controls Wayback request starts.
    inflight_limit = max(config.workers, config.workers * 3)
    # Persist destination paths in bounded staging groups, then feed the
    # executor from memory without another SQLite transaction for every newly
    # opened slot. Keeping local_path durable before replay starts means an
    # abrupt exit after the atomic .part -> final rename can adopt the exact
    # file on the next run instead of downloading it again.
    stage_limit = max(64, min(512, config.workers * 16))
    ready_downloads: deque[tuple[dict[str, object], Path]] = deque()
    futures: dict[concurrent.futures.Future, dict[str, object]] = {}
    rows_exhausted = False
    submitted = downloaded = skipped = failures = 0
    started = time.monotonic()
    last_emit = 0.0
    last_flush = started
    flush_count = max(32, min(128, config.workers * 8))
    success_buffer: list[tuple[str, str, int, str, int, int, int, str]] = []
    skipped_buffer: list[int] = []
    error_buffer: list[tuple[int, dict[str, object], BaseException]] = []

    def flush_results(force: bool = False) -> None:
        nonlocal last_flush
        pending_count = len(success_buffer) + len(skipped_buffer) + len(error_buffer)
        if not pending_count:
            return
        now_mono = time.monotonic()
        # Completion state can safely be coalesced for up to one second. Final
        # capture files are already atomically durable and their local_path was
        # staged before replay, so a crash inside this interval simply causes
        # the next run to adopt the existing file without another network GET.
        if not force and pending_count < flush_count and now_mono - last_flush < 1.0:
            return
        now = utc_now()
        with database:
            if success_buffer:
                database.executemany(
                    """UPDATE captures SET state='downloaded_unscanned',local_path=?,
                       content_hash=?,http_status=?,final_url=?,bytes_saved=?,
                       skip_reason=NULL,classifier_revision=?,
                       detected_encoding=COALESCE(NULLIF(?,''),detected_encoding),
                       download_attempts=download_attempts+1,updated_at=? WHERE id=?""",
                    (
                        (path, content_hash, http_status, final_url, bytes_saved,
                         classifier_revision, encoding, now, capture_id)
                        for path, content_hash, http_status, final_url, bytes_saved,
                            classifier_revision, capture_id, encoding in success_buffer
                    ),
                )
                # Resolve earlier capture errors in one indexed UPDATE instead of
                # one statement per successful replay. Buffers are intentionally
                # small, so this remains below SQLite's variable limit.
                success_ids = [row[-2] for row in success_buffer]
                placeholders = ",".join("?" for _ in success_ids)
                database.execute(
                    "UPDATE errors SET resolved=1,last_seen=? WHERE resolved=0 "
                    f"AND capture_id IN ({placeholders})",
                    (now, *success_ids),
                )
            if skipped_buffer:
                database.executemany(
                    """UPDATE captures SET state='skipped',skip_reason='sniffed_non_text',
                       classifier_revision=?,download_attempts=download_attempts+1,
                       updated_at=? WHERE id=?""",
                    ((CLASSIFIER_REVISION, now, capture_id) for capture_id in skipped_buffer),
                )
            for capture_id, item, exc in error_buffer:
                category, status, retryable = classify_exception(exc)
                database.execute(
                    """UPDATE captures SET state='error',http_status=?,
                       download_attempts=download_attempts+1,updated_at=? WHERE id=?""",
                    (status, now, capture_id),
                )
                record_error(
                    database, "download", category, repr(exc), capture_id=capture_id,
                    http_status=status, retryable=retryable,
                )
                if should_surface_site_issue(category):
                    record_site_issue(
                        database, host_from_url(str(item["original_url"])),
                        "text_download", category,
                        site_issue_message(
                            category, str(item["original_url"]), "text download", status
                        ),
                        target=str(item["original_url"]), http_status=status,
                    )
        success_buffer.clear()
        skipped_buffer.clear()
        error_buffer.clear()
        last_flush = now_mono

    def stage_candidates() -> None:
        """Fill a small durable path queue without project-sized temp tables.

        Path staging is intentionally decoupled from replay submission. On
        Windows, executor completions can arrive one at a time; the old loop
        committed a new 'downloading' row for each freed slot and therefore
        turned platform scheduling differences into dozens of SQLite commits.
        Staging up to a few hundred paths once keeps memory bounded while making
        database pressure independent of thread wake-up timing.
        """
        nonlocal rows_exhausted
        if rows_exhausted or len(ready_downloads) >= stage_limit:
            return
        staged: list[tuple[dict[str, object], Path]] = []
        reserved_paths: set[str] = set()
        while len(ready_downloads) + len(staged) < stage_limit:
            try:
                row = next(row_iter)
            except StopIteration:
                rows_exhausted = True
                break
            item = dict(row)
            path = _allocate_capture_path(database, config.output_dir, row, reserved_paths)
            reserved_paths.add(str(path))
            item["assigned_path"] = str(path)
            staged.append((item, path))
        if not staged:
            return
        now = utc_now()
        with database:
            database.executemany(
                "UPDATE captures SET local_path=?,updated_at=? WHERE id=?",
                ((str(path), now, int(item["id"])) for item, path in staged),
            )
        ready_downloads.extend(staged)

    def emit_progress(force: bool = False) -> None:
        nonlocal last_emit
        if not callback:
            return
        now = time.monotonic()
        if not force and now - last_emit < 0.5:
            return
        last_emit = now
        elapsed = max(0.001, now - started)
        metadata_skipped = int(selection_stats["metadata_skipped"])
        settled = downloaded + skipped + failures + metadata_skipped
        callback(ProgressEvent(
            "download_only",
            f"Download-only: starts {submitted:,} ({submitted/elapsed:.1f}/s); "
            f"saved {downloaded:,} ({downloaded/elapsed:.1f}/s); "
            f"skipped {skipped + metadata_skipped:,}; "
            f"errors {failures:,}; {settled:,}/{total:,}",
            min(settled, total), total,
            {
                "replay_started": submitted,
                "replay_start_rate": submitted / elapsed,
                "downloaded": downloaded,
                "download_rate": downloaded / elapsed,
                "skipped": skipped + metadata_skipped,
                "failures": failures,
                "pending": max(0, total - settled),
                "downloaded_unscanned": downloaded,
                "download_workers": config.workers,
                "scan_workers": 0,
            },
        ))

    try:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=config.workers, thread_name_prefix="archive-download-only"
        ) as pool:
            while True:
                if stop_event.is_set():
                    raise Stopped

                if len(ready_downloads) < max(config.workers, inflight_limit):
                    stage_candidates()

                slots = inflight_limit - len(futures)
                while slots > 0 and ready_downloads:
                    item, path = ready_downloads.popleft()
                    futures[pool.submit(
                        _download_capture, item, path, config, client,
                        verify_existing_hash=False, compute_hash=False,
                    )] = item
                    submitted += 1
                    slots -= 1

                if not futures:
                    if rows_exhausted and not ready_downloads:
                        flush_results(force=True)
                        break
                    flush_results()
                    continue

                done, _ = concurrent.futures.wait(
                    tuple(futures), timeout=0.05,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                if not done:
                    flush_results()
                    emit_progress()
                    continue

                for future in done:
                    item = futures.pop(future)
                    capture_id = int(item["id"])
                    try:
                        result = future.result()
                        if result["kind"] == "non_text":
                            skipped_buffer.append(capture_id)
                            skipped += 1
                            continue
                        path = Path(result["path"])
                        success_buffer.append((
                            str(path), str(result["content_hash"]), int(result["http_status"]),
                            str(result["final_url"]), int(result["bytes_saved"]),
                            CLASSIFIER_REVISION, capture_id, str(result.get("encoding") or ""),
                        ))
                        downloaded += 1
                    except RateLimitDeferred:
                        flush_results(force=True)
                        with database:
                            database.execute(
                                """UPDATE captures SET state='pending',
                                   download_attempts=download_attempts+1,updated_at=? WHERE id=?""",
                                (utc_now(), capture_id),
                            )
                        raise
                    except Stopped:
                        flush_results(force=True)
                        raise
                    except Exception as exc:
                        error_buffer.append((capture_id, item, exc))
                        failures += 1

                # Coalesce completion records across executor wakeups. FIRST_COMPLETED
                # often yields one future at a time; flushing by count/time avoids turning
                # that into one SQLite commit per capture while preserving fast resume.
                flush_results()
                emit_progress()

        flush_results(force=True)
        emit_progress(force=True)
        return {
            "queued": total,
            "downloaded": downloaded,
            "skipped": skipped + int(selection_stats["metadata_skipped"]),
            "errors": failures,
            "elapsed": time.monotonic() - started,
        }
    except Stopped:
        for future in futures:
            future.cancel()
        flush_results(force=True)
        raise
    finally:
        client.close()
