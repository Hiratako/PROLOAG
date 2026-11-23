from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd
import streamlit as st

import prolog_ui.components as ab
from app_core.analysis_dates import parse_analysis_date
from app_core.app_clock import app_today
from app_core.master_snapshot import MasterSnapshot
from app_core.rough_study_drafts import discard_rough_draft, load_rough_draft, locked_rough_draft, save_rough_draft
from app_core.rough_study_input import (
    merge_period_table_rows,
    parse_study_minutes,
    study_edit_diff,
    summarize_study_selection,
)
from app_core.rough_study_service import (
    MAX_PERIOD_DAYS,
    delete_rough_study_batch,
    get_existing_study_days,
    load_rough_study_batches,
    load_rough_study_records,
    save_rough_study_batch,
    validate_rough_study_rows,
)
from app_core.rough_study_week_widgets import render_week_rows
from app_core.study_log_service import load_all_study_sessions


_WEEK = "rough_study_shown_week"
_WEEK_PICKER = "rough_study_week_picker"
_NEXT_WEEK = "rough_study_next_week"
_PERIOD = "rough_study_period_state"
_REQUEST = "rough_study_editor_request"
_OPEN_BATCH = "rough_study_open_batch_id"
_RECENT = "rough_study_recent_save"
_CACHE = "rough_study_draft_cache"
_EDITORS = "rough_study_live_editors"
_DAY_LABELS = "月火水木金土日"
_CLASSIFICATION = ("project_id", "material_id", "unit_id")


@dataclass
class WeekDraftConflict:
    saved: dict | None


@dataclass
class EntryDraft:
    kind: str
    start: date
    end: date
    rows: list[dict]
    existing: dict[date, dict]
    period: dict | None = None
    defaults: list[dict] = field(default_factory=list)
    batch: dict | None = None
    approved: list[dict] | None = None
    operation_id: str = field(default_factory=lambda: uuid4().hex)
    editor_id: str = field(default_factory=lambda: uuid4().hex)
    grid_rows: list[dict] | None = None
    notice: str = ""
    week_conflict: WeekDraftConflict | None = None

    def payload(self) -> dict:
        return {
            "kind": self.kind, "start": self.start.isoformat(), "end": self.end.isoformat(),
            "rows": self.rows, "period": self.period, "defaults": self.defaults,
            "batch": self.batch, "approved": self.approved, "operation_id": self.operation_id,
            "existing": {day.isoformat(): values for day, values in self.existing.items()},
        }

    @classmethod
    def restore(cls, saved: dict) -> EntryDraft:
        return cls(
            saved["kind"], date.fromisoformat(saved["start"]), date.fromisoformat(saved["end"]),
            saved["rows"], {date.fromisoformat(day): info for day, info in saved["existing"].items()},
            period=saved["period"], defaults=saved["defaults"], batch=saved["batch"],
            approved=saved["approved"], operation_id=saved["operation_id"],
        )

    def refresh_grid(self) -> None:
        self.editor_id = uuid4().hex
        self.grid_rows = None


def request_rough_study_editor(batch_id: str, record_id: str | None = None) -> None:
    st.session_state[_REQUEST] = {"batch_id": batch_id, "record_id": record_id,
                                  "location": "history" if record_id else "entry"}


def rough_study_editor_requested() -> bool:
    return st.session_state.get(_REQUEST, {}).get("location") == "history"


def _date_label(day: date) -> str:
    return f"{day:%m/%d}（{_DAY_LABELS[day.weekday()]}）"


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _duration_label(seconds: int | float) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    parts = [f"{hours}時間"] if hours else []
    if minutes or not (hours or seconds):
        parts.append(f"{minutes}分")
    if seconds:
        parts.append(f"{seconds}秒")
    return "".join(parts)


def _read_draft(root: Path, key: str) -> dict | None:
    cache = st.session_state.setdefault(_CACHE, {})
    if key not in cache:
        cache[key] = load_rough_draft(root, key)
    return deepcopy(cache[key])


def _write_draft(root: Path, key: str, payload: dict | None) -> bool:
    try:
        if _read_draft(root, key) != payload:
            if payload is None:
                discard_rough_draft(root, key)
            else:
                save_rough_draft(root, key, payload)
            st.session_state[_CACHE][key] = deepcopy(payload)
    except (OSError, ValueError) as exc:
        st.warning(f"ローカルの下書きを更新できませんでした。{exc}")
        return False
    return True


def _active_editors() -> dict[str, EntryDraft]:
    return st.session_state.setdefault(_EDITORS, {})


def _row(day: date, **fields: Any) -> dict:
    return {"input_id": uuid4().hex, "study_date": day.isoformat(), "include": True,
            "duration": "", "subject_id": "", "memo": "", "delete": False, **fields}


def _has_values(row: dict) -> bool:
    subject_id = row["subject_id"]
    if row["duration"].strip() or subject_id or row["memo"].strip():
        return True
    # A copied row needs visible input. Keep classification-only saved
    # records when their original subject has not been cleared.
    return bool(
        row.get("record_id")
        and subject_id == row.get("original_subject_id", subject_id)
        and any(row.get(name) for name in _CLASSIFICATION)
    )


def _week_rows_by_day(payload: dict | None) -> dict[str, list[dict]]:
    days: dict[str, list[dict]] = {}
    for row in payload["rows"] if payload else ():
        days.setdefault(row["study_date"], []).append(row)
    return days


