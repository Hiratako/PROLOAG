"""Strict, append-only AI roadmap imports with preview-bound atomic saves.

Files are data only. Parsing and previewing never write app records. Imported
source identities survive subsequent edits/deletions in a separate ledger;
existing roadmap entries, allocations, daily plans and logs are not replaced.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import unicodedata
from uuid import NAMESPACE_URL, uuid4, uuid5

from app_core import json_store as store
from app_core.json_operations import atomic_json_operation
from app_core.roadmap_planning import require_schedule_order
from app_core.roadmap_service import empty_roadmap, parse_day, validate_roadmap


FORMAT = "proloag.study-roadmap"
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 500
MAX_MILESTONES = 100
LEDGER_PATH = "settings/roadmap_imports.json"
ROADMAP_PATH = "settings/study_roadmap.json"
MASTER_PATH = "master_data/master.json"
CLASS_NAMES = ("project", "subject", "material", "unit")
CLASS_KEYS = tuple(name + "_id" for name in CLASS_NAMES)
CLASS_COLLECTIONS = ("projects", "subjects", "materials", "units")
_ROOT_KEYS = {"format", "schema_version", "plan_title", "items", "milestones"}
_ITEM_KEYS = {
    "ref", "title", "start_date", "end_date", "deadline", "planned_minutes",
    "status", "progress_kind", "completed", "total", "unit", "classification",
    "predecessor_refs", "memo",
}
_MILESTONE_KEYS = {"ref", "title", "date", "project", "memo"}
_SHA = re.compile(r"[0-9a-f]{64}")
_OPERATION = re.compile(r"[0-9a-f]{32}")
_SECRET = os.urandom(32)


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _keys(value, expected, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label}はJSONオブジェクトで指定してください。")
    missing, extra = expected - set(value), set(value) - expected
    if missing:
        raise ValueError(f"{label}：必須項目がありません：{', '.join(sorted(missing))}。")
    if extra:
        raise ValueError(f"{label}：未対応の項目です：{', '.join(sorted(extra))}。")


def _text(value, label, maximum=200, *, blank=False):
    if (not isinstance(value, str) or "\x00" in value or len(value) > maximum
            or (not blank and not value.strip())):
        raise ValueError(f"{label}は{'0' if blank else '1'}〜{maximum}文字の文字列で指定してください。")
    if not blank and value != value.strip():
        raise ValueError(f"{label}：前後の空白を除いてください。")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError(f"{label}：無効な文字が含まれています。") from exc
    return value


def _name(value, label):
    return None if value is None else _text(value, label)


def _number(value, label, minimum=0, maximum=1e9):
    if type(value) not in (int, float):
        raise ValueError(f"{label}は数値で指定してください。")
    try:
        valid = minimum <= value <= maximum and math.isfinite(value)
    except (OverflowError, TypeError):
        valid = False
    if not valid:
        raise ValueError(f"{label}は{minimum:g}〜{maximum:g}で指定してください。")
    return value


def _date(value, label):
    try:
        return parse_day(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{label}：1900〜2199年のYYYY-MM-DD形式で指定してください。") from exc


def validate_import_document(document: dict) -> dict:
    """Validate the complete interchange schema without changing any values."""
    # JSON escapes may decode to lone UTF-16 surrogates even in valid UTF-8
    # source bytes. Validate every key/value before using any untrusted name
    # in a diagnostic, otherwise rendering that diagnostic can itself fail.
    try:
        _canonical(document)
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError) as exc:
        raise ValueError("JSONに無効な数値・文字・深すぎる構造があります。文字のエスケープも確認してください。") from exc
    _keys(document, _ROOT_KEYS, "ファイル全体")
    if document["format"] != FORMAT:
        raise ValueError(f"formatは「{FORMAT}」にしてください。")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("schema_versionは整数の1にしてください。")
    _text(document["plan_title"], "計画名")
    for collection, maximum in (("items", MAX_ITEMS), ("milestones", MAX_MILESTONES)):
        if not isinstance(document[collection], list) or len(document[collection]) > maximum:
            raise ValueError(f"{collection}は{maximum}件以内の配列で指定してください。")
    if not document["items"] and not document["milestones"]:
        raise ValueError("学習工程または試験・節目を1件以上指定してください。")
    refs, titles = {}, set()
    for number, row in enumerate(document["items"], 1):
        fallback = f"工程{number}"
        label = row.get("ref", fallback) if isinstance(row, dict) else fallback
        _keys(row, _ITEM_KEYS, str(label))
        ref = row["ref"]
        if not isinstance(ref, str) or not re.fullmatch(r"E[0-9]{2,4}", ref):
            raise ValueError(f"{fallback}：refはE01のようなE＋2〜4桁の番号にしてください。")
        if ref in refs:
            raise ValueError(f"{ref}：工程番号が重複しています。")
        refs[ref] = row
        _text(row["title"], f"{ref} 工程名")
        if row["title"] in titles:
            raise ValueError(f"{ref}：同じ工程名が複数あります。対象範囲を区別してください。")
        titles.add(row["title"])
        start = _date(row["start_date"], f"{ref} 開始日")
        end = _date(row["end_date"], f"{ref} 終了予定日")
        if start > end:
            raise ValueError(f"{ref}：開始日が終了予定日より後です。")
        if row["deadline"] is not None and end > _date(row["deadline"], f"{ref} 確定期限"):
            raise ValueError(f"{ref}：終了予定日が確定期限を超えています。")
        if type(row["planned_minutes"]) is not int or not 0 <= row["planned_minutes"] <= 600000:
            raise ValueError(f"{ref}：予定学習時間は0〜600000の整数の分で指定してください。")
        if not isinstance(row["status"], str) or row["status"] not in {"not_started", "in_progress", "done", "pending"}:
            raise ValueError(f"{ref}：状態はnot_started/in_progress/done/pendingのいずれかです。")
        if not isinstance(row["progress_kind"], str) or row["progress_kind"] not in {"quantity", "percent"}:
            raise ValueError(f"{ref}：進捗方式はquantity/percentのいずれかです。")
        _number(row["total"], f"{ref} 総量", 0.000001)
        _number(row["completed"], f"{ref} 完了数", maximum=row["total"])
        _text(row["unit"], f"{ref} 単位", 20)
        if row["progress_kind"] == "percent" and (row["total"] != 100 or row["unit"] != "%"):
            raise ValueError(f"{ref}：割合で管理する場合は総量100・単位%にしてください。")
        if row["status"] == "done" and row["completed"] != row["total"]:
            raise ValueError(f"{ref}：完了の工程は完了数と総量を一致させてください。")
        _keys(row["classification"], set(CLASS_NAMES), f"{ref} 分類")
        for key in CLASS_NAMES:
            _name(row["classification"][key], f"{ref} 分類 {key}")
        _text(row["memo"], f"{ref} メモ", 20000, blank=True)
        parents = row["predecessor_refs"]
        if (not isinstance(parents, list) or len(parents) > 50
                or any(not isinstance(parent, str) for parent in parents)
                or len(set(parents)) != len(parents)):
            raise ValueError(f"{ref}：前提工程は重複のない50件以内の工程番号一覧にしてください。")
    # Validate the graph before hashing or resolving any references.
    done, pending = set(), set()

    def visit(ref):
        if ref in pending:
            raise ValueError(f"{ref}：前提工程が循環しています。")
        if ref in done:
            return
        pending.add(ref)
        for parent in refs[ref]["predecessor_refs"]:
            if parent not in refs:
                raise ValueError(f"{ref}：前提工程{parent}がファイル内にありません。")
            if parent == ref:
                raise ValueError(f"{ref}：自分自身を前提工程にできません。")
            visit(parent)
            if refs[ref]["start_date"] <= refs[parent]["end_date"]:
                raise ValueError(f"{ref}：前提工程{parent}の終了翌日以降に開始してください。")
        pending.remove(ref)
        done.add(ref)

    for ref in refs:
        visit(ref)
    milestone_refs, milestone_values = set(), set()
    for number, row in enumerate(document["milestones"], 1):
        fallback = f"試験・節目{number}"
        label = row.get("ref", fallback) if isinstance(row, dict) else fallback
        _keys(row, _MILESTONE_KEYS, str(label))
        ref = row["ref"]
        if not isinstance(ref, str) or not re.fullmatch(r"M[0-9]{2,4}", ref):
            raise ValueError(f"{fallback}：refはM01のようなM＋2〜4桁の番号にしてください。")
        if ref in milestone_refs:
            raise ValueError(f"{ref}：試験・節目の番号が重複しています。")
        milestone_refs.add(ref)
        _text(row["title"], f"{ref} 試験・節目名")
        _date(row["date"], f"{ref} 日付")
        _name(row["project"], f"{ref} プロジェクト")
        _text(row["memo"], f"{ref} メモ", 20000, blank=True)
        semantic = _sha({key: value for key, value in row.items() if key != "ref"})
        if semantic in milestone_values:
            raise ValueError(f"{ref}：同じ試験・節目が複数あります。重複を除いてください。")
        milestone_values.add(semantic)
    try:
        _canonical(document)
    except (ValueError, UnicodeError, RecursionError, OverflowError) as exc:
        raise ValueError("JSONに無効な数値・文字・深すぎる構造があります。") from exc
    return deepcopy(document)


def parse_import_file(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or not raw:
        raise ValueError("JSONファイルが空です。")
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("JSONファイルは2MiB以内にしてください。")
    try:
        document = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=store._unique_pairs,
                              parse_constant=store._reject_constant)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSONの{exc.lineno}行{exc.colno}列付近に構文エラーがあります。JSONファイルだけを出力し直してください。") from exc
    except (ValueError, UnicodeError, RecursionError, OverflowError) as exc:
        raise ValueError("UTF-8のJSONとして読み込めません。重複キー・無効な数値・文字・深すぎる構造を確認してください。") from exc
    return validate_import_document(document)


def _path(root: Path, relative: str) -> Path:
    path = store.resolved_json_path(root / relative)
    if not path.is_relative_to(root) or path.relative_to(root).as_posix() != relative:
        raise store.JsonStoreError("取り込みの保存先がプロジェクト外または不正な場所を指しています。")
    return path


def _master(data):
    if not isinstance(data, dict):
        raise store.JsonReadError("分類マスタの形式が不正です。")
    for collection, key in zip(CLASS_COLLECTIONS, CLASS_KEYS):
        rows = data.get(collection, [])
        if not isinstance(rows, list):
            raise store.JsonReadError("分類マスタの一覧形式が不正です。")
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise store.JsonReadError("分類マスタに不正な項目があります。")
            identifier = row.get(key)
            if not isinstance(identifier, str) or not identifier or identifier in seen:
                raise store.JsonReadError("分類IDが未設定または重複しています。マスタ管理を確認してください。")
            seen.add(identifier)
            if not isinstance(row.get("name"), str) or not row["name"].strip():
                raise store.JsonReadError("分類名が未設定です。マスタ管理を確認してください。")
            if type(row.get("is_active", True)) is not bool:
                raise store.JsonReadError("分類の有効状態が不正です。")
            if any(not isinstance(row.get(parent, ""), str) for parent in CLASS_KEYS):
                raise store.JsonReadError("分類の所属IDが不正です。")
    return deepcopy(data)


def _empty_ledger():
    return {"schema_version": 1, "imports": [], "records": {}}


def _ledger(data):
    if (not isinstance(data, dict) or type(data.get("schema_version")) is not int
            or data["schema_version"] != 1 or not isinstance(data.get("imports"), list)
            or not isinstance(data.get("records"), dict)):
        raise store.JsonReadError("AI計画の取り込み履歴が不正です。バックアップを確認してください。")
    for entry in data["imports"]:
        if (not isinstance(entry, dict) or not isinstance(entry.get("operation_id"), str)
                or not _OPERATION.fullmatch(entry["operation_id"])
                or not isinstance(entry.get("plan_title"), str)
                or not isinstance(entry.get("imported_at"), str)):
            raise store.JsonReadError("AI計画の取り込み履歴が不正です。")
    for fingerprint, entry in data["records"].items():
        if not isinstance(fingerprint, str) or not _SHA.fullmatch(fingerprint):
            raise store.JsonReadError("AI計画の取り込み元情報が不正です。")
        _validate_provenance(entry)
    return deepcopy(data)


def _validate_provenance(entry):
    if (not isinstance(entry, dict) or not isinstance(entry.get("collection"), str)
            or entry["collection"] not in {"items", "milestones"}
            or not isinstance(entry.get("id"), str)
            or not re.fullmatch(("rm" if entry.get("collection") == "items" else "ms") + r"_[0-9a-f]{32}", entry["id"])
            or not isinstance(entry.get("operation_id"), str)
            or not _OPERATION.fullmatch(entry["operation_id"])):
        raise store.JsonReadError("AI計画の取り込み元情報が不正です。")


def load_import_context(root: Path) -> dict:
    root = store.resolved_json_path(root)
    data, revisions = {}, {}
    defaults = {ROADMAP_PATH: empty_roadmap(), MASTER_PATH: {name: [] for name in CLASS_COLLECTIONS}, LEDGER_PATH: _empty_ledger()}
    for relative, default in defaults.items():
        path = _path(root, relative)
        data[relative] = store.load_json_safe(path, default)
        revisions[relative] = store.observed_json_revision(path)
    roadmap = validate_roadmap(data[ROADMAP_PATH])
    require_schedule_order(roadmap)
    ledger = _ledger(data[LEDGER_PATH])
    return {"master": _master(data[MASTER_PATH]), "roadmap": roadmap,
            "history": list(reversed(deepcopy(ledger["imports"]))), "ledger": ledger,
            "revisions": revisions}


def import_history(root: Path) -> list[dict]:
    root = store.resolved_json_path(root)
    ledger = _ledger(store.load_json_safe(_path(root, LEDGER_PATH), _empty_ledger()))
    return list(reversed(ledger["imports"]))


def classification_requests(document: dict) -> list[dict]:
    document = validate_import_document(document)
    result = {}
    for row in [*document["items"], *document["milestones"]]:
        names = row.get("classification", {"project": row.get("project"), "subject": None, "material": None, "unit": None})
        key = _sha(names)
        result.setdefault(key, {"key": key, "names": deepcopy(names),
                                "label": " / ".join(names[name] or "未設定" for name in CLASS_NAMES)})
    return list(result.values())


def _mapping(mapping, master):
    if not isinstance(mapping, dict) or set(mapping) != set(CLASS_KEYS):
        raise ValueError("分類の対応先はプロジェクト・科目・教材・単元の4つのIDで指定してください。")
    result = {}
    for position, (collection, key) in enumerate(zip(CLASS_COLLECTIONS, CLASS_KEYS)):
        identifier = mapping[key]
        if not isinstance(identifier, str):
            raise ValueError("分類IDは文字列で指定してください。")
        result[key] = identifier
        if not identifier:
            continue
        matches = [row for row in master.get(collection, []) if row[key] == identifier and row.get("is_active", True)]
        if len(matches) != 1:
            raise ValueError("選択した分類が削除または無効化されています。再選択してください。")
        if any(matches[0].get(parent, "") != result[parent] for parent in CLASS_KEYS[:position]):
            raise ValueError("分類の所属が一致しません。プロジェクトから順に選び直してください。")
    return result


def auto_classification_mapping(names: dict, master: dict) -> dict | None:
    """Resolve only an unambiguous, exact full path; never fuzzy-match names."""
    chosen = {key: "" for key in CLASS_KEYS}
    for position, (name, collection, key) in enumerate(zip(CLASS_NAMES, CLASS_COLLECTIONS, CLASS_KEYS)):
        if names[name] is None:
            continue
        matches = [row for row in master.get(collection, [])
                   if row.get("name") == names[name] and row.get("is_active", True)
                   and all(row.get(parent, "") == chosen[parent] for parent in CLASS_KEYS[:position])]
        if len(matches) != 1:
            return None
        chosen[key] = matches[0][key]
    return chosen


def _source_fingerprints(document):
    items = {row["ref"]: row for row in document["items"]}
    result = {}
    remaining = set(items)
    # Iteration avoids exhausting the Python stack for a valid 500-stage chain.
    while remaining:
        for ref in sorted(remaining):
            row = items[ref]
            if any(parent not in result for parent in row["predecessor_refs"]):
                continue
            semantic = {key: value for key, value in row.items() if key not in {"ref", "predecessor_refs"}}
            for key in ("completed", "total"):
                number = semantic[key]
                if type(number) is float and number.is_integer():
                    semantic[key] = int(number)
            semantic["predecessors"] = sorted(result[parent] for parent in row["predecessor_refs"])
            result[ref] = _sha({"collection": "items", "value": semantic})
            remaining.remove(ref)
    for row in document["milestones"]:
        result[row["ref"]] = _sha({"collection": "milestones", "value": {key: value for key, value in row.items() if key != "ref"}})
    return result


def _provenance_index(roadmap, ledger):
    result = deepcopy(ledger["records"])
    for collection in ("items", "milestones"):
        for row in roadmap[collection]:
            provenance = row.get("roadmap_import")
            if provenance is None:
                continue
            if (not isinstance(provenance, dict) or not isinstance(provenance.get("source_fingerprint"), str)
                    or not _SHA.fullmatch(provenance["source_fingerprint"])):
                raise store.JsonReadError("工程のAI取り込み元情報が不正です。")
            entry = {"collection": collection, "id": row["id"], "operation_id": provenance.get("operation_id")}
            _validate_provenance(entry)
            fingerprint = provenance["source_fingerprint"]
            previous = result.get(fingerprint)
            if previous is not None and (previous["id"], previous["collection"]) != (entry["id"], collection):
                raise store.JsonReadError("同じAI取り込み元から複数の工程が登録されています。")
            result[fingerprint] = entry
    return result


def _title_key(value):
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _seal(preview):
    preview["digest"] = _sha(preview)
    preview["signature"] = hmac.new(_SECRET, _canonical(preview), hashlib.sha256).hexdigest()
    return preview


def _preview(root, document, mappings, acknowledge_similar, context, operation_id, created_at):
    document = validate_import_document(document)
    if not isinstance(mappings, dict):
        raise ValueError("分類の対応表が不正です。")
    if type(acknowledge_similar) is not bool:
        raise ValueError("類似する工程の確認状態が不正です。")
    requests = classification_requests(document)
    request_keys = {request["key"] for request in requests}
    if set(mappings) - request_keys:
        raise ValueError("ファイルにない分類の対応が指定されています。プレビューを作り直してください。")
    resolved, issues = {}, []

    def issue(severity, ref, message):
        issues.append({"severity": severity, "ref": ref, "message": message})

    for request in requests:
        key = request["key"]
        candidate = mappings.get(key) if key in mappings else auto_classification_mapping(request["names"], context["master"])
        if candidate is None:
            issue("error", "分類", f"「{request['label']}」の対応先を選ぶか、分類を未設定で取り込むことを選択してください。")
            continue
        try:
            resolved[key] = _mapping(candidate, context["master"])
        except ValueError as exc:
            issue("error", "分類", f"「{request['label']}」：{exc}")
    fingerprints = _source_fingerprints(document)
    index = _provenance_index(context["roadmap"], context["ledger"])
    proposed = deepcopy(context["roadmap"])
    live = {row["id"]: row for collection in ("items", "milestones") for row in proposed[collection]}
    ids, additions, rows = {}, {"items": [], "milestones": []}, []
    existing_count = 0
    similar_count = 0
    for collection in ("items", "milestones"):
        for source in document[collection]:
            ref = source["ref"]
            source_fingerprint = fingerprints[ref]
            previous = index.get(source_fingerprint)
            if previous is not None and previous["collection"] != collection:
                raise store.JsonReadError("取り込み履歴の工程・節目の区分が一致しません。バックアップを確認してください。")
            row = {"collection": collection, "ref": ref, "title": source["title"],
                   "source_fingerprint": source_fingerprint,
                   "action": "existing" if previous else "add", "reason": "新しく追加します。"}
            for key in ("start_date", "end_date", "date", "planned_minutes", "predecessor_refs"):
                if key in source:
                    row[key] = deepcopy(source[key])
            if previous:
                ids[ref] = previous["id"]
                existing_count += 1
                row["reason"] = "取り込み済みのため追加しません。" if previous["id"] in live else "取り込み済みです。現在の工程は削除済みですが、再追加しません。"
                rows.append(row)
                continue
            identifier = ("rm" if collection == "items" else "ms") + "_" + uuid5(NAMESPACE_URL, operation_id + ":" + collection + ":" + ref).hex
            ids[ref] = identifier
            names = source.get("classification", {"project": source.get("project"), "subject": None, "material": None, "unit": None})
            classification = resolved.get(_sha(names), {key: "" for key in CLASS_KEYS})
            payload = {"id": identifier, "title": source["title"], "memo": source["memo"],
                       "created_at": created_at, "updated_at": created_at, "is_archived": False,
                       "roadmap_import": {"source_fingerprint": source_fingerprint,
                                          "operation_id": operation_id, "source_ref": ref,
                                          "plan_title": document["plan_title"]}}
            if collection == "items":
                payload.update({key: deepcopy(source[key]) for key in ("start_date", "end_date", "planned_minutes", "status", "progress_kind", "completed", "total", "unit")})
                payload.update(classification)
                payload.update(deadline=source["deadline"] or "", baseline_start_date=source["start_date"],
                               baseline_end_date=source["end_date"], schedule_history=[], predecessor_ids=[])
            else:
                payload.update(date=source["date"], project_id=classification["project_id"])
            matches = [old for old in proposed[collection]
                       if _title_key(old["title"]) == _title_key(source["title"])]
            if matches:
                similar_count += 1
                issue("warning", ref, "同名・表記違いの既存データがあります。既存データは変更せず、別の工程として追加します。")
            additions[collection].append(payload)
            proposed[collection].append(payload)
            rows.append(row)
    new_items = {row["id"]: row for row in additions["items"]}
    for source in document["items"]:
        payload = new_items.get(ids[source["ref"]])
        if payload is None:
            continue
        payload["predecessor_ids"] = [ids[ref] for ref in source["predecessor_refs"]]
        for parent_ref in source["predecessor_refs"]:
            parent_id = ids[parent_ref]
            parent = next((row for row in proposed["items"] if row["id"] == parent_id), None)
            if parent is None:
                issue("error", source["ref"], f"前提工程{parent_ref}は取り込み済みですが削除されています。復元または計画の見直しが必要です。")
            elif source["start_date"] <= parent["end_date"]:
                issue("error", source["ref"], f"現在保存されている前提工程{parent_ref}の終了翌日以降に開始してください。")
    if similar_count and not acknowledge_similar:
        issue("error", "類似工程", "同名・類似工程を別の工程として追加することを確認してください。")
    # A missing duplicate parent is already a diagnostic; avoid a generic ID error.
    if not any(entry["severity"] == "error" for entry in issues):
        validate_roadmap(proposed)
        require_schedule_order(proposed)
    counts = {"items": len(additions["items"]), "milestones": len(additions["milestones"]), "existing": existing_count}
    if not counts["items"] and not counts["milestones"]:
        issue("info", "取り込み済み", "すべて取り込み済みです。新しく追加する工程・節目はありません。")
    return _seal({"format": "proloag.roadmap-import-preview", "schema_version": 1,
                  "root": str(root), "operation_id": operation_id, "created_at": created_at,
                  "document": document, "mappings": deepcopy(mappings),
                  "acknowledge_similar": acknowledge_similar, "revisions": context["revisions"],
                  "bundle_fingerprint": _sha(sorted(fingerprints.values())),
                  "rows": rows, "proposed": proposed, "additions": additions,
                  "issues": issues, "counts": counts, "similar_count": similar_count,
                  "planned_minutes": sum(row["planned_minutes"] for row in additions["items"]),
                  "can_commit": bool(counts["items"] or counts["milestones"]) and not any(entry["severity"] == "error" for entry in issues)})


def build_import_preview(root: Path, document: dict, mappings=None, *, acknowledge_similar=False) -> dict:
    root = store.resolved_json_path(root)
    return _preview(root, document, {} if mappings is None else mappings, acknowledge_similar,
                    load_import_context(root), uuid4().hex, datetime.now().isoformat(timespec="microseconds"))


def apply_import_preview(root: Path, preview: dict) -> dict:
    root = store.resolved_json_path(root)
    if not isinstance(preview, dict):
        raise ValueError("取り込みプレビューがありません。")
    unsigned = {key: value for key, value in preview.items() if key != "signature"}
    try:
        valid = hmac.compare_digest(str(preview.get("signature", "")), hmac.new(_SECRET, _canonical(unsigned), hashlib.sha256).hexdigest())
        valid = valid and _sha({key: value for key, value in unsigned.items() if key != "digest"}) == preview.get("digest")
    except (ValueError, TypeError, UnicodeError):
        valid = False
    if not valid or preview.get("root") != str(root):
        raise ValueError("確認内容が変更または失効しています。プレビューを作り直してください。")
    if not preview["can_commit"]:
        raise ValueError("取り込み前の確認事項を解消してください。")
    expected = {_path(root, relative): revision for relative, revision in preview["revisions"].items()}
    with atomic_json_operation(root=root, kind="roadmap_import", operation_id=preview["operation_id"],
                               expected_revisions=expected, request_fingerprint=preview["digest"]) as op:
        result = {"operation_id": preview["operation_id"], "replayed": op.already_committed,
                  **preview["counts"], "changed_files": [], "ledger_path": LEDGER_PATH}
        if op.already_committed:
            return result
        context = load_import_context(root)
        fresh = _preview(root, preview["document"], preview["mappings"], preview["acknowledge_similar"],
                         context, preview["operation_id"], preview["created_at"])
        if fresh != preview:
            raise store.JsonConflictError("確認後に計画・分類・取り込み履歴が変更されました。プレビューを作り直してください。")
        ledger = context["ledger"]
        for collection in ("items", "milestones"):
            for row in preview["additions"][collection]:
                fingerprint = row["roadmap_import"]["source_fingerprint"]
                ledger["records"][fingerprint] = {"id": row["id"], "collection": collection,
                                                   "operation_id": preview["operation_id"]}
        ledger["imports"].append({"operation_id": preview["operation_id"],
                                  "plan_title": preview["document"]["plan_title"],
                                  "imported_at": preview["created_at"],
                                  "bundle_fingerprint": preview["bundle_fingerprint"],
                                  **preview["counts"], "planned_minutes": preview["planned_minutes"]})
        _ledger(ledger)
        validate_roadmap(preview["proposed"])
        require_schedule_order(preview["proposed"])
        op.stage(_path(root, ROADMAP_PATH), preview["proposed"])
        op.stage(_path(root, LEDGER_PATH), ledger)
    result["changed_files"] = list(op.changed_files)
    return result
