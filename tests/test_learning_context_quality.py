from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core import integrity_rules
from app_core.export_service import build_export_bundle


class LearningContextQualityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag-context-quality-")
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
        patches.enter_context(patch.object(integrity_rules, "DATED_DATASETS", ()))
        self.write("master_data/master.json", {"projects": [], "subjects": [], "units": [], "tags": [],
                                               "materials": [{"material_id": "m1", "name": "架空教材"}]})

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    @staticmethod
    def valid():
        return {"schema_version": 1, "materials": {"m1": {
            "material_id": "m1", "next_step": "第3章から再開",
            "created_at": "2026-09-16T09:00:00+09:00", "updated_at": "2026-09-16T10:00:00+09:00",
            "links": [{"link_id": "link1", "title": "演習ノート", "url": "https://docs.google.com/document/d/synthetic_id/edit"}],
        }}}

    def snapshot(self):
        return {path.relative_to(self.root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob("*") if path.is_file()}

    def audit(self):
        before = self.snapshot()
        result = integrity_rules.run_deep_integrity_checks()
        self.assertEqual(self.snapshot(), before)
        return result

    def context_issues(self, result):
        return [issue for issue in result["issues"] if "learning_context.json" in issue["対象ファイル"]]

    def test_missing_context_keeps_legacy_workspace_unchanged(self):
        self.assertEqual(self.context_issues(self.audit()), [])
        before = self.snapshot()
        bundle = build_export_bundle(self.root)
        table = next(table for table in bundle.tables if table.key == "settings")
        self.assertEqual(table.row_count, 0)
        self.assertEqual(self.snapshot(), before)

    def test_valid_context_and_unknown_fields_are_read_only(self):
        data = self.valid()
        data["future"] = {"nullable": None}
        data["materials"]["m1"]["future"] = [1, {"keep": True}]
        data["materials"]["m1"]["links"][0]["future"] = "keep"
        self.write("settings/learning_context.json", data)
        self.assertEqual(self.context_issues(self.audit()), [])

    def test_schema_and_container_shapes_are_reported(self):
        for data in ([], {"schema_version": True, "materials": {}}, {"schema_version": 2, "materials": {}},
                     {"schema_version": 1, "materials": []}, {"schema_version": 1, "materials": {"m1": []}}):
            with self.subTest(data=data):
                self.write("settings/learning_context.json", data)
                self.assertTrue(any(issue["重要度"] == integrity_rules.ERROR for issue in self.context_issues(self.audit())))

    def test_ids_link_duplicates_and_nonstring_text_are_reported(self):
        data = self.valid()
        entry = data["materials"]["m1"]
        entry["material_id"] = "other"
        entry["next_step"] = ["not text"]
        entry["links"].append(deepcopy(entry["links"][0]))
        entry["links"][1]["title"] = 123
        entry["links"].append({"link_id": "", "url": "https://drive.google.com/open?id=synthetic_id"})
        self.write("settings/learning_context.json", data)
        messages = "\n".join(issue["内容"] for issue in self.context_issues(self.audit()))
        for expected in ("material_id", "next_step", "link_id", "重複", "title"):
            self.assertIn(expected, messages)

    def test_invalid_links_container_and_items_are_reported(self):
        for links in ({}, [None], [False]):
            with self.subTest(links=links):
                data = self.valid()
                data["materials"]["m1"]["links"] = links
                self.write("settings/learning_context.json", data)
                self.assertTrue(self.context_issues(self.audit()))

    def test_deleted_material_is_notice_and_not_automatically_removed(self):
        data = self.valid()
        data["materials"]["deleted"] = data["materials"].pop("m1")
        data["materials"]["deleted"]["material_id"] = "deleted"
        self.write("settings/learning_context.json", data)
        issues = self.context_issues(self.audit())
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["重要度"], integrity_rules.NOTICE)
        self.assertEqual(issues[0]["問題カテゴリ"], "学習コンテキスト参照")

    def test_broken_master_does_not_invent_orphan_context_notices(self):
        self.write("settings/learning_context.json", self.valid())
        self.write("master_data/master.json", [])
        self.assertFalse(any(issue["問題カテゴリ"] == "学習コンテキスト参照" for issue in self.audit()["issues"]))

    def test_bad_and_reverse_timestamps_are_reported(self):
        for created, updated in (("bad", "2026-09-16T10:00:00"),
                                 ("2026-09-17T10:00:00", "2026-09-16T10:00:00"), ("", ""),
                                 ("2026-09-16", "2026-09-16T10:00:00"),
                                 (" 2026-09-16T09:00:00 ", "2026-09-16T10:00:00")):
            with self.subTest(created=created, updated=updated):
                data = self.valid()
                data["materials"]["m1"].update(created_at=created, updated_at=updated)
                self.write("settings/learning_context.json", data)
                self.assertTrue(any(issue["問題カテゴリ"] == "学習コンテキスト日時" for issue in self.context_issues(self.audit())))

    def test_save_contract_limits_and_control_character_ids_are_reported(self):
        cases = (("next_step", "x" * 10001), ("links", [deepcopy(self.valid()["materials"]["m1"]["links"][0]) for _ in range(51)]))
        for field, value in cases:
            with self.subTest(field=field):
                data = self.valid()
                data["materials"]["m1"][field] = value
                self.write("settings/learning_context.json", data)
                self.assertTrue(any(issue["問題カテゴリ"] == "学習コンテキスト構造" for issue in self.context_issues(self.audit())))
        for field, value in (("title", "x" * 201), ("link_id", "line\nbreak"), ("link_id", "x" * 201)):
            with self.subTest(field=field):
                data = self.valid()
                data["materials"]["m1"]["links"][0][field] = value
                self.write("settings/learning_context.json", data)
                self.assertTrue(self.context_issues(self.audit()))

    def test_inactive_material_and_blank_link_title_are_allowed(self):
        self.write("master_data/master.json", {"materials": [{"material_id": "m1", "name": "架空教材", "is_active": False}]})
        data = self.valid()
        data["materials"]["m1"]["links"][0]["title"] = ""
        self.write("settings/learning_context.json", data)
        self.assertEqual(self.context_issues(self.audit()), [])

    def test_resource_urls_use_same_allowlist_as_local_save(self):
        good = ("https://docs.google.com/document/d/synthetic_id/edit", "https://docs.google.com/spreadsheets/d/synthetic_id/edit#gid=0",
                "https://docs.google.com/presentation/d/synthetic_id/edit", "https://drive.google.com/file/d/synthetic_id/view",
                "https://drive.google.com/drive/folders/synthetic_id", "https://drive.google.com/open?id=synthetic_id")
        bad = ("http://docs.google.com/document/d/x", "https://docs.google.com.evil.example/document/d/x",
               "https://user@docs.google.com/document/d/x", "https://docs.google.com:444/document/d/x",
               "https://docs.google.com/redirect?url=https://example.com", "javascript:alert(1)", "https://drive.google.com/", None)
        for url in (*good, *bad):
            with self.subTest(url=url):
                data = self.valid()
                data["materials"]["m1"]["links"][0]["url"] = url
                self.write("settings/learning_context.json", data)
                issues = self.context_issues(self.audit())
                self.assertEqual(any(issue["問題カテゴリ"] == "学習コンテキストURL" for issue in issues), url in bad)

    def test_json_syntax_duplicates_and_nonfinite_values_are_checked(self):
        path = self.write("settings/learning_context.json", self.valid())
        for payload, category in ((b'{broken', "JSON構文"),
                                  (b'{"schema_version":1,"schema_version":2,"materials":{}}', "JSON構文"),
                                  (b'{"schema_version":1,"materials":{},"future":NaN}', "JSON値")):
            with self.subTest(payload=payload):
                path.write_bytes(payload)
                issues = self.context_issues(self.audit())
                self.assertTrue(any(issue["問題カテゴリ"] == category for issue in issues))

    def test_local_export_keeps_context_and_unknown_fields_but_not_auth_or_reports(self):
        data = self.valid()
        data["future"] = {"keep": "future-value"}
        data["materials"]["m1"]["links"][0]["future"] = [1, None]
        self.write("settings/learning_context.json", data)
        for name in ("settings/credentials.json", "settings/google_docs.json", "backups/google_share/pending.json"):
            self.write(name, {"private": "DO_NOT_EXPORT"})
        before = self.snapshot()
        bundle = build_export_bundle(self.root)
        table = next(table for table in bundle.tables if table.key == "settings")
        rows = list(csv.DictReader(io.StringIO(table.data.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["settings_file"], "learning_context.json")
        self.assertEqual(rows[0]["__source_file"], "settings/learning_context.json")
        self.assertEqual(rows[0]["materials.m1.next_step"], "第3章から再開")
        self.assertEqual(json.loads(rows[0]["materials.m1.links"]), data["materials"]["m1"]["links"])
        self.assertEqual(rows[0]["future.keep"], "future-value")
        self.assertFalse(any(b"DO_NOT_EXPORT" in exported.data for exported in bundle.tables))
        self.assertEqual(self.snapshot(), before)

    def test_corrupt_context_export_warns_without_overwriting(self):
        path = self.write("settings/learning_context.json", self.valid())
        path.write_bytes(b'{broken')
        before = self.snapshot()
        bundle = build_export_bundle(self.root)
        self.assertTrue(any("learning_context.json" in warning for warning in bundle.warnings))
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
