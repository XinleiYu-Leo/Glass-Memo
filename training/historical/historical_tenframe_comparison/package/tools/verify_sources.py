"""Independent real-data acceptance checks for the next RoboMemo source batch.

Reads source data and completed exports. Only --report is written.
No exporter imports, model calls, training, uploads, or source modifications.
"""
from __future__ import annotations

import argparse
import bisect
from collections import Counter
import csv
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import re
import random
import sys
import time
import zipfile

import av
from PIL import Image, ImageChops, ImageStat, __version__ as pillow_version

TOL = 1e-6
COUNTS = {"candidate_positive": 32, "candidate_hard_negative": 22, "candidate_boundary": 10}
SPLIT_COUNTS = {
    "train": {"candidate_positive": 24, "candidate_hard_negative": 16, "candidate_boundary": 8},
    "dev": {"candidate_positive": 8, "candidate_hard_negative": 6, "candidate_boundary": 2},
}
QUOTAS = [("pickup", 12, 4), ("placement", 12, 4), ("wipe_or_clean", 4, 2), ("mix", 4, 2),
          ("sustained_handling_or_operation", 4, 1), ("door_or_drawer", 4, 1),
          ("proxy_near_left_boundary", 4, 1), ("proxy_near_right_boundary", 4, 1)]


def require(ok, message):
    if not ok:
        raise ValueError(message)


def close(a, b, message, tolerance=TOL):
    require(math.isfinite(float(a)) and math.isfinite(float(b))
            and abs(float(a) - float(b)) <= tolerance, message)


def digest_bytes(value):
    return hashlib.sha256(value).hexdigest()


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def parse_json(value):
    def pairs(entries):
        result = {}
        for key, data in entries:
            require(key not in result, "Duplicate JSON key: " + key)
            result[key] = data
        return result

    def invalid(value):
        raise ValueError("Nonfinite JSON number: " + value)

    return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid)


