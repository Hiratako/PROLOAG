"""Controls for a durable PC recording worker, without opening a UI-side mic."""
from __future__ import annotations

import math
from pathlib import Path
import shutil
import sys
from uuid import uuid4

import streamlit as st

import prolog_ui.components as ab
from app_core.long_recording import LongRecordingStore, dependency_dir, engine_available
from app_core.media_conversion import MediaError


SELECTED = "long_recording_selected"
HISTORY_WIDGET = "long_recording_history"
PENDING_START = "long_recording_pending_start"
OPENED = "long_recording_opened_memos"
STATUS = {"starting": "開始準備中", "recording": "録音中", "paused": "一時停止中",
          "stopping": "停止・保存中", "finalizing": "音声をまとめています", "stopped": "録音保存済み",
          "done": "時間上限に達して録音保存済み", "interrupted": "録音が中断されました", "failed": "録音できませんでした",
          "unavailable": "録音状態を確認できません"}
ACTIVE = {"starting", "recording", "paused", "stopping", "finalizing", "unavailable"}


def _number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (ValueError, TypeError):
        return default


def _time(value):
    seconds = max(0, int(_number(value)))
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _active(state):
    return bool(state.get("active")) or state.get("status") in ACTIVE


def _setup(root, available):
    with st.expander("PC録音の準備・保存場所", expanded=not available):
        if available:
            st.caption("PC録音エンジンを検出しました。")
        else:
            st.info("PC録音には初回セットアップが必要です。アプリと同じPythonで次のコマンドを実行してください。")
        st.code(f'& "{sys.executable}" -B "{root / "app_core/setup_recording.py"}"', language="powershell")
        st.caption("録音用ライブラリ：" + str(dependency_dir(root)))
        st.caption("この画面を開くだけでは、インストールやマイクの録音は始まりません。")
        if st.button("PC録音の準備状態を再確認", key="long_recording_setup_refresh"):
            st.rerun()


def _space(root, state=None):
    free = (state or {}).get("free_bytes")
    if free is None:
        try:
            free = shutil.disk_usage(root).free
        except OSError:
            pass
    if free is not None:
        st.caption(f"保存先の空き容量：約{max(0, _number(free)) / 1024 ** 3:.1f} GiB")


def _start(store, root, history, available):
    busy = any(_active(row) for row in history)
    if busy:
        st.info("PC録音が進行中です。下の履歴から状態を確認・操作できます。")
        active_id = next(row["id"] for row in history if _active(row))
        if st.button("進行中のPC録音を表示", key="long_recording_show_active"):
            st.session_state[HISTORY_WIDGET] = active_id
            st.session_state[SELECTED] = active_id
            st.rerun()
    title = st.text_input("PC録音のタイトル（任意）", key="long_recording_title", max_chars=200)
    if st.button("マイク一覧を確認", key="long_recording_devices", disabled=not available):
        try:
            st.session_state["long_recording_device_cache"] = store.devices()
        except (MediaError, OSError, ValueError) as exc:
            ab.show_action_error("マイク一覧を確認できませんでした。" + str(exc))
    devices = st.session_state.get("long_recording_device_cache", [])
    by_id = {int(row["id"]): row for row in devices}
    options = [None, *by_id]
    device_key = "long_recording_device_choice"
    if st.session_state.get(device_key) not in options:
        st.session_state[device_key] = None

    def label(value):
        if value is None:
            return "既定のマイク"
        row = by_id[value]
        return str(row.get("name") or "マイク") + ("（既定）" if row.get("default") else "")

    device = st.selectbox("録音するマイク", options, format_func=label, key=device_key)
    prevent_sleep = st.checkbox("録音中にPCをスリープさせない", value=True, key="long_recording_prevent_sleep")
    st.caption("手動のスリープ・ふたを閉じる操作・機器切断等による中断までは防げません。")
    _space(root)
    if st.button("PCで録音を開始", key="long_recording_start", type="primary", disabled=busy or not available):
        request = {"title": title, "device": device, "max_seconds": 10800, "prevent_sleep": prevent_sleep}
        pending = st.session_state.get(PENDING_START)
        if pending is None or pending["request"] != request:
            pending = {"request": request, "operation_id": uuid4().hex}
            st.session_state[PENDING_START] = pending
        try:
            created = store.start(**request, operation_id=pending["operation_id"])
        except (MediaError, OSError, ValueError) as exc:
            ab.show_action_error("PC録音を開始できませんでした。同じ操作として再確認できます。" + str(exc))
        else:
            st.session_state.pop(PENDING_START, None)
            st.session_state[SELECTED] = created["id"]
            st.session_state[HISTORY_WIDGET] = created["id"]
            st.rerun()


def _statistics(root, state):
    status = state.get("status", "")
    st.markdown("**" + STATUS.get(status, str(status)) + "**")
    columns = st.columns(3)
    columns[0].metric("録音時間", _time(state.get("duration")))
    columns[1].metric("保存済み音声", _time(state.get("saved_duration")))
    columns[2].metric("開始からの経過", _time(state.get("elapsed")))
    st.caption("録音時間は一時停止中に増えません。累計3時間で自動停止します。")
    if _active(state) and state.get("status") != "unavailable":
        st.progress(min(1.0, max(0.0, _number(state.get("level")))), text="マイク入力レベル")
    last_saved = str(state.get("last_saved_at") or "まだ保存されていません").replace("T", " ")
    st.caption("最後に保存した時刻：" + last_saved)
    _space(root, state)
    if state.get("error"):
        st.warning(str(state["error"]))
    if state.get("import_error"):
        st.warning("音声は保存されていますが、メモへの取り込みが完了していません。" + str(state["import_error"]))


