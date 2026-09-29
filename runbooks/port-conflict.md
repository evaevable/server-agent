---
name: port-conflict
title: 端口被占用
symptoms: [端口, 占用, address already in use, 监听, bind]
---
# 端口冲突排查手册

## 判断
`ss -lntp | grep <端口>` 找到占用进程的 pid 与名字。

## 常见原因
1. 旧进程没退出（重启脚本没杀干净）；
2. 两个服务配了同一个端口；
3. TIME_WAIT 过多（表现为偶发 bind 失败）。

## 处置
1. 先确认占用者是不是「本该在跑的服务」；
2. 是僵尸进程 → 温和信号 TERM；
3. 是配置冲突 → 改配置（属于变更，需审批）。
