#!/bin/bash
set -euo pipefail

# ═════════════════════════════════════════════════════════════════
# sites-nav 服务器引导：拉取代码 → 构建镜像 → 启动 → 探活
#
# 用法:
#   sudo bash scripts/bootstrap.sh                     # 只准备（拉代码/建镜像/配 .env），不启动
#   sudo bash scripts/bootstrap.sh --deploy            # 准备后接着部署（已配好 .env 时用）
#   sudo bash scripts/bootstrap.sh /data/sites-nav     # 指定安装目录
#   sudo bash scripts/bootstrap.sh /data/sites-nav v1.2 # 指定分支或标签
#   sudo bash scripts/bootstrap.sh --deploy /data/sites-nav
#
# 安装目录优先级：位置参数 > APP_DIR 环境变量 > DEFAULT_APP_DIR > /opt/sites-nav。
# 想改默认路径又不想每次敲：export DEFAULT_APP_DIR=/data/sites-nav 写到 profile 里。
#
# 默认不启动是有意的：用户常常还要改 .env（MYSQL_* / ADMIN_PASSWORD / PORT），
# 脚本替用户决定"连哪台 MySQL"很容易出事 —— 陈旧 .env 里的 MYSQL_HOST=mysql
# 会静默把连接指到 compose 内置的空容器上。准备完只打印下一步命令。
#
# 参数用位置传入，不用环境变量 —— sudo 会过滤环境，APP_DIR=x sudo bash
# 这种写法变量会静默丢失（除非加 -E）。偏要用环境变量的话：
#   export APP_DIR=/data/sites-nav && sudo -E bash scripts/bootstrap.sh
#
# 幂等：可重复执行，每次都部署远端最新代码。
#       .env 和 data/ 被 gitignore，git 操作不会碰它们（数据和密码不丢）。
#
# 服务器最小准备（只需一次）:
#   curl -fsSL https://raw.githubusercontent.com/xuwanyan/sites-nav/main/scripts/bootstrap.sh -o /tmp/bootstrap.sh
#   sudo bash /tmp/bootstrap.sh
# ═════════════════════════════════════════════════════════════════

# GitHub 加速镜像前缀：可选。设了 GH_MIRROR 就在 git 仓库地址前拼一层国内镜像。
# 与 deploy_versions.sh 的 GH_MIRROR 同一套：服务器只要 export GH_MIRROR 即可同时
# 加速 raw 下载和 git 拉取。示例：export GH_MIRROR=https://ghproxy.com
GH_MIRROR="${GH_MIRROR:-}"
REPO_URL="${REPO_URL:-${GH_MIRROR}https://github.com/xuwanyan/sites-nav.git}"
# APP_DIR / BRANCH 取位置参数，环境变量兜底（位置参数能穿过 sudo）。
# --deploy 是开关：跑完准备后接着部署。不传则只准备、不启动。
_ENV_APP_DIR="${APP_DIR:-}"
_ENV_BRANCH="${BRANCH:-}"
DO_DEPLOY=0
_ARGS=()
for _a in "$@"; do
  case "$_a" in
    --deploy) DO_DEPLOY=1 ;;
    *) _ARGS+=("$_a") ;;
  esac
done
# 安装目录优先级：位置参数 > APP_DIR 环境变量 > DEFAULT_APP_DIR > /opt/sites-nav
# DEFAULT_APP_DIR 让「默认路径」本身也可配：不想每次敲路径就在 profile 里 export 一次。
APP_DIR="${_ARGS[0]:-${_ENV_APP_DIR:-${DEFAULT_APP_DIR:-/opt/sites-nav}}}"
BRANCH="${_ARGS[1]:-${_ENV_BRANCH:-main}}"
IMAGE="${IMAGE:-}"

NEW_PASS=""
LOG()  { printf '\033[1;34m▶\033[0m %s\n' "$*"; }
OK()   { printf '\033[1;32m✔\033[0m %s\n' "$*"; }
WARN() { printf '\033[1;33m⚠\033[0m %s\n' "$*"; }
FAIL() { printf '\033[1;31m✘\033[0m %s\n' "$*" >&2; exit 1; }

# ── 前置检查 ────────────────────────────────────────────────────
for c in git docker curl openssl ss; do
  command -v "$c" >/dev/null 2>&1 || FAIL "缺少命令: $c"
