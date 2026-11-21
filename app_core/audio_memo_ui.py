"""Record first, then transcribe and edit a durable local audio memo."""
from __future__ import annotations

import math
from pathlib import Path
from uuid import uuid4

import streamlit as st

import prolog_ui.components as ab
from app_core.audio_memo_service import AudioMemoStore
from app_core.long_recording_ui import render_long_recording
from app_core.media_conversion import MediaError
from app_core.transcription import ACTIVE
from app_core.transcription_ui import (
    DEVICE_LABELS,
    QUALITY_LABELS,
    STATUS,
    _setup,
    _show_recognition_device,
)


DRAFTS = "audio_memo_drafts"
SELECTED = "audio_memo_selected"
HISTORY_WIDGET = "audio_memo_history"
CAPTURES = "audio_memo_captures"
PLAYBACK = "audio_memo_playback"
LABELS = {**STATUS, "recorded": "録音保存済み", "start_failed": "開始できませんでした", "unavailable": "読み取りできません"}


def _dirty(draft):
    return (draft["title"], draft["text"]) != (draft["saved_title"], draft["saved_text"])


def _new_draft(memo, generation=0):
    title, text = str(memo.get("title") or ""), str(memo.get("text") or "")
    return {"revision": memo["revision"], "title": title, "text": text,
            "saved_title": title, "saved_text": text, "generation": generation}


def _remember_draft(memo_id):
    draft = st.session_state.get(DRAFTS, {}).get(memo_id)
    if draft is None:
        return
    for field in ("title", "text"):
        key = draft.get(field + "_key")
        if key is not None and key in st.session_state:
            draft[field] = st.session_state[key]


def _selection_changed():
    previous = st.session_state.get(SELECTED)
    if previous:
        _remember_draft(previous)
    st.session_state.pop(PLAYBACK, None)


def _select_memo(memo_id):
    _selection_changed()
    st.session_state[SELECTED] = memo_id
    st.session_state[HISTORY_WIDGET] = memo_id


def _draft_for(memo):
    drafts = st.session_state.setdefault(DRAFTS, {})
    memo_id = memo["id"]
    draft = drafts.get(memo_id)
    if draft is None:
        draft = drafts[memo_id] = _new_draft(memo)
    elif draft["revision"] != memo["revision"] and not _dirty(draft):
        draft = drafts[memo_id] = _new_draft(memo, draft["generation"] + 1)
    return draft


def _accept_saved(memo):
    drafts = st.session_state.setdefault(DRAFTS, {})
    generation = drafts.get(memo["id"], {}).get("generation", 0) + 1
    drafts[memo["id"]] = _new_draft(memo, generation)


def _upload_identity(recording):
    # Streamlit's UploadedFile ID identifies a recording event, unlike its
    # bytes: two separate recordings may legitimately have identical audio.
    file_id = getattr(recording, "file_id", None)
    if file_id is not None:
        return "file:" + str(file_id)
    # Keep the fallback object alive so CPython cannot reuse its identity.
    previous = st.session_state.get("audio_memo_upload_object")
    if previous is not recording:
        st.session_state["audio_memo_upload_object"] = recording
        st.session_state["audio_memo_upload_identity"] = "object:" + uuid4().hex
    return st.session_state["audio_memo_upload_identity"]


def _capture_recording(store, recording, title):
    identity = _upload_identity(recording)
    entries = st.session_state.setdefault(CAPTURES, {})
    entry = entries.setdefault(identity, {"capture_id": uuid4().hex})
    if "memo_id" in entry or entry.get("error"):
        return entry, False
    try:
        memo = store.capture(recording.getvalue(), entry["capture_id"], title=title)
    except (MediaError, OSError, ValueError) as exc:
        entry["error"] = str(exc)
        return entry, False
    entry["memo_id"] = memo["id"]
    _select_memo(memo["id"])
    return entry, True


