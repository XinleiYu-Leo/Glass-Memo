"""Offline inference for the historical reviewed-v2 FOUR-frame model.

This is not the newer ten-frame FHO model reported in the project brief.
The checkpoint includes the frozen encoder, so no download or API is used.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image
import torch
from torch import nn
from torchvision.models import mobilenet_v3_small

DEFAULT_CHECKPOINT = Path(__file__).resolve().parents[2] / 'models/local/historical/historical_reviewed_v2.pt'
EXPECTED_CHECKPOINT_SHA256 = '6c80dd106518f165331e7ba662ef33279d672265c6384c0f35c108a1489cb0d7'
ALLOWED_KEYS = frozenset({
    'encoder_name', 'encoder_pretraining', 'encoder_state', 'encoder_sha256',
    'train_labels_sha256', 'dev_labels_sha256', 'train_manifest_sha256',
    'dev_manifest_sha256', 'seed', 'schema', 'policy_version', 'head_state',
    'feature_mean', 'feature_std', 'feature_dimension', 'saved_epoch',
    'development_metric', 'selection_metric', 'frame_offsets_sec', 'sample_fps',
    'threshold', 'input_size', 'resize', 'pixel_mean', 'pixel_std', 'label_source',
    'limitations',
})


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def state_digest(state):
    h = hashlib.sha256()
    for key, value in sorted(state.items()):
        value = value.detach().cpu().contiguous()
        h.update(key.encode())
        h.update(str((str(value.dtype), list(value.shape))).encode())
        h.update(value.numpy().tobytes())
    return h.hexdigest()


def load(checkpoint=DEFAULT_CHECKPOINT, device='cpu'):
    if digest(checkpoint) != EXPECTED_CHECKPOINT_SHA256:
        raise ValueError('Checkpoint differs from the packaged historical model.')
    c = torch.load(checkpoint, map_location='cpu', weights_only=True)
    if set(c) != ALLOWED_KEYS or c['schema'] != 'robomemo-student-v2':
        raise ValueError('Unexpected checkpoint schema or metadata.')
    if c['frame_offsets_sec'] != [-1.5, -1., -.5, 0.] or c['feature_dimension'] != 576:
        raise ValueError('This tool only supports the reviewed-v2 four-frame model.')
    if state_digest(c['encoder_state']) != c['encoder_sha256']:
        raise ValueError('Encoder tensor digest mismatch.')
    base = mobilenet_v3_small(weights=None)
    encoder = nn.Sequential(base.features, base.avgpool, nn.Flatten(1))
    encoder.load_state_dict(c['encoder_state'], strict=True)
    head = nn.Sequential(nn.Linear(2304, 128), nn.ReLU(), nn.Dropout(.2), nn.Linear(128, 1))
    head.load_state_dict(c['head_state'], strict=True)
    encoder.eval().to(device)
    head.eval().to(device)
    for model in (encoder, head):
        for parameter in model.parameters():
            parameter.requires_grad_(False)
    return c, encoder, head


def preprocess(image, c):
    image = image.convert('RGB')
    side = c['input_size']
    scale = min(side / image.width, side / image.height)
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    resized = image.resize(size, Image.Resampling.BILINEAR)
    canvas = Image.new('RGB', (side, side), tuple(round(x * 255) for x in c['pixel_mean']))
    canvas.paste(resized, ((side - size[0]) // 2, (side - size[1]) // 2))
    x = torch.from_numpy(np.asarray(canvas, dtype=np.float32).copy() / 255.).permute(2, 0, 1)
    return (x - torch.tensor(c['pixel_mean'])[:, None, None]) / torch.tensor(c['pixel_std'])[:, None, None]


def predict(images, c, encoder, head, device='cpu'):
    if len(images) != 4:
        raise ValueError('Supply exactly four chronological image frames.')
    x = torch.stack([preprocess(image, c) for image in images]).to(device)
    with torch.inference_mode():
        features = encoder(x).cpu()
        standardized = ((features - c['feature_mean']) / c['feature_std']).flatten().unsqueeze(0)
        probability = float(head(standardized.to(device)).squeeze().sigmoid().cpu())
    return {'schema': 'historical-reviewed-v2-inference',
            'historical_model_date': '2026-10-24',
            'model_input_frames': 4, 'probability_pick_place_proxy': probability,
            'threshold': float(c['threshold']),
            'predicted_positive': probability >= float(c['threshold']),
            'limitations': 'Historical weak-label classifier; not the latest FHO model, not a validated event detector or object-location answer.'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--checkpoint', type=Path, default=DEFAULT_CHECKPOINT)
    p.add_argument('--images', type=Path, nargs=4, metavar=('FRAME1','FRAME2','FRAME3','FRAME4'))
    p.add_argument('--timestamps', type=float, nargs=4,
                   help='Optional actual source times; requires --decision-time. No future or stale inputs allowed.')
    p.add_argument('--decision-time', type=float)
    p.add_argument('--device', choices=('cpu','cuda'), default='cpu')
    p.add_argument('--threads', type=int, default=4)
    p.add_argument('--self-test', action='store_true', help='Use four synthetic solid-color images; not an accuracy test.')
    a = p.parse_args()
    if a.threads < 1:
        p.error('--threads must be positive')
    torch.set_num_threads(a.threads)
    if a.self_test == bool(a.images):
        p.error('Choose either --self-test or --images, not both.')
    if a.timestamps is not None:
        if a.decision_time is None:
            p.error('--timestamps requires --decision-time')
        if any(x >= y for x,y in zip(a.timestamps,a.timestamps[1:])):
            p.error('Source times must strictly increase')
        nominal = [a.decision_time + t for t in (-1.5,-1.,-.5,0.)]
        if any(t > n + 1e-6 or n - t > .250001 for t,n in zip(a.timestamps,nominal)):
            p.error('Frames must be at/before nominal times and no more than 0.25 s stale')
    c,encoder,head=load(a.checkpoint,a.device)
    if a.self_test:
        images=[Image.new('RGB',(300,200),(20+i*35,60+i*20,120)) for i in range(4)]
    else:
        images=[]
        for path in a.images:
            with Image.open(path) as image:
                images.append(image.convert('RGB').copy())
    result=predict(images,c,encoder,head,a.device)
    result['synthetic_self_test']=a.self_test
    print(json.dumps(result,indent=2,allow_nan=False))


if __name__=='__main__':
    main()
