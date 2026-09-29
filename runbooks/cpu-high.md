---
name: cpu-high
title: CPU 使用率或负载过高
symptoms: [cpu, 卡, 负载, load, 慢, 高, 卡顿]
---
# CPU / 负载高排查手册

## 判断
1. `uptime` 看 1/5/15 分钟 load，与 CPU 核数对比（load > 核数 说明有排队）；
2. 看是「用户态高」还是「系统态高」还是「iowait 高」——三者根因完全不同。

## 定位
1. `ps aux --sort=-%cpu | head` 找吃 CPU 的进程；
2. 若是容器环境，注意 ulimit/cgroup 限制导致的「看起来不高但很慢」。

## 处置
1. 先确认这个进程是不是业务必需的（别上来就 kill）；
2. 允许温和信号 TERM；不要用 KILL；
3. 扩容或限流通常是更长期的解法。
