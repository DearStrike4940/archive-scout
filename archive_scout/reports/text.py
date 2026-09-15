from __future__ import annotations

import csv
import json
import os
import shutil
import sqlite3
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from typing import Iterator, TextIO

from ..config import ProjectConfig
from ..downloads.downloader import replay_url
from ..utils import atomic_text_writer, atomic_write_lines, atomic_write_text, json_value, utc_now

REPORT_NAMES = (
    "all_matches_ranked.md",
    "all_matches_ranked.csv",
    "all_matches_ranked.txt",
    "matches_ranked.txt",
    "matched_urls.txt",
    "wayback_urls.txt",
    "interesting_links.txt",
    "keyword_counts.txt",
    "all_indexed_urls.txt",
    "errors.txt",
    "site_issues.txt",
    "summary.txt",
)

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


def _write_all_match_text(handle: TextIO, rank: int, row: sqlite3.Row, hits: dict, fields: dict, snippets: list, links: list) -> None:
    hit_lines = [
        f"{label}={count} [{','.join(fields.get(label, []))}]"
        for label, count in sorted(hits.items(), key=lambda item: (-item[1], item[0].casefold()))
    ]
    snippet_lines = [f"  {index}. {snippet}" for index, snippet in enumerate(snippets, 1)] or ["  None"]
    link_lines = [f"  {link}" for link in links] or ["  None"]
    handle.write("\n".join([
        "=" * 100,
        f"RANK: {rank}",
        f"SCORE: {row['score']}",
        f"SCAN RUN: {row['scan_run_id']}",
        f"SCAN STATUS: {row['scan_status']}",
        f"KEYWORD SET: {row['keyword_set_name']}",
        f"TIMESTAMP: {row['timestamp']}",
        f"TITLE: {row['title'] or '(untitled)'}",
        f"ORIGINAL URL: {row['original_url']}",
        f"WAYBACK URL: {replay_url(row['timestamp'], row['original_url'])}",
        f"LOCAL FILE: {row['path']}",
        f"MIME TYPE: {row['mimetype'] or '(unknown)'}",
        f"REVIEW STATUS: {row['review_status']}",
        f"TAGS: {row['tags'] or '(none)'}",
        f"NOTE: {row['note'] or '(none)'}",
        f"KEYWORD HITS: {'; '.join(hit_lines) if hit_lines else 'None'}",
        "SNIPPETS:",
        *snippet_lines,
        "INTERESTING LINKS:",
        *link_lines,
        "",
        "",
    ]))


def _write_all_match_markdown(handle: TextIO, rank: int, row: sqlite3.Row, hits: dict, fields: dict, snippets: list, links: list) -> None:
    title = _markdown_text(row["title"] or "Untitled match")
    original = str(row["original_url"]).replace("<", "%3C").replace(">", "%3E")
    wayback = replay_url(str(row["timestamp"]), str(row["original_url"])).replace("<", "%3C").replace(">", "%3E")
    hit_text = "; ".join(
        f"{_markdown_text(label)} × {count} ({_markdown_text(', '.join(fields.get(label, [])))})"
        for label, count in sorted(hits.items(), key=lambda item: (-item[1], item[0].casefold()))
    ) or "None"
    handle.write(f"\n## {rank}. {title}\n\n")
    handle.write(f"- **Score:** {row['score']}\n")
    handle.write(f"- **Scan:** {row['scan_run_id']} ({_markdown_text(row['scan_status'])})\n")
    handle.write(f"- **Keyword set:** {_markdown_text(row['keyword_set_name'])}\n")
    handle.write(f"- **Capture:** `{_markdown_text(row['timestamp'])}`\n")
    handle.write(f"- **Original URL:** <{original}>\n")
    handle.write(f"- **Wayback URL:** <{wayback}>\n")
    handle.write(f"- **MIME type:** `{_markdown_text(row['mimetype'] or 'unknown')}`\n")
    handle.write(f"- **Review:** {_markdown_text(row['review_status'])}\n")
    handle.write(f"- **Tags:** {_markdown_text(row['tags'] or 'None')}\n")
    handle.write(f"- **Note:** {_markdown_text(row['note'] or 'None')}\n")
    handle.write(f"- **Keyword hits:** {hit_text}\n")
    handle.write("\n### Snippets\n\n")
    if snippets:
        for snippet in snippets:
            handle.write(f"> {_markdown_text(snippet)}\n>\n")
    else:
        handle.write("None.\n")
    handle.write("\n### Interesting links\n\n")
    if links:
        for link in links:
            safe_link = str(link).replace("<", "%3C").replace(">", "%3E")
            handle.write(f"- <{safe_link}>\n")
    else:
        handle.write("None.\n")


