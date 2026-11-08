"""Verify the optional local-models folder and install into ignored models/local/."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent

def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', required=True, type=Path, help='Extracted local-models folder')
    p.add_argument('--verify-only', action='store_true')
    a = p.parse_args()
    registry = json.loads((ROOT / 'registry.json').read_text(encoding='utf-8'))
    source = a.source.resolve()
    destination = ROOT / 'local'
    entries = registry['model_files']
    # Validate everything before copying anything; never replace a differing destination.
    for entry in entries:
        rel = Path(entry['path'])
        if rel.is_absolute() or '..' in rel.parts:
            raise SystemExit('Unsafe registry path')
        f = source / rel
        if f.is_symlink() or not f.is_file() or not f.resolve().is_relative_to(source):
            raise SystemExit('Missing/unsafe source: ' + rel.as_posix())
        if f.stat().st_size != entry['bytes'] or sha(f) != entry['sha256']:
            raise SystemExit('Checksum mismatch: ' + rel.as_posix())
        target = destination / rel
        if target.exists() and (target.is_symlink() or sha(target) != entry['sha256']):
            raise SystemExit('Refusing to replace different file: ' + rel.as_posix())
    if not a.verify_only:
        for entry in entries:
            target = destination / entry['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copyfile(source / entry['path'], target)
    print(('VERIFIED' if a.verify_only else 'INSTALLED'), len(entries), 'files')

if __name__ == '__main__':
    main()
