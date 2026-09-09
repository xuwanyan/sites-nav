"""新功能回归：kind / connection 字段、跨环境分组、按环境的备注渲染"""
import json, sys, time, urllib.request, urllib.error, base64, subprocess, os

BASE = "http://127.0.0.1:8000"
PASS = FAIL = 0
def check(name, ok, detail=""):
    global PASS, FAIL
    tag = "PASS" if ok else "FAIL"
    if ok: PASS += 1
    else: FAIL += 1
    print(f"[{tag}] {name}" + (f" | {detail}" if detail and not ok else ""))

def req(method, path, body=None, token=None):
    url = BASE + path
    headers = {"Content-Type": "application/json"}
    if token: headers["Authorization"] = "Bearer " + token
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(r, timeout=15)
        return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read().decode("utf-8"))
        except Exception: return e.code, {}

# 登录
password = os.environ.get("SITES_NAV_PASS", "REDACTED")
code, data = req("POST", "/api/login", {"password": password})
check("登录", code == 200, f"got {code}")
tok = data.get("token", "")

TS = int(time.time())
UNIQ = f"E2E新{TS}"
created = []
def add(site, expect=200):
    code, data = req("POST", "/api/sites", site, tok)
    if code == expect:
        if code == 200: created.append(data["id"])
        return data
    check(f"POST {site.get('name','?')}", False, f"expect {expect}, got {code}, {data.get('detail','')}")
    return data
def del_(id_):
    if id_:
        req("DELETE", f"/api/sites/{id_}", token=tok)

def finish():
    for i in created: del_(i)

