from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from app_core import exercise_service as exercise, health_service as health
from app_core import learning_context_service as context, learning_context_ui as context_ui
import master_ui
import prolog_ui.components as ab
import test_health_read_efficiency as health_tests


class SharedDayDisplayTests(unittest.TestCase):
    start, end = health_tests.HealthReadEfficiencyTests.start, health_tests.HealthReadEfficiencyTests.end
    setUp = health_tests.HealthReadEfficiencyTests.setUp
    tearDown = health_tests.HealthReadEfficiencyTests.tearDown
    day_path = health_tests.HealthReadEfficiencyTests.day_path

    def test_health_editor_and_summary_share_selected_day_without_changing_rows(self):
        expected = health.build_health_summary(self.start, self.end, study_summaries={})
        self.reads.clear()
        selected = health.load_health_log(self.start)
        snapshot = deepcopy(selected)
        actual = health.build_health_summary(
            self.start, self.end, study_summaries={},
            health_logs_by_date={self.start.isoformat(): selected},
        )
        self.assertEqual(actual, expected)
        self.assertEqual(selected, snapshot)
        self.assertEqual(self.reads[self.day_path("health", 1)], 1)
        self.assertEqual(self.reads[self.day_path("health", 3)], 1)
        self.reads.clear()
        health.build_health_summary(self.start, self.end, study_summaries={}, health_logs_by_date={})
        self.assertEqual(self.reads[self.day_path("health", 1)], 1)

    def test_exercise_editor_and_range_share_raw_log_preserving_unknown_fields(self):
        expected = exercise.load_exercise_logs_range(self.start, self.end)
        self.reads.clear()
        selected = exercise.load_exercise_log(self.start)
        snapshot = deepcopy(selected)
        actual = exercise.load_exercise_logs_range(
            self.end, self.start, logs_by_date={self.start.isoformat(): selected},
        )
        self.assertEqual(actual, expected)
        self.assertEqual(selected, snapshot)
        self.assertEqual(self.reads[self.day_path("exercise", 1)], 1)
        self.assertEqual(self.reads[self.day_path("exercise", 3)], 1)
        self.reads.clear()
        exercise.load_exercise_logs_range(self.start, self.end, logs_by_date={})
        self.assertEqual(self.reads[self.day_path("exercise", 1)], 1)


class SharedRenderSettingsTests(unittest.TestCase):
    def test_supplied_settings_seed_theme_without_another_disk_read(self):
        supplied = {"display": {"theme_mode": "dark"}}
        before = deepcopy(supplied)
        with patch.object(ab.st, "session_state", {}), patch.object(ab, "load_user_settings", side_effect=AssertionError("extra read")):
            self.assertEqual(ab.current_theme_mode(settings=supplied), "dark")
            self.assertEqual(ab.theme_palette(settings=supplied), ab.get_theme_palette("dark"))
        self.assertEqual(supplied, before)

    def test_existing_theme_choice_survives_shared_settings_and_empty_defaults(self):
        with patch.object(ab.st, "session_state", {ab.THEME_MODE_STATE_KEY: "dark"}), patch.object(ab, "load_user_settings", side_effect=AssertionError("extra read")):
            self.assertEqual(ab.current_theme_mode(settings={}), "dark")
        with patch.object(ab.st, "session_state", {}), patch.object(ab, "load_user_settings", side_effect=AssertionError("extra read")):
            self.assertEqual(ab.current_theme_mode(settings={}), "standard")

    def test_standalone_theme_still_reads_saved_settings_once(self):
        with patch.object(ab.st, "session_state", {}), patch.object(ab, "load_user_settings", return_value={"display": {"theme_mode": "dark"}}) as loader:
            self.assertEqual(ab.current_theme_mode(), "dark")
            ab.theme_palette()
            loader.assert_called_once_with()


class SharedRawMasterTests(unittest.TestCase):
    def test_strict_material_display_accepts_raw_shared_data_without_reread(self):
        raw = {"projects": [], "subjects": [], "materials": [{"material_id": "m", "name": "架空", "is_active": False, "custom": {"keep": True}}]}
        before = deepcopy(raw)
        with patch.object(context.json_store, "load_json_safe", side_effect=AssertionError("extra master read")):
            rows = context.list_materials(Path("unused"), master_data=raw)
        self.assertEqual(rows[0]["material_id"], "m")
        self.assertFalse(rows[0]["is_active"])
        self.assertEqual(raw, before)

    def test_shared_raw_data_keeps_strict_validation_and_fresh_standalone_load(self):
        for raw in ({"materials": {}}, {"materials": [None]}, {"materials": [{"name": "IDなし"}]}):
            with self.subTest(raw=raw), patch.object(context.json_store, "load_json_safe", side_effect=AssertionError("extra read")):
                with self.assertRaises(context.LearningContextError):
                    context.list_materials(Path("unused"), master_data=raw)
        with patch.object(context.json_store, "load_json_safe", return_value={}) as loader:
            self.assertEqual(context.list_materials(Path("unused")), [])
            self.assertEqual(loader.call_count, 1)


class DeferredQuickAddTests(unittest.TestCase):
    def test_deferred_success_saves_fresh_master_and_leaves_rerun_to_caller(self):
        latest = {"projects": [{"project_id": "external", "name": "他の操作"}]}
        with patch.object(master_ui, "load_master_data", return_value=latest), patch.object(master_ui, "save_master_data") as save, patch.object(master_ui.ab, "queue_success") as notify, patch.object(master_ui.st, "rerun") as rerun:
            actual = master_ui._add_master_item(Path("unused"), collection="projects", id_key="project_id", prefix="project", name="追加", description="", defer_rerun=True)
        self.assertTrue(actual)
        self.assertEqual(save.call_args.args[0]["projects"][0]["project_id"], "external")
        self.assertEqual(len(save.call_args.args[0]["projects"]), 2)
        notify.assert_called_once()
        rerun.assert_not_called()

    def test_failure_does_not_request_refresh_and_default_success_still_reruns(self):
        with patch.object(master_ui, "load_master_data", return_value={}), patch.object(master_ui, "save_master_data", side_effect=OSError("fixture")), patch.object(master_ui.ab, "show_action_error"), patch.object(master_ui.st, "rerun") as rerun:
            self.assertFalse(master_ui._add_master_item(Path("unused"), collection="projects", id_key="project_id", prefix="project", name="追加", description="", defer_rerun=True))
            rerun.assert_not_called()
        with patch.object(master_ui, "load_master_data", return_value={}), patch.object(master_ui, "save_master_data"), patch.object(master_ui.ab, "queue_success"), patch.object(master_ui.st, "rerun") as rerun:
            self.assertTrue(master_ui._add_master_item(Path("unused"), collection="projects", id_key="project_id", prefix="project", name="追加", description=""))
            rerun.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
