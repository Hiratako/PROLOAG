from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from app_core import learning_context_service as context
from app_core import learning_context_ui as context_ui
from app_core.json_store import JsonStoreError
import test_learning_context_service as service_tests


class LearningContextUITests(unittest.TestCase):
    def setUp(self):
        service_tests.LearningContextServiceTests.setUp(self)
        from app_core import settings_service
        loader_path = patch.object(settings_service, "USER_SETTINGS_PATH", self.root / "settings/user_settings.json")
        loader_path.start()
        self.addCleanup(loader_path.stop)
    put = service_tests.LearningContextServiceTests.put
    entry = service_tests.LearningContextServiceTests.entry
    seed = service_tests.LearningContextServiceTests.seed

    def page(self, *, resume=False):
        from streamlit import config as streamlit_config
        from streamlit.testing.v1 import AppTest

        streamlit_config.get_config_options(options_from_flags={"global.developmentMode": False, "browser.gatherUsageStats": False})
        function = "render_resume_cards" if resume else "render_learning_context"
        return AppTest.from_string(
            f"from pathlib import Path\nfrom app_core.learning_context_ui import {function}\n"
            f"{function}(Path({str(self.root)!r}))\n", default_timeout=30)

    @staticmethod
    def button(app, label):
        return next(item for item in app.button if item.label == label)

    def open_material(self, app, identifier="m"):
        app.selectbox[0].select(identifier)
        self.button(app, "この教材を開く").click().run()
        self.assertEqual(len(app.exception), 0)

    def test_ui_readonly_then_save_resets_selection_and_shows_success(self):
        app = self.page().run()
        self.assertEqual(len(app.exception), 0)
        self.assertFalse(self.path.exists())
        self.open_material(app)
        app.text_area[0].set_value("次は第4章")
        app.text_input[0].set_value("解説資料")
        app.text_input[1].set_value("https://docs.google.com/document/d/fixture/edit")
        self.button(app, "引き継ぎを保存").click().run()
        self.assertEqual(len(app.exception), 0, [item.value for item in app.exception])
        self.assertEqual(app.selectbox[0].value, "")
        self.assertTrue(any("保存しました" in item.value for item in app.success))
        self.assertEqual(context.load_learning_context(self.root)["materials"]["m"]["next_step"], "次は第4章")

    def test_ui_stale_conflict_keeps_all_input_until_explicit_discard(self):
        first, second = self.page().run(), self.page().run()
        self.open_material(first)
        self.open_material(second)
        first.text_area[0].set_value("先に保存")
        self.button(first, "引き継ぎを保存").click().run()
        second.text_area[0].set_value("未保存のメモ")
        second.text_input[0].set_value("未保存タイトル")
        second.text_input[1].set_value("https://drive.google.com/file/d/fixture/view")
        self.button(second, "引き継ぎを保存").click().run()
        self.assertEqual(len(second.exception), 0)
        self.assertTrue(any("保持" in item.value for item in second.error))
        self.assertEqual(second.text_area[0].value, "未保存のメモ")
        self.assertEqual(second.text_input[0].value, "未保存タイトル")
        self.assertEqual(context.load_learning_context(self.root)["materials"]["m"]["next_step"], "先に保存")
        self.button(second, "入力を破棄して戻る").click().run()
        self.assertEqual(second.text_area[0].value, "未保存のメモ")
        next(item for item in second.checkbox if item.label == "編集中の入力を破棄して教材選択へ戻る").check()
        self.button(second, "入力を破棄して戻る").click().run()
        self.assertEqual(second.selectbox[0].value, "")

    def test_explicit_reload_preserves_draft_new_remote_links_and_resets_confirmation(self):
        self.seed()
        app = self.page().run()
        self.open_material(app)
        app.text_area[0].set_value("編集中の次の着手位置")
        app.text_input[0].set_value("編集中のリンク名")
        latest = context.load_context_snapshot(self.root)
        existing = latest["data"]["materials"]["m"]["links"]
        context.save_material_context(self.root, "m", existing + [{"title": "別画面で追加", "url": "https://drive.google.com/file/d/added/view"}],
                                      "別画面のメモ", expected_revision=latest["revision"])
        before = self.path.read_bytes()
        self.button(app, "最新の保存内容との差を確認").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(any("別画面のメモ" in item.value for item in app.text))
        old_revision = deepcopy(app.session_state["learning_context_edit"]["snapshot"]["revision"])
        self.button(app, "入力を保持して最新を再読込").click().run()
        self.assertTrue(app.error)
        self.assertEqual(app.session_state["learning_context_edit"]["snapshot"]["revision"], old_revision)
        next(item for item in app.checkbox if item.label == "最新との差を確認し、入力を保持して読み直します").check()
        self.button(app, "入力を保持して最新を再読込").click().run()
        self.assertFalse(app.exception)
        edit = app.session_state["learning_context_edit"]
        self.assertEqual(edit["next_step"], "編集中の次の着手位置")
        self.assertEqual(app.text_area(key=f"learning_context_next_{edit['version']}").value, "編集中の次の着手位置")
        self.assertEqual([row["link"]["title"] for row in edit["rows"]], ["編集中のリンク名", "別画面で追加"])
        self.assertNotEqual(edit["snapshot"]["revision"], old_revision)
        self.assertFalse(any(item.value for item in app.checkbox if item.label in {"選択した資料リンクの削除を確認しました", "『次はここから』を空欄にして消すことを確認しました"}))
        self.assertEqual(self.path.read_bytes(), before)
        # Continue with the retained editor state in a fresh AppTest tree because
        # AppTest keeps removed widget nodes after an explicit rerun/version change.
        fresh = self.page()
        fresh.session_state["learning_context_edit"] = deepcopy(edit)
        fresh.run()
        self.button(fresh, "引き継ぎを保存").click().run()
        self.assertFalse(fresh.exception)
        saved = context.load_learning_context(self.root)["materials"]["m"]
        self.assertEqual(saved["next_step"], "編集中の次の着手位置")
        self.assertEqual(len(saved["links"]), 2)
        self.assertEqual(saved["links"][0]["unknown_link"], [1])

    def test_reload_rejects_changes_after_comparison_and_preserves_all_inputs(self):
        app = self.page().run()
        self.open_material(app)
        app.text_area[0].set_value("読み直し中の下書き")
        app.text_input[0].set_value("下書きリンク")
        app.text_input[1].set_value("https://docs.google.com/document/d/draft/edit")
        self.button(app, "最新の保存内容との差を確認").click().run()
        original_revision = deepcopy(app.session_state["learning_context_edit"]["snapshot"]["revision"])
        latest = context.load_context_snapshot(self.root)
        context.save_material_context(self.root, "inactive", [], "別教材の更新", expected_revision=latest["revision"])
        before = self.path.read_bytes()
        next(item for item in app.checkbox if item.label == "最新との差を確認し、入力を保持して読み直します").check()
        self.button(app, "入力を保持して最新を再読込").click().run()
        self.assertTrue(app.error)
        self.assertEqual(self.path.read_bytes(), before)
        edit = app.session_state["learning_context_edit"]
        self.assertEqual(edit["snapshot"]["revision"], original_revision)
        self.assertEqual(edit["next_step"], "読み直し中の下書き")
        self.assertEqual(edit["rows"][0]["link"]["title"], "下書きリンク")

    def test_reload_deleted_link_requires_explicit_new_link_confirmation(self):
        self.seed()
        app = self.page().run()
        self.open_material(app)
        app.text_input[0].set_value("保持するリンク入力")
        latest = context.load_context_snapshot(self.root)
        context.save_material_context(self.root, "m", [], "最新", expected_revision=latest["revision"])
        before = self.path.read_bytes()
        self.button(app, "最新の保存内容との差を確認").click().run()
        next(item for item in app.checkbox if item.label == "最新との差を確認し、入力を保持して読み直します").check()
        self.button(app, "入力を保持して最新を再読込").click().run()
        edit = deepcopy(app.session_state["learning_context_edit"])
        self.assertTrue(edit["rows"][0]["missing"])
        self.assertEqual(self.path.read_bytes(), before)
        fresh = self.page()
        fresh.session_state["learning_context_edit"] = edit
        fresh.run()
        self.button(fresh, "引き継ぎを保存").click().run()
        self.assertTrue(fresh.error)
        self.assertEqual(self.path.read_bytes(), before)
        next(item for item in fresh.checkbox if item.label == "削除済みリンクを新しいリンクとして登録し直す").check()
        self.button(fresh, "引き継ぎを保存").click().run()
        self.assertFalse(fresh.exception)
        saved = context.load_learning_context(self.root)["materials"]["m"]["links"]
        self.assertEqual(saved[0]["title"], "保持するリンク入力")
        self.assertNotEqual(saved[0]["link_id"], "link_original")

    def test_reload_deleted_material_keeps_inputs_without_enabling_save(self):
        app = self.page().run()
        self.open_material(app)
        app.text_area[0].set_value("削除された教材の下書き")
        self.master["materials"] = []
        self.put(self.master_path, self.master)
        self.button(app, "最新の保存内容との差を確認").click().run()
        next(item for item in app.checkbox if item.label == "最新との差を確認し、入力を保持して読み直します").check()
        self.button(app, "入力を保持して最新を再読込").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["learning_context_edit"]["next_step"], "削除された教材の下書き")
        self.assertTrue(self.button(app, "引き継ぎを保存").disabled)
        self.assertFalse(self.path.exists())

    def test_reload_read_failure_keeps_original_baseline_and_draft(self):
        app = self.page().run()
        self.open_material(app)
        app.text_area[0].set_value("読込失敗でも保持")
        self.button(app, "最新の保存内容との差を確認").click().run()
        revision = deepcopy(app.session_state["learning_context_edit"]["snapshot"]["revision"])
        next(item for item in app.checkbox if item.label == "最新との差を確認し、入力を保持して読み直します").check()
        with patch.object(context_ui, "load_context_snapshot", side_effect=JsonStoreError("synthetic reload failure")):
            self.button(app, "入力を保持して最新を再読込").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.error)
        self.assertEqual(app.session_state["learning_context_edit"]["snapshot"]["revision"], revision)
        self.assertEqual(app.session_state["learning_context_edit"]["next_step"], "読込失敗でも保持")
        self.assertFalse(self.path.exists())

    def test_link_delete_and_blank_note_require_separate_confirmations(self):
        self.seed()
        app = self.page().run()
        self.open_material(app)
        before = self.path.read_bytes()
        app.text_area[0].set_value("")
        next(item for item in app.checkbox if item.label == "このリンクを削除する").check()
        self.button(app, "引き継ぎを保存").click().run()
        self.assertEqual(self.path.read_bytes(), before)
        next(item for item in app.checkbox if item.label == "選択した資料リンクの削除を確認しました").check()
        self.button(app, "引き継ぎを保存").click().run()
        self.assertEqual(self.path.read_bytes(), before)
        next(item for item in app.checkbox if item.label == "『次はここから』を空欄にして消すことを確認しました").check()
        self.button(app, "引き継ぎを保存").click().run()
        self.assertEqual(len(app.exception), 0)
        saved = context.load_learning_context(self.root)["materials"]["m"]
        self.assertEqual((saved["links"], saved["next_step"]), ([], ""))

    def test_add_link_row_and_validation_failure_preserve_draft(self):
        app = self.page().run()
        self.open_material(app)
        app.text_area[0].set_value("入力中")
        app.text_input[0].set_value("リンク1")
        app.text_input[1].set_value("https://evil.test")
        self.button(app, "リンク入力欄を追加").click().run()
        self.assertEqual(len(app.text_input), 4)
        self.assertEqual(app.text_area[0].value, "入力中")
        self.assertEqual(app.text_input[0].value, "リンク1")
        self.button(app, "引き継ぎを保存").click().run()
        self.assertFalse(self.path.exists())
        self.assertEqual(app.text_input[1].value, "https://evil.test")

    def test_deleted_material_retains_editor_input_and_readonly_resume(self):
        self.seed()
        app = self.page().run()
        self.open_material(app)
        app.text_area[0].set_value("削除前から編集中")
        self.button(app, "リンク入力欄を追加").click().run()
        self.master["materials"] = []
        self.put(self.master_path, self.master)
        before = self.path.read_bytes()
        app.run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.text_area[0].value, "削除前から編集中")
        self.assertTrue(self.button(app, "引き継ぎを保存").disabled)
        self.assertTrue(any("削除済み" in item.value for item in app.warning))
        resume = self.page(resume=True).run()
        self.assertEqual(len(resume.exception), 0)
        self.assertTrue(any("次は問3" in item.value for item in resume.text))
        self.assertTrue(any("削除済み" in item.value for item in resume.warning))
        self.assertEqual(self.path.read_bytes(), before)

    def test_inactive_material_is_visible_and_resume_performs_no_writes(self):
        data = self.seed()
        data["materials"]["inactive"] = self.entry("inactive", next_step="無効教材の続き")
        self.put(self.path, data)
        before = self.path.read_bytes(), self.master_path.read_bytes()
        with patch.object(context_ui, "save_material_context", side_effect=AssertionError("read-only cards")):
            app = self.page(resume=True).run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any("無効" in item.value for item in app.caption))
        self.assertEqual((self.path.read_bytes(), self.master_path.read_bytes()), before)

    def test_save_failure_retains_inputs_and_retry_uses_original_baseline(self):
        app = self.page().run()
        self.open_material(app)
        app.text_area[0].set_value("失敗しても残す")
        app.text_input[0].set_value("残すリンク")
        app.text_input[1].set_value("https://docs.google.com/spreadsheets/d/fixture/edit")
        with patch.object(context_ui, "save_material_context", side_effect=JsonStoreError("synthetic save failure")):
            self.button(app, "引き継ぎを保存").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.text_area[0].value, "失敗しても残す")
        self.assertEqual(app.text_input[0].value, "残すリンク")
        self.assertFalse(self.path.exists())
        self.button(app, "引き継ぎを保存").click().run()
        self.assertEqual(context.load_learning_context(self.root)["materials"]["m"]["next_step"], "失敗しても残す")

    def test_corrupt_master_during_edit_keeps_draft_visible_but_blocks_save(self):
        app = self.page().run()
        self.open_material(app)
        app.text_area[0].set_value("マスタが壊れても残す")
        self.button(app, "リンク入力欄を追加").click().run()
        self.master_path.write_text("{broken", encoding="utf-8")
        app.run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.text_area[0].value, "マスタが壊れても残す")
        self.assertTrue(self.button(app, "引き継ぎを保存").disabled)
        self.assertFalse(self.path.exists())

    def test_actual_master_page_includes_new_tab_without_source_write(self):
        from pathlib import Path
        from streamlit import config as streamlit_config
        from streamlit.testing.v1 import AppTest

        streamlit_config.get_config_options(options_from_flags={"global.developmentMode": False, "browser.gatherUsageStats": False})
        source = (Path(__file__).resolve().parents[1] / "pages/10_マスタ管理.py").read_text(encoding="utf-8-sig")
        source = source.replace("ROOT = Path(__file__).resolve().parents[1]", f"ROOT = Path({str(self.root)!r})")
        source = source.replace("import prolog_ui.components as ab", """class _Decoration:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None
ab = _Decoration()""")
        before = self.master_path.read_bytes()
        app = AppTest.from_string(source, default_timeout=30).run()
        self.assertEqual(len(app.exception), 0, [item.value for item in app.exception])
        self.assertIn("資料リンク・引き継ぎ", [tab.label for tab in app.tabs])
        self.assertTrue(any(item.label == "編集する教材" for item in app.selectbox))
        self.assertEqual(self.master_path.read_bytes(), before)
        self.assertFalse(self.path.exists())

    def test_daily_page_real_resume_block_is_opt_in_and_readonly(self):
        from streamlit import config as streamlit_config
        from streamlit.testing.v1 import AppTest
        from app_core import json_store

        streamlit_config.get_config_options(options_from_flags={"global.developmentMode": False, "browser.gatherUsageStats": False})
        self.seed()
        before = {path.relative_to(self.root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in self.root.rglob("*.json")}
        source = (Path(__file__).resolve().parents[1] / "pages/09_今日の学習プラン.py").read_text(encoding="utf-8-sig")
        # Execute the page's actual integration block, not a copied equivalent.
        # The unrelated daily health/task UI stays outside this bounded test.
        blocks = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.If)
                  and isinstance(node.test, ast.Call)
                  and any(keyword.arg == "key" and isinstance(keyword.value, ast.Constant)
                          and keyword.value.value == "daily_show_learning_resume" for keyword in node.test.keywords)]
        self.assertEqual(len(blocks), 1)
        app = AppTest.from_string(
            "import streamlit as st\nfrom pathlib import Path\n"
            f"ROOT = Path({str(self.root)!r})\n" + ast.unparse(blocks[0]), default_timeout=30).run()
        self.assertFalse(app.exception)
        self.assertFalse(app.checkbox(key="daily_show_learning_resume").value)
        self.assertEqual(json_store._read.call_count, 0)
        self.assertFalse(any("次は問3" in item.value for item in app.text))
        with patch.object(context_ui, "save_material_context", side_effect=AssertionError("Resume must not save")):
            app.checkbox(key="daily_show_learning_resume").check().run()
        self.assertFalse(app.exception, [item.value for item in app.exception])
        self.assertTrue(any("次は問3から" in item.value for item in app.text))
        self.assertTrue(any("生命科学" in item.label for item in app.expander))
        self.assertTrue(any("削除済み" in item.value for item in app.warning))
        self.assertEqual({item.proto.url for item in app.get("link_button")}, {"https://docs.google.com/document/d/fixture/edit"})
        self.assertEqual({Path(call.args[0]).relative_to(self.root).as_posix() for call in json_store._read.call_args_list},
                         {"settings/learning_context.json", "master_data/master.json"})
        self.assertEqual(before, {path.relative_to(self.root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
                                  for path in self.root.rglob("*.json")})

    def test_export_page_context_mode_opens_editor_and_saves_only_local_context(self):
        import config as app_config
        from app_core import export_service, export_ui
        from streamlit import config as streamlit_config
        from streamlit.testing.v1 import AppTest

        streamlit_config.get_config_options(options_from_flags={"global.developmentMode": False, "browser.gatherUsageStats": False})
        source = (Path(__file__).resolve().parents[1] / "pages/97_データエクスポート.py").read_text(encoding="utf-8-sig")
        source = source.replace("import prolog_ui.components as ab", """class _Decoration:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None
ab = _Decoration()""")
        before = self.master_path.read_bytes(), self.master_path.stat().st_mtime_ns
        # The page's CSV default and its context branch are both pinned to temp.
        # No Google mode is selected or imported and no vault is inspected.
        # from_string has no page registry; the actual guide target is asserted.
        render_csv = export_ui.render_csv_export
        with patch.object(app_config, "PROJECT_ROOT", self.root), patch.object(export_service, "PROJECT_ROOT", self.root), \
                patch("streamlit.page_link") as page_link, \
                patch.object(export_ui, "render_csv_export", side_effect=lambda: render_csv(self.root)), \
                patch.object(export_ui, "build_export_bundle", wraps=export_service.build_export_bundle) as build_csv:
            app = AppTest.from_string(source, default_timeout=30).run()
            self.assertFalse(app.exception, [item.value for item in app.exception])
            build_csv.assert_called_once_with(self.root)
            app.radio(key="export_output_mode").set_value("資料リンク・引き継ぎ").run()
            self.assertFalse(app.exception, [item.value for item in app.exception])
            self.assertFalse(self.path.exists())
            self.open_material(app)
            app.text_area[0].set_value("97ページからの引き継ぎ")
            app.text_input[0].set_value("97の教材資料")
            app.text_input[1].set_value("https://docs.google.com/presentation/d/fixture/edit")
            self.button(app, "引き継ぎを保存").click().run()
            self.assertFalse(app.exception, [item.value for item in app.exception])
            self.assertEqual(app.selectbox[0].value, "")
            self.assertTrue(any("保存しました" in item.value for item in app.success))
            self.assertTrue(any("97ページからの引き継ぎ" in item.value for item in app.text))
            build_csv.assert_called_once_with(self.root)
            page_link.assert_called_with("pages/96_Google連携ガイド.py", label="Google連携ガイドを開く")
        saved = context.load_learning_context(self.root)["materials"]["m"]
        self.assertEqual(saved["next_step"], "97ページからの引き継ぎ")
        self.assertEqual(saved["links"][0]["url"], "https://docs.google.com/presentation/d/fixture/edit")
        self.assertEqual((self.master_path.read_bytes(), self.master_path.stat().st_mtime_ns), before)
        self.assertEqual({path.relative_to(self.root).as_posix() for path in self.root.rglob("*.json")
                          if "backups" not in path.relative_to(self.root).parts},
                         {"master_data/master.json", "settings/learning_context.json"})
