"""Does audio help? Same windows, same protocol: video only vs audio only vs video+audio.

All methods are trained/evaluated on the windows that have usable audio (so the comparison is fair);
video-only on that same subset is the reference. Dev only sets thresholds; eval is scored once.
"""
import argparse, csv, json, sys, time
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C
import metrics_lib as M


def pool(X):
    d = np.abs(np.diff(X, axis=1)).mean(axis=1)
    return np.concatenate([X.mean(1), X.max(1), X[:, -1] - X[:, 0], d], axis=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", type=Path, required=True)
    ap.add_argument("--audio", type=Path, required=True)
    ap.add_argument("--selection", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    a = ap.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    t0 = time.monotonic()
    zv, za = np.load(a.video), np.load(a.audio)
    vpos = {w: i for i, w in enumerate(zv["window_ids"].tolist())}
    apos = {w: i for i, w in enumerate(za["window_ids"].tolist())}
    akeys = [k for k in ("mel_full", "mel_hfp8k", "ast_full", "ast_hfp8k") if k in za.files]
    sel = C.load_selection(a.selection)
    def pack(rows):
        rows = [r for r in rows if r["window_id"] in vpos and r["window_id"] in apos]
        vi = np.array([vpos[r["window_id"]] for r in rows]); ai = np.array([apos[r["window_id"]] for r in rows])
        d = {"rows": rows, "V": zv["features"][vi].astype(np.float64), "MO": zv["motion"][vi].astype(np.float64),
             "y": np.array([r["label"] for r in rows], float), "action": np.array([r["action"] for r in rows]),
             "parent": np.array([r["canonical_parent"] for r in rows])}
        for k in akeys:
            d[k] = za[k][ai].astype(np.float64)
        return d
    tr, dev, ev = pack(sel["train_full"]), pack(sel["dev"]), pack(sel["eval"])
    counts = {name: {"windows": len(d["y"]), "positives": int(d["y"].sum()), "parents": len(set(d["parent"]))}
              for name, d in (("train", tr), ("dev", dev), ("eval", ev))}
    full_counts = {"train": len(sel["train_full"]), "dev": len(sel["dev"]), "eval": len(sel["eval"])}
    print("AUDIO_SUBSET", json.dumps(counts), flush=True)
    flat = tr["V"].reshape(-1, tr["V"].shape[-1]); vmu, vsd = flat.mean(0), flat.std(0).clip(0.05)
    def block(d, name):
        if name == "video":
            return pool((d["V"] - vmu) / vsd)
        return d[name]
    reps = {"video": ["video"]}
    for k in akeys:
        reps[k] = [k]; reps["video+" + k] = ["video", k]
    scores, results = {"dev": {}, "eval": {}}, {}
    def record(key, sd, se):
        scores["dev"][key], scores["eval"][key] = np.asarray(sd).tolist(), np.asarray(se).tolist()
        results[key] = M.evaluate(key, np.asarray(sd), np.asarray(se), dev, ev, M.CONFIG)
    record("baseline/pixel_motion", dev["MO"].mean(1), ev["MO"].mean(1))
    if "mel_full" in akeys:   # audio heuristic: strongest onset (spectral flux max) in the window
        record("baseline/audio_onset", dev["mel_full"][:, -6], ev["mel_full"][:, -6])
    for rep, parts in reps.items():
        Xtr = np.concatenate([block(tr, p) for p in parts], 1)
        m2, s2 = Xtr.mean(0), Xtr.std(0).clip(1e-3)
        X = lambda d: (np.concatenate([block(d, p) for p in parts], 1) - m2) / s2
        Xtr = (Xtr - m2) / s2
        _, f = M.fit_logistic(Xtr, tr["y"], M.CONFIG["logistic"], 0)
        record(f"logistic/{rep}", f(X(dev)), f(X(ev)))
        for seed in M.CONFIG["seeds"]:
            _, f = M.fit_mlp(Xtr, tr["y"], M.CONFIG["mlp"], seed)
            record(f"mlp/{rep}/seed{seed}", f(X(dev)), f(X(ev)))
        print(f"TRAINED_AUDIO {rep} {time.monotonic() - t0:.0f}s", flush=True)
    # seed-averaged groups + paired bootstrap on eval
    groups = {}
    for k in scores["eval"]:
        groups.setdefault(k.rsplit("/seed", 1)[0] if "/seed" in k else k, []).append(k)
    avg = {g: (np.mean([scores["dev"][k] for k in ks], 0), np.mean([scores["eval"][k] for k in ks], 0)) for g, ks in groups.items()}
    table = {}
    for g, (sd, se) in avg.items():
        r = M.evaluate(g, sd, se, dev, ev, M.CONFIG)
        table[g] = {"seeds": len(groups[g]), "eval_ap": r["eval"]["ap"], "eval_ap_ci95": r["eval"]["ap_ci95"], "eval_auc": r["eval"]["auc"],
                    "eval_balanced_accuracy": r["eval"]["balanced_accuracy"], "eval_recall_take": r["eval"]["recall_take"],
                    "eval_recall_put": r["eval"]["recall_put"], "dev_ap": r["dev"]["ap"]}
    y, gp = ev["y"], ev["parent"]; ug = np.unique(gp); idx = {u: np.nonzero(gp == u)[0] for u in ug}
    rng = np.random.default_rng(11)
    draws = [t for t in (np.concatenate([idx[u] for u in rng.choice(ug, len(ug))]) for _ in range(2000)) if y[t].min() != y[t].max()]
    def paired(g, b):
        out = {}
        for name, fn in (("ap", M.ap_score), ("auc", M.auc_score)):
            d = np.array([fn(y[t], avg[g][1][t]) - fn(y[t], avg[b][1][t]) for t in draws])
            out[name] = {"diff": float(fn(y, avg[g][1]) - fn(y, avg[b][1])), "ci95": [float(np.percentile(d, 2.5)), float(np.percentile(d, 97.5))],
                         "p_le_0": float(np.mean(d <= 0))}
        return {"a": g, "b": b, **out}
    pairs = []
    for k in akeys:
        for a_, b_ in ((f"mlp/video+{k}", "mlp/video"), (f"mlp/{k}", "baseline/pixel_motion"), (f"mlp/{k}", "mlp/video")):
            if a_ in avg and b_ in avg:
                pairs.append(paired(a_, b_))
    for full_k, hfp_k in (("mel_full", "mel_hfp8k"), ("ast_full", "ast_hfp8k")):
        if f"mlp/video+{full_k}" in avg and f"mlp/video+{hfp_k}" in avg:
            pairs.append(paired(f"mlp/video+{hfp_k}", f"mlp/video+{full_k}"))
    for part, d in (("dev", dev), ("eval", ev)):
        with (a.output / f"{part}_scores_audio.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); keys = list(scores[part])
            w.writerow(["window_id", "label", "action", "canonical_parent"] + keys)
            for i, r in enumerate(d["rows"]):
                w.writerow([r["window_id"], r["label"], r["action"], r["canonical_parent"]] + [round(scores[part][k][i], 6) for k in keys])
    C.save_json(a.output / "audio_results.json", {"policy": C.POLICY, "config": M.CONFIG, "audio_features": akeys,
                "subset_counts": counts, "full_selection_counts": full_counts, "table": table, "paired": pairs,
                "seconds": round(time.monotonic() - t0, 1),
                "notes": ["All methods use the same audio-available windows.", "hfp8k = audio band-limited through 8 kHz to mimic the glasses' Bluetooth microphone.",
                          "Weak FHO-derived labels; dev only sets thresholds; eval scored once."]})
    for g in sorted(table, key=lambda g: -table[g]["eval_ap"]):
        t = table[g]; print(f"{g:26s} AP {t['eval_ap']:.3f} AUC {t['eval_auc']:.3f} BA {t['eval_balanced_accuracy']:.3f}", flush=True)
    print("TRAIN_EVAL_AUDIO_DONE", round(time.monotonic() - t0, 1), flush=True)


if __name__ == "__main__":
    main()
