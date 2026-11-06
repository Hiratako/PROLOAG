from __future__ import annotations

from copy import deepcopy
from datetime import date
import unittest
from unittest.mock import patch

from app_core import integrity_rules
from app_core.navigation import NAV_GROUPS, SETTINGS_PAGE_PATH


class NavigationCountdownIntegrityTests(unittest.TestCase):
    def audit(self, settings):
        before = deepcopy(settings)
        audit = integrity_rules.Audit()
        with patch.object(integrity_rules, "_read_json", side_effect=AssertionError("unexpected file read")):
            integrity_rules._validate_settings(audit, {"settings": settings})
        self.assertEqual(settings, before)
        return audit.issues

    def assert_notice(self, settings, text):
        issues = self.audit(settings)
        matching = [issue for issue in issues if text in issue["内容"]]
        self.assertTrue(matching, issues)
        self.assertTrue(all(issue["重要度"] == integrity_rules.NOTICE for issue in matching))

    def test_optional_fields_preserve_legacy_settings(self):
        for settings in ({}, {"display": {}}, {"timer": {"hourly_rate": 1000}}, {"countdown": {}}):
            with self.subTest(settings=settings):
                self.assertEqual(self.audit(settings), [])

    def test_valid_countdown_values_include_leap_date_and_disabled_target(self):
        for countdown in (
            {"enabled": False, "title": "目標日", "target_date": None},
            {"enabled": False, "title": "", "target_date": "2028-02-29"},
            {"enabled": True, "title": "試験日", "target_date": "2026-12-31"},
        ):
            with self.subTest(countdown=countdown):
                self.assertEqual(self.audit({"countdown": countdown}), [])

    def test_invalid_countdown_shape_reports_notice_without_crashing(self):
        for value in (None, [], True, "invalid", 123):
            with self.subTest(value=value):
                self.assert_notice({"countdown": value}, "countdown が辞書形式")

    def test_countdown_enabled_requires_actual_boolean(self):
        for value in (1, 0, "false", None, [], {}):
            with self.subTest(value=value):
                self.assert_notice({"countdown": {"enabled": value}}, "enabled が真偽値")

    def test_countdown_title_requires_string(self):
        for value in (False, 42, None, [], {}):
            with self.subTest(value=value):
                self.assert_notice({"countdown": {"title": value}}, "title が文字列")

    def test_enabled_countdown_requires_a_target(self):
        for countdown in ({"enabled": True}, {"enabled": True, "target_date": None}):
            with self.subTest(countdown=countdown):
                self.assert_notice({"countdown": countdown}, "target_date が有効な")

    def test_target_requires_exact_valid_iso_date_even_when_disabled(self):
        for value in ("", "2026-02-29", "2026-13-01", "2026-9-11", "20260911", "2026-09-11T00:00:00", " 2026-09-11 ", 20260911, True, [], {}, date(2026, 9, 11)):
            with self.subTest(value=value):
                self.assert_notice({"countdown": {"enabled": False, "target_date": value}}, "target_date が有効な")

    def test_valid_hidden_pages_follow_shared_registry(self):
        hidden = [item["path"] for group in NAV_GROUPS for item in group["items"] if item["path"] != SETTINGS_PAGE_PATH]
        self.assertGreater(len(hidden), 0)
        self.assertEqual(self.audit({"display": {"hidden_sidebar_pages": hidden}}), [])
        self.assertEqual(self.audit({"display": {"hidden_sidebar_pages": []}}), [])

    def test_invalid_hidden_list_or_entries_are_safe_notices(self):
        for value in (None, {}, "pages/02_勉強ログ.py", True):
            with self.subTest(value=value):
                self.assert_notice({"display": {"hidden_sidebar_pages": value}}, "hidden_sidebar_pages がリスト形式")
        for value in (None, 42, True, [], {}):
            with self.subTest(value=value):
                self.assert_notice({"display": {"hidden_sidebar_pages": [value]}}, "指定が文字列")

    def test_unknown_protected_and_duplicate_hidden_pages_are_notices(self):
        for value in ("", "pages/unknown.py", "../main.py", "main.py"):
            with self.subTest(value=value):
                self.assert_notice({"display": {"hidden_sidebar_pages": [value]}}, "登録されていないページ")
        self.assert_notice({"display": {"hidden_sidebar_pages": [SETTINGS_PAGE_PATH]}}, "常に表示される")
        page_path = next(item["path"] for group in NAV_GROUPS for item in group["items"])
        self.assert_notice({"display": {"hidden_sidebar_pages": [page_path, page_path]}}, "重複しています")


if __name__ == "__main__":
    unittest.main()
