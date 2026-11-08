"""Windows/WSL source preparation for the frozen 4-versus-10-frame comparison.

Select64 -> download only those64 -> original-source PNGs -> independent verify
-> return ZIPs, each <=20 MiB. No teacher API, training, upload, or credentials.
Existing valid stages are checked and resumed; changed frozen outputs STOP.
"""
from __future__ import annotations
import argparse
import os
import shutil
from collections import Counter
import csv
from datetime import datetime, timezone
import importlib
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import zipfile

from export_sources import digest, require, save_json, write_once

PACKAGE = Path(__file__).resolve().parents[1]
MAX_ZIP_BYTES = 20 * 1024 * 1024
OFFICIAL_CLI = Path(os.environ.get("ROBOMEMO_EGO4D_CLI") or shutil.which("ego4d") or "ego4d")
METADATA_ALTERNATIVES = (Path("data/ego4d/ego4d.json"), Path("data/ego4d/v2/ego4d.json"),
                         Path("data/ego4d/v2/annotations/ego4d.json"))


def stage(number, message):
    print(f"\nSTAGE {number}/7: {message}", flush=True)


def preflight(args):
    stage(1, "Check local dependencies and official input files before selection/download")
    for name in ("av", "PIL"):
        try:
            importlib.import_module(name)
        except ImportError as exc:
            raise ValueError(f"Missing Python dependency {name}; install package requirements before running") from exc
    if not args.metadata.is_file():
        alternatives = ", ".join(str(p) for p in METADATA_ALTERNATIVES)
        raise ValueError(f"Official metadata missing: {args.metadata}. Known locations: {alternatives}. Pass --metadata explicitly; no silent substitution.")
    for name in ("manifest", "exclusions", "reservation", "protocol"):
        path = getattr(args, name)
        require(path.is_file() and not path.is_symlink(), f"Missing/unsafe {name}: {path}")
    require(args.annotations.is_dir(), "Missing official annotations directory")
    for name in ("fho_lta_train.json", "fho_lta_taxonomy.json", "fho_lta_val.json", "fho_lta_test_unannotated.json", "vq_val.json", "vq_test_unannotated.json"):
        require((args.annotations / name).is_file(), "Missing official annotation: " + name)
    require(args.reserved_clips.exists(), "Reserved60 input missing: " + str(args.reserved_clips))
    if args.reserved_clips.is_dir():
        uids = {p.stem for p in args.reserved_clips.glob("*.mp4")}
        require(len(uids) == 60, "Reserved directory must contain exactly the existing60 VQ MP4s; pass their original ID text file with --reserved-clips if the directory has changed")
        require(args.reserved_clips.resolve() != (args.output / "downloads/ego4d/v2/clips").resolve(), "Reserved folder cannot be the new source download folder")
    require(OFFICIAL_CLI.is_file(), "Installed official Ego4D CLI missing: " + str(OFFICIAL_CLI))
    require(args.project.is_dir(), "Project folder missing: " + str(args.project))
    require(not args.output.is_symlink(), "Output directory must not be a symbolic link")
    args.output.mkdir(parents=True, exist_ok=True)


def selected_byte_estimate(manifest, selected):
    with manifest.open(newline="", encoding="utf-8-sig") as f:
        rows = {row["exported_clip_uid"]: row for row in csv.DictReader(f)}
    columns = ("size", "size_bytes", "file_size", "file_size_bytes", "bytes", "video_size")
    for column in columns:
        try:
            values = [int(rows[item["clip_uid"]][column]) for item in selected]
            if all(value > 0 for value in values):
                return sum(values), column
        except (KeyError, ValueError, TypeError):
            continue
    return None, None


