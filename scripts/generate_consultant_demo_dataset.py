from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import stat
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any


SOURCE_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_NAME = ".consultant_demo_manifest.json"
GENERATOR_ID = "PROLOAG consultant synthetic dataset"
DATA_FOLDERS = (
    "study_logs", "daily_plans", "diary_entries", "health_logs", "exercise_logs",
    "statistics_practice", "notes_data", "master_data", "settings", "backups",
    "logs", "integrity_check",
)
DATA_FILES = ("flashcards.json", "reminders.json")
EMPTY_SCAFFOLD = {
    "notes_data/notebooks.json": {"notebooks": []},
    "flashcards.json": [],
    "reminders.json": [],
    "master_data/master.json": {"projects": [], "subjects": [], "materials": [], "units": [], "tags": []},
}


def _reject_link(path: Path) -> None:
    if path.is_symlink() or (path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError(f"Demo targets must not contain symbolic links or junctions: {path}")


def validate_demo_root(root: Path) -> Path:
    """Only permit a separate consultant demo package's app directory."""
    candidate = Path(os.path.abspath(root))
    resolved = candidate.resolve()
    if resolved == SOURCE_ROOT or resolved in SOURCE_ROOT.parents:
        raise ValueError("The source project and its ancestors cannot be demo targets.")
    if candidate.name.lower() != "app" or not all(word in candidate.parent.name.lower() for word in ("demo", "consultant")):
        raise ValueError("Use a dedicated consultant_demo package's app directory.")
    for path in (candidate, *candidate.parents):
        _reject_link(path)
    if not (resolved / "main.py").is_file():
        raise FileNotFoundError(f"main.py was not found under target root: {resolved}")
    _reject_link(resolved / "main.py")
    return resolved


def _data_paths(root: Path) -> list[Path]:
    """Preflight every candidate before writing or deleting any demo data."""
    files: list[Path] = []
    pending = [root / name for name in (*DATA_FOLDERS, *DATA_FILES, MANIFEST_NAME)]
    while pending:
        path = pending.pop()
        _reject_link(path)
        resolved = path.resolve()
        if resolved == root or not resolved.is_relative_to(root):
            raise ValueError(f"Demo data path escapes its target: {path}")
        if path.is_dir():
            pending.extend(path.iterdir())
        elif path.exists():
            files.append(path)
    return sorted(files)


def _prepare_demo_target(root: Path, force: bool) -> None:
    paths = _data_paths(root)
    manifest_path = root / MANIFEST_NAME
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("generator") != GENERATOR_ID:
            raise ValueError("The existing demo marker does not identify this generator.")
        if not force:
            raise FileExistsError("Demo data already exists. Use --force only to regenerate this demo.")
        clear_demo_data(root)
        return
    # Fresh portable packages have four known, empty JSON placeholders.
    # --force never grants permission to erase unknown, unmarked records.
    for path in paths:
        relative = path.relative_to(root).as_posix()
        if relative not in EMPTY_SCAFFOLD or json.loads(path.read_text(encoding="utf-8-sig")) != EMPTY_SCAFFOLD[relative]:
            raise ValueError(f"Refusing to overwrite unmarked data: {path}")


MOODS = ["良好", "普通", "低調", "集中", "安定"]
WEATHERS = ["晴れ", "曇り", "雨", "雪"]
EXERCISE_TYPES = ["running", "walking", "strength", "stretch"]

PROJECTS = [
    {
        "project_id": "project_demo_consultant",
        "name": "中小企業診断士",
        "description": "中小企業診断士試験の学習プロジェクト",
    },
    {
        "project_id": "project_demo_business_english",
        "name": "ビジネス英語",
        "description": "英文資料読解とビジネス表現の補助学習",
    },
]

SUBJECTS = [
    {
        "subject_id": "subject_demo_strategy",
        "project_id": "project_demo_consultant",
        "name": "企業経営理論",
        "material_id": "material_demo_strategy_text",
        "material_name": "企業経営理論 スピードテキスト",
        "material_type": "参考書",
        "category": "資格",
        "units": ["経営戦略", "競争戦略", "組織論", "人的資源管理", "マーケティング"],
        "weight": 0.13,
    },
    {
        "subject_id": "subject_demo_finance",
        "project_id": "project_demo_consultant",
        "name": "財務・会計",
        "material_id": "material_demo_finance_workbook",
        "material_name": "財務・会計 過去問完全マスター",
        "material_type": "問題集",
        "category": "資格",
        "units": ["財務諸表", "CVP分析", "投資評価", "キャッシュフロー", "原価計算"],
        "weight": 0.14,
    },
    {
        "subject_id": "subject_demo_operations",
        "project_id": "project_demo_consultant",
        "name": "運営管理",
        "material_id": "material_demo_operations_text",
        "material_name": "運営管理 論点整理ノート",
        "material_type": "自作資料",
        "category": "資格",
        "units": ["生産管理", "IE", "品質管理", "店舗管理", "物流管理"],
        "weight": 0.10,
    },
    {
        "subject_id": "subject_demo_economics",
        "project_id": "project_demo_consultant",
        "name": "経済学・経済政策",
        "material_id": "material_demo_economics_workbook",
        "material_name": "経済学 計算問題トレーニング",
        "material_type": "問題集",
        "category": "資格",
        "units": ["需要供給", "IS-LM分析", "AD-AS分析", "ゲーム理論", "外部性"],
        "weight": 0.09,
    },
    {
        "subject_id": "subject_demo_information",
        "project_id": "project_demo_consultant",
        "name": "経営情報システム",
        "material_id": "material_demo_information_text",
        "material_name": "情報システム 頻出論点集",
        "material_type": "参考書",
        "category": "資格",
        "units": ["データベース", "ネットワーク", "セキュリティ", "システム開発", "IT経営"],
        "weight": 0.08,
    },
    {
        "subject_id": "subject_demo_legal",
        "project_id": "project_demo_consultant",
        "name": "経営法務",
        "material_id": "material_demo_legal_workbook",
        "material_name": "経営法務 一問一答",
        "material_type": "問題集",
        "category": "資格",
        "units": ["会社法", "知的財産権", "民法", "組織再編", "英文契約"],
        "weight": 0.08,
    },
    {
        "subject_id": "subject_demo_policy",
        "project_id": "project_demo_consultant",
        "name": "中小企業経営・政策",
        "material_id": "material_demo_policy_whitepaper",
        "material_name": "中小企業白書・施策整理",
        "material_type": "白書",
        "category": "資格",
        "units": ["白書統計", "小規模企業政策", "補助金制度", "創業支援", "事業承継"],
        "weight": 0.08,
    },
    {
        "subject_id": "subject_demo_case1",
        "project_id": "project_demo_consultant",
        "name": "二次試験 事例I",
        "material_id": "material_demo_case1_answers",
        "material_name": "ふぞろいな合格答案 事例I",
        "material_type": "過去問",
        "category": "資格",
        "units": ["組織構造", "人事施策", "強み弱み分析", "権限委譲", "後継者育成"],
        "weight": 0.08,
    },
    {
        "subject_id": "subject_demo_case2",
        "project_id": "project_demo_consultant",
        "name": "二次試験 事例II",
        "material_id": "material_demo_case2_answers",
        "material_name": "ふぞろいな合格答案 事例II",
        "material_type": "過去問",
        "category": "資格",
        "units": ["顧客分析", "差別化戦略", "販促施策", "地域連携", "ブランド設計"],
        "weight": 0.08,
    },
    {
        "subject_id": "subject_demo_case3",
        "project_id": "project_demo_consultant",
        "name": "二次試験 事例III",
        "material_id": "material_demo_case3_answers",
        "material_name": "ふぞろいな合格答案 事例III",
        "material_type": "過去問",
        "category": "資格",
        "units": ["生産計画", "ボトルネック", "在庫管理", "標準化", "外注管理"],
        "weight": 0.08,
    },
    {
        "subject_id": "subject_demo_case4",
        "project_id": "project_demo_consultant",
        "name": "二次試験 事例IV",
        "material_id": "material_demo_case4_workbook",
        "material_name": "事例IV 計算問題集",
        "material_type": "問題集",
        "category": "資格",
        "units": ["経営分析", "CVP分析", "NPV", "意思決定会計", "キャッシュフロー"],
        "weight": 0.10,
    },
    {
        "subject_id": "subject_demo_business_english",
        "project_id": "project_demo_business_english",
        "name": "ビジネス英語",
        "material_id": "material_demo_business_article",
        "material_name": "海外ビジネス記事読解",
        "material_type": "論文",
        "category": "英語",
        "units": ["要約練習", "財務表現", "マーケティング表現", "交渉表現"],
        "weight": 0.04,
    },
]

TAG_NAMES = ["要復習", "頻出", "理解不十分", "二次試験", "財務強化", "暗記", "ケース演習", "白書確認"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate 中小企業診断士 demo data for PROLOAG.")
    parser.add_argument("--target-root", required=True, help="Path to the app root that contains main.py.")
    parser.add_argument("--days", type=int, default=180, help="Number of days to generate.")
    parser.add_argument("--end-date", default=date.today().isoformat(), help="Last date in YYYY-MM-DD.")
    parser.add_argument("--seed", type=int, default=20260525, help="Random seed.")
    parser.add_argument("--force", action="store_true", help="Clear demo data folders before writing.")
    return parser.parse_args()


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def daily_path(root: Path, folder: str, target: date) -> Path:
    return root / folder / f"{target:%Y}" / f"{target:%m}" / f"{target:%Y%m%d}.json"


def timestamp(target: date, hour: int = 20, minute: int = 0) -> str:
    return datetime.combine(target, time(hour, minute)).strftime("%Y-%m-%d %H:%M:%S")


def iso_dt(target: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(target, time(hour, minute))


def clamp(value: float, low: int, high: int) -> int:
    return int(max(low, min(high, round(value))))


def demo_good_seat_value(health: dict[str, Any], rng: random.Random) -> str | None:
    """Create a four-state seat record with a weak, non-causal outcome signal."""
    if rng.random() < 0.18:
        return None
    condition = health.get("condition", {})
    focus = float(condition.get("focus_score", 3) or 3)
    fatigue = float(condition.get("fatigue", 3) or 3)
    yes_weight = max(0.20, min(0.70, 0.42 + (focus - 3) * 0.08 - (fatigue - 3) * 0.05))
    no_weight = max(0.12, min(0.48, 0.26 - (focus - 3) * 0.05 + (fatigue - 3) * 0.04))
    so_so_weight = max(0.18, 1.0 - yes_weight - no_weight)
    return rng.choices(
        ["yes", "so_so", "no"],
        weights=[yes_weight, so_so_weight, no_weight],
        k=1,
    )[0]


def subject_by_id(subject_id: str) -> dict[str, Any]:
    return next(item for item in SUBJECTS if item["subject_id"] == subject_id)


def weighted_subject(rng: random.Random, day_index: int) -> dict[str, Any]:
    weights = [item["weight"] for item in SUBJECTS]
    if day_index > 120:
        weights = [
            weight * (1.35 if item["name"].startswith("二次試験") or item["name"] == "財務・会計" else 0.90)
            for item, weight in zip(SUBJECTS, weights)
        ]
    return rng.choices(SUBJECTS, weights=weights, k=1)[0]


def slug(value: str) -> str:
    table = {
        "経営戦略": "strategy",
        "競争戦略": "competition",
        "組織論": "organization",
        "人的資源管理": "hrm",
        "マーケティング": "marketing",
        "財務諸表": "statements",
        "CVP分析": "cvp",
        "投資評価": "investment",
        "キャッシュフロー": "cashflow",
        "原価計算": "costing",
        "生産管理": "production",
        "IE": "ie",
        "品質管理": "quality",
        "店舗管理": "store",
        "物流管理": "logistics",
        "需要供給": "supply_demand",
        "IS-LM分析": "islm",
        "AD-AS分析": "adas",
        "ゲーム理論": "game_theory",
        "外部性": "externality",
        "データベース": "database",
        "ネットワーク": "network",
        "セキュリティ": "security",
        "システム開発": "development",
        "IT経営": "it_management",
        "会社法": "company_law",
        "知的財産権": "ip",
        "民法": "civil_law",
        "組織再編": "reorganization",
        "英文契約": "english_contract",
        "白書統計": "whitepaper_stats",
        "小規模企業政策": "small_business_policy",
        "補助金制度": "subsidy",
        "創業支援": "startup_support",
        "事業承継": "succession",
        "組織構造": "org_structure",
        "人事施策": "hr_policy",
        "強み弱み分析": "swot",
        "権限委譲": "delegation",
        "後継者育成": "successor",
        "顧客分析": "customer",
        "差別化戦略": "differentiation",
        "販促施策": "promotion",
        "地域連携": "local_collab",
        "ブランド設計": "brand",
        "生産計画": "production_plan",
        "ボトルネック": "bottleneck",
        "在庫管理": "inventory",
        "標準化": "standardization",
        "外注管理": "outsourcing",
        "経営分析": "financial_analysis",
        "NPV": "npv",
        "意思決定会計": "decision_accounting",
        "要約練習": "summary",
        "財務表現": "finance_terms",
        "マーケティング表現": "marketing_terms",
        "交渉表現": "negotiation",
    }
    return table.get(value, f"unit_{abs(sum(ord(ch) for ch in value))}")


def unit_id(subject: dict[str, Any], unit_name: str) -> str:
    subject_slug = subject["subject_id"].replace("subject_demo_", "")
    return f"unit_demo_{subject_slug}_{slug(unit_name)}"


def build_master_data(reference_date: date | None = None) -> dict[str, Any]:
    now = timestamp(reference_date or date.today())
    projects = [
        {
            "project_id": item["project_id"],
            "name": item["name"],
            "description": item["description"],
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        }
        for item in PROJECTS
    ]
    subjects = []
    materials = []
    units = []
    for subject in SUBJECTS:
        subjects.append(
            {
                "subject_id": subject["subject_id"],
                "project_id": subject["project_id"],
                "name": subject["name"],
                "description": "",
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            }
        )
        materials.append(
            {
                "material_id": subject["material_id"],
                "project_id": subject["project_id"],
                "subject_id": subject["subject_id"],
                "name": subject["material_name"],
                "type": subject["material_type"],
                "description": "",
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            }
        )
        for unit_name in subject["units"]:
            units.append(
                {
                    "unit_id": unit_id(subject, unit_name),
                    "project_id": subject["project_id"],
                    "subject_id": subject["subject_id"],
                    "material_id": subject["material_id"],
                    "name": unit_name,
                    "parent_unit_id": None,
                    "description": "",
                    "is_active": True,
                    "created_at": now,
                    "updated_at": now,
                }
            )
    tags = [
        {"tag_id": f"tag_demo_{idx:03d}", "name": name, "description": "", "is_active": True, "created_at": now, "updated_at": now}
        for idx, name in enumerate(TAG_NAMES, start=1)
    ]
    return {"projects": projects, "subjects": subjects, "materials": materials, "units": units, "tags": tags}


def clear_demo_data(root: Path) -> None:
    root = validate_demo_root(root)
    _data_paths(root)
    marker = root / MANIFEST_NAME
    marker_value = json.loads(marker.read_text(encoding="utf-8")) if marker.is_file() else None
    if not isinstance(marker_value, dict) or marker_value.get("generator") != GENERATOR_ID:
        raise ValueError("Only a previously generated, marked demo may be cleared.")
    for name in DATA_FOLDERS:
        path = root / name
        if path.exists():
            shutil.rmtree(path)
    for name in (*DATA_FILES, MANIFEST_NAME):
        path = root / name
        if path.exists():
            path.unlink()


def sleep_times(duration_min: int, rng: random.Random) -> tuple[str, str]:
    bed_minutes = 23 * 60 + rng.randint(-45, 80)
    wake_minutes = bed_minutes + duration_min
    return f"{(bed_minutes // 60) % 24:02d}:{bed_minutes % 60:02d}", f"{(wake_minutes // 60) % 24:02d}:{wake_minutes % 60:02d}"


def build_health(target: date, day_index: int, rng: random.Random) -> dict[str, Any]:
    weekday = target.weekday()
    exam_pressure = 1 if day_index > 130 else 0
    duration = 420 + (25 if weekday >= 5 else 0) - exam_pressure * 20 + rng.randint(-60, 45)
    if rng.random() < 0.08:
        duration -= rng.randint(35, 80)
    duration = max(300, min(540, duration))
    bed_time, wake_time = sleep_times(duration, rng)
    sleep_quality = clamp(3 + (duration - 390) / 90 + rng.uniform(-0.7, 0.6), 1, 5)
    stress = clamp(2.5 + exam_pressure * 0.8 + rng.uniform(-0.6, 0.9), 1, 5)
    fatigue = clamp(2.8 + (390 - duration) / 95 + stress * 0.16 + rng.uniform(-0.8, 0.8), 1, 5)
    focus = clamp(3.1 + (duration - 390) / 110 - (fatigue - 3) * 0.35 + rng.uniform(-0.5, 0.7), 1, 5)
    physical = clamp(3.4 - (fatigue - 3) * 0.25 + rng.uniform(-0.5, 0.5), 1, 5)
    mood = clamp(3.2 - (stress - 3) * 0.25 + rng.uniform(-0.5, 0.6), 1, 5)
    return {
        "date": target.isoformat(),
        "sleep": {
            "bed_time": bed_time,
            "wake_time": wake_time,
            "sleep_duration_min": duration,
            "sleep_quality": sleep_quality,
            "morning_sleepiness": clamp(3.5 - sleep_quality + rng.uniform(-0.4, 0.8), 1, 5),
            "midnight_awakenings": rng.choices([0, 1, 2, 3], weights=[0.60, 0.27, 0.10, 0.03], k=1)[0],
            "nap_minutes": rng.choice([0, 0, 0, 15, 20, 30]) if weekday >= 5 else rng.choice([0, 0, 0, 10]),
        },
        "condition": {
            "physical_score": physical,
            "fatigue": fatigue,
            "stress": stress,
            "focus_score": focus,
            "mood_score": mood,
            "memo": "デモデータ: 学習負荷とコンディションの推移確認用です。",
        },
        "created_at": timestamp(target, 22, 0),
        "updated_at": timestamp(target, 22, 0),
    }


def build_exercise(target: date, rng: random.Random) -> dict[str, Any] | None:
    if rng.random() > (0.31 if target.weekday() >= 5 else 0.17):
        return None
    exercise_type = rng.choices(EXERCISE_TYPES, weights=[0.32, 0.36, 0.20, 0.12], k=1)[0]
    duration = rng.randint(18, 75)
    distance = 0.0
    if exercise_type == "running":
        distance = round(duration / rng.uniform(5.4, 7.2), 2)
    elif exercise_type == "walking":
        distance = round(duration / rng.uniform(10.5, 14.5), 2)
    pace = round(duration / distance, 2) if distance else 0.0
    label = {"running": "ランニング", "walking": "ウォーキング", "strength": "筋トレ", "stretch": "ストレッチ"}[exercise_type]
    record = {
        "exercise_id": f"exercise_{target:%Y%m%d}_001",
        "type": exercise_type,
        "title": f"{label} 記録",
        "start_time": rng.choice(["06:45", "07:00", "18:30", "20:00"]),
        "duration_min": duration,
        "distance_km": distance,
        "pace_min_per_km": pace,
        "intensity": rng.randint(3, 8),
        "fatigue_after": rng.randint(2, 5),
        "memo": "デモデータ: 生活リズム確認用の運動記録です。",
        "created_at": timestamp(target, 21, 30),
        "updated_at": timestamp(target, 21, 30),
    }
    return {"date": target.isoformat(), "records": [record], "created_at": timestamp(target, 21, 30), "updated_at": timestamp(target, 21, 30)}


def plan_for_day(target: date, day_index: int, rng: random.Random) -> dict[str, Any]:
    task_count = rng.choices([2, 3, 4, 5], weights=[0.20, 0.45, 0.27, 0.08], k=1)[0]
    tasks = []
    selected_subjects = []
    selected_materials = []
    target_total = rng.choice([120, 150, 180, 210, 240]) + (30 if target.weekday() >= 5 else 0)
    for idx in range(1, task_count + 1):
        subject = weighted_subject(rng, day_index)
        unit_name = rng.choice(subject["units"])
        planned = rng.choice([30, 45, 60, 75, 90])
        title_suffix = rng.choice(["論点整理", "過去問演習", "復習", "答案骨子作成", "一問一答"])
        task_id = f"task_{target:%Y%m%d}_{idx:03d}"
        task = {
            "task_id": task_id,
            "title": f"{subject['name']}：{unit_name} {title_suffix}",
            "subject": subject["name"],
            "material": subject["material_name"],
            "planned_minutes": planned,
            "status": "not_started",
            "linked_note_ids": [],
            "linked_session_ids": [],
            "project_id": subject["project_id"],
            "subject_id": subject["subject_id"],
            "material_id": subject["material_id"],
            "unit_id": unit_id(subject, unit_name),
        }
        tasks.append(task)
        selected_subjects.append(subject["name"])
        selected_materials.append(subject["material_name"])
    return {
        "plan_id": f"plan_{target:%Y%m%d}",
        "date": target.isoformat(),
        "target_total_minutes": target_total,
        "focus_subjects": sorted(set(selected_subjects))[:4],
        "materials": sorted(set(selected_materials))[:4],
        "tasks": tasks,
        "review_note_ids": [],
        "review_card_ids": [],
        "created_at": timestamp(target, 7, 30),
        "updated_at": timestamp(target, 7, 30),
    }


def build_sessions_for_day(target: date, day_index: int, plan: dict[str, Any], health: dict[str, Any], rng: random.Random) -> list[dict[str, Any]]:
    focus = int(health["condition"]["focus_score"])
    weekday = target.weekday()
    base_probability = 0.72 if weekday < 5 else 0.82
    if focus >= 4:
        base_probability += 0.10
    if focus <= 2:
        base_probability -= 0.18
    if rng.random() > base_probability:
        return []
    session_count = rng.choices([1, 2, 3, 4], weights=[0.34, 0.42, 0.19, 0.05], k=1)[0]
    sessions = []
    task_pool = list(plan.get("tasks", []))
    start_hour = rng.choice([5, 6, 7, 20, 21]) if weekday < 5 else rng.choice([8, 9, 10, 14, 20])
    # Reserve enough room for every session, keeping records ordered and on
    # their own day rather than wrapping a late evening session to morning.
    latest_start_hour = 23 - (session_count * 115 + (session_count - 1) * 30 + 59) // 60
    cursor = iso_dt(target, min(start_hour, latest_start_hour), rng.choice([0, 10, 20, 30]))
    hourly_rate = 2200
    for idx in range(1, session_count + 1):
        task = rng.choice(task_pool) if task_pool else None
        subject = subject_by_id(task["subject_id"]) if task else weighted_subject(rng, day_index)
        unit_name = next((unit for unit in subject["units"] if unit_id(subject, unit) == (task or {}).get("unit_id")), rng.choice(subject["units"]))
        duration_min = rng.choice([30, 40, 45, 50, 60, 75, 90, 105]) + (10 if focus >= 4 and rng.random() < 0.35 else 0)
        start_dt = cursor
        end_dt = start_dt + timedelta(minutes=duration_min)
        cursor = end_dt + timedelta(minutes=rng.choice([10, 20, 30]))
        duration_sec = duration_min * 60
        money = round(duration_sec / 3600 * hourly_rate)
        session_id = f"session_{target:%Y%m%d}_{idx:03d}"
        memo = f"{subject['name']}：{unit_name}の{rng.choice(['論点整理', '過去問演習', '弱点補強', '答案構成練習'])}"
        session = {
            "session_id": session_id,
            "start_at": start_dt.isoformat(timespec="seconds"),
            "end_at": end_dt.isoformat(timespec="seconds"),
            "duration_sec": duration_sec,
            "money": money,
            "normal_money": money,
            "overtime_money": 0,
            "hourly_rate": hourly_rate,
            "memo": memo,
            "category": subject["category"],
            "environment": rng.choice(["自宅", "カフェ", "図書館", "通勤中"]),
            "effort": rng.randint(5, 10),
            "sleep_hours": round(health["sleep"]["sleep_duration_min"] / 60, 1),
            "alcohol_yesterday": rng.choice(["なし", "なし", "なし", "あり"]),
            "mood": str(health["condition"]["mood_score"]),
            "study_category": subject["category"],
            "input_output": rng.choice(["インプット", "アウトプット", "両方"]),
            "linked_note_ids": [],
            "linked_task_id": task["task_id"] if task else "",
            "project_id": subject["project_id"],
            "subject_id": subject["subject_id"],
            "material_id": subject["material_id"],
            "unit_id": unit_id(subject, unit_name),
        }
        session["io_type"] = session["input_output"]
        session["condition"] = {
            key: session[key] for key in (
                "effort", "environment", "sleep_hours", "mood", "category",
                "input_output", "io_type", "alcohol_yesterday",
            )
        }
        sessions.append(session)
        if task:
            task.setdefault("linked_session_ids", []).append(session_id)
            actual = sum(item["duration_sec"] for item in sessions if item.get("linked_task_id") == task["task_id"]) / 60
            if actual >= task["planned_minutes"] * 0.8:
                task["status"] = "done"
            elif actual > 0:
                task["status"] = rng.choice(["in_progress", "done"])
    for task in plan.get("tasks", []):
        if task.get("status") == "not_started" and rng.random() < 0.12:
            task["status"] = "pending"
    return sessions


def build_diary(
    target: date,
    rng: random.Random,
    sessions: list[dict[str, Any]],
    exercise: dict[str, Any] | None,
    health: dict[str, Any],
    *,
    include_substances: bool = False,
    profiles: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if not sessions and not exercise and not include_substances and rng.random() > 0.25:
        return None
    total_minutes = sum(int(item.get("duration_sec", 0)) for item in sessions) // 60
    title = f"{target:%Y-%m-%d} 学習メモ"
    body = (
        f"中小企業診断士学習のデモ日記です。学習時間は約{total_minutes}分。"
        "主要論点の定着状況と生活リズムを後から確認できるように記録しています。"
    )
    exercise_records = exercise.get("records", []) if isinstance(exercise, dict) else []
    running_records = [
        record
        for record in exercise_records
        if isinstance(record, dict) and record.get("type") == "running"
    ]
    running_distance_km = sum(float(record.get("distance_km", 0) or 0) for record in running_records)
    running_time_min = sum(float(record.get("duration_min", 0) or 0) for record in running_records)
    costs = []
    if rng.random() < 0.35:
        costs.append({"name": rng.choice(["参考書", "模試", "自習室", "オンライン講座"]), "amount": rng.choice([500, 1200, 1800, 2500, 3500]), "source": "demo"})
    caffeine = rng.choice([0, 60, 90, 120, 160, 200])
    first_amount = min(caffeine, rng.choice([60, 80, 100]))
    caffeine_amounts = [amount for amount in (first_amount, caffeine - first_amount) if amount > 0]
    caffeine_times = [(rng.choice([6, 7, 8, 9]), rng.choice([0, 15, 30, 45])), (rng.choice([13, 14, 15, 16]), rng.choice([0, 15, 30, 45]))]
    caffeine_records = []
    for index, amount in enumerate(caffeine_amounts):
        hour, minute = caffeine_times[index]
        taken_at = datetime.combine(target, time(hour, minute))
        caffeine_records.append(
            {
                "caffeine_id": f"caffeine_demo_consultant_{target:%Y%m%d}_{index + 1:02d}",
                "taken_at": taken_at.isoformat(timespec="seconds"),
                "amount_mg": amount,
                "source": "demo_generator",
                "label": rng.choice(["コーヒー", "お茶"]),
                "memo": "デモ用の時刻付き摂取記録",
                "created_at": timestamp(target, 22, 10),
                "updated_at": timestamp(target, 22, 10),
            }
        )
    profile_rows = profiles if profiles is not None else _default_profiles()
    caffeine_profile = next((item for item in profile_rows if item["substance_id"] == "caffeine"), None)
    if caffeine_profile:
        for item in caffeine_records:
            item["model_snapshot"] = _model_snapshot(caffeine_profile)
    substances = [
        {
            **{key: value for key, value in item.items() if key != "caffeine_id"},
            "substance_record_id": item["caffeine_id"],
            "substance_id": "caffeine", "unit": "mg",
        }
        for item in caffeine_records
    ]
    if include_substances:
        # Independent synthetic records to demonstrate the four-panel chart;
        # neither a real person's medication history nor a suggested regimen.
        for index, profile in enumerate(profile_rows):
            if profile["substance_id"] == "caffeine":
                continue
            taken_at = iso_dt(target, 22, index * 10)
            substances.append({
                "substance_record_id": f"{profile['substance_id']}_demo_{target:%Y%m%d}",
                "substance_id": profile["substance_id"],
                "taken_at": taken_at.isoformat(timespec="seconds"),
                "amount_mg": profile["default_amount_mg"], "unit": "mg",
                "source": "demo_generator", "label": profile["default_label"],
                "memo": "完全に架空の表示テスト用記録。実際の服薬・併用例ではありません。",
                "model_snapshot": _model_snapshot(profile),
                "created_at": timestamp(target, 23, 40), "updated_at": timestamp(target, 23, 40),
            })
    return {
        "date": target.isoformat(),
        "title": title,
        "body": body,
        "content": body,
        "mood": rng.choice(MOODS),
        "weather": rng.choice(WEATHERS),
        "good_seat_status": demo_good_seat_value(health, rng),
        "caffeine_mg": caffeine,
        "caffeine_untimed_mg": 0,
        "caffeine_records": caffeine_records,
        "substance_records_schema_version": 1,
        "substance_records": substances,
        "running_distance_km": round(running_distance_km, 3),
        "running_time_min": round(running_time_min, 1),
        "cost_records": costs,
        "created_at": timestamp(target, 22, 10),
        "updated_at": timestamp(target, 22, 10),
    }


def build_notes(source_sessions: list[dict[str, Any]], sessions_by_date: dict[date, list[dict[str, Any]]], rng: random.Random) -> list[dict[str, Any]]:
    selected = source_sessions[:: max(1, len(source_sessions) // 62)] if source_sessions else []
    selected = selected[:62]
    notes: list[dict[str, Any]] = []
    for idx, session in enumerate(selected, start=1):
        created = datetime.fromisoformat(session["start_at"]).date()
        subject = subject_by_id(session["subject_id"])
        unit_name = next((unit for unit in subject["units"] if unit_id(subject, unit) == session["unit_id"]), subject["units"][0])
        note_id = f"note_{created:%Y%m%d}_{idx:03d}"
        related_sessions = [
            item["session_id"]
            for item in sessions_by_date.get(created, [])
            if item.get("subject_id") == session.get("subject_id") and item.get("unit_id") == session.get("unit_id")
        ][:3]
        if session["session_id"] not in related_sessions:
            related_sessions.append(session["session_id"])
        body = (
            "# 学習メモ\n\n"
            f"## テーマ\n{subject['name']} / {unit_name}\n\n"
            "## 理解できたこと\n"
            "- 問われやすい論点と判断基準を整理した。\n"
            "- 過去問で使えるキーワードをカード化する候補として抽出した。\n\n"
            "## 不明点\n"
            "- 似た選択肢で迷いやすいポイントは次回復習で確認する。\n\n"
            "## 次回復習したいこと\n"
            "- 事例問題や計算問題で説明できるか確認する。\n"
        )
        note = {
            "id": note_id,
            "note_id": note_id,
            "title": f"{subject['name']}：{unit_name}の整理",
            "body": body,
            "content": body,
            "tags": rng.sample(TAG_NAMES, k=rng.randint(1, 3)),
            "created_at": timestamp(created, rng.choice([20, 21, 22]), rng.choice([0, 15, 30])),
            "updated_at": timestamp(created, 23, rng.choice([0, 20, 40])),
            "linked_study_session_ids": related_sessions,
            "linked_flashcard_ids": [],
            "linked_reminder_ids": [],
            "project_id": session["project_id"],
            "subject_id": session["subject_id"],
            "material_id": session["material_id"],
            "unit_id": session["unit_id"],
        }
        notes.append(note)
        for target_sessions in sessions_by_date.values():
            for item in target_sessions:
                if item["session_id"] in related_sessions and note_id not in item["linked_note_ids"]:
                    item["linked_note_ids"].append(note_id)
    return notes


def build_note_store(notes: list[dict[str, Any]]) -> dict[str, Any]:
    sections: dict[str, dict[str, Any]] = {}
    for note in notes:
        subject = subject_by_id(note["subject_id"])
        section = sections.setdefault(subject["subject_id"], {"id": f"section_{subject['subject_id']}", "name": subject["name"], "pages": []})
        section["pages"].append(note)
    return {"notebooks": [{"id": "notebook_demo_consultant", "name": "診断士学習ノート", "sections": list(sections.values())}]}


def build_cards(notes: list[dict[str, Any]], rng: random.Random) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for note in notes:
        subject = subject_by_id(note["subject_id"])
        unit_name = next((unit for unit in subject["units"] if unit_id(subject, unit) == note["unit_id"]), subject["units"][0])
        for _ in range(rng.randint(2, 4)):
            card_id = f"card_{note['created_at'][:10].replace('-', '')}_{len(cards) + 1:03d}"
            need_review = rng.random() < 0.40
            question = f"{unit_name}で押さえるべき判断基準を1つ説明してください。"
            answer = f"{subject['name']}の論点として、{unit_name}は設問要求と制約条件を結びつけて整理する。"
            card = {
                "card_id": card_id,
                "flashcard_id": card_id,
                "question": question,
                "answer": answer,
                "front": question,
                "back": answer,
                "explanation": "デモデータ: ノートから作成された復習カードです。",
                "genre": subject["name"],
                "tags": list(dict.fromkeys(note["tags"] + [subject["name"]])),
                "importance": rng.choices(["low", "normal", "high"], weights=[0.15, 0.55, 0.30], k=1)[0],
                "need_review": need_review,
                "important": need_review,
                "source_note_id": note["note_id"],
                "linked_note_ids": [note["note_id"]],
                "project_id": note["project_id"],
                "subject_id": note["subject_id"],
                "material_id": note["material_id"],
                "unit_id": note["unit_id"],
                "created_at": note["created_at"],
                "updated_at": note["updated_at"],
            }
            cards.append(card)
            note["linked_flashcard_ids"].append(card_id)
    return cards


def build_reminders(notes: list[dict[str, Any]], all_dates: list[date], end_date: date, rng: random.Random) -> list[dict[str, Any]]:
    reminders: list[dict[str, Any]] = []
    for note in notes:
        created = date.fromisoformat(note["created_at"][:10])
        for interval in rng.sample([1, 3, 7, 14, 30], k=rng.randint(1, 3)):
            scheduled = created + timedelta(days=interval)
            if scheduled < all_dates[0] or scheduled > end_date + timedelta(days=14):
                continue
            reminder_id = f"reminder_{scheduled:%Y%m%d}_{len(reminders) + 1:03d}"
            status = "done" if scheduled < end_date and rng.random() < 0.66 else "pending"
            reminder = {
                "reminder_id": reminder_id,
                "source_type": "note",
                "source_note_id": note["note_id"],
                "title": f"{note['title']}を復習",
                "keyword": note["title"],
                "memo": "デモデータ: 忘却曲線リマインダーの確認用です。",
                "url": "",
                "review_type": "note_review",
                "priority": rng.choices(["low", "normal", "high"], weights=[0.20, 0.58, 0.22], k=1)[0],
                "status": status,
                "scheduled_date": scheduled.isoformat(),
                "registered": created.isoformat(),
                "linked_note_ids": [note["note_id"]],
                "project_id": note["project_id"],
                "subject_id": note["subject_id"],
                "material_id": note["material_id"],
                "unit_id": note["unit_id"],
                "created_at": note["created_at"],
                "updated_at": timestamp(min(scheduled, end_date), 21, 0),
            }
            reminders.append(reminder)
            note["linked_reminder_ids"].append(reminder_id)
    return reminders


def attach_review_targets_to_plans(plans_by_date: dict[date, dict[str, Any]], reminders: list[dict[str, Any]], cards: list[dict[str, Any]]) -> None:
    card_by_note = defaultdict(list)
    for card in cards:
        if card.get("source_note_id"):
            card_by_note[card["source_note_id"]].append(card["card_id"])
    for reminder in reminders:
        try:
            scheduled = date.fromisoformat(reminder["scheduled_date"])
        except ValueError:
            continue
        plan = plans_by_date.get(scheduled)
        if not plan:
            continue
        note_id = reminder.get("source_note_id")
        if note_id and note_id not in plan["review_note_ids"]:
            plan["review_note_ids"].append(note_id)
        for card_id in card_by_note.get(note_id, [])[:2]:
            if card_id not in plan["review_card_ids"]:
                plan["review_card_ids"].append(card_id)


def _default_profiles() -> list[dict[str, Any]]:
    if str(SOURCE_ROOT) not in sys.path:
        sys.path.insert(0, str(SOURCE_ROOT))
    from app_core.substance_service import default_substance_profiles
    return default_substance_profiles()


def _model_snapshot(profile: dict[str, Any]) -> dict[str, Any]:
    # Import only code defaults/pure helpers; never load personal profiles.
    if str(SOURCE_ROOT) not in sys.path:
        sys.path.insert(0, str(SOURCE_ROOT))
    from app_core.substance_service import model_snapshot_for_profile
    return model_snapshot_for_profile(profile)


def build_dataset(root: Path, days: int, end_date: date, seed: int, force: bool) -> dict[str, int]:
    root = validate_demo_root(root)
    if type(days) is not int or not 1 <= days <= 3650:
        raise ValueError("days must be an integer between 1 and 3650.")
    if not isinstance(end_date, date) or isinstance(end_date, datetime):
        raise ValueError("end_date must be a calendar date.")
    all_dates = [end_date - timedelta(days=days - idx - 1) for idx in range(days)]
    countdown_target = end_date + timedelta(days=30)
    _prepare_demo_target(root, force)

    if str(SOURCE_ROOT) not in sys.path:
        sys.path.insert(0, str(SOURCE_ROOT))
    from app_core.substance_service import PROFILE_SCHEMA_VERSION
    profiles = _default_profiles()

    rng = random.Random(seed)
    write_json(root / "master_data" / "master.json", build_master_data(end_date))
    write_json(root / "settings" / "substance_profiles.json", {"schema_version": PROFILE_SCHEMA_VERSION, "profiles": profiles})

    plans_by_date: dict[date, dict[str, Any]] = {}
    sessions_by_date: dict[date, list[dict[str, Any]]] = {}
    health_by_date: dict[date, dict[str, Any]] = {}
    source_sessions: list[dict[str, Any]] = []
    substance_record_count = 0

    for day_index, target in enumerate(all_dates):
        health = build_health(target, day_index, rng)
        health_by_date[target] = health
        write_json(daily_path(root, "health_logs", target), health)

        exercise = build_exercise(target, rng)
        if exercise:
            write_json(daily_path(root, "exercise_logs", target), exercise)

        plan = plan_for_day(target, day_index, rng)
        plans_by_date[target] = plan
        sessions = build_sessions_for_day(target, day_index, plan, health, rng)
        sessions_by_date[target] = sessions

        diary = build_diary(target, rng, sessions, exercise, health, include_substances=(end_date - target).days < 30, profiles=profiles)
        if diary:
            write_json(daily_path(root, "diary_entries", target), diary)
            substance_record_count += len(diary["substance_records"])

        source_sessions.extend([session for session in sessions if int(session["duration_sec"]) >= 45 * 60])

    notes = build_notes(source_sessions, sessions_by_date, rng)
    cards = build_cards(notes, rng)
    reminders = build_reminders(notes, all_dates, end_date, rng)
    attach_review_targets_to_plans(plans_by_date, reminders, cards)

    for target, sessions in sessions_by_date.items():
        if sessions:
            write_json(daily_path(root, "study_logs", target), sessions)
    for target, plan in plans_by_date.items():
        write_json(daily_path(root, "daily_plans", target), plan)

    write_json(root / "notes_data" / "notebooks.json", build_note_store(notes))
    write_json(root / "flashcards.json", cards)
    write_json(root / "reminders.json", reminders)
    write_json(
        root / "settings" / "user_settings.json",
        {
            "timer": {
                "hourly_rate": 2200,
                "goal_minutes": 180,
                "overtime_enabled": True,
                "overtime_multiplier": 1.25,
                "overtime_threshold_hours": 8.0,
            },
            "diary": {"caffeine_mg": 120, "investment_row_count": 3},
            "health": {
                "bed_time": "23:30",
                "wake_time": "06:30",
                "sleep_quality": 3,
                "fatigue": 3,
                "stress": 3,
                "focus_score": 3,
                "physical_score": 3,
            },
            "exercise": {"type": "running", "duration_min": 0, "distance_km": 0.0, "intensity": 5, "fatigue_after": 3},
            "display": {"hidden_sidebar_pages": [], "show_task_metrics": True, "theme_mode": "standard", "period_label": "直近30日"},
            "countdown": {"enabled": True, "title": "模擬試験（デモ）", "target_date": countdown_target.isoformat()},
        },
    )
    write_json(
        root / "settings" / "demo_mode.json",
        {
            "enabled": True,
            "fixed_today": end_date.isoformat(),
            "demo_name": "中小企業診断士 学習デモ",
        },
    )
    manifest_files = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in _data_paths(root) if path.suffix == ".json" and path.name != MANIFEST_NAME
    }
    write_json(root / MANIFEST_NAME, {
        "schema_version": 1, "generator": GENERATOR_ID,
        "days": days, "end_date": end_date.isoformat(), "seed": seed,
        "files": manifest_files,
    })
    return {
        "days": len(all_dates),
        "study_sessions": sum(len(items) for items in sessions_by_date.values()),
        "plans": len(plans_by_date),
        "health_logs": len(health_by_date),
        "exercise_days": sum(1 for _ in (root / "exercise_logs").rglob("*.json")),
        "diaries": sum(1 for _ in (root / "diary_entries").rglob("*.json")),
        "notes": len(notes),
        "cards": len(cards),
        "reminders": len(reminders),
        "substance_profiles": len(profiles),
        "substance_records": substance_record_count,
        "non_caffeine_substance_records": min(days, 30) * (len(profiles) - 1),
    }


def main() -> None:
    args = parse_args()
    target_root = Path(args.target_root)
    end_date = date.fromisoformat(args.end_date)
    summary = build_dataset(target_root, args.days, end_date, args.seed, args.force)
    print("Consultant demo dataset generated")
    print(f"Target: {target_root}")
    for key, value in summary.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
