#!/bin/bash
set -euo pipefail

tool_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
build_directory="${1:-${TMPDIR:-/tmp}/robomemo-auto-remux-build}"
developer_directory="${DEVELOPER_DIR:-$(xcode-select -p)}"
if [[ ! -x "$developer_directory/Toolchains/XcodeDefault.xctoolchain/usr/bin/swiftc" ]]; then
  developer_directory="$(xcode-select -p)"
fi
mkdir -p "$build_directory/module-cache"
DEVELOPER_DIR="$developer_directory" xcrun swiftc -parse-as-library -O \
  -module-cache-path "$build_directory/module-cache" \
  "$tool_directory/AutoRemux.swift" -o "$build_directory/auto-remux"
cp "$build_directory/auto-remux" "$tool_directory/auto-remux"
chmod 755 "$tool_directory/auto-remux"
printf '%s\n' "$tool_directory/auto-remux"
