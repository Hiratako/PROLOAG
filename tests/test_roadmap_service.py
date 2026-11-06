from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from app_core import json_store
from app_core import roadmap_service as service


class RoadmapTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="pgrm_")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.path = service.roadmap_path(self.root)
        p = patch.object(json_store, "PROJECT_ROOT", self.root)
        p.start()
        self.addCleanup(p.stop)

    def put(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def payload(self, **updates):
        value = dict(
            title="Synthetic stage",
            start_date="2026-11-01",
            end_date="2026-11-10",
            deadline="",
            planned_minutes=600,
            status="in_progress",
            progress_kind="quantity",
            completed=2,
            total=10,
            unit="章",
            memo="",
            is_archived=False,
            project_id="",
            subject_id="",
            material_id="",
            unit_id="",
        )
        return {**value, **updates}

    def revision(self):
        service.load_roadmap(self.root)
        return json_store.observed_json_revision(self.path)

    def save(self, updates=None, identifier="", **kwargs):
        return service.save_entry(
            self.root,
            "items",
            updates or self.payload(),
            identifier=identifier,
            expected_revision=kwargs.pop("revision", self.revision()),
            operation_id=kwargs.pop("opid", uuid4().hex),
            **kwargs,
        )

    def item(self):
        return service.load_roadmap(self.root)["items"][0]

    def allocate(self, item_id=None, target=date(2026, 11, 1), **kwargs):
        from data_utils import daily_plan_path

        service.load_target_plan(self.root, target)
        revisions = {
            self.path: self.revision(),
            daily_plan_path(self.root, target): json_store.observed_json_revision(
                daily_plan_path(self.root, target)
            ),
        }
        return service.allocate_task(
            self.root,
            item_id or self.item()["id"],
            target,
            expected_revisions=kwargs.pop("revisions", revisions),
            operation_id=kwargs.pop("opid", uuid4().hex),
            title=kwargs.pop("title", "daily synthetic"),
            planned_minutes=kwargs.pop("minutes", 60),
            **kwargs,
        )

    def snapshot(self):
        return {
            str(p.relative_to(self.root)): p.read_bytes()
            for p in self.root.rglob("*.json")
            if "backups" not in p.parts
        }

    def test_empty_is_read_only(self):
        self.assertEqual(service.load_roadmap(self.root), service.empty_roadmap())
        self.assertEqual(
            service.build_actuals(self.root, service.empty_roadmap())["seconds"], {}
        )
        self.assertEqual(list(self.root.iterdir()), [])

    def test_create_rename_reschedule_preserves_baseline_and_unknown_fields(self):
        self.save()
        item = self.item()
        identifier = item["id"]
        data = service.load_roadmap(self.root)
        data["items"][0]["future_property"] = {"safe": True}
        self.put("settings/study_roadmap.json", data)
        self.save(
            self.payload(
                title="Renamed", start_date="2026-11-03", end_date="2026-11-15"
            ),
            identifier,
        )
        changed = self.item()
        self.assertEqual(changed["id"], identifier)
        self.assertEqual(
            (changed["baseline_start_date"], changed["baseline_end_date"]),
            ("2026-11-01", "2026-11-10"),
        )
        self.assertEqual(changed["schedule_history"][0]["end_date"], "2026-11-10")
        self.assertTrue(changed["future_property"]["safe"])

    def test_invalid_inputs_do_not_create_document(self):
        for values in (
            {"start_date": "2026-11-20"},
            {"completed": 11},
            {"total": 0},
            {"planned_minutes": True},
            {"completed": float("nan")},
            {"title": ""},
            {"status": "done"},
            {"progress_kind": "percent", "total": 10},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.save(self.payload(**values))
        self.assertFalse(self.path.exists())

    def test_corrupt_and_null_documents_are_not_overwritten(self):
        for bad in (None, [], {"schema_version": True}):
            self.put("settings/study_roadmap.json", bad)
            before = self.path.read_bytes()
            with self.assertRaises(ValueError):
                service.load_roadmap(self.root)
            with self.assertRaises(ValueError):
                service.save_entry(
                    self.root,
                    "items",
                    self.payload(),
                    expected_revision=json_store.observed_json_revision(self.path),
                    operation_id=uuid4().hex,
                )
            self.assertEqual(before, self.path.read_bytes())

    def test_same_operation_retry_is_idempotent_and_changed_payload_is_rejected(self):
        opid = uuid4().hex
        old = self.revision()
        self.save(opid=opid, revision=old)
        before = self.path.read_bytes()
        self.assertFalse(self.save(opid=opid, revision=old))
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaises(ValueError):
            self.save(self.payload(title="different request"), opid=opid, revision=old)

    def test_external_edit_conflict_does_not_overwrite(self):
        self.save()
        item = self.item()
        old = self.revision()
        self.save(self.payload(title="external"), item["id"])
        before = self.path.read_bytes()
        with self.assertRaises(json_store.JsonConflictError):
            self.save(self.payload(title="stale"), item["id"], revision=old)
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_classification_is_rejected(self):
        with self.assertRaises(ValueError):
            self.save(self.payload(project_id="missing"))

    def test_completed_and_pending_stages_are_not_marked_late(self):
        self.assertEqual(
            service.schedule_label(self.payload(status="pending"), date(2027, 1, 1)),
            "保留",
        )
        self.assertEqual(
            service.schedule_label(
                self.payload(status="done", completed=10), date(2027, 1, 1)
            ),
            "完了",
        )
        self.assertEqual(
            service.schedule_label(
                self.payload(deadline="2026-11-09"), date(2026, 11, 10)
            ),
            "期限超過",
        )

    def test_milestone_archive_and_duplicate_ids(self):
        payload = dict(
            title="Mock exam",
            date="2026-12-01",
            project_id="",
            memo="",
            is_archived=False,
        )
        service.save_entry(
            self.root,
            "milestones",
            payload,
            expected_revision=self.revision(),
            operation_id=uuid4().hex,
        )
        data = service.load_roadmap(self.root)
        data["milestones"].append(deepcopy(data["milestones"][0]))
        with self.assertRaises(ValueError):
            service.validate_roadmap(data)

    def test_new_daily_allocation_and_retry_create_one_task(self):
        self.save()
        opid = uuid4().hex
        self.allocate(opid=opid)
        data = service.load_roadmap(self.root)
        plan = service.load_target_plan(self.root, date(2026, 11, 1))
        self.assertEqual(len(plan["tasks"]), 1)
        self.assertEqual(
            plan["tasks"][0]["roadmap_allocation_id"], data["allocations"][0]["id"]
        )
        # A retransmission retains the old revisions; journal acknowledgement precedes conflict checks.
        self.assertFalse(
            self.allocate(
                opid=opid,
                revisions={
                    self.path: None,
                    self.root / "daily_plans/2026/11/20261101.json": None,
                },
            )
        )
        self.assertEqual(
            len(service.load_target_plan(self.root, date(2026, 11, 1))["tasks"]), 1
        )

    def test_link_existing_keeps_task_properties_and_does_not_duplicate(self):
        from data_utils import default_daily_plan

        self.save()
        plan = default_daily_plan(date(2026, 11, 1))
        plan["tasks"] = [
            {
                "task_id": "old",
                "title": "Keep title",
                "planned_minutes": 37,
                "custom": True,
            }
        ]
        self.put("daily_plans/2026/11/20261101.json", plan)
        self.allocate(task_id="old")
        after = service.load_target_plan(self.root, date(2026, 11, 1))["tasks"][0]
        self.assertEqual(
            (after["title"], after["planned_minutes"], after["custom"]),
            ("Keep title", 37, True),
        )
        self.assertFalse(self.allocate(task_id="old"))
        self.assertEqual(len(service.load_roadmap(self.root)["allocations"]), 1)

    def test_plan_conflict_does_not_write_roadmap_or_new_task(self):
        from data_utils import daily_plan_path, default_daily_plan

        self.save()
        target = date(2026, 11, 1)
        path = daily_plan_path(self.root, target)
        revisions = {self.path: self.revision(), path: None}
        self.put("daily_plans/2026/11/20261101.json", default_daily_plan(target))
        before = self.snapshot()
        with self.assertRaises(json_store.JsonConflictError):
            self.allocate(revisions=revisions)
        self.assertEqual(before, self.snapshot())

    def test_partial_allocation_write_failure_rolls_back_both_documents(self):
        self.save()
        before = self.snapshot()
        original = json_store.save_json_safe
        failed = False

        def fail(path, *args, **kwargs):
            nonlocal failed
            if (
                "daily_plans" in Path(path).parts
                and json_store._committing_operation.get() is not None
                and not failed
            ):
                failed = True
                raise OSError("synthetic write failure")
            return original(path, *args, **kwargs)

        with (
            patch.object(json_store, "save_json_safe", side_effect=fail),
            self.assertRaises(ValueError),
        ):
            self.allocate()
        self.assertEqual(before, self.snapshot())

    def actual_fixture(self):
        self.save()
        self.allocate()
        data = service.load_roadmap(self.root)
        first = service.load_target_plan(self.root, date(2026, 11, 1))
        task = first["tasks"][0]
        from data_utils import default_daily_plan

        next_day = default_daily_plan(date(2026, 11, 2))
        carried = deepcopy(task)
        carried.update(
            task_id="carried",
            carried_from_task_id=task["task_id"],
            linked_session_ids=["s2"],
        )
        next_day["tasks"] = [carried]
        self.put("daily_plans/2026/11/20261102.json", next_day)
        task["linked_session_ids"] = ["s1"]
        self.put("daily_plans/2026/11/20261101.json", first)
        records = [
            dict(
                session_id="s1",
                duration_sec=1800,
                linked_task_id=task["task_id"],
                linked_task_date="2026-11-01",
            ),
            dict(
                session_id="s2",
                duration_sec=3600,
                linked_task_id="carried",
                linked_task_date="2026-11-02",
            ),
        ]
        self.put("study_logs/2026/11/20261101.json", records)
        return data, records

    def test_carryover_and_manual_actuals_use_seconds_without_modifying_progress(self):
        data, _ = self.actual_fixture()
        before = self.snapshot()
        actual = service.build_actuals(self.root, data)
        self.assertEqual(actual["seconds"][self.item()["id"]], 5400)
        self.assertEqual(actual["counts"][self.item()["id"]], 2)
        self.assertEqual(service.progress_percent(self.item()), 20)
        self.assertEqual(before, self.snapshot())

    def test_identical_duplicate_session_is_counted_once_and_conflict_excluded(self):
        data, rows = self.actual_fixture()
        self.put("study_logs/2026/11/20261102.json", [rows[0]])
        self.assertEqual(
            service.build_actuals(self.root, data)["seconds"][self.item()["id"]], 5400
        )
        self.put("study_logs/2026/11/20261102.json", [{**rows[0], "duration_sec": 1}])
        actual = service.build_actuals(self.root, data)
        self.assertEqual(actual["seconds"][self.item()["id"]], 3600)
        self.assertTrue(actual["warnings"])

    def test_deleted_or_corrected_sessions_update_actuals(self):
        data, rows = self.actual_fixture()
        self.put(
            "study_logs/2026/11/20261101.json", [{**rows[1], "duration_sec": 7200}]
        )
        self.assertEqual(
            service.build_actuals(self.root, data)["seconds"][self.item()["id"]], 7200
        )

    def test_deleted_carried_task_keeps_historical_actuals_via_lineage(self):
        data, _ = self.actual_fixture()
        target = date(2026, 11, 2)
        plan = service.load_target_plan(self.root, target)
        path = self.root / "daily_plans/2026/11/20261102.json"
        service.save_linked_daily_plan(
            self.root, plan, expected_revision=json_store.observed_json_revision(path)
        )
        plan["tasks"] = []
        self.put("daily_plans/2026/11/20261102.json", plan)
        result = service.build_actuals(self.root, service.load_roadmap(self.root))
        self.assertEqual(result["seconds"][self.item()["id"]], 5400)

    def test_malformed_reference_and_extreme_numeric_values_are_rejected(self):
        for values in ({"status": []}, {"planned_minutes": 10**400}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.save(self.payload(**values))

    def test_unlink_preserves_tasks_and_study_records(self):
        data, _ = self.actual_fixture()
        before = self.snapshot()
        allocation = data["allocations"][0]
        service.deactivate_allocation(
            self.root,
            allocation["id"],
            expected_revision=self.revision(),
            operation_id=uuid4().hex,
        )
        after = self.snapshot()
        self.assertEqual(
            {
                k: v
                for k, v in before.items()
                if k != str(self.path.relative_to(self.root))
            },
            {
                k: v
                for k, v in after.items()
                if k != str(self.path.relative_to(self.root))
            },
        )
        self.assertEqual(
            service.build_actuals(self.root, service.load_roadmap(self.root))[
                "seconds"
            ][self.item()["id"]],
            0,
        )

    def test_local_exports_include_all_roadmap_definitions_and_original_values(self):
        from app_core.export_service import build_export_bundle

        self.actual_fixture()
        self.save(
            self.payload(end_date="2026-12-05", memo="long note"), self.item()["id"]
        )
        bundle = build_export_bundle(self.root)
        tables = {table.key: table for table in bundle.tables}
        self.assertEqual(tables["roadmap_items"].row_count, 1)
        self.assertEqual(tables["roadmap_allocations"].row_count, 1)
        self.assertIn("baseline_end_date", tables["roadmap_items"].columns)
        self.assertIn("long note", tables["roadmap_items"].data.decode("utf-8-sig"))


if __name__ == "__main__":
    unittest.main()
