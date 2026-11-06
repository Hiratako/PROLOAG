from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app_core import diary_service as diary, json_store, substance_residual_service as residual
from app_core.substance_service import default_substance_profiles


class DiaryDisplayReadTests(unittest.TestCase):
    target = date(2026, 10, 7)

    def setUp(self):
        self.profiles = {item["substance_id"]: item for item in default_substance_profiles()}
        self.record = {"date": self.target.isoformat(), "title": "synthetic", "caffeine_mg": 50,
                       "substance_records_schema_version": 1,
                       "substance_records": [{"substance_id": "caffeine", "taken_at": "2026-10-07T09:00:00", "amount_mg": 50}]}

    def test_supplied_profiles_match_fresh_normalization_without_mutating_inputs(self):
        self.profiles["caffeine"]["half_life_hours"] = 8
        original_profiles, original_record = deepcopy(self.profiles), deepcopy(self.record)
        with patch.object(diary, "_now_text", return_value="2026-10-07 12:00:00"), \
                patch.object(diary, "substance_profile_map", return_value=self.profiles) as reader:
            expected = diary.normalize_diary_entry(self.record, self.target)
            actual = diary.normalize_diary_entry(self.record, self.target, profiles=self.profiles)
        self.assertEqual(actual, expected)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(self.profiles, original_profiles)
        self.assertEqual(self.record, original_record)

    def test_explicit_empty_lookup_uses_builtin_caffeine_without_disk_fallback(self):
        supplied = {}
        with patch.object(diary, "substance_profile_map", side_effect=AssertionError("Unexpected profile reload")):
            normalized = diary.normalize_diary_entry(self.record, self.target, profiles=supplied)
        self.assertEqual(supplied, {})
        self.assertEqual(normalized["caffeine_mg"], 50)
        self.assertEqual(normalized["substance_records"][0]["model_snapshot"]["half_life_hours"], 5)

    def test_omitted_lookup_observes_new_profile_on_each_call(self):
        changed = deepcopy(self.profiles)
        changed["caffeine"]["half_life_hours"] = 9
        with patch.object(diary, "substance_profile_map", side_effect=[self.profiles, changed]) as reader:
            first = diary.normalize_diary_entry(self.record, self.target)
            second = diary.normalize_diary_entry(self.record, self.target)
        self.assertEqual(reader.call_count, 2)
        self.assertEqual(first["substance_records"][0]["model_snapshot"]["half_life_hours"], 5)
        self.assertEqual(second["substance_records"][0]["model_snapshot"]["half_life_hours"], 9)

    def test_diary_loader_still_reads_record_fresh_with_a_supplied_lookup(self):
        changed = {**self.record, "title": "external change"}
        with patch.object(diary, "load_json_safe", side_effect=[self.record, changed]) as reader, \
                patch.object(diary, "substance_profile_map", side_effect=AssertionError("Unexpected profile reload")):
            first = diary.load_diary_entry(self.target, profiles=self.profiles)
            second = diary.load_diary_entry(self.target, profiles=self.profiles)
        self.assertEqual(reader.call_count, 2)
        self.assertEqual(first["title"], "synthetic")
        self.assertEqual(second["title"], "external change")

    def test_legacy_caffeine_keeps_its_recorded_model_with_display_profiles(self):
        legacy = {"date": self.target.isoformat(), "caffeine_records": [
            {"caffeine_id": "old", "taken_at": "2026-10-07T09:00:00", "amount_mg": 50}]}
        self.profiles["caffeine"]["half_life_hours"] = 9
        normalized = diary.normalize_diary_entry(legacy, self.target, profiles=self.profiles)
        self.assertEqual(normalized["substance_records"][0]["model_snapshot"]["half_life_hours"], 5)

    def test_residual_lookback_shares_one_profile_read_across_every_diary(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = {}
            for offset in range(3):
                target = self.target - timedelta(days=offset)
                path = root / f"{target:%Y%m%d}.json"
                path.write_text("{}", encoding="utf-8")
                paths[target] = path
            raw = {**self.record, "caffeine_untimed_mg": 5}
            with patch.object(residual, "get_diary_entry_path", side_effect=lambda target: paths[target]), \
                    patch.object(residual, "substance_profile_map", return_value=self.profiles) as profiles_read, \
                    patch.object(diary, "load_json_safe", return_value=raw) as records_read, \
                    patch.object(diary, "substance_profile_map", side_effect=AssertionError("Per-day profile reload")):
                records, untimed = residual.load_recent_substance_records(datetime(2026, 10, 7, 12), lookback_days=2)
            self.assertEqual(profiles_read.call_count, 1)
            self.assertEqual(records_read.call_count, 3)
            self.assertEqual(len(records), 3)
            self.assertEqual(untimed, {"caffeine": 15})

    def test_residual_explicit_empty_lookup_does_not_reload_profiles(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "day.json"
            path.write_text("{}", encoding="utf-8")
            with patch.object(residual, "get_diary_entry_path", return_value=path), \
                    patch.object(residual, "substance_profile_map", side_effect=AssertionError("Unexpected reload")), \
                    patch.object(diary, "substance_profile_map", side_effect=AssertionError("Unexpected reload")), \
                    patch.object(diary, "load_json_safe", return_value=self.record):
                records, _ = residual.load_recent_substance_records(datetime(2026, 10, 7, 12), lookback_days=0, profiles={})
        self.assertEqual(len(records), 1)

    def test_save_uses_fresh_profile_and_preserves_stale_revision_conflict(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(diary, "DIARY_ENTRIES_DIR", root / "diary_entries"), \
                    patch.object(json_store, "PROJECT_ROOT", root):
                current_profiles = deepcopy(self.profiles)
                with patch.object(diary, "substance_profile_map", side_effect=lambda **_kwargs: current_profiles):
                    diary.save_diary_entry(self.target, {"title": "initial"})
                    path = diary.get_diary_entry_path(self.target)
                    diary.load_diary_entry(self.target, profiles=self.profiles)
                    revision = json_store.observed_json_revision(path)
                    current_profiles["caffeine"]["half_life_hours"] = 11
                    diary.save_diary_entry(self.target, self.record, expected_revision=revision)
                    saved = diary.load_diary_entry(self.target, profiles=self.profiles)
                    self.assertEqual(saved["substance_records"][0]["model_snapshot"]["half_life_hours"], 11)
                    before = path.read_bytes()
                    with self.assertRaises(json_store.JsonConflictError):
                        diary.save_diary_entry(self.target, {"title": "stale"}, expected_revision=revision)
                    self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
