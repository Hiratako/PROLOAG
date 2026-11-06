"""Google learning-share UI. Importing/rendering never initiates network I/O."""
from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path

import pandas as pd
import streamlit as st
import prolog_ui.components as ab

from app_core.app_clock import app_today
from app_core.google_share_service import (
    ShareStateError, consultation_prompt, discard_failed_share, load_pending_share,
    load_share_state, publish_share, save_share_preferences,
)
from app_core.google_share_snapshot import ShareSnapshotError, build_share_snapshot
from app_core.google_sheets_client import (
    GoogleShareError, GoogleSheetsClient, connection_status, disconnect,
)
from app_core.json_store import JsonStoreError, json_transaction
from config import PROJECT_ROOT


TABLE_LABELS = {"overview": "相談用サマリー", "daily_summary": "日別実績",
                "subjects": "科目別", "materials": "教材別", "categories": "学習区分別",
                "study_sessions": "学習明細"}


def _safe_error(exc: Exception, *, action: bool = False) -> None:
    if isinstance(exc, (GoogleShareError, ShareStateError, ShareSnapshotError)):
        message = str(exc)
    else:
        message = "処理を完了できませんでした。記録の整合性、接続設定、未完了の保存操作を確認してください。元の記録は変更していません。"
    if action:
        ab.show_action_error(message, toast_message="Google共有の操作を完了できませんでした。画面の案内を確認してください。")
    else:
        st.error(message)


def _preview_frame(rows: list[list]) -> pd.DataFrame:
    """Keep oversized integers exact without changing the approved snapshot."""
    # Convert before pandas inference: very large ints can overflow there, and
    # Arrow also cannot encode them. Match the Sheets transport's exact-integer
    # boundary, and stringify the entire affected column to avoid mixed types.
    text_columns = {
        index for row in rows[1:] for index, value in enumerate(row)
        if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2 ** 53
    }
    preview_rows = [
        [str(value) if index in text_columns and value is not None else value
         for index, value in enumerate(row)]
        for row in rows[1:]
    ]
    return pd.DataFrame(preview_rows, columns=rows[0])


def _show_snapshot(snapshot: dict, *, pending: bool = False) -> None:
    period = snapshot["period"]
    st.caption(f"対象期間: {period['start']} ～ {period['end']} / 生成日時: {snapshot['generated_at']}")
    st.caption(f"共有ID: {snapshot['snapshot_id']}")
    cols = st.columns(2)
    cols[0].metric("学習記録", f"{snapshot['counts']['study_sessions']:,} 件")
    cols[1].metric("記録のある日", f"{snapshot['counts']['study_days']:,} 日")
    for role, rows in snapshot["tables"].items():
        with st.expander(TABLE_LABELS.get(role, role), expanded=role == "overview"):
            if rows:
                frame = _preview_frame(rows)
                if role == "overview":
                    frame = frame.astype(str)
                st.dataframe(frame, width="stretch", hide_index=True)
            else:
                st.caption("データなし")
    if not pending:
        st.caption("このプレビューを固定して送信します。記録を修正した後はプレビューを作り直してください。")


