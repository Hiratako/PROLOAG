"""Read-only pending-operation notice; rollback requires explicit confirmation."""
from pathlib import Path

import streamlit as st
import prolog_ui.components as ab

from app_core.json_operations import pending_json_operations, rollback_json_operation
from app_core.json_store import JsonStoreError


def render_save_recovery(root: Path) -> None:
    try:
        pending = pending_json_operations(root)
    except (JsonStoreError, OSError) as exc:
        st.error(f"保存操作の復旧情報を確認できません: {exc}")
        return
    if not pending:
        return
    st.warning("未完了の保存操作があります。新しい保存の前に復旧してください。")
    with st.expander("保存操作の復旧", expanded=True):
        st.caption("中断した操作を保存前へ戻します。中断後の別編集がある場合は上書きせず停止します。編集フォームの入力は残ります。")
        for entry in pending:
            operation_id = entry["operation_id"]
            st.write(f"{entry.get('created_at', '')} / {entry.get('kind', '')}")
            st.caption(f"操作ID: {operation_id}")
            confirmed = st.checkbox("この中断操作を保存前へ戻す", key=f"save_recovery_confirm_{operation_id}")
            if st.button("確認した操作を復旧", key=f"save_recovery_apply_{operation_id}", disabled=not confirmed):
                try:
                    rollback_json_operation(operation_id, root)
                except (JsonStoreError, OSError) as exc:
                    ab.show_action_error(str(exc), toast_message="保存操作の復旧を完了できませんでした。詳細表示を確認してください。")
                else:
                    ab.queue_success("中断した保存を変更前へ戻しました。入力内容を確認して保存し直してください。")
                    st.rerun()
