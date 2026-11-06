from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from app_core import json_store, study_log_editor, study_log_service


class StudyLogEditorTests(unittest.TestCase):
    """Saved-log edits use synthetic records only; the real application is never loaded."""

    policy = {"overtime_enabled": True, "threshold_hours": 8.0, "multiplier": 1.25}
    session_id = "session_20260828_001"
    original_created_at = "2026-08-28 08:00:00"

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="proloag_editor_test_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patches = ExitStack()
        self.addCleanup(patches.close)
        for module in (json_store, study_log_editor, study_log_service):
            if hasattr(module, "PROJECT_ROOT"):
                patches.enter_context(patch.object(module, "PROJECT_ROOT", self.root))
        self.write_json("master_data/master.json", {
            "projects": [{"project_id": "project_a", "name": "synthetic project", "is_active": True}],
            "subjects": [{"subject_id": "subject_a", "project_id": "project_a", "name": "synthetic subject", "is_active": True}],
            "materials": [{"material_id": "material_a", "project_id": "project_a", "subject_id": "subject_a", "name": "synthetic material", "is_active": True}],
            "units": [{"unit_id": "unit_a", "project_id": "project_a", "subject_id": "subject_a", "material_id": "material_a", "name": "synthetic unit", "is_active": True}],
            "tags": [],
        })

    def write_json(self, relative: str | Path, value: Any) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        return path

    def read_json(self, relative: str | Path) -> Any:
        return json.loads((self.root / relative).read_text(encoding="utf-8-sig"))

    def session(self, **updates: Any) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "start_at": "2026-08-28T09:00:00",
            "end_at": "2026-08-28T10:00:00",
            "duration_sec": 3600,
            "hourly_rate": 1000,
            "money": 1000,
            "normal_money": 1000,
            "overtime_money": 0,
            "money_policy": deepcopy(self.policy),
            "memo": "synthetic original",
            "condition": {"environment": "自宅", "custom_condition": "keep"},
            "project_id": "",
            "subject_id": "",
            "material_id": "",
            "unit_id": "",
            "linked_note_ids": [],
            "linked_task_id": "",
            "created_at": self.original_created_at,
            "updated_at": self.original_created_at,
            "custom_data": {"nested": [1, 2, {"keep": True}]},
            **updates,
        }

    def seed(self, rows: list[dict[str, Any]], target: str = "2026-08-28", *, envelope: bool = False) -> Path:
        path = study_log_service.get_study_log_path(target, self.root)
        payload = {"sessions": deepcopy(rows), "custom_envelope": {"keep": [1, 2]}} if envelope else rows
        return self.write_json(path, payload)

    def rows(self, target: str = "2026-08-28") -> list[dict[str, Any]]:
        path = study_log_service.get_study_log_path(target, self.root)
        if not path.exists():
            return []
        payload = self.read_json(path)
        return payload["sessions"] if isinstance(payload, dict) else payload

    def row(self, session_id: str | None = None, target: str = "2026-08-28") -> dict[str, Any]:
        return next(row for row in self.rows(target) if row.get("session_id") == (session_id or self.session_id))

    def disk_snapshot(self) -> dict[str, tuple[bytes, int]]:
        return {
            str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.root.rglob("*") if path.is_file()
        }

    def json_snapshot(self) -> dict[str, bytes]:
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*.json") if path.is_file()}

    def live_snapshot(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in self.root.rglob("*.json")
            if path.is_file() and "backups" not in path.relative_to(self.root).parts
        }

    def snapshot(self, session_id: str | None = None) -> dict[str, Any]:
        return study_log_editor.load_editable_session(session_id or self.session_id, root=self.root)

    def prepare_edit(self, updates: dict[str, Any], *, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        return study_log_editor.prepare_session_edit(
            snapshot or self.snapshot(), updates, root=self.root, fallback_policy=deepcopy(self.policy),
        )

    def prepare_delete(self, *, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        return study_log_editor.prepare_session_delete(
            snapshot or self.snapshot(), root=self.root, fallback_policy=deepcopy(self.policy),
        )

    def commit(self, plan: dict[str, Any]) -> dict[str, Any]:
        result = study_log_editor.commit_session_change(plan, root=self.root)
        self.assertIn("backup_path", result)
        backup = Path(result["backup_path"])
        if not backup.is_absolute():
            backup = self.root / backup
        self.assertTrue(backup.exists(), "An operation backup must exist after a saved change")
        self.assertTrue(backup.resolve().is_relative_to(self.root.resolve()))
        return result

    def delete(self) -> str:
        self.commit(self.prepare_delete())
        deleted = study_log_editor.list_deleted_sessions(root=self.root)
        self.assertEqual(len(deleted), 1)
        return deleted[0]["trash_id"]

    def prepare_restore(self, trash_id: str, **kwargs: Any) -> dict[str, Any]:
        return study_log_editor.prepare_session_restore(trash_id, root=self.root, fallback_policy=deepcopy(self.policy), **kwargs)

    def assert_public_plan(self, plan: dict[str, Any], kind: str) -> None:
        for key in ("kind", "session_id", "before", "after", "money_changes", "warnings", "changed_files"):
            self.assertIn(key, plan)
        self.assertEqual(plan["kind"], kind)
        self.assertEqual(plan["session_id"], self.session_id)
        self.assertIsInstance(plan["money_changes"], list)
        self.assertIsInstance(plan["warnings"], list)
        self.assertIsInstance(plan["changed_files"], list)

    @staticmethod
    def note(note_id: str, linked: list[str]) -> dict[str, Any]:
        return {
            "id": note_id,
            "note_id": note_id,
            "title": f"synthetic {note_id}",
            "body": f"body for {note_id}",
            "content": f"body for {note_id}",
            "linked_study_session_ids": linked,
            "linked_flashcard_ids": ["flashcard_other"],
            "linked_reminder_ids": ["reminder_other"],
            "custom_note": {"keep": True},
            "created_at": "2026-08-01 10:00:00",
            "updated_at": "2026-08-01 10:00:00",
        }

    def seed_links(self) -> None:
        self.write_json("notes_data/notebooks.json", {
            "custom_store": "keep",
            "notebooks": [{"id": "notebook_a", "name": "synthetic", "sections": [
                {"id": "section_a", "name": "synthetic A", "pages": [self.note("note_a", [self.session_id, "session_other"])]},
                {"id": "section_b", "name": "synthetic B", "pages": [self.note("note_reverse_only", [self.session_id])]},
            ]}],
        })
        for day, task_id in (("2026-08-28", "task_a"), ("2026-08-29", "task_reverse_only")):
            self.write_json(f"daily_plans/2026/08/{day.replace('-', '')}.json", {
                "date": day,
                "custom_plan": {"keep": True},
                "tasks": [{
                    "task_id": task_id,
                    "title": f"synthetic {task_id}",
                    "status": "done",
                    "linked_session_ids": ["session_other", self.session_id],
                    "linked_note_ids": ["note_a"],
                    "custom_task": {"keep": [1, 2]},
                }],
            })

    def note_pages(self) -> list[dict[str, Any]]:
        store = self.read_json("notes_data/notebooks.json")
        return [page for notebook in store["notebooks"] for section in notebook["sections"] for page in section["pages"]]

    def test_load_and_prepare_are_read_only_and_keep_input_snapshot(self) -> None:
        self.seed([self.session()], envelope=True)
        before_disk = self.disk_snapshot()
        snapshot = self.snapshot()
        snapshot_copy = deepcopy(snapshot)
        self.assertEqual(snapshot["session"]["session_id"], self.session_id)
        self.assertFalse(Path(snapshot["source_file"]).is_absolute())
        self.assertIsInstance(snapshot["revision"], str)
        updates = {"memo": "edited memo"}
        plan = self.prepare_edit(updates, snapshot=snapshot)
        self.assert_public_plan(plan, "edit")
        self.assertEqual(snapshot, snapshot_copy)
        self.assertEqual(updates, {"memo": "edited memo"})
        self.assertEqual(plan["before"]["memo"], "synthetic original")
        self.assertEqual(plan["after"]["memo"], "edited memo")
        self.assertEqual(self.disk_snapshot(), before_disk)

    def test_metadata_edit_keeps_recorded_money_identity_and_unknown_fields(self) -> None:
        original = self.session(money=987, normal_money=876, overtime_money=111)
        self.seed([original])
        plan = self.prepare_edit({
            "memo": "corrected memo",
            "project_id": "project_a", "subject_id": "subject_a", "material_id": "material_a", "unit_id": "unit_a",
            "condition": {"environment": "図書館", "mood": "4"},
        })
        self.assertEqual(plan["money_changes"], [])
        self.commit(plan)
        saved = self.row()
        for key in ("session_id", "created_at", "start_at", "end_at", "duration_sec", "money", "normal_money", "overtime_money", "custom_data"):
            self.assertEqual(saved[key], original[key], key)
        self.assertEqual(saved["memo"], "corrected memo")
        self.assertEqual(saved["condition"]["environment"], "図書館")
        self.assertEqual(saved["condition"]["custom_condition"], "keep")
        self.assertNotEqual(saved["updated_at"], original["updated_at"])

    def test_legacy_missing_hourly_rate_can_still_edit_memo_without_money_guess(self) -> None:
        original = self.session(money=777, normal_money=650, overtime_money=127)
        original.pop("hourly_rate")
        original.pop("money_policy")
        self.seed([original])
        plan = self.prepare_edit({"memo": "legacy corrected"})
        self.assertEqual(plan["money_changes"], [])
        self.commit(plan)
        saved = self.row()
        self.assertNotIn("hourly_rate", saved)
        self.assertNotIn("money_policy", saved)
        for key in ("money", "normal_money", "overtime_money"):
            self.assertEqual(saved[key], original[key])

    def test_duration_and_rate_recalculate_money(self) -> None:
        self.seed([self.session()])
        plan = self.prepare_edit({"duration_sec": 1800, "hourly_rate": 3000})
        self.assertEqual(plan["after"]["money"], 1500)
        changes = {row["session_id"]: row for row in plan["money_changes"]}
        self.assertEqual(changes[self.session_id]["before_money"], 1000)
        self.assertEqual(changes[self.session_id]["after_money"], 1500)
        self.commit(plan)
        self.assertEqual(self.row()["normal_money"], 1500)
        self.assertEqual(self.row()["overtime_money"], 0)

    def test_recorded_policy_wins_over_current_fallback_policy(self) -> None:
        stored_policy = {"overtime_enabled": False, "threshold_hours": 1.0, "multiplier": 2.0}
        self.seed([self.session(money_policy=stored_policy)])
        plan = study_log_editor.prepare_session_edit(
            self.snapshot(), {"duration_sec": 7200}, root=self.root,
            fallback_policy={"overtime_enabled": True, "threshold_hours": 1.0, "multiplier": 3.0},
        )
        self.assertEqual(plan["after"]["money"], 2000)
        self.commit(plan)
        self.assertEqual(self.row()["money_policy"], stored_policy)

    def test_legacy_recalculation_warns_and_records_fallback_policy(self) -> None:
        original = self.session()
        original.pop("money_policy")
        self.seed([original])
        plan = self.prepare_edit({"duration_sec": 5400})
        self.assertTrue(plan["warnings"])
        self.assertEqual(plan["after"]["money"], 1500)
        self.commit(plan)
        self.assertEqual(self.row()["money_policy"], self.policy)

    def test_missing_rate_blocks_recalculation_without_writing(self) -> None:
        original = self.session()
        original.pop("hourly_rate")
        self.seed([original])
        before = self.disk_snapshot()
        with self.assertRaises(ValueError):
            self.prepare_edit({"duration_sec": 1800})
        self.assertEqual(self.disk_snapshot(), before)

    def test_recalculate_only_changed_and_cumulative_affected_sessions(self) -> None:
        preceding = self.session(session_id="session_before", start_at="2026-08-28T08:00:00", end_at="2026-08-28T09:00:00", money=432, normal_money=432)
        preceding.pop("hourly_rate")
        target = self.session(end_at="2026-08-28T15:00:00", duration_sec=6 * 3600, money=6000, normal_money=6000)
        later = self.session(session_id="session_after", start_at="2026-08-28T16:00:00", end_at="2026-08-28T18:00:00", duration_sec=7200, money=2250, normal_money=1000, overtime_money=1250)
        self.seed([preceding, target, later])
        plan = self.prepare_edit({"duration_sec": 5 * 3600})
        changes = {row["session_id"]: row for row in plan["money_changes"]}
        self.assertEqual(set(changes), {self.session_id, "session_after"})
        self.assertEqual(changes["session_after"]["after_money"], 2000)
        self.commit(plan)
        self.assertEqual(self.row("session_before"), preceding)
        self.assertEqual(self.row("session_after")["money"], 2000)

    def test_affected_later_record_with_missing_rate_blocks_whole_change(self) -> None:
        later = self.session(session_id="session_after", start_at="2026-08-28T11:00:00", end_at="2026-08-28T12:00:00")
        later.pop("hourly_rate")
        self.seed([self.session(), later])
        before = self.disk_snapshot()
        with self.assertRaises(ValueError):
            self.prepare_edit({"duration_sec": 7200})
        self.assertEqual(self.disk_snapshot(), before)

    def test_duration_is_independent_of_wall_clock_for_pause_and_adjustment(self) -> None:
        for duration in (1800, 7200):
            with self.subTest(duration=duration):
                self.seed([self.session()])
                plan = self.prepare_edit({"duration_sec": duration})
                self.commit(plan)
                saved = self.row()
                self.assertEqual(saved["start_at"], "2026-08-28T09:00:00")
                self.assertEqual(saved["end_at"], "2026-08-28T10:00:00")
                self.assertEqual(saved["duration_sec"], duration)

    def test_overnight_edit_retains_start_day_ownership(self) -> None:
        self.seed([self.session()])
        self.commit(self.prepare_edit({
            "start_at": "2026-08-28T23:30:00", "end_at": "2026-08-29T01:30:00", "duration_sec": 5400,
        }))
        self.assertEqual(self.row()["duration_sec"], 5400)
        self.assertEqual(self.rows("2026-08-29"), [])

    def test_date_move_keeps_id_and_envelope_metadata_in_both_files(self) -> None:
        original = self.session()
        self.seed([original], envelope=True)
        destination = self.session(session_id="session_destination", start_at="2026-08-29T12:00:00", end_at="2026-08-29T13:00:00")
        self.seed([destination], "2026-08-29", envelope=True)
        plan = self.prepare_edit({"start_at": "2026-08-29T09:00:00", "end_at": "2026-08-29T10:00:00"})
        self.assertEqual(plan["after"]["session_id"], original["session_id"])
        self.commit(plan)
        self.assertEqual(self.rows(), [])
        moved = self.row(target="2026-08-29")
        self.assertEqual(moved["created_at"], original["created_at"])
        self.assertEqual(moved["custom_data"], original["custom_data"])
        self.assertEqual(len(self.rows("2026-08-29")), 2)
        for day in ("2026-08-28", "2026-08-29"):
            payload = self.read_json(study_log_service.get_study_log_path(day, self.root))
            self.assertEqual(payload["custom_envelope"], {"keep": [1, 2]})

    def test_date_move_creates_destination_without_rekeying_record(self) -> None:
        self.seed([self.session()])
        self.commit(self.prepare_edit({"start_at": "2026-09-02T09:00:00", "end_at": "2026-09-02T10:00:00"}))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.row(target="2026-09-02")["session_id"], self.session_id)

    def test_invalid_numeric_or_datetime_updates_are_rejected_without_writes(self) -> None:
        self.seed([self.session()])
        before = self.disk_snapshot()
        cases = [
            {"duration_sec": 0}, {"duration_sec": -1}, {"duration_sec": "invalid"},
            {"duration_sec": float("nan")}, {"duration_sec": float("inf")},
            {"hourly_rate": -1}, {"hourly_rate": "invalid"}, {"hourly_rate": float("nan")},
            {"start_at": "invalid"}, {"end_at": "invalid"},
            {"end_at": "2026-08-28T08:59:59"},
        ]
        for updates in cases:
            with self.subTest(updates=updates):
                with self.assertRaises(ValueError):
                    self.prepare_edit(updates)
                self.assertEqual(self.disk_snapshot(), before)

    def test_stale_snapshot_is_rejected_before_preview(self) -> None:
        self.seed([self.session()])
        snapshot = self.snapshot()
        self.seed([self.session(memo="external update")])
        before = self.disk_snapshot()
        with self.assertRaises(json_store.JsonConflictError):
            self.prepare_edit({"memo": "stale edit"}, snapshot=snapshot)
        self.assertEqual(self.disk_snapshot(), before)

    def test_stale_preview_rejects_concurrent_same_day_append(self) -> None:
        self.seed([self.session()])
        plan = self.prepare_edit({"memo": "local change"})
        self.seed([self.session(), self.session(session_id="session_concurrent", start_at="2026-08-28T12:00:00", end_at="2026-08-28T13:00:00")])
        before = self.json_snapshot()
        with self.assertRaises(json_store.JsonConflictError):
            study_log_editor.commit_session_change(plan, root=self.root)
        self.assertEqual(self.json_snapshot(), before)

    def test_stale_move_preview_rejects_new_destination_file(self) -> None:
        self.seed([self.session()])
        plan = self.prepare_edit({"start_at": "2026-08-29T09:00:00", "end_at": "2026-08-29T10:00:00"})
        self.seed([self.session(session_id="session_concurrent", start_at="2026-08-29T12:00:00", end_at="2026-08-29T13:00:00")], "2026-08-29")
        before = self.json_snapshot()
        with self.assertRaises(json_store.JsonConflictError):
            study_log_editor.commit_session_change(plan, root=self.root)
        self.assertEqual(self.json_snapshot(), before)

    def test_delete_preview_is_read_only_and_delete_goes_to_recoverable_trash(self) -> None:
        original = self.session()
        self.seed([original])
        before = self.disk_snapshot()
        plan = self.prepare_delete()
        self.assert_public_plan(plan, "delete")
        self.assertEqual(self.disk_snapshot(), before)
        self.commit(plan)
        self.assertEqual(self.rows(), [])
        deleted = study_log_editor.list_deleted_sessions(root=self.root)
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0]["session"]["session_id"], self.session_id)
        self.assertEqual(deleted[0]["session"]["custom_data"], original["custom_data"])
        self.assertTrue(deleted[0]["deleted_at"])
        self.assertEqual(study_log_service.load_all_study_sessions(self.root), [])

    def test_delete_removes_all_note_and_task_reverse_links_but_preserves_content(self) -> None:
        self.seed([self.session(linked_note_ids=["note_a"], linked_task_id="task_a")])
        self.seed_links()
        old_notes = deepcopy(self.note_pages())
        self.delete()
        for before, after in zip(old_notes, self.note_pages()):
            self.assertNotIn(self.session_id, after["linked_study_session_ids"])
            for key in ("title", "body", "content", "linked_flashcard_ids", "linked_reminder_ids", "custom_note", "created_at"):
                self.assertEqual(after[key], before[key])
        self.assertEqual(self.note_pages()[0]["linked_study_session_ids"], ["session_other"])
        for token in ("20260828", "20260829"):
            plan = self.read_json(f"daily_plans/2026/08/{token}.json")
            task = plan["tasks"][0]
            self.assertEqual(task["linked_session_ids"], ["session_other"])
            self.assertEqual(task["status"], "done")
            self.assertEqual(task["custom_task"], {"keep": [1, 2]})
            self.assertEqual(plan["custom_plan"], {"keep": True})

    def test_restore_relinks_existing_targets_without_overwriting_later_content_changes(self) -> None:
        self.seed([self.session(linked_note_ids=["note_a"], linked_task_id="task_a")])
        self.seed_links()
        trash_id = self.delete()
        store = self.read_json("notes_data/notebooks.json")
        for section in store["notebooks"][0]["sections"]:
            for note in section["pages"]:
                note["body"] = "edited after deletion"
                note["content"] = "edited after deletion"
        self.write_json("notes_data/notebooks.json", store)
        task_path = "daily_plans/2026/08/20260828.json"
        task_plan = self.read_json(task_path)
        task_plan["tasks"][0]["status"] = "in_progress"
        self.write_json(task_path, task_plan)
        before_preview = self.disk_snapshot()
        plan = self.prepare_restore(trash_id)
        self.assert_public_plan(plan, "restore")
        self.assertEqual(self.disk_snapshot(), before_preview)
        self.commit(plan)
        self.assertEqual(set(self.row()["linked_note_ids"]), {"note_a", "note_reverse_only"})
        self.assertEqual(self.row()["linked_task_id"], "task_a")
        for note in self.note_pages():
            self.assertIn(self.session_id, note["linked_study_session_ids"])
            self.assertEqual(note["linked_study_session_ids"].count(self.session_id), 1)
            self.assertEqual(note["body"], "edited after deletion")
        self.assertTrue(plan["warnings"])
        self.assertIn(self.session_id, self.read_json(task_path)["tasks"][0]["linked_session_ids"])
        other_task = self.read_json("daily_plans/2026/08/20260829.json")["tasks"][0]
        self.assertNotIn(self.session_id, other_task["linked_session_ids"])
        self.assertEqual(self.read_json(task_path)["tasks"][0]["status"], "in_progress")

    def test_restore_warns_about_removed_targets_and_drops_dangling_forward_links(self) -> None:
        self.seed([self.session(linked_note_ids=["note_a"], linked_task_id="task_a")])
        self.seed_links()
        trash_id = self.delete()
        self.write_json("notes_data/notebooks.json", {"notebooks": []})
        for token in ("20260828", "20260829"):
            self.write_json(f"daily_plans/2026/08/{token}.json", {"date": "2026-08-28", "tasks": []})
        plan = self.prepare_restore(trash_id)
        self.assertTrue(plan["warnings"])
        self.commit(plan)
        self.assertEqual(self.row()["linked_note_ids"], [])
        self.assertFalse(self.row().get("linked_task_id"))

    def test_restore_clears_owned_day_when_task_link_is_removed(self) -> None:
        self.seed([self.session(linked_task_id="deleted_task", linked_task_date="2026-08-28")])
        trash_id = self.delete()
        self.commit(self.prepare_restore(trash_id))
        self.assertEqual((self.row()["linked_task_id"], self.row()["linked_task_date"]), ("", ""))

    def test_restore_task_owner_disambiguates_duplicate_ids_without_linking_both_days(self) -> None:
        self.seed([self.session(linked_task_id="same_task", linked_task_date="2026-08-28")])
        for token in ("20260828", "20260829"):
            self.write_json(f"daily_plans/2026/08/{token}.json", {
                "date": f"{token[:4]}-{token[4:6]}-{token[6:]}",
                "tasks": [{"task_id": "same_task", "linked_session_ids": [self.session_id]}]})
        trash_id = self.delete()
        before_preview = self.disk_snapshot()
        plan = self.prepare_restore(trash_id)
        self.assertEqual(self.disk_snapshot(), before_preview)
        self.commit(plan)
        self.assertEqual(self.row()["linked_task_date"], "2026-08-28")
        self.assertIn(self.session_id, self.read_json("daily_plans/2026/08/20260828.json")["tasks"][0]["linked_session_ids"])
        self.assertEqual(self.read_json("daily_plans/2026/08/20260829.json")["tasks"][0]["linked_session_ids"], [])

    def test_restore_unique_legacy_task_does_not_add_owned_day(self) -> None:
        self.seed([self.session(linked_task_id="task_a")])
        self.seed_links()
        self.commit(self.prepare_restore(self.delete()))
        self.assertEqual(self.row()["linked_task_id"], "task_a")
        self.assertNotIn("linked_task_date", self.row())

    def test_restore_refuses_same_id_already_live_anywhere(self) -> None:
        self.seed([self.session()])
        trash_id = self.delete()
        self.seed([self.session(start_at="2026-08-29T09:00:00", end_at="2026-08-29T10:00:00", memo="collision")], "2026-08-29")
        before = self.disk_snapshot()
        with self.assertRaises(ValueError):
            self.prepare_restore(trash_id)
        self.assertEqual(self.disk_snapshot(), before)

    def test_successful_restore_cannot_be_repeated(self) -> None:
        self.seed([self.session()])
        trash_id = self.delete()
        self.commit(self.prepare_restore(trash_id))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(study_log_editor.list_deleted_sessions(root=self.root), [])
        before = self.disk_snapshot()
        with self.assertRaises(ValueError):
            self.prepare_restore(trash_id)
        self.assertEqual(self.disk_snapshot(), before)

    def test_new_append_never_reuses_a_deleted_record_id(self) -> None:
        self.seed([self.session()])
        self.delete()
        new_record = self.session()
        new_record.pop("session_id")
        study_log_service.append_study_session(new_record, self.root)
        self.assertNotEqual(new_record["session_id"], self.session_id)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(study_log_editor.list_deleted_sessions(root=self.root)[0]["session"]["session_id"], self.session_id)

    def test_moved_record_id_is_reserved_globally_during_append(self) -> None:
        self.seed([self.session()])
        self.commit(self.prepare_edit({"start_at": "2026-08-29T09:00:00", "end_at": "2026-08-29T10:00:00"}))
        new_record = self.session()
        new_record.pop("session_id")
        study_log_service.append_study_session(new_record, self.root)
        self.assertNotEqual(new_record["session_id"], self.session_id)
        before = self.json_snapshot()
        with self.assertRaises(ValueError):
            study_log_service.append_study_session(self.session(memo="explicit duplicate"), self.root)
        self.assertEqual(self.json_snapshot(), before)

    def test_empty_trash_and_pending_queries_do_not_create_files(self) -> None:
        before = self.disk_snapshot()
        self.assertEqual(study_log_editor.list_deleted_sessions(root=self.root), [])
        self.assertEqual(study_log_editor.pending_session_changes(root=self.root), [])
        self.assertEqual(self.disk_snapshot(), before)

    def test_duplicate_live_ids_are_rejected_instead_of_selecting_arbitrary_record(self) -> None:
        self.seed([self.session()])
        self.seed([self.session(start_at="2026-08-29T09:00:00", end_at="2026-08-29T10:00:00")], "2026-08-29")
        before = self.disk_snapshot()
        with self.assertRaises(ValueError):
            self.snapshot()
        self.assertEqual(self.disk_snapshot(), before)

    def test_corrupt_link_file_blocks_delete_without_touching_any_records(self) -> None:
        self.seed([self.session(linked_note_ids=["note_a"])])
        path = self.root / "notes_data/notebooks.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"unfinished":')
        before = self.disk_snapshot()
        with self.assertRaises(json_store.JsonReadError):
            self.prepare_delete()
        self.assertEqual(self.disk_snapshot(), before)

    def test_equal_start_end_is_allowed_for_positive_counter_adjustment(self) -> None:
        self.seed([self.session()])
        plan = self.prepare_edit({"end_at": "2026-08-28T09:00:00"})
        self.assertTrue(plan["warnings"])
        self.assertEqual(plan["money_changes"], [])
        self.commit(plan)
        self.assertEqual(self.row()["duration_sec"], 3600)

    def test_zero_hourly_rate_is_an_explicit_valid_value(self) -> None:
        self.seed([self.session()])
        self.commit(self.prepare_edit({"hourly_rate": 0}))
        self.assertEqual(self.row()["hourly_rate"], 0)
        self.assertEqual(self.row()["money"], 0)

    def test_legacy_restore_can_accept_explicit_missing_hourly_rate(self) -> None:
        original = self.session()
        original.pop("hourly_rate")
        original.pop("money_policy")
        self.seed([original])
        trash_id = self.delete()
        before = self.disk_snapshot()
        with self.assertRaises(ValueError):
            self.prepare_restore(trash_id)
        self.assertEqual(self.disk_snapshot(), before)
        plan = self.prepare_restore(trash_id, hourly_rate=2500)
        self.assertTrue(plan["warnings"])
        self.commit(plan)
        self.assertEqual(self.row()["hourly_rate"], 2500)
        self.assertEqual(self.row()["money"], 2500)
        self.assertEqual(self.row()["created_at"], original["created_at"])
        self.assertEqual(self.row()["custom_data"], original["custom_data"])

    def test_write_failure_rolls_back_all_live_records_byte_exactly(self) -> None:
        self.seed([self.session(linked_note_ids=["note_a"], linked_task_id="task_a")])
        self.seed_links()
        plan = self.prepare_delete()
        before = self.live_snapshot()
        original_save = study_log_editor.save_json_safe
        failed = False

        def failing_save(path, payload, **kwargs):
            nonlocal failed
            if Path(path) == self.root / "notes_data/notebooks.json" and not failed:
                failed = True
                raise OSError("synthetic write failure")
            return original_save(path, payload, **kwargs)

        with patch.object(study_log_editor, "save_json_safe", side_effect=failing_save):
            with self.assertRaises(json_store.JsonStoreError):
                study_log_editor.commit_session_change(plan, root=self.root)
        self.assertTrue(failed)
        self.assertEqual(self.live_snapshot(), before)
        self.assertEqual(study_log_editor.list_deleted_sessions(root=self.root), [])
        self.assertEqual(study_log_editor.pending_session_changes(root=self.root), [])
        self.assertTrue(list((self.root / "backups").rglob("transaction.json")))

    def test_failure_after_new_destination_write_removes_only_that_new_file(self) -> None:
        self.seed([self.session()], envelope=True)
        plan = self.prepare_edit({"start_at": "2026-09-02T09:00:00", "end_at": "2026-09-02T10:00:00"})
        before = self.live_snapshot()
        target = study_log_service.get_study_log_path("2026-09-02", self.root)
        original_save = study_log_editor.save_json_safe
        failed = False

        def failing_save(path, payload, **kwargs):
            nonlocal failed
            result = original_save(path, payload, **kwargs)
            if Path(path) == target and not failed:
                failed = True
                raise OSError("synthetic failure immediately after destination save")
            return result

        with patch.object(study_log_editor, "save_json_safe", side_effect=failing_save):
            with self.assertRaises(json_store.JsonStoreError):
                study_log_editor.commit_session_change(plan, root=self.root)
        self.assertTrue(failed)
        self.assertEqual(self.live_snapshot(), before)
        self.assertFalse(target.exists())
        self.assertEqual(study_log_editor.pending_session_changes(root=self.root), [])

    def test_failure_marking_journal_committed_rolls_back_data_and_trash(self) -> None:
        self.seed([self.session(linked_note_ids=["note_a"], linked_task_id="task_a")])
        self.seed_links()
        plan = self.prepare_delete()
        before = self.live_snapshot()
        original_save = study_log_editor.save_json_safe
        failed = False

        def failing_save(path, payload, **kwargs):
            nonlocal failed
            if Path(path).name == "transaction.json" and isinstance(payload, dict) and payload.get("status") == "committed" and not failed:
                failed = True
                raise OSError("synthetic journal completion failure")
            return original_save(path, payload, **kwargs)

        with patch.object(study_log_editor, "save_json_safe", side_effect=failing_save):
            with self.assertRaises(json_store.JsonStoreError):
                study_log_editor.commit_session_change(plan, root=self.root)
        self.assertTrue(failed)
        self.assertEqual(self.live_snapshot(), before)
        self.assertEqual(study_log_editor.list_deleted_sessions(root=self.root), [])
        self.assertEqual(study_log_editor.pending_session_changes(root=self.root), [])

    def interrupt_delete(self) -> tuple[dict[str, bytes], str]:
        self.seed([self.session(linked_note_ids=["note_a"], linked_task_id="task_a")])
        self.seed_links()
        plan = self.prepare_delete()
        before = self.live_snapshot()
        original_save = study_log_editor.save_json_safe

        def interrupted_save(path, payload, **kwargs):
            if Path(path) == self.root / "notes_data/notebooks.json":
                raise KeyboardInterrupt("synthetic process interruption")
            return original_save(path, payload, **kwargs)

        with patch.object(study_log_editor, "save_json_safe", side_effect=interrupted_save):
            with self.assertRaises(KeyboardInterrupt):
                study_log_editor.commit_session_change(plan, root=self.root)
        pending = study_log_editor.pending_session_changes(root=self.root)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["kind"], "delete")
        self.assertEqual(pending[0]["session_id"], self.session_id)
        return before, pending[0]["operation_id"]

    def test_interrupted_commit_requires_explicit_rollback_and_keeps_read_queries_read_only(self) -> None:
        before, operation_id = self.interrupt_delete()
        self.assertNotEqual(self.live_snapshot(), before)
        interrupted = self.disk_snapshot()
        for _ in range(2):
            self.assertEqual(len(study_log_editor.pending_session_changes(root=self.root)), 1)
            study_log_editor.list_deleted_sessions(root=self.root)
        self.assertEqual(self.disk_snapshot(), interrupted)
        study_log_editor.rollback_pending_session_change(operation_id, root=self.root)
        self.assertEqual(self.live_snapshot(), before)
        self.assertEqual(study_log_editor.pending_session_changes(root=self.root), [])

    def test_pending_operation_blocks_other_commits_without_mutation(self) -> None:
        self.seed([self.session()])
        preexisting_plan = self.prepare_edit({"memo": "cannot apply while recovery is pending"})
        self.interrupt_delete()
        before = self.json_snapshot()
        with self.assertRaises(json_store.JsonStoreError):
            study_log_editor.commit_session_change(preexisting_plan, root=self.root)
        self.assertEqual(self.json_snapshot(), before)
        self.assertEqual(len(study_log_editor.pending_session_changes(root=self.root)), 1)

    def test_recovery_conflict_checks_all_documents_before_reverting_anything(self) -> None:
        _, operation_id = self.interrupt_delete()
        store = self.read_json("notes_data/notebooks.json")
        store["notebooks"][0]["sections"][0]["pages"][0]["body"] = "a later external edit"
        self.write_json("notes_data/notebooks.json", store)
        before = self.json_snapshot()
        with self.assertRaises(json_store.JsonConflictError):
            study_log_editor.rollback_pending_session_change(operation_id, root=self.root)
        self.assertEqual(self.json_snapshot(), before)
        self.assertEqual(self.rows(), [])
        self.assertEqual(len(study_log_editor.pending_session_changes(root=self.root)), 1)

    def test_restore_preview_rejects_id_collision_added_in_another_day(self) -> None:
        self.seed([self.session()])
        trash_id = self.delete()
        plan = self.prepare_restore(trash_id)
        self.seed([self.session(start_at="2026-08-29T09:00:00", end_at="2026-08-29T10:00:00", memo="external collision")], "2026-08-29")
        before = self.json_snapshot()
        with self.assertRaises(json_store.JsonConflictError):
            study_log_editor.commit_session_change(plan, root=self.root)
        self.assertEqual(self.json_snapshot(), before)

    def test_delete_preview_rejects_new_plan_file_with_later_added_reference(self) -> None:
        self.seed([self.session()])
        plan = self.prepare_delete()
        self.write_json("daily_plans/2026/09/20260901.json", {
            "date": "2026-09-01", "tasks": [{"task_id": "new_task", "linked_session_ids": [self.session_id]}],
        })
        before = self.json_snapshot()
        with self.assertRaises(json_store.JsonConflictError):
            study_log_editor.commit_session_change(plan, root=self.root)
        self.assertEqual(self.json_snapshot(), before)

    def test_restore_preview_rejects_same_id_added_to_existing_other_day(self) -> None:
        self.seed([self.session()])
        self.seed([], "2026-08-29")
        trash_id = self.delete()
        plan = self.prepare_restore(trash_id)
        self.seed([self.session(start_at="2026-08-29T09:00:00", end_at="2026-08-29T10:00:00")], "2026-08-29")
        before = self.json_snapshot()
        with self.assertRaises(json_store.JsonConflictError):
            study_log_editor.commit_session_change(plan, root=self.root)
        self.assertEqual(self.json_snapshot(), before)

    def test_classification_preview_rejects_master_changes_before_commit(self) -> None:
        self.seed([self.session()])
        plan = self.prepare_edit({"project_id": "project_a", "subject_id": "subject_a"})
        master = self.read_json("master_data/master.json")
        master["subjects"][0]["project_id"] = "project_elsewhere"
        self.write_json("master_data/master.json", master)
        before = self.json_snapshot()
        with self.assertRaises(json_store.JsonConflictError):
            study_log_editor.commit_session_change(plan, root=self.root)
        self.assertEqual(self.json_snapshot(), before)

    def test_noop_edit_does_not_write_or_create_backup_files(self) -> None:
        self.seed([self.session()])
        before = self.disk_snapshot()
        plan = self.prepare_edit({})
        self.assertEqual(plan["changed_files"], [])
        result = study_log_editor.commit_session_change(plan, root=self.root)
        self.assertEqual(result["changed_files"], [])
        self.assertEqual(self.disk_snapshot(), before)

    def test_protected_fields_cannot_be_modified_through_updates(self) -> None:
        self.seed([self.session()])
        before = self.disk_snapshot()
        for updates in (
            {"session_id": "injected_id"}, {"created_at": "2000-01-01T00:00:00"},
            {"money": 999999}, {"linked_note_ids": ["note_a"]}, {"linked_task_id": "task_a"},
        ):
            with self.subTest(updates=updates):
                with self.assertRaises(ValueError):
                    self.prepare_edit(updates)
                self.assertEqual(self.disk_snapshot(), before)

    def test_legacy_id_is_generated_in_memory_until_explicit_edit_save(self) -> None:
        original = self.session()
        original.pop("session_id")
        self.seed([original], envelope=True)
        before = self.disk_snapshot()
        snapshot = self.snapshot()
        self.assertEqual(snapshot["session"]["session_id"], self.session_id)
        self.assertEqual(self.disk_snapshot(), before)
        self.commit(self.prepare_edit({"memo": "legacy record corrected"}, snapshot=snapshot))
        saved = self.row()
        self.assertEqual(saved["session_id"], self.session_id)
        self.assertEqual(saved["created_at"], original["created_at"])
        self.assertEqual(saved["custom_data"], original["custom_data"])

    def test_delete_then_restore_reprices_later_sessions_with_preserved_policy(self) -> None:
        first = self.session(duration_sec=7 * 3600, end_at="2026-08-28T16:00:00", money=7000, normal_money=7000)
        later = self.session(session_id="session_after", start_at="2026-08-28T16:00:00", end_at="2026-08-28T18:00:00", duration_sec=7200, money=2250, normal_money=1000, overtime_money=1250)
        self.seed([first, later])
        trash_id = self.delete()
        self.assertEqual(self.row("session_after")["money"], 2000)
        plan = self.prepare_restore(trash_id)
        self.assertIn("session_after", {row["session_id"] for row in plan["money_changes"]})
        self.commit(plan)
        self.assertEqual(self.row("session_after")["money"], 2250)
        self.assertEqual(self.row()["money"], 7000)

    def test_append_recalculates_preview_money_under_current_day_totals(self) -> None:
        first = self.session(duration_sec=7 * 3600, end_at="2026-08-28T16:00:00", money=7000, normal_money=7000)
        self.seed([first])
        later = self.session(start_at="2026-08-28T16:00:00", end_at="2026-08-28T18:00:00", duration_sec=7200, money=2000, normal_money=2000)
        later.pop("session_id")
        study_log_service.append_study_session(later, self.root)
        self.assertEqual(self.row(later["session_id"])["money"], 2250)
        self.assertEqual(self.row(later["session_id"])["overtime_money"], 1250)

    def test_nonpositive_legacy_duration_can_be_deleted_without_blocking_other_records(self) -> None:
        for index, duration in enumerate((0, -120), start=1):
            with self.subTest(duration=duration):
                invalid = self.session(duration_sec=duration, money=0, normal_money=0)
                later = self.session(session_id="session_after", start_at="2026-08-28T11:00:00", end_at="2026-08-28T12:00:00")
                self.seed([invalid, later])
                self.commit(self.prepare_delete())
                self.assertEqual(self.rows(), [later])
                self.assertEqual(len(study_log_editor.list_deleted_sessions(root=self.root)), index)

    def test_id_is_not_reused_by_append_during_a_second_date_move(self) -> None:
        self.seed([self.session()])
        self.commit(self.prepare_edit({"start_at": "2026-08-29T09:00:00", "end_at": "2026-08-29T10:00:00"}))
        plan = self.prepare_edit({"start_at": "2026-08-30T09:00:00", "end_at": "2026-08-30T10:00:00"})
        source_path = study_log_service.get_study_log_path("2026-08-29", self.root)
        original_save = study_log_editor.save_json_safe
        appended = self.session(memo="created while original is between files")
        appended.pop("session_id")
        injected = False

        def interleaved_save(path, payload, **kwargs):
            nonlocal injected
            result = original_save(path, payload, **kwargs)
            if Path(path) == source_path and not injected:
                injected = True
                study_log_service.append_study_session(appended, self.root)
            return result

        with patch.object(study_log_editor, "save_json_safe", side_effect=interleaved_save):
            self.commit(plan)
        self.assertTrue(injected)
        self.assertNotEqual(appended["session_id"], self.session_id)
        self.assertEqual(self.row(appended["session_id"])["memo"], appended["memo"])
        self.assertEqual(self.row(target="2026-08-30")["session_id"], self.session_id)
        self.assertEqual(self.rows("2026-08-29"), [])

    def test_optional_conditions_can_be_cleared_without_erasing_unknown_fields(self) -> None:
        self.seed([self.session(condition={"effort": 7, "sleep_hours": 6.5, "mood": "4", "environment": "自宅", "custom": {"keep": True}})])
        plan = self.prepare_edit({"condition": {"effort": None, "sleep_hours": None, "mood": None, "environment": ""}})
        self.assertEqual(plan["after"]["condition"], {"custom": {"keep": True}})
        self.assertEqual(plan["money_changes"], [])
        self.commit(plan)
        self.assertEqual(self.row()["condition"], {"custom": {"keep": True}})

    def test_deleted_records_are_excluded_from_live_loads_and_csv_export(self) -> None:
        from app_core.export_service import build_export_bundle

        self.seed([self.session()])
        trash_id = self.delete()
        before = self.json_snapshot()
        self.assertEqual(study_log_service.load_all_study_sessions(self.root), [])
        bundle = build_export_bundle(root=self.root)
        table = next(item for item in bundle.tables if item.key == "study_sessions")
        self.assertEqual(table.row_count, 0)
        self.assertEqual(self.json_snapshot(), before)
        self.commit(self.prepare_restore(trash_id))
        bundle = build_export_bundle(root=self.root)
        table = next(item for item in bundle.tables if item.key == "study_sessions")
        self.assertEqual(table.row_count, 1)
        self.assertIn(self.session_id, table.data.decode("utf-8-sig"))

    def test_id_reservation_survives_interrupted_move_and_original_day_append(self) -> None:
        self.seed([self.session()])
        self.commit(self.prepare_edit({"start_at": "2026-08-29T09:00:00", "end_at": "2026-08-29T10:00:00"}))
        plan = self.prepare_edit({"start_at": "2026-08-30T09:00:00", "end_at": "2026-08-30T10:00:00"})
        source_path = study_log_service.get_study_log_path("2026-08-29", self.root)
        original_save = study_log_editor.save_json_safe

        def interrupted_save(path, payload, **kwargs):
            result = original_save(path, payload, **kwargs)
            if Path(path) == source_path:
                raise KeyboardInterrupt("synthetic interrupted second move")
            return result

        with patch.object(study_log_editor, "save_json_safe", side_effect=interrupted_save):
            with self.assertRaises(KeyboardInterrupt):
                study_log_editor.commit_session_change(plan, root=self.root)
        appended = self.session(memo="created after interruption")
        appended.pop("session_id")
        study_log_service.append_study_session(appended, self.root)
        self.assertNotEqual(appended["session_id"], self.session_id)
        pending = study_log_editor.pending_session_changes(root=self.root)
        self.assertEqual(len(pending), 1)
        study_log_editor.rollback_pending_session_change(pending[0]["operation_id"], root=self.root)
        self.assertEqual(self.row(target="2026-08-29")["session_id"], self.session_id)
        self.assertEqual(self.row(appended["session_id"])["memo"], appended["memo"])
        self.assertEqual(self.rows("2026-08-30"), [])
        self.assertEqual(study_log_editor.pending_session_changes(root=self.root), [])


