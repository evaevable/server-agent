# 靶场：三台可被排查的「服务器」

`web-01` / `web-02` / `db-01`，都带 sshd 与 nginx，端口分别是 2201 / 2202 / 2203。

```bash
cd lab
export LAB_SSH_PASSWORD=labpass        # inventory.yaml 里用 password_env 引用它
docker compose up -d --build
docker compose ps
```

## 故障注入

| 脚本 | 制造的问题 | Agent 应该怎么查出来 |
|---|---|---|
| `inject-disk-full.sh` | `/var/log/fill` 下写入 200MB | `remote_run df -h` → 再 `du` 定位 |
| `inject-cpu-hog.sh` | 两个死循环进程 | `remote_run uptime` 看 load → `ps` 找进程 |
| `inject-nginx-down.sh` | nginx 停止 | `ss` 看不到 80 端口 → `systemctl status` |
| `inject-port-conflict.sh` | 80 端口被临时进程占用 | `ss -lntp` 看到占用 pid |

用法（在容器里注入）：`docker exec sa-web-01 bash /opt/faults/inject-disk-full.sh`

清场：`docker exec sa-web-01 bash /opt/faults/fix-all.sh`

## 为什么用容器而不是真机

- 可以随便注入故障、随便删文件，不心疼；
- 可重复：`docker compose down -v && up -d` 就回到干净状态；
- 演练环境才允许 `strict_host_key: false`（真机上请打开校验）。
