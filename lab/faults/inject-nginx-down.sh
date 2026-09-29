#!/usr/bin/env bash
# 故障注入：停掉 nginx
set -e
nginx -s stop 2>/dev/null || pkill -f 'nginx: master' || true
sleep 1
if pgrep -f 'nginx: master' >/dev/null; then echo "nginx 仍在运行"; else echo "nginx 已停止"; fi
