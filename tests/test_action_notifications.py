from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from prolog_ui import components


class FakeST:
    def __init__(self):
        self.session_state = {}
        self.inline = []
        self.toasts = []
        self.toast_failures = []
        self.markdown_calls = []

    def success(self, message):
        self.inline.append(("success", message))

    def info(self, message):
        self.inline.append(("info", message))

    def warning(self, message):
        self.inline.append(("warning", message))

    def error(self, message):
        self.inline.append(("error", message))

    def toast(self, message, **kwargs):
        self.toasts.append((message, kwargs))
        if self.toast_failures:
            failure = self.toast_failures.pop(0)
            if failure is not None:
                raise failure

    def markdown(self, message, **kwargs):
        self.markdown_calls.append((message, kwargs))


class ActionNotificationTests(unittest.TestCase):
    def setUp(self):
        self.ui = FakeST()
        patcher = patch.object(components, "st", self.ui)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_toast(self, index, message, icon):
        self.assertEqual(self.ui.toasts[index], (message, {"icon": icon, "duration": "long"}))

    def test_success_queue_is_consumed_once_and_retains_inline_result(self):
        components.queue_success("日記を保存しました")
        components.show_queued_message()
        self.assertEqual(self.ui.inline, [("success", "日記を保存しました")])
        self.assert_toast(0, "日記を保存しました", "✅")
        self.assertNotIn(components.FLASH_MESSAGE_KEY, self.ui.session_state)
        components.show_queued_message()
        self.assertEqual(len(self.ui.inline), 1)
        self.assertEqual(len(self.ui.toasts), 1)

    def test_multiple_actions_before_rerun_are_not_overwritten(self):
        components.queue_success("ノートを追加しました")
        components.queue_message("変更内容を確認してください", kind="warning")
        components.queue_success("カードを追加しました")
        components.show_queued_message()
        self.assertEqual(self.ui.inline, [
            ("success", "ノートを追加しました"),
            ("warning", "変更内容を確認してください"),
            ("success", "カードを追加しました"),
        ])
        self.assertEqual([message for message, _ in self.ui.toasts],
                         [message for _, message in self.ui.inline])
        self.assert_toast(1, "変更内容を確認してください", "⚠️")

    def test_legacy_direct_dict_remains_compatible_with_new_queued_action(self):
        self.ui.session_state[components.FLASH_MESSAGE_KEY] = {
            "message": "以前の経路で保存しました", "kind": "info",
        }
        components.queue_success("次の操作を保存しました")
        components.show_queued_message()
        self.assertEqual(self.ui.inline, [
            ("info", "以前の経路で保存しました"), ("success", "次の操作を保存しました"),
        ])
        self.assert_toast(0, "以前の経路で保存しました", "ℹ️")

    def test_legacy_single_dict_defaults_to_success_and_is_consumed(self):
        self.ui.session_state[components.FLASH_MESSAGE_KEY] = {"message": "ログを修正しました"}
        components.show_queued_message()
        self.assertEqual(self.ui.inline, [("success", "ログを修正しました")])
        self.assert_toast(0, "ログを修正しました", "✅")
        self.assertNotIn(components.FLASH_MESSAGE_KEY, self.ui.session_state)

    def test_same_message_can_notify_for_each_distinct_action(self):
        for _ in range(2):
            components.queue_success("保存しました")
            components.show_queued_message()
            components.show_queued_message()
        self.assertEqual(len(self.ui.toasts), 2)
        self.assertEqual(self.ui.inline, [("success", "保存しました")] * 2)
        components.queue_success("保存しました")
        components.queue_success("保存しました")
        components.show_queued_message()
        self.assertEqual(len(self.ui.toasts), 4)

    def test_toast_only_queue_does_not_duplicate_retained_inline_result(self):
        components.queue_toast("教材を引き継ぎました", kind="info")
        components.show_queued_toasts()
        components.show_queued_toasts()
        self.assertEqual(self.ui.inline, [])
        self.assert_toast(0, "教材を引き継ぎました", "ℹ️")
        self.assertEqual(len(self.ui.toasts), 1)
        self.assertNotIn(components.TOAST_MESSAGE_KEY, self.ui.session_state)

    def test_immediate_toast_only_result_leaves_existing_inline_status_untouched(self):
        components.show_action_toast("出題候補を確認できませんでした", kind="error")
        self.assertEqual(self.ui.inline, [])
        self.assertEqual(self.ui.session_state, {})
        self.assert_toast(0, "出題候補を確認できませんでした", "❌")

    def test_normal_queue_and_toast_only_queue_are_consumed_independently(self):
        components.queue_success("日記を保存しました")
        components.queue_toast("共有が完了しました")
        components.show_queued_message()
        self.assertIn(components.TOAST_MESSAGE_KEY, self.ui.session_state)
        components.show_queued_toasts()
        self.assertEqual(self.ui.inline, [("success", "日記を保存しました")])
        self.assertEqual([message for message, _ in self.ui.toasts],
                         ["日記を保存しました", "共有が完了しました"])

    def test_inject_css_consumes_both_queues_without_loading_user_data(self):
        components.queue_success("記録を追加しました")
        components.queue_toast("提案を取り込みました")
        with patch.object(components, "_theme_css", return_value="theme"), \
             patch.object(components, "is_dark_theme", return_value=False), \
             patch.object(components, "configure_plot_theme"), \
             patch.object(components.Path, "exists", return_value=False), \
             patch.object(components, "load_user_settings") as read, \
             patch.object(components, "save_user_settings") as write:
            components.inject_css()
            components.inject_css()
        read.assert_not_called()
        write.assert_not_called()
        self.assertEqual(len(self.ui.inline), 1)
        self.assertEqual(len(self.ui.toasts), 2)
        self.assertNotIn(components.FLASH_MESSAGE_KEY, self.ui.session_state)
        self.assertNotIn(components.TOAST_MESSAGE_KEY, self.ui.session_state)

    def test_error_retains_details_inline_and_uses_short_popup(self):
        components.show_action_error("保存競合: 未保存の入力を保持しています。")
        self.assertEqual(self.ui.inline, [("error", "保存競合: 未保存の入力を保持しています。")])
        self.assert_toast(0, "処理を完了できませんでした。詳細表示を確認してください。", "❌")
        components.show_action_error("Google側のエラー詳細", toast_message="共有できませんでした")
        self.assert_toast(1, "共有できませんでした", "❌")

    def test_immediate_result_accepts_custom_popup_and_all_message_kinds(self):
        for kind, icon in (("success", "✅"), ("info", "ℹ️"), ("warning", "⚠️"), ("error", "❌")):
            with self.subTest(kind=kind):
                components.show_action_message("詳細を画面に残します", kind=kind, toast_message="短い結果")
                self.assertEqual(self.ui.inline[-1], (kind, "詳細を画面に残します"))
                self.assert_toast(len(self.ui.toasts) - 1, "短い結果", icon)

    def test_unknown_kind_uses_info_for_legacy_and_immediate_results(self):
        components.queue_message("旧形式の結果", kind="unknown")
        components.show_queued_message()
        components.show_action_message("直接表示の結果", kind="unknown")
        components.queue_toast("通知だけの結果", kind="unknown")
        components.show_queued_toasts()
        self.assertEqual([kind for kind, _ in self.ui.inline], ["info", "info"])
        self.assertTrue(all(kwargs["icon"] == "ℹ️" for _, kwargs in self.ui.toasts))

    def test_blank_and_malformed_queued_payloads_do_not_display_or_replay(self):
        components.queue_success("  ")
        components.queue_toast("")
        components.show_action_message("\n ")
        self.assertEqual(self.ui.session_state, {})
        self.ui.session_state[components.FLASH_MESSAGE_KEY] = [None, "bad payload", {}, {"message": " "}]
        self.ui.session_state[components.TOAST_MESSAGE_KEY] = "bad payload"
        components.show_queued_message()
        components.show_queued_toasts()
        self.assertEqual(self.ui.inline, [])
        self.assertEqual(self.ui.toasts, [])
        self.assertEqual(self.ui.session_state, {})

    def test_blank_custom_popup_keeps_inline_result(self):
        components.show_action_message("保存しました", toast_message=" ")
        self.assertEqual(self.ui.inline, [("success", "保存しました")])
        self.assertEqual(self.ui.toasts, [])

    def test_delivery_failure_preserves_completed_results_and_consumes_queue(self):
        private_detail = "private form input or credential"
        self.ui.toast_failures = [RuntimeError(private_detail)]
        components.queue_success("保存しました")
        components.queue_success("別の記録を保存しました")
        with self.assertLogs(components._LOGGER, level="WARNING") as logged:
            components.show_queued_message()
        self.assertEqual(self.ui.inline, [
            ("success", "保存しました"), ("success", "別の記録を保存しました"),
        ])
        self.assertEqual(len(self.ui.toasts), 2)
        self.assertNotIn(private_detail, " ".join(logged.output))
        self.assertIn("RuntimeError", " ".join(logged.output))
        components.show_queued_message()
        self.assertEqual(len(self.ui.toasts), 2)

    def test_legacy_duration_signature_is_retried_without_duration(self):
        self.ui.toast_failures = [TypeError("toast() got an unexpected keyword argument 'duration'")]
        components.show_action_message("保存しました")
        self.assertEqual(self.ui.inline, [("success", "保存しました")])
        self.assert_toast(0, "保存しました", "✅")
        self.assertEqual(self.ui.toasts[1], ("保存しました", {"icon": "✅"}))

    def test_unrelated_type_error_is_not_retried(self):
        self.ui.toast_failures = [TypeError("private invalid toast input")]
        with self.assertLogs(components._LOGGER, level="WARNING") as logged:
            components.show_action_message("保存しました")
        self.assertEqual(len(self.ui.toasts), 1)
        self.assertEqual(self.ui.inline, [("success", "保存しました")])
        self.assertNotIn("private invalid toast input", " ".join(logged.output))

    def test_legacy_fallback_failure_is_also_isolated_from_completed_action(self):
        self.ui.toast_failures = [
            TypeError("toast() got an unexpected keyword argument 'duration'"),
            RuntimeError("private fallback failure"),
        ]
        with self.assertLogs(components._LOGGER, level="WARNING") as logged:
            components.show_action_message("保存しました")
        self.assertEqual(len(self.ui.toasts), 2)
        self.assertEqual(self.ui.inline, [("success", "保存しました")])
        self.assertNotIn("private fallback failure", " ".join(logged.output))

    def test_missing_native_toast_keeps_inline_and_logs_only_exception_type(self):
        ui_without_toast = SimpleNamespace(
            success=self.ui.success, info=self.ui.info,
            warning=self.ui.warning, error=self.ui.error,
        )
        with patch.object(components, "st", ui_without_toast), \
             self.assertLogs(components._LOGGER, level="WARNING") as logged:
            components.show_action_message("保存しました")
        self.assertEqual(self.ui.inline, [("success", "保存しました")])
        self.assertIn("AttributeError", " ".join(logged.output))

    def test_inline_rendering_errors_are_not_hidden_by_toast_failure_boundary(self):
        with patch.object(self.ui, "success", side_effect=RuntimeError("inline failed")):
            with self.assertRaisesRegex(RuntimeError, "inline failed"):
                components.show_action_message("保存しました")
        self.assertEqual(self.ui.toasts, [])

    def test_rerun_style_interruptions_are_not_swallowed(self):
        class RenderInterrupted(BaseException):
            pass

        self.ui.toast_failures = [RenderInterrupted()]
        components.queue_success("保存しました")
        with self.assertRaises(RenderInterrupted):
            components.show_queued_message()
        self.assertNotIn(components.FLASH_MESSAGE_KEY, self.ui.session_state)
        components.show_queued_message()
        self.assertEqual(len(self.ui.toasts), 1)


if __name__ == "__main__":
    unittest.main()
