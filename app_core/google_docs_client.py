"""Revision-guarded, app-owned daily/weekly Google Docs publications.

Only plain-text report bodies are supported. Foreign structural elements,
suggestions and direct text edits stop publication instead of being erased.
This module performs no I/O at import time and adds no authentication scopes.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from app_core.google_sheets_client import (
    DOCS_API_BASE, GoogleShareError, GoogleSheetsClient, canonical_json, publication_hash,
)


KINDS = ("daily", "weekly")
_ID = re.compile(r"^[A-Za-z0-9_-]{5,160}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_PUBLICATION = re.compile(r"^proloag\.publication\.v1\.([0-9a-f]{64})\.([0-9a-f]{64})\.([0-9a-f]{64})$")
_UNSUPPORTED_TEXT = re.compile(r"[\x00-\x08\x0b-\x1f\ue000-\uf8ff\ud800-\udfff]")
MAX_REPORT_BYTES = 1_000_000


def _kind(kind: str) -> str:
    if kind not in KINDS:
        raise GoogleShareError("レポート種別が不正です。", code="invalid_report_kind")
    return kind


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _body_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _modified() -> GoogleShareError:
    return GoogleShareError("共有文書の本文・構造・管理情報が変更されています。上書きせず停止しました。別の文書で相談してください。", code="remote_modified")


def _contains_suggestions(value: Any) -> bool:
    if isinstance(value, dict):
        return any((str(key).startswith("suggested") and bool(item)) or _contains_suggestions(item)
                   for key, item in value.items())
    return isinstance(value, list) and any(_contains_suggestions(item) for item in value)


def _plain_document(raw: dict, document_id: str) -> dict:
    """Read a single plain body, preserving Docs' terminal newline and indices."""
    try:
        if raw.get("documentId") != document_id or not isinstance(raw.get("revisionId"), str) or not raw["revisionId"]:
            raise _modified()
        if _contains_suggestions(raw):
            raise _modified()
        tabs = raw.get("tabs")
        if tabs is not None:
            if not isinstance(tabs, list) or len(tabs) != 1 or tabs[0].get("childTabs"):
                raise _modified()
            tab_id = tabs[0].get("tabProperties", {}).get("tabId")
            if not isinstance(tab_id, str) or not tab_id:
                raise _modified()
            document = tabs[0]["documentTab"]
        else:
            tab_id, document = None, raw
        for source in (raw, document):
            if any(source.get(key) for key in ("headers", "footers", "footnotes", "inlineObjects", "positionedObjects", "lists", "commentAnchors")):
                raise _modified()
        content = document["body"]["content"]
        if not isinstance(content, list) or len(content) < 2:
            raise _modified()
        # The initial section break is mandatory and is never deleted.
        first = content[0]
        if set(first) - {"startIndex", "endIndex", "sectionBreak"} or first.get("startIndex", 0) != 0 or first.get("endIndex") != 1 or "sectionBreak" not in first:
            raise _modified()
        index, chunks = 1, []
        for element in content[1:]:
            if set(element) - {"startIndex", "endIndex", "paragraph"} or element.get("startIndex") != index:
                raise _modified()
            paragraph = element["paragraph"]
            if paragraph.get("bullet") or paragraph.get("positionedObjectIds"):
                raise _modified()
            runs = paragraph.get("elements")
            if not isinstance(runs, list) or not runs:
                raise _modified()
            paragraph_text = []
            for run in runs:
                if set(run) - {"startIndex", "endIndex", "textRun"} or run.get("startIndex") != index:
                    raise _modified()
                text = run["textRun"]["content"]
                if not isinstance(text, str) or not text:
                    raise _modified()
                index += utf16_length(text)
                if run.get("endIndex") != index:
                    raise _modified()
                paragraph_text.append(text)
            joined = "".join(paragraph_text)
            if not joined.endswith("\n") or element.get("endIndex") != index:
                raise _modified()
            chunks.append(joined)
        text = "".join(chunks)
        if not text.endswith("\n") or len(text.encode("utf-8")) > MAX_REPORT_BYTES:
            raise _modified()
        named = document.get("namedRanges", {})
        if not isinstance(named, dict):
            raise _modified()
        markers = {}
        for name, group in named.items():
            entries = group.get("namedRanges", [])
            if group.get("name") != name or len(entries) != 1:
                raise _modified()
            entry = entries[0]
            ranges = entry.get("ranges", [])
            if (entry.get("name") != name or not isinstance(entry.get("namedRangeId"), str)
                    or not entry["namedRangeId"] or len(ranges) != 1):
                raise _modified()
            region = ranges[0]
            if (region.get("startIndex") != 1 or region.get("endIndex") != index
                    or region.get("segmentId", "") or region.get("tabId", tab_id) != tab_id):
                raise _modified()
            markers[name] = entry["namedRangeId"]
        return {"text": text, "end_index": index, "tab_id": tab_id,
                "revision_id": raw["revisionId"], "markers": markers}
    except GoogleShareError:
        raise
    except Exception:
        raise _modified() from None