done
docker compose version >/dev/null 2>&1 || FAIL "缺少 Docker Compose v2，请升级 Docker"
docker info >/dev/null 2>&1 || FAIL "Docker 未运行或无权限（检查 docker 组 / 用 sudo）"

# 已知占位符密码 —— 必须与 app.py 的 _PLACEHOLDER_PASSWORDS 保持一致
# 命中即视为"未配置"，首次启动会自动生成随机密码
# 注意：app.py 是先 strip().lower() 再比对，这里必须同样处理，
# 否则 "Admin" 这类写法会被误判为真密码，占位符就被当真密码写进 users 表
_is_placeholder() {
  local v
  v="$(printf '%s' "${1:-}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | tr '[:upper:]' '[:lower:]')"
  case "$v" in
    ""|"pleasechangeme"|"changeme"|"change_me"|"password"|"123456"|"admin"|"admin123") return 0 ;;
    *) return 1 ;;
  esac
}

# ── 1. 拉取代码 ─────────────────────────────────────────────────
LOG "拉取代码 $REPO_URL ($BRANCH) -> $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  # 不用 git -C：老版本 git (< 2.11，CentOS 7 / Alinux 常见) 不支持该选项
  OLD="$(cd "$APP_DIR" && git rev-parse HEAD 2>/dev/null || echo none)"
  (cd "$APP_DIR" && git fetch --tags origin "$BRANCH")
  # 用 FETCH_HEAD 而不是 origin/$BRANCH：后者依赖 remote.origin.fetch 的 refspec。
  # 那项缺失时（手动 git init + git remote add 装出来的仓库常见）fetch 只会更新
  # FETCH_HEAD，origin/$BRANCH 停在旧提交 → OLD 永远等于 NEW → 误报"代码已是最新"，
  # 每次更新都静默不生效。FETCH_HEAD 由本次 fetch 保证是远端最新。
  NEW="$(cd "$APP_DIR" && git rev-parse FETCH_HEAD)"
  TRACKED="$(cd "$APP_DIR" && git rev-parse --verify --quiet "origin/$BRANCH" || echo none)"
  if [ "$TRACKED" = "none" ] || [ "$TRACKED" != "$NEW" ]; then
    WARN "origin/$BRANCH 未同步（本地 ${TRACKED:-未设置}，远端 $NEW）"
    WARN "本仓库 remote.origin.fetch 可能没配 refspec，手动 fetch 的改动不会体现在 origin/ 引用上"
    WARN "修：cd $APP_DIR && git config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*' && git fetch origin"
  fi
  if [ "$OLD" = "$NEW" ]; then
    OK "代码已是最新 ($NEW)"
  else
    # checkout -B 而非 pull：部署机不应有本地提交漂移。ignored 文件不受影响。
    (cd "$APP_DIR" && git checkout -B "$BRANCH" "$NEW")
    OK "已更新 $(printf '%.7s' "$OLD") -> $(printf '%.7s' "$NEW")"
  fi
elif [ -e "$APP_DIR" ]; then
  FAIL "$APP_DIR 已存在但不是 git 仓库，请确认 APP_DIR 后重试（不会删除未知目录）"
else
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
  OK "克隆完成"
fi
cd "$APP_DIR"

# 仓库里 .sh 的 mode 是 100644（从 Windows 提交，无法带上执行位），这里补上
chmod +x *.sh
OK "已补齐脚本执行位"

# ── 1.5 内置 MySQL 开关 ─────────────────────────────────────────
# compose 里 mysql 服务挂在 profiles 上，按 .env 的 MYSQL_HOST 决定是否激活。
# 这里 .env 可能还没生成（在下方第 4 步创建），取不到时按默认内置处理；
# 最终 deploy.sh 会用真实的 .env 重算一次。
MH="$(grep -E '^MYSQL_HOST=' .env 2>/dev/null | head -1 | cut -d= -f2- || true)"
MH="${MH:-mysql}"
if [ "$MH" = "mysql" ]; then
  export COMPOSE_PROFILES="builtin-mysql"
else
  # 必须显式清掉：本脚本末尾 exec ./deploy.sh 会继承这里的导出，
  # 而 deploy.sh 只负责"该激活时激活"、不负责"该跳过时跳过"，
  # 父进程留下的 COMPOSE_PROFILES 会让外部 MySQL 场景多起一个空 mysql 容器。
  unset COMPOSE_PROFILES
