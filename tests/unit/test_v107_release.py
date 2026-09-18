from __future__ import annotations

import csv
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from archive_scout.config import (
    MediaConfig,
    ProjectConfig,
    ReportConfig,
    REPORT_FIELD_NAMES,
    load_project_config,
    save_project_config,
)
from archive_scout.database.connection import open_database
from archive_scout.database.repositories import (
    apply_report_storage_policy,
    get_or_create_keyword_set,
    get_or_create_target,
    save_match,
    start_scan_run,
    upsert_capture,
    upsert_document,
)
from archive_scout.media.indexer import _discover_embedded_queue
from archive_scout.operations import _secondary_media_config, run_project
from archive_scout.reports.text import generate_reports
from archive_scout.scanning.jobs import ScanJob
from archive_scout.scanning.scoring import analyze_content
from archive_scout.ui.main_window import ArchiveScoutApp
from archive_scout.utils import hash_text, normalize_search, utc_now


class _StartThread:
    last = None

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.started = False
        type(self).last = self

    def start(self):
        self.started = True

    def is_alive(self):
        return False


class V107ReleaseTests(unittest.TestCase):
    def test_secondary_media_collapse_does_not_mutate_text_query(self):
        config = ProjectConfig(
            output_dir=Path("."),
            targets=["example.com/*"],
            keywords=[],
            cdx_collapses=["digest"],
            media=MediaConfig(enabled=True, snapshot_strategy="earliest"),
        ).normalized()
        media_config = _secondary_media_config(config)
        self.assertEqual(config.cdx_collapses, ["digest"])
        self.assertEqual(media_config.cdx_collapses, ["urlkey"])

        explicit_all = ProjectConfig(
            output_dir=Path("."),
            targets=["example.com/*"],
            keywords=[],
            cdx_collapses=["urlkey", "digest"],
            media=MediaConfig(enabled=True, snapshot_strategy="all"),
        ).normalized()
        self.assertEqual(_secondary_media_config(explicit_all).cdx_collapses, ["digest"])

        explicit_latest = ProjectConfig(
            output_dir=Path("."),
            targets=["example.com/*"],
            keywords=[],
            cdx_collapses=["urlkey"],
            media=MediaConfig(enabled=True, snapshot_strategy="latest"),
        ).normalized()
        self.assertEqual(_secondary_media_config(explicit_latest).cdx_collapses, [])

    def test_report_matrix_round_trips_empty_and_partial_selections(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report = ReportConfig(
                outputs=["matches_ranked", "media_summary"],
                fields={
                    name: (
                        ["score", "original_url"] if name == "matches_ranked"
                        else ["downloaded"] if name == "media_summary"
                        else []
                    )
                    for name in REPORT_FIELD_NAMES
                },
            )
            path = save_project_config(ProjectConfig(
                output_dir=root,
                targets=["example.com/*"],
                keywords=[],
                report=report,
            ))
            loaded = load_project_config(path).report.normalized()
            self.assertEqual(loaded.outputs, ["matches_ranked", "media_summary"])
            self.assertEqual(loaded.fields_for("matches_ranked"), ["score", "original_url"])
            self.assertEqual(loaded.fields_for("media_summary"), ["downloaded"])
            self.assertEqual(loaded.fields_for("interesting_links"), [])

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["report"]["fields"]["interesting_links"], [])

    def test_disabled_report_details_are_not_computed_or_stored(self):
        report = ReportConfig(
            outputs=["matches_ranked"],
            fields={
                name: (["score", "original_url"] if name == "matches_ranked" else [])
                for name in REPORT_FIELD_NAMES
            },
        ).normalized()
        job = ScanJob.create(1, "keywords", ["alpha"])
        with mock.patch("archive_scout.scanning.scoring.make_snippets", side_effect=AssertionError("snippets computed")), mock.patch(
            "archive_scout.scanning.scoring.link_is_interesting", side_effect=AssertionError("interesting links computed")
        ):
            analysis = analyze_content(
                "http://example.com/", "Alpha", "alpha body", "<p>alpha body</p>",
                ["http://example.com/video.mp4"], job.patterns, job.prefilter,
                include_hit_fields=report.store_keyword_fields,
                include_snippets=report.store_snippets,
                include_interesting_links=report.store_interesting_links,
            )
        self.assertEqual(analysis["snippets"], [])
        self.assertEqual(analysis["interesting_links"], [])

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = open_database(root)
            try:
                target_id = get_or_create_target(database, "example.com/*")
                upsert_capture(database, {
                    "original": "http://example.com/page",
                    "timestamp": "20010101000000",
                    "mimetype": "text/html",
                    "statuscode": "200",
                    "digest": "",
                    "length": "10",
                }, target_id, "sig")
                capture_id = int(database.execute("SELECT id FROM captures").fetchone()[0])
                page = root / "captures" / "page.html"
                page.parent.mkdir(parents=True, exist_ok=True)
                page.write_text("alpha body", encoding="utf-8")
                document_id = upsert_document(
                    database, capture_id, page, "Alpha", "alpha body", [], hash_text("alpha body"),
                    hash_text(normalize_search("alpha body")), page.stat().st_size,
                )
                keyword_id = get_or_create_keyword_set(database, "keywords", ["alpha"])
                scan_run = start_scan_run(database, keyword_id, "test", 1, "rescan")
                save_match(database, scan_run, document_id, {
                    **analysis,
                    "hits": {"alpha": 2},
                    "hit_fields": {"alpha": ["title", "body"]},
                    "snippets": ["should not be stored"],
                    "interesting_links": ["http://example.com/video.mp4"],
                }, report)
                row = database.execute(
                    "SELECT hits_json,fields_json,snippets_json,interesting_links_json FROM document_matches"
                ).fetchone()
                self.assertIsNone(row["hits_json"])
                self.assertIsNone(row["fields_json"])
                self.assertIsNone(row["snippets_json"])
                self.assertIsNone(row["interesting_links_json"])
                self.assertEqual(database.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 0)

                config = ProjectConfig(
                    output_dir=root,
                    targets=["example.com/*"],
                    keywords=["alpha"],
                    report=report,
                ).normalized()
                paths = generate_reports(config, database, scan_run)
                ranked = paths["matches_ranked"].read_text(encoding="utf-8")
                self.assertIn("SCORE:", ranked)
                self.assertIn("ORIGINAL URL:", ranked)
                self.assertNotIn("SNIPPETS:", ranked)
                self.assertNotIn("INTERESTING LINKS:", ranked)
                self.assertFalse((root / "reports" / "interesting_links.txt").exists())
                self.assertFalse((root / "reports" / "keyword_counts.txt").exists())
                with paths["all_matches_csv"].open(encoding="utf-8", newline="") as handle:
                    csv_report = csv.DictReader(handle)
                    self.assertEqual(csv_report.fieldnames, ["score", "original_url"])
                    self.assertEqual(len(list(csv_report)), 1)
                markdown = paths["all_matches_markdown"].read_text(encoding="utf-8")
                self.assertIn("**Score:**", markdown)
                self.assertNotIn("### Snippets", markdown)
                self.assertNotIn("**Keyword hits:**", markdown)
                config.report.outputs = []
                disabled = generate_reports(config, database, scan_run)
                self.assertNotIn("all_matches_markdown", disabled)
                for suffix in ("md", "csv", "txt"):
                    self.assertFalse((root / "reports" / f"all_matches_ranked.{suffix}").exists())
            finally:
                database.close()

    def test_report_storage_policy_clears_obsolete_payloads_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = open_database(root)
            try:
                target_id = get_or_create_target(database, "example.com/*")
                upsert_capture(database, {
                    "original": "http://example.com/page",
                    "timestamp": "20010101000000",
                    "mimetype": "text/html",
                    "statuscode": "200",
                    "digest": "",
                    "length": "10",
                }, target_id, "sig")
                capture_id = int(database.execute("SELECT id FROM captures").fetchone()[0])
                page = root / "captures" / "page.html"
                page.parent.mkdir(parents=True, exist_ok=True)
                page.write_text("alpha body", encoding="utf-8")
                document_id = upsert_document(
                    database, capture_id, page, "Alpha", "alpha body", [], hash_text("alpha body"),
                    hash_text(normalize_search("alpha body")), page.stat().st_size,
                )
                keyword_id = get_or_create_keyword_set(database, "keywords", ["alpha"])
                scan_run = start_scan_run(database, keyword_id, "test", 1, "rescan")
                full_report = ReportConfig().normalized()
                save_match(database, scan_run, document_id, {
                    "score": 3,
                    "hits": {"alpha": 2},
                    "hit_fields": {"alpha": ["title", "body"]},
                    "snippets": ["alpha body"],
                    "interesting_links": ["http://example.com/video.mp4"],
                    "excluded": False,
                    "required_missing": False,
                    "proximity": {},
                }, full_report)
                before = database.execute(
                    "SELECT hits_json,fields_json,snippets_json,interesting_links_json FROM document_matches"
                ).fetchone()
                self.assertTrue(all(before[name] is not None for name in before.keys()))
                self.assertEqual(database.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 1)

                lean_report = ReportConfig(
                    outputs=["matches_ranked"],
                    fields={
                        name: (["score", "original_url"] if name == "matches_ranked" else [])
                        for name in REPORT_FIELD_NAMES
                    },
                ).normalized()
                changed = apply_report_storage_policy(database, lean_report)
                self.assertGreater(changed, 0)
                after = database.execute(
                    "SELECT hits_json,fields_json,snippets_json,interesting_links_json FROM document_matches"
                ).fetchone()
                self.assertTrue(all(after[name] is None for name in after.keys()))
                self.assertEqual(database.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 0)

                # The stored policy fingerprint makes subsequent report generation
                # O(1) instead of rescanning document_matches every time.
                self.assertEqual(apply_report_storage_policy(database, lean_report), 0)
                fingerprint = database.execute(
                    "SELECT value FROM project_meta WHERE key='report_storage_policy_v1'"
                ).fetchone()
                self.assertIsNotNone(fingerprint)
            finally:
                database.close()

    def test_download_only_runs_optional_media_pipeline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = ProjectConfig(
                output_dir=root,
                targets=["example.com/*"],
                keywords=[],
                keyword_sets=[],
                cdx_collapses=["digest"],
                media=MediaConfig(
                    enabled=True,
                    discover_embedded=True,
                    allow_external_embeds=True,
                ),
            ).normalized()
            media_report = root / "reports" / "media_summary.txt"

            with mock.patch("archive_scout.operations.index_archive"), mock.patch(
                "archive_scout.operations.download_archive_only",
                return_value={"downloaded": 0, "skipped": 0, "errors": 0, "queued": 0},
            ), mock.patch("archive_scout.operations.index_media") as index_media, mock.patch(
                "archive_scout.operations.download_media"
            ) as download_media, mock.patch(
                "archive_scout.operations.generate_media_reports",
                return_value={"media_summary": media_report},
            ), mock.patch(
                "archive_scout.operations.prepare_scan_jobs",
                side_effect=AssertionError("download-only must not create scan jobs"),
            ):
                paths = run_project(config, "download_only", threading.Event(), None)

            index_media.assert_called_once()
            download_media.assert_called_once()
            secondary = index_media.call_args.args[0]
            self.assertEqual(secondary.cdx_collapses, ["urlkey"])
            self.assertEqual(config.cdx_collapses, ["digest"])
            self.assertTrue(secondary.media.allow_external_embeds)
            self.assertIn("project", paths)
            self.assertIn("media_summary", paths)

    def test_embedded_discovery_uses_download_only_capture_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            capture = root / "captures" / "page.html"
            capture.parent.mkdir(parents=True, exist_ok=True)
            capture.write_text(
                '<html><body><img src="https://cdn.example.net/media/test.jpg"></body></html>',
                encoding="utf-8",
            )
            database = open_database(root)
            try:
                now = utc_now()
                database.execute(
                    """INSERT INTO captures(
                           original_url,timestamp,query_signature,mimetype,statuscode,length,state,
                           local_path,content_hash,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "http://example.com/page.html", "20010101000000", "text-signature", "text/html",
                        "200", capture.stat().st_size, "downloaded_unscanned", str(capture), "", now, now,
                    ),
                )
                database.commit()
                config = ProjectConfig(
                    output_dir=root,
                    targets=["example.com/*"],
                    keywords=[],
                    media=MediaConfig(enabled=True, discover_embedded=True, allow_external_embeds=True),
                ).normalized()
                queued = _discover_embedded_queue(
                    config, database, threading.Event(), None, "media-signature", external_only=False,
                )
                row = database.execute(
                    "SELECT original_url,source_document_id,source_type FROM media_discovery_queue"
                ).fetchone()
                self.assertEqual(queued, 1)
                self.assertEqual(row["original_url"], "https://cdn.example.net/media/test.jpg")
                self.assertIsNone(row["source_document_id"])
                self.assertEqual(row["source_type"], "external_embedded")
                self.assertEqual(database.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)
            finally:
                database.close()

    def test_start_button_creates_worker_before_slow_initialization(self):
        fake = object.__new__(ArchiveScoutApp)
        fake.worker_thread = None
        fake.stop_event = threading.Event()
        fake.progress_var = mock.Mock()
        fake.status_var = mock.Mock()
        fake.progress = mock.Mock()
        fake.start_button = mock.Mock()
        fake.stop_button = mock.Mock()
        fake.ai_start_button = mock.Mock()
        fake.research_search_button = mock.Mock()
        fake.research_ai_button = mock.Mock()
        fake.research_index_button = mock.Mock()
        fake.log = mock.Mock()
        fake.refresh_dashboard = mock.Mock(side_effect=AssertionError("dashboard recount blocked startup"))
        config = ProjectConfig(output_dir=Path("."), targets=["example.com/*"], keywords=[]).normalized()

        _StartThread.last = None
        with mock.patch(
            "archive_scout.ui.main_window.ensure_frozen_bundle_available",
            side_effect=AssertionError("bundle validation ran on Tk thread"),
        ), mock.patch("archive_scout.ui.main_window.threading.Thread", _StartThread):
            ArchiveScoutApp.start(fake, config, "download_only")

        fake.refresh_dashboard.assert_not_called()
        self.assertIsNotNone(_StartThread.last)
        self.assertTrue(_StartThread.last.started)
        fake.progress.configure.assert_called_with(mode="indeterminate")

    def test_fast_v105_network_profile_remains_intact(self):
        config = ProjectConfig(output_dir=Path("."), targets=["example.com/*"], keywords=[]).normalized()
        self.assertEqual(config.workers, 10)
        self.assertEqual(config.network.cdx_workers, 10)
        self.assertEqual(config.cdx_delay, 0.75)
        self.assertEqual(config.download_delay, 0.125)


if __name__ == "__main__":
    unittest.main()
