"""Small, fully synthetic exercises for topics unsupported by personal logs.

Builders intentionally never inspect their context.  The displayed rows and
calculation specification contain every input needed to reproduce an answer.
"""

from __future__ import annotations

import math
from fractions import Fraction
from typing import Any

import numpy as np


SYNTHETIC_LABEL = "補助用の架空データ"


def _answer_helpers():
    # The service registers these builders; defer imports to avoid a cycle.
    from app_core.statistics_practice_service import _choice_part, _numeric_part

    return _numeric_part, _choice_part


def _format(value: Any, decimals: int) -> str:
    from app_core.statistics_practice_service import format_decimal_answer

    return format_decimal_answer(value, decimals)


def _synthetic_payload(source_key: str, **payload: Any) -> dict[str, Any]:
    assumptions = payload.pop("assumptions", [])
    return {
        "data_origin": "synthetic",
        "data_origin_label": SYNTHETIC_LABEL,
        "source_key": f"synthetic:{source_key}",
        "assumptions": [
            "補助用の架空データです。PROLOAGの実ログを加工したデータでも、実在する患者の記録でもありません。",
            *assumptions,
        ],
        **payload,
    }


def _logistic_probability_problem(_context: Any, rng: np.random.Generator) -> dict[str, Any]:
    numeric, _ = _answer_helpers()
    row = {
        "intercept": float(rng.choice((-1.5, -1.0, -0.5))),
        "beta_x": float(rng.choice((0.25, 0.5, 0.75))),
        "beta_z": float(rng.choice((-0.5, 0.5))),
        "x": int(rng.integers(1, 4)),
        "z": int(rng.integers(0, 2)),
    }
    logit = row["intercept"] + row["beta_x"] * row["x"] + row["beta_z"] * row["z"]
    odds = math.exp(logit)
    probability = odds / (1.0 + odds)
    return _synthetic_payload(
        "logistic_probability",
        observation_unit="架空モデルの予測対象1件",
        prompt=(
            "補助用の架空データです。表の係数をもつロジスティックモデル "
            "logit(p) = β0 + βx × x + βz × z を考えます。"
            "p は架空のイベントが起きる確率です。表の x, z における logit、オッズ、予測確率 p を求めてください。"
            "logit は小数第2位、オッズと確率は小数第3位まで。確率は百分率ではなく0〜1で答えます。"
        ),
        data_columns=[
            {"key": "intercept", "label": "切片 β0", "unit": ""},
            {"key": "beta_x", "label": "係数 βx", "unit": ""},
            {"key": "beta_z", "label": "係数 βz", "unit": ""},
            {"key": "x", "label": "説明変数 x", "unit": ""},
            {"key": "z", "label": "説明変数 z", "unit": ""},
        ],
        data_rows=[row],
        answer_parts=[
            numeric("logit", "logit（対数オッズ）", logit, domain="real"),
            numeric("odds", "オッズ", odds, domain="nonnegative", decimals=3),
            numeric("probability", "予測確率 p（0〜1）", probability, domain="probability", decimals=3),
        ],
        hints=[
            "最初に表の係数と説明変数を線形予測式へ代入します。logit は確率そのものではありません。",
            "オッズ = exp(logit)、p = オッズ / (1 + オッズ)。途中の値は丸めずに計算します。",
        ],
        explanation=(
            f"logit = {row['intercept']} + {row['beta_x']} × {row['x']} + "
            f"({row['beta_z']}) × {row['z']} = {_format(logit, 2)}。"
            f"オッズ = exp({_format(logit, 2)}) = {_format(odds, 3)}、"
            f"p = exp(logit) / (1 + exp(logit)) = {_format(probability, 3)}です。"
            "線形予測値をそのまま確率として扱わず、逆logit変換します。"
        ),
        assumptions=[
            "係数は演習用に指定した既知の値です。表から係数を推定する問題ではありません。",
            "対数は自然対数、exp は自然対数の底 e の指数関数です。",
        ],
        calculation_spec={"model": "logit(p)=intercept+beta_x*x+beta_z*z", "probability_scale": "0_to_1"},
    )


