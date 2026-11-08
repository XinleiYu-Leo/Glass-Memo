#!/usr/bin/env python3
"""Export eligible Ego4D FHO actions (all verbs) for RoboMemo window selection.

Read-only. Uses exactly the eligibility rules of audit_annotations.py v1:
parent/group not excluded, clip in LTA train only, consistent LTA parent mapping,
interval redacted == false. Writes one JSON line per narrated action with flags,
times and keyframes. Labels are NOT assigned here; missing data is never negative.
"""
from __future__ import annotations
import argparse, gzip, hashlib, json, sys, time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit_annotations as A  # unchanged v1 library (stream, closure, groups, ...)

SCHEMA = "robomemo-fho-candidates-v1"
KEEP = set(A.TIMES + A.FLAGS + ("uid", "structured_verb", "_structured_verb", "state_transition", "stage"))


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def load_isolation(ann, metadata, policy_path, audits, engine):
    policy = A.bounded_load(policy_path)
    blocked = set(policy["excluded_parent_ids"])
    group_map = dict(policy.get("group_to_canonical_parent_ids", {}))
    groups, meta_hold = A.inspect_metadata(metadata, audits, engine)
    for g, members in groups.items():
        if g in group_map and set(members) != set(group_map[g]):
            raise ValueError("Official grouped-video membership changed: " + g)
        group_map[g] = members
    blocked.update(meta_hold)
    holdouts = sorted(set(ann.glob("*_val.json")) | set(ann.glob("*_test_unannotated.json")))
    need = {"fho_lta_val.json", "fho_lta_test_unannotated.json", "vq_val.json", "vq_test_unannotated.json"}
    if not need <= {p.name for p in holdouts}:
        raise ValueError("Required official holdout files missing")
    for p in holdouts:
        print("Holdout identities:", p.name, flush=True)
        ids = A.collect_holdout(p, audits, engine)
        if not ids:
            raise ValueError("No parent identities in holdout: " + p.name)
        blocked.update(ids)
    return A.closure(blocked, group_map), group_map, policy


def lta_index(ann, audits, engine):
    taxonomy = A.bounded_load(ann / "fho_lta_taxonomy.json")
    index = {}
    for split, name in (("train", "fho_lta_train.json"), ("val", "fho_lta_val.json"), ("test", "fho_lta_test_unannotated.json")):
        print("LTA rows:", name, flush=True)
        A.inspect_lta(ann / name, split, index, taxonomy, set(), {}, set(), audits, engine)
    return index


def export(args):
    started = time.monotonic()
    ann = args.annotations
    out = args.output
    if out.exists() and any(out.iterdir()):
        raise ValueError("Output directory is not empty")
    out.mkdir(parents=True, exist_ok=True)
    audits = {}
    metadata = args.metadata or ann.parent.parent / "ego4d.json"
    blocked, group_map, policy = load_isolation(ann, metadata, args.policy, audits, args.parser)
    index = lta_index(ann, audits, args.parser)
    vp = "videos.item"; ip = vp + ".annotated_intervals.item"; ap = ip + ".narrated_actions.item"
    counts, reasons = Counter(), Counter()
    video = interval = action = None
    rows_path = out / "fho_candidates.jsonl.gz"
    clips = {}
    print("FHO main: exporting eligible actions", flush=True)
    with gzip.open(rows_path, "wt", encoding="utf-8") as gz:
        def flush_video():
            parent = video.get("video_uid")
            related = A.closure({parent}, group_map) if isinstance(parent, str) else set()
            for iv in video["intervals"]:
                uid = iv["fields"].get("clip_uid")
                idx = index.get(uid, {})
                splits = idx.get("splits", set())
                why = []
                if not isinstance(parent, str) or not isinstance(uid, str): why.append("missing_identity")
                if related & blocked: why.append("excluded_parent_or_related_group")
                if "train" not in splits: why.append("not_in_lta_train")
                if splits & {"val", "test"}: why.append("official_lta_holdout_clip")
                if idx.get("parents", set()) != {parent}: why.append("missing_or_conflicting_lta_parent_mapping")
                if iv["fields"].get("redacted") is not False: why.append("redaction_not_explicitly_false")
                n = len(iv["actions"])
                if why:
                    for r in why: reasons[r] += n
                    continue
                canon = sorted(related) if related else [parent]
                c = clips.setdefault(uid, {"clip_uid": uid, "video_uid": parent, "related_parent_ids": canon, "actions": 0})
                c["actions"] += n
                for a in iv["actions"]:
                    verb = a.get("structured_verb") or a.get("_structured_verb")
                    a["groups"] = A.groups(verb)
                    a["video_uid"], a["clip_uid"] = parent, uid
                    a["related_parent_ids"] = canon
                    a["interval_start_sec"] = iv["fields"].get("start_sec")
                    a["interval_end_sec"] = iv["fields"].get("end_sec")
                    gz.write(json.dumps(a, ensure_ascii=False, allow_nan=False) + "\n")
                    counts["exported_actions"] += 1
                    for g in a["groups"]: counts["exported/" + g] += 1
        for p, ev, val in A.stream(ann / "fho_main.json", audits, args.parser):
            if p == vp and ev == "start_map":
                video = {"intervals": []}
            elif p == ip and ev == "start_map":
                interval = {"fields": {}, "actions": []}
            elif p == ap and ev == "start_map":
                action = {"spatial_counts": Counter(), "object_labels": []}
            elif p == ap and ev == "end_map":
                action["spatial_counts"] = dict(action["spatial_counts"])
                interval["actions"].append(action); action = None
            elif p == ip and ev == "end_map":
                video["intervals"].append(interval); interval = None
            elif p == vp and ev == "end_map":
                flush_video(); video = None
            elif action is not None and p.startswith(ap + "."):
                s = p[len(ap) + 1:]
                if A.scalar_event(ev):
                    if s in KEEP: action[s] = val
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
    if not counts["exported_actions"]:
        raise ValueError("No eligible actions exported; review required")
    (out / "eligible_clips.json").write_text(json.dumps(sorted(clips.values(), key=lambda c: c["clip_uid"]), indent=1) + "\n", encoding="utf-8")
    summary = {"schema": SCHEMA, "created_utc": datetime.now(timezone.utc).isoformat(), "status": "complete",
               "exported_actions": counts["exported_actions"], "group_counts": {k: v for k, v in counts.items() if k.startswith("exported/")},
               "eligible_clips": len(clips), "eligible_parents": len({c["video_uid"] for c in clips.values()}),
               "excluded_action_reasons": dict(reasons), "blocked_parents_including_groups": len(blocked),
               "policy_sha256": sha(args.policy), "rows_sha256": sha(rows_path),
               "files_read": audits, "elapsed_seconds": round(time.monotonic() - started, 2),
               "notes": ["No labels assigned. Groups are verb-text tags only.", "Clip frame indices are 30 fps clip frames; verify with decoded video before use."]}
    (out / "export_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print("EXPORT_STATUS: complete actions", counts["exported_actions"], "clips", len(clips), flush=True)
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
