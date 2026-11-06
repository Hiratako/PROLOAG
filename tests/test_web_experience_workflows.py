"""Exercise the real non-Hobby pages against independent disposable visitors.

These tests deliberately use the product services and real Streamlit widgets;
they do not patch out JSON writes or change the shared fixture configuration.
"""
from __future__ import annotations

from datetime import date
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from streamlit.testing.v1 import AppTest

from web_demo.data import data_fingerprint


DAY = date(2026, 11, 5)
PRODUCT_ROUTES = {
    "today", "roadmap", "timer", "logs", "study", "performance", "statistics",
    "growth", "masters", "notes", "city", "cards", "review", "diary",
    "substances", "sleep", "exercise", "health", "condition", "guide",
    "google-guide", "export", "legacy-import", "settings", "integrity",
}


def _render_experience(route, visitor):
    import streamlit as st
    from web_demo.app import make_pages
    from web_demo.catalog import render_product_view
    from web_demo.navigation import PAGES_STATE_KEY
    from web_demo.sandbox import sandbox_context

    st.session_state["_proloag_sandbox"] = visitor
    with sandbox_context(visitor):
        pages = make_pages()
        st.session_state[PAGES_STATE_KEY] = pages
        st.navigation(list(pages.values()))
        render_product_view(route)


def widget(elements, label):
    matches = [item for item in elements if item.label == label]
    if len(matches) != 1:
        raise AssertionError(
            f"Expected one {label!r}; got {len(matches)}: "
            f"{[(item.label, item.key) for item in matches]}")
    return matches[0]


class ExperienceWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit import config as streamlit_config
        streamlit_config.get_config_options(
            force_reparse=True,
            options_from_flags={"global.developmentMode": False},
        )

    def setUp(self):
        from web_demo.sandbox import create_sandbox, destroy_sandbox
        self.seed_before = data_fingerprint()
        # Keep transaction-generation paths below the classic Windows limit.
        self.temp = tempfile.TemporaryDirectory(prefix="w_")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.a = create_sandbox(base_dir=base)
        self.b = create_sandbox(base_dir=base)
        self.addCleanup(destroy_sandbox, self.a)
        self.addCleanup(destroy_sandbox, self.b)

    def tearDown(self):
        self.assertEqual(self.seed_before, data_fingerprint(),
                         "A visitor must never change the bundled seed records")

    def render(self, route, visitor=None):
        app = AppTest.from_function(
            _render_experience, args=(route, visitor or self.a),
            default_timeout=90,
        ).run()
        self.healthy(app)
        return app

    def healthy(self, app):
        self.assertEqual(len(app.exception), 0,
                         [item.message for item in app.exception])
        self.assertEqual(len(app.error), 0, [item.value for item in app.error])

    def sessions(self, visitor):
        from app_core.study_log_service import load_all_study_sessions
        from web_demo.sandbox import sandbox_context
        with sandbox_context(visitor):
            return load_all_study_sessions()

    def test_every_non_hobby_product_page_is_registered_and_renders(self):
        from web_demo import catalog
        self.assertEqual(set(catalog.PRODUCT_VIEWS), PRODUCT_ROUTES)
        self.assertTrue(all(not name.startswith(("20_", "21_", "22_"))
                            for name in catalog.PRODUCT_VIEWS.values()))
        for route in sorted(PRODUCT_ROUTES):
            with self.subTest(route=route):
                app = self.render(route)
                self.assertGreater(len(app.markdown), 0)

    def test_manual_log_add_edit_delete_is_private_and_idempotent(self):
        from app_core import study_log_editor as editor
        from web_demo.sandbox import sandbox_context

        before_a = self.sessions(self.a)
        before_b = self.sessions(self.b)
        app = self.render("logs")
        widget(app.text_input, "勉強内容").set_value("Private visitor A manual log")
        widget(app.button, "追加する").click().run(timeout=90)
        self.healthy(app)
        added = [row for row in self.sessions(self.a)
                 if row.get("memo") == "Private visitor A manual log"]
        self.assertEqual(len(added), 1)
        self.assertEqual(len(self.sessions(self.a)), len(before_a) + 1)
        self.assertEqual(self.sessions(self.b), before_b)
        # AppTest retains fragment deltas after an explicit st.rerun; ask for a
        # complete script render before interacting with the same form again.
        app.run(timeout=90)
        self.healthy(app)
        widget(app.button, "追加する").click().run(timeout=90)
        self.healthy(app)
        self.assertEqual(len(self.sessions(self.a)), len(before_a) + 1,
                         "Repeating a submitted form must not duplicate its log")

        # The preview/commit services used by the log editor must also remain
        # inside the active visitor, including journals and delete before-images.
        sid = added[0]["session_id"]
        with sandbox_context(self.a):
            snapshot = editor.load_editable_session(sid)
            preview = editor.prepare_session_edit(
                snapshot, {"memo": "Private A edited"})
            editor.commit_session_change(preview)
            edited = editor.load_editable_session(sid)
            self.assertEqual(edited["session"]["memo"], "Private A edited")
            deletion = editor.prepare_session_delete(edited)
            editor.commit_session_change(deletion)
            self.assertEqual(len(editor.list_deleted_sessions()), 1)
        self.assertEqual(len(self.sessions(self.a)), len(before_a))
        self.assertEqual(self.sessions(self.b), before_b)
        self.assertFalse((self.b.root / "backups/study_log_trash.json").exists())

    def test_timer_start_pause_and_save_adds_only_one_visitor_record(self):
        before_a = len(self.sessions(self.a))
        before_b = self.sessions(self.b)
        app = self.render("timer")
        widget(app.button, "開始").click().run(timeout=90)
        self.healthy(app)
        self.assertTrue(app.session_state["running"])
        # Use the product's visible correction rather than waiting for wall time.
        widget(app.button, "+60秒").click().run(timeout=90)
        widget(app.button, "一時停止").click().run(timeout=90)
        self.healthy(app)
        self.assertFalse(app.session_state["running"])
        self.assertGreaterEqual(app.session_state["pending_session"]["duration_sec"], 60)
        widget(app.text_area, "勉強内容・メモ").set_value("Private A timer")
        widget(app.button, "保存する").click().run(timeout=90)
        self.healthy(app)
        self.assertEqual(len(self.sessions(self.a)), before_a + 1)
        self.assertEqual(self.sessions(self.b), before_b)
        saved = [s for s in self.sessions(self.a)
                 if s.get("memo") == "Private A timer"]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["start_at"][:10], DAY.isoformat(),
                         "A demo timer must join the fixed demo-day reports")

    def test_today_plan_form_updates_only_the_current_visitor(self):
        from app_core.plan_service import load_daily_plan
        from web_demo.sandbox import sandbox_context

        with sandbox_context(self.b):
            before_b = load_daily_plan(DAY)
        app = self.render("today")
        widget(app.number_input, "今日の目標時間（分）").set_value(450)
        widget(app.text_input, "今日の重点科目（カンマ区切り）").set_value("Private A plan")
        widget(app.button, "基本計画を保存").click().run(timeout=90)
        self.healthy(app)
        with sandbox_context(self.a):
            actual = load_daily_plan(DAY)
            self.assertEqual(actual["target_total_minutes"], 450)
            self.assertIn("Private A plan", actual["focus_subjects"])
        with sandbox_context(self.b):
            self.assertEqual(load_daily_plan(DAY), before_b)

    def test_note_page_creation_and_card_creation_are_private(self):
        from app_core.note_service import load_all_notes
        from app_core.flashcard_service import load_flashcards
        from web_demo.sandbox import sandbox_context

        with sandbox_context(self.b):
            notes_b = load_all_notes()
            cards_b = load_flashcards()
        notes = self.render("notes")
        widget(notes.text_input, "新しいページ名").set_value("Private A notebook page")
        widget(notes.button, "ページ追加").click().run(timeout=90)
        self.healthy(notes)
        with sandbox_context(self.a):
            self.assertEqual(len([n for n in load_all_notes()
                                  if n.get("title") == "Private A notebook page"]), 1)
        cards = self.render("cards")
        widget(cards.text_area, "問題文").set_value("Private A question")
        widget(cards.text_area, "解答").set_value("Private A answer")
        widget(cards.button, "追加").click().run(timeout=90)
        self.healthy(cards)
        with sandbox_context(self.a):
            self.assertEqual(len([c for c in load_flashcards()
                                  if c.get("question") == "Private A question"]), 1)
        with sandbox_context(self.b):
            self.assertEqual(load_all_notes(), notes_b)
            self.assertEqual(load_flashcards(), cards_b)

    def test_diary_and_health_forms_do_not_write_other_visitors(self):
        from app_core.diary_service import load_diary_entry
        from app_core.health_service import load_health_log
        from web_demo.sandbox import sandbox_context

        with sandbox_context(self.b):
            diary_b, health_b = load_diary_entry(DAY), load_health_log(DAY)
        diary = self.render("diary")
        widget(diary.text_input, "タイトル").set_value("Private A diary")
        widget(diary.text_area, "本文").set_value("Synthetic-only private test note")
        widget(diary.button, "保存する").click().run(timeout=90)
        self.healthy(diary)
        sleep = self.render("sleep")
        widget(sleep.text_area, "メモ").set_value("Private A sleep memo")
        widget(sleep.button, "保存する").click().run(timeout=90)
        self.healthy(sleep)
        with sandbox_context(self.a):
            self.assertEqual(load_diary_entry(DAY)["title"], "Private A diary")
            self.assertEqual(load_health_log(DAY)["condition"]["memo"], "Private A sleep memo")
        with sandbox_context(self.b):
            self.assertEqual(load_diary_entry(DAY), diary_b)
            self.assertEqual(load_health_log(DAY), health_b)

    def test_exercise_form_and_settings_service_use_the_active_root(self):
        from app_core.exercise_service import load_exercise_log
        from app_core.settings_service import load_user_settings, update_user_settings
        from web_demo.sandbox import sandbox_context

        with sandbox_context(self.b):
            exercise_b, settings_b = load_exercise_log(DAY), load_user_settings()
        app = self.render("exercise")
        widget(app.text_input, "タイトル").set_value("Private A exercise")
        widget(app.number_input, "運動時間（分）").set_value(15)
        widget(app.button, "運動記録を保存する").click().run(timeout=90)
        self.healthy(app)
        with sandbox_context(self.a):
            self.assertEqual(len([r for r in load_exercise_log(DAY)["records"]
                                  if r.get("title") == "Private A exercise"]), 1)
            update_user_settings({"timer": {"hourly_rate": 3456}})
            self.assertEqual(load_user_settings()["timer"]["hourly_rate"], 3456)
        with sandbox_context(self.b):
            self.assertEqual(load_exercise_log(DAY), exercise_b)
            self.assertEqual(load_user_settings(), settings_b)

    def test_roadmap_preview_import_and_export_remain_visitor_scoped(self):
        from app_core import roadmap_import
        from app_core.export_service import build_export_bundle
        from app_core.note_service import add_note
        from app_core.roadmap_service import load_roadmap
        from web_demo.sandbox import sandbox_context

        title = "Private A imported roadmap"
        document = {
            "format": roadmap_import.FORMAT, "schema_version": 1,
            "plan_title": "Synthetic test plan", "items": [{
                "ref": "E01", "title": title,
                "start_date": "2026-11-09", "end_date": "2026-11-13",
                "deadline": None, "planned_minutes": 120,
                "status": "not_started", "progress_kind": "quantity",
                "completed": 0, "total": 20, "unit": "問",
                "classification": {"project": None, "subject": None,
                                   "material": None, "unit": None},
                "predecessor_refs": [], "memo": "Synthetic test only",
            }], "milestones": [],
        }
        with sandbox_context(self.b):
            roadmap_b = load_roadmap(self.b.root)
        with sandbox_context(self.a):
            preview = roadmap_import.build_import_preview(self.a.root, document)
            self.assertTrue(preview["can_commit"], preview["issues"])
            before = len(load_roadmap(self.a.root)["items"])
            roadmap_import.apply_import_preview(self.a.root, preview)
            self.assertEqual(len(load_roadmap(self.a.root)["items"]), before + 1)
            self.assertIn(title, [r["title"] for r in load_roadmap(self.a.root)["items"]])
            add_note("Private A export marker", body="Synthetic private content")
            bundle_a = build_export_bundle()
        with sandbox_context(self.b):
            self.assertEqual(load_roadmap(self.b.root), roadmap_b)
            bundle_b = build_export_bundle()
        with zipfile.ZipFile(io.BytesIO(bundle_a.zip_data)) as a_zip:
            a_text = b"\n".join(a_zip.read(name) for name in a_zip.namelist())
        with zipfile.ZipFile(io.BytesIO(bundle_b.zip_data)) as b_zip:
            b_text = b"\n".join(b_zip.read(name) for name in b_zip.namelist())
        self.assertIn(b"Private A export marker", a_text)
        self.assertNotIn(b"Private A export marker", b_text)


if __name__ == "__main__":
    unittest.main()
