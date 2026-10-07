# -*- coding: utf-8 -*-
"""前端 JS 逻辑的回归测试：把 static/index.html 里的函数抽出来，用 node 真跑。

跑法：python tests/test_frontend_payload.py（node 不可用时跳过）

为什么需要它：
  - `sitePayload` 的字段清单曾经手写，漏了 memo_content / probe_cert_expire /
    probe_protocol / probe_read_timeout / probe_send / probe_expect，导致「取消监控」
    一点就把这些配置洗掉。这里直接断言"输出必须覆盖 SiteIn 的全部字段"，
    以后再加字段忘了同步，测试立刻红
  - 草稿/脏检查快照 `_readSiteFields` 对 checkbox 用的是 .value（恒为 "on"），
    不能像文本框那样塞进 _SITE_FIELD_IDS；这里断言往返能把"采集证书过期时间"的
    勾选状态原样带回来，且切换它会让快照发生变化（否则关弹框不提示丢改动）
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import app  # noqa: E402

RESULTS = []
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))


def extract_function(src: str, name: str) -> str:
    """按函数名提取（括号/花括号配对），不依赖行号，改动代码后不会失效。

    注意别直接找第一个 "{"：`function f(s, overrides = {})` 的默认参数里就有花括号，
    那样会把函数截断成一行。必须先把参数括号配对完，再从后面找函数体。
    """
    i = src.find(f"function {name}(")
    if i < 0:
        return ""
    # 1) 配对参数括号（从函数名后的 "(" 开始）
    p = src.find("(", i)
    depth = 0
    body_start = -1
    for k in range(p, len(src)):
        if src[k] == "(":
            depth += 1
        elif src[k] == ")":
            depth -= 1
            if depth == 0:
                body_start = k + 1
                break
    if body_start < 0:
        return ""
    # 2) 从参数之后找函数体的 "{"
    j = src.find("{", body_start)
    if j < 0:
        return ""
    # 3) 配对函数体花括号（跳过字符串/模板串/正则里的括号）
    depth = 0
    quote = None
    k = j
    while k < len(src):
        c = src[k]
        if quote:
            if c == "\\":
                k += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'`":
            quote = c
        elif c == "/" and k + 1 < len(src) and src[k + 1] == "/":
            k = src.find("\n", k)
            if k < 0:
                break
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
        k += 1
    return ""


node = shutil.which("node")
if not node:
    print("SKIP  前端脚本测试（未找到 node）")
    sys.exit(0)

payload_js = extract_function(HTML, "sitePayload")
skip_const = re.search(r"const _PAYLOAD_SKIP = \[.*?\];", HTML, re.S)
field_ids = re.search(r"const _SITE_FIELD_IDS = \[.*?\];", HTML, re.S)
read_js = extract_function(HTML, "_readSiteFields")
apply_js = extract_function(HTML, "_applySiteFields")
for label, piece in (("sitePayload", payload_js), ("_PAYLOAD_SKIP", skip_const),
                     ("_SITE_FIELD_IDS", field_ids), ("_readSiteFields", read_js),
                     ("_applySiteFields", apply_js)):
    check(f"能从 index.html 抽到 {label}", bool(piece))

# SiteIn 的字段清单：sitePayload 的输出必须全部覆盖（这是这份测试的核心断言）
model_fields = sorted(app.SiteIn.model_fields.keys())

driver = r"""
const fs = require("fs");
const payload = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const out = [];
function ok(name, cond, detail) { out.push({ name, ok: !!cond, detail: detail || "" }); }

// ── 浏览器环境垫片：只实现被抽出来的这几个函数用到的那点 DOM ──
const ELS = {};
function mkEl(id) { return ELS[id] || (ELS[id] = { id, value: "", checked: false, style: {}, options: [], innerHTML: "" }); }
global.$ = mkEl;
let _timeoutOverride = null;
function getTimeoutValue(id) { return _timeoutOverride !== null ? _timeoutOverride : mkEl(id).value; }
function setTimeoutValue(id, v) { mkEl(id).value = v || ""; }
function setIntervalMode(id, v) { mkEl(id).value = v || ""; }
function updateProbeUrlOptions(v) { if (v !== undefined) mkEl("f_probe_url").value = v || ""; }
function toggleProbeSkip() {}
global.getTimeoutValue = getTimeoutValue; global.setTimeoutValue = setTimeoutValue;
global.setIntervalMode = setIntervalMode; global.updateProbeUrlOptions = updateProbeUrlOptions;
global.toggleProbeSkip = toggleProbeSkip;

eval(fs.readFileSync(process.argv[3], "utf8"));   // 抽出来的真实函数

// ── 1. sitePayload 必须覆盖 SiteIn 的全部字段 ──
const MODEL = payload.modelFields;
const full = {
  name: "站点", kind: "网站", category: "业务系统", public_url: "", private_url: "",
  domain: "a.example.com", connection: "", owner: "张三", env: "生产环境", remark: "备注",
  memo_content: "备忘录正文", monitor: true, probe_url: "https://a.example.com",
  probe_status_codes: "200|301", probe_timeout: "5s", probe_interval: "30s",
  probe_method: "POST", probe_headers: '["X-Token","abc"]', probe_body: '{"k":"v"}',
  probe_follow_redirects: null, probe_insecure_skip_verify: false,
  probe_tls_ca: "/etc/categraf/ca.pem", probe_cert_expire: false,
  probe_protocol: "udp", probe_read_timeout: "3s", probe_send: "PING", probe_expect: "PONG",
  id: "a1b2c3d4", created_at: "2026-01-01 00:00:00", updated_at: "2026-01-01 00:00:00",
};
const p = sitePayload(full);
const missing = MODEL.filter(k => !(k in p));
ok("sitePayload 覆盖 SiteIn 全部字段", missing.length === 0, "缺: " + missing.join(","));
ok("sitePayload 排除 id/created_at/updated_at",
   !("id" in p) && !("created_at" in p) && !("updated_at" in p));
