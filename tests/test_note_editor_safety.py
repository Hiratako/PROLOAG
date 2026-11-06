from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from app_core import app_clock, json_store, note_editor as editor, settings_service
from app_core.json_store import JsonConflictError, JsonStoreError


class NoteEditorSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="proloag_note_test_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        original_read = json_store._read

        def isolated_read(path):
            self.assertTrue(Path(path).resolve().is_relative_to(self.root), f"Out-of-fixture JSON read: {path}")
            return original_read(path)

        for patcher in (
            patch.object(json_store, "_read", side_effect=isolated_read),
            patch.object(json_store, "PROJECT_ROOT", self.root),
            patch.object(app_clock, "DEMO_MODE_PATH", self.root / "settings/demo_mode.json"),
            patch.object(settings_service, "USER_SETTINGS_PATH", self.root / "settings/user_settings.json"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        note = {"id": "legacy_alias", "note_id": "n1", "title": "original", "body": "body A", "content": "body A",
                "tags": [], "unknown": {"preserve": [1, 2]}, "created_at": "2026-09-01T00:00:00",
                "updated_at": "2026-09-01T00:00:00", "linked_study_session_ids": ["s1"],
                "linked_flashcard_ids": ["c1"], "linked_reminder_ids": ["r1"]}
        self.put(editor.NOTE_PATH, {"schema_extra": {"keep": True}, "notebooks": [
            {"id": "b1", "name": "same", "unknown_book": 3, "sections": [
                {"id": "sec1", "name": "same", "extra": 2, "pages": [note, {"id": "n2", "note_id": "n2", "title": "second", "body": "two"}]},
                {"id": "sec2", "name": "same", "pages": [{"id": "n3", "note_id": "n3", "title": "third", "body": "three"}]},
            ]},
            {"id": "b2", "name": "same", "sections": [{"id": "sec3", "name": "same", "pages": [{"id": "n4", "note_id": "n4", "title": "fourth", "body": "four"}]}]},
        ]})
        self.study = "study_logs/2026/09/20260901.json"
        self.plan = "daily_plans/2026/09/20260901.json"
        self.put(self.study, {"envelope": "preserve", "sessions": [{"session_id": "s1", "duration_sec": 60,
                  "start_at": "2026-09-01T10:00:00", "linked_note_ids": ["n1", "n2"], "unknown": 3}]})
        self.put(self.plan, {"plan_id": "p1", "date": "2026-09-01", "review_note_ids": ["n1"], "unknown": 7,
                 "tasks": [{"task_id": "t1", "title": "preserve task", "linked_note_ids": ["legacy_alias", "n2"]}]})
        self.put("flashcards.json", [{"card_id": "c1", "question": "keep card", "linked_note_ids": ["n1", "n2"], "source_note_id": "n1", "unknown": 4}])
        self.put("reminders.json", [{"reminder_id": "r1", "title": "keep reminder", "linked_note_ids": ["n1"], "source_note_id": "n1", "unknown": 5}])

    def put(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, relative):
        return json.loads((self.root / relative).read_text(encoding="utf-8"))

    def snapshot(self, note_id="n1"):
        return editor.capture_note_snapshot(editor.load_note_workspace(self.root), note_id)

    def live_bytes(self):
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*.json")
                if "backups" not in path.relative_to(self.root).parts or path.relative_to(self.root).as_posix() == editor.TRASH_PATH}

    def test_read_and_preview_are_readonly_and_preserve_aliases(self):
        before = self.live_bytes()
        plan = editor.prepare_note_delete(self.snapshot(), root=self.root)
        self.assertEqual(len(plan["references"]), 5)
        self.assertEqual(plan["note"]["id"], "legacy_alias")
        self.assertEqual(self.live_bytes(), before)

    def test_stale_save_survives_internal_reread_and_repeated_retry(self):
        stale = self.snapshot()
        editor.save_note_edits(self.snapshot(), {"title": "new title"}, root=self.root)
        editor.load_note_workspace(self.root)
        for _ in range(2):
            with self.assertRaises(JsonConflictError):
                editor.save_note_edits(stale, {"body": "stale replacement"}, root=self.root)
        note = self.get(editor.NOTE_PATH)["notebooks"][0]["sections"][0]["pages"][0]
        self.assertEqual(note["title"], "new title")
        self.assertEqual(note["body"], "body A")
        self.assertEqual(note["id"], "legacy_alias")
        self.assertEqual(note["unknown"], {"preserve": [1, 2]})

    def test_delete_and_restore_keep_entities_unknown_fields_and_ids(self):
        original = self.get(editor.NOTE_PATH)
        plan = editor.prepare_note_delete(self.snapshot(), root=self.root)
        editor.commit_note_plan(plan, root=self.root)
        # A retry of the same operation is a no-op, even with the old guards.
        editor.commit_note_plan(plan, root=self.root)
        self.assertEqual(self.get(self.study)["sessions"][0]["linked_note_ids"], ["n2"])
        self.assertEqual(self.get(self.study)["envelope"], "preserve")
        self.assertEqual(self.get(self.plan)["review_note_ids"], [])
        self.assertEqual(self.get(self.plan)["tasks"][0]["linked_note_ids"], ["n2"])
        self.assertEqual(self.get("flashcards.json")[0]["question"], "keep card")
        self.assertEqual(self.get("flashcards.json")[0]["source_note_id"], "")
        self.assertEqual(self.get("reminders.json")[0]["title"], "keep reminder")
        entries = editor.list_deleted_notes(self.root)
        self.assertEqual(len(entries), 1)
        restored = editor.prepare_note_restore(entries[0]["trash_id"], root=self.root)
        editor.commit_note_plan(restored, root=self.root)
        self.assertEqual(self.get(editor.NOTE_PATH), original)
        self.assertEqual(self.get(self.plan)["tasks"][0]["linked_note_ids"], ["n2", "legacy_alias"])
        self.assertEqual(self.get("flashcards.json")[0]["source_note_id"], "n1")
        self.assertEqual(editor.list_deleted_notes(self.root), [])

    def test_restore_does_not_recreate_missing_targets_or_replace_new_source(self):
        editor.commit_note_plan(editor.prepare_note_delete(self.snapshot(), root=self.root), root=self.root)
        self.put("flashcards.json", [])
        reminders = self.get("reminders.json")
        reminders[0]["source_note_id"] = "n2"
        self.put("reminders.json", reminders)
        entry = editor.list_deleted_notes(self.root)[0]
        plan = editor.prepare_note_restore(entry["trash_id"], root=self.root)
        self.assertGreaterEqual(len(plan["warnings"]), 2)
        editor.commit_note_plan(plan, root=self.root)
        self.assertEqual(self.get("flashcards.json"), [])
        self.assertEqual(self.get("reminders.json")[0]["source_note_id"], "n2")
        note = self.get(editor.NOTE_PATH)["notebooks"][0]["sections"][0]["pages"][0]
        self.assertEqual(note["linked_flashcard_ids"], [])

    def test_restore_skips_reused_ids_and_changed_record_identity(self):
        editor.commit_note_plan(editor.prepare_note_delete(self.snapshot(), root=self.root), root=self.root)
        plan = self.get(self.plan)
        plan["tasks"][0]["title"] = "unrelated replacement task with reused t1"
        self.put(self.plan, plan)
        cards = self.get("flashcards.json")
        cards[0]["question"] = "unrelated replacement card with reused c1"
        self.put("flashcards.json", cards)
        sessions = self.get(self.study)
        sessions["sessions"][0]["duration_sec"] = 999
        self.put(self.study, sessions)
        entry = editor.list_deleted_notes(self.root)[0]
        restoration = editor.prepare_note_restore(entry["trash_id"], root=self.root)
        self.assertTrue(restoration["warnings"])
        editor.commit_note_plan(restoration, root=self.root)
        self.assertEqual(self.get(self.plan)["tasks"][0]["linked_note_ids"], ["n2"])
        self.assertEqual(self.get("flashcards.json")[0]["linked_note_ids"], ["n2"])
        self.assertEqual(self.get(self.study)["sessions"][0]["linked_note_ids"], ["n2"])
        note = self.snapshot()["raw_note"]
        self.assertEqual(note["linked_flashcard_ids"], [])
        self.assertEqual(note["linked_study_session_ids"], [])
        self.assertEqual(note["linked_reminder_ids"], ["r1"])

    def test_all_documents_and_new_file_inventory_are_guarded(self):
        plan = editor.prepare_note_delete(self.snapshot(), root=self.root)
        self.put("study_logs/2026/09/20260902.json", [{"session_id": "s2", "linked_note_ids": ["n1"]}])
        before = self.live_bytes()
        with self.assertRaises(JsonConflictError):
            editor.commit_note_plan(plan, root=self.root)
        self.assertEqual(self.live_bytes(), before)
        plan = editor.prepare_note_delete(self.snapshot(), root=self.root)
        cards = self.get("flashcards.json")
        cards[0]["unknown"] = 99
        self.put("flashcards.json", cards)
        before = self.live_bytes()
        with self.assertRaises(JsonConflictError):
            editor.commit_note_plan(plan, root=self.root)
        self.assertEqual(self.live_bytes(), before)

    def test_delete_write_failure_rolls_back_every_live_document(self):
        plan = editor.prepare_note_delete(self.snapshot(), root=self.root)
        before = self.live_bytes()
        original_save = json_store.save_json_safe
        failed = False

        def fail_live_card(path, data, **kwargs):
            nonlocal failed
            if Path(path) == self.root / "flashcards.json" and json_store._active_operation.get() is None and not failed:
                failed = True
                raise OSError("synthetic failure")
            return original_save(path, data, **kwargs)

        with patch.object(json_store, "save_json_safe", side_effect=fail_live_card):
            with self.assertRaises(JsonStoreError):
                editor.commit_note_plan(plan, root=self.root)
        self.assertTrue(failed)
        self.assertEqual(self.live_bytes(), before)
        editor.commit_note_plan(plan, root=self.root)
        self.assertEqual(len(editor.list_deleted_notes(self.root)), 1)

    def test_linked_card_creation_is_atomic_and_idempotent(self):
        snapshot = self.snapshot()
        operation_id = uuid4().hex
        result = editor.create_note_linked_record(snapshot, "card", {"question": "new", "answer": "yes"},
                                                   operation_id=operation_id, root=self.root)
        retry = editor.create_note_linked_record(snapshot, "card", {"question": "ignored duplicate"},
                                                  operation_id=operation_id, root=self.root)
        record_id = result["record_id"]
        self.assertEqual(record_id, retry["record_id"])
        self.assertIsNone(retry["snapshot"])
        self.assertEqual(len([row for row in self.get("flashcards.json") if row["card_id"] == record_id]), 1)
        self.assertIn(record_id, self.get(editor.NOTE_PATH)["notebooks"][0]["sections"][0]["pages"][0]["linked_flashcard_ids"])

    def test_linked_card_failure_never_leaves_an_orphan(self):
        before = self.live_bytes()
        snapshot = self.snapshot()
        original_save = json_store.save_json_safe
        failed = False

        def fail_note_commit(path, data, **kwargs):
            nonlocal failed
            if Path(path) == self.root / editor.NOTE_PATH and json_store._active_operation.get() is None and not failed:
                failed = True
                raise OSError("synthetic second-document failure")
            return original_save(path, data, **kwargs)

        operation_id = uuid4().hex
        with patch.object(json_store, "save_json_safe", side_effect=fail_note_commit):
            with self.assertRaises(JsonStoreError):
                editor.create_note_linked_record(snapshot, "card", {"question": "temporary"},
                                                 operation_id=operation_id, root=self.root)
        self.assertTrue(failed)
        self.assertEqual(self.live_bytes(), before)
        result = editor.create_note_linked_record(snapshot, "card", {"question": "temporary"},
                                                  operation_id=operation_id, root=self.root)
        self.assertTrue(result["snapshot"])

    def test_linked_reminder_creation_is_atomic_and_idempotent(self):
        snapshot = self.snapshot()
        operation_id = uuid4().hex
        result = editor.create_note_linked_record(snapshot, "reminder", {"title": "new reminder", "scheduled_date": "2026-09-20"},
                                                  operation_id=operation_id, root=self.root, schedule_mode="standard")
        editor.create_note_linked_record(snapshot, "reminder", {"title": "ignored duplicate"},
                                         operation_id=operation_id, root=self.root)
        record_id = result["record_id"]
        self.assertEqual(len([row for row in self.get("reminders.json") if row["reminder_id"] == record_id]), 1)
        self.assertIn(record_id, self.snapshot()["raw_note"]["linked_reminder_ids"])

    def test_success_snapshot_does_not_adopt_a_later_external_revision(self):
        committed = editor.save_note_edits(self.snapshot(), {"title": "my save"}, root=self.root)
        self.assertEqual(committed["revision"], self.snapshot()["revision"])
        editor.save_note_edits(self.snapshot(), {"title": "later external save"}, root=self.root)
        with self.assertRaises(JsonConflictError):
            editor.save_note_edits(committed, {"body": "continued old draft"}, root=self.root)

    def test_protected_fields_and_last_page_are_rejected(self):
        with self.assertRaises(ValueError):
            editor.save_note_edits(self.snapshot(), {"note_id": "changed"}, root=self.root)
        with self.assertRaises(ValueError):
            editor.prepare_note_delete(self.snapshot("n3"), root=self.root)

    def page_app(self):
        from streamlit import config
        from streamlit.testing.v1 import AppTest

        config.get_config_options(options_from_flags={"global.developmentMode": False, "browser.gatherUsageStats": False})
        page = Path(__file__).resolve().parents[1] / "pages/04_ノート・知識ベース.py"
        source = page.read_text(encoding="utf-8-sig")
        source = source.replace("ROOT = Path(__file__).resolve().parents[1]", f"ROOT = Path({str(self.root)!r})")
        source = source.replace("import prolog_ui.components as ab", """from prolog_ui.components import show_action_error as _show_action_error
class _Decoration:
    show_action_error = staticmethod(_show_action_error)
    def __getattr__(self, name):
        return lambda *args, **kwargs: None
ab = _Decoration()""")
        source = source.replace("from master_ui import render_master_quick_add", "def render_master_quick_add(*args, **kwargs):\n    pass")
        return AppTest.from_string(source, default_timeout=30)

    @staticmethod
    def button(app, label):
        return next(item for item in app.button if item.label == label)

    def test_duplicate_names_are_selectable_in_a_fresh_ui_session(self):
        app = self.page_app().run()
        self.assertEqual(len(app.exception), 0)
        notebooks = next(item for item in app.selectbox if item.label == "ノートブック選択")
        self.assertEqual(len(notebooks.options), 2)
        sections = next(item for item in app.radio if item.label == "セクション選択")
        self.assertEqual(len(sections.options), 2)
        sections.set_value("sec2").run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.session_state["note_section_id"], "sec2")
        next(item for item in app.selectbox if item.label == "ノートブック選択").set_value("b2").run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.session_state["note_notebook_id"], "b2")

    def test_two_ui_sessions_reject_stale_input_across_reruns(self):
        first, second = self.page_app().run(), self.page_app().run()
        self.assertEqual(len(first.exception), 0)
        first.text_area(key="body_n1").set_value("unsaved A").run()
        second.text_input(key="title_n1").set_value("saved by B").run()
        self.button(second, "保存").click().run()
        self.assertEqual(len(second.exception), 0)
        self.button(first, "保存").click().run()
        self.assertEqual(len(first.exception), 0)
        self.assertTrue(first.error)
        self.assertEqual(first.text_area(key="body_n1").value, "unsaved A")
        self.button(first, "保存").click().run()
        self.assertTrue(first.error)
        note = self.get(editor.NOTE_PATH)["notebooks"][0]["sections"][0]["pages"][0]
        self.assertEqual(note["title"], "saved by B")
        self.assertEqual(note["body"], "body A")
        self.button(first, "最新内容を再読込（このノートの未保存入力を破棄）").click().run()
        self.assertEqual(len(first.exception), 0)
        self.assertEqual(first.text_input(key="title_n1").value, "saved by B")
        self.assertEqual(first.text_area(key="body_n1").value, "body A")

    def test_ui_delete_requires_confirmation_and_restore_returns_original_id(self):
        app = self.page_app().run()
        before = self.live_bytes()
        self.button(app, "削除").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(self.live_bytes(), before)
        self.assertTrue(self.button(app, "ごみ箱へ移動を確定").disabled)
        next(item for item in app.checkbox if item.label == "対象と関連付けの変更を確認しました").check().run()
        self.button(app, "ごみ箱へ移動を確定").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(editor.list_deleted_notes(self.root)), 1)
        self.button(app, "復元内容を確認").click().run()
        self.assertTrue(self.button(app, "復元を確定").disabled)
        next(item for item in app.checkbox if item.label == "対象と関連付けの変更を確認しました").check().run()
        self.button(app, "復元を確定").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.session_state["note_page_id"], "n1")
        self.assertEqual(app.text_input(key="title_n1").value, "original")
        self.assertEqual(editor.list_deleted_notes(self.root), [])

    def test_empty_workspace_keeps_draft_ids_across_reruns(self):
        (self.root / editor.NOTE_PATH).unlink()
        app = self.page_app().run()
        self.assertEqual(len(app.exception), 0)
        note_id = app.session_state["note_page_id"]
        app.text_area(key=f"body_{note_id}").set_value("first note draft").run()
        self.assertEqual(app.session_state["note_page_id"], note_id)
        self.button(app, "保存").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.session_state["note_page_id"], note_id)
        self.assertEqual(app.text_area(key=f"body_{note_id}").value, "first note draft")

    def test_external_deletion_keeps_draft_until_explicit_discard(self):
        app = self.page_app().run()
        app.text_area(key="body_n1").set_value("unsaved draft after external deletion").run()
        app.text_input(key="tags_n1").set_value("draft tag").run()
        editor.commit_note_plan(editor.prepare_note_delete(self.snapshot(), root=self.root), root=self.root)
        before = self.live_bytes()
        for _ in range(2):
            app.run()
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(app.session_state["note_page_id"], "n1")
            self.assertEqual(app.text_area(key="body_n1").value, "unsaved draft after external deletion")
            self.assertEqual(app.session_state["note_editor_drafts"]["n1"]["tags_n1"], "draft tag")
            self.assertFalse(any(button.label == "保存" for button in app.button))
            self.assertEqual(self.live_bytes(), before)
        self.button(app, "未保存入力を破棄して別のノートへ移動").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertNotEqual(app.session_state["note_page_id"], "n1")
        self.assertNotIn("n1", app.session_state["note_editor_drafts"])

    def test_navigation_keeps_uncommitted_note_inputs(self):
        app = self.page_app().run()
        app.text_area(key="body_n1").set_value("draft across navigation").run()
        self.button(app, "second").click().run()
        self.assertEqual(app.session_state["note_page_id"], "n2")
        self.button(app, "original").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.text_area(key="body_n1").value, "draft across navigation")

    def test_ui_body_save_keeps_inactive_or_unknown_classification_ids(self):
        store = self.get(editor.NOTE_PATH)
        unknown_ids = {key: f"old_{key}" for key in ("project_id", "subject_id", "material_id", "unit_id")}
        store["notebooks"][0]["sections"][0]["pages"][0].update(unknown_ids)
        self.put(editor.NOTE_PATH, store)
        app = self.page_app().run()
        app.text_area(key="body_n1").set_value("body change only").run()
        self.button(app, "保存").click().run()
        self.assertEqual(len(app.exception), 0)
        saved = self.snapshot()["raw_note"]
        self.assertEqual({key: saved[key] for key in unknown_ids}, unknown_ids)
        self.assertEqual(saved["body"], "body change only")

    def test_ui_new_ids_reserve_live_alias_and_deleted_ids(self):
        store = self.get(editor.NOTE_PATH)
        alias = f"note_{datetime.now():%Y%m%d}_001"
        store["notebooks"][0]["sections"][0]["pages"][0]["id"] = alias
        self.put(editor.NOTE_PATH, store)
        app = self.page_app().run()
        self.button(app, "ページ追加").click().run()
        self.assertEqual(len(app.exception), 0)
        created_id = app.session_state["note_page_id"]
        self.assertNotEqual(created_id, alias)
        self.assertEqual(self.get(editor.NOTE_PATH)["notebooks"][0]["sections"][0]["pages"][0]["id"], alias)
        self.button(app, "削除").click().run()
        next(item for item in app.checkbox if item.label == "対象と関連付けの変更を確認しました").check().run()
        self.button(app, "ごみ箱へ移動を確定").click().run()
        self.button(app, "ページ追加").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertNotIn(app.session_state["note_page_id"], {alias, created_id})
        entry = editor.list_deleted_notes(self.root)[0]
        editor.commit_note_plan(editor.prepare_note_restore(entry["trash_id"], root=self.root), root=self.root)
        self.assertEqual(self.snapshot(created_id)["note"]["note_id"], created_id)


if __name__ == "__main__":
    unittest.main()
