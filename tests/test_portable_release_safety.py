from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "scripts" / "build_portable_release.ps1"
START_SCRIPT = ROOT / "scripts" / "portable_release" / "run_portable_app.ps1"
POWERSHELL = shutil.which("powershell.exe")
GIT = shutil.which("git")

OAUTH_PRIVATE_FILES = (
    "credentials.json", "credentials-personal.json", "google_credentials.json",
    "client_secret_123.apps.googleusercontent.com.json", "client-secret.json", "clientsecret.json",
    "token.json", "token-backup.json", "token_personal.json", "google_auth.json",
    "google_auth.bin", "authorization.dpapi", "service_account.json", "service-account.json",
)


def ps_string(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(POWERSHELL, "Windows PowerShell is needed for portable release scripts")
class PortableReleaseSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="proloag-build-safety-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def run_functions(self, source: Path, body: str) -> subprocess.CompletedProcess:
        # Import only function declarations, never execute the real build/startup.
        script = f"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile({ps_string(source)}, [ref]$tokens, [ref]$errors)
if ($errors.Count) {{ throw ($errors | Out-String) }}
foreach ($function in $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }}, $false)) {{
    . ([scriptblock]::Create($function.Extent.Text))
}}
{body}
"""
        return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script],
                              text=True, capture_output=True, timeout=60)

    def variables(self, release_name="synthetic_demo", output_root=None):
        output = output_root or self.root / "release"
        return f"""
