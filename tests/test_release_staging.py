"""Release upload inputs must retain verified source identity and exact hashes."""
from pathlib import Path
import hashlib
import json
import tempfile
import unittest
import zipfile
import importlib.util


SPEC = importlib.util.spec_from_file_location('stage_windows_release', Path(__file__).resolve().parents[1] / 'scripts/stage_windows_release.py')
stager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stager)


class ReleaseStagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pg-stage-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.version, self.commit = '0.1.0-preview.3', 'a' * 40
        (self.root / 'release-control').mkdir()
        (self.root / 'docs/releases').mkdir(parents=True)
        (self.root / 'release').mkdir()
        (self.root / 'release-control/windows-preview.json').write_text(json.dumps({'version': self.version, 'notes': f'docs/releases/v{self.version}.md'}), encoding='utf-8')
        (self.root / f'docs/releases/v{self.version}.md').write_text('Reviewed release notes', encoding='utf-8')
        self.names = [f'proloag-v{self.version}-windows-x64.zip', f'proloag-demo-consultant-v{self.version}-windows-x64.zip']
        artifacts = []
        for kind, name in zip(('normal', 'demo'), self.names):
            path = self.root / 'release' / name
            with zipfile.ZipFile(path, 'w') as bundle:
                bundle.writestr(name.removesuffix('.zip') + '/RELEASE_MANIFEST.json', json.dumps({
                    'version': self.version, 'source_commit': self.commit, 'source_dirty': False,
                    'validation': {'static_source_data_and_inventory': True, 'all_packaged_pages': True}}))
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            artifacts.append({'variant': kind, 'name': name, 'bytes': path.stat().st_size, 'sha256': digest})
            (self.root / 'release' / (name + '.sha256')).write_text(f'{digest}  {name}\n', encoding='ascii')
        (self.root / 'release' / f'SHA256SUMS-proloag-v{self.version}.txt').write_text(''.join(f"{row['sha256']}  {row['name']}\n" for row in artifacts), encoding='ascii')
        self.result = self.root / 'release' / f'BUILD_RESULT-proloag-v{self.version}.json'
        self.result.write_text(json.dumps({'version': self.version, 'source': {'commit': self.commit, 'dirty': False}, 'artifacts': artifacts}), encoding='utf-8')

    def test_only_exact_assets_and_reviewed_notes_are_staged(self):
        (self.root / 'release' / 'PRIVATE_SENTINEL.txt').write_text('do not publish', encoding='utf-8')
        index = stager.stage(self.root, self.version, self.commit)
        self.assertEqual(len(index['assets']), 5)
        self.assertEqual(len(list((self.root / 'release/staged').iterdir())), 7)
        self.assertFalse((self.root / 'release/staged/PRIVATE_SENTINEL.txt').exists())
        for asset in index['assets']:
            self.assertEqual(stager.sha(self.root / 'release/staged' / asset['name']), asset['sha256'])

    def test_dirty_or_different_source_is_rejected_before_staging(self):
        original = json.loads(self.result.read_text(encoding='utf-8'))
        for source in ({'commit': self.commit, 'dirty': True}, {'commit': 'b' * 40, 'dirty': False}):
            self.result.write_text(json.dumps({**original, 'source': source}), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'provenance'):
                stager.stage(self.root, self.version, self.commit)
            self.assertFalse((self.root / 'release/staged').exists())

    def test_changed_archive_and_checksum_are_rejected(self):
        path = self.root / 'release' / self.names[0]
        path.write_bytes(path.read_bytes() + b'changed')
        with self.assertRaisesRegex(ValueError, 'archive changed'):
            stager.stage(self.root, self.version, self.commit)
        self.assertFalse((self.root / 'release/staged').exists())

    def test_changed_request_and_preexisting_staging_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'request'):
            stager.stage(self.root, '0.1.0-preview.4', self.commit)
        (self.root / 'release/staged').mkdir()
        with self.assertRaises(FileExistsError):
            stager.stage(self.root, self.version, self.commit)


if __name__ == '__main__':
    unittest.main()
