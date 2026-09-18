from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .cdx.client import RateLimitDeferred
from .cdx.indexer import index_archive
from .analysis.workflow import run_analysis
from .config import KeywordSetConfig, ProjectConfig, save_project_config
from .database.connection import open_database
from .database.repositories import (finish_scan_run, get_or_create_keyword_set, latest_scan_run, start_scan_run, start_operation_run, finish_operation_run, update_operation_run)
from .downloads.downloader import download_archive, download_archive_only
from .downloads.retry import retry_error_urls
from .events import ConnectivityPaused, ProgressEvent, Stopped
from .media.downloader import download_media, retry_media_errors
from .media.indexer import index_external_embedded_media, index_media
from .media.reports import generate_media_reports
from .projects.integrity import check_project_integrity
from .projects.backups import create_project_backup
from .projects.compaction import compact_project_storage
from .projects.repair import repair_project
from .projects.diagnostics import export_diagnostics
from .projects.importers import import_text_folder
from .constants import VERSION
from .projects.merge import merge_projects
from .reports.text import generate_index_reports, generate_reports
from .research.index import build_research_index
from .scanning.jobs import ScanJob
from .scanning.rescanner import rescan_keyword_sets
from .scanning.hitlist import load_hitlist, search_with_hitlist

SUPPORTED_MODES = {
    "all", "external_media_after_scan", "index", "download_only", "download", "resume", "rescan", "retry_errors", "report", "integrity",
    "repair", "backup", "diagnostics", "import_folder",
    "media_all", "media_index", "media_download", "media_retry",
    "analysis", "research_index", "forum_rebuild", "merge_project", "hitlist", "compact",
}


def is_recoverable_pause(exc: BaseException) -> bool:
    """Stable integration contract for UI/bot wrappers around run_project()."""
    return isinstance(exc, (ConnectivityPaused, RateLimitDeferred))


def emit(callback: Callable[[ProgressEvent], None] | None, event: ProgressEvent) -> None:
    if callback:
        callback(event)


def prepare_scan_jobs(
    database: sqlite3.Connection,
    config: ProjectConfig,
    mode: str,
) -> list[ScanJob]:
    jobs: list[ScanJob] = []
    selected = config.selected_keyword_sets()
    if not selected:
        raise ValueError("select at least one keyword set containing at least one rule")
    seen_keyword_set_ids: set[int] = set()
    for keyword_set in selected:
        keyword_set_id = get_or_create_keyword_set(database, keyword_set.name, keyword_set.rules)
        if keyword_set_id in seen_keyword_set_ids:
            continue
        seen_keyword_set_ids.add(keyword_set_id)
        compatible_sources = (mode,) if mode not in {"resume", "download", "all"} else ("all", "download", "resume")
        placeholders = ",".join("?" for _ in compatible_sources)
        existing = database.execute(
            f"""SELECT id FROM scan_runs WHERE keyword_set_id=? AND status='interrupted'
                AND minimum_score=? AND source_operation IN ({placeholders}) ORDER BY id DESC LIMIT 1""",
            (keyword_set_id, config.minimum_score, *compatible_sources),
        ).fetchone()
        if existing:
            run_id = int(existing["id"])
            database.execute(
                "UPDATE scan_runs SET status='running',completed_at=NULL,name=? WHERE id=?",
                (f"{keyword_set.name} ({mode})", run_id),
            )
        else:
            run_id = start_scan_run(
                database, keyword_set_id, f"{keyword_set.name} ({mode})",
                config.minimum_score, mode,
                {"keyword_set": keyword_set.name, "rules": keyword_set.rules},
            )
        jobs.append(ScanJob.create(run_id, keyword_set.name, keyword_set.rules))
    database.commit()
    return jobs


def finish_jobs(database: sqlite3.Connection, jobs: list[ScanJob], status: str) -> None:
    for job in jobs:
        finish_scan_run(database, job.scan_run_id, status)


def generate_job_reports(config: ProjectConfig, database: sqlite3.Connection, jobs: list[ScanJob]) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for index, job in enumerate(jobs, 1):
        generated = generate_reports(config, database, job.scan_run_id)
        if index == 1:
            paths.update(generated)
        paths[f"scan_{job.scan_run_id}_folder"] = generated["scan_folder"]
    return paths


