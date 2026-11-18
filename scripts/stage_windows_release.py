"""Stage only verified, unused distribution files for the release workflow.

This script performs no network calls, edits no release, and never reads app
records. The Windows builder must have completed its source/data/page checks.
"""
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import stat
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024**2), b''):
            digest.update(block)
    return digest.hexdigest()


def regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 1024:
        raise ValueError('Release inputs must be regular files')


def stage(root, version, source_commit):
    root = Path(root).resolve()
    if not re.fullmatch(r'\d+\.\d+\.\d+-preview\.\d+', version or ''):
        raise ValueError('Invalid preview version')
    if not re.fullmatch(r'[0-9a-f]{40}', source_commit or ''):
        raise ValueError('A fixed source commit is required')
    request = json.loads((root / 'release-control/windows-preview.json').read_text(encoding='utf-8'))
    notes_name = f'docs/releases/v{version}.md'
    if request != {'version': version, 'notes': notes_name}:
        raise ValueError('Release request mismatch')
    notes = root / notes_name
    regular(notes)
    output = root / 'release'
    if output.is_symlink() or not output.resolve().is_relative_to(root):
        raise ValueError('Unsafe release output')
    result_file = output / f'BUILD_RESULT-proloag-v{version}.json'
    regular(result_file)
    result = json.loads(result_file.read_text(encoding='utf-8'))
    if (result.get('version') != version or result.get('source') != {'commit': source_commit, 'dirty': False}
            or {row.get('variant') for row in result.get('artifacts', [])} != {'normal', 'demo'}
            or len(result.get('artifacts', [])) != 2):
        raise ValueError('Build provenance mismatch')
    zip_names = [f'proloag-v{version}-windows-x64.zip', f'proloag-demo-consultant-v{version}-windows-x64.zip']
    if {row.get('name') for row in result['artifacts']} != set(zip_names):
        raise ValueError('Unexpected build artifacts')
    for name in zip_names:
        path = output / name
        regular(path)
        row = next(row for row in result['artifacts'] if row['name'] == name)
        if row.get('sha256') != sha(path) or row.get('bytes') != path.stat().st_size:
            raise ValueError('Built archive changed')
        with zipfile.ZipFile(path) as bundle:
            manifest = json.loads(bundle.read(name.removesuffix('.zip') + '/RELEASE_MANIFEST.json'))
            if (manifest.get('version') != version or manifest.get('source_commit') != source_commit
                    or manifest.get('source_dirty') is not False
                    or manifest.get('validation', {}).get('static_source_data_and_inventory') is not True
                    or manifest.get('validation', {}).get('all_packaged_pages') is not True):
                raise ValueError('Package verification/provenance mismatch')
    sums_name = f'SHA256SUMS-proloag-v{version}.txt'
    names = [*zip_names, *(name + '.sha256' for name in zip_names), sums_name]
    expected_sums = ''.join(f'{sha(output / name)}  {name}\n' for name in zip_names)
    if (output / sums_name).read_text(encoding='ascii') != expected_sums:
        raise ValueError('Combined checksum mismatch')
    for name in zip_names:
        if (output / (name + '.sha256')).read_text(encoding='ascii') != f'{sha(output / name)}  {name}\n':
            raise ValueError('Archive checksum mismatch')
    assets = []
    for name in names:
        path = output / name
        regular(path)
        assets.append({'name': name, 'bytes': path.stat().st_size, 'sha256': sha(path)})
    target = output / 'staged'
    target.mkdir(exist_ok=False)
    for asset in assets:
        shutil.copyfile(output / asset['name'], target / asset['name'])
        if sha(target / asset['name']) != asset['sha256']:
            raise ValueError('Staged asset changed')
    shutil.copyfile(notes, target / 'release-notes.md')
    index = {'version': version, 'source_commit': source_commit, 'source_dirty': False, 'assets': assets}
    (target / 'index.json').write_text(json.dumps(index, indent=2) + '\n', encoding='utf-8')
    return index


if __name__ == '__main__':
    print(json.dumps(stage(ROOT, os.environ.get('PROLOAG_RELEASE_VERSION'), os.environ.get('GITHUB_SHA'))))
