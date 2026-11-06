"""Boundary, concurrency and lifetime tests for visitor-owned record copies."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import config
from app_core import json_store
from app_core.settings_service import USER_SETTINGS_PATH, load_user_settings, update_user_settings
from web_demo import sandbox
from web_demo.data import data_fingerprint


def fingerprint(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


class ExperienceStorageTests(unittest.TestCase):
    def test_concurrent_transactions_share_a_lock_and_release_its_cache_entry(self):
        import gc
        handle = self.make_visitor()
        path = handle.root / "settings/lock_probe.json"
        def increment(_):
            with sandbox.sandbox_context(handle), json_store.json_transaction(path):
                value = json_store.load_json_safe(path, {"value": 0})
                value["value"] += 1
                json_store.save_json_safe(path, value)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(increment, range(8)))
        with sandbox.sandbox_context(handle):
            self.assertEqual(json_store.load_json_safe(path, {})["value"], 8)
            key = json_store._key(path)
        gc.collect()
        self.assertNotIn(key, json_store._locks)

    def setUp(self):
        self.seed_before = data_fingerprint()
        self.temp = tempfile.TemporaryDirectory(prefix="sx_")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.handles = []

    def tearDown(self):
        self.assertEqual(self.seed_before, data_fingerprint(), "The shared synthetic records changed")

    def make_visitor(self):
        handle = sandbox.create_sandbox(base_dir=self.base)
        self.handles.append(handle)
        self.addCleanup(self.remove_visitor, handle)
        return handle

    def remove_visitor(self, handle):
        if sandbox._registry.get(handle.token) is handle:
            sandbox.destroy_sandbox(handle)

    def test_context_paths_match_plain_path_revision_dictionary_keys(self):
        a, b = self.make_visitor(), self.make_visitor()
        root_object = config.PROJECT_ROOT
        with sandbox.sandbox_context(a):
            deferred = config.DIARY_ENTRIES_DIR / "2026/11/20261105.json"
            concrete = Path(deferred)
            self.assertEqual(deferred, concrete)
            self.assertEqual(concrete, deferred)
            self.assertEqual(hash(deferred), hash(concrete))
            baseline = {concrete: "original-revision"}
            self.assertEqual(baseline[config.DIARY_ENTRIES_DIR / "2026/11/20261105.json"], "original-revision")
        with sandbox.sandbox_context(b):
            self.assertNotEqual(Path(config.DIARY_ENTRIES_DIR / "2026/11/20261105.json"), concrete)
        self.assertIs(config.PROJECT_ROOT, root_object)
        self.assertEqual(Path(config.PROJECT_ROOT), sandbox.SOURCE_ROOT)

    def test_imported_defaults_stay_separate_in_parallel_visitors(self):
        a, b = self.make_visitor(), self.make_visitor()
        # USER_SETTINGS_PATH was imported before either visitor existed.
        def visitor(handle, minutes):
            with sandbox.sandbox_context(handle):
                self.assertEqual(Path(USER_SETTINGS_PATH), handle.root / "settings/user_settings.json")
                update_user_settings({"timer": {"goal_minutes": minutes}})
                return load_user_settings()["timer"]["goal_minutes"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda args: visitor(*args), [(a, 111), (b, 222)]))
        self.assertEqual(results, [111, 222])
        for handle, expected in ((a, 111), (b, 222)):
            with sandbox.sandbox_context(handle):
                self.assertEqual(load_user_settings()["timer"]["goal_minutes"], expected)
        self.assertIsNone(sandbox.current_root())

    def test_cross_visitor_and_shared_seed_read_write_are_rejected(self):
        a, b = self.make_visitor(), self.make_visitor()
        before_b = fingerprint(b.root)
        with sandbox.sandbox_context(a):
            own = a.root / "settings/visitor_probe.json"
            json_store.save_json_safe(own, {"value": "only-a"})
            self.assertEqual(json_store.load_json_safe(own, {}), {"value": "only-a"})
            for root in (b.root, sandbox.SOURCE_ROOT):
                with self.subTest(root=root), self.assertRaises(json_store.JsonReadOnlyError):
                    json_store.load_json_safe(root / "settings/user_settings.json", {})
                with self.subTest(root=root), self.assertRaises(json_store.JsonReadOnlyError):
                    json_store.save_json_safe(root / "settings/visitor_probe.json", {"bad": True})
        self.assertEqual(before_b, fingerprint(b.root))

    def test_unbound_write_lock_directory_and_backup_fail_before_touching_files(self):
        a = self.make_visitor()
        before = fingerprint(a.root)
        actions = (
            lambda: json_store.save_json_safe(a.root / "settings/probe.json", {}),
            lambda: json_store.ensure_dir(a.root / "new-folder"),
            lambda: json_store.backup_json(a.root / "settings/user_settings.json"),
        )
        for action in actions:
            with self.assertRaises(json_store.JsonReadOnlyError):
                action()
        with self.assertRaises(json_store.JsonReadOnlyError):
            with json_store.json_transaction(a.root / "settings/user_settings.json"):
                self.fail("Unbound transaction was entered")
        self.assertEqual(before, fingerprint(a.root))

    def test_json_disk_and_file_quotas_reject_before_creating_lock_or_backup(self):
        a = self.make_visitor()
        before = fingerprint(a.root)
        used, files = sandbox._inventory(a.root)
        with sandbox.sandbox_context(a):
            for setting, limit in (("MAX_JSON_BYTES", 8), ("MAX_SESSION_BYTES", used + 1),
                                   ("MAX_SESSION_FILES", files + 1)):
                with self.subTest(setting=setting), patch.object(sandbox, setting, limit):
                    with self.assertRaises(json_store.JsonReadOnlyError):
                        json_store.save_json_safe(a.root / "settings/quota_probe.json", {"value": "too much"})
                self.assertEqual(before, fingerprint(a.root))

    def test_traversal_cannot_select_a_different_visitor(self):
        a, b = self.make_visitor(), self.make_visitor()
        before = fingerprint(b.root)
        with sandbox.sandbox_context(a):
            escaped = a.root / ".." / b.token / "settings/probe.json"
            with self.assertRaises(sandbox.SandboxAccessError):
                sandbox.guard_path(escaped, write=True)
            with self.assertRaises(json_store.JsonReadOnlyError):
                json_store.save_json_safe(escaped, {"bad": True})
        self.assertEqual(before, fingerprint(b.root))

    def test_symlink_cannot_select_a_different_visitor(self):
        a, b = self.make_visitor(), self.make_visitor()
        link = a.root / "other-visitor"
        try:
            os.symlink(b.root, link, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"This OS account cannot create test symlinks: {exc}")
        try:
            with sandbox.sandbox_context(a):
                with self.assertRaises(sandbox.SandboxAccessError):
                    sandbox.guard_path(link / "settings/user_settings.json")
                with self.assertRaises(json_store.JsonReadOnlyError):
                    json_store.save_json_safe(link / "settings/probe.json", {})
        finally:
            link.unlink()

    def test_reset_creates_fresh_copy_and_defers_deleting_active_old_root(self):
        import streamlit as st
        a = self.make_visitor()
        state = {sandbox.SESSION_KEY: a}
        with (patch.object(st, "session_state", state), patch.object(sandbox, "_DEFAULT_BASE", self.base),
              patch.object(sandbox, "MAX_SESSIONS", 1)):
            with sandbox.sandbox_context(a):
                json_store.save_json_safe(a.root / "settings/temporary_change.json", {"sample": True})
                fresh = sandbox.reset_session_sandbox()
                self.handles.append(fresh)
                self.addCleanup(self.remove_visitor, fresh)
                self.assertIs(state[sandbox.SESSION_KEY], fresh)
                self.assertTrue(a.root.exists())
                self.assertTrue(a.retired)
                self.assertFalse((fresh.root / "settings/temporary_change.json").exists())
            self.assertFalse(a.root.exists())
            with sandbox.sandbox_context(fresh):
                self.assertEqual(Path(config.PROJECT_ROOT), fresh.root)

    def test_ttl_cleanup_removes_idle_visitor_but_keeps_active_visitor(self):
        a, b = self.make_visitor(), self.make_visitor()
        now = time.time()
        a.touched_at = now - sandbox.IDLE_TTL_SECONDS - 1
        with sandbox.sandbox_context(b):
            b.touched_at = now - sandbox.IDLE_TTL_SECONDS - 1
            self.assertEqual(sandbox.cleanup_expired_sandboxes(now=now, force=True), 1)
            self.assertFalse(a.root.exists())
            self.assertTrue(b.root.exists())
        self.assertTrue(b.root.exists())

    def test_forged_handle_is_not_a_visitor_store(self):
        a = self.make_visitor()
        forged = sandbox.Sandbox(root=a.root, token=a.token)
        with self.assertRaises(sandbox.SandboxAccessError):
            with sandbox.sandbox_context(forged):
                self.fail("Unregistered handle was trusted")

    def test_cleanup_retries_transient_file_handle_without_changing_permissions(self):
        handle = self.make_visitor()
        original = sandbox.shutil.rmtree
        calls = []
        def delayed(path):
            calls.append(path)
            if len(calls) < 3:
                raise PermissionError("A scanner still holds a test file")
            original(path)
        with patch.object(sandbox.shutil, "rmtree", side_effect=delayed):
            self.assertTrue(sandbox.destroy_sandbox(handle))
        self.assertEqual(calls, [handle.root] * 3)
        self.assertFalse(handle.root.exists())

    def test_bulk_operation_locks_cannot_exceed_the_file_budget(self):
        from app_core.json_operations import atomic_json_operation
        handle = self.make_visitor()
        before = fingerprint(handle.root)
        cap = len(before) + 4
        with sandbox.sandbox_context(handle), patch.object(sandbox, "MAX_SESSION_FILES", cap):
            with self.assertRaises(json_store.JsonReadOnlyError):
                with atomic_json_operation(root=handle.root, kind="quota_bulk") as operation:
                    for index in range(8):
                        operation.stage(handle.root / f"study_logs/2026/11/quota_probe_{index}.json", [])
        after = fingerprint(handle.root)
        self.assertLessEqual(len(after), cap)
        self.assertTrue(all(name in before or name.endswith(".lock") for name in after))
        self.assertEqual({name: after[name] for name in before}, before)


if __name__ == "__main__":
    unittest.main()
