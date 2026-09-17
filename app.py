import contextlib
import csv
import datetime as _dt
import functools
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import pymysql
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

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

# ── categraf http_provider ──
# CATEGRAF_TOKEN：categraf 拉取配置用的 Bearer token（必配；不设则端点拒绝一切拉取请求）
# 与登录账密解耦：登录密码只用于 Web 页面，categraf config.toml 里只放这个 token
CATEGRAF_TOKEN = os.environ.get("CATEGRAF_TOKEN", "")

# 拨测目标存储文件（端口拨测独立管理；HTTP 拨测从站点 monitor 字段动态生成）
PROBES_FILE = DATA_DIR / "probes.json"
PROBES_BACKUP = DATA_DIR / "probes.json.bak"

# 数据文件读写锁：防止并发请求读改写丢数据（RLock 允许 _load 内部调用 _save）
_file_lock = threading.RLock()
_probes_lock = threading.RLock()

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
    # 拨测参数：留空用默认（状态码 200、GET、不设置超时）；状态码多个用 | 分隔，超时如 3s/500ms/1m（纯数字自动按秒）
    probe_status_codes: str = Field(default="", pattern=r"^(\d{3}(\|\d{3})*)?$")
    probe_timeout: str = Field(default="", pattern=r"^(\d+(ms|s|m))?$")
    # 细粒度拨测配置（对齐原 categraf-http-admin）：方法/请求头/Body/跟随重定向/私有 CA/跳过证书校验
    probe_method: str = Field(default="", pattern=r"^(|GET|POST|PUT|DELETE|HEAD)$")
    probe_headers: str = Field(default="", max_length=2000)  # JSON 数组字符串，如 ["X-Key","val"]
    probe_body: str = Field(default="", max_length=4000)
    probe_follow_redirects: bool | None = None  # None=用 categraf 默认；显式 true/false 才落盘
    probe_insecure_skip_verify: bool = False  # 跳过证书校验（与 tls_ca 互斥，跳过优先）
    probe_tls_ca: str = Field(default="", max_length=500)  # 私有 CA 证书路径（categraf 服务器本地路径）
    # 端口拨测参数：只在「无 URL + 有 connection」时生效（有 URL 走 HTTP 拨测，这几个字段被忽略）
    probe_protocol: str = Field(default="tcp", pattern=r"^(tcp|udp)$")  # 协议
    probe_read_timeout: str = Field(default="", pattern=r"^(\d+(ms|s|m))?$")  # 只有 expect 时才有意义
    probe_send: str = Field(default="", max_length=500)  # 发送内容，\r \n \t 用转义写法
    probe_expect: str = Field(default="", max_length=500)  # 期望响应包含

    @field_validator("probe_timeout", "probe_read_timeout", mode="before")
    @classmethod
    def _norm_probe_timeout(cls, v):
        """超时时长纯数字自动按秒补单位（3 -> 3s），防止漏写 s"""
        v = v.strip() if isinstance(v, str) else v
        if isinstance(v, str) and v.isdigit():
            return f"{v}s"
        return v

    @field_validator("probe_send", "probe_expect", mode="before")
    @classmethod
    def _reject_send_expect_control_chars(cls, v):
        """拒绝真实控制字符：send/expect 里要表达 \r \n \t 用字面转义写法，
        落库时由 _validate_site_payload 还原（与旧的 /api/probes 同一套口径）"""
        v = v.strip() if isinstance(v, str) else v
        if isinstance(v, str) and any(ord(c) < 32 for c in v):
            raise ValueError("发送/期望内容含非法控制字符，请写 \\r \\n \\t")
        return v

    @field_validator("public_url", "private_url", "domain", mode="before")
    @classmethod
    def _norm_url_field(cls, v, info: ValidationInfo):
        """URL 字段：拒绝控制字符和引号（防 attribute 注入），校验 scheme/netloc、
        主机名合法性，以及 IP 归属（域名不填 IP；公网/内网各放各的）"""
        v = (v or "").strip()
        if not v:
            return v
        if v in _PLACEHOLDERS:
            raise ValueError(f"该字段是占位符 {v}，请留空或填真实地址")
        if any(c.isspace() or c in '"\'<>`' for c in v):
            raise ValueError("URL 含非法字符（空格/引号/尖括号）")
        # 先走主机名/端口校验：它对括号内不合法的 IPv6 会吞掉 urlparse 的异常，
        # 自己在这里 urlparse 就会把 http://[::1 变成 500 而不是 422
        host_err = _url_host_error(v)
        if host_err:
            raise ValueError(host_err)
        p = urlparse(_with_scheme(v))
        if p.scheme not in ("http", "https") or not p.netloc:
            raise ValueError("URL 格式不合法，应为 http(s)://host[/path]")
        ip_err = _ip_field_error(info.field_name, v)
        if ip_err:
            raise ValueError(ip_err)
        return v

    @field_validator("probe_body", mode="before")
    @classmethod
    def _reject_body_control_chars(cls, v):
        """拒绝 probe_body 控制字符（< 0x20）：防 TOML 解析失败导致全部目标拨测中断"""
        v = v.strip() if isinstance(v, str) else v
        if isinstance(v, str) and any(ord(c) < 32 for c in v):
            raise ValueError("Body 含非法控制字符")
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

    @model_validator(mode="after")
    def _check_url_fields_distinct(self):
        """域名/公网/内网不能指向同一个地址。

        同一地址填进两个字段：拨测时会产生两个不同的 job 去拨同一个目标，界面上还
        会显示两条几乎一样的记录，用户看不出为什么重复。归一化后比较（去 scheme、
        去默认端口，见 _norm_url），所以这些都算同一个：
          "x.example.com" / "http://x.example.com" / "http://x.example.com/"
          "http://x:3208"   / "https://x:3208"
          "http://x:80"     / "http://x"
        """
        labels = {"domain": "域名", "public_url": "公网地址", "private_url": "内网地址"}
        seen: dict[str, str] = {}
        for field, label in labels.items():
            n = _norm_url(getattr(self, field))
            if not n:
                continue
            if n in seen:
                raise ValueError(
                    f"{label} 与 {seen[n]} 重复：{n}（域名/公网/内网不能指向同一个地址）")
            seen[n] = label
        return self


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
    "probe_method": "",
    "probe_headers": "",
    "probe_body": "",
    "probe_follow_redirects": None,
    "probe_insecure_skip_verify": False,
    "probe_tls_ca": "",
    "probe_protocol": "tcp",
    "probe_read_timeout": "",
    "probe_send": "",
    "probe_expect": "",
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