def generate_all_matches_reports(
    output_dir: Path,
    database: sqlite3.Connection,
) -> dict[str, Path]:
    """Write combined Markdown, spreadsheet CSV, and legacy text reports in one pass."""
    root = output_dir / "reports"
    paths = {
        "all_matches_markdown": root / "all_matches_ranked.md",
        "all_matches_csv": root / "all_matches_ranked.csv",
        "all_matches_ranked": root / "all_matches_ranked.txt",
    }
    with ExitStack() as stack:
        markdown = stack.enter_context(atomic_text_writer(paths["all_matches_markdown"]))
        spreadsheet = stack.enter_context(atomic_text_writer(paths["all_matches_csv"]))
        text = stack.enter_context(atomic_text_writer(paths["all_matches_ranked"]))
        csv_writer = csv.DictWriter(spreadsheet, fieldnames=ALL_MATCHES_CSV_FIELDS)
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
            _write_all_match_text(text, rank, row, hits, fields, snippets, links)
            _write_all_match_markdown(markdown, rank, row, hits, fields, snippets, links)
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


def generate_index_reports(config: ProjectConfig, database: sqlite3.Connection) -> dict[str, Path]:
    """Write useful reports for a CDX-only project that has no scan_run row."""
    root_reports = config.output_dir / "reports"
    root_reports.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    indexed_path = root_reports / "all_indexed_urls.txt"
    atomic_write_lines(
        indexed_path,
        (
            f"{row['timestamp']}\t{row['mimetype'] or ''}\t{row['state']}\t{row['original_url']}"
            for row in database.execute(
                "SELECT timestamp,mimetype,state,original_url FROM captures ORDER BY original_url,timestamp"
            )
        ),
    )
    paths["all_indexed_urls"] = indexed_path

    capture_count = int(database.execute("SELECT COUNT(*) FROM captures").fetchone()[0])
    state_counts = {
        str(row[0]): int(row[1])
        for row in database.execute("SELECT state,COUNT(*) FROM captures GROUP BY state")
    }
    issue_count = int(database.execute("SELECT COUNT(*) FROM site_issues WHERE resolved=0").fetchone()[0])
    error_count = int(database.execute("SELECT COUNT(*) FROM errors WHERE resolved=0").fetchone()[0])
    summary_path = root_reports / "summary.txt"
    atomic_write_lines(
        summary_path,
        [
            "Archive Scout",
            f"Generated: {utc_now()}",
            "Operation: Index URLs only",
            f"Targets: {', '.join(config.targets) or '(none)'}",
            f"Date range: {config.from_date}-{config.to_date}",
            f"Indexed captures: {capture_count:,}",
            f"Unresolved errors: {error_count:,}",
            f"Open site-specific issues: {issue_count:,}",
            "States: " + ", ".join(f"{key}={value:,}" for key, value in sorted(state_counts.items())),
        ],
    )
    paths["summary"] = summary_path

    error_path = root_reports / "errors.txt"
    atomic_write_lines(
        error_path,
        (
            "\t".join([
                str(row["last_seen"] or ""),
                f"operation={row['operation']}",
                f"category={row['category']}",
                f"attempts={row['attempt_count']}",
                f"retryable={bool(row['retryable'])}",
                f"status={row['http_status'] or ''}",
                str(row["timestamp"] or ""),
                str(row["original_url"] or row["path"] or ""),
                str(row["message"] or ""),
            ])
            for row in database.execute(
                """SELECT e.*,c.timestamp,c.original_url,d.path
                   FROM errors e
                   LEFT JOIN captures c ON c.id=e.capture_id
                   LEFT JOIN documents d ON d.id=e.document_id
                   WHERE e.resolved=0
                   ORDER BY e.operation,e.category,e.last_seen,e.id"""
            )
        ),
    )
    paths["errors"] = error_path

    issues_path = root_reports / "site_issues.txt"
    atomic_write_lines(
        issues_path,
        (
            "\t".join([
                str(row["last_seen"] or ""),
                str(row["host"] or ""),
                f"stage={row['stage']}",
                f"category={row['category']}",
                f"status={int(row['http_status'] or 0) or ''}",
                f"occurrences={int(row['occurrence_count'] or 0)}",
                str(row["message"] or ""),
            ])
            for row in database.execute(
                """SELECT host,stage,category,http_status,occurrence_count,last_seen,message
                   FROM site_issues WHERE resolved=0 ORDER BY last_seen DESC,id DESC"""
            )
        ),
    )
    paths["site_issues"] = issues_path
    return paths


