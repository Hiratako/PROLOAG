from __future__ import annotations

from datetime import date, datetime
import math
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT
from app_core.json_store import json_transaction, load_json_safe, record_json_files


def _root(root: Path | None = None) -> Path:
    return root or PROJECT_ROOT


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def session_start_at(session: dict[str, Any]) -> datetime | None:
    value = session.get("start_at")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    except (OSError, OverflowError, ValueError):
        return None


def session_duration_sec(session: dict[str, Any]) -> float:
    return max(_num(session.get("duration_sec")), 0.0)


def duration_before_start(sessions: list[dict[str, Any]], start_at: datetime | None, *, exclude_session_id: str | None = None) -> float:
    total = 0.0
    for session in sessions:
        if not isinstance(session, dict):
            continue
        if exclude_session_id and session.get("session_id") == exclude_session_id:
            continue
        session_start = session_start_at(session)
        if start_at is None or session_start is None or session_start < start_at:
            total += session_duration_sec(session)
    return total


def calculate_session_money(
    duration_sec: int | float,
    hourly_rate: int | float,
    *,
    overtime_enabled: bool = True,
    threshold_hours: int | float = 8.0,
    multiplier: int | float = 1.25,
    previous_duration_sec: int | float = 0,
) -> dict[str, int]:
    duration = max(_num(duration_sec), 0.0)
    rate = max(_num(hourly_rate), 0.0)
    previous = max(_num(previous_duration_sec), 0.0)
    threshold_sec = max(_num(threshold_hours, 8.0), 0.0) * 3600
    overtime_rate = max(_num(multiplier, 1.25), 1.0)

    if not overtime_enabled:
        normal_sec = duration
        overtime_sec = 0.0
    else:
        normal_remaining = max(threshold_sec - previous, 0.0)
        normal_sec = min(duration, normal_remaining)
        overtime_sec = max(duration - normal_sec, 0.0)

    normal_money = normal_sec / 3600 * rate
    overtime_money = overtime_sec / 3600 * rate * overtime_rate
    total_money = normal_money + overtime_money
    return {
        "money": round(total_money),
        "normal_money": round(normal_money),
        "overtime_money": round(overtime_money),
    }


def calculate_session_money_with_daily_total(
    session: dict[str, Any],
    existing_sessions: list[dict[str, Any]],
    *,
    hourly_rate: int | float | None = None,
    overtime_enabled: bool = True,
    threshold_hours: int | float = 8.0,
    multiplier: int | float = 1.25,
) -> dict[str, int]:
    start_at = session_start_at(session)
    previous_duration = duration_before_start(
        existing_sessions,
        start_at,
        exclude_session_id=session.get("session_id"),
    )
    return calculate_session_money(
        session_duration_sec(session),
        hourly_rate if hourly_rate is not None else session.get("hourly_rate", 0),
        overtime_enabled=overtime_enabled,
        threshold_hours=threshold_hours,
        multiplier=multiplier,
        previous_duration_sec=previous_duration,
    )


def get_study_log_path(target: date | datetime | str, root: Path | None = None) -> Path:
    if isinstance(target, str):
        target_dt = datetime.fromisoformat(target[:10]).date()
    elif isinstance(target, datetime):
        target_dt = target.date()
    else:
        target_dt = target
    base = _root(root) / "study_logs"
    return base / f"{target_dt:%Y}" / f"{target_dt:%m}" / f"{target_dt:%Y%m%d}.json"


def load_study_sessions_by_date(target: date | datetime | str, root: Path | None = None, *, write_back: bool = False) -> list[dict[str, Any]]:
    from data_utils import load_study_sessions_file

    return load_study_sessions_file(get_study_log_path(target, root), write_back=write_back)


def save_study_sessions_by_date(target: date | datetime | str, sessions: list[dict[str, Any]], root: Path | None = None) -> None:
    from data_utils import save_study_sessions_file

    save_study_sessions_file(get_study_log_path(target, root), sessions)


def load_all_study_sessions(root: Path | None = None) -> list[dict[str, Any]]:
    from data_utils import load_all_study_sessions as _load

    return _load(_root(root))


def study_trash_path(root: Path | None = None) -> Path:
    return _root(root) / "backups" / "study_log_trash.json"


