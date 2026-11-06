from __future__ import annotations

import ast
from copy import deepcopy
from datetime import date, datetime
import json
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

import data_utils
from app_core import master_snapshot as snapshot_module
from app_core.json_store import JsonConflictError
from app_core.master_snapshot import MasterSnapshot


ROOT = Path(__file__).resolve().parents[1]


def master_fixture() -> dict[str, Any]:
    return {
        "projects": [
            {"project_id": "p", "name": "プロジェクト", "is_active": True},
            {"project_id": "inactive", "name": "非表示", "is_active": False},
            {"project_id": "duplicate", "name": "先頭", "is_active": False},
            {"project_id": "duplicate", "name": "後続", "is_active": True},
        ],
        "subjects": [{"subject_id": "s", "project_id": "p", "name": "科目"}],
        "materials": [
            {"material_id": "m", "project_id": "p", "subject_id": "s", "name": ""},
            {"material_id": "other", "project_id": "p", "subject_id": "other", "name": "別教材"},
        ],
        "units": [{"unit_id": "u", "project_id": "p", "subject_id": "s", "material_id": "m", "name": "単元"}],
        "tags": [{"tag_id": "t", "name": "タグ"}],
    }


class MasterSnapshotTests(unittest.TestCase):
    def test_all_name_and_candidate_queries_share_one_read_and_match_legacy(self) -> None:
        master = master_fixture()
        before = deepcopy(master)
        with patch.object(snapshot_module, "load_master_data", return_value=master) as reader:
            snapshot = MasterSnapshot.load()
            with patch.object(data_utils, "load_master_data", return_value=master):
                for record in (
                    {"project_id": "p", "subject_id": "s", "material_id": "m", "unit_id": "u"},
                    {"project_id": "inactive", "subject_id": "missing"},
                    {"project_id": "duplicate", "material_id": "", "unit_id": None},
                    {},
                ):
                    self.assertEqual(snapshot.names(record), data_utils.resolve_master_names(record))
                for collection, filters in (
                    ("projects", {}), ("subjects", {"project_id": "p"}),
                    ("materials", {"project_id": "p", "subject_id": "s"}),
                    ("units", {"project_id": "p", "material_id": "m"}),
                    ("materials", {"subject_id": ""}), ("tags", {}),
                ):
                    self.assertEqual(snapshot.active(collection, **filters), data_utils._active_items(None, collection, **filters))
                for _ in range(100):
                    self.assertEqual(snapshot.name("project_id", "p"), "プロジェクト")
            reader.assert_called_once()
        self.assertEqual(master, before)

    def test_new_snapshot_reads_changes_without_mutating_old_snapshot(self) -> None:
        first = master_fixture()
        second = deepcopy(first)
        second["projects"][0]["name"] = "新しい名前"
        with patch.object(snapshot_module, "load_master_data", side_effect=[first, second]) as reader:
            old = MasterSnapshot.load()
            new = MasterSnapshot.load()
        self.assertEqual(old.name("project_id", "p"), "プロジェクト")
        self.assertEqual(new.name("project_id", "p"), "新しい名前")
        self.assertEqual(reader.call_count, 2)

    def test_snapshot_read_is_readonly_and_preserves_save_conflict_detection(self) -> None:
        with tempfile.TemporaryDirectory(prefix="proloag-master-snapshot-") as temporary:
            root = Path(temporary)
            path = root / "master_data" / "master.json"
            path.parent.mkdir()
            path.write_text(json.dumps(master_fixture(), ensure_ascii=False), encoding="utf-8")
            before = path.read_bytes()
            snapshot = MasterSnapshot.load(root)
            self.assertEqual(path.read_bytes(), before)
            changed = master_fixture()
            changed["projects"][0]["name"] = "別の画面による更新"
            path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
            external = path.read_bytes()
            with self.assertRaises(JsonConflictError):
                data_utils.save_master_data(snapshot.data, root)
            self.assertEqual(path.read_bytes(), external)


def city_functions() -> dict[str, Any]:
    path = ROOT / "pages" / "15_知識都市ビュー.py"
    source = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    wanted = {"parse_day", "safe_int", "apply_record_filter", "row_key", "blank_row", "build_city_rows"}
    functions = ast.Module(body=[node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in wanted], type_ignores=[])
    namespace = {"Any": Any, "date": date, "datetime": datetime, "MasterSnapshot": MasterSnapshot, "PROJECT_ROOT": ROOT}
    exec(compile(functions, str(path), "exec"), namespace)
    return namespace


class KnowledgeCityReadEfficiencyTests(unittest.TestCase):
    def test_repeated_records_are_aggregated_and_names_resolved_once_per_building(self) -> None:
        functions = city_functions()
        classification = {"project_id": "p", "subject_id": "s", "material_id": "m", "unit_id": "u"}
        sessions = [{**classification, "start_at": "2026-09-01T10:00:00", "duration_sec": 60, "money": 50} for _ in range(100)]
        sessions.append({**classification, "start_at": "2026-08-01", "duration_sec": 999})
        sessions.append({**classification, "project_id": "excluded", "start_at": "2026-09-01", "duration_sec": 999})
        notes = [{**classification, "created_at": "2026-09-01", "title": f"ノート{i}"} for i in range(5)]
        cards = [{**classification, "created_at": "2026-09-02", "need_review": True}]
        reminders = [{**classification, "scheduled_date": "2026-09-02", "status": "done"}, {**classification, "status": "pending"}]
        before = deepcopy((sessions, notes, cards, reminders))
        snapshot = MasterSnapshot(master_fixture())
        with patch.object(snapshot, "names", wraps=snapshot.names) as names, patch.object(MasterSnapshot, "load", side_effect=AssertionError("unexpected read")):
            result = functions["build_city_rows"](sessions, notes, cards, reminders, {"project_id": "p"}, date(2026, 9, 1), date(2026, 9, 2), master_snapshot=snapshot)
        self.assertEqual(len(result), 1)
        names.assert_called_once()
        self.assertEqual(result[0], {
            **classification, "project_name": "プロジェクト", "subject_name": "科目", "material_name": "", "unit_name": "単元",
            "duration_sec": 6000, "money": 5000, "session_count": 100, "note_count": 5, "card_count": 1,
            "review_card_count": 1, "reminder_count": 2, "pending_reminder_count": 1,
            "last_study_date": date(2026, 9, 1), "sample_notes": ["ノート0", "ノート1", "ノート2"],
            "hours": 1.67, "knowledge_assets": 8, "knowledge_density": round(18 / 1.67, 2),
        })
        self.assertEqual((sessions, notes, cards, reminders), before)

    def test_legacy_call_loads_once_and_empty_records_do_not_load(self) -> None:
        build = city_functions()["build_city_rows"]
        snapshot = MasterSnapshot(master_fixture())
        args = ([], [], [], [], {}, date(2026, 9, 1), date(2026, 9, 2))
        with patch.object(MasterSnapshot, "load", return_value=snapshot) as reader:
            self.assertEqual(build(*args), [])
            reader.assert_not_called()
            result = build([{"project_id": "p", "start_at": "2026-09-01"}] * 50, *args[1:])
            reader.assert_called_once()
        self.assertEqual(result[0]["session_count"], 50)
        self.assertEqual(result[0]["subject_name"], "未設定")


if __name__ == "__main__":
    unittest.main()
