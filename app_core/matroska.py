"""Identify Matroska files independently of the filename and shared WebM demuxer."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import BinaryIO

from app_core.media_conversion import MediaError


def _read_vint(stream: BinaryIO, *, identifier: bool = False) -> int:
    first = stream.read(1)
    if not first or first[0] == 0:
        raise ValueError("Invalid EBML integer")
    size = 9 - first[0].bit_length()
    if identifier and size > 4:
        raise ValueError("Invalid EBML element identifier")
    remainder = stream.read(size - 1)
    if len(remainder) != size - 1:
        raise ValueError("Truncated EBML integer")
    value = int.from_bytes(first + remainder, "big")
    if identifier:
        return value
    marker = 1 << (7 * size)
    if value == 2 * marker - 1:
        raise ValueError("EBML header elements must have known sizes")
    return value - marker


def _doc_type(path: Path) -> bytes:
    with path.open("rb") as source:
        if _read_vint(source, identifier=True) != 0x1A45DFA3:
            raise ValueError("Missing EBML header")
        size = _read_vint(source)
        if not 0 < size <= 64 * 1024:
            raise ValueError("Invalid EBML header size")
        payload = source.read(size)
    if len(payload) != size:
        raise ValueError("Truncated EBML header")
    header = BytesIO(payload)
    doc_type = None
    while header.tell() < size:
        element = _read_vint(header, identifier=True)
        length = _read_vint(header)
        if length > size - header.tell():
            raise ValueError("Truncated EBML header element")
        if element == 0x4282:
            if doc_type is not None:
                raise ValueError("Duplicate DocType")
            doc_type = header.read(length)
        else:
            header.seek(length, 1)
    return doc_type or b""


def inspect_matroska(path: Path, probe: dict) -> None:
    """Require the Matroska DocType as well as successful container probing."""
    details = probe.get("format")
    formats = str(details.get("format_name", "")).split(",") if isinstance(details, dict) else []
    try:
        valid = "matroska" in formats and _doc_type(path) == b"matroska"
    except (OSError, ValueError):
        valid = False
    if not valid:
        raise MediaError("ファイルの内容がMKV（Matroska）形式と一致しません。")
