"""Independent boundaries for untrusted canvas requests and safe UI routing."""
from copy import deepcopy
from datetime import date
import unittest
from unittest.mock import patch
from uuid import uuid4

from app_core import json_store
from app_core import roadmap_service as service
from app_core import roadmap_workspace as workspace
import test_roadmap_service as fixtures


class CanvasEventQualityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RoadmapTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.fixture.save(self.fixture.payload(title="Independent synthetic stage"))
        self.data = service.load_roadmap(self.root)
        self.identifier = self.data["items"][0]["id"]
        self.scope = "independent_"
        self.state = {}
        state_patch = patch.object(workspace.st, "session_state", self.state)
        state_patch.start()
        self.addCleanup(state_patch.stop)

    def event(self, kind="edit_item", **updates):
        value = {
            "kind": kind,
            "event_id": uuid4().hex,
            "source_revision": json_store.observed_json_revision(service.roadmap_path(self.root)),
        }
        if kind != "show_capacity":
            value["item_id"] = self.identifier
        value.update(updates)
        return value

    def handle(self, event):
        return workspace.handle_canvas_event(self.root, self.data, self.scope, event)

    def test_invalid_client_values_are_friendly_errors_without_record_or_state_changes(self):
        examples = [None, [], {}, self.event(kind=[]), self.event(kind={}), self.event(extra="unsupported")]
        examples.extend(self.event(kind=kind, item_id=identifier)
                        for kind in ("edit_item", "allocate_item", "focus_item")
                        for identifier in (None, [], {}, True, "", "missing"))
        examples.extend(self.event(event_id=value) for value in (None, [], 3, "xyz", "a" * 33))
        for identifier in (None, [], {}, True, "", "missing"):
            milestone_event = self.event("edit_milestone", milestone_id=identifier)
            milestone_event.pop("item_id")
            examples.append(milestone_event)
        before = self.fixture.snapshot()
        for event in examples:
            self.state.clear()
            with self.subTest(event=event), self.assertRaises(ValueError):
                self.handle(event)
            self.assertEqual(self.fixture.snapshot(), before)
            self.assertEqual(self.state, {})

    def test_stale_revision_cannot_open_editor_or_replace_a_retained_draft(self):
        self.state["draft"] = {"title": "keep this input"}
        before = deepcopy(self.state)
        records = self.fixture.snapshot()
        with self.assertRaises(json_store.JsonConflictError):
            self.handle(self.event(source_revision="outdated"))
        self.assertEqual(self.state, before)
        self.assertEqual(self.fixture.snapshot(), records)

    def test_valid_editor_request_is_read_only_and_repeated_event_is_not_replayed(self):
        before = self.fixture.snapshot()
        event = self.event()
        self.assertTrue(self.handle(event))
        self.assertEqual(self.state[self.scope + "dialog_request"], {
            "collection": "items", "identifier": self.identifier,
        })
        state = deepcopy(self.state)
        self.assertFalse(self.handle(event))
        self.assertEqual(self.state, state)
        self.assertEqual(self.fixture.snapshot(), before)

    def test_focus_defers_date_and_mode_changes_until_before_widgets_are_rendered(self):
        before = self.fixture.snapshot()
        self.assertTrue(self.handle(self.event("focus_item")))
        self.assertEqual(self.state[self.scope + "jump_anchor"], date(2026, 11, 1))
        self.assertEqual(self.state[self.scope + "mode_request"], "学習工程")
        self.assertNotIn(self.scope + "anchor", self.state)
        self.assertNotIn(self.scope + "mode", self.state)
        self.assertEqual(self.fixture.snapshot(), before)

    def test_allocating_from_canvas_only_routes_to_existing_form(self):
        before = self.fixture.snapshot()
        self.assertTrue(self.handle(self.event("allocate_item")))
        self.assertEqual(self.state[self.scope + "allocation_item"], self.identifier)
        self.assertEqual(self.state[self.scope + "mode_request"], "日別タスクとの連携")
        self.assertEqual(self.fixture.snapshot(), before)

    def test_archived_focus_outside_whole_plan_queues_a_bounded_view_without_touching_widget_state(self):
        self.fixture.save(self.fixture.payload(
            title="Archived outside active period", start_date="2025-01-01",
            end_date="2025-01-03", is_archived=True,
        ))
        self.data = service.load_roadmap(self.root)
        archived = next(item for item in self.data["items"] if item["is_archived"])
        self.state[self.scope + "view"] = "全体"
        self.state[self.scope + "anchor"] = date(2026, 11, 1)
        self.state["unrelated_retained_draft"] = {"memo": "keep input"}
        before = self.fixture.snapshot()
        self.assertTrue(self.handle(self.event("focus_item", item_id=archived["id"])))
        self.assertEqual(self.state[self.scope + "view"], "全体")
        self.assertEqual(self.state[self.scope + "anchor"], date(2026, 11, 1))
        self.assertEqual(self.state[self.scope + "jump_view"], "4週間")
        self.assertEqual(self.state[self.scope + "jump_anchor"], date(2025, 1, 1))
        self.assertEqual(self.state["unrelated_retained_draft"], {"memo": "keep input"})
        self.assertEqual(self.fixture.snapshot(), before)

    def test_pending_plan_is_not_replaced_by_another_canvas_action(self):
        from app_core.roadmap_planning import schedule_preview

        for key in ("schedule_pending", "distribution_pending"):
            self.state.clear()
            self.state[self.scope + key] = {"operation_id": "a" * 32, "keep": [1, 2]}
            if key == "schedule_pending":
                self.state[self.scope + key]["preview"] = schedule_preview(self.data, {
                    self.identifier: {"start_date": "2026-11-02", "end_date": "2026-11-11"},
                })
            for kind in ("edit_item", "allocate_item", "show_capacity"):
                before = deepcopy(self.state)
                with self.subTest(key=key, kind=kind), self.assertRaises(ValueError):
                    self.handle(self.event(kind))
                self.assertEqual(self.state, before)
            self.assertTrue(self.handle(self.event("focus_item")))
            self.assertEqual(self.state[self.scope + key]["keep"], [1, 2])

    def test_focus_pending_schedule_follows_preview_dates_and_preserves_unsaved_plan(self):
        from app_core.roadmap_planning import schedule_preview

        preview = schedule_preview(self.data, {
            self.identifier: {"start_date": "2027-01-10", "end_date": "2027-01-19"},
        })
        pending = {"preview": preview, "operation_id": "a" * 32, "revision": self.event()["source_revision"]}
        self.state[self.scope + "schedule_pending"] = deepcopy(pending)
        self.state[self.scope + "view"] = "全体"
        self.state[self.scope + "anchor"] = date(2026, 11, 4)
        self.state["unrelated_retained_draft"] = {"title": "Keep this input"}
        before = self.fixture.snapshot()
        event = self.event("focus_item")
        self.assertTrue(self.handle(event))
        self.assertEqual(self.state[self.scope + "jump_anchor"], date(2027, 1, 10))
        self.assertEqual(self.state[self.scope + "jump_view"], "4週間")
        self.assertEqual(self.state[self.scope + "view"], "全体")
        self.assertEqual(self.state[self.scope + "anchor"], date(2026, 11, 4))
        self.assertEqual(self.state[self.scope + "schedule_pending"], pending)
        self.assertEqual(self.state["unrelated_retained_draft"], {"title": "Keep this input"})
        self.assertEqual(self.fixture.snapshot(), before)
        after = deepcopy(self.state)
        self.assertFalse(self.handle(event))
        self.assertEqual(self.state, after)

    def test_focus_stale_pending_schedule_follows_the_committed_display_without_discarding_input(self):
        from app_core.roadmap_planning import schedule_preview

        preview = schedule_preview(self.data, {
            self.identifier: {"start_date": "2027-01-10", "end_date": "2027-01-19"},
        })
        preview["base_hash"] = "stale-preview-content"
        pending = {"preview": preview, "operation_id": "a" * 32}
        self.state[self.scope + "schedule_pending"] = deepcopy(pending)
        self.state[self.scope + "view"] = "全体"
        before = self.fixture.snapshot()
        self.assertTrue(self.handle(self.event("focus_item")))
        self.assertEqual(self.state[self.scope + "jump_anchor"], date(2026, 11, 1))
        self.assertNotIn(self.scope + "jump_view", self.state)
        self.assertEqual(self.state[self.scope + "schedule_pending"], pending)
        self.assertEqual(self.fixture.snapshot(), before)

    def test_stale_focus_request_cannot_navigate_or_replace_a_pending_schedule(self):
        from app_core.roadmap_planning import schedule_preview

        self.state[self.scope + "schedule_pending"] = {
            "preview": schedule_preview(self.data, {
                self.identifier: {"start_date": "2027-01-10", "end_date": "2027-01-19"},
            }),
            "operation_id": "a" * 32,
        }
        before = deepcopy(self.state)
        records = self.fixture.snapshot()
        with self.assertRaises(json_store.JsonConflictError):
            self.handle(self.event("focus_item", source_revision="stale-request"))
        self.assertEqual(self.state, before)
        self.assertEqual(self.fixture.snapshot(), records)

    def test_archived_stage_cannot_be_allocated_by_a_forged_request(self):
        self.data["items"][0]["is_archived"] = True
        before = self.fixture.snapshot()
        with self.assertRaises(ValueError):
            self.handle(self.event("allocate_item"))
        self.assertEqual(self.fixture.snapshot(), before)
        self.assertEqual(self.state, {})


class RoadmapWorkspaceQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit import config

        config.get_config_options(options_from_flags={
            "global.developmentMode": False,
            "browser.gatherUsageStats": False,
        })

    def setUp(self):
        self.fixture = fixtures.RoadmapTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        link_patch = patch.object(workspace.st, "page_link", return_value=None)
        link_patch.start()
        self.addCleanup(link_patch.stop)

    def app(self, setup):
        from streamlit.testing.v1 import AppTest

        return AppTest.from_string(
            "from pathlib import Path\n"
            "from hashlib import sha256\n"
            "import streamlit as st\n"
            "from app_core.roadmap_ui import render_roadmap\n"
            f"root = Path({str(self.root)!r})\n"
            "scope = 'roadmap_' + sha256(str(root.resolve()).encode()).hexdigest()[:10] + '_'\n"
            + setup + "\nrender_roadmap(root)",
            default_timeout=60,
        ).run()

    def test_pending_schedule_keeps_capacity_and_bulk_edit_controls_inaccessible(self):
        self.fixture.save(self.fixture.payload())
        before = self.fixture.snapshot()
        for mode in ("学習可能時間・配分", "日程の調整", "AI計画の取り込み", "日別タスクとの連携"):
            with self.subTest(mode=mode):
                app = self.app(f"""
from app_core.roadmap_service import load_roadmap
from app_core.roadmap_planning import shift_changes
from app_core.roadmap_planning_ui import start_schedule_preview
if 'qa_primed' not in st.session_state:
    data = load_roadmap(root)
    changes = shift_changes(data, [data['items'][0]['id']], 1, include_descendants=True)
    start_schedule_preview(root, data, scope, changes)
    st.session_state[scope + 'pending_dialog_hidden'] = st.session_state[scope + 'schedule_pending']['operation_id']
    st.session_state[scope + 'mode_memory'] = {mode!r}
    st.session_state['qa_primed'] = True
""")
                self.assertFalse(app.exception)
                radio = next(field for field in app.radio if field.label == "管理する内容")
                self.assertTrue(radio.disabled)
                self.assertTrue(any(field.label == "表示範囲" for field in app.selectbox))
                labels = {button.label for button in app.button}
                self.assertIn("変更内容を確認する", labels)
                for label in ("学習可能時間を保存", "一括移動のプレビュー", "配分案を作成", "この計画を追加する"):
                    self.assertNotIn(label, labels)
                for label in ("工程を追加", "試験・節目を追加", "AI計画を取り込む", "一括調整"):
                    self.assertTrue(next(button for button in app.button if button.label == label).disabled)
                self.assertEqual(before, self.fixture.snapshot())

    def test_367_day_workload_view_does_not_read_daily_plans_or_raise(self):
        self.fixture.save(self.fixture.payload(start_date="2026-01-01", end_date="2027-01-02"))
        before = self.fixture.snapshot()
        with patch.object(workspace, "build_capacity_strip", side_effect=AssertionError("Unexpected daily-plan read")) as load:
            app = self.app("""
st.session_state[scope + 'view'] = '全体'
st.session_state[scope + 'show_load'] = True
""")
        self.assertFalse(app.exception)
        self.assertFalse(app.error)
        load.assert_not_called()
        self.assertTrue(any("日別負荷は1年以内" in message.value for message in app.info))
        self.assertEqual(before, self.fixture.snapshot())

    def test_today_from_whole_plan_switches_to_four_weeks_before_date_widgets(self):
        self.fixture.save(self.fixture.payload())
        before = self.fixture.snapshot()
        with patch.object(workspace, "app_today", return_value=date(2026, 11, 4)):
            app = self.app("""
if 'qa_primed' not in st.session_state:
    st.session_state[scope + 'view'] = '全体'
    st.session_state[scope + 'anchor'] = __import__('datetime').date(2025, 1, 1)
    st.session_state['qa_primed'] = True
""")
            self.assertEqual(next(field for field in app.selectbox if field.label == "表示範囲").value, "全体")
            next(button for button in app.button if button.label == "今日").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(next(field for field in app.selectbox if field.label == "表示範囲").value, "4週間")
        self.assertEqual(next(field for field in app.date_input if field.label == "表示の基準日").value, date(2026, 11, 4))
        self.assertEqual(before, self.fixture.snapshot())

    def test_cancel_confirmation_then_keep_close_retains_payload_and_original_revision(self):
        from streamlit.testing.v1 import AppTest

        self.fixture.save(self.fixture.payload())
        original = service.load_roadmap(self.root)["items"][0]
        revision = json_store.observed_json_revision(service.roadmap_path(self.root))
        before = self.fixture.snapshot()
        scope = "draft_quality_"
        key = scope + "items_" + original["id"]
        pending = self.fixture.payload(title="Unsaved draft", memo="Line one\n二行目", completed=7)
        pending["predecessor_ids"] = []
        app = AppTest.from_string(f"""
from copy import deepcopy
from pathlib import Path
import streamlit as st
from app_core.form_snapshot import form_snapshot
from app_core.master_snapshot import MasterSnapshot
from app_core.roadmap_service import load_roadmap, roadmap_path
from app_core.roadmap_ui import _editor
root = Path({str(self.root)!r})
data = load_roadmap(root)
key = {key!r}
if 'qa_primed' not in st.session_state:
    form_snapshot(st.session_state, key, [roadmap_path(root)], data)
    st.session_state[key + '_pending'] = {pending!r}
    st.session_state[key + '_retained_draft'] = deepcopy(st.session_state[key + '_pending'])
    st.session_state['qa_primed'] = True
def close():
    st.session_state['qa_closed'] = True
if not st.session_state.get('qa_closed'):
    _editor(root, data, MasterSnapshot.load(root), {scope!r}, 'items', identifier={original['id']!r}, on_close=close)
else:
    st.write('Editor closed with draft retained')
""", default_timeout=60).run()
        self.assertFalse(app.exception)
        operation_id = app.session_state["form_baseline:" + key]["operation_id"]
        next(button for button in app.button if button.label == "確認を取り消す").click().run()
        self.assertFalse(app.exception)
        self.assertNotIn(key + "_pending", app.session_state.filtered_state)
        self.assertEqual(next(field for field in app.text_input if field.label == "工程名").value, pending["title"])
        self.assertEqual(next(field for field in app.text_area if field.label == "メモ").value, pending["memo"])
        next(button for button in app.button if button.label == "入力を保持して閉じる").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.session_state["qa_closed"])
        self.assertEqual(app.session_state[key + "_retained_draft"], pending)
        self.assertEqual(app.session_state[key][service.roadmap_path(self.root)], revision)
        self.assertEqual(app.session_state["form_baseline:" + key]["operation_id"], operation_id)
        self.assertEqual(before, self.fixture.snapshot())


if __name__ == "__main__":
    unittest.main()