def _week_row_values(row: dict) -> dict:
    fields = {key: value for key, value in row.items() if key != "input_id"}
    fields.setdefault("delete", False)
    return fields


def _week_day_values(rows: list[dict]) -> list[dict]:
    # Each newly opened tab gets its own blank rows and widget IDs.
    if len(rows) == 1 and not _has_values(rows[0]):
        return []
    return [_week_row_values(row) for row in rows]


def _merge_week_rows(original: dict | None, local: dict, saved: dict) -> tuple[list[dict], list[str]]:
    original_days = _week_rows_by_day(original)
    local_days = _week_rows_by_day(local)
    saved_days = _week_rows_by_day(saved)
    rows = []
    conflicts = []
    for day in sorted(local_days.keys() | saved_days.keys()):
        base_values = _week_day_values(original_days.get(day, []))
        local_rows, saved_rows = local_days.get(day, []), saved_days.get(day, [])
        local_values, saved_values = _week_day_values(local_rows), _week_day_values(saved_rows)
        if local_values == base_values and saved_values != base_values:
            rows.extend(saved_rows)
        else:
            rows.extend(local_rows)
            if local_values != saved_values and saved_values != base_values:
                conflicts.append(day)
    return rows, conflicts


def _week_rows_after_removal(original: dict, draft: EntryDraft) -> list[dict]:
    original_values = {row["input_id"]: _week_row_values(row) for row in original["rows"]}
    changed: dict[str, list[dict]] = {}
    for row in draft.rows:
        if _has_values(row) and _week_row_values(row) != original_values.get(row["input_id"]):
            changed.setdefault(row["study_date"], []).append(row)
    rows = []
    for day in _days(draft.start, draft.end):
        rows.extend(changed.get(day.isoformat()) or [_row(day)])
    return rows


def _store_week_draft(root: Path, key: str, draft: EntryDraft) -> tuple[bool, bool]:
    local = draft.payload()
    payload = local if draft.approved is not None or any(_has_values(row) for row in draft.rows) else None
    original = _read_draft(root, key)
    if payload == original and draft.week_conflict is None:
        return bool(payload), False
    try:
        with locked_rough_draft(root, key) as saved:
            if saved == original:
                if not _write_draft(root, key, payload):
                    return False, False
                draft.week_conflict = None
                return bool(payload), False

            # A removed draft may already have been committed in another tab.
            # Keep the current inputs in this session until the user chooses.
            rows, clashes = _merge_week_rows(original, local, saved) if saved is not None else ([], [])
            if clashes or saved is None:
                if clashes:
                    days = "、".join(_date_label(date.fromisoformat(day)) for day in clashes)
                    st.warning(f"{days}の下書きが別のタブでも変更されました。この画面の入力は残っています。")
                else:
                    st.warning("別のタブでこの週の下書きが確定または削除されました。この画面の入力は残っています。")
                actions = st.columns(2)
                use_saved = actions[0].button(
                    "保存済みの下書きを読み込む", key=f"rough_reload_{draft.editor_id}",
                    help="このタブの未保存の入力は、別のタブで保存した内容に置き換わります。",
                )
                if saved is None:
                    use_local = actions[1].button("このタブで変更した行を下書きに戻す", key=f"rough_keep_{draft.editor_id}")
                else:
                    use_local = actions[1].button(
                        "このタブの入力を反映する", key=f"rough_keep_{draft.editor_id}",
                        help="両方のタブで変更された日は、このタブで入力した内容に置き換わります。",
                    )
                if use_saved:
                    st.session_state[_CACHE][key] = deepcopy(saved)
                    _active_editors().pop(key, None)
                    return bool(saved), True
                same_conflict = draft.week_conflict is not None and draft.week_conflict.saved == saved
                draft.week_conflict = WeekDraftConflict(deepcopy(saved))
                if not (use_local and same_conflict):
                    draft.approved = None
                    return False, False
                if saved is None:
                    assert original is not None
                    rows = _week_rows_after_removal(original, draft)

            rebased = {**local, "rows": rows, "approved": None, "operation_id": uuid4().hex}
            new_draft = EntryDraft.restore(rebased)
            new_draft.existing = _load_existing(root, new_draft)
            new_draft.notice = "別のタブの変更を確認しました。保存前にこの週の内容を確認してください。"
            payload = new_draft.payload() if any(_has_values(row) for row in rows) else None
            if payload is not None:
                save_rough_draft(root, key, payload)
            elif saved is not None:
                discard_rough_draft(root, key)
            st.session_state[_CACHE][key] = deepcopy(payload)
            _active_editors()[key] = new_draft
    except (OSError, ValueError) as exc:
        st.warning(f"ローカルの下書きを更新できませんでした。{exc}")
        return False, False
    return bool(payload), True


def _existing_label(info: dict | None) -> str:
    if not info:
        return ""
    time = info["timed_seconds"] + info["rough_seconds"]
    return _duration_label(time) if time else "内容の記録あり・時間未記録"


def _load_existing(root: Path, draft: EntryDraft) -> dict[date, dict]:
    return get_existing_study_days(
        root, draft.start, draft.end, exclude_batch_id=draft.batch["batch_id"] if draft.batch else None,
    )


