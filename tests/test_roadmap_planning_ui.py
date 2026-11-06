import unittest
from unittest.mock import patch

import streamlit as st
from app_core import roadmap_service as service
from app_core import roadmap_planning as planner
import test_roadmap_service as fixtures


class PlanningUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit import config

        config.get_config_options(
            options_from_flags={
                "global.developmentMode": False,
                "browser.gatherUsageStats": False,
            }
        )

    def setUp(self):
        self.fixture = fixtures.RoadmapTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        p = patch.object(st, "page_link", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def seed(self):
        self.fixture.save(
            self.fixture.payload(
                title="A", start_date="2026-11-01", end_date="2026-11-02"
            )
        )
        a = service.load_roadmap(self.root)["items"][0]
        self.fixture.save(
            self.fixture.payload(
                title="B",
                start_date="2026-11-03",
                end_date="2026-11-04",
                predecessor_ids=[a["id"]],
            )
        )
        return a

    def app(self, *, pending=False):
        from streamlit.testing.v1 import AppTest

        # Prime confirmation screens separately because AppTest retains removed
        # form widgets when a fragment calls a full rerun. Native-browser QA
        # exercises the entire preview/cancel/save sequence with real widgets.
        setup = ""
        if pending:
            setup = """
from hashlib import sha256
from app_core.roadmap_service import load_roadmap
from app_core.roadmap_planning import shift_changes
from app_core.roadmap_planning_ui import start_schedule_preview
import streamlit as st
scope = 'roadmap_' + sha256(str(root.resolve()).encode()).hexdigest()[:10] + '_'
if 'qa_primed' not in st.session_state:
    data = load_roadmap(root)
    selected = [data['items'][0]['id']]
    st.session_state[scope + 'mode_memory'] = '日程の調整'
    st.session_state[scope + 'shift_draft'] = {'selected': selected, 'days': 2, 'include': True}
    start_schedule_preview(root, data, scope, shift_changes(data, selected, 2, include_descendants=True))
    st.session_state['qa_primed'] = True
"""
        return AppTest.from_string(
            f"from pathlib import Path\nfrom app_core.roadmap_ui import render_roadmap\nroot=Path({str(self.root)!r})\n{setup}\nrender_roadmap(root)",
            default_timeout=60,
        ).run()

    def widget(self, app, kind, label):
        return next(widget for widget in getattr(app, kind) if widget.label == label)

    def test_schedule_preview_cancel_restores_draft_and_does_not_save(self):
        a = self.seed()
        before = self.fixture.snapshot()
        app = self.app()
        self.widget(app, "radio", "管理する内容").set_value("日程の調整").run()
        self.widget(app, "multiselect", "移動する工程").set_value([a["id"]])
        self.widget(app, "number_input", "移動する日数（負の値で前倒し）").set_value(2)
        self.widget(app, "button", "一括移動のプレビュー").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(before, self.fixture.snapshot())
        self.assertTrue(any(widget.label == "日程変更を保存" for widget in app.button))
        app = self.app(pending=True)
        self.widget(app, "button", "日程変更を取り消す").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(
            self.widget(app, "number_input", "移動する日数（負の値で前倒し）").value, 2
        )
        self.assertEqual(
            self.widget(app, "multiselect", "移動する工程").value, [a["id"]]
        )

    def test_schedule_save_updates_successors_and_keeps_initial_dates(self):
        self.seed()
        app = self.app(pending=True)
        self.widget(app, "button", "日程変更を保存").click().run()
        self.assertFalse(app.exception)
        data = service.load_roadmap(self.root)
        self.assertEqual(data["items"][0]["start_date"], "2026-11-03")
        self.assertEqual(data["items"][1]["start_date"], "2026-11-05")
        self.assertEqual(data["items"][0]["baseline_start_date"], "2026-11-01")

    def test_stale_schedule_keeps_preview_and_external_changes(self):
        a = self.seed()
        app = self.app(pending=True)
        self.fixture.save(
            self.fixture.payload(
                title="external", start_date="2026-11-01", end_date="2026-11-02"
            ),
            a["id"],
        )
        before = self.fixture.snapshot()
        self.widget(app, "button", "日程変更を保存").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.error)
        self.assertEqual(before, self.fixture.snapshot())
        self.assertTrue(
            any(widget.label == "日程変更を取り消す" for widget in app.button)
        )

    def test_zero_calendar_save_is_explicit_and_opening_is_read_only(self):
        app = self.app()
        self.widget(app, "radio", "管理する内容").set_value("学習可能時間・配分").run()
        self.assertFalse(app.exception)
        self.assertFalse(service.roadmap_path(self.root).exists())
        self.widget(app, "button", "学習可能時間を保存").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(
            planner.calendar_settings(service.load_roadmap(self.root))[
                "weekly_minutes"
            ],
            [0] * 7,
        )

    def test_dependency_selection_is_available_in_stage_editor(self):
        a = self.seed()
        app = self.app()
        self.widget(app, "selectbox", "編集する工程").set_value(a["id"]).run()
        self.assertFalse(app.exception)
        field = self.widget(app, "multiselect", "前提となる工程（終了後に開始）")
        self.assertEqual(len(field.options), 1)


if __name__ == "__main__":
    unittest.main()
