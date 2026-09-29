#!/usr/bin/env bash
# 故障注入：占用 80 端口（用一个 python 临时监听），制造「端口冲突」
set -e
nohup python3 -c "
import socket, time
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
s.bind(('0.0.0.0', 80)); s.listen(1)
open('/tmp/port-hog.pid', 'w').write(str(__import__('os').getpid()))
time.sleep(3600)
" >/dev/null 2>&1 &
sleep 1
echo "已占用 80 端口，pid=$(cat /tmp/port-hog.pid 2>/dev/null)"