def _subject_choices(master_data: dict, recent: list[str]) -> tuple[dict[str, str], dict[str, dict]]:
    master = MasterSnapshot(master_data)
    subjects = {item["subject_id"]: item for item in master.active("subjects") if item.get("subject_id")}
    names = {ident: str(item.get("name") or "名称未設定") for ident, item in subjects.items()}
    counts: dict[str, int] = {}
    for name in names.values():
        counts[name] = counts.get(name, 0) + 1
    labels = {}
    used: set[str] = set()
    for ident in dict.fromkeys([*(ident for ident in recent if ident in subjects), *subjects]):
        name = names[ident]
        if counts[name] > 1:
            project = master.name("project_id", subjects[ident].get("project_id", ""))
            name = f"{name}（{project}）"
        label = name
        suffix = 2
        while label in used:
            label = f"{name} {suffix}"
            suffix += 1
        labels[ident] = label
        used.add(label)
    return labels, subjects


def _records(draft: EntryDraft, subjects: dict[str, dict]) -> tuple[list[dict], list[str]]:
    originals = {row["record_id"]: row for row in draft.batch["rows"]} if draft.batch else {}
    result = []
    errors = []
    for raw in draft.rows:
        if (draft.batch and raw.get("delete")) or (not draft.batch and not raw.get("include", True)):
            continue
        day = date.fromisoformat(raw["study_date"])
        original = originals.get(raw.get("record_id"), {})
        if not _has_values(raw):
            if original:
                errors.append(f"{_date_label(day)} の保存済み記録に内容か時間を入力するか、削除を選んでください。")
            continue
        try:
            seconds = original.get("duration_sec")
            if seconds is None or raw["duration"] != _duration_label(seconds):
                minutes = parse_study_minutes(raw["duration"])
                seconds = None if minutes is None else minutes * 60
        except ValueError as exc:
            errors.append(f"{_date_label(day)} · {exc}")
            continue
        ident = raw["subject_id"]
        classification = {name: raw.get(name, "") for name in _CLASSIFICATION}
        if ident != raw.get("original_subject_id", ident):
            classification = {"project_id": subjects.get(ident, {}).get("project_id", ""),
                              "material_id": "", "unit_id": ""}
        elif not original and not classification["project_id"]:
            classification["project_id"] = subjects.get(ident, {}).get("project_id", "")
        entry = {
            "study_date": day.isoformat(), "duration_sec": seconds,
            "estimated": seconds is not None and original.get("estimated", draft.kind == "period"),
            "subject_id": ident, "memo": raw["memo"].strip(), **classification,
        }
        if original:
            entry["record_id"] = original["record_id"]
        result.append(entry)
    return result, errors


def _summary(draft: EntryDraft, records: list[dict]) -> None:
    totals = summarize_study_selection(records, draft.existing, scope_days=_days(draft.start, draft.end))
    old = sum(row["duration_sec"] or 0 for row in draft.batch["rows"]) if draft.batch else 0
    approximate = "（概算を含む）" if any(row["estimated"] for row in records) else ""
    if draft.batch:
        line = (f"登録済み {_duration_label(totals['existing_seconds'] + old)}"
                f"　→　保存後 {_duration_label(totals['result_seconds'])}{approximate}")
    else:
        line = (f"登録済み {_duration_label(totals['existing_seconds'])}"
                f"　＋　今回 {_duration_label(totals['entered_seconds'])}{approximate}"
                f"　＝　保存後 {_duration_label(totals['result_seconds'])}")
    st.markdown(f"**{line}**")
    st.caption(f"{totals['selected_days']}日・{totals['record_count']}件　｜　時間未記録 {totals['unknown_count']}件")


def _period_counts(draft: EntryDraft, records: list[dict]) -> None:
    included = {row["study_date"] for row in records}
    existing_skips = sum(not row["include"] and date.fromisoformat(row["study_date"]) in draft.existing for row in draft.rows)
    manual_skips = sum(not row["include"] for row in draft.rows) - existing_skips
    incomplete = sum(row["include"] and row["study_date"] not in included for row in draft.rows)
    calendar_days = (draft.end - draft.start).days + 1
    parts = [f"{calendar_days}日中{len(included)}日を追加"]
    if calendar_days > len(draft.rows):
        parts.append(f"週末を除外 {calendar_days - len(draft.rows)}日")
    parts += [f"登録済みで除外 {existing_skips}日", f"手動で除外 {manual_skips}日"]
    if incomplete:
        parts.append(f"未入力・要確認 {incomplete}日")
    st.caption("　｜　".join(parts))


