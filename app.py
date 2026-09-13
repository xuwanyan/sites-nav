import contextlib
import csv
import datetime as _dt
import functools
import hashlib
import hmac
import io
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Literal

import pymysql
import requests
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, field_validator

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_FILE = DATA_DIR / "sites.json"
BACKUP_FILE = DATA_DIR / "sites.json.bak"
# 备份轮转：保留最近 3 份（.bak / .bak.1 / .bak.2），超出删除
BACKUP_ROLL = 3
_backup_files = [DATA_DIR / f"sites.json.bak.{i}" for i in range(BACKUP_ROLL - 1, -1, -1)]  # [.bak.2, .bak.1, .bak]
# 数据恢复警告：从备份加载过就标记，/health 可见
_data_warning = ""


def _load_env_file(path: Path) -> None:
    """极简 .env 加载（KEY=VALUE，# 注释）：已有环境变量优先，不被覆盖。
    让裸跑 uvicorn/run.py/start.bat 与 docker compose 共用同一份 .env 配置"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file(BASE_DIR / ".env")

# 管理密码：仅用于首次启动种子 admin 账号（用户表为空时）。
# 种子后 MySQL users 表是唯一真相源，此变量不再被读取。
# 已知占位符视为未配置，防止 .env.example / Docker 路径下的默认密码成为可用凭证
_PLACEHOLDER_PASSWORDS = {"", "PleaseChangeMe", "changeme", "change_me", "password", "123456", "admin", "admin123"}
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
if ADMIN_PASSWORD and ADMIN_PASSWORD.strip().lower() in _PLACEHOLDER_PASSWORDS:
    ADMIN_PASSWORD = ""

# token 签名密钥：进程内随机，重启即轮换（所有已发 token 失效，安全特性）。
# 多实例不共享 —— 单实例部署是硬约束（见 DEPLOY.md）
TOKEN_SECRET = secrets.token_hex(32)
# 会话有效期：.env 的 TOKEN_TTL_HOURS，默认 12 小时；非法值回退 12h
TOKEN_TTL = int(os.environ.get("TOKEN_TTL_HOURS", "12") or "12") * 3600

# 拨测联动（categraf-http-admin）：不配置 CATEGRAF_ADMIN_URL 时功能整体关闭
CATEGRAF_ADMIN_URL = os.environ.get("CATEGRAF_ADMIN_URL", "").rstrip("/")
CATEGRAF_ADMIN_USER = os.environ.get("CATEGRAF_ADMIN_USER", "admin")
CATEGRAF_ADMIN_PASS = os.environ.get("CATEGRAF_ADMIN_PASS", "")

# 同步结果内存态：site_id -> {"ok": bool, "message": str, "time": str}，重启即清零
_sync_status: dict = {}

# 数据文件读写锁：防止并发请求读改写丢数据（RLock 允许 _load 内部调用 _save）
_file_lock = threading.RLock()

# ── 用户与权限存储：MySQL ──
# 站点数据仍在 data/sites.json（单文件，备份即复制）；用户/角色/启用状态在 MySQL。
# 换库的原因：users.json 损坏时应用会静默全员锁死（_load_users 遇损坏返回空列表，
# 而 _seed_admin_if_needed 见文件存在就跳过），没有任何提示。MySQL 不存在"整个文件坏掉"。
MYSQL_HOST = os.environ.get("MYSQL_HOST", "").strip()
MYSQL_PORT = int(os.environ.get("MYSQL_PORT", "3306") or "3306")
MYSQL_USER = os.environ.get("MYSQL_USER", "sites_nav").strip()
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "").strip()
MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE", "sites_nav").strip()
MYSQL_CONNECT_TIMEOUT = int(os.environ.get("MYSQL_CONNECT_TIMEOUT", "3") or "3")

# 每次请求开短连接、用完即关，不做连接池：单实例低流量，池里过期连接/连接泄漏的
# 排查成本高于省下的毫秒（LAN 内建连约 1ms）。_verify_token 每个 API 请求查一次，可接受。
_MYSQL_KWARGS = dict(
    host=MYSQL_HOST,
    port=MYSQL_PORT,
    user=MYSQL_USER,
    password=MYSQL_PASSWORD,
    database=MYSQL_DATABASE,
    charset="utf8mb4",
    autocommit=False,
    connect_timeout=MYSQL_CONNECT_TIMEOUT,
    read_timeout=10,
    write_timeout=10,
    cursorclass=pymysql.cursors.DictCursor,
)

app = FastAPI(title="内部系统导航")


def _cleanup_old_backups() -> None:
    """启动时清理超出 BACKUP_ROLL 的旧备份文件（如 .bak.3、.bak.4 等）"""
    import re
    if not DATA_DIR.exists():
        return
    for f in DATA_DIR.iterdir():
        m = re.match(r"sites\.json\.bak\.(\d+)$", f.name)
        if m and int(m.group(1)) >= BACKUP_ROLL:
            try:
                f.unlink()
            except OSError:
                pass


_cleanup_old_backups()


class SiteIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: str = Field(default="网站", max_length=30)  # 资源类型：网站 / 缓存 / 消息队列 / 数据库 / 存储 / 任意自定义
    category: str = Field(default="", max_length=50)
    public_url: str = Field(default="", max_length=500)
    private_url: str = Field(default="", max_length=500)
    domain: str = Field(default="", max_length=200)
    connection: str = Field(default="", max_length=500)  # 非 URL 资源的连接串（Redis/MQ/DB/LDAP/自定义）
    owner: str = Field(default="", max_length=100)
    env: Literal["生产环境", "测试环境"]  # 环境必填，且只允许生产环境/测试环境
    remark: str = Field(default="", max_length=500)
    monitor: bool = False
    # 拨测参数：留空用默认（状态码 200、不设置超时）；状态码多个用 | 分隔，超时如 3s/500ms/1m（纯数字自动按秒）
    probe_status_codes: str = Field(default="", pattern=r"^(\d{3}(\|\d{3})*)?$")
    probe_timeout: str = Field(default="", pattern=r"^(\d+(ms|s|m))?$")

    @field_validator("probe_timeout", mode="before")
    @classmethod
    def _norm_probe_timeout(cls, v):
        """超时时长纯数字自动按秒补单位（3 -> 3s），防止漏写 s"""
        v = v.strip() if isinstance(v, str) else v
        if isinstance(v, str) and v.isdigit():
            return f"{v}s"
        return v

    @field_validator("public_url", "private_url", "domain", mode="before")
    @classmethod
    def _norm_url_field(cls, v):
        """URL 字段：拒绝控制字符和引号（防止 attribute 注入），校验 http(s):// + netloc"""
        v = (v or "").strip()
        if not v:
            return v
        if any(c.isspace() or c in '"\'<>`' for c in v):
            raise ValueError("URL 含非法字符（空格/引号/尖括号）")
        from urllib.parse import urlparse
        u = v if v.startswith(("http://", "https://")) else "http://" + v
        p = urlparse(u)
        if p.scheme not in ("http", "https") or not p.netloc:
            raise ValueError("URL 格式不合法，应为 http(s)://host[/path]")
        return v

    @field_validator("connection", "kind", "category", "name", "owner", "remark", mode="before")
    @classmethod
    def _reject_control_chars(cls, v):
        """拒绝控制字符（< 0x20）：防 JSON/CSV 破坏、防跨行注入。允许引号和尖括号（正常内容）"""
        v = v.strip() if isinstance(v, str) else v
        if isinstance(v, str):
            if any(ord(c) < 32 for c in v):
                raise ValueError("字段含非法控制字符")
        return v


