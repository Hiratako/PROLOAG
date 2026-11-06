from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app_core.json_store import JsonReadError, load_json_safe, record_json_files
from app_core.substance_service import default_substance_profiles
from app_core.study_record_reader import unpack_study_document
from config import PROJECT_ROOT


@dataclass(frozen=True)
class CsvExport:
    key: str
    label: str
    description: str
    filename: str
    row_count: int
    column_count: int
    columns: tuple[str, ...]
    data: bytes


@dataclass(frozen=True)
class ExportBundle:
    generated_at: datetime
    tables: tuple[CsvExport, ...]
    manifest: bytes
    zip_data: bytes
    warnings: tuple[str, ...]

    @property
    def total_rows(self) -> int:
        return sum(table.row_count for table in self.tables)


RecordRef = tuple[str, int, dict[str, Any]]
MASTER_KEYS = ("project", "subject", "material", "unit")
BASE_COLUMNS = ("__source_file", "__record_index")


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _read_json(path: Path, root: Path, warnings: list[str]) -> Any:
    if not path.exists():
        return None
    try:
        return load_json_safe(path, None)
    except JsonReadError as exc:
        warnings.append(f"{_relative_path(path, root)} を読み込めませんでした（{type(exc).__name__}）。")
        return None


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def flatten_record(record: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten dictionaries while retaining lists as lossless JSON cells."""
    flattened: dict[str, Any] = {}
    for raw_key, value in record.items():
        key = f"{prefix}.{raw_key}" if prefix else str(raw_key)
        if isinstance(value, dict):
            if value:
                flattened.update(flatten_record(value, key))
            else:
                flattened[key] = "{}"
        elif isinstance(value, (list, tuple)):
            flattened[key] = _json_text(list(value))
        elif value is None:
            flattened[key] = ""
        elif isinstance(value, (date, datetime)):
            flattened[key] = value.isoformat()
        else:
            flattened[key] = value
    return flattened


def _record_refs(path: Path, root: Path, warnings: list[str]) -> list[RecordRef]:
    data = _read_json(path, root, warnings)
    source_file = _relative_path(path, root)
    if data is None:
        return []
    if isinstance(data, list):
        rows: list[RecordRef] = []
        for index, item in enumerate(data):
            if isinstance(item, dict):
                rows.append((source_file, index, item))
            else:
                rows.append((source_file, index, {"value": item}))
        return rows
    if isinstance(data, dict):
        return [(source_file, 0, data)]
    return [(source_file, 0, {"value": data})]


def _directory_refs(root: Path, directory: str, warnings: list[str]) -> list[RecordRef]:
    base = root / directory
    if not base.exists():
        return []
    refs: list[RecordRef] = []
    for path in sorted(record_json_files(base)):
        refs.extend(_record_refs(path, root, warnings))
    return refs


def _study_rows(
    root: Path, maps: dict[str, dict[str, str]], warnings: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One row per actual session, plus a source-linked metadata side table."""
    rows: list[dict[str, Any]] = []
    containers: list[dict[str, Any]] = []
    for path in sorted(record_json_files(root / "study_logs")):
        raw = _read_json(path, root, warnings)
        if raw is None:
            continue
        source = _relative_path(path, root)
        document = unpack_study_document(raw)
        for index, record in document.records:
            row = _flat_row(source, index, record, maps)
            # A legacy record can itself contain the export bookkeeping keys.
            # Keep the real source association authoritative and retain those
            # original values in a JSON cell instead of silently losing them.
            collisions = {key: record[key] for key in BASE_COLUMNS if key in record}
            if collisions:
                key = "__original_export_fields_json"
                while key in row:
                    key += "_"
                row[key] = _json_text(collisions)
            row.update(__source_file=source, __record_index=index)
            rows.append(row)
        containers.append({
            "__source_file": source,
            "container_kind": document.container_kind,
            "session_count": len(document.records),
            # JSON cells preserve dotted keys, nulls, empty objects, and arrays
            # without conflating envelope fields with session fields.
            "metadata_json": _json_text(document.metadata),
            "ignored_items_json": _json_text(document.ignored_items),
        })
        if document.ignored_items or document.container_kind == "unsupported":
            warnings.append(f"{source} の非セッション値はセッション件数に含めず、study_session_containers.csv に保持しました。")
    return rows, containers


def _master_maps(master: dict[str, Any]) -> dict[str, dict[str, str]]:
    maps: dict[str, dict[str, str]] = {}
    for key in MASTER_KEYS:
        collection = master.get(f"{key}s", [])
        maps[key] = {
            str(item.get(f"{key}_id")): str(item.get("name") or "")
            for item in collection
            if isinstance(item, dict) and item.get(f"{key}_id")
        }
    return maps


def _add_master_names(row: dict[str, Any], maps: dict[str, dict[str, str]]) -> None:
    for key in MASTER_KEYS:
        item_id = str(row.get(f"{key}_id") or "")
        if item_id and f"{key}_name" not in row:
            row[f"{key}_name"] = maps[key].get(item_id, "")


def _flat_row(
    source_file: str,
    index: int,
    record: dict[str, Any],
    maps: dict[str, dict[str, str]],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {"__source_file": source_file, "__record_index": index}
    if extra:
        row.update(extra)
    row.update(flatten_record(record))
    _add_master_names(row, maps)
    return row


def _rows_from_refs(refs: list[RecordRef], maps: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    return [_flat_row(source, index, record, maps) for source, index, record in refs]


def _columns_for(rows: list[dict[str, Any]], hints: tuple[str, ...]) -> tuple[str, ...]:
    columns: list[str] = []
    for key in (*BASE_COLUMNS, *hints):
        if key not in columns:
            columns.append(key)
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    return tuple(columns)


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (dict, list, tuple)):
        return _json_text(value)
    return value


def _csv_bytes(rows: list[dict[str, Any]], columns: tuple[str, ...]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _csv_value(row.get(key, "")) for key in columns})
    return buffer.getvalue().encode("utf-8-sig")


def _make_table(
    key: str,
    label: str,
    description: str,
    rows: list[dict[str, Any]],
    hints: tuple[str, ...] = (),
) -> CsvExport:
    columns = _columns_for(rows, hints)
    return CsvExport(
        key=key,
        label=label,
        description=description,
        filename=f"{key}.csv",
        row_count=len(rows),
        column_count=len(columns),
        columns=columns,
        data=_csv_bytes(rows, columns),
    )


def _child_rows(
    refs: list[RecordRef],
    child_key: str,
    maps: dict[str, dict[str, str]],
    parent_fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source, parent_index, parent in refs:
        children = parent.get(child_key, [])
        if not isinstance(children, list):
            continue
        parent_values = {
            f"parent_{field}": parent.get(field, "")
            for field in parent_fields
        }
        parent_values["__parent_record_index"] = parent_index
        for index, child in enumerate(children):
            record = child if isinstance(child, dict) else {"value": child}
            rows.append(_flat_row(source, index, record, maps, parent_values))
    return rows


def _note_rows(
    root: Path,
    maps: dict[str, dict[str, str]],
    warnings: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    path = root / "notes_data" / "notebooks.json"
    store = _read_json(path, root, warnings)
    if not isinstance(store, dict):
        return [], [], []
    source = _relative_path(path, root)
    notebook_rows: list[dict[str, Any]] = []
    section_rows: list[dict[str, Any]] = []
    page_rows: list[dict[str, Any]] = []
    notebooks = store.get("notebooks", [])
    if not isinstance(notebooks, list):
        return [], [], []
    for notebook_index, notebook in enumerate(notebooks):
        if not isinstance(notebook, dict):
            continue
        notebook_rows.append(_flat_row(source, notebook_index, notebook, maps))
        sections = notebook.get("sections", [])
        if not isinstance(sections, list):
            continue
        notebook_meta = {
            "parent_notebook_id": notebook.get("id", ""),
            "parent_notebook_name": notebook.get("name", ""),
            "__parent_record_index": notebook_index,
        }
        for section_index, section in enumerate(sections):
            if not isinstance(section, dict):
                continue
            section_rows.append(_flat_row(source, section_index, section, maps, notebook_meta))
            pages = section.get("pages", [])
            if not isinstance(pages, list):
                continue
            page_meta = {
                **notebook_meta,
                "parent_section_id": section.get("id", ""),
                "parent_section_name": section.get("name", ""),
                "__parent_section_index": section_index,
            }
            for page_index, page in enumerate(pages):
                if isinstance(page, dict):
                    page_rows.append(_flat_row(source, page_index, page, maps, page_meta))
    return notebook_rows, section_rows, page_rows


def _master_rows(
    master: dict[str, Any],
    collection: str,
    maps: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(master.get(collection, [])):
        if isinstance(item, dict):
            rows.append(_flat_row("master_data/master.json", index, item, maps))
    return rows


def _settings_rows(root: Path, maps: dict[str, dict[str, str]], warnings: list[str], *, cached_refs: dict[Path, list[RecordRef]] | None = None) -> list[dict[str, Any]]:
    settings_dir = root / "settings"
    rows: list[dict[str, Any]] = []
    if not settings_dir.exists():
        return rows
    allowed_files = {"user_settings.json", "demo_mode.json", "substance_profiles.json", "learning_context.json",
                     "consultation_inbox.json", "study_categories.json", "study_roadmap.json",
                     "roadmap_imports.json"}
    for path in sorted(item for item in settings_dir.glob("*.json") if item.name in allowed_files):
        refs = cached_refs[path] if cached_refs is not None and path in cached_refs else _record_refs(path, root, warnings)
        for source, index, record in refs:
            rows.append(_flat_row(source, index, record, maps, {"settings_file": path.name}))
    return rows


def _substance_profile_rows(
    root: Path,
    maps: dict[str, dict[str, str]],
    warnings: list[str],
) -> list[dict[str, Any]]:
    path = root / "settings" / "substance_profiles.json"
    raw = _read_json(path, root, warnings)
    if not path.exists():
        profiles = default_substance_profiles()
    elif isinstance(raw, dict):
        profiles = raw.get("profiles", [])
    elif isinstance(raw, list):
        profiles = raw
    else:
        profiles = []
    if not isinstance(profiles, list):
        return []
    source = _relative_path(path, root)
    return [
        _flat_row(source, index, item, maps)
        for index, item in enumerate(profiles)
        if isinstance(item, dict)
    ]


def _substance_record_rows(
    refs: list[RecordRef],
    maps: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source, parent_index, parent in refs:
        raw_generic = parent.get("substance_records", [])
        generic = raw_generic if isinstance(raw_generic, list) else []
        try:
            has_generic_schema = int(parent.get("substance_records_schema_version", 0) or 0) >= 1
        except (TypeError, ValueError):
            has_generic_schema = False
        normalized: list[dict[str, Any]] = [item for item in generic if isinstance(item, dict)]
        if not has_generic_schema:
            seen_ids = {
                str(item.get("substance_record_id") or item.get("intake_id") or "")
                for item in normalized
            }
            caffeine_rows = parent.get("caffeine_records", [])
            if isinstance(caffeine_rows, list):
                for index, item in enumerate(caffeine_rows):
                    if not isinstance(item, dict):
                        continue
                    record_id = str(
                        item.get("caffeine_id")
                        or f"legacy_caffeine_{str(parent.get('date') or '')}_{index + 1:02d}"
                    )
                    if record_id in seen_ids:
                        continue
                    normalized.append(
                        {
                            "substance_record_id": record_id,
                            "substance_id": "caffeine",
                            "taken_at": item.get("taken_at", ""),
                            "amount_mg": item.get("amount_mg", ""),
                            "unit": "mg",
                            "source": item.get("source", "legacy_caffeine"),
                            "label": item.get("label", ""),
                            "memo": item.get("memo", ""),
                            "model_snapshot": item.get("model_snapshot")
                            if isinstance(item.get("model_snapshot"), dict)
                            else {
                                "schema_version": 1,
                                "profile_revision": 1,
                                "unit": "mg",
                                "half_life_hours": 5.0,
                                "peak_delay_minutes": 30,
                                "residual_cutoff_hours": 36.0,
                            },
                            "created_at": item.get("created_at", ""),
                            "updated_at": item.get("updated_at", ""),
                            "legacy_adapter": True,
                        }
                    )
                    seen_ids.add(record_id)
        parent_values = {
            "parent_date": parent.get("date", ""),
            "__parent_record_index": parent_index,
        }
        for index, item in enumerate(normalized):
            rows.append(_flat_row(source, index, item, maps, parent_values))
    return rows


def build_export_bundle(root: Path | None = None, generated_at: datetime | None = None) -> ExportBundle:
    project_root = (root or PROJECT_ROOT).resolve()
    generated = (generated_at or datetime.now()).replace(microsecond=0)
    warnings: list[str] = []

    master_path = project_root / "master_data" / "master.json"
    master_data = _read_json(master_path, project_root, warnings)
    master = master_data if isinstance(master_data, dict) else {}
    maps = _master_maps(master)

    study_rows, study_containers = _study_rows(project_root, maps, warnings)
    plan_refs = _directory_refs(project_root, "daily_plans", warnings)
    diary_refs = _directory_refs(project_root, "diary_entries", warnings)
    health_refs = _directory_refs(project_root, "health_logs", warnings)
    exercise_refs = _directory_refs(project_root, "exercise_logs", warnings)
    statistics_practice_refs = _directory_refs(project_root, "statistics_practice", warnings)
    flashcard_refs = _record_refs(project_root / "flashcards.json", project_root, warnings)
    reminder_refs = _record_refs(project_root / "reminders.json", project_root, warnings)
    notebook_rows, section_rows, page_rows = _note_rows(project_root, maps, warnings)
    roadmap_path = project_root / "settings/study_roadmap.json"
    roadmap_refs = _record_refs(roadmap_path, project_root, warnings) if roadmap_path.exists() else []

    tables = [
        _make_table(
            "study_sessions",
            "勉強セッション",
            "時間、換算金額、分類、コンディション、関連IDを含む学習記録。",
            study_rows,
            ("session_id", "start_at", "end_at", "duration_sec"),
        ),
        _make_table(
            "study_session_containers",
            "勉強ログのファイル情報",
            "元ファイルごとの形式・セッション数・外側の追加情報。__source_fileで勉強セッションと対応し、空ファイルや非セッション値も保持します。",
            study_containers,
            ("container_kind", "session_count", "metadata_json", "ignored_items_json"),
        ),
        _make_table(
            "daily_plans",
            "日次計画",
            "日ごとの目標、重点科目、復習対象、タスク一覧。",
            _rows_from_refs(plan_refs, maps),
            ("plan_id", "date", "target_total_minutes"),
        ),
        _make_table(
            "daily_plan_tasks",
            "計画タスク",
            "日次計画内のタスクを1タスク1行へ展開。",
            _child_rows(plan_refs, "tasks", maps, ("plan_id", "date")),
            ("parent_plan_id", "parent_date", "task_id", "title", "status"),
        ),
        _make_table(
            "diary_entries",
            "日記・ライフログ",
            "本文、気分、天気、良い席の確保、カフェイン合計、自己投資などの日次記録。",
            _rows_from_refs(diary_refs, maps),
            ("date", "title", "body", "mood", "weather", "good_seat_status", "caffeine_mg"),
        ),
        _make_table(
            "caffeine_records",
            "カフェイン摂取履歴",
            "時刻付きカフェインイベントを1摂取1行へ展開。",
            _child_rows(diary_refs, "caffeine_records", maps, ("date",)),
            ("parent_date", "caffeine_id", "taken_at", "amount_mg", "source", "label", "memo"),
        ),
        _make_table(
            "substance_records",
            "成分摂取履歴",
            "カフェインを含む設定済み成分の時刻付き摂取を1件1行へ展開。記録時の残存モデル値も保持します。",
            _substance_record_rows(diary_refs, maps),
            (
                "parent_date",
                "substance_record_id",
                "substance_id",
                "taken_at",
                "amount_mg",
                "unit",
                "label",
                "memo",
                "model_snapshot.half_life_hours",
                "model_snapshot.peak_delay_minutes",
                "model_snapshot.residual_cutoff_hours",
            ),
        ),
        _make_table(
            "substance_profiles",
            "成分設定",
            "成分ごとの初期量・製品や剤形、単純半減期モデル、表示色、面塗り、表示状態。",
            _substance_profile_rows(project_root, maps, warnings),
            (
                "substance_id",
                "name",
                "default_amount_mg",
                "default_label",
                "half_life_hours",
                "peak_delay_minutes",
                "residual_cutoff_hours",
                "color",
                "fill_enabled",
                "is_active",
            ),
        ),
        _make_table(
            "investment_records",
            "自己投資記録",
            "日記内の自己投資・費用明細を1項目1行へ展開。",
            _child_rows(diary_refs, "cost_records", maps, ("date",)),
            ("parent_date", "name", "amount", "source"),
        ),
        _make_table(
            "health_logs",
            "睡眠・体調ログ",
            "睡眠、疲労、ストレス、集中しやすさなどの日次記録。",
            _rows_from_refs(health_refs, maps),
            ("date", "sleep.sleep_duration_min", "condition.fatigue", "condition.stress", "condition.focus_score"),
        ),
        _make_table(
            "exercise_logs",
            "運動日次ログ",
            "運動記録を含む日単位の親データ。",
            _rows_from_refs(exercise_refs, maps),
            ("date", "created_at", "updated_at"),
        ),
        _make_table(
            "exercise_records",
            "運動明細",
            "種目、時間、距離、きつさ、運動後疲労を1運動1行へ展開。",
            _child_rows(exercise_refs, "records", maps, ("date",)),
            ("parent_date", "exercise_id", "type", "duration_min", "distance_km", "intensity", "fatigue_after"),
        ),
        _make_table(
            "statistics_practice_days",
            "統計演習の日次履歴",
            "日ごとの統計演習履歴。解答一覧を損失なくJSON文字列でも保持します。",
            _rows_from_refs(statistics_practice_refs, maps),
            ("schema_version", "date", "attempts"),
        ),
        _make_table(
            "statistics_practice_attempts",
            "統計演習の解答履歴",
            "固定した問題スナップショット、入力した解答、採点結果を1回1行へ展開。",
            _child_rows(statistics_practice_refs, "attempts", maps, ("date", "schema_version")),
            (
                "parent_date",
                "attempt_id",
                "attempted_at",
                "problem_id",
                "template_id",
                "topic",
                "title",
                "score",
                "max_score",
                "correct_count",
                "total_parts",
            ),
        ),
        _make_table(
            "notebooks",
            "ノートブック",
            "ノートブック単位の属性。セクション一覧もJSON文字列で保持。",
            notebook_rows,
            ("id", "name", "sections"),
        ),
        _make_table(
            "note_sections",
            "ノートセクション",
            "ノートブック内のセクション。ページ一覧もJSON文字列で保持。",
            section_rows,
            ("parent_notebook_id", "parent_notebook_name", "id", "name", "pages"),
        ),
        _make_table(
            "note_pages",
            "ノートページ",
            "本文、タグ、分類、関連セッション・カード・リマインダーID。",
            page_rows,
            ("parent_notebook_id", "parent_section_id", "note_id", "title", "body", "content"),
        ),
        _make_table(
            "flashcards",
            "記憶フラッシュ",
            "問題、解答、説明、タグ、重要度、関連ノート・分類。",
            _rows_from_refs(flashcard_refs, maps),
            ("card_id", "flashcard_id", "question", "answer", "front", "back", "explanation", "tags"),
        ),
        _make_table(
            "reminders",
            "復習リマインダー",
            "予定、状態、間隔反復、関連ノート・カード・分類。",
            _rows_from_refs(reminder_refs, maps),
            ("reminder_id", "title", "scheduled_date", "status", "review_history"),
        ),
        _make_table(
            "reminder_review_history",
            "復習履歴",
            "リマインダー内の復習履歴を1回1行へ展開。",
            _child_rows(reminder_refs, "review_history", maps, ("reminder_id", "schedule_id", "title")),
            ("parent_reminder_id", "parent_schedule_id", "reviewed_at", "result"),
        ),
        _make_table("master_projects", "プロジェクトマスタ", "プロジェクト分類。", _master_rows(master, "projects", maps), ("project_id", "name", "is_active")),
        _make_table("master_subjects", "科目マスタ", "科目分類。", _master_rows(master, "subjects", maps), ("subject_id", "project_id", "name", "is_active")),
        _make_table("master_materials", "教材マスタ", "教材分類。", _master_rows(master, "materials", maps), ("material_id", "project_id", "subject_id", "name", "is_active")),
        _make_table("master_units", "単元マスタ", "単元分類。", _master_rows(master, "units", maps), ("unit_id", "project_id", "subject_id", "material_id", "name", "is_active")),
        _make_table("master_tags", "タグマスタ", "タグ分類。", _master_rows(master, "tags", maps), ("tag_id", "name", "is_active")),
        _make_table(
            "settings",
            "ユーザー設定",
            "タイマー、表示テーマ、記録画面の初期値、教材リンク・次はここから、手動取り込みの相談提案箱。認証情報は対象外。",
            _settings_rows(project_root, maps, warnings, cached_refs={roadmap_path: roadmap_refs}),
            ("settings_file",),
        ),
    ]

    if roadmap_refs:
        roadmap = roadmap_refs[0][2] if len(roadmap_refs) == 1 else None
        if isinstance(roadmap, dict):
            for key, label in (("items", "学習工程"), ("milestones", "試験・節目"), ("allocations", "工程の日別割当")):
                records = roadmap.get(key, [])
                if isinstance(records, list):
                    rows = [_flat_row("settings/study_roadmap.json", index, record, maps) for index, record in enumerate(records) if isinstance(record, dict)]
                    tables.append(_make_table("roadmap_"+key, label, "学習ロードマップの定義。変更前の予定・日別タスクへの参照も保持します。", rows, ("id",)))
            if "calendar" in roadmap:
                from app_core.roadmap_planning import calendar_export_rows
                try:
                    capacity = calendar_export_rows(roadmap)
                except ValueError:
                    warnings.append("学習可能時間の設定が不正なため容量表を作成できません。設定一覧に元の内容を保持しました。")
                else:
                    rows = [_flat_row("settings/study_roadmap.json", index, record, maps) for index, record in enumerate(capacity)]
                    tables.append(_make_table("roadmap_capacity", "学習可能時間", "曜日ごとと例外日の学習可能時間（分）。", rows, ("kind", "weekday", "date", "capacity_minutes")))

    manifest_rows = [
        {
            "filename": table.filename,
            "dataset": table.key,
            "label": table.label,
            "rows": table.row_count,
            "columns": table.column_count,
            "description": table.description,
            "column_names": _json_text(list(table.columns)),
            "generated_at": generated.isoformat(timespec="seconds"),
        }
        for table in tables
    ]
    manifest_columns = ("filename", "dataset", "label", "rows", "columns", "description", "column_names", "generated_at")
    manifest = _csv_bytes(manifest_rows, manifest_columns)

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.csv", manifest)
        for table in tables:
            archive.writestr(table.filename, table.data)
        if warnings:
            warning_rows = [{"warning": warning} for warning in warnings]
            archive.writestr("export_warnings.csv", _csv_bytes(warning_rows, ("warning",)))

    return ExportBundle(
        generated_at=generated,
        tables=tuple(tables),
        manifest=manifest,
        zip_data=zip_buffer.getvalue(),
        warnings=tuple(warnings),
    )
