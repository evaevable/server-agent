---
name: disk-full
title: 磁盘使用率过高 / 写满
symptoms: [磁盘, 满了, 空间不足, disk, full, no space, df, 使用率]
---
# 磁盘满排查手册

## 判断
1. `df -h` 看哪个分区使用率最高（>= 90% 就要处理）；
2. 确认是「分区满」还是「inode 满」（后者的表现是写不进文件但还有空间）。

## 定位
1. `du -sh /var/log/* | sort -h | tail` 找大目录（从最大的几层往下钻）；
2. 常见元凶：未轮转的应用日志、容器日志（`/var/lib/docker/containers/*/*-json.log`）、
   被删除但未释放的文件（`lsof | grep deleted`）、core dump。

## 处置（写操作，需要审批）
1. 先轮转/压缩旧日志（`logrotate -f /etc/logrotate.d/nginx`）；
2. 清理临时文件（`clean_directory`，只删 N 天以前的）；
3. 变更数据库类服务前先确认备份与业务窗口。

## 验证
`df -h` 复查；确认应用能继续写日志。
