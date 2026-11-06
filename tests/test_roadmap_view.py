from collections import Counter
from copy import deepcopy
from datetime import date
from pathlib import Path
import unittest
from unittest.mock import patch

from app_core import json_store
from app_core import roadmap_service as service
from app_core import roadmap_view as view
from app_core.master_snapshot import MasterSnapshot
import test_roadmap_service as fixtures


TODAY = date(2026, 11, 4)


def item(identifier=1, **updates):
    return {
        **fixtures.RoadmapTests.payload(None),
        "id": "rm_" + f"{identifier:032x}",
        "baseline_start_date": "2026-11-01",
        "baseline_end_date": "2026-11-10",
        "schedule_history": [],
        "predecessor_ids": [],
        "is_archived": False,
        **updates,
    }


def document(*rows, **updates):
    return {**service.empty_roadmap(), "items": list(rows), **updates}


class PeriodTests(unittest.TestCase):
    def test_week_four_week_month_and_quarter_have_inclusive_dates(self):
        self.assertEqual(view.period_bounds(TODAY, "週間"), (date(2026, 11, 2), date(2026, 11, 8)))
        self.assertEqual(view.period_bounds(TODAY, "4週間"), (date(2026, 11, 2), date(2026, 11, 29)))
        self.assertEqual(view.period_bounds(TODAY, "月間"), (date(2026, 11, 1), date(2026, 11, 30)))
        self.assertEqual(view.period_bounds(TODAY, "3か月"), (date(2026, 11, 1), date(2027, 1, 31)))

    def test_whole_plan_includes_deadline_and_milestones_but_not_archives(self):
        data = document(
            item(start_date="2026-10-21", end_date="2026-11-10", deadline="2026-12-15"),
            item(2, start_date="1900-01-01", end_date="1900-01-02", is_archived=True),
            milestones=[{"date": "2027-01-04", "is_archived": False}, {"date": "2199-01-01", "is_archived": True}],
        )
        self.assertEqual(view.period_bounds(TODAY, "全体", data), (date(2026, 10, 21), date(2027, 1, 4)))
        self.assertEqual(view.period_bounds(TODAY, "全体", document()), view.period_bounds(TODAY, "4週間"))

    def test_year_boundaries_clamp_instead_of_overflowing(self):
        for period in view.PERIODS:
            with self.subTest(period=period):
                start, end = view.period_bounds(date(2199, 12, 31), period)
                self.assertLessEqual(start, end)
                self.assertLessEqual(end, view.MAX_DAY)
                start, end = view.period_bounds(date(1900, 1, 1), period)
                self.assertGreaterEqual(start, view.MIN_DAY)
        self.assertEqual(view.shift_anchor(TODAY, "週間", -(10**20)), view.MIN_DAY)
        self.assertEqual(view.shift_anchor(TODAY, "3か月", 10**20), view.MAX_DAY)

    def test_period_navigation_uses_calendar_months_and_known_units(self):
        self.assertEqual(view.shift_anchor(date(2026, 1, 31), "月間", 1), date(2026, 2, 28))
        self.assertEqual(view.shift_anchor(date(2026, 1, 31), "月間", 2), date(2026, 3, 31))
        self.assertEqual(view.shift_anchor(TODAY, "週間", -1), date(2026, 10, 28))
        self.assertEqual(view.shift_anchor(TODAY, "4週間", 1), date(2026, 12, 2))
        for args in ((TODAY, "unknown"), ("2026-11-04", "月間")):
            with self.assertRaises(ValueError):
                view.period_bounds(*args)
        with self.assertRaises(ValueError):
            view.shift_anchor(TODAY, "週間", True)


