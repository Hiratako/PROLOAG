"""Explicit, previewed legacy CSV imports; reads never normalize or save.

The preview binds source rows, selections, classifications and JSON revisions.
A confirmed commit only adds ready rows, reserves IDs and saves a provenance
ledger in one journaled operation. Historical and existing money stay intact.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import math
import ntpath
import os
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from app_core import ids, json_store
from app_core.json_operations import atomic_json_operation
from app_core.study_record_reader import unpack_study_document


MASTER_KEYS = ("project_id", "subject_id", "material_id", "unit_id")
MASTER_COLLECTIONS = ("projects", "subjects", "materials", "units")
LEDGER_PATH = "settings/legacy_study_imports.json"
FORMAT = "legacy_counter_csv_v1"
_SECRET = os.urandom(32)
_SHA = re.compile(r"[0-9a-f]{64}")
_OPERATION = re.compile(r"[0-9a-f]{32}")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or "\\" in relative:
        raise json_store.JsonStoreError("移行の保存先が不正です。")
    candidate = root / relative
    resolved = json_store.resolved_json_path(candidate)
    if not resolved.is_relative_to(root) or resolved.relative_to(root).as_posix() != relative:
        raise json_store.JsonStoreError("移行の保存先がプロジェクト外です。")
    return candidate


def _read(root: Path, relative: str, default: Any) -> tuple[Any, str | None]:
    data, raw, revision = json_store._read(_path(root, relative))
    return deepcopy(default if raw is None else data), revision


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or value in (None, ""):
        raise ValueError(f"{label}が未入力または不正です。")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}が数値ではありません。") from exc
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise ValueError(f"{label}が範囲外です。")
    return result


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("セッション日時が不正です。")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("セッション日時が不正です。") from exc
    if result.tzinfo is not None:
        # Existing offset-aware records are interpreted in the current local
        # calendar, matching the app's normal study reader.
        result = result.astimezone().replace(tzinfo=None)
    return result


def _validate_master(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise json_store.JsonReadError("分類マスタの形式が不正です。")
    for collection, key in zip(MASTER_COLLECTIONS, MASTER_KEYS):
        items = data.get(collection, [])
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise json_store.JsonReadError("分類マスタの一覧形式が不正です。")
        identifiers = []
        for item in items:
            identifier = item.get(key)
            if not isinstance(identifier, str) or not identifier:
                raise json_store.JsonReadError("分類IDが未設定です。マスタ管理を確認してください。")
            identifiers.append(identifier)
        if len(identifiers) != len(set(identifiers)):
            raise json_store.JsonReadError("分類IDが重複しています。マスタ管理を確認してください。")
    return data


def load_import_classifications(root: Path) -> dict[str, Any]:
    """Load actual master IDs without normalization or any write-back."""
    root = json_store.resolved_json_path(root)
    data, _ = _read(root, "master_data/master.json", {})
    return _validate_master(data)


def _classification(mapping: Any, masters: dict[str, Any]) -> dict[str, str]:
    if mapping is None:
        mapping = {}
    if not isinstance(mapping, dict) or set(mapping) - set(MASTER_KEYS):
        raise ValueError("分類の対応表が不正です。")
    result = {}
    for position, (collection, key) in enumerate(zip(MASTER_COLLECTIONS, MASTER_KEYS)):
        item_id = mapping.get(key) or ""
        if not isinstance(item_id, str):
            raise ValueError("分類IDは文字列で指定してください。")
        result[key] = item_id
        if not item_id:
            continue
        found = [item for item in masters.get(collection, []) if item[key] == item_id]
        if len(found) != 1 or not found[0].get("is_active", True):
            raise ValueError(f"分類が削除・無効化されています: {key}。再選択してください。")
        for parent in MASTER_KEYS[:position]:
            if (found[0].get(parent) or "") != result[parent]:
                raise ValueError("プロジェクト・科目・教材・単元の所属が一致しません。")
    return result


def _ledger(data: Any) -> dict[str, Any]:
    if (not isinstance(data, dict) or data.get("schema_version") != 1
            or not isinstance(data.get("imports"), list) or not isinstance(data.get("records"), dict)):
        raise json_store.JsonReadError("旧版ログの取り込み履歴が不正です。復旧情報を確認してください。")
    for item in data["imports"]:
        if (not isinstance(item, dict) or not isinstance(item.get("operation_id"), str)
                or not _OPERATION.fullmatch(item["operation_id"])):
            raise json_store.JsonReadError("旧版ログの取り込み履歴が不正です。")
    for fingerprint, item in data["records"].items():
        if (not isinstance(fingerprint, str) or not _SHA.fullmatch(fingerprint)
                or not isinstance(item, dict) or not isinstance(item.get("session_id"), str)
                or not item["session_id"] or not isinstance(item.get("batch_id"), str)
                or not _OPERATION.fullmatch(item["batch_id"])):
            raise json_store.JsonReadError("旧版ログの取り込み履歴の記録が不正です。")
    return data


def _provenance(record: dict[str, Any]) -> str | None:
    metadata = record.get("import_metadata")
    if not isinstance(metadata, dict) or metadata.get("format") != FORMAT:
        return None
    fingerprint = metadata.get("fingerprint")
    if not isinstance(fingerprint, str) or not _SHA.fullmatch(fingerprint):
        raise json_store.JsonReadError("既存記録の旧版取り込み情報が不正です。")
    return fingerprint


def _records(data: Any, relative: str) -> list[dict[str, Any]]:
    document = unpack_study_document(data)
    if document.container_kind == "unsupported" or (
        document.container_kind == "sessions" and not isinstance(data.get("sessions"), list)
    ):
        raise json_store.JsonReadError(f"勉強ログの保存形式を確認できません: {relative}")
    return [record for _, record in document.records]


def _append_records(original: Any, additions: list[dict[str, Any]]) -> Any:
    if isinstance(original, list):
        return [*deepcopy(original), *deepcopy(additions)]
    if isinstance(original, dict) and isinstance(original.get("sessions"), list):
        result = deepcopy(original)
        result["sessions"].extend(deepcopy(additions))
        return result
    document = unpack_study_document(original)
    if document.container_kind == "record":
        return [deepcopy(original), *deepcopy(additions)]
    raise json_store.JsonReadError("対象日の勉強ログを安全に追加できません。")


def _snapshot(root: Path) -> dict[str, Any]:
    paths = sorted(json_store.record_json_files(root / "study_logs"))
    inventory = [json_store.resolved_json_path(path).relative_to(root).as_posix() for path in paths]
    revisions, documents = {}, {}
    existing, reserved, provenance = [], set(), {}
    for relative in inventory:
        data, revision = _read(root, relative, [])
        revisions[relative], documents[relative] = revision, data
        for position, record in enumerate(_records(data, relative)):
            session_id = record.get("session_id")
            if session_id:
                if not isinstance(session_id, str) or session_id in reserved:
                    raise json_store.JsonReadError("既存の勉強ログIDが不正または重複しています。")
                reserved.add(session_id)
            try:
                start = _timestamp(record.get("start_at"))
                end = _timestamp(record.get("end_at"))
                duration = _number(record.get("duration_sec"), "既存記録の実勉強時間", positive=True)
            except ValueError as exc:
                raise json_store.JsonReadError(f"既存記録の日時・時間を確認してください: {relative}") from exc
            if end < start:
                raise json_store.JsonReadError(f"既存記録の開始・終了日時が逆転しています: {relative}")
            item = {"session": record, "source_file": relative, "session_id": session_id or "",
                    "position": position, "start": start, "end": end, "duration": duration}
            existing.append(item)
            fingerprint = _provenance(record)
            if fingerprint:
                provenance[fingerprint] = {"session_id": session_id or "", "source_file": relative}
    fixed = {
        "master_data/master.json": {},
        LEDGER_PATH: {"schema_version": 1, "imports": [], "records": {}},
        "backups/study_session_ids.json": [],
        "backups/study_log_trash.json": [],
    }
    for relative, default in fixed.items():
        data, revision = _read(root, relative, default)
        revisions[relative], documents[relative] = revision, data
    masters = _validate_master(documents["master_data/master.json"])
    ledger = _ledger(documents[LEDGER_PATH])
    registry = documents["backups/study_session_ids.json"]
    trash = documents["backups/study_log_trash.json"]
    if not isinstance(registry, list) or any(not isinstance(value, str) or not value for value in registry):
        raise json_store.JsonReadError("勉強ログIDの予約台帳が不正です。")
    reserved.update(registry)
    if not isinstance(trash, list):
        raise json_store.JsonReadError("勉強ログのごみ箱の形式が不正です。")
    for entry in trash:
        if not isinstance(entry, dict) or not isinstance(entry.get("session"), dict):
            raise json_store.JsonReadError("勉強ログのごみ箱の記録が不正です。")
        record = entry["session"]
        session_id = record.get("session_id")
        if session_id:
            if not isinstance(session_id, str):
                raise json_store.JsonReadError("ごみ箱の勉強ログIDが不正です。")
            reserved.add(session_id)
        fingerprint = _provenance(record)
        if fingerprint:
            provenance[fingerprint] = {"session_id": session_id or "", "source_file": "ごみ箱"}
    for fingerprint, entry in ledger["records"].items():
        provenance[fingerprint] = {"session_id": entry["session_id"], "source_file": "取り込み履歴"}
        reserved.add(entry["session_id"])
    return {"inventory": inventory, "revisions": revisions, "documents": documents,
            "existing": existing, "reserved": reserved, "provenance": provenance,
            "masters": masters, "ledger": ledger}


def _row_session(value: Any, classification: dict[str, str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("変換後のセッションが不正です。")
    start, end = _timestamp(value.get("start_at")), _timestamp(value.get("end_at"))
    if min(start.year, end.year) < 1000:
        raise ValueError("開始・終了日時は4桁の西暦（1000年以上）で指定してください。")
    if end < start:
        raise ValueError("終了日時が開始日時より前です。")
    for key in ("duration_sec", "hourly_rate", "money"):
        _number(value.get(key), key, positive=key == "duration_sec")
    condition = value.get("condition", {})
    if not isinstance(condition, dict):
        raise ValueError("コンディションの形式が不正です。")
    for key, minimum, maximum in (("effort", 1, 10), ("mood", 1, 5), ("sleep_hours", 0, 24)):
        if condition.get(key) not in (None, ""):
            if not minimum <= _number(condition[key], key) <= maximum:
                raise ValueError(f"コンディションの{key}が範囲外です。")
    if condition.get("alcohol_yesterday") not in (None, "", "なし", "あり"):
        raise ValueError("前日飲酒の区分が不正です。")
    if condition.get("io_type") not in (None, "", "インプット", "アウトプット", "両方"):
        raise ValueError("インプット・アウトプットの区分が不正です。")
    memo = value.get("memo", "")
    if not isinstance(memo, str):
        raise ValueError("メモは文字列で指定してください。")
    return {"start_at": start.isoformat(), "end_at": end.isoformat(),
            **{key: value[key] for key in ("duration_sec", "hourly_rate", "money")},
            "memo": memo, "condition": deepcopy(condition), **classification,
            "linked_note_ids": [], "linked_task_id": ""}


def _candidate(item: dict[str, Any]) -> dict[str, Any]:
    record = item["session"]
    return {"session_id": item["session_id"], "source_file": item["source_file"],
            "start_at": record.get("start_at"), "end_at": record.get("end_at"),
            "duration_sec": record.get("duration_sec"), "money": record.get("money"),
            "hourly_rate": record.get("hourly_rate"), "memo": record.get("memo", "")}


def _signed(preview: dict[str, Any]) -> str:
    return hmac.new(_SECRET, _canonical({key: value for key, value in preview.items() if key != "signature"}),
                    hashlib.sha256).hexdigest()


def build_import_preview(diagnosis: dict[str, Any], root: Path, *,
                         classification_map: dict[str, dict[str, str]] | None = None,
                         selected_rows: list[int] | None = None,
                         acknowledged_rows: list[int] | None = None) -> dict[str, Any]:
    """Capture a read-only plan; review rows are held until explicitly checked."""
    root = json_store.resolved_json_path(root)
    if not isinstance(diagnosis, dict) or not isinstance(diagnosis.get("rows"), list):
        raise ValueError("CSV診断結果を先に作成してください。")
    source_hash = diagnosis.get("source_sha256")
    if not isinstance(source_hash, str) or not _SHA.fullmatch(source_hash):
        raise ValueError("元CSVの識別情報が不正です。")
    raw_rows = diagnosis["rows"]
    row_numbers = [row.get("row_number") for row in raw_rows if isinstance(row, dict)]
    if (len(row_numbers) != len(raw_rows) or any(type(value) is not int or value < 2 for value in row_numbers)
            or len(set(row_numbers)) != len(row_numbers)):
        raise ValueError("CSV行番号の形式が不正です。")
    selection = set(row_numbers if selected_rows is None else selected_rows)
    acknowledged = set(acknowledged_rows or [])
    if (any(type(value) is not int for value in selection | acknowledged)
            or not selection <= set(row_numbers) or not acknowledged <= set(row_numbers)):
        raise ValueError("対象行または確認済み行を再選択してください。")
    classification_map = classification_map or {}
    if not isinstance(classification_map, dict):
        raise ValueError("分類の対応表が不正です。")
    snapshot = _snapshot(root)
    classifications = {topic: _classification(value, snapshot["masters"])
                       for topic, value in classification_map.items()}
    default_classification = {key: "" for key in MASTER_KEYS}
    existing_ordered = sorted(snapshot["existing"], key=lambda item: item["start"])
    existing_starts, maximum_ends = [], []
    for existing in existing_ordered:
        existing_starts.append(existing["start"])
        maximum_ends.append(max(maximum_ends[-1], existing["end"]) if maximum_ends else existing["end"])
    # Top-level errors belong to file parsing or summary reconciliation. Their
    # row_number may refer to the summary CSV, never to an excludable session.
    global_errors = [deepcopy(issue) for issue in diagnosis.get("issues", [])
                     if issue.get("severity") == "error"]
    rows, accepted_fingerprints = [], {}
    for raw_row in raw_rows:
        number = raw_row["row_number"]
        fingerprint = raw_row.get("fingerprint")
        if not isinstance(fingerprint, str) or not _SHA.fullmatch(fingerprint):
            raise ValueError("変換行の識別情報が不正です。CSVを再診断してください。")
        issues = deepcopy(raw_row.get("issues", []))
        if not isinstance(issues, list) or any(not isinstance(issue, dict) for issue in issues):
            raise ValueError("CSV診断の形式が不正です。")
        raw_original = deepcopy(raw_row.get("raw", {}))
        if not isinstance(raw_original, dict) or any(not isinstance(key, str) for key in raw_original):
            raise ValueError("元CSVの保存情報が不正です。")
        # Invalid rows can retain parser diagnostics such as __extra_cells as
        # JSON-safe lists. They must remain excludable without losing the other
        # valid rows. Accepted source records still require scalar CSV cells.
        try:
            _canonical(raw_original)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("元CSVの保存情報が不正です。") from exc
        old_source = raw_original.pop("__src", "")
        legacy_source_name = ntpath.basename(old_source) if isinstance(old_source, str) else ""
        entry = {"row_number": number, "fingerprint": fingerprint,
                 "legacy_topic": str(raw_row.get("legacy_topic") or ""),
                 "issues": issues, "existing_candidates": [], "session": None,
                 "raw": raw_original, "legacy_source_name": legacy_source_name,
                 "acknowledged": number in acknowledged}
        if number not in selection:
            entry.update(status="excluded", reason="今回の反映対象外")
        elif any(issue.get("severity") == "error" for issue in issues) or raw_row.get("session") is None:
            entry.update(status="error", reason="元CSVのエラーを確認してください。")
        elif any(not isinstance(value, str) for value in raw_original.values()):
            entry.update(status="error", reason="CSVの列数または元の値の形式を確認してください。")
        else:
            try:
                session = _row_session(raw_row["session"], classifications.get(entry["legacy_topic"], default_classification))
            except ValueError as exc:
                entry.update(status="error", reason=str(exc))
                rows.append(entry)
                continue
            entry["session"] = session
            known = snapshot["provenance"].get(fingerprint)
            if known:
                entry["existing_candidates"] = [known]
                entry.update(status="existing", reason="取り込み済みです。編集・削除後も再追加しません。")
            elif fingerprint in accepted_fingerprints:
                entry.update(status="existing", reason=f"同じCSVの{accepted_fingerprints[fingerprint]}行と同じ記録です。")
            else:
                start, end = _timestamp(session["start_at"]), _timestamp(session["end_at"])
                # Prefix maxima preserve long/cross-midnight intervals. Merely
                # checking adjacent calendar days can miss a multi-day record.
                one_second = timedelta(seconds=1)
                near_start = start - one_second if start >= datetime.min + one_second else datetime.min
                near_end = end + one_second if end <= datetime.max - one_second else datetime.max
                left = bisect_right(maximum_ends, near_start)
                right = bisect_left(existing_starts, near_end)
                candidates = existing_ordered[left:right]
                exact, similar = [], []
                for existing in candidates:
                    same_time = start == existing["start"] and end == existing["end"]
                    same_duration = abs(float(session["duration_sec"]) - existing["duration"]) <= 0.000001
                    same_memo = session["memo"] == existing["session"].get("memo", "")
                    if same_time and same_duration and same_memo:
                        exact.append(existing)
                    elif (min(end, existing["end"]) > max(start, existing["start"])) or (
                        abs((start - existing["start"]).total_seconds()) <= 1
                        and abs((end - existing["end"]).total_seconds()) <= 1 and same_duration
                    ):
                        similar.append(existing)
                if exact:
                    entry["existing_candidates"] = [_candidate(item) for item in exact]
                    entry.update(status="existing", reason="同じ日時・時間・メモの既存記録があります。")
                else:
                    if similar:
                        entry["existing_candidates"] = [_candidate(item) for item in similar]
                        entry["issues"].append({"code": "existing_overlap", "severity": "warning",
                                                "message": "既存記録と時間帯が重なるか、ほぼ同じ記録です。"})
                    warnings = [issue for issue in entry["issues"] if issue.get("severity") == "warning"]
                    if warnings and number not in acknowledged:
                        entry.update(status="review", reason="注意内容を確認し、別記録として取り込む場合だけ確認済みにしてください。")
                    else:
                        session["session_id"] = ids.generate_id("session", session["start_at"], snapshot["reserved"])
                        entry.update(status="ready", reason="反映候補（確認済み）" if warnings else "反映候補")
                        accepted_fingerprints[fingerprint] = number
        rows.append(entry)
    counts = Counter(row["status"] for row in rows)
    ready = [row for row in rows if row["status"] == "ready"]
    duration = math.fsum(float(row["session"]["duration_sec"]) for row in ready)
    money = math.fsum(float(row["session"]["money"]) for row in ready)
    # A missing destination is guarded just like an existing daily JSON.
    for row in ready:
        day = _timestamp(row["session"]["start_at"])
        relative = f"study_logs/{day:%Y}/{day:%m}/{day:%Y%m%d}.json"
        if relative not in snapshot["revisions"]:
            data, revision = _read(root, relative, [])
            snapshot["revisions"][relative] = revision
            if revision is not None:
                raise json_store.JsonConflictError("確認中に対象日の記録が追加されました。プレビューを更新してください。")
    preview = {
        "schema_version": 1, "format": FORMAT, "root": str(root), "operation_id": uuid4().hex,
        "prepared_at": datetime.now().isoformat(timespec="microseconds"),
        "source_sha256": source_hash, "summary_sha256": diagnosis.get("summary_sha256"),
        "rows": rows, "blocking_errors": global_errors,
        "summary": {**{status + "_count": counts[status] for status in ("ready", "review", "existing", "error", "excluded")},
                    "error_count": counts["error"] + len(global_errors),
                    "row_count": len(rows), "duration_sec": duration, "money": money},
        "classification_map": classifications, "selected_rows": sorted(selection),
        "acknowledged_rows": sorted(acknowledged),
        "expected_revisions": snapshot["revisions"], "inventory": snapshot["inventory"],
    }
    preview["request_fingerprint"] = _sha(preview)
    preview["signature"] = _signed(preview)
    return preview


def commit_import(preview: dict[str, Any], root: Path) -> dict[str, Any]:
    """Apply an unchanged, explicit preview without repricing any existing row."""
    root = json_store.resolved_json_path(root)
    if (not isinstance(preview, dict) or preview.get("schema_version") != 1
            or preview.get("format") != FORMAT or preview.get("root") != str(root)
            or not isinstance(preview.get("signature"), str)
            or not hmac.compare_digest(preview["signature"], _signed(preview))):
        raise json_store.JsonConflictError("移行プレビューが変更・失効しています。プレビューを更新してください。")
    unsigned_request = {key: value for key, value in preview.items() if key not in {"signature", "request_fingerprint"}}
    if _sha(unsigned_request) != preview.get("request_fingerprint"):
        raise json_store.JsonConflictError("移行内容が変更されています。プレビューを更新してください。")
    if preview["blocking_errors"] or preview["summary"]["error_count"]:
        raise ValueError("CSVのエラーを解消するか対象外にして、プレビューを更新してください。")
    ready = [row for row in preview["rows"] if row["status"] == "ready"]
    if not ready:
        return {"added_count": 0, "original_count": 0, "duration_sec": 0, "money": 0,
                "operation_id": preview["operation_id"], "replayed": False, "changed_files": [], "ledger_path": LEDGER_PATH}
    expected = {_path(root, relative): revision for relative, revision in preview["expected_revisions"].items()}
    now = datetime.now().isoformat(timespec="microseconds")
    changed_files = []
    with atomic_json_operation(root=root, kind="legacy_study_import", operation_id=preview["operation_id"],
                               expected_revisions=expected, request_fingerprint=preview["request_fingerprint"]) as operation:
        replayed = operation.already_committed
        if not replayed:
            operation.watch_inventory(root / "study_logs", preview["inventory"])
            # Read all guarded documents into the operation. This binds even
            # unrelated files used for duplicate detection to the preview.
            documents = {relative: operation.read(_path(root, relative), None)
                         for relative in preview["expected_revisions"]}
            master = _validate_master(documents["master_data/master.json"] or {})
            ledger = _ledger(documents[LEDGER_PATH] if documents[LEDGER_PATH] is not None else
                             {"schema_version": 1, "imports": [], "records": {}})
            registry = documents["backups/study_session_ids.json"] or []
            if not isinstance(registry, list) or any(not isinstance(value, str) for value in registry):
                raise json_store.JsonReadError("勉強ログIDの予約台帳が不正です。")
            by_date = defaultdict(list)
            for row in ready:
                session = deepcopy(row["session"])
                _classification({key: session[key] for key in MASTER_KEYS}, master)
                _row_session(session, {key: session[key] for key in MASTER_KEYS})
                fingerprint = row["fingerprint"]
                if fingerprint in ledger["records"]:
                    raise json_store.JsonConflictError("確認後に同じ旧記録が取り込まれています。プレビューを更新してください。")
                session.update(created_at=now, updated_at=now)
                raw_original = {key: value for key, value in row["raw"].items() if key != "__src"}
                session["import_metadata"] = {
                    "format": FORMAT, "source_sha256": preview["source_sha256"],
                    "source_row": row["row_number"], "fingerprint": fingerprint,
                    "imported_at": now, "raw_original": raw_original,
                    "legacy_source_name": row["legacy_source_name"],
                    "historical_money_preserved": True,
                }
                day = _timestamp(session["start_at"])
                relative = f"study_logs/{day:%Y}/{day:%m}/{day:%Y%m%d}.json"
                by_date[relative].append(session)
                ledger["records"][fingerprint] = {"session_id": session["session_id"],
                    "batch_id": preview["operation_id"], "source_sha256": preview["source_sha256"],
                    "row_number": row["row_number"], "imported_at": now}
            for relative, additions in sorted(by_date.items()):
                original = documents[relative]
                operation.stage(_path(root, relative), _append_records([] if original is None else original, additions))
                changed_files.append(relative)
            ledger["imports"].append({"operation_id": preview["operation_id"], "imported_at": now,
                "source_sha256": preview["source_sha256"], "summary_sha256": preview["summary_sha256"],
                "count": len(ready), "duration_sec": preview["summary"]["duration_sec"],
                "money": preview["summary"]["money"], "rows": [row["row_number"] for row in ready]})
            operation.stage(_path(root, LEDGER_PATH), ledger)
            operation.stage(_path(root, "backups/study_session_ids.json"),
                            sorted(set(registry) | {row["session"]["session_id"] for row in ready}))
            changed_files.extend([LEDGER_PATH, "backups/study_session_ids.json"])
    return {"added_count": 0 if replayed else len(ready), "original_count": len(ready),
            "duration_sec": 0 if replayed else preview["summary"]["duration_sec"],
            "money": 0 if replayed else preview["summary"]["money"],
            "operation_id": preview["operation_id"], "replayed": replayed,
            "changed_files": [] if replayed else changed_files, "ledger_path": LEDGER_PATH}
