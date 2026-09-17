#!/usr/bin/env bash
# 镜像级冒烟：不启动/不停止服务，只验证镜像内容和本机 data/ 的可写性。
#
# 为什么需要这个脚本：deploy.sh 的 wait_healthy 只能看到"30 秒超时"，
# 看不到真正的失败原因。历史上出过两个 bug，症状都是"部署失败但报错方向完全错"：
#   1) Dockerfile 漏了 COPY toml_gen.py —— app.py 模块级 `from toml_gen import ...`，
#      容器启动即 ModuleNotFoundError 无限重启，日志里看到的是 MySQL 连接失败的方向。
#   2) data/ 由 root 创建（没走 bootstrap.sh 的 chown）—— 启动和 /health 都正常，
#      deploy.sh 照常打印"✅ 部署完成"，直到第一次新增站点才 500。
#
# 跑法：
#   ./scripts/smoke_docker.sh           # 镜像级冒烟：build + 只读检查 + 临时写入测试，不碰容器
#   ./scripts/smoke_docker.sh --start   # 额外做完整启动链路：build → up -d → curl /health → down
#
# 默认模式不 up/down 任何容器。--start 会真的起容器并在结束前收摊，所以有防护：
# 本项目容器已经在跑（线上）时拒绝执行 —— 否则结尾的 down 会把生产停掉。
# 已部署环境想验证健康度，直接 curl /health 或用 ./deploy.sh --deploy。

set -euo pipefail

cd "$(dirname "$0")/.."
PROJECT="${COMPOSE_PROJECT_NAME:-sites-nav}"
IMAGE="sites-nav:latest"
DATA_DIR="$(pwd)/data"
START=0
[ "${1:-}" = "--start" ] && START=1

fail() { echo "❌ $*"; exit 1; }

command -v docker >/dev/null 2>&1 || fail "未找到 docker"

# 读 .env 的 PORT（deploy.sh 同款极简解析，已有环境变量优先）
env_get() {
    [ -f .env ] || return 1
    sed -n "s/^$1=//p" .env | tail -1 | tr -d "'\""
}
PORT="${PORT:-$(env_get PORT || true)}"
PORT="${PORT:-8000}"

echo "🔍 1/4 构建镜像"
docker compose build --quiet || fail "docker compose build 失败"

echo "🔍 2/4 镜像内容：app.py 依赖的仓库内文件是否都在"
# app.py 模块级 `from toml_gen import ...`。漏任何一个都会让容器启动即挂，
# 而 wait_healthy 只会报超时，日志方向会带偏。
for f in app.py toml_gen.py static/index.html static/admin.html static/probes.html; do
    if ! docker run --rm --entrypoint ls "$IMAGE" "/app/$f" >/dev/null 2>&1; then
        fail "镜像里缺少 /app/$f（检查 Dockerfile 的 COPY）"
    fi
    echo "   ✓ /app/$f"
done

echo "🔍 3/4 容器内能 import 到 toml_gen（在 uvicorn 启动之前就能暴露问题）"
if ! docker run --rm --entrypoint python "$IMAGE" -c \
        "import toml_gen, app as a; print('   ✓ toml_gen + app 导入成功')" 2>/dev/null; then
    # app 导入需要 MySQL，连不上会 sys.exit —— 那种情况不判失败，单独提示
    echo "   ⚠️  app 导入需要 MySQL 可达；单独验证 toml_gen："
    docker run --rm --entrypoint python "$IMAGE" -c "import toml_gen; print('   ✓ toml_gen 导入成功')" \
        || fail "容器内 import toml_gen 失败 —— Dockerfile 漏了 COPY toml_gen.py"
fi

echo "🔍 4/4 data/ 可写性：容器以 app 用户身份能否写入"
mkdir -p "$DATA_DIR"
TEST_FILE=".smoke-write-$$"
# 用镜像里的 app 用户（非 root）试写，复现 bind mount 沿用宿主机权限的真实情况
if docker run --rm --user app -v "$DATA_DIR:/app/data" --entrypoint sh "$IMAGE" \
        -c "touch /app/data/$TEST_FILE && echo '   ✓ data/ 可写'" >/dev/null 2>&1; then
    rm -f "$DATA_DIR/$TEST_FILE"
