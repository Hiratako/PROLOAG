from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import config as app_config
from app_core import app_clock, json_store, personalization_ui, settings_edit_service, settings_service, substance_service
from app_core.form_snapshot import form_snapshot


class CaffeineMissingSettingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag_caffeine_setting_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "settings/user_settings.json"
        self.profiles_path = self.root / "settings/substance_profiles.json"
        original_read = json_store._read

        def isolated_read(path):
            self.assertTrue(Path(path).resolve().is_relative_to(self.root), f"Out-of-fixture JSON read: {path}")
            return original_read(path)

        for patcher in (
            patch.object(app_config, "PROJECT_ROOT", self.root),
            patch.object(app_config, "USER_SETTINGS_PATH", self.path),
            patch.object(app_config, "SUBSTANCE_PROFILES_PATH", self.profiles_path),
            patch.object(json_store, "PROJECT_ROOT", self.root),
            patch.object(json_store, "_read", side_effect=isolated_read),
            patch.object(settings_service, "USER_SETTINGS_PATH", self.path),
            patch.object(settings_edit_service, "USER_SETTINGS_PATH", self.path),
            patch.object(substance_service, "SUBSTANCE_PROFILES_PATH", self.profiles_path),
            patch.object(app_clock, "DEMO_MODE_PATH", self.root / "settings/demo_mode.json"),
            patch.object(substance_service, "load_substance_profiles", return_value=[]),
            patch.object(personalization_ui, "render_personalization_settings", return_value=False),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(value, ensure_ascii=False, indent=3), encoding="utf-8")

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_snapshot_argument_is_strict_bool_read_only_and_does_not_reload(self):
        cases = [({}, True), ({"analysis": {}}, True), ({"analysis": None}, True),
                 ({"analysis": "invalid"}, True), ({"analysis": {"caffeine_missing_as_zero": False}}, False),
                 ({"analysis": {"caffeine_missing_as_zero": True}}, True)]
        cases.extend(({"analysis": {"caffeine_missing_as_zero": value}}, True)
                     for value in (None, 0, 1, "false", "true", "", [], {}))
        with patch.object(settings_service, "load_user_settings", side_effect=AssertionError("Snapshot must not reread settings")):
            for settings, expected in cases:
                with self.subTest(settings=settings):
                    before = deepcopy(settings)
                    self.assertIs(settings_service.should_treat_missing_caffeine_as_zero(settings), expected)
                    self.assertEqual(settings, before)

    def test_missing_settings_default_true_without_creating_a_file(self):
        self.assertTrue(settings_service.DEFAULT_USER_SETTINGS["analysis"]["caffeine_missing_as_zero"])
        self.assertTrue(settings_service.should_treat_missing_caffeine_as_zero())
        self.assertTrue(settings_service.load_user_settings()["analysis"]["caffeine_missing_as_zero"])
        self.assertFalse(self.path.exists())

    def test_explicit_false_survives_loading_without_normalizing_file(self):
        self.write({"analysis": {"caffeine_missing_as_zero": False, "unknown": {"keep": [1, None]}}})
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        self.assertFalse(settings_service.should_treat_missing_caffeine_as_zero())
        self.assertFalse(settings_service.load_user_settings()["analysis"]["caffeine_missing_as_zero"])
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), before)

    def test_policy_patch_preserves_other_settings_and_unknown_analysis_fields(self):
        self.write({"analysis": {"unknown": {"keep": [1, 2]}},
                    "timer": {"hourly_rate": 3400, "opaque": True},
                    "countdown": {"enabled": True, "title": "keep"}, "future": {"keep": "value"}})
        settings_service.load_user_settings()
        settings_service.update_user_settings({"analysis": {"caffeine_missing_as_zero": False}})
        saved = self.read()
        self.assertIs(saved["analysis"]["caffeine_missing_as_zero"], False)
        self.assertEqual(saved["analysis"]["unknown"], {"keep": [1, 2]})
        self.assertEqual(saved["timer"]["hourly_rate"], 3400)
        self.assertIs(saved["timer"]["opaque"], True)
        self.assertEqual(saved["countdown"]["title"], "keep")
        self.assertEqual(saved["future"], {"keep": "value"})

    def test_own_sibling_setting_save_advances_analysis_baseline_without_losing_draft(self):
        self.write({"analysis": {"caffeine_missing_as_zero": True, "unknown": [1, 2]}})
        state = {}
        loaded = settings_service.load_user_settings()
        general = form_snapshot(state, "user_settings_form", [self.path], loaded)
        countdown = form_snapshot(state, "countdown_settings_form", [self.path], loaded)
        state["settings_caffeine_missing_as_zero_0"] = False
        settings_edit_service.save_settings_form_patch(
            state, "countdown_settings_form", countdown,
            {"countdown": {"enabled": True, "title": "owned sibling save", "target_date": "2027-01-01"}},
        )
        advanced = form_snapshot(state, "user_settings_form", [self.path], settings_service.load_user_settings())
        self.assertEqual(advanced["version"], general["version"])
        self.assertEqual(advanced["operation_id"], general["operation_id"])
        self.assertIs(state["settings_caffeine_missing_as_zero_0"], False)
        settings_edit_service.save_settings_form_patch(
            state, "user_settings_form", advanced,
            {"analysis": {"caffeine_missing_as_zero": state["settings_caffeine_missing_as_zero_0"]}},
        )
        saved = self.read()
        self.assertIs(saved["analysis"]["caffeine_missing_as_zero"], False)
        self.assertEqual(saved["analysis"]["unknown"], [1, 2])
        self.assertEqual(saved["countdown"]["title"], "owned sibling save")

    def page(self):
        from streamlit import config as streamlit_config
        from streamlit.testing.v1 import AppTest

        streamlit_config.get_config_options(options_from_flags={
            "global.developmentMode": False, "browser.gatherUsageStats": False,
        })
        source = (Path(__file__).resolve().parents[1] / "pages/98_ユーザー設定.py").read_text(encoding="utf-8-sig")
        source = source.replace("import prolog_ui.components as ab", """from prolog_ui.components import show_action_error as _show_action_error
class _Decoration:
    show_action_error = staticmethod(_show_action_error)
    def __getattr__(self, name):
        return lambda *args, **kwargs: None
ab = _Decoration()""")
        return AppTest.from_string(source, default_timeout=30)

    @staticmethod
    def policy(app):
        return next(item for item in app.checkbox if item.label == "カフェイン未記録日を0mgとして分析する")

    @staticmethod
    def save(app):
        return next(item for item in app.button if item.label == "設定を保存")

    def assert_no_exception(self, app):
        self.assertEqual(len(app.exception), 0, [item.value for item in app.exception])

    def test_actual_ui_defaults_true_and_only_writes_policy_on_save(self):
        app = self.page().run()
        self.assert_no_exception(app)
        self.assertTrue(self.policy(app).value)
        self.assertIn("未来日", self.policy(app).proto.help)
        self.assertFalse(self.path.exists())
        self.policy(app).uncheck().run()
        self.assertFalse(self.path.exists())
        self.save(app).click().run()
        self.assert_no_exception(app)
        self.assertIs(self.read()["analysis"]["caffeine_missing_as_zero"], False)
        self.assertFalse(self.policy(app).value)
        self.policy(app).check()
        self.save(app).click().run()
        self.assert_no_exception(app)
        self.assertIs(self.read()["analysis"]["caffeine_missing_as_zero"], True)

    def test_actual_ui_save_preserves_unknown_fields_and_other_sections(self):
        self.write({"analysis": {"caffeine_missing_as_zero": False, "unknown": {"keep": [1, None]}},
                    "timer": {"hourly_rate": 3400, "opaque": True},
                    "display": {"show_task_metrics": False, "hidden_sidebar_pages": ["keep.py"], "unknown": 4},
                    "countdown": {"enabled": True, "title": "do not replace"}, "future": {"keep": "yes"}})
        app = self.page().run()
        self.assert_no_exception(app)
        self.assertFalse(self.policy(app).value)
        self.policy(app).check()
        self.save(app).click().run()
        self.assert_no_exception(app)
        saved = self.read()
        self.assertTrue(saved["analysis"]["caffeine_missing_as_zero"])
        self.assertEqual(saved["analysis"]["unknown"], {"keep": [1, None]})
        self.assertEqual(saved["timer"]["hourly_rate"], 3400)
        self.assertIs(saved["timer"]["opaque"], True)
        self.assertFalse(saved["display"]["show_task_metrics"])
        self.assertEqual(saved["display"]["hidden_sidebar_pages"], ["keep.py"])
        self.assertEqual(saved["display"]["unknown"], 4)
        self.assertEqual(saved["countdown"]["title"], "do not replace")
        self.assertEqual(saved["future"], {"keep": "yes"})

    def test_two_ui_sessions_keep_policy_draft_and_reject_stale_form(self):
        self.write({"analysis": {"caffeine_missing_as_zero": True}, "timer": {"hourly_rate": 2000}})
        first, second = self.page().run(), self.page().run()
        self.policy(first).uncheck().run()
        next(item for item in second.number_input if item.label == "初期時給").set_value(3100)
        self.save(second).click().run()
        self.assert_no_exception(second)
        before = self.path.read_bytes()
        for _ in range(2):
            self.save(first).click().run()
            self.assert_no_exception(first)
            self.assertTrue(first.error)
            self.assertEqual(len(first.get("toast")), 1)
            self.assertFalse(self.policy(first).value, "Conflicting save must retain the unsaved policy")
            self.assertEqual(self.path.read_bytes(), before)
        next(item for item in first.button if item.label == "設定の入力を破棄して最新データを再読込").click().run()
        self.assert_no_exception(first)
        self.assertTrue(self.policy(first).value)
        self.assertEqual(next(item for item in first.number_input if item.label == "初期時給").value, 3100)

    def test_policy_initial_value_comes_from_form_snapshot_not_new_disk_value(self):
        self.write({"analysis": {"caffeine_missing_as_zero": False}})
        app = self.page().run()
        self.assertFalse(self.policy(app).value)
        # Simulate an independent settings writer after this form was opened.
        self.write({"analysis": {"caffeine_missing_as_zero": True}})
        before = self.path.read_bytes()
        app.run()
        self.assert_no_exception(app)
        self.assertFalse(self.policy(app).value)
        self.assertFalse(app.session_state["form_baseline:user_settings_form"]["data"]["analysis"]["caffeine_missing_as_zero"])
        self.save(app).click().run()
        self.assert_no_exception(app)
        self.assertTrue(app.error)
        self.assertEqual(len(app.get("toast")), 1)
        self.assertFalse(self.policy(app).value)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
