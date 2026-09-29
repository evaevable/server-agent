#!/usr/bin/env bash
# 把四种故障都恢复（演练结束后的清场）
set -e
rm -rf /var/log/fill
[ -f /tmp/cpu-hog.pids ] && xargs -r kill < /tmp/cpu-hog.pids && rm -f /tmp/cpu-hog.pids
[ -f /tmp/port-hog.pid ] && kill "$(cat /tmp/port-hog.pid)" 2>/dev/null && rm -f /tmp/port-hog.pid
nginx || true
echo "已恢复：清理填充文件、结束 CPU 占用与端口占用、重启 nginx"
