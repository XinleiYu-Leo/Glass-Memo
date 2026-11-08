"""Decode 10 causal frames per selected window with ffmpeg (no future frames).

For each window: targets t-1.5 ... t (10 evenly spaced, ~6 fps). For every target the
newest decoded frame with PTS <= target is used. Real PTS are stored. Windows whose
targets cannot be served by a frame at most 0.10 s old are reported, never silently kept.
Output: <cache>/<window_id>.npz (uint8 frames [10,224,224,3], pts, targets) + manifest.
"""
import argparse, json, re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C

EXTRACTOR_VERSION = "v2-fps_mode-passthrough"  # v1 let ffmpeg duplicate frames at timestamp gaps
PTS_RE = re.compile(r"pts_time:\s*([0-9.eE+-]+)")


def decode(clip, start, dur):
    vf = (f"scale={C.SIZE}:{C.SIZE}:force_original_aspect_ratio=decrease:flags=bilinear,"
          f"pad={C.SIZE}:{C.SIZE}:(ow-iw)/2:(oh-ih)/2:color=0x{C.PAD_RGB[0]:02x}{C.PAD_RGB[1]:02x}{C.PAD_RGB[2]:02x},showinfo")
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "info", "-ss", f"{start:.6f}", "-t", f"{dur:.6f}", "-i", str(clip),
           "-an", "-vf", vf, "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + p.stderr.decode("utf-8", "replace")[-400:])
    rel = [float(m.group(1)) for line in p.stderr.decode("utf-8", "replace").splitlines()
           if "showinfo" in line and "pts_time" in line for m in [PTS_RE.search(line)] if m]
    frame_bytes = C.SIZE * C.SIZE * 3
    if len(p.stdout) != len(rel) * frame_bytes:
        raise RuntimeError(f"frame/pts count mismatch: {len(p.stdout)} bytes vs {len(rel)} pts")
    frames = np.frombuffer(p.stdout, dtype=np.uint8).reshape(len(rel), C.SIZE, C.SIZE, 3)
    return frames, np.array([start + r for r in rel], dtype=np.float64)


def one(window, clip_dir, cache):
    wid, t = window["window_id"], float(window["decision_time_sec"])
    out = cache / f"{wid}.npz"
    if out.is_file():
        with np.load(out) as z:
            return {"window_id": wid, "status": "cached", "pts": z["pts"].tolist()}
    clip = clip_dir / f"{window['clip_uid']}.mp4"
    if not clip.is_file():
        return {"window_id": wid, "status": "missing_clip"}
    tg = C.targets(t)
    start = max(0.0, tg[0] - 0.5)
    try:
        frames, pts = decode(clip, start, t + 0.02 - start)
    except Exception as exc:  # report, never crash the whole batch
        return {"window_id": wid, "status": "decode_error", "error": str(exc)[:300]}
    if not len(pts):
        return {"window_id": wid, "status": "no_frames_decoded"}
    chosen, used = [], []
    for target in tg:
        idx = np.nonzero(pts <= target + 1e-6)[0]
        if not len(idx) or target - pts[idx[-1]] > C.STALE_MAX:
            return {"window_id": wid, "status": "target_not_covered", "target": target,
                    "last_pts": float(pts[-1])}
        chosen.append(int(idx[-1])); used.append(float(pts[idx[-1]]))
    if max(used) > t + 1e-6:
        return {"window_id": wid, "status": "future_frame_bug"}
    tmp = out.with_suffix(".tmp.npz")
    np.savez_compressed(tmp, frames=frames[chosen], pts=np.array(used), targets=np.array(tg))
    tmp.replace(out)
    return {"window_id": wid, "status": "extracted", "pts": used, "duplicates": len(chosen) - len(set(chosen))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selection", type=Path, required=True)
    ap.add_argument("--clips", type=Path, required=True)
    ap.add_argument("--cache", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    sel = C.load_selection(a.selection)
    windows = sel["train_1000"] + sel["dev"] + sel["eval"]
    a.cache.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic(); results = []
    with ThreadPoolExecutor(a.workers) as ex:
        for i, r in enumerate(ex.map(lambda w: one(w, a.clips, a.cache), windows), 1):
            results.append(r)
            if i % 100 == 0 or i == len(windows):
                print(f"EXTRACT {i}/{len(windows)} {time.monotonic() - t0:.0f}s", flush=True)
    status = {}
    for r in results:
        status[r["status"]] = status.get(r["status"], 0) + 1
    clips = sorted({w["clip_uid"] for w in windows})
    print("Hashing source clips ...", flush=True)
    clip_sha = {c: C.sha256_file(a.clips / f"{c}.mp4") for c in clips if (a.clips / f"{c}.mp4").is_file()}
    C.save_json(a.cache / "extraction_manifest.json", {"policy": C.POLICY, "extractor_version": EXTRACTOR_VERSION, "n_frames": C.N_FRAMES, "window_sec": C.WIN,
        "size": C.SIZE, "stale_max_sec": C.STALE_MAX, "status_counts": status, "seconds": round(time.monotonic() - t0, 1),
        "clip_sha256": clip_sha, "windows": results})
    print("EXTRACT_STATUS", json.dumps(status), flush=True)
    bad = {k: v for k, v in status.items() if k not in ("extracted", "cached")}
    return 0 if not bad else 3


if __name__ == "__main__":
    sys.exit(main())