def _logistic_odds_ratio_problem(_context: Any, rng: np.random.Generator) -> dict[str, Any]:
    numeric, choice = _answer_helpers()
    beta = float(rng.choice((-0.7, -0.4, 0.3, 0.5, 0.7)))
    delta = int(rng.integers(2, 4))
    one_unit_or = math.exp(beta)
    change_or = math.exp(beta * delta)
    expected_interpretation = "他の変数が同じならオッズの倍率を表すが、確率の倍率とは限らない"
    return _synthetic_payload(
        "logistic_odds_ratio",
        observation_unit="架空モデルの係数1件",
        prompt=(
            "補助用の架空データです。ロジスティックモデル logit(p) = β0 + βx × x + その他の項において、"
            f"表の βx が与えられました。他の変数を一定にしたとき、x が1単位増える場合と {delta} 単位増える場合の"
            "オッズ比（増加後 / 増加前）を小数第3位まで求め、正しい解釈を選んでください。"
        ),
        data_columns=[
            {"key": "beta", "label": "係数 βx", "unit": ""},
            {"key": "delta", "label": "指定した x の増加量", "unit": "単位"},
        ],
        data_rows=[{"beta": beta, "delta": delta}],
        answer_parts=[
            numeric("or_one_unit", "1単位増加のオッズ比", one_unit_or, domain="nonnegative", decimals=3),
            numeric("or_change", f"{delta}単位増加のオッズ比", change_or, domain="nonnegative", decimals=3),
            choice("interpretation", "オッズ比の解釈", [
                expected_interpretation,
                "常にイベント確率が同じ倍率で変わることを表す",
                "オッズ比が1未満なら確率が負になることを表す",
            ], expected_interpretation),
        ],
        hints=[
            "x の変化量を Δx とすると、対数オッズの差は βx × Δx です。",
            "OR = exp(βx × Δx)。オッズ p/(1−p) と確率 p は別の量です。",
        ],
        explanation=(
            f"1単位の OR = exp({beta}) = {_format(one_unit_or, 3)}、"
            f"{delta}単位の OR = exp({beta} × {delta}) = {_format(change_or, 3)}です。"
            "ORはオッズの比であり、確率の比であるリスク比（RR）とは一般に一致しません。"
            "ORが1未満ならオッズが減少しますが、確率が負になるわけではありません。"
        ),
        assumptions=[
            "説明変数に交互作用項はなく、他の変数は一定とします。係数の推定や有意差判定は行いません。",
            "係数は自然対数オッズの尺度です。",
        ],
        calculation_spec={"formula": "OR=exp(beta*delta)", "comparison": "increased_over_original"},
    )


def _kaplan_meier_steps(rows: list[dict[str, Any]], *, exact: bool = False) -> list[dict[str, Any]]:
    """Use the risk set just before each time; tied events precede censoring."""
    at_risk = len(rows)
    survival = Fraction(1)
    steps: list[dict[str, Any]] = []
    for time in sorted({row["time"] for row in rows}):
        events = sum(row["time"] == time and row["status"] == "イベント" for row in rows)
        censored = sum(row["time"] == time and row["status"] == "打切り" for row in rows)
        factor = 1 - Fraction(events, at_risk)
        survival *= factor
        steps.append({
            "time": time,
            "at_risk": at_risk,
            "events": events,
            "censored": censored,
            "conditional_survival": factor if exact else float(factor),
            "survival": survival if exact else float(survival),
        })
        at_risk -= events + censored
    return steps


