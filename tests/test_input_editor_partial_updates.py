"""Real server fragment runs of pages 02/04 with synthetic stores only."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from app_core import app_clock, json_store, note_editor, settings_service
from tests import test_input_editor_fragments as input_fixture

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
from smoke_timer_partial_updates import FragmentDriver

FULL_COUNTER = "_qa_input_editor_full_runs"


class EditorPartialUpdateTests(unittest.TestCase):
    def fixture(self, kind):
        helper = input_fixture.EditorFragmentTests("runTest")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        fixture = helper.fixture(kind)
        self.reads = helper.reads
        self.root = fixture.root
        if kind == "study":
            for patcher in (
                patch.object(app_clock, "DEMO_MODE_PATH", self.root / "settings/demo_mode.json"),
                patch.object(settings_service, "USER_SETTINGS_PATH", self.root / "settings/user_settings.json"),
            ):
                patcher.start()
                self.addCleanup(patcher.stop)
        return fixture

    def driver(self, number):
        original = next((PROJECT / "pages").glob(f"{number}_*.py"))
        source = original.read_text(encoding="utf-8-sig")
        marker = "ROOT = Path(__file__).resolve().parents[1]"
        self.assertEqual(source.count(marker), 1)
        source = source.replace(marker, f"ROOT = Path({str(self.root)!r})\nst.session_state[{FULL_COUNTER!r}] = st.session_state.get({FULL_COUNTER!r}, 0) + 1")
        source = source.replace("import prolog_ui.components as ab", """from prolog_ui.components import show_action_error as _show_action_error
class _Decoration:
    show_action_error = staticmethod(_show_action_error)
    def __getattr__(self, name):
        return lambda *args, **kwargs: None
