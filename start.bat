@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM sites-nav Windows 本地开发启动（热重载，等价于 python run.py）
REM 配置在 .env（参考 .env.example）；优先用本地虚拟环境，找不到用 PATH 上的 python

echo ============================================
echo  sites-nav 启动中（热重载模式）...
echo  访问地址: http://localhost:8000
echo  配置来源: .env（编辑 .env 后重跑生效）
echo  按 Ctrl+C 停止服务
echo ============================================

if exist "..\.venv\Scripts\python.exe" (
    "..\.venv\Scripts\python.exe" run.py
) else if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" run.py
) else (
    python run.py
)
