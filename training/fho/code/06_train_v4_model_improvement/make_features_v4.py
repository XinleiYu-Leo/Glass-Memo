"""Frozen MobileNetV3-Small features for appearance frames AND frame-difference images.

Appearance: the 10 frames (as before). Motion stream: 9 images |f_k - f_{k-1}| x 4 (clipped),
encoded by the SAME frozen encoder. Plus the scalar pixel-motion baseline.
--fake-encoder is ONLY for offline smoke tests.
"""
import argparse, json, sys, time
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C

MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
DIFF_GAIN = 4.0


def diff_images(frames):
    f = frames.astype(np.int16)
    return np.clip(np.abs(f[1:] - f[:-1]) * DIFF_GAIN, 0, 255).astype(np.uint8)   # [9,224,224,3]


def motion_scalar(frames):
    g = frames.astype(np.float32).mean(axis=3).reshape(len(frames), 56, 4, 56, 4).mean(axis=(2, 4))
    return np.abs(np.diff(g, axis=0)).mean(axis=(1, 2)).astype(np.float32)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, required=True)
    ap.add_argument("--selection", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--batch", type=int, default=380)
    ap.add_argument("--fake-encoder", action="store_true")
    a = ap.parse_args()
    sel = C.load_selection(a.selection)
    wanted = [r["window_id"] for r in sel["train_full"] + sel["dev"] + sel["eval"]]
    ids = [w for w in wanted if (a.cache / f"{w}.npz").is_file()]
    t0 = time.monotonic()
    if a.fake_encoder:
        rng = np.random.default_rng(0); proj = rng.standard_normal((8 * 8 * 3, 576)).astype(np.float32)
        encode = lambda x: (x.reshape(len(x), 8, 28, 8, 28, 3).mean(axis=(2, 4)).reshape(len(x), -1) / 255.0) @ proj
        info = {"name": "FAKE_RANDOM_PROJECTION_FOR_SMOKE_TEST_ONLY"}
    else:
        import torch
        from torch import nn
        from torchvision.models import mobilenet_v3_small, MobileNet_V3_Small_Weights
        if a.device == "cuda" and not torch.cuda.is_available():
            raise SystemExit("CUDA requested but unavailable")
        m = mobilenet_v3_small(weights=MobileNet_V3_Small_Weights.IMAGENET1K_V1)
        enc = nn.Sequential(m.features, m.avgpool, nn.Flatten(1)).eval().to(a.device)
        for p in enc.parameters():
            p.requires_grad_(False)
        def encode(x):
            t = torch.from_numpy(((x.astype(np.float32) / 255.0 - MEAN) / STD).transpose(0, 3, 1, 2).copy())
            with torch.inference_mode():
                return enc(t.to(a.device)).float().cpu().numpy()
        info = {"name": "mobilenet_v3_small", "weights": "IMAGENET1K_V1", "torch": torch.__version__,
                "device": torch.cuda.get_device_name(0) if a.device == "cuda" else a.device}
    n = len(ids)
    app = np.zeros((n, C.N_FRAMES, 576), np.float32)
    dif = np.zeros((n, C.N_FRAMES - 1, 576), np.float32)
    mot = np.zeros((n, C.N_FRAMES - 1), np.float32)
    buf, slots = [], []
    def flush():
        if not buf:
            return
        out = encode(np.concatenate(buf)); k = 0
        for i, kind, cnt in slots:
            (app if kind == "a" else dif)[i] = out[k:k + cnt]; k += cnt
        buf.clear(); slots.clear()
    for i, wid in enumerate(ids):
        with np.load(a.cache / f"{wid}.npz") as z:
            fr = z["frames"]
        if fr.shape != (C.N_FRAMES, C.SIZE, C.SIZE, 3):
            raise ValueError("Unexpected frames for " + wid)
        mot[i] = motion_scalar(fr)
        d = diff_images(fr)
        buf += [fr, d]; slots += [(i, "a", C.N_FRAMES), (i, "d", C.N_FRAMES - 1)]
        if sum(len(b) for b in buf) >= a.batch:
            flush()
        if (i + 1) % 300 == 0:
            print(f"FEATURES_V4 {i + 1}/{n} {time.monotonic() - t0:.0f}s", flush=True)
    flush()
    if not (np.isfinite(app).all() and np.isfinite(dif).all()):
        raise ValueError("Non-finite features")
    a.output.parent.mkdir(parents=True, exist_ok=True)
    np.savez(a.output, window_ids=np.array(ids), features=app, diff_features=dif, motion=mot)
    C.save_json(a.output.with_suffix(".json"), {"encoder": info, "windows": n, "missing_windows": len(wanted) - n,
                "diff_gain": DIFF_GAIN, "seconds": round(time.monotonic() - t0, 1), "sha256": C.sha256_file(a.output)})
    print("FEATURES_V4_DONE", n, flush=True)


if __name__ == "__main__":
    main()
