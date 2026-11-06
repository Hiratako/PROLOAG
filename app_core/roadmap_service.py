"""Long-term study stages, explicit daily allocations and read-only actuals."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from uuid import uuid5, NAMESPACE_URL

from app_core import json_store as store
from app_core.json_operations import atomic_json_operation
from app_core.study_record_reader import unpack_study_document

STATUS_LABELS = {
    "not_started": "未着手",
    "in_progress": "進行中",
    "done": "完了",
    "pending": "保留",
}
CLASS_KEYS = ("project_id", "subject_id", "material_id", "unit_id")
ITEM_FIELDS = {
    "title",
    "start_date",
    "end_date",
    "deadline",
    "planned_minutes",
    "status",
    "progress_kind",
    "completed",
    "total",
    "unit",
    "memo",
    "is_archived",
    "predecessor_ids",
    *CLASS_KEYS,
}
MILESTONE_FIELDS = {"title", "date", "project_id", "memo", "is_archived"}


def roadmap_path(root: Path) -> Path:
    return store.resolved_json_path(Path(root) / "settings/study_roadmap.json")


def empty_roadmap() -> dict:
    return {"schema_version": 1, "items": [], "milestones": [], "allocations": []}


def parse_day(value: str) -> date:
    if not isinstance(value, str):
        raise ValueError("日付を確認してください。")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("日付はYYYY-MM-DD形式で指定してください。") from exc
    if parsed.isoformat() != value or not 1900 <= parsed.year <= 2199:
        raise ValueError("日付は1900〜2199年のYYYY-MM-DD形式で指定してください。")
    return parsed


def _text(value, label: str, maximum=200, *, blank=False):
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not blank and not value.strip())
        or "\x00" in value
    ):
        raise ValueError(
            f"{label}は{'0' if blank else '1'}〜{maximum}文字で入力してください。"
        )
    return value


def _number(value, label, minimum=0, maximum=1e9):
    if (
        type(value) not in (int, float)
        or not minimum <= value <= maximum
        or not math.isfinite(value)
    ):
        raise ValueError(f"{label}の値を確認してください。")


def validate_roadmap(data) -> dict:
    if (
        not isinstance(data, dict)
        or type(data.get("schema_version")) is not int
        or data["schema_version"] != 1
    ):
        raise ValueError("学習ロードマップの保存形式が不正です。")
    all_ids, item_ids, task_keys, lineage_keys = set(), set(), set(), set()
    for collection in ("items", "milestones", "allocations"):
        rows = data.get(collection)
        if not isinstance(rows, list) or len(rows) > 10000:
            raise ValueError("学習ロードマップの一覧形式・件数が不正です。")
        for row in rows:
            prefix = {"items": "rm", "milestones": "ms", "allocations": "ra"}[
                collection
            ]
            identifier = row.get("id") if isinstance(row, dict) else None
            if (
                not isinstance(identifier, str)
                or not re.fullmatch(prefix + r"_[0-9a-f]{32}", identifier)
                or identifier in all_ids
            ):
                raise ValueError("学習ロードマップのIDが不正か重複しています。")
            all_ids.add(identifier)
            if collection == "items":
                item_ids.add(identifier)
                _text(row.get("title"), "工程名")
                start, end = (
                    parse_day(row.get("start_date")),
                    parse_day(row.get("end_date")),
                )
                if start > end:
                    raise ValueError("開始日は終了予定日以前にしてください。")
                if parse_day(row.get("baseline_start_date")) > parse_day(
                    row.get("baseline_end_date")
                ):
                    raise ValueError("当初予定の日付が不正です。")
                if row.get("deadline"):
                    parse_day(row["deadline"])
                _number(row.get("planned_minutes"), "予定時間", maximum=600000)
                if type(row["planned_minutes"]) is not int:
                    raise ValueError("予定時間は整数の分数で入力してください。")
                if (
                    not isinstance(row.get("status"), str)
                    or row["status"] not in STATUS_LABELS
                    or not isinstance(row.get("progress_kind"), str)
                    or row["progress_kind"] not in {"quantity", "percent"}
                ):
                    raise ValueError("工程の状態・進捗方式が不正です。")
                _number(row.get("total"), "総量", minimum=0.000001)
                _number(row.get("completed"), "完了量", maximum=row["total"])
                if row["progress_kind"] == "percent" and row["total"] != 100:
                    raise ValueError("割合で管理する場合の総量は100です。")
                _text(row.get("unit"), "進捗の単位", 20)
                if row["status"] == "done" and row["completed"] < row["total"]:
                    raise ValueError("完了にする場合は進捗を100%にしてください。")
                for key in CLASS_KEYS:
                    _text(row.get(key), "分類ID", 200, blank=True)
                history = row.get("schedule_history")
                if not isinstance(history, list) or len(history) > 1000:
                    raise ValueError("予定変更履歴の形式・件数が不正です。")
                for previous in history:
                    if not isinstance(previous, dict) or parse_day(
                        previous.get("start_date")
                    ) > parse_day(previous.get("end_date")):
                        raise ValueError("予定変更履歴の日付が不正です。")
            elif collection == "milestones":
                _text(row.get("title"), "節目の名前")
                parse_day(row.get("date"))
                _text(row.get("project_id"), "プロジェクトID", 200, blank=True)
            else:
                if (
                    not isinstance(row.get("item_id"), str)
                    or row["item_id"] not in item_ids
                    or type(row.get("is_active")) is not bool
                ):
                    raise ValueError("日別割当の工程・有効状態が不正です。")
                parse_day(row.get("task_date"))
                _text(row.get("task_id"), "タスクID")
                key = row["task_date"], row["task_id"]
                if row["is_active"] and key in task_keys:
                    raise ValueError("同じ日別タスクが重複して割り当てられています。")
                if row["is_active"]:
                    task_keys.add(key)
                lineage = row.get("task_keys", [])
                if not isinstance(lineage, list) or len(lineage) > 10000:
                    raise ValueError("繰越先タスクの一覧が不正です。")
                for owner in lineage:
                    if not isinstance(owner, dict):
                        raise ValueError("繰越先タスクの形式が不正です。")
                    parse_day(owner.get("date"))
                    _text(owner.get("task_id"), "繰越先タスクID")
                if row["is_active"]:
                    owned = {
                        key,
                        *((owner["date"], owner["task_id"]) for owner in lineage),
                    }
                    if owned & lineage_keys:
                        raise ValueError(
                            "繰越先の日別タスクが複数の割当に重複しています。"
                        )
                    lineage_keys.update(owned)
            if collection != "allocations":
                if type(row.get("is_archived")) is not bool:
                    raise ValueError("アーカイブ状態が不正です。")
                _text(row.get("memo"), "メモ", 20000, blank=True)
    from app_core.roadmap_planning import validate_dependencies, calendar_settings

    validate_dependencies(data)
    calendar_settings(data)
    return deepcopy(data)


def load_roadmap(root: Path) -> dict:
    path = roadmap_path(root)
    raw = store.load_json_safe(path, None)
    return (
        empty_roadmap()
        if raw is None and store.observed_json_revision(path) is None
        else validate_roadmap(raw)
    )


def _fingerprint(value):
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode(
            "utf-8"
        )
    ).hexdigest()


def _classification(root: Path, current: dict, updates: dict):
    from app_core.daily_task_service import _validate_changed_classification

    _validate_changed_classification(root, current, updates)


def save_entry(
    root: Path,
    collection: str,
    payload: dict,
    *,
    identifier="",
    expected_revision,
    operation_id: str,
) -> bool:
    if collection not in {"items", "milestones"} or not isinstance(payload, dict):
        raise ValueError("保存対象を確認してください。")
    allowed = ITEM_FIELDS if collection == "items" else MILESTONE_FIELDS
    if set(payload) - allowed:
        raise ValueError("保存対象に変更できない項目があります。")
    path = roadmap_path(root)
    request = _fingerprint([collection, identifier, payload])
    with atomic_json_operation(
        root=root,
        kind="roadmap_edit",
        operation_id=operation_id,
        expected_revisions={path: expected_revision},
        request_fingerprint=request,
    ) as op:
        if op.already_committed:
            return False
        data = validate_roadmap(op.read(path, empty_roadmap()))
        current = next(
            (row for row in data[collection] if row["id"] == identifier), None
        )
        if identifier and current is None:
            raise ValueError("編集対象がありません。入力を保持して再読込してください。")
        previous = deepcopy(current or {})
        now = datetime.now().isoformat(timespec="microseconds")
        if current is None:
            prefix = "rm" if collection == "items" else "ms"
            current = {
                "id": prefix + "_" + uuid5(NAMESPACE_URL, operation_id).hex,
                "created_at": now,
            }
            data[collection].append(current)
        current.update(deepcopy(payload), updated_at=now)
        if collection == "items":
            if not previous:
                current.update(
                    baseline_start_date=current.get("start_date"),
                    baseline_end_date=current.get("end_date"),
                    schedule_history=[],
                )
            elif (previous["start_date"], previous["end_date"]) != (
                current["start_date"],
                current["end_date"],
            ):
                current["schedule_history"].append(
                    {
                        "start_date": previous["start_date"],
                        "end_date": previous["end_date"],
                        "changed_at": now,
                    }
                )
        _classification(root, previous, current)
        validate_roadmap(data)
        if collection == "items" and any(
            previous.get(key) != current.get(key)
            for key in ("start_date", "end_date", "predecessor_ids", "is_archived")
        ):
            from app_core.roadmap_planning import require_schedule_order

            require_schedule_order(data)
        op.stage(path, data)
    return True


def load_target_plan(root: Path, target: date) -> dict:
    from data_utils import daily_plan_path, default_daily_plan

    path = daily_plan_path(root, target)
    raw = store.load_json_safe(path, None)
    if raw is None and store.observed_json_revision(path) is None:
        return default_daily_plan(target)
    if (
        not isinstance(raw, dict)
        or raw.get("date") != target.isoformat()
        or not isinstance(raw.get("tasks", []), list)
    ):
        raise ValueError("対象日の計画が不正です。上書きしません。")
    if any(
        not isinstance(row, dict)
        or not isinstance(row.get("task_id"), str)
        or not row["task_id"]
        for row in raw.get("tasks", [])
    ):
        raise ValueError("対象日のタスク形式が不正です。")
    ids = [row["task_id"] for row in raw.get("tasks", [])]
    if len(ids) != len(set(ids)):
        raise ValueError("対象日のタスクIDが重複しています。")
    return raw


def allocate_task(
    root: Path,
    item_id: str,
    target: date,
    *,
    task_id="",
    title="",
    planned_minutes=0,
    expected_revisions: dict,
    operation_id: str,
) -> bool:
    from data_utils import daily_plan_path, save_daily_plan, generate_id

    if (
        type(target) is not date
        or type(planned_minutes) is not int
        or not 0 <= planned_minutes <= 1440
    ):
        raise ValueError("割当日と予定時間（0〜1440分）を確認してください。")
    path, plan_path = (
        roadmap_path(root),
        store.resolved_json_path(daily_plan_path(root, target)),
    )
    revisions = {
        store.resolved_json_path(key): value
        for key, value in expected_revisions.items()
    }
    if not {path, plan_path} <= revisions.keys():
        raise ValueError("割当開始時の計画を確認できません。再読込してください。")
    request = _fingerprint(
        [item_id, target.isoformat(), task_id, title, planned_minutes]
    )
    with atomic_json_operation(
        root=root,
        kind="roadmap_allocation",
        operation_id=operation_id,
        expected_revisions=revisions,
        request_fingerprint=request,
    ) as op:
        if op.already_committed:
            return False
        data = validate_roadmap(op.read(path, empty_roadmap()))
        item = next(
            (
                row
                for row in data["items"]
                if row["id"] == item_id and not row["is_archived"]
            ),
            None,
        )
        if item is None:
            raise ValueError("有効な学習工程を選択してください。")
        plan = load_target_plan(root, target)
        if task_id:
            task = next(
                (row for row in plan.get("tasks", []) if row["task_id"] == task_id),
                None,
            )
            if task is None:
                raise ValueError("関連付けるタスクが削除されています。")
            linked = next(
                (
                    row
                    for row in data["allocations"]
                    if row["is_active"]
                    and (
                        row["id"] == task.get("roadmap_allocation_id")
                        or (row["task_date"], row["task_id"])
                        == (target.isoformat(), task_id)
                    )
                ),
                None,
            )
            if linked:
                if linked["item_id"] != item_id:
                    raise ValueError(
                        "別の工程に関連付いています。先に関連付けを解除してください。"
                    )
                return False
        else:
            _text(title, "タスク名")
            _classification(root, {}, item)
            task = {
                "task_id": generate_id(
                    "task",
                    target.isoformat(),
                    {row["task_id"] for row in plan.get("tasks", [])},
                ),
                "title": title.strip(),
                "planned_minutes": planned_minutes,
                "status": "not_started",
                "linked_session_ids": [],
                "linked_note_ids": [],
                **{key: item[key] for key in CLASS_KEYS},
            }
            plan.setdefault("tasks", []).append(task)
        allocation = {
            "id": "ra_" + uuid5(NAMESPACE_URL, operation_id).hex,
            "item_id": item_id,
            "task_date": target.isoformat(),
            "task_id": task["task_id"],
            "is_active": True,
            "task_keys": [{"date": target.isoformat(), "task_id": task["task_id"]}],
            "created_at": datetime.now().isoformat(timespec="microseconds"),
        }
        task.update(roadmap_item_id=item_id, roadmap_allocation_id=allocation["id"])
        data["allocations"].append(allocation)
        validate_roadmap(data)
        op.stage(path, data)
        save_daily_plan(root, plan)
    return True


def save_linked_daily_plan(root: Path, plan: dict, *, expected_revision) -> None:
    """Keep explicit carried task identities even if a daily task is later removed."""
    from data_utils import daily_plan_path, save_daily_plan

    target = parse_day(plan.get("date"))
    plan_path = store.resolved_json_path(daily_plan_path(root, target))
    path = roadmap_path(root)
    with atomic_json_operation(
        root=root,
        kind="roadmap_task_lineage",
        expected_revisions={plan_path: expected_revision},
    ) as op:
        op.read(plan_path, None)
        data = validate_roadmap(op.read(path, empty_roadmap()))
        allocations = {row["id"]: row for row in data["allocations"]}
        changed = False
        for task in plan.get("tasks", []):
            identifier = task.get("roadmap_allocation_id")
            if not identifier:
                continue
            row = allocations.get(identifier)
            if row is None or row["item_id"] != task.get("roadmap_item_id"):
                raise ValueError("タスクと学習工程の関連付けを確認できません。")
            owner = {"date": target.isoformat(), "task_id": task.get("task_id")}
            if owner not in row.setdefault("task_keys", []):
                row["task_keys"].append(owner)
                changed = True
        if changed:
            validate_roadmap(data)
            op.stage(path, data)
        save_daily_plan(root, plan)


def deactivate_allocation(
    root: Path, allocation_id: str, *, expected_revision, operation_id: str
) -> bool:
    path = roadmap_path(root)
    with atomic_json_operation(
        root=root,
        kind="roadmap_unlink",
        operation_id=operation_id,
        expected_revisions={path: expected_revision},
        request_fingerprint=_fingerprint(allocation_id),
    ) as op:
        if op.already_committed:
            return False
        data = validate_roadmap(op.read(path, empty_roadmap()))
        row = next(
            (row for row in data["allocations"] if row["id"] == allocation_id), None
        )
        if row is None:
            raise ValueError("関連付けがありません。")
        row["is_active"] = False
        op.stage(path, data)
    return True


def progress_percent(item: dict) -> float:
    return 100 * item["completed"] / item["total"]


def schedule_label(item: dict, today: date) -> str:
    if item["status"] in {"done", "pending"}:
        return STATUS_LABELS[item["status"]]
    if item.get("deadline") and parse_day(item["deadline"]) < today:
        return "期限超過"
    if parse_day(item["end_date"]) < today:
        return "終了予定日超過"
    return STATUS_LABELS[item["status"]]


def build_actuals(root: Path, data: dict) -> dict:
    """Read each plan/session document once; never infer progress from hours."""
    active = {row["id"]: row for row in data["allocations"] if row["is_active"]}
    result = {
        "seconds": {row["id"]: 0.0 for row in data["items"]},
        "counts": {row["id"]: 0 for row in data["items"]},
        "allocation_counts": {row["id"]: 0 for row in data["items"]},
        "task_counts": {row["id"]: 0 for row in data["items"]},
        "broken_allocation_counts": {row["id"]: 0 for row in data["items"]},
        "tasks": defaultdict(list),
        "warnings": [],
    }
    if not active:
        return result
    task_keys_by_item = defaultdict(set)
    broken_allocations = set()
    for allocation in active.values():
        result["allocation_counts"][allocation["item_id"]] += 1
    owners, reverse = defaultdict(set), defaultdict(set)
    originals = {(row["task_date"], row["task_id"]): row for row in active.values()}
    for allocation in active.values():
        for owner in [
            {"date": allocation["task_date"], "task_id": allocation["task_id"]},
            *allocation.get("task_keys", []),
        ]:
            owners[(owner["date"], owner["task_id"])].add(allocation["item_id"])
    for path in store.record_json_files(Path(root) / "daily_plans"):
        plan = store.load_json_safe(path, None)
        if not isinstance(plan, dict) or not isinstance(plan.get("tasks", []), list):
            raise ValueError("日次計画の形式を確認できません。実績集計を中止しました。")
        day = plan.get("date", "")
        parse_day(day)
        task_ids = [
            row.get("task_id") for row in plan.get("tasks", []) if isinstance(row, dict)
        ]
        if any(not isinstance(value, str) for value in task_ids) or len(
            task_ids
        ) != len(set(task_ids)):
            raise ValueError("日別タスクのIDが不正か重複しています。")
        for task in plan.get("tasks", []):
            if not isinstance(task, dict):
                raise ValueError("日次タスクの形式が不正です。")
            if task.get("roadmap_allocation_id") is not None and not isinstance(
                task["roadmap_allocation_id"], str
            ):
                raise ValueError("日別タスクの工程参照が不正です。")
            allocation = active.get(task.get("roadmap_allocation_id")) or originals.get(
                (day, task.get("task_id"))
            )
            if not allocation:
                continue
            if (
                task.get("roadmap_item_id", allocation["item_id"])
                != allocation["item_id"]
            ):
                result["warnings"].append("工程と日別タスクの関連に不一致があります。")
                broken_allocations.add(allocation["id"])
                continue
            identifier = allocation["item_id"]
            owners[(day, task.get("task_id"))].add(identifier)
            result["tasks"][allocation["id"]].append({"date": day, **deepcopy(task)})
            task_keys_by_item[identifier].add((day, task.get("task_id")))
            linked_ids = task.get("linked_session_ids", [])
            if not isinstance(linked_ids, list) or any(
                not isinstance(value, str) for value in linked_ids
            ):
                raise ValueError("日別タスクのセッション参照が不正です。")
            for session_id in linked_ids:
                reverse[session_id].add(identifier)
    sessions = defaultdict(list)
    for path in store.record_json_files(Path(root) / "study_logs"):
        document = unpack_study_document(store.load_json_safe(path, []))
        if document.ignored_items or document.container_kind == "unsupported":
            result["warnings"].append("解釈できない勉強ログの要素があります。")
        for index, row in document.records:
            key = str(row.get("session_id") or f"{path}:{index}")
            sessions[key].append(row)
    for key, records in sessions.items():
        if len({_fingerprint(row) for row in records}) != 1:
            result["warnings"].append(
                "内容の異なる同一セッションIDを実績から除外しました。"
            )
            continue
        row = records[0]
        task_id, task_day = row.get("linked_task_id"), row.get("linked_task_date")
        if (task_id is not None and not isinstance(task_id, str)) or (
            task_day is not None and not isinstance(task_day, str)
        ):
            result["warnings"].append(
                "タスクへの参照形式が不正な学習記録を除外しました。"
            )
            continue
        if task_id and task_day:
            candidates = owners.get((task_day, task_id), set())
        elif task_id:
            candidates = set().union(
                *(values for (day, tid), values in owners.items() if tid == task_id)
            )
        else:
            candidates = (
                reverse.get(key, set()) if "linked_task_id" not in row else set()
            )
        if len(candidates) > 1:
            result["warnings"].append(
                "複数工程に関連する学習記録を実績から除外しました。"
            )
        if len(candidates) != 1:
            continue
        seconds = row.get("duration_sec")
        try:
            valid_seconds = (
                type(seconds) in (int, float)
                and seconds >= 0
                and math.isfinite(seconds)
            )
        except OverflowError:
            valid_seconds = False
        if not valid_seconds:
            result["warnings"].append("実勉強時間が不正な記録を実績から除外しました。")
            continue
        identifier = next(iter(candidates))
        result["seconds"][identifier] += seconds
        result["counts"][identifier] += 1
    for identifier in active:
        if not result["tasks"][identifier]:
            broken_allocations.add(identifier)
            result["warnings"].append(
                "関連付けた日別タスクが見つかりません。削除済みか確認してください。"
            )
    for identifier, keys in task_keys_by_item.items():
        result["task_counts"][identifier] = len(keys)
    for identifier in broken_allocations:
        result["broken_allocation_counts"][active[identifier]["item_id"]] += 1
    result["warnings"] = list(dict.fromkeys(result["warnings"]))
    return result
