"""Pure diagnosis of the old counter's CSV exports; never reads or writes files.

Daily summaries are independent controls, never additional study records.
Original cell text stays on each row so displayed precision is not mistaken for
the precision of the saved record. All dates are explicitly local naive dates.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import hashlib
import io
import json
import math
import re
from typing import Any


MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 10_000
MAX_OVERLAP_PAIRS = 2_000
_CONDITION_COLUMNS = ("cond_effort_10", "cond_env", "cond_sleep_band", "cond_alcohol_yday",
                      "cond_mood_5", "cond_topic", "cond_io")
_ENVIRONMENTS = {"home": "自宅", "cowork": "ワーキングスペース", "other": "その他",
                 "office_self": "職場（旧：office_self）", "client": "訪問先（旧：client）"}
_CATEGORIES = {"medical": "医学", "english": "英語", "license": "資格",
               "programming": "IT", "other": "その他"}
_IO_TYPES = {"input": "インプット", "output": "アウトプット", "both": "両方"}
_NUMBER_TEXT = re.compile(r"[+-]?(?:(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_HMS_TEXT = re.compile(r"[0-9]+:[0-9]{1,2}:[0-9]{1,2}(?:\.[0-9]+)?")


def _issue(code: str, message: str, severity: str = "error", **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, "severity": severity, **extra}


def _read_csv(raw: bytes, *, summary: bool = False) -> tuple[list[dict[str, Any]], list[str], str | None, list[dict[str, Any]]]:
    label = "日次集計CSV" if summary else "セッションCSV"
    if not isinstance(raw, bytes):
        raise ValueError(f"{label}はバイト列で指定してください。")
    if len(raw) > MAX_BYTES:
        return [], [], None, [_issue("file_too_large", f"{label}は10MB以下にしてください。")]
    if not raw:
        return [], [], None, [_issue("empty_file", f"{label}が空です。")]
    text, encoding = None, None
    for candidate in ("utf-8-sig", "cp932"):
        try:
            text = raw.decode(candidate)
            encoding = candidate
            break
        except UnicodeError:
            pass
    if text is None:
        return [], [], None, [_issue("invalid_encoding", f"{label}の文字コードを確認できません。UTF-8またはCP932を使用してください。")]
    if "\x00" in text:
        return [], [], encoding, [_issue("invalid_text", f"{label}に無効な文字が含まれています。")]
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next(reader)
    except (StopIteration, csv.Error):
        return [], [], encoding, [_issue("invalid_header", f"{label}の見出しを読み取れません。")]
    columns = [value.strip() for value in header]
    if not columns or any(not value for value in columns) or len(set(columns)) != len(columns):
        return [], columns, encoding, [_issue("invalid_header", f"{label}の見出しに空欄または重複があります。")]
    required = {"日付", "勉強時間(合計)", "金額換算(合計・円)", "セッション数"} if summary else {
        "duration_sec", "hourly_wage", "money", "memo"}
    missing = sorted(required - set(columns))
    if not summary:
        if not {"start", "start_dt"} & set(columns):
            missing.append("start または start_dt")
        if not {"end", "end_dt"} & set(columns):
            missing.append("end または end_dt")
    if missing:
        return [], columns, encoding, [_issue("missing_columns", f"{label}の必須列がありません: {', '.join(missing)}")]
    rows, issues = [], []
    try:
        while True:
            line = reader.line_num + 1
            try:
                values = next(reader)
            except StopIteration:
                break
            if not values or all(not value.strip() for value in values):
                continue
            if len(rows) >= MAX_ROWS:
                issues.append(_issue("too_many_rows", f"{label}は10,000行以下にしてください。"))
                break
            row_issues = []
            if len(values) != len(columns):
                row_issues.append(_issue("invalid_row_width", "見出しと列数が一致しません。"))
            row = dict(zip(columns, values))
            if len(values) > len(columns):
                row["__extra_cells"] = values[len(columns):]
            rows.append({"row_number": line, "raw": row, "issues": row_issues})
    except csv.Error:
        issues.append(_issue("invalid_csv", f"{label}の引用符またはセルの長さが不正です。", row_number=reader.line_num))
    if not rows and not issues:
        issues.append(_issue("no_records", f"{label}にデータ行がありません。"))
    return rows, columns, encoding, issues


def _decimal(value: str, name: str, issues: list[dict[str, Any]], *, positive: bool = False) -> Decimal | None:
    text = str(value).strip()
    try:
        if not text or len(text) > 128 or _NUMBER_TEXT.fullmatch(text) is None:
            raise ValueError
        result = Decimal(text.replace(",", ""))
        numeric = float(result)
        if not result.is_finite() or not math.isfinite(numeric) or result != 0 and numeric == 0:
            raise ValueError
        if result < (Decimal("0.000001") if positive else 0):
            raise ValueError
        return result
    except (InvalidOperation, ValueError, OverflowError):
        issues.append(_issue("invalid_number", f"{name}は{'正の' if positive else '0以上の'}有限の数値で指定してください。", field=name))
        return None


def _canonical_number(value: Decimal) -> str:
    if not value:
        return "0"
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _condition_identity(raw: dict[str, str]) -> dict[str, str]:
    result = {key: raw.get(key, "").strip() for key in _CONDITION_COLUMNS}
    for key in ("cond_effort_10", "cond_mood_5", "cond_alcohol_yday"):
        text = result[key]
        if text and len(text) <= 128:
            try:
                value = Decimal(text)
                numeric = float(value)
                if value.is_finite() and math.isfinite(numeric) and (not value or numeric):
                    result[key] = _canonical_number(value)
            except (InvalidOperation, ValueError, OverflowError):
                pass
    return result


def _timestamp(value: str, name: str, issues: list[dict[str, Any]]) -> datetime | None:
    try:
        text = str(value).strip()
        if len(text) < 16 or "T" not in text and " " not in text:
            raise ValueError
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is not None:
            issues.append(_issue("timezone_requires_confirmation", f"{name}にタイムゾーンがあります。ローカル時刻への対応を確認してください。", field=name))
            return None
        return parsed
    except (ValueError, OverflowError):
        issues.append(_issue("invalid_datetime", f"{name}の日時を解釈できません。", field=name))
        return None


def _hms(value: str) -> Decimal | None:
    try:
        if len(str(value)) > 128 or _HMS_TEXT.fullmatch(str(value).strip()) is None:
            return None
        parts = str(value).strip().split(":")
        if len(parts) != 3:
            return None
        hh, mm, ss = [Decimal(item) for item in parts]
        if not all(item.is_finite() and math.isfinite(float(item)) for item in (hh, mm, ss)) or hh < 0 or hh != int(hh):
            return None
        if not 0 <= mm < 60 or mm != int(mm) or not 0 <= ss < 60:
            return None
        result = hh * 3600 + mm * 60 + ss
        return result if math.isfinite(float(result)) else None
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _conditions(raw: dict[str, str], issues: list[dict[str, Any]]) -> dict[str, Any]:
    condition: dict[str, Any] = {}
    for source, target, lower, upper in (("cond_effort_10", "effort", 1, 10), ("cond_mood_5", "mood", 1, 5)):
        text = raw.get(source, "").strip()
        if not text:
            continue
        value = _decimal(text, source, issues)
        if value is not None:
            if not lower <= value <= upper:
                issues.append(_issue("condition_out_of_range", f"{source}は{lower}〜{upper}の範囲で指定してください。", field=source))
            else:
                condition[target] = int(value) if value == int(value) else float(value)
    env = raw.get("cond_env", "").strip()
    if env:
        condition["environment"] = _ENVIRONMENTS.get(env, env)
        if env not in {"home", "cowork", "other"}:
            issues.append(_issue("ambiguous_environment", f"旧環境「{env}」の対応を確認してください。元の区分を保持しています。", "warning"))
    topic = raw.get("cond_topic", "").strip()
    if topic:
        condition["category"] = _CATEGORIES.get(topic, topic)
        if topic not in _CATEGORIES:
            issues.append(_issue("ambiguous_category", f"旧カテゴリ「{topic}」の対応を確認してください。元の区分を保持しています。", "warning"))
    io_type = raw.get("cond_io", "").strip()
    if io_type in _IO_TYPES:
        condition["io_type"] = _IO_TYPES[io_type]
    elif io_type:
        issues.append(_issue("invalid_io_type", f"入出力区分「{io_type}」を確認してください。現行区分は未記録にし、元値を保持します。", "warning"))
    alcohol = raw.get("cond_alcohol_yday", "").strip()
    if alcohol:
        value = _decimal(alcohol, "cond_alcohol_yday", issues)
        if value in (Decimal(0), Decimal(1)):
            condition["alcohol_yesterday"] = "あり" if value else "なし"
        elif value is not None:
            issues.append(_issue("invalid_alcohol", "前日飲酒は旧値0または1で指定してください。", "warning"))
    band = raw.get("cond_sleep_band", "").strip()
    if band:
        condition["legacy_sleep_band"] = band
    return condition


def _convert_row(source: dict[str, Any]) -> tuple[dict[str, Any], Decimal | None, Decimal | None]:
    raw = source["raw"]
    issues = list(source["issues"])
    start = _timestamp(raw.get("start_dt") or raw.get("start", ""), "開始", issues)
    end = _timestamp(raw.get("end_dt") or raw.get("end", ""), "終了", issues)
    duration = _decimal(raw.get("duration_sec", ""), "duration_sec", issues, positive=True)
    rate = _decimal(raw.get("hourly_wage", ""), "hourly_wage", issues)
    money = _decimal(raw.get("money", ""), "money", issues)
    condition = _conditions(raw, issues)
    if start and end and end < start:
        issues.append(_issue("end_before_start", "終了日時が開始日時より前です。"))
    if start and end and duration is not None and end >= start:
        wall = Decimal(str((end - start).total_seconds()))
        if abs(wall - duration) > 1:
            issues.append(_issue("duration_wall_difference", "実勉強時間と開始・終了の差が1秒を超えます。時間補正や一時停止を含むか確認してください。", "warning"))
    for precise_key, display_key in (("start_dt", "start"), ("end_dt", "end")):
        if raw.get(precise_key, "").strip() and raw.get(display_key, "").strip():
            precise = _timestamp(raw[precise_key], precise_key, issues)
            display = _timestamp(raw[display_key], display_key, issues)
            if precise and display and abs((precise - display).total_seconds()) >= 1:
                issues.append(_issue("datetime_alias_mismatch", f"{precise_key}と{display_key}が一致しません。"))
    for field in ("date", "日付"):
        if raw.get(field, "").strip():
            try:
                parsed_day = date.fromisoformat(raw[field].strip())
                if start and parsed_day != start.date():
                    issues.append(_issue("date_alias_mismatch", f"{field}と開始日が一致しません。"))
            except ValueError:
                issues.append(_issue("invalid_date", f"{field}の日付を解釈できません。"))
    if "勉強内容" in raw and raw["勉強内容"] != raw.get("memo", ""):
        issues.append(_issue("memo_alias_mismatch", "memoと勉強内容が一致しません。"))
    if "勉強時間" in raw:
        display_duration = _hms(raw["勉強時間"])
        if display_duration is None or duration is not None and abs(display_duration - duration) >= 1:
            issues.append(_issue("duration_alias_mismatch", "表示用の勉強時間とduration_secが一致しません。"))
    if "金額換算(円)" in raw:
        display_money = _decimal(raw["金額換算(円)"], "金額換算(円)", issues)
        if display_money is not None and money is not None and abs(display_money - money) > Decimal("0.050000001"):
            issues.append(_issue("money_alias_mismatch", "表示用金額とmoneyの差が丸め誤差を超えています。"))
    identity = {
        "start": start.isoformat() if start else raw.get("start_dt") or raw.get("start", ""),
        "end": end.isoformat() if end else raw.get("end_dt") or raw.get("end", ""),
        "duration": _canonical_number(duration) if duration is not None else raw.get("duration_sec", ""),
        "rate": _canonical_number(rate) if rate is not None else raw.get("hourly_wage", ""),
        "money": _canonical_number(money) if money is not None else raw.get("money", ""),
        "memo": raw.get("memo", ""),
        "conditions": _condition_identity(raw),
    }
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    session = None
    if not any(issue["severity"] == "error" for issue in issues):
        assert start is not None and end is not None and duration is not None and rate is not None and money is not None
        session = {"start_at": start.isoformat(), "end_at": end.isoformat(), "duration_sec": float(duration),
                   "hourly_rate": float(rate), "money": float(money), "memo": raw.get("memo", ""),
                   "condition": condition, "project_id": "", "subject_id": "", "material_id": "", "unit_id": "",
                   "linked_note_ids": [], "linked_task_id": ""}
    return {"row_number": source["row_number"], "fingerprint": fingerprint, "session": session,
            "legacy_topic": raw.get("cond_topic", "").strip(), "issues": issues, "raw": raw}, duration, money


def _reconcile(daily: dict[str, dict[str, Any]], source: list[dict[str, Any]] | None,
               issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    duplicate_days: set[str] = set()
    for row in source or []:
        raw, line = row["raw"], row["row_number"]
        row_issues = list(row["issues"])
        try:
            day = date.fromisoformat(raw.get("日付", "").strip()).isoformat()
        except ValueError:
            issues.append(_issue("invalid_summary_date", "日次集計の日付を解釈できません。", row_number=line))
            continue
        duration = _hms(raw.get("勉強時間(合計)", ""))
        money = _decimal(raw.get("金額換算(合計・円)", ""), "日次集計の金額", row_issues)
        count = _decimal(raw.get("セッション数", ""), "セッション数", row_issues)
        if duration is None:
            row_issues.append(_issue("invalid_summary_duration", "日次集計の時間を解釈できません。"))
        if count is not None and count != int(count):
            row_issues.append(_issue("invalid_summary_count", "日次集計の件数は整数にしてください。"))
        for issue in row_issues:
            issues.append({**issue, "row_number": line, "source": "summary"})
        if day in summaries:
            duplicate_days.add(day)
            issues.append(_issue("duplicate_summary_date", f"日次集計の日付{day}が重複しています。", row_number=line))
        summaries[day] = {"duration_sec": duration, "money": money, "count": count,
                          "invalid": bool(row_issues)}
    rows = []
    for day in sorted(set(daily) | set(summaries)):
        actual, summary = daily.get(day), summaries.get(day)
        result = {"date": day, "count": actual["count"] if actual else 0,
                  "duration_sec": float(actual["duration_sec"]) if actual else 0.0,
                  "money": float(actual["money"]) if actual else 0.0,
                  "summary_count": float(summary["count"]) if summary and summary["count"] is not None else None,
                  "summary_duration_sec": float(summary["duration_sec"]) if summary and summary["duration_sec"] is not None else None,
                  "summary_money": float(summary["money"]) if summary and summary["money"] is not None else None,
                  "count_delta": None, "duration_delta_sec": None, "money_delta": None, "status": "ok"}
        if source is None:
            rows.append(result)
            continue
        if not actual or not summary:
            issues.append(_issue("summary_date_mismatch", f"{day}がセッションCSVと日次集計CSVの片方にしかありません。"))
            result["status"] = "error"
        elif summary["invalid"] or day in duplicate_days:
            result["status"] = "error"
        else:
            duration_delta = summary["duration_sec"] - actual["duration_sec"]
            money_delta = summary["money"] - actual["money"]
            count_delta = summary["count"] - actual["count"]
            result.update(count_delta=int(count_delta), duration_delta_sec=float(duration_delta), money_delta=float(money_delta))
            if (count_delta or summary["duration_sec"] != actual["duration_sec"].to_integral_value(rounding=ROUND_FLOOR)
                    or abs(money_delta) > Decimal("0.050000001")):
                result["status"] = "error"
                issues.append(_issue("summary_totals_mismatch", f"{day}の日次集計と明細の件数・時間・金額が一致しません。"))
        rows.append(result)
    return rows


def diagnose_legacy_csv(session_bytes: bytes, summary_bytes: bytes | None = None) -> dict[str, Any]:
    """Return an inert conversion proposal; errors never repair or omit records."""
    source, columns, encoding, issues = _read_csv(session_bytes)
    summaries, summary_columns, summary_encoding = None, [], None
    if summary_bytes is not None:
        summaries, summary_columns, summary_encoding, summary_issues = _read_csv(summary_bytes, summary=True)
        issues.extend({**issue, "source": "summary"} for issue in summary_issues)
    rows = []
    daily: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "duration_sec": Decimal(0), "money": Decimal(0)})
    for item in source:
        row, duration, money = _convert_row(item)
        rows.append(row)
        if row["session"] is not None:
            day = row["session"]["start_at"][:10]
            daily[day]["count"] += 1
            daily[day]["duration_sec"] += duration
            daily[day]["money"] += money
    duplicates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        duplicates[row["fingerprint"]].append(row)
    for matches in duplicates.values():
        if len(matches) > 1:
            numbers = [row["row_number"] for row in matches]
            for row in matches:
                shown = numbers[:20]
                row["issues"].append(_issue("duplicate_row", f"同一内容の行が{len(numbers)}件あります（CSV行: {', '.join(map(str, shown))}）。", "warning", rows=shown))
    overlap_pairs = []
    ordered = sorted((row for row in rows if row["session"]), key=lambda row: row["session"]["start_at"])
    active: list[dict[str, Any]] = []
    overlap_limit = False
    for row in ordered:
        start = datetime.fromisoformat(row["session"]["start_at"])
        end = datetime.fromisoformat(row["session"]["end_at"])
        active = [previous for previous in active if datetime.fromisoformat(previous["session"]["end_at"]) > start]
        for previous in active:
            if previous["fingerprint"] == row["fingerprint"]:
                continue
            seconds = (min(end, datetime.fromisoformat(previous["session"]["end_at"])) - start).total_seconds()
            if seconds <= 0:
                continue
            if len(overlap_pairs) >= MAX_OVERLAP_PAIRS:
                overlap_limit = True
                issues.append(_issue("too_many_overlaps", "時間帯の重なりが2,000組を超えました。対象を分けて確認してください。"))
                break
            numbers = [previous["row_number"], row["row_number"]]
            overlap_pairs.append({"rows": numbers, "seconds": seconds})
            for affected in (previous, row):
                affected["issues"].append(_issue("overlapping_time", f"記録の時間帯が重なります（CSV行: {numbers[0]}, {numbers[1]}）。", "warning", rows=numbers))
        active.append(row)
        if overlap_limit:
            break
    reconciliation = _reconcile(daily, summaries, issues)
    duration_total = sum((values["duration_sec"] for values in daily.values()), Decimal(0))
    money_total = sum((values["money"] for values in daily.values()), Decimal(0))
    if not all(math.isfinite(float(value)) for value in (duration_total, money_total)):
        issues.append(_issue("aggregate_overflow", "時間または金額の合計が処理可能な範囲を超えています。"))
    all_issues = [*issues, *(issue for row in rows for issue in row["issues"])]
    return {
        "schema_version": 1, "source_sha256": hashlib.sha256(session_bytes).hexdigest(),
        "summary_sha256": hashlib.sha256(summary_bytes).hexdigest() if summary_bytes is not None else None,
        "source_encoding": encoding, "summary_encoding": summary_encoding,
        "source_columns": columns, "summary_columns": summary_columns,
        "rows": rows, "issues": issues, "overlap_pairs": overlap_pairs,
        "daily_reconciliation": reconciliation,
        "summary": {"row_count": len(rows), "valid_row_count": sum(row["session"] is not None for row in rows),
                    "date_count": len(daily), "first_date": min(daily) if daily else None,
                    "last_date": max(daily) if daily else None,
                    "duration_sec": float(duration_total) if math.isfinite(float(duration_total)) else None,
                    "money": float(money_total) if math.isfinite(float(money_total)) else None,
                    "error_count": sum(issue["severity"] == "error" for issue in all_issues),
                    "warning_count": sum(issue["severity"] == "warning" for issue in all_issues),
                    "reconciliation_status": "not_provided" if summary_bytes is None else
                        "error" if any(issue["severity"] == "error" for issue in issues) or any(row["status"] == "error" for row in reconciliation) else "ok"},
    }
