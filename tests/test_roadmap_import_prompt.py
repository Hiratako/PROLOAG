from pathlib import Path
import unittest

from app_core.roadmap_import_prompt import get_example_json, get_import_prompt


ROOT = Path(__file__).resolve().parents[1]


class RoadmapImportPromptTests(unittest.TestCase):
    def test_downloaded_prompt_is_the_public_asset_and_contains_contract(self):
        prompt = get_import_prompt()
        self.assertEqual(prompt, (ROOT / "docs/ai-roadmap-import-prompt.md").read_text(encoding="utf-8-sig"))
        for key in (
            "format", "schema_version", "plan_title", "items", "milestones", "ref", "title",
            "start_date", "end_date", "deadline", "planned_minutes", "status", "progress_kind",
            "completed", "total", "unit", "classification", "predecessor_refs", "memo", "date",
            "project", "subject", "material",
        ):
            self.assertIn(f"`{key}`", prompt)
        self.assertIn('"proloag.study-roadmap"', prompt)
        self.assertIn("新しい工程・試験／節目の追加用", prompt)
        self.assertIn("ダウンロードできる形で生成", prompt)
        self.assertNotIn("2026-11-09", prompt)
        self.assertNotIn("C:\\Users\\", prompt)

    def test_example_is_strict_json_accepted_by_actual_import_parser(self):
        from app_core.roadmap_import import parse_import_file

        raw = get_example_json()
        self.assertIsInstance(raw, bytes)
        self.assertLess(len(raw), 2 * 1024 * 1024)
        self.assertNotIn(b"```", raw)
        parsed = parse_import_file(raw)
        self.assertEqual(parsed["format"], "proloag.study-roadmap")
        self.assertEqual(len(parsed["items"]), 2)
        self.assertEqual(len(parsed["milestones"]), 1)
        self.assertIn("架空", parsed["plan_title"])
        self.assertEqual(sum(item["planned_minutes"] for item in parsed["items"]), 420)
        self.assertEqual(parsed["items"][1]["predecessor_refs"], ["E01"])
        for item in parsed["items"]:
            self.assertEqual(item["classification"], dict.fromkeys(("project", "subject", "material", "unit")))

class RoadmapImportPackagingTests(unittest.TestCase):
    def test_only_public_prompt_guides_and_code_ship(self):
        import test_portable_release_safety as fixtures

        if not fixtures.POWERSHELL:
            self.skipTest("Windows PowerShell is required")
        fixture = fixtures.PortableReleaseSafetyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        public = (
            "docs/roadmap-ai-import.md", "docs/ai-roadmap-import-prompt.md",
            "app_core/roadmap_import.py", "app_core/roadmap_import_ui.py",
            "app_core/roadmap_import_prompt.py",
        )
        private = (
            "settings/roadmap_imports.json", "settings/study_roadmap.json",
            "docs/private-ai-plan.json", "uploads/学習計画.json",
            "backups/json_operations/synthetic/transaction.json",
        )
        for name in public:
            fixture.write(name, "synthetic public")
        for name in private:
            fixture.write(name, "SYNTHETIC_PRIVATE_ROADMAP_MUST_NOT_SHIP")
        fixture.test_copy_app_allows_only_code_stylesheet_and_streamlit_config()
        app = fixture.root / "release/synthetic_demo/app"
        for name in public:
            self.assertTrue((app / name).is_file(), name)
        for name in private:
            self.assertFalse((app / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