FIELD_DEFAULTS = {
    "name": "",
    "kind": "网站",
    "category": "",
    "public_url": "",
    "private_url": "",
    "domain": "",
    "connection": "",
    "owner": "",
    "env": "",
    "remark": "",
    "monitor": False,
    "probe_status_codes": "",
    "probe_timeout": "",
    "created_at": "",
    "updated_at": "",
}


def _load() -> list:
    with _file_lock:
        return _load_unlocked()


def _load_unlocked() -> list:
    global _data_warning
    if not DATA_FILE.exists():
        return []
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        raw = None
    if raw is None:
        # 主文件损坏：依次尝试 .bak → .bak.1 → .bak.2，找到就恢复
        recovered_from = None
        for i in range(BACKUP_ROLL):
            bk = BACKUP_FILE if i == 0 else DATA_DIR / f"sites.json.bak.{i}"
            if bk.exists():
                try:
                    candidate = json.loads(bk.read_text(encoding="utf-8"))
                    if isinstance(candidate, list):
                        raw = candidate
                        recovered_from = bk.name
                        break
                except (json.JSONDecodeError, OSError):
                    continue
        if recovered_from:
            _data_warning = f"主数据文件损坏，已从备份 {recovered_from} 恢复"
            # 强制重写主文件，下次不再走备份路径
            try:
                _save_unlocked([{**FIELD_DEFAULTS, **item} for item in raw if isinstance(item, dict) and item.get("name")])
            except OSError:
                pass
        else:
            _data_warning = "主数据文件和所有备份均损坏，返回空数据"
    # 加载时校验/修复 id：data 文件可能被手改，防止非法 id 注入 onclick
    sites = [{
        **FIELD_DEFAULTS,
        **item,
        "id": item.get("id") if isinstance(item.get("id"), str) and len(item.get("id")) == 8 and all(c in "0123456789abcdef" for c in item.get("id")) else secrets.token_hex(4),
    } for item in raw if isinstance(item, dict) and item.get("name")]
    # 兼容迁移：旧版 env 值为 "生产"/"测试"，转换为 "生产环境"/"测试环境"
    changed = False
    for s in sites:
        if s.get("env") == "生产":
            s["env"] = "生产环境"
            changed = True
        elif s.get("env") == "测试":
            s["env"] = "测试环境"
            changed = True
    # 兼容迁移（方案A）：撤销旧的"站点名称自动追加 -生产环境/-测试环境"后缀，
    # 仅当剥离后不会与其他记录产生 (name, env) 冲突时才执行，避免误伤同名同环境数据
    for i, s in enumerate(sites):
        n = s.get("name", "")
        stripped = None
        for suffix in ("-生产环境", "-测试环境"):
            if n.endswith(suffix):
                stripped = n[: -len(suffix)]
                break
        if stripped and not any(
            j != i
            and sites[j].get("name") == stripped
            and sites[j].get("env") == s.get("env")
            for j in range(len(sites))
        ):
            s["name"] = stripped
            changed = True
    if changed and raw:
        _save_unlocked(sites)  # 直接调用无锁版本（外层已持有锁）
    return sites


def _save(sites: list) -> None:
    with _file_lock:
        _save_unlocked(sites)


