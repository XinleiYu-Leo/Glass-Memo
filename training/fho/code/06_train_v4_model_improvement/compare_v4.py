"""Score every method with one metric code path and run paired, parent-grouped bootstrap comparisons."""
import argparse, csv, json, sys
from collections import defaultdict
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C
import metrics_lib as M


def read(path):
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    keys = [k for k in rows[0] if k not in ("window_id", "label", "action", "canonical_parent")]
    return rows, {k: np.array([float(r[k]) for r in rows]) for k in keys}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--option_a", type=Path, required=True)
    ap.add_argument("--option_b", type=Path)
    ap.add_argument("--selection", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    a = ap.parse_args()
    sel = C.load_selection(a.selection)
    parts = {}
    for part in ("dev", "eval"):
        rows, sc = read(a.option_a / f"{part}_scores_v4.csv")
        ids = [r["window_id"] for r in rows]
        if a.option_b and (a.option_b / f"{part}_scores_finetune.csv").is_file():
            rb, sb = read(a.option_b / f"{part}_scores_finetune.csv")
            pos = {r["window_id"]: i for i, r in enumerate(rb)}
            if set(pos) != set(ids):
                raise ValueError("Option A and B scored different windows")
            order = np.array([pos[w] for w in ids])
            sc.update({k: v[order] for k, v in sb.items()})
        parts[part] = {"rows": rows, "y": np.array([float(r["label"]) for r in rows]), "action": np.array([r["action"] for r in rows]),
                       "parent": np.array([r["canonical_parent"] for r in rows]), "scores": sc}
    dev, ev = parts["dev"], parts["eval"]
    groups = defaultdict(list)
    for k in ev["scores"]:
        groups[k.rsplit("/seed", 1)[0] if "/seed" in k else k].append(k)
    avg = {g: (np.mean([dev["scores"][k] for k in ks], 0), np.mean([ev["scores"][k] for k in ks], 0)) for g, ks in groups.items()}
    table = {}
    for g, (sd, se) in avg.items():
        r = M.evaluate(g, sd, se, dev, ev, M.CONFIG)
        table[g] = {"seeds": len(groups[g]), "eval_ap": r["eval"]["ap"], "eval_ap_ci95": r["eval"]["ap_ci95"], "eval_auc": r["eval"]["auc"],
                    "eval_balanced_accuracy": r["eval"]["balanced_accuracy"], "eval_recall_take": r["eval"]["recall_take"],
                    "eval_recall_put": r["eval"]["recall_put"], "dev_ap": r["dev"]["ap"]}
    y, gp = ev["y"], ev["parent"]; ug = np.unique(gp); idx = {u: np.nonzero(gp == u)[0] for u in ug}
    rng = np.random.default_rng(11)
    draws = [np.concatenate([idx[u] for u in rng.choice(ug, len(ug))]) for _ in range(2000)]
    draws = [t for t in draws if y[t].min() != y[t].max()]
    ref = next((g for g in ("n1652/mlp/app",) if g in avg), max(avg, key=lambda g: table[g]["eval_ap"]))
    pairs = []
    for g in avg:
        for b in (ref, "baseline/pixel_motion"):
            if g == b or b not in avg:
                continue
            for name, fn in (("ap", M.ap_score), ("auc", M.auc_score)):
                d = np.array([fn(y[t], avg[g][1][t]) - fn(y[t], avg[b][1][t]) for t in draws])
                pairs.append({"a": g, "b": b, "metric": name, "diff": float(fn(y, avg[g][1]) - fn(y, avg[b][1])),
                              "ci95": [float(np.percentile(d, 2.5)), float(np.percentile(d, 97.5))], "p_le_0": float(np.mean(d <= 0))})
    a.output.mkdir(parents=True, exist_ok=True)
    C.save_json(a.output / "comparison_v4.json", {"reference": ref, "eval_windows": len(y), "eval_parents": len(ug), "table": table, "paired": pairs})
    for g in sorted(table, key=lambda g: -table[g]["eval_ap"]):
        t = table[g]; print(f"{g:28s} AP {t['eval_ap']:.3f} AUC {t['eval_auc']:.3f} BA {t['eval_balanced_accuracy']:.3f} seeds {t['seeds']}", flush=True)
    print("COMPARE_V4_DONE", flush=True)


if __name__ == "__main__":
    main()
