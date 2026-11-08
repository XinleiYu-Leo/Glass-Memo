"""Train the fixed small student only from explicitly reviewed v2 visual labels.

This does not call a teacher, relabel data, or use the 24 diagnostic examples as
training data. A reviewed training set AND a separate reviewed development set
are mandatory. All reported scores concern the curated development set.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import re
import time

POLICY = "pick-place-evidence-v2"
LABEL_SCHEMA = "robomemo-reviewed-labels-v2"
MANIFEST_SCHEMA = "robomemo-teacher-pilot-v2"
MEAN = [0.485, 0.456, 0.406]
STD = [0.229, 0.224, 0.225]
DEV_PARENTS = frozenset({
    "edc25190-7d42-4bf9-b69b-f821da0a9a8e",
    "5c3be03c-07a5-4427-a2fe-449f16e8017b",
    "70f2a750-f403-41b8-aabb-480eb3ab4ed4",
    "f6fea609-23c4-431c-8ac2-484f334a3a83",
    "ed90c2e8-c608-423f-a565-7b4cfffbe438",
    "27b0cdc1-b17c-47c2-bc19-9d81f69bfeb1",
})
LIMITATION = (
    "Curated development diagnostics used for checkpoint selection, not an "
    "independent final benchmark. Reviewed teacher labels remain fallible. "
    "This score is not continuous-video event recall or object-finding accuracy."
)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def save_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)


def number(value, name):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError("Expected finite number: " + name)
    return float(value)


def safe_file(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("Invalid relative image path")
    part = Path(relative)
    if part.is_absolute() or ".." in part.parts:
        raise ValueError("Image path must stay inside its manifest directory")
    target = (root / part).resolve()
    if not target.is_relative_to(root.resolve()) or not target.is_file():
        raise ValueError("Image is missing or escapes manifest directory: " + relative)
    return target


def load_reviewed(labels_path, manifest_path, split):
    """Fail closed before loading a GPU model or creating a training run."""
    labels_path, manifest_path = Path(labels_path), Path(manifest_path)
    labels = json.loads(labels_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if labels.get("schema") != LABEL_SCHEMA or labels.get("policy_version") != POLICY:
        raise ValueError("Only v2 reviewed visual labels are supported; weak labels are refused")
    if (labels.get("review_status") != "accepted" or not isinstance(labels.get("reviewer"), str)
            or not labels["reviewer"].strip()):
        raise ValueError("Labels require explicit accepted review and a named reviewer")
    if split not in ("train", "dev") or labels.get("split") != split:
        raise ValueError("Reviewed label split does not match requested split")
    if labels.get("manifest_sha256") != digest(manifest_path):
        raise ValueError("Manifest content hash does not match reviewed labels")
    if manifest.get("schema") != MANIFEST_SCHEMA or manifest.get("policy_version") != POLICY:
        raise ValueError("Wrong source manifest schema or policy")
    rows = labels.get("samples")
    if not isinstance(rows, list) or not rows:
        raise ValueError("No reviewed samples")
    if labels.get("samples_sha256") != canonical_digest(rows):
        raise ValueError("Reviewed samples content hash mismatch")
    split_data = manifest.get("splits", {})
    trains = set(split_data.get("train_parent_ids", []))
    devs = set(split_data.get("dev_parent_ids", []))
    if not trains or not devs or trains & devs or trains & DEV_PARENTS:
        raise ValueError("Source train/dev parent leakage or missing split lists")
    allowed = trains if split == "train" else devs
    windows, checked_images, clip_parents = {}, {}, {}
    for segment in manifest.get("segments", []):
        clip, parent = segment["clip_uid"], segment["video_uid"]
        if clip in clip_parents and clip_parents[clip] != parent:
            raise ValueError("A clip is associated with two parent videos")
        clip_parents[clip] = parent
        if parent not in trains | devs:
            raise ValueError("Segment parent is not assigned to a split")
        frames = {frame["frame_id"]: frame for frame in segment["frames"]}
        if len(frames) != len(segment["frames"]):
            raise ValueError("Duplicate frame IDs inside segment")
        for window in segment["windows"]:
            sid = window["sample_id"]
            if sid in windows:
                raise ValueError("Duplicate sample ID in source manifest")
            windows[sid] = (segment, window, frames)
    accepted, excluded, ids = [], 0, set()
    for row in rows:
        sid = row.get("sample_id")
        if not isinstance(sid, str) or sid in ids or sid not in windows:
            raise ValueError("Unknown or duplicate reviewed sample ID")
        ids.add(sid)
        segment, window, frame_map = windows[sid]
        if row.get("clip_uid") != segment["clip_uid"] or row.get("video_uid") != segment["video_uid"]:
            raise ValueError("Reviewed sample does not match source clip/parent")
        if row["video_uid"] not in allowed or (split == "train" and row["video_uid"] in DEV_PARENTS):
            raise ValueError("Reviewed sample crosses the parent-video split")
        label = row.get("label")
        if label is not None and (type(label) is not int or label not in (0, 1)):
            raise ValueError("Label must be 0, 1, or null")
        if ((label == 1 and row.get("action") not in ("pickup", "place"))
                or (label == 0 and row.get("action") != "other")
                or (label is None and row.get("action") != "uncertain")):
            raise ValueError("Action must agree with the reviewed label")
        if not isinstance(row.get("review_note"), str) or not row["review_note"].strip():
            raise ValueError("Every reviewed sample requires a review note")
        frame_ids = window.get("frame_ids", [])
        if len(frame_ids) != 4 or len(set(frame_ids)) != 4 or any(x not in frame_map for x in frame_ids):
            raise ValueError("Student windows must contain four distinct source frames")
        frames = [frame_map[fid] for fid in frame_ids]
        paths = [f["student_image"] for f in frames]
        actual = [number(f["time_sec"], "frame timestamp") for f in frames]
        nominal = [number(f["nominal_time_sec"], "nominal frame timestamp") for f in frames]
        decision = number(window["decision_time_sec"], "decision timestamp")
        if any(abs(n - (decision - 1.5 + i * .5)) > 1e-6 for i, n in enumerate(nominal)):
            raise ValueError("Student inputs must be a causal four-frame 2 fps window")
        if any(t > n + 1e-6 or n - t > .250001 for t, n in zip(actual, nominal)):
            raise ValueError("Future or excessively stale frame in causal window")
        if any(a >= b for a, b in zip(actual, actual[1:])):
            raise ValueError("Actual input frame timestamps must strictly increase")
        row_times = row.get("actual_frame_times_sec", [])
        if len(row_times) != 4 or any(abs(number(a, "reviewed time") - b) > 1e-6 for a, b in zip(row_times, actual)):
            raise ValueError("Reviewed timestamps disagree with actual source frames")
        if abs(number(row.get("decision_time_sec"), "reviewed decision time") - decision) > 1e-6 or row.get("student_images") != paths:
            raise ValueError("Reviewed student input paths or decision time disagree with manifest")
        evidence = row.get("evidence_frame_ids")
        if not isinstance(evidence, list) or len(set(evidence)) != len(evidence) or any(fid not in frame_ids for fid in evidence):
            raise ValueError("Evidence must refer to distinct frames inside the student window")
        evidence_order = [frame_ids.index(fid) for fid in evidence]
        if evidence_order != sorted(evidence_order):
            raise ValueError("Evidence must be chronological")
        if label is not None and len(evidence) < (2 if label == 1 else 1):
            raise ValueError("Reviewed label lacks sufficient in-window visual evidence")
        resolved, hashes = [], []
        for frame, path in zip(frames, paths):
            expected_hash = frame.get("student_sha256")
            if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
                raise ValueError("Source manifest requires a student image SHA256")
            resolved_path = safe_file(manifest_path.parent, path)
            key = str(resolved_path)
            if key not in checked_images:
                checked_images[key] = digest(resolved_path)
            if checked_images[key] != expected_hash:
                raise ValueError("Student image content hash mismatch: " + path)
            resolved.append(key)
            hashes.append(expected_hash)
        if label is None:
            excluded += 1
        else:
            accepted.append({**row, "resolved_images": resolved, "image_sha256": hashes})
    if {row["label"] for row in accepted} != {0, 1}:
        raise ValueError("Both reviewed classes are required in the " + split + " set; nulls are excluded")
    return accepted, {"labels_sha256": digest(labels_path), "manifest_sha256": digest(manifest_path),
                      "reviewer": labels["reviewer"], "null_samples_excluded": excluded,
                      "reviewed_samples": len(accepted), "declared_train_parents": sorted(trains),
                      "declared_dev_parents": sorted(devs),
                      "synthetic_fixture_only": bool(manifest.get("synthetic_fixture_only") or manifest.get("synthetic_test")),
                      "synthetic_test": bool(manifest.get("synthetic_fixture_only") or manifest.get("synthetic_test"))}


def validate_pair(train_rows, dev_rows, train_info, dev_info):
    if any(train_info[key] != dev_info[key] for key in ("declared_train_parents", "declared_dev_parents")):
        raise ValueError("Training and development manifests disagree on the frozen parent split")
    train_parents = {r["video_uid"] for r in train_rows}
    dev_parents = {r["video_uid"] for r in dev_rows}
    if train_parents & (dev_parents | DEV_PARENTS | set(dev_info["declared_dev_parents"])):
        raise ValueError("Parent-video leakage between training and development")
    if dev_parents & set(train_info["declared_train_parents"]):
        raise ValueError("Development parent was declared as training")
    if {r["sample_id"] for r in train_rows} & {r["sample_id"] for r in dev_rows}:
        raise ValueError("Duplicate sample across train/dev")
    train_hashes = {h for r in train_rows for h in r["image_sha256"]}
    dev_hashes = {h for r in dev_rows for h in r["image_sha256"]}
    if train_hashes & dev_hashes:
        raise ValueError("Identical student image bytes cross train/dev; review before training")


def letterbox(image):
    from PIL import Image
    scale = min(224 / image.width, 224 / image.height)
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    small = image.convert("RGB").resize(size, Image.Resampling.BILINEAR)
    canvas = Image.new("RGB", (224, 224), tuple(round(x * 255) for x in MEAN))
    canvas.paste(small, ((224 - small.width) // 2, (224 - small.height) // 2))
    return canvas


def make_head(dim):
    from torch import nn
    return nn.Sequential(nn.Linear(dim * 4, 128), nn.ReLU(), nn.Dropout(.2), nn.Linear(128, 1))


def state_digest(state):
    h = hashlib.sha256()
    for key, value in sorted(state.items()):
        value = value.detach().cpu().contiguous()
        h.update(key.encode())
        h.update(str((str(value.dtype), list(value.shape))).encode())
        h.update(value.numpy().tobytes())
    return h.hexdigest()


def load_encoder(device, checkpoint):
    import torch
    from torch import nn
    from torchvision.models import mobilenet_v3_small
    model = mobilenet_v3_small(weights=None)
    encoder = nn.Sequential(model.features, model.avgpool, nn.Flatten(1)).eval().to(device)
    state = torch.load(checkpoint, map_location="cpu", weights_only=True)
    encoder.load_state_dict(state["encoder_state"], strict=True)
    for param in encoder.parameters():
        param.requires_grad_(False)
    return encoder


def extract_features(rows, encoder, device, cache_dir):
    import numpy as np
    from PIL import Image
    import torch
    cache_dir.mkdir(parents=True, exist_ok=True)
    encoder_hash = state_digest(encoder.state_dict())
    recipe = {"encoder_sha256": encoder_hash, "policy": POLICY, "size": 224,
              "mean": MEAN, "std": STD, "resize": "letterbox-bilinear"}
    found = {}
    unique = {h: path for row in rows for h, path in zip(row["image_sha256"], row["resolved_images"])}
    missing = []
    for image_hash, path in unique.items():
        key = canonical_digest({**recipe, "image_sha256": image_hash})
        cache_path = cache_dir / (key + ".pt")
        if cache_path.is_file():
            cached = torch.load(cache_path, map_location="cpu", weights_only=True)
            feature = cached.get("feature")
            if (cached.get("key") == key and isinstance(feature, torch.Tensor) and feature.ndim == 1
                    and torch.isfinite(feature).all() and cached.get("feature_sha256") == state_digest({"feature": feature})):
                found[image_hash] = feature
                continue
        missing.append((image_hash, path, key, cache_path))
    print("FEATURE_IMAGES", len(unique), "CACHED", len(found), "NEW", len(missing), flush=True)
    for start in range(0, len(missing), 64):
        batch = missing[start:start + 64]
        tensors = []
        for image_hash, path, _, _ in batch:
            if digest(path) != image_hash:
                raise ValueError("Student image changed after validation")
            with Image.open(path) as image:
                array = np.array(letterbox(image), dtype=np.float32) / 255.0
            tensor = torch.from_numpy(array).permute(2, 0, 1)
            tensor = (tensor - torch.tensor(MEAN)[:, None, None]) / torch.tensor(STD)[:, None, None]
            tensors.append(tensor)
        with torch.inference_mode():
            values = encoder(torch.stack(tensors).to(device)).cpu()
        if values.ndim != 2 or not torch.isfinite(values).all():
            raise ValueError("Invalid frozen encoder features")
        for (image_hash, _, key, cache_path), feature in zip(batch, values):
            found[image_hash] = feature.clone()
            temp = cache_path.with_suffix(".tmp")
            torch.save({"key": key, "feature": feature.clone(), "feature_sha256": state_digest({"feature": feature})}, temp)
            temp.replace(cache_path)
    matrix = torch.stack([torch.stack([found[h] for h in row["image_sha256"]]) for row in rows])
    return matrix, encoder_hash


def metrics(logits, target):
    import torch
    probability = logits.sigmoid()
    positive = probability >= .5
    truth = target == 1
    tp = int((positive & truth).sum())
    fp = int((positive & ~truth).sum())
    fn = int((~positive & truth).sum())
    tn = int((~positive & ~truth).sum())
    return {"loss": float(torch.nn.functional.binary_cross_entropy_with_logits(logits, target)),
            "balanced_accuracy": .5 * (tp / (tp + fn) + tn / (tn + fp)),
            "f1": 2 * tp / max(1, 2 * tp + fp + fn), "precision": tp / max(1, tp + fp),
            "recall": tp / (tp + fn), "tp": tp, "fp": fp, "fn": fn, "tn": tn}


def is_better(candidate, incumbent):
    """Preregistered ordering; exact ties retain the earlier epoch."""
    if incumbent is None:
        return True
    return (candidate["balanced_accuracy"], -candidate["loss"]) > (incumbent["balanced_accuracy"], -incumbent["loss"])


def atomic_torch_save(value, path):
    import torch
    temporary = path.with_suffix(".tmp")
    torch.save(value, temporary)
    temporary.replace(path)


class BestCheckpoint:
    """Save on improvement immediately; never alias mutable live tensors."""
    def __init__(self, path):
        self.path = Path(path)
        self.metric = None
        self.epoch = None

    def consider(self, metric, epoch, checkpoint):
        if not all(math.isfinite(float(metric[key])) for key in ("balanced_accuracy", "loss")):
            raise ValueError("Non-finite checkpoint selection metric")
        if not is_better(metric, self.metric):
            return False
        atomic_torch_save(checkpoint, self.path)
        self.metric, self.epoch = dict(metric), epoch
        return True


def fit_head(train_features, train_labels, dev_features, dev_labels, output, epochs, device, metadata):
    import torch
    from torch import nn
    if type(epochs) is not int or not 1 <= epochs <= 100:
        raise ValueError("epochs must be 1..100")
    output.mkdir(parents=True, exist_ok=True)
    for x, y in ((train_features, train_labels), (dev_features, dev_labels)):
        if x.ndim != 3 or x.shape[1] != 4 or len(x) != len(y) or set(y.tolist()) != {0.0, 1.0} or not torch.isfinite(x).all():
            raise ValueError("Expected finite four-frame features and both reviewed classes")
    if train_features.shape[2] != dev_features.shape[2]:
        raise ValueError("Train/dev encoder dimensions disagree")
    mean = train_features.mean(dim=(0, 1))
    std = train_features.std(dim=(0, 1), unbiased=False).clamp_min(.05)
    tx = ((train_features - mean) / std).flatten(1).to(device)
    dx = ((dev_features - mean) / std).flatten(1).to(device)
    ty, dy = train_labels.to(device), dev_labels.to(device)
    head = make_head(mean.numel()).to(device)
    optimizer = torch.optim.AdamW(head.parameters(), lr=1e-3, weight_decay=1e-4)
    loss_fn = nn.BCEWithLogitsLoss(pos_weight=(1 - ty).sum() / ty.sum())
    best = BestCheckpoint(output / "pick_place_reviewed_best.pt")
    history = []

    def checkpoint(epoch, dev_metric):
        return {**metadata, "schema": "robomemo-student-v2", "policy_version": POLICY,
                "head_state": {key: value.detach().cpu().clone() for key, value in head.state_dict().items()},
                "feature_mean": mean.cpu(), "feature_std": std.cpu(), "feature_dimension": mean.numel(),
                "saved_epoch": epoch, "development_metric": dev_metric,
                "selection_metric": "balanced_accuracy_desc_then_unweighted_bce_asc_then_earliest_epoch",
                "frame_offsets_sec": [-1.5, -1., -.5, 0.], "sample_fps": 2,
                "threshold": .5, "input_size": 224, "resize": "letterbox",
                "pixel_mean": MEAN, "pixel_std": STD, "label_source": "explicitly_reviewed_visual_labels_v2",
                "limitations": LIMITATION}

    for epoch in range(1, epochs + 1):
        head.train()
        order = torch.randperm(len(ty), device=device)
        for start in range(0, len(ty), 64):
            indices = order[start:start + 64]
            optimizer.zero_grad(set_to_none=True)
            loss = loss_fn(head(tx[indices]).squeeze(-1), ty[indices])
            if not torch.isfinite(loss):
                raise ValueError("Training loss is non-finite")
            loss.backward()
            optimizer.step()
        head.eval()
        with torch.no_grad():
            train_metric = metrics(head(tx).squeeze(-1), ty)
            dev_metric = metrics(head(dx).squeeze(-1), dy)
        improved = best.consider(dev_metric, epoch, checkpoint(epoch, dev_metric))
        history.append({"epoch": epoch, "train": train_metric, "dev": dev_metric, "saved_best": improved})
        save_json(output / "history.json", history)
        print("EPOCH", str(epoch) + "/" + str(epochs), "dev_balanced_accuracy", round(dev_metric["balanced_accuracy"], 4),
              "BEST" if improved else "", flush=True)
    atomic_torch_save(checkpoint(epochs, dev_metric), output / "pick_place_reviewed_last.pt")
    saved = torch.load(best.path, map_location="cpu", weights_only=True)
    restored = make_head(saved["feature_dimension"]).eval()
    restored.load_state_dict(saved["head_state"])
    restored_dx = ((dev_features.cpu() - saved["feature_mean"]) / saved["feature_std"]).flatten(1)
    with torch.no_grad():
        restored_logits = restored(restored_dx).squeeze(-1)
        verified_metric = metrics(restored_logits, dev_labels.cpu())
    if saved["saved_epoch"] != best.epoch or any(abs(verified_metric[key] - best.metric[key]) > 1e-5 for key in ("balanced_accuracy", "loss")):
        raise ValueError("Best checkpoint reload verification failed")
    return {"best_epoch": best.epoch, "last_epoch": epochs, "best_dev_metrics": verified_metric,
            "checkpoint_reload_verified": True, "history": history,
            "dev_probabilities": restored_logits.sigmoid().tolist(),
            "best_model_sha256": digest(best.path)}


def run(args):
    import torch
    started = time.perf_counter()
    train_rows, train_info = load_reviewed(args.train_labels, args.manifest, "train")
    dev_rows, dev_info = load_reviewed(args.dev_labels, args.dev_manifest or args.manifest, "dev")
    validate_pair(train_rows, dev_rows, train_info, dev_info)
    if args.device == "cuda" and not torch.cuda.is_available():
        raise ValueError("CUDA requested but unavailable; use --device cpu only intentionally")
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    root = args.project / "reviewed_v2"
    root.mkdir(parents=True, exist_ok=True)
    output = root / "runs" / datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    output.mkdir(parents=True, exist_ok=False)
    save_json(output / "run_status.json", {"status": "running"})
    try:
        encoder = load_encoder(args.device, args.encoder_checkpoint)
        all_features, encoder_hash = extract_features(train_rows + dev_rows, encoder, args.device, root / "feature_cache")
        metadata = {"encoder_name": "mobilenet_v3_small", "encoder_pretraining": "IMAGENET1K_V1",
                    "encoder_state": {k: v.detach().cpu().clone() for k, v in encoder.state_dict().items()},
                    "encoder_sha256": encoder_hash, "train_labels_sha256": train_info["labels_sha256"],
                    "dev_labels_sha256": dev_info["labels_sha256"], "train_manifest_sha256": train_info["manifest_sha256"],
                    "dev_manifest_sha256": dev_info["manifest_sha256"], "seed": args.seed}
        features_seconds = time.perf_counter() - started
        fit = fit_head(all_features[:len(train_rows)], torch.tensor([r["label"] for r in train_rows], dtype=torch.float32),
                       all_features[len(train_rows):], torch.tensor([r["label"] for r in dev_rows], dtype=torch.float32),
                       output, args.epochs, args.device, metadata)
        predictions = [{"sample_id": r["sample_id"], "clip_uid": r["clip_uid"], "video_uid": r["video_uid"],
                        "decision_time_sec": r["decision_time_sec"], "reviewed_label": r["label"], "probability": p}
                       for r, p in zip(dev_rows, fit.pop("dev_probabilities"))]
        save_json(output / "dev_predictions.json", predictions)
        synthetic = train_info["synthetic_fixture_only"] or dev_info["synthetic_fixture_only"]
        status = "synthetic_smoke_complete" if synthetic else "complete"
        result = {**fit, "status": status, "policy_version": POLICY, "train": train_info, "dev": dev_info,
                  "train_parent_count": len({r["video_uid"] for r in train_rows}),
                  "dev_parent_count": len({r["video_uid"] for r in dev_rows}),
                  "parent_isolation_verified": True, "participant_isolation_verified": False,
                  "original_six_dev_parents_excluded_from_training": True,
                  "synthetic_fixture_only": synthetic, "synthetic_test": synthetic,
                  "features_seconds": features_seconds, "total_seconds": time.perf_counter() - started,
                  "device": torch.cuda.get_device_name(0) if args.device == "cuda" else args.device,
                  "torch_version": str(torch.__version__), "script_sha256": digest(__file__),
                  "threshold": .5, "limitations": LIMITATION}
        save_json(output / "metrics.json", result)
        save_json(output / "run_status.json", {"status": status, "synthetic_fixture_only": synthetic})
        save_json(root / "latest_run.json", {"status": status, "synthetic_fixture_only": synthetic, "run_directory": str(output)})
        print("SYNTHETIC_SMOKE_COMPLETE" if synthetic else "REVIEWED_TRAINING_COMPLETE", flush=True)
        print("BEST_MODEL:", output / "pick_place_reviewed_best.pt", flush=True)
        print("METRICS:", output / "metrics.json", flush=True)
        return result
    except Exception as error:
        save_json(output / "run_status.json", {"status": "failed", "error": str(error), "type": type(error).__name__})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--encoder-checkpoint", type=Path, default=Path(__file__).parent / "models/historical_reviewed_v2.pt")
    parser.add_argument("--train-labels", type=Path, required=True)
    parser.add_argument("--dev-labels", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--dev-manifest", type=Path)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--seed", type=int, default=566)
    args = parser.parse_args()
    if not 1 <= args.epochs <= 100:
        parser.error("epochs must be 1..100")
    root = args.project / "reviewed_v2"
    root.mkdir(parents=True, exist_ok=True)
    import fcntl  # Windows workflow runs this inside WSL.
    with (root / "training.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another reviewed-v2 training job is already running")
        run(args)


if __name__ == "__main__":
    main()
