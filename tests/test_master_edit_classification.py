from __future__ import annotations

import ast
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core import json_store
from app_core.json_store import JsonConflictError, JsonStoreError
from app_core.master_edit_service import reference_choices, validate_classification


PROJECT = Path(__file__).resolve().parents[1]


def fixture():
    data = {"projects": [], "subjects": [], "materials": [], "units": [], "tags": [], "future_top": {"keep": True}}
    for number in range(3):
        p, s, m, u = (f"{prefix}{number}" for prefix in "psmu")
        common = {"name": "同名", "is_active": number != 0}
        data["projects"].append({**common, "project_id": p})
        data["subjects"].append({**common, "subject_id": s, "project_id": p})
        data["materials"].append({**common, "material_id": m, "project_id": p, "subject_id": s})
        data["units"].append({**common, "unit_id": u, "project_id": p, "subject_id": s, "material_id": m, "parent_unit_id": ""})
    for collection, id_key in (("subjects", "subject_id"), ("materials", "material_id"), ("units", "unit_id")):
        item = deepcopy(data[collection][0])
        item.update({id_key: f"{id_key[0]}_edit", "name": "編集対象", "is_active": True, "unknown": {"keep": [1, 2]}})
        if collection == "units":
            item["parent_unit_id"] = "u0"
        data[collection].append(item)
    return data


class MasterClassificationTests(unittest.TestCase):
    def test_choices_keep_inactive_missing_and_blank_ids_without_name_collisions(self):
        data = fixture()
        choices = reference_choices(data, "projects", "project_id", "p0")
        self.assertEqual(set(choices), {"", "p0", "p1", "p2"})
        self.assertIn("無効", choices["p0"])
        self.assertNotEqual(choices["p1"], choices["p2"])
        self.assertIn("未登録", reference_choices(data, "projects", "project_id", "deleted")["deleted"])
        choices = reference_choices(data, "subjects", "subject_id", "s0", parents={"project_id": "p2"})
        self.assertEqual(set(choices), {"", "s0", "s2"})

    def test_unchanged_classification_accepts_inactive_missing_and_legacy_mismatch(self):
        for deleted in (False, True):
            data = fixture()
            if deleted:
                for collection in ("projects", "subjects", "materials", "units"):
                    data[collection] = data[collection][1:]
            for collection in ("subjects", "materials", "units"):
                item = deepcopy(data[collection][-1])
                validate_classification(data, collection, {**item, "name": "名称のみ変更"}, item)

    def test_changed_parent_requires_matching_children_and_explicit_valid_targets(self):
        data = fixture()
        current = data["materials"][-1]
        with self.assertRaises(JsonStoreError):
            validate_classification(data, "materials", {**current, "project_id": "p2"}, current)
        validate_classification(data, "materials", {**current, "project_id": "p2", "subject_id": "s2"}, current)
        with self.assertRaises(JsonStoreError):
            validate_classification(data, "subjects", {"name": "新規", "project_id": ""})

    def test_concurrent_reclassification_or_deletion_is_rejected(self):
        data = fixture()
        current = deepcopy(data["materials"][-1])
        data["materials"][-1]["subject_id"] = "other"
        with self.assertRaises(JsonConflictError):
            validate_classification(data, "materials", current, current)
        data["materials"].pop()
        with self.assertRaises(JsonConflictError):
            validate_classification(data, "materials", current, current)

    def test_parent_unit_scope_and_cycles_are_rejected_without_mutation(self):
        data = fixture()
        original = deepcopy(data["units"][-1])
        before = deepcopy(data)
        with self.assertRaises(JsonStoreError):
            validate_classification(data, "units", {**original, "parent_unit_id": "u2"}, original)
        data["units"].append({**original, "unit_id": "descendant", "parent_unit_id": original["unit_id"]})
        with self.assertRaises(JsonStoreError):
            validate_classification(data, "units", {**original, "parent_unit_id": "descendant"}, original)
        self.assertEqual(data["units"][:-1], before["units"])


class MasterClassificationUITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag-master-edit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        from app_core import settings_service
        settings_path = patch.object(settings_service, "USER_SETTINGS_PATH", self.root / "settings/user_settings.json")
        settings_path.start()
        self.addCleanup(settings_path.stop)
        self.path = self.root / "master_data/master.json"
        self.path.parent.mkdir()
        self.data = fixture()
        self.put()
        original_read = json_store._read

        def isolated_read(path):
            self.assertTrue(Path(path).resolve().is_relative_to(self.root), str(path))
            return original_read(path)

        guard = patch.object(json_store, "_read", side_effect=isolated_read)
        guard.start()
        self.addCleanup(guard.stop)

    def put(self):
        self.path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")

    def saved(self, kind):
        collection = {"subject": "subjects", "material": "materials", "unit": "units"}[kind]
        return next(item for item in json.loads(self.path.read_text(encoding="utf-8"))[collection]
                    if item[f"{kind}_id"] == f"{kind[0]}_edit")

    def page(self, kind, *, selected_label=None):
        from streamlit import config as streamlit_config
        from streamlit.testing.v1 import AppTest

        streamlit_config.get_config_options(options_from_flags={"global.developmentMode": False, "browser.gatherUsageStats": False})
        path = PROJECT / "pages/10_マスタ管理.py"
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        nodes = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef))
                 or isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "MATERIAL_TYPES" for target in node.targets)]
        source = ast.unparse(ast.Module(body=nodes, type_ignores=[]))
        source += f"\nROOT = Path({str(self.root)!r})\nMASTER_SNAPSHOT = MasterSnapshot.load(ROOT)\nrender_{kind}_tab()\n"
        app = AppTest.from_string(source, default_timeout=30).run()
        self.assertEqual(len(app.exception), 0)
        next(item for item in app.selectbox if item.key == f"{kind}_select").select(selected_label or f"編集対象 [{kind[0]}_edit]").run()
        self.assertEqual(len(app.exception), 0)
        return app

    @staticmethod
    def select(app, label, value):
        next(item for item in app.selectbox if item.label == label).select(value).run()

    def save(self, app):
        next(item for item in app.button if item.label == "保存").click().run()
        self.assertEqual(len(app.exception), 0, [item.value for item in app.exception])

    def test_initial_render_is_readonly_and_duplicate_labels_are_distinct(self):
        before = self.path.read_bytes()
        app = self.page("subject")
        self.assertEqual(self.path.read_bytes(), before)
        parent = next(item for item in app.selectbox if item.label == "所属プロジェクト")
        self.assertEqual(parent.value, "p0")
        self.assertTrue(any("無効" in label and "p0" in label for label in parent.options))
        self.assertTrue(any("p1" in label for label in parent.options))
        self.assertTrue(any("p2" in label for label in parent.options))

    def test_switching_target_does_not_transfer_unsaved_name_description_or_active_state(self):
        for kind in ("project", "subject", "material", "unit", "tag"):
            with self.subTest(kind=kind):
                self.data = fixture()
                self.data["tags"] = [{"tag_id": f"t{index}", "name": "同名", "is_active": True} for index in (1, 2)]
                self.put()
                app = self.page(kind, selected_label=f"同名 [{kind[0]}1]")
                app.text_input[0].set_value("別対象の未保存名")
                app.text_area[0].set_value("別対象の未保存説明")
                app.checkbox[0].uncheck()
                app.run()
                self.assertEqual(app.text_input[0].value, "別対象の未保存名")
                self.assertEqual(app.text_area[0].value, "別対象の未保存説明")
                self.assertFalse(app.checkbox[0].value)
                app.selectbox(key=f"{kind}_select").select(f"同名 [{kind[0]}2]").run()
                self.assertFalse(app.exception, str(app.exception))
                self.assertEqual(app.text_input[0].value, "同名")
                self.assertEqual(app.text_area[0].value, "")
                self.assertTrue(app.checkbox[0].value)
                self.save(app)
                saved = json.loads(self.path.read_text(encoding="utf-8"))[f"{kind}s"]
                second = next(item for item in saved if item[f"{kind}_id"] == f"{kind[0]}2")
                self.assertEqual(second["name"], "同名")
                self.assertEqual(second["description"], "")
                self.assertTrue(second["is_active"])

    def test_inactive_parents_survive_unchanged_and_name_only_saves_at_each_level(self):
        for kind in ("subject", "material", "unit"):
            with self.subTest(kind=kind):
                self.data = fixture()
                self.put()
                app = self.page(kind)
                before = self.saved(kind)
                self.save(app)
                for key in ("project_id", "subject_id", "material_id", "parent_unit_id", "unknown"):
                    if key in before:
                        self.assertEqual(self.saved(kind)[key], before[key])
                app.text_input[0].set_value("名称のみ変更")
                self.save(app)
                saved = self.saved(kind)
                self.assertEqual(saved["name"], "名称のみ変更")
                for key in ("project_id", "subject_id", "material_id", "parent_unit_id", "unknown"):
                    if key in before:
                        self.assertEqual(saved[key], before[key])

    def test_deleted_parents_survive_unchanged_and_name_only_saves_at_each_level(self):
        for kind in ("subject", "material", "unit"):
            with self.subTest(kind=kind):
                self.data = fixture()
                for collection in ("projects", "subjects", "materials", "units"):
                    self.data[collection] = self.data[collection][1:]
                self.put()
                app = self.page(kind)
                before = self.saved(kind)
                self.save(app)
                app.text_input[0].set_value("削除済み所属を保持")
                self.save(app)
                saved = self.saved(kind)
                self.assertEqual(saved["name"], "削除済み所属を保持")
                for key in ("project_id", "subject_id", "material_id", "parent_unit_id", "unknown"):
                    if key in before:
                        self.assertEqual(saved[key], before[key])

    def test_explicit_reclassification_updates_only_selected_chain_at_each_level(self):
        for kind in ("subject", "material", "unit"):
            with self.subTest(kind=kind):
                self.data = fixture()
                self.put()
                app = self.page(kind)
                self.select(app, "所属プロジェクト", "p2")
                if kind != "subject":
                    self.select(app, "所属科目", "s2")
                if kind == "unit":
                    self.select(app, "所属教材", "m2")
                    self.select(app, "親単元", "u2")
                self.save(app)
                self.assertEqual(len(app.error), 0)
                saved = self.saved(kind)
                self.assertEqual(saved["project_id"], "p2")
                if kind != "subject":
                    self.assertEqual(saved["subject_id"], "s2")
                if kind == "unit":
                    self.assertEqual((saved["material_id"], saved["parent_unit_id"]), ("m2", "u2"))
                self.assertEqual(saved["unknown"], {"keep": [1, 2]})

    def test_changed_parent_does_not_silently_select_child_and_rejected_save_keeps_text(self):
        app = self.page("material")
        before = self.path.read_bytes()
        self.select(app, "所属プロジェクト", "p2")
        subject = next(item for item in app.selectbox if item.label == "所属科目")
        self.assertEqual(subject.value, "s0")
        self.assertTrue(any("s2" in label for label in subject.options))
        app.text_input[0].set_value("保存失敗でも保持")
        self.save(app)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertGreater(len(app.error), 0)
        self.assertEqual(app.text_input[0].value, "保存失敗でも保持")
        self.select(app, "所属科目", "s2")
        self.save(app)
        self.assertEqual(self.saved("material")["name"], "保存失敗でも保持")

    def test_other_editor_reclassification_is_not_overwritten_on_save(self):
        app = self.page("material")
        self.data["materials"][-1].update(project_id="p2", subject_id="s2")
        self.put()
        before = self.path.read_bytes()
        app.text_input[0].set_value("古い画面の名称")
        self.save(app)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(any("編集中に所属" in item.value for item in app.error))

    def test_existing_blank_parents_remain_blank_on_name_only_save(self):
        for kind in ("subject", "material", "unit"):
            with self.subTest(kind=kind):
                self.data = fixture()
                collection = {"subject": "subjects", "material": "materials", "unit": "units"}[kind]
                item = self.data[collection][-1]
                for key in ("project_id", "subject_id", "material_id", "parent_unit_id"):
                    if key in item and key != f"{kind}_id":
                        item[key] = ""
                self.put()
                app = self.page(kind)
                app.text_input[0].set_value("所属なしを保持")
                self.save(app)
                for key in ("project_id", "subject_id", "material_id", "parent_unit_id"):
                    if key in item and key != f"{kind}_id":
                        self.assertEqual(self.saved(kind)[key], "")

    def test_parent_unit_can_be_explicitly_cleared_without_changing_other_ids(self):
        app = self.page("unit")
        before = self.saved("unit")
        self.select(app, "親単元", "")
        self.save(app)
        saved = self.saved("unit")
        self.assertEqual(saved["parent_unit_id"], "")
        for key in ("project_id", "subject_id", "material_id", "unknown"):
            self.assertEqual(saved[key], before[key])

    def test_full_page_composition_uses_only_fixture_and_retains_parent_ids(self):
        from streamlit import config as streamlit_config
        from streamlit.testing.v1 import AppTest
        import prolog_ui.components as components

        streamlit_config.get_config_options(options_from_flags={"global.developmentMode": False, "browser.gatherUsageStats": False})
        source = (PROJECT / "pages/10_マスタ管理.py").read_text(encoding="utf-8-sig")
        source = source.replace("ROOT = Path(__file__).resolve().parents[1]", f"ROOT = Path({str(self.root)!r})")
        before = self.path.read_bytes()
        with patch.object(components, "inject_css"), patch.object(components, "sidemenu"), patch.object(components, "topbar"):
            app = AppTest.from_string(source, default_timeout=30).run()
            self.assertEqual(len(app.exception), 0, [item.value for item in app.exception])
            self.assertEqual(self.path.read_bytes(), before)
            next(item for item in app.selectbox if item.key == "unit_select").select("編集対象 [u_edit]").run()
            self.assertEqual(len(app.exception), 0)
            next(item for item in app.button if item.key == "FormSubmitter:unit_form_u_edit-保存").click().run()
        self.assertEqual(len(app.exception), 0, [item.value for item in app.exception])
        self.assertEqual(self.saved("unit")["parent_unit_id"], "u0")


if __name__ == "__main__":
    unittest.main()
