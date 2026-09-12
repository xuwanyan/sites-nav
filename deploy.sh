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
# 与 docker-compose.yml 的 ${PORT:-8000} 同源，避免"检查的端口"和"实际绑定的端口"不一致。
# 注意：.env 没有 PORT 行时 grep 返回 1，本文件是 set -euo pipefail，
# 写成 PORT="${PORT:-$(grep ...)}" 会让赋值语句整体失败 → 脚本静默退出、一行输出都没有。
# 所以拆成显式判空 + || true。
PORT="${PORT:-}"
if [ -z "$PORT" ]; then
    PORT="$(grep -E '^PORT=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true)"
fi
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
    # 不用 grep -P：非 UTF-8 locale 下 GNU grep 会报 "supports only unibyte and UTF-8 locales"
    echo "✅ Docker $(docker --version | sed 's/.*version //;s/[, ].*//' | head -1) 就绪"
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

# ── 设置管理员初始密码 ──
# ADMIN_PASSWORD 只用于首次启动种子 admin 账号；表已有用户后就不再被读取，
# 所以"留空"不再是只读模式，而是首次启动时生成随机密码并在日志打印一次。
setup_password() {
    local interactive="${1:-1}"
    local existing
    existing=$(grep '^ADMIN_PASSWORD=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
    if ! _is_placeholder "$existing"; then
        echo "✅ 已有管理员初始密码"
        return
    fi
    if [ "$interactive" = "0" ]; then
        echo "ℹ️  未设置管理员初始密码，首次启动会生成随机密码并打印一次（docker compose logs 查看）"
        return
    fi
    echo -n "   请输入管理员初始密码（首次启动创建 admin 账号，之后可在后台改）： "
    read -rsp "" ADMIN_PASSWORD
    echo ""
    if [ -z "$ADMIN_PASSWORD" ]; then
        echo "ℹ️  留空，首次启动会生成随机密码"
        return
    fi
    if grep -q '^ADMIN_PASSWORD=' "$ENV_FILE"; then
        local tmp
        tmp=$(mktemp "$ENV_FILE.XXXXXX")
        awk -v p="$ADMIN_PASSWORD" -F= '
            $1=="ADMIN_PASSWORD" { print "ADMIN_PASSWORD=" p; next }
            { print }
        ' "$ENV_FILE" > "$tmp"
        mv "$tmp" "$ENV_FILE"
    else
        echo "ADMIN_PASSWORD=$ADMIN_PASSWORD" >> "$ENV_FILE"
    fi
    chmod 600 "$ENV_FILE"
    echo "✅ 密码已写入"
}

# ── 设置 MySQL 密码 ──
# 仅当 MYSQL_HOST 指向 compose 内置的 mysql 服务时才自动生成；
# 指向已有实例时绝不能代填，否则应用会拿随机密码连别人的库。
setup_mysql_password() {
    local host_val
    host_val=$(grep -E '^MYSQL_HOST=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
    host_val="${host_val:-mysql}"
    if [ "$host_val" != "mysql" ]; then
        echo "ℹ️  MYSQL_HOST=$host_val 指向外部数据库，请自行填写 MYSQL_USER / MYSQL_PASSWORD"
        return
    fi
    local existing
    existing=$(grep '^MYSQL_PASSWORD=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
    if [ -n "$existing" ]; then
        echo "✅ 已有 MySQL 密码"
    else
        local p
        p=$(openssl rand -hex 24)
        if grep -q '^MYSQL_PASSWORD=' "$ENV_FILE"; then
            local tmp
            tmp=$(mktemp "$ENV_FILE.XXXXXX")
            awk -v p="$p" -F= '
                $1=="MYSQL_PASSWORD" { print "MYSQL_PASSWORD=" p; next }
                { print }
            ' "$ENV_FILE" > "$tmp"
            mv "$tmp" "$ENV_FILE"
        else
            echo "MYSQL_PASSWORD=$p" >> "$ENV_FILE"
        fi
        chmod 600 "$ENV_FILE"
        echo "✅ MySQL 密码已生成并写入 $ENV_FILE"
    fi
    # MYSQL_ROOT_PASSWORD 只在数据卷首次初始化时生效，缺失时 compose 的 :? 会直接报错
    local root_existing
    root_existing=$(grep '^MYSQL_ROOT_PASSWORD=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
    if [ -z "$root_existing" ]; then
        local r tmp
        r=$(openssl rand -hex 24)
        if grep -q '^MYSQL_ROOT_PASSWORD=' "$ENV_FILE"; then
            tmp=$(mktemp "$ENV_FILE.XXXXXX")
            awk -v p="$r" -F= '
                $1=="MYSQL_ROOT_PASSWORD" { print "MYSQL_ROOT_PASSWORD=" p; next }
                { print }
            ' "$ENV_FILE" > "$tmp"
            mv "$tmp" "$ENV_FILE"
        else
            echo "MYSQL_ROOT_PASSWORD=$r" >> "$ENV_FILE"
        fi
        chmod 600 "$ENV_FILE"
        echo "ℹ️  MYSQL_ROOT_PASSWORD 已生成（仅数据卷首次初始化生效）"
    fi
}

# ── 前置检查：compose 与 MYSQL_HOST 是否一致 ──
# 已有 MySQL 的用户忘了删 compose 里的 mysql 服务块时会炸在 docker compose up：
#   ${MYSQL_ROOT_PASSWORD:?} 守卫缺失变量 → 直接失败；或 mysql 容器多起来但应用连的是外部库，
#   而文档里的备份命令 `docker compose exec mysql ... mysqldump` 会打到那个空库，备份到空数据。
check_compose_mysql() {
    [ -f docker-compose.yml ] || return 0
    grep -qE '^[[:space:]]+mysql:[[:space:]]*$' docker-compose.yml || return 0
    local host_val
    host_val=$(grep -E '^MYSQL_HOST=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
    host_val="${host_val:-mysql}"
    [ "$host_val" = "mysql" ] && return 0
    echo "❌ MYSQL_HOST=$host_val 指向外部数据库，但 docker-compose.yml 里还有内置 mysql 服务"
    echo ""
    echo "   这样 compose 会因为 MYSQL_ROOT_PASSWORD 未设置直接失败；"
    echo "   就算塞个假密码绕过，也会多起一个闲置的空 mysql 容器，"
    echo "   而文档里的备份命令会打到那个空库，把空数据当成备份存下来。"
    echo ""
    echo "   先从 docker-compose.yml 删掉三处，再重新部署："
    echo "     ① services 下的整个 mysql: 服务块"
    echo "     ② sites-nav 下的 depends_on 块"
    echo "     ③ 文件末尾的 volumes: mysql_data:"
    echo ""
    echo "   详见 DEPLOY.md → 用已有 MySQL"
    exit 1
}

# ── 端口检查 ──
check_port() {
    if command -v ss >/dev/null 2>&1; then
        if ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
            local pid
            pid=$(ss -ltnp 2>/dev/null | grep ":${PORT} " | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | head -1)
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
                echo "   警告: $(echo "$h" | sed -n 's/.*"data_warning":"\([^"]*\)".*/\1/p' | head -1)"
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
        setup_mysql_password
        check_compose_mysql
        check_port
        do_deploy
        ;;
    *)
        # 默认：交互式部署
        setup_env
        setup_password 1
        setup_mysql_password
        check_compose_mysql
        check_port
        do_deploy
        ;;
esac
