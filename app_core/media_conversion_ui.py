"""MP4 → MP3 UI. Expensive work starts only on an explicit button press."""

from pathlib import Path
from uuid import uuid4

import streamlit as st
import prolog_ui.components as ab
from app_core.media_conversion import (
    ACTIVE,
    PREVIEW_LIMIT,
    MediaError,
    MediaManager,
    default_name,
    discover_tools,
)


@st.cache_resource(show_spinner=False)
def get_manager(root: str):
    return MediaManager(Path(root))


def duration_text(value):
    if value is None:
        return "取得できませんでした"
    seconds = max(0, round(value))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def size_text(value):
    return f"{value / 1024 / 1024:,.1f} MB"


def track_text(track, ordinal=None):
    parts = [
        f"音声 {ordinal}" if ordinal else "音声",
        track.language or "言語未指定",
        track.codec,
    ]
    if track.title:
        parts.append(track.title)
    parts.append(
        "モノラル"
        if track.channels == 1
        else "ステレオ"
        if track.channels == 2
        else f"{track.channels}ch → ステレオ"
    )
    return " / ".join(parts)


def _track_label(source, index):
    position = next(
        position for position, track in enumerate(source.tracks) if track.index == index
    )
    return track_text(source.tracks[position], position + 1)


def _forget_result():
    for key in (
        "media_preview_bytes",
        "media_download_intent",
        "media_start_intent",
        "media_result_saved",
        "media_applied_name",
        "media_notified_job",
    ):
        st.session_state.pop(key, None)


def _setup_help(root):
    with st.expander("変換エンジンの確認・セットアップ"):
        try:
            tools = discover_tools(root)
        except MediaError as exc:
            st.info(str(exc))
        else:
            st.caption("FFmpeg: " + str(tools.ffmpeg))
            st.caption("FFprobe: " + str(tools.ffprobe))
        st.markdown(
            "MP3エンコーダー（libmp3lame）を含む **FFmpegとFFprobe** を使います。配布版の `tools/ffmpeg/bin`、環境変数 `PROLOAG_FFMPEG_DIR`、PCのPATHの順で検出します。"
        )
        st.caption(
            "このページはローカル版向けです。PC内のファイル・保存先は、アプリを実行しているPCの場所を指定します。"
        )


def _source_input(manager, owner, busy):
    preferences = st.session_state.setdefault("media_preferences", {})
    modes = ["アップロード（200MBまで）", "PC内のファイルを直接指定"]
    if "media_input_mode" not in st.session_state:
        st.session_state["media_input_mode"] = preferences.get("mode", modes[0])
    mode = st.radio(
        "動画の指定方法", modes, horizontal=True, key="media_input_mode", disabled=busy
    )
    preferences["mode"] = mode
    uploaded = None
    path = ""
    if mode == modes[0]:
        generation = st.session_state.get("media_upload_generation", 0)
        uploaded = st.file_uploader(
            "MP4ファイルを選択",
            type=["mp4"],
            max_upload_size=200,
            key=f"media_upload_{generation}",
            disabled=busy,
        )
        st.caption(
            "長時間の動画は「PC内のファイルを直接指定」で、動画全体をメモリへ読み込まずに扱えます。"
        )
        token = ("upload", uploaded.file_id) if uploaded else None
    else:
        if "media_local_path" not in st.session_state:
            st.session_state["media_local_path"] = preferences.get("path", "")
        path = st.text_input(
            "MP4ファイルの場所（フルパス）",
            placeholder=r"C:\Users\…\Videos\動画.mp4",
            key="media_local_path",
            disabled=busy,
        )
        preferences["path"] = path
        st.caption(
            "エクスプローラーの「パスのコピー」で取得した、引用符付きの場所も使えます。"
        )
        token = ("local", path.strip().strip('"')) if path.strip() else None
    if st.button(
        "動画を確認",
        type="primary",
        disabled=busy or token is None,
        key="media_prepare",
    ):
        try:
            with st.spinner("動画と音声トラックを確認しています…"):
                if uploaded is not None:
                    manager.prepare_upload(owner, uploaded.name, uploaded.getbuffer())
                else:
                    manager.prepare_local(owner, path)
        except (MediaError, OSError) as exc:
            ab.show_action_error(str(exc))
        else:
            st.session_state["media_prepared_token"] = token
            _forget_result()
            st.rerun()
    return token


