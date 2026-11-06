"""Reset the real application, including the browser-visible confirmation state."""
from pathlib import Path
import unittest

from streamlit.testing.v1 import AppTest

from web_demo.data import data_fingerprint


class ExperienceResetTests(unittest.TestCase):
    def test_main_reset_replaces_data_and_clears_its_confirmation_widget(self):
        from app_core import json_store
        from app_core.settings_service import load_user_settings, update_user_settings
        from streamlit import config as streamlit_config
        from web_demo.sandbox import (
            SESSION_KEY, SandboxAccessError, destroy_sandbox, sandbox_context,
        )

        streamlit_config.get_config_options(
            force_reparse=True,
            options_from_flags={"global.developmentMode": False},
        )
        source = Path(__file__).resolve().parents[1]
        seed_before = data_fingerprint()
        handles = []

        def clean_handles():
            for handle in reversed(handles):
                try:
                    destroy_sandbox(handle)
                except SandboxAccessError:
                    # The reset already retired and removed the old handle.
                    pass

        self.addCleanup(clean_handles)

        def healthy(app):
            self.assertEqual(len(app.exception), 0,
                             [item.message for item in app.exception])
            self.assertEqual(len(app.error), 0, [item.value for item in app.error])

        def element(items, label):
            found = [item for item in items if item.label == label]
            self.assertEqual(len(found), 1, label)
            return found[0]

        app = AppTest.from_file(source / "main.py", default_timeout=90).run()
        healthy(app)
        old = app.session_state[SESSION_KEY]
        handles.append(old)
        probe_name = "settings/reset_probe.json"
        with sandbox_context(old):
            initial_rate = load_user_settings()["timer"]["hourly_rate"]
            update_user_settings({"timer": {"hourly_rate": initial_rate + 123}})
            json_store.save_json_safe(old.root / probe_name, {"synthetic_test": True})
            self.assertTrue((old.root / probe_name).is_file())

        confirm_label = "このタブの操作内容をリセットする"
        button_label = "体験をリセット"
        self.assertFalse(element(app.checkbox, confirm_label).value)
        self.assertTrue(element(app.button, button_label).disabled)
        old_widget_key = element(app.checkbox, confirm_label).key

        element(app.checkbox, confirm_label).check().run(timeout=90)
        healthy(app)
        self.assertTrue(element(app.checkbox, confirm_label).value)
        self.assertFalse(element(app.button, button_label).disabled)
        element(app.button, button_label).click().run(timeout=90)
        healthy(app)

        fresh = app.session_state[SESSION_KEY]
        handles.append(fresh)
        self.assertIsNot(fresh, old)
        self.assertNotEqual(fresh.root, old.root)
        self.assertNotEqual(fresh.token, old.token)
        self.assertFalse((fresh.root / probe_name).exists())
        with sandbox_context(fresh):
            self.assertEqual(load_user_settings()["timer"]["hourly_rate"], initial_rate)

        self.assertFalse(element(app.checkbox, confirm_label).value,
                         "The confirmation must visually reset, not remain checked")
        self.assertTrue(element(app.button, button_label).disabled)
        self.assertNotEqual(element(app.checkbox, confirm_label).key, old_widget_key)
        self.assertEqual(data_fingerprint(), seed_before)


if __name__ == "__main__":
    unittest.main()
