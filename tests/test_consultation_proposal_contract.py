from __future__ import annotations

from copy import deepcopy
import json
import unittest

from app_core import consultation_proposal_contract as contract


class ProposalContractTests(unittest.TestCase):
    def payload(self, **changes):
        result = {"schema_version": 1, "source_label": "週次の相談", "source_report_id": "a" * 64,
                  "proposals": [{"title": "統計の復習", "text": "問題5から再開する", "reason": "昨日は4まで",
                                 "target": {"kind": "next_step", "material_id": "material_1"}}]}
        result.update(changes)
        return result

    def parse(self, payload=None):
        return contract.parse_proposal_payload(json.dumps(payload or self.payload(), ensure_ascii=False))

    def entry(self):
        candidate = self.parse()[0]
        return {**candidate, "status": "pending", "created_at": "2026-09-16T12:00:00+09:00",
                "updated_at": "2026-09-16T12:00:00+09:00"}

    def inbox(self, entry=None):
        entry = entry or self.entry()
        return {"schema_version": 1, "proposals": {entry["proposal_id"]: entry}}

    def test_utf8_bom_and_single_fence_have_identical_ids(self):
        text = json.dumps(self.payload(), ensure_ascii=False)
        expected = self.parse()
        for raw in (text, ("\ufeff" + text).encode("utf-8"), "```json\n" + text + "\n```", "```\n" + text + "\n```"):
            self.assertEqual(expected, contract.parse_proposal_payload(raw))

    def test_missing_optional_fields_are_normalized_and_title_text_preserved(self):
        candidate = self.parse({"schema_version": 1, "proposals": [{"title": "普通の相談", "text": "  =1+1\n<script>text</script>  "}]})[0]
        self.assertEqual(candidate["source"], {"report_id": "", "label": ""})
        self.assertEqual(candidate["proposal"]["target"], {"kind": "reference", "material_id": "", "target_date": ""})
        self.assertEqual(candidate["proposal"]["text"], "  =1+1\n<script>text</script>  ")

    def test_duplicate_entries_collapse_and_order_does_not_change_identity(self):
        payload = self.payload()
        payload["proposals"] *= 2
        self.assertEqual(len(self.parse(payload)), 1)
        changed_order = json.dumps(payload, sort_keys=True)
        self.assertEqual(self.parse(payload), contract.parse_proposal_payload(changed_order))

    def test_source_and_content_participate_in_identity(self):
        first = self.parse()[0]["proposal_id"]
        for change in ("source_label", "text", "target"):
            payload = self.payload()
            if change == "source_label":
                payload[change] = "違う会話"
            elif change == "text":
                payload["proposals"][0]["text"] = "問題6"
            else:
                payload["proposals"][0]["target"]["material_id"] = "material_2"
            self.assertNotEqual(first, self.parse(payload)[0]["proposal_id"])

    def test_unknown_input_fields_are_never_accepted(self):
        for level in ("top", "proposal", "target"):
            payload = self.payload()
            node = payload if level == "top" else payload["proposals"][0]
            if level == "target":
                node = node["target"]
            node["execute"] = "delete records"
            with self.subTest(level=level), self.assertRaises(contract.ProposalError):
                self.parse(payload)

    def test_no_code_or_arbitrary_file_format_is_parsed(self):
        for raw in ("__import__('os').system('ignored')", "title: YAML", "['python']", "null", "{}",
                    "```json\n{}\n```\nmore text", "```python\n{}\n```", b"\xff", "{not JSON}"):
            with self.subTest(raw=raw), self.assertRaises(contract.ProposalError):
                contract.parse_proposal_payload(raw)

    def test_duplicate_keys_nonfinite_and_deep_json_rejected(self):
        for raw in ('{"schema_version":1,"schema_version":1}', '{"x":NaN}', '{"x":1e999}', '[' * 2000 + ']' * 2000):
            with self.subTest(raw=raw[:50]), self.assertRaises(contract.ProposalError):
                contract.parse_proposal_payload(raw)

    def test_size_count_and_string_limits(self):
        with self.assertRaises(contract.ProposalError):
            contract.parse_proposal_payload(" " * (contract.MAX_IMPORT_BYTES + 1))
        for count in (0, 51):
            with self.subTest(count=count), self.assertRaises(contract.ProposalError):
                self.parse(self.payload(proposals=self.payload()["proposals"] * count))
        for key, limit in (("title", 200), ("text", 10000), ("reason", 2000)):
            payload = self.payload()
            payload["proposals"][0][key] = "a" * (limit + 1)
            with self.subTest(key=key), self.assertRaises(contract.ProposalError):
                self.parse(payload)

    def test_schema_and_required_values_are_strict(self):
        for schema in (True, "1", 2, None):
            with self.subTest(schema=schema), self.assertRaises(contract.ProposalError):
                self.parse(self.payload(schema_version=schema))
        for field in ("title", "text"):
            for value in ("", "   ", None, 123, [], {}):
                payload = self.payload()
                payload["proposals"][0][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(contract.ProposalError):
                    self.parse(payload)

    def test_target_hints_cannot_be_commands_or_paths_to_update(self):
        for target in ({"kind": "delete"}, {"kind": []}, {"kind": "reference", "material_id": "m"},
                       {"kind": "next_step", "target_date": "2026-09-17"}, {"kind": "daily_memo", "target_date": "../../settings"},
                       {"kind": "daily_memo", "target_date": "20260917"}, {"kind": "daily_memo", "target_date": "2026-02-30"}):
            payload = self.payload()
            payload["proposals"][0]["target"] = target
            with self.subTest(target=target), self.assertRaises(contract.ProposalError):
                self.parse(payload)

    def test_invalid_report_ids_and_control_characters_rejected(self):
        for source in ("https://docs.google.com/document/d/id/edit", "../data", "A" * 64, [], None):
            with self.subTest(source=source), self.assertRaises(contract.ProposalError):
                self.parse(self.payload(source_report_id=source))
        for text in ("null\x00byte", "bad\x1bescape", "bad\ud800"):
            payload = self.payload()
            payload["proposals"][0]["text"] = text
            with self.assertRaises(contract.ProposalError):
                contract.parse_proposal_payload(json.dumps(payload))

    def test_stored_unknown_fields_are_preserved_and_not_in_hash(self):
        data = self.inbox()
        data["future"] = {"keep": True}
        entry = next(iter(data["proposals"].values()))
        entry["future"] = [1]
        entry["source"]["future"] = 2
        entry["proposal"]["future"] = 3
        entry["proposal"]["target"]["future"] = 4
        saved = contract.validate_inbox(data)
        self.assertEqual(saved, data)
        saved["future"]["keep"] = False
        self.assertTrue(data["future"]["keep"])

    def test_modified_immutable_proposal_or_id_is_rejected(self):
        entry = self.entry()
        entry["proposal"]["text"] = "改変"
        with self.assertRaises(contract.ProposalError):
            contract.validate_inbox(self.inbox(entry))
        data = self.inbox()
        data["proposals"]["wrong"] = data["proposals"].pop(next(iter(data["proposals"])))
        with self.assertRaises(contract.ProposalError):
            contract.validate_inbox(data)

    def test_invalid_states_and_dates_are_rejected(self):
        for changes in ({"status": "unknown"}, {"status": []}, {"updated_at": "2026-09-15T12:00:00+09:00"},
                        {"created_at": "2026-09-16"}, {"status": "applied"}, {"status": "reverted"}):
            entry = self.entry()
            entry.update(changes)
            with self.subTest(changes=changes), self.assertRaises(contract.ProposalError):
                contract.validate_inbox(self.inbox(entry))

    def test_application_target_is_closed_and_undo_needs_history(self):
        entry = self.entry()
        entry.update(status="applied", application={"operation_id": "1" * 32, "kind": "next_step",
                     "target_path": "settings/learning_context.json", "material_id": "m", "target_date": "",
                     "applied_at": "2026-09-16T12:00:00+09:00", "after_revision": "a" * 64,
                     "before": {"file_existed": False, "field_present": False, "value": None}, "applied_value": "next"})
        contract.validate_inbox(self.inbox(entry))
        for path in ("settings/user_settings.json", "../data.json", "C:/data.json"):
            invalid = deepcopy(entry)
            invalid["application"]["target_path"] = path
            with self.assertRaises(contract.ProposalError):
                contract.validate_inbox(self.inbox(invalid))
        entry["status"] = "reverted"
        with self.assertRaises(contract.ProposalError):
            contract.validate_inbox(self.inbox(entry))

    def test_request_template_contains_parseable_example_and_no_automatic_write(self):
        template = contract.proposal_request_template("f" * 64, "週次")
        candidates = contract.parse_proposal_payload(template[template.index('{'):])
        self.assertEqual(candidates[0]["source"]["report_id"], "f" * 64)
        self.assertIn("直接書き込まない", template)

    def test_deep_unknown_stored_fields_fail_with_a_displayable_error(self):
        nested = []
        for _ in range(600):
            nested = [nested]
        for location in ("top", "entry"):
            data = self.inbox()
            target = data if location == "top" else next(iter(data["proposals"].values()))
            target["future"] = nested
            with self.subTest(location=location), self.assertRaises(contract.ProposalError):
                contract.validate_inbox(data)

    def test_existing_plan_memos_follow_the_same_strict_contract(self):
        good = {"proposal_id": "proposal_" + "a" * 64, "title": "", "text": "plan",
                "adopted_at": "2026-09-16T10:00:00", "future": [1]}
        self.assertEqual(contract.validate_plan_memos([good]), [good])
        for field, value in (("proposal_id", "bad"), ("title", []), ("text", "\x00"), ("adopted_at", "not a date")):
            invalid = {**good, field: value}
            with self.subTest(field=field), self.assertRaises(contract.ProposalError):
                contract.validate_plan_memos([invalid])
        with self.assertRaises(contract.ProposalError):
            contract.validate_plan_memos([good, good])


if __name__ == "__main__":
    unittest.main()
