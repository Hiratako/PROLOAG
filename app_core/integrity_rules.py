from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any
from app_core.json_store import record_json_files
from app_core.navigation import NAV_GROUPS, SETTINGS_PAGE_PATH

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
    SETTINGS_DIR,
    STATISTICS_PRACTICE_DIR,
    STUDY_LOGS_DIR,
    SUBSTANCE_PROFILES_PATH,
    USER_SETTINGS_PATH,
)


Issue = dict[str, Any]
ERROR = "エラー"
NOTICE = "注意"

CHECK_SCOPE = [
    "JSON構文、文字コード、重複キー、NaN・Infinity",
    "日次ファイル名・年/月フォルダ・記録日の一致",
    "ID欠損・重複、参照切れ、マスタ階層",
    "勉強時間、開始終了日時、金額内訳、セッション条件",
    "日次計画、タスク状態、復習対象、双方向リンク",
    "日記、良い席の確保、自己投資費用、カフェイン互換合計、成分設定・摂取日時",
    "睡眠・体調、運動、ランニングペース、日記同期",
    "ノート階層、カード互換フィールド、復習スケジュール",
    "ユーザー設定、テーマ、カウントダウン、サイドバー表示、デモ固定日",
    "教材リンク・次はここからの形式、教材参照、Google URL、作成更新日時",
    "手動取り込みの相談提案箱、承認状態・内容ハッシュ・適用履歴、日次相談メモの提案参照",
    "統計演習履歴、解答スナップショット、得点・問題情報の整合性",
    "学習ロードマップの形式、日付・進捗・当初予定、前提工程の参照・循環・日程、学習可能時間、日別タスクとの関連",
    "内容が同一の二重保存候補",
]

DATED_DATASETS = (
    ("sessions", "勉強ログ", STUDY_LOGS_DIR),
    ("plans", "日次計画", DAILY_PLANS_DIR),
    ("diaries", "日記", DIARY_ENTRIES_DIR),
    ("health", "健康ログ", HEALTH_LOGS_DIR),
    ("exercise", "運動ログ", EXERCISE_LOGS_DIR),
    ("statistics_practice", "統計演習履歴", STATISTICS_PRACTICE_DIR),
)


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _issue(
    severity: str,
    category: str,
    path: Path,
    item_id: str,
    message: str,
    recommendation: str,
) -> Issue:
    return {
        "重要度": severity,
        "問題カテゴリ": category,
        "対象ファイル": _rel(path),
        "対象ID": item_id or "未設定",
        "内容": message,
        "推奨対応": recommendation,
    }


class Audit:
    def __init__(self) -> None:
        self.issues: list[Issue] = []
        self.checks = 0

    def check(
        self,
        condition: bool,
        severity: str,
        category: str,
        path: Path,
        item_id: str,
        message: str,
        recommendation: str,
    ) -> bool:
        self.checks += 1
        if not condition:
            self.issues.append(_issue(severity, category, path, item_id, message, recommendation))
        return condition

    def flag(
        self,
        severity: str,
        category: str,
        path: Path,
        item_id: str,
        message: str,
        recommendation: str,
    ) -> None:
        self.checks += 1
        self.issues.append(_issue(severity, category, path, item_id, message, recommendation))


def _json_files(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(record_json_files(folder))


def _all_json_files() -> list[Path]:
    paths: set[Path] = set()
    for _, _, folder in DATED_DATASETS:
        paths.update(_json_files(folder))
    for path in (NOTES_DATA_DIR / "notebooks.json", MASTER_DATA_DIR / "master.json", FLASHCARDS_PATH, REMINDERS_PATH):
        if path.exists():
            paths.add(path)
    paths.update(_json_files(SETTINGS_DIR))
    return sorted(paths)


def _read_json(path: Path) -> tuple[Any, str | None, list[str]]:
    duplicate_keys: list[str] = []

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                duplicate_keys.append(key)
            result[key] = value
        return result

    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle, object_pairs_hook=object_pairs), None, duplicate_keys
    except json.JSONDecodeError as exc:
        return None, f"JSONDecodeError（{exc.lineno}行 {exc.colno}列）", duplicate_keys
    except UnicodeError as exc:
        return None, type(exc).__name__, duplicate_keys
    except OSError as exc:
        return None, f"{type(exc).__name__}（読み取り失敗）", duplicate_keys


