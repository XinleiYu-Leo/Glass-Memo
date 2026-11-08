"""Offline, fixed-final-epoch paired comparison of four and ten input frames.

No teacher/API requests, weight downloads, relabeling, split changes, threshold
tuning or checkpoint selection are implemented. The CLI accepts real frozen
inputs only. Synthetic fixtures are available exclusively through Python tests.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import time

PACKAGE = Path(__file__).resolve().parents[1]
SOURCE_CHECKPOINT_SHA256 = "6c80dd106518f165331e7ba662ef33279d672265c6384c0f35c108a1489cb0d7"
SEEDS = (566, 567, 568)
ARMS = {"four_frame": (0, 3, 6, 9), "ten_frame": tuple(range(10))}
EPOCHS, FEATURE_DIM, SUMMARY_DIM = 40, 576, 1728
PIXEL_MEAN, PIXEL_STD = (.485, .456, .406), (.229, .224, .225)
HEX = re.compile(r"[0-9a-f]{64}\Z")
TOL = 1e-6
LIMITATIONS = (
    "Paired small development comparison with AI-consensus weak labels, not human gold, "
    "an independent final test, continuous-video recall or an on-device benchmark. "
    "Mean/max do not retain internal temporal order. The endpoints and last-minus-first "
    "summary are identical across arms; additional middle observations affect only mean/max. "
    "All three seeds and their mean are reported, without best-seed or dev selection."
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def read_json(path):
    def pairs(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "Duplicate JSON key: " + key)
            result[key] = value
        return result
    def invalid(value):
        raise ValueError("Non-finite JSON constant: " + value)
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=pairs,
                      parse_constant=invalid)


def save_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def number(value, name):
    require(type(value) in (int, float) and math.isfinite(value), "Expected finite number: " + name)
    return float(value)


def string_set(value, name, *, nonempty=True):
    require(isinstance(value, list) and (bool(value) or not nonempty), "Expected list: " + name)
    require(all(isinstance(x, str) and x for x in value) and len(value) == len(set(value)),
            "Invalid or duplicate identifiers: " + name)
    return set(value)


def checked_file(root, relative):
    require(isinstance(relative, str) and relative and "\\" not in relative, "Invalid frame path")
    path = Path(relative)
    require(not path.is_absolute() and ".." not in path.parts, "Frame path escapes dataset")
    result = (Path(root) / path).resolve()
    require(result.is_relative_to(Path(root).resolve()) and result.is_file(), "Missing or escaped frame: " + relative)
    return result


def closure(parents, groups):
    result = set(parents)
    while True:
        before = len(result)
        for members in groups.values():
            if result.intersection(members):
                result.update(members)
        if len(result) == before:
            return result


def validate_protocol(path):
    """Check executable constants against the frozen, hash-linked protocol."""
    path = Path(path)
    protocol = read_json(path)
    require(protocol.get("schema") == "robomemo-tenframe-comparison-protocol-v1", "Wrong protocol schema")
    required = {
        ("selection", "total"): 64, ("selection", "seed"): 20261026,
        ("selection", "split_counts"): {"train": 48, "dev": 16},
        ("input", "duration_seconds"): 1.5, ("input", "frame_count"): 10,
        ("input", "sampling_fps"): 6, ("input", "pixels"): 224,
        ("input", "baseline_indices_zero_based"): [0, 3, 6, 9],
        ("input", "tenframe_indices_zero_based"): list(range(10)),
        ("model", "encoder"): "mobilenet_v3_small", ("model", "encoder_frozen"): True,
        ("model", "encoder_source_checkpoint_sha256"): SOURCE_CHECKPOINT_SHA256,
        ("model", "encoder_features"): FEATURE_DIM, ("model", "summary_dimension"): SUMMARY_DIM,
        ("model", "trainable_parameters"): 1729,
        ("optimization", "seeds"): list(SEEDS), ("optimization", "epochs"): EPOCHS,
        ("optimization", "optimizer"): "AdamW", ("optimization", "learning_rate"): .001,
        ("optimization", "weight_decay"): .01, ("optimization", "threshold"): .5,
        ("labels", "min_train_total"): 24, ("labels", "min_train_each_class"): 8,
        ("labels", "min_dev_total"): 8, ("labels", "min_dev_each_class"): 3,
    }
    for (section, key), value in required.items():
        require(protocol.get(section, {}).get(key) == value, "Protocol differs from implementation: " + section + "." + key)
    artifacts = protocol.get("artifact_sources", {})
    for name in ("source_exclusions.json", "development_reservation.json", "actual_model_instructions.txt"):
        require(HEX.fullmatch(str(artifacts.get(name, ""))) and digest(path.parent / name) == artifacts[name],
                "Protocol artifact hash mismatch: " + name)
    return protocol, read_json(path.parent / "source_exclusions.json"), read_json(path.parent / "development_reservation.json")


def validate_selection(selection, protocol, protocol_hash, old, reservation, *, allow_synthetic=False):
    require(selection.get("schema") == "robomemo-tenframe-selection-frozen-v1", "Wrong selection schema")
    require(selection.get("status") == "selection_frozen_before_download_and_teacher", "Selection is not frozen and complete")
    require(selection.get("protocol_sha256") == protocol_hash, "Selection protocol hash mismatch")
    require(selection.get("synthetic_test") is False or (allow_synthetic and selection.get("synthetic_test") is True),
            "Real mode refuses synthetic or unmarked selection")
    require(selection.get("seed") == 20261026, "Selection seed is not frozen")
    require(selection.get("selection_content_sha256") == canonical_digest({
        "items": selection.get("items"), "rules": selection.get("selection_rules"),
        "pool_audit": selection.get("pool_audit"), "exclusions": selection.get("exclusions")}), "Selection content hash mismatch")
    rows = selection.get("items")
    require(isinstance(rows, list) and len(rows) == 64, "Exactly 64 selected items required")
    require([x.get("item_id") for x in rows] == [f"T{i:02d}" for i in range(1, 65)], "Expected ordered T01..T64 selection")
    expected_counts = protocol["selection"]["stratum_counts"]
    for split in ("train", "dev"):
        require(dict(Counter(x.get("stratum") for x in rows if x.get("split") == split)) == expected_counts[split],
                "Frozen split/stratum quota mismatch")
    exclusions = selection.get("exclusions", {})
    names = ("declared_train_parent_ids", "declared_dev_parent_ids", "official_val_parent_ids",
             "official_test_parent_ids", "metadata_holdout_parent_ids", "reserved_parent_ids",
             "diagnostic_parent_ids", "diagnostic_known_related_parent_ids")
    sets = {name: string_set(exclusions.get(name), name) for name in names}
    require(sets["declared_train_parent_ids"] == set(old["declared_train_parents"]) and
            sets["declared_dev_parent_ids"] == set(old["declared_dev_parents"]), "Old source exclusion mismatch")
    require(reservation.get("exclude_from_next_training_round") is True and
            sets["diagnostic_parent_ids"] == set(reservation["parent_ids"]) and
            sets["diagnostic_known_related_parent_ids"] == set(reservation["known_related_parent_ids"]),
            "Prior diagnostic reservation mismatch")
    require(sets["diagnostic_parent_ids"] <= sets["diagnostic_known_related_parent_ids"], "Diagnostic related set incomplete")
    require(sets["reserved_parent_ids"] <= sets["official_val_parent_ids"], "Reserved parents missing from official validation union")
    reserved_clips = string_set(exclusions.get("reserved_clip_ids"), "reserved_clip_ids")
    require(len(reserved_clips) == 60, "Expected 60 reserved clips")
    resolution = selection.get("group_resolution", {})
    groups = resolution.get("group_to_canonical_parent_ids")
    require(isinstance(groups, dict), "Missing official group resolution")
    groups = {name: string_set(members, "official group " + name) for name, members in groups.items()}
    base = set().union(*sets.values())
    blocked = closure(base, groups)
    require(string_set(exclusions.get("excluded_parent_ids"), "excluded_parent_ids") == blocked,
            "Full exclusion union or official group closure mismatch")
    require(string_set(resolution.get("blocked_parent_ids_before_group_closure"), "blocked-before") == base and
            string_set(resolution.get("additional_blocked_parent_ids_from_group_closure"), "blocked-additional", nonempty=False) == blocked - base,
            "Group closure evidence mismatch")
    sources = selection.get("source_files", {})
    for name in ("fho_lta_val.json", "fho_lta_test_unannotated.json", "vq_val.json", "vq_test_unannotated.json"):
        require(HEX.fullmatch(str(sources.get(name, {}).get("sha256", ""))), "Missing official holdout source hash: " + name)
    require(HEX.fullmatch(str(resolution.get("metadata_sha256", ""))), "Missing official metadata hash")
    repeat_ids = string_set(selection.get("repeat_item_ids"), "repeat_item_ids")
    require(len(repeat_ids) == 6 and repeat_ids == {r["item_id"] for r in rows if r.get("repeat_selected") is True}, "Six predeclared repeat items required")
    require(Counter((r["split"], r["stratum"]) for r in rows if r["item_id"] in repeat_ids) ==
            Counter((split, stratum) for split in ("train", "dev") for stratum in expected_counts[split]),
            "Repeats must cover each split and stratum exactly once")
    used_groups, used_clips = set(), set()
    for i, row in enumerate(rows):
        require(row.get("split") == ("train" if i < 48 else "dev"), "Frozen split assignment changed")
        related = string_set(row.get("related_group_parent_ids"), row["item_id"] + " related parents")
        require(isinstance(row.get("video_uid"), str) and related == closure({row["video_uid"]}, groups), "Incomplete source related-group closure")
        require(not related & blocked, "Excluded source or related group in selection")
        require(not related & used_groups, "Source/group overlap between selected items")
        require(isinstance(row.get("clip_uid"), str) and row["clip_uid"] not in reserved_clips | used_clips, "Reserved or repeated clip")
        used_groups.update(related)
        used_clips.add(row["clip_uid"])
    return rows, repeat_ids


def validate_labels(labels, rows, protocol_hash, manifest_hash, repeat_ids):
    require(labels.get("schema") == "robomemo-tenframe-reviewed-labels-v1", "Wrong reviewed-label schema")
    require(labels.get("protocol_sha256") == protocol_hash, "Reviewed label protocol hash mismatch")
    require(labels.get("dataset_manifest_sha256") == manifest_hash, "Reviewed label dataset hash mismatch")
    entries = labels.get("items")
    require(isinstance(entries, list) and len(entries) == 64, "All 64 reviewed rows required")
    require([x.get("item_id") for x in entries] == [x["item_id"] for x in rows], "Unknown, duplicate, missing or reordered reviewed item")
    accepted = []
    decisions = {None, "present", "absent", "unclear", "rule_unsure"}
    binary = {"present": 1, "absent": 0}
    for row, review in zip(rows, entries):
        item = row["item_id"]
        require(review.get("split") == row["split"], "Reviewed split changed: " + item)
        label = review.get("label")
        require(label is None or (type(label) is int and label in (0, 1)), "Label must be binary integer or null: " + item)
        require(review.get("status") in ("accepted", "held"), "Unknown review status: " + item)
        require(review.get("input_scope") == "same_ten_224_pngs" and review.get("blind_review_saw_teacher") is False,
                "Review must be blind and limited to the shared ten PNGs: " + item)
        teacher, blind = review.get("teacher_decision"), review.get("blind_review_decision")
        require(teacher in decisions and blind in decisions, "Invalid review decision: " + item)
        require(review.get("blind_review_id") is None or isinstance(review.get("blind_review_id"), str), "Invalid blind review ID")
        repeats = review.get("repeat_decisions")
        require(isinstance(repeats, list) and all(x in decisions for x in repeats), "Invalid repeat decisions: " + item)
        require(len(repeats) <= (1 if item in repeat_ids else 0), "Unexpected/extra repeat decision: " + item)
        evidence = review.get("evidence_frame_ids")
        frame_ids = [f"f{i:02d}" for i in range(1, 11)]
        require(isinstance(evidence, list) and all(isinstance(x, str) and x in frame_ids for x in evidence) and
                len(evidence) == len(set(evidence)), "Invalid evidence frame IDs: " + item)
        require(evidence == sorted(evidence, key=frame_ids.index), "Evidence must be chronological: " + item)
        require(isinstance(review.get("rationale"), str), "Missing review rationale: " + item)
        if review["status"] == "held":
            require(label is None, "Held or unknown items cannot carry binary labels: " + item)
            continue
        require(teacher in binary and teacher == blind and label == binary[teacher], "Accepted label lacks agreeing binary blind reviews: " + item)
        require(isinstance(review.get("blind_review_id"), str) and review["blind_review_id"].strip(), "Accepted review missing independent reviewer identity: " + item)
        require((item not in repeat_ids and not repeats) or (item in repeat_ids and repeats == [teacher]),
                "Accepted repeat missing, unknown or disagrees: " + item)
        require(len(evidence) >= (2 if label == 1 else 1) and review["rationale"].strip(), "Accepted label lacks before/after or non-target evidence: " + item)
        accepted.append({**row, "label": label, "review": review})
    counts = {}
    for split, minimum, each in (("train", 24, 8), ("dev", 8, 3)):
        subset = [r for r in accepted if r["split"] == split]
        classes = Counter(r["label"] for r in subset)
        counts[split] = {"accepted": len(subset), "positive": classes[1], "negative": classes[0],
                         "held": sum(r["split"] == split for r in rows) - len(subset)}
        require(len(subset) >= minimum and min(classes[0], classes[1]) >= each,
                f"INSUFFICIENT_ACCEPTED_LABELS: {split} needs >= {minimum} total and >= {each} each class; got {counts[split]}")
    return accepted, counts


def validate_inputs(dataset, selection_path, labels_path, protocol_path, *, allow_synthetic=False):
    """All 64 source/image rows are checked, including held rows, before torch loads."""
    from PIL import Image
    dataset, selection_path, labels_path, protocol_path = map(Path, (dataset, selection_path, labels_path, protocol_path))
    protocol, old, reservation = validate_protocol(protocol_path)
    protocol_hash = digest(protocol_path)
    selection = read_json(selection_path)
    selected, repeats = validate_selection(selection, protocol, protocol_hash, old, reservation, allow_synthetic=allow_synthetic)
    manifest_path = dataset / "manifest.json"
    manifest = read_json(manifest_path)
    require(manifest.get("schema") == "robomemo-tenframe-dataset-v1", "Wrong dataset schema")
    require(manifest.get("synthetic_test") is False or (allow_synthetic and manifest.get("synthetic_test") is True), "Real mode refuses synthetic or unmarked dataset")
    require(manifest.get("synthetic_test") == selection.get("synthetic_test"), "Dataset/selection synthetic flags disagree")
    require(manifest.get("protocol_sha256") == protocol_hash, "Dataset protocol hash mismatch")
    require(manifest.get("selection_sha256") == digest(selection_path), "Dataset selection hash mismatch")
    report_path = dataset.parent / "independent_validation.json"
    validation = read_json(report_path)
    require(validation.get("schema") == "robomemo-tenframe-export-validation-v1" and validation.get("status") == "passed",
            "Independent source and decode validation is missing or not passed")
    require(validation.get("synthetic_test") == manifest["synthetic_test"], "Independent validation synthetic marker mismatch")
    for key, value in (("protocol_sha256", protocol_hash), ("selection_sha256", digest(selection_path)),
                       ("dataset_manifest_sha256", digest(manifest_path))):
        require(validation.get(key) == value, "Independent validation hash mismatch: " + key)
    require(validation.get("independent_official_isolation_replay") is True, "Independent official isolation replay required")
    isolation = validation.get("isolation", {})
    require(isolation.get("selected_independent_group_components") == 64 and isolation.get("selected_parents") == 64 and
            isolation.get("selected_overlap") == 0, "Independent source isolation did not pass")
    replay = isolation.get("independent_seed_replay", {})
    require(replay.get("seed") == 20261026 and replay.get("all_64_items_and_windows_reproduced") is True and
            replay.get("all_six_repeat_choices_reproduced") is True, "Independent frozen-selection replay did not pass")
    decoded = validation.get("dataset", {})
    require(decoded.get("png_count") == 640 and decoded.get("case_count") == 64 and decoded.get("source_decode_verified") is True,
            "Independent original-source decode validation required")
    decoded_rows = decoded.get("items")
    require(isinstance(decoded_rows, list) and len(decoded_rows) == 64 and
            [r.get("item_id") for r in decoded_rows] == [r["item_id"] for r in selected] and
            all(r.get("png_count") == 10 and r.get("unique_source_frames") == 10 and r.get("original_decode_verified") is True for r in decoded_rows),
            "Independent decode evidence must cover all 64 ten-frame cases")
    rows = manifest.get("items")
    require(isinstance(rows, list) and len(rows) == 64, "Dataset must contain all 64 items")
    checked = []
    hashes_by_split = {"train": set(), "dev": set()}
    all_paths = set()
    for row, source in zip(rows, selected):
        for key in ("item_id", "split", "video_uid", "clip_uid", "related_group_parent_ids", "target_start_sec", "target_end_sec"):
            require(row.get(key) == source.get(key), "Dataset differs from frozen selection: " + key)
        start, end = number(row["target_start_sec"], "target start"), number(row["target_end_sec"], "target end")
        require(start >= 0 and abs(end - start - 1.5) <= TOL, "Target must be a fixed causal 1.5 second window")
        frames = row.get("frames")
        require(isinstance(frames, list) and len(frames) == 10, "Exactly ten source frames required")
        require([f.get("frame_id") for f in frames] == [f"f{i:02d}" for i in range(1, 11)], "Frame IDs/order mismatch")
        indices, actual_times, paths = [], [], []
        for i, frame in enumerate(frames):
            requested, actual = number(frame.get("requested_time_sec"), "requested time"), number(frame.get("actual_time_sec"), "actual time")
            require(abs(requested - (start + i / 6)) <= TOL, "Requested frame is outside fixed six-fps grid")
            require(start - TOL <= actual <= requested + TOL and actual <= end + TOL, "Future or out-of-window frame")
            index = frame.get("source_frame_index")
            require(type(index) is int and index >= 0, "Invalid source frame index")
            indices.append(index)
            actual_times.append(actual)
            relative = frame.get("path")
            require(relative == f"student_frames/{row['item_id']}_f{i + 1:02d}.png", "Unexpected student image path")
            path = checked_file(dataset, relative)
            require(str(path) not in all_paths, "Repeated image path")
            all_paths.add(str(path))
            require(HEX.fullmatch(str(frame.get("sha256", ""))) and digest(path) == frame["sha256"], "Student image hash mismatch: " + relative)
            with Image.open(path) as image:
                require(image.format == "PNG" and image.mode == "RGB" and image.size == (224, 224), "Expected exact RGB 224x224 PNG: " + relative)
                image.load()
            hashes_by_split[row["split"]].add(frame["sha256"])
            paths.append(str(path))
        require(len(set(indices)) == 10 and all(a < b for a, b in zip(indices, indices[1:])), "Ten distinct, increasing source frames required")
        require(all(a < b for a, b in zip(actual_times, actual_times[1:])), "Actual frame timestamps must increase")
        checked.append({**row, "resolved_images": paths})
    require(not hashes_by_split["train"] & hashes_by_split["dev"], "Identical frame bytes cross train/dev")
    accepted, counts = validate_labels(read_json(labels_path), checked, protocol_hash, digest(manifest_path), repeats)
    provenance = {"protocol_sha256": protocol_hash, "selection_sha256": digest(selection_path),
                  "dataset_manifest_sha256": digest(manifest_path), "reviewed_labels_sha256": digest(labels_path),
                  "independent_validation_sha256": digest(report_path),
                  "source_checkpoint_sha256": SOURCE_CHECKPOINT_SHA256, "accepted_counts": counts,
                  "accepted_item_ids": [r["item_id"] for r in accepted],
                  "held_item_ids": [r["item_id"] for r in checked if r["item_id"] not in {a["item_id"] for a in accepted}],
                  "repeat_item_ids": sorted(repeats), "all_64_images_checked": True,
                  "source_and_related_group_isolation_verified": True, "synthetic_test": manifest["synthetic_test"]}
    return accepted, provenance


def state_digest(state):
    result = hashlib.sha256()
    for key, value in sorted(state.items()):
        value = value.detach().cpu().contiguous()
        result.update(key.encode())
        result.update(str((str(value.dtype), list(value.shape))).encode())
        result.update(value.numpy().tobytes())
    return result.hexdigest()


def frozen_encoder(state, device="cpu"):
    from torch import nn
    from torchvision.models import mobilenet_v3_small
    model = mobilenet_v3_small(weights=None)  # Absolutely no pretrained-weight network request.
    encoder = nn.Sequential(model.features, model.avgpool, nn.Flatten(1))
    encoder.load_state_dict(state, strict=True)
    encoder.eval().to(device)
    for parameter in encoder.parameters():
        parameter.requires_grad_(False)
    return encoder


def load_encoder(checkpoint, device):
    import torch
    require(digest(checkpoint) == SOURCE_CHECKPOINT_SHA256, "Encoder source checkpoint hash mismatch")
    source = torch.load(checkpoint, map_location="cpu", weights_only=True)
    require(isinstance(source.get("encoder_state"), dict), "Source checkpoint has no encoder state")
    # Only the encoder is imported. Old classifier, labels and feature statistics are ignored.
    return frozen_encoder(source["encoder_state"], device)


def extract_features(rows, encoder, device):
    import numpy as np
    from PIL import Image
    import torch
    before = state_digest(encoder.state_dict())
    require(not encoder.training and not any(p.requires_grad for p in encoder.parameters()), "Encoder must be frozen and eval")
    found, unique = {}, {}
    for row in rows:
        for frame, path in zip(row["frames"], row["resolved_images"]):
            unique.setdefault(frame["sha256"], path)
    pending = list(unique.items())
    mean, std = torch.tensor(PIXEL_MEAN)[:, None, None], torch.tensor(PIXEL_STD)[:, None, None]
    for start in range(0, len(pending), 32):
        batch, tensors = pending[start:start + 32], []
        for image_hash, path in batch:
            require(digest(path) == image_hash, "Image changed after validation")
            with Image.open(path) as image:
                require(image.mode == "RGB" and image.size == (224, 224), "Input changed after validation")
                array = np.array(image, dtype=np.float32) / 255.0
            tensors.append((torch.from_numpy(array).permute(2, 0, 1) - mean) / std)
        with torch.inference_mode():
            features = encoder(torch.stack(tensors).to(device)).cpu()
        require(features.shape == (len(batch), FEATURE_DIM) and bool(torch.isfinite(features).all()), "Invalid encoder features")
        for (image_hash, _), feature in zip(batch, features):
            found[image_hash] = feature.clone()
        print(f"FEATURE_IMAGES {min(start + 32, len(pending))}/{len(pending)}", flush=True)
    require(state_digest(encoder.state_dict()) == before, "Frozen encoder changed during feature extraction")
    values = torch.stack([torch.stack([found[f["sha256"]] for f in r["frames"]]) for r in rows])
    return values, {"unique_images_encoded_once": len(pending), "encoder_sha256": before,
                    "features_sha256": state_digest({"features": values}), "shape": list(values.shape)}


def summarize(features, arm):
    import torch
    require(arm in ARMS and features.ndim == 3 and tuple(features.shape[1:]) == (10, FEATURE_DIM), "Expected [N,10,576] shared features")
    require(bool(torch.isfinite(features).all()), "Non-finite shared features")
    x = features[:, ARMS[arm], :]
    return torch.cat((x.mean(1), x.max(1).values, x[:, -1] - x[:, 0]), dim=1)


def normalize_from_train(train, dev):
    require(train.ndim == dev.ndim == 2 and train.shape[1] == dev.shape[1] == SUMMARY_DIM, "Expected summary matrices")
    mean = train.mean(0)
    std = train.std(0, unbiased=False).clamp_min(.05)
    return (train - mean) / std, (dev - mean) / std, mean, std


def make_head(seed, device="cpu"):
    import torch
    torch.manual_seed(seed)
    # CPU initialization is identical even when subsequent training uses CUDA.
    head = torch.nn.Linear(SUMMARY_DIM, 1)
    require(sum(p.numel() for p in head.parameters()) == 1729, "Head capacity mismatch")
    return head.to(device)


def metrics(logits, target):
    import torch
    require(logits.shape == target.shape and bool(torch.isfinite(logits).all()), "Invalid prediction logits")
    positive, truth = logits.sigmoid() >= .5, target == 1
    tp, fp = int((positive & truth).sum()), int((positive & ~truth).sum())
    fn, tn = int((~positive & truth).sum()), int((~positive & ~truth).sum())
    require(tp + fn > 0 and tn + fp > 0, "Both evaluation classes required")
    return {"balanced_accuracy": .5 * (tp / (tp + fn) + tn / (tn + fp)),
            "precision": tp / max(1, tp + fp), "recall": tp / (tp + fn),
            "f1": 2 * tp / max(1, 2 * tp + fp + fn),
            "unweighted_bce": float(torch.nn.functional.binary_cross_entropy_with_logits(logits, target)),
            "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn}}


def atomic_torch_save(path, value):
    import torch
    temporary = Path(path).with_suffix(".tmp")
    torch.save(value, temporary)
    temporary.replace(path)


def verify_final_checkpoint(path, features, expected_logits, expected_encoder_hash, device):
    import torch
    saved = torch.load(path, map_location="cpu", weights_only=True)
    require(saved["saved_epoch"] == EPOCHS and saved["threshold"] == .5, "Final checkpoint settings changed")
    require(state_digest(saved["encoder_state"]) == expected_encoder_hash, "Saved encoder changed")
    restored_encoder = frozen_encoder(saved["encoder_state"])
    require(state_digest(restored_encoder.state_dict()) == expected_encoder_hash, "Reloaded encoder changed")
    restored = make_head(saved["seed"], device).eval()
    restored.load_state_dict(saved["head_state"], strict=True)
    x = (summarize(features, saved["arm"]) - saved["summary_mean"]) / saved["summary_std"]
    with torch.no_grad():
        logits = restored(x.to(device)).squeeze(-1).cpu()
    require(torch.equal(logits, expected_logits.cpu()), "Final checkpoint reload predictions differ")
    return {"exact_logits_equal": True, "encoder_hash_unchanged": True,
            "checkpoint_sha256": digest(path), "head_sha256": state_digest(saved["head_state"])}


def fit_comparison(features, rows, output, encoder_state, provenance, device="cpu", *, synthetic_test=False):
    """Core fixture seam: synthetic data must be explicitly marked by the caller."""
    import torch
    require(provenance.get("synthetic_test") is synthetic_test, "Explicit synthetic fixture flag mismatch")
    require(isinstance(synthetic_test, bool), "Synthetic marker must be boolean")
    require(features.shape == (len(rows), 10, FEATURE_DIM) and bool(torch.isfinite(features).all()), "Bad shared feature tensor")
    require(len({r["item_id"] for r in rows}) == len(rows), "Duplicate accepted row")
    for split, minimum, each in (("train", 24, 8), ("dev", 8, 3)):
        counts = Counter(r["label"] for r in rows if r["split"] == split)
        require(set(counts) == {0, 1} and sum(counts.values()) >= minimum and min(counts.values()) >= each, "Accepted class gate failed before fitting")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    require(not any((output / f"seed_{seed}_{arm}").exists() for seed in SEEDS for arm in ARMS), "Run artifacts already exist")
    features = features.detach().cpu().float()
    train_indices = [i for i, row in enumerate(rows) if row["split"] == "train"]
    dev_indices = [i for i, row in enumerate(rows) if row["split"] == "dev"]
    labels = torch.tensor([r["label"] for r in rows], dtype=torch.float32)
    ty, dy = labels[train_indices].to(device), labels[dev_indices].to(device)
    pos_weight = (1 - ty).sum() / ty.sum()
    encoder_hash = state_digest(encoder_state)
    encoder_state = {k: v.detach().cpu().clone() for k, v in encoder_state.items()}
    runs, paired = [], []
    prepared = {}
    for arm in ARMS:
        summaries = summarize(features, arm)
        tx, dx, mean, std = normalize_from_train(summaries[train_indices], summaries[dev_indices])
        prepared[arm] = (tx.to(device), dx.to(device), mean, std)
    require(torch.equal(summarize(features, "four_frame")[:, 1152:], summarize(features, "ten_frame")[:, 1152:]), "Paired endpoints differ")
    for seed in SEEDS:
        initial_hash = None
        for arm in ARMS:
            directory = output / f"seed_{seed}_{arm}"
            directory.mkdir(exist_ok=False)
            tx, dx, mean, std = prepared[arm]
            head = make_head(seed, device)
            current_hash = state_digest(head.state_dict())
            if initial_hash is None:
                initial_hash = current_hash
            require(current_hash == initial_hash, "Paired classifier initialization differs")
            optimizer = torch.optim.AdamW(head.parameters(), lr=.001, weight_decay=.01)
            loss_fn = torch.nn.BCEWithLogitsLoss(pos_weight=pos_weight)
            history = []
            for epoch in range(1, EPOCHS + 1):
                head.train()
                optimizer.zero_grad(set_to_none=True)
                loss = loss_fn(head(tx).squeeze(-1), ty)  # Same fixed full-batch row order in both arms.
                require(bool(torch.isfinite(loss)), "Non-finite training loss")
                loss.backward()
                optimizer.step()
                history.append({"epoch": epoch, "weighted_training_bce_before_update": float(loss.detach())})
                if epoch == 1 or epoch % 10 == 0:
                    print(f"TRAIN seed={seed} arm={arm} epoch={epoch}/{EPOCHS}", flush=True)
            head.eval()
            with torch.no_grad():
                train_logits = head(tx).squeeze(-1).cpu()
                dev_logits = head(dx).squeeze(-1).cpu()
            checkpoint = {"schema": "robomemo-tenframe-final-checkpoint-v1", "arm": arm, "seed": seed,
                          "saved_epoch": EPOCHS, "checkpoint_rule": "fixed_final_epoch_no_dev_selection",
                          "head_state": {k: v.detach().cpu().clone() for k, v in head.state_dict().items()},
                          "encoder_state": encoder_state, "encoder_sha256": encoder_hash,
                          "summary_mean": mean, "summary_std": std, "summary_dimension": SUMMARY_DIM,
                          "summary_recipe": "mean,max,last-first", "frame_indices_zero_based": list(ARMS[arm]),
                          "threshold": .5, "pixel_mean": list(PIXEL_MEAN), "pixel_std": list(PIXEL_STD),
                          "input_size": 224, "input_format": "RGB PNG already letterboxed",
                          "normalization_population_std_floor": .05, "pos_weight": float(pos_weight),
                          "initial_head_sha256": initial_hash, "provenance": provenance,
                          "synthetic_test": synthetic_test, "limitations": LIMITATIONS}
            checkpoint_path = directory / "final.pt"
            atomic_torch_save(checkpoint_path, checkpoint)
            verification = verify_final_checkpoint(checkpoint_path, features[dev_indices], dev_logits, encoder_hash, device)
            predictions = []
            for split, indices, logits in (("train", train_indices, train_logits), ("dev", dev_indices, dev_logits)):
                for index, logit, probability in zip(indices, logits.tolist(), logits.sigmoid().tolist()):
                    predictions.append({"item_id": rows[index]["item_id"], "split": split, "label": rows[index]["label"],
                                        "logit": logit, "probability": probability, "prediction": int(probability >= .5)})
            save_json(directory / "predictions.json", predictions)
            save_json(directory / "history.json", history)
            result = {"seed": seed, "arm": arm, "saved_epoch": EPOCHS, "threshold": .5,
                      "initial_head_sha256": initial_hash, "trainable_parameters": 1729,
                      "train": metrics(train_logits, ty.cpu()), "dev": metrics(dev_logits, dy.cpu()),
                      "checkpoint": str(checkpoint_path.relative_to(output)), "reload": verification,
                      "predictions_sha256": digest(directory / "predictions.json"),
                      "history_sha256": digest(directory / "history.json"),
                      "summary_mean_sha256": state_digest({"mean": mean}), "summary_std_sha256": state_digest({"std": std})}
            save_json(directory / "result.json", result)
            runs.append(result)
        four, ten = runs[-2:]
        paired.append({"seed": seed, "direction": "ten_frame_minus_four_frame", "dev": {
            key: ten["dev"][key] - four["dev"][key] for key in ("balanced_accuracy", "precision", "recall", "f1", "unweighted_bce")}})
    require(len(runs) == 6 and {(r["seed"], r["arm"]) for r in runs} == {(s, a) for s in SEEDS for a in ARMS}, "Incomplete paired runs")
    require(state_digest(encoder_state) == encoder_hash, "Frozen encoder changed during training")
    metric_names = ("balanced_accuracy", "precision", "recall", "f1", "unweighted_bce")
    means = {arm: {key: sum(r["dev"][key] for r in runs if r["arm"] == arm) / len(SEEDS) for key in metric_names} for arm in ARMS}
    baseline = {"constant_probability": .5, "threshold": .5, "rule": "predict positive for every row; finite BCE at probability 0.5",
                "train": metrics(torch.zeros_like(ty.cpu()), ty.cpu()), "dev": metrics(torch.zeros_like(dy.cpu()), dy.cpu())}
    report = {"schema": "robomemo-tenframe-comparison-results-v1", "status": "synthetic_test_complete" if synthetic_test else "complete",
              "synthetic_test": synthetic_test, "provenance": provenance, "seeds": list(SEEDS), "epochs": EPOCHS,
              "threshold": .5, "checkpoint_rule": "fixed_final_epoch_no_dev_selection", "runs": runs,
              "paired_differences": paired, "mean_dev_metrics": means,
              "mean_paired_difference": {key: sum(x["dev"][key] for x in paired) / len(SEEDS) for key in metric_names},
              "all_positive_baseline": baseline, "encoder_sha256": encoder_hash, "encoder_unchanged": True,
              "shared_features_sha256": state_digest({"features": features}), "limitations": LIMITATIONS}
    save_json(output / "results.json", report)
    return report


def make_review_archive(output):
    """Package only the explicit JSON review allowlist; checkpoints stay local."""
    import zipfile
    output = Path(output)
    names = ["results.json", "validated_inputs.json", "feature_extraction.json", "artifact_manifest.json", "status.json"]
    names.extend(f"seed_{seed}_{arm}/{name}" for seed in SEEDS for arm in ARMS
                 for name in ("result.json", "predictions.json", "history.json"))
    names = sorted(names)
    archive = output / "results_for_review.zip"
    require(not archive.exists(), "Review archive already exists")
    temporary = archive.with_suffix(".zip.tmp")
    require(not temporary.exists(), "Temporary review archive already exists")
    expected = {}
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        for name in names:
            path = output / name
            require(path.is_file() and not path.is_symlink(), "Missing or linked review JSON: " + name)
            data = path.read_bytes()
            expected[name] = hashlib.sha256(data).hexdigest()
            zipped.writestr(name, data)
    with zipfile.ZipFile(temporary) as zipped:
        require(zipped.namelist() == names and zipped.testzip() is None, "Review archive names/CRC verification failed")
        for name in names:
            require(hashlib.sha256(zipped.read(name)).hexdigest() == expected[name], "Review archive content hash mismatch: " + name)
    temporary.replace(archive)
    archive_hash = digest(archive)
    (output / "results_for_review.zip.sha256").write_text(archive_hash + "  results_for_review.zip\n", encoding="ascii")
    return {"path": str(archive), "sha256": archive_hash, "json_file_count": len(names),
            "names_crc_and_content_hashes_verified": True}


def run(args):
    started = time.perf_counter()
    require(not args.output.exists(), "Output directory already exists; refusing overwrite")
    args.output.mkdir(parents=True, exist_ok=False)
    save_json(args.output / "status.json", {"status": "validating", "started_utc": datetime.now(timezone.utc).isoformat()})
    try:
        print("VALIDATING_DATASET_AND_REVIEWED_LABELS", flush=True)
        rows, provenance = validate_inputs(args.dataset, args.selection, args.labels, args.protocol)
        save_json(args.output / "validated_inputs.json", provenance)
        import torch
        torch.set_num_threads(max(1, min(4, torch.get_num_threads())))
        torch.use_deterministic_algorithms(True)
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
        if args.device == "cuda":
            require(torch.cuda.is_available(), "CUDA requested but unavailable")
        device = "cuda" if args.device == "auto" and torch.cuda.is_available() else ("cpu" if args.device == "auto" else args.device)
        if device == "cuda":
            import os
            os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
        encoder = load_encoder(args.checkpoint, device)
        features, feature_info = extract_features(rows, encoder, device)
        atomic_torch_save(args.output / "shared_features.pt", {"features": features, "item_ids": [r["item_id"] for r in rows], **feature_info})
        save_json(args.output / "feature_extraction.json", feature_info)
        report = fit_comparison(features, rows, args.output, encoder.state_dict(), provenance, device)
        require(state_digest(encoder.state_dict()) == feature_info["encoder_sha256"], "Original encoder changed")
        report.update({"device": device, "torch_version": str(torch.__version__), "total_seconds": time.perf_counter() - started,
                       "script_sha256": digest(__file__), "feature_extraction": feature_info})
        save_json(args.output / "results.json", report)
        artifact_hashes = {str(path.relative_to(args.output)): digest(path) for path in sorted(args.output.rglob("*"))
                           if path.is_file() and path.name != "status.json"}
        save_json(args.output / "artifact_manifest.json", {"schema": "robomemo-tenframe-run-artifacts-v1", "files": artifact_hashes,
                                                          "input_hashes": provenance, "synthetic_test": False})
        save_json(args.output / "status.json", {"status": "complete", "six_final_runs_reported": True,
                                               "results_sha256": digest(args.output / "results.json"),
                                               "artifact_manifest_sha256": digest(args.output / "artifact_manifest.json")})
        review_archive = make_review_archive(args.output)
        print("PAIRED_COMPARISON_COMPLETE: " + str(args.output / "results.json"), flush=True)
        print("RETURN_RESULTS: " + review_archive["path"], flush=True)
        print("RETURN_RESULTS_SHA256: " + review_archive["sha256"], flush=True)
        return report
    except Exception as error:
        save_json(args.output / "status.json", {"status": "stopped", "error_type": type(error).__name__, "error": str(error),
                                               "no_split_move_or_refill": True})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=PACKAGE / "dataset")
    parser.add_argument("--selection", type=Path, default=PACKAGE / "selection" / "selection_frozen.json")
    parser.add_argument("--labels", type=Path, default=PACKAGE / "dataset" / "reviewed_labels.json")
    parser.add_argument("--checkpoint", type=Path, default=PACKAGE / "checkpoint" / "pick_place_reviewed_best.pt")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, default=PACKAGE / "protocol" / "experiment.json")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    args = parser.parse_args()
    try:
        run(args)
    except (ValueError, OSError, KeyError, TypeError, RuntimeError) as error:
        parser.exit(1, f"COMPARISON_STOPPED: {type(error).__name__}: {error}\n")


if __name__ == "__main__":
    main()
