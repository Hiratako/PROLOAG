from __future__ import annotations

import ast
import copy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch

from app_core import exercise_service, ids, note_service
import data_utils


class RecordIdTests(unittest.TestCase):
    target = date(2026, 8, 28)
    token = "20260828"

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        original_date_token = ids.date_token
        token_patch = patch.object(
            ids,
            "date_token",
            side_effect=lambda value=None: self.token if value is None else original_date_token(value),
        )
        token_patch.start()
        self.addCleanup(token_patch.stop)
        exercise_patch = patch.object(exercise_service, "EXERCISE_LOGS_DIR", self.root / "exercise_logs")
        exercise_patch.start()
        self.addCleanup(exercise_patch.stop)

    @staticmethod
    def write_fixture(path: Path, value: Any) -> bytes:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        path.write_bytes(payload)
        return payload

    def note_page(self, title: str, **extra: Any) -> dict[str, Any]:
        return {"title": title, "created_at": "2026-08-28T09:05:35", **extra}

    @staticmethod
    def hierarchy_id_lists(store: dict[str, Any]) -> dict[str, list[str]]:
        notebooks = store["notebooks"]
        sections = [section for notebook in notebooks for section in notebook["sections"]]
        pages = [page for section in sections for page in section["pages"]]
        return {
            "notebook": [notebook["id"] for notebook in notebooks],
            "section": [section["id"] for section in sections],
            "note": [page["note_id"] for page in pages],
        }

    def test_generate_id_reserves_into_caller_empty_set(self) -> None:
        reserved: set[str] = set()
        generated = [ids.generate_id("task", self.target, reserved) for _ in range(3)]
        self.assertEqual(generated, [f"task_{self.token}_{number:03d}" for number in (1, 2, 3)])
        self.assertEqual(reserved, set(generated))
        self.assertEqual(
            ids.generate_daily_sequence_id("task", reserved, date=self.target),
            f"task_{self.token}_004",
        )

    def test_sequence_keeps_existing_ids_and_advances_beyond_three_digits(self) -> None:
        reserved = {
            f"note_{self.token}_999",
            f"note_{self.token}_1000",
            f"note_{self.token}_1002",
            "note_20260827_9000",
            "unrelated_20260828_9000",
            "legacy-note-id",
        }
        original = set(reserved)
        self.assertEqual(ids.generate_id("note", self.target, reserved), f"note_{self.token}_1003")
        self.assertTrue(original.issubset(reserved))

    def test_datetime_boundaries_preserve_full_seconds_and_offset(self) -> None:
        expected = datetime(2026, 8, 28, 9, 5, 35)
        for value in ("2026-08-28T09:05:35", "2026-08-28 09:05:35", " 2026-08-28T09:05:35 "):
            with self.subTest(value=value):
                self.assertEqual(ids.parse_datetime(value), expected)
        self.assertEqual(ids.parse_datetime("2024-02-29"), datetime(2024, 2, 29))
        self.assertEqual(
            ids.parse_datetime("2026-08-28T23:59:59+09:00"),
            datetime(2026, 8, 28, 23, 59, 59, tzinfo=timezone(timedelta(hours=9))),
        )
        self.assertIsNone(ids.parse_datetime("2026-02-29"))
        self.assertIsNone(ids.parse_datetime("2026-08-28T09:05:35 invalid"))
        self.assertEqual(ids.date_token("2026-08-28T23:59:59+09:00"), self.token)

    def test_multiple_carried_tasks_from_an_empty_day_remain_unique_after_save(self) -> None:
        plan = data_utils.default_daily_plan(self.target)
        existing_ids: set[str] = set()
        for index in range(3):
            plan["tasks"].append(
                {
                    "task_id": data_utils.generate_id("task", plan["date"], existing_ids),
                    "title": f"carryover {index}",
                    "carried_from_task_id": f"task_20260827_{index + 1:03d}",
                }
            )
        expected_ids = [task["task_id"] for task in plan["tasks"]]
        data_utils.save_daily_plan(self.root, plan)
        saved = data_utils.load_daily_plan(self.root, self.target)
        self.assertEqual(len(set(expected_ids)), 3)
        self.assertEqual([task["task_id"] for task in saved["tasks"]], expected_ids)
        self.assertEqual(
            [task["carried_from_task_id"] for task in saved["tasks"]],
            [f"task_20260827_{index:03d}" for index in (1, 2, 3)],
        )

    def test_legacy_tasks_and_master_items_get_unique_missing_ids(self) -> None:
        plan, _ = data_utils.normalize_daily_plan(
            {"date": self.target.isoformat(), "tasks": [{"title": "first"}, {"title": "second"}]},
            self.target,
        )
        self.assertEqual([task["task_id"] for task in plan["tasks"]], [f"task_{self.token}_001", f"task_{self.token}_002"])
        master, _ = data_utils.normalize_master_data({"projects": [{"name": "first"}, {"name": "second"}]})
        self.assertEqual([item["project_id"] for item in master["projects"]], [f"project_{self.token}_001", f"project_{self.token}_002"])

    def test_legacy_study_sessions_are_unique_and_read_only(self) -> None:
        path = self.root / "study_logs" / "2026" / "08" / f"{self.token}.json"
        for include_existing in (False, True):
            with self.subTest(include_existing=include_existing):
                rows = [{"start_at": "2026-08-28T09:00:00"}, {"start_at": "2026-08-28T10:00:00"}]
                if include_existing:
                    rows.append({"session_id": f"session_{self.token}_001", "start_at": "2026-08-28T11:00:00"})
                before = self.write_fixture(path, rows)
                loaded = data_utils.load_study_sessions_file(path)
                generated = [row["session_id"] for row in loaded]
                self.assertEqual(len(set(generated)), len(rows))
                if include_existing:
                    self.assertEqual(generated, [f"session_{self.token}_002", f"session_{self.token}_003", f"session_{self.token}_001"])
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual([row["session_id"] for row in data_utils.load_study_sessions_file(path)], generated)
                self.assertEqual(path.read_bytes(), before)

    def test_legacy_exercise_records_reserve_existing_ids_before_new_ones(self) -> None:
        path = exercise_service.get_exercise_log_path(self.target)
        for include_existing in (False, True):
            with self.subTest(include_existing=include_existing):
                records = [{"title": "first"}, {"title": "second"}]
                if include_existing:
                    records.append({"exercise_id": f"exercise_{self.token}_001", "title": "existing"})
                before = self.write_fixture(path, {"date": self.target.isoformat(), "records": records})
                loaded = exercise_service.load_exercise_log(self.target)
                generated = [record["exercise_id"] for record in loaded["records"]]
                self.assertEqual(len(set(generated)), len(records))
                if include_existing:
                    self.assertEqual(generated, [f"exercise_{self.token}_002", f"exercise_{self.token}_003", f"exercise_{self.token}_001"])
                self.assertEqual(path.read_bytes(), before)

        reserved: set[str] = set()
        first = exercise_service.normalize_exercise_record({}, self.target, reserved)
        second = exercise_service.normalize_exercise_record({}, self.target, reserved)
        self.assertEqual(reserved, {first["exercise_id"], second["exercise_id"]})
        self.assertNotEqual(first["exercise_id"], second["exercise_id"])

    def test_note_hierarchy_missing_ids_do_not_collide_with_later_existing_ids(self) -> None:
        known_notebook_id = f"notebook_{self.token}_003"
        known_section_id = f"section_{self.token}_005"
        known_note_id = f"note_{self.token}_004"
        store = {
            "notebooks": [
                {"name": "new", "sections": [{"pages": [self.note_page("first"), self.note_page("second")]}, {"pages": []}]},
                {
                    "id": known_notebook_id,
                    "name": "existing",
                    "sections": [{"id": known_section_id, "pages": [self.note_page("legacy alias", id=known_note_id)]}],
                },
                {"name": "default section"},
            ]
        }
        normalized, _ = data_utils.normalize_note_store(store)
        all_ids = self.hierarchy_id_lists(normalized)
        for kind, values in all_ids.items():
            with self.subTest(kind=kind):
                self.assertEqual(len(values), len(set(values)))
        self.assertEqual(normalized["notebooks"][1]["id"], known_notebook_id)
        known_section = normalized["notebooks"][1]["sections"][0]
        self.assertEqual(known_section["id"], known_section_id)
        self.assertEqual(known_section["pages"][0]["note_id"], known_note_id)
        self.assertEqual(known_section["pages"][0]["id"], known_note_id)

    def test_read_time_note_normalization_never_writes_legacy_file(self) -> None:
        path = data_utils.notes_path(self.root)
        before = self.write_fixture(
            path,
            {"notebooks": [{"sections": [{"pages": [self.note_page("first"), self.note_page("second")]}]}, {"sections": []}]},
        )
        first = data_utils.load_note_store(self.root)
        second = data_utils.load_note_store(self.root)
        self.assertEqual(self.hierarchy_id_lists(first), self.hierarchy_id_lists(second))
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((path.parent / "backups").exists())

    def test_duplicate_legacy_note_recovery_does_not_migrate_source_file(self) -> None:
        path = data_utils.notes_path(self.root)
        duplicate_id = f"note_{self.token}_001"
        before = self.write_fixture(
            path,
            {"notebooks": [{"sections": [{"pages": [self.note_page("first", note_id=duplicate_id), self.note_page("second", note_id=duplicate_id)]}]}]},
        )
        loaded = data_utils.load_note_store(self.root)
        self.assertEqual(self.hierarchy_id_lists(loaded)["note"], [duplicate_id, f"note_{self.token}_002"])
        self.assertEqual(path.read_bytes(), before)

    def test_add_note_returns_the_id_that_is_saved(self) -> None:
        original = data_utils.default_note_store()
        original_ids = self.hierarchy_id_lists(original)
        data_utils.save_note_store(self.root, original)
        returned = [note_service.add_note(f"new {index}", "body", root=self.root) for index in range(3)]
        returned.append(note_service.add_note("metadata", root=self.root, note_id="caller-id", id="caller-al"))
        saved = data_utils.load_note_store(self.root)
        pages = [item["page"] for item in data_utils.iter_note_pages(saved)]
        self.assertEqual(len(set(returned)), 4)
        self.assertTrue(set(original_ids["note"]).issubset({page["note_id"] for page in pages}))
        for note_id in returned:
            matching = [page for page in pages if page["note_id"] == note_id]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0]["id"], note_id)
        self.assertNotIn("caller-id", {page["note_id"] for page in pages})

    def test_note_page_additions_issue_unique_ids_before_normalization(self) -> None:
        # Execute only the page's pure creation helpers; no Streamlit page or real data is loaded.
        page_path = Path(__file__).resolve().parents[1] / "pages" / "04_ノート・知識ベース.py"
        source = ast.parse(page_path.read_text(encoding="utf-8-sig"))
        helper_names = {"new_page", "add_notebook", "add_section", "add_page"}
        helpers = ast.Module(body=[node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in helper_names], type_ignores=[])
        state = SimpleNamespace()
        namespace = {
            "Any": Any,
            "generate_id": data_utils.generate_id,
            "collect_note_store_ids": data_utils.collect_note_store_ids,
            "now_text": data_utils.now_text,
            "st": SimpleNamespace(session_state=state),
        }
        exec(compile(helpers, str(page_path), "exec"), namespace)
        store = data_utils.default_note_store()
        namespace["add_notebook"](store, "notebook two")
        namespace["add_notebook"](store, "notebook three")
        for notebook in store["notebooks"]:
            namespace["add_section"](store, notebook, "extra section")
            section = notebook["sections"][0]
            namespace["add_page"](store, section, "extra page one")
            namespace["add_page"](store, section, "extra page two")
            self.assertEqual(state.note_page_id, section["pages"][-1]["note_id"])
        before_ids = self.hierarchy_id_lists(store)
        for kind, values in before_ids.items():
            with self.subTest(kind=kind):
                self.assertEqual(len(values), len(set(values)))
        normalized, _ = data_utils.normalize_note_store(copy.deepcopy(store))
        self.assertEqual(self.hierarchy_id_lists(normalized), before_ids)


if __name__ == "__main__":
    unittest.main()
