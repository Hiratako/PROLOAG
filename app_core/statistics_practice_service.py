from __future__ import annotations

import hashlib
import json
import math
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import numpy as np
import pandas as pd

from app_core.app_clock import app_now
from app_core.analysis_dates import parse_analysis_date, parse_analysis_datetime
from app_core.json_store import json_transaction, load_json_safe, record_json_files, save_json_safe
from app_core.performance_analysis_service import build_daily_feature_frame
from app_core.study_log_service import load_all_study_sessions, session_duration_sec, session_start_at
from config import STATISTICS_PRACTICE_DIR


SCHEMA_VERSION = 1
GRADING_CONTRACT_VERSION = 2
ROUNDING_RULE = "ROUND_HALF_UP"
NUMERIC_RULE = "数値解答は十進四捨五入（ROUND_HALF_UP、ちょうど半分は0から遠ざける）で指定桁に丸めます。入力も同じ規約で丸めて正解と一致比較します。整数欄と値の範囲は丸め前に確認します。表示値の基本四則演算は厳密な有理数で計算します。平方根・指数・検定統計量やp値等は数値計算の精度に依存し、微小な値を足して丸め境界を調整しません。"
_INPUT_DOMAINS = {
    "real": {"integer": False, "minimum": None, "maximum": None},
    "nonnegative": {"integer": False, "minimum": 0, "maximum": None},
    "integer_count": {"integer": True, "minimum": 0, "maximum": None},
    "probability": {"integer": False, "minimum": 0, "maximum": 1},
    "percentage": {"integer": False, "minimum": 0, "maximum": 100},
    "correlation": {"integer": False, "minimum": -1, "maximum": 1},
    "percentage_difference": {"integer": False, "minimum": -100, "maximum": 100},
}

TOPIC_LABELS: dict[str, str] = {
    "descriptive": "記述統計",
    "probability": "確率",
    "distribution": "確率分布・標準化",
    "inference": "推定",
    "categorical": "カテゴリデータ",
    "correlation": "相関・回帰",
    "testing": "仮説検定",
    "nonparametric": "ノンパラメトリック",
    "logistic": "ロジスティック回帰",
    "survival": "生存時間解析",
    "clinical": "診断指標・研究デザイン",
}

_ENVIRONMENTS = ("自宅", "カフェ", "図書館", "ワーキングスペース", "職場", "移動中")
_IO_TYPES = ("インプット", "アウトプット", "両方")
_STUDY_CATEGORIES = ("医学", "英語", "語学", "資格", "IT", "読書", "仕事", "その他")


class NoAvailableProblemError(ValueError):
    """Raised when the requested period cannot support any safe exercise."""


class _TemplateUnavailable(Exception):
    pass


@dataclass
class _DataContext:
    start_date: date
    end_date: date
    daily: pd.DataFrame
    sessions: pd.DataFrame


@dataclass(frozen=True)
class _Template:
    template_id: str
    topic: str
    title: str
    builder: Callable[[_DataContext, np.random.Generator], dict[str, Any]]
    data_origin: str = "personal_logs"


def _coerce_date(value: date | datetime | str, field_name: str) -> date:
    parsed = parse_analysis_date(value)
    if parsed is None:
        raise ValueError(f"{field_name} は日付として解釈できません。")
    return parsed


def _period(start_date: date | datetime | str, end_date: date | datetime | str) -> tuple[date, date]:
    start = _coerce_date(start_date, "start_date")
    end = _coerce_date(end_date, "end_date")
    return (end, start) if end < start else (start, end)


def _practice_dir(root: Path | None) -> Path:
    return STATISTICS_PRACTICE_DIR if root is None else Path(root) / "statistics_practice"


