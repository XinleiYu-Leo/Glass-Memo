"""Immutable source identity and preprocessing contract, copied from input8-v1.
The old CONDITIONS describe only the frozen source request, never the new output.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile
import time
import zipfile

REQUEST_SHA256 = "e22d0624a7c91547856c3a86d3b012e27e50502e02bb70b11f319786aeaba47c"
SOURCE_MANIFEST_SHA256 = "d9b4511a4484715052295cc46d9cbb720b6822525945b66fcd9a80f2842bb9cd"
POLICY = "pick-place-presence-causal-input-diagnostic-v1"
SCOPE = "selected_training_source_input_diagnostic_not_independent_evaluation"
CONDITIONS = ["short4_224", "history13_224", "short4_detail"]
DEV_PARENTS = frozenset({
    "edc25190-7d42-4bf9-b69b-f821da0a9a8e", "5c3be03c-07a5-4427-a2fe-449f16e8017b",
    "70f2a750-f403-41b8-aabb-480eb3ab4ed4", "f6fea609-23c4-431c-8ac2-484f334a3a83",
    "ed90c2e8-c608-423f-a565-7b4cfffbe438", "27b0cdc1-b17c-47c2-bc19-9d81f69bfeb1",
})
HEX = re.compile(r"[0-9a-f]{64}\Z")
UID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
TOLERANCE = 1e-6
VIDEO_TIME_BASE = Fraction(1, 90000)


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def save_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def validate_request(request, *, synthetic=False, request_bytes=None):
    if not synthetic and (request_bytes is None or hashlib.sha256(request_bytes).hexdigest() != REQUEST_SHA256):
        raise ValueError("Request differs from the frozen eight-case request. Re-extract the supplied package.")
    if request.get("schema") != "robomemo-input-pilot-request-v1" or request.get("policy_version") != POLICY:
        raise ValueError("Unknown request schema or policy")
    if request.get("source_manifest_sha256") != SOURCE_MANIFEST_SHA256:
        raise ValueError("Unknown frozen source manifest")
    if request.get("scope") != SCOPE or request.get("not_training_labels") is not True:
        raise ValueError("Input diagnostic must not be treated as training labels")
    if request.get("history_seconds") != 6 or request.get("sampling_fps") != 2 or request.get("conditions") != CONDITIONS:
        raise ValueError("Expected six seconds, 2 fps, and the three fixed input conditions")
    if not re.fullmatch(r"input8-[0-9a-f]{16}", str(request.get("request_id", ""))):
        raise ValueError("Invalid request identity")
    items = request.get("items", [])
    if not items or (not synthetic and len(items) != 8):
        raise ValueError("Expected eight request items")
    for key in ("item_id", "clip_uid", "video_uid", "sample_id"):
        if len({row.get(key) for row in items}) != len(items):
            raise ValueError("Duplicate request " + key)
    for row in items:
        if not re.fullmatch(r"V[0-9]{2}", str(row.get("item_id", ""))):
            raise ValueError("Invalid item ID")
        if not all(UID.fullmatch(str(row.get(key, ""))) for key in ("clip_uid", "video_uid")):
            raise ValueError("Invalid source clip or parent ID")
        if row.get("internal_split") != "train" or row["video_uid"] in DEV_PARENTS:
            raise ValueError("Development/test source cannot enter this diagnostic")
        if not HEX.fullmatch(str(row.get("source_sha256", ""))):
            raise ValueError("Invalid source hash")
        names = ("history_start_sec", "decision_time_sec", "target_start_sec", "target_end_sec")
        values = [float(row[key]) for key in names]
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Invalid request time")
        first, decision, target_start, target_end = values
        if first < 0 or abs(decision - first - 6) > TOLERANCE or abs(decision - target_start - 1.5) > TOLERANCE or abs(target_end - decision) > TOLERANCE:
            raise ValueError("History and fixed target interval disagree")
        last_tick = round(decision * 2)
        if abs(last_tick / 2 - decision) > TOLERANCE or row["sample_id"] != f"{row['clip_uid']}:{last_tick}":
            raise ValueError("Sample identity differs from decision time")
        four = row.get("original_four", [])
        if len(four) != 4:
            raise ValueError("Expected four original input frames")
        for ref, tick in zip(four, range(last_tick - 3, last_tick + 1)):
            actual = float(ref["time_sec"])
            if ref.get("grid_index") != tick or ref.get("frame_id") != "f" + str(tick):
                raise ValueError("Original frame grid mismatch")
            if not math.isfinite(actual) or actual > tick / 2 + TOLERANCE or tick / 2 - actual > 0.25:
                raise ValueError("Original input is non-causal or stale")
            if not HEX.fullmatch(str(ref.get("student_sha256", ""))):
                raise ValueError("Invalid original frame hash")


def letterbox(image):
    """Exact preprocessing used in the original pilot, including PNG encoding."""
    from PIL import Image
    scale = min(224 / image.width, 224 / image.height)
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    small = image.convert("RGB").resize(size, Image.Resampling.BILINEAR)
    canvas = Image.new("RGB", (224, 224), tuple(round(value * 255) for value in (0.485, 0.456, 0.406)))
    canvas.paste(small, ((224 - small.width) // 2, (224 - small.height) // 2))
    return canvas

