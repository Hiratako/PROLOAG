"""Session-local, explicitly refreshed CSV/ZIP output; never a save baseline."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

import prolog_ui.components as ab
from app_core.export_service import build_export_bundle
from config import PROJECT_ROOT


SNAPSHOT_KEY = "_csv_export_snapshot"


def render_csv_export(root: Path = PROJECT_ROOT) -> None:
    st.info(
        "このページはJSONを読み取るだけで、保存・正規化・書き戻しを行いません。"
        "出力ファイルには日記本文や体調などの個人情報が含まれるため、取り扱いに注意してください。"
    )
    st.caption(
        "出力は生成時点の固定スナップショットです。記録の追加・編集・削除は自動反映されません。"
        "最新の記録を反映するには『CSV・ZIPを生成・更新』を押してください。"
        "個別CSVの選択やダウンロードでは再生成しません。"
    )
    root = Path(root).resolve()
    snapshot = st.session_state.get(SNAPSHOT_KEY)
    if snapshot is None or snapshot.get("root") != str(root):
        snapshot = {"root": str(root), "attempted": False, "bundle": None, "error": None}
        st.session_state[SNAPSHOT_KEY] = snapshot
    requested = st.button("CSV・ZIPを生成・更新", type="primary", key="csv_export_refresh")
    if requested or not snapshot["attempted"]:
        # A failed first attempt is not retried on every unrelated widget rerun.
        # Replace the entire bundle only after generation has fully succeeded.
        snapshot["attempted"] = True
        try:
            with st.spinner("保存済み記録からCSV・ZIPを生成しています..."):
                bundle = build_export_bundle(root)
        except (ValueError, OSError, RuntimeError) as exc:
            snapshot["error"] = str(exc)
            if requested:
                ab.show_action_toast("CSV・ZIPを生成できませんでした。詳細表示を確認してください。", kind="error")
        else:
            snapshot.update(bundle=bundle, error=None)
            if requested:
                if bundle.warnings:
                    ab.show_action_message("CSV・ZIPを生成しましたが、一部の記録を読み込めませんでした。", kind="warning")
                else:
                    ab.show_action_message("CSV・ZIPを生成・更新しました。")

    bundle = snapshot["bundle"]
    if snapshot["error"] is not None:
        st.error(f"CSV・ZIPの生成に失敗しました。記録の変更は行っていません。{snapshot['error']}")
        if bundle is not None:
            st.warning(
                f"更新は完了していません。以下は前回生成した出力です（生成日時: {bundle.generated_at:%Y-%m-%d %H:%M:%S}）。"
                "最新の記録は反映されていません。原因を確認してから、もう一度生成・更新してください。"
            )
    if bundle is None:
        st.info("ダウンロードできる出力はまだありません。原因を確認して『CSV・ZIPを生成・更新』から再試行してください。")
        return

    st.caption(f"この出力の生成日時: {bundle.generated_at:%Y-%m-%d %H:%M:%S}")
    non_empty_tables = sum(table.row_count > 0 for table in bundle.tables)
    metric_cols = st.columns(4)
    with metric_cols[0]:
        ab.metric_block("CSVファイル数", f"{len(bundle.tables)} 件", "manifest.csvを別途同梱")
    with metric_cols[1]:
        ab.metric_block("データあり", f"{non_empty_tables} 件", "1行以上のCSV")
    with metric_cols[2]:
        ab.metric_block("CSV合計行数", f"{bundle.total_rows:,} 行", "親・明細テーブルの合計")
    with metric_cols[3]:
        ab.metric_block("生成時刻", bundle.generated_at.strftime("%H:%M:%S"), bundle.generated_at.strftime("%Y-%m-%d"))

    archive_name = f"proloag_all_records_{bundle.generated_at:%Y%m%d_%H%M%S}.zip"
    st.download_button(
        "全記録CSVをまとめてダウンロード", data=bundle.zip_data,
        file_name=archive_name, mime="application/zip", type="primary",
        width="stretch", on_click="ignore",
    )
    st.caption("ZIPには種類別CSVとmanifest.csvが入ります。UTF-8 BOM付きのため、Excelでも日本語を読み込みやすい形式です。")
    if bundle.warnings:
        st.warning("一部ファイルを読み込めませんでした。ZIP内のexport_warnings.csvを確認してください。")
        for warning in bundle.warnings:
            st.caption(warning)

    st.subheader("出力対象")
    summary = pd.DataFrame([
        {"データ": table.label, "ファイル名": table.filename, "行数": table.row_count,
         "列数": table.column_count, "内容": table.description}
        for table in bundle.tables
    ])
    st.dataframe(summary, width="stretch", hide_index=True)
    st.subheader("個別CSV")
    table_options = {f"{table.label}（{table.row_count:,}行）": table for table in bundle.tables}
    selected_label = st.selectbox("ダウンロードするデータ", list(table_options), key="export_dataset")
    selected = table_options[selected_label]
    st.caption(selected.description)
    st.caption(f"列: {', '.join(selected.columns)}")
    st.download_button(
        f"{selected.label}CSVをダウンロード", data=selected.data,
        file_name=selected.filename, mime="text/csv", key=f"download_{selected.key}",
        width="stretch", on_click="ignore",
    )
    with st.expander("出力仕様", expanded=False):
        st.markdown("""
- 辞書型の属性は `condition.effort`、`sleep.sleep_quality` のような列名へ平坦化します。
- リスト型の属性はJSON文字列として元の要素を保持します。
- タスク、カフェイン・成分摂取、成分設定、自己投資、運動明細、統計演習の解答履歴、ノートページ、復習履歴は分析しやすいよう別CSVにも展開します。
- セッションなどの分類IDには、現在のマスタから解決した名称列も追加します。
- `__source_file` はプロジェクトからの相対パスです。開発PCの絶対パスは出力しません。
- `backups/`、旧ノートバックアップ、配布物、ログ、`.env`、`.streamlit/secrets.toml` は対象外です。
""")
