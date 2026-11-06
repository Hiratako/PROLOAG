"""Only registered non-Hobby pages execute, always inside visitor storage."""
from contextlib import nullcontext
from pathlib import Path
import unittest
from unittest.mock import patch

from web_demo import catalog


class ExperienceRouteBoundaryTests(unittest.TestCase):
    def test_unknown_or_path_shaped_routes_never_execute_a_file(self):
        rejected = (
            "", None, "admin", "upload", "google", "hobby", "media", "transcription", "ocr",
            "../main.py", "../../config.py", "/etc/passwd", "C:\\Windows\\win.ini",
            "reference_pages/01_時は金なり｜秒給カウンター.py",
            "reference_pages/98_ユーザー設定.py", "%2e%2e%2fmain.py", "study/../settings",
        )
        with patch.object(catalog, "_execute_view") as execute, \
                patch("web_demo.sandbox.ensure_session_sandbox") as allocate:
            for route in rejected:
                with self.subTest(route=route), self.assertRaises(ValueError):
                    catalog.render_product_view(route)
            execute.assert_not_called()
            allocate.assert_not_called()

    def test_all_product_views_dispatch_only_bundled_files_inside_the_visitor_context(self):
        reference = (catalog.ROOT / "reference_pages").resolve()
        handle = object()
        for route, filename in catalog.PRODUCT_VIEWS.items():
            with self.subTest(route=route):
                expected = (reference / filename).resolve()
                self.assertTrue(expected.is_file())
                self.assertEqual(expected.parent, reference)
                with patch.object(catalog, "_execute_view") as execute, \
                        patch("web_demo.sandbox.ensure_session_sandbox", return_value=handle) as allocate, \
                        patch("web_demo.sandbox.sandbox_context", return_value=nullcontext()) as context:
                    catalog.render_product_view(route)
                    allocate.assert_called_once_with()
                    context.assert_called_once_with(handle)
                    execute.assert_called_once()
                    dispatched = Path(execute.call_args.args[0]).resolve()
                    self.assertEqual(dispatched, expected)

    def test_legacy_automatic_page_discovery_is_absent(self):
        # Otherwise unregistered Hobby pages could bypass the explicit catalog.
        self.assertFalse((catalog.ROOT / "pages").exists())

    def test_all_non_hobby_pages_are_reachable_and_route_names_are_unique(self):
        self.assertEqual(len(catalog.DEMO_ROUTES), len(set(catalog.DEMO_ROUTES)))
        self.assertTrue(set(catalog.PRODUCT_VIEWS).issubset(catalog.DEMO_ROUTES))
        self.assertEqual(len(catalog.PRODUCT_VIEWS), 25)
        self.assertEqual(len(catalog.DEMO_ROUTES), 27)
        expected = {page.name for page in (catalog.ROOT / "reference_pages").glob("*.py")
                    if not page.name.startswith(("20_", "21_", "22_"))}
        self.assertEqual(set(catalog.PRODUCT_VIEWS.values()), expected)
        self.assertTrue({"timer", "notes", "settings", "roadmap", "legacy-import"}
                        .issubset(catalog.PRODUCT_VIEWS))
        for route in catalog.DEMO_ROUTES:
            self.assertIsInstance(route, str)
            self.assertTrue(route.isascii())
            self.assertNotIn("/", route)


if __name__ == "__main__":
    unittest.main()
