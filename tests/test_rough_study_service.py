from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from app_core.json_store import JsonConflictError, JsonReadError
from app_core.rough_study_service import (
    delete_rough_study_batch,
    get_existing_study_days,
    load_rough_study_batches,
    load_rough_study_records,
    rough_study_path,
    save_rough_study_batch,
    validate_rough_study_rows,
)


class RoughStudyServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    @staticmethod
    def row(day="2026-11-16", duration_sec=3600, **fields):
        return {"study_date": day, "duration_sec": duration_sec, **fields}

    def save_week(self, rows=None, **params):
        return save_rough_study_batch(
            self.root, kind="week",
            rows=rows if rows is not None else [self.row()],
            operation_id=params.pop("operation_id", uuid4().hex), **params,
        )

    def test_read_empty_store_does_not_create_a_file(self):
        self.assertEqual(load_rough_study_batches(self.root), [])
        self.assertEqual(load_rough_study_records(self.root), [])
        self.assertFalse(rough_study_path(self.root).parent.exists())

    def test_week_multiple_records_on_one_day_and_unknown_time(self):
        self.save_week([self.row(memo="本文"), self.row(duration_sec=None, subject_id="english")])
        records = load_rough_study_records(self.root)
        self.assertEqual([row["duration_sec"] for row in records], [3600, None])
        self.assertEqual(records[1]["subject_id"], "english")
        self.assertTrue(all("start_at" not in row and "end_at" not in row for row in records))
        self.assertFalse((self.root / "study_logs").exists())

    def test_period_metadata_retains_estimates_and_manual_overrides(self):
        period = {"start_date": "2026-11-16", "end_date": "2026-11-23",
                  "average_minutes": 90, "weekdays_only": True}
        batch = save_rough_study_batch(
            self.root, kind="period", operation_id=uuid4().hex, period=period,
            rows=[self.row(duration_sec=5400), self.row("2026-11-20", 2700, estimated=False)],
        )
        self.assertEqual(batch["period"], period)
        self.assertEqual([row["estimated"] for row in batch["rows"]], [True, False])
        self.assertEqual(sum(row["duration_sec"] for row in batch["rows"]), 8100)

    def test_replay_stale_revision_and_delete_preserve_entry_identity(self):
        op = uuid4().hex
        first = self.save_week(operation_id=op)
        replay = self.save_week(operation_id=op)
        batch_id = first["batch_id"]
        self.assertEqual(replay["batch_id"], batch_id)
        self.assertTrue(replay["replayed"])
        with self.assertRaises(JsonConflictError):
            self.save_week([self.row(duration_sec=7200)], operation_id=op)

        row = {**first["rows"][0], "memo": "訂正"}
        edit = self.save_week([row], batch_id=batch_id, expected_revision=1)
        self.assertEqual(edit["rows"][0]["record_id"], row["record_id"])
        self.assertEqual(edit["revision"], 2)
        with self.assertRaises(JsonConflictError):
            self.save_week([row], batch_id=batch_id, expected_revision=1)

        drop = uuid4().hex
        delete_rough_study_batch(self.root, batch_id=batch_id, expected_revision=2, operation_id=drop)
        delete_rough_study_batch(self.root, batch_id=batch_id, expected_revision=2, operation_id=drop)
        self.assertTrue(self.save_week(operation_id=op)["replayed"])
        self.assertEqual(load_rough_study_batches(self.root), [])

    def test_invalid_input_and_corrupt_storage_leave_data_unchanged(self):
        with self.assertRaises(ValueError):
            self.save_week([self.row(duration_sec=None)])
        with self.assertRaises(ValueError):
            validate_rough_study_rows([self.row(duration_sec=True)], kind="week")
        with self.assertRaises(ValueError):
            validate_rough_study_rows([self.row(), self.row("2026-11-23")], kind="week")
        path = rough_study_path(self.root)
        self.assertFalse(path.exists())
        path.parent.mkdir()
        path.write_text('{"schema_version": 99}', encoding="utf-8")
        with self.assertRaises(JsonReadError):
            self.save_week()
        self.assertEqual(path.read_text(encoding="utf-8"), '{"schema_version": 99}')

    def test_valid_json_with_malformed_record_rejects_reads_and_saves(self):
        first = self.save_week()
        path = rough_study_path(self.root)
        before = path.read_bytes()
        for key, value in (("study_date", "2026-02-30"), ("duration_sec", -5), ("estimated", "yes"), ("period", [])):
            with self.subTest(field=key):
                document = json.loads(before)
                batch = document["batches"][first["batch_id"]]
                target = batch if key == "period" else batch["rows"][0]
                target[key] = value
                malformed = json.dumps(document).encode()
                path.write_bytes(malformed)
                with self.assertRaises(JsonReadError):
                    load_rough_study_records(self.root)
                with self.assertRaises(JsonReadError):
                    self.save_week()
                self.assertEqual(path.read_bytes(), malformed)

    def test_write_failure_can_retry_and_concurrent_saves_do_not_lose_records(self):
        self.save_week()
        path = rough_study_path(self.root)
        before = path.read_bytes()
        failed_id = uuid4().hex
        with patch("app_core.rough_study_service.save_json_safe", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.save_week([self.row("2026-11-17")], operation_id=failed_id)
        self.assertEqual(path.read_bytes(), before)

        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(self.save_week, [self.row("2026-11-17")], operation_id=failed_id)
            second = pool.submit(self.save_week, [self.row("2026-11-18")])
            repeat = pool.submit(self.save_week, [self.row("2026-11-17")], operation_id=failed_id)
            self.assertEqual(first.result()["batch_id"], repeat.result()["batch_id"])
            second.result()
        self.assertEqual(len(load_rough_study_records(self.root)), 3)

    def test_existing_days_include_legacy_and_unknown_duration_without_rewriting(self):
        day = date(2026, 11, 16)
        second_day = date(2026, 11, 17)
        folder = self.root / "study_logs" / "2026" / "11"
        folder.mkdir(parents=True)
        path = folder / "20261116.json"
        legacy = json.dumps([{"duration_sec": 30, "memo": "test"}]).encode()
        path.write_bytes(legacy)
        (folder / "imported.json").write_text(
            json.dumps([{"start_at": "2026-11-17T18:00:00", "duration_sec": 120}]), encoding="utf-8",
        )
        (folder / "20261116_backup_20261119_120000.json").write_bytes(legacy)
        batch = self.save_week([self.row(duration_sec=None, memo="時間不明")])
        actual = get_existing_study_days(self.root, day, second_day)
        self.assertEqual(actual[day], {"timed_seconds": 30, "timed_count": 1,
                                       "rough_seconds": 0, "rough_count": 1})
        self.assertEqual(actual[second_day], {"timed_seconds": 120, "timed_count": 1,
                                              "rough_seconds": 0, "rough_count": 0})
        ignored = get_existing_study_days(self.root, day, day, exclude_batch_id=batch["batch_id"])
        self.assertEqual(ignored[day]["rough_count"], 0)
        self.assertEqual(path.read_bytes(), legacy)


if __name__ == "__main__":
    unittest.main()