# ── categraf http_provider：拨测目标管理与配置下发 ──
#
# sites-nav 直接作为 categraf 的 http_provider：
# - HTTP 拨测目标：从站点数据动态生成（monitor=true 的站点自动成为拨测目标）
# - 端口拨测目标：独立存储在 data/probes.json，通过 API 在线管理
# - categraf 通过 GET /api/config/http_response 拉取两类拨测的 TOML 配置
#
# 原 categraf-http-admin 的同步逻辑（登录/对账/重试）已全部移除，
# 改为"写站点即生效"模式：拨测目标实时从站点数据生成，无需中间同步。

from toml_gen import (
    KIND_HTTP,
    KIND_NET,
    config_version,
    generate_http_toml,
    generate_net_toml,
    validate_status_codes,
)


_URL_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://")


def _with_scheme(v: str) -> str:
    """补上缺失的 scheme。判断大小写不敏感——否则 HTTP://x 会被当成主机名 HTTP，
    后面跟的一大段变成 path，整条地址就废了。"""
    return v if v.lower().startswith(("http://", "https://")) else "http://" + v


def _split_netloc(netloc: str) -> tuple[str, str]:
    """拆出 (host, port)。port 保留原文，越界/非数字也不丢。

    与前端 splitUrl 是同一套写法：查重口径一旦分叉，重复值就漏网（见测试）。
    调用方已去掉 userinfo。"""
    if netloc.startswith("["):
        end = netloc.find("]")
        host = netloc[1:end] if end >= 0 else ""
        tail = netloc[end + 1:]
        port = tail[1:] if tail.startswith(":") else ""
        return host, port
    ci = netloc.find(":")
    return (netloc, "") if ci < 0 else (netloc[:ci], netloc[ci + 1:])


def _split_url(v: str) -> tuple[str, str, str]:
    """http(s) 地址 -> (host, port, 去掉 userinfo 的 netloc)。

    解析失败（括号内 IPv6 不合法）返回 ("", "", "")，由调用方决定报错还是退化。"""
    try:
        netloc = urlparse(_with_scheme(v)).netloc
    except ValueError:
        return "", "", ""
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    host, port = _split_netloc(netloc)
    return host, port, netloc


def _ip_field_error(field: str, v: str) -> str:
    """IP 填错字段时的错误信息；相符、或不是 IP 字面量返回空串。

    域名框只放域名；公网/内网各放各的 IP。放错之后两个字段各生成一个拨测 job，
    指向同一台机器，运维看不出为什么重复。"""
    host = _split_url(v)[0]
    kind = _classify_ip(host)
    if kind is None:
        return ""
    if field == "domain":
        return f"域名不能填 IP 地址（{host}），请填到 公网地址 / 内网地址"
    if field == "public_url" and kind == "internal":
        return f"公网地址不能填内网 IP（{host}），请填到 内网地址"
    if field == "private_url" and kind == "public":
        return f"内网地址不能填公网 IP（{host}），请填到 公网地址"
    return ""


def _norm_url(v: str) -> str:
    """归一化用于比对：转小写、去 scheme、去默认端口、去尾斜杠。

    去 scheme 是有意的：http://x:3208 与 https://x:3208 是同一个服务（同 host:port），
    当成两条会生成两个 job 去拨同一个目标；默认端口同理（http://x:80 与 http://x、
    https://x:443 与 https://x）。这只影响查重；拨测地址本身由 _pick_probe_url 用原值
    生成，scheme 和端口在那条链路上都不丢。
    端口保留原文（越界的 :320802 也要带着），不然会和"无端口"的写法撞成假重复。
    绝不抛异常：历史脏数据里的 http://[1:::2]:8080 会让 urlparse 直接 ValueError，
    而这里对每条已存记录都要跑一遍（建查重索引 / 出配置），不能让它拖垮接口。
    """
    v = (v or "").strip().lower()
    if not v:
        return ""
    if not _URL_SCHEME_RE.match(v):
        v = "http://" + v
    try:
        p = urlparse(v)
        path = v.split("://", 1)[1][len(p.netloc):]
    except ValueError:
        return v.rstrip("/")          # 括号内 IPv6 不合法：退化成字面比较
    netloc = p.netloc
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    host, port = _split_netloc(netloc)
    if not host:
        return v.rstrip("/")          # 解析不出主机（最常见是 IPv6 漏了方括号）
    if (p.scheme, port) in (("http", "80"), ("https", "443")):
        port = ""
    return f"{host}{(':' + port if port else '')}{path}".rstrip("/")


# 表格里表示「无此值」的占位符。这些值过去会被原样存进 URL / 连接串字段，
# 变成永远解析不出地址的拨测目标（例如 public_url="-"），而 "N/A" 更隐蔽：
# urlparse 在 "/" 处切开，主机名会变成单字母 "N"，校验全部通过。
# 导入时统一归一为空；表单里手输则报明确的错，不静默吃掉。
_PLACEHOLDERS = {
    "-", "--", "---", "—", "–", "／", "/", "\\", "*", "×",
    "无", "暂无", "无。", "空白", "N/A", "n/a",
    "None", "none", "null", "NULL", "nil",
}


def _blank_if_placeholder(v: str) -> str:
    s = (v or "").strip()
    return "" if s in _PLACEHOLDERS else s


# IP 字面量校验用 ipaddress 模块，不自己写正则：
# 正则 \d{1,3} 会放过 999.1.1.1 / 256.0.0.1 这类每段超界的地址。
def _is_valid_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


