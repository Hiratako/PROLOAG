from pathlib import Path
import json
import tempfile
import unittest

from scripts.init_local_data import EMPTY_FILES, initialize

class PublicSourceScaffoldTests(unittest.TestCase):
    def test_empty_scaffold_and_repeat_do_not_change_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = initialize(root)
            self.assertEqual(set(first['created']), set(EMPTY_FILES))
            before = {name: (root/name).read_bytes() for name in EMPTY_FILES}
            for name, value in EMPTY_FILES.items():
                self.assertEqual(json.loads(before[name]), value)
            second = initialize(root)
            self.assertEqual(set(second['kept']), set(EMPTY_FILES))
            self.assertEqual(before, {name: (root/name).read_bytes() for name in EMPTY_FILES})

    def test_existing_nonempty_and_invalid_json_is_never_read_or_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'flashcards.json').write_bytes(b'not-json: preserve exactly\x00')
            before=(root/'flashcards.json').read_bytes()
            result=initialize(root)
            self.assertIn('flashcards.json',result['kept'])
            self.assertEqual((root/'flashcards.json').read_bytes(),before)

    def test_linked_output_is_rejected_before_initialization(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root=Path(directory)
            try: (root/'notes_data').symlink_to(Path(outside),target_is_directory=True)
            except OSError: self.skipTest('Symlink creation is unavailable')
            with self.assertRaises(ValueError): initialize(root)
            self.assertEqual(list(Path(outside).iterdir()),[])
            self.assertFalse((root/'flashcards.json').exists())
