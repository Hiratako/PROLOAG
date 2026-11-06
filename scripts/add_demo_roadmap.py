"""Add a small deterministic roadmap to a fresh, verified consultant demo.

This release-only helper never accepts an unmarked/modified dataset or the
development application. Run after generate_consultant_demo_dataset.py and
before packaging. No original personal JSON is used.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
from uuid import NAMESPACE_URL, uuid5

MANIFEST = ".consultant_demo_manifest.json"
GENERATOR = "PROLOAG consultant synthetic dataset"
AUGMENTATION = "PROLOAG public release roadmap sample v1"
SOURCE_ROOT = Path(__file__).resolve().parents[1]


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def no_link(path):
    if path.is_symlink() or (path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError(f"Links/junctions are not accepted: {path}")


def safe_target(target, source_root=None):
    candidate = Path(os.path.abspath(target))
    for part in (candidate, *candidate.parents):
        no_link(part)
    root = candidate.resolve()
    forbidden = [Path(source_root or SOURCE_ROOT).resolve()]
    if any(root == p or root in p.parents for p in forbidden):
        raise ValueError("The development/source application and its ancestors are never accepted")
    if root.name.lower() != "app" or not all(word in root.parent.name.lower() for word in ("demo", "consultant")):
        raise ValueError("Use a dedicated demo_consultant package's app directory")
    if not (root / "main.py").is_file():
        raise ValueError("Target must contain the packaged main.py")
    if not (root / MANIFEST).is_file():
        raise ValueError("A fresh consultant dataset manifest is required")
    for p in root.rglob("*"):
        no_link(p)
    return root


def inspect_dataset(root, end_date):
    marker = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    if not isinstance(marker, dict) or marker.get("schema_version") != 1 or marker.get("generator") != GENERATOR:
        raise ValueError("Unknown synthetic dataset provenance")
    if marker.get("end_date") != end_date.isoformat():
        raise ValueError("Roadmap date must equal the dataset's fixed reference date")
    listed = marker.get("files")
    if not isinstance(listed, dict) or not listed:
        raise ValueError("Dataset manifest must contain generated files")
    json_names = {p.relative_to(root).as_posix() for p in root.rglob("*.json") if p.name != MANIFEST}
    if json_names != set(listed):
        raise ValueError("Unlisted/missing JSON exists; refusing to modify the dataset")
    for name, expected in listed.items():
        rel = PurePosixPath(name)
        if rel.is_absolute() or ".." in rel.parts or "\\" in name or not name.endswith(".json"):
            raise ValueError("Unsafe dataset manifest path")
        p = root / name
        if not p.resolve().is_relative_to(root) or hashlib.sha256(p.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Generated data has changed; regenerate instead of overwriting: {name}")
    mode = json.loads((root / "settings/demo_mode.json").read_text(encoding="utf-8"))
    if mode.get("enabled") is not True or mode.get("fixed_today") != end_date.isoformat():
        raise ValueError("Target is not the expected fixed-date Demo")
    return marker


def identifier(prefix, key):
    return prefix + "_" + uuid5(NAMESPACE_URL, AUGMENTATION + ":" + key).hex


def build_roadmap(master, today):
    subjects = {row["subject_id"]: row for row in master["subjects"]}
    materials = {row["material_id"]: row for row in master["materials"]}
    courses = [
        ("strategy", "企業経営理論", "material_demo_strategy_text", [
            ("経営戦略・組織論の基礎整理", -14, -8, "done", 8, 8, "章", 240),
            ("経営理論の過去問演習", -7, 2, "in_progress", 18, 40, "問", 480),
            ("弱点の復習・模試準備", 3, 10, "not_started", 0, 20, "問", 240),
        ]),
        ("finance", "財務・会計", "material_demo_finance_workbook", [
            ("財務諸表・会計の基礎整理", -12, -7, "done", 5, 5, "章", 240),
            ("財務会計の過去問演習", -6, 3, "in_progress", 12, 30, "問", 480),
            ("投資評価の復習・模試準備", 4, 12, "not_started", 0, 20, "問", 300),
        ]),
        ("operations", "運営管理", "material_demo_operations_text", [
            ("生産管理の基礎整理", -10, -4, "done", 5, 5, "章", 240),
            ("運営管理の過去問演習", -3, 5, "in_progress", 8, 25, "問", 420),
            ("店舗・物流管理の復習", 6, 16, "not_started", 0, 20, "問", 240),
        ]),
    ]
    data = {"schema_version": 1, "items": [], "milestones": [], "allocations": [],
            "calendar": {"weekly_minutes": [240, 240, 240, 240, 240, 360, 360], "overrides": {}}}
    in_progress = {}
    stamp = today.isoformat() + "T07:00:00"
    for key, label, material_id, stages in courses:
        sid = "subject_demo_" + key
        subject, material = subjects.get(sid), materials.get(material_id)
        if not subject or subject.get("name") != label or not material or material.get("subject_id") != sid:
            raise ValueError("Consultant master schema differs; sample classification cannot be verified")
        previous = None
        for index, (title, first, final, status, completed, total, unit, minutes) in enumerate(stages):
            rid = identifier("rm", f"{key}-{index}")
            start, end = [(today + timedelta(days=n)).isoformat() for n in (first, final)]
            row = {"id": rid, "title": title, "start_date": start, "end_date": end,
                   "baseline_start_date": start, "baseline_end_date": end, "schedule_history": [],
                   "deadline": end, "planned_minutes": minutes, "status": status,
                   "progress_kind": "quantity", "completed": completed, "total": total, "unit": unit,
                   "project_id": subject["project_id"], "subject_id": sid, "material_id": material_id, "unit_id": "",
                   "memo": "架空の表示確認用計画です。進捗はサンプル値で、学習時間から計算していません。",
                   "is_archived": False, "predecessor_ids": [previous] if previous else [],
                   "created_at": stamp, "updated_at": stamp}
            data["items"].append(row)
            if status == "in_progress":
                in_progress[sid] = row
            previous = rid
    for key, title, offset in [("mini", "科目別ミニ模試（架空デモ）", 18), ("full", "模擬試験（デモ）", 30)]:
        data["milestones"].append({"id": identifier("ms", key), "title": title,
            "date": (today + timedelta(days=offset)).isoformat(), "project_id": "project_demo_consultant",
            "memo": "実際の試験日程ではない、表示確認用の架空の節目です。", "is_archived": False,
            "created_at": stamp, "updated_at": stamp})
    return data, in_progress


def augment(target, end_date, source_root=None):
    root = safe_target(target, source_root)
    marker = inspect_dataset(root, end_date)
    if marker.get("roadmap_sample", {}).get("generator") == AUGMENTATION:
        return {"already_present": True, **marker["roadmap_sample"]}
    roadmap_path = root / "settings/study_roadmap.json"
    if roadmap_path.exists():
        raise ValueError("An unknown roadmap already exists; refusing to overwrite")
    master = json.loads((root / "master_data/master.json").read_text(encoding="utf-8"))
    roadmap, in_progress = build_roadmap(master, end_date)
    originals, changes, linked_existing = {}, {}, 0
    plans = {}
    for name in sorted(marker["files"]):
        if name.startswith("daily_plans/"):
            plans[name] = json.loads((root / name).read_text(encoding="utf-8"))
    used_subjects = set()

    def link(plan, task, row):
        allocation_id = identifier("ra", plan["date"] + ":" + task["task_id"])
        if task.get("roadmap_item_id") or task.get("roadmap_allocation_id"):
            raise ValueError("A task has an unknown existing roadmap link")
        task.update(roadmap_item_id=row["id"], roadmap_allocation_id=allocation_id)
        roadmap["allocations"].append({"id": allocation_id, "item_id": row["id"], "task_date": plan["date"],
            "task_id": task["task_id"], "is_active": True,
            "task_keys": [{"date": plan["date"], "task_id": task["task_id"]}],
            "created_at": end_date.isoformat() + "T07:00:00"})

    # Attach up to one recent, already recorded task per selected subject.
    # Existing task/session identities and their original classifications stay intact.
    for name in sorted(plans, reverse=True):
        plan = plans[name]
        day = date.fromisoformat(plan["date"])
        if day >= end_date or day < end_date - timedelta(days=7):
            continue
        for task in plan.get("tasks", []):
            sid = task.get("subject_id")
            row = in_progress.get(sid)
            if sid in used_subjects or not row or not task.get("linked_session_ids"):
                continue
            if not row["start_date"] <= plan["date"] <= row["end_date"]:
                continue
            link(plan, task, row)
            used_subjects.add(sid)
            changes[name] = json_bytes(plan)
            linked_existing += 1

    # Visible "today" examples and one future allocation demonstrate the full
    # stage -> daily task -> timer path without altering historical session data.
    names = {row["subject_id"]: row["name"] for row in master["subjects"]}
    mat_names = {row["material_id"]: row["name"] for row in master["materials"]}
    for key, offset, minutes, title in [
        ("finance", 0, 45, "財務会計：過去問を5問解く（デモ工程）"),
        ("strategy", 0, 30, "企業経営理論：間違えた論点を復習（デモ工程）"),
        ("operations", 1, 45, "運営管理：過去問を5問解く（デモ工程）"),
    ]:
        day = end_date + timedelta(days=offset)
        name = f"daily_plans/{day:%Y/%m/%Y%m%d}.json"
        stamp = day.isoformat() + "T07:30:00"
        plan = plans.setdefault(name, {"plan_id": f"plan_{day:%Y%m%d}", "date": day.isoformat(),
            "target_total_minutes": 180, "focus_subjects": [], "materials": [], "tasks": [],
            "review_note_ids": [], "review_card_ids": [], "created_at": stamp, "updated_at": stamp})
        row = in_progress["subject_demo_" + key]
        task = {"task_id": f"task_demo_roadmap_{day:%Y%m%d}_{key}", "title": title,
            "subject": names[row["subject_id"]], "material": mat_names[row["material_id"]],
            "planned_minutes": minutes, "status": "not_started", "linked_note_ids": [], "linked_session_ids": [],
            **{k: row[k] for k in ("project_id", "subject_id", "material_id", "unit_id")}}
        if any(t["task_id"] == task["task_id"] for t in plan["tasks"]):
            raise ValueError("Sample task already exists without an augmentation marker")
        plan["tasks"].append(task)
        plan["focus_subjects"] = sorted(set(plan.get("focus_subjects", [])) | {task["subject"]})
        plan["materials"] = sorted(set(plan.get("materials", [])) | {task["material"]})
        plan["target_total_minutes"] = max(plan.get("target_total_minutes", 0), sum(t.get("planned_minutes", 0) for t in plan["tasks"]))
        link(plan, task, row)
        changes[name] = json_bytes(plan)

    # Validate with the actual packaged schema, not a duplicate schema here.
    sys.path.insert(0, str(root))
    from app_core.roadmap_service import validate_roadmap
    from app_core.roadmap_planning import dependency_violations
    import config
    if Path(config.PROJECT_ROOT).resolve() != root:
        raise RuntimeError("Validation imports escaped the selected synthetic package")
    validate_roadmap(roadmap)
    if dependency_violations(roadmap):
        raise ValueError("The demonstration contains an invalid prerequisite schedule")
    changes["settings/study_roadmap.json"] = json_bytes(roadmap)
    summary = {"generator": AUGMENTATION, "reference_date": end_date.isoformat(),
               "subjects": 3, "items": len(roadmap["items"]), "milestones": len(roadmap["milestones"]),
               "allocations": len(roadmap["allocations"]), "linked_existing_tasks": linked_existing,
               "new_tasks": 3}
    marker = deepcopy(marker)
    marker["roadmap_sample"] = summary
    for name, data in changes.items():
        marker["files"][name] = hashlib.sha256(data).hexdigest()
    changes[MANIFEST] = json_bytes(marker)
    # Everything is preflighted and validated before any data write. Restore
    # the exact fresh bytes if a normal I/O error interrupts this build step.
    for name in changes:
        path = root / name
        if path.resolve().is_relative_to(root) is False:
            raise ValueError("Output escaped the dedicated synthetic target")
        originals[name] = path.read_bytes() if path.exists() else None
    try:
        for name, data in changes.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    except BaseException:
        for name, data in originals.items():
            path = root / name
            if data is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(data)
        raise
    inspect_dataset(root, end_date)
    return {"already_present": False, **summary}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-app", required=True, type=Path)
    parser.add_argument("--reference-date", default="2026-11-05", type=date.fromisoformat)
    parser.add_argument("--source-root", type=Path, default=SOURCE_ROOT, help="Source checkout to protect from writes")
    args = parser.parse_args()
    print(json.dumps(augment(args.target_app, args.reference_date, args.source_root), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