def load_json(path):
    return parse_json(Path(path).read_bytes())


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def video_uids(value):
    """Conservatively collect parent IDs from every original official split record."""
    result = set()
    if isinstance(value, dict):
        for key in ("video_uid", "video_id"):
            uid = value.get(key)
            if isinstance(uid, str) and uid:
                result.add(uid)
        for key, child in value.items():
            if key == "video_metadata" and isinstance(child, dict):
                result.update(uid for uid in child if re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", uid) or uid.startswith("grp-"))
            result.update(video_uids(child))
    elif isinstance(value, list):
        for child in value:
            result.update(video_uids(child))
    return result


def source_families(metadata_videos):
    """Use explicit official group membership, with graph components for closure."""
    resolved = {}

    def resolve(uid, visited=frozenset()):
        require(uid in metadata_videos and uid not in visited, "Missing or cyclic official grouped parent: " + uid)
        if uid in resolved:
            return resolved[uid]
        row = metadata_videos[uid]
        if row.get("is_grouped") is True:
            members = row.get("group_video_uids")
            require(isinstance(members, list) and len(members) == len(set(members)) and len(members) > 1,
                    "Official grouped parent lacks explicit member list")
            leaves = set().union(*(resolve(member, visited | {uid}) for member in members))
        else:
            require(re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", uid)
                    and not row.get("group_video_uids"), "Unknown canonical parent schema")
            leaves = {uid}
        resolved[uid] = leaves
        return leaves

    groups = {uid: sorted(resolve(uid)) for uid, row in metadata_videos.items() if row.get("is_grouped") is True}
    neighbors = {}
    for members in groups.values():
        first = members[0]
        for uid in members:
            neighbors.setdefault(first, set()).add(uid)
            neighbors.setdefault(uid, set()).add(first)
    components = {}
    for start in neighbors:
        if start in components:
            continue
        pending, found = [start], set()
        while pending:
            uid = pending.pop()
            if uid in found:
                continue
            found.add(uid)
            pending.extend(neighbors.get(uid, set()) - found)
        for uid in found:
            components[uid] = frozenset(found)

    def family(uid):
        return set(components.get(uid, {uid}))

    return resolve, family, groups


def verify_isolation(args, selection):
    excluded = load_json(args.exclusions)
    old_train = set(excluded["declared_train_parents"])
    old_dev = set(excluded["declared_dev_parents"])
    require(len(old_train) == 18 and len(old_dev) == 6 and not old_train & old_dev,
            "The original 18 train / 6 development parent exclusions differ")
    reservation = load_json(args.reservation)
    diagnostic = set(reservation["parent_ids"])
    diagnostic_related = set(reservation["known_related_parent_ids"])
    require(len(diagnostic) == 24 and diagnostic <= diagnostic_related
            and reservation.get("exclude_from_next_training_round") is True, "Prior 24 diagnostic reservation differs")
    metadata = load_json(args.metadata)
    metadata_videos = {row["video_uid"]: row for row in metadata["videos"]}
    metadata_clips = {row["clip_uid"]: row for row in metadata["clips"]}
    resolve, family, group_map = source_families(metadata_videos)
    require(selection["group_resolution"]["group_to_canonical_parent_ids"] == group_map,
            "Frozen official grouped-video mapping differs")
    split_parents, split_files = {}, []
    for path in sorted(args.annotations.glob("*.json")):
        if not (path.name.endswith("_val.json") or path.name.endswith("_test_unannotated.json")):
            continue
        raw_parents = video_uids(load_json(path))
        parents = set().union(*(resolve(uid) for uid in raw_parents))
        require(parents, "No parent IDs resolved from official split: " + path.name)
        split_parents[path.name] = parents
        split_files.append({"file": path.name, "sha256": digest(path), "parent_count": len(parents)})
    require(all(name in split_parents for name in ("fho_lta_val.json", "vq_val.json", "fho_lta_test_unannotated.json", "vq_test_unannotated.json")),
            "Essential official validation annotations missing")
    official_val = set().union(*(v for k, v in split_parents.items() if k.endswith("_val.json")))
    official_test = set().union(*(v for k, v in split_parents.items() if k.endswith("_test_unannotated.json")))
    with args.manifest.open(newline="", encoding="utf-8-sig") as stream:
        clip_map = {row["exported_clip_uid"]: row for row in csv.DictReader(stream)}
    reserved_bytes = (("\n".join(sorted(p.stem for p in args.reserved_clips.glob("*.mp4"))) + "\n").encode("ascii")
                      if args.reserved_clips.is_dir() else args.reserved_clips.read_bytes())
    reserved = reserved_bytes.decode("utf-8-sig").split()
    require(len(reserved) == len(set(reserved)) == 60, "The 60 VQ holdout IDs differ")
    require(all(uid in clip_map for uid in reserved), "Unmapped reserved VQ clip")
    reserved_parents = {clip_map[uid]["parent_video_uid"] for uid in reserved}
    vq_val = load_json(args.annotations / "vq_val.json")
    require(set(reserved) <= {c["clip_uid"] for v in vq_val["videos"] for c in v["clips"]}, "Reserved60 not official VQ validation")
    require(all(uid in metadata_clips and metadata_clips[uid]["video_uid"] == clip_map[uid]["parent_video_uid"] for uid in reserved), "Reserved metadata/manifest mapping differs")
    require(reserved_parents <= official_val, "Reserved parents not all excluded by official validation")
    raw_metadata_holdout = {uid for uid, row in metadata_videos.items()
                            if any(value in ("val", "test") for key, value in row.items() if key.startswith("split_"))}
    metadata_holdout = set().union(*(resolve(uid) for uid in raw_metadata_holdout))
    before_closure = old_train | old_dev | official_val | official_test | reserved_parents | metadata_holdout | diagnostic | diagnostic_related
    forbidden = set().union(*(family(uid) for uid in before_closure))
    require(selection["group_resolution"]["metadata_sha256"] == digest(args.metadata)
            and set(selection["group_resolution"]["blocked_parent_ids_before_group_closure"]) == before_closure
            and set(selection["group_resolution"]["additional_blocked_parent_ids_from_group_closure"]) == forbidden - before_closure,
            "Frozen grouped-source exclusion closure differs")
    require(set(selection["exclusions"]["excluded_parent_ids"]) == forbidden, "Frozen excluded parent union differs from original sources")
    require(set(selection["exclusions"]["official_val_parent_ids"]) == official_val
            and set(selection["exclusions"]["official_test_parent_ids"]) == official_test, "Frozen official holdout sets differ")
    validate_selection(selection)
    items = selection["items"]
    require(len(items) == 64 and len({x["video_uid"] for x in items}) == 64,
            "Expected exactly 64 different parent videos")
    require(len({x["clip_uid"] for x in items}) == 64, "Duplicate selected clip")
    require({x["item_id"] for x in items} == {f"T{x:02d}" for x in range(1, 65)}, "Anonymous sample IDs differ")
    require(Counter(x["stratum"] for x in items) == COUNTS, "Candidate stratum totals differ")
    require(Counter(x["stratum"] for x in items if x["repeat_selected"]) == {k: 2 for k in COUNTS},
            "Expected two predeclared repeats in each stratum")
    require(not {x["video_uid"] for x in items} & forbidden, "Parent-video isolation failed")
    require(len({tuple(sorted(family(item["video_uid"]))) for item in items}) == 64, "Selected canonical videos share a grouped source family")
    for item in items:
        related = family(item["video_uid"])
        require(item["related_group_parent_ids"] == sorted(related), "Selected source-family membership differs")
        require(item["official_group_ids"] == sorted(uid for uid, members in group_map.items() if set(members) & related), "Selected official group list differs")
    require(not {x["clip_uid"] for x in items} & set(reserved), "Reserved VQ clip selected")
    train = load_json(args.annotations / "fho_lta_train.json")
    taxonomy = load_json(args.annotations / "fho_lta_taxonomy.json")
    require(train["split"] == "train", "Wrong original action split")
    named_inputs = {"ego4d.json": args.metadata, "source_exclusions.json": args.exclusions,
                    "clips_manifest.csv": args.manifest, "reserved_vq_clip_ids.txt": args.reserved_clips,
                    args.reservation.name: args.reservation, args.protocol.name: args.protocol}
    for name, recorded in selection["source_files"].items():
        if name == "reserved_vq_clip_ids.txt":
            require(len(reserved_bytes) == recorded["bytes"] and digest_bytes(reserved_bytes) == recorded["sha256"], "Reserved clip set changed")
            continue
        source_path = named_inputs.get(name, args.annotations / name)
        require(Path(name).name == name and source_path.is_file(), "Unresolved frozen source artifact: " + name)
        require(source_path.stat().st_size == recorded["bytes"] and digest(source_path) == recorded["sha256"],
                "Frozen annotation/provenance source changed: " + name)
    train_rows = {digest_bytes(canonical(row)) for row in train["clips"]}
    for item in items:
        row = item["raw_annotation"]
        require(train["clips"][item["annotation_row_index"]] == row, "Annotation row provenance differs")
        require(digest_bytes(canonical(row)) in train_rows, item["item_id"] + " raw action is not in official LTA train")
        require(row["video_uid"] == item["video_uid"] and row["clip_uid"] == item["clip_uid"], "Annotation identity differs")
        require(taxonomy["verbs"][row["verb_label"]] == row["verb"]
                and taxonomy["nouns"][row["noun_label"]] == row["noun"], "Taxonomy labels differ")
        mapping = clip_map[item["clip_uid"]]
        require(mapping["parent_video_uid"] == item["video_uid"], "Official parent mapping differs")
        close(mapping["parent_start_sec"], row["clip_parent_start_sec"], "Official clip origin differs", 1e-4)
        close(mapping["parent_end_sec"], row["clip_parent_end_sec"], "Official clip end differs", 1e-4)
        close(item["clip_parent_start_sec"], row["clip_parent_start_sec"], "Selected clip origin differs")
        close(item["clip_parent_end_sec"], row["clip_parent_end_sec"], "Selected clip end differs")
        close(item["target_end_sec"], item["decision_time_sec"], "Decision endpoint differs")
        close(item["target_end_sec"] - item["target_start_sec"], 1.5, "Target is not 1.5 seconds")
        close(item["decision_time_sec"] * 6, round(item["decision_time_sec"] * 6), "Decision off sixth-second grid")
        close(item["audit_start_sec"], max(0, item["decision_time_sec"] - 6), "Audit start differs")
        require(0 <= item["target_start_sec"] < item["target_end_sec"]
                <= row["clip_parent_end_sec"] - row["clip_parent_start_sec"], "Target outside canonical clip")
    replay = replay_selection(train["clips"], taxonomy, clip_map, metadata_clips,
                              metadata_videos, forbidden, set(reserved), selection, family)
    return {"old_train_parents": len(old_train), "old_dev_parents": len(old_dev),
            "official_validation_parents": len(official_val), "official_test_parents": len(official_test),
            "reserved_vq_clips": len(reserved), "reserved_vq_parents": len(reserved_parents),
            "metadata_holdout_parents": len(metadata_holdout),
            "official_group_count": len(group_map), "blocked_before_group_closure": len(before_closure),
            "additional_blocked_from_group_closure": len(forbidden - before_closure),
            "selected_independent_group_components": 64,
            "excluded_parent_union": len(forbidden), "selected_parents": 64,
            "selected_overlap": 0, "official_split_sources": split_files,
            "independent_seed_replay": replay}


def replay_selection(rows, taxonomy, clip_map, metadata_clips, metadata_videos, forbidden, reserved, frozen, family):
    """Rebuild eligible identity pools and independently repeat the frozen RNG sequence."""
    groups = QUOTAS
    pools, seen = {name: [] for name, _, _ in groups}, set()
    for row_index, row in enumerate(rows):
        parent, uid = row["video_uid"], row["clip_uid"]
        if parent in forbidden or uid in reserved or uid not in clip_map or uid not in metadata_clips or parent not in metadata_videos:
            continue
        mapping, meta = clip_map[uid], metadata_clips[uid]
        if mapping["parent_video_uid"] != parent or meta["video_uid"] != parent:
            continue
        start, end = row["clip_parent_start_sec"], row["clip_parent_end_sec"]
        if any(abs(float(a) - b) > TOL for a, b in ((mapping["parent_start_sec"], start), (mapping["parent_end_sec"], end),
                                                   (meta["video_start_sec"], start), (meta["video_end_sec"], end))):
            continue
        if "FHO" not in mapping["benchmarks"] or not mapping["s3_path"].startswith("s3://") or not mapping["s3_path"].endswith("/" + uid + ".mp4"):
            continue
        a, b = row["action_clip_start_sec"], row["action_clip_end_sec"]
        if not all(isinstance(row[key], (int, float)) and math.isfinite(row[key]) for key in (
                "action_clip_start_sec", "action_clip_end_sec", "clip_parent_start_sec", "clip_parent_end_sec", "interval_start_sec", "interval_end_sec")):
            continue
        if not (0 <= a < b <= end - start + TOL) or type(row["action_idx"]) is not int or row["action_idx"] < 0:
            continue
        if taxonomy["verbs"][row["verb_label"]] != row["verb"] or taxonomy["nouns"][row["noun_label"]] != row["noun"]:
            continue
        if max(abs(row["action_clip_start_frame"] / 30 - a), abs(row["action_clip_end_frame"] / 30 - b)) > 1 / 30 + TOL:
            continue
        identity = (parent, uid, row["action_idx"])
        if identity in seen:
            continue
        seen.add(identity)
        key = (*identity, a, b, row["verb"], row["noun"])
        names = []
        if row["verb"] in ("take_(pick,_grab,_get)", "put_(place,_leave,_drop)"):
            names += ["pickup" if row["verb"] == "take_(pick,_grab,_get)" else "placement",
                      "proxy_near_left_boundary", "proxy_near_right_boundary"]
        if row["verb"] in ("wipe", "clean_(sweep,_scrub,_mop,_dust)"):
            names.append("wipe_or_clean")
        if row["verb"] == "mix":
            names.append("mix")
        if row["verb"] in ("hold_(support,_grip,_grasp)", "operate_(use,_dial,_click-button)"):
            names.append("sustained_handling_or_operation")
        if row["verb"] in ("open", "close") and row["noun"] in ("door", "drawer"):
            names.append("door_or_drawer")
        for name in names:
            midpoint = (a + b) / 2
            if name == "proxy_near_left_boundary":
                tick = math.floor(midpoint * 6) + 9
            elif name == "proxy_near_right_boundary":
                tick = math.ceil(midpoint * 6)
            else:
                tick = math.ceil((midpoint + 0.75) * 6)
            if (tick - 9) / 6 < 0 or tick / 6 >= float(mapping["parent_end_sec"]) - float(mapping["parent_start_sec"]) - TOL:
                continue
            pools[name].append({"key": key, "group": name, "row_index": row_index, "tick": tick})
    require(frozen["seed"] == frozen["selection_rules"]["seed"] == 20261026, "Frozen seed differs")
    rng, chosen, parents = random.Random(20261026), [], set()
    for name, train_quota, dev_quota in groups:
        quota = train_quota + dev_quota
        pool = sorted(pools[name], key=lambda x: x["key"])
        require(len(pool) == frozen["pool_audit"]["subgroups"][name]["candidate_rows"], "Independent candidate pool count differs: " + name)
        rng.shuffle(pool)
        count = 0
        for row in pool:
            if row["key"][0] in parents:
                continue
            row["split"] = "train" if count < train_quota else "dev"
            chosen.append(row)
            parents.update(family(row["key"][0]))
            count += 1
            if count == quota:
                break
        require(count == quota, "Independent source pool insufficient: " + name)
    rng.shuffle(chosen)
    chosen.sort(key=lambda item: 0 if item["split"] == "train" else 1)
    for expected, actual in zip(chosen, frozen["items"]):
        row = actual["raw_annotation"]
        require(expected["split"] == actual["split"] and expected["row_index"] == actual["annotation_row_index"]
                and expected["group"] == actual["selection_subgroup"]
                and expected["tick"] == actual["decision_tick_sixths"], "Seed replay produced a different selected source/window")
        close(actual["anchor"]["clip_time_sec"], (row["action_clip_start_sec"] + row["action_clip_end_sec"]) / 2, "Proxy anchor differs")
        require(actual["anchor"]["is_observed_completion"] is False and actual["stratum_is_not_true_label"] is True,
                "Candidate annotation promoted to ground truth")
    repeated = set()
    for split in SPLIT_COUNTS:
        for stratum in COUNTS:
            choices = sorted([item for item in frozen["items"] if item["split"] == split and item["stratum"] == stratum],
                             key=lambda x: (x["video_uid"], x["clip_uid"], x["action_index"], x["raw_annotation"]["action_clip_start_sec"],
                                            x["raw_annotation"]["action_clip_end_sec"], x["raw_annotation"]["verb"], x["raw_annotation"]["noun"]))
            rng.shuffle(choices)
            repeated.update(row["item_id"] for row in choices[:1])
    require(repeated == set(frozen["repeat_item_ids"])
            == {x["item_id"] for x in frozen["items"] if x["repeat_selected"]}, "Seed replay produced a different repeat set")
    content = {"items": frozen["items"], "rules": frozen["selection_rules"],
               "pool_audit": frozen["pool_audit"], "exclusions": frozen["exclusions"]}
    require(digest_bytes(canonical(content)) == frozen["selection_content_sha256"], "Frozen selection content SHA differs")
    return {"seed": 20261026, "all_64_items_and_windows_reproduced": True, "all_six_repeat_choices_reproduced": True}


def independent_png(frame):
    source = frame.to_image().convert("RGB")
    scale = min(224 / source.width, 224 / source.height)
    size = (max(1, round(source.width * scale)), max(1, round(source.height * scale)))
    canvas = Image.new("RGB", (224, 224), (124, 116, 104))
    canvas.paste(source.resize(size, Image.Resampling.BILINEAR), ((224 - size[0]) // 2, (224 - size[1]) // 2))
    encoded = io.BytesIO()
    canvas.save(encoded, format="PNG")
    return encoded.getvalue()



def validate_selection(selection):
    require(selection.get("schema") == "robomemo-tenframe-selection-frozen-v1", "Wrong frozen selection schema")
    require(selection.get("status") == "selection_frozen_before_download_and_teacher", "Source pool was insufficient; STOP without reselection")
    require(selection.get("synthetic_test") is False and selection.get("stratum_is_not_true_label") is True, "Not an unlabeled real-source freeze")
    items = selection["items"]
    require(len(items) == 64 and [i["item_id"] for i in items] == [f"T{n:02d}" for n in range(1, 65)], "Expected ordered T01..T64")
    for field in ("video_uid", "clip_uid", "item_id"):
        require(len({i[field] for i in items}) == 64, "Repeated selected " + field)
    require([i["split"] for i in items] == ["train"] * 48 + ["dev"] * 16, "Split/order changed after freeze")
    for split, counts in SPLIT_COUNTS.items():
        require(dict(Counter(i["stratum"] for i in items if i["split"] == split)) == counts, "Frozen split/stratum quota differs")
    for subgroup, train_quota, dev_quota in QUOTAS:
        for split, quota in (("train", train_quota), ("dev", dev_quota)):
            require(sum(i["selection_subgroup"] == subgroup and i["split"] == split for i in items) == quota, "Frozen subgroup quota differs")
    repeated = [i for i in items if i["repeat_selected"]]
    require(Counter((i["split"], i["stratum"]) for i in repeated) == Counter({(s, t): 1 for s in SPLIT_COUNTS for t in COUNTS}), "Expected six repeats, one per split/stratum")
    require(selection["repeat_item_ids"] == [i["item_id"] for i in repeated]
            and selection["maximum_future_teacher_calls"] == 70
            and selection["repeats_are_training_rows"] is False, "Repeat/request contract differs")
    require(selection["seed"] == 20261026 and re.fullmatch(r"[0-9a-f]{64}", selection["protocol_sha256"]), "Unfrozen seed or protocol")
    excluded = selection["exclusions"]
    forbidden = set(excluded["excluded_parent_ids"])
    require(len(excluded["declared_train_parent_ids"]) == 18 and len(excluded["declared_dev_parent_ids"]) == 6,
            "Original parent exclusion counts differ")
    require(len(set(excluded["diagnostic_parent_ids"])) == 24 and len(set(excluded["reserved_clip_ids"])) == 60,
            "Prior24 or reserved60 exclusions missing")
    before = set()
    for field in ("declared_train_parent_ids", "declared_dev_parent_ids", "official_val_parent_ids", "official_test_parent_ids", "metadata_holdout_parent_ids", "reserved_parent_ids", "diagnostic_parent_ids", "diagnostic_known_related_parent_ids"):
        before.update(excluded[field])
    groups = selection["group_resolution"]["group_to_canonical_parent_ids"]
    def closure(values):
        found = set(values)
        while True:
            old = set(found)
            for group in groups.values():
                if found.intersection(group):
                    found.update(group)
            if old == found:
                return found
    require(closure(before) == forbidden, "Exclusions lack full group closure")
    used = set()
    for item in items:
        related = closure({item["video_uid"]})
        require(item["related_group_parent_ids"] == sorted(related), "Incomplete item group closure")
        require(not related & (used | forbidden), "Source family overlaps selected or excluded parent")
        require(item["clip_uid"] not in excluded["reserved_clip_ids"], "Reserved clip selected")
        require(item["stratum_is_not_true_label"] is True, "Candidate stratum treated as label")
        close(item["target_end_sec"] - item["target_start_sec"], 1.5, "Target duration differs")
        close(item["decision_tick_sixths"] / 6, item["target_end_sec"], "Grid endpoint differs")
        used.update(related)
    content = {"items": items, "rules": selection["selection_rules"], "pool_audit": selection["pool_audit"], "exclusions": excluded}
    require(digest_bytes(canonical(content)) == selection["selection_content_sha256"], "Selection content fingerprint differs")
    return {"selected_parents": 64, "train": 48, "dev": 16, "repeats": 6, "group_overlap": 0}


def verify_one(item, frozen, dataset_dir, video=None):
    """Independent binary/PTS check; original MP4 additionally proves direct pixel lineage."""
    require(item["item_id"] == frozen["item_id"], "Dataset item order differs")
    for key in ("video_uid", "clip_uid", "split", "target_start_sec", "target_end_sec", "related_group_parent_ids"):
        require(item[key] == frozen[key], "Dataset differs from frozen " + key)
    frames = item["frames"]
    require(len(frames) == 10, "Expected ten PNGs per unique dataset row")
    tick = frozen["decision_tick_sixths"]
    requested = [Fraction(t, 6) for t in range(tick - 9, tick + 1)]
    audit = item["source_audit"]
    require(audit["original_source_direct_decode"] is True and audit["interpolation"] is False, "No direct-source provenance")
    evidence = audit["decoded_pts_until_first_after_target"]
    origin = Fraction(audit["source_origin_fraction"])
    close(origin, audit["source_origin_sec"], "Source clock origin differs")
    times = []
    for n, point in enumerate(evidence):
        require(point["source_frame_index"] == n and type(point["source_pts"]) is int, "Invalid PTS evidence index")
        actual = Fraction(point["source_pts"]) * Fraction(point["source_time_base"]) - origin
        close(actual, point["actual_time_sec"], "PTS evidence time differs")
        require(not times or actual > times[-1], "Source PTS not strictly increasing")
        times.append(actual)
    require(times, "No source PTS evidence")
    unique = set()
    for n, (frame, nominal) in enumerate(zip(frames, requested), 1):
        require(frame["frame_id"] == f"f{n:02d}" and frame["path"] == f"student_frames/{item['item_id']}_f{n:02d}.png", "Unexpected frame identity/path")
        path = dataset_dir / frame["path"]
        require(path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(dataset_dir.resolve()), "Unsafe/missing frame path")
        require(digest(path) == frame["sha256"], "PNG hash mismatch")
        with Image.open(path) as image:
            require(image.format == "PNG" and image.mode == "RGB" and image.size == (224, 224), "PNG size/color differs")
            image.load()
        close(nominal, frame["requested_time_sec"], "Requested 6fps time differs")
        index = bisect.bisect_right(times, nominal) - 1
        require(index >= 0 and index == frame["source_frame_index"], "Frame is not actual nearest causal predecessor")
        actual = times[index]
        require(actual <= nominal and actual <= requested[-1] and float(actual) >= float(requested[0]) - TOL,
                "Frame outside target or in future; no clamping allowed")
        close(actual, frame["actual_time_sec"], "Actual source time differs")
        require(frame["source_pts"] == evidence[index]["source_pts"] and frame["source_time_base"] == evidence[index]["source_time_base"], "Chosen PTS differs")
        require(frame["repeated_source_frame"] == (index in unique), "Repeated frame flag differs")
        close(nominal - actual, frame["sampling_lag_sec"], "Sampling lag differs")
        successor = float(times[index + 1]) if index + 1 < len(times) else None
        require(frame["next_source_actual_time_sec"] == successor, "Successor evidence differs")
        unique.add(index)
    require(len(unique) == 10, "Ten-frame input contains repeated source observations")
    require(audit["source_ended_before_target"] is False and times[-1] >= requested[-1], "Source ended before target")
    if video is not None:
        require(video.is_file() and digest(video) == audit["source_sha256"] and video.stat().st_size == audit["source_bytes"], "Original source changed")
        decoded = []
        pixel_hashes = {}
        with av.open(str(video)) as container:
            require(len(container.streams.video) == 1, "Unexpected source video streams")
            stream = container.streams.video[0]
            direct_origin = Fraction(stream.start_time) * stream.time_base if stream.start_time is not None else None
            for index, frame in enumerate(container.decode(video=0)):
                require(frame.pts is not None and frame.time_base is not None, "Original source missing PTS")
                absolute = Fraction(frame.pts) * frame.time_base
                if direct_origin is None:
                    direct_origin = absolute
                actual = absolute - direct_origin
                require(not decoded or actual > decoded[-1], "Original source nonmonotonic PTS")
                require(index < len(evidence), "Original decoder produced extra evidence")
                require(evidence[index] == {"source_frame_index": index, "source_pts": frame.pts, "source_time_base": str(frame.time_base), "actual_time_sec": float(actual)}, "Recorded source PTS differs from original decode")
                require([frame.width, frame.height] == audit["source_dimensions"], "Source dimensions differ")
                decoded.append(actual)
                if index in unique:
                    pixel_hashes[index] = digest_bytes(independent_png(frame))
                if actual > requested[-1]:
                    break
        require(decoded == times and direct_origin == origin, "Source timing evidence incomplete")
        for frame in frames:
            require(pixel_hashes[frame["source_frame_index"]] == frame["sha256"], "PNG not direct reviewed source preprocessing")
    return {"item_id": item["item_id"], "png_count": 10, "unique_source_frames": len(unique),
            "original_decode_verified": video is not None}


def verify_dataset(args, selection):
    dataset = load_json(args.output / "manifest.json")
    require(dataset["schema"] == "robomemo-tenframe-dataset-v1" and dataset["synthetic_test"] is False, "Wrong real dataset schema")
    require(dataset["selection_sha256"] == digest(args.selection)
            and dataset["protocol_sha256"] == digest(args.protocol) == selection["protocol_sha256"], "Dataset protocol or selection hash mismatch")
    require(dataset["baseline_four_indices_zero_based"] == [0, 3, 6, 9], "Baseline is not exact ten-frame subset")
    require(dataset["frame_count"] == 10 and dataset["sampling_fps"] == 6 and dataset["target_seconds"] == 1.5, "Frame contract differs")
    require(dataset["preprocessing"] == {"pixels": [224, 224], "mode": "RGB", "resize": "bilinear_letterbox", "padding_rgb": [124, 116, 104], "format": "PNG", "frozen_module_sha256": "a5a1b517094f324647ad76a5237300b478e3e1c7bace14e728a2c558d1f9dfc5"}, "Preprocessing contract differs")
    receipt_path = args.output.parent / "download_receipt.json"
    require(receipt_path.is_file() and dataset["download_receipt_sha256"] == digest(receipt_path), "Download receipt binding differs")
    receipt = load_json(receipt_path)
    require(receipt["status"] == "complete" and receipt["selection_sha256"] == digest(args.selection)
            and receipt["official_manifest_sha256"] == selection["source_files"]["clips_manifest.csv"]["sha256"], "Downloaded source freeze differs")
    sources = {s["clip_uid"]: s for s in receipt["sources"]}
    require(len(sources) == len(receipt["sources"]) == 64, "Download receipt source count differs")
    require(len(dataset["items"]) == 64, "Dataset must have64 unique rows, no extra repeat rows")
    expected_files = {"manifest.json", "teacher_manifest.json"}
    reports, anonymous = [], []
    for number, (item, frozen) in enumerate(zip(dataset["items"], selection["items"]), 1):
        print(f"VERIFY {number}/64 {item['item_id']} hashes, PNGs and original source PTS", flush=True)
        require(item["clip_uid"] in sources and sources[item["clip_uid"]]["sha256"] == item["source_audit"]["source_sha256"]
                and sources[item["clip_uid"]]["bytes"] == item["source_audit"]["source_bytes"], "Source audit differs from download receipt")
        require(item["source_audit"]["preprocessing_module_sha256"] == dataset["preprocessing"]["frozen_module_sha256"], "Item preprocessing hash differs")
        video = args.clips_dir / (item["clip_uid"] + ".mp4") if args.clips_dir else None
        reports.append(verify_one(item, frozen, args.output, video))
        expected_files.update(f["path"] for f in item["frames"])
        anonymous.append({"item_id": item["item_id"], "target_start_sec": item["target_start_sec"], "target_end_sec": item["target_end_sec"],
                          "frames": [{k: f[k] for k in ("frame_id", "path", "sha256", "requested_time_sec", "actual_time_sec")} for f in item["frames"]]})
    actual_files = {p.relative_to(args.output).as_posix() for p in args.output.rglob("*") if p.is_file()}
    require(actual_files == expected_files, "Dataset has unexpected/missing files")
    teacher = load_json(args.output / "teacher_manifest.json")
    require(set(teacher) == {"schema", "synthetic_test", "frame_count", "time_origin", "items"}
            and teacher["items"] == anonymous and teacher["synthetic_test"] is False, "Teacher manifest leaks metadata or differs from PNG contract")
    return {"png_count": 640, "case_count": 64, "source_decode_verified": args.clips_dir is not None, "items": reports}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("selection", "output", "protocol"):
        p.add_argument("--" + name, type=Path, required=True)
    for name in ("clips-dir", "annotations", "metadata", "manifest", "exclusions", "reservation", "reserved-clips", "report"):
        p.add_argument("--" + name, type=Path)
    args = p.parse_args()
    try:
        selection = load_json(args.selection)
        isolation = validate_selection(selection)
        source_options = (args.annotations, args.metadata, args.manifest, args.exclusions, args.reservation, args.reserved_clips)
        require(not any(source_options) or all(source_options), "Provide all official isolation inputs together")
        if all(source_options):
            isolation = verify_isolation(args, selection)
        report = {"schema": "robomemo-tenframe-export-validation-v1", "status": "passed", "synthetic_test": False,
                  "selection_sha256": digest(args.selection), "protocol_sha256": digest(args.protocol),
                  "dataset_manifest_sha256": digest(args.output / "manifest.json"),
                  "independent_official_isolation_replay": all(source_options),
                  "isolation": isolation, "dataset": verify_dataset(args, selection),
                  "teacher_calls": 0, "training_runs": 0, "uploads": 0}
        if args.report:
            data = (json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()
            if args.report.exists():
                require(args.report.read_bytes() == data, "Existing verification report differs; refusing overwrite")
            else:
                args.report.write_bytes(data)
        print("VERIFIED: 64 unique cases / 640 causal original-source PNGs / split48+16 / 6 repeat IDs", flush=True)
    except Exception as exc:
        p.exit(1, f"VERIFICATION_STOPPED: {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
