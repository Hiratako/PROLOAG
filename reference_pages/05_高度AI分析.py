from __future__ import annotations

from web_demo.runtime import fragment as demo_fragment

from datetime import date, timedelta
from pathlib import Path
from secrets import randbelow
from typing import Any

import pandas as pd
import matplotlib.pyplot as plt
import streamlit as st

import prolog_ui.components as ab
from app_core.app_clock import app_today
from app_core.statistics_practice_service import (
    TOPIC_LABELS,
    GRADING_CONTRACT_VERSION,
    available_problem_summary,
    generate_problem,
    format_decimal_answer,
    grade_problem,
    load_attempts,
    numeric_answer_rule,
    practice_progress,
    save_attempt,
    template_catalog,
)
from app_core.plot_style import configure_japanese_font, style_chart_axis


from app_core.settings_service import load_user_settings as _load_page_settings
PAGE_SETTINGS = _load_page_settings()

from config import get_data_root

ROOT = get_data_root()

CURRENT_PROBLEM_KEY = "statistics_practice_current_problem"
CURRENT_GRADE_KEY = "statistics_practice_current_grade"
CURRENT_ANSWERS_KEY = "statistics_practice_current_answers"
CURRENT_ERROR_KEY = "statistics_practice_current_error"
SUMMARY_KEY = "statistics_practice_available_summary"
SUMMARY_ERROR_KEY = "statistics_practice_summary_error"
START_DATE_KEY = "statistics_practice_start_date"
END_DATE_KEY = "statistics_practice_end_date"
TOPIC_KEY = "statistics_practice_topic"
DATA_MODE_KEY = "statistics_practice_data_mode"
TEMPLATE_KEY = "statistics_practice_template"
SELECTION_KEY = "statistics_practice_selection"
VIEW_KEY = "statistics_practice_view"
HISTORY_DETAIL_KEY = "statistics_history_detail"
HISTORY_PROGRESS_VISIBLE_KEY = "statistics_history_progress_visible"
HISTORY_DETAIL_VISIBLE_KEY = "statistics_history_detail_visible"
DATA_LABELS = {"personal": "PROLOAGの実ログ", "synthetic": "補助用の架空データ", "all": "両方から出題"}


st.set_page_config(page_title="マイデータ統計演習", layout="wide")
ab.inject_css(settings=PAGE_SETTINGS)
ab.sidemenu("自分の記録を固定した問題データに変換し、計算と結果の読み方を練習します。", settings=PAGE_SETTINGS)
ab.topbar(
    "マイデータ統計演習",
    "PROLOAGの学習・生活ログで、統計学の計算と解釈を一問ずつ練習します。",
)


def topic_choices() -> list[tuple[str, str]]:
    choices: list[tuple[str, str]] = [("mixed", "おまかせ")]
    if isinstance(TOPIC_LABELS, dict):
        for key, label in TOPIC_LABELS.items():
            normalized_key = str(key)
            if normalized_key == "mixed":
                choices[0] = ("mixed", str(label))
            else:
                choices.append((normalized_key, str(label)))
    return choices


def retain_practice_inputs() -> None:
    """Detach this page's input state from hidden-widget cleanup on every run.

    Answers and dates use ordinary widgets (not a form), so a view switch can
    submit the draft without grading, generating a problem, or writing history.
    """
    keys = (
        START_DATE_KEY, END_DATE_KEY, TOPIC_KEY, DATA_MODE_KEY, TEMPLATE_KEY,
        SELECTION_KEY, HISTORY_DETAIL_KEY, HISTORY_PROGRESS_VISIBLE_KEY,
        HISTORY_DETAIL_VISIBLE_KEY,
    )
    for key in keys:
        if key in st.session_state:
            st.session_state[key] = st.session_state[key]
    for key in list(st.session_state):
        if key.startswith("statistics_answer_"):
            st.session_state[key] = st.session_state[key]


