from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core import consultation_report_service as service
from app_core import consultation_report_ui as ui
from app_core import google_share_ui as share_ui
from app_core import json_store
from app_core.google_sheets_client import GoogleShareError


class FakeDocsClient:
    """In-memory remote state: no vault, credentials, browser, or Google calls."""

    def __init__(self, status):
        self.status = status
        self.calls = []
        self.writes = []
        self.publications = {}
        self.fail_after_commit = False
        self.fail_before_commit = False

    def create_report_document(self, kind):
        self.calls.append(("create", kind))
        destination = {"document_id": f"fixture-{kind}",
                       "url": f"https://docs.google.com/document/d/fixture-{kind}/edit"}
        self.status.setdefault("docs", {})[kind] = destination
        return deepcopy(destination)

    def read_report_publication(self, kind):
        self.calls.append(("read", kind))
        destination = self.status.get("docs", {}).get(kind, {})
        return {"document_id": destination.get("document_id"), **deepcopy(self.publications.get(kind, {}))}

    def publish_report(self, kind, report, *, expected_document_id):
        self.calls.append(("publish", kind))
        if expected_document_id != self.status["docs"][kind]["document_id"]:
            raise GoogleShareError("共有先が変わりました。", code="destination")
        if self.fail_before_commit:
            raise GoogleShareError("送信できませんでした。", code="unavailable", uncertain=False)
        self.writes.append((kind, deepcopy(report)))
        self.publications[kind] = {"snapshot_id": report["snapshot_id"], "payload_hash": service.payload_hash(report)}
        if self.fail_after_commit:
            raise GoogleShareError("応答が失われました。", code="timeout", uncertain=True)


class ConsultationReportUITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag-docs-ui-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.log = self.write("study_logs/2026/09/20260916.json", [{
            "session_id": "s1", "start_at": "2026-09-16T09:00:00", "duration_sec": 3600,
            "material_id": "m1", "memo": "PRIVATE_STUDY_MEMO", "money": 87654321,
        }])
        self.write("master_data/master.json", {"projects": [], "subjects": [], "materials": [
            {"material_id": "m1", "name": "同じ教材名", "is_active": True},
            {"material_id": "m2", "name": "同じ教材名", "is_active": True},
        ]})
        self.resource = "https://docs.google.com/document/d/PRIVATE_LINK_M1/edit"
        self.write("settings/learning_context.json", {"schema_version": 1, "materials": {
            key: {"material_id": key, "next_step": f"PRIVATE_NEXT_{key}",
                  "created_at": "2026-09-16T09:00:00+09:00", "updated_at": "2026-09-16T10:00:00+09:00",
                  "links": [{"link_id": f"link_{key}", "title": f"資料{key}",
                             "url": self.resource if key == "m1" else "https://drive.google.com/file/d/PRIVATE_LINK_M2/view"}]}
            for key in ("m1", "m2")}})
        self.original_sources = self.source_snapshot()
        self.status = {"connected": False, "dependencies_available": False, "supported": True,
                       "spreadsheet_id": None, "url": None, "docs": {}}
        self.client = FakeDocsClient(self.status)
        self.read_paths = []
        original_read = json_store._read

        def isolated_read(path):
            resolved = Path(path).resolve()
            self.assertTrue(resolved.is_relative_to(self.root), f"Read outside fixture: {resolved}")
            self.read_paths.append(resolved)
            return original_read(path)

        for patcher in (
            patch.object(ui, "app_today", return_value=date(2026, 9, 16)),
            patch.object(ui, "connection_status", side_effect=lambda **kwargs: deepcopy(self.status)),
            patch.object(ui.GoogleDocsClient, "from_saved_connection", return_value=self.client),
            patch.object(share_ui.GoogleSheetsClient, "connect", side_effect=AssertionError("Real auth is forbidden")),
            patch.object(share_ui, "disconnect", side_effect=AssertionError("Real vault is forbidden")),
            patch.object(json_store, "_read", side_effect=isolated_read),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        from streamlit import config as streamlit_config
        streamlit_config.get_config_options(options_from_flags={"global.developmentMode": False,
                                                                 "browser.gatherUsageStats": False})

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def source_snapshot(self):
        return {path.relative_to(self.root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
                for folder in ("study_logs", "master_data", "settings")
                for path in (self.root / folder).rglob("*") if path.is_file()}

    def assert_sources_unchanged(self):
        self.assertEqual(self.source_snapshot(), self.original_sources)

    def app(self):
        from streamlit.testing.v1 import AppTest
        return AppTest.from_string(
            "from pathlib import Path\nfrom app_core.consultation_report_ui import render_consultation_reports\n"
            f"render_consultation_reports(Path({str(self.root)!r}))", default_timeout=30).run()

    def connect(self, *, destinations=True):
        self.status.update(connected=True, dependencies_available=True, spreadsheet_id="fixture-sheet",
                           url="https://docs.google.com/spreadsheets/d/fixture-sheet/edit")
        if destinations:
            self.status["docs"] = {kind: {"document_id": f"fixture-{kind}",
                                          "url": f"https://docs.google.com/document/d/fixture-{kind}/edit"}
                                   for kind in ("daily", "weekly")}

    @staticmethod
    def preview(app):
        return next(item for item in app.button if item.label == "相談レポートのプレビューを作成").click().run()

    @staticmethod
    def approval(app):
        return next(item for item in app.checkbox if item.label == "このレポートの内容と送信先を確認しました")

    def send(self, app):
        self.approval(app).check().run()
        return app.button(key="docs_report_send").click().run()

    def test_initial_render_no_writes_or_remote_calls_and_shared_connect_disabled(self):
        before = self.source_snapshot()
        app = self.app()
        self.assertFalse(app.exception)
        self.assertEqual(self.client.calls, [])
        self.assertTrue(self.read_paths)
        self.assertFalse((self.root / "backups/google_share").exists())
        self.assertTrue(app.button(key="google_share_connect").disabled)
        self.assertTrue(app.button(key="docs_report_destination").disabled)
        self.assertEqual(self.source_snapshot(), before)

    def test_unconnected_preview_is_private_read_only_and_cannot_send_after_approval(self):
        app = self.preview(self.app())
        self.assertFalse(app.exception)
        report = app.session_state["docs_report_preview"]
        self.assertEqual(report["counts"]["study_sessions"], 1)
        self.assertEqual(report["period"]["start"], "2026-09-10")
        self.assertEqual(report["period"]["end"], "2026-09-16")
        for private in ("PRIVATE_STUDY_MEMO", "PRIVATE_NEXT_", "PRIVATE_LINK_", "87654321"):
            self.assertNotIn(private, json.dumps(report, ensure_ascii=False))
        self.approval(app).check().run()
        self.assertTrue(app.button(key="docs_report_send").disabled)
        self.assertEqual(self.client.calls, [])
        self.assert_sources_unchanged()

    def test_memo_links_and_next_step_are_independent_opt_ins_for_selected_id(self):
        for key, included, excluded in (
            ("docs_report_links", "PRIVATE_LINK_M1", ("PRIVATE_NEXT_m1", "PRIVATE_STUDY_MEMO")),
            ("docs_report_next", "PRIVATE_NEXT_m1", ("PRIVATE_LINK_M1", "PRIVATE_STUDY_MEMO")),
            ("docs_report_memos", "PRIVATE_STUDY_MEMO", ("PRIVATE_NEXT_m1", "PRIVATE_LINK_M1")),
        ):
            with self.subTest(key=key):
                app = self.app()
                app.checkbox(key=key).check()
                app.multiselect(key="docs_report_materials").set_value(["m1"])
                app = self.preview(app)
                self.assertFalse(app.exception)
                text = app.session_state["docs_report_preview"]["text"]
                self.assertIn(included, text)
                for value in (*excluded, "PRIVATE_LINK_M2", "PRIVATE_NEXT_m2"):
                    self.assertNotIn(value, text)
        self.assertEqual(self.client.calls, [])
        self.assert_sources_unchanged()

    def test_context_opt_in_requires_explicit_material_selection(self):
        app = self.app()
        app.checkbox(key="docs_report_links").check()
        app = self.preview(app)
        self.assertFalse(app.exception)
        self.assertTrue(any("教材を選択" in item.value for item in app.error))
        self.assertNotIn("docs_report_preview", app.session_state)
        self.assertEqual(self.client.calls, [])

    def test_send_is_disabled_until_approval_then_has_success_feedback_without_rerun_resend(self):
        self.connect()
        app = self.preview(self.app())
        self.assertTrue(app.button(key="docs_report_send").disabled)
        self.assertEqual(self.client.writes, [])
        app = self.send(app)
        self.assertFalse(app.exception)
        self.assertEqual(len(self.client.writes), 1)
        self.assertTrue(any("相談レポートを共有しました" in item.value for item in app.success))
        self.assertTrue(any("fixture-weekly" in item.value for item in app.code))
        self.assertIsNone(service.load_pending_report(self.root))
        self.assertNotIn("docs_report_preview", app.session_state)
        self.app()
        self.assertEqual(len(self.client.writes), 1)
        self.assert_sources_unchanged()

    def test_destination_creation_is_explicit_and_does_not_publish_preview(self):
        self.connect(destinations=False)
        app = self.preview(self.app())
        self.approval(app).check().run()
        self.assertTrue(app.button(key="docs_report_send").disabled)
        self.assertEqual(self.client.calls, [])
        app.button(key="docs_report_destination").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(self.client.calls, [("create", "weekly")])
        self.assertEqual(self.client.writes, [])
        self.assertFalse(self.approval(app).value)
        self.assertTrue(app.button(key="docs_report_send").disabled)
        self.assert_sources_unchanged()

    def test_destination_change_resets_approval(self):
        self.connect()
        app = self.preview(self.app())
        self.approval(app).check().run()
        self.assertFalse(app.button(key="docs_report_send").disabled)
        self.status["docs"]["weekly"] = {"document_id": "other-doc",
                                           "url": "https://docs.google.com/document/d/other-doc/edit"}
        app.run()
        self.assertFalse(app.exception)
        self.assertFalse(self.approval(app).value)
        self.assertTrue(app.button(key="docs_report_send").disabled)
        self.assertEqual(self.client.writes, [])

    def test_kind_change_clears_old_preview_and_daily_is_one_selected_day(self):
        app = self.preview(self.app())
        self.assertEqual(app.session_state["docs_report_preview"]["kind"], "weekly")
        app.radio(key="docs_report_kind").set_value("daily").run()
        self.assertFalse(app.exception)
        self.assertNotIn("docs_report_preview", app.session_state)
        self.assertFalse(any(item.key == "docs_report_send" for item in app.button))
        app = self.preview(app)
        report = app.session_state["docs_report_preview"]
        self.assertEqual(report["kind"], "daily")
        self.assertEqual(report["period"]["start"], "2026-09-16")
        self.assertEqual(report["period"]["end"], "2026-09-16")

    def test_lost_response_can_be_confirmed_and_blocks_shared_disconnect(self):
        self.connect()
        self.client.fail_after_commit = True
        app = self.send(self.preview(self.app()))
        self.assertFalse(app.exception)
        self.assertTrue(app.error)
        self.assertEqual(service.load_pending_report(self.root)["status"], "uncertain")
        app.run()
        self.assertTrue(app.checkbox(key="google_share_forget_confirm").disabled)
        self.assertTrue(app.button(key="google_share_disconnect").disabled)
        self.assertFalse(any(item.label == "未送信レポートを取り消す" for item in app.button))
        next(item for item in app.button if item.label == "レポート送信済みか確認").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(len(self.client.writes), 1)
        self.assertIsNone(service.load_pending_report(self.root))
        self.assertTrue(any("共有の完了を確認" in item.value for item in app.success))
        self.assert_sources_unchanged()

    def test_failed_before_commit_can_discard_without_publishing(self):
        self.connect()
        self.client.fail_before_commit = True
        app = self.send(self.preview(self.app()))
        self.assertFalse(app.exception)
        self.assertEqual(service.load_pending_report(self.root)["status"], "failed")
        app.run()
        next(item for item in app.button if item.label == "未送信レポートを取り消す").click().run()
        self.assertFalse(app.exception)
        self.assertIsNone(service.load_pending_report(self.root))
        self.assertEqual(self.client.writes, [])
        self.assert_sources_unchanged()

    def test_corrupt_source_clears_previous_preview_and_cannot_send_stale_content(self):
        self.connect()
        app = self.preview(self.app())
        self.approval(app).check().run()
        self.log.write_text("[broken", encoding="utf-8")
        app = self.preview(app)
        self.assertFalse(app.exception)
        self.assertTrue(app.error)
        self.assertNotIn("docs_report_preview", app.session_state)
        self.assertFalse(any(item.key == "docs_report_send" for item in app.button))
        self.assertEqual(self.client.writes, [])
        self.assertEqual(self.log.read_text(encoding="utf-8"), "[broken")

    def test_stale_pending_buttons_do_not_act_on_replacement_operation(self):
        self.connect()
        self.client.fail_before_commit = True
        self.send(self.preview(self.app()))
        pending = service.load_pending_report(self.root)
        self.assertEqual(pending["status"], "failed")
        for label in ("レポート送信済みか確認", "同じレポートを再送", "未送信レポートを取り消す"):
            with self.subTest(label=label):
                self.write("backups/google_share/report_pending.json", pending)
                app = self.app()
                old_button = next(item for item in app.button if item.label == label)
                replacement = deepcopy(pending)
                replacement["operation_id"] = "f" * 32
                self.assertNotEqual(replacement["operation_id"], pending["operation_id"])
                path = self.write("backups/google_share/report_pending.json", replacement)
                before = path.read_bytes()
                calls = deepcopy(self.client.calls)
                old_button.click().run()
                self.assertFalse(app.exception)
                self.assertEqual(self.client.calls, calls)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(service.load_pending_report(self.root)["operation_id"], replacement["operation_id"])
        self.assert_sources_unchanged()

    def test_send_uses_fixed_preview_even_if_source_is_edited_later(self):
        self.connect()
        app = self.app()
        app.checkbox(key="docs_report_memos").check()
        app = self.preview(app)
        approved = deepcopy(app.session_state["docs_report_preview"])
        self.write("study_logs/2026/09/20260916.json", [{"session_id": "s1", "start_at": "2026-09-16T09:00:00",
                                                       "duration_sec": 7200, "memo": "CHANGED_AFTER_PREVIEW"}])
        after_edit = self.source_snapshot()
        app = self.send(app)
        self.assertFalse(app.exception)
        self.assertEqual(self.client.writes, [("weekly", approved)])
        self.assertEqual(self.source_snapshot(), after_edit)


if __name__ == "__main__":
    unittest.main()
