#!/usr/bin/env python3
"""Read-only export v2: same as v1 plus a RELAXED eligibility rule, flagged per action.

strict  (v1): parent/group not excluded, clip in LTA train only, consistent LTA parent, redacted false.
relaxed (v2): parent/group not excluded (all official val/test holdouts + historical reservations,
              group closure), redacted false, and IF the clip appears in LTA it must be train-only with
              a consistent parent. Clips absent from LTA are allowed (flag not_in_lta_index).
Rows are written for every relaxed-eligible action with field eligibility = strict | relaxed_only.
"""
import argparse, gzip, json, sys, time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit_annotations as A
import export_candidates_v1 as V1

SCHEMA = "robomemo-fho-candidates-v2"


def export(args):
    started = time.monotonic()
    ann, out = args.annotations, args.output
    if out.exists() and any(out.iterdir()):
        raise ValueError("Output directory is not empty")
    out.mkdir(parents=True, exist_ok=True)
    audits = {}
    metadata = args.metadata or ann.parent.parent / "ego4d.json"
    blocked, group_map, _ = V1.load_isolation(ann, metadata, args.policy, audits, args.parser)
    index = V1.lta_index(ann, audits, args.parser)
    vp = "videos.item"; ip = vp + ".annotated_intervals.item"; ap = ip + ".narrated_actions.item"
    counts, reasons = Counter(), Counter()
    clips, parents = {}, {"strict": set(), "relaxed_only": set()}
    video = interval = action = None
    rows_path = out / "fho_candidates_v2.jsonl.gz"
    print("FHO main: exporting strict + relaxed eligible actions", flush=True)
    with gzip.open(rows_path, "wt", encoding="utf-8") as gz:
        def flush_video():
            parent = video.get("video_uid")
            related = A.closure({parent}, group_map) if isinstance(parent, str) else set()
            for iv in video["intervals"]:
                uid = iv["fields"].get("clip_uid")
                idx = index.get(uid)
                n = len(iv["actions"])
                hard = []
                if not isinstance(parent, str) or not isinstance(uid, str): hard.append("missing_identity")
                if related & blocked: hard.append("excluded_parent_or_related_group")
                if iv["fields"].get("redacted") is not False: hard.append("redaction_not_explicitly_false")
                if idx is not None:
                    if idx["splits"] & {"val", "test"}: hard.append("official_lta_holdout_clip")
                    if idx["parents"] != {parent}: hard.append("missing_or_conflicting_lta_parent_mapping")
                if hard:
                    for r in hard: reasons[r] += n
                    continue
                elig = "strict" if (idx is not None and idx["splits"] == {"train"}) else "relaxed_only"
                parents[elig].add(parent)
                canon = sorted(related) if related else [parent]
                c = clips.setdefault(uid, {"clip_uid": uid, "video_uid": parent, "related_parent_ids": canon,
                                           "eligibility": elig, "in_lta_index": idx is not None, "actions": 0})
                c["actions"] += n
                for a in iv["actions"]:
                    verb = a.get("structured_verb") or a.get("_structured_verb")
                    a["groups"] = A.groups(verb)
                    a.update(video_uid=parent, clip_uid=uid, related_parent_ids=canon, eligibility=elig,
                             in_lta_index=idx is not None, interval_start_sec=iv["fields"].get("start_sec"),
                             interval_end_sec=iv["fields"].get("end_sec"))
                    gz.write(json.dumps(a, ensure_ascii=False, allow_nan=False) + "\n")
                    counts[elig + "/actions"] += 1
                    for g in a["groups"]: counts[elig + "/" + g] += 1
        for p, ev, val in A.stream(ann / "fho_main.json", audits, args.parser):
            if p == vp and ev == "start_map":
                video = {"intervals": []}
            elif p == ip and ev == "start_map":
                interval = {"fields": {}, "actions": []}
            elif p == ap and ev == "start_map":
                action = {"spatial_counts": Counter(), "object_labels": []}
            elif p == ap and ev == "end_map":
                action["spatial_counts"] = dict(action["spatial_counts"]); interval["actions"].append(action); action = None
            elif p == ip and ev == "end_map":
                video["intervals"].append(interval); interval = None
            elif p == vp and ev == "end_map":
                flush_video(); video = None
            elif action is not None and p.startswith(ap + "."):
                s = p[len(ap) + 1:]
                if A.scalar_event(ev):
                    if s in V1.KEEP: action[s] = val
                    for ns in ("critical_frames", "clip_critical_frames"):
                        if s.startswith(ns + ".") and s[len(ns) + 1:] in A.CRITICAL:
                            action.setdefault(ns, {})[s[len(ns) + 1:]] = val
                    if s == "frames.item.frame_type": action["spatial_counts"]["frames_" + str(val)] += 1
                    if s == "frames.item.boxes.item.object_type": action["spatial_counts"]["boxes_" + str(val)] += 1
                    if s == "frames.item.boxes.item.structured_noun" and isinstance(val, str) and val not in action["object_labels"] and len(action["object_labels"]) < 8:
                        action["object_labels"].append(val)
            elif interval is not None and A.scalar_event(ev) and p.startswith(ip + "."):
                s = p[len(ip) + 1:]
                if "." not in s: interval["fields"][s] = val
            elif video is not None and A.scalar_event(ev) and p == vp + ".video_uid":
                video["video_uid"] = val
    (out / "eligible_clips_v2.json").write_text(json.dumps(sorted(clips.values(), key=lambda c: c["clip_uid"]), indent=1) + "\n", encoding="utf-8")
    summary = {"schema": SCHEMA, "created_utc": datetime.now(timezone.utc).isoformat(), "status": "complete",
               "counts": dict(sorted(counts.items())), "excluded_action_reasons": dict(reasons),
               "clips": dict(Counter(c["eligibility"] for c in clips.values())),
               "parents": {k: len(v) for k, v in parents.items()},
               "parents_relaxed_only_not_in_strict": len(parents["relaxed_only"] - parents["strict"]),
               "blocked_parents_including_groups": len(blocked), "policy_sha256": V1.sha(args.policy),
               "rows_sha256": V1.sha(rows_path), "files_read": audits, "elapsed_seconds": round(time.monotonic() - started, 2)}
    (out / "export_summary_v2.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print("EXPORT_V2 strict actions", counts["strict/actions"], "relaxed_only actions", counts["relaxed_only/actions"],
          "| new parents", summary["parents_relaxed_only_not_in_strict"], flush=True)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--annotations", type=Path, required=True)
    ap.add_argument("--metadata", type=Path)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--policy", type=Path, default=Path(__file__).resolve().parent / "protocol/exclusion_policy.json")
    ap.add_argument("--parser", choices=("auto", "stdlib", "ijson"), default="auto")
    return export(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