def _table_rows(draft: EntryDraft, labels: dict[str, str]) -> list[dict]:
    """Render the period grid or saved-batch editor with fixed rows."""
    if draft.grid_rows is None:
        draft.grid_rows = deepcopy(draft.rows)
    options = dict(labels)
    for row in draft.grid_rows:
        ident = row["subject_id"]
        if ident and ident not in options:
            name = f"登録済み科目（{len(options) + 1}）"
            while name in options.values():
                name += "・"
            options[ident] = name
    by_label = {value: ident for ident, value in options.items()}
    frame = pd.DataFrame([
        {
            "include": row["include"], "delete": row["delete"],
            "study_date": date.fromisoformat(row["study_date"]),
            "weekday": _DAY_LABELS[date.fromisoformat(row["study_date"]).weekday()],
            "duration": row["duration"], "subject": options.get(row["subject_id"], ""),
            "memo": row["memo"], "existing": _existing_label(draft.existing.get(date.fromisoformat(row["study_date"]))),
        }
        for row in draft.grid_rows
    ])
    editing = draft.batch is not None
    columns = ["study_date", "weekday", "duration", "subject", "memo", "existing", "delete" if editing else "include"]
    shown = st.data_editor(
        frame, key=f"rough_table_{draft.editor_id}", width="stretch", hide_index=True, num_rows="fixed",
        column_order=columns,
        disabled=["weekday", "existing", *([] if editing else ["study_date"])],
        column_config={
            "study_date": st.column_config.DateColumn("日付", format="MM/DD", pinned=True, min_value=draft.start, max_value=draft.end),
            "weekday": st.column_config.TextColumn("曜", width="small"),
            "duration": st.column_config.TextColumn("学習時間", help="1:30、90分、1時間30分。数字だけなら分。空欄は未記録。"),
            "subject": st.column_config.SelectboxColumn("科目", options=["", *options.values()]),
            "memo": st.column_config.TextColumn("勉強した内容", width="medium"),
            "existing": st.column_config.TextColumn("同日の既存記録"),
            "include": st.column_config.CheckboxColumn("追加対象"),
            "delete": st.column_config.CheckboxColumn("この行を削除"),
        },
    )
    result = []
    for raw, edited in zip(draft.grid_rows, shown.to_dict("records"), strict=True):
        edited_day = edited["study_date"]
        day = raw["study_date"] if pd.isna(edited_day) else str(edited_day)[:10]
        subject = edited["subject"]
        result.append({
            **raw, "study_date": day,
            "include": bool(edited["include"]) if not editing else True,
            "delete": bool(edited["delete"]) if editing else False,
            "duration": "" if pd.isna(edited["duration"]) else str(edited["duration"]),
            "subject_id": "" if pd.isna(subject) else by_label.get(subject, ""),
            "memo": "" if pd.isna(edited["memo"]) else str(edited["memo"]),
        })
    return result


def _period_selection(root: Path, draft: EntryDraft) -> bool:
    columns = st.columns(2)
    reset = columns[0].button("未登録日を選び直す", key=f"rough_select_empty_{draft.editor_id}")
    clear = columns[1].button("全解除", key=f"rough_clear_selection_{draft.editor_id}")
    if not (reset or clear):
        return False
    if reset:
        draft.existing = _load_existing(root, draft)
    draft.rows = [
        {**row, "include": reset and date.fromisoformat(row["study_date"]) not in draft.existing}
        for row in draft.rows
    ]
    draft.refresh_grid()
    return True


def _preview(draft: EntryDraft, records: list[dict], errors: list[str]) -> bool:
    if errors:
        for message in dict.fromkeys(errors):
            st.error(message)
        return False
    try:
        approved = validate_rough_study_rows(records, kind=draft.kind)
    except ValueError as exc:
        if draft.batch and not records:
            st.error("全件を削除する場合は「このまとまりを削除」を使ってください。")
        else:
            st.error(str(exc))
        return False
    if draft.batch:
        changes = study_edit_diff(draft.batch["rows"], approved)
        if not any(changes.values()):
            st.info("保存済みの記録から変更された内容がありません。")
            return False
    draft.approved = approved
    return True


def _display_row(row: dict, labels: dict[str, str]) -> dict:
    duration = row["duration_sec"]
    time = "時間未記録" if duration is None else _duration_label(duration)
    if row["estimated"] and duration is not None:
        time = f"約{time}"
    return {"日付": _date_label(date.fromisoformat(row["study_date"])), "学習時間": time,
            "科目": labels.get(row["subject_id"], row["subject_id"] and "登録済み科目"),
            "勉強した内容": row["memo"]}


def _diff_preview(draft: EntryDraft, labels: dict[str, str]) -> None:
    changes = study_edit_diff(draft.batch["rows"], draft.approved)
    st.markdown(f"**追加 {len(changes['added'])}件・変更 {len(changes['changed'])}件・削除 {len(changes['deleted'])}件**")
    expanded = [{"操作": "追加", **_display_row(row, labels)} for row in changes["added"]]
    for change in changes["changed"]:
        expanded.append({"操作": "変更前", **_display_row(change["before"], labels)})
        expanded.append({"操作": "変更後", **_display_row(change["after"], labels)})
    expanded.extend({"操作": "削除", **_display_row(row, labels)} for row in changes["deleted"])
    if changes["deleted"]:
        st.warning("「削除」と表示された記録は、変更を保存すると勉強ログから消えます。")
    st.dataframe(expanded, width="stretch", hide_index=True)


def _save(root: Path, key: str, draft: EntryDraft) -> bool:
    records = draft.approved
    try:
        current = _load_existing(root, draft)
        touched = {date.fromisoformat(row["study_date"]) for row in records}
        if draft.batch:
            touched.update(date.fromisoformat(row["study_date"]) for row in draft.batch["rows"])
        if any(current.get(day) != draft.existing.get(day) for day in touched):
            draft.existing = current
            draft.approved = None
            draft.refresh_grid()
            draft.notice = "確認後に登録済みの記録が変わりました。対象日と時間を確認してください。"
            return False
        saved = save_rough_study_batch(
            root, kind=draft.kind, rows=records, operation_id=draft.operation_id, period=draft.period,
            batch_id=draft.batch["batch_id"] if draft.batch else None,
            expected_revision=draft.batch["revision"] if draft.batch else None,
        )
    except (OSError, ValueError) as exc:
        ab.show_action_error(f"保存できませんでした。{exc}")
        return False
    _write_draft(root, key, None)
    _active_editors().pop(key, None)
    st.session_state[_RECENT] = {"batch_id": saved["batch_id"], "count": len(records)}
    st.session_state.pop("rough_recent_subjects", None)
    ab.queue_success(f"ざっくり記録を{len(records)}件保存しました。")
    return True


