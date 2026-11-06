from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core import json_store
from app_core import roadmap_import_ui as ui
from app_core.roadmap_import_prompt import get_example_json
from app_core.roadmap_service import load_roadmap


class RoadmapImportUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit import config

        config.get_config_options(options_from_flags={
            "global.developmentMode": False, "browser.gatherUsageStats": False,
        })

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pgrimpui_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.raw = get_example_json()
        self.filename = "synthetic-plan.json"
        patches = [
            patch.object(json_store, "PROJECT_ROOT", self.root),
            patch.object(ui.st, "file_uploader", side_effect=self.upload),
            patch.object(ui.service, "parse_import_file", wraps=ui.service.parse_import_file),
            patch.object(ui.service, "load_import_context", wraps=ui.service.load_import_context),
            patch.object(ui.service, "build_import_preview", wraps=ui.service.build_import_preview),
            patch.object(ui.service, "apply_import_preview", wraps=ui.service.apply_import_preview),
            patch.object(ui.service, "import_history", wraps=ui.service.import_history),
        ]
        (_, self.upload_mock, self.parse, self.context, self.preview, self.commit, self.history) = [p.start() for p in patches]
        for item in patches:
            self.addCleanup(item.stop)

    def upload(self, *args, **kwargs):
        if self.raw is None:
            return None
        value = io.BytesIO(self.raw)
        value.name = self.filename
        return value

    def app(self):
        from streamlit.testing.v1 import AppTest

        return AppTest.from_string(
            "from pathlib import Path\nimport streamlit as st\n"
            "from app_core.roadmap_import_ui import render_import\n"
            "if st.session_state.get('visible', True):\n"
            f"    render_import(Path(st.session_state.get('fixture_root', {str(self.root)!r})), 'fixture_')\n"
            "else:\n    st.write('Other page')\n",
            default_timeout=60,
        ).run()

    def parsed(self):
        app = self.app()
        app.button(key="fixture_parse").click().run()
        self.assertFalse(app.exception)
        return app

    def previewed(self):
        app = self.parsed()
        app.button(key="fixture_preview").click().run()
        self.assertFalse(app.exception)
        return app

    def record_files(self):
        return [path for path in self.root.rglob("*.json") if "backups" not in path.parts]

    def test_selecting_file_does_not_parse_read_records_or_save(self):
        app = self.app()
        self.assertFalse(app.exception)
        self.parse.assert_not_called()
        self.context.assert_not_called()
        self.preview.assert_not_called()
        self.commit.assert_not_called()
        self.assertEqual(self.record_files(), [])
        self.assertEqual(self.upload_mock.call_args.kwargs["max_upload_size"], 2)

    def test_parse_and_reruns_do_not_repeat_parsing_or_build_preview(self):
        app = self.parsed()
        self.assertEqual(self.parse.call_count, 1)
        self.assertEqual(self.context.call_count, 1)
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(self.parse.call_count, 1)
        self.assertEqual(self.context.call_count, 1)
        self.preview.assert_not_called()
        self.commit.assert_not_called()
        self.assertEqual(self.record_files(), [])
        self.assertTrue(all(item.proto.ignore_rerun for item in app.get("download_button")))

    def test_preview_is_read_only_and_exposes_imported_values(self):
        app = self.previewed()
        self.assertFalse(app.button(key="fixture_commit").disabled)
        self.assertEqual(self.preview.call_count, 1)
        self.commit.assert_not_called()
        self.assertEqual(self.record_files(), [])
        columns = set().union(*(set(item.value.columns) for item in app.dataframe))
        self.assertTrue({"状態", "完了数 / 完了割合", "総量", "メモ", "科目", "確定期限"} <= columns)
        self.assertTrue(app.get("plotly_chart"))

    def test_explicit_commit_adds_all_and_reruns_never_repeat_save(self):
        app = self.previewed()
        app.button(key="fixture_commit").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(self.commit.call_count, 1)
        self.assertTrue(app.button(key="fixture_commit").disabled)
        data = load_roadmap(self.root)
        self.assertEqual((len(data["items"]), len(data["milestones"])), (2, 1))
        self.assertEqual(data["allocations"], [])
        app.run()
        self.assertEqual(self.commit.call_count, 1)
        self.assertTrue(any("反映した計画" in item.value for item in app.caption))
        self.assertEqual(len(self.record_files()), 2)

    def test_changed_file_invalidates_old_preview_without_automatic_parse(self):
        app = self.previewed()
        value = json.loads(self.raw)
        value["plan_title"] = "Changed source"
        self.raw = json.dumps(value).encode()
        app.run()
        self.assertFalse(app.exception)
        state = app.session_state["fixture_state"]
        self.assertIsNone(state["preview"])
        self.assertIsNone(state["document"])
        self.assertEqual(self.parse.call_count, 1)
        self.commit.assert_not_called()

    def test_changed_mapping_disables_old_preview_without_rebuilding(self):
        app = self.previewed()
        next(item for item in app.selectbox if item.label == "この分類の扱い").set_value(ui._PENDING).run()
        self.assertFalse(app.exception)
        self.assertTrue(app.button(key="fixture_commit").disabled)
        self.assertTrue(any("作り直して" in item.value for item in app.warning))
        self.assertEqual(self.preview.call_count, 1)
        self.commit.assert_not_called()

    def test_failed_save_keeps_preview_and_does_not_automatically_retry(self):
        app = self.previewed()
        operation = app.session_state["fixture_state"]["preview"]["operation_id"]
        self.commit.side_effect = ValueError("synthetic external update")
        app.button(key="fixture_commit").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.error)
        self.assertEqual(app.session_state["fixture_state"]["preview"]["operation_id"], operation)
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(self.commit.call_count, 1)
        self.assertIn("synthetic external update", app.session_state["fixture_state"]["error"])
        self.assertEqual(self.record_files(), [])

    def test_malformed_file_shows_repair_download_without_saving(self):
        self.raw = b'{"format":'
        app = self.parsed()
        self.assertTrue(app.error)
        self.assertTrue(any("修正依頼文" in item.label for item in app.get("download_button")))
        self.context.assert_not_called()
        self.commit.assert_not_called()
        self.assertEqual(self.record_files(), [])
        self.assertTrue(all(item.proto.ignore_rerun for item in app.get("download_button")))

    def test_exact_reimport_shows_registered_and_disables_commit(self):
        app = self.previewed()
        app.button(key="fixture_commit").click().run()
        app.button(key="fixture_parse").click().run()
        app.button(key="fixture_preview").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.button(key="fixture_commit").disabled)
        self.assertEqual(app.session_state["fixture_state"]["preview"]["counts"]["existing"], 3)
        self.assertFalse(any("修正依頼文" in item.label for item in app.get("download_button")))
        self.assertEqual(self.commit.call_count, 1)

    def test_navigation_keeps_document_mappings_acknowledgement_and_preview(self):
        app = self.previewed()
        operation = app.session_state["fixture_state"]["preview"]["operation_id"]
        app.session_state["visible"] = False
        app.run()
        self.raw = None
        app.session_state["visible"] = True
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["fixture_state"]["preview"]["operation_id"], operation)
        self.assertFalse(app.button(key="fixture_commit").disabled)
        self.assertEqual(self.parse.call_count, 1)

    def test_other_root_never_reuses_previous_preview(self):
        app = self.previewed()
        app.session_state["fixture_root"] = str(self.root / "another")
        app.run()
        self.assertFalse(app.exception)
        self.assertIsNone(app.session_state["fixture_state"]["document"])
        self.assertIsNone(app.session_state["fixture_state"]["preview"])

    def test_large_file_blocks_parse_and_save(self):
        self.raw = b" " * (ui.MAX_BYTES + 1)
        app = self.app()
        self.assertFalse(app.exception)
        self.assertTrue(app.button(key="fixture_parse").disabled)
        self.parse.assert_not_called()
        self.commit.assert_not_called()

    def test_unresolved_classification_requires_explicit_mapping_or_unassigned(self):
        value = json.loads(self.raw)
        value["items"][0]["classification"]["project"] = "Not registered"
        self.raw = json.dumps(value).encode()
        app = self.previewed()
        self.assertTrue(app.button(key="fixture_commit").disabled)
        scope_state = app.session_state["fixture_state"]
        self.assertTrue(any(item["severity"] == "error" for item in scope_state["preview"]["issues"]))
        next(item for item in app.button if item.label == "未対応の分類をすべて未設定にする").click().run()
        self.assertFalse(app.exception)
        app.button(key="fixture_preview").click().run()
        self.assertFalse(app.exception)
        self.assertFalse(app.button(key="fixture_commit").disabled)
        self.assertEqual(self.record_files(), [])

    def test_history_is_explicitly_loaded_and_read_only(self):
        app = self.app()
        self.history.assert_not_called()
        app.button(key="fixture_history").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(self.history.call_count, 1)
        app.run()
        self.assertEqual(self.history.call_count, 1)
        self.assertEqual(self.record_files(), [])

    def test_milestone_only_one_day_preview_and_commit(self):
        value = json.loads(self.raw)
        value["items"] = []
        self.raw = json.dumps(value).encode()
        app = self.previewed()
        self.assertTrue(app.get("plotly_chart"))
        self.assertFalse(app.button(key="fixture_commit").disabled)
        app.button(key="fixture_commit").click().run()
        self.assertFalse(app.exception)
        data = load_roadmap(self.root)
        self.assertEqual((len(data["items"]), len(data["milestones"])), (0, 1))

    def test_explicit_hierarchy_mapping_filters_children_and_applies_to_preview(self):
        master = {
            "projects": [{"project_id": "P", "name": "Project P"}, {"project_id": "Q", "name": "Project Q"}],
            "subjects": [{"subject_id": "S", "project_id": "P", "name": "Subject P"},
                         {"subject_id": "T", "project_id": "Q", "name": "Subject Q"}],
            "materials": [], "units": [],
        }
        path = self.root / "master_data/master.json"
        path.parent.mkdir()
        path.write_text(json.dumps(master), encoding="utf-8")
        app = self.parsed()
        next(item for item in app.selectbox if item.label == "この分類の扱い").set_value(ui._CHOOSE).run()
        next(item for item in app.selectbox if item.label == "プロジェクト").set_value("P").run()
        subject = next(item for item in app.selectbox if item.label == "科目")
        self.assertIn("Subject P", subject.options)
        self.assertNotIn("Subject Q", subject.options)
        subject.set_value("S").run()
        app.button(key="fixture_preview").click().run()
        self.assertFalse(app.exception)
        preview = app.session_state["fixture_state"]["preview"]
        self.assertTrue(preview["can_commit"])
        self.assertEqual(preview["additions"]["items"][0]["subject_id"], "S")
        self.assertEqual(preview["additions"]["milestones"][0]["project_id"], "P")
        self.assertEqual(len(self.record_files()), 1)

    def test_mapping_expander_default_stays_open_after_partial_resolution(self):
        value = json.loads(self.raw)
        for row in value["items"]:
            row["classification"]["project"] = "Unknown project"
        value["milestones"] = []
        self.raw = json.dumps(value).encode()
        path = self.root / "master_data/master.json"
        path.parent.mkdir()
        path.write_text(json.dumps({
            "projects": [{"project_id": "P", "name": "Project P"}],
            "subjects": [{"subject_id": "S", "project_id": "P", "name": "Subject P"}],
            "materials": [], "units": [],
        }), encoding="utf-8")
        app = self.parsed()

        def mapping_expanded():
            return next(item for item in app.get("expander") if item.label == "分類の対応付け").proto.expanded

        self.assertTrue(mapping_expanded())
        next(item for item in app.selectbox if item.label == "この分類の扱い").set_value(ui._CHOOSE).run()
        self.assertTrue(mapping_expanded())
        next(item for item in app.selectbox if item.label == "プロジェクト").set_value("P").run()
        self.assertTrue(mapping_expanded())
        next(item for item in app.selectbox if item.label == "科目").set_value("S").run()
        self.assertTrue(mapping_expanded())
        next(item for item in app.selectbox if item.label == "この分類の扱い").set_value(ui._UNASSIGNED).run()
        self.assertTrue(mapping_expanded())
        self.assertFalse(app.exception)

    def test_full_roadmap_fragment_keeps_resolved_mapping_preview_current(self):
        from streamlit.testing.v1 import AppTest

        value = json.loads(self.raw)
        for row in value["items"]:
            row["classification"] = {"project": "Unknown project", "subject": "Unknown subject",
                                     "material": "Unknown material", "unit": "Unknown unit"}
        value["milestones"] = []
        self.raw = json.dumps(value).encode()
        master = {
            "projects": [{"project_id": "P", "name": "Project P"}],
            "subjects": [{"subject_id": "S", "project_id": "P", "name": "Subject P"}],
            "materials": [{"material_id": "M", "project_id": "P", "subject_id": "S", "name": "Material P"}],
            "units": [{"unit_id": "U", "project_id": "P", "subject_id": "S", "material_id": "M", "name": "Unit P"}],
        }
        path = self.root / "master_data/master.json"
        path.parent.mkdir()
        path.write_text(json.dumps(master), encoding="utf-8")
        app = AppTest.from_string(
            "from pathlib import Path\nfrom app_core.roadmap_ui import render_roadmap\n"
            f"render_roadmap(Path({str(self.root)!r}))\n", default_timeout=60,
        ).run()
        next(item for item in app.radio if item.label == "管理する内容").set_value("AI計画の取り込み").run()

        def button(label):
            return next(item for item in app.button if item.label == label)

        button("ファイルを確認").click().run()
        button("反映前のプレビューを作成").click().run()
        self.assertTrue(button("この計画を追加する").disabled)
        next(item for item in app.selectbox if item.label == "この分類の扱い").set_value(ui._CHOOSE).run()
        for label, identifier in (("プロジェクト", "P"), ("科目", "S"), ("教材", "M"), ("単元", "U")):
            next(item for item in app.selectbox if item.label == label).set_value(identifier).run()
        button("反映前のプレビューを作成").click().run()
        self.assertFalse(app.exception)
        self.assertFalse(button("この計画を追加する").disabled)
        self.assertFalse(any("作り直して" in item.value for item in app.warning))
        self.assertEqual(len(self.record_files()), 1)


if __name__ == "__main__":
    unittest.main()