def _json_ready(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (float, np.floating)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if value is pd.NA:
        return None
    return str(value)


def _ensure_json(value: Any) -> Any:
    ready = _json_ready(value)
    json.dumps(ready, ensure_ascii=False, allow_nan=False)
    return ready


def _load_context(start: date, end: date, root: Path | None) -> _DataContext:
    from app_core.study_category_service import load_study_category_catalog, resolve_study_category
    category_catalog = load_study_category_catalog(root)
    daily = build_daily_feature_frame(start, end, root)
    daily = daily.copy()
    if not daily.empty and "date" in daily:
        daily["date"] = pd.to_datetime(daily["date"], errors="coerce")

    session_rows: list[dict[str, Any]] = []
    for session in load_all_study_sessions(root):
        if not isinstance(session, dict):
            continue
        started = parse_analysis_datetime(session_start_at(session))
        if started is None or not start <= started.date() <= end:
            continue
        duration = session_duration_sec(session)
        if not math.isfinite(duration) or duration <= 0:
            continue
        condition = session.get("condition") if isinstance(session.get("condition"), dict) else {}
        environment = str(condition.get("environment") or "").strip()
        io_type = str(condition.get("io_type") or "").strip()
        category = str(condition.get("category") or "").strip()
        session_rows.append(
            {
                "date": started.date().isoformat(),
                "started_at": started.isoformat(timespec="minutes"),
                "session_minutes": duration / 60.0,
                "environment": environment if environment in _ENVIRONMENTS else None,
                "io_type": io_type if io_type in _IO_TYPES else None,
                "study_category": resolve_study_category(category, category_catalog, unknown="") or None,
            }
        )
    sessions = pd.DataFrame(
        session_rows,
        columns=("date", "started_at", "session_minutes", "environment", "io_type", "study_category"),
    )
    if not sessions.empty:
        sessions = sessions.sort_values("started_at", kind="stable").reset_index(drop=True)
    return _DataContext(start, end, daily, sessions)


def _sample_frame(
    frame: pd.DataFrame,
    rng: np.random.Generator,
    *,
    minimum: int,
    maximum: int,
) -> pd.DataFrame:
    if len(frame) < minimum:
        raise _TemplateUnavailable(f"有効な観測が{minimum}件以上必要です。")
    upper = min(maximum, len(frame))
    size = int(rng.integers(minimum, upper + 1)) if upper > minimum else minimum
    if size < len(frame):
        positions = np.sort(rng.choice(len(frame), size=size, replace=False))
        result = frame.iloc[positions].copy()
    else:
        result = frame.copy()
    return result.reset_index(drop=True)


def _numeric_sources(
    context: _DataContext,
    *,
    minimum: int,
    require_variation: bool = False,
) -> list[dict[str, Any]]:
    specs = (
        ("daily", "study_hours", "日別学習時間", "時間", "日"),
        ("daily", "sleep_hours", "睡眠時間", "時間", "日"),
        ("daily", "focus_score", "集中度", "/ 5", "日"),
        ("daily", "fatigue", "疲労感", "/ 5", "日"),
        ("daily", "running_distance_km", "走行距離", "km", "運動記録日"),
        ("daily", "running_duration_min", "運動時間", "分", "運動記録日"),
        ("session", "session_minutes", "学習セッション時間", "分", "学習セッション"),
    )
    sources: list[dict[str, Any]] = []
    for scope, key, label, unit, observation_unit in specs:
        source_frame = context.sessions if scope == "session" else context.daily
        if key not in source_frame or "date" not in source_frame:
            continue
        values = pd.to_numeric(source_frame[key], errors="coerce")
        finite = values.notna() & np.isfinite(values)
        rows = pd.DataFrame(
            {
                "date": source_frame.loc[finite, "date"].map(
                    lambda item: item.date().isoformat() if isinstance(item, pd.Timestamp) else str(item)[:10]
                ),
                "value": values.loc[finite].astype(float),
            }
        ).reset_index(drop=True)
        if len(rows) < minimum:
            continue
        if require_variation and rows["value"].nunique(dropna=True) < 2:
            continue
        sources.append(
            {
                "key": key,
                "label": label,
                "unit": unit,
                "observation_unit": observation_unit,
                "rows": rows,
            }
        )
    return sources


def _choose_numeric(
    context: _DataContext,
    rng: np.random.Generator,
    *,
    minimum: int,
    require_variation: bool = False,
) -> dict[str, Any]:
    sources = _numeric_sources(context, minimum=minimum, require_variation=require_variation)
    if not sources:
        suffix = "かつ値にばらつきのある" if require_variation else ""
        raise _TemplateUnavailable(f"{suffix}数値記録が{minimum}件以上必要です。")
    return sources[int(rng.integers(0, len(sources)))]


def _categorical_sources(context: _DataContext, *, minimum: int) -> list[dict[str, Any]]:
    specs = (
        ("io_type", "インプット／アウトプット", "学習セッション"),
        ("environment", "学習環境", "学習セッション"),
        ("study_category", "学習カテゴリ", "学習セッション"),
    )
    sources: list[dict[str, Any]] = []
    for key, label, observation_unit in specs:
        if key not in context.sessions:
            continue
        mask = context.sessions[key].notna()
        rows = context.sessions.loc[mask, ["date", key]].rename(columns={key: "category"}).reset_index(drop=True)
        if len(rows) >= minimum and rows["category"].nunique() >= 2:
            sources.append(
                {
                    "key": key,
                    "label": label,
                    "observation_unit": observation_unit,
                    "rows": rows,
                }
            )

    if "study_hours" in context.daily and "date" in context.daily:
        values = pd.to_numeric(context.daily["study_hours"], errors="coerce")
        mask = values.notna()
        weekday_rows = pd.DataFrame(
            {
                "date": context.daily.loc[mask, "date"].dt.date.map(date.isoformat),
                "category": np.where(context.daily.loc[mask, "is_weekend"].astype(bool), "休日", "平日"),
            }
        ).reset_index(drop=True)
        if len(weekday_rows) >= minimum and weekday_rows["category"].nunique() >= 2:
            sources.append(
                {
                    "key": "weekday_group",
                    "label": "曜日区分",
                    "observation_unit": "学習記録日",
                    "rows": weekday_rows,
                }
            )
    return sources


def _choose_categorical(context: _DataContext, rng: np.random.Generator, *, minimum: int) -> dict[str, Any]:
    sources = _categorical_sources(context, minimum=minimum)
    if not sources:
        raise _TemplateUnavailable(f"2区分以上を含むカテゴリ記録が{minimum}件以上必要です。")
    return sources[int(rng.integers(0, len(sources)))]


def _categorical_pairs(context: _DataContext, *, minimum: int) -> list[dict[str, Any]]:
    specs = (
        ("environment", "学習環境", "io_type", "インプット／アウトプット"),
        ("study_category", "学習カテゴリ", "io_type", "インプット／アウトプット"),
        ("environment", "学習環境", "study_category", "学習カテゴリ"),
    )
    pairs: list[dict[str, Any]] = []
    for x_key, x_label, y_key, y_label in specs:
        if x_key not in context.sessions or y_key not in context.sessions:
            continue
        rows = context.sessions.dropna(subset=[x_key, y_key]).loc[:, ["date", x_key, y_key]].copy()
        rows = rows.rename(columns={x_key: "x", y_key: "y"}).reset_index(drop=True)
        if len(rows) < minimum or rows["x"].nunique() < 2 or rows["y"].nunique() < 2:
            continue
        pairs.append(
            {
                "key": f"{x_key}_by_{y_key}",
                "x_label": x_label,
                "y_label": y_label,
                "observation_unit": "学習セッション",
                "rows": rows,
            }
        )
    return pairs


def _choose_categorical_pair(context: _DataContext, rng: np.random.Generator, *, minimum: int) -> dict[str, Any]:
    pairs = _categorical_pairs(context, minimum=minimum)
    if not pairs:
        raise _TemplateUnavailable(f"2種類のカテゴリがそろった記録が{minimum}件以上必要です。")
    return pairs[int(rng.integers(0, len(pairs)))]


def _correlation_sources(context: _DataContext, *, minimum: int) -> list[dict[str, Any]]:
    specs = (
        ("sleep_hours", "睡眠時間", "時間", "study_hours", "日別学習時間", "時間"),
        ("sleep_hours", "睡眠時間", "時間", "focus_score", "集中度", "/ 5"),
        ("study_hours", "日別学習時間", "時間", "focus_score", "集中度", "/ 5"),
        ("running_duration_min", "運動時間", "分", "study_hours", "日別学習時間", "時間"),
    )
    sources: list[dict[str, Any]] = []
    for x_key, x_label, x_unit, y_key, y_label, y_unit in specs:
        if x_key not in context.daily or y_key not in context.daily:
            continue
        x_values = pd.to_numeric(context.daily[x_key], errors="coerce")
        y_values = pd.to_numeric(context.daily[y_key], errors="coerce")
        mask = x_values.notna() & y_values.notna() & np.isfinite(x_values) & np.isfinite(y_values)
        rows = pd.DataFrame(
            {
                "date": context.daily.loc[mask, "date"].dt.date.map(date.isoformat),
                "x": x_values.loc[mask].astype(float),
                "y": y_values.loc[mask].astype(float),
            }
        ).reset_index(drop=True)
        if len(rows) < minimum or rows["x"].nunique() < 2 or rows["y"].nunique() < 2:
            continue
        sources.append(
            {
                "key": f"{x_key}_to_{y_key}",
                "x_key": x_key,
                "x_label": x_label,
                "x_unit": x_unit,
                "y_key": y_key,
                "y_label": y_label,
                "y_unit": y_unit,
                "observation_unit": "日",
                "rows": rows,
            }
        )
    return sources


def _choose_correlation(context: _DataContext, rng: np.random.Generator, *, minimum: int) -> dict[str, Any]:
    sources = _correlation_sources(context, minimum=minimum)
    if not sources:
        raise _TemplateUnavailable(f"同じ日にそろった、ばらつきのある2変数が{minimum}組以上必要です。")
    return sources[int(rng.integers(0, len(sources)))]


def _display_numeric_rows(source: dict[str, Any], sampled: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {"no": position + 1, "date": row.date, "value": round(float(row.value), 3)}
        for position, row in enumerate(sampled.itertuples(index=False))
    ]


def _numeric_part(
    key: str,
    label: str,
    expected: float,
    *,
    domain: str,
    decimals: int = 2,
    unit: str = "",
    weight: float = 1.0,
    tolerance: float | None = None,
) -> dict[str, Any]:
    if domain not in _INPUT_DOMAINS:
        raise ValueError("数値解答の入力範囲が不正です。")
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 8:
        raise ValueError("解答の小数桁数は0～8の整数で指定してください。")
    if tolerance is not None and (isinstance(tolerance, bool) or tolerance != 0):
        raise ValueError("新規問題は丸め後の完全一致で採点します。許容差は0です。")
    number = _parse_decimal_answer(expected)
    input_domain = {"kind": domain, **_INPUT_DOMAINS[domain], "allow_percent_suffix": unit == "%"}
    if number is None or _domain_error(number, input_domain):
        raise ValueError("正解の値が入力範囲に一致しません。")
    if input_domain["integer"] and decimals != 0:
        raise ValueError("整数欄の小数桁数は0にしてください。")
    expected_text = format_decimal_answer(number, decimals)
    if not math.isfinite(float(expected_text)):
        raise ValueError("正解は保存可能な有限の数値で指定してください。")
    return {
        "key": key,
        "label": label,
        "kind": "numeric",
        "expected": float(expected_text),
        "expected_text": expected_text,
        "tolerance": 0.0,
        "decimals": int(decimals),
        "contract_version": GRADING_CONTRACT_VERSION,
        "rounding": ROUNDING_RULE,
        "comparison": "rounded_equal",
        "input_domain": input_domain,
        "unit": unit,
        "options": [],
        "weight": float(weight),
    }


def _parse_decimal_answer(value: Any) -> Decimal | None:
    """Use decimal text, never the binary float's exact expansion."""
    if value is None or isinstance(value, (bool, np.bool_)):
        return None
    if isinstance(value, Fraction):
        with localcontext() as context:
            context.prec = 50
            value = Decimal(value.numerator) / Decimal(value.denominator)
    text = unicodedata.normalize("NFKC", str(value)).strip().replace(",", "")
    if text.endswith("%"):
        text = text[:-1].strip()
    if not text or len(text) > 256:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite() or number.adjusted() > 308:
        return None
    return number


def _rational(value: Any) -> Fraction:
    """The displayed finite decimal, not the binary representation beneath it."""
    return Fraction(str(value))


def _exact_mean(values: Any) -> Fraction:
    numbers = [_rational(value) for value in values]
    return sum(numbers, Fraction(0)) / len(numbers)


def _exact_sample_variance(values: Any) -> Fraction:
    numbers = [_rational(value) for value in values]
    mean = sum(numbers, Fraction(0)) / len(numbers)
    return sum(((value - mean) ** 2 for value in numbers), Fraction(0)) / (len(numbers) - 1)


def _rounded_decimal(number: Decimal, decimals: int) -> Decimal:
    with localcontext() as context:
        context.prec = max(32, len(number.as_tuple().digits), max(number.adjusted(), 0) + decimals + 2)
        rounded = number.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    return abs(rounded) if rounded == 0 else rounded


def format_decimal_answer(value: Any, decimals: int = 2) -> str:
    number = _parse_decimal_answer(value)
    if number is None or isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 8:
        raise ValueError("有限の数値と0～8の小数桁数が必要です。")
    return format(_rounded_decimal(number, decimals), f".{decimals}f")


def _domain_error(number: Decimal, domain: dict[str, Any]) -> str:
    if domain["integer"] and number != number.to_integral_value():
        return "整数で入力してください（小数を丸めて人数・件数にはしません）。"
    minimum, maximum = domain["minimum"], domain["maximum"]
    if minimum is not None and number < Decimal(str(minimum)):
        return f"入力範囲外です。{minimum}以上で入力してください。"
    if maximum is not None and number > Decimal(str(maximum)):
        return f"入力範囲外です。{maximum}以下で入力してください。"
    return ""


def numeric_answer_rule(part: dict[str, Any]) -> str:
    """Human-readable rules from the frozen part, including old contracts."""
    if part.get("contract_version") != GRADING_CONTRACT_VERSION:
        return f"旧問題の保存済み規約: 正解との差が許容差 ±{part.get('tolerance', 0)} 以内。入力は再丸めしません。"
    domain = part["input_domain"]
    range_text = "有限の実数"
    if domain["minimum"] is not None and domain["maximum"] is not None:
        range_text = f"{domain['minimum']}～{domain['maximum']}（両端を含む）"
    elif domain["minimum"] is not None:
        range_text = f"{domain['minimum']}以上"
    if domain["integer"]:
        return f"整数のみ、{range_text}。元の入力を検証し、完全一致で採点（許容差0）。"
    return f"小数第{part['decimals']}位、十進四捨五入 ROUND_HALF_UP（半分は0から遠ざける）。元入力: {range_text}。丸め後一致（許容差0）。"


def _choice_part(
    key: str,
    label: str,
    options: list[str],
    expected: str,
    *,
    weight: float = 1.0,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "kind": "choice",
        "options": options,
        "expected": expected,
        "tolerance": None,
        "decimals": None,
        "unit": "",
        "weight": float(weight),
    }


def _assumptions(observation_unit: str) -> list[str]:
    first = (
        "同じ日の複数セッションを含む場合があり、各観測は互いに独立とは限りません。"
        if observation_unit == "学習セッション"
        else "同一人物の日々の反復記録であり、時系列相関があるため、独立な無作為標本とは限りません。"
    )
    return [
        first,
        "欠測は0として補わず、この表に表示された有効な観測だけで計算します。",
        "計算練習上のモデルであり、結果から因果関係は判断しません。",
    ]


def _numeric_assumptions(source: dict[str, Any]) -> list[str]:
    result = _assumptions(str(source.get("observation_unit") or ""))
    if source.get("key") in {"focus_score", "fatigue"}:
        result.append(
            "1～5の順序尺度を、計算練習のため便宜的に等間隔の数値尺度として扱います。"
        )
    return result


def _correlation_assumptions(source: dict[str, Any]) -> list[str]:
    result = _assumptions(str(source.get("observation_unit") or ""))
    if source.get("x_key") in {"focus_score", "fatigue"} or source.get("y_key") in {"focus_score", "fatigue"}:
        result.append(
            "1～5の順序尺度を、相関・回帰の計算練習のため便宜的に等間隔の数値尺度として扱います。"
        )
    return result


def _mean_problem(context: _DataContext, rng: np.random.Generator) -> dict[str, Any]:
    source = _choose_numeric(context, rng, minimum=5)
    sampled = _sample_frame(source["rows"], rng, minimum=5, maximum=10)
    rows = _display_numeric_rows(source, sampled)
    values = np.array([row["value"] for row in rows], dtype=float)
    mean = _exact_mean(values)
    return {
        "prompt": f"表の{source['label']} {len(values)}件の算術平均を求めてください。小数第2位まで答えます。",
        "observation_unit": source["observation_unit"],
        "data_columns": [
            {"key": "no", "label": "番号", "unit": ""},
            {"key": "date", "label": "日付", "unit": ""},
            {"key": "value", "label": source["label"], "unit": source["unit"]},
        ],
        "data_rows": rows,
        "answer_parts": [_numeric_part("mean", "算術平均", mean, domain="real", unit=source["unit"])],
        "hints": ["すべての値の合計を観測数で割ります。", "平均 = Σx / n"],
        "explanation": f"表示値の合計を{len(values)}で割ると、平均は{format_decimal_answer(mean, 2)}{source['unit']}です。",
        "assumptions": _numeric_assumptions(source),
        "source_key": source["key"],
    }


def _sample_sd_problem(context: _DataContext, rng: np.random.Generator) -> dict[str, Any]:
    source = _choose_numeric(context, rng, minimum=6, require_variation=True)
    sampled = _sample_frame(source["rows"], rng, minimum=6, maximum=11)
    rows = _display_numeric_rows(source, sampled)
    values = np.array([row["value"] for row in rows], dtype=float)
    variance = _exact_sample_variance(values)
    deviation = math.sqrt(variance)
    return {
        "prompt": f"表の{source['label']}について、不偏分散（分母 n−1）と標本標準偏差を求めてください。",
        "observation_unit": source["observation_unit"],
        "data_columns": [
            {"key": "no", "label": "番号", "unit": ""},
            {"key": "date", "label": "日付", "unit": ""},
            {"key": "value", "label": source["label"], "unit": source["unit"]},
        ],
        "data_rows": rows,
        "answer_parts": [
            _numeric_part("sample_variance", "不偏分散", variance, domain="nonnegative", unit=f"({source['unit']})²"),
            _numeric_part("sample_sd", "標本標準偏差", deviation, domain="nonnegative", unit=source["unit"]),
        ],
        "hints": ["偏差平方和を n−1 で割ると不偏分散です。", "標本標準偏差は不偏分散の正の平方根です。"],
        "explanation": f"不偏分散は{format_decimal_answer(variance, 2)}、標本標準偏差は{format_decimal_answer(deviation, 2)}{source['unit']}です。",
        "assumptions": _numeric_assumptions(source),
        "source_key": source["key"],
    }


def _category_proportion_problem(context: _DataContext, rng: np.random.Generator) -> dict[str, Any]:
    source = _choose_categorical(context, rng, minimum=8)
    sampled = _sample_frame(source["rows"], rng, minimum=8, maximum=20)
    counts = sampled["category"].value_counts()
    eligible = [str(level) for level, count in counts.items() if 0 < int(count) < len(sampled)]
    if not eligible:
        raise _TemplateUnavailable("割合を計算できる2区分以上の記録が必要です。")
    event = eligible[int(rng.integers(0, len(eligible)))]
    count = int((sampled["category"] == event).sum())
    proportion = Fraction(count, len(sampled)) * 100
    rows = [
        {"no": position + 1, "date": str(row.date), "category": str(row.category)}
        for position, row in enumerate(sampled.itertuples(index=False))
    ]
    return {
        "prompt": f"表の{source['label']}で「{event}」に該当する件数と割合を求めてください。割合は%で小数第2位まで答えます。",
        "observation_unit": source["observation_unit"],
        "data_columns": [
            {"key": "no", "label": "番号", "unit": ""},
            {"key": "date", "label": "日付", "unit": ""},
            {"key": "category", "label": source["label"], "unit": ""},
        ],
        "data_rows": rows,
        "answer_parts": [
            _numeric_part("event_count", "該当件数", count, domain="integer_count", decimals=0, unit="件", tolerance=0),
            _numeric_part("event_percent", "該当割合", proportion, domain="percentage", unit="%"),
        ],
        "hints": ["割合 = 該当件数 ÷ 全件数", "%で答えるときは100を掛けます。"],
        "explanation": f"{len(sampled)}件中{count}件なので、{count}/{len(sampled)}×100 = {format_decimal_answer(proportion, 2)}%です。",
        "assumptions": _assumptions(source["observation_unit"]),
        "source_key": source["key"],
    }


def _conditional_probability_problem(context: _DataContext, rng: np.random.Generator) -> dict[str, Any]:
    source = _choose_categorical_pair(context, rng, minimum=10)
    sampled = _sample_frame(source["rows"], rng, minimum=10, maximum=24)
    candidates: list[tuple[str, str, int, int]] = []
    for condition in sampled["x"].drop_duplicates():
        conditioned = sampled.loc[sampled["x"] == condition]
        if len(conditioned) < 3:
            continue
        for event in sampled["y"].drop_duplicates():
            numerator = int((conditioned["y"] == event).sum())
            if 0 < numerator < len(conditioned):
                candidates.append((str(condition), str(event), numerator, len(conditioned)))
    if not candidates:
        raise _TemplateUnavailable("条件付き確率の分子・分母を作れる組合せが不足しています。")
    condition, event, numerator, denominator = candidates[int(rng.integers(0, len(candidates)))]
    probability = Fraction(numerator, denominator) * 100
    rows = [
        {"no": position + 1, "date": str(row.date), "x": str(row.x), "y": str(row.y)}
        for position, row in enumerate(sampled.itertuples(index=False))
    ]
    return {
        "prompt": f"{source['x_label']}が「{condition}」であるという条件の下で、{source['y_label']}が「{event}」である条件付き確率を求めてください。",
        "observation_unit": source["observation_unit"],
        "data_columns": [
            {"key": "no", "label": "番号", "unit": ""},
            {"key": "date", "label": "日付", "unit": ""},
            {"key": "x", "label": source["x_label"], "unit": ""},
            {"key": "y", "label": source["y_label"], "unit": ""},
        ],
        "data_rows": rows,
        "answer_parts": [
            _numeric_part("denominator", "条件に該当する件数（分母）", denominator, domain="integer_count", decimals=0, unit="件", tolerance=0),
            _numeric_part("numerator", "両方に該当する件数（分子）", numerator, domain="integer_count", decimals=0, unit="件", tolerance=0),
            _numeric_part("conditional_percent", "条件付き確率", probability, domain="percentage", unit="%"),
        ],
        "hints": ["まず条件に該当する行だけに絞ります。", "条件付き確率 = 両方に該当する件数 ÷ 条件に該当する件数"],
        "explanation": f"条件に該当する{denominator}件中、目的の事象は{numerator}件なので、確率は{format_decimal_answer(probability, 2)}%です。",
        "assumptions": _assumptions(source["observation_unit"]),
        "source_key": source["key"],
    }


def _binomial_moments_problem(context: _DataContext, rng: np.random.Generator) -> dict[str, Any]:
    source = _choose_categorical(context, rng, minimum=10)
    sampled = _sample_frame(source["rows"], rng, minimum=10, maximum=24)
    counts = sampled["category"].value_counts()
    eligible = [str(level) for level, count in counts.items() if 0 < int(count) < len(sampled)]
    if not eligible:
        raise _TemplateUnavailable("0と1の間の経験割合を作れるカテゴリ記録が必要です。")
    event = eligible[int(rng.integers(0, len(eligible)))]
    count = int((sampled["category"] == event).sum())
    probability = Fraction(count, len(sampled))
    trial_options = (5, 8, 10, 12, 20)
    trials = trial_options[int(rng.integers(0, len(trial_options)))]
    expectation = trials * probability
    variance = trials * probability * (1 - probability)
    rows = [
        {"no": position + 1, "date": str(row.date), "category": str(row.category)}
        for position, row in enumerate(sampled.itertuples(index=False))
    ]
    return {
        "prompt": (
            f"表から「{event}」の経験確率 p を求め、そのpが一定で各試行が独立と仮定した二項分布 "
            f"X~Bin({trials}, p) の期待値と分散を求めてください。"
        ),
        "observation_unit": source["observation_unit"],
        "data_columns": [
            {"key": "no", "label": "番号", "unit": ""},
            {"key": "date", "label": "日付", "unit": ""},
            {"key": "category", "label": source["label"], "unit": ""},
        ],
        "data_rows": rows,
        "answer_parts": [
            _numeric_part("p_percent", "経験確率 p", probability * 100, domain="percentage", unit="%"),
            _numeric_part("expected_count", "期待値 E[X]", expectation, domain="nonnegative", unit="回"),
            _numeric_part("variance", "分散 Var[X]", variance, domain="nonnegative", unit="回²"),
        ],
        "hints": ["p = 該当件数 ÷ 全件数", "E[X] = np", "Var[X] = np(1−p)"],
        "explanation": (
            f"p={count}/{len(sampled)}={format_decimal_answer(probability, 4)}です。したがってE[X]={format_decimal_answer(expectation, 2)}、"
            f"Var[X]={format_decimal_answer(variance, 2)}です。"
        ),
        "assumptions": _assumptions(source["observation_unit"])
        + ["二項モデルでは、各試行が独立で成功確率pが一定だと仮定します。実際の反復記録ではこの仮定が崩れる可能性があります。"],
        "source_key": source["key"],
    }


def _z_score_problem(context: _DataContext, rng: np.random.Generator) -> dict[str, Any]:
    source = _choose_numeric(context, rng, minimum=7, require_variation=True)
    sampled = _sample_frame(source["rows"], rng, minimum=7, maximum=12)
    rows = _display_numeric_rows(source, sampled)
    target_position = int(rng.integers(0, len(rows)))
    for position, row in enumerate(rows):
        row["target"] = "対象" if position == target_position else ""
    values = np.array([row["value"] for row in rows], dtype=float)
    mean = _exact_mean(values)
    deviation = math.sqrt(_exact_sample_variance(values))
    if deviation <= 0:
        raise _TemplateUnavailable("z得点を計算するためのばらつきがありません。")
    target_value = values[target_position]
    z_score = float(_rational(target_value) - mean) / deviation
    return {
        "prompt": f"表全体の平均と標本標準偏差（分母 n−1）を使い、「対象」と記した{source['label']}のz得点を求めてください。",
        "observation_unit": source["observation_unit"],
        "data_columns": [
            {"key": "no", "label": "番号", "unit": ""},
            {"key": "date", "label": "日付", "unit": ""},
            {"key": "value", "label": source["label"], "unit": source["unit"]},
            {"key": "target", "label": "対象", "unit": ""},
        ],
        "data_rows": rows,
        "answer_parts": [
            _numeric_part("mean", "標本平均", mean, domain="real", unit=source["unit"]),
            _numeric_part("sample_sd", "標本標準偏差", deviation, domain="nonnegative", unit=source["unit"]),
            _numeric_part("z_score", "z得点", z_score, domain="real", unit=""),
        ],
        "hints": ["z = (対象値 − 平均) ÷ 標本標準偏差", "標準偏差は分母 n−1 で計算します。"],
        "explanation": f"平均{format_decimal_answer(mean, 2)}、標本標準偏差{format_decimal_answer(deviation, 2)}より、z得点は{format_decimal_answer(z_score, 2)}です。",
        "assumptions": _numeric_assumptions(source),
        "source_key": source["key"],
    }


def _mean_ci_problem(context: _DataContext, rng: np.random.Generator) -> dict[str, Any]:
    source = _choose_numeric(context, rng, minimum=10, require_variation=True)
    sampled = _sample_frame(source["rows"], rng, minimum=10, maximum=20)
    rows = _display_numeric_rows(source, sampled)
    values = np.array([row["value"] for row in rows], dtype=float)
    mean = _exact_mean(values)
    deviation = math.sqrt(_exact_sample_variance(values))
    standard_error = deviation / math.sqrt(len(values))
    lower = float(mean) - 1.96 * standard_error
    upper = float(mean) + 1.96 * standard_error
    return {
        "prompt": (
            f"表の{source['label']}について、標本平均、平均の標準誤差、正規近似による95%信頼区間を求めてください。"
            "標本標準偏差は分母 n−1、係数は1.96を使います。"
        ),
        "observation_unit": source["observation_unit"],
        "data_columns": [
            {"key": "no", "label": "番号", "unit": ""},
            {"key": "date", "label": "日付", "unit": ""},
            {"key": "value", "label": source["label"], "unit": source["unit"]},
        ],
        "data_rows": rows,
        "answer_parts": [
            _numeric_part("mean", "標本平均", mean, domain="real", unit=source["unit"]),
            _numeric_part("standard_error", "標準誤差", standard_error, domain="nonnegative", unit=source["unit"]),
            _numeric_part("ci_lower", "95%信頼区間 下限", lower, domain="real", unit=source["unit"]),
            _numeric_part("ci_upper", "95%信頼区間 上限", upper, domain="real", unit=source["unit"]),
        ],
        "hints": ["SE = s / √n", "95%信頼区間 = 平均 ± 1.96×SE"],
        "explanation": (
            f"n={len(values)}、平均={format_decimal_answer(mean, 2)}、SE={format_decimal_answer(standard_error, 2)}なので、"
            f"95%信頼区間は{format_decimal_answer(lower, 2)}～{format_decimal_answer(upper, 2)}{source['unit']}です。"
        ),
        "assumptions": _numeric_assumptions(source)
        + ["この区間は独立性と正規近似を仮定した計算練習です。反復時系列への厳密な区間推定ではありません。"],
        "source_key": source["key"],
    }


def _proportion_ci_problem(context: _DataContext, rng: np.random.Generator) -> dict[str, Any]:
    source = _choose_categorical(context, rng, minimum=12)
    sampled = _sample_frame(source["rows"], rng, minimum=12, maximum=30)
    counts = sampled["category"].value_counts()
    eligible = [
        str(level)
        for level, count in counts.items()
        if int(count) >= 2 and len(sampled) - int(count) >= 2
    ]
    if not eligible:
        raise _TemplateUnavailable("割合の正規近似に必要な両区分の件数が不足しています。")
    event = eligible[int(rng.integers(0, len(eligible)))]
    count = int((sampled["category"] == event).sum())
    proportion = Fraction(count, len(sampled))
    standard_error = math.sqrt(proportion * (1 - proportion) / len(sampled))
    lower = proportion - 1.96 * standard_error
    upper = proportion + 1.96 * standard_error
    rows = [
        {"no": position + 1, "date": str(row.date), "category": str(row.category)}
        for position, row in enumerate(sampled.itertuples(index=False))
    ]
    return {
        "prompt": (
            f"表における「{event}」の標本割合と標準誤差、正規近似による95%信頼区間を求めてください。"
            "すべて%（またはパーセントポイント）で答え、区間は0～100%に切り詰めません。"
        ),
        "observation_unit": source["observation_unit"],
        "data_columns": [
            {"key": "no", "label": "番号", "unit": ""},
            {"key": "date", "label": "日付", "unit": ""},
            {"key": "category", "label": source["label"], "unit": ""},
        ],
        "data_rows": rows,
        "answer_parts": [
            _numeric_part("sample_percent", "標本割合", proportion * 100, domain="percentage", unit="%"),
            _numeric_part("standard_error_pp", "標準誤差", standard_error * 100, domain="nonnegative", unit="ポイント"),
            _numeric_part("ci_lower_percent", "95%信頼区間 下限", lower * 100, domain="real", unit="%"),
            _numeric_part("ci_upper_percent", "95%信頼区間 上限", upper * 100, domain="real", unit="%"),
        ],
        "hints": ["p̂ = 該当件数 ÷ n", "SE = √{p̂(1−p̂)/n}", "95%信頼区間 = p̂ ± 1.96×SE"],
        "explanation": (
            f"p̂={count}/{len(sampled)}={format_decimal_answer(proportion, 4)}、SE={format_decimal_answer(standard_error, 4)}なので、"
            f"95%信頼区間は{format_decimal_answer(lower * 100, 2)}%～{format_decimal_answer(upper * 100, 2)}%です。"
        ),
        "assumptions": _assumptions(source["observation_unit"])
        + ["正規近似を使った計算練習であり、小標本や極端な割合では近似が不安定になります。"],
        "source_key": source["key"],
    }


def _contingency_problem(context: _DataContext, rng: np.random.Generator) -> dict[str, Any]:
    source = _choose_categorical_pair(context, rng, minimum=12)
    rows = source["rows"]
    top_x = [str(value) for value in rows["x"].value_counts().index[:2]]
    top_y = [str(value) for value in rows["y"].value_counts().index[:2]]
    filtered = rows.loc[rows["x"].isin(top_x) & rows["y"].isin(top_y)].copy()
    if len(filtered) < 8 or len(top_x) < 2 or len(top_y) < 2:
        raise _TemplateUnavailable("2×2分割表を作るためのカテゴリ件数が不足しています。")
    table = pd.crosstab(filtered["x"], filtered["y"]).reindex(index=top_x, columns=top_y, fill_value=0)
    target_x = top_x[int(rng.integers(0, 2))]
    target_y = top_y[int(rng.integers(0, 2))]
    observed = int(table.at[target_x, target_y])
    row_total = int(table.loc[target_x].sum())
    column_total = int(table[target_y].sum())
    total = int(table.to_numpy().sum())
    row_percent = Fraction(observed, row_total) * 100
    expected = Fraction(row_total * column_total, total)
    data_rows = [
        {"row_category": row_level, "column_category": column_level, "count": int(table.at[row_level, column_level])}
        for row_level in top_x
        for column_level in top_y
    ]
    return {
        "prompt": (
            f"2×2表について、行「{target_x}」のうち列「{target_y}」が占める行割合と、"
            "2変数が独立だと仮定した同セルの期待度数を求めてください。"
        ),
        "observation_unit": source["observation_unit"],
        "data_columns": [
            {"key": "row_category", "label": source["x_label"], "unit": ""},
            {"key": "column_category", "label": source["y_label"], "unit": ""},
            {"key": "count", "label": "度数", "unit": "件"},
        ],
        "data_rows": data_rows,
        "answer_parts": [
            _numeric_part("row_percent", "行割合", row_percent, domain="percentage", unit="%"),
            _numeric_part("expected_count", "独立を仮定した期待度数", expected, domain="nonnegative", unit="件"),
        ],
        "hints": ["行割合 = 観測度数 ÷ その行の合計", "期待度数 = 行合計×列合計÷総数"],
        "explanation": (
            f"行割合は{observed}/{row_total}×100={format_decimal_answer(row_percent, 2)}%です。期待度数は"
            f"{row_total}×{column_total}/{total}={format_decimal_answer(expected, 2)}件です。"
        ),
        "assumptions": _assumptions(source["observation_unit"])
        + ["期待度数が小さいセルがある場合、χ²近似の適否は別途確認が必要です。"],
        "source_key": source["key"],
    }


def _display_pair_rows(source: dict[str, Any], sampled: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {
            "no": position + 1,
            "date": str(row.date),
            "x": round(float(row.x), 3),
            "y": round(float(row.y), 3),
        }
        for position, row in enumerate(sampled.itertuples(index=False))
    ]


def _pair_columns(source: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"key": "no", "label": "番号", "unit": ""},
        {"key": "date", "label": "日付", "unit": ""},
        {"key": "x", "label": source["x_label"], "unit": source["x_unit"]},
        {"key": "y", "label": source["y_label"], "unit": source["y_unit"]},
    ]


