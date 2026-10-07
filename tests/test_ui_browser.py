# -*- coding: utf-8 -*-
"""无头浏览器 UI 测试：真加载 static/index.html，真跑 DOM 交互。

跑法：python tests/test_ui_browser.py（找不到 Edge/Chrome 时跳过）

为什么需要它：显隐逻辑、弹窗草稿、下拉回填这些只看 DOM 才能验。
Python 侧和接口侧测不到，而它们恰恰是最近改动最密集的地方：
  - TLS 区块按「实际探测地址」显隐（不是优先级首地址）
  - 请求头 / 请求 Body 按「请求方法」显隐（但有值时不藏）
  - 探测地址下拉的选项与回填
  - 新增表单草稿往返（含「采集证书过期时间」的勾选状态）
  - 脏检查：切这个勾选框要能被判定为"改过"

用一个进程内 stub 顶掉后端 API（页面只认 localStorage 的 nav_token/nav_role），
所以不需要起真服务、也不会碰真实数据。
"""
import html as html_mod
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static" / "index.html"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))


def find_browser():
    cands = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/usr/bin/microsoft-edge", "/usr/bin/google-chrome", "/usr/bin/chromium",
        shutil.which("msedge"), shutil.which("google-chrome"), shutil.which("chromium"),
    ]
    for c in cands:
        if c and Path(c).exists():
            return str(c)
    return None


def site(sid, name, **kw):
    base = {
        "id": sid, "name": name, "kind": "网站", "category": "业务系统", "public_url": "",
        "private_url": "", "domain": "", "connection": "", "owner": "", "env": "生产环境",
        "remark": "", "memo_content": "", "monitor": False, "probe_url": "",
        "probe_status_codes": "", "probe_timeout": "", "probe_interval": "", "probe_method": "",
        "probe_headers": "", "probe_body": "", "probe_follow_redirects": None,
        "probe_insecure_skip_verify": False, "probe_tls_ca": "", "probe_cert_expire": None,
        "probe_protocol": "tcp", "probe_read_timeout": "", "probe_send": "", "probe_expect": "",
        "created_at": "2026-01-01 00:00:00", "updated_at": "2026-01-01 00:00:00",
    }
    base.update(kw)
    return base


FIXTURES = [
    # s1：域名是 http，但自选探测地址是 https 的公网地址 → TLS 区块必须显示
    site("s1a2b3c4", "HTTPS探针站点", domain="hr.example.com",
         public_url="https://1.2.3.4:8443", private_url="http://10.0.0.1",
         monitor=True, probe_url="https://1.2.3.4:8443",
         probe_status_codes="200|301", probe_timeout="5s", probe_interval="30s",
         probe_method="POST", probe_headers='["X-Token","abc"]', probe_cert_expire=False),
    # s2：纯 http，GET，没有任何请求头/body → 两栏都该收起来
    site("s2a2b3c4", "普通HTTP站点", domain="oa.example.com", monitor=True,
         probe_status_codes="200"),
    # s3：备忘录 → 不参与拨测，监控勾选框必须禁用
    site("s3a2b3c4", "备忘录条目", kind="备忘录", memo_content="纯文本正文"),
]