fi

# ── 2. 构建镜像 ─────────────────────────────────────────────────
LOG "构建镜像"
docker compose build
if [ -z "$IMAGE" ]; then
  # 不能用 `docker compose config --images | head -1`：内置 mysql 服务激活时
  # 它会同时列出 mysql:8.0 和 sites-nav:latest，head -1 拿到 mysql；
  # 直接 grep 整个 compose 文件取第一个 image: 也一样（mysql 块在前面）。
  # 下面只取 sites-nav 服务块内的值，不依赖 Compose 的输出顺序。
  # compose 里 image 是字面量 sites-nav:latest，无插值，awk 与 compose 解析结果一致。
  IMAGE="$(awk '/^  sites-nav:/{f=1} f && /^[[:space:]]{4}image:/{print $2; exit}' docker-compose.yml | tr -d '"' || true)"
fi
IMAGE="${IMAGE:-sites-nav:latest}"
OK "镜像就绪: $IMAGE"

# ── 3. 修 data/ 目录归属 ────────────────────────────────────────
# 容器 read_only + 非 root，唯一可写位置是 bind mount 的 ./data。
# 以 root 创建的 data/ 会让容器内写入 PermissionError（页面能看、一点新增就 500）。
LOG "设置数据目录权限"
mkdir -p "$APP_DIR/data"
ID_LINE="$(docker run --rm --entrypoint id "$IMAGE")"
APP_UID="$(printf '%s' "$ID_LINE" | sed -n 's/.*uid=\([0-9]*\).*/\1/p')"
APP_GID="$(printf '%s' "$ID_LINE" | sed -n 's/.*gid=\([0-9]*\).*/\1/p')"
[ -n "$APP_UID" ] && [ -n "$APP_GID" ] || FAIL "无法从镜像解析运行用户: $ID_LINE"
chown -R "$APP_UID:$APP_GID" "$APP_DIR/data"
OK "data/ -> $APP_UID:$APP_GID（容器运行用户）"

# ── 4. .env 与管理密码 ──────────────────────────────────────────
LOG "检查 .env"
if [ ! -f .env ]; then
  (umask 077 && cp .env.example .env)
  OK "已创建 .env"
fi
chmod 600 .env
CUR="$(sed -n 's/^ADMIN_PASSWORD=//p' .env | head -1)"
if _is_placeholder "$CUR"; then
  P="$(openssl rand -hex 24)"
  if grep -q '^ADMIN_PASSWORD=' .env; then
    T="$(mktemp)"
    awk -v p="$P" 'BEGIN{FS=OFS="="} $1=="ADMIN_PASSWORD"{print "ADMIN_PASSWORD=" p; next} {print}' .env > "$T"
    mv "$T" .env
  else
    printf 'ADMIN_PASSWORD=%s\n' "$P" >> .env
  fi
  chmod 600 .env
  NEW_PASS="$P"
  WARN "ADMIN_PASSWORD 为空或占位符，已生成随机密码"
  # 在这里打印而不是部署后：下面是 exec，之后无法再输出
  echo ""
  printf '\033[1;33m┌─ 刚生成的管理密码（本次唯一一次展示，请妥善保存）─┐\033[0m\n'
  printf '  %s\n' "$NEW_PASS"
  printf '\033[1;33m└─ 建议改为自定义强密码: 编辑 %s 后 docker compose up -d ┘\033[0m\n' "$APP_DIR/.env"
  echo ""
else
  OK "ADMIN_PASSWORD 已配置"
fi

# MySQL 密码：用户与权限存储。MYSQL_HOST 指向 compose 内置 mysql 服务（默认）时自动生成；
# 指向外部实例时绝不能代填，否则应用会拿随机密码去连别人的库
MYSQL_HOST_VAL="$(sed -n 's/^MYSQL_HOST=//p' .env | head -1)"
MYSQL_HOST_VAL="${MYSQL_HOST_VAL:-mysql}"
if [ "$MYSQL_HOST_VAL" != "mysql" ]; then
  WARN "MYSQL_HOST=$MYSQL_HOST_VAL 指向外部数据库，请自行填写 MYSQL_USER / MYSQL_PASSWORD"