class CanvasRowTests(unittest.TestCase):
    def build(self, *rows, master=None, actuals=None, allocations=None):
        return view.build_canvas_rows(document(*rows, allocations=allocations or []), master or {}, actuals, today=TODAY)

    def test_pending_lifecycle_remains_visible_with_expired_fixed_deadline(self):
        row = self.build(item(status="pending", start_date="2026-11-01", end_date="2026-11-03", deadline="2026-11-03"))[0]
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["view_state_label"], "保留")
        self.assertEqual(row["view_risk_label"], "確定期限超過")
        self.assertTrue(row["view_needs_attention"])

    def test_date_warnings_are_factual_and_do_not_judge_learning_pace(self):
        overdue = self.build(item(end_date="2026-11-03"))[0]
        self.assertEqual(overdue["view_risk_label"], "終了予定日超過")
        due = self.build(item(end_date="2026-11-04", deadline="2026-11-04"))[0]
        self.assertEqual(due["view_risk_label"], "本日期限")
        soon = self.build(item(end_date="2026-11-07", deadline="2026-11-07"))[0]
        self.assertEqual(soon["view_risk_label"], "期限まで3日")
        self.assertFalse(soon["view_needs_attention"])
        self.assertNotIn("順調", " ".join(str(value) for value in soon.values()))

    def test_completed_historic_stage_is_not_labeled_late_without_completion_date(self):
        row = self.build(item(status="done", completed=10, end_date="2026-11-02", deadline="2026-11-03"))[0]
        self.assertEqual(row["view_state_label"], "完了")
        self.assertEqual(row["view_risk_label"], "")
        self.assertEqual(self.build(item(is_archived=True, end_date="2026-11-01"))[0]["view_risk_label"], "")

    def test_dependency_conflict_and_missing_reference_remain_visible(self):
        parent = item(end_date="2026-11-06")
        child = item(2, start_date="2026-11-06", predecessor_ids=[parent["id"]])
        rows = self.build(parent, child)
        self.assertEqual(rows[1]["view_risk_severity"], "error")
        self.assertIn("前提", rows[1]["view_risk_label"])
        row = self.build(item(predecessor_ids=["missing"]))[0]
        self.assertIn("参照", row["view_risk_label"])

    def test_quantity_is_not_inferred_from_logged_hours_or_calendar_position(self):
        stage = item(completed=2, total=10, unit="章")
        actuals = {"seconds": {stage["id"]: 90000}, "counts": {stage["id"]: 1}}
        row = self.build(stage, actuals=actuals)[0]
        self.assertEqual(row["view_progress_label"], "2/10章")
        self.assertEqual(row["view_progress_percent"], 20)
        self.assertEqual(row["completed"], 2)
        self.assertNotIn("remaining_minutes", row)

    def test_missing_or_invalid_progress_is_unknown_not_zero(self):
        for updates in ({"completed": None}, {"total": 0}, {"total": "40"}, {"completed": True}, {"completed": 11}):
            with self.subTest(updates=updates):
                row = self.build(item(**updates))[0]
                self.assertEqual(row["view_progress_label"], "進捗未確認")
                self.assertIsNone(row["view_progress_percent"])
        percentage = self.build(item(progress_kind="percent", completed=25, total=100, unit="%"))[0]
        self.assertEqual(percentage["view_progress_label"], "25%")

    def test_missing_aggregation_is_uncomputed_not_a_broken_reference(self):
        stage = item()
        allocation = {"id": "ra_" + "a" * 32, "item_id": stage["id"], "is_active": True}
        for actuals in (None, {}, {"seconds": {stage["id"]: 0}, "counts": {stage["id"]: 0}}):
            with self.subTest(actuals=actuals):
                row = self.build(stage, actuals=actuals, allocations=[allocation])[0]
                self.assertEqual(row["view_actual_label"], "未集計")
                self.assertEqual(row["view_actual_state"], "uncomputed")

    def test_names_preserve_inactive_historical_references_and_color_identity(self):
        master = {"projects": [{"project_id": "p", "name": "計画"}], "subjects": [{"subject_id": "s", "name": "生物", "is_active": False}]}
        stage = item(project_id="p", subject_id="s", material_id="missing")
        first = self.build(stage, master=MasterSnapshot(master))[0]
        self.assertEqual(first["view_subject"], "生物")
        self.assertEqual(first["view_material"], "未登録の教材")
        master["subjects"][0]["name"] = "生命科学"
        second = self.build(stage, master=master)[0]
        self.assertEqual(second["view_subject"], "生命科学")
        self.assertEqual(first["view_group_color_key"], second["view_group_color_key"])

    def test_all_5_25_and_100_rows_and_unknown_fields_are_preserved_without_io(self):
        for size in (5, 25, 100):
            rows = [item(number, title=f"Stage {number}", future_field={"keep": [number]}) for number in range(1, size + 1)]
            data = document(*rows)
            before = deepcopy(data)
            with patch.object(json_store, "load_json_safe", side_effect=AssertionError("Presentation attempted IO")):
                rendered = view.build_canvas_rows(data, {}, None, today=TODAY)
            self.assertEqual(len(rendered), size)
            self.assertEqual([row["id"] for row in rendered], [row["id"] for row in rows])
            self.assertEqual(data, before)
            rendered[0]["future_field"]["keep"].append("new")
            self.assertEqual(data, before)


class ActualMeaningTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RoadmapTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def canvas(self):
        data = service.load_roadmap(self.root)
        actuals = service.build_actuals(self.root, data)
        row = view.build_canvas_rows(data, {}, actuals, today=TODAY)[0]
        return row, actuals

    def test_no_allocation_is_unlinked_not_zero_hours_and_does_not_scan_logs(self):
        self.fixture.save()
        data = service.load_roadmap(self.root)
        with patch.object(service.store, "record_json_files", side_effect=AssertionError("Unlinked scan")):
            actual = service.build_actuals(self.root, data)
        row = view.build_canvas_rows(data, {}, actual, today=TODAY)[0]
        self.assertEqual(row["view_actual_label"], "未連携")
        self.assertEqual(actual["seconds"][row["id"]], 0)
        self.assertEqual(actual["allocation_counts"][row["id"]], 0)

    def test_valid_link_without_logs_is_not_observed_zero(self):
        self.fixture.save()
        self.fixture.allocate()
        row, actual = self.canvas()
        self.assertEqual(row["view_actual_label"], "連携済・記録なし")
        self.assertEqual(actual["task_counts"][row["id"]], 1)
        self.assertEqual(actual["broken_allocation_counts"][row["id"]], 0)

    def test_valid_zero_duration_log_is_an_observed_zero(self):
        self.fixture.save()
        self.fixture.allocate()
        task = service.load_target_plan(self.root, date(2026, 11, 1))["tasks"][0]
        self.fixture.put("study_logs/2026/11/20261101.json", [{"session_id": "zero", "duration_sec": 0, "linked_task_id": task["task_id"], "linked_task_date": "2026-11-01"}])
        row, actual = self.canvas()
        self.assertEqual(row["view_actual_state"], "recorded")
        self.assertEqual(row["view_actual_label"], "0分")
        self.assertEqual(actual["counts"][row["id"]], 1)

    def test_deleted_task_is_reference_warning_not_zero_or_unlinked(self):
        self.fixture.save()
        self.fixture.allocate()
        plan = service.load_target_plan(self.root, date(2026, 11, 1))
        plan["tasks"] = []
        self.fixture.put("daily_plans/2026/11/20261101.json", plan)
        row, actual = self.canvas()
        self.assertEqual(row["view_actual_label"], "参照要確認")
        self.assertEqual(actual["allocation_counts"][row["id"]], 1)
        self.assertEqual(actual["broken_allocation_counts"][row["id"]], 1)

    def test_historical_log_remains_visible_if_linked_task_was_deleted(self):
        data, records = self.fixture.actual_fixture()
        for target in (date(2026, 11, 1), date(2026, 11, 2)):
            plan = service.load_target_plan(self.root, target)
            plan["tasks"] = []
            self.fixture.put(f"daily_plans/2026/11/{target:%Y%m%d}.json", plan)
        # Only the original source task has persisted lineage in this fixture.
        self.fixture.put("study_logs/2026/11/20261101.json", [records[0]])
        row, actual = self.canvas()
        self.assertEqual(actual["seconds"][row["id"]], 1800)
        self.assertIn("30分", row["view_actual_label"])
        self.assertIn("参照要確認", row["view_actual_label"])

    def test_existing_one_pass_carryover_aggregation_stays_read_only(self):
        data, _ = self.fixture.actual_fixture()
        before = self.fixture.snapshot()
        original = service.store.load_json_safe
        reads = []
        def counted(path, *args, **kwargs):
            reads.append(str(Path(path).resolve()))
            return original(path, *args, **kwargs)
        with patch.object(service.store, "load_json_safe", side_effect=counted):
            actual = service.build_actuals(self.root, data)
        identifier = data["items"][0]["id"]
        self.assertEqual(actual["seconds"][identifier], 5400)
        self.assertEqual(actual["task_counts"][identifier], 2)
        self.assertEqual(actual["allocation_counts"][identifier], 1)
        self.assertTrue(all(count == 1 for count in Counter(reads).values()))
        self.assertEqual(before, self.fixture.snapshot())


class CapacityStripTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RoadmapTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def test_absent_calendar_is_unknown_and_bar_hours_are_not_daily_allocation(self):
        self.fixture.put("daily_plans/2026/11/20261102.json", {"date": "2026-11-02", "tasks": [{"task_id": "t", "planned_minutes": 37}]})
        data = document(item(planned_minutes=600))
        before = self.fixture.snapshot()
        rows = view.build_capacity_strip(self.root, data, date(2026, 11, 2), date(2026, 11, 3))
        self.assertEqual(rows[0]["planned_minutes"], 37)
        self.assertEqual(rows[1]["planned_minutes"], 0)
        self.assertIsNone(rows[0]["capacity_minutes"])
        self.assertIsNone(rows[0]["over_minutes"])
        self.assertFalse(rows[0]["capacity_configured"])
        self.assertEqual(before, self.fixture.snapshot())

    def test_explicit_zero_day_and_override_are_not_missing(self):
        data = document(calendar={"weekly_minutes": [90, 90, 90, 90, 90, 120, 120], "overrides": {"2026-11-03": 0}})
        self.fixture.put("daily_plans/2026/11/20261103.json", {"date": "2026-11-03", "tasks": [{"task_id": "t", "planned_minutes": 30}]})
        before = self.fixture.snapshot()
        rows = view.build_capacity_strip(self.root, data, date(2026, 11, 2), date(2026, 11, 3))
        self.assertEqual(rows[0]["capacity_minutes"], 90)
        self.assertEqual(rows[1]["capacity_minutes"], 0)
        self.assertEqual(rows[1]["over_minutes"], 30)
        self.assertTrue(rows[1]["capacity_configured"])
        self.assertEqual(before, self.fixture.snapshot())

    def test_long_load_range_is_rejected_before_io(self):
        with patch("app_core.roadmap_planning.load_planning_context", side_effect=AssertionError("Unexpected IO")):
            with self.assertRaises(ValueError):
                view.build_capacity_strip(self.root, document(), date(2026, 1, 1), date(2027, 12, 31))


if __name__ == "__main__":
    unittest.main()