def _save_confirmed(root: Path, key: str, draft: EntryDraft) -> bool:
    if draft.kind != "week" or draft.batch is not None:
        return _save(root, key, draft)
    try:
        with locked_rough_draft(root, key) as saved:
            if saved == _read_draft(root, key):
                return _save(root, key, draft)
    except (OSError, ValueError) as exc:
        ab.show_action_error(f"保存できませんでした。{exc}")
        return False
    draft.approved = None
    draft.operation_id = uuid4().hex
    draft.refresh_grid()
    draft.notice = "確認後に別のタブで下書きが変わりました。保存前に内容を確認し直してください。"
    return False


def _confirm(root: Path, key: str, draft: EntryDraft, labels: dict[str, str]) -> str:
    _summary(draft, draft.approved)
    if draft.batch:
        _diff_preview(draft, labels)
    else:
        if any(date.fromisoformat(row["study_date"]) in draft.existing for row in draft.approved):
            st.warning("登録済みの記録がある日を含みます。今回の入力は別の記録として追加されます。")
        st.dataframe([_display_row(row, labels) for row in draft.approved], width="stretch", hide_index=True)
    actions = st.columns([1, 1, 3])
    if actions[0].button("入力に戻る", key=f"rough_back_{draft.editor_id}"):
        draft.approved = None
        draft.operation_id = uuid4().hex
        draft.refresh_grid()
        return "rerun"
    if actions[1].button("変更を保存" if draft.batch else "保存する", key=f"rough_commit_{draft.editor_id}", type="primary"):
        if _save_confirmed(root, key, draft):
            return "saved"
        if draft.approved is None:
            return "rerun"
    return "persist"


def _copy_previous(root: Path, draft: EntryDraft) -> bool:
    previous_start = draft.start - timedelta(days=7)
    signatures = {(row["study_date"], row["subject_id"], row["memo"]) for row in draft.rows if _has_values(row)}
    changed = False
    for item in [*load_all_study_sessions(root), *load_rough_study_records(root)]:
        day = parse_analysis_date(item.get("study_date") or item.get("start_at"))
        if day is None or not previous_start <= day < draft.start:
            continue
        current_day = day + timedelta(days=7)
        ident, memo = str(item.get("subject_id") or ""), str(item.get("memo") or "").strip()
        if not ident and not memo:
            continue
        signature = (current_day.isoformat(), ident, memo)
        if signature in signatures:
            continue
        copied = _row(current_day, subject_id=ident, original_subject_id=ident, memo=memo,
                      **{name: str(item.get(name) or "") for name in _CLASSIFICATION})
        for index, row in enumerate(draft.rows):
            if row["study_date"] == copied["study_date"] and not _has_values(row):
                draft.rows[index] = copied
                break
        else:
            draft.rows.append(copied)
        signatures.add(signature)
        changed = True
    return changed


def _render_week(root: Path, labels: dict[str, str], subjects: dict[str, dict]) -> None:
    today = _monday(app_today())
    start = st.session_state.setdefault(_WEEK, today)
    if _NEXT_WEEK in st.session_state:
        st.session_state[_WEEK_PICKER] = st.session_state.pop(_NEXT_WEEK)
    if _WEEK_PICKER not in st.session_state:
        st.session_state[_WEEK_PICKER] = start
    key = f"week:{start.isoformat()}"
    end = start + timedelta(days=6)
    editors = _active_editors()
    if key not in editors:
        saved = _read_draft(root, key)
        editors[key] = EntryDraft.restore(saved) if saved else EntryDraft(
            "week", start, end, [_row(day) for day in _days(start, end)], get_existing_study_days(root, start, end),
        )
    draft = editors[key]
    navigation = st.columns([1, 1, 1, 2])
    previous = navigation[0].button("前の週", key="rough_week_previous")
    now = navigation[1].button("今週", key="rough_week_today", disabled=start == today)
    following = navigation[2].button("次の週", key="rough_week_next")
    picked = navigation[3].date_input("週に含まれる日", key=_WEEK_PICKER, label_visibility="collapsed")
    st.markdown(f"**{start:%Y年}{_date_label(start)} 〜 {_date_label(end)}**")
    draft_status = st.empty()
    if draft.notice:
        st.warning(draft.notice)
    if draft.approved is not None:
        status = _confirm(root, key, draft, labels)
    else:
        header = st.empty()
        st.caption("学習時間は例 1:30・90分。時間が分からない日は内容だけ入力できます。")
        copy_week = st.button("前週の科目・内容を使う", key="rough_week_copy", help="時間欄は空欄で下書きに追加します。")
        prior_row_ids = [row['input_id'] for row in draft.rows]
        draft.rows = render_week_rows(
            draft.rows, labels, {day.isoformat(): _existing_label(info) for day, info in draft.existing.items()},
            key=draft.editor_id,
        )
        records, errors = _records(draft, subjects)
        with header.container():
            _summary(draft, records)
        row_structure_changed = prior_row_ids != [row['input_id'] for row in draft.rows]
        status = "rerun" if row_structure_changed else "persist"
        if copy_week:
            if _copy_previous(root, draft):
                draft.refresh_grid()
                status = "rerun"
            else:
                st.caption("前週から新たに追加できる科目・内容はありません。")
        elif st.button("保存内容を確認", type="primary", key=f"rough_preview_{draft.editor_id}"):
            if _preview(draft, records, errors):
                status = "rerun"
        elif errors:
            st.caption(errors[0])
    destination = _monday(picked) if picked else start
    if previous:
        destination = start - timedelta(days=7)
    elif following:
        destination = start + timedelta(days=7)
    elif now:
        destination = today
    if status == "saved":
        st.rerun()
    with draft_status.container():
        stored, reload_widgets = _store_week_draft(root, key, draft)
        if stored:
            st.caption("この週の下書きはローカルに保存済みです。")
    if destination != start:
        st.session_state[_WEEK] = destination
        st.session_state[_NEXT_WEEK] = destination
        st.rerun()
    if status == "rerun" or reload_widgets:
        st.rerun()


