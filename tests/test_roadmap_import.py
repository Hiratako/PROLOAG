from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from app_core import json_store
from app_core import roadmap_import as service
from app_core.roadmap_service import load_roadmap, save_entry


def item(ref="E01", **updates):
    value = dict(
        ref=ref, title="架空教材｜問題1〜40・初回演習", start_date="2026-11-09",
        end_date="2026-11-13", deadline=None, planned_minutes=300,
        status="not_started", progress_kind="quantity", completed=0, total=40,
        unit="問", classification=dict(project=None, subject=None, material=None, unit=None),
        predecessor_refs=[], memo="架空データ。各問の解答・解説確認で完了。",
    )
    value.update(updates)
    return value


def document(**updates):
    value = dict(format=service.FORMAT, schema_version=1, plan_title="架空の計画",
                 items=[item()], milestones=[])
    value.update(updates)
    return value


def milestone(**updates):
    value = dict(ref="M01", title="架空模試", date="2026-11-15", project=None, memo="架空データ")
    value.update(updates)
    return value


def master():
    return dict(
        projects=[dict(project_id="p1", name="編入", is_active=True)],
        subjects=[dict(subject_id="s1", project_id="p1", name="生物", is_active=True)],
        materials=[dict(material_id="m1", project_id="p1", subject_id="s1", name="教材A", is_active=True)],
        units=[dict(unit_id="u1", project_id="p1", subject_id="s1", material_id="m1", name="細胞", is_active=True)],
        tags=[],
    )


class RoadmapImportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="pgrimp_")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        mock = patch.object(json_store, "PROJECT_ROOT", self.root)
        mock.start()
        self.addCleanup(mock.stop)

    def put(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*.json") if "backups" not in path.parts}

    def preview(self, doc=None, **kwargs):
        return service.build_import_preview(self.root, document() if doc is None else doc, **kwargs)

    def imported(self, doc=None, **kwargs):
        preview = self.preview(doc, **kwargs)
        self.assertTrue(preview["can_commit"], preview["issues"])
        result = service.apply_import_preview(self.root, preview)
        return preview, result

    def test_valid_utf8_bom_and_full_schema_round_trip(self):
        value = document(items=[item(), item("E02", title="架空教材｜見直し", start_date="2026-11-14", end_date="2026-11-15", predecessor_refs=["E01"], planned_minutes=120)], milestones=[milestone()])
        raw = b"\xef\xbb\xbf" + json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.assertEqual(service.parse_import_file(raw), value)

    def test_bad_json_encodings_duplicate_keys_and_nonfinite_rejected(self):
        cases = [b"", b"{}{}", b"```json\n{}\n```", b'{"format":1,"format":2}',
                 b'{"a":NaN}', b"\xff\xfe", b"[" * 2000 + b"]" * 2000,
                 b"a" * (service.MAX_FILE_BYTES + 1)]
        for raw in cases:
            with self.subTest(raw=raw[:50]), self.assertRaises(ValueError):
                service.parse_import_file(raw)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_unknown_root_item_and_nested_keys_rejected(self):
        cases = []
        for kind in ("root_missing", "root_extra", "item_missing", "item_extra", "classification_extra"):
            value = document()
            if kind == "root_missing": del value["items"]
            elif kind == "root_extra": value["calendar"] = {}
            elif kind == "item_missing": del value["items"][0]["memo"]
            elif kind == "item_extra": value["items"][0]["id"] = "rm_untrusted"
            else: value["items"][0]["classification"]["subject_id"] = "s1"
            cases.append(value)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                service.validate_import_document(value)

    def test_invalid_unicode_keys_refs_and_names_have_renderable_diagnostics(self):
        cases = []
        value = document()
        value["\ud800"] = "extra key"
        cases.append(value)
        value = document()
        value["items"][0]["\udfff"] = "extra item key"
        cases.append(value)
        value = document()
        value["items"][0]["ref"] = "\ud800"
        del value["items"][0]["memo"]
        cases.append(value)
        value = document()
        value["items"][0]["classification"]["project"] = "\udfff"
        cases.append(value)
        value = document(milestones=[milestone(ref="\ud800")])
        del value["milestones"][0]["memo"]
        cases.append(value)
        cases.append(document(plan_title="\ud800"))
        for value in cases:
            raw = json.dumps(value, ensure_ascii=True).encode("utf-8")
            with self.subTest(raw=raw[-100:]), self.assertRaises(ValueError) as raised:
                service.parse_import_file(raw)
            self.assertTrue(str(raised.exception).encode("utf-8"))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_type_range_status_and_progress_contracts(self):
        cases = [dict(planned_minutes=True), dict(planned_minutes=1.5), dict(planned_minutes=-1),
                 dict(planned_minutes=600001), dict(total=0), dict(total=True), dict(total=1e100),
                 dict(total=float("inf")), dict(completed=float("nan")), dict(completed=41),
                 dict(completed=True), dict(status="未着手"), dict(status=[]), dict(status="done"),
                 dict(progress_kind="percent", total=40), dict(progress_kind="percent", total=100, unit="問"),
                 dict(title=""), dict(title="前後空白 "), dict(unit="x"*21), dict(memo="x"*20001),
                 dict(deadline=""), dict(deadline="2026-11-12"), dict(start_date="2026-11-14"),
                 dict(start_date="2026-2-01"), dict(end_date="2200-01-01"), dict(ref="rm_unsafe")]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                service.validate_import_document(document(items=[item(**changes)]))

    def test_empty_and_oversized_collections_and_duplicate_titles_rejected(self):
        for value in (document(items=[]), document(schema_version=True), document(items=[item()] * 501),
                      document(milestones=[milestone()] * 101), document(items=[item(), item("E02")]),
                      document(milestones=[milestone(), milestone(ref="M02")])):
            with self.subTest(value=str(value)[:80]), self.assertRaises(ValueError):
                service.validate_import_document(value)

    def test_dependency_missing_self_duplicate_cycle_and_same_day_rejected(self):
        cases = [document(items=[item(predecessor_refs=["E99"])]),
                 document(items=[item(predecessor_refs=["E01"])]),
                 document(items=[item(predecessor_refs=["E02", "E02"])]),
                 document(items=[item(predecessor_refs=["E02"]), item("E02", title="Second", predecessor_refs=["E01"])]),
                 document(items=[item(), item("E02", title="Second", predecessor_refs=["E01"], start_date="2026-11-13")])]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                service.validate_import_document(value)

    def test_parse_preview_and_context_are_read_only_even_with_unresolved_mapping(self):
        value = document(items=[item(classification=dict(project="不明", subject=None, material=None, unit=None))])
        service.parse_import_file(json.dumps(value).encode())
        context = service.load_import_context(self.root)
        self.assertEqual(context["roadmap"]["items"], [])
        preview = self.preview(value)
        self.assertFalse(preview["can_commit"])
        self.assertTrue(any("対応先" in issue["message"] for issue in preview["issues"]))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_exact_hierarchy_auto_mapping_and_grouping(self):
        self.put(service.MASTER_PATH, master())
        names = dict(project="編入", subject="生物", material="教材A", unit="細胞")
        value = document(items=[item(classification=names), item("E02", title="Second", classification=names)])
        self.assertEqual(len(service.classification_requests(value)), 1)
        preview, _ = self.imported(value)
        row = preview["additions"]["items"][0]
        self.assertEqual([row[key] for key in service.CLASS_KEYS], ["p1", "s1", "m1", "u1"])

    def test_ambiguous_name_requires_explicit_mapping_and_wrong_parent_rejected(self):
        value_master = master()
        value_master["projects"].append(dict(project_id="p2", name="編入", is_active=True))
        self.put(service.MASTER_PATH, value_master)
        names = dict(project="編入", subject=None, material=None, unit=None)
        value = document(items=[item(classification=names)])
        request = service.classification_requests(value)[0]
        self.assertIsNone(service.auto_classification_mapping(names, value_master))
        self.assertFalse(self.preview(value)["can_commit"])
        mapping = dict(project_id="p2", subject_id="s1", material_id="", unit_id="")
        self.assertFalse(self.preview(value, mappings={request["key"]: mapping})["can_commit"])
        mapping["subject_id"] = ""
        self.assertTrue(self.preview(value, mappings={request["key"]: mapping})["can_commit"])

    def test_explicit_unassigned_is_allowed_but_unknown_and_inactive_ids_are_not(self):
        value_master = master()
        value_master["projects"][0]["is_active"] = False
        self.put(service.MASTER_PATH, value_master)
        value = document(items=[item(classification=dict(project="編入", subject=None, material=None, unit=None))])
        key = service.classification_requests(value)[0]["key"]
        mapping = {name: "" for name in service.CLASS_KEYS}
        self.assertTrue(self.preview(value, mappings={key: mapping})["can_commit"])
        for identifier in ("p1", "missing"):
            self.assertFalse(self.preview(value, mappings={key: {**mapping, "project_id": identifier}})["can_commit"])

    def test_shared_project_path_maps_item_hierarchy_and_milestone_project_only(self):
        self.put(service.MASTER_PATH, master())
        names = dict(project="編入", subject=None, material=None, unit=None)
        value = document(items=[item(classification=names)], milestones=[milestone(project="編入")])
        requests = service.classification_requests(value)
        self.assertEqual(len(requests), 1)
        mappings = {requests[0]["key"]: dict(project_id="p1", subject_id="s1", material_id="m1", unit_id="u1")}
        preview, _ = self.imported(value, mappings=mappings)
        self.assertEqual(preview["additions"]["items"][0]["unit_id"], "u1")
        self.assertEqual(preview["additions"]["milestones"][0]["project_id"], "p1")
        self.assertNotIn("subject_id", preview["additions"]["milestones"][0])

    def test_malformed_master_does_not_get_normalized_or_rewritten(self):
        bad_cases = [None, [], {"projects": [{"name": "No ID"}]},
                     {"projects": [{"project_id": "p", "name": "P"}, {"project_id": "p", "name": "P2"}]},
                     {"projects": [{"project_id": "p", "name": "P", "is_active": "yes"}]}]
        for value in bad_cases:
            path = self.put(service.MASTER_PATH, value)
            before = path.read_bytes()
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.preview()
            self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.root/service.ROADMAP_PATH).exists())

    def test_import_generates_ids_baselines_predecessors_and_history(self):
        value = document(items=[item(), item("E02", title="Second", start_date="2026-11-14", end_date="2026-11-15", predecessor_refs=["E01"], planned_minutes=120)], milestones=[milestone()])
        preview, result = self.imported(value)
        self.assertEqual(result["items"], 2)
        self.assertEqual(result["milestones"], 1)
        self.assertEqual(set(result["changed_files"]), {service.ROADMAP_PATH, service.LEDGER_PATH})
        rows = load_roadmap(self.root)["items"]
        self.assertRegex(rows[0]["id"], r"^rm_[0-9a-f]{32}$")
        self.assertEqual(rows[1]["predecessor_ids"], [rows[0]["id"]])
        self.assertEqual(rows[0]["baseline_start_date"], "2026-11-09")
        self.assertEqual(rows[0]["schedule_history"], [])
        history = service.import_history(self.root)
        self.assertEqual(history[0]["operation_id"], preview["operation_id"])
        self.assertEqual(history[0]["planned_minutes"], 420)
        journal = self.root/"backups"/"json_operations"/preview["operation_id"]/"transaction.json"
        self.assertEqual(json.loads(journal.read_text(encoding="utf-8"))["status"], "committed")

    def test_append_preserves_existing_data_unknown_fields_and_other_records(self):
        old_doc = document(items=[item(title="Existing")])
        self.imported(old_doc)
        current = load_roadmap(self.root)
        current["future_property"] = {"keep": True}
        current["calendar"] = {"weekly_minutes": [90]*7, "overrides": {"2026-11-20": 0}}
        current["items"][0]["future_field"] = {"also": "keep"}
        self.put(service.ROADMAP_PATH, current)
        self.put("daily_plans/2026/2026-11-09.json", {"synthetic": "untouched"})
        self.put("study_logs/synthetic.json", [{"synthetic": "untouched"}])
        others = {path: data for path, data in self.snapshot().items() if not path.startswith("settings")}
        self.imported()
        changed = load_roadmap(self.root)
        self.assertEqual(changed["items"][0], current["items"][0])
        self.assertEqual(changed["calendar"], current["calendar"])
        self.assertEqual(changed["future_property"], current["future_property"])
        for path, data in others.items(): self.assertEqual((self.root/path).read_bytes(), data)

    def test_same_preview_retry_is_idempotent_even_after_later_edit(self):
        preview, _ = self.imported()
        current = load_roadmap(self.root)
        current["items"][0]["memo"] = "Later edit"
        self.put(service.ROADMAP_PATH, current)
        before = self.snapshot()
        result = service.apply_import_preview(self.root, preview)
        self.assertTrue(result["replayed"])
        self.assertEqual(self.snapshot(), before)

    def test_semantic_duplicate_ignores_json_order_item_order_refs_and_plan_title(self):
        value = document(items=[item(), item("E02", title="Second", start_date="2026-11-14", end_date="2026-11-15", predecessor_refs=["E01"])], milestones=[milestone()])
        self.imported(value)
        changed = deepcopy(value)
        changed["plan_title"] = "Renamed plan"
        changed["items"][0]["ref"] = "E77"
        changed["items"][1]["ref"] = "E88"
        changed["items"][1]["predecessor_refs"] = ["E77"]
        changed["items"].reverse()
        changed["milestones"][0]["ref"] = "M99"
        changed = json.loads(json.dumps(changed, sort_keys=True))
        preview = self.preview(changed)
        self.assertEqual(preview["counts"], dict(items=0, milestones=0, existing=3))
        self.assertFalse(preview["can_commit"])
        before = self.snapshot()
        with self.assertRaises(ValueError): service.apply_import_preview(self.root, preview)
        self.assertEqual(self.snapshot(), before)

    def test_partial_overlap_links_new_child_to_previously_imported_parent(self):
        self.imported()
        parent = load_roadmap(self.root)["items"][0]
        value = document(items=[item(), item("E02", title="Second", start_date="2026-11-14", end_date="2026-11-15", predecessor_refs=["E01"])])
        preview, result = self.imported(value)
        self.assertEqual(result["existing"], 1)
        self.assertEqual(result["items"], 1)
        self.assertEqual(load_roadmap(self.root)["items"][1]["predecessor_ids"], [parent["id"]])

    def test_numeric_spelling_does_not_reimport_same_values(self):
        self.imported()
        value = document(items=[item(completed=0.0, total=40.0)])
        preview = self.preview(value)
        self.assertEqual(preview["counts"], dict(items=0, milestones=0, existing=1))
        self.assertFalse(preview["can_commit"])

    def test_maximum_length_dependency_chain_is_supported(self):
        from datetime import date, timedelta

        start = date(2026, 1, 1)
        rows = []
        for number in range(1, 501):
            target = (start + timedelta(days=number)).isoformat()
            rows.append(item(f"E{number:03}", title=f"Stage {number}", start_date=target,
                             end_date=target, predecessor_refs=[] if number == 1 else [f"E{number-1:03}"]))
        preview = self.preview(document(items=rows))
        self.assertTrue(preview["can_commit"], preview["issues"])
        self.assertEqual(preview["counts"]["items"], 500)

    def test_duplicate_stays_duplicate_after_edit_archive_or_delete(self):
        self.imported()
        current = load_roadmap(self.root)
        current["items"][0].update(title="Renamed by user", is_archived=True, memo="edited")
        self.put(service.ROADMAP_PATH, current)
        self.assertEqual(self.preview()["counts"]["existing"], 1)
        current["items"] = []
        self.put(service.ROADMAP_PATH, current)
        preview = self.preview()
        self.assertEqual(preview["counts"]["existing"], 1)
        self.assertIn("削除済み", preview["rows"][0]["reason"])
        self.assertFalse(preview["can_commit"])

    def test_deleted_duplicate_parent_blocks_new_dependent_not_independent_item(self):
        self.imported()
        current = load_roadmap(self.root)
        current["items"] = []
        self.put(service.ROADMAP_PATH, current)
        value = document(items=[item(), item("E02", title="Second", start_date="2026-11-14", end_date="2026-11-15", predecessor_refs=["E01"])])
        preview = self.preview(value)
        self.assertFalse(preview["can_commit"])
        self.assertTrue(any("削除" in entry["message"] for entry in preview["issues"]))
        value["items"][1]["predecessor_refs"] = []
        self.assertTrue(self.preview(value)["can_commit"])

    def test_live_duplicate_parent_date_changes_are_checked(self):
        self.imported()
        current = load_roadmap(self.root)
        current["items"][0]["end_date"] = "2026-11-20"
        self.put(service.ROADMAP_PATH, current)
        value = document(items=[item(), item("E02", title="Second", start_date="2026-11-14", end_date="2026-11-15", predecessor_refs=["E01"])])
        preview = self.preview(value)
        self.assertFalse(preview["can_commit"])
        self.assertTrue(any("現在保存" in entry["message"] for entry in preview["issues"]))

    def test_live_provenance_prevents_duplicates_if_ledger_is_missing(self):
        self.imported()
        (self.root/service.LEDGER_PATH).unlink()
        preview = self.preview()
        self.assertEqual(preview["counts"]["existing"], 1)
        self.assertFalse(preview["can_commit"])

    def test_similar_existing_title_requires_explicit_acknowledgement(self):
        self.imported()
        changed = document(items=[item(planned_minutes=450)])
        preview = self.preview(changed)
        self.assertFalse(preview["can_commit"])
        self.assertEqual(preview["similar_count"], 1)
        preview, _ = self.imported(changed, acknowledge_similar=True)
        self.assertEqual(len(load_roadmap(self.root)["items"]), 2)

    def test_preview_mutations_signature_loss_and_process_restart_are_rejected(self):
        preview = self.preview()
        for field, value in (("can_commit", False), ("mappings", {"invented": {}}),
                             ("acknowledge_similar", True), ("planned_minutes", 1)):
            changed = deepcopy(preview)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                service.apply_import_preview(self.root, changed)
        with patch.object(service, "_SECRET", b"another-process"):
            with self.assertRaises(ValueError): service.apply_import_preview(self.root, preview)
        self.assertFalse((self.root/service.ROADMAP_PATH).exists())

    def test_external_master_roadmap_and_ledger_changes_conflict_without_overwrite(self):
        for relative in (service.MASTER_PATH, service.ROADMAP_PATH, service.LEDGER_PATH):
            with self.subTest(relative=relative):
                child = self.root/relative.replace("/", "_")
                preview = service.build_import_preview(child, document())
                if relative == service.MASTER_PATH: new = master()
                elif relative == service.ROADMAP_PATH: new = {"schema_version":1,"items":[],"milestones":[],"allocations":[],"unknown":1}
                else: new = {"schema_version":1,"imports":[],"records":{},"unknown":1}
                path = child/relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(new), encoding="utf-8")
                before = path.read_bytes()
                with self.assertRaises(json_store.JsonConflictError):
                    service.apply_import_preview(child, preview)
                self.assertEqual(path.read_bytes(), before)

    def test_failed_second_write_rolls_back_first_record_and_keeps_retry_possible(self):
        self.imported(document(items=[item(title="Existing")]))
        preview = self.preview()
        before = self.snapshot()
        original = json_store.save_json_safe
        injected = []

        def save(path, value, **kwargs):
            if Path(path) == self.root/service.LEDGER_PATH and not injected:
                injected.append(True)
                raise OSError("synthetic write failure")
            return original(path, value, **kwargs)

        with patch.object(json_store, "save_json_safe", side_effect=save):
            with self.assertRaises(json_store.JsonStoreError):
                service.apply_import_preview(self.root, preview)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(service.apply_import_preview(self.root, preview)["items"])

    def test_corrupt_ledger_or_roadmap_is_not_overwritten(self):
        for relative, value in ((service.LEDGER_PATH, None), (service.LEDGER_PATH, {"schema_version":1,"imports":[],"records":{"bad":{}}}),
                                (service.ROADMAP_PATH, None)):
            path = self.put(relative, value)
            before = path.read_bytes()
            with self.subTest(relative=relative), self.assertRaises(ValueError): self.preview()
            self.assertEqual(path.read_bytes(), before)
            path.unlink()

    def test_malformed_ledger_collection_types_fail_as_read_errors(self):
        for bad_collection in ([], {}, True, 42, None):
            value = {"schema_version": 1, "imports": [], "records": {
                "0" * 64: {"id": "rm_" + "0" * 32,
                           "operation_id": "0" * 32, "collection": bad_collection}}}
            path = self.put(service.LEDGER_PATH, value)
            before = path.read_bytes()
            with self.subTest(collection=bad_collection), self.assertRaises(json_store.JsonReadError):
                service.load_import_context(self.root)
            with self.assertRaises(json_store.JsonReadError):
                service.import_history(self.root)
            self.assertEqual(path.read_bytes(), before)

    def test_milestone_only_and_nonzero_progress_are_supported(self):
        preview, result = self.imported(document(items=[], milestones=[milestone()]))
        self.assertEqual(result["items"], 0)
        self.assertEqual(result["milestones"], 1)
        self.assertEqual(preview["planned_minutes"], 0)
        self.imported(document(items=[item(status="in_progress", completed=12)]))
        self.assertEqual(load_roadmap(self.root)["items"][0]["completed"], 12)


if __name__ == "__main__":
    unittest.main()
