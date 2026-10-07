# -*- coding: utf-8 -*-
"""sites-nav 全功能端到端测试（导入/查重那部分在 e2e_import.py，这里覆盖其余全部接口）。

跑法: 先起服务（python run.py 或 docker compose up -d），再 python e2e_full.py

覆盖：
  1. 探活 + 静态页面
  2. 站点 CRUD 与字段校验（含备忘录、端口拨测、非法输入）
  3. 快捷监控字段保全（sitePayload 修复点的后端侧回归）
  4. 导出 CSV / JSON
  5. 端口拨测目标 CRUD 与冲突检测
  6. 用户管理（创建/改密/启停/角色/删除 + 各种保护）
  7. 角色权限（普通用户只读、写操作 403、token 失效）
  8. categraf provider（TOML 内容 + version 稳定性/变化）
  9. 数据备份落盘

自建数据带 E2E2- 前缀、测试用户带 e2e2_ 前缀，结束时全部清理，不动真实数据。
登录限流用独立 IP 在进程内测，见 tests/test_api_units.py（避免把本机 IP 锁 15 分钟）。
"""
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:8000"
SP = "E2E2-"          # 站点名前缀（清理靠它）
UP = "e2e2_"          # 测试用户名前缀
TEST_PW = "E2e2Passw0rd"      # ≥10 位、含字母+数字
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def env_get(key):
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def login(username, password):
    return requests.post(f"{BASE}/api/login", json={"username": username, "password": password}, timeout=5)


def get_sites(H):
    r = requests.get(f"{BASE}/api/sites", headers=H, timeout=5)
    if r.status_code != 200:
        raise RuntimeError(f"GET /api/sites {r.status_code}: {r.text[:200]}")
    return r.json()


def cleanup(H):
    """删除本次测试留下的所有站点"""
    for s in get_sites(H):
        if str(s.get("name", "")).startswith(SP) or str(s.get("name", "")).startswith("E2E-导入测试"):
            requests.delete(f"{BASE}/api/sites/{s['id']}", headers=H, timeout=5)


def cleanup_users(H):
    for u in requests.get(f"{BASE}/api/users", headers=H, timeout=5).json():
        if str(u.get("username", "")).startswith(UP):
            requests.delete(f"{BASE}/api/users/{u['id']}", headers=H, timeout=5)


