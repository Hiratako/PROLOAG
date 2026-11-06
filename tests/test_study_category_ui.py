from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core import json_store
from app_core.study_category_service import load_study_category_catalog, study_category_names, save_study_category


class StudyCategoryUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit import config
        config.get_config_options(options_from_flags={"global.developmentMode": False, "browser.gatherUsageStats": False})

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pgcui_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        patcher = patch.object(json_store, "PROJECT_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def app(self, mode="management"):
        from streamlit.testing.v1 import AppTest
        method = "render_study_category_management" if mode == "management" else "render_study_category_quick_add"
        return AppTest.from_string(
            "from pathlib import Path\n"
            f"from app_core.study_category_ui import {method}\n"
            f"{method}(Path({str(self.root)!r}), 'category_test')\n", default_timeout=30).run()

    def test_management_open_is_read_only(self):
        app = self.app()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.dataframe[0].value), 8)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_quick_add_creates_category_only_on_submit(self):
        app = self.app("quick")
        app.text_input[0].set_value("統計学").run()
        self.assertEqual(list(self.root.iterdir()), [])
        next(button for button in app.button if button.label == "勉強カテゴリを追加").click().run()
        self.assertFalse(app.exception)
        self.assertIn("統計学", study_category_names(load_study_category_catalog(self.root)))
        self.assertEqual(app.text_input[0].value, "")

    def test_management_rename_and_disable_preserve_old_alias(self):
        app = self.app()
        app.selectbox[0].set_value("category_default_1").run()
        app.text_input[0].set_value("学士医学")
        app.checkbox[0].uncheck()
        next(button for button in app.button if button.label == "勉強カテゴリを保存").click().run()
        self.assertFalse(app.exception)
        catalog = load_study_category_catalog(self.root)
        entry = next(item for item in catalog["categories"] if item["category_id"] == "category_default_1")
        self.assertEqual(entry["name"], "学士医学")
        self.assertEqual(entry["aliases"], ["医学"])
        self.assertFalse(entry["is_active"])

    def test_duplicate_name_retains_input_and_does_not_save(self):
        app = self.app("quick")
        app.text_input[0].set_value("医学")
        next(button for button in app.button if button.label == "勉強カテゴリを追加").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.error)
        self.assertEqual(app.text_input[0].value, "医学")
        self.assertFalse((self.root / "settings/study_categories.json").exists())

    def test_add_after_own_rename_does_not_require_reload(self):
        app = self.app()
        app.selectbox[0].set_value("category_default_1").run()
        app.text_input[0].set_value("学士医学")
        next(button for button in app.button if button.label == "勉強カテゴリを保存").click().run()
        app.selectbox[0].set_value("").run()
        app.text_input[0].set_value("統計学")
        next(button for button in app.button if button.label == "勉強カテゴリを保存").click().run()
        self.assertFalse(app.exception)
        self.assertFalse(app.error)
        self.assertIn("統計学", study_category_names(load_study_category_catalog(self.root)))

    def test_external_change_blocks_stale_form_and_retains_draft(self):
        app = self.app("quick")
        save_study_category(self.root, "法律")
        app.text_input[0].set_value("統計学")
        next(button for button in app.button if button.label == "勉強カテゴリを追加").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.error)
        self.assertEqual(app.text_input[0].value, "統計学")
        names = study_category_names(load_study_category_catalog(self.root))
        self.assertIn("法律", names)
        self.assertNotIn("統計学", names)

    def test_quick_add_reload_can_be_clicked_after_a_save_error(self):
        app = self.app("quick")
        app.text_input[0].set_value("医学")
        next(button for button in app.button if button.label == "勉強カテゴリを追加").click().run()
        next(button for button in app.button if button.label == "入力を破棄してカテゴリを再読込").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.text_input[0].value, "")


if __name__ == "__main__":
    unittest.main()
