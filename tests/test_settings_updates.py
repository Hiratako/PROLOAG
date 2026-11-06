from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app_core import json_store, settings_service as service


class SettingsUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="proloag_settings_update_")
        self.path = Path(self.temp.name) / "user_settings.json"
        self.path_patch = patch.object(service, "USER_SETTINGS_PATH", self.path)
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.temp.cleanup()

    def test_new_defaults_are_opt_in_and_read_does_not_write(self):
        settings = service.load_user_settings()
        self.assertEqual(settings["countdown"], {"enabled": False, "title": "目標日", "target_date": None})
        self.assertEqual(settings["display"]["hidden_sidebar_pages"], [])
        self.assertFalse(self.path.exists())
        settings["display"]["hidden_sidebar_pages"].append("not-shared")
        self.assertEqual(service.load_user_settings()["display"]["hidden_sidebar_pages"], [])

    def test_countdown_update_preserves_other_and_unknown_settings(self):
        service.save_user_settings({"display": {"theme_mode": "night", "hidden_sidebar_pages": ["pages/07_記憶フラッシュ.py"]}, "future_extension": {"sample": 42}})
        original = service.load_user_settings()
        update = {"countdown": {"enabled": True, "title": "試験日", "target_date": "2027-04-01"}}
        expected = deepcopy(update)
        service.update_user_settings(update)
        saved = service.load_user_settings()
        self.assertEqual(saved["countdown"], update["countdown"])
        self.assertEqual(saved["display"], original["display"])
        self.assertEqual(saved["future_extension"], {"sample": 42})
        self.assertEqual(update, expected)

    def test_sidebar_update_preserves_countdown_and_general_settings(self):
        countdown = {"enabled": True, "title": "試験日", "target_date": "2027-04-01"}
        service.save_user_settings({"countdown": countdown, "display": {"show_task_metrics": False, "future_flag": "keep"}})
        service.update_user_settings({"display": {"hidden_sidebar_pages": ["pages/07_記憶フラッシュ.py"]}})
        saved = service.load_user_settings()
        self.assertEqual(saved["countdown"], countdown)
        self.assertFalse(saved["display"]["show_task_metrics"])
        self.assertEqual(saved["display"]["future_flag"], "keep")
        self.assertEqual(saved["display"]["hidden_sidebar_pages"], ["pages/07_記憶フラッシュ.py"])

    def test_general_update_preserves_new_sections_and_nested_unknown_values(self):
        service.save_user_settings({"countdown": {"enabled": True, "target_date": "2027-04-01"}, "display": {"hidden_sidebar_pages": ["pages/07_記憶フラッシュ.py"]}, "timer": {"unknown": "keep"}})
        service.update_user_settings({"timer": {"hourly_rate": 500}, "display": {"period_label": "直近7日"}})
        saved = service.load_user_settings()
        self.assertEqual(saved["timer"]["hourly_rate"], 500)
        self.assertEqual(saved["timer"]["unknown"], "keep")
        self.assertTrue(saved["countdown"]["enabled"])
        self.assertEqual(saved["display"]["hidden_sidebar_pages"], ["pages/07_記憶フラッシュ.py"])

    def test_save_without_previous_read_uses_current_file(self):
        self.path.write_bytes(b'{"extension": {"preserve": true}}')
        service.update_user_settings({"countdown": {"title": "試験日"}})
        self.assertEqual(service.load_user_settings()["extension"], {"preserve": True})

    def test_stale_read_is_not_silently_accepted_by_internal_reload(self):
        service.save_user_settings({"timer": {"hourly_rate": 100}})
        service.load_user_settings()
        failures = []

        def other_writer():
            try:
                service.update_user_settings({"timer": {"hourly_rate": 300}})
            except Exception as exc:
                failures.append(exc)

        worker = threading.Thread(target=other_writer)
        worker.start()
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        original = self.path.read_bytes()
        with self.assertRaises(json_store.JsonConflictError):
            service.update_user_settings({"countdown": {"title": "stale"}})
        self.assertEqual(self.path.read_bytes(), original)

    def test_corrupt_file_and_invalid_updates_are_not_overwritten(self):
        for value in (None, [], "invalid"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                service.update_user_settings(value)
        self.assertFalse(self.path.exists())
        self.path.write_bytes(b'{broken')
        with self.assertRaises(json_store.JsonReadError):
            service.update_user_settings({"countdown": {"enabled": True}})
        self.assertEqual(self.path.read_bytes(), b'{broken')

    def test_reset_restores_visible_pages_and_disabled_countdown(self):
        service.update_user_settings({"display": {"hidden_sidebar_pages": ["pages/07_記憶フラッシュ.py"]}, "countdown": {"enabled": True, "target_date": "2027-04-01"}})
        service.reset_user_settings()
        self.assertEqual(service.load_user_settings(), service.DEFAULT_USER_SETTINGS)


if __name__ == "__main__":
    unittest.main()
