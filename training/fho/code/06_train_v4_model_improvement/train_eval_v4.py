"""Option A: frozen appearance features + frozen frame-difference ("motion stream") features.
Same fixed hyper-parameters and evaluation protocol as v3 (dev only sets the threshold).
Writes per-window scores for dev and eval so all methods are compared with one metric code path."""
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
    ap.add_argument("--features", type=Path, required=True)
    ap.add_argument("--selection", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    a = ap.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    t0 = time.monotonic()
    z = np.load(a.features)
    pos = {w: i for i, w in enumerate(z["window_ids"].tolist())}
    A, D, MO = z["features"].astype(np.float64), z["diff_features"].astype(np.float64), z["motion"].astype(np.float64)
    sel = C.load_selection(a.selection)
    def pack(rows):
        rows = [r for r in rows if r["window_id"] in pos]
        ii = np.array([pos[r["window_id"]] for r in rows])
        return {"rows": rows, "A": A[ii], "D": D[ii], "M": MO[ii], "y": np.array([r["label"] for r in rows], float),
                "action": np.array([r["action"] for r in rows]), "parent": np.array([r["canonical_parent"] for r in rows])}
    dev, ev = pack(sel["dev"]), pack(sel["eval"])
    scores = {"dev": {}, "eval": {}}
    results = {}
    def record(key, sd, se):
        scores["dev"][key], scores["eval"][key] = sd.tolist(), se.tolist()
        results[key] = M.evaluate(key, sd, se, dev, ev, M.CONFIG)
    record("baseline/pixel_motion", dev["M"].mean(1), ev["M"].mean(1))
    sizes = sorted(sel["nested"])
    for n in [s for s in sizes if s >= 1000]:
        tr = pack([r for r in sel["train_full"] if r["window_id"] in sel["nested"][n]])
        stats = {}
        for name in ("A", "D"):
            flat = tr[name].reshape(-1, tr[name].shape[-1]); stats[name] = (flat.mean(0), flat.std(0).clip(0.05))
        def rep(d, kind):
            pa = pool((d["A"] - stats["A"][0]) / stats["A"][1])
            dd = (d["D"] - stats["D"][0]) / stats["D"][1]
            pd = np.concatenate([dd.mean(1), dd.max(1)], axis=1)
            return {"app": pa, "app+diff": np.concatenate([pa, pd], 1), "diff": pd}[kind]
        for kind in ("app", "app+diff", "diff"):
            Xtr = rep(tr, kind); m2, s2 = Xtr.mean(0), Xtr.std(0).clip(1e-3)
            X = lambda d: (rep(d, kind) - m2) / s2
            Xtr = (Xtr - m2) / s2
            _, f = M.fit_logistic(Xtr, tr["y"], M.CONFIG["logistic"], 0)
            record(f"n{n}/logistic/{kind}", f(X(dev)), f(X(ev)))
            for seed in M.CONFIG["seeds"]:
                _, f = M.fit_mlp(Xtr, tr["y"], M.CONFIG["mlp"], seed)
                record(f"n{n}/mlp/{kind}/seed{seed}", f(X(dev)), f(X(ev)))
            print(f"TRAINED_V4 n={n} {kind} {time.monotonic() - t0:.0f}s", flush=True)
    for part, d in (("dev", dev), ("eval", ev)):
        with (a.output / f"{part}_scores_v4.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); keys = list(scores[part])
            w.writerow(["window_id", "label", "action", "canonical_parent"] + keys)
            for i, r in enumerate(d["rows"]):
                w.writerow([r["window_id"], r["label"], r["action"], r["canonical_parent"]] + [round(scores[part][k][i], 6) for k in keys])
    C.save_json(a.output / "metrics_v4.json", {"policy": C.POLICY, "config": M.CONFIG, "results": results,
                "seconds": round(time.monotonic() - t0, 1), "note": "Option A: frozen appearance + frozen difference-image features"})
    print("TRAIN_EVAL_V4_DONE", round(time.monotonic() - t0, 1), flush=True)


if __name__ == "__main__":
    main()
