"""Read-only, allowlisted learning snapshots for explicit Google sharing.

No transport, authentication, normalization, or source write belongs here.
The operation lock is held only while copying and verifying source documents.
Only a session's ``memo`` can become shared free text, and only when both
detail and content sharing are enabled. Other free text is never inferred.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

from app_core.analysis_dates import parse_analysis_datetime
from app_core import json_store
from app_core.json_operations import operation_guard
from app_core.study_record_reader import unpack_study_document
from app_core.study_category_service import default_category_catalog, validate_category_catalog, resolve_study_category, other_category_name


class ShareSnapshotError(json_store.JsonStoreError):
    """Do not send a snapshot when its source cannot be interpreted safely."""


_CLASSIFICATIONS = (
    ("project_id", "projects", "プロジェクト"),
    ("subject_id", "subjects", "科目"),
    ("material_id", "materials", "教材"),
    ("unit_id", "units", "単元"),
)
_CATEGORIES = {"医学", "英語", "語学", "資格", "IT", "読書", "仕事", "その他"}
_IO_TYPES = {"input": "インプット", "output": "アウトプット", "both": "両方",
             "インプット": "インプット", "アウトプット": "アウトプット", "両方": "両方"}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _inside(root: Path, path: Path) -> Path:
    resolved = json_store.resolved_json_path(path)
    if not resolved.is_relative_to(root):
        raise ShareSnapshotError("共有対象の保存先がプロジェクト外を参照しています。")
    if resolved != Path(os.path.abspath(path)):
        raise ShareSnapshotError("共有対象の保存先がリンクで別の場所を参照しています。")
    return resolved


def _inventory(root: Path, start: date, end: date) -> tuple[Path, ...]:
    """Inspect names only; read just the comparison period and offset margin.

    Date-keyed legacy locations are supported as well as YYYY/MM/YYYYMMDD.
    An undated live file cannot safely be assigned to a period, so fail closed.
    """
    folder = _inside(root, root / "study_logs")
    if not folder.exists():
        return ()
    if not folder.is_dir():
        raise ShareSnapshotError("勉強ログの保存先がフォルダではありません。")
    result = []

    def on_error(error: OSError) -> None:
        raise json_store.JsonReadError("勉強ログ一覧を読み込めません。共有を中止しました。") from error

    for directory, folders, names in os.walk(folder, onerror=on_error, followlinks=False):
        folders[:] = [name for name in folders if name.lower() not in {"backup", "backups"}]
        for name in folders:
            child = Path(directory) / name
            _inside(root, child)
            if child.is_symlink():
                raise ShareSnapshotError("勉強ログ内のリンクフォルダは共有対象にできません。")
        for name in names:
            path = Path(directory) / name
            if path.suffix.lower() != ".json" or "_backup_" in path.stem.lower():
                continue
            owner = None
            for pattern in ("%Y%m%d", "%Y-%m-%d"):
                try:
                    owner = datetime.strptime(path.stem, pattern).date()
                    break
                except ValueError:
                    pass
            if owner is None:
                raise ShareSnapshotError("日付を特定できない勉強ログがあります。データ整合性を確認してください。")
            if start <= owner <= end:
                _inside(root, path)
                result.append(path)
    return tuple(sorted(result, key=lambda path: path.relative_to(root).as_posix()))


def _revision(root: Path, path: Path) -> str | None:
    _inside(root, path)
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise json_store.JsonReadError("共有前の読取確認に失敗しました。共有を中止しました。") from exc


def _capture(root: Path, start: date, end: date) -> tuple[list[Any], Any, dict[str, Any]]:
    """Make a detached committed-source snapshot, rejecting concurrent changes."""
    if json_store._active_operation.get() is not None:
        raise ShareSnapshotError("保存操作中は共有データを作成できません。保存完了後に再実行してください。")
    with operation_guard(root):
        paths = _inventory(root, start, end)
        master_path = root / "master_data/master.json"
        category_path = root / "settings/study_categories.json"
        _inside(root, master_path)
        copies = []
        revisions = {}
        for path in (*paths, master_path, category_path):
            _inside(root, path)
            data, raw, revision = json_store._read(path)
            if path not in {master_path, category_path} and raw is None:
                raise json_store.JsonConflictError("読取中に勉強ログが削除されました。再実行してください。")
            copies.append(default_category_catalog() if path == category_path and raw is None else {} if path == master_path and raw is None else data)
            revisions[path] = revision
        if _inventory(root, start, end) != paths:
            raise json_store.JsonConflictError("読取中に勉強ログ一覧が変わりました。再実行してください。")
        for path, revision in revisions.items():
            if _revision(root, path) != revision:
                raise json_store.JsonConflictError("読取中に記録が変更されました。再実行してください。")
    try:
        catalog = validate_category_catalog(copies[-1])
    except ValueError as exc:
        raise ShareSnapshotError("勉強カテゴリ設定を確認できません。共有を中止しました。") from exc
    return copies[:-2], copies[-2], catalog


def _text(value: Any, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ShareSnapshotError(f"勉強ログの{field}が文字列ではありません。共有を中止しました。")
    return value


def _session(record: dict[str, Any], *, include_content: bool, category_catalog: dict[str, Any] | None = None) -> dict[str, Any]:
    starts = _text(record.get("start_at"), "開始日時")
    ends = _text(record.get("end_at"), "終了日時")
    started = parse_analysis_datetime(starts) if starts else None
    ended = parse_analysis_datetime(ends) if ends else None
    if (starts and started is None) or (ends and ended is None) or (started is None and ended is None):
        raise ShareSnapshotError("日時を解釈できない勉強ログがあります。共有を中止しました。")
    if started is not None and ended is not None and ended < started:
        raise ShareSnapshotError("開始より終了が早い勉強ログがあります。共有を中止しました。")
    duration = record.get("duration_sec")
    try:
        valid_duration = type(duration) in (int, float) and math.isfinite(duration) and duration >= 0
    except OverflowError:
        valid_duration = False
    if not valid_duration:
        raise ShareSnapshotError("学習時間が有限の非負数ではない勉強ログがあります。共有を中止しました。")
    condition = record.get("condition")
    if condition is not None and not isinstance(condition, dict):
        raise ShareSnapshotError("勉強ログの分類形式が不正です。共有を中止しました。")
    condition = condition or {}
    category = _text(condition.get("category") or record.get("category"), "学習カテゴリ")
    io_type = _text(condition.get("io_type") or record.get("io_type"), "入出力区分")
    # Share only explicitly registered names (including aliases). Arbitrary
    # legacy text still falls back to the registered "other" category.
    category = resolve_study_category(category, category_catalog, unknown=other_category_name(category_catalog)) if category else "未設定"
    result = {
        "session_id": _text(record.get("session_id"), "セッションID"),
        "date": (started or ended).date().isoformat(),
        "start_at": started.isoformat() if started is not None else "",
        "end_at": ended.isoformat() if ended is not None else "",
        "duration_sec": duration,
        **{key: _text(record.get(key), label + "ID") for key, _, label in _CLASSIFICATIONS},
        "category": category,
        "io_type": _IO_TYPES.get(io_type.lower(), "未設定"),
    }
    if include_content:
        result["memo"] = _text(record.get("memo"), "勉強内容")
    return result


def _stable_ids(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Legacy IDs do not depend on paths, document format, or array order.

    Equal allowlisted identity fields get an occurrence suffix. The ID set for
    indistinguishable duplicates is stable; it is not a persisted edit identity.
    Private memo content is excluded even when content sharing is enabled.
    """
    records.sort(key=lambda row: (row["date"], row["start_at"], _canonical(row)))
    seen = set()
    occurrences: dict[str, int] = defaultdict(int)
    for record in records:
        identifier = record["session_id"]
        if not identifier:
            digest = _hash({key: value for key, value in record.items() if key not in {"session_id", "memo"}})
            occurrences[digest] += 1
            identifier = f"legacy_{digest}_{occurrences[digest]}"
            record["session_id"] = identifier
        if identifier in seen:
            raise ShareSnapshotError("重複した学習セッションIDがあります。共有を中止しました。")
        seen.add(identifier)
    return records


