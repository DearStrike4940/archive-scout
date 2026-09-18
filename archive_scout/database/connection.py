from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from ..constants import SCHEMA_VERSION
from .schema import initialize_schema

DATABASE_NAME = "archive_scout.sqlite3"


def database_version(path: Path) -> int | None:
    if not path.exists():
        return None
    database: sqlite3.Connection | None = None
    try:
        database = sqlite3.connect(path)
        row = database.execute("SELECT version FROM schema_info LIMIT 1").fetchone()
        return int(row[0]) if row else None
    except Exception:
        return None
    finally:
        if database is not None:
            database.close()


def is_modern_database(path: Path) -> bool:
    return database_version(path) in {2, 3, 4, 5, 6, 7, SCHEMA_VERSION}


def open_database(root: Path, migrate: bool = True) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    path = root / DATABASE_NAME
    version = database_version(path) if path.exists() else None
    if version is not None and version > SCHEMA_VERSION:
        raise RuntimeError(f"Project schema {version} is newer than supported schema {SCHEMA_VERSION}; update Archive Scout before opening it")
    if migrate and path.exists() and version not in {2, 3, 4, 5, 6, 7, SCHEMA_VERSION}:
        from ..projects.migration import migrate_legacy_project
        migrate_legacy_project(root)
        version = database_version(path)
    if migrate and version in {2, 3, 4, 5, 6, 7}:
        try:
            from ..projects.backups import create_project_backup
            create_project_backup(root, reason=f"before_schema_{SCHEMA_VERSION}", keep=5)
        except Exception as exc:
            raise RuntimeError("Could not back up the project before migration; no schema changes were made") from exc
    database = sqlite3.connect(path, timeout=60)
    try:
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("PRAGMA synchronous=NORMAL")
        database.execute("PRAGMA foreign_keys=ON")
        database.execute("PRAGMA temp_store=MEMORY")
        database.execute("PRAGMA cache_size=-65536")
        database.execute("PRAGMA mmap_size=268435456")
        database.execute("PRAGMA wal_autocheckpoint=10000")
        database.execute("PRAGMA journal_size_limit=67108864")
        database.execute("PRAGMA busy_timeout=60000")
        initialize_schema(database)
        # Recover states left behind by a terminated process. A second connection
        # opened by the active UI must not mark the current worker as crashed.
        current_pid = os.getpid()
        active_current_process = database.execute(
            "SELECT 1 FROM operation_runs WHERE status='running' AND process_id=? LIMIT 1",
            (current_pid,),
        ).fetchone()
        if not active_current_process:
            database.execute("UPDATE captures SET state='pending' WHERE state='downloading'")
            database.execute("UPDATE captures SET state='downloaded_unscanned' WHERE state='scanning'")
            database.execute("UPDATE media_captures SET state='pending' WHERE state='downloading'")
            database.execute("UPDATE scan_runs SET status='interrupted' WHERE status='running'")
            database.execute("UPDATE quick_search_runs SET status='interrupted',updated_at=datetime('now') WHERE status='running'")
        database.execute(
            """
            UPDATE operation_runs
            SET status='interrupted',completed_at=datetime('now'),updated_at=datetime('now'),
                message=COALESCE(message,'Recovered after an unclean shutdown')
            WHERE status='running' AND (process_id IS NULL OR process_id<>?)
            """,
            (current_pid,),
        )
        database.commit()
        return database
    except Exception:
        database.close()
        raise
