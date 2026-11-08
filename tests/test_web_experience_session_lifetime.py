"""Exercise session ownership using private, disposable synthetic visitors."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from streamlit.runtime.caching import clear_session_resource_cache
from streamlit.testing.v1 import AppTest

from web_demo import sandbox
from web_demo.data import data_fingerprint


def _owner_probe(session_id):
    import streamlit as st
    from streamlit.runtime.scriptrunner import get_script_run_ctx
    from web_demo.sandbox import ensure_session_sandbox

    # AppTest otherwise assigns every instance the same "test session id".
    # Give these test clients distinct server-side identities before caching.
    get_script_run_ctx().session_id = session_id
    handle = ensure_session_sandbox()
    st.write(handle.token)


def fingerprint(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*.json") if path.is_file()}


class ExperienceSessionLifetimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit import config
        config.get_config_options(force_reparse=True,
                                  options_from_flags={"global.developmentMode": False})

    def setUp(self):
        os_temp = (Path(os.environ["LOCALAPPDATA"]) / "Temp"
                   if os.name == "nt" else Path("/tmp"))
        if "onedrive" in str(os_temp.resolve()).casefold():
            raise RuntimeError("Session lifetime fixtures must use OS Temp outside OneDrive.")
        self.seed_before = data_fingerprint()
        self.temp = tempfile.TemporaryDirectory(prefix="sl_", dir=os_temp)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.owners = []
        self.handles = []
        self.before = {}
        self.session_ids = []
        self.addCleanup(self.cleanup_owned_visitors)

    def tearDown(self):
        self.assertEqual(self.seed_before, data_fingerprint(), "Shared seed records changed")
        for handle in self.handles:
            if sandbox._registry.get(handle.token) is handle and handle.root.exists():
                self.assertEqual(self.before[handle.token], fingerprint(handle.root),
                                 "A surviving visitor's records changed")

    def cleanup_owned_visitors(self):
        for session_id in self.session_ids:
            clear_session_resource_cache(session_id)
        for owner in self.owners:
            sandbox._release_session_owner(owner)
        for handle in self.handles:
            if sandbox._registry.get(handle.token) is handle:
                self.assertTrue(sandbox.destroy_sandbox(handle))

    def owner(self):
        owner = sandbox.SessionOwner()
        self.owners.append(owner)
        return owner

    def remember(self, handle):
        if handle not in self.handles:
            self.handles.append(handle)
            self.before[handle.token] = fingerprint(handle.root)
            if handle.owner is not None and handle.owner not in self.owners:
                self.owners.append(handle.owner)
        return handle

    def visitor(self, owner=None):
        return self.remember(sandbox.create_sandbox(base_dir=self.base, _owner=owner))

    def healthy(self, app):
        self.assertEqual(len(app.exception), 0, [item.message for item in app.exception])
        self.assertEqual(len(app.error), 0, [item.value for item in app.error])

    def test_main_widget_callback_recovers_released_store_before_the_entrypoint(self):
        from prolog_ui.components import THEME_TOGGLE_KEY

        # The real main.py runs with AppTest's native session ID. This test
        # owns its cache entry and clears it again during its own cleanup.
        session_id = "test session id"
        self.session_ids.append(session_id)
        clear_session_resource_cache(session_id)
        other = self.visitor(self.owner())
        with patch.object(sandbox, "_DEFAULT_BASE", self.base):
            app = AppTest.from_file(sandbox.SOURCE_ROOT / "main.py", default_timeout=90).run()
            self.healthy(app)
            old = self.remember(app.session_state[sandbox.SESSION_KEY])
            app.session_state["lifetime_old_draft"] = "Synthetic stale callback state"
            toggle = app.toggle(key=THEME_TOGGLE_KEY)
            previous_value = toggle.value

            clear_session_resource_cache(session_id)
            self.assertTrue(old.owner.released)
            self.assertFalse(old.root.exists())

            # Streamlit invokes this widget's actual on_change callback before
            # running main.py again. It must not save through the released root.
            toggle.set_value(not previous_value).run(timeout=90)
            self.healthy(app)
            fresh = self.remember(app.session_state[sandbox.SESSION_KEY])
            self.assertIsNot(fresh.owner, old.owner)
            self.assertFalse(fresh.owner.released)
            self.assertNotEqual(fresh.root, old.root)
            self.assertTrue(fresh.root.is_dir())
            self.assertNotIn("lifetime_old_draft", app.session_state)
            self.assertEqual(self.before[other.token], fingerprint(other.root))

    def test_native_session_cache_reuses_owner_isolates_clients_and_releases_on_clear(self):
        session_a, session_b = ("lifetime-" + uuid4().hex for _ in range(2))
        self.session_ids.extend((session_a, session_b))
        with patch.object(sandbox, "_DEFAULT_BASE", self.base):
            app_a = AppTest.from_function(_owner_probe, args=(session_a,), default_timeout=60).run()
            self.healthy(app_a)
            a = self.remember(app_a.session_state[sandbox.SESSION_KEY])
            app_a.run(timeout=60)
            self.healthy(app_a)
            self.assertIs(app_a.session_state[sandbox.SESSION_KEY], a)
            self.assertFalse(a.owner.released)

            app_b = AppTest.from_function(_owner_probe, args=(session_b,), default_timeout=60).run()
            self.healthy(app_b)
            b = self.remember(app_b.session_state[sandbox.SESSION_KEY])
            self.assertIsNot(a.owner, b.owner)

            # Use the actual Streamlit disconnect-cache path, not a mock of
            # the application callback, so scope/on_release wiring is covered.
            clear_session_resource_cache(session_a)
            self.assertTrue(a.owner.released)
            self.assertNotIn(a.token, sandbox._registry)
            self.assertFalse(a.root.exists())
            self.assertFalse(b.owner.released)
            self.assertEqual(self.before[b.token], fingerprint(b.root))

            # Streamlit may reconnect with the same SessionState after clearing
            # its session cache. Old widget/page state must not reference A.
            app_a.session_state["lifetime_old_draft"] = "Synthetic stale session state"
            app_a.run(timeout=60)
            self.healthy(app_a)
            fresh = self.remember(app_a.session_state[sandbox.SESSION_KEY])
            self.assertIsNot(fresh.owner, a.owner)
            self.assertFalse(fresh.owner.released)
            self.assertNotEqual(fresh.root, a.root)
            self.assertNotIn("lifetime_old_draft", app_a.session_state)
            self.assertIs(app_b.session_state[sandbox.SESSION_KEY], b)

    def test_released_short_visits_do_not_exhaust_capacity_or_remove_another_owner(self):
        active_owner = self.owner()
        with patch.object(sandbox, "MAX_SESSIONS", 2):
            active = self.visitor(active_owner)
            for _ in range(5):
                owner = self.owner()
                temporary = self.visitor(owner)
                sandbox._release_session_owner(owner)
                self.assertTrue(owner.released)
                self.assertFalse(temporary.root.exists())
                self.assertNotIn(temporary.token, sandbox._registry)
                self.assertEqual(self.before[active.token], fingerprint(active.root))
                self.assertIs(sandbox._registry[active.token], active)

    def test_release_defers_an_active_context_until_it_finishes(self):
        owner = self.owner()
        handle = self.visitor(owner)
        with sandbox.sandbox_context(handle):
            sandbox._release_session_owner(owner)
            self.assertTrue(owner.released)
            self.assertTrue(handle.retired)
            self.assertEqual(handle.users, 1)
            self.assertTrue(handle.root.exists())
            self.assertEqual(self.before[handle.token], fingerprint(handle.root))
            sandbox._release_session_owner(owner)  # A repeated callback is safe.
            self.assertTrue(handle.root.exists())
        self.assertFalse(handle.root.exists())
        self.assertNotIn(handle.token, sandbox._registry)

    def test_connected_owner_survives_idle_ttl_while_unowned_store_expires(self):
        live = self.visitor(self.owner())
        unowned = self.visitor()
        now = time.time()
        live.touched_at = unowned.touched_at = now - sandbox.IDLE_TTL_SECONDS - 1
        self.assertEqual(sandbox.cleanup_expired_sandboxes(now=now, force=True), 1)
        self.assertTrue(live.root.exists())
        self.assertIs(sandbox._registry[live.token], live)
        self.assertFalse(unowned.root.exists())

    def test_real_full_capacity_rejects_new_owner_without_evicting_existing_data(self):
        with patch.object(sandbox, "MAX_SESSIONS", 2):
            a, b = self.visitor(self.owner()), self.visitor(self.owner())
            roots = set(self.base.iterdir())
            with self.assertRaises(sandbox.SandboxLimitError):
                self.visitor(self.owner())
            self.assertEqual(set(self.base.iterdir()), roots)
            self.assertIs(sandbox._registry[a.token], a)
            self.assertIs(sandbox._registry[b.token], b)
            # A different live session's handle cannot be supplied as a way
            # to borrow its slot or evict its store at capacity.
            with self.assertRaises(sandbox.SandboxLimitError):
                sandbox.create_sandbox(base_dir=self.base, _replaces=a, _owner=self.owner())
            self.assertEqual(set(self.base.iterdir()), roots)
            self.assertIs(sandbox._registry[a.token], a)
            self.assertIs(sandbox._registry[b.token], b)
            self.assertEqual(self.before[a.token], fingerprint(a.root))
            self.assertEqual(self.before[b.token], fingerprint(b.root))

    def test_released_owner_cannot_create_a_new_store(self):
        owner = self.owner()
        sandbox._release_session_owner(owner)
        roots = set(self.base.iterdir())
        with self.assertRaises(sandbox.SandboxAccessError):
            self.visitor(owner)
        self.assertEqual(set(self.base.iterdir()), roots)

    def test_reset_preserves_the_current_owner_and_releases_its_old_handle(self):
        import streamlit as st
        owner = self.owner()
        state = {}
        with patch.object(st, "session_state", state), \
                patch.object(sandbox, "_DEFAULT_BASE", self.base), \
                patch.object(sandbox, "_get_session_owner", return_value=owner):
            old = self.remember(sandbox.ensure_session_sandbox())
            self.assertIs(sandbox.ensure_session_sandbox(), old)
            fresh = self.remember(sandbox.reset_session_sandbox())
            self.assertIs(fresh.owner, owner)
            self.assertIs(state[sandbox.SESSION_KEY], fresh)
            self.assertNotEqual(fresh.root, old.root)
            self.assertFalse(old.root.exists())
            self.assertFalse(owner.released)

    def test_bare_code_has_no_session_owner(self):
        self.assertIsNone(sandbox._get_session_owner())


if __name__ == "__main__":
    unittest.main()
