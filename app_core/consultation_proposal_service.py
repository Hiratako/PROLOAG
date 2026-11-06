"""Explicit, revision-bound local adoption of untrusted consultation proposals."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
from uuid import uuid4

from app_core import json_store
from app_core.consultation_proposal_contract import MAX_PROPOSALS, ProposalError, validate_candidate, validate_inbox, validate_plan_memos
from app_core.json_operations import atomic_json_operation, operation_guard
from app_core.learning_context_service import MAX_NEXT_STEP_LENGTH, _validate_context, list_materials


_PREVIEW_SECRET = secrets.token_bytes(32)
_HASH = re.compile(r"^[0-9a-f]{64}$")
_OP_ID = re.compile(r"^[0-9a-f]{32}$")
_INBOX = "settings/consultation_inbox.json"
_CONTEXT = "settings/learning_context.json"
_MASTER = "master_data/master.json"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def _canonical(value) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, OverflowError):
        raise ProposalError("提案または確認内容の形式が不正です。") from None


def _path(root: Path, relative: str) -> Path:
    base = json_store.resolved_json_path(root)
    path = json_store.resolved_json_path(base / relative)
    if not path.is_relative_to(base):
        raise ProposalError("提案の保存先がプロジェクト外を参照しています。")
    return path


def _root_key(root: Path) -> str:
    return str(json_store.resolved_json_path(root))


def _revision(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")).hexdigest()


def _expected(value) -> None:
    if value is not None and (not isinstance(value, str) or not _HASH.fullmatch(value)):
        raise ProposalError("編集開始時の状態がありません。提案箱を開き直してください。")


def _read_inbox(root: Path) -> dict:
    return validate_inbox(json_store.load_json_safe(_path(root, _INBOX), {"schema_version": 1, "proposals": {}}))


def _entry(data: dict, proposal_id: str) -> dict:
    entry = data["proposals"].get(proposal_id)
    if not isinstance(entry, dict):
        raise ProposalError("選択した提案が見つかりません。提案箱を開き直してください。")
    return entry


def _assert_revision(root: Path, expected_revision) -> None:
    _expected(expected_revision)
    if json_store.observed_json_revision(_path(root, _INBOX)) != expected_revision:
        raise json_store.JsonConflictError("確認後に提案箱が変更されました。入力を保持したまま内容を確認してください。")


def _recheck_revisions(root: Path, revisions: dict) -> None:
    for relative, revision in revisions.items():
        if json_store._read(_path(root, relative))[2] != revision:
            raise json_store.JsonConflictError("確認画面の準備中に記録が変更されました。入力を保持して再確認してください。")


def load_inbox_snapshot(root: Path) -> dict:
    with operation_guard(root):
        data = _read_inbox(root)
        return {"data": data, "revision": json_store.observed_json_revision(_path(root, _INBOX))}


def import_proposals(root: Path, candidates: list[dict], *, expected_revision) -> dict:
    _expected(expected_revision)
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= MAX_PROPOSALS:
        raise ProposalError("取り込む提案がありません。")
    candidates = [validate_candidate(item) for item in candidates]
    added = duplicates = 0
    with atomic_json_operation(root=root, kind="proposal_import", expected_revisions={_path(root, _INBOX): expected_revision}):
        data = _read_inbox(root)
        for candidate in candidates:
            identifier = candidate["proposal_id"]
            if identifier in data["proposals"]:
                duplicates += 1
                continue
            timestamp = _now()
            data["proposals"][identifier] = {**{key: deepcopy(candidate[key]) for key in ("proposal_id", "source", "proposal")},
                                            "status": "pending", "created_at": timestamp, "updated_at": timestamp}
            added += 1
        validate_inbox(data)
        json_store.save_json_safe(_path(root, _INBOX), data, expected_revision=expected_revision)
    return {"added": added, "duplicates": duplicates}


def set_proposal_status(root: Path, proposal_id: str, status: str, *, expected_revision) -> dict:
    _expected(expected_revision)
    if status not in {"pending", "deferred", "rejected"}:
        raise ProposalError("指定した提案状態へ変更できません。")
    with atomic_json_operation(root=root, kind="proposal_status", expected_revisions={_path(root, _INBOX): expected_revision}):
        data = _read_inbox(root)
        entry = _entry(data, proposal_id)
        if entry["status"] in {"applied", "reverted"}:
            raise ProposalError("採用・取消済みの提案は履歴として保持します。状態だけの変更はできません。")
        entry.update(status=status, updated_at=_now())
        validate_inbox(data)
        json_store.save_json_safe(_path(root, _INBOX), data, expected_revision=expected_revision)
    return deepcopy(entry)


def _date(value) -> str:
    if type(value) is date:
        return value.isoformat()
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    raise ProposalError("計画メモの対象日を選び直してください。")


def _target(kind: str, material_id: str, target_date) -> tuple[dict, str]:
    if kind == "next_step":
        if not isinstance(material_id, str) or not material_id or len(material_id) > 200:
            raise ProposalError("反映先の教材を明示的に選択してください。")
        return {"kind": kind, "material_id": material_id, "target_date": "", "path": _CONTEXT}, _CONTEXT
    if kind == "daily_memo":
        token = _date(target_date)
        path = f"daily_plans/{token[:4]}/{token[5:7]}/{token.replace('-', '')}.json"
        return {"kind": kind, "material_id": "", "target_date": token, "path": path}, path
    raise ProposalError("参考情報は自動反映しません。教材の引き継ぎか計画メモを明示的に選択してください。")


def _memos(value) -> list:
    return validate_plan_memos(value)


def _seal(preview: dict) -> dict:
    data = deepcopy(preview)
    data["preview_hash"] = hmac.new(_PREVIEW_SECRET, _canonical(data), hashlib.sha256).hexdigest()
    return data


def _verify(root: Path, preview: dict, action: str) -> dict:
    if not isinstance(preview, dict):
        raise ProposalError("確認画面を作り直してください。")
    data = deepcopy(preview)
    signature = data.pop("preview_hash", None)
    if (not isinstance(signature, str) or not _HASH.fullmatch(signature)
            or not hmac.compare_digest(signature, hmac.new(_PREVIEW_SECRET, _canonical(data), hashlib.sha256).hexdigest())
            or data.get("root") != _root_key(root) or data.get("action") != action):
        raise ProposalError("確認内容が変更されたか有効期限が切れました。内容を確認し直してください。")
    return data


def prepare_application(root: Path, proposal_id: str, *, kind: str, text: str, title: str = "",
                        material_id: str = "", target_date=None, expected_revision) -> dict:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_NEXT_STEP_LENGTH:
        raise ProposalError("反映する本文は空白だけにせず、10000文字以内で入力してください。")
    if not isinstance(title, str) or len(title) > 200:
        raise ProposalError("タイトルは200文字以内で入力してください。")
    if any(ord(char) < 32 and char not in "\n\r\t" or ord(char) == 127 for char in text + title):
        raise ProposalError("反映する本文またはタイトルに未対応の制御文字があります。")
    target, relative = _target(kind, material_id, target_date)
    with operation_guard(root):
        data = _read_inbox(root)
        _assert_revision(root, expected_revision)
        entry = _entry(data, proposal_id)
        if entry["status"] not in {"pending", "deferred"}:
            raise ProposalError("未確認または保留中の提案だけを採用できます。却下済みは先に未確認へ戻してください。")
        timestamp = _now()
        path = _path(root, relative)
        revisions = {_INBOX: expected_revision}
        if kind == "next_step":
            original = json_store.load_json_safe(path, {"schema_version": 1, "materials": {}})
            updated = _validate_context(original)
            materials = {item["material_id"]: item for item in list_materials(root)}
            revisions[_MASTER] = json_store.observed_json_revision(_path(root, _MASTER))
            if material_id not in materials or not materials[material_id]["is_active"]:
                raise ProposalError("教材が削除・無効化されています。有効な教材を選び直してください。")
            before_value = deepcopy(updated["materials"].get(material_id))
            before_present = material_id in updated["materials"]
            current = deepcopy(before_value) if before_present else {
                "material_id": material_id, "links": [], "created_at": timestamp,
            }
            old_text = current.get("next_step", "")
            current.update(next_step=text, updated_at=timestamp)
            updated["materials"][material_id] = current
            _validate_context(updated)
            applied_value, display_before, display_after = text, old_text, text
        else:
            from data_utils import default_daily_plan
            original = json_store.load_json_safe(path, None)
            if not isinstance(original, dict) and json_store.observed_json_revision(path) is not None:
                raise ProposalError("既存の日次計画の形式が不正です。上書きしません。")
            updated = deepcopy(original) if original is not None else default_daily_plan(target["target_date"])
            if updated.get("date", target["target_date"]) != target["target_date"]:
                raise ProposalError("日次計画の記録日と反映先が一致しません。")
            before_present = "consultation_memos" in updated
            before_value = deepcopy(updated.get("consultation_memos"))
            memos = _memos(before_value) if before_present else []
            if any(item["proposal_id"] == proposal_id for item in memos):
                raise ProposalError("同じ提案の計画メモがすでにあります。重複追加しません。")
            applied_value = {"proposal_id": proposal_id, "title": title, "text": text, "adopted_at": timestamp}
            updated["consultation_memos"] = [*memos, applied_value]
            display_before, display_after = memos, updated["consultation_memos"]
        revisions[relative] = json_store.observed_json_revision(path)
        before = {"file_existed": revisions[relative] is not None, "field_present": before_present, "value": before_value}
        application = {"operation_id": uuid4().hex, "kind": kind, "target_path": relative,
            "material_id": target["material_id"], "target_date": target["target_date"], "applied_at": timestamp,
            "after_revision": _revision(updated), "before": before, "applied_value": applied_value}
        _recheck_revisions(root, revisions)
        return _seal({"root": _root_key(root), "action": "apply", "proposal_id": proposal_id,
            "operation_id": application["operation_id"], "kind": kind, "title": title, "text": text,
            "material_id": target["material_id"], "target_date": target["target_date"], "target": target,
            "before": deepcopy(display_before), "after": deepcopy(display_after), "revisions": revisions,
            "_target_document": updated, "_application": application})


def _committed_entry(root: Path, preview: dict, key: str) -> dict:
    data = validate_inbox(json_store._read(_path(root, _INBOX))[0])
    entry = _entry(data, preview["proposal_id"])
    if entry.get("application", {}).get(key) != preview["operation_id"]:
        raise ProposalError("完了済み操作と提案の履歴が一致しません。自動再適用しません。")
    return deepcopy(entry)


def approve_application(root: Path, preview: dict) -> dict:
    plan = _verify(root, preview, "apply")
    with atomic_json_operation(root=root, kind="proposal_apply", operation_id=plan["operation_id"],
                              expected_revisions={_path(root, key): value for key, value in plan["revisions"].items()}) as operation:
        if operation.already_committed:
            return _committed_entry(root, plan, "operation_id")
        data = _read_inbox(root)
        entry = _entry(data, plan["proposal_id"])
        if entry["status"] not in {"pending", "deferred"}:
            raise ProposalError("提案の状態が変更されています。採用内容を確認し直してください。")
        if plan["kind"] == "next_step":
            materials = {item["material_id"]: item for item in list_materials(root)}
            if plan["material_id"] not in materials or not materials[plan["material_id"]]["is_active"]:
                raise ProposalError("教材が削除・無効化されています。反映先を確認し直してください。")
        entry.update(status="applied", updated_at=plan["_application"]["applied_at"], application=deepcopy(plan["_application"]))
        validate_inbox(data)
        json_store.save_json_safe(_path(root, plan["target"]["path"]), plan["_target_document"])
        json_store.save_json_safe(_path(root, _INBOX), data)
    return deepcopy(entry)


def _application(entry: dict) -> dict:
    app = entry.get("application")
    try:
        target, relative = _target(app["kind"], app["material_id"], app["target_date"])
        before = app["before"]
        if (app["target_path"] != relative or not _OP_ID.fullmatch(app["operation_id"])
                or not _HASH.fullmatch(app["after_revision"]) or not isinstance(before, dict)
                or type(before["file_existed"]) is not bool or type(before["field_present"]) is not bool
                or "value" not in before):
            raise ValueError("invalid application")
        if before["field_present"]:
            if app["kind"] == "next_step":
                _validate_context({"schema_version": 1, "materials": {app["material_id"]: before["value"]}})
            else:
                _memos(before["value"])
        elif before["value"] is not None:
            raise ValueError("unexpected before")
        return deepcopy(app)
    except Exception:
        raise ProposalError("採用履歴の復元情報が不正です。自動取消は行いません。") from None


def prepare_revert(root: Path, proposal_id: str, *, expected_revision) -> dict:
    with operation_guard(root):
        data = _read_inbox(root)
        _assert_revision(root, expected_revision)
        entry = _entry(data, proposal_id)
        if entry["status"] != "applied":
            raise ProposalError("採用済みの提案だけを取り消せます。")
        application = _application(entry)
        target, relative = _target(application["kind"], application["material_id"], application["target_date"])
        path = _path(root, relative)
        current = json_store.load_json_safe(path, None)
        revision = json_store.observed_json_revision(path)
        if revision != application["after_revision"] or not isinstance(current, dict):
            raise json_store.JsonConflictError("採用後に反映先が編集されています。後の編集を守るため取消を停止しました。")
        updated = deepcopy(current)
        before = application["before"]
        if application["kind"] == "next_step":
            _validate_context(current)
            display_before = current["materials"].get(application["material_id"], {}).get("next_step", "")
            if before["field_present"]:
                updated["materials"][application["material_id"]] = deepcopy(before["value"])
            else:
                updated["materials"].pop(application["material_id"], None)
            _validate_context(updated)
            display_after = before["value"].get("next_step", "") if before["field_present"] else ""
        else:
            display_before = _memos(current.get("consultation_memos"))
            if before["field_present"]:
                updated["consultation_memos"] = deepcopy(before["value"])
            else:
                updated.pop("consultation_memos", None)
            display_after = deepcopy(before["value"]) if before["field_present"] else []
        revisions = {_INBOX: expected_revision, relative: revision}
        _recheck_revisions(root, revisions)
        return _seal({"root": _root_key(root), "action": "revert", "proposal_id": proposal_id,
            "operation_id": uuid4().hex, "kind": application["kind"], "title": entry["proposal"]["title"],
            "text": application["applied_value"] if application["kind"] == "next_step" else application["applied_value"]["text"],
            "material_id": target["material_id"], "target_date": target["target_date"], "target": target,
            "before": display_before, "after": display_after,
            "revisions": revisions,
            "_target_document": updated, "_reverted_at": _now()})


def revert_application(root: Path, preview: dict) -> dict:
    plan = _verify(root, preview, "revert")
    with atomic_json_operation(root=root, kind="proposal_revert", operation_id=plan["operation_id"],
                              expected_revisions={_path(root, key): value for key, value in plan["revisions"].items()}) as operation:
        if operation.already_committed:
            return _committed_entry(root, plan, "revert_operation_id")
        data = _read_inbox(root)
        entry = _entry(data, plan["proposal_id"])
        if entry["status"] != "applied":
            raise ProposalError("提案の採用状態が変更されています。取消内容を確認し直してください。")
        _application(entry)
        entry.update(status="reverted", updated_at=plan["_reverted_at"])
        entry["application"].update(reverted_at=plan["_reverted_at"], revert_operation_id=plan["operation_id"])
        validate_inbox(data)
        json_store.save_json_safe(_path(root, plan["target"]["path"]), plan["_target_document"])
        json_store.save_json_safe(_path(root, _INBOX), data)
    return deepcopy(entry)
