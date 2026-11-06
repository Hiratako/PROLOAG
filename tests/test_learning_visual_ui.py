from __future__ import annotations

from copy import deepcopy
from datetime import date
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app_core.analytics_service import LearningDataSnapshot
from app_core.learning_visual_service import build_learning_visual_payload
from app_core.master_snapshot import MasterSnapshot
import prolog_ui.learning_visuals as visuals
from prolog_ui.theme import get_theme_palette


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "pages" / "11_学習蓄積ビジュアライザー.py"


def synthetic_inputs():
    master = MasterSnapshot({
        "projects": [{"project_id": "p", "name": "架空の計画"}],
        "subjects": [{"project_id": "p", "subject_id": "s", "name": "統計学"}],
        "materials": [{"project_id": "p", "subject_id": "s", "material_id": "m", "name": "架空の教材"}],
        "units": [{"project_id": "p", "subject_id": "s", "material_id": "m", "unit_id": "u", "name": '<script>単元</script>'}],
    })
    sessions = [{"session_id": "manual", "start_at": "2026-10-11T08:00:00", "end_at": "2026-10-11T08:10:00", "duration_sec": 33000,
                 "project_id": "p", "subject_id": "s", "material_id": "m", "unit_id": "u", "memo": "手動追加9時間10分", "money": 0}]
    return master, sessions


class LearningVisualMountTests(unittest.TestCase):
    def test_mount_passes_snapshot_as_data_without_writes_or_python_callbacks(self):
        master, sessions = synthetic_inputs()
        payload = build_learning_visual_payload(sessions, master, date(2026, 10, 1), date(2026, 10, 11))
        palette = get_theme_palette("dark")
        before = deepcopy((payload, palette))
        mount = Mock()
        with patch.object(visuals.st.components.v2, "component", return_value=mount) as register:
            visuals.render_learning_visuals(payload, palette=palette, key="fixture")
        self.assertEqual(register.call_args.args, ("proloag_learning_visuals",))
        self.assertEqual(mount.call_args.kwargs, {"key": "fixture", "data": {"payload": payload, "palette": palette}, "width": "stretch"})
        self.assertEqual((payload, palette), before)
        self.assertNotIn("手動追加", register.call_args.kwargs["html"])
        self.assertNotIn("<script>単元</script>", register.call_args.kwargs["js"])

    def test_unavailable_component_keeps_actual_duration_in_fallback(self):
        master, sessions = synthetic_inputs()
        payload = build_learning_visual_payload(sessions, master, "2026-10-01", "2026-10-11")
        with patch.object(visuals.st, "components", SimpleNamespace()), patch.object(visuals.st, "info"), patch.object(visuals.st, "dataframe") as table:
            visuals.render_learning_visuals(payload, palette=get_theme_palette("standard"))
        self.assertEqual(table.call_args.args[0][0]["学習時間（分）"], 550)
        self.assertEqual(table.call_args.kwargs["width"], "stretch")


class LearningVisualPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit import config
        # Match the app launcher when bundled Streamlit is detected as a checkout.
        config.get_config_options(options_from_flags={
            "global.developmentMode": False, "browser.gatherUsageStats": False,
        })

    def run_page(self, master, sessions):
        from streamlit.testing.v1 import AppTest
        import app_core.analytics_service as analytics
        import app_core.app_clock as clock
        import app_core.settings_service as settings
        import prolog_ui.components as ab
        snapshot = LearningDataSnapshot(deepcopy(sessions), deepcopy(sessions), [], [], [], [])
        with patch.object(clock, "app_today", return_value=date(2026, 10, 11)), patch.object(settings, "load_user_settings", return_value={}), \
             patch.object(MasterSnapshot, "load", return_value=master) as master_loader, \
             patch.object(LearningDataSnapshot, "load", return_value=snapshot) as learning_loader, \
             patch.object(analytics, "load_json_safe", side_effect=AssertionError("extra learning JSON read")), \
             patch.object(ab, "sidemenu"):
            app = AppTest.from_file(str(PAGE), default_timeout=25).run()
            self.assertFalse(app.exception, str(app.exception))
            master_loader.assert_called_once()
            learning_loader.assert_called_once()
        return app

    def test_page_mounts_atlas_and_manual_log_matches_existing_kpi(self):
        master, sessions = synthetic_inputs()
        app = self.run_page(master, sessions)
        headings = [heading.value for heading in app.subheader]
        self.assertIn("学習アトラス", headings)
        self.assertNotIn("月間学習の森", headings)
        self.assertNotIn("学習年輪", headings)
        self.assertTrue(any("9時間10分" in item.value for item in app.markdown))
        self.assertFalse(any(box.label == "森表示の基準" for box in app.selectbox))

    def test_empty_page_remains_usable_without_loading_more_records(self):
        app = self.run_page(MasterSnapshot({}), [])
        self.assertIn("学習アトラス", [heading.value for heading in app.subheader])
        self.assertTrue(any("勉強ログ" in item.value for item in app.info))


if __name__ == "__main__":
    unittest.main()