def _controls(store, state):
    recording_id, status = state["id"], state.get("status")
    if status not in {"starting", "recording", "paused"}:
        return
    pause_col, stop_col = st.columns(2)
    action = None
    if status == "recording" and pause_col.button("録音を一時停止", key="long_recording_pause_" + recording_id):
        action = store.pause
    elif status == "paused" and pause_col.button("録音を再開", key="long_recording_resume_" + recording_id):
        action = store.resume
    if stop_col.button("録音を停止して保存", key="long_recording_stop_" + recording_id, type="primary"):
        action = store.stop
    if action is not None:
        try:
            action(recording_id)
        except (MediaError, OSError, ValueError) as exc:
            ab.show_action_error("録音への操作を反映できませんでした。" + str(exc))
        else:
            st.rerun()


def _open_memo(state, callback, *, force=False):
    memo_id = state.get("memo_id")
    if not memo_id or callback is None:
        return
    pair = state["id"] + ":" + memo_id
    opened = st.session_state.setdefault(OPENED, [])
    if pair not in opened or force:
        callback(memo_id)
        if pair not in opened:
            opened.append(pair)


def _terminal(store, state, on_memo_ready):
    recording_id = state["id"]
    memo_id = state.get("memo_id")
    if memo_id:
        st.success("録音をメモへ取り込み済みです。下の録音メモで文字起こし・編集できます。")
        _open_memo(state, on_memo_ready)
        if st.button("この録音のメモを開く", key="long_recording_open_" + recording_id):
            _open_memo(state, on_memo_ready, force=True)
    if state.get("status") == "unavailable":
        st.warning("状態が不明な録音は操作しません。音声ファイルを削除せず、アプリの状態を確認してください。")
        return
    if not memo_id:
        st.info("保存済み音声はまだメモへ取り込まれていません。下のボタンから取り込みを確認・復旧できます。")
    st.caption("中断した録音は保存済みの区間から取り込み直せます。保存前だった区間の復元は保証しません。")
    if st.button("保存済み音声をメモへ取り込み直す", key="long_recording_recover_" + recording_id):
        try:
            recovered = store.recover(recording_id)
        except (MediaError, OSError, ValueError) as exc:
            ab.show_action_error("保存済み音声を取り込めませんでした。音声は削除していません。" + str(exc))
        else:
            _open_memo(recovered, on_memo_ready)
            st.rerun()


def render_long_recording(root, *, on_memo_ready=None):
    root = Path(root).resolve()
    store = LongRecordingStore(root)
    st.subheader("長時間録音（PC）")
    st.info("累計3時間で自動停止して保存します。一時停止中の時間は3時間に含めません。")
    st.caption("録音はPC内の別プロセスで続きます。ページ移動・再読み込み・ブラウザを閉じても、本体が動作中なら継続します。本体終了時は録音を停止・保存します。重要な録音は、終了前に「録音を停止して保存」で完了を確認してください。")
    st.caption("PCのスリープやマイク切断では中断する場合があります。重要な録音では入力レベルと保存済み時間を確認してください。")
    available = engine_available(root)
    try:
        history = store.history()
    except (MediaError, OSError, ValueError) as exc:
        ab.show_action_error("PC録音の履歴を確認できませんでした。" + str(exc))
        _setup(root, available)
        return
    _start(store, root, history, available)
    if st.button("PC録音の状態を更新", key="long_recording_refresh"):
        st.rerun()
    if history:
        by_id = {row["id"]: row for row in history}
        selected = st.session_state.get(SELECTED)
        if selected not in by_id:
            selected = history[0]["id"]
        if st.session_state.get(HISTORY_WIDGET) not in by_id:
            st.session_state[HISTORY_WIDGET] = selected
        selected = st.selectbox("表示するPC録音", list(by_id),
                                format_func=lambda value: f"{str(by_id[value].get('created_at') or '')[:16].replace('T', ' ')}  {by_id[value].get('title') or '録音'}  [{STATUS.get(by_id[value].get('status'), by_id[value].get('status', ''))}]  {value[:6]}",
                                key=HISTORY_WIDGET)
        st.session_state[SELECTED] = selected
        try:
            initial = store.get(selected)
        except (MediaError, OSError, ValueError) as exc:
            ab.show_action_error(str(exc))
        else:
            if _active(initial) and initial.get("status") != "unavailable":
                @st.fragment(run_every=1)
                def activity():
                    try:
                        current = store.get(selected)
                    except (MediaError, OSError, ValueError) as exc:
                        ab.show_action_error(str(exc))
                        return
                    if not _active(current):
                        # Re-enter the full page before changing the memo's
                        # history widget, which may already exist outside this fragment.
                        st.rerun()
                    _statistics(root, current)
                    _controls(store, current)
                activity()
            else:
                _statistics(root, initial)
                _terminal(store, initial, on_memo_ready)
    else:
        st.caption("保存したPC録音はここに表示されます。")
    _setup(root, available)
