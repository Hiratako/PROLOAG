"""Frozen hypothesis-testing exercises built only from existing numeric logs.

The service is imported lazily so its template registry can import these builders.
All statistics use exactly the rounded values shown in the exercise.
"""
from __future__ import annotations

from itertools import combinations
import math
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats


_ALPHA = 0.05
_REJECT = "帰無仮説を棄却する"
_RETAIN = "帰無仮説を棄却しない"
_CONTINUOUS = {"study_hours", "sleep_hours", "running_distance_km", "running_duration_min", "session_minutes"}
_NULL_MEANS = {"study_hours": 2.0, "sleep_hours": 7.0, "running_distance_km": 5.0,
               "running_duration_min": 30.0, "session_minutes": 60.0}
_MODEL_NOTE = "個人1名の反復記録ですが、計算練習上は観測（対応ありでは組間）が独立であると仮定します。"
_SAMPLING_NOTE = "計算可能な変動を含む観測を抽出しており、無作為抽出とはみなしません。有意・非有意を抽出条件にはしていません。"


def _service():
    from app_core import statistics_practice_service
    return statistics_practice_service


def _check_result(statistic: float, pvalue: float) -> None:
    if not math.isfinite(statistic) or not math.isfinite(pvalue) or not 0 <= pvalue <= 1:
        raise _service()._TemplateUnavailable("有限の検定統計量とp値を計算できる記録が必要です。")
    if _service().format_decimal_answer(pvalue, 4) == "0.0500":
        raise _service()._TemplateUnavailable("p値を4桁に丸めると5%の判断境界と重なるため、この標本は出題しません。")


def _test_parts(key: str, label: str, statistic: float, pvalue: float, *, decimals: int = 3, p_label: str = "両側p値") -> list[dict]:
    service = _service()
    _check_result(statistic, pvalue)
    return [
        service._numeric_part(key, label, statistic, decimals=decimals,
                              domain={"observed_probability": "probability", "chi_square": "nonnegative", "w_statistic": "nonnegative"}.get(key, "real")),
        service._numeric_part("p_value", p_label, pvalue, domain="probability", decimals=4),
        service._choice_part("decision", "有意水準5%での判断", [_REJECT, _RETAIN],
                             _REJECT if pvalue < _ALPHA else _RETAIN),
    ]


def _parameters(method: str, pvalue: float, **kwargs: Any) -> dict:
    return {"method": method, "alternative": "two-sided", "alpha": _ALPHA,
            "p_value_unrounded": pvalue, **kwargs}


def _decision_text(pvalue: float) -> str:
    sign = "<" if pvalue < _ALPHA else "≥"
    decision = _REJECT if pvalue < _ALPHA else _RETAIN
    return f"丸め前のp値 {pvalue:.8g} {sign} 0.05 より、{decision}。棄却しないことは同等性の証明ではありません。"


def _sources(context, *, minimum: int, paired: bool = False) -> list[dict]:
    sources = [source for source in _service()._numeric_sources(context, minimum=minimum, require_variation=True)
               if source["key"] in _CONTINUOUS and (not paired or source["key"] != "session_minutes")]
    if not sources:
        raise _service()._TemplateUnavailable("ばらつきのある学習時間・睡眠・運動の数値記録が不足しています。")
    return sources


def _sample_for_calculation(frame: pd.DataFrame, rng: np.random.Generator, *, minimum: int,
                            maximum: int, key: str = "value", nonzero_minimum: int = 0) -> pd.DataFrame:
    """Find a computable sample without selecting on any test statistic or p-value."""
    service = _service()

    def usable(values: np.ndarray) -> bool:
        return (np.count_nonzero(values) >= nonzero_minimum if nonzero_minimum
                else len(np.unique(values)) >= 2)

    values = np.round(frame[key].to_numpy(dtype=float), 3)
    if len(frame) < minimum or not usable(values):
        raise service._TemplateUnavailable("提示桁で計算できる変動または0以外の差が不足しています。")
    for _ in range(3):
        sampled = service._sample_frame(frame, rng, minimum=minimum, maximum=maximum)
        if usable(np.round(sampled[key].to_numpy(dtype=float), 3)):
            return sampled

    # Only guarantee variation (or enough nonzero differences), never significance.
    required = (list(np.flatnonzero(values)[:nonzero_minimum]) if nonzero_minimum
                else [int(np.argmin(values)), int(np.argmax(values))])
    required = sorted(set(required))
    upper = min(maximum, len(frame))
    size = int(rng.integers(minimum, upper + 1)) if upper > minimum else minimum
    remaining = np.array([index for index in range(len(frame)) if index not in required], dtype=int)
    extras = rng.choice(remaining, size=size-len(required), replace=False)
    return frame.iloc[sorted(required + list(extras))].copy().reset_index(drop=True)