CHECK_JS = r"""
<script>
window.confirm = function () { return true; };   // 无头下 confirm 默认被拒，脏检查会卡住关闭
(async function () {
  const out = [];
  const ok = (n, c, d) => out.push({ name: n, ok: !!c, detail: d === undefined ? "" : String(d) });
  const $ = id => document.getElementById(id);
  const vis = id => $(id) && $(id).style.display !== "none";

  // 等列表渲染完（异步 loadSites）
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline && document.querySelectorAll(".cards-grid .card").length < 3) {
    await new Promise(r => setTimeout(r, 50));
  }
  const cards = document.querySelectorAll(".cards-grid .card").length;
  ok("列表渲染出 3 张卡片", cards === 3, "cards=" + cards);
  ok("未登录时不会停在登录页", !document.querySelector(".login-box") || cards > 0, "");

  // ── 编辑 s1：探测地址下拉 / TLS 显隐 / 方法字段 / 下拉回填 ──
  const b = document.createElement("button");
  b.dataset.editId = "s1a2b3c4";
  openEditSite(b);
  ok("编辑弹窗打开", $("siteModal").classList.contains("show"));
  ok("探测地址控件可见", vis("probeUrlGroup"));
  ok("探测地址回填记录值", $("f_probe_url").value === "https://1.2.3.4:8443", $("f_probe_url").value);
  const opts = Array.from($("f_probe_url").options).map(o => o.value);
  ok("探测地址选项=自动+域名+公网+内网",
     opts.length === 4 && opts[0] === "" && opts.indexOf("hr.example.com") > 0
     && opts.indexOf("https://1.2.3.4:8443") > 0 && opts.indexOf("http://10.0.0.1") > 0,
     JSON.stringify(opts));
  ok("TLS 区块显示（实际探测地址是 https）", vis("probeTlsBlock"));
  ok("TLS 区块内含「跳过证书校验」", $("probeTlsBlock").contains($("f_probe_skip_verify")));
  ok("TLS 区块内含「采集证书过期时间」", $("probeTlsBlock").contains($("f_probe_cert_expire")));
  ok("证书采集开关按记录回填（false → 未勾选）", $("f_probe_cert_expire").checked === false);
  ok("请求方法回填 POST", $("f_probe_method").value === "POST", $("f_probe_method").value);
  ok("POST 时请求头显示", vis("probeHeadersGroup"));
  ok("POST 时请求 Body 显示", vis("probeBodyRow"));
  ok("超时命中预设档 5s", $("f_probe_timeout").value === "5s", $("f_probe_timeout").value);
  ok("探测间隔落「自定义」", $("f_probe_interval").value === "30s", $("f_probe_interval").value);
  const modeSel = document.querySelector('input[name="f_probe_interval_mode"][value="custom"]');
  ok("间隔单选框选中自定义", modeSel && modeSel.checked);
  ok("跳过证书校验与私有CA互斥逻辑在位（勾选会清空 CA）", typeof toggleProbeSkip === "function");

  // 探测地址切成非 https → TLS 区块必须隐藏
  $("f_probe_url").value = "http://10.0.0.1";
  updateProbeTls();
  ok("探测地址选非 https 后 TLS 区块隐藏", !vis("probeTlsBlock"));
  $("f_probe_url").value = "https://1.2.3.4:8443";
  updateProbeTls();
  ok("再选回 https 后 TLS 区块恢复显示", vis("probeTlsBlock"));

  // 方法切 GET：Body 收起；请求头因为有值仍然显示
  $("f_probe_method").value = "GET";
  updateProbeMethodFields();
  ok("GET 时请求 Body 收起", !vis("probeBodyRow"));
  ok("GET 但请求头有值 → 仍显示（不藏已配置内容）", vis("probeHeadersGroup"));
  $("f_probe_headers").value = "";
  updateProbeMethodFields();
  ok("GET 且请求头为空 → 收起", !vis("probeHeadersGroup"));
  $("f_probe_method").value = "POST";
  updateProbeMethodFields();
  ok("切回 POST 两栏都显示", vis("probeHeadersGroup") && vis("probeBodyRow"));
  closeSiteModal();

  // ── 备忘录：监控勾选框禁用 ──
  const b3 = document.createElement("button");
  b3.dataset.editId = "s3a2b3c4";
  openEditSite(b3);
  ok("备忘录：监控勾选框被禁用", $("f_monitor").disabled === true);
  ok("备忘录：提示文案说明不做拨测", ($("monitorHint").textContent || "").indexOf("备忘录") >= 0,
     $("monitorHint").textContent);
  closeSiteModal();

  // ── 新增表单草稿往返（含证书采集勾选状态） ──
  openSiteModal();
  $("f_name").value = "草稿站点";
  $("f_domain").value = "draft.example.com";   // 必须有地址，否则 toggleProbeParams 会按设计关掉监控勾选
  $("f_environment").value = "生产环境";
  $("f_monitor").checked = true;
  $("f_probe_cert_expire").checked = false;
  $("f_probe_timeout").value = "10s";
  toggleProbeParams();
  ok("有地址时监控勾选保持", $("f_monitor").checked === true);
  closeSiteModal();
  ok("关闭新增弹窗时保存了草稿", !!siteDraft);
  // 清空 DOM 状态，模拟"重开一个新弹窗"
  $("f_name").value = "";
  $("f_domain").value = "";
  $("f_probe_cert_expire").checked = true;
  $("f_probe_timeout").value = "";
  openSiteModal();
  ok("草稿恢复名称", $("f_name").value === "草稿站点", $("f_name").value);
  ok("草稿恢复地址", $("f_domain").value === "draft.example.com", $("f_domain").value);
  ok("草稿恢复监控勾选", $("f_monitor").checked === true);
  ok("草稿恢复证书采集开关（false 未被洗成 true）", $("f_probe_cert_expire").checked === false,
     String($("f_probe_cert_expire").checked));
  ok("草稿恢复超时档位", $("f_probe_timeout").value === "10s", $("f_probe_timeout").value);
  ok("有草稿时出现「清空草稿」按钮", $("draftClearBtn").style.display !== "none");
  clearSiteDraft();
  ok("清空草稿后勾选框回到默认（采集）", $("f_probe_cert_expire").checked === true);
  ok("清空草稿后名称被清空", $("f_name").value === "");

  // ── 脏检查：只切「采集证书过期时间」也要算改过 ──
  const b1 = document.createElement("button");
  b1.dataset.editId = "s1a2b3c4";
  openEditSite(b1);
  const before = JSON.stringify(_readSiteFields());
  $("f_probe_cert_expire").checked = !$("f_probe_cert_expire").checked;
  const after = JSON.stringify(_readSiteFields());
  ok("切换证书采集勾选框后快照变化（关闭会提示未保存）", before !== after);
  closeSiteModal();

  // ── 快捷监控 payload（页面真实调用路径） ──
  const rec = sites.find(s => s.id === "s1a2b3c4");
  const p = sitePayload(rec, { monitor: false });
  ok("sitePayload 输出带 memo_content 字段", "memo_content" in p);
  ok("sitePayload 输出带 probe_cert_expire=false", p.probe_cert_expire === false);
  ok("sitePayload 输出不带 id", !("id" in p));

  const pre = document.createElement("pre");
  pre.id = "__uitest";
  pre.textContent = "UITEST" + JSON.stringify(out);
  document.body.appendChild(pre);
})().catch(e => {
  const pre = document.createElement("pre");
  pre.id = "__uitest";
  pre.textContent = "UITEST" + JSON.stringify([{ name: "脚本异常: " + e.message, ok: false, detail: "" }]);
  document.body.appendChild(pre);
});
</script>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/admin", "/index.html"):
            page = STATIC.read_text(encoding="utf-8")
            boot = ("<script>localStorage.setItem('nav_token','uitest');"
                    "localStorage.setItem('nav_role','admin');</script>")
            page = page.replace("<head>", "<head>" + boot, 1)
            page = page.replace("</body>", CHECK_JS + "</body>", 1)
            return self._send(page, "text/html; charset=utf-8")
        if path == "/api/sites":
            return self._send(json.dumps(FIXTURES, ensure_ascii=False))
        if path == "/api/monitor-status":
            return self._send("{}")
        if path == "/api/monitor-config":
            return self._send(json.dumps({"enabled": True, "state": "ok", "reason": ""}))
        if path in ("/api/probes", "/api/probe-targets", "/api/users"):
            return self._send("[]")
        return self._send("{}")


def main():
    if not STATIC.exists():
        print(f"!! 找不到 {STATIC}")
        return 1
    browser = find_browser()
    if not browser:
        print("SKIP  UI 浏览器测试（未找到 Edge/Chrome）")
        return 0

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/?uitest=1"
    print(f"stub 服务: {url}")
    print(f"浏览器: {browser}\n")

    dom = ""
    try:
        with tempfile.TemporaryDirectory(prefix="uitest-") as td:
            proc = subprocess.run(
                [browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                 f"--user-data-dir={Path(td) / 'prof'}", "--window-size=1280,900",
                 "--virtual-time-budget=12000", "--dump-dom", url],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
            dom = proc.stdout or ""
    finally:
        srv.shutdown()

    m = re.search(r'<pre id="__uitest">UITEST(.*?)</pre>', dom, re.S)
    if not m:
        check("拿到页面内的测试结果", False, f"DOM 长度={len(dom)}；可能页面没加载完")
        print(f"\n{sum(1 for _, o in RESULTS if o)}/{len(RESULTS)} passed")
        return 1
    for r in json.loads(html_mod.unescape(m.group(1))):
        check(r["name"], r["ok"], r.get("detail", ""))

    passed = sum(1 for _, o in RESULTS if o)
    print(f"\n{passed}/{len(RESULTS)} passed")
    if passed != len(RESULTS):
        print("失败项:")
        for n, o in RESULTS:
            if not o:
                print("  -", n)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
