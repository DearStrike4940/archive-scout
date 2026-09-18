from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from archive_scout.cdx.client import HttpClient, TransientRequestError
from archive_scout.cdx.indexer import _request_paged_count, decode_plan, index_archive, index_windows
from archive_scout.cdx.parallel import fetch_cdx_pages
from archive_scout.cdx.parameters import cdx_query_signature
from archive_scout.config import MediaConfig, NetworkConfig, ProjectConfig, load_project_config
from archive_scout.database.connection import open_database
from archive_scout.database.repositories import get_or_create_target, upsert_captures
from archive_scout.downloads.rate_limit import FixedRateLimiter
from archive_scout.events import ConnectivityPaused
from archive_scout.media.indexer import index_media
from archive_scout.network.transports import TransportResponse
from archive_scout.utils import utc_now


HEADER = ["timestamp", "original", "mimetype", "statuscode", "digest", "length"]


class _TextTransport:
    def __init__(self, body: bytes):
        self.body = body
        self.urls: list[str] = []

    def request(self, url, _headers, _max_bytes, _stop_event):
        self.urls.append(url)
        return TransportResponse(200, {"Content-Type": "text/plain"}, url, self.body, "fake", 0.001)

    def close(self):
        return None


class _ParallelClient:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def get_cdx_any(self, _urls, params, max_bytes=64 * 1024 * 1024, prefer_text=False):
        del max_bytes, prefer_text
        page = int(dict(params)["page"])
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.04)
            return [HEADER, [f"2001010100000{page}", f"http://example.com/{page}", "text/html", "200", str(page), "1"]]
        finally:
            with self.lock:
                self.active -= 1


