"""Browser-owned mini window with controls for the existing session timer.

All frontend assets live here so source-only previews and portable releases
include them with their existing Python-file copy rules. No timer state or
records are written by this component, including when its window is closed.
"""
from __future__ import annotations

import math
import time

import streamlit as st


MINIWINDOW_KEY = "_proloag_timer_miniwindow"

_HTML = """
<div class="miniwindow-control">
  <button type="button" class="miniwindow-button">時間を小窓で表示</button>
  <p class="miniwindow-message" role="status" aria-live="polite"></p>
</div>
"""

_CSS = """
.miniwindow-control {
  font-family: var(--st-font, sans-serif);
  color: var(--ab-ink, var(--st-text-color, #152238));
}
.miniwindow-button {
  cursor: pointer;
  min-height: 2.5rem;
  padding: .4rem .85rem;
  border: 1px solid var(--ab-line, #d9e2ec);
  border-radius: .6rem;
  background: var(--ab-card, var(--st-background-color, #fff));
  color: inherit;
  font: inherit;
}
.miniwindow-button:hover:not(:disabled) {
  border-color: var(--ab-primary, #003b71);
}
.miniwindow-button:focus-visible {
  outline: 2px solid var(--ab-primary, #003b71);
  outline-offset: 2px;
}
.miniwindow-button:disabled { cursor: default; opacity: .6; }
.miniwindow-message {
  margin: .3rem 0 0;
  font-size: .82rem;
  color: var(--ab-muted, #667085);
}
.miniwindow-message:empty { display: none; }
.miniwindow-message[data-error="true"] { color: var(--ab-red, #b54747); }
"""

