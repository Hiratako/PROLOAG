"""Explicit diagnosis, mapping and reviewed import of the legacy counter CSV."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

import prolog_ui.components as ab
from app_core.legacy_study_csv import diagnose_legacy_csv
from app_core.legacy_study_import import (
    build_import_preview, commit_import, load_import_classifications,
)

STATE_KEY = "_legacy_study_import_ui"
_STATUS = {"ready": "取り込み候補", "review": "確認待ち", "existing": "登録済み候補",
           "error": "エラー", "excluded": "対象外"}
_MASTER = (("projects", "project_id", "プロジェクト"), ("subjects", "subject_id", "科目"),
           ("materials", "material_id", "教材"), ("units", "unit_id", "単元"))


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode("utf-8")).hexdigest()


def _csv_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def csv_bytes(rows: list[dict[str, Any]], columns: list[str] | None = None) -> bytes:
    """Keep numbers as numbers; protect text when the download is opened in Excel."""
    columns = columns or list(dict.fromkeys(key for row in rows for key in row))
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _csv_cell(row.get(key)) for key in columns})
    return buffer.getvalue().encode("utf-8-sig")


def _row_label(row: dict[str, Any]) -> str:
    session = row.get("session") or {}
    return f"{row['row_number']}行｜{str(session.get('start_at', '日時未確定'))[:19]}｜{str(session.get('memo', ''))[:48]}"


def _issue_rows(diagnosis: dict[str, Any]) -> list[dict[str, Any]]:
    result, seen = [], set()
    issues = [(issue.get("row_number"), issue) for issue in diagnosis.get("issues", [])]
    issues.extend((row["row_number"], issue) for row in diagnosis.get("rows", []) for issue in row.get("issues", []))
    for number, issue in issues:
        marker = (number, issue.get("code"), issue.get("message"))
        if marker not in seen:
            seen.add(marker)
            result.append({"元CSV行": number if number is not None else "ファイル全体",
                           "重要度": "エラー" if issue.get("severity") == "error" else "注意",
                           "内容": issue.get("message", ""), "診断コード": issue.get("code", "")})
    return result


def _mapping_controls(diagnosis: dict[str, Any], master: dict[str, Any], key: str) -> dict[str, dict[str, str]]:
    topics = sorted({row.get("legacy_topic", "") for row in diagnosis["rows"]})
    result = {}
    with st.expander("分類を対応させる（任意）", expanded=False):
        st.caption("旧カテゴリごとに登録済みの分類を選べます。すべて未設定でも取り込めます。分類の新規作成はマスタ管理で行ってください。")
        for index, topic in enumerate(topics):
            st.text(f"旧カテゴリ：{topic or '未設定'}")
            selected: dict[str, str] = {}
            columns = st.columns(4)
            for column, (collection, id_key, label) in zip(columns, _MASTER):
                items = [item for item in master.get(collection, [])
                         if isinstance(item, dict) and item.get("is_active", True) and item.get(id_key)]
                if collection != "projects":
                    needed = ("project_id",) if collection == "subjects" else (
                        ("project_id", "subject_id") if collection == "materials" else ("project_id", "subject_id", "material_id"))
                    items = [item for item in items if all(selected.get(parent) and item.get(parent) == selected[parent] for parent in needed)]
                choices = ["", *[str(item[id_key]) for item in items]]
                names = {str(item[id_key]): str(item.get("name") or "名称未設定") for item in items}
                widget_key = f"{key}_map_{index}_{id_key}"
                if st.session_state.get(widget_key) not in choices:
                    st.session_state[widget_key] = ""
                with column:
                    selected[id_key] = st.selectbox(label, choices, key=widget_key,
                                                    format_func=lambda item, labels=names: labels.get(item, "未設定"))
            result[topic] = selected
    return result


def _preview_rows(preview: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for row in preview.get("rows", []):
        session = row.get("session") or {}
        result.append({"元CSV行": row["row_number"], "状態": _STATUS.get(row["status"], row["status"]),
                       "開始日時": session.get("start_at"), "終了日時": session.get("end_at"),
                       "実時間（秒）": session.get("duration_sec"), "換算金額（円）": session.get("money"),
                       "メモ": session.get("memo"), "理由": row.get("reason", "")})
    return result


def _converted_rows(preview: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for row in preview.get("rows", []):
        session = row.get("session")
        if not session:
            continue
        original = {key: value for key, value in row.get("raw", {}).items() if key != "__src"}
        result.append({"source_row": row["row_number"], "status": row["status"],
                       "reason": row.get("reason", ""), "fingerprint": row["fingerprint"],
                       **{key: session.get(key, "") for key in ("start_at", "end_at", "duration_sec", "hourly_rate", "money", "memo",
                                                              "project_id", "subject_id", "material_id", "unit_id")},
                       "condition": session.get("condition", {}), "legacy_original": original})
    return result


def render_legacy_study_import(root: Path) -> None:
    root = Path(root).resolve()
    state = st.session_state.get(STATE_KEY)
    if not isinstance(state, dict) or state.get("root") != str(root):
        state = {"root": str(root), "diagnosis": None, "preview": None, "inputs": None,
                 "error": None, "result": None}
        st.session_state[STATE_KEY] = state
    st.info("旧式秒級カウンターのCSVを現行形式へ移行します。診断・変換プレビューでは保存記録を変更しません。元CSVへの上書きは行いません。")
    st.caption("セッション明細が取り込み対象です。日次集計は件数・時間・金額の照合にだけ使います。対象はこの旧版形式のCSVです（各10MB・明細10,000件まで）。")
    uploads = st.columns(2)
    with uploads[0]:
        session_file = st.file_uploader("セッション明細CSV（study_sessions）", type=["csv"], key="legacy_session_upload")
    with uploads[1]:
        summary_file = st.file_uploader("日次集計CSV（照合用・任意）", type=["csv"], key="legacy_summary_upload")
    if session_file is None:
        state.update(diagnosis=None, preview=None, inputs=None, known_review_numbers=[])
        st.caption("セッション明細CSVを選択して「ファイルを診断」を押してください。")
        return
    session_data = session_file.getvalue()
    summary_data = summary_file.getvalue() if summary_file is not None else None
    current_inputs = (hashlib.sha256(session_data).hexdigest(),
                      hashlib.sha256(summary_data).hexdigest() if summary_data is not None else None)
    if state.get("inputs") != current_inputs:
        state.update(inputs=current_inputs, diagnosis=None, preview=None, error=None, result=None, known_review_numbers=[])
    if st.button("ファイルを診断", type="primary", key="legacy_diagnose"):
        state.update(preview=None, error=None, result=None)
        try:
            with st.spinner("旧CSVの形式と合計を確認しています..."):
                state["diagnosis"] = diagnose_legacy_csv(session_data, summary_data)
        except (ValueError, OSError, RuntimeError) as exc:
            state["diagnosis"] = None
            state["error"] = str(exc)
    if state.get("error"):
        st.error(state["error"])
    diagnosis = state.get("diagnosis")
    if diagnosis is None:
        return
    summary = diagnosis["summary"]
    st.subheader("ファイルの診断")
    metrics = st.columns(4)
    for column, label, value in zip(metrics, ("明細", "記録日数", "記録時間", "換算金額"), (
        f"{summary.get('row_count', 0):,}件", f"{summary.get('date_count', 0):,}日",
        f"{float(summary.get('duration_sec') or 0) / 3600:,.3f}時間", f"{float(summary.get('money') or 0):,.2f}円")):
        with column:
            st.metric(label, value)
    if summary.get("first_date"):
        st.caption(f"対象期間：{summary['first_date']} 〜 {summary['last_date']}。小数秒・元の換算金額を保持します。睡眠区分から正確な睡眠時間や健康ログは生成しません。")
    issues = _issue_rows(diagnosis)
    if issues:
        st.dataframe(pd.DataFrame(issues)[["元CSV行", "重要度", "内容"]], hide_index=True, width="stretch")
    else:
        st.success("CSV内の形式に問題は見つかりませんでした。既存記録との照合は次のプレビューで行います。")
    reconciliation = diagnosis.get("daily_reconciliation", [])
    if reconciliation:
        with st.expander("日次集計との照合"):
            st.caption("元の精度で合計した値と照合します。旧表示の秒未満・小数金額の丸め差は区別します。")
            checks = pd.DataFrame(reconciliation)
            checks["status"] = checks["status"].map({"ok": "一致", "warning": "要確認", "error": "不一致"})
            checks = checks.rename(columns={"date": "日付", "count": "明細件数", "duration_sec": "明細合計（秒）",
                "money": "明細合計（円）", "summary_count": "日次集計件数", "summary_duration_sec": "日次集計（秒）",
                "summary_money": "日次集計（円）", "count_delta": "件数差", "duration_delta_sec": "時間差（秒）",
                "money_delta": "金額差（円）", "status": "判定"})
            st.dataframe(checks, hide_index=True, width="stretch")
    st.download_button("診断結果CSVをダウンロード", csv_bytes(issues, ["元CSV行", "重要度", "内容", "診断コード"]),
                       file_name="legacy_study_diagnostics.csv", mime="text/csv", on_click="ignore", key="legacy_diagnostics_download")
    if not diagnosis.get("rows"):
        return
    try:
        master = load_import_classifications(root)
    except (ValueError, OSError, RuntimeError) as exc:
        st.error(f"分類を読み込めませんでした。保存は行っていません。{exc}")
        return
    scope_key = "legacy_" + _digest([str(root), *current_inputs])[:16]
    classification_map = _mapping_controls(diagnosis, master, scope_key)
    source_rows = {row["row_number"]: row for row in diagnosis["rows"]}
    excluded = st.multiselect("取り込まない行（任意）", list(source_rows),
                              format_func=lambda number: _row_label(source_rows[number]), key=f"{scope_key}_excluded")
    warnings = {row["row_number"] for row in diagnosis["rows"]
                if any(issue.get("severity") == "warning" for issue in row.get("issues", []))}
    warnings.update(state.get("known_review_numbers", []))
    for row in (state.get("preview") or {}).get("rows", []):
        if row.get("status") == "review":
            warnings.add(row["row_number"])
    state["known_review_numbers"] = sorted(warnings)
    acknowledged = st.multiselect("内容を確認し、そのまま取り込む要確認行", sorted(warnings),
                                  format_func=lambda number: _row_label(source_rows[number]), key=f"{scope_key}_ack")
    st.caption("未確認の行は保留します。不正な入出力区分は元値を来歴に残し、未設定として扱います。時間帯の重なりを認める行だけ確認済みにしてください。")
    selected = sorted(set(source_rows) - set(excluded))
    selection = _digest({"inputs": current_inputs, "mapping": classification_map, "selected": selected,
                         "acknowledged": sorted(acknowledged)})
    if st.button("既存記録と照合して変換プレビュー", key="legacy_build_preview", type="primary"):
        state.update(error=None, result=None)
        try:
            with st.spinner("保存済みログとの重複・分類・反映候補を確認しています..."):
                preview = build_import_preview(diagnosis, root, classification_map=classification_map,
                                               selected_rows=selected, acknowledged_rows=sorted(acknowledged))
            state.update(preview=preview, preview_selection=selection)
            discovered = {row["row_number"] for row in preview.get("rows", []) if row.get("status") == "review"}
            if discovered - warnings:
                # Expose newly discovered existing-log conflicts immediately.
                # The fixed preview is reused; this does not rebuild the plan.
                state["known_review_numbers"] = sorted(warnings | discovered)
                st.rerun()
        except (ValueError, OSError, RuntimeError) as exc:
            state.update(preview=None, error=str(exc))
            ab.show_action_error(f"変換プレビューを作成できませんでした。{exc}")
    preview = state.get("preview")
    if preview is None:
        return
    st.subheader("反映前のプレビュー")
    totals = preview["summary"]
    st.write(" / ".join(f"{_STATUS[status]} {totals.get(status + '_count', 0):,}件"
                        for status in ("ready", "review", "existing", "error", "excluded")))
    st.write(f"追加候補の合計：{float(totals.get('duration_sec', 0)) / 3600:,.3f}時間・{float(totals.get('money', 0)):,.2f}円")
    display_rows = _preview_rows(preview)
    st.dataframe(pd.DataFrame(display_rows), hide_index=True, width="stretch")
    comparisons = []
    for row in preview.get("rows", []):
        for candidate in row.get("existing_candidates", []):
            comparisons.append({"元CSV行": row["row_number"], "状態": _STATUS.get(row["status"], row["status"]),
                                "既存の記録ID": candidate.get("session_id") or "ID未設定",
                                "既存の開始日時": candidate.get("start_at", ""),
                                "既存の終了日時": candidate.get("end_at", ""),
                                "既存の実時間（秒）": candidate.get("duration_sec"),
                                "既存の換算金額（円）": candidate.get("money"),
                                "既存のメモ": candidate.get("memo", ""),
                                "確認元": candidate.get("source_file", "")})
    if comparisons:
        with st.expander("既存ログとの比較", expanded=totals.get("review_count", 0) > 0):
            st.dataframe(pd.DataFrame(comparisons), hide_index=True, width="stretch")
    outputs = st.columns(2)
    with outputs[0]:
        st.download_button("反映候補・判定結果CSVをダウンロード", csv_bytes(display_rows),
                           file_name="legacy_study_import_preview.csv", mime="text/csv", on_click="ignore", key="legacy_preview_download")
    with outputs[1]:
        st.download_button("現行形式への変換コピーをダウンロード", csv_bytes(_converted_rows(preview)),
                           file_name="legacy_study_converted.csv", mime="text/csv", on_click="ignore", key="legacy_converted_download")
    current = state.get("preview_selection") == selection
    if not current:
        st.warning("ファイル・分類・対象行・確認済み行の条件が変わりました。変換プレビューを作り直してください。")
    if state.get("result"):
        result = state["result"]
        st.success(f"{result.get('added_count', 0):,}件の反映が完了しています。同じ操作の再送では二重追加しません。")
        st.caption(f"操作番号：{result['operation_id']}。変更前のバックアップは backups/json_operations/ 内に保存されます。")
    can_commit = current and totals.get("ready_count", 0) > 0 and not totals.get("error_count", 0) and not state.get("result")
    confirm = st.checkbox("取り込み候補と保留行を確認し、候補だけを現在の勉強ログに追加する",
                          key=f"{scope_key}_{preview['operation_id']}_confirm")
    st.caption("確定時に変更前の記録をバックアップします。確認後に保存記録が変わっていた場合は停止します。既存記録の分類・金額は書き換えません。")
    if st.button("バックアップして取り込み候補を反映", disabled=not (can_commit and confirm),
                 type="primary", key="legacy_commit"):
        try:
            with st.spinner("バックアップを取り、確認した候補を保存しています..."):
                result = commit_import(preview, root)
        except (ValueError, OSError, RuntimeError) as exc:
            state["error"] = str(exc)
            ab.show_action_error(f"反映を完了できませんでした。{exc}")
        else:
            state.update(result=result, error=None)
            ab.queue_success(f"旧版の勉強記録を{result.get('added_count', 0):,}件反映しました。")
            st.rerun()
