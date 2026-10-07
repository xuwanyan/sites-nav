#!/bin/bash
# sites-nav 更新部署：拉新 bootstrap → git 更新 + 重建镜像 → 原地替换容器 → 探活
#
# 与 deploy.sh 的分工：
#   deploy.sh  —— 用「当前代码」部署（build + up -d --force-recreate）
#   update.sh  —— 先把代码更新到远端最新，再交给 deploy.sh
# 日常更新只跑本脚本即可。
#
# 服务器直连 github.com 不通时（Empty reply / connection reset），两种解法：
#   1) 一次性给 git 配 URL 重写（推荐，之后所有 github 操作自动走镜像）：
#        git config --global url."https://ghproxy.net/https://github.com/".insteadOf "https://github.com/"
#   2) 跑本脚本时带上镜像前缀（git 与 raw 下载都走它）：
#        export GH_MIRROR=https://ghproxy.net
# 也可用 REPO_URL 整体替换仓库地址（比如换到 Gitee 镜像）：
#        export REPO_URL=https://gitee.com/<你的用户名>/sites-nav.git
set -euo pipefail

export APP_DIR="${APP_DIR:-/root/sites-nav}"
BRANCH="${BRANCH:-main}"
GH_MIRROR="${GH_MIRROR:-}"
REPO_URL="${REPO_URL:-}"

log()  { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!!  %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31mXX  %s\033[0m\n' "$*" >&2; exit 1; }

cd "$APP_DIR" || die "进不去 $APP_DIR"

# ── 0. 前置检查 ────────────────────────────────────────────────────
[ -f deploy.sh ] || die "$APP_DIR/deploy.sh 不存在，APP_DIR 对吗？（当前 $APP_DIR）"
command -v curl   >/dev/null 2>&1 || die "缺少 curl"
command -v docker >/dev/null 2>&1 || die "缺少 docker"

# CATEGRAF_TOKEN 未配时 /api/config/http_response 会对一切拉取返回 401（fail closed），
# categraf 将拿不到任何拨测配置。这不是更新失败，但值得当场提醒。
if [ -f .env ] && ! grep -qE '^CATEGRAF_TOKEN=.+' .env; then
  warn ".env 里没有 CATEGRAF_TOKEN —— categraf 会拉不到任何拨测配置（端点 fail closed）"
fi

OLD_REV="$(git rev-parse --short HEAD 2>/dev/null || echo none)"
log "当前版本 $OLD_REV，开始更新"

# ── 1. 拉新 bootstrap（多镜像；下到临时文件校验后再原子替换） ────────
# 顺序：显式 GH_MIRROR → 已实测可用的 ghproxy 系镜像 → 原有备选
BOOTSTRAP_PATH="xuwanyan/sites-nav/${BRANCH}/scripts/bootstrap.sh"
BOOTSTRAP_URLS=()
if [ -n "$GH_MIRROR" ]; then
  BOOTSTRAP_URLS+=("${GH_MIRROR%/}/https://raw.githubusercontent.com/${BOOTSTRAP_PATH}")
fi
BOOTSTRAP_URLS+=(
  "https://ghproxy.net/https://raw.githubusercontent.com/${BOOTSTRAP_PATH}"
  "https://ghfast.top/https://raw.githubusercontent.com/${BOOTSTRAP_PATH}"
  "https://gh-proxy.com/https://raw.githubusercontent.com/${BOOTSTRAP_PATH}"
  "https://raw.gitmirror.com/${BOOTSTRAP_PATH}"
  "https://cdn.jsdelivr.net/gh/xuwanyan/sites-nav@${BRANCH}/scripts/bootstrap.sh"
)

TMP_SH="$(mktemp /tmp/bootstrap.sh.XXXXXX)"
GOT_URL=""

# 优先用仓库里的本地 bootstrap.sh。原因：raw 文件的镜像会按 URL 缓存，而
# "…/main/scripts/bootstrap.sh" 不带版本号 → 可能拿到旧内容（线上就踩到了：
# 下载到的 bootstrap 缺了刚提交的修复）。本地副本至少是上次成功更新时的版本，
# 而且省一次网络往返。要强制走网络：FORCE_REMOTE_BOOTSTRAP=1
LOCAL_BS="${APP_DIR}/scripts/bootstrap.sh"
if [ -f "$LOCAL_BS" ] && [ "${FORCE_REMOTE_BOOTSTRAP:-0}" != "1" ]; then
  cp -f "$LOCAL_BS" /tmp/bootstrap.sh
  log "bootstrap.sh 来源: 仓库本地 $LOCAL_BS（强制走网络：FORCE_REMOTE_BOOTSTRAP=1）"
else
  for url in "${BOOTSTRAP_URLS[@]}"; do
    for attempt in 1 2; do
      if curl -fsSL --connect-timeout 6 --max-time 60 "$url" -o "$TMP_SH" 2>/dev/null; then
        # 校验：必须真是 shell 脚本。镜像返回 502/HTML 错误页时 curl 也是 0，
        # 不校验就会把错误页当脚本执行，报一堆莫名其妙的语法错。
        if head -n1 "$TMP_SH" 2>/dev/null | grep -q '^#!'; then
          GOT_URL="$url"
          break 2
        fi
        warn "内容不像脚本（可能是错误页），换下一个镜像: $url"
        break
      fi
      warn "下载失败，重试 $attempt/2: $url"
      sleep 2
    done
  done

  if [ -z "$GOT_URL" ]; then
    rm -f "$TMP_SH"
    die "所有镜像都下不到 bootstrap.sh。
  若服务器直连 GitHub 不通，任选其一后重跑：
    1) export GH_MIRROR=https://ghproxy.net
    2) git config --global url.\"https://ghproxy.net/https://github.com/\".insteadOf \"https://github.com/\"
  注意：/tmp/bootstrap.sh 未被改动，仍可手动 sudo -E bash /tmp/bootstrap.sh 用旧版脚本。"
  fi
  log "bootstrap.sh 来源: $GOT_URL"
  mv "$TMP_SH" /tmp/bootstrap.sh     # 原子替换：下载失败时旧脚本原样保留
