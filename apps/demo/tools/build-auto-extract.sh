#!/bin/bash
set -euo pipefail

tool_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
build_directory="${1:-${TMPDIR:-/tmp}/robomemo-auto-extract-build}"
developer_directory="${DEVELOPER_DIR:-$(xcode-select -p)}"
if [[ ! -x "$developer_directory/Toolchains/XcodeDefault.xctoolchain/usr/bin/swiftc" ]]; then
  developer_directory="$(xcode-select -p)"
fi
mkdir -p "$build_directory/module-cache"
DEVELOPER_DIR="$developer_directory" xcrun swiftc -parse-as-library -O \
  -module-cache-path "$build_directory/module-cache" \
  "$tool_directory/AutoExtract.swift" -o "$build_directory/auto-extract"
cp "$build_directory/auto-extract" "$tool_directory/auto-extract"
chmod 755 "$tool_directory/auto-extract"
printf '%s\n' "$tool_directory/auto-extract"
