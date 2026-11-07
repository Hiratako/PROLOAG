"""Build only small synthetic source fixtures in an owned OS temp folder."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import build_web_demo_package as builder


class WebDemoLicensePackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os_temp = (Path(os.environ["LOCALAPPDATA"]) / "Temp"
                   if os.name == "nt" else Path("/tmp"))
        if "onedrive" in str(os_temp.resolve()).casefold():
            raise RuntimeError("Package fixtures must not use OneDrive.")
        cls.temporary = tempfile.TemporaryDirectory(prefix="proloag-license-package-tests-", dir=os_temp)
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.workspace = Path(cls.temporary.name)

    def fixture(self, mode):
        workspace = Path(tempfile.mkdtemp(prefix="case-", dir=self.workspace))
        root = workspace / "source"
        (root / "web_demo").mkdir(parents=True)
        (root / "docs").mkdir()
        (root / "main.py").write_text("# Synthetic public entry point\n", encoding="utf-8")
        requirements = b"# Synthetic dependency fixture\n"
        (root / "requirements.txt").write_bytes(requirements)
        (root / "web_demo/dataset_manifest.json").write_text(
            json.dumps({"record_sha256": {}}), encoding="utf-8")
        (root / "WEB_DEMO_VERIFICATION.json").write_text(
            json.dumps({"status": "passed", "data_unchanged": True}), encoding="utf-8")
        (root / "docs/linux-dependency-verification.json").write_text(
            json.dumps({"status": "passed", "requirements_sha256": hashlib.sha256(requirements).hexdigest()}),
            encoding="utf-8")
        if mode in {"selected", "both"}:
            (root / "LICENSE").write_text(
                "LicenseRef-PROLOAG-No-Sale-1.0\nSynthetic selected terms fixture.\n", encoding="utf-8")
        if mode in {"pending", "both"}:
            (root / "LICENSE_STATUS.md").write_text("Synthetic pending status fixture.\n", encoding="utf-8")
        return root, workspace / "source.zip"

    def build_fixture(self, root, output):
        with ExitStack() as stack:
            stack.enter_context(patch.object(builder, "ROOT", root))
            verify = stack.enter_context(patch("web_demo.data.verify_demo_dataset", return_value={}))
            result = builder.build(output)
            verify.assert_called_once_with()
        return result

    def test_selected_license_is_included_exactly_and_recorded_in_manifest(self):
        root, output = self.fixture("selected")
        expected = (root / "LICENSE").read_bytes()
        result = self.build_fixture(root, output)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(archive.read("proloag_web_demo/LICENSE"), expected)
            self.assertNotIn("proloag_web_demo/LICENSE_STATUS.md", archive.namelist())
            manifest = json.loads(archive.read("proloag_web_demo/WEB_DEMO_PACKAGE_MANIFEST.json"))
        self.assertEqual(manifest["license_status"], "selected")
        self.assertEqual(manifest["license_file"], "LICENSE")
        self.assertEqual(manifest["license_identifier"], "LicenseRef-PROLOAG-No-Sale-1.0")
        self.assertEqual(manifest["license_sha256"], hashlib.sha256(expected).hexdigest())
        self.assertEqual(manifest["file_sha256"]["LICENSE"], manifest["license_sha256"])
        self.assertEqual(result["sha256"], hashlib.sha256(output.read_bytes()).hexdigest())

    def test_missing_or_conflicting_license_markers_fail_before_verification_or_output(self):
        for mode in ("missing", "both"):
            root, output = self.fixture(mode)
            with self.subTest(mode=mode), patch.object(builder, "ROOT", root), \
                    patch("web_demo.data.verify_demo_dataset") as verify:
                with self.assertRaisesRegex(ValueError, "exactly one"):
                    builder.build(output)
                verify.assert_not_called()
                with self.assertRaisesRegex(ValueError, "exactly one"):
                    builder.inventory()
            self.assertFalse(output.exists())
            self.assertFalse((root / "WEB_DEMO_PACKAGE_MANIFEST.json").exists())

    def test_legacy_pending_fixture_keeps_its_distinct_status(self):
        root, output = self.fixture("pending")
        self.build_fixture(root, output)
        with zipfile.ZipFile(output) as archive:
            self.assertIn("proloag_web_demo/LICENSE_STATUS.md", archive.namelist())
            self.assertNotIn("proloag_web_demo/LICENSE", archive.namelist())
            manifest = json.loads(archive.read("proloag_web_demo/WEB_DEMO_PACKAGE_MANIFEST.json"))
        self.assertEqual(manifest["license_status"], "unselected_prepublication_preparation")
        self.assertEqual(manifest["license_file"], "LICENSE_STATUS.md")
        self.assertIsNone(manifest["license_identifier"])


if __name__ == "__main__":
    unittest.main()