def _master_names(master: Any, records: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    if not isinstance(master, dict):
        raise ShareSnapshotError("分類マスタの形式が不正です。共有を中止しました。")
    names = {}
    for key, collection, label in _CLASSIFICATIONS:
        wanted = {row[key] for row in records if row[key]}
        names[key] = {}
        items = master.get(collection, [])
        if not isinstance(items, list):
            raise ShareSnapshotError("分類マスタの一覧形式が不正です。共有を中止しました。")
        for item in items:
            if not isinstance(item, dict):
                raise ShareSnapshotError("分類マスタに不正な記録があります。共有を中止しました。")
            identifier = item.get(key)
            if not isinstance(identifier, str) or identifier not in wanted:
                continue
            if identifier in names[key]:
                raise ShareSnapshotError("参照先の分類IDが重複しています。共有を中止しました。")
            names[key][identifier] = _text(item.get("name"), label + "名") or "名称未設定"
    return names


def _total(records: list[dict[str, Any]]) -> float:
    try:
        result = math.fsum(row["duration_sec"] for row in records)
    except (OverflowError, ValueError) as exc:
        raise ShareSnapshotError("合計学習時間が表現可能な範囲を超えています。") from exc
    if not math.isfinite(result):
        raise ShareSnapshotError("合計学習時間が表現可能な範囲を超えています。")
    return result


def build_share_snapshot(
    root: Path, start_date: date, end_date: date, *, include_details: bool = True,
    include_content: bool = False, consultation_note: str = "", generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Return JSON-compatible tables with header rows, without sending anything.

    The immediately preceding equal-length period is compared. Two adjacent
    owner days cover ISO offset-to-local day changes. Missing sessions do not
    imply no learning. Content is omitted unless *both* content/details are on.
    ``snapshot_id`` hashes shared content, excluding generation time.
    """
    if type(start_date) is not date or type(end_date) is not date or end_date < start_date:
        raise ShareSnapshotError("共有期間の開始日・終了日を確認してください。")
    if type(include_details) is not bool or type(include_content) is not bool:
        raise ShareSnapshotError("共有オプションは真偽値で指定してください。")
    if not isinstance(consultation_note, str):
        raise ShareSnapshotError("相談事項は文字列で指定してください。")
    if generated_at is not None and not isinstance(generated_at, datetime):
        raise ShareSnapshotError("生成日時は日時で指定してください。")
    span = (end_date - start_date).days + 1
    try:
        previous_start = start_date - timedelta(days=span)
        previous_end = start_date - timedelta(days=1)
        read_start = previous_start - timedelta(days=2)
        read_end = end_date + timedelta(days=2)
    except OverflowError as exc:
        raise ShareSnapshotError("共有期間が日時の有効範囲を超えています。") from exc
    root = json_store.resolved_json_path(root)
    documents, master, category_catalog = _capture(root, read_start, read_end)
    rows = []
    for raw in documents:
        document = unpack_study_document(raw)
        if document.container_kind == "unsupported" or document.ignored_items:
            raise ShareSnapshotError("未対応または不正な勉強ログ形式があります。共有を中止しました。")
        for _, record in document.records:
            row = _session(record, include_content=include_details and include_content, category_catalog=category_catalog)
            if previous_start.isoformat() <= row["date"] <= end_date.isoformat():
                rows.append(row)
    rows = _stable_ids(rows)
    current = [row for row in reversed(rows) if start_date.isoformat() <= row["date"] <= end_date.isoformat()]
    previous = [row for row in rows if previous_start.isoformat() <= row["date"] <= previous_end.isoformat()]
    names = _master_names(master, current)

    def name(key: str, identifier: str) -> str:
        return names[key].get(identifier, "不明") if identifier else "未設定"

    total, old_total = _total(current), _total(previous)
    counts = {"study_sessions": len(current), "study_days": len({row["date"] for row in current})}
    overview = [
        ["項目", "値"], ["対象", "保存済み学習記録（学習時間は理解度・正答率ではありません）"],
        ["期間開始", start_date.isoformat()], ["期間終了", end_date.isoformat()],
        ["総学習時間（秒）", total], ["総学習時間（時間）", total / 3600],
        ["学習セッション数", counts["study_sessions"]],
        ["学習記録のある日数", counts["study_days"]], ["期間日数", span],
        ["1日平均学習時間（秒）", total / span],
        ["前期間開始", previous_start.isoformat()], ["前期間終了", previous_end.isoformat()],
        ["前期間総学習時間（秒）", old_total], ["前期間総学習時間（時間）", old_total / 3600],
        ["前期間学習セッション数", len(previous)],
        ["前期間学習記録のある日数", len({row["date"] for row in previous})],
        ["前期間との差（秒）", total - old_total],
        ["日付基準", "PCのローカル開始日（開始日時がない旧記録は終了日）"],
        ["注意", "記録がないことは未学習を意味しません。理解度・正答率・教材消化率は推定していません。"],
    ]
    if consultation_note:
        overview.append(["相談事項", consultation_note])
    daily = [["日付", "学習時間（秒）", "セッション数", "学習セッションあり"]]
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in current:
        by_day[row["date"]].append(row)
    for offset in range(span - 1, -1, -1):
        day = (start_date + timedelta(days=offset)).isoformat()
        records = by_day[day]
        daily.append([day, _total(records), len(records), bool(records)])
    tables = {"overview": overview, "daily_summary": daily}
    for table_key, key, label in (("subjects", "subject_id", "科目"), ("materials", "material_id", "教材")):
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in current:
            groups[row[key]].append(row)
        tables[table_key] = [[label + "ID", label, "学習時間（秒）", "セッション数", "学習日数"]]
        for identifier in sorted(groups):
            records = groups[identifier]
            tables[table_key].append([identifier, name(key, identifier), _total(records), len(records),
                                      len({row["date"] for row in records})])
    categories: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in current:
        categories[(row["category"], row["io_type"])].append(row)
    tables["categories"] = [["学習カテゴリ", "入出力区分", "学習時間（秒）", "セッション数"]]
    for (category, io_type), records in sorted(categories.items()):
        tables["categories"].append([category, io_type, _total(records), len(records)])
    if include_details:
        header = ["セッションID", "日付", "開始日時", "終了日時", "学習時間（秒）"]
        for key, _, label in _CLASSIFICATIONS:
            header.extend([label + "ID", label])
        header.extend(["学習カテゴリ", "入出力区分"])
        if include_content:
            header.append("勉強内容")
        details = [header]
        for row in current:
            detail = [row[key] for key in ("session_id", "date", "start_at", "end_at", "duration_sec")]
            for key, _, _ in _CLASSIFICATIONS:
                detail.extend([row[key], name(key, row[key])])
            detail.extend([row["category"], row["io_type"]])
            if include_content:
                detail.append(row["memo"])
            details.append(detail)
        tables["study_sessions"] = details
    payload = {
        "schema_version": 1,
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "options": {"include_details": include_details, "include_content": include_content},
        "counts": counts,
        "tables": tables,
    }
    return {**payload, "snapshot_id": _hash(payload),
            "generated_at": (generated_at or datetime.now().astimezone()).isoformat()}
