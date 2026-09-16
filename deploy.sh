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
#   ./deploy.sh --help       显示帮助
# ═══════════════════════════════════════════════════════

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
ENV_FILE="$DIR/.env"
DATA_DIR="$DIR/data"
MODE="${1:-}"

# ── 参数校验 ──
# 不校验会让拼错的参数（--depoly）落到默认分支走完整交互式部署：
# 改完密码、重建镜像才发现打错字。这里只放行显式列出的值，其余报错退出。
# 放在 check_prereqs 之前，这样 --help 和拼错参数都不需要 Docker 就绪。
usage() {
    cat <<'USAGE'
用法:
  ./deploy.sh              交互式部署（首次）
  ./deploy.sh --deploy     非交互式部署（.env 已配好）
  ./deploy.sh --update     拉取新镜像重启
  ./deploy.sh --stop       停止容器
  ./deploy.sh --status     查看状态
  ./deploy.sh --help       显示本帮助
USAGE
}

case "$MODE" in
    ""|--deploy|--update|--stop|--status|--help|-h) : ;;
    *)
        echo "❌ 未知参数: $MODE"
        usage
        exit 1
        ;;
esac

# ── 从 .env 读键值 ──
# 本文件是 set -euo pipefail：直接写 VAR="$(grep -E '^KEY=' file | head -1 | cut ...)"
# 在 KEY 不存在时 grep 返回 1 → 整个赋值失败 → 脚本静默退出一行不输出。
# 所有 .env 取值统一走这里。必须定义在任何取值之前（bash 按行执行，先定义后调用）。
env_get() {
    grep -E "^${1}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true
}

# 端口：shell 变量 > .env 里的 PORT > 默认 8000
# 与 docker-compose.yml 的 ${PORT:-8000} 同源，避免"检查的端口"和"实际绑定的端口"不一致。
# 注意：.env 没有 PORT 行时 grep 返回 1，本文件是 set -euo pipefail，
# 写成 PORT="${PORT:-$(grep ...)}" 会让赋值语句整体失败 → 脚本静默退出、一行输出都没有。
# 所以拆成显式判空 + || true。
PORT="${PORT:-}"
if [ -z "$PORT" ]; then
    PORT="$(env_get PORT)"
fi
PORT="${PORT:-8000}"
export PORT

# ── 内置 MySQL 开关 ──
# compose 里 mysql 服务挂在 profiles: ["builtin-mysql"] 上。MYSQL_HOST=mysql 时激活
# （起内置容器），否则不激活（用 .env 指向的已有实例）。这样切换两种部署方式
# 只改 .env 一个文件，不用手工编辑 docker-compose.yml。
# 必须在任何 docker compose 调用之前 export，否则 compose 看不到这个 profile。
MYSQL_HOST_VAL="$(env_get MYSQL_HOST)"
MYSQL_HOST_VAL="${MYSQL_HOST_VAL:-mysql}"
if [ "$MYSQL_HOST_VAL" = "mysql" ]; then
    export COMPOSE_PROFILES="builtin-mysql"
else
    # 必须显式 unset：COMPOSE_PROFILES 可能由调用方（bootstrap.sh 的 exec、
    # 用户的 shell、cron）留在环境里。只"该激活时激活"会让外部 MySQL 场景
    # 被父进程残留的 profile 多起一个空 mysql 容器，文档里的备份命令又会打到它。
    unset COMPOSE_PROFILES
fi

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
    existing="$(env_get ADMIN_PASSWORD)"
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
    host_val="$(env_get MYSQL_HOST)"
    host_val="${host_val:-mysql}"
    if [ "$host_val" != "mysql" ]; then
        echo "ℹ️  MYSQL_HOST=$host_val 指向外部数据库，请自行填写 MYSQL_USER / MYSQL_PASSWORD"
        return
    fi
    local existing
    existing="$(env_get MYSQL_PASSWORD)"
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
    # MYSQL_ROOT_PASSWORD 只在数据卷首次初始化时生效；缺失时 mysql 容器自己拒绝启动
    # （compose 里是 ${MYSQL_ROOT_PASSWORD:-}，不会在配置阶段报错）
    local root_existing
    root_existing="$(env_get MYSQL_ROOT_PASSWORD)"
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
    # build 与 up 分开，且 up 带 --force-recreate。
    # 原来用 `up -d --build`：镜像重建没问题，但 up 对"配置没变化"的服务会跳过重建，
    # .env 的改动不一定被当成配置变化 —— 而 CATEGRAF_TOKEN / MYSQL_* 等变量是
    # 进程启动时一次性读入的，容器不重建就永远是旧值。踩过的坑：.env 明明改对了、
    # 容器里的 printenv 也有值，前端却还显示未配置。
    # --force-recreate 保证每次部署都用新镜像 + 新 env。
    # 代价：内置 mysql（builtin-mysql profile 激活时）也会跟着重建一次。
    # 数据在 mysql_data 命名卷里不受影响，只是多一次重启；mysql 的 env 本来就只在
    # 首次初始化生效（见 docker-compose.yml），重建对它无害。
    docker compose build
    docker compose up -d --force-recreate
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
    echo "🔄 重建并重启..."
    # 同 do_deploy：必须 --force-recreate，否则 .env 的改动不会生效。
    # 这里不 git pull —— 镜像从当前目录构建，要更新版本得先自己 git pull。
    docker compose pull 2>/dev/null || docker compose build
    docker compose up -d --force-recreate
    wait_healthy
    echo "✅ 更新完成"
}

# ── 停止 ──
do_stop() {
    # --remove-orphans：从"内置 mysql"切到"已有 MySQL"后，profile 不再激活，
    # 但之前起过的 mysql 容器仍在跑且已不在当前 compose 配置里。不带这个参数时
    # down 只停 sites-nav，那个空库容器会一直挂着，文档里的备份命令又会打到它。
    # mysql_data 卷不受影响（没带 -v）。
    docker compose down --remove-orphans
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
    --help|-h)
        usage
        ;;
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
        check_port
        do_deploy
        ;;
    "")
        # 无参数：交互式部署（未知参数已在上方校验拦截，不会再落到这里）
        setup_env
        # stdin 不是终端时 read 会直接 EOF 失败，本文件是 set -e → 脚本在这里静默退出，
        # 终端上只剩一行残缺的密码提示，看不出发生了什么。退回非交互路径并说明。
        INTERACTIVE=1
        if ! [ -t 0 ]; then
            INTERACTIVE=0
            echo "ℹ️  非交互环境（stdin 不是终端），跳过密码交互；首次启动会生成随机密码并打印一次"
        fi
        setup_password "$INTERACTIVE"
        setup_mysql_password
        check_port
        do_deploy
        ;;
esac
