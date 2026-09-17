"""categraf http_provider 的 TOML 配置生成器。

移植自 categraf-http-admin/toml.go，保持 TOML 输出格式完全一致：
- http_response: [mappings] + [[instances]] 按配置画像分组合并
- net_response:  同上，画像 = protocol + timeout + read_timeout + send + expect
- 默认值省略：与 categraf 默认行为一致的配置自动省略（tcp 协议、GET 方法等）
- expect_response_status_codes 必须显式落盘（categraf 不配时不检查状态码）
"""

import json
import re

# ── 拨测类型 ──
KIND_HTTP = "http"
KIND_NET = "net"

# 期望状态码格式：三位数字，多值用 | 分隔
_STATUS_CODES_RE = re.compile(r"^\d{3}(\|\d{3})*$")


def validate_status_codes(s: str) -> str:
    """校验期望状态码格式，非法返回错误信息，合法返回空串"""
    if not s:
        return ""
    if not _STATUS_CODES_RE.match(s):
        return f"状态码格式非法: {s}（应为三位数字，多值用|分隔，如 200 或 200|301）"
    return ""


# ── HTTP 拨测配置画像 ──

def _http_profile_key(t: dict) -> str:
    """HTTP 拨测配置画像 key：只有完全相同的配置才能合并到同一个 [[instances]]"""
    profile = {
        "method": t.get("method", "GET"),
        "expected_status_codes": t.get("expected_status_codes", "200"),
        "response_timeout": t.get("response_timeout", ""),
        "body": t.get("body", ""),
        "headers": _header_key(t.get("headers", [])),
        "use_tls": t.get("use_tls", False),
        "tls_ca": t.get("tls_ca", ""),
        "insecure_skip_verify": t.get("insecure_skip_verify", False),
        "follow_redirects": t.get("follow_redirects"),
    }
    return json.dumps(profile, ensure_ascii=False, sort_keys=True)


def _header_key(headers: list) -> str:
    if not headers:
        return ""
    return "\x00".join(sorted(headers))


# ── 端口拨测配置画像 ──

def _net_profile_key(t: dict) -> str:
    profile = {
        "protocol": t.get("protocol", "tcp"),
        "timeout": t.get("timeout", ""),
        "read_timeout": t.get("read_timeout", ""),
        "send": t.get("send", ""),
        "expect": t.get("expect", ""),
    }
    return json.dumps(profile, ensure_ascii=False, sort_keys=True)


# ── 生成 http_response.toml ──

