"""Run the integrity action against real, visitor-owned synthetic records."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import unittest

from streamlit.testing.v1 import AppTest

from app_core.integrity_rules import _all_json_files
from tests.test_web_experience_workflows import _render_experience, widget
from web_demo.data import data_fingerprint
from web_demo import sandbox


def json_fingerprint(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*.json") if path.is_file()}


class ExperienceIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit import config as streamlit_config
        streamlit_config.get_config_options(
            force_reparse=True, options_from_flags={"global.developmentMode": False})

    def setUp(self):
        os_temp = (Path(os.environ["LOCALAPPDATA"]) / "Temp"
                   if os.name == "nt" else Path("/tmp"))
        if "onedrive" in str(os_temp.resolve()).casefold():
            raise RuntimeError("Integrity fixtures must use OS Temp outside OneDrive.")
        self.seed_before = data_fingerprint()
        self.temporary = tempfile.TemporaryDirectory(prefix="ix_", dir=os_temp)
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.a = sandbox.create_sandbox(base_dir=base)
        self.addCleanup(self.destroy_visitor, self.a)
        self.b = sandbox.create_sandbox(base_dir=base)
        self.addCleanup(self.destroy_visitor, self.b)
        self.a_before = json_fingerprint(self.a.root)
        self.b_before = json_fingerprint(self.b.root)

    def destroy_visitor(self, visitor):
        self.assertTrue(sandbox.destroy_sandbox(visitor), "Release only this test's visitor store")

    def tearDown(self):
        self.assertEqual(data_fingerprint(), self.seed_before, "Shared seed JSON changed")
        self.assertEqual(json_fingerprint(self.a.root), self.a_before, "Visitor A JSON changed")
        self.assertEqual(json_fingerprint(self.b.root), self.b_before, "Visitor B JSON changed")

    def assert_healthy(self, app):
        self.assertEqual(len(app.exception), 0, [item.message for item in app.exception])
        self.assertEqual(len(app.error), 0, [item.value for item in app.error])

    def test_integrity_button_runs_twice_and_offers_the_result_csv(self):
        app = AppTest.from_function(
            _render_experience, args=("integrity", self.a), default_timeout=120).run()
        self.assert_healthy(app)
        for attempt in range(2):
            with self.subTest(attempt=attempt + 1):
                widget(app.button, "チェックを実行する").click().run(timeout=120)
                self.assert_healthy(app)
                result = app.session_state["integrity_result"]
                self.assertEqual(result["checked_file_count"], 715)
                self.assertEqual(result["error_count"], 0)
                self.assertGreater(result["total_checks"], 0)
                downloads = app.get("download_button")
                self.assertEqual(len(downloads), 1)
                self.assertEqual(downloads[0].proto.label, "整合性チェック結果CSVをダウンロード")

    def test_integrity_file_inventory_resolves_paths_in_each_current_visitor(self):
        with sandbox.sandbox_context(self.a):
            files_a = _all_json_files()
        with sandbox.sandbox_context(self.b):
            files_b = _all_json_files()
        for visitor, other, paths in ((self.a, self.b, files_a), (self.b, self.a, files_b)):
            with self.subTest(visitor=visitor.token):
                self.assertEqual(len(paths), 715)
                self.assertTrue(all(isinstance(path, Path) for path in paths))
                self.assertEqual(paths, sorted(paths))
                self.assertTrue(all(path.is_relative_to(visitor.root) for path in paths))
                self.assertTrue(all(not path.is_relative_to(other.root) for path in paths))
                self.assertTrue(all(not path.is_relative_to(sandbox.SOURCE_ROOT) for path in paths))
        self.assertEqual({path.relative_to(self.a.root) for path in files_a},
                         {path.relative_to(self.b.root) for path in files_b})
        # Values returned in A must remain concrete A paths after switching B.
        self.assertTrue(all(path.is_relative_to(self.a.root) for path in files_a))


if __name__ == "__main__":
    unittest.main()
