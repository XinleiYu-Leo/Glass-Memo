"""Apply a packaged RoboMemo head (logistic or MLP on pooled frozen MobileNetV3-Small features).

Two input modes:
  --features F.npz   npz with window_ids [N] and features [N,10,576] (as written by make_features.py)
  --frames DIR       directory of <window_id>.npz files with frames [10,224,224,3] uint8 (as written by
                     extract_windows.py); features are computed here with the same frozen encoder (needs torch and a local encoder state_dict; no download is attempted).
Writes a CSV of per-seed scores and the seed mean. Scores are probabilities of 'pick/place completed in the
last 1.5 s' under the fho_transfer_v1_draft3 label policy (weak labels), not calibrated probabilities.
"""
import argparse, csv, json
from pathlib import Path
import numpy as np

MEAN = np.array([0.485, 0.456, 0.406], np.float32); STD = np.array([0.229, 0.224, 0.225], np.float32)
FOUR = [0, 3, 6, 9]


def represent(F, kind, mu, sd):
    X = (F - mu) / sd
    if kind == "last":
        return X[:, -1]
    if kind == "pool4":
        X = X[:, FOUR]
    d = np.abs(np.diff(X, axis=1)).mean(axis=1)
    return np.concatenate([X.mean(1), X.max(1), X[:, -1] - X[:, 0], d], axis=1)


def sig(z):
    return 1 / (1 + np.exp(-np.clip(z, -40, 40)))


def load_model(model_dir):
    model_dir = Path(model_dir)
    cfg = json.loads((model_dir / "config.json").read_text(encoding="utf-8"))
    st = np.load(model_dir / "stats.npz", allow_pickle=False); w = np.load(model_dir / "weights.npz", allow_pickle=False)
    return cfg, st, w


def score(F, cfg, st, w):
    X = (represent(F.astype(np.float64), cfg["representation"], st["frame_mu"], st["frame_sd"]) - st["rep_mu"]) / st["rep_sd"]
    out = {}
    for seed in cfg["seeds"]:
        p = f"seed{seed}__"
        if cfg["head"] == "logistic":
            out[f"seed{seed}"] = sig(X @ w[p + "W"] + w[p + "b"][0])
        else:
            h = np.maximum(X @ w[p + "W1"] + w[p + "b1"], 0)
            out[f"seed{seed}"] = sig(h @ w[p + "w2"] + w[p + "b2"][0])
    out["mean"] = np.mean([v for k, v in out.items()], axis=0)
    return out


def features_from_frames(frames_dir, device, encoder_checkpoint):
    import torch
    from torch import nn
    from torchvision.models import mobilenet_v3_small
    m = mobilenet_v3_small(weights=None)
    m.load_state_dict(torch.load(encoder_checkpoint, map_location="cpu", weights_only=True), strict=True)
    enc = nn.Sequential(m.features, m.avgpool, nn.Flatten(1)).eval().to(device)
    ids, feats = [], []
    for f in sorted(Path(frames_dir).glob("*.npz")):
        with np.load(f, allow_pickle=False) as z:
            fr = z["frames"]
        t = torch.from_numpy(((fr.astype(np.float32) / 255.0 - MEAN) / STD).transpose(0, 3, 1, 2).copy())
        with torch.inference_mode():
            feats.append(enc(t.to(device)).float().cpu().numpy())
        ids.append(f.stem)
    return np.array(ids), np.stack(feats)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", type=Path, required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--features", type=Path)
    g.add_argument("--frames", type=Path)
    ap.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    ap.add_argument("--encoder-checkpoint", type=Path, default=Path(__file__).resolve().parents[2] / "models/local/pretrained/mobilenet_v3_small_imagenet1k_v1.pth", help="Local torchvision ImageNet state_dict; no download is attempted")
    ap.add_argument("--output", type=Path, required=True)
    a = ap.parse_args()
    cfg, st, w = load_model(a.model)
    if a.features:
        z = np.load(a.features, allow_pickle=False); ids, F = z["window_ids"], z["features"]
    else:
        ids, F = features_from_frames(a.frames, a.device, a.encoder_checkpoint)
    if F.ndim != 3 or F.shape[1:] != (10, 576) or len(ids) != len(F) or not np.isfinite(F).all():
        raise ValueError("Expected finite [N,10,576] features and matching window IDs")
    s = score(F, cfg, st, w)
    with a.output.open("w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f); keys = list(s)
        wr.writerow(["window_id"] + keys)
        for i, wid in enumerate(ids):
            wr.writerow([wid] + [round(float(s[k][i]), 6) for k in keys])
    print("SCORED", len(ids), "->", a.output)


if __name__ == "__main__":
    main()
