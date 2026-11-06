"""Guide/export/inspection partial interactions with synthetic source reads."""
from __future__ import annotations

from collections import Counter
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import prolog_ui.components as ab
from app_core import export_ui, integrity_service, json_store, save_recovery_ui, settings_service

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
from smoke_timer_partial_updates import FragmentDriver

FULL_COUNTER = "_qa_auxiliary_full_runs"


class AuxiliaryInputFragmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit import config
        config.get_config_options(options_from_flags={"global.developmentMode": False,
                                                     "browser.gatherUsageStats": False})

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag-auxiliary-fragments-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.reads = Counter()
        original_read = json_store._read

        def read(path):
            path = Path(path).resolve()
            self.assertTrue(path.is_relative_to(self.root), f"Read escaped synthetic root: {path}")
            self.reads[str(path.relative_to(self.root)).replace("\\", "/")] += 1
            return original_read(path)

        self.stack.enter_context(patch.object(json_store, "_read", side_effect=read))
        self.stack.enter_context(patch.object(json_store, "PROJECT_ROOT", self.root))
        self.stack.enter_context(patch.object(settings_service, "USER_SETTINGS_PATH", self.root / "settings/user_settings.json"))
        self.write("settings/user_settings.json", {"display": {"theme_mode": "standard", "show_task_metrics": False}})
        self.write("backups/fixture_journal.json", {"status": "committed"})
        self.recovery = self.stack.enter_context(patch.object(
            save_recovery_ui, "render_save_recovery",
            side_effect=lambda _root: json_store.load_json_safe(self.root / "backups/fixture_journal.json", {})))
        import streamlit as st
        self.stack.enter_context(patch.object(st, "page_link"))
        self.downloads = self.stack.enter_context(patch.object(st, "download_button", wraps=st.download_button))
        # Exercise the actual CSV builder against this isolated root. The page's
        # no-argument entry point otherwise binds the importing source's root.
        original_export_ui = export_ui.render_csv_export
        self.stack.enter_context(patch.object(export_ui, "render_csv_export",
                                              side_effect=lambda: original_export_ui(self.root)))
        self.builder = self.stack.enter_context(patch.object(export_ui, "build_export_bundle",
                                                             wraps=export_ui.build_export_bundle))
        self.write("study_logs/2026/09/20260917.json", [{"session_id": "s1", "start_at": "2026-09-17T10:00:00",
                   "end_at": "2026-09-17T10:10:00", "duration_sec": 600, "money": 100, "memo": "架空"}])
        self.write("notes_data/notebooks.json", {"notebooks": [{"id": "book", "name": "架空", "sections": [
            {"id": "section", "name": "架空", "pages": [{"note_id": "n1", "title": "架空ノート", "body": "本文"}]}]}]})
        self.result = {
            "checked_at": "2026-09-17 12:00:00", "total_checks": 10, "checked_file_count": 2,
            "issue_count": 2, "error_count": 1, "notice_count": 1, "check_scope": ["架空の検査範囲"],
            "issues": [
                {"重要度": "エラー", "問題カテゴリ": "架空ID", "対象ファイル": "fixture.json",
                 "対象ID": "a", "内容": "架空のエラー", "推奨対応": "架空確認"},
                {"重要度": "注意", "問題カテゴリ": "架空参照", "対象ファイル": "fixture.json",
                 "対象ID": "b", "内容": "架空の注意", "推奨対応": "架空確認"},
            ],
        }
        self.write("fixture_integrity_result.json", self.result)
        # UI-only boundary: the inspection backend's existing safety tests cover
        # rules. Here a strict synthetic read proves explicit fresh invocation.
        self.inspect = self.stack.enter_context(patch.object(
            integrity_service, "run_integrity_checks",
            side_effect=lambda: json_store.load_json_safe(self.root / "fixture_integrity_result.json", {})))

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def driver(self, number):
        original = next((PROJECT / "pages").glob(f"{number}_*.py"))
        source = original.read_text(encoding="utf-8-sig")
        marker = "PAGE_SETTINGS = _load_page_settings()"
        self.assertEqual(source.count(marker), 1)
        source = source.replace(marker, f"st.session_state[{FULL_COUNTER!r}] = st.session_state.get({FULL_COUNTER!r}, 0) + 1\n{marker}")
        path = self.root / "pages" / original.name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        driver = FragmentDriver(path)
        driver.run()
        # CSV also exports the settings dataset, so its initial snapshot has
        # one independent source read in addition to the shared page settings.
        self.assertEqual(self.reads["settings/user_settings.json"], 2 if number == "97" else 1)
        self.assertEqual(self.recovery.call_count, 1)
        return driver

    def partial(self, driver, widget):
        before = driver.app.session_state[FULL_COUNTER]
        recovery_before, builds_before, inspect_before = self.recovery.call_count, self.builder.call_count, self.inspect.call_count
        self.reads.clear()
        driver.run(widget)
        self.assertEqual(driver.app.session_state[FULL_COUNTER], before)
        self.assertEqual(self.reads, {})
        self.assertEqual(self.recovery.call_count, recovery_before)
        self.assertEqual(self.builder.call_count, builds_before)
        self.assertEqual(self.inspect.call_count, inspect_before)

    def test_user_guide_topic_only_reruns_guide_content(self):
        driver = self.driver("95")
        widget = driver.app.radio(key="user_guide_topic")
        widget.set_value(widget.options[-1])
        self.partial(driver, widget)
        self.assertEqual(driver.app.radio(key="user_guide_topic").value, widget.options[-1])

    def test_google_guide_topic_and_environment_do_not_reload_sidebar(self):
        driver = self.driver("96")
        widget = driver.app.radio(key="google_guide_topic")
        widget.set_value(widget.options[-1])
        self.partial(driver, widget)
        # The environment selector belongs to the initialization topic.
        topic = driver.app.radio(key="google_guide_topic")
        topic.set_value("初回設定")
        self.partial(driver, topic)
        environment = driver.app.selectbox(key="google_guide_environment")
        environment.set_value(environment.options[-1])
        self.partial(driver, environment)

    def test_csv_dataset_is_partial_and_refresh_reads_new_synthetic_records(self):
        driver = self.driver("97")
        self.assertEqual(self.builder.call_count, 1)
        first_bundle = driver.app.session_state[export_ui.SNAPSHOT_KEY]["bundle"]
        first_study = next(table for table in first_bundle.tables if table.key == "study_sessions")
        self.assertEqual(first_study.row_count, 1)
        selected = driver.app.selectbox(key="export_dataset")
        selected.set_value(next(option for option in selected.options if option.startswith("ノート")))
        self.partial(driver, selected)
        self.assertTrue(all(item.proto.ignore_rerun for item in driver.app.get("download_button")))
        self.write("study_logs/2026/09/20260917.json", [
            {"session_id": "s1", "start_at": "2026-09-17T10:00:00", "duration_sec": 600, "money": 100},
            {"session_id": "s2", "start_at": "2026-09-17T11:00:00", "duration_sec": 1200, "money": 200},
        ])
        refresh = driver.app.button(key="csv_export_refresh")
        refresh.click()
        before = driver.app.session_state[FULL_COUNTER]
        self.reads.clear()
        driver.run(refresh)
        self.assertEqual(driver.app.session_state[FULL_COUNTER], before)
        self.assertEqual(self.builder.call_count, 2)
        self.assertGreater(self.reads["study_logs/2026/09/20260917.json"], 0)
        new_bundle = driver.app.session_state[export_ui.SNAPSHOT_KEY]["bundle"]
        study = next(table for table in new_bundle.tables if table.key == "study_sessions")
        self.assertEqual(study.row_count, 2)
        self.assertIn(b"s2", study.data)
        selected_table = next(table for table in new_bundle.tables
                              if driver.app.selectbox(key="export_dataset").value == f"{table.label}（{table.row_count:,}行）")
        self.assertEqual(self.downloads.call_args.kwargs["data"], selected_table.data)
        self.assertEqual(self.downloads.call_args.kwargs["file_name"], selected_table.filename)
        self.assertTrue(all(item.proto.ignore_rerun for item in driver.app.get("download_button")))

    def test_inspection_filters_are_partial_and_explicit_check_observes_new_result(self):
        driver = self.driver("99")
        self.inspect.assert_not_called()
        check = next(item for item in driver.app.button if item.label == "チェックを実行する")
        check.click()
        driver.run(check)
        self.assertEqual(self.inspect.call_count, 1)
        severity = driver.app.multiselect(key="integrity_severity_filter")
        severity.set_value(["注意"])
        self.partial(driver, severity)
        issue_table = next(item.value for item in driver.app.dataframe if "内容" in item.value.columns)
        self.assertEqual(list(issue_table["重要度"]), ["注意"])
        category = driver.app.selectbox(key="integrity_category_filter")
        category.set_value("架空ID")
        self.partial(driver, category)
        self.assertTrue(all(item.proto.ignore_rerun for item in driver.app.get("download_button")))
        self.result["checked_at"] = "2026-09-17 13:00:00"
        self.result["issues"][0]["内容"] = "更新された架空のエラー"
        self.write("fixture_integrity_result.json", self.result)
        check = next(item for item in driver.app.button if item.label == "チェックを実行する")
        check.click()
        self.reads.clear()
        driver.run(check)
        self.assertEqual(self.inspect.call_count, 2)
        self.assertEqual(self.reads["fixture_integrity_result.json"], 1)
        self.assertEqual(driver.app.session_state["integrity_result"]["checked_at"], "2026-09-17 13:00:00")
        csv_data = self.downloads.call_args.kwargs["data"].decode("utf-8-sig")
        self.assertIn("更新された架空のエラー", csv_data)
        self.assertTrue(self.downloads.call_args.kwargs["on_click"] == "ignore")


if __name__ == "__main__":
    unittest.main()
