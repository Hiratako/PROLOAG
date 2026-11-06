from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from app_core.study_log_service import duration_before_start, session_start_at


class StudyLogServiceDatetimeTests(unittest.TestCase):
    def test_timezone_aware_stored_start_compares_with_manual_naive_start(self) -> None:
        aware_start = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)
        local_start = aware_start.astimezone().replace(tzinfo=None)
        sessions = [{"start_at": aware_start.isoformat(), "duration_sec": 3600}]

        self.assertEqual(session_start_at(sessions[0]), local_start)
        self.assertEqual(duration_before_start(sessions, local_start + timedelta(hours=1)), 3600)


if __name__ == "__main__":
    unittest.main()
