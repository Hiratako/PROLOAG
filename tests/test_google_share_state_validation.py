"""Malformed local sharing state must fail closed before display/network."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app_core import google_share_service as service, google_share_ui as ui, json_store
from app_core.google_share_snapshot import build_share_snapshot


class GoogleShareStateValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag-share-validation-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.snapshot = build_share_snapshot(self.root, date(2026, 9, 10), date(2026, 9, 16))
        original_read = json_store._read
        def read(path):
            self.assertTrue(Path(path).resolve().is_relative_to(self.root))
            return original_read(path)
        reader = patch.object(json_store, "_read", side_effect=read)
        reader.start()
        self.addCleanup(reader.stop)

    def save(self, name, value):
        path = self.root / "backups/google_share" / name
        json_store.save_json_safe(path, value)
        return path

    def pending(self):
        return {"schema_version": 1, "operation_id": "fixture-operation", "spreadsheet_id": "fixture-book",
                "created_at": "2026-09-17T09:00:00+09:00",
                "status": "uncertain", "snapshot": deepcopy(self.snapshot),
                "payload_hash": service.payload_hash(self.snapshot)}

    def test_malformed_history_and_preferences_are_rejected_read_only(self):
        for state in ({"history": [None]}, {"history": [{"published_at": "2026-09-17"}]},
                      {"last_success": {"url": "https://example.invalid"}},
                      {"preferences": []}, {"preferences": {"days": []}}, {"schema_version": True}):
            with self.subTest(state=state):
                path = self.save("state.json", state)
                before = path.read_bytes()
                with self.assertRaises(service.ShareStateError):
                    service.load_share_state(self.root)
                self.assertEqual(path.read_bytes(), before)

    def test_structurally_invalid_pending_is_rejected_even_with_matching_hash(self):
        for field, value in (("period", {}), ("counts", {}), ("generated_at", None),
                             ("tables", {"overview": [["a", "b"], ["one"]]}),
                             ("tables", {"overview": [["duplicate", "duplicate"], ["one", "two"]]}),
                             ("tables", {"overview": [["a"], [{"invalid": "cell"}]]})):
            with self.subTest(field=field, value=value):
                pending = self.pending()
                pending["snapshot"][field] = value
                pending["payload_hash"] = service.payload_hash(pending["snapshot"])
                path = self.save("pending.json", pending)
                before = path.read_bytes()
                client = Mock()
                with self.assertRaises(service.ShareStateError):
                    service.publish_share(client, root=self.root)
                self.assertEqual(client.mock_calls, [])
                self.assertEqual(path.read_bytes(), before)

    def test_duplicate_headers_render_error_without_network(self):
        from streamlit import config as streamlit_config
        from streamlit.testing.v1 import AppTest
        streamlit_config.get_config_options(options_from_flags={"global.developmentMode": False})
        pending = self.pending()
        pending["snapshot"]["tables"] = {"overview": [["duplicate", "duplicate"], ["one", "two"]]}
        pending["payload_hash"] = service.payload_hash(pending["snapshot"])
        path = self.save("pending.json", pending)
        before = path.read_bytes()
        with patch.object(ui, "connection_status") as status:
            app = AppTest.from_string(
                "from pathlib import Path\nfrom app_core.google_share_ui import render_google_share\n"
                f"render_google_share(Path({str(self.root)!r}))", default_timeout=30).run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertEqual(len(app.error), 1)
        status.assert_not_called()
        self.assertEqual(path.read_bytes(), before)

    def test_unhashable_pending_status_raises_user_facing_error(self):
        pending = self.pending()
        pending["status"] = []
        self.save("pending.json", pending)
        with self.assertRaises(service.ShareStateError):
            service.load_pending_share(self.root)

    def test_pending_creation_timestamp_is_required_before_display_or_network(self):
        from streamlit import config as streamlit_config
        from streamlit.testing.v1 import AppTest
        streamlit_config.get_config_options(options_from_flags={"global.developmentMode": False})
        for timestamp in (None, "invalid", []):
            with self.subTest(timestamp=timestamp):
                pending = self.pending()
                pending["created_at"] = timestamp
                path = self.save("pending.json", pending)
                before = path.read_bytes()
                client = Mock()
                with self.assertRaises(service.ShareStateError):
                    service.publish_share(client, root=self.root)
                self.assertEqual(client.mock_calls, [])
                with patch.object(ui, "connection_status") as status:
                    app = AppTest.from_string(
                        "from pathlib import Path\nfrom app_core.google_share_ui import render_google_share\n"
                        f"render_google_share(Path({str(self.root)!r}))", default_timeout=30).run()
                self.assertFalse(app.exception, str(app.exception))
                self.assertEqual(len(app.error), 1)
                status.assert_not_called()
                self.assertEqual(path.read_bytes(), before)

    def test_invalid_new_preview_and_corrupt_history_stop_before_remote_calls(self):
        client = Mock()
        invalid = deepcopy(self.snapshot)
        invalid.pop("generated_at")
        with self.assertRaises(service.ShareStateError):
            service.publish_share(client, invalid, root=self.root)
        self.assertEqual(client.mock_calls, [])
        self.assertIsNone(service.load_pending_share(self.root))
        path = self.save("state.json", {"history": [None]})
        before = path.read_bytes()
        with self.assertRaises(service.ShareStateError):
            service.publish_share(client, self.snapshot, root=self.root)
        self.assertEqual(client.mock_calls, [])
        self.assertEqual(path.read_bytes(), before)

    def test_corrupt_history_renders_error_without_page_exception_or_connection_read(self):
        from streamlit import config as streamlit_config
        from streamlit.testing.v1 import AppTest
        streamlit_config.get_config_options(options_from_flags={"global.developmentMode": False})
        path = self.save("state.json", {"history": [None]})
        before = path.read_bytes()
        with patch.object(ui, "connection_status", return_value={"connected": False}) as status, \
                patch.object(ui, "app_today", return_value=date(2026, 9, 17)):
            app = AppTest.from_string(
                "from pathlib import Path\nfrom app_core.google_share_ui import render_google_share\n"
                f"render_google_share(Path({str(self.root)!r}))", default_timeout=30).run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertEqual(len(app.error), 1)
        status.assert_not_called()
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
