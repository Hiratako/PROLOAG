"""Full daily/master page renders in guarded synthetic source copies."""
from __future__ import annotations

from collections import Counter
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
from smoke_page_performance import fixtures
from smoke_phase1_analysis import ANCHOR, copy_sources, daily_path, snapshot_json, write_json


# Captured from the pre-change pages, using the same synthetic records below.
# Master hashes include the 2026-10-31 study-category table; verified against
# source-only pre-roadmap copies before updating these three stale values.
# Compare all table cells, exact CSV bytes, metrics and plotted values.
BASELINE_OUTPUT_HASHES = {
    "10/initial": "da3369e1783c6e109947d5145dcd36c45cca51cd170782e52b502b87d66b8338",
    "10/repeat": "da3369e1783c6e109947d5145dcd36c45cca51cd170782e52b502b87d66b8338",
    "10/external-master": "e887df53c0f3b20000d4a50d50fc455c81313bec26250bf59da4d836fe9831af",
    "12/initial": "11b58c1abf4ed0a0e9314e81752f1233ac2a45a0e2e4681df140eae3fe891837",
    "12/repeat": "11b58c1abf4ed0a0e9314e81752f1233ac2a45a0e2e4681df140eae3fe891837",
    "12/date-switch": "5fbfff192d04bb5650f81bd93e921d8f7267901ce10646225a39e7b1f9606850",
    "12/external-log": "25ca1062c2f3cd458fb83f5945782a8dd379f5e4f9c0c6d546a5f386cb2eaeca",
    "12/external-settings": "e14b06fa1ea6f0d7eff6bd0f4aefb12640a2bee824f10798304bd277552f8ca9",
    "12/outside-period": "7ab96536947e18b164703489de05361e61715a3cc6af5451599c6b55c035e766",
    "13/initial": "54f5c7c95ee75577211a7677f41e9792742c5a2e54c0caaf89c9e0940f4f7221",
    "13/repeat": "54f5c7c95ee75577211a7677f41e9792742c5a2e54c0caaf89c9e0940f4f7221",
    "13/date-switch": "9e1ab25019d8fad1212ffd858a332bb3d265cad21cad285345903f612b0e2750",
    "13/external-log": "002b6b3f33017d6f3ba38c9adf60b416ea7cb131d9fd0537528fe0ba90cf3a99",
    "13/external-settings": "002b6b3f33017d6f3ba38c9adf60b416ea7cb131d9fd0537528fe0ba90cf3a99",
    "13/outside-period": "7f8c95b26d7d628596a491b7da626322b8570f07bbbd9da4a10f85b5d42f4ff9",
}


def output_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def worker(root: Path, output: Path) -> None:
    root = root.resolve()
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if (root.name != "app" or not root.parent.parent.name.startswith("proloag-daily-page-reads-")
            or root.parent.parent.parent != temporary_root):
        raise AssertionError("Only this test's isolated temporary source copies are permitted")
    sys.path.insert(0, str(root))
    import config
    if Path(config.PROJECT_ROOT).resolve() != root:
        raise AssertionError("App imports escaped the synthetic root")
    import matplotlib
    matplotlib.use("Agg")
    import streamlit as st
    from streamlit import config as streamlit_config
    from streamlit.testing.v1 import AppTest
    from app_core import exercise_service, health_service, json_store
    import prolog_ui.components as ab

    streamlit_config.get_config_options(options_from_flags={
        "global.developmentMode": False, "browser.gatherUsageStats": False,
    })
    # Legacy fixtures without timestamps are normalized with the current clock.
    # Freeze that clock so exact CSV comparisons do not depend on run time.
    exercise_service._now_text = lambda: f"{ANCHOR} 09:00:00"
    health_service._now_text = lambda: f"{ANCHOR} 09:00:00"
    st.page_link = lambda *args, **kwargs: None
    reads: Counter[str] = Counter()
    metrics, downloads, graphs = [], [], []
    original_read = json_store._read
    original_metric, original_download = ab.metric_block, st.download_button

    def guarded_read(path):
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(root):
            raise AssertionError(f"Out-of-fixture JSON read: {resolved}")
        reads[resolved.relative_to(root).as_posix()] += 1
        return original_read(path)

    def metric(label, value, caption="", **kwargs):
        metrics.append([label, str(value), caption])
        return original_metric(label, value, caption, **kwargs)

    def download(label, data=None, *args, **kwargs):
        if isinstance(data, str):
            data = data.encode("utf-8")
        downloads.append({"label": label, "bytes": data.hex(),
                          "on_click": kwargs.get("on_click", "rerun")})
        return original_download(label, data, *args, **kwargs)

    def plot(fig=None, *args, **kwargs):
        # Plot construction still executes; omit PNG encoding from this data check.
        if fig is not None:
            graphs.append([{
                "title": axis.get_title(),
                "lines": [{"label": line.get_label(), "x": list(map(str, line.get_xdata())),
                           "y": list(map(str, line.get_ydata()))} for line in axis.lines],
                "collections": [{"offsets": [list(map(str, point)) for point in collection.get_offsets()],
                                 "paths": [[list(map(str, point)) for point in path.vertices]
                                           for path in collection.get_paths()]}
                                for collection in axis.collections],
            } for axis in fig.axes])

    def prohibit_write(*args, **kwargs):
        raise AssertionError("Read-only rendering attempted to save JSON")

    json_store._read = guarded_read
    json_store.save_json_safe = prohibit_write
    ab.metric_block, st.download_button, st.pyplot = metric, download, plot
    result = {}

    def run(app, key, selected_date=None):
        before = snapshot_json(root)
        reads.clear()
        metrics.clear()
        downloads.clear()
        graphs.clear()
        app.run(timeout=60)
        if app.exception:
            raise AssertionError(f"{key}: {[item.message for item in app.exception]}")
        if before != snapshot_json(root):
            raise AssertionError(f"{key}: read-only rendering changed synthetic JSON")
        result[key] = {
            "reads": dict(reads),
            "selected_date": None if selected_date is None else selected_date.isoformat(),
            "downloads": list(downloads),
            "output": {
                "tables": [json.loads(item.value.to_json(orient="split", date_format="iso",
                             force_ascii=False, default_handler=str)) for item in app.dataframe],
                "metrics": list(metrics), "graphs": list(graphs),
                "csv": [{"label": item["label"], "bytes": item["bytes"]} for item in downloads],
            },
        }

    settings_path = root / "settings/user_settings.json"
    for number in ("10", "12", "13"):
        settings = {"display": {"show_task_metrics": False, "theme_mode": "standard",
                                "period_label": "直近7日"},
                    "analysis": {"caffeine_missing_as_zero": False}}
        write_json(settings_path, settings)
        app = AppTest.from_file(next((root / "pages").glob(f"{number}_*.py")), default_timeout=60)
        run(app, f"{number}/initial", ANCHOR if number != "10" else None)
        run(app, f"{number}/repeat", ANCHOR if number != "10" else None)
        if number == "10":
            path = root / "master_data/master.json"
            master = json.loads(path.read_text(encoding="utf-8"))
            master["projects"][0]["name"] = "外部更新プロジェクト"
            master["materials"][0]["name"] = "外部更新教材"
            write_json(path, master)
            run(app, "10/external-master")
        else:
            selected = ANCHOR - timedelta(days=1)
            next(item for item in app.date_input if item.label == "記録日").set_value(selected)
            run(app, f"{number}/date-switch", selected)
            folder = "health_logs" if number == "12" else "exercise_logs"
            path = daily_path(root, folder, selected)
            value = json.loads(path.read_text(encoding="utf-8"))
            if number == "12":
                value["sleep"]["sleep_duration_min"] = 222
                value["condition"]["memo"] = "外部更新体調"
                value["condition"]["focus_score"] = 5
            else:
                value["records"][0].update(title="外部更新運動", duration_min=77, distance_km=6.25)
            write_json(path, value)
            run(app, f"{number}/external-log", selected)
            settings["display"]["show_task_metrics"] = True
            write_json(settings_path, settings)
            run(app, f"{number}/external-settings", selected)
            outside = ANCHOR - timedelta(days=10)
            next(item for item in app.date_input if item.label == "記録日").set_value(outside)
            run(app, f"{number}/outside-period", outside)
    output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


