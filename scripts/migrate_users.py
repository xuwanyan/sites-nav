#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性把旧版 data/users.json（或 users.json.bak）导入 MySQL。

用户与权限从 JSON 文件迁到 MySQL 时用。bcrypt / pbkdf2 哈希原样搬运，**密码不变、无需重设**。
迁移后旧 token 全部失效（token 签名密钥是进程内随机生成的，重启本来就会轮换，属预期行为）。

用法:
    python scripts/migrate_users.py data/users.json --dry-run    # 只看会做什么
    python scripts/migrate_users.py data/users.json --yes        # 真正写入
    python scripts/migrate_users.py data/users.bak --replace --yes

写库前会做三段 pre-flight，任何一段不过都不会碰数据库：
  1. 源文件：用户名/id 文件内重复（唯一键大小写不敏感，"Admin" 与 "admin" 也算重复）、
     id 格式、pwd_epoch 是否为整数、created_at/updated_at 是否为 DATETIME 字面量、
     password_hash 长度与前缀、bcrypt 哈希是否全 ASCII。
     —— 哈希长度超 VARCHAR(128) 或非 ASCII 这两项写进去就是「该用户永久无法登录」，
        而且不会报错，必须提前拦。
  2. 数据库：可达性 + users 表结构（按 app.py 的 DDL 逐列核对，连错库也在这里暴露）。
  3. 计划：逐条算出新增/覆盖/跳过并打印，同时查 id 与库里现有记录的主键冲突。

真实写入必须显式带 --yes。不是交互确认（stdin 可能不是 TTY），是强制的显式开关：
脚本只跑一次、写入不可逆，宁可多打一遍命令。

