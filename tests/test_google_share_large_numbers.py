from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from app_core import google_share_snapshot, google_share_ui, json_store
from app_core.google_sheets_client import GoogleShareError, GoogleSheetsClient


class GoogleShareLargeNumbersTests(unittest.TestCase):
    def test_cell_encodes_arbitrarily_large_integers_without_float_conversion(self):
        for value in (2 ** 100, -(2 ** 100), 2 ** 2000, -(2 ** 2000)):
            with self.subTest(value_bits=value.bit_length()):
                self.assertEqual(GoogleSheetsClient._cell(value), {"userEnteredValue": {"stringValue": str(value)}})

    def test_cell_normal_scalars_and_nonfinite_checks_are_unchanged(self):
        for value in (0, 42, 2 ** 53, -12, 1.25):
            self.assertEqual(GoogleSheetsClient._cell(value), {"userEnteredValue": {"numberValue": value}})
        self.assertEqual(GoogleSheetsClient._cell(True), {"userEnteredValue": {"boolValue": True}})
        self.assertEqual(GoogleSheetsClient._cell(None), {})
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.assertRaises(GoogleShareError):
                GoogleSheetsClient._cell(value)

    def test_preview_mixed_large_integer_columns_preserve_exact_text_and_normal_numeric_dtype(self):
        rows = [["large", "normal", "mixed"], [2 ** 2000, 1.25, "text"], [-2 ** 100, 2.5, 2 ** 100], [7, None, None]]
        before = deepcopy(rows)
        frame = google_share_ui._preview_frame(rows)
        self.assertEqual(frame["large"].tolist(), [str(2 ** 2000), str(-2 ** 100), "7"])
        self.assertEqual(frame["mixed"].tolist()[:2], ["text", str(2 ** 100)])
        self.assertTrue(pd.isna(frame.loc[2, "mixed"]))
        self.assertTrue(pd.api.types.is_numeric_dtype(frame["normal"]))
        self.assertEqual(frame.loc[0, "normal"], 1.25)
        self.assertEqual(rows, before)

    def test_pending_snapshot_renders_large_integers_without_mutating_payload(self):
        from streamlit import config
        from streamlit.testing.v1 import AppTest
        config.get_config_options(options_from_flags={"global.developmentMode": False, "browser.gatherUsageStats": False})
        source = '''
import streamlit as st
from app_core.google_share_ui import _show_snapshot
if "fixture_snapshot" not in st.session_state:
    st.session_state.fixture_snapshot = {
        "period": {"start": "2026-09-17", "end": "2026-09-17"},
        "generated_at": "2026-09-17T12:00:00", "snapshot_id": "fixture",
        "counts": {"study_sessions": 1, "study_days": 1},
        "tables": {"daily_summary": [["x", "numeric"], [2 ** 2000, 1.5], [2 ** 100, 2.5]]},
    }
_show_snapshot(st.session_state.fixture_snapshot, pending=True)
'''
        with patch.object(json_store, "_read", side_effect=AssertionError("No personal data")), \
             patch.object(GoogleSheetsClient, "from_saved_connection", side_effect=AssertionError("No network")):
            app = AppTest.from_string(source, default_timeout=30).run()
            self.assertFalse(app.exception, str(app.exception))
            self.assertEqual(app.dataframe[0].value["x"].tolist(), [str(2 ** 2000), str(2 ** 100)])
            self.assertTrue(pd.api.types.is_numeric_dtype(app.dataframe[0].value["numeric"]))
            self.assertEqual(app.session_state.fixture_snapshot["tables"]["daily_summary"][1][0], 2 ** 2000)

    def test_history_large_counts_render_exactly_without_mutating_saved_state(self):
        from streamlit import config
        from streamlit.testing.v1 import AppTest
        config.get_config_options(options_from_flags={"global.developmentMode": False, "browser.gatherUsageStats": False})
        for count in (3, 2 ** 2000):
            with self.subTest(bits=count.bit_length()):
                entry = {
                    "operation_id": "synthetic", "snapshot_id": "a" * 64, "payload_hash": "b" * 64,
                    "published_at": "2026-09-17T12:00:00", "generated_at": "2026-09-17T11:00:00",
                    "period": {"start": "2026-09-17", "end": "2026-09-17"},
                    "counts": {"study_sessions": count, "study_days": 1},
                    "spreadsheet_id": "fixture-book", "url": "https://docs.google.com/spreadsheets/d/fixture-book/edit",
                }
                state = {"schema_version": 1, "history": [entry], "last_success": None, "preferences": {}}
                before = deepcopy(state)
                with patch.object(google_share_ui, "load_share_state", return_value=state), \
                     patch.object(google_share_ui, "load_pending_share", return_value=None), \
                     patch.object(google_share_ui, "connection_status", return_value={"connected": False}), \
                     patch.object(google_share_ui, "_connection_panel"), \
                     patch.object(google_share_ui, "app_today", return_value=date(2026, 9, 17)), \
                     patch.object(json_store, "_read", side_effect=AssertionError("No personal data")), \
                     patch.object(GoogleSheetsClient, "from_saved_connection", side_effect=AssertionError("No network")):
                    app = AppTest.from_string(
                        "from app_core.google_share_ui import render_google_share\nrender_google_share()", default_timeout=30,
                    ).run()
                    self.assertFalse(app.exception, str(app.exception))
                    value = app.dataframe[0].value["学習記録数"]
                    self.assertEqual(value.iloc[0], str(count) if count > 2 ** 53 else count)
                    self.assertEqual(pd.api.types.is_numeric_dtype(value), count <= 2 ** 53)
                    self.assertEqual(state, before)

    def test_normal_builder_tables_keep_previous_preview_values_and_dtypes(self):
        with tempfile.TemporaryDirectory(prefix="proloag-share-large-") as directory:
            root = Path(directory).resolve()
            log_path = root / "study_logs" / "2026" / "09" / "20260917.json"
            log_path.parent.mkdir(parents=True)
            log_path.write_text(json.dumps([{
                "session_id": "synthetic", "start_at": "2026-09-17T09:00:00",
                "end_at": "2026-09-17T10:00:00", "duration_sec": 3600,
                "condition": {"category": "IT", "io_type": "input"},
            }]), encoding="utf-8")
            original = json_store._read

            def guarded(path):
                self.assertTrue(Path(path).resolve().is_relative_to(root), str(path))
                return original(path)

            with patch.object(json_store, "_read", side_effect=guarded):
                snapshot = google_share_snapshot.build_share_snapshot(root, date(2026, 9, 17), date(2026, 9, 17), generated_at=datetime(2026, 9, 17, 12))
            before = deepcopy(snapshot)
            for role, rows in snapshot["tables"].items():
                expected = pd.DataFrame(rows[1:], columns=rows[0])
                actual = google_share_ui._preview_frame(rows)
                if role == "overview":
                    expected, actual = expected.astype(str), actual.astype(str)
                pd.testing.assert_frame_equal(actual, expected)
            self.assertEqual(snapshot, before)


if __name__ == "__main__":
    unittest.main()