class FastIndexingTests(unittest.TestCase):
    def test_broad_paged_query_uses_one_year_window(self):
        config = ProjectConfig(
            output_dir=Path("."),
            targets=["example.com/*"],
            keywords=[],
            from_date="2001",
            to_date="2001",
            network=NetworkConfig(index_strategy="paged"),
        ).normalized()
        self.assertEqual(index_windows(config, "example.com/*", 2001), [("20010101000000", "20011231235959")])

    def test_page_fetches_overlap_but_remain_bounded(self):
        client = _ParallelClient()
        results = fetch_cdx_pages(
            client,
            ("https://example.invalid/cdx",),
            list(range(6)),
            lambda page: [("page", str(page))],
            threading.Event(),
            workers=3,
        )
        self.assertEqual([item.page for item in results], list(range(6)))
        self.assertGreaterEqual(client.max_active, 2)
        self.assertLessEqual(client.max_active, 3)

    def test_failed_page_is_requeued_without_repeating_successful_pages(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = ProjectConfig(
                output_dir=root,
                targets=["example.com/*"],
                keywords=[],
                from_date="20010101",
                to_date="20010101",
                cdx_delay=0,
                network=NetworkConfig(index_strategy="paged", page_blocks=2, cdx_workers=3),
            ).normalized()
            database = open_database(root)
            page_calls: dict[int, int] = {}

            def fake_get(_self, _urls, params, max_bytes=64 * 1024 * 1024, prefer_text=False):
                del max_bytes, prefer_text
                values = dict(params)
                if values.get("showNumPages") == "true":
                    return 4
                page = int(values["page"])
                page_calls[page] = page_calls.get(page, 0) + 1
                if page == 1 and page_calls[page] == 1:
                    raise TransientRequestError("one slow page", timed_out=True, splittable=True)
                return [HEADER, [f"2001010100000{page}", f"http://example.com/{page}", "text/html", "200", str(page), "1"]]

            with patch("archive_scout.cdx.client.HttpClient.get_cdx_any", new=fake_get):
                index_archive(config, database, threading.Event())

            self.assertEqual(database.execute("SELECT COUNT(*) FROM captures").fetchone()[0], 4)
            self.assertEqual(page_calls, {0: 1, 1: 2, 2: 1, 3: 1})
            self.assertEqual(database.execute("SELECT complete FROM index_state").fetchone()[0], 1)
            database.close()

    def test_repeated_slow_page_saves_only_exact_failed_page(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = ProjectConfig(
                output_dir=root,
                targets=["example.com/*"],
                keywords=[],
                from_date="20010101",
                to_date="20010101",
                cdx_delay=0,
                network=NetworkConfig(index_strategy="paged", page_blocks=2, cdx_workers=3),
            ).normalized()
            database = open_database(root)
            page_calls: dict[int, int] = {}
            resume_calls = 0

            def fake_get(_self, _urls, params, max_bytes=64 * 1024 * 1024, prefer_text=False):
                nonlocal resume_calls
                del max_bytes, prefer_text
                values = dict(params)
                if values.get("showNumPages") == "true":
                    return 3
                if "page" in values:
                    page = int(values["page"])
                    page_calls[page] = page_calls.get(page, 0) + 1
                    if page == 1:
                        raise TransientRequestError("persistent slow page", timed_out=True, splittable=True)
                    return [HEADER, [f"2001010100000{page}", f"http://example.com/{page}", "text/html", "200", str(page), "1"]]
                resume_calls += 1
                return []

            with patch("archive_scout.cdx.client.HttpClient.get_cdx_any", new=fake_get):
                with self.assertRaises(ConnectivityPaused):
                    index_archive(config, database, threading.Event())

            self.assertEqual(page_calls.get(1), 5)
            self.assertEqual(resume_calls, 0)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM captures").fetchone()[0], 2)
            state = database.execute("SELECT complete,resume_key FROM index_state").fetchone()
            self.assertEqual(state["complete"], 0)
            plan = decode_plan(state["resume_key"], [])
            self.assertEqual(plan.pending[0].strategy, "paged")
            self.assertEqual(plan.pending[0].retry_pages, [1])
            self.assertEqual(plan.pending[0].page_failures, {1: 5})
            database.close()

    def test_page_count_timeout_switches_to_resumable_windows(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = ProjectConfig(
                output_dir=root,
                targets=["example.com/*"],
                keywords=[],
                from_date="2001",
                to_date="2001",
                cdx_delay=0,
                retries=1,
                network=NetworkConfig(index_strategy="paged", cdx_workers=4),
            ).normalized()
            database = open_database(root)
            calls = 0

            def fake_get(_self, _urls, _params, max_bytes=64 * 1024 * 1024, prefer_text=False):
                nonlocal calls
                del max_bytes, prefer_text
                calls += 1
                if calls == 1:
                    raise TransientRequestError("page count timed out", timed_out=True, splittable=True)
                return []

            with patch("archive_scout.cdx.client.HttpClient.get_cdx_any", new=fake_get):
                index_archive(config, database, threading.Event())

            self.assertGreater(calls, 1)
            self.assertLess(calls, 20)
            self.assertEqual(database.execute("SELECT complete FROM index_state").fetchone()[0], 1)
            database.close()

    def test_timed_out_page_count_does_not_multiply_long_waits(self):
        class CountClient:
            retry_callback = None

            def __init__(self):
                self.calls = 0

            def get_cdx_any(self, _urls, _params, max_bytes=0, prefer_text=False):
                del max_bytes, prefer_text
                self.calls += 1
                raise TransientRequestError("count timeout", timed_out=True, splittable=True)

        client = CountClient()
        config = ProjectConfig(
            output_dir=Path("."), targets=["example.com/*"], keywords=[], retries=1
        ).normalized()
        with self.assertRaises(TransientRequestError):
            _request_paged_count(
                client,
                ("https://web.archive.org/web/timemap/json",),
                [("showNumPages", "true")],
                config,
                threading.Event(),
            )
        self.assertEqual(client.calls, 1)

    def test_paged_index_emits_live_page_progress(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = ProjectConfig(
                output_dir=root,
                targets=["example.com/*"],
                keywords=[],
                from_date="20010101",
                to_date="20010101",
                cdx_delay=0,
                network=NetworkConfig(index_strategy="paged", page_blocks=2, cdx_workers=2),
            ).normalized()
            database = open_database(root)
            messages: list[str] = []

            def fake_get(_self, _urls, params, max_bytes=0, prefer_text=False):
                del max_bytes, prefer_text
                values = dict(params)
                if values.get("showNumPages") == "true":
                    return 2
                page = int(values["page"])
                return [HEADER, [f"2001010100000{page}", f"http://example.com/{page}", "text/html", "200", str(page), "1"]]

            with patch("archive_scout.cdx.client.HttpClient.get_cdx_any", new=fake_get):
                index_archive(
                    config,
                    database,
                    threading.Event(),
                    callback=lambda event: messages.append(event.message),
                )
            self.assertTrue(any("completed 2/2 Timemap pages" in message for message in messages))
            database.close()

    def test_text_output_is_requested_first_for_bulk_pages(self):
        body = b"20010101000000 text/html 200 ABC 1 http://example.com/page\n"
        transport = _TextTransport(body)
        client = HttpClient(
            FixedRateLimiter(0),
            1,
            5,
            "test",
            threading.Event(),
            transport=transport,
        )
        payload = client.get_cdx_any(
            ("https://example.invalid/cdx",),
            [("url", "example.com/*"), ("output", "json"), ("fl", ",".join(HEADER))],
            prefer_text=True,
        )
        client.close()
        self.assertIn("output=txt", transport.urls[0])
        self.assertEqual(payload[1][-1], "http://example.com/page")

    def test_timeout_does_not_cascade_across_every_endpoint(self):
        client = HttpClient(FixedRateLimiter(0), 1, 5, "test", threading.Event(), transport=_TextTransport(b""))
        timeout = TransientRequestError("read timed out", timed_out=True, splittable=True)
        with patch.object(client, "get", side_effect=timeout) as mocked:
            with self.assertRaises(TransientRequestError):
                client.get_cdx_any(
                    ("https://one.invalid/cdx", "https://two.invalid/cdx"),
                    [("output", "json")],
                    prefer_text=True,
                )
        self.assertEqual(mocked.call_count, 1)
        client.close()

    def test_media_reuses_completed_main_index_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = ProjectConfig(
                output_dir=root,
                targets=["example.com/*"],
                keywords=[],
                from_date="2001",
                to_date="2001",
                cdx_delay=0,
                network=NetworkConfig(index_strategy="paged"),
                media=MediaConfig(
                    enabled=True,
                    include_images=True,
                    include_videos=False,
                    include_extensions=["jpg"],
                    discover_embedded=False,
                ),
            ).normalized()
            database = open_database(root)
            target_id = get_or_create_target(database, "example.com/*")
            signature = cdx_query_signature(config)
            rows = [
                {"timestamp": "20010101000000", "original": "http://example.com/a.jpg", "mimetype": "image/jpeg", "statuscode": "200", "digest": "A", "length": "10"},
                {"timestamp": "20010101000001", "original": "http://example.com/a.html", "mimetype": "text/html", "statuscode": "200", "digest": "B", "length": "10"},
            ]
            upsert_captures(database, rows, target_id, signature)
            database.execute(
                "INSERT INTO index_state(target_id,year,query_signature,resume_key,complete,seen,error_id,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (target_id, 2001, signature, None, 1, 2, None, utc_now()),
            )
            database.commit()
            with patch("archive_scout.cdx.client.HttpClient.get_cdx_any", side_effect=AssertionError("network should not run")):
                index_media(config, database, threading.Event())
            row = database.execute("SELECT original_url,source_type FROM media_captures").fetchone()
            self.assertEqual(row["original_url"], "http://example.com/a.jpg")
            self.assertEqual(row["source_type"], "main_index")
            database.close()

    def test_earlier_defaults_migrate_to_fast_safe_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "project.json"
            path.write_text(
                json.dumps(
                    {
                        "version": "prerelease",
                        "output_dir": temp,
                        "targets": ["example.com/*"],
                        "keywords": [],
                        "from_date": "2001",
                        "to_date": "2001",
                        "page_size": 5000,
                        "cdx_delay": 1.0,
                        "network": {"page_blocks": 1},
                    }
                ),
                encoding="utf-8",
            )
            config = load_project_config(path)
            self.assertEqual(config.page_size, 100000)
            self.assertEqual(config.cdx_delay, 0.75)
            self.assertEqual(config.network.page_blocks, 9)
            self.assertEqual(config.network.cdx_workers, 10)


if __name__ == "__main__":
    unittest.main()
