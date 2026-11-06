from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core import export_ui, json_store
from app_core.export_service import CsvExport, ExportBundle


class ExportFakeST:
    def __init__(self):
        self.session_state = {}
        self.requested = False
        self.errors = []
        self.warnings = []

    def info(self, message):
        pass

    def caption(self, message):
        pass

    def button(self, *args, **kwargs):
        return self.requested

    def spinner(self, *args, **kwargs):
        return nullcontext()

    def error(self, message):
        self.errors.append(message)

    def warning(self, message):
        self.warnings.append(message)

    def columns(self, count):
        return [nullcontext() for _ in range(count)]

    def download_button(self, *args, **kwargs):
        pass

    def subheader(self, *args, **kwargs):
        pass

    def dataframe(self, *args, **kwargs):
        pass

    def selectbox(self, label, options, **kwargs):
        return options[0]

    def expander(self, *args, **kwargs):
        return nullcontext()

    def markdown(self, *args, **kwargs):
        pass


class ExportActionNotificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag_export_notifications_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.ui = ExportFakeST()
        self.bundle = ExportBundle(
            generated_at=datetime(2026, 10, 3, 20, 0),
            tables=(CsvExport("notes", "ノート", "架空の本文", "notes.csv", 1, 1,
                              ("body",), b"body\nfixture\n"),),
            manifest=b"fixture-manifest", zip_data=b"fixture-zip", warnings=(),
        )
        for target, attribute, kwargs in (
            (export_ui, "st", {"new": self.ui}),
            (export_ui, "build_export_bundle", {"return_value": self.bundle}),
            (export_ui.ab, "metric_block", {}),
            (export_ui.ab, "show_action_message", {}),
            (export_ui.ab, "show_action_toast", {}),
            (json_store, "_read", {"side_effect": AssertionError("No live record reads")}),
        ):
            patcher = patch.object(target, attribute, **kwargs)
            value = patcher.start()
            self.addCleanup(patcher.stop)
            if attribute == "build_export_bundle":
                self.build = value
            elif attribute == "show_action_message":
                self.result = value
            elif attribute == "show_action_toast":
                self.toast = value

    def render(self, *, requested=False):
        self.ui.requested = requested
        self.ui.errors.clear()
        self.ui.warnings.clear()
        export_ui.render_csv_export(self.root)

    def test_only_explicit_success_notifies_and_unrelated_reruns_are_silent(self):
        self.render()
        self.render()
        self.build.assert_called_once_with(self.root)
        self.result.assert_not_called()
        self.toast.assert_not_called()
        self.render(requested=True)
        self.assertEqual(self.build.call_count, 2)
        self.result.assert_called_once_with("CSV・ZIPを生成・更新しました。")
        self.toast.assert_not_called()
        self.render()
        self.assertEqual(self.result.call_count, 1)
        self.assertEqual(self.build.call_count, 2)
        self.render(requested=True)
        self.assertEqual(self.result.call_count, 2, "Separate refreshes may repeat the same success text")

    def test_initial_failure_and_persistent_error_renders_are_silent(self):
        self.build.side_effect = ValueError("fixture generation failure")
        self.render()
        self.assertTrue(self.ui.errors)
        self.assertIn("fixture generation failure", self.ui.errors[0])
        self.result.assert_not_called()
        self.toast.assert_not_called()
        self.render()
        self.assertEqual(self.build.call_count, 1)
        self.assertTrue(self.ui.errors)
        self.toast.assert_not_called()
        self.render(requested=True)
        self.assertEqual(self.build.call_count, 2)
        self.toast.assert_called_once_with(
            "CSV・ZIPを生成できませんでした。詳細表示を確認してください。", kind="error",
        )
        self.result.assert_not_called()
        self.render()
        self.assertEqual(self.toast.call_count, 1)
        self.assertEqual(self.build.call_count, 2)
        self.assertTrue(self.ui.errors)

    def test_partial_output_warns_only_on_explicit_refresh_and_keeps_download_snapshot(self):
        self.build.return_value = replace(self.bundle, warnings=("fixture unreadable record",))
        self.render()
        self.result.assert_not_called()
        self.assertTrue(self.ui.warnings)
        self.render(requested=True)
        self.result.assert_called_once_with(
            "CSV・ZIPを生成しましたが、一部の記録を読み込めませんでした。", kind="warning",
        )
        self.toast.assert_not_called()
        snapshot = self.ui.session_state[export_ui.SNAPSHOT_KEY]
        self.assertIs(snapshot["bundle"], self.build.return_value)
        self.assertIsNone(snapshot["error"])
        self.assertTrue(self.ui.warnings)
        self.render()
        self.assertEqual(self.result.call_count, 1)
        self.assertTrue(self.ui.warnings)
        self.assertIs(snapshot["bundle"], self.build.return_value)

    def test_failed_refresh_keeps_old_snapshot_and_notifies_failure_once(self):
        self.render()
        self.build.side_effect = OSError("fixture refresh failure")
        self.render(requested=True)
        snapshot = self.ui.session_state[export_ui.SNAPSHOT_KEY]
        self.assertIs(snapshot["bundle"], self.bundle)
        self.assertEqual(snapshot["error"], "fixture refresh failure")
        self.assertIn("前回生成した出力", self.ui.warnings[0])
        self.toast.assert_called_once()
        self.result.assert_not_called()
        self.render()
        self.assertIs(snapshot["bundle"], self.bundle)
        self.assertTrue(self.ui.errors)
        self.assertIn("前回生成した出力", self.ui.warnings[0])
        self.assertEqual(self.toast.call_count, 1)
        self.build.side_effect = None
        self.render(requested=True)
        self.assertIs(snapshot["bundle"], self.bundle)
        self.assertIsNone(snapshot["error"])
        self.assertEqual(self.ui.errors, [])
        self.assertEqual(self.ui.warnings, [])
        self.result.assert_called_once_with("CSV・ZIPを生成・更新しました。")
        self.assertEqual(self.toast.call_count, 1)


if __name__ == "__main__":
    unittest.main()