def _save_unlocked(sites: list) -> None:
    """无锁写入：调用方必须已持有 _file_lock。写入前轮转备份（保留最近 3 份）"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # 轮转：.bak.1 → .bak.2, .bak → .bak.1（旧的 .bak.2 被覆盖删除）
    if DATA_FILE.exists():
        try:
            _rotate_backups()
            BACKUP_FILE.write_bytes(DATA_FILE.read_bytes())
        except OSError:
            pass  # 备份失败不阻塞写入，但避免静默丢原始数据
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sites, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, DATA_FILE)


def _rotate_backups() -> None:
    """备份轮转：sites.json.bak → .bak.1 → .bak.2（超出 BACKUP_ROLL 删除）"""
    for i in range(BACKUP_ROLL - 1, 0, -1):
        src = DATA_DIR / f"sites.json.bak.{i - 1}" if i > 1 else BACKUP_FILE
        dst = DATA_DIR / f"sites.json.bak.{i}"
        if src.exists():
            try:
                dst.write_bytes(src.read_bytes())
            except OSError:
                pass


def _load_mutate(fn) -> list:
    """原子读-改-写：fn(sites) 原地修改列表。fn 抛 HTTPException 时不写入。
    解决 TOCTOU 竞态：_load() 后到 _save() 之间的间隙不再有锁保护，
    两个并发写者会各读一份快照导致最后一个 save 覆盖前一个的修改。"""
    with _file_lock:
        sites = _load_unlocked()
        fn(sites)  # 抛 HTTPException 时不 save
        _save_unlocked(sites)
        return sites


# ── 用户存储（MySQL users 表）──

# 字段与旧 data/users.json 的记录一一对应，便于按 scripts/migrate_users.py 原样迁移
USERS_DDL = """
CREATE TABLE IF NOT EXISTS users (
  id            CHAR(8)      NOT NULL,
  username      VARCHAR(32)  NOT NULL,
  password_hash VARCHAR(128) NOT NULL,
  role          VARCHAR(8)   NOT NULL DEFAULT 'user',
  enabled       TINYINT(1)   NOT NULL DEFAULT 1,
  pwd_epoch     INT          NOT NULL DEFAULT 0,
  created_at    DATETIME     NULL,
  updated_at    DATETIME     NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_username (username),
  KEY idx_role (role)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户与权限'
"""

# SELECT 列清单：调用方拼 f"SELECT {_USER_COLS} ..."
_USER_COLS = "id, username, password_hash, role, enabled, pwd_epoch, created_at, updated_at"


class DbDown(Exception):
    """MySQL 不可用。单独一类异常，供上层转 503、且不计入登录失败次数"""


def _db_connect():
    """建连失败抛 DbDown，调用方不要直接依赖 pymysql 异常类型"""
    try:
        return pymysql.connect(**_MYSQL_KWARGS)
    except pymysql.MySQLError as exc:
        raise DbDown(str(exc)) from exc


@contextlib.contextmanager
def _db_conn():
    """短连接上下文：退出即关闭，不留连接池"""
    conn = _db_connect()
    try:
        yield conn
    finally:
        with contextlib.suppress(Exception):
            conn.close()


@contextlib.contextmanager
def _db_tx():
    """事务上下文：正常退出 commit，异常回滚。
    多步检查（如"至少保留一个管理员"）必须用它，否则检查与更新之间可被并发请求插队"""
    conn = _db_connect()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        with contextlib.suppress(Exception):
            conn.rollback()
        raise
    finally:
        with contextlib.suppress(Exception):
            cur.close()
        with contextlib.suppress(Exception):
            conn.close()


def _db_fetchall(sql, args=()):
    """只读查询。连接级失败（MySQL 重启/网络抖动）重连重试一次"""
    last = None
    for _ in range(2):
        try:
            with _db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, args)
                    return cur.fetchall()
        except DbDown:
            raise
        except pymysql.MySQLError as exc:
            last = exc
    raise DbDown(str(last)) from last


def _db_execute(sql, args=()):
    """写操作：commit 后返回影响行数。
    不重试 —— 语句可能已落库，重试有重复副作用风险。
    IntegrityError 原样抛出（唯一约束等业务冲突），其余 MySQLError 归入 DbDown"""
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                conn.commit()
                return cur.rowcount
    except pymysql.err.IntegrityError:
        raise
    except pymysql.MySQLError as exc:
        raise DbDown(str(exc)) from exc


def _db_pong() -> bool:
    """MySQL 是否可用。/health 用它把容器健康检查挂到数据库状态上"""
    try:
        _db_fetchall("SELECT 1")
        return True
    except DbDown:
        return False


def _db_guard(fn):
    """数据库不可用时返回 503（区别于业务错误）。
    站点接口不装饰，改由 _require_user 内部转换"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except DbDown:
            raise HTTPException(status_code=503, detail="用户服务暂不可用，请稍后再试")
    return wrapper


def _fmt_dt(v) -> str:
    """DATETIME → 'YYYY-MM-DD HH:MM:SS'，与旧 users.json 的字符串格式保持一致"""
    if v is None:
        return ""
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    return str(v)


def _user_row(row) -> dict:
    """MySQL 行 → 与原 users.json 记录同形状的 dict（TINYINT→bool 等类型归一化）"""
    return {
        "id": row["id"],
        "username": row["username"],
        "password_hash": row["password_hash"],
        "role": row["role"],
        "enabled": bool(row["enabled"]),
        "pwd_epoch": int(row["pwd_epoch"]),
        "created_at": _fmt_dt(row["created_at"]),
        "updated_at": _fmt_dt(row["updated_at"]),
    }


def _get_user(*, uid: str | None = None, username: str | None = None) -> dict | None:
    """按 id 或 username（大小写不敏感）取用户；不存在返回 None"""
    if uid is not None:
        rows = _db_fetchall(f"SELECT {_USER_COLS} FROM users WHERE id = %s", (uid,))
    elif username is not None:
        rows = _db_fetchall(f"SELECT {_USER_COLS} FROM users WHERE LOWER(username) = LOWER(%s)", (username,))
    else:
        return None
    return _user_row(rows[0]) if rows else None


# ── 密码哈希：优先 bcrypt，未装则退 pbkdf2_hmac（标准库，零依赖）──

try:
    import bcrypt as _bcrypt

    def _hash_password(pw: str) -> str:
        return _bcrypt.hashpw(pw.encode("utf-8"), _bcrypt.gensalt()).decode("ascii")

    def _verify_password(pw: str, hash_str: str) -> bool:
        try:
            return _bcrypt.checkpw(pw.encode("utf-8"), hash_str.encode("ascii"))
        except (ValueError, TypeError):
            return False

    _PWD_ALGO = "bcrypt"
except ImportError:
    def _hash_password(pw: str) -> str:
        salt = secrets.token_hex(16)
        h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode("ascii"), 200_000)
        return f"pbkdf2${salt}${h.hex()}"

    def _verify_password(pw: str, hash_str: str) -> bool:
        try:
            algo, salt, h = hash_str.split("$", 2)
        except ValueError:
            return False
        if algo != "pbkdf2":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode("ascii"), 200_000)
        return hmac.compare_digest(calc.hex(), h)

    _PWD_ALGO = "pbkdf2"


def _validate_password(pw: str) -> None:
    """强密码校验：≥10 位、含字母+数字、≤72 字节。不通过抛 400。
    72 字节是 bcrypt 的硬边界：超出部分被静默丢弃，用户以为设了长密码实际只有前 72 字节生效"""
    if len(pw) < 10:
        raise HTTPException(status_code=400, detail="密码至少 10 位")
    if not any(c.isalpha() for c in pw) or not any(c.isdigit() for c in pw):
        raise HTTPException(status_code=400, detail="密码必须同时包含字母和数字")
    if len(pw.encode("utf-8")) > 72:
        raise HTTPException(status_code=400, detail="密码过长（bcrypt 上限 72 字节）")


_dummy_hash_cache = None


def _dummy_hash() -> str:
    """校验假哈希：用户不存在时也跑一次 bcrypt，让"用户不存在"和"密码错"耗时一致（防枚举）"""
    global _dummy_hash_cache
    if _dummy_hash_cache is None:
        _dummy_hash_cache = _hash_password("invalid-password-for-timing-parity")
    return _dummy_hash_cache


def _seed_admin_if_needed() -> None:
    """首次启动且用户表为空时，从 .env 的 ADMIN_PASSWORD 种子 admin 账号。
    ADMIN_PASSWORD 为空则生成随机强密码，启动日志打印一次。表非空不做任何事"""
    rows = _db_fetchall("SELECT COUNT(*) AS n FROM users")
    if rows and int(rows[0]["n"]) > 0:
        return
    pwd = ADMIN_PASSWORD if ADMIN_PASSWORD else secrets.token_hex(12)
    now = _now()
    try:
        _db_execute(
            "INSERT INTO users (id, username, password_hash, role, enabled, pwd_epoch, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (secrets.token_hex(4), "admin", _hash_password(pwd), "admin", 1, 0, now, now),
        )
    except pymysql.err.IntegrityError:
        return  # 竞态：另一进程/线程已种子
    if ADMIN_PASSWORD:
        print("[seed] 已从 ADMIN_PASSWORD 创建 admin 账号（用户名: admin）", flush=True)
    else:
        print(f"[seed] 已创建 admin 账号，随机密码（仅此一次）：{pwd}", flush=True)


def _init_db() -> None:
    """建表 + 种子 admin。失败则中止启动：没有 MySQL 时登录必然不可用，
    与其带病启动（/health 报 200 但全站 401）不如直接起不来，让容器健康检查暴露问题。
    最多等 30s，容忍 MySQL 刚起来还没开始接受连接"""
    if not MYSQL_HOST:
        sys.exit(
            "[fatal] 未配置 MYSQL_HOST，无法启动\n"
            "        用户与权限已改为存储于 MySQL，请在 .env 填写 MYSQL_HOST / MYSQL_PORT /\n"
            "        MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE，或先启动数据库:\n"
            "        docker compose up -d mysql"
        )
    last = None
    ok = False
    for _ in range(30):
        try:
            with _db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(USERS_DDL)
                conn.commit()
            ok = True
            break
        except (DbDown, pymysql.MySQLError) as exc:
            last = exc
            time.sleep(1)
    if not ok:
        sys.exit(
            f"[fatal] 无法连接 MySQL {MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}，启动中止\n"
            f"        最近错误: {last}\n"
            f"        检查 .env 的 MYSQL_* 配置，或先启动数据库: docker compose up -d mysql"
        )
    try:
        _seed_admin_if_needed()
    except DbDown as exc:
        sys.exit(f"[fatal] 初始化用户数据失败: {exc}")