else
  if [ -z "$(sed -n 's/^MYSQL_PASSWORD=//p' .env | head -1)" ]; then
    MP="$(openssl rand -hex 24)"
    T="$(mktemp)"
    awk -v p="$MP" 'BEGIN{FS=OFS="="} $1=="MYSQL_PASSWORD"{print "MYSQL_PASSWORD=" p; next} {print}' .env > "$T"
    mv "$T" .env
    chmod 600 .env
    OK "MYSQL_PASSWORD 已生成并写入 .env"
  else
    OK "MYSQL_PASSWORD 已配置"
  fi
  # MYSQL_ROOT_PASSWORD 只在数据卷首次初始化时生效；缺失时 mysql 容器自己拒绝启动
  # （compose 里是 ${MYSQL_ROOT_PASSWORD:-}，不会在配置阶段报错）
  if [ -z "$(sed -n 's/^MYSQL_ROOT_PASSWORD=//p' .env | head -1)" ]; then
    MR="$(openssl rand -hex 24)"
    T="$(mktemp)"
    awk -v p="$MR" 'BEGIN{FS=OFS="="} $1=="MYSQL_ROOT_PASSWORD"{print "MYSQL_ROOT_PASSWORD=" p; next} {print}' .env > "$T"
    mv "$T" .env
    chmod 600 .env
    OK "MYSQL_ROOT_PASSWORD 已生成（仅数据卷首次初始化生效）"
  fi
fi

# ── 5. 端口（只读，用于下面提示；本脚本不动任何正在跑的东西）────
# PORT：shell 变量 > .env 里的 PORT > 默认 8000，与 docker-compose.yml 的 ${PORT:-8000} 同源
# .env 没有 PORT 行时 grep 返回 1，本文件是 set -euo pipefail，
# 写成 PORT="${PORT:-$(grep ...)}" 会让赋值整体失败 → 脚本静默退出一行不输出。
PORT="${PORT:-}"
if [ -z "$PORT" ]; then
  PORT="$(grep -E '^PORT=' .env 2>/dev/null | head -1 | cut -d= -f2- || true)"
fi
PORT="${PORT:-8000}"
export PORT
# 注意这里**不**清端口、不 down 任何容器：bootstrap 只准备，停止/启动一律走 deploy.sh。
# 端口冲突由 deploy.sh 的 check_port 报清楚（它只报错，不杀任何进程）。

# ── 6. 收尾 ─────────────────────────────────────────────────────
# 默认只准备不启动：给用户一个改 .env 的窗口。传 --deploy 才接着部署，
# 给已经配好 .env 的重复部署 / 升级用。
if [ "$DO_DEPLOY" = 1 ]; then
  LOG "启动"
  exec ./deploy.sh --deploy
fi

PW="$(sed -n 's/^MYSQL_PASSWORD=//p' .env | head -1)"
if [ -n "$PW" ]; then PW_NOTE="已填写"
else PW_NOTE="空 ← 内置模式已自动生成；外部模式需要你自己填，不填应用会报 1045"; fi
if [ "$MYSQL_HOST_VAL" = "mysql" ]; then
  MODE_NOTE="内置（会启动 compose 里的 mysql 容器）"
else
  MODE_NOTE="外部（不启动内置 mysql 容器）"
fi

cat <<EOF

$(printf '\033[1;32m✔\033[0m') 准备完成，但**服务还没启动**

  代码目录      $APP_DIR
  镜像          $IMAGE
  监听端口      $PORT
  MySQL 模式    $MODE_NOTE
  MYSQL_HOST    $MYSQL_HOST_VAL
  MYSQL_PASSWORD  $PW_NOTE

$(printf '\033[1;33m⚠\033[0m')  确认 .env 里 MYSQL_HOST / MYSQL_PASSWORD 是你要的值，再启动。
  最常见的错：.env 是上一次部署留下的，MYSQL_HOST 还指向内置容器，
  于是应用拿那个库的密码去连你的库 → 1045。

  下一步：
    cd $APP_DIR && sudo ./deploy.sh --deploy

  以后升级（.env 已配好，一条命令）：
    sudo bash /tmp/bootstrap.sh --deploy
$(printf '─────────────────────────────────────────────────────────────')
EOF
