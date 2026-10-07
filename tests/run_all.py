# -*- coding: utf-8 -*-
"""一键跑全部测试套件。

跑法：python tests/run_all.py

分两类：
  离线套件（不需要服务）：toml_gen / url_validation / api_units / frontend_payload / ui_browser
  在线套件（需要服务在 8000 跑着）：e2e_import / e2e_full —— 服务没起会自动跳过并提示
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

OFFLINE = [
    ("单元 · TOML 生成", "tests/test_toml_gen.py"),
    ("单元 · URL 校验与前后端对表", "tests/test_url_validation.py"),
    ("单元 · 限流/provider/持久化/密码", "tests/test_api_units.py"),
    ("前端 · sitePayload 与草稿快照（node）", "tests/test_frontend_payload.py"),
    ("前端 · 无头浏览器 DOM 交互", "tests/test_ui_browser.py"),
]
ONLINE = [
    ("端到端 · 导入/查重/拨测冲突", "e2e_import.py"),
    ("端到端 · 全接口（站点/用户/权限/provider/备份）", "e2e_full.py"),
]


def service_up() -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def run(label, rel):
    p = ROOT / rel
    if not p.exists():
        return label, None, "文件不存在"
    proc = subprocess.run([PY, str(p)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=str(ROOT))
    out = (proc.stdout or "") + (proc.stderr or "")
    summary = ""
    for line in reversed(out.strip().splitlines()):
        if "passed" in line or "通过" in line or line.startswith("SKIP"):
            summary = line.strip()
            break
    skipped = summary.startswith("SKIP") or "SKIP" in summary
    return label, (None if skipped else proc.returncode == 0), summary or out.strip()[-120:]


def main():
    online = service_up()
    rows = []
    for label, rel in OFFLINE:
        rows.append(run(label, rel))
    if online:
        for label, rel in ONLINE:
            rows.append(run(label, rel))
    else:
        for label, rel in ONLINE:
            rows.append((label, None, "SKIP 服务未启动（python run.py）"))

    print("=" * 78)
    print(f"{'套件':<40} {'结果':<8} 摘要")
    print("-" * 78)
    bad = 0
    for label, ok, summary in rows:
        mark = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
        if ok is False:
            bad += 1
        print(f"{label:<40} {mark:<8} {summary}")
    print("=" * 78)
    print(f"套件总数 {len(rows)}；失败 {bad}")
    if not online:
        print("提示：起服务后再跑一次可覆盖两个端到端套件（python run.py）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
