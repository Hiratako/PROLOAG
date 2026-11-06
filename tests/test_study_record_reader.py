from __future__ import annotations

import csv
from copy import deepcopy
from datetime import date, datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import data_utils
from app_core import analytics_service as analytics
from app_core import exercise_service, health_service, json_store
from app_core.analysis_dates import parse_analysis_date
from app_core.export_service import build_export_bundle
from app_core.master_snapshot import MasterSnapshot
from app_core.study_record_reader import unpack_study_document
from test_learning_read_efficiency import page_aggregations


class StudyRecordReaderPureTests(unittest.TestCase):
    def test_formats_keep_original_indices_ids_and_detached_unknown_fields(self):
        records = [{"session_id": "original", "duration_sec": 60, "unknown": {"a": [1]}}, None,
                   {"duration_sec": 120, "linked_note_ids": "legacy scalar"}]
        for source in (records, {"sessions": records, "outer": {"preserve": [2]}}):
            with self.subTest(source=source):
                before = deepcopy(source)
                document = unpack_study_document(source)
                self.assertEqual([index for index, _ in document.records], [0, 2])
                self.assertEqual(document.records[0][1]["session_id"], "original")
                self.assertNotIn("session_id", document.records[1][1])
                self.assertEqual(document.records[1][1]["linked_note_ids"], "legacy scalar")
                self.assertEqual(document.ignored_items, [{"index": 1, "value": None}])
                document.records[0][1]["unknown"]["a"].append(99)
                self.assertEqual(source, before)

    def test_single_record_is_supported_but_metadata_is_not_a_fake_session(self):
        for source in ({"start_at": "2026-09-05"}, {"session_id": "old"}, {"duration_sec": 60}):
            document = unpack_study_document(source)
            self.assertEqual(document.container_kind, "record")
            self.assertEqual(document.records, [(0, source)])
        for source in ({}, {"schema_version": 1, "unknown": "metadata"}):
            document = unpack_study_document(source)
            self.assertEqual(document.records, [])
            self.assertEqual(document.metadata, source)

    def test_empty_or_malformed_envelope_never_becomes_a_record(self):
        for sessions in ([], {}, None, "unexpected"):
            source = {"sessions": sessions, "session_id": "outer-is-not-a-session", "unknown": [1, None]}
            document = unpack_study_document(source)
            self.assertEqual(document.records, [])
            self.assertEqual(document.metadata, {"session_id": "outer-is-not-a-session", "unknown": [1, None]})
            self.assertEqual(document.container_kind, "sessions")
            if sessions != []:
                self.assertEqual(document.ignored_items, [{"field": "sessions", "value": sessions}])

    def test_duplicate_ids_are_not_silently_deduplicated_or_reassigned(self):
        records = [{"session_id": "same", "duration_sec": 30}, {"session_id": "same", "duration_sec": 60}]
        self.assertEqual([record for _, record in unpack_study_document(records).records], records)


class StudyReadConsistencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag_study_read_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        original_read = json_store._read

        def isolated_read(path):
            self.assertTrue(Path(path).resolve().is_relative_to(self.root), f"Out-of-fixture JSON read: {path}")
            return original_read(path)

        patches = [patch.object(json_store, "_read", side_effect=isolated_read),
                   patch.object(json_store, "PROJECT_ROOT", self.root),
                   patch.object(analytics, "app_today", return_value=date(2026, 9, 16))]
        for attribute, relative in {
            "STUDY_LOGS_DIR": "study_logs", "DAILY_PLANS_DIR": "daily_plans", "NOTES_DATA_DIR": "notes_data",
            "FLASHCARDS_PATH": "flashcards.json", "REMINDERS_PATH": "reminders.json",
        }.items():
            patches.append(patch.object(analytics, attribute, self.root / relative))
        for module in (health_service, exercise_service):
            patches.append(patch.object(module, "STUDY_LOGS_DIR", self.root / "study_logs"))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.start, self.end = date(2026, 9, 5), date(2026, 9, 6)
        self.master = MasterSnapshot({"subjects": [{"subject_id": "s", "name": "科目"}]})
        self.list_path = "study_logs/2026/09/20260905.json"
        self.envelope_path = "study_logs/2026/09/20260904.json"
        self.single_path = "study_logs/2026/09/20260907.json"
        self.outer_metadata = {"schema_version": 0, "custom.outer": {"empty": {}, "nil": None, "array": [1, "二"]},
                               "session_id": "outer-container-not-record"}
        self.put(self.list_path, [self.session("s1", "2026-09-05T08:00:00", 3600),
                                  self.session(None, "2026-09-06 09:00:00", 1800)])
        self.put(self.envelope_path, {**self.outer_metadata, "sessions": [
            self.session("s2", "2026-09-05T20:00:00", 7200), "do not count me",
            self.session("s3", "2026-09-06T21:00:00", 3600),
        ]})
        self.put(self.single_path, self.session("s4", "2026-09-06T12:00:00", 900))
        self.put("study_logs/2026/09/20260908.json", {"sessions": [], "empty_metadata": {"keep": True}})
        self.put("study_logs/2026/09/20260909.json", {"schema_version": 7, "unknown": "not a session"})
        self.put("study_logs/2026/09/20260910.json", {"sessions": {"malformed": True}, "outer": "keep"})
        self.put("study_logs/2026/09/20260901.json", [self.session("outside", "2026-09-01T12:00:00", 36000)])
        self.put("study_logs/2026/09/20260911.json", [self.session("invalid-date", "2026-09-05 invalid", 180)])
        self.put("study_logs/backups/ignored.json", [self.session("backup", "2026-09-05T20:00:00", 99999)])

    @staticmethod
    def session(identifier, start_at, duration):
        record = {"start_at": start_at, "duration_sec": duration, "money": 100, "subject_id": "s", "project_id": "p",
                  "condition": {"category": "legacy", "io_type": "input"}, "unknown": {"keep": [None, "metadata"]}}
        if identifier is not None:
            record["session_id"] = identifier
        return record

    def put(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("\ufeff" + json.dumps(data, ensure_ascii=False, indent=1) + "\r\n").encode("utf-8"))

    def bytes_and_times(self):
        return {path.relative_to(self.root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob("*") if path.is_file()}

    @staticmethod
    def csv_rows(bundle, key):
        table = next(table for table in bundle.tables if table.key == key)
        rows = list(csv.DictReader(io.StringIO(table.data.decode("utf-8-sig"))))
        assert len(rows) == table.row_count
        return rows

    def test_daily_subject_category_hour_and_csv_share_exact_session_set(self):
        before = self.bytes_and_times()
        snapshot = analytics.LearningDataSnapshot.load()
        daily = analytics.build_daily_learning_summary(self.start, self.end, snapshot=snapshot)
        subjects = analytics.build_subject_growth_summary(self.start, self.end, snapshot=snapshot, master=self.master)
        selected = [row for row in snapshot.sessions if (day := parse_analysis_date(row.get("start_at"))) and self.start <= day <= self.end]
        hours = analytics.aggregate_by_hour(selected)
        page = page_aggregations(self.root)
        categories = page["build_category_io_summary"](self.start, self.end, {}, sessions=snapshot.activity_sessions)
        csv_records = self.csv_rows(build_export_bundle(self.root), "study_sessions")
        csv_selected = [row for row in csv_records if (day := parse_analysis_date(row.get("start_at"))) and self.start <= day <= self.end]
        self.assertEqual(sum(row["total_duration_sec"] for row in daily), 17100)
        self.assertEqual(sum(row["session_count"] for row in daily), 5)
        self.assertEqual(sum(row["duration_sec"] for row in subjects), 17100)
        self.assertEqual(sum(row["session_count"] for row in subjects), 5)
        self.assertEqual(sum(row["duration_sec"] for row in hours.values()), 17100)
        self.assertEqual(sum(row["sessions"] for row in hours.values()), 5)
        self.assertEqual(sum(row["total_hours"] for row in categories), 4.75)
        self.assertEqual(sum(row["session_count"] for row in categories), 5)
        self.assertEqual(page["most_studied_hour"](self.start, self.end, {}, sessions=snapshot.activity_sessions), "20:00台")
        self.assertEqual(sum(int(row["duration_sec"]) for row in csv_selected), 17100)
        self.assertEqual(len(csv_selected), 5)
        self.assertEqual(len(csv_records), 7)
        self.assertEqual(self.bytes_and_times(), before)

    def test_raw_all_loader_does_not_supply_ids_or_change_source_values(self):
        before = self.bytes_and_times()
        rows = data_utils.load_all_study_sessions(self.root)
        self.assertEqual(len(rows), 7)
        missing_id = next(row for row in rows if row["start_at"] == "2026-09-06 09:00:00")
        self.assertNotIn("session_id", missing_id)
        self.assertEqual(missing_id["unknown"], {"keep": [None, "metadata"]})
        self.assertEqual(self.bytes_and_times(), before)
        # The explicit per-file compatibility normalizer retains its existing
        # in-memory ID contract, but does not migrate the file on a read.
        normalized = data_utils.load_study_sessions_file(self.root / self.list_path)
        self.assertTrue(all(row.get("session_id") for row in normalized))
        self.assertEqual(self.bytes_and_times(), before)

    def test_csv_envelope_metadata_empty_containers_and_positions_are_lossless(self):
        bundle = build_export_bundle(self.root)
        sessions = self.csv_rows(bundle, "study_sessions")
        containers = {row["__source_file"]: row for row in self.csv_rows(bundle, "study_session_containers")}
        outer = containers[self.envelope_path]
        self.assertEqual(json.loads(outer["metadata_json"]), self.outer_metadata)
        self.assertEqual(json.loads(outer["ignored_items_json"]), [{"index": 1, "value": "do not count me"}])
        associated = [row for row in sessions if row["__source_file"] == self.envelope_path]
        self.assertEqual([row["__record_index"] for row in associated], ["0", "2"])
        self.assertEqual([row["session_id"] for row in associated], ["s2", "s3"])
        empty = containers["study_logs/2026/09/20260908.json"]
        self.assertEqual(empty["session_count"], "0")
        self.assertEqual(json.loads(empty["metadata_json"]), {"empty_metadata": {"keep": True}})
        malformed = containers["study_logs/2026/09/20260910.json"]
        self.assertEqual(json.loads(malformed["ignored_items_json"]), [{"field": "sessions", "value": {"malformed": True}}])
        self.assertFalse(any(row.get("session_id") == "outer-container-not-record" for row in sessions))
        self.assertNotIn("study_logs/backups/ignored.json", containers)

    def test_csv_source_relation_survives_reserved_unknown_field_collision(self):
        relative = "study_logs/2026/09/20260912.json"
        self.put(relative, [{"session_id": "collision", "duration_sec": 60, "__source_file": "legacy source", "__record_index": 99}])
        rows = self.csv_rows(build_export_bundle(self.root), "study_sessions")
        row = next(row for row in rows if row["session_id"] == "collision")
        self.assertEqual(row["__source_file"], relative)
        self.assertEqual(row["__record_index"], "0")
        self.assertEqual(json.loads(row["__original_export_fields_json"]), {"__source_file": "legacy source", "__record_index": 99})

    def test_health_and_exercise_read_same_legacy_container_once(self):
        before = self.bytes_and_times()
        for target, expected_seconds, expected_count in ((date(2026, 9, 4), 10800, 2), (date(2026, 9, 7), 900, 1), (date(2026, 9, 8), 0, 0)):
            with self.subTest(target=target):
                with patch.object(health_service, "load_json_safe", wraps=health_service.load_json_safe) as reader:
                    result = health_service._study_summary(target)
                    self.assertEqual(reader.call_count, 1)
                self.assertEqual(result["study_duration_sec"], expected_seconds)
                self.assertEqual(result["study_session_count"], expected_count)
                self.assertEqual(health_service._study_duration_sec(target), expected_seconds)
                with patch.object(exercise_service, "load_json_safe", wraps=exercise_service.load_json_safe) as reader:
                    self.assertEqual(exercise_service._study_duration_sec(target), expected_seconds)
                    self.assertEqual(reader.call_count, 1)
        self.assertEqual(self.bytes_and_times(), before)

    def test_local_date_and_hour_boundaries_do_not_change_raw_timestamps(self):
        # Expected local values come from datetime itself, not the helper under
        # test, so this works independently of the machine's local timezone.
        aware = datetime(2026, 9, 5, 23, 30, tzinfo=timezone.utc)
        expected = aware.astimezone().replace(tzinfo=None)
        record = {"start_at": aware.isoformat(), "duration_sec": 60, "money": 1}
        raw = deepcopy(record)
        self.assertEqual(analytics.aggregate_daily_study_logs([record]), {expected.date().isoformat(): {"duration_sec": 60, "money": 1, "sessions": 1}})
        self.assertEqual(analytics.aggregate_by_hour([record]), {f"{expected.hour:02d}": {"duration_sec": 60, "money": 1, "sessions": 1}})
        self.assertEqual(record, raw)

    def test_zero_and_fractional_seconds_are_counted_without_truncation(self):
        day = date(2026, 9, 15)
        relative = "study_logs/2026/09/20260915.json"
        records = [self.session("zero", "2026-09-15T12:00:00", 0),
                   self.session("fraction-a", "2026-09-15T12:00:01", 0.25),
                   self.session("fraction-b", "2026-09-15T12:00:02", "60.5")]
        self.put(relative, {"sessions": records, "outer": {"unchanged": True}})
        before = self.bytes_and_times()
        snapshot = analytics.LearningDataSnapshot.load()
        daily = analytics.build_daily_learning_summary(day, day, snapshot=snapshot)
        subject = analytics.build_subject_growth_summary(day, day, snapshot=snapshot, master=self.master)
        page = page_aggregations(self.root)
        category = page["build_category_io_summary"](day, day, {}, sessions=snapshot.activity_sessions)
        hourly = analytics.aggregate_by_hour(records)
        csv_rows = [row for row in self.csv_rows(build_export_bundle(self.root), "study_sessions") if row["__source_file"] == relative]
        self.assertEqual((daily[0]["total_duration_sec"], daily[0]["session_count"]), (60.75, 3))
        self.assertEqual((subject[0]["duration_sec"], subject[0]["session_count"]), (60.75, 3))
        self.assertEqual((category[0]["total_duration_sec"], category[0]["session_count"]), (60.75, 3))
        self.assertEqual((hourly["12"]["duration_sec"], hourly["12"]["sessions"]), (60.75, 3))
        self.assertEqual(sum(float(row["duration_sec"]) for row in csv_rows), 60.75)
        self.assertEqual(len(csv_rows), 3)
        self.assertEqual(analytics.cumulative_curve_from_daily(daily)[0]["cumulative_duration_sec"], 60.75)
        self.assertEqual(health_service._study_summary(day)["study_duration_sec"], 60.75)
        self.assertEqual(health_service._study_summary(day)["study_session_count"], 3)
        self.assertEqual(exercise_service._study_duration_sec(day), 60.75)
        self.assertEqual(self.bytes_and_times(), before)


if __name__ == "__main__":
    unittest.main()
