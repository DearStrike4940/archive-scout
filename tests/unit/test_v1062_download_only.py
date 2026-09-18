from __future__ import annotations

import hashlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from archive_scout.cdx.parameters import cdx_query_signature
from archive_scout.config import ProjectConfig
from archive_scout.constants import OPERATION_MODES, VERSION
from archive_scout.database.connection import open_database
from archive_scout.defaults import PRESETS
from archive_scout.downloads import downloader as download_mod
from archive_scout.downloads.downloader import download_archive_only
from archive_scout.operations import run_project
from archive_scout.scanning.hitlist import search_with_hitlist
from archive_scout.utils import utc_now


class _ImmediateDownloadClient:
    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    def close(self):
        return None

    def download_to_path(self, url, destination, max_bytes, *, compute_hash=True):
        del max_bytes
        type(self).calls += 1
        payload = b"<html><body>download-only payload</body></html>"
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        return {
            "headers": {"content-type": "text/html; charset=utf-8"},
            "preview": payload,
            "bytes": len(payload),
            "content_hash": hashlib.sha256(payload).hexdigest() if compute_hash else "",
            "status": 200,
            "final_url": url,
        }


class V1062DownloadOnlyTests(unittest.TestCase):
    def test_release_defaults_and_presets(self):
        self.assertEqual(VERSION, "1.0.7.1")
        config = ProjectConfig(output_dir=Path("."), targets=[], keywords=[]).normalized()
        self.assertEqual(config.workers, 10)
        self.assertEqual(config.scan_workers, 0)
        self.assertEqual(config.max_file_mb, 25.0)
        self.assertEqual(config.cdx_delay, 0.75)
        self.assertEqual(config.download_delay, 0.125)
        self.assertEqual(config.network.cdx_workers, 10)
        self.assertEqual(config.network.page_blocks, 0)
        self.assertEqual(config.network.retry_base_seconds, 5.0)
        self.assertEqual(config.network.retry_max_seconds, 300.0)
        self.assertEqual(config.network.failure_pause_threshold, 8)
        self.assertEqual(config.backup_keep, 5)
        self.assertEqual(config.backup_max_mb, 1024.0)
        self.assertFalse(config.auto_backup)
        self.assertIn("Index and download only (no scanning)", OPERATION_MODES)
        self.assertEqual(OPERATION_MODES["Index and download only (no scanning)"], "download_only")
        self.assertNotIn("Ogrish 9/11 research", PRESETS)
        self.assertIn("General web archive research", PRESETS)
        self.assertIn("Legacy forum research", PRESETS)
        self.assertIn("Lost media discovery", PRESETS)

    def test_download_only_uses_capture_manifest_without_scanning(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = ProjectConfig(
                output_dir=root,
                targets=["example.com/*"],
                keywords=[],
                workers=4,
                download_delay=0.0,
                download_scope="all_text",
            ).normalized()
            db = open_database(root)
            signature = cdx_query_signature(config)
            now = utc_now()
            db.executemany(
                """INSERT INTO captures(
                       original_url,timestamp,query_signature,mimetype,statuscode,length,state,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        f"http://example.com/page-{index}.html",
                        f"20010101{index:06d}"[:14],
                        signature,
                        "text/html",
                        "200",
                        48,
                        "pending",
                        now,
                        now,
                    )
                    for index in range(12)
                ],
            )
            db.commit()
            _ImmediateDownloadClient.calls = 0
            with mock.patch.object(download_mod, "HttpClient", _ImmediateDownloadClient), mock.patch.object(
                download_mod, "_scan_saved_capture", side_effect=AssertionError("scanner must never run")
            ):
                stats = download_archive_only(config, db, threading.Event(), None)

            self.assertEqual(stats["downloaded"], 12)
            self.assertEqual(_ImmediateDownloadClient.calls, 12)
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM captures WHERE state='downloaded_unscanned'").fetchone()[0],
                12,
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM document_matches").fetchone()[0], 0)
            for row in db.execute("SELECT local_path FROM captures"):
                self.assertTrue(Path(row["local_path"]).is_file())
            db.close()

    def test_download_only_capture_is_immediately_searchable_by_hitlist(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = ProjectConfig(
                output_dir=root,
                targets=["example.com/*"],
                keywords=[],
                workers=2,
                download_delay=0.0,
                download_scope="all_text",
            ).normalized()
            db = open_database(root)
            now = utc_now()
            db.execute(
                """INSERT INTO captures(
                       original_url,timestamp,query_signature,mimetype,statuscode,length,state,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    "http://example.com/a.html", "20010101000000", cdx_query_signature(config),
                    "text/html", "200", 48, "pending", now, now,
                ),
            )
            db.commit()
            _ImmediateDownloadClient.calls = 0
            with mock.patch.object(download_mod, "HttpClient", _ImmediateDownloadClient), mock.patch.object(
                download_mod, "_scan_saved_capture", side_effect=AssertionError("scanner must never run")
            ):
                stats = download_archive_only(config, db, threading.Event(), None)
            self.assertEqual(stats["downloaded"], 1)
            result = search_with_hitlist(root, db, ["download-only payload"], threading.Event())
            self.assertEqual(result["matches"], 1)
            self.assertEqual(result["local_checked"], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)
            db.close()

    def test_download_only_operation_needs_no_keyword_set_and_skips_all_analysis(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = ProjectConfig(
                output_dir=root,
                targets=["example.com/*"],
                keywords=[],
                keyword_sets=[],
                workers=2,
                download_delay=0.0,
            ).normalized()

            def fake_index(cfg, database, stop_event, callback):
                del stop_event, callback
                now = utc_now()
                database.execute(
                    """INSERT OR IGNORE INTO captures(
                           original_url,timestamp,query_signature,mimetype,statuscode,length,state,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        "http://example.com/a.html",
                        "20010101000000",
                        cdx_query_signature(cfg),
                        "text/html",
                        "200",
                        48,
                        "pending",
                        now,
                        now,
                    ),
                )
                database.commit()

            _ImmediateDownloadClient.calls = 0
            with mock.patch("archive_scout.operations.index_archive", side_effect=fake_index), mock.patch.object(
                download_mod, "HttpClient", _ImmediateDownloadClient
            ), mock.patch("archive_scout.operations.prepare_scan_jobs", side_effect=AssertionError("no scan jobs")), mock.patch(
                "archive_scout.operations.build_research_index", side_effect=AssertionError("no research index")
            ), mock.patch("archive_scout.operations.index_media", side_effect=AssertionError("no media index")):
                paths = run_project(config, "download_only", threading.Event(), None)

            self.assertEqual(_ImmediateDownloadClient.calls, 1)
            self.assertEqual(set(paths), {"project"})
            self.assertTrue(Path(paths["project"]).samefile(root / "project.json"))
            db = open_database(root)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM document_matches").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM media_captures").fetchone()[0], 0)
            state = db.execute("SELECT state FROM captures").fetchone()[0]
            self.assertEqual(state, "downloaded_unscanned")
            db.close()


if __name__ == "__main__":
    unittest.main()
