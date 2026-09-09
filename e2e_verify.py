import time
import requests

BASE = "http://127.0.0.1:8000"
ADMIN = "http://127.0.0.1:5000"
URL = "http://e2e-probe-test.example.com"

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS" if ok else "FAIL"), name, detail)


def wait_sync(site_id, want_ok=True, contain=""):
    for _ in range(20):
        time.sleep(0.5)
        st = requests.get(f"{BASE}/api/monitor-status", timeout=5).json().get(site_id)
        if st and st.get("ok") == want_ok and (not contain or contain in st.get("message", "")):
            return st
    return st


# 1. 登录 sites-nav
r = requests.post(f"{BASE}/api/login", json={"password": "REDACTED"}, timeout=5)
token = r.json().get("token", "")
check("登录sites-nav", r.status_code == 200 and token, f"status={r.status_code}")
H = {"Authorization": f"Bearer {token}"}

# 清理可能残留的旧数据
for s in requests.get(f"{BASE}/api/sites", timeout=5).json():
    if s["name"] == "E2E拨测验证":
        requests.delete(f"{BASE}/api/sites/{s['id']}", headers=H, timeout=5)
admin_sess = requests.Session()
try:
    admin_sess.post(f"{ADMIN}/login", data={"username": "admin", "password": "x"}, timeout=3)
    for t in admin_sess.get(f"{ADMIN}/api/targets", timeout=3).json().get("targets", []):
        if "e2e-probe-test" in t.get("url", ""):
            admin_sess.delete(f"{ADMIN}/api/targets/{t['id']}", timeout=3)
except requests.ConnectionError:
    pass

# 2. 新增网站并勾选拨测
r = requests.post(
    f"{BASE}/api/sites",
    headers=H,
    json={"name": "E2E拨测验证", "env": "生产环境", "domain": "e2e-probe-test.example.com", "monitor": True},
    timeout=5,
)
check("新增网站(勾拨测)", r.status_code == 200, f"status={r.status_code}")
sid = r.json()["id"]

# 3. 等待后台同步
st = wait_sync(sid)
check("后台同步成功", bool(st and st.get("ok")), st and st.get("message"))

# 4. 检查 admin 侧目标字段
targets = admin_sess.get(f"{ADMIN}/api/targets", timeout=5).json()["targets"]
t = next((x for x in targets if x["url"] == URL), None)
check("admin存在该目标", t is not None)
check("job名带环境后缀", t and t.get("job") == "E2E拨测验证-生产环境", t and t.get("job"))
check("状态码已写入200", t and t.get("expected_status_codes") == "200", t and str(t.get("expected_status_codes")))

# 5. 检查下发给 categraf 的 TOML（接口返回 JSON，config 字段内是 TOML 文本）
cfg = admin_sess.get(f"{ADMIN}/api/config/http_response", timeout=5).json()
toml = "".join(v["config"] for v in cfg["configs"].get("http_response", {}).values())
check("TOML含状态码检查", URL in toml and 'expect_response_status_codes = "200"' in toml)

# 6. 模拟手动配置：admin 侧直接改目标，加 response_timeout、去掉状态码
r = admin_sess.post(
    f"{ADMIN}/api/targets/{t['id']}/edit",
    json={"kind": "http", "url": URL, "job": t["job"], "response_timeout": "3s"},
    timeout=5,
)
check("模拟手动配置", r.status_code == 200, f"status={r.status_code}")

# 7. sites-nav 编辑（改负责人）→ 触发合并更新
r = requests.put(
    f"{BASE}/api/sites/{sid}",
    headers=H,
    json={"name": "E2E拨测验证", "env": "生产环境", "domain": "e2e-probe-test.example.com", "monitor": True, "owner": "运维"},
    timeout=5,
)
check("编辑网站", r.status_code == 200, f"status={r.status_code}")
st = wait_sync(sid, contain="已是最新")
check("编辑后同步完成", bool(st and st.get("ok")), st and st.get("message"))
t2 = next((x for x in admin_sess.get(f"{ADMIN}/api/targets", timeout=5).json()["targets"] if x["url"] == URL), None)
check("合并保留手动timeout", t2 and t2.get("response_timeout") == "3s", t2 and str(t2.get("response_timeout")))
check("合并补齐状态码200", t2 and t2.get("expected_status_codes") == "200", t2 and str(t2.get("expected_status_codes")))

# 8. 清理：删除网站 → admin 目标同步删除
requests.delete(f"{BASE}/api/sites/{sid}", headers=H, timeout=5)
time.sleep(1.5)
t3 = next((x for x in admin_sess.get(f"{ADMIN}/api/targets", timeout=5).json()["targets"] if x["url"] == URL), None)
check("删除后admin目标同步清理", t3 is None)

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
raise SystemExit(1 if failed else 0)
