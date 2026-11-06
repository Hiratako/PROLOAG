from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import tempfile
import unittest

from app_core.export_service import build_export_bundle


class ExportQualityTests(unittest.TestCase):
    def test_roadmap_import_history_is_retained_in_settings_export(self):
        with tempfile.TemporaryDirectory(prefix="proloag_export_test_") as temporary:
            root = Path(temporary)
            settings = root / "settings"
            settings.mkdir()
            ledger = {"schema_version": 1, "imports": [
                {"operation_id": "a" * 32, "plan_title": "架空の学習計画", "items": 2}
            ]}
            path = settings / "roadmap_imports.json"
            path.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
            (settings / "credentials.json").write_text('{"private":"DO_NOT_EXPORT"}', encoding="utf-8")
            before = path.read_bytes()
            bundle = build_export_bundle(root)
            exported = "\n".join(table.data.decode("utf-8-sig") for table in bundle.tables)
            self.assertIn("roadmap_imports.json", exported)
            self.assertIn("架空の学習計画", exported)
            self.assertNotIn("DO_NOT_EXPORT", exported)
            self.assertEqual(path.read_bytes(), before)

    def test_statistics_practice_attempts_are_exported_as_rows(self):
        with tempfile.TemporaryDirectory(prefix="proloag_export_test_") as temporary:
            root = Path(temporary)
            path = root / "statistics_practice" / "2026" / "08" / "20260831.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "schema_version": 1,
                "date": "2026-08-31",
                "attempts": [{
                    "attempt_id": "statistics_attempt_001",
                    "attempted_at": "2026-08-31T12:00:00",
                    "problem_id": "statistics_problem_001",
                    "template_id": "daily_mean",
                    "topic": "descriptive",
                    "title": "平均値",
                    "score": 100.0,
                    "max_score": 100.0,
                    "correct_count": 1,
                    "total_parts": 1,
                    "answers": {"mean": 2.5},
                    "grade": {"all_correct": True},
                    "problem_snapshot": {"data_rows": [[2.0], [3.0]]},
                }],
            }, ensure_ascii=False), encoding="utf-8")

            bundle = build_export_bundle(root)
            table = next(item for item in bundle.tables if item.key == "statistics_practice_attempts")
            rows = list(csv.DictReader(io.StringIO(table.data.decode("utf-8-sig"))))

            self.assertEqual(table.row_count, 1)
            self.assertEqual(rows[0]["attempt_id"], "statistics_attempt_001")
            self.assertEqual(rows[0]["parent_date"], "2026-08-31")
            self.assertEqual(rows[0]["score"], "100.0")

    def test_invalid_json_is_reported_without_losing_valid_rows_or_writing_files(self):
        with tempfile.TemporaryDirectory(prefix="proloag_export_test_") as temporary:
            root = Path(temporary)
            study_dir = root / "study_logs" / "2026" / "08"
            study_dir.mkdir(parents=True)
            valid = study_dir / "20260801.json"
            valid.write_text(json.dumps([{
                "session_id": "session_20260801_001",
                "start_at": "2026-08-01T09:00:00",
                "duration_sec": 600,
            }]), encoding="utf-8")
            invalid_files = (
                ("20260802.json", b'[{"duration_sec":NaN}]'),
                ("20260803.json", b'[{"duration_sec":1,"duration_sec":2}]'),
                ("20260804.json", b'\xff'),
                ("20260805.json", b'[{'),
            )
            for name, content in invalid_files:
                (study_dir / name).write_bytes(content)
            backup = study_dir / "backups" / "ignored.json"
            backup.parent.mkdir()
            backup.write_text(valid.read_text(encoding="utf-8"), encoding="utf-8")
            before = {
                str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in root.rglob("*") if path.is_file()
            }
            bundle = build_export_bundle(root)
            study_table = next(table for table in bundle.tables if table.key == "study_sessions")
            self.assertEqual(study_table.row_count, 1)
            self.assertEqual(len(bundle.warnings), len(invalid_files))
            for name, _ in invalid_files:
                self.assertTrue(any(name in warning for warning in bundle.warnings))
            after = {
                str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in root.rglob("*") if path.is_file()
            }
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
