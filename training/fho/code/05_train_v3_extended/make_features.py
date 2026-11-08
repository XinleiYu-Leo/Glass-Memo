"""Frozen ImageNet MobileNetV3-Small features for every extracted frame (+ pixel-motion baseline).

Same encoder/normalisation as earlier RoboMemo students (IMAGENET1K_V1, features+avgpool, 576-d).
--fake-encoder is ONLY for offline smoke tests without torch; never use it for results.
"""
import argparse, json, sys, time
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C

MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def motion_features(frames):
    g = frames.astype(np.float32).mean(axis=3)                    # [10,224,224]
    g = g.reshape(len(g), 56, 4, 56, 4).mean(axis=(2, 4))          # [10,56,56]
    d = np.abs(np.diff(g, axis=0)).mean(axis=(1, 2))                # [9]
    return d.astype(np.float32)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--fake-encoder", action="store_true")
    a = ap.parse_args()
    manifest = json.loads((a.cache / "extraction_manifest.json").read_text(encoding="utf-8"))
    ids = [r["window_id"] for r in manifest["windows"] if r["status"] in ("extracted", "cached")]
    t0 = time.monotonic()
    if a.fake_encoder:
        rng = np.random.default_rng(0); proj = rng.standard_normal((8 * 8 * 3, 576)).astype(np.float32)
        encode = lambda x: (x.reshape(len(x), 8, 28, 8, 28, 3).mean(axis=(2, 4)).reshape(len(x), -1) / 255.0) @ proj
        encoder_info = {"name": "FAKE_RANDOM_PROJECTION_FOR_SMOKE_TEST_ONLY"}
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
        encoder_info = {"name": "mobilenet_v3_small", "weights": "IMAGENET1K_V1", "torch": torch.__version__,
                        "device": torch.cuda.get_device_name(0) if a.device == "cuda" else a.device}
    feats = np.zeros((len(ids), C.N_FRAMES, 576), dtype=np.float32)
    motion = np.zeros((len(ids), C.N_FRAMES - 1), dtype=np.float32)
    pts = np.zeros((len(ids), C.N_FRAMES), dtype=np.float64)
    buf, where = [], []
    def flush():
        if buf:
            out = encode(np.concatenate(buf))
            k = 0
            for i in where:
                feats[i] = out[k:k + C.N_FRAMES]; k += C.N_FRAMES
            buf.clear(); where.clear()
    for i, wid in enumerate(ids):
        with np.load(a.cache / f"{wid}.npz") as z:
            fr = z["frames"]; pts[i] = z["pts"]
        if fr.shape != (C.N_FRAMES, C.SIZE, C.SIZE, 3):
            raise ValueError("Unexpected frame array for " + wid)
        motion[i] = motion_features(fr)
        buf.append(fr); where.append(i)
        if len(buf) * C.N_FRAMES >= a.batch:
            flush()
        if (i + 1) % 200 == 0:
            print(f"FEATURES {i + 1}/{len(ids)} {time.monotonic() - t0:.0f}s", flush=True)
    flush()
    if not np.isfinite(feats).all():
        raise ValueError("Non-finite features")
    a.output.parent.mkdir(parents=True, exist_ok=True)
    np.savez(a.output, window_ids=np.array(ids), features=feats, motion=motion, pts=pts)
    C.save_json(a.output.with_suffix(".json"), {"encoder": encoder_info, "windows": len(ids), "shape": list(feats.shape),
                "seconds": round(time.monotonic() - t0, 1), "features_sha256": C.sha256_file(a.output)})
    print("FEATURES_DONE", len(ids), flush=True)


if __name__ == "__main__":
    main()