def _connection_panel(root: Path, status: dict, *, has_pending: bool) -> None:
    from app_core.consultation_report_service import load_pending_report

    try:
        has_pending = has_pending or load_pending_report(root) is not None
    except (ValueError, OSError):
        has_pending = True
        st.error("相談レポートの未完了状態を確認できません。安全のため接続解除を停止しています。")
    with st.expander("Google接続・初回設定", expanded=not status.get("connected")):
        st.markdown(
            "初回はGoogle Cloudで **Google Sheets API** を有効化し、OAuthのアプリ種類を"
            " **デスクトップアプリ** にして、クライアントJSONを取得してください。"
            "詳しい手順は `docs/google-sheets-setup.md` にあります。"
        )
        st.caption("ChatGPTのGoogle連携とは別の接続です。アプリが作った専用ファイルだけを扱う権限を使います。")
        if not status.get("supported", True):
            st.warning("Google接続情報の安全な保存はWindows版で対応しています。プレビューは接続なしで利用できます。")
        if not status.get("dependencies_available", False):
            st.info("Google連携用の追加ライブラリが必要です。アプリと同じPython環境へ requirements-google.txt を導入してください。手順書に実行例があります。")
        enabled = status.get("supported", True) and status.get("dependencies_available", False)
        version = st.session_state.get("google_share_upload_version", 0)
        upload = st.file_uploader("OAuthクライアントJSON（デスクトップアプリ）", type=["json"],
                                  key=f"google_share_client_upload_{version}", disabled=not enabled)
        if st.button("Googleに接続して共有先を作成", key="google_share_connect",
                     disabled=not enabled or upload is None):
            try:
                if upload.size > 64 * 1024:
                    raise ShareStateError("クライアントJSONが大きすぎます。Google Cloudから取得したファイルを選んでください。")
                config = json.loads(upload.getvalue().decode("utf-8-sig"))
                st.info("認証用ブラウザが開きます。Googleへのログイン・許可を完了してください。待機は最大120秒です。")
                with json_transaction(root / "backups/google_share/sync", timeout=0.2), st.spinner("Googleへの接続を待っています…"):
                    client = GoogleSheetsClient.connect(config, root=root)
                    client.create_spreadsheet("PROLOAG 学習共有")
                st.session_state["google_share_notice"] = "Googleに接続し、専用の共有先を用意しました。学習データはまだ送っていません。"
                st.session_state.pop("google_share_preview", None)
                st.session_state.pop("docs_report_preview", None)
                st.session_state["google_share_upload_version"] = version + 1
                st.rerun()
            except (GoogleShareError, ShareStateError, ValueError, OSError) as exc:
                _safe_error(exc, action=True)
        if status.get("connected") or status.get("error_code"):
            if status.get("connected"):
                st.caption("接続済み。送信時に認証期限を確認します。認証情報はプロジェクト外へ暗号化保存します。")
            else:
                st.warning("保存済み接続を読み取れません。未完了の共有がなければ、このPCの接続情報を解除して再設定できます。Google上の表は残ります。")
            if status.get("url"):
                st.link_button("共有先を開く", status["url"])
            forget = st.checkbox("このPCの接続情報を削除する（Google上のファイルは残す）", key="google_share_forget_confirm",
                                 disabled=has_pending)
            if st.button("接続を解除", key="google_share_disconnect", disabled=not forget or has_pending):
                try:
                    with json_transaction(root / "backups/google_share/sync", timeout=0.2):
                        if load_pending_share(root) is not None or load_pending_report(root) is not None:
                            raise ShareStateError("別画面で共有が開始されています。未完了の共有を先に確認してください。")
                        disconnect(root=root)
                    st.session_state.pop("google_share_preview", None)
                    st.session_state.pop("docs_report_preview", None)
                    st.session_state["google_share_notice"] = "このPCの接続情報を削除しました。Google上のファイル・公開済みデータは残っています。"
                    st.rerun()
                except (ValueError, OSError) as exc:
                    _safe_error(exc, action=True)
            if has_pending:
                st.caption("未完了の共有を確認してから接続解除できます。再認証は可能です。")