def _dedup_targets(targets: list[dict]) -> list[dict]:
    """按 url 去重（大小写不敏感），保留先出现的。
    同一个 url 出现两次时 [mappings] 会产生重复键，TOML 解析直接失败 ——
    后果是整份配置被 categraf 拒绝、该类拨测**全部**中断，而不是只少这一条。
    正常写入路径（_find_duplicate / create_probe）已经拦住，这里兜底历史脏数据，
    保证生成的 TOML 永远可解析。"""
    seen: set[str] = set()
    out: list[dict] = []
    for t in targets:
        key = (t.get("url") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def generate_http_toml(targets: list[dict]) -> str:
    """将 HTTP 拨测目标渲染为 http_response.toml"""
    targets = _dedup_targets([t for t in targets if t.get("kind", KIND_HTTP) == KIND_HTTP])
    if not targets:
        return "# (no targets configured)\n"

    # 按完整配置分组合并
    groups: dict[str, dict] = {}
    keys: list[str] = []
    for t in targets:
        k = _http_profile_key(t)
        if k not in groups:
            keys.append(k)
            groups[k] = {
                "profile": {
                    "method": t.get("method", "GET"),
                    "expected_status_codes": t.get("expected_status_codes", "200"),
                    "response_timeout": t.get("response_timeout", ""),
                    "body": t.get("body", ""),
                    "headers": t.get("headers", []),
                    "use_tls": t.get("use_tls", False),
                    "tls_ca": t.get("tls_ca", ""),
                    "insecure_skip_verify": t.get("insecure_skip_verify", False),
                    "follow_redirects": t.get("follow_redirects"),
                },
                "urls": [],
            }
        groups[k]["urls"].append(t["url"])

    lines: list[str] = []

    # [mappings] 段：给每个 target 打上 job 标签
    has_mapping = False
    for t in targets:
        job = t.get("job", "")
        if job:
            if not has_mapping:
                lines.append("[mappings]")
                has_mapping = True
            lines.append(f'{_toml_quote(t["url"])} = {{ job = {_toml_quote(job)} }}')
    if has_mapping:
        lines.append("")

    # [[instances]] 段
    for gi, k in enumerate(keys):
        g = groups[k]
        urls = sorted(g["urls"])
        p = g["profile"]

        if gi > 0:
            lines.append("")

        lines.append("[[instances]]")
        lines.append("targets = [")
        for i, u in enumerate(urls):
            comma = "," if i < len(urls) - 1 else ""
            lines.append(f"    {_toml_quote(u)}{comma}")
        lines.append("]")

        # 超时：为空则不写，categraf 用默认值
        if p["response_timeout"]:
            lines.append(f'response_timeout = {_toml_quote(p["response_timeout"])}')
        # method：GET 是 categraf 默认值，省略
        if p["method"] and p["method"] != "GET":
            lines.append(f'method = {_toml_quote(p["method"])}')
        # 状态码必须显式落盘：categraf 不配置时不做任何状态码检查
        if p["expected_status_codes"]:
            lines.append(f'expect_response_status_codes = {_toml_quote(p["expected_status_codes"])}')
        if p["headers"]:
            quoted = ", ".join(_toml_quote(h) for h in sorted(p["headers"]))
            lines.append(f"headers = [{quoted}]")
        if p["body"]:
            # body 一律用单行 quoted string，不用多行基本字符串（"""）。
            # 多行写法有两处硬伤，都实测确认过：
            #   1) TOML 只裁掉紧跟开头的换行，结尾那个换行保留 —— 每条 body 都被静默加尾随 \n，
            #      对做 body 签名/校验和的接口是坏数据。
            #   2) 多行基本字符串仍会处理转义序列：body 里的字面 \n 会变成真实换行（请求体被改写）；
            #      出现 \U \u 等非法转义时整份 TOML 解析失败（Invalid hex value），
            #      后果不是"这一条目标挂"，而是 categraf 拒绝整份 http_response，所有 HTTP 拨测一起中断。
            # probe_body 只过滤控制字符、反斜杠合法，所以上面两种情况从页面填写即可触发。
            # 代价是长 body 在 TOML 里不好看，换来的是配置永远可解析。别改回多行。
            lines.append(f'body = {_toml_quote(p["body"])}')
        # follow_redirects：显式设置才落盘，留空用 categraf 默认值
        if p["follow_redirects"] is not None:
            lines.append(f'follow_redirects = {"true" if p["follow_redirects"] else "false"}')
        if p["use_tls"] or p["insecure_skip_verify"]:
            # insecure_skip_verify 必须配合 use_tls = true 才生效
            lines.append("use_tls = true")
            if p["tls_ca"]:
                lines.append(f'tls_ca = {_toml_quote(p["tls_ca"])}')
            if p["insecure_skip_verify"]:
                lines.append("insecure_skip_verify = true")

    return "\n".join(lines) + "\n"


# ── 生成 net_response.toml ──

def generate_net_toml(targets: list[dict]) -> str:
    """将端口拨测目标渲染为 net_response.toml"""
    targets = _dedup_targets([t for t in targets if t.get("kind") == KIND_NET])
    if not targets:
        return "# (no targets configured)\n"

    groups: dict[str, dict] = {}
    keys: list[str] = []
    for t in targets:
        k = _net_profile_key(t)
        if k not in groups:
            keys.append(k)
            groups[k] = {
                "profile": {
                    "protocol": t.get("protocol", "tcp"),
                    "timeout": t.get("timeout", ""),
                    "read_timeout": t.get("read_timeout", ""),
                    "send": t.get("send", ""),
                    "expect": t.get("expect", ""),
                },
                "addrs": [],
            }
        groups[k]["addrs"].append(t["url"])

    lines: list[str] = []

    # [mappings] 段
    has_mapping = False
    for t in targets:
        job = t.get("job", "")
        if job:
            if not has_mapping:
                lines.append("[mappings]")
                has_mapping = True
            lines.append(f'{_toml_quote(t["url"])} = {{ job = {_toml_quote(job)} }}')
    if has_mapping:
        lines.append("")

    # [[instances]] 段
    for gi, k in enumerate(keys):
        g = groups[k]
        addrs = sorted(g["addrs"])
        p = g["profile"]

        if gi > 0:
            lines.append("")

        lines.append("[[instances]]")
        lines.append("targets = [")
        for i, a in enumerate(addrs):
            comma = "," if i < len(addrs) - 1 else ""
            lines.append(f"    {_toml_quote(a)}{comma}")
        lines.append("]")

        # tcp 为 categraf 默认值，仅 udp 时显式落盘
        if p["protocol"] == "udp":
            lines.append('protocol = "udp"')
        if p["timeout"]:
            lines.append(f'timeout = {_toml_quote(p["timeout"])}')
        if p["read_timeout"]:
            lines.append(f'read_timeout = {_toml_quote(p["read_timeout"])}')
        if p["send"]:
            lines.append(f'send = {_toml_quote(p["send"])}')
        if p["expect"]:
            lines.append(f'expect = {_toml_quote(p["expect"])}')

    return "\n".join(lines) + "\n"


# ── TOML 字符串转义 ──

def _toml_quote(s: str) -> str:
    """TOML 双引号字符串转义。
    TOML 要求 U+0000–U+0008、U+000A–U+001F、U+007F 全部转义，少一个就是
    Illegal character，整份 net_response 解析失败、所有端口拨测一起中断。
    注意 U+007F 不在 < 0x20 里，Go 的 %q 也只转 < 0x20，所以这个坑两侧都有。
    非 ASCII 可打印字符（中文名、环境标识等）保持原样输出（对齐原 categraf-http-admin
    用 %q 的行为），仅对 TOML 要求转义的控制字符做转义 —— 否则中文会变成 \\u4e2d\\u6587 一串
    不可读的转义，预览完全没法看。"""
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    s = s.replace("\b", "\\b").replace("\f", "\\f")
    # 统一判可打印：控制字符与 0x7F 转义；可打印非 ASCII（中文等）保留原样
    s = "".join(c if c.isprintable() else f"\\u{ord(c):04x}" for c in s)
    return f'"{s}"'


# ── 配置版本 ──

def config_version(targets: list[dict]) -> str:
    """计算当前配置 MD5。内容不变则 version 不变，categraf 不会误重启采集实例。
    生成器版本盐：TOML 生成逻辑变更时递增，强制 categraf 重新拉取配置。
    """
    import hashlib
    h = hashlib.md5()
    # 生成器版本盐：TOML 生成逻辑变更时递增，强制 categraf 重新拉取配置
    h.update(b"schema:v3|")
    sorted_targets = sorted(targets, key=lambda t: t.get("id", ""))
    for t in sorted_targets:
        headers = t.get("headers") or []
        parts = [
            t.get("id", ""),
            t.get("kind", ""),
            t.get("url", ""),
            t.get("method", ""),
            t.get("job", ""),
            t.get("expected_status_codes", ""),
            t.get("response_timeout", ""),
            str(bool(t.get("use_tls", False))),
            t.get("tls_ca", ""),
            str(bool(t.get("insecure_skip_verify", False))),
            t.get("protocol", ""),
            t.get("timeout", ""),
            t.get("read_timeout", ""),
            t.get("send", ""),
            t.get("expect", ""),
            t.get("body", ""),
            json.dumps(sorted(headers), ensure_ascii=False) if headers else "",
            str(t.get("follow_redirects")),
        ]
        h.update("|".join(parts).encode("utf-8"))
    return h.hexdigest()
