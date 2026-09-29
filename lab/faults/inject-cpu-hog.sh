#!/usr/bin/env bash
# 故障注入：拉起两个吃满 CPU 的进程，并把它们的 pid 记到 /tmp/cpu-hog.pids
set -e
for _ in 1 2; do
  nohup sh -c 'while :; do :; done' >/dev/null 2>&1 &
  echo $! >> /tmp/cpu-hog.pids
done
echo "已启动 CPU 占用进程：$(cat /tmp/cpu-hog.pids | tr '\n' ' ')"
