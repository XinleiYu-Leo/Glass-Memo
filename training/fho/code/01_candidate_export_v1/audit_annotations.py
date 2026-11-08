#!/usr/bin/env python3
"""Read-only, bounded-memory Ego4D annotation inventory. Never labels missing data negative.

Python 3.9+ standard library is sufficient; optional ijson speeds up large files.
Only --output is written. No network, training, video decoding, or source mutation.
"""
from __future__ import annotations

import argparse
import codecs
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import time

VERSION = "robomemo-annotation-inventory-v1"
CHUNK = 65536
TOKEN_LIMIT = 1024 * 1024
STR_RE = re.compile(r'"(?:[^"\\]|\\.)*"', re.S)
WS_RE = re.compile(r'[ \t\r\n]*')
SCALAR_RE = re.compile(r'[^ \t\r\n,\]}]+')
NUM_RE = re.compile(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z')
UID_RE = re.compile(r'(?:grp-)?[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z')
CRITICAL = ("pre_frame", "contact_frame", "pnr_frame", "post_frame")
FLAGS = ("is_valid_action", "is_partial", "is_rejected", "is_invalid_annotation")
TIMES = ("start_sec", "end_sec", "start_frame", "end_frame", "clip_start_sec", "clip_end_sec", "clip_start_frame", "clip_end_frame", "narration_timestamp_sec", "clip_narration_timestamp_sec")
ACTION_FIELDS = set(TIMES + FLAGS + ("uid", "structured_verb", "_structured_verb", "freeform_verb", "state_transition", "stage"))
INTERACTIONS = {"take", "pick", "grab", "get", "put", "place", "leave", "drop", "hold", "support", "grip", "grasp", "touch", "move", "lift", "carry", "remove", "open", "close", "wipe", "clean", "mix", "operate", "use", "push", "pull", "pour", "cut", "insert", "release"}


class TrackedReader:
    def __init__(self, path):
        self.path = Path(path)
        self.file = self.path.open("rb")
        self.hash = hashlib.sha256()
        self.bytes = 0

    def read(self, n=CHUNK):
        if n < 0:
            raise ValueError("Unbounded reads are forbidden")
        data = self.file.read(n)
        self.hash.update(data)
        self.bytes += len(data)
        return data

    def close(self):
        self.file.close()


class BoundedJSON:
    """Small strict streaming event parser; retains at most one bounded scalar token.

    Object/array containers are never materialized. Event format matches ijson.parse.
    This fallback is slower than ijson but needs no installation.
    """
    def __init__(self, reader):
        self.reader = reader
        self.buf = ""
        self.pos = 0
        self.eof = False
        self.decoder = codecs.getincrementaldecoder("utf-8-sig")()

    def fill(self):
        self.buf = self.buf[self.pos:]
        self.pos = 0
        if not self.eof:
            data = self.reader.read(CHUNK)
            self.eof = not data
            self.buf += self.decoder.decode(data, final=self.eof)

    def peek(self):
        while self.pos >= len(self.buf) and not self.eof:
            self.fill()
        return self.buf[self.pos:self.pos + 1]

    def whitespace(self):
        while True:
            m = WS_RE.match(self.buf, self.pos)
            self.pos = m.end()
            if self.pos < len(self.buf) or self.eof:
                return
            self.fill()

    def expect(self, char):
        self.whitespace()
        if self.peek() != char:
            raise ValueError("Malformed JSON: expected " + repr(char))
        self.pos += 1

    def scalar(self):
        self.whitespace()
        if self.peek() == '"':
            while True:
                m = STR_RE.match(self.buf, self.pos)
                if m:
                    token = m.group()
                    if len(token) > TOKEN_LIMIT:
                        raise ValueError("JSON string exceeds 1 MiB safety limit")
                    self.pos = m.end()
                    return "string", json.loads(token)
                if self.eof or len(self.buf) - self.pos > TOKEN_LIMIT:
                    raise ValueError("Unterminated/oversized JSON string")
                self.fill()
        while True:
            m = SCALAR_RE.match(self.buf, self.pos)
            if not m:
                raise ValueError("Missing JSON value")
            token = m.group()
            if len(token) > TOKEN_LIMIT:
                raise ValueError("JSON token exceeds safety limit")
            if m.end() == len(self.buf) and not self.eof:
                self.fill()
                continue
            self.pos = m.end()
            if token == "null":
                return "null", None
            if token in ("true", "false"):
                return "boolean", token == "true"
            if not NUM_RE.fullmatch(token):
                raise ValueError("Invalid JSON scalar")
            val = json.loads(token)
            if isinstance(val, float) and not math.isfinite(val):
                raise ValueError("Non-finite JSON number")
            return "number", val

    def value(self, prefix="", depth=0):
        if depth > 128:
            raise ValueError("JSON nesting exceeds 128")
        self.whitespace()
        char = self.peek()
        if char == "{":
            self.pos += 1
            yield prefix, "start_map", None
            self.whitespace()
            if self.peek() != "}":
                while True:
                    if self.peek() != '"':
                        raise ValueError("Expected JSON map key")
                    _, key = self.scalar()
                    yield prefix, "map_key", key
                    self.expect(":")
                    yield from self.value(prefix + "." + key if prefix else key, depth + 1)
                    self.whitespace()
                    if self.peek() != ",":
                        break
                    self.pos += 1
                    self.whitespace()
            self.expect("}")
            yield prefix, "end_map", None
        elif char == "[":
            self.pos += 1
            yield prefix, "start_array", None
            self.whitespace()
            if self.peek() != "]":
                while True:
                    yield from self.value(prefix + ".item" if prefix else "item", depth + 1)
                    self.whitespace()
                    if self.peek() != ",":
                        break
                    self.pos += 1
            self.expect("]")
            yield prefix, "end_array", None
        else:
            event, value = self.scalar()
            yield prefix, event, value

    def events(self):
        yield from self.value()
        self.whitespace()
        if self.peek():
            raise ValueError("Trailing JSON content")


def stream(path, file_audits, engine="auto"):
    reader = TrackedReader(path)
    started = time.monotonic()
    backend = "stdlib_bounded"
    complete = False
    last_progress = started
    try:
        parser = None
        if engine != "stdlib":
            try:
                import ijson
                parser = ijson.parse(reader, use_float=True, buf_size=CHUNK)
                backend = "ijson_" + ijson.backend
            except ImportError:
                if engine == "ijson":
                    raise RuntimeError("ijson is not installed; use --parser stdlib or auto")
        for prefix, event, value in parser if parser is not None else BoundedJSON(reader).events():
            if time.monotonic() - last_progress >= 30:
                print("Reading", Path(path).name, ":", round(reader.bytes / 1048576, 1), "MiB", flush=True)
                last_progress = time.monotonic()
            yield prefix, event, value
        complete = True
    finally:
        reader.close()
        file_audits[str(Path(path))] = {"bytes_read": reader.bytes, "file_bytes": Path(path).stat().st_size, "sha256": reader.hash.hexdigest(), "complete": complete, "parser": backend, "seconds": round(time.monotonic() - started, 3)}


def scalar_event(event):
    return event in {"string", "number", "boolean", "null"}


def flat_rows(events, prefix):
    """Collect only direct scalar properties of each map at prefix."""
    row = None
    for p, event, value in events:
        if p == prefix and event == "start_map":
            row = {}
        elif p == prefix and event == "end_map":
            yield row
            row = None
        elif row is not None and scalar_event(event) and p.startswith(prefix + "."):
            suffix = p[len(prefix) + 1:]
            if "." not in suffix:
                row[suffix] = value


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def frame(value):
    return finite(value) and value >= 0 and float(value).is_integer()


def groups(verb):
    words = set(re.findall(r'[a-z]+', str(verb or "").lower()))
    result = ["all"]
    if words & {"take", "pick", "grab", "get"}:
        result.append("take")
    if words & {"put", "place", "leave", "drop"}:
        result.append("put")
    if words & INTERACTIONS:
        result.append("interaction")
    return result


def critical_check(action, namespace):
    values = action.get(namespace, {})
    result = {}
    start, end = (action.get("clip_start_frame"), action.get("clip_end_frame")) if namespace == "clip_critical_frames" else (action.get("start_frame"), action.get("end_frame"))
    bounded = frame(start) and frame(end) and start <= end
    for name in CRITICAL:
        if name not in values:
            result[name] = "missing"
        elif values[name] is None:
            result[name] = "null"
        elif not frame(values[name]):
            result[name] = "invalid_frame"
        elif not bounded:
            result[name] = "numeric_bounds_unavailable"
        elif start <= values[name] <= end:
            result[name] = "within_annotation_context"
        else:
            result[name] = "outside_annotation_context"
    if all(frame(values.get(name)) for name in ("pre_frame", "pnr_frame", "post_frame")):
        result["pre_pnr_post_order"] = "ordered" if values["pre_frame"] <= values["pnr_frame"] <= values["post_frame"] else "unordered"
    return result


def action_metrics(action):
    metrics = Counter({"actions": 1})
    for name in FLAGS:
        value = action.get(name, "missing")
        label = "true" if value is True else "false" if value is False else "null" if value is None else "missing" if value == "missing" else "invalid"
        metrics["flags/" + name + "/" + label] += 1
    metrics["status/explicitly_valid_nonpartial_unrejected" if action.get("is_valid_action") is True and all(action.get(k) is False for k in ("is_partial", "is_rejected", "is_invalid_annotation")) else "status/not_confirmed_complete_and_valid"] += 1
    for name in TIMES:
        value = action.get(name)
        metrics["time_fields/" + name + "/" + ("missing" if name not in action else "numeric" if finite(value) else "null_or_invalid")] += 1
    for namespace in ("critical_frames", "clip_critical_frames"):
        for name, status in critical_check(action, namespace).items():
            metrics[namespace + "/" + name + "/" + status] += 1
    explicitly_valid = action.get("is_valid_action") is True and all(action.get(k) is False for k in ("is_partial", "is_rejected", "is_invalid_annotation"))
    for namespace in ("critical_frames", "clip_critical_frames"):
        checked = critical_check(action, namespace)
        usable = all(checked.get(name) == "within_annotation_context" for name in ("pre_frame", "pnr_frame", "post_frame")) and checked.get("pre_pnr_post_order") == "ordered"
        if usable:
            metrics["quality/" + namespace + "/ordered_pre_pnr_post_within_context"] += 1
            if explicitly_valid:
                metrics["quality/" + namespace + "/explicit_valid_and_ordered_pre_pnr_post"] += 1
                if checked.get("contact_frame") == "within_annotation_context":
                    metrics["quality/" + namespace + "/explicit_valid_and_all_four_numeric_within_context"] += 1
    pairs = (("start_sec", "end_sec"), ("clip_start_sec", "clip_end_sec"))
    for start, end in pairs:
        if finite(action.get(start)) and finite(action.get(end)):
            duration = action[end] - action[start]
            label = "negative_or_zero" if duration <= 0 else "approximately_8s" if abs(duration - 8) < 0.001 else "positive_other"
            metrics["duration/" + start + "/" + label] += 1
    if action.get("state_transition") is not None:
        metrics["state_transition/non_null"] += 1
    for name, count in action.get("spatial_counts", {}).items():
        metrics["spatial/" + name] += count
    if action.get("critical_frames") and action.get("clip_critical_frames"):
        differences = [action["critical_frames"][k] - action["clip_critical_frames"][k] for k in CRITICAL if frame(action["critical_frames"].get(k)) and frame(action["clip_critical_frames"].get(k))]
        if differences:
            metrics["coordinates/parent_minus_clip_offset_consistent" if max(differences) == min(differences) else "coordinates/parent_minus_clip_offset_inconsistent"] += 1
    return metrics


def compact_example(action):
    keep = {k: v for k, v in action.items() if k in ACTION_FIELDS or k in ("critical_frames", "clip_critical_frames", "spatial_counts", "structured_object_labels")}
    # Never return free narration or freeform verbs; structured labels are sufficient.
    keep.pop("freeform_verb", None)
    keep["critical_validity"] = {k: critical_check(action, k) for k in ("critical_frames", "clip_critical_frames")}
    return keep


def inspect_main(path, clip_index, blocked, groups_map, local_clips, file_audits, engine, max_examples):
    stats = defaultdict(Counter)
    video_sets = defaultdict(set)
    clip_sets = defaultdict(set)
    verbs = Counter()
    examples = []
    structure = Counter()
    vprefix = "videos.item"
    iprefix = vprefix + ".annotated_intervals.item"
    aprefix = iprefix + ".narrated_actions.item"
    video = interval = action = None
    def finish_video():
        parent = video.get("video_uid")
        related = closure({parent}, groups_map) if isinstance(parent, str) else set()
        parent_blocked = bool(related & blocked)
        for item in video["intervals"]:
            uid = item.get("clip_uid")
            idx = clip_index.get(uid, {})
            splits = idx.get("splits", set())
            reasons = []
            if not isinstance(parent, str) or not isinstance(uid, str):
                reasons.append("missing_identity")
            if parent_blocked:
                reasons.append("excluded_parent_or_related_group")
            if "train" not in splits:
                reasons.append("not_in_lta_train")
            if splits & {"val", "test"}:
                reasons.append("official_lta_holdout_clip")
            if idx.get("parents", set()) != {parent}:
                reasons.append("missing_or_conflicting_lta_parent_mapping")
            if item.get("redacted") is not False:
                reasons.append("redaction_not_explicitly_false")
            eligible = not reasons
            scopes = ["all", "local" if uid in local_clips else "not_local", "eligible_train" if eligible else "excluded_or_unresolved"]
            if eligible and uid in local_clips:
                scopes.append("eligible_train_and_local")
            for scope in scopes:
                for group, metrics in item["stats"].items():
                    key = scope + "/" + group
                    stats[key].update(metrics)
                    if parent:
                        video_sets[key].add(parent)
                    if uid:
                        clip_sets[key].add(uid)
            for reason in reasons:
                stats["eligibility_reasons"][reason] += item["stats"]["all"]["actions"]
            if len(examples) < max_examples:
                for sample in item["examples"]:
                    if len(examples) >= max_examples:
                        break
                    examples.append({"video_uid": parent, "clip_uid": uid, "lta_splits": sorted(splits), "local_nonempty_mp4": uid in local_clips, "eligible_train_source": eligible, "exclusion_reasons": reasons, "interval_redacted": item.get("redacted"), "action": sample})
    for p, event, value in stream(path, file_audits, engine):
        if p == vprefix and event == "start_map":
            structure["video_records"] += 1
            video = {"intervals": []}
        elif p == iprefix and event == "start_map":
            structure["interval_records"] += 1
            interval = {"stats": defaultdict(Counter), "examples": []}
        elif p == aprefix and event == "start_map":
            action = {"spatial_counts": Counter()}
        elif p == aprefix and event == "end_map":
            verb = action.get("structured_verb") or action.get("_structured_verb")
            verbs[str(verb or "<missing>")] += 1
            metrics = action_metrics(action)
            for group in groups(verb):
                interval["stats"][group].update(metrics)
            if set(groups(verb)) & {"take", "put"} and len(interval["examples"]) < 2:
                interval["examples"].append(compact_example(action))
            action = None
        elif p == iprefix and event == "end_map":
            structure["interval_redacted/" + str(interval.get("redacted", "missing"))] += 1
            if len(video["intervals"]) >= 512:
                raise ValueError("More than 512 intervals in one video; bounded-memory review required")
            video["intervals"].append(interval)
            interval = None
        elif p == vprefix and event == "end_map":
            finish_video()
            video = None
        elif action is not None and p.startswith(aprefix + "."):
            suffix = p[len(aprefix) + 1:]
            if scalar_event(event):
                if suffix in ACTION_FIELDS:
                    action[suffix] = value
                for namespace in ("critical_frames", "clip_critical_frames"):
                    if suffix.startswith(namespace + ".") and suffix[len(namespace) + 1:] in CRITICAL:
                        action.setdefault(namespace, {})[suffix[len(namespace) + 1:]] = value
                if suffix == "frames.item.frame_type":
                    action["spatial_counts"]["frames_" + str(value)] += 1
                if suffix == "frames.item.boxes.item.object_type":
                    action["spatial_counts"]["boxes_" + str(value)] += 1
                if suffix == "frames.item.boxes.item.structured_noun" and isinstance(value, str):
                    labels = action.setdefault("structured_object_labels", [])
                    if value not in labels and len(labels) < 8:
                        labels.append(value)
            if suffix == "frames.item.boxes.item" and event == "start_map":
                action["spatial_counts"]["boxes"] += 1
        elif interval is not None and scalar_event(event) and p.startswith(iprefix + "."):
            suffix = p[len(iprefix) + 1:]
            if "." not in suffix:
                interval[suffix] = value
        elif video is not None and scalar_event(event) and p == vprefix + ".video_uid":
            video["video_uid"] = value
    if not structure["video_records"] or not structure["interval_records"] or not stats["all/all"]["actions"]:
        raise ValueError("FHO main has no video/interval/action records; unexpected/empty schema")
    return {"structure": dict(structure), "counts": {k: dict(v) for k, v in sorted(stats.items())}, "unique_source_videos": {k: len(v) for k, v in sorted(video_sets.items())}, "unique_clips": {k: len(v) for k, v in sorted(clip_sets.items())}, "top_structured_verbs": dict(verbs.most_common(150)), "examples": examples}


def closure(ids, group_map):
    result = set(ids)
    while True:
        before = len(result)
        for group, members in group_map.items():
            component = set(members) | {group}
            if result & component:
                result.update(component)
        if before == len(result):
            return result


def collect_holdout(path, audits, engine):
    parents = set()
    for p, event, value in stream(path, audits, engine):
        if scalar_event(event) and p.rsplit(".", 1)[-1] in ("video_uid", "video_id") and isinstance(value, str):
            parents.add(value)
        if event == "map_key" and p.rsplit(".", 1)[-1] == "video_metadata" and UID_RE.fullmatch(str(value)):
            parents.add(value)
    return parents


def inspect_metadata(path, audits, engine):
    group_map, holdouts = {}, set()
    records = 0
    current = None
    for p, event, value in stream(path, audits, engine):
        if p == "videos.item" and event == "start_map":
            current = {"members": [], "holdout": False}
        elif p == "videos.item" and event == "end_map":
            records += 1
            if current.get("is_grouped") is True:
                if not current.get("video_uid") or not current["members"]:
                    raise ValueError("Grouped video lacks explicit member IDs")
                group_map[current["video_uid"]] = current["members"]
            if current["holdout"] and current.get("video_uid"):
                holdouts.add(current["video_uid"])
            current = None
        elif current is not None and scalar_event(event):
            if p in ("videos.item.video_uid", "videos.item.is_grouped"):
                current[p.rsplit(".", 1)[-1]] = value
            elif p == "videos.item.group_video_uids.item" and isinstance(value, str):
                current["members"].append(value)
            elif p.startswith("videos.item.split_") and value in ("val", "test"):
                current["holdout"] = True
    if not records:
        raise ValueError("Official metadata has no video records")
    return group_map, holdouts


def bounded_load(path, limit=4 * 1024 * 1024):
    if Path(path).stat().st_size > limit:
        raise ValueError("Small configuration exceeds size limit: " + str(path))
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def inspect_lta(path, split, index, taxonomy, blocked, group_map, local, audits, engine):
    counts = Counter()
    verbs = Counter()
    durations = Counter()
    sources = defaultdict(set)
    for row in flat_rows(stream(path, audits, engine), "clips.item"):
        counts["rows"] += 1
        uid, parent = row.get("clip_uid"), row.get("video_uid")
        if isinstance(uid, str):
            item = index.setdefault(uid, {"splits": set(), "parents": set()})
            item["splits"].add(split)
            item["parents"].add(parent)
        verb = row.get("verb")
        verbs[str(verb or "<missing>")] += 1
        for name in groups(verb):
            counts["actions/" + name] += 1
            if parent:
                sources[name].add(parent)
            if uid in local:
                counts["local_rows/" + name] += 1
            if split == "train" and parent and not closure({parent}, group_map) & blocked:
                counts["not_excluded_by_parent/" + name] += 1
                if uid in local:
                    counts["not_excluded_by_parent_and_local/" + name] += 1
        start, end = row.get("action_clip_start_sec"), row.get("action_clip_end_sec")
        if finite(start) and finite(end):
            duration = end - start
            durations["approximately_8s" if abs(duration - 8) < 0.001 else "positive_other" if duration > 0 else "nonpositive"] += 1
        else:
            durations["missing_or_invalid"] += 1
        for key, labels in (("verb", taxonomy.get("verbs", [])), ("noun", taxonomy.get("nouns", []))):
            n = row.get(key + "_label")
            if type(n) is int and 0 <= n < len(labels) and labels[n] == row.get(key):
                counts["taxonomy_match/" + key] += 1
            else:
                counts["taxonomy_unverified_or_mismatch/" + key] += 1
    if not counts["rows"]:
        raise ValueError("LTA file has no clips.item records: " + str(path))
    return {"counts": dict(counts), "unique_source_videos": {k: len(v) for k, v in sources.items()}, "duration_counts": dict(durations), "verb_counts": dict(verbs)}


def run(args):
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError("Output directory is not empty; choose a new directory")
    args.output.mkdir(parents=True, exist_ok=True)
    audits = {}
    report = {"schema": VERSION, "created_utc": datetime.now(timezone.utc).isoformat(), "status": "running", "read_only_sources": True, "video_downloads": 0, "api_calls": 0, "training_runs": 0, "notes": ["Counts describe annotations or nonempty file availability, not verified pickup/place completion labels.", "Missing annotations and other action verbs are never assigned negative labels.", "PRE/CONTACT/PNR/POST are distinct; POST is not assumed to be the first completion frame.", "Frame bounds use the annotation context; passing them does not verify visual alignment.", "Historical returned counts are separate in mac_inventory.json."]}
    try:
        policy = bounded_load(args.policy)
        blocked = set(policy["excluded_parent_ids"])
        group_map = dict(policy.get("group_to_canonical_parent_ids", {}))
        report["policy"] = {"path": str(args.policy), "sha256": hashlib.sha256(args.policy.read_bytes()).hexdigest(), "parent_count_before_current_scan": len(blocked), "description": policy.get("description")}
        report["annotation_file_inventory"] = [{"name": p.name, "bytes": p.stat().st_size} for p in sorted(args.annotations.glob("fho*.json"))]
        local, local_report = set(), []
        clip_roots = args.clips or [args.annotations.parent / "clips", args.project / "ego4d/v2/clips", args.project / "tenframe_comparison_20261026/downloads/ego4d/v2/clips", args.project / "next_source_check24/sources/ego4d/v2/clips"]
        for root in clip_roots:
            found, empty, byte_total = set(), 0, 0
            if root.is_dir():
                for p in root.glob("*.mp4"):
                    size = p.stat().st_size
                    if size > 0:
                        found.add(p.stem)
                        byte_total += size
                    else:
                        empty += 1
            local.update(found)
            local_report.append({"path": str(root), "exists": root.is_dir(), "nonempty_mp4": len(found), "empty_mp4": empty, "total_bytes": byte_total})
        report["local_clips"] = {"roots": local_report, "unique_nonempty_clip_ids": len(local), "verification": "Top-level *.mp4 filename and size only; no decoding or download."}
        metadata = args.metadata or args.annotations.parent.parent / "ego4d.json"
        if not metadata.is_file():
            raise FileNotFoundError("Required official metadata missing: " + str(metadata))
        current_groups, metadata_holdout = inspect_metadata(metadata, audits, args.parser)
        for group, members in current_groups.items():
            if group in group_map and set(members) != set(group_map[group]):
                raise ValueError("Official grouped-video membership changed; review required: " + group)
            group_map[group] = members
        blocked.update(metadata_holdout)
        holdout_reports = {}
        holdout_paths = sorted(set(args.annotations.glob("*_val.json")) | set(args.annotations.glob("*_test_unannotated.json")))
        required = {"fho_lta_val.json", "fho_lta_test_unannotated.json", "vq_val.json", "vq_test_unannotated.json"}
        if not required <= {p.name for p in holdout_paths}:
            raise ValueError("Required LTA/VQ official holdout files missing")
        for p in holdout_paths:
            print("Reading holdout identities:", p.name, flush=True)
            ids = collect_holdout(p, audits, args.parser)
            if not ids:
                raise ValueError("No parent identities resolved in holdout: " + p.name)
            blocked.update(ids)
            holdout_reports[p.name] = {"source_parent_ids": len(ids)}
        blocked = closure(blocked, group_map)
        report["isolation"] = {"official_holdout_files": holdout_reports, "metadata_holdout_parents": len(metadata_holdout), "official_group_count": len(group_map), "excluded_parents_including_groups": len(blocked), "fresh_metadata_and_holdouts_scanned": True, "conservative_existing_reservations_retained": True}
        taxonomy = bounded_load(args.annotations / "fho_lta_taxonomy.json")
        taxonomy_path = args.annotations / "fho_lta_taxonomy.json"
        audits[str(taxonomy_path)] = {"file_bytes": taxonomy_path.stat().st_size, "sha256": hashlib.sha256(taxonomy_path.read_bytes()).hexdigest(), "complete": True, "parser": "bounded_small_config"}
        main_taxonomy_path = args.annotations / "fho_main_taxonomy.json"
        if main_taxonomy_path.is_file():
            main_taxonomy = bounded_load(main_taxonomy_path)
            report["main_taxonomy"] = {"root_keys": sorted(main_taxonomy), "root_collection_sizes": {k: len(v) for k, v in main_taxonomy.items() if isinstance(v, (list, dict))}}
            audits[str(main_taxonomy_path)] = {"file_bytes": main_taxonomy_path.stat().st_size, "sha256": hashlib.sha256(main_taxonomy_path.read_bytes()).hexdigest(), "complete": True, "parser": "bounded_small_config"}
        report["taxonomy"] = {"verbs": len(taxonomy.get("verbs", [])), "nouns": len(taxonomy.get("nouns", [])), "interaction_verbs": [v for v in taxonomy.get("verbs", []) if "interaction" in groups(v)]}
        report["lta"] = {}
        clip_index = {}
        for split, name in (("train", "fho_lta_train.json"), ("val", "fho_lta_val.json"), ("test", "fho_lta_test_unannotated.json")):
            print("Reading action rows:", name, flush=True)
            report["lta"][split] = inspect_lta(args.annotations / name, split, clip_index, taxonomy, blocked, group_map, local, audits, args.parser)
        report["lta_clip_mapping"] = {"unique_clips": len(clip_index), "cross_split_clips": sum(len(v["splits"]) > 1 for v in clip_index.values()), "conflicting_parent_clips": sum(len(v["parents"]) > 1 for v in clip_index.values())}
        print("Reading FHO main; only counters and bounded examples are retained.", flush=True)
        report["fho_main"] = inspect_main(args.annotations / "fho_main.json", clip_index, blocked, group_map, local, audits, args.parser, args.examples)
        report["status"] = "complete"
    except Exception as exc:
        report["status"] = "incomplete"
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
    report["files_read"] = audits
    try:
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        report["peak_process_rss_mib"] = round(rss / (1048576 if sys.platform == "darwin" else 1024), 2)
    except ImportError:
        report["peak_process_rss_mib"] = None
    (args.output / "annotation_inventory.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print("Inventory status:", report["status"], "Output:", str(args.output / "annotation_inventory.json"), flush=True)
    return 0 if report["status"] == "complete" else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--clips", type=Path, action="append")
    parser.add_argument("--policy", type=Path, default=Path(__file__).parent / "protocol/exclusion_policy.json")
    parser.add_argument("--parser", choices=("auto", "stdlib", "ijson"), default="auto")
    parser.add_argument("--examples", type=int, choices=range(0, 17), default=8, metavar="0..16")
    return run(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
