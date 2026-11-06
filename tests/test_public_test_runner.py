from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest


RUNNER = Path(__file__).resolve().parents[1] / "scripts/run_tests.py"
OPTIONAL = ("test_media_conversion.py", "test_media_conversion_ui.py", "test_transcription_process.py", "test_ocr_process.py")


class EntryPointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="proloag-public-runner-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "scripts").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "tests/__init__.py").write_text("", encoding="utf-8")
        shutil.copy2(RUNNER, self.root / "scripts/run_tests.py")
        for name in ("test_basic.py", *OPTIONAL):
            (self.root / "tests" / name).write_text("import unittest\nclass T(unittest.TestCase):\n def test_pass(self): self.assertTrue(True)\n", encoding="utf-8")

    def run_it(self, kind="core"):
        return subprocess.run([sys.executable, "-B", str(self.root / "scripts/run_tests.py"), "--suite", kind],
                              cwd=self.root, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"},
                              text=True, capture_output=True, encoding="utf-8", timeout=30)

    def write_json(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_core_excludes_external_modules_before_importing_them(self):
        for name in OPTIONAL:
            (self.root / "tests" / name).write_text("raise RuntimeError('must not import')\n", encoding="utf-8")
        result = self.run_it()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Selected 1 test modules", result.stdout)
        for name in OPTIONAL:
            self.assertIn(name, result.stdout)

    def test_integration_and_all_select_the_documented_modules(self):
        for kind, count in (("integration", 4), ("all", 5)):
            result = self.run_it(kind)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(f"Selected {count} test modules", result.stdout)

    def test_failing_test_is_not_hidden(self):
        (self.root / "tests/test_basic.py").write_text("import unittest\nclass T(unittest.TestCase):\n def test_fail(self): self.fail('synthetic failure')\n", encoding="utf-8")
        result = self.run_it()
        self.assertEqual(result.returncode, 1)
        self.assertIn("failures=1", result.stdout)

    def test_unknown_json_is_rejected_before_any_test_import(self):
        path = self.root / "settings/unknown.json"
        path.parent.mkdir()
        path.write_text("not JSON - must not be parsed", encoding="utf-8")
        (self.root / "tests/test_basic.py").write_text("raise RuntimeError('must not import tests')\n", encoding="utf-8")
        result = self.run_it()
        self.assertEqual(result.returncode, 2)
        self.assertIn("settings/unknown.json", result.stderr)
        self.assertNotIn("must not import tests", result.stdout + result.stderr)

    def test_four_exact_empty_files_are_allowed_but_user_content_is_rejected(self):
        for name, value in {
            "notes_data/notebooks.json": {"notebooks": []},
            "master_data/master.json": {"projects": [], "subjects": [], "materials": [], "units": [], "tags": []},
            "flashcards.json": [], "reminders.json": [],
        }.items():
            self.write_json(name, value)
        result = self.run_it()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.write_json("notes_data/notebooks.json", {"notebooks": [{"name": "PRIVATE_SENTINEL"}]})
        result = self.run_it()
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("PRIVATE_SENTINEL", result.stdout + result.stderr)

    def test_invalid_and_duplicate_key_scaffold_is_rejected(self):
        self.write_json("notes_data/notebooks.json", {"notebooks": []})
        path = self.root / "notes_data/notebooks.json"
        for value in ('{"notebooks":[1],"notebooks":[]}', '{broken'):
            path.write_text(value, encoding="utf-8")
            result = self.run_it()
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_incomplete_test_inventory_is_an_error(self):
        (self.root / "tests" / OPTIONAL[0]).unlink()
        result = self.run_it()
        self.assertEqual(result.returncode, 2)
        self.assertIn("Incomplete source test inventory", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
