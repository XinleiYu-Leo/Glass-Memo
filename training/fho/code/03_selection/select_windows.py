"""Deterministic window selection for fho_transfer_v1 (draft policy). Pure stdlib.

Input : fho_candidates.jsonl.gz from export_candidates.py (robomemo-fho-candidates-v1)
Output: candidates_labeled.jsonl (every usable window with label + reason),
        split_manifest.json, train_{200,500,1000}.json (nested), dev.json, eval.json,
        clips_to_download.txt, selection_report.json
Labels are annotation-derived weak labels, not visual gold.
"""
import argparse, gzip, hashlib, json, random
from collections import Counter, defaultdict
from pathlib import Path

POLICY = "fho_transfer_v1_draft3"
SALT = "robomemo-fho-transfer-v1:"
FPS = 30.0
WIN = 1.5
SLACK = 0.25
POS = ("take_(pick,_grab,_get)", "put_(place,_leave,_drop)")
NEG = {"cut_(trim,_slice,_chop)", "clean_(sweep,_scrub,_mop,_dust)", "wash", "wipe", "open", "close",
       "adjust_(regulate,_increase/reduce,_change)", "turn_(spin,_rotate,_flip,_turn_over)", "touch", "paint",
       "pour", "apply_(spread,_smear)", "dip", "knit", "smooth", "press", "operate_(use,_dial,_click-button)",
       "turn_on_(switch_on,_start,_light)", "turn_off_(turn_off,_switch_off)", "tighten", "loosen", "draw",
       "spray", "scrape", "screw", "mix", "sew_(weave,_stitch,_sew)", "iron", "crochet", "squeeze", "drill",
       "mark", "hit_(knock,_hit,_hammer)", "scoop", "inspect_(check,_look,_examine,_view)", "mold", "shake"}
# Anything not in POS or NEG (incl. missing verbs) blocks nearby negatives as ambiguous.
CAPS = {"train": (4, 4), "dev": (3, 3), "eval": (3, 3)}


def verb(r):
    return r.get("structured_verb") or r.get("_structured_verb")


def valid(r):
    return (r.get("is_valid_action") is True and r.get("is_partial") is False
            and r.get("is_rejected") is False and r.get("is_invalid_annotation") is False)


def keyframes(r):
    c = r.get("clip_critical_frames") or {}
    pre, pnr, post = c.get("pre_frame"), c.get("pnr_frame"), c.get("post_frame")
    if not all(isinstance(x, (int, float)) for x in (pre, pnr, post)) or not pre <= pnr <= post:
        return None
    return pre / FPS, pnr / FPS, post / FPS


def split_of(canon):
    b = int(hashlib.sha256((SALT + canon).encode()).hexdigest(), 16) % 100
    return "train" if b < 70 else "dev" if b < 80 else "eval"


def busy_interval(r):
    k = keyframes(r)
    if k:
        return k[0] - 0.5, k[2] + 0.5
    t = r.get("clip_narration_timestamp_sec")
    if isinstance(t, (int, float)):
        return t - 2.0, t + 2.0
    s, e = r.get("clip_start_sec"), r.get("clip_end_sec")
    return (s, e) if isinstance(s, (int, float)) and isinstance(e, (int, float)) else (None, None)


def label_windows(rows):
    by_clip = defaultdict(list)
    for r in rows:
        by_clip[r["clip_uid"]].append(r)
    out, reasons = [], Counter()
    for clip, rs in by_clip.items():
        blockers = [(busy_interval(r), r["uid"]) for r in rs if verb(r) not in NEG]
        for r in rs:
            v = verb(r)
            if v not in POS and v not in NEG:
                continue
            kind = "pos" if v in POS else "neg"
            if not valid(r):
                reasons[kind + "/not_explicitly_valid"] += 1; continue
            k = keyframes(r)
            if not k:
                reasons[kind + "/keyframes_missing_or_unordered"] += 1; continue
            pre, pnr, post = k
            if kind == "pos":
                # positive: window ends just after POST and must contain PNR (completed transfer)
                t = post + SLACK
                if pnr < t - WIN:
                    reasons["pos/transfer_longer_than_window"] += 1; continue
            else:
                # negative: window lies inside the other action's own PRE..POST span (action ongoing)
                t = min(post + SLACK, max(pnr + 0.75, pre + WIN))
                if t - WIN < pre - 0.5:
                    reasons["neg/action_shorter_than_window"] += 1; continue
            if t - WIN < 0:
                reasons[kind + "/window_before_clip_start"] += 1; continue
            if kind == "neg":
                lo, hi = t - WIN, t
                if any(a is not None and b is not None and a <= hi and b >= lo and u != r["uid"] for (a, b), u in blockers):
                    reasons["neg/overlaps_transfer_or_ambiguous_action"] += 1; continue
            canon = min(r["related_parent_ids"])
            out.append({"window_id": r["uid"], "label": 1 if kind == "pos" else 0, "verb": v,
                        "action": "take" if v == POS[0] else "put" if v == POS[1] else "other",
                        "video_uid": r["video_uid"], "clip_uid": clip, "canonical_parent": canon,
                        "split": split_of(canon), "decision_time_sec": round(t, 6),
                        "window_start_sec": round(t - WIN, 6), "pre_sec": round(pre, 6), "pnr_sec": round(pnr, 6),
                        "post_sec": round(post, 6), "object_labels": r.get("object_labels", []),
                        "label_source": "ego4d_fho_annotation_weak", "policy": POLICY})
            reasons[kind + "/usable"] += 1
    return out, reasons


