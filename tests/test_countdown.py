from __future__ import annotations

import ast
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import Mock, patch

from app_core.countdown_service import get_countdown


PROJECT = Path(__file__).resolve().parents[1]


def countdown_settings(target: Any, title: Any = "試験日", enabled: Any = True) -> dict[str, Any]:
    return {"countdown": {"enabled": enabled, "title": title, "target_date": target}}


class CountdownTests(unittest.TestCase):
    def test_future_uses_calendar_difference_without_inclusive_day(self):
        result = get_countdown(countdown_settings("2026-09-12"), date(2026, 9, 11))
        self.assertEqual(result, {
            "title": "試験日", "target_date": "2026-09-12",
            "days_remaining": 1, "value": "あと 1 日",
        })

    def test_same_day_and_past_have_explicit_labels(self):
        same = get_countdown(countdown_settings("2026-09-11"), date(2026, 9, 11))
        past = get_countdown(countdown_settings("2026-09-09"), date(2026, 9, 11))
        self.assertEqual((same["days_remaining"], same["value"]), (0, "今日が目標日"))
        self.assertEqual((past["days_remaining"], past["value"]), (-2, "目標日から 2 日"))

    def test_leap_day_and_year_boundary(self):
        for today, target, expected in [
            (date(2028, 2, 28), "2028-03-01", 2),
            (date(2028, 2, 29), "2028-03-01", 1),
            (date(2027, 2, 28), "2027-03-01", 1),
            (date(2026, 12, 31), "2027-01-01", 1),
        ]:
            with self.subTest(today=today, target=target):
                self.assertEqual(get_countdown(countdown_settings(target), today)["days_remaining"], expected)

    def test_disabled_unset_and_malformed_sections_are_hidden(self):
        for settings in [
            {}, None, [], {"countdown": None}, {"countdown": []}, {"countdown": "invalid"},
            countdown_settings("2026-09-12", enabled=False),
            countdown_settings("2026-09-12", enabled="false"),
            countdown_settings("2026-09-12", enabled=1),
        ]:
            with self.subTest(settings=settings):
                self.assertIsNone(get_countdown(settings, date(2026, 9, 11)))

    def test_invalid_dates_are_not_silently_truncated_or_normalized(self):
        for target in [
            None, "", "2026-02-29", "2026-13-01", "2026-09-11T00:00:00",
            "20260912", " 2026-09-12", "2026-09-12 ", "2026-W37-6",
            date(2026, 9, 12), 20260912, {}, [],
        ]:
            with self.subTest(target=target):
                self.assertIsNone(get_countdown(countdown_settings(target), date(2026, 9, 11)))

    def test_blank_or_nontext_title_has_safe_fallback(self):
        for title in [None, "", " \t ", {}, 12]:
            with self.subTest(title=title):
                self.assertEqual(get_countdown(countdown_settings("2026-09-12", title), date(2026, 9, 11))["title"], "目標日")

    def test_input_unchanged_and_today_is_supplied_not_system_clock(self):
        settings = countdown_settings("2030-01-01", "  新年  ")
        before = deepcopy(settings)
        result = get_countdown(settings, date(2029, 12, 31))
        self.assertEqual(settings, before)
        self.assertEqual(result["title"], "新年")
        self.assertEqual(result["days_remaining"], 1)
        self.assertIsNone(get_countdown(settings, datetime(2029, 12, 31)))

    def test_page_uses_common_escaped_metric_and_hides_unset(self):
        import prolog_ui.components as ab

        page = PROJECT / "pages" / "09_今日の学習プラン.py"
        tree = ast.parse(page.read_text(encoding="utf-8-sig"), filename=str(page))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "render_countdown")
        namespace = {"Any": Any, "date": date, "get_countdown": get_countdown, "ab": ab}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(page), "exec"), namespace)
        render = namespace["render_countdown"]
        markup = Mock()
        with patch.object(ab.st, "markdown", markup):
            render({}, date(2026, 9, 11))
            markup.assert_not_called()
            render(countdown_settings("2026-09-12", '<script>alert("x")</script>&'), date(2026, 9, 11))
        html = markup.call_args.args[0]
        self.assertIn('class="ab-metric"', html)
        self.assertIn("あと 1 日", html)
        self.assertIn("目標日 2026-09-12", html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;&amp;", html)


if __name__ == "__main__":
    unittest.main()
