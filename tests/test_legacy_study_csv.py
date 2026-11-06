from __future__ import annotations

import csv
from datetime import datetime, timedelta
from decimal import Decimal
import io
import unittest
from unittest.mock import patch

from app_core import legacy_study_csv as legacy


SESSION_COLUMNS = ["start", "end", "duration_sec", "hourly_wage", "money", "memo",
                   "cond_effort_10", "cond_env", "cond_sleep_band", "cond_alcohol_yday",
                   "cond_mood_5", "cond_topic", "__src", "cond_io", "start_dt", "end_dt",
                   "date", "勉強時間", "金額換算(円)", "日付", "勉強内容"]
SUMMARY_COLUMNS = ["日付", "勉強時間(合計)", "金額換算(合計・円)", "勉強内容(集約)", "セッション数"]


def csv_bytes(rows, columns=SESSION_COLUMNS, encoding="utf-8-sig"):
    target = io.StringIO(newline="")
    writer = csv.DictWriter(target, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    return target.getvalue().encode(encoding)


def hms(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def record(start="2026-01-01T09:00:00", duration="3600.75", memo="架空の復習", **updates):
    started = datetime.fromisoformat(start)
    ended = started + timedelta(seconds=float(duration))
    money = Decimal(duration) * Decimal("1000") / 3600
    result = {"start": started.strftime("%Y-%m-%d %H:%M:%S"),
              "end": ended.strftime("%Y-%m-%d %H:%M:%S"), "duration_sec": duration,
              "hourly_wage": "1000.0", "money": str(money), "memo": memo,
              "cond_effort_10": "6.0", "cond_env": "cowork", "cond_sleep_band": "6h",
              "cond_alcohol_yday": "0.0", "cond_mood_5": "3.0", "cond_topic": "medical",
              "__src": "C:/synthetic/old/study_logs/2026/01/20260101.json", "cond_io": "both",
              "start_dt": started.isoformat(sep=" "), "end_dt": ended.isoformat(sep=" "),
              "date": started.date().isoformat(), "勉強時間": hms(Decimal(duration)),
              "金額換算(円)": str(money.quantize(Decimal("0.1"))),
              "日付": started.date().isoformat(), "勉強内容": memo}
    result.update(updates)
    return result


def issue_codes(report):
    return [issue["code"] for issue in report["issues"]] + [
        issue["code"] for row in report["rows"] for issue in row["issues"]]


class LegacyCsvDiagnosisTests(unittest.TestCase):
    def test_precise_values_and_originals_are_retained(self):
        original = record()
        report = legacy.diagnose_legacy_csv(csv_bytes([original]))
        row = report["rows"][0]
        self.assertEqual(report["summary"]["error_count"], 0)
        self.assertEqual(row["session"]["duration_sec"], 3600.75)
        self.assertEqual(row["session"]["money"], float(Decimal(original["money"])))
        self.assertEqual(row["session"]["end_at"], "2026-01-01T10:00:00.750000")
        self.assertEqual(row["raw"], original)
        self.assertEqual(row["session"]["condition"]["environment"], "ワーキングスペース")
        self.assertEqual(row["session"]["condition"]["category"], "医学")
        self.assertEqual(row["session"]["condition"]["io_type"], "両方")
        self.assertEqual(row["session"]["project_id"], "")
        self.assertNotIn("session_id", row["session"])
        self.assertNotIn("money_policy", row["session"])

    def test_empty_conditions_and_memo_are_not_replaced_with_defaults(self):
        row = record(memo="", **{key: "" for key in SESSION_COLUMNS if key.startswith("cond_")})
        report = legacy.diagnose_legacy_csv(csv_bytes([row]))
        converted = report["rows"][0]
        self.assertEqual(converted["session"]["condition"], {})
        self.assertEqual(converted["session"]["memo"], "")
        self.assertEqual(converted["issues"], [])

    def test_sleep_band_is_not_fabricated_as_sleep_hours(self):
        for band in ("6h", "<4h", ">=8h"):
            with self.subTest(band=band):
                report = legacy.diagnose_legacy_csv(csv_bytes([record(cond_sleep_band=band)]))
                condition = report["rows"][0]["session"]["condition"]
                self.assertEqual(condition["legacy_sleep_band"], band)
                self.assertNotIn("sleep_hours", condition)
                self.assertEqual(report["summary"]["warning_count"], 0)

    def test_invalid_io_is_actionable_warning_with_original_value(self):
        report = legacy.diagnose_legacy_csv(csv_bytes([record(cond_io="cowork")]))
        row = report["rows"][0]
        self.assertIn("invalid_io_type", issue_codes(report))
        self.assertIsNotNone(row["session"])
        self.assertNotIn("io_type", row["session"]["condition"])
        self.assertEqual(row["raw"]["cond_io"], "cowork")

    def test_ambiguous_categories_and_environments_keep_distinct_values(self):
        report = legacy.diagnose_legacy_csv(csv_bytes([record(cond_env="client", cond_topic="consult")]))
        condition = report["rows"][0]["session"]["condition"]
        self.assertEqual(condition["environment"], "訪問先（旧：client）")
        self.assertEqual(condition["category"], "consult")
        self.assertEqual(set(issue_codes(report)), {"ambiguous_environment", "ambiguous_category"})

    def test_nan_infinity_booleans_negative_and_missing_numbers_block_only_affected_rows(self):
        for value in ("NaN", "Infinity", "-1", "True", "", "1e999999", "1e-999999", "1,2", "1_000"):
            with self.subTest(value=value):
                original = record()
                original["duration_sec"] = value
                report = legacy.diagnose_legacy_csv(csv_bytes([original, record(start="2026-01-02T09:00:00")]))
                self.assertIsNone(report["rows"][0]["session"])
                self.assertIsNotNone(report["rows"][1]["session"])
                self.assertIn("invalid_number", issue_codes(report))
                self.assertEqual(report["summary"]["row_count"], 2)

    def test_zero_duration_and_out_of_range_conditions_are_errors(self):
        for updates in ({"duration_sec": "0"}, {"cond_effort_10": "11"}, {"cond_mood_5": "0"}):
            with self.subTest(updates=updates):
                report = legacy.diagnose_legacy_csv(csv_bytes([record(**updates)]))
                self.assertGreater(report["summary"]["error_count"], 0)
                self.assertIsNone(report["rows"][0]["session"])

    def test_datetime_order_date_alias_and_timezone_are_validated(self):
        cases = [({"end_dt": "2026-01-01 08:00:00"}, "end_before_start"),
                 ({"date": "2026-01-02"}, "date_alias_mismatch"),
                 ({"start_dt": "2026-01-01 09:00:00+09:00"}, "timezone_requires_confirmation"),
                 ({"start_dt": "not a timestamp"}, "invalid_datetime"),
                 ({"start_dt": "2026-01-01"}, "invalid_datetime")]
        for updates, code in cases:
            with self.subTest(code=code):
                original = record()
                original.update(updates)
                report = legacy.diagnose_legacy_csv(csv_bytes([original]))
                self.assertIn(code, issue_codes(report))
                self.assertIsNone(report["rows"][0]["session"])

    def test_cross_midnight_is_supported_without_changing_start_day(self):
        report = legacy.diagnose_legacy_csv(csv_bytes([record(start="2026-01-01T23:30:00", duration="3600")]))
        self.assertEqual(report["summary"]["error_count"], 0)
        self.assertEqual(report["daily_reconciliation"][0]["date"], "2026-01-01")
        self.assertEqual(report["rows"][0]["session"]["end_at"], "2026-01-02T00:30:00")

    def test_display_aliases_cannot_silently_override_original_values(self):
        for updates, code in (({"start": "2026-01-01 08:00:00"}, "datetime_alias_mismatch"),
                              ({"勉強時間": "09:00:00"}, "duration_alias_mismatch"),
                              ({"金額換算(円)": "99999"}, "money_alias_mismatch"),
                              ({"勉強内容": "different"}, "memo_alias_mismatch")):
            with self.subTest(code=code):
                original = record()
                original.update(updates)
                report = legacy.diagnose_legacy_csv(csv_bytes([original]))
                self.assertIn(code, issue_codes(report))
                self.assertIsNone(report["rows"][0]["session"])

    def test_no_summary_is_optional_and_does_not_create_a_warning(self):
        report = legacy.diagnose_legacy_csv(csv_bytes([record()]))
        self.assertEqual(report["summary"]["reconciliation_status"], "not_provided")
        self.assertEqual(report["summary"]["warning_count"], 0)
        self.assertIsNone(report["daily_reconciliation"][0]["summary_count"])

    def test_summary_compares_floor_of_daily_sum_not_sum_of_floored_rows(self):
        first = record(duration="3600.75")
        second = record(start="2026-01-01T11:00:00", duration="1800.5")
        money = Decimal(first["money"]) + Decimal(second["money"])
        summary = {"日付": "2026-01-01", "勉強時間(合計)": "01:30:01",
                   "金額換算(合計・円)": str(money.quantize(Decimal("0.1"))),
                   "勉強内容(集約)": "架空", "セッション数": "2"}
        report = legacy.diagnose_legacy_csv(csv_bytes([first, second]), csv_bytes([summary], SUMMARY_COLUMNS))
        self.assertEqual(report["summary"]["error_count"], 0)
        self.assertEqual(report["summary"]["reconciliation_status"], "ok")
        self.assertEqual(report["summary"]["row_count"], 2)
        daily = report["daily_reconciliation"][0]
        self.assertEqual(daily["duration_sec"], 5401.25)
        self.assertEqual(daily["duration_delta_sec"], -0.25)

    def test_real_summary_mismatch_is_an_error(self):
        summary = {"日付": "2026-01-01", "勉強時間(合計)": "01:00:00",
                   "金額換算(合計・円)": "1000.2", "勉強内容(集約)": "架空", "セッション数": "99"}
        report = legacy.diagnose_legacy_csv(csv_bytes([record()]), csv_bytes([summary], SUMMARY_COLUMNS))
        self.assertIn("summary_totals_mismatch", issue_codes(report))
        self.assertEqual(report["daily_reconciliation"][0]["status"], "error")

    def test_duplicate_summary_dates_and_missing_dates_are_errors(self):
        summary = {"日付": "2026-01-02", "勉強時間(合計)": "01:00:00",
                   "金額換算(合計・円)": "1000.2", "勉強内容(集約)": "架空", "セッション数": "1"}
        report = legacy.diagnose_legacy_csv(csv_bytes([record()]), csv_bytes([summary, summary], SUMMARY_COLUMNS))
        self.assertIn("duplicate_summary_date", issue_codes(report))
        self.assertIn("summary_date_mismatch", issue_codes(report))

    def test_summary_malformed_numeric_and_duration_values_block(self):
        summary = {"日付": "2026-01-01", "勉強時間(合計)": "1e999999:00:00",
                   "金額換算(合計・円)": "NaN", "勉強内容(集約)": "架空", "セッション数": "1.5"}
        report = legacy.diagnose_legacy_csv(csv_bytes([record()]), csv_bytes([summary], SUMMARY_COLUMNS))
        self.assertIn("invalid_summary_duration", issue_codes(report))
        self.assertIn("invalid_summary_count", issue_codes(report))
        self.assertEqual(report["summary"]["reconciliation_status"], "error")

    def test_duplicate_fingerprints_ignore_order_source_paths_and_numeric_rendering(self):
        original = record()
        repeated = record(**{"hourly_wage": "1000.0000", "cond_effort_10": "6", "cond_mood_5": "3",
                             "cond_alcohol_yday": "0", "__src": "D:/other/export.json"})
        other = record(start="2026-01-02T09:00:00")
        first = legacy.diagnose_legacy_csv(csv_bytes([original, other]))
        second = legacy.diagnose_legacy_csv(csv_bytes([other, repeated]))
        self.assertEqual(first["rows"][0]["fingerprint"], second["rows"][1]["fingerprint"])
        duplicate = legacy.diagnose_legacy_csv(csv_bytes([original, repeated]))
        self.assertEqual([issue["code"] for issue in duplicate["rows"][0]["issues"]], ["duplicate_row"])
        self.assertEqual(duplicate["overlap_pairs"], [])

    def test_overlaps_are_warnings_and_do_not_delete_or_deduct_time(self):
        first = record(duration="3600")
        second = record(start="2026-01-01T09:30:00", duration="3600", memo="別の架空記録")
        report = legacy.diagnose_legacy_csv(csv_bytes([second, first]))
        self.assertEqual(report["summary"]["row_count"], 2)
        self.assertEqual(report["summary"]["duration_sec"], 7200)
        self.assertEqual(report["overlap_pairs"][0]["seconds"], 1800)
        self.assertTrue(all(any(issue["code"] == "overlapping_time" for issue in row["issues"]) for row in report["rows"]))

    def test_oversized_overlap_diagnostics_are_bounded_and_block_commit(self):
        rows = [record(duration="3600", memo=f"synthetic {index}") for index in range(4)]
        with patch.object(legacy, "MAX_OVERLAP_PAIRS", 2):
            report = legacy.diagnose_legacy_csv(csv_bytes(rows))
        self.assertEqual(len(report["overlap_pairs"]), 2)
        self.assertIn("too_many_overlaps", issue_codes(report))
        self.assertEqual(report["summary"]["row_count"], 4)

    def test_minimal_header_uses_legacy_datetime_fallback(self):
        columns = ["start", "end", "duration_sec", "hourly_wage", "money", "memo"]
        original = record(duration="3600")
        report = legacy.diagnose_legacy_csv(csv_bytes([{key: original[key] for key in columns}], columns))
        self.assertEqual(report["summary"]["error_count"], 0)
        self.assertEqual(report["rows"][0]["session"]["start_at"], "2026-01-01T09:00:00")

    def test_duplicate_missing_headers_bad_encoding_and_bad_quotes_are_reported(self):
        cases = [(b"start,start\n1,2\n", "invalid_header"),
                 (b"start,end,memo\n1,2,3\n", "missing_columns"),
                 (b"\x81", "invalid_encoding"),
                 (b"start,end,duration_sec,hourly_wage,money,memo\n\"unterminated", "invalid_csv"),
                 (b"", "empty_file")]
        for data, code in cases:
            with self.subTest(code=code):
                report = legacy.diagnose_legacy_csv(data)
                self.assertIn(code, issue_codes(report))
                self.assertGreater(report["summary"]["error_count"], 0)

    def test_wrong_width_rows_are_retained_and_not_silently_dropped(self):
        encoded = csv_bytes([record()]).decode("utf-8-sig")
        encoded += "too,few,cells\n"
        report = legacy.diagnose_legacy_csv(encoded.encode("utf-8"))
        self.assertEqual(report["summary"]["row_count"], 2)
        self.assertIsNone(report["rows"][1]["session"])
        self.assertIn("invalid_row_width", issue_codes(report))

    def test_cp932_and_multiline_memos_are_parsed_without_executing_content(self):
        original = record(memo="=架空の式\n次の行")
        report = legacy.diagnose_legacy_csv(csv_bytes([original, record(start="2026-01-02T09:00:00")], encoding="cp932"))
        self.assertEqual(report["source_encoding"], "cp932")
        self.assertEqual(report["rows"][0]["session"]["memo"], original["memo"])
        self.assertGreater(report["rows"][1]["row_number"], 3)

    def test_limits_are_errors_not_silent_truncation(self):
        with patch.object(legacy, "MAX_BYTES", 5):
            report = legacy.diagnose_legacy_csv(csv_bytes([record()]))
        self.assertIn("file_too_large", issue_codes(report))
        with patch.object(legacy, "MAX_ROWS", 1):
            report = legacy.diagnose_legacy_csv(csv_bytes([record(), record(start="2026-01-02T09:00:00")]))
        self.assertIn("too_many_rows", issue_codes(report))


if __name__ == "__main__":
    unittest.main()
