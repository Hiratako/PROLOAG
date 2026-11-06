from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core import google_share_ui as ui
from app_core import google_share_service as service
from app_core import json_store
from app_core.google_sheets_client import GoogleShareError


class FakeClient:
    def __init__(self):
        self.writes = []
        self.publication = {}
        self.fail_after_commit = False

    def create_spreadsheet(self, title):
        return {"spreadsheet_id": "fixture-book"}

    def read_publication(self, spreadsheet_id):
        return self.publication

    def publish(self, spreadsheet_id, snapshot):
        self.writes.append(deepcopy(snapshot))
        self.publication = {"snapshot_id": snapshot["snapshot_id"], "payload_hash": service.payload_hash(snapshot)}
        if self.fail_after_commit:
            raise GoogleShareError("応答不明", uncertain=True)


class GoogleShareUITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag_google_ui_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        log = self.root / "study_logs/2026/09/20260916.json"
        log.parent.mkdir(parents=True)
        log.write_text(json.dumps([{"session_id": "s1", "start_at": "2026-09-16T09:00:00",
                                   "duration_sec": 3600, "memo": "PRIVATE_MEMO", "money": 99999}]), encoding="utf-8")
        self.log_bytes = log.read_bytes()
        self.log = log
        self.client = FakeClient()
        self.status = {"connected": False, "dependencies_available": False, "supported": True,
                       "spreadsheet_id": None, "url": None}
        original_read = json_store._read
        def isolated_read(path):
            self.assertTrue(Path(path).resolve().is_relative_to(self.root))
            return original_read(path)
        for patcher in (
            patch.object(ui, "app_today", return_value=date(2026, 9, 16)),
            patch.object(ui, "connection_status", side_effect=lambda **kwargs: deepcopy(self.status)),
            patch.object(ui.GoogleSheetsClient, "from_saved_connection", return_value=self.client),
            patch.object(json_store, "_read", side_effect=isolated_read),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        from streamlit import config as streamlit_config
        streamlit_config.get_config_options(options_from_flags={"global.developmentMode": False,
                                                                 "browser.gatherUsageStats": False})

    def app(self):
        from streamlit.testing.v1 import AppTest
        return AppTest.from_string(
            "from pathlib import Path\nfrom app_core.google_share_ui import render_google_share\n"
            f"render_google_share(Path({str(self.root)!r}))", default_timeout=30).run()

    def connect(self):
        self.status.update(connected=True, dependencies_available=True, spreadsheet_id="fixture-book",
                           url="https://docs.google.com/spreadsheets/d/fixture-book/edit")

    @staticmethod
    def preview(app):
        return next(button for button in app.button if button.label == "プレビューを作成・更新").click().run()

    @staticmethod
    def approval(app):
        return next(item for item in app.checkbox if item.label == "このプレビューの内容と共有先を確認しました")

    def test_initial_render_no_network_no_state_write_optional_dependency_missing(self):
        app = self.app()
        self.assertFalse(app.exception)
        self.assertEqual(self.client.writes, [])
        self.assertFalse((self.root / "backups/google_share").exists())
        self.assertTrue(app.button(key="google_share_connect").disabled)

    def test_preview_works_without_auth_and_is_private_by_default(self):
        app = self.preview(self.app())
        self.assertFalse(app.exception)
        snapshot = app.session_state["google_share_preview"]
        self.assertEqual(snapshot["counts"]["study_sessions"], 1)
        self.assertNotIn("PRIVATE_MEMO", json.dumps(snapshot))
        self.assertNotIn("99999", json.dumps(snapshot))
        self.assertTrue(app.button(key="google_share_send").disabled)
        self.assertEqual(self.log_bytes, self.log.read_bytes())

    def test_explicit_memo_selection_and_send_success_feedback(self):
        self.connect()
        app = self.app()
        app.checkbox(key="google_share_content").check()
        app.text_area(key="google_share_note").input("統計を優先したい")
        app = self.preview(app)
        self.assertIn("PRIVATE_MEMO", json.dumps(app.session_state["google_share_preview"]))
        self.assertTrue(app.button(key="google_share_send").disabled)
        self.approval(app).check().run()
        app.button(key="google_share_send").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(len(self.client.writes), 1)
        self.assertTrue(any("共有が完了" in item.value for item in app.success))
        self.assertTrue(any("fixture-book" in item.value for item in app.code))
        self.assertIsNone(service.load_pending_share(self.root))
        self.assertEqual(self.log_bytes, self.log.read_bytes())
        # A new browser session also must not re-send the last successful payload.
        # AppTest retains removed dynamic checkbox nodes across explicit reruns.
        app = self.app()
        self.assertEqual(len(self.client.writes), 1)

    def test_unknown_response_can_be_checked_after_reload(self):
        self.connect()
        self.client.fail_after_commit = True
        app = self.preview(self.app())
        self.approval(app).check().run()
        app.button(key="google_share_send").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.error)
        app.run()
        self.assertFalse(app.exception)
        next(button for button in app.button if button.label == "送信済みか確認").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(len(self.client.writes), 1)
        self.assertIsNone(service.load_pending_share(self.root))

    def test_destination_change_resets_approval(self):
        self.connect()
        app = self.preview(self.app())
        self.approval(app).check().run()
        self.assertFalse(app.button(key="google_share_send").disabled)
        self.status.update(spreadsheet_id="another-book", url="https://docs.google.com/spreadsheets/d/another-book/edit")
        app.run()
        self.assertFalse(self.approval(app).value)
        self.assertTrue(app.button(key="google_share_send").disabled)

    def test_stale_pending_buttons_never_act_on_replacement_operation(self):
        self.connect()
        snapshot = self.preview(self.app()).session_state["google_share_preview"]
        with patch.object(self.client, "publish", side_effect=GoogleShareError("未送信", uncertain=False)):
            with self.assertRaises(GoogleShareError):
                service.publish_share(self.client, snapshot, root=self.root)
        original = service.load_pending_share(self.root)
        for label in ("送信済みか確認", "同じ内容を再送", "未送信のプレビューを取り消す"):
            with self.subTest(label=label):
                service._save_pending(self.root, original)
                app = self.app()
                stale = next(button for button in app.button if button.label == label)
                replacement = deepcopy(original)
                replacement["operation_id"] = "replacement-" + original["operation_id"]
                service._save_pending(self.root, replacement)
                pending_path = self.root / "backups/google_share/pending.json"
                before = pending_path.read_bytes()
                self.client.publication = {"snapshot_id": snapshot["snapshot_id"], "payload_hash": replacement["payload_hash"]}
                with patch.object(self.client, "read_publication", wraps=self.client.read_publication) as read:
                    stale.click().run()
                    read.assert_not_called()
                self.assertFalse(app.exception)
                self.assertEqual(pending_path.read_bytes(), before)
                self.assertEqual(self.client.writes, [])
                self.assertNotIn("last_success", service.load_share_state(self.root))
        self.assertEqual(self.log.read_bytes(), self.log_bytes)

    def test_bad_source_invalidates_previous_preview(self):
        app = self.preview(self.app())
        self.log.write_text("[broken", encoding="utf-8")
        app = self.preview(app)
        self.assertFalse(app.exception)
        self.assertTrue(app.error)
        self.assertNotIn("google_share_preview", app.session_state)
        self.assertEqual(self.client.writes, [])

    def test_unreadable_connection_has_explicit_local_reset_without_pending(self):
        self.status["error_code"] = "private_storage"
        app = self.app()
        self.assertFalse(app.exception)
        self.assertTrue(app.button(key="google_share_disconnect").disabled)
        app.checkbox(key="google_share_forget_confirm").check().run()
        self.assertFalse(app.button(key="google_share_disconnect").disabled)
        with patch.object(ui, "disconnect") as forget:
            app.button(key="google_share_disconnect").click().run()
        self.assertFalse(app.exception)
        forget.assert_called_once_with(root=self.root)


if __name__ == "__main__":
    unittest.main()