_JS = r"""
const instances = new Map();
const STALE_AFTER_MS = 15000;
const CONTROL_RETRY_AFTER_MS = 10000;

export function formatElapsed(value) {
  const seconds = Number.isFinite(value) ? Math.max(Math.floor(value), 0) : 0;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  return [hours, minutes, remainder].map(part => String(part).padStart(2, "0")).join(":");
}

export function elapsedAt(snapshot, receivedAt, now) {
  const age = Math.max(now - receivedAt, 0);
  const stale = age > STALE_AFTER_MS;
  return {
    seconds: snapshot.elapsedSeconds + (snapshot.running ? Math.min(age, STALE_AFTER_MS) / 1000 : 0),
    stale,
  };
}

function setMessage(state, message, error = false) {
  state.message = message;
  state.messageError = error;
  const node = state.parentElement.querySelector(".miniwindow-message");
  if (node) {
    node.textContent = message;
    node.dataset.error = String(error);
  }
}

function updateButton(state) {
  const button = state.parentElement.querySelector(".miniwindow-button");
  if (!button) return;
  button.disabled = !state.supported || state.opening;
  button.textContent = state.opening ? "小窓を開いています…" :
    state.pipWindow && !state.pipWindow.closed ? "開いている小窓を表示" : "時間を小窓で表示";
}

function updateTheme(state) {
  if (!state.pipWindow || state.pipWindow.closed) return;
  const button = state.parentElement.querySelector(".miniwindow-button");
  if (!button) return;
  // Read only our component's inherited colors; never inspect app DOM internals.
  const colors = state.ownerWindow.getComputedStyle(button);
  const defaults = { card: "#ffffff", ink: "#152238", muted: "#667085", green: "#2c7a5a", amber: "#b7791f" };
  for (const [name, fallback] of Object.entries(defaults)) {
    state.pipWindow.document.documentElement.style.setProperty(
      `--mini-${name}`, colors.getPropertyValue(`--ab-${name}`).trim() || fallback
    );
  }
}

function draw(state) {
  const pip = state.pipWindow;
  if (!pip || pip.closed) return;
  const now = state.ownerWindow.performance.now();
  const result = elapsedAt(state.snapshot, state.receivedAt, now);
  // A lost round trip may be retried, but only while fresh Python heartbeats
  // confirm the same revision. The page also rejects stale/duplicate requests.
  if (state.pending && !result.stale && now - state.pending.sentAt >= CONTROL_RETRY_AFTER_MS) {
    state.pending = null;
  }
  const display = pip.document.querySelector(".miniwindow-time");
  const status = pip.document.querySelector(".miniwindow-status");
  const control = pip.document.querySelector(".miniwindow-toggle");
  if (display) display.textContent = formatElapsed(result.seconds);
  if (status) {
    status.textContent = result.stale ? "同期が途切れました" : state.pending ? "操作を反映しています…" :
      state.snapshot.running ? "計測中" : state.snapshot.canResume ? "一時停止中" : "停止中";
    status.dataset.running = String(state.snapshot.running && !result.stale);
    status.dataset.stale = String(result.stale);
  }
  if (control) {
    control.textContent = state.snapshot.running ? "一時停止" : "再開";
    control.disabled = result.stale || Boolean(state.pending) ||
      (!state.snapshot.running && !state.snapshot.canResume);
    control.setAttribute("aria-busy", String(Boolean(state.pending)));
  }
}

function requestControl(state) {
  if (state.disposed || !state.pipWindow || state.pipWindow.closed) return;
  // Re-evaluate freshness here too: a click can arrive between drawing ticks.
  draw(state);
  const button = state.pipWindow.document.querySelector(".miniwindow-toggle");
  if (!button || button.disabled) return;
  const request = {
    action: state.snapshot.running ? "pause" : "resume",
    revision: state.snapshot.controlRevision,
  };
  state.pending = { sentAt: state.ownerWindow.performance.now() };
  draw(state);
  try {
    state.setTriggerValue("control", request);
  } catch (_error) {
    state.pending = null;
    setMessage(state, "小窓の操作を送信できませんでした。接続を確認して、もう一度操作してください。", true);
    draw(state);
  }
}

function stopDrawing(state, pip) {
  if (state.interval !== null) {
    pip.clearInterval(state.interval);
    state.interval = null;
  }
}

function dispose(state) {
  if (state.disposed) return;
  state.disposed = true;
  state.ownerWindow.removeEventListener("pagehide", state.onPageHide);
  const pip = state.pipWindow;
  state.pipWindow = null;
  if (pip) {
    stopDrawing(state, pip);
    if (!pip.closed) pip.close();
  }
  if (instances.get(state.key) === state) instances.delete(state.key);
}

async function openMiniwindow(state) {
  if (state.disposed || !state.supported || state.opening) return;
  if (state.pipWindow && !state.pipWindow.closed) {
    try {
      state.pipWindow.focus();
    } catch (_error) {
      setMessage(state, "小窓へ切り替えられませんでした。開いている小窓を選択してください。", true);
    }
    return;
  }
  state.opening = true;
  setMessage(state, "");
  updateButton(state);
  let pip;
  try {
    // This call stays in the user's click handler: no Python round trip or rerun.
    pip = await state.ownerWindow.documentPictureInPicture.requestWindow({ width: 280, height: 190 });
    if (state.disposed) {
      pip.close();
      return;
    }
    state.pipWindow = pip;
    const doc = pip.document;
    doc.title = "現在セッション｜PROLOAG";
    doc.documentElement.lang = "ja";
    const style = doc.createElement("style");
    style.textContent = `
      html { height: 100%; background: var(--mini-card); color: var(--mini-ink); }
      body { margin: 0; min-height: 100%; display: grid; place-items: center; font-family: system-ui, sans-serif; }
      .miniwindow-display { text-align: center; padding: 10px 12px; box-sizing: border-box; width: 100%; }
      .miniwindow-label { color: var(--mini-muted); font-size: 12px; margin: 0 0 2px; }
      .miniwindow-time { font-family: ui-monospace, Consolas, monospace; font-variant-numeric: tabular-nums; font-size: clamp(24px, 14vw, 42px); line-height: 1.25; }
      .miniwindow-status { color: var(--mini-muted); font-size: 12px; margin-top: 4px; }
      .miniwindow-status[data-running="true"] { color: var(--mini-green); }
      .miniwindow-status[data-stale="true"] { color: var(--mini-amber); }
      .miniwindow-toggle { margin-top: 10px; min-height: 34px; padding: 5px 22px; border: 1px solid var(--mini-muted); border-radius: 7px; background: var(--mini-card); color: var(--mini-ink); font: inherit; font-size: 13px; cursor: pointer; }
      .miniwindow-toggle:hover:not(:disabled) { border-color: var(--mini-green); }
      .miniwindow-toggle:focus-visible { outline: 2px solid var(--mini-green); outline-offset: 3px; }
      .miniwindow-toggle:disabled { cursor: default; opacity: .5; }
    `;
    doc.head.appendChild(style);
    const content = doc.createElement("div");
    content.className = "miniwindow-display";
    const label = doc.createElement("p");
    label.className = "miniwindow-label";
    label.textContent = "現在セッション";
    const time = doc.createElement("div");
    time.className = "miniwindow-time";
    time.setAttribute("role", "timer");
    time.setAttribute("aria-label", "現在セッションの時間");
    const status = doc.createElement("div");
    status.className = "miniwindow-status";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    const control = doc.createElement("button");
    control.className = "miniwindow-toggle";
    control.setAttribute("type", "button");
    control.onclick = () => requestControl(state);
    content.appendChild(label);
    content.appendChild(time);
    content.appendChild(status);
    content.appendChild(control);
    doc.body.appendChild(content);
    pip.addEventListener("pagehide", () => {
      if (state.pipWindow !== pip) return;
      stopDrawing(state, pip);
      state.pipWindow = null;
      if (!state.disposed) {
        setMessage(state, "小窓を閉じても、本体の計測は続きます。");
        updateButton(state);
      }
    }, { once: true });
    updateTheme(state);
    draw(state);
    state.interval = pip.setInterval(() => draw(state), 200);
    setMessage(state, "小窓から一時停止・再開できます。時間の補正と保存は本体で行ってください。");
  } catch (_error) {
    if (pip && !pip.closed) pip.close();
    state.pipWindow = null;
    if (!state.disposed) {
      setMessage(state, "小窓を開けませんでした。ChromeまたはEdgeで開き直し、「時間を小窓で表示」をもう一度押してください。", true);
    }
  } finally {
    state.opening = false;
    if (!state.disposed) updateButton(state);
  }
}

export default function(component) {
  const { key, parentElement, data } = component;
  const ownerWindow = parentElement.ownerDocument.defaultView;
  let state = instances.get(key);
  if (!state || state.disposed) {
    state = {
      key, parentElement, ownerWindow, pipWindow: null, interval: null,
      disposed: false, opening: false, message: "", messageError: false,
      pending: null,
      supported: typeof ownerWindow.documentPictureInPicture?.requestWindow === "function",
    };
    state.onPageHide = () => dispose(state);
    ownerWindow.addEventListener("pagehide", state.onPageHide);
    instances.set(key, state);
  }
  state.parentElement = parentElement;
  state.setTriggerValue = (name, value) => component.setTriggerValue(name, value);
  const revision = Number.isSafeInteger(data?.control_revision) && data.control_revision >= 0 ? data.control_revision : 0;
  if (state.snapshot && state.snapshot.controlRevision !== revision) state.pending = null;
  state.snapshot = {
    elapsedSeconds: Number.isFinite(data?.elapsed_seconds) ? Math.max(data.elapsed_seconds, 0) : 0,
    running: data?.running === true,
    controlRevision: revision,
    canResume: data?.can_resume === true,
  };
  state.receivedAt = ownerWindow.performance.now();
  const button = parentElement.querySelector(".miniwindow-button");
  button.onclick = () => openMiniwindow(state);
  if (!state.supported) {
    setMessage(state, "このブラウザでは小窓表示を利用できません。対応するChromeまたはEdgeで開いてください。");
  } else {
    setMessage(state, state.message, state.messageError);
  }
  updateButton(state);
  updateTheme(state);
  draw(state);
  // Streamlit v2 calls the renderer on data updates and this cleanup on unmount.
  // A stable Python key keeps the same owner and PiP window across reruns.
  return () => dispose(state);
}
"""


