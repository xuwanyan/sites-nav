#!/bin/bash
set -euo pipefail

# ═════════════════════════════════════════════════════════════════
# sites-nav 多版本分区部署
# 把各版本部署成相互独立的实例：目录 / 容器 / 数据库 / 端口全部隔离。
# 原理：docker compose 项目名 = 目录名，每个目录自带 mysql_data 卷，
#       sites-nav 服务未设固定 container_name，可同名镜像多实例并存。
# 用法:
#   sudo bash scripts/deploy_versions.sh                # 部署全部 3 个版本
#   sudo bash scripts/deploy_versions.sh main           # 只部署新版 main
#   sudo bash scripts/deploy_versions.sh dev            # 只部署开发线 develop-20260918
#   sudo bash scripts/deploy_versions.sh legacy         # 只部署旧版 release/v1-对接categraf
#
# 首次用前（服务器没有本仓库时）:
#   curl -fsSL https://raw.githubusercontent.com/xuwanyan/sites-nav/main/scripts/deploy_versions.sh -o /tmp/deploy_versions.sh
#   sudo bash /tmp/deploy_versions.sh
# ═════════════════════════════════════════════════════════════════

# GitHub 加速镜像前缀：可选。设了 GH_MIRROR 就在拉 raw 和 git 前拼一层国内镜像。
# 常见值（任选其一，失效就换）：
#   https://ghproxy.com         https://gh-proxy.com
#   https://gitclone.com/github.com   （git clone 专用）
GH_MIRROR="${GH_MIRROR:-}"
_GH_RAW_BASE="${GH_MIRROR}https://raw.githubusercontent.com"
BOOTSTRAP_URL="${_GH_RAW_BASE}/xuwanyan/sites-nav/main/scripts/bootstrap.sh"
BOOTSTRAP_SH="${BOOTSTRAP_SH:-/tmp/bootstrap.sh}"

LOG()  { printf '\033[1;34m▶\033[0m %s\n' "$*"; }
OK()   { printf '\033[1;32m✔\033[0m %s\n' "$*"; }

# ── 读写 .env 里的键（与 deploy.sh 的 env_set 同款，密码/值不经命令行）──
ensure_env() {
  local key="$1" val="$2" env="$3"
  local tmp; tmp="$(mktemp)"
  if [ -f "$env" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in
        "$key="*) printf '%s=%s\n' "$key" "$val" >> "$tmp" ;;
        *)        printf '%s\n' "$line" >> "$tmp" ;;
      esac
    done < "$env"
  fi
  if ! grep -q "^${key}=" "$tmp" 2>/dev/null; then
    printf '%s=%s\n' "$key" "$val" >> "$tmp"
  fi
  mv "$tmp" "$env"
  chmod 600 "$env"
}

deploy_version() {
  local name="$1" branch="$2" dir="$3" port="$4"
  LOG "$name（$branch）-> $dir :$port"
  if [ ! -e "$BOOTSTRAP_SH" ]; then
    curl -fsSL "$BOOTSTRAP_URL" -o "$BOOTSTRAP_SH"
  fi
  # bootstrap：拉代码(指定分支) + 构建镜像 + 生成 .env，不启动
  sudo bash "$BOOTSTRAP_SH" "$dir" "$branch"
  ensure_env PORT "$port" "$dir/.env"
  # deploy：从该目录自身的代码构建并启动（必要时改 ADMIN_PASSWORD 后再跑）
  ( cd "$dir" && sudo ./deploy.sh --deploy )
  OK "$name 已就绪：http://<host>:$port（目录 $dir）"
}

SELECT="${1:-all}"
case "$SELECT" in
  main|dev|legacy|all) ;;
  *) echo "❌ 未知版本: $SELECT（可选 main / dev / legacy / all）"; exit 1 ;;
esac

# name  branch                         dir                    port
while read -r name branch dir port; do
  [ -n "$name" ] || continue
  [ "$SELECT" = "all" ] || [ "$SELECT" = "$name" ] || continue
  deploy_version "$name" "$branch" "$dir" "$port"
done <<EOF
main    main                     /opt/sites-nav         8000
dev     develop-20260918         /opt/sites-nav-dev     8001
legacy  release/v1-对接categraf  /opt/sites-nav-legacy  8002
EOF

echo ""
echo "═══ 全部完成 ═══"
echo "  main    http://<host>:8000   (新版，main)"
echo "  dev     http://<host>:8001   (开发线，develop-20260918)"
echo "  legacy  http://<host>:8002   (旧版，release/v1-对接categraf)"
echo "登录密码：各目录首次部署时 bootstrap.sh 打印的随机 ADMIN_PASSWORD，或在各 .env 自行修改。"