def one_sample_t_problem(context, rng: np.random.Generator) -> dict:
    service = _service()
    sources = _sources(context, minimum=5)
    for position in rng.permutation(len(sources)):
        source = sources[int(position)]
        try:
            sampled = _sample_for_calculation(source["rows"], rng, minimum=5, maximum=10)
        except service._TemplateUnavailable:
            continue
        rows = service._display_numeric_rows(source, sampled)
        values = np.array([row["value"] for row in rows], dtype=float)
        if len(np.unique(values)) < 2:
            continue
        null_mean = _NULL_MEANS[source["key"]]
        result = stats.ttest_1samp(values, popmean=null_mean, alternative="two-sided")
        statistic, pvalue = float(result.statistic), float(result.pvalue)
        try:
            _check_result(statistic, pvalue)
        except service._TemplateUnavailable:
            continue
        mean, sd = float(values.mean()), float(values.std(ddof=1))
        return {
            "prompt": (f"{_MODEL_NOTE} 各観測は正規母集団から得られたと仮定します。表の{source['label']}について、"
                       f"H₀: μ={null_mean:g}{source['unit']}、H₁: μ≠{null_mean:g}{source['unit']}の1標本t検定を行い、"
                       "t値・両側p値・有意水準5%での判断を答えてください。基準値は演習用で、目標や推奨値ではありません。"),
            "observation_unit": source["observation_unit"],
            "data_columns": [{"key": "no", "label": "番号", "unit": ""},
                             {"key": "date", "label": "日付", "unit": ""},
                             {"key": "value", "label": source["label"], "unit": source["unit"]}],
            "data_rows": rows,
            "answer_parts": _test_parts("t_statistic", "t値", statistic, pvalue),
            "hints": ["t = (標本平均 − μ₀) / (s / √n)、sは不偏標準偏差", "自由度はn−1。両側p = 2P(T ≥ |t|)。分布表や電卓を使って構いません。"],
            "explanation": (f"n={len(values)}、平均={mean:.5g}、s={sd:.5g}、自由度={len(values)-1}。"
                            f"t={statistic:.6g}、p={pvalue:.6g}。{_decision_text(pvalue)}"),
            "assumptions": service._numeric_assumptions(source) + [_SAMPLING_NOTE, "母分布の正規性を仮定し、母分散は未知とします。判断には丸め前のp値を使います。"],
            "source_key": source["key"], "data_origin": "personal_logs",
            "parameters": _parameters("one_sample_t", pvalue, null_mean=null_mean, df=len(values)-1),
        }
    raise service._TemplateUnavailable("提示桁に丸めた後も標本分散が正になる記録が必要です。")


def _group_sources(context) -> list[dict]:
    service = _service()
    candidates: list[dict] = []
    try:
        daily_sources = _sources(context, minimum=8, paired=True)
    except service._TemplateUnavailable:
        daily_sources = []
    for source in daily_sources:
        rows = source["rows"].copy()
        dates = pd.to_datetime(rows["date"], errors="coerce")
        rows["group"] = np.where(dates.dt.dayofweek >= 5, "土日", "平日")
        if all(int((rows["group"] == label).sum()) >= 4 for label in ("平日", "土日")):
            candidates.append({**source, "rows": rows, "groups": ["平日", "土日"], "group_key": "weekday"})
    for key in ("environment", "io_type", "study_category"):
        frame = context.sessions
        if key not in frame or "session_minutes" not in frame:
            continue
        rows = frame.loc[:, ["date", "session_minutes", key]].copy().rename(columns={"session_minutes": "value", key: "group"})
        rows["value"] = pd.to_numeric(rows["value"], errors="coerce")
        rows = rows.loc[rows["group"].notna() & np.isfinite(rows["value"])].copy()
        groups = [str(level) for level, count in rows["group"].value_counts().items() if count >= 4]
        for group_a, group_b in combinations(groups, 2):
            candidates.append({"key": "session_minutes", "label": "学習セッション時間", "unit": "分",
                               "observation_unit": "学習セッション", "rows": rows, "groups": [group_a, group_b], "group_key": key})
    return candidates


