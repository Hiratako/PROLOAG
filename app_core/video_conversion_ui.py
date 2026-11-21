"""Streamlit controls for the local MKV to MP4 workflow."""

from pathlib import Path
from uuid import uuid4

import streamlit as st

import prolog_ui.components as ab
from app_core.media_conversion import ACTIVE, MediaError, UPLOAD_LIMIT
from app_core.media_conversion_ui import duration_text, size_text
from app_core.video_conversion import VideoManager, default_video_name


_MODES = {"copy": "高速・映像そのまま", "compatible": "互換性重視（H.264 / AAC）"}


@st.cache_resource(show_spinner=False)
def get_video_manager(root: str):
    return VideoManager(Path(root))


def _source_input(manager, owner, busy):
    mode = st.radio(
        "動画の指定方法",
        ["PC内のファイルを直接指定", "アップロード"],
        horizontal=True,
        key="mkv_input_mode",
        disabled=busy,
    )
    uploaded = None
    path = ""
    if mode == "アップロード":
        uploaded = st.file_uploader(
            "MKVファイルを選択（200MBまで）",
            type=["mkv"],
            max_upload_size=UPLOAD_LIMIT // (1024 * 1024),
            key="mkv_upload",
            disabled=busy,
        )
        token = (mode, uploaded.file_id) if uploaded else None
    else:
        path = st.text_input(
            "MKVファイルの場所（フルパス）",
            placeholder=r"C:\Users\…\Videos\動画.mkv",
            key="mkv_input_path",
            disabled=busy,
        )
        value = path.strip().strip('"')
        token = (mode, value) if value else None
        st.caption("大きな動画はPC内のパスで指定できます。エクスプローラーの「パスのコピー」も使えます。")
    if st.button("動画を確認", type="primary", disabled=busy or token is None, key="mkv_prepare"):
        try:
            with st.spinner("動画のトラックを確認しています…"):
                if uploaded is not None:
                    manager.prepare_upload(owner, uploaded.name, uploaded.getbuffer())
                else:
                    manager.prepare_local(owner, path)
        except (MediaError, OSError) as exc:
            ab.show_action_error(str(exc))
        else:
            st.session_state["mkv_confirmed_input"] = token
            st.session_state.pop("mkv_confirmed_plan", None)
            st.rerun()
    return token


def _track_label(track):
    properties = [track.language, track.title, track.codec]
    if getattr(track, "width", 0) and getattr(track, "height", 0):
        properties.append(f"{track.width}×{track.height}")
    if getattr(track, "channels", 0):
        properties.append(f"{track.channels}ch")
    return f"トラック {track.index}  /  " + "  /  ".join(value for value in properties if value)


def _settings(source, busy):
    st.subheader("出力に含めるトラック")
    st.text(source.name)
    st.caption(f"長さ {duration_text(source.duration)}  /  容量 {size_text(source.identity[0])}")
    key = "mkv_" + source.id
    videos = {track.index: track for track in source.videos}
    video = st.selectbox(
        "映像", list(videos), key=key + "_video", disabled=busy,
        format_func=lambda index: _track_label(videos[index]),
    )
    audios = {track.index: track for track in source.audios}
    default_audio = next(
        (track.index for track in source.audios if track.default),
        next(iter(audios), None),
    )
    audio_options = [*audios, None]
    audio = st.selectbox(
        "音声", audio_options, index=audio_options.index(default_audio),
        key=key + "_audio", disabled=busy,
        format_func=lambda index: "音声なし" if index is None else _track_label(audios[index]),
    )
    subtitles = {track.index: track for track in source.subtitles if track.codec == "subrip"}
    subtitle = st.selectbox(
        "字幕", [None, *subtitles], key=key + "_subtitle", disabled=busy,
        format_func=lambda index: "字幕なし" if index is None else _track_label(subtitles[index]),
    )
    if len(subtitles) != len(source.subtitles):
        st.caption("変換できる字幕はSubRip（SRT）です。その他の字幕はMKVに残ります。")
    mode = st.radio(
        "変換方式", list(_MODES), format_func=_MODES.get, horizontal=True,
        key=key + "_mode", disabled=busy,
    )
    st.caption(
        "高速では対応する映像を再圧縮せず、必要なときだけ音声をAACに変換します。"
        if mode == "copy" else
        "映像をH.264、音声をAACに変換します。時間がかかり、画質・容量が変わります。"
    )
    directory = st.text_input(
        "保存先フォルダー（フルパス）",
        value=str(source.path.parent) if source.mode == "local" else "",
        key=key + "_directory", disabled=busy,
    ).strip()
    name = st.text_input(
        "保存ファイル名", value=default_video_name(source.name),
        key=key + "_name", disabled=busy,
    ).strip()
    return {
        "video_index": video, "audio_index": audio, "subtitle_index": subtitle,
        "mode": mode, "directory": directory, "name": name,
    }


