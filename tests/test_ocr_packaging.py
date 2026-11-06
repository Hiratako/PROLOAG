import unittest
import test_portable_release_safety as fixtures


@unittest.skipUnless(fixtures.POWERSHELL, "PowerShell is required")
class OCRPackagingTests(unittest.TestCase):
    def test_public_files_ship_without_inputs_history_or_models(self):
        fixture = fixtures.PortableReleaseSafetyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        public = [
            "requirements-ocr.txt",
            "docs/ocr.md",
            "app_core/ocr_service.py",
            "app_core/ocr_worker.py",
            "app_core/ocr_ui.py",
            "app_core/setup_ocr.py",
            "prolog_ui/ocr_viewer.py",
            "pages/22_画像・PDFの文字読み取り.py",
        ]
        private = [
            "ocr_data/jobs/abcdef/source.pdf",
            "ocr_data/jobs/abcdef/edits.json",
            "ocr_data/jobs/abcdef/page-1.png",
            "tools/ocr-models/model.onnx",
            ".deps-ocr311/rapidocr/__init__.py",
        ]
        for name in public + private:
            fixture.write(name, "synthetic")
        fixture.test_copy_app_allows_only_code_stylesheet_and_streamlit_config()
        app = fixture.root / "release/synthetic_demo/app"
        for name in public:
            self.assertTrue((app / name).is_file(), name)
        for name in private:
            self.assertFalse((app / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
