"""C2 consultation briefs; all Google operations require explicit buttons."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st
import prolog_ui.components as ab

from app_core.app_clock import app_today
from app_core.consultation_report_service import (
    KINDS, ReportError, build_consultation_report, create_report_destination,
    discard_failed_report, load_pending_report, load_report_state,
    publish_consultation_report, report_consultation_prompt,
)
from app_core.google_docs_client import GoogleDocsClient
from app_core.google_share_service import load_pending_share, payload_hash
from app_core.google_share_snapshot import ShareSnapshotError
from app_core.google_share_ui import _connection_panel
from app_core.google_sheets_client import GoogleShareError, connection_status
from app_core.json_store import JsonStoreError, json_transaction
from app_core.learning_context_service import list_materials
from config import PROJECT_ROOT


def _error(exc: Exception, *, action: bool = False) -> None:
    message = str(exc) if isinstance(exc, (ReportError, GoogleShareError, ShareSnapshotError)) else \
        "処理を完了できませんでした。保存済み記録・接続・未完了操作を確認してください。入力は保持しています。"
    if action:
        ab.show_action_error(message, toast_message="相談レポートの操作を完了できませんでした。入力は保持しています。")
    else:
        st.error(message)


def _preview(report: dict) -> None:
    st.caption(f"対象: {report['period']['start']} ～ {report['period']['end']} / 生成: {report['generated_at']}")
    st.caption(f"レポートID: {report['snapshot_id']}")
    st.text_area("送信するレポート本文", value=report["text"], height=420, disabled=True,
                 key=f"docs_report_body_{payload_hash(report)}")
    st.download_button("レポートをテキストで保存", report["text"],
                       file_name=f"proloag_{report['kind']}_{report['period']['end']}.txt",
                       mime="text/plain; charset=utf-8", key="docs_report_download", width="stretch")


def render_consultation_reports(root: Path = PROJECT_ROOT) -> None:
    st.subheader("Google Docs相談レポート")
    st.write("日次・週次の学習実績を読みやすい文書にまとめます。Google未接続でもプレビュー・テキスト保存できます。")
    st.caption("AIによる推測は加えません。教材リンク・引き継ぎメモ・学習メモは、選んだ場合だけ含めます。")
    try:
        state = load_report_state(root)
        pending = load_pending_report(root)
        sheets_pending = load_pending_share(root)
        status = connection_status(root=root)
    except (ValueError, OSError) as exc:
        _error(exc)
        return
    notice = st.session_state.pop("docs_report_notice", None) or st.session_state.pop("google_share_notice", None)
    if notice:
        ab.show_action_message(notice)
    st.info("Google Docs APIも有効にしてください。C1の認証（drive.file）を共用し、新しい権限の追加は不要です。手順: docs/google-docs-reports.md")
    _connection_panel(root, status, has_pending=bool(pending or sheets_pending))
    st.caption("日次用・週次用の2つの専用文書を更新します。本文に追記・変更がある場合は上書きを止めます。相談の続きは別文書へ残してください。装飾だけの変更は保護対象外です。")
    if pending:
        st.warning("前回のレポート共有が未完了です。先に送信確認または同じ内容の再送をしてください。")
        st.caption(f"状態: {pending['status']} / 操作ID: {pending['operation_id']}")
        _preview(pending["report"])
        cols = st.columns(2)
        operation_id = pending["operation_id"]
        check = cols[0].button("レポート送信済みか確認", key=f"docs_report_check_{operation_id}", disabled=not status.get("connected"))
        retry = cols[1].button("同じレポートを再送", key=f"docs_report_retry_{operation_id}", disabled=not status.get("connected"))
        if check or retry:
            try:
                with json_transaction(root / "backups/google_share/sync", timeout=0.2), st.spinner("レポートの共有結果を確認しています…"):
                    client = GoogleDocsClient.from_saved_connection(root=root)
                    result = publish_consultation_report(client, root=root, check_only=check, expected_operation_id=operation_id)
                if result.get("confirmed") is False:
                    ab.show_action_message("同じレポートの共有完了を確認できませんでした。同じ内容を再送できます。",
                                           kind="info", toast_message="レポート共有の完了を確認できませんでした。同じ内容を再送できます。")
                else:
                    st.session_state.pop("docs_report_preview", None)
                    st.session_state["docs_report_notice"] = "レポート共有の完了を確認しました。二重追加はしていません。"
                    st.rerun()
            except (ValueError, OSError) as exc:
                _error(exc, action=True)
        if pending["status"] == "failed" and st.button("未送信レポートを取り消す", key=f"docs_report_discard_{operation_id}"):
            try:
                discard_failed_report(root, expected_operation_id=operation_id)
                st.session_state.pop("docs_report_preview", None)
                st.session_state["docs_report_notice"] = "未送信レポートを取り消しました。"
                st.rerun()
            except (ValueError, OSError) as exc:
                _error(exc, action=True)
        return
    kind = st.radio("レポートの種類", list(KINDS), format_func=lambda item: KINDS[item][0],
                    index=1, horizontal=True, key="docs_report_kind")
    old_preview = st.session_state.get("docs_report_preview")
    if old_preview and old_preview["kind"] != kind:
        st.session_state.pop("docs_report_preview", None)
    destination = status.get("docs", {}).get(kind, {})
    if destination.get("url"):
        st.link_button("共有先ドキュメントを開く", destination["url"])
    if st.button("共有先ドキュメントを作成・確認", key="docs_report_destination", disabled=not status.get("connected")):
        try:
            with json_transaction(root / "backups/google_share/sync", timeout=0.2), st.spinner("専用ドキュメントを確認しています…"):
                create_report_destination(GoogleDocsClient.from_saved_connection(root=root), kind, root=root)
            st.session_state["docs_report_notice"] = "専用ドキュメントを確認しました。この操作だけでは学習実績を送信していません。"
            st.rerun()
        except (ValueError, OSError) as exc:
            _error(exc, action=True)
    last = state.get("last_success", {}).get(kind)
    if last:
        st.success(f"最終レポート共有: {last['published_at']} / 学習記録 {last['counts']['study_sessions']} 件")
        st.link_button("最後に共有したレポートを開く", last["url"])
        with st.expander("このレポートを使ってGPTに相談する"):
            st.caption("依頼文をコピーして、普段のGPTの会話へ貼り付けます。GPTへの自動送信はしません。")
            st.code(report_consultation_prompt(last), language=None, wrap_lines=True)
            from app_core.consultation_proposal_contract import proposal_request_template

            st.caption("提案をアプリへ戻したい場合は、次の形式指定もGPTへ渡してください。返答は『相談提案の取り込み』で確認してから採用します。")
            st.code(proposal_request_template(last["snapshot_id"], f"PROLOAG {KINDS[kind][0]}レポート"), language=None, wrap_lines=True)
    try:
        materials = list_materials(root)
    except (ValueError, OSError) as exc:
        _error(exc)
        return
    names = {item["material_id"]: item["label"] for item in materials}
    with st.form("docs_report_form"):
        end = st.date_input("対象期間の最終日", value=app_today(), max_value=app_today(), key="docs_report_end")
        st.caption("日次は選択日1日、週次は選択日までの7日間です。同じ長さの前期間と比較します。")
        links = st.checkbox("登録した資料リンクを含める", value=False, key="docs_report_links")
        next_step = st.checkbox("『次はここから』メモを含める", value=False, key="docs_report_next")
        selected = st.multiselect("資料・引き継ぎメモを共有する教材", list(names), format_func=names.get,
                                  key="docs_report_materials")
        memos = st.checkbox("期間内の学習メモを含める", value=False, key="docs_report_memos")
        note = st.text_area("今回相談したいこと（任意・レポートに含めます）", max_chars=4000, key="docs_report_note")
        requested = st.form_submit_button("相談レポートのプレビューを作成", type="primary", width="stretch")
    if requested:
        st.session_state.pop("docs_report_preview", None)
        try:
            with st.spinner("記録から相談レポートを作成しています…"):
                st.session_state["docs_report_preview"] = build_consultation_report(
                    root, end, kind, material_ids=selected, include_links=links, include_next_step=next_step,
                    include_study_memo=memos, consultation_note=note)
        except (ValueError, OSError) as exc:
            _error(exc, action=True)
    report = st.session_state.get("docs_report_preview")
    if report:
        _preview(report)
        st.caption("送るのは上の固定プレビューです。元の記録・登録メモを変更した後は、プレビューを作り直してください。")
        document_id = destination.get("document_id")
        if document_id:
            st.caption(f"送信先: {destination['url']}")
        else:
            st.caption("Google送信には、初回接続と『共有先ドキュメントを作成・確認』が必要です。")
        approved = st.checkbox("このレポートの内容と送信先を確認しました",
                               key=f"docs_report_approve_{report['snapshot_id']}_{document_id}")
        if st.button("Google Docsへ送信", key="docs_report_send", type="primary", width="stretch",
                     disabled=not approved or not status.get("connected") or not document_id):
            try:
                with json_transaction(root / "backups/google_share/sync", timeout=0.2), st.spinner("Google Docsへレポートを共有しています…"):
                    publish_consultation_report(GoogleDocsClient.from_saved_connection(root=root), report,
                                                root=root, expected_document_id=document_id)
                st.session_state.pop("docs_report_preview", None)
                st.session_state["docs_report_notice"] = "相談レポートを共有しました。リンクまたは依頼文を使ってGPTに相談できます。"
                st.rerun()
            except (ValueError, OSError) as exc:
                _error(exc, action=True)
                st.caption("結果が未確定の場合、ページを再表示して送信確認・同じ内容の再送を行ってください。")
    if state.get("history"):
        with st.expander("レポート共有履歴（最新20件）"):
            st.dataframe(pd.DataFrame([{"共有日時": item["published_at"], "種別": KINDS[item["kind"]][0],
                                       "開始": item["period"]["start"], "終了": item["period"]["end"],
                                       "件数": item["counts"]["study_sessions"]} for item in state["history"]]),
                         width="stretch", hide_index=True)
