#!/bin/bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

ENV_FILE="$DIR/.env"

cleanup() {
    if [ -n "${ENV_FILE_TMP:-}" ] && [ -f "$ENV_FILE_TMP" ]; then
        rm -f "$ENV_FILE_TMP"
    fi
}
trap cleanup EXIT

# ── 1. 确保 .env 存在（umask 077 保证权限 0600）──
if [ ! -f "$ENV_FILE" ]; then
    (umask 077 && cp .env.example "$ENV_FILE")
    chmod 600 "$ENV_FILE"
    echo "✅ 已从 .env.example 创建 .env（权限 0600）"
else
    chmod 600 "$ENV_FILE" 2>/dev/null || true
fi

# ── 2. 设置管理密码 ──
# 已知占位符视为未设置（与 app.py 保持一致）
_is_placeholder() {
    local v="${1:-}"
    case "$v" in
        ""|"PleaseChangeMe"|"changeme"|"change_me"|"password"|"123456"|"admin"|"admin123") return 0 ;;
        *) return 1 ;;
    esac
}

_prompt_password() {
    if [ ! -t 0 ]; then
        echo "⚠️  非交互式终端，跳过密码设置（只读模式）"
        return
    fi
    read -rsp "请输入管理密码（留空跳过 = 只读模式）： " ADMIN_PASSWORD || {
        echo ""
        echo "⚠️  未读取到输入，以只读模式启动"
        return
    }
    echo ""
}

_write_password() {
    # 用 awk 重写整行，避免 sed 里 & / \ * [ ] 等字符破坏替换
    ENV_FILE_TMP="$(mktemp "$ENV_FILE.XXXXXX")"
    awk -v p="$ADMIN_PASSWORD" -F= '
        $1=="ADMIN_PASSWORD" { print "ADMIN_PASSWORD=" p; next }
        { print }
    ' "$ENV_FILE" > "$ENV_FILE_TMP"
    mv "$ENV_FILE_TMP" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    ENV_FILE_TMP=""
    echo "✅ 管理密码已写入 .env"
}

if grep -q '^ADMIN_PASSWORD=' "$ENV_FILE" 2>/dev/null; then
    existing=$(grep '^ADMIN_PASSWORD=' "$ENV_FILE" | head -1 | cut -d= -f2-)
    if _is_placeholder "$existing"; then
        _prompt_password
        if [ -n "${ADMIN_PASSWORD:-}" ]; then
            _write_password
        else
            echo "ℹ️  未设置密码，将以只读模式启动"
        fi
    else
        echo "⚠️  .env 中已有管理密码，如需修改请编辑 $ENV_FILE 后重跑"
    fi
else
    _prompt_password
    if [ -n "${ADMIN_PASSWORD:-}" ]; then
        { echo "ADMIN_PASSWORD=$ADMIN_PASSWORD"; } >> "$ENV_FILE"
        chmod 600 "$ENV_FILE"
        echo "✅ 管理密码已写入 .env"
    else
        echo "ℹ️  未设置密码，将以只读模式启动"
    fi
fi

# ── 3. 端口预检 ──
if command -v ss >/dev/null 2>&1; then
    if ss -ltn 2>/dev/null | grep -q ':8000 '; then
        echo "❌ 端口 8000 已被占用，请先停掉占用进程或改用其他端口"
        exit 1
    fi
elif command -v netstat >/dev/null 2>&1; then
    if netstat -ltn 2>/dev/null | grep -q ':8000 '; then
        echo "❌ 端口 8000 已被占用，请先停掉占用进程或改用其他端口"
        exit 1
    fi
fi

# ── 4. 启动 ──
echo ""
echo "🚀 正在构建并启动 sites-nav ..."
docker compose up -d --build

# 等待容器健康（最多 30s）
for i in $(seq 1 30); do
    status=$(docker compose ps --format json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0].get('Health','') if d else '')" 2>/dev/null || echo "")
    if [ "$status" = "healthy" ]; then
        echo ""
        echo "✅ 启动完成！浏览器访问 http://<服务器IP>:8000"
        break
    fi
    sleep 1
    if [ "$i" = "30" ]; then
        echo ""
        echo "⚠️  容器启动但未在 30 秒内健康。查看日志：docker compose logs -f"
    fi
done
echo "   查看日志：docker compose logs -f"
echo "   停止：    docker compose down"
