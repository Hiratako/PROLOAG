"""Image/PDF reading, source comparison, corrections and durable history."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from uuid import uuid4

import streamlit as st
import prolog_ui.components as ab
from prolog_ui.ocr_viewer import render_ocr_viewer
from app_core.ocr_service import (
    ACTIVE,
    OCRSources,
    OCRStore,
    OCRError,
    dependency_dir,
    engine_available,
    export_result,
    parse_pages,
    transform,
)

STATUS = {
    "starting": "起動中",
    "loading": "モデル準備中",
    "reading": "読み取り中",
    "cancelling": "中止中",
    "done": "完了",
    "cancelled": "中止",
    "failed": "失敗",
    "interrupted": "中断",
}


@st.cache_resource(show_spinner=False)
def source_manager(root):
    return OCRSources(Path(root))


def _setup(root):
    with st.expander("OCRエンジン・保存場所"):
        if engine_available(root):
            st.success("OCRエンジンを検出しました。")
        else:
            st.info(
                "初回のみ、次のPowerShellコマンドで専用エンジンを導入してください。"
            )
        st.code(
            f'& "{sys.executable}" -B "{root / "app_core/setup_ocr.py"}"',
            language="powershell",
        )
        st.caption("専用ライブラリ：" + str(dependency_dir(root)))
        st.caption("原本コピー・履歴・修正：" + str(root / "ocr_data"))
        st.caption("モデル：" + str(root / "tools/ocr-models"))
        st.markdown(
            "認識はPC内で行います。初回の認識モデル取得にはインターネット接続が必要です。このページを開くだけでは取得しません。"
        )
        st.caption(
            "日本語・英語・日英混在を共通のモデルで読み取ります。手書き・数式・化学式・複雑な表は原本と照合してください。"
        )


def _input(root, store, busy):
    manager = source_manager(str(root))
    owner = st.session_state.setdefault("ocr_owner", uuid4().hex)
    prefs = st.session_state.setdefault("ocr_preferences", {})
    if "ocr_input_mode" not in st.session_state:
        st.session_state["ocr_input_mode"] = prefs.get("mode", "アップロード")
    mode = st.radio(
        "ファイルの指定方法",
        ["アップロード", "PC内のファイルを直接指定"],
        horizontal=True,
        key="ocr_input_mode",
        disabled=busy,
    )
    prefs["mode"] = mode
    uploaded, path = None, ""
    if mode == "アップロード":
        uploaded = st.file_uploader(
            "JPEG・PNG・PDFを選択（200MBまで）",
            type=["jpg", "jpeg", "png", "pdf"],
            max_upload_size=200,
            key="ocr_upload",
            disabled=busy,
        )
        token = (mode, uploaded.file_id) if uploaded else None
    else:
        if "ocr_local_path" not in st.session_state:
            st.session_state["ocr_local_path"] = prefs.get("path", "")
        path = st.text_input(
            "画像・PDFの場所（フルパス）", key="ocr_local_path", disabled=busy
        )
        prefs["path"] = path
        token = (mode, path.strip().strip('"')) if path.strip() else None
    st.caption(
        "元ファイルは変更しません。PC内の直接指定は1GBまで、1回の読み取りは100ページまでです。"
    )
    if st.button("ファイルを確認", disabled=busy or not token, key="ocr_confirm"):
        try:
            with st.spinner("ファイルを確認しています…"):
                if uploaded:
                    manager.prepare(
                        owner, name=uploaded.name, buffer=uploaded.getbuffer()
                    )
                else:
                    manager.prepare(owner, path=path)
            st.session_state["ocr_confirmed_token"] = token
            st.session_state["ocr_operation"] = uuid4().hex
            st.session_state.pop("ocr_preview", None)
        except (OCRError, OSError) as exc:
            ab.show_action_error(str(exc))
    source = manager.snapshot(owner)
    if not source:
        return
    st.text(source.name)
    st.caption(f"{source.pages}ページ / {source.identity[0] / 1024**2:,.1f}MB")
    settings = st.session_state.setdefault("ocr_page_settings", {}).setdefault(
        source.id, {}
    )
    page_key = "ocr_pages_" + source.id
    if page_key not in st.session_state:
        st.session_state[page_key] = prefs.get(
            "pages_" + source.id,
            f"1-{min(source.pages, 100)}" if source.pages > 1 else "1",
        )
    page_input = st.text_input(
        "読み取るページ",
        key=page_key,
        disabled=busy,
        help="例：1-3,5。空欄は全ページです。",
    )
    prefs["pages_" + source.id] = page_input
    try:
        pages = parse_pages(page_input, source.pages)
    except OCRError as exc:
        st.warning(str(exc))
        pages = []
    with st.expander(
        "回転・読み取り範囲を指定", expanded=bool(st.session_state.get("ocr_preview"))
    ):
        preview_key = "ocr_preview_page_" + source.id
        chosen = st.selectbox(
            "設定するページ", pages or [1], key=preview_key, disabled=busy or not pages
        )
        config = settings.setdefault(str(chosen), transform())
        rotation_key = f"ocr_rotation_{source.id}_{chosen}"
        if rotation_key not in st.session_state:
            st.session_state[rotation_key] = config["rotation"]
        rotation = st.selectbox(
            "右回りに回転",
            [0, 90, 180, 270],
            format_func=lambda n: f"{n}°",
            key=rotation_key,
            disabled=busy,
        )
        if config["rotation"] != rotation:
            config.update(rotation=rotation, crop=[0, 0, 1, 1])
            st.session_state["ocr_crop_generation"] = (
                st.session_state.get("ocr_crop_generation", 0) + 1
            )
        if st.button(
            "プレビューを表示", key="ocr_preview_button", disabled=busy or not pages
        ):
            try:
                with st.spinner("ページを表示しています…"):
                    png = manager.preview(source, chosen, rotation)
                st.session_state["ocr_preview"] = {
                    "source": source.id,
                    "page": chosen,
                    "rotation": rotation,
                    "png": png,
                }
            except (OCRError, OSError) as exc:
                ab.show_action_error(str(exc))
        cached = st.session_state.get("ocr_preview", {})
        if (cached.get("source"), cached.get("page"), cached.get("rotation")) == (
            source.id,
            chosen,
            rotation,
        ):
            event = render_ocr_viewer(
                cached["png"],
                key=f"ocr_crop_{source.id}_{chosen}_{rotation}",
                token=f"{source.id}:{chosen}:{rotation}",
                crop=config["crop"],
                mode="crop",
                disabled=busy,
            )
            if (
                event
                and not busy
                and event.get("event_id") != st.session_state.get("ocr_crop_event")
            ):
                try:
                    config.update(
                        transform({"rotation": rotation, "crop": event.get("crop")})
                    )
                except OCRError as exc:
                    ab.show_action_error(str(exc))
                else:
                    st.session_state["ocr_crop_event"] = event.get("event_id")
                    st.session_state["ocr_crop_generation"] = (
                        st.session_state.get("ocr_crop_generation", 0) + 1
                    )
                    st.rerun()
        st.caption(
            "適用済みの範囲："
            + " / ".join(
                f"{label} {value * 100:.0f}%"
                for label, value in zip(("左", "上", "右", "下"), config["crop"])
            )
        )
        st.caption(
            "範囲は回転後のページに対して指定します。回転角度を変えると範囲はページ全体へ戻ります。"
        )
        with st.expander("範囲を数値で指定（%）"):
            generation = st.session_state.get("ocr_crop_generation", 0)
            with st.form(f"ocr_crop_form_{source.id}_{chosen}_{rotation}_{generation}"):
                values = []
                for col, label, value in zip(
                    st.columns(4), ("左", "上", "右", "下"), config["crop"]
                ):
                    with col:
                        values.append(
                            st.number_input(
                                label,
                                min_value=0.0,
                                max_value=100.0,
                                value=float(value * 100),
                                step=1.0,
                                disabled=busy,
                            )
                        )
                if st.form_submit_button("数値の範囲を適用", disabled=busy):
                    try:
                        config.update(
                            transform(
                                {
                                    "rotation": rotation,
                                    "crop": [v / 100 for v in values],
                                }
                            )
                        )
                        st.rerun()
                    except OCRError as exc:
                        ab.show_action_error(str(exc))
    labels = {"auto": "自動（PDFの既存文字を優先）", "ocr": "画像としてOCR"}
    if "ocr_strategy" not in st.session_state:
        st.session_state["ocr_strategy"] = prefs.get("strategy", "auto")
    strategy = st.selectbox(
        "読み取り方法",
        list(labels),
        format_func=labels.get,
        key="ocr_strategy",
        disabled=busy,
    )
    prefs["strategy"] = strategy
    st.caption(
        "画像部分の文章が抽出されないPDFは「画像としてOCR」を選びます。回転・範囲指定したページも画像として読み取ります。"
    )
    configured = [
        {
            "ページ": page,
            "回転": str(settings.get(str(page), transform())["rotation"]) + "°",
            "範囲（左・上・右・下%）": " / ".join(
                f"{v * 100:.0f}" for v in settings.get(str(page), transform())["crop"]
            ),
        }
        for page in pages
    ]
    with st.expander("今回の設定を確認"):
        st.dataframe(configured, hide_index=True, width="stretch")
    confirmed = token and token == st.session_state.get("ocr_confirmed_token")
    if not confirmed:
        st.info("ファイルを変更した場合は「ファイルを確認」を押してください。")
    if st.button(
        "読み取りを開始",
        type="primary",
        disabled=busy or not pages or not confirmed or not engine_available(root),
        key="ocr_start",
    ):
        try:
            with st.spinner("読み取り用の原本を保存しています…"):
                job_id = store.start(
                    source,
                    pages,
                    settings,
                    strategy,
                    st.session_state.setdefault("ocr_operation", uuid4().hex),
                )
            st.session_state["ocr_selected"] = job_id
            st.session_state["ocr_operation"] = uuid4().hex
            st.session_state.pop("ocr_history_widget", None)
            st.rerun()
        except (OCRError, OSError) as exc:
            ab.show_action_error(str(exc))
    if st.button("確認済み入力をクリア", disabled=busy, key="ocr_clear"):
        manager.clear(owner)
        st.session_state.pop("ocr_preview", None)
        st.session_state.pop("ocr_confirmed_token", None)
        st.rerun()


def _capture(job_id, revision, block_id, key):
    draft = st.session_state.setdefault("ocr_drafts", {}).setdefault(
        job_id, {"revision": revision, "texts": {}}
    )
    draft["texts"][block_id] = st.session_state[key]


def _result(store, job_id, result):
    if result["state"]["status"] != "done":
        st.warning("途中までの結果です。完了したページのみを表示・出力します。")
    pages = result["pages"]
    if not pages:
        st.info(
            "読み取りを完了したページはまだありません。元ファイルを指定して再実行できます。"
        )
        return
    page_by_id = {p["number"]: p for p in pages}
    page_number = st.selectbox(
        "表示するページ", list(page_by_id), key="ocr_result_page_" + job_id
    )
    page = page_by_id[page_number]
    st.caption(
        "PDFの文字を抽出しました。"
        if page["method"] == "pdf_text"
        else "画像から文字を読み取りました。"
    )
    blocks = page["blocks"]
    by_id = {b["id"]: b for b in blocks}
    block_key = f"ocr_block_{job_id}_{page_number}"
    selected = st.session_state.get(block_key)
    if selected not in by_id:
        selected = blocks[0]["id"] if blocks else None
        if selected:
            st.session_state[block_key] = selected
    draft = st.session_state.get("ocr_drafts", {}).get(job_id)
    if draft and draft["texts"]:
        st.info("未保存の修正があります。「修正を保存」で確定してください。")
    left, right = st.columns([1, 1])
    with left:
        st.markdown("**原本との照合**")
        cache_key = (job_id, page_number)
        cached = st.session_state.get("ocr_result_image", {})
        if cached.get("key") != cache_key:
            cached = {"key": cache_key, "png": store.image(job_id, page_number)}
            st.session_state["ocr_result_image"] = cached
        event = render_ocr_viewer(
            cached["png"],
            key=f"ocr_result_viewer_{job_id}_{page_number}",
            token=f"{job_id}:{page_number}",
            blocks=blocks,
            selected=selected,
        )
        if event and event.get("event_id") != st.session_state.get("ocr_block_event"):
            st.session_state["ocr_block_event"] = event.get("event_id")
            if event.get("block_id") in by_id and event["block_id"] != selected:
                st.session_state[block_key] = event["block_id"]
                st.rerun()
    with right:
        st.markdown("**読み取った文章を修正**")
        if blocks:
            selected = st.selectbox(
                "修正する文章",
                list(by_id),
                format_func=lambda key: (
                    f"{list(by_id).index(key) + 1}. {by_id[key]['original_text'][:70]}"
                ),
                key=block_key,
            )
            block = by_id[selected]
            score = block["score"]
            if score is not None:
                st.caption(
                    f"認識スコア {score:.0%}"
                    + (" / 原本を確認してください" if score < 0.8 else "")
                )
            generation = st.session_state.get("ocr_editor_generation", 0)
            key = f"ocr_editor_{job_id}_{result['revision']}_{selected}_{generation}"
            st.text_area(
                "この文章の内容",
                value=(draft or {}).get("texts", {}).get(selected, block["text"]),
                key=key,
                on_change=_capture,
                args=(job_id, result["revision"], selected, key),
                height=150,
            )
            with st.expander("読み取り時の原文"):
                st.text(block["original_text"])
        else:
            st.info(
                "このページから文章を取得できませんでした。範囲・回転・読み取り方法を変えて再実行できます。"
            )
    draft = st.session_state.get("ocr_drafts", {}).get(job_id)
    if st.button(
        "修正を保存",
        type="primary",
        disabled=not draft or not draft["texts"],
        key="ocr_save",
    ):
        try:
            store.save_edits(job_id, draft["revision"], draft["texts"])
        except (OCRError, OSError) as exc:
            ab.show_action_error(str(exc))
        else:
            st.session_state["ocr_drafts"].pop(job_id, None)
            ab.queue_success("読み取り結果の修正を保存しました。")
            st.rerun()
    if draft and draft["texts"]:
        if st.button("未保存の修正を破棄", key="ocr_discard"):
            st.session_state["ocr_drafts"].pop(job_id, None)
            st.session_state["ocr_editor_generation"] = (
                st.session_state.get("ocr_editor_generation", 0) + 1
            )
            st.rerun()
    st.caption(
        "原文・ページ番号・位置を残して文章だけ修正します。未保存の修正はブラウザーのセッション中に保持します。"
    )
    with st.expander("このページの保存済み全文", expanded=True):
        st.text_area(
            "保存済みの文章",
            "\n".join(b["text"] for b in blocks),
            height=180,
            disabled=True,
            key=f"ocr_full_{job_id}_{page_number}_{result['revision']}",
        )
    st.subheader("保存済みの結果を出力")
    stem = Path(result["source_name"]).stem[:100] or "ocr"
    for col, kind, mime in zip(
        st.columns(3),
        ("txt", "md", "json"),
        ("text/plain", "text/markdown", "application/json"),
    ):
        with col:
            st.download_button(
                kind.upper(),
                export_result(result, kind),
                file_name=f"{stem}_ocr.{kind}",
                mime=mime,
                on_click="ignore",
                key="ocr_download_" + kind,
            )
    st.caption(
        "出力は完了した全ページの保存済み文章を含みます。JSONには原文・修正後・ページ・位置・回転と範囲も残します。"
    )


def render_ocr(root):
    root = Path(root).resolve()
    store = OCRStore(root)
    try:
        history = store.history()
    except OSError:
        st.warning("履歴を一時的に読み込めませんでした。少し待って再読込してください。")
        if st.button("履歴を再読込", key="ocr_retry_history"):
            st.rerun()
        return
    busy = any(row["status"] in ACTIVE for row in history)
    st.caption(
        "日本語・英語の教材画像やPDFを、読み返せる文章へ。原本を見ながら修正し、ページと出典を残して保存できます。"
    )

    @st.fragment
    def inputs():
        with st.expander("新しく読み取る", expanded=not busy):
            _input(root, store, busy)

    inputs()
    st.subheader("読み取り履歴")
    if st.button("履歴を更新", key="ocr_refresh"):
        st.rerun()
    if not history:
        st.info("読み取り結果はここに保存されます。")
        _setup(root)
        return
    by_id = {row["id"]: row for row in history}
    preferred = st.session_state.get("ocr_selected")
    if preferred not in by_id:
        preferred = history[0]["id"]
    if st.session_state.get("ocr_history_widget") not in by_id:
        st.session_state["ocr_history_widget"] = preferred

    def label(key):
        row = by_id[key]
        date = datetime.fromtimestamp(
            row["created"], timezone(timedelta(hours=9))
        ).strftime("%m/%d %H:%M")
        return f"{date}  {row['name']}  [{STATUS.get(row['status'], row['status'])}]  {key[:6]}"

    selected = st.selectbox(
        "表示する履歴", list(by_id), format_func=label, key="ocr_history_widget"
    )
    st.session_state["ocr_selected"] = selected
    active = by_id[selected]["status"] in ACTIVE

    @st.fragment(run_every=1 if active else None)
    def activity():
        try:
            state = store.state(selected)
        except OSError:
            st.warning(
                "処理状況を確認しています。少し待つか「履歴を更新」を押してください。"
            )
            return
        if active and state["status"] not in ACTIVE:
            if state["status"] == "done":
                ab.queue_success("画像・PDFの読み取りが完了しました。")
            st.rerun()
        if state["status"] in ACTIVE:
            st.progress(
                max(0.0, min(1.0, state.get("progress", 0.0))),
                text=state.get("message", "読み取り中"),
            )
            st.caption(
                f"経過 {round(state.get('elapsed', 0))}秒 / 完了 {len(state.get('completed_pages', []))}ページ"
            )
            st.caption(
                "初回のモデル取得中は進捗率が動かない場合があります。別ページに移動しても処理は続きます。"
            )
            if st.button("読み取りを中止", key="ocr_cancel"):
                store.cancel(selected)
                st.info("中止を要求しました。完了したページは保持します。")
        else:
            st.caption(
                STATUS.get(state["status"], state["status"])
                + f" / 経過 {round(state.get('elapsed', 0))}秒"
            )
            if state.get("error"):
                st.error(state["error"])
            _result(store, selected, store.result(selected))

    activity()
    with st.expander("この履歴を削除"):
        consent = st.checkbox(
            "この履歴の原本コピー・認識結果・修正を削除する",
            key="ocr_delete_consent_" + selected,
            disabled=active,
        )
        if st.button("履歴を削除", disabled=active or not consent, key="ocr_delete"):
            try:
                store.delete(selected)
            except (OCRError, OSError) as exc:
                ab.show_action_error(str(exc))
            else:
                st.session_state.get("ocr_drafts", {}).pop(selected, None)
                st.session_state.pop("ocr_selected", None)
                st.session_state.pop("ocr_result_image", None)
                ab.queue_success("読み取り履歴を削除しました。")
                st.rerun()
    _setup(root)
