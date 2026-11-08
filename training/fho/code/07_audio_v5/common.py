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
    """Load a selection dir. v1: train_1000/dev/eval. v2 adds train_all.json and eval_v1_ids.json.
    Returns train_full (largest train set, ordered), dev, eval, nested {size: id set}, eval_v1_ids (or None)."""
    sel_dir = Path(sel_dir)
    def rows(name):
        data = json.loads((sel_dir / f"{name}.json").read_text(encoding="utf-8"))
        for r in data:
            if r.get("policy") != POLICY:
                raise ValueError("Selection policy mismatch: " + str(r.get("policy")))
        return data
    out = {"dev": rows("dev"), "eval": rows("eval")}
    nested = {}
    for n in (200, 500, 1000):
        ids = {r["window_id"] for r in rows(f"train_{n}")}
        nested[len(ids)] = ids  # keyed by actual size
    full = rows("train_all") if (sel_dir / "train_all.json").is_file() else rows("train_1000")
    nested[len(full)] = {r["window_id"] for r in full}
    sizes = sorted(nested)
    for a, b in zip(sizes, sizes[1:]):
        if not nested[a] <= nested[b]:
            raise ValueError("Nested training subsets are not nested")
    out["train_full"], out["nested"] = full, nested
    ev1 = sel_dir / "eval_v1_ids.json"
    out["eval_v1_ids"] = set(json.loads(ev1.read_text(encoding="utf-8"))) if ev1.is_file() else None
    if out["eval_v1_ids"] is not None and not out["eval_v1_ids"] <= {r["window_id"] for r in out["eval"]}:
        raise ValueError("eval_v1 ids must be a subset of eval")
    ids = [r["window_id"] for r in full + out["dev"] + out["eval"]]
    if len(ids) != len(set(ids)) or None in ids:
        raise ValueError("Window IDs must be unique across splits")
    parents = {k: {r["canonical_parent"] for r in v} for k, v in (("train", full), ("dev", out["dev"]), ("eval", out["eval"]))}
    if parents["train"] & parents["dev"] or parents["train"] & parents["eval"] or parents["dev"] & parents["eval"]:
        raise ValueError("Parent leakage between splits")
    return out


def targets(decision_time):
    step = WIN / (N_FRAMES - 1)
    return [decision_time - WIN + k * step for k in range(N_FRAMES)]
