from __future__ import annotations

from copy import deepcopy
import ast
from datetime import date
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app_core import consultation_proposal_ui as ui
from app_core import consultation_proposal_service as service
from app_core.consultation_proposal_contract import parse_proposal_payload
from app_core import json_store
from app_core.learning_context_service import load_context_snapshot, save_material_context, load_learning_context


class ConsultationProposalUITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag_proposal_ui_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.master_path = self.root / "master_data/master.json"
        self.context_path = self.root / "settings/learning_context.json"
        self.day = date(2026, 9, 17)
        self.plan_path = self.root / "daily_plans/2026/09/20260917.json"
        self.put(self.master_path, {"projects": [{"project_id": "p", "name": "編入"}],
                                    "subjects": [{"subject_id": "s", "name": "生命科学"}],
                                    "materials": [{"material_id": "m", "name": "教材", "project_id": "p", "subject_id": "s"}]})
        original_read = json_store._read

        def isolated_read(path):
            self.assertTrue(Path(path).resolve().is_relative_to(self.root), f"Read outside fixture: {path}")
            return original_read(path)

        for patcher in (patch.object(json_store, "_read", side_effect=isolated_read), patch.object(json_store, "PROJECT_ROOT", self.root)):
            patcher.start()
            self.addCleanup(patcher.stop)
        from streamlit import config as streamlit_config
        streamlit_config.get_config_options(options_from_flags={"global.developmentMode": False, "browser.gatherUsageStats": False})

    def put(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def app(self, *, daily=False):
        from streamlit.testing.v1 import AppTest
        call = f"render_daily_consultation_memos(Path({str(self.root)!r}), date(2026, 9, 17))" if daily else f"render_proposal_inbox(Path({str(self.root)!r}))"
        return AppTest.from_string(
            "from pathlib import Path\nfrom datetime import date\n"
            "from app_core.consultation_proposal_ui import render_proposal_inbox, render_daily_consultation_memos\n" + call,
            default_timeout=30).run()

    @staticmethod
    def button(app, label):
        return next(item for item in app.button if item.label == label)

    @staticmethod
    def widget(app, kind, label):
        return next(item for item in getattr(app, kind) if item.label == label)

    def assert_clean(self, app):
        self.assertFalse(app.exception, [item.value for item in app.exception])

    def seed(self, *, text="翌日は問3から", hint=None, title="相談提案"):
        raw = {"schema_version": 1, "source_report_id": "a" * 64, "source_label": "手動の相談",
               "proposals": [{"title": title, "text": text, "reason": "配分の見直し",
                              "target": hint or {"kind": "reference"}}]}
        candidates = parse_proposal_payload(json.dumps(raw, ensure_ascii=False))
        service.import_proposals(self.root, candidates, expected_revision=service.load_inbox_snapshot(self.root)["revision"])
        return candidates[0]["proposal_id"]

    def inbox(self):
        return service.load_inbox_snapshot(self.root)["data"]["proposals"]

    def open(self, app, identifier):
        app.selectbox(key="proposal_inbox_selection").select(identifier)
        self.button(app, "この提案を開く").click().run()
        self.assert_clean(app)

    def prepare(self, app, *, kind="next_step", text="採用する修正版"):
        self.widget(app, "text_area", "採用する本文").set_value(text)
        self.widget(app, "selectbox", "反映先の種類").select(kind)
        if kind == "next_step":
            self.widget(app, "selectbox", "反映する教材（必要時に選択）").select("m")
        else:
            self.widget(app, "date_input", "反映する日（必要時に選択）").set_value(self.day)
        self.button(app, "採用前の差分を作成").click().run()
        self.assert_clean(app)

    def approve(self, app):
        self.widget(app, "checkbox", "この採用差分と反映先を確認しました").check().run()
        self.button(app, "承認して採用・反映").click().run()
        self.assert_clean(app)

    def test_initial_render_and_import_preview_write_no_json(self):
        before = {path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*.json") if path.is_file()}
        app = self.app()
        self.assert_clean(app)
        self.assertEqual(app.radio[0].value, "本文を貼り付け")
        self.assertEqual(before, {path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*.json")})
        self.widget(app, "text_area", "GPTの提案本文").set_value("手動で貼った提案")
        self.button(app, "取り込みプレビューを作成").click().run()
        self.assert_clean(app)
        self.assertTrue(any(item.value == "手動で貼った提案" for item in app.text))
        self.assertEqual(self.inbox(), {})
        self.assertFalse(self.context_path.exists())
        self.button(app, "この候補を提案箱へ保存").click().run()
        self.assert_clean(app)
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(next(iter(self.inbox().values()))["status"], "pending")
        self.assertEqual(app.session_state["proposal_inbox_import_version"], 1)
        self.assertEqual(app.text_area(key="proposal_inbox_import_text_1").value, "")
        self.assertFalse(self.context_path.exists())
        self.assertFalse(self.plan_path.exists())

    def test_duplicate_import_does_not_add_a_second_proposal(self):
        for _ in range(2):
            app = self.app()
            self.widget(app, "text_area", "GPTの提案本文").set_value("同じ提案")
            self.button(app, "取り込みプレビューを作成").click().run()
            self.button(app, "この候補を提案箱へ保存").click().run()
            self.assert_clean(app)
        self.assertEqual(len(self.inbox()), 1)
        self.assertTrue(any("重複 1 件" in item.value for item in app.success))

    def test_json_source_reason_hints_are_visible_but_not_auto_selected(self):
        app = self.app()
        app.radio[0].set_value("JSONを貼り付け").run()
        raw = {"schema_version": 1, "source_report_id": "a" * 64, "source_label": "出典は自己申告",
               "proposals": [{"title": "<script>alert(1)</script>", "text": "![remote](https://example.invalid)",
                              "reason": "理由を確認", "target": {"kind": "next_step", "material_id": "deleted-id"}}]}
        self.widget(app, "text_area", "提案JSON").set_value(json.dumps(raw, ensure_ascii=False))
        self.button(app, "取り込みプレビューを作成").click().run()
        self.assert_clean(app)
        for value in (raw["proposals"][0]["title"], raw["proposals"][0]["text"], "理由を確認", "出典は自己申告", "a" * 64):
            self.assertTrue(any(item.value == value for item in app.text))
        self.assertTrue(any("入力された情報" in item.value for item in app.caption))
        self.button(app, "この候補を提案箱へ保存").click().run()
        # AppTest retains removed delta nodes after explicit st.rerun. Verify
        # subsequent entry through a fresh browser session, as in C1/C2 tests.
        app = self.app()
        self.open(app, next(iter(self.inbox())))
        self.assertEqual(self.widget(app, "selectbox", "反映先の種類").value, "reference")
        self.assertEqual(self.widget(app, "selectbox", "反映する教材（必要時に選択）").value, "")
        self.assertIsNone(self.widget(app, "date_input", "反映する日（必要時に選択）").value)

    def test_upload_preview_utf8_validation_and_size_cap(self):
        import streamlit as st

        app = self.app()
        fake = SimpleNamespace(size=200001, getvalue=lambda: (_ for _ in ()).throw(AssertionError("Oversize upload must not be read")))
        with patch.object(st, "file_uploader", return_value=fake):
            app.radio[0].set_value("JSONファイル").run()
            self.button(app, "取り込みプレビューを作成").click().run()
        self.assert_clean(app)
        self.assertTrue(any("200000" in item.value for item in app.error))
        self.assertEqual(self.inbox(), {})
        invalid = SimpleNamespace(size=1, getvalue=lambda: b"\xff")
        with patch.object(st, "file_uploader", return_value=invalid):
            self.button(app, "取り込みプレビューを作成").click().run()
        self.assert_clean(app)
        self.assertTrue(app.error)
        raw = b'{"schema_version":1,"proposals":[{"title":"uploaded","text":"body"}]}'
        valid = SimpleNamespace(size=len(raw), getvalue=lambda: raw)
        with patch.object(st, "file_uploader", return_value=valid):
            self.button(app, "取り込みプレビューを作成").click().run()
            self.assertTrue(any(item.value == "uploaded" for item in app.text))
        self.assertEqual(self.inbox(), {})

    def test_apply_needs_target_and_explicit_approval_then_updates_only_next_step(self):
        identifier = self.seed()
        baseline = load_context_snapshot(self.root)
        save_material_context(self.root, "m", [], "変更前のメモ", expected_revision=baseline["revision"])
        master_before = self.master_path.read_bytes()
        app = self.app()
        self.open(app, identifier)
        self.button(app, "採用前の差分を作成").click().run()
        self.assertTrue(app.error)
        self.prepare(app)
        self.assertTrue(any(item.value == "変更前のメモ" for item in app.text))
        self.assertTrue(any(item.value == "採用する修正版" for item in app.text))
        self.assertTrue(self.button(app, "承認して採用・反映").disabled)
        self.assertEqual(load_learning_context(self.root)["materials"]["m"]["next_step"], "変更前のメモ")
        self.approve(app)
        self.assertEqual(self.inbox()[identifier]["status"], "applied")
        self.assertEqual(load_learning_context(self.root)["materials"]["m"]["next_step"], "採用する修正版")
        self.assertEqual(self.master_path.read_bytes(), master_before)
        self.assertFalse(self.plan_path.exists())
        self.assertEqual(app.selectbox(key="proposal_inbox_selection").value, "")

    def test_new_difference_requires_new_approval(self):
        identifier = self.seed()
        app = self.app()
        self.open(app, identifier)
        self.prepare(app, text="差分1")
        self.widget(app, "checkbox", "この採用差分と反映先を確認しました").check().run()
        self.assertFalse(self.button(app, "承認して採用・反映").disabled)
        self.prepare(app, text="差分2")
        self.assertFalse(self.widget(app, "checkbox", "この採用差分と反映先を確認しました").value)
        self.assertTrue(self.button(app, "承認して採用・反映").disabled)

    def test_daily_memo_apply_and_readonly_display_preserve_tasks_and_unknown_fields(self):
        identifier = self.seed()
        plan = {"date": self.day.isoformat(), "tasks": [{"task_id": "existing-task", "title": "保持"}], "opaque": [1, 2]}
        self.put(self.plan_path, plan)
        app = self.app()
        self.open(app, identifier)
        self.prepare(app, kind="daily_memo", text="明日の方針だけ")
        self.approve(app)
        saved = json.loads(self.plan_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["tasks"], plan["tasks"])
        self.assertEqual(saved["opaque"], [1, 2])
        self.assertEqual(saved["consultation_memos"][0]["text"], "明日の方針だけ")
        before = self.plan_path.read_bytes(), self.plan_path.stat().st_mtime_ns
        daily = self.app(daily=True)
        self.assert_clean(daily)
        self.assertTrue(any(item.value == "明日の方針だけ" for item in daily.text))
        self.assertEqual((self.plan_path.read_bytes(), self.plan_path.stat().st_mtime_ns), before)

    def test_defer_reject_and_return_pending_do_not_apply(self):
        identifier = self.seed()
        app = self.app()
        for label, status in (("保留にする", "deferred"), ("見送る", "rejected"), ("未確認へ戻す", "pending")):
            app = self.app()
            self.open(app, identifier)
            if self.inbox()[identifier]["status"] == "rejected":
                self.assertTrue(self.button(app, "採用前の差分を作成").disabled)
            self.button(app, label).click().run()
            self.assert_clean(app)
            self.assertEqual(self.inbox()[identifier]["status"], status)
        self.assertFalse(self.context_path.exists())
        self.assertFalse(self.plan_path.exists())

    def test_revert_needs_approval_and_stops_if_target_changed_after_preview(self):
        identifier = self.seed()
        baseline = load_context_snapshot(self.root)
        save_material_context(self.root, "m", [], "元のメモ", expected_revision=baseline["revision"])
        app = self.app()
        self.open(app, identifier)
        self.prepare(app)
        self.approve(app)
        app = self.app()
        self.open(app, identifier)
        self.button(app, "適用取消のプレビューを作成").click().run()
        self.assert_clean(app)
        self.assertTrue(self.button(app, "適用を取り消す").disabled)
        current = load_context_snapshot(self.root)
        save_material_context(self.root, "m", [], "後からの編集", expected_revision=current["revision"])
        self.widget(app, "checkbox", "この取消差分を確認しました").check().run()
        self.button(app, "適用を取り消す").click().run()
        self.assert_clean(app)
        self.assertTrue(app.error)
        self.assertEqual(load_learning_context(self.root)["materials"]["m"]["next_step"], "後からの編集")
        self.assertEqual(self.inbox()[identifier]["status"], "applied")

    def test_successful_revert_is_terminal_and_restores_previous_value(self):
        identifier = self.seed()
        baseline = load_context_snapshot(self.root)
        save_material_context(self.root, "m", [], "元のメモ", expected_revision=baseline["revision"])
        app = self.app()
        self.open(app, identifier)
        self.prepare(app)
        self.approve(app)
        app = self.app()
        self.open(app, identifier)
        self.button(app, "適用取消のプレビューを作成").click().run()
        self.widget(app, "checkbox", "この取消差分を確認しました").check().run()
        self.button(app, "適用を取り消す").click().run()
        self.assert_clean(app)
        self.assertEqual(self.inbox()[identifier]["status"], "reverted")
        self.assertEqual(load_learning_context(self.root)["materials"]["m"]["next_step"], "元のメモ")
        app = self.app()
        self.open(app, identifier)
        self.assertFalse(any(item.label == "採用前の差分を作成" for item in app.button))
        self.assertFalse(any(item.label == "未確認へ戻す" for item in app.button))

    def test_two_tabs_stale_apply_keeps_draft_and_does_not_overwrite(self):
        identifier = self.seed()
        first, stale = self.app(), self.app()
        self.open(first, identifier)
        self.open(stale, identifier)
        self.prepare(first, text="先に採用")
        self.prepare(stale, text="古い画面の下書き")
        self.approve(first)
        self.approve(stale)
        self.assertTrue(stale.error)
        self.assertEqual(self.widget(stale, "text_area", "採用する本文").value, "古い画面の下書き")
        self.assertEqual(load_learning_context(self.root)["materials"]["m"]["next_step"], "先に採用")

    def test_reload_after_unrelated_import_keeps_draft_targets_and_requires_new_approval(self):
        identifier = self.seed()
        app = self.app()
        self.open(app, identifier)
        self.widget(app, "text_input", "採用するタイトル").set_value("編集中のタイトル")
        self.widget(app, "date_input", "反映する日（必要時に選択）").set_value(self.day)
        self.prepare(app, text="編集中の本文")
        self.widget(app, "checkbox", "この採用差分と反映先を確認しました").check().run()
        old_hash = app.session_state["proposal_inbox_edit"]["preview"]["preview_hash"]
        old_revision = app.session_state["proposal_inbox_edit"]["revision"]
        self.seed(text="別の提案", title="別窓から取り込み")
        before = (self.root / "settings/consultation_inbox.json").read_bytes()
        self.button(app, "最新の保存内容との差を確認").click().run()
        self.assert_clean(app)
        self.assertNotIn("preview", app.session_state["proposal_inbox_edit"])
        self.assertFalse(any(item.label == "承認して採用・反映" for item in app.button))
        self.button(app, "入力を保持して最新を再読込").click().run()
        self.assertTrue(app.error)
        self.assertEqual(app.session_state["proposal_inbox_edit"]["revision"], old_revision)
        self.widget(app, "checkbox", "最新との差を確認し、入力を保持して読み直します").check()
        # Last-minute form edits must be preserved by the reload submit itself.
        self.widget(app, "text_area", "採用する本文").set_value("確認中に加筆した本文")
        self.button(app, "入力を保持して最新を再読込").click().run()
        self.assert_clean(app)
        edit = deepcopy(app.session_state["proposal_inbox_edit"])
        self.assertEqual((edit["title"], edit["text"], edit["kind"], edit["material_id"], edit["target_date"]),
                         ("編集中のタイトル", "確認中に加筆した本文", "next_step", "m", self.day))
        suffix = f"{identifier}_{edit['version']}"
        self.assertEqual(app.text_area(key=f"proposal_inbox_text_{suffix}").value, "確認中に加筆した本文")
        self.assertEqual(app.selectbox(key=f"proposal_inbox_material_{suffix}").value, "m")
        self.assertEqual(app.date_input(key=f"proposal_inbox_date_{suffix}").value, self.day)
        self.assertNotEqual(edit["revision"], old_revision)
        self.assertEqual((self.root / "settings/consultation_inbox.json").read_bytes(), before)
        self.assertFalse(self.context_path.exists())
        fresh = self.app()
        fresh.session_state["proposal_inbox_edit"] = edit
        fresh.run()
        self.button(fresh, "採用前の差分を作成").click().run()
        self.assert_clean(fresh)
        self.assertNotEqual(fresh.session_state["proposal_inbox_edit"]["preview"]["preview_hash"], old_hash)
        self.assertFalse(self.widget(fresh, "checkbox", "この採用差分と反映先を確認しました").value)
        self.assertTrue(self.button(fresh, "承認して採用・反映").disabled)
        self.approve(fresh)
        self.assertEqual(load_learning_context(self.root)["materials"]["m"]["next_step"], "確認中に加筆した本文")

    def test_reload_comparison_becoming_stale_rejects_without_losing_daily_target(self):
        identifier = self.seed()
        app = self.app()
        self.open(app, identifier)
        self.prepare(app, kind="daily_memo", text="日次に残す下書き")
        self.button(app, "最新の保存内容との差を確認").click().run()
        revision = app.session_state["proposal_inbox_edit"]["revision"]
        self.seed(text="比較後に追加", title="再競合")
        before = (self.root / "settings/consultation_inbox.json").read_bytes()
        self.widget(app, "checkbox", "最新との差を確認し、入力を保持して読み直します").check()
        self.button(app, "入力を保持して最新を再読込").click().run()
        self.assert_clean(app)
        self.assertTrue(app.error)
        edit = app.session_state["proposal_inbox_edit"]
        self.assertEqual(edit["revision"], revision)
        self.assertEqual((edit["text"], edit["kind"], edit["target_date"]), ("日次に残す下書き", "daily_memo", self.day))
        self.assertNotIn("preview", edit)
        self.assertEqual((self.root / "settings/consultation_inbox.json").read_bytes(), before)
        self.assertFalse(self.plan_path.exists())

    def test_reload_keeps_deleted_target_visible_and_refuses_application(self):
        identifier = self.seed()
        app = self.app()
        self.open(app, identifier)
        self.prepare(app, text="教材削除後も保持する下書き")
        master = json.loads(self.master_path.read_text(encoding="utf-8"))
        master["materials"] = []
        self.put(self.master_path, master)
        self.button(app, "最新の保存内容との差を確認").click().run()
        self.widget(app, "checkbox", "最新との差を確認し、入力を保持して読み直します").check()
        self.button(app, "入力を保持して最新を再読込").click().run()
        self.assert_clean(app)
        edit = deepcopy(app.session_state["proposal_inbox_edit"])
        self.assertEqual(edit["material_id"], "m")
        fresh = self.app()
        fresh.session_state["proposal_inbox_edit"] = edit
        fresh.run()
        self.assertTrue(any("削除済み教材" in option for option in self.widget(fresh, "selectbox", "反映する教材（必要時に選択）").options))
        self.button(fresh, "採用前の差分を作成").click().run()
        self.assert_clean(fresh)
        self.assertTrue(fresh.error)
        self.assertNotIn("preview", fresh.session_state["proposal_inbox_edit"])
        self.assertFalse(self.context_path.exists())

    def _assert_terminal_reload_preserves_rescue_draft(self, status):
        identifier = self.seed()
        app = self.app()
        self.open(app, identifier)
        self.prepare(app, text="捨てない下書き")
        if status == "deleted":
            self.put(self.root / "settings/consultation_inbox.json", {"schema_version": 1, "proposals": {}})
        else:
            preview = service.prepare_application(self.root, identifier, kind="next_step", text="別の画面の採用", material_id="m",
                                                  expected_revision=service.load_inbox_snapshot(self.root)["revision"])
            service.approve_application(self.root, preview)
            if status == "reverted":
                preview = service.prepare_revert(self.root, identifier, expected_revision=service.load_inbox_snapshot(self.root)["revision"])
                service.revert_application(self.root, preview)
        before = {path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*.json") if path.is_file()}
        self.button(app, "最新の保存内容との差を確認").click().run()
        self.widget(app, "checkbox", "最新との差を確認し、入力を保持して読み直します").check()
        self.button(app, "入力を保持して最新を再読込").click().run()
        self.assert_clean(app)
        self.assertEqual(self.widget(app, "text_area", "保持している未保存の本文").value, "捨てない下書き")
        self.assertFalse(any(button.label == "採用前の差分を作成" for button in app.button))
        self.assertEqual({path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*.json") if path.is_file()}, before)
        self.assertNotIn("preview", app.session_state["proposal_inbox_edit"])

    def test_reload_applied_proposal_rescues_draft_without_reapplying(self):
        self._assert_terminal_reload_preserves_rescue_draft("applied")

    def test_reload_reverted_proposal_rescues_draft_without_reapplying(self):
        self._assert_terminal_reload_preserves_rescue_draft("reverted")

    def test_reload_deleted_proposal_rescues_draft_without_recreating(self):
        self._assert_terminal_reload_preserves_rescue_draft("deleted")

    def test_apply_failure_retains_text_preview_and_retry_can_succeed(self):
        identifier = self.seed()
        app = self.app()
        self.open(app, identifier)
        self.prepare(app, text="保存失敗でも残る")
        self.widget(app, "checkbox", "この採用差分と反映先を確認しました").check().run()
        with patch.object(ui, "approve_application", side_effect=json_store.JsonStoreError("synthetic failure")):
            self.button(app, "承認して採用・反映").click().run()
        self.assert_clean(app)
        self.assertEqual(self.widget(app, "text_area", "採用する本文").value, "保存失敗でも残る")
        self.assertFalse(self.context_path.exists())
        self.button(app, "承認して採用・反映").click().run()
        self.assert_clean(app)
        self.assertEqual(load_learning_context(self.root)["materials"]["m"]["next_step"], "保存失敗でも残る")

    def test_invalid_import_clears_old_preview_but_preserves_text(self):
        app = self.app()
        self.widget(app, "text_area", "GPTの提案本文").set_value("一度プレビュー")
        self.button(app, "取り込みプレビューを作成").click().run()
        app.radio[0].set_value("JSONを貼り付け").run()
        self.widget(app, "text_area", "提案JSON").set_value("{broken")
        self.button(app, "取り込みプレビューを作成").click().run()
        self.assert_clean(app)
        self.assertTrue(app.error)
        self.assertEqual(self.widget(app, "text_area", "提案JSON").value, "{broken")
        self.assertFalse(any(item.label == "この候補を提案箱へ保存" for item in app.button))
        self.assertEqual(self.inbox(), {})

    def test_stale_import_rejects_and_retains_fixed_preview_and_input(self):
        first, stale = self.app(), self.app()
        for app, text in ((first, "先に格納"), (stale, "古い画面の候補")):
            self.widget(app, "text_area", "GPTの提案本文").set_value(text)
            self.button(app, "取り込みプレビューを作成").click().run()
        self.button(first, "この候補を提案箱へ保存").click().run()
        self.button(stale, "この候補を提案箱へ保存").click().run()
        self.assert_clean(stale)
        self.assertTrue(stale.error)
        self.assertEqual(self.widget(stale, "text_area", "GPTの提案本文").value, "古い画面の候補")
        self.assertTrue(any(item.value == "古い画面の候補" for item in stale.text))
        self.assertEqual(len(self.inbox()), 1)
        self.assertFalse(self.context_path.exists())
        self.assertFalse(self.plan_path.exists())

    def test_daily_reader_rejects_bad_timestamp_without_rendering_markup_or_writing(self):
        plan = {"consultation_memos": [{"proposal_id": "synthetic", "title": "not-rendered", "text": "not-rendered",
                                         "adopted_at": "![remote](https://example.invalid)"}]}
        self.put(self.plan_path, plan)
        before = self.plan_path.read_bytes(), self.plan_path.stat().st_mtime_ns
        app = self.app(daily=True)
        self.assert_clean(app)
        self.assertTrue(app.error)
        self.assertFalse(any("not-rendered" in item.value for item in app.text))
        self.assertEqual((self.plan_path.read_bytes(), self.plan_path.stat().st_mtime_ns), before)

    def test_daily_page_real_blocks_show_adopted_memo_and_opt_in_inbox(self):
        from streamlit.testing.v1 import AppTest

        self.put(self.plan_path, {"date": self.day.isoformat(), "tasks": [], "consultation_memos": [
            {"proposal_id": "proposal_" + "a" * 64, "title": "採用した方針", "text": "今日は演習から",
             "adopted_at": "2026-09-16T12:00:00+09:00"}]})
        source = (Path(__file__).resolve().parents[1] / "pages/09_今日の学習プラン.py").read_text(encoding="utf-8-sig")
        input_fragments = [node for node in ast.parse(source).body
                           if isinstance(node, ast.FunctionDef) and node.name == "render_daily_inputs"]
        self.assertEqual(len(input_fragments), 1)
        blocks = [node for node in input_fragments[0].body if isinstance(node, ast.If)
                  and ("render_daily_consultation_memos" in ast.unparse(node) or "daily_open_proposal_inbox" in ast.unparse(node))]
        self.assertEqual(len(blocks), 2)
        before = self.plan_path.read_bytes()
        app = AppTest.from_string(
            "import streamlit as st\nfrom pathlib import Path\nfrom datetime import date\n"
            f"ROOT=Path({str(self.root)!r})\ntarget_date=date(2026,9,17)\nplan={{'consultation_memos':True}}\n"
            + "\n".join(ast.unparse(node) for node in blocks), default_timeout=30).run()
        self.assert_clean(app)
        self.assertTrue(any(item.value == "今日は演習から" for item in app.text))
        self.assertFalse(app.checkbox(key="daily_open_proposal_inbox").value)
        self.assertFalse(app.radio)
        app.checkbox(key="daily_open_proposal_inbox").check().run()
        self.assert_clean(app)
        self.assertEqual(app.radio[0].value, "本文を貼り付け")
        self.assertTrue(any("schema_version" in item.value for item in app.code))
        self.assertEqual(self.plan_path.read_bytes(), before)
        self.assertEqual(self.inbox(), {})

    def test_export_page_proposal_mode_imports_only_to_local_inbox(self):
        import config as app_config
        from app_core import export_service, export_ui, settings_service
        from streamlit.testing.v1 import AppTest

        source = (Path(__file__).resolve().parents[1] / "pages/97_データエクスポート.py").read_text(encoding="utf-8-sig")
        source = source.replace("import prolog_ui.components as ab", """class _Decoration:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None
ab = _Decoration()""")
        before = self.master_path.read_bytes()
        render_csv = export_ui.render_csv_export
        # from_string has no registered page navigation. Bind the CSV renderer's
        # explicit root too: its default argument may predate this config patch.
        with patch.object(app_config, "PROJECT_ROOT", self.root), patch.object(export_service, "PROJECT_ROOT", self.root), \
                patch.object(settings_service, "USER_SETTINGS_PATH", self.root / "settings/user_settings.json"), \
                patch("streamlit.page_link") as page_link, \
                patch.object(export_ui, "render_csv_export", side_effect=lambda: render_csv(self.root)), \
                patch.object(export_ui, "build_export_bundle", wraps=export_service.build_export_bundle) as build_csv:
            app = AppTest.from_string(source, default_timeout=30).run()
            self.assert_clean(app)
            build_csv.assert_called_once_with(self.root)
            app.radio(key="export_output_mode").set_value("相談提案の取り込み").run()
            self.assert_clean(app)
            self.widget(app, "text_area", "GPTの提案本文").set_value("97ページからの提案")
            self.button(app, "取り込みプレビューを作成").click().run()
            self.assertEqual(self.inbox(), {})
            self.button(app, "この候補を提案箱へ保存").click().run()
            self.assert_clean(app)
            build_csv.assert_called_once_with(self.root)
            page_link.assert_called_with("pages/96_Google連携ガイド.py", label="Google連携ガイドを開く")
        self.assertEqual(len(self.inbox()), 1)
        entry = next(iter(self.inbox().values()))
        self.assertEqual(entry["proposal"]["text"], "97ページからの提案")
        self.assertEqual(entry["status"], "pending")
        self.assertFalse(self.context_path.exists())
        self.assertFalse(self.plan_path.exists())
        self.assertEqual(self.master_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
