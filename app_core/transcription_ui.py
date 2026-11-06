"""Japanese/English transcription, durable history, listening and corrections."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from uuid import uuid4

import streamlit as st
import prolog_ui.components as ab
from app_core.media_conversion import MediaError
from app_core.media_conversion_ui import duration_text, _track_label
from app_core.transcription import (
    ACTIVE,
    TranscriptionSources,
    TranscriptionStore,
    dependency_dir,
    engine_available,
    export_result,
    timestamp,
)

STATUS = {
    "starting": "起動中",
    "preparing": "音声準備中",
    "loading": "モデル準備中",
    "transcribing": "文字起こし中",
    "cancelling": "中止中",
    "done": "完了",
    "cancelled": "中止",
    "failed": "失敗",
    "interrupted": "中断",
}


@st.cache_resource(show_spinner=False)
def source_manager(root):
    return TranscriptionSources(Path(root))


def _setup(root):
    with st.expander("文字起こしエンジン・保存場所"):
        if engine_available(root):
            st.success("文字起こしエンジンを検出しました。")
        else:
            st.info(
                "初回のみ、起動用と同じPythonで次のセットアップを実行してください。"
            )
        st.code(
            f'& "{sys.executable}" -B "{root / "app_core/setup_transcription.py"}"',
            language="powershell",
        )
        st.caption("専用ライブラリ：" + str(dependency_dir(root)))
        st.caption("履歴と聞き直し用音声：" + str(root / "transcription_data"))
        st.caption("モデル：" + str(root / "tools/asr-models"))
        st.markdown(
            "初回のモデル取得にはインターネット接続が必要です。取得後の認識はPC内で行います。標準はsmall、精度優先はmediumです。音声の長さ・内容により速度と精度が変わります。"
        )
        st.caption(
            "このページを開くだけでインストールやモデルのダウンロードは行いません。聞き直し用の音声は1時間あたり約115MBです。履歴を削除すると、その履歴専用の音声・結果・修正も削除します。"
        )


def _input(root, store, busy):
    manager = source_manager(str(root))
    owner = st.session_state.setdefault("asr_owner", uuid4().hex)
    prefs = st.session_state.setdefault("asr_preferences", {})
    if "asr_input_mode" not in st.session_state:
        st.session_state["asr_input_mode"] = prefs.get("input_mode", "アップロード")
    mode = st.radio(
        "ファイルの指定方法",
        ["アップロード", "PC内のファイルを直接指定"],
        horizontal=True,
        key="asr_input_mode",
        disabled=busy,
    )
    prefs["input_mode"] = mode
    uploaded, path = None, ""
    if mode == "アップロード":
        uploaded = st.file_uploader(
            "MP4／MP3を選択（200MBまで）",
            type=["mp4", "mp3"],
            max_upload_size=200,
            key="asr_upload",
            disabled=busy,
        )
        token = (mode, uploaded.file_id) if uploaded else None
    else:
        if "asr_local_path" not in st.session_state:
            st.session_state["asr_local_path"] = prefs.get("path", "")
        path = st.text_input(
            "MP4／MP3の場所（フルパス）", key="asr_local_path", disabled=busy
        )
        prefs["path"] = path
        token = (mode, path.strip().strip('"')) if path.strip() else None
    st.caption(
        "大容量ファイルはPC内の場所を直接指定できます。元ファイルは変更しません。"
    )
    if st.button("音声を確認", disabled=busy or not token, key="asr_confirm"):
        try:
            with st.spinner("音声トラックを確認しています…"):
                source = (
                    manager.prepare_upload(owner, uploaded.name, uploaded.getbuffer())
                    if uploaded
                    else manager.prepare_local(owner, path)
                )
            st.session_state["asr_confirmed_token"] = token
            st.session_state["asr_operation"] = uuid4().hex
        except (MediaError, OSError) as exc:
            ab.show_action_error(str(exc))
    source = manager.snapshot(owner)["source"]
    if not source:
        return
    st.text(source.name)
    st.caption("音声の長さ：" + duration_text(source.duration))
    track = st.selectbox(
        "文字起こしする音声",
        [t.index for t in source.tracks],
        format_func=lambda i: _track_label(source, i),
        key="asr_track_" + source.id,
        disabled=busy,
    )
    language_labels = {"ja": "日本語", "en": "英語", "auto": "自動判定"}
    quality_labels = {"standard": "標準（small）", "accurate": "精度優先（medium）"}

    def select(label, options, key, default):
        widget_key = "asr_" + key
        if widget_key not in st.session_state:
            st.session_state[widget_key] = prefs.get(key, default)
        value = st.selectbox(
            label, list(options), format_func=options.get, key=widget_key, disabled=busy
        )
        prefs[key] = value
        return value

    cols = st.columns(2)
    with cols[0]:
        language = select("音声の言語", language_labels, "language", "ja")
    with cols[1]:
        quality = select("認識モード", quality_labels, "quality", "standard")
    if "asr_hotwords" not in st.session_state:
        st.session_state["asr_hotwords"] = prefs.get("hotwords", "")
    hotwords = st.text_input(
        "用語ヒント（任意）",
        placeholder="例：ミトコンドリア、ATP、acetylcholine",
        max_chars=1000,
        key="asr_hotwords",
        disabled=busy,
    )
    prefs["hotwords"] = hotwords
    st.caption(
        "用語ヒントは認識の参考情報です。自動判定は区間冒頭の音声を参考にし、日英が混在する音声では誤認識が残ることがあります。翻訳は行いません。"
    )
    with st.expander("処理デバイス"):
        device = select(
            "認識に使うデバイス",
            {"cpu": "CPU（推奨）", "auto": "GPUを試す／利用できなければCPU"},
            "device",
            "cpu",
        )
        st.caption(
            "GPUには対応するCUDA・cuDNNが必要です。未導入の場合はCPUを選んでください。"
        )
    confirmed = token and token == st.session_state.get("asr_confirmed_token")
    ready = engine_available(root)
    if not confirmed:
        st.info("ファイルを変更した場合は「音声を確認」を押してください。")
    if not ready:
        st.info("下のセットアップ案内から、文字起こしエンジンを導入してください。")
    if st.button(
        "文字起こしを開始",
        type="primary",
        disabled=busy or not confirmed or not ready,
        key="asr_start",
    ):
        try:
            job_id = store.start(
                source,
                track,
                language,
                quality,
                hotwords,
                device,
                st.session_state.setdefault("asr_operation", uuid4().hex),
            )
            st.session_state["asr_selected"] = job_id
            st.session_state.pop("asr_history_widget", None)
            st.session_state["asr_operation"] = uuid4().hex
            st.rerun()
        except (MediaError, OSError) as exc:
            ab.show_action_error(str(exc))


def _capture_draft(job_id, revision, segment_id, key):
    drafts = st.session_state.setdefault("asr_drafts", {})
    draft = drafts.setdefault(job_id, {"revision": revision, "texts": {}})
    draft["texts"][segment_id] = st.session_state[key]


def _result(store, job_id, result):
    rows = result["segments"]
    state = result["state"]
    if state["status"] != "done":
        st.warning("途中までの結果です。出力には完了していない区間を含みません。")
    if not rows:
        st.info("認識できた文章はまだありません。音声に発話があるか確認してください。")
        return
    draft = st.session_state.get("asr_drafts", {}).get(job_id)
    if draft and draft["texts"]:
        st.info("未保存の修正があります。「修正を保存」で確定してください。")
    with st.expander("全文", expanded=True):
        st.text_area(
            "保存済みの文章",
            "\n".join(row["text"] for row in rows),
            height=180,
            disabled=True,
            key=f"asr_full_{job_id}_{result['revision']}",
        )
    st.subheader("区間ごとに聞き直し・修正")
    page_count = max(1, (len(rows) + 24) // 25)
    page = st.selectbox(
        "表示する区間",
        list(range(page_count)),
        format_func=lambda p: (
            f"{p * 25 + 1}〜{min(len(rows), (p + 1) * 25)} / {len(rows)}区間"
        ),
        key="asr_segment_page_" + job_id,
    )
    selected_rows = rows[page * 25 : (page + 1) * 25]
    ids = [r["id"] for r in selected_rows]
    by_id = {row["id"]: row for row in rows}
    listen_id = st.selectbox(
        "聞き直す区間（時刻を選択）",
        ids,
        format_func=lambda i: (
            timestamp(by_id[i]["start"]) + "  " + by_id[i]["text"][:60]
        ),
        key=f"asr_listen_{job_id}_{page}",
    )
    if st.button("この区間を聞く", key="asr_listen_button"):
        row = by_id[listen_id]
        try:
            data = store.playback(job_id, row["start"], row["end"])
            st.session_state["asr_audio"] = {
                "job": job_id,
                "id": listen_id,
                "data": data,
            }
        except (MediaError, OSError) as exc:
            ab.show_action_error(str(exc))
    audio = st.session_state.get("asr_audio", {})
    if audio.get("job") == job_id and audio.get("id") == listen_id:
        st.audio(audio["data"], format="audio/wav", autoplay=True)
        st.caption("選択区間の先頭から再生します（最大60秒）。")
    generation = st.session_state.get("asr_editor_generation", 0)
    row = by_id[listen_id]
    key = f"asr_editor_{job_id}_{result['revision']}_{listen_id}_{generation}"
    st.text_area(
        "この区間の文章",
        value=(draft or {}).get("texts", {}).get(listen_id, row["text"]),
        key=key,
        on_change=_capture_draft,
        args=(job_id, result["revision"], listen_id, key),
        height=120,
    )
    with st.expander("自動認識の原文・区間一覧"):
        st.text(row["original_text"])
        st.dataframe(
            [
                {
                    "開始": timestamp(r["start"]),
                    "終了": timestamp(r["end"]),
                    "保存済みの文章": r["text"],
                }
                for r in selected_rows
            ],
            hide_index=True,
            width="stretch",
        )
    draft = st.session_state.get("asr_drafts", {}).get(job_id)
    if st.button(
        "修正を保存",
        disabled=not draft or not draft["texts"],
        key="asr_save_edits",
        type="primary",
    ):
        try:
            store.save_edits(job_id, draft["revision"], draft["texts"])
        except (MediaError, OSError) as exc:
            ab.show_action_error(str(exc))
        else:
            st.session_state["asr_drafts"].pop(job_id, None)
            ab.queue_success("文字起こしの修正を保存しました。")
            st.rerun()
    if draft and draft["texts"]:
        if st.button("未保存の修正を破棄", key="asr_discard_edits"):
            st.session_state["asr_drafts"].pop(job_id, None)
            st.session_state["asr_editor_generation"] = generation + 1
            st.rerun()
    st.caption(
        "原文と時刻を残して文章だけ修正します。未保存の修正は、このブラウザーのセッション中に保持します。"
    )
    st.subheader("保存済みの結果を出力")
    stem = Path(result["source_name"]).stem[:100] or "transcript"
    cols = st.columns(4)
    for col, kind, mime in zip(
        cols,
        ("txt", "srt", "vtt", "json"),
        ("text/plain", "application/x-subrip", "text/vtt", "application/json"),
    ):
        with col:
            st.download_button(
                kind.upper(),
                export_result(result, kind),
                file_name=f"{stem}_transcript.{kind}",
                mime=mime,
                on_click="ignore",
                key="asr_download_" + kind,
            )
    st.caption(
        "JSONには原文・修正後の文章・元の時刻・処理状態を含みます。TXT／SRT／VTTは保存済みの修正を反映します。"
    )


def render_transcription(root):
    root = Path(root).resolve()
    store = TranscriptionStore(root)
    history = store.history()
    busy = any(row["status"] in ACTIVE for row in history)
    st.caption(
        "日本語・英語の音声を、読み返せる文章と字幕へ。処理中にページを移動しても続行し、結果はアプリを閉じた後も残ります。"
    )
    with st.expander("新しく文字起こしする", expanded=not busy):
        _input(root, store, busy)
    st.subheader("文字起こし履歴")
    if st.button("履歴を更新", key="asr_refresh"):
        st.rerun()
    if not history:
        st.info("文字起こし結果はここに保存されます。")
        _setup(root)
        return
    by_id = {row["id"]: row for row in history}
    selected = st.session_state.get("asr_selected")
    if selected not in by_id:
        selected = history[0]["id"]

    def label(job_id):
        row = by_id[job_id]
        date = datetime.fromtimestamp(
            row["created"], timezone(timedelta(hours=9))
        ).strftime("%m/%d %H:%M")
        return f"{date}  {row['name']}  [{STATUS.get(row['status'], row['status'])}]  {job_id[:6]}"

    if st.session_state.get("asr_history_widget") not in by_id:
        st.session_state["asr_history_widget"] = selected
    selected = st.selectbox(
        "表示する履歴", list(by_id), format_func=label, key="asr_history_widget"
    )
    st.session_state["asr_selected"] = selected
    initial = store._state(selected)
    active = initial["status"] in ACTIVE

    @st.fragment(run_every=1 if active else None)
    def activity():
        state = store._state(selected)
        if active and state["status"] not in ACTIVE:
            if state["status"] == "done":
                ab.queue_success("文字起こしが完了しました。")
            st.rerun()
        if state["status"] in ACTIVE:
            st.progress(
                max(0.0, min(1.0, state.get("progress", 0.0))),
                text=state.get("message", "処理中"),
            )
            st.caption(
                f"経過 {duration_text(state.get('elapsed', 0))} / 認識済み {state.get('segments', 0)}区間"
            )
            st.caption(
                "モデル準備中は進捗率が動かないことがあります。ページを移動しても続きます。"
            )
            if st.button("文字起こしを中止", key="asr_cancel"):
                store.cancel(selected)
                st.info("中止を要求しました。認識済みの区間は残します。")
        else:
            st.caption(
                STATUS.get(state["status"], state["status"])
                + " / 経過 "
                + duration_text(state.get("elapsed", 0))
            )
            if state.get("warning"):
                st.info(state["warning"])
            if state.get("error"):
                st.error(state["error"])
            _result(store, selected, store.result(selected))

    activity()
    with st.expander("この履歴を削除"):
        consent = st.checkbox(
            "この履歴の音声・原文・修正を削除する",
            key="asr_delete_consent_" + selected,
            disabled=active,
        )
        if st.button("履歴を削除", disabled=active or not consent, key="asr_delete"):
            try:
                store.delete(selected)
            except (MediaError, OSError) as exc:
                ab.show_action_error(str(exc))
            else:
                st.session_state.get("asr_drafts", {}).pop(selected, None)
                st.session_state.pop("asr_selected", None)
                st.session_state.pop("asr_audio", None)
                ab.queue_success("文字起こし履歴を削除しました。")
                st.rerun()
    _setup(root)
