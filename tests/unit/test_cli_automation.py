from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from archive_scout.cli import EXIT_OK, cli_main
from archive_scout.config import ProjectConfig, save_project_config
from archive_scout.database.connection import open_database
from archive_scout.events import ProgressEvent


class CLIAutomationTests(unittest.TestCase):
    def test_progress_event_has_stable_machine_shape(self):
        payload = ProgressEvent('index', 'Working', 3, 10, {'target': 'example.com'}).to_dict()
        self.assertEqual(payload, {
            'stage': 'index', 'message': 'Working', 'current': 3, 'total': 10, 'detail': {'target': 'example.com'}
        })

    def test_init_and_status_json_are_machine_readable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / 'project.json'
            stdout = io.StringIO(); stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli_main(['init', str(project), '--output-dir', str(root), '--target', 'example.com/*', '--format', 'json'])
            self.assertEqual(code, EXIT_OK)
            created = json.loads(stdout.getvalue())
            self.assertEqual(created['status'], 'created')
            self.assertEqual(stderr.getvalue(), '')

            db = open_database(root)
            db.close()
            stdout = io.StringIO(); stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli_main(['status', str(project), '--format', 'json'])
            self.assertEqual(code, EXIT_OK)
            status = json.loads(stdout.getvalue())
            self.assertTrue(status['database'])
            self.assertEqual(status['version'], '1.0.7.1')
            self.assertEqual(stderr.getvalue(), '')

    def test_readonly_commands_do_not_write_project_database(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = ProjectConfig(output_dir=root, targets=['example.com/*'], keywords=[]).normalized()
            project = save_project_config(config)
            db = open_database(root)
            db.execute("INSERT INTO site_issues(target,host,stage,category,http_status,message,occurrence_count,resolved,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       ('example.com','example.com','index','no_captures',0,'none',1,0,'x','x'))
            db.commit(); db.close()
            path = root / 'archive_scout.sqlite3'
            before = path.stat().st_mtime_ns
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = cli_main(['errors', str(project), '--format', 'json'])
            self.assertEqual(code, EXIT_OK)
            json.loads(stdout.getvalue())
            self.assertEqual(path.stat().st_mtime_ns, before)


if __name__ == '__main__':
    unittest.main()