def pick(windows, split, seed):
    """Per-parent caps, prefer few clips; returns ordered list (balanced, deterministic)."""
    cap_pos, cap_neg = CAPS[split]
    rng = random.Random(seed)
    per = defaultdict(lambda: {1: [], 0: []})
    for w in windows:
        if w["split"] == split:
            per[w["canonical_parent"]][w["label"]].append(w)
    chosen = {1: [], 0: []}
    for parent in sorted(per):
        for lab, cap in ((1, cap_pos), (0, cap_neg)):
            items = sorted(per[parent][lab], key=lambda w: (w["clip_uid"], w["decision_time_sec"]))
            # spread in time: keep windows >= 3 s apart within a clip
            kept, last = [], {}
            for w in items:
                if w["clip_uid"] in last and w["decision_time_sec"] - last[w["clip_uid"]] < 3.0:
                    continue
                kept.append(w); last[w["clip_uid"]] = w["decision_time_sec"]
            # prefer the parent's clip with most usable windows (fewer downloads)
            clip_count = Counter(w["clip_uid"] for w in per[parent][1] + per[parent][0])
            kept.sort(key=lambda w: (-clip_count[w["clip_uid"]], w["clip_uid"], w["decision_time_sec"]))
            if lab == 1:  # balance take/put inside a parent when possible
                takes = [w for w in kept if w["action"] == "take"]; puts = [w for w in kept if w["action"] == "put"]
                inter = [x for pair in zip(takes, puts) for x in pair] + takes[len(puts):] + puts[len(takes):]
                kept = inter
            chosen[lab].extend(kept[:cap])
    for lab in (1, 0):
        rng.shuffle(chosen[lab])
    return chosen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=20261027)
    a = ap.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    raw = a.candidates.read_bytes()
    rows = [json.loads(l) for l in gzip.decompress(raw).decode("utf-8").splitlines() if l.strip()]
    windows, reasons = label_windows(rows)
    with (a.output / "candidates_labeled.jsonl").open("w", encoding="utf-8") as f:
        for w in windows:
            f.write(json.dumps(w, ensure_ascii=False) + "\n")
    picks = {s: pick(windows, s, a.seed + i) for i, s in enumerate(("train", "dev", "eval"))}
    train_pos, train_neg = picks["train"][1], picks["train"][0]
    nested = {}
    for n in (200, 500, 1000):
        half = n // 2
        if len(train_pos) >= half and len(train_neg) >= half:
            nested[n] = train_pos[:half] + train_neg[:half]
    for n, ws in nested.items():
        (a.output / f"train_{n}.json").write_text(json.dumps(ws, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    for s in ("dev", "eval"):
        m = min(len(picks[s][1]), len(picks[s][0]))
        ws = picks[s][1][:m] + picks[s][0][:m]
        (a.output / f"{s}.json").write_text(json.dumps(ws, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    biggest = nested[max(nested)] if nested else []
    dev = json.loads((a.output / "dev.json").read_text()); ev = json.loads((a.output / "eval.json").read_text())
    clips = sorted({w["clip_uid"] for w in biggest + dev + ev})
    (a.output / "clips_to_download.txt").write_text("\n".join(clips) + "\n", encoding="utf-8")
    parents = {s: sorted({w["canonical_parent"] for w in windows if w["split"] == s}) for s in ("train", "dev", "eval")}
    assert not (set(parents["train"]) & set(parents["dev"])) and not (set(parents["train"]) & set(parents["eval"])) and not (set(parents["dev"]) & set(parents["eval"]))
    (a.output / "split_manifest.json").write_text(json.dumps({"policy": POLICY, "salt": SALT, "rule": "sha256(salt+min(related_parent_ids)) mod 100: <70 train, <80 dev, else eval",
        "parents": parents, "parent_counts": {s: len(v) for s, v in parents.items()}}, indent=1) + "\n", encoding="utf-8")
    report = {"policy": POLICY, "seed": a.seed, "candidates_sha256": hashlib.sha256(raw).hexdigest(),
              "input_rows": len(rows), "window_reasons": dict(sorted(reasons.items())),
              "usable_windows_by_split": {s: dict(Counter(("pos" if w["label"] else "neg") + "/" + w["action"] for w in windows if w["split"] == s).most_common()) for s in ("train", "dev", "eval")},
              "after_caps": {s: {"pos": len(picks[s][1]), "neg": len(picks[s][0])} for s in picks},
              "nested_train_sizes": sorted(nested), "dev_size": len(dev), "eval_size": len(ev),
              "clips_to_download": len(clips),
              "parents_in_selected": {"train_max": len({w["canonical_parent"] for w in biggest}),
                                      "dev": len({w["canonical_parent"] for w in dev}), "eval": len({w["canonical_parent"] for w in ev})},
              "take_put_in_largest_train": dict(Counter(w["action"] for w in biggest)),
              "negative_verbs_in_largest_train": dict(Counter(w["verb"] for w in biggest if w["label"] == 0).most_common(15))}
    (a.output / "selection_report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
