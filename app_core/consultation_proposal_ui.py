"""Manual local proposal inbox. No model, Google, or linked-resource requests."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import streamlit as st
import prolog_ui.components as ab

from app_core.consultation_proposal_contract import (
    MAX_IMPORT_BYTES, MAX_SOURCE_LABEL_LENGTH, MAX_TEXT_LENGTH, ProposalError,
    parse_proposal_payload, proposal_request_template, _timestamp,
)
from app_core.analysis_dates import parse_analysis_datetime
from app_core.consultation_proposal_service import (
    approve_application, import_proposals, load_inbox_snapshot, prepare_application,
    prepare_revert, revert_application, set_proposal_status,
)
from app_core.json_operations import operation_guard
from app_core.json_store import JsonStoreError, load_json_safe, resolved_json_path
from app_core.learning_context_service import list_materials


STATUSES = {"pending": "未確認", "deferred": "保留", "rejected": "見送り", "applied": "採用済み", "reverted": "取消済み"}
KINDS = {"reference": "参照のみ（反映先をまだ選ばない）", "next_step": "教材の『次はここから』", "daily_memo": "日次の相談メモ"}


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _error(exc: Exception, *, action: bool = False) -> None:
    message = f"処理できませんでした。入力は保持しています。{exc}"
    if action:
        ab.show_action_error(message, toast_message="相談提案の操作を完了できませんでした。入力は保持しています。")
    else:
        st.error(message)


def _show_candidate(candidate: dict) -> None:
    proposal, source = candidate["proposal"], candidate["source"]
    st.text(proposal["title"])
    st.text(proposal["text"])
    if proposal.get("reason"):
        st.caption("提案理由")
        st.text(proposal["reason"])
    if source.get("label"):
        st.caption("相談元")
        st.text(source["label"])
    if source.get("report_id"):
        st.caption("元レポートID")
        st.text(source["report_id"])
    if source.get("label") or source.get("report_id"):
        st.caption("出典ID・相談元は入力された情報です。実際に参照した文書・日時は確認してください。")
    target = proposal.get("target", {})
    if target:
        st.caption("提案された反映先（ヒントのみ・自動選択しません）")
        st.text(json.dumps(target, ensure_ascii=False, indent=2))


def _show_change(preview: dict) -> None:
    st.caption("反映先")
    st.text(json.dumps(preview.get("target", {}), ensure_ascii=False, indent=2))
    columns = st.columns(2)
    for column, field, label in ((columns[0], "before", "変更前"), (columns[1], "after", "変更後")):
        with column:
            st.caption(label)
            value = preview.get(field)
            st.text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))
    st.caption("この差分を固定して反映します。本文・反映先を変更したら、差分を作り直してください。")


def _clear_editor(prefix: str, notice: str = "") -> None:
    st.session_state.pop(prefix + "_edit", None)
    st.session_state[prefix + "_selection"] = ""
    if notice:
        st.session_state[prefix + "_notice"] = notice


def _reload_controls(edit: dict, prefix: str) -> tuple[bool, bool]:
    reload = edit.get("reload")
    if not reload:
        return False, False
    st.caption("再読込する提案の比較（まだ採用・反映はしません）")
    latest = reload["snapshot"]["data"]["proposals"].get(edit["proposal_id"])
    for column, value, label in zip(st.columns(2), (edit["entry"], latest), ("編集開始時", "最新の保存内容")):
        with column:
            st.caption(label)
            st.text(json.dumps(value, ensure_ascii=False, indent=2))
    st.caption("タイトル・本文・反映先の入力は保持します。古い採用・取消差分と同意は失効し、反映には新しい差分の作成と承認が必要です。削除・採用済み・取消済みの場合は再採用できません。")
    approved = st.checkbox("最新との差を確認し、入力を保持して読み直します", key=f"{prefix}_reload_approve_{reload['token']}")
    requested = st.form_submit_button("入力を保持して最新を再読込")
    return approved, requested


def _reload_editor(root: Path, prefix: str, edit: dict, *, compare: bool, approved: bool) -> None:
    # Invalidating the old signed preview never approves or mutates a target.
    edit.pop("preview", None)
    try:
        snapshot = load_inbox_snapshot(root)
        if compare:
            edit["reload"] = {"snapshot": snapshot, "token": uuid4().hex}
        else:
            if not approved:
                raise ProposalError("最新との差を確認するチェックが必要です。入力は保持しています。")
            if snapshot["revision"] != edit["reload"]["snapshot"]["revision"]:
                raise ProposalError("比較後に提案箱が変わりました。入力を保持したまま、最新との差をもう一度確認してください。")
            entry = snapshot["data"]["proposals"].get(edit["proposal_id"])
            edit.update(revision=snapshot["revision"], version=uuid4().hex, missing=entry is None)
            if entry is not None:
                edit["entry"] = deepcopy(entry)
            edit.pop("reload", None)
            st.session_state[prefix + "_notice"] = "入力を保持して最新の状態を読み直しました。反映する場合は、新しい差分を確認・承認してください。"
        st.rerun()
    except (ValueError, OSError) as exc:
        _error(exc, action=True)


def _render_import(root: Path, prefix: str, snapshot: dict) -> None:
    version = st.session_state.get(prefix + "_import_version", 0)
    mode = st.radio("提案の入力方法", ["本文を貼り付け", "JSONを貼り付け", "JSONファイル"],
                    key=f"{prefix}_import_mode_{version}", horizontal=True)
    mode_key = prefix + "_last_import_mode"
    if st.session_state.get(mode_key, mode) != mode:
        st.session_state.pop(prefix + "_import_preview", None)
    st.session_state[mode_key] = mode
    with st.form(f"{prefix}_import_form_{version}"):
        title = source_label = report_id = body = ""
        upload = None
        if mode == "本文を貼り付け":
            title = st.text_input("提案タイトル（任意）", max_chars=200, key=f"{prefix}_import_title_{version}")
            source_label = st.text_input("相談元のメモ（任意）", max_chars=MAX_SOURCE_LABEL_LENGTH, key=f"{prefix}_import_source_{version}")
            report_id = st.text_input("元レポートID（任意）", max_chars=64, key=f"{prefix}_import_report_{version}")
            body = st.text_area("GPTの提案本文", height=220, max_chars=MAX_TEXT_LENGTH, key=f"{prefix}_import_text_{version}")
        elif mode == "JSONを貼り付け":
            body = st.text_area("提案JSON", height=260, key=f"{prefix}_import_json_{version}")
        else:
            upload = st.file_uploader("提案JSONファイル（UTF-8・200000バイト以内）", type=["json"], key=f"{prefix}_import_upload_{version}")
        requested = st.form_submit_button("取り込みプレビューを作成", type="primary")
    if requested:
        st.session_state.pop(prefix + "_import_preview", None)
        try:
            if mode == "本文を貼り付け":
                payload = json.dumps({"schema_version": 1, "source_report_id": report_id, "source_label": source_label,
                                      "proposals": [{"title": title.strip() or "相談からの提案", "text": body,
                                                     "target": {"kind": "reference"}}]}, ensure_ascii=False)
            elif mode == "JSONを貼り付け":
                payload = body
            else:
                if upload is None:
                    raise ProposalError("JSONファイルを選択してください。")
                if upload.size > MAX_IMPORT_BYTES:
                    raise ProposalError("JSONファイルは200000バイト以内にしてください。")
                payload = upload.getvalue()
                if len(payload) > MAX_IMPORT_BYTES:
                    raise ProposalError("JSONファイルは200000バイト以内にしてください。")
            candidates = parse_proposal_payload(payload)
            preview = {"candidates": candidates, "revision": snapshot["revision"]}
            preview["preview_hash"] = _digest(preview)
            st.session_state[prefix + "_import_preview"] = preview
        except (ValueError, OSError) as exc:
            _error(exc, action=True)
    preview = st.session_state.get(prefix + "_import_preview")
    if preview:
        st.caption("まだ提案箱へ保存していません。教材・日次メモにも反映していません。")
        for index, candidate in enumerate(preview["candidates"], 1):
            with st.expander(f"候補 {index}", expanded=True):
                _show_candidate(candidate)
        if st.button("この候補を提案箱へ保存", key=f"{prefix}_import_save_{preview['preview_hash']}"):
            try:
                result = import_proposals(root, preview["candidates"], expected_revision=preview["revision"])
                st.session_state[prefix + "_notice"] = f"提案箱へ保存しました。追加 {result['added']} 件・重複 {result['duplicates']} 件。まだ採用・反映はしていません。"
                st.session_state.pop(prefix + "_import_preview", None)
                st.session_state[prefix + "_import_version"] = version + 1
                st.rerun()
            except (ValueError, OSError) as exc:
                _error(exc, action=True)


def _render_editor(root: Path, prefix: str, edit: dict, *, blocked: bool = False) -> None:
    entry = edit["entry"]
    identifier, version = edit["proposal_id"], edit["version"]
    suffix = f"{identifier}_{version}"
    st.subheader("提案の確認・採用")
    st.caption("状態: " + STATUSES.get(entry["status"], entry["status"]))
    _show_candidate(entry)
    if edit.get("missing") or entry["status"] in {"applied", "reverted"}:
        if edit.get("missing"):
            st.warning("この提案は削除済みです。保持した入力をコピーできますが、採用・状態変更はできません。")
        elif entry["status"] == "reverted":
            st.info("取消済みの履歴です。再採用する場合は、新しい内容の提案として取り込んでください。")
        elif st.button("適用取消のプレビューを作成", key=f"{prefix}_prepare_revert_{suffix}", disabled=blocked):
            try:
                edit["preview"] = prepare_revert(root, identifier, expected_revision=edit["revision"])
            except (ValueError, OSError) as exc:
                edit.pop("preview", None)
                _error(exc, action=True)
        if edit.get("has_draft"):
            st.text_area("保持している未保存のタイトル", value=edit["title"], disabled=True, key=f"{prefix}_rescued_title_{suffix}")
            st.text_area("保持している未保存の本文", value=edit["text"], disabled=True, key=f"{prefix}_rescued_text_{suffix}")
            st.text(json.dumps({"kind": edit.get("kind"), "material_id": edit.get("material_id"), "target_date": str(edit.get("target_date") or "")}, ensure_ascii=False))
        with st.form(f"{prefix}_closed_form_{suffix}"):
            discard_confirmed = st.checkbox("保持している未保存入力を破棄して一覧へ戻る", key=f"{prefix}_closed_discard_{suffix}") if edit.get("has_draft") else True
            back = st.form_submit_button("提案一覧へ戻る")
            compare = st.form_submit_button("最新の保存内容との差を確認", disabled=blocked)
            reload_approved, reload_requested = _reload_controls(edit, prefix)
        if compare or reload_requested:
            _reload_editor(root, prefix, edit, compare=compare, approved=reload_approved)
        elif back:
            if discard_confirmed:
                _clear_editor(prefix)
                st.rerun()
            else:
                ab.show_action_error("保持した入力を破棄する確認が必要です。入力は残しています。",
                                     toast_message="入力を破棄する確認が必要です。入力は残しています。")
    else:
        try:
            materials = list_materials(root)
            edit["materials"] = materials
        except (ValueError, OSError) as exc:
            materials = edit.get("materials", [])
            blocked = True
            _error(exc)
        names = {item["material_id"]: item["label"] for item in materials}
        selected_material = st.session_state.get(f"{prefix}_material_{suffix}", edit.get("material_id", ""))
        if selected_material and selected_material not in names:
            names[selected_material] = f"削除済み教材 [{selected_material}]（反映できません）"
        st.caption("原文は保持します。編集した本文は採用時のみ保存されます。保留・見送り・未確認へ戻す操作では編集内容を保存しません。")
        with st.form(f"{prefix}_edit_form_{suffix}"):
            title = st.text_input("採用するタイトル", value=edit["title"], max_chars=200, key=f"{prefix}_title_{suffix}")
            text = st.text_area("採用する本文", value=edit["text"], height=220, max_chars=MAX_TEXT_LENGTH, key=f"{prefix}_text_{suffix}")
            kind = st.selectbox("反映先の種類", list(KINDS), index=list(KINDS).index(edit.get("kind", "reference")), format_func=KINDS.get, key=f"{prefix}_kind_{suffix}")
            material_id = st.selectbox("反映する教材（必要時に選択）", [""] + list(names),
                                       index=([""] + list(names)).index(selected_material),
                                       format_func=lambda value: names.get(value, "教材を選択してください"), key=f"{prefix}_material_{suffix}")
            target_date = st.date_input("反映する日（必要時に選択）", value=edit.get("target_date"), key=f"{prefix}_date_{suffix}")
            discard_confirmed = st.checkbox("採用前の本文編集を破棄して一覧へ戻る", key=f"{prefix}_discard_confirm_{suffix}")
            columns = st.columns(3)
            prepare = columns[0].form_submit_button("採用前の差分を作成", type="primary", disabled=blocked or entry["status"] == "rejected")
            defer = columns[1].form_submit_button("保留にする", disabled=blocked or entry["status"] == "rejected")
            reject = columns[2].form_submit_button("見送る", disabled=blocked)
            columns = st.columns(2)
            pending = columns[0].form_submit_button("未確認へ戻す", disabled=blocked)
            discard = columns[1].form_submit_button("編集を破棄して一覧へ戻る")
            compare = st.form_submit_button("最新の保存内容との差を確認", disabled=blocked)
            reload_approved, reload_requested = _reload_controls(edit, prefix)
        if prepare or defer or reject or pending or discard or compare or reload_requested:
            edit.update(title=title, text=text, kind=kind, material_id=material_id, target_date=target_date, has_draft=True)
            if compare or reload_requested:
                _reload_editor(root, prefix, edit, compare=compare, approved=reload_approved)
            elif discard:
                if not discard_confirmed:
                    ab.show_action_error("本文編集を破棄する確認にチェックしてください。入力は残しています。",
                                         toast_message="本文編集を破棄する確認が必要です。入力は残しています。")
                else:
                    _clear_editor(prefix)
                    st.rerun()
            elif defer or reject or pending:
                status = "deferred" if defer else "rejected" if reject else "pending"
                try:
                    set_proposal_status(root, identifier, status, expected_revision=edit["revision"])
                    _clear_editor(prefix, "提案を「" + STATUSES[status] + "」にしました。実績・教材・日次メモは変更していません。")
                    st.rerun()
                except (ValueError, OSError) as exc:
                    _error(exc, action=True)
            else:
                edit.pop("preview", None)
                try:
                    if kind == "reference":
                        raise ProposalError("反映先の種類を選択してください。参照のみの場合は提案箱に残せます。")
                    if kind == "next_step" and not material_id:
                        raise ProposalError("反映する教材を選択してください。提案内の教材IDは自動選択しません。")
                    if kind == "daily_memo" and target_date is None:
                        raise ProposalError("反映する日を選択してください。提案内の日付は自動選択しません。")
                    edit["preview"] = prepare_application(root, identifier, kind=kind, title=title, text=text,
                                                          material_id=material_id if kind == "next_step" else "",
                                                          target_date=target_date if kind == "daily_memo" else None,
                                                          expected_revision=edit["revision"])
                except (ValueError, OSError) as exc:
                    _error(exc, action=True)
    preview = edit.get("preview")
    if preview:
        st.subheader("確定する変更差分")
        _show_change(preview)
        is_revert = preview["action"] == "revert"
        approved = st.checkbox("この取消差分を確認しました" if is_revert else "この採用差分と反映先を確認しました",
                               key=f"{prefix}_approve_{identifier}_{preview['preview_hash']}")
        if st.button("適用を取り消す" if is_revert else "承認して採用・反映",
                     key=f"{prefix}_commit_{identifier}_{preview['preview_hash']}", type="primary", disabled=not approved or blocked):
            try:
                if is_revert:
                    revert_application(root, preview)
                else:
                    approve_application(root, preview)
                _clear_editor(prefix, "提案の適用を取り消しました。" if is_revert else "提案を採用して反映しました。タスクは作成していません。")
                st.rerun()
            except (ValueError, OSError) as exc:
                _error(exc, action=True)


def render_proposal_inbox(root: Path, key_prefix: str = "proposal_inbox") -> None:
    st.subheader("相談の提案箱")
    st.caption("GPTとの相談結果を手動で取り込みます。提案は事実ではありません。保存だけでは教材・日次メモ・タスクを変更しません。")
    st.caption("出典ID・相談元は自己申告の情報です。実際に参照した文書・日時を確認してください。")
    notice = st.session_state.pop(key_prefix + "_notice", None)
    if notice:
        ab.show_action_message(notice)
    edit = st.session_state.get(key_prefix + "_edit")
    try:
        snapshot = load_inbox_snapshot(root)
    except (ValueError, OSError) as exc:
        _error(exc)
        if edit is not None:
            _render_editor(root, key_prefix, edit, blocked=True)
        return
    if edit is not None:
        if snapshot["revision"] != edit["revision"]:
            st.warning("別の操作で提案箱が更新されています。『最新の保存内容との差を確認』から、入力を保持して読み直せます。")
        _render_editor(root, key_prefix, edit)
        return
    with st.expander("GPTへ渡す提案JSONの依頼文"):
        st.code(proposal_request_template(), language=None, wrap_lines=True)
    with st.expander("新しい提案を取り込む", expanded=True):
        _render_import(root, key_prefix, snapshot)
    proposals = snapshot["data"]["proposals"]
    ordered = sorted(proposals, key=lambda identifier: parse_analysis_datetime(proposals[identifier]["updated_at"]), reverse=True)
    if not ordered:
        st.caption("提案箱は空です。取り込みプレビューを確認してから保存してください。")
        return
    with st.form(key_prefix + "_selection_form"):
        selected = st.selectbox("確認する提案", [""] + ordered,
                                format_func=lambda identifier: (f"{STATUSES[proposals[identifier]['status']]} / {proposals[identifier]['proposal']['title']} [{identifier[:20]}]") if identifier else "提案を選択してください",
                                key=key_prefix + "_selection")
        opened = st.form_submit_button("この提案を開く")
    if opened and selected:
        entry = deepcopy(proposals[selected])
        st.session_state[key_prefix + "_edit"] = {"proposal_id": selected, "entry": entry, "revision": snapshot["revision"],
                                                  "version": uuid4().hex, "title": entry["proposal"]["title"], "text": entry["proposal"]["text"]}
        st.rerun()
    for index, identifier in enumerate(ordered, 1):
        entry = proposals[identifier]
        with st.expander(f"提案 {index} / {STATUSES[entry['status']]}"):
            _show_candidate(entry)


def render_daily_consultation_memos(root: Path, target_date: date) -> None:
    """Display adopted daily memos without normalizing/saving the daily plan."""
    if type(target_date) is not date:
        st.error("日次相談メモの日付が不正です。")
        return
    try:
        root = resolved_json_path(root)
        path = root / f"daily_plans/{target_date:%Y}/{target_date:%m}/{target_date:%Y%m%d}.json"
        if not resolved_json_path(path).is_relative_to(root):
            raise ProposalError("日次相談メモの保存先がプロジェクト外です。")
        with operation_guard(root):
            plan = load_json_safe(path, {})
        if not isinstance(plan, dict) or not isinstance(plan.get("consultation_memos", []), list):
            raise ProposalError("日次相談メモの形式が不正です。")
        memos = plan.get("consultation_memos", [])
        if any(not isinstance(item, dict) or any(not isinstance(item.get(key), str) for key in ("proposal_id", "title", "text", "adopted_at")) for item in memos):
            raise ProposalError("日次相談メモの記録形式が不正です。")
        for memo in memos:
            _timestamp(memo["adopted_at"])
        if not memos:
            st.caption("この日の採用済み相談メモはありません。")
            return
        for memo in reversed(memos):
            st.text(memo["title"])
            st.text(memo["text"])
            st.caption("採用日時")
            st.text(memo["adopted_at"])
    except (ValueError, OSError) as exc:
        _error(exc)