def study_id_registry_path(root: Path | None = None) -> Path:
    return _root(root) / "backups" / "study_session_ids.json"


def _registered_study_ids(root: Path | None = None) -> set[str]:
    values = load_json_safe(study_id_registry_path(root), [])
    if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
        raise ValueError("勉強ログのID予約台帳が不正です。バックアップを確認してください。")
    return set(values)


def reserve_study_session_ids(session_ids: set[str], root: Path | None = None) -> None:
    """Keep IDs through moves/rollbacks, even after a process interruption."""
    from app_core.json_store import save_json_safe

    with json_transaction(study_id_registry_path(root)):
        reserved = _registered_study_ids(root)
        if session_ids - reserved:
            save_json_safe(study_id_registry_path(root), sorted(reserved | session_ids))


def reserved_study_session_ids(root: Path | None = None) -> set[str]:
    """Never reuse an ID after moving, deleting or restoring a session."""
    reserved = {str(item["session_id"]) for item in load_all_study_sessions(root) if item.get("session_id")}
    trash = load_json_safe(study_trash_path(root), [])
    if not isinstance(trash, list):
        raise ValueError("勉強ログのごみ箱の形式が不正です。バックアップを確認してください。")
    for entry in trash:
        if not isinstance(entry, dict) or not isinstance(entry.get("session"), dict):
            raise ValueError("勉強ログのごみ箱に不正な記録があります。")
        if entry["session"].get("session_id"):
            reserved.add(str(entry["session"]["session_id"]))
    return reserved | _registered_study_ids(root)


def _saved_money_policy(session: dict[str, Any]) -> dict[str, Any]:
    """Never infer historical overtime settings while inserting a new record."""
    policy = session.get("money_policy")
    session_id = session.get("session_id", "ID未設定")
    required = {"overtime_enabled", "threshold_hours", "multiplier"}
    if not isinstance(policy, dict) or not required <= policy.keys():
        raise ValueError(
            f"後続の勉強記録 {session_id} の残業設定が未保存です。"
            "金額を推測して変更しないため追加を中止しました。先にその記録の金額計算設定を確認してください。"
        )
    enabled = policy["overtime_enabled"]
    if not isinstance(enabled, bool):
        raise ValueError(f"勉強記録 {session_id} の残業設定が不正です。")
    values = {}
    for key, minimum in (("threshold_hours", 0), ("multiplier", 1)):
        value = policy[key]
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"勉強記録 {session_id} の残業設定が不正です。") from exc
        if isinstance(value, bool) or not math.isfinite(number) or number < minimum:
            raise ValueError(f"勉強記録 {session_id} の残業設定が不正です。")
        values[key] = number
    return {"overtime_enabled": enabled, **values}


def _reprice_after_append(
    sessions: list[dict[str, Any]], session: dict[str, Any], timestamp: str,
) -> None:
    """Reprice only later records whose overtime allocation actually changes."""
    combined = [*sessions, session]
    for existing in sessions:
        start = session_start_at(existing)
        before = duration_before_start(sessions, start, exclude_session_id=existing.get("session_id"))
        after = duration_before_start(combined, start, exclude_session_id=existing.get("session_id"))
        if before == after:
            continue
        if start is None:
            raise ValueError("後続の勉強記録の開始日時が不正です。追加前に記録を修正してください。")
        policy = _saved_money_policy(existing)
        if not policy["overtime_enabled"]:
            continue
        values = {}
        for key in ("duration_sec", "hourly_rate"):
            value = existing.get(key)
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"後続の勉強記録 {existing.get('session_id')} の時間または時給が未記録・不正です。") from exc
            if isinstance(value, bool) or not math.isfinite(number) or number < 0:
                raise ValueError(f"後続の勉強記録 {existing.get('session_id')} の時間または時給が不正です。")
            values[key] = number
        old_money = calculate_session_money(**values, previous_duration_sec=before, **policy)
        new_money = calculate_session_money(**values, previous_duration_sec=after, **policy)
        if old_money != new_money:
            existing.update(new_money)
            existing["updated_at"] = timestamp


