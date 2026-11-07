from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time as dt_time
import math
import re
from typing import Any, Iterable
from uuid import uuid4

import config as _app_config

from app_core.app_clock import app_now, app_today
from app_core.json_store import _UNSET, JsonConflictError, json_transaction, json_transaction_for, load_json_safe, observed_json_revision, save_json_safe
from config import SUBSTANCE_PROFILES_PATH


PROFILE_SCHEMA_VERSION = 1
MODEL_SCHEMA_VERSION = 1
CAFFEINE_SUBSTANCE_ID = "caffeine"
_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


DEFAULT_SUBSTANCE_PROFILES: tuple[dict[str, Any], ...] = (
    {
        "substance_id": "caffeine",
        "name": "カフェイン",
        "unit": "mg",
        "default_amount_mg": 200.0,
        "default_label": "タブレット",
        "amount_step_mg": 25.0,
        "half_life_hours": 5.0,
        "peak_delay_minutes": 30,
        "residual_cutoff_hours": 36.0,
        "color": "#25A9D6",
        "fill_enabled": True,
        "is_active": True,
        "sort_order": 10,
        "model_revision": 1,
    },
    {
        "substance_id": "acetaminophen",
        "name": "アセトアミノフェン",
        "unit": "mg",
        "default_amount_mg": 300.0,
        "default_label": "錠剤",
        "amount_step_mg": 100.0,
        "half_life_hours": 2.4,
        "peak_delay_minutes": 30,
        "residual_cutoff_hours": 24.0,
        "color": "#EF7D57",
        "fill_enabled": True,
        "is_active": True,
        "sort_order": 20,
        "model_revision": 1,
    },
)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _bounded_number(value: Any, default: float, minimum: float, maximum: float) -> float:
    number = _number(value, default)
    return min(max(number, minimum), maximum)


def _parse_date(value: date | datetime | str | None = None) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            pass
    return app_today()


def parse_substance_datetime(
    value: Any,
    target: date | datetime | str | None = None,
) -> datetime | None:
    # Only time-only input needs a date. Full timestamps must not reread the
    # app clock settings for every record at every residual-curve point.
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, dt_time):
        parsed = datetime.combine(_parse_date(target), value)
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                parsed_time = dt_time.fromisoformat(text)
            except ValueError:
                return None
            parsed = datetime.combine(_parse_date(target), parsed_time)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def default_substance_profiles() -> list[dict[str, Any]]:
    if getattr(_app_config, "WEB_DEMO_EXPERIENCE", False):
        from web_demo.substances import default_web_substance_profiles
        return default_web_substance_profiles()
    return deepcopy(list(DEFAULT_SUBSTANCE_PROFILES))


def _fallback_profile(substance_id: str) -> dict[str, Any] | None:
    return next(
        (item for item in default_substance_profiles() if item["substance_id"] == substance_id),
        None,
    )


def normalize_substance_profile(
    value: Any,
    *,
    fallback: dict[str, Any] | None = None,
    index: int = 0,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    source = {**(fallback or {}), **value}
    raw_id = str(source.get("substance_id") or "").strip().lower()
    if not _PROFILE_ID_RE.fullmatch(raw_id):
        raw_id = f"custom_{index + 1:02d}"
    name = str(source.get("name") or "").strip()
    if not name:
        name = raw_id
    color = str(source.get("color") or "#25A9D6").strip()
    if not _COLOR_RE.fullmatch(color):
        color = str((fallback or {}).get("color") or "#25A9D6")
    profile = {
        "substance_id": raw_id,
        "name": name[:80],
        "unit": "mg",
        "default_amount_mg": round(
            _bounded_number(source.get("default_amount_mg"), 1.0, 0.001, 100000.0), 6
        ),
        "default_label": str(source.get("default_label") or "").strip(),
        "amount_step_mg": round(
            _bounded_number(source.get("amount_step_mg"), 0.5, 0.001, 10000.0), 6
        ),
        "half_life_hours": round(
            _bounded_number(source.get("half_life_hours"), 5.0, 0.05, 240.0), 6
        ),
        "peak_delay_minutes": int(
            round(_bounded_number(source.get("peak_delay_minutes"), 30.0, 0.0, 720.0))
        ),
        "residual_cutoff_hours": round(
            _bounded_number(source.get("residual_cutoff_hours"), 36.0, 0.1, 720.0), 6
        ),
        "color": color.upper(),
        "fill_enabled": bool(source.get("fill_enabled", True)),
        "is_active": bool(source.get("is_active", True)),
        "sort_order": int(round(_bounded_number(source.get("sort_order"), (index + 1) * 10, 0, 100000))),
        "model_revision": max(1, int(_number(source.get("model_revision"), 1))),
    }
    for key in ("created_at", "updated_at"):
        if source.get(key):
            profile[key] = str(source[key])
    return profile


def _raw_profile_rows() -> list[Any]:
    raw = load_json_safe(SUBSTANCE_PROFILES_PATH, {})
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict) and isinstance(raw.get("profiles"), list):
        return raw["profiles"]
    return []