def _period_defaults(today: date) -> dict:
    return {"first": (today - timedelta(days=6)).isoformat(), "last": today.isoformat(),
            "duration": "1時間", "weekdays_only": False, "subject_id": "", "memo": "", "settings_id": uuid4().hex}


def _period_parameters(settings: dict, labels: dict[str, str]) -> tuple[dict, bool]:
    settings_id = settings["settings_id"]
    first = date.fromisoformat(settings["first"])
    last = date.fromisoformat(settings["last"]) if settings["last"] else None
    selected = st.date_input("対象期間", value=(first, last) if last else (first,), key=f"rough_period_dates_{settings_id}")
    columns = st.columns([1, 2])
    duration = columns[0].text_input("1日平均の学習時間", value=settings["duration"],
                                     key=f"rough_average_{settings_id}", placeholder="例 1時間30分")
    weekdays = columns[1].checkbox("平日だけを対象にする", value=settings["weekdays_only"],
                                   key=f"rough_weekdays_{settings_id}")
    options = dict(labels)
    if settings["subject_id"] and settings["subject_id"] not in options:
        options[settings["subject_id"]] = "登録済み科目"
    choices = ["", *options]
    subject = st.selectbox("全日に入れる科目", choices, index=choices.index(settings["subject_id"]),
                           format_func=lambda ident: options.get(ident, "未設定"),
                           key=f"rough_period_subject_{settings_id}")
    memo = st.text_input("全日に入れる勉強内容（任意）", value=settings["memo"], key=f"rough_period_memo_{settings_id}")
    if selected:
        first = selected[0]
    last = selected[1] if selected and len(selected) == 2 else None
    return {
        "first": first.isoformat(), "last": last.isoformat() if last else "", "duration": duration,
        "weekdays_only": weekdays, "subject_id": subject, "memo": memo, "settings_id": settings_id,
    }, st.button("日別の入力を作る", type="primary", key=f"rough_period_generate_{settings_id}")


def _generate_period(root: Path, settings: dict, previous: EntryDraft | None) -> EntryDraft:
    if not settings["last"]:
        raise ValueError("期間の開始日と終了日を選んでください。")
    start, end = date.fromisoformat(settings["first"]), date.fromisoformat(settings["last"])
    if not 1 <= (end - start).days + 1 <= MAX_PERIOD_DAYS:
        raise ValueError(f"期間は1日以上、{MAX_PERIOD_DAYS}日以内にしてください。")
    average = parse_study_minutes(settings["duration"])
    if average is None:
        raise ValueError("1日平均の学習時間を入力してください。")
    days = [day for day in _days(start, end) if not settings["weekdays_only"] or day.weekday() < 5]
    if not days:
        raise ValueError("指定期間に平日がありません。期間を変更してください。")
    existing = get_existing_study_days(root, start, end)
    generated = [
        _row(day, include=day not in existing, duration=_duration_label(average * 60),
             subject_id=settings["subject_id"], memo=settings["memo"])
        for day in days
    ]
    rows = merge_period_table_rows(generated, previous.rows, previous.defaults) if previous else deepcopy(generated)
    period = {"start_date": start.isoformat(), "end_date": end.isoformat(),
              "average_minutes": average, "weekdays_only": settings["weekdays_only"]}
    return EntryDraft("period", start, end, rows, existing, period=period, defaults=generated)