def download(args, selection):
    stage(3, "Download only the64 already frozen clips using the installed Ego4D profile")
    expected_bytes, column = selected_byte_estimate(args.manifest, selection["items"])
    if expected_bytes is None:
        print("DOWNLOAD_SIZE_ESTIMATE: unavailable in this manifest; exact downloaded bytes will be recorded. Only64 frozen clip IDs are requested.", flush=True)
    else:
        print(f"DOWNLOAD_SIZE_ESTIMATE: {expected_bytes:,} bytes ({expected_bytes / 1024**3:.3f} GiB), manifest column {column};64 clips", flush=True)
    folder = args.output / "downloads"
    clip_dir = folder / "ego4d/v2/clips"
    folder.mkdir(parents=True, exist_ok=True)
    receipt_path = args.output / "download_receipt.json"
    uids = sorted(x["clip_uid"] for x in selection["items"])
    uid_path = folder / "frozen_download_uids.txt"
    write_once(uid_path, ("\n".join(uids) + "\n").encode("ascii"))
    existing = {p.stem for p in clip_dir.glob("*.mp4")}
    require(existing <= set(uids), "New source folder contains unfrozen MP4s; STOP without deleting")
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_bytes())
        require(receipt["selection_sha256"] == digest(args.output / "selection_frozen.json")
                and receipt["official_manifest_sha256"] == digest(args.manifest), "Resume download freeze mismatch")
        require(existing == set(uids) and {s["clip_uid"] for s in receipt["sources"]} == set(uids), "Resume source list differs")
        for source in receipt["sources"]:
            path = clip_dir / (source["clip_uid"] + ".mp4")
            require(path.stat().st_size == source["bytes"] and digest(path) == source["sha256"], "Downloaded source changed: " + source["clip_uid"])
        print("DOWNLOAD_RESUME: all64 source hashes match the frozen receipt", flush=True)
        return clip_dir
    selection_hash = digest(args.output / "selection_frozen.json")
    command = [str(OFFICIAL_CLI), "--output_directory", str(folder / "ego4d"), "--datasets", "clips",
               "--video_uid_file", str(uid_path), "--version", "v2", "--no-metadata",
               "--manifest-override-path", str(args.manifest), "--aws_profile_name", "default", "-y"]
    # Missing receipt means completion is unproven, even if all64 filenames exist.
    # Always let the official downloader check/resume its version/size state.
    # Reuse the already-installed profile. Never open or copy credential files.
    print(f"DOWNLOAD_PROGRESS: {len(existing)}/64 filenames local; official CLI will verify/resume all64", flush=True)
    result = subprocess.run(command, check=False)
    require(result.returncode == 0, "Official download failed; rerun the same command to resume, never resample")
    require(digest(args.output / "selection_frozen.json") == selection_hash, "Selection changed during download")
    require({p.stem for p in clip_dir.glob("*.mp4")} == set(uids), "Downloaded MP4 set differs from frozen64")
    sources = []
    for n, item in enumerate(selection["items"], 1):
        path = clip_dir / (item["clip_uid"] + ".mp4")
        require(path.is_file() and not path.is_symlink() and path.stat().st_size > 0, "Missing/empty downloaded source")
        print(f"HASH_SOURCE {n}/64 {item['item_id']}", flush=True)
        sources.append({"item_id": item["item_id"], "clip_uid": item["clip_uid"], "video_uid": item["video_uid"],
                        "s3_path": item["s3_path"], "bytes": path.stat().st_size, "sha256": digest(path)})
    save_json(receipt_path, {"schema": "robomemo-tenframe-download-receipt-v1", "status": "complete",
                            "selection_sha256": selection_hash, "official_manifest_sha256": digest(args.manifest),
                            "command": command, "source_count": 64, "source_bytes": sum(s["bytes"] for s in sources),
                            "manifest_estimated_bytes": expected_bytes, "downloader_returncode": 0,
                            "download_version_file_sha256": digest(clip_dir / "manifest.ver") if (clip_dir / "manifest.ver").is_file() else None,
                            "sources": sources,
                            "teacher_calls": 0, "training_runs": 0, "uploads": 0})
    return clip_dir


def archive_bytes(entries, *, compressed=False):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED if compressed else zipfile.ZIP_STORED) as archive:
        for name, data in sorted(entries.items()):
            require(not name.startswith("/") and ".." not in Path(name).parts and "\\" not in name, "Unsafe archive member")
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED if compressed else zipfile.ZIP_STORED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return output.getvalue()


def verify_archive(data, expected):
    require(len(data) <= MAX_ZIP_BYTES, "Return archive exceeds20 MiB")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        require(len(archive.namelist()) == len(set(archive.namelist())) and set(archive.namelist()) == set(expected), "Return archive file list differs")
        require(archive.testzip() is None, "Return archive CRC failed")
        for name, content in expected.items():
            require(archive.read(name) == content, "Return archive bytes differ: " + name)