ab = _Decoration()""")
        source = source.replace("from master_ui import render_master_quick_add", "def render_master_quick_add(*args, **kwargs):\n    return False")
        path = self.root / "pages" / original.name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        driver = FragmentDriver(path)
        driver.run()
        return driver

    @staticmethod
    def widget(items, label):
        return next(item for item in items if item.label == label)

    def partial(self, driver, widget):
        self.assertTrue(driver.app.session_state._state._new_widget_state.widget_metadata[widget.id].fragment_id)
        before = driver.app.session_state[FULL_COUNTER]
        self.reads.clear()
        with patch.object(json_store, "save_json_safe", side_effect=AssertionError("Readonly input attempted a write")):
            driver.run(widget)
        self.assertEqual(driver.app.session_state[FULL_COUNTER], before)
        forbidden = ("study_logs/", "daily_plans/", "diary_entries/")
        self.assertFalse(any(path.startswith(forbidden) or path in ("flashcards.json", "reminders.json") for path in self.reads), self.reads)

    def test_study_page_classification_is_partial_with_latest_master_and_no_history_read(self):
        fixture = self.fixture("study")
        fixture.seed([fixture.session()])
        driver = self.driver("02")
        selector = driver.app.selectbox(key="study_log_edit_selected_id")
        selector.set_value(fixture.session_id)
        driver.run(selector)  # Initial selection deliberately reads its safe edit baseline.
        before_json = fixture.live_snapshot()
        project = next(item for item in driver.app.selectbox if item.key and item.key.startswith("study_log_edit_") and item.label == "プロジェクト")
        project.set_value("project_a")
        self.partial(driver, project)
        self.assertEqual(self.reads.count("master_data/master.json"), 1)
        subject = next(item for item in driver.app.selectbox if item.key and item.key.startswith("study_log_edit_") and item.label == "科目")
        self.assertIn("synthetic subject [subject_a]", subject.options)
        master = fixture.read_json("master_data/master.json")
        master["projects"].append({"project_id": "project_b", "name": "external project", "is_active": True})
        fixture.write_json("master_data/master.json", master)
        subject.set_value("subject_a")
        self.partial(driver, subject)
        self.assertIn("external project [project_b]", next(item for item in driver.app.selectbox if item.key and item.key.startswith("study_log_edit_") and item.label == "プロジェクト").options)
        # Only the explicit external fixture edit above changes JSON.
        before_json[str(Path("master_data/master.json"))] = (fixture.root / "master_data/master.json").read_bytes()
        self.assertEqual(fixture.live_snapshot(), before_json)

    def test_note_page_body_and_classification_are_partial_and_keep_sibling_drafts(self):
        fixture = self.fixture("note")
        fixture.put("master_data/master.json", {"projects": [{"project_id": "p1", "name": "first", "is_active": True}],
                    "subjects": [{"subject_id": "s1", "project_id": "p1", "name": "child", "is_active": True}]})
        driver = self.driver("04")
        self.widget(driver.app.text_input, "問題文").set_value("unsubmitted card")
        self.widget(driver.app.text_area, "メモ").set_value("unsubmitted reminder")
        body = driver.app.text_area(key="body_n1")
        body.set_value("unsaved partial preview")
        before_json = fixture.live_bytes()
        self.partial(driver, body)
        self.assertEqual(self.reads.count("notes_data/notebooks.json"), 1)
        self.assertEqual(self.reads.count("master_data/master.json"), 1)
        self.assertFalse(driver.app.session_state["note_parent_rendering"])
        self.assertTrue(any(item.value == "unsaved partial preview" for item in driver.app.markdown))
        project = driver.app.selectbox(key="note_n1_project")
        project.set_value("first [p1]")
        self.partial(driver, project)
        self.assertIn("child [s1]", driver.app.selectbox(key="note_n1_subject").options)
        self.assertEqual(driver.app.text_area(key="body_n1").value, "unsaved partial preview")
        self.assertEqual(self.widget(driver.app.text_input, "問題文").value, "unsubmitted card")
        self.assertEqual(self.widget(driver.app.text_area, "メモ").value, "unsubmitted reminder")
        self.assertEqual(fixture.live_bytes(), before_json)

    def test_note_partial_success_save_refreshes_full_parent_and_preserves_sibling_forms(self):
        fixture = self.fixture("note")
        driver = self.driver("04")
        self.widget(driver.app.text_input, "問題文").set_value("card draft across partial save")
        self.widget(driver.app.text_area, "メモ").set_value("reminder draft across partial save")
        body = driver.app.text_area(key="body_n1")
        body.set_value("saved by fragment")
        self.partial(driver, body)
        save = self.widget(driver.app.button, "保存")
        save.click()
        before = driver.app.session_state[FULL_COUNTER]
        driver.run(save)
        self.assertEqual(driver.app.session_state[FULL_COUNTER], before + 1)
        self.assertFalse(driver.app.session_state["note_parent_rendering"])
        self.assertEqual(fixture.snapshot()["raw_note"]["body"], "saved by fragment")
        self.assertEqual(self.widget(driver.app.text_input, "問題文").value, "card draft across partial save")
        self.assertEqual(self.widget(driver.app.text_area, "メモ").value, "reminder draft across partial save")
        self.assertEqual(driver.app.text_area(key="body_n1").value, "saved by fragment")

    def test_note_external_change_warns_in_partial_and_save_rejection_keeps_draft(self):
        fixture = self.fixture("note")
        driver = self.driver("04")
        body = driver.app.text_area(key="body_n1")
        body.set_value("my draft")
        self.partial(driver, body)
        revision = driver.app.session_state["note_editor_snapshots"]["n1"]["revision"]
        note_editor.save_note_edits(fixture.snapshot(), {"title": "external change"}, root=fixture.root)
        tags = driver.app.text_input(key="tags_n1")
        tags.set_value("unsaved tag")
        self.partial(driver, tags)
        self.assertEqual(driver.app.session_state["note_editor_snapshots"]["n1"]["revision"], revision)
        self.assertTrue(any("編集開始後" in item.value for item in driver.app.warning))
        before_json = fixture.live_bytes()
        save = self.widget(driver.app.button, "保存")
        save.click()
        before = driver.app.session_state[FULL_COUNTER]
        driver.run(save)
        self.assertEqual(driver.app.session_state[FULL_COUNTER], before)
        self.assertTrue(driver.app.error)
        self.assertEqual(driver.app.text_area(key="body_n1").value, "my draft")
        self.assertEqual(fixture.live_bytes(), before_json)


if __name__ == "__main__":
    unittest.main()