def load_substance_profiles(*, include_inactive: bool = True) -> list[dict[str, Any]]:
    rows = _raw_profile_rows()
    if getattr(_app_config, "WEB_DEMO_EXPERIENCE", False):
        from web_demo.substances import web_profile_rows
        rows = web_profile_rows(rows)
    if not rows:
        profiles = default_substance_profiles()
    else:
        profiles = []
        seen: set[str] = set()
        for index, item in enumerate(rows):
            item_id = str(item.get("substance_id") or "").strip().lower() if isinstance(item, dict) else ""
            profile = normalize_substance_profile(item, fallback=_fallback_profile(item_id), index=index)
            if profile is None or profile["substance_id"] in seen:
                continue
            profiles.append(profile)
            seen.add(profile["substance_id"])
        for default in default_substance_profiles():
            if default["substance_id"] not in seen:
                profiles.append(deepcopy(default))
    profiles.sort(key=lambda item: (int(item.get("sort_order", 0)), str(item.get("name") or "")))
    if include_inactive:
        return profiles
    return [item for item in profiles if item.get("is_active", True)]


def substance_profile_map(*, include_inactive: bool = True) -> dict[str, dict[str, Any]]:
    return {
        str(profile["substance_id"]): profile
        for profile in load_substance_profiles(include_inactive=include_inactive)
    }


_MODEL_FIELDS = (
    "half_life_hours",
    "peak_delay_minutes",
    "residual_cutoff_hours",
)


def _profile_model_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return any(before.get(key) != after.get(key) for key in _MODEL_FIELDS)


def _validate_profile_for_save(profile: dict[str, Any], *, existing_names: set[str]) -> None:
    name = str(profile.get("name") or "").strip()
    if not name:
        raise ValueError("成分名を入力してください。")
    name_key = name.casefold()
    if name_key in existing_names:
        raise ValueError(f"成分名「{name}」はすでに使われています。")
    peak_hours = float(profile["peak_delay_minutes"]) / 60.0
    if float(profile["residual_cutoff_hours"]) <= peak_hours:
        raise ValueError("0 mg扱いまでの時間は、ピーク到達時間より長く設定してください。")
    existing_names.add(name_key)


@json_transaction_for(lambda profiles, **_kwargs: SUBSTANCE_PROFILES_PATH)
def save_substance_profiles(profiles: Iterable[dict[str, Any]], *, expected_revision: Any = _UNSET) -> list[dict[str, Any]]:
    if expected_revision is _UNSET:
        expected_revision = observed_json_revision(SUBSTANCE_PROFILES_PATH)
    existing = {item["substance_id"]: item for item in load_substance_profiles(include_inactive=True)}
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_names: set[str] = set()
    for index, item in enumerate(profiles):
        item_id = str(item.get("substance_id") or "").strip().lower()
        before = existing.get(item_id, {})
        profile = normalize_substance_profile(item, fallback=before or _fallback_profile(item_id), index=index)
        if profile is None:
            continue
        if profile["substance_id"] in seen:
            raise ValueError(f"成分ID「{profile['substance_id']}」が重複しています。")
        _validate_profile_for_save(profile, existing_names=seen_names)
        if before:
            profile["created_at"] = str(before.get("created_at") or timestamp)
            profile["model_revision"] = int(before.get("model_revision", 1) or 1) + (
                1 if _profile_model_changed(before, profile) else 0
            )
        else:
            profile["created_at"] = timestamp
            profile["model_revision"] = 1
        profile["updated_at"] = timestamp
        normalized.append(profile)
        seen.add(profile["substance_id"])

    # Referenced profiles are archived rather than silently deleted.
    for item_id, before in existing.items():
        if item_id in seen:
            continue
        archived = {**before, "is_active": False, "updated_at": timestamp}
        normalized.append(archived)

    normalized.sort(key=lambda item: (int(item.get("sort_order", 0)), str(item.get("name") or "")))
    save_json_safe(
        SUBSTANCE_PROFILES_PATH,
        {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "profiles": normalized,
            "updated_at": timestamp,
        },
        expected_revision=expected_revision,
    )
    return normalized