class GoogleDocsClient(GoogleSheetsClient):
    def _docs_request(self, method: str, suffix: str = "", **kwargs: Any) -> dict:
        return self._request(method, suffix, _api_base=DOCS_API_BASE, **kwargs)

    def _entry(self, kind: str, *, required: bool = True) -> dict | None:
        _kind(kind)
        registry = self._state.get("docs", {})
        if not isinstance(registry, dict):
            raise GoogleShareError("共有文書の登録情報を確認できません。", code="ownership")
        entry = registry.get(kind)
        if entry is None and not required:
            return None
        if (not isinstance(entry, dict) or not _ID.fullmatch(str(entry.get("document_id", "")))
                or entry.get("status") not in {"initializing", "ready"}):
            raise GoogleShareError("このアプリが作成した共有文書が登録されていません。", code="ownership")
        return entry

    def _owner_name(self, kind: str) -> str:
        return f"proloag.owner.v1.{self._state['connection_id']}.{kind}"

    def _read(self, kind: str) -> dict:
        entry = self._entry(kind)
        raw = self._docs_request("GET", "/" + entry["document_id"], params={
            "includeTabsContent": "true", "suggestionsViewMode": "SUGGESTIONS_INLINE"})
        return _plain_document(raw, entry["document_id"])

    @staticmethod
    def _range(view: dict, end_index: int | None = None, start_index: int = 1) -> dict:
        region = {"startIndex": start_index, "endIndex": end_index if end_index is not None else view["end_index"]}
        if view["tab_id"] is not None:
            region["tabId"] = view["tab_id"]
        return region

    def _publication(self, kind: str, view: dict) -> dict:
        markers = view["markers"]
        owner = self._owner_name(kind)
        if owner not in markers:
            raise _modified()
        others = [name for name in markers if name != owner]
        if not others and view["text"] == "\n":
            return {}
        if len(others) != 1:
            raise _modified()
        match = _PUBLICATION.fullmatch(others[0])
        if not match or _body_hash(view["text"]) != match.group(3):
            raise _modified()
        entry = self._entry(kind)
        return {"schema_version": 1, "kind": kind, "snapshot_id": match.group(1),
                "payload_hash": match.group(2), "body_hash": match.group(3),
                "document_id": entry["document_id"], "url": f"https://docs.google.com/document/d/{entry['document_id']}/edit"}

    def create_report_document(self, kind: str) -> dict:
        """Create once; never retry an unknown create or adopt an arbitrary ID."""
        _kind(kind)
        self._check_connection_current()
        entry = self._entry(kind, required=False)
        if entry is None:
            pending = self._state.setdefault("docs_creation_pending", {})
            if not isinstance(pending, dict):
                raise GoogleShareError("共有文書の作成状態を確認できません。", code="ownership")
            if pending.get(kind):
                raise GoogleShareError("前回の文書作成結果が不明です。自動再作成は行いません。", code="creation_unknown", uncertain=True)
            pending[kind] = True
            self._save()
            try:
                created = self._docs_request("POST", json={"title": "PROLOAG 日次共有" if kind == "daily" else "PROLOAG 週次共有"})
            except GoogleShareError as exc:
                if not exc.uncertain:
                    pending.pop(kind, None)
                    self._save()
                raise
            document_id = created.get("documentId")
            if not isinstance(document_id, str) or not _ID.fullmatch(document_id):
                raise GoogleShareError("文書作成結果を特定できません。自動再作成を停止しました。", code="creation_unknown", uncertain=True)
            entry = {"document_id": document_id, "status": "initializing"}
            self._state.setdefault("docs", {})[kind] = entry
            pending.pop(kind, None)
            try:
                self._save()
            except GoogleShareError:
                raise GoogleShareError("作成した文書の登録保存を確認できません。自動再作成を停止しました。", code="creation_unknown", uncertain=True) from None
        view = self._read(kind)
        if entry["status"] == "initializing" and not view["markers"]:
            if view["text"] != "\n":
                raise _modified()
            self._docs_request("POST", "/" + entry["document_id"] + ":batchUpdate", json={
                "writeControl": {"requiredRevisionId": view["revision_id"]},
                "requests": [{"createNamedRange": {"name": self._owner_name(kind), "range": self._range(view)}}]})
            view = self._read(kind)
        self._publication(kind, view)
        if entry["status"] != "ready":
            entry["status"] = "ready"
            self._save()
        return {"document_id": entry["document_id"], "url": f"https://docs.google.com/document/d/{entry['document_id']}/edit"}

    def read_report_publication(self, kind: str) -> dict:
        if self._entry(kind, required=False) is None:
            return {}
        return self._publication(kind, self._read(kind))

    def publish_report(self, kind: str, report: dict, expected_document_id: str) -> dict:
        entry = self._entry(kind)
        if entry["status"] != "ready" or entry["document_id"] != expected_document_id:
            raise GoogleShareError("確認した共有文書と現在の登録先が一致しません。", code="ownership")
        if (not isinstance(report, dict) or type(report.get("schema_version")) is not int or report["schema_version"] != 1
                or report.get("kind") != kind or not _HASH.fullmatch(str(report.get("snapshot_id", "")))
                or not isinstance(report.get("text"), str) or not report["text"].strip()
                or _UNSUPPORTED_TEXT.search(report["text"])):
            raise GoogleShareError("送信レポートの形式または文字が不正です。", code="invalid_report")
        if len(canonical_json(report)) > MAX_REPORT_BYTES:
            raise GoogleShareError("レポートが送信上限を超えています。", code="payload_too_large")
        text = report["text"] if report["text"].endswith("\n") else report["text"] + "\n"
        payload_hash = publication_hash(report)
        view = self._read(kind)
        previous = self._publication(kind, view)
        if previous.get("snapshot_id") == report["snapshot_id"] and previous.get("payload_hash") == payload_hash:
            return previous
        requests = []
        for marker_id in view["markers"].values():
            request = {"namedRangeId": marker_id}
            if view["tab_id"] is not None:
                request["tabsCriteria"] = {"tabIds": [view["tab_id"]]}
            requests.append({"deleteNamedRange": request})
        if view["end_index"] > 2:
            requests.append({"deleteContentRange": {"range": self._range(view, view["end_index"] - 1)}})
        location = {"index": 1}
        if view["tab_id"] is not None:
            location["tabId"] = view["tab_id"]
        # Preserve the mandatory terminal newline rather than inserting an extra.
        requests.append({"insertText": {"location": location, "text": text[:-1]}})
        new_end = 1 + utf16_length(text)
        region = self._range(view, new_end)
        requests.append({"updateTextStyle": {"range": region, "textStyle": {}, "fields": "*"}})
        requests.append({"updateParagraphStyle": {"range": region, "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "fields": "namedStyleType"}})
        headings = report.get("heading_lines", [])
        if not isinstance(headings, list) or any(not isinstance(line, str) for line in headings):
            raise GoogleShareError("見出し設定が不正です。", code="invalid_report")
        index = 1
        for number, line in enumerate(segment + "\n" for segment in text[:-1].split("\n")):
            end = index + utf16_length(line)
            style = "TITLE" if number == 0 else "HEADING_1" if line.rstrip("\n") in headings else None
            if style:
                requests.append({"updateParagraphStyle": {"range": self._range(view, end, index),
                    "paragraphStyle": {"namedStyleType": style}, "fields": "namedStyleType"}})
            index = end
        body_hash = _body_hash(text)
        publication_name = f"proloag.publication.v1.{report['snapshot_id']}.{payload_hash}.{body_hash}"
        for name in (self._owner_name(kind), publication_name):
            requests.append({"createNamedRange": {"name": name, "range": region}})
        body = {"writeControl": {"requiredRevisionId": view["revision_id"]}, "requests": requests}
        if len(canonical_json(body)) > MAX_REPORT_BYTES * 2:
            raise GoogleShareError("レポートが送信上限を超えています。", code="payload_too_large")
        self._docs_request("POST", "/" + entry["document_id"] + ":batchUpdate", json=body)
        return {"schema_version": 1, "kind": kind, "snapshot_id": report["snapshot_id"],
                "payload_hash": payload_hash, "body_hash": body_hash, "document_id": entry["document_id"],
                "url": f"https://docs.google.com/document/d/{entry['document_id']}/edit"}
