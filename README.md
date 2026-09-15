# sites-nav · 内部系统导航

公司内部系统登录入口一览页，管理员维护，其他用户登录后只读浏览。**同时作为 categraf 的 http_provider**，直接下发 `http_response` 和 `net_response` 两类拨测配置。

- **公网地址 / 内网地址 / 域名** 一卡展示，一键复制
- 所有用户需登录；管理员可新增/编辑/删除，普通用户只读
- 站点数据落地单个 JSON 文件（备份即复制），用户与权限存 MySQL
- **HTTP 拨测**：站点勾选「加入监控」即自动成为 categraf `http_response` 拨测目标
- **端口拨测**：独立管理页面 `/probes`，支持 TCP/UDP 连通性 + send/expect 校验
- categraf 通过 `http_provider` 从本服务拉取两类拨测的 TOML 配置，categraf 侧零配置改动

## 架构

```
浏览器 ──HTTP──→ uvicorn(FastAPI) ──读写──→ data/sites.json    站点数据
                  │                   └──读写──→ data/probes.json   端口拨测目标
                  ├─ GET  /               静态首页（无数据，需登录后才有内容）
                  ├─ GET  /admin          登录页（独立地址，建议收藏）
                  ├─ GET  /probes         端口拨测管理页（需管理员）
                  ├─ GET  /api/sites      列表（需登录 token）
                  ├─ GET  /api/probes     端口拨测列表（需管理员）
                  ├─ GET  /api/users      用户列表（需管理员）
                  ├─ GET  /api/config/http_response  categraf 配置拉取端点（Bearer token）
                  ├─ GET  /health         探活（会查 MySQL，不通时 503）
                  ├─ POST /api/login      登录换 token（默认 12h 有效）
                  └─ POST/PUT/DELETE       写操作（需管理员 token）
                        │
                        └──读写──→ MySQL users 表   用户与权限

categraf ──http_provider──→ GET /api/config/http_response
        （一次拉取同时返回 http_response 与 net_response 两份 TOML）
```

- **后端**：FastAPI。站点数据用 JSON 文件（`tmp` + `os.replace` 原子写，带 `.bak` 轮转备份）；用户与权限用 MySQL
- **鉴权**：用户名 + 密码登录 → HMAC-SHA256 token，携带 `user_id` + `pwd_epoch`，默认 12h 有效。
  每个 API 请求都会回查 MySQL 里的 `enabled` 与 `pwd_epoch`，所以**禁用用户 / 改密后旧会话立即失效**，不只依赖 token 签名
- **前端**：单文件 `static/index.html` + 登录页 `static/admin.html` + 拨测管理页 `static/probes.html`，无构建步骤
- **categraf http_provider**：`/api/config/http_response` 端点，Bearer token 认证（`CATEGRAF_TOKEN` 环境变量），返回 `version`（内容 MD5）+ 两份 TOML

## 快速启动（Docker）

```bash
cd sites-nav
cp .env.example .env
# 编辑 .env：填 MYSQL_PASSWORD（bootstrap 会自动生成，也可自己写强密码）
# 如需 categraf 拉取认证：生成 CATEGRAF_TOKEN
docker compose up -d --build
```

浏览器访问 `http://<服务器IP>:8000`，登录入口在 **`/admin`**（首页故意不显示登录入口）。

> 服务器部署见 [DEPLOY.md](DEPLOY.md)：`bootstrap.sh` 拉代码 + 构建 + 配 `.env`（默认不启动），确认 `.env` 后 `./deploy.sh --deploy` 启动 + 探活。

> **⚠️ 首次启动**：`ADMIN_PASSWORD` 只用于种子第一个 admin 账号，之后用户全部在后台「👤 用户管理」里维护。
> 留空则自动生成随机强密码并在启动日志打印**一次**（`docker compose logs sites-nav`），务必立即保存或改成自己的密码。
> 任何已知占位符（`PleaseChangeMe` / `changeme` / `password` / `admin` / `123456` 等）都视为未配置。

