from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from data_utils import (
    build_master_name_lookup,
    load_master_data,
    resolve_master_names_from_lookup,
)


class MasterSnapshot:
    """One render's classification data; never retained across reruns or saved.

    Loading still uses the strict JSON reader. Save operations must continue to
    load their own current data and keep the existing conflict checks.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = deepcopy(data)
        self._names = build_master_name_lookup(self.data)

    @classmethod
    def load(cls, root: Path | None = None) -> MasterSnapshot:
        return cls(load_master_data(root))

    def active(self, collection: str, **filters: Any) -> list[dict[str, Any]]:
        items = [item for item in self.data.get(collection, []) if item.get("is_active", True)]
        for key, value in filters.items():
            if value:
                items = [item for item in items if item.get(key) == value]
        return items

    def names(self, record: dict[str, Any]) -> dict[str, str]:
        return resolve_master_names_from_lookup(record, self._names)

    def name(self, id_key: str, item_id: Any) -> str:
        output_key = {
            "project_id": "project_name",
            "subject_id": "subject_name",
            "material_id": "material_name",
            "unit_id": "unit_name",
        }[id_key]
        return self.names({id_key: item_id})[output_key]