# 内网地址段：RFC 1918 三段 + 环回 + 链路本地；IPv6 的 ULA/环回/链路本地同样算内网。
# 100.64.0.0/10（运营商级 NAT）没算进来——它公网不可路由，但也不算传统内网，
# 环境里真在用的话往这个列表加一行就行。
_INTERNAL_NETS = [
    ipaddress.ip_network(n) for n in (
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
        "127.0.0.0/8", "169.254.0.0/16",
        "::/128", "::1/128", "fc00::/7", "fe80::/9",
    )
]


def _classify_ip(host: str) -> str | None:
    """IP 归属：'internal' / 'public'；不是 IP 字面量返回 None。

    域名一律返回 None：校验时发 DNS 请求既慢又不稳定，还会拖慢整个保存动作，
    所以域名在公网/内网两个字段里都放行（填错的位置靠人眼判断）。
    IPv4 映射的 IPv6（::ffff:10.0.0.1）按内嵌的 IPv4 判定；fe80::1%eth0 先剥掉 zone。
    """
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    nets = [n for n in _INTERNAL_NETS if n.version == ip.version]
    return "internal" if any(ip in n for n in nets) else "public"


def _url_host_error(v: str) -> str:
    """校验 URL 里的主机名和端口是否合法。合法返回空串，否则返回错误信息。

    原来只校验 netloc 非空，会放过一堆根本连不通的写法，而且这些脏值进了库之后
    会变成拨测目标，运维分不清是配置错还是网络不通：
      "." / ".." / "a..b.com"     —— 空标签，DNS 解析必然失败
      "-bad.com" / "bad-.com"     —— 标签不能以连字符开头或结尾
      "中文.example.com"          —— 非 ASCII；categraf 不做 IDNA 转换
      "999.1.1.1" / "256.0.0.1"   —— IP 每段超界（手写正则拦不住，交给 ipaddress）
      "1.2.3.4.5" / "192.168.1"   —— 既不是合法域名也不是合法 IP
      ":320802" / ":abc"          —— 端口越界或非数字（见 _validate_host_port 的同款口径）
      http://::1:8080             —— IPv6 漏了方括号
    """
    try:
        return _url_host_error_checked(v)
    except ValueError:
        # urlparse 对括号内不合法的 IPv6 直接抛异常（http://[1:::2]:8080、http://[::1）
        # 必须接住：一条脏输入不能 500 掉整个接口
        return f"URL 格式不合法：{v}（请检查括号内 IPv6 的写法，如 http://[::1]:8080）"


def _url_host_error_checked(v: str) -> str:
    # 这里直接 urlparse 而不是走 _split_url：urlparse 对括号内不合法的 IPv6 会抛异常
    # （http://[::1、http://[1:::2]:8080），得让它冒到 _url_host_error 去换成具体提示
    p = urlparse(_with_scheme(v))
    netloc = p.netloc
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]  # 跳过 userinfo，只看 host:port
    if not netloc:
        return "URL 格式不合法，应为 http(s)://host[/path]"
    host, port = _split_netloc(netloc)
    if not host:
        # netloc 非空却拆不出主机，最常见是 IPv6 漏了方括号
        return "URL 格式不合法：IPv6 地址需加方括号，如 http://[::1]:8080"
    if port and not _port_ok(port):
        # 用 netloc 原文判断而不是 p.port：urlparse 对 http://a:1:2 会当成 host=a
        # port=2 悄悄丢掉一段，拨测连的端口就和你填的不是一个
        return f"端口不合法：{port}（应为 1-65535 之间的数字）"
    # IP 字面量：含冒号，或不含字母（"1.2.3.4.5" / "192.168.1" / "123" 都在这里拦住）。
    # 判定交给 ipaddress，自己写正则只会放过超界的段。
    if ":" in host or not any(c.isalpha() for c in host):
        if not _is_valid_ip(host):
            return f"IP 地址不合法：{host}（请检查格式，IPv4 每段需在 0-255 范围内）"
        return ""
    if not host.isascii():
        return f"域名含非 ASCII 字符：{host}（请改用 punycode 写法，如 xn-- 开头）"
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        return f"域名含非法字符：{host}（只允许字母、数字、点和连字符）"
    if host.startswith(".") or host.endswith("."):
        return f"域名不能以点开头或结尾：{host}"
    for label in host.split("."):
        if not label:
            return f"域名含空标签：{host}（不能有连续的点）"
        if label.startswith("-") or label.endswith("-"):
            return f"域名标签不能以连字符开头或结尾：{label}"
    return ""


def _pick_probe_url(site: dict) -> str:
    """拨测地址：域名 > 公网地址 > 内网地址。无 scheme 时补 http://"""
    url = (site.get("domain") or "").strip() or (site.get("public_url") or "").strip() or (site.get("private_url") or "").strip()
    if not url:
        return ""
    if not url.lower().startswith(("http://", "https://")):
        url = "http://" + url
    return url


# 环境标识 -> 拨测 job 名称后缀
_ENV_SUFFIX = {"生产环境": "-生产环境", "测试环境": "-测试环境"}


def _probe_job(site: dict) -> str:
    """拨测 job 名 = 站点名称 + 环境后缀"""
    name = (site.get("name") or "").strip()
    env = (site.get("env") or "").strip()
    suffix = _ENV_SUFFIX.get(env, "")
    return f"{name}{suffix}" if suffix else name


def _site_to_http_target(site: dict) -> dict | None:
    """站点 → HTTP 拨测目标（monitor=true 且有效地址才生成）"""
    if not site.get("monitor"):
        return None
    url = _pick_probe_url(site)
    if not url:
        return None
    codes = (site.get("probe_status_codes") or "").strip() or "200"
    timeout = (site.get("probe_timeout") or "").strip()
    skip_verify = bool(site.get("probe_insecure_skip_verify"))
    tls_ca = "" if skip_verify else (site.get("probe_tls_ca") or "").strip()
    target = {
        "id": site["id"],
        "kind": KIND_HTTP,
        "url": url,
        "job": _probe_job(site),
        "method": (site.get("probe_method") or "").strip() or "GET",
        "expected_status_codes": codes,
        "headers": _parse_probe_headers(site.get("probe_headers") or ""),
        "body": site.get("probe_body") or "",
        "follow_redirects": site.get("probe_follow_redirects"),
        # use_tls 由是否需要 TLS 配置块自动推导（对齐 Go 版 normalizeTLS）
        "use_tls": skip_verify or bool(tls_ca),
        "tls_ca": tls_ca,
        "insecure_skip_verify": skip_verify,
    }
    if timeout:
        target["response_timeout"] = timeout
    return target


