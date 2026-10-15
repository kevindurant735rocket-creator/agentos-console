#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""环境面第四/五面：浏览器 / 预览的后端取字节面（D88）。

只接回环，只报事实。单独成模块而不是塞进 serve.py，有两个理由：

1. serve.py 已经 1700 行、路由与执行面混在一起；这一面的安全边界
   （scheme / 凭据 / host 三道 fail-closed）值得能被门单独 import 来测，
   不用起一个 HTTP 服务才能验一条校验。
2. 这一面与文件/代码/终端三面没有任何共享状态，抽出来不产生循环依赖。

为什么只允许回环：8790 是常驻在本机 loopback 的长期进程，一个「任意 URL 都代取」
的代理，等于把本机所有在监听的端口（8787 headroom、其它开发服务、任何本地 debug
口）连同能读它们的路径一起开放给任何能打开 localhost:8790 的页面。这是教科书式的
SSRF。限制在回环不砍功能：工作区要看的浏览器/预览本来就在本机开发服务上
（8790 自己、8787、用户自己的 dev server）。

为什么不代取后塞进 iframe src：那样被预览的页面会以为自己在同源下，脚本能顺着
src 拿到同源能力。这里的契约是「取字节、报事实」，渲染发生在前端一块 sandbox
（无 allow-same-origin）里，两头都不给同源。

只用标准库，零外部依赖 —— 与 serve.py 同口径。
"""
import http.client
import socket
import ssl
import time
import urllib.parse

MAX_BYTES = 393216          # 单次取回上限 384 KiB
TIMEOUT = 8.0               # 秒；慢的不等，用户宁可看到超时也不看转圈
HOSTS = frozenset(["127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"])
TEXT_TYPES = ("text/html", "text/plain", "application/json",
              "application/xhtml+xml", "text/css", "application/javascript",
              "text/javascript", "text/markdown", "application/xml", "text/xml")

REJECT_HOST = "只接回环地址（收到 host=%r）—— 这一面是给你看本机开发服务的，不是任意 URL 代理"
REJECT_REDIRECT = "这是重定向（Location: %s），本面不自动跟随 —— 跟随会让「只接回环」这个前提在中途失效"
REJECT_BINARY = "内容类型 %s 不是文本，不在预览面渲染（字节数 %d 如实报上）"


def url_check(raw):
    """解析并校验一个 URL。返回 (url, err)；err 非空就是拒绝理由。

    逐条拒：非 http(s)（file:// / data: / gopher: 一律不接）、带凭据的 URL
    （http://user:pw@host 会把凭据写进日志与前端台面）、非回环 host。
    这三道任何一道都必须 fail-closed —— 校验写松一点，这条端点就成了注释里
    说的那个洞。
    """
    s = (raw or "").strip()
    if not s:
        return None, "空 URL"
    if len(s) > 1024:
        return None, "URL 太长（>1024 字符）"
    try:
        u = urllib.parse.urlsplit(s)
    except ValueError as e:
        return None, "URL 解析失败：%s" % str(e)[:80]
    if u.scheme not in ("http", "https"):
        return None, "只接 http/https（收到 %r）" % u.scheme[:20]
    if u.username or u.password:
        return None, "URL 里不许带凭据"
    host = (u.hostname or "").strip().lower()
    if host not in HOSTS:
        return None, REJECT_HOST % host[:60]
    try:
        u.port                                # 触发端口校验（非法端口在这里抛）
    except ValueError as e:
        return None, "端口非法：%s" % str(e)[:60]
    return s, None


def fetch(raw):
    """取一个回环 URL 的字节，如实报 status / content-type / 字节数 / 耗时。

    四条诚实口径：
    1. 读超时/拒连一律 ok=false + 原话，不返回「页面为空」冒充正常；
    2. 超 MAX_BYTES 一定带 truncated=true，绝不静默截；
    3. 非文本类型直接说「不在预览面渲染」，不塞 base64 假装能看；
    4. 一条重定向也不跟。跟随会把「校验过回环」这个前提在中途丢掉 ——
       本机服务回一个 Location: http://evil/ 就绕过了整道 host 校验。
    """
    url, err = url_check(raw)
    if err:
        return {"ok": False, "error": err, "url": (raw or "")[:200]}
    t0 = time.time()
    u = urllib.parse.urlsplit(url)
    port = u.port or (443 if u.scheme == "https" else 80)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    conn = None
    try:
        if u.scheme == "https":
            ctx = ssl._create_unverified_context()   # 回环自签/本地 https 也要能看
            conn = http.client.HTTPSConnection(u.hostname, port,
                                               timeout=TIMEOUT, context=ctx)
        else:
            conn = http.client.HTTPConnection(u.hostname, port, timeout=TIMEOUT)
        conn.request("GET", path, headers={"User-Agent": "AgentOS-Console/1",
                                           "Accept": "*/*",
                                           "Connection": "close"})
        resp = conn.getresponse()
        loc = resp.getheader("Location")
        if resp.status in (301, 302, 303, 307, 308) and loc:
            return {"ok": False, "url": url, "status": resp.status,
                    "error": REJECT_REDIRECT % loc[:120]}
        raw_bytes = resp.read(MAX_BYTES + 1)
        trunc = len(raw_bytes) > MAX_BYTES
        if trunc:
            raw_bytes = raw_bytes[:MAX_BYTES]
        ctype = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
        base = {"ok": True, "url": url, "status": resp.status,
                "reason": resp.reason, "content_type": ctype or "(无)",
                "bytes": len(raw_bytes), "truncated": trunc,
                "limit": MAX_BYTES, "dur": round(time.time() - t0, 3)}
        if not ctype.startswith(TEXT_TYPES):
            base["ok"] = False
            base["error"] = REJECT_BINARY % (ctype or "(无)", len(raw_bytes))
            return base
        base["text"] = raw_bytes.decode("utf-8", "replace")
        return base
    except socket.timeout:
        return {"ok": False, "url": url,
                "error": "超时（>%.0fs，没有拿到任何字节）" % TIMEOUT}
    except (OSError, ValueError, http.client.HTTPException) as e:
        return {"ok": False, "url": url,
                "error": "%s: %s" % (type(e).__name__, str(e)[:100])}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:                       # noqa: BLE001
                pass
