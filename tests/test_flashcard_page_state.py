from __future__ import annotations

from copy import deepcopy
from contextlib import nullcontext
from datetime import date
from pathlib import Path
import unittest
from unittest.mock import patch

import data_utils
import prolog_ui.components as ab
from app_core import app_clock, json_operations, json_store, master_snapshot, reminder_service


PROJECT = Path(__file__).resolve().parents[1]
PAGE = PROJECT / "pages" / "07_記憶フラッシュ.py"


class FlashcardPageStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit import config
        config.get_config_options(options_from_flags={"global.developmentMode": False, "browser.gatherUsageStats": False})

    def setUp(self):
        self.cards = [
            {"card_id": "c1", "question": "架空の問題 A", "answer": "答え A", "created_at": "2026-09-17"},
            {"card_id": "c2", "question": "架空の問題 B", "answer": "答え B", "created_at": "2026-09-17"},
        ]
        self.patches = [
            patch.object(data_utils, "load_all_flashcards", side_effect=lambda *args, **kwargs: deepcopy(self.cards)),
            patch.object(data_utils, "load_all_notes", return_value=[]),
            patch.object(data_utils, "save_flashcards", side_effect=AssertionError("Unexpected save")),
            patch.object(master_snapshot, "load_master_data", return_value={}),
            patch.object(app_clock, "app_today", return_value=date(2026, 9, 17)),
            patch.object(reminder_service, "get_reminders_by_card_id", return_value=[]),
            patch.object(ab, "inject_css"), patch.object(ab, "sidemenu"), patch.object(ab, "topbar"),
            patch.object(ab, "queue_success"),
            patch.object(json_store, "_read", side_effect=AssertionError("No personal JSON reads")),
            patch.object(json_operations, "atomic_json_operation", side_effect=lambda **kwargs: nullcontext()),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.random_patch = patch("random.choice", side_effect=lambda options: options[0])
        self.choose = self.random_patch.start()
        self.addCleanup(self.random_patch.stop)
        self.create_patch = patch.object(reminder_service, "create_reminder", return_value={"reminder_id": "synthetic"})
        self.create = self.create_patch.start()
        self.addCleanup(self.create_patch.stop)

    def app(self):
        from streamlit.testing.v1 import AppTest
        result = AppTest.from_file(str(PAGE), default_timeout=30).run()
        self.assertFalse(result.exception, str(result.exception))
        return result

    def test_schedule_action_uses_the_card_that_was_displayed(self):
        app = self.app()
        self.choose.side_effect = lambda options: options[-1]
        app.button(key="schedule_card_c1").click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.create.assert_called_once()
        self.assertEqual(self.create.call_args.args[0]["source_card_id"], "c1")
        self.assertEqual(self.choose.call_count, 1)
        self.assertTrue(any(button.key == "schedule_card_c1" for button in app.button))

    def test_rerun_keeps_card_and_explicit_next_selects_another_without_saving(self):
        app = self.app()
        app.run()
        self.assertEqual(self.choose.call_count, 1)
        app.button(key="p07_next_card").click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertTrue(any(button.key == "schedule_card_c2" for button in app.button))
        self.assertEqual(self.choose.call_count, 2)
        self.create.assert_not_called()

    def test_filter_or_external_deletion_reselects_only_available_cards(self):
        app = self.app()
        app.text_input(key="card_filter_keyword").input("B").run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertTrue(any(button.key == "schedule_card_c2" for button in app.button))
        app.text_input(key="card_filter_keyword").input("").run()
        self.cards[:] = self.cards[:1]
        app.run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertTrue(any(button.key == "schedule_card_c1" for button in app.button))
        self.create.assert_not_called()

    def test_current_card_content_is_refreshed_without_drawing_again(self):
        app = self.app()
        app.checkbox(key="p07_show_answer").check().run()
        self.cards[0]["answer"] = "外部変更した架空の答え"
        app.run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertTrue(any(item.value == "外部変更した架空の答え" for item in app.markdown))
        self.assertEqual(self.choose.call_count, 1)

    def test_information_card_has_line_breaks_without_literal_html_markup(self):
        app = self.app()
        information = next(item.value for item in app.markdown if "カード情報" in item.value)
        self.assertIn("<br>", information)
        self.assertNotIn("&lt;br&gt;", information)

    def test_answer_is_hidden_when_the_card_changes(self):
        app = self.app()
        app.checkbox(key="p07_show_answer").check().run()
        self.assertTrue(any(item.value == "答え A" for item in app.markdown))
        app.button(key="p07_next_card").click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertFalse(app.checkbox(key="p07_show_answer").value)
        self.assertFalse(any(item.value == "答え B" for item in app.markdown))
        app.checkbox(key="p07_show_answer").check().run()
        app.text_input(key="card_filter_keyword").input("A").run()
        self.assertFalse(app.checkbox(key="p07_show_answer").value)

    def test_next_with_one_card_and_no_matching_cards_do_not_fail(self):
        self.cards[:] = self.cards[:1]
        app = self.app()
        app.checkbox(key="p07_show_answer").check().run()
        app.button(key="p07_next_card").click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertFalse(app.checkbox(key="p07_show_answer").value)
        app.text_input(key="card_filter_keyword").input("見つからない").run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertFalse(any(button.key == "p07_next_card" for button in app.button))
        self.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