def _kaplan_meier_problem(_context: Any, rng: np.random.Generator) -> dict[str, Any]:
    numeric, choice = _answer_helpers()
    patterns = (
        ((1, 1), (2, 0), (3, 1), (3, 0), (4, 1), (5, 0), (6, 1), (7, 0)),
        ((1, 0), (2, 1), (2, 1), (3, 0), (4, 1), (4, 0), (6, 0), (7, 1)),
        ((1, 1), (2, 1), (2, 0), (3, 0), (4, 1), (6, 0)),
    )
    pattern = patterns[int(rng.integers(0, len(patterns)))]
    scale = int(rng.integers(1, 3))
    rows = [
        {"subject": chr(65 + index), "time": time * scale, "status": "イベント" if event else "打切り"}
        for index, (time, event) in enumerate(pattern)
    ]
    steps = _kaplan_meier_steps(rows, exact=True)
    event_steps = [step for step in steps if step["events"]]
    focus = event_steps[1]
    last = steps[-1]
    expected_censoring = "打切り時刻では生存率は下がらず、その後のリスク集合から除く"
    factors = " × ".join(f"(1 − {step['events']}/{step['at_risk']})" for step in event_steps)
    return _synthetic_payload(
        "kaplan_meier",
        observation_unit="架空の追跡対象者",
        prompt=(
            "補助用の架空データです。全員を時刻0から追跡した表を用い、イベント未発生確率 S(t) を"
            "Kaplan–Meier法で計算します。イベントと打切りが同時刻なら、直前の全リスク集合でイベントを処理した後、"
            f"打切りを除きます。時刻 {focus['time']} 日の直前のリスク集合人数と、その時刻の条件付き生存確率、"
            f"{last['time']} 日時点の S(t) を求めてください。確率は0〜1、小数第3位まで。"
        ),
        data_columns=[
            {"key": "subject", "label": "架空ID", "unit": ""},
            {"key": "time", "label": "イベントまたは打切りまで", "unit": "日"},
            {"key": "status", "label": "状態", "unit": ""},
        ],
        data_rows=rows,
        answer_parts=[
            numeric("at_risk", f"{focus['time']}日直前のリスク集合人数", focus["at_risk"], domain="integer_count", decimals=0, unit="人"),
            numeric("conditional_survival", f"{focus['time']}日の条件付き生存確率", focus["conditional_survival"], domain="probability", decimals=3),
            numeric("survival", f"S({last['time']})", last["survival"], domain="probability", decimals=3),
            choice("censoring", "打切りの扱い", [
                expected_censoring,
                "打切りもイベント1件として生存率を下げる",
                "打切り対象者を時刻0から存在しなかったことにする",
            ], expected_censoring),
        ],
        hints=[
            "その時刻より前にイベントまたは打切りとなった人を除き、直前のリスク集合 n を数えます。",
            "イベント数を d とすると条件付き生存確率は 1−d/n。S(t) は各イベント時刻の値の積です。",
        ],
        explanation=(
            f"{focus['time']}日直前は n={focus['at_risk']}人、イベント d={focus['events']}人なので、"
            f"条件付き生存確率は 1−{focus['events']}/{focus['at_risk']}={_format(focus['conditional_survival'], 3)}です。"
            f"S({last['time']}) = {factors} = {_format(last['survival'], 3)}。"
            "打切りだけの時刻では曲線は下がりません。同時刻の打切り対象者は、その時刻のイベントの分母には含めます。"
        ),
        assumptions=[
            "右打切りのみで、遅れて参加する対象者はいません。追跡対象者は独立とし、打切りはイベント発生時間と独立と仮定します。",
            "ここでの生存は指定イベントがまだ起きていないことを指します。実際の生存予後の推定ではありません。",
        ],
        calculation_spec={"method": "Kaplan-Meier", "ties": "events_before_censoring", "focus_time": focus["time"], "target_time": last["time"]},
        result_chart={
            "kind": "survival",
            "rows": [{"time": 0, "survival": 1.0}, *[{"time": step["time"], "survival": float(step["survival"])} for step in steps]],
            "x_label": "追跡時間（日）",
            "y_label": "イベント未発生確率 S(t)",
        },
    )


def _diagnostic_accuracy_problem(_context: Any, rng: np.random.Generator) -> dict[str, Any]:
    numeric, _ = _answer_helpers()
    tp = int(rng.choice((24, 30, 36, 40)))
    fn = int(rng.choice((4, 6, 10, 12)))
    fp = int(rng.choice((6, 10, 12, 15)))
    tn = int(rng.choice((40, 48, 60, 72)))
    sensitivity = Fraction(tp, tp + fn) * 100
    specificity = Fraction(tn, tn + fp) * 100
    ppv = Fraction(tp, tp + fp) * 100
    npv = Fraction(tn, tn + fn) * 100
    return _synthetic_payload(
        "diagnostic_accuracy",
        observation_unit="架空の検査対象者",
        prompt=(
            "補助用の架空データです。ある架空の集団で、検査結果と基準判定による疾患の有無を全員について確認しました。"
            "表から感度・特異度・陽性的中率（PPV）・陰性的中率（NPV）を求めてください。"
            "いずれも百分率（%）、小数第2位まで答えます。"
        ),
        data_columns=[
            {"key": "test", "label": "検査結果", "unit": ""},
            {"key": "disease_positive", "label": "疾患あり（基準判定）", "unit": "人"},
            {"key": "disease_negative", "label": "疾患なし（基準判定）", "unit": "人"},
        ],
        data_rows=[
            {"test": "陽性", "disease_positive": tp, "disease_negative": fp},
            {"test": "陰性", "disease_positive": fn, "disease_negative": tn},
        ],
        answer_parts=[
            numeric("sensitivity", "感度", sensitivity, domain="percentage", unit="%"),
            numeric("specificity", "特異度", specificity, domain="percentage", unit="%"),
            numeric("ppv", "陽性的中率（PPV）", ppv, domain="percentage", unit="%"),
            numeric("npv", "陰性的中率（NPV）", npv, domain="percentage", unit="%"),
        ],
        hints=[
            "感度・特異度では疾患の有無、的中率では検査結果で分母を分けます。",
            "感度=TP/(TP+FN)、特異度=TN/(TN+FP)、PPV=TP/(TP+FP)、NPV=TN/(TN+FN)。%に直します。",
        ],
        explanation=(
            f"TP={tp}、FN={fn}、FP={fp}、TN={tn}。"
            f"感度={tp}/({tp}+{fn})×100={_format(sensitivity, 2)}%、"
            f"特異度={tn}/({tn}+{fp})×100={_format(specificity, 2)}%、"
            f"PPV={tp}/({tp}+{fp})×100={_format(ppv, 2)}%、"
            f"NPV={tn}/({tn}+{fn})×100={_format(npv, 2)}%です。"
            "的中率は集団の有病割合にも依存するので、この集団での値を別の集団へそのまま移せるとは限りません。"
        ),
        assumptions=[
            "基準判定は誤りがなく、全員に同じ基準判定を実施し、判定不能や欠測はないとします。",
            "症例数と非症例数を別々に固定して抽出した症例対照標本ではなく、一つの集団の集計です。",
        ],
        calculation_spec={"design": "complete_population_cross_tabulation", "output_scale": "percent"},
    )