else
    echo "   ❌ data/ 容器内不可写，绑定挂载沿用了宿主机权限"
    echo "      容器以非 root 用户运行（Dockerfile: USER app）"
    APP_UID="$(docker run --rm --user app --entrypoint id "$IMAGE" -u 2>/dev/null)"
    echo "      修复：sudo chown -R $APP_UID:$APP_UID $DATA_DIR"
    echo "      （或直接走 scripts/bootstrap.sh，它会自动 chown）"
    echo "      症状：启动和 /health 都正常，但新增/编辑站点会 500"
    exit 1
fi

echo ""
if [ "$START" -eq 0 ]; then
    echo "✅ 镜像级冒烟通过（未起容器）"
    echo "   完整启动链路验证：./scripts/smoke_docker.sh --start"
    echo "   正式部署：        ./deploy.sh --deploy"
    exit 0
fi

# ── 5/5 完整启动链路：up -d → curl /health → 收摊 ──
# 前 4 步只验证镜像和挂载，看不到「容器真起来后」的问题。历史上两个 bug
# （漏 COPY toml_gen.py、data/ 属主是 root）都必须真的起一次容器才暴露，
# 而 deploy.sh 的 wait_healthy 之前只会报「30 秒超时」，方向完全错。

# 防护：--start 结束时会 down 容器，本项目已经在跑时不能执行，否则停掉的是生产。
ALREADY_UP=$({ docker compose ps -q 2>/dev/null || true; } | wc -l)
if [ "${ALREADY_UP:-0}" -gt 0 ]; then
    echo ""
    echo "❌ --start 结束时会 down 掉本项目的容器，检测到 ${ALREADY_UP} 个正在运行，拒绝执行"
    echo "   已部署环境验证健康度请直接跑："
    echo "   curl -s http://127.0.0.1:${PORT}/health"
    exit 1
fi

# 无论成败都要收摊，不能把容器留在 running。绝不加 -v：mysql_data 是命名卷，
# down -v 会把数据库连同用户表和站点数据一起删掉。
cleanup_started() {
    echo ""
    echo "🧹 收摊中（数据卷保留，未 down -v）..."
    { docker compose down --remove-orphans 2>/dev/null || true; }
}
trap cleanup_started EXIT

echo "🔍 5/5 完整启动链路：up -d → curl /health"
docker compose up -d

BODY=$(mktemp)
HEALTHY=0
for i in $(seq 1 30); do
    code=$(curl -s -o "$BODY" -w '%{http_code}' "http://127.0.0.1:${PORT}/health" 2>/dev/null || true)
    if [ "$code" = "200" ]; then
        HEALTHY=1
        echo "   ✓ /health 返回 200（第 ${i} 秒）"
        if grep -q data_warning "$BODY" 2>/dev/null; then
            echo "   ⚠️ 但 /health 带了 data_warning："
            sed -n 's/.*"data_warning":"\([^"]*\)".*/      \1/p' "$BODY" | head -1
        fi
        break
    fi
    # 容器已响应但 /health 非 200：服务起来了但有问题，不用等满 30 秒
    if [ -n "$code" ] && [ "$code" != "000" ]; then
        echo "   ❌ /health 返回 $code（容器已响应，服务本身有问题）"
        echo "      响应体：$(cat "$BODY" 2>/dev/null)"
        break
    fi
    sleep 1
done
rm -f "$BODY"

if [ "$HEALTHY" -ne 1 ]; then
    echo ""
    echo "   ── 容器状态 ──"
    docker compose ps --format '     {{.Name}}  {{.State}}  {{.Status}}' 2>/dev/null || true
    echo "   ── 最近日志（最后 40 行）──"
    docker compose logs --no-color --tail=40 2>/dev/null | sed 's/^/     /' || true
    echo "   ── 完整日志: docker compose logs -f ──"
    fail "启动链路失败（容器已在收摊阶段 down 掉），按上面的日志定位"
fi

echo ""
echo "✅ 镜像级冒烟 + 完整启动链路全部通过"
echo "   服务已按设计收摊（数据卷保留）。正式部署：./deploy.sh --deploy"
