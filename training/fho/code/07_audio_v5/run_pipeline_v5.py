"""RoboMemo fho_transfer_v1 · audio round (v5): does sound help pick/place detection?

Steps: (cached) frames -> frozen video features -> causal window audio (clip + 8 kHz glasses-like)
-> audio features (log-mel always; AudioSet transformer if `transformers` is installed)
-> video vs audio vs video+audio on the same windows. No downloads of data, no uploads, no API calls
(the optional transformer model is fetched once from Hugging Face if available).
"""
import argparse, json, shutil, subprocess, sys, time, zipfile
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C


def run(cmd, log):
    with open(log, "x", encoding="utf-8") as f:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        for line in p.stdout:
            print(line, end="", flush=True); f.write(line); f.flush()
        return p.wait()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--fake-encoder", action="store_true", help="offline smoke test only")
    ap.add_argument("--skip-ast", action="store_true")
    a = ap.parse_args()
    root = Path(__file__).resolve().parent
    for item in json.loads((root / "package_manifest.json").read_text(encoding="utf-8"))["files"]:
        if C.sha256_file(root / item["path"]) != item["sha256"]:
            raise SystemExit("PACKAGE_CHANGED: " + item["path"])
    base = a.project / "fho_transfer_v1"
    sel, clips, cache = base / "selection_v2", base / "downloads" / "v2" / "clips", base / "frames_v2"
    for p in (sel, clips):
        if not p.is_dir():
            raise SystemExit("MISSING: " + str(p))
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool.upper()}_NOT_FOUND: in WSL run  sudo apt install -y ffmpeg  then rerun")
    out = base / "runs" / ("run_v5_audio_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"))
    out.mkdir(parents=True, exist_ok=False)
    print("RUN_DIR:", out, flush=True)
    t0, py, steps = time.monotonic(), sys.executable, {}
    steps["extract_frames"] = run([py, "-B", "-u", str(root / "extract_windows.py"), "--selection", str(sel), "--clips", str(clips), "--cache", str(cache)], out / "extract_frames.log")
    vcmd = [py, "-B", "-u", str(root / "make_features_v4.py"), "--cache", str(cache), "--selection", str(sel), "--output", str(out / "video_features.npz"), "--device", a.device]
    if a.fake_encoder:
        vcmd.append("--fake-encoder")
    steps["video_features"] = run(vcmd, out / "video_features.log")
    steps["extract_audio"] = run([py, "-B", "-u", str(root / "extract_audio.py"), "--selection", str(sel), "--clips", str(clips),
                                  "--output", str(out / "audio_windows.npz")], out / "extract_audio.log")
    if steps["extract_audio"] == 0:
        acmd = [py, "-B", "-u", str(root / "audio_features.py"), "--audio", str(out / "audio_windows.npz"), "--output", str(out / "audio_features.npz"), "--device", a.device]
        if a.skip_ast:
            acmd.append("--skip-ast")
        steps["audio_features"] = run(acmd, out / "audio_features.log")
    if steps.get("video_features") == 0 and steps.get("audio_features") == 0:
        steps["train_eval_audio"] = run([py, "-B", "-u", str(root / "train_eval_audio.py"), "--video", str(out / "video_features.npz"),
                                         "--audio", str(out / "audio_features.npz"), "--selection", str(sel), "--output", str(out / "results")],
                                        out / "train_eval_audio.log")
    ok = steps.get("train_eval_audio") == 0
    status = {"status": "completed" if ok else "needs_attention", "steps_exit_codes": steps, "fake_encoder": a.fake_encoder,
              "seconds": round(time.monotonic() - t0, 1), "policy": C.POLICY, "uploads": 0, "api_calls": 0,
              "package_manifest_sha256": C.sha256_file(root / "package_manifest.json")}
    C.save_json(out / "run_status.json", status)
    keep = ["run_status.json", "extract_frames.log", "video_features.log", "video_features.json", "extract_audio.log", "audio_windows.json",
            "audio_features.log", "audio_features.json", "train_eval_audio.log", "results/audio_results.json",
            "results/dev_scores_audio.csv", "results/eval_scores_audio.csv"]
    files = [out / k for k in keep if (out / k).is_file()] + [root / "package_manifest.json"]
    rel = lambda f: f.relative_to(out).as_posix() if f.is_relative_to(out) else "package_manifest.json"
    C.save_json(out / "return_manifest.json", {"files": [{"path": rel(f), "bytes": f.stat().st_size, "sha256": C.sha256_file(f)} for f in files]})
    zp = out / "RoboMemo_audio_results_v5.zip"
    with zipfile.ZipFile(zp, "x", zipfile.ZIP_DEFLATED) as z:
        for f in files + [out / "return_manifest.json"]:
            z.write(f, rel(f))
    print("RETURN_ZIP:", zp, flush=True)
    print("RETURN_SIZE_MB:", round(zp.stat().st_size / 1048576, 2), flush=True)
    print("PIPELINE_STATUS:", status["status"], flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
