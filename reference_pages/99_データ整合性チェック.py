from __future__ import annotations

from web_demo.runtime import fragment as demo_fragment

import pandas as pd
import streamlit as st

import prolog_ui.components as ab
from app_core.integrity_service import run_integrity_checks


from app_core.settings_service import load_user_settings as _load_page_settings
PAGE_SETTINGS = _load_page_settings()

def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


st.set_page_config(page_title="データ整合性チェック", layout="wide")
ab.inject_css(settings=PAGE_SETTINGS)
ab.sidemenu("JSON構造、日付、参照、値、設定を読み取り専用で確認します。", settings=PAGE_SETTINGS)

ab.topbar(
    "データ整合性チェック",
    "保存データ全体のJSON構造、ID参照、日付配置、値域、階層、設定を横断して確認します。",
)
st.caption("読み取り専用です。自動修復・自動保存・JSONの書き戻しは行いません。エラーは整合性への影響がある項目、注意は旧形式や確認推奨項目です。")

if st.button("チェックを実行する", type="primary", width="stretch"):
    with st.spinner("保存データを検査しています..."):
        st.session_state.integrity_result = run_integrity_checks()

result = st.session_state.get("integrity_result")
if not result:
    st.info("チェックを実行すると、結果がここに表示されます。")
    st.stop()

st.caption(f"チェック実行日時: {result['checked_at']}（ローカルPC時刻）")
cols = st.columns(4)
with cols[0]:
    ab.metric_block("対象ファイル", f"{result.get('checked_file_count', 0):,}", "読み取ったJSON")
with cols[1]:
    ab.metric_block("総チェック件数", f"{result['total_checks']:,}", "読み取り検査の件数")
with cols[2]:
    ab.metric_block("エラー", f"{result.get('error_count', result['issue_count']):,}", "整合性への影響あり")
with cols[3]:
    ab.metric_block("注意", f"{result.get('notice_count', 0):,}", "旧形式・確認推奨")

with st.expander("今回の検査範囲", expanded=False):
    scope = result.get("check_scope", [])
    if scope:
        st.dataframe(pd.DataFrame({"検査項目": scope}), width="stretch", hide_index=True)
    else:
        st.caption("検査範囲情報はありません。")

issues = result.get("issues", [])
if not issues:
    st.success("検査した範囲では、エラーも注意項目も見つかりませんでした。")
    empty_df = pd.DataFrame(columns=["重要度", "問題カテゴリ", "対象ファイル", "対象ID", "内容", "推奨対応"])
    st.download_button(
        "整合性チェック結果CSVをダウンロード",
        data=to_csv_bytes(empty_df),
        file_name="integrity_check_result.csv",
        mime="text/csv",
        width="stretch", on_click="ignore")
    st.stop()

df = pd.DataFrame(issues).reindex(columns=["重要度", "問題カテゴリ", "対象ファイル", "対象ID", "内容", "推奨対応"])
error_count = int(result.get("error_count", (df["重要度"] == "エラー").sum()))
notice_count = int(result.get("notice_count", (df["重要度"] == "注意").sum()))
if error_count:
    st.error(f"エラーが {error_count} 件あります。自動修復はせず、対象ファイルをバックアップしてから内容を確認してください。")
elif notice_count:
    st.warning(f"エラーはありません。確認推奨の注意項目が {notice_count} 件あります。")

@demo_fragment
def render_issue_results(issue_frame: pd.DataFrame) -> None:
    st.subheader("検出結果")
    filter_cols = st.columns(2)
    severity_options = [value for value in ("エラー", "注意") if value in set(issue_frame["重要度"].dropna())]
    with filter_cols[0]:
        selected_severities = st.multiselect(
            "重要度",
            options=severity_options,
            default=severity_options,
            key="integrity_severity_filter",
        )
    category_options = sorted(str(value) for value in issue_frame["問題カテゴリ"].dropna().unique())
    with filter_cols[1]:
        selected_category = st.selectbox(
            "問題カテゴリ",
            options=["すべて", *category_options],
            key="integrity_category_filter",
        )
    filtered = issue_frame[issue_frame["重要度"].isin(selected_severities)] if selected_severities else issue_frame.iloc[0:0]
    if selected_category != "すべて":
        filtered = filtered[filtered["問題カテゴリ"] == selected_category]
    st.caption(f"表示 {len(filtered):,} 件 / 全 {len(issue_frame):,} 件")
    st.dataframe(filtered, width="stretch", hide_index=True)


render_issue_results(df)

st.subheader("問題カテゴリ別件数")
category_counts = (
    df.groupby(["重要度", "問題カテゴリ"], dropna=False)
    .size()
    .reset_index(name="件数")
    .sort_values(["重要度", "件数", "問題カテゴリ"], ascending=[True, False, True])
)
st.dataframe(category_counts, width="stretch", hide_index=True)

st.download_button(
    "整合性チェック結果CSVをダウンロード",
    data=to_csv_bytes(df),
    file_name="integrity_check_result.csv",
    mime="text/csv",
    width="stretch", on_click="ignore")
