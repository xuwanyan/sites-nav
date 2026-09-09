# sites-nav · 内部系统导航

公司内部系统登录入口一览页，运维统一维护，开发只读浏览。

- **公网地址 / 内网地址 / 域名** 一卡展示，一键复制
- 运维登录后可新增/编辑/删除，其他人只读
- 数据落地单个 JSON 文件，备份即复制该文件

## 架构

```
浏览器 ──HTTP──→ uvicorn(FastAPI) ──读写──→ data/sites.json
                  │
                  ├─ GET  /               静态首页（所有人）
                  ├─ GET  /api/sites      列表（所有人）
                  ├─ GET  /health         探活（所有人，供 healthcheck）
                  ├─ POST /api/login      管理登录换 token（12h 有效）
                  └─ POST/PUT/DELETE        写操作（需管理 token）
```

- **后端**：FastAPI + JSON 文件存储（`tmp` + `os.replace` 原子写，带 `.bak` 备份）
- **鉴权**：管理密码登录 → HMAC-SHA256 token，12h 有效；未登录者仅可 `GET`。`ADMIN_PASSWORD` 未配置时写操作自动禁用（fail-closed，只读模式）
- **前端**：单文件 `static/index.html`，无构建步骤

## 快速启动（Docker）

```bash
cd sites-nav
cp .env.example .env
# ⚠️ 必做：编辑 .env，把 ADMIN_PASSWORD 改成强密码
# 默认是空值（只读模式）；不要留占位符直接部署
docker compose up -d --build
```

浏览器访问 `http://<服务器IP>:8000`。

> **⚠️ 安全警告**：`ADMIN_PASSWORD` 留空 = 只读模式（写操作禁用）。任何已知占位符（`PleaseChangeMe` / `changeme` / `password` / `admin` / `123456` 等）都被后端识别为未配置，自动降级只读。如果生产环境忘记改密码，应用会启动但无法写数据——这是预期行为。

## 入口脚本

| 脚本 | 用途 | 绑定地址 |
|---|---|---|
| `start.sh` | Linux/macOS 一键启动（Docker compose） | 容器 `0.0.0.0:8000` |
| `start.bat` | Windows 本地开发 | `127.0.0.1:8000`（默认，可 `HOST=0.0.0.0` 覆盖） |
| `run.py` | Windows 本地开发（带 --reload 热重载） | `127.0.0.1:8000`（默认，可 `HOST=0.0.0.0` 覆盖） |
| `uvicorn app:app` | 裸跑（不推荐，需手动管理 .env） | 由参数决定 |

> **⚠️ 不要带默认密码部署到服务器**：任何能访问 `.env.example` 的人（包括知道本 README 的人）都能用默认密码登录。生产环境必须改成强密码。

## 环境变量

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `ADMIN_PASSWORD` | 否 | 空 | 管理密码。留空 = 只读模式（写操作禁用）；设了才能登录后增删改 |
| `CATEGRAF_ADMIN_URL` | 否 | 空 | categraf-http-admin 服务地址。留空 = 拨测联动整体关闭 |
| `CATEGRAF_ADMIN_USER` | 否 | `admin` | 拨测管理端登录用户名 |
| `CATEGRAF_ADMIN_PASS` | 否 | 空 | 拨测管理端登录密码；与 URL 同时配置才启用联动 |

## 端口

默认 `8000`，改 `docker-compose.yml` 的 `ports` 即可。

## 数据与备份

- 数据文件：`./data/sites.json`（运行时生成）
- 备份：直接复制该文件；文件损坏时自动回退 `sites.json.bak`
- 持久化：`docker-compose.yml` 把 `./data` 挂载进容器，重建容器数据不丢

## 管理员使用

1. 访问独立登录地址 **`/admin`**（首页不显示任何登录入口，建议收藏该地址），输入 `ADMIN_PASSWORD`
2. 登录后自动跳回首页，出现「＋ 新增网站」「批量导入」「导出」及卡片上的 加入监控/编辑/删除 按钮
3. token 12h 失效；**容器重启后需重新登录**（`TOKEN_SECRET` 进程内生成，重启即轮换，属安全特性）

### 拨测监控快捷操作

运维模式下每张卡片有「加入监控 / 取消监控」快捷按钮，无需打开编辑弹窗：

- 点「加入监控」→ 填 **期望状态码**（3 位数字，多个用 `|` 分隔如 `200|301`，留空默认 `200`）和 **超时时长**（如 `3s` / `500ms` / `1m`，留空不设置）→ 确认后自动注册到 categraf 拨测
- 两个参数保存在站点记录中，编辑弹窗勾选「拨测监控」后可查看和修改，保存后自动同步到拨测端

## 只读用户

直接访问首页即可搜索/浏览/复制地址，无需登录。

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
uvicorn app:app --reload --port 8000
```
