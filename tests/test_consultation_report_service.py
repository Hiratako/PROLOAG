from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core import consultation_report_service as service
from app_core import json_store
from app_core.google_share_snapshot import ShareSnapshotError
from app_core.google_sheets_client import GoogleShareError


class FakeDocs:
    def __init__(self):
        self.publication = {"document_id": "fixture-doc"}
        self.writes = []
        self.error = None
        self.commit_before_error = False
        self.after_publish_read_error = False
        self.creates = []

    def create_report_document(self, kind):
        self.creates.append(kind)
        return {"document_id": "fixture-doc", "url": "https://docs.google.com/document/d/fixture-doc/edit"}

    def read_report_publication(self, kind):
        if self.after_publish_read_error and self.writes:
            raise GoogleShareError("認証期限", code="reauthorize")
        return deepcopy(self.publication)

    def publish_report(self, kind, report, *, expected_document_id):
        if self.error and not self.commit_before_error:
            raise self.error
        self.writes.append(deepcopy(report))
        self.publication = {"document_id": expected_document_id, "snapshot_id": report["snapshot_id"],
                            "payload_hash": service.payload_hash(report)}
        if self.error:
            raise self.error


class ConsultationReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag_report_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.client = FakeDocs()
        self.end = date(2026, 9, 16)
        self.now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
        self.put("master_data/master.json", {
            "projects": [{"project_id": "p", "name": "編入"}],
            "subjects": [{"subject_id": "s", "project_id": "p", "name": "統計学"}],
            "materials": [{"material_id": "m", "project_id": "p", "subject_id": "s", "name": "統計小問"},
                          {"material_id": "m2", "name": "非共有教材"}], "private_master": "MASTER_SECRET"})
        for day, seconds in ((16, 3600), (10, 1800), (9, 7200), (3, 3600)):
            self.put(f"study_logs/2026/09/202609{day:02}.json", [{
                "session_id": f"s{day}", "start_at": f"2026-09-{day:02}T09:00:00", "duration_sec": seconds,
                "project_id": "p", "subject_id": "s", "material_id": "m", "memo": "PRIVATE_STUDY_MEMO",
                "money": "MONEY_SECRET", "health": "HEALTH_SECRET", "unknown": "SESSION_SECRET"}])
        entries = {}
        for key in ("m", "m2"):
            entries[key] = {"material_id": key, "next_step": f"NEXT_{key}",
                            "created_at": "2026-09-16T10:00:00+09:00", "updated_at": "2026-09-16T10:00:00+09:00",
                            "links": [{"link_id": "link_" + key, "title": f"LINK_{key}",
                                       "url": f"https://docs.google.com/document/d/{key}/edit", "unknown": "LINK_SECRET"}],
                            "unknown": "CONTEXT_SECRET"}
        self.put("settings/learning_context.json", {"schema_version": 1, "materials": entries, "private": "TOP_SECRET"})
        self.put("diary_entries/2026/09/20260916.json", {"memo": "DIARY_SECRET"})
        original = json_store._read

        def isolated_read(path):
            self.assertTrue(Path(path).resolve().is_relative_to(self.root))
            self.assertNotIn("diary_entries", Path(path).parts)
            return original(path)

        reader = patch.object(json_store, "_read", side_effect=isolated_read)
        reader.start()
        self.addCleanup(reader.stop)

    def put(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def build(self, kind="weekly", **kwargs):
        return service.build_consultation_report(self.root, self.end, kind, generated_at=self.now, **kwargs)

    def send(self, report=None, **kwargs):
        return service.publish_consultation_report(self.client, report, root=self.root,
                                                   expected_document_id="fixture-doc", **kwargs)

    def test_weekly_factual_totals_previous_period_newest_first_and_no_source_writes(self):
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*.json")}
        report = self.build()
        self.assertEqual(report["period"], {"start": "2026-09-10", "end": "2026-09-16"})
        self.assertEqual(report["counts"], {"study_sessions": 2, "study_days": 2})
        self.assertIn("総学習時間: 1.50 時間（5400 秒）", report["text"])
        self.assertIn("比較期間の総学習時間: 3.00 時間（10800 秒）", report["text"])
        self.assertIn("前期間との差: -1.50 時間", report["text"])
        self.assertLess(report["text"].index("・2026-09-16:"), report["text"].index("・2026-09-10:"))
        self.assertIn("未学習とは断定しません", report["text"])
        self.assertEqual(before, {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*.json")})

    def test_daily_is_one_day_and_compares_previous_day(self):
        report = self.build("daily")
        self.assertEqual(report["period"]["start"], "2026-09-16")
        self.assertEqual(report["counts"]["study_sessions"], 1)
        self.assertIn("比較期間: 2026-09-15 ～ 2026-09-15", report["text"])

    def test_private_by_default_and_ignore_unrequested_context(self):
        report = self.build(material_ids=["deleted"])
        for secret in ("PRIVATE_STUDY_MEMO", "MONEY_SECRET", "HEALTH_SECRET", "MASTER_SECRET", "NEXT_m", "LINK_m", "DIARY_SECRET"):
            self.assertNotIn(secret, report["text"])
        self.assertEqual(report["options"]["material_ids"], [])

    def test_context_selection_and_memo_are_separate_allowlisted_opt_ins(self):
        report = self.build(material_ids=["m"], include_links=True, include_next_step=True,
                            include_study_memo=True, consultation_note="=FORMULA is plain text\n相談事項")
        for included in ("NEXT_m", "LINK_m", "PRIVATE_STUDY_MEMO", "相談事項", "=FORMULA is plain text"):
            self.assertIn(included, report["text"])
        for excluded in ("NEXT_m2", "LINK_m2", "MONEY_SECRET", "LINK_SECRET", "CONTEXT_SECRET", "TOP_SECRET"):
            self.assertNotIn(excluded, report["text"])
        only_links = self.build(material_ids=["m"], include_links=True)
        self.assertNotIn("NEXT_m", only_links["text"])
        only_next = self.build(material_ids=["m"], include_next_step=True)
        self.assertNotIn("LINK_m", only_next["text"])

    def test_identity_stable_across_generation_times_changes_with_content(self):
        one = self.build()
        two = service.build_consultation_report(self.root, self.end, generated_at=datetime(2026, 9, 17))
        self.assertEqual(one["snapshot_id"], two["snapshot_id"])
        self.assertNotEqual(service.payload_hash(one), service.payload_hash(two))
        self.assertNotEqual(one["snapshot_id"], self.build(consultation_note="new")["snapshot_id"])

    def test_invalid_options_deleted_material_and_corrupt_source_stop(self):
        for kwargs in ({"include_links": True}, {"include_next_step": True, "material_ids": ["deleted"]},
                       {"include_study_memo": 1}, {"consultation_note": "x" * 4001}):
            with self.subTest(kwargs=kwargs), self.assertRaises(service.ReportError):
                self.build(**kwargs)
        (self.root / "study_logs/2026/09/20260916.json").write_text("[bad", encoding="utf-8")
        with self.assertRaises((ValueError, OSError)):
            self.build()
        self.assertEqual(self.client.writes, [])

    def test_context_corruption_only_blocks_when_context_is_selected(self):
        (self.root / "settings/learning_context.json").write_text("broken", encoding="utf-8")
        self.build()
        with self.assertRaises((ValueError, OSError)):
            self.build(material_ids=["m"], include_links=True)

    def test_read_state_does_not_create_json(self):
        before = sorted(str(p) for p in self.root.rglob("*.json"))
        self.assertEqual(service.load_report_state(self.root), {})
        self.assertIsNone(service.load_pending_report(self.root))
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*.json")))

    def test_explicit_creation_sends_no_report_and_success_tracks_history(self):
        service.create_report_destination(self.client, "weekly", root=self.root)
        self.assertEqual(self.client.writes, [])
        report = self.build()
        entry = self.send(report)
        self.assertFalse(entry["reconciled"])
        self.assertIsNone(service.load_pending_report(self.root))
        self.assertEqual(service.load_report_state(self.root)["last_success"]["weekly"], entry)
        self.assertIn(report["snapshot_id"], service.report_consultation_prompt(entry))

    def test_lost_response_reconciles_without_duplicate(self):
        self.client.error = GoogleShareError("応答不明", uncertain=True)
        self.client.commit_before_error = True
        with self.assertRaises(GoogleShareError):
            self.send(self.build())
        self.assertEqual(service.load_pending_report(self.root)["status"], "uncertain")
        self.assertTrue(self.send(check_only=True)["reconciled"])
        self.assertEqual(len(self.client.writes), 1)

    def test_uncertain_retry_pins_report_blocks_creation_and_discard(self):
        self.client.error = GoogleShareError("応答不明", uncertain=True)
        report = self.build()
        with self.assertRaises(GoogleShareError):
            self.send(report)
        with self.assertRaises(service.ReportError):
            self.send(self.build(consultation_note="different"))
        with self.assertRaises(service.ReportError):
            service.create_report_destination(self.client, "daily", root=self.root)
        with self.assertRaises(service.ReportError):
            service.discard_failed_report(self.root)
        self.assertFalse(self.send(check_only=True)["confirmed"])
        self.assertEqual(self.client.writes, [])
        self.client.error = None
        self.send()
        self.assertEqual(self.client.writes, [report])

    def test_definite_failure_and_changed_destination_are_cancellable(self):
        self.client.error = GoogleShareError("権限なし", code="reauthorize")
        with self.assertRaises(GoogleShareError):
            self.send(self.build())
        self.assertEqual(service.load_pending_report(self.root)["status"], "failed")
        service.discard_failed_report(self.root)
        self.client.publication["document_id"] = "other-doc"
        with self.assertRaises(GoogleShareError):
            self.send(self.build())
        self.assertEqual(service.load_pending_report(self.root)["status"], "failed")
        service.discard_failed_report(self.root)
        self.assertEqual(self.client.writes, [])

    def test_post_commit_read_error_remains_uncertain(self):
        self.client.after_publish_read_error = True
        with self.assertRaises(GoogleShareError):
            self.send(self.build())
        self.assertEqual(service.load_pending_report(self.root)["status"], "uncertain")
        self.client.after_publish_read_error = False
        self.assertTrue(self.send()["reconciled"])

    def test_history_save_failure_and_pending_clear_failure_recover_idempotently(self):
        original = json_store.save_json_safe
        for filename in ("reports_state.json", "report_pending.json"):
            def fail(path, data, **kwargs):
                if Path(path).name == filename and (filename == "reports_state.json" or data is None):
                    raise OSError("disk unavailable")
                return original(path, data, **kwargs)
            with patch.object(json_store, "save_json_safe", side_effect=fail), self.assertRaises(OSError):
                self.send(self.build(consultation_note=filename))
            self.assertEqual(service.load_pending_report(self.root)["status"], "uncertain")
            self.assertTrue(self.send()["reconciled"])
        self.assertEqual(len(self.client.writes), 2)
        self.assertEqual(len(service.load_report_state(self.root)["history"]), 2)

    def test_pending_payload_tampering_stops_before_write(self):
        self.client.error = GoogleShareError("応答不明", uncertain=True)
        with self.assertRaises(GoogleShareError):
            self.send(self.build())
        pending = service.load_pending_report(self.root)
        pending["report"]["text"] += "tampered"
        self.put("backups/google_share/report_pending.json", pending)
        with self.assertRaises(service.ReportError):
            self.send()
        self.assertEqual(self.client.writes, [])

    def test_stale_pending_actions_cannot_operate_on_another_report(self):
        self.client.error = GoogleShareError("権限なし", code="reauthorize")
        with self.assertRaises(GoogleShareError):
            self.send(self.build())
        old_id = service.load_pending_report(self.root)["operation_id"]
        service.discard_failed_report(self.root, expected_operation_id=old_id)
        with self.assertRaises(GoogleShareError):
            self.send(self.build(consultation_note="new"))
        with self.assertRaises(service.ReportError):
            self.send(expected_operation_id=old_id)
        with self.assertRaises(service.ReportError):
            service.discard_failed_report(self.root, expected_operation_id=old_id)
        self.assertEqual(service.load_pending_report(self.root)["report"]["text"], self.build(consultation_note="new")["text"])
        self.assertEqual(self.client.writes, [])

    def test_malformed_state_fails_before_network_or_pending_write(self):
        for bad in ({"history": ["bad"]}, {"last_success": {"weekly": {}}},
                    {"destinations": {"weekly": {"document_id": "valid-id", "url": "javascript:bad"}}},
                    {"schema_version": True}):
            with self.subTest(bad=bad):
                self.put("backups/google_share/reports_state.json", bad)
                with self.assertRaises(service.ReportError):
                    self.send(self.build())
                with self.assertRaises(service.ReportError):
                    service.create_report_destination(self.client, "weekly", root=self.root)
                self.assertIsNone(service.load_pending_report(self.root))
        self.assertEqual(self.client.writes, [])
        self.assertEqual(self.client.creates, [])

    def test_malformed_report_cannot_be_queued(self):
        for field, value in (("kind", []), ("period", {}), ("counts", None), ("snapshot_id", "bad")):
            report = self.build()
            report[field] = value
            with self.subTest(field=field), self.assertRaises(service.ReportError):
                self.send(report)
        self.assertIsNone(service.load_pending_report(self.root))
        self.assertEqual(self.client.writes, [])


if __name__ == "__main__":
    unittest.main()
