from __future__ import annotations

from copy import deepcopy
import csv
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from app_core import legacy_study_import_ui as ui


class LegacyStudyImportUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit import config
        config.get_config_options(options_from_flags={"global.developmentMode": False,
                                                     "browser.gatherUsageStats": False})

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="proloag_legacy_ui_")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.session_bytes = b"synthetic-session-file"
        self.summary_bytes = b"synthetic-summary-file"
        rows = []
        for number in (2, 3):
            rows.append({"row_number": number, "fingerprint": str(number) * 64,
                         "legacy_topic": "medical", "raw": {"memo": f"synthetic-{number}"},
                         "session": {"start_at": f"2025-01-01T0{number}:00:00", "end_at": f"2025-01-01T0{number}:10:00",
                                     "duration_sec": 600.5, "hourly_rate": 1000, "money": 166.805,
                                     "memo": f"synthetic-{number}", "condition": {}},
                         "issues": []})
        self.diagnosis = {"schema_version": 1, "source_sha256": "a" * 64, "summary_sha256": "b" * 64,
                          "rows": rows, "issues": [], "daily_reconciliation": [],
                          "summary": {"row_count": 2, "date_count": 1, "first_date": "2025-01-01", "last_date": "2025-01-01",
                                      "duration_sec": 1201, "money": 333.61, "error_count": 0, "warning_count": 0}}
        self.master = {key: [] for key in ("projects", "subjects", "materials", "units", "tags")}
        patches = [
            patch.object(ui.st, "file_uploader", side_effect=self.upload),
            patch.object(ui, "diagnose_legacy_csv", side_effect=lambda *_: deepcopy(self.diagnosis)),
            patch.object(ui, "load_import_classifications", return_value=self.master),
            patch.object(ui, "build_import_preview", side_effect=self.build_preview),
            patch.object(ui, "commit_import", side_effect=lambda preview, root: {
                "added_count": preview["summary"]["ready_count"], "operation_id": preview["operation_id"], "replayed": False}),
        ]
        self.upload_mock, self.diagnose, self.master_mock, self.build, self.commit = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)

    def upload(self, label, **kwargs):
        value = self.session_bytes if "study_sessions" in label else self.summary_bytes
        return io.BytesIO(value) if value is not None else None

    def build_preview(self, diagnosis, root, **kwargs):
        rows = deepcopy(diagnosis["rows"])
        for row in rows:
            row["status"] = "excluded" if row["row_number"] not in kwargs["selected_rows"] else "ready"
            row["reason"] = ""
        return {"operation_id": uuid4().hex, "rows": rows,
                "summary": {"ready_count": sum(row["status"] == "ready" for row in rows),
                            "review_count": 0, "existing_count": 0, "error_count": 0,
                            "excluded_count": sum(row["status"] == "excluded" for row in rows),
                            "duration_sec": 1201, "money": 333.61}}

    def app(self):
        from streamlit.testing.v1 import AppTest
        return AppTest.from_string(
            "from pathlib import Path\nimport streamlit as st\n"
            "from app_core.legacy_study_import_ui import render_legacy_study_import\n"
            f"render_legacy_study_import(Path(st.session_state.get('fixture_root', {str(self.root)!r})))\n",
            default_timeout=30,
        ).run()

    def diagnosed(self):
        app = self.app()
        app.button(key="legacy_diagnose").click().run()
        self.assertFalse(app.exception)
        return app

    def previewed(self):
        app = self.diagnosed()
        app.button(key="legacy_build_preview").click().run()
        self.assertFalse(app.exception)
        return app

    def test_file_selection_does_not_diagnose_read_records_or_save(self):
        app = self.app()
        self.assertFalse(app.exception)
        self.diagnose.assert_not_called()
        self.master_mock.assert_not_called()
        self.build.assert_not_called()
        self.commit.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_diagnosis_and_repeated_reruns_do_not_build_snapshot_or_save(self):
        app = self.diagnosed()
        self.diagnose.assert_called_once_with(self.session_bytes, self.summary_bytes)
        self.build.assert_not_called()
        self.commit.assert_not_called()
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(self.diagnose.call_count, 1)
        self.build.assert_not_called()
        self.assertTrue(all(item.proto.ignore_rerun for item in app.get("download_button")))

    def test_import_requires_preview_then_new_explicit_confirmation(self):
        app = self.previewed()
        self.assertTrue(app.button(key="legacy_commit").disabled)
        self.commit.assert_not_called()
        app.checkbox[0].check().run()
        self.assertFalse(app.button(key="legacy_commit").disabled)
        app.button(key="legacy_commit").click().run()
        self.assertFalse(app.exception)
        self.commit.assert_called_once()
        self.assertTrue(app.button(key="legacy_commit").disabled)
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(self.commit.call_count, 1)

    def test_selection_change_disables_old_preview_and_never_auto_rebuilds(self):
        app = self.previewed()
        app.checkbox[0].check().run()
        app.multiselect[0].set_value([2]).run()
        self.assertFalse(app.exception)
        self.assertTrue(app.button(key="legacy_commit").disabled)
        self.assertTrue(any("作り直して" in item.value for item in app.warning))
        self.assertEqual(self.build.call_count, 1)
        app.button(key="legacy_build_preview").click().run()
        self.assertFalse(app.exception)
        self.assertFalse(app.checkbox[0].value)
        self.assertTrue(app.button(key="legacy_commit").disabled)
        self.assertEqual(self.build.call_args.kwargs["selected_rows"], [3])

    def test_rebuilding_same_conditions_also_requires_fresh_confirmation(self):
        app = self.previewed()
        app.checkbox[0].check().run()
        app.button(key="legacy_build_preview").click().run()
        self.assertFalse(app.exception)
        self.assertFalse(app.checkbox[0].value)
        self.assertTrue(app.button(key="legacy_commit").disabled)

    def test_changed_csv_invalidates_diagnosis_and_previous_preview(self):
        app = self.previewed()
        self.session_bytes = b"different-synthetic-file"
        app.run()
        self.assertFalse(app.exception)
        self.assertIsNone(app.session_state[ui.STATE_KEY]["diagnosis"])
        self.assertIsNone(app.session_state[ui.STATE_KEY]["preview"])
        self.assertEqual(len(app.checkbox), 0)
        self.commit.assert_not_called()

    def test_new_existing_review_rows_are_immediately_selectable_without_rebuilding(self):
        def review_preview(*args, **kwargs):
            preview = self.build_preview(*args, **kwargs)
            for row in preview["rows"]:
                row.update(status="review", reason="既存の時間帯と重なります。")
            preview["summary"].update(ready_count=0, review_count=2)
            return preview
        self.build.side_effect = review_preview
        app = self.previewed()
        self.assertEqual(self.build.call_count, 1)
        self.assertEqual(list(app.multiselect[1].options), [ui._row_label(row) for row in self.diagnosis["rows"]])
        self.assertTrue(app.button(key="legacy_commit").disabled)

    def test_other_root_never_reuses_diagnosis_or_preview(self):
        app = self.previewed()
        app.session_state["fixture_root"] = str(self.root / "other")
        app.run()
        self.assertFalse(app.exception)
        self.assertIsNone(app.session_state[ui.STATE_KEY]["preview"])
        self.assertIsNone(app.session_state[ui.STATE_KEY]["diagnosis"])

    def test_fatal_structure_error_does_not_load_classifications(self):
        self.diagnosis.update(rows=[], issues=[{"code": "bad_header", "message": "不正なヘッダー", "severity": "error"}])
        app = self.diagnosed()
        self.master_mock.assert_not_called()
        self.build.assert_not_called()
        self.commit.assert_not_called()

    def test_daily_reconciliation_is_readable_and_does_not_trigger_import(self):
        self.diagnosis["daily_reconciliation"] = [{"date": "2025-01-01", "count": 2, "duration_sec": 1201.0,
            "money": 333.61, "summary_count": 2, "summary_duration_sec": 1201.0, "summary_money": 333.6,
            "count_delta": 0, "duration_delta_sec": 0.0, "money_delta": -0.01, "status": "ok"}]
        app = self.diagnosed()
        self.assertIn("日付", app.dataframe[0].value.columns)
        self.assertEqual(app.dataframe[0].value["判定"].iloc[0], "一致")
        self.build.assert_not_called()
        self.commit.assert_not_called()

    def test_save_failure_keeps_preview_and_user_input_without_automatic_retry(self):
        app = self.previewed()
        operation = app.session_state[ui.STATE_KEY]["preview"]["operation_id"]
        self.commit.side_effect = ValueError("synthetic conflict")
        app.checkbox[0].check().run()
        app.button(key="legacy_commit").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.error)
        self.assertEqual(app.session_state[ui.STATE_KEY]["preview"]["operation_id"], operation)
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(self.commit.call_count, 1)
        self.assertIn("synthetic conflict", app.session_state[ui.STATE_KEY]["error"])

    def test_csv_downloads_preserve_numeric_precision_and_escape_formula_text(self):
        data = ui.csv_bytes([{"memo": " =HYPERLINK(1)", "duration_sec": 600.528529882431,
                              "money": 166.5074439839602, "raw": {"keep": "original"}}])
        row = next(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
        self.assertEqual(row["memo"], "' =HYPERLINK(1)")
        self.assertEqual(row["duration_sec"], "600.528529882431")
        self.assertEqual(row["money"], "166.5074439839602")
        self.assertIn("original", row["raw"])


if __name__ == "__main__":
    unittest.main()