$ProjectRoot = {ps_string(self.root)}
$OutputRootPath = {ps_string(output)}
$ReleaseName = {ps_string(release_name)}
$ReleaseDir = [System.IO.Path]::GetFullPath((Join-Path $OutputRootPath $ReleaseName))
$AppDir = Join-Path $ReleaseDir 'app'
$RuntimeDir = Join-Path $ReleaseDir 'runtime\\python311'
"""

    def write(self, relative: str, value="synthetic") -> Path:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value, encoding="utf-8")
        return target

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_both_scripts_parse_without_executing_their_entrypoints(self):
        self.assert_success(self.run_functions(BUILD_SCRIPT, ""))
        self.assert_success(self.run_functions(START_SCRIPT, ""))

    def test_release_targets_accept_only_safe_release_descendants(self):
        self.assert_success(self.run_functions(BUILD_SCRIPT, self.variables() + "Assert-ReleaseTargets"))
        cases = [self.variables(name) for name in ("..", ".", "../pages", "nested/demo", "demo.", "demo ")]
        cases += [self.variables(output_root=self.root), self.variables(output_root=self.root / "release_other")]
        for variables in cases:
            with self.subTest(variables=variables):
                result = self.run_functions(BUILD_SCRIPT, variables + "Assert-ReleaseTargets")
                self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_path_guard_rejects_root_parent_and_prefix_sibling(self):
        allowed = self.root / "release" / "synthetic_demo"
        for target in (allowed, allowed.parent, allowed.parent / "synthetic_demo_other", allowed / ".." / "outside"):
            with self.subTest(target=target):
                result = self.run_functions(BUILD_SCRIPT,
                    f"Assert-PathWithin -Path {ps_string(target)} -AllowedRoot {ps_string(allowed)}")
                self.assertNotEqual(result.returncode, 0)
        result = self.run_functions(BUILD_SCRIPT,
            f"Assert-PathWithin -Path {ps_string(allowed / 'app' / 'cache')} -AllowedRoot {ps_string(allowed)}")
        self.assert_success(result)

    def test_copy_app_allows_only_code_stylesheet_and_streamlit_config(self):
        for name in ("main.py", "config.py", "data_utils.py", "master_ui.py", "requirements.txt",
                     "requirements-google.txt", "README.md", "docs/google-sheets-setup.md", "docs/google-docs-reports.md",
                     "docs/consultation-proposals.md"):
            self.write(name)
        required = ["app_core/startup_launcher.py", "app_core/navigation.py", "app_core/countdown_service.py",
                    "app_core/personalization_ui.py", "prolog_ui/styles.css", "pages/01_sample.py"]
        for name in required:
            self.write(name)
        self.write("app_core/nested/service.py")
        self.write("app_core/google_share_service.py")
        self.write("app_core/google_sheets_client.py")
        self.write("app_core/google_share_auth.py")
        new_features = (
            "pages/95_使い方ガイド.py", "pages/96_Google連携ガイド.py",
            "app_core/user_guide_ui.py", "app_core/google_guide_ui.py",
            "app_core/export_ui.py", "app_core/reference_snapshot.py",
        )
        for name in new_features:
            self.write(name)
        for name in ("app_core/private.json", "app_core/.env", "prolog_ui/secret.txt", "pages/record.json",
                     "pages/__pycache__/sample.pyc", ".streamlit/secrets.toml", ".streamlit/credentials.json"):
            self.write(name, "FAKE_SECRET_MUST_NOT_SHIP")
        for directory in ("", "app_core", "pages", "prolog_ui", "docs", ".streamlit"):
            for filename in OAUTH_PRIVATE_FILES:
                self.write(f"{directory}/{filename}".lstrip("/"), "FAKE_SECRET_MUST_NOT_SHIP")
        for directory in (".deps-google311", ".deps-google312"):
            self.write(f"{directory}/google/auth/__init__.py", "FAKE_SECRET_MUST_NOT_SHIP")
            self.write(f"{directory}/token.json", "FAKE_SECRET_MUST_NOT_SHIP")
        self.write(".streamlit/config.toml", "[client]\nshowSidebarNavigation = false\n")
        result = self.run_functions(BUILD_SCRIPT, self.variables() + "Assert-ReleaseTargets; Copy-AppFiles")
        self.assert_success(result)
        app = self.root / "release" / "synthetic_demo" / "app"
        files = {path.relative_to(app).as_posix() for path in app.rglob("*") if path.is_file()}
        self.assertTrue(set(required).issubset(files))
        self.assertTrue(set(new_features).issubset(files))
        self.assertIn("app_core/nested/service.py", files)
        self.assertTrue({"requirements-google.txt", "docs/google-sheets-setup.md",
                         "docs/google-docs-reports.md",
                         "docs/consultation-proposals.md",
                         "app_core/google_share_service.py", "app_core/google_sheets_client.py",
                         "app_core/google_share_auth.py"}.issubset(files))
        self.assertFalse({Path(item).name for item in files}.intersection(OAUTH_PRIVATE_FILES))
        self.assertFalse(any(item.startswith((".deps-google311/", ".deps-google312/")) for item in files))
        self.assertEqual([item for item in files if item.startswith(".streamlit/")], [".streamlit/config.toml"])
        for path in app.rglob("*"):
            if path.is_file():
                self.assertNotIn("FAKE_SECRET_MUST_NOT_SHIP", path.read_text(encoding="utf-8"))

    @unittest.skipUnless(GIT, "Git is needed to check ignore rules")
    def test_oauth_private_names_are_ignored_but_feature_sources_are_not(self):
        self.write(".gitignore", (ROOT / ".gitignore").read_text(encoding="utf-8-sig"))
        subprocess.run([GIT, "init", "--quiet", str(self.root)], check=True, capture_output=True, timeout=30)
        private_paths = [f"{folder}/{name}" for folder in ("app_core", "private") for name in OAUTH_PRIVATE_FILES]
        private_paths += [f"{folder}/google/auth/__init__.py" for folder in (".deps-google311", ".deps-google312")]
        public_paths = ["requirements-google.txt", "docs/google-sheets-setup.md", "docs/google-docs-reports.md",
                        "docs/consultation-proposals.md",
                        "app_core/google_share_auth.py", "app_core/google_sheets_client.py"]
        result = subprocess.run([GIT, "-C", str(self.root), "check-ignore", "--no-index",
                                 *private_paths, *public_paths],
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(result.stdout.splitlines()), set(private_paths))

    def test_copy_runtime_excludes_installed_tools_packages_and_user_files(self):
        for name in ("python.exe", "pythonw.exe", "python3.dll", "python311.dll", "vcruntime140.dll", "LICENSE.txt",
                     "DLLs/_ssl.pyd", "DLLs/libcrypto.dll", "Lib/os.py", "Lib/ensurepip/_bundled/pip.whl"):
            self.write("python_source/" + name)
        for name in ("private.json", "Scripts/tool.exe", "etc/jupyter/config.json", "share/data.json", "include/python.h",
                     "Lib/site-packages/private.py", "Lib/__pycache__/os.pyc", "DLLs/private.json"):
            self.write("python_source/" + name, "FAKE_PRIVATE_RUNTIME_DATA")
        result = self.run_functions(BUILD_SCRIPT, self.variables() +
            f"Assert-ReleaseTargets; Copy-PythonRuntime -SourcePythonExe {ps_string(self.root / 'python_source' / 'python.exe')} -Destination $RuntimeDir")
        self.assert_success(result)
        runtime = self.root / "release" / "synthetic_demo" / "runtime" / "python311"
        files = {path.relative_to(runtime).as_posix() for path in runtime.rglob("*") if path.is_file()}
        self.assertIn("Lib/ensurepip/_bundled/pip.whl", files)
        self.assertIn("DLLs/_ssl.pyd", files)
        self.assertFalse(any(name.startswith(("Scripts/", "etc/", "share/", "include/", "Lib/site-packages/")) for name in files))
        self.assertFalse(any(name.endswith(".pyc") for name in files))
        self.assertFalse(any("FAKE_PRIVATE_RUNTIME_DATA" in path.read_text() for path in runtime.rglob("*") if path.is_file()))

    def test_demo_port_is_separate_and_invalid_metadata_fails_closed(self):
        app = self.root / "app"
        command = f"$AppDir = {ps_string(app)}; Write-Output (Get-AppPort)"
        result = self.run_functions(START_SCRIPT, command)
        self.assert_success(result)
        self.assertEqual(result.stdout.strip(), "8502")
        for data, expected in (({}, "8502"), ({"enabled": False}, "8502"), ({"enabled": True}, "8503")):
            path = self.write("app/settings/demo_mode.json", json.dumps(data))
            before = path.read_bytes()
            result = self.run_functions(START_SCRIPT, command)
            self.assert_success(result)
            self.assertEqual(result.stdout.strip(), expected)
            self.assertEqual(path.read_bytes(), before)
        for text in ("{broken", "[]", "null", '{"enabled":"true"}', '{"enabled":1}'):
            path = self.write("app/settings/demo_mode.json", text)
            before = path.read_bytes()
            result = self.run_functions(START_SCRIPT, command)
            self.assertNotEqual(result.returncode, 0, (text, result.stdout, result.stderr))
            self.assertEqual(path.read_bytes(), before)

    def test_deletion_uses_one_guarded_literal_path_entrypoint(self):
        source = BUILD_SCRIPT.read_text(encoding="utf-8-sig")
        self.assertEqual(source.count("Remove-Item "), 1)
        self.assertIn("Remove-Item -LiteralPath ([System.IO.Path]::GetFullPath($Path))", source)
        self.assertIn("Assert-PathWithin -Path $Path -AllowedRoot $AllowedRoot", source)
        self.assertIn("Release ZIP already exists", source)
        self.assertLess(source.index("Assert-ReleaseTargets\nif (-not $NoZip"), source.index('Write-Step "Prepare release directory"'))


if __name__ == "__main__":
    unittest.main()