## 入口脚本

| 脚本 | 用途 | 绑定地址 |
|---|---|---|
| `scripts/bootstrap.sh` | 服务器准备（拉代码 + 构建 + 配 `.env`），默认不启动；加 `--deploy` 接着启动 | 容器 `0.0.0.0:8000` |
| `deploy.sh` | 本地已克隆后的运维（部署 / 更新 / 停止 / 状态） | 容器 `0.0.0.0:8000` |
| `start.bat` | Windows 本地开发（调 `run.py`，热重载） | `127.0.0.1:8000` |
| `run.py` | 跨平台本地开发（带 `--reload` 热重载） | `127.0.0.1:8000`（可 `HOST=0.0.0.0` 覆盖） |

> **⚠️ 不要带默认密码部署到服务器**：`.env.example` 里的占位符都已被后端识别为未配置，但自己随手写个弱密码照样能登录。生产环境必须用强密码，并尽快在后台把 admin 密码改成自己的。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `MYSQL_HOST` | **是** | 空 | MySQL 地址。走 compose 内置服务填 `mysql`；用已有实例填实际地址 |
| `MYSQL_PORT` | 否 | `3306` | MySQL 端口 |
| `MYSQL_USER` | 否 | `sites_nav` | 应用账号 |
| `MYSQL_PASSWORD` | **是** | 空 | 应用账号密码（bootstrap 会自动生成） |
| `MYSQL_DATABASE` | 否 | `sites_nav` | 库名 |
| `MYSQL_ROOT_PASSWORD` | 内置时是 | 空 | 内置 mysql 的 root 密码，**仅数据卷首次初始化生效** |
| `ADMIN_PASSWORD` | 否 | 空 | 仅用于首次启动种子 admin。留空自动生成随机密码并打印一次 |
| `TOKEN_TTL_HOURS` | 否 | `12` | 会话有效期（小时），超时需重新登录 |
| `CATEGRAF_TOKEN` | 否 | 空 | categraf 拉取配置用的 Bearer token。不设则 `/api/config/http_response` 公开（建议配置） |