def _convert_controls(manager, owner, source, job, candidate):
    busy = bool(job and job["status"] in ACTIVE)
    st.subheader("確認済みの動画")
    st.text(source.name)
    a, b, c = st.columns(3)
    a.metric("動画の長さ", duration_text(source.duration))
    b.metric("元ファイル", size_text(source.identity[0]))
    c.metric("音声トラック", str(len(source.tracks)))
    changed = candidate is not None and candidate != st.session_state.get(
        "media_prepared_token"
    )
    if changed:
        st.info(
            "別の動画が指定されています。「動画を確認」を押すと、変換対象を切り替えます。"
        )
    preferences = st.session_state.setdefault("media_preferences", {})
    indices = [track.index for track in source.tracks]
    track_key = "media_track_" + source.id
    selected = st.selectbox(
        "変換する音声",
        indices,
        format_func=lambda value: _track_label(source, value),
        key=track_key,
        disabled=busy,
    )
    qualities = {
        128: "軽量（128kbps）",
        192: "標準（192kbps）",
        320: "高ビットレート（320kbps）",
    }
    if "media_bitrate" not in st.session_state:
        st.session_state["media_bitrate"] = preferences.get("bitrate", 192)
    bitrate = st.selectbox(
        "MP3の音質",
        list(qualities),
        format_func=qualities.get,
        key="media_bitrate",
        disabled=busy,
    )
    preferences["bitrate"] = bitrate
    st.caption(
        "元動画の音質を超える改善はありません。3ch以上の音声はステレオへ変換します。"
    )
    fingerprint = (source.id, selected, bitrate)
    same_result = bool(
        job and job["status"] == "done" and job["fingerprint"] == fingerprint
    )
    retry = bool(job and job["status"] in {"cancelled", "failed"})
    if st.button(
        "再試行する" if retry else "MP3に変換",
        type="primary",
        disabled=busy or changed or same_result,
        key="media_start",
    ):
        intent = st.session_state.get("media_start_intent")
        if not intent or intent["fingerprint"] != fingerprint or retry:
            intent = {"fingerprint": fingerprint, "operation_id": uuid4().hex}
            st.session_state["media_start_intent"] = intent
        try:
            manager.start(
                owner, source.id, selected, bitrate, operation_id=intent["operation_id"]
            )
        except (MediaError, OSError) as exc:
            ab.show_action_error(str(exc))
        else:
            st.session_state.pop("media_preview_bytes", None)
            st.session_state.pop("media_result_saved", None)
            st.rerun()
    if same_result:
        st.caption("この設定の変換結果は下から試聴・保存できます。")


def _result(manager, owner, source, job):
    st.subheader("変換結果")
    st.caption("使用した音声：" + _track_label(source, job["fingerprint"][1]))
    a, b, c = st.columns(3)
    a.metric("MP3の長さ", duration_text(job["duration"]))
    b.metric("MP3の容量", size_text(job["size"]))
    c.metric("音質", f"{job['bitrate']}kbps")
    preferences = st.session_state.setdefault("media_preferences", {})
    name_key = "media_filename_" + job["id"]
    from app_core.media_conversion import mp3_name

    applied = st.session_state.get("media_applied_name")
    if not applied or applied["job_id"] != job["id"]:
        applied = {"job_id": job["id"], "name": default_name(source.name)}
        st.session_state["media_applied_name"] = applied
    with st.form("media_name_form_" + job["id"]):
        draft_name = st.text_input(
            "保存ファイル名", value=applied["name"], key=name_key
        )
        rename = st.form_submit_button("保存名を適用")
    if rename:
        try:
            filename = mp3_name(draft_name)
        except MediaError as exc:
            st.warning(str(exc))
        else:
            st.session_state["media_applied_name"] = {
                "job_id": job["id"],
                "name": filename,
            }
            st.session_state.pop("media_result_saved", None)
            st.rerun()
    filename = applied["name"]
    st.caption("保存する名前：" + filename)
    if job["size"] <= PREVIEW_LIMIT:
        cached = st.session_state.get("media_preview_bytes")
        if not cached or cached["job_id"] != job["id"]:
            try:
                cached = {
                    "job_id": job["id"],
                    "data": manager.result_bytes(owner, job["id"]),
                }
                st.session_state["media_preview_bytes"] = cached
            except (MediaError, OSError) as exc:
                st.error(str(exc))
                cached = None
        if cached:
            st.audio(cached["data"], format="audio/mpeg")
            try:
                download_name = mp3_name(filename)
            except MediaError as exc:
                st.warning(str(exc))
            else:
                st.download_button(
                    "MP3をダウンロード",
                    cached["data"],
                    file_name=download_name,
                    mime="audio/mpeg",
                    on_click="ignore",
                    key="media_download_" + job["id"],
                    type="primary",
                )
    else:
        st.info(
            "変換が完了しました。64MBを超える結果は「PCへ直接保存」から受け取れます。"
        )
    with st.expander(
        "PCへ直接保存（大容量にも対応）",
        expanded=job["size"] > PREVIEW_LIMIT
        or bool(st.session_state.get("media_result_saved")),
    ):
        folder_key = "media_save_folder_" + source.id
        initial = preferences.get(
            "folder", str(source.path.parent) if source.mode == "local" else ""
        )
        folder = st.text_input(
            "保存先フォルダー（フルパス）",
            value=initial,
            placeholder=r"C:\Users\…\Music",
            key=folder_key,
        )
        preferences["folder"] = folder
        st.caption(
            "既存のフォルダーへ別ファイルとして保存します。同名ファイルがある場合は別名を指定してください。"
        )
        if st.button(
            "PCへ直接保存", disabled=not folder.strip(), key="media_save_" + job["id"]
        ):
            try:
                with st.spinner("MP3を保存しています…"):
                    saved = manager.save_result(owner, job["id"], folder, filename)
            except (MediaError, OSError) as exc:
                ab.show_action_error(str(exc))
            else:
                st.session_state["media_result_saved"] = str(saved)
                ab.queue_success("MP3を保存しました。")
                st.rerun()
        if st.session_state.get("media_result_saved"):
            st.success("保存先：" + st.session_state["media_result_saved"])