def clear_problem_state() -> None:
    st.session_state.pop(CURRENT_PROBLEM_KEY, None)
    st.session_state.pop(CURRENT_GRADE_KEY, None)
    st.session_state.pop(CURRENT_ANSWERS_KEY, None)
    st.session_state.pop(CURRENT_ERROR_KEY, None)
    # Drafts belong to the fixed current problem, not the next generated one.
    for key in list(st.session_state):
        if key.startswith("statistics_answer_"):
            st.session_state.pop(key, None)


def request_problem(start_date: date, end_date: date, topic_key: str) -> None:
    clear_problem_state()
    st.session_state.pop(SUMMARY_KEY, None)
    st.session_state.pop(SUMMARY_ERROR_KEY, None)
    if start_date > end_date:
        st.session_state[CURRENT_ERROR_KEY] = "開始日は終了日以前の日付を選んでください。"
        ab.show_action_toast(st.session_state[CURRENT_ERROR_KEY], kind="warning")
        return
    try:
        problem = generate_problem(
            start_date=start_date,
            end_date=end_date,
            topic_key=topic_key,
            seed=randbelow(2_147_483_647),
            root=ROOT,
            data_mode=st.session_state.get(DATA_MODE_KEY, "personal"),
            template_id=st.session_state.get(TEMPLATE_KEY) or None,
            selection_mode=st.session_state.get(SELECTION_KEY, "random"),
        )
    except (ValueError, LookupError) as exc:
        message = str(exc).strip()
        st.session_state[CURRENT_ERROR_KEY] = message or "この条件で作れる問題がありませんでした。"
        ab.show_action_toast("問題を作成できませんでした。条件と詳細表示を確認してください。", kind="warning")
        return
    except Exception as exc:
        st.session_state[CURRENT_ERROR_KEY] = (
            "問題を作成できませんでした。期間を広げるか、別の単元を選んでください。"
            f"（{type(exc).__name__}: {exc}）"
        )
        ab.show_action_toast("問題を作成できませんでした。条件と詳細表示を確認してください。", kind="warning")
        return

    if not isinstance(problem, dict) or not problem.get("answer_parts"):
        st.session_state[CURRENT_ERROR_KEY] = (
            "この条件で利用できる問題がありませんでした。期間を広げるか、別の単元を選んでください。"
        )
        ab.show_action_toast("この条件で利用できる問題がありませんでした。", kind="warning")
        return
    st.session_state[CURRENT_PROBLEM_KEY] = problem


def request_available_summary(start_date: date, end_date: date) -> None:
    st.session_state.pop(SUMMARY_KEY, None)
    st.session_state.pop(SUMMARY_ERROR_KEY, None)
    if start_date > end_date:
        st.session_state[SUMMARY_ERROR_KEY] = "開始日は終了日以前の日付を選んでください。"
        ab.show_action_toast(st.session_state[SUMMARY_ERROR_KEY], kind="warning")
        return
    try:
        summary = available_problem_summary(
            start_date, end_date, root=ROOT,
            data_mode=st.session_state.get(DATA_MODE_KEY, "personal"),
        )
    except Exception as exc:
        st.session_state[SUMMARY_ERROR_KEY] = (
            "出題候補を確認できませんでした。"
            f"（{type(exc).__name__}: {exc}）"
        )
        ab.show_action_toast("出題候補を確認できませんでした。詳細表示を確認してください。", kind="warning")
        return
    if isinstance(summary, dict):
        st.session_state[SUMMARY_KEY] = summary


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if pd.isna(number):
        return default
    return number


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def format_period(period: Any) -> str:
    if not isinstance(period, dict):
        return "期間未設定"
    start = str(period.get("start_date") or "-")
    end = str(period.get("end_date") or "-")
    return f"{start} ～ {end}"