**用已有 MySQL（服务器上已装）时**：`.env` 里把 `MYSQL_HOST` 改成实际地址，**`docker-compose.yml` 不用动**——内置 mysql 服务挂在 `profiles` 上，部署脚本会按 `.env` 自动决定是否激活。改完重跑 `deploy.sh` 即可。建库建账号 SQL、`MYSQL_HOST` 的取值、备份命令差异见 [DEPLOY.md → 用已有 MySQL](DEPLOY.md#用已有-mysql)。

> 容易踩的坑：MySQL 装在跑 Docker 的同一台宿主机上时，`MYSQL_HOST` 填 `host.docker.internal`，**不是** `127.0.0.1`（容器里的 loopback 不是你宿主机的 MySQL）。

## 端口

默认 `8000`，改 `docker-compose.yml` 的 `ports` 即可。

## 数据与备份

站点数据、端口拨测目标和用户数据分三处，备份方式不同：

```bash
# 站点数据 + 端口拨测目标（两个 JSON 文件）
tar czf /backup/sites-nav-data-$(date +\%F).tgz -C /opt/sites-nav data

# 用户与权限（MySQL）
COMPOSE_PROFILES=builtin-mysql docker compose exec -T mysql sh -c \
  'exec mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines sites_nav' \
  > /backup/sites-nav-users-$(date +\%F).sql
```

- 站点数据：`./data/sites.json`，损坏时自动回退 `sites.json.bak`（保留 3 份轮转），恢复结果会在 `/health` 的 `data_warning` 里提示
- 端口拨测目标：`./data/probes.json`，带 `.bak` 备份
- 用户数据：MySQL `users` 表，容器卷 `mysql_data`。启动时自动建表（`CREATE TABLE IF NOT EXISTS`），无需手动建表
- 持久化：`docker-compose.yml` 把 `./data` 挂载进容器、MySQL 数据放命名卷，重建容器数据不丢

## 管理员使用

1. 访问独立登录地址 **`/admin`**（首页不显示任何登录入口，建议收藏该地址），输入用户名 + 密码
2. 登录后自动跳回首页，出现「＋ 新增网站」「批量导入」「导出」「📡 拨测管理」「👤 用户管理」及卡片上的 加入监控/编辑/删除 按钮
3. token 默认 12h 失效；**容器重启后需重新登录**（`TOKEN_SECRET` 进程内生成，重启即轮换，属安全特性）

### 用户管理

「👤 用户管理」里可以新建用户（用户名 + 强密码 + 角色）、启用/禁用、改密码、删除。

- **强密码**：≥10 位，须同时含字母和数字，≤72 字节（bcrypt 的硬上限，超出部分会被静默丢弃）
- **禁用用户** → 该用户旧会话立即失效且无法再登录；启用后旧会话恢复
- **改密码** → 该用户所有旧会话立即失效（`pwd_epoch` 在 SQL 里原子自增）
- 禁止删除自己、禁止降级或删除最后一名管理员
- 用户名 1-32 位，允许中文/字母/数字/`_ . @ -`

### 拨测监控快捷操作

运维模式下每张卡片有「加入监控 / 取消监控」快捷按钮，无需打开编辑弹窗：

- 点「加入监控」→ 填 **期望状态码**（3 位数字，多个用 `|` 分隔如 `200|301`，留空默认 `200`）和 **超时时长**（如 `3s` / `500ms` / `1m`，留空不设置）→ 确认后自动成为 categraf `http_response` 拨测目标
- 两个参数保存在站点记录中，编辑弹窗勾选「拨测监控」后可查看和修改
- **写站点即生效**：拨测目标从站点数据实时生成，categraf 下次拉取时自动包含，无需额外同步

## 普通用户

需要登录才能看（数据全部走带鉴权的 API，页面本身不含任何数据）。

1. 管理员在「👤 用户管理」里为研发建账号（角色选「普通用户」），把用户名密码发给对方
2. 对方访问 `/admin` 登录，即可搜索/浏览/复制地址
3. 权限不足只读：新增/编辑/删除/导入/导出按钮不显示，直接调接口也会被拒 403
4. 人离职时点「禁用」即可，旧会话立刻失效，不用删账号

## categraf 拨测配置

sites-nav 直接作为 categraf 的 `http_provider`，同时下发两类拨测配置：

### HTTP 拨测（http_response）

站点勾选「加入监控」即自动成为拨测目标：

- 探测地址优先级：**域名 > 公网地址 > 内网地址**（无 scheme 时补 `http://`）
- 拨测 job 名 = 系统名称 + 环境后缀（`-生产环境` / `-测试环境`）
- 期望状态码：默认 `200`，支持 `200|301` 多值
- 超时时长：留空不设置（categraf 用默认值）
- 取消勾选或删除系统时，拨测目标自动消失（下次 categraf 拉取时不再包含）

### 端口拨测（net_response）

访问 `/probes` 页面在线管理 TCP/UDP 端口拨测目标：

| 字段 | 说明 |
|------|------|
| 目标地址 | `host:port` 格式，如 `10.0.0.1:22` |
| 协议 | TCP / UDP |
| 名称 | 用于 `[mappings]` 中的 job 标签 |
| 连接超时 | 对应 `timeout`，空则用 categraf 默认 1s |
| 发送内容 (send) | 建连后发送的字符串，支持 `\r` `\n` `\t` 转义 |
| 期望响应包含 (expect) | 响应中需包含的字符串，不填则仅检测连通 |
| 读超时 (read_timeout) | 配合 send/expect 的读超时，空则用默认 3s |

> ⚠️ UDP 是无连接协议，不配 send/expect 时"发包不报错即算成功"，判活不可靠；UDP 目标请务必配置 send/expect。

**告警建议**：net_response 用 `result_code != 0` 告警（0=成功 1=超时 2=连接失败 3=读失败 4=expect 不匹配）。

### categraf 配置

在 categraf `conf/config.toml` 中修改：

```toml
providers = ["local", "http"]

[http_provider]
remote_url = "http://你的服务器IP:8000/api/config/http_response"
headers = ["Authorization", "Bearer <你的CATEGRAF_TOKEN>"]
timeout = 5
reload_interval = 60
```

> `headers` 走 `Authorization: Bearer <token>` 请求头，token 不进 URL / 日志。`<你的CATEGRAF_TOKEN>` 填 `.env` 里 `CATEGRAF_TOKEN` 的值。若你的 categraf 版本不支持 `http_provider.headers`，需升级或保留该端点公开。

然后重启 categraf：

```bash
systemctl restart categraf
```

> ⚠️ 迁移提醒：目标交给 sites-nav 管理后，删掉本地 `conf/input.http_response/`、`conf/input.net_response/` 中的同名目标，否则 local 与 http 两个 provider 会叠加，同一目标被拨测两遍。

### TOML 合并规则

配置画像相同的目标自动合并到同一个 `[[instances]]`，`job` 名称不参与分组（放在 `[mappings]` 里逐目标打标）：

- **http_response 画像**：方法 + 状态码 + 超时 + Body + 请求头 + TLS
- **net_response 画像**：协议 + 连接超时 + read_timeout + send + expect

默认值不落盘：`protocol = "tcp"`、`method = "GET"` 等与 categraf 默认行为一致的配置会自动省略。`expect_response_status_codes` 始终显式落盘（categraf 不配时不做任何状态码检查）。

### 配置版本

`version` 是全部目标内容的 MD5 哈希（现场计算、不落盘），内容不变则 version 不变，categraf 不会误重启采集实例。

## 升级

```bash
git pull                        # 或直接替换代码
docker compose build
docker compose up -d --force-recreate
```

> `--force-recreate` 不能省。`.env` 的改动不一定被 `up -d` 当成配置变化而重建容器，
> 而变量是进程启动时一次性读入的 —— 容器不重建就永远是旧值。
> 也等价于 `./deploy.sh --deploy` / `--update`（都已内置）。

## 本地开发（非 Docker）

```bash
pip install -r requirements.txt
# 先起一个 MySQL：用 compose 内置的，或把 .env 的 MYSQL_* 指向已有实例
COMPOSE_PROFILES=builtin-mysql docker compose up -d mysql
python run.py          # 带 --reload，改 app.py 自动重载
```

应用启动会连 MySQL 建表并（表为空时）种子 admin。连不上会直接中止启动并提示缺哪个配置。

> 注意：每次热重载都会轮换 `TOKEN_SECRET`，保存 `app.py` 后需要重新登录。

## 从旧版 users.json 迁移

用户与权限从 JSON 迁到 MySQL 时，用一次性脚本导入（bcrypt 哈希原样搬运，**密码不用重设**）：

```bash
python scripts/migrate_users.py data/users.json --dry-run    # 先看会做什么
python scripts/migrate_users.py data/users.json              # 正式导入
```

## 从 categraf-http-admin 迁移

如果之前用 categraf-http-admin 管理拨测目标：

1. 在 sites-nav 的 `.env` 中配置 `CATEGRAF_TOKEN`（可与原 admin 的 token 相同）
2. 端口拨测目标：从原 admin 的 `targets.json` 中提取 `kind=net` 的记录，在 `/probes` 页面逐个添加
3. HTTP 拨测目标：在 sites-nav 的站点卡片上勾选「加入监控」，参数与原 admin 中一致
4. 更新 categraf `config.toml` 的 `remote_url` 指向 sites-nav 的 `/api/config/http_response`
5. 删除本地 `conf/input.http_response/`、`conf/input.net_response/` 中的同名目标
6. 重启 categraf，停掉 categraf-http-admin 服务