def _risk_ratio_odds_ratio_problem(_context: Any, rng: np.random.Generator) -> dict[str, Any]:
    numeric, choice = _answer_helpers()
    exposed_total = int(rng.choice((40, 60, 80)))
    control_total = int(rng.choice((40, 60, 80)))
    exposed_events = int(exposed_total * float(rng.choice((0.25, 0.5))))
    control_events = int(control_total * float(rng.choice((0.1, 0.2))))
    a, b = exposed_events, exposed_total - exposed_events
    c, d = control_events, control_total - control_events
    exposed_risk, control_risk = Fraction(a, a + b), Fraction(c, c + d)
    rr = exposed_risk / control_risk
    odds_ratio = Fraction(a * d, b * c)
    risk_difference = (exposed_risk - control_risk) * 100
    interpretation = "この表では曝露群と非曝露群の関連を計算できるが、因果関係は断定できない"
    return _synthetic_payload(
        "risk_ratio_odds_ratio",
        observation_unit="架空の追跡対象者",
        prompt=(
            "補助用の架空データです。開始時に全員が疾患未発生だった2群を、同じ期間だけ追跡した架空の観察研究です。"
            "追跡脱落はありません。曝露群 / 非曝露群のリスク比（RR）とオッズ比（OR）を小数第3位まで、"
            "リスク差（曝露群 − 非曝露群）をパーセントポイント・小数第2位まで求め、解釈を選んでください。"
        ),
        data_columns=[
            {"key": "group", "label": "群", "unit": ""},
            {"key": "events", "label": "期間内に発生", "unit": "人"},
            {"key": "no_events", "label": "期間内に発生せず", "unit": "人"},
        ],
        data_rows=[
            {"group": "曝露群", "events": a, "no_events": b},
            {"group": "非曝露群", "events": c, "no_events": d},
        ],
        answer_parts=[
            numeric("risk_ratio", "リスク比（RR）", rr, domain="nonnegative", decimals=3),
            numeric("odds_ratio", "オッズ比（OR）", odds_ratio, domain="nonnegative", decimals=3),
            numeric("risk_difference", "リスク差（曝露群 − 非曝露群）", risk_difference, domain="percentage_difference", unit="パーセントポイント"),
            choice("interpretation", "研究結果の解釈", [
                interpretation,
                "RRが1を超えていれば、それだけで曝露が原因だと証明できる",
                "RRとORは定義が同じなので必ず一致する",
            ], interpretation),
        ],
        hints=[
            "各群のリスクはイベント数 / 群の総人数、オッズはイベント数 / 非イベント数です。",
            "RR=リスク同士の比、OR=オッズ同士の比。リスク差は割合の差×100でパーセントポイントになります。",
        ],
        explanation=(
            f"曝露群のリスク={a}/{a+b}={_format(exposed_risk, 3)}、非曝露群={c}/{c+d}={_format(control_risk, 3)}。"
            f"RR={_format(rr, 3)}、OR=({a}/{b})/({c}/{d})={_format(odds_ratio, 3)}、"
            f"リスク差=({_format(exposed_risk, 3)}−{_format(control_risk, 3)})×100={_format(risk_difference, 2)}パーセントポイントです。"
            "RRとORは分母が異なります。観察研究の単純集計には交絡などがあり得るため、因果関係は断定できません。"
        ),
        assumptions=[
            "架空のコホート研究で、追跡期間とイベント判定は両群で同じとします。",
            "罹患率の比（人時間を分母とする指標）ではなく、同じ期間の累積リスクの比を求めます。",
        ],
        calculation_spec={"design": "cohort", "comparison": "exposed_over_unexposed", "risk_difference_scale": "percentage_points"},
    )


