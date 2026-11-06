"""Synthetic fixtures only; no app server, runtime build, or network is used."""
from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("public_builder", ROOT / "scripts/build_public_release.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
roadmap_spec = importlib.util.spec_from_file_location("public_roadmap_sample", ROOT / "scripts/add_demo_roadmap.py")
roadmap = importlib.util.module_from_spec(roadmap_spec)
roadmap_spec.loader.exec_module(roadmap)


class PublicReleaseBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="proloag-public-builder-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "public-source"
        self.root.mkdir()

    def fixture(self):
        names = list(builder.REQUIRED_SOURCE)
        names += ["docs/" + n for n in builder.PUBLIC_GUIDES]
        names += ["docs/distribution/" + n for n in (*builder.COMMON_DISTRIBUTION_DOCS, "README_RELEASE.md", "README_DEMO.md", "README_APP.md")]
        names += ["LICENSE_STATUS.md"]
        for name in names:
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("Synthetic public input\n", encoding="utf-8")

    def test_version_names_reject_paths_and_invalid_identifiers(self):
        names = builder.release_names("0.1.0-preview.1", "both")
        self.assertEqual(set(names), {"normal", "demo"})
        for value in ("../main", "v1.2.3", "1.2.3/other", "1.2.3-a..", "1.2.3-a.", "1.2.3-a ", "1.2.3-" + "a" * 90):
            with self.subTest(value=value), self.assertRaises(ValueError):
                builder.release_names(value, "both")

    def test_preflight_is_read_only_and_requires_public_inputs(self):
        with self.assertRaises(ValueError):
            builder.preflight(self.root, "1.2.3", "both")
        self.fixture()
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = builder.preflight(self.root, "1.2.3", "both")
        self.assertEqual(result["license_status"], "unselected_prepublication_preparation")
        self.assertFalse((self.root / "release").exists())
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_pending_and_selected_license_cannot_coexist(self):
        self.fixture()
        (self.root / "LICENSE").write_text("Synthetic selected license", encoding="utf-8")
        with self.assertRaises(ValueError):
            builder.preflight(self.root, "1.2.3", "normal")
        (self.root / "LICENSE_STATUS.md").unlink()
        self.assertEqual(builder.preflight(self.root, "1.2.3", "normal")["license_status"], "selected")

    def test_every_existing_release_target_stops_before_build(self):
        self.fixture()
        output = self.root / "release"
        output.mkdir()
        name = builder.release_names("1.2.3", "normal")["normal"]
        for leaf in (name, name + ".zip", name + ".zip.partial", name + ".zip.sha256", "SHA256SUMS-proloag-v1.2.3.txt", "BUILD_RESULT-proloag-v1.2.3.json"):
            path = output / leaf
            path.write_text("Keep original", encoding="utf-8")
            with self.subTest(leaf=leaf), self.assertRaises(FileExistsError):
                builder.preflight(self.root, "1.2.3", "normal")
            self.assertEqual(path.read_text(encoding="utf-8"), "Keep original")
            path.unlink()

    def test_render_updates_version_and_fixed_date(self):
        text = "{{VERSION}} {{REFERENCE_DATE}} {{REFERENCE_DATE_JA}} 0.1.0-preview.1 2026年11月5日 2026-11-05"
        rendered = builder.render(text, "1.2.3", date(2027, 2, 3))
        self.assertEqual(rendered, "1.2.3 2027-02-03 2027年2月3日 1.2.3 2027年2月3日 2027-02-03")

    def test_archive_matches_all_files_and_preserves_version_name(self):
        package = self.root / "release/proloag-v1.2.3-preview.1-windows-x64"
        (package / "app").mkdir(parents=True)
        (package / "app/main.py").write_text("# Synthetic public code\n", encoding="utf-8")
        (package / "LICENSE_STATUS.md").write_text("Pending\n", encoding="utf-8")
        result = builder.create_archive(package)
        archive = package.parent / result["name"]
        self.assertEqual(archive.name, package.name + ".zip")
        self.assertEqual(result["files"], 2)
        self.assertEqual(result["sha256"], hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertFalse((package.parent / (package.name + ".zip.partial")).exists())
        with zipfile.ZipFile(archive) as z:
            self.assertEqual(z.read(package.name + "/app/main.py"), (package / "app/main.py").read_bytes())
        with self.assertRaises(FileExistsError):
            builder.create_archive(package)

    def test_roadmap_source_guard_has_no_machine_specific_path(self):
        for bad in (self.root, self.root.parent):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                roadmap.safe_target(bad, self.root)
        target = self.root / "release/demo_consultant/app"
        target.mkdir(parents=True)
        (target / "main.py").write_text("# Synthetic", encoding="utf-8")
        (target / roadmap.MANIFEST).write_text("{}", encoding="utf-8")
        self.assertEqual(roadmap.safe_target(target, self.root), target.resolve())

    def test_roadmap_rejects_unlisted_or_modified_synthetic_data(self):
        target = self.root / "release/demo_consultant/app"
        (target / "settings").mkdir(parents=True)
        mode = target / "settings/demo_mode.json"
        mode.write_text(json.dumps({"enabled": True, "fixed_today": "2026-11-05"}), encoding="utf-8")
        marker = {"schema_version": 1, "generator": roadmap.GENERATOR, "end_date": "2026-11-05", "files": {"settings/demo_mode.json": builder.digest(mode)}}
        (target / roadmap.MANIFEST).write_text(json.dumps(marker), encoding="utf-8")
        self.assertEqual(roadmap.inspect_dataset(target, date(2026, 11, 5)), marker)
        extra = target / "settings/unknown.json"
        extra.write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            roadmap.inspect_dataset(target, date(2026, 11, 5))
        extra.unlink()
        mode.write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            roadmap.inspect_dataset(target, date(2026, 11, 5))


if __name__ == "__main__":
    unittest.main()
