"""RoboMemo fho_transfer_v1: extract frames -> frozen features -> small heads -> return ZIP.

Read-only for sources. Writes only under <project>/fho_transfer_v1/{frames_v1,runs}.
No downloads, uploads or API calls. Ego4D images are NOT placed in the return ZIP.
"""
import argparse, hashlib, json, shutil, subprocess, sys, time, zipfile
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


def contact_sheets(cache, eval_rows, out_dir, n=12):
    try:
        import numpy as np
        from PIL import Image, ImageDraw
    except ImportError:
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    made = 0
    for label in (1, 0):
        rows = [r for r in eval_rows if r["label"] == label and (cache / f"{r['window_id']}.npz").is_file()][:n]
        if not rows:
            continue
        sheet = Image.new("RGB", (10 * 112, len(rows) * 130), (255, 255, 255)); d = ImageDraw.Draw(sheet)
        for i, r in enumerate(rows):
            with np.load(cache / f"{r['window_id']}.npz") as z:
                for k, fr in enumerate(z["frames"]):
                    sheet.paste(Image.fromarray(fr).resize((112, 112)), (k * 112, i * 130 + 18))
            d.text((4, i * 130 + 2), f"{r['window_id'][:8]} {r['verb']} label={label}", fill=(0, 0, 0))
        sheet.save(out_dir / f"eval_{'positive' if label else 'negative'}_examples.png"); made += 1
    return made


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--fake-encoder", action="store_true", help="smoke test only")
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
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = base / "runs" / f"run_{stamp}"
    out.mkdir(parents=True, exist_ok=False)
    print("RUN_DIR:", out, flush=True)
    t0 = time.monotonic(); steps = {}
    py = sys.executable
    steps["extract"] = run([py, "-B", "-u", str(root / "extract_windows.py"), "--selection", str(sel), "--clips", str(clips),
                            "--cache", str(cache), "--workers", str(a.workers)], out / "extract.log")
    if steps["extract"] in (0, 3):
        fcmd = [py, "-B", "-u", str(root / "make_features.py"), "--cache", str(cache), "--output", str(out / "features.npz"), "--device", a.device]
        if a.fake_encoder:
            fcmd.append("--fake-encoder")
        steps["features"] = run(fcmd, out / "features.log")
        if steps["features"] == 0:
            steps["train_eval"] = run([py, "-B", "-u", str(root / "train_eval.py"), "--features", str(out / "features.npz"),
                                       "--selection", str(sel), "--output", str(out / "results")], out / "train_eval.log")
    ok = steps.get("train_eval") == 0
    status = {"status": "completed" if ok else "needs_attention", "steps_exit_codes": steps, "fake_encoder": a.fake_encoder,
              "seconds": round(time.monotonic() - t0, 1), "policy": C.POLICY, "downloads": 0, "uploads": 0, "api_calls": 0,
              "package_manifest_sha256": C.sha256_file(root / "package_manifest.json")}
    try:
        man = json.loads((cache / "extraction_manifest.json").read_text(encoding="utf-8"))
        C.save_json(out / "extraction_summary.json", {"status_counts": man["status_counts"], "seconds": man["seconds"],
                    "not_ok": [w for w in man["windows"] if w["status"] not in ("extracted", "cached")],
                    "clip_sha256": man["clip_sha256"], "n_frames": man["n_frames"], "window_sec": man["window_sec"]})
    except Exception as exc:
        status["extraction_summary_error"] = str(exc)
    sel_rows = C.load_selection(sel)
    status["local_contact_sheets"] = contact_sheets(cache, sel_rows["eval"], out / "local_only_contact_sheets")
    C.save_json(out / "run_status.json", status)
    keep = ["run_status.json", "extraction_summary.json", "extract.log", "features.log", "train_eval.log", "features.json",
            "results/metrics.json", "results/eval_predictions.csv", "results/head_weights.npz"]
    files = [out / k for k in keep if (out / k).is_file()]
    files.append(root / "package_manifest.json")
    manifest = {"files": [{"path": (f.relative_to(out).as_posix() if f.is_relative_to(out) else "package_manifest.json"),
                           "bytes": f.stat().st_size, "sha256": C.sha256_file(f)} for f in files]}
    C.save_json(out / "return_manifest.json", manifest)
    zp = out / "RoboMemo_fho_transfer_v1_results_v3.zip"
    with zipfile.ZipFile(zp, "x", zipfile.ZIP_DEFLATED) as z:
        for f in files + [out / "return_manifest.json"]:
            z.write(f, f.relative_to(out).as_posix() if f.is_relative_to(out) else "package_manifest.json")
    print("LOCAL_CONTACT_SHEETS (view only, do not upload):", out / "local_only_contact_sheets", flush=True)
    print("RETURN_ZIP:", zp, flush=True)
    print("RETURN_SIZE_MB:", round(zp.stat().st_size / 1048576, 2), flush=True)
    print("PIPELINE_STATUS:", status["status"], flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
