"""Option B: partially fine-tune MobileNetV3-Small (last blocks) + temporal pooling head, end to end.

Fixed recipe (no tuning on dev/eval): ImageNet init, blocks features[9:] trainable, BatchNorm frozen,
AdamW (backbone 1e-4, head 1e-3, wd 1e-2), 12 epochs, 32 windows/batch, bf16 autocast,
consistent per-window augmentation (flip, crop 0.8-1.0, brightness/contrast). Dev AP is logged
per epoch for information only; the final epoch is used. Writes dev/eval scores for comparison.
"""
import argparse, csv, json, random, sys, time
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C
import metrics_lib as M

RECIPE = {"trainable_from_block": 9, "epochs": 12, "batch_windows": 32, "lr_backbone": 1e-4, "lr_head": 1e-3,
          "weight_decay": 1e-2, "crop_scale": [0.8, 1.0], "flip": 0.5, "brightness": 0.2, "contrast": 0.2,
          "head": "LayerNorm(2304)->Linear(128)->ReLU->Dropout(0.3)->Linear(1)", "pool": "mean,max,last-first,mean|diff|"}
MEAN = [0.485, 0.456, 0.406]; STD = [0.229, 0.224, 0.225]


def load_frames(cache, rows):
    keep = [r for r in rows if (cache / f"{r['window_id']}.npz").is_file()]
    arr = np.zeros((len(keep), C.N_FRAMES, C.SIZE, C.SIZE, 3), np.uint8)
    for i, r in enumerate(keep):
        with np.load(cache / f"{r['window_id']}.npz") as z:
            arr[i] = z["frames"]
    return keep, arr


def build(torch, nn, pretrained=True):
    from torchvision.models import mobilenet_v3_small, MobileNet_V3_Small_Weights
    class Student(nn.Module):
        def __init__(self):
            super().__init__()
            m = mobilenet_v3_small(weights=MobileNet_V3_Small_Weights.IMAGENET1K_V1 if pretrained else None)
            self.backbone, self.pool = m.features, nn.AdaptiveAvgPool2d(1)
            for p in self.backbone.parameters():
                p.requires_grad_(False)
            for blk in self.backbone[RECIPE["trainable_from_block"]:]:
                for mod in blk.modules():
                    if not isinstance(mod, nn.BatchNorm2d):
                        for p in mod.parameters(recurse=False):
                            p.requires_grad_(True)
            self.head = nn.Sequential(nn.LayerNorm(576 * 4), nn.Linear(576 * 4, 128), nn.ReLU(), nn.Dropout(0.3), nn.Linear(128, 1))
        def forward(self, x):                      # x: B,T,3,H,W normalised
            B, T = x.shape[:2]
            f = self.pool(self.backbone(x.flatten(0, 1))).flatten(1).view(B, T, -1)
            d = (f[:, 1:] - f[:, :-1]).abs().mean(1)
            return self.head(torch.cat([f.mean(1), f.amax(1), f[:, -1] - f[:, 0], d], 1)).squeeze(-1)
        def freeze_bn(self):
            for mod in self.backbone.modules():
                if isinstance(mod, nn.BatchNorm2d):
                    mod.eval()
    return Student


def to_input(torch, F, xb, device, rng=None):
    x = xb.to(device, non_blocking=True).permute(0, 1, 4, 2, 3).float() / 255.0   # B,T,C,H,W
    if rng is not None:
        out = torch.empty_like(x)
        for i in range(x.shape[0]):
            xi = x[i]
            if rng.random() < RECIPE["flip"]:
                xi = xi.flip(-1)
            s = rng.uniform(*RECIPE["crop_scale"]); h = max(32, int(C.SIZE * s))
            top, left = rng.randint(0, C.SIZE - h), rng.randint(0, C.SIZE - h)
            xi = F.interpolate(xi[:, :, top:top + h, left:left + h], size=(C.SIZE, C.SIZE), mode="bilinear", align_corners=False)
            b = 1 + rng.uniform(-RECIPE["brightness"], RECIPE["brightness"])
            c = 1 + rng.uniform(-RECIPE["contrast"], RECIPE["contrast"])
            mu = xi.mean()
            out[i] = (((xi - mu) * c + mu) * b).clamp(0, 1)
        x = out
    mean = torch.tensor(MEAN, device=device).view(1, 1, 3, 1, 1); std = torch.tensor(STD, device=device).view(1, 1, 3, 1, 1)
    return (x - mean) / std


