"""Extend the fho_transfer_v1 selection with relaxed-eligibility sources, keeping every old window.

Old train_1000 / dev / eval stay exactly as they were (so earlier results remain comparable).
New windows are added per split under the same label policy, the same parent-hash split,
the same per-parent caps (counting old windows) and the same 3 s spacing rule.
Additions are balanced so each final set has equal positives and negatives.
"""
import argparse, gzip, hashlib, json, random, sys
from collections import Counter, defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import select_windows as S

POLICY_NOTE = "fho_transfer_v1_draft3 labels; sources = strict + relaxed eligibility (export v2)"


def extend(old_rows, pool, split, cap, seed):
    old_ids = {r["window_id"] for r in old_rows}
    have = defaultdict(Counter)
    last = defaultdict(list)
    for r in old_rows:
        have[r["canonical_parent"]][r["label"]] += 1
        last[(r["clip_uid"], r["label"])].append(r["decision_time_sec"])
    cands = sorted((w for w in pool if w["split"] == split and w["window_id"] not in old_ids),
                   key=lambda w: (w["canonical_parent"], w["clip_uid"], w["label"], w["decision_time_sec"]))
    added = {1: [], 0: []}
    for w in cands:
        k = (w["clip_uid"], w["label"])
        if any(abs(w["decision_time_sec"] - t) < 3.0 for t in last[k]):
            continue
        if have[w["canonical_parent"]][w["label"]] >= cap:
            continue
        have[w["canonical_parent"]][w["label"]] += 1
        last[k].append(w["decision_time_sec"])
        added[w["label"]].append(w)
    rng = random.Random(seed)
    for lab in (1, 0):
        rng.shuffle(added[lab])
    old_pos = sum(r["label"] == 1 for r in old_rows); old_neg = len(old_rows) - old_pos
    # balance the final set: total pos == total neg
    n_pos, n_neg = len(added[1]), len(added[0])
    target = min(old_pos + n_pos, old_neg + n_neg)
    add = added[1][:target - old_pos] + added[0][:target - old_neg]
    return add


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates_v2", type=Path, required=True)
    ap.add_argument("--old_selection", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=20261027)
    a = ap.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    raw = a.candidates_v2.read_bytes()
    rows = [json.loads(l) for l in gzip.decompress(raw).decode("utf-8").splitlines() if l.strip()]
    pool, reasons = S.label_windows(rows)
    old = {n: json.loads((a.old_selection / f"{n}.json").read_text(encoding="utf-8")) for n in ("train_200", "train_500", "train_1000", "dev", "eval")}
    out = {}
    for split, name, cap, seed in (("train", "train_1000", 4, a.seed), ("dev", "dev", 3, a.seed + 1), ("eval", "eval", 3, a.seed + 2)):
        add = extend(old[name], pool, split, cap, seed)
        for w in add:
            w["selection_round"] = "v2_extension"
        out[split] = old[name] + add
    # keep nested ordering: old 200 < 500 < 1000 < all
    for n in ("train_200", "train_500", "train_1000"):
        (a.output / f"{n}.json").write_text(json.dumps(old[n], indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    files = {"train_all": out["train"], "dev": out["dev"], "eval": out["eval"]}
    for n, ws in files.items():
        (a.output / f"{n}.json").write_text(json.dumps(ws, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    (a.output / "eval_v1_ids.json").write_text(json.dumps([r["window_id"] for r in old["eval"]]) + "\n", encoding="utf-8")
    parents = {s: {w["canonical_parent"] for w in ws} for s, ws in out.items()}
    assert not (parents["train"] & parents["dev"]) and not (parents["train"] & parents["eval"]) and not (parents["dev"] & parents["eval"])
    ids = [w["window_id"] for ws in out.values() for w in ws]
    assert len(ids) == len(set(ids)) and None not in ids
    have_dl = set((a.old_selection / "clips_to_download.txt").read_text().split())
    need = sorted({w["clip_uid"] for ws in out.values() for w in ws} - have_dl)
    (a.output / "clips_to_download_v2_new.txt").write_text("\n".join(need) + "\n", encoding="utf-8")
    with (a.output / "candidates_labeled_v2.jsonl").open("w", encoding="utf-8") as f:
        for w in pool:
            f.write(json.dumps(w, ensure_ascii=False) + "\n")
    report = {"note": POLICY_NOTE, "seed": a.seed, "candidates_v2_sha256": hashlib.sha256(raw).hexdigest(),
              "window_reasons": dict(sorted(reasons.items())),
              "sizes": {k: {"windows": len(v), "pos": sum(w["label"] for w in v), "parents": len({w["canonical_parent"] for w in v}),
                            "clips": len({w["clip_uid"] for w in v}), "actions": dict(Counter(w["action"] for w in v))} for k, v in
                        {"train_1000": old["train_1000"], "train_all": out["train"], "dev": out["dev"], "eval_v1": old["eval"], "eval_all": out["eval"]}.items()},
              "new_clips_to_download": len(need)}
    (a.output / "selection_report_v2.json").write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report["sizes"], indent=1), "\nnew clips:", len(need))


if __name__ == "__main__":
    main()