def _show_plan(plan):
    st.subheader("変換する内容")
    st.caption("保存先")
    st.text(str(plan.target))
    video = "コピー" if plan.video_codec == "copy" else "H.264へ変換"
    audio = {"copy": "コピー", "aac": "AACへ変換"}.get(plan.audio_codec, "音声なし")
    subtitle = "MP4のテキスト字幕に変換" if plan.subtitle_codec else "字幕なし"
    st.write(f"映像　{video}　／　音声　{audio}　／　字幕　{subtitle}")
    if plan.estimated_bytes:
        st.caption(f"必要容量の目安 {size_text(plan.estimated_bytes)}")
    for warning in plan.warnings:
        st.warning(warning)
    if plan.dropped_tracks:
        with st.expander("出力に含まれないトラック・情報", expanded=True):
            for track in plan.dropped_tracks:
                st.text(track)


def _activity(manager, owner, busy):
    @st.fragment(run_every=0.5 if busy else None)
    def render():
        job = manager.snapshot(owner)["job"]
        if busy and (not job or job["status"] not in ACTIVE):
            st.rerun()
        if not job:
            return
        if job["status"] in ACTIVE:
            st.subheader("変換中")
            elapsed = duration_text(job.get("elapsed"))
            progress = job.get("progress")
            if progress is None or (job.get("plan") and not job["plan"].expected_duration):
                st.info(f"処理中です。経過 {elapsed}")
            else:
                st.progress(progress, text=f"{progress:.0%} / 経過 {elapsed}")
            if job["status"] == "cancelling":
                st.info("中止しています…")
            if st.button("変換を中止", key="mkv_cancel", disabled=job["status"] == "cancelling"):
                manager.cancel(owner)
                st.rerun()
        elif job["status"] == "done":
            st.success("MP4を保存しました。")
            st.text(str(job["output_path"]))
            st.caption(f"容量 {size_text(job['size'])}")
        elif job["status"] == "cancelled":
            st.info("変換を中止しました。確認済みの動画から再試行できます。")
        else:
            st.error(job["error"])
        for warning in job.get("warnings", ()):
            st.warning(warning)
        if job.get("details"):
            with st.expander("処理の詳細"):
                st.code(job["details"], language="text")

    render()


def render_video_conversion(root: Path):
    st.caption("MKVから再生用のMP4を作ります。元のMKVはそのまま残ります。")
    manager = get_video_manager(str(root.resolve()))
    owner = st.session_state.setdefault("mkv_owner", uuid4().hex)
    manager.prune()
    current = manager.snapshot(owner)
    job = current["job"]
    busy = bool(job and job["status"] in ACTIVE)
    candidate = _source_input(manager, owner, busy)
    source = current["source"]
    if source is not None:
        values = _settings(source, busy)
        confirmed_input = bool(candidate and candidate == st.session_state.get("mkv_confirmed_input"))
        settings_key = (source.id, tuple(values.items()))
        if not confirmed_input:
            st.info("ファイルを変更した場合は、もう一度「動画を確認」を押してください。")
        if st.button(
            "変換内容を確認", key="mkv_plan",
            disabled=busy or not confirmed_input or not values["directory"] or not values["name"],
        ):
            st.session_state.pop("mkv_confirmed_plan", None)
            try:
                with st.spinner("出力するトラックと保存先を確認しています…"):
                    plan = manager.plan(owner, source.id, **values)
            except (MediaError, OSError) as exc:
                ab.show_action_error(str(exc))
            else:
                st.session_state["mkv_confirmed_plan"] = (plan.id, settings_key, uuid4().hex)
                current["plan"] = plan
        confirmed = st.session_state.get("mkv_confirmed_plan")
        plan = current["plan"]
        ready = bool(confirmed_input and plan and confirmed and confirmed[:2] == (plan.id, settings_key))
        if ready:
            _show_plan(plan)
        elif plan:
            st.info("設定を変更した場合は「変換内容を確認」を押してください。")
        saved = bool(job and job["status"] == "done" and job.get("plan") == plan)
        if st.button(
            "MP4に変換して保存", type="primary", key="mkv_start",
            disabled=busy or not ready or saved,
        ):
            try:
                manager.start(owner, plan.id, operation_id=confirmed[2])
            except (MediaError, OSError) as exc:
                ab.show_action_error(str(exc))
            else:
                st.session_state["mkv_confirmed_plan"] = (plan.id, settings_key, uuid4().hex)
                st.rerun()
    _activity(manager, owner, busy)
    if source is not None and st.button("変換画面をクリア", key="mkv_clear", disabled=busy):
        try:
            manager.clear(owner)
        except (MediaError, OSError) as exc:
            ab.show_action_error(str(exc))
        else:
            st.session_state.pop("mkv_confirmed_input", None)
            st.session_state.pop("mkv_confirmed_plan", None)
            st.rerun()
    with st.expander("変換エンジンの準備"):
        st.write("FFmpegとFFprobeを使います。互換性重視にはH.264（libx264）とAACのエンコーダーが必要です。")
        st.caption("tools/ffmpeg/bin、PROLOAG_FFMPEG_DIR、PCのPATHから検出します。")
