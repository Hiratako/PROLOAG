from copy import deepcopy
from datetime import date
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import Mock, patch

from prolog_ui import roadmap_canvas as canvas


class CanvasMessageTests(unittest.TestCase):
    def action(self, kind="edit_item", **changes):
        value = {"kind": kind, "event_id": "a" * 32, "source_revision": "b" * 64}
        if kind == "edit_milestone":
            value["milestone_id"] = "ms_" + "c" * 32
        elif kind != "show_capacity":
            value["item_id"] = "rm_" + "c" * 32
        return {**value, **changes}

    def test_explicit_supported_actions_keep_revision_and_stable_ids(self):
        for kind in ("edit_item", "allocate_item", "show_capacity", "focus_item", "edit_milestone"):
            with self.subTest(kind=kind):
                action = self.action(kind)
                self.assertEqual(canvas.canvas_action(action), action)
                self.assertIsNot(canvas.canvas_action(action), action)

    def test_invalid_messages_do_not_authorize_any_operation(self):
        for value in (None, [], "edit", {}, self.action(kind=[]), self.action(kind="delete_item"),
                      self.action(event_id="not-an-operation"), self.action(event_id=1),
                      self.action(source_revision={}), self.action(item_id=[]),
                      self.action(item_id="../../settings/master.json"), self.action(item_id="ms_" + "a" * 32),
                      self.action(extra="unapproved"), self.action(milestone_id="ms_" + "a" * 32)):
            with self.subTest(value=value):
                self.assertIsNone(canvas.canvas_action(value))

    def test_missing_file_revision_and_generic_capacity_request_are_valid(self):
        self.assertIsNotNone(canvas.canvas_action(self.action(source_revision=None)))
        self.assertIsNotNone(canvas.canvas_action(self.action("show_capacity")))
        self.assertIsNone(canvas.canvas_action(self.action("show_capacity", item_id=None)))

    def drag(self, **changes):
        return self.action("schedule_change", old_start="2026-11-01", old_end="2026-11-03",
                           start_date="2026-11-03", end_date="2026-11-05", **changes)

    def test_drag_retains_old_dates_for_backend_conflict_detection(self):
        action = self.drag()
        self.assertEqual(canvas.canvas_action(action), action)
        self.assertEqual(canvas.canvas_action(action)["old_end"], "2026-11-03")

    def test_drag_rejects_noncanonical_dates_inversion_and_boundaries(self):
        for name, value in (("start_date", "2026-W45-1"), ("start_date", "20261103"),
                            ("start_date", "2026-11-06"), ("old_start", "2026-11-04"),
                            ("end_date", "2200-01-01"), ("start_date", "1899-12-31"),
                            ("start_date", None), ("end_date", 0)):
            with self.subTest(name=name, value=value):
                action = self.drag()
                action[name] = value
                self.assertIsNone(canvas.canvas_action(action))

    def test_stale_but_well_formed_revision_is_preserved_for_clear_server_error(self):
        action = self.action(source_revision="e" * 64)
        self.assertEqual(canvas.canvas_action(action)["source_revision"], "e" * 64)


