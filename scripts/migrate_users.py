#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性把旧版 data/users.json（或 users.json.bak）导入 MySQL。

用户与权限从 JSON 文件迁到 MySQL 时用。bcrypt / pbkdf2 哈希原样搬运，**密码不变、无需重设**。
迁移后旧 token 全部失效（token 签名密钥是进程内随机生成的，重启本来就会轮换，属预期行为）。

用法:
    python scripts/migrate_users.py data/users.json
    python scripts/migrate_users.py data/users.bak
    python scripts/migrate_users.py data/users.bak --replace   # 同名用户改为覆盖
    python scripts/migrate_users.py data/users.json --dry-run  # 只看会做什么

数据库连接读 .env 里的 MYSQL_*（与 app.py 完全同一套配置）。
表必须已存在：先正常启动一次应用让它建表，再跑本脚本。
"""
import argparse
import contextlib
import json
import os
import secrets
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))


def _load_env_file(path: Path) -> None:
    """与 app.py 同款极简 .env 加载：已有环境变量优先"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file(BASE / ".env")

import pymysql  # noqa: E402

HOST = os.environ.get("MYSQL_HOST", "").strip()
PORT = int(os.environ.get("MYSQL_PORT", "3306") or "3306")
USER = os.environ.get("MYSQL_USER", "sites_nav").strip()
PASSWORD = os.environ.get("MYSQL_PASSWORD", "").strip()
DATABASE = os.environ.get("MYSQL_DATABASE", "sites_nav").strip()

COLS = ("id", "username", "password_hash", "role", "enabled", "pwd_epoch", "created_at", "updated_at")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _valid_id(v) -> bool:
    return isinstance(v, str) and len(v) == 8 and all(c in "0123456789abcdef" for c in v)


def main() -> int:
    ap = argparse.ArgumentParser(description="把旧版 users.json 导入 MySQL")
    ap.add_argument("file", help="旧版 users.json 或 users.json.bak")
    ap.add_argument("--replace", action="store_true", help="同名用户改为覆盖（默认跳过并提示）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的操作，不写入")
    args = ap.parse_args()

    path = Path(args.file) if Path(args.file).is_absolute() else BASE / args.file
    if not path.exists():
        print(f"[fatal] 文件不存在: {path}")
        return 1
    if not HOST:
        print("[fatal] .env 里没有 MYSQL_HOST，无法连接数据库")
        return 1

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[fatal] 读取失败: {exc}")
        return 1
    if not isinstance(raw, list):
        print("[fatal] 文件格式不对：应为用户数组")
        return 1

    users = [u for u in raw if isinstance(u, dict) and u.get("username")]
    if not users:
        print(f"文件里没有可用用户记录: {path}")
        return 0
    print(f"读到 {len(users)} 个用户，连接 MySQL {HOST}:{PORT}/{DATABASE} ...")

    try:
        conn = pymysql.connect(host=HOST, port=PORT, user=USER, password=PASSWORD, database=DATABASE,
                               charset="utf8mb4", autocommit=False, connect_timeout=5,
                               cursorclass=pymysql.cursors.DictCursor)
    except pymysql.MySQLError as exc:
        print(f"[fatal] 连接失败: {exc}")
        return 1

    added, replaced, skipped = 0, 0, 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = 'users'")
            if not cur.fetchone()["n"]:
                print("[fatal] users 表不存在：先正常启动一次应用让它建表，再跑本脚本")
                return 1

            for u in users:
                uname = str(u["username"]).strip()
                pwd_hash = str(u.get("password_hash") or "")
                if not pwd_hash:
                    print(f"  跳过 {uname}: 记录里没有 password_hash")
                    skipped += 1
                    continue
                # 哈希必须是 bcrypt（$2a/$2b/$2y$）或本应用自建的 pbkdf2$ 格式
                if not (pwd_hash.startswith("$2") or pwd_hash.startswith("pbkdf2$")):
                    print(f"  跳过 {uname}: password_hash 格式无法识别，无法校验")
                    skipped += 1
                    continue
                uid = u["id"] if _valid_id(u.get("id")) else secrets.token_hex(4)
                role = "admin" if str(u.get("role")) == "admin" else "user"
                enabled = 1 if u.get("enabled", True) else 0
                pwd_epoch = int(u.get("pwd_epoch", 0) or 0)
                created_at = u.get("created_at") or _now()
                updated_at = u.get("updated_at") or _now()

                cur.execute("SELECT id FROM users WHERE LOWER(username) = LOWER(%s)", (uname,))
                hit = cur.fetchone()
                if hit:
                    if not args.replace:
                        print(f"  跳过 {uname}: 已存在（用 --replace 覆盖）")
                        skipped += 1
                        continue
                    if not args.dry_run:
                        cur.execute(
                            "UPDATE users SET password_hash = %s, role = %s, enabled = %s, "
                            "pwd_epoch = pwd_epoch + 1, updated_at = %s WHERE id = %s",
                            (pwd_hash, role, enabled, updated_at, hit["id"]))
                    replaced += 1
                    print(f"  覆盖 {uname}: 密码哈希已更新（旧 token 失效）")
                    continue
                if not args.dry_run:
                    cur.execute(
                        "INSERT INTO users (id, username, password_hash, role, enabled, pwd_epoch, "
                        "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (uid, uname, pwd_hash, role, enabled, pwd_epoch, created_at, updated_at))
                added += 1
                print(f"  导入 {uname} ({role}, {'启用' if enabled else '禁用'})")

            if not args.dry_run:
                conn.commit()
    except pymysql.MySQLError as exc:
        with contextlib.suppress(Exception):
            conn.rollback()
        print(f"[fatal] 写入失败（已回滚）: {exc}")
        return 1
    finally:
        conn.close()

    print(f"\n完成：导入 {added}  覆盖 {replaced}  跳过 {skipped}"
          + ("（dry-run，未写入）" if args.dry_run else ""))
    if args.dry_run:
        return 0
    print("密码无需重设。请让应用重启一次，旧 token 会全部失效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
