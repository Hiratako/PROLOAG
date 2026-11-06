from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core import consultation_proposal_ui as proposals
from app_core import consultation_report_ui as reports
from app_core import google_share_ui as sheets
from app_core import json_store
from app_core import learning_context_ui as learning


class ActionNoticeAdapterTests(unittest.TestCase):
    """Exercise UI notifications without local records or Google connections."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag_action_notices_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.share_state = {}
        self.report_state = {}
        self.status = {"connected": False, "dependencies_available": False}
        self.materials = []
        for patcher in (
            patch.object(sheets, "load_share_state", side_effect=lambda root: self.share_state),
            patch.object(sheets, "load_pending_share", return_value=None),
            patch.object(sheets, "connection_status", side_effect=lambda **kwargs: self.status),
            patch.object(sheets, "_connection_panel"),
            patch.object(sheets, "app_today", return_value=date(2026, 10, 3)),
            patch.object(reports, "load_report_state", side_effect=lambda root: self.report_state),
            patch.object(reports, "load_pending_report", return_value=None),
            patch.object(reports, "load_pending_share", return_value=None),
            patch.object(reports, "connection_status", side_effect=lambda **kwargs: self.status),
            patch.object(reports, "_connection_panel"),
            patch.object(reports, "list_materials", return_value=[]),
            patch.object(reports, "app_today", return_value=date(2026, 10, 3)),
            patch.object(learning, "_read", side_effect=lambda root: ({"materials": {}}, self.materials)),
            patch.object(proposals, "load_inbox_snapshot",
                         return_value={"revision": "fixture", "data": {"proposals": {}}}),
            patch.object(json_store, "_read", side_effect=AssertionError("No JSON reads are expected")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        from streamlit import config as streamlit_config
        streamlit_config.get_config_options(options_from_flags={"global.developmentMode": False,
                                                                 "browser.gatherUsageStats": False})

    def app(self, module, function):
        from streamlit.testing.v1 import AppTest

        return AppTest.from_string(
            f"from pathlib import Path\nfrom app_core.{module} import {function}\n"
            f"{function}(Path({str(self.root)!r}))", default_timeout=30)

    @staticmethod
    def button(app, label):
        return next(button for button in app.button if button.label == label)

    def assert_clean(self, app):
        self.assertFalse(app.exception, [item.message for item in app.exception])

    def test_notices_keep_inline_feedback_and_toast_once_per_action(self):
        cases = (
            ("google_share_ui", "render_google_share", "google_share_notice"),
            ("consultation_report_ui", "render_consultation_reports", "docs_report_notice"),
            ("learning_context_ui", "render_learning_context", "learning_context_notice"),
            ("consultation_proposal_ui", "render_proposal_inbox", "proposal_inbox_notice"),
        )
        for module, function, notice_key in cases:
            with self.subTest(module=module), patch("streamlit.toast") as toast:
                app = self.app(module, function)
                app.session_state[notice_key] = "保存しました。"
                app.run()
                self.assert_clean(app)
                self.assertTrue(any(item.value == "保存しました。" for item in app.success))
                self.assertNotIn(notice_key, app.session_state)
                self.assertEqual(toast.call_count, 1)
                app.run()
                self.assert_clean(app)
                self.assertEqual(toast.call_count, 1)
                # Equal wording from a new action is a new event, not a duplicate.
                app.session_state[notice_key] = "保存しました。"
                app.run()
                self.assert_clean(app)
                self.assertEqual(toast.call_count, 2)

    def test_docs_keeps_existing_notice_priority_and_consumption_order(self):
        with patch("streamlit.toast") as toast:
            app = self.app("consultation_report_ui", "render_consultation_reports")
            app.session_state["docs_report_notice"] = "レポートを共有しました。"
            app.session_state["google_share_notice"] = "接続を解除しました。"
            app.run()
            self.assert_clean(app)
            self.assertEqual(app.session_state["google_share_notice"], "接続を解除しました。")
            self.assertNotIn("docs_report_notice", app.session_state)
            self.assertEqual(toast.call_count, 1)
            app.run()
            self.assert_clean(app)
            self.assertNotIn("google_share_notice", app.session_state)
            self.assertTrue(any(item.value == "接続を解除しました。" for item in app.success))
            self.assertEqual(toast.call_count, 2)
            app.run()
            self.assertEqual(toast.call_count, 2)

    def test_last_publication_status_remains_inline_without_toast(self):
        last = {"published_at": "2026-10-03T09:00:00+09:00", "counts": {"study_sessions": 1},
                "url": "https://docs.google.com/document/d/fixture/edit", "snapshot_id": "a" * 64}
        self.share_state["last_success"] = last
        kind = list(reports.KINDS)[1]
        self.report_state["last_success"] = {kind: last}
        cases = (
            ("google_share_ui", "render_google_share", sheets, "consultation_prompt", "最終共有成功"),
            ("consultation_report_ui", "render_consultation_reports", reports, "report_consultation_prompt", "最終レポート共有"),
        )
        for module, function, ui, prompt, label in cases:
            with self.subTest(module=module), patch("streamlit.toast") as toast, \
                    patch.object(ui, prompt, return_value="相談用の依頼文"):
                app = self.app(module, function).run()
                self.assert_clean(app)
                self.assertTrue(any(label in item.value for item in app.success))
                app.run()
                self.assert_clean(app)
                toast.assert_not_called()

    def test_shared_error_helpers_notify_only_explicit_actions(self):
        for ui, helper in ((sheets, "_safe_error"), (reports, "_error"), (proposals, "_error")):
            with self.subTest(module=ui.__name__), patch.object(ui.st, "error") as inline, \
                    patch.object(ui.ab, "show_action_error") as notify:
                error = ValueError("fixture failure")
                getattr(ui, helper)(error)
                inline.assert_called_once()
                notify.assert_not_called()
                getattr(ui, helper)(error, action=True)
                notify.assert_called_once()
                self.assertEqual(inline.call_count, 1)

    def test_failed_preview_buttons_notify_once_without_retry_on_render(self):
        cases = (
            ("google_share_ui", "render_google_share", sheets, "build_share_snapshot",
             sheets.ShareSnapshotError("プレビュー作成失敗"), "プレビューを作成・更新"),
            ("consultation_report_ui", "render_consultation_reports", reports, "build_consultation_report",
             reports.ReportError("プレビュー作成失敗"), "相談レポートのプレビューを作成"),
            ("consultation_proposal_ui", "render_proposal_inbox", proposals, "parse_proposal_payload",
             proposals.ProposalError("プレビュー作成失敗"), "取り込みプレビューを作成"),
        )
        for module, function, ui, builder, error, label in cases:
            with self.subTest(module=module), patch("streamlit.toast") as toast, \
                    patch.object(ui, builder, side_effect=error) as build:
                app = self.app(module, function).run()
                self.assert_clean(app)
                toast.assert_not_called()
                self.button(app, label).click().run()
                self.assert_clean(app)
                self.assertTrue(app.error)
                self.assertEqual(toast.call_count, 1)
                self.assertEqual(build.call_count, 1)
                app.run()
                self.assert_clean(app)
                self.assertEqual(toast.call_count, 1)
                self.assertEqual(build.call_count, 1)

    def test_learning_open_failure_notifies_once_without_automatic_retry(self):
        self.materials.append({"material_id": "m", "label": "教材", "is_active": True})
        with patch("streamlit.toast") as toast, \
                patch.object(learning, "load_context_snapshot", side_effect=json_store.JsonStoreError("fixture failure")) as load:
            app = self.app("learning_context_ui", "render_learning_context").run()
            self.assert_clean(app)
            toast.assert_not_called()
            app.selectbox(key="learning_context_material").set_value("m")
            self.button(app, "この教材を開く").click().run()
            self.assert_clean(app)
            self.assertTrue(app.error)
            self.assertEqual(toast.call_count, 1)
            self.assertEqual(load.call_count, 1)
            app.run()
            self.assert_clean(app)
            self.assertEqual(toast.call_count, 1)
            self.assertEqual(load.call_count, 1)

    def test_read_failures_do_not_repeatedly_toast(self):
        cases = (
            ("google_share_ui", "render_google_share", sheets, "load_share_state", sheets.ShareStateError("fixture failure")),
            ("consultation_report_ui", "render_consultation_reports", reports, "load_report_state", reports.ReportError("fixture failure")),
            ("learning_context_ui", "render_learning_context", learning, "_read", json_store.JsonStoreError("fixture failure")),
            ("consultation_proposal_ui", "render_proposal_inbox", proposals, "load_inbox_snapshot", proposals.ProposalError("fixture failure")),
        )
        for module, function, ui, loader, error in cases:
            with self.subTest(module=module), patch("streamlit.toast") as toast, \
                    patch.object(ui, loader, side_effect=error):
                app = self.app(module, function).run()
                self.assert_clean(app)
                self.assertTrue(app.error)
                app.run()
                self.assert_clean(app)
                toast.assert_not_called()


if __name__ == "__main__":
    unittest.main()
