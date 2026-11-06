from __future__ import annotations

import argparse
from datetime import date, timedelta
from functools import wraps
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any
from unittest.mock import patch


ANCHOR = date(2026, 8, 27)
SOURCE_FOLDERS = ("app_core", "prolog_ui", "pages")


def copy_sources(source: Path, destination: Path) -> None:
    """Copy code/styles only; never copy personal JSON, settings or dependency trees."""
    destination.mkdir(parents=True, exist_ok=True)
    for path in source.glob("*.py"):
        shutil.copy2(path, destination / path.name)
    for folder in SOURCE_FOLDERS:
        for path in (source / folder).rglob("*"):
            if path.is_file() and path.suffix in {".py", ".css"}:
                target = destination / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def daily_path(root: Path, folder: str, target: date) -> Path:
    return root / folder / f"{target:%Y}" / f"{target:%m}" / f"{target:%Y%m%d}.json"


def create_fixtures(root: Path) -> None:
    write_json(root / "settings" / "demo_mode.json", {"enabled": True, "fixed_today": ANCHOR.isoformat()})
    write_json(root / "settings" / "user_settings.json", {
        "display": {"show_task_metrics": False, "theme_mode": "standard"},
        "analysis": {"caffeine_missing_as_zero": False},
    })
    for days_ago in range(119, -1, -1):
        target = ANCHOR - timedelta(days=days_ago)
        running = days_ago % 4 in {0, 1}
        hours = 1.0 + (days_ago % 11) * 0.3 + (0.2 if running else 0.0)
        if days_ago < 7 or days_ago % 11 != 0:
            start_hour = 1 if days_ago == 1 else 10
            write_json(daily_path(root, "study_logs", target), [{
                "session_id": f"smoke_session_{target:%Y%m%d}",
                "start_at": f"{target.isoformat()}T{start_hour:02d}:00:00",
                "end_at": f"{target.isoformat()}T{start_hour + 4:02d}:00:00",
                "duration_sec": round(hours * 3600),
                "money": round(hours * 2000),
                "hourly_rate": 2000,
                "memo": "合成データ 学習確認",
                "condition": {"category": "医学", "io_type": "アウトプット", "effort": 1 + days_ago % 5},
            }])
        write_json(daily_path(root, "health_logs", target), {
            "date": target.isoformat(),
            "sleep": {
                "bed_time": "23:00", "wake_time": "06:30",
                "sleep_duration_min": 360 + (days_ago % 5) * 30,
                "sleep_quality": 1 + days_ago % 5,
                "morning_sleepiness": 1 + (days_ago * 3) % 5,
            },
            "condition": {
                "physical_score": 1 + (days_ago * 3) % 5,
                "focus_score": 1 + (days_ago * 2) % 5,
                "fatigue": 1 + (days_ago + 2) % 5,
                "stress": 1 + (days_ago + 3) % 5,
                "mood_score": 1 + (days_ago + 1) % 5,
            },
        })
        write_json(daily_path(root, "daily_plans", target), {
            "date": target.isoformat(), "target_total_minutes": 120 + (days_ago % 3) * 30,
            "tasks": [],
        })
        if running:
            write_json(daily_path(root, "exercise_logs", target), {
                "date": target.isoformat(),
                "records": [{
                    "exercise_id": f"smoke_exercise_{target:%Y%m%d}", "type": "running",
                    "start_time": "07:00", "duration_min": 40, "distance_km": 5.0,
                    "intensity": 4 + days_ago % 5, "fatigue_after": 1 + days_ago % 5,
                }],
            })
        elif days_ago % 4 == 2:
            # Explicit no-running observation, distinct from absent exercise days.
            write_json(daily_path(root, "exercise_logs", target), {"date": target.isoformat(), "records": []})
        if days_ago % 8 != 0 and days_ago != 1:
            amount = 0 if days_ago % 3 == 0 else 200
            intake_time = "23:00:00" if days_ago == 2 else "08:00:00"
            records = [{
                "caffeine_id": f"smoke_caffeine_{target:%Y%m%d}",
                "taken_at": f"{target.isoformat()}T{intake_time}",
                "amount_mg": amount, "label": "合成タブレット",
            }] if amount else []
            write_json(daily_path(root, "diary_entries", target), {
                "date": target.isoformat(), "caffeine_mg": amount, "caffeine_untimed_mg": 0,
                "caffeine_records": records,
                "good_seat_status": ("yes", "no", "so_so")[days_ago % 3],
            })
    future = ANCHOR + timedelta(days=1)
    write_json(daily_path(root, "study_logs", future), [])
    write_json(daily_path(root, "health_logs", future), {
        "date": future.isoformat(),
        "sleep": {"sleep_duration_min": 0, "morning_sleepiness": 3},
        "condition": {"focus_score": 3, "fatigue": 3, "stress": 3},
    })
    write_json(daily_path(root, "exercise_logs", future), {"records": []})
    write_json(daily_path(root, "diary_entries", future), {"caffeine_mg": 0})
    partial = ANCHOR - timedelta(days=40)
    write_json(daily_path(root, "diary_entries", partial), {"caffeine_mg": 100})
    write_json(daily_path(root, "exercise_logs", partial), {"records": [{"type": "running"}]})
    write_json(daily_path(root, "health_logs", partial), {"condition": {"focus_score": 0}})