fi

# ── 2. 准备：git 更新 + 构建镜像 + 配 .env + 修 data 权限（不启动） ──
log "运行 bootstrap（更新代码 + 构建镜像）"
export APP_DIR BRANCH
[ -n "$GH_MIRROR" ] && export GH_MIRROR
[ -n "$REPO_URL" ]  && export REPO_URL
# -E 保留上面的变量，让 bootstrap 里的 git 拉取也走镜像
if ! sudo -E bash /tmp/bootstrap.sh; then
  die "bootstrap 失败。常见原因和对策：

  A. 服务器连不上 github.com（报 Empty reply / connection reset）
     1) export GH_MIRROR=https://ghproxy.net/
     2) git config --global url.\"https://ghproxy.net/https://github.com/\".insteadOf \"https://github.com/\"
     3) 换仓库源： export REPO_URL=https://gitee.com/<用户名>/sites-nav.git

  B. 报 'untracked working tree files would be overwritten by checkout'
     仓库里新跟踪的文件（如 scripts/xxx.sh）在本地已存在但未跟踪，git 拒绝覆盖。
     把 bootstrap 列出的那几个文件 mv 走或删掉，再重跑本脚本。

  此时容器仍在用旧镜像运行，服务没有中断。"
fi

NEW_REV="$(git rev-parse --short HEAD 2>/dev/null || echo none)"
if [ "$NEW_REV" = "$OLD_REV" ]; then
  log "代码无变化（仍是 $NEW_REV），继续重建部署以应用当前代码"
else
  log "代码更新：$OLD_REV → $NEW_REV"
fi

# 自我同步：仓库里的 scripts/update.sh 受版本管理，APP_DIR/update.sh 是运行副本（未跟踪）。
# 拷过去下次跑就是最新版；两个文件相同则跳过（update.sh 是指向 scripts/ 的软链时也走这条）。
# 用 cp 到临时文件再 mv：mv 是原子替换、换新 inode，不会影响"正在运行的本脚本"的读取
# （直接 cp 覆盖会截断同一 inode，正在执行的 shell 可能读到半截内容）。
SELF_SRC="${APP_DIR}/scripts/update.sh"
SELF_DST="${APP_DIR}/update.sh"
if [ -f "$SELF_SRC" ] && ! cmp -s "$SELF_SRC" "$SELF_DST" 2>/dev/null; then
  if cp -f "$SELF_SRC" "${SELF_DST}.new" && chmod +x "${SELF_DST}.new" && mv -f "${SELF_DST}.new" "$SELF_DST" 2>/dev/null; then
    log "已把 update.sh 同步为仓库里的最新版（下次运行生效）"
  else
    rm -f "${SELF_DST}.new" 2>/dev/null || true
    warn "update.sh 自我同步失败（不影响本次部署），可手动 cp scripts/update.sh update.sh"
  fi
fi

# ── 3. 原地替换容器 ────────────────────────────────────────────────
# 不再先跑 deploy.sh --stop：--deploy 内部就是 up -d --force-recreate，本来就会停旧起新；
# 先 stop 只会拉长停机时间，还会把"更新失败"变成"服务一直不在线"。
log "部署容器（原地替换）"
./deploy.sh --deploy

# ── 4. 探活：真请求 /health，而不是只看容器在不在 ─────────────────
# 宿主端口由 .env 的 PORT 决定（compose 里是 "${PORT:-8000}:8000"）
PORT="$(grep -E '^PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '[:space:]')"
PORT="${PORT:-8000}"

log "探活 http://127.0.0.1:${PORT}/health"
HEALTH_OK=0
LAST_CODE="无响应"
for _ in $(seq 1 15); do
  LAST_CODE="$(curl -s -o /dev/null -w '%{http_code}' -m 3 "http://127.0.0.1:${PORT}/health" 2>/dev/null || echo 000)"
  if [ "$LAST_CODE" = "200" ]; then HEALTH_OK=1; break; fi
  sleep 2
done

./deploy.sh --status || true
docker compose ps || true

if [ "$HEALTH_OK" = "1" ]; then
  log "完成：$OLD_REV → $NEW_REV，/health 返回 200"
else
  warn "探活未通过（最后一次返回 $LAST_CODE）。排查：
    ./deploy.sh --status
    docker compose logs --tail=80
    curl -v http://127.0.0.1:${PORT}/health
  端口取自 .env 的 PORT（当前 ${PORT}），若与实际映射不符请改 .env 或本脚本。"
  exit 1
fi
