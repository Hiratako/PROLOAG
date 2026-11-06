"""The public experience never reads credentials or creates Google sessions."""
from pathlib import Path
import unittest
from unittest.mock import patch

from app_core import google_sheets_client as google
from app_core.google_docs_client import GoogleDocsClient


class ExternalConnectionBoundaryTests(unittest.TestCase):
    def denied(self, action):
        with self.assertRaises(google.GoogleShareError) as error:
            action()
        self.assertEqual(error.exception.code, "web_demo_disabled")

    def test_status_does_not_scan_dependencies_or_private_storage(self):
        with patch.object(google, "dependencies_available", side_effect=AssertionError("dependency scan")), \
             patch.object(google, "_PrivateVault", side_effect=AssertionError("private storage")):
            result = google.connection_status(Path("unused"))
        self.assertFalse(result["connected"])
        self.assertFalse(result["supported"])
        self.assertEqual(result["error_code"], "web_demo_disabled")

    def test_vault_and_disconnect_are_blocked_before_directory_resolution(self):
        with patch.object(google, "_private_directory", side_effect=AssertionError("directory access")):
            self.denied(lambda: google._PrivateVault(Path("unused")))
            self.denied(lambda: google.disconnect(Path("unused")))

    def test_sheets_and_docs_construction_do_not_use_a_transport(self):
        for cls in (google.GoogleSheetsClient, GoogleDocsClient):
            self.denied(lambda: cls(root=Path("unused"), transport=None, vault=None, state={}))

    def test_authorized_transport_is_blocked_before_google_sdk_import(self):
        self.denied(lambda: google._authorized_transport({}))


if __name__ == "__main__":
    unittest.main()
