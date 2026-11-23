from datetime import date
import unittest

from app_core.rough_study_input import (
    merge_period_table_rows,
    parse_study_minutes,
    study_edit_diff,
    summarize_study_selection,
)


class RoughStudyInputTests(unittest.TestCase):
    def test_duration_accepts_number_clock_and_japanese_forms(self):
        examples = {
            90: 90,
            "90": 90,
            "90分": 90,
            "1:30": 90,
            "1時間30分": 90,
            " １時間 ３０分　": 90,
            "１：０５": 65,
            "2時間": 120,
            "24:00": 1440,
            "1": 1,
        }
        for value, expected in examples.items():
            with self.subTest(value=value):
                self.assertEqual(parse_study_minutes(value), expected)

    def test_duration_preserves_blank_and_flags_invalid_or_ambiguous_values(self):
        for value in (None, "", "　 "):
            self.assertIsNone(parse_study_minutes(value))
        for value in (0, "0:00", "-3", "1:60", "24:01", "1時間90分", "1.5", True, 3.5, "x", "9" * 5000):
            with self.subTest(value=repr(value)[:30]), self.assertRaises(ValueError):
                parse_study_minutes(value)

    def test_diff_identifies_edits_deletions_and_additions_by_persisted_identity(self):
        first = {"record_id": "first", "study_date": "2026-11-16", "duration_sec": 60, "memo": "same"}
        second = {"record_id": "second", "study_date": "2026-11-17", "duration_sec": None}
        third = {"record_id": "third", "study_date": "2026-11-18", "duration_sec": 3600}
        changed = {**first, "duration_sec": 90}
        new = {"study_date": "2026-11-16", "duration_sec": 60}
        self.assertEqual(
            study_edit_diff([first, second, third], [third, changed, new]),
            {"added": [new], "changed": [{"before": first, "after": changed}], "deleted": [second]},
        )

    def test_diff_ignores_display_metadata_and_default_equivalent_fields(self):
        before = {"record_id": "a", "study_date": "2026-11-16", "duration_sec": None, "memo": "読んだ"}
        after = {**before, "display": "読書", "subject_id": "", "estimated": False}
        self.assertEqual(study_edit_diff([before], [after]), {"added": [], "changed": [], "deleted": []})

    def test_summary_counts_each_registered_day_once_and_preserves_unknown_time(self):
        records = [
            {"study_date": "2026-11-16", "duration_sec": 900},
            {"study_date": "2026-11-16", "duration_sec": None},
            {"study_date": "2026-11-17", "duration_sec": 3600},
        ]
        first, second, third = (date(2026, 11, day) for day in (16, 17, 18))
        existing = {first: {"timed_seconds": 9000, "rough_seconds": 1800}, third: {"rough_seconds": 600}}
        summary = summarize_study_selection(records, existing)
        self.assertEqual(
            summary,
            {"selected_days": 2, "record_count": 3, "unknown_count": 1,
             "entered_seconds": 4500, "existing_seconds": 10800, "result_seconds": 15300},
        )
        scope_summary = summarize_study_selection(records, existing, scope_days=[first, second, third, third])
        self.assertEqual(scope_summary["existing_seconds"], 11400)
        self.assertEqual(scope_summary["result_seconds"], 15900)

    def test_new_period_preserves_only_overlapping_manual_changes(self):
        a, b, c = "2026-11-16", "2026-11-17", "2026-11-18"
        def row(day):
            return {"study_date": day, "input_id": "old" + day, "duration": "60分",
                    "subject_id": "English", "memo": "復習", "include": True}

        defaults = [row(a), row(b)]
        adjusted = [
            {**row(a), "duration": "1:30", "include": False, "memo": ""},
            row(b),
        ]
        generated = [
            {**row(a), "input_id": "fresh-a", "duration": "90分", "memo": "新しい内容", "subject_id": "Math"},
            {**row(c), "input_id": "fresh-c"},
        ]
        result = merge_period_table_rows(generated, adjusted, defaults)
        self.assertEqual(result[0], {
            **generated[0], "input_id": "old" + a, "duration": "1:30", "include": False, "memo": "",
        })
        self.assertEqual(result[1], generated[1])
        self.assertEqual(generated[0]["input_id"], "fresh-a")

    def test_new_average_updates_unchanged_overlap_days(self):
        before = {"study_date": "2026-11-16", "input_id": "old", "duration": "60分", "include": True}
        fresh = {**before, "input_id": "new", "duration": "90分", "include": False}
        self.assertEqual(merge_period_table_rows([fresh], [before], [before]), [{**fresh, "input_id": "old"}])


if __name__ == "__main__":
    unittest.main()
