from copy import deepcopy
from datetime import date
import unittest
from unittest.mock import patch
from uuid import uuid4

from app_core import json_store
from app_core import roadmap_service as service
from app_core import roadmap_planning as planner
import test_roadmap_service as fixtures


class PlanningTests(unittest.TestCase):
    def integrity(self, data):
        import test_integrity_quality as quality
        from app_core import integrity_service

        fixture = quality.IntegrityQualityTests()
        fixture.setUp()
        try:
            fixture.write_json("settings/study_roadmap.json", data)
            before = fixture.file_snapshot()
            result = integrity_service.run_integrity_checks()
            self.assertEqual(fixture.file_snapshot(), before)
            return [
                row
                for row in result["issues"]
                if row["問題カテゴリ"] == "学習ロードマップ"
            ]
        finally:
            fixture.doCleanups()

    def test_integrity_reports_dependency_date_order_as_notice(self):
        a, b, c = self.chain()
        data = self.data()
        data["items"][1]["start_date"] = a["end_date"]
        issues = self.integrity(data)
        self.assertTrue(any("前提工程" in row["内容"] for row in issues))

    def test_integrity_reports_cycles_and_bad_capacity_without_writing(self):
        a, b, c = self.chain()
        for case in ("cycle", "calendar"):
            data = self.data()
            if case == "cycle":
                data["items"][0]["predecessor_ids"] = [c["id"]]
            else:
                data["calendar"] = {"weekly_minutes": [-1] * 7, "overrides": {}}
            with self.subTest(case=case):
                self.assertTrue(self.integrity(data))

    def setUp(self):
        self.fixture = fixtures.RoadmapTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.path = service.roadmap_path(self.root)

    def add(self, title="A", start="2026-11-01", end="2026-11-02", **updates):
        self.fixture.save(
            self.fixture.payload(
                **{
                    **dict(
                        title=title,
                        start_date=start,
                        end_date=end,
                        completed=0,
                        predecessor_ids=[],
                    ),
                    **updates,
                }
            )
        )
        return service.load_roadmap(self.root)["items"][-1]

    def chain(self):
        a = self.add()
        b = self.add("B", "2026-11-03", "2026-11-04", predecessor_ids=[a["id"]])
        c = self.add("C", "2026-11-05", "2026-11-06", predecessor_ids=[b["id"]])
        return a, b, c

    def data(self):
        return service.load_roadmap(self.root)

    def revision(self):
        return self.fixture.revision()

    def calendar(self, weekly=None, overrides=None):
        planner.save_calendar(
            self.root,
            {"weekly_minutes": weekly or [60] * 7, "overrides": overrides or {}},
            expected_revision=self.revision(),
            operation_id=uuid4().hex,
        )

    def preview(self, requests, start=date(2026, 11, 1), end=date(2026, 11, 6)):
        return planner.build_distribution_preview(
            self.root, self.data(), requests, start, end
        )

    def commit(self, preview, **kwargs):
        return planner.apply_distribution_preview(
            self.root,
            preview,
            expected_revision=kwargs.pop("revision", self.revision()),
            operation_id=kwargs.pop("operation_id", uuid4().hex),
            **kwargs,
        )

    def test_legacy_catalog_defaults_are_read_only(self):
        data = service.load_roadmap(self.root)
        self.assertEqual(planner.calendar_settings(data)["weekly_minutes"], [0] * 7)
        self.assertEqual(planner.validate_dependencies(data), [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cycle_missing_and_duplicate_predecessors_are_rejected(self):
        a, b, c = self.chain()
        data = self.data()
        for values in ([a["id"]], ["missing"], [b["id"], b["id"]], [c["id"]]):
            changed = deepcopy(data)
            changed["items"][0]["predecessor_ids"] = values
            with self.subTest(values=values), self.assertRaises(ValueError):
                service.validate_roadmap(changed)

    def test_large_dag_does_not_use_recursive_traversal(self):
        rows = []
        for index in range(1500):
            rows.append(
                {"id": str(index), "predecessor_ids": [str(index - 1)] if index else []}
            )
        self.assertEqual(len(planner.validate_dependencies({"items": rows})), 1500)

    def test_schedule_cascades_only_as_needed_and_does_not_write_on_preview(self):
        a, b, c = self.chain()
        before = self.fixture.snapshot()
        preview = planner.schedule_preview(
            self.data(),
            {a["id"]: {"start_date": "2026-11-04", "end_date": "2026-11-05"}},
        )
        changes = {row["item_id"]: row for row in preview["changes"]}
        self.assertEqual(changes[b["id"]]["start_date"], "2026-11-06")
        self.assertEqual(changes[c["id"]]["start_date"], "2026-11-08")
        self.assertEqual(before, self.fixture.snapshot())

    def test_bulk_shift_includes_descendants_and_keeps_buffers(self):
        a = self.add()
        b = self.add("B", "2026-11-10", "2026-11-12", predecessor_ids=[a["id"]])
        changes = planner.shift_changes(
            self.data(), [a["id"]], 2, include_descendants=True
        )
        preview = planner.schedule_preview(self.data(), changes)
        mapped = {row["item_id"]: row for row in preview["changes"]}
        self.assertEqual(mapped[b["id"]]["start_date"], "2026-11-12")
        earlier = planner.schedule_preview(
            self.data(), planner.shift_changes(self.data(), [a["id"]], -1)
        )
        self.assertEqual(len(earlier["changes"]), 1)

    def test_resizing_a_single_day_and_crossing_year_are_inclusive(self):
        a = self.add(start="2026-12-31", end="2026-12-31")
        preview = planner.schedule_preview(
            self.data(),
            {a["id"]: {"start_date": "2027-01-01", "end_date": "2027-01-01"}},
        )
        self.assertEqual(preview["changes"][0]["end_date"], "2027-01-01")

    def test_moving_completed_or_archived_stage_is_rejected(self):
        for state in ({"status": "done", "completed": 10}, {"is_archived": True}):
            a = self.add(**state)
            with self.assertRaises(ValueError):
                planner.schedule_preview(
                    self.data(),
                    {a["id"]: {"start_date": "2026-11-02", "end_date": "2026-11-03"}},
                )

    def test_locked_successor_blocks_cascade(self):
        a = self.add()
        self.add(
            "B",
            "2026-11-03",
            "2026-11-04",
            predecessor_ids=[a["id"]],
            status="done",
            completed=10,
        )
        with self.assertRaisesRegex(ValueError, "完了"):
            planner.schedule_preview(
                self.data(), planner.shift_changes(self.data(), [a["id"]], 3)
            )

    def test_non_cascading_conflict_and_reversed_dates_are_rejected(self):
        a, b, c = self.chain()
        with self.assertRaises(ValueError):
            planner.schedule_preview(
                self.data(),
                planner.shift_changes(self.data(), [a["id"]], 4),
                cascade=False,
            )
        with self.assertRaises(ValueError):
            planner.schedule_preview(
                self.data(),
                {a["id"]: {"start_date": "2026-11-10", "end_date": "2026-11-01"}},
            )

    def test_schedule_commit_keeps_baseline_history_and_idempotency(self):
        a, b, c = self.chain()
        preview = planner.schedule_preview(
            self.data(), planner.shift_changes(self.data(), [a["id"]], 2)
        )
        revision = self.revision()
        opid = uuid4().hex
        self.assertTrue(
            planner.apply_schedule_preview(
                self.root, preview, expected_revision=revision, operation_id=opid
            )
        )
        data = self.data()
        self.assertEqual(data["items"][0]["baseline_start_date"], "2026-11-01")
        self.assertEqual(len(data["items"][0]["schedule_history"]), 1)
        before = self.path.read_bytes()
        self.assertFalse(
            planner.apply_schedule_preview(
                self.root, preview, expected_revision=revision, operation_id=opid
            )
        )
        self.assertEqual(before, self.path.read_bytes())

    def test_schedule_stale_or_tampered_preview_cannot_overwrite(self):
        a = self.add()
        preview = planner.schedule_preview(
            self.data(), planner.shift_changes(self.data(), [a["id"]], 2)
        )
        revision = self.revision()
        modified = deepcopy(preview)
        modified["changes"][0]["end_date"] = "2026-12-31"
        with self.assertRaises(ValueError):
            planner.apply_schedule_preview(
                self.root,
                modified,
                expected_revision=revision,
                operation_id=uuid4().hex,
            )
        self.fixture.save(self.fixture.payload(title="external"), a["id"])
        before = self.path.read_bytes()
        with self.assertRaises(json_store.JsonConflictError):
            planner.apply_schedule_preview(
                self.root, preview, expected_revision=revision, operation_id=uuid4().hex
            )
        self.assertEqual(before, self.path.read_bytes())

    def test_deadlines_and_existing_allocations_are_reported(self):
        a = self.add(deadline="2026-11-03")
        self.fixture.allocate(a["id"])
        preview = planner.schedule_preview(
            self.data(), planner.shift_changes(self.data(), [a["id"]], 5)
        )
        self.assertTrue(any("確定期限" in warning for warning in preview["warnings"]))
        self.assertTrue(any("日別割当" in warning for warning in preview["warnings"]))

    def test_calendar_bounds_and_override_precedence(self):
        self.calendar([60] * 7, {"2026-11-02": 0, "2026-11-03": 180})
        settings = planner.calendar_settings(self.data())
        self.assertEqual(planner.capacity_minutes(settings, date(2026, 11, 2)), 0)
        self.assertEqual(planner.capacity_minutes(settings, date(2026, 11, 3)), 180)
        for value in (
            {"weekly_minutes": [True] * 7, "overrides": {}},
            {"weekly_minutes": [1] * 6, "overrides": {}},
            {"weekly_minutes": [1] * 7, "overrides": {"2026-02-30": 60}},
            {"weekly_minutes": [1] * 7, "overrides": {"2026-01-01": 1441}},
        ):
            with self.assertRaises(ValueError):
                planner.calendar_settings({"calendar": value})

    def test_capacity_uses_existing_tasks_and_does_not_touch_records_on_preview(self):
        from data_utils import default_daily_plan

        a = self.add(end="2026-11-03")
        self.calendar([60] * 7, {"2026-11-02": 0})
        plan = default_daily_plan(date(2026, 11, 1))
        plan["tasks"] = [
            {"task_id": "unrelated", "planned_minutes": 45, "status": "done"}
        ]
        self.fixture.put("daily_plans/2026/11/20261101.json", plan)
        before = self.fixture.snapshot()
        preview = self.preview({a["id"]: 90}, end=date(2026, 11, 3))
        self.assertEqual(
            [(row["date"], row["minutes"]) for row in preview["rows"]],
            [("2026-11-01", 15), ("2026-11-03", 60)],
        )
        self.assertEqual(preview["unassigned"][0]["minutes"], 15)
        self.assertEqual(before, self.fixture.snapshot())

    def test_weekly_load_uses_only_the_requested_partial_week(self):
        self.add()
        self.calendar()
        plans, _ = planner.load_planning_context(
            self.root, date(2026, 11, 4), date(2026, 11, 6)
        )
        rows = planner.load_rows(
            self.data(), plans, date(2026, 11, 4), date(2026, 11, 6)
        )
        self.assertEqual(planner.weekly_load(rows)[0]["capacity_minutes"], 180)

    def test_dependencies_and_shortage_prevent_downstream_assignment(self):
        a, b, c = self.chain()
        self.calendar([30] * 7)
        preview = self.preview({a["id"]: 100, b["id"]: 30})
        self.assertEqual(
            sum(row["minutes"] for row in preview["rows"] if row["item_id"] == a["id"]),
            60,
        )
        self.assertFalse(any(row["item_id"] == b["id"] for row in preview["rows"]))
        self.assertTrue(
            any("前提工程" in row["reason"] for row in preview["unassigned"])
        )

    def test_allocation_respects_stage_dates_and_hard_deadline(self):
        a = self.add(start="2026-11-03", end="2026-11-06", deadline="2026-11-04")
        self.calendar()
        preview = self.preview({a["id"]: 180})
        self.assertEqual(
            [row["date"] for row in preview["rows"]], ["2026-11-03", "2026-11-04"]
        )
        self.assertEqual(preview["unassigned"][0]["minutes"], 60)

    def test_distribution_never_exceeds_day_capacity_across_stages(self):
        a = self.add()
        b = self.add("B")
        self.calendar()
        preview = self.preview({a["id"]: 90, b["id"]: 90}, end=date(2026, 11, 2))
        self.assertTrue(
            all(
                row["total_minutes"] <= row["capacity_minutes"]
                for row in preview["load"]
            )
        )
        self.assertEqual(sum(row["minutes"] for row in preview["rows"]), 120)

    def test_zero_capacity_and_invalid_requests_do_not_create_tasks(self):
        a = self.add()
        preview = self.preview({a["id"]: 60})
        self.assertEqual(preview["rows"], [])
        with self.assertRaises(ValueError):
            self.commit(preview, allow_partial=True)
        for requests in ({}, {a["id"]: 0}, {a["id"]: True}, {"missing": 60}):
            with self.assertRaises(ValueError):
                self.preview(requests)
        with self.assertRaises(ValueError):
            planner.planning_days(date(2026, 1, 1), date(2027, 1, 2))
        self.assertFalse((self.root / "daily_plans").exists())

    def test_distribution_commit_and_retry_are_atomic_and_idempotent(self):
        a = self.add(end="2026-11-04")
        self.calendar()
        preview = self.preview({a["id"]: 120}, end=date(2026, 11, 4))
        revision = self.revision()
        opid = uuid4().hex
        self.assertTrue(self.commit(preview, revision=revision, operation_id=opid))
        data = self.data()
        self.assertEqual(len(data["allocations"]), 2)
        self.assertEqual(data["items"][0]["completed"], 0)
        before = self.fixture.snapshot()
        self.assertFalse(self.commit(preview, revision=revision, operation_id=opid))
        self.assertEqual(before, self.fixture.snapshot())

    def test_partial_commit_requires_explicit_confirmation(self):
        a = self.add()
        self.calendar()
        preview = self.preview({a["id"]: 180}, end=date(2026, 11, 2))
        before = self.fixture.snapshot()
        with self.assertRaises(ValueError):
            self.commit(preview)
        self.assertEqual(before, self.fixture.snapshot())
        self.assertTrue(self.commit(preview, allow_partial=True))

    def test_new_plan_after_preview_is_not_silently_merged(self):
        from data_utils import default_daily_plan

        a = self.add(end="2026-11-04")
        self.calendar()
        preview = self.preview({a["id"]: 60}, end=date(2026, 11, 4))
        revision = self.revision()
        self.fixture.put(
            "daily_plans/2026/11/20261104.json", default_daily_plan(date(2026, 11, 4))
        )
        before = self.fixture.snapshot()
        with self.assertRaises(json_store.JsonConflictError):
            self.commit(preview, revision=revision)
        self.assertEqual(before, self.fixture.snapshot())

    def test_calendar_change_invalidates_distribution_preview(self):
        a = self.add()
        self.calendar()
        preview = self.preview({a["id"]: 60})
        revision = self.revision()
        self.calendar([30] * 7)
        with self.assertRaises(json_store.JsonConflictError):
            self.commit(preview, revision=revision)

    def test_bulk_failure_restores_catalog_and_all_daily_documents(self):
        a = self.add(end="2026-11-04")
        self.calendar()
        preview = self.preview({a["id"]: 180}, end=date(2026, 11, 4))
        before = self.fixture.snapshot()
        original = json_store.save_json_safe
        count = 0

        def fail(path, *args, **kwargs):
            nonlocal count
            if (
                "daily_plans" in path.parts
                and json_store._committing_operation.get() is not None
            ):
                count += 1
                if count == 2:
                    raise OSError("synthetic second-file failure")
            return original(path, *args, **kwargs)

        with (
            patch.object(json_store, "save_json_safe", side_effect=fail),
            self.assertRaises(ValueError),
        ):
            self.commit(preview)
        self.assertEqual(before, self.fixture.snapshot())

    def test_export_contains_dependencies_capacity_and_distribution_ids(self):
        from app_core.export_service import build_export_bundle

        a, b, c = self.chain()
        self.calendar()
        preview = self.preview({a["id"]: 60})
        self.commit(preview)
        tables = {table.key: table for table in build_export_bundle(self.root).tables}
        self.assertIn("predecessor_ids", tables["roadmap_items"].columns)
        self.assertEqual(tables["roadmap_capacity"].row_count, 7)
        self.assertIn("distribution_id", tables["roadmap_allocations"].columns)


if __name__ == "__main__":
    unittest.main()
