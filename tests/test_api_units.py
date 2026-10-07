# -*- coding: utf-8 -*-
"""进程内单元测试：登录限流、categraf provider 鉴权、数据持久化/备份/损坏恢复、密码策略。

跑法：python tests/test_api_units.py

为什么单独一个文件、不走 HTTP：
  - 登录限流是"每 IP 10 次 / 15 分钟"，打到真实服务上会把本机 IP 连浏览器一起锁 15 分钟，
    所以这里用**伪造 IP** 直接调 app.login()，只动那个假 IP 的计数
  - 持久化/损坏恢复需要制造"主文件坏掉、备份链残缺"的场景，绝不能拿真实 data/ 做实验，
    所以把 DATA_DIR 指到临时目录再调底层函数
CATEGRAF_TOKEN 必须在 import app 之前塞进环境（模块级常量只在导入时读一次）。
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["CATEGRAF_TOKEN"] = "unit-test-token"   # 必须在 import app 之前
import app  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))


# ── 1. 登录限流（伪造 IP，不碰本机真实计数） ──────────────────────────
print("== 登录限流 ==")


class _Client:
    def __init__(self, host):
        self.host = host


class _Req:
    def __init__(self, host):
        self.client = _Client(host)


FAKE_IP = "203.0.113.77"          # TEST-NET-3，本机不可能有真实请求来自它
FAKE_IP2 = "203.0.113.78"
app._login_attempts.pop(FAKE_IP, None)
app._login_attempts.pop(FAKE_IP2, None)

codes = []
for _ in range(app._LOGIN_MAX_FAILS):
    try:
        app.login(app.LoginIn(username="definitely_no_such_user", password="WrongPass123"), _Req(FAKE_IP))
        codes.append(200)
    except Exception as e:
        codes.append(getattr(e, "status_code", "EXC"))
check(f"前 {app._LOGIN_MAX_FAILS} 次失败都是 401", all(c == 401 for c in codes), str(codes))
try:
    app.login(app.LoginIn(username="definitely_no_such_user", password="WrongPass123"), _Req(FAKE_IP))
    check("达到上限后再试 → 429", False, "没有限流")
except Exception as e:
    check("达到上限后再试 → 429", getattr(e, "status_code", None) == 429,
          f"status={getattr(e, 'status_code', None)} {getattr(e, 'detail', '')}")
# 限流按 IP 隔离：换个 IP 不受影响
try:
    app.login(app.LoginIn(username="definitely_no_such_user", password="WrongPass123"), _Req(FAKE_IP2))
    check("限流按 IP 隔离", False, "另一个 IP 也被拦")
except Exception as e:
    check("限流按 IP 隔离", getattr(e, "status_code", None) == 401,
          f"status={getattr(e, 'status_code', None)}")
# 成功登录重置计数
admin_pw = ""
envp = ROOT / ".env"
if envp.exists():
    for line in envp.read_text(encoding="utf-8").splitlines():
        if line.startswith("ADMIN_PASSWORD="):
            admin_pw = line.split("=", 1)[1].strip().strip("'\"")
app._login_attempts[FAKE_IP2] = (3, __import__("time").monotonic())
if admin_pw:
    try:
        app.login(app.LoginIn(username="admin", password=admin_pw), _Req(FAKE_IP2))
        check("成功登录后计数被重置", FAKE_IP2 not in app._login_attempts, str(app._login_attempts.get(FAKE_IP2)))
    except Exception as e:
        check("成功登录后计数被重置", False, f"{type(e).__name__}: {e}")
else:
    print("SKIP  成功登录重置计数（.env 无 ADMIN_PASSWORD）")
app._login_attempts.pop(FAKE_IP, None)
app._login_attempts.pop(FAKE_IP2, None)

# ── 2. categraf provider 鉴权与载荷 ──────────────────────────────────
print("\n== categraf provider ==")
check("导入时 CATEGRAF_TOKEN 生效", app.CATEGRAF_TOKEN == "unit-test-token", app.CATEGRAF_TOKEN)
try:
    out = app.categraf_config(authorization="Bearer unit-test-token")
    keys = set(out.get("configs", {}).keys())
    check("正确 token 返回 http+net 两份配置", keys == {"http_response", "net_response"}, str(keys))
    check("返回 version", bool(out.get("version")), str(out.get("version"))[:12])
    http_cfg = list(out["configs"]["http_response"].values())[0]
    net_cfg = list(out["configs"]["net_response"].values())[0]
    check("配置是 toml 格式", http_cfg.get("format") == "toml" and net_cfg.get("format") == "toml")
    check("配置体是字符串", isinstance(http_cfg.get("config"), str) and isinstance(net_cfg.get("config"), str))
except Exception as e:
    check("正确 token 返回配置", False, f"{type(e).__name__}: {e}")
for label, tok in (("错误 token", "Bearer wrong"), ("无 token", None), ("空 Bearer", "Bearer ")):
    try:
        app.categraf_config(authorization=tok)
        check(f"{label} → 拒绝", False, "竟然通过了")
    except Exception as e:
        check(f"{label} → 拒绝", getattr(e, "status_code", None) == 401,
              f"status={getattr(e, 'status_code', None)}")
_old = app.CATEGRAF_TOKEN
app.CATEGRAF_TOKEN = ""
try:
    app.categraf_config(authorization="Bearer anything")
    check("未配置 token 时 fail closed", False, "未配置竟然放行")
except Exception as e:
    check("未配置 token 时 fail closed", getattr(e, "status_code", None) == 401,
          f"status={getattr(e, 'status_code', None)}")
app.CATEGRAF_TOKEN = _old

# ── 3. 密码策略 ──────────────────────────────────────────────────────
print("\n== 密码策略 ==")
def pw_ok(pw):
    try:
        app._validate_password(pw)
        return True, ""
    except Exception as e:
        return False, getattr(e, "detail", str(e))


for label, pw, want in (
    ("10 位字母+数字", "Abcdef1234", True),
    ("9 位", "Abcdef123", False),
    ("纯字母", "abcdefghij", False),
    ("纯数字", "1234567890", False),
    ("中文+数字（按字节算长度）", "中文密码测试一二三四五六七八", False),
):
    ok, why = pw_ok(pw)
    check(f"密码策略：{label}", ok == want, f"got={ok} {why}")
ok72, why72 = pw_ok("A1" * 40)          # 80 字节 > 72
check("超 72 字节被拒（bcrypt 边界）", not ok72, why72)
ok_ascii, _ = pw_ok("Abcdef1234")
check("合法密码通过", ok_ascii)

# ── 4. 持久化：原子写 / 备份轮转 / 损坏恢复 ──────────────────────────
print("\n== 数据持久化（临时目录） ==")
tmp = Path(tempfile.mkdtemp(prefix="sitesnav-test-"))
orig = (app.DATA_DIR, app.DATA_FILE, app.BACKUP_FILE)
app.DATA_DIR, app.DATA_FILE, app.BACKUP_FILE = tmp, tmp / "sites.json", tmp / "sites.json.bak"
try:
    def rec(name):
        return {**app.FIELD_DEFAULTS, "id": "aaaaaaaa", "name": name, "env": "生产环境"}

    app._save_unlocked([rec("第一版")])
    check("首次写入：主文件存在", app.DATA_FILE.exists())
    check("首次写入：不残留 .tmp", not list(tmp.glob("*.tmp")), str(list(tmp.glob("*"))))
    app._save_unlocked([rec("第二版")])
    check("第二次写入：生成 .bak", app.BACKUP_FILE.exists())
    bak = json.loads(app.BACKUP_FILE.read_text(encoding="utf-8"))
    check(".bak 是上一版内容", bak[0]["name"] == "第一版", bak[0]["name"])
    for i in range(2, app.BACKUP_ROLL + 2):
        app._save_unlocked([rec(f"第{i}版")])
    chain = [app.BACKUP_FILE] + [tmp / f"sites.json.bak.{i}" for i in range(1, app.BACKUP_ROLL)]
    check(f"备份链共 {app.BACKUP_ROLL} 级", all(p.exists() for p in chain),
          str([p.name for p in chain if p.exists()]))

    # 主文件损坏 → 从最近可用备份恢复，并给出 warning
    app.DATA_FILE.write_text("{ 这不是合法 JSON", encoding="utf-8")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        loaded = app._load_unlocked()
    check("主文件损坏可从备份恢复", isinstance(loaded, list) and len(loaded) == 1 and "name" in loaded[0],
          f"n={len(loaded) if isinstance(loaded, list) else '?'}")
    check("恢复后主文件被重写", json.loads(app.DATA_FILE.read_text(encoding="utf-8"))[0].get("name"),
          str(json.loads(app.DATA_FILE.read_text(encoding="utf-8"))[0].get("name")))

    # 主文件坏 + 一个备份也坏 → 记下是哪个备份、为什么坏（不静默）
    app.DATA_FILE.write_text("{ 坏", encoding="utf-8")
    app.BACKUP_FILE.write_text("{ 也坏", encoding="utf-8")
    err2 = io.StringIO()
    with contextlib.redirect_stderr(err2):
        loaded2 = app._load_unlocked()
    check("备份损坏会被记录到 stderr（不再静默）",
          "不可用" in err2.getvalue() and app.BACKUP_FILE.name in err2.getvalue(),
          err2.getvalue().strip()[:120])
    check("坏备份不影响从下一级恢复", isinstance(loaded2, list) and len(loaded2) >= 1, f"n={len(loaded2)}")

    # 全部损坏 → 返回空 + warning
    for p in [app.DATA_FILE] + [app.BACKUP_FILE] + [tmp / f"sites.json.bak.{i}" for i in range(1, app.BACKUP_ROLL)]:
        p.write_text("###", encoding="utf-8")
    loaded3 = app._load_unlocked()
    check("全部损坏 → 返回空数据不抛异常", loaded3 == [], str(loaded3))
    check("全部损坏时有 warning", bool(getattr(app, "_data_warning", "")), str(getattr(app, "_data_warning", ""))[:60])

    # 备份写失败必须出声（模拟只读目标）
    app._save_unlocked([rec("正常版")])
    err3 = io.StringIO()
    real_rotate = app._rotate_backups
    def boom():
        raise OSError("模拟备份失败")
    app._rotate_backups = boom
    try:
        with contextlib.redirect_stderr(err3):
            app._save_unlocked([rec("备份失败版")])
    finally:
        app._rotate_backups = real_rotate
    check("备份失败会打印告警（不再静默吞）", "备份失败" in err3.getvalue(), err3.getvalue().strip()[:120])
    check("备份失败不影响主文件写入",
          json.loads(app.DATA_FILE.read_text(encoding="utf-8"))[0]["name"] == "备份失败版")
finally:
    app.DATA_DIR, app.DATA_FILE, app.BACKUP_FILE = orig
    shutil.rmtree(tmp, ignore_errors=True)

# ── 5. 站点模型的字段清单（前端 sitePayload 必须覆盖全部） ────────────
print("\n== 站点字段清单 ==")
fields = set(app.SiteIn.model_fields.keys())
must = {"name", "kind", "category", "public_url", "private_url", "domain", "connection", "owner",
        "env", "remark", "memo_content", "monitor", "probe_url", "probe_status_codes",
        "probe_timeout", "probe_interval", "probe_method", "probe_headers", "probe_body",
        "probe_follow_redirects", "probe_insecure_skip_verify", "probe_tls_ca",
        "probe_cert_expire", "probe_protocol", "probe_read_timeout", "probe_send", "probe_expect"}
missing = must - fields
check("SiteIn 字段与预期一致", not missing, f"缺: {missing}")
print(f"      SiteIn 共 {len(fields)} 个字段: {','.join(sorted(fields))}")

passed = sum(1 for _, ok in RESULTS if ok)
print(f"\n{passed}/{len(RESULTS)} passed")
if passed != len(RESULTS):
    print("失败项:")
    for n, ok in RESULTS:
        if not ok:
            print("  -", n)
sys.exit(0 if passed == len(RESULTS) else 1)
