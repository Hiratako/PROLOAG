from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import Mock, patch

from app_core import exercise_service, json_store, study_log_service
from data_utils import generate_id


class RecordUpdateTests(unittest.TestCase):
    target = date(2026, 8, 28)
    initial_time = datetime(2026, 8, 28, 12, 0, 0)

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.clock = self.initial_time
        owner = self

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return owner.clock if tz is None else owner.clock.astimezone(tz)

        for patcher in (
            patch.object(json_store, "PROJECT_ROOT", self.root),
            patch.object(study_log_service, "datetime", FrozenDatetime),
            patch.object(exercise_service, "EXERCISE_LOGS_DIR", self.root / "exercise_logs"),
            patch.object(exercise_service, "_now_text", side_effect=self.timestamp),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def timestamp(self) -> str:
        return self.clock.strftime("%Y-%m-%d %H:%M:%S")

    def advance(self) -> str:
        self.clock += timedelta(minutes=5)
        return self.timestamp()

    def session(self, **extra: Any) -> dict[str, Any]:
        return {
            "start_at": "2026-08-28T09:00:00",
            "end_at": "2026-08-28T10:00:00",
            "duration_sec": 3600,
            "hourly_rate": 1000,
            "money": 1000,
            "normal_money": 1000,
            "overtime_money": 0,
            "linked_note_ids": [],
            **extra,
        }

    def rows(self) -> list[dict[str, Any]]:
        return study_log_service.load_study_sessions_by_date(self.target, self.root)

    def file_snapshot(self) -> dict[str, bytes]:
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    @staticmethod
    def run_parallel(operations: list[Any]) -> list[Any]:
        barrier = threading.Barrier(len(operations), timeout=10)

        def run(operation):
            barrier.wait()
            return operation()

        with ThreadPoolExecutor(max_workers=len(operations)) as executor:
            futures = [executor.submit(run, operation) for operation in operations]
            return [future.result(timeout=20) for future in futures]

    def test_append_assigns_unique_ids_and_creation_update_times(self) -> None:
        first = self.session(memo="first")
        first_time = self.timestamp()
        path = study_log_service.append_study_session(first, self.root)
        second_time = self.advance()
        second = self.session(memo="second")
        self.assertEqual(study_log_service.append_study_session(second, self.root), path)
        rows = self.rows()
        self.assertEqual([row["session_id"] for row in rows], ["session_20260828_001", "session_20260828_002"])
        self.assertEqual(first["session_id"], rows[0]["session_id"])
        self.assertEqual(second["session_id"], rows[1]["session_id"])
        self.assertEqual((rows[0]["created_at"], rows[0]["updated_at"]), (first_time, first_time))
        self.assertEqual((rows[1]["created_at"], rows[1]["updated_at"]), (second_time, second_time))

    def test_append_preserves_supplied_unique_id_and_created_time(self) -> None:
        created = "2026-08-27 18:00:00"
        record = self.session(session_id="external_session_id", created_at=created, updated_at=created)
        study_log_service.append_study_session(record, self.root)
        saved = self.rows()[0]
        self.assertEqual(saved["session_id"], "external_session_id")
        self.assertEqual(saved["created_at"], created)
        self.assertEqual(saved["updated_at"], self.timestamp())

    def test_append_without_start_time_uses_current_time(self) -> None:
        record = {"duration_sec": 60, "memo": "no start supplied"}
        path = study_log_service.append_study_session(record, self.root)
        self.assertEqual(path, study_log_service.get_study_log_path(self.target, self.root))
        saved = self.rows()[0]
        self.assertEqual(saved["start_at"], self.clock.isoformat())
        self.assertEqual(saved["created_at"], self.timestamp())
        self.assertEqual(saved["updated_at"], self.timestamp())

    def test_duplicate_append_does_not_change_files(self) -> None:
        record = self.session(session_id="session_20260828_001")
        study_log_service.append_study_session(record, self.root)
        before = self.file_snapshot()
        self.advance()
        with self.assertRaisesRegex(ValueError, "同じID"):
            study_log_service.append_study_session(self.session(session_id=record["session_id"], memo="duplicate"), self.root)
        self.assertEqual(self.file_snapshot(), before)
        self.assertEqual(len(self.rows()), 1)

    def test_timer_retry_after_link_failure_does_not_duplicate_session(self) -> None:
        # Execute the actual save block without loading Streamlit or writing JSON.
        page = next((Path(__file__).resolve().parents[1] / "pages").glob("01_*.py"))
        tree = ast.parse(page.read_text(encoding="utf-8-sig"), filename=str(page))
        functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        blocks = [node for node in ast.walk(functions["render_pending_save"])
                  if isinstance(node, ast.With) and isinstance(node.items[0].context_expr, ast.Call)
                  and isinstance(node.items[0].context_expr.func, ast.Name)
                  and node.items[0].context_expr.func.id == "atomic_json_operation"]
        self.assertEqual(len(blocks), 1)
        code = compile(ast.Module(body=[functions["ensure_session_id"], blocks[0]], type_ignores=[]), str(page), "exec")
        saved: list[dict[str, Any]] = []
        pending = self.session()

        def save_in_memory(_target, rows, _root):
            saved[:] = deepcopy(rows)

        scope = {
            "datetime": datetime, "Any": Any, "generate_id": generate_id,
            "reserved_study_session_ids": lambda _root: {row["session_id"] for row in saved},
            "ROOT": self.root, "json_transaction": lambda *_a, **_k: nullcontext(),
            "atomic_json_operation": lambda **_k: nullcontext(SimpleNamespace(already_committed=False)),
            "operation_id": "a" * 32,
            "daily_path": lambda target: study_log_service.get_study_log_path(target, self.root),
            "load_sessions": lambda _path: deepcopy(saved),
            "session_start": datetime.fromisoformat(pending["start_at"]),
            "note_options": {"existing": "note_existing"}, "selected_note_labels": ["existing"],
            "load_all_notes": lambda _root: [{"note_id": "note_existing"}],
            "create_note": False, "st": SimpleNamespace(session_state=SimpleNamespace(pending_session=pending, get=lambda *_a: None)),
            "link_notes_to_session": Mock(side_effect=[json_store.JsonLockTimeoutError("test link failure"), None]),
            "link_session_to_plan_task": Mock(),
            "append_session": lambda session: study_log_service.append_study_session(session, self.root),
        }
        with patch("app_core.json_operations.operation_guard", lambda *_a, **_k: nullcontext()), \
             patch.object(study_log_service, "json_transaction", lambda *_a, **_k: nullcontext()), \
             patch.object(study_log_service, "load_study_sessions_by_date", lambda *_a, **_k: deepcopy(saved)), \
             patch.object(study_log_service, "save_study_sessions_by_date", save_in_memory):
            scope["session"] = dict(pending)
            with self.assertRaises(json_store.JsonLockTimeoutError):
                exec(code, scope)
            self.assertEqual(saved, [])
            scope["link_session_to_plan_task"].assert_not_called()
            scope["session"] = dict(pending)
            exec(code, scope)

        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["session_id"], "session_20260828_001")
        self.assertEqual(saved[0]["linked_note_ids"], ["note_existing"])
        self.assertEqual(pending["session_id"], saved[0]["session_id"])
        self.assertEqual(self.file_snapshot(), {})

    def test_note_link_updates_timestamp_once_and_repeated_link_is_noop(self) -> None:
        record = self.session(linked_task_id="task_original")
        study_log_service.append_study_session(record, self.root)
        created = self.timestamp()
        updated = self.advance()
        self.assertTrue(study_log_service.attach_note_to_session(record["session_id"], "note_one", self.root))
        saved = self.rows()[0]
        self.assertEqual(saved["linked_note_ids"], ["note_one"])
        self.assertEqual(saved["linked_task_id"], "task_original")
        self.assertEqual(saved["created_at"], created)
        self.assertEqual(saved["updated_at"], updated)
        before = self.file_snapshot()
        self.advance()
        self.assertTrue(study_log_service.attach_note_to_session(record["session_id"], "note_one", self.root))
        self.assertEqual(self.file_snapshot(), before)

    def test_task_link_updates_timestamp_and_repeated_link_is_noop(self) -> None:
        record = self.session(linked_note_ids=["note_original"])
        study_log_service.append_study_session(record, self.root)
        created = self.timestamp()
        updated = self.advance()
        self.assertTrue(study_log_service.attach_task_to_session(record["session_id"], "task_one", self.root))
        saved = self.rows()[0]
        self.assertEqual(saved["linked_task_id"], "task_one")
        self.assertEqual(saved["linked_note_ids"], ["note_original"])
        self.assertEqual(saved["created_at"], created)
        self.assertEqual(saved["updated_at"], updated)
        before = self.file_snapshot()
        self.advance()
        self.assertTrue(study_log_service.attach_task_to_session(record["session_id"], "task_one", self.root))
        self.assertEqual(self.file_snapshot(), before)
        next_updated = self.advance()
        self.assertTrue(study_log_service.attach_task_to_session(record["session_id"], "task_two", self.root))
        saved = self.rows()[0]
        self.assertEqual(saved["linked_task_id"], "task_two")
        self.assertEqual(saved["created_at"], created)
        self.assertEqual(saved["updated_at"], next_updated)

    def test_linking_an_unknown_session_is_read_only(self) -> None:
        before = self.file_snapshot()
        self.assertFalse(study_log_service.attach_note_to_session("unknown_session", "note_one", self.root))
        self.assertFalse(study_log_service.attach_task_to_session("unknown_session", "task_one", self.root))
        self.assertEqual(self.file_snapshot(), before)

    def test_parallel_session_appends_keep_every_record_and_unique_id(self) -> None:
        records = [self.session(memo=f"parallel {index}") for index in range(6)]
        self.run_parallel([lambda record=record: study_log_service.append_study_session(record, self.root) for record in records])
        saved = self.rows()
        self.assertEqual(len(saved), len(records))
        self.assertEqual({row["memo"] for row in saved}, {record["memo"] for record in records})
        self.assertEqual({row["session_id"] for row in saved}, {f"session_20260828_{index:03d}" for index in range(1, 7)})
        self.assertTrue(all(row["created_at"] == self.timestamp() == row["updated_at"] for row in saved))

    def test_parallel_note_and_task_links_keep_all_changes(self) -> None:
        record = self.session(linked_note_ids=["note_original"])
        study_log_service.append_study_session(record, self.root)
        created = self.timestamp()
        updated = self.advance()
        note_ids = [f"note_{index}" for index in range(5)]
        operations = [lambda note_id=note_id: study_log_service.attach_note_to_session(record["session_id"], note_id, self.root) for note_id in note_ids]
        operations.append(lambda: study_log_service.attach_task_to_session(record["session_id"], "task_parallel", self.root))
        self.assertEqual(self.run_parallel(operations), [True] * len(operations))
        saved = self.rows()[0]
        self.assertEqual(set(saved["linked_note_ids"]), {"note_original", *note_ids})
        self.assertEqual(len(saved["linked_note_ids"]), len(note_ids) + 1)
        self.assertEqual(saved["linked_task_id"], "task_parallel")
        self.assertEqual(saved["created_at"], created)
        self.assertEqual(saved["updated_at"], updated)

    def test_parallel_repeated_note_link_is_not_duplicated(self) -> None:
        record = self.session()
        study_log_service.append_study_session(record, self.root)
        self.advance()
        operations = [lambda: study_log_service.attach_note_to_session(record["session_id"], "same_note", self.root) for _ in range(4)]
        self.assertEqual(self.run_parallel(operations), [True] * 4)
        self.assertEqual(self.rows()[0]["linked_note_ids"], ["same_note"])

    def test_diary_exercise_upsert_preserves_creation_and_other_records(self) -> None:
        unrelated = exercise_service.add_exercise_record(self.target, {"type": "walking", "title": "unrelated", "duration_min": 15})
        created = self.timestamp()
        first = exercise_service.upsert_diary_running_record(self.target, 3, 20, memo="first")
        updated = self.advance()
        second = exercise_service.upsert_diary_running_record(self.target, 4, 25, memo="changed")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first["exercise_id"], second["exercise_id"])
        self.assertEqual(second["created_at"], created)
        self.assertEqual(second["updated_at"], updated)
        self.assertEqual(second["distance_km"], 4)
        self.assertEqual(second["duration_min"], 25)
        saved = exercise_service.load_exercise_log(self.target)["records"]
        self.assertEqual(len(saved), 2)
        self.assertEqual(next(row for row in saved if row["exercise_id"] == unrelated["exercise_id"]), unrelated)
        self.assertEqual(next(row for row in saved if row["exercise_id"] == second["exercise_id"]), second)

    def test_daily_hub_upsert_preserves_creation_and_updates_timestamp(self) -> None:
        created = self.timestamp()
        first = exercise_service.upsert_daily_hub_exercise_record(self.target, {"type": "running", "duration_min": 20, "distance_km": 3})
        updated = self.advance()
        second = exercise_service.upsert_daily_hub_exercise_record(self.target, {
            "type": "running", "duration_min": 30, "distance_km": 5,
            "created_at": "2000-01-01 00:00:00", "updated_at": "2000-01-01 00:00:00",
        })
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first["exercise_id"], second["exercise_id"])
        self.assertEqual(second["created_at"], created)
        self.assertEqual(second["updated_at"], updated)
        self.assertEqual(second["duration_min"], 30)
        self.assertEqual(second["distance_km"], 5)
        self.assertEqual(exercise_service.load_exercise_log(self.target)["records"], [second])

    def test_legacy_daily_hub_record_preserves_original_creation_time(self) -> None:
        created = "2026-08-27 20:00:00"
        exercise_service.add_exercise_record(self.target, {
            "exercise_id": "legacy_hub_id", "source": "daily_hub", "type": "running",
            "duration_min": 20, "distance_km": 3, "created_at": created,
        })
        updated = self.advance()
        saved = exercise_service.upsert_daily_hub_exercise_record(self.target, {"type": "running", "duration_min": 30, "distance_km": 4})
        self.assertIsNotNone(saved)
        self.assertEqual(saved["created_at"], created)
        self.assertEqual(saved["updated_at"], updated)
        self.assertEqual(saved["exercise_id"], "exercise_20260828_daily_hub")
        self.assertEqual(exercise_service.load_exercise_log(self.target)["records"], [saved])


if __name__ == "__main__":
    unittest.main()
