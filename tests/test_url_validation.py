# -*- coding: utf-8 -*-
"""URL 校验回归测试：后端 app.py 与前端 static/index.html 必须同一口径。

直接跑：python tests/test_url_validation.py（无第三方依赖；node 可用时才跑前端部分）

覆盖三类曾经漏网的值——它们进了库之后就变成永远连不通的拨测目标，
运维分不清是配置错还是网络不通：

1. 地址查重把 http:// 和 https:// 当成两个地址。
   后端 _norm_url 去 scheme，前端 normUrl 却补 scheme，两边口径不一致：
   同一台机器两条记录各填一个就都通过了。http://x:80 与 http://x、
   https://x:443 与 https://x 也应算同一个（默认端口）。

2. 域名框里填 IP 不做校验。"每段 1-3 位数字"的正则放过 999.1.1.1 / 256.0.0.1；
   1.2.3.4.5 / 192.168.1 既不是合法域名也不是合法 IP，却两边都能过。
   http://a:1:2 更隐蔽：urlparse 把它当成 host=a port=2，悄悄丢掉一段，
   拨测连的端口和用户填的不是一个。

3. IP 填错字段。域名框混进 IP 会丢掉"内网/公网"信息；公网框填内网 IP、
   内网框填公网 IP 也一样——两个字段各生成一个拨测 job 指向同一台机器，
   界面上一眼看不出为什么重复。

最后一节把两端的判断结果对表比较——这次改动的起因就是两边分叉。
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import app  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))


# ---- 表 1：(字段, 值, 应否通过, 错误信息关键字) ----------------------
# 格式校验在归属校验之前：999.1.1.1 先报"IP 地址不合法"，不报"域名不能填 IP"
CASES = [
    # 域名框：只放域名
    ("domain", "gitlab.company.com", True, ""),
    ("domain", "a-b.com", True, ""),
    ("domain", "localhost", True, ""),
    ("domain", "xn--nxasmq6b.example.com", True, ""),
    ("domain", "http://x.example.com/nwms", True, ""),
    ("domain", "HTTP://X.EXAMPLE.COM:8080", True, ""),
    ("domain", "47.100.200.114", False, "域名不能填 IP"),
    ("domain", "https://10.0.0.1:8080", False, "域名不能填 IP"),
    ("domain", "999.1.1.1", False, "IP 地址不合法"),
    ("domain", "256.0.0.1", False, "IP 地址不合法"),
    ("domain", "1.2.3.4.5", False, "IP 地址不合法"),
    ("domain", "192.168.1", False, "IP 地址不合法"),
    ("domain", "123", False, "IP 地址不合法"),
    ("domain", "192.168.001.1", False, "IP 地址不合法"),
    ("domain", "中文.example.com", False, "非 ASCII"),
    ("domain", "a..b.com", False, "空标签"),
    ("domain", "-bad.com", False, "连字符"),
    ("domain", "bad-.com", False, "连字符"),
    ("domain", ".", False, "IP 地址不合法"),
    ("domain", "-", False, "占位符"),
    # 公网地址：IP 必须是公网；域名放行（校验时不发 DNS，判不了归属）
    ("public_url", "180.235.66.237:7000", True, ""),
    ("public_url", "gitlab.company.com", True, ""),
    ("public_url", "https://47.100.200.114:3208", True, ""),
    ("public_url", "[2001:db8::1]:8080", True, ""),
    ("public_url", "10.0.0.1:8080", False, "公网地址不能填内网 IP"),
    ("public_url", "172.20.1.5", False, "内网 IP"),
    ("public_url", "192.168.1.1", False, "内网 IP"),
    ("public_url", "127.0.0.1", False, "内网 IP"),
    ("public_url", "169.254.10.10", False, "内网 IP"),
    ("public_url", "172.31.255.255", False, "内网 IP"),
    ("public_url", "[fe80::1]:8080", False, "内网 IP"),
    ("public_url", "[::ffff:10.0.0.1]:80", False, "内网 IP"),
    ("public_url", "http://x:320802", False, "端口不合法"),
    ("public_url", "http://x:abc", False, "端口不合法"),
    ("public_url", "http://a:1:2", False, "端口不合法"),
    # 内网地址：IP 必须是内网
    ("private_url", "192.168.1.100:6379", True, ""),
    ("private_url", "10.0.1.20:6379", True, ""),
    ("private_url", "172.31.255.255:80", True, ""),
    ("private_url", "[fc00::1]:80", True, ""),
    ("private_url", "redis.internal.local", True, ""),
    ("private_url", "180.235.66.237:8080", False, "内网地址不能填公网 IP"),
    ("private_url", "172.15.0.1", False, "公网 IP"),
    ("private_url", "172.32.0.1", False, "公网 IP"),
    ("private_url", "[2001:db8::1]:8080", False, "公网 IP"),
    ("private_url", "[::ffff:180.235.66.237]:80", False, "公网 IP"),
    ("private_url", "N/A", False, "占位符"),
    # 格式问题（与字段无关，换个字段再验证一遍）
    ("private_url", "http://::1:8080", False, "方括号"),
    ("private_url", "http://[::1", False, "括号"),
    ("private_url", "http://[1:::2]:8080", False, "括号"),
    ("private_url", "http://[1:2:3:4:5:6:7:8:9]", False, "括号"),
]

# ---- 表 2：查重归一化，组内应当相等 ----------------------------------
NORM_GROUPS = [
    ["http://47.100.200.114:3208", "https://47.100.200.114:3208"],
    ["http://47.100.200.114:3208", "47.100.200.114:3208"],
    ["http://47.100.200.114:3208", "https://47.100.200.114:3208/"],
    ["http://x:80", "http://x", "https://x:443"],
    ["HTTP://X.EXAMPLE.COM:8080", "http://x.example.com:8080/"],
]
# 不该被归成同一个（端口不同）
NORM_DIFFERENT = [
    ["http://x:80", "https://x:80"],
    ["http://x:8080", "http://x:8081"],
]


def backend_err(v, field="domain"):
    """SiteIn 指定字段的错误信息；合法返回空串"""
    try:
        app.SiteIn(name="t", env="生产环境", **{field: v})
        return ""
    except Exception as e:
        errs = getattr(e, "errors", [])
        if callable(errs):
            errs = errs()
        for err in errs or []:
            msg = err.get("msg", "")
            if msg.startswith("Value error, "):
                return msg[len("Value error, "):]
        return str(e)


print("== 字段校验 ==")
for field, v, expect_ok, kw in CASES:
    err = backend_err(v, field)
    if expect_ok:
        check(f"{field} 接受 {v!r}", err == "", err)
    else:
        check(f"{field} 拒绝 {v!r}", bool(err) and kw in err, err or "（竟然通过了）")

print("\n== 查重归一化 ==")
for group in NORM_GROUPS:
    norms = {app._norm_url(v) for v in group}
    check(f"同组归一 {group}", len(norms) == 1, ",".join(sorted(norms)))
for a, b in NORM_DIFFERENT:
    check(f"不同组不归一 {a!r} vs {b!r}", app._norm_url(a) != app._norm_url(b),
          f"{app._norm_url(a)!r} / {app._norm_url(b)!r}")

print("\n== http/https 同 host:port 算重复 ==")
# 公网框是公网 IP、内网框是内网 IP，两个框不可能填同一个 IP，
# 所以跨字段的重复只能靠域名触发，这里用域名
try:
    app.SiteIn(name="t", env="生产环境",
               public_url="http://svc.example.com:3208",
               private_url="https://svc.example.com:3208")
    check("同记录 公网+内网 http/https 报重复", False, "（竟然通过了）")
except Exception as e:
    check("同记录 公网+内网 http/https 报重复", "重复" in str(e), str(e)[:100])

idx = app._build_dup_index([
    {"id": "a", "name": "站点A", "env": "生产环境", "public_url": "http://47.100.200.114:3208",
     "connection": ""},
])
dup = app._find_duplicate([], {
    "name": "站点B", "env": "测试环境", "private_url": "https://47.100.200.114:3208",
    "connection": "",
}, index=idx)
check("跨记录 http/https 同 host:port 报重复", bool(dup), dup)
dup2 = app._find_duplicate([], {
    "name": "站点C", "env": "生产环境", "public_url": "http://x:80", "connection": "",
}, index=app._build_dup_index([
    {"id": "b", "name": "站点C2", "env": "生产环境", "private_url": "https://x:443", "connection": ""},
]))
check("跨记录 默认端口 80/443 报重复", bool(dup2), dup2)


# ==================== 前端 vs 后端 对表 ====================
print("\n== 前端/后端一致性 ==")

JS_BEGIN = "const URL_PLACEHOLDERS"
JS_END = "// 域名/公网/内网不能指向同一个地址"

_driver = """
const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));
const cases = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const out = cases.map(({ v, field }) => {
  let err = "", n = "";
  try { err = urlHostError(v, field) || ""; } catch (e) { err = "THROW:" + e.message; }
  try { n = normUrl(v); } catch (e) { n = "THROW:" + e.message; }
  return { v, field, err, n };
});
process.stdout.write(JSON.stringify(out));
"""

html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
b, e = html.find(JS_BEGIN), html.find(JS_END)
node = shutil.which("node")
if not node or b < 0 or e < 0:
    print(f"SKIP  前端校验（node={bool(node)}, 代码块定位={b >= 0 and e >= 0}）")
else:
    seen = set()
    cases = [{"v": v, "field": f} for f, v, _ok, _kw in CASES]
    cases = [c for c in cases if not ((c["field"], c["v"]) in seen
                                      or seen.add((c["field"], c["v"])))]
    norm_only = {v for g in NORM_GROUPS for v in g} | {v for pair in NORM_DIFFERENT for v in pair}
    have = {(c["field"], c["v"]) for c in cases}
    cases += [{"v": v, "field": "public_url"} for v in sorted(norm_only)
              if ("public_url", v) not in have]
    with tempfile.TemporaryDirectory() as td:
        jsf = Path(td) / "js.js"
        cf = Path(td) / "cases.json"
        # .cjs：用户目录上可能有 "type": "module" 的 package.json，.js 会被当成 ESM，
        # 那样 require 直接 ReferenceError，前端校验就静默跳过了
        driver = Path(td) / "driver.cjs"
        jsf.write_text(html[b:e], encoding="utf-8")
        cf.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
        driver.write_text(_driver, encoding="utf-8")
        proc = subprocess.run([node, str(driver), str(jsf), str(cf)],
                              capture_output=True, text=True, encoding="utf-8")
        js_out = {(r["v"], r["field"]): r for r in json.loads(proc.stdout)} if proc.returncode == 0 else {}
        if not js_out:
            print(f"FAIL  前端脚本执行失败: {proc.stderr.strip()[:200]}")
            RESULTS.append(("前端脚本执行", False))
        else:
            mism = []
            for c in cases:
                key = (c["v"], c["field"])
                j = js_out.get(key, {})
                be_bad, js_bad = bool(backend_err(c["v"], c["field"])), bool(j.get("err"))
                if be_bad != js_bad:
                    mism.append(f"{c['field']}={c['v']!r}: 后端{'拒' if be_bad else '收'}"
                                f"/前端{'拒' if js_bad else '收'}({j.get('err', '')[:34]})")
                # 归一化只对能存进库的值有意义；非法值两边都不会存，
                # 解析不出主机的退化成字面比较，写法可以不一样
                if not be_bad and c["v"] in norm_only and j.get("n") != app._norm_url(c["v"]):
                    mism.append(f"{c['v']!r} 归一化不同: 前端 {j.get('n')!r} / 后端 {app._norm_url(c['v'])!r}")
            check("前后端判断一致", not mism, "; ".join(mism[:4]) or f"全部一致（{len(cases)} 例）")
            a, z = ("http://47.100.200.114:3208", "public_url"), ("https://47.100.200.114:3208", "public_url")
            check("前端 http/https 归一化一致",
                  js_out[a]["n"] == js_out[z]["n"]
                  == app._norm_url(a[0]) == app._norm_url(z[0]),
                  f"{js_out[a]['n']!r} / {js_out[z]['n']!r}")


# ---- 汇总 ----
print()
failed = [n for n, ok in RESULTS if not ok]
total = len(RESULTS)
print(f"{total - len(failed)}/{total} 通过")
if failed:
    print("失败项：")
    for n in failed:
        print("  -", n)
    sys.exit(1)