def append_study_session(session: dict[str, Any], root: Path | None = None) -> Path:
    from app_core.json_operations import operation_guard
    from app_core.json_store import _active_operation

    if _active_operation.get() is not None:
        return _append_study_session_locked(session, root)
    # Participate in inventory-sensitive note edits even for legacy/direct
    # single-file callers. UI operations perform the pending-recovery check;
    # preserve the direct service API's existing recovery/conflict semantics.
    with operation_guard(_root(root), allow_pending=True):
        return _append_study_session_locked(session, root)


def _append_study_session_locked(session: dict[str, Any], root: Path | None = None) -> Path:
    from data_utils import generate_id

    target = session.get("start_at") or datetime.now().isoformat()
    path = get_study_log_path(target, root)
    with json_transaction(path), json_transaction(study_id_registry_path(root)):
        sessions = load_study_sessions_by_date(target, root)
        existing_ids = reserved_study_session_ids(root) | {item.get("session_id", "") for item in sessions}
        if session.get("session_id") and session["session_id"] in existing_ids:
            raise ValueError("同じIDの勉強記録がすでにあります。再読込して記録を確認してください。")
        if not session.get("session_id"):
            session["session_id"] = generate_id("session", target, existing_ids)
        session.setdefault("start_at", target)
        # The preview may precede a concurrent edit of earlier sessions. New UI
        # records carry their policy, so finalize their amount under the day lock.
        policy = session.get("money_policy")
        if isinstance(policy, dict):
            session.update(calculate_session_money_with_daily_total(
                session, sessions,
                overtime_enabled=policy.get("overtime_enabled", True),
                threshold_hours=policy.get("threshold_hours", 8.0),
                multiplier=policy.get("multiplier", 1.25),
            ))
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _reprice_after_append(sessions, session, timestamp)
        session.setdefault("created_at", timestamp)
        session["updated_at"] = timestamp
        sessions.append(session)
        save_study_sessions_by_date(target, sessions, root)
    return path


def ensure_session_ids(root: Path | None = None) -> int:
    from data_utils import load_study_sessions_file

    count = 0
    for path in record_json_files(_root(root) / "study_logs"):
        before = load_study_sessions_file(path, write_back=False)
        after = load_study_sessions_file(path, write_back=True)
        count += sum(1 for old, new in zip(before, after) if old.get("session_id") != new.get("session_id"))
    return count


def find_session_by_id(session_id: str, root: Path | None = None) -> dict[str, Any] | None:
    return next((session for session in load_all_study_sessions(root) if session.get("session_id") == session_id), None)


def resolve_session_label(session_id: str | None, root: Path | None = None) -> str:
    if not session_id:
        return "未設定"
    session = find_session_by_id(session_id, root)
    if not session:
        return "不明なセッション"
    return f"{session.get('start_at', '')} {session.get('memo', '')}".strip()


def aggregate_daily_study_logs(sessions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for session in sessions:
        day = str(session.get("start_at", ""))[:10] or "未設定"
        row = rows.setdefault(day, {"duration_sec": 0, "money": 0, "sessions": 0})
        row["duration_sec"] += int(float(session.get("duration_sec", 0) or 0))
        row["money"] += int(float(session.get("money", 0) or 0))
        row["sessions"] += 1
    return rows


def attach_note_to_session(session_id: str, note_id: str, root: Path | None = None) -> bool:
    for session in load_all_study_sessions(root):
        if session.get("session_id") != session_id:
            continue
        start_at = session.get("start_at")
        with json_transaction(get_study_log_path(start_at, root)):
            sessions = load_study_sessions_by_date(start_at, root)
            for item in sessions:
                if item.get("session_id") == session_id:
                    linked = item.setdefault("linked_note_ids", [])
                    if note_id not in linked:
                        linked.append(note_id)
                        item["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        save_study_sessions_by_date(start_at, sessions, root)
                    return True
    return False


def attach_task_to_session(session_id: str, task_id: str, root: Path | None = None) -> bool:
    for session in load_all_study_sessions(root):
        if session.get("session_id") != session_id:
            continue
        start_at = session.get("start_at")
        with json_transaction(get_study_log_path(start_at, root)):
            sessions = load_study_sessions_by_date(start_at, root)
            for item in sessions:
                if item.get("session_id") == session_id:
                    if item.get("linked_task_id") != task_id:
                        item["linked_task_id"] = task_id
                        item["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        save_study_sessions_by_date(start_at, sessions, root)
                    return True
    return False