def generate_reports(
    config: ProjectConfig,
    database: sqlite3.Connection,
    scan_run_id: int,
) -> dict[str, Path]:
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
    capture_count = int(database.execute("SELECT COUNT(*) FROM captures").fetchone()[0])
    unresolved_count = int(database.execute("SELECT COUNT(*) FROM errors WHERE resolved=0").fetchone()[0])
    state_counts = {str(row[0]): int(row[1]) for row in database.execute("SELECT state,COUNT(*) FROM captures GROUP BY state")}

    keyword_counts: Counter[str] = Counter()
    database.execute("DROP TABLE IF EXISTS temp.archive_scout_report_links")
    database.execute(
        "CREATE TEMP TABLE archive_scout_report_links(source TEXT NOT NULL,link TEXT NOT NULL,PRIMARY KEY(source,link)) WITHOUT ROWID"
    )

    def ranked_lines() -> Iterator[str]:
        for rank, row in enumerate(database.execute(RANKED_SELECT, (scan_run_id, config.minimum_score)), 1):
            hits = json_value(row["hits_json"], {})
            fields = json_value(row["fields_json"], {})
            snippets = json_value(row["snippets_json"], [])
            links = json_value(row["interesting_links_json"], [])
            keyword_counts.update(hits)
            if links:
                database.executemany(
                    "INSERT OR IGNORE INTO archive_scout_report_links(source,link) VALUES(?,?)",
                    ((str(row["original_url"]), str(link)) for link in links),
                )
            hit_lines = [
                f"{label}={count} [{','.join(fields.get(label, []))}]"
                for label, count in sorted(hits.items(), key=lambda item: (-item[1], item[0].casefold()))
            ]
            snippet_lines = [f"  {index}. {snippet}" for index, snippet in enumerate(snippets, 1)] or ["  None"]
            link_lines = [f"  {link}" for link in links] or ["  None"]
            yield "\n".join(
                [
                    "=" * 100,
                    f"RANK: {rank}",
                    f"SCORE: {row['score']}",
                    f"SCAN RUN: {scan_run_id}",
                    f"TIMESTAMP: {row['timestamp']}",
                    f"TITLE: {row['title'] or '(untitled)'}",
                    f"ORIGINAL URL: {row['original_url']}",
                    f"WAYBACK URL: {replay_url(row['timestamp'], row['original_url'])}",
                    f"LOCAL FILE: {row['path']}",
                    f"MIME TYPE: {row['mimetype'] or '(unknown)'}",
                    f"REVIEW STATUS: {row['review_status']}",
                    f"TAGS: {row['tags'] or '(none)'}",
                    f"NOTE: {row['note'] or '(none)'}",
                    f"KEYWORD HITS: {'; '.join(hit_lines) if hit_lines else 'None'}",
                    "SNIPPETS:",
                    *snippet_lines,
                    "INTERESTING LINKS:",
                    *link_lines,
                    "",
                ]
            )

    run_path = run_dir / "matches_ranked.txt"
    atomic_write_lines(run_path, ranked_lines())
    latest_path = root_reports / "matches_ranked.txt"
    _copy_latest(run_path, latest_path)
    paths["matches_ranked"] = latest_path

    def matched_urls() -> Iterator[str]:
        seen: set[str] = set()
        for row in database.execute(MATCH_URL_SELECT, (scan_run_id, config.minimum_score)):
            value = str(row["original_url"])
            if value not in seen:
                seen.add(value)
                yield value

    run_path = run_dir / "matched_urls.txt"
    atomic_write_lines(run_path, matched_urls())
    latest_path = root_reports / "matched_urls.txt"
    _copy_latest(run_path, latest_path)
    paths["matched_urls"] = latest_path

    def wayback_urls() -> Iterator[str]:
        seen: set[str] = set()
        for row in database.execute(MATCH_URL_SELECT, (scan_run_id, config.minimum_score)):
            value = replay_url(str(row["timestamp"]), str(row["original_url"]))
            if value not in seen:
                seen.add(value)
                yield value

    run_path = run_dir / "wayback_urls.txt"
    atomic_write_lines(run_path, wayback_urls())
    latest_path = root_reports / "wayback_urls.txt"
    _copy_latest(run_path, latest_path)
    paths["wayback_urls"] = latest_path

    run_path = run_dir / "interesting_links.txt"
    atomic_write_lines(
        run_path,
        (f"{row['source']}\t{row['link']}" for row in database.execute(
            "SELECT source,link FROM archive_scout_report_links ORDER BY source,link"
        )),
    )
    latest_path = root_reports / "interesting_links.txt"
    _copy_latest(run_path, latest_path)
    paths["interesting_links"] = latest_path

    run_path = run_dir / "keyword_counts.txt"
    atomic_write_lines(run_path, (f"{count}\t{label}" for label, count in keyword_counts.most_common()))
    latest_path = root_reports / "keyword_counts.txt"
    _copy_latest(run_path, latest_path)
    paths["keyword_counts"] = latest_path

    run_path = run_dir / "all_indexed_urls.txt"
    atomic_write_lines(
        run_path,
        (
            f"{row['timestamp']}\t{row['mimetype'] or ''}\t{row['state']}\t{row['original_url']}"
            for row in database.execute(
                "SELECT timestamp,mimetype,state,original_url FROM captures ORDER BY original_url,timestamp"
            )
        ),
    )
    latest_path = root_reports / "all_indexed_urls.txt"
    _copy_latest(run_path, latest_path)
    paths["all_indexed_urls"] = latest_path

    error_query = """
        SELECT e.*,c.timestamp,c.original_url,d.path
        FROM errors e
        LEFT JOIN captures c ON c.id=e.capture_id
        LEFT JOIN documents d ON d.id=e.document_id
        WHERE e.resolved=0
        ORDER BY e.operation,e.category,e.last_seen,e.id
    """

    def error_lines() -> Iterator[str]:
        for row in database.execute(error_query):
            yield "\t".join(
                [
                    row["last_seen"],
                    f"operation={row['operation']}",
                    f"category={row['category']}",
                    f"attempts={row['attempt_count']}",
                    f"retryable={bool(row['retryable'])}",
                    f"status={row['http_status'] or ''}",
                    row["timestamp"] or "",
                    row["original_url"] or row["path"] or "",
                    row["message"],
                ]
            )

    run_path = run_dir / "errors.txt"
    atomic_write_lines(run_path, error_lines())
    latest_path = root_reports / "errors.txt"
    _copy_latest(run_path, latest_path)
    paths["errors"] = latest_path

    def site_issue_lines() -> Iterator[str]:
        for row in database.execute(
            """SELECT host,stage,category,http_status,occurrence_count,last_seen,message
               FROM site_issues WHERE resolved=0 ORDER BY last_seen DESC,id DESC"""
        ):
            yield "\t".join([
                str(row["last_seen"] or ""),
                str(row["host"] or ""),
                f"stage={row['stage']}",
                f"category={row['category']}",
                f"status={int(row['http_status'] or 0) or ''}",
                f"occurrences={int(row['occurrence_count'] or 0)}",
                str(row["message"] or ""),
            ])

    run_path = run_dir / "site_issues.txt"
    atomic_write_lines(run_path, site_issue_lines())
    latest_path = root_reports / "site_issues.txt"
    _copy_latest(run_path, latest_path)
    paths["site_issues"] = latest_path

    site_issue_count = int(database.execute(
        "SELECT COUNT(*) FROM site_issues WHERE resolved=0"
    ).fetchone()[0])
    keywords = json.loads(run["keywords_json"])
    summary_lines = [
        "Archive Scout",
        f"Generated: {utc_now()}",
        f"Output directory: {config.output_dir}",
        f"Scan run: {scan_run_id}",
        f"Keyword set: {run['keyword_set_name']}",
        f"Keyword rules: {len(keywords):,}",
        f"Scan source operation: {run['source_operation']}",
        f"Scan started: {run['started_at']}",
        f"Scan completed: {run['completed_at'] or '(not marked complete)'}",
        f"Targets: {', '.join(config.targets) or '(project database only)'}",
        f"Date range: {config.from_date}-{config.to_date}",
        f"Indexed captures: {capture_count:,}",
        f"Ranked matches at score >= {config.minimum_score}: {match_count:,}",
        f"Unresolved errors: {unresolved_count:,}",
        f"Open site-specific issues: {site_issue_count:,}",
        "States: " + ", ".join(f"{key}={value:,}" for key, value in sorted(state_counts.items())),
    ]
    run_path = run_dir / "summary.txt"
    atomic_write_lines(run_path, summary_lines)
    latest_path = root_reports / "summary.txt"
    _copy_latest(run_path, latest_path)
    paths["summary"] = latest_path

    paths.update(generate_all_matches_reports(config.output_dir, database))

    atomic_write_text(root_reports / "latest_scan_run.txt", f"{scan_run_id}\n{run_dir}\n")
    paths["scan_folder"] = run_dir
    return paths