def _render_period(root: Path, labels: dict[str, str], subjects: dict[str, dict]) -> None:
    key = "period"
    initial = _period_defaults(app_today())
    if _PERIOD not in st.session_state:
        saved = _read_draft(root, key)
        st.session_state[_PERIOD] = {
            "settings": saved["settings"] if saved else initial,
            "settings_open": saved["settings_open"] if saved else True,
            "draft": EntryDraft.restore(saved["draft"]) if saved and saved["draft"] else None,
        }
    state = st.session_state[_PERIOD]
    settings = state["settings"]
    draft = state["draft"]
    draft_status = st.empty()
    status = "persist"
    if state["settings_open"]:
        settings, generate = _period_parameters(settings, labels)
        state["settings"] = settings
        if generate:
            try:
                state["draft"] = draft = _generate_period(root, settings, draft)
            except (OSError, ValueError) as exc:
                st.error(str(exc))
            else:
                state["settings_open"] = False
                status = "rerun"
    elif draft:
        average = _duration_label(draft.period["average_minutes"] * 60)
        st.markdown(f"**{draft.start:%Y年}{_date_label(draft.start)} 〜 {_date_label(draft.end)}　1日平均 約{average}**")
        if draft.notice:
            st.warning(draft.notice)
        if draft.approved is not None:
            status = _confirm(root, key, draft, labels)
        else:
            header = st.empty()
            st.caption("登録済みの日は初期状態で対象外です。休んだ日は「追加対象」を外せます。")
            edit_settings = st.button("期間の指定に戻る", key="rough_period_settings")
            draft.rows = _table_rows(draft, labels)
            selection_changed = _period_selection(root, draft)
            records, errors = _records(draft, subjects)
            with header.container():
                _summary(draft, records)
                _period_counts(draft, records)
            if edit_settings:
                settings["settings_id"] = uuid4().hex
                state["settings_open"] = True
                status = "rerun"
            elif selection_changed:
                status = "rerun"
            elif st.button("保存内容を確認", type="primary", key=f"rough_preview_{draft.editor_id}"):
                if _preview(draft, records, errors):
                    status = "rerun"
            elif errors:
                st.caption(errors[0])
    if status == "saved":
        st.session_state.pop(_PERIOD, None)
        st.rerun()
    meaningful = draft or any(settings[name] != initial[name] for name in settings if name != "settings_id")
    payload = {"settings": settings, "settings_open": state["settings_open"],
               "draft": draft.payload() if draft else None} if meaningful else None
    if _write_draft(root, key, payload) and payload:
        draft_status.caption("期間の下書きはローカルに保存済みです。")
    if status == "rerun":
        st.rerun()


def _batch_label(batch: dict) -> str:
    days = sorted({row["study_date"] for row in batch["rows"]})
    bounds = days[0] if len(days) == 1 else f"{days[0]} 〜 {days[-1]}"
    method = "週まとめ" if batch["kind"] == "week" else "期間から入力"
    return f"{bounds} · {method} · {len(batch['rows'])}件"


def _new_editor(root: Path, batch: dict, focus: str | None) -> EntryDraft:
    period = batch["period"]
    if period:
        start, end = date.fromisoformat(period["start_date"]), date.fromisoformat(period["end_date"])
    else:
        start = _monday(date.fromisoformat(batch["rows"][0]["study_date"]))
        end = start + timedelta(days=6)
    originals = sorted(batch["rows"], key=lambda row: (row["record_id"] != focus, row["study_date"]))
    rows = [
        _row(
            date.fromisoformat(row["study_date"]), input_id=row["record_id"], record_id=row["record_id"],
            original_subject_id=row["subject_id"], subject_id=row["subject_id"], memo=row["memo"],
            duration="" if row["duration_sec"] is None else _duration_label(row["duration_sec"]),
            **{name: row.get(name, "") for name in _CLASSIFICATION},
        )
        for row in originals
    ]
    draft = EntryDraft(batch["kind"], start, end, rows, {}, period=period, batch=batch)
    draft.existing = _load_existing(root, draft)
    return draft


def _close_editor() -> None:
    st.session_state.pop(_REQUEST, None)
    st.session_state.pop(_OPEN_BATCH, None)
    st.rerun()


