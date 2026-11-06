from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from app_core import export_ui as ui
from app_core import json_store
from app_core.export_service import CsvExport, ExportBundle, build_export_bundle


class ExportUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit import config as streamlit_config

        streamlit_config.get_config_options(options_from_flags={
            "global.developmentMode": False, "browser.gatherUsageStats": False,
        })

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag_csv_ui_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.bundle = ExportBundle(
            generated_at=datetime(2026, 9, 16, 9, 10, 11),
            tables=(
                CsvExport("study_sessions", "勉強ログ", "全項目を保持", "study_sessions.csv", 1, 2,
                          ("session_id", "custom"), b"\xef\xbb\xbfsession_id,custom\ns1,old\n"),
                CsvExport("notes", "ノート", "本文も保持", "notes.csv", 2, 1,
                          ("body",), b"\xef\xbb\xbfbody\na\nb\n"),
            ), manifest=b"fixture-manifest", zip_data=b"fixture-zip", warnings=(),
        )
        builder = patch.object(ui, "build_export_bundle", return_value=self.bundle)
        self.build = builder.start()
        self.addCleanup(builder.stop)
        download = patch.object(ui.st, "download_button", wraps=ui.st.download_button)
        self.download = download.start()
        self.addCleanup(download.stop)

    def app(self):
        from streamlit.testing.v1 import AppTest

        return AppTest.from_string(
            "from pathlib import Path\nimport streamlit as st\n"
            "from app_core.export_ui import render_csv_export\n"
            f"render_csv_export(Path(st.session_state.get('fixture_root', {str(self.root)!r})))\n",
            default_timeout=30,
        ).run()

    @staticmethod
    def all_text(app):
        return "\n".join(str(item.value) for kind in ("caption", "info", "warning", "error") for item in app.get(kind))

    def test_first_render_generates_once_and_selection_or_rerun_does_not_regenerate(self):
        with patch.object(json_store, "_read", side_effect=AssertionError("No record reads during mocked render")) as read:
            app = self.app()
            self.assertFalse(app.exception)
            self.build.assert_called_once_with(self.root)
            app.selectbox(key="export_dataset").set_value("ノート（2行）").run()
            app.run()
            self.assertFalse(app.exception)
            self.assertEqual(self.build.call_count, 1)
            self.assertIs(app.session_state[ui.SNAPSHOT_KEY]["bundle"], self.bundle)
            read.assert_not_called()
        self.assertIn("固定スナップショット", self.all_text(app))
        self.assertIn("自動反映されません", self.all_text(app))
        self.assertIn("2026-09-16 09:10:11", self.all_text(app))

    def test_downloads_use_exact_cached_bytes_without_triggering_a_rerun(self):
        app = self.app()
        downloads = app.get("download_button")
        self.assertEqual(len(downloads), 2)
        self.assertTrue(all(item.proto.ignore_rerun for item in downloads))
        calls = self.download.call_args_list[-2:]
        self.assertIs(calls[0].kwargs["data"], self.bundle.zip_data)
        self.assertEqual(calls[0].kwargs["file_name"], "proloag_all_records_20260916_091011.zip")
        self.assertIs(calls[1].kwargs["data"], self.bundle.tables[0].data)
        app.selectbox(key="export_dataset").set_value("ノート（2行）").run()
        self.assertIs(self.download.call_args.kwargs["data"], self.bundle.tables[1].data)
        self.assertEqual(self.build.call_count, 1)

    def test_explicit_refresh_replaces_the_snapshot_once(self):
        app = self.app()
        newer = replace(self.bundle, generated_at=datetime(2026, 9, 16, 10, 20, 30),
                        zip_data=b"new-zip", warnings=("fixture warning",))
        self.build.return_value = newer
        app.button(key="csv_export_refresh").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(self.build.call_count, 2)
        self.assertIs(app.session_state[ui.SNAPSHOT_KEY]["bundle"], newer)
        self.assertIn("2026-09-16 10:20:30", self.all_text(app))
        self.assertIn("fixture warning", self.all_text(app))
        app.run()
        self.assertEqual(self.build.call_count, 2)

    def test_failed_refresh_keeps_old_output_and_persistent_failure_notice_until_success(self):
        app = self.app()
        self.build.side_effect = OSError("fixture read failure")
        app.button(key="csv_export_refresh").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(self.build.call_count, 2)
        self.assertIs(app.session_state[ui.SNAPSHOT_KEY]["bundle"], self.bundle)
        self.assertIn("生成に失敗", self.all_text(app))
        self.assertIn("前回生成した出力", self.all_text(app))
        self.assertIn("2026-09-16 09:10:11", self.all_text(app))
        app.selectbox(key="export_dataset").set_value("ノート（2行）").run()
        self.assertEqual(self.build.call_count, 2)
        self.assertIn("更新は完了していません", self.all_text(app))
        self.build.side_effect = None
        self.build.return_value = replace(self.bundle, generated_at=datetime(2026, 9, 16, 11, 0))
        app.button(key="csv_export_refresh").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(self.build.call_count, 3)
        self.assertFalse(app.error)
        self.assertNotIn("前回生成した出力", self.all_text(app))

    def test_failed_initial_generation_retries_only_when_requested(self):
        self.build.side_effect = ValueError("fixture failure")
        app = self.app()
        self.assertFalse(app.exception)
        self.assertTrue(app.error)
        self.assertEqual(len(app.get("download_button")), 0)
        app.run()
        self.assertEqual(self.build.call_count, 1)
        self.build.side_effect = None
        app.button(key="csv_export_refresh").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(self.build.call_count, 2)
        self.assertEqual(len(app.get("download_button")), 2)

    def test_cache_is_session_local_and_not_reused_for_another_root(self):
        app = self.app()
        other_session = self.app()
        self.assertFalse(other_session.exception)
        self.assertEqual(self.build.call_count, 2)
        app.session_state["fixture_root"] = str(self.root / "another_project")
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(self.build.call_count, 3)
        self.build.assert_called_with(self.root / "another_project")

    def test_google_and_context_modes_do_not_generate_csv_or_discard_its_snapshot(self):
        from contextlib import ExitStack
        from streamlit.testing.v1 import AppTest

        source = Path(__file__).resolve().parents[1] / "pages/97_データエクスポート.py"
        targets = {
            "Google Sheets共有": "app_core.google_share_ui.render_google_share",
            "Google Docs相談レポート": "app_core.consultation_report_ui.render_consultation_reports",
            "資料リンク・引き継ぎ": "app_core.learning_context_ui.render_learning_context",
            "相談提案の取り込み": "app_core.consultation_proposal_ui.render_proposal_inbox",
        }
        with ExitStack() as stack:
            for name in ("inject_css", "sidemenu", "topbar"):
                stack.enter_context(patch.object(ui.ab, name))
            stack.enter_context(patch("app_core.settings_service.load_user_settings", return_value={}))
            page_link = stack.enter_context(patch.object(ui.st, "page_link"))
            handlers = {mode: stack.enter_context(patch(target)) for mode, target in targets.items()}
            read = stack.enter_context(patch.object(json_store, "_read", side_effect=AssertionError("No live records")))
            network = stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("No network")))
            app = AppTest.from_file(str(source), default_timeout=30)
            app.session_state["export_output_mode"] = "Google Sheets共有"
            app.run()
            self.assertFalse(app.exception)
            self.build.assert_not_called()
            app.radio(key="export_output_mode").set_value("CSV・ZIPダウンロード").run()
            self.assertEqual(self.build.call_count, 1)
            for mode, handler in handlers.items():
                with self.subTest(mode=mode):
                    handler.reset_mock()
                    app.radio(key="export_output_mode").set_value(mode).run()
                    self.assertFalse(app.exception)
                    handler.assert_called_once()
                    self.assertEqual(self.build.call_count, 1)
                    app.radio(key="export_output_mode").set_value("CSV・ZIPダウンロード").run()
                    self.assertFalse(app.exception)
                    self.assertEqual(self.build.call_count, 1)
                    self.assertIs(app.session_state[ui.SNAPSHOT_KEY]["bundle"], self.bundle)
            page_link.assert_called_with("pages/96_Google連携ガイド.py", label="Google連携ガイドを開く")
            read.assert_not_called()
            network.assert_not_called()

    def test_real_bundle_keeps_all_tables_columns_metadata_and_source_bytes(self):
        fixtures = {
            "study_logs/2026/09/20260916.json": {
                "container_custom": {"future": [1, None]},
                "sessions": [{"session_id": "s1", "start_at": "2026-09-16T09:00:00",
                              "duration_sec": 600, "custom": {"deep": "架空データ"}, "future_list": [1, 2]}],
            },
            "daily_plans/2026/09/20260916.json": {"date": "2026-09-16", "tasks": [{"task_id": "t1", "custom": "keep"}]},
            "settings/user_settings.json": {"custom": {"future_option": True}},
        }
        for relative, data in fixtures.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.root.rglob("*.json")}
        fixed_time = datetime(2026, 9, 16, 12, 0)
        expected = build_export_bundle(self.root, generated_at=fixed_time)
        self.build.side_effect = lambda root: build_export_bundle(root, generated_at=fixed_time)
        original_read = json_store._read

        def isolated_read(path):
            self.assertTrue(Path(path).resolve().is_relative_to(self.root))
            return original_read(path)

        with patch.object(json_store, "_read", side_effect=isolated_read) as read:
            app = self.app()
            self.assertFalse(app.exception)
            actual = app.session_state[ui.SNAPSHOT_KEY]["bundle"]
            self.assertEqual(actual.tables, expected.tables)
            self.assertEqual(actual.manifest, expected.manifest)
            self.assertEqual(actual.warnings, expected.warnings)
            with zipfile.ZipFile(io.BytesIO(actual.zip_data)) as actual_zip, zipfile.ZipFile(io.BytesIO(expected.zip_data)) as expected_zip:
                self.assertEqual(actual_zip.namelist(), expected_zip.namelist())
                for name in actual_zip.namelist():
                    self.assertEqual(actual_zip.read(name), expected_zip.read(name))
            initial_reads = read.call_count
            for option in app.selectbox(key="export_dataset").options:
                app.selectbox(key="export_dataset").set_value(option).run()
                self.assertFalse(app.exception)
            self.assertEqual(read.call_count, initial_reads)
            self.assertEqual(self.build.call_count, 1)
        after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.root.rglob("*.json")}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
