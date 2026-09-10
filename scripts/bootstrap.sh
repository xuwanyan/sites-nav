#!/bin/bash
set -euo pipefail

# ═════════════════════════════════════════════════════════════════
# sites-nav 服务器引导：拉取代码 → 构建镜像 → 启动 → 探活
#
# 用法:
#   sudo bash scripts/bootstrap.sh                  # 首次部署 / 重新部署
#   REPO_URL=git@github.com:x/sites-nav.git sudo -E bash scripts/bootstrap.sh
#
# 幂等：可重复执行，每次都部署远端最新代码。
#       .env 和 data/ 被 gitignore，git 操作不会碰它们（数据和密码不丢）。
#
# 服务器最小准备（只需一次）:
#   curl -fsSL https://raw.githubusercontent.com/xuwanyan/sites-nav/main/scripts/bootstrap.sh -o /tmp/bootstrap.sh
#   sudo bash /tmp/bootstrap.sh
# ═════════════════════════════════════════════════════════════════

REPO_URL="${REPO_URL:-https://github.com/xuwanyan/sites-nav.git}"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-/opt/sites-nav}"
PORT="${PORT:-8000}"
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
# 命中即视为"未配置"，app 会自动降级只读模式
# 注意：app.py 是先 strip().lower() 再比对，这里必须同样处理，
# 否则 "Admin" 这类写法会被误判为真密码，结果应用静默降级只读
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
  OLD="$(git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || echo none)"
  git -C "$APP_DIR" fetch --tags origin "$BRANCH"
  NEW="$(git -C "$APP_DIR" rev-parse "origin/$BRANCH")"
  if [ "$OLD" = "$NEW" ]; then
    OK "代码已是最新 ($NEW)"
  else
    # reset 而非 pull：部署机不应有本地提交漂移。ignored 文件不受影响。
    git -C "$APP_DIR" checkout -B "$BRANCH" "$NEW"
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

# ── 2. 构建镜像 ─────────────────────────────────────────────────
LOG "构建镜像"
docker compose build
IMAGE="${IMAGE:-$(docker compose config --images | head -1)}"
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

# ── 5. 端口冲突处理 ─────────────────────────────────────────────
# 只清自己项目占的端口；被无关进程占用时留给 deploy.sh 报清楚，不替用户杀进程
if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${PORT}$" && [ -n "$(docker compose ps -q 2>/dev/null)" ]; then
  WARN "端口 $PORT 被现有容器占用，先停止旧容器"
  docker compose down
fi

# ── 6. 启动 + 探活 ──────────────────────────────────────────────
# 密码/端口/健康检查/占位符拦截都交给项目自带的 deploy.sh，不重复实现
LOG "启动"
exec ./deploy.sh --deploy
