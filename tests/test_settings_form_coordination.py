from __future__ import annotations

from contextvars import Context
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core import json_store as store, settings_service as settings, settings_edit_service as edits
from app_core.form_snapshot import discard_form_snapshot, form_snapshot


class SettingsFormCoordinationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag-settings-forms-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "settings" / "user_settings.json"
        for patcher in (patch.object(store, "PROJECT_ROOT", self.root),
                        patch.object(settings, "USER_SETTINGS_PATH", self.path),
                        patch.object(edits, "USER_SETTINGS_PATH", self.path)):
            patcher.start()
            self.addCleanup(patcher.stop)
        settings.save_user_settings({"timer": {"hourly_rate": 2000}, "future": {"keep": True}})
        self.state = {}
        self.general = self.capture("user_settings_form")
        self.countdown = self.capture("countdown_settings_form")
        self.sidebar = self.capture("sidebar_visibility_form")

    def capture(self, key):
        return form_snapshot(self.state, key, [self.path], settings.load_user_settings())

    def test_own_partial_updates_advance_siblings_without_resetting_drafts(self):
        self.state["unsubmitted_general_widget"] = 3100
        edits.save_settings_form_patch(self.state, "countdown_settings_form", self.countdown,
                                       {"countdown": {"enabled": True, "title": "local", "target_date": "2026-12-01"}})
        sidebar = self.capture("sidebar_visibility_form")
        edits.save_settings_form_patch(self.state, "sidebar_visibility_form", sidebar,
                                       {"display": {"hidden_sidebar_pages": ["pages/example.py"]}})
        general = self.capture("user_settings_form")
        self.assertEqual(general["version"], self.general["version"])
        self.assertEqual(general["operation_id"], self.general["operation_id"])
        self.assertEqual(self.state["unsubmitted_general_widget"], 3100)
        edits.save_settings_form_patch(self.state, "user_settings_form", general, {"timer": {"hourly_rate": 3100}})
        saved = settings.load_user_settings()
        self.assertEqual(saved["timer"]["hourly_rate"], 3100)
        self.assertEqual(saved["countdown"]["title"], "local")
        self.assertEqual(saved["display"]["hidden_sidebar_pages"], ["pages/example.py"])
        self.assertEqual(saved["future"], {"keep": True})

    def test_external_change_rejects_both_partial_and_general_forms(self):
        Context().run(lambda: settings.update_user_settings({"timer": {"hourly_rate": 4000}}))
        before = self.path.read_bytes()
        for key, edit, updates in (
            ("countdown_settings_form", self.countdown, {"countdown": {"title": "stale"}}),
            ("user_settings_form", self.general, {"timer": {"hourly_rate": 3100}}),
        ):
            with self.subTest(key=key), self.assertRaises(store.JsonConflictError):
                edits.save_settings_form_patch(self.state, key, edit, updates)
            self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.capture("user_settings_form"), self.general)

    def test_refreshing_only_partial_form_cannot_authorize_stale_general(self):
        Context().run(lambda: settings.update_user_settings({"timer": {"hourly_rate": 4000}}))
        discard_form_snapshot(self.state, "countdown_settings_form")
        fresh_countdown = self.capture("countdown_settings_form")
        edits.save_settings_form_patch(self.state, "countdown_settings_form", fresh_countdown,
                                       {"countdown": {"title": "own update after explicit reload"}})
        still_stale = self.capture("user_settings_form")
        self.assertEqual(still_stale, self.general)
        with self.assertRaises(store.JsonConflictError):
            edits.save_settings_form_patch(self.state, "user_settings_form", still_stale,
                                           {"timer": {"hourly_rate": 3100}})
        self.assertEqual(settings.load_user_settings()["timer"]["hourly_rate"], 4000)


if __name__ == "__main__":
    unittest.main()
