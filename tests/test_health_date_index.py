from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from app_core import health_service as health


class HealthDateIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = date(2026, 9, 1)
        self.notes = [
            {"created_at": "2026-09-01T12:34:56.123456", "updated_at": "2026-09-03"},
            {"created_at": "", "updated_at": "2026-09-02 11:00:00"},
            {"updated_at": "2026-09-03"},
            {"created_at": "invalid", "updated_at": "2026-09-01"},
            {"created_at": None, "updated_at": "2026-09-01"},
            {"created_at": 0, "updated_at": "2026-09-01"},
            {"created_at": 1, "updated_at": "2026-09-01"},
            {"created_at": datetime(2026, 9, 2, 0, 30, tzinfo=timezone(timedelta(hours=14)))},
            {"created_at": "2026-09-01T23:30:00-12:00"},
            {"created_at": date(2026, 9, 2)},
            {"created_at": "2000-01-01"},
            {},
        ]
        self.cards = [
            {"created_at": "2026-09-03T03:04:05"},
            {"updated_at": "2026-09-01"},
            {"created_at": "2026-09-02"},
            {"created_at": "2026-09-02"},
            {"created_at": "not-a-date", "updated_at": "2026-09-03"},
        ]

    def join(self, notes, cards, *, days=3):
        summary = [
            {"date": (self.start + timedelta(days=index)).isoformat(), "has_health_log": False, "unknown": {"keep": index}}
            for index in range(days)
        ]
        study = {self.start.isoformat(): {"study_duration_sec": 0, "study_hours": 0.0, "study_session_count": 1}}
        original = deepcopy((notes, cards, summary, study))

        def read(path, default=None):
            self.assertEqual(path, health.FLASHCARDS_PATH, "No personal data may be read")
            return cards

        with patch.object(health, "_load_notes_flat", return_value=notes), \
             patch.object(health, "load_json_safe", side_effect=read) as reads, \
             patch.object(health, "_task_summary", return_value={"task_count": 0}) as tasks, \
             patch.object(health, "save_json_safe", side_effect=AssertionError("Read-only analysis")):
            rows = health.build_health_study_joined_summary(
                self.start, self.start + timedelta(days=days - 1),
                health_summary=summary, study_summaries=study,
            )
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(tasks.call_count, days)
        self.assertEqual((notes, cards, summary, study), original)
        self.assertEqual([row["unknown"] for row in rows], [row["unknown"] for row in summary])
        return rows

    def test_index_matches_legacy_count_for_mixed_dates_and_missing_values(self):
        original = deepcopy(self.notes)
        counts = health._created_counts_by_date(self.notes)
        for offset in range(-2, 7):
            target = self.start + timedelta(days=offset)
            self.assertEqual(counts.get(target, 0), health._created_count_by_date(self.notes, target))
        self.assertEqual(counts[date(2000, 1, 1)], 1)
        self.assertNotIn(None, counts)
        self.assertEqual(self.notes, original)

    def test_invalid_truthy_created_at_does_not_fall_back_to_updated_at(self):
        items = [{"created_at": value, "updated_at": "2026-09-01"} for value in ("bad", 12, True, {"bad": True})]
        self.assertEqual(health._created_counts_by_date(items), {})

    def test_offset_dates_follow_existing_local_timezone_conversion(self):
        values = ["2026-09-01T23:30:00-12:00", "2026-09-02T00:30:00+14:00"]
        for value in values:
            expected = datetime.fromisoformat(value).astimezone().date()
            self.assertEqual(health._created_counts_by_date([{"created_at": value}]), {expected: 1})

    def test_joined_counts_and_inputs_match_per_day_reference(self):
        cards = self.cards + [None, "ignored"]
        rows = self.join(self.notes, cards)
        for row in rows:
            target = date.fromisoformat(row["date"])
            self.assertEqual(row["note_count"], health._created_count_by_date(self.notes, target))
            self.assertEqual(row["card_count"], health._created_count_by_date(self.cards, target))
        self.assertEqual(rows[0]["study_session_count"], 1)

    def test_each_item_is_parsed_once_independent_of_number_of_days(self):
        for days in (1, 180):
            with self.subTest(days=days), patch.object(health, "_parse_item_date", wraps=health._parse_item_date) as parse:
                self.join(self.notes, self.cards + [None], days=days)
                self.assertEqual(parse.call_count, len(self.notes) + len(self.cards))

    def test_empty_and_invalid_card_containers_keep_zero_counts(self):
        for cards in ([], {}, None):
            with self.subTest(cards=cards), patch.object(health, "_parse_item_date", wraps=health._parse_item_date) as parse:
                rows = self.join([], cards)
                self.assertTrue(all(row["note_count"] == row["card_count"] == 0 for row in rows))
                self.assertEqual(parse.call_count, 0)

    def test_next_call_rebuilds_index_and_observes_changed_inputs(self):
        notes, cards = deepcopy(self.notes), deepcopy(self.cards)
        first = self.join(notes, cards)
        notes.append({"created_at": "2026-09-01"})
        cards.append({"created_at": "2026-09-03"})
        second = self.join(notes, cards)
        self.assertEqual(second[0]["note_count"], first[0]["note_count"] + 1)
        self.assertEqual(second[2]["card_count"], first[2]["card_count"] + 1)


if __name__ == "__main__":
    unittest.main()
