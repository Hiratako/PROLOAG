from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core import json_store
from app_core import learning_context_service as context


class LearningContextServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag_learning_context_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "settings/learning_context.json"
        self.master_path = self.root / "master_data/master.json"
        self.master = {"projects": [{"project_id": "p", "name": "編入"}],
                       "subjects": [{"subject_id": "s", "name": "生命科学"}],
                       "materials": [{"material_id": "m", "name": "同名教材", "project_id": "p", "subject_id": "s"},
                                     {"material_id": "inactive", "name": "同名教材", "is_active": False}],
                       "unknown": {"keep": "master"}}
        self.put(self.master_path, self.master)
        original_read = json_store._read

        def isolated_read(path):
            self.assertTrue(Path(path).resolve().is_relative_to(self.root), f"Read outside fixture: {path}")
            return original_read(path)

        for patcher in (patch.object(json_store, "PROJECT_ROOT", self.root), patch.object(json_store, "_read", side_effect=isolated_read)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.original_read = original_read

    def put(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\ufeff" + json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def entry(self, identifier="m", **changes):
        result = {"material_id": identifier, "next_step": "次は問3から", "links": [
            {"link_id": "link_original", "title": "参考資料", "url": "https://docs.google.com/document/d/fixture/edit", "unknown_link": [1]}],
                  "created_at": "2026-09-16T01:02:03+09:00", "updated_at": "2026-09-16T01:02:03+09:00", "unknown_entry": [2]}
        result.update(changes)
        return result

    def seed(self):
        data = {"schema_version": 1, "materials": {"m": self.entry(), "orphan": self.entry("orphan")}, "unknown_top": [3]}
        self.put(self.path, data)
        return data

    def test_missing_read_creates_no_files_and_material_names_are_unambiguous(self):
        before = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(context.load_learning_context(self.root), {"schema_version": 1, "materials": {}})
        materials = context.list_materials(self.root)
        self.assertEqual(len({item["label"] for item in materials}), 2)
        self.assertIn("生命科学", next(item["label"] for item in materials if item["material_id"] == "m"))
        self.assertIn("無効", next(item["label"] for item in materials if item["material_id"] == "inactive"))
        self.assertEqual(before, {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()})

    def test_google_resource_url_allowlist(self):
        accepted = ["https://docs.google.com/document/d/Ab_123-foo/edit", "https://docs.google.com/spreadsheets/d/abc/edit#gid=0",
                    "https://docs.google.com/presentation/d/abc/edit?usp=sharing", "https://docs.google.com/document/u/2/d/abc/edit",
                    "https://docs.google.com/document/d/e/published/pub", "https://drive.google.com/file/d/abc/view?resourcekey=0-a",
                    "https://drive.google.com/drive/folders/abc", "https://drive.google.com/drive/u/0/folders/abc",
                    "https://drive.google.com/open?id=abc", "https://docs.google.com:443/document/d/abc/edit"]
        for value in accepted:
            with self.subTest(url=value):
                self.assertEqual(context.normalize_resource_url("  " + value + "  "), value)
                self.assertTrue(context.is_valid_google_resource_url(value))
        rejected = [None, "", "http://docs.google.com/document/d/abc", "javascript:alert(1)", "file:///etc/passwd",
                    "https://docs.google.com.evil.test/document/d/abc", "https://evil.test/?docs.google.com/document/d/abc",
                    "https://user:password@docs.google.com/document/d/abc", "https://@docs.google.com/document/d/abc",
                    "https://docs.google.com:444/document/d/abc", "https://docs.google.com./document/d/abc",
                    "https://docs.google.com/url?q=https://evil.test", "https://drive.google.com/open?id=a&id=b",
                    "https://docs.google.com/document/d/abc/../../url", "https://docs.google.com/document/d/abc/%2e%2e/url",
                    "https://docs.google.com/document/d/a\nbc", "https://docs.google.com\\@evil.test/document/d/abc",
                    "https://drive.google.com/uc?id=abc", "https://docs.google.com/forms/d/abc", "https://drive.google.com/open?id="]
        for value in rejected:
            with self.subTest(url=value), self.assertRaises(context.LearningContextError):
                context.normalize_resource_url(value)

    def test_save_preserves_unknown_fields_orphans_original_ids_and_master_bytes(self):
        original = self.seed()
        before_master = self.master_path.read_bytes(), self.master_path.stat().st_mtime_ns
        snapshot = context.load_context_snapshot(self.root)
        saved = context.save_material_context(self.root, "m", [
            {"link_id": "link_original", "title": "変更名", "url": "https://docs.google.com/document/d/new/edit"},
            {"title": "追加", "url": "https://drive.google.com/file/d/new/view"}], "次は問4から", expected_revision=snapshot["revision"])
        data = context.load_learning_context(self.root)
        self.assertEqual(data["unknown_top"], [3])
        self.assertEqual(data["materials"]["orphan"], original["materials"]["orphan"])
        self.assertEqual(saved["unknown_entry"], [2])
        self.assertEqual(saved["links"][0]["unknown_link"], [1])
        self.assertEqual(saved["links"][0]["link_id"], "link_original")
        self.assertTrue(saved["links"][1]["link_id"].startswith("link_"))
        self.assertEqual(saved["created_at"], original["materials"]["m"]["created_at"])
        self.assertEqual(before_master, (self.master_path.read_bytes(), self.master_path.stat().st_mtime_ns))

    def test_two_snapshots_context_conflict_does_not_overwrite(self):
        first, stale = context.load_context_snapshot(self.root), context.load_context_snapshot(self.root)
        context.save_material_context(self.root, "m", [], "最初の保存", expected_revision=first["revision"])
        before = self.path.read_bytes()
        with self.assertRaises(json_store.JsonConflictError):
            context.save_material_context(self.root, "m", [], "古い編集", expected_revision=stale["revision"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_master_change_since_edit_is_a_conflict_even_if_id_still_exists(self):
        snapshot = context.load_context_snapshot(self.root)
        self.master["materials"][0]["name"] = "別画面で改名"
        self.put(self.master_path, self.master)
        with self.assertRaises(json_store.JsonConflictError):
            context.save_material_context(self.root, "m", [], "入力保持対象", expected_revision=snapshot["revision"])
        self.assertFalse(self.path.exists())

    def test_master_raw_race_at_commit_is_rejected_without_context_write(self):
        snapshot = context.load_context_snapshot(self.root)
        original_save = json_store.save_json_safe

        def concurrent_master(path, data, **kwargs):
            if Path(path) == self.path:
                changed = deepcopy(self.master)
                changed["materials"] = []
                self.put(self.master_path, changed)
            return original_save(path, data, **kwargs)

        with patch.object(json_store, "save_json_safe", side_effect=concurrent_master):
            with self.assertRaises(json_store.JsonConflictError):
                context.save_material_context(self.root, "m", [], "race", expected_revision=snapshot["revision"])
        self.assertFalse(self.path.exists())

    def test_inactive_is_editable_orphan_is_readable_but_not_savable(self):
        self.seed()
        snapshot = context.load_context_snapshot(self.root)
        with self.assertRaises(context.LearningContextError):
            context.save_material_context(self.root, "orphan", [], "残す", expected_revision=snapshot["revision"])
        saved = context.save_material_context(self.root, "inactive", [], "無効教材のメモ", expected_revision=snapshot["revision"])
        self.assertEqual(saved["next_step"], "無効教材のメモ")
        self.assertIn("orphan", context.load_learning_context(self.root)["materials"])

    def test_invalid_record_and_json_fail_without_repair(self):
        for data in ({}, {"schema_version": True, "materials": {}}, {"schema_version": 1, "materials": []},
                     {"schema_version": 1, "materials": {"m": self.entry(material_id="wrong")}},
                     {"schema_version": 1, "materials": {"m": self.entry(updated_at="2026-09-16")}},
                     {"schema_version": 1, "materials": {"m": self.entry(updated_at="2026-09-15T00:00:00+09:00")}},
                     {"schema_version": 1, "materials": {"m": self.entry(links=[{"link_id": "x", "title": "", "url": "javascript:alert(1)"}])}}):
            with self.subTest(data=data):
                self.put(self.path, data)
                before = self.path.read_bytes()
                with self.assertRaises(context.LearningContextError):
                    context.load_learning_context(self.root)
                self.assertEqual(self.path.read_bytes(), before)
        self.path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
        with self.assertRaises(json_store.JsonReadError):
            context.load_learning_context(self.root)

    def test_invalid_save_input_and_foreign_link_id_do_not_write(self):
        self.seed()
        snapshot = context.load_context_snapshot(self.root)
        before = self.path.read_bytes()
        for links in ([{"title": "bad", "url": "https://evil.test"}],
                      [{"link_id": "missing", "title": "", "url": "https://docs.google.com/document/d/abc"}],
                      [self.entry()["links"][0]] * 2):
            with self.subTest(links=links), self.assertRaises(context.LearningContextError):
                context.save_material_context(self.root, "m", links, "memo", expected_revision=snapshot["revision"])
            self.assertEqual(self.path.read_bytes(), before)

    def test_commit_failure_rolls_back_byte_exact(self):
        self.seed()
        before = self.path.read_bytes()
        snapshot = context.load_context_snapshot(self.root)
        original_save = json_store.save_json_safe

        def fail_live(path, data, **kwargs):
            if Path(path) == self.path and json_store._active_operation.get() is None:
                original_save(path, data, **kwargs)
                raise OSError("synthetic disk failure")
            return original_save(path, data, **kwargs)

        with patch.object(json_store, "save_json_safe", side_effect=fail_live):
            with self.assertRaises(json_store.JsonStoreError):
                context.save_material_context(self.root, "m", [], "not saved", expected_revision=snapshot["revision"])
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
