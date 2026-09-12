#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 本地调试启动脚本 — python run.py"""
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
os.chdir(BASE)  # reload 子进程继承调用方 cwd，显式切到项目目录

ENV_FILE = BASE / ".env"

# 1. 确保 .env 存在
if not ENV_FILE.exists():
    example = BASE / ".env.example"
    if example.exists():
        ENV_FILE.write_bytes(example.read_bytes())
        print(f"[OK] created .env from .env.example")
    else:
        ENV_FILE.write_text(
            "# 首次启动种子 admin 的密码；留空则自动生成随机密码并打印一次\nADMIN_PASSWORD=\n",
            encoding="utf-8")
        print(f"[OK] created .env (password empty, generated on first start)")
else:
    print(f"[OK] .env exists")

# 2. 检查管理密码（仅用于首次启动种子 admin 账号，之后在后台「用户管理」里维护）
with ENV_FILE.open("r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line.startswith("ADMIN_PASSWORD="):
            pwd = line.split("=", 1)[1]
            if pwd and pwd != "PleaseChangeMe":
                print(f"[OK] admin password is set")
                os.environ["ADMIN_PASSWORD"] = pwd
                break
    else:
        print(f"[WARN] no admin password in .env, a random one will be generated on first start")

# 3. 检查 MySQL（用户与权限存储，必填）
_mh = os.environ.get("MYSQL_HOST", "").strip()
if not _mh:
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("MYSQL_HOST="):
            _mh = line.split("=", 1)[1].strip()
            break
if not _mh:
    print("[WARN] MYSQL_HOST 未设置：用户与权限已迁移到 MySQL，应用启动会中止。")
    print("       本地开发可先跑 `docker compose up -d mysql`，或在 .env 填 MYSQL_* 指向已有实例。")

# 4. 启动
print()
print("=" * 40)
print("  sites-nav starting...")
print("  URL: http://localhost:8000")
print("  Press Ctrl+C to stop")
print("=" * 40)
print()

import uvicorn

# Windows 上 multiprocessing 用 spawn，子进程会把本文件当 __mp_main__ 重新 import；
# 没有 __main__ 保护的话顶层 uvicorn.run 会被执行第二次 → RuntimeError
# （"An attempt has been made to start a new process before the current process
#  has finished its bootstrapping phase"）
if __name__ == "__main__":
    uvicorn.run("app:app", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")), reload=True)
