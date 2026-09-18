from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from ..constants import REVIEW_STATUSES
from ..document_store import compress_text, document_body
from ..scanning.keywords import keyword_rules_to_lines, parse_keyword_rules, serialize_keyword_rules
from ..utils import utc_now


def get_or_create_target(database: sqlite3.Connection, pattern: str, settings: dict | None = None) -> int:
    row = database.execute("SELECT id FROM targets WHERE pattern=?", (pattern,)).fetchone()
    settings_json = json.dumps(settings or {}, ensure_ascii=False, sort_keys=True)
    if row:
        if settings is not None:
            database.execute("UPDATE targets SET settings_json=? WHERE id=?", (settings_json, row["id"]))
        return int(row["id"])
    cursor = database.execute(
        "INSERT INTO targets(pattern,settings_json,created_at) VALUES(?,?,?)",
        (pattern, settings_json, utc_now()),
    )
    return int(cursor.lastrowid)


_CDX_FIELD_NAMES = ("timestamp", "original", "mimetype", "statuscode", "digest", "length")
_CDX_FIELD_POSITIONS = {name: index for index, name in enumerate(_CDX_FIELD_NAMES)}


def _string_value(value: object) -> str:
    return str(value if value is not None else "")


def _cdx_fields(row: Mapping[str, object] | Sequence[object]) -> tuple[str, str, str, str, str, str]:
    """Extract the compact CDX row once instead of rebuilding lookup state per field."""
    if isinstance(row, Mapping):
        return tuple(_string_value(row.get(name, "")) for name in _CDX_FIELD_NAMES)  # type: ignore[return-value]
    length = len(row)
    return tuple(_string_value(row[index]) if index < length else "" for index in range(6))  # type: ignore[return-value]


def _cdx_value(row: Mapping[str, object] | Sequence[object], name: str) -> str:
    return _cdx_fields(row)[_CDX_FIELD_POSITIONS[name]]


def cdx_row_to_dict(row: Mapping[str, object] | Sequence[object]) -> dict[str, str]:
    timestamp, original, mimetype, statuscode, digest, length = _cdx_fields(row)
    return {
        "timestamp": timestamp,
        "original": original,
        "mimetype": mimetype,
        "statuscode": statuscode,
        "digest": digest,
        "length": length,
    }


def _safe_length(value: str) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _capture_values(
    row: Mapping[str, object] | Sequence[object],
    target_id: int,
    query_signature: str,
    now: str,
) -> tuple:
    timestamp, original, mimetype, statuscode, digest, length = _cdx_fields(row)
    return (
        original,
        timestamp,
        target_id,
        query_signature,
        mimetype,
        statuscode,
        digest,
        _safe_length(length),
        "pending",
        now,
        now,
    )


def upsert_captures(
    database: sqlite3.Connection,
    rows: Iterable[Mapping[str, object] | Sequence[object]],
    target_id: int,
    query_signature: str,
) -> int:
    now = utc_now()
    before = database.total_changes
    statement = """
        INSERT INTO captures(
            original_url,timestamp,target_id,query_signature,mimetype,statuscode,digest,length,state,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(original_url,timestamp,query_signature) DO UPDATE SET
            target_id=excluded.target_id,
            mimetype=excluded.mimetype,
            statuscode=excluded.statuscode,
            digest=excluded.digest,
            length=excluded.length,
            updated_at=excluded.updated_at
        WHERE captures.target_id IS NOT excluded.target_id
           OR captures.mimetype IS NOT excluded.mimetype
           OR captures.statuscode IS NOT excluded.statuscode
           OR captures.digest IS NOT excluded.digest
           OR captures.length IS NOT excluded.length
        """
    batch: list[tuple] = []
    for row in rows:
        batch.append(_capture_values(row, target_id, query_signature, now))
        if len(batch) >= 5000:
            database.executemany(statement, batch)
            batch.clear()
    if batch:
        database.executemany(statement, batch)
    return database.total_changes - before


def upsert_capture(database: sqlite3.Connection, row: dict[str, str], target_id: int, query_signature: str) -> bool:
    existing = database.execute(
        "SELECT 1 FROM captures WHERE original_url=? AND timestamp=? AND query_signature=?",
        (row["original"], row["timestamp"], query_signature),
    ).fetchone()
    upsert_captures(database, [row], target_id, query_signature)
    return existing is None


def keyword_fingerprint(keywords: list[str | dict]) -> str:
    rules = parse_keyword_rules(keywords)
    raw = serialize_keyword_rules(rules)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_or_create_keyword_set(database: sqlite3.Connection, name: str, keywords: list[str | dict]) -> int:
    rules = parse_keyword_rules(keywords)
    lines = keyword_rules_to_lines(rules)
    fingerprint = keyword_fingerprint(lines)
    row = database.execute("SELECT id FROM keyword_sets WHERE fingerprint=?", (fingerprint,)).fetchone()
    now = utc_now()
    rules_json = serialize_keyword_rules(rules)
    keywords_json = json.dumps(lines, ensure_ascii=False)
    if row:
        database.execute(
            "UPDATE keyword_sets SET name=?,keywords_json=?,rules_json=?,updated_at=? WHERE id=?",
            (name, keywords_json, rules_json, now, row["id"]),
        )
        return int(row["id"])
    cursor = database.execute(
        "INSERT INTO keyword_sets(name,fingerprint,keywords_json,rules_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        (name, fingerprint, keywords_json, rules_json, now, now),
    )
    return int(cursor.lastrowid)


def list_keyword_sets(database: sqlite3.Connection) -> list[sqlite3.Row]:
    return database.execute("SELECT * FROM keyword_sets ORDER BY name COLLATE NOCASE,id").fetchall()


