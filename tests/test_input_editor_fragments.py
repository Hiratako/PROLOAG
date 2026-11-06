from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from streamlit import config
from streamlit.testing.v1 import AppTest

from app_core import json_store, note_editor
from tests import test_note_editor_safety as note_fixture
from tests import test_study_log_editor as study_fixture


class EditorFragmentTests(unittest.TestCase):
    """Execute real editor bodies with synthetic stores and no parent history UI.

    AppTest runs the driver again for widgets. Browser checks cover Streamlit's
    partial execution; these checks verify the fragment's reads and safety.
    """

    def setUp(self):
        config.get_config_options(options_from_flags={"global.developmentMode": False, "browser.gatherUsageStats": False})
        self.reads = []

    def fixture(self, kind):
        cls = note_fixture.NoteEditorSafetyTests if kind == "note" else study_fixture.StudyLogEditorTests
        fixture = cls("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        original = json_store._read

        def observed_read(path):
            resolved = Path(path).resolve()
            self.assertTrue(resolved.is_relative_to(fixture.root.resolve()), f"Out-of-fixture JSON read: {path}")
            self.reads.append(resolved.relative_to(fixture.root.resolve()).as_posix())
            return original(path)

        patcher = patch.object(json_store, "_read", side_effect=observed_read)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fixture

    @staticmethod
    def button(app, label):
        return next(item for item in app.button if item.label == label)

    @staticmethod
    def widget(items, label):
        return next(item for item in items if item.label == label)

    def note_fragment_app(self, fixture):
        source = (Path(__file__).resolve().parents[1] / "pages/04_ノート・知識ベース.py").read_text(encoding="utf-8-sig")
        source = source[:source.index("st.set_page_config(")]
        source = source.replace("ROOT = Path(__file__).resolve().parents[1]", f"ROOT = Path({str(fixture.root)!r})")
        source = source.replace("import prolog_ui.components as ab", """from prolog_ui.components import show_action_error as _show_action_error
class _Decoration:
    show_action_error = staticmethod(_show_action_error)
    def __getattr__(self, name):
        return lambda *args, **kwargs: None
ab = _Decoration()""")
        source = source.replace("from master_ui import render_master_quick_add", "def render_master_quick_add(*args, **kwargs):\n    return False")
        source += """
st.set_page_config(layout="wide")
st.session_state.note_page_id = "n1"
st.session_state["note_parent_rendering"] = True
try:
    render_note_editor("n1")
finally:
    st.session_state["note_parent_rendering"] = False
if st.session_state.pop("note_page_refresh_requested", False):
    st.rerun()
"""
        return AppTest.from_string(source, default_timeout=30)

    def study_fragment_app(self, fixture):
        return AppTest.from_string(f"""
from pathlib import Path
import streamlit as st
from app_core.study_log_editor_ui import render_study_log_management
st.set_page_config(layout="wide")
render_study_log_management({[fixture.session()]!r}, root=Path({str(fixture.root)!r}), fallback_policy={fixture.policy!r})
""", default_timeout=30)

    def assert_no_history_reads(self):
        forbidden = ("study_logs/", "daily_plans/", "diaries/")
        self.assertFalse(any(path.startswith(forbidden) or path in ("flashcards.json", "reminders.json") for path in self.reads), self.reads)

    def test_note_input_updates_preview_without_loading_related_history_and_uses_fresh_master(self):
        fixture = self.fixture("note")
        fixture.put("master_data/master.json", {"projects": [{"project_id": "p1", "name": "first", "is_active": True}]})
        app = self.note_fragment_app(fixture).run()
        self.assertFalse(app.exception)
        before = fixture.live_bytes()
        self.reads.clear()
        app.text_area(key="body_n1").set_value("unsaved preview text").run()
        self.assertFalse(app.exception)
        self.assertTrue(any(item.value == "unsaved preview text" for item in app.markdown))
        self.assertEqual(fixture.live_bytes(), before)
        self.assert_no_history_reads()
        self.assertEqual(self.reads.count("master_data/master.json"), 1)
        fixture.put("master_data/master.json", {"projects": [{"project_id": "p2", "name": "newly added", "is_active": True}]})
        self.reads.clear()
        app.text_input(key="title_n1").set_value("unsaved title").run()
        self.assertIn("newly added [p2]", app.selectbox(key="note_n1_project").options)
        self.assertEqual(app.text_area(key="body_n1").value, "unsaved preview text")
        self.assert_no_history_reads()

    def test_note_fragment_reread_keeps_edit_baseline_and_rejects_external_change(self):
        fixture = self.fixture("note")
        app = self.note_fragment_app(fixture).run()
        app.text_area(key="body_n1").set_value("my unsaved draft").run()
        revision = app.session_state["note_editor_snapshots"]["n1"]["revision"]
        note_editor.save_note_edits(fixture.snapshot(), {"title": "external title"}, root=fixture.root)
        app.text_input(key="tags_n1").set_value("unsaved tag").run()
        self.assertTrue(any("編集開始後" in item.value for item in app.warning))
        self.assertEqual(app.session_state["note_editor_snapshots"]["n1"]["revision"], revision)
        before = fixture.live_bytes()
        for _ in range(2):
            self.button(app, "保存").click().run()
            self.assertFalse(app.exception)
            self.assertTrue(app.error)
            self.assertEqual(app.text_area(key="body_n1").value, "my unsaved draft")
            self.assertEqual(fixture.live_bytes(), before)

    def test_note_sibling_creation_forms_keep_drafts_across_editor_save(self):
        fixture = self.fixture("note")
        app = fixture.page_app().run()
        self.widget(app.text_input, "問題文").set_value("unsubmitted card question")
        self.widget(app.text_area, "解答").set_value("unsubmitted card answer")
        self.widget(app.text_area, "メモ").set_value("unsubmitted reminder memo")
        app.text_area(key="body_n1").set_value("saved body while sibling drafts remain").run()
        self.button(app, "保存").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(self.widget(app.text_input, "問題文").value, "unsubmitted card question")
        self.assertEqual(self.widget(app.text_area, "解答").value, "unsubmitted card answer")
        self.assertEqual(self.widget(app.text_area, "メモ").value, "unsubmitted reminder memo")
        self.button(app, "単語カードを保存").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(fixture.get("flashcards.json")[-1]["question"], "unsubmitted card question")
        self.assertEqual(self.widget(app.text_area, "メモ").value, "unsubmitted reminder memo")

    def test_study_classification_updates_dependents_and_master_without_rereading_history(self):
        fixture = self.fixture("study")
        fixture.seed([fixture.session()])
        app = self.study_fragment_app(fixture).run()
        app.selectbox(key="study_log_edit_selected_id").set_value(fixture.session_id).run()
        self.assertFalse(app.exception)
        before = fixture.live_snapshot()
        self.reads.clear()
        self.widget(app.selectbox, "プロジェクト").set_value("project_a").run()
        self.assertFalse(app.exception)
        self.assertIn("synthetic subject [subject_a]", self.widget(app.selectbox, "科目").options)
        self.assertEqual(fixture.live_snapshot(), before)
        self.assert_no_history_reads()
        self.assertEqual(self.reads.count("master_data/master.json"), 1)
        master = fixture.read_json("master_data/master.json")
        master["projects"].append({"project_id": "project_b", "name": "new project", "is_active": True})
        fixture.write_json("master_data/master.json", master)
        self.widget(app.selectbox, "科目").set_value("subject_a").run()
        self.assertIn("new project [project_b]", self.widget(app.selectbox, "プロジェクト").options)
        self.assertIn("synthetic material [material_a]", self.widget(app.selectbox, "教材").options)

    def test_study_successful_save_updates_classification_and_memo_and_clears_editor(self):
        fixture = self.fixture("study")
        fixture.seed([fixture.session()])
        app = self.study_fragment_app(fixture).run()
        app.selectbox(key="study_log_edit_selected_id").set_value(fixture.session_id).run()
        self.widget(app.selectbox, "プロジェクト").set_value("project_a").run()
        self.widget(app.selectbox, "科目").set_value("subject_a").run()
        self.widget(app.text_area, "勉強内容").set_value("saved with classification")
        self.button(app, "変更内容を確認").click().run()
        next(item for item in app.checkbox if "確認" in item.label).check().run()
        self.button(app, "変更を保存する").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox(key="study_log_edit_selected_id").value, "")
        saved = fixture.row()
        self.assertEqual(saved["memo"], "saved with classification")
        self.assertEqual(saved["project_id"], "project_a")
        self.assertEqual(saved["subject_id"], "subject_a")
        self.assertEqual(saved["custom_data"], fixture.session()["custom_data"])
        self.assertEqual(saved["created_at"], fixture.original_created_at)

    def test_study_cas_rejects_stale_preview(self):
        fixture = self.fixture("study")
        fixture.seed([fixture.session()])
        app = self.study_fragment_app(fixture).run()
        app.selectbox(key="study_log_edit_selected_id").set_value(fixture.session_id).run()
        self.widget(app.text_area, "勉強内容").set_value("my edit")
        self.button(app, "変更内容を確認").click().run()
        self.assertFalse(app.exception)
        session = fixture.row()
        session["memo"] = "external edit"
        fixture.seed([session])
        before = fixture.live_snapshot()
        confirmation = next(item for item in app.checkbox if "確認" in item.label)
        confirmation.check().run()
        self.button(app, "変更を保存する").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.error)
        self.assertEqual(fixture.live_snapshot(), before)
        self.assertEqual(app.session_state["study_log_editor_state"]["plan"]["after"]["memo"], "my edit")
        self.assertEqual(fixture.row()["custom_data"], fixture.session()["custom_data"])


if __name__ == "__main__":
    unittest.main()
