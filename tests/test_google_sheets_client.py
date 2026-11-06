"""Google publication tests: synthetic state, fake network, no real accounts."""
from __future__ import annotations

from copy import deepcopy
import json
import logging
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app_core import google_sheets_client as gs


SECRET = "DO_NOT_EXPOSE_TOKEN_OR_HTTP_BODY"
BOOK_ID = "fake_spreadsheet_12345"


class MemoryVault:
    def __init__(self, state=None):
        self.state = deepcopy(state)
        self.writes = 0

    def read(self):
        return deepcopy(self.state)

    def write(self, state):
        self.state = deepcopy(state)
        self.writes += 1

    def delete(self):
        self.state = None


class FakeResponse:
    def __init__(self, data=None, status=200):
        self.data = deepcopy(data or {})
        self.status_code = status
        self.text = SECRET

    def json(self):
        return deepcopy(self.data)


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.book = None
        self.cells = {}
        self.fail_before = None
        self.lose_after = None
        self.status = None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, deepcopy(kwargs)))
        if self.fail_before and self.fail_before in url:
            self.fail_before = None
            raise RuntimeError(SECRET)
        if self.status:
            status, self.status = self.status, None
            return FakeResponse(status=status)
        if method == "GET":
            response = FakeResponse(self.book)
        elif url == gs.API_BASE:
            body = kwargs["json"]
            self.book = {"spreadsheetId": BOOK_ID, "sheets": deepcopy(body["sheets"])}
            response = FakeResponse({"spreadsheetId": BOOK_ID})
        elif url.endswith(":batchUpdate"):
            for request in kwargs["json"]["requests"]:
                if "createDeveloperMetadata" in request:
                    marker = request["createDeveloperMetadata"]["developerMetadata"]
                    if marker["location"].get("spreadsheet"):
                        self.book.setdefault("developerMetadata", []).append(deepcopy(marker))
                    else:
                        sheet = self.sheet(marker["location"]["sheetId"])
                        sheet.setdefault("developerMetadata", []).append(deepcopy(marker))
                elif "updateSheetProperties" in request:
                    props = request["updateSheetProperties"]["properties"]
                    self.sheet(props["sheetId"])["properties"]["gridProperties"] = deepcopy(props["gridProperties"])
                elif "updateCells" in request:
                    change = request["updateCells"]
                    self.cells[change["range"]["sheetId"]] = deepcopy(change["rows"])
                else:
                    raise AssertionError("Unexpected operation")
            response = FakeResponse({"spreadsheetId": BOOK_ID})
        elif url.endswith(":getByDataFilter"):
            response = FakeResponse({"sheets": [{"properties": {"sheetId": gs.SHEET_IDS["manifest"]},
                "data": [{"rowData": deepcopy(self.cells.get(gs.SHEET_IDS["manifest"], []))}]}]})
        else:
            raise AssertionError("Unexpected network destination")
        if self.lose_after and self.lose_after == ("create" if url == gs.API_BASE else url.rsplit(":", 1)[-1]):
            self.lose_after = None
            raise RuntimeError(SECRET)
        return response

    def sheet(self, sheet_id):
        return next(s for s in self.book["sheets"] if s["properties"]["sheetId"] == sheet_id)

    def close(self):
        pass


def credentials_payload(client_id="fake-client"):
    return {"token": SECRET, "refresh_token": SECRET, "client_secret": SECRET,
            "client_id": client_id, "token_uri": gs.TOKEN_URI, "scopes": list(gs.SCOPES)}


def config(client_id="fake-client"):
    return {"installed": {"client_id": client_id, "client_secret": SECRET,
                          "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": gs.TOKEN_URI}}


def initial_state(root):
    return {"schema_version": 1, "project_key": gs._project_key(root), "connection_id": "a" * 32,
            "credentials": credentials_payload()}


