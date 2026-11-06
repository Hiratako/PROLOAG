from __future__ import annotations

import os
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parent

# This separate distribution is an exhibition, never a personal data store.
# No environment variable, query parameter or session option disables this.
WEB_DEMO_READ_ONLY = True
WEB_DEMO_EXPERIENCE = True


def get_data_root() -> Path:
    from web_demo.sandbox import current_root
    return current_root() or SOURCE_ROOT


class ContextPath(os.PathLike):
    """Resolve legacy imported paths/defaults in the visitor's own context."""

    def __init__(self, relative: os.PathLike | str = "."):
        self._relative = Path(relative)
        if self._relative.is_absolute() or ".." in self._relative.parts:
            raise ValueError("Data path must be relative to its visitor root.")

    def _path(self) -> Path:
        return get_data_root() / self._relative

    def __fspath__(self) -> str:
        return os.fspath(self._path())

    def __str__(self) -> str:
        return str(self._path())

    def __repr__(self) -> str:
        return f"ContextPath({str(self._relative)!r})"

    def __eq__(self, other) -> bool:
        try:
            return self._path() == Path(other)
        except TypeError:
            return False

    def __hash__(self) -> int:
        # Legacy forms look up a context-relative path in dictionaries whose
        # keys were canonicalized by edit_snapshot. Match pathlib's hash in
        # the caller's context. Persisted snapshots still store concrete Paths.
        return hash(self._path())

    def __truediv__(self, value: os.PathLike | str) -> ContextPath:
        return ContextPath(self._relative / value)

    def joinpath(self, *parts: os.PathLike | str) -> ContextPath:
        return ContextPath(self._relative.joinpath(*parts))

    def __getattr__(self, name):
        return getattr(self._path(), name)


PROJECT_ROOT = ContextPath()

# Reserved for a future migration. Existing data remains in the current
# top-level folders for now to avoid moving or breaking local records.
DATA_DIR = PROJECT_ROOT / "data"

STUDY_LOGS_DIR = PROJECT_ROOT / "study_logs"
DAILY_PLANS_DIR = PROJECT_ROOT / "daily_plans"
NOTES_DATA_DIR = PROJECT_ROOT / "notes_data"
MASTER_DATA_DIR = PROJECT_ROOT / "master_data"
DIARY_ENTRIES_DIR = PROJECT_ROOT / "diary_entries"
HEALTH_LOGS_DIR = PROJECT_ROOT / "health_logs"
EXERCISE_LOGS_DIR = PROJECT_ROOT / "exercise_logs"
STATISTICS_PRACTICE_DIR = PROJECT_ROOT / "statistics_practice"
SETTINGS_DIR = PROJECT_ROOT / "settings"
USER_SETTINGS_PATH = SETTINGS_DIR / "user_settings.json"
SUBSTANCE_PROFILES_PATH = SETTINGS_DIR / "substance_profiles.json"
FLASHCARDS_PATH = PROJECT_ROOT / "flashcards.json"
REMINDERS_PATH = PROJECT_ROOT / "reminders.json"
BACKUP_DIR = PROJECT_ROOT / "backups"
