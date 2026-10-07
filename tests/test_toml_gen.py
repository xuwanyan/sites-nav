# -*- coding: utf-8 -*-
"""toml_gen.py 回归测试。无第三方依赖，直接跑：python tests/test_toml_gen.py

只依赖标准库 tomllib（Python 3.11+，Dockerfile 用的是 3.12）。

覆盖两个曾经把整套拨测打断的缺陷：
1. HTTP body 用多行基本字符串写 → 每条 body 被加尾随换行；含反斜杠时整份 TOML
   解析失败（Invalid hex value），categraf 拒绝整份 http_response，所有 HTTP 拨测一起中断。
2. 同一个 host:port 出现两次 → [mappings] 重复键 → 整份 net_response 解析失败，
   所有端口拨测一起中断。

这两类问题在 e2e_import.py 里覆盖不到（那个脚本只走导入接口），所以单独放这里。
"""
import importlib.util
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("toml_gen", ROOT / "toml_gen.py")
toml_gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(toml_gen)

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))


def http_target(body="", **overrides):
    t = {
        "id": "a1b2c3d4", "kind": "http", "url": "http://10.0.0.1:8080/api",
        "job": "T-prod", "method": "POST", "expected_status_codes": "200",
        "headers": ["Content-Type", "application/json"], "body": body,
        "use_tls": False, "insecure_skip_verify": False, "follow_redirects": None,
    }
    t.update(overrides)
    return t


def net_target(url, job, **overrides):
    t = {"id": "e5f6a7b8", "kind": "net", "url": url, "job": job, "protocol": "tcp"}
    t.update(overrides)
    return t


def http_body(body):
    """生成 http_response TOML 并解析，返回实例里的 body 值"""
    return tomllib.loads(toml_gen.generate_http_toml([http_target(body)]))["instances"][0]["body"]


# ── 1. body 往返无损 ────────────────────────────────────────────

BODY_CASES = [
    r'{"user":"admin","pwd":"x"}',          # 普通 JSON
    r'{"path":"C:\Users\report"}',          # Windows 路径：原来报 Invalid hex value，整份配置挂
    '{"note":"line1\\nline2"}',             # JSON 里写字面 \n：原来被改成真实换行
    r'{"files":["a.txt"]}',
    "a\\b",                                  # 单反斜杠 + 非法转义
    '"""',                                   # 三引号
    '末尾反斜杠\\',                           # 行续符
    "真实换行\n第二行",
    "制表符\t和回车\r",
    "中文body 全角括号（）",
    '带双引号 "x" 和单引号 \'' ,
]

lost = []
for b in BODY_CASES:
    try:
        got = http_body(b)
    except Exception as e:                     # 解析失败 = 整份配置被拒绝，最严重
        lost.append(f"{b!r} -> 解析失败: {e}")
        continue
    if got != b:
        lost.append(f"{b!r} -> 实际 {got!r}")
check(f"body 往返无损（{len(BODY_CASES)} 组）", not lost, "; ".join(lost))

# 回归点：body 不能被加尾随换行
check("body 不被加尾随换行", http_body('{"k":"v"}') == '{"k":"v"}', repr(http_body('{"k":"v"}')))

# 空 body 不生成 body 字段
inst = tomllib.loads(toml_gen.generate_http_toml([http_target("")]))["instances"][0]
check("空 body 不生成 body 字段", "body" not in inst, str(inst))

# ── 2. 重复目标不产生重复 [mappings] 键 ─────────────────────────

def mappings_keys(txt):
    return list(tomllib.loads(txt)["mappings"].keys())


# 站点连接串写法不同但解析出同一 host:port（生产环境里最容易踩）
net_dup = [
    net_target("172.16.16.78:6379", "Redis-prod", id="a1b2c3d4"),
    net_target("172.16.16.78:6379", "Redis2-prod", id="e5f6a7b8"),
    net_target("10.0.0.5:53", "DNS", id="c9d0e1f2", protocol="udp", send="ping\r"),
]
net_txt = toml_gen.generate_net_toml(net_dup)
keys = mappings_keys(net_txt)
check("net_response 重复 host:port 去重后仍可解析", len(keys) == 2, str(keys))
check("去重保留先出现的", keys[0] == "172.16.16.78:6379", str(keys))
check("去重不影响其他目标", "10.0.0.5:53" in keys, str(keys))

# HTTP 侧同样兜底
http_dup = [
    http_target("x", url="http://h:8080/api", id="a1b2c3d4"),
    http_target("y", url="http://h:8080/api", id="b2c3d4e5"),
]
hk = mappings_keys(toml_gen.generate_http_toml(http_dup))
check("http_response 重复 URL 去重后仍可解析", len(hk) == 1, str(hk))