def welch_t_problem(context, rng: np.random.Generator) -> dict:
    service = _service()
    try:
        sources = _group_sources(context)
    except service._TemplateUnavailable:
        sources = []
    for position in rng.permutation(len(sources)):
        source = sources[int(position)]
        rows, samples = [], []
        for group in source["groups"]:
            subset = source["rows"].loc[source["rows"]["group"] == group]
            try:
                sampled = _sample_for_calculation(subset, rng, minimum=4, maximum=8)
            except service._TemplateUnavailable:
                break
            shown = service._display_numeric_rows(source, sampled)
            samples.append(np.array([row["value"] for row in shown], dtype=float))
            offset = len(rows)
            rows.extend({**row, "group": group, "no": offset + index + 1} for index, row in enumerate(shown))
        if len(samples) != 2:
            continue
        a, b = samples
        if len(np.unique(a)) < 2 or len(np.unique(b)) < 2:
            continue
        result = stats.ttest_ind(a, b, equal_var=False, alternative="two-sided")
        statistic, pvalue = float(result.statistic), float(result.pvalue)
        try:
            _check_result(statistic, pvalue)
        except service._TemplateUnavailable:
            continue
        va, vb = float(a.var(ddof=1)) / len(a), float(b.var(ddof=1)) / len(b)
        df = (va + vb) ** 2 / (va**2 / (len(a)-1) + vb**2 / (len(b)-1))
        group_a, group_b = source["groups"]
        return {
            "prompt": (f"{_MODEL_NOTE} 表の{source['label']}を A={group_a}、B={group_b} の2群に分け、"
                       "H₀: μA=μB、H₁: μA≠μB のWelchのt検定を行ってください。各群の正規性を仮定しますが、"
                       "等分散は仮定しません。tの符号はA−Bです。t値・両側p値・5%での判断を答えてください。"),
            "observation_unit": source["observation_unit"],
            "data_columns": [{"key": "no", "label": "番号", "unit": ""}, {"key": "date", "label": "日付", "unit": ""},
                             {"key": "group", "label": "群", "unit": ""}, {"key": "value", "label": source["label"], "unit": source["unit"]}],
            "data_rows": rows, "answer_parts": _test_parts("t_statistic", "Welchのt値（A−B）", statistic, pvalue),
            "hints": ["t = (平均A−平均B) / √(sA²/nA+sB²/nB)",
                      "ν = (vA+vB)² / {vA²/(nA−1)+vB²/(nB−1)}、v=s²/n。自由度は整数に丸めません。", "両側p = 2P(Tν ≥ |t|)。分布表や電卓を使って構いません。"],
            "explanation": (f"nA={len(a)}、nB={len(b)}、平均A={a.mean():.5g}、平均B={b.mean():.5g}、"
                            f"自由度={df:.6g}、t={statistic:.6g}、p={pvalue:.6g}。{_decision_text(pvalue)}"),
            "assumptions": service._numeric_assumptions(source) + [_SAMPLING_NOTE, "2群を独立と仮定する練習です。実記録の群間差は因果効果を示しません。"],
            "source_key": f"{source['key']}_by_{source['group_key']}", "data_origin": "personal_logs",
            "parameters": _parameters("welch_t", pvalue, df=df, group_a=group_a, group_b=group_b),
        }
    raise service._TemplateUnavailable("平日/土日または学習条件の2群に、それぞれ4件以上のばらつきのある記録が必要です。")


def _adjacent_pairs(source: dict) -> pd.DataFrame:
    """Non-overlapping consecutive calendar days; never bridge missing days."""
    rows = source["rows"].copy()
    rows["parsed_date"] = pd.to_datetime(rows["date"], errors="coerce")
    rows = rows.dropna(subset=["parsed_date"]).sort_values("parsed_date", kind="stable").drop_duplicates("parsed_date")
    records = list(rows.itertuples(index=False))
    pairs, position = [], 0
    while position + 1 < len(records):
        first, second = records[position], records[position+1]
        if (second.parsed_date - first.parsed_date).days == 1:
            pairs.append({"date_before": first.parsed_date.date().isoformat(), "date_after": second.parsed_date.date().isoformat(),
                          "before": round(float(first.value), 3), "after": round(float(second.value), 3)})
            position += 2
        else:
            position += 1
    return pd.DataFrame(pairs, columns=["date_before", "date_after", "before", "after"])


