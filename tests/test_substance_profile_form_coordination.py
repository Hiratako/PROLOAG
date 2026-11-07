from __future__ import annotations

import ast
from contextvars import Context
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import config
import prolog_ui.components as ab
from app_core import json_store, substance_service


PROJECT = Path(__file__).resolve().parents[1]


class SubstanceProfileFormCoordinationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit import config as streamlit_config
        streamlit_config.get_config_options(options_from_flags={"global.developmentMode": False, "browser.gatherUsageStats": False})

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag-profile-forms-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "settings" / "substance_profiles.json"
        original_read = json_store._read

        def isolated_read(path):
            self.assertTrue(Path(path).resolve().is_relative_to(self.root), str(path))
            return original_read(path)

        for patcher in (
            patch.object(json_store, "PROJECT_ROOT", self.root),
            patch.object(config, "SUBSTANCE_PROFILES_PATH", self.path),
            patch.object(substance_service, "SUBSTANCE_PROFILES_PATH", self.path),
            patch.object(json_store, "_read", side_effect=isolated_read),
            patch.object(ab, "queue_success"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        substance_service.save_substance_profiles(substance_service.load_substance_profiles())

    def app(self):
        from streamlit.testing.v1 import AppTest
        page = PROJECT / "pages" / "98_ユーザー設定.py"
        tree = ast.parse(page.read_text(encoding="utf-8-sig"))
        nodes = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
        start = next(index for index, node in enumerate(tree.body) if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == "profiles" for target in node.targets))
        code = ast.unparse(ast.Module(body=nodes, type_ignores=[])) + "\npage_rerun_requested = False\n"
        code += ast.unparse(ast.Module(body=tree.body[start:], type_ignores=[]))
        app = AppTest.from_string(code, default_timeout=30).run()
        self.assertFalse(app.exception, str(app.exception))
        return app

    def save(self, app, identifier, version=0):
        app.button(key=f"FormSubmitter:substance_profile_form_{identifier}_{version}-この成分設定を保存").click().run()
        self.assertFalse(app.exception, str(app.exception))

    def test_saving_first_profile_keeps_second_draft_and_allows_its_save(self):
        app = self.app()
        app.number_input(key="substance_default_caffeine_0").set_value(250.0)
        app.number_input(key="substance_default_acetaminophen_0").set_value(350.0)
        self.save(app, "caffeine")
        self.assertFalse(app.error, str(app.error))
        self.assertEqual(app.number_input(key="substance_default_acetaminophen_0").value, 350.0)
        self.save(app, "acetaminophen")
        self.assertFalse(app.error, str(app.error))
        saved = substance_service.substance_profile_map()
        self.assertEqual(saved["caffeine"]["default_amount_mg"], 250.0)
        self.assertEqual(saved["acetaminophen"]["default_amount_mg"], 350.0)

    def test_external_edit_is_rejected_and_draft_remains(self):
        app = self.app()
        app.number_input(key="substance_default_acetaminophen_0").set_value(350.0)
        Context().run(lambda: substance_service.upsert_substance_profile({"substance_id": "caffeine", "default_label": "外部変更"}))
        before = self.path.read_bytes()
        self.save(app, "acetaminophen")
        self.assertTrue(app.error)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(app.number_input(key="substance_default_acetaminophen_0").value, 350.0)

    def test_same_profile_external_change_cannot_be_overwritten(self):
        app = self.app()
        app.number_input(key="substance_default_acetaminophen_0").set_value(350.0)
        Context().run(lambda: substance_service.upsert_substance_profile({"substance_id": "acetaminophen", "default_amount_mg": 400.0}))
        before = self.path.read_bytes()
        self.save(app, "acetaminophen")
        self.assertTrue(app.error)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(app.number_input(key="substance_default_acetaminophen_0").value, 350.0)

    def test_refreshing_one_form_does_not_authorize_another_stale_draft(self):
        app = self.app()
        app.number_input(key="substance_default_acetaminophen_0").set_value(350.0)
        Context().run(lambda: substance_service.upsert_substance_profile({"substance_id": "caffeine", "default_label": "外部変更"}))
        app.button(key="reload_substance_profile:caffeine").click().run()
        app.number_input(key="substance_default_caffeine_1").set_value(225.0)
        self.save(app, "caffeine", version=1)
        self.assertFalse(app.error, str(app.error))
        before = self.path.read_bytes()
        self.save(app, "acetaminophen")
        self.assertTrue(app.error)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(app.number_input(key="substance_default_acetaminophen_0").value, 350.0)

    def test_adding_profile_advances_only_current_sibling_baselines(self):
        app = self.app()
        app.number_input(key="substance_default_acetaminophen_0").set_value(350.0)
        next(widget for widget in app.text_input if widget.label == "成分名").input("架空成分")
        app.button(key="FormSubmitter:new_substance_profile_form-成分を追加").click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertFalse(app.error, str(app.error))
        self.save(app, "acetaminophen")
        self.assertFalse(app.error, str(app.error))
        self.assertEqual(substance_service.substance_profile_map()["acetaminophen"]["default_amount_mg"], 350.0)


if __name__ == "__main__":
    unittest.main()