def _has_url(rec: dict) -> bool:
    """域名/公网/内网任一非空 → 走 HTTP 拨测；否则有 connection 时走端口拨测。
    判断"会不会产生端口拨测目标"必须用它，不能只看 connection 是否为空"""
    return any((rec.get(k) or "").strip() for k in ("domain", "public_url", "private_url"))


def _port_ok(port) -> bool:
    """端口是否合法（1-65535）。0 保留给特权进程、探测它没有意义，也不放行。"""
    try:
        return 1 <= int(port) <= 65535
    except (TypeError, ValueError):
        return False


def _extract_host_port(value: str) -> str:
    """从 'host:port' 或 'scheme://[user:pass@]host:port[/path]' 提取 host:port。
    提取不到、端口非法都返回空串，**绝不抛异常**：
    urlparse().port 在端口越界或非数字时会抛 ValueError，而这个函数会被
    /api/config/http_response 对每条记录调用一次 —— 一条脏数据就能让整个
    配置端点 500，所有拨测目标一起消失。"""
    v = (value or "").strip()
    if not v:
        return ""
    import re
    m = re.match(r"^[a-zA-Z0-9._-]+:(\d+)$", v)
    if m:
        return v if _port_ok(m.group(1)) else ""
    if "://" in v:
        try:
            u = urlparse(v)
            host, port = u.hostname, u.port
        except ValueError:
            return ""
        if host and _port_ok(port):
            return f"{host}:{port}"
    return ""


def _pick_probe_addr(site: dict) -> str:
    """从连接串提取 host:port，用于端口拨测。无法提取返回空串。"""
    return _extract_host_port(site.get("connection") or "")


def _connection_port_error(connection: str) -> str:
    """连接串里的端口越界/格式非法时返回错误信息，否则返回空串。
    在写入时拦住，而不是等生成 TOML 时才发现问题。"""
    import re
    c = (connection or "").strip()
    if not c:
        return ""
    # 无 scheme 的裸 host:port 形态：端口段必须是 1-65535 的数字。
    # 原来只匹配 \d+，"10.0.0.5:abc" 两个分支都不命中 → 静默存库，而且永远产出
    # 不了拨测目标，用户看不出"这个资源根本没在被监测"。
    # 用 partition 而不是整串正则，"10.0.0.5:abc:1" 这种多冒号的也能抓到。
    # 主机名部分不在合法字符集内的（自由格式连接串）不拦，避免误伤。
    if "://" not in c and ":" in c:
        head, _, tail = c.partition(":")
        if re.match(r"^[a-zA-Z0-9._-]+$", head):
            port = tail.strip()
            if not port.isdigit():
                return f"连接串端口 {port!r} 非法（应为 1-65535 的数字）"
            if not _port_ok(port):
                return f"连接串端口 {port} 非法（应为 1-65535）"
            return ""
    if "://" in c:
        try:
            port = urlparse(c).port
        except ValueError:
            # urlparse().port 对非数字端口和越界端口都抛 ValueError
            return f"连接串端口非法（应为 1-65535 的数字）: {c}"
        if port is None or _port_ok(port):
            return ""
        return f"连接串端口 {port} 非法（应为 1-65535）"
    return ""


def _site_to_net_target(site: dict) -> dict | None:
    """站点 → 端口拨测目标（monitor=true 且无 URL 但有 connection 才生成）。
    从连接串提取 host:port；协议/超时/send/expect 走站点自己的拨测字段，
    所以端口拨测的配置全部在站点表单里完成，不再需要独立的端口拨测管理页。"""
    if not site.get("monitor"):
        return None
    # 有 URL 的站点走 HTTP 拨测，不在这里重复
    if _pick_probe_url(site):
        return None
    addr = _pick_probe_addr(site)
    if not addr:
        return None
    protocol = (site.get("probe_protocol") or "").strip().lower()
    if protocol not in ("tcp", "udp"):
        protocol = "tcp"   # 历史脏数据兜底
    target = {
        "id": site["id"],
        "kind": KIND_NET,
        "url": addr,
        "job": _probe_job(site),
        "protocol": protocol,
    }
    # 字段名不同（站点带 probe_ 前缀），非空才写，让 TOML 走 categraf 默认值
    for src, dst in (("probe_timeout", "timeout"), ("probe_read_timeout", "read_timeout"),
                     ("probe_send", "send"), ("probe_expect", "expect")):
        val = (site.get(src) or "").strip()
        if val:
            target[dst] = val
    return target


def _parse_probe_headers(raw) -> list[str]:
    """解析请求头 JSON 数组。非法 JSON 或非字符串数组抛 ValueError（对齐 Go 版 targetFromForm）"""
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(h) for h in raw]
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError(f"请求头 JSON 格式非法: {e}")
    if not isinstance(parsed, list) or not all(isinstance(h, str) for h in parsed):
        raise ValueError("请求头 JSON 格式非法: 应为字符串数组，如 [\"X-Key\",\"val\"]")
    return parsed


def _normalize_probe_tls(data: dict) -> None:
    """归一化 TLS 字段（对齐 Go 版 normalizeTLS）：跳过校验与 CA 互斥（跳过优先，清空 CA）"""
    data["probe_tls_ca"] = (data.get("probe_tls_ca") or "").strip()
    if data.get("probe_insecure_skip_verify"):
        data["probe_tls_ca"] = ""


# ── 端口拨测目标存储（data/probes.json）──


class ProbeIn(BaseModel):
    """端口拨测目标输入模型"""
    url: str = Field(min_length=1, max_length=200)  # host:port
    job: str = Field(min_length=1, max_length=100)
    protocol: str = Field(default="tcp", pattern=r"^(tcp|udp)$")
    timeout: str = Field(default="", pattern=r"^(\d+(ms|s|m))?$")
    read_timeout: str = Field(default="", pattern=r"^(\d+(ms|s|m))?$")
    send: str = Field(default="", max_length=500)
    expect: str = Field(default="", max_length=500)


