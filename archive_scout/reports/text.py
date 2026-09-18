from __future__ import annotations

import csv
import json
import os
import shutil
import sqlite3
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from typing import Iterable, Iterator, TextIO

from ..config import ProjectConfig, ReportConfig
from ..downloads.downloader import replay_url
from ..database.repositories import apply_report_storage_policy
from ..utils import atomic_text_writer, atomic_write_lines, atomic_write_text, json_value, utc_now

REPORT_FILENAMES = {
    "matches_ranked": "matches_ranked.txt",
    "matched_urls": "matched_urls.txt",
    "wayback_urls": "wayback_urls.txt",
    "interesting_links": "interesting_links.txt",
    "keyword_counts": "keyword_counts.txt",
    "all_indexed_urls": "all_indexed_urls.txt",
    "errors": "errors.txt",
    "site_issues": "site_issues.txt",
    "summary": "summary.txt",
}
REPORT_NAMES = tuple(REPORT_FILENAMES.values())

ALL_RANKED_SELECT = """
    SELECT m.*,d.path,d.title,d.size_bytes,c.original_url,c.timestamp,c.mimetype,c.state,
           sr.status AS scan_status,ks.name AS keyword_set_name,
           COALESCE(r.status,'unreviewed') AS review_status,
           COALESCE((SELECT text FROM notes n WHERE n.match_id=m.id ORDER BY n.id LIMIT 1),'') AS note,
           COALESCE((SELECT GROUP_CONCAT(t.name, ', ') FROM match_tags mt JOIN tags t ON t.id=mt.tag_id WHERE mt.match_id=m.id),'') AS tags
    FROM document_matches m
    JOIN scan_runs sr ON sr.id=m.scan_run_id
    JOIN keyword_sets ks ON ks.id=sr.keyword_set_id
    JOIN documents d ON d.id=m.document_id
    JOIN captures c ON c.id=d.capture_id
    LEFT JOIN reviews r ON r.match_id=m.id
    WHERE m.score>=sr.minimum_score AND m.excluded=0 AND m.required_missing=0
    ORDER BY m.score DESC,c.timestamp,c.original_url,m.scan_run_id
"""

RANKED_SELECT = """
    SELECT m.*,d.path,d.title,d.size_bytes,c.original_url,c.timestamp,c.mimetype,c.state,
           COALESCE(r.status,'unreviewed') AS review_status,
           COALESCE((SELECT text FROM notes n WHERE n.match_id=m.id ORDER BY n.id LIMIT 1),'') AS note,
           COALESCE((SELECT GROUP_CONCAT(t.name, ', ') FROM match_tags mt JOIN tags t ON t.id=mt.tag_id WHERE mt.match_id=m.id),'') AS tags
    FROM document_matches m
    JOIN documents d ON d.id=m.document_id
    JOIN captures c ON c.id=d.capture_id
    LEFT JOIN reviews r ON r.match_id=m.id
    WHERE m.scan_run_id=? AND m.score>=? AND m.excluded=0 AND m.required_missing=0
    ORDER BY m.score DESC,c.timestamp,c.original_url
"""

MATCH_URL_SELECT = """
    SELECT c.original_url,c.timestamp
    FROM document_matches m
    JOIN documents d ON d.id=m.document_id
    JOIN captures c ON c.id=d.capture_id
    WHERE m.scan_run_id=? AND m.score>=? AND m.excluded=0 AND m.required_missing=0
    ORDER BY m.score DESC,c.timestamp,c.original_url
"""

ERROR_QUERY = """
    SELECT e.*,c.timestamp,c.original_url,d.path
    FROM errors e
    LEFT JOIN captures c ON c.id=e.capture_id
    LEFT JOIN documents d ON d.id=e.document_id
    WHERE e.resolved=0
    ORDER BY e.operation,e.category,e.last_seen,e.id
"""

SITE_ISSUE_QUERY = """
    SELECT host,stage,category,http_status,occurrence_count,last_seen,message
    FROM site_issues WHERE resolved=0 ORDER BY last_seen DESC,id DESC
"""


def safe_run_name(value: str) -> str:
    cleaned = "".join(character if character.isalnum() or character in "-_" else "-" for character in value.strip())
    return cleaned.strip("-")[:60] or "scan"


