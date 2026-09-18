from __future__ import annotations

import codecs
import json
import re
import tempfile
import threading
import urllib.parse
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import httpx

from archive_scout.cdx.client import HttpClient, MalformedCDXResponse, parse_cdx_rows_payload
from archive_scout.cdx.parallel import iter_cdx_pages
from archive_scout.config import ProjectConfig, ReportConfig, load_project_config, save_project_config
from archive_scout.content import decode_bytes, looks_textual_bytes
from archive_scout.database.connection import open_database
from archive_scout.database.repositories import (
    get_or_create_keyword_set, get_or_create_target, save_match, start_scan_run,
    upsert_capture, upsert_document,
)
from archive_scout.downloads.downloader import _allocate_capture_path, _download_capture
from archive_scout.downloads.rate_limit import FixedRateLimiter
from archive_scout.network.transports import (
    InvalidRangeResponse, ResilientTransport, TransportExhaustedError,
    Urllib3Backend, CurlBackend, _validate_range, _validate_range_size,
)
from archive_scout.reports.text import generate_reports
from archive_scout.scanning.automaton import LiteralAutomaton
from archive_scout.scanning.hitlist import search_with_hitlist
from archive_scout.storage import capture_path, media_path


class RangeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    payload = b"prefix remaining capture content"
    requests = []

    def do_GET(self):
        range_header = self.headers.get("Range")
        type(self).requests.append((self.path, range_header, self.headers.get("Accept-Encoding")))
        payload = self.payload
        status = 200
        content_range = None
        if range_header:
            offset = int(range_header[6:-1])
            if self.path == "/reject":
                status, payload = 416, b""
            elif self.path != "/ignore":
                status = 206
                start = 0 if self.path == "/bad" else offset
                content_range = f"bytes {start}-{len(payload)-1}/{len(payload)}"
                payload = payload[start:]
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        if content_range:
            self.send_header("Content-Range", content_range)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_):
        pass


