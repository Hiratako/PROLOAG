"""Check timer partial updates using source-only copies and synthetic records.

Streamlit 1.57 AppTest starts a new fragment store for every run and does not
send fragment IDs. FragmentDriver uses its real ScriptRunner with a persistent
store and the widget's registered fragment ID. This exercises server partial
runs, not browser rendering or automatic one-second scheduling. Time readings
are reference values, not latency guarantees. No web server is started.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from smoke_phase1_analysis import ANCHOR, copy_sources, daily_path, snapshot_json, write_json
from smoke_page_performance import fixtures


FULL_COUNTER = "_qa_timer_full_runs"


class FragmentDriver:
    """Test-only bridge for real fragment reruns absent from public AppTest."""

    def __init__(self, path: Path):
        from streamlit.runtime.fragment import MemoryFragmentStorage
        from streamlit.testing.v1 import AppTest
        self.app = AppTest.from_file(path, default_timeout=120)
        self.storage = MemoryFragmentStorage()
        self.component_registry = None
        self.messages = []
        self.fragment_id: str | None = None

    def run(self, widget=None, *, fragment_id: str | None = None):
        import streamlit.testing.v1.app_test as app_test_module
        from streamlit.proto.WidgetStates_pb2 import WidgetStates
        from streamlit.testing.v1.element_tree import get_widget_state
        from streamlit.testing.v1.local_script_runner import LocalScriptRunner
        owner = self
        if widget is not None:
            metadata = self.app.session_state._state._new_widget_state.widget_metadata[widget.id]
            self.fragment_id = metadata.fragment_id
        else:
            self.fragment_id = fragment_id

        class PersistentRunner(LocalScriptRunner):
            def __init__(self, *args, **kwargs):
                from streamlit.runtime import Runtime
                super().__init__(*args, **kwargs)
                self._fragment_storage = owner.storage
                # A real server keeps its Runtime and v2 component registrations.
                # Public AppTest creates another Runtime for every .run().
                runtime = Runtime.instance()
                if owner.component_registry is None:
                    owner.component_registry = runtime.bidi_component_registry
                else:
                    runtime.bidi_component_registry = owner.component_registry
                # Keep widgets outside the fragment available in the test tree.
                # A normal full run starts with a clean message queue.
                if owner.fragment_id:
                    for message in owner.messages:
                        self.forward_msg_queue.enqueue(message)

            def request_rerun(self, data):
                if owner.fragment_id:
                    data = replace(data, fragment_id_queue=[owner.fragment_id])
                    owner.fragment_id = None
                return super().request_rerun(data)

            def run(self, *args, **kwargs):
                tree = super().run(*args, **kwargs)
                owner.messages = list(self.forward_msgs())
                return tree

        # AppTest retains removed delta nodes after an internal full rerun.
        # Only active registered widgets should be sent, as in a browser.
        widget_states = WidgetStates()
        for node in self.app._tree:
            if hasattr(node, "id") and node.id not in self.app.session_state:
                continue
            state = get_widget_state(node)
            if state is not None:
                widget_states.widgets.append(state)
        with patch.object(app_test_module, "LocalScriptRunner", PersistentRunner):
            self.app._run(widget_states, timeout=120)
        if self.app.exception:
            raise AssertionError([item.message for item in self.app.exception])
        return self.app


def timer_fixtures(root: Path) -> None:
    fixtures(root)
    master_path = root / "master_data/master.json"
    master = json.loads(master_path.read_text(encoding="utf-8"))
    stamp = ANCHOR.isoformat()
    for collection, id_key, identifier, parents in (
        ("projects", "project_id", "p2", {}),
        ("subjects", "subject_id", "s2", {"project_id": "p2"}),
        ("materials", "material_id", "m2", {"project_id": "p2", "subject_id": "s2"}),
        ("units", "unit_id", "u2", {"project_id": "p2", "subject_id": "s2", "material_id": "m2"}),
    ):
        master[collection].append({id_key: identifier, "name": "別分類 " + identifier,
                                   "is_active": True, "created_at": stamp,
                                   "updated_at": stamp, **parents})
    write_json(master_path, master)
    plan_path = daily_path(root, "daily_plans", ANCHOR)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["tasks"] = [
        {"task_id": "task_a", "title": "最初のタスク", "planned_minutes": 30,
         "project_id": "p", "subject_id": "s", "material_id": "m", "unit_id": "u"},
        {"task_id": "task_b", "title": "最新のタスク", "planned_minutes": 30,
         "project_id": "p2", "subject_id": "s2", "material_id": "m2", "unit_id": "u2"},
    ]
    write_json(plan_path, plan)


def instrument(root: Path) -> Path:
    path = next((root / "pages").glob("01_*.py"))
    source = path.read_text(encoding="utf-8-sig")
    marker = "ROOT = Path(__file__).resolve().parents[1]"
    if source.count(marker) != 1:
        raise AssertionError("Cannot locate timer initialization")
    source = source.replace(marker, marker + (
        f"\nst.session_state[{FULL_COUNTER!r}] = st.session_state.get({FULL_COUNTER!r}, 0) + 1"
    ))
    path.write_text(source, encoding="utf-8")
    return path


def worker(root: Path, output: Path, *, improved: bool) -> None:
    root = root.resolve()
    if root.name != "app" or not root.parent.parent.name.startswith("proloag-timer-partial-"):
        raise RuntimeError("Only an isolated timer test copy is permitted")
    if root.parent.parent.parent != Path(tempfile.gettempdir()).resolve():
        raise RuntimeError("Timer tests must stay inside the temporary directory")
    sys.path.insert(0, str(root))
    import config
    if Path(config.PROJECT_ROOT).resolve() != root:
        raise RuntimeError("Source imports escaped timer test isolation")
    import streamlit as st
    from app_core import json_store
    import prolog_ui.components as ab
    logging.disable(logging.WARNING)
    st.page_link = lambda *args, **kwargs: None
    reads: Counter[str] = Counter()
    metrics: dict[str, str] = {}
    original_read, original_metric = json_store._read, ab.metric_block

    def read(path):
        path = Path(path).resolve()
        if not path.is_relative_to(root):
            raise AssertionError(f"JSON read escaped synthetic root: {path}")
        reads[str(path.relative_to(root))] += 1
        return original_read(path)

    def metric(label, value, caption="", **kwargs):
        metrics[label] = str(value)
        metrics[label + ":caption"] = caption
        return original_metric(label, value, caption, **kwargs)

    def prohibit_write(*args, **kwargs):
        raise AssertionError("Read-only interaction attempted a JSON write")

    json_store._read, ab.metric_block = read, metric
    driver = FragmentDriver(next((root / "pages").glob("01_*.py")))
    initial = snapshot_json(root)
    results: dict[str, object] = {"measurements": {}, "checks": {}}

    def run(label: str, widget=None, *, fragment_id=None, writes=False):
        reads.clear()
        metrics.clear()
        before = driver.app.session_state.filtered_state.get(FULL_COUNTER, 0)
        started = time.perf_counter()
        guard = patch.object(json_store, "save_json_safe", prohibit_write)
        if writes:
            driver.run(widget, fragment_id=fragment_id)
        else:
            with guard:
                driver.run(widget, fragment_id=fragment_id)
        elapsed = time.perf_counter() - started
        groups: Counter[str] = Counter()
        for path, count in reads.items():
            groups[Path(path).parts[0]] += count
        full_runs = driver.app.session_state[FULL_COUNTER] - before
        result = {"seconds": round(elapsed, 4), "reads": sum(reads.values()),
                  "groups": dict(groups), "full_runs": full_runs,
                  "metrics": dict(metrics),
                  "choices": [[item.key, list(item.options), str(item.value)]
                              for item in driver.app.selectbox
                              if item.key and (item.key.startswith("timer_master_none_")
                                               or item.key.startswith("condition_"))]}
        results["measurements"][label] = result
        print(f"{root.parent.name}/{label}: full={full_runs}, JSON={sum(reads.values())}, {elapsed:.4f}s", flush=True)
        return result

    def set_value(kind, key, value, label):
        widget = getattr(driver.app, kind)(key=key)
        widget.set_value(value)
        return run(label, widget)

    def by_label(kind, label):
        return next(item for item in getattr(driver.app, kind) if item.label == label)

    def select_id(key, identifier, label):
        widget = driver.app.selectbox(key=key)
        value = next(option for option in widget.options if option.endswith(f"[{identifier}]"))
        widget.set_value(value)
        return run(label, widget)

    run("initial")
    run("ordinary_rerun")
    for key, value, label in (("condition_environment", "カフェ", "environment"),
                              ("condition_category", "医学", "category")):
        result = set_value("selectbox", key, value, label)
        if improved:
            assert result["full_runs"] == 0
            assert result["groups"].get("study_logs", 0) == 0
            assert result["groups"].get("master_data") == 1
    for key, identifier, label in (("project", "p", "project"), ("subject", "s", "subject"),
                                    ("material", "m", "material"), ("unit", "u", "unit")):
        result = select_id(f"timer_master_none_{key}", identifier, label)
        if improved:
            assert result["full_runs"] == 0
            assert result["groups"].get("study_logs", 0) == 0
    if improved:
        results["checks"]["selection_partial"] = True
    select_id("timer_master_none_project", "p2", "change_parent")
    assert driver.app.selectbox(key="timer_master_none_subject").value == "未設定"
    assert driver.app.selectbox(key="timer_master_none_material").value == "未設定"
    assert driver.app.selectbox(key="timer_master_none_unit").value == "未設定"
    results["checks"]["parent_clears_children"] = True

    if snapshot_json(root) != initial:
        raise AssertionError("Read-only selections changed synthetic JSON")
    results["checks"]["read_only_json_unchanged"] = True
    if not improved:
        output.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
        return

    # Set a paused, unsaved session in the isolated test's memory only.
    state = driver.app.session_state
    state["running"] = False
    state["start_perf"] = None
    state["start_at"] = datetime.combine(ANCHOR, datetime.min.time()).replace(hour=15)
    state["accumulated_sec"] = 600.0
    state["pending_session"] = {"start_at": f"{ANCHOR}T15:00:00", "end_at": f"{ANCHOR}T15:10:00",
                                "duration_sec": 600, "hourly_rate": 2000}
    run("paused_pending")
    set_value("text_area", "pending_memo", "保持するメモ", "memo")
    notes = driver.app.multiselect(key="pending_notes")
    selected_note = notes.options[0]
    notes.set_value([selected_note])
    result = run("related_notes", notes)
    assert result["full_runs"] == 0 and result["groups"].get("study_logs", 0) == 1
    set_value("checkbox", "pending_create_note", True, "new_note")
    set_value("text_input", "pending_new_note_title", "保持する新規ノート", "new_note_title")
    before_duration = state["pending_session"]["duration_sec"]
    for label in ("開始", "一時停止", "+60秒", "-60秒"):
        button = by_label("button", label)
        button.click()
        run("control_" + label, button)
        assert state["pending_memo"] == "保持するメモ"
        assert state["pending_notes"] == [selected_note]
        assert state["pending_create_note"] is True
        assert state["pending_new_note_title"] == "保持する新規ノート"
    assert abs(state["pending_session"]["duration_sec"] - before_duration) < 3
    results["checks"]["draft_controls"] = True

    # Setting edits must refresh the paused quote in the same partial run.
    result = set_value("number_input", "overtime_threshold", 0.5, "pending_threshold")
    assert result["full_runs"] == 0
    result = set_value("number_input", "overtime_multiplier", 2.0, "pending_multiplier")
    assert result["full_runs"] == 0
    expected_money = round(state["pending_session"]["duration_sec"] / 3600 * 2000 * 2)
    assert state["pending_session"]["money"] == expected_money
    assert result["metrics"]["保存予定時間:caption"] == f"換算金額 {expected_money:,}円"
    assert state["pending_memo"] == "保持するメモ" and state["pending_notes"] == [selected_note]
    assert state["pending_new_note_title"] == "保持する新規ノート"
    results["checks"]["settings_refresh_pending"] = True

    # External changes are synthetic fixture mutations, never app saves.
    master_path = root / "master_data/master.json"
    master = json.loads(master_path.read_text(encoding="utf-8"))
    master["subjects"].append({"subject_id": "s3", "project_id": "p2", "name": "外部追加科目", "is_active": True})
    write_json(master_path, master)
    set_value("selectbox", "condition_environment", "図書館", "external_master")
    assert any(option.endswith("[s3]") for option in driver.app.selectbox(key="timer_master_none_subject").options)
    results["checks"]["external_master_fresh"] = True

    # Fail the complete staged save because its selected task was removed.
    task_widget = by_label("selectbox", "今日の記録・計画からタスクを選択")
    task_widget.set_value(next(value for value in task_widget.options if value.endswith("[task_a]")))
    run("select_removed_task", task_widget)
    plan_path = daily_path(root, "daily_plans", ANCHOR)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["tasks"] = [task for task in plan["tasks"] if task["task_id"] != "task_a"]
    write_json(plan_path, plan)
    study_path = daily_path(root, "study_logs", ANCHOR)
    study_before_failure = study_path.read_bytes()
    note_before_failure = (root / "notes_data/notebooks.json").read_bytes()
    button = by_label("button", "保存する")
    button.click()
    result = run("save_failure", button, writes=True)
    assert result["full_runs"] == 0
    assert driver.app.error and "入力は保持しています" in driver.app.error[-1].value
    assert state["pending_session"] is not None
    assert state["pending_memo"] == "保持するメモ" and state["pending_notes"] == [selected_note]
    assert state["pending_new_note_title"] == "保持する新規ノート"
    assert study_path.read_bytes() == study_before_failure
    assert (root / "notes_data/notebooks.json").read_bytes() == note_before_failure
    results["checks"]["failure_keeps_draft"] = True

    task_widget = by_label("selectbox", "今日の記録・計画からタスクを選択")
    task_widget.set_value(next(value for value in task_widget.options if value.endswith("[task_b]")))
    run("latest_task", task_widget)
    set_value("selectbox", "condition_category", "IT", "latest_category")
    set_value("selectbox", "condition_environment", "図書館", "latest_environment")
    duration = state["pending_session"]["duration_sec"]
    total_before = sum(float(item.get("duration_sec", 0)) for path in (root / "study_logs").rglob("*.json")
                       for item in json.loads(path.read_text(encoding="utf-8")))
    button = by_label("button", "保存する")
    button.click()
    result = run("save_success", button, writes=True)
    assert result["full_runs"] == 1
    saved = json.loads(study_path.read_text(encoding="utf-8"))[-1]
    assert {key: saved[key] for key in ("project_id", "subject_id", "material_id", "unit_id")} == {
        "project_id": "p2", "subject_id": "s2", "material_id": "m2", "unit_id": "u2"}
    assert saved["linked_task_id"] == "task_b" and saved["linked_task_date"] == ANCHOR.isoformat()
    assert saved["condition"]["category"] == "IT" and saved["condition"]["environment"] == "図書館"
    assert saved["memo"] == "保持するメモ" and saved["duration_sec"] == duration
    assert len(saved["linked_note_ids"]) == 2 and "n0" in saved["linked_note_ids"]
    assert state["pending_session"] is None
    total_after = sum(float(item.get("duration_sec", 0)) for path in (root / "study_logs").rglob("*.json")
                      for item in json.loads(path.read_text(encoding="utf-8")))
    assert total_after == total_before + duration
    hours, rem = divmod(int(total_after), 3600)
    minutes, seconds = divmod(rem, 60)
    assert by_label("metric", "全期間の総勉強時間").value == f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    results["checks"]["latest_values_saved"] = True
    results["checks"]["own_save_refreshes_totals"] = True

    # Trigger the actual registered display fragment without browser scheduling.
    full_count = state[FULL_COUNTER]
    tick_ids = {message.auto_rerun.fragment_id for message in driver.messages
                if message.WhichOneof("type") == "auto_rerun"
                and driver.storage.contains(message.auto_rerun.fragment_id)}
    assert len(tick_ids) == 1, f"Expected one live timer fragment, found {tick_ids}"
    tick_id = next(iter(tick_ids))
    first = run("fresh_tick_before", fragment_id=tick_id)
    rows = json.loads(study_path.read_text(encoding="utf-8"))
    rows.append({"session_id": "external_tick", "start_at": f"{ANCHOR}T12:00:00", "duration_sec": 60, "money": 60})
    write_json(study_path, rows)
    second = run("fresh_tick_after", fragment_id=tick_id)
    assert state[FULL_COUNTER] == full_count
    assert first["groups"].get("study_logs") == second["groups"].get("study_logs") == 1
    def seconds(text):
        h, m, s = map(int, text.split(":"))
        return h * 3600 + m * 60 + s
    assert seconds(second["metrics"]["今日の累計勉強時間"]) - seconds(first["metrics"]["今日の累計勉強時間"]) == 60
    results["checks"]["tick_fresh"] = True

    # Keep the selected task's owning day after the demo date advances.
    write_json(root / "settings/demo_mode.json", {"enabled": True, "fixed_today": (ANCHOR + timedelta(days=1)).isoformat()})
    task_day = state["selected_plan_task_date"]
    set_value("selectbox", "condition_mood", "4", "midnight_context")
    assert state["selected_plan_task_id"] == "task_b"
    assert state["selected_plan_task_date"] == task_day == ANCHOR.isoformat()
    assert any(ANCHOR.isoformat() in item.value for item in driver.app.caption)
    results["checks"]["task_owner_day"] = True
    output.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")


def run_isolated(source: Path, baseline: Path | None = None) -> dict:
    source = source.resolve()
    with tempfile.TemporaryDirectory(prefix="proloag-timer-partial-") as temporary:
        temp = Path(temporary)
        outputs = {}
        variants = [("before", baseline)] if baseline is not None else []
        variants.append(("after", source))
        for variant, origin in variants:
            root = temp / variant / "app"
            copy_sources(origin, root)
            timer_fixtures(root)
            instrument(root)
            output = temp / f"{variant}.json"
            environment = {
                **os.environ,
                "PYTHONPATH": os.pathsep.join([str(root), os.environ.get("PYTHONPATH", "")]),
                "PYTHONIOENCODING": "utf-8", "MPLCONFIGDIR": str(temp / "matplotlib"),
                "STREAMLIT_GLOBAL_DEVELOPMENT_MODE": "false", "STREAMLIT_LOGGER_LEVEL": "error",
            }
            command = [sys.executable, "-B", str(Path(__file__).resolve()), "--worker", str(root), "--output", str(output)]
            if variant == "after":
                command.append("--improved")
            result = subprocess.run(command, cwd=temp, env=environment, check=False)
            if result.returncode:
                raise AssertionError(f"{variant}: isolated timer worker failed ({result.returncode})")
            outputs[variant] = json.loads(output.read_text(encoding="utf-8"))
        return outputs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, help="Read-only baseline source directory")
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--improved", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker, args.output, improved=args.improved)
        return 0
    results = run_isolated(args.source, args.baseline)
    if args.output:
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    if "before" in results:
        for label in ("initial", "ordinary_rerun", "environment", "category", "project", "subject", "material", "unit", "change_parent"):
            old = results["before"]["measurements"][label]
            new = results["after"]["measurements"][label]
            assert old["choices"] == new["choices"], f"{label}: classification/condition choices changed"
            if label in ("initial", "ordinary_rerun"):
                assert old["metrics"] == new["metrics"], f"{label}: timer values changed"
        for label in ("environment", "category", "project", "subject", "material", "unit"):
            old = results["before"]["measurements"][label]
            new = results["after"]["measurements"][label]
            assert old["full_runs"] == 1 and new["full_runs"] == 0
            assert new["groups"].get("study_logs", 0) == 0
            print(f"MATCH {label}: full {old['full_runs']} -> {new['full_runs']}, JSON {old['reads']} -> {new['reads']}")
    print(f"PASS: {len(results['after']['checks'])} timer safety and partial-update checks; automatic scheduling requires browser QA")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