def _copy_latest(run_path: Path, latest_path: Path) -> None:
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        latest_path.unlink(missing_ok=True)
        os.link(run_path, latest_path)
    except OSError:
        shutil.copyfile(run_path, latest_path)


ALL_MATCHES_CSV_FIELDS = (
    "rank", "score", "scan_run", "scan_status", "keyword_set", "timestamp", "title",
    "original_url", "wayback_url", "local_file", "mime_type", "review_status", "tags",
    "note", "keyword_hits", "hit_fields", "snippets", "interesting_links",
)


def _markdown_text(value: object) -> str:
    text = " ".join(str(value or "").split())
    for character in ("\\", "`", "*", "_", "{", "}", "[", "]", "<", ">", "#", "|"):
        text = text.replace(character, "\\" + character)
    return text


def _write_all_match_text(handle: TextIO, rank: int, row: sqlite3.Row, hits: dict, fields: dict, snippets: list, links: list, enabled: set[str]) -> None:
    hit_lines = [
        f"{label}={count} [{','.join(fields.get(label, []))}]"
        for label, count in sorted(hits.items(), key=lambda item: (-item[1], item[0].casefold()))
    ]
    snippet_lines = [f"  {index}. {snippet}" for index, snippet in enumerate(snippets, 1)] or ["  None"]
    link_lines = [f"  {link}" for link in links] or ["  None"]
    values = {
        "rank": f"RANK: {rank}",
        "score": f"SCORE: {row['score']}",
        "scan_run": f"SCAN RUN: {row['scan_run_id']}",
        "scan_status": f"SCAN STATUS: {row['scan_status']}",
        "keyword_set": f"KEYWORD SET: {row['keyword_set_name']}",
        "timestamp": f"TIMESTAMP: {row['timestamp']}",
        "title": f"TITLE: {row['title'] or '(untitled)'}",
        "original_url": f"ORIGINAL URL: {row['original_url']}",
        "wayback_url": f"WAYBACK URL: {replay_url(row['timestamp'], row['original_url'])}",
        "local_file": f"LOCAL FILE: {row['path']}",
        "mime_type": f"MIME TYPE: {row['mimetype'] or '(unknown)'}",
        "review_status": f"REVIEW STATUS: {row['review_status']}",
        "tags": f"TAGS: {row['tags'] or '(none)'}",
        "note": f"NOTE: {row['note'] or '(none)'}",
        "keyword_hits": f"KEYWORD HITS: {'; '.join(hit_lines) if hit_lines else 'None'}",
        "snippets": "SNIPPETS:\n" + "\n".join(snippet_lines),
        "interesting_links": "INTERESTING LINKS:\n" + "\n".join(link_lines),
    }
    handle.write("\n".join(["=" * 100, *(value for field, value in values.items() if field in enabled), "", ""]))


def _write_all_match_markdown(handle: TextIO, rank: int, row: sqlite3.Row, hits: dict, fields: dict, snippets: list, links: list, enabled: set[str]) -> None:
    title = _markdown_text(row["title"] or "Untitled match") if "title" in enabled else "Match"
    original = str(row["original_url"]).replace("<", "%3C").replace(">", "%3E")
    wayback = replay_url(str(row["timestamp"]), str(row["original_url"])).replace("<", "%3C").replace(">", "%3E")
    hit_text = "; ".join(
        f"{_markdown_text(label)} × {count} ({_markdown_text(', '.join(fields.get(label, [])))})"
        for label, count in sorted(hits.items(), key=lambda item: (-item[1], item[0].casefold()))
    ) or "None"
    prefix = f"{rank}. " if "rank" in enabled else ""
    handle.write(f"\n## {prefix}{title}\n\n")
    values = {
        "score": f"**Score:** {row['score']}",
        "scan_run": f"**Scan:** {row['scan_run_id']} ({_markdown_text(row['scan_status'])})",
        "keyword_set": f"**Keyword set:** {_markdown_text(row['keyword_set_name'])}",
        "timestamp": f"**Capture:** `{_markdown_text(row['timestamp'])}`",
        "original_url": f"**Original URL:** <{original}>",
        "wayback_url": f"**Wayback URL:** <{wayback}>",
        "local_file": f"**Local file:** {_markdown_text(row['path'])}",
        "mime_type": f"**MIME type:** {_markdown_text(row['mimetype'] or 'unknown')}",
        "review_status": f"**Review:** {_markdown_text(row['review_status'])}",
        "tags": f"**Tags:** {_markdown_text(row['tags'] or 'None')}",
        "note": f"**Note:** {_markdown_text(row['note'] or 'None')}",
        "keyword_hits": f"**Keyword hits:** {hit_text}",
    }
    for field, value in values.items():
        if field in enabled:
            handle.write(f"- {value}\n")
    if "snippets" in enabled:
        handle.write("\n### Snippets\n\n")
        for snippet in snippets:
            handle.write(f"> {_markdown_text(snippet)}\n>\n")
        if not snippets:
            handle.write("None.\n")
    if "interesting_links" in enabled:
        handle.write("\n### Interesting links\n\n")
        for link in links:
            safe_link = str(link).replace("<", "%3C").replace(">", "%3E")
            handle.write(f"- <{safe_link}>\n")
        if not links:
            handle.write("None.\n")


