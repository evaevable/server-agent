# 运维手册（内部约定）

## 目录与部署约定
- 应用代码部署在 `/opt/apps/<服务名>/current`，历史版本在 `releases/` 下按时间戳命名。
- nginx 站点配置在 `/etc/nginx/conf.d/*.conf`，日志在 `/var/log/nginx/`（access.log 与 error.log）。
- 所有服务用 systemd 管理：`systemctl status/restart <服务名>`；不要用 `kill` 直接杀进程，除非确认它没有守护进程接管。

## 备份约定
- 数据库备份目录是 `/data/backup/<dbname>/`，每天 03:00 由 `backup-db.timer` 触发。
- 备份保留 14 天，超期由 `backup-prune` 清理；**不要手工删除备份目录里的文件**，需要清理用 `clean_directory` 并经过审批。
- 备份失败会在 `/var/log/backup/error.log` 留下记录，值班需要当天处理。

## 日志与轮转
- 日志轮转配置在 `/etc/logrotate.d/`，默认保留 7 份、压缩旧文件。
- 访问日志暴涨通常是两个原因：被扫描（异常 UA/IP）或客户端重试风暴；先用 `grep` 统计再判断。
- 容器日志默认无上限，需要在 compose/daemon 层配置 `max-size`；排查磁盘满时优先看 `/var/lib/docker/containers/`。

## 变更与审批
- 生产环境的变更窗口是周二、周四 20:00-22:00。
- 重启服务前必须确认：没有正在跑的备份任务、没有正在进行的数据迁移。
- 所有高危操作（重启、清理、kill）都要走审批，并保留审计记录。

## 命名规范
- 主机名：`<角色>-<序号>`，例如 web-01、db-01、cache-01。
- 服务名与 systemd unit 名保持一致，避免出现 `myapp` 与 `my-app` 两种写法。
