from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent

# This separate distribution is an exhibition, never a personal data store.
# No environment variable, query parameter or session option disables this.
WEB_DEMO_READ_ONLY = True

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