def image_entries(dataset, items, part, count):
    document = {"schema": "robomemo-tenframe-anonymous-input-part-v1", "synthetic_test": False,
                "part_number": part, "part_count": count, "total_case_count": 64,
                "time_origin": "source clip start; requested and actual times use this origin", "items": items}
    entries = {f"dataset/teacher_manifest_part{part:02d}.json": (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode()}
    for item in items:
        for frame in item["frames"]:
            entries["dataset/" + frame["path"]] = (dataset / frame["path"]).read_bytes()
    return entries


def package_returns(args):
    dataset = args.output / "dataset"
    anonymous = json.loads((dataset / "teacher_manifest.json").read_bytes())["items"]
    groups, current = [], []
    for item in anonymous:
        trial = current + [item]
        # Two digits reserve enough header room for every possible64-case part count.
        if len(archive_bytes(image_entries(dataset, trial, 99, 99))) > MAX_ZIP_BYTES:
            require(current, "One case exceeds20 MiB; cannot split its10-frame evidence")
            groups.append(current)
            current = [item]
        else:
            current = trial
    if current:
        groups.append(current)
    archives = []
    for index, group in enumerate(groups, 1):
        entries = image_entries(dataset, group, index, len(groups))
        data = archive_bytes(entries)
        verify_archive(data, entries)
        path = args.output / f"return_images_part{index:02d}.zip"
        write_once(path, data)
        archives.append({"path": path.name, "sha256": digest(path), "bytes": len(data),
                         "item_ids": [i["item_id"] for i in group], "members": sorted(entries)})
    require([i for a in archives for i in a["item_ids"]] == [f"T{x:02d}" for x in range(1, 65)], "Return parts omit or duplicate cases")
    expected_images = {a["path"] for a in archives}
    require({p.name for p in args.output.glob("return_images_part*.zip")} == expected_images, "Unexpected old return image parts")
    metadata_names = ("selection_frozen.json", "exclusion_audit.json", "download_receipt.json", "independent_validation.json", "dataset/manifest.json", "reviewed_labels_TEMPLATE.json")
    entries = {name: (args.output / name).read_bytes() for name in metadata_names}
    for name, source in (("experiment.json", args.protocol), ("source_exclusions.json", args.exclusions),
                         ("development_reservation.json", args.reservation),
                         ("actual_model_instructions.txt", PACKAGE / "protocol/actual_model_instructions.txt")):
        entries["protocol/" + name] = source.read_bytes()
    entries["return_images_index.json"] = (json.dumps({"schema": "robomemo-tenframe-return-images-index-v1", "archives": archives}, indent=2) + "\n").encode()
    data = archive_bytes(entries, compressed=True)
    verify_archive(data, entries)
    path = args.output / "return_metadata.zip"
    write_once(path, data)
    metadata_record = {"path": path.name, "sha256": digest(path), "bytes": len(data), "members": sorted(entries)}
    report = {"schema": "robomemo-tenframe-return-validation-v1", "status": "passed", "synthetic_test": False,
              "archive_limit_bytes": MAX_ZIP_BYTES, "images": archives, "metadata": metadata_record,
              "png_count": 640, "case_count": 64, "raw_videos_in_archives": 0,
              "teacher_calls": 0, "training_runs": 0, "uploads": 0}
    save_json(args.output / "return_validation.json", report)
    return report


def run(args):
    preflight(args)
    from select_sources import run as select
    from export_sources import export
    from verify_sources import load_json, validate_selection, verify_isolation, verify_dataset
    stage(2, "Freeze48 train +16 dev before opening/downloading images; independently replay exclusions and seed")
    args.selection = args.output / "selection_frozen.json"
    if not args.selection.exists():
        code = select(args)
        require(code == 0, "Insufficient eligible sources: STOP, no quota relaxation or resampling")
    require(all((args.output / name).is_file() for name in ("selection_frozen.json", "exclusion_audit.json", "download_uids.txt")),
            "Selection publication interrupted before all three freeze files completed. STOP before network; preserve the freeze for manual recovery, never reselect")
    selection = load_json(args.selection)
    validate_selection(selection)
    require((args.output / "download_uids.txt").read_text() == "\n".join(sorted(i["clip_uid"] for i in selection["items"])) + "\n",
            "Frozen download IDs changed")
    audit = load_json(args.output / "exclusion_audit.json")
    require(audit["exclusions"] == selection["exclusions"] and audit["source_files"] == selection["source_files"]
            and audit["group_resolution"] == selection["group_resolution"], "Frozen exclusion companion differs")
    require(selection["protocol_sha256"] == digest(args.protocol), "Protocol changed after selection freeze")
    isolation = verify_isolation(args, selection)
    clips = download(args, selection)
    dataset_dir = args.output / "dataset"
    stage(4, "Decode original sources into ten224px causal PNGs; four-frame subset indices0,3,6,9")
    if not (dataset_dir / "manifest.json").exists():
        export(SimpleNamespace(selection=args.selection, protocol=args.protocol,
                               receipt=args.output / "download_receipt.json", clips_dir=clips, output=dataset_dir))
    else:
        print("EXPORT_RESUME: manifest exists; independent full-source verification follows", flush=True)
    stage(5, "Independently verify every PNG/hash/PTS, source pixel lineage, fixed split and group isolation")
    verified = verify_dataset(SimpleNamespace(output=dataset_dir, selection=args.selection, protocol=args.protocol, clips_dir=clips), selection)
    save_json(args.output / "independent_validation.json", {
        "schema": "robomemo-tenframe-export-validation-v1", "status": "passed", "synthetic_test": False,
        "selection_sha256": digest(args.selection), "protocol_sha256": digest(args.protocol),
        "dataset_manifest_sha256": digest(dataset_dir / "manifest.json"),
        "independent_official_isolation_replay": True, "isolation": isolation, "dataset": verified,
        "teacher_calls": 0, "training_runs": 0, "uploads": 0})
    stage(6, "Create held/null review template; no candidate stratum becomes a label")
    template = args.output / "reviewed_labels_TEMPLATE.json"
    if not template.exists():
        subprocess.run([sys.executable, str(PACKAGE / "tools/make_labels_template.py"), "--dataset", str(dataset_dir), "--output", str(template)], check=True)
    value = load_json(template)
    require(value["schema"] == "robomemo-tenframe-reviewed-labels-v1"
            and value["protocol_sha256"] == digest(args.protocol)
            and value["dataset_manifest_sha256"] == digest(dataset_dir / "manifest.json")
            and [i["item_id"] for i in value["items"]] == [i["item_id"] for i in selection["items"]],
            "Existing review template belongs to a different dataset; preserve it separately and STOP")
    stage(7, "Build/verify exact return archives; each<=20MiB; original videos stay local")
    report = package_returns(args)
    print("PREPARATION_COMPLETE:", args.output, flush=True)
    for entry in report["images"] + [report["metadata"]]:
        print(f"RETURN_FILE: {args.output / entry['path']} ({entry['bytes']:,} bytes)", flush=True)
    print("Teacher calls0; uploads0; training runs0. Transfer only return_images_part*.zip and return_metadata.zip. Keep metadata separate from teacher requests.", flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project", type=Path, required=True)
    p.add_argument("--output", type=Path)
    p.add_argument("--annotations", type=Path, required=True)
    p.add_argument("--metadata", type=Path, default=METADATA_ALTERNATIVES[0])
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--reserved-clips", type=Path, required=True)
    p.add_argument("--exclusions", type=Path, default=PACKAGE / "protocol/source_exclusions.json")
    p.add_argument("--reservation", type=Path, default=PACKAGE / "protocol/development_reservation.json")
    p.add_argument("--protocol", type=Path, default=PACKAGE / "protocol/experiment.json")
    args = p.parse_args()
    args.output = args.output or args.project / "tenframe_comparison_20261026"
    try:
        run(args)
    except Exception as exc:
        p.exit(1, f"PREPARATION_STOPPED: {type(exc).__name__}: {exc}\nFrozen selection is retained; do not resample or relax exclusions. Correct the cause and rerun the same command.\n")


if __name__ == "__main__":
    main()
