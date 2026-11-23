from datetime import date
import locale
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from streamlit.testing.v1 import AppTest
from streamlit.testing.v1.element_tree import get_widget_state
from streamlit.proto.WidgetStates_pb2 import WidgetStates

from app_core.rough_study_drafts import load_rough_draft
from app_core.rough_study_service import load_rough_study_records, save_rough_study_batch
from app_core.rough_study_ui import _date_range_label, _new_editor, _records


class RoughStudyWeekConflictTests(unittest.TestCase):
    week = "week:2026-11-23"

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def open_week(self, *, labels=None, subjects=None):
        app = AppTest.from_string(
            f"""
from datetime import date
from pathlib import Path
from unittest.mock import patch
from app_core import rough_study_ui

with patch.object(rough_study_ui, "app_today", return_value=date(2026, 11, 23)):
    rough_study_ui._render_week(Path({str(self.root)!r}), {labels or {}!r}, {subjects or {}!r})
""",
            default_timeout=20,
        ).run()
        self.assertFalse(app.exception)
        return app

    def memo_inputs(self, app):
        return [widget for widget in app.text_input
                if widget.label == "勉強した内容" and widget.key in app.session_state]

    def run_week(self, app):
        # A rerun may leave orphaned widgets in AppTest's delta tree after
        # confirmation replaces the form. The browser sends live state only.
        states = WidgetStates()
        sent = set()
        for node in app._tree:
            try:
                state = get_widget_state(node)
            except KeyError:
                continue
            if state is not None and state.id not in sent:
                states.widgets.add().CopyFrom(state)
                sent.add(state.id)
        app._run(states)
        self.assertFalse(app.exception)

    def enter_memo(self, app, day_index, memo):
        self.memo_inputs(app)[day_index].set_value(memo)
        self.run_week(app)

    def click(self, app, label):
        next(button for button in reversed(app.button)
             if button.label == label and button.key in app.session_state).click()
        self.run_week(app)

    def saved_memos(self):
        saved = load_rough_draft(self.root, self.week)
        return [row["memo"] for row in saved["rows"][:3]]

    def test_date_range_label_supports_a_non_japanese_locale(self):
        original_locale = locale.setlocale(locale.LC_CTYPE)
        self.addCleanup(locale.setlocale, locale.LC_CTYPE, original_locale)
        locale.setlocale(locale.LC_CTYPE, "C")
        self.assertEqual(
            _date_range_label(date(2026, 11, 23), date(2026, 11, 29)),
            "2026年11/23（月） 〜 11/29（日）",
        )

    def test_cleared_copied_rows_do_not_block_review_and_save(self):
        save_rough_study_batch(
            self.root,
            kind="week",
            rows=[
                {"study_date": "2026-11-16", "subject_id": "english", "project_id": "project",
                 "memo": "月曜日の元の内容"},
                {"study_date": "2026-11-18", "project_id": "project", "memo": "水曜日の元の内容"},
                {"study_date": "2026-11-19", "subject_id": "english", "project_id": "project",
                 "material_id": "material", "memo": "木曜日の元の内容"},
            ],
            operation_id=uuid4().hex,
        )
        app = self.open_week(labels={"english": "英語"}, subjects={"english": {"project_id": "project"}})
        self.click(app, "前週の科目・内容を使う")

        subjects = [widget for widget in app.selectbox
                    if widget.label == "科目" and widget.key in app.session_state]
        self.assertEqual(subjects[0].value, "english")
        subjects[0].set_value("")
        memos = self.memo_inputs(app)
        memos[0].set_value("")
        memos[1].set_value("火曜日に入力した内容")
        memos[2].set_value("")
        memos[3].set_value("")
        self.click(app, "保存内容を確認")

        self.assertEqual([error.value for error in app.error], [])
        self.click(app, "保存する")
        new_records = [row for row in load_rough_study_records(self.root) if row["study_date"] >= "2026-11-23"]
        self.assertCountEqual(
            [(row["study_date"], row["subject_id"], row["project_id"], row["material_id"], row["memo"])
             for row in new_records],
            [("2026-11-24", "", "", "", "火曜日に入力した内容"),
             ("2026-11-26", "english", "project", "material", "")],
        )

    def test_existing_classification_only_record_still_counts_when_editing(self):
        batch = save_rough_study_batch(
            self.root, kind="week", rows=[{"study_date": "2026-11-23", "project_id": "project"}],
            operation_id=uuid4().hex,
        )
        draft = _new_editor(self.root, batch, focus=None)

        records, errors = _records(draft, {})

        self.assertEqual(errors, [])
        self.assertEqual(records, batch["rows"])

    def test_edits_to_different_days_survive_both_autosaves(self):
        first, second = self.open_week(), self.open_week()
        self.enter_memo(first, 0, "月曜日の入力")
        self.enter_memo(second, 1, "火曜日の入力")

        expected = ["月曜日の入力", "火曜日の入力", ""]
        self.assertEqual(self.saved_memos(), expected)
        self.assertEqual([widget.value for widget in self.memo_inputs(second)[:3]], expected)
        self.assertEqual([widget.value for widget in self.memo_inputs(self.open_week())[:3]], expected)
        self.assertEqual(load_rough_study_records(self.root), [])

    def test_merged_autosave_preserves_the_same_event_week_navigation(self):
        first, second = self.open_week(), self.open_week()
        self.enter_memo(first, 0, "月曜日")
        self.memo_inputs(second)[1].set_value("火曜日")
        self.click(second, "次の週")

        self.assertEqual(self.saved_memos(), ["月曜日", "火曜日", ""])
        self.assertEqual([widget.value for widget in self.memo_inputs(second)[:2]], ["", ""])
        self.click(second, "前の週")
        self.assertEqual([widget.value for widget in self.memo_inputs(second)[:2]], ["月曜日", "火曜日"])

    def test_same_day_conflict_keeps_the_input_and_checks_the_choice_again(self):
        first, second = self.open_week(), self.open_week()
        self.enter_memo(first, 0, "月曜日 A")
        self.enter_memo(second, 0, "月曜日 B")

        self.assertIn("別のタブでも変更", second.warning[0].value)
        self.assertEqual(self.memo_inputs(second)[0].value, "月曜日 B")
        self.assertEqual(self.saved_memos(), ["月曜日 A", "", ""])

        self.enter_memo(first, 2, "水曜日 A")
        self.click(second, "このタブの入力を反映する")
        self.assertEqual(self.saved_memos(), ["月曜日 A", "", "水曜日 A"])
        self.assertEqual(self.memo_inputs(second)[0].value, "月曜日 B")

        self.click(second, "このタブの入力を反映する")
        self.assertEqual(self.saved_memos(), ["月曜日 B", "", "水曜日 A"])
        self.assertFalse(any("別のタブでも変更" in item.value for item in second.warning))

    def test_stale_confirmation_requires_review_before_saving(self):
        first, second = self.open_week(), self.open_week()
        self.enter_memo(first, 0, "月曜日の入力")
        self.click(first, "保存内容を確認")
        self.enter_memo(second, 1, "火曜日の入力")
        self.click(first, "保存する")

        self.assertEqual(load_rough_study_records(self.root), [])
        self.assertEqual(self.saved_memos(), ["月曜日の入力", "火曜日の入力", ""])
        self.click(first, "保存内容を確認")
        self.click(first, "保存する")
        self.assertCountEqual(
            [row["memo"] for row in load_rough_study_records(self.root)],
            ["月曜日の入力", "火曜日の入力"],
        )
        self.assertIsNone(load_rough_draft(self.root, self.week))

    def test_other_tab_cannot_recreate_an_already_confirmed_draft(self):
        first = self.open_week()
        self.enter_memo(first, 0, "確定する月曜日")
        second = self.open_week()
        self.click(first, "保存内容を確認")
        self.click(first, "保存する")
        self.enter_memo(second, 1, "火曜日の未保存入力")

        self.assertIn("確定または削除", second.warning[0].value)
        self.assertEqual(self.memo_inputs(second)[1].value, "火曜日の未保存入力")
        self.assertIsNone(load_rough_draft(self.root, self.week))

        self.click(second, "保存済みの下書きを読み込む")
        self.assertEqual([widget.value for widget in self.memo_inputs(second)[:2]], ["", ""])
        self.assertEqual([row["memo"] for row in load_rough_study_records(self.root)], ["確定する月曜日"])

    def test_only_locally_changed_rows_are_recovered_after_an_external_commit(self):
        first = self.open_week()
        self.enter_memo(first, 0, "確定する月曜日")
        second = self.open_week()
        self.click(first, "保存内容を確認")
        self.click(first, "保存する")
        self.enter_memo(second, 1, "新しい火曜日")

        self.click(second, "このタブで変更した行を下書きに戻す")
        self.assertEqual(self.saved_memos(), ["", "新しい火曜日", ""])
        self.assertEqual([row["memo"] for row in load_rough_study_records(self.root)], ["確定する月曜日"])