def _load_probes_unlocked() -> list[dict]:
    """无锁加载：调用方必须已持有 _probes_lock"""
    if not PROBES_FILE.exists():
        return []
    try:
        raw = json.loads(PROBES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # 主文件损坏：尝试从备份恢复
        if PROBES_BACKUP.exists():
            try:
                raw = json.loads(PROBES_BACKUP.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return []
        else:
            return []
    if not isinstance(raw, list):
        return []
    # 确保每条记录有合法 id
    result = []
    for item in raw:
        if isinstance(item, dict) and item.get("url"):
            if not isinstance(item.get("id"), str) or len(item.get("id", "")) != 8:
                item["id"] = secrets.token_hex(4)
            result.append(item)
    return result


def _load_probes() -> list[dict]:
    """加载端口拨测目标列表"""
    with _probes_lock:
        return _load_probes_unlocked()


def _save_probes_unlocked(probes: list[dict]) -> None:
    """无锁保存：调用方必须已持有 _probes_lock"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if PROBES_FILE.exists():
        try:
            PROBES_BACKUP.write_bytes(PROBES_FILE.read_bytes())
        except OSError:
            pass
    tmp = PROBES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(probes, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, PROBES_FILE)


def _save_probes(probes: list[dict]) -> None:
    """保存端口拨测目标列表（带备份轮转）"""
    with _probes_lock:
        _save_probes_unlocked(probes)


def _load_mutate_probes(fn) -> list:
    """原子读-改-写：fn(probes) 原地修改列表。fn 抛 HTTPException 时不写入。
    与站点 _load_mutate 同模式：防止 _load_probes→_save_probes 之间被并发请求覆盖。"""
    with _probes_lock:
        probes = _load_probes_unlocked()
        fn(probes)
        _save_probes_unlocked(probes)
        return probes


def _all_probe_targets() -> list[dict]:
    """汇总所有拨测目标：HTTP（有 URL 的站点）+ 端口（connection 站点 + probes.json 独立管理）"""
    sites = _load()
    targets = []
    for s in sites:
        # 有 URL → HTTP 拨测；无 URL 有 connection → 端口拨测
        t = _site_to_http_target(s)
        if t:
            targets.append(t)
        else:
            t = _site_to_net_target(s)
            if t:
                targets.append(t)
    net_targets = _load_probes()
    return targets + net_targets


# ── categraf Bearer token 认证 ──


def _require_categraf_token(authorization: str | None = Header(default=None)) -> None:
    """categraf http_provider 拉取端点的 Bearer token 认证。
    未配 CATEGRAF_TOKEN 时拒绝一切拉取请求（fail closed，防止配置意外裸露）。
    常量时间比较防时序侧信道。"""
    if not CATEGRAF_TOKEN:
        raise HTTPException(status_code=401, detail="unauthorized")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="unauthorized")
    token = authorization[7:]
    if not hmac.compare_digest(token.encode(), CATEGRAF_TOKEN.encode()):
        raise HTTPException(status_code=401, detail="unauthorized")


# ── categraf http_provider 端点 ──


@app.get("/api/config/http_response")
def categraf_config(authorization: str | None = Header(default=None)):
    """categraf http_provider 配置拉取端点。
    同一个 URL 同时下发 http_response 和 net_response 两个插件配置。
    version 是全部目标内容的 MD5 哈希，内容不变则 version 不变，
    categraf 不会误重启采集实例。"""
    _require_categraf_token(authorization)
    targets = _all_probe_targets()
    version = config_version(targets)
    return {
        "version": version,
        "configs": {
            "http_response": {
                version: {
                    "config": generate_http_toml(targets),
                    "format": "toml",
                }
            },
            "net_response": {
                version: {
                    "config": generate_net_toml(targets),
                    "format": "toml",
                }
            },
        },
    }


# ── 端口拨测目标 CRUD API（管理员）──


@app.get("/api/config/preview")
def config_preview(authorization: str | None = Header(default=None)):
    """给管理页面预览当前生成的 TOML 配置（对齐原 categraf-http-admin 首页的 TOML 展示）"""
    _require_admin(authorization)
    targets = _all_probe_targets()
    return {
        "version": config_version(targets),
        "http_toml": generate_http_toml(targets),
        "net_toml": generate_net_toml(targets),
    }


@app.get("/api/probes")
def list_probes(authorization: str | None = Header(default=None)):
    """查看所有端口拨测目标"""
    _require_admin(authorization)
    return _load_probes()


def _site_net_addrs() -> dict[str, str]:
    """站点派生出的端口拨测目标：host:port(小写) -> 站点名称。
    忽略 monitor 开关：开关随时可以打开，一旦打开就会和独立目标在 net_response
    里撞成重复键。有 URL 的站点走 HTTP 拨测，不产生端口目标。"""
    out: dict[str, str] = {}
    for s in _load():
        if _has_url(s):
            continue
        addr = _pick_probe_addr(s)
        if addr:
            out.setdefault(addr.lower(), s.get("name") or "")
    return out


def _check_probe_url_site_conflict(url: str) -> None:
    """独立端口拨测目标与站点派生出的目标撞车时返回 409。
    同一个 host:port 在 net_response 里出现两次会产生重复的 [mappings] 键，
    TOML 解析直接失败，后果是所有端口拨测一起中断（不是只少这一条）。
    必须在 _load_mutate_probes 之外调用：那里已持有 _probes_lock，再调 _load()
    会变成 probes→file 反向持锁，与 _all_probe_targets 的 file→probes 构成死锁。"""
    name = _site_net_addrs().get(url.strip().lower())
    if name is not None:
        raise HTTPException(
            status_code=409,
            detail=f"目标地址 {url} 已被站点「{name}」的连接串使用，两者会生成重复的端口拨测目标",
        )


@app.post("/api/probes")
def create_probe(probe: ProbeIn, authorization: str | None = Header(default=None)):
    """添加端口拨测目标"""
    _require_admin(authorization)
    data = probe.model_dump()
    # 校验 host:port 格式
    _validate_host_port(data["url"])
    _check_probe_url_site_conflict(data["url"])
    # 转义 send/expect 中的控制字符
    data["send"] = _unescape_ctl(data["send"])
    data["expect"] = _unescape_ctl(data["expect"])
    data["kind"] = KIND_NET

    def _mutate(probes):
        # URL 不允许重复；job 在同类型内不允许重复
        for p in probes:
            if p["url"] == data["url"]:
                raise HTTPException(status_code=409, detail=f"目标地址已存在: {data['url']}")
            if p.get("job") == data["job"]:
                raise HTTPException(status_code=409, detail=f"job 名称已存在: {data['job']}")
        data["id"] = secrets.token_hex(4)
        data["created_at"] = _now()
        probes.append(data)

    _load_mutate_probes(_mutate)
    return data


@app.put("/api/probes/{probe_id}")
def update_probe(probe_id: str, probe: ProbeIn, authorization: str | None = Header(default=None)):
    """编辑端口拨测目标"""
    _require_admin(authorization)
    data = probe.model_dump()
    _validate_host_port(data["url"])
    _check_probe_url_site_conflict(data["url"])
    data["send"] = _unescape_ctl(data["send"])
    data["expect"] = _unescape_ctl(data["expect"])
    data["kind"] = KIND_NET

    captured: dict = {}

    def _mutate(probes):
        idx = next((i for i, p in enumerate(probes) if p["id"] == probe_id), None)
        if idx is None:
            raise HTTPException(status_code=404, detail="拨测目标不存在")
        # 检查重复（排除自身）
        for p in probes:
            if p["id"] != probe_id:
                if p["url"] == data["url"]:
                    raise HTTPException(status_code=409, detail=f"目标地址已存在: {data['url']}")
                if p.get("job") == data["job"]:
                    raise HTTPException(status_code=409, detail=f"job 名称已存在: {data['job']}")
        data["id"] = probe_id
        data["created_at"] = probes[idx].get("created_at", _now())
        data["updated_at"] = _now()
        probes[idx] = data
        captured["data"] = data

    _load_mutate_probes(_mutate)
    return captured["data"]


@app.delete("/api/probes/{probe_id}")
def delete_probe(probe_id: str, authorization: str | None = Header(default=None)):
    """删除端口拨测目标"""
    _require_admin(authorization)

    def _mutate(probes):
        idx = next((i for i, p in enumerate(probes) if p["id"] == probe_id), None)
        if idx is None:
            raise HTTPException(status_code=404, detail="拨测目标不存在")
        probes.pop(idx)

    _load_mutate_probes(_mutate)
    return {"ok": True}


def _validate_host_port(url: str) -> None:
    """校验 host:port 格式 + 端口范围。
    原来只查格式不查范围：越界端口会落进 probes.json，categraf 拨测失败，
    而界面上分不清是配置错还是网络不通。"""
    import re
    m = re.match(r"^[a-zA-Z0-9._-]+:(\d+)$", url.strip())
    if not m:
        raise HTTPException(
            status_code=400,
            detail=f"目标地址格式非法: {url}（应为 host:port，如 10.0.0.1:22）",
        )
    if not _port_ok(m.group(1)):
        raise HTTPException(status_code=400, detail=f"目标地址端口 {m.group(1)} 非法（应为 1-65535）")


def _unescape_ctl(s: str) -> str:
    """把表单里输入的 \r \n \t 转义序列还原成真实控制字符"""
    return s.replace("\\r", "\r").replace("\\n", "\n").replace("\\t", "\t")


# 中国大陆自 1991 年起无夏令时，固定 UTC+8 偏移即可，不需要 tzdata。
# 不显式指定时区会得到容器本地时间（默认 UTC），比北京时间早 8 小时。
# 不用 zoneinfo：python:slim 镜像不带 /usr/share/zoneinfo，会抛 ZoneInfoNotFoundError。
TZ_CN = _dt.timezone(_dt.timedelta(hours=8), name="Asia/Shanghai")


def _now() -> str:
    """北京时间 YYYY-MM-DD HH:MM:SS。所有写入的时间戳统一走这里"""
    return _dt.datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")


def _dup_index_entry(s: dict) -> dict:
    """把一条记录预处理成查重用的比较键。

    索引里的字段必须和 _find_duplicate 的判断一一对应，否则查重结果会分叉。"""
    return {
        "id": s["id"],
        "name": (s.get("name") or "").strip().lower(),
        "env": (s.get("env") or "").strip(),
        "urls": {_norm_url(s.get(k)) for k in ("domain", "public_url", "private_url")} - {""},
        "conn": (s.get("connection") or "").strip().lower(),
        "addr": "" if _has_url(s) else _pick_probe_addr(s),
        "display": s.get("name") or "",
    }


def _build_dup_index(sites: list) -> list:
    return [_dup_index_entry(s) for s in sites]


def _find_duplicate(sites: list, data: dict, exclude_id: str | None = None,
                    probe_addrs: set | None = None, index: list | None = None) -> str:
    """多维查重：名称、域名/公网/内网地址、连接串、端口拨测目标。
    返回冲突描述，无冲突返回空串。

    probe_addrs / index 都是可选的预计算结果，批量调用（导入）时传进来：
      - index 不传则每次扫描都重新 norm URL、解析连接串，5000 行 x 2000 条要 55 秒
      - probe_addrs 不传则每行读一次 probes.json
    """
    if index is None:
        index = _build_dup_index(sites)
    name = data["name"].strip().lower()
    env = (data.get("env") or "").strip()
    urls = {_norm_url(data.get(k)) for k in ("domain", "public_url", "private_url")} - {""}
    conn = (data.get("connection") or "").strip().lower()
    # 端口拨测目标地址：只有"无 URL + 有 connection"的记录才会产生端口目标，
    # 其 TOML 目标 key 是连接串解析出的 host:port，而不是连接串本身
    has_url = _has_url(data)
    data_addr = "" if has_url else _pick_probe_addr(data)
    # 独立管理的端口拨测目标（data/probes.json）。
    # 锁顺序 file→probes，与 _all_probe_targets 一致，不构成反向持锁。
    if probe_addrs is None:
        probe_addrs = {(p.get("url") or "").strip().lower() for p in _load_probes()} - {""}
    # 与拨测管理页的独立目标撞车（与 sites 内容无关，必须在循环外）
    if data_addr and data_addr.lower() in probe_addrs:
        return f"端口拨测目标 {data_addr} 已被拨测管理页的独立目标占用"
    for e in index:
        if exclude_id is not None and e["id"] == exclude_id:
            continue
        # 同名同环境不允许重复；同名不同环境（分组卡：同站点跨环境）允许
        if e["name"] == name and e["env"] == env:
            return f"同名同环境的「{e['display']}」已存在（环境：{env or '未指定'}）"
        hit = urls & e["urls"]
        if hit:
            return f"地址 {next(iter(hit))} 已被「{e['display']}」使用"
        if conn and conn == e["conn"]:
            return f"连接串已被「{e['display']}」使用"
        # 端口拨测目标地址查重。同一个 host:port 在 net_response 里出现两次会产生
        # 重复的 [mappings] 键，TOML 解析直接失败，后果是**所有**端口拨测一起中断，
        # 而不是只少这一条。来源有两种，都要拦：
        #   1) 两个站点连接串写法不同但解析出同一地址：172.16.16.78:6379 vs redis://172.16.16.78:6379
        #      —— 上面的连接串字面比较拦不住
        #   2) 站点连接串解析出的地址与拨测管理页的独立目标相同
        # 有 URL 的记录走 HTTP 拨测，其目标 key 是完整 URL，不可能和 host:port 撞上，
        # 所以 addr 为空（有 URL）时直接跳过。
        if data_addr and e["addr"] and e["addr"].lower() == data_addr.lower():
            return f"端口拨测目标 {data_addr} 已被「{e['display']}」使用（连接串写法不同但解析出同一个 host:port）"
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


@app.get("/probes")
def probes_page():
    """端口拨测管理页面（需登录，管理员才能操作）"""
    return FileResponse(BASE_DIR / "static" / "probes.html")


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
    # monitor=true 的站点自动成为拨测目标，写站点即生效，无需额外同步
    return item


def _validate_site_payload(data: dict) -> None:
    """校验创建/更新站点的最小字段：URL 或连接串至少填一个；勾选拨测必须有 URL 或连接串。
    有 URL → HTTP 拨测（校验状态码/headers 等细粒度字段）；只有 connection → 端口拨测（不需 HTTP 字段）"""
    if not any((data.get(k) or "").strip() for k in ("domain", "public_url", "private_url", "connection")):
        raise HTTPException(status_code=400, detail="域名/公网/内网/连接串至少填一个")
    # 连接串端口越界要在写入时拦住。否则记录落库后，urlparse().port 在生成
    # TOML 时抛 ValueError，把整个配置端点打成 500，所有拨测目标一起消失。
    conn_err = _connection_port_error(data.get("connection") or "")
    if conn_err:
        raise HTTPException(status_code=400, detail=conn_err)
    if data.get("monitor"):
        has_url = any((data.get(k) or "").strip() for k in ("domain", "public_url", "private_url"))
        has_conn = bool((data.get("connection") or "").strip())
        if not has_url and not has_conn:
            raise HTTPException(status_code=400, detail="勾选拨测监控需要至少填一个 URL 地址或连接串")
        # HTTP 拨测的细粒度校验只在有 URL 时做（端口拨测不需要 method/headers/body/status_codes）
        if has_url:
            codes_err = validate_status_codes(data.get("probe_status_codes", ""))
            if codes_err:
                raise HTTPException(status_code=400, detail=codes_err)
            try:
                _parse_probe_headers(data.get("probe_headers") or "")
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
    # TLS 字段归一化：跳过校验与私有 CA 互斥（跳过优先，清空 CA），无论是否勾选拨测都统一处理
    _normalize_probe_tls(data)

    # 还原 send/expect 里 \r \n \t 的字面转义为真实控制字符（对齐 /api/probes）。
    # 输入校验 _reject_send_expect_control_chars 已拒收真控制字符，这里只把用户按转义写法
    # 输入的字面文本还原，这样 categraf 收到的才是真正的回车/换行/制表。
    data["probe_send"] = _unescape_ctl(data.get("probe_send") or "")
    data["probe_expect"] = _unescape_ctl(data.get("probe_expect") or "")


@app.put("/api/sites/{site_id}")
def update_site(site_id: str, site: SiteIn, authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    # 先读一次现有记录，顺带让 404 在这里给出，不用等 _mutate。
    # 这里是只读检查，可能与并发写入竞态；不影响数据正确性（_mutate 内有最终 404 兜底）。
    existing = next((s for s in _load() if s["id"] == site_id), None)
    if existing is None:
        raise HTTPException(status_code=404, detail="条目不存在")
    data = site.model_dump()
    data["name"] = (data.get("name") or "").strip()
    _validate_site_payload(data)

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
    # monitor=true 的站点自动成为拨测目标，写站点即生效，无需额外同步
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
    # 删除站点后，其拨测目标自动消失（下次 categraf 拉取时不再包含）
    return {"ok": True}


@app.get("/api/monitor-status")
def monitor_status(authorization: str | None = Header(default=None)):
    """各系统拨测状态（写站点即生效，无需同步状态跟踪）"""
    _require_user(authorization)
    # 返回空 dict：拨测目标从站点数据实时生成，不存在"同步失败"状态
    return {}


@app.get("/api/monitor-config")
def monitor_config(authorization: str | None = Header(default=None)):
    """拨测配置状态。enabled=True 表示本服务可作为 categraf http_provider 使用。
    state：ok=已配置 CATEGRAF_TOKEN，error=未配置（端点拒绝一切拉取请求，fail closed）。"""
    _require_user(authorization)
    if CATEGRAF_TOKEN:
        return {"enabled": True, "state": "ok", "reason": ""}
    return {"enabled": False, "state": "error", "reason": "未配置 CATEGRAF_TOKEN，/api/config/http_response 将拒绝所有拉取请求（fail closed）"}


# ── 批量导入 / 导出 ──


def _parse_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "是", "y")


def _parse_tri_bool(v) -> bool | None:
    """三态布尔：空/none/null → None（用 categraf 默认值），其余走 _parse_bool"""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("", "none", "null"):
        return None
    return s in ("1", "true", "yes", "是", "y")


def _normalize_import_row(row: dict, idx: int) -> dict:
    name = str(row.get("name") or row.get("系统名称") or "").strip()
    if not name:
        raise ValueError(f"第{idx}行缺少系统名称")
    kind = str(row.get("kind") or row.get("资源类型") or "网站").strip() or "网站"
    env = str(row.get("env") or row.get("环境标识") or "").strip()
    if env not in ("生产环境", "测试环境"):
        raise ValueError(f"第{idx}行「{name}」环境标识必须是 生产环境 或 测试环境")
    monitor = _parse_bool(row.get("monitor") if "monitor" in row else row.get("拨测监控"))
    # 占位符归一必须在「至少填一个」校验之前：一整行 URL 都是 "-" 时应当报"什么都没填"
    urls = [
        _blank_if_placeholder(str(row.get(k) or row.get(cn) or ""))
        for k, cn in (("domain", "域名"), ("public_url", "公网地址"), ("private_url", "内网地址"))
    ]
    connection = _blank_if_placeholder(str(row.get("connection") or row.get("连接串") or ""))
    # URL 三字段和连接串至少填一个（非 URL 类资源允许只填连接串）
    if not any(urls) and not connection:
        raise ValueError(f"第{idx}行「{name}」域名/公网/内网/连接串至少填一个")
    result = {
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
        # 细粒度拨测配置（与创建/更新路径一致）
        "probe_method": str(row.get("probe_method") or row.get("拨测方法") or "").strip(),
        "probe_headers": str(row.get("probe_headers") or row.get("拨测请求头") or "").strip(),
        "probe_body": str(row.get("probe_body") or row.get("拨测Body") or "").strip(),
        "probe_follow_redirects": _parse_tri_bool(row.get("probe_follow_redirects") if "probe_follow_redirects" in row else row.get("跟随重定向")),
        "probe_insecure_skip_verify": _parse_bool(row.get("probe_insecure_skip_verify") if "probe_insecure_skip_verify" in row else row.get("跳过证书校验")),
        "probe_tls_ca": str(row.get("probe_tls_ca") or row.get("私有CA路径") or "").strip(),
    }

    # 表格占位符归一为空。不处理 name / env / monitor（有独立校验，不能变空）
    for k in ("category", "kind", "owner", "remark", "domain", "public_url", "private_url",
              "connection", "probe_status_codes", "probe_timeout", "probe_method",
              "probe_headers", "probe_body", "probe_tls_ca"):
        result[k] = _blank_if_placeholder(result[k])
    return result


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
                "probe_method": item_data.get("probe_method", ""),
                "probe_headers": item_data.get("probe_headers", ""),
                "probe_body": item_data.get("probe_body", ""),
                "probe_follow_redirects": item_data.get("probe_follow_redirects"),
                "probe_insecure_skip_verify": item_data.get("probe_insecure_skip_verify", False),
                "probe_tls_ca": item_data.get("probe_tls_ca", ""),
            }).model_dump()
            # 与创建/更新路径同一条校验线：勾选拨测必须有 URL 或连接串，格式校验一致
            _validate_site_payload(validated)
            parsed.append(validated)
        except HTTPException as exc:
            skipped.append({"reason": f"第{idx}行：{exc.detail}"})
        except (ValueError, Exception) as exc:  # noqa: BLE001
            skipped.append({"reason": str(exc)})

    # 原子读-改-写：_load_mutate 内做去重 + 批量 append
    # 独立拨测目标一次性取出：循环里每行调用 _find_duplicate 时传进去，
    # 不然 5000 行就是 5000 次读 probes.json
    probe_addrs = {(p.get("url") or "").strip().lower() for p in _load_probes()} - {""}
    added: list = []

    def _mutate(sites):
        # 一次性建索引：不然每行查重都重新 norm URL + 解析连接串（5000 行 x 2000 条 = 55 秒）
        index = _build_dup_index(sites)
        now = _now()
        for item_data in parsed:
            # 走和创建/更新完全同一条查重线。原来这里另写一套字面比较，
            # 后果是 _find_duplicate 里的检查对导入路径全部失效：
            #   - 撞拨测页的独立端口目标（导入能塞进重复的 host:port）
            #   - 两个站点连接串写法不同但解析出同一 host:port（字面比拦不住）
            dup = _find_duplicate(sites, item_data, probe_addrs=probe_addrs, index=index)
            if dup:
                skipped.append({"reason": f"{dup}，重复项已跳过"})
                continue
            item = {
                "id": secrets.token_hex(4),
                **item_data,
                "created_at": now,
                "updated_at": now,
            }
            sites.append(item)
            index.append(_dup_index_entry(item))   # 后续行也要和新写入的记录查重
            added.append(item)

    _load_mutate(_mutate)

    # monitor=true 的站点自动成为拨测目标，写站点即生效
    return {"added": len(added), "skipped_count": len(skipped), "skipped": skipped[:20]}


def _export_csv(sites: list) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    header_cn = ["系统名称", "资源类型", "分类", "域名", "公网地址", "内网地址", "连接串", "负责人", "环境标识", "备注", "拨测监控", "拨测状态码", "拨测超时", "拨测方法", "拨测请求头", "拨测Body", "跟随重定向", "跳过证书校验", "私有CA路径"]
    writer.writerow(header_cn)
    key_map = ["name", "kind", "category", "domain", "public_url", "private_url", "connection", "owner", "env", "remark", "monitor", "probe_status_codes", "probe_timeout", "probe_method", "probe_headers", "probe_body", "probe_follow_redirects", "probe_insecure_skip_verify", "probe_tls_ca"]
    for s in sites:
        row = []
        for k in key_map:
            v = s.get(k, "")
            if k == "probe_follow_redirects":
                row.append("" if v is None else ("true" if v else "false"))
            elif isinstance(v, bool):
                row.append("true" if v else "false")
            else:
                row.append(v)
        writer.writerow(row)
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
