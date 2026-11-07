from __future__ import annotations

import ast
from contextvars import Context
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from app_core import daily_save_service as daily
from app_core import diary_service as diary
from app_core import exercise_service as exercise
from app_core import health_service as health
from app_core import json_store as store
from app_core import substance_service as substance
from app_core.form_snapshot import discard_form_snapshot, form_snapshot


PROJECT = Path(__file__).resolve().parents[1]


class DailyFormSafetyTests(unittest.TestCase):
    target = date(2026, 9, 10)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag-daily-safety-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for patcher in (
            patch.object(store, "PROJECT_ROOT", self.root),
            patch.object(diary, "DIARY_ENTRIES_DIR", self.root / "diary_entries"),
            patch.object(health, "HEALTH_LOGS_DIR", self.root / "health_logs"),
            patch.object(exercise, "EXERCISE_LOGS_DIR", self.root / "exercise_logs"),
            patch.object(substance, "SUBSTANCE_PROFILES_PATH", self.root / "settings" / "substance_profiles.json"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def revisions(self):
        diary.load_diary_entry(self.target)
        health.load_health_log(self.target)
        exercise.load_exercise_log(self.target)
        paths = [diary.get_diary_entry_path(self.target), health.get_health_log_path(self.target), exercise.get_exercise_log_path(self.target)]
        return {path: store.observed_json_revision(path) for path in paths}

    def live_bytes(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for folder in ("diary_entries", "health_logs", "exercise_logs")
                for p in (self.root / folder).rglob("*.json")}

    def seed_daily(self):
        diary.save_diary_entry(self.target, {"title": "before"})
        health.save_health_log(self.target, {"condition": {"memo": "before"}})
        exercise.add_exercise_record(self.target, {"type": "walking", "duration_min": 10})

    def save_daily(self, revisions, operation_id=None):
        daily.save_daily_records(
            self.target, {"title": "after"}, root=self.root, operation_id=operation_id or uuid4().hex,
            expected_revisions=revisions, health_data={"condition": {"memo": "after"}},
            exercise_mode="hub", exercise_data={"type": "running", "duration_min": 20},
        )

    def test_form_baseline_survives_reload_and_missing_file_revision(self):
        state = {}
        initial = diary.load_diary_entry(self.target)
        path = diary.get_diary_entry_path(self.target)
        baseline = form_snapshot(state, "test", [path], initial)
        self.assertIsNone(baseline["revisions"][path])
        Context().run(lambda: diary.save_diary_entry(self.target, {"title": "remote"}))
        latest = diary.load_diary_entry(self.target)
        again = form_snapshot(state, "test", [path], latest)
        self.assertEqual(again, baseline)
        discard_form_snapshot(state, "test")
        refreshed = form_snapshot(state, "test", [path], latest)
        self.assertEqual(refreshed["data"]["title"], "remote")
        self.assertNotEqual(refreshed["operation_id"], baseline["operation_id"])
        self.assertEqual(refreshed["version"], 1)

    def test_substance_replace_rejects_added_and_deleted_records(self):
        for change in ("add", "delete"):
            with self.subTest(change=change):
                substance.add_substance_record(self.target, "acetaminophen", 1, taken_at=datetime(2026, 9, 10, 12))
                entry = diary.load_diary_entry(self.target)
                rows = deepcopy(substance.records_for_substance(entry, "acetaminophen"))
                ids = {r["substance_record_id"] for r in rows}
                revision = store.observed_json_revision(diary.get_diary_entry_path(self.target))
                if change == "add":
                    Context().run(lambda: substance.add_substance_record(self.target, "acetaminophen", 2, taken_at=datetime(2026, 9, 10, 15)))
                else:
                    Context().run(lambda: substance.replace_substance_records_for_day(self.target, "acetaminophen", []))
                before = self.live_bytes()
                for _ in range(2):
                    with self.assertRaises(store.JsonConflictError):
                        substance.replace_substance_records_for_day(self.target, "acetaminophen", rows,
                            expected_revision=revision, expected_record_ids=ids)
                    self.assertEqual(self.live_bytes(), before)

    def test_profile_upsert_rejects_stale_full_profile_even_after_reload(self):
        substance.save_substance_profiles(substance.load_substance_profiles())
        stale = deepcopy(substance.substance_profile_map()["caffeine"])
        revision = store.observed_json_revision(substance.SUBSTANCE_PROFILES_PATH)
        Context().run(lambda: substance.upsert_substance_profile({"substance_id": "caffeine", "half_life_hours": 7.0}))
        substance.load_substance_profiles()
        stale["default_label"] = "local label"
        with self.assertRaises(store.JsonConflictError):
            substance.upsert_substance_profile(stale, expected_revision=revision)
        self.assertEqual(substance.substance_profile_map()["caffeine"]["half_life_hours"], 7.0)

    def test_daily_body_failure_writes_no_live_data(self):
        self.seed_daily()
        revisions, before = self.revisions(), self.live_bytes()
        with patch.object(daily, "save_health_log", side_effect=OSError("synthetic body failure")):
            with self.assertRaises(OSError):
                self.save_daily(revisions)
        self.assertEqual(self.live_bytes(), before)

    def test_daily_commit_failure_restores_every_live_file(self):
        self.seed_daily()
        revisions, before = self.revisions(), self.live_bytes()
        original_replace = store.os.replace
        failed = False
        def fail_health_once(source, destination):
            nonlocal failed
            if Path(destination) == health.get_health_log_path(self.target) and not failed:
                failed = True
                raise OSError("synthetic commit failure")
            return original_replace(source, destination)
        with patch.object(store.os, "replace", side_effect=fail_health_once):
            with self.assertRaises(store.JsonStoreError):
                self.save_daily(revisions)
        self.assertTrue(failed)
        self.assertEqual(self.live_bytes(), before)

    def test_daily_conflict_changes_nothing_and_committed_retry_is_noop(self):
        self.seed_daily()
        revisions = self.revisions()
        Context().run(lambda: health.save_health_log(self.target, {"condition": {"memo": "remote"}}))
        before = self.live_bytes()
        with self.assertRaises(store.JsonConflictError):
            self.save_daily(revisions)
        self.assertEqual(self.live_bytes(), before)
        revisions, operation_id = self.revisions(), uuid4().hex
        self.save_daily(revisions, operation_id)
        after = self.live_bytes()
        self.save_daily(revisions, operation_id)
        self.assertEqual(self.live_bytes(), after)


class DailyFormAppTests(DailyFormSafetyTests):
    """Run real page form ASTs without loading any production data or server."""

    @classmethod
    def setUpClass(cls):
        from streamlit import config
        config.get_config_options(options_from_flags={"global.developmentMode": False, "browser.gatherUsageStats": False})

    def app(self, source):
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_string(source, default_timeout=15).run()
        self.assertEqual(len(app.exception), 0, str(app.exception))
        return app

    def header(self):
        return f'''\nimport streamlit as st
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from types import SimpleNamespace
from app_core.form_snapshot import form_snapshot, discard_form_snapshot
from app_core.daily_save_service import save_daily_records
from app_core.diary_service import get_diary_entry_path, investment_total, load_diary_entry, save_diary_entry, upsert_daily_hub_cost
from app_core.exercise_service import calculate_running_pace, get_exercise_log_path, load_exercise_log
from app_core.health_service import calculate_sleep_duration_minutes, get_health_log_path, load_health_log
from app_core.settings_service import DEFAULT_USER_SETTINGS
ab=SimpleNamespace(metric_block=lambda *a, **k:None, queue_success=lambda *a, **k:None,
                   show_action_error=lambda message, **k:st.error(message))
st.page_link=lambda *a, **k:None
ROOT=Path({str(self.root)!r})
target=date(2026,9,10)
diary_settings={{}}
load_diary_record=load_diary_entry
'''

    def diary_app_source(self):
        tree = ast.parse((PROJECT / "pages" / "06_日記.py").read_text(encoding="utf-8-sig"))
        wanted = {"diary_path", "clean_costs", "yen", "minutes_label", "caffeine_mg", "load_diary_record",
                  "health_summary", "exercise_summary", "render_diary_editor"}
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
        extra = '''
from app_core.substance_service import format_mg, substance_profile_map
load_user_settings=lambda: DEFAULT_USER_SETTINGS
ab.card=lambda *a, **k:None
'''
        return self.header() + extra + "\n" + "\n".join(ast.unparse(n) for n in nodes) + "\nrender_diary_editor(target)"

    def hub_app_source(self, settings=None):
        tree = ast.parse((PROJECT / "pages" / "09_今日の学習プラン.py").read_text(encoding="utf-8-sig"))
        wanted = {"render_daily_quick_log", "find_daily_hub_exercise_record", "yen", "minutes_label", "parse_time_value", "safe_index"}
        functions = "\n".join(ast.unparse(n) for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted)
        settings_source = "DEFAULT_USER_SETTINGS" if settings is None else repr(settings)
        return self.header() + functions + f"\nrender_daily_quick_log(target, settings={settings_source})"

    def substance_app_source(self):
        tree = ast.parse((PROJECT / "pages" / "18_カフェイン記録.py").read_text(encoding="utf-8-sig"))
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in {"parse_record_time", "clean_records", "render_substance_editor"}]
        active = False
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "all_profiles" for t in node.targets):
                active = True
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "summary" for t in node.targets):
                break
            if active:
                nodes.append(node)
        extra = '''
from datetime import time as dt_time
from hashlib import sha256
from uuid import uuid4
from app_core.substance_service import CAFFEINE_SUBSTANCE_ID, add_substance_record, format_mg, latest_substance_record, load_substance_profiles, model_snapshot_for_profile, parse_substance_datetime, records_for_substance, replace_substance_records_for_day, substance_records_total
app_now=lambda: datetime(2026,9,10,12)
'''
        return self.header() + extra + "\n".join(ast.unparse(n) for n in nodes)

    def profile_app_source(self):
        tree = ast.parse((PROJECT / "pages" / "98_ユーザー設定.py").read_text(encoding="utf-8-sig"))
        nodes = [n for n in tree.body if (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "profiles" for t in n.targets))
                 or (isinstance(n, ast.For) and isinstance(n.target, ast.Name) and n.target.id == "profile")]
        extra = f'''
from app_core.substance_service import load_substance_profiles, upsert_substance_profile, format_mg
SUBSTANCE_PROFILES_PATH=Path({str(substance.SUBSTANCE_PROFILES_PATH)!r})
'''
        return self.header() + extra + "\npage_rerun_requested=False\n" + "\n".join(ast.unparse(n) for n in nodes) + "\nif page_rerun_requested: st.rerun()"

    def test_hub_other_tab_conflict_preserves_input_until_discard(self):
        diary.save_diary_entry(self.target, {"title": "initial", "body": "before"})
        health.save_health_log(self.target, {"sleep": {
            "sleep_quality": 4, "morning_sleepiness": 1, "midnight_awakenings": 2,
        }})
        app = self.app(self.hub_app_source())
        Context().run(lambda: diary.save_diary_entry(self.target, {"title": "remote"}))
        Context().run(lambda: health.save_health_log(self.target, {"sleep": {
            "sleep_quality": 2, "morning_sleepiness": 3, "midnight_awakenings": 1,
        }}))
        before = self.live_bytes()
        next(w for w in app.text_area if w.label == "今日のメモ").input("local body")
        next(w for w in app.slider if w.label == "睡眠の質").set_value(5)
        next(w for w in app.slider if w.label == "起床時の眠気").set_value(4)
        next(w for w in app.number_input if w.label == "中途覚醒回数").set_value(3)
        next(w for w in app.button if w.label == "今日の主要記録を保存").click().run()
        self.assertEqual(len(app.exception), 0, str(app.exception))
        self.assertTrue(app.error)
        self.assertEqual(self.live_bytes(), before)
        self.assertEqual(diary.load_diary_entry(self.target)["title"], "remote")
        self.assertEqual(next(w for w in app.text_area if w.label == "今日のメモ").value, "local body")
        self.assertEqual(next(w for w in app.slider if w.label == "睡眠の質").value, 5)
        self.assertEqual(next(w for w in app.slider if w.label == "起床時の眠気").value, 4)
        self.assertEqual(next(w for w in app.number_input if w.label == "中途覚醒回数").value, 3)
        next(w for w in app.button if w.label == "今日の主要記録を保存").click().run()
        self.assertTrue(app.error)
        self.assertEqual(self.live_bytes(), before)
        next(w for w in app.button if w.label == "入力を破棄して最新の主要記録を再読込").click().run()
        self.assertEqual(next(w for w in app.text_input if w.label == "日記タイトル").value, "remote")
        self.assertEqual(next(w for w in app.slider if w.label == "睡眠の質").value, 2)
        self.assertEqual(next(w for w in app.slider if w.label == "起床時の眠気").value, 3)
        self.assertEqual(next(w for w in app.number_input if w.label == "中途覚醒回数").value, 1)

    def test_hub_sleep_fields_save_and_reopen_without_losing_other_health_values(self):
        health.save_health_log(self.target, {
            "sleep": {"sleep_quality": 4, "morning_sleepiness": 1,
                      "midnight_awakenings": 2, "nap_minutes": 45},
            "condition": {"mood_score": 2, "memo": "keep synthetic health memo"},
        })
        settings = {"health": {"sleep_quality": 2, "morning_sleepiness": 5,
                               "midnight_awakenings": 4}}
        source = self.hub_app_source(settings)
        app = self.app(source)
        self.assertEqual(next(w for w in app.slider if w.label == "睡眠の質").value, 4)
        self.assertEqual(next(w for w in app.slider if w.label == "起床時の眠気").value, 1)
        self.assertEqual(next(w for w in app.number_input if w.label == "中途覚醒回数").value, 2)
        next(w for w in app.slider if w.label == "睡眠の質").set_value(5)
        next(w for w in app.slider if w.label == "起床時の眠気").set_value(4)
        next(w for w in app.number_input if w.label == "中途覚醒回数").set_value(3)
        next(w for w in app.button if w.label == "今日の主要記録を保存").click().run()
        self.assertEqual(len(app.exception), 0, str(app.exception))
        self.assertFalse(app.error, str(app.error))
        saved = health.load_health_log(self.target)
        self.assertEqual(saved["sleep"]["sleep_quality"], 5)
        self.assertEqual(saved["sleep"]["morning_sleepiness"], 4)
        self.assertEqual(saved["sleep"]["midnight_awakenings"], 3)
        self.assertEqual(saved["sleep"]["nap_minutes"], 45)
        self.assertEqual(saved["condition"]["mood_score"], 2)
        self.assertEqual(saved["condition"]["memo"], "keep synthetic health memo")
        reopened = self.app(source)
        self.assertEqual(next(w for w in reopened.slider if w.label == "睡眠の質").value, 5)
        self.assertEqual(next(w for w in reopened.slider if w.label == "起床時の眠気").value, 4)
        self.assertEqual(next(w for w in reopened.number_input if w.label == "中途覚醒回数").value, 3)

    def test_hub_sleep_fields_use_user_defaults_without_creating_a_health_log(self):
        path = health.get_health_log_path(self.target)
        self.assertFalse(path.exists())
        before = self.live_bytes()
        settings = {"health": {"sleep_quality": 2, "morning_sleepiness": 5,
                               "midnight_awakenings": 4}}
        app = self.app(self.hub_app_source(settings))
        self.assertEqual(next(w for w in app.slider if w.label == "睡眠の質").value, 2)
        self.assertEqual(next(w for w in app.slider if w.label == "起床時の眠気").value, 5)
        self.assertEqual(next(w for w in app.number_input if w.label == "中途覚醒回数").value, 4)
        self.assertFalse(path.exists())
        self.assertEqual(self.live_bytes(), before)

    def test_diary_costs_are_never_truncated_by_body_only_save(self):
        for count in (0, 20, 21, 50):
            with self.subTest(count=count):
                costs = [{"name": f"item_{i}", "item": f"legacy_{i}", "amount": 100.0,
                          "cost_id": f"cost_{i}", "custom": {"retain": [i, "metadata"]}} for i in range(count)]
                diary.load_diary_entry(self.target)
                diary.save_diary_entry(self.target, {"cost_records": costs})
                app = self.app(self.diary_app_source())
                next(w for w in app.text_area if w.label == "本文").input(f"body {count}")
                next(w for w in app.button if w.label == "保存する").click().run()
                self.assertEqual(len(app.exception), 0, str(app.exception))
                self.assertFalse(app.error, str(app.error))
                self.assertEqual(diary.load_diary_entry(self.target)["cost_records"], costs)

    def test_twenty_costs_plus_hub_cost_and_explicit_delete(self):
        costs = [{"name": f"item_{i}", "amount": 100.0} for i in range(20)]
        diary.save_diary_entry(self.target, {"cost_records": costs})
        diary.save_diary_entry(self.target, diary.upsert_daily_hub_cost(diary.load_diary_entry(self.target), "hub", 500))
        app = self.app(self.diary_app_source())
        count = next(w for w in app.number_input if w.label == "自己投資費用の入力行数")
        self.assertEqual(count.min, 21)
        next(w for w in app.checkbox if w.label == "削除 21").check()
        next(w for w in app.button if w.label == "保存する").click().run()
        self.assertEqual(len(app.exception), 0, str(app.exception))
        self.assertEqual(diary.load_diary_entry(self.target)["cost_records"], costs)

    def test_medication_ui_conflict_does_not_rebind_old_inputs_to_new_row(self):
        substance.add_substance_record(self.target, "caffeine", 100, taken_at=datetime(2026, 9, 10, 12))
        original_id = diary.load_diary_entry(self.target)["substance_records"][0]["substance_record_id"]
        app = self.app(self.substance_app_source())
        memo_key = next(w.key for w in app.text_input if str(w.key).startswith("substance_edit_memo_") and original_id in str(w.key))
        Context().run(lambda: substance.add_substance_record(self.target, "caffeine", 50, taken_at=datetime(2026, 9, 10, 9)))
        before = self.live_bytes()
        app.text_input(key=memo_key).input("keep my local memo")
        next(w for w in app.button if w.label == "履歴を保存").click().run()
        self.assertEqual(len(app.exception), 0, str(app.exception))
        self.assertTrue(app.error)
        self.assertEqual(self.live_bytes(), before)
        self.assertEqual(app.text_input(key=memo_key).value, "keep my local memo")
        next(w for w in app.button if w.label == "履歴の入力を破棄して最新データを再読込").click().run()
        self.assertEqual(len(app.exception), 0, str(app.exception))
        live_ids = {r["substance_record_id"] for r in diary.load_diary_entry(self.target)["substance_records"]}
        for record_id in live_ids:
            self.assertTrue(any(record_id in str(w.key) for w in app.text_input))

    def test_profile_ui_conflict_keeps_typed_value_without_reverting_model(self):
        substance.save_substance_profiles(substance.load_substance_profiles())
        app = self.app(self.profile_app_source())
        Context().run(lambda: substance.upsert_substance_profile({"substance_id": "caffeine", "half_life_hours": 7.0}))
        app.text_input(key="substance_default_label_caffeine_0").input("local brand")
        next(w for w in app.button if w.label == "この成分設定を保存" and "caffeine" in str(w.key)).click().run()
        self.assertEqual(len(app.exception), 0, str(app.exception))
        self.assertTrue(app.error)
        self.assertEqual(app.text_input(key="substance_default_label_caffeine_0").value, "local brand")
        self.assertEqual(substance.substance_profile_map()["caffeine"]["half_life_hours"], 7.0)
        app.button(key="reload_substance_profile:caffeine").click().run()
        self.assertEqual(app.number_input(key="substance_half_life_caffeine_1").value, 7.0)


if __name__ == "__main__":
    unittest.main()
