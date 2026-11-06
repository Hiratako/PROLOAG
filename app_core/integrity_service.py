from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from config import (
    DAILY_PLANS_DIR,
    DIARY_ENTRIES_DIR,
    EXERCISE_LOGS_DIR,
    FLASHCARDS_PATH,
    HEALTH_LOGS_DIR,
    MASTER_DATA_DIR,
    NOTES_DATA_DIR,
    PROJECT_ROOT,
    REMINDERS_PATH,
    STUDY_LOGS_DIR,
)
from app_core.integrity_rules import ERROR, NOTICE, _read_json, run_deep_integrity_checks
from app_core.json_store import record_json_files


Issue = dict[str, Any]


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _issue(
    category: str,
    path: Path,
    item_id: str,
    message: str,
    recommendation: str,
    severity: str = ERROR,
) -> Issue:
    return {
        "重要度": severity,
        "問題カテゴリ": category,
        "対象ファイル": _rel(path),
        "対象ID": item_id or "未設定",
        "内容": message,
        "推奨対応": recommendation,
    }


def _json_files(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(record_json_files(folder))


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _load_json_for_audit(path: Path, default: Any) -> Any:
    """Build reference context without stopping on files already reported by the deep audit."""
    data, error, _ = _read_json(path)
    return default if error is not None else data


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _valid_date(value: Any) -> bool:
    if not value:
        return False
    try:
        datetime.fromisoformat(str(value)[:10])
        return True
    except ValueError:
        return False


def _load_notes() -> tuple[list[dict[str, Any]], Path]:
    path = NOTES_DATA_DIR / "notebooks.json"
    store = _load_json_for_audit(path, {"notebooks": []})
    notes: list[dict[str, Any]] = []
    if not isinstance(store, dict):
        return notes, path
    for notebook in _as_list(store.get("notebooks")):
        if not isinstance(notebook, dict):
            continue
        for section in _as_list(notebook.get("sections")):
            if not isinstance(section, dict):
                continue
            for page in _as_list(section.get("pages")):
                if isinstance(page, dict):
                    notes.append({**page, "_file": path})
    return notes, path


def _load_sessions() -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    for path in _json_files(STUDY_LOGS_DIR):
        data = _load_json_for_audit(path, [])
        if isinstance(data, dict):
            data = data.get("sessions", [])
        for item in _as_list(data):
            if isinstance(item, dict):
                sessions.append({**item, "_file": path})
    return sessions


def _load_tasks() -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for path in _json_files(DAILY_PLANS_DIR):
        data = _load_json_for_audit(path, {})
        if not isinstance(data, dict):
            continue
        for item in _as_list(data.get("tasks")):
            if isinstance(item, dict):
                tasks.append({**item, "_file": path, "_plan_date": data.get("date", "")})
    return tasks


def _load_cards() -> tuple[list[dict[str, Any]], Path]:
    data = _load_json_for_audit(FLASHCARDS_PATH, [])
    cards = []
    for item in _as_list(data):
        if not isinstance(item, dict):
            continue
        card_id = item.get("card_id") or item.get("flashcard_id")
        cards.append({**item, "card_id": card_id, "_file": FLASHCARDS_PATH})
    return cards, FLASHCARDS_PATH


def _load_reminders() -> tuple[list[dict[str, Any]], Path]:
    data = _load_json_for_audit(REMINDERS_PATH, [])
    reminders = [{**item, "_file": REMINDERS_PATH} for item in _as_list(data) if isinstance(item, dict)]
    return reminders, REMINDERS_PATH


def _load_master() -> tuple[dict[str, list[dict[str, Any]]], Path]:
    path = MASTER_DATA_DIR / "master.json"
    data = _load_json_for_audit(path, {"projects": [], "subjects": [], "materials": [], "units": [], "tags": []})
    if not isinstance(data, dict):
        data = {"projects": [], "subjects": [], "materials": [], "units": [], "tags": []}
    return {
        "projects": [item for item in _as_list(data.get("projects")) if isinstance(item, dict)],
        "subjects": [item for item in _as_list(data.get("subjects")) if isinstance(item, dict)],
        "materials": [item for item in _as_list(data.get("materials")) if isinstance(item, dict)],
        "units": [item for item in _as_list(data.get("units")) if isinstance(item, dict)],
        "tags": [item for item in _as_list(data.get("tags")) if isinstance(item, dict)],
    }, path


def check_duplicate_ids(records: list[dict[str, Any]], id_key: str, category: str) -> list[Issue]:
    ids = [str(record.get(id_key) or "") for record in records if record.get(id_key)]
    counts = Counter(ids)
    issues: list[Issue] = []
    for record in records:
        item_id = str(record.get(id_key) or "")
        if item_id and counts[item_id] > 1:
            issues.append(_issue(category, Path(record.get("_file", PROJECT_ROOT)), item_id, f"{id_key} が重複しています。", "バックアップ後に一意のIDへ修正してください。"))
    return issues


def check_missing_ids(records: list[dict[str, Any]], id_key: str, category: str) -> list[Issue]:
    issues: list[Issue] = []
    for record in records:
        if not record.get(id_key):
            issues.append(_issue(category, Path(record.get("_file", PROJECT_ROOT)), "", f"{id_key} が欠損しています。", "既存のID補完処理を通すか、バックアップ後にIDを付与してください。"))
    return issues


def _check_link_list(record: dict[str, Any], key: str, valid_ids: set[str], category: str, target_name: str) -> list[Issue]:
    issues: list[Issue] = []
    for value in _as_list(record.get(key)):
        if value and str(value) not in valid_ids:
            issues.append(_issue(category, Path(record.get("_file", PROJECT_ROOT)), str(record.get("note_id") or record.get("session_id") or record.get("task_id") or record.get("card_id") or record.get("reminder_id") or ""), f"{key} に存在しない {target_name} ID があります: {value}", "参照先を確認し、不要なリンクはバックアップ後に削除してください。"))
    return issues


def check_broken_references(context: dict[str, Any]) -> list[Issue]:
    issues: list[Issue] = []
    invalid = set(context.get("invalid_datasets", []))
    note_ids = context["note_ids"]
    session_ids = context["session_ids"]
    task_ids = context["task_ids"]
    card_ids = context["card_ids"]
    reminder_ids = context["reminder_ids"]

    for note in context["notes"]:
        if "sessions" not in invalid:
            issues.extend(_check_link_list(note, "linked_study_session_ids", session_ids, "ノート参照", "session"))
        if "cards" not in invalid:
            issues.extend(_check_link_list(note, "linked_flashcard_ids", card_ids, "ノート参照", "card"))
        if "reminders" not in invalid:
            issues.extend(_check_link_list(note, "linked_reminder_ids", reminder_ids, "ノート参照", "reminder"))
    for session in context["sessions"]:
        task_id = session.get("linked_task_id")
        if "plans" not in invalid and task_id and str(task_id) not in task_ids:
            issues.append(_issue("勉強ログ参照", Path(session.get("_file", PROJECT_ROOT)), str(session.get("session_id") or ""), f"linked_task_id の参照先が存在しません: {task_id}", "タスクが削除済みか確認してください。勉強ログ自体は保持できます。"))
        if "notes" not in invalid:
            issues.extend(_check_link_list(session, "linked_note_ids", note_ids, "勉強ログ参照", "note"))
    for task in context["tasks"]:
        if "sessions" not in invalid:
            issues.extend(_check_link_list(task, "linked_session_ids", session_ids, "学習プラン参照", "session"))
        if "notes" not in invalid:
            issues.extend(_check_link_list(task, "linked_note_ids", note_ids, "学習プラン参照", "note"))
    for card in context["cards"]:
        source_note_id = card.get("source_note_id")
        if "notes" not in invalid and source_note_id and str(source_note_id) not in note_ids:
            issues.append(_issue("カード参照", Path(card.get("_file", PROJECT_ROOT)), str(card.get("card_id") or card.get("flashcard_id") or ""), f"source_note_id の参照先が存在しません: {source_note_id}", "元ノートが削除済みか確認してください。"))
        if "notes" not in invalid:
            issues.extend(_check_link_list(card, "linked_note_ids", note_ids, "カード参照", "note"))
    for reminder in context["reminders"]:
        source_note_id = reminder.get("source_note_id")
        if "notes" not in invalid and source_note_id and str(source_note_id) not in note_ids:
            issues.append(_issue("リマインダー参照", Path(reminder.get("_file", PROJECT_ROOT)), str(reminder.get("reminder_id") or ""), f"source_note_id の参照先が存在しません: {source_note_id}", "元ノートが削除済みか確認してください。"))
        if "notes" not in invalid:
            issues.extend(_check_link_list(reminder, "linked_note_ids", note_ids, "リマインダー参照", "note"))
        source_card_id = reminder.get("source_card_id")
        if "cards" not in invalid and source_card_id and str(source_card_id) not in card_ids:
            issues.append(_issue("リマインダー参照", Path(reminder.get("_file", PROJECT_ROOT)), str(reminder.get("reminder_id") or ""), f"source_card_id の参照先が存在しません: {source_card_id}", "元カードが削除済みか確認してください。"))
        if "cards" not in invalid:
            issues.extend(_check_link_list(reminder, "linked_card_ids", card_ids, "リマインダー参照", "card"))
    return issues


def check_master_references(context: dict[str, Any]) -> list[Issue]:
    issues: list[Issue] = []
    if "master" in set(context.get("invalid_datasets", [])):
        return issues
    master_sets = context["master_sets"]
    key_map = {
        "project_id": ("project_ids", "プロジェクト"),
        "subject_id": ("subject_ids", "科目"),
        "material_id": ("material_ids", "教材"),
        "unit_id": ("unit_ids", "単元"),
    }
    for category, records in (
        ("ノート分類", context["notes"]),
        ("勉強ログ分類", context["sessions"]),
        ("学習プラン分類", context["tasks"]),
        ("カード分類", context["cards"]),
        ("リマインダー分類", context["reminders"]),
    ):
        for record in records:
            record_id = str(record.get("note_id") or record.get("session_id") or record.get("task_id") or record.get("card_id") or record.get("reminder_id") or "")
            for key, (set_name, label) in key_map.items():
                value = record.get(key)
                if value and str(value) not in master_sets[set_name]:
                    issues.append(_issue(category, Path(record.get("_file", PROJECT_ROOT)), record_id, f"{label}IDの参照先が存在しません: {value}", "マスタ管理画面で存在を確認し、必要に応じて再選択してください。"))

    for subject in context["master"]["subjects"]:
        if subject.get("project_id") and subject["project_id"] not in master_sets["project_ids"]:
            issues.append(_issue("マスタ参照", context["master_path"], str(subject.get("subject_id") or ""), f"科目の project_id が存在しません: {subject.get('project_id')}", "科目の所属プロジェクトを確認してください。"))
    for material in context["master"]["materials"]:
        if material.get("project_id") and material["project_id"] not in master_sets["project_ids"]:
            issues.append(_issue("マスタ参照", context["master_path"], str(material.get("material_id") or ""), f"教材の project_id が存在しません: {material.get('project_id')}", "教材の所属プロジェクトを確認してください。"))
        if material.get("subject_id") and material["subject_id"] not in master_sets["subject_ids"]:
            issues.append(_issue("マスタ参照", context["master_path"], str(material.get("material_id") or ""), f"教材の subject_id が存在しません: {material.get('subject_id')}", "教材の所属科目を確認してください。"))
    for unit in context["master"]["units"]:
        if unit.get("project_id") and unit["project_id"] not in master_sets["project_ids"]:
            issues.append(_issue("マスタ参照", context["master_path"], str(unit.get("unit_id") or ""), f"単元の project_id が存在しません: {unit.get('project_id')}", "単元の所属プロジェクトを確認してください。"))
        if unit.get("subject_id") and unit["subject_id"] not in master_sets["subject_ids"]:
            issues.append(_issue("マスタ参照", context["master_path"], str(unit.get("unit_id") or ""), f"単元の subject_id が存在しません: {unit.get('subject_id')}", "単元の所属科目を確認してください。"))
        if unit.get("material_id") and unit["material_id"] not in master_sets["material_ids"]:
            issues.append(_issue("マスタ参照", context["master_path"], str(unit.get("unit_id") or ""), f"単元の material_id が存在しません: {unit.get('material_id')}", "単元の所属教材を確認してください。"))
    return issues


def check_health_log_values() -> list[Issue]:
    issues: list[Issue] = []
    for path in _json_files(HEALTH_LOGS_DIR):
        data = _load_json_for_audit(path, {})
        if not isinstance(data, dict):
            issues.append(_issue("健康ログ", path, "", "JSON形式が想定と異なります。", "ファイル内容を確認してください。"))
            continue
        sleep = data.get("sleep") if isinstance(data.get("sleep"), dict) else {}
        condition = data.get("condition") if isinstance(data.get("condition"), dict) else {}
        if not data.get("date"):
            issues.append(_issue("健康ログ", path, "", "date が欠損しています。", "日付キーを確認してください。"))
        duration = _num(sleep.get("sleep_duration_min"))
        if duration is None or duration < 0 or duration > 1440:
            issues.append(_issue("健康ログ", path, str(data.get("date") or ""), "sleep_duration_min が不正です。", "睡眠時間を0〜1440分の範囲で確認してください。"))
        for key in ("sleep_quality",):
            value = _num(sleep.get(key))
            if value is None or not 1 <= value <= 5:
                issues.append(_issue("健康ログ", path, str(data.get("date") or ""), f"{key} が1〜5の範囲外です。", "入力値を確認してください。"))
        for key in ("fatigue", "stress", "focus_score"):
            value = _num(condition.get(key))
            if value is None or not 1 <= value <= 5:
                issues.append(_issue("健康ログ", path, str(data.get("date") or ""), f"{key} が1〜5の範囲外です。", "入力値を確認してください。"))
    return issues


def check_reminder_schedule_values(reminders: list[dict[str, Any]]) -> list[Issue]:
    """間隔反復リマインダーの追加フィールドを検査します。"""
    issues: list[Issue] = []
    valid_modes = {"single", "light", "standard", "intensive", "custom", "sm2"}
    valid_schedule_statuses = {"active", "paused", "cancelled", "completed"}
    for reminder in reminders:
        path = Path(reminder.get("_file", PROJECT_ROOT))
        reminder_id = str(reminder.get("reminder_id") or "")
        mode = str(reminder.get("schedule_mode") or "single")
        if mode not in valid_modes:
            issues.append(_issue("復習スケジュール", path, reminder_id, f"schedule_mode が不正です: {mode}", "有効な復習方式を再設定してください。"))
        schedule_status = str(reminder.get("schedule_status") or "active")
        if schedule_status not in valid_schedule_statuses:
            issues.append(_issue("復習スケジュール", path, reminder_id, f"schedule_status が不正です: {schedule_status}", "系列状態を確認してください。"))
        if mode != "single" and not reminder.get("schedule_id"):
            issues.append(_issue("復習スケジュール", path, reminder_id, "間隔反復予定に schedule_id がありません。", "バックアップ後に系列IDを付与してください。"))
        if mode in {"light", "standard", "intensive", "custom"}:
            intervals = reminder.get("intervals_days")
            if not isinstance(intervals, list) or any(_num(value) is None or float(value) <= 0 for value in intervals):
                issues.append(_issue("復習スケジュール", path, reminder_id, "intervals_days に不正な値があります。", "1日以上の整数間隔を設定してください。"))
        if mode == "sm2" or reminder.get("algorithm") == "sm2":
            ease_factor = _num(reminder.get("ease_factor"))
            interval_days = _num(reminder.get("interval_days"))
            if ease_factor is None or ease_factor < 1.3:
                issues.append(_issue("復習スケジュール", path, reminder_id, "ease_factor が1.3未満または不正です。", "SM-2状態を確認してください。"))
            if interval_days is None or interval_days < 0:
                issues.append(_issue("復習スケジュール", path, reminder_id, "interval_days が不正です。", "SM-2状態を確認してください。"))
        if not isinstance(reminder.get("review_history", []), list):
            issues.append(_issue("復習スケジュール", path, reminder_id, "review_history がリスト形式ではありません。", "復習履歴の形式を確認してください。"))
    return issues


def check_exercise_log_values() -> list[Issue]:
    issues: list[Issue] = []
    all_ids: list[str] = []
    records: list[dict[str, Any]] = []
    for path in _json_files(EXERCISE_LOGS_DIR):
        data = _load_json_for_audit(path, {})
        if not isinstance(data, dict):
            issues.append(_issue("運動ログ", path, "", "JSON形式が想定と異なります。", "ファイル内容を確認してください。"))
            continue
        for record in _as_list(data.get("records")):
            if isinstance(record, dict):
                records.append({**record, "_file": path})
                if record.get("exercise_id"):
                    all_ids.append(str(record.get("exercise_id")))
    counts = Counter(all_ids)
    for record in records:
        path = Path(record.get("_file", PROJECT_ROOT))
        exercise_id = str(record.get("exercise_id") or "")
        if not exercise_id:
            issues.append(_issue("運動ログ", path, "", "exercise_id が欠損しています。", "バックアップ後にIDを付与してください。"))
        elif counts[exercise_id] > 1:
            issues.append(_issue("運動ログ", path, exercise_id, "exercise_id が重複しています。", "バックアップ後に一意のIDへ修正してください。"))
        for key in ("duration_min", "distance_km", "pace_min_per_km"):
            value = _num(record.get(key))
            if value is None or value < 0:
                issues.append(_issue("運動ログ", path, exercise_id, f"{key} が不正です。", "0以上の数値として確認してください。"))
    return issues


def check_diary_caffeine_values() -> tuple[list[Issue], int]:
    issues: list[Issue] = []
    checks = 0
    all_ids: list[str] = []
    records: list[dict[str, Any]] = []
    for path in _json_files(DIARY_ENTRIES_DIR):
        data = _load_json_for_audit(path, {})
        checks += 1
        if not isinstance(data, dict):
            issues.append(_issue("日記カフェイン", path, "", "JSON形式が想定と異なります。", "ファイル内容を確認してください。"))
            continue
        daily_total = _num(data.get("caffeine_mg"))
        if daily_total is not None and daily_total < 0:
            issues.append(_issue("日記カフェイン", path, str(data.get("date") or ""), "caffeine_mg が負の値です。", "0以上の値へ修正してください。"))

        if "caffeine_records" not in data and "caffeine_untimed_mg" not in data:
            continue
        raw_records = data.get("caffeine_records", [])
        if not isinstance(raw_records, list):
            issues.append(_issue("日記カフェイン", path, str(data.get("date") or ""), "caffeine_records がリスト形式ではありません。", "バックアップ後に摂取履歴の形式を確認してください。"))
            continue
        untimed_mg = _num(data.get("caffeine_untimed_mg", 0))
        if untimed_mg is None or untimed_mg < 0:
            issues.append(_issue("日記カフェイン", path, str(data.get("date") or ""), "caffeine_untimed_mg が不正です。", "時刻不明分を0以上の数値として確認してください。"))
            untimed_mg = 0

        timed_total = 0.0
        diary_date = str(data.get("date") or "")[:10]
        for record in raw_records:
            checks += 1
            if not isinstance(record, dict):
                issues.append(_issue("日記カフェイン", path, "", "caffeine_records 内に辞書形式でない項目があります。", "不正な項目を確認してください。"))
                continue
            stored = {**record, "_file": path, "_diary_date": diary_date}
            records.append(stored)
            caffeine_id = str(record.get("caffeine_id") or "")
            if caffeine_id:
                all_ids.append(caffeine_id)
            amount = _num(record.get("amount_mg"))
            if amount is None or amount <= 0:
                issues.append(_issue("日記カフェイン", path, caffeine_id, "amount_mg が1以上の数値ではありません。", "摂取量を確認してください。"))
            else:
                timed_total += amount
            try:
                taken_at = datetime.fromisoformat(str(record.get("taken_at") or ""))
            except ValueError:
                taken_at = None
                issues.append(_issue("日記カフェイン", path, caffeine_id, "taken_at が有効な日時ではありません。", "ISO形式の摂取日時を確認してください。"))
            if taken_at is not None and diary_date and taken_at.date().isoformat() != diary_date:
                issues.append(_issue("日記カフェイン", path, caffeine_id, "摂取日と日記ファイルの日付が一致しません。", "正しい摂取日の記録へ移してください。"))

        if daily_total is None or abs(daily_total - (timed_total + float(untimed_mg or 0))) > 0.01:
            issues.append(_issue("日記カフェイン", path, str(data.get("date") or ""), "caffeine_mg が時刻付き記録と時刻不明分の合計に一致しません。", "日記ページで内容を確認して保存し直してください。"))

    counts = Counter(all_ids)
    for record in records:
        path = Path(record.get("_file", PROJECT_ROOT))
        caffeine_id = str(record.get("caffeine_id") or "")
        if not caffeine_id:
            issues.append(_issue("日記カフェイン", path, "", "caffeine_id が欠損しています。", "バックアップ後に一意のIDを付与してください。"))
        elif counts[caffeine_id] > 1:
            issues.append(_issue("日記カフェイン", path, caffeine_id, "caffeine_id が重複しています。", "バックアップ後に一意のIDへ修正してください。"))
    return issues, checks


def _build_context() -> dict[str, Any]:
    notes, notes_path = _load_notes()
    sessions = _load_sessions()
    tasks = _load_tasks()
    cards, cards_path = _load_cards()
    reminders, reminders_path = _load_reminders()
    master, master_path = _load_master()
    return {
        "notes": notes,
        "notes_path": notes_path,
        "sessions": sessions,
        "tasks": tasks,
        "cards": cards,
        "cards_path": cards_path,
        "reminders": reminders,
        "reminders_path": reminders_path,
        "master": master,
        "master_path": master_path,
        "note_ids": {str(item.get("note_id")) for item in notes if item.get("note_id")},
        "session_ids": {str(item.get("session_id")) for item in sessions if item.get("session_id")},
        "task_ids": {str(item.get("task_id")) for item in tasks if item.get("task_id")},
        "card_ids": {str(item.get("card_id") or item.get("flashcard_id")) for item in cards if item.get("card_id") or item.get("flashcard_id")},
        "reminder_ids": {str(item.get("reminder_id")) for item in reminders if item.get("reminder_id")},
        "master_sets": {
            "project_ids": {str(item.get("project_id")) for item in master["projects"] if item.get("project_id")},
            "subject_ids": {str(item.get("subject_id")) for item in master["subjects"] if item.get("subject_id")},
            "material_ids": {str(item.get("material_id")) for item in master["materials"] if item.get("material_id")},
            "unit_ids": {str(item.get("unit_id")) for item in master["units"] if item.get("unit_id")},
            "tag_ids": {str(item.get("tag_id")) for item in master["tags"] if item.get("tag_id")},
        },
    }


def run_integrity_checks() -> dict[str, Any]:
    deep_result = run_deep_integrity_checks()
    context = _build_context()
    context["invalid_datasets"] = deep_result.get("invalid_datasets", [])
    issues: list[Issue] = list(deep_result.get("issues", []))
    total_checks = int(deep_result.get("total_checks", 0))

    for records, id_key, category in (
        (context["notes"], "note_id", "ノートID"),
        (context["sessions"], "session_id", "勉強ログID"),
        (context["tasks"], "task_id", "タスクID"),
        (context["cards"], "card_id", "カードID"),
        (context["reminders"], "reminder_id", "リマインダーID"),
    ):
        total_checks += len(records) * 2
        issues.extend(check_missing_ids(records, id_key, category))
        issues.extend(check_duplicate_ids(records, id_key, category))

    for key, id_key in (("projects", "project_id"), ("subjects", "subject_id"), ("materials", "material_id"), ("units", "unit_id"), ("tags", "tag_id")):
        records = [{**item, "_file": context["master_path"]} for item in context["master"][key]]
        total_checks += len(records) * 2
        issues.extend(check_missing_ids(records, id_key, "マスタID"))
        issues.extend(check_duplicate_ids(records, id_key, "マスタID"))

    total_checks += len(context["notes"]) + len(context["sessions"]) + len(context["tasks"]) + len(context["cards"]) + len(context["reminders"])
    issues.extend(check_broken_references(context))
    issues.extend(check_master_references(context))

    severity_order = {ERROR: 0, NOTICE: 1}
    issues.sort(
        key=lambda item: (
            severity_order.get(str(item.get("重要度")), 9),
            str(item.get("問題カテゴリ", "")),
            str(item.get("対象ファイル", "")),
            str(item.get("対象ID", "")),
            str(item.get("内容", "")),
        )
    )
    error_count = sum(1 for item in issues if item.get("重要度") == ERROR)
    notice_count = sum(1 for item in issues if item.get("重要度") == NOTICE)

    return {
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_checks": total_checks,
        "checked_file_count": int(deep_result.get("checked_file_count", 0)),
        "issue_count": len(issues),
        "error_count": error_count,
        "notice_count": notice_count,
        "check_scope": deep_result.get("check_scope", []),
        "issues": issues,
    }
