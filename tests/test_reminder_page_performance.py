from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
import unittest
from unittest.mock import patch

import data_utils
import prolog_ui.components as ab
from app_core import app_clock, json_store, master_snapshot, reminder_service
from app_core.reminder_page_state import (
    ReminderSourceTitles,
    capture_review_draft,
    discard_review_draft,
    restore_review_draft,
)


PROJECT = Path(__file__).resolve().parents[1]
PAGE = PROJECT / "pages" / "08_忘却曲線リマインダー.py"
TODAY = date(2026, 9, 17)


def fixture_reminder(index: int, *, upcoming: bool = False) -> dict:
    return {
        "reminder_id": f"r{index}", "title": f"架空の復習 {index}",
        "memo": f"保存済み本文 {index}", "status": "pending",
        "scheduled_date": (TODAY + timedelta(days=3 if upcoming else -1)).isoformat(),
        "schedule_mode": "standard", "schedule_status": "active",
        "intervals_days": [1, 3, 7], "current_step": 0, "review_count": 0,
        "source_note_id": "n1", "source_card_id": "c1" if index % 2 else "",
        "review_history": [{"recorded_at": "2026-09-15T11:00:00", "result": "normal"}],
    }


class ReminderPageHelpersTests(unittest.TestCase):
    def test_source_titles_match_legacy_including_duplicates_aliases_and_missing(self):
        notes = [
            {"note_id": "n", "title": "先頭"}, {"note_id": "n", "title": "後続"},
            {"note_id": "untitled"}, {"note_id": "empty", "title": ""},
            {"note_id": "null", "title": None},
        ]
        cards = [
            {"card_id": "c", "flashcard_id": "alias", "question": "先頭"},
            {"card_id": "alias", "question": "後続"},
            {"flashcard_id": "front", "front": "表面"}, {"card_id": "empty"},
        ]
        references = ReminderSourceTitles(notes, cards)
        with patch.object(data_utils, "load_all_notes", return_value=notes), patch.object(data_utils, "load_all_flashcards", return_value=cards):
            for note_id in ("n", "untitled", "empty", "null", "missing", ""):
                self.assertEqual(references.note_title(note_id), data_utils.resolve_note_title(PROJECT, note_id))
            for card_id in ("c", "alias", "front", "empty", "missing", ""):
                self.assertEqual(references.card_title(card_id), data_utils.resolve_card_title(PROJECT, card_id))

    def test_preloaded_due_filter_matches_legacy_and_avoids_load_even_when_empty(self):
        records = [
            fixture_reminder(1), fixture_reminder(2, upcoming=True),
            {**fixture_reminder(3), "status": "done"},
            {**fixture_reminder(4), "schedule_status": "paused"},
            {**fixture_reminder(5), "scheduled_date": "invalid"},
            {**fixture_reminder(6), "scheduled_date": "", "registered": "2026-09-10"},
            {**fixture_reminder(7), "scheduled_date": datetime(2026, 9, 17, 12)},
        ]
        with patch.object(reminder_service, "load_reminders", return_value=records) as load, patch.object(reminder_service, "app_today", return_value=TODAY):
            expected = reminder_service.get_due_reminders(TODAY, PROJECT)
            load.assert_called_once_with(PROJECT)
            load.reset_mock()
            actual = reminder_service.get_due_reminders(TODAY, PROJECT, reminders=records)
            self.assertEqual(actual, expected)
            self.assertEqual(reminder_service.get_due_reminders(TODAY, PROJECT, reminders=[]), [])
            load.assert_not_called()
        self.assertEqual([item["reminder_id"] for item in actual], ["r1", "r5", "r6", "r7"])

    def test_draft_survives_hidden_widget_cleanup_and_is_separate_per_reminder(self):
        state = {}
        memo_key, duration_key = restore_review_draft(state, "r1")
        state[memo_key], state[duration_key] = "送信前のメモ", 25
        capture_review_draft(state, "r1")
        del state[memo_key], state[duration_key]
        second_keys = restore_review_draft(state, "r2")
        self.assertEqual([state[key] for key in second_keys], ["", 0])
        restore_review_draft(state, "r1")
        self.assertEqual([state[memo_key], state[duration_key]], ["送信前のメモ", 25])
        state[memo_key] = "ブラウザから届いた新しい値"
        restore_review_draft(state, "r1")
        self.assertEqual(state[memo_key], "ブラウザから届いた新しい値")

    def test_discard_removes_only_completed_reminder_draft_and_widgets(self):
        state = {"p08_review_drafts": {"r1": {"memo": "完了", "duration": 5}, "r2": {"memo": "未完了", "duration": 10}}}
        first = restore_review_draft(state, "r1")
        second = restore_review_draft(state, "r2")
        discard_review_draft(state, "r1")
        self.assertNotIn("r1", state["p08_review_drafts"])
        self.assertFalse(any(key in state for key in first))
        self.assertEqual([state[key] for key in second], ["未完了", 10])


class ReminderPageAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit import config
        config.get_config_options(options_from_flags={"global.developmentMode": False, "browser.gatherUsageStats": False})

    def setUp(self):
        self.records = [fixture_reminder(i, upcoming=i >= 40) for i in range(60)]
        self.notes = [{"note_id": "n1", "title": "架空ノート"}]
        self.cards = [{"card_id": "c1", "question": "架空カード"}]
        self.loads = {}
        for name, records in (("load_all_reminders", self.records), ("load_all_notes", self.notes), ("load_all_flashcards", self.cards)):
            patcher = patch.object(data_utils, name, side_effect=lambda _root, data=records, **_kwargs: deepcopy(data))
            self.loads[name] = patcher.start()
            self.addCleanup(patcher.stop)
        patchers = [
            patch.object(master_snapshot, "load_master_data", return_value={}),
            patch.object(app_clock, "app_today", return_value=TODAY),
            patch.object(reminder_service, "app_today", return_value=TODAY),
            patch.object(ab, "inject_css"), patch.object(ab, "sidemenu"),
            patch.object(ab, "topbar"), patch.object(ab, "queue_success"),
            patch.object(json_store, "_read", side_effect=AssertionError("No production JSON may be read by this test")),
        ]
        self.master_load = patchers[0].start()
        self.addCleanup(patchers[0].stop)
        for patcher in patchers[1:]:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.original_complete = reminder_service.complete_review.__wrapped__
        self.save_patch = patch.object(reminder_service, "complete_review")
        self.complete = self.save_patch.start()
        self.addCleanup(self.save_patch.stop)

    def app(self, path: Path = PAGE):
        from streamlit.testing.v1 import AppTest
        result = AppTest.from_file(str(path), default_timeout=30).run()
        self.assertFalse(result.exception, str(result.exception))
        return result

    @staticmethod
    def widget_with_label(widgets, label):
        return next(widget for widget in widgets if widget.label == label)

    def test_only_selected_view_and_one_detail_render_with_one_read_each(self):
        app = self.app()
        self.assertEqual(len(app.dataframe), 1)
        self.assertEqual(len(app.dataframe[0].value), 40)
        self.assertEqual(len(app.get("expander")), 1)  # registration only
        self.assertEqual(len([item for item in app.text_input if item.label == "今回の復習メモ"]), 1)
        self.assertFalse(any(button.label == "再開" for button in app.button))
        for load in [*self.loads.values(), self.master_load]:
            self.assertEqual(load.call_count, 1)
        for view, rows in (("今後の予定", 20), ("全件", 60), ("スケジュール管理", 1)):
            for load in [*self.loads.values(), self.master_load]:
                load.reset_mock()
            app.radio(key="p08_selected_view").set_value(view).run()
            self.assertFalse(app.exception, str(app.exception))
            self.assertEqual(len(app.dataframe), 1)
            self.assertEqual(len(app.dataframe[0].value), rows)
            self.assertFalse(any(item.label == "今回の復習メモ" for item in app.text_input))
            for load in [*self.loads.values(), self.master_load]:
                self.assertEqual(load.call_count, 1)

    def test_review_draft_and_registration_inputs_survive_view_and_item_switch(self):
        app = self.app()
        self.widget_with_label(app.text_input, "タイトル").input("新規フォームの入力")
        self.widget_with_label(app.text_area, "メモ").input("まだ登録していない説明")
        app.text_input(key="p08_review_memo_r0").input("r0 の下書き")
        app.number_input(key="p08_review_duration_r0").set_value(35)
        app.selectbox(key="p08_reminder_selection_due").set_value(("r1", 0)).run()
        self.assertFalse(app.exception, str(app.exception))
        app.text_input(key="p08_review_memo_r1").input("r1 の下書き").run()
        app.radio(key="p08_selected_view").set_value("全件").run()
        app.radio(key="p08_selected_view").set_value("今日の復習").run()
        self.assertEqual(app.text_input(key="p08_review_memo_r1").value, "r1 の下書き")
        app.selectbox(key="p08_reminder_selection_due").set_value(("r0", 0)).run()
        self.assertEqual(app.text_input(key="p08_review_memo_r0").value, "r0 の下書き")
        self.assertEqual(app.number_input(key="p08_review_duration_r0").value, 35)
        self.assertEqual(self.widget_with_label(app.text_input, "タイトル").value, "新規フォームの入力")
        self.assertEqual(self.widget_with_label(app.text_area, "メモ").value, "まだ登録していない説明")
        self.complete.assert_not_called()

    def test_all_result_buttons_forward_unchanged_payload_to_save_service(self):
        for result in ("forgot", "difficult", "normal", "easy"):
            with self.subTest(result=result):
                app = self.app()
                app.text_input(key="p08_review_memo_r0").input(" 記録するメモ ")
                app.number_input(key="p08_review_duration_r0").set_value(15)
                self.complete.reset_mock()
                app.button(key=f"review_due_r0_{result}").click().run()
                self.assertFalse(app.exception, str(app.exception))
                self.complete.assert_called_once_with("r0", result, memo=" 記録するメモ ", duration_min=15, root=PROJECT)

    def test_current_source_names_are_reloaded_after_external_edits(self):
        app = self.app()
        self.assertIn("ノート: 架空ノート", app.dataframe[0].value["参照元"].tolist())
        self.notes[0]["title"] = "外部で変わった名称"
        app.run()
        self.assertIn("ノート: 外部で変わった名称", app.dataframe[0].value["参照元"].tolist())

    def test_empty_views_and_keyword_filter_do_not_render_stale_details(self):
        self.records.clear()
        app = self.app()
        for view in ("今日の復習", "今後の予定", "全件", "スケジュール管理"):
            app.radio(key="p08_selected_view").set_value(view).run()
            self.assertFalse(app.exception, str(app.exception))
            self.assertEqual(len(app.dataframe), 0)
            self.assertEqual(len(app.info), 1)
        self.records.append(fixture_reminder(0))
        app.radio(key="p08_selected_view").set_value("今日の復習").run()
        self.widget_with_label(app.text_input, "キーワード").input("一致しない文言").run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertFalse(any(item.label == "今回の復習メモ" for item in app.text_input))
        self.widget_with_label(app.text_input, "キーワード").input("架空ノート").run()
        self.assertEqual(len(app.dataframe[0].value), 1)

    def test_save_failure_does_not_erase_review_or_registration_draft(self):
        app = self.app()
        self.widget_with_label(app.text_input, "タイトル").input("登録前の別フォーム")
        app.text_input(key="p08_review_memo_r0").input("失敗時も保持する下書き")
        self.complete.side_effect = RuntimeError("fixture-only save failure")
        app.button(key="review_due_r0_normal").click().run()
        self.assertEqual(len(app.exception), 1)
        self.complete.assert_called_once()
        self.complete.side_effect = None
        app.run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertEqual(app.text_input(key="p08_review_memo_r0").value, "失敗時も保持する下書き")
        self.assertEqual(self.widget_with_label(app.text_input, "タイトル").value, "登録前の別フォーム")

    def test_completed_or_skipped_cycle_does_not_restore_old_memo_when_due_again(self):
        actual_skip = reminder_service.skip_review.__wrapped__
        for action in ("complete", "skip"):
            with self.subTest(action=action):
                self.records[:] = [fixture_reminder(0), fixture_reminder(1)]
                app_clock.app_today.return_value = TODAY
                reminder_service.app_today.return_value = TODAY
                self.complete.side_effect = self.original_complete
                with patch.object(reminder_service, "load_reminders", return_value=self.records), patch.object(reminder_service, "save_reminders"), patch.object(reminder_service, "skip_review", side_effect=actual_skip):
                    app = self.app()
                    app.selectbox(key="p08_reminder_selection_due").set_value(("r1", 0)).run()
                    app.text_input(key="p08_review_memo_r1").input("別予定の未送信メモ").run()
                    app.selectbox(key="p08_reminder_selection_due").set_value(("r0", 0)).run()
                    app.text_input(key="p08_review_memo_r0").input("この周期だけのメモ")
                    app.number_input(key="p08_review_duration_r0").set_value(20)
                    button = "review_due_r0_normal" if action == "complete" else "skip_due_r0"
                    app.button(key=button).click().run()
                    self.assertFalse(app.exception, str(app.exception))
                    self.assertEqual(self.records[0]["scheduled_date"], "2026-09-18")
                    self.assertNotIn("r0", app.session_state["p08_review_drafts"])
                    self.assertEqual(app.session_state["p08_review_drafts"]["r1"]["memo"], "別予定の未送信メモ")
                    app.radio(key="p08_selected_view").set_value("今後の予定").run()
                    app_clock.app_today.return_value = TODAY + timedelta(days=1)
                    reminder_service.app_today.return_value = TODAY + timedelta(days=1)
                    app.radio(key="p08_selected_view").set_value("今日の復習").run()
                    app.selectbox(key="p08_reminder_selection_due").set_value(("r0", 0)).run()
                    self.assertEqual(app.text_input(key="p08_review_memo_r0").value, "")
                    self.assertEqual(app.number_input(key="p08_review_duration_r0").value, 0)

    def test_missing_record_or_failed_skip_keeps_draft(self):
        for action in ("complete_missing", "skip_missing", "skip_failure"):
            with self.subTest(action=action), patch.object(reminder_service, "skip_review") as skip:
                self.complete.return_value = None
                skip.return_value = None
                skip.side_effect = RuntimeError("fixture-only skip failure") if action == "skip_failure" else None
                app = self.app()
                app.text_input(key="p08_review_memo_r0").input("未保存メモ")
                app.number_input(key="p08_review_duration_r0").set_value(10)
                button = "review_due_r0_normal" if action == "complete_missing" else "skip_due_r0"
                app.button(key=button).click().run()
                if action == "skip_failure":
                    self.assertEqual(len(app.exception), 1)
                    skip.side_effect = None
                    app.run()
                self.assertEqual(app.text_input(key="p08_review_memo_r0").value, "未保存メモ")
                self.assertEqual(app.number_input(key="p08_review_duration_r0").value, 10)
                self.assertEqual(app.session_state["p08_review_drafts"]["r0"]["memo"], "未保存メモ")

    def test_duplicate_ids_are_read_only_even_when_other_duplicate_is_filtered_out(self):
        self.records[:] = [fixture_reminder(0), {**fixture_reminder(0), "title": "同じIDの別レコード"}, fixture_reminder(1)]
        with patch.object(reminder_service, "skip_review") as skip, patch.object(reminder_service, "defer_reminder") as defer, patch.object(reminder_service, "set_schedule_status") as status:
            app = self.app()
            for occurrence in (0, 1):
                app.selectbox(key="p08_reminder_selection_due").set_value(("r0", occurrence)).run()
                self.assertTrue(any("保存先を一意に特定できません" in item.value for item in app.warning))
                self.assertFalse(any(item.label == "今回の復習メモ" for item in app.text_input))
                self.assertFalse(any(item.key and (item.key.startswith("review_due_") or item.key.startswith("skip_due_")) for item in app.button))
            app.selectbox(key="p08_reminder_selection_due").set_value(("r1", 0)).run()
            app.text_input(key="p08_review_memo_r1").input("通常IDの下書き").run()
            self.records[1]["status"] = "done"
            app.selectbox(key="p08_reminder_selection_due").set_value(("r0", 0)).run()
            self.assertFalse(any(item.label == "今回の復習メモ" for item in app.text_input))
            app.radio(key="p08_selected_view").set_value("スケジュール管理").run()
            for occurrence in (0, 1):
                app.selectbox(key="p08_reminder_selection_schedule").set_value(("r0", occurrence)).run()
                self.assertFalse(any(item.label in {"再開", "一時停止", "キャンセル"} for item in app.button))
            app.radio(key="p08_selected_view").set_value("今日の復習").run()
            app.selectbox(key="p08_reminder_selection_due").set_value(("r1", 0)).run()
            self.assertEqual(app.text_input(key="p08_review_memo_r1").value, "通常IDの下書き")
            self.complete.assert_not_called()
            skip.assert_not_called()
            defer.assert_not_called()
            status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
