from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from app_core import (
    diary_service, exercise_service, flashcard_service, health_service,
    master_service, note_service, plan_service, reminder_service,
    settings_service, substance_service,
)
from app_core.json_store import JsonReadError
from app_core import json_store


class StorageIntegrationTests(unittest.TestCase):
    """All fixtures and generated backup/lock files stay below a temporary root."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="proloag_integration_")
        self.root = Path(self.temp.name)
        self.target = date(2026, 8, 28)
        self.patches = ExitStack()
        for module, field, relative in (
            (diary_service, "DIARY_ENTRIES_DIR", "diary_entries"),
            (health_service, "HEALTH_LOGS_DIR", "health_logs"),
            (exercise_service, "EXERCISE_LOGS_DIR", "exercise_logs"),
            (settings_service, "USER_SETTINGS_PATH", "settings/user_settings.json"),
            (substance_service, "SUBSTANCE_PROFILES_PATH", "settings/substance_profiles.json"),
        ):
            self.patches.enter_context(patch.object(module, field, self.root / relative))

    def tearDown(self):
        self.patches.close()
        self.temp.cleanup()

    def parallel(self, function, count=12):
        barrier = threading.Barrier(count)

        def run(index):
            barrier.wait(timeout=10)
            return function(index)

        with ThreadPoolExecutor(max_workers=count) as pool:
            futures = [pool.submit(run, index) for index in range(count)]
            return [future.result(timeout=30) for future in futures]

    def test_parallel_substances_and_caffeine_mirror_use_actual_intake_day(self):
        wrong_target = self.target - timedelta(days=1)

        def add(index):
            taken_at = datetime(2026, 8, 28, 8, index)
            if index % 3 == 0:
                return diary_service.add_caffeine_record(wrong_target, 10, taken_at=taken_at)
            substance = "caffeine" if index % 3 == 1 else "melatonin"
            return substance_service.add_substance_record(
                wrong_target, substance, 10 if substance == "caffeine" else 0.5,
                taken_at=taken_at, label="synthetic",
            )

        self.parallel(add)
        entry = diary_service.load_diary_entry(self.target)
        records = entry["substance_records"]
        self.assertEqual(len(records), 12)
        self.assertEqual(len({r["substance_record_id"] for r in records}), 12)
        caffeine = [r for r in records if r["substance_id"] == "caffeine"]
        self.assertEqual(len(caffeine), 8)
        self.assertEqual(entry["caffeine_mg"], 80)
        mirror = entry["caffeine_records"]
        self.assertEqual(len(mirror), 8)
        self.assertEqual(
            {(r["substance_record_id"], r["taken_at"], r["amount_mg"]) for r in caffeine},
            {(r["caffeine_id"], r["taken_at"], r["amount_mg"]) for r in mirror},
        )
        self.assertFalse(diary_service.get_diary_entry_path(wrong_target).exists())

    def test_parallel_exercise_additions(self):
        self.parallel(lambda index: exercise_service.add_exercise_record(
            self.target, {"type": "walking", "duration_min": index + 1, "title": f"synthetic-{index}"},
        ))
        records = exercise_service.load_exercise_log(self.target)["records"]
        self.assertEqual(len(records), 12)
        self.assertEqual(len({r["exercise_id"] for r in records}), 12)
        self.assertEqual(sum(r["duration_min"] for r in records), sum(range(1, 13)))

    def test_parallel_notes_return_their_own_saved_ids(self):
        returned = self.parallel(lambda index: note_service.add_note(
            f"synthetic-{index}", root=self.root,
        ))
        notes = note_service.load_all_notes(self.root)
        generated = [r for r in notes if r["title"].startswith("synthetic-")]
        self.assertEqual(len(generated), 12)
        self.assertEqual(len(set(returned)), 12)
        by_id = {r["note_id"]: r for r in notes}
        self.assertEqual(len(by_id), len(notes))
        for index, returned_id in enumerate(returned):
            self.assertEqual(by_id[returned_id]["title"], f"synthetic-{index}")

    def test_parallel_task_additions(self):
        returned = self.parallel(lambda index: plan_service.add_task(
            self.target, {"title": f"synthetic-{index}"}, root=self.root,
        ))
        tasks = plan_service.load_daily_plan(self.target, root=self.root)["tasks"]
        self.assertEqual(len(tasks), 12)
        self.assertEqual(len({r["task_id"] for r in tasks}), 12)
        self.assertEqual({r["task_id"] for r in returned}, {r["task_id"] for r in tasks})

    def test_arbitrary_root_repeated_links_ignore_fallback_backups(self):
        from app_core import study_log_service, export_service
        from data_utils import load_all_daily_plans

        self.assertNotEqual(json_store.PROJECT_ROOT, self.root)
        record = {"start_at": "2026-08-28T09:00:00", "end_at": "2026-08-28T10:00:00",
                  "duration_sec": 3600, "money": 1000, "linked_note_ids": []}
        path = study_log_service.append_study_session(record, self.root)
        for index in range(3):
            self.assertTrue(study_log_service.attach_note_to_session(record["session_id"], f"note_{index}", self.root))
            self.assertTrue(study_log_service.attach_task_to_session(record["session_id"], f"task_{index}", self.root))
            self.assertTrue(study_log_service.attach_note_to_session(record["session_id"], f"note_{index}", self.root))
        self.assertTrue(any(p.is_file() for p in (path.parent / "backups").rglob("*.json")))
        rows = study_log_service.load_all_study_sessions(self.root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["linked_note_ids"], ["note_0", "note_1", "note_2"])
        self.assertEqual(rows[0]["linked_task_id"], "task_2")
        self.assertEqual(study_log_service.ensure_session_ids(self.root), 0)
        warnings = []
        self.assertEqual(len(export_service._directory_refs(self.root, "study_logs", warnings)), 1)
        self.assertEqual(warnings, [])
        plan_path = self.root / "daily_plans" / "2026" / "08" / "2026-08-28.json"
        json_store.save_json_safe(plan_path, {"date": "2026-08-28", "tasks": []})
        json_store.save_json_safe(plan_path, {"date": "2026-08-28", "tasks": [], "memo": "updated"})
        self.assertEqual(len(load_all_daily_plans(self.root)), 1)

    def test_captured_revision_survives_internal_reload(self):
        path = self.root / "revision.json"
        self.assertIs(json_store.observed_json_revision(path), json_store._UNSET)
        json_store.load_json_safe(path, {})
        self.assertIsNone(json_store.observed_json_revision(path))
        json_store.save_json_safe(path, {"x": 1})
        original_revision = json_store.observed_json_revision(path)
        self.assertIsInstance(original_revision, str)
        path.write_text('{"x": 2}', encoding="utf-8")
        json_store.load_json_safe(path, {})
        self.assertNotEqual(json_store.observed_json_revision(path), original_revision)
        with self.assertRaises(json_store.JsonConflictError):
            json_store.save_json_safe(path, {"x": 3}, expected_revision=original_revision)
        self.assertEqual(path.read_text(), '{"x": 2}')

    def test_service_snapshot_conflict_and_retry_require_explicit_reload(self):
        def change_diary(data, value):
            data["title"] = value

        def change_health(data, value):
            data["condition"]["memo"] = value

        def change_exercise(data, value):
            data["records"][0]["title"] = value

        def change_profiles(data, value):
            data[0]["default_label"] = value

        exercise_service.add_exercise_record(self.target, {"type": "walking", "duration_min": 5})
        cases = (
            ("diary", diary_service.get_diary_entry_path(self.target),
             lambda: diary_service.load_diary_entry(self.target),
             lambda data: diary_service.save_diary_entry(self.target, data), change_diary),
            ("health", health_service.get_health_log_path(self.target),
             lambda: health_service.load_health_log(self.target),
             lambda data: health_service.save_health_log(self.target, data), change_health),
            ("exercise", exercise_service.get_exercise_log_path(self.target),
             lambda: exercise_service.load_exercise_log(self.target),
             lambda data: exercise_service.save_exercise_log(self.target, data), change_exercise),
            ("profiles", substance_service.SUBSTANCE_PROFILES_PATH,
             substance_service.load_substance_profiles,
             substance_service.save_substance_profiles, change_profiles),
        )
        for label, path, load, save, change in cases:
            with self.subTest(service=label):
                initial = load()
                save(initial)
                stale = deepcopy(load())
                old_revision = json_store.observed_json_revision(path)
                change(stale, "synthetic-local")

                def remote_update():
                    current = load()
                    change(current, "synthetic-remote")
                    save(current)

                with ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(remote_update).result(timeout=15)
                remote_bytes = path.read_bytes()
                for attempt in range(2):
                    with self.subTest(attempt=attempt):
                        with self.assertRaises(json_store.JsonConflictError):
                            save(stale)
                        self.assertEqual(path.read_bytes(), remote_bytes)
                        self.assertEqual(json_store.observed_json_revision(path), old_revision)
                fresh = load()
                change(fresh, "synthetic-after-reload")
                save(fresh)
                self.assertNotEqual(path.read_bytes(), remote_bytes)

    @unittest.skipUnless(os.name == "nt", "Windows extended path spelling")
    def test_extended_path_keys_and_backup_root_are_canonical(self):
        for regular, extended in (
            (r"C:\temporary\record.json", r"\\?\C:\temporary\record.json"),
            (r"\\server\share\record.json", r"\\?\UNC\server\share\record.json"),
        ):
            with self.subTest(path_kind=regular[:2]):
                with patch.object(Path, "resolve", return_value=Path(regular)):
                    ordinary_key = json_store._key(Path("unused"))
                with patch.object(Path, "resolve", return_value=Path(extended)):
                    extended_key = json_store._key(Path("unused"))
                self.assertEqual(ordinary_key, extended_key)

        path = self.root / "diary_entries" / "record.json"
        with patch.object(json_store, "PROJECT_ROOT", self.root):
            json_store.save_json_safe(path, {"x": 1})
            extended_path = Path("\\\\?\\" + str(path))
            # Exercise a prefix on the source itself, rather than relying on
            # Windows reproducing the transient resolve spelling every run.
            json_store._save_generation(extended_path, b'{"x":1}')
        generations = [p for p in (self.root / "backups").rglob("*.json") if p.is_file()]
        self.assertEqual(len(generations), 1)
        self.assertFalse((path.parent / "backups").exists())

    def test_corrupt_existing_files_cannot_be_saved_by_services(self):
        cases = (
            (diary_service.get_diary_entry_path(self.target),
             lambda: diary_service.save_diary_entry(self.target, {"title": "synthetic"})),
            (health_service.get_health_log_path(self.target),
             lambda: health_service.save_health_log(self.target, {})),
            (exercise_service.get_exercise_log_path(self.target),
             lambda: exercise_service.save_exercise_log(self.target, {})),
            (self.root / "notes_data/notebooks.json",
             lambda: note_service.save_notebooks({"notebooks": []}, root=self.root)),
            (plan_service.get_plan_path(self.target, root=self.root),
             lambda: plan_service.save_daily_plan(self.target, {}, root=self.root)),
            (self.root / "flashcards.json",
             lambda: flashcard_service.save_flashcards([], root=self.root)),
            (self.root / "reminders.json",
             lambda: reminder_service.save_reminders([], root=self.root)),
            (self.root / "master_data/master.json",
             lambda: master_service.save_master_data({}, root=self.root)),
            (settings_service.USER_SETTINGS_PATH,
             lambda: settings_service.save_user_settings({})),
            (substance_service.SUBSTANCE_PROFILES_PATH,
             lambda: substance_service.save_substance_profiles(substance_service.default_substance_profiles())),
        )
        for path, operation in cases:
            with self.subTest(dataset=path.relative_to(self.root)):
                path.parent.mkdir(parents=True, exist_ok=True)
                raw = b'{"broken":'
                path.write_bytes(raw)
                with self.assertRaises(JsonReadError):
                    operation()
                self.assertEqual(path.read_bytes(), raw)

    def test_normal_reads_preserve_bytes_mtime_and_directory_contents(self):
        diary_service.save_diary_entry(self.target, {"title": "synthetic"})
        health_service.save_health_log(self.target, {})
        exercise_service.add_exercise_record(self.target, {"type": "walking", "duration_min": 5})
        note_service.add_note("synthetic", root=self.root)
        plan_service.add_task(self.target, {"title": "synthetic"}, root=self.root)
        flashcard_service.save_flashcards([], root=self.root)
        reminder_service.save_reminders([], root=self.root)
        master_service.save_master_data({}, root=self.root)
        settings_service.save_user_settings({})
        substance_service.save_substance_profiles(substance_service.default_substance_profiles())

        def snapshot():
            return {str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in self.root.rglob("*") if path.is_file()}

        before = snapshot()
        for _ in range(2):
            diary_service.load_diary_entry(self.target)
            health_service.load_health_log(self.target)
            exercise_service.load_exercise_log(self.target)
            note_service.load_all_notes(self.root)
            plan_service.load_daily_plan(self.target, root=self.root)
            flashcard_service.load_flashcards(root=self.root)
            reminder_service.load_reminders(root=self.root)
            master_service.load_master_data(root=self.root)
            settings_service.load_user_settings()
            substance_service.load_substance_profiles()
        self.assertEqual(snapshot(), before)


if __name__ == "__main__":
    unittest.main()
