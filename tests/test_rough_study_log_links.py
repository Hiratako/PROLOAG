from __future__ import annotations

import ast
import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
import unittest

from streamlit.testing.v1 import AppTest

from data_utils import resolve_master_names_from_lookup
from tests.test_reference_read_efficiency import page_functions, UNUSED_ROOT


def detail_view() -> AppTest:
    page = next((Path(__file__).resolve().parents[1] / "pages").glob("02_*.py"))
    module = ast.parse(page.read_bytes())
    detail = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "render_study_detail"
    )
    return AppTest.from_string(
        """
from hashlib import sha256
import pandas as pd
import streamlit as st


def request_rough_study_editor(batch_id, record_id):
    st.session_state.chosen = (batch_id, record_id)


"""
        + ast.unparse(detail)
        + """


source = pd.DataFrame({
    "entry_kind": ["timed", "week", "period"],
    "batch_id": [None, "week-batch", "period-batch"],
    "record_id": [None, "week-entry", "period-entry"],
    "display": ["timed", "rough 1", "rough 2"],
})
if st.checkbox("外す"):
    source = source.iloc[[0, 2]]
if st.checkbox("通常記録のみ"):
    source = source.iloc[[0]]
render_study_detail(source, source[["display"]], show_rough=len(source) > 1)
""",
    ).run()


def select_detail_row(app: AppTest, row: int, *, edit: bool = False) -> None:
    if edit:
        app.button[0].click()
    # AppTest does not carry dataframe selection when it serializes other widgets.
    widgets = app._tree.get_widget_states()
    widgets.widgets.add(
        id=app.dataframe[0].proto.id,
        string_value=json.dumps({"selection": {"rows": [row]}}),
    )
    app._run(widgets)


class RoughStudyLogLinksTests(unittest.TestCase):
    def test_edit_action_targets_selected_saved_rough_record(self):
        app = detail_view()
        self.assertFalse(app.exception)
        self.assertTrue(app.button[0].proto.disabled)

        select_detail_row(app, 0)
        self.assertTrue(app.button[0].proto.disabled)

        select_detail_row(app, 1)
        self.assertFalse(app.button[0].proto.disabled)
        select_detail_row(app, 1, edit=True)
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state.chosen, ("week-batch", "week-entry"))

    def test_filter_change_does_not_reuse_a_selection_for_another_record(self):
        app = detail_view()
        select_detail_row(app, 1)
        previous_table_id = app.dataframe[0].proto.id

        app.checkbox[0].set_value(True).run()
        self.assertNotEqual(app.dataframe[0].proto.id, previous_table_id)
        self.assertTrue(app.button[0].proto.disabled)
        select_detail_row(app, 1)
        self.assertFalse(app.button[0].proto.disabled)
        select_detail_row(app, 1, edit=True)
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state.chosen, ("period-batch", "period-entry"))

        app.checkbox[1].set_value(True).run()
        self.assertFalse(app.exception)
        self.assertFalse(app.button)
        self.assertEqual(len(app.dataframe), 1)


    def test_rough_only_history_resolves_dates_without_start_times(self):
        # pandas 3 may leave .dt.date as datetime64 when every value is NaT.
        # Rough rows must be able to supply their own date on this path.
        functions = page_functions(
            "02", {"load_study_df", "table_datetime", "date_from_source"},
            Path=Path, datetime=datetime, STUDY_DIR=UNUSED_ROOT,
            record_json_files=lambda root: [],
            ReferenceSnapshot=lambda root: SimpleNamespace(
                note_titles=lambda links: "", task_title=lambda link: "",
            ),
            sec_to_hms=str, yen=str,
            resolve_master_names_from_lookup=resolve_master_names_from_lookup,
        )
        result = functions["load_study_df"]({}, [
            {"kind": "week", "study_date": "2026-11-16", "estimated": False,
             "duration_sec": 1800, "record_id": "row-b", "batch_id": "batch-b"},
            {"kind": "period", "study_date": "2026-11-17", "estimated": True,
             "duration_sec": 2700, "record_id": "row-c", "batch_id": "batch-c"},
        ])
        self.assertEqual(result["date"].tolist(), [date(2026, 11, 17), date(2026, 11, 16)])
        self.assertTrue(result["start_at"].isna().all())
