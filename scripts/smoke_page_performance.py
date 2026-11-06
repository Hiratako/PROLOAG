"""Compare page output and JSON reads in isolated synthetic app copies."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import timedelta
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from smoke_phase1_analysis import ANCHOR, copy_sources, create_fixtures, snapshot_json, write_json


PAGES = ("01", "04", "07", "08", "09", "10", "11", "12", "13", "14", "15")


def fixtures(root: Path) -> None:
    create_fixtures(root)
    stamp = f"{ANCHOR.isoformat()} 09:00:00"
    classification = {"project_id": "p", "subject_id": "s", "material_id": "m", "unit_id": "u"}
    write_json(root / "master_data" / "master.json", {
        "projects": [{"project_id": "p", "name": "検証プロジェクト", "is_active": True, "created_at": stamp, "updated_at": stamp}],
        "subjects": [{"subject_id": "s", "project_id": "p", "name": "検証科目", "is_active": True, "created_at": stamp, "updated_at": stamp}],
        "materials": [{"material_id": "m", "project_id": "p", "subject_id": "s", "name": "検証教材", "is_active": True, "created_at": stamp, "updated_at": stamp}],
        "units": [{**classification, "name": "検証単元", "is_active": True, "created_at": stamp, "updated_at": stamp}],
        "tags": [{"tag_id": "t", "name": "検証タグ", "is_active": True, "created_at": stamp, "updated_at": stamp}],
    })
    for path in (root / "study_logs").rglob("*.json"):
        records = json.loads(path.read_text(encoding="utf-8"))
        for row in records:
            row.update(classification)
        write_json(path, records)
    notes = [{**classification, "id": f"n{i}", "note_id": f"n{i}", "title": f"検証ノート{i}", "body": "検証本文", "content": "検証本文", "tags": [], "created_at": stamp, "updated_at": stamp,
              "linked_study_session_ids": [], "linked_flashcard_ids": [], "linked_reminder_ids": []} for i in range(8)]
    write_json(root / "notes_data" / "notebooks.json", {"notebooks": [{"id": "book", "notebook_id": "book", "name": "検証ノートブック", "sections": [{"id": "section", "section_id": "section", "name": "検証セクション", "pages": notes}]}]})
    write_json(root / "flashcards.json", [
        {**classification, "card_id": f"c{i}", "question": f"検証問題{i}", "front": f"検証問題{i}", "answer": "検証解答", "back": "検証解答", "created_at": stamp, "updated_at": stamp, "need_review": True, "tags": [], "linked_note_ids": []} for i in range(12)
    ])
    write_json(root / "reminders.json", [
        {**classification, "reminder_id": f"r{i}", "title": f"検証復習{i}", "keyword": f"検証復習{i}", "registered": ANCHOR.isoformat(), "scheduled_date": (ANCHOR + timedelta(days=i % 3)).isoformat(), "status": "pending", "created_at": stamp, "updated_at": stamp, "linked_note_ids": [], "history": []} for i in range(9)
    ])


def worker(root: Path, output: Path) -> None:
    root = root.resolve()
    if root.name != "app" or not root.parent.parent.name.startswith("proloag-page-perf-") or root.parent.parent.parent != Path(tempfile.gettempdir()).resolve():
        raise RuntimeError("Only an isolated temporary app is permitted")
    sys.path.insert(0, str(root))
    import config
    if Path(config.PROJECT_ROOT).resolve() != root:
        raise RuntimeError("Source imports escaped isolation")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    from app_core import json_store
    import prolog_ui.components as ab

    logging.disable(logging.WARNING)
    st.page_link = lambda *args, **kwargs: None
    reads: Counter[str] = Counter()
    graphs: list = []
    metrics: list = []
    original_read, original_plot, original_metric = json_store._read, st.pyplot, ab.metric_block

    def read(path):
        reads[str(Path(path).relative_to(root))] += 1
        return original_read(path)

    def plot(fig=None, *args, **kwargs):
        if fig is not None:
            graphs.append([{
                "title": axis.get_title(),
                "lines": [{"label": line.get_label(), "x": list(map(str, line.get_xdata())), "y": list(map(str, line.get_ydata()))} for line in axis.lines],
                "bars": [[patch.get_x(), patch.get_y(), patch.get_width(), patch.get_height()] for patch in axis.patches if hasattr(patch, "get_width") and hasattr(patch, "get_height")],
                "collections": [{
                    "offsets": [list(map(str, point)) for point in collection.get_offsets()],
                    "paths": [[list(map(str, point)) for point in path.vertices] for path in collection.get_paths()],
                    "values": None if collection.get_array() is None else list(map(str, collection.get_array().ravel())),
                } for collection in axis.collections],
            } for axis in fig.axes])
        return original_plot(fig, *args, **kwargs)

    def metric(label, value, caption="", **kwargs):
        metrics.append([label, str(value), caption])
        return original_metric(label, value, caption, **kwargs)

    def prohibit_write(*args, **kwargs):
        raise AssertionError("Read-only page attempted to save JSON")

    json_store._read = read
    json_store.save_json_safe = prohibit_write
    st.pyplot, ab.metric_block = plot, metric
    initial = snapshot_json(root)
    results = {}
    for prefix in PAGES:
        path = next((root / "pages").glob(f"{prefix}_*.py"))
        app = AppTest.from_file(path, default_timeout=120)
        for state in ("initial", "repeat"):
            reads.clear()
            graphs.clear()
            metrics.clear()
            started = time.perf_counter()
            app.run(timeout=120)
            seconds = time.perf_counter() - started
            if app.exception:
                raise AssertionError(f"{prefix}/{state}: {[item.message for item in app.exception]}")
            results[f"{prefix}/{state}"] = {
                "seconds": round(seconds, 3), "reads": sum(reads.values()), "master_reads": reads.get("master_data\\master.json", 0),
                "output": {
                    "tables": [json.loads(item.value.to_json(orient="split", date_format="iso", force_ascii=False, default_handler=str)) for item in app.dataframe],
                    "metrics": list(metrics), "graphs": list(graphs),
                    "selectboxes": [[item.label, item.options, str(item.value)] for item in app.selectbox],
                },
            }
            print(f"{root.parent.name} {prefix}/{state}: {seconds:.3f}s, JSON reads={sum(reads.values())}", flush=True)
            plt.close("all")
    if snapshot_json(root) != initial:
        raise AssertionError("Synthetic JSON changed")
    output.write_text(json.dumps(results, ensure_ascii=False, default=str), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, help="Pre-change source backup directory")
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker, args.output)
        return 0
    if not args.baseline or not args.baseline.is_dir():
        parser.error("--baseline must identify the pre-change source backup")
    source = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="proloag-page-perf-") as temporary:
        temp = Path(temporary)
        outputs = []
        for variant in ("before", "after"):
            root = temp / variant / "app"
            copy_sources(source, root)
            if variant == "before":
                for path in args.baseline.rglob("*.py"):
                    target = root / path.relative_to(args.baseline)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
            fixtures(root)
            output = temp / f"{variant}.json"
            environment = {**os.environ, "PYTHONPATH": os.pathsep.join([str(root), str(source / ".deps311")]), "PYTHONIOENCODING": "utf-8", "MPLCONFIGDIR": str(temp / "matplotlib"), "STREAMLIT_GLOBAL_DEVELOPMENT_MODE": "false", "STREAMLIT_LOGGER_LEVEL": "error"}
            completed = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--worker", str(root), "--output", str(output)], cwd=temp, env=environment, check=False)
            if completed.returncode:
                return completed.returncode
            outputs.append(json.loads(output.read_text(encoding="utf-8")))
        for name, before in outputs[0].items():
            after = outputs[1][name]
            if before["output"] != after["output"]:
                different = [key for key in before["output"] if before["output"][key] != after["output"][key]]
                raise AssertionError(f"{name}: output changed: {different}")
            print(f"MATCH {name}: JSON {before['reads']} -> {after['reads']}; master {before['master_reads']} -> {after['master_reads']}; time {before['seconds']} -> {after['seconds']}s", flush=True)
        print(f"PASS: {len(outputs[0])} states; tables, graph data, metrics and choices unchanged; synthetic JSON unchanged", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