class CanvasPayloadTests(unittest.TestCase):
    def item(self, index=0, **changes):
        return {"id": "rm_" + f"{index:032x}", "title": "架空の工程 <script>alert(1)</script>",
                "start_date": "2026-11-01", "end_date": "2026-11-03", "deadline": "",
                "completed": 0, "total": 40, "unit": "問", "planned_minutes": 300,
                "status": "not_started", "is_archived": False, "memo": "原文\n保管",
                "predecessor_ids": [], "baseline_start_date": "2026-11-01", "baseline_end_date": "2026-11-03",
                "project_id": "historic'project__proto__", "subject_id": "subject\"name", "material_id": "",
                "view_state_label": "未着手", "view_actual_label": "未連携", "view_progress_percent": 0,
                "future_private": {"preserve_in_record_not_component": True}, **changes}

    def render(self, items=None, milestones=None, actuals=None, returned=None, **kwargs):
        runner = Mock(return_value={"action": returned})
        with patch.object(canvas.st.components.v2, "component", return_value=runner) as factory:
            result = canvas.render_roadmap_canvas(
                [self.item()] if items is None else items, milestones or [],
                date(2026, 11, 1), date(2026, 11, 30), today=date(2026, 11, 4),
                source_revision="b" * 64, palette={"mode": "dark"}, key="synthetic_canvas",
                actuals=actuals, **kwargs,
            )
        return factory, runner, result

    def test_read_only_transport_preserves_user_text_and_metadata_without_mutation(self):
        values = [self.item()]
        original = deepcopy(values)
        _, runner, _ = self.render(items=values)
        payload = runner.call_args.kwargs["data"]
        self.assertEqual(values, original)
        self.assertEqual(payload["items"][0]["title"], values[0]["title"])
        self.assertEqual(payload["items"][0]["view_actual_label"], "未連携")
        self.assertEqual(payload["items"][0]["view_progress_percent"], 0)
        self.assertEqual(payload["items"][0]["project_id"], "historic'project__proto__")
        self.assertNotIn("future_private", payload["items"][0])
        self.assertNotIn("innerHTML", canvas._JS)

    def test_aggregate_payload_does_not_copy_related_task_bodies_or_import_ledgers(self):
        actuals = {"seconds": {"a": 0}, "counts": {"a": 1}, "task_counts": {"a": 2},
                   "tasks": {"allocation": [{"memo": "Unneeded private task body"}]}}
        milestone = {"id": "ms_" + "a" * 32, "title": "架空模試", "date": "2026-11-30",
                     "project_id": "", "is_archived": False, "memo": "Only needed in server editor",
                     "roadmap_import": {"operation_id": "a" * 32}}
        _, runner, _ = self.render(actuals=actuals, milestones=[milestone])
        payload = runner.call_args.kwargs["data"]
        self.assertNotIn("tasks", payload["actuals"])
        self.assertEqual(payload["actuals"]["counts"], {"a": 1})
        self.assertNotIn("memo", payload["milestones"][0])
        self.assertNotIn("roadmap_import", payload["milestones"][0])

    def test_more_than_import_limit_is_not_silently_dropped(self):
        _, runner, _ = self.render(items=[self.item(index) for index in range(1001)])
        self.assertEqual(len(runner.call_args.kwargs["data"]["items"]), 1001)

    def test_empty_and_milestone_only_payloads_are_supported(self):
        _, runner, _ = self.render(items=[])
        self.assertEqual(runner.call_args.kwargs["data"]["items"], [])
        _, runner, _ = self.render(items=[], milestones=[{"id": "ms_" + "a" * 32, "title": "模試", "date": "2026-11-30"}])
        self.assertEqual(len(runner.call_args.kwargs["data"]["milestones"]), 1)

    def test_pending_saved_date_ghost_and_ack_are_transported(self):
        item = self.item(view_is_draft=True, view_before_start="2026-10-30", view_before_end="2026-11-01")
        _, runner, _ = self.render(items=[item], pending=True, ack_event_id="c" * 32)
        payload = runner.call_args.kwargs["data"]
        self.assertTrue(payload["pending"])
        self.assertEqual(payload["items"][0]["view_before_end"], "2026-11-01")
        self.assertEqual(payload["ack_event_id"], "c" * 32)

    def test_callback_only_returns_validated_actions(self):
        _, _, result = self.render(returned={"kind": "delete_item"})
        self.assertIsNone(result)
        value = CanvasMessageTests().action()
        _, _, result = self.render(returned=value)
        self.assertEqual(result, value)

    def test_js_module_syntax_and_offline_contract(self):
        node = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"
        if not Path(node).is_file():
            self.skipTest("Node is required for a module syntax check")
        result = subprocess.run([node, "--input-type=module", "--check"], input=canvas._JS,
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("fetch(", canvas._JS)
        self.assertNotIn("XMLHttpRequest", canvas._JS)
        self.assertNotIn("https://", canvas._JS)


if __name__ == "__main__":
    unittest.main()
