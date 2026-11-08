"""Export only original-source causal PNGs from the frozen 64-case selection.

No API calls, upload, training, audit-video transcoding, or label creation.
Interrupted exports resume by checking existing exact bytes; never overwrite.
"""
from __future__ import annotations
import argparse
from bisect import bisect_right
from fractions import Fraction
import hashlib
import io
import json
import os
from pathlib import Path
import types

FROZEN_MODULE = Path(__file__).with_name("frozen_source.py")
FROZEN_MODULE_SHA = "a5a1b517094f324647ad76a5237300b478e3e1c7bace14e728a2c558d1f9dfc5"
TOL = 1e-6


def require(ok, message):
    if not ok:
        raise ValueError(message)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_once(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".pending")
    if path.exists():
        require(path.is_file() and not path.is_symlink() and path.read_bytes() == data,
                "Existing frozen output differs; refusing overwrite: " + str(path))
        if pending.exists():
            require(pending.is_file() and not pending.is_symlink(), "Unsafe interrupted temporary output")
            pending.unlink()
    else:
        if pending.exists():
            require(pending.is_file() and not pending.is_symlink(), "Unsafe interrupted temporary output")
            pending.unlink()
        try:
            with pending.open("xb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(pending, path)  # Atomic publication, never replace an existing freeze.
            except FileExistsError:
                require(path.read_bytes() == data, "Concurrent output differs; refusing overwrite")
        finally:
            if pending.exists():
                pending.unlink()



def save_json(path, value):
    write_once(path, (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode())


def load_preprocessing():
    require(digest(FROZEN_MODULE) == FROZEN_MODULE_SHA, "Reviewed preprocessing helper changed")
    module = types.ModuleType("reviewed_frozen_source")
    exec(compile(FROZEN_MODULE.read_bytes(), str(FROZEN_MODULE), "exec"), module.__dict__)
    return module.letterbox


def export_one(row, video, dataset, letterbox):
    import av
    from PIL import Image
    label = row["item_id"]
    tick = row["decision_tick_sixths"]
    nominal = [Fraction(n, 6) for n in range(tick - 9, tick + 1)]
    require(abs(float(nominal[0]) - row["target_start_sec"]) <= TOL
            and abs(float(nominal[-1]) - row["target_end_sec"]) <= TOL, "Window/grid mismatch")
    frames, timestamps, evidence = [], [], []
    previous, last_actual, origin, cursor = None, None, None, 0
    dimensions = None

    def emit(chosen, requested):
        nonlocal cursor
        require(chosen is not None, label + " lacks actual causal predecessor")
        source_frame, actual, index = chosen
        require(actual <= requested and actual <= nominal[-1], label + " future sample rejected")
        require(float(actual) >= float(nominal[0]) - TOL,
                label + " predecessor before target start; unsupported boundary, no clamp/reselection")
        frame_id = f"f{cursor + 1:02d}"
        relative = f"student_frames/{label}_{frame_id}.png"
        image = letterbox(source_frame.to_image())
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        data = buffer.getvalue()
        write_once(dataset / relative, data)
        with Image.open(io.BytesIO(data)) as check:
            require(check.format == "PNG" and check.mode == "RGB" and check.size == (224, 224), "Invalid PNG")
            check.load()
        frames.append({"frame_id": frame_id, "path": relative,
                       "sha256": hashlib.sha256(data).hexdigest(),
                       "requested_time_sec": float(requested), "actual_time_sec": float(actual),
                       "source_frame_index": index, "source_pts": source_frame.pts,
                       "source_time_base": str(source_frame.time_base),
                       "sampling_lag_sec": float(requested - actual),
                       "repeated_source_frame": index in {f["source_frame_index"] for f in frames}})
        cursor += 1

    with av.open(str(video)) as source:
        require(len(source.streams.video) == 1, label + " requires one original video stream")
        stream = source.streams.video[0]
        stream.thread_type = "AUTO"
        stream.codec_context.thread_count = 4
        origin = Fraction(stream.start_time) * stream.time_base if stream.start_time is not None else None
        for index, frame in enumerate(source.decode(video=0)):
            require(frame.pts is not None and frame.time_base is not None, label + " missing source PTS")
            absolute = Fraction(frame.pts) * frame.time_base
            if origin is None:
                origin = absolute
            actual = absolute - origin
            require(last_actual is None or actual > last_actual, label + " nonincreasing source PTS")
            if dimensions is None:
                dimensions = [frame.width, frame.height]
            require(dimensions == [frame.width, frame.height], label + " changed source dimensions")
            timestamps.append(actual)
            evidence.append({"source_frame_index": index, "source_pts": frame.pts,
                             "source_time_base": str(frame.time_base), "actual_time_sec": float(actual)})
            current = (frame, actual, index)
            while cursor < 10 and nominal[cursor] <= actual:
                emit(current if nominal[cursor] == actual else previous, nominal[cursor])
            previous, last_actual = current, actual
            if actual > nominal[-1]:
                break
        while cursor < 10:
            emit(previous, nominal[cursor])
    require(len(frames) == 10, label + " missing samples")
    require(len({f["source_frame_index"] for f in frames}) == 10, label + " requires ten distinct source observations; no duplicates/reselection")
    require(timestamps[-1] >= nominal[-1], label + " source ended before target; no endpoint padding/reselection")
    for frame, requested in zip(frames, nominal):
        predecessor_index = bisect_right(timestamps, requested) - 1
        require(predecessor_index == frame["source_frame_index"], "Not nearest causal source predecessor")
        next_index = predecessor_index + 1
        frame["next_source_actual_time_sec"] = float(timestamps[next_index]) if next_index < len(timestamps) else None
    return {"frames": frames, "source_audit": {
        "source_sha256": digest(video), "source_bytes": video.stat().st_size,
        "source_clip_filename": video.name, "source_origin_sec": float(origin),
        "source_origin_fraction": str(origin), "source_dimensions": dimensions,
        "decoded_pts_until_first_after_target": evidence,
        "source_ended_before_target": bool(timestamps[-1] < nominal[-1]),
        "preprocessing_module_sha256": FROZEN_MODULE_SHA,
        "original_source_direct_decode": True, "interpolation": False,
    }}


def export(args):
    import av
    from PIL import __version__ as pillow_version
    from verify_sources import validate_selection
    selection = json.loads(args.selection.read_bytes())
    validate_selection(selection)
    require(selection["protocol_sha256"] == digest(args.protocol), "Protocol changed after source freeze")
    receipt = json.loads(args.receipt.read_bytes())
    require(receipt["selection_sha256"] == digest(args.selection), "Download receipt selection differs")
    source_map = {x["clip_uid"]: x for x in receipt["sources"]}
    require(set(source_map) == {x["clip_uid"] for x in selection["items"]}, "Download receipt source set differs")
    letterbox = load_preprocessing()
    args.output.mkdir(parents=True, exist_ok=True)
    items, anonymous = [], []
    for number, selected in enumerate(selection["items"], 1):
        source = args.clips_dir / (selected["clip_uid"] + ".mp4")
        require(source.is_file() and digest(source) == source_map[selected["clip_uid"]]["sha256"], "Source changed after download: " + selected["item_id"])
        print(f"EXPORT {number}/64 {selected['item_id']} original-source frames", flush=True)
        extracted = export_one(selected, source, args.output, letterbox)
        item = {key: selected[key] for key in ("item_id", "split", "video_uid", "clip_uid", "related_group_parent_ids", "target_start_sec", "target_end_sec")}
        item.update(extracted)
        items.append(item)
        anonymous.append({"item_id": item["item_id"], "target_start_sec": item["target_start_sec"],
                          "target_end_sec": item["target_end_sec"],
                          "frames": [{k: frame[k] for k in ("frame_id", "path", "sha256", "requested_time_sec", "actual_time_sec")} for frame in item["frames"]]})
    dataset = {"schema": "robomemo-tenframe-dataset-v1", "synthetic_test": False,
               "protocol_sha256": digest(args.protocol), "selection_sha256": digest(args.selection),
               "download_receipt_sha256": digest(args.receipt),
               "frame_count": 10, "sampling_fps": 6, "target_seconds": 1.5,
               "baseline_four_indices_zero_based": [0, 3, 6, 9],
               "preprocessing": {"pixels": [224, 224], "mode": "RGB", "resize": "bilinear_letterbox", "padding_rgb": [124, 116, 104], "format": "PNG", "frozen_module_sha256": FROZEN_MODULE_SHA},
               "extraction_versions": {"pyav": av.__version__, "pillow": pillow_version},
               "teacher_calls": 0, "training_runs": 0, "uploads": 0, "items": items}
    save_json(args.output / "manifest.json", dataset)
    save_json(args.output / "teacher_manifest.json", {
        "schema": "robomemo-tenframe-anonymous-inputs-v1", "synthetic_test": False,
        "frame_count": 10, "time_origin": "source clip start; actual and requested times use this origin",
        "items": anonymous})
    expected = {"manifest.json", "teacher_manifest.json"} | {f["path"] for i in items for f in i["frames"]}
    actual = {p.relative_to(args.output).as_posix() for p in args.output.rglob("*") if p.is_file()}
    require(actual == expected, "Dataset contains unlisted files")
    return dataset


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("selection", "protocol", "receipt", "clips-dir", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    args = p.parse_args()
    try:
        export(args)
    except Exception as exc:
        p.exit(1, f"EXPORT_STOPPED: {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
