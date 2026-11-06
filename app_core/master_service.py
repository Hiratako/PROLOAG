from __future__ import annotations

from pathlib import Path
from typing import Any

from config import PROJECT_ROOT


def _root(root: Path | None = None) -> Path:
    return root or PROJECT_ROOT


def load_master_data(root: Path | None = None, *, write_back: bool = False) -> dict[str, Any]:
    from data_utils import load_master_data as _load

    return _load(_root(root), write_back=write_back)


def save_master_data(data: dict[str, Any], root: Path | None = None) -> None:
    from data_utils import save_master_data as _save

    _save(data, _root(root))


def ensure_master_data(root: Path | None = None) -> dict[str, Any]:
    return load_master_data(root, write_back=True)


def get_active_projects(root: Path | None = None) -> list[dict[str, Any]]:
    from data_utils import get_active_projects as _get

    return _get(_root(root))


def get_active_subjects(project_id: str | None = None, root: Path | None = None) -> list[dict[str, Any]]:
    from data_utils import get_active_subjects as _get

    return _get(project_id, _root(root))


def get_active_materials(project_id: str | None = None, subject_id: str | None = None, root: Path | None = None) -> list[dict[str, Any]]:
    from data_utils import get_active_materials as _get

    return _get(project_id, subject_id, _root(root))


def get_active_units(project_id: str | None = None, subject_id: str | None = None, material_id: str | None = None, root: Path | None = None) -> list[dict[str, Any]]:
    from data_utils import get_active_units as _get

    return _get(project_id, subject_id, material_id, _root(root))


def get_active_tags(root: Path | None = None) -> list[dict[str, Any]]:
    from data_utils import get_active_tags as _get

    return _get(_root(root))


def resolve_project_name(project_id: str | None, root: Path | None = None) -> str:
    from data_utils import resolve_project_name as _resolve

    return _resolve(project_id, _root(root))


def resolve_subject_name(subject_id: str | None, root: Path | None = None) -> str:
    from data_utils import resolve_subject_name as _resolve

    return _resolve(subject_id, _root(root))


def resolve_material_name(material_id: str | None, root: Path | None = None) -> str:
    from data_utils import resolve_material_name as _resolve

    return _resolve(material_id, _root(root))


def resolve_unit_name(unit_id: str | None, root: Path | None = None) -> str:
    from data_utils import resolve_unit_name as _resolve

    return _resolve(unit_id, _root(root))


def resolve_tag_name(tag_id: str | None, root: Path | None = None) -> str:
    from data_utils import resolve_tag_name as _resolve

    return _resolve(tag_id, _root(root))


def resolve_master_names(record: dict[str, Any], root: Path | None = None) -> dict[str, str]:
    from data_utils import resolve_master_names as _resolve

    return _resolve(record, _root(root))
