# 一台「最小可被排查的服务器」：sshd + nginx + 常用排障命令。
FROM debian:12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-server nginx procps psmisc iproute2 curl less vim-tiny \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/sshd /var/log/nginx \
    && echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config \
    && echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 22 80
ENTRYPOINT ["/entrypoint.sh"]
