from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import config as app_config
from app_core import json_store
from app_core.json_operations import atomic_json_operation, pending_json_operations


class SimulatedProcessCrash(BaseException):
    """Bypass ordinary exception rollback, as a process interruption would."""


class SaveRecoveryUITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag_recovery_ui_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.first = self.root / "diary_entries/2026/09/20260916.json"
        self.last = self.root / "health_logs/2026/09/20260916.json"
        self.created = self.root / "settings/new_document.json"
        self.operation_id = "e" * 32
        self.journal = self.root / f"backups/json_operations/{self.operation_id}/transaction.json"
        self.first.parent.mkdir(parents=True)
        self.last.parent.mkdir(parents=True)
        # Deliberately retain a BOM, whitespace, and CRLF: recovery must restore
        # bytes, not just an equivalent parsed JSON value.
        self.first.write_bytes(b'\xef\xbb\xbf{ "value": "before", "unknown": [1, 2] }\r\n')
        self.last.write_bytes(b'{\r\n  "value": "before last"\r\n}\r\n')
        self.original_bytes = {path: path.read_bytes() for path in (self.first, self.last)}
        original_read = json_store._read

        def isolated_read(path):
            self.assertTrue(Path(path).resolve().is_relative_to(self.root), f"Out-of-fixture JSON read: {path}")
            return original_read(path)

        for patcher in (
            patch.object(app_config, "PROJECT_ROOT", self.root),
            patch.object(json_store, "PROJECT_ROOT", self.root),
            patch.object(json_store, "_read", side_effect=isolated_read),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def all_file_state(self):
        """Include journals/lock files so display-only actions cannot write."""
        return {
            path.relative_to(self.root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.root.rglob("*") if path.is_file()
        }

    def make_interrupted_operation(self):
        original_save = json_store.save_json_safe
        interrupted = False

        def crash_before_last(path, data, **kwargs):
            nonlocal interrupted
            if Path(path) == self.last and json_store._active_operation.get() is None:
                interrupted = True
                raise SimulatedProcessCrash("synthetic process interruption")
            return original_save(path, data, **kwargs)

        with patch.object(json_store, "save_json_safe", side_effect=crash_before_last):
            with self.assertRaises(SimulatedProcessCrash):
                with atomic_json_operation(root=self.root, kind="recovery_ui_test", operation_id=self.operation_id):
                    json_store.save_json_safe(self.first, {"value": "partially saved"})
                    json_store.save_json_safe(self.created, {"new": True})
                    json_store.save_json_safe(self.last, {"value": "not reached"})
        self.assertTrue(interrupted)
        self.assertNotEqual(self.first.read_bytes(), self.original_bytes[self.first])
        self.assertTrue(self.created.exists())
        self.assertEqual(self.last.read_bytes(), self.original_bytes[self.last])
        self.assertEqual(json.loads(self.journal.read_text(encoding="utf-8"))["status"], "prepared")
        self.assertEqual(len(pending_json_operations(self.root)), 1)

    def app(self):
        from streamlit import config as streamlit_config
        from streamlit.testing.v1 import AppTest

        streamlit_config.get_config_options(options_from_flags={
            "global.developmentMode": False,
            "browser.gatherUsageStats": False,
        })
        # Exercise the production recovery renderer and flash consumer without
        # opening the real application or its settings/data-driven sidebar.
        return AppTest.from_string("""
import streamlit as st
from config import PROJECT_ROOT
from app_core.save_recovery_ui import render_save_recovery
from prolog_ui.components import show_queued_message

st.session_state["recovery_ui_runs"] = st.session_state.get("recovery_ui_runs", 0) + 1
show_queued_message()
st.text_area("未保存の入力", key="unsaved_form_input")
with st.sidebar:
    render_save_recovery(PROJECT_ROOT)
    # The real sidemenu continues with title/navigation controls after recovery.
    # Keep those two delta positions populated across an internal st.rerun;
    # AppTest otherwise retains discarded elements in the old sidebar tree.
    st.caption("Recovery fixture sidebar footer")
    st.divider()
""", default_timeout=30)

    def confirm(self, app):
        return app.checkbox(key=f"save_recovery_confirm_{self.operation_id}")

    def apply(self, app):
        return app.button(key=f"save_recovery_apply_{self.operation_id}")

    def assert_no_exception(self, app):
        self.assertEqual(len(app.exception), 0, [item.value for item in app.exception])

    def test_no_pending_operation_is_silent_and_read_only(self):
        before = self.all_file_state()
        app = self.app().run()
        app.run()
        self.assert_no_exception(app)
        self.assertFalse(app.warning)
        self.assertFalse(app.error)
        self.assertFalse(app.checkbox)
        self.assertFalse(app.button)
        self.assertEqual(self.all_file_state(), before)
        self.assertEqual(pending_json_operations(self.root), [])

    def test_pending_notice_and_confirmation_are_read_only_until_apply(self):
        self.make_interrupted_operation()
        before = self.all_file_state()
        app = self.app().run()
        self.assert_no_exception(app)
        self.assertTrue(app.warning)
        self.assertFalse(self.confirm(app).value)
        self.assertTrue(self.apply(app).disabled)
        self.assertEqual(self.all_file_state(), before)
        self.confirm(app).check().run()
        self.assert_no_exception(app)
        self.assertFalse(self.apply(app).disabled)
        self.assertEqual(self.all_file_state(), before)
        self.confirm(app).uncheck().run()
        self.assertTrue(self.apply(app).disabled)
        self.assertEqual(self.all_file_state(), before)
        self.assertEqual(len(pending_json_operations(self.root)), 1)

    def test_confirmed_recovery_restores_bytes_reruns_flashes_and_keeps_draft(self):
        self.make_interrupted_operation()
        app = self.app().run()
        app.text_area(key="unsaved_form_input").set_value("unsaved draft survives recovery").run()
        self.confirm(app).check().run()
        before_runs = app.session_state["recovery_ui_runs"]
        self.apply(app).click().run()
        self.assert_no_exception(app)
        self.assertEqual(app.session_state["recovery_ui_runs"], before_runs + 2)
        self.assertEqual({path: path.read_bytes() for path in self.original_bytes}, self.original_bytes)
        self.assertFalse(self.created.exists())
        self.assertEqual(pending_json_operations(self.root), [])
        self.assertEqual(json.loads(self.journal.read_text(encoding="utf-8"))["status"], "rolled_back")
        self.assertFalse(app.warning)
        self.assertFalse(app.checkbox)
        self.assertFalse(app.button)
        self.assertEqual(len(app.success), 1)
        self.assertIn("中断した保存を変更前へ戻しました", app.success[0].value)
        self.assertEqual(len(app.get("toast")), 1)
        self.assertIn("中断した保存を変更前へ戻しました", app.get("toast")[0].proto.body)
        self.assertNotIn("_proloag_flash_message", app.session_state)
        self.assertEqual(app.text_area(key="unsaved_form_input").value, "unsaved draft survives recovery")
        after = self.all_file_state()
        app.run()
        self.assert_no_exception(app)
        self.assertFalse(app.success, "The success flash must be displayed only once")
        self.assertFalse(app.get("toast"), "The popup must be displayed only once")
        self.assertEqual(self.all_file_state(), after)
        self.assertEqual(app.text_area(key="unsaved_form_input").value, "unsaved draft survives recovery")

    def test_external_change_after_confirmation_refuses_all_recovery_writes(self):
        self.make_interrupted_operation()
        app = self.app().run()
        app.text_area(key="unsaved_form_input").set_value("keep draft on refusal").run()
        self.confirm(app).check().run()
        # Bypass cooperating writers to simulate an independent raw edit after
        # the user reviewed the journal. Even untouched/new targets must remain.
        self.first.write_bytes(b'{ "value": "later independent edit" }\r\n')
        before = self.all_file_state()
        before_runs = app.session_state["recovery_ui_runs"]
        self.apply(app).click().run()
        self.assert_no_exception(app)
        self.assertEqual(app.session_state["recovery_ui_runs"], before_runs + 1)
        self.assertTrue(app.error)
        self.assertIn("中断後に別の変更があります", app.error[0].value)
        self.assertEqual(len(app.get("toast")), 1)
        self.assertIn("復旧を完了できませんでした", app.get("toast")[0].proto.body)
        self.assertFalse(app.success)
        self.assertTrue(app.warning)
        self.assertEqual(self.all_file_state(), before)
        self.assertEqual(len(pending_json_operations(self.root)), 1)
        self.assertEqual(app.text_area(key="unsaved_form_input").value, "keep draft on refusal")
        self.assertNotIn("_proloag_flash_message", app.session_state)


if __name__ == "__main__":
    unittest.main()
