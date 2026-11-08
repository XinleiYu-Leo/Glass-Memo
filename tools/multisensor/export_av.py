#!/usr/bin/env python3
"""Create a presentation MP4 from recorded MOV + WAV + timestamp sidecars.

This maps SDK audio PTS to the video's SDK-relative timeline. It does NOT
calibrate physical capture/codec latency. It includes all recorded speech.
Requires av and numpy. Originals are never edited.
"""
import argparse
import csv
import json
import os
import wave
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

from audit_session import audit, timestamp


def export(session, destination):
    session, destination = Path(session), Path(destination)
    if destination.exists() or destination.is_symlink() or destination.resolve().is_relative_to(session.resolve()):
        raise ValueError("Use a new output file outside the original session folder")
    if destination.suffix.lower() != ".mp4":
        raise ValueError("Output must end in .mp4")
    report_path = destination.with_suffix(".alignment.json")
    temporary = destination.with_suffix(".partial.mp4")
    if any(p.exists() or p.is_symlink() for p in (report_path, temporary)):
        raise ValueError("Export sidecar or partial file already exists; choose another output name")
    # Bound the in-memory resampling before reading the full PCM or decoding video.
    with wave.open(str(session / "audio.wav"), "rb") as w:
        rate, count = w.getframerate(), w.getnframes()
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("This export supports mono PCM16 only")
        if not 8000 <= rate <= 48000 or not 0 < count / rate <= 310:
            raise ValueError("Expected 8–48 kHz audio of no more than approximately five minutes")
    with av.open(str(session / "video.mov")) as probe:
        if not probe.streams.video:
            raise ValueError("No video stream")
        stream = probe.streams.video[0]
        if stream.codec_context.name != "h264" or stream.duration is None:
            raise ValueError("Expected H.264 with a known duration")
        if not 0 < float(stream.duration * stream.time_base) <= 310:
            raise ValueError("Expected a recording of no more than approximately five minutes")
    quality = audit(session, decode_video=True)
    if not quality["all_structural_checks_passed"]:
        raise ValueError("Session failed structural checks; audit it before export")
    if quality["audio"]["possible_source_gap_count"]:
        raise ValueError("Audio has possible source gaps: affine export would hide gaps; inspect first")
    if quality["audio"]["channels"] != 1 or quality["audio"]["sample_width_bytes"] != 2:
        raise ValueError("This export supports mono PCM16 only")
    def read(name):
        with (session / name).open(encoding="utf-8-sig", newline="") as f:
            return [r for r in csv.DictReader(f) if r["result"] == "written"]
    video, audio = read("frames.csv"), read("audio_chunks.csv")
    audio = [r for r in audio if int(r["inputFrames"]) > 0]
    if len(audio) < 3:
        raise ValueError("At least three written audio chunks are required")
    origin, audio_origin = timestamp(video[0], "sourcePTS"), timestamp(audio[0], "sourcePTS")
    if video[0].get("sourcePTSEpoch", "0") != audio[0].get("sourcePTSEpoch", "0"):
        raise ValueError("Video/audio PTS epochs differ")
    with wave.open(str(session / "audio.wav"), "rb") as w:
        rate, count = w.getframerate(), w.getnframes()
        pcm = np.frombuffer(w.readframes(count), dtype="<i2").astype(float) / 32768
    wavtime = np.array([int(r["wavSampleOffset"]) / rate for r in audio])
    sdk = np.array([float(timestamp(r, "sourcePTS") - audio_origin) for r in audio])
    slope, intercept = np.polyfit(wavtime, sdk, 1)
    keep = np.ones(len(audio), dtype=bool)
    for _ in range(3):
        residual = sdk - (intercept + slope * wavtime)
        median = np.median(residual)
        mad = np.median(np.abs(residual - median))
        keep = np.abs(residual - median) < max(.006, 4 * 1.4826 * mad)
        if keep.sum() < 3:
            raise ValueError("Insufficient consistent audio timestamp blocks")
        slope, intercept = np.polyfit(wavtime[keep], sdk[keep], 1)
    if not .98 <= slope <= 1.02:
        raise ValueError("Unexpected audio clock slope; inspect clock mapping manually")
    residual = sdk - (intercept + slope * wavtime)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(session / "video.mov")) as src:
        vs = src.streams.video[0]
        if vs.codec_context.name != "h264" or vs.duration is None:
            raise ValueError("Expected H.264 with a known duration")
        duration = float(vs.duration * vs.time_base)
        if not 0 < duration <= 310:
            raise ValueError("Expected a recording of no more than approximately five minutes")
        total = round(duration * rate)
        coords = ((float(origin - audio_origin) + np.arange(total) / rate - intercept) / slope) * rate
        if not np.any((coords >= 0) & (coords <= count - 1)):
            raise ValueError("Audio and video source timelines do not overlap")
        mapped = np.interp(coords, np.arange(count), pcm, left=0, right=0).astype(np.float32)
        report = {"method": "robust affine SDK audio clock to video SDK-relative timeline",
                  "capture_latency_calibrated": False, "includes_recorded_speech": True,
                  "video_reencoded": False, "audio_encoded": "AAC mono",
                  "duration_seconds": duration, "sample_rate": rate,
                  "clock_slope": float(slope), "clock_drift_ppm": float((slope-1)*1e6),
                  "fit_max_absolute_residual_seconds": float(np.max(np.abs(residual))),
                  "fit_inlier_chunks": int(keep.sum()),
                  "padded_seconds": float(np.sum((coords < 0) | (coords > count-1)) / rate)}
        # Exclusive creation refuses existing files, including dangling symlinks.
        with temporary.open("xb"):
            pass
        report_created = False
        try:
            with av.open(str(temporary), "w", format="mp4", options={"movflags": "+faststart", "use_editlist": "1"}) as dst:
                vo = dst.add_stream_from_template(vs)
                vo.metadata.clear()
                ao = dst.add_stream("aac", rate=rate)
                ao.layout, ao.bit_rate, ao.time_base = "mono", 64000, Fraction(1, rate)
                packets = []
                for start in range(0, total, 1024):
                    frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(mapped[start:start+1024][None,:]), format="fltp", layout="mono")
                    frame.sample_rate, frame.time_base, frame.pts = rate, Fraction(1, rate), start
                    for packet in ao.encode(frame):
                        packets.append((Fraction(packet.dts) * packet.time_base, 1, packet))
                for packet in ao.encode(None):
                    packets.append((Fraction(packet.dts) * packet.time_base, 1, packet))
                for packet in src.demux(vs):
                    if packet.dts is not None:
                        moment = Fraction(packet.dts) * packet.time_base
                        packet.stream = vo
                        packets.append((moment, 0, packet))
                for _, _, packet in sorted(packets, key=lambda p: (p[0],p[1])):
                    dst.mux(packet)
            with report_path.open("x", encoding="utf-8") as f:
                report_created = True
                f.write(json.dumps(report, indent=2, allow_nan=False) + "\n")
            # Publish without overwriting a destination created during encoding.
            os.link(temporary, destination)
        except BaseException:
            if report_created:
                report_path.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("session", type=Path)
    p.add_argument("output", type=Path)
    a = p.parse_args()
    print(json.dumps(export(a.session, a.output), indent=2))
