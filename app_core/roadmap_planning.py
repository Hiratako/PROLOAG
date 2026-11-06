"""Validated schedule previews and capacity-aware, explicit daily allocations."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta
from hashlib import sha256
import heapq
import json
import math
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from app_core import json_store as store
from app_core.json_operations import atomic_json_operation
from app_core.roadmap_service import (
    CLASS_KEYS,
    _classification,
    empty_roadmap,
    load_target_plan,
    parse_day,
    roadmap_path,
    validate_roadmap,
)


def content_hash(value) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def validate_dependencies(data: dict) -> list[str]:
    items = {row["id"]: row for row in data["items"]}
    degree = {key: 0 for key in items}
    children = defaultdict(list)
    for key, row in items.items():
        predecessors = row.get("predecessor_ids", [])
        if (
            not isinstance(predecessors, list)
            or len(predecessors) > 50
            or any(not isinstance(value, str) for value in predecessors)
        ):
            raise ValueError("前提工程は50件以内のID一覧で指定してください。")
        if (
            len(predecessors) != len(set(predecessors))
            or key in predecessors
            or any(value not in items for value in predecessors)
        ):
            raise ValueError("前提工程が重複・自己参照しているか、存在しません。")
        degree[key] = len(predecessors)
        for parent in predecessors:
            children[parent].append(key)
    priority = lambda key: (
        items[key].get("deadline") or items[key].get("end_date", "9999-12-31"),
        items[key].get("start_date", ""),
        key,
    )
    queue = [priority(key) for key, value in degree.items() if value == 0]
    heapq.heapify(queue)
    ordered = []
    while queue:
        key = heapq.heappop(queue)[-1]
        ordered.append(key)
        for child in sorted(children[key]):
            degree[child] -= 1
            if degree[child] == 0:
                heapq.heappush(queue, priority(child))
    if len(ordered) != len(items):
        raise ValueError("前提工程のつながりが循環しています。順序を見直してください。")
    return ordered


def calendar_settings(data: dict) -> dict:
    calendar = deepcopy(
        data.get("calendar", {"weekly_minutes": [0] * 7, "overrides": {}})
    )
    if not isinstance(calendar, dict):
        raise ValueError("学習可能時間の設定形式が不正です。")
    weekly = calendar.get("weekly_minutes")
    overrides = calendar.get("overrides")
    if (
        not isinstance(weekly, list)
        or len(weekly) != 7
        or any(type(value) is not int or not 0 <= value <= 1440 for value in weekly)
    ):
        raise ValueError("曜日ごとの学習可能時間を0〜1440分で設定してください。")
    if not isinstance(overrides, dict) or len(overrides) > 5000:
        raise ValueError("例外日の形式・件数が不正です。")
    for target, value in overrides.items():
        parse_day(target)
        if type(value) is not int or not 0 <= value <= 1440:
            raise ValueError("例外日の学習可能時間を0〜1440分で指定してください。")
    return calendar


def capacity_minutes(calendar: dict, target: date) -> int:
    return calendar["overrides"].get(
        target.isoformat(), calendar["weekly_minutes"][target.weekday()]
    )


def dependency_violations(data: dict) -> list[dict]:
    items = {row["id"]: row for row in data["items"]}
    result = []
    for row in items.values():
        if row["is_archived"]:
            continue
        for parent_id in row.get("predecessor_ids", []):
            parent = items[parent_id]
            if parse_day(row["start_date"]) <= parse_day(parent["end_date"]):
                result.append(
                    {
                        "item_id": row["id"],
                        "parent_id": parent_id,
                        "title": row["title"],
                        "parent_title": parent["title"],
                    }
                )
    return result


def require_schedule_order(data: dict) -> None:
    problems = dependency_violations(data)
    if problems:
        first = problems[0]
        raise ValueError(
            f"「{first['title']}」は前提工程「{first['parent_title']}」の終了翌日以降に開始してください。日程の一括調整も利用できます。"
        )


def descendants(data: dict, selected: set[str]) -> set[str]:
    children = defaultdict(set)
    for row in data["items"]:
        for parent in row.get("predecessor_ids", []):
            children[parent].add(row["id"])
    result = set(selected)
    pending = list(selected)
    while pending:
        key = pending.pop()
        for child in children[key] - result:
            result.add(child)
            pending.append(child)
    return result


def shift_changes(
    data: dict, identifiers: list[str], days: int, *, include_descendants=False
) -> dict:
    if (
        type(days) is not int
        or not -3650 <= days <= 3650
        or not isinstance(identifiers, list)
        or not identifiers
    ):
        raise ValueError("移動する工程と日数（最大±3650日）を指定してください。")
    items = {row["id"]: row for row in data["items"]}
    if any(not isinstance(key, str) or key not in items for key in identifiers):
        raise ValueError("移動対象の工程がありません。")
    targets = (
        descendants(data, set(identifiers)) if include_descendants else set(identifiers)
    )
    return {
        key: {
            "start_date": (
                parse_day(items[key]["start_date"]) + timedelta(days=days)
            ).isoformat(),
            "end_date": (
                parse_day(items[key]["end_date"]) + timedelta(days=days)
            ).isoformat(),
        }
        for key in sorted(targets)
        if not items[key]["is_archived"] and items[key]["status"] != "done"
    }


def _seal(preview: dict) -> dict:
    preview["digest"] = content_hash(preview)
    return preview


def _check_seal(preview: dict, kind: str) -> None:
    if (
        not isinstance(preview, dict)
        or preview.get("kind") != kind
        or preview.get("digest")
        != content_hash(
            {key: value for key, value in preview.items() if key != "digest"}
        )
    ):
        raise ValueError("確認内容が変更されています。プレビューを作り直してください。")


def schedule_preview(data: dict, changes: dict, *, cascade=True) -> dict:
    data = validate_roadmap(data)
    if (
        not isinstance(changes, dict)
        or len(changes) > 1000
        or type(cascade) is not bool
    ):
        raise ValueError("日程変更の形式・件数が不正です。")
    proposed = deepcopy(data)
    items = {row["id"]: row for row in proposed["items"]}
    for key, change in changes.items():
        if (
            key not in items
            or not isinstance(change, dict)
            or set(change) != {"start_date", "end_date"}
        ):
            raise ValueError("変更する工程と日程を確認してください。")
        item = items[key]
        if item["is_archived"] or item["status"] == "done":
            raise ValueError("完了・アーカイブ済みの工程は一括移動しません。")
        if parse_day(change["start_date"]) > parse_day(change["end_date"]):
            raise ValueError("開始日は終了予定日以前にしてください。")
        item.update(change)
    affected = descendants(proposed, set(changes)) if changes else set(items)
    if cascade:
        for key in validate_dependencies(proposed):
            item = items[key]
            if key not in affected or item["is_archived"]:
                continue
            parents = [items[parent] for parent in item.get("predecessor_ids", [])]
            if not parents:
                continue
            earliest = max(
                parse_day(parent["end_date"]) + timedelta(days=1) for parent in parents
            )
            start = parse_day(item["start_date"])
            if start < earliest:
                if item["status"] == "done":
                    raise ValueError(
                        "完了済みの後続工程と日程が衝突します。日程変更を見直してください。"
                    )
                delta = earliest - start
                item.update(
                    start_date=earliest.isoformat(),
                    end_date=(parse_day(item["end_date"]) + delta).isoformat(),
                )
    validate_roadmap(proposed)
    require_schedule_order(proposed)
    result = []
    warnings = []
    for old in data["items"]:
        new = items[old["id"]]
        if (old["start_date"], old["end_date"]) == (new["start_date"], new["end_date"]):
            continue
        result.append(
            {
                "item_id": old["id"],
                "title": old["title"],
                "before_start": old["start_date"],
                "before_end": old["end_date"],
                "start_date": new["start_date"],
                "end_date": new["end_date"],
            }
        )
        if new.get("deadline") and new["end_date"] > new["deadline"]:
            warnings.append(f"{new['title']}：確定期限 {new['deadline']} を超えます。")
        outside = sum(
            1
            for allocation in data["allocations"]
            if allocation["is_active"]
            and allocation["item_id"] == old["id"]
            and any(
                not new["start_date"] <= owner["date"] <= new["end_date"]
                for owner in [
                    {"date": allocation["task_date"]},
                    *allocation.get("task_keys", []),
                ]
            )
        )
        if outside:
            warnings.append(
                f"{new['title']}：既存の日別割当{outside}件が期間外になります。日別タスクは移動せず、元の日付に残します。"
            )
    return _seal(
        {
            "kind": "schedule",
            "base_hash": content_hash(data),
            "request": {"changes": deepcopy(changes), "cascade": cascade},
            "changes": result,
            "warnings": warnings,
        }
    )


def apply_schedule_preview(
    root: Path, preview: dict, *, expected_revision, operation_id: str
) -> bool:
    _check_seal(preview, "schedule")
    path = roadmap_path(root)
    with atomic_json_operation(
        root=root,
        kind="roadmap_schedule",
        operation_id=operation_id,
        expected_revisions={path: expected_revision},
        request_fingerprint=preview["digest"],
    ) as op:
        if op.already_committed:
            return False
        data = validate_roadmap(op.read(path, empty_roadmap()))
        if content_hash(data) != preview["base_hash"]:
            raise store.JsonConflictError(
                "確認後に計画が変更されました。プレビューを作り直してください。"
            )
        fresh = schedule_preview(data, **preview["request"])
        if fresh != preview:
            raise ValueError("日程の確認内容が一致しません。")
        if not preview["changes"]:
            raise ValueError("変更する日程がありません。")
        items = {row["id"]: row for row in data["items"]}
        now = datetime.now().isoformat(timespec="microseconds")
        for change in preview["changes"]:
            item = items[change["item_id"]]
            item["schedule_history"].append(
                {
                    "start_date": item["start_date"],
                    "end_date": item["end_date"],
                    "changed_at": now,
                }
            )
            item.update(
                start_date=change["start_date"],
                end_date=change["end_date"],
                updated_at=now,
            )
        validate_roadmap(data)
        op.stage(path, data)
    return True


def save_calendar(
    root: Path, calendar: dict, *, expected_revision, operation_id: str
) -> bool:
    calendar = calendar_settings({"calendar": calendar})
    path = roadmap_path(root)
    with atomic_json_operation(
        root=root,
        kind="roadmap_capacity",
        operation_id=operation_id,
        expected_revisions={path: expected_revision},
        request_fingerprint=content_hash(calendar),
    ) as op:
        if op.already_committed:
            return False
        data = validate_roadmap(op.read(path, empty_roadmap()))
        data["calendar"] = {**data.get("calendar", {}), **calendar}
        validate_roadmap(data)
        op.stage(path, data)
    return True


def planning_days(start: date, end: date) -> list[date]:
    if (
        type(start) is not date
        or type(end) is not date
        or not 0 <= (end - start).days < 366
    ):
        raise ValueError("配分期間は開始日から最大366日以内にしてください。")
    parse_day(start.isoformat())
    parse_day(end.isoformat())
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def load_planning_context(root: Path, start: date, end: date) -> tuple[dict, dict]:
    from data_utils import daily_plan_path

    plans = {}
    revisions = {}
    for target in planning_days(start, end):
        plans[target.isoformat()] = load_target_plan(root, target)
        path = store.resolved_json_path(daily_plan_path(root, target))
        revisions[path.relative_to(Path(root).resolve()).as_posix()] = (
            store.observed_json_revision(path)
        )
    return plans, revisions


def _planned_minutes(task: dict) -> int:
    raw = task.get("planned_minutes", 0)
    try:
        number = float(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("既存タスクの予定時間を解釈できません。") from exc
    if (
        isinstance(raw, bool)
        or not math.isfinite(number)
        or not 0 <= number <= 1440
        or not number.is_integer()
    ):
        raise ValueError("既存タスクの予定時間は0〜1440の整数分である必要があります。")
    return int(number)


def load_rows(
    data: dict, plans: dict, start: date, end: date, additions: list | None = None
) -> list[dict]:
    calendar = calendar_settings(data)
    added = defaultdict(int)
    for row in additions or []:
        added[row["date"]] += row["minutes"]
    result = []
    for target in planning_days(start, end):
        day = target.isoformat()
        plan = plans[day]
        existing = sum(_planned_minutes(task) for task in plan.get("tasks", []))
        capacity = capacity_minutes(calendar, target)
        new = added[day]
        total = existing + new
        result.append(
            {
                "date": day,
                "capacity_minutes": capacity,
                "existing_minutes": existing,
                "additional_minutes": new,
                "total_minutes": total,
                "free_minutes": max(0, capacity - total),
                "over_minutes": max(0, total - capacity),
            }
        )
    return result


def weekly_load(rows: list[dict]) -> list[dict]:
    weeks = {}
    for row in rows:
        day = parse_day(row["date"])
        key = (day - timedelta(days=day.weekday())).isoformat()
        bucket = weeks.setdefault(
            key,
            {
                "week": key,
                "capacity_minutes": 0,
                "existing_minutes": 0,
                "additional_minutes": 0,
            },
        )
        for field in ("capacity_minutes", "existing_minutes", "additional_minutes"):
            bucket[field] += row[field]
    return [weeks[key] for key in sorted(weeks)]


def distribution_preview(
    data: dict, requests: dict, start: date, end: date, plans: dict, revisions: dict
) -> dict:
    data = validate_roadmap(data)
    days = planning_days(start, end)
    items = {row["id"]: row for row in data["items"]}
    if (
        not isinstance(requests, dict)
        or not 1 <= len(requests) <= 100
        or any(
            not isinstance(key, str)
            or key not in items
            or type(value) is not int
            or not 0 <= value <= 600000
            for key, value in requests.items()
        )
    ):
        raise ValueError(
            "配分対象は100工程以内、追加する時間は0〜600000分の整数で指定してください。"
        )
    if not any(requests.values()):
        raise ValueError("今回新たに配分する時間を1分以上入力してください。")
    if set(plans) != {day.isoformat() for day in days}:
        raise ValueError("対象日の計画一覧が一致しません。")
    load = load_rows(data, plans, start, end)
    available = {row["date"]: row["free_minutes"] for row in load}
    ranks = {key: index for index, key in enumerate(validate_dependencies(data))}
    # Topological priority ensures a selected prerequisite is considered first.
    order = sorted(
        requests,
        key=lambda key: (
            ranks[key],
            items[key].get("deadline") or items[key]["end_date"],
            key,
        ),
    )
    rows = []
    unassigned = []
    short = set()
    for key in order:
        item = items[key]
        remaining = requests[key]
        if not remaining:
            continue
        if item["is_archived"] or item["status"] in {"done", "pending"}:
            raise ValueError("完了・保留・アーカイブ済みの工程には自動配分しません。")
        parents = item.get("predecessor_ids", [])
        blocked = any(
            parent in short for parent in descendants_reverse(data, {key}) - {key}
        )
        earliest = max(
            [
                parse_day(item["start_date"]),
                start,
                *[
                    parse_day(items[parent]["end_date"]) + timedelta(days=1)
                    for parent in parents
                ],
            ]
        )
        latest = min(
            parse_day(item["end_date"]),
            end,
            parse_day(item["deadline"]) if item.get("deadline") else end,
        )
        if not blocked:
            for target in days:
                if not earliest <= target <= latest:
                    continue
                day = target.isoformat()
                minutes = min(remaining, available[day])
                if minutes:
                    rows.append(
                        {
                            "item_id": key,
                            "title": item["title"],
                            "date": day,
                            "minutes": minutes,
                        }
                    )
                    available[day] -= minutes
                    remaining -= minutes
                if not remaining:
                    break
        if remaining:
            short.add(key)
            unassigned.append(
                {
                    "item_id": key,
                    "title": item["title"],
                    "minutes": remaining,
                    "reason": "前提工程に未配分時間があります"
                    if blocked
                    else "工程の期間・確定期限内の空き時間が不足しています",
                }
            )
    if len(rows) > 2000:
        raise ValueError("配分結果が2000件を超えます。工程や期間を分けてください。")
    return _seal(
        {
            "kind": "distribution",
            "base_hash": content_hash(data),
            "requests": deepcopy(requests),
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "plan_revisions": deepcopy(revisions),
            "rows": rows,
            "unassigned": unassigned,
            "load": load_rows(data, plans, start, end, rows),
        }
    )


def descendants_reverse(data: dict, selected: set[str]) -> set[str]:
    parents = {row["id"]: row.get("predecessor_ids", []) for row in data["items"]}
    result = set(selected)
    pending = list(selected)
    while pending:
        key = pending.pop()
        for parent in set(parents[key]) - result:
            result.add(parent)
            pending.append(parent)
    return result


def build_distribution_preview(
    root: Path, data: dict, requests: dict, start: date, end: date
) -> dict:
    plans, revisions = load_planning_context(root, start, end)
    return distribution_preview(data, requests, start, end, plans, revisions)


def apply_distribution_preview(
    root: Path,
    preview: dict,
    *,
    expected_revision,
    operation_id: str,
    allow_partial=False,
) -> bool:
    from data_utils import daily_plan_path, generate_id, save_daily_plan

    _check_seal(preview, "distribution")
    if type(allow_partial) is not bool or (preview["unassigned"] and not allow_partial):
        raise ValueError(
            "未配分時間があります。配分できた分だけ保存する場合は確認してください。"
        )
    if not preview["rows"]:
        raise ValueError(
            "配分できた時間がありません。学習可能時間や期間を見直してください。"
        )
    start, end = parse_day(preview["start_date"]), parse_day(preview["end_date"])
    expected_paths = {
        store.resolved_json_path(daily_plan_path(root, day))
        .relative_to(Path(root).resolve())
        .as_posix()
        for day in planning_days(start, end)
    }
    if set(preview["plan_revisions"]) != expected_paths:
        raise ValueError("確認した日次計画の一覧が一致しません。")
    path = roadmap_path(root)
    expected = {
        Path(root) / key: value for key, value in preview["plan_revisions"].items()
    }
    expected[path] = expected_revision
    fingerprint = content_hash([preview["digest"], allow_partial])
    with atomic_json_operation(
        root=root,
        kind="roadmap_distribution",
        operation_id=operation_id,
        expected_revisions=expected,
        request_fingerprint=fingerprint,
    ) as op:
        if op.already_committed:
            return False
        data = validate_roadmap(op.read(path, empty_roadmap()))
        if content_hash(data) != preview["base_hash"]:
            raise store.JsonConflictError(
                "確認後に工程・学習可能時間が変わりました。プレビューを作り直してください。"
            )
        plans, revisions = load_planning_context(root, start, end)
        fresh = distribution_preview(
            data, preview["requests"], start, end, plans, revisions
        )
        if fresh != preview:
            raise store.JsonConflictError(
                "確認後に日別計画が変わりました。プレビューを作り直してください。"
            )
        items = {row["id"]: row for row in data["items"]}
        changed_days = set()
        ids = {
            day: {task["task_id"] for task in plan.get("tasks", [])}
            for day, plan in plans.items()
        }
        now = datetime.now().isoformat(timespec="microseconds")
        for row in preview["rows"]:
            item = items[row["item_id"]]
            _classification(root, {}, item)
            day = row["date"]
            task_id = generate_id("task", day, ids[day])
            allocation_id = (
                "ra_"
                + uuid5(NAMESPACE_URL, operation_id + ":" + day + ":" + item["id"]).hex
            )
            task = {
                "task_id": task_id,
                "title": item["title"] + "（追加配分）",
                "planned_minutes": row["minutes"],
                "status": "not_started",
                "linked_session_ids": [],
                "linked_note_ids": [],
                "roadmap_item_id": item["id"],
                "roadmap_allocation_id": allocation_id,
                **{key: item[key] for key in CLASS_KEYS},
            }
            plans[day].setdefault("tasks", []).append(task)
            changed_days.add(day)
            data["allocations"].append(
                {
                    "id": allocation_id,
                    "item_id": item["id"],
                    "task_date": day,
                    "task_id": task_id,
                    "task_keys": [{"date": day, "task_id": task_id}],
                    "is_active": True,
                    "created_at": now,
                    "source": "roadmap_distribution",
                    "distribution_id": operation_id,
                }
            )
        validate_roadmap(data)
        op.stage(path, data)
        for day in sorted(changed_days):
            save_daily_plan(root, plans[day])
    return True


def calendar_export_rows(data: dict) -> list[dict]:
    calendar = calendar_settings(data)
    return [
        {"kind": "weekday", "weekday": index, "date": "", "capacity_minutes": minutes}
        for index, minutes in enumerate(calendar["weekly_minutes"])
    ] + [
        {"kind": "override", "weekday": "", "date": day, "capacity_minutes": minutes}
        for day, minutes in sorted(calendar["overrides"].items())
    ]