def _render_recorder(store):
    st.subheader("録音する")
    st.caption("停止後、受け取った音声をこのPCへWAVで自動保存します。文字起こしは後から開始できます。")
    st.info("1回10分まで（64MiBまで）。10分で自動停止はしません。録音中はこのページを開いたままにしてください。")
    st.caption("録音中の再読み込みやページ移動では、まだ保存されていない音声が失われる場合があります。数分程度を目安に録音してください。")
    title = st.text_input("新しい録音のタイトル（任意）", key="audio_memo_capture_title",
                          help="未入力なら録音した日時を使います。保存後も変更できます。", max_chars=200)
    recording = st.audio_input("マイクで録音", sample_rate=16000, key="audio_memo_recorder")
    if recording is None:
        return
    entry, fresh = _capture_recording(store, recording, title)
    if entry.get("error"):
        ab.show_action_error("録音音声を保存できませんでした。" + entry["error"])
        st.warning("録音を取り直す前に、元のWAVをダウンロードして保管できます。")
        st.download_button("元の録音WAVをダウンロード", recording.getvalue(),
                           file_name="recording-recovery.wav", mime="audio/wav",
                           key="audio_memo_rescue", on_click="ignore")
        if st.button("録音の保存を再試行", key="audio_memo_capture_retry"):
            entry.pop("error", None)
            st.rerun()
    elif fresh:
        st.success("録音を保存しました。下のメモから文字起こしできます。")
    else:
        st.caption("この録音は保存済みです。もう一度保存することはありません。")


def _history_label(memo):
    created = str(memo.get("created_at") or "")[:16].replace("T", " ")
    return f"{created}  {memo.get('title') or '録音メモ'}  [{LABELS.get(memo.get('status'), memo.get('status', ''))}]  {memo['id'][:6]}"


def _is_active(memo):
    return bool(memo.get("active")) or memo.get("status") in ACTIVE


def _progress_value(value):
    try:
        value = float(value)
        return min(1.0, max(0.0, value)) if math.isfinite(value) else 0.0
    except (ValueError, TypeError):
        return 0.0


def _duration_text(value):
    try:
        seconds = max(0, int(float(value)))
    except (ValueError, TypeError, OverflowError):
        return "—"
    return f"{seconds // 60}分{seconds % 60:02d}秒"


def _render_activity(store, memo):
    memo_id = memo["id"]

    @st.fragment(run_every=1)
    def activity():
        try:
            current = store.sync(memo_id)
        except (MediaError, OSError, ValueError) as exc:
            ab.show_action_error(str(exc))
            return
        if not _is_active(current):
            st.rerun()
        st.progress(_progress_value(current.get("progress")), text=current.get("message") or "文字起こし中")
        st.caption("経過 " + _duration_text(current.get("elapsed", 0)))
        _show_recognition_device(current)
        if current.get("resumed_seconds"):
            st.caption("前回の認識済み区間から再開：" + _duration_text(current["resumed_seconds"]))
        st.caption("モデル準備中は進捗率が動かないことがあります。保存済みの録音は残ります。")
        if st.button("文字起こしを中止", key="audio_memo_cancel_" + memo_id,
                     disabled=current.get("status") == "cancelling"):
            try:
                store.cancel(memo_id)
            except (MediaError, OSError, ValueError) as exc:
                ab.show_action_error(str(exc))
            else:
                st.info("中止を要求しました。保存済みの録音は保持します。")
                st.rerun()

    activity()


def _render_start(store, memo, dirty):
    if memo.get("status") not in {"recorded", "start_failed", "failed", "cancelled", "interrupted", "done"}:
        return
    memo_id = memo["id"]
    with st.expander("文字起こしの設定", expanded=not bool(memo.get("job_id"))):
        settings = memo.get("start_settings") or {}

        def select(label, labels, setting, key):
            values = list(labels)
            current = settings.get(setting)
            return st.selectbox(
                label, values, index=values.index(current) if current in labels else 0,
                format_func=labels.get, key=f"audio_memo_{key}_{memo_id}",
            )

        language = select("認識言語", {"ja": "日本語", "en": "英語", "auto": "自動判定"},
                          "language", "language")
        mode = select("認識モード", QUALITY_LABELS, "mode", "mode")
        device = select("認識に使うデバイス", DEVICE_LABELS, "device", "device")
        hotwords = st.text_input(
            "認識しやすくする語句（任意）", value=settings.get("hotwords", ""),
            max_chars=1000, key="audio_memo_hotwords_" + memo_id,
        )
    try:
        busy = store.busy()
    except (MediaError, OSError, ValueError) as exc:
        ab.show_action_error(str(exc))
        busy = True
    if busy:
        st.info("ほかの文字起こしが実行中です。完了または中止後に開始できます。")
    if dirty:
        st.caption("文字起こしの前に、編集中のタイトル・本文を保存するか、保存済み内容を読み直してください。")
    if memo.get("job_id"):
        st.caption("再認識すると認識原文を更新します。編集して保存した本文・タイトルは保持します。")
    label = "もう一度文字起こし" if memo.get("status") == "done" else "文字起こしを再試行" if memo.get("job_id") else "文字起こし"
    if st.button(label,
                 key="audio_memo_start_" + memo_id, type="primary", disabled=busy or dirty):
        try:
            store.start(memo_id, language=language, mode=mode, hotwords=hotwords,
                        retry=bool(memo.get("job_id")), device=device)
        except (MediaError, OSError, ValueError) as exc:
            ab.show_action_error("文字起こしを開始できませんでした。" + str(exc))
        else:
            st.rerun()


