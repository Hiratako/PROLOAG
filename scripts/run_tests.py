"""Run the existing unittest suite from a clean source checkout.

Core excludes only the four modules that require external media/OCR tools.
The exclusion list is printed explicitly; unittest skips and errors stay visible.
Unknown record JSON is rejected by name. Only the four documented empty
scaffold JSON files may be read to verify their exact, empty initial values.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import stat
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
INTEGRATION_MODULES = {
    "test_media_conversion.py": "FFmpeg/FFprobe with required encoders",
    "test_media_conversion_ui.py": "FFmpeg/FFprobe media fixtures",
    "test_transcription_process.py": "FFmpeg/FFprobe; recognition uses a test double",
    "test_ocr_process.py": "optional OCR dependencies installed by app_core/setup_ocr.py",
}
RECORD_DIRECTORIES = (
    "study_logs", "daily_plans", "diary_entries", "health_logs", "exercise_logs",
    "statistics_practice", "notes_data", "master_data", "settings",
    "transcription_data", "ocr_data",
)
EMPTY_SCAFFOLD = {
    "notes_data/notebooks.json": {"notebooks": []},
    "master_data/master.json": {"projects": [], "subjects": [], "materials": [], "units": [], "tags": []},
    "flashcards.json": [],
    "reminders.json": [],
}


def existing_record_paths(root: Path) -> list[str]:
    """Reject unknown records before reading the four permitted empty files."""
    def linked(path: Path) -> bool:
        return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))

    found, candidates = [], set()
    for name in RECORD_DIRECTORIES:
        folder = root / name
        if folder.is_symlink() or folder.exists() and (linked(folder) or not folder.is_dir()):
            found.append(name)
            continue
        if not folder.is_dir():
            continue
        pending = [folder]
        while pending:
            for path in pending.pop().iterdir():
                relative = path.relative_to(root).as_posix()
                if linked(path):
                    found.append(relative + " (link)")
                elif path.is_dir():
                    pending.append(path)
                elif path.suffix.lower() == ".json":
                    if relative in EMPTY_SCAFFOLD:
                        candidates.add(relative)
                    else:
                        found.append(relative)
    for name in ("flashcards.json", "reminders.json"):
        path = root / name
        if path.is_symlink() or path.exists():
            if linked(path) or not path.is_file():
                found.append(name + " (link/non-file)")
            else:
                candidates.add(name)
    if found:
        return sorted(set(found))

    def unique_pairs(pairs):
        values = {}
        for key, value in pairs:
            if key in values:
                raise ValueError("Duplicate scaffold key")
            values[key] = value
        return values

    for name in candidates:
        path = root / name
        try:
            if path.stat().st_size > 4096:
                found.append(name + " (not an empty scaffold)")
                continue
            value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs)
            if value != EMPTY_SCAFFOLD[name]:
                found.append(name + " (not an empty scaffold)")
        except (OSError, UnicodeError, ValueError):
            found.append(name + " (invalid scaffold)")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("core", "integration", "all"), default="core")
    parser.add_argument("--verbosity", choices=(0, 1, 2), type=int, default=1)
    args = parser.parse_args(argv)

    if not TESTS.is_dir() or not (TESTS / "__init__.py").is_file():
        parser.error("Run from the complete public source package containing tests/__init__.py.")
    present = existing_record_paths(ROOT)
    if present:
        print("Refusing to test a checkout containing application records/settings.", file=sys.stderr)
        print("Use a fresh source checkout or the four exact empty initialization files.", file=sys.stderr)
        print("Unknown record JSON was not opened; existing user values are never printed.", file=sys.stderr)
        for name in present:
            print(f"  {name}", file=sys.stderr)
        return 2

    paths = sorted(TESTS.glob("test_*.py"))
    if not paths:
        parser.error("No test_*.py modules found.")
    if args.suite == "core":
        selected = [path for path in paths if path.name not in INTEGRATION_MODULES]
        print("Core suite: external-tool modules are excluded explicitly:", flush=True)
        for name, reason in INTEGRATION_MODULES.items():
            print(f"  {name}: {reason}", flush=True)
    elif args.suite == "integration":
        selected = [path for path in paths if path.name in INTEGRATION_MODULES]
    else:
        selected = paths
    if args.suite != "core":
        print("External tools must be prepared before this suite; missing prerequisites remain errors.", flush=True)
    missing = sorted(set(INTEGRATION_MODULES) - {path.name for path in paths})
    if missing:
        parser.error("Incomplete source test inventory: " + ", ".join(missing))

    # Existing tests use both sibling imports and tests.<fixture> imports.
    # Keep those conventions and do not rewrite product code or test fixtures.
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(TESTS))
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName(path.stem) for path in selected)
    print(f"Selected {len(selected)} test modules for {args.suite}; {suite.countTestCases()} test cases loaded.", flush=True)
    result = unittest.TextTestRunner(verbosity=args.verbosity, stream=sys.stdout).run(suite)
    print(f"SUMMARY suite={args.suite} run={result.testsRun} failures={len(result.failures)} errors={len(result.errors)} skipped={len(result.skipped)}", flush=True)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
