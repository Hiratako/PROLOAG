from __future__ import annotations

import ast
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import itertools
import json
from pathlib import Path
import tempfile
from typing import Any
import unittest

import pandas as pd

from app_core.analysis_dates import analysis_datetime_series, parse_analysis_date, parse_analysis_datetime
from app_core.caffeine_timing_service import parse_record_datetime
from app_core.daily_analysis_service import build_calendar_study_totals
from app_core.json_store import record_json_files
from data_utils import load_study_sessions_file


ROOT = Path(__file__).resolve().parents[1]


def page_functions(filename: str, names: set[str], **extra: Any) -> dict[str, Any]:
    path = ROOT / "pages" / filename
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {
        "Any": Any, "date": date, "datetime": datetime, "Path": Path,
        "pd": pd, "defaultdict": defaultdict, **extra,
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


class AnalysisDateTests(unittest.TestCase):
    def test_mixed_iso_formats_are_independent_of_first_row(self) -> None:
        values = ["2026-09-01T08:00:00", "2026-09-01 09:00:00", "2026-09-01T10:00:00.123456"]
        for order in itertools.permutations(values):
            with self.subTest(order=order):
                result = analysis_datetime_series(order)
                self.assertFalse(result.isna().any())
                self.assertEqual(result.tolist(), [pd.Timestamp(datetime.fromisoformat(value)) for value in order])

    def test_missing_and_invalid_values_are_not_epoch_or_prefix_dates(self) -> None:
        for value in (None, "", "not-a-date", "2026-09-01garbage", "2026-02-30", 0, 1, True, False, [], {}, pd.NaT, pd.NA, float("nan")):
            with self.subTest(value=repr(value)):
                self.assertIsNone(parse_analysis_datetime(value))
                self.assertIsNone(parse_analysis_date(value))
        self.assertTrue(analysis_datetime_series([None, pd.NaT, "bad"]).isna().all())

    def test_date_and_naive_time_remain_local_wall_time(self) -> None:
        self.assertEqual(parse_analysis_datetime(date(2026, 9, 1)), datetime(2026, 9, 1))
        self.assertEqual(parse_analysis_datetime(" 2026-09-01 "), datetime(2026, 9, 1))
        value = datetime(2026, 9, 1, 8, 2, 3, 456789)
        self.assertEqual(parse_analysis_datetime(value), value)
        self.assertEqual(parse_analysis_datetime(value.isoformat()), value)

    def test_offset_and_z_representations_of_same_instant_agree(self) -> None:
        utc = datetime(2026, 9, 1, 23, 30, 0, 123456, tzinfo=timezone.utc)
        expected = utc.astimezone().replace(tzinfo=None)
        offset = utc.astimezone(timezone(timedelta(hours=9)))
        for value in (utc, offset.isoformat(), utc.isoformat().replace("+00:00", "Z")):
            with self.subTest(value=value):
                self.assertEqual(parse_analysis_datetime(value), expected)
                self.assertEqual(parse_analysis_date(value), expected.date())
                self.assertEqual(parse_record_datetime(value), expected)

    def test_series_keeps_index_name_and_microseconds(self) -> None:
        source = pd.Series(["bad", "2026-09-01 08:00:00.123456"], index=[9, 4], name="start_at")
        before = source.copy(deep=True)
        result = analysis_datetime_series(source)
        self.assertEqual(result.index.tolist(), [9, 4])
        self.assertEqual(result.name, "start_at")
        self.assertTrue(pd.isna(result.loc[9]))
        self.assertEqual(result.loc[4].microsecond, 123456)
        pd.testing.assert_series_equal(source, before)

    def test_calendar_aggregation_keeps_every_valid_mixed_date(self) -> None:
        source = pd.DataFrame({
            "date": ["2026-09-01T08:00:00", "2026-09-01 09:00:00.12", date(2026, 9, 3), "bad"],
            "duration_sec": [3600, 7200, 3600, 99999], "money": [10, 20, 10, 99999],
        })
        before = source.copy(deep=True)
        result = build_calendar_study_totals(source, date(2026, 9, 1), date(2026, 9, 3))
        self.assertEqual(result["study_hours"].tolist(), [3, 0, 1])
        self.assertEqual(result["sessions"].tolist(), [2, 0, 1])
        self.assertEqual(result.iloc[-1]["cum_hours"], 4)
        pd.testing.assert_frame_equal(source, before)

    def test_learning_pages_share_local_day_and_hour(self) -> None:
        value = "2026-09-01T23:30:00-05:00"
        expected = datetime.fromisoformat(value).astimezone().replace(tzinfo=None)
        learning = page_functions("11_学習蓄積ビジュアライザー.py", {"parse_day", "most_studied_hour"})
        city = page_functions("15_知識都市ビュー.py", {"parse_day"})
        study = page_functions("02_勉強ログ.py", {"table_datetime"})
        self.assertEqual(learning["parse_day"](value), expected.date())
        self.assertEqual(city["parse_day"](value), expected.date())
        self.assertEqual(study["table_datetime"](value), expected)
        self.assertEqual(learning["most_studied_hour"](
            expected.date(), expected.date(), {}, sessions=[{"start_at": value, "duration_sec": 3600}],
        ), f"{expected.hour:02d}:00台")


class DashboardDateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.functions = page_functions(
            "03_可視化_ダッシュボード.py", {"load_sessions", "load_df"},
            STUDY_DIR=self.root / "study_logs", record_json_files=record_json_files,
            analysis_datetime_series=analysis_datetime_series,
            load_study_sessions_file=load_study_sessions_file,
            WEEKDAY_ORDER=["月", "火", "水", "木", "金", "土", "日"],
        )

    def write_fixture(self, name: str, payload: object) -> Path:
        path = self.root / "study_logs" / "2026" / "09" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_real_dashboard_loader_keeps_mixed_dates_and_reports_invalid_count(self) -> None:
        records = [
            {"start_at": value, "duration_sec": 3600, "money": 10, "unknown": {"keep": True}}
            for value in ("2026-09-01T08:00:00", "2026-09-01 09:00:00", "2026-09-01T10:00:00.12", "bad")
        ]
        path = self.write_fixture("20260901.json", {"sessions": records, "outer_metadata": "keep"})
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        frame = self.functions["load_df"]()
        self.assertEqual(len(frame), 3)
        self.assertEqual(frame["duration_sec"].sum(), 10800)
        self.assertEqual(frame["hour"].tolist(), [8, 9, 10])
        self.assertEqual(frame.attrs["invalid_datetime_count"], 1)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(list(self.root.rglob("*.*")), [path])

    def test_all_invalid_or_absent_starts_keep_warning_count_without_crashing(self) -> None:
        self.write_fixture("20260901.json", [{"start_at": "bad", "duration_sec": 100}, {"duration_sec": 100}])
        frame = self.functions["load_df"]()
        self.assertTrue(frame.empty)
        self.assertEqual(frame.attrs["invalid_datetime_count"], 2)

    def test_empty_store_is_read_only(self) -> None:
        frame = self.functions["load_df"]()
        self.assertTrue(frame.empty)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_invalid_datetime_warning_is_visible_even_when_all_rows_are_invalid(self) -> None:
        from streamlit import config
        from streamlit.testing.v1 import AppTest

        config.get_config_options(options_from_flags={"global.developmentMode": False, "browser.gatherUsageStats": False})
        self.write_fixture("20260901.json", [{"start_at": "bad", "duration_sec": 100}])
        path = ROOT / "pages" / "03_可視化_ダッシュボード.py"
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in {"load_sessions", "load_df"}]
        warning = next(node for node in tree.body if isinstance(node, ast.If) and "df.attrs.get('invalid_datetime_count'" in ast.unparse(node.test))
        source = (
            "from typing import Any\nfrom pathlib import Path\nimport pandas as pd\nimport streamlit as st\n"
            "from app_core.analysis_dates import analysis_datetime_series\n"
            "from app_core.json_store import record_json_files\nfrom data_utils import load_study_sessions_file\n"
            f"STUDY_DIR = Path({str(self.root / 'study_logs')!r})\n"
            "WEEKDAY_ORDER = ['月', '火', '水', '木', '金', '土', '日']\n"
            + "\n\n".join(ast.unparse(node) for node in functions)
            + "\ndf = load_df()\n" + ast.unparse(warning)
        )
        app = AppTest.from_string(source, default_timeout=15).run()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.warning), 1)
        self.assertIn("1 件", app.warning[0].value)
        self.assertIn("元の記録は変更していません", app.warning[0].value)


if __name__ == "__main__":
    unittest.main()
