from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import threading
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable

from ..content import decode_bytes, looks_textual_bytes, parse_page
from ..events import ProgressEvent, Stopped
from ..storage import sha256_file
from ..utils import normalize_search, utc_now
from .automaton import LiteralAutomaton


def _normalized_keywords(values: list[str]) -> list[str]:
    unique: dict[str, str] = {}
    for raw in values:
        display = str(raw).strip()
        normalized = normalize_search(display)
        if normalized:
            unique.setdefault(normalized, display)
    return list(unique.values())


def hitlist_fingerprint(values: list[str]) -> str:
    normalized = sorted({normalize_search(value) for value in values if normalize_search(value)})
    return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()


def load_hitlist(keywords: list[str] | None = None, file_path: str | Path = "") -> list[str]:
    values = list(keywords or [])
    if str(file_path or "").strip():
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(path)
        values.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
    return _normalized_keywords(values)


def _count_matches(automaton: LiteralAutomaton, text: str) -> dict[str, int]:
    return automaton.count_non_overlapping(text)


def _resume_or_create_run(database: sqlite3.Connection, keywords: list[str]) -> tuple[int, int]:
    fingerprint = hitlist_fingerprint(keywords)
    row = database.execute(
        """SELECT id,last_capture_id FROM quick_search_runs
           WHERE fingerprint=? AND status IN ('running','interrupted') ORDER BY id DESC LIMIT 1""",
        (fingerprint,),
    ).fetchone()
    now = utc_now()
    if row:
        run_id = int(row["id"])
        database.execute(
            "UPDATE quick_search_runs SET status='running',updated_at=? WHERE id=?", (now, run_id)
        )
        return run_id, int(row["last_capture_id"] or 0)
    cursor = database.execute(
        """INSERT INTO quick_search_runs(fingerprint,keywords_json,status,started_at,updated_at)
           VALUES(?,?,'running',?,?)""",
        (fingerprint, json.dumps(keywords, ensure_ascii=False), now, now),
    )
    return int(cursor.lastrowid), 0


