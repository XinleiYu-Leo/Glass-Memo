"""RoboMemo fho_transfer_v1 · model-improvement round (v4).

Option A: frozen appearance + frozen frame-difference features (numpy heads).
Option B: partial fine-tuning of MobileNetV3-Small on the GPU (smoke test first).
Then one comparison on the same dev/eval windows. No downloads, uploads or API calls.
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
    ap.add_argument("--skip-finetune", action="store_true", help="offline smoke test only")
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
    if not shutil.which("ffmpeg"):
        raise SystemExit("FFMPEG_NOT_FOUND: in WSL run  sudo apt install -y ffmpeg  then rerun")
    out = base / "runs" / ("run_v4_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"))
    out.mkdir(parents=True, exist_ok=False)
    print("RUN_DIR:", out, flush=True)
    t0, py, steps = time.monotonic(), sys.executable, {}
    steps["extract"] = run([py, "-B", "-u", str(root / "extract_windows.py"), "--selection", str(sel), "--clips", str(clips), "--cache", str(cache)], out / "extract.log")
    fcmd = [py, "-B", "-u", str(root / "make_features_v4.py"), "--cache", str(cache), "--selection", str(sel), "--output", str(out / "features_v4.npz"), "--device", a.device]
    if a.fake_encoder:
        fcmd.append("--fake-encoder")
    steps["features_v4"] = run(fcmd, out / "features_v4.log") if steps["extract"] in (0, 3) else None
    if steps["features_v4"] == 0:
        steps["option_a"] = run([py, "-B", "-u", str(root / "train_eval_v4.py"), "--features", str(out / "features_v4.npz"),
                                 "--selection", str(sel), "--output", str(out / "option_a")], out / "option_a.log")
    if not a.skip_finetune:
        steps["finetune_smoke"] = run([py, "-B", "-u", str(root / "finetune.py"), "--cache", str(cache), "--selection", str(sel),
                                       "--output", str(out / "finetune_smoke"), "--device", a.device, "--smoke"], out / "finetune_smoke.log")
        if steps["finetune_smoke"] == 0:
            steps["option_b"] = run([py, "-B", "-u", str(root / "finetune.py"), "--cache", str(cache), "--selection", str(sel),
                                     "--output", str(out / "option_b"), "--device", a.device], out / "option_b.log")
    if steps.get("option_a") == 0:
        ccmd = [py, "-B", "-u", str(root / "compare_v4.py"), "--option_a", str(out / "option_a"), "--selection", str(sel), "--output", str(out / "comparison")]
        if steps.get("option_b") == 0:
            ccmd += ["--option_b", str(out / "option_b")]
        steps["compare"] = run(ccmd, out / "compare.log")
    ok_a = steps.get("option_a") == 0 and steps.get("compare") == 0
    ok_b = steps.get("option_b") == 0
    status = {"status": "completed" if ok_a and (ok_b or a.skip_finetune) else ("partial_option_a_only" if ok_a else "needs_attention"),
              "steps_exit_codes": steps, "fake_encoder": a.fake_encoder, "seconds": round(time.monotonic() - t0, 1),
              "policy": C.POLICY, "downloads": 0, "uploads": 0, "api_calls": 0,
              "package_manifest_sha256": C.sha256_file(root / "package_manifest.json")}
    C.save_json(out / "run_status.json", status)
    keep = ["run_status.json", "extract.log", "features_v4.log", "features_v4.json", "option_a.log", "option_a/metrics_v4.json",
            "option_a/dev_scores_v4.csv", "option_a/eval_scores_v4.csv", "finetune_smoke.log", "option_b.log",
            "option_b/finetune_summary.json", "option_b/finetune_progress.log", "option_b/dev_scores_finetune.csv",
            "option_b/eval_scores_finetune.csv", "option_b/finetune_n_full_seed0.pt", "compare.log", "comparison/comparison_v4.json"]
    files = [out / k for k in keep if (out / k).is_file()] + [root / "package_manifest.json"]
    rel = lambda f: f.relative_to(out).as_posix() if f.is_relative_to(out) else "package_manifest.json"
    C.save_json(out / "return_manifest.json", {"files": [{"path": rel(f), "bytes": f.stat().st_size, "sha256": C.sha256_file(f)} for f in files]})
    zp = out / "RoboMemo_fho_transfer_v1_results_v4.zip"
    with zipfile.ZipFile(zp, "x", zipfile.ZIP_DEFLATED) as z:
        for f in files + [out / "return_manifest.json"]:
            z.write(f, rel(f))
    print("RETURN_ZIP:", zp, flush=True)
    print("RETURN_SIZE_MB:", round(zp.stat().st_size / 1048576, 2), flush=True)
    print("PIPELINE_STATUS:", status["status"], flush=True)
    return 0 if status["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