def generate_all_matches_reports(
    output_dir: Path,
    database: sqlite3.Connection,
    report: ReportConfig | None = None,
) -> dict[str, Path]:
    """Write combined Markdown, spreadsheet CSV, and legacy text reports in one pass."""
    root = output_dir / "reports"
    paths = {
        "all_matches_markdown": root / "all_matches_ranked.md",
        "all_matches_csv": root / "all_matches_ranked.csv",
        "all_matches_ranked": root / "all_matches_ranked.txt",
    }
    report = (report or ReportConfig()).normalized()
    if not report.output_enabled("matches_ranked"):
        for path in paths.values():
            path.unlink(missing_ok=True)
            path.with_name(path.name + ".zip").unlink(missing_ok=True)
        return {}
    enabled = set(report.ranked_fields)
    if "scan_run" in enabled:
        enabled.update(("scan_status", "keyword_set"))
    if "keyword_hits" in enabled:
        enabled.add("hit_fields")
    csv_fields = [field for field in ALL_MATCHES_CSV_FIELDS if field in enabled]
    with ExitStack() as stack:
        markdown = stack.enter_context(atomic_text_writer(paths["all_matches_markdown"]))
        spreadsheet = stack.enter_context(atomic_text_writer(paths["all_matches_csv"]))
        text = stack.enter_context(atomic_text_writer(paths["all_matches_ranked"]))
        csv_writer = csv.DictWriter(spreadsheet, fieldnames=csv_fields, extrasaction="ignore")
        csv_writer.writeheader()
        markdown.write("# Archive Scout combined qualifying matches\n\n")
        markdown.write("Includes original, interrupted, and resumed scan runs.\n")
        text.write("Archive Scout combined qualifying matches\n")
        text.write("Includes original, interrupted, and resumed scan runs.\n\n")
        found = False
        for rank, row in enumerate(database.execute(ALL_RANKED_SELECT), 1):
            found = True
            hits = json_value(row["hits_json"], {})
            fields = json_value(row["fields_json"], {})
            snippets = json_value(row["snippets_json"], [])
            links = json_value(row["interesting_links_json"], [])
            _write_all_match_text(text, rank, row, hits, fields, snippets, links, enabled)
            _write_all_match_markdown(markdown, rank, row, hits, fields, snippets, links, enabled)
            csv_writer.writerow({
                "rank": rank,
                "score": row["score"],
                "scan_run": row["scan_run_id"],
                "scan_status": row["scan_status"],
                "keyword_set": row["keyword_set_name"],
                "timestamp": row["timestamp"],
                "title": row["title"] or "",
                "original_url": row["original_url"],
                "wayback_url": replay_url(row["timestamp"], row["original_url"]),
                "local_file": row["path"],
                "mime_type": row["mimetype"] or "",
                "review_status": row["review_status"],
                "tags": row["tags"] or "",
                "note": row["note"] or "",
                "keyword_hits": json.dumps(hits, ensure_ascii=False, sort_keys=True),
                "hit_fields": json.dumps(fields, ensure_ascii=False, sort_keys=True),
                "snippets": json.dumps(snippets, ensure_ascii=False),
                "interesting_links": json.dumps(links, ensure_ascii=False),
            })
        if not found:
            message = "No qualifying matches have been found for this project."
            markdown.write(f"\n{message}\n")
            text.write(message + "\n")
    return paths


