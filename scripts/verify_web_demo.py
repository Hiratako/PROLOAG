"""Run Web experience checks and record their actual local verification scope."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fingerprint_digest(records: dict[str, str]) -> str:
    encoded = json.dumps(records, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _problem_list(items):
    return [{"test": str(test), "message": details.strip().splitlines()[-1]}
            for test, details in items]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "WEB_DEMO_VERIFICATION.json",
                        help="Result JSON path; bundled records cannot be an output target")
    parser.add_argument("--verbosity", type=int, choices=(0, 1, 2), default=1)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    # The test command must not become a way to overwrite its own fixtures.
    guarded_folders = ("study_logs", "daily_plans", "notes_data", "master_data",
                       "diary_entries", "health_logs", "exercise_logs", "settings",
                       "statistics_practice", "backups")
    guarded_files = {ROOT / "flashcards.json", ROOT / "reminders.json",
                     ROOT / "WEB_DEMO_DATA_MANIFEST.json",
                     ROOT / "UPSTREAM_SOURCE_MANIFEST.json", ROOT / "WEB_DEMO_MANIFEST.json",
                     ROOT / "WEB_DEMO_PACKAGE_MANIFEST.json",
                     ROOT / "web_demo" / "dataset_manifest.json"}
    if (output.suffix.lower() != ".json" or output in guarded_files or
            any(output.is_relative_to((ROOT / name).resolve())
                for name in guarded_folders)):
        parser.error("The verification report cannot overwrite exhibition records.")

    os.environ.setdefault("STREAMLIT_GLOBAL_DEVELOPMENT_MODE", "false")
    started = time.monotonic()
    report = {
        "schema_version": 1,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": {
            "kind": "local_web_experience_verification",
            "platform": platform.system(),
            "python": platform.python_version(),
            "linux_runtime": sys.platform.startswith("linux"),
            "community_cloud_deployed": False,
            "browser_navigation": "not included in this command",
            "test_pattern": "test_web_*.py",
        },
        "status": "failed",
        "data_unchanged": False,
    }
    exit_code = 2
    try:
        from config import WEB_DEMO_READ_ONLY
        from web_demo.catalog import PRODUCT_VIEWS
        from web_demo.data import data_fingerprint, verify_demo_dataset

        if WEB_DEMO_READ_ONLY is not True:
            raise ValueError("The shared demonstration fixtures must remain write protected.")
        report["dataset"] = verify_demo_dataset()
        report["experience"] = {
            "product_pages": len(PRODUCT_VIEWS),
            "hobby_pages_excluded": not any(
                name.startswith(("20_", "21_", "22_"))
                for name in PRODUCT_VIEWS.values()),
            "visitor_storage": "private disposable sandbox",
        }
        report["streamlit"] = importlib.metadata.version("streamlit")
        before = data_fingerprint()
        report["record_hash_before"] = _fingerprint_digest(before)
        suite = unittest.defaultTestLoader.discover(
            start_dir=str(ROOT / "tests"), pattern="test_web_*.py",
            top_level_dir=str(ROOT))
        count = suite.countTestCases()
        if count == 0:
            raise ValueError("No exhibition tests were discovered.")
        result = unittest.TextTestRunner(verbosity=args.verbosity).run(suite)
        after = data_fingerprint()
        report["record_hash_after"] = _fingerprint_digest(after)
        report["data_unchanged"] = before == after
        report["tests"] = {
            "run": result.testsRun,
            "failures": _problem_list(result.failures),
            "errors": _problem_list(result.errors),
            "skipped": [{"test": str(test), "reason": reason}
                        for test, reason in result.skipped],
        }
        passed = result.wasSuccessful() and report["data_unchanged"]
        report["status"] = "passed" if passed else "failed"
        exit_code = 0 if passed else 1
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}

    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    print(json.dumps({"status": report["status"],
                      "tests_run": report.get("tests", {}).get("run", 0),
                      "data_unchanged": report["data_unchanged"],
                      "cloud_deployment_checked": False}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