def format_answer(value: Any, part: dict[str, Any] | None = None, *, submitted: bool = False) -> str:
    if value is None or value == "":
        return "未回答"
    part = part or {}
    if str(part.get("kind")) == "numeric":
        decimals = max(0, min(safe_int(part.get("decimals"), 2), 8))
        try:
            if submitted:
                text = str(value)  # Never hide rejected extra digits such as 5.49 people.
            elif part.get("contract_version") == GRADING_CONTRACT_VERSION:
                text = format_decimal_answer(value, decimals)
            else:
                text = f"{float(value):,.{decimals}f}"
        except (TypeError, ValueError):
            text = str(value)
    else:
        text = str(value)
    unit = str(part.get("unit") or "").strip()
    return text if unit and text.endswith(unit) else f"{text} {unit}".strip()


def problem_dataframe(problem: dict[str, Any]) -> pd.DataFrame:
    rows = problem.get("data_rows")
    if not isinstance(rows, list) or not rows:
        return pd.DataFrame()

    columns = problem.get("data_columns")
    if not isinstance(columns, list) or not columns:
        return pd.DataFrame([row for row in rows if isinstance(row, dict)])

    normalized_columns = [column for column in columns if isinstance(column, dict)]
    if not normalized_columns:
        return pd.DataFrame([row for row in rows if isinstance(row, dict)])

    display_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        display_row: dict[str, Any] = {}
        for column in normalized_columns:
            key = str(column.get("key") or "")
            if not key:
                continue
            label = str(column.get("label") or key)
            unit = str(column.get("unit") or "").strip()
            display_label = f"{label}（{unit}）" if unit else label
            display_row[display_label] = row.get(key)
        display_rows.append(display_row)
    return pd.DataFrame(display_rows)


def render_problem_data(problem: dict[str, Any]) -> None:
    st.markdown("#### 問題で使うデータ")
    frame = problem_dataframe(problem)
    if frame.empty:
        st.info("この問題は表データを表示せずに解く形式です。")
        return
    st.dataframe(frame, width="stretch", hide_index=True)
    observation_unit = str(problem.get("observation_unit") or "").strip()
    if observation_unit:
        st.caption(f"観測単位: {observation_unit}　／　出題後はこのデータを固定して採点します。")
    else:
        st.caption("出題後はこのデータを固定して採点します。")


def render_hints(problem: dict[str, Any]) -> None:
    hints = problem.get("hints")
    if not isinstance(hints, list) or not hints:
        return
    with st.expander("ヒントを見る"):
        for index, hint in enumerate(hints, start=1):
            st.markdown(f"{index}. {hint}")