def generate_all_matches_report(output_dir: Path, database: sqlite3.Connection) -> Path:
    """Backward-compatible entry point returning the legacy text report."""
    return generate_all_matches_reports(output_dir, database)["all_matches_ranked"]


def _remove_report(root_reports: Path, name: str, run_dir: Path | None = None) -> None:
    filename = REPORT_FILENAMES[name]
    (root_reports / filename).unlink(missing_ok=True)
    if run_dir is not None:
        (run_dir / filename).unlink(missing_ok=True)


def _write_report(
    root_reports: Path,
    name: str,
    lines: Iterable[str],
    *,
    run_dir: Path | None = None,
) -> Path:
    filename = REPORT_FILENAMES[name]
    if run_dir is None:
        path = root_reports / filename
        atomic_write_lines(path, lines)
        return path
    run_path = run_dir / filename
    atomic_write_lines(run_path, lines)
    latest = root_reports / filename
    _copy_latest(run_path, latest)
    return latest


def _tab_line(values: dict[str, object], fields: list[str]) -> str:
    return "\t".join(str(values.get(field, "") if values.get(field, "") is not None else "") for field in fields)


def _summary_lines(values: dict[str, str], fields: list[str]) -> Iterator[str]:
    for field in fields:
        value = values.get(field)
        if value is not None:
            yield value


def _indexed_url_lines(database: sqlite3.Connection, fields: list[str]) -> Iterator[str]:
    if not fields:
        return
    for row in database.execute(
        "SELECT timestamp,mimetype,state,original_url FROM captures ORDER BY original_url,timestamp"
    ):
        yield _tab_line(
            {
                "timestamp": row["timestamp"],
                "mime_type": row["mimetype"] or "",
                "state": row["state"],
                "original_url": row["original_url"],
            },
            fields,
        )


def _error_lines(database: sqlite3.Connection, fields: list[str]) -> Iterator[str]:
    if not fields:
        return
    for row in database.execute(ERROR_QUERY):
        yield _tab_line(
            {
                "last_seen": row["last_seen"] or "",
                "operation": row["operation"],
                "category": row["category"],
                "attempts": row["attempt_count"],
                "retryable": bool(row["retryable"]),
                "http_status": row["http_status"] or "",
                "timestamp": row["timestamp"] or "",
                "source": row["original_url"] or row["path"] or "",
                "message": row["message"] or "",
            },
            fields,
        )


def _site_issue_lines(database: sqlite3.Connection, fields: list[str]) -> Iterator[str]:
    if not fields:
        return
    for row in database.execute(SITE_ISSUE_QUERY):
        yield _tab_line(
            {
                "last_seen": row["last_seen"] or "",
                "host": row["host"] or "",
                "stage": row["stage"],
                "category": row["category"],
                "http_status": int(row["http_status"] or 0) or "",
                "occurrences": int(row["occurrence_count"] or 0),
                "message": row["message"] or "",
            },
            fields,
        )


