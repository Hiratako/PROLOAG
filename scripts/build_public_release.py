"""Build unused Windows packages from the public source checkout.

No application records, development dependencies, models, or credentials are
copied from the checkout. Existing releases are never overwritten. This tool
does not publish anything to GitHub. Python 3.11 x64 and Windows are required
for the actual runtime/launcher build; --check performs a read-only preflight.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import zipfile

SOURCE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VERSION = "0.1.0-preview.1"
DEFAULT_REFERENCE_DATE = date(2026, 11, 5)
PUBLIC_GUIDES = (
    "study-roadmap.md", "legacy-study-import.md", "media-conversion.md",
    "transcription.md", "ocr.md", "google-sheets-setup.md",
    "google-docs-reports.md", "consultation-proposals.md",
    "roadmap-ai-import.md", "ai-roadmap-import-prompt.md",
)
COMMON_DISTRIBUTION_DOCS = (
    "README_FIRST.md", "RELEASE_NOTES.md", "UPDATE_GUIDE.md",
)
REQUIRED_SOURCE = (
    "main.py", "config.py", "data_utils.py", "master_ui.py",
    "requirements.txt", "requirements-release.lock.txt",
    "THIRD_PARTY_NOTICES.md", ".streamlit/config.toml",
    "scripts/build_portable_release.ps1",
    "scripts/generate_consultant_demo_dataset.py",
    "scripts/add_demo_roadmap.py", "scripts/verify_portable_demo.py",
    "scripts/verify_portable_light.py",
    "scripts/portable_release/PROLOAGLauncher.cs",
    "scripts/portable_release/start_app.bat",
    "scripts/portable_release/run_portable_app.ps1",
)
NORMAL_JSON = {
    "notes_data/notebooks.json", "master_data/master.json",
    "flashcards.json", "reminders.json",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def no_links(path: Path) -> None:
    """Check existing ancestors without following a reparse point."""
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or candidate.exists() and getattr(candidate.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f"Links/reparse points are not accepted: {candidate}")


def safe_files(root: Path) -> list[Path]:
    no_links(root)
    result, pending = [], [root]
    while pending:
        item = pending.pop()
        no_links(item)
        if item.is_dir():
            pending.extend(item.iterdir())
        elif item.is_file():
            result.append(item)
    return sorted(result)


def release_names(version: str, variant: str) -> dict[str, str]:
    if len(version) > 80 or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?", version):
        raise ValueError("Use a safe version such as 0.1.0-preview.1 (without a leading v)")
    choices = {"normal": f"proloag-v{version}-windows-x64", "demo": f"proloag-demo-consultant-v{version}-windows-x64"}
    if variant not in {"normal", "demo", "both"}:
        raise ValueError("Unknown variant")
    return choices if variant == "both" else {variant: choices[variant]}


def preflight(root: Path, version: str, variant: str) -> dict[str, object]:
    root = root.resolve()
    names = release_names(version, variant)
    required = list(REQUIRED_SOURCE)
    required += ["docs/" + name for name in PUBLIC_GUIDES]
    required += ["docs/distribution/" + name for name in (*COMMON_DISTRIBUTION_DOCS, "README_RELEASE.md", "README_DEMO.md", "README_APP.md")]
    missing = [name for name in required if not (root / name).is_file()]
    if missing:
        raise ValueError("Required public build inputs are missing: " + ", ".join(missing))
    for name in required:
        no_links(root / name)
    # Future license selection must replace the pending status, not coexist
    # with a contradictory second license marker.
    selected, pending = (root / "LICENSE").is_file(), (root / "LICENSE_STATUS.md").is_file()
    if selected == pending:
        raise ValueError("Provide exactly one of LICENSE or LICENSE_STATUS.md")
    no_links(root / ("LICENSE" if selected else "LICENSE_STATUS.md"))
    output = root / "release"
    no_links(output)
    occupied = []
    for name in names.values():
        for suffix in ("", ".zip", ".zip.partial", ".zip.sha256"):
            target = output / (name + suffix)
            no_links(target)
            if target.exists():
                occupied.append(target.name)
    checksums = output / f"SHA256SUMS-proloag-v{version}.txt"
    if checksums.exists():
        occupied.append(checksums.name)
    result = output / f"BUILD_RESULT-proloag-v{version}.json"
    if result.exists():
        occupied.append(result.name)
    if occupied:
        raise FileExistsError("Existing release targets are never overwritten: " + ", ".join(occupied))
    return {"root": str(root), "version": version, "variants": names, "license_status": "selected" if selected else "unselected_prepublication_preparation"}


def git_provenance(root: Path) -> dict[str, object]:
    def git(*args: str):
        return subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True, timeout=10)
    try:
        top = git("rev-parse", "--show-toplevel")
        if top.returncode or Path(top.stdout.strip()).resolve() != root:
            return {"commit": None, "dirty": None}
        head, status = git("rev-parse", "HEAD"), git("status", "--porcelain")
        return {"commit": head.stdout.strip() if head.returncode == 0 else None, "dirty": bool(status.stdout.strip()) if status.returncode == 0 else None}
    except (OSError, subprocess.TimeoutExpired):
        return {"commit": None, "dirty": None}


def run(command: list[str], *, cwd: Path, environment: dict[str, str]) -> None:
    print("RUN " + " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=environment, check=True)


def render(text: str, version: str, reference_date: date) -> str:
    japanese = f"{reference_date.year}年{reference_date.month}月{reference_date.day}日"
    # Tokens are preferred in future templates; the known original literals
    # allow the initial public-source snapshot's reviewed documents to work.
    return (text.replace("{{VERSION}}", version).replace("{{REFERENCE_DATE}}", reference_date.isoformat())
            .replace("{{REFERENCE_DATE_JA}}", japanese).replace(DEFAULT_VERSION, version)
            .replace("2026年11月5日", japanese).replace("2026-11-05", reference_date.isoformat()))


def prepare_documents(root: Path, package: Path, kind: str, version: str, reference_date: date) -> None:
    app = package / "app"
    inputs = [(root / "docs/distribution" / name, package / name) for name in COMMON_DISTRIBUTION_DOCS]
    chosen = "README_RELEASE.md" if kind == "normal" else "README_DEMO.md"
    inputs += [(root / "docs/distribution" / chosen, package / chosen), (root / "docs/distribution/README_APP.md", app / "README.md"), (root / "THIRD_PARTY_NOTICES.md", package / "THIRD_PARTY_NOTICES.md")]
    for source, target in inputs:
        target.write_text(render(source.read_text(encoding="utf-8-sig"), version, reference_date), encoding="utf-8")
    shutil.copyfile(root / "requirements-release.lock.txt", app / "requirements-release.lock.txt")
    for name in PUBLIC_GUIDES:
        shutil.copyfile(root / "docs" / name, app / "docs" / name)
    license_name = "LICENSE" if (root / "LICENSE").is_file() else "LICENSE_STATUS.md"
    shutil.copyfile(root / license_name, package / license_name)
    if license_name == "LICENSE_STATUS.md":
        banner = "> 公開前の配布準備版です。本体の公開ライセンスは未確定です。LICENSE_STATUS.mdを確認してください。\n"
        for name in ("README_FIRST.md", "RELEASE_NOTES.md"):
            path = package / name
            first, _, rest = path.read_text(encoding="utf-8").partition("\n")
            path.write_text(first + "\n\n" + banner + rest, encoding="utf-8")
    if (root / "licenses").is_dir():
        safe_files(root / "licenses")
        shutil.copytree(root / "licenses", package / "licenses")


def static_validate(root: Path, package: Path, kind: str) -> dict[str, str]:
    # The shared validators perform strict JSON/provenance and allowed-source
    # checks without importing the application or reading checkout records.
    import verify_portable_demo as shared
    import verify_portable_light as light
    files = shared.relative_files(package)
    shared.verify_forbidden_files(files)
    hashes = shared.verify_sources(root, package / "app")
    data = light.verify_light_data(package / "app") if kind == "normal" else shared.verify_demo_json(package / "app")
    for markdown in [package / "README_FIRST.md", package / ("README_RELEASE.md" if kind == "normal" else "README_DEMO.md"), package / "app/README.md", *list((package / "app/docs").glob("*.md"))]:
        for target in re.findall(r"\]\(([^)]+)\)", markdown.read_text(encoding="utf-8-sig")):
            if re.match(r"^[a-z]+:", target) or target.startswith("#"):
                continue
            target = target.split("#")[0]
            if target and not (markdown.parent / target).is_file():
                raise ValueError(f"Broken packaged Markdown link in {markdown.name}: {target}")
    if (package / "app/.deps311").exists() or (package / "app/sitecustomize.py").exists():
        raise ValueError("Development/verification files reached the unused package")
    return {**hashes, **{f"app/{name}": value for name, value in data.items()}}


def create_archive(package: Path) -> dict[str, object]:
    files = safe_files(package)
    expected = {package.name + "/" + p.relative_to(package).as_posix(): digest(p) for p in files}
    archive = package.parent / (package.name + ".zip")
    temporary = package.parent / (package.name + ".zip.partial")
    if archive.exists() or temporary.exists():
        raise FileExistsError(archive)
    with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zipped:
        for file in files:
            zipped.write(file, package.name + "/" + file.relative_to(package).as_posix())
    with zipfile.ZipFile(temporary) as zipped:
        if zipped.testzip() or set(zipped.namelist()) != set(expected):
            raise ValueError("ZIP inventory/CRC mismatch")
        for name, sha in expected.items():
            if hashlib.sha256(zipped.read(name)).hexdigest() != sha:
                raise ValueError("ZIP content mismatch: " + name)
    # Exclusive creation above and preflight protect release names. Rename
    # only a fully verified archive; incomplete builds remain *.partial.
    temporary.rename(archive)
    result = {"name": archive.name, "bytes": archive.stat().st_size, "sha256": digest(archive), "files": len(expected)}
    (archive.parent / (archive.name + ".sha256")).write_text(result["sha256"] + "  " + archive.name + "\n", encoding="ascii")
    return result


def build(root: Path, args: argparse.Namespace) -> dict[str, object]:
    plan = preflight(root, args.version, args.variant)
    if args.check:
        return {"preflight_passed": True, **plan}
    if os.name != "nt":
        raise ValueError("Actual Windows portable builds require Windows")
    python = Path(args.python or sys.executable).resolve()
    probe = subprocess.run([str(python), "-I", "-c", "import json,struct,sys; print(json.dumps({'version':list(sys.version_info[:3]),'bits':struct.calcsize('P')*8}))"], capture_output=True, text=True, check=True)
    runtime = json.loads(probe.stdout)
    if runtime["version"][:2] != [3, 11] or runtime["bits"] != 64:
        raise ValueError("Specify a standard Windows x64 Python 3.11 installation")
    output = root / "release"
    output.mkdir(exist_ok=True)
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
    environment.pop("PYTHONHOME", None)
    environment["PYTHONPATH"] = os.pathsep.join([str(root), str(root / ".deps311")])
    packages = {}
    for kind, name in plan["variants"].items():
        package = output / name
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(root / "scripts/build_portable_release.ps1"), "-ReleaseName", name, "-PythonExe", str(python), "-NoZip"]
        if kind == "normal":
            command.append("-IncludeSampleMaster")
        run(command, cwd=root, environment=environment)
        if kind == "demo":
            run([str(python), "-B", str(root / "scripts/generate_consultant_demo_dataset.py"), "--target-root", str(package / "app"), "--days", str(args.days), "--end-date", args.reference_date.isoformat(), "--seed", str(args.seed)], cwd=root, environment=environment)
            run([str(python), "-B", str(root / "scripts/add_demo_roadmap.py"), "--target-app", str(package / "app"), "--reference-date", args.reference_date.isoformat(), "--source-root", str(root)], cwd=root, environment=environment)
        prepare_documents(root, package, kind, args.version, args.reference_date)
        static_validate(root, package, kind)
        if args.verify_pages:
            verifier = "verify_portable_light.py" if kind == "normal" else "verify_portable_demo.py"
            run([str(python), "-B", str(root / "scripts" / verifier), "--target", str(package), "--source", str(root)], cwd=root, environment=environment)
        packages[kind] = package
    code = {}
    for kind, package in packages.items():
        code[kind] = {p.relative_to(package / "app").as_posix(): digest(p) for p in safe_files(package / "app") if p.suffix == ".py" or p.name == "styles.css"}
    if len(code) == 2 and code["normal"] != code["demo"]:
        raise ValueError("Normal and Demo application code differs")
    provenance = git_provenance(root)
    artifacts = []
    for kind, package in packages.items():
        content = {p.relative_to(package).as_posix(): digest(p) for p in safe_files(package)}
        manifest = {"format": 2, "version": args.version, "variant": kind, "release_name": package.name, "build_timestamp_utc": datetime.now(timezone.utc).isoformat(), "platform": "Windows x64", "bundled_python": ".".join(map(str, runtime["version"])), "source_commit": provenance["commit"], "source_dirty": provenance["dirty"], "license_status": plan["license_status"], "dependencies": {"mode": "first-run online exact-pinned install", "lock_file": "app/requirements-release.lock.txt", "lock_sha256": digest(package / "app/requirements-release.lock.txt")}, "validation": {"static_source_data_and_inventory": True, "all_packaged_pages": bool(args.verify_pages), "fresh_windows_machine": False}, "file_sha256": content}
        if kind == "demo":
            generated = json.loads((package / "app/.consultant_demo_manifest.json").read_text(encoding="utf-8"))
            manifest["synthetic_dataset"] = {key: generated[key] for key in ("generator", "days", "end_date", "seed", "roadmap_sample") if key in generated}
        write_json(package / "RELEASE_MANIFEST.json", manifest)
        artifacts.append({"variant": kind, **create_archive(package)})
    (output / f"SHA256SUMS-proloag-v{args.version}.txt").write_text("".join(x["sha256"] + "  " + x["name"] + "\n" for x in artifacts), encoding="ascii")
    result = {"version": args.version, "source": provenance, "license_status": plan["license_status"], "artifacts": artifacts, "published_to_github": False}
    write_json(output / f"BUILD_RESULT-proloag-v{args.version}.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--variant", choices=("normal", "demo", "both"), default="both")
    parser.add_argument("--python", help="Standard Windows x64 Python 3.11 executable")
    parser.add_argument("--reference-date", type=date.fromisoformat, default=DEFAULT_REFERENCE_DATE)
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--seed", type=int, default=20261105)
    parser.add_argument("--verify-pages", action="store_true", help="Run read-only all-page checks; requires checkout .deps311")
    parser.add_argument("--check", action="store_true", help="Read-only source/license/output preflight; do not build")
    args = parser.parse_args()
    if not 1 <= args.days <= 3650:
        parser.error("--days must be between 1 and 3650")
    try:
        result = build(SOURCE_ROOT, args)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Build stopped: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
