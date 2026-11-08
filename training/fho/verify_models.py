"""Offline checks of actual packaged head tensors using synthetic inputs, never an accuracy test."""
from __future__ import annotations
import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import numpy as np
from apply_heads import features_from_frames, load_model, score

ROOT = Path(__file__).resolve().parents[2]

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--models-root', type=Path, default=ROOT / 'models/local')
    p.add_argument('--encoder-checkpoint', type=Path,
                   help='Optional: exercise actual ImageNet encoder on synthetic frames as well')
    p.add_argument('--output', type=Path)
    a = p.parse_args()
    rng = np.random.default_rng(42)
    features = rng.normal(size=(3, 10, 576)).astype(np.float32)
    report = {'schema': 'robomemo-head-smoke-v1', 'status': 'passed',
              'inputs': 'synthetic features', 'network_calls': 0, 'training_runs': 0,
              'evaluated_real_dataset_accuracy': False, 'models': []}
    if a.encoder_checkpoint:
        with tempfile.TemporaryDirectory() as tmp:
            frames = rng.integers(0, 256, size=(10, 224, 224, 3), dtype=np.uint8)
            np.savez(Path(tmp) / 'synthetic.npz', frames=frames)
            _, features = features_from_frames(tmp, 'cpu', a.encoder_checkpoint)
        report['inputs'] = 'synthetic frames through actual frozen encoder'
    paths = sorted((a.models_root / 'fho/models').rglob('config.json'))
    if not paths:
        raise SystemExit('No models found; run models/install_local.py first or set --models-root.')
    for path in paths:
        if 'finetune' in path.parent.name:
            continue
        cfg, stats, weights = load_model(path.parent)
        result = score(features, cfg, stats, weights)
        assert all(v.shape == (len(features),) and np.isfinite(v).all()
                   and ((v >= 0) & (v <= 1)).all() for v in result.values())
        # Verify the packaged model is an exact view of the original saved head archive.
        raw = a.models_root / 'fho/raw_head_weights' / ('head_weights_' + cfg['source_run'] + '.npz')
        with np.load(raw, allow_pickle=False) as source:
            for seed in cfg['seeds']:
                prefix = f"n{cfg['train_windows']}__{cfg['head']}__{cfg['representation']}__"
                if cfg['head'] == 'mlp':
                    prefix += f'seed{seed}__'
                for name in weights.files:
                    if name.startswith(f'seed{seed}__'):
                        assert np.array_equal(weights[name], source[prefix + name.split('__', 1)[1]])
        report['models'].append({'name': cfg['name'], 'synthetic_inference': 'passed',
                                 'matches_original_saved_tensors': True})
        stats.close()
        weights.close()
    assert len(report['models']) == 16, 'Unexpected number of frozen heads'
    if a.output:
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print('HEAD_SMOKE_PASSED', len(report['models']))
    print('Synthetic input check only; no accuracy or device performance claim.')

if __name__ == '__main__':
    main()