数据库连接读 .env 里的 MYSQL_*（与 app.py 完全同一套配置）。
表必须已存在：先正常启动一次应用让它建表，再跑本脚本。
"""
import argparse
import contextlib
import json
import os
import re
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

# 与 app.py 的 CREATE TABLE users 对齐。pre-flight 用它核对目标表结构，
# 而不是假设「users 表存在」就等于「列都对」。
COLS = ("id", "username", "password_hash", "role", "enabled", "pwd_epoch", "created_at", "updated_at")
DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _valid_id(v) -> bool:
    return isinstance(v, str) and len(v) == 8 and all(c in "0123456789abcdef" for c in v)


def _parse_datetime(v, uname: str, field: str, errors: list) -> str:
    """created_at / updated_at 必须是 DATETIME 字面量。

    源文件里常见 ISO 形式（2024-01-02T03:04:05）或时间戳，直接写进 DATETIME 列
    要么被 MySQL 拒绝、要么（非严格模式）被写成 0000-00-00 00:00:00 脏数据。
    这是纯元数据，不认识就退回当前时间并提示，不算致命错误。"""
    s = str(v).strip() if v is not None else ""
    if DATETIME_RE.match(s):
        return s
    if s:
        print(f"  提示 {uname}: {field}={s!r} 不是 'YYYY-MM-DD HH:MM:SS'，已改用当前时间")
    return _now()


def _validate_hash(uname: str, pwd_hash: str, errors: list) -> bool:
    """哈希能不能被 app.py 认出来。认不出来 = 该用户迁移后**永远登录不上**。

    这里查的三件事都是「写进去就再也回不来」的：
      - 长度 > 128：password_hash 是 VARCHAR(128)。严格模式报错整批回滚；
        非严格模式静默截断成半个哈希，用户永久锁死且日志里看不出原因。
      - bcrypt 哈希含非 ASCII：_verify_password 走 hash_str.encode("ascii")，
        UnicodeEncodeError 会被当成校验失败，同样永久锁死。
      - 前缀不是 $2* / pbkdf2$：两种算法都不认。
    """
    if len(pwd_hash) > 128:
        errors.append(f"用户 {uname!r}: password_hash 长 {len(pwd_hash)} 字符，超过 VARCHAR(128)，"
                      f"会被截断成非法哈希 → 永久无法登录")
        return False
    if not (pwd_hash.startswith("$2") or pwd_hash.startswith("pbkdf2$")):
        errors.append(f"用户 {uname!r}: password_hash 格式无法识别（应为 bcrypt $2* 或 pbkdf2$），"
                      f"无法校验 → 永久无法登录")
        return False
    if pwd_hash.startswith("$2"):
        try:
            pwd_hash.encode("ascii")
        except UnicodeEncodeError:
            errors.append(f"用户 {uname!r}: bcrypt 哈希含非 ASCII 字符，校验时会当密码错处理")
            return False
    return True


def read_source(path: Path) -> tuple[list, list, list]:
    """读取并解析源文件 → (待写入记录, 致命错误, 跳过的说明)。

    致命错误必须在连数据库之前就报出来：这个脚本只跑一次、写入不可逆，
    不该走到写库阶段才被 UNIQUE 约束打断。
    """
    errors: list = []
    notes: list = []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return [], [f"读取失败: {exc}"], []
    if not isinstance(raw, list):
        return [], ["文件格式不对：应为用户数组"], []

    rows = [(i, u) for i, u in enumerate(raw) if isinstance(u, dict) and u.get("username")]

    # 第一遍只做查重，独立成一遍。边查边剔除会漏报：第 2 行因重名被剔后它占用的 id
    # 不再登记，第 3 行的重复 id 就检不出来，用户得修一次、重跑一次才看得到下一个问题。
    name_first: dict = {}   # username(小写) -> 首次出现的行号
    id_first: dict = {}     # id -> 首次出现的行号
    dup_rows: set = set()
    for i, u in rows:
        uname = str(u["username"]).strip()
        row = f"第 {i + 1} 行"
        # uk_username 建立在 utf8mb4_unicode_ci 上，大小写不敏感；按字面比会漏掉
        # "Admin" / "admin"，第 2 条撞唯一索引、整批回滚。
        hit = name_first.get(uname.lower())
        if hit:
            errors.append(f"{row} 用户 {uname!r} 与第 {hit} 行重名（唯一键大小写不敏感）"
                          f"→ 撞 uk_username，整批回滚。保留第 {hit} 行")
            dup_rows.add(i)
        else:
            name_first[uname.lower()] = i + 1
        uid = u["id"] if _valid_id(u.get("id")) else None
        if uid:
            hit = id_first.get(uid)
            if hit:
                errors.append(f"{row} 用户 {uname!r} 的 id={uid} 与第 {hit} 行相同 → 撞主键，整批回滚")
                dup_rows.add(i)
            else:
                id_first[uid] = i + 1

    # 第二遍：校验字段并生成待写入记录
    out: list = []
    for i, u in rows:
        if i in dup_rows:
            continue
        uname = str(u["username"]).strip()
        row = f"第 {i + 1} 行"
        uid = u["id"] if _valid_id(u.get("id")) else secrets.token_hex(4)

        pwd_hash = str(u.get("password_hash") or "")
        if not pwd_hash:
            notes.append(f"{row} 跳过 {uname}: 记录里没有 password_hash")
            continue
        if not _validate_hash(uname, pwd_hash, errors):
            notes.append(f"{row} 跳过 {uname}: password_hash 不可用")
            continue

        try:
            pwd_epoch = int(u.get("pwd_epoch", 0) or 0)
        except (TypeError, ValueError):
            errors.append(f"{row} 用户 {uname!r}: pwd_epoch={u.get('pwd_epoch')!r} 不是整数，无法写入 INT 列")
            continue

        out.append({
            "uname": uname, "uid": uid, "pwd_hash": pwd_hash,
            "role": "admin" if str(u.get("role")) == "admin" else "user",
            "enabled": 1 if u.get("enabled", True) else 0,
            "pwd_epoch": pwd_epoch,
            "created_at": _parse_datetime(u.get("created_at"), uname, "created_at", errors),
            "updated_at": _parse_datetime(u.get("updated_at"), uname, "updated_at", errors),
        })
    return out, errors, notes


def check_schema(cur) -> list:
    """核对 users 表的列，返回缺列/多余列说明（空列表表示结构对齐）。

    原来只查「表存在」，但表存在不等于列对：手工建过表、或连错了库，
    都是表在但 INSERT 到一半才报 Unknown column。"""
    cur.execute(
        "SELECT COLUMN_NAME FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = 'users'")
    cols = {r["COLUMN_NAME"] for r in cur.fetchall()}
    missing = [c for c in COLS if c not in cols]
    if missing:
        return [f"users 表缺列 {missing}：表结构与应用不匹配，先确认连的是不是 sites_nav 库、"
                f"或先跑一次应用让它按最新 DDL 建表"]
    return []


def build_plan(cur, users: list, replace: bool) -> tuple[list, list]:
    """对比库里现状，算出每条记录的最终动作 → (计划, 致命错误)。

    计划里只有 write / skip 两种；写事务循环只负责执行，不再做判断。
    """
    errors: list = []
    cur.execute("SELECT id, username, role FROM users")
    by_uid = {r["id"]: r["username"] for r in cur.fetchall()}

    plan: list = []
    for u in users:
        # id 撞库：源文件里 id 合法但库里已有同一主键，且不是同一个人 → 撞主键。
        # 这一步必须独立于「用户名是否已存在」判断：新用户名 + 复用别人的 id 是最
        # 容易踩的写法，嵌套在用户名命中分支里会完全查不出来。比较按小写，对齐
        # uk_username 的大小写不敏感。
        owner = by_uid.get(u["uid"])
        if owner is not None and owner.lower() != u["uname"].lower():
            errors.append(
                f"用户 {u['uname']!r} 的 id={u['uid']} 已被「{owner!r}」占用 → 撞主键。"
                f"把该条的 id 改成别的 8 位十六进制，或删掉 id 字段让脚本随机生成")
            continue
        cur.execute("SELECT id, role FROM users WHERE LOWER(username) = LOWER(%s)", (u["uname"],))
        r = cur.fetchone()
        if r:
            if replace:
                plan.append(("write", u, f"覆盖已有用户（{r['role']}，旧 token 失效）"))
            else:
                plan.append(("skip", u, "已存在（用 --replace 覆盖）"))
        else:
            plan.append(("write", u, f"新增（{u['role']}，{'启用' if u['enabled'] else '禁用'}）"))
    return plan, errors


def main() -> int:
    # 注意：print 里不要用 emoji（🔍 ✅ ❌ ✍️ 等）。这是 Python 脚本，Windows 上
    # stdout 编码是 GBK/cp936，emoji 不在 GBK 里 → UnicodeEncodeError，整个脚本崩掉
    # （本仓库的 bash 脚本可以随便用，那边是 UTF-8）。用 [check] [ok] [err] [fatal]
    # 这类 ASCII 标记，和 app.py 的 [seed] 保持一致。
    ap = argparse.ArgumentParser(description="把旧版 users.json 导入 MySQL")
    ap.add_argument("file", help="旧版 users.json 或 users.json.bak")
    ap.add_argument("--replace", action="store_true", help="同名用户改为覆盖（默认跳过并提示）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的操作，不写入")
    ap.add_argument("--yes", action="store_true",
                    help="确认写入。真实写入必须显式带这个：脚本只跑一次、写入不可逆")
    args = ap.parse_args()

    path = Path(args.file) if Path(args.file).is_absolute() else BASE / args.file
    if not path.exists():
        print(f"[fatal] 文件不存在: {path}")
        return 1

    # ── pre-flight 1/3：源文件本身（不需要数据库，错了就别连库）──
    print(f"[check] pre-flight 1/3：解析源文件 {path.name}")
    users, errors, notes = read_source(path)
    for n in notes:
        print(f"  {n}")
    if errors:
        for e in errors:
            print(f"  [err] {e}")
        print(f"\n[fatal] 源文件有 {len(errors)} 个致命问题，未连数据库、未写入。修好后重跑。")
        return 1
    if not users:
        print("[skip] 文件里没有可用用户记录（或全部被跳过），退出")
        return 0
    print(f"  [ok] {len(users)} 条可用记录")

    if not HOST:
        print("[fatal] .env 里没有 MYSQL_HOST，无法连接数据库")
        return 1

    # ── pre-flight 2/3：数据库可达 + 表结构 ──
    print(f"[check] pre-flight 2/3：连接 MySQL {HOST}:{PORT}/{DATABASE}，核对 users 表结构")
    try:
        conn = pymysql.connect(host=HOST, port=PORT, user=USER, password=PASSWORD, database=DATABASE,
                               charset="utf8mb4", autocommit=False, connect_timeout=5,
                               cursorclass=pymysql.cursors.DictCursor)
    except pymysql.MySQLError as exc:
        print(f"[fatal] 连接失败: {exc}")
        return 1

    skips = 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = 'users'")
            if not cur.fetchone()["n"]:
                print("[fatal] users 表不存在：先正常启动一次应用让它建表，再跑本脚本")
                return 1
            schema_err = check_schema(cur)
            if schema_err:
                print(f"  [err] {schema_err[0]}")
                print("[fatal] 表结构不匹配，未写入")
                return 1
            print("  [ok] users 表结构与应用对齐")

            # ── pre-flight 3/3：对比现状，算出计划并查冲突 ──
            print("[check] pre-flight 3/3：与库里现状对比，生成写入计划")
            plan, errors = build_plan(cur, users, args.replace)
            if errors:
                for e in errors:
                    print(f"  [err] {e}")
                print("[fatal] 存在主键冲突，未写入")
                return 1
            for act, u, why in plan:
                skips += act == "skip"
                print(f"  {'>' if act == 'write' else '.'} {u['uname']:24} {why}")

            writes = [p for p in plan if p[0] == "write"]
            print(f"\n计划：写入 {len(writes)}（含覆盖）  跳过 {skips}")
            if not writes:
                print("没有需要写入的记录，退出")
                return 0
            if args.dry_run:
                print("\n（--dry-run，未写入）")
                return 0
            if not args.yes:
                print("\n[fatal] 真实写入需要显式确认，数据库未被改动。重跑时加 --yes：")
                print(f"        python scripts/migrate_users.py {args.file} --yes"
                      + (" --replace" if args.replace else ""))
                return 1

            # ── 执行：只跑计划，不再做判断 ──
            print(f"\n[write] 写入 {len(writes)} 条 ...")
            added = replaced = 0
            for _, u, _why in writes:
                # 执行时再查一次，不信任计划：计划算完到写入之间库可能被改过
                cur.execute("SELECT id FROM users WHERE LOWER(username) = LOWER(%s)", (u["uname"],))
                hit = cur.fetchone()
                if hit:
                    cur.execute(
                        "UPDATE users SET password_hash = %s, role = %s, enabled = %s, "
                        "pwd_epoch = pwd_epoch + 1, updated_at = %s WHERE id = %s",
                        (u["pwd_hash"], u["role"], u["enabled"], u["updated_at"], hit["id"]))
                    replaced += 1
                    print(f"  覆盖 {u['uname']}: 密码哈希已更新（旧 token 失效）")
                else:
                    cur.execute(
                        "INSERT INTO users (id, username, password_hash, role, enabled, pwd_epoch, "
                        "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (u["uid"], u["uname"], u["pwd_hash"], u["role"], u["enabled"],
                         u["pwd_epoch"], u["created_at"], u["updated_at"]))
                    added += 1
                    print(f"  导入 {u['uname']} ({u['role']}, {'启用' if u['enabled'] else '禁用'})")
            conn.commit()
    except pymysql.MySQLError as exc:
        with contextlib.suppress(Exception):
            conn.rollback()
        print(f"[fatal] 写入失败（已回滚，数据库未被改动）: {exc}")
        return 1
    finally:
        conn.close()

    print(f"\n[done] 完成：导入 {added}  覆盖 {replaced}  跳过 {skips}")
    print("密码无需重设。请让应用重启一次，旧 token 会全部失效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
