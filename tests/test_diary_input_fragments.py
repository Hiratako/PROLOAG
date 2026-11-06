"""Real diary/intake input fragments with synthetic records and guarded reads."""
from __future__ import annotations

from collections import Counter
from contextvars import Context
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import config as app_config
from app_core import app_clock, diary_service as diary, exercise_service as exercise, health_service as health
from app_core import json_store, settings_service, substance_service
import prolog_ui.components as ab

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
from smoke_timer_partial_updates import FragmentDriver


class DiaryInputFragmentTests(unittest.TestCase):
    target = date(2026, 10, 7)

    @classmethod
    def setUpClass(cls):
        from streamlit import config
        config.get_config_options(options_from_flags={"global.developmentMode": False, "browser.gatherUsageStats": False})

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag-diary-input-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reads = Counter()
        original_read = json_store._read

        def guarded_read(path):
            path = Path(path).resolve()
            self.assertTrue(path.is_relative_to(self.root), f"Out-of-fixture read: {path}")
            self.reads[path.relative_to(self.root).as_posix()] += 1
            return original_read(path)

        for patcher in (
            patch.object(app_config, "PROJECT_ROOT", self.root),
            patch.object(json_store, "PROJECT_ROOT", self.root),
            patch.object(json_store, "_read", side_effect=guarded_read),
            patch.object(diary, "DIARY_ENTRIES_DIR", self.root / "diary_entries"),
            patch.object(exercise, "EXERCISE_LOGS_DIR", self.root / "exercise_logs"),
            patch.object(health, "HEALTH_LOGS_DIR", self.root / "health_logs"),
            patch.object(settings_service, "USER_SETTINGS_PATH", self.root / "settings/user_settings.json"),
            patch.object(substance_service, "SUBSTANCE_PROFILES_PATH", self.root / "settings/substance_profiles.json"),
            patch.object(app_clock, "DEMO_MODE_PATH", self.root / "settings/demo_mode.json"),
            patch.object(app_clock, "app_today", return_value=self.target),
            patch.object(app_clock, "app_now", return_value=datetime(2026, 10, 7, 12)),
            patch.object(ab, "PROJECT_ROOT", self.root),
            patch("streamlit.page_link", return_value=None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        for offset in range(14):
            target = self.target - timedelta(days=offset)
            path = diary.get_diary_entry_path(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"date": target.isoformat(), "title": "initial", "body": "before",
                "substance_records_schema_version": 1, "substance_records": [{"substance_record_id": f"dose_{offset}",
                "substance_id": "caffeine", "taken_at": f"{target}T09:00:00", "amount_mg": 25,
                "model_snapshot": {"half_life_hours": 5, "peak_delay_minutes": 30, "residual_cutoff_hours": 36, "model_revision": 1}}]}), encoding="utf-8")
        health.save_health_log(self.target, {"sleep": {"sleep_duration_min": 420}})
        exercise.add_exercise_record(self.target, {"type": "walking", "duration_min": 10})
        self.reads.clear()

    def driver(self, number):
        page = next((PROJECT / "pages").glob(f"{number}_*.py"))
        source = page.read_text(encoding="utf-8-sig")
        source = source.replace("ROOT = Path(__file__).resolve().parents[1]", f"ROOT = Path({str(self.root)!r})")
        source = source.replace("st.set_page_config", "st.session_state['_qa_input_full_runs'] = st.session_state.get('_qa_input_full_runs', 0) + 1\nst.set_page_config", 1)
        path = self.root / f"probe_{number}.py"
        path.write_text(source, encoding="utf-8")
        driver = FragmentDriver(path)
        driver.run()
        return driver

    def test_full_page_shares_initial_small_files_once_and_repeat_reads_fresh(self):
        for number in ("06", "18"):
            with self.subTest(page=number):
                self.reads.clear()
                driver = self.driver(number)
                self.assertEqual(self.reads["settings/user_settings.json"], 1)
                self.assertEqual(self.reads["settings/substance_profiles.json"], 1)
                profile = next(item for item in substance_service.default_substance_profiles()
                               if item["substance_id"] == "caffeine")
                profile.update(name="外部更新カフェイン", half_life_hours=11)
                path = self.root / "settings/substance_profiles.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps([profile]), encoding="utf-8")
                self.reads.clear()
                before = driver.app.session_state["_qa_input_full_runs"]
                driver.run()
                self.assertEqual(driver.app.session_state["_qa_input_full_runs"], before + 1)
                self.assertEqual(self.reads["settings/user_settings.json"], 1)
                self.assertEqual(self.reads["settings/substance_profiles.json"], 1)
                if number == "18":
                    selector = driver.app.selectbox(key="substance_record_selected_id")
                    self.assertIn("外部更新カフェイン", selector.options)
                self.assertFalse(any("initial_context" in key for key in driver.app.session_state.filtered_state))

    def run_change(self, driver, widget):
        before = driver.app.session_state["_qa_input_full_runs"]
        self.reads.clear()
        driver.run(widget)
        return driver.app.session_state["_qa_input_full_runs"] - before

    def assert_selected_day_only(self):
        paths = [path for path in self.reads if path.startswith("diary_entries")]
        self.assertEqual(paths, [diary.get_diary_entry_path(self.target).relative_to(self.root).as_posix()])
        self.assertEqual(self.reads[paths[0]], 1)
        self.assertEqual(self.reads["settings/substance_profiles.json"], 1)

    def test_diary_row_growth_is_partial_keeps_draft_and_reuses_daily_summaries(self):
        driver = self.driver("06")
        app = driver.app
        next(w for w in app.text_area if w.label == "本文").set_value("draft body")
        next(w for w in app.text_input if w.label == "項目 1").set_value("draft cost")
        rows = next(w for w in app.number_input if w.label == "自己投資費用の入力行数")
        rows.set_value(rows.value + 1)
        self.assertEqual(self.run_change(driver, rows), 0)
        self.assert_selected_day_only()
        self.assertEqual(self.reads["settings/user_settings.json"], 1)
        self.assertEqual(sum(count for path, count in self.reads.items() if path.startswith("health_logs")), 1)
        self.assertEqual(sum(count for path, count in self.reads.items() if path.startswith("exercise_logs")), 1)
        self.assertEqual(next(w for w in app.text_area if w.label == "本文").value, "draft body")
        self.assertEqual(next(w for w in app.text_input if w.label == "項目 1").value, "draft cost")

    def test_diary_external_change_conflicts_without_losing_fragment_draft(self):
        driver = self.driver("06")
        app = driver.app
        next(w for w in app.text_area if w.label == "本文").set_value("local body")
        Context().run(lambda: diary.save_diary_entry(self.target, {"title": "external"}))
        path = diary.get_diary_entry_path(self.target)
        before = path.read_bytes()
        save = next(w for w in app.button if w.label == "保存する")
        save.click()
        self.assertEqual(self.run_change(driver, save), 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(next(w for w in app.text_area if w.label == "本文").value, "local body")
        self.assertTrue(app.error)

    def test_diary_success_refreshes_read_only_history_in_a_full_run(self):
        driver = self.driver("06")
        app = driver.app
        next(w for w in app.text_input if w.label == "タイトル").set_value("saved local title")
        save = next(w for w in app.button if w.label == "保存する")
        save.click()
        self.assertEqual(self.run_change(driver, save), 1)
        self.assertIn("saved local title", app.dataframe[0].value["タイトル"].tolist())

    def test_intake_row_growth_is_partial_and_keeps_both_form_drafts(self):
        driver = self.driver("18")
        app = driver.app
        next(w for w in app.text_input if w.label == "メモ（任意）").set_value("quick draft")
        next(w for w in app.text_input if w.label == "メモ 1").set_value("history draft")
        rows = next(w for w in app.number_input if w.label == "履歴の入力行数")
        rows.set_value(rows.value + 1)
        self.assertEqual(self.run_change(driver, rows), 0)
        self.assert_selected_day_only()
        self.assertEqual(next(w for w in app.text_input if w.label == "メモ（任意）").value, "quick draft")
        self.assertEqual(next(w for w in app.text_input if w.label == "メモ 1").value, "history draft")

    def test_intake_external_change_keeps_original_json_and_editor_draft(self):
        driver = self.driver("18")
        app = driver.app
        next(w for w in app.text_input if w.label == "メモ 1").set_value("local memo")
        Context().run(lambda: diary.save_diary_entry(self.target, {"title": "external"}))
        path = diary.get_diary_entry_path(self.target)
        before = path.read_bytes()
        save = next(w for w in app.button if w.label == "履歴を保存")
        save.click()
        self.assertEqual(self.run_change(driver, save), 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(next(w for w in app.text_input if w.label == "メモ 1").value, "local memo")
        self.assertTrue(app.error)

    def test_intake_next_partial_reads_changed_profiles_and_save_refreshes_history(self):
        driver = self.driver("18")
        app = driver.app
        profile = next(item for item in substance_service.default_substance_profiles() if item["substance_id"] == "caffeine")
        profile["half_life_hours"] = 10
        path = substance_service.SUBSTANCE_PROFILES_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"profiles": [profile]}), encoding="utf-8")
        rows = next(w for w in app.number_input if w.label == "履歴の入力行数")
        rows.set_value(rows.value + 1)
        self.assertEqual(self.run_change(driver, rows), 0)
        self.assert_selected_day_only()
        next(w for w in app.number_input if w.label == "摂取量 2（mg）").set_value(40)
        save = next(w for w in app.button if w.label == "履歴を保存")
        save.click()
        self.assertEqual(self.run_change(driver, save), 1)
        saved = diary.load_diary_entry(self.target)
        models = {record["amount_mg"]: record["model_snapshot"]["half_life_hours"] for record in saved["substance_records"]}
        self.assertEqual(models, {25.0: 5.0, 40.0: 10.0})
        self.assertEqual(app.dataframe[0].value.iloc[0]["日次合計mg"], 65)


if __name__ == "__main__":
    unittest.main()
