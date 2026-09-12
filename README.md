# sites-nav · 内部系统导航

公司内部系统登录入口一览页，管理员维护，其他用户登录后只读浏览。

- **公网地址 / 内网地址 / 域名** 一卡展示，一键复制
- 所有用户需登录；管理员可新增/编辑/删除，普通用户只读
- 站点数据落地单个 JSON 文件（备份即复制），用户与权限存 MySQL

## 架构

```
浏览器 ──HTTP──→ uvicorn(FastAPI) ──读写──→ data/sites.json   站点数据
                  │
                  ├─ GET  /               静态首页（无数据，需登录后才有内容）
                  ├─ GET  /admin          登录页（独立地址，建议收藏）
                  ├─ GET  /api/sites      列表（需登录 token）
                  ├─ GET  /api/users      用户列表（需管理员）
                  ├─ GET  /health         探活（会查 MySQL，不通时 503）
                  ├─ POST /api/login      登录换 token（默认 12h 有效）
                  └─ POST/PUT/DELETE       写操作（需管理员 token）
                        │
                        └──读写──→ MySQL users 表   用户与权限
```

- **后端**：FastAPI。站点数据用 JSON 文件（`tmp` + `os.replace` 原子写，带 `.bak` 轮转备份）；用户与权限用 MySQL
- **鉴权**：用户名 + 密码登录 → HMAC-SHA256 token，携带 `user_id` + `pwd_epoch`，默认 12h 有效。
  每个 API 请求都会回查 MySQL 里的 `enabled` 与 `pwd_epoch`，所以**禁用用户 / 改密后旧会话立即失效**，不只依赖 token 签名
- **前端**：单文件 `static/index.html` + 登录页 `static/admin.html`，无构建步骤

## 快速启动（Docker）

```bash
cd sites-nav
cp .env.example .env
# 编辑 .env：填 MYSQL_PASSWORD（bootstrap 会自动生成，也可自己写强密码）
docker compose up -d --build
```

浏览器访问 `http://<服务器IP>:8000`，登录入口在 **`/admin`**（首页故意不显示登录入口）。

> 服务器一键部署（拉代码 + 构建镜像 + 启动 + 探活，一条命令）见 [DEPLOY.md](DEPLOY.md)。

> **⚠️ 首次启动**：`ADMIN_PASSWORD` 只用于种子第一个 admin 账号，之后用户全部在后台「👤 用户管理」里维护。
> 留空则自动生成随机强密码并在启动日志打印**一次**（`docker compose logs sites-nav`），务必立即保存或改成自己的密码。
> 任何已知占位符（`PleaseChangeMe` / `changeme` / `password` / `admin` / `123456` 等）都视为未配置。

## 入口脚本

| 脚本 | 用途 | 绑定地址 |
|---|---|---|
| `scripts/bootstrap.sh` | 服务器一键部署（拉代码 + 构建 + 启动 + 探活） | 容器 `0.0.0.0:8000` |
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
| `CATEGRAF_ADMIN_URL` | 否 | 空 | categraf-http-admin 服务地址。留空 = 拨测联动整体关闭 |
| `CATEGRAF_ADMIN_USER` | 否 | `admin` | 拨测管理端登录用户名 |
| `CATEGRAF_ADMIN_PASS` | 否 | 空 | 拨测管理端登录密码；与 URL 同时配置才启用联动 |

## 端口

默认 `8000`，改 `docker-compose.yml` 的 `ports` 即可。

## 数据与备份

站点数据和用户数据分两处，备份方式不同：

```bash
# 站点数据（单文件）
tar czf /backup/sites-nav-sites-$(date +\%F).tgz -C /opt/sites-nav data

# 用户与权限（MySQL）
docker compose exec -T mysql sh -c 'exec mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines sites_nav' \
  > /backup/sites-nav-users-$(date +\%F).sql
```

- 站点数据：`./data/sites.json`，损坏时自动回退 `sites.json.bak`（保留 3 份轮转），恢复结果会在 `/health` 的 `data_warning` 里提示
- 用户数据：MySQL `users` 表，容器卷 `mysql_data`。启动时自动建表（`CREATE TABLE IF NOT EXISTS`），无需手动建表
- 持久化：`docker-compose.yml` 把 `./data` 挂载进容器、MySQL 数据放命名卷，重建容器数据不丢

## 管理员使用

1. 访问独立登录地址 **`/admin`**（首页不显示任何登录入口，建议收藏该地址），输入用户名 + 密码
2. 登录后自动跳回首页，出现「＋ 新增网站」「批量导入」「导出」「👤 用户管理」及卡片上的 加入监控/编辑/删除 按钮
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

- 点「加入监控」→ 填 **期望状态码**（3 位数字，多个用 `|` 分隔如 `200|301`，留空默认 `200`）和 **超时时长**（如 `3s` / `500ms` / `1m`，留空不设置）→ 确认后自动注册到 categraf 拨测
- 两个参数保存在站点记录中，编辑弹窗勾选「拨测监控」后可查看和修改，保存后自动同步到拨测端

## 普通用户

需要登录才能看（数据全部走带鉴权的 API，页面本身不含任何数据）。

1. 管理员在「👤 用户管理」里为研发建账号（角色选「普通用户」），把用户名密码发给对方
2. 对方访问 `/admin` 登录，即可搜索/浏览/复制地址
3. 权限不足只读：新增/编辑/删除/导入/导出按钮不显示，直接调接口也会被拒 403
4. 人离职时点「禁用」即可，旧会话立刻失效，不用删账号

## 拨测联动（categraf 自动拨测）

新增/编辑系统时勾选「加入 categraf 自动拨测（域名优先探测）」，保存后自动把该系统注册到 categraf-http-admin，由其下发 categraf `http_response` 插件配置：

- 探测地址优先级：**域名 > 公网地址 > 内网地址**（无 scheme 时补 `http://`）
- 拨测 job 名 = 系统名称 + 环境后缀（`-生产环境` / `-测试环境`）
- 取消勾选或删除系统时自动同步移除拨测目标
- 同步为后台异步执行，结果在卡片 📡 徽标与 `/api/monitor-status` 查看（内存态，重启清零）

需先配置 `CATEGRAF_ADMIN_URL` 与 `CATEGRAF_ADMIN_PASS`（见环境变量表），否则联动关闭。

## 升级

```bash
git pull          # 或直接替换代码
docker compose build
docker compose up -d
```

## 本地开发（非 Docker）

```bash
pip install -r requirements.txt
# 先起一个 MySQL：用 compose 内置的，或把 .env 的 MYSQL_* 指向已有实例
docker compose up -d mysql
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
