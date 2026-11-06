from __future__ import annotations

import ast
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from app_core import master_snapshot
from app_core.master_snapshot import MasterSnapshot
import data_utils


PROJECT = Path(__file__).resolve().parents[1]


class PageUI:
    """Run the actual classification widgets without loading personal records."""

    def __init__(self) -> None:
        self.session_state: dict[str, Any] = {}
        self.choices: list[tuple[str, list[str], str | None]] = []
        self.tables: list[pd.DataFrame] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def selectbox(self, label, options, *, index=0, key=None, **_kwargs):
        options = list(options)
        self.choices.append((label, options, key))
        return options[index]

    def form(self, *_args, **_kwargs):
        return self

    def text_input(self, _label, *, value="", **_kwargs):
        return value

    text_area = text_input

    def checkbox(self, _label, *, value=False, **_kwargs):
        return value

    def form_submit_button(self, *_args, **_kwargs):
        return False

    def dataframe(self, value, **_kwargs):
        self.tables.append(value.copy(deep=True))

    def info(self, *_args, **_kwargs):
        pass


def page_scope(number: str, ui: PageUI) -> dict[str, Any]:
    """Extract real page functions and its per-render snapshot initialization."""
    path = next((PROJECT / "pages").glob(f"{number}_*.py"))
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    constants = {"RAW_MASTER", "MASTER_SNAPSHOT", "MATERIAL_TYPES", "IMPORTANCE_OPTIONS", "PRIORITY_OPTIONS", "STATUS_OPTIONS", "SCHEDULE_OPTIONS", "RESULT_LABELS", "TODAY"}
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            node = deepcopy(node)
            node.decorator_list = []
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in constants for target in node.targets):
            nodes.append(node)
    scope = {
        **vars(data_utils),
        "MasterSnapshot": MasterSnapshot,
        "deepcopy": deepcopy,
        # The master page now shares its raw read with the strict materials UI.
        # This harness still exercises widgets without accessing real files.
        "load_json_safe": lambda _path, _default: master_snapshot.load_master_data(Path("unused-test-root")),
        "ROOT": Path("unused-test-root"),
        "Any": Any,
        "st": ui,
        "pd": pd,
        "app_today": lambda: date(2026, 9, 6),
        "schedule_progress": lambda _item: "1 / 3",
        "urlparse": urlparse,
    }
    code = compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec")
    exec(code, scope)
    return scope


class MasterSnapshotPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.master = {
            "projects": [
                {"project_id": "p1", "name": "受験", "is_active": True},
                {"project_id": "p2", "name": "仕事", "is_active": True},
                {"project_id": "p0", "name": "終了", "is_active": False},
            ],
            "subjects": [
                {"subject_id": "s1", "project_id": "p1", "name": "統計"},
                {"subject_id": "s2", "project_id": "p2", "name": "英語"},
                {"subject_id": "s0", "project_id": "p1", "name": "無効", "is_active": False},
            ],
            "materials": [
                {"material_id": "m1", "project_id": "p1", "subject_id": "s1", "name": "小問集"},
                {"material_id": "m2", "project_id": "p2", "subject_id": "s2", "name": "単語帳"},
            ],
            "units": [
                {"unit_id": "u1", "project_id": "p1", "subject_id": "s1", "material_id": "m1", "name": "記述統計"},
                {"unit_id": "u2", "project_id": "p2", "subject_id": "s2", "material_id": "m2", "name": "章1"},
            ],
            "tags": [{"tag_id": "t1", "name": "重要"}],
        }
        self.record = {"project_id": "p1", "subject_id": "s1", "material_id": "m1", "unit_id": "u1"}

    def expected_choices(self, record: dict[str, str], *, timer: bool) -> list[list[str]]:
        """Use the established service behavior as the option-order reference."""
        selected = []
        expected = []
        loaders = [data_utils.get_active_projects, data_utils.get_active_subjects, data_utils.get_active_materials, data_utils.get_active_units]
        keys = ["project_id", "subject_id", "material_id", "unit_id"]
        for position, (loader, key) in enumerate(zip(loaders, keys)):
            items = loader(*selected)
            if timer and position >= 2 and not selected[-1]:
                items = []
            options = {"未設定": ""}
            options.update({f"{item.get('name', '名称未設定')} [{item.get(key, '')}]": item.get(key, "") for item in items})
            expected.append(list(options))
            selected.append(record.get(key, "") if record.get(key, "") in options.values() else "")
        return expected

    def test_classification_options_and_defaults_match_without_repeat_loads(self) -> None:
        records = [self.record, {}, {"project_id": "p0", "subject_id": "missing"}]
        for number in ("01", "04", "07", "08", "09"):
            for record in records:
                with self.subTest(page=number, record=record):
                    with patch.object(data_utils, "load_master_data", return_value=self.master):
                        expected = self.expected_choices(record, timer=number == "01")
                    if number in ("04", "09") and record.get("project_id") == "p0":
                        # An unrelated note-body edit must not erase saved
                        # inactive/unknown classification IDs (package A).
                        expected = [
                            ["未設定", "受験 [p1]", "仕事 [p2]", "保存済み（非表示・未登録） [p0]"],
                            ["未設定", "保存済み（非表示・未登録） [missing]"],
                            ["未設定"], ["未設定"],
                        ]
                    ui = PageUI()
                    with patch.object(master_snapshot, "load_master_data", return_value=self.master) as read:
                        scope = page_scope(number, ui)
                        # The timer now owns its snapshot inside the entry fragment.
                        snapshot = MasterSnapshot.load(scope["ROOT"]) if number == "01" else None
                        for _ in range(3):
                            actual = scope["master_selects"]("test", record, snapshot) if number == "01" else scope["master_selects"]("test", record)
                        self.assertEqual(read.call_count, 1)
                    self.assertEqual([choices for _, choices, _ in ui.choices[:4]], expected)
                    self.assertEqual([key for _, _, key in ui.choices[:4]], ["test_project", "test_subject", "test_material", "test_unit"])
                    if record is self.record:
                        self.assertEqual(actual, self.record)
                    elif number in ("04", "09") and record.get("project_id") == "p0":
                        self.assertEqual(actual, {"project_id": "p0", "subject_id": "missing", "material_id": "", "unit_id": ""})
                    else:
                        self.assertEqual(actual, dict.fromkeys(self.record, ""))

    def test_next_render_reloads_updated_candidates(self) -> None:
        changed = deepcopy(self.master)
        changed["projects"].append({"project_id": "p3", "name": "新規"})
        with patch.object(master_snapshot, "load_master_data", side_effect=[self.master, changed]) as read:
            first_ui, second_ui = PageUI(), PageUI()
            page_scope("04", first_ui)["master_selects"]("test", {})
            page_scope("04", second_ui)["master_selects"]("test", {})
            self.assertEqual(read.call_count, 2)
        self.assertNotIn("新規 [p3]", first_ui.choices[0][1])
        self.assertIn("新規 [p3]", second_ui.choices[0][1])

    def test_note_export_card_and_reminder_names_reuse_the_snapshot(self) -> None:
        for number, function in (("04", "page_to_markdown"), ("07", "card_row"), ("08", "reminder_row")):
            with self.subTest(page=number):
                ui = PageUI()
                record = {**self.record, "title": "確認", "question": "問題", "answer": "答え"}
                with patch.object(master_snapshot, "load_master_data", return_value=self.master) as read:
                    scope = page_scope(number, ui)
                    for _ in range(10):
                        result = scope[function]({}, {}, record) if number == "04" else scope[function](record)
                    self.assertEqual(read.call_count, 1)
                if number == "04":
                    self.assertIn("- Project: 受験\n- Subject: 統計\n- Material: 小問集\n- Unit: 記述統計", result)
                else:
                    self.assertEqual([result[key] for key in ("科目", "教材", "単元")], ["統計", "小問集", "記述統計"])

    def test_master_tabs_and_tables_share_one_read_and_preserve_inactive_names(self) -> None:
        ui = PageUI()
        with patch.object(master_snapshot, "load_master_data", return_value=self.master) as read:
            scope = page_scope("10", ui)
            for kind in ("project", "subject", "material", "unit", "tag"):
                scope[f"render_{kind}_tab"]()
            self.assertEqual(read.call_count, 1)
        self.assertEqual(len(ui.tables), 5)
        self.assertEqual(ui.tables[0].iloc[2]["project_name"], "不明なプロジェクト")
        self.assertEqual(ui.tables[2].iloc[0]["subject_name"], "統計")
        self.assertEqual(ui.tables[3].iloc[0]["material_name"], "小問集")

    def test_master_save_loads_latest_data_instead_of_saving_display_snapshot(self) -> None:
        latest = deepcopy(self.master)
        latest["projects"].append({"project_id": "external", "name": "別操作で追加"})
        with patch.object(master_snapshot, "load_master_data", return_value=self.master):
            scope = page_scope("10", PageUI())
        read, save = Mock(return_value=latest), Mock()
        scope.update(load_master_data=read, save_master_data=save)
        scope["upsert_item"]("projects", "project_id", {"project_id": "p1", "name": "変更"})
        read.assert_called_once_with(scope["ROOT"])
        saved = save.call_args.args[0]
        self.assertEqual(saved["projects"][0]["name"], "変更")
        self.assertEqual(saved["projects"][-1]["project_id"], "external")
        self.assertEqual(scope["MASTER_SNAPSHOT"].data["projects"][0]["name"], "受験")

    def test_master_display_normalization_does_not_repair_raw_material_validation(self) -> None:
        from app_core.learning_context_service import LearningContextError, list_materials

        raw = {"projects": [], "subjects": [], "materials": [None, {"name": "IDなし"}]}
        original = deepcopy(raw)
        with patch.object(master_snapshot, "load_master_data", return_value=raw):
            scope = page_scope("10", PageUI())
        self.assertEqual(raw, original)
        self.assertEqual(scope["RAW_MASTER"], original)
        with self.assertRaises(LearningContextError):
            list_materials(scope["ROOT"], master_data=scope["RAW_MASTER"])


if __name__ == "__main__":
    unittest.main()
