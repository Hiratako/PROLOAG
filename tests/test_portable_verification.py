from __future__ import annotations

import ast
from contextlib import redirect_stdout
from io import StringIO
import io
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
from typing import Any
import unittest
from unittest.mock import patch
import zipfile

from scripts import verify_portable_demo as verify
from app_core import json_store
from tests.test_portable_release_safety import OAUTH_PRIVATE_FILES


ROOT = Path(__file__).resolve().parents[1]


class PortableVerificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag-verifier-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.package = self.root / "new_package"
        self.app = self.package / "app"

    @staticmethod
    def write(root, name, content="# synthetic public code\n"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def scaffold(self):
        names = [*verify.TOP_PYTHON, *verify.PUBLIC_SUPPORT_FILES,
                 "pages/95_使い方ガイド.py", "pages/96_Google連携ガイド.py", "pages/97_データエクスポート.py",
                 "app_core/user_guide_ui.py", "app_core/google_guide_ui.py",
                 "app_core/export_ui.py", "app_core/reference_snapshot.py", "prolog_ui/styles.css"]
        for name in names:
            self.write(self.source, name)
            self.write(self.app, name)

    def hashes(self):
        with redirect_stdout(StringIO()):
            return verify.verify_sources(self.source, self.app)

    def test_new_code_and_public_google_guides_are_verified_without_source_json(self):
        self.scaffold()
        private = self.write(self.source, "app_core/token.json", "DO_NOT_READ")
        original = Path.read_bytes

        def guarded(path):
            if path == private:
                raise AssertionError("Source personal JSON must not be inspected")
            return original(path)

        with patch.object(Path, "read_bytes", guarded):
            hashes = self.hashes()
        for name in verify.PUBLIC_SUPPORT_FILES:
            self.assertIn("app/" + name, hashes)
        self.assertIn("app/pages/95_使い方ガイド.py", hashes)
        self.assertIn("app/app_core/export_ui.py", hashes)
        self.assertIn("app/app_core/reference_snapshot.py", hashes)

    def test_missing_or_changed_public_guide_and_google_requirements_are_rejected(self):
        self.scaffold()
        for name in verify.PUBLIC_SUPPORT_FILES:
            path = self.app / name
            original = path.read_text(encoding="utf-8")
            with self.subTest(name=name):
                path.write_text("changed public document", encoding="utf-8")
                with self.assertRaisesRegex(AssertionError, "Packaged source differs"):
                    self.hashes()
                path.unlink()
                with self.assertRaisesRegex(AssertionError, "Packaged source differs"):
                    self.hashes()
                path.write_text(original, encoding="utf-8")

    def test_public_support_file_hashes_are_checked_inside_zip(self):
        self.scaffold()
        hashes = self.hashes()
        files = verify.relative_files(self.package)
        archive = self.root / "new_package.zip"
        with zipfile.ZipFile(archive, "x") as zipped:
            for name, path in files.items():
                payload = path.read_bytes()
                if name == "app/requirements-google.txt":
                    payload = b"# modified public code!\n"  # Same bytes length as the scaffold.
                    payload = payload[:len(path.read_bytes())].ljust(len(path.read_bytes()), b" ")
                zipped.writestr(self.package.name + "/" + name, payload)
        with patch.object(verify, "REQUIRED_FILES", ()), self.assertRaisesRegex(AssertionError, "code/data hash mismatch"):
            verify.verify_zip(archive, self.package, files, hashes)

    def test_page_discovery_includes_all_current_pages_without_hardcoded_count(self):
        self.scaffold()
        first = verify.packaged_pages(self.app)
        self.assertEqual(len(first), 4)
        future = self.write(self.app, "pages/100_future.py")
        self.assertEqual(verify.packaged_pages(self.app), [self.app / "main.py", *sorted((self.app / "pages").glob("*.py"))])
        self.assertIn(future, verify.packaged_pages(self.app))
        selected = verify.packaged_pages(self.app, ("95", "96", "97"))
        self.assertEqual([path.name[:2] for path in selected[1:]], ["95", "96", "97"])

    def test_required_guide_is_not_silently_omitted_or_duplicated(self):
        self.scaffold()
        for prefix in verify.REQUIRED_GUIDE_PREFIXES:
            duplicate = self.write(self.app, f"pages/{prefix}_duplicate.py")
            with self.subTest(prefix=prefix, kind="duplicate"), self.assertRaises(AssertionError):
                verify.packaged_pages(self.app)
            duplicate.unlink()
        (self.app / "pages/95_使い方ガイド.py").unlink()
        with self.assertRaises(AssertionError):
            verify.packaged_pages(self.app)

    def test_oauth_files_dependency_caches_and_share_backups_are_rejected_by_name(self):
        forbidden = ["app/" + name for name in OAUTH_PRIVATE_FILES]
        forbidden += [
            "connection.dpapi", "app/.deps311/library.py", "app/.deps312/library.py",
            "app/.deps-google311/google/auth/__init__.py", "app/.deps-google312/google/auth/__init__.py",
            "app/backups/google_share/pending.json", "app/.streamlit/secrets.toml",
        ]
        for name in forbidden:
            with self.subTest(name=name), self.assertRaisesRegex(AssertionError, "Forbidden"):
                verify.verify_forbidden_files({name: self.root / "not-read"})
        with redirect_stdout(StringIO()):
            verify.verify_forbidden_files({name: self.root / "not-read" for name in (
                "app/app_core/google_share_auth.py", "app/app_core/google_sheets_client.py",
                "app/requirements-google.txt", "app/docs/google-sheets-setup.md",
            )})

    def audit_guard(self, kind):
        path = ROOT / "scripts" / f"verify_portable_{kind}.py"
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        captured = []
        scope = {"Path": Path, "Any": Any, "os": os, "app": self.app,
                 "sys": SimpleNamespace(addaudithook=captured.append)}
        names = {"inside_package", "prevent_package_writes"} if kind == "demo" else {"install_write_guard"}
        nodes = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name in names]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), scope)
        if kind == "demo":
            return scope["prevent_package_writes"]
        scope["install_write_guard"](self.package)
        return captured[0]

    def test_workers_block_network_process_and_package_write_operations(self):
        for kind in ("demo", "light"):
            guard = self.audit_guard(kind)
            for event, args in (
                ("socket.connect", (object(), ("example.invalid", 443))),
                ("socket.getaddrinfo", ("example.invalid", 443)),
                ("subprocess.Popen", ("not-run",)),
                ("os.startfile", ("not-opened",)),
                ("open", (str(self.app / "settings/never-written.json"), "w", os.O_WRONLY)),
            ):
                with self.subTest(kind=kind, event=event), self.assertRaises(AssertionError):
                    guard(event, args)
        self.assertFalse(self.package.exists())

    def test_read_locks_are_reentrant_and_create_only_temporary_lock_files(self):
        self.app.mkdir(parents=True)
        target = self.app / "settings/learning_context.json"
        original = json_store._file_transaction
        with verify.isolated_verification_locks(json_store, self.app) as lock_root:
            with json_store.json_transaction(target):
                with json_store.json_transaction(target):
                    files = list(lock_root.iterdir())
                    self.assertEqual(len(files), 2)  # Operation + document lock.
                    self.assertTrue(all(path.name.endswith(".lock") for path in files))
                    self.assertEqual(list(self.app.rglob("*")), [])
                with json_store.json_transaction(self.app / "settings/another.json"):
                    self.assertEqual(len(list(lock_root.iterdir())), 3)
            # Windows byte-range locks deliberately prevent reading that byte
            # while held, so inspect the lock sentinel only after release.
            self.assertTrue(all(path.read_bytes() == b"\0" for path in lock_root.iterdir()))
        self.assertIs(json_store._file_transaction, original)
        self.assertFalse(lock_root.exists())
        self.assertEqual(list(self.app.rglob("*")), [])

    def test_redirected_lock_still_excludes_another_thread_and_releases_on_error(self):
        self.app.mkdir(parents=True)
        target = self.app / "settings/learning_context.json"
        outcomes = []

        def competing_reader():
            try:
                with json_store.json_transaction(target, timeout=0.05):
                    outcomes.append("unexpected entry")
            except json_store.JsonLockTimeoutError:
                outcomes.append("timeout")

        with verify.isolated_verification_locks(json_store, self.app):
            with self.assertRaisesRegex(RuntimeError, "synthetic body failure"):
                with json_store.json_transaction(target):
                    competing = threading.Thread(target=competing_reader)
                    competing.start()
                    competing.join(timeout=2)
                    self.assertFalse(competing.is_alive())
                    self.assertEqual(outcomes, ["timeout"])
                    raise RuntimeError("synthetic body failure")
            # Exception cleanup must release both the in-process and file locks.
            with json_store.json_transaction(target, timeout=0.1):
                pass

    def test_lock_mapping_rejects_paths_outside_the_packaged_app(self):
        self.app.mkdir(parents=True)
        with verify.isolated_verification_locks(json_store, self.app) as lock_root:
            for target in (self.source / "settings/private.json", self.package / "outside.json"):
                with self.subTest(target=target), self.assertRaisesRegex(AssertionError, "Lock target escaped"):
                    with json_store.json_transaction(target):
                        pass
            self.assertEqual(list(lock_root.iterdir()), [])

    def test_lock_redirection_does_not_allow_data_or_lock_writes_to_package(self):
        self.app.mkdir(parents=True)
        original_open = io.open
        for kind in ("demo", "light"):
            guard = self.audit_guard(kind)

            def guarded_open(file, mode="r", *args, **kwargs):
                guard("open", (file, mode, 0))
                return original_open(file, mode, *args, **kwargs)

            with self.subTest(kind=kind), patch.object(io, "open", guarded_open):
                with verify.isolated_verification_locks(json_store, self.app) as lock_root:
                    with json_store.json_transaction(self.app / "settings/learning_context.json"):
                        self.assertEqual(len(list(lock_root.iterdir())), 2)
                        for name in ("must_not_save.json", ".must_not_write.lock"):
                            with self.assertRaises(AssertionError):
                                (self.app / name).write_text("must not reach disk", encoding="utf-8")
                    self.assertEqual(list(self.app.rglob("*")), [])

    def test_temporary_mapping_is_restored_if_verification_raises(self):
        self.app.mkdir(parents=True)
        original = json_store._file_transaction
        with self.assertRaisesRegex(RuntimeError, "synthetic check failure"):
            with verify.isolated_verification_locks(json_store, self.app) as lock_root:
                with json_store.json_transaction(self.app / "settings/example.json"):
                    pass
                raise RuntimeError("synthetic check failure")
        self.assertIs(json_store._file_transaction, original)
        self.assertFalse(lock_root.exists())


if __name__ == "__main__":
    unittest.main()
