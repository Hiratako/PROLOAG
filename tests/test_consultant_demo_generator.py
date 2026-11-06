from __future__ import annotations

from contextlib import ExitStack
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app_core import integrity_rules, integrity_service
from app_core.substance_service import default_substance_profiles, unified_substance_records
from scripts import generate_consultant_demo_dataset as demo


class ConsultantDemoGeneratorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag-consultant-demo-tests-")
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name)
        self.root = self.temporary / "consultant_demo_package" / "app"
        self.root.mkdir(parents=True)
        (self.root / "main.py").write_text("# Synthetic test app scaffold\n", encoding="utf-8")

    def generate(self, days=180, force=False):
        return demo.build_dataset(self.root, days, date(2026, 9, 11), 20260525, force)

    def read(self, relative):
        return json.loads((self.root / relative).read_text(encoding="utf-8"))

    def audit(self):
        with ExitStack() as patches:
            for module in (integrity_rules, integrity_service):
                for name, value in list(vars(module).items()):
                    if isinstance(value, Path) and value.is_relative_to(demo.SOURCE_ROOT):
                        patches.enter_context(patch.object(module, name, self.root / value.relative_to(demo.SOURCE_ROOT)))
            datasets = tuple((name, title, self.root / path.relative_to(demo.SOURCE_ROOT)) for name, title, path in integrity_rules.DATED_DATASETS)
            patches.enter_context(patch.object(integrity_rules, "DATED_DATASETS", datasets))
            return integrity_service.run_integrity_checks()

    def test_180_day_demo_integrity_defaults_and_manifest(self):
        summary = self.generate()
        self.assertEqual(summary["days"], 180)
        self.assertEqual(summary["non_caffeine_substance_records"], 90)
        self.assertGreater(summary["substance_records"], 90)
        self.assertEqual(summary["substance_profiles"], 4)
        settings = self.read("settings/user_settings.json")
        self.assertEqual(settings["countdown"], {"enabled": True, "title": "模擬試験（デモ）", "target_date": "2026-10-11"})
        self.assertEqual(settings["display"]["hidden_sidebar_pages"], [])
        self.assertEqual(self.read("settings/demo_mode.json")["fixed_today"], "2026-09-11")
        self.assertEqual(self.read("settings/substance_profiles.json")["profiles"], default_substance_profiles())
        manifest = self.read(demo.MANIFEST_NAME)
        expected_paths = {path.relative_to(self.root).as_posix() for path in self.root.rglob("*.json") if path.name != demo.MANIFEST_NAME}
        self.assertEqual(set(manifest["files"]), expected_paths)
        for relative, digest in manifest["files"].items():
            self.assertEqual(hashlib.sha256((self.root / relative).read_bytes()).hexdigest(), digest)
        before = {path: path.read_bytes() for path in self.root.rglob("*.json")}
        audit = self.audit()
        self.assertEqual(audit["issues"], [])
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob("*.json")})
        profile_map = {profile["substance_id"]: profile for profile in default_substance_profiles()}
        last_entries = [self.read(path.relative_to(self.root)) for path in sorted((self.root / "diary_entries").rglob("*.json"))[-30:]]
        seen = {record["substance_id"] for entry in last_entries for record in unified_substance_records(entry, profiles=profile_map)}
        self.assertEqual(seen, set(profile_map))
        for path in (self.root / "study_logs").rglob("*.json"):
            rows = self.read(path.relative_to(self.root))
            for index, row in enumerate(rows):
                start, end = datetime.fromisoformat(row["start_at"]), datetime.fromisoformat(row["end_at"])
                self.assertEqual(start.date(), end.date())
                self.assertEqual((end - start).total_seconds(), row["duration_sec"])
                self.assertEqual(row["io_type"], row["condition"]["io_type"])
                if index:
                    self.assertGreater(start, datetime.fromisoformat(rows[index - 1]["end_at"]))

    def test_fresh_portable_empty_scaffold_is_allowed_without_force(self):
        for relative, value in demo.EMPTY_SCAFFOLD.items():
            demo.write_json(self.root / relative, value)
        self.generate(days=1)
        self.assertTrue((self.root / demo.MANIFEST_NAME).exists())

    def test_source_project_and_ancestors_rejected_before_any_write_or_delete(self):
        with patch.object(demo, "write_json") as write, patch.object(demo.shutil, "rmtree") as delete:
            for path in (demo.SOURCE_ROOT, demo.SOURCE_ROOT.parent, demo.SOURCE_ROOT.parent.parent):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    demo.build_dataset(path, 180, date(2026, 9, 11), 1, True)
            write.assert_not_called()
            delete.assert_not_called()

    def test_non_demo_app_and_unmarked_records_cannot_be_forced(self):
        ordinary = self.temporary / "ordinary" / "app"
        ordinary.mkdir(parents=True)
        (ordinary / "main.py").write_text("# test", encoding="utf-8")
        with self.assertRaises(ValueError):
            demo.build_dataset(ordinary, 1, date(2026, 9, 11), 1, True)
        imported = self.root / "study_logs" / "keep.json"
        demo.write_json(imported, [{"memo": "Synthetic sentinel: never overwrite"}])
        original = imported.read_bytes()
        with self.assertRaises(ValueError):
            self.generate(days=1, force=True)
        self.assertEqual(imported.read_bytes(), original)

    def test_marked_demo_regeneration_is_deterministic_and_requires_force(self):
        self.generate(days=10)
        first = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*.json")}
        with self.assertRaises(FileExistsError):
            self.generate(days=10)
        self.generate(days=10, force=True)
        self.assertEqual(first, {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*.json")})

    def test_invalid_range_cannot_clear_existing_demo(self):
        self.generate(days=1)
        before = (self.root / demo.MANIFEST_NAME).read_bytes()
        for days in (0, -1, 3651, True):
            with self.subTest(days=days), self.assertRaises(ValueError):
                demo.build_dataset(self.root, days, date(2026, 9, 11), 1, True)
        self.assertEqual(before, (self.root / demo.MANIFEST_NAME).read_bytes())

    def test_linked_data_target_is_rejected_before_clear(self):
        self.generate(days=1)
        outside = self.temporary / "outside"
        outside.mkdir()
        sentinel = outside / "keep.json"
        sentinel.write_text("[]", encoding="utf-8")
        link = self.root / "settings" / "outside"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Symlink creation is unavailable on this Windows account")
        with self.assertRaises(ValueError):
            self.generate(days=1, force=True)
        self.assertTrue(sentinel.exists())

    def test_windows_reparse_point_is_rejected_without_link_creation_privileges(self):
        path = Mock(spec=Path)
        path.is_symlink.return_value = False
        path.exists.return_value = True
        path.lstat.return_value = SimpleNamespace(st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        with self.assertRaises(ValueError):
            demo._reject_link(path)


if __name__ == "__main__":
    unittest.main()
