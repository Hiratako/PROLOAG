"""Derive the Web-only caffeine/acetaminophen fixture from the known Demo.

This does not change the normal Demo generator, a visitor's private store, or
the local application's records. Only an exact, known synthetic Web seed (or
this script's already-derived output) is accepted. No files are deleted.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
from typing import Any


SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from web_demo.substances import (  # noqa: E402
    LEGACY_DEMO_SUBSTANCE_IDS, WEB_SUBSTANCE_VARIANT,
    default_web_substance_profiles,
)


BASE_ARCHIVE = "proloag-demo-consultant-v0.1.0-preview.1-windows-x64.zip"
BASE_ARCHIVE_SHA256 = "678d4c4850bdb3458d69d5431faf05bae1089312f08699556253c45a62c2202c"
BASE_RECORD_SET_SHA256 = "ee55dc85e14324470a2eba05fc518a425d01fcbfb7517cdd20d19f1805e36bcf"
NON_SUBSTANCE_SHA256 = "88a3f3d8734f41ba65784a927bf70121d379adb010b93a708beba12474aa9609"
CAFFEINE_ROWS_SHA256 = "cea0f414392271335425cd2effbadb14e7cbf9c93df0638fbd5d55cfe71cab2f"
MANIFEST_PATH = "web_demo/dataset_manifest.json"
OVERLAY_SOURCE = "web_demo_substance_overlay"
_DATA_FOLDERS = frozenset({
    "study_logs", "daily_plans", "notes_data", "master_data", "diary_entries",
    "health_logs", "exercise_logs", "settings", "statistics_practice",
})
_DATA_FILES = frozenset({"flashcards.json", "reminders.json"})


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _not_link(path: Path) -> None:
    if path.is_symlink() or (path.exists() and getattr(path.lstat(), "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
        raise ValueError("Linked folders/files are not Web fixture targets.")


def _record_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or "\\" in relative or ":" in relative:
        raise ValueError("Invalid Web fixture path.")
    path = PurePosixPath(relative)
    if (path.is_absolute() or not path.parts or any(p in {".", ".."} for p in path.parts)
            or path.as_posix() != relative or path.suffix != ".json"
            or (path.parts[0] not in _DATA_FOLDERS and relative not in _DATA_FILES)):
        raise ValueError("Unexpected Web fixture path.")
    candidate = root.joinpath(*path.parts)
    cursor = root
    for part in path.parts:
        cursor /= part
        _not_link(cursor)
    if not candidate.resolve().is_relative_to(root):
        raise ValueError("The Web fixture path escaped its target.")
    return candidate


def non_substance_fingerprint(documents: dict[str, Any]) -> str:
    values = {}
    for relative, value in documents.items():
        if relative == "settings/substance_profiles.json":
            continue
        value = deepcopy(value)
        if relative.startswith("diary_entries/"):
            value.pop("substance_records", None)
        values[relative] = value
    return _digest(values)


def caffeine_rows_fingerprint(documents: dict[str, Any]) -> str:
    return _digest({relative: [row for row in value.get("substance_records", [])
                               if row.get("substance_id") == "caffeine"]
                    for relative, value in documents.items()
                    if relative.startswith("diary_entries/")})


def _load_target(root: Path) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, str]]:
    root = Path(os.path.abspath(root))
    if any("onedrive" in part.casefold() for part in root.parts):
        raise ValueError("Use a separate Web source or OS temporary folder, not OneDrive.")
    for path in (root, *root.parents):
        _not_link(path)
    root = root.resolve(strict=True)
    manifest_path = root / MANIFEST_PATH
    _not_link(manifest_path.parent)
    _not_link(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or manifest.get("synthetic") is not True
            or manifest.get("reference_date") != "2026-11-05"
            or manifest.get("period_start") != "2026-05-10"
            or manifest.get("days") != 180 or manifest.get("seed") != 20261105):
        raise ValueError("Only the known synthetic Web fixture is accepted.")
    derived = manifest.get("variant") == WEB_SUBSTANCE_VARIANT
    if derived:
        if (manifest.get("source_kind") != "derived_synthetic_web_variant"
                or manifest.get("base_source_archive") != BASE_ARCHIVE
                or manifest.get("base_source_sha256") != BASE_ARCHIVE_SHA256):
            raise ValueError("Invalid derived Web fixture provenance.")
        base_hashes = manifest.get("base_record_sha256")
    else:
        if (manifest.get("variant") is not None or manifest.get("source_archive") != BASE_ARCHIVE
                or manifest.get("source_sha256") != BASE_ARCHIVE_SHA256):
            raise ValueError("The input does not identify the original synthetic Demo.")
        base_hashes = manifest.get("record_sha256")
    if (not isinstance(base_hashes, dict) or len(base_hashes) != 715
            or _digest(base_hashes) != BASE_RECORD_SET_SHA256):
        raise ValueError("The base records do not match the independently regenerated Demo.")
    hashes = manifest.get("record_sha256")
    if not isinstance(hashes, dict) or set(hashes) != set(base_hashes):
        raise ValueError("Unexpected, missing or additional Web fixture records.")
    actual = set()
    for name in _DATA_FOLDERS:
        folder = root / name
        _not_link(folder)
        if not folder.exists():
            continue
        for directory, names, files in os.walk(folder, followlinks=False):
            directory = Path(directory)
            _not_link(directory)
            for name in names:
                _not_link(directory / name)
            for name in files:
                path = directory / name
                _not_link(path)
                actual.add(path.relative_to(root).as_posix())
    actual.update(name for name in _DATA_FILES if (root / name).exists())
    if actual != set(hashes):
        raise ValueError("Unknown or missing files were found in the synthetic records.")
    documents = {}
    for relative, expected in hashes.items():
        raw = _record_path(root, relative).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("The target has modified records; visitor/personal stores are not supported.")
        documents[relative] = json.loads(raw.decode("utf-8-sig"))
    if (non_substance_fingerprint(documents) != NON_SUBSTANCE_SHA256
            or caffeine_rows_fingerprint(documents) != CAFFEINE_ROWS_SHA256):
        raise ValueError("The input contains changed non-substance or caffeine data.")
    return root, manifest, documents, base_hashes


def acetaminophen_demo_dates(dates: list[str], *, reference_date: str = "2026-11-05") -> frozenset[str]:
    """Sparse independent display samples, not a daily schedule or old-log relabel."""
    months: dict[str, list[str]] = {}
    for value in sorted(set(dates)):
        date.fromisoformat(value)
        months.setdefault(value[:7], []).append(value)
    selected = set()
    reference = date.fromisoformat(reference_date)
    for month, available in sorted(months.items()):
        if month == reference_date[:7] and reference.day <= 15:
            selected.add(reference_date if reference_date in available else available[-1])
            continue
        for half in ([value for value in available if int(value[8:10]) <= 15],
                     [value for value in available if int(value[8:10]) > 15]):
            if half:
                selected.add(min(half, key=lambda value: hashlib.sha256(
                    ("web-acetaminophen-v1:" + value).encode()).digest()))
    return frozenset(selected)


def _acetaminophen_record(day: str, profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "substance_record_id": "acetaminophen_web_demo_" + day.replace("-", "") + "_01",
        "substance_id": "acetaminophen",
        "taken_at": day + "T14:00:00",
        "amount_mg": 300.0,
        "unit": "mg",
        "source": OVERLAY_SOURCE,
        "label": "架空サンプル（錠剤）",
        "memo": "Web体験版の表示用に新規作成した架空記録です。実際の服用や推奨を示すものではありません。",
        "model_snapshot": {
            "schema_version": 1,
            "profile_revision": profile["model_revision"],
            "unit": "mg",
            "half_life_hours": profile["half_life_hours"],
            "peak_delay_minutes": profile["peak_delay_minutes"],
            "residual_cutoff_hours": profile["residual_cutoff_hours"],
        },
        "created_at": day + " 14:00:00",
        "updated_at": day + " 14:00:00",
    }


def prepare_web_substance_demo(root: Path) -> dict[str, Any]:
    root, manifest, documents, base_hashes = _load_target(root)
    profiles = default_web_substance_profiles()
    by_id = {row["substance_id"]: row for row in profiles}
    if set(by_id) != {"caffeine", "acetaminophen"}:
        raise ValueError("The Web fixture must have exactly two default substances.")
    before = deepcopy(documents)
    old_caffeine = next((row for row in documents["settings/substance_profiles.json"]["profiles"]
                         if row.get("substance_id") == "caffeine"), None)
    if old_caffeine != by_id["caffeine"]:
        raise ValueError("This transformation must not change the caffeine profile.")
    diary_paths = [relative for relative in documents if relative.startswith("diary_entries/")]
    selected = acetaminophen_demo_dates([documents[relative]["date"] for relative in diary_paths])
    removed = 0
    for relative in diary_paths:
        document = documents[relative]
        records = document.get("substance_records", [])
        removed += sum(row.get("substance_id") in LEGACY_DEMO_SUBSTANCE_IDS for row in records)
        keep = [row for row in records if row.get("substance_id") not in LEGACY_DEMO_SUBSTANCE_IDS
                and not (row.get("substance_id") == "acetaminophen" and row.get("source") == OVERLAY_SOURCE)]
        if document["date"] in selected:
            keep.append(_acetaminophen_record(document["date"], by_id["acetaminophen"]))
        document["substance_records"] = keep
    documents["settings/substance_profiles.json"] = {"schema_version": 1, "profiles": profiles}
    if (non_substance_fingerprint(documents) != NON_SUBSTANCE_SHA256
            or caffeine_rows_fingerprint(documents) != CAFFEINE_ROWS_SHA256):
        raise ValueError("The proposed overlay changed unrelated fixture data.")
    planned: dict[str, bytes] = {}
    for relative, document in documents.items():
        if document != before[relative]:
            planned[relative] = (json.dumps(document, ensure_ascii=False, indent=2,
                                            allow_nan=False) + "\n").encode("utf-8")
    hashes = {relative: hashlib.sha256(planned.get(relative, _record_path(root, relative).read_bytes())).hexdigest()
              for relative in sorted(documents)}
    result_manifest = deepcopy(manifest)
    result_manifest.pop("source_archive", None)
    result_manifest.pop("source_sha256", None)
    result_manifest.update({
        "schema_version": 2,
        "variant": WEB_SUBSTANCE_VARIANT,
        "source_kind": "derived_synthetic_web_variant",
        "base_source_archive": BASE_ARCHIVE,
        "base_source_sha256": BASE_ARCHIVE_SHA256,
        "base_record_set_sha256": BASE_RECORD_SET_SHA256,
        "base_record_sha256": base_hashes,
        "non_substance_sha256": NON_SUBSTANCE_SHA256,
        "caffeine_substance_rows_sha256": CAFFEINE_ROWS_SHA256,
        "transformation": {
            "script": "scripts/prepare_web_substance_demo.py",
            "version": 1,
            "removed_legacy_ids": sorted(LEGACY_DEMO_SUBSTANCE_IDS),
            "default_substance_ids": [row["substance_id"] for row in profiles],
            "new_acetaminophen_records": len(selected),
            "new_records_are_independent_samples": True,
            "model_note": "表示用の単純モデル。健康成人での錠剤データを丸めた参考値で、個人の血中濃度・効果を予測しません。",
            "model_sources": [
                "https://www.pmda.go.jp/PmdaSearch/iyakuDetail/ResultDataSetPDF/172190_1141007F1063_5_07",
                "https://www.tylenol.jp/products-tylenol",
            ],
        },
        "record_sha256": hashes,
    })
    # All checks and proposed bytes are complete before any authorized seed
    # file is overwritten. Unchanged records keep their original bytes.
    for relative, raw in planned.items():
        _record_path(root, relative).write_bytes(raw)
    manifest_changed = result_manifest != manifest
    if manifest_changed:
        (root / MANIFEST_PATH).write_text(json.dumps(result_manifest, ensure_ascii=False,
            indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n")
    return {
        "variant": WEB_SUBSTANCE_VARIANT,
        "records": len(hashes),
        "removed_legacy_records_this_run": removed,
        "acetaminophen_records": len(selected),
        "acetaminophen_dates": sorted(selected),
        "changed_files": sorted([*planned, *([MANIFEST_PATH] if manifest_changed else [])]),
        "record_set_sha256": _digest(hashes),
        "non_substance_sha256": non_substance_fingerprint(documents),
        "caffeine_substance_rows_sha256": caffeine_rows_fingerprint(documents),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-root", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare_web_substance_demo(args.target_root), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