# 大小写不同的同一地址也算重复
mixed = [
    net_target("10.0.0.1:6379", "A", id="a1b2c3d4"),
    net_target("10.0.0.1:6379", "B", id="e5f6a7b8"),
]
check("host:port 大小写归一后去重", len(mappings_keys(toml_gen.generate_net_toml(mixed))) == 1)

# ── 3. 配置完整性回归 ──────────────────────────────────────────

full = [
    http_target("", id="a1b2c3d4", url="http://47.101.200.201:6080/CMS", job="CMS-prod",
                method="GET", response_timeout="5s"),
    http_target("", id="b2c3d4e5", url="https://oam.internal/login", job="OAM-prod",
                method="GET", expected_status_codes="200|302",
                headers=["X-Token", "abc"], use_tls=True, insecure_skip_verify=True,
                follow_redirects=True),
    net_target("172.16.16.78:6379", "Redis-prod", id="c3d4e5f6", timeout="3s"),
    net_target("10.0.0.5:53", "DNS", id="d4e5f6a7", protocol="udp"),
]
h_txt = toml_gen.generate_http_toml(full)
n_txt = toml_gen.generate_net_toml(full)
check("正常配置 http_response 可解析", isinstance(tomllib.loads(h_txt)["instances"], list))
check("正常配置 net_response 可解析", isinstance(tomllib.loads(n_txt)["instances"], list))
check("空目标列表返回占位注释", toml_gen.generate_http_toml([]).strip().startswith("#"))
# 用 body 而不是 send：send 是端口拨测的字段，对 HTTP 目标不产生任何输出。
# version 现在对「生成的 TOML」取哈希，不改变输出就不该改变 version（见第 5 节）
check("version 内容变化时改变",
      toml_gen.config_version(full) != toml_gen.config_version(
          [dict(full[0], body="zz")] + full[1:]))
check("version 与目标顺序无关",
      toml_gen.config_version(full) == toml_gen.config_version(list(reversed(full))))

# ── 4. 控制字符转义 ─────────────────────────────────────────────
# TOML 要求 U+0000–U+0008、U+000A–U+001F、U+007F 全部转义。少转义一个就是
# Illegal character，整份 net_response 解析失败，所有端口拨测一起中断。
# 0x7F 不在 < 0x20 里，Go 的 %q 同样漏转义，所以这个坑参考实现也有。

for ch, label in [("\x00", "NUL"), ("\x08", "BS"), ("\x0b", "VT"),
                  ("\x1b", "ESC"), ("\x1f", "US"), ("\x7f", "DEL")]:
    txt = toml_gen.generate_net_toml(
        [net_target("10.0.0.9:5000", "J", id="a1b2c3d4", send="a" + ch + "b")])
    try:
        got = tomllib.loads(txt)["instances"][0]["send"]
        check(f"net send 含 {label} 可解析", got == "a" + ch + "b", repr(got))
    except Exception as e:
        check(f"net send 含 {label} 可解析", False, type(e).__name__ + ": " + str(e))


# ── 5. version 必须跟生成的 TOML 同步 ───────────────────────────
# 回归点：config_version 原本是手写的字段清单，漏了 interval 和 cert_expire。
# 这两项只影响实例分组和可选行（interval = ".." / metrics_drop），不显眼：
# 改它们 TOML 变了、version 却没变 → categraf 认为配置没变、不重载 → 改动静默失效。
# 线上「支持指定网址不采集证书过期时间」这个功能就中过招。
# 不变量：只要生成的 TOML 变了，version 就必须跟着变。

VERSION_PAIRS = [
    ("HTTP interval 空 → 15s", "http",
     http_target("", id="a1b2c3d4", interval=""),
     http_target("", id="a1b2c3d4", interval="15s")),
    ("HTTP interval 15s → 30s", "http",
     http_target("", id="a1b2c3d4", interval="15s"),
     http_target("", id="a1b2c3d4", interval="30s")),
    ("HTTP cert_expire None → False", "http",
     http_target("", id="a1b2c3d4"),
     http_target("", id="a1b2c3d4", cert_expire=False)),
    ("HTTP cert_expire False → True", "http",
     http_target("", id="a1b2c3d4", cert_expire=False),
     http_target("", id="a1b2c3d4", cert_expire=True)),
    ("net interval 空 → 30s", "net",
     net_target("10.0.0.1:22", "J1", id="a1b2c3d4", interval=""),
     net_target("10.0.0.1:22", "J1", id="a1b2c3d4", interval="30s")),
    ("net expect 空 → pong", "net",
     net_target("10.0.0.1:22", "J1", id="a1b2c3d4"),
     net_target("10.0.0.1:22", "J1", id="a1b2c3d4", expect="pong")),
]

