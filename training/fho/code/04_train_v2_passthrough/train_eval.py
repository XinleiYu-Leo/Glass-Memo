"""Train small heads on frozen features and evaluate on the fixed dev/eval sets (numpy only).

Hyper-parameters are fixed in advance (no tuning on dev or eval). Dev is used only to pick a
decision threshold; eval is scored once with that threshold. Labels are weak FHO-derived labels.
"""
import argparse, csv, json, sys, time
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C

CONFIG = {"sizes": [200, 500, 1000], "seeds": [0, 1, 2],
          "logistic": {"steps": 600, "lr": 0.01, "l2": 1e-2},
          "mlp": {"hidden": 64, "steps": 300, "lr": 1e-3, "l2": 1e-2},
          "bootstrap": {"resamples": 1000, "seed": 7, "unit": "canonical_parent"}}


def represent(F, kind, mu, sd):
    X = (F - mu) / sd
    if kind == "last":
        return X[:, -1]
    if kind == "pool4":
        X = X[:, C.FOUR]
    d = np.abs(np.diff(X, axis=1)).mean(axis=1)
    return np.concatenate([X.mean(1), X.max(1), X[:, -1] - X[:, 0], d], axis=1)


def adam_fit(params, grad_fn, steps, lr):
    m = [np.zeros_like(p) for p in params]; v = [np.zeros_like(p) for p in params]
    for t in range(1, steps + 1):
        grads = grad_fn(params)
        for i, g in enumerate(grads):
            m[i] = 0.9 * m[i] + 0.1 * g; v[i] = 0.999 * v[i] + 0.001 * g * g
            params[i] -= lr * (m[i] / (1 - 0.9 ** t)) / (np.sqrt(v[i] / (1 - 0.999 ** t)) + 1e-8)
    return params


def sig(z):
    return 1 / (1 + np.exp(-np.clip(z, -40, 40)))


def fit_logistic(X, y, cfg, seed):
    w = [np.zeros(X.shape[1]), np.zeros(1)]
    pw = (1 - y).sum() / max(1, y.sum()); wt = np.where(y == 1, pw, 1.0); wt /= wt.mean()
    def g(p):
        r = (sig(X @ p[0] + p[1][0]) - y) * wt / len(y)
        return [X.T @ r + cfg["l2"] * p[0], np.array([r.sum()])]
    w = adam_fit(w, g, cfg["steps"], cfg["lr"])
    return {"W": w[0], "b": w[1]}, lambda Z: sig(Z @ w[0] + w[1][0])


def fit_mlp(X, y, cfg, seed):
    rng = np.random.default_rng(seed); h = cfg["hidden"]
    p = [rng.standard_normal((X.shape[1], h)) * np.sqrt(2 / X.shape[1]), np.zeros(h), rng.standard_normal(h) * np.sqrt(1 / h), np.zeros(1)]
    pw = (1 - y).sum() / max(1, y.sum()); wt = np.where(y == 1, pw, 1.0); wt /= wt.mean()
    def g(q):
        a = X @ q[0] + q[1]; hdn = np.maximum(a, 0); out = sig(hdn @ q[2] + q[3][0])
        r = (out - y) * wt / len(y)
        gw2 = hdn.T @ r; gb2 = np.array([r.sum()])
        dh = np.outer(r, q[2]) * (a > 0)
        return [X.T @ dh + cfg["l2"] * q[0], dh.sum(0), gw2 + cfg["l2"] * q[2], gb2]
    p = adam_fit(p, g, cfg["steps"], cfg["lr"])
    return {"W1": p[0], "b1": p[1], "w2": p[2], "b2": p[3]}, lambda Z: sig(np.maximum(Z @ p[0] + p[1], 0) @ p[2] + p[3][0])


def ap_score(y, s):
    """Average precision with tied scores treated as one threshold (all-equal scores -> prevalence)."""
    o = np.argsort(-s, kind="mergesort"); y, s = y[o], s[o]
    tp = np.cumsum(y); n = np.arange(1, len(y) + 1)
    last = np.r_[s[1:] != s[:-1], True]          # last index of each tie block
    tp, n = tp[last], n[last]
    prec, rec = tp / n, tp / max(1, y.sum())
    return float(np.sum(np.diff(np.r_[0.0, rec]) * prec))