def _edit_batch(root: Path, batch: dict, labels: dict[str, str], subjects: dict[str, dict], *, focus: str | None = None) -> None:
    key = f"edit:{batch['batch_id']}"
    editors = _active_editors()
    if key not in editors:
        saved = _read_draft(root, key)
        editors[key] = EntryDraft.restore(saved) if saved else _new_editor(root, batch, focus)
    draft = editors[key]
    stale = draft.batch["revision"] != batch["revision"]
    st.markdown(f"**{_batch_label(batch)}**")
    if focus:
        row = next((row for row in batch["rows"] if row["record_id"] == focus), None)
        if row:
            st.caption(f"選択した記録　{_date_label(date.fromisoformat(row['study_date']))}　{row['memo'] or labels.get(row['subject_id'], '')}")
    if draft.notice:
        st.warning(draft.notice)
    if stale:
        st.warning("下書きの作成後に保存済みの記録が変更されました。最新の記録を読み直せます。")
        if st.button("下書きを破棄して最新の記録を読み直す", key=f"rough_reload_{batch['batch_id']}"):
            if _write_draft(root, key, None):
                editors.pop(key, None)
                st.rerun()
    go_back = st.button("記録一覧に戻る", key=f"rough_close_{batch['batch_id']}")
    draft_status = st.empty()
    starting_rows = draft.rows
    if draft.approved is not None:
        status = _confirm(root, key, draft, labels)
    else:
        header = st.empty()
        st.caption("削除したい行の「この行を削除」を選ぶと、次の画面で日付と内容を確認できます。")
        draft.rows = _table_rows(draft, labels)
        date_changed = any(old['study_date'] != new['study_date'] for old, new in zip(starting_rows, draft.rows, strict=True))
        add_columns = st.columns([2, 1, 3])
        addition_date = add_columns[0].date_input("追加する日", value=draft.start, min_value=draft.start,
                                                 max_value=draft.end, key=f"rough_add_date_{draft.editor_id}")
        add = add_columns[1].button("この日に1行追加", key=f"rough_add_{draft.editor_id}")
        records, errors = _records(draft, subjects)
        with header.container():
            _summary(draft, records)
        status = "persist"
        if add:
            draft.rows.append(_row(addition_date))
            draft.refresh_grid()
            status = "rerun"
        elif st.button("変更内容を確認", type="primary", key=f"rough_preview_{draft.editor_id}"):
            if _preview(draft, records, errors):
                status = "rerun"
        elif errors:
            st.caption(errors[0])
        with st.expander("このまとまりを削除"):
            st.caption(f"対象のざっくり記録 {len(batch['rows'])}件を勉強ログから削除します。")
            acknowledged = st.checkbox("全件を削除することを確認しました", key=f"rough_ack_delete_{draft.editor_id}")
            if st.button("このまとまりを削除する", disabled=not acknowledged or stale, key=f"rough_delete_{draft.editor_id}"):
                try:
                    delete_rough_study_batch(root, batch_id=batch["batch_id"], expected_revision=batch["revision"],
                                             operation_id=uuid4().hex)
                except (OSError, ValueError) as exc:
                    ab.show_action_error(f"削除できませんでした。{exc}")
                else:
                    st.session_state.pop(_RECENT, None)
                    _write_draft(root, key, None)
                    editors.pop(key, None)
                    ab.queue_success("このまとまりを削除しました。")
                    status = "saved"
        if date_changed and draft.approved is None and status == 'persist':
            draft.refresh_grid()
            status = 'rerun'
    if status == "saved":
        _close_editor()
    has_draft = starting_rows != draft.rows or status == "rerun" or _read_draft(root, key) is not None
    if has_draft and _write_draft(root, key, draft.payload()):
        draft_status.caption("編集中の下書きはローカルに保存済みです。")
    if go_back:
        _close_editor()
    if status == "rerun":
        st.rerun()


def _render_edit(root: Path, labels: dict[str, str], subjects: dict[str, dict]) -> None:
    request = st.session_state.get(_REQUEST)
    current = request["batch_id"] if request else st.session_state.get(_OPEN_BATCH)
    batches = load_rough_study_batches(root)
    if current:
        batch = next((batch for batch in batches if batch["batch_id"] == current), None)
        if batch:
            _edit_batch(root, batch, labels, subjects, focus=request.get("record_id") if request else None)
        else:
            st.info("このまとまりはすでに削除されています。")
            if st.button("記録一覧に戻る", key="rough_missing_back"):
                _close_editor()
        return
    if not batches:
        st.caption("まとめて編集できるざっくり記録はまだありません。")
        return
    options = {batch["batch_id"]: batch for batch in batches}
    selection = st.selectbox("保存した記録（新しい順）", list(options),
                             format_func=lambda ident: _batch_label(options[ident]), key="rough_pick_saved_batch")
    if st.button("選択した記録を開く", key="rough_open_saved_batch"):
        st.session_state[_OPEN_BATCH] = selection
        st.rerun()


def _render_recent() -> None:
    recent = st.session_state.get(_RECENT)
    if recent:
        columns = st.columns([2, 1])
        columns[0].success(f"ざっくり記録を{recent['count']}件保存しました。")
        if columns[1].button("保存した記録を開く", key="rough_open_recent"):
            request_rough_study_editor(recent["batch_id"])
            st.rerun()


@st.fragment
def render_rough_study_entry(root: Path, master_data: dict[str, Any]) -> None:
    try:
        if st.session_state.get(_REQUEST):
            labels, subjects = _subject_choices(master_data, [])
            with st.container(border=True):
                st.subheader("ざっくり記録の編集")
                _render_edit(root, labels, subjects)
            return

        box = st.expander("ざっくり記録", key="rough_study_box", on_change="rerun")
        previous = st.session_state.get("rough_study_visible_panel")
        if not box.open and previous is None:
            return
        recent_key = "rough_recent_subjects"
        if recent_key not in st.session_state:
            recent = sorted(load_rough_study_records(root), key=lambda row: row["study_date"], reverse=True)
            st.session_state[recent_key] = list(dict.fromkeys(row["subject_id"] for row in recent if row["subject_id"]))
        labels, subjects = _subject_choices(master_data, st.session_state[recent_key])
        renderers = {"週まとめ": _render_week, "期間から入力": _render_period, "まとめて編集": _render_edit}
        with box:
            if not box.open:
                # Consume input delivered with the close event before hiding the editor.
                st.session_state.pop("rough_study_visible_panel", None)
                renderers[previous](root, labels, subjects)
                return
            _render_recent()
            tabs = dict(zip(renderers, st.tabs(list(renderers), key="rough_study_tabs", on_change="rerun"), strict=True))
            active = next((name for name, tab in tabs.items() if tab.open), "週まとめ")
            st.session_state["rough_study_visible_panel"] = active
            if previous and previous != active:
                # The last edit may arrive in the same event as the tab selection.
                with tabs[previous]:
                    renderers[previous](root, labels, subjects)
            with tabs[active]:
                renderers[active](root, labels, subjects)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        st.error(f"ざっくり記録を開けませんでした。{exc}")
