from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
import csv
from datetime import date, datetime, timedelta
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core import integrity_rules, integrity_service
from app_core import statistics_practice_service as service
from app_core.export_service import build_export_bundle


class StatisticsPhase2SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="proloag-statistics-phase2-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.start = date(2020, 1, 1)
        self.end = self.start + timedelta(days=39)

    def write_json(self, relative: str, value: object) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def source_snapshot(self) -> dict[str, tuple[str, int]]:
        return {
            str(path.relative_to(self.root)): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
            for folder in ("study_logs", "health_logs", "exercise_logs", "diary_entries")
            for path in (self.root / folder).rglob("*.json")
        }

    def seed_personal(self) -> None:
        for index in range(40):
            target = self.start + timedelta(days=index)
            stamp = f"{target:%Y/%m/%Y%m%d}"
            sessions = [{
                "session_id": f"session_{target:%Y%m%d}_{part}",
                "start_at": f"{target.isoformat()}T{8 + part * 4:02d}:00:00",
                "end_at": f"{target.isoformat()}T{10 + part * 4:02d}:00:00",
                "duration_sec": 60 * (30 + (index * 7 + part * 11) % 80),
                "memo": "private_source_text",
                "condition": {
                    "environment": ("自宅", "カフェ", "図書館")[(index + part) % 3],
                    "io_type": ("インプット", "アウトプット", "両方")[(index * 2 + part) % 3],
                    "category": ("医学", "英語", "IT")[(index + part * 2) % 3],
                },
            } for part in range(2)]
            self.write_json(f"study_logs/{stamp}.json", sessions)
            self.write_json(f"health_logs/{stamp}.json", {
                "sleep": {"sleep_duration_min": 330 + index % 9 * 20},
                "condition": {"focus_score": 1 + index % 5, "fatigue": 1 + index * 2 % 5},
            })

    @staticmethod
    def answers(problem: dict) -> dict:
        return {part["key"]: part["expected"] for part in problem["answer_parts"]}

    def generate(self, **kwargs: object) -> dict:
        return service.generate_problem(self.start, self.end, root=self.root, seed=97531, **kwargs)

    def test_catalog_modes_are_explicit_disjoint_and_complete(self) -> None:
        catalogs = {mode: service.template_catalog(data_mode=mode) for mode in ("personal", "synthetic", "all")}
        ids = {mode: {row["template_id"] for row in rows} for mode, rows in catalogs.items()}
        self.assertEqual((len(ids["personal"]), len(ids["synthetic"]), len(ids["all"])), (18, 6, 24))
        self.assertFalse(ids["personal"] & ids["synthetic"])
        self.assertEqual(ids["all"], ids["personal"] | ids["synthetic"])
        for mode in ("personal", "synthetic"):
            for row in catalogs[mode]:
                self.assertEqual(row["data_origin"], "personal_logs" if mode == "personal" else "synthetic")
                self.assertTrue(row["title"])
                self.assertEqual(row["topic_label"], service.TOPIC_LABELS[row["topic"]])
        for topic in service.TOPIC_LABELS:
            self.assertTrue(all(row["topic"] == topic for row in service.template_catalog(topic_key=topic)))

    def test_synthetic_works_without_logs_and_does_not_read_personal_context(self) -> None:
        before = list(self.root.iterdir())
        with patch.object(service, "_load_context", side_effect=AssertionError("personal context was read")):
            summary = service.available_problem_summary(self.start, self.end, self.root, data_mode="synthetic")
            self.assertEqual(summary["available_count"], 6)
            self.assertEqual(summary["total_templates"], 6)
            for row in service.template_catalog(data_mode="synthetic"):
                problem = self.generate(data_mode="synthetic", template_id=row["template_id"])
                self.assertEqual(problem["template_id"], row["template_id"])
                self.assertEqual(problem["data_origin"], "synthetic")
                self.assertEqual(service.grade_problem(problem, self.answers(problem))["score"], 100)
        self.assertEqual(list(self.root.iterdir()), before)

    def test_mode_and_template_boundaries_reject_invalid_requests(self) -> None:
        personal = service.template_catalog(data_mode="personal")[0]["template_id"]
        synthetic = service.template_catalog(data_mode="synthetic")[0]["template_id"]
        for kwargs in (
            {"data_mode": "invalid"}, {"selection_mode": "invalid"},
            {"data_mode": "personal", "template_id": synthetic},
            {"data_mode": "synthetic", "template_id": personal},
            {"data_mode": "all", "template_id": "missing_template"},
            {"data_mode": "synthetic", "topic_key": "missing_topic"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.generate(**kwargs)
        with self.assertRaises(service.NoAvailableProblemError):
            self.generate(data_mode="personal")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_all_mode_with_empty_logs_has_only_synthetic_candidates(self) -> None:
        summary = service.available_problem_summary(self.start, self.end, self.root, data_mode="all")
        self.assertEqual(summary["total_templates"], 24)
        self.assertEqual(summary["available_count"], 6)
        self.assertTrue(all(row["data_origin"] == "synthetic" for row in summary["templates"] if row["available"]))
        problem = self.generate(data_mode="all")
        self.assertEqual(problem["data_origin"], "synthetic")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_every_template_is_seed_reproducible_with_frozen_metadata(self) -> None:
        self.seed_personal()
        before = self.source_snapshot()
        for row in service.template_catalog():
            with self.subTest(template=row["template_id"]):
                first = self.generate(data_mode="all", template_id=row["template_id"])
                second = self.generate(data_mode="all", template_id=row["template_id"])
                self.assertEqual(first, second)
                for key in ("data_origin", "data_origin_label"):
                    self.assertIn(key, first)
                    self.assertEqual(first[key], first["snapshot"][key])
                for key in ("parameters", "calculation_spec", "result_chart", "selection"):
                    if key in first:
                        self.assertEqual(first[key], first["snapshot"][key])
                self.assertNotIn("private_source_text", json.dumps(first, ensure_ascii=False, allow_nan=False))
                self.assertEqual(service.grade_problem(first, self.answers(first))["score"], 100)
        self.assertEqual(self.source_snapshot(), before)

    def test_progress_uses_latest_five_scores_and_keeps_unpracticed_weight(self) -> None:
        catalog = service.template_catalog(data_mode="synthetic")
        good, weak, unpracticed = [row["template_id"] for row in catalog[:3]]
        attempts = []
        for index, score in enumerate((0, 0, 100, 100, 100, 100, 100)):
            attempts.append({"template_id": good, "score": score, "attempted_at": f"2026-08-{index + 1:02d}T10:00:00"})
        attempts.append({"template_id": weak, "score": 0, "attempted_at": "2026-08-08T10:00:00"})
        original = deepcopy(attempts)
        rows = {row["template_id"]: row for row in service.practice_progress(attempts=list(reversed(attempts)), root=self.root)}
        self.assertEqual(rows[good]["attempt_count"], 7)
        self.assertEqual(rows[good]["recent_count"], 5)
        self.assertEqual(rows[good]["average_score"], 100)
        self.assertEqual(rows[good]["weight"], 1)
        self.assertEqual(rows[good]["last_attempted_at"], "2026-08-07T10:00:00")
        self.assertEqual(rows[weak]["weight"], 6)
        self.assertEqual(rows[unpracticed]["weight"], 3)
        self.assertEqual(rows[unpracticed]["attempt_count"], 0)
        self.assertIsNone(rows[unpracticed]["average_score"])
        self.assertEqual(attempts, original)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_weak_selection_is_seed_reproducible_and_favors_low_recent_scores(self) -> None:
        catalog = service.template_catalog(data_mode="synthetic")
        weak = catalog[0]["template_id"]
        attempts = [{"template_id": row["template_id"], "score": 0 if row["template_id"] == weak else 100,
                     "attempted_at": "2026-08-01T10:00:00"} for row in catalog]
        counts = {row["template_id"]: 0 for row in catalog}
        with patch.object(service, "load_attempts", return_value=attempts):
            first = self.generate(data_mode="synthetic", selection_mode="weak")
            self.assertEqual(first, self.generate(data_mode="synthetic", selection_mode="weak"))
            for seed in range(80):
                problem = service.generate_problem(self.start, self.end, seed=seed, root=self.root,
                                                   data_mode="synthetic", selection_mode="weak")
                counts[problem["template_id"]] += 1
        self.assertGreater(counts[weak], max(count for key, count in counts.items() if key != weak))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_weak_selection_metadata_remains_frozen_when_history_changes(self) -> None:
        attempts = [{"template_id": row["template_id"], "score": 20, "attempted_at": "2026-08-01T10:00:00"}
                    for row in service.template_catalog(data_mode="synthetic")]
        with patch.object(service, "load_attempts", return_value=attempts):
            problem = self.generate(data_mode="synthetic", selection_mode="weak")
        frozen = deepcopy(problem)
        for attempt in attempts:
            attempt["score"] = 100
        self.assertEqual(problem["selection"]["average_score"], 20)
        self.assertEqual(problem["selection"], problem["snapshot"]["selection"])
        with patch.object(service, "load_attempts", side_effect=AssertionError("grading reloaded history")):
            self.assertEqual(service.grade_problem(problem, self.answers(problem))["score"], 100)
        self.assertEqual(problem, frozen)

    def test_old_phase1_snapshot_remains_loadable_gradable_and_resavable(self) -> None:
        self.seed_personal()
        problem = self.generate(template_id="descriptive_mean")
        for container in (problem, problem["snapshot"]):
            for key in ("data_origin", "data_origin_label", "parameters", "calculation_spec", "result_chart", "selection"):
                container.pop(key, None)
        answers = self.answers(problem)
        grade = service.grade_problem(problem, answers)
        now = datetime(2026, 8, 31, 10, 0)
        with patch.object(service, "app_now", return_value=now):
            saved = service.save_attempt(problem, answers, grade, self.root)
        loaded = service.load_attempts(root=self.root)
        self.assertEqual(loaded, [saved])
        self.assertEqual(loaded[0]["problem_snapshot"], problem)
        self.assertEqual(service.grade_problem(loaded[0]["problem_snapshot"], answers)["score"], 100)
        progress = {row["template_id"]: row for row in service.practice_progress(root=self.root)}
        self.assertEqual(progress["descriptive_mean"]["attempt_count"], 1)
        self.assertEqual(progress["descriptive_mean"]["weight"], 1)

    def test_generated_snapshot_does_not_change_when_source_data_changes(self) -> None:
        self.seed_personal()
        problem = self.generate(template_id="descriptive_mean")
        frozen = deepcopy(problem)
        for path in (self.root / "study_logs").rglob("*.json"):
            rows = json.loads(path.read_text(encoding="utf-8"))
            for row in rows:
                row["duration_sec"] *= 3
            path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        with patch.object(service, "_load_context", side_effect=AssertionError("grading reloaded logs")):
            grade = service.grade_problem(problem, self.answers(problem))
        self.assertEqual(grade["score"], 100)
        self.assertEqual(problem, frozen)

    def test_phase2_history_roundtrips_export_and_integrity_without_source_writes(self) -> None:
        self.seed_personal()
        before = self.source_snapshot()
        problem = self.generate(data_mode="synthetic")
        answers = self.answers(problem)
        with patch.object(service, "app_now", return_value=datetime(2026, 8, 31, 11, 0)):
            saved = service.save_attempt(problem, answers, service.grade_problem(problem, answers), self.root)
        loaded = service.load_attempts(root=self.root)
        self.assertEqual(loaded, [saved])
        self.assertEqual(loaded[0]["problem_snapshot"], problem)
        all_before = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*.json")}
        bundle = build_export_bundle(self.root)
        table = next(item for item in bundle.tables if item.key == "statistics_practice_attempts")
        rows = list(csv.DictReader(io.StringIO(table.data.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["template_id"], problem["template_id"])
        self.assertEqual(rows[0]["attempt_id"], saved["attempt_id"])
        locations = {"PROJECT_ROOT": self.root, "STATISTICS_PRACTICE_DIR": self.root / "statistics_practice"}
        for name in ("STUDY_LOGS", "DAILY_PLANS", "DIARY_ENTRIES", "HEALTH_LOGS", "EXERCISE_LOGS", "NOTES_DATA", "MASTER_DATA", "SETTINGS"):
            locations[f"{name}_DIR"] = self.root / name.lower()
        for name, relative in (("FLASHCARDS_PATH", "flashcards.json"), ("REMINDERS_PATH", "reminders.json"),
                               ("USER_SETTINGS_PATH", "settings/user_settings.json"),
                               ("SUBSTANCE_PROFILES_PATH", "settings/substance_profiles.json")):
            locations[name] = self.root / relative
        with ExitStack() as stack:
            for module in (integrity_rules, integrity_service):
                for name, path in locations.items():
                    if hasattr(module, name):
                        stack.enter_context(patch.object(module, name, path))
            stack.enter_context(patch.object(integrity_rules, "DATED_DATASETS", (
                ("statistics_practice", "統計演習履歴", self.root / "statistics_practice"),
            )))
            report = integrity_service.run_integrity_checks()
        self.assertEqual([row for row in report["issues"] if row["重要度"] == integrity_rules.ERROR], [])
        self.assertEqual({str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*.json")}, all_before)
        self.assertEqual(self.source_snapshot(), before)


if __name__ == "__main__":
    unittest.main()
