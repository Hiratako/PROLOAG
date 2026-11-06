"""Create an empty local scaffold without reading or overwriting existing data."""
from __future__ import annotations

import json
from pathlib import Path
import stat

FOLDERS = (
    'study_logs', 'daily_plans', 'diary_entries', 'health_logs', 'exercise_logs',
    'statistics_practice', 'notes_data', 'master_data', 'settings', 'backups',
)
EMPTY_FILES = {
    'notes_data/notebooks.json': {'notebooks': []},
    'master_data/master.json': {'projects': [], 'subjects': [], 'materials': [], 'units': [], 'tags': []},
    'flashcards.json': [],
    'reminders.json': [],
}

def assert_plain_path(path: Path, root: Path) -> None:
    if not path.is_relative_to(root):
        raise ValueError('Initialization path is outside the source directory')
    for candidate in (path, *path.parents):
        if candidate.exists() or candidate.is_symlink():
            info = candidate.lstat()
            if candidate.is_symlink() or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError('Initialization does not follow symlinks or junctions')
        if candidate == root:
            break

def initialize(root: Path | None = None) -> dict[str, list[str]]:
    root = Path(root or Path(__file__).resolve().parents[1]).absolute()
    if not root.is_dir():
        raise ValueError('The source directory must exist')
    for name in (*FOLDERS, *EMPTY_FILES):
        assert_plain_path(root / name, root)
    for name in FOLDERS:
        (root / name).mkdir(parents=True, exist_ok=True)
    result: dict[str, list[str]] = {'created': [], 'kept': []}
    for name, value in EMPTY_FILES.items():
        path = root / name
        try:
            with path.open('x', encoding='utf-8', newline='\n') as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
        except FileExistsError:
            result['kept'].append(name)
        else:
            result['created'].append(name)
    return result

if __name__ == '__main__':
    print(json.dumps(initialize(), ensure_ascii=False, indent=2))