def _paired_candidates(context, rng: np.random.Generator, *, wilcoxon: bool = False):
    service = _service()
    sources = _sources(context, minimum=10, paired=True)
    for position in rng.permutation(len(sources)):
        source = sources[int(position)]
        pairs = _adjacent_pairs(source)
        if len(pairs) < 5:
            continue
        pairs["_difference"] = np.round(pairs["after"] - pairs["before"], 3)
        try:
            selected = _sample_for_calculation(pairs, rng, minimum=5, maximum=10, key="_difference",
                                               nonzero_minimum=3 if wilcoxon else 0).drop(columns="_difference")
        except service._TemplateUnavailable:
            continue
        rows = [{"no": index+1, **row} for index, row in enumerate(selected.to_dict("records"))]
        # Rounded subtraction makes repeated decimal differences true ties.
        differences = np.array([round(row["after"] - row["before"], 3) for row in rows], dtype=float)
        if wilcoxon:
            if np.count_nonzero(differences) < 3:
                continue
        elif len(np.unique(differences)) < 2:
            continue
        yield source, rows, differences


def _paired_columns(source: dict) -> list[dict]:
    return [{"key": "no", "label": "組", "unit": ""},
            {"key": "date_before", "label": "前日", "unit": ""},
            {"key": "date_after", "label": "翌日", "unit": ""},
            {"key": "before", "label": f"前日の{source['label']}", "unit": source["unit"]},
            {"key": "after", "label": f"翌日の{source['label']}", "unit": source["unit"]}]


def paired_t_problem(context, rng: np.random.Generator) -> dict:
    service = _service()
    for source, rows, differences in _paired_candidates(context, rng):
        result = stats.ttest_1samp(differences, popmean=0, alternative="two-sided")
        statistic, pvalue = float(result.statistic), float(result.pvalue)
        try:
            _check_result(statistic, pvalue)
        except service._TemplateUnavailable:
            continue
        return {
            "prompt": (f"{_MODEL_NOTE} 同じ指標の隣接する暦日を、重複しない組にした計算練習です。介入前後の記録ではありません。"
                       f"{source['label']}の差を『翌日−前日』とし、差が正規分布すると仮定して H₀: μ差=0、H₁: μ差≠0 の"
                       "対応のあるt検定を行ってください。t値・両側p値・5%での判断を答えてください。"),
            "observation_unit": "隣接暦日の組", "data_columns": _paired_columns(source), "data_rows": rows,
            "answer_parts": _test_parts("t_statistic", "対応のあるt値（翌日−前日）", statistic, pvalue),
            "hints": ["各組の差dを計算し、t = 平均d / (sd/√n)。独立2群の式は使いません。", "自由度は組数−1。両側p = 2P(T ≥ |t|)。"],
            "explanation": (f"組数={len(rows)}、差の平均={differences.mean():.5g}、差の標準偏差={differences.std(ddof=1):.5g}、"
                            f"自由度={len(rows)-1}、t={statistic:.6g}、p={pvalue:.6g}。{_decision_text(pvalue)}"),
            "assumptions": service._numeric_assumptions(source) + [_SAMPLING_NOTE, "同じ単位・同じ指標だけを対応づけ、欠測日をまたぐ組や日付の再利用はありません。"],
            "source_key": f"{source['key']}_adjacent_days", "data_origin": "personal_logs",
            "parameters": _parameters("paired_t", pvalue, df=len(rows)-1, difference="after_minus_before", pairing="nonoverlapping_adjacent_calendar_days"),
        }
    raise service._TemplateUnavailable("同じ指標の隣接暦日が5組以上あり、差にばらつきが必要です。")


def exact_signed_rank(differences: np.ndarray) -> tuple[float, float, np.ndarray]:
    """Two-sided conditional sign enumeration with average ties and zero removal."""
    values = np.asarray(differences, dtype=float)
    if values.ndim != 1 or not np.all(np.isfinite(values)):
        raise ValueError("差は有限の一次元配列で指定してください。")
    values = values[values != 0]
    if not 1 <= len(values) <= 12:
        raise ValueError("符号列挙には0以外の差が1～12個必要です。")
    ranks = stats.rankdata(np.abs(values), method="average")
    # Twice-ranks are integers even when tied ranks end in .5.
    ranks2 = np.rint(ranks * 2).astype(int)
    total2 = int(ranks2.sum())
    positive2 = int(ranks2[values > 0].sum())
    statistic2 = min(positive2, total2-positive2)
    sums = np.array([0], dtype=int)
    for rank in ranks2:
        sums = np.concatenate((sums, sums + rank))
    extremes = np.minimum(sums, total2-sums) <= statistic2
    return statistic2 / 2.0, float(np.count_nonzero(extremes) / len(sums)), ranks


