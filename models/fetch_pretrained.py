"""Explicitly download one external pretrained checkpoint and verify its full SHA-256."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent

def check(path, expected_size, expected_sha):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return path.stat().st_size == expected_size and h.hexdigest() == expected_sha

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('model', choices=['imagenet', '100doh'])
    p.add_argument('--output', type=Path)
    a = p.parse_args()
    reg = json.loads((ROOT / 'registry.json').read_text(encoding='utf-8'))
    if a.model == 'imagenet':
        e = reg['external_pretrained'][1]
        url, size, sha = e['url'], e['bytes'], e['sha256']
        target = a.output or ROOT / 'local/pretrained/mobilenet_v3_small_imagenet1k_v1.pth'
    else:
        e = reg['external_pretrained'][0]
        url, size, sha = e['mirror_url'], e['checkpoint_bytes'], e['checkpoint_sha256']
        target = a.output or ROOT.parent / 'baselines/hand_object/checkpoints/faster_rcnn_1_8_89999.pth'
    if target.exists():
        if not check(target, size, sha):
            raise SystemExit('Existing file does not match; move it aside first.')
        print('ALREADY_VERIFIED', a.model)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.download-', delete=False) as f:
            temporary = Path(f.name)
            with urllib.request.urlopen(url, timeout=60) as response:
                received = 0
                while block := response.read(1024 * 1024):
                    received += len(block)
                    if received > size:
                        raise ValueError('Downloaded file exceeds the expected size')
                    f.write(block)
        if not check(temporary, size, sha):
            raise ValueError('Downloaded bytes do not match the recorded checkpoint SHA-256')
        if target.exists():
            raise FileExistsError('Destination appeared while downloading; refusing replacement')
        temporary.rename(target)
        print('DOWNLOADED_AND_VERIFIED', a.model, size, 'bytes')
    finally:
        if temporary and temporary.exists():
            temporary.unlink()

if __name__ == '__main__':
    main()