def _trial_design_problem(_context: Any, rng: np.random.Generator) -> dict[str, Any]:
    _, choice = _answer_helpers()
    total = int(rng.choice((80, 100, 120)))
    crossovers = int(rng.integers(3, 8))
    design = "無作為化比較試験（RCT）"
    allocation = "登録前に次の割付を予測できないようにして、選択バイアスを抑える"
    blinding = "参加者・担当者・評価者が割付を知ることによる行動や評価の偏りを抑える"
    analysis = "治療変更があっても、全員を最初に無作為割付された群として解析する"
    return _synthetic_payload(
        "trial_design",
        observation_unit="架空の試験計画1件",
        prompt=(
            f"補助用の架空データです。架空の試験で {total} 人を介入Aと対照Bに1:1でコンピュータ無作為割付しました。"
            "割付は登録後に中央システムで知らせ、登録前に次の群は分かりません。"
            "参加者・治療担当者・結果評価者には割付を伏せています。"
            f"途中でA群の {crossovers} 人がBへ治療変更しましたが、全員の転帰を把握できました。"
            "研究デザイン、割付の隠蔽、盲検化の目的、ITT原則に沿った解析方法をそれぞれ選んでください。"
        ),
        data_columns=[
            {"key": "group", "label": "最初の割付群", "unit": ""},
            {"key": "randomized", "label": "無作為割付された人数", "unit": "人"},
            {"key": "changed", "label": "相手群の治療へ変更", "unit": "人"},
            {"key": "observed", "label": "転帰を把握", "unit": "人"},
        ],
        data_rows=[
            {"group": "介入A", "randomized": total // 2, "changed": crossovers, "observed": total // 2},
            {"group": "対照B", "randomized": total // 2, "changed": 0, "observed": total // 2},
        ],
        answer_parts=[
            choice("design", "研究デザイン", [design, "症例対照研究", "横断研究"], design),
            choice("allocation", "割付の隠蔽の主な目的", [allocation, "転帰を必ず完全に把握する", "試験後のすべての解析者から結果を隠す"], allocation),
            choice("blinding", "盲検化の主な目的", [blinding, "無作為割付を不要にする", "交絡因子を各群で必ず完全に一致させる"], blinding),
            choice("itt", "ITT原則に沿った解析", [analysis, "治療を変更した人を解析から除く", "実際に受けた治療群へ移して解析する"], analysis),
        ],
        hints=[
            "介入を研究者が割り当て、その割付が無作為である点に注目します。隠蔽は主に割付前、盲検化は主に割付後の偏りに関係します。",
            "ITT（intention-to-treat）は当初の無作為割付を解析でも尊重する原則です。",
        ],
        explanation=(
            "介入を無作為に割り付けて対照群と比較するためRCTです。"
            "割付の隠蔽は登録前の割付予測を防ぎ、参加者選択の偏りを抑えます。"
            "盲検化は割付を知ることによる対応や評価の偏りを抑えます。"
            f"ITT原則では治療変更した {crossovers} 人も最初のA群に含め、A・B各 {total//2} 人として解析します。"
            "無作為化は個々の試験ですべての背景因子を完全に均等化する保証ではありません。"
        ),
        assumptions=[
            "転帰は全員分そろっているとします。欠測値の扱いや中間事象に対する推定対象の詳細は、この基本問題の範囲外です。",
            "『二重盲検』という人数が曖昧な表現を使わず、割付を知らない立場を問題文で具体的に示しています。",
        ],
        calculation_spec={"design": "randomized_controlled_trial", "allocation_ratio": "1:1", "analysis_principle": "intention_to_treat", "outcomes_complete": True},
    )


SUPPLEMENT_SPECS = (
    ("logistic_probability", "logistic", "ロジスティックモデルの予測確率", _logistic_probability_problem),
    ("logistic_odds_ratio", "logistic", "ロジスティック回帰係数とオッズ比", _logistic_odds_ratio_problem),
    ("kaplan_meier", "survival", "Kaplan–Meier法と打切り", _kaplan_meier_problem),
    ("diagnostic_accuracy", "clinical", "感度・特異度・的中率", _diagnostic_accuracy_problem),
    ("risk_ratio_odds_ratio", "clinical", "リスク比・オッズ比・リスク差", _risk_ratio_odds_ratio_problem),
    ("trial_design", "clinical", "無作為割付・盲検・ITT", _trial_design_problem),
)
