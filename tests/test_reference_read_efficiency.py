from __future__ import annotations

import ast
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import Mock, patch

import pandas as pd

import data_utils
from app_core import reference_snapshot
from app_core.json_store import JsonReadError
from app_core.reference_snapshot import ReferenceSnapshot


PROJECT = Path(__file__).resolve().parents[1]
UNUSED_ROOT = Path("unused-reference-test-root")


def page_functions(prefix: str, names: set[str], **extra: Any) -> dict[str, Any]:
    path = next((PROJECT / "pages").glob(f"{prefix}_*.py"))
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    nodes = ast.parse("from __future__ import annotations").body
    nodes.extend(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names)
    namespace = {
        "ROOT": UNUSED_ROOT, "pd": pd, "Any": Any,
        "ReferenceSnapshot": ReferenceSnapshot,
        **extra,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


def session(identifier: str, memo: str, start: str = "2026-09-16T09:00:00", task: str = "t") -> dict[str, Any]:
    return {"session_id": identifier, "memo": memo, "start_at": start,
            "end_at": start, "duration_sec": 60, "money": 100,
            "hourly_rate": 6000, "linked_task_id": task, "linked_note_ids": ["n"]}


class ReferenceSnapshotTests(unittest.TestCase):
    def test_note_names_match_existing_resolver_with_duplicates_missing_ids_and_order(self):
        notes = [{"note_id": "b", "title": "B"}, {"note_id": "a", "title": "A1"},
                 {"note_id": "a", "title": "A2"}, {"note_id": 1, "title": "numeric"}]
        before = deepcopy(notes)
        values = [["a", "b", "a", "missing", "other"], ["a"], [], None,
                  "a", [1, "1", "b"], ["", None, False]]
        with patch.object(data_utils, "load_all_notes", return_value=notes):
            expected = [data_utils.resolve_note_titles(UNUSED_ROOT, value) for value in values]
        with patch.object(reference_snapshot, "load_all_notes", return_value=notes) as read:
            snapshot = ReferenceSnapshot(UNUSED_ROOT)
            self.assertEqual([snapshot.note_titles(value) for value in values], expected)
            self.assertEqual(expected[0], "B / A1 / A2 / 不明なノート / 不明なノート")
            read.assert_called_once_with(UNUSED_ROOT)
        self.assertEqual(notes, before)

    def test_task_names_keep_last_duplicate_id_and_missing_titles(self):
        plans = [{"date": "2026-09-15", "tasks": [{"task_id": "t", "title": "old"}]},
                 {"date": "2026-09-16", "tasks": [{"task_id": "t", "title": "new"}, {"task_id": "blank"}]}]
        values = ["t", "missing", "blank", "", None, "t"]
        with patch.object(data_utils, "load_all_daily_plans", return_value=plans):
            expected = [data_utils.resolve_task_title(UNUSED_ROOT, value) for value in values]
            with patch.object(reference_snapshot, "load_task_lookup", wraps=data_utils.load_task_lookup) as read:
                snapshot = ReferenceSnapshot(UNUSED_ROOT)
                self.assertEqual([snapshot.task_title(value) for value in values], expected)
                read.assert_called_once_with(UNUSED_ROOT)
        self.assertEqual(expected, ["new", "不明なタスク", "不明なタスク", "", "", "new"])

    def test_empty_references_do_not_load_any_store(self):
        with patch.object(reference_snapshot, "load_all_notes", side_effect=AssertionError("read")), \
             patch.object(reference_snapshot, "load_task_lookup", side_effect=AssertionError("read")), \
             patch.object(reference_snapshot, "load_all_study_sessions", side_effect=AssertionError("read")):
            snapshot = ReferenceSnapshot(UNUSED_ROOT)
            self.assertEqual(snapshot.note_titles([]), "")
            self.assertEqual(snapshot.task_title(""), "")

    def test_sessions_keep_last_row_lookup_and_all_matching_rows(self):
        records = [session("s", "first"), session("other", "other"), session("s", "last")]
        before = deepcopy(records)
        with patch.object(reference_snapshot, "load_all_study_sessions", return_value=records) as read:
            snapshot = ReferenceSnapshot(UNUSED_ROOT)
            self.assertEqual(snapshot.sessions_by_id["s"]["memo"], "last")
            self.assertEqual([row["memo"] for row in snapshot.sessions_for_id("s")], ["first", "last"])
            self.assertEqual(snapshot.sessions_for_id("missing"), [])
            read.assert_called_once_with(UNUSED_ROOT)
        self.assertEqual(records, before)

    def test_next_page_snapshot_observes_changes_without_cross_rerun_cache(self):
        notes = [{"note_id": "n", "title": "before"}]
        tasks = {"t": {"title": "before"}}
        records = [session("s", "before")]
        with patch.object(reference_snapshot, "load_all_notes", side_effect=lambda _: deepcopy(notes)) as note_read, \
             patch.object(reference_snapshot, "load_task_lookup", side_effect=lambda _: deepcopy(tasks)) as task_read, \
             patch.object(reference_snapshot, "load_all_study_sessions", side_effect=lambda _: deepcopy(records)) as session_read:
            first = ReferenceSnapshot(UNUSED_ROOT)
            self.assertEqual((first.note_titles(["n"]), first.task_title("t"), first.sessions_by_id["s"]["memo"]),
                             ("before", "before", "before"))
            notes[0]["title"] = tasks["t"]["title"] = records[0]["memo"] = "after"
            second = ReferenceSnapshot(UNUSED_ROOT)
            self.assertEqual((second.note_titles(["n"]), second.task_title("t"), second.sessions_by_id["s"]["memo"]),
                             ("after", "after", "after"))
            self.assertEqual(first.note_titles(["n"]), "before")
            self.assertEqual(first.task_title("t"), "before")
            self.assertEqual(first.sessions_by_id["s"]["memo"], "before")
            self.assertEqual([note_read.call_count, task_read.call_count, session_read.call_count], [2, 2, 2])

    def test_read_errors_propagate_and_do_not_poison_a_partially_built_index(self):
        cases = [("load_all_notes", lambda s: s.note_titles(["n"])),
                 ("load_task_lookup", lambda s: s.task_title("t")),
                 ("load_all_study_sessions", lambda s: s.sessions_for_id("s"))]
        for loader, operation in cases:
            with self.subTest(loader=loader), patch.object(reference_snapshot, loader, side_effect=JsonReadError("broken")) as read:
                snapshot = ReferenceSnapshot(UNUSED_ROOT)
                for _ in range(2):
                    with self.assertRaises(JsonReadError):
                        operation(snapshot)
                self.assertEqual(read.call_count, 2)


class ReferencePageTests(unittest.TestCase):
    def note_page(self):
        return page_functions("04", {"related_session_rows", "related_task_rows", "sec_to_hms", "yen"})

    def test_note_tables_share_one_history_and_keep_duplicate_and_unknown_rows(self):
        records = [session("s", "first", task="old"), session("s", "last", task="t"),
                   session("x", "extra", task="missing"), session("none", "unlinked", task="")]
        task_lookup = {"old": {"title": "old task", "date": "2026-09-15"},
                       "t": {"title": "new task", "date": "2026-09-16"}}
        page = {"linked_study_session_ids": ["s", "s", "absent", "x", "none"]}
        before = deepcopy((records, task_lookup, page))
        functions = self.note_page()
        with patch.object(reference_snapshot, "load_all_study_sessions", return_value=records) as read, \
             patch.object(reference_snapshot, "load_task_lookup", return_value=task_lookup) as task_read:
            snapshot = ReferenceSnapshot(UNUSED_ROOT)
            rows = functions["related_session_rows"](page, snapshot)
            tasks = functions["related_task_rows"](page, snapshot)
            self.assertEqual([row["勉強内容"] for row in rows], ["last", "extra", "unlinked", "不明なセッション"])
            self.assertEqual([(row["タスク名"], row["session_id"]) for row in tasks],
                             [("new task", "s"), ("new task", "s"), ("old task", "s"), ("old task", "s"), ("不明なタスク", "x")])
            read.assert_called_once_with(UNUSED_ROOT)
            task_read.assert_called_once_with(UNUSED_ROOT)
        self.assertEqual((records, task_lookup, page), before)

    def test_note_empty_links_do_not_read_history_or_tasks(self):
        functions = self.note_page()
        with patch.object(reference_snapshot, "load_all_study_sessions", side_effect=AssertionError("read")), \
             patch.object(reference_snapshot, "load_task_lookup", side_effect=AssertionError("read")):
            for name in ("related_session_rows", "related_task_rows"):
                self.assertEqual(functions[name]({"linked_study_session_ids": []}), [])

    def test_note_sessions_without_linked_tasks_do_not_load_plans(self):
        with patch.object(reference_snapshot, "load_all_study_sessions", return_value=[session("s", "note", task="")]), \
             patch.object(reference_snapshot, "load_task_lookup", side_effect=AssertionError("read")):
            self.assertEqual(self.note_page()["related_task_rows"]({"linked_study_session_ids": ["s"]}), [])

    def test_note_table_keeps_legacy_nonstring_session_id_semantics(self):
        records = [session(1, "numeric", task="t"), session("1", "text", task="t")]
        with patch.object(reference_snapshot, "load_all_study_sessions", return_value=records), \
             patch.object(reference_snapshot, "load_task_lookup", return_value={"t": {"title": "task", "date": "2026-09-16"}}):
            functions = self.note_page()
            snapshot = ReferenceSnapshot(UNUSED_ROOT)
            self.assertEqual(functions["related_session_rows"]({"linked_study_session_ids": [1]}, snapshot)[0]["勉強内容"], "text")
            self.assertEqual(functions["related_task_rows"]({"linked_study_session_ids": [1]}, snapshot)[0]["session_id"], 1)

    def test_daily_helpers_share_supplied_data_and_match_fallback_output(self):
        records = [session("s", "first"), session("s", "last"), session("x", "older", "2026-09-15T10:00:00")]
        notes = [{"note_id": "n", "title": "first"}, {"note_id": "n", "title": "last"}]
        session_read, note_read = Mock(return_value=records), Mock(return_value=notes)
        functions = page_functions("09", {"session_rows", "note_title_map", "sec_to_hms", "yen"},
                                   load_all_study_sessions=session_read, load_all_notes=note_read)
        ids = ["s", "x", "missing", "s", ""]
        expected = functions["session_rows"](ids)
        expected_notes = functions["note_title_map"]()
        session_read.reset_mock()
        note_read.reset_mock()
        lookup = {row.get("session_id"): row for row in records if row.get("session_id")}
        before = deepcopy((records, notes))
        for _ in range(10):
            self.assertEqual(functions["session_rows"](ids, lookup), expected)
            self.assertEqual(functions["note_title_map"](notes), expected_notes)
        session_read.assert_not_called()
        note_read.assert_not_called()
        self.assertEqual((records, notes), before)
        self.assertEqual(expected_notes, {"n": "last"})

    def test_daily_empty_supplied_data_is_not_reloaded(self):
        forbid = Mock(side_effect=AssertionError("read"))
        functions = page_functions("09", {"session_rows", "note_title_map", "sec_to_hms", "yen"},
                                   load_all_study_sessions=forbid, load_all_notes=forbid)
        self.assertEqual(functions["session_rows"](["missing"], {})[0]["内容"], "不明なセッション")
        self.assertEqual(functions["note_title_map"]([]), {})
        forbid.assert_not_called()

    def test_study_dataframe_loads_references_once_and_refreshes_on_next_call(self):
        rows = [session(str(i), f"memo {i}") for i in range(20)]
        notes = [{"note_id": "n", "title": "before"}]
        tasks = {"t": {"title": "before task"}}
        before = deepcopy(rows)
        functions = page_functions("02", {"load_study_df", "table_datetime", "date_from_source"},
                                   Path=Path, datetime=datetime, STUDY_DIR=UNUSED_ROOT,
                                   record_json_files=lambda _: [UNUSED_ROOT / "20260916.json"],
                                   load_sessions=lambda _: deepcopy(rows), sec_to_hms=str, yen=str,
                                   resolve_master_names_from_lookup=lambda row, lookup: {
                                       f"{key}_name": "" for key in ("project", "subject", "material", "unit")})
        with patch.object(reference_snapshot, "load_all_notes", side_effect=lambda _: deepcopy(notes)) as note_read, \
             patch.object(reference_snapshot, "load_task_lookup", side_effect=lambda _: deepcopy(tasks)) as task_read:
            first = functions["load_study_df"]({})
            self.assertEqual(first["related_notes"].tolist(), ["before"] * 20)
            self.assertEqual(first["related_task"].tolist(), ["before task"] * 20)
            self.assertEqual((note_read.call_count, task_read.call_count), (1, 1))
            notes[0]["title"] = "after"
            tasks["t"]["title"] = "after task"
            second = functions["load_study_df"]({})
            self.assertEqual(second["related_notes"].tolist(), ["after"] * 20)
            self.assertEqual(second["related_task"].tolist(), ["after task"] * 20)
            self.assertEqual((note_read.call_count, task_read.call_count), (2, 2))
        self.assertEqual(rows, before)


if __name__ == "__main__":
    unittest.main()
