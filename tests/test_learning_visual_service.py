from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
import unittest

from app_core.analysis_dates import parse_analysis_date
from app_core.learning_visual_service import build_learning_visual_payload


class FakeMasterSnapshot:
    """The read-only names contract, backed exclusively by synthetic data."""

    def __init__(self) -> None:
        self.data = {
            "project_id": {"p1": "資格", "p2": "語学"},
            "subject_id": {"s1": "同じ科目", "s2": "同じ科目"},
            "material_id": {"m1": "同じ教材", "m2": "同じ教材"},
            "unit_id": {"u1": "同じ単元", "u2": "同じ単元"},
        }

    def names(self, record: dict) -> dict[str, str]:
        result = {}
        for field, name, unknown in (
            ("project_id", "project_name", "不明なプロジェクト"),
            ("subject_id", "subject_name", "不明な科目"),
            ("material_id", "material_name", "不明な教材"),
            ("unit_id", "unit_name", "不明な単元"),
        ):
            identity = record.get(field)
            result[name] = "未設定" if not identity else self.data[field].get(identity, unknown)
        return result


def session(**changes) -> dict:
    record = {
        "session_id": "session-1",
        "start_at": "2026-10-01T10:00:00",
        "end_at": "2026-10-01T11:00:00",
        "duration_sec": 3600,
        "project_id": "p1",
        "subject_id": "s1",
        "material_id": "m1",
        "unit_id": "u1",
        "memo": "合成データ",
    }
    record.update(changes)
    return record


class LearningVisualServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.master = FakeMasterSnapshot()

    def build(self, records, *, start="2026-10-01", end="2026-10-03", filters=None, notes=None):
        return build_learning_visual_payload(records, self.master, start, end, filters, notes=notes)

    def test_manual_duration_is_authoritative_and_missing_days_are_retained(self) -> None:
        payload = self.build([
            session(duration_sec=33000, end_at="2026-10-01T10:00:00", manual=True),
            session(session_id="second", start_at="2026-10-03 09:00:00", duration_sec="1800"),
        ])
        self.assertEqual(payload["days"], ["2026-10-01", "2026-10-02", "2026-10-03"])
        self.assertEqual(payload["total_sec"], 34800)
        self.assertEqual(payload["total_sessions"], 2)
        node = payload["nodes"][0]
        self.assertEqual(node["daily"], [33000, 0, 1800])
        self.assertEqual(node["session_count"], 2)
        self.assertEqual(node["last_day"], "2026-10-03")
        self.assertEqual(node["history"][1]["duration_sec"], 33000)

    def test_reverse_period_is_normalized_with_inclusive_boundaries(self) -> None:
        records = [session(start_at=f"2026-10-{day:02d}") for day in (1, 2, 3, 4)]
        payload = self.build(records, start=date(2026, 10, 3), end=date(2026, 10, 1))
        self.assertEqual((payload["start"], payload["end"]), ("2026-10-01", "2026-10-03"))
        self.assertEqual(payload["total_sessions"], 3)
        self.assertEqual(payload["total_sec"], 10800)

    def test_invalid_range_raises_clear_error(self) -> None:
        for start, end in (("bad", "2026-10-03"), ("2026-10-01", None)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.build([], start=start, end=end)

    def test_all_four_classification_filters_apply_to_saved_ids(self) -> None:
        for key, matching, other in (
            ("project_id", "p1", "p2"),
            ("subject_id", "s1", "s2"),
            ("material_id", "m1", "m2"),
            ("unit_id", "u1", "u2"),
        ):
            with self.subTest(key=key):
                payload = self.build([session(), session(**{key: other})], filters={key: matching})
                self.assertEqual(payload["total_sessions"], 1)
                self.assertEqual(payload["total_sec"], 3600)
        payload = self.build([session(), session(material_id="m2")], filters={
            "project_id": "p1", "subject_id": "s1", "material_id": "m1", "unit_id": "u1",
        })
        self.assertEqual(payload["total_sessions"], 1)

    def test_equal_names_and_equal_child_ids_do_not_merge_different_hierarchies(self) -> None:
        records = [
            session(),
            session(project_id="p2"),
            session(subject_id="s2"),
            session(material_id="m2"),
            session(unit_id="u2"),
        ]
        payload = self.build(records)
        self.assertEqual(len(payload["subjects"]), 3)
        self.assertEqual(len(payload["materials"]), 4)
        self.assertEqual(len(payload["nodes"]), 5)
        self.assertEqual({node["label"] for node in payload["nodes"]}, {"同じ単元"})
        self.assertEqual(sum(node["total_sec"] for node in payload["nodes"]), payload["total_sec"])
        for node in payload["nodes"]:
            self.assertRegex(node["id"], r"^unit_[0-9a-f]{24}$")
        reordered = self.build(list(reversed(records)))
        self.assertEqual([row["id"] for row in payload["nodes"]], [row["id"] for row in reordered["nodes"]])
        self.master.data["unit_id"]["u1"] = "名称変更後"
        renamed = self.build(records)
        self.assertEqual([row["id"] for row in payload["nodes"]], [row["id"] for row in renamed["nodes"]])

    def test_unset_and_removed_master_ids_remain_separate(self) -> None:
        payload = self.build([
            session(project_id="", subject_id=None, material_id="", unit_id=""),
            session(project_id="gone-p", subject_id="gone-s", material_id="gone-m", unit_id="gone-u"),
        ])
        self.assertEqual(len(payload["nodes"]), 2)
        nodes = {node["label"]: node for node in payload["nodes"]}
        self.assertEqual(nodes["単元未設定"]["subject_name"], "科目未設定")
        self.assertEqual(nodes["単元未設定"]["material_name"], "教材未設定")
        self.assertEqual(nodes["不明な単元"]["subject_name"], "不明な科目")

    def test_zero_is_valid_but_negative_nonfinite_and_missing_duration_are_excluded(self) -> None:
        records = [session(duration_sec=value) for value in (0, 0.25, -1, "bad", None, float("nan"), float("inf"), -float("inf"), True)]
        payload = self.build(records)
        self.assertEqual(payload["total_sec"], 0.25)
        self.assertEqual(payload["total_sessions"], 2)
        self.assertEqual(payload["excluded"], {"invalid_date": 0, "invalid_duration": 7})
        self.assertEqual(payload["nodes"][0]["history_total"], 2)
        json.dumps(payload, allow_nan=False)
        zero = self.build([session(duration_sec=0)])
        self.assertEqual(len(zero["nodes"]), 1)
        self.assertEqual(zero["nodes"][0]["total_sec"], 0)

    def test_finite_extreme_values_do_not_overflow_json_totals(self) -> None:
        payload = self.build([session(duration_sec=1e308), session(duration_sec=1e308)])
        self.assertEqual(payload["total_sessions"], 1)
        self.assertEqual(payload["excluded"]["invalid_duration"], 1)
        json.dumps(payload, allow_nan=False)

    def test_invalid_dates_are_excluded_without_epoch_or_prefix_interpretation(self) -> None:
        records = [session(start_at=value) for value in ("bad", "2026-10-01garbage", "2026-02-30", 1234)]
        records += [session(start_at="bad", end_at="2026-10-01T11:00:00")]
        records += [session(start_at="", end_at="2026-10-02T08:00:00")]
        payload = self.build(records)
        self.assertEqual(payload["excluded"]["invalid_date"], 5)
        self.assertEqual(payload["total_sessions"], 1)
        self.assertEqual(payload["nodes"][0]["daily"], [0, 3600, 0])

    def test_aware_timestamps_use_existing_local_analysis_date(self) -> None:
        utc = datetime(2026, 10, 1, 23, 30, tzinfo=timezone.utc)
        other_offset = utc.astimezone(timezone(timedelta(hours=9)))
        expected_day = parse_analysis_date(utc)
        payload = self.build([
            session(start_at=utc), session(start_at=other_offset.isoformat()),
            session(start_at=utc.isoformat().replace("+00:00", "Z")),
        ], start=expected_day, end=expected_day)
        self.assertEqual(payload["nodes"][0]["daily"], [10800])
        self.assertEqual({row["day"] for row in payload["nodes"][0]["history"]}, {expected_day.isoformat()})

    def test_exclusions_respect_filters_and_outside_range(self) -> None:
        payload = self.build([
            session(subject_id="s2", start_at="bad"),
            session(start_at="2026-09-30", duration_sec="bad"),
            session(start_at="bad"),
            session(duration_sec="bad"),
            session(),
        ], filters={"subject_id": "s1"})
        self.assertEqual(payload["excluded"], {"invalid_date": 1, "invalid_duration": 1})
        self.assertEqual(payload["total_sessions"], 1)

    def test_no_top_fifteen_unit_or_day_cutoff(self) -> None:
        records = [session(unit_id=f"unit-{day}", start_at=f"2026-10-{day:02d}") for day in range(1, 25)]
        payload = self.build(records, end="2026-10-31")
        self.assertEqual(len(payload["nodes"]), 24)
        self.assertEqual(len(payload["days"]), 31)
        self.assertEqual(payload["total_sec"], 24 * 3600)

    def test_history_is_latest_thirty_with_total_and_memo_limit(self) -> None:
        records = [session(session_id=f"session-{hour}", start_at=datetime(2026, 10, 1) + timedelta(hours=hour), memo="あ" * 1200) for hour in range(40)]
        payload = self.build(records)
        node = payload["nodes"][0]
        self.assertEqual(node["history_total"], 40)
        self.assertEqual(node["session_count"], 40)
        self.assertEqual(len(node["history"]), 30)
        self.assertEqual(node["history"][0]["start_at"], "2026-10-02T15:00:00")
        self.assertEqual(node["history"][-1]["start_at"], "2026-10-01T10:00:00")
        self.assertTrue(all(len(row["memo"]) == 1000 for row in node["history"]))

    def test_saved_note_links_work_in_both_directions_and_aliases(self) -> None:
        records = [
            session(session_id="one", linked_note_ids=["n1"]),
            session(session_id="two", unit_id="u2", notebook_page_id="page-alias"),
            session(session_id="three", unit_id="u3", note_id="n3"),
        ]
        notes = [
            {"note_id": "n1", "title": "セッションから", "unit_id": "u2"},
            {"note_id": "n2", "id": "page-alias", "title": "旧ページID"},
            {"note_id": "n3", "title": "旧ノートID"},
            {"note_id": "n4", "title": "ノートから共有", "linked_study_session_ids": ["one", "two"]},
            {"note_id": "n5", "title": "互換リンク", "linked_session_ids": ["three"]},
        ]
        payload = self.build(records, notes=notes)
        by_count = {node["label"]: {note["title"] for note in node["notes"]} for node in payload["nodes"]}
        # u1/u2 intentionally share names; inspect history to identify nodes.
        first = next(node for node in payload["nodes"] if any(note["title"] == "セッションから" for note in node["notes"]))
        second = next(node for node in payload["nodes"] if any(note["title"] == "旧ページID" for note in node["notes"]))
        third = next(node for node in payload["nodes"] if node["label"] == "不明な単元")
        self.assertEqual({note["title"] for note in first["notes"]}, {"セッションから", "ノートから共有"})
        self.assertEqual({note["title"] for note in second["notes"]}, {"旧ページID", "ノートから共有"})
        self.assertEqual({note["title"] for note in third["notes"]}, {"旧ノートID", "互換リンク"})
        self.assertTrue(by_count)

    def test_classification_notes_match_all_nonempty_ids_without_inferred_links(self) -> None:
        payload = self.build([
            session(), session(session_id="two", unit_id="u2"),
            session(session_id="three", material_id="m2", unit_id="u3"),
        ], notes=[
            {"note_id": "material", "title": "教材メモ", "project_id": "p1", "material_id": "m1"},
            {"note_id": "unit", "title": "単元メモ", "unit_id": "u2"},
            {"note_id": "wrong", "title": "別プロジェクト", "project_id": "p2", "unit_id": "u1"},
            {"note_id": "none", "title": "関連未設定"},
            {"note_id": "outside", "title": "対象外セッション", "material_id": "m1", "linked_study_session_ids": ["outside-period"]},
        ])
        self.assertEqual(sum(node["note_count"] for node in payload["nodes"]), 3)
        titles = [note["title"] for node in payload["nodes"] for note in node["notes"]]
        self.assertEqual(titles.count("教材メモ"), 2)
        self.assertEqual(titles.count("単元メモ"), 1)
        self.assertNotIn("別プロジェクト", titles)
        self.assertNotIn("関連未設定", titles)
        self.assertNotIn("対象外セッション", titles)

    def test_note_previews_are_latest_five_deduplicated_with_excerpt_limit(self) -> None:
        notes = [{"note_id": f"n{i}", "title": f"ノート{i}", "unit_id": "u1", "updated_at": f"2026-10-{i + 1:02d}", "body": "文" * 900} for i in range(8)]
        notes += [{"note_id": "n7", "title": "古い同一ノート", "unit_id": "u1", "updated_at": "2026-10-01", "content": "旧内容"}]
        node = self.build([session()], notes=notes)["nodes"][0]
        self.assertEqual(node["note_count"], 8)
        self.assertEqual(len(node["notes"]), 5)
        self.assertEqual([note["title"] for note in node["notes"]], [f"ノート{i}" for i in range(7, 2, -1)])
        self.assertTrue(all(len(note["excerpt"]) == 800 for note in node["notes"]))

    def test_inputs_are_not_mutated_and_output_does_not_alias_inputs(self) -> None:
        records = [session(linked_note_ids=["n1"], condition={"io_type": "インプット"}, _source_file="private.json")]
        notes = [{"note_id": "n1", "title": "ノート", "body": "本文", "linked_study_session_ids": ["session-1"]}]
        filters = {"project_id": "p1"}
        before = deepcopy((records, notes, filters, self.master.data))
        payload = self.build(records, filters=filters, notes=notes)
        self.assertEqual((records, notes, filters, self.master.data), before)
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        self.assertNotIn("private.json", encoded)
        payload["nodes"][0]["history"][0]["memo"] = "変更"
        payload["nodes"][0]["notes"][0]["title"] = "変更"
        self.assertEqual((records, notes, filters, self.master.data), before)

    def test_empty_payload_has_no_artificial_nodes(self) -> None:
        payload = self.build([], notes=[{"title": "単独ノート", "unit_id": "u1"}])
        self.assertEqual(payload["total_sec"], 0)
        self.assertEqual(payload["total_sessions"], 0)
        for key in ("subjects", "materials", "nodes"):
            self.assertEqual(payload[key], [])
        self.assertEqual(payload["excluded"], {"invalid_date": 0, "invalid_duration": 0})


if __name__ == "__main__":
    unittest.main()
