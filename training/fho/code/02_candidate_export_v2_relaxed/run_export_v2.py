"""Run the read-only FHO candidate export and pack a small return ZIP."""
import argparse, hashlib, json, sys, traceback, zipfile
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_candidates_v2 as E


def digest(p):
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument("--annotations", type=Path, required=True)
    a = ap.parse_args()
    root = Path(__file__).resolve().parent
    receipt = json.loads((root / "package_manifest.json").read_text(encoding="utf-8"))
    for item in receipt["files"]:
        if digest(root / item["path"]) != item["sha256"]:
            raise SystemExit("PACKAGE_CHANGED: " + item["path"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    out = a.project / "candidate_exports_v2" / ("export_" + stamp)
    data = out / "data"
    print("OUTPUT:", out, flush=True)
    print("Read-only export of eligible FHO actions. No download, upload or training.", flush=True)
    status = "export_completed"
    try:
        E.export(argparse.Namespace(annotations=a.annotations, metadata=None, output=data,
                                    policy=root / "protocol/exclusion_policy.json", parser="auto"))
    except Exception as exc:
        status = "export_needs_attention"
        out.mkdir(parents=True, exist_ok=True)
        (out / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        print("ERROR:", type(exc).__name__, exc, flush=True)
    (out / "run_status.json").write_text(json.dumps({"status": status, "package_manifest_sha256": digest(root / "package_manifest.json"),
                                                     "downloads": 0, "uploads": 0, "training_runs": 0}, indent=2) + "\n", encoding="utf-8")
    files = sorted(p for p in out.rglob("*") if p.is_file())
    (out / "return_manifest.json").write_text(json.dumps({"files": [{"path": p.relative_to(out).as_posix(), "bytes": p.stat().st_size, "sha256": digest(p)} for p in files]}, indent=2) + "\n", encoding="utf-8")
    files.append(out / "return_manifest.json")
    zp = out / "RoboMemo_candidates_v2_return.zip"
    with zipfile.ZipFile(zp, "x", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, p.relative_to(out).as_posix())
    if zp.stat().st_size > 30 * 1024 * 1024:
        print("WARNING: return ZIP larger than 30 MiB", flush=True)
    print("RETURN_ZIP:", zp, flush=True)
    print("RETURN_SIZE_MB:", round(zp.stat().st_size / 1048576, 3), flush=True)
    print("EXPORT_STATUS:", status, flush=True)
    return 0 if status == "export_completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
