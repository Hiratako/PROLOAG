from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from app_core import ocr_ui as ui
from app_core.ocr_service import OCRStore, atomic_json, transform
from test_ocr import PNG


class OCRUITests(unittest.TestCase):
    def test_history_read_error_shows_retry_without_start_controls(self):
        with patch.object(OCRStore, "history", side_effect=PermissionError("busy")):
            app = self.app()
        self.assertFalse(app.exception)
        self.assertTrue(any(b.label == "履歴を再読込" for b in app.button))

    def test_edit_second_block_does_not_change_selection_on_save(self):
        self.seed()
        folder = self.store.folder(self.job)
        from app_core.ocr_service import read_json

        page = read_json(folder / "page-1.json")
        page["blocks"].append(
            {**page["blocks"][0], "id": "p1b1", "original_text": "second original"}
        )
        atomic_json(folder / "page-1.json", page)
        app = self.app()
        next(s for s in app.selectbox if s.label == "修正する文章").set_value(
            "p1b1"
        ).run()
        next(t for t in app.text_area if t.label == "この文章の内容").set_value(
            "second corrected"
        ).run()
        self.button(app, "修正を保存").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(
            next(s for s in app.selectbox if s.label == "修正する文章").value, "p1b1"
        )

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
        self.temp = tempfile.TemporaryDirectory(prefix="pgocr_ui_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = OCRStore(self.root)
        self.job = "a" * 32
        self.manager = ui.source_manager(str(self.root))
        self.addCleanup(self.manager.close)

    def seed(self, status="done", job=None, blocks=True):
        job = job or self.job
        folder = self.store.folder(job)
        folder.mkdir(parents=True)
        atomic_json(
            folder / "request.json",
            {
                "pages": [1],
                "source_name": "教材.pdf",
                "source_sha256": "b" * 64,
                "strategy": "auto",
                "languages": ["ja", "en"],
                "transforms": {"1": transform()},
            },
        )
        atomic_json(
            folder / "state.json",
            {
                "id": job,
                "name": "教材.pdf",
                "created": time.time(),
                "status": status,
                "completed_pages": [1],
                "progress": 1.0,
                "elapsed": 1.0,
                "error": "",
            },
        )
        atomic_json(
            folder / "page-1.json",
            {
                "number": 1,
                "method": "ocr",
                "width": 100,
                "height": 100,
                "transform": transform(),
                "blocks": [
                    {
                        "id": "p1b0",
                        "original_text": "元の文章",
                        "box": [[0, 0], [1, 0], [1, 1], [0, 1]],
                        "score": 0.7,
                    }
                ]
                if blocks
                else [],
            },
        )
        (folder / "page-1.png").write_bytes(PNG)

    def app(self):
        from streamlit.testing.v1 import AppTest

        return AppTest.from_string(
            f"from pathlib import Path\nfrom app_core.ocr_ui import render_ocr\nrender_ocr(Path({str(self.root)!r}))",
            default_timeout=30,
        ).run()

    def button(self, app, label):
        return next(b for b in app.button if b.label == label)

    def test_opening_and_typing_do_not_probe_render_or_create_files(self):
        with patch.object(
            self.manager, "_tool", side_effect=AssertionError("unexpected tool")
        ):
            app = self.app()
            app.radio[0].set_value("PC内のファイルを直接指定").run()
            app.text_input[0].set_value(str(self.root / "sample.pdf")).run()
        self.assertFalse(app.exception)
        self.assertIsNone(self.manager._base)
        self.assertFalse(self.store.base.exists())

    def test_result_has_editor_preview_and_three_exports(self):
        self.seed()
        app = self.app()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.get("download_button")), 3)
        self.assertTrue(any(t.label == "この文章の内容" for t in app.text_area))
        self.assertTrue(self.button(app, "修正を保存").disabled)

    def test_edit_callback_and_save_keep_raw_text(self):
        self.seed()
        app = self.app()
        editor = next(t for t in app.text_area if t.label == "この文章の内容")
        editor.set_value("修正済みの文章").run()
        self.button(app, "修正を保存").click().run()
        self.assertFalse(app.exception)
        block = self.store.result(self.job)["pages"][0]["blocks"][0]
        self.assertEqual(block["text"], "修正済みの文章")
        self.assertEqual(block["original_text"], "元の文章")

    def test_conflicting_edit_retains_draft(self):
        self.seed()
        app = self.app()
        next(t for t in app.text_area if t.label == "この文章の内容").set_value(
            "my draft"
        ).run()
        self.store.save_edits(self.job, 0, {"p1b0": "external"})
        self.button(app, "修正を保存").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(
            app.session_state["ocr_drafts"][self.job]["texts"]["p1b0"], "my draft"
        )
        self.assertEqual(
            self.store.result(self.job)["pages"][0]["blocks"][0]["text"], "external"
        )

    def test_empty_page_still_exports_its_result(self):
        self.seed(blocks=False)
        app = self.app()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.get("download_button")), 3)
        self.assertTrue(any("文章を取得できません" in i.value for i in app.info))

    def test_active_job_can_cancel_but_cannot_edit_or_delete(self):
        self.seed("reading")
        app = self.app()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.get("download_button")), 0)
        self.assertTrue(self.button(app, "履歴を削除").disabled)
        self.button(app, "読み取りを中止").click().run()
        self.assertTrue((self.store.folder(self.job) / "cancel").exists())

    def test_partial_job_displays_warning_and_completed_page(self):
        self.seed("cancelled")
        app = self.app()
        self.assertFalse(app.exception)
        self.assertTrue(any("途中" in w.value for w in app.warning))
        self.assertEqual(len(app.get("download_button")), 3)

    def test_delete_requires_checkbox(self):
        self.seed()
        app = self.app()
        self.assertTrue(self.button(app, "履歴を削除").disabled)
        app.checkbox[0].check().run()
        self.button(app, "履歴を削除").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(self.store.history(), [])

    def test_same_filename_history_selection_survives_save(self):
        self.seed()
        self.seed(job="b" * 32)
        app = self.app()
        next(s for s in app.selectbox if s.label == "表示する履歴").set_value(
            self.job
        ).run()
        next(t for t in app.text_area if t.label == "この文章の内容").set_value(
            "target"
        ).run()
        self.button(app, "修正を保存").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["ocr_selected"], self.job)


if __name__ == "__main__":
    unittest.main()