def render_answer_form(problem: dict[str, Any]) -> None:
    parts = [part for part in problem.get("answer_parts", []) if isinstance(part, dict)]
    if not parts:
        st.warning("この問題には解答欄が設定されていません。別の問題を生成してください。")
        return

    problem_id = str(problem.get("problem_id") or problem.get("template_id") or "problem")
    answers: dict[str, Any] = {}
    with st.container():
        st.markdown("#### 解答")
        st.caption("解答の下書きは表示を切り替えても保持されます。採点・履歴保存は「採点する」を押したときだけ行います。")
        st.caption("途中の計算は丸めず、各数値欄に保存された規約に従って答えてください。p値は分布表や統計電卓を利用できます。")
        for index, part in enumerate(parts):
            part_key = str(part.get("key") or f"part_{index + 1}")
            label = str(part.get("label") or f"設問 {index + 1}")
            unit = str(part.get("unit") or "").strip()
            shown_label = f"{label}（{unit}）" if unit else label
            widget_key = f"statistics_answer_{problem_id}_{part_key}"
            kind = str(part.get("kind") or "numeric")
            if kind == "choice":
                options = [str(option) for option in part.get("options", [])]
                placeholder = "— 選択してください —"
                selected = st.selectbox(shown_label, [placeholder, *options], key=widget_key)
                answers[part_key] = None if selected == placeholder else selected
            elif kind == "numeric":
                rule = numeric_answer_rule(part)
                st.caption(f"{label}: {rule}")
                if part.get("contract_version") == GRADING_CONTRACT_VERSION:
                    answers[part_key] = st.text_input(shown_label, placeholder="数値を入力（入力した桁をそのまま保持）",
                                                     help=rule, key=widget_key)
                    continue
                decimals = max(0, min(safe_int(part.get("decimals"), 2), 8))
                answers[part_key] = st.number_input(
                    shown_label,
                    value=None,
                    step=float(10 ** (-decimals)),
                    format=f"%.{decimals}f",
                    placeholder="数値を入力",
                    help=rule,
                    key=widget_key,
                )
            else:
                answers[part_key] = st.text_input(shown_label, key=widget_key)
        submitted = st.button("採点する", key=f"statistics_grade_{problem_id}", type="primary", width="stretch")

    if not submitted:
        return

    try:
        grade = grade_problem(problem, answers)
    except Exception as exc:
        ab.show_action_error(f"採点できませんでした。入力値を確認してください。（{type(exc).__name__}: {exc}）", toast_message="採点できませんでした。入力値と詳細表示を確認してください。")
        return
    if not isinstance(grade, dict):
        ab.show_action_error("採点結果を読み取れませんでした。別の問題で再度お試しください。")
        return

    st.session_state[CURRENT_GRADE_KEY] = grade
    st.session_state[CURRENT_ANSWERS_KEY] = answers

    try:
        save_attempt(problem, answers, grade, root=ROOT)
    except Exception as exc:
        ab.show_action_message(
            f"採点は完了しましたが、学習履歴を保存できませんでした。（{type(exc).__name__}: {exc}）",
            kind="warning", toast_message="採点は完了しましたが、学習履歴を保存できませんでした。",
        )
    else:
        ab.show_action_message("採点結果を学習履歴に保存しました。")


def render_grade(problem: dict[str, Any], grade: dict[str, Any]) -> None:
    score = max(0.0, min(safe_float(grade.get("score")), 100.0))
    correct_count = max(safe_int(grade.get("correct_count")), 0)
    total_parts = max(safe_int(grade.get("total_parts")), 0)
    earned_points = safe_float(grade.get("earned_points"))
    max_points = safe_float(grade.get("max_points"), 100.0)

    if bool(grade.get("all_correct")):
        st.success("全問正解です。")
    elif score > 0:
        st.info("採点しました。設問ごとの結果と解説を確認しましょう。")
    else:
        st.warning("採点しました。正解と計算手順を確認して、次の問題で試してみましょう。")

    left, center, right = st.columns(3)
    with left:
        ab.metric_block("正答率", f"{score:.1f}%", "部分点を含む得点率")
    with center:
        ab.metric_block("正解した設問", f"{correct_count} / {total_parts}", "設問単位の正誤")
    with right:
        ab.metric_block("得点", f"{earned_points:g} / {max_points:g}", "重み付き得点")
    st.progress(score / 100.0)

    original_parts = {
        str(part.get("key")): part
        for part in problem.get("answer_parts", [])
        if isinstance(part, dict)
    }
    result_rows: list[dict[str, Any]] = []
    for index, result in enumerate(grade.get("parts", []), start=1):
        if not isinstance(result, dict):
            continue
        key = str(result.get("key") or "")
        source_part = original_parts.get(key, {})
        result_rows.append(
            {
                "設問": str(result.get("label") or source_part.get("label") or f"設問 {index}"),
                "あなたの解答": format_answer(result.get("answer"), source_part, submitted=True),
                "正解": format_answer(result.get("expected"), source_part),
                "判定": "正解" if bool(result.get("correct")) else "要確認",
                "得点": f"{safe_float(result.get('earned_points')):g} / {safe_float(result.get('max_points')):g}",
                "フィードバック": str(result.get("feedback") or ""),
            }
        )
    if result_rows:
        st.dataframe(pd.DataFrame(result_rows), width="stretch", hide_index=True)

    explanation = str(problem.get("explanation") or "").strip()
    if explanation:
        st.markdown("#### 解説")
        st.markdown(explanation)

    chart = problem.get("result_chart")
    if isinstance(chart, dict) and chart.get("kind") == "survival" and chart.get("rows"):
        values = pd.DataFrame(chart["rows"])
        configure_japanese_font()
        fig, ax = plt.subplots(figsize=(8, 3.6))
        ax.step(values["time"], values["survival"], where="post", linewidth=2,
                color="#39D5FF" if ab.is_dark_theme() else "#005A9C")
        ax.set(xlabel=chart.get("x_label", "追跡時間（日）"), ylabel=chart.get("y_label", "生存率 S(t)"), ylim=(-0.02, 1.05),
               title="補助用の架空データ：Kaplan–Meier曲線")
        style_chart_axis(ax, ab.theme_palette(settings=PAGE_SETTINGS))
        st.pyplot(fig, width="stretch")
        plt.close(fig)

    assumptions = problem.get("assumptions")
    if isinstance(assumptions, list) and assumptions:
        st.markdown("#### この問題で置いている仮定")
        for assumption in assumptions:
            st.markdown(f"- {assumption}")


