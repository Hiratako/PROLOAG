from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time as dt_time, timedelta, timezone
import json
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from app_core import app_clock, json_store, substance_residual_service, substance_service


def _legacy_parse_substance_datetime(
    value: Any,
    target: date | datetime | str | None = None,
) -> datetime | None:
    """Frozen pre-optimization parser for exact curve-output comparisons."""
    target_date = substance_service._parse_date(target)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, dt_time):
        parsed = datetime.combine(target_date, value)
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                parsed = datetime.combine(target_date, dt_time.fromisoformat(text))
            except ValueError:
                return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.replace(microsecond=0)


class SubstanceDatetimePerformanceTests(unittest.TestCase):
    def test_datetime_object_does_not_request_a_default_date(self) -> None:
        value = datetime(2026, 8, 28, 13, 14, 15, 987654)
        with patch.object(substance_service, "app_today", side_effect=AssertionError("unexpected date lookup")) as today:
            actual = substance_service.parse_substance_datetime(value)
        self.assertEqual(actual, value.replace(microsecond=0))
        today.assert_not_called()

    def test_complete_iso_datetime_does_not_request_a_default_date(self) -> None:
        for value in ("2026-08-28T13:14:15.987654", "2026-08-28 13:14:15"):
            with self.subTest(value=value):
                with patch.object(substance_service, "app_today", side_effect=AssertionError("unexpected date lookup")) as today:
                    actual = substance_service.parse_substance_datetime(value)
                self.assertEqual(actual, datetime(2026, 8, 28, 13, 14, 15))
                today.assert_not_called()

    def test_iso_date_only_does_not_request_a_default_date(self) -> None:
        with patch.object(substance_service, "app_today", side_effect=AssertionError("unexpected date lookup")) as today:
            actual = substance_service.parse_substance_datetime("2026-08-28")
        self.assertEqual(actual, datetime(2026, 8, 28))
        today.assert_not_called()

    def test_complete_datetime_ignores_an_unused_invalid_target(self) -> None:
        with patch.object(substance_service, "app_today", side_effect=AssertionError("unexpected date lookup")) as today:
            actual = substance_service.parse_substance_datetime("2026-08-28T13:14:15", "invalid target")
        self.assertEqual(actual, datetime(2026, 8, 28, 13, 14, 15))
        today.assert_not_called()

    def test_timezone_conversion_and_microsecond_truncation_are_preserved(self) -> None:
        values = (
            datetime(2026, 8, 28, 0, 1, 2, 999999, tzinfo=timezone.utc),
            datetime(2026, 8, 28, 23, 59, 58, 123456, tzinfo=timezone(timedelta(hours=9))),
            datetime(2026, 8, 28, 8, 7, 6, 100000, tzinfo=timezone(timedelta(hours=-4, minutes=-30))),
        )
        for value in values:
            expected = value.astimezone().replace(tzinfo=None, microsecond=0)
            for encoded in (value, value.isoformat()):
                with self.subTest(value=encoded):
                    with patch.object(substance_service, "app_today", side_effect=AssertionError("unexpected date lookup")):
                        actual = substance_service.parse_substance_datetime(encoded)
                    self.assertEqual(actual, expected)
                    self.assertIsNone(actual.tzinfo)
                    self.assertEqual(actual.microsecond, 0)

    def test_utc_z_iso_timestamp_is_normalized_without_default_date(self) -> None:
        expected = datetime(2026, 8, 28, 12, 0, 1, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
        with patch.object(substance_service, "app_today", side_effect=AssertionError("unexpected date lookup")):
            actual = substance_service.parse_substance_datetime("2026-08-28T12:00:01.999999Z")
        self.assertEqual(actual, expected)

    def test_time_only_uses_an_explicit_target_without_default_date(self) -> None:
        targets = (date(2031, 2, 3), datetime(2031, 2, 3, 23, 59), "2031-02-03", "2031-02-03T23:59:00")
        values = (dt_time(13, 14, 15, 987654), "13:14:15.987654")
        for target in targets:
            for value in values:
                with self.subTest(target=target, value=value):
                    with patch.object(substance_service, "app_today", side_effect=AssertionError("unexpected date lookup")) as today:
                        actual = substance_service.parse_substance_datetime(value, target)
                    self.assertEqual(actual, datetime(2031, 2, 3, 13, 14, 15))
                    today.assert_not_called()

    def test_timezone_aware_time_only_keeps_target_and_timezone_semantics(self) -> None:
        target = date(2031, 2, 3)
        value = dt_time(0, 1, 2, 987654, tzinfo=timezone(timedelta(hours=9)))
        expected = datetime.combine(target, value).astimezone().replace(tzinfo=None, microsecond=0)
        for encoded in (value, value.isoformat()):
            with self.subTest(value=encoded):
                with patch.object(substance_service, "app_today", side_effect=AssertionError("unexpected date lookup")):
                    actual = substance_service.parse_substance_datetime(encoded, target)
                self.assertEqual(actual, expected)

    def test_time_only_uses_the_demo_reference_date_and_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            demo_path = Path(folder) / "demo_mode.json"
            demo_path.write_text(json.dumps({"enabled": True, "fixed_today": "2041-02-03"}), encoding="utf-8")
            before = demo_path.read_bytes()
            with patch.object(app_clock, "DEMO_MODE_PATH", demo_path):
                for value in (dt_time(13, 14, 15, 987654), "13:14:15.987654"):
                    with self.subTest(value=value):
                        with patch.object(app_clock, "load_json_safe", wraps=app_clock.load_json_safe) as reader:
                            actual = substance_service.parse_substance_datetime(value)
                        self.assertEqual(actual, datetime(2041, 2, 3, 13, 14, 15))
                        reader.assert_called_once_with(demo_path, {})
            self.assertEqual(demo_path.read_bytes(), before)
            self.assertEqual(list(Path(folder).iterdir()), [demo_path])

    def test_time_only_with_invalid_target_falls_back_to_app_date_once(self) -> None:
        with patch.object(substance_service, "app_today", return_value=date(2041, 2, 3)) as today:
            actual = substance_service.parse_substance_datetime("13:14:15", "invalid target")
        self.assertEqual(actual, datetime(2041, 2, 3, 13, 14, 15))
        today.assert_called_once_with()

    def test_empty_and_invalid_inputs_do_not_read_settings(self) -> None:
        values = (None, "", "   ", "not-a-date", "2026-99-99T25:99:99", "25:99", False, 0, [], {})
        with patch.object(substance_service, "app_today", side_effect=AssertionError("unexpected date lookup")) as today:
            with patch.object(json_store, "_read", side_effect=AssertionError("unexpected JSON read")) as reader:
                for value in values:
                    with self.subTest(value=value):
                        self.assertIsNone(substance_service.parse_substance_datetime(value))
        today.assert_not_called()
        reader.assert_not_called()

    def test_complete_dates_and_times_do_not_read_json(self) -> None:
        with patch.object(json_store, "_read", side_effect=AssertionError("unexpected JSON read")) as reader:
            self.assertEqual(
                substance_service.parse_substance_datetime(datetime(2026, 8, 28, 12)),
                datetime(2026, 8, 28, 12),
            )
            self.assertEqual(
                substance_service.parse_substance_datetime("2026-08-28T12:00:00"),
                datetime(2026, 8, 28, 12),
            )
            self.assertEqual(substance_service.parse_substance_datetime("2026-08-28"), datetime(2026, 8, 28))
        reader.assert_not_called()

    def test_curve_generation_has_no_settings_or_json_reads(self) -> None:
        reference = datetime(2026, 8, 28, 12)
        profiles = substance_service.default_substance_profiles()
        with patch.object(substance_service, "app_today", side_effect=AssertionError("unexpected date lookup")) as today:
            with patch.object(json_store, "_read", side_effect=AssertionError("unexpected JSON read")) as reader:
                for profile in profiles:
                    records = [
                        {"taken_at": reference - timedelta(hours=2), "amount_mg": profile["default_amount_mg"]},
                        {"taken_at": "2026-08-28T07:05:06.123456", "amount_mg": profile["default_amount_mg"]},
                        {"taken_at": "2026-08-28", "amount_mg": profile["default_amount_mg"]},
                    ]
                    before = deepcopy(records)
                    with self.subTest(profile=profile["substance_id"]):
                        curve = substance_residual_service.build_substance_residual_curve(
                            records, reference - timedelta(hours=24), reference + timedelta(hours=24),
                            profile=profile, interval_minutes=5,
                        )
                        self.assertGreaterEqual(len(curve), 577)
                        self.assertEqual(records, before)
        today.assert_not_called()
        reader.assert_not_called()

    def test_curves_exactly_match_the_legacy_parser_for_multiple_models(self) -> None:
        start = datetime(2026, 8, 28, 12)
        end = start + timedelta(hours=48)
        profiles = substance_service.default_substance_profiles()
        profiles.append({
            "substance_id": "immediate_release", "default_amount_mg": 1.25,
            "half_life_hours": 0.25, "peak_delay_minutes": 0, "residual_cutoff_hours": 1.5,
        })
        for profile in profiles:
            cutoff = float(profile["residual_cutoff_hours"])
            taken_at = start + timedelta(hours=1) - timedelta(hours=cutoff)
            records = [
                {
                    "taken_at": taken_at,
                    "amount_mg": profile["default_amount_mg"],
                    "model_snapshot": substance_service.model_snapshot_for_profile(profile),
                },
                {
                    "taken_at": (start + timedelta(hours=2, minutes=11)).isoformat(),
                    "amount_mg": 0.75,
                    "model_snapshot": {"half_life_hours": 2.5, "peak_delay_minutes": 0, "residual_cutoff_hours": 6.0},
                },
                {"taken_at": start.date().isoformat(), "amount_mg": 0.5},
                {"taken_at": "invalid datetime", "amount_mg": 100.0},
            ]
            before = deepcopy(records)
            with self.subTest(profile=profile["substance_id"]):
                with patch.object(substance_service, "app_today", return_value=start.date()):
                    with patch.object(substance_residual_service, "parse_substance_datetime", _legacy_parse_substance_datetime):
                        legacy = substance_residual_service.build_substance_residual_curve(
                            records, start, end, profile=profile, interval_minutes=5,
                        )
                with patch.object(json_store, "_read", side_effect=AssertionError("unexpected JSON read")):
                    optimized = substance_residual_service.build_substance_residual_curve(
                        records, start, end, profile=profile, interval_minutes=5,
                    )
                self.assertEqual(optimized, legacy)
                self.assertEqual(records, before)
                times = {point for point, amount in optimized}
                self.assertIn(start + timedelta(hours=1) - timedelta(microseconds=1), times)
                self.assertIn(start + timedelta(hours=1), times)

    def test_curve_cutoff_transition_is_preserved_for_saved_models(self) -> None:
        taken_at = datetime(2026, 8, 28, 12)
        for profile in substance_service.default_substance_profiles():
            snapshot = substance_service.model_snapshot_for_profile(profile)
            cutoff_at = taken_at + timedelta(hours=snapshot["residual_cutoff_hours"])
            record = {"taken_at": taken_at, "amount_mg": 1.25, "model_snapshot": snapshot}
            with self.subTest(profile=profile["substance_id"]):
                with patch.object(json_store, "_read", side_effect=AssertionError("unexpected JSON read")):
                    curve = substance_residual_service.build_substance_residual_curve(
                        [record], taken_at - timedelta(minutes=1), cutoff_at + timedelta(minutes=1),
                        profile={"half_life_hours": 0.1, "peak_delay_minutes": 0, "residual_cutoff_hours": 0.2},
                    )
                values = dict(curve)
                self.assertGreater(values[cutoff_at - timedelta(microseconds=1)], 0.0)
                self.assertEqual(values[cutoff_at], 0.0)
                self.assertEqual(curve[-1][1], 0.0)


if __name__ == "__main__":
    unittest.main()
