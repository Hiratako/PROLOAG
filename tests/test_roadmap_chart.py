from datetime import date
import unittest
from app_core.roadmap_chart import build_roadmap_figure
import test_roadmap_service as fixtures


class RoadmapChartTests(unittest.TestCase):
    def item(self, **changes):
        return {
            **fixtures.RoadmapTests.payload(self),
            "id": "rm_" + "a" * 32,
            "baseline_start_date": "2026-11-01",
            "baseline_end_date": "2026-11-10",
            **changes,
        }

    def figure(self, item):
        return build_roadmap_figure(
            [item], [], date(2026, 11, 1), date(2026, 12, 31), today=date(2026, 11, 5)
        )

    def test_single_day_includes_one_whole_day_and_progress_is_separate(self):
        figure = self.figure(
            self.item(start_date="2026-11-10", end_date="2026-11-10", completed=5)
        )
        bars = [trace for trace in figure.data if trace.type == "bar"]
        self.assertTrue(any(float(trace.x[0]) == 86400000 for trace in bars))
        self.assertTrue(any(float(trace.x[0]) == 43200000 for trace in bars))

    def test_changed_schedule_baseline_deadline_and_today_are_present(self):
        figure = self.figure(self.item(end_date="2026-12-01", deadline="2026-11-29"))
        self.assertTrue(
            any(
                trace.type == "scatter" and trace.mode == "lines"
                for trace in figure.data
            )
        )
        self.assertTrue(
            any(
                trace.type == "scatter" and trace.marker.symbol == "diamond-open"
                for trace in figure.data
            )
        )
        self.assertEqual(len(figure.layout.shapes), 1)

    def test_zero_progress_item_still_has_selectable_full_span(self):
        item = self.item(completed=0)
        figure = self.figure(item)
        self.assertTrue(
            any(
                trace.type == "bar"
                and trace.customdata is not None
                and trace.customdata[0][0] == item["id"]
                for trace in figure.data
            )
        )

    def test_title_is_escaped_and_no_remote_resources_are_required(self):
        figure = self.figure(self.item(title="<b>literal</b>"))
        self.assertTrue(
            any(
                "&lt;b&gt;" in str(getattr(trace, "hovertemplate", ""))
                for trace in figure.data
            )
        )
        self.assertEqual(figure.layout.dragmode, "pan")

    def test_import_preview_does_not_fabricate_zero_actual_time(self):
        figure = build_roadmap_figure(
            [self.item()], [], date(2026, 11, 1), date(2026, 12, 31),
            today=date(2026, 11, 5), show_actuals=False,
        )
        hovers = " ".join(str(getattr(trace, "hovertemplate", "")) for trace in figure.data)
        self.assertIn("予定 10h", hovers)
        self.assertNotIn("実績", hovers)
        original = self.figure(self.item())
        self.assertTrue(any("実績" in str(getattr(trace, "hovertemplate", "")) for trace in original.data))


if __name__ == "__main__":
    unittest.main()
