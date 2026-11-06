from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core import json_store
from app_core import legacy_study_import as migration


class LegacyStudyImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag_legacy_import_test_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        patcher = patch.object(json_store, "PROJECT_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def read(self, relative):
        return json.loads((self.root / relative).read_text(encoding="utf-8"))

    def row(self, number=2, start="2025-01-01T09:00:00.123456", duration=1200.234567,
            memo="架空の学習記録", warnings=False):
        end = datetime.fromisoformat(start) + timedelta(seconds=duration)
        session = {"start_at": start, "end_at": end.isoformat(), "duration_sec": duration,
                   "hourly_rate": 2501.875, "money": 833.9581234, "memo": memo,
                   "condition": {"legacy_sleep_band": ">=8h"}}
        fingerprint = hashlib.sha256(json.dumps(session, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return {"row_number": number, "fingerprint": fingerprint, "session": session,
                "legacy_topic": "medical", "raw": {"duration_sec": str(duration),
                "memo": memo, "__src": r"C:\private-user\old-app\study_logs\20250101.json"},
                "issues": [{"severity": "warning", "code": "source_overlap", "message": "要確認"}] if warnings else []}

    def diagnosis(self, *rows, source="a"):
        return {"schema_version": 1, "source_sha256": source * 64, "summary_sha256": None,
                "rows": list(rows or [self.row()]), "issues": []}

    def preview(self, diagnosis=None, **kwargs):
        return migration.build_import_preview(diagnosis or self.diagnosis(), self.root, **kwargs)

    def existing(self, row=None, session_id="session_20250101_001", **updates):
        record = deepcopy((row or self.row())["session"])
        record.update(session_id=session_id, money=9876.54321, created_at="2025-01-01T10:00:00",
                      unknown={"preserved": [1, 2]}, **updates)
        return record

    def master(self):
        return {"projects": [{"project_id": "p", "name": "架空のプロジェクト", "is_active": True}],
                "subjects": [{"subject_id": "s", "project_id": "p", "name": "架空の科目"}],
                "materials": [{"material_id": "m", "project_id": "p", "subject_id": "s", "name": "架空の教材"}],
                "units": [{"unit_id": "u", "project_id": "p", "subject_id": "s", "material_id": "m", "name": "架空の単元"}]}

    def test_preview_does_not_create_any_file_or_directory(self):
        result = self.preview()
        self.assertEqual(result["summary"]["ready_count"], 1)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_first_import_preserves_precision_and_provenance_without_absolute_source_path(self):
        row = self.row()
        preview = self.preview(self.diagnosis(row))
        self.assertNotIn("__src", preview["rows"][0]["raw"])
        result = migration.commit_import(preview, self.root)
        saved = self.read("study_logs/2025/01/20250101.json")[0]
        self.assertEqual(result["added_count"], 1)
        self.assertEqual(saved["duration_sec"], row["session"]["duration_sec"])
        self.assertEqual(saved["money"], row["session"]["money"])
        self.assertEqual(saved["hourly_rate"], row["session"]["hourly_rate"])
        self.assertEqual(saved["condition"], row["session"]["condition"])
        self.assertNotIn("sleep_hours", saved["condition"])
        self.assertEqual(saved["import_metadata"]["legacy_source_name"], "20250101.json")
        self.assertNotIn("private-user", json.dumps(saved, ensure_ascii=False))
        self.assertEqual(saved["created_at"], saved["import_metadata"]["imported_at"])
        self.assertNotEqual(saved["created_at"][:10], "2025-01-01")
        self.assertEqual(self.read("backups/study_session_ids.json"), [saved["session_id"]])
        self.assertIn(row["fingerprint"], self.read(migration.LEDGER_PATH)["records"])
        journal = self.read(f"backups/json_operations/{result['operation_id']}/transaction.json")
        self.assertEqual(journal["status"], "committed")
        self.assertEqual(len(journal["documents"]), 3)

    def test_same_operation_replay_returns_no_new_addition(self):
        preview = self.preview()
        first = migration.commit_import(preview, self.root)
        before = (self.root / "study_logs/2025/01/20250101.json").read_bytes()
        again = migration.commit_import(preview, self.root)
        self.assertEqual(first["added_count"], 1)
        self.assertTrue(again["replayed"])
        self.assertEqual(again["added_count"], 0)
        self.assertEqual(before, (self.root / "study_logs/2025/01/20250101.json").read_bytes())

    def test_fresh_preview_same_content_different_file_hash_and_row_number_is_skipped(self):
        row = self.row()
        migration.commit_import(self.preview(self.diagnosis(row)), self.root)
        row["row_number"] = 55
        second = self.preview(self.diagnosis(row, source="b"))
        self.assertEqual(second["summary"]["existing_count"], 1)
        self.assertEqual(second["summary"]["ready_count"], 0)
        self.assertEqual(migration.commit_import(second, self.root)["changed_files"], [])

    def test_edited_import_still_deduplicates_by_fingerprint(self):
        diagnosis = self.diagnosis()
        migration.commit_import(self.preview(diagnosis), self.root)
        path = "study_logs/2025/01/20250101.json"
        rows = self.read(path)
        rows[0]["memo"] = "取り込み後の修正"
        self.write(path, rows)
        self.assertEqual(self.preview(diagnosis)["rows"][0]["status"], "existing")

    def test_deleted_import_is_not_resurrected(self):
        diagnosis = self.diagnosis()
        migration.commit_import(self.preview(diagnosis), self.root)
        self.write("study_logs/2025/01/20250101.json", [])
        second = self.preview(diagnosis)
        self.assertEqual(second["rows"][0]["status"], "existing")
        self.assertEqual(migration.commit_import(second, self.root)["added_count"], 0)

    def test_exact_existing_identity_ignores_changed_master_and_money(self):
        existing = self.existing(project_id="old", memo=self.row()["session"]["memo"])
        existing["start_at"] = existing["start_at"].replace("T", " ")
        self.write("study_logs/2025/01/20250101.json", [existing])
        preview = self.preview()
        self.assertEqual(preview["rows"][0]["status"], "existing")
        self.assertEqual(preview["rows"][0]["existing_candidates"][0]["session_id"], existing["session_id"])

    def test_similar_changed_memo_is_held_until_acknowledged(self):
        existing = self.existing(memo="以前に修正したメモ")
        self.write("study_logs/2025/01/20250101.json", [existing])
        self.assertEqual(self.preview()["rows"][0]["status"], "review")
        acknowledged = self.preview(acknowledged_rows=[2])
        self.assertEqual(acknowledged["rows"][0]["status"], "ready")
        self.assertEqual(acknowledged["rows"][0]["session"]["session_id"], "session_20250101_002")
        migration.commit_import(acknowledged, self.root)
        self.assertEqual(self.read("study_logs/2025/01/20250101.json")[0], existing)

    def test_multiday_existing_interval_is_not_missed_by_calendar_filter(self):
        existing = self.existing(start_at="2024-12-28T18:00:00", end_at="2025-01-02T19:00:00",
                                 memo="架空の長時間記録")
        self.write("study_logs/2024/12/20241228.json", [existing])
        preview = self.preview()
        self.assertEqual(preview["rows"][0]["status"], "review")
        self.assertEqual(preview["rows"][0]["existing_candidates"][0]["session_id"], existing["session_id"])

    def test_minimum_datetime_is_a_clear_row_error_without_side_effects(self):
        row = self.row(start="0001-01-01T00:00:00", duration=1)
        preview = self.preview(self.diagnosis(row))
        self.assertEqual(preview["rows"][0]["status"], "error")
        self.assertIn("4桁", preview["rows"][0]["reason"])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_maximum_datetime_does_not_overflow_interval_query(self):
        row = self.row(start="9999-12-31T23:59:58", duration=1.999999)
        preview = self.preview(self.diagnosis(row))
        self.assertEqual(preview["rows"][0]["status"], "ready")
        self.assertTrue(preview["rows"][0]["session"]["session_id"].startswith("session_99991231_"))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_existing_money_and_legacy_envelope_nonrecords_are_preserved(self):
        existing = self.existing(start_at="2025-01-01T18:00:00", end_at="2025-01-01T19:00:00",
                                 money_policy={"overtime_enabled": True, "threshold_hours": 0, "multiplier": 10})
        envelope = {"sessions": [existing, "unknown", None], "custom_metadata": {"keep": True}}
        self.write("study_logs/2025/01/20250101.json", envelope)
        migration.commit_import(self.preview(), self.root)
        saved = self.read("study_logs/2025/01/20250101.json")
        self.assertEqual(saved["custom_metadata"], envelope["custom_metadata"])
        self.assertEqual(saved["sessions"][:3], envelope["sessions"])
        self.assertEqual(len(saved["sessions"]), 4)

    def test_legacy_single_record_remains_exact_inside_new_list(self):
        existing = self.existing(start_at="2025-01-01T18:00:00", end_at="2025-01-01T19:00:00")
        self.write("study_logs/2025/01/20250101.json", existing)
        migration.commit_import(self.preview(), self.root)
        self.assertEqual(self.read("study_logs/2025/01/20250101.json")[0], existing)

    def test_warning_is_held_while_clean_ready_row_can_be_imported(self):
        warning = self.row(warnings=True)
        good = self.row(number=3, start="2025-01-02T09:00:00")
        preview = self.preview(self.diagnosis(warning, good))
        self.assertEqual(preview["summary"]["review_count"], 1)
        self.assertEqual(preview["summary"]["ready_count"], 1)
        self.assertEqual(migration.commit_import(preview, self.root)["added_count"], 1)
        self.assertFalse((self.root / "study_logs/2025/01/20250101.json").exists())

    def test_selected_error_blocks_but_excluded_error_does_not(self):
        bad = self.row()
        bad["session"] = None
        bad["issues"] = [{"severity": "error", "code": "invalid", "message": "不正"}]
        good = self.row(number=3, start="2025-01-02T09:00:00")
        diagnosis = self.diagnosis(bad, good)
        with self.assertRaisesRegex(ValueError, "エラー"):
            migration.commit_import(self.preview(diagnosis), self.root)
        preview = self.preview(diagnosis, selected_rows=[3])
        self.assertEqual(preview["summary"]["excluded_count"], 1)
        self.assertEqual(migration.commit_import(preview, self.root)["added_count"], 1)

    def test_invalid_extra_cells_row_can_be_excluded_without_blocking_good_row(self):
        bad = self.row()
        bad["raw"]["__extra_cells"] = ["余分なセル", "もう一つ"]
        bad["session"] = None
        bad["issues"] = [{"severity": "error", "code": "extra_cells", "message": "列数不正"}]
        good = self.row(number=3, start="2025-01-02T09:00:00")
        preview = self.preview(self.diagnosis(bad, good), selected_rows=[3])
        self.assertEqual(preview["summary"]["error_count"], 0)
        self.assertEqual(preview["rows"][0]["status"], "excluded")
        self.assertEqual(preview["rows"][0]["raw"]["__extra_cells"], bad["raw"]["__extra_cells"])
        self.assertEqual(migration.commit_import(preview, self.root)["added_count"], 1)

    def test_global_summary_mismatch_blocks_entire_commit(self):
        diagnosis = self.diagnosis()
        diagnosis["issues"] = [{"severity": "error", "code": "summary_mismatch", "message": "日次集計不一致"}]
        preview = self.preview(diagnosis)
        self.assertEqual(preview["summary"]["error_count"], 1)
        with self.assertRaises(ValueError):
            migration.commit_import(preview, self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_file_or_summary_error_with_row_number_cannot_be_excluded(self):
        diagnosis = self.diagnosis(self.row(), self.row(number=3, start="2025-01-02T09:00:00"))
        diagnosis["issues"] = [{"severity": "error", "code": "invalid_summary_date", "message": "日次集計の日時不正", "row_number": 2}]
        preview = self.preview(diagnosis, selected_rows=[3])
        self.assertEqual(preview["summary"]["error_count"], 1)
        with self.assertRaises(ValueError):
            migration.commit_import(preview, self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_classification_uses_active_ids_and_actual_hierarchy(self):
        master = self.master()
        self.write("master_data/master.json", master)
        mapping = {"medical": {"project_id": "p", "subject_id": "s", "material_id": "m", "unit_id": "u"}}
        preview = self.preview(classification_map=mapping)
        migration.commit_import(preview, self.root)
        saved = self.read("study_logs/2025/01/20250101.json")[0]
        self.assertEqual({key: saved[key] for key in migration.MASTER_KEYS}, mapping["medical"])
        self.assertEqual(self.read("master_data/master.json"), master)

    def test_inactive_unknown_or_mismatched_classification_is_rejected(self):
        master = self.master()
        self.write("master_data/master.json", master)
        for mapping in ({"project_id": "missing"}, {"subject_id": "s"}, {"project_id": "p", "material_id": "m"}):
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                self.preview(classification_map={"medical": mapping})
        master["projects"][0]["is_active"] = False
        self.write("master_data/master.json", master)
        with self.assertRaisesRegex(ValueError, "無効"):
            self.preview(classification_map={"medical": {"project_id": "p"}})

    def test_classification_loader_does_not_manufacture_ids_or_write(self):
        self.assertEqual(migration.load_import_classifications(self.root), {})
        self.assertEqual(list(self.root.iterdir()), [])
        self.write("master_data/master.json", {"projects": [{"name": "IDなし"}]})
        before = (self.root / "master_data/master.json").read_bytes()
        with self.assertRaises(json_store.JsonReadError):
            migration.load_import_classifications(self.root)
        self.assertEqual(before, (self.root / "master_data/master.json").read_bytes())

    def test_changed_daily_file_blocks_without_overwriting(self):
        relative = "study_logs/2025/01/20250101.json"
        existing = self.existing(start_at="2025-01-01T18:00:00", end_at="2025-01-01T19:00:00")
        self.write(relative, [existing])
        preview = self.preview()
        existing["memo"] = "並行編集"
        self.write(relative, [existing])
        with self.assertRaises(json_store.JsonConflictError):
            migration.commit_import(preview, self.root)
        self.assertEqual(self.read(relative), [existing])
        self.assertFalse((self.root / migration.LEDGER_PATH).exists())

    def test_new_inventory_file_blocks_without_adding_preview_rows(self):
        preview = self.preview()
        existing = self.existing(start_at="2025-01-02T18:00:00", end_at="2025-01-02T19:00:00")
        self.write("study_logs/2025/01/20250102.json", [existing])
        with self.assertRaises(json_store.JsonConflictError):
            migration.commit_import(preview, self.root)
        self.assertFalse((self.root / "study_logs/2025/01/20250101.json").exists())
        self.assertEqual(self.read("study_logs/2025/01/20250102.json"), [existing])

    def test_changed_master_blocks_commit(self):
        self.write("master_data/master.json", self.master())
        preview = self.preview(classification_map={"medical": {"project_id": "p"}})
        master = self.master()
        master["projects"][0]["is_active"] = False
        self.write("master_data/master.json", master)
        with self.assertRaises(json_store.JsonConflictError):
            migration.commit_import(preview, self.root)

    def test_tampered_payload_root_or_signature_is_rejected(self):
        preview = self.preview()
        for mutate in (
            lambda value: value["rows"][0]["session"].update(money=100),
            lambda value: value.update(operation_id="0" * 32),
            lambda value: value.update(root=str(self.root.parent)),
            lambda value: value.update(signature="0" * 64),
        ):
            altered = deepcopy(preview)
            mutate(altered)
            with self.assertRaises(json_store.JsonConflictError):
                migration.commit_import(altered, self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_write_failure_rolls_back_all_changed_files_and_ledger(self):
        first, second = self.row(), self.row(number=3, start="2025-01-02T09:00:00")
        preview = self.preview(self.diagnosis(first, second))
        original_save = json_store.save_json_safe
        def failing_save(path, data, **kwargs):
            if Path(path) == self.root / "study_logs/2025/01/20250102.json" and json_store._committing_operation.get() is not None:
                raise OSError("synthetic failure")
            return original_save(path, data, **kwargs)
        with patch.object(json_store, "save_json_safe", side_effect=failing_save):
            with self.assertRaises(json_store.JsonStoreError):
                migration.commit_import(preview, self.root)
        self.assertFalse((self.root / "study_logs/2025/01/20250101.json").exists())
        self.assertFalse((self.root / "study_logs/2025/01/20250102.json").exists())
        self.assertFalse((self.root / migration.LEDGER_PATH).exists())
        self.assertFalse((self.root / "backups/study_session_ids.json").exists())
        journal = self.read(f"backups/json_operations/{preview['operation_id']}/transaction.json")
        self.assertEqual(journal["status"], "rolled_back")

    def test_unsupported_or_malformed_existing_documents_are_not_emptied(self):
        relative = "study_logs/2025/01/20250101.json"
        for bad in ({"sessions": "bad", "keep": True}, {"unrecognized": True}):
            self.write(relative, bad)
            before = (self.root / relative).read_bytes()
            with self.assertRaises(json_store.JsonReadError):
                self.preview()
            self.assertEqual(before, (self.root / relative).read_bytes())
        (self.root / relative).write_text('{"bad":', encoding="utf-8")
        with self.assertRaises(json_store.JsonReadError):
            self.preview()

    def test_invalid_ledger_or_registry_is_not_overwritten(self):
        for relative, value in ((migration.LEDGER_PATH, {}), ("backups/study_session_ids.json", [False])):
            with self.subTest(relative=relative):
                path = self.write(relative, value)
                before = path.read_bytes()
                with self.assertRaises(json_store.JsonReadError):
                    self.preview()
                self.assertEqual(before, path.read_bytes())
                path.unlink()

    def test_deleted_provenance_in_trash_prevents_reimport_without_ledger(self):
        row = self.row()
        record = self.existing()
        record["import_metadata"] = {"format": migration.FORMAT, "fingerprint": row["fingerprint"]}
        self.write("backups/study_log_trash.json", [{"session": record}])
        self.assertEqual(self.preview(self.diagnosis(row))["rows"][0]["status"], "existing")

    def test_duplicate_source_fingerprint_cannot_be_added_twice(self):
        first = self.row()
        second = deepcopy(first)
        second["row_number"] = 3
        preview = self.preview(self.diagnosis(first, second))
        self.assertEqual(preview["summary"]["ready_count"], 1)
        self.assertEqual(preview["summary"]["existing_count"], 1)
        migration.commit_import(preview, self.root)
        self.assertEqual(len(self.read("study_logs/2025/01/20250101.json")), 1)

    def test_id_reservations_in_ledger_registry_and_trash_are_not_reused(self):
        self.write("backups/study_session_ids.json", ["session_20250101_009"])
        self.write("backups/study_log_trash.json", [{"session": {"session_id": "session_20250101_011"}}])
        preview = self.preview()
        self.assertEqual(preview["rows"][0]["session"]["session_id"], "session_20250101_012")


if __name__ == "__main__":
    unittest.main()
