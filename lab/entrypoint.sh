#!/usr/bin/env bash
# 启动 sshd 与 nginx；密码来自环境变量，方便演练。
set -e
echo "root:${ROOT_PASSWORD:-labpass}" | chpasswd
mkdir -p /var/run/sshd
nginx || true
exec /usr/sbin/sshd -D -e
