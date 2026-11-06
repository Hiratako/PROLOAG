from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
from smoke_timer_partial_updates import run_isolated


class TimerPartialUpdateTests(unittest.TestCase):
    """Real page callbacks and fragments; all records live in temporary copies."""

    @classmethod
    def setUpClass(cls):
        cls.result = run_isolated(PROJECT)["after"]

    def test_selection_does_not_rerun_page_or_scan_history(self):
        self.assertTrue(self.result["checks"]["selection_partial"])
        for label in ("environment", "category", "project", "subject", "material", "unit"):
            measurement = self.result["measurements"][label]
            self.assertEqual(measurement["full_runs"], 0)
            self.assertEqual(measurement["groups"].get("study_logs", 0), 0)

    def test_parent_change_clears_incompatible_descendants(self):
        self.assertTrue(self.result["checks"]["parent_clears_children"])

    def test_pause_resume_and_adjustments_keep_all_unsaved_fields(self):
        self.assertTrue(self.result["checks"]["draft_controls"])

    def test_external_master_change_is_visible_on_next_selection(self):
        self.assertTrue(self.result["checks"]["external_master_fresh"])

    def test_settings_update_pending_price_without_losing_drafts(self):
        self.assertTrue(self.result["checks"]["settings_refresh_pending"])

    def test_failed_save_keeps_inputs_and_original_documents(self):
        self.assertTrue(self.result["checks"]["failure_keeps_draft"])

    def test_save_uses_latest_selected_values_and_updates_summary(self):
        self.assertTrue(self.result["checks"]["latest_values_saved"])
        self.assertTrue(self.result["checks"]["own_save_refreshes_totals"])

    def test_tick_observes_new_daily_record_without_history_scan(self):
        self.assertTrue(self.result["checks"]["tick_fresh"])

    def test_selected_task_keeps_its_owning_day_after_midnight(self):
        self.assertTrue(self.result["checks"]["task_owner_day"])


if __name__ == "__main__":
    unittest.main()
