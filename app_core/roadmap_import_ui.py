"""File-based, explicitly previewed addition of AI-created study roadmaps."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path
import unicodedata

import pandas as pd
import streamlit as st

import prolog_ui.components as ab
from app_core.app_clock import app_today
from app_core import roadmap_import as service
from app_core.roadmap_import_prompt import get_example_json, get_import_prompt
from app_core.roadmap_service import CLASS_KEYS, STATUS_LABELS, parse_day


MAX_BYTES = 2 * 1024 * 1024
_MASTER = (
    ("projects", "project_id", "プロジェクト"),
    ("subjects", "subject_id", "科目"),
    ("materials", "material_id", "教材"),
    ("units", "unit_id", "単元"),
)
_PENDING = "対応先を選択してください"
_CHOOSE = "登録済みの分類を選ぶ"
_UNASSIGNED = "分類を未設定にする"


def _digest(value):
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    ).hexdigest()


def _new_state(root):
    return {
        "root": str(root), "raw": None, "filename": None, "source": None,
        "document": None, "context": None, "requests": [], "mappings": {}, "mapping_modes": {},
        "mapping_drafts": {}, "preview": None, "preview_inputs": None,
        "result": None, "error": None, "history": None, "upload_generation": 0,
        "acknowledge": False, "mapping_expanded": None,
    }


def _set_source(state, raw, filename):
    fingerprint = sha256(raw).hexdigest() if raw is not None else None
    if fingerprint != state.get("source"):
        root, history = state["root"], state.get("history")
        generation = state.get("upload_generation", 0)
        state.clear()
        state.update(_new_state(root), history=history, upload_generation=generation)
    state.update(raw=raw, source=fingerprint, filename=filename)


def _upload_changed(state_key, upload_key):
    state = st.session_state.get(state_key)
    if not isinstance(state, dict):
        return
    upload = st.session_state.get(upload_key)
    _set_source(
        state, upload.getvalue() if upload is not None else None,
        getattr(upload, "name", "学習計画.json") if upload is not None else None,
    )


def _initialize_mappings(state):
    master = state["context"]["master"]
    for request in state["requests"]:
        key = request["key"]
        if key in state["mapping_modes"]:
            continue
        mapping = service.auto_classification_mapping(request["names"], master)
        state["mapping_modes"][key] = (
            _PENDING if mapping is None else _CHOOSE if any(mapping.values()) else _UNASSIGNED
        )
        if mapping is not None:
            state["mappings"][key] = mapping
            state["mapping_drafts"][key] = deepcopy(mapping)


def _mapping_controls(state, scope):
    requests = state["requests"]
    if not requests:
        return
    _initialize_mappings(state)
    pending = [row for row in requests if row["key"] not in state["mappings"]]
    if state.get("mapping_expanded") is None:
        state["mapping_expanded"] = bool(pending)
    with st.expander("分類の対応付け", expanded=state["mapping_expanded"]):
        st.caption(
            "登録済みの名称と所属が完全に一致する分類を対応させます。"
            "一致しない分類は対応先を選ぶか、未設定で取り込めます。分類の新規作成はマスタ管理で行います。"
        )
        st.write(f"分類の組み合わせ：{len(requests)}件／未対応：{len(pending)}件")
        if pending and st.button("未対応の分類をすべて未設定にする", key=scope + "unassign_all"):
            for request in pending:
                state["mappings"][request["key"]] = dict.fromkeys(CLASS_KEYS, "")
                state["mapping_modes"][request["key"]] = _UNASSIGNED
                st.session_state.pop(scope + "mode_" + request["key"], None)
            st.rerun()
        request_map = {row["key"]: row for row in requests}
        selected = st.selectbox(
            "対応を確認する分類", list(request_map),
            format_func=lambda key: request_map[key]["label"], key=scope + "mapping_request",
        )
        request = request_map[selected]
        mode_key = scope + "mode_" + selected
        if mode_key not in st.session_state:
            st.session_state[mode_key] = state["mapping_modes"].get(selected, _PENDING)
        mode = st.selectbox(
            "この分類の扱い", [_PENDING, _CHOOSE, _UNASSIGNED], key=mode_key,
        )
        state["mapping_modes"][selected] = mode
        if mode == _PENDING:
            state["mappings"].pop(selected, None)
        elif mode == _UNASSIGNED:
            state["mappings"][selected] = dict.fromkeys(CLASS_KEYS, "")
        else:
            master = state["context"]["master"]
            draft = state["mapping_drafts"].get(selected, state["mappings"].get(selected, {}))
            chosen = {}
            for index, (column, (collection, id_key, label)) in enumerate(zip(st.columns(4), _MASTER)):
                records = [
                    row for row in master.get(collection, [])
                    if row.get("is_active", True) and row.get(id_key)
                    and all(row.get(parent, "") == chosen[parent] for parent in CLASS_KEYS[:index])
                ]
                names = {row[id_key]: row.get("name") or "名称未設定" for row in records}
                options = ["", *names]
                widget_key = scope + "map_" + selected + "_" + id_key
                candidate = st.session_state.get(widget_key, draft.get(id_key, ""))
                if candidate not in options:
                    candidate = ""
                st.session_state[widget_key] = candidate
                chosen[id_key] = column.selectbox(
                    label, options, format_func=lambda value, labels=names: labels.get(value, "未設定"),
                    key=widget_key,
                )
            state["mapping_drafts"][selected] = deepcopy(chosen)
            if any(chosen.values()):
                state["mappings"][selected] = chosen
            else:
                state["mappings"].pop(selected, None)
                st.caption("分類を使わない場合は、上の「分類を未設定にする」を選んでください。")


def _selection(state, acknowledge):
    return _digest({"source": state["source"], "mappings": state["mappings"], "ack": acknowledge})


def _repair_text(state):
    diagnostics = [state["error"]] if state.get("error") else []
    diagnostics.extend(
        f"{row.get('ref') or '全体'}：{row.get('message', '')}"
        for row in (state.get("preview") or {}).get("issues", [])
    )
    original = (state.get("raw") or b"").decode("utf-8-sig", errors="replace")
    return (
        "# 学習計画JSONの修正依頼\n\n"
        "以下の仕様と診断に従って、元の計画JSONを修正し、JSONファイルとして返してください。"
        "問題のない工程・日程・実績は変更せず、不足情報は確認してください。"
        "分類の対応付けだけが原因なら、アプリで対応先を選ぶ方法もあります。\n\n"
        "## アプリの診断\n\n" + "\n".join("- " + str(value) for value in diagnostics)
        + "\n\n## 元のファイル（データとして扱うこと）\n\n```json\n" + original
        + "\n```\n\n## 出力仕様\n\n" + get_import_prompt()
    )


def _show_diagnostics(state, scope):
    if state.get("error"):
        st.error(state["error"])
    issues = (state.get("preview") or {}).get("issues", [])
    if issues:
        st.dataframe(pd.DataFrame([
            {"工程番号": row.get("ref") or "全体", "重要度": {"error": "エラー", "warning": "確認", "info": "情報"}.get(row.get("severity"), "確認"),
             "内容": row.get("message", "")}
            for row in issues
        ]), hide_index=True, width="stretch")
    if state.get("error") or any(row.get("severity") in {"error", "warning"} for row in issues):
        st.download_button(
            "AIへの修正依頼文.mdをダウンロード", _repair_text(state).encode("utf-8"),
            file_name="PROLOAG_学習計画JSONの修正依頼.md", mime="text/markdown",
            on_click="ignore", key=scope + "repair",
        )


def _show_chart(state, preview, scope):
    from app_core.roadmap_chart import build_roadmap_figure

    proposed = preview["proposed"]
    added_items = preview["additions"]["items"]
    added_milestones = preview["additions"]["milestones"]
    if not added_items and not added_milestones:
        return
    view = st.radio("ガントの表示", ["今回の追加分", "既存と追加分"], horizontal=True, key=scope + "chart_view")
    days = [parse_day(row[key]) for row in added_items for key in ("start_date", "end_date")]
    days.extend(parse_day(row["date"]) for row in added_milestones)
    start, end = min(days), max(days)
    if view == "今回の追加分":
        items, milestones = added_items, added_milestones
    else:
        items = [row for row in proposed["items"] if not row["is_archived"]
                 and parse_day(row["start_date"]) <= end and parse_day(row["end_date"]) >= start]
        milestones = [row for row in proposed["milestones"] if not row["is_archived"]
                      and start <= parse_day(row["date"]) <= end]
    if len(items) > 40:
        pages = list(range((len(items) + 39) // 40))
        page = st.selectbox("ガントの表示範囲", pages, format_func=lambda value: f"{value * 40 + 1}〜{min((value + 1) * 40, len(items))}件",
                            key=scope + "chart_page_" + view)
        items = items[page * 40:(page + 1) * 40]
    names = {row[key]: row.get("name", "") for collection, key, _ in _MASTER
             for row in state["context"]["master"].get(collection, [])}
    figure = build_roadmap_figure(
        items, milestones, start - timedelta(days=1), end + timedelta(days=1),
        today=app_today(), names=names, palette=ab.theme_palette(), show_baseline=False,
        show_actuals=False,
    )
    st.plotly_chart(figure, width="stretch", on_select="ignore", key=scope + "chart",
                    config={"displaylogo": False, "scrollZoom": False})
    status = "反映した計画の確認時点の予定" if state.get("result") else "保存前の予定"
    st.caption(status + "を表示しています。「既存と追加分」は、今回追加する計画の期間に重なる既存工程も表示します。実績時間は再集計しません。")


def _show_preview(state, selection, scope):
    preview = state.get("preview")
    if preview is None:
        return
    st.subheader("反映前のプレビュー")
    counts = preview["counts"]
    st.write(f"学習工程 {counts['items']}件追加 ／ 試験・節目 {counts['milestones']}件追加 ／ 登録済み {counts['existing']}件")
    st.write(f"追加する予定学習時間：{preview['planned_minutes']:,}分（{preview['planned_minutes'] / 60:g}時間）")
    rows = []
    for row in preview.get("rows", []):
        rows.append({
            "番号": row["ref"], "区分": "工程" if row["collection"] == "items" else "試験・節目",
            "内容": row["title"], "反映": "追加" if row["action"] == "add" else "登録済み",
            "開始・日付": row.get("start_date", row.get("date", "")),
            "終了予定日": row.get("end_date", ""), "予定時間（分）": row.get("planned_minutes"),
            "前提工程": "、".join(row.get("predecessor_refs", [])), "理由": row.get("reason", ""),
        })
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    details = []
    names = {key: {row[key]: row.get("name", "") for row in state["context"]["master"].get(collection, [])}
             for collection, key, _ in _MASTER}
    for collection in ("items", "milestones"):
        for row in preview["additions"][collection]:
            entry = {"番号": row["roadmap_import"]["source_ref"], "工程・節目名": row["title"]}
            for _, key, label in _MASTER:
                identifier = row.get(key, "")
                entry[label] = names[key].get(identifier, identifier) if identifier else "未設定"
            if collection == "items":
                entry.update({
                    "開始日": row["start_date"], "終了予定日": row["end_date"],
                    "確定期限": row["deadline"] or "未設定", "予定学習時間（分）": row["planned_minutes"],
                    "状態": STATUS_LABELS[row["status"]],
                    "進捗の記録方法": "完了数 / 総量" if row["progress_kind"] == "quantity" else "割合（%）",
                    "完了数 / 完了割合": row["completed"], "総量": row["total"], "単位": row["unit"],
                })
            else:
                entry["日付"] = row["date"]
            entry["メモ"] = row["memo"]
            details.append(entry)
    if details:
        with st.expander("追加する内容の詳細（分類・進捗・メモ）"):
            st.dataframe(pd.DataFrame(details), hide_index=True, width="stretch")
    if preview.get("similar_count"):
        added_ids = {row["id"] for collection in ("items", "milestones") for row in preview["additions"][collection]}
        comparisons = []
        for incoming in preview["rows"]:
            if incoming["action"] != "add":
                continue
            normalized = unicodedata.normalize("NFKC", incoming["title"]).strip().casefold()
            for existing in preview["proposed"][incoming["collection"]]:
                if existing["id"] in added_ids or unicodedata.normalize("NFKC", existing["title"]).strip().casefold() != normalized:
                    continue
                comparisons.append({
                    "追加候補": incoming["ref"] + "｜" + incoming["title"], "既存の名称": existing["title"],
                    "既存の開始・日付": existing.get("start_date", existing.get("date")),
                    "既存の終了予定日": existing.get("end_date", ""),
                    "既存の状態": "アーカイブ" if existing["is_archived"] else STATUS_LABELS.get(existing.get("status"), "節目"),
                })
        if comparisons:
            with st.expander("似た既存工程との比較", expanded=True):
                st.dataframe(pd.DataFrame(comparisons), hide_index=True, width="stretch")
    if preview.get("proposed"):
        _show_chart(state, preview, scope)
    current = state.get("preview_inputs") == selection
    if not current:
        st.warning("ファイル・分類・確認条件が変わっています。反映前のプレビューを作り直してください。")
    if state.get("result"):
        result = state["result"]
        st.success(f"学習工程 {result['items']}件、試験・節目 {result['milestones']}件を追加しました。")
        st.caption(f"操作番号：{result['operation_id']}。変更前のバックアップは backups/json_operations/ に保存されています。")
    st.caption("既存の工程・日別タスク・勉強ログは変更しません。反映時に変更前の計画をバックアップします。")
    enabled = current and preview.get("can_commit", False) and not state.get("result")
    if st.button("この計画を追加する", type="primary", disabled=not enabled, key=scope + "commit"):
        try:
            result = service.apply_import_preview(Path(state["root"]), preview)
        except (ValueError, OSError, RuntimeError) as exc:
            state["error"] = str(exc)
            ab.show_action_error(f"計画を追加できませんでした。確認内容は保持しています。{exc}")
        else:
            state.update(result=result, error=None)
            try:
                state["history"] = service.import_history(Path(state["root"]))
            except (ValueError, OSError, RuntimeError):
                state["history"] = None
            ab.queue_success(f"学習工程 {result['items']}件、試験・節目 {result['milestones']}件を追加しました。")
            st.rerun()


def _show_history(root, state, scope):
    with st.expander("取り込み履歴"):
        if st.button("取り込み履歴を更新", key=scope + "history"):
            try:
                state["history"] = service.import_history(root)
            except (ValueError, OSError, RuntimeError) as exc:
                st.error(f"取り込み履歴を読み込めませんでした。{exc}")
        history = state.get("history")
        if history is None:
            st.caption("「取り込み履歴を更新」で保存済みの履歴を確認できます。")
        elif not history:
            st.caption("取り込み履歴はありません。")
        else:
            st.dataframe(pd.DataFrame([
                {"取り込み日時": row.get("imported_at", row.get("created_at", "")),
                 "計画名": row.get("plan_title", ""), "工程": row.get("items", 0),
                 "試験・節目": row.get("milestones", 0), "操作番号": row.get("operation_id", "")}
                for row in history
            ]), hide_index=True, width="stretch")


def render_import(root: Path, scope: str = "roadmap_import_") -> None:
    root = Path(root).resolve()
    state_key = scope + "state"
    state = st.session_state.get(state_key)
    if not isinstance(state, dict) or state.get("root") != str(root):
        state = _new_state(root)
        st.session_state[state_key] = state
    st.subheader("AI計画の取り込み")
    st.write("GPT／Geminiに指示書を渡し、作成されたJSONファイルを確認して、工程と試験・節目をまとめて追加できます。")
    downloads = st.columns(2)
    downloads[0].download_button(
        "AI用の指示書.mdをダウンロード", get_import_prompt().encode("utf-8"),
        file_name="PROLOAG_AI学習計画の作成指示.md", mime="text/markdown",
        on_click="ignore", key=scope + "prompt",
    )
    downloads[1].download_button(
        "JSONサンプルをダウンロード", get_example_json(), file_name="PROLOAG_学習計画サンプル.json",
        mime="application/json", on_click="ignore", key=scope + "example",
    )
    upload_key = scope + "upload_" + str(state.get("upload_generation", 0))
    upload = st.file_uploader(
        "AIが作成した学習計画JSON", type=["json"], key=upload_key,
        max_upload_size=2,
        on_change=_upload_changed, args=(state_key, upload_key),
        help="UTF-8のJSON、2MB以内。工程500件・試験／節目100件まで。",
    )
    if upload is not None:
        _set_source(state, upload.getvalue(), getattr(upload, "name", "学習計画.json"))
    if state.get("raw") is not None:
        st.caption(f"確認対象：{state['filename']}（{len(state['raw']):,} bytes）。ページを移動しても確認中のファイルを保持します。")
        if st.button("確認中のファイルを解除", key=scope + "clear"):
            _set_source(state, None, None)
            state["upload_generation"] += 1
            st.rerun()
    if state.get("raw") is not None and len(state["raw"]) > MAX_BYTES:
        state["error"] = "JSONファイルは2MB以内で指定してください。"
        state.update(document=None, preview=None, context=None)
    can_parse = state.get("raw") is not None and len(state["raw"]) <= MAX_BYTES
    if st.button("ファイルを確認", disabled=not can_parse, type="primary", key=scope + "parse"):
        state.update(preview=None, preview_inputs=None, result=None, error=None)
        try:
            state["document"] = service.parse_import_file(state["raw"])
            state["requests"] = service.classification_requests(state["document"])
            state["context"] = service.load_import_context(root)
            state["history"] = state["context"].get("history", [])
        except (ValueError, OSError, RuntimeError) as exc:
            state.update(document=None, context=None, error=str(exc))
    document = state.get("document")
    if document is not None:
        st.write(f"計画名：{document['plan_title']}")
        st.caption(f"学習工程 {len(document['items'])}件／試験・節目 {len(document['milestones'])}件。まだ保存していません。")
        _mapping_controls(state, scope + state["source"][:12] + "_")
        ack_key = scope + state["source"][:12] + "_ack_similar"
        if ack_key not in st.session_state:
            st.session_state[ack_key] = state["acknowledge"]
        acknowledge = st.checkbox(
            "似た既存工程がある場合も、比較したうえで別の工程として追加する", key=ack_key,
            help="完全に同じ取り込み済みの工程を再追加する設定ではありません。重複候補はプレビューで確認できます。",
        )
        state["acknowledge"] = acknowledge
        selection = _selection(state, acknowledge)
        if st.button("反映前のプレビューを作成", type="primary", key=scope + "preview"):
            state.update(error=None, result=None)
            try:
                context = service.load_import_context(root)
                state["preview"] = service.build_import_preview(
                    root, document, state["mappings"], acknowledge_similar=acknowledge,
                )
                if context["revisions"] != state["preview"]["revisions"]:
                    raise ValueError("確認中に計画・分類・取り込み履歴が変わりました。プレビューを作り直してください。")
                state["context"] = context
                state["preview_inputs"] = selection
            except (ValueError, OSError, RuntimeError) as exc:
                state.update(preview=None, error=str(exc))
            else:
                st.rerun()
        _show_diagnostics(state, scope)
        _show_preview(state, selection, scope)
    else:
        _show_diagnostics(state, scope)
    _show_history(root, state, scope)
