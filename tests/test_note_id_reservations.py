from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core import json_store, note_service
import data_utils


class NoteCreationReservationTests(unittest.TestCase):
    """Both non-page creation paths reserve historical IDs, using temp data only."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag_note_ids_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.token = datetime.now().strftime("%Y%m%d")
        self.day = datetime.now().date().isoformat()
        self.notes_path = self.root / "notes_data/notebooks.json"
        self.trash_path = self.root / "backups/note_trash.json"
        self.put(self.notes_path, {
            "unknown_store": {"keep": [1, 2]},
            "notebooks": [{"id": "book", "name": "book", "unknown_book": 3,
                "sections": [{"id": "section", "name": "section", "unknown_section": 4,
                    "pages": [{"note_id": "live_note", "id": f"note_{self.token}_009",
                        "title": "unchanged", "body": "original body", "unknown_note": {"keep": 5}}]}]}],
        })
        self.put(self.trash_path, [
            {"note_id": f"note_{self.token}_010", "note": {"note_id": f"note_{self.token}_010", "id": f"note_{self.token}_011"}},
            {"note_id": f"note_{self.token}_019", "note": {"note_id": f"note_{self.token}_019", "id": f"note_{self.token}_020"}, "restored_at": "previously restored"},
        ])
        patcher = patch.object(json_store, "PROJECT_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def put(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def get(self):
        return json.loads(self.notes_path.read_text(encoding="utf-8"))

    def assert_unknown_fields_preserved(self):
        store = self.get()
        self.assertEqual(store["unknown_store"], {"keep": [1, 2]})
        book = store["notebooks"][0]
        self.assertEqual(book["unknown_book"], 3)
        section = book["sections"][0]
        self.assertEqual(section["unknown_section"], 4)
        self.assertEqual(section["pages"][0]["unknown_note"], {"keep": 5})
        self.assertEqual(section["pages"][0]["body"], "original body")

    def test_service_add_reserves_all_trash_history_including_restored_alias(self):
        note_id = note_service.add_note("new", "new body", root=self.root)
        self.assertEqual(note_id, f"note_{self.token}_021")
        self.assert_unknown_fields_preserved()

    def test_session_creation_reserves_all_trash_history(self):
        note_id = data_utils.create_note_from_session(
            self.root, {"session_id": "session_synthetic", "start_at": f"{self.day}T09:00:00"},
            "new", [], "new body",
        )
        self.assertEqual(note_id, f"note_{self.token}_021")
        self.assert_unknown_fields_preserved()

    def test_service_add_reserves_raw_live_alias_lost_by_normalization(self):
        raw = self.get()
        raw["notebooks"][0]["sections"][0]["pages"][0]["id"] = f"note_{self.token}_040"
        self.put(self.notes_path, raw)
        self.assertEqual(note_service.add_note("new", root=self.root), f"note_{self.token}_041")

    def test_session_creation_reserves_raw_live_alias_lost_by_normalization(self):
        raw = self.get()
        raw["notebooks"][0]["sections"][0]["pages"][0]["id"] = f"note_{self.token}_040"
        self.put(self.notes_path, raw)
        note_id = data_utils.create_note_from_session(
            self.root, {"session_id": "session_synthetic", "start_at": f"{self.day}T09:00:00"},
            "new", [], "new body",
        )
        self.assertEqual(note_id, f"note_{self.token}_041")

    def test_invalid_trash_stops_creation_without_modifying_notes(self):
        self.put(self.trash_path, [{"note": "invalid"}])
        before = self.notes_path.read_bytes()
        with self.assertRaises(ValueError):
            note_service.add_note("must not save", root=self.root)
        self.assertEqual(self.notes_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
