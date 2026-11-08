"""Run the read-only inventory and small operator probe; return only reports."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import zipfile


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def verify_package(root):
    receipt = json.loads((root / "package_manifest.json").read_text(encoding="utf-8"))
    for item in receipt["files"]:
        path = (root / item["path"]).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError("Unsafe package manifest path")
        if not path.is_file() or digest(path) != item["sha256"]:
            raise ValueError("Package file missing or changed: " + item["path"])
    return receipt


def run_logged(command, log_path):
    with log_path.open("x", encoding="utf-8") as log:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace")
        for line in process.stdout:
            print(line, end="", flush=True)
            log.write(line)
            log.flush()
        return process.wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    verify_package(root)
    if not args.annotations.is_dir():
        raise SystemExit("ANNOTATIONS_NOT_FOUND: " + str(args.annotations))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    out = args.output or args.project / "annotation_checks" / ("check_" + stamp)
    out.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    print("OUTPUT:", out, flush=True)
    print("This checks annotations and small operators. No download, upload or training.", flush=True)
    probe_code = run_logged([sys.executable, "-B", "-u", str(root / "environment_probe.py"),
                            "--output", str(out / "environment_probe.json")], out / "environment_probe.log")
    audit_code = run_logged([sys.executable, "-B", "-u", str(root / "audit_annotations.py"),
                            "--annotations", str(args.annotations), "--project", str(args.project),
                            "--output", str(out / "inventory")], out / "inventory.log")
    status = {"schema": "robomemo-annotation-check-return-v1",
              "status": "checks_completed" if audit_code == 0 and probe_code == 0 else "check_needs_attention",
              "annotation_exit_code": audit_code, "environment_probe_exit_code": probe_code,
              "elapsed_seconds": round(time.monotonic() - started, 2),
              "new_training_runs": 0, "uploads": 0, "downloads": 0,
              "package_manifest_sha256": digest(root / "package_manifest.json")}
    (out / "run_status.json").write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    files = sorted(p for p in out.rglob("*") if p.is_file())
    receipt = {"files": [{"path": p.relative_to(out).as_posix(), "bytes": p.stat().st_size,
                          "sha256": digest(p)} for p in files]}
    (out / "return_manifest.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    files.append(out / "return_manifest.json")
    zip_path = out / "RoboMemo_annotation_return.zip"
    with zipfile.ZipFile(zip_path, "x", zipfile.ZIP_DEFLATED) as z:
        for path in files:
            z.write(path, path.relative_to(out).as_posix())
    with zipfile.ZipFile(zip_path) as z:
        if z.testzip() is not None:
            raise RuntimeError("Return archive CRC check failed")
    if zip_path.stat().st_size > 20 * 1024 * 1024:
        raise RuntimeError("Return archive exceeds expected 20 MiB; do not upload before checking")
    print("RETURN_ZIP:", zip_path, flush=True)
    print("RETURN_SIZE_MB:", round(zip_path.stat().st_size / 1024 / 1024, 3), flush=True)
    print("CHECK_STATUS:", status["status"], flush=True)
    if status["status"] != "checks_completed":
        print("Send the ZIP even if a check failed; it contains the error details.", flush=True)
    return 0 if status["status"] == "checks_completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
