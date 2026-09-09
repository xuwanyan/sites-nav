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
        ENV_FILE.write_text("# 请改成强密码；留空 = 只读模式\nADMIN_PASSWORD=\n", encoding="utf-8")
        print(f"[OK] created .env (empty password, read-only mode)")
else:
    print(f"[OK] .env exists")

# 2. 检查管理密码
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
        print(f"[WARN] no admin password in .env, read-only mode")

# 3. 启动
print()
print("=" * 40)
print("  sites-nav starting...")
print("  URL: http://localhost:8000")
print("  Press Ctrl+C to stop")
print("=" * 40)
print()

import uvicorn

uvicorn.run("app:app", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")), reload=True)
