from __future__ import annotations

from typing import Any, Iterable

import numpy as np
import pandas as pd


DEFAULT_BOOTSTRAP_ITERATIONS = 2000
DEFAULT_RANDOM_SEED = 20260711


def numeric_array(values: Iterable[Any]) -> np.ndarray:
    series = pd.to_numeric(pd.Series(list(values), dtype="object"), errors="coerce")
    return series.dropna().to_numpy(dtype=float)


def _comparison_arrays(
    group_a: Iterable[Any],
    group_b: Iterable[Any],
    *,
    paired: bool,
) -> tuple[np.ndarray, np.ndarray]:
    if not paired:
        return numeric_array(group_a), numeric_array(group_b)
    left = list(group_a)
    right = list(group_b)
    if len(left) != len(right):
        return np.array([], dtype=float), np.array([], dtype=float)
    # Missing values must remove the entire pair, not shift either side independently.
    pairs = pd.DataFrame(
        {
            "left": pd.to_numeric(pd.Series(left, dtype="object"), errors="coerce"),
            "right": pd.to_numeric(pd.Series(right, dtype="object"), errors="coerce"),
        }
    ).dropna()
    return pairs["left"].to_numpy(dtype=float), pairs["right"].to_numpy(dtype=float)


def safe_median_difference(group_a: Iterable[Any], group_b: Iterable[Any]) -> float | None:
    a = numeric_array(group_a)
    b = numeric_array(group_b)
    if not len(a) or not len(b):
        return None
    return float(np.median(a) - np.median(b))


def bootstrap_interval(
    group_a: Iterable[Any],
    group_b: Iterable[Any],
    *,
    paired: bool = False,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    confidence: float = 0.95,
    seed: int = DEFAULT_RANDOM_SEED,
    statistic: str = "median",
) -> tuple[float | None, float | None]:
    if statistic not in {"median", "mean"}:
        raise ValueError("statistic must be 'median' or 'mean'")
    a, b = _comparison_arrays(group_a, group_b, paired=paired)
    if not len(a) or not len(b):
        return None, None

    rng = np.random.default_rng(seed)
    estimator = np.median if statistic == "median" else np.mean
    samples = np.empty(max(int(iterations), 1), dtype=float)
    for index in range(len(samples)):
        if paired:
            positions = rng.integers(0, len(a), len(a))
            samples[index] = estimator(a[positions] - b[positions])
        else:
            sample_a = a[rng.integers(0, len(a), len(a))]
            sample_b = b[rng.integers(0, len(b), len(b))]
            samples[index] = estimator(sample_a) - estimator(sample_b)

    alpha = max(0.0, min(1.0, 1.0 - confidence))
    low, high = np.quantile(samples, [alpha / 2, 1 - alpha / 2])
    return float(low), float(high)


def spearman_correlation(left: Iterable[Any], right: Iterable[Any]) -> tuple[float | None, int]:
    frame = pd.DataFrame(
        {
            "left": pd.to_numeric(pd.Series(list(left), dtype="object"), errors="coerce"),
            "right": pd.to_numeric(pd.Series(list(right), dtype="object"), errors="coerce"),
        }
    ).dropna()
    if len(frame) < 3 or frame["left"].nunique() < 2 or frame["right"].nunique() < 2:
        return None, len(frame)
    value = frame["left"].corr(frame["right"], method="spearman")
    return (None if pd.isna(value) else float(value)), len(frame)


def classify_evidence_level(group_a_size: int, group_b_size: int | None = None) -> str:
    effective_size = int(group_a_size)
    if group_b_size is not None:
        effective_size = min(effective_size, int(group_b_size))
    if effective_size < 8:
        return "結論表示なし"
    if effective_size < 15:
        return "探索的"
    if effective_size < 30:
        return "暫定的"
    return "比較可能"


def robust_scale(values: Iterable[Any]) -> float | None:
    data = numeric_array(values)
    if not len(data):
        return None
    q1, q3 = np.quantile(data, [0.25, 0.75])
    scale = float(q3 - q1)
    if scale > 0:
        return scale
    standard_deviation = float(np.std(data))
    return standard_deviation if standard_deviation > 0 else None


def summarize_difference(
    group_a: Iterable[Any],
    group_b: Iterable[Any],
    *,
    paired: bool = False,
    seed: int = DEFAULT_RANDOM_SEED,
) -> dict[str, Any]:
    a, b = _comparison_arrays(group_a, group_b, paired=paired)
    difference = (
        float(np.median(a - b))
        if paired and len(a)
        else safe_median_difference(a, b)
    )
    low, high = bootstrap_interval(a, b, paired=paired, seed=seed)
    return {
        "group_a_n": len(a),
        "group_b_n": len(b),
        "group_a_median": float(np.median(a)) if len(a) else None,
        "group_b_median": float(np.median(b)) if len(b) else None,
        "median_difference": difference,
        "interval_low": low,
        "interval_high": high,
        "evidence_level": classify_evidence_level(len(a), len(b)),
    }


def summarize_rate_difference(
    group_a: Iterable[Any],
    group_b: Iterable[Any],
    *,
    seed: int = DEFAULT_RANDOM_SEED,
) -> dict[str, Any]:
    """Compare binary recording rates as proportions, before any percentage scaling."""
    a = numeric_array(group_a)
    b = numeric_array(group_b)
    if not np.isin(a, [0.0, 1.0]).all() or not np.isin(b, [0.0, 1.0]).all():
        raise ValueError("recording rates require binary values")
    rate_a = float(np.mean(a)) if len(a) else None
    rate_b = float(np.mean(b)) if len(b) else None
    low, high = bootstrap_interval(a, b, seed=seed, statistic="mean")
    return {
        "group_a_n": len(a),
        "group_b_n": len(b),
        "group_a_rate": rate_a,
        "group_b_rate": rate_b,
        "rate_difference": rate_a - rate_b if rate_a is not None and rate_b is not None else None,
        "interval_low": low,
        "interval_high": high,
        "evidence_level": classify_evidence_level(len(a), len(b)),
    }
