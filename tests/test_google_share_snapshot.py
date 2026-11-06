from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app_core import google_share_snapshot as share
from app_core import json_store
from app_core.json_operations import atomic_json_operation


class GoogleShareSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag_google_snapshot_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.day = date(2026, 9, 16)
        self.now = datetime(2026, 9, 16, 17, 30, tzinfo=timezone.utc)
        original_read = json_store._read

        def isolated_read(path):
            self.assertTrue(Path(path).resolve().is_relative_to(self.root), f"Read outside fixture: {path}")
            return original_read(path)

        patcher = patch.object(json_store, "_read", side_effect=isolated_read)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.read = original_read
        self.put("master_data/master.json", {
            "projects": [{"project_id": "p", "name": "医学部編入", "description": "SECRET_MASTER_DESCRIPTION"}],
            "subjects": [{"subject_id": "s", "name": "生命科学"},
                         {"subject_id": "unused", "name": "SECRET_UNUSED_SUBJECT"}],
            "materials": [{"material_id": "m", "name": "指定教材"}],
            "units": [{"unit_id": "u", "name": "細胞"}],
            "tags": [{"name": "SECRET_MASTER_TAG"}],
        })

    def put(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("\ufeff" + json.dumps(value, ensure_ascii=False, indent=1) + "\r\n").encode("utf-8"))
        return path

    def log(self, owner, value):
        return self.put(f"study_logs/{owner:%Y}/{owner:%m}/{owner:%Y%m%d}.json", value)

    def session(self, identifier="existing", **changes):
        record = {
            "session_id": identifier, "start_at": "2026-09-16T09:00:00", "end_at": "2026-09-16T10:00:00",
            "duration_sec": 3600, "project_id": "p", "subject_id": "s", "material_id": "m", "unit_id": "u",
            "memo": "SECRET_LEARNING_MEMO", "money": 999999, "hourly_rate": 123456,
            "money_policy": {"description": "SECRET_MONEY_POLICY"}, "tag_ids": ["SECRET_TAG"],
            "condition": {"category": "医学", "io_type": "input", "sleep_hours": 7, "mood": "SECRET_MOOD",
                          "medication": "SECRET_MEDICATION", "environment": "SECRET_ENVIRONMENT"},
            "unknown": "SECRET_UNKNOWN", "linked_note_ids": ["SECRET_NOTE_ID"],
        }
        record.update(changes)
        return record

    def build(self, start=None, end=None, **options):
        return share.build_share_snapshot(self.root, start or self.day, end or self.day,
                                          generated_at=options.pop("generated_at", self.now), **options)

    def state(self):
        return {path.relative_to(self.root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob("*.json")}

    @staticmethod
    def overview(snapshot):
        return dict(snapshot["tables"]["overview"][1:])

    @staticmethod
    def details(snapshot):
        table = snapshot["tables"]["study_sessions"]
        return [dict(zip(table[0], row)) for row in table[1:]]

    def test_allowlist_and_byte_exact_readonly_without_sensitive_file_reads(self):
        self.log(self.day, [self.session()])
        for relative in ("health_logs/2026/09/20260916.json", "settings/user_settings.json",
                         "diary_entries/2026/09/20260916.json", "notes_data/notebooks.json",
                         "flashcards.json", "reminders.json"):
            self.put(relative, {"sentinel": "SECRET_OTHER_DATA"})
        before = self.state()
        with patch.object(json_store, "save_json_safe", side_effect=AssertionError("Source writes forbidden")):
            snapshot = self.build()
        self.assertEqual(self.state(), before)
        encoded = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
        self.assertNotIn("SECRET", encoded)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn("999999", encoded)
        self.assertNotIn("123456", encoded)
        self.assertNotIn("source_file", encoded)
        self.assertEqual(snapshot["schema_version"], 1)
        self.assertEqual(snapshot["counts"], {"study_sessions": 1, "study_days": 1})
        self.assertEqual(self.details(snapshot)[0]["セッションID"], "existing")
        self.assertEqual(self.details(snapshot)[0]["科目"], "生命科学")
        paths = [Path(call.args[0]).relative_to(self.root).as_posix() for call in json_store._read.call_args_list]
        self.assertEqual(set(paths), {"study_logs/2026/09/20260916.json", "master_data/master.json", "settings/study_categories.json"})
        self.assertEqual(set(snapshot["tables"]), {"overview", "daily_summary", "subjects", "materials", "categories", "study_sessions"})

    def test_content_is_explicit_and_formula_strings_are_not_rewritten(self):
        formula = '=HYPERLINK("https://example.invalid","学習内容")'
        self.log(self.day, [self.session(memo=formula)])
        note = "+相談: 学習の優先順位"
        snapshot = self.build(include_content=True, consultation_note=note)
        self.assertEqual(self.details(snapshot)[0]["勉強内容"], formula)
        self.assertEqual(self.overview(snapshot)["相談事項"], note)
        self.assertNotIn("SECRET", json.dumps(snapshot, ensure_ascii=False))
        summary_only = self.build(include_details=False, include_content=True)
        self.assertNotIn("study_sessions", summary_only["tables"])
        self.assertNotIn(formula, json.dumps(summary_only, ensure_ascii=False))

    def test_list_envelope_single_record_and_fractional_zero_totals_match(self):
        self.log(self.day, [self.session("list", duration_sec=0)])
        self.log(self.day - timedelta(days=1), {"sessions": [self.session("envelope", duration_sec=0.25)],
                                              "outer": "SECRET_ENVELOPE"})
        self.log(self.day + timedelta(days=1), self.session("single", duration_sec=60.5))
        snapshot = self.build()
        self.assertEqual(snapshot["counts"], {"study_sessions": 3, "study_days": 1})
        for name, column in (("daily_summary", 1), ("subjects", 2), ("materials", 2), ("categories", 2), ("study_sessions", 4)):
            self.assertEqual(sum(row[column] for row in snapshot["tables"][name][1:]), 60.75)
        self.assertEqual(self.overview(snapshot)["総学習時間（秒）"], 60.75)
        self.assertNotIn("SECRET", json.dumps(snapshot))

    def test_equal_length_previous_period_and_date_boundaries(self):
        end = self.day + timedelta(days=1)
        previous_start = self.day - timedelta(days=2)
        for offset, duration in ((-3, 10000), (-2, 10), (-1, 20), (0, 30), (1, 40), (2, 20000)):
            target = self.day + timedelta(days=offset)
            self.log(target, [self.session(str(offset), start_at=f"{target}T12:00:00", end_at="", duration_sec=duration)])
        snapshot = self.build(end=end)
        overview = self.overview(snapshot)
        self.assertEqual(overview["前期間開始"], previous_start.isoformat())
        self.assertEqual((overview["総学習時間（秒）"], overview["前期間総学習時間（秒）"], overview["前期間との差（秒）"]), (70, 30, 40))
        self.assertEqual(snapshot["counts"], {"study_sessions": 2, "study_days": 2})

    def test_offset_crosses_owner_day_and_end_only_legacy_is_counted(self):
        local = datetime(2026, 9, 16, 0, 30).astimezone()
        opposite_offset = timezone(timedelta(hours=-12 if local.utcoffset() >= timedelta() else 14))
        stored = local.astimezone(opposite_offset)
        self.assertNotEqual(stored.date(), self.day)
        self.log(stored.date(), [self.session("offset", start_at=stored.isoformat(), end_at="", duration_sec=15.5)])
        self.log(self.day, [self.session("endonly", start_at="", end_at="2026-09-16 15:30:00.123", duration_sec=4.5)])
        snapshot = self.build()
        self.assertEqual(snapshot["counts"], {"study_sessions": 2, "study_days": 1})
        self.assertEqual(self.overview(snapshot)["総学習時間（秒）"], 20)
        rows = {row["セッションID"]: row for row in self.details(snapshot)}
        self.assertEqual(rows["offset"]["開始日時"], "2026-09-16T00:30:00")
        self.assertEqual(rows["endonly"]["開始日時"], "")

    def test_empty_snapshot_and_unrecorded_day_are_valid_not_mastery_claims(self):
        self.log(self.day, {"sessions": [], "outer": "SECRET_OUTER"})
        snapshot = self.build(end=self.day + timedelta(days=1))
        self.assertEqual(snapshot["counts"], {"study_sessions": 0, "study_days": 0})
        self.assertEqual(snapshot["tables"]["daily_summary"][1:], [["2026-09-17", 0, 0, False], ["2026-09-16", 0, 0, False]])
        self.assertEqual(len(snapshot["tables"]["subjects"]), 1)
        self.assertIn("理解度・正答率", self.overview(snapshot)["注意"])
        self.assertNotIn("SECRET", json.dumps(snapshot))

    def test_no_records_or_master_is_a_valid_empty_snapshot(self):
        (self.root / "master_data/master.json").unlink()
        before = self.state()
        snapshot = self.build()
        self.assertEqual(snapshot["counts"], {"study_sessions": 0, "study_days": 0})
        self.assertEqual(self.state(), before)

    def test_daily_and_session_tables_are_newest_first_with_precise_hour_summary(self):
        end = self.day + timedelta(days=1)
        self.log(self.day, [self.session("early", duration_sec=0.25, start_at="2026-09-16T08:00:00", end_at=""),
                            self.session("later", duration_sec=0.5, start_at="2026-09-16T20:00:00", end_at="")])
        self.log(end, [self.session("next-day", duration_sec=3600, start_at="2026-09-17T07:00:00", end_at="")])
        snapshot = self.build(end=end)
        self.assertEqual([row[0] for row in snapshot["tables"]["daily_summary"][1:]], ["2026-09-17", "2026-09-16"])
        self.assertEqual([row["セッションID"] for row in self.details(snapshot)], ["next-day", "later", "early"])
        self.assertEqual(self.overview(snapshot)["総学習時間（秒）"], 3600.75)
        self.assertEqual(self.overview(snapshot)["総学習時間（時間）"], 3600.75 / 3600)

    def test_content_hash_ignores_generation_time_and_private_changes(self):
        record = self.session()
        self.log(self.day, [record])
        first = self.build()
        second = self.build(generated_at=self.now + timedelta(days=1))
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertNotEqual(first["generated_at"], second["generated_at"])
        record.update(money=1, memo="CHANGED_PRIVATE_MEMO", unknown="CHANGED_PRIVATE_UNKNOWN")
        self.log(self.day, [record])
        self.assertEqual(first["snapshot_id"], self.build()["snapshot_id"])
        self.assertNotEqual(first["snapshot_id"], self.build(include_content=True)["snapshot_id"])
        self.assertNotEqual(first["snapshot_id"], self.build(consultation_note="明示した相談文")["snapshot_id"])
        payload = {key: value for key, value in first.items() if key not in {"snapshot_id", "generated_at"}}
        expected = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        self.assertEqual(first["snapshot_id"], expected)

    def test_legacy_ids_and_snapshot_survive_reorder_container_and_file_move(self):
        records = [self.session("", duration_sec=10), self.session(None, duration_sec=10), self.session("", duration_sec=20)]
        old_path = self.log(self.day, records)
        before = self.state()
        first = self.build()
        self.assertEqual(self.state(), before)
        ids = [row["セッションID"] for row in self.details(first)]
        self.assertEqual(len(set(ids)), 3)
        self.assertTrue(all(identifier.startswith("legacy_") for identifier in ids))
        old_path.unlink()
        self.log(self.day + timedelta(days=1), {"sessions": list(reversed(records)), "outer": "SECRET_NEW_LOCATION"})
        self.assertEqual(first, self.build())

    def test_unknown_freeform_categories_are_not_shared_but_known_aliases_are(self):
        self.log(self.day, [self.session(condition=None, category="SECRET_CATEGORY", io_type="SECRET_IO"),
                            self.session("alias", condition={}, category="英語", io_type="output")])
        snapshot = self.build()
        self.assertNotIn("SECRET", json.dumps(snapshot))
        rows = {row["セッションID"]: row for row in self.details(snapshot)}
        self.assertEqual((rows["existing"]["学習カテゴリ"], rows["existing"]["入出力区分"]), ("その他", "未設定"))
        self.assertEqual((rows["alias"]["学習カテゴリ"], rows["alias"]["入出力区分"]), ("英語", "アウトプット"))

    def test_corrupt_and_invalid_records_stop_without_source_changes(self):
        invalids = [None, {}, {"sessions": {}}, [None], [{}],
                    [self.session(duration_sec=-1)], [self.session(duration_sec=True)],
                    [self.session(duration_sec="60")], [self.session(duration_sec=10 ** 400)],
                    [self.session(start_at="invalid")], [self.session(start_at="", end_at="")],
                    [self.session(end_at="2026-09-15T10:00:00")], [self.session(subject_id=["s"])],
                    [self.session(condition=[])], [self.session(), self.session()]]
        for raw in invalids:
            with self.subTest(raw=repr(raw)[:80]):
                self.log(self.day, raw)
                before = self.state()
                with self.assertRaises(share.ShareSnapshotError):
                    self.build()
                self.assertEqual(self.state(), before)
        path = self.log(self.day, [])
        for raw in (b'{"sessions": [], "sessions": []}', b'[{"duration_sec": NaN}]', b'{broken'):
            path.write_bytes(raw)
            before = self.state()
            with self.assertRaises(json_store.JsonReadError):
                self.build()
            self.assertEqual(self.state(), before)

    def test_invalid_master_and_duplicate_referenced_ids_stop(self):
        self.log(self.day, [self.session()])
        for master in (None, [], {"subjects": {}}, {"subjects": [None]},
                       {"subjects": [{"subject_id": "s", "name": "A"}, {"subject_id": "s", "name": "B"}]}):
            with self.subTest(master=master):
                self.put("master_data/master.json", master)
                before = self.state()
                with self.assertRaises(share.ShareSnapshotError):
                    self.build()
                self.assertEqual(self.state(), before)

    def test_external_revision_change_is_rejected(self):
        path = self.log(self.day, [self.session()])
        replacement = [self.session(duration_sec=99)]

        def change_after_read(target):
            value = self.read(target)
            if Path(target) == path:
                self.log(self.day, replacement)
            return value

        with patch.object(json_store, "_read", side_effect=change_after_read):
            with self.assertRaises(json_store.JsonConflictError):
                self.build()
        self.assertEqual(self.read(path)[0], replacement)

    def test_external_inventory_addition_is_rejected(self):
        path = self.log(self.day, [self.session()])

        def add_after_read(target):
            value = self.read(target)
            if Path(target) == path:
                self.log(self.day + timedelta(days=1), [])
            return value

        with patch.object(json_store, "_read", side_effect=add_after_read):
            with self.assertRaises(json_store.JsonConflictError):
                self.build()

    def test_read_failure_pending_save_and_active_stage_are_not_sendable(self):
        self.log(self.day, [self.session()])
        with patch.object(json_store, "_read", side_effect=json_store.JsonReadError("synthetic read failure")):
            with self.assertRaises(json_store.JsonReadError):
                self.build()
        operation_id = "a" * 32
        path = self.put(f"backups/json_operations/{operation_id}/transaction.json", {
            "schema_version": 1, "operation_id": operation_id, "status": "prepared", "kind": "test"})
        before = self.state()
        with self.assertRaises(json_store.JsonStoreError):
            self.build()
        self.assertEqual(self.state(), before)
        path.unlink()
        with atomic_json_operation(root=self.root, kind="test", operation_id="b" * 32):
            with self.assertRaises(share.ShareSnapshotError):
                self.build()

    def test_lock_is_held_for_capture_but_not_aggregation(self):
        self.log(self.day, [self.session()])
        real_inventory, real_session = share._inventory, share._session
        events = []

        def inventory(*args):
            events.append("inventory")
            held = getattr(json_store._held, "paths", set())
            self.assertIn(json_store._key(self.root / "backups/json_operations/operations"), held)
            return real_inventory(*args)

        def session(*args, **kwargs):
            events.append("aggregate")
            held = getattr(json_store._held, "paths", set())
            self.assertNotIn(json_store._key(self.root / "backups/json_operations/operations"), held)
            return real_session(*args, **kwargs)

        with patch.object(share, "_inventory", side_effect=inventory), patch.object(share, "_session", side_effect=session):
            self.build()
        self.assertEqual(events, ["inventory", "inventory", "aggregate"])

    def test_normal_writer_waits_until_committed_snapshot_capture_finishes(self):
        path = self.log(self.day, [self.session()])
        source_read = threading.Event()
        allow_capture = threading.Event()
        writer_attempted = threading.Event()
        writer_done = threading.Event()
        captured = []
        errors = []
        replacement = [self.session(duration_sec=10)]

        def slow_read(target):
            value = self.read(target)
            if Path(target) == path and threading.current_thread().name == "share-capture":
                source_read.set()
                if not allow_capture.wait(5):
                    raise AssertionError("Capture release timed out")
            return value

        def capture():
            try:
                captured.append(self.build())
            except BaseException as exc:
                errors.append(exc)

        def writer():
            writer_attempted.set()
            try:
                with json_store.json_transaction(path):
                    json_store.save_json_safe(path, replacement)
            except BaseException as exc:
                errors.append(exc)
            finally:
                writer_done.set()

        with patch.object(json_store, "_read", side_effect=slow_read), patch.object(json_store, "PROJECT_ROOT", self.root):
            capturing = threading.Thread(target=capture, name="share-capture")
            writing = threading.Thread(target=writer, name="share-writer")
            capturing.start()
            try:
                self.assertTrue(source_read.wait(5))
                writing.start()
                self.assertTrue(writer_attempted.wait(5))
                self.assertFalse(writer_done.wait(0.05))
            finally:
                allow_capture.set()
                capturing.join(5)
                if writing.ident is not None:
                    writing.join(5)
            self.assertFalse(capturing.is_alive())
            self.assertFalse(writing.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.overview(captured[0])["総学習時間（秒）"], 3600)
        self.assertEqual(self.read(path)[0], replacement)

    def test_undated_live_file_and_remapped_source_are_rejected(self):
        self.put("study_logs/unassigned.json", [])
        before = self.state()
        with self.assertRaises(share.ShareSnapshotError):
            self.build()
        self.assertEqual(self.state(), before)
        for resolved in (self.root.parent / "outside.json", self.root / "health_logs/private.json"):
            with self.subTest(resolved=resolved), patch.object(json_store, "resolved_json_path", return_value=resolved):
                with self.assertRaises(share.ShareSnapshotError):
                    share._inside(self.root, self.root / "master_data/master.json")

    def test_outside_period_and_backup_payloads_are_never_read(self):
        self.log(self.day, [self.session()])
        self.log(self.day - timedelta(days=30), {"intentionally": "unsupported outside range"})
        self.put("study_logs/backups/20260916.json", {"bad": "backup"})
        self.put("study_logs/2026/09/20260916_backup_1.json", {"bad": "backup"})
        self.build()
        self.assertEqual({Path(call.args[0]).relative_to(self.root).as_posix() for call in json_store._read.call_args_list},
                         {"study_logs/2026/09/20260916.json", "master_data/master.json", "settings/study_categories.json"})

    def test_no_source_root_or_input_values_escape_the_json_cell_contract(self):
        self.log(self.day, [self.session()])
        snapshot = self.build()
        for name, table in snapshot["tables"].items():
            self.assertTrue(name.isascii())
            for row in table:
                self.assertEqual(len(row), len(table[0]))
                for cell in row:
                    self.assertIn(type(cell), (str, int, float, bool, type(None)))
        for options in ({"start": self.day, "end": self.day - timedelta(days=1)},
                        {"include_details": "yes"}, {"include_content": 1}, {"consultation_note": None}):
            with self.subTest(options=options), self.assertRaises(share.ShareSnapshotError):
                self.build(**options)


if __name__ == "__main__":
    unittest.main()
