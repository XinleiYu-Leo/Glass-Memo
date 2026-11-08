#!/usr/bin/env python3
"""Read-only, aggregate audit of a RoboMemo multisensor session (stdlib only).

--decode-video additionally needs PyAV. No media, source timestamps, session
identifiers, absolute paths, or free-form device/errors fields enter the report.
This does not calibrate sensor clocks or certify real-time operation.
"""
import argparse
import csv
import json
import math
import statistics
import wave
from collections import Counter
from fractions import Fraction
from pathlib import Path


def rows(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def timestamp(row, prefix):
    flags, scale = int(row[prefix + "Flags"]), int(row[prefix + "Timescale"])
    # CoreMedia valid + hasBeenRounded are finite. Infinity/indefinite are not.
    if not flags & 1 or flags & (4 | 8 | 16) or scale <= 0:
        raise ValueError("Expected a finite timestamp and positive timescale")
    return Fraction(int(row[prefix + "Value"]), scale)


def public_number(value, *, integer=False):
    """Do not copy a caller-controlled string/object into a shareable report."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0 or (integer and int(value) != value):
        return None
    return int(value) if integer else value


def stats(values):
    values = sorted(map(float, values))
    if not values:
        return None
    return {"min": values[0], "median": statistics.median(values),
            "max": values[-1], "mean": statistics.mean(values)}


def intervals(times):
    return [b - a for a, b in zip(times, times[1:])]


def same_epoch(items, prefix):
    return len({r.get(prefix + "Epoch", "0") for r in items}) <= 1


def result_counts(items):
    # These are recorder status codes, not arbitrary log/error messages.
    allowed = {"written", "error", "limit_reached", "source_limit_reached",
               "dropped_invalid_source_pts", "dropped_source_epoch_change",
               "dropped_nonmonotonic_source_pts", "dropped_source_pts_ahead_of_wall",
               "dropped_busy", "dropped_nonmonotonic", "dropped_pool",
               "ignored_empty", "not_written", "not_written_invalid_pcm"}
    return dict(Counter(r["result"] if r["result"] in allowed else "unknown_status" for r in items))


def audit(directory, decode_video=False):
    directory = Path(directory)
    required = ["video.mov", "frames.csv", "audio.wav", "audio_chunks.csv",
                "motion.csv", "device_state.csv", "session.json"]
    missing = [p for p in required if not (directory / p).is_file()]
    if missing:
        raise ValueError("Missing required files: " + ", ".join(missing))
    session = json.loads((directory / "session.json").read_text())
    config = session.get("configuration", {})
    frames = rows(directory / "frames.csv")
    video = [r for r in frames if r["result"] == "written"]
    chunks = rows(directory / "audio_chunks.csv")
    audio = [r for r in chunks if r["result"] == "written" and int(r["inputFrames"]) > 0]
    motion = rows(directory / "motion.csv")
    checks = {}
    resolution = config.get("requestedResolution")
    enabled = config.get("requestedAudioEnabled")
    report = {"schema_version": 1, "configuration": {
        "requestedResolution": resolution if resolution in ("high", "medium", "low") else None,
        "requestedVideoFPS": public_number(config.get("requestedVideoFPS")),
        "requestedAudioEnabled": enabled if isinstance(enabled, bool) else None,
        "requestedMotionRateHz": public_number(config.get("requestedMotionRateHz"), integer=True)},
        "checks": checks,
        "limits": ["Timestamp continuity is not content synchronization.",
                   "Host receipt gaps include upstream buffering and scheduling.",
                   "Source gap excess is not hardware packet loss.",
                   "No Motion-to-video clock mapping is assumed."]}
    source = [timestamp(r, "sourcePTS") for r in video]
    output = [timestamp(r, "outputPTS") for r in video]
    checks["video_has_written_frames"] = bool(video)
    checks["video_source_single_epoch"] = same_epoch(video, "sourcePTS")
    checks["video_output_single_epoch"] = same_epoch(video, "outputPTS")
    checks["video_source_strictly_increasing"] = bool(video) and all(x > 0 for x in intervals(source))
    errors = [abs(o - (s - source[0])) for o, s in zip(output, source)]
    checks["video_output_matches_relative_source"] = bool(video) and max(errors) <= Fraction(1, 1000000)
    report["video"] = {
        "csv_rows": len(frames), "written_frames": len(video),
        "result_counts": result_counts(frames),
        "native_size_counts": dict(Counter(f'{int(r["sourceWidth"])}x{int(r["sourceHeight"])}' for r in video)),
        "source_intervals_seconds": stats(intervals(source)),
        "host_intervals_seconds": stats(intervals([timestamp(r, "hostTime") for r in video])),
        "output_vs_source_max_error_seconds": float(max(errors)) if errors else None,
        "decoded": False}
    if decode_video:
        import av
        with av.open(str(directory / "video.mov")) as container:
            decoded = [Fraction(f.pts) * f.time_base for f in container.decode(video=0)]
        checks["movie_frame_count_matches_csv"] = len(decoded) == len(output)
        checks["movie_pts_matches_csv"] = (bool(output) and len(decoded) == len(output)
                                               and all(abs(a-b) <= Fraction(1,1000000) for a,b in zip(decoded,output)))
        report["video"].update(decoded=True, decoded_frames=len(decoded))

    with wave.open(str(directory / "audio.wav"), "rb") as w:
        rate, channels, width, count = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        payload = 0
        while block := w.readframes(65536):
            payload += len(block)
    checks["wav_payload_matches_header"] = payload == count * channels * width
    offset = 0
    offsets_ok = True
    for r in audio:
        offsets_ok &= int(r["wavSampleOffset"]) == offset
        offsets_ok &= int(r["wavByteOffset"]) == 44 + offset * channels * width
        offsets_ok &= float(r["sampleRate"]) == rate and int(r["channels"]) == channels
        offset += int(r["inputFrames"])
    checks["audio_chunks_cover_wav"] = bool(offsets_ok and offset == count)
    checks["audio_source_single_epoch"] = same_epoch(audio, "sourcePTS")
    residuals, jitter, gaps, nonincrease = [], 0, 0, 0
    for a, b in zip(audio, audio[1:]):
        if a.get("sourcePTSEpoch", "0") != b.get("sourcePTSEpoch", "0"):
            continue
        delta = timestamp(b, "sourcePTS") - timestamp(a, "sourcePTS")
        nonincrease += delta <= 0
        duration = Fraction(int(a["inputFrames"]), int(float(a["sampleRate"])))
        residual = delta - duration
        tolerance = max(Fraction(2,1000), Fraction(2, int(float(b["sampleRate"]))))
        residuals.append(residual)
        jitter += abs(residual) > tolerance
        gaps += residual > max(tolerance, duration / 2)
    checks["audio_source_strictly_increasing"] = nonincrease == 0
    if config.get("requestedAudioEnabled") is False:
        checks["disabled_audio_is_empty"] = count == 0 and not audio
    elif config.get("requestedAudioEnabled") is True:
        checks["requested_audio_has_samples"] = count > 0
    report["audio"] = {"sample_rate": rate, "channels": channels, "sample_width_bytes": width,
        "samples": count, "duration_seconds": count / rate, "written_chunks": len(audio),
        "result_counts": result_counts(chunks),
        "source_residual_seconds": stats(residuals), "timestamp_jitter_count": jitter,
        "possible_source_gap_count": gaps, "nonincreasing_source_pts_count": nonincrease,
        "host_intervals_seconds": stats(intervals([timestamp(r,"hostTime") for r in audio]))}

    # Do not combine distinct motion sources into one synthetic timeline.
    groups = {}
    for r in motion:
        groups.setdefault(r["source"], []).append(r)
    summaries = []
    for group in groups.values():
        arrival = [int(r["timestampNs"]) for r in group]
        ordered = sorted(group, key=lambda r: int(r["timestampNs"]))
        unique = {int(r["timestampNs"]): r for r in ordered}
        ordered = list(unique.values())
        gaps, excess = [], 0.0
        for a, b in zip(ordered, ordered[1:]):
            dt = (int(b["timestampNs"]) - int(a["timestampNs"])) / 1e9
            slower = min(int(a["requestedRateHz"]), int(b["requestedRateHz"]))
            if slower <= 0:
                raise ValueError("Motion requested rate must be positive")
            if dt > 2.5 / slower:
                gaps.append(dt)
                excess += dt - 1 / slower
        source_deltas = [x / 1e9 for x in intervals(sorted(unique))]
        span = (max(unique) - min(unique)) / 1e9 if len(unique) > 1 else 0
        summaries.append({"rows": len(group), "unique_source_samples": len(unique),
            "duplicate_source_timestamps": len(group)-len(unique),
            "out_of_order_arrivals": sum(x < 0 for x in intervals(arrival)),
            "requested_rates_hz": sorted({int(r["requestedRateHz"]) for r in group}),
            "source_intervals_seconds": stats(source_deltas),
            "host_intervals_seconds": stats(intervals([timestamp(r,"hostTime") for r in group])),
            "source_gaps_over_2_5_periods_seconds": gaps,
            "thresholded_gap_excess_fraction": excess / span if span else None})
    report["motion"] = {"rows": len(motion), "sources": summaries}
    report["device_state_rows"] = len(rows(directory / "device_state.csv"))
    queue = session.get("queue", {})
    dropped = queue.get("overflowDroppedByModule", {})
    report["app_queue"] = {
        "peakPendingDataJobs": public_number(queue.get("peakPendingDataJobs"), integer=True),
        "maximumResidenceSeconds": public_number(queue.get("maximumResidenceSeconds")),
        "overflowDroppedByModule": {k: public_number(dropped.get(k), integer=True)
                                     for k in ("audio", "audioDiagnostics", "motion", "deviceState")}
        if isinstance(dropped, dict) else None}
    report["all_structural_checks_passed"] = all(checks.values())
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("session", type=Path)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--decode-video", action="store_true")
    args = p.parse_args()
    if args.output.resolve().is_relative_to(args.session.resolve()):
        p.error("Write reports outside the original session folder")
    result = audit(args.session, args.decode_video)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"structural_checks_passed": result["all_structural_checks_passed"],
                      "video_frames": result["video"]["written_frames"], "motion_rows": result["motion"]["rows"]}))
    return 0 if result["all_structural_checks_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
