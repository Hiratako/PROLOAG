from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from app_core import integrity_rules, integrity_service, json_store


class IntegrityQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patches = ExitStack()
        self.addCleanup(patches.close)
        locations = {
            "PROJECT_ROOT": self.root,
            "STUDY_LOGS_DIR": self.root / "study_logs",
            "DAILY_PLANS_DIR": self.root / "daily_plans",
            "DIARY_ENTRIES_DIR": self.root / "diary_entries",
            "HEALTH_LOGS_DIR": self.root / "health_logs",
            "EXERCISE_LOGS_DIR": self.root / "exercise_logs",
            "STATISTICS_PRACTICE_DIR": self.root / "statistics_practice",
            "NOTES_DATA_DIR": self.root / "notes_data",
            "MASTER_DATA_DIR": self.root / "master_data",
            "SETTINGS_DIR": self.root / "settings",
            "FLASHCARDS_PATH": self.root / "flashcards.json",
            "REMINDERS_PATH": self.root / "reminders.json",
            "USER_SETTINGS_PATH": self.root / "settings" / "user_settings.json",
            "SUBSTANCE_PROFILES_PATH": self.root / "settings" / "substance_profiles.json",
        }
        for module in (integrity_rules, integrity_service):
            for name, value in locations.items():
                if hasattr(module, name):
                    patches.enter_context(patch.object(module, name, value))
        datasets = (
            ("sessions", "勉強ログ", locations["STUDY_LOGS_DIR"]),
            ("plans", "日次計画", locations["DAILY_PLANS_DIR"]),
            ("diaries", "日記", locations["DIARY_ENTRIES_DIR"]),
            ("health", "健康ログ", locations["HEALTH_LOGS_DIR"]),
            ("exercise", "運動ログ", locations["EXERCISE_LOGS_DIR"]),
            ("statistics_practice", "統計演習履歴", locations["STATISTICS_PRACTICE_DIR"]),
        )
        patches.enter_context(patch.object(integrity_rules, "DATED_DATASETS", datasets))

    def write_bytes(self, relative_path: str, payload: bytes) -> Path:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def write_json(self, relative_path: str, value: Any) -> Path:
        return self.write_bytes(relative_path, json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def file_snapshot(self) -> dict[str, bytes]:
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    @staticmethod
    def session(start: str, end: str, **extra: Any) -> dict[str, Any]:
        return {
            "session_id": "session_20260828_001",
            "start_at": start,
            "end_at": end,
            "duration_sec": 3600,
            "hourly_rate": 1000,
            "money": 1000,
            "normal_money": 1000,
            "overtime_money": 0,
            "linked_note_ids": [],
            **extra,
        }

    @staticmethod
    def note_store(created: str, updated: str, **extra: Any) -> dict[str, Any]:
        return {
            "notebooks": [
                {
                    "id": "notebook_20260828_001",
                    "sections": [
                        {
                            "id": "section_20260828_001",
                            "pages": [
                                {
                                    "note_id": "note_20260828_001",
                                    "id": "note_20260828_001",
                                    "title": "synthetic note",
                                    "body": "",
                                    "content": "",
                                    "tags": [],
                                    "linked_study_session_ids": [],
                                    "linked_flashcard_ids": [],
                                    "linked_reminder_ids": [],
                                    "created_at": created,
                                    "updated_at": updated,
                                    **extra,
                                }
                            ],
                        }
                    ],
                }
            ]
        }

    @staticmethod
    def statistics_attempt(attempt_id: str = "stat_attempt_001", **extra: Any) -> dict[str, Any]:
        period = {"start_date": "2026-08-01", "end_date": "2026-08-30"}
        problem_id = "stat_problem_001"
        template_id = "descriptive_mean"
        topic = "descriptive"
        grade = {
            "score": 100.0,
            "earned_points": 1.0,
            "max_points": 1.0,
            "correct_count": 1,
            "total_parts": 1,
            "all_correct": True,
            "parts": [],
        }
        return {
            "attempt_id": attempt_id,
            "attempted_at": "2026-08-30T10:30:00",
            "date": "2026-08-30",
            "problem_id": problem_id,
            "template_id": template_id,
            "topic": topic,
            "title": "平均値を求める",
            "period": period,
            "answers": {"mean": 2.5},
            "grade": grade,
            "score": 100.0,
            "max_score": 100.0,
            "correct_count": 1,
            "total_parts": 1,
            "problem_snapshot": {
                "problem_id": problem_id,
                "template_id": template_id,
                "topic": topic,
                "title": "平均値を求める",
                "period": period,
            },
            **extra,
        }

    def assert_corrupt_context_is_reported(self, payload: bytes, error_name: str) -> None:
        corrupted_paths = [
            self.write_bytes(relative, payload)
            for relative in (
                "notes_data/notebooks.json",
                "master_data/master.json",
                "flashcards.json",
                "reminders.json",
                "study_logs/2026/08/20260828.json",
                "daily_plans/2026/08/20260828.json",
            )
        ]
        self.write_json(
            "study_logs/2026/08/20260829.json",
            [self.session("2026-08-29T09:00:00", "2026-08-29T10:00:00", duration_sec=-1)],
        )
        before = self.file_snapshot()
        result = integrity_service.run_integrity_checks()
        syntax_issues = [item for item in result["issues"] if item["問題カテゴリ"] == "JSON構文"]
        self.assertEqual(
            {item["対象ファイル"] for item in syntax_issues},
            {str(path.relative_to(self.root)) for path in corrupted_paths},
        )
        self.assertTrue(all(error_name in item["内容"] for item in syntax_issues))
        self.assertTrue(any("duration_sec" in item["内容"] for item in result["issues"]))
        self.assertEqual(result["checked_file_count"], 7)
        self.assertGreater(result["total_checks"], len(corrupted_paths))
        self.assertEqual(self.file_snapshot(), before)
        with self.assertRaises(json_store.JsonReadError):
            json_store.load_json_safe(corrupted_paths[0], {})
        self.assertEqual(self.file_snapshot(), before)

    def test_corrupt_json_reports_every_context_file_and_finishes(self) -> None:
        self.assert_corrupt_context_is_reported(b'{"unfinished":', "JSONDecodeError")

    def test_invalid_utf8_reports_every_context_file_and_finishes(self) -> None:
        self.assert_corrupt_context_is_reported(b"\xff\xfe\xff", "UnicodeDecodeError")

    def test_duplicate_keys_and_nonfinite_values_remain_visible(self) -> None:
        path = self.write_bytes(
            "flashcards.json",
            b'[{"card_id":"first","card_id":"second","question":"Q","answer":"A","weight":NaN}]',
        )
        before = self.file_snapshot()
        result = integrity_service.run_integrity_checks()
        self.assertTrue(any("キー 'card_id' が重複" in item["内容"] for item in result["issues"]))
        self.assertTrue(any("NaN または Infinity" in item["内容"] for item in result["issues"]))
        with self.assertRaises(json_store.JsonReadError):
            json_store.load_json_safe(path, [])
        self.assertEqual(self.file_snapshot(), before)

    def test_unreadable_target_does_not_hide_unrelated_reference_errors(self) -> None:
        self.write_bytes("notes_data/notebooks.json", b"{")
        self.write_json(
            "study_logs/2026/08/20260828.json",
            [self.session(
                "2026-08-28T09:00:00",
                "2026-08-28T10:00:00",
                linked_note_ids=["unknown_note"],
                linked_task_id="unknown_task",
                project_id="unknown_project",
            )],
        )
        before = self.file_snapshot()
        result = integrity_service.run_integrity_checks()
        messages = [item["内容"] for item in result["issues"]]
        self.assertTrue(any("JSONを読み込めません" in message for message in messages))
        self.assertTrue(any("unknown_task" in message for message in messages))
        self.assertTrue(any("unknown_project" in message for message in messages))
        self.assertFalse(any("unknown_note" in message for message in messages))
        self.assertEqual(self.file_snapshot(), before)

    def test_reciprocal_link_checks_are_preserved(self) -> None:
        self.write_json(
            "notes_data/notebooks.json",
            self.note_store("2026-08-28T09:00:00", "2026-08-28T10:00:00"),
        )
        self.write_json(
            "study_logs/2026/08/20260828.json",
            [self.session("2026-08-28T09:00:00", "2026-08-28T10:00:00", linked_note_ids=["note_20260828_001"])],
        )
        before = self.file_snapshot()
        result = integrity_service.run_integrity_checks()
        self.assertTrue(any(
            item["問題カテゴリ"] == "双方向リンク" and "ノート側にセッションIDがありません" in item["内容"]
            for item in result["issues"]
        ))
        self.assertEqual(self.file_snapshot(), before)

    def test_valid_statistics_practice_history_passes_without_writes(self) -> None:
        self.write_json(
            "statistics_practice/2026/08/20260830.json",
            {
                "schema_version": 1,
                "date": "2026-08-30",
                "attempts": [self.statistics_attempt()],
            },
        )
        before = self.file_snapshot()
        result = integrity_service.run_integrity_checks()
        statistics_errors = [
            item
            for item in result["issues"]
            if item["重要度"] == integrity_rules.ERROR and "統計演習" in item["問題カテゴリ"]
        ]
        self.assertEqual(statistics_errors, [])
        self.assertEqual(result["checked_file_count"], 1)
        self.assertEqual(self.file_snapshot(), before)

    def test_malformed_statistics_practice_history_reports_structure_and_consistency(self) -> None:
        duplicate_id = "stat_attempt_duplicate"
        malformed = self.statistics_attempt(
            duplicate_id,
            attempted_at="not-a-datetime",
            problem_id="",
            title=123,
            period={"start_date": "2026-08-31", "end_date": "2026-08-01"},
            answers=[],
            grade=[],
            problem_snapshot=[],
            score=-1,
            max_score=0,
            correct_count=2,
            total_parts=1,
        )
        mismatch = self.statistics_attempt(duplicate_id)
        mismatch["problem_snapshot"] = {
            **mismatch["problem_snapshot"],
            "problem_id": "another_problem",
            "period": {"start_date": "2026-07-01", "end_date": "2026-07-31"},
        }
        self.write_json(
            "statistics_practice/2026/08/20260830.json",
            {
                "schema_version": 2,
                "date": "2026-08-29",
                "attempts": [malformed, mismatch],
            },
        )
        self.write_json(
            "statistics_practice/2026/08/20260831.json",
            {"schema_version": 1, "date": "2026-08-31", "attempts": {}},
        )
        before = self.file_snapshot()
        result = integrity_service.run_integrity_checks()
        messages = [item["内容"] for item in result["issues"]]
        self.assertTrue(any("schema_version が 1" in message for message in messages))
        self.assertTrue(any("attempts がリスト形式" in message for message in messages))
        self.assertTrue(any("attempt_id が別の演習履歴と重複" in message for message in messages))
        self.assertTrue(any("attempted_at" in message for message in messages))
        self.assertTrue(any("problem_id が欠損" in message for message in messages))
        self.assertTrue(any("period.start_date が period.end_date より後" in message for message in messages))
        self.assertTrue(any("answers が辞書形式" in message for message in messages))
        self.assertTrue(any("score が数値でないか" in message for message in messages))
        self.assertTrue(any("max_score が 0 以下" in message for message in messages))
        self.assertTrue(any("correct_count が total_parts を超え" in message for message in messages))
        self.assertTrue(any("problem_snapshot.problem_id" in message for message in messages))
        self.assertTrue(any("problem_snapshot.period" in message for message in messages))
        self.assertEqual(result["checked_file_count"], 2)
        self.assertEqual(self.file_snapshot(), before)

    def test_aware_datetime_values_convert_to_local_naive_time(self) -> None:
        aware = datetime(2026, 8, 27, 18, 30, 45, tzinfo=timezone.utc)
        expected = aware.astimezone().replace(tzinfo=None)
        for value in (aware, aware.isoformat(), aware.isoformat().replace("+00:00", "Z")):
            with self.subTest(value=value):
                parsed = integrity_rules._parse_datetime(value)
                self.assertEqual(parsed, expected)
                self.assertIsNone(parsed.tzinfo)
        self.assertEqual(integrity_rules._parse_datetime(expected), expected)
        self.assertIsNone(integrity_rules._parse_datetime("not a datetime"))

    def test_mixed_datetime_inputs_complete_without_false_order_errors(self) -> None:
        aware_start = datetime(2026, 8, 28, 0, 0, 0, tzinfo=timezone.utc)
        local_start = aware_start.astimezone().replace(tzinfo=None)
        local_end = local_start + timedelta(hours=1)
        self.write_json(
            f"study_logs/{local_start:%Y}/{local_start:%m}/{local_start:%Y%m%d}.json",
            [self.session(aware_start.isoformat(), local_end.isoformat())],
        )
        self.write_json("notes_data/notebooks.json", self.note_store(aware_start.isoformat(), local_end.isoformat()))
        before = self.file_snapshot()
        result = integrity_service.run_integrity_checks()
        self.assertFalse(any(item["重要度"] == integrity_rules.ERROR for item in result["issues"]))
        self.assertEqual(self.file_snapshot(), before)

    def test_mixed_datetime_inputs_still_report_reversed_order(self) -> None:
        aware_start = datetime(2026, 8, 28, 0, 0, 0, tzinfo=timezone.utc)
        local_start = aware_start.astimezone().replace(tzinfo=None)
        earlier = local_start - timedelta(hours=1)
        self.write_json(
            f"study_logs/{local_start:%Y}/{local_start:%m}/{local_start:%Y%m%d}.json",
            [self.session(aware_start.isoformat(), earlier.isoformat())],
        )
        self.write_json("notes_data/notebooks.json", self.note_store(aware_start.isoformat(), earlier.isoformat()))
        before = self.file_snapshot()
        result = integrity_service.run_integrity_checks()
        messages = [item["内容"] for item in result["issues"]]
        self.assertTrue(any("end_at が start_at より前" in message for message in messages))
        self.assertTrue(any("updated_at が created_at より前" in message for message in messages))
        self.assertEqual(self.file_snapshot(), before)


if __name__ == "__main__":
    unittest.main()