def generate_index_reports(config: ProjectConfig, database: sqlite3.Connection) -> dict[str, Path]:
    """Write the user-selected reports for a CDX-only project."""
    report = config.report.normalized()
    apply_report_storage_policy(database, report)
    root_reports = config.output_dir / "reports"
    root_reports.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for name in REPORT_FILENAMES:
        if not report.output_enabled(name):
            _remove_report(root_reports, name)

    if report.output_enabled("all_indexed_urls"):
        path = _write_report(
            root_reports,
            "all_indexed_urls",
            _indexed_url_lines(database, report.fields_for("all_indexed_urls")),
        )
        paths["all_indexed_urls"] = path
    else:
        _remove_report(root_reports, "all_indexed_urls")

    if report.output_enabled("errors"):
        path = _write_report(root_reports, "errors", _error_lines(database, report.fields_for("errors")))
        paths["errors"] = path
    else:
        _remove_report(root_reports, "errors")

    if report.output_enabled("site_issues"):
        path = _write_report(
            root_reports, "site_issues", _site_issue_lines(database, report.fields_for("site_issues"))
        )
        paths["site_issues"] = path
    else:
        _remove_report(root_reports, "site_issues")

    if report.output_enabled("summary"):
        capture_count = int(database.execute("SELECT COUNT(*) FROM captures").fetchone()[0])
        state_counts = {
            str(row[0]): int(row[1])
            for row in database.execute("SELECT state,COUNT(*) FROM captures GROUP BY state")
        }
        issue_count = int(database.execute("SELECT COUNT(*) FROM site_issues WHERE resolved=0").fetchone()[0])
        error_count = int(database.execute("SELECT COUNT(*) FROM errors WHERE resolved=0").fetchone()[0])
        values = {
            "heading": "Archive Scout",
            "generated": f"Generated: {utc_now()}",
            "output_directory": f"Output directory: {config.output_dir}",
            "operation": "Operation: Index URLs only",
            "targets": f"Targets: {', '.join(config.targets) or '(none)'}",
            "date_range": f"Date range: {config.from_date}-{config.to_date}",
            "indexed_captures": f"Indexed captures: {capture_count:,}",
            "unresolved_errors": f"Unresolved errors: {error_count:,}",
            "site_issues": f"Open site-specific issues: {issue_count:,}",
            "states": "States: " + ", ".join(f"{key}={value:,}" for key, value in sorted(state_counts.items())),
        }
        path = _write_report(root_reports, "summary", _summary_lines(values, report.fields_for("summary")))
        paths["summary"] = path
    else:
        _remove_report(root_reports, "summary")

    # Scan-only report files may be left from a previous project mode. Do not
    # delete them here: they remain valid artifacts from the last scan run.
    return paths


