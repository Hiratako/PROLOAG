from __future__ import annotations

from copy import deepcopy
import unittest
from unittest.mock import patch

import data_utils


class MasterNameLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.master = {
            "projects": [
                {"project_id": "project_active", "name": "有効プロジェクト", "is_active": True},
                {"project_id": "project_inactive", "name": "無効プロジェクト", "is_active": False},
                {"project_id": "project_duplicate", "name": "先頭の無効項目", "is_active": False},
                {"project_id": "project_duplicate", "name": "後続の有効項目", "is_active": True},
            ],
            "subjects": [{"subject_id": "subject_active", "name": "有効科目", "is_active": True}],
            "materials": [{"material_id": "material_active", "name": "", "is_active": True}],
            "units": [{"unit_id": "unit_inactive", "name": "無効単元", "is_active": False}],
            "tags": [],
        }
        self.lookup = data_utils.build_master_name_lookup(self.master)

    def assert_matches_existing_resolver(self, record: dict[str, object]) -> None:
        with patch.object(data_utils, "load_master_data", return_value=self.master):
            expected = data_utils.resolve_master_names(record)
        actual = data_utils.resolve_master_names_from_lookup(record, self.lookup)
        self.assertEqual(actual, expected)

    def test_active_blank_inactive_and_missing_ids_match_existing_behavior(self) -> None:
        records = [
            {
                "project_id": "project_active",
                "subject_id": "subject_active",
                "material_id": "material_active",
                "unit_id": "unit_inactive",
            },
            {"project_id": "", "subject_id": None, "material_id": 0, "unit_id": "unit_missing"},
            {"project_id": "project_inactive", "subject_id": "subject_missing"},
        ]
        for record in records:
            with self.subTest(record=record):
                self.assert_matches_existing_resolver(record)

    def test_duplicate_ids_keep_the_first_item_behavior(self) -> None:
        self.assert_matches_existing_resolver({"project_id": "project_duplicate"})
        self.assertEqual(
            data_utils.resolve_master_names_from_lookup({"project_id": "project_duplicate"}, self.lookup)["project_name"],
            "不明なプロジェクト",
        )

    def test_building_the_lookup_does_not_mutate_the_master_snapshot(self) -> None:
        before = deepcopy(self.master)
        data_utils.build_master_name_lookup(self.master)
        self.assertEqual(self.master, before)


if __name__ == "__main__":
    unittest.main()
