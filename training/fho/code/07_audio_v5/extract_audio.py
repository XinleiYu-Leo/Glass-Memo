"""Extract the causal audio of every selected window: [t-1.5, t], mono.

Two versions per window, both 16 kHz int16 so one feature code path serves both:
  full : the clip's own audio resampled to 16 kHz
  hfp8k: resampled to 8 kHz then back to 16 kHz (band-limited like the glasses' 8 kHz Bluetooth mic)
Clips without an audio stream or with a silent stream are reported, never silently mixed in.
"""
import argparse, json, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C

SR = 16000
N = int(C.WIN * SR)   # 24000 samples


def probe(clip):
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_name,sample_rate,channels",
                        "-of", "json", str(clip)], capture_output=True, text=True)
    try:
        streams = json.loads(p.stdout or "{}").get("streams", [])
    except json.JSONDecodeError:
        streams = []
    return streams[0] if streams else None


def grab(clip, start, filt):
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", f"{start:.6f}", "-t", f"{C.WIN:.6f}", "-i", str(clip),
           "-vn", "-ac", "1", "-af", filt, "-f", "s16le", "-acodec", "pcm_s16le", "-"]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace")[-300:])
    a = np.frombuffer(p.stdout, dtype=np.int16)
    if len(a) < N:                       # pad a short tail with zeros, record how much was missing
        return np.concatenate([a, np.zeros(N - len(a), np.int16)]), N - len(a)
    return a[:N], 0


def one(w, clips, probes):
    clip = clips / f"{w['clip_uid']}.mp4"
    if not clip.is_file():
        return w["window_id"], None, None, {"status": "missing_clip"}
    if probes.get(w["clip_uid"]) is None:
        return w["window_id"], None, None, {"status": "no_audio_stream"}
    start = float(w["decision_time_sec"]) - C.WIN
    if start < 0:
        return w["window_id"], None, None, {"status": "window_before_start"}
    try:
        full, miss = grab(clip, start, f"aresample={SR}")
        hfp, _ = grab(clip, start, f"aresample=8000,aresample={SR}")
    except Exception as exc:
        return w["window_id"], None, None, {"status": "decode_error", "error": str(exc)[:200]}
    if miss > SR * 0.1:
        return w["window_id"], None, None, {"status": "audio_too_short", "missing_samples": miss}
    if int(np.abs(full).max()) == 0:
        return w["window_id"], None, None, {"status": "silent"}
    return w["window_id"], full, hfp, {"status": "ok", "peak": int(np.abs(full).max())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selection", type=Path, required=True)
    ap.add_argument("--clips", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    sel = C.load_selection(a.selection)
    windows = sel["train_full"] + sel["dev"] + sel["eval"]
    t0 = time.monotonic()
    clips = sorted({w["clip_uid"] for w in windows})
    probes = {}
    for c in clips:
        f = a.clips / f"{c}.mp4"
        probes[c] = probe(f) if f.is_file() else None
    print(f"AUDIO_PROBE clips {len(clips)} with_audio {sum(v is not None for v in probes.values())}", flush=True)
    ids, full, hfp, report = [], [], [], {}
    with ThreadPoolExecutor(a.workers) as ex:
        for i, (wid, f, h, r) in enumerate(ex.map(lambda w: one(w, a.clips, probes), windows), 1):
            report[wid] = r
            if f is not None:
                ids.append(wid); full.append(f); hfp.append(h)
            if i % 200 == 0 or i == len(windows):
                print(f"AUDIO {i}/{len(windows)} {time.monotonic() - t0:.0f}s", flush=True)
    counts = {}
    for r in report.values():
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    a.output.parent.mkdir(parents=True, exist_ok=True)
    np.savez(a.output, window_ids=np.array(ids), full=np.stack(full) if full else np.zeros((0, N), np.int16),
             hfp8k=np.stack(hfp) if hfp else np.zeros((0, N), np.int16))
    C.save_json(a.output.with_suffix(".json"), {"sample_rate": SR, "window_sec": C.WIN, "status_counts": counts,
                "clips": len(clips), "clips_with_audio": sum(v is not None for v in probes.values()),
                "codec_examples": sorted({json.dumps(v, sort_keys=True) for v in probes.values() if v})[:5],
                "seconds": round(time.monotonic() - t0, 1), "windows": report})
    print("AUDIO_STATUS", json.dumps(counts), flush=True)


if __name__ == "__main__":
    main()
