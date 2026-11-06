"""Read-only verification of a newly built consultant demo portable package.

Usage: python -B scripts/verify_portable_demo.py --target release/PACKAGE
       [--source SOURCE_ROOT] [--zip release/PACKAGE.zip]

Source comparison reads application code only, never original personal JSON.
Runtime validation imports code from the packaged app in a separate process,
using the source project's .deps311 libraries without starting any server.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
from datetime import date
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any
import zipfile


TOP_PYTHON = ("main.py", "config.py", "data_utils.py", "master_ui.py")
CODE_FOLDERS = ("app_core", "prolog_ui", "pages")
PUBLIC_SUPPORT_FILES = (
    "requirements.txt", "requirements-google.txt",
    "docs/google-sheets-setup.md", "docs/google-docs-reports.md",
    "docs/consultation-proposals.md",
)
REQUIRED_GUIDE_PREFIXES = ("95", "96")
MANIFEST_NAME = ".consultant_demo_manifest.json"
REQUIRED_FILES = (
    "PROLOAG.exe", "起動する.bat", "run_portable_app.ps1", "README_FIRST.md",
    "runtime/python311/python.exe", "app/main.py", "app/requirements.txt",
    "app/.streamlit/config.toml", "app/settings/demo_mode.json",
    f"app/{MANIFEST_NAME}",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def strict_json(raw: bytes, label: str) -> Any:
    if raw.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
        raise ValueError(f"JSON must be UTF-8 without BOM: {label}")

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key in {label}: {key}")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError(f"Non-finite JSON constant in {label}: {value}")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs, parse_constant=reject_constant)
        json.dumps(value, allow_nan=False)
    except (UnicodeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid JSON in {label}: {exc}") from exc
    return value


def files_without_links(root: Path) -> list[Path]:
    """Reject symlinks and Windows junctions before traversing any directory."""
    files = []
    pending = [root]
    while pending:
        current = pending.pop()
        metadata = current.lstat()
        if current.is_symlink() or getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f"Link/reparse point is not allowed in a portable package: {current}")
        if current.is_dir():
            pending.extend(current.iterdir())
        elif current.is_file():
            files.append(current)
    return sorted(files)


def relative_files(root: Path) -> dict[str, Path]:
    return {path.relative_to(root).as_posix(): path for path in files_without_links(root)}


def verify_sources(source: Path, app: Path) -> dict[str, str]:
    expected = {name: source / name for name in TOP_PYTHON}
    for folder in CODE_FOLDERS:
        for path in (source / folder).rglob("*"):
            if path.is_file() and path.suffix in {".py", ".css"}:
                expected[path.relative_to(source).as_posix()] = path
    expected.update({name: source / name for name in PUBLIC_SUPPORT_FILES})
    actual_python = {path.relative_to(app).as_posix() for path in app.rglob("*.py") if path.is_file()}
    expected_python = {name for name in expected if name.endswith(".py")}
    if actual_python != expected_python:
        raise AssertionError(f"Python inventory mismatch: missing={sorted(expected_python - actual_python)}, extra={sorted(actual_python - expected_python)}")
    hashes = {}
    for relative, source_path in sorted(expected.items()):
        target = app / relative
        if not target.is_file() or digest(target) != digest(source_path):
            raise AssertionError(f"Packaged source differs from current source: {relative}")
        if target.suffix == ".py":
            ast.parse(target.read_text(encoding="utf-8-sig"), filename=relative)
        hashes[f"app/{relative}"] = digest(target)
    print(f"PASS source hashes and Python syntax: {len(expected_python)} Python files; {len(hashes)} code/assets", flush=True)
    return hashes


def packaged_pages(app: Path, prefixes: tuple[str, ...] | None = None) -> list[Path]:
    """Discover current pages; source inventory equality is checked separately."""
    pages = sorted((app / "pages").glob("*.py"))
    for prefix in REQUIRED_GUIDE_PREFIXES:
        if len([path for path in pages if path.name.startswith(prefix + "_")]) != 1:
            raise AssertionError(f"Expected one packaged guide page with prefix {prefix}")
    if prefixes is None:
        if not pages:
            raise AssertionError("No packaged application pages found")
        return [app / "main.py", *pages]
    selected = [app / "main.py"]
    for prefix in prefixes:
        matches = [path for path in pages if path.name.startswith(prefix + "_")]
        if len(matches) != 1:
            raise AssertionError(f"Expected one packaged page with prefix {prefix}")
        selected.append(matches[0])
    return selected


def verify_forbidden_files(package_files: dict[str, Path]) -> None:
    violations = []
    for relative in package_files:
        parts = PurePosixPath(relative).parts
        # Runtime's standard-library test fixtures are not application secrets.
        if parts[0] == "runtime":
            continue
        lowered = [part.lower() for part in parts]
        name = lowered[-1]
        if (
            name == ".env" or name.startswith(".env.")
            or name in {"secrets.toml", "credentials.json", "token.json", "tokens.json", "id_rsa", "id_ed25519"}
            or name.startswith(("client_secret", "client-secret", "clientsecret", "service_account", "service-account"))
            or (name.startswith(("credentials", "google_credentials", "token", "google_auth")) and name.endswith((".json", ".bin")))
            or name.endswith((".pem", ".p12", ".pfx", ".key", ".dpapi", ".pyc", ".log"))
            or any(part in {".git", ".codex", ".agents", "__pycache__", "backups", "backup"} or part.startswith(".deps") for part in lowered)
        ):
            violations.append(relative)
    if violations:
        raise AssertionError(f"Forbidden local/credential/backup files in package: {violations}")
    print("PASS forbidden-file inventory (no secret file contents read)", flush=True)


def verify_demo_json(app: Path) -> dict[str, str]:
    paths = {name: path for name, path in relative_files(app).items() if name.endswith(".json")}
    parsed = {name: strict_json(path.read_bytes(), name) for name, path in paths.items()}
    hashes = {name: digest(path) for name, path in paths.items()}
    manifest = parsed.get(MANIFEST_NAME)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise AssertionError("Missing or unsupported synthetic-data manifest")
    if manifest.get("generator") != "PROLOAG consultant synthetic dataset":
        raise AssertionError("Unexpected demo dataset generator")
    generated = manifest.get("files")
    if not isinstance(generated, dict) or not generated:
        raise AssertionError("Synthetic-data manifest must list generated JSON files")
    actual_generated = {name: value for name, value in hashes.items() if name != MANIFEST_NAME}
    for name, value in generated.items():
        if not isinstance(name, str) or "\\" in name or PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts:
            raise AssertionError("Unsafe path in synthetic-data manifest")
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise AssertionError(f"Invalid manifest SHA-256: {name}")
    if generated != actual_generated:
        missing = sorted(set(generated) - set(actual_generated))
        extra = sorted(set(actual_generated) - set(generated))
        changed = sorted(name for name in set(generated) & set(actual_generated) if generated[name] != actual_generated[name])
        raise AssertionError(f"Generated-data manifest mismatch: missing={missing}, extra={extra}, changed={changed}")
    demo = parsed.get("settings/demo_mode.json")
    if not isinstance(demo, dict) or demo.get("enabled") is not True:
        raise AssertionError("The packaged application must have demo mode enabled")
    end_date = date.fromisoformat(str(manifest.get("end_date", "")))
    if demo.get("fixed_today") != end_date.isoformat():
        raise AssertionError("Demo clock does not match the generated dataset end date")
    print(f"PASS {len(paths)} strict UTF-8 JSON files and synthetic manifest; demo date={end_date}", flush=True)
    return hashes


def verify_zip(archive: Path, package: Path, package_files: dict[str, Path], app_hashes: dict[str, str]) -> None:
    with zipfile.ZipFile(archive) as zipped:
        file_infos = [item for item in zipped.infolist() if not item.is_dir()]
        mapped = {}
        seen = set()
        prefixes = [item.filename.replace("\\", "/")[:-len("app/main.py")] for item in file_infos if item.filename.replace("\\", "/").endswith("app/main.py")]
        if len(prefixes) != 1 or prefixes[0] not in {"", f"{package.name}/"}:
            raise AssertionError(f"Unexpected ZIP root layout: {prefixes}")
        prefix = prefixes[0]
        for info in file_infos:
            name = info.filename.replace("\\", "/")
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or ":" in name or not name.startswith(prefix):
                raise AssertionError(f"Unsafe ZIP member path: {name}")
            if info.flag_bits & 1:
                raise AssertionError(f"Unexpected encrypted ZIP member: {name}")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise AssertionError(f"ZIP symlink is not allowed: {name}")
            relative = name[len(prefix):]
            if relative.casefold() in seen:
                raise AssertionError(f"Duplicate/case-colliding ZIP member: {relative}")
            seen.add(relative.casefold())
            mapped[relative] = info
        if set(mapped) != set(package_files):
            raise AssertionError(f"ZIP file inventory differs from package: missing={sorted(set(package_files)-set(mapped))}, extra={sorted(set(mapped)-set(package_files))}")
        for relative in REQUIRED_FILES:
            if relative not in mapped:
                raise AssertionError(f"ZIP required file missing: {relative}")
        bad = zipped.testzip()
        if bad is not None:
            raise AssertionError(f"ZIP CRC failure: {bad}")
        for relative, info in mapped.items():
            if info.file_size != package_files[relative].stat().st_size:
                raise AssertionError(f"ZIP file size mismatch: {relative}")
        for relative, expected in app_hashes.items():
            if hashlib.sha256(zipped.read(mapped[relative])).hexdigest() != expected:
                raise AssertionError(f"ZIP application code/data hash mismatch: {relative}")
    print(f"PASS ZIP CRC, full inventory, required files and {len(app_hashes)} application hashes", flush=True)


@contextmanager
def isolated_verification_locks(store: Any, app: Path):
    """Keep real locking while placing only its lock files outside the package.

    Read-only snapshots legitimately acquire locks. Their canonical identities
    remain one-to-one, but the immutable artifact must not receive lock files.
    This does not redirect data reads, saves, journals, or arbitrary file writes.
    """
    original_transaction = store._file_transaction
    app_key = Path(store._key(app))
    with tempfile.TemporaryDirectory(prefix="proloag-verification-locks-") as temporary:
        lock_root = Path(temporary).resolve()
        if lock_root.is_relative_to(app.resolve().parent):
            raise AssertionError("Verification lock files must remain outside the package")

        @contextmanager
        def redirected_transaction(path: Path, timeout: float = 10.0):
            original_key = Path(store._key(store.resolved_json_path(Path(path))))
            if not original_key.is_relative_to(app_key):
                raise AssertionError(f"Lock target escaped packaged app: {path}")
            lock_name = hashlib.sha256(str(original_key).encode("utf-8")).hexdigest()
            with original_transaction(lock_root / lock_name, timeout):
                yield

        store._file_transaction = redirected_transaction
        try:
            yield lock_root
        finally:
            store._file_transaction = original_transaction


def run_worker(app: Path) -> None:
    if app.name != "app" or not (app / MANIFEST_NAME).is_file():
        raise RuntimeError("Worker accepts only an app directory with a synthetic-data manifest")
    sys.dont_write_bytecode = True

    def inside_package(value: Any) -> bool:
        if not isinstance(value, (str, bytes, os.PathLike)):
            return False
        return Path(os.fsdecode(value)).resolve().is_relative_to(app.parent)

    def prevent_package_writes(event: str, args: tuple[Any, ...]) -> None:
        # The service-level save guard below catches supported save paths. This
        # audit guard also blocks accidental direct filesystem writes/deletes.
        if event == "open" and args and inside_package(args[0]):
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else 0
            writing = isinstance(mode, str) and any(char in mode for char in "wax+")
            writing = writing or bool((flags or 0) & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if writing:
                raise AssertionError(f"Direct write to portable package is blocked: {args[0]}")
        elif event == "os.mkdir" and args and inside_package(args[0]):
            if not Path(os.fsdecode(args[0])).is_dir():
                raise AssertionError(f"Directory creation in portable package is blocked: {args[0]}")
        elif event in {"os.remove", "os.rmdir", "os.chmod", "os.utime"} and args and inside_package(args[0]):
            raise AssertionError(f"Filesystem mutation in portable package is blocked: {event}")
        elif event in {"os.rename", "os.symlink", "os.link"} and any(inside_package(value) for value in args[:2]):
            raise AssertionError(f"Filesystem mutation in portable package is blocked: {event}")
        elif event in {"socket.connect", "socket.getaddrinfo"}:
            raise AssertionError(f"Network access during package verification is blocked: {event}")
        elif event in {"subprocess.Popen", "os.system", "os.startfile", "os.startfile/2"}:
            raise AssertionError(f"Starting a process/browser during package verification is blocked: {event}")

    # Streamlit's first platform lookup may use a local `ver` query on Windows.
    import platform
    platform.uname()
    platform.processor()
    sys.addaudithook(prevent_package_writes)
    sys.path.insert(0, str(app))
    import config
    if Path(config.PROJECT_ROOT).resolve() != app:
        raise RuntimeError("Application imports did not resolve to packaged sources")
    from app_core import json_store
    with isolated_verification_locks(json_store, app):
        _run_worker_checks(app, json_store)


def _run_worker_checks(app: Path, json_store: Any) -> None:
    original_read = json_store._read

    def guarded_read(path: Path):
        if not Path(path).resolve().is_relative_to(app):
            raise AssertionError(f"JSON read escaped packaged app: {path}")
        return original_read(path)

    def blocked_save(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Verification attempted to save application data")

    json_store._read = guarded_read
    json_store.save_json_safe = blocked_save
    before = {name: digest(path) for name, path in relative_files(app).items() if name.endswith(".json")}

    def unchanged() -> None:
        after = {name: digest(path) for name, path in relative_files(app).items() if name.endswith(".json")}
        if before != after:
            raise AssertionError("Packaged JSON changed during read-only validation")
        for name, module in tuple(sys.modules.items()):
            if name in {"config", "data_utils", "master_ui"} or name.startswith(("app_core.", "prolog_ui.")) or name in {"app_core", "prolog_ui"}:
                filename = getattr(module, "__file__", None)
                if filename and not Path(filename).resolve().is_relative_to(app):
                    raise AssertionError(f"Application module imported outside package: {name}")

    from app_core.integrity_service import run_integrity_checks
    result = run_integrity_checks()
    unchanged()
    print(f"INTEGRITY: errors={result['error_count']} notices={result['notice_count']} files={result['checked_file_count']} checks={result['total_checks']}", flush=True)
    failures = []
    if result["error_count"] or result["notice_count"]:
        failures.append("Integrity check returned issues")
        for issue in result["issues"][:30]:
            print(json.dumps(issue, ensure_ascii=False, default=str), flush=True)

    logging.disable(logging.INFO)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    st.page_link = lambda *_args, **_kwargs: None
    # Export stays on its default local CSV view; no Google mode or send button.
    pages = packaged_pages(app)
    for path in pages:
        try:
            tested = AppTest.from_file(path, default_timeout=120).run(timeout=120)
            if len(tested.exception):
                raise AssertionError([str(item.message) for item in tested.exception])
            unchanged()
            print(f"PASS packaged AppTest: {path.relative_to(app).as_posix()}", flush=True)
        except Exception as exc:
            failures.append(f"{path.name}: {exc}")
            print(f"FAIL packaged AppTest: {path.name}: {exc}", flush=True)
        finally:
            plt.close("all")
    unchanged()
    if failures:
        raise AssertionError("; ".join(failures))
    print(f"PASS integrity and {len(pages)} packaged pages; {len(before)} JSON files unchanged", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, help="Portable package folder containing app and runtime")
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1], help="Canonical current application source")
    parser.add_argument("--zip", dest="archive", type=Path, help="Optional ZIP to verify against the package folder")
    parser.add_argument("--worker-app", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker_app is not None:
        run_worker(args.worker_app.resolve())
        return 0
    if args.target is None:
        parser.error("--target is required")
    package = args.target.resolve()
    source = args.source.resolve()
    app = package / "app"
    if app.resolve() == source or package == source:
        raise ValueError("The verification target must not be the working application")
    if not package.is_dir() or not app.is_dir():
        raise ValueError("The target must contain a packaged app directory")
    package_files = relative_files(package)
    for relative in REQUIRED_FILES:
        if relative not in package_files:
            raise AssertionError(f"Required package file missing: {relative}")
    verify_forbidden_files(package_files)
    # Root-level build manifests are allowed, but have the same strict JSON
    # requirements as the app data. Standard-library runtime fixtures are not
    # application records and are deliberately outside this JSON contract.
    for relative, path in package_files.items():
        if relative.endswith(".json") and not relative.startswith(("runtime/", "app/")):
            strict_json(path.read_bytes(), relative)
    source_hashes = verify_sources(source, app)
    json_hashes = verify_demo_json(app)
    print("NOTE: synthetic-data provenance is checked against the generated manifest; original personal JSON is never read.", flush=True)
    dependencies = source / ".deps311"
    if not dependencies.is_dir():
        raise RuntimeError("Canonical .deps311 is required for read-only packaged AppTest")
    with tempfile.TemporaryDirectory(prefix="proloag-portable-verify-") as temporary:
        workspace = Path(temporary)
        runner = workspace / "verify_runner.py"
        shutil.copy2(Path(__file__).resolve(), runner)
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
    if verify_demo_json(app) != json_hashes:
        raise AssertionError("Package JSON changed during verification")
    if args.archive is not None:
        hashes = {**source_hashes, **{f"app/{name}": value for name, value in json_hashes.items()}}
        verify_zip(args.archive.resolve(), package, package_files, hashes)
    print(f"PORTABLE DEMO VERIFICATION PASSED: {package}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
