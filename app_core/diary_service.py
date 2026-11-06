from __future__ import annotations

from datetime import date, datetime, time as dt_time
from pathlib import Path
from typing import Any
from uuid import uuid4

from app_core.app_clock import app_now, app_today
from app_core.substance_service import (
    CAFFEINE_SUBSTANCE_ID,
    default_substance_profiles,
    model_snapshot_for_profile,
    normalize_model_snapshot,
    normalize_substance_records,
    substance_profile_map,
)
from config import DIARY_ENTRIES_DIR
from app_core.json_store import _UNSET, json_transaction, json_transaction_for, load_json_safe, observed_json_revision, save_json_safe


LEGACY_CAFFEINE_PROFILE = {
    "substance_id": CAFFEINE_SUBSTANCE_ID,
    "name": "カフェイン",
    "unit": "mg",
    "half_life_hours": 5.0,
    "peak_delay_minutes": 30,
    "residual_cutoff_hours": 36.0,
    "model_revision": 1,
}


def _parse_date(value: date | datetime | str | None = None) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return app_today()
    return app_today()


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_diary_entry_path(target: date | datetime | str | None = None) -> Path:
    target_date = _parse_date(target)
    return DIARY_ENTRIES_DIR / f"{target_date:%Y}" / f"{target_date:%m}" / f"{target_date:%Y%m%d}.json"


def default_diary_entry(target: date | datetime | str | None = None) -> dict[str, Any]:
    target_date = _parse_date(target)
    timestamp = _now_text()
    return {
        "date": target_date.isoformat(),
        "title": "",
        "body": "",
        "mood": "未記録",
        "weather": "未記録",
        "good_seat_status": None,
        "caffeine_mg": 0,
        "caffeine_untimed_mg": 0,
        "caffeine_records": [],
        "substance_records_schema_version": 1,
        "substance_records": [],
        "running_distance_km": 0.0,
        "running_time_min": 0.0,
        "cost_records": [],
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _normalize_cost_records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("item") or "").strip()
        amount = _num(item.get("amount"))
        source = str(item.get("source") or "")
        if name or amount:
            record = {**item, "name": name, "amount": amount}
            if source:
                record["source"] = source
            rows.append(record)
    return rows


def _parse_caffeine_datetime(value: Any, target: date | datetime | str | None = None) -> datetime | None:
    target_date = _parse_date(target)
    if isinstance(value, datetime):
        return value.replace(microsecond=0)
    if isinstance(value, dt_time):
        return datetime.combine(target_date, value).replace(microsecond=0)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).replace(microsecond=0)
    except ValueError:
        try:
            return datetime.combine(target_date, dt_time.fromisoformat(text)).replace(microsecond=0)
        except ValueError:
            return None