def _nonfinite_paths(value: Any, prefix: str = "$") -> list[str]:
    paths: list[str] = []
    if isinstance(value, float) and not math.isfinite(value):
        paths.append(prefix)
    elif isinstance(value, dict):
        for key, child in value.items():
            paths.extend(_nonfinite_paths(child, f"{prefix}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.extend(_nonfinite_paths(child, f"{prefix}[{index}]"))
    return paths


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _signature_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return repr(value)
    try:
        hash(value)
        return value
    except TypeError:
        return repr(value)


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if len(text) < 10 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text[:10]):
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _parse_datetime(value: Any) -> datetime | None:
    try:
        if isinstance(value, datetime):
            parsed = value
        else:
            text = str(value or "").strip()
            if not text:
                return None
            parsed = datetime.fromisoformat(text)
        # Stored naive timestamps use the app machine's local time. Match that
        # convention only in memory so offset-bearing records can be compared.
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _valid_time(value: Any) -> bool:
    text = str(value or "").strip()
    if len(text) < 5:
        return False
    try:
        datetime.strptime(text[:5], "%H:%M")
        return True
    except ValueError:
        return False


def _path_date(path: Path) -> date | None:
    if not re.fullmatch(r"\d{8}", path.stem):
        return None
    try:
        return datetime.strptime(path.stem, "%Y%m%d").date()
    except ValueError:
        return None


def _record_id(record: dict[str, Any]) -> str:
    for key in (
        "session_id",
        "plan_id",
        "task_id",
        "note_id",
        "card_id",
        "flashcard_id",
        "reminder_id",
        "exercise_id",
        "attempt_id",
        "caffeine_id",
        "project_id",
        "subject_id",
        "material_id",
        "unit_id",
        "tag_id",
    ):
        if record.get(key):
            return str(record[key])
    return ""


def _check_dated_path(audit: Audit, path: Path, category: str) -> date | None:
    file_date = _path_date(path)
    if not audit.check(
        file_date is not None,
        ERROR,
        category,
        path,
        "",
        "ファイル名が有効な YYYYMMDD.json ではありません。",
        "ファイル名と保存日を確認してください。自動移動は行いません。",
    ):
        return None
    assert file_date is not None
    audit.check(
        path.parent.name == f"{file_date:%m}" and path.parent.parent.name == f"{file_date:%Y}",
        ERROR,
        category,
        path,
        "",
        "年/月フォルダとファイル名の日付が一致しません。",
        "バックアップ後に正しい年/月フォルダへの配置を確認してください。",
    )
    return file_date


def _check_record_date(audit: Audit, path: Path, value: Any, category: str, item_id: str = "") -> date | None:
    record_date = _parse_date(value)
    if not audit.check(
        record_date is not None,
        ERROR,
        category,
        path,
        item_id,
        "date が欠損しているか、有効な YYYY-MM-DD ではありません。",
        "記録日を確認してください。",
    ):
        return None
    file_date = _path_date(path)
    if file_date is not None:
        audit.check(
            record_date == file_date,
            ERROR,
            category,
            path,
            item_id,
            "記録日と日次ファイルの日付が一致しません。",
            "内容を確認し、バックアップ後に正しい日付のファイルへ整理してください。",
        )
    return record_date


def _check_timestamps(audit: Audit, record: dict[str, Any], path: Path, category: str, item_id: str = "") -> None:
    missing = [key for key in ("created_at", "updated_at") if not record.get(key)]
    audit.checks += 2
    if missing:
        audit.issues.append(
            _issue(
                NOTICE,
                category,
                path,
                item_id,
                f"{', '.join(missing)} が欠損しています。",
                "旧形式として利用できますが、編集日時の追跡が必要ならバックアップ後に補完してください。",
            )
        )
        return
    created = _parse_datetime(record.get("created_at"))
    updated = _parse_datetime(record.get("updated_at"))
    audit.check(
        created is not None and updated is not None,
        ERROR,
        category,
        path,
        item_id,
        "created_at または updated_at が有効な日時ではありません。",
        "ISO形式または YYYY-MM-DD HH:MM:SS 形式の日時を確認してください。",
    )
    if created is not None and updated is not None:
        audit.check(
            updated >= created,
            ERROR,
            category,
            path,
            item_id,
            "updated_at が created_at より前です。",
            "作成日時と更新日時を確認してください。",
        )


def _check_list(
    audit: Audit,
    record: dict[str, Any],
    key: str,
    path: Path,
    category: str,
    item_id: str,
    *,
    required: bool = True,
) -> list[Any]:
    if key not in record and not required:
        audit.checks += 1
        return []
    value = record.get(key)
    if not audit.check(
        isinstance(value, list),
        ERROR if key in record else NOTICE,
        category,
        path,
        item_id,
        f"{key} がリスト形式ではありません。" if key in record else f"{key} が欠損しています。",
        "保存形式を確認してください。互換読込では値が空になる可能性があります。",
    ):
        return []
    assert isinstance(value, list)
    tokens = [str(item) for item in value if item not in (None, "")]
    audit.check(
        len(tokens) == len(set(tokens)),
        NOTICE,
        category,
        path,
        item_id,
        f"{key} 内に同じ値が複数あります。",
        "表示への影響は限定的ですが、バックアップ後に重複リンクを1件へ整理してください。",
    )
    return value


def _check_number_range(
    audit: Audit,
    record: dict[str, Any],
    key: str,
    path: Path,
    category: str,
    item_id: str,
    minimum: float | None = None,
    maximum: float | None = None,
    *,
    required: bool = True,
) -> float | None:
    if key not in record and not required:
        audit.checks += 1
        return None
    number = _number(record.get(key))
    valid = number is not None
    if valid and minimum is not None:
        valid = number >= minimum
    if valid and maximum is not None:
        valid = number <= maximum
    bounds = []
    if minimum is not None:
        bounds.append(f"{minimum:g}以上")
    if maximum is not None:
        bounds.append(f"{maximum:g}以下")
    audit.check(
        valid,
        ERROR,
        category,
        path,
        item_id,
        f"{key} が数値でないか、許容範囲（{'・'.join(bounds) or '有限値'}）外です。",
        "入力値を確認してください。",
    )
    return number if valid else None


def _load_and_check_json(audit: Audit) -> tuple[dict[str, Any], set[str]]:
    workspace: dict[str, Any] = {
        "sessions": [],
        "plans": [],
        "tasks": [],
        "diaries": [],
        "health": [],
        "exercise_logs": [],
        "exercise_records": [],
        "statistics_practice_days": [],
        "statistics_attempts": [],
        "notes": [],
        "notebooks": [],
        "sections": [],
        "cards": [],
        "reminders": [],
        "master": {"projects": [], "subjects": [], "materials": [], "units": [], "tags": []},
        "settings": {},
        "learning_context": None,
        "consultation_inbox": None,
        "substance_profiles": [],
        "demo_mode": {},
    }
    invalid: set[str] = set()

    for dataset, category, folder in DATED_DATASETS:
        for path in _json_files(folder):
            _check_dated_path(audit, path, "JSON・ファイル配置")
            data, error, duplicate_keys = _read_json(path)
            if error:
                audit.flag(ERROR, "JSON構文", path, "", f"JSONを読み込めません: {error}", "バックアップとファイル末尾を確認し、JSON構文を修正してください。")
                invalid.add(dataset)
                continue
            audit.checks += 1
            for key in sorted(set(duplicate_keys)):
                audit.flag(ERROR, "JSON構文", path, "", f"同じオブジェクト内でキー '{key}' が重複しています。", "後勝ちで値が失われるため、バックアップ後に正しい値を1つだけ残してください。")
                invalid.add(dataset)
            for value_path in _nonfinite_paths(data):
                audit.flag(ERROR, "JSON値", path, "", f"NaN または Infinity が含まれています: {value_path}", "有限の数値または null へ修正してください。")

            if dataset == "sessions":
                if isinstance(data, dict) and isinstance(data.get("sessions"), list):
                    audit.flag(NOTICE, "旧形式", path, "", "勉強ログが {'sessions': [...]} の旧形式です。", "現在も読込可能ですが、次回の明示的な保存時に配列形式へ統一できます。")
                    rows = data["sessions"]
                elif isinstance(data, list):
                    audit.checks += 1
                    rows = data
                else:
                    audit.flag(ERROR, "JSON構造", path, "", "勉強ログの最上位が配列形式ではありません。", "セッション辞書の配列であることを確認してください。")
                    invalid.add(dataset)
                    continue
                for index, item in enumerate(rows):
                    if not isinstance(item, dict):
                        audit.flag(ERROR, "JSON構造", path, str(index), "勉強ログ配列に辞書形式でない項目があります。", "不正な項目を確認してください。")
                        invalid.add(dataset)
                        continue
                    workspace["sessions"].append({**item, "_file": path})
                continue

            if dataset == "diaries" and isinstance(data, list):
                audit.flag(NOTICE, "旧形式", path, "", "日記が配列形式の旧データです。最後の項目が表示対象になります。", "過去互換で読込可能です。編集して保存すると現在形式へ統一されます。")
                dict_rows = [item for item in data if isinstance(item, dict)]
                audit.check(len(dict_rows) == len(data) and bool(dict_rows), ERROR, "JSON構造", path, "", "日記配列に不正な項目があるか、配列が空です。", "最後の有効な日記辞書を確認してください。")
                if not dict_rows:
                    invalid.add(dataset)
                    continue
                data = dict_rows[-1]

            if not isinstance(data, dict):
                audit.flag(ERROR, "JSON構造", path, "", f"{category}の最上位が辞書形式ではありません。", "ファイルの保存形式を確認してください。")
                invalid.add(dataset)
                continue
            audit.checks += 1
            stored = {**data, "_file": path}
            if dataset == "plans":
                workspace["plans"].append(stored)
                tasks = data.get("tasks")
                if isinstance(tasks, list):
                    for index, item in enumerate(tasks):
                        if isinstance(item, dict):
                            workspace["tasks"].append({**item, "_file": path, "_plan_date": data.get("date", "")})
                        else:
                            audit.flag(ERROR, "JSON構造", path, str(index), "tasks に辞書形式でない項目があります。", "不正なタスク項目を確認してください。")
                            invalid.add(dataset)
                elif tasks is not None:
                    invalid.add(dataset)
            elif dataset == "diaries":
                workspace["diaries"].append(stored)
            elif dataset == "health":
                workspace["health"].append(stored)
            elif dataset == "exercise":
                workspace["exercise_logs"].append(stored)
                records = data.get("records")
                if isinstance(records, list):
                    for index, item in enumerate(records):
                        if isinstance(item, dict):
                            workspace["exercise_records"].append({**item, "_file": path, "_log_date": data.get("date", "")})
                        else:
                            audit.flag(ERROR, "JSON構造", path, str(index), "records に辞書形式でない項目があります。", "不正な運動記録を確認してください。")
                            invalid.add(dataset)
                elif records is not None:
                    invalid.add(dataset)
            elif dataset == "statistics_practice":
                workspace["statistics_practice_days"].append(stored)
                attempts = data.get("attempts")
                if isinstance(attempts, list):
                    for index, item in enumerate(attempts):
                        if isinstance(item, dict):
                            workspace["statistics_attempts"].append(
                                {
                                    **item,
                                    "_file": path,
                                    "_practice_date": data.get("date", ""),
                                    "_attempt_index": index,
                                }
                            )
                        else:
                            audit.flag(
                                ERROR,
                                "統計演習履歴構造",
                                path,
                                str(index),
                                "attempts に辞書形式でない項目があります。",
                                "不正な履歴項目を確認してください。",
                            )
                            invalid.add(dataset)
                else:
                    invalid.add(dataset)

    singleton_specs = (
        ("notes", NOTES_DATA_DIR / "notebooks.json", dict, {"notebooks": []}),
        ("master", MASTER_DATA_DIR / "master.json", dict, {"projects": [], "subjects": [], "materials": [], "units": [], "tags": []}),
        ("cards", FLASHCARDS_PATH, list, []),
        ("reminders", REMINDERS_PATH, list, []),
    )
    for dataset, path, expected_type, default in singleton_specs:
        if not path.exists():
            workspace[dataset] = default
            audit.checks += 1
            continue
        data, error, duplicate_keys = _read_json(path)
        if error:
            audit.flag(ERROR, "JSON構文", path, "", f"JSONを読み込めません: {error}", "バックアップとファイル末尾を確認し、JSON構文を修正してください。")
            invalid.add(dataset)
            workspace[dataset] = default
            continue
        audit.checks += 1
        for key in sorted(set(duplicate_keys)):
            audit.flag(ERROR, "JSON構文", path, "", f"同じオブジェクト内でキー '{key}' が重複しています。", "後勝ちで値が失われるため、正しい値を1つだけ残してください。")
            invalid.add(dataset)
        for value_path in _nonfinite_paths(data):
            audit.flag(ERROR, "JSON値", path, "", f"NaN または Infinity が含まれています: {value_path}", "有限の数値または null へ修正してください。")
        if not isinstance(data, expected_type):
            audit.flag(ERROR, "JSON構造", path, "", f"最上位が {expected_type.__name__} 形式ではありません。", "保存形式を確認してください。")
            invalid.add(dataset)
            workspace[dataset] = default
        else:
            workspace[dataset] = data

    for path in _json_files(SETTINGS_DIR):
        data, error, duplicate_keys = _read_json(path)
        optional_datasets = {SETTINGS_DIR / "learning_context.json": "learning_context",
                             SETTINGS_DIR / "consultation_inbox.json": "consultation_inbox",
                             SETTINGS_DIR / "study_categories.json": "study_categories",
                             SETTINGS_DIR / "study_roadmap.json": "roadmap"}
        dataset = optional_datasets.get(path, "settings")
        if error:
            audit.flag(ERROR, "JSON構文", path, "", f"JSONを読み込めません: {error}", "設定ファイルのJSON構文を確認してください。")
            invalid.add(dataset)
            continue
        audit.checks += 1
        for key in sorted(set(duplicate_keys)):
            audit.flag(ERROR, "JSON構文", path, "", f"同じオブジェクト内でキー '{key}' が重複しています。", "正しい設定値を1つだけ残してください。")
            invalid.add(dataset)
        for value_path in _nonfinite_paths(data):
            audit.flag(ERROR, "JSON値", path, "", f"NaN または Infinity が含まれています: {value_path}", "有限の数値または null へ修正してください。")
        if not isinstance(data, dict):
            audit.flag(ERROR, "JSON構造", path, "", "設定ファイルの最上位が辞書形式ではありません。", "設定ファイルの保存形式を確認してください。")
            invalid.add(dataset)
            continue
        if path == USER_SETTINGS_PATH:
            workspace["settings"] = data
        elif path == SETTINGS_DIR / "learning_context.json":
            workspace["learning_context"] = data
        elif path == SETTINGS_DIR / "consultation_inbox.json":
            workspace["consultation_inbox"] = data
        elif path == SUBSTANCE_PROFILES_PATH:
            _check_number_range(
                audit,
                data,
                "schema_version",
                path,
                "成分設定構造",
                "schema_version",
                1,
                1,
            )
            profiles = data.get("profiles")
            if audit.check(
                isinstance(profiles, list),
                ERROR,
                "成分設定構造",
                path,
                "profiles",
                "profiles が配列形式ではありません。",
                "ユーザー設定の成分管理から保存し直してください。",
            ):
                workspace["substance_profiles"] = profiles
        elif path.name == "demo_mode.json":
            workspace["demo_mode"] = data
        elif path.name == "study_roadmap.json":
            from app_core.roadmap_service import validate_roadmap
            try:
                workspace["roadmap"] = validate_roadmap(data)
                audit.checks += 1
            except ValueError as exc:
                audit.flag(ERROR, "学習ロードマップ", path, "", str(exc), "ロードマップ設定のバックアップを確認してください。")
                invalid.add("roadmap")
        elif path.name == "study_categories.json":
            from app_core.study_category_service import validate_category_catalog
            try:
                validate_category_catalog(data)
                audit.checks += 1
            except ValueError as exc:
                audit.flag(ERROR, "勉強カテゴリ設定", path, "", str(exc), "カテゴリ設定のバックアップを確認してください。")
                invalid.add("study_categories")

    return workspace, invalid


def _validate_study_sessions(audit: Audit, sessions: list[dict[str, Any]]) -> None:
    signatures: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for session in sessions:
        path = Path(session["_file"])
        session_id = str(session.get("session_id") or "")
        start = _parse_datetime(session.get("start_at"))
        end = _parse_datetime(session.get("end_at"))
        audit.check(start is not None, ERROR, "勉強ログ日時", path, session_id, "start_at が欠損しているか、有効な日時ではありません。", "セッション開始日時を確認してください。")
        audit.check(end is not None, ERROR, "勉強ログ日時", path, session_id, "end_at が欠損しているか、有効な日時ではありません。", "セッション終了日時を確認してください。")
        if start is not None:
            file_date = _path_date(path)
            if file_date is not None:
                audit.check(start.date() == file_date, ERROR, "勉強ログ日時", path, session_id, "start_at の日付と保存ファイルの日付が一致しません。", "開始日に対応する日次ファイルへ保存されているか確認してください。")
        if start is not None and end is not None:
            audit.check(end >= start, ERROR, "勉強ログ日時", path, session_id, "end_at が start_at より前です。", "開始・終了日時を確認してください。")

        _check_number_range(audit, session, "duration_sec", path, "勉強ログ値", session_id, 0.000001)
        _check_number_range(audit, session, "hourly_rate", path, "勉強ログ値", session_id, 0)
        money = _check_number_range(audit, session, "money", path, "勉強ログ値", session_id, 0)
        has_normal = "normal_money" in session
        has_overtime = "overtime_money" in session
        audit.check(
            has_normal == has_overtime,
            NOTICE,
            "勉強ログ金額",
            path,
            session_id,
            "normal_money と overtime_money の片方だけが保存されています。",
            "合計 money は利用できますが、通常・残業内訳を確認してください。",
        )
        if has_normal and has_overtime:
            normal = _check_number_range(audit, session, "normal_money", path, "勉強ログ金額", session_id, 0)
            overtime = _check_number_range(audit, session, "overtime_money", path, "勉強ログ金額", session_id, 0)
            if money is not None and normal is not None and overtime is not None:
                audit.check(abs(money - normal - overtime) <= 2, ERROR, "勉強ログ金額", path, session_id, "money が normal_money + overtime_money と一致しません。", "残業設定と保存時の金額内訳を確認してください。")

        _check_list(audit, session, "linked_note_ids", path, "勉強ログ構造", session_id, required=False)
        condition = session.get("condition")
        if condition is not None:
            if audit.check(isinstance(condition, dict), ERROR, "勉強ログ条件", path, session_id, "condition が辞書形式ではありません。", "セッション条件の保存形式を確認してください。"):
                assert isinstance(condition, dict)
                _check_number_range(audit, condition, "effort", path, "勉強ログ条件", session_id, 1, 10, required=False)
                _check_number_range(audit, condition, "sleep_hours", path, "勉強ログ条件", session_id, 0, 24, required=False)
                _check_number_range(audit, condition, "mood", path, "勉強ログ条件", session_id, 1, 5, required=False)
                if condition.get("alcohol_yesterday") not in (None, "", "なし", "あり"):
                    audit.flag(NOTICE, "勉強ログ条件", path, session_id, "alcohol_yesterday が現在の選択肢と異なります。", "旧形式の可能性があります。表示に問題がある場合だけ再選択してください。")
                else:
                    audit.checks += 1
                if condition.get("io_type") not in (None, "", "インプット", "アウトプット", "両方"):
                    audit.flag(NOTICE, "勉強ログ条件", path, session_id, "io_type が現在の選択肢と異なります。", "インプット・アウトプット分析に含める場合は値を確認してください。")
                else:
                    audit.checks += 1
        else:
            audit.checks += 1

        signatures[tuple(
            _signature_value(session.get(key))
            for key in ("start_at", "end_at", "duration_sec", "project_id", "subject_id", "material_id", "unit_id", "memo")
        )].append(session)

    for duplicates in signatures.values():
        audit.checks += 1
        if len(duplicates) > 1:
            first = duplicates[0]
            audit.issues.append(_issue(NOTICE, "二重保存候補", Path(first["_file"]), _record_id(first), f"内容が同一の勉強セッションが {len(duplicates)} 件あります。", "意図した別記録か確認し、不要な場合のみバックアップ後に整理してください。"))


def _validate_plans(audit: Audit, workspace: dict[str, Any], invalid: set[str]) -> None:
    plans = workspace["plans"]
    tasks = workspace["tasks"]
    note_ids = {str(item.get("note_id")) for item in workspace["notes"] if item.get("note_id")}
    card_ids = {str(item.get("card_id") or item.get("flashcard_id")) for item in workspace["cards"] if item.get("card_id") or item.get("flashcard_id")}
    task_ids = {str(item.get("task_id")) for item in tasks if item.get("task_id")}
    plan_id_counts = Counter(str(plan.get("plan_id") or "") for plan in plans if plan.get("plan_id"))

    for plan in plans:
        path = Path(plan["_file"])
        plan_id = str(plan.get("plan_id") or "")
        record_date = _check_record_date(audit, path, plan.get("date"), "日次計画", plan_id)
        audit.check(bool(plan_id), ERROR, "日次計画", path, plan_id, "plan_id が欠損しています。", "バックアップ後に日付由来の plan_id を付与してください。")
        if plan_id:
            audit.check(plan_id_counts[plan_id] == 1, ERROR, "日次計画", path, plan_id, "plan_id が別ファイルと重複しています。", "各日次計画のIDを一意にしてください。")
        if record_date is not None and plan_id:
            audit.check(plan_id == f"plan_{record_date:%Y%m%d}", NOTICE, "日次計画", path, plan_id, "plan_id が記録日から生成される標準形式と一致しません。", "参照に問題がなければ利用できますが、日付との対応を確認してください。")
        _check_number_range(audit, plan, "target_total_minutes", path, "日次計画値", plan_id, 0)
        _check_timestamps(audit, plan, path, "日次計画日時", plan_id)
        _check_list(audit, plan, "focus_subjects", path, "日次計画構造", plan_id)
        _check_list(audit, plan, "materials", path, "日次計画構造", plan_id)
        _check_list(audit, plan, "tasks", path, "日次計画構造", plan_id)
        review_notes = _check_list(audit, plan, "review_note_ids", path, "日次計画構造", plan_id)
        review_cards = _check_list(audit, plan, "review_card_ids", path, "日次計画構造", plan_id)
        if "notes" not in invalid:
            for note_id in review_notes:
                audit.check(not note_id or str(note_id) in note_ids, ERROR, "日次計画参照", path, plan_id, f"review_note_ids に存在しないノートIDがあります: {note_id}", "復習対象ノートを再選択してください。")
        if "cards" not in invalid:
            for card_id in review_cards:
                audit.check(not card_id or str(card_id) in card_ids, ERROR, "日次計画参照", path, plan_id, f"review_card_ids に存在しないカードIDがあります: {card_id}", "復習対象カードを再選択してください。")

    valid_statuses = {"not_started", "in_progress", "done", "pending"}
    for task in tasks:
        path = Path(task["_file"])
        task_id = str(task.get("task_id") or "")
        status = str(task.get("status") or "")
        if status == "paused":
            audit.flag(NOTICE, "タスク値", path, task_id, "status が旧形式 paused です。", "読込時は pending として扱われます。次回編集保存で統一できます。")
        else:
            audit.check(status in valid_statuses, ERROR, "タスク値", path, task_id, f"status が不正です: {status or '未設定'}", "タスク状態を画面から再設定してください。")
        _check_number_range(audit, task, "planned_minutes", path, "タスク値", task_id, 0)
        _check_list(audit, task, "linked_note_ids", path, "タスク構造", task_id, required=False)
        _check_list(audit, task, "linked_session_ids", path, "タスク構造", task_id, required=False)
        carried_from = str(task.get("carried_from_task_id") or "")
        if carried_from and "plans" not in invalid:
            audit.check(carried_from in task_ids, NOTICE, "タスク参照", path, task_id, f"carried_from_task_id の元タスクが見つかりません: {carried_from}", "元日の計画を削除済みならそのまま利用できます。履歴が必要な場合は確認してください。")
        else:
            audit.checks += 1


def _validate_diaries(audit: Audit, diaries: list[dict[str, Any]]) -> None:
    caffeine_records: list[dict[str, Any]] = []
    signatures: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for diary in diaries:
        path = Path(diary["_file"])
        diary_date = _check_record_date(audit, path, diary.get("date"), "日記")
        diary_id = diary_date.isoformat() if diary_date else ""
        _check_timestamps(audit, diary, path, "日記日時", diary_id)
        _check_number_range(audit, diary, "running_distance_km", path, "日記値", diary_id, 0)
        _check_number_range(audit, diary, "running_time_min", path, "日記値", diary_id, 0)
        good_seat_status = diary.get("good_seat_status")
        valid_good_seat_status = good_seat_status is None or (
            isinstance(good_seat_status, str) and good_seat_status in {"yes", "so_so", "no"}
        )
        audit.check(
            valid_good_seat_status,
            ERROR,
            "日記値",
            path,
            diary_id,
            "good_seat_status は yes / so_so / no / null のいずれかである必要があります。",
            "「今日の記録・計画」で Yes、So-so、No、未記録のいずれかを選び、保存し直してください。",
        )
        legacy_good_seat = diary.get("good_seat_obtained")
        audit.check(
            legacy_good_seat is None or type(legacy_good_seat) is bool,
            ERROR,
            "日記値",
            path,
            diary_id,
            "互換項目 good_seat_obtained は true / false / null のいずれかである必要があります。",
            "旧形式を手作業で編集している場合は true、false、null のいずれかへ修正してください。",
        )
        if (
            "good_seat_status" in diary
            and "good_seat_obtained" in diary
            and valid_good_seat_status
            and type(legacy_good_seat) is bool
        ):
            expected_legacy_value = {"yes": True, "no": False}.get(good_seat_status)
            audit.check(
                legacy_good_seat is expected_legacy_value,
                NOTICE,
                "日記値",
                path,
                diary_id,
                "good_seat_status と互換項目 good_seat_obtained の値が一致しません。",
                "good_seat_status を正本として扱います。次回の明示的な保存時に旧互換項目を整理できます。",
            )

        costs = _check_list(audit, diary, "cost_records", path, "日記構造", diary_id, required=False)
        for index, cost in enumerate(costs):
            if not audit.check(isinstance(cost, dict), ERROR, "自己投資費用", path, f"{diary_id}#{index + 1}", "cost_records に辞書形式でない項目があります。", "費用項目の形式を確認してください。"):
                continue
            assert isinstance(cost, dict)
            amount = _check_number_range(audit, cost, "amount", path, "自己投資費用", f"{diary_id}#{index + 1}", 0)
            if amount and not str(cost.get("name") or "").strip():
                audit.flag(NOTICE, "自己投資費用", path, f"{diary_id}#{index + 1}", "金額がある費用項目の name が空です。", "集計時に内容を識別できるよう名称を入力してください。")
            else:
                audit.checks += 1

        has_modern_caffeine = "caffeine_records" in diary or "caffeine_untimed_mg" in diary
        total = _check_number_range(audit, diary, "caffeine_mg", path, "日記カフェイン", diary_id, 0, required=has_modern_caffeine)
        if not has_modern_caffeine:
            audit.checks += 1
            continue
        untimed = _check_number_range(audit, diary, "caffeine_untimed_mg", path, "日記カフェイン", diary_id, 0)
        rows = _check_list(audit, diary, "caffeine_records", path, "日記カフェイン", diary_id)
        timed_total = 0.0
        for index, item in enumerate(rows):
            if not audit.check(isinstance(item, dict), ERROR, "日記カフェイン", path, f"{diary_id}#{index + 1}", "caffeine_records に辞書形式でない項目があります。", "不正な摂取記録を確認してください。"):
                continue
            assert isinstance(item, dict)
            stored = {**item, "_file": path, "_diary_date": diary_id}
            caffeine_records.append(stored)
            caffeine_id = str(item.get("caffeine_id") or "")
            audit.check(bool(caffeine_id), ERROR, "日記カフェインID", path, caffeine_id, "caffeine_id が欠損しています。", "バックアップ後に一意のIDを付与してください。")
            amount = _check_number_range(audit, item, "amount_mg", path, "日記カフェイン", caffeine_id, 0.000001)
            if amount is not None:
                timed_total += amount
            taken_at = _parse_datetime(item.get("taken_at"))
            audit.check(taken_at is not None, ERROR, "日記カフェイン", path, caffeine_id, "taken_at が有効な日時ではありません。", "摂取日時を確認してください。")
            if taken_at is not None and diary_date is not None:
                audit.check(taken_at.date() == diary_date, ERROR, "日記カフェイン", path, caffeine_id, "摂取日と日記ファイルの日付が一致しません。", "正しい摂取日の記録へ整理してください。")
            if item.get("created_at") or item.get("updated_at"):
                _check_timestamps(audit, item, path, "日記カフェイン日時", caffeine_id)
            else:
                audit.checks += 2
            signatures[tuple(_signature_value(item.get(key)) for key in ("taken_at", "amount_mg", "label", "memo"))].append(stored)
        if total is not None and untimed is not None:
            audit.check(abs(total - timed_total - untimed) <= 0.01, ERROR, "日記カフェイン", path, diary_id, "caffeine_mg が時刻付き記録と時刻不明分の合計に一致しません。", "日記またはカフェイン記録ページで内容を確認し、明示的に保存し直してください。")

    id_counts = Counter(str(item.get("caffeine_id") or "") for item in caffeine_records if item.get("caffeine_id"))
    for item in caffeine_records:
        caffeine_id = str(item.get("caffeine_id") or "")
        if caffeine_id:
            audit.check(id_counts[caffeine_id] == 1, ERROR, "日記カフェインID", Path(item["_file"]), caffeine_id, "caffeine_id が別記録と重複しています。", "バックアップ後に一意のIDへ修正してください。")
    for duplicates in signatures.values():
        audit.checks += 1
        if len(duplicates) > 1:
            first = duplicates[0]
            audit.issues.append(_issue(NOTICE, "二重保存候補", Path(first["_file"]), _record_id(first), f"時刻・量・内容が同一のカフェイン記録が {len(duplicates)} 件あります。", "意図した複数摂取か確認してください。"))


def _validate_substances(audit: Audit, workspace: dict[str, Any]) -> None:
    profile_path = SUBSTANCE_PROFILES_PATH
    profile_rows = workspace.get("substance_profiles", [])
    profile_ids: list[str] = []
    if isinstance(profile_rows, list):
        for index, item in enumerate(profile_rows):
            item_id = str(item.get("substance_id") or "") if isinstance(item, dict) else ""
            if not audit.check(
                isinstance(item, dict),
                ERROR,
                "成分設定構造",
                profile_path,
                f"profile#{index + 1}",
                "profiles に辞書形式でない項目があります。",
                "ユーザー設定の成分管理から保存し直してください。",
            ):
                continue
            assert isinstance(item, dict)
            audit.check(
                bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", item_id)),
                ERROR,
                "成分設定ID",
                profile_path,
                item_id,
                "substance_id が欠損しているか、使用できない形式です。",
                "成分IDは画面で生成された固定値を使用してください。",
            )
            if item_id:
                profile_ids.append(item_id)
            audit.check(
                bool(str(item.get("name") or "").strip()),
                ERROR,
                "成分設定値",
                profile_path,
                item_id,
                "成分の表示名が空です。",
                "ユーザー設定で表示名を入力してください。",
            )
            audit.check(
                str(item.get("unit") or "mg") == "mg",
                ERROR,
                "成分設定値",
                profile_path,
                item_id,
                "現在対応している単位は mg のみです。",
                "unit を mg に統一してください。",
            )
            if "default_label" in item:
                audit.check(
                    isinstance(item["default_label"], str),
                    ERROR,
                    "成分設定値",
                    profile_path,
                    item_id,
                    "製品・剤形の初期値が文字列ではありません。",
                    "ユーザー設定で製品・剤形の初期値を保存し直してください。",
                )
            _check_number_range(audit, item, "default_amount_mg", profile_path, "成分設定値", item_id, 0.001, 100000)
            _check_number_range(audit, item, "amount_step_mg", profile_path, "成分設定値", item_id, 0.001, 10000)
            _check_number_range(audit, item, "half_life_hours", profile_path, "成分設定値", item_id, 0.05, 240)
            peak_delay = _check_number_range(audit, item, "peak_delay_minutes", profile_path, "成分設定値", item_id, 0, 720)
            cutoff = _check_number_range(audit, item, "residual_cutoff_hours", profile_path, "成分設定値", item_id, 0.1, 720)
            if peak_delay is not None and cutoff is not None:
                audit.check(
                    cutoff > peak_delay / 60,
                    ERROR,
                    "成分設定値",
                    profile_path,
                    item_id,
                    "0 mg扱いまでの時間がピーク到達時間以下です。",
                    "打切時間をピーク到達時間より長く設定してください。",
                )
            else:
                audit.checks += 1
            color = str(item.get("color") or "")
            audit.check(
                bool(re.fullmatch(r"#[0-9A-Fa-f]{6}", color)),
                ERROR,
                "成分表示設定",
                profile_path,
                item_id,
                "color が #RRGGBB 形式ではありません。",
                "ユーザー設定のカラーピッカーから保存し直してください。",
            )
            for key in ("fill_enabled", "is_active"):
                audit.check(
                    isinstance(item.get(key), bool),
                    ERROR,
                    "成分表示設定",
                    profile_path,
                    item_id,
                    f"{key} が真偽値ではありません。",
                    "ユーザー設定から保存し直してください。",
                )
            _check_number_range(audit, item, "sort_order", profile_path, "成分表示設定", item_id, 0)
            _check_number_range(audit, item, "model_revision", profile_path, "成分設定値", item_id, 1)

    profile_counts = Counter(profile_ids)
    for item_id, count in profile_counts.items():
        audit.check(
            count == 1,
            ERROR,
            "成分設定ID",
            profile_path,
            item_id,
            f"substance_id が {count} 件重複しています。",
            "成分IDを一意にしてください。",
        )

    known_profile_ids = set(profile_ids) | {"caffeine", "eszopiclone", "trazodone", "melatonin"}
    stored_records: list[dict[str, Any]] = []
    signatures: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for diary in workspace.get("diaries", []):
        if not isinstance(diary, dict):
            continue
        path = Path(diary["_file"])
        diary_date = _parse_date(diary.get("date"))
        has_generic_schema = "substance_records_schema_version" in diary or "substance_records" in diary
        if not has_generic_schema:
            audit.checks += 1
            continue
        schema_version = _check_number_range(
            audit,
            diary,
            "substance_records_schema_version",
            path,
            "成分摂取構造",
            str(diary.get("date") or ""),
            1,
            1,
        )
        rows = _check_list(
            audit,
            diary,
            "substance_records",
            path,
            "成分摂取構造",
            str(diary.get("date") or ""),
        )
        caffeine_generic_total = 0.0
        caffeine_generic_ids: set[str] = set()
        caffeine_generic_by_id: dict[str, tuple[Any, ...]] = {}
        for index, item in enumerate(rows):
            row_label = f"{str(diary.get('date') or '')}#{index + 1}"
            if not audit.check(
                isinstance(item, dict),
                ERROR,
                "成分摂取構造",
                path,
                row_label,
                "substance_records に辞書形式でない項目があります。",
                "服薬ログページで履歴を確認してください。",
            ):
                continue
            assert isinstance(item, dict)
            record_id = str(item.get("substance_record_id") or "")
            substance_id = str(item.get("substance_id") or "")
            stored = {**item, "_file": path, "_diary_date": str(diary.get("date") or "")}
            stored_records.append(stored)
            audit.check(
                bool(record_id),
                ERROR,
                "成分摂取ID",
                path,
                record_id,
                "substance_record_id が欠損しています。",
                "服薬ログページで保存し直してください。",
            )
            audit.check(
                bool(substance_id),
                ERROR,
                "成分摂取値",
                path,
                record_id,
                "substance_id が欠損しています。",
                "成分を選択して保存し直してください。",
            )
            if substance_id:
                audit.check(
                    substance_id in known_profile_ids,
                    ERROR,
                    "成分設定参照",
                    path,
                    record_id,
                    f"設定に存在しない成分IDです: {substance_id}",
                    "成分設定を復元するか、正しい成分へ記録を整理してください。",
                )
            amount = _check_number_range(audit, item, "amount_mg", path, "成分摂取値", record_id, 0.000001, 100000)
            audit.check(
                str(item.get("unit") or "mg") == "mg",
                ERROR,
                "成分摂取値",
                path,
                record_id,
                "現在対応している摂取単位は mg のみです。",
                "unit を mg に統一してください。",
            )
            taken_at = _parse_datetime(item.get("taken_at"))
            audit.check(
                taken_at is not None,
                ERROR,
                "成分摂取日時",
                path,
                record_id,
                "taken_at が有効な日時ではありません。",
                "摂取日時を確認してください。",
            )
            if taken_at is not None and diary_date is not None:
                audit.check(
                    taken_at.date() == diary_date,
                    ERROR,
                    "成分摂取日時",
                    path,
                    record_id,
                    "摂取日と日記ファイルの日付が一致しません。",
                    "正しい摂取日の記録へ整理してください。",
                )
            snapshot = item.get("model_snapshot")
            if audit.check(
                isinstance(snapshot, dict),
                ERROR,
                "成分残存モデル",
                path,
                record_id,
                "model_snapshot が欠損しているか辞書形式ではありません。",
                "服薬ログページで履歴を保存し直してください。",
            ):
                assert isinstance(snapshot, dict)
                _check_number_range(audit, snapshot, "half_life_hours", path, "成分残存モデル", record_id, 0.05, 240)
                snapshot_peak = _check_number_range(audit, snapshot, "peak_delay_minutes", path, "成分残存モデル", record_id, 0, 720)
                snapshot_cutoff = _check_number_range(audit, snapshot, "residual_cutoff_hours", path, "成分残存モデル", record_id, 0.1, 720)
                if snapshot_peak is not None and snapshot_cutoff is not None:
                    audit.check(
                        snapshot_cutoff > snapshot_peak / 60,
                        ERROR,
                        "成分残存モデル",
                        path,
                        record_id,
                        "記録時モデルの打切時間がピーク到達時間以下です。",
                        "記録内容と成分設定を確認してください。",
                    )
                else:
                    audit.checks += 1
            if item.get("created_at") or item.get("updated_at"):
                _check_timestamps(audit, item, path, "成分摂取日時", record_id)
            else:
                audit.checks += 2
            if substance_id == "caffeine" and amount is not None:
                caffeine_generic_total += amount
                if record_id:
                    caffeine_generic_ids.add(record_id)
                    caffeine_generic_by_id[record_id] = tuple(
                        _signature_value(value)
                        for value in (
                            item.get("taken_at"),
                            item.get("amount_mg"),
                            item.get("label"),
                            item.get("memo"),
                        )
                    )
            signatures[
                tuple(
                    _signature_value(value)
                    for value in (
                        substance_id,
                        item.get("taken_at"),
                        item.get("amount_mg"),
                        item.get("label"),
                        item.get("memo"),
                    )
                )
            ].append(stored)

        if schema_version is not None:
            mirror_rows = diary.get("caffeine_records", [])
            if isinstance(mirror_rows, list):
                mirror_total = sum(
                    value
                    for value in (_number(item.get("amount_mg")) for item in mirror_rows if isinstance(item, dict))
                    if value is not None and value >= 0
                )
                mirror_ids = {
                    str(item.get("caffeine_id") or "")
                    for item in mirror_rows
                    if isinstance(item, dict) and item.get("caffeine_id")
                }
                mirror_by_id = {
                    str(item.get("caffeine_id")): tuple(
                        _signature_value(value)
                        for value in (
                            item.get("taken_at"),
                            item.get("amount_mg"),
                            item.get("label"),
                            item.get("memo"),
                        )
                    )
                    for item in mirror_rows
                    if isinstance(item, dict) and item.get("caffeine_id")
                }
                audit.check(
                    math.isclose(caffeine_generic_total, mirror_total, abs_tol=1e-6),
                    ERROR,
                    "カフェイン互換項目",
                    path,
                    str(diary.get("date") or ""),
                    "汎用成分記録と caffeine_records の合計が一致しません。",
                    "服薬ログページでカフェイン履歴を保存し直してください。",
                )
                audit.check(
                    caffeine_generic_ids == mirror_ids,
                    ERROR,
                    "カフェイン互換項目",
                    path,
                    str(diary.get("date") or ""),
                    "汎用成分記録と caffeine_records のIDが一致しません。",
                    "服薬ログページでカフェイン履歴を保存し直してください。",
                )
                audit.check(
                    caffeine_generic_by_id == mirror_by_id,
                    ERROR,
                    "カフェイン互換項目",
                    path,
                    str(diary.get("date") or ""),
                    "汎用成分記録と caffeine_records の日時・量・内容が一致しません。",
                    "服薬ログページでカフェイン履歴を保存し直してください。",
                )

    record_counts = Counter(
        str(item.get("substance_record_id") or "")
        for item in stored_records
        if item.get("substance_record_id")
    )
    for item in stored_records:
        record_id = str(item.get("substance_record_id") or "")
        if record_id:
            audit.check(
                record_counts[record_id] == 1,
                ERROR,
                "成分摂取ID",
                Path(item["_file"]),
                record_id,
                "substance_record_id が別記録と重複しています。",
                "服薬ログページで履歴を整理してください。",
            )
    for duplicates in signatures.values():
        audit.checks += 1
        if len(duplicates) > 1:
            first = duplicates[0]
            audit.issues.append(
                _issue(
                    NOTICE,
                    "二重保存候補",
                    Path(first["_file"]),
                    str(first.get("substance_record_id") or ""),
                    f"成分・時刻・量・内容が同一の摂取記録が {len(duplicates)} 件あります。",
                    "意図した複数摂取か確認してください。",
                )
            )


def _validate_health(audit: Audit, logs: list[dict[str, Any]]) -> None:
    for log in logs:
        path = Path(log["_file"])
        log_date = _check_record_date(audit, path, log.get("date"), "健康ログ")
        item_id = log_date.isoformat() if log_date else ""
        _check_timestamps(audit, log, path, "健康ログ日時", item_id)
        sleep = log.get("sleep")
        condition = log.get("condition")
        if not audit.check(isinstance(sleep, dict), ERROR, "健康ログ構造", path, item_id, "sleep が辞書形式ではありません。", "睡眠項目の保存形式を確認してください。"):
            sleep = {}
        if not audit.check(isinstance(condition, dict), ERROR, "健康ログ構造", path, item_id, "condition が辞書形式ではありません。", "体調項目の保存形式を確認してください。"):
            condition = {}
        assert isinstance(sleep, dict) and isinstance(condition, dict)
        for key in ("bed_time", "wake_time"):
            audit.check(_valid_time(sleep.get(key)), ERROR, "健康ログ値", path, item_id, f"{key} が有効な HH:MM ではありません。", "時刻を確認してください。")
        _check_number_range(audit, sleep, "sleep_duration_min", path, "健康ログ値", item_id, 0, 1440)
        _check_number_range(audit, sleep, "sleep_quality", path, "健康ログ値", item_id, 1, 5)
        _check_number_range(audit, sleep, "morning_sleepiness", path, "健康ログ値", item_id, 1, 5)
        _check_number_range(audit, sleep, "midnight_awakenings", path, "健康ログ値", item_id, 0)
        _check_number_range(audit, sleep, "nap_minutes", path, "健康ログ値", item_id, 0, 1440)
        for key in ("physical_score", "fatigue", "stress", "focus_score", "mood_score"):
            _check_number_range(audit, condition, key, path, "健康ログ値", item_id, 1, 5)


def _validate_exercise(audit: Audit, workspace: dict[str, Any]) -> None:
    records = workspace["exercise_records"]
    diaries_by_date = {str(item.get("date") or "")[:10]: item for item in workspace["diaries"]}
    for log in workspace["exercise_logs"]:
        path = Path(log["_file"])
        log_date = _check_record_date(audit, path, log.get("date"), "運動ログ")
        item_id = log_date.isoformat() if log_date else ""
        _check_timestamps(audit, log, path, "運動ログ日時", item_id)
        _check_list(audit, log, "records", path, "運動ログ構造", item_id)

    id_counts = Counter(str(item.get("exercise_id") or "") for item in records if item.get("exercise_id"))
    signatures: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    sources_by_day: dict[tuple[str, str], int] = Counter()
    valid_types = {"running", "walking", "strength", "stretch", "other"}
    for record in records:
        path = Path(record["_file"])
        exercise_id = str(record.get("exercise_id") or "")
        audit.check(bool(exercise_id), ERROR, "運動ログID", path, exercise_id, "exercise_id が欠損しています。", "バックアップ後に一意のIDを付与してください。")
        if exercise_id:
            audit.check(id_counts[exercise_id] == 1, ERROR, "運動ログID", path, exercise_id, "exercise_id が別記録と重複しています。", "バックアップ後に一意のIDへ修正してください。")
        exercise_type = str(record.get("type") or "")
        audit.check(exercise_type in valid_types, NOTICE, "運動ログ値", path, exercise_id, f"type が現在の選択肢と異なります: {exercise_type or '未設定'}", "旧形式なら利用できます。集計分類が必要な場合は再選択してください。")
        if record.get("start_time"):
            audit.check(_valid_time(record.get("start_time")), ERROR, "運動ログ値", path, exercise_id, "start_time が有効な HH:MM ではありません。", "開始時刻を確認してください。")
        else:
            audit.checks += 1
        duration = _check_number_range(audit, record, "duration_min", path, "運動ログ値", exercise_id, 0)
        distance = _check_number_range(audit, record, "distance_km", path, "運動ログ値", exercise_id, 0)
        pace = _check_number_range(audit, record, "pace_min_per_km", path, "運動ログ値", exercise_id, 0)
        _check_number_range(audit, record, "intensity", path, "運動ログ値", exercise_id, 1, 10)
        _check_number_range(audit, record, "fatigue_after", path, "運動ログ値", exercise_id, 1, 5)
        _check_timestamps(audit, record, path, "運動ログ日時", exercise_id)
        if distance and duration and pace is not None:
            expected = round(duration / distance, 2)
            audit.check(abs(pace - expected) <= 0.11, NOTICE, "運動ログ値", path, exercise_id, "pace_min_per_km が duration_min / distance_km と一致しません。", "手入力したペースならそのまま利用できます。自動計算を意図した場合は確認してください。")
        else:
            audit.checks += 1

        source = str(record.get("source") or "")
        log_date = str(record.get("_log_date") or "")[:10]
        if source:
            sources_by_day[(log_date, source)] += 1
        if source == "diary":
            diary = diaries_by_date.get(log_date)
            if diary is None:
                audit.flag(NOTICE, "運動・日記同期", path, exercise_id, "source=diary ですが対応する日記がありません。", "日記を削除済みなら運動ログはそのまま保持できます。")
            else:
                diary_distance = _number(diary.get("running_distance_km"))
                diary_duration = _number(diary.get("running_time_min"))
                audit.check(
                    diary_distance is not None and diary_duration is not None and distance is not None and duration is not None and abs(diary_distance - distance) <= 0.001 and abs(diary_duration - duration) <= 0.01,
                    NOTICE,
                    "運動・日記同期",
                    path,
                    exercise_id,
                    "日記由来の運動記録と日記のランニング値が一致しません。",
                    "どちらを正本として残すか確認し、必要なら日記から再同期してください。",
                )
        else:
            audit.checks += 1
        signatures[tuple(
            _signature_value(value)
            for value in (log_date, exercise_type, record.get("start_time"), record.get("duration_min"), record.get("distance_km"), record.get("title"), source)
        )].append(record)

    for (log_date, source), count in sources_by_day.items():
        if source in {"diary", "daily_hub"}:
            path = next(Path(item["_file"]) for item in records if str(item.get("_log_date") or "")[:10] == log_date and str(item.get("source") or "") == source)
            audit.check(count == 1, NOTICE, "運動ログ同期", path, log_date, f"同じ日に source={source} の記録が {count} 件あります。", "同期記録は通常1件です。二重保存でないか確認してください。")
    for duplicates in signatures.values():
        audit.checks += 1
        if len(duplicates) > 1:
            first = duplicates[0]
            audit.issues.append(_issue(NOTICE, "二重保存候補", Path(first["_file"]), _record_id(first), f"内容が同一の運動記録が {len(duplicates)} 件あります。", "意図した別記録か確認してください。"))


def _statistics_integer(
    audit: Audit,
    record: dict[str, Any],
    key: str,
    path: Path,
    item_id: str,
    minimum: int,
) -> int | None:
    number = _number(record.get(key))
    valid = number is not None and number.is_integer() and number >= minimum
    audit.check(
        valid,
        ERROR,
        "統計演習履歴得点",
        path,
        item_id,
        f"{key} が {minimum} 以上の整数ではありません。",
        "保存済みの採点結果を確認してください。",
    )
    return int(number) if valid and number is not None else None


def _statistics_period(audit: Audit, value: Any, path: Path, item_id: str) -> tuple[date, date] | None:
    if not audit.check(
        isinstance(value, dict),
        ERROR,
        "統計演習期間",
        path,
        item_id,
        "period が辞書形式ではありません。",
        "問題作成時の対象期間を確認してください。",
    ):
        return None
    assert isinstance(value, dict)
    start = _parse_date(value.get("start_date"))
    end = _parse_date(value.get("end_date"))
    audit.check(start is not None, ERROR, "統計演習期間", path, item_id, "period.start_date が有効な日付ではありません。", "YYYY-MM-DD 形式の日付を確認してください。")
    audit.check(end is not None, ERROR, "統計演習期間", path, item_id, "period.end_date が有効な日付ではありません。", "YYYY-MM-DD 形式の日付を確認してください。")
    if start is None or end is None:
        return None
    audit.check(start <= end, ERROR, "統計演習期間", path, item_id, "period.start_date が period.end_date より後です。", "問題作成時の対象期間を確認してください。")
    return start, end


def _validate_statistics_practice(audit: Audit, workspace: dict[str, Any]) -> None:
    for day in workspace["statistics_practice_days"]:
        path = Path(day["_file"])
        item_id = str(day.get("date") or "")
        schema_version = day.get("schema_version")
        audit.check(
            isinstance(schema_version, int) and not isinstance(schema_version, bool) and schema_version == 1,
            ERROR,
            "統計演習履歴構造",
            path,
            item_id,
            "schema_version が 1 ではありません。",
            "統計演習履歴の保存形式を確認してください。",
        )
        _check_record_date(audit, path, day.get("date"), "統計演習履歴日付", item_id)
        audit.check(
            isinstance(day.get("attempts"), list),
            ERROR,
            "統計演習履歴構造",
            path,
            item_id,
            "attempts がリスト形式ではありません。",
            "日別履歴の attempts を配列形式で保存してください。",
        )

    attempts = workspace["statistics_attempts"]
    id_counts = Counter(
        str(item.get("attempt_id") or "").strip()
        for item in attempts
        if str(item.get("attempt_id") or "").strip()
    )
    for attempt in attempts:
        path = Path(attempt["_file"])
        raw_attempt_id = attempt.get("attempt_id")
        attempt_id = str(raw_attempt_id or "").strip()
        valid_attempt_id = isinstance(raw_attempt_id, str) and bool(attempt_id)
        audit.check(valid_attempt_id, ERROR, "統計演習履歴ID", path, attempt_id, "attempt_id が欠損しているか、文字列ではありません。", "バックアップ後に一意の attempt_id を設定してください。")
        if attempt_id:
            audit.check(id_counts[attempt_id] == 1, ERROR, "統計演習履歴ID", path, attempt_id, "attempt_id が別の演習履歴と重複しています。", "バックアップ後に一意の attempt_id へ修正してください。")

        attempted_at = _parse_datetime(attempt.get("attempted_at"))
        audit.check(attempted_at is not None, ERROR, "統計演習履歴日時", path, attempt_id, "attempted_at が欠損しているか、有効な日時ではありません。", "ISO形式の解答日時を確認してください。")
        attempt_date = _check_record_date(audit, path, attempt.get("date"), "統計演習履歴日時", attempt_id)
        if attempted_at is not None and attempt_date is not None:
            audit.check(attempted_at.date() == attempt_date, ERROR, "統計演習履歴日時", path, attempt_id, "attempted_at の日付と date が一致しません。", "解答日時と日別履歴の保存日を確認してください。")
        parent_date = _parse_date(attempt.get("_practice_date"))
        if attempt_date is not None and parent_date is not None:
            audit.check(attempt_date == parent_date, ERROR, "統計演習履歴日時", path, attempt_id, "解答の date と日別履歴の date が一致しません。", "正しい日付の統計演習履歴へ整理してください。")

        for key in ("problem_id", "template_id", "topic", "title"):
            value = attempt.get(key)
            audit.check(isinstance(value, str) and bool(value.strip()), ERROR, "統計演習履歴構造", path, attempt_id, f"{key} が欠損しているか、空の文字列です。", "保存済みの問題情報を確認してください。")
        _statistics_period(audit, attempt.get("period"), path, attempt_id)
        for key in ("answers", "grade", "problem_snapshot"):
            audit.check(isinstance(attempt.get(key), dict), ERROR, "統計演習履歴構造", path, attempt_id, f"{key} が辞書形式ではありません。", "保存済みの解答と問題スナップショットを確認してください。")

        score = _check_number_range(audit, attempt, "score", path, "統計演習履歴得点", attempt_id, 0)
        max_score = _check_number_range(audit, attempt, "max_score", path, "統計演習履歴得点", attempt_id, 0)
        if max_score is not None:
            audit.check(max_score > 0, ERROR, "統計演習履歴得点", path, attempt_id, "max_score が 0 以下です。", "max_score を正の値で保存してください。")
        if score is not None and max_score is not None:
            audit.check(score <= max_score, ERROR, "統計演習履歴得点", path, attempt_id, "score が max_score を超えています。", "保存済みの採点結果を確認してください。")
        correct_count = _statistics_integer(audit, attempt, "correct_count", path, attempt_id, 0)
        total_parts = _statistics_integer(audit, attempt, "total_parts", path, attempt_id, 1)
        if correct_count is not None and total_parts is not None:
            audit.check(correct_count <= total_parts, ERROR, "統計演習履歴得点", path, attempt_id, "correct_count が total_parts を超えています。", "保存済みの採点結果を確認してください。")

        grade = attempt.get("grade")
        if isinstance(grade, dict):
            grade_score = _check_number_range(audit, grade, "score", path, "統計演習履歴得点", attempt_id, 0, 100)
            earned_points = _check_number_range(audit, grade, "earned_points", path, "統計演習履歴得点", attempt_id, 0)
            max_points = _check_number_range(audit, grade, "max_points", path, "統計演習履歴得点", attempt_id, 0)
            if max_points is not None:
                audit.check(max_points > 0, ERROR, "統計演習履歴得点", path, attempt_id, "grade.max_points が 0 以下です。", "保存済みの配点を確認してください。")
            if earned_points is not None and max_points is not None:
                audit.check(earned_points <= max_points, ERROR, "統計演習履歴得点", path, attempt_id, "grade.earned_points が grade.max_points を超えています。", "保存済みの配点を確認してください。")
            grade_correct = _statistics_integer(audit, grade, "correct_count", path, attempt_id, 0)
            grade_total = _statistics_integer(audit, grade, "total_parts", path, attempt_id, 1)
            if grade_correct is not None and grade_total is not None:
                audit.check(grade_correct <= grade_total, ERROR, "統計演習履歴得点", path, attempt_id, "grade.correct_count が grade.total_parts を超えています。", "保存済みの採点結果を確認してください。")
            if score is not None and grade_score is not None:
                audit.check(abs(score - grade_score) <= 1e-9, ERROR, "統計演習履歴得点", path, attempt_id, "score と grade.score が一致しません。", "保存済みの採点結果を確認してください。")
            if correct_count is not None and grade_correct is not None:
                audit.check(correct_count == grade_correct, ERROR, "統計演習履歴得点", path, attempt_id, "correct_count と grade.correct_count が一致しません。", "保存済みの採点結果を確認してください。")
            if total_parts is not None and grade_total is not None:
                audit.check(total_parts == grade_total, ERROR, "統計演習履歴得点", path, attempt_id, "total_parts と grade.total_parts が一致しません。", "保存済みの採点結果を確認してください。")

        snapshot = attempt.get("problem_snapshot")
        if isinstance(snapshot, dict):
            for key in ("problem_id", "template_id", "topic"):
                audit.check(snapshot.get(key) == attempt.get(key), ERROR, "統計演習スナップショット", path, attempt_id, f"problem_snapshot.{key} と履歴の {key} が一致しません。", "保存済みの問題スナップショットを確認してください。")
            audit.check(snapshot.get("period") == attempt.get("period"), ERROR, "統計演習スナップショット", path, attempt_id, "problem_snapshot.period と履歴の period が一致しません。", "保存済みの問題スナップショットを確認してください。")


def _validate_master(audit: Audit, workspace: dict[str, Any], invalid: set[str]) -> None:
    path = MASTER_DATA_DIR / "master.json"
    master = workspace["master"]
    if not isinstance(master, dict):
        return
    collections = (("projects", "project_id", ()), ("subjects", "subject_id", ("project_id",)), ("materials", "material_id", ("project_id", "subject_id")), ("units", "unit_id", ("project_id", "subject_id", "material_id")), ("tags", "tag_id", ()))
    for key, _, _ in collections:
        value = master.get(key)
        if not audit.check(isinstance(value, list), ERROR, "マスタ構造", path, key, f"{key} がリスト形式ではありません。", "マスタJSONのコレクション形式を確認してください。"):
            invalid.add("master")
            master[key] = []

    lookups: dict[str, dict[str, dict[str, Any]]] = {}
    for key, id_key, parents in collections:
        rows = [item for item in master.get(key, []) if isinstance(item, dict)]
        audit.check(len(rows) == len(master.get(key, [])), ERROR, "マスタ構造", path, key, f"{key} に辞書形式でない項目があります。", "不正なマスタ項目を確認してください。")
        lookups[key] = {str(item.get(id_key)): item for item in rows if item.get(id_key)}
        active_names: dict[tuple[tuple[str, ...], str], list[dict[str, Any]]] = defaultdict(list)
        for item in rows:
            item_id = str(item.get(id_key) or "")
            audit.check(bool(str(item.get("name") or "").strip()), ERROR, "マスタ値", path, item_id, "name が空です。", "マスタ管理画面で名称を入力してください。")
            audit.check(isinstance(item.get("is_active"), bool), ERROR if "is_active" in item else NOTICE, "マスタ値", path, item_id, "is_active が真偽値ではないか欠損しています。", "有効・無効状態をマスタ管理画面で保存し直してください。")
            _check_timestamps(audit, item, path, "マスタ日時", item_id)
            for parent_key in parents:
                audit.check(bool(item.get(parent_key)), ERROR, "マスタ階層", path, item_id, f"{parent_key} が欠損しています。", "所属先をマスタ管理画面で設定してください。")
            if item.get("is_active") is True:
                parent_token = tuple(str(item.get(parent) or "") for parent in parents)
                active_names[(parent_token, str(item.get("name") or "").strip().casefold())].append(item)
        for (_, normalized_name), duplicates in active_names.items():
            audit.checks += 1
            if normalized_name and len(duplicates) > 1:
                audit.issues.append(_issue(NOTICE, "マスタ重複名", path, _record_id(duplicates[0]), f"同じ所属先に同名の有効な {key} が {len(duplicates)} 件あります。", "選択肢が判別しにくくなるため、統合または名称変更を検討してください。"))

    projects = lookups.get("projects", {})
    subjects = lookups.get("subjects", {})
    materials = lookups.get("materials", {})
    units = lookups.get("units", {})
    for subject_id, subject in subjects.items():
        project_id = str(subject.get("project_id") or "")
        audit.check(not project_id or project_id in projects, ERROR, "マスタ階層", path, subject_id, f"科目の project_id が存在しません: {project_id}", "所属プロジェクトを確認してください。")
    for material_id, material in materials.items():
        project_id = str(material.get("project_id") or "")
        subject_id = str(material.get("subject_id") or "")
        subject = subjects.get(subject_id)
        audit.check(not project_id or project_id in projects, ERROR, "マスタ階層", path, material_id, f"教材の project_id が存在しません: {project_id}", "所属プロジェクトを確認してください。")
        audit.check(not subject_id or subject is not None, ERROR, "マスタ階層", path, material_id, f"教材の subject_id が存在しません: {subject_id}", "所属科目を確認してください。")
        if subject is not None and project_id:
            audit.check(str(subject.get("project_id") or "") == project_id, ERROR, "マスタ階層", path, material_id, "教材の project_id と所属科目の project_id が一致しません。", "教材の所属階層を再設定してください。")
    for unit_id, unit in units.items():
        project_id = str(unit.get("project_id") or "")
        subject_id = str(unit.get("subject_id") or "")
        material_id = str(unit.get("material_id") or "")
        subject = subjects.get(subject_id)
        material = materials.get(material_id)
        audit.check(not project_id or project_id in projects, ERROR, "マスタ階層", path, unit_id, f"単元の project_id が存在しません: {project_id}", "所属プロジェクトを確認してください。")
        audit.check(not subject_id or subject is not None, ERROR, "マスタ階層", path, unit_id, f"単元の subject_id が存在しません: {subject_id}", "所属科目を確認してください。")
        audit.check(not material_id or material is not None, ERROR, "マスタ階層", path, unit_id, f"単元の material_id が存在しません: {material_id}", "所属教材を確認してください。")
        if subject is not None and project_id:
            audit.check(str(subject.get("project_id") or "") == project_id, ERROR, "マスタ階層", path, unit_id, "単元の project_id と所属科目の project_id が一致しません。", "単元の所属階層を再設定してください。")
        if material is not None:
            audit.check(str(material.get("project_id") or "") == project_id and str(material.get("subject_id") or "") == subject_id, ERROR, "マスタ階層", path, unit_id, "単元と所属教材のプロジェクト・科目が一致しません。", "単元の所属教材を再設定してください。")
        parent_id = str(unit.get("parent_unit_id") or "")
        if parent_id:
            parent = units.get(parent_id)
            audit.check(parent_id != unit_id and parent is not None, ERROR, "マスタ階層", path, unit_id, f"parent_unit_id が自己参照または存在しません: {parent_id}", "親単元を確認してください。")
            if parent is not None:
                audit.check(all(str(parent.get(key) or "") == str(unit.get(key) or "") for key in ("project_id", "subject_id", "material_id")), ERROR, "マスタ階層", path, unit_id, "親単元と子単元の所属階層が一致しません。", "同じ教材内の親単元を指定してください。")
        else:
            audit.checks += 1


def _collect_notes(audit: Audit, workspace: dict[str, Any], invalid: set[str]) -> None:
    path = NOTES_DATA_DIR / "notebooks.json"
    store = workspace.get("notes")
    if not isinstance(store, dict):
        workspace["notes"] = []
        return
    notebooks = store.get("notebooks")
    if not audit.check(isinstance(notebooks, list), ERROR, "ノート構造", path, "", "notebooks がリスト形式ではありません。", "ノートJSONの階層を確認してください。"):
        invalid.add("notes")
        workspace["notes"] = []
        return
    notebook_ids: list[str] = []
    section_ids: list[str] = []
    pages: list[dict[str, Any]] = []
    valid_notebooks: list[dict[str, Any]] = []
    valid_sections: list[dict[str, Any]] = []
    for notebook_index, notebook in enumerate(notebooks):
        if not audit.check(isinstance(notebook, dict), ERROR, "ノート構造", path, str(notebook_index), "notebook が辞書形式ではありません。", "不正なノートブック項目を確認してください。"):
            invalid.add("notes")
            continue
        assert isinstance(notebook, dict)
        valid_notebooks.append(notebook)
        notebook_id = str(notebook.get("id") or "")
        notebook_ids.append(notebook_id)
        audit.check(bool(notebook_id), ERROR, "ノート階層ID", path, notebook_id, "notebook id が欠損しています。", "バックアップ後に一意のIDを付与してください。")
        sections = notebook.get("sections")
        if not audit.check(isinstance(sections, list), ERROR, "ノート構造", path, notebook_id, "sections がリスト形式ではありません。", "ノートブック階層を確認してください。"):
            invalid.add("notes")
            continue
        assert isinstance(sections, list)
        for section_index, section in enumerate(sections):
            if not audit.check(isinstance(section, dict), ERROR, "ノート構造", path, f"{notebook_id}#{section_index + 1}", "section が辞書形式ではありません。", "不正なセクション項目を確認してください。"):
                invalid.add("notes")
                continue
            assert isinstance(section, dict)
            valid_sections.append(section)
            section_id = str(section.get("id") or "")
            section_ids.append(section_id)
            audit.check(bool(section_id), ERROR, "ノート階層ID", path, section_id, "section id が欠損しています。", "バックアップ後に一意のIDを付与してください。")
            raw_pages = section.get("pages")
            if not audit.check(isinstance(raw_pages, list), ERROR, "ノート構造", path, section_id, "pages がリスト形式ではありません。", "セクション階層を確認してください。"):
                invalid.add("notes")
                continue
            assert isinstance(raw_pages, list)
            for page_index, page in enumerate(raw_pages):
                if not audit.check(isinstance(page, dict), ERROR, "ノート構造", path, f"{section_id}#{page_index + 1}", "page が辞書形式ではありません。", "不正なページ項目を確認してください。"):
                    invalid.add("notes")
                    continue
                assert isinstance(page, dict)
                pages.append({**page, "_file": path})
    notebook_counts = Counter(item for item in notebook_ids if item)
    section_counts = Counter(item for item in section_ids if item)
    for notebook in valid_notebooks:
        notebook_id = str(notebook.get("id") or "")
        if notebook_id:
            audit.check(notebook_counts[notebook_id] == 1, ERROR, "ノート階層ID", path, notebook_id, "notebook id が重複しています。", "ノートブックIDを一意にしてください。")
    for section in valid_sections:
        section_id = str(section.get("id") or "")
        if section_id:
            audit.check(section_counts[section_id] == 1, ERROR, "ノート階層ID", path, section_id, "section id が重複しています。", "セクションIDを一意にしてください。")
    workspace["notebooks"] = valid_notebooks
    workspace["sections"] = valid_sections
    workspace["notes"] = pages


def _validate_notes_cards_reminders(audit: Audit, workspace: dict[str, Any]) -> None:
    for note in workspace["notes"]:
        path = Path(note["_file"])
        note_id = str(note.get("note_id") or "")
        alias_id = str(note.get("id") or "")
        audit.check(not alias_id or not note_id or alias_id == note_id, NOTICE, "ノート互換ID", path, note_id, "id と note_id が一致しません。", "表示は note_id を優先します。バックアップ後に同じIDへ統一してください。")
        if "body" in note and "content" in note:
            audit.check(str(note.get("body") or "") == str(note.get("content") or ""), NOTICE, "ノート互換本文", path, note_id, "body と content の内容が一致しません。", "現在の画面で正しい本文を確認し、明示的に保存してください。")
        else:
            audit.checks += 1
        _check_timestamps(audit, note, path, "ノート日時", note_id)
        for key in ("tags", "linked_study_session_ids", "linked_flashcard_ids", "linked_reminder_ids"):
            _check_list(audit, note, key, path, "ノート構造", note_id, required=False)

    card_path = FLASHCARDS_PATH
    for index, card in enumerate(workspace["cards"]):
        if not audit.check(isinstance(card, dict), ERROR, "カード構造", card_path, str(index), "カード配列に辞書形式でない項目があります。", "不正なカード項目を確認してください。"):
            continue
        assert isinstance(card, dict)
        card_id = str(card.get("card_id") or card.get("flashcard_id") or "")
        if card.get("card_id") and card.get("flashcard_id"):
            audit.check(str(card["card_id"]) == str(card["flashcard_id"]), ERROR, "カード互換ID", card_path, card_id, "card_id と flashcard_id が一致しません。", "両方を同じ一意IDへ統一してください。")
        else:
            audit.checks += 1
        if "question" in card and "front" in card:
            audit.check(str(card.get("question") or "") == str(card.get("front") or ""), NOTICE, "カード互換本文", card_path, card_id, "question と front が一致しません。", "正しい問題文を確認して保存し直してください。")
        else:
            audit.checks += 1
        if "answer" in card and "back" in card:
            audit.check(str(card.get("answer") or "") == str(card.get("back") or ""), NOTICE, "カード互換本文", card_path, card_id, "answer と back が一致しません。", "正しい解答を確認して保存し直してください。")
        else:
            audit.checks += 1
        audit.check(str(card.get("importance") or "normal") in {"low", "normal", "high"}, NOTICE, "カード値", card_path, card_id, "importance が現在の選択肢と異なります。", "重要度フィルタを利用する場合は再選択してください。")
        for key in ("tags", "linked_note_ids"):
            _check_list(audit, card, key, card_path, "カード構造", card_id, required=False)
        _check_timestamps(audit, card, card_path, "カード日時", card_id)
        if "need_review" in card and "important" in card:
            audit.check(bool(card["need_review"]) == bool(card["important"]), NOTICE, "カード互換値", card_path, card_id, "need_review と important が一致しません。", "要復習状態を画面で保存し直してください。")
        else:
            audit.checks += 1

    reminder_path = REMINDERS_PATH
    schedule_ids: list[str] = []
    for index, reminder in enumerate(workspace["reminders"]):
        if not audit.check(isinstance(reminder, dict), ERROR, "リマインダー構造", reminder_path, str(index), "リマインダー配列に辞書形式でない項目があります。", "不正なリマインダー項目を確認してください。"):
            continue
        assert isinstance(reminder, dict)
        reminder_id = str(reminder.get("reminder_id") or "")
        mode = str(reminder.get("schedule_mode") or "single")
        audit.check(mode in {"single", "light", "standard", "intensive", "custom", "sm2"}, ERROR, "復習スケジュール", reminder_path, reminder_id, f"schedule_mode が不正です: {mode}", "有効な復習方式を再設定してください。")
        audit.check(str(reminder.get("status") or "pending") in {"pending", "done"}, ERROR, "リマインダー値", reminder_path, reminder_id, "status が pending / done ではありません。", "リマインダー状態を確認してください。")
        audit.check(str(reminder.get("priority") or "normal") in {"low", "normal", "high"}, NOTICE, "リマインダー値", reminder_path, reminder_id, "priority が現在の選択肢と異なります。", "優先度フィルタを利用する場合は再選択してください。")
        schedule_status = str(reminder.get("schedule_status") or "active")
        audit.check(schedule_status in {"active", "paused", "cancelled", "completed"}, ERROR, "復習スケジュール", reminder_path, reminder_id, f"schedule_status が不正です: {schedule_status}", "系列状態を確認してください。")
        scheduled = _parse_date(reminder.get("scheduled_date"))
        audit.check(scheduled is not None, ERROR, "リマインダー日時", reminder_path, reminder_id, "scheduled_date が欠損しているか不正です。", "復習予定日を確認してください。")
        if reminder.get("next_review_date"):
            audit.check(_parse_date(reminder.get("next_review_date")) is not None, ERROR, "リマインダー日時", reminder_path, reminder_id, "next_review_date が不正です。", "次回復習日を確認してください。")
        else:
            audit.checks += 1
        _check_timestamps(audit, reminder, reminder_path, "リマインダー日時", reminder_id)
        _check_list(audit, reminder, "linked_note_ids", reminder_path, "リマインダー構造", reminder_id, required=False)
        _check_list(audit, reminder, "linked_card_ids", reminder_path, "リマインダー構造", reminder_id, required=False)
        schedule_id = str(reminder.get("schedule_id") or "")
        if mode != "single":
            audit.check(bool(schedule_id), ERROR, "復習スケジュール", reminder_path, reminder_id, "間隔反復予定に schedule_id がありません。", "バックアップ後に系列IDを付与してください。")
            if schedule_id:
                schedule_ids.append(schedule_id)
        else:
            audit.checks += 1
        if mode in {"light", "standard", "intensive", "custom"}:
            intervals = reminder.get("intervals_days")
            valid_intervals = isinstance(intervals, list) and bool(intervals)
            normalized: list[int] = []
            if isinstance(intervals, list):
                for value in intervals:
                    number = _number(value)
                    if number is None or not number.is_integer() or not 1 <= number <= 3650:
                        valid_intervals = False
                        break
                    normalized.append(int(number))
                if normalized != sorted(set(normalized)):
                    valid_intervals = False
            audit.check(valid_intervals, ERROR, "復習スケジュール", reminder_path, reminder_id, "intervals_days が昇順・重複なしの1〜3650日の整数配列ではありません。", "復習間隔を画面から再設定してください。")
            step = _number(reminder.get("current_step"))
            audit.check(step is not None and step.is_integer() and step >= 0 and (not normalized or step <= len(normalized)), ERROR, "復習スケジュール", reminder_path, reminder_id, "current_step が不正です。", "復習系列の進捗を確認してください。")
        else:
            audit.checks += 2
        if mode == "sm2" or reminder.get("algorithm") == "sm2":
            _check_number_range(audit, reminder, "ease_factor", reminder_path, "復習スケジュール", reminder_id, 1.3)
            _check_number_range(audit, reminder, "interval_days", reminder_path, "復習スケジュール", reminder_id, 0)
            _check_number_range(audit, reminder, "repetition", reminder_path, "復習スケジュール", reminder_id, 0)
        else:
            audit.checks += 3
        history = _check_list(audit, reminder, "review_history", reminder_path, "復習履歴", reminder_id, required=False)
        for history_index, row in enumerate(history):
            history_id = f"{reminder_id}#{history_index + 1}"
            if not audit.check(isinstance(row, dict), ERROR, "復習履歴", reminder_path, history_id, "review_history に辞書形式でない項目があります。", "不正な復習履歴を確認してください。"):
                continue
            assert isinstance(row, dict)
            audit.check(_parse_date(row.get("reviewed_on")) is not None, ERROR, "復習履歴", reminder_path, history_id, "reviewed_on が不正です。", "復習実施日を確認してください。")
            audit.check(str(row.get("result") or "") in {"forgot", "difficult", "normal", "easy", "skipped"}, NOTICE, "復習履歴", reminder_path, history_id, "result が現在の選択肢と異なります。", "復習結果の集計に含める場合は値を確認してください。")
            _check_number_range(audit, row, "duration_min", reminder_path, "復習履歴", history_id, 0, required=False)
            if row.get("recorded_at"):
                audit.check(_parse_datetime(row.get("recorded_at")) is not None, ERROR, "復習履歴", reminder_path, history_id, "recorded_at が不正です。", "記録日時を確認してください。")
            else:
                audit.checks += 1
    schedule_counts = Counter(schedule_ids)
    for reminder in workspace["reminders"]:
        if not isinstance(reminder, dict):
            continue
        schedule_id = str(reminder.get("schedule_id") or "")
        if schedule_id:
            audit.check(schedule_counts[schedule_id] == 1, ERROR, "復習スケジュールID", reminder_path, str(reminder.get("reminder_id") or ""), "schedule_id が別リマインダーと重複しています。", "系列IDを一意にしてください。")


def _validate_countdown_and_navigation(audit: Audit, settings: dict[str, Any]) -> None:
    """Check optional display preferences without rewriting legacy settings."""
    path = USER_SETTINGS_PATH
    recommendation = "ユーザー設定画面でカウントダウンを確認し、保存し直してください。"
    if "countdown" in settings:
        countdown = settings["countdown"]
        if audit.check(isinstance(countdown, dict), NOTICE, "ユーザー設定構造", path, "countdown", "countdown が辞書形式ではありません。カウントダウンは無効として扱われます。", recommendation):
            if "enabled" in countdown:
                audit.check(isinstance(countdown["enabled"], bool), NOTICE, "ユーザー設定値", path, "countdown", "enabled が真偽値ではありません。カウントダウンの有効状態を確認してください。", recommendation)
            if "title" in countdown:
                audit.check(isinstance(countdown["title"], str), NOTICE, "ユーザー設定値", path, "countdown", "title が文字列ではありません。目標名を確認してください。", recommendation)
            target = countdown.get("target_date")
            if target is not None or countdown.get("enabled") is True:
                valid_date = (
                    isinstance(target, str)
                    and re.fullmatch(r"\d{4}-\d{2}-\d{2}", target) is not None
                    and _parse_date(target) is not None
                )
                audit.check(valid_date, NOTICE, "ユーザー設定値", path, "countdown", "target_date が有効な YYYY-MM-DD ではありません。有効時は目標日が必要です。", recommendation)

    display = settings.get("display")
    if not isinstance(display, dict) or "hidden_sidebar_pages" not in display:
        return
    hidden = display["hidden_sidebar_pages"]
    recommendation = "ユーザー設定画面でサイドバーの表示ページを確認し、保存し直してください。"
    if not audit.check(isinstance(hidden, list), NOTICE, "ユーザー設定構造", path, "display", "hidden_sidebar_pages がリスト形式ではありません。この非表示設定は適用されません。", recommendation):
        return
    known_paths = {item["path"] for group in NAV_GROUPS for item in group["items"]}
    seen: set[str] = set()
    for index, page_path in enumerate(hidden):
        item_id = f"display.hidden_sidebar_pages[{index}]"
        if not audit.check(isinstance(page_path, str), NOTICE, "ユーザー設定値", path, item_id, "非表示ページの指定が文字列ではありません。この項目は無視されます。", recommendation):
            continue
        audit.check(page_path in known_paths, NOTICE, "ユーザー設定値", path, item_id, f"サイドバーに登録されていないページです: {page_path}", recommendation)
        audit.check(page_path != SETTINGS_PAGE_PATH, NOTICE, "ユーザー設定値", path, item_id, "ユーザー設定ページは常に表示されるため、この非表示指定は適用されません。", recommendation)
        audit.check(page_path not in seen, NOTICE, "ユーザー設定値", path, item_id, "同じ非表示ページが重複しています。表示結果には影響しません。", recommendation)
        seen.add(page_path)


def _validate_learning_context(audit: Audit, workspace: dict[str, Any], invalid: set[str]) -> None:
    """Inspect optional local context without normalizing, fetching, or saving it."""
    data = workspace.get("learning_context")
    if data is None:
        return
    from app_core.learning_context_service import (
        MAX_LINKS, MAX_NEXT_STEP_LENGTH, MAX_TITLE_LENGTH, is_valid_google_resource_url,
    )

    path = SETTINGS_DIR / "learning_context.json"
    recommendation = "教材リンク・次はここからの設定を確認してください。検査による自動修正は行いません。"
    category = "学習コンテキスト構造"
    if not audit.check(type(data.get("schema_version")) is int and data.get("schema_version") == 1,
                       ERROR, category, path, "schema_version", "schema_version が整数 1 ではありません。", recommendation):
        invalid.add("learning_context")
        return
    entries = data.get("materials")
    if not audit.check(isinstance(entries, dict), ERROR, category, path, "materials",
                       "materials が教材IDをキーとする辞書形式ではありません。", recommendation):
        invalid.add("learning_context")
        return
    master = workspace.get("master")
    materials = master.get("materials", []) if isinstance(master, dict) else []
    material_ids = {item["material_id"] for item in materials
                    if isinstance(item, dict) and isinstance(item.get("material_id"), str)} if isinstance(materials, list) else set()

    def valid_identifier(value: Any) -> bool:
        return (isinstance(value, str) and bool(value.strip()) and len(value) <= 200
                and not any(ord(character) < 32 or ord(character) == 127 for character in value))

    for key, entry in entries.items():
        item_id = str(key)
        valid_key = valid_identifier(key)
        audit.check(valid_key, ERROR, "学習コンテキストID", path, item_id,
                    "教材IDのキーが空白のみ、200文字超過、または制御文字を含んでいます。", recommendation)
        if not audit.check(isinstance(entry, dict), ERROR, category, path, item_id,
                           "教材の学習コンテキストが辞書形式ではありません。", recommendation):
            continue
        audit.check(isinstance(entry.get("material_id"), str) and entry["material_id"] == key,
                    ERROR, "学習コンテキストID", path, item_id,
                    "material_id が materials のキーと一致しません。", recommendation)
        if "master" not in invalid and valid_key:
            audit.check(key in material_ids, NOTICE, "学習コンテキスト参照", path, item_id,
                        "参照する教材IDがマスタにありません。削除済み教材の補足情報の可能性があります。",
                        "教材IDと残すべき補足情報を確認してください。内容は自動削除されません。")
        audit.check(isinstance(entry.get("next_step"), str) and len(entry["next_step"]) <= MAX_NEXT_STEP_LENGTH,
                    ERROR, category, path, item_id, f"next_step が {MAX_NEXT_STEP_LENGTH} 文字以内の文字列ではありません。", recommendation)
        timestamps = {}
        for field in ("created_at", "updated_at"):
            value = entry.get(field)
            parsed = None
            if isinstance(value, str) and "T" in value:
                try:
                    # Unlike legacy timestamps, this new schema does not trim
                    # malformed whitespace before validating the ISO value.
                    datetime.fromisoformat(value)
                    parsed = _parse_datetime(value)
                except ValueError:
                    pass
            timestamps[field] = parsed
            audit.check(parsed is not None, ERROR, "学習コンテキスト日時", path, item_id,
                        f"{field} が T 区切りの有効な ISO 日時ではありません。", recommendation)
        if all(value is not None for value in timestamps.values()):
            audit.check(timestamps["updated_at"] >= timestamps["created_at"], ERROR, "学習コンテキスト日時", path, item_id,
                        "updated_at が created_at より前です。", recommendation)
        links = entry.get("links")
        if not audit.check(isinstance(links, list), ERROR, category, path, item_id,
                           "links が配列形式ではありません。", recommendation):
            continue
        audit.check(len(links) <= MAX_LINKS, ERROR, category, path, item_id,
                    f"links が上限 {MAX_LINKS} 件を超えています。", recommendation)
        seen: set[str] = set()
        for index, link in enumerate(links):
            link_item = f"{item_id}/links[{index}]"
            if not audit.check(isinstance(link, dict), ERROR, category, path, link_item,
                               "リンクが辞書形式ではありません。", recommendation):
                continue
            link_id = link.get("link_id")
            valid_id = valid_identifier(link_id)
            audit.check(valid_id, ERROR, "学習コンテキストID", path, link_item,
                        "link_id が文字列以外、空白のみ、200文字超過、または制御文字を含んでいます。", recommendation)
            if valid_id:
                audit.check(link_id not in seen, ERROR, "学習コンテキストID", path, link_item,
                            "同じ教材内で link_id が重複しています。", recommendation)
                seen.add(link_id)
            audit.check(isinstance(link.get("title"), str) and len(link["title"]) <= MAX_TITLE_LENGTH,
                        ERROR, category, path, link_item,
                        f"リンクの title が {MAX_TITLE_LENGTH} 文字以内の文字列ではありません。", recommendation)
            audit.check(is_valid_google_resource_url(link.get("url")), ERROR, "学習コンテキストURL", path, link_item,
                        "リンクが許可された Google 教材ファイルの HTTPS URL ではありません。", recommendation)


def _validate_consultation_proposals(audit: Audit, workspace: dict[str, Any], invalid: set[str]) -> None:
    """Validate manual suggestions and plan memos without executing their text."""
    data = workspace.get("consultation_inbox")
    plans = workspace.get("plans", [])
    if data is None and not any("consultation_memos" in plan for plan in plans):
        return
    from app_core.consultation_proposal_contract import (
        MAX_TEXT_LENGTH, MAX_TITLE_LENGTH, ProposalError, validate_inbox,
    )

    path = SETTINGS_DIR / "consultation_inbox.json"
    recommendation = "相談提案箱の保存形式を確認してください。検査で提案の実行・適用・自動修正は行いません。"
    proposals = {}
    if data is not None:
        try:
            proposals = validate_inbox(data)["proposals"]
            audit.checks += 1
        except ProposalError as exc:
            audit.flag(ERROR, "相談提案形式", path, "", str(exc), recommendation)
            invalid.add("consultation_inbox")
    master = workspace.get("master")
    materials = master.get("materials", []) if isinstance(master, dict) else []
    material_ids = {item["material_id"] for item in materials
                    if isinstance(item, dict) and isinstance(item.get("material_id"), str)} if isinstance(materials, list) else set()
    for proposal_id, entry in proposals.items():
        referenced_materials = {entry["proposal"]["target"]["material_id"]}
        application = entry.get("application", {})
        if application.get("kind") == "next_step":
            referenced_materials.add(application["material_id"])
        for material_id in referenced_materials - {""}:
            if "master" not in invalid:
                audit.check(material_id in material_ids, NOTICE, "相談提案参照", path, proposal_id,
                            "提案または反映履歴の教材IDがマスタにありません。未登録・削除済みの教材の可能性があります。",
                            "提案と反映先を確認してください。提案や既存のメモは自動削除されません。")

    for plan in plans:
        if "consultation_memos" not in plan:
            continue
        plan_path = Path(plan["_file"])
        plan_id = str(plan.get("plan_id") or "")
        memos = plan["consultation_memos"]
        memo_recommendation = "日次計画の相談メモと元の提案を確認してください。内容は自動削除されません。"
        if not audit.check(isinstance(memos, list), ERROR, "相談メモ構造", plan_path, plan_id,
                           "consultation_memos が配列形式ではありません。", memo_recommendation):
            continue
        seen: set[str] = set()
        for index, memo in enumerate(memos):
            item_id = f"{plan_id}/consultation_memos[{index}]"
            if not audit.check(isinstance(memo, dict), ERROR, "相談メモ構造", plan_path, item_id,
                               "相談メモが辞書形式ではありません。", memo_recommendation):
                continue
            proposal_id = memo.get("proposal_id")
            valid_id = isinstance(proposal_id, str) and re.fullmatch(r"proposal_[0-9a-f]{64}", proposal_id) is not None
            audit.check(valid_id, ERROR, "相談メモID", plan_path, item_id,
                        "proposal_id が proposal_ と64桁の小文字16進数の形式ではありません。", memo_recommendation)
            if valid_id:
                audit.check(proposal_id not in seen, ERROR, "相談メモID", plan_path, proposal_id,
                            "同じ日次計画で相談メモの proposal_id が重複しています。", memo_recommendation)
                seen.add(proposal_id)
                if "consultation_inbox" not in invalid:
                    audit.check(proposal_id in proposals, NOTICE, "相談メモ参照", plan_path, proposal_id,
                                "相談メモの元の提案IDが提案箱にありません。", memo_recommendation)
                    entry = proposals.get(proposal_id)
                    if entry is not None:
                        application = entry.get("application", {})
                        audit.check(entry["status"] == "applied" and application.get("kind") == "daily_memo"
                                    and application.get("target_date") == plan.get("date"),
                                    NOTICE, "相談メモ参照", plan_path, proposal_id,
                                    "相談メモの保存日と提案の反映状態・反映先が一致しません。", memo_recommendation)
            for field, limit in (("title", MAX_TITLE_LENGTH), ("text", MAX_TEXT_LENGTH)):
                value = memo.get(field)
                valid_text = (isinstance(value, str) and bool(value.strip()) and len(value) <= limit
                              and not any(ord(char) < 32 and char not in "\n\r\t" or ord(char) == 127 for char in value))
                if valid_text:
                    try:
                        value.encode("utf-8")
                    except UnicodeError:
                        valid_text = False
                audit.check(valid_text,
                            ERROR, "相談メモ構造", plan_path, item_id,
                            f"{field} が空でない {limit} 文字以内の有効な文字列ではありません。", memo_recommendation)
            adopted_at = memo.get("adopted_at")
            parsed = None
            if isinstance(adopted_at, str) and "T" in adopted_at:
                try:
                    datetime.fromisoformat(adopted_at)
                    parsed = _parse_datetime(adopted_at)
                except ValueError:
                    pass
            audit.check(parsed is not None, ERROR, "相談メモ日時", plan_path, item_id,
                        "adopted_at が T 区切りの有効な ISO 日時ではありません。", memo_recommendation)


def _validate_settings(audit: Audit, workspace: dict[str, Any]) -> None:
    settings = workspace.get("settings")
    if isinstance(settings, dict) and settings:
        _validate_countdown_and_navigation(audit, settings)
        path = USER_SETTINGS_PATH
        sections: dict[str, dict[str, Any]] = {}
        for key in ("timer", "health", "exercise", "diary", "display"):
            value = settings.get(key)
            if value is None:
                audit.checks += 1
                sections[key] = {}
            elif audit.check(isinstance(value, dict), ERROR, "ユーザー設定構造", path, key, f"{key} が辞書形式ではありません。", "設定画面が開けなくなる可能性があります。バックアップ後に設定を初期化または修正してください。"):
                sections[key] = value
            else:
                sections[key] = {}
        timer = sections["timer"]
        _check_number_range(audit, timer, "hourly_rate", path, "ユーザー設定値", "timer", 0, required=False)
        _check_number_range(audit, timer, "goal_minutes", path, "ユーザー設定値", "timer", 1, required=False)
        _check_number_range(audit, timer, "overtime_multiplier", path, "ユーザー設定値", "timer", 1, required=False)
        _check_number_range(audit, timer, "overtime_threshold_hours", path, "ユーザー設定値", "timer", 0.5, required=False)
        _check_number_range(audit, timer, "interleaving_minutes", path, "ユーザー設定値", "timer", 5, required=False)
        for key in ("overtime_enabled", "interleaving_enabled"):
            if key in timer:
                audit.check(isinstance(timer[key], bool), ERROR, "ユーザー設定値", path, "timer", f"{key} が真偽値ではありません。", "設定画面で保存し直してください。")
            else:
                audit.checks += 1

        health = sections["health"]
        for key in ("bed_time", "wake_time"):
            if key in health:
                audit.check(_valid_time(health[key]), ERROR, "ユーザー設定値", path, "health", f"{key} が有効な HH:MM ではありません。", "設定画面で時刻を保存し直してください。")
            else:
                audit.checks += 1
        for key in ("sleep_quality", "morning_sleepiness", "physical_score", "fatigue", "stress", "focus_score", "mood_score"):
            _check_number_range(audit, health, key, path, "ユーザー設定値", "health", 1, 5, required=False)
        _check_number_range(audit, health, "midnight_awakenings", path, "ユーザー設定値", "health", 0, required=False)
        _check_number_range(audit, health, "nap_minutes", path, "ユーザー設定値", "health", 0, required=False)

        exercise = sections["exercise"]
        if "type" in exercise:
            audit.check(str(exercise["type"]) in {"running", "walking", "strength", "stretch", "other"}, NOTICE, "ユーザー設定値", path, "exercise", "運動 type が現在の選択肢と異なります。", "初期運動種目を設定画面で再選択してください。")
        else:
            audit.checks += 1
        if "start_time" in exercise:
            audit.check(_valid_time(exercise["start_time"]), ERROR, "ユーザー設定値", path, "exercise", "start_time が有効な HH:MM ではありません。", "設定画面で時刻を保存し直してください。")
        else:
            audit.checks += 1
        _check_number_range(audit, exercise, "duration_min", path, "ユーザー設定値", "exercise", 0, required=False)
        _check_number_range(audit, exercise, "distance_km", path, "ユーザー設定値", "exercise", 0, required=False)
        _check_number_range(audit, exercise, "intensity", path, "ユーザー設定値", "exercise", 1, 10, required=False)
        _check_number_range(audit, exercise, "fatigue_after", path, "ユーザー設定値", "exercise", 1, 5, required=False)

        diary = sections["diary"]
        _check_number_range(audit, diary, "investment_row_count", path, "ユーザー設定値", "diary", 1, 20, required=False)
        _check_number_range(audit, diary, "caffeine_mg", path, "ユーザー設定値", "diary", 0, required=False)

        display = sections["display"]
        if "period_label" in display:
            audit.check(str(display["period_label"]) in {"直近7日", "直近14日", "直近30日", "今月"}, NOTICE, "ユーザー設定値", path, "display", "period_label が現在の選択肢と異なります。", "表示期間の初期値を設定画面で再選択してください。")
        else:
            audit.checks += 1
        if "theme_mode" in display:
            audit.check(str(display["theme_mode"]) in {"standard", "dark"}, ERROR, "ユーザー設定値", path, "display", "theme_mode が standard / dark ではありません。", "共通テーマを切り替えて保存し直してください。")
        else:
            audit.checks += 1
        if "show_task_metrics" in display:
            audit.check(isinstance(display["show_task_metrics"], bool), ERROR, "ユーザー設定値", path, "display", "show_task_metrics が真偽値ではありません。", "ユーザー設定画面でタスク関連指標の表示設定を保存し直してください。")
        else:
            audit.checks += 1

    demo = workspace.get("demo_mode")
    if isinstance(demo, dict) and demo:
        path = SETTINGS_DIR / "demo_mode.json"
        if "enabled" in demo:
            audit.check(isinstance(demo["enabled"], bool), ERROR, "デモ設定", path, "", "enabled が真偽値ではありません。", "デモ設定生成スクリプトを確認してください。")
        else:
            audit.checks += 1
        if demo.get("enabled"):
            audit.check(_parse_date(demo.get("fixed_today")) is not None, ERROR, "デモ設定", path, "", "デモ有効時の fixed_today が不正です。", "YYYY-MM-DD形式の固定日を設定してください。")
        else:
            audit.checks += 1


def _validate_record_hierarchies(audit: Audit, workspace: dict[str, Any], invalid: set[str]) -> None:
    if "master" in invalid or not isinstance(workspace.get("master"), dict):
        return
    master = workspace["master"]
    subjects = {str(item.get("subject_id")): item for item in master.get("subjects", []) if isinstance(item, dict) and item.get("subject_id")}
    materials = {str(item.get("material_id")): item for item in master.get("materials", []) if isinstance(item, dict) and item.get("material_id")}
    units = {str(item.get("unit_id")): item for item in master.get("units", []) if isinstance(item, dict) and item.get("unit_id")}
    record_groups = (workspace["sessions"], workspace["tasks"], workspace["notes"], [item for item in workspace["cards"] if isinstance(item, dict)], [item for item in workspace["reminders"] if isinstance(item, dict)])
    for records in record_groups:
        for record in records:
            path = Path(record.get("_file") or (FLASHCARDS_PATH if "card_id" in record or "flashcard_id" in record else REMINDERS_PATH))
            item_id = _record_id(record)
            project_id = str(record.get("project_id") or "")
            subject_id = str(record.get("subject_id") or "")
            material_id = str(record.get("material_id") or "")
            unit_id = str(record.get("unit_id") or "")
            subject = subjects.get(subject_id)
            material = materials.get(material_id)
            unit = units.get(unit_id)
            if subject is not None and project_id:
                audit.check(str(subject.get("project_id") or "") == project_id, ERROR, "分類階層", path, item_id, "project_id と subject_id の所属関係が一致しません。", "分類を画面から再選択してください。")
            else:
                audit.checks += 1
            if material is not None:
                expected = (str(material.get("project_id") or ""), str(material.get("subject_id") or ""))
                audit.check((not project_id or expected[0] == project_id) and (not subject_id or expected[1] == subject_id), ERROR, "分類階層", path, item_id, "教材の所属プロジェクト・科目と記録の分類が一致しません。", "教材を画面から再選択してください。")
            else:
                audit.checks += 1
            if unit is not None:
                expected = (str(unit.get("project_id") or ""), str(unit.get("subject_id") or ""), str(unit.get("material_id") or ""))
                audit.check((not project_id or expected[0] == project_id) and (not subject_id or expected[1] == subject_id) and (not material_id or expected[2] == material_id), ERROR, "分類階層", path, item_id, "単元の所属階層と記録の分類が一致しません。", "単元を画面から再選択してください。")
            else:
                audit.checks += 1


def _validate_reciprocal_links(audit: Audit, workspace: dict[str, Any], invalid: set[str]) -> None:
    if {"sessions", "plans"}.isdisjoint(invalid):
        sessions = {str(item.get("session_id")): item for item in workspace["sessions"] if item.get("session_id")}
        tasks = {str(item.get("task_id")): item for item in workspace["tasks"] if item.get("task_id")}
        for session_id, session in sessions.items():
            task_id = str(session.get("linked_task_id") or "")
            if task_id in tasks:
                audit.check(session_id in _as_list(tasks[task_id].get("linked_session_ids")), NOTICE, "双方向リンク", Path(session["_file"]), session_id, "勉強ログからタスクへリンクしていますが、タスク側にセッションIDがありません。", "リンク表示に差が出るため、タスクまたはログの関連付けを保存し直してください。")
            else:
                audit.checks += 1
        for task_id, task in tasks.items():
            for session_id in _as_list(task.get("linked_session_ids")):
                if str(session_id) in sessions:
                    audit.check(str(sessions[str(session_id)].get("linked_task_id") or "") == task_id, NOTICE, "双方向リンク", Path(task["_file"]), task_id, "タスクからセッションへリンクしていますが、勉強ログ側の linked_task_id が一致しません。", "関連付けを保存し直してください。")

    if {"sessions", "notes"}.isdisjoint(invalid):
        sessions = {str(item.get("session_id")): item for item in workspace["sessions"] if item.get("session_id")}
        notes = {str(item.get("note_id")): item for item in workspace["notes"] if item.get("note_id")}
        for session_id, session in sessions.items():
            for note_id in _as_list(session.get("linked_note_ids")):
                if str(note_id) in notes:
                    audit.check(session_id in _as_list(notes[str(note_id)].get("linked_study_session_ids")), NOTICE, "双方向リンク", Path(session["_file"]), session_id, "勉強ログからノートへリンクしていますが、ノート側にセッションIDがありません。", "関連付けを保存し直してください。")
        for note_id, note in notes.items():
            for session_id in _as_list(note.get("linked_study_session_ids")):
                if str(session_id) in sessions:
                    audit.check(note_id in _as_list(sessions[str(session_id)].get("linked_note_ids")), NOTICE, "双方向リンク", Path(note["_file"]), note_id, "ノートから勉強ログへリンクしていますが、ログ側にノートIDがありません。", "関連付けを保存し直してください。")


def _validate_roadmap_links(audit: Audit, workspace: dict[str, Any], invalid: set[str]) -> None:
    data = workspace.get("roadmap")
    if "roadmap" in invalid or "plans" in invalid:
        return
    path = SETTINGS_DIR / "study_roadmap.json"
    allocations = {row["id"]: row for row in data["allocations"]} if data else {}
    for task in workspace["tasks"]:
        if task.get("roadmap_allocation_id") or task.get("roadmap_item_id"):
            reference = task.get("roadmap_allocation_id")
            allocation = allocations.get(reference) if isinstance(reference, str) else None
            audit.check(allocation is not None and allocation["item_id"] == task.get("roadmap_item_id"), ERROR, "学習ロードマップ", Path(task["_file"]), task.get("task_id", ""), "日別タスクの工程・割当参照が見つからないか一致しません。", "ロードマップ設定と関連付けを確認してください。")
    if not data:
        return
    from app_core.roadmap_planning import dependency_violations
    for problem in dependency_violations(data):
        audit.flag(NOTICE, "学習ロードマップ", path, problem["item_id"],
                   f"工程「{problem['title']}」が前提工程「{problem['parent_title']}」の終了前に開始しています。",
                   "日程の調整で前提関係に合わせたプレビューを確認してください。")
    for allocation in data["allocations"]:
        if not allocation["is_active"]:
            continue
        matches = [task for task in workspace["tasks"] if task.get("roadmap_allocation_id") == allocation["id"]
                   or (task.get("_plan_date"), task.get("task_id")) == (allocation["task_date"], allocation["task_id"])]
        audit.check(bool(matches), NOTICE, "学習ロードマップ", path, allocation["id"], "関連付けた日別タスクが見つかりません。", "削除済みの場合はロードマップで関連付けを解除してください。")
        for task in matches:
            audit.check(task.get("roadmap_item_id") == allocation["item_id"], ERROR, "学習ロードマップ", Path(task["_file"]), task.get("task_id", ""), "工程とタスクの参照が一致しません。", "関連付けを確認してください。")


def run_deep_integrity_checks() -> dict[str, Any]:
    audit = Audit()
    workspace, invalid = _load_and_check_json(audit)
    _collect_notes(audit, workspace, invalid)
    _validate_study_sessions(audit, workspace["sessions"])
    _validate_plans(audit, workspace, invalid)
    _validate_diaries(audit, workspace["diaries"])
    _validate_substances(audit, workspace)
    _validate_health(audit, workspace["health"])
    _validate_exercise(audit, workspace)
    _validate_statistics_practice(audit, workspace)
    _validate_master(audit, workspace, invalid)
    _validate_notes_cards_reminders(audit, workspace)
    _validate_settings(audit, workspace)
    _validate_learning_context(audit, workspace, invalid)
    _validate_consultation_proposals(audit, workspace, invalid)
    _validate_record_hierarchies(audit, workspace, invalid)
    _validate_reciprocal_links(audit, workspace, invalid)
    _validate_roadmap_links(audit, workspace, invalid)
    return {
        "issues": audit.issues,
        "total_checks": audit.checks,
        "checked_file_count": len(_all_json_files()),
        "invalid_datasets": sorted(invalid),
        "check_scope": CHECK_SCOPE,
    }