def _pair_values(rows: list[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray]:
    return (
        np.array([row["x"] for row in rows], dtype=float),
        np.array([row["y"] for row in rows], dtype=float),
    )


def _sample_pair_rows(
    source: dict[str, Any],
    rng: np.random.Generator,
    *,
    minimum: int = 8,
    maximum: int = 16,
) -> tuple[list[dict[str, Any]], np.ndarray, np.ndarray]:
    for _ in range(8):
        sampled = _sample_frame(source["rows"], rng, minimum=minimum, maximum=maximum)
        rows = _display_pair_rows(source, sampled)
        x, y = _pair_values(rows)
        if (
            len(x) >= minimum
            and np.isfinite(x).all()
            and np.isfinite(y).all()
            and np.unique(x).size >= 2
            and np.unique(y).size >= 2
        ):
            return rows, x, y
    raise _TemplateUnavailable("抽出後の2変数に相関・回帰を計算できるばらつきがありません。")


def _pearson_problem(context: _DataContext, rng: np.random.Generator) -> dict[str, Any]:
    source = _choose_correlation(context, rng, minimum=8)
    rows, x, y = _sample_pair_rows(source, rng)
    correlation = float(np.corrcoef(x, y)[0, 1])
    if not math.isfinite(correlation):
        raise _TemplateUnavailable("抽出後のデータから有限の相関係数を計算できません。")
    return {
        "prompt": f"表の{source['x_label']}と{source['y_label']}のPearson相関係数 r を求めてください。",
        "observation_unit": source["observation_unit"],
        "data_columns": _pair_columns(source),
        "data_rows": rows,
        "answer_parts": [_numeric_part("pearson_r", "Pearson相関係数 r", correlation, domain="correlation", unit="")],
        "hints": ["r = 共分散 ÷（xの標準偏差×yの標準偏差）", "rの範囲は−1から1です。"],
        "explanation": f"表示された{len(rows)}組から計算したPearson相関係数は r={format_decimal_answer(correlation, 2)}です。",
        "assumptions": _correlation_assumptions(source)
        + ["Pearson相関は線形な関連を要約する指標で、外れ値の影響を受けます。"],
        "source_key": source["key"],
    }


def _regression_problem(context: _DataContext, rng: np.random.Generator) -> dict[str, Any]:
    source = _choose_correlation(context, rng, minimum=8)
    rows, x, y = _sample_pair_rows(source, rng)
    x_mean, y_mean = _exact_mean(x), _exact_mean(y)
    x_centered = [_rational(value) - x_mean for value in x]
    y_centered = [_rational(value) - y_mean for value in y]
    denominator = sum((value ** 2 for value in x_centered), Fraction(0))
    if denominator <= 0:
        raise _TemplateUnavailable("回帰係数を計算するための説明変数のばらつきがありません。")
    slope = sum((a * b for a, b in zip(x_centered, y_centered)), Fraction(0)) / denominator
    intercept = y_mean - slope * x_mean
    return {
        "prompt": (
            f"{source['x_label']}をx、{source['y_label']}をyとして、最小二乗法による単回帰式 y=a+bx の"
            "傾きbと切片aを求めてください。"
        ),
        "observation_unit": source["observation_unit"],
        "data_columns": _pair_columns(source),
        "data_rows": rows,
        "answer_parts": [
            _numeric_part("slope", "傾き b", slope, domain="real", unit=f"{source['y_unit']}/{source['x_unit']}"),
            _numeric_part("intercept", "切片 a", intercept, domain="real", unit=source["y_unit"]),
        ],
        "hints": ["b = Σ(x−x̄)(y−ȳ) ÷ Σ(x−x̄)²", "a = ȳ − bx̄"],
        "explanation": f"最小二乗法で計算すると、回帰式は y={format_decimal_answer(intercept, 2)}+({format_decimal_answer(slope, 2)})x です。",
        "assumptions": _correlation_assumptions(source)
        + ["単回帰式は表示範囲の線形関連を記述するもので、未調整の交絡や時間順序を扱いません。"],
        "source_key": source["key"],
    }


def _correlation_interpretation_problem(context: _DataContext, rng: np.random.Generator) -> dict[str, Any]:
    source = _choose_correlation(context, rng, minimum=8)
    rows, x, y = _sample_pair_rows(source, rng)
    correlation = float(np.corrcoef(x, y)[0, 1])
    if not math.isfinite(correlation):
        raise _TemplateUnavailable("抽出後のデータから有限の相関係数を計算できません。")
    correct = "線形な関連は示すが、これだけでは因果関係までは示さない"
    options = [
        correct,
        "相関係数が0でなければ、xがyを変化させたと断定できる",
        "同一人物の時系列データなら、交絡や時間傾向を考慮しなくてよい",
    ]
    return {
        "prompt": (
            f"表からPearson相関係数を計算し、この{source['x_label']}と{source['y_label']}の結果について"
            "最も適切な解釈を選んでください。"
        ),
        "observation_unit": source["observation_unit"],
        "data_columns": _pair_columns(source),
        "data_rows": rows,
        "answer_parts": [
            _numeric_part("pearson_r", "Pearson相関係数 r", correlation, domain="correlation", unit=""),
            _choice_part("interpretation", "最も適切な解釈", options, correct),
        ],
        "hints": ["相関係数は線形な関連の向きと強さを要約します。", "相関と因果は別の概念です。"],
        "explanation": (
            f"相関係数は r={format_decimal_answer(correlation, 2)}です。ただし同一人物の反復時系列であり、曜日、時間傾向、"
            "他の生活条件などを調整していないため、因果関係は断定できません。"
        ),
        "assumptions": _correlation_assumptions(source),
        "source_key": source["key"],
    }


from app_core.statistics_practice_tests import (
    chi_square_problem,
    fisher_exact_problem,
    one_sample_t_problem,
    paired_t_problem,
    welch_t_problem,
    wilcoxon_problem,
)


_TEMPLATES: tuple[_Template, ...] = (
    _Template("descriptive_mean", "descriptive", "算術平均", _mean_problem),
    _Template("descriptive_sample_sd", "descriptive", "不偏分散と標本標準偏差", _sample_sd_problem),
    _Template("category_proportion", "probability", "件数と割合", _category_proportion_problem),
    _Template("conditional_probability", "probability", "条件付き確率", _conditional_probability_problem),
    _Template("binomial_moments", "distribution", "二項分布の期待値と分散", _binomial_moments_problem),
    _Template("z_score", "distribution", "z得点", _z_score_problem),
    _Template("mean_se_ci", "inference", "平均の標準誤差と95%信頼区間", _mean_ci_problem),
    _Template("proportion_ci", "inference", "割合の標準誤差と95%信頼区間", _proportion_ci_problem),
    _Template("contingency_expected", "categorical", "分割表の行割合と期待度数", _contingency_problem),
    _Template("pearson_correlation", "correlation", "Pearson相関係数", _pearson_problem),
    _Template("simple_regression", "correlation", "単回帰の傾きと切片", _regression_problem),
    _Template("correlation_interpretation", "correlation", "相関の計算と解釈", _correlation_interpretation_problem),
    _Template("one_sample_t_test", "testing", "1標本t検定", one_sample_t_problem),
    _Template("welch_t_test", "testing", "独立2群のWelchのt検定", welch_t_problem),
    _Template("paired_t_test", "testing", "対応のあるt検定", paired_t_problem),
    _Template("chi_square_independence", "testing", "χ²独立性検定", chi_square_problem),
    _Template("fisher_exact_test", "testing", "Fisherの正確確率検定", fisher_exact_problem),
    _Template("wilcoxon_signed_rank", "nonparametric", "Wilcoxon符号付順位検定", wilcoxon_problem),
)


from app_core.statistics_practice_supplement import SUPPLEMENT_SPECS

_TEMPLATES += tuple(_Template(*spec, data_origin="synthetic") for spec in SUPPLEMENT_SPECS)


def _seed_for_template(seed: int, template_id: str) -> int:
    digest = hashlib.sha256(f"{seed}:{template_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def _resolved_seed(seed: int | None) -> int:
    if seed is None:
        return secrets.randbits(63)
    if isinstance(seed, bool):
        raise ValueError("seed は整数で指定してください。")
    try:
        return int(seed) % (2**63)
    except (TypeError, ValueError) as exc:
        raise ValueError("seed は整数で指定してください。") from exc


def _build_payload(template: _Template, context: _DataContext, seed: int) -> dict[str, Any]:
    rng = np.random.default_rng(_seed_for_template(seed, template.template_id))
    return template.builder(context, rng)


def _finalize_problem(
    template: _Template,
    payload: dict[str, Any],
    context: _DataContext,
    seed: int,
) -> dict[str, Any]:
    period = {"start_date": context.start_date.isoformat(), "end_date": context.end_date.isoformat()}
    data_columns = _ensure_json(payload.get("data_columns", []))
    data_rows = _ensure_json(payload.get("data_rows", []))
    answer_parts = _ensure_json(payload.get("answer_parts", []))
    choice_rng = np.random.default_rng(_seed_for_template(seed, f"{template.template_id}:choices"))
    for part in answer_parts:
        if part.get("kind") == "choice":
            choice_rng.shuffle(part["options"])
        elif part.get("kind") == "numeric":
            _validate_numeric_contract(part)
    source_digest = hashlib.sha256(
        json.dumps({"columns": data_columns, "rows": data_rows}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    answer_key = [
        {
            key: part.get(key)
            for key in ("key", "kind", "expected", "expected_text", "tolerance", "decimals", "rounding", "comparison", "input_domain", "contract_version", "unit", "options", "weight")
            if key in part
        }
        for part in answer_parts
    ]
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "grading_contract_version": GRADING_CONTRACT_VERSION,
        "seed": seed,
        "period": period,
        "template_id": template.template_id,
        "source_key": payload.get("source_key", ""),
        "observation_unit": payload.get("observation_unit", ""),
        "data_columns": data_columns,
        "data_rows": data_rows,
        "answer_key": answer_key,
        "rounding": {
            part["key"]: {key: part[key] for key in ("decimals", "tolerance", "rounding", "comparison", "input_domain", "contract_version")}
            for part in answer_parts
            if part.get("kind") == "numeric"
        },
        "source_digest": source_digest,
    }
    # Only additive metadata: legacy phase-one snapshots remain readable.
    metadata = {
        "data_origin": template.data_origin,
        "data_origin_label": "補助用の架空データ" if template.data_origin == "synthetic" else "PROLOAGの実ログ",
    }
    for key in ("parameters", "calculation_spec", "result_chart", "selection"):
        if key in payload:
            metadata[key] = _ensure_json(payload[key])
    snapshot.update(metadata)
    identity = hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    ).hexdigest()[:24]
    problem = {
        "problem_id": f"stat_problem_{identity}",
        "template_id": template.template_id,
        "topic": template.topic,
        "topic_label": TOPIC_LABELS[template.topic],
        "title": template.title,
        "prompt": str(payload.get("prompt", "")) + ("\n\n" + NUMERIC_RULE if any(part.get("kind") == "numeric" for part in answer_parts) else ""),
        "observation_unit": payload.get("observation_unit", ""),
        "period": period,
        "data_columns": data_columns,
        "data_rows": data_rows,
        "answer_parts": answer_parts,
        "hints": payload.get("hints", []),
        "explanation": str(payload.get("explanation", "")) + (
            "\n\n指定桁の正解: " + "、".join(f"{part['label']} = {part['expected_text']}{part.get('unit', '')}"
                                             for part in answer_parts if part.get("kind") == "numeric")
            if any(part.get("kind") == "numeric" for part in answer_parts) else ""),
        "assumptions": payload.get("assumptions", []),
        "snapshot": snapshot,
    }
    problem.update(metadata)
    return _ensure_json(problem)


def _matching_templates(data_mode: str, topic_key: str = "mixed") -> list[_Template]:
    if data_mode not in {"personal", "synthetic", "all"}:
        raise ValueError("data_mode は personal / synthetic / all から選んでください。")
    if topic_key != "mixed" and topic_key not in TOPIC_LABELS:
        raise ValueError("未対応の単元です。")
    origin = {"personal": "personal_logs", "synthetic": "synthetic"}.get(data_mode)
    return [
        template for template in _TEMPLATES
        if (origin is None or template.data_origin == origin)
        and (topic_key == "mixed" or template.topic == topic_key)
    ]


def template_catalog(data_mode: str = "all", topic_key: str = "mixed") -> list[dict[str, Any]]:
    """Read the catalog without inspecting logs or creating files."""
    return [
        {"template_id": item.template_id, "topic": item.topic,
         "topic_label": TOPIC_LABELS[item.topic], "title": item.title,
         "data_origin": item.data_origin}
        for item in _matching_templates(data_mode, topic_key)
    ]


def practice_progress(
    attempts: list[dict[str, Any]] | None = None,
    root: Path | None = None,
) -> list[dict[str, Any]]:
    """Prioritize recent weaknesses, with a moderate weight for unseen topics."""
    records = load_attempts(root=root) if attempts is None else attempts
    grouped: dict[str, list[tuple[str, float]]] = {}
    for attempt in records:
        if not isinstance(attempt, dict):
            continue
        grade = attempt.get("grade") if isinstance(attempt.get("grade"), dict) else {}
        score = _parse_numeric_answer(grade.get("score", attempt.get("score")))
        if score is None or not 0 <= score <= 100:
            continue
        grouped.setdefault(str(attempt.get("template_id") or ""), []).append(
            (str(attempt.get("attempted_at") or attempt.get("date") or ""), score)
        )
    result = []
    for item in template_catalog():
        history = sorted(grouped.get(item["template_id"], []), key=lambda row: row[0], reverse=True)
        recent = history[:5]
        average = sum(score for _, score in recent) / len(recent) if recent else None
        result.append({
            **item, "attempt_count": len(history), "recent_count": len(recent),
            "average_score": round(average, 1) if average is not None else None,
            "weight": 3.0 if average is None else 1.0 + (100.0 - average) / 20.0,
            "last_attempted_at": recent[0][0] if recent else "",
        })
    return result


def generate_problem(
    start_date: date | datetime | str,
    end_date: date | datetime | str,
    topic_key: str = "mixed",
    seed: int | None = None,
    root: Path | None = None,
    *,
    data_mode: str = "personal",
    template_id: str | None = None,
    selection_mode: str = "random",
) -> dict[str, Any]:
    """Generate one frozen, JSON-serializable exercise without modifying source logs."""
    start, end = _period(start_date, end_date)
    topic = str(topic_key or "mixed").strip().lower()
    if topic != "mixed" and topic not in TOPIC_LABELS:
        raise ValueError(f"topic_key は mixed または {', '.join(TOPIC_LABELS)} から選んでください。")
    resolved_seed = _resolved_seed(seed)
    if selection_mode not in {"random", "weak"}:
        raise ValueError("selection_mode は random / weak から選んでください。")
    templates = _matching_templates(data_mode, topic)
    if template_id:
        templates = [item for item in templates if item.template_id == template_id]
        if not templates:
            raise ValueError("指定の問題タイプは選択したデータ・単元に含まれません。")
    context = (
        _load_context(start, end, root)
        if any(item.data_origin == "personal_logs" for item in templates)
        else _DataContext(start, end, pd.DataFrame(), pd.DataFrame())
    )
    candidates: list[tuple[_Template, dict[str, Any]]] = []
    reasons: list[str] = []
    for template in templates:
        try:
            candidates.append((template, _build_payload(template, context, resolved_seed)))
        except _TemplateUnavailable as exc:
            reasons.append(f"{template.title}: {exc}")
    if not candidates:
        detail = " / ".join(reasons[:4]) or "対象期間に有効な記録がありません。"
        raise NoAvailableProblemError(f"この条件で作成できる統計問題がありません。{detail}")
    selector = np.random.default_rng(resolved_seed)
    progress = None
    if selection_mode == "weak" and not template_id:
        progress = {item["template_id"]: item for item in practice_progress(root=root)}
        weights = np.array([progress[item.template_id]["weight"] for item, _ in candidates], dtype=float)
        index = int(selector.choice(len(candidates), p=weights / weights.sum()))
    else:
        index = int(selector.integers(0, len(candidates)))
    template, payload = candidates[index]
    if selection_mode == "weak":
        item = progress[template.template_id] if progress else None
        payload["selection"] = {
            "mode": "weak", "recent_count": item["recent_count"] if item else 0,
            "average_score": item["average_score"] if item else None,
            "reason": "問題タイプを指定して出題" if template_id else (
                "未演習の問題タイプを練習" if not item["recent_count"]
                else f"直近{item['recent_count']}回の平均得点 {format_decimal_answer(item['average_score'], 1)}%をもとに出題"
            ),
        }
    return _finalize_problem(template, payload, context, resolved_seed)


def available_problem_summary(
    start_date: date | datetime | str,
    end_date: date | datetime | str,
    root: Path | None = None,
    *,
    data_mode: str = "personal",
) -> dict[str, Any]:
    """Report template availability and data coverage without writing anything."""
    start, end = _period(start_date, end_date)
    templates = _matching_templates(data_mode)
    context = (
        _load_context(start, end, root)
        if any(item.data_origin == "personal_logs" for item in templates)
        else _DataContext(start, end, pd.DataFrame(), pd.DataFrame())
    )
    template_rows: list[dict[str, Any]] = []
    topic_counts = {key: 0 for key in TOPIC_LABELS}
    for template in templates:
        try:
            payload = _build_payload(template, context, 0)
            available = True
            reason = ""
            eligible_samples = len(payload.get("data_rows", []))
            topic_counts[template.topic] += 1
        except _TemplateUnavailable as exc:
            available = False
            reason = str(exc)
            eligible_samples = 0
        template_rows.append(
            {
                "template_id": template.template_id,
                "topic": template.topic,
                "topic_label": TOPIC_LABELS[template.topic],
                "title": template.title,
                "data_origin": template.data_origin,
                "available": available,
                "eligible_samples": eligible_samples,
                "reason": reason,
            }
        )

    daily = context.daily
    sessions = context.sessions
    coverage_columns = (
        "study_hours",
        "sleep_hours",
        "focus_score",
        "fatigue",
        "running_distance_km",
        "running_duration_min",
    )
    numeric_coverage = {
        column: int(pd.to_numeric(daily[column], errors="coerce").notna().sum()) if column in daily else 0
        for column in coverage_columns
    }
    numeric_coverage["session_minutes"] = int(
        pd.to_numeric(sessions.get("session_minutes", pd.Series(dtype=float)), errors="coerce").notna().sum()
    )
    categorical_coverage = {
        column: int(sessions[column].notna().sum()) if column in sessions else 0
        for column in ("environment", "io_type", "study_category")
    }
    issues: list[str] = []
    if numeric_coverage["study_hours"] == 0 and numeric_coverage["session_minutes"] == 0:
        issues.append("対象期間に学習時間の有効な記録がありません。")
    if not any(numeric_coverage[column] for column in ("sleep_hours", "focus_score", "fatigue")):
        issues.append("対象期間に睡眠・体調の有効な数値記録がありません。")
    if not any(categorical_coverage.values()):
        issues.append("対象期間に演習へ利用できる学習カテゴリ記録がありません。")
    result = {
        "data_mode": data_mode,
        "period": {"start_date": start.isoformat(), "end_date": end.isoformat()},
        "available_count": sum(1 for item in template_rows if item["available"]),
        "total_templates": len(template_rows),
        "templates": template_rows,
        "topic_counts": topic_counts,
        "data_summary": {
            "calendar_days": len(daily),
            "study_days": numeric_coverage["study_hours"],
            "session_rows": len(sessions),
            "numeric_coverage": numeric_coverage,
            "categorical_coverage": categorical_coverage,
        },
        "issues": [] if data_mode == "synthetic" else issues,
    }
    return _ensure_json(result)


def _parse_numeric_answer(value: Any) -> float | None:
    if value is None or isinstance(value, (bool, np.bool_)):
        return None
    if isinstance(value, (int, float, np.number)):
        try:
            number = float(value)
        except (ValueError, OverflowError, TypeError):
            return None
        return number if math.isfinite(number) else None
    text = unicodedata.normalize("NFKC", str(value)).strip().replace(",", "")
    if text.endswith("%"):
        text = text[:-1].strip()
    if not text:
        return None
    try:
        number = float(text)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _validate_numeric_contract(part: dict[str, Any]) -> Decimal:
    decimals, domain = part.get("decimals"), part.get("input_domain")
    if (part.get("contract_version") != GRADING_CONTRACT_VERSION
            or part.get("rounding") != ROUNDING_RULE or part.get("comparison") != "rounded_equal"
            or isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 8
            or _parse_decimal_answer(part.get("tolerance")) != Decimal(0)
            or not isinstance(domain, dict) or not {"kind", "integer", "minimum", "maximum"}.issubset(domain)
            or not isinstance(domain["integer"], bool)):
        raise ValueError("保存済みの数値採点規約が不正です。")
    for bound in (domain["minimum"], domain["maximum"]):
        if bound is not None and _parse_decimal_answer(bound) is None:
            raise ValueError("保存済みの入力範囲が不正です。")
    if domain["integer"] and decimals != 0:
        raise ValueError("整数欄の採点規約が不正です。")
    expected = _parse_decimal_answer(part.get("expected_text"))
    if (expected is None or expected != _parse_decimal_answer(part.get("expected"))
            or expected != _rounded_decimal(expected, decimals) or _domain_error(expected, domain)):
        raise ValueError("保存済みの正解が採点規約に一致しません。")
    return expected


def grade_problem(problem: dict[str, Any], answers: dict[str, Any]) -> dict[str, Any]:
    """Grade only against the frozen problem; source logs are never reloaded."""
    if not isinstance(problem, dict) or not isinstance(problem.get("answer_parts"), list):
        raise ValueError("問題データの形式が不正です。")
    if not isinstance(answers, dict):
        raise ValueError("answers は辞書で指定してください。")
    results: list[dict[str, Any]] = []
    earned_points = 0.0
    max_points = 0.0
    correct_count = 0
    for part in problem["answer_parts"]:
        if not isinstance(part, dict) or not part.get("key"):
            raise ValueError("解答欄の形式が不正です。")
        key = str(part["key"])
        label = str(part.get("label") or key)
        kind = str(part.get("kind") or "")
        weight = max(float(part.get("weight", 1.0) or 0.0), 0.0)
        answer = answers.get(key)
        max_points += weight
        if kind == "numeric":
            domain_error = ""
            if "contract_version" in part:
                expected = _validate_numeric_contract(part)
                parsed = _parse_decimal_answer(answer)
                domain_error = _domain_error(parsed, part["input_domain"]) if parsed is not None else ""
                if (unicodedata.normalize("NFKC", str(answer)).strip().endswith("%")
                        and not part["input_domain"].get("allow_percent_suffix", False)):
                    domain_error = "この欄は百分率ではありません。%記号を付けず、指定された尺度で入力してください。"
                correct = parsed is not None and not domain_error and _rounded_decimal(parsed, part["decimals"]) == expected
            else:
                # Legacy snapshots keep their stored expected value and the
                # original float tolerance, including the historical epsilon.
                parsed = _parse_numeric_answer(answer)
                expected = _parse_numeric_answer(part.get("expected"))
                tolerance = max(float(part.get("tolerance", 0.0) or 0.0), 0.0)
                correct = parsed is not None and expected is not None and abs(parsed - expected) <= tolerance + 1e-12
            if answer in (None, ""):
                feedback = "未回答です。"
            elif parsed is None:
                feedback = "数値として解釈できません。"
            elif domain_error:
                feedback = domain_error
            elif correct:
                feedback = "正解です。"
            else:
                feedback = f"正解は {part.get('expected_text', part.get('expected'))}{part.get('unit') or ''} です。"
        elif kind == "choice":
            expected = str(part.get("expected") or "")
            options = [str(option) for option in part.get("options", [])]
            normalized_answer = "" if answer is None else str(answer).strip()
            correct = normalized_answer == expected and expected in options
            feedback = "正解です。" if correct else ("未回答です。" if not normalized_answer else f"正解は「{expected}」です。")
        else:
            raise ValueError(f"未対応の解答形式です: {kind}")
        points = weight if correct else 0.0
        earned_points += points
        correct_count += int(correct)
        results.append(
            {
                "key": key,
                "label": label,
                "kind": kind,
                "answer": answer,
                "expected": part.get("expected"),
                "correct": correct,
                "earned_points": points,
                "max_points": weight,
                "feedback": feedback,
            }
        )
    score = round(earned_points / max_points * 100, 1) if max_points else 0.0
    return _ensure_json(
        {
            "score": score,
            "earned_points": earned_points,
            "max_points": max_points,
            "correct_count": correct_count,
            "total_parts": len(results),
            "all_correct": bool(results) and correct_count == len(results),
            "parts": results,
        }
    )


def _attempt_path(target: date, root: Path | None) -> Path:
    return _practice_dir(root) / f"{target:%Y}" / f"{target:%m}" / f"{target:%Y%m%d}.json"


def _validated_attempt_payload(
    problem: dict[str, Any],
    answers: dict[str, Any],
    grade: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    required_problem_keys = {
        "problem_id",
        "template_id",
        "topic",
        "topic_label",
        "title",
        "prompt",
        "period",
        "data_columns",
        "data_rows",
        "answer_parts",
        "snapshot",
    }
    if not isinstance(problem, dict) or not required_problem_keys.issubset(problem):
        raise ValueError("保存する問題データの形式が不正です。")
    if not isinstance(problem.get("period"), dict) or not isinstance(problem.get("snapshot"), dict):
        raise ValueError("問題の期間またはスナップショットが不正です。")
    if not isinstance(problem.get("answer_parts"), list) or not problem["answer_parts"]:
        raise ValueError("問題に有効な解答欄がありません。")
    if not isinstance(answers, dict) or not isinstance(grade, dict):
        raise ValueError("answers と grade は辞書で指定してください。")

    problem_ready = _ensure_json(problem)
    answers_ready = _ensure_json(answers)
    grade_ready = _ensure_json(grade)
    calculated = grade_problem(problem_ready, answers_ready)
    summary_keys = (
        "score",
        "earned_points",
        "max_points",
        "correct_count",
        "total_parts",
        "all_correct",
    )
    if any(grade_ready.get(key) != calculated[key] for key in summary_keys):
        raise ValueError("採点結果が問題と解答に一致しません。再採点してください。")
    if grade_ready.get("parts") != calculated["parts"]:
        raise ValueError("設問別の採点結果が問題と解答に一致しません。再採点してください。")
    return problem_ready, answers_ready, grade_ready


def save_attempt(
    problem: dict[str, Any],
    answers: dict[str, Any],
    grade: dict[str, Any],
    root: Path | None = None,
) -> dict[str, Any]:
    """Append an attempt to a separate history store using lock + atomic save."""
    problem_ready, answers_ready, grade_ready = _validated_attempt_payload(problem, answers, grade)
    now = app_now()
    target = now.date()
    path = _attempt_path(target, root)
    attempt = _ensure_json(
        {
            "attempt_id": f"stat_attempt_{uuid4().hex}",
            "attempted_at": now.isoformat(),
            "date": target.isoformat(),
            "problem_id": problem_ready.get("problem_id"),
            "template_id": problem_ready.get("template_id"),
            "topic": problem_ready.get("topic"),
            "title": problem_ready.get("title"),
            "period": problem_ready.get("period", {}),
            "answers": answers_ready,
            "grade": grade_ready,
            "score": float(grade_ready.get("score", 0.0) or 0.0),
            "max_score": 100.0,
            "correct_count": int(grade_ready.get("correct_count", 0) or 0),
            "total_parts": int(grade_ready.get("total_parts", 0) or 0),
            "problem_snapshot": problem_ready,
        }
    )
    with json_transaction(path):
        store = load_json_safe(path, {"schema_version": SCHEMA_VERSION, "date": target.isoformat(), "attempts": []})
        if not isinstance(store, dict) or not isinstance(store.get("attempts"), list):
            raise ValueError(f"統計演習履歴の形式が不正です: {path}")
        if store.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"統計演習履歴のスキーマが不正です: {path}")
        if str(store.get("date") or "") != target.isoformat():
            raise ValueError(f"統計演習履歴の日付が保存先と一致しません: {path}")
        if any(not isinstance(item, dict) for item in store["attempts"]):
            raise ValueError(f"統計演習履歴に不正な解答行があります: {path}")
        store["attempts"].append(attempt)
        save_json_safe(path, store)
    return attempt


def load_attempts(
    start_date: date | datetime | str | None = None,
    end_date: date | datetime | str | None = None,
    root: Path | None = None,
) -> list[dict[str, Any]]:
    """Load attempt history newest-first; this function never writes or normalizes files."""
    start = _coerce_date(start_date, "start_date") if start_date is not None else None
    end = _coerce_date(end_date, "end_date") if end_date is not None else None
    if start is not None and end is not None and end < start:
        start, end = end, start
    results: list[dict[str, Any]] = []
    for path in record_json_files(_practice_dir(root)):
        store = load_json_safe(path, {})
        if not isinstance(store, dict) or not isinstance(store.get("attempts"), list):
            raise ValueError(f"統計演習履歴の形式が不正です: {path}")
        for attempt in store["attempts"]:
            if not isinstance(attempt, dict):
                raise ValueError(f"統計演習履歴に不正な行があります: {path}")
            try:
                attempt_date = _coerce_date(attempt.get("date") or attempt.get("attempted_at"), "attempt date")
            except ValueError as exc:
                raise ValueError(f"統計演習履歴の日付が不正です: {path}") from exc
            if start is not None and attempt_date < start:
                continue
            if end is not None and attempt_date > end:
                continue
            results.append(_ensure_json(attempt))
    return sorted(
        results,
        key=lambda item: (str(item.get("attempted_at") or ""), str(item.get("attempt_id") or "")),
        reverse=True,
    )
