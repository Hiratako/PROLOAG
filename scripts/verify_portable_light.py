"""Verify an unused normal portable release containing only the sample master.

Usage: python -B scripts/verify_portable_light.py --target release/PACKAGE
       [--source SOURCE_ROOT] [--zip release/PACKAGE.zip]

The source directory is read only for code hashes and dependency libraries.
Personal source JSON, running servers and browsers are never accessed.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any

import verify_portable_demo as shared


REQUIRED_FILES = (
    "PROLOAG.exe", "起動する.bat", "run_portable_app.ps1", "README_FIRST.md",
    "runtime/python311/python.exe", "app/main.py", "app/requirements.txt",
    "app/.streamlit/config.toml", "app/notes_data/notebooks.json",
    "app/master_data/master.json", "app/flashcards.json", "app/reminders.json",
)
EMPTY_RECORD_FOLDERS = {
    "study_logs", "daily_plans", "diary_entries", "health_logs", "exercise_logs",
    "statistics_practice", "settings", "backups", "logs",
}
EXPECTED_JSON_FILES = {
    "notes_data/notebooks.json", "master_data/master.json", "flashcards.json", "reminders.json",
}


def expected_sample_master() -> dict[str, Any]:
    common = {
        "description": "", "is_active": True,
        "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00",
    }
    return {
        "projects": [
            {"project_id": "project_default_001", "name": "医学", **common},
            {"project_id": "project_default_002", "name": "英語", **common},
        ],
        "subjects": [
            {
                "subject_id": f"subject_default_{index:03d}",
                "project_id": "project_default_002" if index == 7 else "project_default_001",
                "name": name, **common,
            }
            for index, name in enumerate(("解剖学", "生理学", "生化学", "免疫学", "分子生物学", "疾患知識", "英語論文"), start=1)
        ],
        "materials": [], "units": [], "tags": [],
    }


def verify_light_data(app: Path) -> dict[str, str]:
    files = shared.relative_files(app)
    json_paths = {name: path for name, path in files.items() if name.lower().endswith(".json")}
    if set(json_paths) != EXPECTED_JSON_FILES:
        raise AssertionError(f"Normal release must have exactly four initial JSON files: missing={sorted(EXPECTED_JSON_FILES-set(json_paths))}, extra={sorted(set(json_paths)-EXPECTED_JSON_FILES)}")
    unwanted = [name for name in files if name.split("/", 1)[0].lower() in EMPTY_RECORD_FOLDERS]
    unwanted.extend(name for name in files if name.startswith(("notes_data/", "master_data/")) and name not in EXPECTED_JSON_FILES)
    if unwanted:
        raise AssertionError(f"Normal release contains record, settings, backup or log files: {sorted(set(unwanted))}")
    expected = {
        "notes_data/notebooks.json": {"notebooks": []},
        "master_data/master.json": expected_sample_master(),
        "flashcards.json": [], "reminders.json": [],
    }
    for name, path in json_paths.items():
        parsed = shared.strict_json(path.read_bytes(), name)
        # Canonical JSON equality distinguishes true from 1 and rejects every
        # extra key/value, including hidden notes inside the sample master.
        actual_text = json.dumps(parsed, ensure_ascii=False, sort_keys=True, allow_nan=False)
        expected_text = json.dumps(expected[name], ensure_ascii=False, sort_keys=True, allow_nan=False)
        if actual_text != expected_text:
            raise AssertionError(f"Normal release initial data differs from the exact allowed sample: {name}")
    print("PASS normal initial data: four strict JSON files; 2 sample projects / 7 subjects; records/settings/demo absent", flush=True)
    return {name: shared.digest(path) for name, path in json_paths.items()}


def verify_light_zip(archive: Path, package: Path, files: dict[str, Path], hashes: dict[str, str]) -> None:
    # The shared ZIP validator is otherwise format-independent. Scope its
    # required-file contract to this normal release, restoring it afterwards.
    original_required = shared.REQUIRED_FILES
    try:
        shared.REQUIRED_FILES = REQUIRED_FILES
        shared.verify_zip(archive, package, files, hashes)
    finally:
        shared.REQUIRED_FILES = original_required


def install_write_guard(package: Path) -> None:
    def inside(value: Any) -> bool:
        return isinstance(value, (str, bytes, os.PathLike)) and Path(os.fsdecode(value)).resolve().is_relative_to(package)

    def guard(event: str, args: tuple[Any, ...]) -> None:
        if event == "open" and args and inside(args[0]):
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else 0
            writing = isinstance(mode, str) and any(char in mode for char in "wax+")
            writing = writing or bool((flags or 0) & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if writing:
                raise AssertionError(f"Write to normal portable package is blocked: {args[0]}")
        elif event == "os.mkdir" and args and inside(args[0]):
            if not Path(os.fsdecode(args[0])).is_dir():
                raise AssertionError(f"Directory creation in portable package is blocked: {args[0]}")
        elif event in {"os.remove", "os.rmdir", "os.chmod", "os.utime"} and args and inside(args[0]):
            raise AssertionError(f"Portable package mutation is blocked: {event}")
        elif event in {"os.rename", "os.symlink", "os.link"} and any(inside(value) for value in args[:2]):
            raise AssertionError(f"Portable package mutation is blocked: {event}")
        elif event in {"subprocess.Popen", "os.system", "os.startfile", "os.startfile/2"}:
            raise AssertionError(f"Starting a process/browser during package verification is blocked: {event}")
        elif event in {"socket.connect", "socket.getaddrinfo"}:
            raise AssertionError(f"Network access during package verification is blocked: {event}")

    sys.addaudithook(guard)


def run_worker(app: Path) -> None:
    if app.name != "app" or not (app / "main.py").is_file():
        raise RuntimeError("Worker requires a packaged application directory")
    before = verify_light_data(app)
    sys.dont_write_bytecode = True
    # On Windows, Python's first platform lookup may execute the read-only
    # `ver` system query. Resolve/cache it before blocking application process
    # launches; subsequent Streamlit imports then need no child process.
    import platform
    platform.uname()
    platform.processor()
    install_write_guard(app.parent)
    sys.path.insert(0, str(app))
    import config
    if Path(config.PROJECT_ROOT).resolve() != app:
        raise RuntimeError("Application imports escaped the packaged app directory")
    from app_core import json_store
    with shared.isolated_verification_locks(json_store, app):
        _run_worker_checks(app, json_store, before)


def _run_worker_checks(app: Path, json_store: Any, before: dict[str, str]) -> None:
    original_read = json_store._read

    def guarded_read(path: Path):
        if not Path(path).resolve().is_relative_to(app):
            raise AssertionError(f"Application JSON read outside portable package is blocked: {path}")
        return original_read(path)

    def blocked_save(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Read-only package verification attempted to save data")

    json_store._read = guarded_read
    json_store.save_json_safe = blocked_save

    def unchanged() -> None:
        after = {name: shared.digest(path) for name, path in shared.relative_files(app).items() if name.lower().endswith(".json")}
        if before != after:
            raise AssertionError("Packaged JSON changed during verification")
        for name, module in tuple(sys.modules.items()):
            if name in {"config", "data_utils", "master_ui", "app_core", "prolog_ui"} or name.startswith(("app_core.", "prolog_ui.")):
                filename = getattr(module, "__file__", None)
                if filename and not Path(filename).resolve().is_relative_to(app):
                    raise AssertionError(f"Application module was imported from outside package: {name}")

    from app_core.integrity_service import run_integrity_checks
    result = run_integrity_checks()
    unchanged()
    print(f"INTEGRITY: errors={result['error_count']} notices={result['notice_count']} files={result['checked_file_count']} checks={result['total_checks']}", flush=True)
    failures = []
    if result["error_count"] or result["notice_count"]:
        failures.append("Integrity checker returned issues for initial data")
        for issue in result["issues"][:30]:
            print(json.dumps(issue, ensure_ascii=False, default=str), flush=True)

    logging.disable(logging.INFO)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    st.page_link = lambda *_args, **_kwargs: None
    pages = shared.packaged_pages(app)
    for path in pages:
        try:
            tested = AppTest.from_file(path, default_timeout=120).run(timeout=120)
            if len(tested.exception):
                raise AssertionError([str(item.message) for item in tested.exception])
            unchanged()
            print(f"PASS normal empty-data AppTest: {path.relative_to(app).as_posix()}", flush=True)
        except Exception as exc:
            failures.append(f"{path.name}: {exc}")
            print(f"FAIL normal empty-data AppTest: {path.name}: {exc}", flush=True)
        finally:
            plt.close("all")
    unchanged()
    if failures:
        raise AssertionError("; ".join(failures))
    print(f"PASS normal integrity and all {len(pages)} pages; four initial JSON files unchanged", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, help="Normal portable release folder containing app and runtime")
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1], help="Canonical application source root")
    parser.add_argument("--zip", dest="archive", type=Path, help="Optional normal-release ZIP")
    parser.add_argument("--worker-app", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker_app is not None:
        run_worker(args.worker_app.resolve())
        return 0
    if args.target is None:
        parser.error("--target is required")
    source = args.source.resolve()
    package = args.target.resolve()
    app = package / "app"
    if package == source or app.resolve() == source:
        raise ValueError("Verification must not target the working application")
    if not package.is_dir() or not app.is_dir():
        raise ValueError("Target must be a normal portable release containing app")
    files = shared.relative_files(package)
    for relative in REQUIRED_FILES:
        if relative not in files:
            raise AssertionError(f"Required normal-release file missing: {relative}")
    shared.verify_forbidden_files(files)
    for relative, path in files.items():
        if relative.lower().endswith(".json") and not relative.startswith(("runtime/", "app/")):
            shared.strict_json(path.read_bytes(), relative)
    source_hashes = shared.verify_sources(source, app)
    data_hashes = verify_light_data(app)
    dependencies = source / ".deps311"
    if not dependencies.is_dir():
        raise RuntimeError("Canonical .deps311 libraries are required for AppTest")
    with tempfile.TemporaryDirectory(prefix="proloag-portable-light-verify-") as temporary:
        workspace = Path(temporary)
        runner = workspace / "verify_light_runner.py"
        shutil.copy2(Path(__file__).resolve(), runner)
        shutil.copy2(Path(shared.__file__).resolve(), workspace / "verify_portable_demo.py")
        environment = os.environ.copy()
        environment.update({
            "PYTHONPATH": os.pathsep.join([str(app), str(dependencies)]), "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1", "MPLCONFIGDIR": str(workspace / "matplotlib"),
            "STREAMLIT_GLOBAL_DEVELOPMENT_MODE": "false", "STREAMLIT_LOGGER_LEVEL": "error",
            "STREAMLIT_SERVER_ENABLE_CORS": "true",
        })
        result = subprocess.run([sys.executable, "-B", str(runner), "--worker-app", str(app)], cwd=workspace, env=environment, check=False)
        if result.returncode:
            return result.returncode
    if verify_light_data(app) != data_hashes:
        raise AssertionError("Normal release JSON changed during validation")
    if args.archive is not None:
        hashes = {**source_hashes, **{f"app/{name}": value for name, value in data_hashes.items()}}
        verify_light_zip(args.archive.resolve(), package, files, hashes)
    print(f"PORTABLE LIGHT VERIFICATION PASSED: {package}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
