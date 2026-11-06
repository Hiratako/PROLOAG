"""Exhibition routes must never dispatch an editor or arbitrary local path."""
from pathlib import Path
import unittest
from unittest.mock import patch

from web_demo import catalog


class ExhibitionRouteBoundaryTests(unittest.TestCase):
    def test_unknown_or_path_shaped_routes_never_execute_a_file(self):
        rejected = (
            "", None, "settings", "timer", "notes", "admin", "upload", "google",
            "../main.py", "../../config.py", "/etc/passwd", "C:\\Windows\\win.ini",
            "reference_pages/01_時は金なり｜秒給カウンター.py",
            "reference_pages/98_ユーザー設定.py", "%2e%2e%2fmain.py", "study/../settings",
        )
        with patch.object(catalog, "_execute_view") as execute:
            for route in rejected:
                with self.subTest(route=route), self.assertRaises(ValueError):
                    catalog.render_product_view(route)
            execute.assert_not_called()

    def test_declared_readonly_views_dispatch_only_their_bundled_files(self):
        reference = (catalog.ROOT / "reference_pages").resolve()
        for route, filename in catalog.READ_ONLY_VIEWS.items():
            with self.subTest(route=route):
                expected = (reference / filename).resolve()
                self.assertTrue(expected.is_file())
                self.assertEqual(expected.parent, reference)
                with patch.object(catalog, "_execute_view") as execute:
                    catalog.render_product_view(route)
                    execute.assert_called_once()
                    dispatched = Path(execute.call_args.args[0]).resolve()
                    self.assertEqual(dispatched, expected)

    def test_legacy_automatic_page_discovery_is_absent(self):
        # Otherwise visiting the app before custom navigation initializes could
        # expose one of the unregistered local product editors.
        self.assertFalse((catalog.ROOT / "pages").exists())

    def test_readonly_views_are_reachable_and_route_names_are_unique(self):
        self.assertEqual(len(catalog.DEMO_ROUTES), len(set(catalog.DEMO_ROUTES)))
        self.assertTrue(set(catalog.READ_ONLY_VIEWS).issubset(catalog.DEMO_ROUTES))
        for route in catalog.DEMO_ROUTES:
            self.assertIsInstance(route, str)
            self.assertTrue(route.isascii())
            self.assertNotIn("/", route)


if __name__ == "__main__":
    unittest.main()
