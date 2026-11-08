#!/usr/bin/env python3
"""Check the shareable source tree for common accidental additions.

This is a guardrail, not a guarantee that arbitrary new text/media is anonymous.
Local generated model/state/build directories are outside this source audit.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {".git", "__pycache__", "node_modules", ".venv", "venv", "DerivedData",
              ".build", "SourcePackages", "xcuserdata", ".private-state", ".auto-work"}
SKIP_PREFIXES = ("models/local/", "baselines/hand_object/checkpoints/", "apps/demo/.state/",
                 "services/bridge/.state/", "apps/demo/state/", "services/bridge/state/")
BINARY_ARTIFACTS = {".pt", ".pth", ".npz", ".npy", ".safetensors", ".onnx", ".zip", ".p12", ".p8", ".pem", ".mobileprovision"}
MEDIA = {".png", ".jpg", ".jpeg", ".gif", ".mp4", ".mov", ".wav", ".m4a", ".mp3", ".heic"}
PATTERNS = {
    "personal_home_path": re.compile(r"(?:/Users/|/home/)[A-Za-z0-9_.-]+/|[A-Z]:\\\\?Users\\\\?[A-Za-z0-9_.-]+", re.I),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    "provider_secret": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b"),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
}


def run(root=ROOT):
    allow = json.loads((root / "tools/public_assets.json").read_text())
    findings, count, total, largest = [], 0, 0, 0
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if (set(path.relative_to(root).parts) & SKIP_PARTS or rel.startswith(SKIP_PREFIXES)
                or any(part.startswith(".venv-") for part in path.relative_to(root).parts)
                or rel.startswith("local-data/")):
            continue
        if path.is_symlink():
            findings.append({"file": rel, "reason": "symlink is not allowed in source package"})
            continue
        if not path.is_file():
            continue
        count += 1
        size = path.stat().st_size
        total += size
        largest = max(largest, size)
        if size >= 25 * 1024 * 1024:
            findings.append({"file":rel,"reason":"unexpected large source file"})
        if path.suffix.lower() in BINARY_ARTIFACTS:
            findings.append({"file":rel,"reason":"model/archive/credential artifact outside local-only folders"})
        if path.name in {".env", "auth.json", "credentials.json", "tokens.json", "Config.local.xcconfig"}:
            findings.append({"file":rel,"reason":"local credentials/config filename"})
        data = path.read_bytes()
        if path.suffix.lower() in MEDIA:
            approved = allow.get(rel)
            if not approved or hashlib.sha256(data).hexdigest() != approved["sha256"]:
                findings.append({"file":rel,"reason":"media not in reviewed public asset allowlist"})
            continue
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(content):
                findings.append({"file":rel,"reason":label})
    return {"passed":not findings,"files_checked":count,"bytes_checked":total,
            "largest_file_bytes":largest,"findings":findings,
            "limits":"Pattern scan and approved asset hashes only. Review every new diff. Does not scan excluded local state or certify arbitrary model/data redistribution rights."}


if __name__ == "__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output",type=Path)
    args=p.parse_args()
    result=run()
    encoded=json.dumps(result,indent=2,ensure_ascii=False)+"\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded,end="")
    raise SystemExit(0 if result["passed"] else 1)
