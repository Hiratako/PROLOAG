from __future__ import annotations

import ast
from contextlib import nullcontext
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import Mock

from app_core.json_store import JsonReadError
from app_core.study_log_service import calculate_session_money_with_daily_total


class TimerReadEfficiencyTests(unittest.TestCase):
    """Exercise actual display helpers without executing the page or reading records."""

    @classmethod
    def setUpClass(cls):
        page = next((Path(__file__).resolve().parents[1] / "pages").glob("01_*.py"))
        selected = {"calc_money", "study_log_path", "daily_path", "load_sessions",
                    "load_day_totals", "render_realtime", "sec_to_hms", "yen"}
        tree = ast.parse(page.read_text(encoding="utf-8-sig"))
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in selected]
        cls.code = compile(ast.Module(body=functions, type_ignores=[]), str(page), "exec")

    def setUp(self):
        self.start = datetime(2026, 9, 16, 23, 50)
        self.state = SimpleNamespace(
            start_at=self.start, hourly_rate=3600, overtime_enabled=True,
            overtime_threshold=1, overtime_multiplier=1.5, goal_minutes=120,
            goal_toasted=False, interleaving_enabled=False,
        )
        self.rows = [{"session_id": "synthetic", "start_at": "2026-09-16T09:00:00",
                      "duration_sec": 3000, "money": 3000}]
        self.reader = Mock(return_value=self.rows)
        self.metrics = Mock()
        self.ui = SimpleNamespace(
            session_state=self.state, columns=lambda count: [nullcontext() for _ in range(count)],
            toast=Mock(), progress=Mock(),
        )
        self.scope = {
            "Any": Any, "date": date, "datetime": datetime, "Path": Path,
            "STUDY_LOGS_DIR": Path("synthetic-study-logs"),
            "load_study_sessions_file": self.reader,
            "calculate_session_money_with_daily_total": calculate_session_money_with_daily_total,
            "current_elapsed": Mock(return_value=1200), "app_today": lambda: date(2026, 9, 17),
            "st": self.ui, "ab": SimpleNamespace(metric_block=self.metrics),
        }
        exec(self.code, self.scope)

    def test_tick_reads_once_and_preserves_overtime_display(self):
        before = deepcopy(self.rows)
        self.scope["render_realtime"]()
        self.reader.assert_called_once_with(Path("synthetic-study-logs/2026/09/20260916.json"))
        # Existing 50 min + current 20 min: 10 regular + 10 overtime minutes.
        self.metrics.assert_any_call("現在セッション換算", "1,500円", "通常 600円 / 残業 900円")
        self.metrics.assert_any_call("今日の累計勉強時間", "01:10:00", "保存済み 00:50:00 / 1件")
        self.metrics.assert_any_call("今日の換算金額", "4,500円", "目標達成率 58.3%")
        self.assertEqual(self.rows, before)

    def test_next_tick_observes_external_changes(self):
        self.reader.side_effect = [self.rows, []]
        self.scope["render_realtime"]()
        self.scope["render_realtime"]()
        self.assertEqual(self.reader.call_count, 2)
        self.metrics.assert_any_call("今日の累計勉強時間", "00:20:00", "保存済み 00:00:00 / 0件")
        self.metrics.assert_any_call("現在セッション換算", "1,200円", "通常 1,200円 / 残業 0円")

    def test_empty_supplied_snapshot_does_not_fall_back_to_reading(self):
        self.reader.side_effect = AssertionError("unexpected disk read")
        self.assertEqual(self.scope["load_day_totals"](self.start.date(), sessions=[]), (0, 0, 0))
        money = self.scope["calc_money"](1200, 3600, True, 1, 1.5, self.start, sessions=[])
        self.assertEqual(money, (1200, 1200, 0))

    def test_default_money_path_still_reads_fresh_for_freeze_and_save(self):
        self.reader.side_effect = [self.rows, []]
        first = self.scope["calc_money"](1200, 3600, True, 1, 1.5, self.start)
        second = self.scope["calc_money"](1200, 3600, True, 1, 1.5, self.start)
        self.assertEqual(first, (1500, 600, 900))
        self.assertEqual(second, (1200, 1200, 0))
        self.assertEqual(self.reader.call_count, 2)

    def test_default_total_path_keeps_compatibility(self):
        self.assertEqual(self.scope["load_day_totals"](self.start.date()), (3000, 3000, 1))
        self.reader.assert_called_once()

    def test_idle_display_reads_today_but_active_display_uses_start_day(self):
        self.scope["render_realtime"]()
        self.state.start_at = None
        self.scope["render_realtime"]()
        self.assertEqual([call.args[0].name for call in self.reader.call_args_list],
                         ["20260916.json", "20260917.json"])

    def test_corrupt_record_error_is_not_hidden(self):
        self.reader.side_effect = JsonReadError("synthetic corrupt JSON")
        with self.assertRaises(JsonReadError):
            self.scope["render_realtime"]()
        self.metrics.assert_not_called()


if __name__ == "__main__":
    unittest.main()
