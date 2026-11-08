"""Shared helpers for RoboMemo fho_transfer_v1 (frames -> features -> heads)."""
import hashlib, json, os
from pathlib import Path

POLICY = "fho_transfer_v1_draft3"
N_FRAMES = 10
WIN = 1.5
SIZE = 224
PAD_RGB = (124, 116, 104)  # ImageNet mean, same letterbox colour as earlier RoboMemo students
FOUR = [0, 3, 6, 9]        # four-frame comparison subset (2 fps)
STALE_MAX = 0.10           # seconds; a target whose newest past frame is older than this is invalid


def sha256_file(path, block=1 << 22):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(block), b""):
            h.update(b)
    return h.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def save_json(path, obj):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_selection(sel_dir):
    sel_dir = Path(sel_dir)
    out = {}
    for name in ("train_1000", "dev", "eval"):
        rows = json.loads((sel_dir / f"{name}.json").read_text(encoding="utf-8"))
        for r in rows:
            if r.get("policy") != POLICY:
                raise ValueError("Selection policy mismatch: " + str(r.get("policy")))
        out[name] = rows
    t200 = {r["window_id"] for r in json.loads((sel_dir / "train_200.json").read_text(encoding="utf-8"))}
    t500 = {r["window_id"] for r in json.loads((sel_dir / "train_500.json").read_text(encoding="utf-8"))}
    ids1000 = [r["window_id"] for r in out["train_1000"]]
    if not (t200 <= t500 <= set(ids1000)):
        raise ValueError("Nested training subsets are not nested")
    out["nested"] = {200: t200, 500: t500, 1000: set(ids1000)}
    parents = {k: {r["canonical_parent"] for r in out[k]} for k in ("train_1000", "dev", "eval")}
    if parents["train_1000"] & parents["dev"] or parents["train_1000"] & parents["eval"] or parents["dev"] & parents["eval"]:
        raise ValueError("Parent leakage between splits")
    return out


def targets(decision_time):
    step = WIN / (N_FRAMES - 1)
    return [decision_time - WIN + k * step for k in range(N_FRAMES)]