def render_available_summary() -> None:
    error = st.session_state.get(SUMMARY_ERROR_KEY)
    if error:
        st.warning(str(error))
        return
    summary = st.session_state.get(SUMMARY_KEY)
    if not isinstance(summary, dict):
        return
    if summary.get("data_mode", "personal") != st.session_state.get(DATA_MODE_KEY, "personal"):
        return
    count = max(safe_int(summary.get("available_count")), 0)
    templates = summary.get("templates")
    template_count = len(templates) if isinstance(templates, list) else 0
    st.caption(f"選択したデータで、全{template_count}種類のうち {count} 種類を出題できます。")
    if summary.get("data_mode") != "synthetic":
        st.caption(f"確認した期間: {format_period(summary.get('period'))}")
    if isinstance(templates, list):
        st.dataframe(pd.DataFrame([
            {"単元": row["topic_label"], "問題タイプ": row["title"],
             "出題": "可能" if row["available"] else "条件不足", "条件": row.get("reason", "")}
            for row in templates
        ]), width="stretch", hide_index=True)
    issues = summary.get("issues")
    if isinstance(issues, list) and issues:
        with st.expander("出題できなかった項目"):
            for issue in issues:
                if isinstance(issue, dict):
                    message = issue.get("message") or issue.get("reason") or issue
                else:
                    message = issue
                st.markdown(f"- {message}")


@demo_fragment
def render_exercise_answers(problem: dict[str, Any]) -> None:
    """Commit drafts promptly without reloading the surrounding page on edits."""
    render_answer_form(problem)
    grade = st.session_state.get(CURRENT_GRADE_KEY)
    if isinstance(grade, dict):
        st.divider()
        render_grade(problem, grade)
        if st.button("同じ条件で次の問題", type="primary", width="stretch"):
            request_problem(
                st.session_state[START_DATE_KEY],
                st.session_state[END_DATE_KEY],
                st.session_state[TOPIC_KEY],
            )
            st.rerun()


