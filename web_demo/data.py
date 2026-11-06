"""Read-only access to the fixed, synthetic exhibition records.

The internal cache is never returned directly: callers receive private copies,
so changing a filter or derived row cannot alter another visitor's inputs.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any

from app_core.study_record_reader import unpack_study_document
from web_demo.substances import WEB_SUBSTANCE_VARIANT


DATA_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_DATE = date(2026, 11, 5)
PERIOD_START = REFERENCE_DATE - timedelta(days=179)
DATA_MANIFEST = DATA_ROOT / "web_demo/dataset_manifest.json"
DATA_FOLDERS = frozenset({
    "study_logs", "daily_plans", "notes_data", "master_data", "diary_entries",
    "health_logs", "exercise_logs", "settings", "statistics_practice",
})
DATA_FILES = frozenset({"flashcards.json", "reminders.json"})
# This archive is the base, not a claim that the derived Web fixture remains
# byte-identical to the Windows Demo. Its two-substance overlay is explicit.
SOURCE_ARCHIVE_SHA256 = "678d4c4850bdb3458d69d5431faf05bae1089312f08699556253c45a62c2202c"
BASE_RECORD_SET_SHA256 = "ee55dc85e14324470a2eba05fc518a425d01fcbfb7517cdd20d19f1805e36bcf"


def _read(relative: str, default: Any) -> Any:
    path = DATA_ROOT / relative
    if not path.is_file():
        return deepcopy(default)
    return json.loads(path.read_text(encoding="utf-8-sig"))


@lru_cache(maxsize=1)
def _bundle() -> dict[str, Any]:
    sessions = []
    for path in sorted((DATA_ROOT / "study_logs").rglob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8-sig"))
        sessions.extend(dict(record) for _, record in unpack_study_document(document).records)
    notebooks = _read("notes_data/notebooks.json", {"notebooks": []})
    notes = [page for notebook in notebooks.get("notebooks", [])
             for section in notebook.get("sections", []) for page in section.get("pages", [])]
    health = [json.loads(path.read_text(encoding="utf-8-sig"))
              for path in sorted((DATA_ROOT / "health_logs").rglob("*.json"))]
    return {
        "sessions": sessions,
        "master": _read("master_data/master.json", {}),
        "notebooks": notebooks,
        "notes": notes,
        "cards": _read("flashcards.json", []),
        "reminders": _read("reminders.json", []),
        "health": health,
        "roadmap": _read("settings/study_roadmap.json", {}),
    }


def demo_snapshot() -> dict[str, Any]:
    """Return independent record values, never a shared mutable cache value."""
    return deepcopy(_bundle())


def demo_summary() -> dict[str, int | float]:
    data = _bundle()
    seconds = sum(max(float(row.get("duration_sec", 0) or 0), 0) for row in data["sessions"])
    return {
        "study_hours": round(seconds / 3600, 1),
        "sessions": len(data["sessions"]),
        "notes": len(data["notes"]),
        "cards": len(data["cards"]),
        "subjects": sum(row.get("is_active", True) is not False for row in data["master"].get("subjects", [])),
        "days": (REFERENCE_DATE - PERIOD_START).days + 1,
    }


def _checked_records(root: Path, manifest: dict[str, Any]) -> dict[str, Path]:
    records = manifest.get("record_sha256")
    if not isinstance(records, dict) or not records:
        raise ValueError("The exhibition record manifest is empty or invalid.")
    result = {}
    root = root.resolve(strict=True)
    for relative, digest in records.items():
        if (not isinstance(relative, str) or "\\" in relative or ":" in relative
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError("Invalid exhibition record or hash.")
        parts = PurePosixPath(relative).parts
        if (not parts or PurePosixPath(relative).is_absolute() or any(p in {".", ".."} for p in parts)
                or str(PurePosixPath(relative)) != relative
                or PurePosixPath(relative).suffix != ".json"
                or (parts[0] not in DATA_FOLDERS and relative not in DATA_FILES)):
            raise ValueError("Unexpected record path in the exhibition manifest.")
        # Canonical relative components contain no traversal. The filesystem
        # inventory below checks every encountered file/folder before use.
        result[relative] = root.joinpath(*parts)
    actual: set[str] = set()

    def reject_link(path: Path) -> None:
        if path.is_symlink():
            raise ValueError("Linked exhibition records are not allowed.")
        # Windows junctions are reparse points, not necessarily symlinks.
        if getattr(path.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise ValueError("Linked exhibition records are not allowed.")

    def visit(folder: Path) -> None:
        reject_link(folder)
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.is_symlink():
                    raise ValueError("Linked exhibition records are not allowed.")
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    visit(path)
                elif entry.is_file(follow_symlinks=False):
                    actual.add(path.relative_to(root).as_posix())
                else:
                    raise ValueError("Unexpected filesystem item in the exhibition data.")

    for folder in DATA_FOLDERS:
        directory = root / folder
        if directory.is_symlink():
            raise ValueError("Linked exhibition folders are not allowed.")
        if directory.exists():
            visit(directory)
    for name in DATA_FILES:
        path = root / name
        if path.exists() or path.is_symlink():
            reject_link(path)
            if not path.is_file():
                raise ValueError("Unexpected filesystem item in the exhibition data.")
            actual.add(name)
    if actual != set(result):
        raise ValueError("Unknown, extra or missing exhibition records were detected.")
    return result


def data_fingerprint() -> dict[str, str]:
    """Hash the allowlisted records for verification; performs no writes."""
    manifest = json.loads(DATA_MANIFEST.read_text(encoding="utf-8"))
    records = _checked_records(DATA_ROOT, manifest)
    return {relative: hashlib.sha256(path.read_bytes()).hexdigest() for relative, path in records.items()}


def verify_demo_dataset() -> dict[str, Any]:
    manifest = json.loads(DATA_MANIFEST.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("The exhibition manifest must be an object.")
    if (manifest.get("synthetic") is not True or manifest.get("reference_date") != REFERENCE_DATE.isoformat()
            or manifest.get("period_start") != PERIOD_START.isoformat() or manifest.get("days") != 180
            or manifest.get("seed") != 20261105 or manifest.get("schema_version") != 2
            or manifest.get("variant") != WEB_SUBSTANCE_VARIANT
            or manifest.get("source_kind") != "derived_synthetic_web_variant"
            or manifest.get("base_source_sha256") != SOURCE_ARCHIVE_SHA256
            or manifest.get("base_record_set_sha256") != BASE_RECORD_SET_SHA256):
        raise ValueError("The exhibition data metadata is inconsistent.")
    base = manifest.get("base_record_sha256")
    if (not isinstance(base, dict) or len(base) != 715
            or set(base) != set(manifest.get("record_sha256", {}))
            or hashlib.sha256(json.dumps(base, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest() != BASE_RECORD_SET_SHA256):
        raise ValueError("The derived exhibition data provenance is inconsistent.")
    if data_fingerprint() != manifest["record_sha256"]:
        raise ValueError("The fixed exhibition data has changed.")
    mode = _read("settings/demo_mode.json", {})
    if mode.get("enabled") is not True or mode.get("fixed_today") != REFERENCE_DATE.isoformat():
        raise ValueError("The exhibition reference date is inconsistent.")
    if len(_bundle()["health"]) != 180 or not _bundle()["roadmap"].get("items"):
        raise ValueError("The exhibition records are incomplete.")
    return {"records": len(manifest["record_sha256"]), "days": 180, "synthetic": True,
            "reference_date": REFERENCE_DATE.isoformat(), "variant": WEB_SUBSTANCE_VARIANT}


def verify_demo_data() -> dict[str, Any]:
    """Backward-compatible name used by the source verification tests."""
    return verify_demo_dataset()
