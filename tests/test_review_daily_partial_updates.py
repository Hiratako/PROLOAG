"""Real fragment interactions with isolated synthetic JSON; no server needed."""
from __future__ import annotations

from collections import Counter
from contextlib import ExitStack
from datetime import date, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import data_utils
import prolog_ui.components as ab
from app_core import app_clock, diary_service, exercise_service, health_service, json_store
from app_core import settings_service, substance_service

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
from smoke_timer_partial_updates import FragmentDriver

TODAY = date(2026, 9, 17)
FULL_COUNTER = "_qa_review_daily_full_runs"


class ReminderNormalizationClockTests(unittest.TestCase):
    def test_complete_dates_do_not_eagerly_load_clock(self):
        rows = [{"reminder_id": str(index), "scheduled_date": TODAY.isoformat(),
                 "registered": TODAY.isoformat(), "created_at": "stamp", "updated_at": "stamp"}
                for index in range(100)]
        with patch.object(data_utils, "app_today", side_effect=AssertionError("Unneeded clock read")):
            actual, _ = data_utils.normalize_reminders(rows)
        self.assertEqual(len(actual), 100)

    def test_missing_dates_share_one_clock_and_explicit_today_never_loads_it(self):
        def rows():
            return [{"reminder_id": str(index), "created_at": "stamp", "updated_at": "stamp"}
                    for index in range(100)]
        with patch.object(data_utils, "app_today", return_value=TODAY) as clock:
            actual, _ = data_utils.normalize_reminders(rows())
            clock.assert_called_once_with()
        self.assertTrue(all(row["registered"] == row["scheduled_date"] == TODAY.isoformat() for row in actual))
        with patch.object(data_utils, "app_today", side_effect=AssertionError("Explicit date supplied")):
            actual, _ = data_utils.normalize_reminders(rows(), today=TODAY - timedelta(days=1))
        self.assertTrue(all(row["registered"] == "2026-09-16" for row in actual))


class ReviewDailyPartialUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit import config
        config.get_config_options(options_from_flags={"global.developmentMode": False,
                                                     "browser.gatherUsageStats": False})

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag-review-daily-partial-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        paths = (
            (json_store, "PROJECT_ROOT", self.root),
            (settings_service, "USER_SETTINGS_PATH", self.root / "settings/user_settings.json"),
            (app_clock, "DEMO_MODE_PATH", self.root / "settings/demo_mode.json"),
            (diary_service, "DIARY_ENTRIES_DIR", self.root / "diary_entries"),
            (health_service, "HEALTH_LOGS_DIR", self.root / "health_logs"),
            (exercise_service, "EXERCISE_LOGS_DIR", self.root / "exercise_logs"),
            (substance_service, "SUBSTANCE_PROFILES_PATH", self.root / "settings/substance_profiles.json"),
        )
        for module, name, value in paths:
            self.stack.enter_context(patch.object(module, name, value))
        self.settings = {"display": {"show_task_metrics": False}}
        self.stack.enter_context(patch.object(ab, "sidemenu", return_value=self.settings))
        self.stack.enter_context(patch.object(ab, "inject_css"))
        self.stack.enter_context(patch.object(ab, "topbar"))
        import streamlit as st
        self.stack.enter_context(patch.object(st, "page_link"))
        self.reads = Counter()
        original_read = json_store._read

        def read(path):
            path = Path(path).resolve()
            self.assertTrue(path.is_relative_to(self.root), f"Read escaped synthetic root: {path}")
            self.reads[str(path.relative_to(self.root)).replace("\\", "/")] += 1
            return original_read(path)

        self.stack.enter_context(patch.object(json_store, "_read", side_effect=read))
        self.write("settings/demo_mode.json", {"enabled": True, "fixed_today": TODAY.isoformat()})
        self.write("settings/user_settings.json", self.settings)
        self.write("master_data/master.json", {"projects": [], "subjects": [], "materials": [], "units": [], "tags": []})
        self.write("notes_data/notebooks.json", {"notebooks": [{"id": "b", "name": "架空", "sections": [
            {"id": "s", "name": "架空", "pages": [{"note_id": "n", "title": "先頭ノート", "body": ""}]}]}]})
        self.cards = [{"card_id": f"c{index}", "flashcard_id": f"c{index}", "question": f"架空問題 {index}",
                       "answer": f"架空答え {index}", "source_note_id": "n", "linked_note_ids": ["n"],
                       "created_at": "2026-09-17T09:00:00", "updated_at": "2026-09-17T09:00:00"}
                      for index in range(12)]
        self.write("flashcards.json", self.cards)
        self.reminders = [{"reminder_id": "r", "title": "架空復習", "registered": TODAY.isoformat(),
                           "scheduled_date": TODAY.isoformat(), "status": "pending", "schedule_status": "active",
                           "source_note_id": "n", "created_at": "stamp", "updated_at": "stamp"}]
        self.write("reminders.json", self.reminders)
        for offset in range(6):
            target = TODAY - timedelta(days=offset)
            self.write(self.daily("study_logs", target), [{"session_id": f"s{offset}",
                       "start_at": f"{target}T10:00:00", "duration_sec": 600, "money": 100, "memo": "架空"}])
            self.write(self.daily("daily_plans", target), {"date": target.isoformat(), "target_total_minutes": 60,
                       "tasks": [{"task_id": "t1", "title": "first", "planned_minutes": 30,
                                  "linked_session_ids": ["s0"], "status": "not_started"},
                                 {"task_id": "t2", "title": "second", "planned_minutes": 30,
                                  "linked_session_ids": [], "status": "not_started"}] if offset == 0 else []})

    @staticmethod
    def daily(folder, target=TODAY):
        return f"{folder}/{target:%Y}/{target:%m}/{target:%Y%m%d}.json"

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def read_value(self, relative):
        return json.loads((self.root / relative).read_text(encoding="utf-8"))

    def driver(self, number):
        original = next((PROJECT / "pages").glob(f"{number}_*.py"))
        source = original.read_text(encoding="utf-8-sig")
        marker = "ROOT = Path(__file__).resolve().parents[1]"
        self.assertEqual(source.count(marker), 1)
        source = source.replace(marker, f"ROOT = Path({str(self.root)!r})\nst.session_state[{FULL_COUNTER!r}] = st.session_state.get({FULL_COUNTER!r}, 0) + 1")
        path = self.root / "pages" / original.name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        driver = FragmentDriver(path)
        driver.run()
        return driver

    def partial(self, driver, widget):
        before = driver.app.session_state[FULL_COUNTER]
        self.reads.clear()
        driver.run(widget)
        self.assertEqual(driver.app.session_state[FULL_COUNTER], before)
        self.assertFalse(any(path.startswith("study_logs/") for path in self.reads))

    def test_card_answer_and_next_are_partial_and_refresh_content_without_redrawing_card(self):
        with patch("random.choice", side_effect=lambda values: values[0]) as choose:
            driver = self.driver("07")
            self.assertEqual(self.reads["notes_data/notebooks.json"], 1)
            self.assertEqual(choose.call_count, 1)
            self.cards[0]["answer"] = "別画面で変えた答え"
            self.write("flashcards.json", self.cards)
            answer = driver.app.checkbox(key="p07_show_answer")
            answer.check()
            self.partial(driver, answer)
            self.assertEqual(choose.call_count, 1)
            self.assertTrue(any(item.value == "別画面で変えた答え" for item in driver.app.markdown))
            self.assertEqual(self.reads["notes_data/notebooks.json"], 1)
            next_button = driver.app.button(key="p07_next_card")
            next_button.click()
            self.partial(driver, next_button)
            self.assertEqual(choose.call_count, 2)
            self.assertFalse(driver.app.checkbox(key="p07_show_answer").value)

    def test_review_inputs_are_partial_and_hidden_external_duplicate_blocks_save(self):
        driver = self.driver("08")
        self.assertEqual(self.reads["settings/demo_mode.json"], 1)
        memo = driver.app.text_input(key="p08_review_memo_r")
        memo.input("残す復習メモ")
        self.partial(driver, memo)
        duration = driver.app.number_input(key="p08_review_duration_r")
        duration.set_value(25)
        self.partial(driver, duration)
        self.reminders.append({**self.reminders[0], "status": "done", "schedule_status": "paused"})
        self.write("reminders.json", self.reminders)
        button = next(item for item in driver.app.button if item.label == "普通")
        button.click()
        with patch("app_core.reminder_service.complete_review") as complete:
            self.partial(driver, button)
            complete.assert_not_called()
        self.assertTrue(any("重複" in item.value for item in driver.app.warning))
        draft = driver.app.session_state["p08_review_drafts"]["r"]
        self.assertEqual(draft, {"memo": "残す復習メモ", "duration": 25})

    def test_daily_exercise_and_master_inputs_do_not_scan_history_or_integrity(self):
        driver = self.driver("09")
        exercise = next(item for item in driver.app.checkbox if item.label == "運動の簡易記録を保存する")
        exercise.check()
        self.partial(driver, exercise)
        self.assertEqual(sum(value for path, value in self.reads.items() if path.startswith("daily_plans/")), 2)
        self.assertNotIn("_p09_integrity_result", driver.app.session_state)
        category = driver.app.selectbox(key="daily_plan_master_quick_material_project")
        category.set_value(category.value)
        self.partial(driver, category)

    def test_daily_main_record_save_keeps_task_basic_and_add_form_drafts(self):
        driver = self.driver("09")
        app = driver.app
        next(item for item in app.text_input if str(item.key).startswith("title_") and "_t2_" in item.key).input("残す兄弟タスク")
        next(item for item in app.text_input if item.label == "今日の重点科目（カンマ区切り）").input("未保存科目")
        next(item for item in app.text_input if item.label == "タスク名" and item.key is None).input("未追加タスク")
        next(item for item in app.text_area if item.label == "今日のメモ").input("保存する主要記録")
        button = next(item for item in app.button if item.label == "今日の主要記録を保存")
        button.click()
        before = app.session_state[FULL_COUNTER]
        driver.run(button)
        self.assertGreater(driver.app.session_state[FULL_COUNTER], before)
        self.assertEqual(self.read_value(self.daily("diary_entries"))["body"], "保存する主要記録")
        self.assertEqual(next(item for item in driver.app.text_input if str(item.key).startswith("title_") and "_t2_" in item.key).value, "残す兄弟タスク")
        self.assertEqual(next(item for item in driver.app.text_input if item.label == "今日の重点科目（カンマ区切り）").value, "未保存科目")
        self.assertEqual(next(item for item in driver.app.text_input if item.label == "タスク名" and item.key is None).value, "未追加タスク")

    def test_integrity_is_explicit_and_own_save_discards_the_dated_result(self):
        driver = self.driver("09")
        self.assertEqual(sum(value for path, value in self.reads.items() if path.startswith("daily_plans/")), 2)
        button = driver.app.button(key="p09_check_integrity")
        button.click()
        self.reads.clear()
        driver.run(button)
        self.assertGreater(sum(value for path, value in self.reads.items() if path.startswith("daily_plans/")), 2)
        self.assertIn("_p09_integrity_result", driver.app.session_state)
        self.assertTrue(any("確認日時:" in item.value for item in driver.app.caption))
        button = next(item for item in driver.app.button if item.label == "基本計画を保存")
        button.click()
        driver.run(button)
        self.assertNotIn("_p09_integrity_result", driver.app.session_state)


if __name__ == "__main__":
    unittest.main()
