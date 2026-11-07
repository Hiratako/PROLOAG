"""Web-only two-substance sample, data provenance, and existing-visitor regressions.

All test fixtures are retained in a new OS temporary directory outside OneDrive.
The original local app, Windows distributions, and existing folders are not used
as writable fixtures or cleanup targets.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_IDS = {"caffeine", "acetaminophen"}
EXPECTED_NAMES = {"カフェイン", "アセトアミノフェン"}
LEGACY_NAMES = {"エスゾピクロン", "トラゾドン", "メラトニン"}
# Canonical 714-document map from the independently generated original sample:
# exclude the profile document and only diary.substance_records. Everything
# else, including caffeine and arbitrary diary fields, must stay unchanged.
NON_SUBSTANCE_BASELINE = "88a3f3d8734f41ba65784a927bf70121d379adb010b93a708beba12474aa9609"


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def source_manifest():
    return json.loads((ROOT / "web_demo/dataset_manifest.json").read_text(encoding="utf-8"))


def seed_documents():
    return {name: json.loads((ROOT / name).read_text(encoding="utf-8-sig"))
            for name in source_manifest()["record_sha256"]}


def _render_product(route, visitor):
    import streamlit as st
    from web_demo.app import make_pages
    from web_demo.catalog import render_product_view
    from web_demo.navigation import PAGES_STATE_KEY
    from web_demo.sandbox import SESSION_KEY, sandbox_context
    st.session_state[SESSION_KEY] = visitor
    with sandbox_context(visitor):
        pages = make_pages()
        st.session_state[PAGES_STATE_KEY] = pages
        st.navigation(list(pages.values()))
        render_product_view(route)


class WebSubstanceDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Do not follow a caller's TMP into a synchronized workspace.
        os_temp = (Path(os.environ["LOCALAPPDATA"]) / "Temp"
                   if os.name == "nt" else Path("/tmp"))
        if "onedrive" in str(os_temp.resolve()).casefold():
            raise RuntimeError("Substance verification must not use OneDrive.")
        cls.retained = Path(tempfile.mkdtemp(prefix="proloag-substances-", dir=os_temp))

    def setUp(self):
        self.before = {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in source_manifest()["record_sha256"]
        }

    def tearDown(self):
        after = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                 for name in source_manifest()["record_sha256"]}
        self.assertEqual(after, self.before, "Tests must not edit the shipped seed")

    def visitor(self):
        from web_demo.sandbox import create_sandbox
        return create_sandbox(base_dir=self.retained)

    def render(self, route, visitor, state=None):
        from streamlit import config as streamlit_config
        from streamlit.testing.v1 import AppTest
        streamlit_config.get_config_options(
            force_reparse=True,
            options_from_flags={"global.developmentMode": False},
        )
        app = AppTest.from_function(_render_product, args=(route, visitor), default_timeout=90)
        for name, value in (state or {}).items():
            app.session_state[name] = value
        app.run()
        self.healthy(app)
        return app

    def healthy(self, app):
        self.assertEqual(len(app.exception), 0, [x.message for x in app.exception])
        self.assertEqual(len(app.error), 0, [x.value for x in app.error])

    def widget(self, elements, label):
        matches = [item for item in elements if item.label == label]
        self.assertEqual(len(matches), 1, label)
        return matches[0]

    def assert_no_legacy_text(self, app):
        rendered = []
        for kind in ("markdown", "caption", "info", "warning", "error", "expander",
                     "selectbox", "multiselect", "text_input", "number_input"):
            for item in app.get(kind):
                rendered.extend(str(getattr(item, key, ""))
                                for key in ("value", "label", "options"))
        text = "\n".join(rendered)
        from web_demo.substances import LEGACY_DEMO_SUBSTANCE_IDS
        for legacy in {*LEGACY_DEMO_SUBSTANCE_IDS, *LEGACY_NAMES}:
            self.assertNotIn(legacy, text)

    def test_all_seed_records_are_valid_and_contain_only_two_sample_substances(self):
        from web_demo.data import verify_demo_dataset
        from web_demo.substances import LEGACY_DEMO_SUBSTANCE_IDS, WEB_SUBSTANCE_VARIANT
        manifest = source_manifest()
        self.assertEqual(verify_demo_dataset()["records"], 715)
        self.assertEqual(manifest["variant"], WEB_SUBSTANCE_VARIANT)
        documents = seed_documents()
        profiles = documents["settings/substance_profiles.json"]["profiles"]
        self.assertEqual({p["substance_id"] for p in profiles}, EXPECTED_IDS)
        self.assertEqual({p["name"] for p in profiles}, EXPECTED_NAMES)
        self.assertEqual(len(profiles), 2)
        for name, document in documents.items():
            text = json.dumps(document, ensure_ascii=False)
            for forbidden in {*LEGACY_DEMO_SUBSTANCE_IDS, *LEGACY_NAMES}:
                self.assertNotIn(forbidden, text, name)

    def test_all_non_substance_values_match_the_original_synthetic_sample(self):
        stripped = {}
        for name, document in seed_documents().items():
            if name == "settings/substance_profiles.json":
                continue
            document = deepcopy(document)
            if name.startswith("diary_entries/") and isinstance(document, dict):
                document.pop("substance_records", None)
            stripped[name] = document
        self.assertEqual(len(stripped), 714)
        self.assertEqual(hashlib.sha256(canonical(stripped)).hexdigest(), NON_SUBSTANCE_BASELINE)

    def test_acetaminophen_examples_are_sparse_and_use_matching_model_snapshots(self):
        from app_core.substance_service import model_snapshot_for_profile
        docs = seed_documents()
        profile = next(p for p in docs["settings/substance_profiles.json"]["profiles"]
                       if p["substance_id"] == "acetaminophen")
        acetaminophen = []
        caffeine = []
        for name, document in docs.items():
            if not name.startswith("diary_entries/"):
                continue
            caffeine.extend(document.get("caffeine_records", []))
            for record in document.get("substance_records", []):
                if record.get("substance_id") == "acetaminophen":
                    taken = datetime.fromisoformat(record["taken_at"])
                    self.assertEqual(taken.date().isoformat(), document["date"])
                    self.assertEqual((taken.hour, taken.minute), (14, 0))
                    self.assertEqual(record["amount_mg"], 300.0)
                    self.assertEqual(record["unit"], "mg")
                    self.assertEqual(record["model_snapshot"], model_snapshot_for_profile(profile))
                    acetaminophen.append(record)
        self.assertEqual(len(caffeine), 239)
        self.assertEqual(len(acetaminophen), 13)
        self.assertEqual(len({r["substance_record_id"] for r in acetaminophen}), 13)
        months = {}
        for record in acetaminophen:
            months[record["taken_at"][:7]] = months.get(record["taken_at"][:7], 0) + 1
        self.assertEqual(months, {**{f"2026-{month:02d}": 2 for month in range(5, 11)},
                                  "2026-11": 1})

    def test_preparation_is_idempotent_on_an_already_prepared_private_copy(self):
        from scripts.prepare_web_substance_demo import prepare_web_substance_demo
        target = self.retained / ("overlay-" + uuid4().hex)
        target.mkdir()
        for name in source_manifest()["record_sha256"]:
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((ROOT / name).read_bytes())
        (target / "web_demo").mkdir()
        (target / "web_demo/dataset_manifest.json").write_bytes(
            (ROOT / "web_demo/dataset_manifest.json").read_bytes())

        def inventory():
            return {path.relative_to(target).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in target.rglob("*") if path.is_file()}

        before = inventory()
        prepare_web_substance_demo(target)
        first = inventory()
        prepare_web_substance_demo(target)
        self.assertEqual(first, before)
        self.assertEqual(inventory(), first)

    def test_missing_profile_fallback_and_local_defaults_match_public_examples(self):
        import config
        from app_core import substance_service as service
        from web_demo.sandbox import sandbox_context
        visitor = self.visitor()
        missing = visitor.root / "settings/missing_profile_fixture.json"
        with sandbox_context(visitor), patch.object(service, "SUBSTANCE_PROFILES_PATH", missing):
            self.assertFalse(missing.exists())
            self.assertEqual({p["substance_id"] for p in service.load_substance_profiles()}, EXPECTED_IDS)
            with patch.object(config, "WEB_DEMO_EXPERIENCE", False):
                original = {p["substance_id"] for p in service.default_substance_profiles()}
                self.assertEqual(original, EXPECTED_IDS)
            self.assertFalse(missing.exists(), "Reading a missing profile must not write defaults")

    def test_fresh_display_record_and_profile_editor_show_only_two_defaults(self):
        visitor = self.visitor()
        condition = self.render("condition", visitor)
        self.assertEqual(set(self.widget(condition.multiselect, "表示成分").options), EXPECTED_NAMES)
        self.assert_no_legacy_text(condition)
        records = self.render("substances", visitor)
        self.assertEqual(set(self.widget(records.selectbox, "記録・編集する項目").options), EXPECTED_NAMES)
        self.assert_no_legacy_text(records)
        settings = self.render("settings", visitor)
        profile_names = {item.value for item in settings.text_input
                         if str(item.key or "").startswith("substance_name_")}
        self.assertEqual(profile_names, EXPECTED_NAMES)
        self.assert_no_legacy_text(settings)
        today = self.render("today", visitor)
        self.assert_no_legacy_text(today)

    def test_existing_visitor_keeps_custom_data_and_old_widget_choices_are_sanitized(self):
        from app_core import json_store, substance_service as service
        from web_demo.sandbox import SESSION_KEY, sandbox_context
        from web_demo.substances import default_web_substance_profiles
        visitor = self.visitor()
        caffeine = default_web_substance_profiles()[0]
        old_rows = [deepcopy(caffeine)]
        for identifier, name in zip(("eszopiclone", "trazodone", "melatonin"),
                                    ("エスゾピクロン", "トラゾドン", "メラトニン")):
            old_rows.append({**deepcopy(caffeine), "substance_id": identifier, "name": name})
        custom = {**deepcopy(caffeine), "substance_id": "custom_demo", "name": "独自の体験用成分"}
        old_rows.append(custom)
        with sandbox_context(visitor):
            path = visitor.root / "settings/substance_profiles.json"
            json_store.save_json_safe(path, {"schema_version": 1, "profiles": old_rows})
            probe = visitor.root / "settings/visitor_probe.json"
            json_store.save_json_safe(probe, {"note": "keep my trial change"})
            profiles_before = path.read_bytes()
            probe_before = probe.read_bytes()
            self.assertEqual({p["substance_id"] for p in service.load_substance_profiles()},
                             EXPECTED_IDS | {"custom_demo"})
            legacy_records = [
                {"substance_record_id": "visitor_" + row["substance_id"],
                 "substance_id": row["substance_id"], "taken_at": "2026-11-05T10:00:00",
                 "amount_mg": 25.0, "memo": "keep stored visitor data"}
                for row in old_rows
            ]
            stored_before = deepcopy(legacy_records)
            lookup = {row["substance_id"]: row for row in old_rows}
            normalized = service.normalize_substance_records(legacy_records, profiles=lookup)
            self.assertEqual({row["substance_id"] for row in normalized}, {"caffeine", "custom_demo"})
            self.assertEqual(legacy_records, stored_before)
            import config
            with patch.object(config, "WEB_DEMO_EXPERIENCE", False):
                local_rows = service.normalize_substance_records(legacy_records, profiles=lookup)
                self.assertEqual({row["substance_id"] for row in local_rows}, set(lookup))
        records = self.render("substances", visitor,
                              {"substance_record_selected_id": "melatonin"})
        self.assertEqual(self.widget(records.selectbox, "記録・編集する項目").value, "caffeine")
        self.assertEqual(set(self.widget(records.selectbox, "記録・編集する項目").options),
                         EXPECTED_NAMES | {custom["name"]})
        self.assertIs(records.session_state[SESSION_KEY], visitor)
        widget_log = io.StringIO()
        handler = logging.StreamHandler(widget_log)
        logger = logging.getLogger("streamlit.elements.lib.policies")
        logger.addHandler(handler)
        from streamlit.elements.lib import policies
        try:
            with patch.object(policies, "_shown_default_value_warning", False):
                condition = self.render("condition", visitor,
                                        {"substance_residual_selected_ids": ["caffeine", "melatonin"]})
        finally:
            logger.removeHandler(handler)
        warnings = widget_log.getvalue() + "\n".join(x.value for x in condition.warning)
        self.assertNotIn("created with a default value", warnings)
        self.assertNotIn("Session State API", warnings)
        selected = self.widget(condition.multiselect, "表示成分")
        self.assertEqual(selected.value, ["caffeine"])
        self.assertEqual(set(selected.options), EXPECTED_NAMES | {custom["name"]})
        self.assert_no_legacy_text(condition)
        self.assertEqual(path.read_bytes(), profiles_before, "Do not migrate existing private JSON")
        self.assertEqual(probe.read_bytes(), probe_before, "Do not reset unrelated visitor changes")

    def test_reset_restores_two_defaults_without_changing_the_shared_seed(self):
        from streamlit.testing.v1 import AppTest
        from app_core import json_store, substance_service as service
        from web_demo.sandbox import SESSION_KEY, sandbox_context
        from web_demo.substances import default_web_substance_profiles
        app = AppTest.from_file(ROOT / "main.py", default_timeout=90).run()
        self.healthy(app)
        previous = app.session_state[SESSION_KEY]
        with sandbox_context(previous):
            rows = default_web_substance_profiles()
            rows.append({**deepcopy(rows[0]), "substance_id": "custom_reset", "name": "追加した体験用成分"})
            json_store.save_json_safe(previous.root / "settings/substance_profiles.json",
                                      {"schema_version": 1, "profiles": rows})
        self.widget(app.checkbox, "このタブの操作内容をリセットする").check().run(timeout=90)
        self.widget(app.button, "体験をリセット").click().run(timeout=90)
        self.healthy(app)
        fresh = app.session_state[SESSION_KEY]
        self.assertIsNot(fresh, previous)
        with sandbox_context(fresh):
            self.assertEqual({p["substance_id"] for p in service.load_substance_profiles()}, EXPECTED_IDS)
        self.assertEqual(set(self.widget(self.render("substances", fresh).selectbox,
                                         "記録・編集する項目").options), EXPECTED_NAMES)


if __name__ == "__main__":
    unittest.main()