def main():
    try:
        requests.get(f"{BASE}/health", timeout=3)
    except requests.RequestException as e:
        print(f"!! 服务未就绪（{type(e).__name__}）")
        print("   先启动：python run.py  或  ./deploy.sh --deploy")
        return 1

    pwd = env_get("ADMIN_PASSWORD")
    if not pwd:
        print("!! .env 里没有 ADMIN_PASSWORD")
        return 1
    r = login("admin", pwd)
    check("管理员登录", r.status_code == 200 and r.json().get("token"), f"status={r.status_code}")
    if r.status_code != 200:
        return 1
    H = {"Authorization": f"Bearer {r.json()['token']}"}

    cleanup(H)
    cleanup_users(H)
    try:
        # ── 1. 探活与静态页面 ────────────────────────────────────────────
        h = requests.get(f"{BASE}/health", timeout=5)
        check("/health 返回 ok", h.status_code == 200 and h.json().get("ok") is True, str(h.json()))
        for path, kw in (("/", "sites"), ("/admin", ""), ("/probes", "")):
            p = requests.get(f"{BASE}{path}", timeout=5)
            check(f"页面 {path} 可访问", p.status_code == 200 and len(p.text) > 500,
                  f"status={p.status_code} len={len(p.text)}")

        # ── 2. 站点 CRUD 与校验 ──────────────────────────────────────────
        ok_site = {"name": f"{SP}HTTP站点", "kind": "网站", "env": "生产环境",
                   "domain": "e2e2-http.example.com", "category": "业务系统", "owner": "测试员",
                   "remark": "端到端", "memo_content": "备忘录正文-应保留",
                   "monitor": True, "probe_url": "https://e2e2-http.example.com",
                   "probe_status_codes": "200|301", "probe_timeout": "5s", "probe_interval": "30s",
                   "probe_method": "POST", "probe_headers": '["X-Token","abc"]',
                   "probe_body": '{"k":"v"}', "probe_follow_redirects": True,
                   "probe_tls_ca": "/etc/categraf/ca.pem", "probe_cert_expire": False}
        c = requests.post(f"{BASE}/api/sites", headers=H, json=ok_site, timeout=5)
        check("创建 HTTP 站点", c.status_code == 200, f"status={c.status_code} {c.text[:120]}")
        sid = c.json().get("id") if c.status_code == 200 else None

        port_site = {"name": f"{SP}端口站点", "kind": "非网站", "env": "测试环境",
                     "connection": "redis://10.9.9.11:6379/0", "monitor": True,
                     "probe_protocol": "udp", "probe_timeout": "2s", "probe_read_timeout": "3s",
                     "probe_send": "PING", "probe_expect": "PONG",
                     "memo_content": "端口站备忘录"}
        c2 = requests.post(f"{BASE}/api/sites", headers=H, json=port_site, timeout=5)
        check("创建端口拨测站点", c2.status_code == 200, f"status={c2.status_code} {c2.text[:120]}")
        pid = c2.json().get("id") if c2.status_code == 200 else None

        memo = {"name": f"{SP}备忘录", "kind": "备忘录", "env": "生产环境", "memo_content": "纯文本正文"}
        c3 = requests.post(f"{BASE}/api/sites", headers=H, json=memo, timeout=5)
        check("创建备忘录（无需地址）", c3.status_code == 200, f"status={c3.status_code}")
        # 备忘录强制不拨测：即使误传 monitor=true
        m = next((s for s in get_sites(H) if s["name"] == f"{SP}备忘录"), {})
        check("备忘录强制 monitor=false", m.get("monitor") is False, f"monitor={m.get('monitor')}")

        # 非法输入
        bad_cases = [
            ("缺名称", {"kind": "网站", "env": "生产环境", "domain": "e2e2-x.example.com"}),
            ("环境值非法", {"name": f"{SP}坏环境", "env": "生产", "domain": "e2e2-x.example.com"}),
            ("无任何地址", {"name": f"{SP}无地址", "kind": "网站", "env": "生产环境"}),
            ("URL 含空格", {"name": f"{SP}坏URL", "env": "生产环境", "public_url": "http://a b"}),
            ("端口越界", {"name": f"{SP}坏端口", "env": "生产环境", "public_url": "http://x:99999"}),
            ("状态码非法", {"name": f"{SP}坏状态码", "env": "生产环境", "domain": "e2e2-b.example.com",
                            "monitor": True, "probe_status_codes": "20"}),
            ("超时格式非法", {"name": f"{SP}坏超时", "env": "生产环境", "domain": "e2e2-c.example.com",
                              "monitor": True, "probe_timeout": "5x"}),
            ("域名填 IP", {"name": f"{SP}域名填IP", "env": "生产环境", "domain": "10.0.0.1"}),
        ]
        for label, body in bad_cases:
            rb = requests.post(f"{BASE}/api/sites", headers=H, json=body, timeout=5)
            check(f"拒绝非法创建：{label}", rb.status_code in (400, 422),
                  f"status={rb.status_code}")

        # 重复：同名同环境 / 同地址
        rd = requests.post(f"{BASE}/api/sites", headers=H, json=dict(ok_site), timeout=5)
        check("同名同环境被拒（更新语义不适用于创建）", rd.status_code == 409,
              f"status={rd.status_code} {rd.text[:100]}")
        rd2 = requests.post(f"{BASE}/api/sites", headers=H,
                            json={"name": f"{SP}换名", "env": "测试环境",
                                  "domain": "e2e2-http.example.com"}, timeout=5)
        check("同地址被拒", rd2.status_code == 409, f"status={rd2.status_code} {rd2.text[:100]}")

        # 读取 / 更新 / 404
        one = requests.get(f"{BASE}/api/sites", headers=H, timeout=5).json()
        check("列表包含刚建的记录", any(s["id"] == sid for s in one), f"共 {len(one)} 条")
        up = requests.put(f"{BASE}/api/sites/{sid}", headers=H,
                          json={**ok_site, "remark": "改过的备注"}, timeout=5)
        check("更新站点", up.status_code == 200, f"status={up.status_code}")
        after = next((s for s in get_sites(H) if s["id"] == sid), {})
        check("更新生效", after.get("remark") == "改过的备注", f"remark={after.get('remark')}")
        nf = requests.put(f"{BASE}/api/sites/nonexist", headers=H, json=ok_site, timeout=5)
        check("更新不存在的站点 → 404", nf.status_code == 404, f"status={nf.status_code}")

        # ── 2b. 批量删除 ────────────────────────────────────────────────
        made = []
        for i in range(3):
            rr = requests.post(f"{BASE}/api/sites", headers=H, json={
                "name": f"{SP}批量删除{i}", "env": "测试环境",
                "domain": f"e2e2-batch{i}.example.com"}, timeout=5)
            if rr.status_code == 200:
                made.append(rr.json()["id"])
        check("批量删除：先建 3 条", len(made) == 3, f"n={len(made)}")
        if len(made) == 3:
            br = requests.post(f"{BASE}/api/sites/batch-delete", headers=H,
                               json={"ids": made[:2] + ["deadbeef"]}, timeout=10)
            check("批量删除返回真正删掉的数量",
                  br.status_code == 200 and br.json().get("deleted") == 2, str(br.json())[:160])
            check("批量删除报告不存在的 id（不静默吞）",
                  br.status_code == 200 and br.json().get("not_found") == ["deadbeef"], str(br.json())[:160])
            left = {s["id"] for s in get_sites(H)}
            check("只剩没选中的那一条", made[2] in left and made[0] not in left and made[1] not in left,
                  f"存在情况={[m in left for m in made]}")
            check("批量删除空列表 → 422",
                  requests.post(f"{BASE}/api/sites/batch-delete", headers=H,
                                json={"ids": []}, timeout=5).status_code == 422)
            check("批量删除纯空白 id → 400",
                  requests.post(f"{BASE}/api/sites/batch-delete", headers=H,
                                json={"ids": ["   "]}, timeout=5).status_code == 400)
            check("批量删除不存在的 id 全部 → deleted=0",
                  requests.post(f"{BASE}/api/sites/batch-delete", headers=H,
                                json={"ids": ["ffffffff"]}, timeout=5).json().get("deleted") == 0)

        # ── 3. 快捷监控字段保全（sitePayload 修复点） ────────────────────
        # 修好后的 sitePayload 会把整条记录铺开（只去掉 id/时间戳）。这里就用这个形状 PUT，
        # 断言每个字段都原样保留 —— 漏任何一个（memo_content / probe_cert_expire /
        # probe_protocol / probe_read_timeout / probe_send / probe_expect）都算回归。
        for label, target_id in (("HTTP 站点", sid), ("端口站点", pid)):
            rec = next((s for s in get_sites(H) if s["id"] == target_id), None)
            if not rec:
                check(f"快捷监控字段保全：{label} 存在", False)
                continue
            keep = {k: v for k, v in rec.items() if k not in ("id", "created_at", "updated_at")}
            keep["monitor"] = False          # 模拟「取消监控」
            requests.put(f"{BASE}/api/sites/{target_id}", headers=H, json=keep, timeout=5)
            back = next((s for s in get_sites(H) if s["id"] == target_id), {})
            lost = [k for k, v in keep.items() if back.get(k) != v]
            check(f"取消监控不丢字段：{label}", not lost, f"丢/变了: {lost}")
        # 具体点名几个曾经被洗掉的字段，失败时一眼看出是哪个
        focus = next((s for s in get_sites(H) if s["id"] == pid), {})
        check("  协议未被洗回 tcp", focus.get("probe_protocol") == "udp",
              f"probe_protocol={focus.get('probe_protocol')}")
        check("  发送内容未丢", focus.get("probe_send") == "PING", f"send={focus.get('probe_send')}")
        check("  期望响应未丢", focus.get("probe_expect") == "PONG", f"expect={focus.get('probe_expect')}")
        check("  备忘录正文未丢", focus.get("memo_content") == "端口站备忘录",
              f"memo_content={focus.get('memo_content')}")
        focus_h = next((s for s in get_sites(H) if s["id"] == sid), {})
        check("  证书采集开关未被洗（仍为 False）", focus_h.get("probe_cert_expire") is False,
              f"probe_cert_expire={focus_h.get('probe_cert_expire')}")
        # 恢复监控，后面 provider 用例要用
        requests.put(f"{BASE}/api/sites/{sid}", headers=H, json={**ok_site, "monitor": True}, timeout=5)
        requests.put(f"{BASE}/api/sites/{pid}", headers=H, json={**port_site, "monitor": True}, timeout=5)

        # ── 4. 导出 ─────────────────────────────────────────────────────
        ec = requests.get(f"{BASE}/api/sites/export?format=csv", headers=H, timeout=10)
        check("导出 CSV", ec.status_code == 200 and ec.content[:3] == b"\xef\xbb\xbf",
              f"status={ec.status_code} BOM={ec.content[:3]!r}")
        text = ec.content.decode("utf-8-sig")
        head = text.splitlines()[0]
        need_cols = ["系统名称", "拨测监控", "拨测状态码", "探测间隔", "拨测方法", "请求头",
                     "跟随重定向", "跳过证书校验", "私有CA路径", "采集证书过期时间", "备忘录内容"]
        miss = [c for c in need_cols if c not in head]
        check("导出 CSV 列完整", not miss, f"缺: {miss}")
        row = next((ln for ln in text.splitlines() if f"{SP}HTTP站点" in ln), "")
        check("导出 CSV 含记录值", "200|301" in row and "30s" in row, row[:120])
        ej = requests.get(f"{BASE}/api/sites/export?format=json", headers=H, timeout=10)
        jd = ej.json()
        check("导出 JSON", ej.status_code == 200 and isinstance(jd, list) and len(jd) > 0,
              f"status={ej.status_code} n={len(jd) if isinstance(jd, list) else '?'}")
        check("导出 JSON 不含内部 id", isinstance(jd, list) and all("id" not in s for s in jd))

        # ── 5. 端口拨测目标 CRUD 与冲突 ─────────────────────────────────
        pr = requests.post(f"{BASE}/api/probes", headers=H,
                           json={"url": "10.9.9.20:6379", "job": f"{SP}独立探针",
                                 "protocol": "tcp", "timeout": "3s"}, timeout=5)
        check("创建独立拨测目标", pr.status_code == 200, f"status={pr.status_code} {pr.text[:120]}")
        probe_id = pr.json().get("id") if pr.status_code == 200 else None
        pl = requests.get(f"{BASE}/api/probes", headers=H, timeout=5)
        check("列出拨测目标", pl.status_code == 200 and any(p["id"] == probe_id for p in pl.json()))

        # 站点连接串撞上独立目标 → 409
        conf = requests.post(f"{BASE}/api/sites", headers=H,
                             json={"name": f"{SP}撞独立目标", "env": "生产环境",
                                   "connection": "10.9.9.20:6379", "monitor": True}, timeout=5)
        check("站点撞独立拨测目标 → 409", conf.status_code == 409, f"status={conf.status_code} {conf.text[:120]}")
        # 独立目标撞站点连接串 → 409
        conf2 = requests.post(f"{BASE}/api/probes", headers=H,
                              json={"url": "10.9.9.11:6379", "job": f"{SP}撞站点"}, timeout=5)
        check("独立目标撞站点连接串 → 409", conf2.status_code == 409, f"status={conf2.status_code}")
        # 更新 / 删除
        pu = requests.put(f"{BASE}/api/probes/{probe_id}", headers=H,
                          json={"url": "10.9.9.21:6379", "job": f"{SP}独立探针改",
                                "protocol": "udp", "timeout": "5s"}, timeout=5)
        check("更新独立拨测目标", pu.status_code == 200, f"status={pu.status_code}")
        pd = requests.delete(f"{BASE}/api/probes/{probe_id}", headers=H, timeout=5)
        check("删除独立拨测目标", pd.status_code == 200, f"status={pd.status_code}")
        check("删除后不在列表", all(p["id"] != probe_id for p in
                                  requests.get(f"{BASE}/api/probes", headers=H, timeout=5).json()))

        # ── 6. 用户管理 ─────────────────────────────────────────────────
        uname = f"{UP}reader"
        uc = requests.post(f"{BASE}/api/users", headers=H,
                           json={"username": uname, "password": TEST_PW, "role": "user"}, timeout=5)
        check("创建普通用户", uc.status_code == 200, f"status={uc.status_code} {uc.text[:100]}")
        users = requests.get(f"{BASE}/api/users", headers=H, timeout=5).json()
        target = next((u for u in users if u["username"] == uname), {})
        uid = target.get("id")
        check("用户列表含新用户", bool(uid), f"n={len(users)}")
        check("用户视图不含密码哈希", all("password_hash" not in u for u in users))
        rc = requests.post(f"{BASE}/api/users", headers=H,
                           json={"username": uname, "password": TEST_PW, "role": "user"}, timeout=5)
        check("重复用户名 → 409", rc.status_code == 409, f"status={rc.status_code}")
        for label, pw in (("太短", "Ab1"), ("纯字母", "abcdefghijkl"), ("纯数字", "1234567890")):
            rp = requests.post(f"{BASE}/api/users", headers=H,
                               json={"username": f"{UP}x_{abs(hash(label)) % 9999}",
                                     "password": pw, "role": "user"}, timeout=5)
            check(f"弱密码被拒：{label}", rp.status_code == 400, f"status={rp.status_code}")

        # 普通用户可读、不可写
        rl = login(uname, TEST_PW)
        check("普通用户登录", rl.status_code == 200, f"status={rl.status_code}")
        HU = {"Authorization": f"Bearer {rl.json()['token']}"}
        check("普通用户可读列表", requests.get(f"{BASE}/api/sites", headers=HU, timeout=5).status_code == 200)
        forbidden = [
            ("创建站点", lambda: requests.post(f"{BASE}/api/sites", headers=HU, json=ok_site, timeout=5)),
            ("更新站点", lambda: requests.put(f"{BASE}/api/sites/{sid}", headers=HU, json=ok_site, timeout=5)),
            ("删除站点", lambda: requests.delete(f"{BASE}/api/sites/{sid}", headers=HU, timeout=5)),
            ("用户列表", lambda: requests.get(f"{BASE}/api/users", headers=HU, timeout=5)),
            ("拨测目标", lambda: requests.get(f"{BASE}/api/probes", headers=HU, timeout=5)),
            ("配置预览", lambda: requests.get(f"{BASE}/api/config/preview", headers=HU, timeout=5)),
            ("批量删除", lambda: requests.post(f"{BASE}/api/sites/batch-delete", headers=HU,
                                           json={"ids": ["aaaaaaaa"]}, timeout=5)),
        ]
        for label, fn in forbidden:
            check(f"普通用户被拒：{label}", fn().status_code == 403)

        # 无 token / 坏 token
        check("无 token → 401", requests.get(f"{BASE}/api/sites", timeout=5).status_code == 401)
        check("坏 token → 401",
              requests.get(f"{BASE}/api/sites", headers={"Authorization": "Bearer bad"}, timeout=5).status_code == 401)

        # 改密码 → 旧 token 立即失效；新密码可登录
        rp2 = requests.post(f"{BASE}/api/users/{uid}/password", headers=H,
                            json={"password": "NewE2ePassw0rd"}, timeout=5)
        check("重置密码", rp2.status_code == 200, f"status={rp2.status_code}")
        check("改密后旧 token 失效",
              requests.get(f"{BASE}/api/sites", headers=HU, timeout=5).status_code == 401)
        check("新密码可登录", login(uname, "NewE2ePassw0rd").status_code == 200)
        check("旧密码不可登录", login(uname, TEST_PW).status_code == 401)

        # 禁用 → 登录失败；启用恢复
        rd3 = requests.put(f"{BASE}/api/users/{uid}", headers=H, json={"enabled": False}, timeout=5)
        check("禁用用户", rd3.status_code == 200, f"status={rd3.status_code}")
        check("禁用后无法登录", login(uname, "NewE2ePassw0rd").status_code == 401)
        requests.put(f"{BASE}/api/users/{uid}", headers=H, json={"enabled": True}, timeout=5)
        check("启用后可登录", login(uname, "NewE2ePassw0rd").status_code == 200)
        check("无字段更新 → 400",
              requests.put(f"{BASE}/api/users/{uid}", headers=H, json={}, timeout=5).status_code == 400)
        check("更新不存在用户 → 404",
              requests.put(f"{BASE}/api/users/nonexist", headers=H, json={"enabled": True}, timeout=5).status_code == 404)

        # 保护：不能删自己、不能删最后一个管理员
        me = next(u for u in requests.get(f"{BASE}/api/users", headers=H, timeout=5).json()
                  if u["username"] == "admin")
        check("不能删除自己 → 400",
              requests.delete(f"{BASE}/api/users/{me['id']}", headers=H, timeout=5).status_code == 400)
        admins = [u for u in requests.get(f"{BASE}/api/users", headers=H, timeout=5).json()
                  if u["role"] == "admin"]
        if len(admins) == 1:
            check("最后一个管理员不能降级 → 400",
                  requests.put(f"{BASE}/api/users/{admins[0]['id']}", headers=H,
                               json={"role": "user"}, timeout=5).status_code == 400)
        else:
            print("SKIP  最后一个管理员保护（当前有多个管理员）")
        check("删除测试用户", requests.delete(f"{BASE}/api/users/{uid}", headers=H, timeout=5).status_code == 200)
        check("删除不存在用户 → 404",
              requests.delete(f"{BASE}/api/users/{uid}", headers=H, timeout=5).status_code == 404)

        # ── 7. 监控状态 / categraf provider ─────────────────────────────
        ms = requests.get(f"{BASE}/api/monitor-status", headers=H, timeout=5)
        check("监控状态可读", ms.status_code == 200 and isinstance(ms.json(), (dict, list)),
              f"status={ms.status_code}")
        mc = requests.get(f"{BASE}/api/monitor-config", headers=H, timeout=5)
        check("监控配置可读", mc.status_code == 200, f"status={mc.status_code}")

        pv = requests.get(f"{BASE}/api/config/preview", headers=H, timeout=5)
        check("配置预览可读", pv.status_code == 200 and "http_toml" in pv.json(), f"status={pv.status_code}")
        toml = pv.json().get("http_toml", "") if pv.status_code == 200 else ""
        check("预览含新建的 HTTP 目标", "e2e2-http.example.com" in pv.json().get("http_toml", ""),
              toml[:120])
        check("预览含端口目标", "10.9.9.11:6379" in pv.json().get("net_toml", ""),
              pv.json().get("net_toml", "")[:120])

        ct = env_get("CATEGRAF_TOKEN")
        if not ct:
            print("SKIP  categraf provider（.env 未配 CATEGRAF_TOKEN）")
        else:
            HC = {"Authorization": f"Bearer {ct}"}
            prov = requests.get(f"{BASE}/api/config/http_response", headers=HC, timeout=10)
            check("provider 拉取成功", prov.status_code == 200, f"status={prov.status_code}")
            pj = prov.json() if prov.status_code == 200 else {}
            v1 = pj.get("version", "")
            confs = pj.get("configs", {})
            check("provider 同时下发 http/net 两个插件",
                  set(confs.keys()) == {"http_response", "net_response"}, f"keys={list(confs.keys())}")
            body = json.dumps(confs, ensure_ascii=False)
            check("provider TOML 含 HTTP 目标", "e2e2-http.example.com" in body)
            check("provider TOML 含端口目标", "10.9.9.11:6379" in body)
            # version 稳定性：内容不变 → version 不变
            prov2 = requests.get(f"{BASE}/api/config/http_response", headers=HC, timeout=10)
            check("内容不变 version 不变", prov2.json().get("version") == v1,
                  f"{v1[:8]} vs {prov2.json().get('version', '')[:8]}")
            # 内容变化 → version 必须变（曾经 interval/cert_expire 改动不影响 version）
            requests.put(f"{BASE}/api/sites/{sid}", headers=H,
                         json={**ok_site, "probe_interval": "31s", "monitor": True}, timeout=5)
            prov3 = requests.get(f"{BASE}/api/config/http_response", headers=HC, timeout=10)
            check("改探测间隔后 version 必变", prov3.json().get("version") != v1,
                  f"{v1[:8]} -> {prov3.json().get('version', '')[:8]}")
            check("provider 无 token → 401",
                  requests.get(f"{BASE}/api/config/http_response", timeout=10).status_code == 401)
            check("provider 坏 token → 401",
                  requests.get(f"{BASE}/api/config/http_response",
                               headers={"Authorization": "Bearer bad"}, timeout=10).status_code == 401)

        # ── 8. 备份落盘 ─────────────────────────────────────────────────
        data_file = ROOT / "data" / "sites.json"
        bak = ROOT / "data" / "sites.json.bak"
        if data_file.exists() and time.time() - data_file.stat().st_mtime < 600:
            check("主数据文件已落盘", data_file.stat().st_size > 2)
            check("备份文件存在", bak.exists(), f"path={bak}")
            if bak.exists():
                try:
                    items = json.loads(bak.read_text(encoding="utf-8"))
                    check("备份内容是可解析的 JSON 数组", isinstance(items, list), f"n={len(items)}")
                except Exception as e:
                    check("备份内容可解析", False, f"{type(e).__name__}: {e}")
        else:
            print("SKIP  备份检查（本地 data/sites.json 不新鲜，服务可能跑在容器里）")

    finally:
        cleanup(H)
        cleanup_users(H)

    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} passed")
    if passed != len(RESULTS):
        print("\n失败项:")
        for n, ok in RESULTS:
            if not ok:
                print("  -", n)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