def render_google_share(root: Path = PROJECT_ROOT) -> None:
    st.subheader("Google Sheetsで学習実績を共有")
    st.write("学習実績と相談用サマリーを専用スプレッドシートへ送ります。元のJSONは変更しません。")
    st.caption("日記・健康・服薬・金額・ノート本文・タスクは送りません。学習メモは選択時のみ対象です。")
    try:
        state = load_share_state(root)
        pending = load_pending_share(root)
        status = connection_status(root=root)
    except (GoogleShareError, ShareStateError, JsonStoreError, OSError) as exc:
        _safe_error(exc)
        return
    notice = st.session_state.pop("google_share_notice", None)
    if notice:
        ab.show_action_message(notice)
    _connection_panel(root, status, has_pending=pending is not None)
    last = state.get("last_success")
    if last:
        st.success(f"最終共有成功: {last['published_at']} / 学習記録 {last['counts']['study_sessions']:,} 件")
        st.link_button("最後に共有した表を開く", last["url"])
        with st.expander("GPTへ渡す依頼文"):
            st.caption("右上のコピーボタンでコピーし、普段のGPTの会話へ貼り付けてください。GPTへの送信は自動では行いません。")
            st.code(consultation_prompt(last), language=None, wrap_lines=True)
            from app_core.consultation_proposal_contract import proposal_request_template

            st.caption("提案をアプリへ戻す場合は、以下の形式指定も利用できます。返答は『相談提案の取り込み』で確認してください。")
            st.code(proposal_request_template(last["snapshot_id"], "PROLOAG Sheets学習共有"), language=None, wrap_lines=True)
    if pending:
        st.warning("前回の共有が未完了です。新しい内容を送る前に、送信結果を確認するか同じ内容を再送してください。")
        st.caption(f"状態: {pending['status']} / 操作ID: {pending['operation_id']} / 作成: {pending['created_at']}")
        _show_snapshot(pending["snapshot"], pending=True)
        columns = st.columns(2)
        operation_id = pending["operation_id"]
        check = columns[0].button("送信済みか確認", key=f"google_share_check_{operation_id}", disabled=not status.get("connected"))
        retry = columns[1].button("同じ内容を再送", key=f"google_share_retry_{operation_id}", disabled=not status.get("connected"))
        if check or retry:
            try:
                with json_transaction(root / "backups/google_share/sync", timeout=0.2), st.spinner("共有結果を確認しています…" if check else "同じプレビューを再送しています…"):
                    result = publish_share(GoogleSheetsClient.from_saved_connection(root=root), root=root, check_only=check,
                                           expected_operation_id=operation_id)
                if result.get("confirmed") is False:
                    ab.show_action_message("同じ共有IDの送信完了は確認できませんでした。同じ内容を再送できます。",
                                           kind="info", toast_message="共有完了を確認できませんでした。同じ内容を再送できます。")
                else:
                    st.session_state.pop("google_share_preview", None)
                    st.session_state["google_share_notice"] = "共有完了を確認しました。重複追加はしていません。"
                    st.rerun()
            except (GoogleShareError, ShareStateError, JsonStoreError, OSError) as exc:
                _safe_error(exc, action=True)
        if pending["status"] == "failed" and st.button("未送信のプレビューを取り消す", key=f"google_share_discard_{operation_id}"):
            try:
                discard_failed_share(root, expected_operation_id=operation_id)
                st.session_state.pop("google_share_preview", None)
                st.session_state["google_share_notice"] = "未送信のプレビューを取り消しました。"
                st.rerun()
            except (ShareStateError, JsonStoreError) as exc:
                _safe_error(exc, action=True)
        return
    preferences = state.get("preferences", {})
    days_options = [7, 30, 90]
    preferred_days = preferences.get("days", 7)
    with st.form("google_share_preview_form"):
        cols = st.columns(2)
        days = cols[0].selectbox("共有する期間", days_options,
                                 index=days_options.index(preferred_days) if preferred_days in days_options else 0,
                                 format_func=lambda value: f"直近{value}日", key="google_share_days")
        end = cols[1].date_input("期間の最終日", value=app_today(), max_value=app_today(), key="google_share_end")
        details = st.checkbox("学習明細も共有する", value=preferences.get("include_details", True), key="google_share_details")
        content = st.checkbox("学習明細に学習メモを含める（個人情報がないか確認してください）",
                              value=preferences.get("include_content", False), key="google_share_content")
        note = st.text_area("GPTへ共有したい相談メモ（任意）", max_chars=4000, key="google_share_note")
        preview_requested = st.form_submit_button("プレビューを作成・更新", type="primary", width="stretch")
    if preview_requested:
        st.session_state.pop("google_share_preview", None)
        try:
            with st.spinner("学習記録から共有内容を作成しています…"):
                snapshot = build_share_snapshot(root, end - timedelta(days=days - 1), end,
                                                include_details=details, include_content=content and details,
                                                consultation_note=note)
                save_share_preferences({"days": days, "include_details": details,
                                        "include_content": content and details}, root)
                st.session_state["google_share_preview"] = snapshot
        except (ValueError, JsonStoreError, OSError) as exc:
            _safe_error(exc, action=True)
    snapshot = st.session_state.get("google_share_preview")
    if snapshot:
        st.subheader("送信する内容のプレビュー")
        _show_snapshot(snapshot)
        st.info("専用表の管理タブを、この期間・項目の内容へ置き換えます。前回送った期間外の行や、今回共有しない学習メモ・明細も管理タブから除かれます。自分で追加した別タブは変更しません。")
        approved = st.checkbox("このプレビューの内容と共有先を確認しました",
                               key=f"google_share_approve_{snapshot['snapshot_id']}_{status.get('spreadsheet_id')}")
        if status.get("url"):
            st.caption(f"共有先: {status['url']}")
        if st.button("Googleへ送信", key="google_share_send", type="primary", width="stretch",
                     disabled=not approved or not status.get("connected") or not status.get("spreadsheet_id")):
            try:
                with json_transaction(root / "backups/google_share/sync", timeout=0.2), st.spinner("Google Sheetsへ共有しています…"):
                    publish_share(GoogleSheetsClient.from_saved_connection(root=root), snapshot, root=root,
                                  expected_spreadsheet_id=status["spreadsheet_id"])
                st.session_state.pop("google_share_preview", None)
                st.session_state["google_share_notice"] = "Googleへの共有が完了しました。最新の依頼文をコピーしてGPTに渡せます。"
                st.rerun()
            except (GoogleShareError, ShareStateError, JsonStoreError, OSError) as exc:
                _safe_error(exc, action=True)
                st.caption("送信途中で失敗した場合は、画面を再読込すると同じ内容の確認・再送ができます。")
        if not status.get("connected"):
            st.caption("プレビューは接続なしで使えます。送信するには上のGoogle接続を完了してください。")
    if state.get("history"):
        with st.expander("共有成功履歴（最新20件）"):
            rows = [["共有日時", "学習記録数", "対象開始", "対象終了", "共有ID"]]
            rows.extend([item["published_at"], item["counts"]["study_sessions"],
                         item["period"]["start"], item["period"]["end"], item["snapshot_id"]]
                        for item in state["history"])
            st.dataframe(_preview_frame(rows), width="stretch", hide_index=True)