def snapshot_json(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*.json")
    }


def run_worker(root: Path, *, extended: bool = False, additional_only: bool = False) -> None:
    temporary_base = Path(tempfile.gettempdir()).resolve()
    if (
        root.name != "app"
        or not root.parent.name.startswith("proloag-analysis-smoke-")
        or root.parent.parent.resolve() != temporary_base
    ):
        raise RuntimeError("The smoke worker accepts only its managed temporary app copy")
    logging.disable(logging.INFO)
    sys.path.insert(0, str(root))
    import config
    if Path(config.PROJECT_ROOT).resolve() != root.resolve():
        raise RuntimeError("App imports did not resolve to the isolated temporary source tree")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import pandas as pd
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    st.page_link = lambda *args, **kwargs: None
    captured: list[dict[str, Any]] = []
    original_pyplot = st.pyplot

    @wraps(original_pyplot)
    def capture_pyplot(fig: Any = None, *args: Any, **kwargs: Any) -> Any:
        if fig is not None:
            captured.append({
                "lines": [
                    {"label": line.get_label(), "values": np.asarray(line.get_ydata()).copy()}
                    for axis in fig.axes for line in axis.lines
                ],
            })
        return original_pyplot(fig, *args, **kwargs)

    st.pyplot = capture_pyplot
    baseline = snapshot_json(root)
    completed: list[str] = []

    def checked_run(app: Any, name: str, *, expect_plots: bool = True) -> Any:
        captured.clear()
        app.run(timeout=120)
        if len(app.exception):
            messages = [str(item.message) for item in app.exception]
            raise AssertionError(f"{name}: {messages}")
        if expect_plots and not captured:
            raise AssertionError(f"{name}: no graph was rendered")
        if snapshot_json(root) != baseline:
            raise AssertionError(f"{name}: a read-only analysis page changed its JSON fixtures")
        plt.close("all")
        completed.append(name)
        print(f"PASS {name}: graphs={len(captured)} tables={len(app.dataframe)}", flush=True)
        return app

    def widget_by_label(widgets: Any, label: str) -> Any:
        found = [widget for widget in widgets if widget.label == label]
        if len(found) != 1:
            raise AssertionError(f"Expected one widget named {label}, got {len(found)}")
        return found[0]

    def table_with(app: Any, required: set[str]) -> Any:
        candidates = [item.value for item in app.dataframe if required.issubset(set(item.value.columns))]
        if not candidates:
            raise AssertionError(f"Missing UI table columns: {sorted(required)}")
        return candidates[0]

    def run_additional_pages() -> None:
        failures: list[str] = []
        for prefix in ("01", "02", "04", "05", "09", "12", "13", "18", "97", "98", "99"):
            try:
                paths = sorted((root / "pages").glob(f"{prefix}_*.py"))
                if len(paths) != 1:
                    raise AssertionError(f"Expected one page for prefix {prefix}, got {len(paths)}")
                app = AppTest.from_file(paths[0], default_timeout=120)
                checked_run(app, f"{prefix} initial view", expect_plots=False)
                if prefix == "05":
                    app.selectbox(key="statistics_practice_template").set_value("descriptive_mean")
                    widget_by_label(app.button, "問題を生成").click()
                    checked_run(app, "05 generated problem stays read-only", expect_plots=False)
                    problem = app.session_state["statistics_practice_current_problem"]
                    if problem.get("template_id") != "descriptive_mean":
                        raise AssertionError("05: the explicitly requested mean problem was not generated")
                    table_with(app, {"番号", "日付"})
                if prefix == "99":
                    widget_by_label(app.button, "チェックを実行する").click()
                    checked_run(app, "99 read-only integrity check", expect_plots=False)
                    table_with(app, {"検査項目"})
            except Exception as exc:
                failures.append(f"{prefix}: {exc}")
                print(f"FAIL {prefix}: {exc}", flush=True)
                plt.close("all")
        if failures:
            raise AssertionError("Additional UI smoke failures: " + "; ".join(failures))

    if additional_only:
        run_additional_pages()
        print(f"UI SMOKE PASSED: {len(completed)} additional states; {len(baseline)} synthetic JSON files unchanged", flush=True)
        return

    app03 = AppTest.from_file(root / "pages" / "03_可視化_ダッシュボード.py", default_timeout=120)
    checked_run(app03, "03 default")
    for name, start, end in (
        ("03 sparse calendar", ANCHOR - timedelta(days=34), ANCHOR - timedelta(days=21)),
        ("03 dense calendar", ANCHOR - timedelta(days=6), ANCHOR),
    ):
        widget_by_label(app03.date_input, "開始日").set_value(start)
        widget_by_label(app03.date_input, "終了日").set_value(end)
        checked_run(app03, name)
        moving_average = [line for fig in captured for line in fig["lines"] if line["label"] == "7日移動平均"]
        if not moving_average or len(moving_average[0]["values"]) != (end - start).days + 1:
            raise AssertionError(f"{name}: moving average did not cover calendar days")
        expected = 0.0
        for offset in range(7):
            path = daily_path(root, "study_logs", end - timedelta(days=offset))
            if path.exists():
                expected += sum(float(row["duration_sec"]) for row in json.loads(path.read_text(encoding="utf-8"))) / 3600
        if not np.isclose(moving_average[0]["values"][-1], expected / 7):
            raise AssertionError(f"{name}: moving average differs from the calendar-based expectation")

    app16 = AppTest.from_file(root / "pages" / "16_カフェイン・運動分析.py", default_timeout=120)
    checked_run(app16, "16 mixed missing and zero days")
    view = app16.radio(key="rhythm_analysis_view")
    if view.options != ["カフェイン比較", "摂取タイミング", "勉強・体調", "翌日ラグ", "ランニング", "日別データ"]:
        raise AssertionError("16: all six analysis views must remain available")
    comparison = table_with(app16, {"平均勉強時間", "平均勉強時間 有効n", "平均睡眠時間 有効n"})
    if (comparison["平均勉強時間 有効n"] > comparison["日数"]).any():
        raise AssertionError("16: effective metric counts exceed the group size")
    for label in view.options[1:]:
        app16.radio(key="rhythm_analysis_view").set_value(label)
        checked_run(app16, f"16 selected view: {label}")
    daily = table_with(app16, {"日付", "カフェインmg", "カフェイン帯"})
    if not (daily["カフェイン帯"] == "未記録").any() or not (daily["カフェインmg"] == 0).any():
        raise AssertionError("16: missing and stored-zero days were not both rendered")
    if daily.loc[daily["カフェイン帯"] == "未記録", "カフェインmg"].notna().any():
        raise AssertionError("16: an absent diary day became zero mg")
    if not {"未記録", "ランニングなし", "ランニングあり"}.issubset(set(daily["ランニング記録"])):
        raise AssertionError("16: known no-running and unrecorded days must remain distinct")
    app16.radio(key="substance_residual_comparison_mode").set_value("残存量（mg）")
    app16.radio(key="substance_residual_layout").set_value("1列")
    app16.selectbox(key="substance_residual_display_hours").set_value(72)
    checked_run(app16, "16 alternate residual controls")
    widget_by_label(app16.selectbox, "期間").set_value("任意期間")
    checked_run(app16, "16 custom-period controls")
    cross_day = ANCHOR - timedelta(days=1)
    widget_by_label(app16.date_input, "開始日").set_value(cross_day)
    widget_by_label(app16.date_input, "終了日").set_value(cross_day)
    app16.radio(key="rhythm_analysis_view").set_value("摂取タイミング")
    checked_run(app16, "16 cross-midnight intake outside display period")
    timing = table_with(app16, {"セッション開始", "直近摂取", "開始前6時間mg"})
    if len(timing) != 1 or not np.isclose(float(timing.iloc[0]["開始前6時間mg"]), 200):
        raise AssertionError("16: preceding-day intake was not included in the UI six-hour total")
    if str(timing.iloc[0]["直近摂取"]) != f"{(ANCHOR - timedelta(days=2)).isoformat()} 23:00":
        raise AssertionError("16: preceding-day last-intake timestamp was not displayed")
    zero_day = ANCHOR - timedelta(days=3)
    widget_by_label(app16.date_input, "開始日").set_value(zero_day)
    widget_by_label(app16.date_input, "終了日").set_value(zero_day)
    app16.radio(key="rhythm_analysis_view").set_value("カフェイン比較")
    checked_run(app16, "16 single stored-zero group")
    # The terminal day's next outcome must survive slicing to a single day.
    observed_terminal = ANCHOR - timedelta(days=2)
    widget_by_label(app16.date_input, "開始日").set_value(observed_terminal)
    widget_by_label(app16.date_input, "終了日").set_value(observed_terminal)
    app16.radio(key="rhythm_analysis_view").set_value("翌日ラグ")
    checked_run(app16, "16 terminal next-day context outside display")
    lag = table_with(app16, {"翌日睡眠時間", "翌日睡眠時間 有効n"})
    if len(lag) != 1 or float(lag.iloc[0]["翌日睡眠時間"]) != 6.5 or int(lag.iloc[0]["翌日睡眠時間 有効n"]) != 1:
        raise AssertionError("16: single-day terminal next-day context or effective count was lost")
    widget_by_label(app16.date_input, "開始日").set_value(ANCHOR)
    widget_by_label(app16.date_input, "終了日").set_value(ANCHOR + timedelta(days=1))
    app16.radio(key="rhythm_analysis_view").set_value("日別データ")
    checked_run(app16, "16 future stored defaults stay missing")
    daily = table_with(app16, {"日付", "カフェインmg", "学習記録", "ランニング記録"})
    future_row = daily.loc[daily["日付"] == (ANCHOR + timedelta(days=1)).isoformat()].iloc[0]
    if pd.notna(future_row["カフェインmg"]) or future_row["学習記録"] != "未記録" or future_row["ランニング記録"] != "未記録":
        raise AssertionError("16: future default files became actual observations")
    partial = ANCHOR - timedelta(days=40)
    widget_by_label(app16.date_input, "開始日").set_value(partial)
    widget_by_label(app16.date_input, "終了日").set_value(partial)
    checked_run(app16, "16 partial health and exercise values stay missing")
    daily = table_with(app16, {"日付", "睡眠の質", "ラン平均きつさ", "集中しやすさ"})
    if pd.notna(daily.iloc[0]["睡眠の質"]) or pd.notna(daily.iloc[0]["ラン平均きつさ"]) or daily.iloc[0]["集中しやすさ"] != 0:
        raise AssertionError("16: partial records imported defaults or discarded explicit zero")
    # Exercise the alternate setting without editing any fixture JSON. The
    # settings service's default/persistence has its own dedicated UI tests.
    with patch("app_core.settings_service.should_treat_missing_caffeine_as_zero", return_value=True):
        app16_zero = AppTest.from_file(root / "pages" / "16_カフェイン・運動分析.py", default_timeout=120)
        checked_run(app16_zero, "16 missing caffeine as zero setting: comparison")
        app16_zero.radio(key="rhythm_analysis_view").set_value("日別データ")
        checked_run(app16_zero, "16 missing caffeine as zero setting: daily data")
        daily = table_with(app16_zero, {"日付", "カフェイン記録", "カフェインmg", "勉強時間"})
        imputed = daily.loc[daily["カフェイン記録"] == "未記録（0mg扱い）"]
        if imputed.empty or not imputed["カフェインmg"].eq(0).all():
            raise AssertionError("16: missing-as-zero setting did not preserve raw status and impute comparison values")
        if not (daily["勉強時間"] == "未記録").any():
            raise AssertionError("16: caffeine setting incorrectly filled missing study observations")
        app17_zero = AppTest.from_file(root / "pages" / "17_学習パフォーマンス・条件分析.py", default_timeout=120)
        checked_run(app17_zero, "17 missing caffeine as zero setting")
        details = table_with(app17_zero, {"日付", "カフェイン状態"})
        if not details["カフェイン状態"].eq("未記録（0mg扱い）").any():
            raise AssertionError("17: missing-as-zero setting did not reach performance analysis")

    app17 = AppTest.from_file(root / "pages" / "17_学習パフォーマンス・条件分析.py", default_timeout=120)
    checked_run(app17, "17 dense history profile and paired comparison")
    profile = table_with(app17, {"条件", "比較方法", "差", "95%区間"})
    rate = profile.loc[profile["条件"] == "ランニング記録率"]
    if len(rate) != 1 or rate.iloc[0]["比較方法"] != "記録率差":
        raise AssertionError("17: the binary rate row was not identified as a rate difference")
    pairs = table_with(app17, {"条件Aの日", "条件Bの日", "成果差"})
    if pairs.empty:
        raise AssertionError("17: comparable-day pairs were not rendered")
    app17.selectbox(key="performance_mode").set_value("直近28日中央値差")
    app17.selectbox(key="performance_primary").set_value("総勉強時間")
    app17.selectbox(key="performance_next").set_value("翌日集中しやすさ")
    app17.selectbox(key="performance_factor").set_value("ランニング記録")
    checked_run(app17, "17 rolling baseline and alternate factor")
    widget_by_label(app17.selectbox, "対象期間").set_value("任意期間")
    checked_run(app17, "17 custom-period controls")
    period_start = ANCHOR - timedelta(days=59)
    app17.date_input(key="performance_start").set_value(period_start)
    app17.date_input(key="performance_end").set_value(ANCHOR - timedelta(days=1))
    checked_run(app17, "17 preceding baseline and observed terminal next day")
    details = table_with(app17, {"日付", "学習記録", "翌日指標：翌日集中しやすさ"})
    terminal = details.loc[details["日付"] == (ANCHOR - timedelta(days=1)).isoformat()].iloc[0]
    if terminal["翌日指標：翌日集中しやすさ"] != 1:
        raise AssertionError("17: the observed next day outside the display period was omitted")
    first = details.loc[details["日付"] == period_start.isoformat()].iloc[0]
    primary_column = next(column for column in details.columns if column.startswith("主指標："))
    if pd.isna(first[primary_column]):
        raise AssertionError("17: the display-period first day lost its preceding 28-day baseline")
    app17.date_input(key="performance_end").set_value(ANCHOR)
    checked_run(app17, "17 future stored defaults excluded")
    details = table_with(app17, {"日付", "学習記録", "翌日指標：翌日集中しやすさ"})
    if pd.notna(details.loc[details["日付"] == ANCHOR.isoformat(), "翌日指標：翌日集中しやすさ"].iloc[0]):
        raise AssertionError("17: a future stored default was rendered as an observed next day")
    app17.multiselect(key="performance_caffeine_status").set_value([])
    checked_run(app17, "17 empty analysis filter", expect_plots=False)
    if not len(app17.info):
        raise AssertionError("17: an empty filter should explain why no comparison is available")
    if extended:
        run_additional_pages()
    if snapshot_json(root) != baseline:
        raise AssertionError("Analysis UI smoke changed its synthetic JSON inputs")
    print(f"UI SMOKE PASSED: {len(completed)} states; {len(baseline)} synthetic JSON files unchanged", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run isolated read-only Streamlit UI smoke checks for analysis pages.")
    parser.add_argument("--worker-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--extended", action="store_true", help="Also smoke-test the main record, export, settings and integrity pages.")
    parser.add_argument("--additional-only", action="store_true", help="Run only the additional record, export, settings and integrity pages.")
    args = parser.parse_args()
    if args.worker_root is not None:
        run_worker(args.worker_root.resolve(), extended=args.extended, additional_only=args.additional_only)
        return 0
    source = Path(__file__).resolve().parents[1]
    dependencies = source / ".deps311"
    if not dependencies.is_dir():
        raise RuntimeError("The project's .deps311 directory is required for the smoke test")
    with tempfile.TemporaryDirectory(prefix="proloag-analysis-smoke-") as temporary:
        workspace = Path(temporary)
        isolated = workspace / "app"
        copy_sources(source, isolated)
        create_fixtures(isolated)
        runner = workspace / "smoke_runner.py"
        shutil.copy2(Path(__file__).resolve(), runner)
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join([str(isolated), str(dependencies)])
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["MPLCONFIGDIR"] = str(workspace / "matplotlib")
        environment["STREAMLIT_GLOBAL_DEVELOPMENT_MODE"] = "false"
        environment["STREAMLIT_LOGGER_LEVEL"] = "error"
        environment["STREAMLIT_SERVER_ENABLE_CORS"] = "true"
        command = [sys.executable, "-B", str(runner), "--worker-root", str(isolated)]
        if args.extended:
            command.append("--extended")
        if args.additional_only:
            command.append("--additional-only")
        result = subprocess.run(
            command,
            cwd=workspace, env=environment, check=False,
        )
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
