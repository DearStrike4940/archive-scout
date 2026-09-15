from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from archive_scout.config import NetworkConfig, ProjectConfig
from archive_scout.database.connection import open_database
from archive_scout.operations import run_project


class EndToEndTests(unittest.TestCase):
    def test_mocked_index_download_scan_rescan_and_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = ProjectConfig(
                output_dir=root,
                targets=["example.com/*"],
                keywords=["WTC", "jumpers"],
                keyword_set_name="First search",
                from_date="2006",
                to_date="2006",
                workers=2,
                cdx_delay=0,
                download_delay=0,
                network=NetworkConfig(index_strategy="resume"),
            )
            cdx_payload = [
                ["timestamp", "original", "mimetype", "statuscode", "digest", "length"],
                ["20060102030405", "http://example.com/thread/1", "text/html", "200", "ABC", "100"],
            ]
            page = b"<html><title>9/11 discussion</title><body>WTC jumpers and canopy footage</body></html>"

            with patch("archive_scout.cdx.client.HttpClient.get_cdx_any", return_value=cdx_payload), patch(
                "archive_scout.cdx.client.HttpClient.get",
                return_value={
                    "data": page,
                    "status": 200,
                    "headers": {"Content-Type": "text/html; charset=utf-8"},
                    "final_url": "https://web.archive.org/web/20060102030405id_/http://example.com/thread/1",
                },
            ):
                paths = run_project(config, "all")
            self.assertTrue(paths["matches_ranked"].exists())
            self.assertIn("WTC", paths["matches_ranked"].read_text(encoding="utf-8"))

            database = open_database(root)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM captures").fetchone()[0], 1)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM scan_runs WHERE status='complete'").fetchone()[0], 1)
            database.close()

            second = ProjectConfig(
                output_dir=root,
                targets=["example.com/*"],
                keywords=["canopy"],
                keyword_set_name="Second search",
                from_date="2006",
                to_date="2006",
            )
            second_paths = run_project(second, "rescan")
            self.assertIn("canopy", second_paths["matches_ranked"].read_text(encoding="utf-8").casefold())
            all_matches = second_paths["all_matches_ranked"].read_text(encoding="utf-8")
            self.assertIn("WTC", all_matches)
            self.assertIn("canopy", all_matches.casefold())
            self.assertIn("SCAN RUN: 1", all_matches)
            self.assertIn("SCAN RUN: 2", all_matches)
            markdown = second_paths["all_matches_markdown"].read_text(encoding="utf-8")
            self.assertIn("# Archive Scout combined qualifying matches", markdown)
            self.assertIn("## 1.", markdown)
            with second_paths["all_matches_csv"].open(encoding="utf-8", newline="") as handle:
                spreadsheet = list(csv.DictReader(handle))
            self.assertEqual(len(spreadsheet), 2)
            self.assertEqual({row["scan_run"] for row in spreadsheet}, {"1", "2"})
            self.assertIn("original_url", spreadsheet[0])
            database = open_database(root)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM scan_runs WHERE status='complete'").fetchone()[0], 2)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM document_matches").fetchone()[0], 2)
            database.close()


if __name__ == "__main__":
    unittest.main()
