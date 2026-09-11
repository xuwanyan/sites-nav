#!/bin/bash
set -euo pipefail

# ═══════════════════════════════════════════════════════
# sites-nav 运维脚本：部署 / 更新 / 停止 / 状态
# 定位：本地已克隆代码后的日常运维。服务器首次拉代码+构建用 scripts/bootstrap.sh
# 用法:
#   ./deploy.sh              交互式部署（首次）
#   ./deploy.sh --deploy     非交互式部署（.env 已配好）
#   ./deploy.sh --update     拉取新镜像重启
#   ./deploy.sh --stop       停止容器
#   ./deploy.sh --status     查看状态
# ═══════════════════════════════════════════════════════

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
ENV_FILE="$DIR/.env"
DATA_DIR="$DIR/data"
MODE="${1:-}"

# 端口：shell 变量 > .env 里的 PORT > 默认 8000
# 与 docker-compose.yml 的 ${PORT:-8000} 同源，避免"检查的端口"和"实际绑定的端口"不一致
PORT="${PORT:-$(grep -E '^PORT=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)}"
PORT="${PORT:-8000}"
export PORT

# 已知占位符密码（与 app.py 保持一致）
# app.py 是 strip().lower() 后比对，这里同样处理，
# 否则 "Admin" 会被当作真密码接受，而应用实际已降级只读
_is_placeholder() {
    local v
    v=$(printf '%s' "${1:-}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | tr '[:upper:]' '[:lower:]')
    case "$v" in
        ""|"pleasechangeme"|"changeme"|"change_me"|"password"|"123456"|"admin"|"admin123") return 0 ;;
        *) return 1 ;;
    esac
}

# ── 前置检查 ──
check_prereqs() {
    if ! command -v docker >/dev/null 2>&1; then
        echo "❌ 未安装 Docker，请先安装: https://docs.docker.com/get-docker/"
        exit 1
    fi
    if ! docker compose version >/dev/null 2>&1; then
        echo "❌ 未安装 Docker Compose V2，请更新 Docker"
        exit 1
    fi
    if ! docker info >/dev/null 2>&1; then
        echo "❌ Docker 服务未运行，请启动 Docker"
        exit 1
    fi
    echo "✅ Docker $(docker --version | grep -oP '\d+\.\d+\.\d+' | head -1) 就绪"
}

# ── 创建 .env ──
setup_env() {
    if [ ! -f "$ENV_FILE" ]; then
        (umask 077 && cp .env.example "$ENV_FILE")
        chmod 600 "$ENV_FILE"
        echo "✅ 已创建 $ENV_FILE（权限 0600）"
    else
        chmod 600 "$ENV_FILE" 2>/dev/null || true
        echo "✅ $ENV_FILE 已存在"
    fi
    mkdir -p "$DATA_DIR"
}

# ── 设置管理密码 ──
setup_password() {
    local interactive="${1:-1}"
    if grep -q '^ADMIN_PASSWORD=' "$ENV_FILE" 2>/dev/null; then
        local existing
        existing=$(grep '^ADMIN_PASSWORD=' "$ENV_FILE" | head -1 | cut -d= -f2-)
        if _is_placeholder "$existing"; then
            echo "⚠️  .env 中 ADMIN_PASSWORD 为空或占位符"
            if [ "$interactive" = "0" ]; then
                echo "   非交互模式，将以只读模式启动"
                return
            fi
            echo -n "   请输入管理密码（留空 = 只读模式）： "
            read -rsp "" ADMIN_PASSWORD
            echo ""
            if [ -n "$ADMIN_PASSWORD" ]; then
                local tmp
                tmp=$(mktemp "$ENV_FILE.XXXXXX")
                awk -v p="$ADMIN_PASSWORD" -F= '
                    $1=="ADMIN_PASSWORD" { print "ADMIN_PASSWORD=" p; next }
                    { print }
                ' "$ENV_FILE" > "$tmp"
                mv "$tmp" "$ENV_FILE"
                chmod 600 "$ENV_FILE"
                echo "✅ 密码已写入"
            else
                echo "ℹ️  未设置密码，只读模式"
            fi
        else
            echo "✅ 已有管理密码"
        fi
    else
        if [ "$interactive" = "0" ]; then
            echo "ℹ️  未设置密码，只读模式"
            return
        fi
        echo -n "请输入管理密码（留空 = 只读模式）： "
        read -rsp "" ADMIN_PASSWORD
        echo ""
        if [ -n "$ADMIN_PASSWORD" ]; then
            echo "ADMIN_PASSWORD=$ADMIN_PASSWORD" >> "$ENV_FILE"
            chmod 600 "$ENV_FILE"
            echo "✅ 密码已写入"
        fi
    fi
}

# ── 端口检查 ──
check_port() {
    if command -v ss >/dev/null 2>&1; then
        if ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
            local pid
            pid=$(ss -ltnp 2>/dev/null | grep ":${PORT} " | grep -oP 'pid=\K\d+' | head -1)
            echo "❌ 端口 $PORT 已被占用${pid:+ (PID $pid)}"
            echo "   先执行: ./deploy.sh --stop 或 kill $pid"
            exit 1
        fi
    elif command -v netstat >/dev/null 2>&1; then
        if netstat -ltn 2>/dev/null | grep -q ":${PORT} "; then
            echo "❌ 端口 $PORT 已被占用"
            exit 1
        fi
    fi
}

# ── 等待健康 ──
wait_healthy() {
    echo -n "   等待容器健康..."
    for i in $(seq 1 30); do
        if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
            local h
            h=$(curl -s "http://127.0.0.1:${PORT}/health")
            if echo "$h" | grep -q "data_warning"; then
                echo " ⚠️ 恢复"
                echo "   警告: $(echo "$h" | grep -oP 'data_warning.{0,60}')"
            else
                echo " ✅"
            fi
            return 0
        fi
        sleep 1
    done
    echo " ❌ 超时"
    echo "   查看日志: docker compose logs -f"
    return 1
}

# ── 部署 ──
do_deploy() {
    echo "🚀 构建并启动 sites-nav..."
    docker compose up -d --build
    wait_healthy
    echo ""
    echo "══════════════════════════════════════"
    echo "  ✅ 部署完成"
    echo "  访问: http://$(hostname -I 2>/dev/null | awk '{print $1}'):${PORT}"
    echo "  日志: docker compose logs -f"
    echo "  停止: ./deploy.sh --stop"
    echo "══════════════════════════════════════"
}

# ── 更新 ──
do_update() {
    echo "🔄 拉取并重启..."
    docker compose pull 2>/dev/null || docker compose build
    docker compose up -d
    wait_healthy
    echo "✅ 更新完成"
}

# ── 停止 ──
do_stop() {
    docker compose down
    echo "✅ 已停止"
}

# ── 状态 ──
do_status() {
    docker compose ps
    echo ""
    echo "健康状态:"
    curl -s "http://127.0.0.1:${PORT}/health" 2>/dev/null || echo "  服务未运行"
    echo ""
    echo "数据目录: $DATA_DIR"
    ls -lh "$DATA_DIR" 2>/dev/null || echo "  (空)"
}

# ═══════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════
check_prereqs

case "$MODE" in
    --stop)
        do_stop
        ;;
    --status)
        do_status
        ;;
    --update)
        do_update
        ;;
    --deploy)
        setup_env
        setup_password 0
        check_port
        do_deploy
        ;;
    *)
        # 默认：交互式部署
        setup_env
        setup_password 1
        check_port
        do_deploy
        ;;
esac
