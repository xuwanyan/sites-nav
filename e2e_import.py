# -*- coding: utf-8 -*-
"""导入功能端到端测试。

只依赖 sites-nav 自身，不需要 categraf 管理端。
跑法: 先起服务（python run.py 或 docker compose up -d），再 python e2e_import.py

覆盖：JSON / CSV / 中英文表头 / 查重 / 非法行 / 与创建路径的校验一致性 /
端口拨测目标去重。测试会自建数据并在结束时清理，不会动你的真实数据。

注意：服务必须已配置 ADMIN_PASSWORD（.env）。app.py 把已知占位符视为未配置、
改生成随机密码只打进启动日志，那种情况下本脚本无法登录。
TOML 生成器的单元测试不在这（只走导入接口，覆盖不到），见 tests/test_toml_gen.py。
"""
import json
import re
import sys
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8000"
PREFIX = "E2E-导入测试"          # 所有测试记录都带这个前缀，便于清理
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def admin_password():
    p = Path(__file__).resolve().parent / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.startswith("ADMIN_PASSWORD="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def get_sites(H):
    """列出站点。/api/sites 需要登录，漏传 headers 会得到 401 的 {"detail":...}，
    当数组遍历会直接抛 AttributeError —— 这个坑之前让整套测试静默挂掉过。"""
    r = requests.get(f"{BASE}/api/sites", headers=H, timeout=5)
    if r.status_code != 200:
        raise RuntimeError(f"GET /api/sites 返回 {r.status_code}: {r.text[:200]}")
    return r.json()


def cleanup(H):
    """删除本次测试留下的所有记录"""
    for s in get_sites(H):
        if s.get("name", "").startswith(PREFIX):
            r = requests.delete(f"{BASE}/api/sites/{s['id']}", headers=H, timeout=5)
            if r.status_code != 200:
                print(f"  !! 清理失败 {s['id']}: {r.status_code}")


def do_import(H, fmt, content):
    return requests.post(f"{BASE}/api/sites/import", headers=H,
                         json={"format": fmt, "content": content}, timeout=10)


def service_reachable():
    """先探活，返回 (是否就绪, 原因)。

    服务没起时直接说清楚，而不是在 /api/login 上吐一屏 ConnectionError traceback：
    那个堆栈会让人以为是测试脚本的问题，实际只是忘了起服务。"""
    try:
        r = requests.get(f"{BASE}/health", timeout=3)
    except requests.RequestException as exc:
        return False, f"连不上 {BASE}（{type(exc).__name__}）"
    if r.status_code != 200:
        return False, f"/health 返回 {r.status_code}: {r.text[:120]}"
    return True, ""


def main():
    ok, why = service_reachable()
    if not ok:
        print(f"!! 服务未就绪：{why}")
        print(f"   先启动：python run.py（本地）或 ./deploy.sh --deploy（容器）")
        print("   容器路径的镜像级冒烟：./scripts/smoke_docker.sh --start")
        return 1

    pwd = admin_password()
    if not pwd:
        print("!! .env 里没有 ADMIN_PASSWORD，跳过（app.py 未配置时会生成随机密码，只打进启动日志一次）")
        return 1
    r = requests.post(f"{BASE}/api/login", json={"username": "admin", "password": pwd}, timeout=5)
    check("登录", r.status_code == 200 and r.json().get("token"), f"status={r.status_code}")
    if r.status_code != 200:
        print(f"!! 登录失败（username=admin），无法继续。status={r.status_code} body={r.text[:200]}")
        return 1
    H = {"Authorization": f"Bearer {r.json()['token']}"}

    cleanup(H)   # 幂等：先清掉上次没清干净的
    try:
        # ── 1. JSON 导入 ────────────────────────────────────────
        rows = [
            {"name": f"{PREFIX}-JSON-正常", "env": "生产环境", "domain": "e2e-json-1.example.com"},
            {"name": f"{PREFIX}-JSON-带连接串", "env": "测试环境",
             "kind": "缓存", "connection": "redis://10.9.9.1:6379/0", "owner": "运维"},
        ]
        r = do_import(H, "json", json.dumps(rows, ensure_ascii=False))
        check("JSON 导入 2 条", r.status_code == 200 and r.json()["added"] == 2,
              f"status={r.status_code} {r.json() if r.status_code != 200 else ''}")
        kinds = {s["name"]: s.get("kind") for s in get_sites(H)
                 if s["name"].startswith(PREFIX)}
        check("JSON 导入保留 kind 字段", kinds.get(f"{PREFIX}-JSON-带连接串") == "缓存",
              f"kind={kinds.get(PREFIX + '-JSON-带连接串')}")

        # ── 2. CSV 导入 + 中文表头 ──────────────────────────────
        csv_cn = (
            "系统名称,资源类型,分类,域名,公网地址,内网地址,连接串,负责人,环境标识,备注,拨测监控\n"
            f"{PREFIX}-CSV-中文表头,网站,工具,,-,http://10.8.8.8:8888,,张三,测试环境,CSV导入,否\n"
        )
        r = do_import(H, "csv", csv_cn)
        check("CSV 中文表头导入", r.status_code == 200 and r.json()["added"] == 1,
              f"status={r.status_code} {r.json() if r.status_code != 200 else ''}")
        got = [s for s in get_sites(H)
               if s["name"] == f"{PREFIX}-CSV-中文表头"]
        s = got[0] if got else {}
        check("CSV 中文表头: 内网地址落到 private_url", s.get("private_url") == "http://10.8.8.8:8888",
              f"private_url={s.get('private_url')}")
        check("CSV 中文表头: 连接串字段为空", not (s.get("connection") or "").strip())
        check("CSV 中文表头: 负责人已写入", s.get("owner") == "张三")
        check("CSV 中文表头: 监控默认关", s.get("monitor") is False)

        # ── 3. 查重 ─────────────────────────────────────────────
        r = do_import(H, "json", json.dumps([
            {"name": f"{PREFIX}-JSON-正常", "env": "生产环境", "domain": "e2e-dup-name.example.com"},  # 同名同环境
        ], ensure_ascii=False))
        check("同名同环境被跳过", r.json()["added"] == 0 and r.json()["skipped_count"] == 1, str(r.json()))
        r = do_import(H, "json", json.dumps([
            {"name": f"{PREFIX}-JSON-换个名", "env": "测试环境", "domain": "e2e-json-1.example.com"},  # 地址重复
        ], ensure_ascii=False))
        check("地址重复被跳过", r.json()["added"] == 0 and "重复" in str(r.json()["skipped"]), str(r.json()))

        # ── 4. 非法行 ────────────────────────────────────────────
        bad = [
            {"env": "生产环境", "domain": "e2e-x.example.com"},                        # 缺名称
            {"name": f"{PREFIX}-坏环境", "env": "生产", "domain": "e2e-x2.example.com"},  # 环境值非法
            {"name": f"{PREFIX}-无地址", "env": "测试环境"},                              # 无 URL 无连接串
            {"name": f"{PREFIX}-坏URL", "env": "测试环境", "public_url": "http://a b"},   # URL 非法
        ]
        r = do_import(H, "json", json.dumps(bad, ensure_ascii=False))
        check("4 条非法行全部跳过", r.json()["added"] == 0 and r.json()["skipped_count"] == 4,
              str(r.json()["skipped"]))
        # 非法行不应半写入
        check("非法行无残留", not any(x["name"].startswith(PREFIX) and x["name"] in
               (f"{PREFIX}-坏环境", f"{PREFIX}-无地址", f"{PREFIX}-坏URL")
               for x in get_sites(H)))

        # ── 5. 非法 format ────────────────────────────────────────
        r = do_import(H, "xml", "<x/>")
        check("非法 format 返回 400", r.status_code == 400, f"status={r.status_code}")

        # ── 6. 与创建路径的校验一致性（这是重点）─────────────────────
        # 6a. monitor=true 但没有 URL，只有连接串 → 端口拨测，允许创建
        create = requests.post(f"{BASE}/api/sites", headers=H, json={
            "name": f"{PREFIX}-无URL监控", "env": "生产环境",
            "connection": "redis://10.9.9.2:6379/0", "monitor": True,
        }, timeout=5)
        check("创建路径允许: 连接串+监控（端口拨测）", create.status_code == 200,
              f"status={create.status_code} {create.json()}")
        # 注意：连接串必须用不同的，和 6a 相同的会被查重拦掉（连接串全局唯一）
        imp = do_import(H, "json", json.dumps([{
            "name": f"{PREFIX}-无URL监控2", "env": "生产环境",
            "connection": "redis://10.9.9.3:6379/0", "monitor": True,
        }], ensure_ascii=False))
        check("导入路径允许: 连接串+监控（端口拨测）",
              imp.json()["added"] == 1,
              f"added={imp.json()['added']} skipped={imp.json()['skipped']}")

        # ── 7. CSV 拨测参数往返：导出列齐全，导入能读回 ──────────────
        csv_probe = (
            "系统名称,资源类型,分类,域名,公网地址,内网地址,连接串,负责人,环境标识,备注,拨测监控,拨测状态码,拨测超时\n"
            f"{PREFIX}-拨测参数,网站,工具,e2e-probe.example.com,,,,,测试环境,,是,200|301,5s\n"
        )
        r = do_import(H, "csv", csv_probe)
        check("CSV 导入带拨测参数", r.json()["added"] == 1, str(r.json()))
        got = [s for s in get_sites(H)
               if s["name"] == f"{PREFIX}-拨测参数"]
        s = got[0] if got else {}
        check("CSV 拨测状态码已落库", s.get("probe_status_codes") == "200|301", f"={s.get('probe_status_codes')}")
        check("CSV 拨测超时已落库", s.get("probe_timeout") == "5s", f"={s.get('probe_timeout')}")
        check("CSV 拨测监控开关已落库", s.get("monitor") is True, f"={s.get('monitor')}")

        exp = requests.get(f"{BASE}/api/sites/export?format=csv", headers=H, timeout=5).content.decode("utf-8-sig")
        header = exp.splitlines()[0]
        check("导出 CSV 含拨测参数列", "拨测状态码" in header and "拨测超时" in header, header)

        # ── 8. 端口拨测目标去重：同一 host:port 只能有一个来源 ─────────
        # 重复的 host:port 会在 net_response 里产生重复的 [mappings] 键，TOML 解析
        # 失败会让所有端口拨测一起中断，所以必须在写入时拦住。
        probe_url = "10.9.9.8:6379"
        pr = requests.post(f"{BASE}/api/probes", headers=H, json={
            "url": probe_url, "job": f"{PREFIX}-探针",
        }, timeout=5)
        check("先建独立端口拨测目标", pr.status_code == 200, f"status={pr.status_code} {pr.text[:150]}")
        try:
            # 8a. 站点连接串解析出的 host:port 撞上独立目标
            c = requests.post(f"{BASE}/api/sites", headers=H, json={
                "name": f"{PREFIX}-撞独立目标", "env": "生产环境",
                "connection": f"redis://{probe_url}/0", "monitor": True,
            }, timeout=5)
            check("站点连接串撞上拨测页独立目标 → 409", c.status_code == 409,
                  f"status={c.status_code} {c.text[:150]}")

            # 8b. 导入路径同样拦
            imp = do_import(H, "json", json.dumps([{
                "name": f"{PREFIX}-撞独立目标2", "env": "测试环境",
                "connection": f"redis://{probe_url}/0", "monitor": True,
            }], ensure_ascii=False))
            check("导入路径撞上独立目标 → 跳过", imp.json()["added"] == 0,
                  f"added={imp.json()['added']} skipped={imp.json()['skipped']}")

            # 8c. 两个站点连接串写法不同但解析出同一 host:port
            c1 = requests.post(f"{BASE}/api/sites", headers=H, json={
                "name": f"{PREFIX}-写法A", "env": "生产环境",
                "connection": "10.9.9.9:6379", "monitor": True,
            }, timeout=5)
            check("写法A（裸 host:port）可创建", c1.status_code == 200,
                  f"status={c1.status_code} {c1.text[:150]}")
            c2 = requests.post(f"{BASE}/api/sites", headers=H, json={
                "name": f"{PREFIX}-写法B", "env": "测试环境",
                "connection": "REDIS://10.9.9.9:6379/2", "monitor": True,
            }, timeout=5)
            check("写法B（同 host:port）→ 409", c2.status_code == 409,
                  f"status={c2.status_code} {c2.text[:150]}")

            # 8d. 同机不同端口必须放行
            c3 = requests.post(f"{BASE}/api/sites", headers=H, json={
                "name": f"{PREFIX}-不同端口", "env": "测试环境",
                "connection": "10.9.9.9:9092", "monitor": True,
            }, timeout=5)
            check("同机不同端口 → 放行", c3.status_code == 200,
                  f"status={c3.status_code} {c3.text[:150]}")

            # 8e. 有 URL 的站点走 HTTP 拨测，不产生端口目标 → 连接串解析出的
            # host:port 即使撞上也不拦。连接串本身必须不同（连接串全局唯一）
            c4 = requests.post(f"{BASE}/api/sites", headers=H, json={
                "name": f"{PREFIX}-有URL豁免", "env": "生产环境",
                "domain": "e2e-exempt.example.com", "connection": "redis://10.9.9.9:6379/5",
                "monitor": True,
            }, timeout=5)
            check("有 URL 的站点（走 HTTP 拨测）→ 放行", c4.status_code == 200,
                  f"status={c4.status_code} {c4.text[:150]}")
        finally:
            # 清理本段建的独立拨测目标
            for p in requests.get(f"{BASE}/api/probes", headers=H, timeout=5).json():
                if str(p.get("url", "")) == probe_url:
                    requests.delete(f"{BASE}/api/probes/{p['id']}", headers=H, timeout=5)

    finally:
        cleanup(H)

    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} passed")
    if passed != len(RESULTS):
        print("\n失败项:")
        for name, ok in RESULTS:
            if not ok:
                print(f"  - {name}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