def render_exercise_tab() -> None:
    today = app_today()
    st.session_state.setdefault(START_DATE_KEY, today - timedelta(days=89))
    st.session_state.setdefault(END_DATE_KEY, today)
    st.session_state.setdefault(TOPIC_KEY, "mixed")
    st.session_state.setdefault(DATA_MODE_KEY, "personal")
    st.session_state.setdefault(TEMPLATE_KEY, "")
    st.session_state.setdefault(SELECTION_KEY, "random")

    source_col, mode_col = st.columns(2)
    with source_col:
        data_mode = st.selectbox("出題データ", list(DATA_LABELS), format_func=DATA_LABELS.get, key=DATA_MODE_KEY)
    with mode_col:
        st.selectbox("出題方法", ["random", "weak"],
                     format_func=lambda key: "弱点を優先" if key == "weak" else "ランダム", key=SELECTION_KEY)
    if data_mode == "synthetic":
        st.info("診断指標・研究デザイン・生存解析などを、実ログとは独立した架空の小さなデータで練習します。期間による実ログの抽出は行いません。")
    if st.session_state[SELECTION_KEY] == "weak":
        st.caption("問題タイプごとの直近5回の成績から、低得点の問題を優先します。未演習も出題し、条件を満たさない問題は除きます。タイプを指定した場合は、その指定を優先します。")
    allowed_topics = {item["topic"] for item in template_catalog(data_mode)}
    choices = [(key, label) for key, label in topic_choices() if key == "mixed" or key in allowed_topics]
    choice_labels = {key: label for key, label in choices}
    valid_keys = list(choice_labels)
    if st.session_state.get(TOPIC_KEY) not in valid_keys:
        st.session_state[TOPIC_KEY] = valid_keys[0]

    topic_col, template_col = st.columns([1, 2])
    with topic_col:
        topic_key = st.selectbox("単元", valid_keys, format_func=choice_labels.get, key=TOPIC_KEY)
    catalog = template_catalog(data_mode, topic_key)
    template_labels = {"": "おまかせ", **{row["template_id"]: row["title"] for row in catalog}}
    if st.session_state[TEMPLATE_KEY] not in template_labels:
        st.session_state[TEMPLATE_KEY] = ""
    with template_col:
        st.selectbox("問題タイプ", list(template_labels), format_func=template_labels.get, key=TEMPLATE_KEY)

    ab.card(
        "一問ずつ、固定したデータで計算",
        "実ログは期間を選んで抽出し、架空データは問題用に生成します。出題後の表・正解・計算条件は変わりません。",
    )
    with st.container():
        start_col, end_col = st.columns(2)
        with start_col:
            start_date = st.date_input("開始日", key=START_DATE_KEY, disabled=data_mode == "synthetic")
        with end_col:
            end_date = st.date_input("終了日", key=END_DATE_KEY, disabled=data_mode == "synthetic")
        generate_col, summary_col = st.columns([2, 1])
        with generate_col:
            generated = st.button("問題を生成", type="primary", width="stretch")
        with summary_col:
            summary_requested = st.button("出題候補を確認", width="stretch")

    if generated:
        request_problem(start_date, end_date, topic_key)
    if summary_requested:
        request_available_summary(start_date, end_date)
    render_available_summary()

    error = st.session_state.get(CURRENT_ERROR_KEY)
    if error:
        st.warning(str(error))

    problem = st.session_state.get(CURRENT_PROBLEM_KEY)
    if not isinstance(problem, dict):
        st.info("期間と単元を選び、「問題を生成」を押すと演習を開始できます。")
        return

    st.divider()
    topic_label = str(problem.get("topic_label") or problem.get("topic") or "統計演習")
    is_synthetic = problem.get("data_origin") == "synthetic"
    origin = "補助用の架空データ（実ログではありません）" if is_synthetic else f"実ログ　｜　{format_period(problem.get('period'))}"
    st.caption(f"{topic_label}　｜　{origin}")
    if problem.get("selection", {}).get("reason"):
        st.caption(problem["selection"]["reason"])
    st.subheader(str(problem.get("title") or "統計演習"))
    prompt = str(problem.get("prompt") or "").strip()
    if prompt:
        st.markdown(prompt)
    render_problem_data(problem)
    render_hints(problem)
    render_exercise_answers(problem)


def attempt_score(attempt: dict[str, Any]) -> float | None:
    grade = attempt.get("grade")
    if isinstance(grade, dict) and grade.get("score") is not None:
        return safe_float(grade.get("score"))
    if attempt.get("score") is not None:
        return safe_float(attempt.get("score"))
    return None


