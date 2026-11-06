from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import unittest

from web_demo.roadmap import READONLY_CANVAS_JS, SCOPE, consume_focus_event


class ReadonlyRoadmapTests(unittest.TestCase):
    def setUp(self):
        self.item_id = "rm_" + "a" * 32
        self.revision = "b" * 64
        self.data = {"items": [{"id": self.item_id, "start_date": "2026-11-01", "is_archived": False}]}
        self.event = {"kind": "focus_item", "event_id": "c" * 32,
                      "source_revision": self.revision, "item_id": self.item_id}

    def test_focus_changes_only_session_navigation_once(self):
        state = {}
        before = deepcopy(self.data)
        self.assertTrue(consume_focus_event(self.event, self.data, self.revision, state))
        self.assertEqual(state[SCOPE + "focus"], {"id": self.item_id, "start": "2026-11-01"})
        after = deepcopy(state)
        self.assertFalse(consume_focus_event(self.event, self.data, self.revision, state))
        self.assertEqual(state, after)
        self.assertEqual(self.data, before)

    def test_all_mutation_messages_are_ignored(self):
        for kind in ("edit_item", "allocate_item", "show_capacity", "schedule_change", "edit_milestone"):
            with self.subTest(kind=kind):
                state = {}
                self.assertFalse(consume_focus_event({**self.event, "kind": kind}, self.data, self.revision, state))
                self.assertEqual(state, {})

    def test_bad_revision_and_unknown_or_archived_item_are_ignored(self):
        cases = [({**self.event, "source_revision": "d" * 64}, self.data),
                 ({**self.event, "item_id": "rm_" + "e" * 32}, self.data),
                 (self.event, {"items": [{**self.data["items"][0], "is_archived": True}]}),
                 ({**self.event, "extra": "untrusted"}, self.data),
                 ({key: value for key, value in self.event.items() if key != "source_revision"}, self.data),
                 (None, self.data)]
        for event, data in cases:
            state = {}
            self.assertFalse(consume_focus_event(event, data, self.revision, state))
            self.assertEqual(state, {})

    def test_canvas_has_no_editing_controls_or_drag_editing(self):
        self.assertNotIn("日程を編集", READONLY_CANVAS_JS)
        self.assertNotIn("工程を編集", READONLY_CANVAS_JS)
        self.assertNotIn("日別タスクへ割当", READONLY_CANVAS_JS)
        self.assertNotIn("負荷・配分を開く", READONLY_CANVAS_JS)
        self.assertNotIn("label+'を編集'", READONLY_CANVAS_JS)
        self.assertIn("canEdit=false;", READONLY_CANVAS_JS)
        self.assertIn("state.editable=false;state.externalEditable=false;", READONLY_CANVAS_JS)
        self.assertIn("if(kind!=='focus_item'||state.clientPending)return;", READONLY_CANVAS_JS)

    def test_period_and_workload_controls_render_without_writing_dataset(self):
        from streamlit import config as streamlit_config
        from streamlit.testing.v1 import AppTest
        streamlit_config.get_config_options(force_reparse=True, options_from_flags={"global.developmentMode": False})
        root = Path(__file__).resolve().parents[1]
        folders = ("settings", "daily_plans", "study_logs", "master_data")
        def snapshot():
            return {str(path.relative_to(root)): sha256(path.read_bytes()).hexdigest()
                    for name in folders for path in (root / name).rglob("*") if path.is_file()}
        before = snapshot()
        app = AppTest.from_string(
            "from web_demo.roadmap import render_roadmap_demo\nrender_roadmap_demo()"
        ).run(timeout=30)
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.selectbox(key=SCOPE + "view").value, "全体")
        self.assertEqual([metric.value for metric in app.metric], ["9件", "3件", "3件", "2件"])
        app.selectbox(key=SCOPE + "view").set_value("週間").run(timeout=30)
        self.assertEqual(len(app.exception), 0)
        app.checkbox(key=SCOPE + "show_workload").check().run(timeout=30)
        self.assertEqual(len(app.exception), 0)
        self.assertEqual({button.label for button in app.button}, {"前の期間", "基準日に戻る", "次の期間"})
        anchor = app.date_input(key=SCOPE + "anchor").value
        app.button(key=SCOPE + "next").click().run(timeout=30)
        self.assertEqual((app.date_input(key=SCOPE + "anchor").value - anchor).days, 7)
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(snapshot(), before)


if __name__ == "__main__":
    unittest.main()