def _render_editor(store, memo):
    memo_id = memo["id"]
    draft = _draft_for(memo)
    prefix = f"audio_memo_edit_{memo_id}_{draft['generation']}"
    draft["title_key"], draft["text_key"] = prefix + "_title", prefix + "_text"
    st.text_input("メモのタイトル", value=draft["title"], key=draft["title_key"],
                  on_change=_remember_draft, args=(memo_id,), max_chars=200)
    st.text_area("全文を編集", value=draft["text"], key=draft["text_key"], height=320,
                 on_change=_remember_draft, args=(memo_id,))
    _remember_draft(memo_id)
    dirty = _dirty(draft)
    if dirty:
        st.info("未保存の変更があります。他の録音メモへ切り替えても、この画面内の下書きは保持します。")
    else:
        st.caption("タイトル・本文は保存済みです。")
    if draft["revision"] != memo["revision"]:
        st.warning("別の画面等で保存内容が更新されています。編集中の入力は保持しています。保存済みの内容を確認してください。")
        with st.expander("最新の保存済み内容"):
            st.write(memo.get("title") or "録音メモ")
            st.code(memo.get("text") or "", language=None)
    save_col, reload_col = st.columns([1, 2])
    if save_col.button("メモを保存", key="audio_memo_save_" + memo_id, type="primary", disabled=not dirty):
        try:
            saved = store.save(memo_id, draft["revision"], draft["title"], draft["text"])
        except (MediaError, OSError, ValueError) as exc:
            ab.show_action_error("保存できませんでした。入力は保持しています。" + str(exc))
        else:
            _accept_saved(saved)
            ab.queue_success("メモを保存しました。")
            st.rerun()
    if reload_col.button("保存済み内容を読み直す（このメモの未保存入力を破棄）",
                          key="audio_memo_reload_" + memo_id):
        try:
            saved = store.get(memo_id)
        except (MediaError, OSError, ValueError) as exc:
            ab.show_action_error(str(exc))
        else:
            _accept_saved(saved)
            st.rerun()
    with st.expander("認識した原文"):
        st.code(memo.get("original_text") or "まだ認識した原文はありません。", language=None)
    return dirty