def render_history_tab() -> None:
    st.subheader("学習履歴")
    try:
        attempts = load_attempts(root=ROOT)
    except Exception as exc:
        st.warning(f"学習履歴を読み込めませんでした。（{type(exc).__name__}: {exc}）")
        return
    attempts = [attempt for attempt in attempts if isinstance(attempt, dict)] if isinstance(attempts, list) else []
    if not attempts:
        st.info("まだ採点済みの問題はありません。「表示内容」で演習を選び、最初の一問に挑戦してみましょう。")
        return

    attempts.sort(key=lambda item: str(item.get("attempted_at") or item.get("date") or ""), reverse=True)
    scores = [score for attempt in attempts if (score := attempt_score(attempt)) is not None]
    average_score = sum(scores) / len(scores) if scores else 0.0
    perfect_count = sum(score >= 99.999 for score in scores)

    left, center, right = st.columns(3)
    with left:
        ab.metric_block("解答数", f"{len(attempts)} 問", "保存済みの採点履歴")
    with center:
        ab.metric_block("平均正答率", f"{average_score:.1f}%", "部分点を含む平均")
    with right:
        ab.metric_block("満点", f"{perfect_count} 問", "正答率100%")

    rows: list[dict[str, Any]] = []
    for attempt in attempts:
        attempted_at = attempt.get("attempted_at") or attempt.get("date") or ""
        parsed_at = pd.to_datetime(attempted_at, errors="coerce")
        attempted_label = str(attempted_at)
        if not pd.isna(parsed_at):
            attempted_label = parsed_at.strftime("%Y-%m-%d %H:%M")
        score = attempt_score(attempt)
        topic = str(attempt.get("topic") or "")
        synthetic = attempt.get("problem_snapshot", {}).get("data_origin") == "synthetic"
        rows.append(
            {
                "解答日時": attempted_label,
                "単元": str(attempt.get("topic_label") or TOPIC_LABELS.get(topic, topic or "-")),
                "問題": str(attempt.get("title") or attempt.get("template_id") or "-"),
                "データ": "補助用の架空データ" if synthetic else "実ログ",
                "正答率": f"{score:.1f}%" if score is not None else "-",
                "結果": "満点" if score is not None and score >= 99.999 else "復習候補",
                "対象期間": "期間不使用" if synthetic else format_period(attempt.get("period")),
            }
        )
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    if st.checkbox("問題タイプごとの成績・弱点を表示", key=HISTORY_PROGRESS_VISIBLE_KEY):
        progress = sorted(practice_progress(attempts), key=lambda item: item["weight"], reverse=True)
        st.caption("直近5回の平均から出題優先度を計算します。低得点ほど優先し、未演習も候補に残します。")
        st.dataframe(pd.DataFrame([
            {"単元": item["topic_label"], "問題タイプ": item["title"], "解答数": item["attempt_count"],
             "直近の平均": f"{item['average_score']:.1f}%" if item["average_score"] is not None else "未演習",
             "集計件数": item["recent_count"], "出題の重み": round(item["weight"], 2)}
            for item in progress
        ]), width="stretch", hide_index=True)

    if st.checkbox("保存済みの問題・解説を振り返る", key=HISTORY_DETAIL_VISIBLE_KEY):
        options = {str(item["attempt_id"]): item for item in attempts if item.get("attempt_id")}
        if not options:
            st.info("詳細を表示できる解答履歴はありません。")
            return
        if st.session_state.get(HISTORY_DETAIL_KEY) not in options:
            st.session_state[HISTORY_DETAIL_KEY] = next(iter(options))
        selected = st.selectbox(
            "解答履歴", list(options), key=HISTORY_DETAIL_KEY,
            format_func=lambda key: f"{options[key].get('attempted_at', '')}｜{options[key].get('title', '')}",
        )
        attempt = options.get(selected, {})
        saved_problem = attempt.get("problem_snapshot")
        if isinstance(saved_problem, dict) and isinstance(attempt.get("grade"), dict):
            st.caption(saved_problem.get("data_origin_label", "PROLOAGの実ログ"))
            st.markdown(saved_problem.get("prompt", ""))
            render_problem_data(saved_problem)
            render_grade(saved_problem, attempt["grade"])


