"""Startup failures are diagnosable by owners without exposing them to visitors."""
from pathlib import Path
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


ROOT = Path(__file__).resolve().parents[1]
GENERIC_NOTICE = "体験データを準備できませんでした。少し待ってから開き直してください。"


class ExperienceStartupLoggingTests(unittest.TestCase):
    def test_preparation_failure_is_logged_without_exposing_details_in_the_page(self):
        from streamlit import config
        config.get_config_options(force_reparse=True,
                                  options_from_flags={"global.developmentMode": False})
        private_detail = "private-preparation-detail /private/internal/visitor-store"
        for exception_type in (OSError, ValueError, RuntimeError):
            with self.subTest(exception_type=exception_type.__name__):
                # No temporary visitor store is created and no source data is
                # modified. Run the existing entrypoint, not a generated script.
                with patch("web_demo.sandbox.ensure_session_sandbox",
                           side_effect=exception_type(private_detail)), \
                        self.assertLogs("web_demo.app", level="ERROR") as captured:
                    app = AppTest.from_file(ROOT / "main.py", default_timeout=20).run()
                self.assertEqual(len(app.exception), 0)
                self.assertEqual([element.value for element in app.error], [GENERIC_NOTICE])
                visible = "\n".join(
                    str(element.value) for name in ("error", "markdown", "text", "caption")
                    for element in app.get(name)
                )
                self.assertNotIn(private_detail, visible)
                logged = "\n".join(captured.output)
                self.assertIn("Web experience sandbox preparation failed", logged)
                self.assertIn("Traceback (most recent call last)", logged)
                self.assertIn(exception_type.__name__, logged)
                self.assertIn(private_detail, logged)


if __name__ == "__main__":
    unittest.main()