def search_with_hitlist(
    root: Path,
    database: sqlite3.Connection,
    keywords: list[str],
    stop_event: threading.Event,
    callback: Callable[[ProgressEvent], None] | None = None,
    *,
    batch_size: int = 250,
) -> dict[str, object]:
    keywords = _normalized_keywords(keywords)
    if not keywords:
        raise ValueError("Search with Hitlist requires at least one keyword")
    normalized_to_display = {normalize_search(value): value for value in keywords}
    automaton = LiteralAutomaton(normalized_to_display)
    run_id, last_id = _resume_or_create_run(database, keywords)
    database.commit()
    total = int(database.execute("SELECT COUNT(*) FROM captures").fetchone()[0])
    indexed_checked = local_checked = unavailable = 0

    try:
        while True:
            rows = database.execute(
                """SELECT c.id,c.original_url,c.timestamp,c.local_path,c.document_id,c.mimetype,c.detected_encoding,d.path AS document_path
                   FROM captures c LEFT JOIN documents d ON d.id=c.document_id
                   WHERE c.id>? ORDER BY c.id LIMIT ?""",
                (last_id, max(1, int(batch_size))),
            ).fetchall()
            if not rows:
                break
            hit_rows: list[tuple[int, int, str, str, int]] = []
            for row in rows:
                if stop_event.is_set():
                    raise Stopped
                capture_id = int(row["id"])
                last_id = capture_id
                indexed_checked += 1
                fields_by_pattern: dict[str, set[str]] = defaultdict(set)
                counts_by_pattern: Counter[str] = Counter()

                url_counts = _count_matches(automaton, normalize_search(str(row["original_url"])))
                for pattern, count in url_counts.items():
                    counts_by_pattern[pattern] += count
                    fields_by_pattern[pattern].add("url")

                local = str(row["local_path"] or row["document_path"] or "")
                path = Path(local) if local else None
                data = None
                if path:
                    try:
                        if path.resolve().is_relative_to(Path(root).resolve()):
                            data = path.read_bytes()
                    except OSError:
                        pass  # Missing/unreadable files are coverage gaps, not false negatives.
                content_type = str(row["mimetype"] or "")
                if row["detected_encoding"]:
                    content_type += "; charset=" + str(row["detected_encoding"])
                if data is not None and looks_textual_bytes(data[:16384], content_type):
                    local_checked += 1
                    raw = decode_bytes(data, content_type)
                    del data
                    source_counts = _count_matches(automaton, normalize_search(raw))
                    for pattern, count in source_counts.items():
                        counts_by_pattern[pattern] += count
                        fields_by_pattern[pattern].add("source")
                    # Raw markup can split visible phrases. Only pay for a DOM
                    # parse when the raw source did not already contain every
                    # requested literal.
                    if len(source_counts) < len(normalized_to_display):
                        _title, visible, links = parse_page(raw, str(row["original_url"]))
                        view = normalize_search(visible + "\n" + "\n".join(links))
                        for pattern, count in _count_matches(automaton, view).items():
                            if pattern not in source_counts:
                                counts_by_pattern[pattern] += count
                                fields_by_pattern[pattern].add("rendered")
                else:
                    unavailable += 1

                for pattern, count in counts_by_pattern.items():
                    display = normalized_to_display.get(pattern, pattern)
                    hit_rows.append(
                        (run_id, capture_id, display, ",".join(sorted(fields_by_pattern[pattern])), int(count))
                    )

            with database:
                if hit_rows:
                    database.executemany(
                        """INSERT INTO quick_search_hits(run_id,capture_id,keyword,fields,count)
                           VALUES(?,?,?,?,?)
                           ON CONFLICT(run_id,capture_id,keyword) DO UPDATE SET
                           fields=excluded.fields,count=excluded.count""",
                        hit_rows,
                    )
                database.execute(
                    """UPDATE quick_search_runs SET last_capture_id=?,indexed_checked=indexed_checked+?,
                       local_checked=local_checked+?,unavailable_count=unavailable_count+?,
                       match_count=(SELECT COUNT(DISTINCT capture_id) FROM quick_search_hits WHERE run_id=?),updated_at=?
                       WHERE id=?""",
                    (last_id, len(rows), local_checked, unavailable, run_id, utc_now(), run_id),
                )
            # These are per-loop counters in SQL; reset after checkpoint.
            local_checked = 0
            unavailable = 0
            if callback:
                matched = int(database.execute(
                    "SELECT COUNT(DISTINCT capture_id) FROM quick_search_hits WHERE run_id=?", (run_id,)
                ).fetchone()[0])
                callback(ProgressEvent(
                    "hitlist", f"Hitlist search {last_id:,}/{total:,}; matching captures {matched:,}",
                    last_id, total, {"run_id": run_id, "matches": matched},
                ))
        with database:
            database.execute(
                """UPDATE quick_search_runs SET status='complete',completed_at=?,updated_at=?,
                   match_count=(SELECT COUNT(DISTINCT capture_id) FROM quick_search_hits WHERE run_id=?) WHERE id=?""",
                (utc_now(), utc_now(), run_id, run_id),
            )
    except Stopped:
        with database:
            database.execute(
                    "UPDATE quick_search_runs SET status='interrupted',updated_at=? WHERE id=?",
                (utc_now(), run_id),
            )
        raise

    row = database.execute("SELECT * FROM quick_search_runs WHERE id=?", (run_id,)).fetchone()
    report_dir = Path(root) / "reports" / f"hitlist_{run_id}"
    report_dir.mkdir(parents=True, exist_ok=True)
    csv_path = report_dir / "matches.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["keyword", "original_url", "timestamp", "fields", "count", "local_path"])
        for hit in database.execute(
            """SELECT h.keyword,c.original_url,c.timestamp,h.fields,h.count,COALESCE(c.local_path,d.path,'') AS local_path
               FROM quick_search_hits h JOIN captures c ON c.id=h.capture_id
               LEFT JOIN documents d ON d.id=c.document_id WHERE h.run_id=?
               ORDER BY c.id,h.keyword COLLATE NOCASE""",
            (run_id,),
        ):
            writer.writerow(list(hit))
    summary_path = report_dir / "summary.txt"
    summary_path.write_text(
        "Search with Hitlist\n\n"
        f"Run: {run_id}\n"
        f"Indexed URLs checked: {int(row['indexed_checked'] or 0):,}\n"
        f"Local capture contents checked: {int(row['local_checked'] or 0):,}\n"
        f"Captures without local content: {int(row['unavailable_count'] or 0):,}\n"
        f"Matching captures: {int(row['match_count'] or 0):,}\n",
        encoding="utf-8",
    )
    return {
        "run_id": run_id,
        "csv": csv_path,
        "summary": summary_path,
        "indexed_checked": int(row["indexed_checked"] or 0),
        "local_checked": int(row["local_checked"] or 0),
        "unavailable": int(row["unavailable_count"] or 0),
        "matches": int(row["match_count"] or 0),
    }