def render_scope_tab() -> None:
    st.subheader("第一・第二段階の収録範囲")
    st.write(
        "現在のPROLOAGに蓄積された日別・セッション別の記録を、問題ごとに固定して出題します。"
        "数値計算だけでなく、結果の読み方とデータ上の仮定も確認します。"
    )

    scope_rows = [
        {"領域": "代表値と散布度", "主な演習": "平均、標本分散、標準偏差", "主なデータ": "学習時間・睡眠時間など"},
        {"領域": "割合と条件付き確率", "主な演習": "度数、割合、クロス集計、条件付き確率", "主なデータ": "学習環境・分類・記録条件"},
        {"領域": "分布と標準化", "主な演習": "二項分布の期待値・分散、z得点", "主なデータ": "日別・セッション別指標"},
        {"領域": "推定", "主な演習": "標準誤差、母平均・母比率の95%信頼区間", "主なデータ": "日別学習時間・条件割合"},
        {"領域": "カテゴリ分析", "主な演習": "行割合、クロス表の期待度数", "主なデータ": "2つのカテゴリ記録"},
        {"領域": "相関・回帰", "主な演習": "Pearson相関、単回帰の傾き・切片、因果との区別", "主なデータ": "睡眠・運動・学習の組合せ"},
        {"領域": "仮説検定", "主な演習": "1標本t・Welch・対応ありt・χ²・Fisher、p値と棄却判断", "主なデータ": "実ログの数値・カテゴリ・隣接日のペア"},
        {"領域": "ノンパラメトリック", "主な演習": "Wilcoxon符号付順位、ゼロ差・同順位・両側p値", "主なデータ": "実ログの同じ指標・隣接日のペア"},
        {"領域": "ロジスティック回帰", "主な演習": "与えた係数からlogit・確率・ORを計算", "主なデータ": "補助用の架空データ"},
        {"領域": "生存時間解析", "主な演習": "Kaplan–Meier法、リスク集合・打切り・生存率", "主なデータ": "補助用の架空データ"},
        {"領域": "診断指標・研究デザイン", "主な演習": "感度・特異度・PPV・NPV・RR・OR・リスク差、RCT・ITT", "主なデータ": "補助用の架空データ"},
    ]
    st.dataframe(pd.DataFrame(scope_rows), width="stretch", hide_index=True)

    st.markdown("#### 今後の拡張候補")
    st.write(
        "ロジスティック回帰の係数推定そのもの、Cox回帰・ログランク検定、手書き途中式の採点、復習間隔の自動調整などは未実装です。"
        "第二段階では、提示済み係数による計算と小規模なKaplan–Meier表を扱います。"
    )

    st.markdown("#### 出題時の扱い")
    st.markdown(
        "- 元の記録は読み取り専用で、問題生成によって変更されません。\n"
        "- 問題に使った値はスナップショットとして固定し、後からログを編集しても採点結果を再現できます。\n"
        "- 欠測値は0として補わず、問題ごとの条件に合う観測だけを使います。\n"
        "- 同じ人の日々の記録であることを踏まえ、関連をそのまま因果関係とは扱いません。\n"
        "- 日記本文やメモなどの自由記述は、問題データに使用しません。"
        "\n- 仮説検定は両側・有意水準5%。χ²は期待度数の条件を確認し、Fisher・Wilcoxonの計算規則を問題に明記します。"
        "\n- 架空データは実ログから生成せず、問題文・解答履歴で区別します。"
    )


retain_practice_inputs()
selected_view = st.radio("表示内容", ["演習", "学習履歴", "収録範囲"], horizontal=True, key=VIEW_KEY)
if selected_view == "演習":
    render_exercise_tab()
elif selected_view == "学習履歴":
    render_history_tab()
else:
    render_scope_tab()
