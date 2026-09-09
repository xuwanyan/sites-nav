@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM ===== 本地开发启动脚本 =====
REM 配置项在 .env 里（参考 .env.example）；要临时覆盖可在下面 set
REM set ADMIN_PASSWORD=xxx        REM 覆盖管理密码
REM set CATEGRAF_ADMIN_URL=      REM 覆盖拨测联动地址
REM set HOST=0.0.0.0             REM 对外暴露（默认 127.0.0.1）
REM set PORT=8000                REM 改端口

echo ============================================
echo  sites-nav 启动中...
echo  访问地址: http://%HOST%:8000
echo  配置来源: .env（编辑 .env 后重跑生效）
echo  按 Ctrl+C 停止服务
echo ============================================

c:\vscode\.venv\Scripts\python.exe app.py
