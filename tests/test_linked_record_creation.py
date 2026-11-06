"""Run the pages' real creation transaction blocks against synthetic records."""
from __future__ import annotations

import ast
from copy import deepcopy
from datetime import date
from pathlib import Path
import unittest
from unittest.mock import patch

import data_utils
from app_core import app_clock, json_operations, json_store, reminder_service
from tests import test_study_log_editor as fixtures


class LinkedRecordCreationTests(unittest.TestCase):
    flows = {
        "flashcard_add": "07_記憶フラッシュ.py",
        "card_reminder_add": "07_記憶フラッシュ.py",
        "reminder_add": "08_忘却曲線リマインダー.py",
    }

    def setUp(self):
        self.fixture = fixtures.StudyLogEditorTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.enterContext(patch.object(app_clock, "DEMO_MODE_PATH", self.root / "settings/demo_mode.json"))
        self.fixture.seed_links()
        self.original = self.fixture.live_snapshot()

    def namespace(self):
        return {
            "ROOT": self.root,
            "atomic_json_operation": json_operations.atomic_json_operation,
            "save_flashcards": data_utils.save_flashcards,
            "link_notes_to_item": data_utils.link_notes_to_item,
            "create_reminder": reminder_service.create_reminder,
            "cards": data_utils.load_all_flashcards(self.root),
            "new_card": {"card_id": "card_synthetic", "flashcard_id": "card_synthetic",
                         "question": "synthetic question", "answer": "synthetic answer",
                         "source_note_id": "note_a", "linked_note_ids": ["note_a"]},
            "linked_note_ids": ["note_a"], "card_id": "card_synthetic",
            "source_note_id": "note_a", "note_id": "note_a",
            "current_card_id": "card_synthetic",
            "current_card": {"question": "synthetic card question", "project_id": "project_a"},
            "card_question": lambda card: card["question"],
            "app_today": lambda: date(2026, 9, 17),
            # The review fragment and reminder page share one captured date.
            "review_today": date(2026, 9, 17), "TODAY": date(2026, 9, 17),
            "first_review_date": date(2026, 9, 20),
            "title": "synthetic reminder", "memo": "synthetic memo", "url": "",
            "REVIEW_TYPE_OPTIONS": {"general": "一般"}, "review_type_label": "一般",
            "PRIORITY_OPTIONS": {"normal": "標準"}, "priority_label": "標準",
            "master_values": {"project_id": "project_a"},
            "schedule_mode": "custom", "custom_intervals": "2, 5, 10",
            "PRESET_INTERVALS": reminder_service.PRESET_INTERVALS,
        }

    def run_flow(self, flow, namespace=None):
        page = Path(__file__).resolve().parents[1] / "pages" / self.flows[flow]
        tree = ast.parse(page.read_text(encoding="utf-8-sig"))
        blocks = [node for node in ast.walk(tree) if isinstance(node, ast.With)
                  and any(isinstance(item.context_expr, ast.Call)
                          and isinstance(item.context_expr.func, ast.Name)
                          and item.context_expr.func.id == "atomic_json_operation"
                          and any(keyword.arg == "kind" and isinstance(keyword.value, ast.Constant)
                                  and keyword.value.value == flow for keyword in item.context_expr.keywords)
                          for item in node.items)]
        self.assertEqual(len(blocks), 1, "The actual UI submit flow must remain transaction-bound")
        block = deepcopy(blocks[0])
        calls = {node.func.id for node in ast.walk(block) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertIn("link_notes_to_item", calls)
        self.assertIn("save_flashcards" if flow == "flashcard_add" else "create_reminder", calls)
        namespace = self.namespace() if namespace is None else namespace
        code = compile(ast.fix_missing_locations(ast.Module(body=[block], type_ignores=[])), str(page), "exec")
        exec(code, namespace)
        return namespace

    def test_each_creation_saves_entity_and_note_backlink_together(self):
        for flow in self.flows:
            with self.subTest(flow=flow):
                values = self.run_flow(flow)
                if flow == "flashcard_add":
                    saved = data_utils.load_all_flashcards(self.root)
                    identifier, key = values["card_id"], "linked_flashcard_ids"
                    self.assertEqual(len(saved), 1)
                    self.assertEqual(saved[0]["question"], "synthetic question")
                else:
                    identifier, key = values["created"]["reminder_id"], "linked_reminder_ids"
                    saved = reminder_service.find_reminder_by_id(identifier, self.root)
                    self.assertEqual(saved["source_note_id"], "note_a")
                    self.assertEqual(saved["registered"], "2026-09-17")
                    if flow == "reminder_add":
                        self.assertEqual(saved["intervals_days"], [2, 5, 10])
                        self.assertEqual(saved["scheduled_date"], "2026-09-20")
                    else:
                        self.assertEqual(saved["algorithm"], "sm2")
                self.assertIn(identifier, self.fixture.note_pages()[0][key])
                self.assertEqual(json_operations.pending_json_operations(self.root), [])

    def test_backlink_preparation_failure_writes_no_live_entity(self):
        for flow in self.flows:
            with self.subTest(flow=flow):
                with patch.object(data_utils, "save_note_store", side_effect=OSError("synthetic note staging failure")):
                    with self.assertRaises(OSError):
                        self.run_flow(flow)
                self.assertEqual(self.fixture.live_snapshot(), self.original)
                self.assertEqual(json_operations.pending_json_operations(self.root), [])

    def test_failure_committing_note_restores_entity_and_allows_single_retry(self):
        for flow in self.flows:
            with self.subTest(flow=flow):
                save = json_store.save_json_safe

                def fail_note(path, payload, **kwargs):
                    if Path(path) == self.root / "notes_data/notebooks.json" and json_store._committing_operation.get() is not None:
                        raise OSError("synthetic note commit failure")
                    return save(path, payload, **kwargs)

                before = self.fixture.live_snapshot()
                with patch.object(json_store, "save_json_safe", side_effect=fail_note):
                    with self.assertRaises(json_store.JsonStoreError):
                        self.run_flow(flow)
                self.assertEqual(self.fixture.live_snapshot(), before)
                self.assertEqual(json_operations.pending_json_operations(self.root), [])
                self.run_flow(flow)
        self.assertEqual(len(data_utils.load_all_flashcards(self.root)), 1)
        self.assertEqual(len(reminder_service.load_reminders(self.root)), 2)

    def test_process_interruption_remains_explicitly_recoverable_for_each_flow(self):
        for flow in self.flows:
            with self.subTest(flow=flow):
                save = json_store.save_json_safe

                def interrupt_note(path, payload, **kwargs):
                    if Path(path) == self.root / "notes_data/notebooks.json" and json_store._committing_operation.get() is not None:
                        raise KeyboardInterrupt("synthetic process interruption")
                    return save(path, payload, **kwargs)

                with patch.object(json_store, "save_json_safe", side_effect=interrupt_note):
                    with self.assertRaises(KeyboardInterrupt):
                        self.run_flow(flow)
                pending = json_operations.pending_json_operations(self.root)
                self.assertEqual(len(pending), 1)
                self.assertNotEqual(self.fixture.live_snapshot(), self.original)
                json_operations.rollback_json_operation(pending[0]["operation_id"], self.root)
                self.assertEqual(self.fixture.live_snapshot(), self.original)

    def test_concurrent_card_update_is_not_overwritten_by_displayed_list(self):
        namespace = self.namespace()
        self.fixture.write_json("flashcards.json", [{"card_id": "external", "question": "keep external"}])
        before = self.fixture.live_snapshot()
        with self.assertRaises(json_store.JsonConflictError):
            self.run_flow("flashcard_add", namespace)
        self.assertEqual(self.fixture.live_snapshot(), before)

    def test_creation_without_related_notes_still_saves_normally(self):
        for flow in self.flows:
            with self.subTest(flow=flow):
                namespace = self.namespace()
                namespace.update(note_id="", source_note_id="", linked_note_ids=[])
                namespace["new_card"].update(source_note_id="", linked_note_ids=[])
                before_notes = (self.root / "notes_data/notebooks.json").read_bytes()
                self.run_flow(flow, namespace)
                self.assertEqual((self.root / "notes_data/notebooks.json").read_bytes(), before_notes)


if __name__ == "__main__":
    unittest.main()