def wilcoxon_problem(context, rng: np.random.Generator) -> dict:
    service = _service()
    for source, rows, differences in _paired_candidates(context, rng, wilcoxon=True):
        statistic, pvalue, ranks = exact_signed_rank(differences)
        try:
            _check_result(statistic, pvalue)
        except service._TemplateUnavailable:
            continue
        effective = int(np.count_nonzero(differences))
        return {
            "prompt": (f"{_MODEL_NOTE} 同じ指標の隣接暦日を重複しない組にした演習で、介入前後ではありません。"
                       f"{source['label']}の差を『翌日−前日』として、Wilcoxon符号付順位検定を行います。H₀は差の分布が0を中心に対称、"
                       "H₁は位置が0ではない、です。0差は除外、|差|の同順位は平均順位、W=min(W⁺,W⁻)とします。"
                       "各差の±符号を全列挙し、Wが観測値以下となる割合を両側p値としてください。W・p値・5%での判断を答えてください。"),
            "observation_unit": "隣接暦日の組", "data_columns": _paired_columns(source), "data_rows": rows,
            "answer_parts": _test_parts("w_statistic", "W（正負の順位和の小さい方）", statistic, pvalue, decimals=1),
            "hints": ["0差を除外し、残りの|差|を小さい順に順位づけします。同値には平均順位を割り当てます。",
                      "W⁺=正の差の順位和、W⁻=負の差の順位和。0以外の差がm個なら2^m通りの符号を考えます。", "p値は電卓・表計算で列挙して構いません。連続性補正・正規近似は使いません。"],
            "explanation": (f"0差除外後m={effective}、順位={ranks.tolist()}、全符号数={2**effective}。"
                            f"W={statistic:g}、両側p={pvalue:.6g}。{_decision_text(pvalue)}"),
            "assumptions": service._numeric_assumptions(source) + [_SAMPLING_NOTE, "帰無仮説の下で各差の符号が独立・等確率であると仮定します。差の分布の対称性が必要で、単なる中央値検定ではありません。"],
            "source_key": f"{source['key']}_adjacent_days", "data_origin": "personal_logs",
            "parameters": _parameters("exact_sign_enumeration", pvalue, difference="after_minus_before", pairing="nonoverlapping_adjacent_calendar_days", zero_method="wilcox", ties="average", effective_n=effective),
        }
    raise service._TemplateUnavailable("同じ指標の隣接暦日が5組以上あり、0以外の差が3組以上必要です。")


def _tables(context, rng: np.random.Generator, *, chi_square: bool = False):
    service = _service()
    sources = service._categorical_pairs(context, minimum=4)
    for position in rng.permutation(len(sources)):
        source = sources[int(position)]
        rows = source["rows"]
        x_groups = list(combinations([str(item) for item in rows["x"].value_counts().index], 2))
        y_groups = list(combinations([str(item) for item in rows["y"].value_counts().index], 2))
        options = [(x, y) for x in x_groups for y in y_groups]
        for index in rng.permutation(len(options)):
            xs, ys = options[int(index)]
            subset = rows.loc[rows["x"].isin(xs) & rows["y"].isin(ys)]
            table = pd.crosstab(subset["x"], subset["y"]).reindex(index=list(xs), columns=list(ys), fill_value=0).to_numpy(dtype=int)
            if min(table.sum(axis=0)) <= 0 or min(table.sum(axis=1)) <= 0:
                continue
            expected = np.outer(table.sum(axis=1), table.sum(axis=0)) / table.sum()
            if chi_square and float(expected.min()) < 5:
                continue
            yield source, xs, ys, table, expected


def _table_rows(xs, ys, table: np.ndarray) -> list[dict]:
    return [{"row_category": xs[i], "column_category": ys[j], "count": int(table[i, j])}
            for i in range(2) for j in range(2)]


def _table_columns(source: dict) -> list[dict]:
    return [{"key": "row_category", "label": source["x_label"], "unit": ""},
            {"key": "column_category", "label": source["y_label"], "unit": ""},
            {"key": "count", "label": "度数", "unit": "件"}]