def _render_outputs(store, memo, dirty):
    memo_id = memo["id"]
    st.subheader("TXT・コピー・録音")
    if dirty:
        st.caption("TXTとコピーは保存済みの本文です。編集中の変更を反映するには「メモを保存」を押してください。")
    if memo.get("txt_synced"):
        try:
            path = store.txt_path(memo_id)
            data = path.read_bytes()
        except (MediaError, OSError, ValueError) as exc:
            st.warning("本文は保存済みですが、TXTを読み取れませんでした。" + str(exc))
        else:
            st.download_button("TXTをダウンロード", data, file_name=f"memo-{memo_id[:8]}.txt",
                               mime="text/plain", key="audio_memo_txt_" + memo_id, on_click="ignore")
            st.caption("TXTファイル：" + str(path))
    else:
        st.warning("本文の保存とTXTの更新は別です。TXTは未作成、または最新の本文が反映されていません。")
        if memo.get("txt_error"):
            st.caption(str(memo["txt_error"]))
    if st.button("TXTを作り直す", key="audio_memo_regenerate_" + memo_id, disabled=dirty):
        try:
            saved = store.regenerate_txt(memo_id, memo["revision"])
        except (MediaError, OSError, ValueError) as exc:
            ab.show_action_error(str(exc))
        else:
            _accept_saved(saved)
            st.rerun()
    with st.expander("保存済みの全文をコピー"):
        st.caption("右上のコピーを使えます。未保存の変更は含みません。")
        st.code(memo.get("text") or "", language=None)
    try:
        timed = store.timed_text(memo_id) if memo.get("job_id") or memo.get("original_text") else b""
    except (MediaError, OSError, ValueError) as exc:
        st.warning("認識原文の時刻付きTXTを用意できませんでした。" + str(exc))
    else:
        st.download_button("認識原文（時刻付きTXT）", timed,
                           file_name=f"memo-original-{memo_id[:8]}.txt", mime="text/plain",
                           key="audio_memo_timed_" + memo_id, disabled=not bool(timed), on_click="ignore")
        st.caption("時刻は認識原文の区間に対応します。編集した全文へ時刻を付け直すものではありません。")
        if not timed:
            st.caption("時刻付きの認識原文はまだありません。")
    duration = max(0.0, float(memo.get("duration") or 0))
    start_sec = st.number_input("再生開始（秒）", min_value=0.0, max_value=max(0.0, duration - 0.001),
                                 value=0.0, step=30.0, format="%.1f", key="audio_memo_play_start_" + memo_id)
    seconds = st.selectbox("再生する長さ（秒）", [15, 30, 60], index=2,
                           key="audio_memo_play_seconds_" + memo_id)
    st.caption("長い音声は最大60秒ずつ再生します。")
    if st.button("録音を再生", key="audio_memo_play_" + memo_id):
        try:
            data = store.playback(memo_id, start_sec=start_sec, seconds=seconds)
        except (MediaError, OSError, ValueError) as exc:
            ab.show_action_error(str(exc))
        else:
            st.session_state[PLAYBACK] = {"id": memo_id, "start_sec": start_sec, "seconds": seconds, "data": data}
    playback = st.session_state.get(PLAYBACK)
    if (playback and playback["id"] == memo_id and playback.get("start_sec") == start_sec
            and playback.get("seconds") == seconds):
        st.audio(playback["data"], format="audio/wav")


def render_audio_memo(root):
    root = Path(root).resolve()
    store = AudioMemoStore(root)
    ab.show_queued_message()
    ab.show_queued_toasts()
    render_long_recording(root, on_memo_ready=_select_memo)
    with st.expander("簡易録音（ブラウザ・10分まで）", expanded=False):
        _render_recorder(store)
    st.subheader("録音メモの履歴")
    if st.button("録音メモの履歴を更新", key="audio_memo_refresh"):
        st.rerun()
    try:
        history = store.history()
    except (MediaError, OSError, ValueError) as exc:
        ab.show_action_error(str(exc))
        return
    if not history:
        st.info("録音を止めて受信すると、保存したメモがここに表示されます。")
        _setup(root)
        return
    by_id = {row["id"]: row for row in history}
    selected = st.session_state.get(SELECTED)
    if selected not in by_id:
        selected = history[0]["id"]
    if st.session_state.get(HISTORY_WIDGET) not in by_id:
        st.session_state[HISTORY_WIDGET] = selected
    selected = st.selectbox("表示する録音メモ", list(by_id),
                            format_func=lambda value: _history_label(by_id[value]),
                            key=HISTORY_WIDGET, on_change=_selection_changed)
    st.session_state[SELECTED] = selected
    try:
        memo = store.sync(selected)
    except (MediaError, OSError, ValueError) as exc:
        ab.show_action_error("この録音メモを開けませんでした。" + str(exc))
        _setup(root)
        return
    st.caption(LABELS.get(memo.get("status"), memo.get("status", "")))
    st.caption("録音時間：" + _duration_text(memo.get("duration")) + " / 作成：" + str(memo.get("created_at") or "").replace("T", " ")[:19])
    if memo.get("error"):
        st.warning(str(memo["error"]))
    if memo.get("resume_warning"):
        st.info(str(memo["resume_warning"]))
    if memo.get("status") == "done" and memo.get("latest_result_empty"):
        st.info("今回は文章を認識できませんでした。前回の保存内容がある場合は、そのまま保持しています。録音を聞き直して確認してください。")
    if _is_active(memo):
        _render_activity(store, memo)
    else:
        _show_recognition_device(memo)
        if memo.get("status") in {"failed", "cancelled", "interrupted", "start_failed"}:
            st.warning("文字起こしは完了していません。保存済みの音声と、認識できた文章は保持します。")
        dirty = _render_editor(store, memo)
        _render_start(store, memo, dirty)
        _render_outputs(store, memo, dirty)
    _setup(root)