try:
    # ── 场景 1：kind 默认值 = "网站" ──
    add({"name": f"{UNIQ}-默认", "env": "生产环境", "domain": f"a{TS}.example.com"})
    code, lst = req("GET", "/api/sites")
    rec = [s for s in lst if s["name"] == f"{UNIQ}-默认"][0]
    check("默认 kind=网站", rec.get("kind") == "网站", f"got {rec.get('kind')!r}")
    check("默认 connection 为空", rec.get("connection") == "", f"got {rec.get('connection')!r}")

    # ── 场景 2：kind 自定义文本 + connection ──
    add({"name": f"{UNIQ}-nginx", "env": "生产环境", "kind": "Nginx-自定义", "connection": "nginx://10.0.5.1"})
    code, lst = req("GET", "/api/sites")
    rec = [s for s in lst if s["name"] == f"{UNIQ}-nginx"][0]
    check("kind 自定义保存", rec.get("kind") == "Nginx-自定义", f"got {rec.get('kind')!r}")
    check("connection 保存", rec.get("connection") == "nginx://10.0.5.1", f"got {rec.get('connection')!r}")

    # ── 场景 3：跨环境分组 —— 生产+测试同名 ──
    add({"name": UNIQ, "env": "生产环境", "domain": f"prod{TS}.example.com", "remark": "生产备注"})
    add({"name": UNIQ, "env": "测试环境", "domain": f"test{TS}.example.com", "remark": "测试备注"})
    code, lst = req("GET", "/api/sites")
    grp = [s for s in lst if s["name"] == UNIQ]
    check("跨环境分组保存两条", len(grp) == 2, f"got {len(grp)}")
    check("生产备注保留", grp[0].get("remark") == "生产备注", f"got {grp[0].get('remark')!r}")
    check("测试备注保留", grp[1].get("remark") == "测试备注", f"got {grp[1].get('remark')!r}")

    # ── 场景 4：同名同环境拒绝 ──
    d4 = add({"name": UNIQ, "env": "生产环境", "domain": f"dup{TS}.example.com"}, expect=409)
    check("同名同环境 409", True)

    # ── 场景 5：URL 唯一性拒绝 ──
    d5 = add({"name": f"{UNIQ}-dup2", "env": "生产环境", "domain": f"prod{TS}.example.com"}, expect=409)
    check("URL 重复 409", True)

    # ── 场景 6：connection 唯一性拒绝 ──
    d6 = add({"name": f"{UNIQ}-dup3", "env": "生产环境", "connection": "nginx://10.0.5.1"}, expect=409)
    check("connection 重复 409", True)

    # ── 场景 7：只有 connection 无 URL ──
    add({"name": f"{UNIQ}-redis", "env": "生产环境", "kind": "缓存", "connection": f"redis://10.0.9.{TS}:6379"})
    code, lst = req("GET", "/api/sites")
    rec = [s for s in lst if s["name"] == f"{UNIQ}-redis"][0]
    check("纯 connection 保存", rec.get("domain") == "" and rec.get("connection").startswith("redis://"))

    # ── 场景 8：URL + connection 都填 ──
    add({"name": f"{UNIQ}-harbor", "env": "生产环境", "kind": "存储", "domain": f"harbor{TS}.example.com", "connection": f"https://harbor{TS}.example.com"})
    code, lst = req("GET", "/api/sites")
    rec = [s for s in lst if s["name"] == f"{UNIQ}-harbor"][0]
    check("URL+connection 都保存", rec.get("domain") == f"harbor{TS}.example.com" and rec.get("connection").startswith("https://"))

    # ── 场景 9：纯元数据（无 URL 无 connection）应被后端拒绝 ──
    # _validate_site_payload 强制至少填一个 URL 或 connection
    add({"name": f"{UNIQ}-meta", "env": "生产环境", "kind": "K8s"}, expect=400)
    check("纯元数据被拒绝（至少一个 URL 或 connection）", True)

    # ── 场景 10：编辑保存 kind + connection ──
    target = [s for s in req("GET","/api/sites")[1] if s["name"] == f"{UNIQ}-nginx"][0]
    upd = {**target, "kind": "Nginx-改名", "connection": "nginx://10.0.5.2"}
    code, data = req("PUT", f"/api/sites/{target['id']}", upd, tok)
    check("编辑 kind+connection", code == 200)
    code, lst = req("GET", "/api/sites")
    rec = [s for s in lst if s["id"] == target["id"]][0]
    check("编辑后 kind 更新", rec["kind"] == "Nginx-改名", f"got {rec['kind']!r}")
    check("编辑后 connection 更新", rec["connection"] == "nginx://10.0.5.2", f"got {rec['connection']!r}")

    # ── 场景 11：搜索覆盖 connection 字段 ──
    code, lst = req("GET", "/api/sites")
    # 前端搜索在前端做，后端搜索接口不一定有 —— 跳过

    # ── 场景 12：CSV 导入 kind + connection ──
    # 11 列：系统名称,资源类型,分类,域名,公网地址,内网地址,连接串,负责人,环境标识,备注,拨测监控
    # 域名/公网/内网（第 4-6 列）留空，连接串（第 7 列）填 ldap://...
    csv = (
        "系统名称,资源类型,分类,域名,公网地址,内网地址,连接串,负责人,环境标识,备注,拨测监控\n"
        f"{UNIQ}-csv,LDAP,安全,,,,ldap://ldap{TS}.example.com:389,ops,生产环境,导入测试,false\n"
    )
    code, data = req("POST", "/api/sites/import", {"format": "csv", "content": csv}, tok)
    check("CSV 导入 kind+connection", code == 200 and data.get("added") == 1, f"got {code}, {data}")
    code, lst = req("GET", "/api/sites")
    rec = [s for s in lst if s["name"] == f"{UNIQ}-csv"][0]
    check("CSV 导入 kind", rec.get("kind") == "LDAP", f"got {rec.get('kind')!r}")
    check("CSV 导入 connection", rec.get("connection").startswith("ldap://"), f"got {rec.get('connection')!r}")

    # ── 场景 13：CSV 导出包含 kind/connection 列 ──
    req_r = urllib.request.Request(BASE + "/api/sites/export?format=csv", headers={"Authorization": f"Bearer {tok}"})
    csv_out = urllib.request.urlopen(req_r, timeout=15).read().decode("utf-8-sig")
    header = csv_out.splitlines()[0]
    check("CSV 导出含 资源类型", "资源类型" in header, f"header: {header}")
    check("CSV 导出含 连接串", "连接串" in header, f"header: {header}")

    # ── 场景 14：老数据兼容（缺 kind 字段）──
    # 直接改磁盘数据来测试 —— 先备份再改再恢复
    data_file = "data/sites.json"
    if os.path.exists(data_file):
        backup = open(data_file, encoding="utf-8").read()
        try:
            data = json.loads(backup)
            # 加一条无 kind 字段的记录模拟老数据
            data.append({"id": "deadbeef", "name": f"{UNIQ}-legacy", "env": "生产环境", "domain": f"legacy{TS}.example.com", "owner": "legacy"})
            with open(data_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            time.sleep(0.2)
            code, lst = req("GET", "/api/sites")
            rec = [s for s in lst if s["id"] == "deadbeef"][0]
            check("老数据 kind 默认=网站", rec.get("kind") == "网站", f"got {rec.get('kind')!r}")
            check("老数据 connection 默认空", rec.get("connection") == "", f"got {rec.get('connection')!r}")
            # 清理这条测试记录
            req("DELETE", "/api/sites/deadbeef", token=tok)
        finally:
            with open(data_file, "w", encoding="utf-8") as f:
                f.write(backup)
    else:
        print("[SKIP] data/sites.json 不存在，跳过老数据兼容测试")

finally:
    finish()

print(f"\n{'='*40}\n{PASS} passed, {FAIL} failed\n{'='*40}")
sys.exit(0 if FAIL == 0 else 1)
