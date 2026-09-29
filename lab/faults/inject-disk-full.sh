#!/usr/bin/env bash
# 故障注入：把某个目录写满（默认 /var/log/fill）
set -e
TARGET="${1:-/var/log/fill}"
mkdir -p "$TARGET"
dd if=/dev/zero of="$TARGET/bigfile" bs=1M count=200 status=none
echo "已写入 $TARGET/bigfile（200MB）"
df -h "$TARGET" | tail -1
