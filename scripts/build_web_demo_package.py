"""Build the read-only source package. No application data is generated here."""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {"__pycache__", ".git", ".venv", ".pytest_cache", ".ruff_cache"}
EXCLUDED_NAMES = {"WEB_DEMO_PACKAGE_MANIFEST.json"}
SOURCE_FOLDERS = {"app_core", "prolog_ui", "reference_pages", "docs", "licenses", "scripts", "tests", "web_demo"}
ROOT_FILES = {"main.py", "config.py", "data_utils.py", "master_ui.py", ".gitignore", "README.md", "WEB_DEMO_README.md", "LICENSE_STATUS.md", "THIRD_PARTY_NOTICES.md", "requirements.txt", "requirements-google.txt", "requirements-ocr.txt", "requirements-transcription.txt", "requirements-release.lock.txt", "run_app.ps1", "run_app.bat", "UPSTREAM_SOURCE_MANIFEST.json", "WEB_DEMO_VERIFICATION.json"}


def inventory():
    files = []
    synthetic = set(json.loads((ROOT / "web_demo/dataset_manifest.json").read_text(encoding="utf-8"))["record_sha256"])
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if any(part in EXCLUDED_PARTS or part.startswith(".deps") for part in relative.parts):
            continue
        if path.suffix in {".pyc", ".pyo", ".log", ".zip"} or path.name in EXCLUDED_NAMES:
            continue
        if path.is_symlink():
            raise ValueError("Linked files must not be packaged.")
        name = relative.as_posix()
        if (path.name.startswith(".env") or re.search(r"(?:credentials?|client.?secret|service.?account|token).*\.json$", path.name, re.I)
                or path.suffix.lower() in {".pem", ".pfx", ".p12", ".key", ".dpapi"}):
            raise ValueError("Private configuration must not be packaged: " + name)
        if not (name in synthetic or name in ROOT_FILES or name == ".streamlit/config.toml" or relative.parts[0] in SOURCE_FOLDERS):
            raise ValueError("Unexpected file outside the source/data allowlist: " + name)
        if path.suffix == ".py":
            ast.parse(path.read_text(encoding="utf-8-sig"), filename=relative.as_posix())
        files.append(path)
    return files


def build(output: Path):
    from web_demo.data import verify_demo_dataset
    verify_demo_dataset()
    check = json.loads((ROOT / "WEB_DEMO_VERIFICATION.json").read_text(encoding="utf-8"))
    dependencies = json.loads((ROOT / "docs/linux-dependency-verification.json").read_text(encoding="utf-8"))
    if check.get("status") != "passed" or check.get("data_unchanged") is not True:
        raise ValueError("Run the exhibition verification before building.")
    if (dependencies.get("status") != "passed" or dependencies.get("requirements_sha256") != hashlib.sha256((ROOT / "requirements.txt").read_bytes()).hexdigest()):
        raise ValueError("Dependency verification is missing or stale.")
    if output.exists():
        raise FileExistsError("Choose a new package path; existing archives are not replaced.")
    if output.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("Save the archive outside the source folder.")
    files = inventory()
    manifest = {
        "kind": "proloag_readonly_web_exhibition_source",
        "reference_date": "2026-11-05",
        "license_status": "unselected_prepublication_preparation",
        "linux_runtime_verified": False,
        "community_cloud_deployed": False,
        "entrypoint": "main.py",
        "upstream_source_commit": "66511380cf03a0c9b35b7fd7e5ca9baafebf565f",
        "file_sha256": {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
    }
    (ROOT / "WEB_DEMO_PACKAGE_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    files.append(ROOT / "WEB_DEMO_PACKAGE_MANIFEST.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, arcname="proloag_web_demo/" + path.relative_to(ROOT).as_posix())
    with zipfile.ZipFile(output) as archive:
        bad = archive.testzip()
        if bad:
            raise ValueError("Archive CRC mismatch: " + bad)
        for relative, digest in manifest["file_sha256"].items():
            if hashlib.sha256(archive.read("proloag_web_demo/" + relative)).hexdigest() != digest:
                raise ValueError("Archive file hash mismatch: " + relative)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_name(output.name + ".sha256").write_text(digest + "  " + output.name + "\n", encoding="ascii")
    return {"files": len(files), "bytes": output.stat().st_size, "sha256": digest, "zip": str(output)}


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(ROOT))
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    options = parser.parse_args()
    print(json.dumps(build(options.output), ensure_ascii=True))
