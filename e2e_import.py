# -*- coding: utf-8 -*-
"""导入功能端到端测试。

只依赖 sites-nav 自身，不需要 categraf 管理端。
跑法: python e2e_import.py

覆盖：JSON / CSV / 中英文表头 / 查重 / 非法行 / 与创建路径的校验一致性。
测试会自建数据并在结束时清理，不会动你的真实数据。
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


def cleanup(H):
    """删除本次测试留下的所有记录"""
    for s in requests.get(f"{BASE}/api/sites", timeout=5).json():
        if s.get("name", "").startswith(PREFIX):
            r = requests.delete(f"{BASE}/api/sites/{s['id']}", headers=H, timeout=5)
            if r.status_code != 200:
                print(f"  !! 清理失败 {s['id']}: {r.status_code}")


def do_import(H, fmt, content):
    return requests.post(f"{BASE}/api/sites/import", headers=H,
                         json={"format": fmt, "content": content}, timeout=10)


def main():
    pwd = admin_password()
    r = requests.post(f"{BASE}/api/login", json={"password": pwd}, timeout=5)
    check("登录", r.status_code == 200 and r.json().get("token"), f"status={r.status_code}")
    if r.status_code != 200:
        print("!! 登录失败，无法继续（检查 .env 的 ADMIN_PASSWORD）")
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
        kinds = {s["name"]: s.get("kind") for s in requests.get(f"{BASE}/api/sites", timeout=5).json()
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
        got = [s for s in requests.get(f"{BASE}/api/sites", timeout=5).json()
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
               for x in requests.get(f"{BASE}/api/sites", timeout=5).json()))

        # ── 5. 非法 format ────────────────────────────────────────
        r = do_import(H, "xml", "<x/>")
        check("非法 format 返回 400", r.status_code == 400, f"status={r.status_code}")

        # ── 6. 与创建路径的校验一致性（这是重点）─────────────────────
        # 6a. monitor=true 但没有 URL，只有连接串
        create = requests.post(f"{BASE}/api/sites", headers=H, json={
            "name": f"{PREFIX}-无URL监控", "env": "生产环境",
            "connection": "redis://10.9.9.2:6379/0", "monitor": True,
        }, timeout=5)
        check("创建路径拒绝: 勾选监控但无 URL", create.status_code == 400,
              f"status={create.status_code} {create.json()}")
        imp = do_import(H, "json", json.dumps([{
            "name": f"{PREFIX}-无URL监控", "env": "生产环境",
            "connection": "redis://10.9.9.2:6379/0", "monitor": True,
        }], ensure_ascii=False))
        check("导入路径同样拒绝: 勾选监控但无 URL",
              imp.json()["added"] == 0 and "至少填一个 URL" in str(imp.json()["skipped"]),
              f"added={imp.json()['added']} skipped={imp.json()['skipped']}")

        # 6b. monitor=true 且地址是回环（SSRF 防护）
        create2 = requests.post(f"{BASE}/api/sites", headers=H, json={
            "name": f"{PREFIX}-回环监控", "env": "生产环境",
            "public_url": "http://127.0.0.1:9999", "monitor": True,
        }, timeout=5)
        check("创建路径拒绝: 回环地址+监控", create2.status_code == 400,
              f"status={create2.status_code} {create2.json()}")
        imp2 = do_import(H, "json", json.dumps([{
            "name": f"{PREFIX}-回环监控", "env": "生产环境",
            "public_url": "http://127.0.0.1:9999", "monitor": True,
        }], ensure_ascii=False))
        check("导入路径同样拒绝: 回环地址+监控",
              imp2.json()["added"] == 0 and "不允许拨测" in str(imp2.json()["skipped"]),
              f"added={imp2.json()['added']} skipped={imp2.json()['skipped']}")

        # ── 7. CSV 拨测参数往返：导出列齐全，导入能读回 ──────────────
        csv_probe = (
            "系统名称,资源类型,分类,域名,公网地址,内网地址,连接串,负责人,环境标识,备注,拨测监控,拨测状态码,拨测超时\n"
            f"{PREFIX}-拨测参数,网站,工具,e2e-probe.example.com,,,,,测试环境,,是,200|301,5s\n"
        )
        r = do_import(H, "csv", csv_probe)
        check("CSV 导入带拨测参数", r.json()["added"] == 1, str(r.json()))
        got = [s for s in requests.get(f"{BASE}/api/sites", timeout=5).json()
               if s["name"] == f"{PREFIX}-拨测参数"]
        s = got[0] if got else {}
        check("CSV 拨测状态码已落库", s.get("probe_status_codes") == "200|301", f"={s.get('probe_status_codes')}")
        check("CSV 拨测超时已落库", s.get("probe_timeout") == "5s", f"={s.get('probe_timeout')}")
        check("CSV 拨测监控开关已落库", s.get("monitor") is True, f"={s.get('monitor')}")

        exp = requests.get(f"{BASE}/api/sites/export?format=csv", headers=H, timeout=5).content.decode("utf-8-sig")
        header = exp.splitlines()[0]
        check("导出 CSV 含拨测参数列", "拨测状态码" in header and "拨测超时" in header, header)

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
