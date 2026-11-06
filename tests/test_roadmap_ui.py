from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import streamlit as st
from app_core import json_store
from app_core.roadmap_service import load_roadmap, save_entry, roadmap_path
import test_roadmap_service as fixtures


class RoadmapUITests(unittest.TestCase):
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
        temp = tempfile.TemporaryDirectory(prefix="pgrui_")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        for p in (
            patch.object(json_store, "PROJECT_ROOT", self.root),
            patch.object(st, "page_link", return_value=None),
        ):
            p.start()
            self.addCleanup(p.stop)

    def app(self):
        from streamlit.testing.v1 import AppTest

        return AppTest.from_string(
            f"from pathlib import Path\nfrom app_core.roadmap_ui import render_roadmap\nrender_roadmap(Path({str(self.root)!r}))",
            default_timeout=60,
        ).run()

    def button(self, app, label):
        return next(w for w in app.button if w.label == label)

    def test_empty_open_and_form_edit_do_not_write_records(self):
        app = self.app()
        self.assertFalse(app.exception)
        self.button(app, "工程を追加").click().run()
        app.text_input[0].set_value("Synthetic").run()
        self.assertEqual(list(self.root.rglob("*.json")), [])

    def test_create_with_confirmation_then_reschedule_keeps_baseline(self):
        app = self.app()
        self.button(app, "工程を追加").click().run()
        app.text_input[0].set_value("Study plan")
        self.button(app, "変更内容を確認").click().run()
        self.assertFalse(app.exception)
        self.assertFalse(roadmap_path(self.root).exists())
        self.button(app, "この内容で保存").click().run()
        self.assertFalse(app.exception)
        item = load_roadmap(self.root)["items"][0]
        # AppTest retains removed dialog widgets after its full rerun. The
        # native-browser QA covers reopening in the same real session.
        app = self.app()
        select = next(w for w in app.selectbox if w.label == "編集する工程")
        select.set_value(item["id"]).run()
        self.assertFalse(app.exception)
        next(w for w in app.date_input if w.label == "終了予定日").set_value(
            date(2026, 12, 1)
        )
        self.button(app, "変更内容を確認").click().run()
        self.button(app, "この内容で保存").click().run()
        self.assertFalse(app.exception)
        changed = load_roadmap(self.root)["items"][0]
        self.assertEqual(changed["baseline_end_date"], item["end_date"])
        self.assertEqual(changed["end_date"], "2026-12-01")

    def test_invalid_progress_preserves_pending_input(self):
        app = self.app()
        self.button(app, "工程を追加").click().run()
        app.text_input[0].set_value("Synthetic")
        next(w for w in app.number_input if w.label == "完了数 / 完了割合").set_value(
            200.0
        )
        self.button(app, "変更内容を確認").click().run()
        self.button(app, "この内容で保存").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.error)
        self.assertFalse(roadmap_path(self.root).exists())
        self.button(app, "確認を取り消す").click().run()
        self.assertEqual(app.text_input[0].value, "Synthetic")

    def test_external_update_blocks_pending_save(self):
        from uuid import uuid4

        app = self.app()
        self.button(app, "工程を追加").click().run()
        app.text_input[0].set_value("Local")
        self.button(app, "変更内容を確認").click().run()
        payload = fixtures.RoadmapTests.payload(self, title="External")
        save_entry(
            self.root,
            "items",
            payload,
            expected_revision=None,
            operation_id=uuid4().hex,
        )
        self.button(app, "この内容で保存").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.error)
        self.assertEqual(
            [row["title"] for row in load_roadmap(self.root)["items"]], ["External"]
        )


if __name__ == "__main__":
    unittest.main()