def snapshot():
    return {"snapshot_id": "b" * 64, "schema_version": 1, "generated_at": "2026-09-16T12:34:00+09:00",
            "period": {"start": "2026-09-01", "end": "2026-09-16"},
            "options": {"include_content": True, "include_study_sessions": True},
            "counts": {"study_sessions": 1, "study_days": 1},
            "tables": {"overview": [["key", "value"], ["study_hours", 1]],
                       "study_sessions": [["id", "memo"], ["record-1", "old private text"]]}}


class SheetsClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.vault = MemoryVault(initial_state(self.root))
        self.transport = FakeTransport()
        self.client = gs.GoogleSheetsClient(root=self.root, transport=self.transport, vault=self.vault, state=self.vault.read())

    def ready(self):
        return self.client.create_spreadsheet("PROLOAG test")

    def posts(self):
        return [call for call in self.transport.calls if call[0] == "POST"]

    def test_create_registers_owned_tabs_and_reuses_book(self):
        result = self.ready()
        self.assertEqual(result["spreadsheet_id"], BOOK_ID)
        self.assertEqual(self.vault.state["book"]["status"], "ready")
        self.assertEqual(self.client.read_metadata(BOOK_ID)["sheets"], gs.SHEET_IDS)
        self.ready()
        self.assertEqual(sum(url == gs.API_BASE for _, url, _ in self.posts()), 1)
        self.assertEqual(len(self.transport.book["developerMetadata"]), 1)
        for _, _, kwargs in self.transport.calls:
            self.assertEqual(kwargs["timeout"], 30)
            self.assertEqual(kwargs["max_allowed_time"], 30)
            self.assertFalse(kwargs["allow_redirects"])

    def test_arbitrary_book_rejected_before_network(self):
        self.ready()
        count = len(self.transport.calls)
        with self.assertRaises(gs.GoogleShareError) as caught:
            self.client.publish("someone_else_book", snapshot())
        self.assertEqual(caught.exception.code, "ownership")
        self.assertEqual(len(self.transport.calls), count)

    def test_typed_cells_full_replace_manifest_last_and_user_tab_untouched(self):
        self.ready()
        user_sheet = {"properties": {"sheetId": 99999, "title": "My own tab"}}
        self.transport.book["sheets"].append(deepcopy(user_sheet))
        self.transport.cells[99999] = [{"values": [{"userEnteredValue": {"formulaValue": "=1+1"}}]}]
        payload = snapshot()
        payload["tables"]["overview"] = [["=IMPORTXML(secret)", "+CMD", "-text", "@test", -2, 0, True, None, 2 ** 54]]
        expected = deepcopy(payload)
        self.client.publish(BOOK_ID, payload)
        batch = self.posts()[-1][2]["json"]["requests"]
        self.assertEqual(len(batch), 7)
        self.assertEqual(batch[-1]["updateCells"]["range"]["sheetId"], gs.SHEET_IDS["manifest"])
        cells = batch[0]["updateCells"]["rows"][0]["values"]
        self.assertTrue(all("stringValue" in item["userEnteredValue"] for item in cells[:4]))
        self.assertEqual(cells[4]["userEnteredValue"], {"numberValue": -2})
        self.assertEqual(cells[5]["userEnteredValue"], {"numberValue": 0})
        self.assertEqual(cells[6]["userEnteredValue"], {"boolValue": True})
        self.assertEqual(cells[7], {})
        self.assertEqual(cells[8]["userEnteredValue"], {"stringValue": str(2 ** 54)})
        self.assertEqual(payload, expected)
        self.assertEqual(self.transport.sheet(99999), user_sheet)
        self.assertEqual(self.transport.cells[99999][0]["values"][0]["userEnteredValue"], {"formulaValue": "=1+1"})
        payload["tables"].pop("study_sessions")
        payload["tables"]["overview"] = [["key"], ["redacted"]]
        self.client.publish(BOOK_ID, payload)
        self.assertEqual(self.transport.cells[gs.SHEET_IDS["study_sessions"]], [])
        new_batch = self.posts()[-1][2]["json"]["requests"]
        self.assertEqual(new_batch[0]["updateCells"]["range"]["endColumnIndex"], 26)
        self.assertTrue(all(set(r) == {"updateCells"} for r in new_batch))

    def test_manifest_roundtrip_and_retry_do_not_append(self):
        self.ready()
        self.assertEqual(self.client.read_publication(BOOK_ID), {})
        data = snapshot()
        self.client.publish(BOOK_ID, data)
        before = deepcopy(self.transport.cells)
        self.client.publish(BOOK_ID, data)
        self.assertEqual(self.transport.cells, before)
        manifest = self.client.read_publication(BOOK_ID)
        self.assertEqual(manifest["snapshot_id"], data["snapshot_id"])
        self.assertEqual(manifest["payload_hash"], gs.publication_hash(data))
        self.assertEqual(manifest["options"], data["options"])

    def test_response_loss_is_uncertain_and_manifest_resolves_commit(self):
        self.ready()
        self.transport.lose_after = "batchUpdate"
        data = snapshot()
        before = len(self.posts())
        with self.assertRaises(gs.GoogleShareError) as caught:
            self.client.publish(BOOK_ID, data)
        self.assertTrue(caught.exception.uncertain)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(len(self.posts()), before + 1)
        self.assertEqual(self.client.read_publication(BOOK_ID)["payload_hash"], gs.publication_hash(data))

    def test_read_post_response_loss_never_claims_remote_mutation(self):
        self.ready()
        self.transport.fail_before = ":getByDataFilter"
        with self.assertRaises(gs.GoogleShareError) as caught:
            self.client.read_publication(BOOK_ID)
        self.assertFalse(caught.exception.uncertain)

    def test_unknown_creation_does_not_retry_or_overwrite(self):
        self.transport.lose_after = "create"
        with self.assertRaises(gs.GoogleShareError) as caught:
            self.ready()
        self.assertTrue(caught.exception.uncertain)
        self.assertTrue(self.vault.state["creation_pending"])
        replacement = gs.GoogleSheetsClient(root=self.root, transport=self.transport, vault=self.vault, state=self.vault.read())
        before = len(self.transport.calls)
        with self.assertRaises(gs.GoogleShareError) as caught:
            replacement.create_spreadsheet("Retry")
        self.assertEqual(caught.exception.code, "creation_unknown")
        self.assertEqual(len(self.transport.calls), before)

    def test_metadata_initialization_response_loss_recovers_without_duplicate(self):
        self.transport.lose_after = "batchUpdate"
        with self.assertRaises(gs.GoogleShareError):
            self.ready()
        self.assertEqual(self.vault.state["book"]["status"], "initializing")
        replacement = gs.GoogleSheetsClient(root=self.root, transport=self.transport, vault=self.vault, state=self.vault.read())
        replacement.create_spreadsheet("Retry")
        self.assertEqual(self.vault.state["book"]["status"], "ready")
        self.assertEqual(len(self.transport.book["developerMetadata"]), 1)
        self.assertEqual(sum(url == gs.API_BASE for _, url, _ in self.posts()), 1)

    def test_owner_marker_tamper_missing_tabs_and_duplicate_marker_stop_writes(self):
        self.ready()
        good = deepcopy(self.transport.book)
        for mutation in (lambda b: b["developerMetadata"][0].update(metadataValue="someone else"),
                         lambda b: b["sheets"].pop(),
                         lambda b: b["developerMetadata"].append(deepcopy(b["developerMetadata"][0])),
                         lambda b: b["sheets"][0]["developerMetadata"][0].update(metadataValue="wrong role")):
            with self.subTest(mutation=mutation):
                self.transport.book = deepcopy(good)
                mutation(self.transport.book)
                before = len(self.posts())
                with self.assertRaises(gs.GoogleShareError) as caught:
                    self.client.publish(BOOK_ID, snapshot())
                self.assertEqual(caught.exception.code, "ownership")
                self.assertEqual(len(self.posts()), before)

    def test_http_errors_and_invalid_destination_are_sanitized(self):
        self.ready()
        for status, uncertain in ((400, False), (401, False), (403, False), (429, False), (503, True)):
            with self.subTest(status=status):
                self.transport.status = status
                with self.assertRaises(gs.GoogleShareError) as caught:
                    self.client._request("POST", "/" + BOOK_ID + ":batchUpdate", json={})
                self.assertEqual(caught.exception.uncertain, uncertain)
                self.assertNotIn(SECRET, str(caught.exception))
        before = len(self.transport.calls)
        for bad in ("https://evil.test", "//evil.test", "/../bad", "/abcde?token=x", "/abcde:evil", ":batchUpdate"):
            with self.assertRaises(gs.GoogleShareError):
                self.client._request("POST", bad)
        self.assertEqual(len(self.transport.calls), before)

    def test_invalid_snapshot_and_oversize_cannot_publish(self):
        self.ready()
        for change in (lambda p: p.update(schema_version=2), lambda p: p["tables"].update(manifest=[]),
                       lambda p: p["tables"].update(overview=[[float("nan")]]),
                       lambda p: p["tables"].update(overview=[["x" * 40001]])):
            payload = snapshot()
            change(payload)
            before = len(self.posts())
            with self.assertRaises(gs.GoogleShareError):
                self.client.publish(BOOK_ID, payload)
            self.assertEqual(len(self.posts()), before)
        with patch.object(gs, "MAX_REQUEST_BYTES", 10):
            with self.assertRaises(gs.GoogleShareError) as caught:
                self.client.publish(BOOK_ID, snapshot())
            self.assertEqual(caught.exception.code, "payload_too_large")

    def test_formula_or_partial_manifest_is_not_accepted(self):
        self.ready()
        self.client.publish(BOOK_ID, snapshot())
        self.transport.cells[gs.SHEET_IDS["manifest"]][1]["values"][0] = {"userEnteredValue": {"formulaValue": "=secret"}}
        with self.assertRaises(gs.GoogleShareError) as caught:
            self.client.read_publication(BOOK_ID)
        self.assertEqual(caught.exception.code, "invalid_publication")

    def test_disconnect_only_forgets_local_connection(self):
        self.ready()
        before = len(self.transport.calls)
        self.client.disconnect()
        self.assertIsNone(self.vault.state)
        self.assertIsNotNone(self.transport.book)
        self.assertEqual(len(self.transport.calls), before)


class AuthenticationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"

    def test_optional_dependencies_are_not_required_to_import(self):
        with patch.object(gs.importlib.util, "find_spec", return_value=None):
            self.assertFalse(gs.dependencies_available())
        self.assertEqual(gs.GoogleShareError("safe", code="x").code, "x")

    def test_non_google_endpoints_and_web_client_are_rejected(self):
        for value in ({"web": config()["installed"]}, {"installed": dict(config()["installed"], token_uri="https://evil.test/token")},
                      {"installed": dict(config()["installed"], auth_uri="https://accounts.google.com.evil.test/auth")}):
            with self.assertRaises(gs.GoogleShareError) as caught:
                gs.GoogleSheetsClient.connect(value, self.root)
            self.assertEqual(caught.exception.code, "invalid_oauth_config")

    def test_new_oauth_client_cannot_destroy_existing_book_or_pending_creation(self):
        for key, value in (("book", {"spreadsheet_id": BOOK_ID}), ("creation_pending", True)):
            state = initial_state(self.root)
            state[key] = value
            vault = MemoryVault(state)
            with patch.object(gs, "_PrivateVault", return_value=vault), patch.object(gs, "_run_oauth") as oauth:
                with self.assertRaises(gs.GoogleShareError) as caught:
                    gs.GoogleSheetsClient.connect(config("different-client"), self.root)
                self.assertEqual(caught.exception.code, "oauth_client_mismatch")
                self.assertEqual(vault.read(), state)
                oauth.assert_not_called()

    def test_connect_minimal_scope_pkce_and_same_client_preserves_registry(self):
        state = initial_state(self.root)
        state["book"] = {"spreadsheet_id": BOOK_ID}
        vault = MemoryVault(state)
        flow = SimpleNamespace(oauth2session=SimpleNamespace(trust_env=True))
        factory = MagicMock(return_value=flow)
        module = SimpleNamespace(InstalledAppFlow=SimpleNamespace(from_client_config=factory))
        credentials = SimpleNamespace(to_json=lambda: json.dumps(credentials_payload()))
        with patch.dict(sys.modules, {"google_auth_oauthlib.flow": module}), patch.object(gs, "_PrivateVault", return_value=vault), \
                patch.object(gs, "dependencies_available", return_value=True), \
                patch.object(gs, "_run_oauth", return_value=credentials), patch.object(gs, "_authorized_transport", return_value=FakeTransport()):
            gs.GoogleSheetsClient.connect(config(), self.root)
        self.assertEqual(factory.call_args.kwargs["scopes"], gs.SCOPES)
        self.assertTrue(factory.call_args.kwargs["autogenerate_code_verifier"])
        self.assertFalse(flow.oauth2session.trust_env)
        self.assertEqual(vault.state["book"], state["book"])
        self.assertEqual(vault.state["connection_id"], state["connection_id"])

    def test_oauth_exception_secrets_are_never_exposed_or_saved(self):
        vault = MemoryVault()
        flow = SimpleNamespace(oauth2session=SimpleNamespace())
        module = SimpleNamespace(InstalledAppFlow=SimpleNamespace(from_client_config=lambda *a, **k: flow))
        with patch.dict(sys.modules, {"google_auth_oauthlib.flow": module}), patch.object(gs, "_PrivateVault", return_value=vault), \
                patch.object(gs, "dependencies_available", return_value=True), \
                patch.object(gs, "_run_oauth", side_effect=RuntimeError(SECRET)):
            with self.assertRaises(gs.GoogleShareError) as caught:
                gs.GoogleSheetsClient.connect(config(), self.root)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertIsNone(vault.state)

    def test_fake_dpapi_vault_contains_no_plaintext_and_is_root_namespaced(self):
        private = Path(self.tmp.name) / "private"
        def cipher(data, *, decrypt=False):
            if decrypt:
                self.assertTrue(data.startswith(b"ENCRYPTED:"))
                data = data[10:]
            encoded = bytes(value ^ 0xA5 for value in data)
            return encoded if decrypt else b"ENCRYPTED:" + encoded
        with patch.object(gs, "_private_directory", return_value=private), patch.object(gs, "_dpapi", side_effect=cipher):
            vault = gs._PrivateVault(self.root)
            value = initial_state(self.root)
            vault.write(value)
            self.assertNotIn(SECRET.encode(), vault.path.read_bytes())
            self.assertEqual(vault.read(), value)
            self.assertEqual(list(private.glob("*.tmp")), [])
            vault.delete()
            self.assertIsNone(vault.read())
        self.assertNotEqual(gs._project_key(self.root), gs._project_key(self.root / "demo"))

    def test_status_is_network_free_and_suppresses_corrupt_vault_details(self):
        state = initial_state(self.root)
        state["book"] = {"spreadsheet_id": BOOK_ID, "status": "ready"}
        with patch.object(gs, "_PrivateVault", return_value=MemoryVault(state)), patch.object(gs, "_authorized_transport") as transport:
            status = gs.connection_status(self.root)
            self.assertTrue(status["connected"])
            self.assertEqual(status["spreadsheet_id"], BOOK_ID)
            transport.assert_not_called()
        broken = MagicMock()
        broken.read.side_effect = gs.GoogleShareError("safe", code="private_storage")
        with patch.object(gs, "_PrivateVault", return_value=broken):
            self.assertFalse(gs.connection_status(self.root)["connected"])

    def test_logging_suppression_restores_previous_threshold(self):
        previous = logging.root.manager.disable
        with gs._no_sensitive_logs():
            self.assertGreaterEqual(logging.root.manager.disable, logging.CRITICAL)
        self.assertEqual(logging.root.manager.disable, previous)

    def test_optional_dedicated_directory_is_appended_lazily_once(self):
        directory = Path(self.tmp.name) / "optional"
        directory.mkdir()
        previous = list(sys.path)
        try:
            with patch.object(gs, "_optional_dependencies_directory", return_value=directory), \
                    patch.object(gs.importlib.util, "find_spec", return_value=object()):
                self.assertNotIn(str(directory), sys.path)
                self.assertTrue(gs.dependencies_available())
                self.assertTrue(gs.dependencies_available())
            self.assertEqual(sys.path.count(str(directory)), 1)
            self.assertEqual(sys.path[:-1], previous)
        finally:
            sys.path[:] = previous

    def test_missing_dependencies_stop_oauth_and_saved_connection(self):
        vault = MemoryVault(initial_state(self.root))
        with patch.object(gs, "_PrivateVault", return_value=vault), patch.object(gs, "dependencies_available", return_value=False), \
                patch.object(gs, "_run_oauth") as oauth, patch.object(gs, "_authorized_transport") as transport:
            for action in (lambda: gs.GoogleSheetsClient.connect(config(), self.root),
                           lambda: gs.GoogleSheetsClient.from_saved_connection(self.root)):
                with self.assertRaises(gs.GoogleShareError) as caught:
                    action()
                self.assertEqual(caught.exception.code, "dependencies_missing")
            oauth.assert_not_called()
            transport.assert_not_called()

    def test_quiet_loopback_validates_state_and_uses_bounded_token_timeout(self):
        received = []
        class Server:
            server_port = 45678
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def handle_request(self):
                app({"PATH_INFO": "/", "QUERY_STRING": "state=wrong&code=private"}, lambda *args: received.append(args[0]))
                app({"PATH_INFO": "/", "QUERY_STRING": "&".join("x=1" for _ in range(21))}, lambda *args: received.append(args[0]))
                app({"PATH_INFO": "/", "QUERY_STRING": "state=expected&code=private"}, lambda *args: received.append(args[0]))
        def server_factory(host, port, application, **kwargs):
            nonlocal app
            self.assertEqual((host, port), ("127.0.0.1", 0))
            app = application
            return Server()
        app = None
        flow = SimpleNamespace(authorization_url=MagicMock(return_value=("https://accounts.google.com/o/oauth2/auth", "expected")),
                               fetch_token=MagicMock(), credentials="fake-credentials")
        with patch.object(gs, "make_server", side_effect=server_factory), patch.object(gs.webbrowser, "open", return_value=True):
            self.assertEqual(gs._run_oauth(flow), "fake-credentials")
        self.assertEqual(received, ["400 Bad Request", "400 Bad Request", "200 OK"])
        self.assertEqual(flow.redirect_uri, "http://127.0.0.1:45678/")
        self.assertEqual(flow.fetch_token.call_args.kwargs["timeout"], 30)

    def test_oauth_timeout_never_fetches_token(self):
        server = MagicMock()
        server.__enter__.return_value = server
        server.server_port = 12345
        flow = SimpleNamespace(authorization_url=MagicMock(return_value=("https://accounts.google.com/o/oauth2/auth", "expected")), fetch_token=MagicMock())
        with patch.object(gs, "make_server", return_value=server), patch.object(gs.webbrowser, "open", return_value=True), \
                patch.object(gs.time, "monotonic", side_effect=[0, 121]):
            with self.assertRaises(gs.GoogleShareError) as caught:
                gs._run_oauth(flow)
        self.assertEqual(caught.exception.code, "oauth_timeout")
        flow.fetch_token.assert_not_called()


if __name__ == "__main__":
    unittest.main()