class RestoredTaskOwnershipTests(unittest.TestCase):
    @staticmethod
    def plan(day, task_id="task", **updates):
        document = {"path": f"daily_plans/{day[:4]}/{day[5:7]}/{day.replace('-', '')}.json",
                    "after": {"date": day, "unknown": True, "tasks": [{"task_id": task_id, "linked_session_ids": [], "keep": "body"}]}}
        document.update(updates)
        return document

    @staticmethod
    def restore(row, plans, backlinks=()):
        warnings = []
        study_log_editor._restore_task_link(row, {"task_backlinks": list(backlinks)}, plans, warnings)
        return warnings

    def test_changed_id_sets_actual_owner_with_or_without_old_date(self):
        for has_date in (False, True):
            with self.subTest(has_date=has_date):
                row = {"session_id": "session", "linked_task_id": "gone", "unknown": {"keep": 1}}
                if has_date:
                    row["linked_task_date"] = "2026-08-28"
                plan = self.plan("2026-08-29", "fallback")
                self.restore(row, [plan], [{"task_id": "fallback", "source_file": plan["path"]}])
                self.assertEqual((row["linked_task_id"], row["linked_task_date"]), ("fallback", "2026-08-29"))
                self.assertEqual(row["unknown"], {"keep": 1})
                self.assertEqual(plan["after"]["tasks"][0]["linked_session_ids"], ["session"])

    def test_unique_id_updates_existing_stale_or_empty_date(self):
        for owner in ("2026-08-28", "", "invalid"):
            row = {"session_id": "session", "linked_task_id": "task", "linked_task_date": owner}
            self.restore(row, [self.plan("2026-08-29")])
            self.assertEqual(row["linked_task_date"], "2026-08-29")

    def test_duplicate_id_uses_unique_backlink_source_and_records_owner(self):
        first, second = self.plan("2026-08-28"), self.plan("2026-08-29")
        before = deepcopy(first)
        row = {"session_id": "session", "linked_task_id": "task"}
        self.restore(row, [first, second], [{"task_id": "task", "source_file": second["path"]}])
        self.assertEqual((row["linked_task_id"], row["linked_task_date"]), ("task", "2026-08-29"))
        self.assertEqual(first, before)
        self.assertEqual(second["after"]["tasks"][0]["linked_session_ids"], ["session"])

    def test_ambiguous_duplicate_id_never_links_multiple_plans(self):
        for with_sources in (False, True):
            plans = [self.plan("2026-08-28"), self.plan("2026-08-29")]
            before = deepcopy(plans)
            row = {"session_id": "session", "linked_task_id": "task"}
            backlinks = ([{"task_id": "task", "source_file": plan["path"]} for plan in plans]
                         if with_sources else [{"task_id": "task"}])
            self.assertTrue(self.restore(row, plans, backlinks))
            self.assertEqual(row["linked_task_id"], "")
            self.assertNotIn("linked_task_date", row)
            self.assertEqual(plans, before)

    def test_conflicting_owner_and_backlink_are_not_guessed(self):
        plans = [self.plan("2026-08-28"), self.plan("2026-08-29")]
        before = deepcopy(plans)
        row = {"session_id": "session", "linked_task_id": "task", "linked_task_date": "2026-08-28"}
        self.assertTrue(self.restore(row, plans, [{"task_id": "task", "source_file": plans[1]["path"]}]))
        self.assertEqual((row["linked_task_id"], row["linked_task_date"]), ("", ""))
        self.assertEqual(plans, before)

    def test_duplicate_id_in_same_plan_remains_ambiguous(self):
        plan = self.plan("2026-08-28")
        plan["after"]["tasks"].append(deepcopy(plan["after"]["tasks"][0]))
        before = deepcopy(plan)
        row = {"session_id": "session", "linked_task_id": "task", "linked_task_date": "2026-08-28"}
        self.assertTrue(self.restore(row, [plan], [{"task_id": "task", "source_file": plan["path"]}]))
        self.assertEqual(row["linked_task_id"], "")
        self.assertEqual(plan, before)

    def test_invalid_owner_path_or_body_day_is_skipped_without_plan_writes(self):
        for defect in ("body", "missing_body", "directory", "invalid_date"):
            with self.subTest(defect=defect):
                plan = self.plan("2026-08-28")
                if defect == "body":
                    plan["after"]["date"] = "2026-08-29"
                elif defect == "missing_body":
                    plan["after"].pop("date")
                elif defect == "directory":
                    plan["path"] = "daily_plans/2026/07/20260828.json"
                else:
                    plan["path"] = "daily_plans/2026/08/20260899.json"
                before = deepcopy(plan)
                row = {"session_id": "session", "linked_task_id": "task", "linked_task_date": "2026-08-28"}
                self.assertTrue(self.restore(row, [plan]))
                self.assertEqual((row["linked_task_id"], row["linked_task_date"]), ("", ""))
                self.assertEqual(plan, before)

    def test_backlink_source_does_not_retarget_same_id_to_another_file(self):
        plan = self.plan("2026-08-29", "fallback")
        before = deepcopy(plan)
        row = {"session_id": "session", "linked_task_id": "gone"}
        self.restore(row, [plan], [{"task_id": "fallback", "source_file": "daily_plans/2026/08/20260828.json"}])
        self.assertEqual(row["linked_task_id"], "")
        self.assertEqual(plan, before)


if __name__ == "__main__":
    unittest.main()
