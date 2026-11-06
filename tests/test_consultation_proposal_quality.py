from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
import csv
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core import integrity_rules
from app_core.export_service import build_export_bundle
from app_core.google_share_snapshot import build_share_snapshot
from app_core.consultation_report_service import build_consultation_report


class ConsultationProposalQualityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag-proposal-quality-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patches = ExitStack()
        self.addCleanup(patches.close)
        locations = {
            "PROJECT_ROOT": self.root, "SETTINGS_DIR": self.root / "settings",
            "NOTES_DATA_DIR": self.root / "notes_data", "MASTER_DATA_DIR": self.root / "master_data",
            "FLASHCARDS_PATH": self.root / "flashcards.json", "REMINDERS_PATH": self.root / "reminders.json",
            "USER_SETTINGS_PATH": self.root / "settings/user_settings.json",
            "SUBSTANCE_PROFILES_PATH": self.root / "settings/substance_profiles.json",
        }
        for name, value in locations.items():
            patches.enter_context(patch.object(integrity_rules, name, value))
        patches.enter_context(patch.object(integrity_rules, "DATED_DATASETS", (
            ("plans", "日次計画", self.root / "daily_plans"),)))
        self.write("master_data/master.json", {"projects": [], "subjects": [], "units": [], "tags": [],
                                               "materials": [{"material_id": "m1", "name": "架空教材"}]})

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    @staticmethod
    def entry(*, kind="reference", material_id="", target_date="", status="pending", title="架空の提案"):
        source = {"report_id": "a" * 64, "label": "手動で貼り付けた架空相談"}
        proposal = {"title": title, "text": "PRIVATE_PROPOSAL_TEXT", "reason": "架空の理由",
                    "target": {"kind": kind, "material_id": material_id, "target_date": target_date}}
        # Independent construction of the documented identity, not a production helper.
        serialized = json.dumps({"source": source, "proposal": proposal}, ensure_ascii=False,
                                sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        identifier = "proposal_" + hashlib.sha256(serialized).hexdigest()
        return {"proposal_id": identifier, "source": source, "proposal": proposal, "status": status,
                "created_at": "2026-09-16T10:00:00+09:00", "updated_at": "2026-09-16T11:00:00+09:00"}

    @staticmethod
    def memo(entry):
        return {"proposal_id": entry["proposal_id"], "title": entry["proposal"]["title"],
                "text": entry["proposal"]["text"], "adopted_at": "2026-09-16T10:30:00+09:00"}

    @classmethod
    def application(cls, entry, *, kind="daily_memo", day="2099-12-31", material_id="m1"):
        return {"operation_id": "1" * 32, "kind": kind, "material_id": material_id if kind == "next_step" else "",
                "target_date": day if kind == "daily_memo" else "",
                "target_path": f"daily_plans/{day[:4]}/{day[5:7]}/{day.replace('-', '')}.json" if kind == "daily_memo" else "settings/learning_context.json",
                "applied_at": "2026-09-16T10:30:00+09:00", "after_revision": "b" * 64,
                "before": {"file_existed": False, "field_present": False, "value": None},
                "applied_value": cls.memo(entry) if kind == "daily_memo" else entry["proposal"]["text"]}

    def inbox(self, *entries, **extra):
        return self.write("settings/consultation_inbox.json", {"schema_version": 1,
                         "proposals": {entry["proposal_id"]: entry for entry in entries}, **extra})

    def plan(self, memos, day="2099-12-31"):
        return self.write(f"daily_plans/{day[:4]}/{day[5:7]}/{day.replace('-', '')}.json", {
            "plan_id": "plan_" + day.replace("-", ""), "date": day, "target_total_minutes": 0,
            "created_at": "2026-09-16T10:00:00", "updated_at": "2026-09-16T11:00:00",
            "focus_subjects": [], "materials": [], "tasks": [], "review_note_ids": [], "review_card_ids": [],
            "consultation_memos": memos,
        })

    def snapshot(self):
        return {path.relative_to(self.root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob("*.json")}

    def audit(self):
        before = self.snapshot()
        result = integrity_rules.run_deep_integrity_checks()
        self.assertEqual(self.snapshot(), before)
        return result

    @staticmethod
    def proposal_issues(result):
        return [issue for issue in result["issues"] if "consultation_inbox.json" in issue["対象ファイル"]
                or issue["問題カテゴリ"].startswith("相談")]

    def test_absent_optional_data_is_compatible_and_not_created(self):
        self.assertEqual(self.proposal_issues(self.audit()), [])
        before = self.snapshot()
        bundle = build_export_bundle(self.root)
        self.assertEqual(next(table for table in bundle.tables if table.key == "settings").row_count, 0)
        self.assertEqual(self.snapshot(), before)

    def test_valid_states_unknown_fields_and_future_targets_are_read_only(self):
        entries = []
        for status in ("pending", "deferred", "rejected", "applied", "reverted"):
            entry = self.entry(kind="daily_memo", target_date="2099-12-31", status=status, title=status)
            if status in ("applied", "reverted"):
                entry["application"] = self.application(entry)
            if status == "reverted":
                entry["application"].update(reverted_at="2026-09-16T10:45:00+09:00", revert_operation_id="2" * 32)
            entry["unknown"] = {"keep": [1, None]}
            entry["source"]["future"] = "retained"
            entry["proposal"]["future"] = {"kept": True}
            entry["proposal"]["target"]["future"] = 3
            entries.append(entry)
        self.inbox(*entries, future={"schema_extension": True})
        self.assertEqual(self.proposal_issues(self.audit()), [])

    def test_schema_hash_status_source_and_target_date_errors_are_reported(self):
        mutations = [
            lambda data, entry: data.update(schema_version=True),
            lambda data, entry: data.update(proposals=[]),
            lambda data, entry: entry.update(status="automatically_executed"),
            lambda data, entry: entry["proposal"].update(text="tampered"),
            lambda data, entry: entry["source"].update(report_id="not-a-report-id"),
            lambda data, entry: entry["proposal"]["target"].update(target_date="2026-02-30"),
            lambda data, entry: entry.update(proposal_id="proposal_" + "0" * 64),
            lambda data, entry: entry.update(updated_at="2026-09-15T10:00:00"),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                entry = self.entry(kind="daily_memo", target_date="2099-12-31")
                data = {"schema_version": 1, "proposals": {entry["proposal_id"]: entry}}
                mutate(data, entry)
                self.write("settings/consultation_inbox.json", data)
                self.assertTrue(any(issue["重要度"] == integrity_rules.ERROR for issue in self.proposal_issues(self.audit())))

    def test_malformed_application_is_rejected_without_target_writes(self):
        for change in ({"target_path": "../outside.json"}, {"after_revision": "bad"}, {"operation_id": "bad"}, {"before": None}):
            with self.subTest(change=change):
                entry = self.entry(status="applied")
                entry["application"] = {**self.application(entry), **change}
                self.inbox(entry)
                self.assertTrue(any(issue["問題カテゴリ"] == "相談提案形式" for issue in self.proposal_issues(self.audit())))
        self.assertFalse((self.root / "daily_plans").exists())

    def test_unknown_hint_or_deleted_applied_material_is_only_notice(self):
        entry = self.entry(kind="next_step", material_id="unknown-material", status="applied")
        entry["application"] = self.application(entry, kind="next_step", material_id="deleted-material")
        self.inbox(entry)
        issues = self.proposal_issues(self.audit())
        self.assertEqual(len(issues), 2)
        self.assertTrue(all(issue["重要度"] == integrity_rules.NOTICE for issue in issues))

    def test_daily_memo_uses_actual_application_not_original_reference_hint(self):
        entry = self.entry(status="applied")
        entry["application"] = self.application(entry)
        self.inbox(entry)
        self.plan([self.memo(entry)])
        self.assertEqual(self.proposal_issues(self.audit()), [])

    def test_daily_memo_wrong_owner_or_status_is_notice(self):
        entry = self.entry(status="applied")
        entry["application"] = self.application(entry, day="2099-12-30")
        self.inbox(entry)
        self.plan([self.memo(entry)])
        issues = self.proposal_issues(self.audit())
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["問題カテゴリ"], "相談メモ参照")
        self.assertEqual(issues[0]["重要度"], integrity_rules.NOTICE)

    def test_missing_proposal_reference_is_notice_and_memo_is_preserved(self):
        self.plan([self.memo(self.entry())])
        issues = self.proposal_issues(self.audit())
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["重要度"], integrity_rules.NOTICE)

    def test_memo_shape_ids_text_and_adoption_time_are_checked(self):
        entry = self.entry(status="applied")
        entry["application"] = self.application(entry)
        self.inbox(entry)
        invalid_memos = [None, {}, [None], [{**self.memo(entry), "proposal_id": "bad"}],
                         [{**self.memo(entry), "title": []}], [{**self.memo(entry), "text": "\x00"}],
                         [{**self.memo(entry), "adopted_at": "2099-12-31"}],
                         [self.memo(entry), self.memo(entry)]]
        for memos in invalid_memos:
            with self.subTest(memos=memos):
                self.plan(memos)
                self.assertTrue(any(issue["重要度"] == integrity_rules.ERROR for issue in self.proposal_issues(self.audit())))

    def test_invalid_inbox_does_not_invent_missing_reference_notice(self):
        path = self.inbox(self.entry())
        path.write_bytes(b'{broken')
        self.plan([self.memo(self.entry())])
        issues = self.proposal_issues(self.audit())
        self.assertTrue(any(issue["問題カテゴリ"] == "JSON構文" for issue in issues))
        self.assertFalse(any(issue["問題カテゴリ"] == "相談メモ参照" for issue in issues))

    def test_json_duplicate_keys_and_nonfinite_values_are_in_existing_settings_scan(self):
        path = self.inbox()
        for payload, category in ((b'{"schema_version":1,"schema_version":2,"proposals":{}}', "JSON構文"),
                                  (b'{"schema_version":1,"proposals":{},"unknown":NaN}', "JSON値")):
            with self.subTest(payload=payload):
                path.write_bytes(payload)
                self.assertTrue(any(issue["問題カテゴリ"] == category for issue in self.proposal_issues(self.audit())))

    def test_deep_stored_unknown_extension_is_reported_without_recursion_crash(self):
        future = []
        for _ in range(600):
            future = [future]
        self.inbox(self.entry(), future=future)
        issues = self.proposal_issues(self.audit())
        self.assertTrue(any(issue["問題カテゴリ"] == "相談提案形式" for issue in issues))

    def test_local_export_includes_inbox_and_daily_memos_but_keeps_closed_allowlist(self):
        entry = self.entry(status="applied")
        entry["application"] = self.application(entry)
        entry["future"] = "KEEP_UNKNOWN"
        self.inbox(entry)
        memo = self.memo(entry)
        self.plan([memo])
        self.write("settings/credentials.json", {"secret": "NEVER_EXPORT_AUTH"})
        self.write("backups/google_share/reports_state.json", {"private": "NEVER_EXPORT_AUTH"})
        before = self.snapshot()
        bundle = build_export_bundle(self.root)
        settings = next(table for table in bundle.tables if table.key == "settings")
        rows = list(csv.DictReader(io.StringIO(settings.data.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["settings_file"], "consultation_inbox.json")
        prefix = "proposals." + entry["proposal_id"]
        self.assertEqual(rows[0][prefix + ".proposal.text"], "PRIVATE_PROPOSAL_TEXT")
        self.assertEqual(rows[0][prefix + ".future"], "KEEP_UNKNOWN")
        plans = next(table for table in bundle.tables if table.key == "daily_plans")
        plan_rows = list(csv.DictReader(io.StringIO(plans.data.decode("utf-8-sig"))))
        self.assertEqual(json.loads(plan_rows[0]["consultation_memos"]), [memo])
        self.assertFalse(any(b"NEVER_EXPORT_AUTH" in table.data for table in bundle.tables))
        self.assertEqual(self.snapshot(), before)

    def test_local_proposals_and_plan_memos_are_not_automatically_added_to_google_outputs(self):
        entry = self.entry(status="applied")
        entry["application"] = self.application(entry, day="2026-09-16")
        self.inbox(entry)
        self.plan([self.memo(entry)], day="2026-09-16")
        before = self.snapshot()
        snapshot = build_share_snapshot(self.root, date(2026, 9, 16), date(2026, 9, 16))
        report = build_consultation_report(self.root, date(2026, 9, 16), "daily")
        self.assertNotIn("PRIVATE_PROPOSAL_TEXT", json.dumps(snapshot))
        self.assertNotIn("PRIVATE_PROPOSAL_TEXT", json.dumps(report))
        self.assertEqual(self.snapshot(), before)

    def test_corrupt_inbox_export_warns_and_preserves_file(self):
        path = self.inbox()
        path.write_bytes(b'{broken')
        before = self.snapshot()
        bundle = build_export_bundle(self.root)
        self.assertTrue(any("consultation_inbox.json" in warning for warning in bundle.warnings))
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