def generate_reports(
    config: ProjectConfig,
    database: sqlite3.Connection,
    scan_run_id: int,
) -> dict[str, Path]:
    report = config.report.normalized()
    apply_report_storage_policy(database, report)
    run = database.execute(
        """
        SELECT sr.*,ks.name AS keyword_set_name,ks.keywords_json
        FROM scan_runs sr JOIN keyword_sets ks ON ks.id=sr.keyword_set_id WHERE sr.id=?
        """,
        (scan_run_id,),
    ).fetchone()
    if not run:
        raise RuntimeError(f"scan run {scan_run_id} does not exist")

    run_dir = config.output_dir / "reports" / f"scan-{scan_run_id:05d}-{safe_run_name(run['keyword_set_name'])}"
    root_reports = config.output_dir / "reports"
    run_dir.mkdir(parents=True, exist_ok=True)
    root_reports.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    match_count = int(database.execute(
        """
        SELECT COUNT(*) FROM document_matches
        WHERE scan_run_id=? AND score>=? AND excluded=0 AND required_missing=0
        """,
        (scan_run_id, config.minimum_score),
    ).fetchone()[0])

    keyword_counts: Counter[str] = Counter()
    need_keyword_counts = report.output_enabled("keyword_counts") and bool(report.fields_for("keyword_counts"))
    need_interesting_links = report.output_enabled("interesting_links") and bool(report.fields_for("interesting_links"))
    need_ranked = report.output_enabled("matches_ranked")
    need_match_rows = need_ranked or need_keyword_counts or need_interesting_links

    database.execute("DROP TABLE IF EXISTS temp.archive_scout_report_links")
    if need_interesting_links:
        database.execute(
            "CREATE TEMP TABLE archive_scout_report_links(source TEXT NOT NULL,link TEXT NOT NULL,PRIMARY KEY(source,link)) WITHOUT ROWID"
        )

    ranked_fields = report.fields_for("matches_ranked")

    def consume_match_rows(write_ranked: bool) -> Iterator[str]:
        for rank, row in enumerate(database.execute(RANKED_SELECT, (scan_run_id, config.minimum_score)), 1):
            hits = json_value(row["hits_json"], {}) if (need_keyword_counts or "keyword_hits" in ranked_fields) else {}
            fields = json_value(row["fields_json"], {}) if "keyword_hits" in ranked_fields else {}
            snippets = json_value(row["snippets_json"], []) if "snippets" in ranked_fields else []
            links = json_value(row["interesting_links_json"], []) if (need_interesting_links or "interesting_links" in ranked_fields) else []
            if need_keyword_counts:
                keyword_counts.update(hits)
            if need_interesting_links and links:
                database.executemany(
                    "INSERT OR IGNORE INTO archive_scout_report_links(source,link) VALUES(?,?)",
                    ((str(row["original_url"]), str(link)) for link in links),
                )
            if not write_ranked:
                continue

            hit_lines = [
                f"{label}={count} [{','.join(fields.get(label, []))}]"
                for label, count in sorted(hits.items(), key=lambda item: (-item[1], item[0].casefold()))
            ]
            value_lines: dict[str, list[str]] = {
                "rank": [f"RANK: {rank}"],
                "score": [f"SCORE: {row['score']}"],
                "scan_run": [f"SCAN RUN: {scan_run_id}"],
                "timestamp": [f"TIMESTAMP: {row['timestamp']}"],
                "title": [f"TITLE: {row['title'] or '(untitled)'}"],
                "original_url": [f"ORIGINAL URL: {row['original_url']}"],
                "wayback_url": [f"WAYBACK URL: {replay_url(row['timestamp'], row['original_url'])}"],
                "local_file": [f"LOCAL FILE: {row['path']}"],
                "mime_type": [f"MIME TYPE: {row['mimetype'] or '(unknown)'}"],
                "review_status": [f"REVIEW STATUS: {row['review_status']}"],
                "tags": [f"TAGS: {row['tags'] or '(none)'}"],
                "note": [f"NOTE: {row['note'] or '(none)'}"],
                "keyword_hits": [f"KEYWORD HITS: {'; '.join(hit_lines) if hit_lines else 'None'}"],
                "snippets": ["SNIPPETS:", *([f"  {i}. {value}" for i, value in enumerate(snippets, 1)] or ["  None"])],
                "interesting_links": ["INTERESTING LINKS:", *([f"  {link}" for link in links] or ["  None"])],
            }
            lines = ["=" * 100] if ranked_fields else []
            for field in ranked_fields:
                lines.extend(value_lines[field])
            if lines:
                lines.append("")
                yield "\n".join(lines)

    if need_match_rows:
        if need_ranked:
            path = _write_report(root_reports, "matches_ranked", consume_match_rows(True), run_dir=run_dir)
            paths["matches_ranked"] = path
        else:
            for _ in consume_match_rows(False):
                pass
            _remove_report(root_reports, "matches_ranked", run_dir)
    else:
        _remove_report(root_reports, "matches_ranked", run_dir)

    if report.output_enabled("matched_urls"):
        fields = report.fields_for("matched_urls")

        def matched_urls() -> Iterator[str]:
            if not fields:
                return
            seen: set[str] = set()
            for row in database.execute(MATCH_URL_SELECT, (scan_run_id, config.minimum_score)):
                value = str(row["original_url"])
                if value not in seen:
                    seen.add(value)
                    yield _tab_line({"original_url": value}, fields)

        path = _write_report(root_reports, "matched_urls", matched_urls(), run_dir=run_dir)
        paths["matched_urls"] = path
    else:
        _remove_report(root_reports, "matched_urls", run_dir)

    if report.output_enabled("wayback_urls"):
        fields = report.fields_for("wayback_urls")

        def wayback_urls() -> Iterator[str]:
            if not fields:
                return
            seen: set[str] = set()
            for row in database.execute(MATCH_URL_SELECT, (scan_run_id, config.minimum_score)):
                value = replay_url(str(row["timestamp"]), str(row["original_url"]))
                if value not in seen:
                    seen.add(value)
                    yield _tab_line({"wayback_url": value}, fields)

        path = _write_report(root_reports, "wayback_urls", wayback_urls(), run_dir=run_dir)
        paths["wayback_urls"] = path
    else:
        _remove_report(root_reports, "wayback_urls", run_dir)

    if report.output_enabled("interesting_links"):
        fields = report.fields_for("interesting_links")

        def interesting_lines() -> Iterator[str]:
            if not fields:
                return
            if fields == ["source_url"]:
                rows = database.execute("SELECT DISTINCT source FROM archive_scout_report_links ORDER BY source")
            elif fields == ["link"]:
                rows = database.execute("SELECT DISTINCT link FROM archive_scout_report_links ORDER BY link")
            else:
                rows = database.execute("SELECT source,link FROM archive_scout_report_links ORDER BY source,link")
            seen: set[str] = set()
            for row in rows:
                values = {
                    "source_url": row["source"] if "source" in row.keys() else "",
                    "link": row["link"] if "link" in row.keys() else "",
                }
                line = _tab_line(values, fields)
                if line not in seen:
                    seen.add(line)
                    yield line

        path = _write_report(root_reports, "interesting_links", interesting_lines(), run_dir=run_dir)
        paths["interesting_links"] = path
    else:
        _remove_report(root_reports, "interesting_links", run_dir)

    if report.output_enabled("keyword_counts"):
        fields = report.fields_for("keyword_counts")
        lines = (
            _tab_line({"count": count, "keyword": label}, fields)
            for label, count in keyword_counts.most_common()
        ) if fields else ()
        path = _write_report(root_reports, "keyword_counts", lines, run_dir=run_dir)
        paths["keyword_counts"] = path
    else:
        _remove_report(root_reports, "keyword_counts", run_dir)

    if report.output_enabled("all_indexed_urls"):
        path = _write_report(
            root_reports,
            "all_indexed_urls",
            _indexed_url_lines(database, report.fields_for("all_indexed_urls")),
            run_dir=run_dir,
        )
        paths["all_indexed_urls"] = path
    else:
        _remove_report(root_reports, "all_indexed_urls", run_dir)

    if report.output_enabled("errors"):
        path = _write_report(root_reports, "errors", _error_lines(database, report.fields_for("errors")), run_dir=run_dir)
        paths["errors"] = path
    else:
        _remove_report(root_reports, "errors", run_dir)

    if report.output_enabled("site_issues"):
        path = _write_report(
            root_reports, "site_issues", _site_issue_lines(database, report.fields_for("site_issues")), run_dir=run_dir
        )
        paths["site_issues"] = path
    else:
        _remove_report(root_reports, "site_issues", run_dir)

    if report.output_enabled("summary"):
        capture_count = int(database.execute("SELECT COUNT(*) FROM captures").fetchone()[0])
        unresolved_count = int(database.execute("SELECT COUNT(*) FROM errors WHERE resolved=0").fetchone()[0])
        site_issue_count = int(database.execute("SELECT COUNT(*) FROM site_issues WHERE resolved=0").fetchone()[0])
        state_counts = {
            str(row[0]): int(row[1])
            for row in database.execute("SELECT state,COUNT(*) FROM captures GROUP BY state")
        }
        keywords = json.loads(run["keywords_json"])
        values = {
            "heading": "Archive Scout",
            "generated": f"Generated: {utc_now()}",
            "output_directory": f"Output directory: {config.output_dir}",
            "operation": "Operation: Text scan",
            "scan_run": f"Scan run: {scan_run_id}",
            "keyword_set": f"Keyword set: {run['keyword_set_name']}",
            "keyword_rules": f"Keyword rules: {len(keywords):,}",
            "source_operation": f"Scan source operation: {run['source_operation']}",
            "scan_started": f"Scan started: {run['started_at']}",
            "scan_completed": f"Scan completed: {run['completed_at'] or '(not marked complete)'}",
            "targets": f"Targets: {', '.join(config.targets) or '(project database only)'}",
            "date_range": f"Date range: {config.from_date}-{config.to_date}",
            "indexed_captures": f"Indexed captures: {capture_count:,}",
            "ranked_matches": f"Ranked matches at score >= {config.minimum_score}: {match_count:,}",
            "unresolved_errors": f"Unresolved errors: {unresolved_count:,}",
            "site_issues": f"Open site-specific issues: {site_issue_count:,}",
            "states": "States: " + ", ".join(f"{key}={value:,}" for key, value in sorted(state_counts.items())),
        }
        path = _write_report(
            root_reports, "summary", _summary_lines(values, report.fields_for("summary")), run_dir=run_dir
        )
        paths["summary"] = path
    else:
        _remove_report(root_reports, "summary", run_dir)

    paths.update(generate_all_matches_reports(config.output_dir, database, report))

    atomic_write_text(root_reports / "latest_scan_run.txt", f"{scan_run_id}\n{run_dir}\n")
    paths["scan_folder"] = run_dir
    return paths