def auc_score(y, s):
    from math import isclose
    order = np.argsort(s, kind="mergesort"); ranks = np.empty(len(s)); ranks[order] = np.arange(1, len(s) + 1)
    for v in np.unique(s):  # average ties
        idx = s == v
        if idx.sum() > 1:
            ranks[idx] = ranks[idx].mean()
    n1 = y.sum(); n0 = len(y) - n1
    return float((ranks[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def at_threshold(y, s, thr):
    p = s >= thr; tp = int((p & (y == 1)).sum()); fp = int((p & (y == 0)).sum())
    fn = int((~p & (y == 1)).sum()); tn = int((~p & (y == 0)).sum())
    rec = tp / max(1, tp + fn); spec = tn / max(1, tn + fp)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "recall": rec, "specificity": spec,
            "balanced_accuracy": 0.5 * (rec + spec), "precision": tp / max(1, tp + fp)}


def best_threshold(y, s):
    cands = np.unique(s); best = (-1, 0.5)
    for c in cands:
        ba = at_threshold(y, s, c)["balanced_accuracy"]
        if ba > best[0] + 1e-12:
            best = (ba, float(c))
    return best[1]


def bootstrap(y, s, groups, thr, n, seed):
    rng = np.random.default_rng(seed); ug = np.unique(groups); idx = {g: np.nonzero(groups == g)[0] for g in ug}
    aps, bas = [], []
    for _ in range(n):
        take = np.concatenate([idx[g] for g in rng.choice(ug, len(ug), replace=True)])
        yy, ss = y[take], s[take]
        if yy.min() == yy.max():
            continue
        aps.append(ap_score(yy, ss)); bas.append(at_threshold(yy, ss, thr)["balanced_accuracy"])
    q = lambda v: [float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5))] if v else None
    return {"ap_ci95": q(aps), "balanced_accuracy_ci95": q(bas), "valid_resamples": len(aps)}