def render_media_conversion(root: Path):
    owner = st.session_state.setdefault("media_owner", uuid4().hex)
    manager = get_manager(str(root.resolve()))
    manager.prune()
    initial = manager.snapshot(owner)
    job = initial["job"]
    busy = bool(job and job["status"] in ACTIVE)
    if (
        job
        and job["status"] == "done"
        and st.session_state.get("media_notified_job") != job["id"]
    ):
        st.session_state["media_notified_job"] = job["id"]
        ab.queue_success("MP3への変換が完了しました。")
        st.rerun()
    st.caption(
        "MP4の音声をMP3に変換します。変換中は別のページへ移動でき、戻ると処理状況や結果を確認できます。"
    )
    candidate = _source_input(manager, owner, busy)
    source = initial["source"]
    if source:
        _convert_controls(manager, owner, source, job, candidate)

    @st.fragment(run_every=0.5 if busy else None)
    def activity():
        current = manager.snapshot(owner)
        current_job = current["job"]
        if busy and (not current_job or current_job["status"] not in ACTIVE):
            st.rerun()
        if not current_job:
            return
        if current_job["status"] in ACTIVE:
            st.subheader("変換中")
            selected_track = next(
                track
                for track in current["source"].tracks
                if track.index == current_job["fingerprint"][1]
            )
            if selected_track.duration or current["source"].duration:
                st.progress(
                    current_job["progress"],
                    text=f"{current_job['progress']:.0%} / 経過 {duration_text(current_job['elapsed'])}",
                )
            else:
                st.info(
                    "変換しています。進捗率を取得できない動画です。経過 "
                    + duration_text(current_job["elapsed"])
                )
            st.caption(
                "進捗は音声の長さから計算した目安です。完了時に変換結果を確認します。"
            )
            if st.button(
                "変換を中止",
                key="media_cancel",
                disabled=current_job["status"] == "cancelling",
            ):
                manager.cancel(owner)
                st.rerun()
            if current_job["status"] == "cancelling":
                st.info("中止しています…")
        elif current_job["status"] == "done":
            _result(manager, owner, current["source"], current_job)
        elif current_job["status"] == "cancelled":
            st.info("変換を中止しました。確認済みの動画から再試行できます。")
        else:
            st.error(current_job["error"])
            if current_job["details"]:
                with st.expander("変換エラーの詳細"):
                    st.code(current_job["details"], language="text")

    activity()
    if source:
        if st.button("結果・一時ファイルをクリア", disabled=busy, key="media_clear"):
            try:
                manager.clear(owner)
            except (MediaError, OSError) as exc:
                ab.show_action_error(str(exc))
            else:
                _forget_result()
                st.session_state.pop("media_prepared_token", None)
                st.session_state["media_upload_generation"] = (
                    st.session_state.get("media_upload_generation", 0) + 1
                )
                ab.queue_success("変換結果と一時ファイルをクリアしました。")
                st.rerun()
        st.caption(
            "変換用の一時データはクリア操作・アプリの通常終了時に削除します。24時間利用されていない結果は、この機能の次回利用時にも片付けます。"
        )
    _setup_help(root)