def chi_square_problem(context, rng: np.random.Generator) -> dict:
    service = _service()
    for source, xs, ys, table, expected in _tables(context, rng, chi_square=True):
        result = stats.chi2_contingency(table, correction=False)
        statistic, pvalue = float(result.statistic), float(result.pvalue)
        try:
            _check_result(statistic, pvalue)
        except service._TemplateUnavailable:
            continue
        return {
            "prompt": (f"{_MODEL_NOTE} 表示した2行・2列のカテゴリに限り、H₀: {source['x_label']}と{source['y_label']}は独立、"
                       "H₁: 独立ではない、をPearsonのχ²検定で調べてください。全セルの期待度数は5以上です。"
                       "Yatesの連続性補正は使いません。χ²値・p値・5%での判断を答えてください。"),
            "observation_unit": source["observation_unit"], "data_columns": _table_columns(source), "data_rows": _table_rows(xs, ys, table),
            "answer_parts": _test_parts("chi_square", "Pearsonのχ²値", statistic, pvalue, p_label="p値（χ²分布の上側確率）"),
            "hints": ["Eij=行合計×列合計/総数、χ²=Σ(Oij−Eij)²/Eij", "自由度=(行数−1)(列数−1)=1。p=P(χ²₁≥観測値)で、2倍しません。"],
            "explanation": (f"期待度数（行ごと）={np.round(expected, 4).tolist()}、自由度=1、χ²={statistic:.6g}、"
                            f"p={pvalue:.6g}。{_decision_text(pvalue)}"),
            "assumptions": service._assumptions(source["observation_unit"]) + ["表示した2カテゴリずつに限定した部分集計です。疎な表にはこのテンプレートを出題せず、Fisher検定を利用します。"],
            "source_key": source["key"], "data_origin": "personal_logs",
            "parameters": _parameters("pearson_chi_square", pvalue, df=1, correction=False, expected=expected.tolist()),
        }
    raise service._TemplateUnavailable("2×2表の全セルの期待度数が5以上必要です。少数件の表はFisher検定を利用できます。")


def fisher_exact_problem(context, rng: np.random.Generator) -> dict:
    service = _service()
    for source, xs, ys, table, expected in _tables(context, rng):
        result = stats.fisher_exact(table, alternative="two-sided")
        pvalue = float(result.pvalue)
        total, row_total, column_total = int(table.sum()), int(table[0].sum()), int(table[:, 0].sum())
        observed_probability = float(stats.hypergeom.pmf(int(table[0, 0]), total, row_total, column_total))
        try:
            _check_result(observed_probability, pvalue)
        except service._TemplateUnavailable:
            continue
        return {
            "prompt": (f"{_MODEL_NOTE} 表示した2行・2列について、周辺度数を固定し、H₀: オッズ比=1（独立）、H₁: オッズ比≠1、"
                       "をFisherの正確確率検定で調べます。観測表そのものの確率と両側p値、5%での判断を答えてください。"
                       "両側p値は、観測表以下の確率を持つ全ての可能な表の確率の和です。片側p値の単純な2倍ではありません。"),
            "observation_unit": source["observation_unit"], "data_columns": _table_columns(source), "data_rows": _table_rows(xs, ys, table),
            "answer_parts": _test_parts("observed_probability", "観測表そのものの確率", observed_probability, pvalue, decimals=4),
            "hints": ["aを左上、rを第1行合計、cを第1列合計、Nを総数とするとP(a)=C(c,a)C(N−c,r−a)/C(N,r)。",
                      "aの範囲はmax(0,r+c−N)～min(r,c)。P(a)≤観測表の確率となる表だけを合計します。電卓・表計算を使って構いません。"],
            "explanation": (f"N={total}、第1行合計={row_total}、第1列合計={column_total}、左上={int(table[0,0])}。"
                            f"観測表の確率={observed_probability:.6g}、両側p={pvalue:.6g}。{_decision_text(pvalue)}"),
            "assumptions": service._assumptions(source["observation_unit"]) + ["表示した2カテゴリずつに限定し、周辺度数を固定した条件付き検定です。ゼロセルでも連続性補正は加えません。"],
            "source_key": source["key"], "data_origin": "personal_logs",
            "parameters": _parameters("fisher_exact_probability_ordering", pvalue, observed_probability=observed_probability, expected=expected.tolist()),
        }
    raise service._TemplateUnavailable("各行・各列の合計が正になる2×2のカテゴリ表が必要です。")
