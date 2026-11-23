"""Date-only study records stored atomically in their own document."""
from __future__ import annotations

from datetime import date, datetime
from hashlib import sha256
import json
import re
from pathlib import Path
from uuid import UUID, uuid4

from app_core.analysis_dates import parse_analysis_date
from app_core.json_store import (
    JsonConflictError,
    JsonReadError,
    json_transaction,
    load_json_safe,
    resolved_json_path,
    save_json_safe,
)
from app_core.study_log_service import load_all_study_sessions, session_duration_sec


MAX_PERIOD_DAYS = 366
MAX_DURATION_MINUTES = 24 * 60
_BATCH_KINDS = {"week", "period"}
_MASTER_FIELDS = ("project_id", "subject_id", "material_id", "unit_id")


def rough_study_path(root: Path) -> Path:
    root = resolved_json_path(root)
    path = resolved_json_path(root / "rough_study_logs" / "batches.json")
    if not path.is_relative_to(root):
        raise ValueError("ざっくり記録の保存先がアプリのフォルダー外です。")
    return path


def _load_document(path: Path) -> dict:
    data = load_json_safe(path, {"schema_version": 1, "batches": {}, "operations": {}})
    if (
        not isinstance(data, dict)
        or type(data.get("schema_version")) is not int
        or data["schema_version"] != 1
        or not isinstance(data.get("batches"), dict)
        or not isinstance(data.get("operations"), dict)
    ):
        raise JsonReadError("ざっくり記録の保存形式を読み込めません。アプリ内からの上書きは行いません。")
    try:
        for batch_id, batch in data["batches"].items():
            _validate_stored_batch(batch_id, batch)
        for operation_id, receipt in data["operations"].items():
            _validate_stored_receipt(operation_id, receipt)
    except ValueError as exc:
        raise JsonReadError(
            "保存済みのざっくり記録を読み込めません。内容とバックアップを確認してください。"
        ) from exc
    return data


def load_rough_study_batches(root: Path) -> list[dict]:
    batches = _load_document(rough_study_path(root))["batches"]
    return sorted(
        batches.values(),
        key=lambda batch: (str(batch.get("created_at", "")), batch["batch_id"]),
        reverse=True,
    )


def load_rough_study_records(root: Path) -> list[dict]:
    return [
        {**row, "batch_id": batch["batch_id"], "kind": batch["kind"]}
        for batch in load_rough_study_batches(root)
        for row in batch["rows"]
    ]


def _iso_date(value: object, label: str) -> date:
    if type(value) is date:
        return value
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
            if value == parsed.isoformat():
                return parsed
        except ValueError:
            pass
    raise ValueError(f"{label}を正しい日付で入力してください。")


def _range_dates(start: date, end: date) -> None:
    days = (end - start).days + 1
    if not 1 <= days <= MAX_PERIOD_DAYS:
        raise ValueError(f"期間は1日以上、{MAX_PERIOD_DAYS}日以内にしてください。")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label}を文字で入力してください。")
    return value.strip()


def _uuid(value: object, label: str) -> str:
    if isinstance(value, str):
        try:
            return UUID(value).hex
        except ValueError:
            pass
    raise ValueError(f"{label}が不正です。")


def _validate_kind(kind: str) -> None:
    if not isinstance(kind, str) or kind not in _BATCH_KINDS:
        raise ValueError("ざっくり記録の入力方法が不正です。")


def validate_rough_study_rows(rows: list[dict], *, kind: str) -> list[dict]:
    """Normalize a nonempty preview without touching stored records."""
    _validate_kind(kind)
    if not isinstance(rows, list) or not rows:
        raise ValueError("保存する日を選び、学習時間または内容を入力してください。")

    result = []
    dates: list[date] = []
    record_ids: set[str] = set()
    for index, row in enumerate(rows, start=1):
        label = f"{index}行目"
        if not isinstance(row, dict):
            raise ValueError(f"{label}の入力が不正です。")
        day = _iso_date(row.get("study_date"), f"{label}の学習日")
        duration = row.get("duration_sec")
        if duration is not None and (type(duration) is not int or not 1 <= duration <= MAX_DURATION_MINUTES * 60):
            raise ValueError(f"{label}の学習時間は0秒より長く、24時間以内にしてください。")
        estimated = row.get("estimated", kind == "period")
        if type(estimated) is not bool:
            raise ValueError(f"{label}の概算区分が不正です。")
        memo = _text(row.get("memo", ""), f"{label}の内容")
        master = {field: _text(row.get(field, ""), f"{label}の分類") for field in _MASTER_FIELDS}
        if duration is None and not (memo or any(master.values())):
            raise ValueError(f"{label}に学習時間、内容、または科目などを入力してください。")

        entry = {"study_date": day.isoformat(), "duration_sec": duration,
                 "estimated": estimated and duration is not None, "memo": memo, **master}
        if row.get("record_id"):
            record_id = _uuid(row["record_id"], f"{label}の記録ID")
            if record_id in record_ids:
                raise ValueError("同じ記録を複数の行に割り当てることはできません。")
            record_ids.add(record_id)
            entry["record_id"] = record_id
        dates.append(day)
        result.append(entry)

    if kind == "week" and len({(day.isocalendar().year, day.isocalendar().week) for day in dates}) != 1:
        raise ValueError("週まとめは月曜日から日曜日までの同じ週を選んでください。")
    _range_dates(min(dates), max(dates))
    return result