# ── token：携带 user_id + pwd_epoch 的 HMAC，撤销靠每次查 user ──


def _make_token(user: dict) -> str:
    expires = int(time.time()) + TOKEN_TTL
    uid = user["id"]
    epoch = user.get("pwd_epoch", 0)
    sig = hmac.new(TOKEN_SECRET.encode(), f"{uid}:{expires}:{epoch}".encode(), hashlib.sha256).hexdigest()
    return f"{uid}:{expires}:{sig}"


def _verify_token(authorization: str | None) -> dict | None:
    """校验签名+过期，再到 users 表重查用户（enabled / pwd_epoch）。返回 user 或 None。
    数据库不通时抛 DbDown 而不是返回 None：返回 None 会被前端当成"登录过期"清掉会话，
    而实际上只是基础设施抖动，不该逼用户重新登录"""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:]
    try:
        uid, expires_str, sig = token.split(":", 2)
        expires = int(expires_str)
    except ValueError:
        return None
    if time.time() > expires:
        return None
    user = _get_user(uid=uid)
    if not user or not user.get("enabled", True):
        return None
    epoch = user.get("pwd_epoch", 0)
    expected = hmac.new(TOKEN_SECRET.encode(), f"{uid}:{expires}:{epoch}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return user


def _require_user(authorization: str | None) -> dict:
    """所有站点接口都过这里，DbDown → 503 也在这里统一转换"""
    try:
        user = _verify_token(authorization)
    except DbDown:
        raise HTTPException(status_code=503, detail="用户服务暂不可用，请稍后再试")
    if not user:
        raise HTTPException(status_code=401, detail="未授权，请先登录")
    return user


def _require_admin(authorization: str | None) -> dict:
    user = _require_user(authorization)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# ── categraf-http-admin 拨测联动 ──

_monitor_session_lock = threading.Lock()
_monitor_session: requests.Session | None = None


def _monitor_enabled() -> bool:
    return bool(CATEGRAF_ADMIN_URL and CATEGRAF_ADMIN_PASS)


def _norm_url(v: str) -> str:
    """归一化用于比对：补 scheme、转小写、去尾斜杠"""
    v = (v or "").strip().lower()
    if not v:
        return ""
    if not v.startswith(("http://", "https://")):
        v = "http://" + v
    return v.rstrip("/")


def _assert_public_url(url: str) -> None:
    """拒绝内网/环回/链路本地地址作为拨测目标（防 SSRF 探测内网服务）"""
    from urllib.parse import urlparse
    from ipaddress import ip_address
    u = url if "://" in url else "http://" + url
    p = urlparse(u)
    if p.scheme not in ("http", "https"):
        return  # 非 http(s)，不在拨测范围内，无需检查
    host = p.hostname or ""
    if not host:
        return
    try:
        ip = ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise HTTPException(
                status_code=400,
                detail=f"不允许拨测内网/环回/链路本地地址：{host}",
            )
    except ValueError:
        # 非 IP，可能是域名 —— 放行（admin 应自行评估域名指向）
        pass


def _delete_target_with_retry(sess, tid: str, *, max_attempts: int = 3) -> bool:
    """带指数退避的目标删除：瞬时失败重试，成功或终态失败都返回 True/False。
    失败不静默吞掉，由调用方收集后写 _sync_status"""
    import requests as _req
    for attempt in range(max_attempts):
        try:
            r = sess.delete(f"{CATEGRAF_ADMIN_URL}/api/targets/{tid}", timeout=5)
            if r.status_code in (200, 404):
                return True
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(0.5 * (2 ** attempt))
                continue
            return False  # 其他 4xx：终态失败
        except _req.RequestException:
            time.sleep(0.5 * (2 ** attempt))
    return False


def _candidate_urls(site: dict) -> set:
    """该系统所有可能注册过拨测的地址（域名/公网/内网）"""
    return {_norm_url(site.get(k)) for k in ("domain", "public_url", "private_url")} - {""}


def _dedupe_targets(sess, targets: list, related_urls: set) -> list:
    """同步前对账去重：只清理与当前系统候选地址相关的重复目标。
    - 同 URL 多条 → 保留第一条，其余删除
    - 同 job 且地址同属该系统 → 保留第一条，其余删除
    其他系统的目标绝不触碰。返回清理后的目标列表"""
    related = {_norm_url(u) for u in related_urls} - {""}
    seen_url, seen_job, remove_ids = set(), set(), set()
    kept = []
    for t in targets:
        u = _norm_url(t.get("url") or "")
        if u not in related:
            kept.append(t)
            continue
        j = t.get("job") or ""
        if u in seen_url or (j and j in seen_job):
            remove_ids.add(t["id"])
            continue
        seen_url.add(u)
        if j:
            seen_job.add(j)
        kept.append(t)
    for tid in remove_ids:
        try:
            sess.delete(f"{CATEGRAF_ADMIN_URL}/api/targets/{tid}", timeout=5)
        except requests.RequestException:
            pass
    return kept


def _monitor_login() -> requests.Session:
    """登录 categraf-http-admin 拿 session cookie，全局复用直到失效重登"""
    global _monitor_session
    with _monitor_session_lock:
        if _monitor_session is not None:
            return _monitor_session
        sess = requests.Session()
        r = sess.post(
            f"{CATEGRAF_ADMIN_URL}/login",
            data={"username": CATEGRAF_ADMIN_USER, "password": CATEGRAF_ADMIN_PASS},
            timeout=5,
            allow_redirects=False,
        )
        # admin 未启用认证时不提供 /login(405)，API 本身开放，直接用无 cookie 会话
        if r.status_code == 405:
            _monitor_session = sess
            return sess
        if r.status_code != 302 or "ccsid" not in sess.cookies:
            raise RuntimeError(f"拨测管理端登录失败(HTTP {r.status_code})")
        _monitor_session = sess
        return sess


def _pick_probe_url(site: dict) -> str:
    """拨测地址：域名 > 公网地址 > 内网地址。无 scheme 时补 http://"""
    url = (site.get("domain") or "").strip() or (site.get("public_url") or "").strip() or (site.get("private_url") or "").strip()
    if not url:
        return ""
    if not url.lower().startswith(("http://", "https://")):
        url = "http://" + url
    return url


# 环境标识 -> 拨测 job 名称后缀（仅生产环境/测试环境映射，其他环境不加后缀）
_ENV_SUFFIX = {"生产环境": "-生产环境", "测试环境": "-测试环境"}


def _env_suffixed(name: str, env: str) -> str:
    """[已废弃] 站点名称自动带环境后缀。
    自方案A起站点名称不再带后缀（便于按 name 聚合双环境分组卡），
    拨测 job 名由 _probe_job 用 name + env 拼接。保留仅供一次性迁移引用。"""
    suffix = _ENV_SUFFIX.get(env or "", "")
    if not suffix or "生产" in name or "测试" in name:
        return name
    return f"{name}{suffix}"


def _probe_job(site: dict) -> str:
    """拨测 job 名 = 站点名称 + 环境后缀（与站点名称分离，站点页保持干净名称）"""
    name = (site.get("name") or "").strip()
    env = (site.get("env") or "").strip()
    suffix = _ENV_SUFFIX.get(env, "")
    return f"{name}{suffix}" if suffix else name


def _register_probe(site: dict, stale_urls: set | None = None) -> None:
    url = _pick_probe_url(site)
    if not url:
        raise RuntimeError("没有可用的域名/公网/内网地址")
    job = _probe_job(site)
    codes = (site.get("probe_status_codes") or "").strip() or "200"
    timeout = (site.get("probe_timeout") or "").strip()
    sess = _monitor_login()
    stale = {_norm_url(u) for u in (stale_urls or [])} - {""} - {_norm_url(url)}

    targets = sess.get(f"{CATEGRAF_ADMIN_URL}/api/targets", timeout=5).json().get("targets", [])
    # 先对账去重：清理与当前系统相关的重复目标（同URL/同job），再执行注册逻辑
    targets = _dedupe_targets(sess, targets, {_norm_url(url)} | stale | _candidate_urls(site))
    # 换过地址时只清理旧地址目标；当前地址目标保留，避免抹掉手动配置
    # 失败收集（不静默吞掉），写入 _sync_status 让用户可见
    stale_failures = 0
    for t in targets:
        if _norm_url(t.get("url")) in stale:
            if not _delete_target_with_retry(sess, t["id"]):
                stale_failures += 1
    existing = next((t for t in targets if _norm_url(t.get("url")) == _norm_url(url)), None)

    if existing is None:
        payload = {"kind": "http", "url": url, "job": job, "expected_status_codes": codes}
        if timeout:
            payload["response_timeout"] = timeout
        r = sess.post(f"{CATEGRAF_ADMIN_URL}/api/targets", json=payload, timeout=5)
        if r.status_code != 200:
            detail = ""
            try:
                detail = r.json().get("error", "")
            except ValueError:
                pass
            # session 过期等情况：清掉缓存强制下次重登（旧 session 显式关闭，避免连接池泄漏）
            global _monitor_session
            with _monitor_session_lock:
                old, _monitor_session = _monitor_session, None
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
            raise RuntimeError(f"注册拨测失败(HTTP {r.status_code}) {detail}")
        msg = f"已加入拨测：{url}"
        if stale_failures:
            msg += f"（旧地址 {stale_failures} 个清理失败）"
        _sync_status[site["id"]] = {"ok": stale_failures == 0, "message": msg, "time": _now()}
        return

    # 目标已存在：job/状态码/超时与站点配置一致则跳过；否则合并更新
    # 超时留空 = 保留目标上已有的手动配置，不抹掉
    need_update = (
        existing.get("job") != job
        or existing.get("expected_status_codes") != codes
        or (bool(timeout) and existing.get("response_timeout") != timeout)
    )
    if not need_update:
        _sync_status[site["id"]] = {"ok": True, "message": f"拨测已是最新：{url}（{job}）", "time": _now()}
        return
    payload = dict(existing)
    payload["kind"] = "http"
    payload["url"] = url
    payload["job"] = job
    payload["expected_status_codes"] = codes
    if timeout:
        payload["response_timeout"] = timeout
    r = sess.post(
        f"{CATEGRAF_ADMIN_URL}/api/targets/{existing['id']}/edit",
        json=payload,
        timeout=5,
    )
    if r.status_code == 200:
        msg = f"拨测已更新：{job} → {url}"
        if stale_failures:
            msg += f"（旧地址 {stale_failures} 个清理失败）"
        _sync_status[site["id"]] = {"ok": stale_failures == 0, "message": msg, "time": _now()}
    else:
        raise RuntimeError(f"更新拨测失败(HTTP {r.status_code})")


def _remove_probe(site: dict, extra_urls: set | None = None) -> None:
    """按该系统所有候选地址（+额外指定的历史地址）逐个清理拨测目标。
    失败收集后写入 _sync_status，不静默吞掉"""
    urls = (_candidate_urls(site) | {_norm_url(u) for u in (extra_urls or [])}) - {""}
    if not urls:
        return
    sess = _monitor_login()
    targets = sess.get(f"{CATEGRAF_ADMIN_URL}/api/targets", timeout=5).json().get("targets", [])
    failures = 0
    for t in targets:
        if _norm_url(t.get("url")) in urls:
            if not _delete_target_with_retry(sess, t["id"]):
                failures += 1
    if failures:
        _sync_status[site["id"]] = {"ok": False, "message": f"{failures} 个拨测目标删除失败", "time": _now()}


def _sync_probe_async(site: dict, action: str, extra_urls: set | None = None) -> None:
    """后台执行拨测同步；未启用或失败都不影响主流程，只记状态"""

    def _run():
        try:
            if action == "register":
                if not _monitor_enabled():
                    _sync_status[site["id"]] = {"ok": False, "message": "拨测联动未配置(CATEGRAF_ADMIN_URL)", "time": _now()}
                    return
                # 就地调和：只清理换掉的旧地址，当前地址目标保留手动配置
                _register_probe(site, stale_urls=extra_urls)
            else:
                if _monitor_enabled():
                    _remove_probe(site, extra_urls)
                _sync_status.pop(site["id"], None)
        except Exception as exc:  # noqa: BLE001 后台任务兜底，不让线程崩
            if action == "register":
                _sync_status[site["id"]] = {"ok": False, "message": str(exc), "time": _now()}

    threading.Thread(target=_run, daemon=True).start()


# 中国大陆自 1991 年起无夏令时，固定 UTC+8 偏移即可，不需要 tzdata。
# 不显式指定时区会得到容器本地时间（默认 UTC），比北京时间早 8 小时。
# 不用 zoneinfo：python:slim 镜像不带 /usr/share/zoneinfo，会抛 ZoneInfoNotFoundError。
TZ_CN = _dt.timezone(_dt.timedelta(hours=8), name="Asia/Shanghai")


def _now() -> str:
    """北京时间 YYYY-MM-DD HH:MM:SS。所有写入的时间戳统一走这里"""
    return _dt.datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")


def _find_duplicate(sites: list, data: dict, exclude_id: str | None = None) -> str:
    """多维查重：名称、域名/公网/内网地址（忽略大小写和结尾斜杠）。返回冲突描述，无冲突返回空"""
    def _norm(v: str) -> str:
        v = (v or "").strip().lower()
        if not v:
            return ""
        if not v.startswith(("http://", "https://")) and ("." in v or ":" in v):
            # 裸域名/IP 与带 scheme 的写法视为同一个
            return "http://" + v.rstrip("/")
        return v.rstrip("/")
    name = data["name"].strip().lower()
    env = (data.get("env") or "").strip()
    urls = {_norm(data.get(k)) for k in ("domain", "public_url", "private_url")} - {""}
    conn = (data.get("connection") or "").strip().lower()
    for s in sites:
        if s["id"] == exclude_id:
            continue
        # 同名同环境不允许重复；同名不同环境（分组卡：同站点跨环境）允许
        if s["name"].strip().lower() == name and (s.get("env") or "").strip() == env:
            return f"同名同环境的「{s['name']}」已存在（环境：{env or '未指定'}）"
        other = {_norm(s.get(k)) for k in ("domain", "public_url", "private_url")} - {""}
        hit = urls & other
        if hit:
            return f"地址 {next(iter(hit))} 已被「{s['name']}」使用"
        other_conn = (s.get("connection") or "").strip().lower()
        if conn and conn == other_conn:
            return f"连接串已被「{s['name']}」使用"
    return ""


# 用户名：1-32 位，允许中文/字母/数字/_ . @ -；禁空白与 HTML/属性注入相关字符
_USERNAME_RE = r"^[A-Za-z0-9_一-鿿.@-]{1,32}$"


class LoginIn(BaseModel):
    username: str = Field(pattern=_USERNAME_RE)
    # 登录侧只限长度防爆 body，不强制 72 字节（过长密码直接 401 即可）
    password: str = Field(max_length=256)


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/admin")
def admin_page():
    # 运维登录独立入口：首页不出现任何登录入口，此地址需直接访问/收藏
    return FileResponse(BASE_DIR / "static" / "admin.html")


@app.get("/health")
@_db_guard
def health():
    # 供 Docker healthcheck 探活；数据恢复时附带 warning 让运维可见。
    # MySQL 不通时返回 503：容器被标为 unhealthy，deploy.sh 的 curl -sf 也会失败
    _db_fetchall("SELECT 1")
    result = {"ok": True}
    if _data_warning:
        result["data_warning"] = _data_warning
    return result


_login_attempts: dict = {}  # ip -> [fail_count, first_fail_mono_time]
_LOGIN_MAX_FAILS = 10
_LOGIN_WINDOW_S = 15 * 60  # 15 分钟


@app.post("/api/login")
@_db_guard
def login(body: LoginIn, request: Request):
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    # 清理过期窗口
    expired = [k for k, (_, ts) in _login_attempts.items() if now - ts > _LOGIN_WINDOW_S]
    for k in expired:
        _login_attempts.pop(k, None)
    fails, first_ts = _login_attempts.get(ip, (0, now))
    if fails >= _LOGIN_MAX_FAILS:
        raise HTTPException(status_code=429, detail="尝试次数过多，请 15 分钟后再试")
    # 用户名不存在/密码错/已禁用 都返回同一 401，防用户名枚举。
    # 用户不存在时也跑一次 bcrypt 校验假哈希，让两种情况耗时一致（防时序枚举）。
    # 注意：DbDown 在计数之前就抛出，基础设施故障不计入失败次数，避免短暂抖动把全员锁死。
    user = _get_user(username=body.username)
    cred_hash = user["password_hash"] if user else _dummy_hash()
    if not user or not user["enabled"] or not _verify_password(body.password, cred_hash):
        _login_attempts[ip] = (fails + 1, first_ts)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    _login_attempts.pop(ip, None)  # 成功登录重置计数
    return {"token": _make_token(user), "expires_in": TOKEN_TTL, "role": user["role"]}


@app.get("/api/sites")
def list_sites(authorization: str | None = Header(default=None)):
    _require_user(authorization)
    return _load()


@app.post("/api/sites")
def create_site(site: SiteIn, authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    # 站点名称保持用户填写的原样（不带后缀），按 name + env 聚合双环境
    data = site.model_dump()
    data["name"] = (data.get("name") or "").strip()
    _validate_site_payload(data)
    now = _now()
    item = {
        "id": secrets.token_hex(4),
        **data,
        "created_at": now,
        "updated_at": now,
    }

    def _mutate(sites):
        dup = _find_duplicate(sites, data)
        if dup:
            raise HTTPException(status_code=409, detail=dup)
        sites.append(item)

    # 原子读-改-写：避免 _load→_save 之间被并发请求覆盖（TOCTOU）
    _load_mutate(_mutate)
    if item["monitor"]:
        _sync_probe_async(dict(item), "register")
    return item


def _validate_site_payload(data: dict, *, monitor_already_on: bool = False) -> None:
    """校验创建/更新站点的最小字段：URL 或连接串至少填一个；勾选拨测必须有 URL；拨测 URL 必须是公网地址（防 SSRF）

    monitor_already_on=True 表示该条目本来就是监控状态、monitor=true 不是本次新加的勾选。
    这个区分是必须的：前端编辑时总是把 monitor 原样带上（sitePayload），
    如果不区分，联动后来被关掉之后，已经勾选过的站点连改个备注都会被 400 挡下。
    """
    if not any((data.get(k) or "").strip() for k in ("domain", "public_url", "private_url", "connection")):
        raise HTTPException(status_code=400, detail="域名/公网/内网/连接串至少填一个")
    if data.get("monitor") and not monitor_already_on:
        # 新勾选监控但联动未配置：直接拒绝，而不是把 monitor 标记写进去。
        # 否则站点会永久显示「已监控」，实际一个拨测任务都没建 ——
        # 用户看到的状态和真实情况完全不符。
        if not _monitor_enabled():
            raise HTTPException(
                status_code=400,
                detail="拨测联动未配置（缺 CATEGRAF_ADMIN_URL 或 CATEGRAF_ADMIN_PASS），"
                       "无法加入监控；请先在 .env 配好这两项并重启容器",
            )
    if data.get("monitor"):
        if not any((data.get(k) or "").strip() for k in ("domain", "public_url", "private_url")):
            raise HTTPException(status_code=400, detail="勾选拨测监控需要至少填一个 URL 地址")
        # 拨测 URL 必须是公网地址，禁止内网/环回/链路本地（SSRF 防护）
        probe_url = _pick_probe_url(data)
        if probe_url:
            _assert_public_url(probe_url)


@app.put("/api/sites/{site_id}")
def update_site(site_id: str, site: SiteIn, authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    # 先读一次现有记录，判断 monitor=true 是不是本次新加的勾选（见 _validate_site_payload）。
    # 顺带让 404 在这里给出，不用等 _mutate。
    # 这里是只读检查，可能与并发写入竞态；最坏情况是读到稍旧的 monitor 值，
    # 只影响"是否放宽联动配置校验"这一项，不造成数据问题。
    existing = next((s for s in _load() if s["id"] == site_id), None)
    if existing is None:
        raise HTTPException(status_code=404, detail="条目不存在")
    data = site.model_dump()
    data["name"] = (data.get("name") or "").strip()
    _validate_site_payload(data, monitor_already_on=bool(existing.get("monitor")))

    captured: dict = {}

    def _mutate(sites):
        item = next((s for s in sites if s["id"] == site_id), None)
        if item is None:
            raise HTTPException(status_code=404, detail="条目不存在")
        dup = _find_duplicate(sites, data, exclude_id=site_id)
        if dup:
            raise HTTPException(status_code=409, detail=dup)

        captured["old_probe_url"] = _pick_probe_url(item)
        captured["was_monitor"] = item.get("monitor", False)

        updated = {**item, **data, "updated_at": _now()}
        item.clear()
        item.update(updated)
        captured["item"] = item

    # 原子读-改-写：避免 _load→_save 之间被并发请求覆盖（TOCTOU）
    _load_mutate(_mutate)

    item = captured["item"]
    # 编辑任何字段都按勾选状态对账同步：
    # - 勾选：旧地址目标清理、job 名（名称/环境）或地址变了就地更新
    # - 取消勾选：摘除该系统所有拨测目标
    if item["monitor"]:
        _sync_status.pop(item["id"], None)
        stale = {captured["old_probe_url"]} if captured["was_monitor"] and captured["old_probe_url"] and captured["old_probe_url"] != _pick_probe_url(item) else set()
        _sync_probe_async(dict(item), "register", extra_urls=stale)
    elif captured["was_monitor"]:
        _sync_probe_async(dict(item), "remove")
    return item


@app.delete("/api/sites/{site_id}")
def delete_site(site_id: str, authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    captured: dict = {}

    def _mutate(sites):
        removed = next((s for s in sites if s["id"] == site_id), None)
        if removed is None:
            raise HTTPException(status_code=404, detail="条目不存在")
        captured["removed"] = removed
        sites.remove(removed)

    # 原子读-改-写：避免 _load→_save 之间被并发请求覆盖（TOCTOU）
    _load_mutate(_mutate)
    removed = captured["removed"]
    _sync_status.pop(site_id, None)
    # 删除时把该系统所有可能的拨测地址都清理掉
    # 注意：_remove_probe 内部已遍历所有候选 URL，无需按 URL 起多个线程
    if removed.get("monitor"):
        _sync_probe_async(dict(removed), "remove")
    return {"ok": True}


@app.get("/api/monitor-status")
def monitor_status(authorization: str | None = Header(default=None)):
    """各系统拨测同步结果（内存态，供前端卡片展示）"""
    _require_user(authorization)
    return _sync_status


@app.get("/api/monitor-config")
def monitor_config(authorization: str | None = Header(default=None)):
    """拨测联动是否已配置。前端据此把「加入监控」置灰 ——
    后端未配置时会拒绝 monitor=true（见 _validate_site_payload），
    所以前端必须提前禁用，否则用户点了才知道失败，还容易留下脏状态。"""
    _require_user(authorization)
    return {
        "enabled": _monitor_enabled(),
        "reason": "" if _monitor_enabled() else "缺 CATEGRAF_ADMIN_URL 或 CATEGRAF_ADMIN_PASS",
    }


# ── 批量导入 / 导出 ──


def _parse_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "是", "y")


def _normalize_import_row(row: dict, idx: int) -> dict:
    name = str(row.get("name") or row.get("系统名称") or "").strip()
    if not name:
        raise ValueError(f"第{idx}行缺少系统名称")
    kind = str(row.get("kind") or row.get("资源类型") or "网站").strip() or "网站"
    env = str(row.get("env") or row.get("环境标识") or "").strip()
    if env not in ("生产环境", "测试环境"):
        raise ValueError(f"第{idx}行「{name}」环境标识必须是 生产环境 或 测试环境")
    monitor = _parse_bool(row.get("monitor") if "monitor" in row else row.get("拨测监控"))
    urls = [
        str(row.get(k) or row.get(cn) or "").strip()
        for k, cn in (("domain", "域名"), ("public_url", "公网地址"), ("private_url", "内网地址"))
    ]
    connection = str(row.get("connection") or row.get("连接串") or "").strip()
    # URL 三字段和连接串至少填一个（非 URL 类资源允许只填连接串）
    if not any(urls) and not connection:
        raise ValueError(f"第{idx}行「{name}」域名/公网/内网/连接串至少填一个")
    return {
        # 站点名称保持原样（不带后缀），同站点双环境靠 name 相同 + env 不同表达
        "name": name,
        "kind": kind,
        "category": str(row.get("category") or row.get("分类") or "").strip(),
        "domain": urls[0],
        "public_url": urls[1],
        "private_url": urls[2],
        "connection": connection,
        "owner": str(row.get("owner") or row.get("负责人") or "").strip(),
        "env": env,
        "remark": str(row.get("remark") or row.get("备注") or "").strip(),
        "monitor": monitor,
        # 拨测参数：留空即默认（状态码 200、不设超时），与 _export_csv 的列一一对应
        "probe_status_codes": str(row.get("probe_status_codes") or row.get("拨测状态码") or "").strip(),
        "probe_timeout": str(row.get("probe_timeout") or row.get("拨测超时") or "").strip(),
    }


class ImportIn(BaseModel):
    format: str  # json / csv
    content: str = Field(max_length=10_000_000)  # 10 MB 硬上限，防 OOM


@app.post("/api/sites/import")
def import_sites(body: ImportIn, authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    fmt = body.format.lower().strip()

    # 解析成统一 dict 列表（中英文表头都认）
    rows: list[dict]
    try:
        if fmt == "json":
            data = json.loads(body.content)
            if not isinstance(data, list):
                raise ValueError("JSON 应为数组")
            rows = [item for item in data if isinstance(item, dict)]
        elif fmt == "csv":
            reader = csv.DictReader(io.StringIO(body.content))
            rows = list(reader)
        else:
            raise HTTPException(status_code=400, detail="format 仅支持 json 或 csv")
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"解析失败: {exc}") from exc

    if len(rows) > 5000:
        raise HTTPException(status_code=400, detail=f"行数过多（{len(rows)}），请分批导入（上限 5000）")

    # 解析 + 校验每行（SiteIn 模型保证字段合法性 + 长度 + URL 格式）
    parsed, skipped = [], []
    for idx, raw_row in enumerate(rows, start=2):  # 从2开始：CSV 第1行是表头
        try:
            item_data = _normalize_import_row(raw_row, idx)
            # 走一遍 SiteIn 校验（长度/pattern/URL 格式），与 API 创建路径一致
            validated = SiteIn(**{
                "name": item_data["name"],
                "kind": item_data.get("kind", "网站"),
                "category": item_data["category"],
                "domain": item_data["domain"],
                "public_url": item_data["public_url"],
                "private_url": item_data["private_url"],
                "connection": item_data["connection"],
                "owner": item_data["owner"],
                "env": item_data["env"],
                "remark": item_data["remark"],
                "monitor": item_data["monitor"],
                "probe_status_codes": item_data.get("probe_status_codes", ""),
                "probe_timeout": item_data.get("probe_timeout", ""),
            }).model_dump()
            # 与创建/更新路径同一条校验线。之前导入漏掉这步，两条防线都能被批量导入绕过：
            #   1. 勾选拨测但只有连接串（无 URL）→ 记录 monitor=true 却永远无可探测地址
            #   2. SSRF 防护 _assert_public_url → 可把拨测指向内网/环回地址
            _validate_site_payload(validated)
            parsed.append(validated)
        except HTTPException as exc:
            skipped.append({"reason": f"第{idx}行：{exc.detail}"})
        except (ValueError, Exception) as exc:  # noqa: BLE001
            skipped.append({"reason": str(exc)})

    # 原子读-改-写：_load_mutate 内做去重 + 批量 append
    added: list = []

    def _mutate(sites):
        existing_names = {(s["name"].strip().lower(), (s.get("env") or "").strip()) for s in sites}
        existing_urls = set()
        existing_conns = set()
        for s in sites:
            for k in ("domain", "public_url", "private_url"):
                n = _norm_url(s.get(k))
                if n:
                    existing_urls.add(n)
            c = (s.get("connection") or "").strip().lower()
            if c:
                existing_conns.add(c)

        now = _now()
        for item_data in parsed:
            name_key = (item_data["name"].strip().lower(), item_data["env"])
            dup_urls = {_norm_url(item_data.get(k)) for k in ("domain", "public_url", "private_url")} - {""}
            dup_conn = (item_data["connection"] or "").strip().lower()
            if name_key in existing_names or (dup_urls & existing_urls) or (dup_conn and dup_conn in existing_conns):
                skipped.append({"reason": f"「{item_data['name']}」已存在（名称+环境、地址或连接串重复），跳过"})
                continue
            item = {
                "id": secrets.token_hex(4),
                **item_data,
                "created_at": now,
                "updated_at": now,
            }
            sites.append(item)
            existing_names.add(name_key)
            existing_urls.update(dup_urls)
            if dup_conn:
                existing_conns.add(dup_conn)
            added.append(item)

    _load_mutate(_mutate)

    if added:
        for item in added:
            if item["monitor"]:
                _sync_probe_async(dict(item), "register")
    return {"added": len(added), "skipped_count": len(skipped), "skipped": skipped[:20]}


def _export_csv(sites: list) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    header_cn = ["系统名称", "资源类型", "分类", "域名", "公网地址", "内网地址", "连接串", "负责人", "环境标识", "备注", "拨测监控", "拨测状态码", "拨测超时"]
    writer.writerow(header_cn)
    key_map = ["name", "kind", "category", "domain", "public_url", "private_url", "connection", "owner", "env", "remark", "monitor", "probe_status_codes", "probe_timeout"]
    for s in sites:
        writer.writerow([s.get(k, "") for k in key_map])
    # utf-8-sig 带 BOM，Excel 直接打开不乱码
    return buf.getvalue().encode("utf-8-sig")


@app.get("/api/sites/export")
def export_sites(format: str = "json", authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    sites = _load()
    stamp = _dt.datetime.now(TZ_CN).strftime("%Y%m%d_%H%M%S")
    if format == "csv":
        return Response(
            content=_export_csv(sites),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename=sites_{stamp}.csv"},
        )
    clean = [{k: v for k, v in s.items() if k not in ("id",)} for s in sites]
    return Response(
        content=json.dumps(clean, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=sites_{stamp}.json"},
    )


# ── 用户管理（仅 admin）──


class UserCreateIn(BaseModel):
    username: str = Field(pattern=_USERNAME_RE)
    password: str
    role: Literal["admin", "user"] = "user"


class UserUpdateIn(BaseModel):
    enabled: bool | None = None
    role: Literal["admin", "user"] | None = None


class PasswordResetIn(BaseModel):
    password: str


def _public_user(u: dict) -> dict:
    """脱去 password_hash / pwd_epoch 的对外视图"""
    return {
        "id": u["id"],
        "username": u["username"],
        "role": u.get("role", "user"),
        "enabled": u.get("enabled", True),
        "created_at": u.get("created_at", ""),
        "updated_at": u.get("updated_at", ""),
    }


@app.get("/api/users")
@_db_guard
def list_users(authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    rows = _db_fetchall(f"SELECT {_USER_COLS} FROM users ORDER BY created_at, username")
    return [_public_user(_user_row(r)) for r in rows]


@app.post("/api/users")
@_db_guard
def create_user(body: UserCreateIn, authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    _validate_password(body.password)
    uname = body.username.strip()
    now = _now()
    # 唯一性交给数据库的 UNIQUE 约束判定，不先查再插：避免并发创建竞态
    try:
        _db_execute(
            "INSERT INTO users (id, username, password_hash, role, enabled, pwd_epoch, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (secrets.token_hex(4), uname, _hash_password(body.password), body.role, 1, 0, now, now),
        )
    except pymysql.err.IntegrityError:
        raise HTTPException(status_code=409, detail=f"用户名 {uname} 已存在")
    return {"ok": True}


@app.put("/api/users/{user_id}")
@_db_guard
def update_user(user_id: str, body: UserUpdateIn, authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    if body.enabled is None and body.role is None:
        raise HTTPException(status_code=400, detail="无可更新字段")
    now = _now()
    # 事务 + FOR UPDATE：读到的 role 到提交前不会被并发修改，"最后一个 admin" 检查才成立
    with _db_tx() as cur:
        cur.execute(f"SELECT {_USER_COLS} FROM users WHERE id = %s FOR UPDATE", (user_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")
        user = _user_row(row)
        if body.enabled is not None and body.enabled != user["enabled"]:
            cur.execute("UPDATE users SET enabled = %s, updated_at = %s WHERE id = %s",
                        (1 if body.enabled else 0, now, user_id))
        if body.role is not None and body.role != user["role"]:
            # 不允许把最后一个 admin 降级
            if body.role != "admin":
                cur.execute("SELECT COUNT(*) AS n FROM users WHERE role = %s AND id <> %s", ("admin", user_id))
                if not cur.fetchone()["n"]:
                    raise HTTPException(status_code=400, detail="至少保留一个管理员")
            cur.execute("UPDATE users SET role = %s, updated_at = %s WHERE id = %s", (body.role, now, user_id))
        cur.execute("UPDATE users SET updated_at = %s WHERE id = %s", (now, user_id))
        cur.execute(f"SELECT {_USER_COLS} FROM users WHERE id = %s", (user_id,))
        result = _public_user(_user_row(cur.fetchone()))
    return result


@app.post("/api/users/{user_id}/password")
@_db_guard
def reset_password(user_id: str, body: PasswordResetIn, authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    _validate_password(body.password)
    # pwd_epoch 在 SQL 里自增：单条语句即原子，旧 token 立即失效，无读改写竞态
    n = _db_execute(
        "UPDATE users SET password_hash = %s, pwd_epoch = pwd_epoch + 1, updated_at = %s WHERE id = %s",
        (_hash_password(body.password), _now(), user_id),
    )
    if n == 0:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"ok": True}


@app.delete("/api/users/{user_id}")
@_db_guard
def delete_user(user_id: str, authorization: str | None = Header(default=None)):
    admin = _require_admin(authorization)
    if admin["id"] == user_id:
        raise HTTPException(status_code=400, detail="不能删除自己")
    with _db_tx() as cur:
        cur.execute("SELECT role FROM users WHERE id = %s FOR UPDATE", (user_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")
        if row["role"] == "admin":
            cur.execute("SELECT COUNT(*) AS n FROM users WHERE role = %s AND id <> %s", ("admin", user_id))
            if not cur.fetchone()["n"]:
                raise HTTPException(status_code=400, detail="至少保留一个管理员")
        cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
    return {"ok": True}


# 启动时连库、建表、种子 admin（表为空时）
_init_db()


if __name__ == "__main__":
    import uvicorn
    # 本地开发默认绑 127.0.0.1；要对外暴露请用 run.py 或 docker compose
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