for label, kind, a, b in VERSION_PAIRS:
    gen = toml_gen.generate_http_toml if kind == "http" else toml_gen.generate_net_toml
    t_a, t_b = gen([a]), gen([b])
    v_a, v_b = toml_gen.config_version([a]), toml_gen.config_version([b])
    check(f"TOML 变则 version 必变：{label}", t_a == t_b or v_a != v_b,
          f"TOML变了={t_a != t_b} version变了={v_a != v_b}")


# ── 6. 证书过期指标只对 https 目标有意义 ─────────────────────────
# categraf 仅在 https 目标上产出 cert_expire_timestamp
# （http_response.go: strings.HasPrefix(target, "https://") && resp.TLS != nil）。
# 所以 http:// 目标上的 cert_expire=False 不该下发 metrics_drop —— 那是一段
# 永远不起作用的配置，还会让"关掉证书采集"这件事看起来生效了其实没有。

CERT_CASES = [
    ("https + 不采集", "https://a.example.com", False, True),
    ("https + 采集", "https://a.example.com", True, False),
    ("https + 默认(None)", "https://a.example.com", None, False),
    ("http + 不采集", "http://a.example.com", False, False),
    ("http + 默认(None)", "http://a.example.com", None, False),
    ("无 scheme + 不采集", "a.example.com", False, False),
]
for label, url, ce, want_drop in CERT_CASES:
    txt = toml_gen.generate_http_toml(
        [http_target("", id="a1b2c3d4", url=url, cert_expire=ce)])
    got = "metrics_drop" in txt
    check(f"metrics_drop 只在 https 上生效：{label}", got == want_drop, f"metrics_drop={got}")


# ── 7. 响应内容匹配（expect_response_substring / expect_response_regular_expression） ──
# categraf 侧是"空值就不做该项检查"，所以只有非空才该写进 TOML。
_t = http_target("", id="a1b2c3d4", url="https://a.example.com", expect_substring="statusok")
_txt = toml_gen.generate_http_toml([_t])
check("响应包含写进 TOML", 'expect_response_substring = "statusok"' in _txt, _txt[-90:].strip())
_t2 = http_target("", id="a1b2c3d4", url="https://a.example.com", expect_regex=r'"code":\s*0')
_txt2 = toml_gen.generate_http_toml([_t2])
check("响应正则写进 TOML",
      'expect_response_regular_expression = "\\"code\\":\\\\s*0"' in _txt2, _txt2[-110:].strip())
_t3 = http_target("", id="a1b2c3d4", url="https://a.example.com")
_txt3 = toml_gen.generate_http_toml([_t3])
check("留空不写这两项", "expect_response_substring" not in _txt3
      and "expect_response_regular_expression" not in _txt3, _txt3[-90:].strip())
# 只有响应匹配不同 → 必须拆成两个 instance（否则其中一个的检查会被另一个覆盖）
_txt4 = toml_gen.generate_http_toml([
    http_target("", id="a1b2c3d4", url="https://a.example.com", expect_substring="aaa"),
    http_target("", id="b1b2c3d4", url="https://b.example.com", expect_substring="bbb"),
])
check("响应包含不同 → 拆成两个 instance", _txt4.count("[[instances]]") == 2,
      f"instances={_txt4.count('[[instances]]')}")

# ── 8. 响应正则校验（Go RE2 不支持的构造必须拦下） ──
# categraf 用 regexp.MustCompile：非法正则会 panic，整个 http_response 插件起不来
REGEX_OK = ["", r'"code":\s*0', r"^OK$", r"a{1,3}b", r"(?i)ok", r"a\+b"]
REGEX_BAD = [r"a(b", r"a[", r"*abc", r"(?P<n>x", r"\1", r"(?=x)", r"(?!x)", r"(?<=x)", r"(?>x)", r"a*+", r"a++"]
for rx in REGEX_OK:
    err = toml_gen.validate_regex(rx)
    check(f"正则通过：{rx!r}", err == "", err)
for rx in REGEX_BAD:
    err = toml_gen.validate_regex(rx)
    check(f"正则被拒：{rx!r}", bool(err), err or "竟然通过了")


ok = sum(1 for _, r in RESULTS if r)
print(f"\n{ok}/{len(RESULTS)} passed")
if ok != len(RESULTS):
    print("\n失败项:")
    for name, r in RESULTS:
        if not r:
            print(f"  - {name}")
sys.exit(0 if ok == len(RESULTS) else 1)
