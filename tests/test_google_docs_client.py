"""Synthetic Google Docs API contract tests; no real OAuth/network/files."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app_core import google_docs_client as docs
from app_core import google_sheets_client as sheets
from tests.test_google_sheets_client import MemoryVault, FakeResponse, SECRET, initial_state, config, credentials_payload


def report(kind="daily", text="学習レポート 🧠\n集計\n学習時間: 1.5時間\n"):
    return {"schema_version": 1, "kind": kind, "snapshot_id": "b" * 64,
            "generated_at": "2026-09-16T12:00:00+09:00", "title": "学習レポート",
            "text": text, "period": {"start": "2026-09-16", "end": "2026-09-16"},
            "counts": {"study_sessions": 1}, "heading_lines": ["集計"],
            "options": {"include_content": False}, "source_snapshot_id": "c" * 64}


def utf16_slice(value, start, end=None):
    return value.encode("utf-16-le")[start * 2: None if end is None else end * 2].decode("utf-16-le")


class FakeDocsTransport:
    def __init__(self):
        self.documents = {}
        self.calls = []
        self.next_id = 1
        self.next_marker = 1
        self.lose_after = None
        self.conflict_on_batch = False
        self.status = None
        self.raw_mutation = None
        self.fail_get = False
        self.initial_text = "\n"

    def raw(self, document_id):
        state = self.documents[document_id]
        content = [{"sectionBreak": {"sectionStyle": {}}, "endIndex": 1}]
        index = 1
        for piece in state["text"][:-1].split("\n"):
            text = piece + "\n"
            end = index + docs.utf16_length(text)
            content.append({"startIndex": index, "endIndex": end,
                "paragraph": {"elements": [{"startIndex": index, "endIndex": end, "textRun": {"content": text, "textStyle": {}}}],
                              "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}}})
            index = end
        named = {}
        for marker_id, marker in state["markers"].items():
            name = marker["name"]
            named.setdefault(name, {"name": name, "namedRanges": []})["namedRanges"].append(
                {"name": name, "namedRangeId": marker_id, "ranges": [deepcopy(marker["range"])]})
        raw = {"documentId": document_id, "revisionId": f"revision-{state['revision']}",
               "tabs": [{"tabProperties": {"tabId": "t.0", "title": "Tab 1"},
                         "documentTab": {"body": {"content": content}, "namedRanges": named}}]}
        if self.raw_mutation:
            self.raw_mutation(raw)
        return raw

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, deepcopy(kwargs)))
        if self.status:
            status, self.status = self.status, None
            return FakeResponse(status=status)
        if url == sheets.DOCS_API_BASE and method == "POST":
            document_id = f"fake_document_{self.next_id}"
            self.next_id += 1
            self.documents[document_id] = {"text": self.initial_text, "revision": 1, "markers": {}}
            result, operation = {"documentId": document_id}, "create"
        elif method == "GET":
            if self.fail_get:
                raise RuntimeError(SECRET)
            document_id = url.rsplit("/", 1)[-1]
            result, operation = self.raw(document_id), "get"
        elif url.endswith(":batchUpdate"):
            document_id = url.rsplit("/", 1)[-1].split(":")[0]
            current = self.documents[document_id]
            if self.conflict_on_batch:
                current["revision"] += 1
                self.conflict_on_batch = False
            body = kwargs["json"]
            if body["writeControl"]["requiredRevisionId"] != f"revision-{current['revision']}":
                return FakeResponse(status=400)
            updated = deepcopy(current)
            for request in body["requests"]:
                if "deleteNamedRange" in request:
                    del updated["markers"][request["deleteNamedRange"]["namedRangeId"]]
                elif "deleteContentRange" in request:
                    region = request["deleteContentRange"]["range"]
                    self.assert_range(region, updated["text"], deletion=True)
                    updated["text"] = utf16_slice(updated["text"], 0, region["startIndex"] - 1) + utf16_slice(updated["text"], region["endIndex"] - 1)
                elif "insertText" in request:
                    instruction = request["insertText"]
                    index = instruction["location"]["index"] - 1
                    updated["text"] = utf16_slice(updated["text"], 0, index) + instruction["text"] + utf16_slice(updated["text"], index)
                elif "createNamedRange" in request:
                    instruction = request["createNamedRange"]
                    self.assert_range(instruction["range"], updated["text"])
                    if docs.utf16_length(instruction["name"]) > 256:
                        raise AssertionError("Named range name too long")
                    updated["markers"][f"range-{self.next_marker}"] = deepcopy(instruction)
                    self.next_marker += 1
                elif "updateParagraphStyle" in request or "updateTextStyle" in request:
                    self.assert_range(next(iter(request.values()))["range"], updated["text"])
                else:
                    raise AssertionError("Unexpected API operation")
            updated["revision"] += 1
            self.documents[document_id] = updated
            result, operation = {"documentId": document_id, "writeControl": {"requiredRevisionId": f"revision-{updated['revision']}"}}, "batch"
        else:
            raise AssertionError("Unexpected API destination")
        if self.lose_after == operation:
            self.lose_after = None
            raise RuntimeError(SECRET)
        return FakeResponse(result)

    @staticmethod
    def assert_range(region, text, deletion=False):
        assert region.get("tabId") == "t.0"
        assert 1 <= region["startIndex"] < region["endIndex"] <= 1 + docs.utf16_length(text)
        if deletion:
            assert region["endIndex"] <= docs.utf16_length(text), "Cannot delete final newline"
        # Decode slices to reject indices that split surrogate pairs.
        utf16_slice(text, region["startIndex"] - 1, region["endIndex"] - 1)


class GoogleDocsClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.vault = MemoryVault(initial_state(self.root))
        self.transport = FakeDocsTransport()
        self.client = docs.GoogleDocsClient(root=self.root, transport=self.transport, vault=self.vault, state=self.vault.read())

    def create(self, kind="daily"):
        return self.client.create_report_document(kind)["document_id"]

    def writes(self):
        return [call for call in self.transport.calls if call[0] == "POST"]

    def publish(self, kind="daily", payload=None):
        document_id = self.create(kind)
        result = self.client.publish_report(kind, payload or report(kind), document_id)
        return document_id, result

    def test_only_two_fixed_kinds_registered_and_reused(self):
        daily = self.create()
        weekly = self.create("weekly")
        self.assertNotEqual(daily, weekly)
        self.assertEqual(self.create(), daily)
        self.assertEqual(self.create("weekly"), weekly)
        self.assertEqual(set(self.vault.state["docs"]), {"daily", "weekly"})
        self.assertEqual(len(self.transport.documents), 2)
        with self.assertRaises(sheets.GoogleShareError):
            self.create("anything_else")
        self.assertEqual(self.client.read_report_publication("daily"), {})

    def test_unregistered_read_is_empty_and_publish_cannot_adopt_existing_doc(self):
        self.assertEqual(self.client.read_report_publication("weekly"), {})
        with self.assertRaises(sheets.GoogleShareError) as caught:
            self.client.publish_report("daily", report(), "some_other_document")
        self.assertEqual(caught.exception.code, "ownership")
        self.assertEqual(self.transport.calls, [])

    def test_full_plaintext_publish_revision_bound_atomic_markers_utf16_styles(self):
        document_id = self.create()
        payload = report()
        original = deepcopy(payload)
        result = self.client.publish_report("daily", payload, document_id)
        state = self.transport.documents[document_id]
        self.assertEqual(state["text"], payload["text"])
        self.assertEqual(len(state["markers"]), 2)
        batch = self.writes()[-1][2]["json"]
        self.assertEqual(batch["writeControl"], {"requiredRevisionId": "revision-2"})
        self.assertEqual([next(iter(item)) for item in batch["requests"]][-2:], ["createNamedRange", "createNamedRange"])
        styles = [item["updateParagraphStyle"]["paragraphStyle"]["namedStyleType"] for item in batch["requests"] if "updateParagraphStyle" in item]
        self.assertEqual(styles, ["NORMAL_TEXT", "TITLE", "HEADING_1"])
        self.assertEqual(result["payload_hash"], sheets.publication_hash(payload))
        self.assertEqual(self.client.read_report_publication("daily"), result)
        self.assertEqual(payload, original)
        for _, url, kwargs in self.transport.calls:
            self.assertTrue(url.startswith(sheets.DOCS_API_BASE))
            self.assertFalse(kwargs["allow_redirects"])

    def test_repeated_payload_does_not_append_or_write(self):
        document_id, first = self.publish()
        before = len(self.writes())
        second = self.client.publish_report("daily", report(), document_id)
        self.assertEqual(second, first)
        self.assertEqual(len(self.writes()), before)

    def test_new_payload_replaces_old_and_preserves_final_newline(self):
        document_id, _ = self.publish()
        previous_length = docs.utf16_length(self.transport.documents[document_id]["text"])
        payload = report(text="短い 🧠 レポート")
        payload["snapshot_id"] = "d" * 64
        self.client.publish_report("daily", payload, document_id)
        self.assertEqual(self.transport.documents[document_id]["text"], payload["text"] + "\n")
        requests = self.writes()[-1][2]["json"]["requests"]
        deleted = next(item["deleteContentRange"] for item in requests if "deleteContentRange" in item)
        self.assertEqual(deleted["range"]["endIndex"], previous_length)

    def test_publish_response_loss_reconciles_via_remote_markers(self):
        document_id = self.create()
        self.transport.lose_after = "batch"
        before = len(self.writes())
        with self.assertRaises(sheets.GoogleShareError) as caught:
            self.client.publish_report("daily", report(), document_id)
        self.assertTrue(caught.exception.uncertain)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(len(self.writes()), before + 1)
        found = self.client.read_report_publication("daily")
        self.assertEqual(found["payload_hash"], sheets.publication_hash(report()))
        self.client.publish_report("daily", report(), document_id)
        self.assertEqual(len(self.writes()), before + 1)

    def test_unknown_creation_does_not_repeat_even_from_reloaded_client(self):
        self.transport.lose_after = "create"
        with self.assertRaises(sheets.GoogleShareError) as caught:
            self.create()
        self.assertTrue(caught.exception.uncertain)
        self.assertTrue(self.vault.state["docs_creation_pending"]["daily"])
        second = docs.GoogleDocsClient(root=self.root, transport=self.transport, vault=self.vault, state=self.vault.read())
        before = len(self.transport.calls)
        with self.assertRaises(sheets.GoogleShareError) as caught:
            second.create_report_document("daily")
        self.assertEqual(caught.exception.code, "creation_unknown")
        self.assertEqual(len(self.transport.calls), before)

    def test_initial_owner_batch_response_loss_can_recover_known_id(self):
        self.transport.lose_after = "batch"
        with self.assertRaises(sheets.GoogleShareError):
            self.create()
        entry = self.vault.state["docs"]["daily"]
        self.assertEqual(entry["status"], "initializing")
        second = docs.GoogleDocsClient(root=self.root, transport=self.transport, vault=self.vault, state=self.vault.read())
        destination = second.create_report_document("daily")
        self.assertEqual(destination["document_id"], entry["document_id"])
        self.assertEqual(len(self.transport.documents), 1)
        self.assertEqual(self.vault.state["docs"]["daily"]["status"], "ready")
        self.assertEqual(len(self.transport.documents[entry["document_id"]]["markers"]), 1)

    def test_nonempty_new_document_is_never_initialized(self):
        self.transport.initial_text = "Someone's content\n"
        with self.assertRaises(sheets.GoogleShareError) as caught:
            self.create()
        self.assertEqual(caught.exception.code, "remote_modified")
        self.assertEqual(len(self.writes()), 1)

    def test_direct_text_edit_and_removed_owner_stop_before_mutation(self):
        document_id, _ = self.publish()
        baseline = deepcopy(self.transport.documents[document_id])
        self.transport.documents[document_id]["text"] = baseline["text"].replace("1.5", "9.5")
        before = len(self.writes())
        with self.assertRaises(sheets.GoogleShareError) as caught:
            self.client.publish_report("daily", report(), document_id)
        self.assertEqual(caught.exception.code, "remote_modified")
        self.assertEqual(len(self.writes()), before)
        self.transport.documents[document_id] = baseline
        markers = self.transport.documents[document_id]["markers"]
        del markers[next(key for key, value in markers.items() if value["name"].startswith("proloag.owner"))]
        with self.assertRaises(sheets.GoogleShareError):
            self.client.publish_report("daily", report(), document_id)
        self.assertEqual(len(self.writes()), before)

    def test_unsupported_structures_tabs_and_suggestions_stop_before_write(self):
        document_id, _ = self.publish()
        mutations = [
            lambda raw: raw["tabs"].append(deepcopy(raw["tabs"][0])),
            lambda raw: raw["tabs"][0].update(childTabs=[{}]),
            lambda raw: raw["tabs"][0]["documentTab"].update(headers={"h": {}}),
            lambda raw: raw["tabs"][0]["documentTab"].update(inlineObjects={"i": {}}),
            lambda raw: raw["tabs"][0]["documentTab"]["body"]["content"][1].update(table={}),
            lambda raw: raw["tabs"][0]["documentTab"]["body"]["content"][1]["paragraph"]["elements"][0].update(suggestedInsertionIds=["s"]),
            lambda raw: raw["tabs"][0]["documentTab"]["body"]["content"][1]["paragraph"]["elements"][0].update(inlineObjectElement={"inlineObjectId": "i"}),
        ]
        before = len(self.writes())
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.transport.raw_mutation = mutation
                with self.assertRaises(sheets.GoogleShareError) as caught:
                    self.client.publish_report("daily", report(), document_id)
                self.assertEqual(caught.exception.code, "remote_modified")
                self.assertEqual(len(self.writes()), before)

    def test_foreign_named_range_and_duplicate_owner_stop(self):
        document_id, _ = self.publish()
        baseline = deepcopy(self.transport.documents[document_id]["markers"])
        owner = next(value for value in baseline.values() if value["name"].startswith("proloag.owner"))
        for name in ("my_annotation", owner["name"]):
            self.transport.documents[document_id]["markers"] = deepcopy(baseline)
            self.transport.documents[document_id]["markers"]["foreign"] = dict(deepcopy(owner), name=name)
            with self.assertRaises(sheets.GoogleShareError) as caught:
                self.client.read_report_publication("daily")
            self.assertEqual(caught.exception.code, "remote_modified")

    def test_revision_conflict_is_atomic_and_does_not_auto_retry(self):
        document_id, _ = self.publish()
        before_text = self.transport.documents[document_id]["text"]
        self.transport.conflict_on_batch = True
        before = len(self.writes())
        with self.assertRaises(sheets.GoogleShareError) as caught:
            self.client.publish_report("daily", report(text="Next report\n"), document_id)
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(self.transport.documents[document_id]["text"], before_text)
        self.assertEqual(len(self.writes()), before + 1)

    def test_wrong_expected_id_report_kind_controls_or_size_rejected(self):
        document_id = self.create()
        before = len(self.writes())
        with self.assertRaises(sheets.GoogleShareError):
            self.client.publish_report("daily", report(), "someone_else_document")
        for payload in (report("weekly"), report(text="bad\x01text\n"), report(text="bad\ue000text\n"), report(text="")):
            with self.assertRaises(sheets.GoogleShareError):
                self.client.publish_report("daily", payload, document_id)
        with patch.object(docs, "MAX_REPORT_BYTES", 100):
            with self.assertRaises(sheets.GoogleShareError) as caught:
                self.client.publish_report("daily", report(), document_id)
            self.assertEqual(caught.exception.code, "payload_too_large")
        self.assertEqual(len(self.writes()), before)

    def test_read_failure_is_certain_and_secret_free(self):
        self.create()
        self.transport.fail_get = True
        with self.assertRaises(sheets.GoogleShareError) as caught:
            self.client.read_report_publication("daily")
        self.assertFalse(caught.exception.uncertain)
        self.assertNotIn(SECRET, str(caught.exception))

    def test_stale_sheets_client_cannot_erase_docs_registry_or_make_requests(self):
        stale = sheets.GoogleSheetsClient(root=self.root, transport=self.transport, vault=self.vault, state=self.vault.read())
        self.create()
        before = len(self.transport.calls)
        with self.assertRaises(sheets.GoogleShareError) as caught:
            stale._request("GET", "/fake_spreadsheet")
        self.assertEqual(caught.exception.code, "connection_stale")
        self.assertEqual(len(self.transport.calls), before)
        with self.assertRaises(sheets.GoogleShareError):
            stale._save()
        self.assertIn("daily", self.vault.state["docs"])

    def test_saved_connection_factory_returns_subclass_without_network(self):
        with patch.object(sheets, "_PrivateVault", return_value=self.vault), patch.object(sheets, "dependencies_available", return_value=True), \
                patch.object(sheets, "_authorized_transport", return_value=self.transport):
            client = docs.GoogleDocsClient.from_saved_connection(self.root)
        self.assertIsInstance(client, docs.GoogleDocsClient)
        self.assertEqual(self.transport.calls, [])

    def test_reauthorization_preserves_docs_registry_and_rejects_changed_client(self):
        self.create()
        state = self.vault.read()
        state["docs_creation_pending"]["weekly"] = True
        self.vault.write(state)
        with patch.object(sheets, "_PrivateVault", return_value=self.vault):
            with self.assertRaises(sheets.GoogleShareError) as caught:
                sheets.GoogleSheetsClient.connect(config("other-client"), self.root)
            self.assertEqual(caught.exception.code, "oauth_client_mismatch")
        flow = SimpleNamespace(oauth2session=SimpleNamespace())
        module = SimpleNamespace(InstalledAppFlow=SimpleNamespace(from_client_config=MagicMock(return_value=flow)))
        credentials = SimpleNamespace(to_json=lambda: json.dumps(credentials_payload()))
        with patch.dict(sys.modules, {"google_auth_oauthlib.flow": module}), patch.object(sheets, "_PrivateVault", return_value=self.vault), \
                patch.object(sheets, "dependencies_available", return_value=True), patch.object(sheets, "_run_oauth", return_value=credentials), \
                patch.object(sheets, "_authorized_transport", return_value=self.transport):
            sheets.GoogleSheetsClient.connect(config(), self.root)
        self.assertEqual(self.vault.state["docs"], state["docs"])
        self.assertEqual(self.vault.state["docs_creation_pending"], {"weekly": True})
        self.assertEqual(self.vault.state["connection_id"], state["connection_id"])

    def test_connection_status_exposes_only_safe_document_destinations(self):
        document_id = self.create()
        with patch.object(sheets, "_PrivateVault", return_value=self.vault):
            status = sheets.connection_status(self.root)
        self.assertEqual(status["docs"]["daily"]["document_id"], document_id)
        self.assertNotIn(SECRET, json.dumps(status))


if __name__ == "__main__":
    unittest.main()