def start_scan_run(
    database: sqlite3.Connection,
    keyword_set_id: int,
    name: str,
    minimum_score: int,
    source_operation: str,
    metadata: dict | None = None,
) -> int:
    cursor = database.execute(
        """
        INSERT INTO scan_runs(keyword_set_id,name,status,minimum_score,started_at,source_operation,metadata_json)
        VALUES(?,?,'running',?,?,?,?)
        """,
        (keyword_set_id, name, minimum_score, utc_now(), source_operation, json.dumps(metadata or {}, ensure_ascii=False)),
    )
    return int(cursor.lastrowid)


def finish_scan_run(database: sqlite3.Connection, scan_run_id: int, status: str = "complete") -> None:
    started = database.execute("SELECT started_at FROM scan_runs WHERE id=?", (scan_run_id,)).fetchone()
    completed = utc_now()
    document_count = database.execute(
        "SELECT COUNT(*) FROM document_matches WHERE scan_run_id=?", (scan_run_id,)
    ).fetchone()[0]
    minimum = database.execute("SELECT minimum_score FROM scan_runs WHERE id=?", (scan_run_id,)).fetchone()
    minimum_score = int(minimum[0]) if minimum else 1
    match_count = database.execute(
        "SELECT COUNT(*) FROM document_matches WHERE scan_run_id=? AND score>=? AND excluded=0 AND required_missing=0",
        (scan_run_id, minimum_score),
    ).fetchone()[0]
    duration = 0.0
    if started:
        try:
            from datetime import datetime
            duration = max(0.0, (datetime.fromisoformat(completed) - datetime.fromisoformat(started[0])).total_seconds())
        except Exception:
            duration = 0.0
    database.execute(
        """
        UPDATE scan_runs SET status=?,completed_at=?,document_count=?,match_count=?,duration_seconds=? WHERE id=?
        """,
        (status, completed, int(document_count), int(match_count), float(duration), scan_run_id),
    )


