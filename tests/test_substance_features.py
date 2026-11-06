from __future__ import annotations

import csv
from datetime import date, datetime
import io
import json
from pathlib import Path
import tempfile
import unittest

from app_core import diary_service
from app_core import integrity_rules
from app_core import substance_service
from app_core.export_service import build_export_bundle
from app_core.substance_residual_service import (
    required_lookback_days,
    residual_amount_mg,
    substance_residual_total_at,
)


class SubstanceFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.original_diary_dir = diary_service.DIARY_ENTRIES_DIR
        self.original_profiles_path = substance_service.SUBSTANCE_PROFILES_PATH
        diary_service.DIARY_ENTRIES_DIR = self.root / "diary_entries"
        substance_service.SUBSTANCE_PROFILES_PATH = self.root / "settings" / "substance_profiles.json"

    def tearDown(self) -> None:
        diary_service.DIARY_ENTRIES_DIR = self.original_diary_dir
        substance_service.SUBSTANCE_PROFILES_PATH = self.original_profiles_path
        self.temp_dir.cleanup()

    def test_default_profiles_and_model_revision(self) -> None:
        profiles = substance_service.load_substance_profiles(include_inactive=True)
        self.assertEqual(
            [item["substance_id"] for item in profiles[:4]],
            ["caffeine", "eszopiclone", "trazodone", "melatonin"],
        )
        self.assertEqual(
            {
                item["substance_id"]: (item["default_amount_mg"], item["default_label"])
                for item in profiles
            },
            {
                "caffeine": (200.0, "タブレット"),
                "eszopiclone": (2.0, "タブレット"),
                "trazodone": (25.0, "タブレット"),
                "melatonin": (3.0, "タブレット"),
            },
        )
        caffeine = next(item for item in profiles if item["substance_id"] == "caffeine")
        caffeine["color"] = "#112233"
        saved = substance_service.save_substance_profiles(profiles)
        saved_caffeine = next(item for item in saved if item["substance_id"] == "caffeine")
        self.assertEqual(saved_caffeine["model_revision"], 1)

        saved_caffeine["half_life_hours"] = 5.5
        saved_again = substance_service.save_substance_profiles(saved)
        changed = next(item for item in saved_again if item["substance_id"] == "caffeine")
        self.assertEqual(changed["model_revision"], 2)

    def test_legacy_profile_keeps_saved_amount_and_gets_optional_label_without_write(self) -> None:
        profiles = substance_service.default_substance_profiles()
        for profile in profiles:
            profile.pop("default_label")
        profiles[0]["default_amount_mg"] = 75.0
        path = substance_service.SUBSTANCE_PROFILES_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"profiles": profiles}, ensure_ascii=False), encoding="utf-8")
        before = path.read_bytes()

        loaded = substance_service.substance_profile_map()
        self.assertEqual(loaded["caffeine"]["default_amount_mg"], 75.0)
        self.assertTrue(all(item["default_label"] == "タブレット" for item in loaded.values()))
        self.assertEqual(path.read_bytes(), before)

    def test_input_defaults_persist_without_changing_records_or_model(self) -> None:
        target = date(2026, 8, 24)
        for hour, label in ((8, "コーヒー"), (12, "")):
            substance_service.add_substance_record(
                target, "caffeine", 100,
                taken_at=datetime(2026, 8, 24, hour, 0), label=label,
            )
        path = diary_service.get_diary_entry_path(target)
        before_bytes = path.read_bytes()
        before_records = diary_service.load_diary_entry(target)["substance_records"]
        before_model = substance_service.model_snapshot_for_profile(
            substance_service.substance_profile_map()["caffeine"]
        )
        saved = substance_service.upsert_substance_profile(
            {"substance_id": "caffeine", "default_amount_mg": 225.0, "default_label": "カプセル"}
        )
        reloaded = substance_service.substance_profile_map()["caffeine"]
        self.assertEqual(reloaded["default_amount_mg"], 225.0)
        self.assertEqual(reloaded["default_label"], "カプセル")
        self.assertEqual(substance_service.model_snapshot_for_profile(saved), before_model)
        self.assertEqual(diary_service.load_diary_entry(target)["substance_records"], before_records)
        self.assertEqual(path.read_bytes(), before_bytes)

        substance_service.upsert_substance_profile({"substance_id": "caffeine", "default_label": ""})
        self.assertEqual(substance_service.substance_profile_map()["caffeine"]["default_label"], "")

    def test_custom_profile_accepts_default_label(self) -> None:
        created = substance_service.create_substance_profile(
            "追加項目", default_amount_mg=1.5, default_label="  タブレット  "
        )
        reloaded = substance_service.substance_profile_map()[created["substance_id"]]
        self.assertEqual(reloaded["default_amount_mg"], 1.5)
        self.assertEqual(reloaded["default_label"], "タブレット")
        blank = substance_service.create_substance_profile("ラベル未設定")
        self.assertEqual(blank["default_label"], "")

    def test_integrity_allows_missing_or_empty_default_label_but_rejects_invalid_type(self) -> None:
        profiles = substance_service.default_substance_profiles()
        profiles[0].pop("default_label")
        profiles[1]["default_label"] = ""
        audit = integrity_rules.Audit()
        integrity_rules._validate_substances(audit, {"substance_profiles": profiles})
        self.assertEqual(audit.issues, [])

        profiles[2]["default_label"] = 123
        audit = integrity_rules.Audit()
        integrity_rules._validate_substances(audit, {"substance_profiles": profiles})
        self.assertEqual(len(audit.issues), 1)
        self.assertIn("製品・剤形の初期値", audit.issues[0]["内容"])

    def test_profile_validation_rejects_duplicate_names_and_invalid_cutoff(self) -> None:
        profiles = substance_service.default_substance_profiles()
        profiles[1]["name"] = profiles[0]["name"]
        with self.assertRaisesRegex(ValueError, "すでに使われています"):
            substance_service.save_substance_profiles(profiles)

        profiles = substance_service.default_substance_profiles()
        profiles[0]["peak_delay_minutes"] = 120
        profiles[0]["residual_cutoff_hours"] = 2
        with self.assertRaisesRegex(ValueError, "ピーク到達時間より長く"):
            substance_service.save_substance_profiles(profiles)

    def test_legacy_caffeine_is_adapted_without_read_time_write(self) -> None:
        target = date(2026, 8, 24)
        path = diary_service.get_diary_entry_path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = {
            "date": target.isoformat(),
            "title": "legacy",
            "caffeine_mg": 125,
            "caffeine_untimed_mg": 25,
            "caffeine_records": [
                {
                    "caffeine_id": "legacy_caffeine_1",
                    "taken_at": "2026-08-24T08:00:00",
                    "amount_mg": 100,
                    "source": "legacy",
                    "label": "コーヒー",
                    "memo": "",
                }
            ],
        }
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        before = path.read_bytes()

        loaded = diary_service.load_diary_entry(target)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(loaded["substance_records"]), 1)
        self.assertEqual(loaded["substance_records"][0]["substance_id"], "caffeine")
        self.assertEqual(loaded["substance_records"][0]["model_snapshot"]["half_life_hours"], 5.0)

        diary_service.save_diary_entry(target, {"title": "migrated"})
        migrated = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["substance_records_schema_version"], 1)
        self.assertEqual(migrated["substance_records"][0]["substance_record_id"], "legacy_caffeine_1")
        self.assertEqual(migrated["caffeine_records"][0]["caffeine_id"], "legacy_caffeine_1")
        self.assertEqual(migrated["caffeine_mg"], 125)

    def test_decimal_substance_and_caffeine_compatibility_mirror(self) -> None:
        target = date(2026, 8, 24)
        substance_service.add_substance_record(
            target,
            "eszopiclone",
            1.5,
            taken_at=datetime(2026, 8, 24, 22, 30),
            label="錠剤",
        )
        substance_service.add_substance_record(
            target,
            "caffeine",
            100,
            taken_at=datetime(2026, 8, 24, 8, 0),
            label="コーヒー",
        )
        entry = diary_service.load_diary_entry(target)
        totals = {
            item_id: substance_service.substance_records_total(
                substance_service.records_for_substance(entry, item_id)
            )
            for item_id in ("caffeine", "eszopiclone")
        }
        self.assertEqual(totals["eszopiclone"], 1.5)
        self.assertEqual(totals["caffeine"], 100.0)
        self.assertEqual(entry["caffeine_mg"], 100.0)
        self.assertEqual(len(entry["caffeine_records"]), 1)
        caffeine_generic = next(
            item for item in entry["substance_records"] if item["substance_id"] == "caffeine"
        )
        self.assertEqual(caffeine_generic["substance_record_id"], entry["caffeine_records"][0]["caffeine_id"])

        substance_service.replace_substance_records_for_day(
            target,
            "caffeine",
            substance_service.records_for_substance(entry, "caffeine"),
            caffeine_untimed_mg=12.5,
        )
        reloaded = diary_service.load_diary_entry(target)
        self.assertEqual(reloaded["caffeine_untimed_mg"], 12.5)
        self.assertEqual(reloaded["caffeine_mg"], 112.5)

    def test_archived_profile_allows_existing_edits_but_rejects_additions(self) -> None:
        target = date(2026, 8, 24)
        substance_service.add_substance_record(
            target,
            "eszopiclone",
            1.5,
            taken_at=datetime(2026, 8, 24, 22, 30),
        )
        profiles = substance_service.load_substance_profiles(include_inactive=True)
        for profile in profiles:
            if profile["substance_id"] == "eszopiclone":
                profile["is_active"] = False
        substance_service.save_substance_profiles(profiles)

        entry = diary_service.load_diary_entry(target)
        existing = substance_service.records_for_substance(entry, "eszopiclone")
        existing[0]["amount_mg"] = 1.0
        updated = substance_service.replace_substance_records_for_day(target, "eszopiclone", existing)
        self.assertEqual(
            substance_service.records_for_substance(updated, "eszopiclone")[0]["amount_mg"],
            1.0,
        )
        with self.assertRaisesRegex(ValueError, "アーカイブ中"):
            substance_service.add_substance_record(
                target,
                "eszopiclone",
                0.5,
                taken_at=datetime(2026, 8, 24, 23, 0),
            )
        with self.assertRaisesRegex(ValueError, "アーカイブ中"):
            substance_service.replace_substance_records_for_day(
                target,
                "eszopiclone",
                [
                    *existing,
                    {
                        **existing[0],
                        "substance_record_id": "new_archived_record",
                        "taken_at": "2026-08-24T23:00:00",
                    },
                ],
            )

    def test_residual_model_boundaries_and_record_snapshots(self) -> None:
        self.assertGreaterEqual(
            required_lookback_days(
                [{"residual_cutoff_hours": 8}],
                past_view_days=6,
                max_display_half_span_hours=36,
            ),
            38,
        )
        self.assertEqual(
            residual_amount_mg(2, -0.1, half_life_hours=6, peak_delay_minutes=60, residual_cutoff_hours=48),
            0,
        )
        self.assertAlmostEqual(
            residual_amount_mg(2, 1, half_life_hours=6, peak_delay_minutes=60, residual_cutoff_hours=48),
            2,
        )
        self.assertAlmostEqual(
            residual_amount_mg(2, 7, half_life_hours=6, peak_delay_minutes=60, residual_cutoff_hours=48),
            1,
        )
        self.assertEqual(
            residual_amount_mg(2, 48, half_life_hours=6, peak_delay_minutes=60, residual_cutoff_hours=48),
            0,
        )
        record = {
            "taken_at": "2026-08-24T20:00:00",
            "amount_mg": 2,
            "model_snapshot": {
                "half_life_hours": 6,
                "peak_delay_minutes": 60,
                "residual_cutoff_hours": 48,
            },
        }
        current_profile = {
            "half_life_hours": 2,
            "peak_delay_minutes": 0,
            "residual_cutoff_hours": 12,
            "model_revision": 2,
        }
        self.assertAlmostEqual(
            substance_residual_total_at([record], datetime(2026, 8, 25, 3, 0), profile=current_profile),
            1,
        )

    def test_export_contains_substance_tables(self) -> None:
        profiles = substance_service.load_substance_profiles(include_inactive=True)
        substance_service.save_substance_profiles(profiles)
        substance_service.add_substance_record(
            date(2026, 8, 24),
            "melatonin",
            0.5,
            taken_at=datetime(2026, 8, 24, 22, 0),
        )
        bundle = build_export_bundle(self.root, generated_at=datetime(2026, 8, 24, 23, 0))
        tables = {item.key: item for item in bundle.tables}
        self.assertEqual(tables["substance_profiles"].row_count, 4)
        self.assertIn("default_label", tables["substance_profiles"].columns)
        self.assertEqual(tables["substance_records"].row_count, 1)
        self.assertIn("model_snapshot.half_life_hours", tables["substance_records"].columns)

    def test_export_includes_virtual_default_profiles_before_first_settings_save(self) -> None:
        bundle = build_export_bundle(self.root, generated_at=datetime(2026, 8, 24, 23, 0))
        table = next(item for item in bundle.tables if item.key == "substance_profiles")
        self.assertEqual(table.row_count, 4)
        rows = list(csv.DictReader(io.StringIO(table.data.decode("utf-8-sig"))))
        self.assertEqual(
            {row["substance_id"]: float(row["default_amount_mg"]) for row in rows},
            {"caffeine": 200.0, "eszopiclone": 2.0, "trazodone": 25.0, "melatonin": 3.0},
        )
        self.assertTrue(all(row["default_label"] == "タブレット" for row in rows))
        self.assertFalse(substance_service.SUBSTANCE_PROFILES_PATH.exists())


if __name__ == "__main__":
    unittest.main()