def run_copy(source: Path, root: Path, output: Path) -> dict:
    copy_sources(source, root)
    fixtures(root)
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8",
                   "MPLCONFIGDIR": str(root.parent / "matplotlib"), "STREAMLIT_LOGGER_LEVEL": "error"}
    completed = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--worker",
                                str(root), str(output)], cwd=root.parent.parent, env=environment,
                               capture_output=True, text=True, encoding="utf-8", timeout=120)
    if completed.returncode:
        raise AssertionError(completed.stdout + completed.stderr)
    return json.loads(output.read_text(encoding="utf-8"))


class DailySharedPageReadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="proloag-daily-page-reads-")
        cls.addClassCleanup(cls.temporary.cleanup)
        temporary = Path(cls.temporary.name)
        cls.result = run_copy(PROJECT, temporary / "after/app", temporary / "after.json")

    def test_settings_master_and_selected_daily_logs_are_read_once_each_render(self):
        for key, state in self.result.items():
            with self.subTest(state=key):
                self.assertEqual(state["reads"].get("settings/user_settings.json", 0), 1)
                number = key.split("/")[0]
                if number == "10":
                    self.assertEqual(state["reads"].get("master_data/master.json", 0), 1)
                else:
                    folder = "health_logs" if number == "12" else "exercise_logs"
                    from datetime import date
                    selected = date.fromisoformat(state["selected_date"])
                    relative = f"{folder}/{selected:%Y}/{selected:%m}/{selected:%Y%m%d}.json"
                    self.assertEqual(state["reads"].get(relative, 0), 1)

    def test_tables_csv_metrics_and_graph_values_match_prechange_pages(self):
        self.assertEqual(set(self.result), set(BASELINE_OUTPUT_HASHES))
        for key, state in self.result.items():
            with self.subTest(state=key):
                self.assertEqual(output_hash(state["output"]), BASELINE_OUTPUT_HASHES[key])

    def test_external_changes_are_visible_and_downloads_do_not_trigger_reruns(self):
        master = self.result["10/external-master"]["output"]
        self.assertIn("外部更新プロジェクト", json.dumps(master, ensure_ascii=False))
        self.assertIn("外部更新教材", json.dumps(master, ensure_ascii=False))
        health = self.result["12/external-log"]["output"]
        self.assertIn("外部更新体調", json.dumps(health, ensure_ascii=False))
        self.assertIn(["睡眠時間", "3時間42分", "就寝・起床時刻から自動計算"], health["metrics"])
        exercise = self.result["13/external-log"]["output"]
        self.assertIn("外部更新運動", json.dumps(exercise, ensure_ascii=False))
        self.assertIn("1時間17分", json.dumps(exercise, ensure_ascii=False))
        initial_csv = bytes.fromhex(self.result["12/initial"]["output"]["csv"][0]["bytes"])
        updated_csv = bytes.fromhex(self.result["12/external-settings"]["output"]["csv"][0]["bytes"])
        self.assertNotIn(b"task_completion_rate", initial_csv)
        self.assertIn(b"task_completion_rate", updated_csv)
        for state in self.result.values():
            for item in state["downloads"]:
                self.assertEqual(item["on_click"], "ignore")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        worker(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        unittest.main()
