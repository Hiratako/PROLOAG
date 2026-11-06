from __future__ import annotations

import json
import shutil
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from prolog_ui import timer_miniwindow as mini


_NODE_BEHAVIOR_TEST = r"""
import assert from "node:assert/strict";
const { default: render, formatElapsed, elapsedAt } = await import(
  "data:text/javascript;base64," + Buffer.from(source, "utf8").toString("base64")
);

class Element {
  constructor(doc) {
    this.ownerDocument = doc;
    this.children = [];
    this.className = "";
    this.dataset = {};
    this.textContent = "";
    this.properties = {};
    this.style = { setProperty: (key, value) => { this.properties[key] = value; } };
  }
  appendChild(child) { this.children.push(child); return child; }
  setAttribute(key, value) { this[key] = value; }
  querySelector(selector) {
    if (selector.startsWith(".") && this.className.split(" ").includes(selector.slice(1))) return this;
    for (const child of this.children) {
      const result = child.querySelector(selector);
      if (result) return result;
    }
    return null;
  }
}

class Window {
  constructor() {
    this.now = 0;
    this.closed = false;
    this.focusCount = 0;
    this.listeners = new Map();
    this.intervals = new Map();
    this.nextInterval = 1;
    this.performance = { now: () => this.now };
    const doc = this.document = { defaultView: this };
    doc.createElement = () => new Element(doc);
    doc.documentElement = new Element(doc);
    doc.head = new Element(doc);
    doc.body = new Element(doc);
    doc.querySelector = selector => doc.body.querySelector(selector);
  }
  getComputedStyle() { return { getPropertyValue: () => "" }; }
  addEventListener(type, listener) {
    const list = this.listeners.get(type) ?? new Set();
    list.add(listener);
    this.listeners.set(type, list);
  }
  removeEventListener(type, listener) { this.listeners.get(type)?.delete(listener); }
  dispatch(type) { for (const listener of [...(this.listeners.get(type) ?? [])]) listener(); }
  setInterval(callback) { const id = this.nextInterval++; this.intervals.set(id, callback); return id; }
  clearInterval(id) { this.intervals.delete(id); }
  tick() { for (const callback of this.intervals.values()) callback(); }
  focus() { this.focusCount++; }
  close() { this.closed = true; this.dispatch("pagehide"); }
}

function mounted(owner, key, data) {
  const parentElement = new Element(owner.document);
  const button = new Element(owner.document);
  button.className = "miniwindow-button";
  const message = new Element(owner.document);
  message.className = "miniwindow-message";
  parentElement.appendChild(button);
  parentElement.appendChild(message);
  const events = [];
  const component = { key, parentElement, data,
    setStateValue: () => { throw Error("miniwindow must only send explicit control triggers"); },
    setTriggerValue: (event, value) => events.push({event, value}),
  };
  return { component, button, message, events, cleanup: render(component) };
}

assert.equal(formatElapsed(33037), "09:10:37");
assert.equal(formatElapsed(360000), "100:00:00");
assert.equal(formatElapsed(-60), "00:00:00");
assert.equal(formatElapsed(NaN), "00:00:00");
assert.deepEqual(elapsedAt({elapsedSeconds: 5, running: false}, 0, 50000), {seconds: 5, stale: true});
assert.deepEqual(elapsedAt({elapsedSeconds: 5, running: true}, 0, 50000), {seconds: 20, stale: true});
assert.deepEqual(elapsedAt({elapsedSeconds: 5, running: true}, 1000, 500), {seconds: 5, stale: false});

const owner = new Window();
const pip = new Window();
let requests = 0;
owner.documentPictureInPicture = { requestWindow: async options => {
  requests++;
  assert.deepEqual(options, {width: 280, height: 190});
  return pip;
} };
const data = Object.freeze({elapsed_seconds: 33037.25, running: true});
const view = mounted(owner, "active", data);
assert.equal(requests, 0, "mounting must never open a window without a user click");
await view.button.onclick();
assert.equal(requests, 1);
assert.equal(pip.document.querySelector(".miniwindow-time").textContent, "09:10:37");
owner.now = 1800;
pip.tick();
assert.equal(pip.document.querySelector(".miniwindow-time").textContent, "09:10:39");
await view.button.onclick();
assert.equal(requests, 1, "second click focuses the existing window");
assert.equal(pip.focusCount, 1);

// Actual v2 renderer data updates preserve the existing PiP and its one interval.
view.component.data = {elapsed_seconds: 33040, running: false, can_resume: true};
const pausedCleanup = render(view.component);
assert.equal(pip.closed, false);
assert.equal(pip.intervals.size, 1);
owner.now = 40000;
pip.tick();
assert.equal(pip.document.querySelector(".miniwindow-time").textContent, "09:10:40");
assert.equal(pip.document.querySelector(".miniwindow-status").textContent, "同期が途切れました");
assert.equal(pip.document.querySelector(".miniwindow-toggle").disabled, true);

// Resume, correction, synchronization loss, recovery and reset are display updates.
view.component.data = {elapsed_seconds: 32980, running: true};
render(view.component);
assert.equal(pip.document.querySelector(".miniwindow-time").textContent, "09:09:40");
owner.now += 14900;
pip.tick();
const beforeStale = pip.document.querySelector(".miniwindow-time").textContent;
owner.now += 200;
pip.tick();
assert.equal(pip.document.querySelector(".miniwindow-time").textContent, "09:09:55");
assert.ok(pip.document.querySelector(".miniwindow-time").textContent >= beforeStale);
assert.equal(pip.document.querySelector(".miniwindow-status").textContent, "同期が途切れました");
owner.now += 500000;
pip.tick();
assert.equal(pip.document.querySelector(".miniwindow-time").textContent, "09:09:55");
view.component.data = {elapsed_seconds: 33900, running: true};
render(view.component);
assert.equal(pip.document.querySelector(".miniwindow-status").textContent, "計測中");
assert.equal(pip.document.querySelector(".miniwindow-time").textContent, "09:25:00");
view.component.data = {elapsed_seconds: 0, running: false};
render(view.component);
assert.equal(pip.document.querySelector(".miniwindow-time").textContent, "00:00:00");
assert.deepEqual(data, {elapsed_seconds: 33037.25, running: true});
assert.deepEqual(view.events, [], "updates, local ticks and reset snapshots never trigger Python");
pausedCleanup();
assert.equal(pip.closed, true, "page unmount closes its display-only window");
assert.equal(pip.intervals.size, 0);
assert.equal(owner.listeners.get("pagehide").size, 0);
assert.deepEqual(view.events, []);

// Explicit control requests wait for authoritative snapshots, never toggling locally.
const controlOwner = new Window();
const controlPip = new Window();
controlOwner.documentPictureInPicture = {requestWindow: async () => controlPip};
const controls = mounted(controlOwner, "controls", {
  elapsed_seconds: 120, running: true, control_revision: 8, can_resume: false,
});
await controls.button.onclick();
const toggle = controlPip.document.querySelector(".miniwindow-toggle");
const status = controlPip.document.querySelector(".miniwindow-status");
assert.equal(toggle.type, "button");
assert.equal(toggle.textContent, "一時停止");
assert.equal(toggle.disabled, false);
toggle.onclick();
assert.deepEqual(controls.events, [{event: "control", value: {action: "pause", revision: 8}}]);
assert.equal(toggle.disabled, true);
assert.equal(toggle.textContent, "一時停止", "no optimistic change before Python acknowledgement");
assert.equal(status.textContent, "操作を反映しています…");
toggle.onclick();
assert.equal(controls.events.length, 1, "duplicate clicks are gated");
controlOwner.now = 6000;
controls.component.data = {elapsed_seconds: 126, running: true, control_revision: 8, can_resume: false};
render(controls.component);
assert.equal(toggle.disabled, true, "same-revision heartbeat does not acknowledge a request");
controlOwner.now = 10001;
controlPip.tick();
assert.equal(toggle.disabled, false, "fresh heartbeat allows a lost request to be retried after ten seconds");
toggle.onclick();
assert.equal(controls.events.length, 2);
assert.deepEqual(controls.events[1], controls.events[0]);
controls.component.data = {elapsed_seconds: 130, running: false, control_revision: 9, can_resume: true};
render(controls.component);
assert.equal(toggle.textContent, "再開");
assert.equal(toggle.disabled, false);
assert.equal(status.textContent, "一時停止中");
toggle.onclick();
assert.deepEqual(controls.events.at(-1), {event: "control", value: {action: "resume", revision: 9}});
assert.equal(controlPip.document.querySelector(".miniwindow-time").textContent, "00:02:10");
assert.equal(toggle.disabled, true);
controls.component.data = {elapsed_seconds: 130, running: true, control_revision: 10, can_resume: false};
render(controls.component);
assert.equal(toggle.textContent, "一時停止");
assert.equal(toggle.disabled, false);

// Both idle and reset-pending sessions block resume; a stale snapshot blocks either action.
controls.component.data = {elapsed_seconds: 0, running: false, control_revision: 11, can_resume: false};
render(controls.component);
const beforeBlocked = controls.events.length;
toggle.onclick();
assert.equal(toggle.disabled, true);
assert.equal(controls.events.length, beforeBlocked);
controls.component.data = {elapsed_seconds: 130, running: false, control_revision: 12, can_resume: false};
render(controls.component);
toggle.onclick();
assert.equal(controls.events.length, beforeBlocked, "reset pending cannot start another timer");
controls.component.data = {elapsed_seconds: 130, running: false, control_revision: 13, can_resume: true};
render(controls.component);
controlOwner.now += 15001;
// Click before the drawing interval also checks staleness.
toggle.onclick();
assert.equal(toggle.disabled, true);
assert.equal(status.textContent, "同期が途切れました");
assert.equal(controls.events.length, beforeBlocked);
controls.component.data = {elapsed_seconds: 130, running: true, control_revision: 14, can_resume: false};
render(controls.component);
controlOwner.now += 15001;
controlPip.tick();
toggle.onclick();
assert.equal(controls.events.length, beforeBlocked);
controls.component.data = {elapsed_seconds: 145, running: true, control_revision: 14, can_resume: false};
render(controls.component);
assert.equal(toggle.disabled, false, "fresh heartbeat recovers controls");
toggle.onclick();
controlOwner.now += 20000;
controlPip.tick();
assert.equal(toggle.disabled, true, "pending timeout cannot permit a retry without a fresh heartbeat");
const beforeControlClose = controls.events.length;
controlPip.close();
toggle.onclick();
controls.cleanup();
toggle.onclick();
assert.equal(controls.events.length, beforeControlClose, "closing or unmounting sends no control event");
assert.equal(controlPip.intervals.size, 0);

// Paused snapshots must still reach the renderer when Streamlit skips identical data.
const heartbeatOwner = new Window();
const heartbeatPip = new Window();
heartbeatOwner.documentPictureInPicture = {requestWindow: async () => heartbeatPip};
const heartbeat = mounted(heartbeatOwner, "paused-heartbeat", {
  elapsed_seconds: 130, running: false, control_revision: 13, can_resume: true, heartbeat: "0",
});
await heartbeat.button.onclick();
let previousPayload = JSON.stringify(heartbeat.component.data);
for (let seconds = 1; seconds <= 180; seconds++) {
  heartbeatOwner.now = seconds * 1000;
  heartbeat.component.data = {
    elapsed_seconds: 130, running: false, control_revision: 13, can_resume: true,
    heartbeat: String(seconds),
  };
  const nextPayload = JSON.stringify(heartbeat.component.data);
  if (nextPayload !== previousPayload) render(heartbeat.component);
  previousPayload = nextPayload;
  heartbeatPip.tick();
  assert.equal(heartbeatPip.document.querySelector(".miniwindow-status").textContent, "一時停止中");
  assert.equal(heartbeatPip.document.querySelector(".miniwindow-time").textContent, "00:02:10");
  assert.equal(heartbeatPip.document.querySelector(".miniwindow-toggle").disabled, false);
}
assert.deepEqual(heartbeat.events, [], "heartbeats never send control requests");
heartbeatPip.document.querySelector(".miniwindow-toggle").onclick();
assert.deepEqual(heartbeat.events, [{event: "control", value: {action: "resume", revision: 13}}]);
heartbeatOwner.now += 15001;
heartbeatPip.tick();
assert.equal(heartbeatPip.document.querySelector(".miniwindow-status").textContent, "同期が途切れました");
assert.equal(heartbeatPip.document.querySelector(".miniwindow-toggle").disabled, true,
  "real heartbeat loss still blocks stale commands");
heartbeat.cleanup();

const failedOwner = new Window();
const failedPip = new Window();
failedOwner.documentPictureInPicture = {requestWindow: async () => failedPip};
const failed = mounted(failedOwner, "failed-control", {elapsed_seconds: 1, running: true});
await failed.button.onclick();
failed.component.setTriggerValue = () => { throw Error("connection lost"); };
failedPip.document.querySelector(".miniwindow-toggle").onclick();
assert.match(failed.message.textContent, /操作を送信できませんでした/);
assert.equal(failedPip.document.querySelector(".miniwindow-toggle").disabled, false);
failed.cleanup();

const mainCloseOwner = new Window();
const mainClosePip = new Window();
mainCloseOwner.documentPictureInPicture = {requestWindow: async () => mainClosePip};
const mainClose = mounted(mainCloseOwner, "pagehide", {elapsed_seconds: 1, running: true});
await mainClose.button.onclick();
mainCloseOwner.dispatch("pagehide");
assert.equal(mainClosePip.closed, true);
assert.equal(mainClosePip.intervals.size, 0);

const manualOwner = new Window();
const manualPip = new Window();
manualOwner.documentPictureInPicture = {requestWindow: async () => manualPip};
const manualData = Object.freeze({elapsed_seconds: 10, running: true});
const manual = mounted(manualOwner, "manual", manualData);
await manual.button.onclick();
manualPip.close();
assert.deepEqual(manualData, {elapsed_seconds: 10, running: true});
assert.equal(manualPip.intervals.size, 0);
assert.equal(manual.button.textContent, "時間を小窓で表示");
assert.match(manual.message.textContent, /本体の計測は続きます/);
manual.cleanup();

const unsupported = mounted(new Window(), "unsupported", {elapsed_seconds: 0, running: false});
assert.equal(unsupported.button.disabled, true);
assert.match(unsupported.message.textContent, /このブラウザでは/);
unsupported.cleanup();

const deniedOwner = new Window();
deniedOwner.documentPictureInPicture = {requestWindow: async () => { throw new Error("Permission denied"); }};
const denied = mounted(deniedOwner, "denied", {elapsed_seconds: 10, running: true});
await denied.button.onclick();
assert.equal(denied.button.disabled, false);
assert.match(denied.message.textContent, /小窓を開けませんでした/);
assert.equal(denied.message.dataset.error, "true");
denied.cleanup();

// Opening is asynchronous: duplicate clicks or leaving the page must not orphan a window.
const openingOwner = new Window();
const latePip = new Window();
let resolveOpening;
let openingRequests = 0;
openingOwner.documentPictureInPicture = {requestWindow: () => {
  openingRequests++;
  return new Promise(resolve => { resolveOpening = resolve; });
}};
const opening = mounted(openingOwner, "opening", {elapsed_seconds: 10, running: true});
const firstOpen = opening.button.onclick();
await opening.button.onclick();
assert.equal(openingRequests, 1);
assert.equal(opening.button.disabled, true);
opening.cleanup();
resolveOpening(latePip);
await firstOpen;
assert.equal(latePip.closed, true);
assert.equal(latePip.intervals.size, 0);
console.log("Miniwindow frontend behavior passed");
"""


class TimerMiniwindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit import config
        config.get_config_options(options_from_flags={
            "global.developmentMode": False, "browser.gatherUsageStats": False,
        })

    def test_snapshot_uses_stable_key_and_registers_only_control_callback(self):
        renderer = Mock(return_value={})
        with patch.object(mini, "_get_component", return_value=renderer), \
             patch.object(mini.time, "monotonic_ns", side_effect=[100, 200]):
            self.assertIsNone(mini.render_timer_miniwindow(33037.25, True, control_revision=3))
            self.assertIsNone(mini.render_timer_miniwindow(32977.25, False, control_revision=4, can_resume=True))
        first, second = [call.kwargs for call in renderer.call_args_list]
        self.assertEqual(first["key"], second["key"])
        self.assertEqual(first["data"], {"elapsed_seconds": 33037.25, "running": True,
                                         "control_revision": 3, "can_resume": False, "heartbeat": "100"})
        self.assertEqual(second["data"], {"elapsed_seconds": 32977.25, "running": False,
                                          "control_revision": 4, "can_resume": True, "heartbeat": "200"})
        self.assertEqual(first["on_control_change"], mini._control_changed)
        self.assertEqual([key for key in first if key.startswith("on_")], ["on_control_change"])

    def test_paused_snapshots_change_only_heartbeat_and_keep_the_control_revision(self):
        renderer = Mock(return_value={})
        with patch.object(mini, "_get_component", return_value=renderer), \
             patch.object(mini.time, "monotonic_ns", side_effect=[300, 400]):
            for _ in range(2):
                self.assertIsNone(mini.render_timer_miniwindow(130, False, control_revision=13, can_resume=True))
        first, second = [call.kwargs for call in renderer.call_args_list]
        self.assertEqual(first["key"], second["key"])
        self.assertNotEqual(json.dumps(first["data"]), json.dumps(second["data"]))
        self.assertEqual(first["data"].pop("heartbeat"), "300")
        self.assertEqual(second["data"].pop("heartbeat"), "400")
        self.assertEqual(first["data"], second["data"])
        self.assertEqual(first["data"]["control_revision"], 13)

    def test_trigger_is_returned_for_page_to_validate_without_executing_callback(self):
        request = {"action": "pause", "revision": 3}
        renderer = Mock(return_value={"control": request})
        with patch.object(mini, "_get_component", return_value=renderer):
            self.assertIs(mini.render_timer_miniwindow(10, True), request)
            for result in ({}, {"control": None}, {"control": "pause"}, {"control": []}):
                renderer.return_value = result
                self.assertIsNone(mini.render_timer_miniwindow(10, True))
        # The callback intentionally does nothing; handling in the page body is
        # necessary because Streamlit suppresses st.rerun inside callbacks.
        with patch.object(mini.st, "rerun") as rerun:
            mini._control_changed()
        rerun.assert_not_called()

    def test_nonfinite_or_invalid_time_never_reaches_frontend(self):
        renderer = Mock()
        with patch.object(mini, "_get_component", return_value=renderer):
            for invalid in (-60, float("inf"), float("nan"), None, "invalid", 10 ** 500):
                mini.render_timer_miniwindow(invalid, False)
        for call in renderer.call_args_list:
            self.assertRegex(call.kwargs["data"].pop("heartbeat"), r"^\d+$")
            self.assertEqual(call.kwargs["data"], {"elapsed_seconds": 0.0, "running": False,
                                                   "control_revision": 0, "can_resume": False})

    def test_revision_must_be_a_javascript_safe_nonnegative_integer(self):
        renderer = Mock(return_value={})
        with patch.object(mini, "_get_component", return_value=renderer):
            for invalid in (-1, "3", None, True, float("inf"), 2**53):
                mini.render_timer_miniwindow(1, True, control_revision=invalid)
        for call in renderer.call_args_list:
            self.assertEqual(call.kwargs["data"]["control_revision"], 0)

    def test_registration_targets_current_runtime_and_has_all_assets_inline(self):
        factory = Mock(return_value=Mock())
        fake_st = SimpleNamespace(components=SimpleNamespace(v2=SimpleNamespace(component=factory)))
        with patch.object(mini, "st", fake_st):
            mini._get_component()
            mini._get_component()
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(factory.call_args.kwargs["js"], mini._JS)
        self.assertEqual(factory.call_args.kwargs["css"], mini._CSS)
        self.assertEqual(factory.call_args.kwargs["html"], mini._HTML)

    def test_real_streamlit_reruns_keep_component_identity_and_registration(self):
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_string("""
import streamlit as st
from prolog_ui.timer_miniwindow import render_timer_miniwindow
elapsed = st.number_input("Synthetic elapsed", value=10, key="elapsed")
render_timer_miniwindow(elapsed, True)
""", default_timeout=60).run()
        self.assertFalse(app.exception)
        first_id = app.get("bidi_component")[0].proto.id
        app.number_input(key="elapsed").set_value(70).run()
        self.assertFalse(app.exception)
        self.assertEqual(app.get("bidi_component")[0].proto.id, first_id)
        data = json.loads(app.get("bidi_component")[0].proto.json)
        self.assertRegex(data.pop("heartbeat"), r"^\d+$")
        self.assertEqual(data, {"elapsed_seconds": 70.0, "running": True,
                                "control_revision": 0, "can_resume": False})

    def test_real_streamlit_paused_reruns_emit_fresh_data_with_the_same_component_identity(self):
        from streamlit.testing.v1 import AppTest

        # Windows Python 3.11's monotonic clock uses GetTickCount64 (15.625 ms
        # granularity), so two fast AppTest runs can legitimately share a tick.
        # Advance only the component's clock deterministically; leave the real
        # Streamlit registration and the process-wide time module unchanged.
        heartbeat_clock = Mock(side_effect=[1_000_000_000, 2_000_000_000])
        with patch.object(mini, "time", SimpleNamespace(monotonic_ns=heartbeat_clock)):
            app = AppTest.from_string("""
from prolog_ui.timer_miniwindow import render_timer_miniwindow
render_timer_miniwindow(130, False, control_revision=13, can_resume=True)
""", default_timeout=60).run()
            self.assertFalse(app.exception)
            first = app.get("bidi_component")[0].proto
            first_id, first_json = first.id, first.json
            app.run()
            self.assertFalse(app.exception)
            second = app.get("bidi_component")[0].proto
        self.assertEqual(heartbeat_clock.call_count, 2)
        self.assertEqual(first_id, second.id)
        self.assertNotEqual(first_json, second.json)
        first_data, second_data = json.loads(first_json), json.loads(second.json)
        self.assertEqual(first_data.pop("heartbeat"), "1000000000")
        self.assertEqual(second_data.pop("heartbeat"), "2000000000")
        self.assertEqual(first_data, second_data)
        self.assertEqual(first_data, {"elapsed_seconds": 130.0, "running": False,
                                     "control_revision": 13, "can_resume": True})

    def test_missing_component_api_leaves_the_timer_usable(self):
        fake_st = SimpleNamespace(components=SimpleNamespace(), caption=Mock())
        with patch.object(mini, "st", fake_st), patch.object(mini, "_get_component") as factory:
            mini.render_timer_miniwindow(10, True)
        factory.assert_not_called()
        fake_st.caption.assert_called_once()

    @unittest.skipUnless(shutil.which("node"), "Node is needed only to verify frontend behavior")
    def test_actual_frontend_lifecycle_and_timer_synchronization(self):
        script = "const source = " + json.dumps(mini._JS, ensure_ascii=False) + ";\n" + _NODE_BEHAVIOR_TEST
        result = subprocess.run(
            [shutil.which("node"), "--input-type=module"], input=script, text=True,
            encoding="utf-8", capture_output=True, timeout=20, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Miniwindow frontend behavior passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