class V1071Tests(unittest.TestCase):
    def test_cdx_request_encoding_is_readable_and_roundtrips_exact_values(self):
        from archive_scout.utils import cdx_request_url
        params = [("url", "https://example.com/a%2Fb?q=a+b&x=1#fragment*"),
                  ("filter", "statuscode:200"), ("filter", "mimetype:text/.*"),
                  ("fl", "timestamp,original"), ("resumeKey", "abc+def%20&x=/")]
        url = cdx_request_url("https://web.archive.org/cdx/search/cdx", params)
        self.assertTrue(url.startswith("https://web.archive.org/cdx/search/cdx?url=https://example.com/"))
        self.assertIn("fl=timestamp,original", url)
        self.assertIn("%252F", url)  # One quoting layer for an already-escaped nested path.
        self.assertEqual(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query), params)
        self.assertEqual(urllib.parse.urlsplit(url).fragment, "")

    def test_encoded_and_copied_cdx_inputs_resolve_original_target(self):
        from archive_scout.utils import normalize_target
        original = "https://example.com/a%2Fb?tag=a+b&x=1*"
        expected = "example.com/a%2Fb?tag=a+b&x=1*"
        self.assertEqual(normalize_target(original), expected)
        encoded = urllib.parse.quote(original, safe="")
        self.assertEqual(normalize_target(encoded), expected)
        self.assertEqual(normalize_target(urllib.parse.quote(encoded, safe="")), expected)
        self.assertEqual(normalize_target("example.com%2Fforum%2F%2A"), "example.com/forum/*")
        request = "https://web.archive.org/cdx/search/cdx?" + urllib.parse.urlencode({"url": original, "output": "json"})
        self.assertEqual(normalize_target(request), expected)
        self.assertEqual(normalize_target(request.replace("&", "&amp;")), expected)
        with self.assertRaises(ValueError):
            normalize_target("https://web.archive.org/cdx/search/cdx?output=json")

    def test_media_only_collapse_cannot_be_overridden_by_text_extra_parameters(self):
        from archive_scout.operations import _secondary_media_config
        from archive_scout.media.indexer import build_media_params
        config = ProjectConfig(output_dir=Path("."), targets=["example.com/*"], keywords=[],
                               cdx_extra_params=["collapse=timestamp:4", "filter=statuscode:200"]).normalized()
        media = _secondary_media_config(config)
        params = build_media_params(media, "example.com/*", "20000101000000", "20001231235959")
        self.assertEqual([value for key, value in params if key == "collapse"], ["urlkey"])
        self.assertEqual(config.cdx_extra_params[0], "collapse=timestamp:4")

    def test_binary_signatures_override_text_mime(self):
        for data in (b"\x89PNG\r\n\x1a\nrest", b"GIF89a" + b"A" * 32,
                     b"%PDF-1.7\nhello", b"PK\x03\x04" + b"a" * 30,
                     b"RIFFxxxxWEBPVP8 ", b"\x00\x00\x00\x20ftypmp42", b"\xff\xd8\xffrest"):
            for mime in ("text/plain", "text/html", "application/octet-stream", ""):
                with self.subTest(data=data, mime=mime):
                    self.assertFalse(looks_textual_bytes(data, mime))

    def test_text_recall_with_bad_mime_and_unicode(self):
        for encoding in ("utf-8-sig", "utf-16", "utf-32", "windows-1252"):
            data = "café evidence".encode(encoding)
            self.assertTrue(looks_textual_bytes(data, "application/octet-stream"))
            self.assertIn("café", decode_bytes(data))
        self.assertTrue(looks_textual_bytes(b"<html>real text</html>", "image/jpeg"))
        self.assertTrue(looks_textual_bytes(b"<svg><text>evidence</text></svg>", "image/svg+xml"))
        self.assertFalse(looks_textual_bytes(b"a\x00b\x01c\x00", "text/plain"))
        self.assertTrue(looks_textual_bytes(("a" * 8190 + "😀").encode("utf-16")[:16384]))

    def test_capture_suffix_and_filename_budget(self):
        for url in ("https://example.com/a.php", "https://example.com/readme.txt", "https://example.com/" + "é" * 400):
            path = capture_path(Path("project"), "20010101000000", url, disambiguate=True)
            self.assertEqual(path.suffix, ".txt")
            self.assertLessEqual(len((path.name + ".part").encode()), 255)
        self.assertEqual(media_path(Path("project"), "image", "https://e/x.png").suffix, ".png")

    def test_malformed_json_never_silently_drops_captures(self):
        header = ["timestamp", "original", "mimetype"]
        valid = ["20010101000000", "http://example.com", "text/html"]
        for payload in (None, "html", [header, valid, ["2001"]], [header, ["", "http://e", "text/plain"]]):
            with self.subTest(payload=payload), self.assertRaises((MalformedCDXResponse, RuntimeError)):
                parse_cdx_rows_payload(payload)
        parsed = parse_cdx_rows_payload([header, valid, [], ["resume-token"]])
        self.assertEqual(len(parsed.rows), 1)
        self.assertEqual(parsed.resume_key, "resume-token")

    def test_cdx_scheduler_does_not_submit_entire_rolling_window(self):
        from concurrent.futures import Future
        from archive_scout.cdx.client import CDXRows
        executor = mock.Mock()
        count = 0

        def submit(fn, page):
            nonlocal count
            count += 1
            future = Future()
            future.set_result(fn(page))
            return future

        executor.submit.side_effect = submit
        with mock.patch("archive_scout.cdx.parallel.ThreadPoolExecutor", return_value=executor), mock.patch(
            "archive_scout.cdx.parallel.request_cdx_rows", return_value=CDXRows([])
        ):
            pages = iter_cdx_pages(mock.Mock(), ["https://example.com"], list(range(1000)), lambda _: [], threading.Event(), 10)
            next(pages)
            self.assertEqual(count, 20)
            pages.close()

    def test_literal_counts_preserve_boundaries_unicode_and_overlaps(self):
        for native in (True, False):
            context = mock.patch("archive_scout.scanning.automaton.ahocorasick_rs", None) if not native else mock.patch.dict({}, {})
            with context:
                patterns = ["a", "aa", "aba", "needle", "café", "αβ", "a" * 70]
                text = ("ababa café needle αβ " + "a" * 90) * 90
                matcher = LiteralAutomaton(patterns)
                counts = matcher.count_non_overlapping(text, chunk_size=17)
                expected = {pattern: len(re.findall(re.escape(pattern), text)) for pattern in patterns}
                self.assertEqual(counts, expected)

    def test_range_validation(self):
        request = {"Range": "bytes=6-"}
        self.assertTrue(_validate_range(206, {"Content-Range": "bytes 6-9/10"}, request, 6))
        self.assertFalse(_validate_range(200, {}, request, 6))
        for headers in ({}, {"Content-Range": "bytes 0-9/10"}, {"Content-Range": "bytes 6-8/10"},
                        {"Content-Range": "bytes 6-9/10", "Content-Encoding": "gzip"}):
            with self.subTest(headers=headers), self.assertRaises(InvalidRangeResponse):
                _validate_range(206, headers, request, 6)
        with self.assertRaises(InvalidRangeResponse):
            _validate_range_size(206, {"Content-Range": "bytes 6-9/10"}, 9)

    def test_resume_validates_and_recovers_on_local_server(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for backend in ("httpx", "urllib3", "curl"):
                import shutil
                if backend == "curl" and not shutil.which("curl"):
                    continue
                client = HttpClient(FixedRateLimiter(0), 1, 2, "test", threading.Event(),
                                    network_backend=backend, trust_environment=False)
                try:
                    for endpoint in ("/good", "/ignore", "/bad", "/reject"):
                        with self.subTest(backend=backend, endpoint=endpoint), tempfile.TemporaryDirectory() as temp:
                            part = Path(temp) / "capture.part"
                            part.write_bytes(b"prefix")
                            RangeHandler.requests = []
                            result = client.download_to_path(f"http://127.0.0.1:{server.server_port}{endpoint}", part, 1024, compute_hash=False)
                            self.assertEqual(part.read_bytes(), RangeHandler.payload)
                            self.assertEqual(result["bytes"], len(RangeHandler.payload))
                            self.assertLessEqual(len(RangeHandler.requests), 2)
                            self.assertTrue(all(request[2] == "identity" for request in RangeHandler.requests))
                finally:
                    client.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_backend_selection_survives_unrelated_initialization_failure(self):
        with mock.patch("archive_scout.network.transports.HttpxBackend", side_effect=ImportError("no socks extra")), mock.patch(
            "archive_scout.network.transports.Urllib3Backend"
        ) as urllib, mock.patch("archive_scout.network.transports.CurlBackend"):
            transport = ResilientTransport(pool_size=10, connect_timeout=2, read_timeout=2, mode="urllib3")
            self.assertEqual(transport.backend_names, ("urllib3",))
            transport.close()
            transport = ResilientTransport(pool_size=10, connect_timeout=2, read_timeout=2)
            self.assertNotIn("httpx", transport.backend_names)
            self.assertIn("urllib3", transport.backend_names)
            transport.close()

    def test_proxy_policy_is_consistent(self):
        with mock.patch.dict("os.environ", {"HTTPS_PROXY": "http://proxy.invalid:8080", "NO_PROXY": "localhost"}, clear=True):
            backend = Urllib3Backend(2, 2, 2, trust_env=True)
            try:
                self.assertIsNot(backend._pool_for("https://web.archive.org"), backend.pool)
                self.assertIs(backend._pool_for("https://localhost"), backend.pool)
            finally:
                backend.close()
            backend = Urllib3Backend(2, 2, 2, trust_env=False)
            try:
                self.assertIs(backend._pool_for("https://web.archive.org"), backend.pool)
            finally:
                backend.close()
            curl = CurlBackend.__new__(CurlBackend)
            curl.trust_env = False
            self.assertNotIn("HTTPS_PROXY", curl._environment())

    def test_partial_download_is_preserved_after_read_timeout(self):
        transport = ResilientTransport.__new__(ResilientTransport)
        transport.lock = threading.Lock()
        transport.cooldown_until = {}
        transport.last_success = None
        transport.callback = None
        first, second = mock.Mock(), mock.Mock()
        transport.backends = {"httpx": first, "urllib3": second}
        transport.order = list(transport.backends)
        with tempfile.TemporaryDirectory() as temp:
            part = Path(temp) / "capture.part"

            def fail(*_args, **_kwargs):
                part.write_bytes(b"valid prefix")
                raise httpx.ReadTimeout("read stalled")

            first.download.side_effect = fail
            with self.assertRaises(TransportExhaustedError):
                transport.download("https://example.com", {}, part, 1024, threading.Event())
            self.assertEqual(part.read_bytes(), b"valid prefix")
            second.download.assert_not_called()

    def test_report_settings_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            config = ProjectConfig(output_dir=Path(temp), targets=[], keywords=[], report=ReportConfig(
                outputs=[], retain_scan_details=False, sort_order="newest", max_matches=7,
                snippet_limit=2, snippet_chars=80, link_limit=3,
            ))
            self.assertEqual(load_project_config(save_project_config(config)).report.to_payload(), config.report.to_payload())

    def test_reports_is_visible_between_media_and_analysis_in_simple_mode(self):
        from archive_scout.ui.main_window import ArchiveScoutApp
        app = object.__new__(ArchiveScoutApp)
        app.nav_buttons = {}
        app.interface_mode_var = mock.Mock(get=lambda: "Simple")
        app.sidebar = mock.Mock()
        app.notebook = mock.Mock()
        names = ["Dashboard", "Media", "Reports", "Archive analysis", "Settings"]
        app.notebook.tabs.return_value = names
        app.notebook.tab.side_effect = lambda tab, _option: tab
        app.notebook.select.return_value = "Reports"
        app.update_navigation_selection = mock.Mock()
        app.show_page = mock.Mock()
        with mock.patch("archive_scout.ui.main_window.ttk.Button"), mock.patch(
            "archive_scout.ui.main_window.ttk.Separator"
        ), mock.patch("archive_scout.ui.main_window.ttk.Label"):
            app.refresh_navigation()
        self.assertEqual(list(app.nav_buttons), names[:-1])
        app.show_page.assert_not_called()

    def test_failed_thread_start_restores_controls(self):
        from archive_scout.ui.main_window import ArchiveScoutApp
        app = object.__new__(ArchiveScoutApp)
        app.worker_thread = None
        app.stop_event = threading.Event()
        for name in ("progress_var", "status_var", "progress", "start_button", "stop_button", "log",
                     "ai_start_button", "research_search_button", "research_ai_button", "research_index_button"):
            setattr(app, name, mock.Mock())
        with mock.patch("archive_scout.ui.main_window.threading.Thread") as thread, mock.patch(
            "archive_scout.ui.main_window.messagebox.showerror"
        ) as showerror:
            thread.return_value.start.side_effect = RuntimeError("thread unavailable")
            app.start(ProjectConfig(output_dir=Path("."), targets=[], keywords=[]), "download_only")
        app.start_button.configure.assert_called_with(state="normal")
        app.stop_button.configure.assert_called_with(state="disabled")
        app.progress.stop.assert_called_once()
        self.assertIsNone(app.worker_thread)
        showerror.assert_called_once()


class ProjectRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = open_database(self.root)
        self.target = get_or_create_target(self.db, "example.com/*")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def capture(self, index, local=None):
        url = f"http://example.com/needle/{index}"
        upsert_capture(self.db, {"original": url, "timestamp": f"200{index}0101000000", "mimetype": "text/html",
                               "statuscode": "200", "digest": "", "length": "20"}, self.target, "sig")
        row = self.db.execute("SELECT * FROM captures WHERE original_url=?", (url,)).fetchone()
        if local:
            self.db.execute("UPDATE captures SET local_path=? WHERE id=?", (str(local), row["id"]))
        self.db.commit()
        return row

    def test_hitlist_pause_does_not_skip_uncommitted_batch(self):
        for index in range(1, 4):
            self.capture(index)
        stop = threading.Event()
        from archive_scout.scanning import hitlist
        from archive_scout.events import Stopped
        real_count = hitlist._count_matches

        def interrupt(*args):
            stop.set()
            return real_count(*args)

        with mock.patch.object(hitlist, "_count_matches", side_effect=interrupt), self.assertRaises(Stopped):
            search_with_hitlist(self.root, self.db, ["needle"], stop)
        self.assertEqual(self.db.execute("SELECT last_capture_id FROM quick_search_runs").fetchone()[0], 0)
        result = search_with_hitlist(self.root, self.db, ["needle"], threading.Event())
        self.assertEqual(result["indexed_checked"], 3)
        self.assertEqual(result["matches"], 3)

    def test_hitlist_excludes_binary_bytes_but_checks_url(self):
        path = self.root / "fake.txt"
        path.write_bytes(b"GIF89abodyneedle")
        self.capture(1, path)
        result = search_with_hitlist(self.root, self.db, ["needle", "bodyneedle"], threading.Event())
        self.assertEqual(result["local_checked"], 0)
        self.assertEqual(result["unavailable"], 1)
        self.assertEqual([row[0] for row in self.db.execute("SELECT fields FROM quick_search_hits")], ["url"])

    def test_existing_capture_paths_are_not_renamed(self):
        path = self.root / "old.html"
        path.write_text("old evidence")
        row = self.capture(1, path)
        row = self.db.execute("SELECT * FROM captures WHERE id=?", (row["id"],)).fetchone()
        self.assertEqual(_allocate_capture_path(self.db, self.root, row), path)
        client = mock.Mock()
        config = ProjectConfig(output_dir=self.root, targets=[], keywords=[])
        self.assertEqual(_download_capture(dict(row), path, config, client)["kind"], "downloaded")
        client.download_to_path.assert_not_called()

    def test_portable_filename_collisions_do_not_adopt_unrelated_files(self):
        row = self.capture(1)
        reserved = set()
        paths = []
        for _ in range(4):
            path = _allocate_capture_path(self.db, self.root, row, reserved)
            reserved.add(str(path))
            paths.append(path)
        self.assertEqual(len(set(paths)), 4)

    def test_download_only_preserves_header_charset_for_hitlist(self):
        from archive_scout.downloads.downloader import download_archive_only
        row = self.capture(1)
        text = "日本語の証拠"
        payload = text.encode("shift_jis")
        config = ProjectConfig(output_dir=self.root, targets=[], keywords=[], download_delay=0).normalized()
        client = mock.Mock()

        def download(url, path, _limit, **_kwargs):
            path.write_bytes(payload)
            return {"headers": {"content-type": "text/plain; charset=shift_jis"}, "preview": payload,
                    "bytes": len(payload), "content_hash": "", "status": 200, "final_url": url}

        client.download_to_path.side_effect = download
        with mock.patch("archive_scout.downloads.downloader.HttpClient", return_value=client):
            download_archive_only(config, self.db, threading.Event(), None, capture_ids=[row["id"]])
        saved = self.db.execute("SELECT local_path,detected_encoding FROM captures").fetchone()
        self.assertEqual(Path(saved["local_path"]).read_bytes(), payload)
        self.assertEqual(saved["detected_encoding"], "shift_jis")
        result = search_with_hitlist(self.root, self.db, ["証拠"], threading.Event())
        self.assertEqual(result["matches"], 1)

    def test_full_run_rate_limit_pause_is_not_a_name_error(self):
        from archive_scout.cdx.client import RateLimitDeferred
        from archive_scout.downloads.downloader import download_archive
        row = self.capture(1)
        config = ProjectConfig(output_dir=self.root, targets=[], keywords=["needle"], download_delay=0).normalized()
        with mock.patch("archive_scout.downloads.downloader.HttpClient"), mock.patch(
            "archive_scout.downloads.downloader._download_capture", side_effect=RateLimitDeferred("paused")
        ), self.assertRaises(RateLimitDeferred):
            download_archive(config, self.db, 1, threading.Event(), None, capture_ids=[row["id"]])
        self.assertEqual(self.db.execute("SELECT state FROM captures").fetchone()[0], "pending")

    def test_scanner_failure_is_not_retried_forever_in_same_run(self):
        from archive_scout.downloads import downloader
        row = self.capture(1)
        path = self.root / "page.txt"
        path.write_text("needle")
        self.db.execute("UPDATE captures SET state='downloaded_unscanned',local_path=?", (str(path),))
        self.db.commit()
        config = ProjectConfig(output_dir=self.root, targets=[], keywords=["needle"], download_delay=0).normalized()
        real_pending = downloader._pending_scan_rows
        calls = 0

        def guard(*args):
            nonlocal calls
            calls += 1
            if calls > 8:
                raise AssertionError("unbounded scan retry")
            return real_pending(*args)

        with mock.patch.object(downloader, "HttpClient"), mock.patch.object(
            downloader, "_pending_scan_rows", side_effect=guard
        ), mock.patch.object(downloader, "_scan_saved_capture", side_effect=OSError("unreadable")) as scan:
            downloader.download_archive(config, self.db, 1, threading.Event(), None, capture_ids=[row["id"]])
        scan.assert_called_once()
        self.assertEqual(self.db.execute("SELECT state FROM captures").fetchone()[0], "downloaded_unscanned")

    def test_future_schema_is_not_migrated_as_legacy(self):
        self.db.execute("UPDATE schema_info SET version=999")
        self.db.commit()
        with mock.patch("archive_scout.projects.migration.migrate_legacy_project") as migrate, self.assertRaisesRegex(RuntimeError, "newer"):
            open_database(self.root)
        migrate.assert_not_called()

    def test_report_toggles_are_reversible_and_limits_consistent(self):
        keyword = get_or_create_keyword_set(self.db, "test", ["needle"])
        scan = start_scan_run(self.db, keyword, "sig", 2, "rescan")
        for index in (1, 2):
            capture = self.capture(index)
            path = self.root / f"{index}.txt"
            path.write_text("needle")
            document = upsert_document(self.db, capture["id"], path, "Title", "needle", [], "hash", "normal", 6)
            save_match(self.db, scan, document, {"score": index, "hits": {"needle": index},
                       "snippets": ["evidence one", "evidence two"], "interesting_links": ["http://media/x.png"]})
        before = [tuple(row) for row in self.db.execute("SELECT * FROM document_matches")]
        config = ProjectConfig(output_dir=self.root, targets=[], keywords=[], report=ReportConfig(outputs=[]))
        generate_reports(config, self.db, scan)
        self.assertEqual(before, [tuple(row) for row in self.db.execute("SELECT * FROM document_matches")])
        config.report = ReportConfig(sort_order="oldest", max_matches=1, snippet_limit=1, snippet_chars=8, link_limit=1)
        paths = generate_reports(config, self.db, scan)
        self.assertEqual(paths["matched_urls"].read_text().strip(), "http://example.com/needle/1")
        ranked = paths["matches_ranked"].read_text()
        self.assertIn("evidence", ranked)
        self.assertNotIn("evidence two", ranked)
        self.assertNotIn("needle/2", ranked)
        self.assertEqual(before, [tuple(row) for row in self.db.execute("SELECT * FROM document_matches")])


if __name__ == "__main__":
    unittest.main()