def _get_component():
    """Register identical assets in the current runtime, keeping one mount key.

    Streamlit permits re-registering the same name. A process-level cached
    callable can outlive the runtime registry (notably in AppTest), so use the
    public registration API here rather than retaining a stale registration.
    """
    return st.components.v2.component(
        "proloag_timer_miniwindow", html=_HTML, css=_CSS, js=_JS,
    )


def _control_changed() -> None:
    """Register the trigger; the page consumes it outside callback execution."""


def render_timer_miniwindow(
    elapsed_seconds: float,
    running: bool,
    *,
    control_revision: int = 0,
    can_resume: bool = False,
) -> dict | None:
    """Update the snapshot and return an explicit mini-window control request."""
    try:
        elapsed = float(elapsed_seconds)
    except (TypeError, ValueError, OverflowError):
        elapsed = 0.0
    if not math.isfinite(elapsed):
        elapsed = 0.0
    if not hasattr(st.components, "v2"):
        st.caption("現在のアプリ環境では小窓表示を利用できません。")
        return
    revision = control_revision if type(control_revision) is int and 0 <= control_revision <= 2**53 - 1 else 0
    result = _get_component()(
        key=MINIWINDOW_KEY,
        # Paused snapshots otherwise repeat unchanged and Streamlit can skip
        # their frontend render. Keep liveness separate from control revisions;
        # this opaque token is never a timer value or a browser control event.
        data={"elapsed_seconds": max(elapsed, 0.0), "running": bool(running),
              "control_revision": revision, "can_resume": bool(can_resume),
              "heartbeat": str(time.monotonic_ns())},
        on_control_change=_control_changed,
        width="content",
    )
    request = result.get("control")
    return request if isinstance(request, dict) else None
