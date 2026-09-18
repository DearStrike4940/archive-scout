from __future__ import annotations

import unittest

from archive_scout.ui import main_window
from archive_scout.ui.event_queue import CoalescingEventQueue


class StartupRegressionTests(unittest.TestCase):
    def test_main_window_imports_bounded_event_queue(self):
        self.assertIs(main_window.CoalescingEventQueue, CoalescingEventQueue)

    def test_public_version_is_official_release(self):
        self.assertEqual(main_window.VERSION, "1.0.7.1")


if __name__ == "__main__":
    unittest.main()
