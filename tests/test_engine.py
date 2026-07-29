from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from archive_scout.engine import (
    ProjectConfig,
    analyze_content,
    build_cdx_params,
    cdx_query_signature,
    cdx_year_window,
    compile_keywords,
    generate_reports,
    normalize_cdx_date,
    normalize_target,
    open_database,
    parse_cdx_parameter_lines,
    parse_cdx,
    parse_page,
    upsert_capture,
)


class EngineTests(unittest.TestCase):
    def test_normalize_target(self):
        self.assertEqual(normalize_target("example.com"), "example.com/*")
        self.assertEqual(normalize_target("https://example.com/forum/"), "example.com/forum/*")
        self.assertEqual(normalize_target("forum.example.com/showthread.php?*"), "forum.example.com/showthread.php?*")

    def test_cdx_date_normalization_and_window(self):
        self.assertEqual(normalize_cdx_date("2001", False), "20010101000000")
        self.assertEqual(normalize_cdx_date("2001-09-11", True), "20010911235959")
        config = ProjectConfig(
            Path("."),
            ["example.com/*"],
            ["example"],
            from_date="20010901",
            to_date="20011001",
        ).normalized()
        self.assertEqual(cdx_year_window(config, 2001), ("20010901000000", "20011001235959"))

    def test_cdx_parameters(self):
        config = ProjectConfig(
            Path("."),
            ["example.com/*"],
            ["example"],
            from_date="2001",
            to_date="2001",
            cdx_filters=["statuscode:200", "mimetype:text/html"],
            cdx_collapses=["urlkey", "digest"],
            cdx_match_type="domain",
            cdx_extra_params=["resolveRevisits=true", "fastLatest=true"],
            page_size=2500,
        ).normalized()
        params = build_cdx_params(config, "example.com/*", config.from_date, config.to_date)
        self.assertIn(("filter", "statuscode:200"), params)
        self.assertIn(("filter", "mimetype:text/html"), params)
        self.assertIn(("collapse", "urlkey"), params)
        self.assertIn(("collapse", "digest"), params)
        self.assertIn(("matchType", "domain"), params)
        self.assertIn(("url", "example.com"), params)
        self.assertIn(("resolveRevisits", "true"), params)
        self.assertIn(("limit", "2500"), params)
        self.assertEqual(parse_cdx_parameter_lines(["foo=bar"]), [("foo", "bar")])
        with self.assertRaises(ValueError):
            parse_cdx_parameter_lines(["output=txt"])

    def test_cdx_signature_changes_with_options(self):
        first = ProjectConfig(Path("."), ["example.com/*"], ["x"], cdx_filters=["statuscode:200"]).normalized()
        second = ProjectConfig(Path("."), ["example.com/*"], ["x"], cdx_filters=["statuscode:404"]).normalized()
        self.assertNotEqual(cdx_query_signature(first), cdx_query_signature(second))

    def test_parse_cdx_resume_key(self):
        payload = [
            ["timestamp", "original", "mimetype", "statuscode", "digest", "length"],
            ["20010911000000", "http://example.com/a", "text/html", "200", "A", "10"],
            [],
            ["resume-token"],
        ]
        rows, resume = parse_cdx(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(resume, "resume-token")

    def test_new_query_signature_replaces_old_query_timestamp(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = open_database(Path(temporary))
            old = {
                "timestamp": "20010101000000",
                "original": "http://example.com/a",
                "mimetype": "text/html",
                "statuscode": "200",
                "digest": "old",
                "length": "50",
            }
            current = dict(old, timestamp="20050101000000", digest="current")
            with database:
                upsert_capture(database, old, "example.com/*", "old-query")
                upsert_capture(database, current, "example.com/*", "current-query")
            row = database.execute("SELECT timestamp,digest,query_signature FROM captures").fetchone()
            self.assertEqual(row["timestamp"], "20050101000000")
            self.assertEqual(row["digest"], "current")
            self.assertEqual(row["query_signature"], "current-query")
            database.close()

    def test_earliest_capture_wins(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = open_database(Path(temporary))
            later = {
                "timestamp": "20050101000000",
                "original": "http://example.com/a",
                "mimetype": "text/html",
                "statuscode": "200",
                "digest": "later",
                "length": "50",
            }
            earlier = dict(later, timestamp="20020101000000", digest="earlier")
            with database:
                upsert_capture(database, later, "example.com/*", "test")
                upsert_capture(database, earlier, "example.com/*", "test")
            row = database.execute("SELECT timestamp,digest FROM captures").fetchone()
            self.assertEqual(row["timestamp"], "20020101000000")
            self.assertEqual(row["digest"], "earlier")
            database.close()

    def test_keyword_analysis(self):
        raw = "<html><head><title>Rare WTC footage</title></head><body>September 11 jumper footage discussion</body></html>"
        title, visible, links = parse_page(raw, "http://example.com/topic")
        result = analyze_content(
            "http://example.com/topic",
            title,
            visible,
            raw,
            links,
            compile_keywords(["World Trade Center", "WTC", "September 11", "jumper footage"]),
        )
        self.assertGreater(result["score"], 0)
        self.assertIn("September 11", result["hits"])
        self.assertTrue(result["snippets"])

    def test_reports_are_plain_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = ProjectConfig(root, ["example.com/*"], ["example"], minimum_score=1).normalized()
            database = open_database(root)
            with database:
                database.execute(
                    """
                    INSERT INTO captures(
                        original,timestamp,source_target,query_signature,mimetype,statuscode,state,title,score,
                        keyword_hits,hit_fields,snippets,interesting_links,bytes_saved,updated_at
                    ) VALUES(?,?,?,?,?,?,'done',?,?,?,?,?,?,?,?)
                    """,
                    (
                        "http://example.com/a",
                        "20010101000000",
                        "example.com/*",
                        cdx_query_signature(config),
                        "text/html",
                        "200",
                        "Example",
                        4,
                        json.dumps({"example": 1}),
                        json.dumps({"example": ["title"]}),
                        json.dumps(["example text"]),
                        json.dumps([]),
                        100,
                        "now",
                    ),
                )
            paths = generate_reports(config, database)
            self.assertTrue(paths["matches"].exists())
            self.assertIn("ORIGINAL URL", paths["matches"].read_text(encoding="utf-8"))
            database.close()


if __name__ == "__main__":
    unittest.main()
