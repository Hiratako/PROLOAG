"""Exercise real exhibition views, filters and independent visitor state."""
from datetime import timedelta
from html import unescape
from pathlib import Path
import re
import unittest

from streamlit.testing.v1 import AppTest

from web_demo.data import DATA_ROOT, PERIOD_START, REFERENCE_DATE, data_fingerprint


def _render_product(route):
    from web_demo.catalog import render_product_view
    render_product_view(route)


def _render_about():
    from web_demo.app import about
    about()


def _widget(elements, label):
    return next(element for element in elements if element.label == label)


def _metric(app, label):
    for block in app.markdown:
        if f'>{label}</div>' in block.value:
            found = re.search(r'class="ab-metric-value">(.*?)</div>', block.value, re.S)
            if found:
                return unescape(found.group(1)).strip()
    raise AssertionError(f"Visible metric was not rendered: {label}")


class ExhibitionViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit import config
        config.get_config_options(force_reparse=True,
                                  options_from_flags={"global.developmentMode": False})

    def setUp(self):
        self.before = data_fingerprint()
        self.apps = []

    def tearDown(self):
        try:
            self.assertEqual(data_fingerprint(), self.before,
                             "Viewing/filtering must not mutate the bundled records")
        finally:
            from web_demo.sandbox import SESSION_KEY, destroy_sandbox
            for app in self.apps:
                try:
                    handle = app.session_state[SESSION_KEY]
                except KeyError:
                    continue
                self.assertTrue(destroy_sandbox(handle), "Finished visitors must release temporary data")

    def render(self, route):
        app = AppTest.from_function(_render_product, args=(route,), default_timeout=60)
        self.apps.append(app)
        app.run()
        self.assert_healthy(app)
        return app

    def assert_healthy(self, app):
        self.assertEqual(len(app.exception), 0,
                         [error.message for error in app.exception])
        self.assertEqual(len(app.error), 0, [error.value for error in app.error])
        self.assertEqual(len(app.get("file_uploader")), 0)
        self.assertEqual(len(app.get("data_editor")), 0)
        for button in app.button:
            self.assertFalse(any(word in button.label
                                 for word in ("保存", "削除", "取り込", "送信", "接続")),
                             f"Mutation control is visible: {button.label}")

    def test_four_product_views_initially_render_without_mutation_controls(self):
        for route in ("study", "growth", "city", "health"):
            with self.subTest(route=route):
                app = self.render(route)
                self.assertGreater(len(app.markdown), 0)
                self.assertGreater(len(app.get("download_button")), 0)

    def test_study_period_updates_the_visible_aggregate(self):
        app = self.render("study")
        self.assertEqual(_metric(app, "カレンダー日数"), "180日")
        original_total = _metric(app, "総勉強時間")
        _widget(app.date_input, "開始日").set_value(
            REFERENCE_DATE - timedelta(days=6)).run(timeout=60)
        self.assert_healthy(app)
        self.assertEqual(_metric(app, "カレンダー日数"), "7日")
        self.assertNotEqual(_metric(app, "総勉強時間"), original_total)

    def test_growth_subject_filter_updates_the_visible_total(self):
        app = self.render("growth")
        before = _metric(app, "期間内の総勉強時間")
        subjects = _widget(app.selectbox, "科目")
        self.assertGreater(len(subjects.options), 1)
        subjects.set_value(subjects.options[1]).run(timeout=60)
        self.assert_healthy(app)
        self.assertNotEqual(_metric(app, "期間内の総勉強時間"), before)

    def test_city_subject_filter_only_displays_the_selected_subject(self):
        app = self.render("city")
        subjects = _widget(app.selectbox, "科目")
        self.assertGreater(len(subjects.options), 1)
        selected = subjects.options[1]
        subjects.set_value(selected).run(timeout=60)
        self.assert_healthy(app)
        self.assertEqual(len(app.dataframe), 1)
        rows = app.dataframe[0].value
        self.assertGreater(len(rows), 0)
        self.assertEqual(set(rows["科目"]), {selected})

    def test_two_health_visitors_keep_independent_periods(self):
        visitor_a = self.render("health")
        visitor_b = self.render("health")
        self.assertEqual(len(visitor_a.dataframe[0].value), 30)
        self.assertEqual(len(visitor_b.dataframe[0].value), 30)
        _widget(visitor_a.selectbox, "期間").set_value("直近7日").run(timeout=60)
        self.assert_healthy(visitor_a)
        self.assertEqual(len(visitor_a.dataframe[0].value), 7)
        visitor_b.run(timeout=60)
        self.assert_healthy(visitor_b)
        self.assertEqual(_widget(visitor_b.selectbox, "期間").value, "直近30日")
        self.assertEqual(len(visitor_b.dataframe[0].value), 30)
        self.assertEqual(len(visitor_a.dataframe[0].value), 7)

    def test_health_custom_period_is_operable_inside_the_sample_window(self):
        app = self.render("health")
        _widget(app.selectbox, "期間").set_value("任意期間").run(timeout=60)
        self.assert_healthy(app)
        _widget(app.date_input, "開始日").set_value(PERIOD_START).run(timeout=60)
        self.assert_healthy(app)
        _widget(app.date_input, "終了日").set_value(
            PERIOD_START + timedelta(days=13)).run(timeout=60)
        self.assert_healthy(app)
        self.assertEqual(len(app.dataframe[0].value), 14)

    def test_home_has_working_journey_and_feature_destinations(self):
        app = AppTest.from_file(DATA_ROOT / "main.py", default_timeout=60)
        self.apps.append(app)
        app.run()
        self.assert_healthy(app)
        self.assertEqual([element.proto.page for element in app.get("page_link")],
                         ["today", "timer", "notes", "study", "roadmap", "cards", "health"])
        self.assertTrue(all(element.proto.page_script_hash
                            for element in app.get("page_link")))
        text = "\n".join(element.value for element in app.markdown)
        self.assertIn("180", text)
        self.assertIn("2026/05/10", text)
        self.assertIn("2026/11/05", text)

    def test_about_explains_the_isolated_temporary_and_synthetic_scope(self):
        app = AppTest.from_function(_render_about, default_timeout=60).run()
        self.assert_healthy(app)
        text = "\n".join(element.value for element in app.markdown)
        self.assertIn("架空", text)
        self.assertIn("追加・編集", text)
        self.assertIn("セッションごと", text)
        self.assertIn("永続保存は行いません", text)
        self.assertIn("Google", text)
        self.assertIn("Hobby & Tools", text)
        self.assertIn("利用・改変・無料再配布可／販売・有料提供禁止", text)
        self.assertIn("個人・企業の内部業務", text)
        self.assertIn("https://github.com/Hiratako/PROLOAG/blob/codex/web-demo/LICENSE", text)
        self.assertIn("LicenseRef-PROLOAG-No-Sale-1.0", text)
        self.assertNotIn("ライセンスは未確定", text)


if __name__ == "__main__":
    unittest.main()