def _secondary_media_config(config: ProjectConfig) -> ProjectConfig:
    """Return media CDX settings without mutating the primary text query.

    Media acquired as a follow-up to a text/download-only run defaults to one
    archived timestamp per URL. The default ``earliest`` policy therefore uses
    ``collapse=urlkey`` regardless of the text query's collapse settings. If the
    user explicitly selects ``latest`` or ``all``, the automatic URL-key collapse
    is removed for the media query so that policy can actually take effect.
    Dedicated media-only operations keep their normal CDX settings unchanged.
    """
    normalized = config.normalized()
    if normalized.media.snapshot_strategy == "earliest":
        collapses = ["urlkey"]
    else:
        collapses = [value for value in normalized.cdx_collapses if value != "urlkey"]
    # Advanced text parameters must not sneak a second collapse policy back
    # into the follow-up media request. Keep unrelated user parameters intact.
    media_extra = [line for line in normalized.cdx_extra_params
                   if line.partition("=")[0].strip().casefold() != "collapse"]
    return replace(normalized, cdx_collapses=collapses, cdx_extra_params=media_extra).normalized()


def run_project(
    config: ProjectConfig,
    mode: str = "all",
    stop_event: threading.Event | None = None,
    callback: Callable[[ProgressEvent], None] | None = None,
) -> dict[str, Path]:
    config = config.normalized()
    if mode == "external_media_after_scan":
        config.media = replace(
            config.media.normalized(),
            enabled=True,
            discover_embedded=True,
            allow_external_embeds=True,
        )
    if mode not in SUPPORTED_MODES:
        raise ValueError(f"unsupported mode: {mode}")
    if config.from_date > config.to_date:
        raise ValueError("start date must not be later than end date")
    if mode in {"all", "external_media_after_scan", "index", "download_only"} and not config.targets:
        raise ValueError("at least one target is required")
    if mode.startswith("media_") and not (config.media.targets or config.targets):
        raise ValueError("at least one media target or site target is required")
    if mode in {"all", "external_media_after_scan", "download", "resume", "rescan", "retry_errors"} and not config.selected_keyword_sets():
        raise ValueError("select at least one keyword set")
    stop_event = stop_event or threading.Event()
    emit(callback, ProgressEvent("starting", "Opening project database; large projects may need recovery or migration…"))
    if stop_event.is_set():
        raise Stopped
    config.output_dir.mkdir(parents=True, exist_ok=True)
    (config.output_dir / "captures").mkdir(exist_ok=True)
    (config.output_dir / "media" / "images").mkdir(parents=True, exist_ok=True)
    (config.output_dir / "media" / "videos").mkdir(parents=True, exist_ok=True)
    (config.output_dir / "reports").mkdir(exist_ok=True)
    database = open_database(config.output_dir, migrate=True)
    jobs: list[ScanJob] = []
    operation_run_id = start_operation_run(database, mode, VERSION)
    database.commit()
    original_callback = callback
    owner_thread_id = threading.get_ident()
    last_progress_write = 0.0
    last_progress_stage = ""
    progress_persist_interval = 5.0 if mode == "download_only" else 0.75

    def operation_callback(event: ProgressEvent) -> None:
        nonlocal last_progress_write, last_progress_stage
        if threading.get_ident() == owner_thread_id:
            now = time.monotonic()
            completed_boundary = (
                event.current is not None
                and event.total is not None
                and event.total >= 0
                and event.current >= event.total
            )
            stage_changed = bool(event.stage) and event.stage != last_progress_stage
            # UI/bot callbacks still receive every event immediately. Persisted
            # operation progress is rate-limited so a fast download/scan no longer
            # forces a full SQLite commit for every single completed item.
            should_write = stage_changed or completed_boundary or now - last_progress_write >= progress_persist_interval
            if should_write:
                update_operation_run(
                    database,
                    operation_run_id,
                    message=event.message,
                    completed=event.current,
                    total=event.total,
                    stage=event.stage,
                )
                database.commit()
                last_progress_write = now
                last_progress_stage = event.stage
        if original_callback:
            original_callback(event)

    callback = operation_callback
    try:
        save_project_config(config)
        if mode == "backup":
            path = create_project_backup(config.output_dir, reason="manual", keep=config.backup_keep, max_mb=config.backup_max_mb)
            emit(callback, ProgressEvent("backup", f"Backup written to {path}"))
            finish_operation_run(database, operation_run_id, "complete", str(path))
            database.commit()
            return {"backup": path}
        if mode == "repair":
            path = repair_project(config.output_dir, database, callback, keep_backups=config.backup_keep, backup_max_mb=config.backup_max_mb)
            finish_operation_run(database, operation_run_id, "complete", str(path))
            database.commit()
            return {"repair": path}
        if mode == "diagnostics":
            path = export_diagnostics(config.output_dir, database, callback)
            finish_operation_run(database, operation_run_id, "complete", str(path))
            database.commit()
            return {"diagnostics": path}
        if mode == "import_folder":
            source = Path(config.import_source).expanduser()
            if not source.is_dir():
                raise ValueError("choose an existing folder to import")
            if config.auto_backup and (config.output_dir / "archive_scout.sqlite3").exists():
                create_project_backup(config.output_dir, reason="before_import", keep=config.backup_keep, max_mb=config.backup_max_mb)
            imported = import_text_folder(config.output_dir, source, database, stop_event, callback)
            report = config.output_dir / "reports" / "import_summary.txt"
            report.write_text(f"Archive Scout import\n\nSource: {source}\nImported: {imported}\n", encoding="utf-8")
            finish_operation_run(database, operation_run_id, "complete", f"Imported {imported}")
            database.commit()
            return {"import_summary": report}
        if mode == "hitlist":
            keywords = load_hitlist(config.hitlist_keywords, config.hitlist_file)
            if not keywords:
                # A selected keyword set is a convenient fallback for users who
                # want a quick literal check without duplicating their hitlist.
                selected = config.selected_keyword_sets()
                if selected:
                    from .scanning.keywords import parse_keyword_rules
                    values: list[str] = []
                    for keyword_set in selected:
                        for rule in parse_keyword_rules(keyword_set.rules):
                            if (not rule.regex and not rule.case_sensitive and not rule.whole_word
                                    and not rule.excluded and str(rule.expression).strip()):
                                values.append(str(rule.expression))
                    keywords = load_hitlist(values)
            if not keywords:
                raise ValueError("enter at least one literal keyword or choose a hitlist file")
            result = search_with_hitlist(config.output_dir, database, keywords, stop_event, callback)
            finish_operation_run(database, operation_run_id, "complete", f"Hitlist search complete: {result['matches']} matching captures")
            database.commit()
            return {"hitlist_csv": Path(result["csv"]), "hitlist_summary": Path(result["summary"])}
        if mode == "compact":
            result = compact_project_storage(config.output_dir, database, stop_event, callback)
            report = config.output_dir / "reports" / "storage_compaction.txt"
            report.write_text(
                "Archive Scout storage compaction\n\n" +
                "\n".join(f"{key}: {value}" for key, value in sorted(result.items())) + "\n",
                encoding="utf-8",
            )
            finish_operation_run(database, operation_run_id, "complete", str(report))
            database.commit()
            return {"storage_compaction": report}
        if mode == "integrity":
            path = check_project_integrity(config.output_dir, database, callback)
            emit(callback, ProgressEvent("integrity", f"Integrity report written to {path}"))
            finish_operation_run(database, operation_run_id, "complete", str(path))
            database.commit()
            return {"integrity": path}
        if mode == "analysis":
            paths = run_analysis(config, database, stop_event, callback, forum_only=False)
            finish_operation_run(database, operation_run_id, "complete", "Analysis complete")
            database.commit()
            return paths
        if mode == "research_index":
            summary = build_research_index(config, database, stop_event, callback)
            report = config.output_dir / "reports" / "research_index.json"
            import json as _json
            report.write_text(_json.dumps(summary.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
            finish_operation_run(database, operation_run_id, "complete", "Research Intelligence index complete")
            database.commit()
            return {"research_index": report}
        if mode == "forum_rebuild":
            paths = run_analysis(config, database, stop_event, callback, forum_only=True)
            finish_operation_run(database, operation_run_id, "complete", "Forum rebuild complete")
            database.commit()
            return paths
        if mode == "merge_project":
            source = Path(config.analysis.normalized().merge_source).expanduser()
            if not str(source).strip() or not source.exists():
                raise ValueError("choose an existing Archive Scout project folder to merge")
            if config.auto_backup and (config.output_dir / "archive_scout.sqlite3").exists():
                create_project_backup(config.output_dir, reason="before_merge", keep=config.backup_keep, max_mb=config.backup_max_mb)
            summary = merge_projects(config.output_dir, source, database, stop_event, callback)
            merge_report = config.output_dir / "reports" / "merge_summary.txt"
            merge_report.write_text("Archive Scout project merge\n\n" + "\n".join(f"{key}: {value}" for key, value in summary.items()) + "\n", encoding="utf-8")
            finish_operation_run(database, operation_run_id, "complete", str(merge_report))
            database.commit()
            return {"merge_summary": merge_report}
        if mode == "download_only":
            # Keep SQLite as a lightweight durable manifest/resume queue. No
            # scan/document/match/research work is created here. Optional media
            # acquisition uses the same Media-page settings as a full text run,
            # but never creates scan jobs merely to discover embedded media.
            acquisition_config = replace(config, download_scope="all_text")
            index_archive(acquisition_config, database, stop_event, callback)
            stats = download_archive_only(
                acquisition_config, database, stop_event, callback, states=("pending",)
            )
            paths: dict[str, Path] = {"project": config.output_dir / "project.json"}
            if config.media.enabled:
                media_config = _secondary_media_config(config)
                emit(
                    callback,
                    ProgressEvent(
                        "media_index",
                        "Text acquisition is complete. Indexing optional media with a single archived timestamp per URL by default…",
                    ),
                )
                index_media(media_config, database, stop_event, callback)
                download_media(media_config, database, stop_event, callback)
                paths.update(generate_media_reports(media_config, database))
            emit(
                callback,
                ProgressEvent(
                    "download_only",
                    f"Download-only acquisition complete: {int(stats['downloaded']):,} saved; "
                    f"{int(stats['skipped']):,} non-text skipped; {int(stats['errors']):,} errors. "
                    "Run Search with Hitlist when ready.",
                    int(stats["queued"]), int(stats["queued"]),
                    {"downloaded": int(stats["downloaded"]), "scan_workers": 0},
                ),
            )
            finish_operation_run(database, operation_run_id, "complete", "Download-only acquisition complete")
            database.commit()
            return paths
        if mode == "index":
            index_archive(config, database, stop_event, callback)
            paths = generate_index_reports(config, database)
            paths["project"] = config.output_dir / "project.json"
            finish_operation_run(database, operation_run_id, "complete", "Index complete")
            database.commit()
            return paths
        if mode == "report":
            existing = latest_scan_run(database)
            if existing is None:
                if database.execute("SELECT COUNT(*) FROM captures").fetchone()[0]:
                    paths = generate_index_reports(config, database)
                else:
                    raise RuntimeError("this project does not contain indexed captures or a completed scan run")
            else:
                paths = generate_reports(config, database, existing)
            emit(callback, ProgressEvent("report", f"Reports written to {config.output_dir / 'reports'}"))
            finish_operation_run(database, operation_run_id, "complete", "Reports regenerated")
            database.commit()
            return paths
        if mode == "media_index":
            index_media(config, database, stop_event, callback)
            paths = generate_media_reports(config, database)
            finish_operation_run(database, operation_run_id, "complete", "Media index complete")
            database.commit()
            return paths
        if mode == "media_download":
            download_media(config, database, stop_event, callback)
            paths = generate_media_reports(config, database)
            finish_operation_run(database, operation_run_id, "complete", "Media download complete")
            database.commit()
            return paths
        if mode == "media_retry":
            retry_media_errors(config, database, stop_event, callback)
            paths = generate_media_reports(config, database)
            finish_operation_run(database, operation_run_id, "complete", "Media retry complete")
            database.commit()
            return paths
        if mode == "media_all":
            index_media(config, database, stop_event, callback)
            download_media(config, database, stop_event, callback)
            paths = generate_media_reports(config, database)
            finish_operation_run(database, operation_run_id, "complete", "Media operation complete")
            database.commit()
            return paths

        if mode in {"all", "external_media_after_scan", "resume"}:
            index_archive(config, database, stop_event, callback)

        jobs = prepare_scan_jobs(database, config, mode)
        primary_run_id = jobs[0].scan_run_id
        if mode in {"all", "external_media_after_scan"}:
            download_archive(config, database, primary_run_id, stop_event, callback, states=("pending",), scan_jobs=jobs)
        elif mode in {"download", "resume"}:
            download_archive(config, database, primary_run_id, stop_event, callback, states=("pending",), scan_jobs=jobs)
        elif mode == "rescan":
            rescan_keyword_sets(database, jobs, stop_event, callback, workers=(config.scan_workers or None), report_config=config.report)
        elif mode == "retry_errors":
            retry_error_urls(config, database, primary_run_id, stop_event, callback, jobs)
            media_error_count = database.execute(
                "SELECT COUNT(*) FROM errors WHERE resolved=0 AND ignored=0 AND retryable=1 AND media_capture_id IS NOT NULL"
            ).fetchone()[0]
            if media_error_count:
                retry_media_errors(
                    config,
                    database,
                    stop_event,
                    callback,
                    config.retry_media_capture_ids or None,
                )
        finish_jobs(database, jobs, "complete")
        database.commit()
        paths = generate_job_reports(config, database, jobs)
        if mode == "retry_errors" and database.execute("SELECT COUNT(*) FROM media_captures").fetchone()[0]:
            paths.update(generate_media_reports(config, database))
        if mode == "external_media_after_scan":
            media_config = _secondary_media_config(config)
            emit(
                callback,
                ProgressEvent(
                    "media_embed",
                    "Text scanning is complete. Discovering external images/videos in saved pages and resolving their Wayback captures…",
                ),
            )
            media_signature = index_external_embedded_media(media_config, database, stop_event, callback)
            media_total = int(database.execute(
                "SELECT COUNT(*) FROM media_captures WHERE query_signature=?", (media_signature,)
            ).fetchone()[0])
            pending_media = int(database.execute(
                "SELECT COUNT(*) FROM media_captures WHERE query_signature=? AND state IN ('pending','error')",
                (media_signature,),
            ).fetchone()[0])
            emit(
                callback,
                ProgressEvent(
                    "media_embed",
                    f"External-media indexing complete: {media_total:,} archived media captures resolved; {pending_media:,} need download. Starting streamed downloads…",
                    0,
                    pending_media,
                ),
            )
            download_media(media_config, database, stop_event, callback)
            paths.update(generate_media_reports(media_config, database))
        elif mode == "all" and config.media.enabled:
            media_config = _secondary_media_config(config)
            index_media(media_config, database, stop_event, callback)
            download_media(media_config, database, stop_event, callback)
            paths.update(generate_media_reports(media_config, database))
        if config.research.enabled and config.research.auto_build:
            research_summary = build_research_index(config, database, stop_event, callback)
            research_report = config.output_dir / "reports" / "research_index.json"
            import json as _json
            research_report.write_text(_json.dumps(research_summary.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
            paths["research_index"] = research_report
        emit(callback, ProgressEvent("report", f"Reports written to {config.output_dir / 'reports'}"))
        finish_operation_run(database, operation_run_id, "complete", "Operation complete")
        database.commit()
        return paths
    except ConnectivityPaused as exc:
        with database:
            database.execute("UPDATE captures SET state='pending' WHERE state='downloading'")
            database.execute("UPDATE captures SET state='downloaded_unscanned' WHERE state='scanning'")
            database.execute("UPDATE media_captures SET state='pending' WHERE state='downloading'")
            finish_jobs(database, jobs, "interrupted")
        finish_operation_run(database, operation_run_id, "paused", str(exc))
        database.commit()
        emit(callback, ProgressEvent("network_paused", str(exc)))
        raise
    except RateLimitDeferred as exc:
        with database:
            database.execute("UPDATE captures SET state='pending' WHERE state='downloading'")
            database.execute("UPDATE captures SET state='downloaded_unscanned' WHERE state='scanning'")
            database.execute("UPDATE media_captures SET state='pending' WHERE state='downloading'")
            finish_jobs(database, jobs, "interrupted")
        finish_operation_run(database, operation_run_id, "interrupted", str(exc))
        database.commit()
        emit(
            callback,
            ProgressEvent(
                "rate_limit",
                f"Wayback stayed rate limited beyond the wait budget. Progress was saved; use Resume later. {exc}",
            ),
        )
        raise
    except Stopped:
        with database:
            database.execute("UPDATE captures SET state='pending' WHERE state='downloading'")
            database.execute("UPDATE captures SET state='downloaded_unscanned' WHERE state='scanning'")
            database.execute("UPDATE media_captures SET state='pending' WHERE state='downloading'")
            finish_jobs(database, jobs, "interrupted")
        finish_operation_run(database, operation_run_id, "interrupted", "Stopped by user")
        database.commit()
        emit(callback, ProgressEvent("stopped", "Stopped. Progress was saved and can be resumed."))
        raise
    except Exception as exc:
        # Never leave active queue rows stranded after a local programming,
        # parsing, database, or filesystem failure. They remain resumable even
        # before the project is reopened and crash-recovery runs.
        with database:
            database.execute("UPDATE captures SET state='pending' WHERE state='downloading'")
            database.execute("UPDATE captures SET state='downloaded_unscanned' WHERE state='scanning'")
            database.execute("UPDATE media_captures SET state='pending' WHERE state='downloading'")
            if jobs:
                finish_jobs(database, jobs, "failed")
        finish_operation_run(database, operation_run_id, "failed", f"{type(exc).__name__}: {exc}")
        database.commit()
        raise
    finally:
        # Once worker pools have drained, checkpoint WAL so a clean Pause & Save
        # or normal shutdown has a small, self-contained durable database.
        try:
            database.commit()
            database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        database.close()