def latest_scan_run(database: sqlite3.Connection, keyword_set_id: int | None = None) -> int | None:
    if keyword_set_id is None:
        row = database.execute(
            "SELECT id FROM scan_runs WHERE status='complete' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    else:
        row = database.execute(
            "SELECT id FROM scan_runs WHERE status='complete' AND keyword_set_id=? ORDER BY id DESC LIMIT 1",
            (keyword_set_id,),
        ).fetchone()
    return int(row["id"]) if row else None


def list_scan_runs(database: sqlite3.Connection) -> list[sqlite3.Row]:
    return database.execute(
        """
        SELECT sr.*,ks.name AS keyword_set_name
        FROM scan_runs sr JOIN keyword_sets ks ON ks.id=sr.keyword_set_id
        ORDER BY sr.id DESC
        """
    ).fetchall()


def rename_scan_run(database: sqlite3.Connection, scan_run_id: int, name: str) -> None:
    database.execute("UPDATE scan_runs SET name=? WHERE id=?", (name.strip() or f"Scan {scan_run_id}", scan_run_id))


def delete_scan_run(database: sqlite3.Connection, scan_run_id: int) -> None:
    database.execute("DELETE FROM scan_runs WHERE id=?", (scan_run_id,))


def _fts_enabled(database: sqlite3.Connection) -> bool:
    row = database.execute("SELECT value FROM project_meta WHERE key='fts5'").fetchone()
    return bool(row and row["value"] == "1")


def _fts_replace_document(
    database: sqlite3.Connection,
    document_id: int,
    title: str,
    body_text: str,
    original_url: str,
    old_row: sqlite3.Row | None = None,
) -> None:
    if not _fts_enabled(database):
        return
    # External-content FTS stores only the inverted index.  Deleting an indexed
    # row requires the old token stream, so recover it from the compact body
    # cache when an existing document changes.
    if old_row is not None:
        old_title = str(old_row["title"] or "")
        old_original = str(old_row["original_url"] or original_url)
        old_body = document_body(old_row)
        try:
            database.execute(
                "INSERT INTO documents_fts(documents_fts,rowid,title,body_text,original_url) VALUES('delete',?,?,?,?)",
                (document_id, old_title, old_body, old_original),
            )
        except sqlite3.OperationalError:
            # Older SQLite builds may not support the delete command on the
            # exact external-content shape. Rebuild-on-repair remains safe.
            pass
    database.execute(
        "INSERT INTO documents_fts(rowid,title,body_text,original_url) VALUES(?,?,?,?)",
        (document_id, title, body_text, original_url),
    )


def upsert_document(
    database: sqlite3.Connection,
    capture_id: int,
    path: Path,
    title: str,
    body_text: str,
    links: list[str],
    content_hash: str,
    normalized_hash: str,
    size_bytes: int,
) -> int:
    now = utc_now()
    capture = database.execute(
        "SELECT original_url FROM captures WHERE id=?", (capture_id,)
    ).fetchone()
    original = str(capture["original_url"] if capture else "")
    row = database.execute(
        """SELECT id,path,title,body_text,body_zlib,body_chars,original_url,links_json,
                  content_hash,normalized_hash,size_bytes
           FROM documents WHERE capture_id=?""",
        (capture_id,),
    ).fetchone()
    links_json = json.dumps(links, ensure_ascii=False)
    # The canonical replay file is authoritative. Avoid storing another full
    # body in SQLite when that file is safely present; compressed body_zlib is
    # only a fallback for imported/legacy records without a durable local file.
    body_blob = None if Path(path).is_file() else compress_text(body_text)
    document_changed = True
    if row:
        document_id = int(row["id"])
        document_changed = any(
            (
                str(row["path"] or "") != str(path),
                str(row["title"] or "") != title,
                str(row["links_json"] or "") != links_json,
                str(row["content_hash"] or "") != content_hash,
                str(row["normalized_hash"] or "") != normalized_hash,
                int(row["size_bytes"] or 0) != int(size_bytes),
                int(row["body_chars"] or 0) != len(body_text),
            )
        )
        if document_changed:
            database.execute(
                """
                UPDATE documents SET path=?,title=?,body_text='',body_zlib=?,body_chars=?,original_url=?,
                    links_json=?,content_hash=?,normalized_hash=?,size_bytes=?,updated_at=?
                WHERE id=?
                """,
                (str(path), title, body_blob, len(body_text), original, links_json, content_hash, normalized_hash, size_bytes, now, document_id),
            )
    else:
        cursor = database.execute(
            """
            INSERT INTO documents(capture_id,path,title,body_text,body_zlib,body_chars,original_url,links_json,content_hash,normalized_hash,size_bytes,created_at,updated_at)
            VALUES(?,?,?,'',?,?,?,?,?,?,?,?,?)
            """,
            (capture_id, str(path), title, body_blob, len(body_text), original, links_json, content_hash, normalized_hash, size_bytes, now, now),
        )
        document_id = int(cursor.lastrowid)
    database.execute(
        """UPDATE captures SET document_id=?,state='downloaded',local_path=?,content_hash=?,bytes_saved=?,updated_at=?
           WHERE id=? AND (document_id IS NOT ? OR state IS NOT 'downloaded' OR COALESCE(local_path,'') IS NOT ?
                           OR COALESCE(content_hash,'') IS NOT ? OR bytes_saved IS NOT ?)""",
        (document_id, str(path), content_hash, size_bytes, now, capture_id,
         document_id, str(path), content_hash, int(size_bytes)),
    )
    if document_changed:
        _fts_replace_document(database, document_id, title, body_text, original, row)
    return document_id

def save_match(
    database: sqlite3.Connection,
    scan_run_id: int,
    document_id: int,
    analysis: dict,
    report_config=None,
) -> int:
    now = utc_now()
    store_keyword_counts = report_config is None or bool(report_config.store_keyword_counts)
    store_keyword_fields = report_config is None or bool(report_config.store_keyword_fields)
    store_snippets = report_config is None or bool(report_config.store_snippets)
    store_interesting_links = report_config is None or bool(report_config.store_interesting_links)
    create_default_reviews = report_config is None or bool(report_config.create_default_reviews)
    values = (
        scan_run_id,
        document_id,
        int(round(float(analysis.get("score") or 0))),
        (json.dumps(analysis.get("hits") or {}, ensure_ascii=False, sort_keys=True) if store_keyword_counts else None),
        (json.dumps(analysis.get("hit_fields") or {}, ensure_ascii=False, sort_keys=True) if store_keyword_fields else None),
        (json.dumps(analysis.get("snippets") or [], ensure_ascii=False) if store_snippets else None),
        (json.dumps(analysis.get("interesting_links") or [], ensure_ascii=False) if store_interesting_links else None),
        int(bool(analysis.get("excluded"))),
        int(bool(analysis.get("required_missing"))),
        json.dumps(analysis.get("proximity") or {}, ensure_ascii=False, sort_keys=True),
        now,
        now,
    )
    before = database.total_changes
    database.execute(
        """
        INSERT INTO document_matches(
            scan_run_id,document_id,score,hits_json,fields_json,snippets_json,interesting_links_json,
            excluded,required_missing,proximity_json,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(scan_run_id,document_id) DO UPDATE SET
            score=excluded.score,hits_json=COALESCE(excluded.hits_json,document_matches.hits_json),
            fields_json=COALESCE(excluded.fields_json,document_matches.fields_json),
            snippets_json=COALESCE(excluded.snippets_json,document_matches.snippets_json),
            interesting_links_json=COALESCE(excluded.interesting_links_json,document_matches.interesting_links_json),
            excluded=excluded.excluded,required_missing=excluded.required_missing,
            proximity_json=excluded.proximity_json,updated_at=excluded.updated_at
        WHERE document_matches.score IS NOT excluded.score
           OR document_matches.hits_json IS NOT excluded.hits_json
           OR document_matches.fields_json IS NOT excluded.fields_json
           OR document_matches.snippets_json IS NOT excluded.snippets_json
           OR document_matches.interesting_links_json IS NOT excluded.interesting_links_json
           OR document_matches.excluded IS NOT excluded.excluded
           OR document_matches.required_missing IS NOT excluded.required_missing
           OR document_matches.proximity_json IS NOT excluded.proximity_json
        """,
        values,
    )
    match_changed = database.total_changes > before
    row = database.execute(
        "SELECT id FROM document_matches WHERE scan_run_id=? AND document_id=?",
        (scan_run_id, document_id),
    ).fetchone()
    match_id = int(row["id"])
    if match_changed:
        # hits_json/fields_json are the canonical match representation. Older
        # releases duplicated the same information into keyword_hits, which
        # materially inflated million-page databases without any internal read
        # path using that table. Remove legacy duplicates and do not recreate
        # them for v1.0.6 matches.
        database.execute("DELETE FROM keyword_hits WHERE match_id=?", (match_id,))
    if create_default_reviews:
        database.execute("INSERT OR IGNORE INTO reviews(match_id,status) VALUES(?,'unreviewed')", (match_id,))
    return match_id


def apply_report_storage_policy(database: sqlite3.Connection, report_config) -> int:
    """Compatibility hook: report visibility must never erase past scan data.

    v1.0.7 rewrote every historical match when a checkbox changed. Apart from
    expensive writes, that made regeneration, exports and AI lose evidence.
    Lean enrichment is now a prospective setting handled by save_match only.
    """
    return 0


def record_recovery_event(
    database: sqlite3.Connection,
    stage: str,
    category: str,
    message: str,
    *,
    capture_id: int | None = None,
    media_capture_id: int | None = None,
    details: dict | None = None,
) -> int:
    cursor = database.execute(
        """INSERT INTO recovery_events(stage,category,message,capture_id,media_capture_id,details_json,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (stage, category, message, capture_id, media_capture_id, json.dumps(details or {}, ensure_ascii=False), utc_now()),
    )
    return int(cursor.lastrowid)


def mark_capture_skipped(database: sqlite3.Connection, capture_id: int, reason: str, classifier_revision: int = 1) -> None:
    database.execute(
        "UPDATE captures SET state='skipped',skip_reason=?,classifier_revision=?,updated_at=? WHERE id=?",
        (reason, int(classifier_revision), utc_now(), int(capture_id)),
    )


def requeue_reclassifiable_skips(database: sqlite3.Connection, download_scope: str, classifier_revision: int = 1) -> int:
    reasons = ["ambiguous_metadata", "legacy_non_text"]
    if download_scope == "all_text":
        reasons.append("url_keyword_filter")
    placeholders = ",".join("?" for _ in reasons)
    before = database.total_changes
    database.execute(
        f"""UPDATE captures SET state='pending',skip_reason=NULL,classifier_revision=?,updated_at=?
             WHERE state='skipped' AND (skip_reason IN ({placeholders}) OR classifier_revision<?)""",
        [int(classifier_revision), utc_now(), *reasons, int(classifier_revision)],
    )
    return database.total_changes - before


def record_error(
    database: sqlite3.Connection,
    operation: str,
    category: str,
    message: str,
    capture_id: int | None = None,
    document_id: int | None = None,
    media_capture_id: int | None = None,
    http_status: int | None = None,
    retryable: bool = True,
) -> int:
    now = utc_now()
    # Keep nullable identity columns sargable. Older code wrapped every identity
    # in COALESCE(), which prevented SQLite from using the capture/media error
    # lookup indexes precisely when a failing large project needed them most.
    clauses = ["resolved=0", "ignored=0", "operation=?", "category=?"]
    params: list[object] = [operation, category]
    for column, value in (
        ("capture_id", capture_id),
        ("document_id", document_id),
        ("media_capture_id", media_capture_id),
    ):
        if value is None:
            clauses.append(f"{column} IS NULL")
        else:
            clauses.append(f"{column}=?")
            params.append(int(value))
    row = database.execute(
        "SELECT id,attempt_count FROM errors WHERE "
        + " AND ".join(clauses)
        + " ORDER BY id DESC LIMIT 1",
        params,
    ).fetchone()
    if row:
        database.execute(
            "UPDATE errors SET message=?,http_status=?,attempt_count=?,retryable=?,last_seen=? WHERE id=?",
            (message, http_status, int(row["attempt_count"]) + 1, int(retryable), now, row["id"]),
        )
        return int(row["id"])
    cursor = database.execute(
        """
        INSERT INTO errors(
            capture_id,document_id,media_capture_id,operation,category,message,http_status,
            attempt_count,retryable,resolved,ignored,first_seen,last_seen
        ) VALUES(?,?,?,?,?,?,?,1,?,0,0,?,?)
        """,
        (capture_id, document_id, media_capture_id, operation, category, message, http_status, int(retryable), now, now),
    )
    return int(cursor.lastrowid)


def resolve_errors(
    database: sqlite3.Connection,
    capture_id: int | None = None,
    document_id: int | None = None,
    media_capture_id: int | None = None,
    operations: tuple[str, ...] | None = None,
) -> None:
    clauses = ["resolved=0"]
    params: list[object] = []
    if capture_id is not None:
        clauses.append("capture_id=?")
        params.append(capture_id)
    if document_id is not None:
        clauses.append("document_id=?")
        params.append(document_id)
    if media_capture_id is not None:
        clauses.append("media_capture_id=?")
        params.append(media_capture_id)
    if operations:
        clauses.append("operation IN (" + ",".join("?" for _ in operations) + ")")
        params.extend(operations)
    database.execute("UPDATE errors SET resolved=1,last_seen=? WHERE " + " AND ".join(clauses), (utc_now(), *params))


def ignore_errors(database: sqlite3.Connection, error_ids: list[int], ignored: bool = True) -> None:
    if not error_ids:
        return
    now = utc_now()
    values = [int(value) for value in error_ids]
    for start in range(0, len(values), 500):
        chunk = values[start:start + 500]
        database.execute(
            "UPDATE errors SET ignored=?,last_seen=? WHERE id IN (" + ",".join("?" for _ in chunk) + ")",
            (int(ignored), now, *chunk),
        )


def list_error_categories(database: sqlite3.Connection, unresolved_only: bool = True) -> list[str]:
    where = "WHERE resolved=0 AND ignored=0" if unresolved_only else ""
    return [
        str(row[0])
        for row in database.execute(
            f"SELECT DISTINCT category FROM errors {where} ORDER BY category COLLATE NOCASE"
        )
    ]


def list_errors(
    database: sqlite3.Connection,
    unresolved_only: bool = True,
    category: str = "",
    limit: int = 2000,
    offset: int = 0,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[object] = []
    if unresolved_only:
        clauses.extend(["e.resolved=0", "e.ignored=0"])
    if category:
        clauses.append("e.category=?")
        params.append(category)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.extend([max(1, int(limit)), max(0, int(offset))])
    return database.execute(
        f"""
        SELECT e.*,c.original_url,c.timestamp,d.path,mc.original_url AS media_url,mc.path AS media_path
        FROM errors e
        LEFT JOIN captures c ON c.id=e.capture_id
        LEFT JOIN documents d ON d.id=e.document_id
        LEFT JOIN media_captures mc ON mc.id=e.media_capture_id
        {where}
        ORDER BY e.last_seen DESC,e.id DESC
        LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()


def set_review(database: sqlite3.Connection, match_id: int, status: str, reviewer: str = "") -> None:
    if status not in REVIEW_STATUSES:
        raise ValueError(f"unsupported review status: {status}")
    database.execute(
        """
        INSERT INTO reviews(match_id,status,reviewer,reviewed_at) VALUES(?,?,?,?)
        ON CONFLICT(match_id) DO UPDATE SET status=excluded.status,reviewer=excluded.reviewer,reviewed_at=excluded.reviewed_at
        """,
        (match_id, status, reviewer.strip() or None, utc_now() if status != "unreviewed" else None),
    )


def save_note(database: sqlite3.Connection, match_id: int, text: str, author: str = "") -> None:
    now = utc_now()
    row = database.execute("SELECT id FROM notes WHERE match_id=? ORDER BY id LIMIT 1", (match_id,)).fetchone()
    if row:
        database.execute(
            "UPDATE notes SET text=?,author=?,updated_at=? WHERE id=?",
            (text, author.strip() or None, now, row["id"]),
        )
    elif text.strip():
        database.execute(
            "INSERT INTO notes(match_id,text,author,created_at,updated_at) VALUES(?,?,?,?,?)",
            (match_id, text, author.strip() or None, now, now),
        )


def set_match_tags(database: sqlite3.Connection, match_id: int, tags: list[str]) -> None:
    database.execute("DELETE FROM match_tags WHERE match_id=?", (match_id,))
    names = list(dict.fromkeys(raw.strip() for raw in tags if raw.strip()))
    if not names:
        return
    database.executemany("INSERT OR IGNORE INTO tags(name) VALUES(?)", ((name,) for name in names))
    tag_ids: dict[str, int] = {}
    for offset in range(0, len(names), 500):
        chunk = names[offset:offset + 500]
        placeholders = ",".join("?" for _ in chunk)
        for row in database.execute(
            f"SELECT id,name FROM tags WHERE name IN ({placeholders})",
            chunk,
        ):
            tag_ids[str(row["name"])] = int(row["id"])
    database.executemany(
        "INSERT OR IGNORE INTO match_tags(match_id,tag_id) VALUES(?,?)",
        ((match_id, tag_ids[name]) for name in names),
    )


def _result_filters(
    scan_run_id: int,
    minimum_score: int,
    review_status: str,
    search: str,
) -> tuple[list[str], list[object]]:
    clauses = ["m.scan_run_id=?", "m.score>=?"]
    params: list[object] = [scan_run_id, minimum_score]
    if review_status:
        clauses.append("COALESCE(r.status,'unreviewed')=?")
        params.append(review_status)
    if search.strip():
        clauses.append("(LOWER(c.original_url) LIKE ? OR LOWER(d.title) LIKE ? OR d.id IN (SELECT rowid FROM documents_fts WHERE documents_fts MATCH ?))")
        value = "%" + search.casefold() + "%"
        phrase = '"' + search.replace('"', '""') + '"'
        params.extend([value, value, phrase])
    return clauses, params


def _result_select(clauses: list[str]) -> str:
    return (
        """
        SELECT m.*,d.path,d.title,d.size_bytes,c.original_url,c.timestamp,c.mimetype,
               COALESCE(r.status,'unreviewed') AS review_status,r.reviewer,r.reviewed_at,
               COALESCE((SELECT text FROM notes n WHERE n.match_id=m.id ORDER BY n.id LIMIT 1),'') AS note,
               COALESCE((SELECT GROUP_CONCAT(t.name, ', ') FROM match_tags mt JOIN tags t ON t.id=mt.tag_id WHERE mt.match_id=m.id),'') AS tags
        FROM document_matches m
        JOIN documents d ON d.id=m.document_id
        JOIN captures c ON c.id=d.capture_id
        LEFT JOIN reviews r ON r.match_id=m.id
        WHERE """
        + " AND ".join(clauses)
        + " ORDER BY m.score DESC,c.timestamp,c.original_url,m.id"
    )


def result_rows(
    database: sqlite3.Connection,
    scan_run_id: int,
    minimum_score: int = 0,
    review_status: str = "",
    search: str = "",
    limit: int = 500,
    offset: int = 0,
) -> list[sqlite3.Row]:
    clauses, params = _result_filters(scan_run_id, minimum_score, review_status, search)
    params.extend([max(0, int(limit)), max(0, int(offset))])
    return database.execute(
        _result_select(clauses) + " LIMIT ? OFFSET ?",
        params,
    ).fetchall()


def iter_result_rows(
    database: sqlite3.Connection,
    scan_run_id: int,
    minimum_score: int = 0,
    review_status: str = "",
    search: str = "",
    batch_size: int = 1000,
):
    """Stream ordered result rows from one cursor with bounded resident memory."""
    clauses, params = _result_filters(scan_run_id, minimum_score, review_status, search)
    cursor = database.execute(_result_select(clauses), params)
    try:
        while True:
            rows = cursor.fetchmany(max(1, int(batch_size)))
            if not rows:
                return
            yield from rows
    finally:
        cursor.close()


def result_count(
    database: sqlite3.Connection,
    scan_run_id: int,
    minimum_score: int = 0,
    review_status: str = "",
    search: str = "",
) -> int:
    clauses, params = _result_filters(scan_run_id, minimum_score, review_status, search)
    return int(database.execute(
        """
        SELECT COUNT(*)
        FROM document_matches m
        JOIN documents d ON d.id=m.document_id
        JOIN captures c ON c.id=d.capture_id
        LEFT JOIN reviews r ON r.match_id=m.id
        WHERE """ + " AND ".join(clauses),
        params,
    ).fetchone()[0])

def get_or_create_media_target(database: sqlite3.Connection, pattern: str) -> int:
    row = database.execute("SELECT id FROM media_targets WHERE pattern=?", (pattern,)).fetchone()
    if row:
        return int(row["id"])
    cursor = database.execute(
        "INSERT INTO media_targets(pattern,created_at) VALUES(?,?)", (pattern, utc_now())
    )
    return int(cursor.lastrowid)


def upsert_media_captures(
    database: sqlite3.Connection,
    items: Iterable[tuple[Mapping[str, object] | Sequence[object], str, str]],
    target_id: int | None,
    query_signature: str,
    source_document_id: int | None = None,
    source_type: str = "cdx",
) -> int:
    now = utc_now()
    before = database.total_changes
    statement = """
        INSERT INTO media_captures(
            original_url,timestamp,target_id,source_document_id,source_type,query_signature,media_kind,extension,
            mimetype,statuscode,digest,length,state,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(original_url,timestamp,query_signature) DO UPDATE SET
            target_id=excluded.target_id,
            source_document_id=COALESCE(excluded.source_document_id,media_captures.source_document_id),
            source_type=excluded.source_type,
            media_kind=excluded.media_kind,
            extension=excluded.extension,
            mimetype=excluded.mimetype,
            statuscode=excluded.statuscode,
            digest=excluded.digest,
            length=excluded.length,
            updated_at=excluded.updated_at
        WHERE media_captures.target_id IS NOT excluded.target_id
           OR media_captures.source_document_id IS NOT COALESCE(excluded.source_document_id,media_captures.source_document_id)
           OR media_captures.source_type IS NOT excluded.source_type
           OR media_captures.media_kind IS NOT excluded.media_kind
           OR media_captures.extension IS NOT excluded.extension
           OR media_captures.mimetype IS NOT excluded.mimetype
           OR media_captures.statuscode IS NOT excluded.statuscode
           OR media_captures.digest IS NOT excluded.digest
           OR media_captures.length IS NOT excluded.length
        """
    batch: list[tuple] = []
    for row, media_kind, extension in items:
        timestamp, original, mimetype, statuscode, digest, length = _cdx_fields(row)
        batch.append(
            (
                original,
                timestamp,
                target_id,
                source_document_id,
                source_type,
                query_signature,
                media_kind,
                extension,
                mimetype,
                statuscode,
                digest,
                _safe_length(length),
                "pending",
                now,
                now,
            )
        )
        if len(batch) >= 5000:
            database.executemany(statement, batch)
            batch.clear()
    if batch:
        database.executemany(statement, batch)
    return database.total_changes - before


def upsert_media_capture(
    database: sqlite3.Connection,
    row: dict[str, str],
    target_id: int | None,
    query_signature: str,
    media_kind: str,
    extension: str,
    source_document_id: int | None = None,
    source_type: str = "cdx",
) -> bool:
    existing = database.execute(
        "SELECT 1 FROM media_captures WHERE original_url=? AND timestamp=? AND query_signature=?",
        (row["original"], row["timestamp"], query_signature),
    ).fetchone()
    upsert_media_captures(
        database,
        [(row, media_kind, extension)],
        target_id,
        query_signature,
        source_document_id,
        source_type,
    )
    return existing is None


def save_media_success(
    database: sqlite3.Connection,
    media_capture_id: int,
    path: Path,
    bytes_saved: int,
    content_hash: str,
    http_status: int,
    final_url: str,
) -> None:
    database.execute(
        """
        UPDATE media_captures SET state='downloaded',path=?,bytes_saved=?,content_hash=?,http_status=?,final_url=?,updated_at=?
        WHERE id=?
        """,
        (str(path), int(bytes_saved), content_hash, int(http_status), final_url, utc_now(), media_capture_id),
    )
    resolve_errors(database, media_capture_id=media_capture_id)


def start_operation_run(database: sqlite3.Connection, mode: str, app_version: str) -> int:
    import os
    now = utc_now()
    cursor = database.execute(
        "INSERT INTO operation_runs(mode,status,started_at,updated_at,process_id,app_version) VALUES(?,'running',?,?,?,?)",
        (mode, now, now, os.getpid(), app_version),
    )
    return int(cursor.lastrowid)


def update_operation_run(
    database: sqlite3.Connection,
    operation_run_id: int,
    *,
    message: str = "",
    completed: int | None = None,
    total: int | None = None,
    stage: str = "",
) -> None:
    payload = {"completed": completed, "total": total, "stage": stage}
    database.execute(
        "UPDATE operation_runs SET updated_at=?,message=?,progress_json=? WHERE id=?",
        (utc_now(), message, json.dumps(payload, ensure_ascii=False), operation_run_id),
    )


def finish_operation_run(database: sqlite3.Connection, operation_run_id: int, status: str, message: str = "") -> None:
    now = utc_now()
    database.execute(
        "UPDATE operation_runs SET status=?,message=?,updated_at=?,completed_at=? WHERE id=?",
        (status, message, now, now, operation_run_id),
    )



def start_ai_run(
    database: sqlite3.Connection,
    scan_run_id: int,
    prompt: str,
    model: str,
    candidate_count: int,
    minimum_relevance: int,
    metadata: dict | None = None,
    provider: str = "openai",
) -> int:
    cursor = database.execute(
        """
        INSERT INTO ai_runs(
            scan_run_id,prompt,provider,model,status,candidate_count,result_count,
            minimum_relevance,started_at,metadata_json
        ) VALUES(?,?,?,?,'running',?,0,?,?,?)
        """,
        (
            int(scan_run_id), prompt.strip(), provider.strip().casefold() or "openai", model.strip(), int(candidate_count),
            int(minimum_relevance), utc_now(), json.dumps(metadata or {}, ensure_ascii=False),
        ),
    )
    return int(cursor.lastrowid)


def finish_ai_run(
    database: sqlite3.Connection,
    ai_run_id: int,
    status: str = "complete",
    error_message: str = "",
) -> None:
    result_count = int(database.execute(
        "SELECT COUNT(*) FROM ai_results WHERE ai_run_id=?", (int(ai_run_id),)
    ).fetchone()[0])
    database.execute(
        """UPDATE ai_runs SET status=?,result_count=?,completed_at=?,error_message=? WHERE id=?""",
        (status, result_count, utc_now(), error_message.strip() or None, int(ai_run_id)),
    )


def save_ai_results(database: sqlite3.Connection, ai_run_id: int, results: list[dict]) -> None:
    now = utc_now()
    rows = []
    for item in results:
        try:
            match_id = int(item.get("match_id"))
        except (TypeError, ValueError):
            continue
        score = min(100, max(0, int(round(float(item.get("relevance_score", 0))))))
        confidence = min(1.0, max(0.0, float(item.get("confidence", 0.0))))
        rows.append((
            int(ai_run_id), match_id, score, confidence,
            str(item.get("category") or "")[:120],
            str(item.get("reason") or "")[:1200],
            str(item.get("evidence") or "")[:1800],
            now,
        ))
    if not rows:
        return
    database.executemany(
        """
        INSERT INTO ai_results(
            ai_run_id,match_id,relevance_score,confidence,category,reason,evidence,created_at
        ) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(ai_run_id,match_id) DO UPDATE SET
            relevance_score=excluded.relevance_score,
            confidence=excluded.confidence,
            category=excluded.category,
            reason=excluded.reason,
            evidence=excluded.evidence
        """,
        rows,
    )


def list_ai_runs(database: sqlite3.Connection, scan_run_id: int | None = None) -> list[sqlite3.Row]:
    if scan_run_id is None:
        return database.execute(
            """
            SELECT ar.*,sr.name AS scan_name,ks.name AS keyword_set_name
            FROM ai_runs ar
            JOIN scan_runs sr ON sr.id=ar.scan_run_id
            JOIN keyword_sets ks ON ks.id=sr.keyword_set_id
            ORDER BY ar.id DESC
            """
        ).fetchall()
    return database.execute(
        """
        SELECT ar.*,sr.name AS scan_name,ks.name AS keyword_set_name
        FROM ai_runs ar
        JOIN scan_runs sr ON sr.id=ar.scan_run_id
        JOIN keyword_sets ks ON ks.id=sr.keyword_set_id
        WHERE ar.scan_run_id=? ORDER BY ar.id DESC
        """,
        (int(scan_run_id),),
    ).fetchall()


def ai_result_rows(
    database: sqlite3.Connection,
    ai_run_id: int,
    minimum_relevance: int = 0,
    limit: int = 500,
    offset: int = 0,
) -> list[sqlite3.Row]:
    return database.execute(
        """
        SELECT ar.*,m.document_id,m.score AS archive_score,m.snippets_json,
               d.path,d.title,c.original_url,c.timestamp,
               COALESCE(r.status,'unreviewed') AS review_status
        FROM ai_results ar
        JOIN document_matches m ON m.id=ar.match_id
        JOIN documents d ON d.id=m.document_id
        JOIN captures c ON c.id=d.capture_id
        LEFT JOIN reviews r ON r.match_id=m.id
        WHERE ar.ai_run_id=? AND ar.relevance_score>=?
        ORDER BY ar.relevance_score DESC,ar.confidence DESC,m.score DESC,ar.id
        LIMIT ? OFFSET ?
        """,
        (int(ai_run_id), int(minimum_relevance), max(1, int(limit)), max(0, int(offset))),
    ).fetchall()


def ai_result_count(database: sqlite3.Connection, ai_run_id: int, minimum_relevance: int = 0) -> int:
    return int(database.execute(
        "SELECT COUNT(*) FROM ai_results WHERE ai_run_id=? AND relevance_score>=?",
        (int(ai_run_id), int(minimum_relevance)),
    ).fetchone()[0])


def record_site_issue(
    database: sqlite3.Connection,
    host: str,
    stage: str,
    category: str,
    message: str,
    *,
    target: str = "",
    http_status: int | None = None,
) -> int:
    host = host.strip().casefold() or "unknown"
    status_value = int(http_status or 0)
    now = utc_now()
    database.execute(
        """
        INSERT INTO site_issues(
            target,host,stage,category,http_status,message,occurrence_count,resolved,first_seen,last_seen
        ) VALUES(?,?,?,?,?,?,1,0,?,?)
        ON CONFLICT(host,stage,category,http_status) DO UPDATE SET
            target=COALESCE(NULLIF(excluded.target,''),site_issues.target),
            message=excluded.message,
            occurrence_count=site_issues.occurrence_count+1,
            resolved=0,
            last_seen=excluded.last_seen
        """,
        (target.strip() or None, host, stage, category, status_value, message[:2000], now, now),
    )
    row = database.execute(
        "SELECT id FROM site_issues WHERE host=? AND stage=? AND category=? AND http_status=?",
        (host, stage, category, status_value),
    ).fetchone()
    return int(row[0])


def list_site_issues(database: sqlite3.Connection, unresolved_only: bool = True, limit: int = 1000) -> list[sqlite3.Row]:
    where = "WHERE resolved=0" if unresolved_only else ""
    return database.execute(
        f"SELECT * FROM site_issues {where} ORDER BY last_seen DESC,id DESC LIMIT ?",
        (max(1, int(limit)),),
    ).fetchall()


def resolve_site_issue(database: sqlite3.Connection, issue_id: int, resolved: bool = True) -> None:
    database.execute("UPDATE site_issues SET resolved=?,last_seen=? WHERE id=?", (int(resolved), utc_now(), int(issue_id)))


def queue_media_discovery_candidates(
    database: sqlite3.Connection,
    query_signature: str,
    candidates: list[tuple[str, int | None, str, str]],
) -> int:
    """Persist discovered media URLs and return the number of newly queued URLs."""
    if not candidates:
        return 0
    now = utc_now()
    rows = [
        (query_signature, url, document_id, source_type, kind_hint, now, now)
        for url, document_id, source_type, kind_hint in candidates
    ]
    before = database.total_changes
    database.executemany(
        """
        INSERT OR IGNORE INTO media_discovery_queue(
            query_signature,original_url,source_document_id,source_type,kind_hint,state,
            lookup_attempts,result_count,last_error,created_at,updated_at
        ) VALUES(?,?,?,?,?,'pending',0,0,NULL,?,?)
        """,
        rows,
    )
    inserted = database.total_changes - before
    database.executemany(
        """
        UPDATE media_discovery_queue
        SET source_document_id=COALESCE(source_document_id,?),
            source_type=CASE WHEN source_type='' THEN ? ELSE source_type END,
            kind_hint=CASE WHEN COALESCE(kind_hint,'')='' THEN ? ELSE kind_hint END
        WHERE query_signature=? AND original_url=?
        """,
        (
            (document_id, source_type, kind_hint, query_signature, url)
            for url, document_id, source_type, kind_hint in candidates
        ),
    )
    return int(inserted)


def blocked_site_reasons(database: sqlite3.Connection) -> dict[str, str]:
    """Return hosts currently known to be unavailable due to archive policy."""
    result: dict[str, str] = {}
    for row in database.execute(
        """SELECT host,category FROM site_issues
           WHERE resolved=0 AND category IN ('wayback_excluded','robots_blocked')
           ORDER BY CASE category WHEN 'wayback_excluded' THEN 0 ELSE 1 END,last_seen DESC"""
    ):
        host = str(row[0] or '').strip().casefold()
        if host:
            result.setdefault(host, str(row[1]))
    return result


def blocked_site_hosts(database: sqlite3.Connection) -> set[str]:
    return set(blocked_site_reasons(database))


def media_discovery_needs_scan(
    database: sqlite3.Connection,
    query_signature: str,
    document_id: int,
    content_hash: str,
) -> bool:
    row = database.execute(
        "SELECT content_hash FROM media_discovery_documents WHERE query_signature=? AND document_id=?",
        (query_signature, int(document_id)),
    ).fetchone()
    return row is None or str(row[0] or "") != str(content_hash or "")


def mark_media_discovery_document(
    database: sqlite3.Connection,
    query_signature: str,
    document_id: int,
    content_hash: str,
    candidate_count: int,
) -> None:
    database.execute(
        """
        INSERT INTO media_discovery_documents(query_signature,document_id,content_hash,candidate_count,scanned_at)
        VALUES(?,?,?,?,?)
        ON CONFLICT(query_signature,document_id) DO UPDATE SET
            content_hash=excluded.content_hash,candidate_count=excluded.candidate_count,scanned_at=excluded.scanned_at
        """,
        (query_signature, int(document_id), content_hash or "", int(candidate_count), utc_now()),
    )


def media_discovery_counts(database: sqlite3.Connection, query_signature: str) -> dict[str, int]:
    result = {"pending": 0, "indexed": 0, "unavailable": 0, "error": 0}
    for row in database.execute(
        "SELECT state,COUNT(*) FROM media_discovery_queue WHERE query_signature=? GROUP BY state",
        (query_signature,),
    ):
        result[str(row[0])] = int(row[1])
    return result


def iter_media_discovery_rows(
    database: sqlite3.Connection,
    query_signature: str,
    max_attempts: int,
    batch_size: int = 500,
):
    last_id = 0
    while True:
        rows = database.execute(
            """
            SELECT * FROM media_discovery_queue
            WHERE query_signature=? AND state IN ('pending','error') AND lookup_attempts<? AND id>?
            ORDER BY id LIMIT ?
            """,
            (query_signature, int(max_attempts), last_id, max(1, int(batch_size))),
        ).fetchall()
        if not rows:
            return
        for row in rows:
            last_id = int(row["id"])
            yield row


def mark_media_discovery_lookup(
    database: sqlite3.Connection,
    queue_id: int,
    state: str,
    *,
    result_count: int = 0,
    error: str = "",
    increment_attempt: bool = True,
) -> None:
    database.execute(
        """
        UPDATE media_discovery_queue
        SET state=?,result_count=?,last_error=?,
            lookup_attempts=lookup_attempts+?,updated_at=?
        WHERE id=?
        """,
        (
            state,
            max(0, int(result_count)),
            error[:2000] or None,
            int(bool(increment_attempt)),
            utc_now(),
            int(queue_id),
        ),
    )