def evaluate(name, sd, se, dev, ev, cfg):
    for part, y in (("dev", dev["y"]), ("eval", ev["y"])):
        if len(y) == 0 or y.min() == y.max():
            raise ValueError(part + " set needs both classes; got " + str(np.unique(y).tolist()))
    thr = best_threshold(dev["y"], sd)
    out = {"threshold_from_dev": thr, "dev": {"ap": ap_score(dev["y"], sd), "auc": auc_score(dev["y"], sd), **at_threshold(dev["y"], sd, thr)},
           "eval": {"ap": ap_score(ev["y"], se), "auc": auc_score(ev["y"], se), **at_threshold(ev["y"], se, thr)}}
    for act in ("take", "put"):
        m = ev["action"] == act
        out["eval"]["recall_" + act] = float((se[m] >= thr).mean()) if m.any() else None
    out["eval"].update(bootstrap(ev["y"], se, ev["parent"], thr, cfg["bootstrap"]["resamples"], cfg["bootstrap"]["seed"]))
    return out


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
    F, M = z["features"].astype(np.float64), z["motion"].astype(np.float64)
    sel = C.load_selection(a.selection)
    def pack(rows):
        rows = [r for r in rows if r["window_id"] in pos]
        ii = np.array([pos[r["window_id"]] for r in rows])
        return {"rows": rows, "F": F[ii], "M": M[ii], "y": np.array([r["label"] for r in rows], dtype=np.float64),
                "action": np.array([r["action"] for r in rows]), "parent": np.array([r["canonical_parent"] for r in rows])}
    dev, ev = pack(sel["dev"]), pack(sel["eval"])
    order = [r for r in sel["train_1000"]]
    results, preds, counts = {}, {"window_id": [r["window_id"] for r in ev["rows"]]}, {}
    # baselines without training
    results["baseline/all_positive"] = evaluate("allpos", np.ones(len(dev["y"])), np.ones(len(ev["y"])), dev, ev, CONFIG)
    results["baseline/pixel_motion"] = evaluate("motion", dev["M"].mean(1), ev["M"].mean(1), dev, ev, CONFIG)
    preds["baseline/pixel_motion"] = ev["M"].mean(1).tolist()
    weights = {}
    for n in CONFIG["sizes"]:
        tr = pack([r for r in order if r["window_id"] in sel["nested"][n]])
        counts[n] = {"windows": len(tr["y"]), "positives": int(tr["y"].sum()), "parents": int(len(set(tr["parent"])))}
        mu = tr["F"].reshape(-1, tr["F"].shape[-1]).mean(0); sd_ = tr["F"].reshape(-1, tr["F"].shape[-1]).std(0).clip(0.05)
        for kind in ("pool10", "pool4", "last"):
            Xtr, Xd, Xe = (represent(d["F"], kind, mu, sd_) for d in (tr, dev, ev))
            m2, s2 = Xtr.mean(0), Xtr.std(0).clip(1e-3)
            Xtr, Xd, Xe = (Xtr - m2) / s2, (Xd - m2) / s2, (Xe - m2) / s2
            params, f = fit_logistic(Xtr, tr["y"], CONFIG["logistic"], 0)
            key = f"n{n}/logistic/{kind}"
            results[key] = evaluate(key, f(Xd), f(Xe), dev, ev, CONFIG); results[key]["train_ap"] = ap_score(tr["y"], f(Xtr))
            preds[key] = f(Xe).tolist(); weights[key] = params
            if kind == "last":
                continue
            for seed in CONFIG["seeds"]:
                params, f = fit_mlp(Xtr, tr["y"], CONFIG["mlp"], seed)
                key = f"n{n}/mlp/{kind}/seed{seed}"
                results[key] = evaluate(key, f(Xd), f(Xe), dev, ev, CONFIG); results[key]["train_ap"] = ap_score(tr["y"], f(Xtr))
                preds[key] = f(Xe).tolist()
                if n == 1000:
                    weights[key] = params
            print(f"TRAINED n={n} {kind} {time.monotonic() - t0:.0f}s", flush=True)
    summary = []
    for k, v in results.items():
        ci = v["eval"]["ap_ci95"]
        summary.append({"config": k, "eval_ap": round(v["eval"]["ap"], 4), "eval_ap_ci95": [round(x, 4) for x in ci] if ci else None,
                        "eval_auc": round(v["eval"]["auc"], 4), "eval_balanced_acc": round(v["eval"]["balanced_accuracy"], 4),
                        "eval_recall_take": v["eval"].get("recall_take"), "eval_recall_put": v["eval"].get("recall_put"),
                        "dev_ap": round(v["dev"]["ap"], 4), "train_ap": round(v.get("train_ap", float("nan")), 4) if "train_ap" in v else None})
    C.save_json(a.output / "metrics.json", {"policy": C.POLICY, "config": CONFIG, "train_counts": counts,
        "dev_counts": {"windows": len(dev["y"]), "positives": int(dev["y"].sum()), "parents": int(len(set(dev["parent"])))},
        "eval_counts": {"windows": len(ev["y"]), "positives": int(ev["y"].sum()), "parents": int(len(set(ev["parent"])))},
        "results": results, "summary": summary, "seconds": round(time.monotonic() - t0, 1),
        "notes": ["Weak FHO-derived labels, not visual gold.", "Threshold chosen on dev only; eval scored once.",
                  "pool = mean, max, last-first and mean |frame diff| of frozen per-frame features."]})
    with (a.output / "eval_predictions.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); keys = [k for k in preds if k != "window_id"]
        w.writerow(["window_id", "label", "action", "canonical_parent"] + keys)
        for i, r in enumerate(ev["rows"]):
            w.writerow([r["window_id"], r["label"], r["action"], r["canonical_parent"]] + [round(preds[k][i], 6) for k in keys])
    np.savez(a.output / "head_weights.npz", **{k.replace("/", "__") + "__" + p: np.asarray(v) for k, ps in weights.items() for p, v in ps.items()})
    print("TRAIN_EVAL_DONE", round(time.monotonic() - t0, 1), "s", flush=True)


if __name__ == "__main__":
    main()