def run_one(torch, nn, F, Student, data, n_train_ids, seed, device, epochs, log):
    random.seed(seed); np.random.seed(seed); torch.manual_seed(seed); torch.cuda.manual_seed_all(seed)
    rng = random.Random(seed)
    tr_idx = np.array([i for i, r in enumerate(data["train"][0]) if r["window_id"] in n_train_ids])
    ytr = torch.tensor([data["train"][0][i]["label"] for i in tr_idx], dtype=torch.float32)
    model = Student().to(device)
    bb = [p for p in model.backbone.parameters() if p.requires_grad]
    opt = torch.optim.AdamW([{"params": bb, "lr": RECIPE["lr_backbone"]}, {"params": model.head.parameters(), "lr": RECIPE["lr_head"]}],
                            weight_decay=RECIPE["weight_decay"])
    lossf = nn.BCEWithLogitsLoss()
    Xtr = torch.from_numpy(data["train"][1])
    history = []
    for ep in range(1, epochs + 1):
        model.train(); model.freeze_bn()
        order = rng.sample(range(len(tr_idx)), len(tr_idx)); tot = 0.0
        for s in range(0, len(order), RECIPE["batch_windows"]):
            b = tr_idx[order[s:s + RECIPE["batch_windows"]]]
            x = to_input(torch, F, Xtr[b], device, rng)
            with torch.autocast("cuda", dtype=torch.bfloat16, enabled=(device == "cuda")):
                logit = model(x)
            loss = lossf(logit.float(), ytr[order[s:s + RECIPE["batch_windows"]]].to(device))
            opt.zero_grad(set_to_none=True); loss.backward(); opt.step(); tot += float(loss) * len(b)
        sd = predict(torch, F, model, data["dev"][1], device)
        dev_ap = M.ap_score(np.array([r["label"] for r in data["dev"][0]], float), sd)
        history.append({"epoch": ep, "train_loss": tot / len(tr_idx), "dev_ap_info_only": dev_ap})
        log(f"FINETUNE seed={seed} n={len(tr_idx)} epoch {ep}/{epochs} loss {tot / len(tr_idx):.4f} devAP {dev_ap:.4f}")
    return model, history


def predict(torch, F, model, X, device, bs=64):
    model.eval(); out = []
    Xt = torch.from_numpy(X)
    with torch.no_grad(), torch.autocast("cuda", dtype=torch.bfloat16, enabled=(device == "cuda")):
        for s in range(0, len(Xt), bs):
            out.append(torch.sigmoid(model(to_input(torch, F, Xt[s:s + bs], device)).float()).cpu().numpy())
    return np.concatenate(out) if out else np.zeros(0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, required=True)
    ap.add_argument("--selection", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--smoke", action="store_true", help="1 epoch, 64 windows per split: crash test only")
    a = ap.parse_args()
    import torch
    from torch import nn
    import torch.nn.functional as F
    if a.device == "cuda" and not torch.cuda.is_available():
        raise SystemExit("CUDA requested but unavailable")
    torch.backends.cudnn.benchmark = False
    a.output.mkdir(parents=True, exist_ok=False)
    t0 = time.monotonic()
    logf = open(a.output / "finetune_progress.log", "x", encoding="utf-8")
    def log(msg):
        print(msg, flush=True); logf.write(msg + "\n"); logf.flush()
    sel = C.load_selection(a.selection)
    lim = 64 if a.smoke else None
    data = {k: load_frames(a.cache, rows[:lim] if lim else rows) for k, rows in
            (("train", sel["train_full"]), ("dev", sel["dev"]), ("eval", sel["eval"]))}
    log(f"LOADED train {len(data['train'][0])} dev {len(data['dev'][0])} eval {len(data['eval'][0])} in {time.monotonic() - t0:.0f}s")
    Student = build(torch, nn)
    runs = [(max(sel["nested"]), 0)] if a.smoke else [(max(sel["nested"]), s) for s in (0, 1, 2)] + [(1000, 0)]
    epochs = 1 if a.smoke else RECIPE["epochs"]
    scores = {"dev": {}, "eval": {}}; hist = {}
    for n, seed in runs:
        ids = sel["nested"][n] if not a.smoke else {r["window_id"] for r in data["train"][0]}
        model, h = run_one(torch, nn, F, Student, data, ids, seed, a.device, epochs, log)
        key = f"finetune/n{n}/seed{seed}"; hist[key] = h
        scores["dev"][key] = predict(torch, F, model, data["dev"][1], a.device).tolist()
        scores["eval"][key] = predict(torch, F, model, data["eval"][1], a.device).tolist()
        if seed == 0 and n == max(sel["nested"]) and not a.smoke:
            torch.save({"state_dict": {k: v.cpu() for k, v in model.state_dict().items()}, "recipe": RECIPE}, a.output / "finetune_n_full_seed0.pt")
        del model; torch.cuda.empty_cache()
    for part in ("dev", "eval"):
        rows = data[part][0]
        with (a.output / f"{part}_scores_finetune.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); keys = list(scores[part])
            w.writerow(["window_id", "label", "action", "canonical_parent"] + keys)
            for i, r in enumerate(rows):
                w.writerow([r["window_id"], r["label"], r["action"], r["canonical_parent"]] + [round(scores[part][k][i], 6) for k in keys])
    C.save_json(a.output / "finetune_summary.json", {"recipe": RECIPE, "smoke": a.smoke, "history": hist, "torch": torch.__version__,
                "device": torch.cuda.get_device_name(0) if a.device == "cuda" else a.device, "seconds": round(time.monotonic() - t0, 1)})
    log(f"FINETUNE_DONE {round(time.monotonic() - t0, 1)}s")


if __name__ == "__main__":
    main()
