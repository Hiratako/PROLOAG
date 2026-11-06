"""Existing intake timestamps retain their date when editing an owner file."""
from __future__ import annotations

import ast
from copy import deepcopy
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import patch
from uuid import uuid4

import prolog_ui.components as ab
from app_core import app_clock, diary_service, form_snapshot, json_store, substance_service


PAGE = Path(__file__).resolve().parents[1] / "pages" / "18_カフェイン記録.py"
DAY = date(2026, 9, 17)
PROFILE = {
    "substance_id": "caffeine", "name": "カフェイン", "is_active": True,
    "default_amount_mg": 50.0, "amount_step_mg": 5.0,
    "half_life_hours": 5.0, "peak_delay_minutes": 30,
    "residual_cutoff_hours": 36.0, "model_revision": 2,
}
SNAPSHOT = {"schema_version": 1, "profile_revision": 1, "unit": "mg", "half_life_hours": 8.0, "peak_delay_minutes": 45, "residual_cutoff_hours": 60.0}


def page_functions():
    tree = ast.parse(PAGE.read_text(encoding="utf-8-sig"))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in {"clean_records", "parse_record_time"}]
    scope = {
        "date": date, "datetime": datetime, "dt_time": time, "Any": Any, "uuid4": uuid4,
        "model_snapshot_for_profile": substance_service.model_snapshot_for_profile,
        "parse_substance_datetime": substance_service.parse_substance_datetime,
        "app_now": lambda: datetime(2026, 9, 17, 12),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(PAGE), "exec"), scope)
    return scope


class SubstanceEditorDateTests(unittest.TestCase):
    def setUp(self):
        self.functions = page_functions()

    def clean(self, timestamp: str, clock: time | None = None):
        normalized = substance_service.parse_substance_datetime(timestamp, DAY)
        row = {"substance_record_id": "old", "taken_at": normalized.isoformat(),
               "taken_time": clock or normalized.time(), "amount_mg": 25.0,
               "memo": "編集したメモ", "model_snapshot": deepcopy(SNAPSHOT)}
        before = deepcopy(row)
        result = self.functions["clean_records"]([row], DAY, PROFILE)[0]
        self.assertEqual(row, before)
        self.assertEqual(result["model_snapshot"], SNAPSHOT)
        return normalized, result

    def test_existing_local_same_previous_and_next_dates_survive_memo_edit(self):
        for value in ("2026-09-16T23:45:30", "2026-09-17T12:30:00", "2026-09-18T00:15:00"):
            with self.subTest(value=value):
                normalized, result = self.clean(value)
                self.assertEqual(result["taken_at"], normalized.isoformat(timespec="seconds"))

    def test_offset_timestamps_keep_normalized_local_date_and_model(self):
        for value in ("2026-09-17T23:30:00-05:00", "2026-09-17T00:30:00+14:00", "2026-09-17T03:30:00Z"):
            with self.subTest(value=value):
                normalized, result = self.clean(value)
                self.assertEqual(result["taken_at"], normalized.isoformat(timespec="seconds"))

    def test_time_edit_changes_only_time_on_existing_actual_intake_day(self):
        normalized, result = self.clean("2026-09-18T13:30:00", time(14, 45))
        self.assertEqual(result["taken_at"], datetime.combine(normalized.date(), time(14, 45)).isoformat())

    def test_new_record_uses_selected_day_and_unused_untimed_row_is_not_added(self):
        rows = [
            {"taken_time": time(15, 30), "amount_mg": 10, "taken_at": "2026-09-18T12:00:00"},
            {"taken_time": time(12), "amount_mg": 0},
            {"taken_time": None, "amount_mg": 10},
        ]
        result = self.functions["clean_records"](rows, DAY, PROFILE)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["taken_at"], "2026-09-17T15:30:00")
        self.assertEqual(self.functions["parse_record_time"]("", time(9, 15)), time(9, 15))

    def test_real_editor_passes_original_day_and_untimed_amount_to_save(self):
        from streamlit import config
        from streamlit.testing.v1 import AppTest
        config.get_config_options(options_from_flags={"global.developmentMode": False, "browser.gatherUsageStats": False})
        timestamp = "2026-09-18T13:30:00"
        entry = {"date": DAY.isoformat(), "substance_records_schema_version": 1,
                 "caffeine_untimed_mg": 5.0,
                 "substance_records": [{"substance_record_id": "old", "substance_id": "caffeine",
                                        "taken_at": timestamp, "amount_mg": 25.0, "memo": "変更前",
                                        "model_snapshot": SNAPSHOT}]}
        path = Path("fixture-only-unused") / "20260917.json"
        patches = [
            patch("app_core.settings_service.load_user_settings", return_value={}),
            patch.object(app_clock, "app_today", return_value=DAY),
            patch.object(app_clock, "app_now", return_value=datetime(2026, 9, 17, 12)),
            patch.object(substance_service, "load_substance_profiles", return_value=[PROFILE]),
            patch.object(diary_service, "load_diary_entry", return_value=entry),
            patch.object(diary_service, "get_diary_entry_path", return_value=path),
            patch.object(form_snapshot, "form_snapshot", return_value={"data": entry, "version": 0, "revisions": {path: "fixture"}}),
            patch.object(form_snapshot, "discard_form_snapshot"),
            patch.object(json_store, "_read", side_effect=AssertionError("No personal JSON reads")),
        ]
        for name in ("inject_css", "sidemenu", "topbar", "sidebar_page_link", "queue_success"):
            patches.append(patch.object(ab, name))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        with patch.object(substance_service, "replace_substance_records_for_day") as save:
            app = AppTest.from_file(str(PAGE), default_timeout=30).run()
            self.assertFalse(app.exception, str(app.exception))
            self.assertTrue(any("摂取日: 2026-09-18" in item.value for item in app.caption))
            next(item for item in app.text_input if item.label == "メモ 1").input("メモだけ更新")
            next(item for item in app.button if item.label == "履歴を保存").click().run()
            self.assertFalse(app.exception, str(app.exception))
            save.assert_called_once()
            self.assertEqual(save.call_args.args[:2], (DAY, "caffeine"))
            saved = save.call_args.args[2][0]
            self.assertEqual(saved["taken_at"], timestamp)
            self.assertEqual(saved["memo"], "メモだけ更新")
            self.assertEqual(saved["model_snapshot"], SNAPSHOT)
            self.assertEqual(save.call_args.kwargs["caffeine_untimed_mg"], 5.0)


if __name__ == "__main__":
    unittest.main()