def normalize_caffeine_records(
    value: Any,
    target: date | datetime | str | None = None,
    *,
    caffeine_profile: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    target_date = _parse_date(target)
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            continue
        amount_mg = round(max(0, _num(item.get("amount_mg"))), 6)
        taken_at = _parse_caffeine_datetime(item.get("taken_at"), target_date)
        if amount_mg <= 0 or taken_at is None:
            continue
        caffeine_id = str(item.get("caffeine_id") or f"caffeine_{taken_at:%Y%m%d_%H%M%S}_{index + 1:02d}")
        record = {
            "caffeine_id": caffeine_id,
            "taken_at": taken_at.isoformat(timespec="seconds"),
            "amount_mg": amount_mg,
            "source": str(item.get("source") or "diary"),
            "label": str(item.get("label") or ""),
            "memo": str(item.get("memo") or ""),
        }
        if caffeine_profile is not None or item.get("model_snapshot"):
            profile = caffeine_profile or substance_profile_map(include_inactive=True)[CAFFEINE_SUBSTANCE_ID]
            record["model_snapshot"] = normalize_model_snapshot(item.get("model_snapshot"), profile)
        if item.get("created_at"):
            record["created_at"] = str(item["created_at"])
        if item.get("updated_at"):
            record["updated_at"] = str(item["updated_at"])
        rows.append(record)
    return sorted(rows, key=lambda row: (row["taken_at"], row["caffeine_id"]))


def caffeine_records_total(entry: dict[str, Any]) -> float:
    return round(
        sum(max(0, _num(item.get("amount_mg"))) for item in entry.get("caffeine_records", []) if isinstance(item, dict)),
        6,
    )


def latest_caffeine_record(entry: dict[str, Any]) -> dict[str, Any] | None:
    records = [item for item in entry.get("caffeine_records", []) if isinstance(item, dict) and item.get("taken_at")]
    return max(records, key=lambda item: str(item.get("taken_at"))) if records else None


def normalize_diary_entry(
    data: Any,
    target: date | datetime | str | None = None,
    *,
    profiles: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    base = default_diary_entry(target)
    if isinstance(data, list):
        data = data[-1] if data else {}
    if not isinstance(data, dict):
        return base

    base.update(
        {
            key: data.get(key, base[key])
            for key in base
            if key
            not in (
                "cost_records",
                "caffeine_records",
                "substance_records",
                "substance_records_schema_version",
            )
        }
    )
    base["date"] = str(data.get("date") or base["date"])[:10]
    base["title"] = str(base.get("title") or "")
    base["body"] = str(base.get("body") or data.get("content") or "")
    base["mood"] = str(base.get("mood") or "未記録")
    base["weather"] = str(base.get("weather") or "未記録")
    if "good_seat_status" in data:
        seat_status = data.get("good_seat_status")
        base["good_seat_status"] = (
            seat_status if isinstance(seat_status, str) and seat_status in {"yes", "so_so", "no"} else None
        )
    else:
        legacy_seat_value = data.get("good_seat_obtained")
        base["good_seat_status"] = (
            "yes" if legacy_seat_value is True else "no" if legacy_seat_value is False else None
        )
    # A supplied lookup is one display run's snapshot, even when empty.
    # Work on our own map and supply the built-in caffeine fallback so the
    # substance normalizer never falls back to reading settings for {}.
    profile_lookup = dict(substance_profile_map(include_inactive=True) if profiles is None else profiles)
    if CAFFEINE_SUBSTANCE_ID not in profile_lookup:
        profile_lookup[CAFFEINE_SUBSTANCE_ID] = next(
            item for item in default_substance_profiles() if item["substance_id"] == CAFFEINE_SUBSTANCE_ID
        )
    caffeine_profile = profile_lookup[CAFFEINE_SUBSTANCE_ID]
    legacy_records = normalize_caffeine_records(
        data.get("caffeine_records", []),
        base["date"],
        caffeine_profile=LEGACY_CAFFEINE_PROFILE,
    )
    generic_records = normalize_substance_records(
        data.get("substance_records", []),
        base["date"],
        profiles=profile_lookup,
    )
    try:
        has_generic_schema = int(data.get("substance_records_schema_version", 0) or 0) >= 1
    except (TypeError, ValueError):
        has_generic_schema = False
    if not has_generic_schema:
        seen_ids = {str(item.get("substance_record_id") or "") for item in generic_records}
        for item in legacy_records:
            record_id = str(item.get("caffeine_id") or "")
            if record_id in seen_ids:
                continue
            generic_records.append(
                {
                    "substance_record_id": record_id,
                    "substance_id": CAFFEINE_SUBSTANCE_ID,
                    "taken_at": item["taken_at"],
                    "amount_mg": item["amount_mg"],
                    "unit": "mg",
                    "source": item.get("source", "diary"),
                    "label": item.get("label", ""),
                    "memo": item.get("memo", ""),
                    "model_snapshot": normalize_model_snapshot(item.get("model_snapshot"), caffeine_profile),
                    **({"created_at": item["created_at"]} if item.get("created_at") else {}),
                    **({"updated_at": item["updated_at"]} if item.get("updated_at") else {}),
                }
            )
            seen_ids.add(record_id)
        generic_records.sort(key=lambda row: (row["taken_at"], row["substance_record_id"]))
    base["substance_records_schema_version"] = 1
    base["substance_records"] = generic_records

    caffeine_generic_records = [
        item for item in generic_records if item.get("substance_id") == CAFFEINE_SUBSTANCE_ID
    ]
    base["caffeine_records"] = [
        {
            "caffeine_id": item["substance_record_id"],
            "taken_at": item["taken_at"],
            "amount_mg": item["amount_mg"],
            "source": item.get("source", "diary"),
            "label": item.get("label", ""),
            "memo": item.get("memo", ""),
            "model_snapshot": normalize_model_snapshot(item.get("model_snapshot"), caffeine_profile),
            **({"created_at": item["created_at"]} if item.get("created_at") else {}),
            **({"updated_at": item["updated_at"]} if item.get("updated_at") else {}),
        }
        for item in caffeine_generic_records
    ]

    if data.get("caffeine_mg") in (None, "") and data.get("caffeine_cups") not in (None, ""):
        legacy_caffeine_mg = round(max(0.0, _num(data.get("caffeine_cups")) * 100), 6)
    else:
        legacy_caffeine_mg = round(max(0.0, _num(base.get("caffeine_mg"))), 6)
    timed_caffeine_mg = caffeine_records_total(base)
    if "caffeine_untimed_mg" in data:
        untimed_caffeine_mg = round(max(0.0, _num(data.get("caffeine_untimed_mg"))), 6)
    else:
        untimed_caffeine_mg = max(legacy_caffeine_mg - timed_caffeine_mg, 0) if base["caffeine_records"] else legacy_caffeine_mg
    base["caffeine_untimed_mg"] = untimed_caffeine_mg
    base["caffeine_mg"] = round(timed_caffeine_mg + untimed_caffeine_mg, 6)
    base["running_distance_km"] = round(max(0.0, _num(base.get("running_distance_km"))), 3)
    base["running_time_min"] = round(max(0.0, _num(base.get("running_time_min"))), 1)
    base["cost_records"] = _normalize_cost_records(data.get("cost_records", []))
    base["created_at"] = data.get("created_at") or base["created_at"]
    base["updated_at"] = data.get("updated_at") or base["created_at"]
    return base


def load_diary_entry(
    target: date | datetime | str | None = None,
    *,
    profiles: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    path = get_diary_entry_path(target)
    return normalize_diary_entry(load_json_safe(path, default_diary_entry(target)), target, profiles=profiles)


@json_transaction_for(lambda target, data, **_kwargs: get_diary_entry_path(target))
def save_diary_entry(target: date | datetime | str | None, data: dict[str, Any], *, expected_revision: Any = _UNSET) -> None:
    target_date = _parse_date(target)
    if expected_revision is _UNSET:
        expected_revision = observed_json_revision(get_diary_entry_path(target_date))
    existing = load_diary_entry(target_date) if get_diary_entry_path(target_date).exists() else default_diary_entry(target_date)
    incoming = dict(data)
    profiles = substance_profile_map(include_inactive=True)
    caffeine_profile = profiles[CAFFEINE_SUBSTANCE_ID]
    if "substance_records" in incoming:
        incoming["substance_records"] = normalize_substance_records(
            incoming.get("substance_records", []), target_date, profiles=profiles
        )
        incoming["substance_records_schema_version"] = 1
    elif "caffeine_records" in incoming:
        non_caffeine_records = [
            item
            for item in existing.get("substance_records", [])
            if isinstance(item, dict) and item.get("substance_id") != CAFFEINE_SUBSTANCE_ID
        ]
        caffeine_records = normalize_caffeine_records(
            incoming.get("caffeine_records", []),
            target_date,
            caffeine_profile=caffeine_profile,
        )
        incoming["substance_records"] = [
            *non_caffeine_records,
            *[
                {
                    "substance_record_id": item["caffeine_id"],
                    "substance_id": CAFFEINE_SUBSTANCE_ID,
                    "taken_at": item["taken_at"],
                    "amount_mg": item["amount_mg"],
                    "unit": "mg",
                    "source": item.get("source", "diary"),
                    "label": item.get("label", ""),
                    "memo": item.get("memo", ""),
                    "model_snapshot": normalize_model_snapshot(item.get("model_snapshot"), caffeine_profile),
                    **({"created_at": item["created_at"]} if item.get("created_at") else {}),
                    **({"updated_at": item["updated_at"]} if item.get("updated_at") else {}),
                }
                for item in caffeine_records
            ],
        ]
        incoming["substance_records_schema_version"] = 1
    if "caffeine_mg" in incoming and "caffeine_untimed_mg" not in incoming:
        records_source = incoming.get("substance_records", existing.get("substance_records", []))
        timed_total = sum(
            max(0, _num(item.get("amount_mg")))
            for item in records_source
            if isinstance(item, dict) and item.get("substance_id") == CAFFEINE_SUBSTANCE_ID
        )
        requested_total = max(0, _num(incoming.get("caffeine_mg")))
        incoming["caffeine_untimed_mg"] = max(requested_total - timed_total, 0)
    payload = normalize_diary_entry({**existing, **incoming}, target_date)
    payload["date"] = target_date.isoformat()
    payload["created_at"] = existing.get("created_at") or _now_text()
    payload["updated_at"] = _now_text()
    save_json_safe(get_diary_entry_path(target_date), payload, expected_revision=expected_revision)


def add_caffeine_record(
    target: date | datetime | str | None,
    amount_mg: Any,
    *,
    taken_at: datetime | dt_time | str | None = None,
    source: str = "diary",
    label: str = "",
    memo: str = "",
    model_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target_date = _parse_date(target)
    intake_at = _parse_caffeine_datetime(taken_at, target_date) or app_now().replace(microsecond=0)
    target_date = intake_at.date()
    amount = round(max(0, _num(amount_mg)), 6)
    if amount <= 0:
        raise ValueError("カフェイン摂取量は0より大きい値で入力してください。")

    with json_transaction(get_diary_entry_path(target_date)):
        entry = load_diary_entry(target_date)
        timestamp = _now_text()
        profiles = substance_profile_map(include_inactive=True)
        caffeine_profile = profiles[CAFFEINE_SUBSTANCE_ID]
        records = list(entry.get("substance_records", []))
        records.append(
            {
                "substance_record_id": f"caffeine_{intake_at:%Y%m%d_%H%M%S}_{uuid4().hex[:8]}",
                "substance_id": CAFFEINE_SUBSTANCE_ID,
                "taken_at": intake_at.isoformat(timespec="seconds"),
                "amount_mg": amount,
                "unit": "mg",
                "source": str(source or "diary"),
                "label": str(label or ""),
                "memo": str(memo or ""),
                "model_snapshot": normalize_model_snapshot(
                    model_snapshot or model_snapshot_for_profile(caffeine_profile), caffeine_profile
                ),
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        save_diary_entry(target_date, {"substance_records": records})
        return load_diary_entry(target_date)


def investment_total(entry: dict[str, Any]) -> float:
    return sum(_num(item.get("amount")) for item in entry.get("cost_records", []) if isinstance(item, dict))


def upsert_daily_hub_cost(entry: dict[str, Any], name: str, amount: Any) -> dict[str, Any]:
    records = [item for item in entry.get("cost_records", []) if isinstance(item, dict) and item.get("source") != "daily_hub"]
    amount_value = _num(amount)
    clean_name = str(name or "").strip()
    if clean_name or amount_value:
        records.append({"name": clean_name or "今日の記録・計画", "amount": amount_value, "source": "daily_hub"})
    entry["cost_records"] = records
    return entry