@json_transaction_for(lambda value, **_kwargs: SUBSTANCE_PROFILES_PATH)
def upsert_substance_profile(value: dict[str, Any], *, expected_revision: Any = _UNSET) -> dict[str, Any]:
    if expected_revision is _UNSET:
        expected_revision = observed_json_revision(SUBSTANCE_PROFILES_PATH)
    profiles = load_substance_profiles(include_inactive=True)
    item_id = str(value.get("substance_id") or "").strip().lower()
    replaced = False
    for index, profile in enumerate(profiles):
        if profile["substance_id"] == item_id:
            profiles[index] = {**profile, **value, "substance_id": item_id}
            replaced = True
            break
    if not replaced:
        if not _PROFILE_ID_RE.fullmatch(item_id):
            item_id = f"custom_{uuid4().hex[:12]}"
        profiles.append({**value, "substance_id": item_id, "sort_order": len(profiles) * 10 + 10})
    saved = save_substance_profiles(profiles, expected_revision=expected_revision)
    return next(item for item in saved if item["substance_id"] == item_id)


def create_substance_profile(
    name: str,
    *,
    default_amount_mg: Any = 1.0,
    default_label: str = "",
    amount_step_mg: Any = 0.5,
    half_life_hours: Any = 5.0,
    peak_delay_minutes: Any = 60,
    residual_cutoff_hours: Any = 40.0,
    color: str = "#5C8CE6",
    fill_enabled: bool = True,
) -> dict[str, Any]:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("成分名を入力してください。")
    return upsert_substance_profile(
        {
            "substance_id": f"custom_{uuid4().hex[:12]}",
            "name": clean_name,
            "default_amount_mg": default_amount_mg,
            "default_label": default_label,
            "amount_step_mg": amount_step_mg,
            "half_life_hours": half_life_hours,
            "peak_delay_minutes": peak_delay_minutes,
            "residual_cutoff_hours": residual_cutoff_hours,
            "color": color,
            "fill_enabled": fill_enabled,
            "is_active": True,
        }
    )


def model_snapshot_for_profile(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": MODEL_SCHEMA_VERSION,
        "profile_revision": max(1, int(_number(profile.get("model_revision"), 1))),
        "unit": "mg",
        "half_life_hours": round(_bounded_number(profile.get("half_life_hours"), 5.0, 0.05, 240.0), 6),
        "peak_delay_minutes": int(round(_bounded_number(profile.get("peak_delay_minutes"), 30, 0, 720))),
        "residual_cutoff_hours": round(
            _bounded_number(profile.get("residual_cutoff_hours"), 36.0, 0.1, 720.0), 6
        ),
    }


def normalize_model_snapshot(value: Any, profile: dict[str, Any]) -> dict[str, Any]:
    fallback = model_snapshot_for_profile(profile)
    source = value if isinstance(value, dict) else {}
    return {
        "schema_version": MODEL_SCHEMA_VERSION,
        "profile_revision": max(1, int(_number(source.get("profile_revision"), fallback["profile_revision"]))),
        "unit": "mg",
        "half_life_hours": round(
            _bounded_number(source.get("half_life_hours"), fallback["half_life_hours"], 0.05, 240.0), 6
        ),
        "peak_delay_minutes": int(
            round(_bounded_number(source.get("peak_delay_minutes"), fallback["peak_delay_minutes"], 0, 720))
        ),
        "residual_cutoff_hours": round(
            _bounded_number(
                source.get("residual_cutoff_hours"), fallback["residual_cutoff_hours"], 0.1, 720.0
            ),
            6,
        ),
    }


