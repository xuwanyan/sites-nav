# 部署指南

服务器上一条命令拉代码 + 构建镜像 + 启动 + 探活，可重复执行。

## 一键部署

```bash
curl -fsSL https://raw.githubusercontent.com/xuwanyan/sites-nav/main/scripts/bootstrap.sh -o /tmp/bootstrap.sh
sudo bash /tmp/bootstrap.sh
```

首次和以后每次部署都是这一条。脚本幂等：已克隆就 `git fetch` 更新，没有就 `git clone`；`.env` 和 `data/` 被 gitignore，git 操作不会动到它们。

默认装到 `/opt/sites-nav`，监听 `8000`。

## 自定义

参数用位置传入（**不要用 `APP_DIR=x sudo bash`，sudo 会过滤环境变量，值会丢失**）：

```bash
sudo bash /tmp/bootstrap.sh /data/sites-nav          # 自定义安装目录
sudo bash /tmp/bootstrap.sh /data/sites-nav v1.2     # 自定义分支或标签
```

或用环境变量（必须 `sudo -E`）：

```bash
export APP_DIR=/data/sites-nav && sudo -E bash /tmp/bootstrap.sh
```

### 端口

改宿主机映射端口：编辑 `<安装目录>/.env`，加一行

```
PORT=8080
```

容器内固定 8000（Dockerfile 的 CMD 绑 8000），改的是宿主机映射。compose 的 `${PORT:-8000}:8000`、`deploy.sh` 的端口检查、`bootstrap.sh` 的端口冲突判断三边都从同一处读，不会出现"检查的端口和实际绑定的端口不一致"。

### 自定义目录的两个坑

1. **父目录要先存在。** `git clone` 不会建多级父目录，`/data` 不存在会直接失败。`/opt` 一般都有，用默认最省事。
2. **换目录要迁数据。** compose 的卷是相对路径 `./data:/app/data`，换目录等于换了宿主机卷路径。而两个目录名都叫 `sites-nav` → compose 算出相同项目名 → `docker compose down` 在哪个目录执行都会找到同一个容器。所以换目录的完整流程：

   ```bash
   mkdir -p /root/sites-nav/data
   cp -a /opt/sites-nav/.env /root/sites-nav/.env        # 密码
   cp -a /opt/sites-nav/data/. /root/sites-nav/data/     # 数据
   cd /opt/sites-nav && docker compose down               # 停旧容器，腾端口
   sudo bash /tmp/bootstrap.sh /root/sites-nav
   rm -rf /opt/sites-nav                                  # 新环境正常后再删
   ```

   不迁 `.env` 会在新目录重新生成一个随机密码，旧密码就孤立在旧目录了。

`/root` 作为服务目录不理想（占 root 家目录、分区常较小、不便于备份脚本按路径匹配），`/opt` 或 `/data` 更常规。

## 服务器前提

- Docker + Compose V2（`docker compose version` 能跑通）
- `git`、`curl`、`openssl`、`ss`（`ss` 在 iproute 包里，CentOS 7+ 默认有）
- 老版本 git（< 2.11，CentOS 7 / Alinux 常见）也能用：脚本不用 `git -C`，全部 `(cd dir && git ...)`

脚本会检查这些，缺了直接报清楚缺哪个。

## 首次部署后会自动处理的两件事

1. **脚本执行位**：从 Windows 提交的 `.sh` mode 是 `100644`，脚本内 `chmod +x *.sh` 补上
2. **`data/` 目录归属**：容器 `read_only` + 非 root，唯一可写位置是 `./data`。脚本从镜像解析运行用户 UID 后 `chown`，避免"页面能看、一点新增就 500"的 PermissionError

`.env` 缺失或 `ADMIN_PASSWORD` 为空/占位符时，脚本用 `openssl rand -hex 24` 生成强密码，并在终端打印**一次**。

## 升级

```bash
sudo bash /tmp/bootstrap.sh          # 重新拉最新代码 + 重建镜像 + 重启
```

或手动：

```bash
cd /opt/sites-nav
git fetch origin main && git checkout -B main origin/main
docker compose build
docker compose up -d
```

## 备份

单文件存储，备份就是复制文件：

```
15 3 * * * tar czf /backup/sites-nav-$(date +\%F).tgz -C /opt/sites-nav data && find /backup -name 'sites-nav-*.tgz' -mtime +30 -delete
```

## 反向代理 + HTTPS

应用本身不带 TLS。前面套 nginx 时，容器已内置 `--proxy-headers --forwarded-allow-ips 127.0.0.1`（Dockerfile CMD），所以 uvicorn 会正确读取 `X-Forwarded-For`，登录限流按真实客户端 IP 计数，不会"一个人输错 10 次锁全公司"。

```nginx
server {
    listen 443 ssl http2;
    server_name nav.yourcompany.com;
    ssl_certificate     /etc/nginx/certs/nav.pem;
    ssl_certificate_key /etc/nginx/certs/nav.key;

    allow 10.0.0.0/8;          # 内网白名单，可选但建议加
    allow 192.168.0.0/16;
    deny all;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

建议把 compose 的端口绑定改成只听本地：`docker-compose.yml` 里 `"${PORT:-8000}:8000"` → `"127.0.0.1:${PORT:-8000}:8000"`，让流量只走 nginx。

## 单实例红线

**必须单进程单实例**：不要 `--workers N`、不要 `replicas > 1`、不要多容器。

原因：`TOKEN_SECRET` 是进程内生成的（`app.py` 顶层 `secrets.token_hex(32)`），多进程各自一个 secret → 刚登录的 token 会被随机打到不认它的进程上，表现为"刷新一下登录就掉了"。`_login_attempts`、`_sync_status` 也都是进程内 dict，多进程间不共享。

单用户运维场景，单进程完全够用。

## 常见故障

| 症状 | 原因 | 处理 |
|---|---|---|
| `docker pull` 卡住/超时 | Docker Hub 国内拉不动 | 配 Docker 镜像加速器（`/etc/docker/daemon.json` 的 `registry-mirrors`），或 `docker pull python:3.12.7-slim` 后再 build |
| 页面能看、点新增就 500 | `data/` 目录归属不对 | `docker compose exec sites-nav id` 看 uid:gid，`chown -R <uid>:<gid> data` |
| 看不到「加入监控/编辑/删除」按钮 | 没登录 | 访问 `/admin` 登录（首页故意不显示登录入口） |
| 改了 app.py 不生效 | 没开 reload 或缓存 | 容器重建即 `docker compose up -d --build`；本地开发用 `python run.py`（带 `--reload`） |
| token 频繁失效 | 重启/重载轮换了 `TOKEN_SECRET` | 正常现象（安全设计），重启后去 `/admin` 重新登录 |

## 本地开发（非 Docker）

```bash
pip install -r requirements.txt
python run.py          # 带 --reload，改 app.py 自动重载
```

`run.py` 已加 `if __name__ == '__main__'` 保护，Windows 上 `multiprocessing` 用 spawn 不会崩。

> 注意：每次热重载都会轮换 `TOKEN_SECRET`，保存 `app.py` 后需要重新登录。
