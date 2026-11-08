"""Freeze new RoboMemo sources from local official annotations; no model or training.

Writes only selection_frozen.json, exclusion_audit.json and download_uids.txt to
--output. Never reads teacher outputs, credentials, images or video content.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import csv
import hashlib
import json
import math
from pathlib import Path
import random
import re

from export_sources import write_once

SEED = 20261026
TOL = 1e-6
UID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
TAKE = "take_(pick,_grab,_get)"
PUT = "put_(place,_leave,_drop)"
# Frozen subgroup quotas: (name, stratum, train, dev). No data-dependent relaxation.
SPLIT_GROUP_QUOTAS = (
    ("pickup", "candidate_positive", 12, 4),
    ("placement", "candidate_positive", 12, 4),
    ("wipe_or_clean", "candidate_hard_negative", 4, 2),
    ("mix", "candidate_hard_negative", 4, 2),
    ("sustained_handling_or_operation", "candidate_hard_negative", 4, 1),
    ("door_or_drawer", "candidate_hard_negative", 4, 1),
    ("proxy_near_left_boundary", "candidate_boundary", 4, 1),
    ("proxy_near_right_boundary", "candidate_boundary", 4, 1),
)
GROUPS = tuple((g, s, tr + dv) for g, s, tr, dv in SPLIT_GROUP_QUOTAS)
SPLIT_QUOTAS = {
    "train": {"candidate_positive": 24, "candidate_hard_negative": 16, "candidate_boundary": 8},
    "dev": {"candidate_positive": 8, "candidate_hard_negative": 6, "candidate_boundary": 2},
}
DESIRED = {k: sum(q[k] for q in SPLIT_QUOTAS.values()) for k in SPLIT_QUOTAS["train"]}
SOURCE_URLS = [
    "https://ego4d-data.org/docs/data/annotations-schemas/",
    "https://ego4d-data.org/docs/data/videos/",
    "https://ego4d-data.org/docs/updates/",
    "https://raw.githubusercontent.com/EGO4D/forecasting/main/ego4d_forecasting/datasets/ptv_dataset_helper.py",
]


def require(value, message):
    if not value:
        raise ValueError(message)


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def json_read(path, sources):
    raw = path.read_bytes()
    sources[path.name] = {"path": str(path), "sha256": digest_bytes(raw), "bytes": len(raw)}
    return json.loads(raw)


def parent_references(value):
    result = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ("video_uid", "video_id") and isinstance(child, str):
                result.add(child)
            elif key == "video_metadata" and isinstance(child, dict):
                result.update(key for key in child if UID.fullmatch(key) or key.startswith("grp-"))
            if isinstance(child, (dict, list)):
                result.update(parent_references(child))
    elif isinstance(value, list):
        for child in value:
            result.update(parent_references(child))
    return result


def grouped_parent_resolver(meta_videos):
    """Resolve groups from explicit official members, never by stripping grp-."""
    groups = {}
    memo = {}

    def resolve(uid, visiting=frozenset()):
        if uid in memo:
            return memo[uid]
        require(uid in meta_videos, "Official parent ID absent from metadata: " + uid)
        require(uid not in visiting, "Cycle in official grouped-video metadata: " + uid)
        row = meta_videos[uid]
        if row.get("is_grouped") is True:
            members = row.get("group_video_uids")
            require(isinstance(members, list) and len(members) > 1,
                    "Grouped parent missing explicit members: " + uid)
            require(len(members) == len(set(members)), "Duplicate grouped parent member: " + uid)
            leaves = set()
            for member in members:
                leaves.update(resolve(member, visiting | {uid}))
            groups[uid] = sorted(leaves)
        else:
            require(UID.fullmatch(uid) and not row.get("group_video_uids"),
                    "Unknown noncanonical parent format: " + uid)
            leaves = {uid}
        memo[uid] = leaves
        return leaves

    for uid, row in meta_videos.items():
        if row.get("is_grouped") is True:
            resolve(uid)

    def closure(parents):
        result = set(parents)
        while True:
            before = len(result)
            for members in groups.values():
                if result.intersection(members):
                    result.update(members)
            if len(result) == before:
                return result

    return resolve, closure, groups


def unique_map(rows, key):
    result = {}
    for row in rows:
        require(row[key] not in result, "Duplicate official mapping key: " + row[key])
        result[row[key]] = row
    return result


def stable_key(item):
    row = item["raw_annotation"]
    return (item["video_uid"], item["clip_uid"], item["action_index"],
            row["action_clip_start_sec"], row["action_clip_end_sec"], row["verb"], row["noun"])


def group_names(row):
    verb, noun = row["verb"], row["noun"]
    names = []
    if verb == TAKE:
        names.append("pickup")
    if verb == PUT:
        names.append("placement")
    if verb in (TAKE, PUT):
        names.extend(("proxy_near_left_boundary", "proxy_near_right_boundary"))
    if verb in ("wipe", "clean_(sweep,_scrub,_mop,_dust)"):
        names.append("wipe_or_clean")
    if verb == "mix":
        names.append("mix")
    if verb in ("hold_(support,_grip,_grasp)", "operate_(use,_dial,_click-button)"):
        names.append("sustained_handling_or_operation")
    if verb in ("open", "close") and noun in ("door", "drawer"):
        names.append("door_or_drawer")
    return names


def make_candidate(row, annotation_index, mapping, group, stratum):
    midpoint = (row["action_clip_start_sec"] + row["action_clip_end_sec"]) / 2
    if group == "proxy_near_left_boundary":
        decision_tick = math.floor(midpoint * 6) + 9
    elif group == "proxy_near_right_boundary":
        decision_tick = math.ceil(midpoint * 6)
    else:
        decision_tick = math.ceil((midpoint + 0.75) * 6)
    end = decision_tick / 6
    start = (decision_tick - 9) / 6
    duration = float(mapping["parent_end_sec"]) - float(mapping["parent_start_sec"])
    if start < 0 or end >= duration - TOL:
        return None
    return {
        "clip_uid": row["clip_uid"], "video_uid": row["video_uid"],
        "stratum": stratum, "selection_subgroup": group,
        "raw_annotation": row, "annotation_file": "fho_lta_train.json",
        "annotation_row_index": annotation_index, "action_index": row["action_idx"],
        "event_identity": [row["video_uid"], row["clip_uid"], row["action_idx"]],
        "stratum_is_not_true_label": True,
        "candidate_rationale": {
            "subgroup": group, "annotation_verb": row["verb"], "annotation_noun": row["noun"],
            "meaning": "Annotation-based sampling rationale only; target event presence and visibility remain unknown.",
        },
        "anchor": {"kind": "lta_context_window_midpoint_proxy", "clip_time_sec": midpoint,
                   "parent_time_sec": row["clip_parent_start_sec"] + midpoint,
                   "is_observed_completion": False, "is_pnr_annotation": False,
                   "boundary_claim": "Proxy-time boundary only, not an observed completion/visibility boundary." if stratum == "candidate_boundary" else None},
        "target_start_sec": start, "target_end_sec": end, "decision_time_sec": end,
        "decision_tick_sixths": decision_tick, "audit_start_sec": max(0.0, end - 6),
        "clip_parent_start_sec": row["clip_parent_start_sec"],
        "clip_parent_end_sec": row["clip_parent_end_sec"],
        "target_parent_start_sec": row["clip_parent_start_sec"] + start,
        "target_parent_end_sec": row["clip_parent_start_sec"] + end,
        "s3_path": mapping["s3_path"], "download_dataset": "clips",
        "repeat_selected": False,
    }


def run(args):
    names = ("selection_frozen.json", "download_uids.txt", "exclusion_audit.json")
    require(not any((args.output / name).exists() for name in names), "Frozen output exists; refusing overwrite or reselection")
    sources = {}
    protocol = json_read(args.protocol, sources)
    excluded = json_read(args.exclusions, sources)
    old_train, old_dev = set(excluded["declared_train_parents"]), set(excluded["declared_dev_parents"])
    require(len(old_train) == 18 and len(old_dev) == 6 and not old_train & old_dev, "Expected disjoint 18/6 old parents")
    reservation = json_read(args.reservation, sources)
    diagnostic = set(reservation["parent_ids"])
    diagnostic_related = set(reservation["known_related_parent_ids"])
    require(len(diagnostic) == 24 and diagnostic <= diagnostic_related,
            "Expected all 24 prior diagnostic parents and known group components")
    require(reservation.get("exclude_from_next_training_round") is True, "Diagnostic reservation is not active")
    metadata = json_read(args.metadata, sources)
    meta_clips = unique_map(metadata["clips"], "clip_uid")
    meta_videos = unique_map(metadata["videos"], "video_uid")
    resolve_parent, group_closure, group_mapping = grouped_parent_resolver(meta_videos)
    raw_metadata_holdout = {uid for uid, row in meta_videos.items()
                            if any(value in ("val", "test") for key, value in row.items() if key.startswith("split_"))}
    metadata_holdout = set().union(*(resolve_parent(uid) for uid in raw_metadata_holdout))
    raw_manifest = args.manifest.read_bytes()
    sources["clips_manifest.csv"] = {"path": str(args.manifest), "sha256": digest_bytes(raw_manifest), "bytes": len(raw_manifest)}
    mapping = unique_map(list(csv.DictReader(raw_manifest.decode("utf-8-sig").splitlines())), "exported_clip_uid")
    if args.reserved_clips.is_dir():
        reserved_raw = ("\n".join(sorted(p.stem for p in args.reserved_clips.glob("*.mp4"))) + "\n").encode("ascii")
    else:
        reserved_raw = args.reserved_clips.read_bytes()
    sources["reserved_vq_clip_ids.txt"] = {"path": str(args.reserved_clips), "sha256": digest_bytes(reserved_raw), "bytes": len(reserved_raw)}
    reserved = set(reserved_raw.decode("utf-8-sig").split())
    require(len(reserved) == 60 and all(UID.fullmatch(uid) for uid in reserved), "Expected 60 reserved VQ clip IDs")
    reserved_parents = set()
    for uid in reserved:
        require(uid in mapping and uid in meta_clips, "Reserved clip has no official mapping: " + uid)
        require(mapping[uid]["parent_video_uid"] == meta_clips[uid]["video_uid"], "Reserved parent mapping disagreement")
        reserved_parents.add(mapping[uid]["parent_video_uid"])
    official = {"val": set(), "test": set()}
    file_audits = {}
    for split, pattern in (("val", "*_val.json"), ("test", "*_test_unannotated.json")):
        paths = sorted(args.annotations.glob(pattern))
        require(paths, "Missing official holdout annotations: " + pattern)
        require(any(p.name == "vq_" + ("val.json" if split == "val" else "test_unannotated.json") for p in paths), "Missing VQ holdout file")
        require(any(p.name == "fho_lta_" + ("val.json" if split == "val" else "test_unannotated.json") for p in paths), "Missing LTA holdout file")
        for path in paths:
            data = json_read(path, sources)
            raw_parents = parent_references(data)
            parents = set().union(*(resolve_parent(uid) for uid in raw_parents))
            require(parents, "Cannot resolve parent schema in " + path.name)
            official[split].update(parents)
            file_audits[path.name] = {"split": split, "parent_count": len(parents), "parent_ids": sorted(parents),
                                     "raw_parent_ids": sorted(raw_parents), "group_ids": sorted(raw_parents & group_mapping.keys()),
                                     "root_keys": sorted(data), "sha256": sources[path.name]["sha256"]}
            if path.name == "vq_val.json":
                vq_val_clips = {clip["clip_uid"] for video in data["videos"] for clip in video["clips"]}
                require(reserved <= vq_val_clips, "Reserved 60 clips are not all official VQ validation clips")
    require(reserved_parents <= official["val"], "Reserved parents missing from official validation exclusion")
    exclusion_sets = {
        "declared_train_parent_ids": sorted(old_train), "declared_dev_parent_ids": sorted(old_dev),
        "official_val_parent_ids": sorted(official["val"]), "official_test_parent_ids": sorted(official["test"]),
        "metadata_holdout_parent_ids": sorted(metadata_holdout),
        "diagnostic_parent_ids": sorted(diagnostic), "diagnostic_known_related_parent_ids": sorted(diagnostic_related),
        "reserved_clip_ids": sorted(reserved), "reserved_parent_ids": sorted(reserved_parents),
    }
    blocked_before_closure = old_train | old_dev | official["val"] | official["test"] | metadata_holdout | reserved_parents | diagnostic | diagnostic_related
    for parent in blocked_before_closure:
        require(parent in meta_videos, "Excluded canonical parent absent from metadata: " + parent)
        require(resolve_parent(parent) == {parent}, "Exclusion must declare a canonical parent: " + parent)
    blocked = group_closure(blocked_before_closure)
    group_resolution = {"metadata_sha256": sources[args.metadata.name]["sha256"],
                        "group_count": len(group_mapping),
                        "group_to_canonical_parent_ids": group_mapping,
                        "blocked_parent_ids_before_group_closure": sorted(blocked_before_closure),
                        "additional_blocked_parent_ids_from_group_closure": sorted(blocked - blocked_before_closure),
                        "raw_metadata_holdout_ids": sorted(raw_metadata_holdout),
                        "resolution_method": "Official ego4d.json videos[is_grouped=true].group_video_uids; validate every member, expand recursively, take transitive group-member closure. Never infer by stripping grp-.",
                        "official_source": "https://ego4d-data.org/docs/updates/"}
    exclusion_sets["excluded_parent_ids"] = sorted(blocked)
    train = json_read(args.annotations / "fho_lta_train.json", sources)
    taxonomy = json_read(args.annotations / "fho_lta_taxonomy.json", sources)
    require(train["split"] == "train" and isinstance(train["clips"], list), "Expected official LTA train schema")
    groups = {name: [] for name, _, _ in GROUPS}
    group_to_stratum = {name: stratum for name, stratum, _ in GROUPS}
    rejection = Counter()
    exclusion_reasons = Counter()
    durations = Counter()
    seen_events = set()
    eligible_parents = set()
    max_frame_second_difference = 0.0
    for index, row in enumerate(train["clips"]):
        uid, parent = row["clip_uid"], row["video_uid"]
        require(UID.fullmatch(uid) and UID.fullmatch(parent), "Malformed train clip/parent identity")
        for reason, parents in (("old_train", old_train), ("old_dev", old_dev), ("official_val", official["val"]),
                                ("official_test", official["test"]), ("metadata_holdout", metadata_holdout), ("reserved_parent", reserved_parents), ("diagnostic_reserved", diagnostic_related)):
            if parent in parents:
                exclusion_reasons[reason] += 1
        durations[round(row["action_clip_end_sec"] - row["action_clip_start_sec"], 6)] += 1
        if parent in blocked or uid in reserved:
            rejection["excluded_parent_or_reserved_clip"] += 1
            continue
        if uid not in mapping or uid not in meta_clips or parent not in meta_videos:
            rejection["missing_official_clip_or_parent_mapping"] += 1
            continue
        cm, mm = mapping[uid], meta_clips[uid]
        if cm["parent_video_uid"] != parent or mm["video_uid"] != parent:
            rejection["official_parent_mapping_disagreement"] += 1
            continue
        start, end = row["clip_parent_start_sec"], row["clip_parent_end_sec"]
        if any(abs(float(value) - expected) > TOL for value, expected in (
                (cm["parent_start_sec"], start), (cm["parent_end_sec"], end),
                (mm["video_start_sec"], start), (mm["video_end_sec"], end))):
            rejection["official_time_origin_mapping_disagreement"] += 1
            continue
        if "FHO" not in cm["benchmarks"] or not cm["s3_path"].startswith("s3://") or not cm["s3_path"].endswith("/" + uid + ".mp4"):
            rejection["unexpected_download_manifest_entry"] += 1
            continue
        sec_fields = ("action_clip_start_sec", "action_clip_end_sec", "clip_parent_start_sec", "clip_parent_end_sec", "interval_start_sec", "interval_end_sec")
        if not all(isinstance(row[key], (int, float)) and math.isfinite(row[key]) for key in sec_fields):
            rejection["invalid_annotation_time"] += 1
            continue
        a, b = row["action_clip_start_sec"], row["action_clip_end_sec"]
        if not (0 <= a < b <= end - start + TOL):
            rejection["annotation_outside_canonical_clip"] += 1
            continue
        if type(row["action_idx"]) is not int or row["action_idx"] < 0:
            rejection["invalid_action_index"] += 1
            continue
        if taxonomy["verbs"][row["verb_label"]] != row["verb"] or taxonomy["nouns"][row["noun_label"]] != row["noun"]:
            rejection["taxonomy_disagreement"] += 1
            continue
        frame_second_difference = max(abs(row[key.replace("_sec", "_frame")] / 30 - row[key])
                                      for key in ("action_clip_start_sec", "action_clip_end_sec"))
        max_frame_second_difference = max(max_frame_second_difference, frame_second_difference)
        if frame_second_difference > 1 / 30 + TOL:
            rejection["frame_seconds_not_consistent_with_canonical_30fps"] += 1
            continue
        event = (parent, uid, row["action_idx"])
        if event in seen_events:
            rejection["duplicate_event_identity"] += 1
            continue
        seen_events.add(event)
        eligible_parents.add(parent)
        names_for_row = group_names(row)
        if not names_for_row:
            rejection["verb_noun_not_in_predeclared_candidate_groups"] += 1
        for group in names_for_row:
            candidate = make_candidate(row, index, cm, group, group_to_stratum[group])
            if candidate is None:
                rejection["target_outside_clip_" + group] += 1
            else:
                groups[group].append(candidate)
    rng = random.Random(SEED)
    selected = []
    used_parents = set()
    used_related_parents = set()
    pool_audit = {}
    for group, stratum, train_quota, dev_quota in SPLIT_GROUP_QUOTAS:
        quota = train_quota + dev_quota
        pool = sorted(groups[group], key=stable_key)
        pool_hash = digest_bytes(canonical(pool))
        rng.shuffle(pool)
        chosen = []
        for candidate in pool:
            if candidate["video_uid"] in used_related_parents:
                continue
            candidate["split"] = "train" if len(chosen) < train_quota else "dev"
            chosen.append(candidate)
            used_parents.add(candidate["video_uid"])
            used_related_parents.update(group_closure({candidate["video_uid"]}))
            if len(chosen) == quota:
                break
        selected.extend(chosen)
        pool_audit[group] = {"stratum": stratum, "quota": quota, "candidate_rows": len(pool),
                             "candidate_parents": len({x["video_uid"] for x in pool}),
                             "canonical_stable_sorted_pool_sha256": pool_hash, "selected": len(chosen)}
    rng.shuffle(selected)
    selected.sort(key=lambda item: 0 if item["split"] == "train" else 1)
    for index, item in enumerate(selected, 1):
        item["item_id"] = f"T{index:02d}"
        item["sample_id"] = f"{item['clip_uid']}:{item['decision_tick_sixths']}"
        item["related_group_parent_ids"] = sorted(group_closure({item["video_uid"]}))
        item["official_group_ids"] = sorted(uid for uid, members in group_mapping.items()
                                             if set(members).intersection(item["related_group_parent_ids"]))
    for split in SPLIT_QUOTAS:
        for stratum in DESIRED:
            choices = sorted((x for x in selected if x["split"] == split and x["stratum"] == stratum), key=stable_key)
            rng.shuffle(choices)
            if choices:
                choices[0]["repeat_selected"] = True
    actual = dict(Counter(item["stratum"] for item in selected))
    isolation = {"selected_parent_count": len(used_parents), "duplicate_parent_count": len(selected) - len(used_parents),
                 "overlap_with_excluded_parent_ids": sorted(used_parents & blocked),
                 "overlap_with_reserved_clip_ids": sorted({x["clip_uid"] for x in selected} & reserved),
                 "selected_related_group_component_count": len({tuple(x["related_group_parent_ids"]) for x in selected}),
                 "duplicate_related_group_component_count": len(selected) - len({tuple(x["related_group_parent_ids"]) for x in selected})}
    isolation["passed"] = (not isolation["duplicate_parent_count"] and not isolation["overlap_with_excluded_parent_ids"]
                           and not isolation["overlap_with_reserved_clip_ids"] and not isolation["duplicate_related_group_component_count"])
    require(isolation["passed"], "Selected sources fail isolation")
    rules = {
        "seed": SEED, "rng": "Python random.Random (Mersenne Twister), one seeded instance",
        "stable_sort_fields": ["video_uid", "clip_uid", "action_index", "raw_annotation.action_clip_start_sec", "raw_annotation.action_clip_end_sec", "raw_annotation.verb", "raw_annotation.noun"],
        "subgroup_order_and_quotas": [{"subgroup": group, "stratum": stratum, "train": tr, "dev": dv} for group, stratum, tr, dv in SPLIT_GROUP_QUOTAS],
        "split_quotas": SPLIT_QUOTAS,
        "algorithm": "For each subgroup in declared order: stable-sort its complete eligible pool, rng.shuffle it, scan to quota rejecting already-selected parents and their official grouped-video related canonical members. Assign the first train-quota accepted candidates to train and the remaining dev-quota to dev. Then rng.shuffle all selected items, stable-sort train before dev, assign T01..T64. For each split train/dev, then each stratum in declared order: stable-sort its selected items, rng.shuffle, mark first one repeat. No viewing videos or model results before freeze.",
        "normal_window": "mid=(action_clip_start_sec+action_clip_end_sec)/2; t=ceil((mid+0.75)*6)/6; target=[t-1.5,t]",
        "left_boundary_window": "t=floor(mid*6)/6+1.5; proxy is at/within 1/6 second after target start",
        "right_boundary_window": "t=ceil(mid*6)/6; proxy is at/within 1/6 second before target end",
        "proxy_caveat": "The LTA interval is a context window, not precise event onset/completion. Its midpoint is only an a-priori sampling proxy, not a PNR or observed support-transfer time. Boundary examples are proxy-time boundaries; no visibility boundary is asserted.",
        "subgroup_predicates": {"pickup": [TAKE], "placement": [PUT], "wipe_or_clean": ["wipe", "clean_(sweep,_scrub,_mop,_dust)"], "mix": ["mix"], "sustained_handling_or_operation": ["hold_(support,_grip,_grasp)", "operate_(use,_dial,_click-button)"], "door_or_drawer": {"verbs": ["open", "close"], "nouns": ["door", "drawer"]}, "boundary_verbs": [TAKE, PUT]},
        "one_window_per_parent_and_event": True, "no_reselection_after_teacher": True,
        "one_window_per_official_grouped_source_component": True,
        "download_availability_was_not_a_selection_filter": True,
    }
    exclusion_audit = {"schema": "robomemo-tenframe-exclusion-audit-v1", "source_files": sources,
                       "official_holdout_files": file_audits, "exclusions": exclusion_sets,
                       "counts": {key: len(value) for key, value in exclusion_sets.items()},
                       "isolation_checks": isolation, "group_resolution": group_resolution,
                       "parent_resolution": "Extract official video_uid/video_id and STA info.video_metadata parent references; expand any grouped ID through official ego4d.json explicit group_video_uids. Cross-check all reserved clips in official manifest+metadata+vq_val. Metadata split_* val/test adds conservative exclusion, then apply group-member closure to all blocked canonical parents.",
                       "additional_conservative_exclusions": ["All local *_test_unannotated.json parents", "Official metadata parents with any split_* value val/test", "All canonical members connected through official grouped-video metadata to any old, reserved or official holdout parent"]}
    frozen = {
        "schema": "robomemo-tenframe-selection-frozen-v1",
        "status": "selection_frozen_before_download_and_teacher" if len(selected) == 64 and actual == DESIRED and all(dict(Counter(x["stratum"] for x in selected if x["split"] == split)) == quotas for split, quotas in SPLIT_QUOTAS.items()) else "insufficient_eligible_sources_no_isolation_relaxation",
        "created_utc": datetime.now(timezone.utc).isoformat(), "seed": SEED,
        "purpose": "Frozen 48 train / 16 dev new-source comparison; strata are sampling metadata, never labels; not final test or continuous-video recall.",
        "protocol_sha256": sources[args.protocol.name]["sha256"],
        "split_counts": dict(Counter(x["split"] for x in selected)),
        "desired_split_stratum_counts": SPLIT_QUOTAS,
        "synthetic_test": False, "stratum_is_not_true_label": True,
        "teacher_calls": 0, "training_runs": 0, "uploads": 0,
        "desired_stratum_counts": DESIRED, "actual_stratum_counts": actual,
        "selected_count": len(selected), "selected_parent_count": len(used_parents),
        "repeat_item_ids": [item["item_id"] for item in selected if item["repeat_selected"]],
        "maximum_future_teacher_calls": 70, "repeats_are_training_rows": False,
        "source_files": sources, "exclusions": exclusion_sets, "isolation_checks": isolation,
        "group_resolution": group_resolution,
        "selection_rules": rules,
        "time_schema": {"official_sources": SOURCE_URLS, "authoritative_seconds": "action_clip_start_sec/end_sec are relative to <clip_uid>.mp4, used directly by official loader. clip_parent_start_sec/end_sec define canonical parent position, verified against metadata video_start_sec/end_sec and clips manifest parent_start_sec/end_sec.",
                        "parent_conversion": "parent_time_sec = clip_parent_start_sec + clip_relative_time_sec",
                        "frame_crosscheck": "Canonical clips documented as 30fps; annotation frame/30 vs seconds is crosschecked within one source-frame period, not substituted for authoritative seconds.",
                        "max_observed_frame_seconds_difference_sec": max_frame_second_difference,
                        "lta_annotation_window_duration_counts": dict(sorted((str(key), value) for key, value in durations.items())),
                        "duration_is_not_action_completion": True},
        "pool_audit": {"official_train_action_rows": len(train["clips"]), "eligible_parents_after_schema_and_isolation": len(eligible_parents),
                       "rejected_or_non_candidate_rows": dict(rejection), "overlapping_exclusion_reason_row_counts": dict(exclusion_reasons), "subgroups": pool_audit},
        "input_contract": {"target_seconds": 1.5, "nominal_fps": 6, "frame_count": 10, "pixels": 224, "baseline_four_indices_zero_based": [0, 3, 6, 9],
                           "preprocessing": "Frozen RGB bilinear letterbox with padding RGB(124,116,104); PNG", "sampling": "Nearest actual source frame at or before each one-sixth-second grid timestamp; no interpolation", "audit_video_contains_no_frame_after_target_end": True, "audit_audio_enabled": False},
        "items": selected,
    }
    frozen["selection_content_sha256"] = digest_bytes(canonical({"items": selected, "rules": rules, "pool_audit": frozen["pool_audit"], "exclusions": exclusion_sets}))
    args.output.mkdir(parents=True, exist_ok=True)
    for name, data in (("selection_frozen.json", frozen), ("exclusion_audit.json", exclusion_audit)):
        write_once(args.output / name, (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8"))
    write_once(args.output / "download_uids.txt", ("\n".join(sorted(item["clip_uid"] for item in selected)) + "\n").encode("ascii"))
    print(json.dumps({"selection_path": str(args.output / "selection_frozen.json"), "status": frozen["status"], "selected": len(selected), "parents": len(used_parents), "strata": actual, "repeats": frozen["repeat_item_ids"], "exclusion_counts": exclusion_audit["counts"], "pool_audit": pool_audit, "content_sha256": frozen["selection_content_sha256"]}, ensure_ascii=False), flush=True)
    return 0 if frozen["status"] == "selection_frozen_before_download_and_teacher" else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--exclusions", type=Path, required=True)
    parser.add_argument("--reservation", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--reserved-clips", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        return run(args)
    except (ValueError, OSError, KeyError, TypeError, IndexError) as exc:
        parser.exit(1, f"SELECTION_STOPPED: {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
