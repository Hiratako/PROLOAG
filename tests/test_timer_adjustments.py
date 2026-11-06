from __future__ import annotations

import ast
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import Mock, patch

from app_core import json_store, study_log_service
import data_utils


class SessionState(dict):
    def __getattr__(self, key: str) -> Any:
        try:
            return self[key]
        except KeyError as error:
            raise AttributeError(key) from error

    def __setattr__(self, key: str, value: Any) -> None:
        self[key] = value


class RerunRequested(Exception):
    pass


class TimerUI:
    """Minimal Streamlit stand-in: run actual page callbacks, never the page body."""

    def __init__(self) -> None:
        self.session_state = SessionState()
        self.clicked: str | None = None
        self.buttons: dict[str, bool] = {}
        self.rendered_keys: set[str] = set()
        self.rerun_count = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def columns(self, columns, **_kwargs):
        return [self] * (columns if isinstance(columns, int) else len(columns))

    def expander(self, *_args, **_kwargs):
        return self

    def button(self, label: str, *, disabled: bool = False, on_click=None, args=None, kwargs=None, **_kwargs) -> bool:
        self.buttons[label] = disabled
        clicked = self.clicked == label and not disabled
        if clicked and callable(on_click):
            on_click(*(args or ()), **(kwargs or {}))
        return clicked

    def rerun(self) -> None:
        self.rerun_count += 1
        raise RerunRequested()

    def text_area(self, *_args, value="", key=None, **_kwargs):
        if key:
            self.rendered_keys.add(key)
        return self.session_state.get(key, value)

    def multiselect(self, *_args, key=None, **_kwargs):
        if key:
            self.rendered_keys.add(key)
        return self.session_state.get(key, [])

    def checkbox(self, *_args, key=None, **_kwargs):
        if key:
            self.rendered_keys.add(key)
        return self.session_state.get(key, False)

    def text_input(self, *_args, value="", key=None, **_kwargs):
        if key:
            self.rendered_keys.add(key)
        return self.session_state.get(key, value)

    def subheader(self, *_args, **_kwargs):
        pass

    def caption(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def progress(self, *_args, **_kwargs):
        pass

    def toast(self, *_args, **_kwargs):
        pass


class TimerAdjustmentTests(unittest.TestCase):
    target = date(2026, 9, 3)

    @classmethod
    def setUpClass(cls) -> None:
        page = next((Path(__file__).resolve().parents[1] / "pages").glob("01_*.py"))
        tree = ast.parse(page.read_text(encoding="utf-8-sig"), filename=str(page))
        functions = [deepcopy(node) for node in tree.body if isinstance(node, ast.FunctionDef)]
        for function in functions:
            function.decorator_list = []
        cls.page_code = compile(ast.Module(body=functions, type_ignores=[]), str(page), "exec")

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.clock = datetime(2026, 9, 3, 9, 0)
        self.perf = 100.0
        self.ui = TimerUI()
        self.metrics = Mock()
        owner = self

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return owner.clock if tz is None else owner.clock.astimezone(tz)

        self.scope = {
            "Any": Any,
            "Path": Path,
            "date": date,
            "datetime": FrozenDatetime,
            "time": SimpleNamespace(perf_counter=lambda: self.perf),
            "st": self.ui,
            "ab": SimpleNamespace(metric_block=self.metrics, queue_success=Mock(),
                                  show_action_error=lambda message, **_kwargs: self.ui.error(message)),
            "ROOT": self.root,
            "STUDY_LOGS_DIR": self.root / "study_logs",
            "app_today": lambda: self.target,
            "calculate_session_money_with_daily_total": study_log_service.calculate_session_money_with_daily_total,
            "load_study_sessions_file": data_utils.load_study_sessions_file,
            "reserved_study_session_ids": study_log_service.reserved_study_session_ids,
            "generate_id": data_utils.generate_id,
            "append_study_session": study_log_service.append_study_session,
            "json_transaction": json_store.json_transaction,
            "load_all_notes": lambda _root: [],
            "link_notes_to_session": Mock(),
            "link_session_to_plan_task": Mock(),
        }
        exec(self.page_code, self.scope)
        self.scope["init_state"]()
        self.scope["init_timer_settings"]({"timer": {"hourly_rate": 3600}})
        patcher = patch.object(json_store, "PROJECT_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    @property
    def state(self) -> SessionState:
        return self.ui.session_state

    def advance(self, seconds: float) -> None:
        self.perf += seconds
        self.clock += timedelta(seconds=seconds)

    def elapsed(self) -> float:
        return self.scope["current_elapsed"]()

    def click(self, label: str, *, pending: bool = False) -> None:
        self.ui.clicked = label
        self.ui.buttons.clear()
        try:
            if pending:
                self.scope["render_pending_save"]({}, {"category": "テスト"})
            else:
                self.scope["render_controls"]()
        except RerunRequested:
            pass
        finally:
            self.ui.clicked = None

    def start_and_run(self, seconds: float = 1200) -> None:
        self.click("開始")
        self.assertTrue(self.state.running)
        self.advance(seconds)

    def rows(self) -> list[dict[str, Any]]:
        return study_log_service.load_study_sessions_by_date(self.target, self.root)

    def assert_save_matches(self, seconds: int, *, money: int | None = None) -> dict[str, Any]:
        expected_money = seconds if money is None else money
        pending = deepcopy(self.state.pending_session)
        self.assertEqual(pending["duration_sec"], seconds)
        self.assertEqual(pending["money"], expected_money)
        self.click("保存する", pending=True)
        self.metrics.assert_called_with(
            "保存予定時間", self.scope["sec_to_hms"](seconds),
            f"換算金額 {self.scope['yen'](expected_money)}",
        )
        saved = self.rows()[-1]
        self.assertEqual(saved["duration_sec"], seconds)
        self.assertEqual(saved["money"], expected_money)
        for key in ("start_at", "end_at", "normal_money", "overtime_money", "hourly_rate"):
            self.assertEqual(saved[key], pending[key])
        self.assertIsNone(self.state.pending_session)
        self.assertFalse(self.state.running)
        self.assertEqual(self.elapsed(), 0)
        return saved

    def test_running_minus_then_pause_and_save(self) -> None:
        self.start_and_run()
        for _ in range(3):
            self.click("-60秒")
        self.assertEqual(self.elapsed(), 1020)
        self.click("一時停止")
        self.assert_save_matches(1020)

    def test_paused_minus_then_save_updates_display_duration_and_money(self) -> None:
        self.start_and_run()
        self.click("一時停止")
        for _ in range(3):
            self.click("-60秒")
        self.assertEqual(self.elapsed(), 1020)
        self.assert_save_matches(1020)

    def test_paused_plus_then_save_updates_display_duration_and_money(self) -> None:
        self.start_and_run()
        self.click("一時停止")
        for _ in range(3):
            self.click("+60秒")
        self.assertEqual(self.elapsed(), 1380)
        self.assert_save_matches(1380)

    def test_mixed_adjustments_preserve_start_end_and_task_metadata(self) -> None:
        self.state.selected_plan_task_id = "task_test"
        self.start_and_run()
        self.click("一時停止")
        before = deepcopy(self.state.pending_session)
        for label in ("-60秒", "-60秒", "+60秒", "-60秒", "+60秒"):
            self.click(label)
        for key in ("start_at", "end_at", "hourly_rate", "linked_task_id"):
            self.assertEqual(self.state.pending_session[key], before[key])
        self.assertEqual(self.elapsed(), 1140)
        self.assert_save_matches(1140)

    def test_running_zero_floor_does_not_leave_hidden_negative_adjustment(self) -> None:
        self.start_and_run(30)
        for _ in range(3):
            self.click("-60秒")
        self.assertEqual(self.elapsed(), 0)
        self.advance(10)
        self.assertEqual(self.elapsed(), 10)
        self.click("+60秒")
        self.click("一時停止")
        self.assert_save_matches(70)

    def test_paused_zero_duration_cannot_be_saved_and_can_be_increased_again(self) -> None:
        self.start_and_run(30)
        self.click("一時停止")
        for _ in range(3):
            self.click("-60秒")
        self.assertEqual(self.elapsed(), 0)
        self.assertEqual(self.state.pending_session["duration_sec"], 0)
        self.click("保存する", pending=True)
        self.assertEqual(self.rows(), [])
        self.assertFalse((self.root / "study_logs").exists())
        self.assertIsNotNone(self.state.pending_session)
        self.click("+60秒")
        self.assertEqual(self.elapsed(), 60)
        self.assert_save_matches(60)

    def test_idle_zero_floor_does_not_hide_next_plus(self) -> None:
        for _ in range(3):
            self.click("-60秒")
        self.click("+60秒")
        self.assertEqual(self.elapsed(), 60)
        self.assertIsNone(self.state.pending_session)
        self.assertEqual(list(self.root.rglob("*.json")), [])

    def test_reset_pending_adjustments_keep_reset_display_at_zero(self) -> None:
        self.start_and_run()
        self.click("リセット")
        self.assertIsNone(self.state.start_at)
        self.assertEqual(self.elapsed(), 0)
        for _ in range(3):
            self.click("-60秒")
        self.assertEqual(self.elapsed(), 0)
        self.click("+60秒")
        self.assertEqual(self.elapsed(), 0)
        self.assert_save_matches(1080)

    def test_resume_paused_pending_accumulates_without_double_adjustment(self) -> None:
        self.start_and_run()
        self.click("一時停止")
        initial_start = self.state.start_at
        self.click("-60秒")
        self.click("開始")
        self.assertEqual(self.state.start_at, initial_start)
        self.advance(120)
        self.click("一時停止")
        self.assertEqual(self.elapsed(), 1260)
        self.assert_save_matches(1260)

    def test_reset_pending_blocks_new_start_until_saved(self) -> None:
        self.start_and_run()
        self.click("リセット")
        self.click("-60秒")
        pending = deepcopy(self.state.pending_session)
        self.click("開始")
        self.assertFalse(self.state.running)
        self.assertIsNone(self.state.start_at)
        self.assertEqual(self.state.pending_session, pending)
        self.assertEqual(self.elapsed(), 0)
        self.assert_save_matches(1140)
        self.click("開始")
        self.assertTrue(self.state.running)
        self.assertEqual(self.elapsed(), 0)

    def test_old_pending_cannot_be_saved_or_discarded_while_running(self) -> None:
        self.start_and_run()
        self.click("一時停止")
        self.click("-60秒")
        self.click("開始")
        self.advance(120)
        before = deepcopy(self.state)
        for label in ("保存する", "破棄する"):
            with self.subTest(label=label):
                self.click(label, pending=True)
                self.assertEqual(self.state, before)
                self.assertEqual(self.rows(), [])
        self.click("一時停止")
        self.assert_save_matches(1260)

    def test_control_callbacks_keep_pending_inputs_rendered_without_forced_rerun(self) -> None:
        self.start_and_run()
        self.click("一時停止")
        self.state.pending_memo = "補正と再開をしても保持するメモ"
        for label in ("-60秒", "+60秒", "開始", "一時停止", "リセット"):
            with self.subTest(control=label):
                reruns_before = self.ui.rerun_count
                self.click(label)
                self.assertEqual(self.ui.rerun_count, reruns_before)
                self.ui.rendered_keys.clear()
                self.scope["render_pending_save"]({}, {})
                self.assertTrue({"pending_memo", "pending_notes", "pending_create_note"} <= self.ui.rendered_keys)
                self.assertEqual(self.state.pending_memo, "補正と再開をしても保持するメモ")
                if label == "開始":
                    self.assertTrue(self.ui.buttons["保存する"])
                    self.assertTrue(self.ui.buttons["破棄する"])
                    self.advance(120)
        saved = self.assert_save_matches(1320)
        self.assertEqual(saved["memo"], "補正と再開をしても保持するメモ")

    def test_resume_then_zero_and_pause_replaces_old_nonzero_pending(self) -> None:
        self.start_and_run(60)
        self.click("一時停止")
        self.click("開始")
        self.click("-60秒")
        self.click("一時停止")
        self.assertEqual(self.state.pending_session["duration_sec"], 0)
        self.click("保存する", pending=True)
        self.assertEqual(self.rows(), [])
        self.assertEqual(list(self.root.rglob("*.json")), [])

    def test_resume_then_zero_and_reset_replaces_old_nonzero_pending(self) -> None:
        self.start_and_run(60)
        self.click("一時停止")
        self.click("開始")
        self.click("-60秒")
        self.click("リセット")
        self.assertEqual(self.state.pending_session["duration_sec"], 0)
        self.click("保存する", pending=True)
        self.assertEqual(self.rows(), [])
        self.assertEqual(list(self.root.rglob("*.json")), [])

    def test_existing_paused_display_adjustment_is_reconciled_before_save(self) -> None:
        self.start_and_run()
        self.click("一時停止")
        # Session state left by the old page: adjusted display, stale pending copy.
        self.state.display_adjust_sec = -180
        self.assertEqual(self.state.pending_session["duration_sec"], 1200)
        self.scope["render_pending_save"]({}, {})
        self.assertEqual(self.elapsed(), 1020)
        self.assert_save_matches(1020)

    def test_plus_after_subsecond_pause_creates_a_savable_pending_session(self) -> None:
        self.start_and_run(0.2)
        self.click("一時停止")
        self.assertIsNone(self.state.pending_session)
        self.click("+60秒")
        self.assert_save_matches(60)

    def test_overtime_is_recalculated_after_paused_adjustment(self) -> None:
        self.state.overtime_threshold = 0.25
        self.state.overtime_multiplier = 2.0
        self.start_and_run()
        self.click("一時停止")
        self.assertEqual(self.state.pending_session["money"], 1500)
        for _ in range(6):
            self.click("-60秒")
        self.assertEqual(self.elapsed(), 840)
        saved = self.assert_save_matches(840, money=840)
        self.assertEqual(saved["normal_money"], 840)
        self.assertEqual(saved["overtime_money"], 0)

    def test_subsecond_elapsed_display_money_and_saved_integer_seconds_match(self) -> None:
        self.start_and_run(1200.8)
        self.scope["render_realtime"]()
        self.metrics.assert_any_call("現在セッション", "00:20:00", "画面上の計測時間")
        self.metrics.assert_any_call("現在セッション換算", "1,200円", "通常 1,200円 / 残業 0円")
        self.click("一時停止")
        self.click("-60秒")
        self.scope["render_realtime"]()
        self.metrics.assert_any_call("現在セッション", "00:19:00", "画面上の計測時間")
        self.metrics.assert_any_call("現在セッション換算", "1,140円", "通常 1,140円 / 残業 0円")
        self.assert_save_matches(1140)

        with self.subTest(hourly_rate=2000, adjusted_minutes=17):
            self.state.hourly_rate = 2000
            self.metrics.reset_mock()
            self.start_and_run(1200.8)
            self.click("一時停止")
            for _ in range(3):
                self.click("-60秒")
            self.scope["render_realtime"]()
            self.metrics.assert_any_call("現在セッション", "00:17:00", "画面上の計測時間")
            self.metrics.assert_any_call("現在セッション換算", "567円", "通常 567円 / 残業 0円")
            self.assert_save_matches(1020, money=567)

    def test_miniwindow_pause_resume_preserves_time_draft_and_saved_duration(self) -> None:
        self.start_and_run(120)
        initial_start = self.state.start_at
        self.state.pending_memo = "小窓から再開しても残るメモ"
        self.state.pending_notes = []
        self.state.pending_create_note = False
        command = lambda action: {"action": action, "revision": self.state.miniwindow_control_revision}
        self.assertTrue(self.scope["apply_miniwindow_control"](command("pause")))
        self.assertFalse(self.state.running)
        self.assertEqual(self.state.pending_session["duration_sec"], 120)
        self.advance(90)
        self.assertEqual(self.elapsed(), 120)
        self.assertTrue(self.scope["apply_miniwindow_control"](command("resume")))
        self.assertEqual(self.state.start_at, initial_start)
        self.advance(30)
        self.assertTrue(self.scope["apply_miniwindow_control"](command("pause")))
        self.assertEqual(self.state.pending_session["duration_sec"], 150)
        self.assertEqual(self.state.pending_memo, "小窓から再開しても残るメモ")
        self.assertEqual(self.state.pending_notes, [])
        self.assertEqual(list(self.root.rglob("*.json")), [])
        saved = self.assert_save_matches(150)
        self.assertEqual(saved["memo"], "小窓から再開しても残るメモ")

    def test_miniwindow_duplicate_or_delayed_commands_do_not_change_new_state(self) -> None:
        self.start_and_run(60)
        old_pause = {"action": "pause", "revision": self.state.miniwindow_control_revision}
        self.assertTrue(self.scope["apply_miniwindow_control"](old_pause))
        self.assertFalse(self.scope["apply_miniwindow_control"](old_pause))
        self.click("開始")
        self.advance(20)
        self.assertFalse(self.scope["apply_miniwindow_control"](old_pause))
        self.assertTrue(self.state.running)
        self.assertEqual(self.elapsed(), 80)
        self.click("一時停止")
        old_resume = {"action": "resume", "revision": self.state.miniwindow_control_revision}
        self.click("リセット")
        self.assertFalse(self.scope["apply_miniwindow_control"](old_resume))
        self.assertFalse(self.scope["apply_miniwindow_control"]({
            "action": "resume", "revision": self.state.miniwindow_control_revision,
        }))
        self.assertFalse(self.state.running)
        self.assertIsNone(self.state.start_at)
        self.assertEqual(self.state.pending_session["duration_sec"], 80)
        self.assert_save_matches(80)
        self.assertFalse(self.scope["apply_miniwindow_control"](old_resume))
        self.assertFalse(self.scope["apply_miniwindow_control"]({
            "action": "resume", "revision": self.state.miniwindow_control_revision,
        }))

    def test_miniwindow_rejects_idle_and_invalid_commands_without_writes(self) -> None:
        apply = self.scope["apply_miniwindow_control"]
        before = deepcopy(dict(self.state))
        for command in (None, [], {}, {"action": "toggle", "revision": 0},
                        {"action": "pause", "revision": False},
                        {"action": "resume", "revision": "0"},
                        {"action": "resume", "revision": 0},
                        {"action": "pause", "revision": 0}):
            with self.subTest(command=command):
                self.assertFalse(apply(command))
                self.assertEqual(dict(self.state), before)
        self.assertEqual(list(self.root.rglob("*.json")), [])

    def test_miniwindow_command_refreshes_outer_controls_once_but_ticks_do_not(self) -> None:
        self.start_and_run(60)
        revision = self.state.miniwindow_control_revision
        self.scope["render_realtime"] = Mock()
        renderer = self.scope["render_timer_miniwindow"] = Mock(return_value={
            "action": "pause", "revision": revision,
        })
        with self.assertRaises(RerunRequested):
            self.scope["ticking_realtime"]()
        self.assertEqual(self.ui.rerun_count, 1)
        self.assertFalse(self.state.running)
        renderer.assert_called_once_with(60.0, True, control_revision=revision, can_resume=False)
        renderer.return_value = None
        self.scope["ticking_realtime"]()
        self.assertEqual(self.ui.rerun_count, 1)
        self.assertTrue(renderer.call_args.kwargs["can_resume"])

    def test_adjustment_and_rerender_without_save_do_not_write_json(self) -> None:
        self.start_and_run()
        self.click("一時停止")
        self.click("-60秒")
        pending = deepcopy(self.state.pending_session)
        for _ in range(3):
            self.scope["render_pending_save"]({}, {})
        self.assertEqual(self.state.pending_session, pending)
        self.assertEqual(list(self.root.rglob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