def _period_metadata(kind: str, period: dict | None, rows: list[dict]) -> dict | None:
    if kind == "week":
        if period is not None:
            raise ValueError("週まとめには期間入力の設定を指定できません。")
        return None
    if not isinstance(period, dict):
        raise ValueError("期間入力の設定を確認してください。")
    start = _iso_date(period.get("start_date"), "開始日")
    end = _iso_date(period.get("end_date"), "終了日")
    _range_dates(start, end)
    minutes = period.get("average_minutes")
    if type(minutes) is not int or not 1 <= minutes <= MAX_DURATION_MINUTES:
        raise ValueError("1日平均の学習時間は1分以上、24時間以内にしてください。")
    weekdays_only = period.get("weekdays_only", False)
    if type(weekdays_only) is not bool:
        raise ValueError("期間入力の対象曜日が不正です。")
    for row in rows:
        day = date.fromisoformat(row["study_date"])
        if not start <= day <= end:
            raise ValueError("期間入力の学習日は指定した期間に含めてください。")
    return {"start_date": start.isoformat(), "end_date": end.isoformat(),
            "average_minutes": minutes, "weekdays_only": weekdays_only}


def _validate_stored_batch(batch_id: str, batch: object) -> None:
    if (
        not isinstance(batch, dict)
        or _uuid(batch_id, "一括記録ID") != batch_id
        or batch.get("batch_id") != batch_id
        or type(batch.get("revision")) is not int
        or batch["revision"] < 1
    ):
        raise ValueError("保存済みの一括記録の管理情報が不正です。")
    for key in ("created_at", "updated_at"):
        value = batch.get(key)
        if not isinstance(value, str):
            raise ValueError("保存済みの一括記録の更新日時が不正です。")
        datetime.fromisoformat(value)

    rows = validate_rough_study_rows(batch.get("rows"), kind=batch.get("kind"))
    if rows != batch["rows"] or any("record_id" not in row for row in rows):
        raise ValueError("保存済みの学習日の記録形式が不正です。")
    if _period_metadata(batch["kind"], batch.get("period"), rows) != batch.get("period"):
        raise ValueError("保存済みの期間入力の設定が不正です。")


def _validate_stored_receipt(operation_id: str, receipt: object) -> None:
    if (
        _uuid(operation_id, "保存操作ID") != operation_id
        or not isinstance(receipt, dict)
        or not isinstance(receipt.get("fingerprint"), str)
        or re.fullmatch(r"[0-9a-f]{64}", receipt["fingerprint"]) is None
        or _uuid(receipt.get("batch_id"), "一括記録ID") != receipt["batch_id"]
        or type(receipt.get("revision")) is not int
        or receipt["revision"] < 1
    ):
        raise ValueError("保存済みの操作情報が不正です。")


