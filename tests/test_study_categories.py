from copy import deepcopy
import csv
from datetime import date
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core import json_store
from app_core import study_category_service as service


class StudyCategoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pgcat_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = service.category_catalog_path(self.root)
        patcher = patch.object(json_store, "PROJECT_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def put(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def revision(self):
        service.load_study_category_catalog(self.root)
        return json_store.observed_json_revision(self.path)

    def rename(self, category_id="category_default_1", name="学士医学", active=True):
        return service.save_study_category(self.root, name, category_id=category_id,
                                          is_active=active, expected_revision=self.revision())

    def test_default_read_and_choices_do_not_create_files(self):
        self.assertEqual(service.study_category_names(service.load_study_category_catalog(self.root)), service.DEFAULT_STUDY_CATEGORIES)
        self.assertEqual(service.study_category_options(self.root)[0], "")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_add_rename_and_alias_rename_back_preserve_identifier(self):
        saved = service.save_study_category(self.root, "統計学")
        self.assertIn("統計学", service.study_category_options(self.root))
        changed = self.rename(saved["category_id"], "データ分析")
        self.assertEqual(changed["category_id"], saved["category_id"])
        self.assertEqual(changed["aliases"], ["統計学"])
        catalog = service.load_study_category_catalog(self.root)
        self.assertEqual(service.resolve_study_category("統計学", catalog), "データ分析")
        back = self.rename(saved["category_id"], "統計学")
        self.assertEqual(back["aliases"], ["データ分析"])

    def test_rename_never_rewrites_study_or_health_records(self):
        study = self.put("study_logs/2026/01/20260101.json", [{"condition": {"category": "医学"}, "memo": "synthetic"}])
        health = self.put("health_logs/2026/01/20260101.json", {"sleep": {"duration_min": 420}})
        before = study.read_bytes(), health.read_bytes()
        self.rename()
        self.assertEqual((study.read_bytes(), health.read_bytes()), before)
        self.assertEqual(service.resolve_study_category("医学", service.load_study_category_catalog(self.root)), "学士医学")

    def test_disabled_name_disappears_from_new_choices_but_history_is_retained(self):
        self.rename(active=False)
        self.assertNotIn("学士医学", service.study_category_options(self.root))
        self.assertIn("医学", service.study_category_options(self.root, current="医学"))
        catalog = service.load_study_category_catalog(self.root)
        self.assertIn("学士医学", service.study_category_names(catalog, include_inactive=True))
        self.assertEqual(service.resolve_study_category("医学", catalog), "学士医学")

    def test_old_name_and_disabled_name_cannot_be_reused_for_another_category(self):
        self.rename(active=False)
        for name in ("医学", "学士医学", "ＩＴ", "it"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "既に"):
                service.save_study_category(self.root, name)

    def test_empty_long_control_and_nonboolean_values_are_rejected(self):
        for name in ("", "   ", "x" * 81, "a\nb", "a\x00b"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                service.save_study_category(self.root, name)
        with self.assertRaises(ValueError):
            service.save_study_category(self.root, "統計学", is_active=1)
        self.assertFalse(self.path.exists())

    def test_unconfirmed_existing_edit_requires_revision(self):
        with self.assertRaisesRegex(ValueError, "再読込"):
            service.save_study_category(self.root, "学士医学", category_id="category_default_1")

    def test_concurrent_change_is_not_overwritten(self):
        old = self.revision()
        service.save_study_category(self.root, "統計学")
        before = self.path.read_bytes()
        with self.assertRaises(json_store.JsonConflictError):
            service.save_study_category(self.root, "学士医学", category_id="category_default_1", expected_revision=old)
        self.assertEqual(self.path.read_bytes(), before)

    def test_corrupt_or_null_catalog_is_not_replaced(self):
        for bad in (None, [], {"schema_version": True, "categories": []}):
            self.put("settings/study_categories.json", bad)
            before = self.path.read_bytes()
            with self.assertRaises(ValueError):
                service.load_study_category_catalog(self.root)
            with self.assertRaises(ValueError):
                service.save_study_category(self.root, "統計学")
            self.assertEqual(self.path.read_bytes(), before)

    def test_duplicate_alias_catalog_and_invalid_active_state_are_rejected(self):
        for modify in (lambda data: data["categories"][1]["aliases"].append("医学"),
                       lambda data: data["categories"][0].update(is_active="false")):
            data = service.default_category_catalog()
            modify(data)
            with self.assertRaises(ValueError):
                service.validate_category_catalog(data)

    def test_write_failure_preserves_previous_catalog(self):
        service.save_study_category(self.root, "統計学")
        before = self.path.read_bytes()
        original = json_store.save_json_safe
        failed = False
        def fail_once(path, *args, **kwargs):
            nonlocal failed
            if Path(path).resolve() == self.path and not failed:
                failed = True
                raise OSError("synthetic save failure")
            return original(path, *args, **kwargs)
        with patch.object(json_store, "save_json_safe", side_effect=fail_once), self.assertRaises(ValueError):
            service.save_study_category(self.root, "法律")
        self.assertEqual(self.path.read_bytes(), before)

    def records(self):
        return [
            {"session_id": "synthetic1", "start_at": "2026-01-01T09:00:00", "end_at": "2026-01-01T09:30:00",
             "duration_sec": 1800, "hourly_rate": 1000, "money": 500, "condition": {"category": "医学", "io_type": "インプット"}},
            {"session_id": "synthetic2", "start_at": "2026-01-01T10:00:00", "end_at": "2026-01-01T11:00:00",
             "duration_sec": 3600, "hourly_rate": 1000, "money": 1000, "condition": {"category": "統計学", "io_type": "アウトプット"}},
        ]

    def configured_records(self):
        service.save_study_category(self.root, "統計学")
        self.rename()
        self.put("study_logs/2026/01/20260101.json", self.records())

    def test_daily_analysis_exposes_custom_category_and_renamed_history(self):
        from app_core.performance_analysis_service import build_daily_feature_frame
        self.configured_records()
        frame = build_daily_feature_frame(date(2026, 1, 1), date(2026, 1, 1), self.root,
                                          as_of_date=date(2026, 1, 2))
        row = frame.iloc[0]
        self.assertEqual(row["study_category_統計学_hours"], 1)
        self.assertEqual(row["study_category_学士医学_hours"], 0.5)
        self.assertIn("prior_28d_study_category_統計学_hours_median", frame)
        self.assertEqual(row["study_hours"], 1.5)

    def test_statistics_exercises_keep_registered_custom_and_alias_names(self):
        from app_core.statistics_practice_service import _load_context
        self.configured_records()
        context = _load_context(date(2026, 1, 1), date(2026, 1, 1), self.root)
        self.assertEqual(set(context.sessions["study_category"]), {"学士医学", "統計学"})

    def test_google_snapshot_shares_registered_names_but_not_arbitrary_legacy_text(self):
        from app_core.google_share_snapshot import build_share_snapshot
        self.configured_records()
        extra = deepcopy(self.records()[0])
        extra.update(session_id="synthetic3", start_at="2026-01-01T12:00:00", end_at="2026-01-01T12:30:00")
        extra["condition"]["category"] = "PRIVATE_UNREGISTERED_TEXT"
        self.put("study_logs/2026/01/20260101.json", [*self.records(), extra])
        snapshot = build_share_snapshot(self.root, date(2026, 1, 1), date(2026, 1, 1))
        rendered = json.dumps(snapshot, ensure_ascii=False)
        self.assertIn("統計学", rendered)
        self.assertIn("学士医学", rendered)
        self.assertNotIn("PRIVATE_UNREGISTERED_TEXT", rendered)

    def test_category_definitions_and_aliases_are_in_local_export(self):
        from app_core.export_service import build_export_bundle
        self.configured_records()
        bundle = build_export_bundle(self.root)
        settings = next(table for table in bundle.tables if table.key == "settings")
        rows = list(csv.DictReader(io.StringIO(settings.data.decode("utf-8-sig"))))
        matching = [row for row in rows if row["settings_file"] == "study_categories.json"]
        self.assertEqual(len(matching), 1)
        self.assertIn("学士医学", json.dumps(matching, ensure_ascii=False))
        self.assertIn("医学", json.dumps(matching, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
