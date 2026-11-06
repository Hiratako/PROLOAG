"""Temporary synthetic proposal adoption/rollback tests; never personal data."""
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core import consultation_proposal_service as service
from app_core import json_store
from app_core.consultation_proposal_contract import ProposalError, parse_proposal_payload
from app_core.json_operations import pending_json_operations, rollback_json_operation


def candidates(title="Proposal", text="Study chapter 2"):
    return parse_proposal_payload(json.dumps({"schema_version": 1, "proposals": [{"title": title, "text": text}]}))


class ProposalServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inbox_path = self.root / "settings/consultation_inbox.json"
        self.context_path = self.root / "settings/learning_context.json"
        self.plan_path = self.root / "daily_plans/2026/09/20260916.json"
        self.master_path = self.root / "master_data/master.json"
        self.write(self.master_path, {"projects": [], "subjects": [], "materials": [
            {"material_id": "material_1", "name": "Book", "is_active": True},
            {"material_id": "material_off", "name": "Inactive", "is_active": False}]})
        self.proposal_id = candidates()[0]["proposal_id"]
        service.import_proposals(self.root, candidates(), expected_revision=None)

    def write(self, path, data):
        json_store.load_json_safe(path, None)
        json_store.save_json_safe(path, data)

    def read(self, path):
        return json_store.load_json_safe(path, None)

    def snapshot(self):
        return service.load_inbox_snapshot(self.root)

    def prepare(self, kind="next_step", **kwargs):
        return service.prepare_application(self.root, self.proposal_id, kind=kind, text="Approved user-edited text",
            title="Approved title", material_id="material_1" if kind == "next_step" else "",
            target_date=date(2026, 9, 16) if kind == "daily_memo" else None,
            expected_revision=self.snapshot()["revision"], **kwargs)

    def current_entry(self):
        return self.snapshot()["data"]["proposals"][self.proposal_id]

    def existing_context(self):
        stamp = "2026-01-01T00:00:00+09:00"
        return {"schema_version": 1, "unknown_outer": {"keep": True}, "materials": {
            "material_1": {"material_id": "material_1", "next_step": "Original note", "links": [
                {"link_id": "link_1", "title": "Private source", "url": "https://docs.google.com/document/d/fake_source/edit", "custom": 3}],
                "created_at": stamp, "updated_at": stamp, "unknown_entry": [1, 2]},
            "other_material": {"material_id": "other_material", "next_step": "Other original", "links": [],
                "created_at": stamp, "updated_at": stamp}}}

    def test_import_duplicate_does_not_create_new_items_and_ignores_injected_state(self):
        snapshot = self.snapshot()
        result = service.import_proposals(self.root, candidates(), expected_revision=snapshot["revision"])
        self.assertEqual(result, {"added": 0, "duplicates": 1})
        self.assertEqual(self.snapshot()["data"], snapshot["data"])
        injected = candidates("Other")
        injected[0].update(status="applied", application={"evil": True}, target_path="health_logs/private.json")
        service.import_proposals(self.root, injected, expected_revision=self.snapshot()["revision"])
        saved = self.snapshot()["data"]["proposals"][injected[0]["proposal_id"]]
        self.assertEqual(saved["status"], "pending")
        self.assertNotIn("application", saved)
        self.assertNotIn("target_path", saved)

    def test_import_and_status_keep_existing_unknown_fields(self):
        data = self.snapshot()["data"]
        data["custom"] = "outer"
        data["proposals"][self.proposal_id]["custom"] = {"x": True}
        self.write(self.inbox_path, data)
        service.set_proposal_status(self.root, self.proposal_id, "deferred", expected_revision=self.snapshot()["revision"])
        service.import_proposals(self.root, candidates("Another"), expected_revision=self.snapshot()["revision"])
        saved = self.snapshot()["data"]
        self.assertEqual(saved["custom"], "outer")
        self.assertEqual(saved["proposals"][self.proposal_id]["custom"], {"x": True})

    def test_stale_inbox_rejects_status_import_and_prepare(self):
        old = self.snapshot()["revision"]
        service.set_proposal_status(self.root, self.proposal_id, "deferred", expected_revision=old)
        before = self.inbox_path.read_bytes()
        for action in (
            lambda: service.set_proposal_status(self.root, self.proposal_id, "rejected", expected_revision=old),
            lambda: service.import_proposals(self.root, candidates("new"), expected_revision=old),
            lambda: service.prepare_application(self.root, self.proposal_id, kind="next_step", text="edit", material_id="material_1", expected_revision=old),
        ):
            with self.assertRaises(json_store.JsonConflictError):
                action()
        self.assertEqual(self.inbox_path.read_bytes(), before)

    def test_next_step_only_changes_selected_entry_text_metadata_and_roundtrip_undo(self):
        original = self.existing_context()
        self.write(self.context_path, original)
        preview = self.prepare()
        self.assertEqual(preview["before"], "Original note")
        self.assertEqual(preview["after"], "Approved user-edited text")
        result = service.approve_application(self.root, preview)
        self.assertEqual(result["status"], "applied")
        actual = self.read(self.context_path)
        self.assertEqual(actual["materials"]["material_1"]["links"], original["materials"]["material_1"]["links"])
        self.assertEqual(actual["materials"]["material_1"]["unknown_entry"], [1, 2])
        self.assertEqual(actual["materials"]["other_material"], original["materials"]["other_material"])
        self.assertEqual(actual["unknown_outer"], original["unknown_outer"])
        self.assertEqual(result["application"]["after_revision"], json_store.observed_json_revision(self.context_path))
        undo = service.prepare_revert(self.root, self.proposal_id, expected_revision=self.snapshot()["revision"])
        reverted = service.revert_application(self.root, undo)
        self.assertEqual(reverted["status"], "reverted")
        self.assertEqual(self.read(self.context_path), original)
        self.assertEqual(self.current_entry()["proposal"]["text"], "Study chapter 2")

    def test_daily_append_preserves_raw_legacy_tasks_unknowns_and_undo(self):
        original = {"date": "2026-09-16", "tasks": [{"title": "Legacy no ID", "status": "paused", "custom": 7}, "unknown row"],
                    "target_total_minutes": 99, "custom": {"nested": 3}, "updated_at": "unchanged"}
        self.write(self.plan_path, original)
        preview = self.prepare("daily_memo")
        result = service.approve_application(self.root, preview)
        saved = self.read(self.plan_path)
        memo = saved.pop("consultation_memos")[0]
        self.assertEqual(saved, original)
        self.assertEqual(memo, {"proposal_id": self.proposal_id, "title": "Approved title", "text": "Approved user-edited text", "adopted_at": result["application"]["applied_at"]})
        undo = service.prepare_revert(self.root, self.proposal_id, expected_revision=self.snapshot()["revision"])
        service.revert_application(self.root, undo)
        self.assertEqual(self.read(self.plan_path), original)

    def test_existing_daily_memos_with_unknowns_are_preserved(self):
        old = {"proposal_id": "proposal_" + "a" * 64, "title": "old", "text": "old note", "adopted_at": "2026-01-01T00:00:00", "custom": True}
        self.write(self.plan_path, {"date": "2026-09-16", "consultation_memos": [old]})
        service.approve_application(self.root, self.prepare("daily_memo"))
        self.assertEqual(self.read(self.plan_path)["consultation_memos"][0], old)
        undo = service.prepare_revert(self.root, self.proposal_id, expected_revision=self.snapshot()["revision"])
        service.revert_application(self.root, undo)
        self.assertEqual(self.read(self.plan_path)["consultation_memos"], [old])

    def test_new_target_file_undo_keeps_container_without_adopted_field(self):
        for kind in ("next_step", "daily_memo"):
            with self.subTest(kind=kind):
                # A distinct inbox allows independent history without resetting state.
                candidate = candidates(kind)
                service.import_proposals(self.root, candidate, expected_revision=self.snapshot()["revision"])
                identifier = candidate[0]["proposal_id"]
                preview = service.prepare_application(self.root, identifier, kind=kind, text="new", material_id="material_1",
                    target_date="2026-09-16", expected_revision=self.snapshot()["revision"])
                service.approve_application(self.root, preview)
                undo = service.prepare_revert(self.root, identifier, expected_revision=self.snapshot()["revision"])
                service.revert_application(self.root, undo)
                if kind == "next_step":
                    self.assertEqual(self.read(self.context_path)["materials"], {})
                else:
                    self.assertNotIn("consultation_memos", self.read(self.plan_path))
                    self.assertEqual(self.read(self.plan_path)["tasks"], [])

    def test_duplicate_approve_and_revert_are_idempotent(self):
        preview = self.prepare("daily_memo")
        first = service.approve_application(self.root, preview)
        inbox_bytes, plan_bytes = self.inbox_path.read_bytes(), self.plan_path.read_bytes()
        self.assertEqual(service.approve_application(self.root, preview), first)
        self.assertEqual((self.inbox_path.read_bytes(), self.plan_path.read_bytes()), (inbox_bytes, plan_bytes))
        undo = service.prepare_revert(self.root, self.proposal_id, expected_revision=self.snapshot()["revision"])
        first_undo = service.revert_application(self.root, undo)
        inbox_bytes, plan_bytes = self.inbox_path.read_bytes(), self.plan_path.read_bytes()
        self.assertEqual(service.revert_application(self.root, undo), first_undo)
        self.assertEqual((self.inbox_path.read_bytes(), self.plan_path.read_bytes()), (inbox_bytes, plan_bytes))

    def test_apply_conflicts_on_target_master_or_inbox_change(self):
        for which in ("target", "master", "inbox"):
            with self.subTest(which=which):
                preview = self.prepare()
                if which == "target":
                    self.write(self.context_path, self.existing_context())
                elif which == "master":
                    master = self.read(self.master_path)
                    master["custom"] = True
                    self.write(self.master_path, master)
                else:
                    service.set_proposal_status(self.root, self.proposal_id, "deferred", expected_revision=self.snapshot()["revision"])
                inbox_before = self.inbox_path.read_bytes()
                target_before = self.context_path.read_bytes() if self.context_path.exists() else None
                with self.assertRaises(json_store.JsonConflictError):
                    service.approve_application(self.root, preview)
                self.assertEqual(self.inbox_path.read_bytes(), inbox_before)
                self.assertEqual(self.context_path.read_bytes() if self.context_path.exists() else None, target_before)

    def test_material_deleted_inactive_reference_and_bad_dates_are_rejected(self):
        for kind, material, day in (("next_step", "missing", None), ("next_step", "material_off", None),
                                    ("reference", "", None), ("daily_memo", "", "../../outside"), ("daily_memo", "", "2026-02-30")):
            with self.subTest(kind=kind, material=material, day=day):
                with self.assertRaises(ProposalError):
                    service.prepare_application(self.root, self.proposal_id, kind=kind, text="edit", material_id=material,
                                                target_date=day, expected_revision=self.snapshot()["revision"])

    def test_material_deleted_or_disabled_after_preview_rejects_adoption(self):
        baseline = self.read(self.master_path)
        for change in ("delete", "disable"):
            with self.subTest(change=change):
                self.write(self.master_path, baseline)
                preview = self.prepare()
                changed = deepcopy(baseline)
                if change == "delete":
                    changed["materials"] = []
                else:
                    changed["materials"][0]["is_active"] = False
                self.write(self.master_path, changed)
                with self.assertRaises(json_store.JsonConflictError):
                    service.approve_application(self.root, preview)
                self.assertFalse(self.context_path.exists())
                self.assertEqual(self.current_entry()["status"], "pending")

    def test_external_change_during_prepare_is_detected_before_preview_return(self):
        snapshot = self.snapshot()
        original_recheck = service._recheck_revisions
        def changed(root, revisions):
            master = self.read(self.master_path)
            master["changed_during_preview"] = True
            self.write(self.master_path, master)
            original_recheck(root, revisions)
        with patch.object(service, "_recheck_revisions", side_effect=changed):
            with self.assertRaises(json_store.JsonConflictError):
                service.prepare_application(self.root, self.proposal_id, kind="next_step", text="edit", material_id="material_1",
                                            expected_revision=snapshot["revision"])

    def test_preview_tampering_or_cross_root_cannot_write(self):
        original = self.prepare("daily_memo")
        for path, value in (("text", "tampered"), ("operation_id", "0" * 32), ("_target_document", {"evil": True}),
                            ("revisions", {}), ("target", {"path": "health_logs/secret.json"})):
            preview = deepcopy(original)
            preview[path] = value
            with self.assertRaises(ProposalError):
                service.approve_application(self.root, preview)
        with self.assertRaises(ProposalError):
            service.approve_application(self.root / "other", original)
        self.assertFalse(self.plan_path.exists())
        self.assertEqual(self.current_entry()["status"], "pending")

    def test_undo_rejects_later_target_edit_and_later_inbox_edit(self):
        preview = self.prepare("daily_memo")
        service.approve_application(self.root, preview)
        undo = service.prepare_revert(self.root, self.proposal_id, expected_revision=self.snapshot()["revision"])
        current = self.read(self.plan_path)
        current["user_later_edit"] = "preserve"
        self.write(self.plan_path, current)
        with self.assertRaises(json_store.JsonConflictError):
            service.prepare_revert(self.root, self.proposal_id, expected_revision=self.snapshot()["revision"])
        with self.assertRaises(json_store.JsonConflictError):
            service.revert_application(self.root, undo)
        self.assertEqual(self.read(self.plan_path), current)
        self.assertEqual(self.current_entry()["status"], "applied")

    def test_undo_stale_inbox_preserves_unrelated_new_proposal(self):
        service.approve_application(self.root, self.prepare("daily_memo"))
        undo = service.prepare_revert(self.root, self.proposal_id, expected_revision=self.snapshot()["revision"])
        service.import_proposals(self.root, candidates("New later proposal"), expected_revision=self.snapshot()["revision"])
        before = self.inbox_path.read_bytes(), self.plan_path.read_bytes()
        with self.assertRaises(json_store.JsonConflictError):
            service.revert_application(self.root, undo)
        self.assertEqual((self.inbox_path.read_bytes(), self.plan_path.read_bytes()), before)

    def test_rejected_requires_pending_and_applied_reverted_are_immutable_states(self):
        service.set_proposal_status(self.root, self.proposal_id, "rejected", expected_revision=self.snapshot()["revision"])
        with self.assertRaises(ProposalError):
            self.prepare()
        service.set_proposal_status(self.root, self.proposal_id, "pending", expected_revision=self.snapshot()["revision"])
        service.approve_application(self.root, self.prepare())
        with self.assertRaises(ProposalError):
            service.set_proposal_status(self.root, self.proposal_id, "pending", expected_revision=self.snapshot()["revision"])
        undo = service.prepare_revert(self.root, self.proposal_id, expected_revision=self.snapshot()["revision"])
        service.revert_application(self.root, undo)
        with self.assertRaises(ProposalError):
            service.set_proposal_status(self.root, self.proposal_id, "pending", expected_revision=self.snapshot()["revision"])

    def test_malformed_existing_plan_or_memo_is_not_normalized_or_overwritten(self):
        for invalid in (None, [], {"date": "wrong"}, {"date": "2026-09-16", "consultation_memos": None},
                        {"date": "2026-09-16", "consultation_memos": [{"text": "missing id"}]}):
            self.write(self.plan_path, invalid)
            before = self.plan_path.read_bytes()
            with self.assertRaises(ProposalError):
                self.prepare("daily_memo")
            self.assertEqual(self.plan_path.read_bytes(), before)

    def test_failure_on_second_live_write_rolls_back_both_and_retries(self):
        self.write(self.context_path, self.existing_context())
        preview = self.prepare()
        before = (self.context_path.read_bytes(), self.inbox_path.read_bytes())
        original_save = json_store.save_json_safe
        failed = False
        def fail_once(path, data, *args, **kwargs):
            nonlocal failed
            if Path(path) == self.inbox_path and json_store._committing_operation.get() is not None and not failed:
                failed = True
                raise OSError("synthetic disk failure")
            return original_save(path, data, *args, **kwargs)
        with patch.object(json_store, "save_json_safe", side_effect=fail_once):
            with self.assertRaises(json_store.JsonStoreError):
                service.approve_application(self.root, preview)
        self.assertTrue(failed)
        self.assertEqual((self.context_path.read_bytes(), self.inbox_path.read_bytes()), before)
        self.assertEqual(pending_json_operations(self.root), [])
        service.approve_application(self.root, preview)
        self.assertEqual(self.current_entry()["status"], "applied")

    def test_process_interruption_requires_existing_recovery_then_same_retry(self):
        self.write(self.context_path, self.existing_context())
        preview = self.prepare()
        before = (self.context_path.read_bytes(), self.inbox_path.read_bytes())
        original_save = json_store.save_json_safe
        def interrupted(path, data, *args, **kwargs):
            if Path(path) == self.inbox_path and json_store._committing_operation.get() is not None:
                raise KeyboardInterrupt("synthetic interruption")
            return original_save(path, data, *args, **kwargs)
        with patch.object(json_store, "save_json_safe", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                service.approve_application(self.root, preview)
        self.assertEqual(len(pending_json_operations(self.root)), 1)
        with self.assertRaises(json_store.JsonStoreError):
            self.snapshot()
        rollback_json_operation(preview["operation_id"], self.root)
        self.assertEqual((self.context_path.read_bytes(), self.inbox_path.read_bytes()), before)
        service.approve_application(self.root, preview)
        self.assertEqual(self.current_entry()["status"], "applied")

    def test_no_out_of_scope_json_is_touched_by_import_apply_or_revert(self):
        paths = [self.root / relative for relative in ("study_logs/2026/09/20260916.json", "health_logs/2026/09/20260916.json",
                 "diary_entries/2026/09/20260916.json", "settings/substance_profiles.json", "notes_data/notebooks.json")]
        for path in paths:
            self.write(path, {"sentinel": str(path.name), "unknown": [1, 2]})
        before = {path: path.read_bytes() for path in paths}
        service.approve_application(self.root, self.prepare("daily_memo"))
        service.revert_application(self.root, service.prepare_revert(self.root, self.proposal_id, expected_revision=self.snapshot()["revision"]))
        self.assertEqual({path: path.read_bytes() for path in paths}, before)


if __name__ == "__main__":
    unittest.main()