def _fingerprint(request: dict) -> str:
    encoded = json.dumps(request, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return sha256(encoded).hexdigest()


def _replay(data: dict, operation_id: str, fingerprint: str) -> dict | None:
    if operation_id not in data["operations"]:
        return None
    receipt = data["operations"][operation_id]
    if not isinstance(receipt, dict) or receipt.get("fingerprint") != fingerprint:
        raise JsonConflictError("同じ保存操作が異なる内容で再送されました。入力を保持しています。")
    return receipt


def _get_batch(data: dict, batch_id: str, expected_revision: int) -> dict:
    batch = data["batches"].get(batch_id)
    if batch is None or batch["revision"] != expected_revision:
        raise JsonConflictError("この一括記録は変更または削除されています。最新の記録を読み直してください。")
    return batch


def _check_revision(batch_id: str | None, expected_revision: int | None) -> None:
    if batch_id is None and expected_revision is None:
        return
    if batch_id is None or type(expected_revision) is not int or expected_revision < 1:
        raise ValueError("一括記録の更新番号が不正です。")


def save_rough_study_batch(
    root: Path, *, kind: str, rows: list[dict], operation_id: str,
    batch_id: str | None = None, expected_revision: int | None = None,
    period: dict | None = None,
) -> dict:
    """Append or replace one batch without partial daily writes or duplicate retries."""
    normalized = validate_rough_study_rows(rows, kind=kind)
    metadata = _period_metadata(kind, period, normalized)
    operation_id = _uuid(operation_id, "保存操作ID")
    batch_id = _uuid(batch_id, "一括記録ID") if batch_id is not None else None
    _check_revision(batch_id, expected_revision)
    request = {"action": "save", "batch_id": batch_id, "revision": expected_revision,
               "kind": kind, "rows": normalized, "period": metadata}
    fingerprint = _fingerprint(request)
    path = rough_study_path(root)
    with json_transaction(path):
        data = _load_document(path)
        replay = _replay(data, operation_id, fingerprint)
        if replay is not None:
            current = data["batches"].get(replay["batch_id"], {})
            return {**current, "batch_id": replay["batch_id"], "revision": replay["revision"], "replayed": True}
        now = datetime.now().isoformat(timespec="seconds")
        if batch_id is None:
            batch_id = uuid4().hex
            previous_ids = set()
            created_at = now
            revision = 1
        else:
            previous = _get_batch(data, batch_id, expected_revision)
            if previous["kind"] != kind:
                raise ValueError("既存の一括記録の入力方法は変更できません。")
            previous_ids = {row["record_id"] for row in previous["rows"]}
            created_at = previous["created_at"]
            revision = previous["revision"] + 1
        for row in normalized:
            if "record_id" in row:
                if row["record_id"] not in previous_ids:
                    raise ValueError("別の一括記録の行を保存することはできません。")
            else:
                row["record_id"] = uuid4().hex
        batch = {"batch_id": batch_id, "revision": revision, "kind": kind, "rows": normalized,
                 "period": metadata, "created_at": created_at, "updated_at": now}
        data["batches"][batch_id] = batch
        data["operations"][operation_id] = {"fingerprint": fingerprint, "batch_id": batch_id, "revision": revision}
        save_json_safe(path, data)
    return {**batch, "replayed": False}


def delete_rough_study_batch(
    root: Path, *, batch_id: str, expected_revision: int, operation_id: str,
) -> None:
    batch_id = _uuid(batch_id, "一括記録ID")
    operation_id = _uuid(operation_id, "保存操作ID")
    _check_revision(batch_id, expected_revision)
    fingerprint = _fingerprint({"action": "delete", "batch_id": batch_id, "revision": expected_revision})
    path = rough_study_path(root)
    with json_transaction(path):
        data = _load_document(path)
        if _replay(data, operation_id, fingerprint) is not None:
            return
        _get_batch(data, batch_id, expected_revision)
        del data["batches"][batch_id]
        data["operations"][operation_id] = {"fingerprint": fingerprint, "batch_id": batch_id,
                                            "revision": expected_revision}
        save_json_safe(path, data)


def _timed_study_day(row: dict) -> date | None:
    day = parse_analysis_date(row.get("start_at"))
    if day is not None:
        return day
    try:
        return datetime.strptime(Path(row.get("source_file") or "").stem, "%Y%m%d").date()
    except ValueError:
        return None


def get_existing_study_days(
    root: Path, start: date, end: date, *, exclude_batch_id: str | None = None,
) -> dict[date, dict[str, int | float]]:
    """Return preview totals including content-only records and legacy file dates."""
    start = _iso_date(start, "開始日")
    end = _iso_date(end, "終了日")
    _range_dates(start, end)
    days: dict[date, dict[str, int | float]] = {}

    def totals(day: date) -> dict[str, int | float]:
        return days.setdefault(day, {"timed_seconds": 0, "rough_seconds": 0, "timed_count": 0, "rough_count": 0})

    for row in load_all_study_sessions(root):
        day = _timed_study_day(row)
        if day is not None and start <= day <= end:
            totals(day)["timed_seconds"] += session_duration_sec(row)
            totals(day)["timed_count"] += 1
    for row in load_rough_study_records(root):
        if row["batch_id"] == exclude_batch_id:
            continue
        day = date.fromisoformat(row["study_date"])
        if start <= day <= end:
            totals(day)["rough_seconds"] += row["duration_sec"] or 0
            totals(day)["rough_count"] += 1
    return days