ok("sitePayload 保留备忘录正文", p.memo_content === "备忘录正文", String(p.memo_content));
ok("sitePayload 保留证书采集开关 false", p.probe_cert_expire === false, String(p.probe_cert_expire));
ok("sitePayload 保留协议 udp", p.probe_protocol === "udp", String(p.probe_protocol));
ok("sitePayload 保留读超时", p.probe_read_timeout === "3s", String(p.probe_read_timeout));
ok("sitePayload 保留发送内容", p.probe_send === "PING", String(p.probe_send));
ok("sitePayload 保留期望响应", p.probe_expect === "PONG", String(p.probe_expect));
ok("sitePayload 保留自定义探测地址", p.probe_url === "https://a.example.com", String(p.probe_url));

// overrides 优先级
const off = sitePayload(full, { monitor: false });
ok("overrides 覆盖 monitor", off.monitor === false);
ok("overrides 不影响其他字段", off.memo_content === "备忘录正文" && off.probe_send === "PING");
ok("取消监控形状仍带全部字段", MODEL.every(k => k in off));

// 三态字段：None 必须保持 null（categraf 默认），不能被压成 false
[null, true, false].forEach(v => {
  const q = sitePayload({ ...full, probe_follow_redirects: v });
  ok("三态 probe_follow_redirects 保持 " + String(v), q.probe_follow_redirects === v,
     String(q.probe_follow_redirects));
});
// 缺字段的旧记录：不能因为 undefined 就把 body 之类写成 undefined
const thin = sitePayload({ name: "只有名字", env: "生产环境", monitor: false });
ok("稀疏记录不产生 undefined 值", !Object.values(thin).some(v => v === undefined),
   JSON.stringify(Object.entries(thin).filter(([, v]) => v === undefined).map(([k]) => k)));

// ── 2. 草稿/脏检查快照：checkbox 状态必须能往返 ──
function setForm(certExpire) {
  mkEl("f_probe_cert_expire").checked = certExpire;
  mkEl("f_monitor").checked = true;
  mkEl("f_probe_skip_verify").checked = false;
  mkEl("f_kind").value = "网站";
  mkEl("f_environment").value = "生产环境";
  mkEl("f_name").value = "草稿站点";
  mkEl("f_probe_interval").value = "30s";
  _timeoutOverride = "5s";
}
setForm(true);
const snapChecked = _readSiteFields();
ok("快照记录勾选状态为 true", snapChecked.f_probe_cert_expire === true,
   JSON.stringify(snapChecked.f_probe_cert_expire));
setForm(false);
const snapUnchecked = _readSiteFields();
ok("快照记录勾选状态为 false", snapUnchecked.f_probe_cert_expire === false,
   JSON.stringify(snapUnchecked.f_probe_cert_expire));
ok("切换该勾选框会让快照变化（关闭弹框会提示未保存）",
   JSON.stringify(snapChecked) !== JSON.stringify(snapUnchecked));
ok("快照不是 checkbox 的 .value 恒真值", snapChecked.f_probe_cert_expire !== "on",
   JSON.stringify(snapChecked.f_probe_cert_expire));

// 回填：draft.f_probe_cert_expire=false → 取消勾选；缺省 → 按"采集"勾上
mkEl("f_probe_cert_expire").checked = true;
_applySiteFields({ ...snapUnchecked });
ok("草稿回填能把勾选取消掉", mkEl("f_probe_cert_expire").checked === false,
   String(mkEl("f_probe_cert_expire").checked));
ok("草稿往返后其他字段也在", mkEl("f_name").value === "草稿站点" && mkEl("f_monitor").checked === true,
   mkEl("f_name").value + "/" + mkEl("f_monitor").checked);
_applySiteFields({});
ok("清空草稿/新建时默认勾选（采集）", mkEl("f_probe_cert_expire").checked === true,
   String(mkEl("f_probe_cert_expire").checked));
_applySiteFields({ f_probe_cert_expire: true });
ok("草稿回填能勾上", mkEl("f_probe_cert_expire").checked === true);

process.stdout.write(JSON.stringify(out));
"""

js_parts = "\n".join(filter(None, [
    skip_const.group(0) if skip_const else "",
    field_ids.group(0) if field_ids else "",
    payload_js, read_js, apply_js,
]))

with tempfile.TemporaryDirectory() as td:
    jsf = Path(td) / "front.js"
    cf = Path(td) / "payload.json"
    pf = Path(td) / "driver.cjs"        # .cjs：避免被上层 package.json 的 type=module 影响
    jsf.write_text(js_parts, encoding="utf-8")
    cf.write_text(json.dumps({"modelFields": model_fields}, ensure_ascii=False), encoding="utf-8")
    pf.write_text(driver, encoding="utf-8")
    proc = subprocess.run([node, str(pf), str(cf), str(jsf)],
                          capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0 or not proc.stdout.strip():
        check("node 执行前端脚本", False, (proc.stderr or "").strip()[:300])
    else:
        for r in json.loads(proc.stdout):
            check(r["name"], r["ok"], r.get("detail", ""))

passed = sum(1 for _, o in RESULTS if o)
print(f"\n{passed}/{len(RESULTS)} passed")
if passed != len(RESULTS):
    print("失败项:")
    for n, o in RESULTS:
        if not o:
            print("  -", n)
sys.exit(0 if passed == len(RESULTS) else 1)