def normalize_substance_records(
    value: Any,
    target: date | datetime | str | None = None,
    *,
    profiles: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    target_date = _parse_date(target)
    profile_lookup = profiles or substance_profile_map(include_inactive=True)
    retired_examples = frozenset()
    if getattr(_app_config, "WEB_DEMO_EXPERIENCE", False):
        from web_demo.substances import LEGACY_DEMO_SUBSTANCE_IDS
        retired_examples = LEGACY_DEMO_SUBSTANCE_IDS
    caffeine_fallback = profile_lookup.get(CAFFEINE_SUBSTANCE_ID) or _fallback_profile(CAFFEINE_SUBSTANCE_ID) or {}
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            continue
        substance_id = str(item.get("substance_id") or "").strip().lower()
        if not substance_id or substance_id in retired_examples:
            continue
        amount_mg = round(max(_number(item.get("amount_mg"), 0.0), 0.0), 6)
        taken_at = parse_substance_datetime(item.get("taken_at"), target_date)
        if amount_mg <= 0 or taken_at is None:
            continue
        profile = profile_lookup.get(substance_id) or {
            **caffeine_fallback,
            "substance_id": substance_id,
            "name": substance_id,
        }
        record_id = str(
            item.get("substance_record_id")
            or item.get("record_id")
            or f"substance_{taken_at:%Y%m%d_%H%M%S}_{index + 1:02d}"
        )
        record = {
            "substance_record_id": record_id,
            "substance_id": substance_id,
            "taken_at": taken_at.isoformat(timespec="seconds"),
            "amount_mg": amount_mg,
            "unit": "mg",
            "source": str(item.get("source") or "substance_record_page"),
            "label": str(item.get("label") or ""),
            "memo": str(item.get("memo") or ""),
            "model_snapshot": normalize_model_snapshot(item.get("model_snapshot"), profile),
        }
        for key in ("created_at", "updated_at"):
            if item.get(key):
                record[key] = str(item[key])
        rows.append(record)
    return sorted(rows, key=lambda row: (row["taken_at"], row["substance_record_id"]))


def unified_substance_records(
    entry: dict[str, Any],
    *,
    profiles: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    profile_lookup = profiles or substance_profile_map(include_inactive=True)
    target = str(entry.get("date") or "")[:10]
    rows = normalize_substance_records(entry.get("substance_records", []), target, profiles=profile_lookup)
    seen = {str(item.get("substance_record_id") or "") for item in rows}
    caffeine_profile = profile_lookup.get(CAFFEINE_SUBSTANCE_ID) or _fallback_profile(CAFFEINE_SUBSTANCE_ID) or {}
    for index, item in enumerate(entry.get("caffeine_records", [])):
        if not isinstance(item, dict):
            continue
        taken_at = parse_substance_datetime(item.get("taken_at"), target)
        amount_mg = round(max(_number(item.get("amount_mg"), 0.0), 0.0), 6)
        if taken_at is None or amount_mg <= 0:
            continue
        record_id = str(
            item.get("caffeine_id")
            or f"caffeine_{taken_at:%Y%m%d_%H%M%S}_{index + 1:02d}"
        )
        if record_id in seen:
            continue
        rows.append(
            {
                "substance_record_id": record_id,
                "substance_id": CAFFEINE_SUBSTANCE_ID,
                "taken_at": taken_at.isoformat(timespec="seconds"),
                "amount_mg": amount_mg,
                "unit": "mg",
                "source": str(item.get("source") or "caffeine_record_page"),
                "label": str(item.get("label") or ""),
                "memo": str(item.get("memo") or ""),
                "model_snapshot": normalize_model_snapshot(item.get("model_snapshot"), caffeine_profile),
                **({"created_at": str(item["created_at"])} if item.get("created_at") else {}),
                **({"updated_at": str(item["updated_at"])} if item.get("updated_at") else {}),
            }
        )
        seen.add(record_id)
    return sorted(rows, key=lambda row: (row["taken_at"], row["substance_record_id"]))


def records_for_substance(
    entry: dict[str, Any],
    substance_id: str,
    *,
    profiles: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    return [
        item
        for item in unified_substance_records(entry, profiles=profiles)
        if item.get("substance_id") == substance_id
    ]


def latest_substance_record(records: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    valid = [item for item in records if item.get("taken_at")]
    return max(valid, key=lambda item: str(item.get("taken_at"))) if valid else None


def substance_records_total(records: Iterable[dict[str, Any]]) -> float:
    return round(sum(max(_number(item.get("amount_mg"), 0.0), 0.0) for item in records), 6)


def add_substance_record(
    target: date | datetime | str | None,
    substance_id: str,
    amount_mg: Any,
    *,
    taken_at: datetime | dt_time | str | None = None,
    source: str = "substance_record_page",
    label: str = "",
    memo: str = "",
) -> dict[str, Any]:
    profile_lookup = substance_profile_map(include_inactive=True)
    profile = profile_lookup.get(str(substance_id or "").strip().lower())
    if profile is None:
        raise ValueError("選択した成分が設定にありません。")
    if not profile.get("is_active", True):
        raise ValueError("アーカイブ中の成分には新しい記録を追加できません。ユーザー設定で再表示してください。")
    amount = round(max(_number(amount_mg, 0.0), 0.0), 6)
    if amount <= 0:
        raise ValueError("摂取量は0より大きい値で入力してください。")
    target_date = _parse_date(target)
    intake_at = parse_substance_datetime(taken_at, target_date) or app_now().replace(microsecond=0)
    snapshot = model_snapshot_for_profile(profile)

    from app_core.diary_service import add_caffeine_record, get_diary_entry_path, load_diary_entry, save_diary_entry

    if profile["substance_id"] == CAFFEINE_SUBSTANCE_ID:
        return add_caffeine_record(
            intake_at.date(),
            amount,
            taken_at=intake_at,
            source=source,
            label=label,
            memo=memo,
            model_snapshot=snapshot,
        )

    with json_transaction(get_diary_entry_path(intake_at.date())):
        entry = load_diary_entry(intake_at.date())
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        records = list(entry.get("substance_records", []))
        records.append(
            {
                "substance_record_id": f"{profile['substance_id']}_{intake_at:%Y%m%d_%H%M%S}_{uuid4().hex[:8]}",
                "substance_id": profile["substance_id"],
                "taken_at": intake_at.isoformat(timespec="seconds"),
                "amount_mg": amount,
                "unit": "mg",
                "source": str(source or "substance_record_page"),
                "label": str(label or ""),
                "memo": str(memo or ""),
                "model_snapshot": snapshot,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        save_diary_entry(intake_at.date(), {"substance_records": records})
        return load_diary_entry(intake_at.date())


def _diary_path_for_update(target: date | datetime | str | None, *_args: Any, **_kwargs: Any):
    from app_core.diary_service import get_diary_entry_path

    return get_diary_entry_path(target)


@json_transaction_for(_diary_path_for_update)
def replace_substance_records_for_day(
    target: date | datetime | str | None,
    substance_id: str,
    records: Iterable[dict[str, Any]],
    *,
    caffeine_untimed_mg: Any | None = None,
    expected_revision: Any = _UNSET,
    expected_record_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    from app_core.diary_service import get_diary_entry_path, load_diary_entry, save_diary_entry

    target_date = _parse_date(target)
    if expected_revision is _UNSET:
        expected_revision = observed_json_revision(get_diary_entry_path(target_date))
    profile_lookup = substance_profile_map(include_inactive=True)
    clean_id = str(substance_id or "").strip().lower()
    profile = profile_lookup.get(clean_id)
    if profile is None:
        raise ValueError("選択した成分が設定にありません。")
    entry = load_diary_entry(target_date)
    if expected_record_ids is not None:
        current_ids = {str(item["substance_record_id"]) for item in records_for_substance(entry, clean_id, profiles=profile_lookup)}
        if current_ids != set(expected_record_ids):
            raise JsonConflictError("服薬履歴が別の画面で追加・削除されています。入力を確認して再読込してください。")
    selected_rows = normalize_substance_records(list(records), target_date, profiles=profile_lookup)
    selected_rows = [item for item in selected_rows if item.get("substance_id") == clean_id]
    selected_ids = [str(item["substance_record_id"]) for item in selected_rows]
    if len(selected_ids) != len(set(selected_ids)):
        raise ValueError("服薬履歴のIDが重複しています。重複した行を確認してください。")
    if not profile.get("is_active", True):
        existing_ids = {
            str(item.get("substance_record_id") or "")
            for item in records_for_substance(entry, clean_id, profiles=profile_lookup)
        }
        added_ids = {
            str(item.get("substance_record_id") or "")
            for item in selected_rows
            if str(item.get("substance_record_id") or "") not in existing_ids
        }
        if added_ids:
            raise ValueError("アーカイブ中の成分には新しい記録を追加できません。既存履歴の編集・削除だけ行えます。")

    generic_other = [
        item
        for item in normalize_substance_records(
            entry.get("substance_records", []), target_date, profiles=profile_lookup
        )
        if item.get("substance_id") != clean_id
    ]
    payload: dict[str, Any] = {"substance_records": [*generic_other, *selected_rows]}
    if clean_id == CAFFEINE_SUBSTANCE_ID and caffeine_untimed_mg is not None:
        payload["caffeine_untimed_mg"] = round(max(0.0, _number(caffeine_untimed_mg, 0.0)), 6)
    save_diary_entry(target_date, payload, expected_revision=expected_revision)
    return load_diary_entry(target_date)


def format_mg(value: Any, *, maximum_decimals: int = 3) -> str:
    number = max(_number(value, 0.0), 0.0)
    text = f"{number:.{maximum_decimals}f}".rstrip("0").rstrip(".")
    return text or "0"
