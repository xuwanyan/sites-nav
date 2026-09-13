# 部署指南

服务器上一条命令拉代码 + 构建镜像 + 启动 + 探活，可重复执行。

## 先选一种：MySQL 放哪

| 你的情况 | 用哪种 | 看哪节 |
|---|---|---|
| 服务器上没装 MySQL，想省事 | **内置**（compose 自带 mysql 容器，默认配置） | [一键部署](#一键部署) |
| 服务器上已有 MySQL，不想再加一个容器 | **已有实例** | [用已有 MySQL](#用已有-mysql) |
| 已有 MySQL 在另一台机器 | **已有实例** | 同上，`MYSQL_HOST` 填那台机器的地址 |

应用代码两种情况完全一样，**差别只在 `.env` 的 `MYSQL_HOST` 一个变量**。`docker-compose.yml` 两种情况都不用改——内置 mysql 服务挂在 `profiles` 上，部署脚本会按 `.env` 自动激活或跳过。

切换两种部署方式只改 `.env` 后重跑 `deploy.sh` 即可，数据在两边各存一份，不会丢。建议先用内置跑通，确认没问题后再决定是否并入已有库。

> **从内置切到已有 MySQL 后**，旧的 mysql 容器不再属于当前 compose 配置。`./deploy.sh --stop` 带 `--remove-orphans` 会把它一起停掉；但 `mysql_data` 卷不会被删（没带 `-v`），之后想切回内置，数据还在。

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

   MySQL 数据在命名卷 `mysql_data`，不跟目录走但**也不用迁**：两个目录名都叫 `sites-nav`，compose 算出的项目名相同，新目录会直接连上同一个卷，用户数据原地可用。

`/root` 作为服务目录不理想（占 root 家目录、分区常较小、不便于备份脚本按路径匹配），`/opt` 或 `/data` 更常规。

## 用已有 MySQL

服务器上已有 MySQL 且不想再跑一个 mysql 容器时走这条路。三步。

### 1. 建库建账号

```sql
CREATE DATABASE IF NOT EXISTS sites_nav
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE USER 'sites_nav'@'%' IDENTIFIED BY '<随机强密码>';

-- 最小权限：应用只需要这 5 个。CREATE 是必需的，启动时自动建 users 表
GRANT CREATE, SELECT, INSERT, UPDATE, DELETE ON sites_nav.* TO 'sites_nav'@'%';

FLUSH PRIVILEGES;
```

**库必须你先建，表不用。** 应用启动会自动 `CREATE TABLE IF NOT EXISTS users`，但库不存在会直接报 `1049 Unknown database` 并中止启动。

### 2. 改 `.env`

```
MYSQL_HOST=<见下方说明>
MYSQL_PORT=3306
MYSQL_USER=sites_nav
MYSQL_PASSWORD=<刚设的强密码>
MYSQL_DATABASE=sites_nav
```

**`MYSQL_HOST` 填什么**（最容易错的一步）：

| MySQL 在哪 | 填 |
|---|---|
| 和 sites-nav 同一台机器，装在宿主机上 | `host.docker.internal` |
| 另一台机器 | 那台的内网 IP / 域名 |
| 同 compose 里的另一个服务 | 那个服务的 `name` |

**别填 `127.0.0.1`**：sites-nav 跑在容器里，容器自己的 loopback 不是你宿主机的 MySQL。

> `host.docker.internal` 需要 Docker 20.10+。compose 里已配 `extra_hosts: host.docker.internal:host-gateway`
> 提供这个解析，但 `host-gateway` 这个关键字要 20.10 才支持。CentOS 7 常见的 Docker 19.03
> 会直接报 `unsupported host value`，把那行改成 `host.docker.internal:172.17.0.1`（默认 bridge 网关）。
> 查版本：`docker version --format '{{.Server.Version}}'`。

MySQL 侧要确认：`bind-address` 放得进容器过来的连接（不能只绑 `127.0.0.1`），用户授权的 host（`'%'` 或具体网段）包含容器出口 IP。

> `MYSQL_HOST` 不是 `mysql` 时，`bootstrap.sh` / `deploy.sh` **不会**生成 `MYSQL_PASSWORD`——只打印一行提示让你自己填。这是故意的：代填等于拿随机密码去连你的库。

### 3. `docker-compose.yml` 不用改

内置 mysql 服务挂在 `profiles: ["builtin-mysql"]` 上，`deploy.sh` / `bootstrap.sh` 会读 `.env` 的 `MYSQL_HOST` 自动决定是否激活（`export COMPOSE_PROFILES`）。**两种部署方式切换只改 `.env` 一个文件。**

所以用已有 MySQL 的完整改动就是第 1、2 步：MySQL 里建库建账号 + `.env` 填真实值。`deploy.sh` 照常跑。

### 验证

```bash
cd /opt/sites-nav
docker compose up -d
docker compose logs -f sites-nav   # 应出现 [seed] 已从 ADMIN_PASSWORD 创建 admin 账号
curl http://127.0.0.1:8000/health  # {"ok":true}；MySQL 不通会返回 503
```

启动失败时 `logs` 里有 `[fatal] 无法连接 MySQL <host>:<port>/<db>` 加错误码：

| 错误码 | 含义 |
|---|---|
| `1049` | 库没建（第 1 步漏了） |
| `1045` | 账号或密码错，或授权 host 不匹配 |
| `1044` | 权限不够（缺 `CREATE` 之类） |
| `2003` | 网络不通：`MYSQL_HOST` 填错、防火墙、`bind-address` |

### 备份命令的差异

外部 MySQL 不能用 `docker compose exec mysql ...`，改用下面任一种：

```bash
# 方式 A：宿主机装 mysql 客户端
apt install -y mysql-client        # 或 yum install -y mysql
mysqldump -h10.0.1.5 -usites_nav -p sites_nav --single-transaction --routines \
  > /backup/sites-nav-users-$(date +\%F).sql

# 方式 B：临时拉 mysql 镜像跑，宿主机不用装客户端
docker run --rm -e MYSQL_PWD='<密码>' mysql:8.0 \
  mysqldump -h10.0.1.5 -usites_nav --single-transaction --routines sites_nav \
  > /backup/sites-nav-users-$(date +\%F).sql
```

站点数据的备份命令不受影响（仍是复制 `data/`）。

## 服务器前提

- Docker + Compose V2（`docker compose version` 能跑通）
- `git`、`curl`、`openssl`、`ss`（`ss` 在 iproute 包里，CentOS 7+ 默认有）
- 老版本 git（< 2.11，CentOS 7 / Alinux 常见）也能用：脚本不用 `git -C`，全部 `(cd dir && git ...)`

脚本会检查这些，缺了直接报清楚缺哪个。

## 首次部署后会自动处理的三件事

1. **脚本执行位**：从 Windows 提交的 `.sh` mode 是 `100644`，脚本内 `chmod +x *.sh` 补上
2. **`data/` 目录归属**：容器 `read_only` + 非 root，唯一可写位置是 `./data`。脚本从镜像解析运行用户 UID 后 `chown`，避免"页面能看、一点新增就 500"的 PermissionError

3. **MySQL 密码**：`MYSQL_HOST` 指向 compose 内置的 `mysql` 服务时，`MYSQL_PASSWORD` 和 `MYSQL_ROOT_PASSWORD` 缺失会用 `openssl rand -hex 24` 生成并写入 `.env`；指向外部实例时**不会代填**（代填会让应用拿随机密码去连你的库）

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

数据分两处：站点数据是单文件，用户与权限在 MySQL。

```bash
# 站点数据：复制文件即可
tar czf /backup/sites-nav-sites-$(date +\%F).tgz -C /opt/sites-nav data

# 用户与权限：mysqldump（走内置 mysql 时注意 --profile，见下）
cd /opt/sites-nav && COMPOSE_PROFILES=builtin-mysql docker compose exec -T mysql sh -c \
  'exec mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines sites_nav' \
  > /backup/sites-nav-users-$(date +\%F).sql
```

cron 合起来一条：

```
15 3 * * * cd /opt/sites-nav && tar czf /backup/sites-nav-sites-$(date +\%F).tgz data && COMPOSE_PROFILES=builtin-mysql docker compose exec -T mysql sh -c 'exec mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines sites_nav' > /backup/sites-nav-users-$(date +\%F).sql && find /backup -name 'sites-nav-*' -mtime +30 -delete
```

> **`--profile` 只在走内置 mysql 时需要。** mysql 服务挂在 `profiles` 上，手动跑 `docker compose exec mysql ...` 时 compose 看不到它，会报 "no such service"。部署脚本内部会自动 `export COMPOSE_PROFILES`，但你手敲命令或在 cron 里得自己带上。
>
> 用已有 MySQL 时不要跑上面这条——它连的是那个空容器库。改成对真实实例跑 mysqldump（见 [用已有 MySQL → 备份命令的差异](#备份命令的差异)）。

- MySQL 数据在命名卷 `mysql_data`，`docker compose down`（不带 `-v`）不会删它；要彻底清库才需要 `docker volume rm`
- 应用启动会自动建表（`CREATE TABLE IF NOT EXISTS`），空库也能直接起
- 站点数据损坏时自动回退 `sites.json.bak`（保留 3 份轮转），恢复结果会在 `/health` 的 `data_warning` 字段里提示

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
| 全站 503「用户服务暂不可用」 | MySQL 连不上 | `docker compose ps` 看 mysql 是否 healthy，`docker compose logs mysql` 查原因。**恢复后应用自动可用，不用重启**，已登录用户的会话也不丢 |
| 应用起不来：无法连接 MySQL | `.env` 的 `MYSQL_*` 填错，或 mysql 服务没起 | 走内置：`COMPOSE_PROFILES=builtin-mysql docker compose up -d mysql`；核对 `MYSQL_HOST`（走内置填 `mysql`）/ `MYSQL_PASSWORD` |
| 应用起不来：无法连接 MySQL | 用了已有实例但库/账号/网络不对 | `docker compose logs sites-nav` 看 `[fatal]` 行的错误码：`1049` 库没建、`1045` 账号密码或授权 host、`1044` 权限不够、`2003` 网络不通 |
| mysql 容器起了但 `root password ... is not set` | `.env` 里 `MYSQL_ROOT_PASSWORD` 是空 | 走 `deploy.sh` / `bootstrap.sh` 会自动生成并写进 `.env`；绕过脚本手敲 `docker compose up` 就会遇到 |
| 手动 `docker compose exec mysql ...` 报 no such service | mysql 挂在 profile 上，未激活时 compose 看不到 | 命令前加 `COMPOSE_PROFILES=builtin-mysql` |
| `docker compose ps` 里没有 mysql 但它还在跑 | 切到已有 MySQL 后 mysql 已不在当前配置里，`down` 不带 `--remove-orphans` 不会停它 | `./deploy.sh --stop`（已带该参数），或 `docker compose down --remove-orphans` |
| `2003` 且 `MYSQL_HOST=host.docker.internal`，日志里是 `Name or service not known` | Linux 上 Docker 不注入该主机名，或版本低于 20.10 不支持 `host-gateway` | `docker version --format '{{.Server.Version}}'`；低于 20.10 把 compose 的 extra_hosts 改成 `host.docker.internal:172.17.0.1` |
| `docker compose up` 直接报 `unsupported host value host-gateway` | Docker 版本低于 20.10（CentOS 7 的 19.03 常见） | 同上，改成 `host.docker.internal:172.17.0.1`；或升级 Docker |
| 登录 429 尝试次数过多 | 同一 IP 15 分钟内失败 10 次 | 等 15 分钟；反向代理后记得配 `X-Forwarded-For`，否则全公司共享一个计数 |

## 本地开发（非 Docker）

```bash
pip install -r requirements.txt
COMPOSE_PROFILES=builtin-mysql docker compose up -d mysql   # 起一个 MySQL 给应用连
python run.py                                                # 带 --reload，改 app.py 自动重载
```

`run.py` 已加 `if __name__ == '__main__'` 保护，Windows 上 `multiprocessing` 用 spawn 不会崩。

> 注意：每次热重载都会轮换 `TOKEN_SECRET`，保存 `app.py` 后需要重新登录。

### 本机没有 Docker 怎么办

应用启动强依赖 MySQL（连不上直接中止，这是刻意的），所以需要一个真数据库。两个办法：

1. **装 Docker Desktop**（推荐）。生产就是 `docker compose` 跑的，本地起同一个 mysql 容器最接近真实环境，认证插件也一致（MySQL 8 默认 `caching_sha2_password`，`cryptography` 依赖就是为此加的）。
2. **本机裸装 MySQL/MariaDB**，把 `.env` 的 `MYSQL_*` 指向 `127.0.0.1`。省事但版本和认证插件可能和生产不同，`caching_sha2_password` 那条路径测不到。

注意：**本地填的 `.env` 别拷到服务器**。`MYSQL_HOST=127.0.0.1` 会让 `bootstrap.sh` 判定为"外部实例"而不生成密码，服务器上那份 `.env` 是独立的、由 bootstrap 从 `.env.example` 新建。
