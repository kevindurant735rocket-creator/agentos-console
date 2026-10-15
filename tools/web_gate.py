#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D88 浏览器/预览面门：那一面真的在**取字节**，而不是画一个 iframe 框。

为什么必须单独一道门（前面 28 门全绿时这一面可以是死的）：
    * env_gate 只覆盖工作区前 3 面（文件/代码/终端）。浏览器与预览是第四、第五面，
      屏上有一个输入框、一个「取字节」按钮、一个 iframe —— 而这三样在**完全没接后端**
      时也是齐的：按钮在、框在、iframe 空着。机械门按「屏里有真内容」判，全绿。
    * 更具体的形状是「端点活着但接不上」或「接上了但撒谎」：后端 /__web 能取到字节，
      前端把 ok=false 的那一次也画成「加载成功」；或者取回的是 2MB，前端塞进
      sandbox 里，屏上看起来有内容 —— 实际早就被 384KiB 上限砍过，门看不见。
    * 这一面还带**安全边界**（只接回环 / 不带凭据 / 只 http(s) / 不跟随重定向）。
      边界写松一点不会让任何一道现有门变红 —— 现有门没有一个会去请求一个外网 URL。
      所以它既需要一个正例门，也需要一个**专门证伪边界还在**的门。

判据分层（刻意不复刻被测对象）：
    * 后端 14 条打**真 HTTP**，打到 serve.py 起在临时端口上的真实例；上游另起一个
      夹具服务（重定向 / 超限文本 / 二进制 / 正常 JSON），不 import 内部函数走后门。
    * 前端 7 条起**真 Chromium**，点输入框、点按钮，读屏上真实 innerText，
      并读 iframe 的 sandbox/src/srcdoc 与父页可达性。
    * 静态两条（不走 HTTP）：webface.py 里没有跟随重定向的写法；/__web 永远
      200 + 带 ok 字段（fail-closed 口径）。

上牙（七个变异体，五个改临时副本、一个改 served 的 index.html 字节、一个纯副本缺失）：
   W1 回环 host 判据改成把 host 改写成本机（放行而非拒）-> 非回环 URL 必须被读到，门红；
   W2 关掉重定向判据（一跟就跟出去）      -> 重定向必须被显式拒，门红；
   W3 truncated 写死 False               -> 超限内容必须报 truncated，门红；
   W4 关掉文本类型判据                    -> 二进制必须被明确拒，门红；
   SV serve.py 里 webface 路径指到不存在的文件 -> 必须 fail-closed 出原话，门红；
   W5 摘掉 srcdoc 赋值（取回了但不渲染）   -> srcdoc 必须真被设上，门红；
   W6 右栏「/__health 读不到」分支写死假值 -> 读不到时必须如实写「不可判定」，门红。
   变异体只改 tools/_webmut_* 临时副本与 served 字节，活的 serve.py / webface.py /
   index.html 零写入，跑完 sha256 自证 + 残留文件自证。

安全红线（刻意的设计约束）：
    * W1 改成「放行 + 把 host 改写成本机」，而不是「去掉判据真去连 evil.example.com」。
      一个为了证明自己抓得住变异体而发真实外网请求的门，本身是缺陷。
    * 因此全部判据用的 URL 只有三类：本机 serve.py、自起夹具、一个已关闭的本机端口。

用法：python3 tools/web_gate.py   # rc=0 全过 / 1 有红项 / 2 环境不可用
"""
import hashlib
import importlib.util
import io
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVE_PY = os.path.join(HERE, "serve.py")
WEBFACE_PY = os.path.join(HERE, "webface.py")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
WORK = os.path.join(ROOT, "work")

G = {}
WHY = {}
PORT = [0]
FIX = [0]        # 上游夹具端口
DEAD = [0]       # 一个已关闭的本机端口（拒连正例）

LIVE = {"index.html": os.path.join(ROOT, "index.html"),
        "serve.py": SERVE_PY, "webface.py": WEBFACE_PY}


def add(name, cond, detail=""):
    try:
        v = bool(cond)
    except Exception as e:                                  # noqa: BLE001
        v = False
        detail = "%s | EXC %r" % (detail, e)
    G[name] = v
    if not v:
        WHY[name] = str(detail)[:400]
    return v


def sha256_file(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def stamp(t):
    return time.strftime("%m-%d %H:%M:%S", time.localtime(t))


def stat_fp(path):
    """活文件指纹：sha256 + size + mtime 三元组。

    为什么不能只比 sha：见 env_gate 同名的实测记录（2026-10-10 14:42 外部
    管线在同一窗口改活 serve.py，两道门同时把「零写入」判红并自认写了活文件）。
    判据不放松，但归因要能自证清白：字节变了就是红，且要说清是谁写的。
    """
    st = os.stat(path)
    return (sha256_file(path), st.st_size, int(st.st_mtime))


def drift_why(nm, base, now, prefix):
    """零写入判红时的归因细节；判绿返回空串（不进 WHY）。"""
    if now[0] == base[0]:
        return ""
    return ("%s 本轮字节被改：size %d->%d mtime %s->%s sha %s..->%s..；"
            "本门变异只写 tools/%s* 临时副本且残留自证为零 -> 写入者在门外"
            % (nm, base[1], now[1], stamp(base[2]), stamp(now[2]),
               base[0][:10], now[0][:10], prefix))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def load_mod(path, tag):
    spec = importlib.util.spec_from_file_location(tag, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def start_server(port, mod):
    """起一份 serve.py 实例；只在门自己的实例上静音 stderr，被测行为一字未改。"""
    from http.server import ThreadingHTTPServer
    quiet = type("QuietHandler", (mod.Handler,),
                 {"log_message": lambda self, *a, **k: None})

    class QuietSrv(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            pass

    srv = QuietSrv(("127.0.0.1", port), quiet)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2},
                     daemon=True).start()
    return srv


def _dec(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return {"raw": raw[:200]}


def http(method, url, payload=None, timeout=30):
    """返回 (status, dict)。HTTP 错误也是结果，不是异常。"""
    data = None
    hdrs = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
        hdrs["X-AgentOS-Action"] = "1"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, _dec(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, _dec(e.read().decode("utf-8", "replace"))
    except Exception as e:                                   # noqa: BLE001
        return None, {"ok": False, "error": "TRANSPORT %r" % (e,)}


def web_q(url, timeout=30):
    return http("GET", "http://127.0.0.1:%d/__web?url=%s"
                % (PORT[0], urllib.parse.quote(url, safe="")), timeout=timeout)


def fx(path):
    return "http://127.0.0.1:%d%s" % (FIX[0], path)


def start_fixture():
    """上游夹具：正常 JSON / 302 / 超限文本 / 二进制。"""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    BIG = 500000                      # > webface.MAX_BYTES(393216)，逼出 truncated

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self):                                    # noqa: N802
            p = self.path.split("?")[0]
            if p == "/redir":
                self._send(302, b"", "text/plain",
                           {"Location": "http://evil.example.com/"})
            elif p == "/big.txt":
                self._send(200, b"x" * BIG, "text/plain; charset=utf-8")
            elif p == "/blob.bin":
                self._send(200, bytes(bytearray(range(256))) * 4,
                           "application/octet-stream")
            else:
                self._send(200, b'{"hello":"agentos","n":5}',
                           "application/json")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2},
                     daemon=True).start()
    FIX[0] = srv.server_address[1]
    return srv


# ---------------------------------------------------------------- 后端判据
def c_self_health():
    st, j = web_q("http://127.0.0.1:%d/__health" % PORT[0])
    return (st == 200 and j.get("ok") and j.get("status") == 200
            and str(j.get("content_type", "")).startswith("application/json")
            and (j.get("bytes") or 0) > 20 and not j.get("truncated")
            and '"code_sha"' in (j.get("text") or "")), \
        "HTTP=%r ok=%r status=%r ctype=%r bytes=%r trunc=%r" % (
            st, j.get("ok"), j.get("status"), j.get("content_type"),
            j.get("bytes"), j.get("truncated"))


def c_self_index():
    st, j = web_q("http://127.0.0.1:%d/index.html" % PORT[0])
    return (st == 200 and j.get("ok") and j.get("status") == 200
            and str(j.get("content_type", "")).startswith("text/html")
            and "AgentOS" in (j.get("text") or "")
            and "<html" in (j.get("text") or "").lower()), \
        "HTTP=%r ok=%r status=%r ctype=%r head=%r" % (
            st, j.get("ok"), j.get("status"), j.get("content_type"),
            (j.get("text") or "")[:60])


def c_evil_host():
    st, j = web_q("http://evil.example.com/")
    return (st == 200 and j.get("ok") is False
            and "只接回环地址" in (j.get("error") or "")
            and "text" not in j), \
        "HTTP=%r ok=%r err=%r" % (st, j.get("ok"), (j.get("error") or "")[:110])


def c_evil_https():
    st, j = web_q("https://www.google.com/")
    return (st == 200 and j.get("ok") is False
            and "只接回环地址" in (j.get("error") or "")), \
        "HTTP=%r ok=%r err=%r" % (st, j.get("ok"), (j.get("error") or "")[:110])


def c_not_loopback_ip():
    """127.0.0.2 是回环段但不在白名单里 —— 这一条防的是「白名单写成只判 127.0.0.1
    前缀」的写法（那样 127.0.0.2、127.0.0.99 全被放行）。"""
    st, j = web_q("http://127.0.0.2:%d/index.html" % PORT[0])
    return (st == 200 and j.get("ok") is False
            and "只接回环地址" in (j.get("error") or "")), \
        "HTTP=%r ok=%r err=%r" % (st, j.get("ok"), (j.get("error") or "")[:110])


def c_creds():
    st, j = web_q("http://user:pw@127.0.0.1:%d/index.html" % PORT[0])
    return (st == 200 and j.get("ok") is False
            and "凭据" in (j.get("error") or "")), \
        "HTTP=%r ok=%r err=%r" % (st, j.get("ok"), (j.get("error") or "")[:110])


def c_scheme():
    st, j = web_q("file:///etc/passwd")
    return (st == 200 and j.get("ok") is False
            and "只接 http/https" in (j.get("error") or "")), \
        "HTTP=%r ok=%r err=%r" % (st, j.get("ok"), (j.get("error") or "")[:110])


def c_empty():
    st, j = web_q("")
    return (st == 200 and j.get("ok") is False and "空 URL" in (j.get("error") or "")), \
        "HTTP=%r ok=%r err=%r" % (st, j.get("ok"), (j.get("error") or "")[:110])


def c_toolong():
    st, j = web_q("http://127.0.0.1:8790/" + "a" * 1200)
    return (st == 200 and j.get("ok") is False
            and "太长" in (j.get("error") or "")), \
        "HTTP=%r ok=%r err=%r" % (st, j.get("ok"), (j.get("error") or "")[:110])


def c_dead_port():
    """拒连必须原话上报，不能变成「页面为空」。"""
    st, j = web_q("http://127.0.0.1:%d/index.html" % DEAD[0])
    err = j.get("error") or ""
    return (st == 200 and j.get("ok") is False and "Connection" in err
            and "text" not in j), \
        "HTTP=%r ok=%r err=%r" % (st, j.get("ok"), err[:130])


def c_redirect():
    st, j = web_q(fx("/redir"))
    err = j.get("error") or ""
    return (st == 200 and j.get("ok") is False and "重定向" in err
            and "evil.example.com" in err and "text" not in j), \
        "HTTP=%r ok=%r err=%r" % (st, j.get("ok"), err[:130])


def c_truncated():
    st, j = web_q(fx("/big.txt"))
    return (st == 200 and j.get("ok") and j.get("truncated") is True
            and j.get("bytes") == 393216 and j.get("limit") == 393216
            and len(j.get("text") or "") == 393216), \
        "HTTP=%r ok=%r trunc=%r bytes=%r limit=%r len=%r" % (
            st, j.get("ok"), j.get("truncated"), j.get("bytes"),
            j.get("limit"), len(j.get("text") or ""))


def c_binary():
    st, j = web_q(fx("/blob.bin"))
    err = j.get("error") or ""
    return (st == 200 and j.get("ok") is False and "不是文本" in err
            and "text" not in j), \
        "HTTP=%r ok=%r err=%r" % (st, j.get("ok"), err[:130])


def c_fixture_json():
    """夹具自己的正常响应也走一遍同一条路（防「只有拒绝路径是活的」）。"""
    st, j = web_q(fx("/api"))
    return (st == 200 and j.get("ok") and j.get("status") == 200
            and '"agentos"' in (j.get("text") or "")), \
        "HTTP=%r ok=%r status=%r text=%r" % (
            st, j.get("ok"), j.get("status"), (j.get("text") or "")[:80])


BACKEND = [
    ("取回 面能取到本机 JSON", c_self_health),
    ("取回 面能取到本机 HTML", c_self_index),
    ("夹具 正常 JSON 通路是活的", c_fixture_json),
    ("边界 非回环 http 被拒", c_evil_host),
    ("边界 非回环 https 被拒", c_evil_https),
    ("边界 回环段但非白名单 IP 被拒", c_not_loopback_ip),
    ("边界 带凭据 URL 被拒", c_creds),
    ("边界 非 http(s) scheme 被拒", c_scheme),
    ("边界 空 URL 被拒", c_empty),
    ("边界 超长 URL 被拒", c_toolong),
    ("诚实 拒连原话上报", c_dead_port),
    ("边界 重定向不跟随", c_redirect),
    ("诚实 超限显式报 truncated", c_truncated),
    ("边界 二进制明确拒", c_binary),
]


# ---------------------------------------------------------------- 静态判据
def c_static_no_follow():
    """webface 里不许出现「跟随重定向」的写法。

    为什么静态查而不是行为查：行为查只能覆盖我们想到的那一个 302。真正要堵的是
    任何人日后往 fetch 里塞一个 urlopen(...)/set_response()/check_hostname=False，
    那些写法在重定向判据前面还是后面都能改变行为。这里把**手段**也判掉。
    """
    src = io.open(WEBFACE_PY, encoding="utf-8").read()
    banned = ["urllib.request.urlopen", "requests.get", "httpx.get",
              "allow_redirects", "HTTPRedirectHandler",
              "response.geturl()", "resp.geturl()"]
    hit = [b for b in banned if b in src]
    # 判据本身必须在盘上（否则上面那句「不许出现」就是空的）
    wired = "REJECT_REDIRECT" in src and "308" in src
    return (not hit and wired), "命中禁止写法=%s redirect判据在盘=%s" % (hit, wired)


def c_static_failclosed():
    """静态：serve.py 必须有 /__web 路由，且 webface 走的是按路径加载 + 失败 stub。

    前者防「路由根本没接」；后者防「有人把裸 import 挪回来」—— 裸 import 在门的
    importlib 加载方式下会 ModuleNotFoundError，现象是这一面整个消失且不报错。
    """
    s = io.open(SERVE_PY, encoding="utf-8").read()
    w = io.open(WEBFACE_PY, encoding="utf-8").read()
    ok = ("self.path.startswith(\"/__web\")" in s
          and "_load_webface" in s
          and "取字节模块 webface.py 加载失败" in s
          and "def fetch(" in w
          and "MAX_BYTES" in w and "TIMEOUT" in w)
    return ok, ("/__web 路由=%s _load_webface=%s stub原话=%s "
                "fetch定义=%s MAX_BYTES=%s TIMEOUT=%s" % (
                    "self.path.startswith(\"/__web\")" in s, "_load_webface" in s,
                    "取字节模块 webface.py 加载失败" in s, "def fetch(" in w,
                    "MAX_BYTES" in w, "TIMEOUT" in w))


def c_frontend_wired():
    """前端接线静态：五面落位 + 表单 + iframe 契约 + wsWeb 在 wsInit 里被接上。"""
    src = io.open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
    need = ['id="t-ws"', 'id="v-ws"', "ws:'v-ws'", "ws:'t-ws'",
            'id="wswebform"', 'id="wswebu"', 'id="wswebstat"', 'id="wsprev"',
            'class="wsp ws-web"', 'class="wsp ws-prev"',
            "function wsWeb(", "async function wsWeb("]
    miss = [n for n in need if src.count(n) != 1]
    # 预览面 iframe 必须无 allow-same-origin、必须有 no-referrer，且不能有 src=
    fr = re.search(r'<iframe[^>]*id="wsprev"[^>]*>', src, re.S)
    if not fr:
        miss.append("<iframe id=wsprev>")
    else:
        tag = fr.group(0)
        if "allow-same-origin" in tag:
            miss.append("iframe 含 allow-same-origin")
        if 'referrerpolicy="no-referrer"' not in tag:
            miss.append("iframe 缺 referrerpolicy")
        if re.search(r'\bsrc=', tag):
            miss.append("iframe 写了 src=")
    # 取字节按钮必须是 type=button（否则点一次触发 click+submit 两次，第二次读空 input）
    go = re.search(r'<button[^>]*id="wswebgo"[^>]*>', src)
    if not go or 'type="button"' not in go.group(0):
        miss.append("取字节按钮缺 type=button")
    # wsWeb 必须真被接上（两处：submit 与 click），且在 wsInit 里
    n_wsweb = len(re.findall(r"wsWeb\(u\.value\)", src))
    if n_wsweb != 2:
        miss.append("wsWeb 接线 %d 处（应 2）" % n_wsweb)
    boot_at = src.find("function boot()")
    wsinit_at = src.find("function wsInit()")
    # 从 boot() 往后找调用点：wsInit 的**定义**排在 boot 之后（文件按功能往后追加），
    # 从定义处往后找只会得到 -1，然后把正确的接线报成红。
    called = src.find("wsInit();", boot_at)
    in_boot = 0 <= boot_at <= called < src.find("</script>", boot_at)
    if not in_boot:
        miss.append("wsInit() 不在 boot 内")
    return (not miss), "缺=%s" % (miss,)


STATIC = [
    ("静态 webface 无跟随重定向写法且判据在盘", c_static_no_follow),
    ("静态 /__web 路由 + 按路径加载 + 失败 stub", c_static_failclosed),
    ("静态 前端五面落位/iframe 契约/wsWeb 接线", c_frontend_wired),
]


# ------------------------------------------------------------ 真浏览器判据
READ_WEB_JS = (
    "() => { const q=s=>document.querySelector(s);"
    " const t=e=>(e?(e.innerText||e.textContent||''):'');"
    " const vis=id=>{const e=q(id); if(!e) return false;"
    " const r=e.getBoundingClientRect(); return r.width>4 && r.height>4;};"
    " const fr=q('#wsprev');"
    " const cx=t(q('#cxinst'));"
    " return { panels:[vis('#v-ws .ws-files'),vis('#v-ws .ws-code'),vis('#v-ws .ws-term'),"
    "   vis('#v-ws .ws-web'),vis('#v-ws .ws-prev')],"
    " stat:t(q('#wswebstat')), srcdoc:fr?(fr.getAttribute('srcdoc')||''):null,"
    " src:fr?fr.getAttribute('src'):'NOFRAME',"
    " sandbox:fr?(fr.getAttribute('sandbox')||''):'NOFRAME',"
    " sameorigin:fr?(()=>{try{"
    "   const d=fr.contentDocument;"
    "   return (d&&d.documentElement)?'REACHABLE':'opaque';"
    "   }catch(e){return 'opaque:'+e.name;}})():'NOFRAME',"
    " cxinst:cx.slice(0,400), errs:window.__wsErrs||[] }; }"
)


def _killer(anchor, repl, out):
    def fn(route):
        resp = route.fetch()
        body = resp.body().decode("utf-8", "replace")
        if anchor not in body:
            out.setdefault("anchorMiss", True)
            route.fulfill(response=resp)
            return
        out["anchorHit"] = out.get("anchorHit", 0) + 1
        route.fulfill(response=resp,
                      body=body.replace(anchor, repl, 1).encode("utf-8"))
    return fn


def _blank(err, out=None):
    """浏览器没跑起来时的占位结果。

    必须把调用方的 out 带回来（anchorHit / anchorMiss）：浏览器挂掉和「变异字节
    没进 served 响应」是两回事。丢掉 anchorHit 会把一个纯 JS 语法错的变异体误报成
    「变异没生效」，也就是把门自己的病说成产品没病。
    """
    b = {"panels": [False] * 5, "stat": "", "srcdoc": None, "src": "NOFRAME",
         "sandbox": "NOFRAME", "sameorigin": "NOFRAME", "cxinst": "", "errs": [err]}
    r = {"ok": {"errs": [err]}, "evil": dict(b), "nohealth": dict(b),
         "bad": [("BLANK", str(err)[:120])], "errs": [err]}
    if out:
        r["anchorHit"] = out.get("anchorHit", 0)
        r["anchorMiss"] = bool(out.get("anchorMiss"))
    return r


def run_browser(port, anchor=None, repl=None):
    """真 Chromium 走一遍浏览器/预览面；第二页把 /__health 掐掉验右栏诚实降级。"""
    from playwright.sync_api import sync_playwright
    out = {"errs": [], "bad": []}
    with sync_playwright() as pw:
        br = pw.chromium.launch(executable_path=CHROME)
        try:
            ctx = br.new_context(viewport={"width": 1440, "height": 900})
            # 变异必须挂在 context 上而不是 pg 上：右栏诚实降级那一条是在**第二页**
            # p2 上验的（p2 把 /__health 掐掉）。挂在 pg 上只有首页拿得到变异体，
            # p2 读的仍是好代码，于是 W6 这种「右栏编假值」的变异结构上抓不住 ——
            # 门看起来有上牙，其实那牙咬不到它该咬的那条路径。
            if anchor is not None:
                ctx.route("**/index.html", _killer(anchor, repl, out))
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: out["errs"].append(str(e)))
            # /favicon.ico 是浏览器自己发的那一次请求（页面从没声明 favicon），
            # 它 404 不是应用缺陷；混进「无 JS 报错」会让这条判据永远红。
            # 所以拆成两条：页面自己报的错 0 条；非 favicon 的 >=400 响应 0 个。
            pg.on("console", lambda m: out["errs"].append("console:" + m.text)
                  if (m.type == "error"
                      and "favicon.ico" not in ((m.location or {}).get("url") or ""))
                  else None)
            pg.on("response", lambda r: out["bad"].append((r.status, r.url))
                  if r.status >= 400 and "favicon" not in r.url else None)
            pg.goto("http://127.0.0.1:%d/index.html" % port, wait_until="load")
            pg.click("#t-ws")
            pg.wait_for_timeout(800)
            pg.fill("#wswebu", "http://127.0.0.1:%d/__health" % port)
            pg.click("#wswebgo")
            pg.wait_for_timeout(1200)
            out["ok"] = pg.evaluate(READ_WEB_JS)
            out["ok"]["errs"] = list(out["errs"])
            pg.fill("#wswebu", "http://evil.example.com/")
            pg.click("#wswebgo")
            pg.wait_for_timeout(1200)
            out["evil"] = pg.evaluate(READ_WEB_JS)
            # 第二页：掐掉 /__health，右栏必须整栏「不可判定」而不是编一个 clients
            p2 = ctx.new_page()
            p2.route("**/__health", lambda r: r.abort())
            p2.on("pageerror", lambda e: out["errs"].append("p2:" + str(e)))
            p2.goto("http://127.0.0.1:%d/index.html" % port, wait_until="load")
            p2.click("#t-ws")
            p2.wait_for_timeout(1800)
            out["nohealth"] = p2.evaluate(READ_WEB_JS)
        except Exception as e:                                # noqa: BLE001
            return _blank("浏览器跑不起来：%r" % (e,), out)
        finally:
            try:
                br.close()
            except Exception:                                 # noqa: BLE001
                pass
    out["anchorHit"] = out.get("anchorHit", 0)
    out["anchorMiss"] = bool(out.get("anchorMiss"))
    return out


def check_browser(res):
    """返回 [(name, ok, detail)]；真浏览器判据。"""
    o = []
    k = res.get("ok") or {}
    errs = k.get("errs") or []
    bad = res.get("bad") or []
    o.append(("浏览器 无 JS 报错", not errs, repr(errs[:2])))
    o.append(("浏览器 无非 favicon 的 >=400 响应", not bad, repr(bad[:3])))
    o.append(("前端 五面都真渲染（非零尺寸）",
              bool(k.get("panels")) and all(k.get("panels") or []),
              "panels=%r" % (k.get("panels"),)))
    stat = k.get("stat") or ""
    o.append(("前端 取回本机 URL 显示 200 + 字节数 + 类型",
              "200" in stat and "application/json" in stat and "B ·" in stat,
              "stat=%r" % stat[:150]))
    o.append(("前端 取回内容进 sandbox srcdoc",
              bool(k.get("srcdoc")) and '"code_sha"' in (k.get("srcdoc") or ""),
              "srcdoc 长度=%r" % len(k.get("srcdoc") or "")))
    o.append(("前端 iframe 无 src 属性（不借 src 给同源）",
              k.get("src") in (None, "NOFRAME"), "src=%r" % (k.get("src"),)))
    o.append(("前端 iframe sandbox 无 allow-same-origin 且父页读不到内容",
              k.get("sandbox") not in (None, "NOFRAME")
              and "allow-same-origin" not in (k.get("sandbox") or "")
              and str(k.get("sameorigin")).startswith("opaque"),
              "sandbox=%r sameorigin=%r" % (k.get("sandbox"), k.get("sameorigin"))))
    e = res.get("evil") or {}
    estat = e.get("stat") or ""
    o.append(("前端 非回环 URL 显示服务端原话（不谎报成功）",
              "只接回环地址" in estat and "evil.example.com" in estat,
              "stat=%r" % estat[:150]))
    n = res.get("nohealth") or {}
    ncx = n.get("cxinst") or ""
    o.append(("前端 /__health 掐掉时右栏整栏不可判定",
              "不可判定" in ncx and "clients" in ncx and "deadbeef" not in ncx,
              "cxinst=%r" % ncx[:150]))
    return o


# ---------------------------------------------------------------- 变异体
# (tag, 说明, 文件, 锚点, 替换, kind)
# 锚点必须在被测文件里**恰好命中一次**。命中 0 次说明源码漂移、门自己失效了 ——
# 那比红更坏，所以直接判红而不是「跳过」。
W1 = ("W1", "回环 host 判据改成把 host 改写成本机（放行而非拒）",
      "webface", "    if host not in HOSTS:\n        return None, REJECT_HOST % host[:60]",
      "    if host not in HOSTS:\n        return s.replace(host, \"127.0.0.1\", 1), None",
      "backend")
W2 = ("W2", "关掉重定向判据（一跟就跟出去）",
      "webface", "        if resp.status in (301, 302, 303, 307, 308) and loc:",
      "        if False:", "backend")
W3 = ("W3", "truncated 写死 False（假装读全了）",
      "webface", "        trunc = len(raw_bytes) > MAX_BYTES",
      "        trunc = False", "backend")
W4 = ("W4", "关掉文本类型判据（二进制也塞进预览）",
      "webface", "        if not ctype.startswith(TEXT_TYPES):",
      "        if False:", "backend")
SV = ("SV", "serve.py 里 webface 路径指到不存在的文件（加载必失败）",
      "serve", '    path = os.path.join(HERE, "webface.py")',
      '    path = os.path.join(HERE, "_webmut_missing_webface.py")', "backend")
W5 = ("W5", "摘掉 srcdoc 赋值（取回了字节但不渲染）",
      "index", "  if(j.text!=null) fr.setAttribute('srcdoc',j.text);",
      # 替换成 `void j.text;` 会留下一个孤立 else（下一行是 else 分支）→ JS 语法错
      # → 整页死掉。那样这条变异是被「浏览器报错」抓住的，不是被「srcdoc 没被设上」
      # 抓住的 —— 后者才是这条上牙真正要证明的能力。改成同结构的空 if：
      # 取回照走、页面照活，只是预览面静默不渲染。
      "  if(j.text!=null);", "browser")
W6 = ("W6", "右栏「/__health 读不到」分支写死假值（编一个 clients/code_sha）",
      "index", "    cxRows($('#cxinst'),[['clients','不可判定','dim'],['code_sha','不可判定','dim'],",
      "    cxRows($('#cxinst'),[['clients','1','dim'],['code_sha','deadbeef','dim'],", "browser")
MUTANTS = [W1, W2, W3, W4, SV, W5, W6]

SRC = {"webface": WEBFACE_PY, "serve": SERVE_PY, "index": LIVE["index.html"]}


def tmp_name(tag, which):
    return "_webmut_%s_%s.py" % (tag, which)


def write_mutant(tag, which, anchor, repl):
    """写一份临时副本并返回路径；锚点命中数不等于 1 就拒绝（门失能，不是跳过）。"""
    src = io.open(SRC[which], encoding="utf-8").read()
    n = src.count(anchor)
    if n != 1:
        return None, "%s 里锚点命中 %d 次（必须恰好 1 次）" % (which, n)
    out = os.path.join(HERE, tmp_name(tag, which))
    with io.open(out, "w", encoding="utf-8") as f:
        f.write(src.replace(anchor, repl, 1))
    return out, ""


def build_backend_mutant(tag, which, anchor, repl):
    """后端变异体要两份：webface 副本 + 指向它的 serve.py 副本。

    serve.py 按 `os.path.join(HERE, "webface.py")` 取字节模块，所以只改 webface 副本
    是无效变异（serve 仍加载活文件）。这里把 serve 副本里那行指到 webface 副本 ——
    SV 变异体恰好就是「指到一个不存在的文件」，两者共用同一条改法。
    """
    made = []
    if which == "webface":
        wp, werr = write_mutant(tag, "webface", anchor, repl)
        if not wp:
            return None, werr
        made.append(wp)
        sp, serr = write_mutant(
            tag, "serve", '    path = os.path.join(HERE, "webface.py")',
            '    path = os.path.join(HERE, "%s")' % os.path.basename(wp))
        if not sp:
            return None, serr
        made.append(sp)
        return sp, ""
    sp, serr = write_mutant(tag, which, anchor, repl)
    if not sp:
        return None, serr
    made.append(sp)
    return sp, ""


def cleanup_tmp():
    for f in sorted(os.listdir(HERE)):
        if f.startswith("_webmut_"):
            p = os.path.join(HERE, f)
            assert p.startswith(HERE + os.sep) and p.endswith(".py"), p
            os.unlink(p)


def env_check():
    miss = [n for n, p in (("Chromium", CHROME), ("serve.py", SERVE_PY),
                           ("webface.py", WEBFACE_PY),
                           ("index.html", LIVE["index.html"]))
            if not os.path.exists(p)]
    try:
        import playwright.sync_api  # noqa: F401
    except Exception as e:                                    # noqa: BLE001
        miss.append("playwright(%r)" % (e,))
    return (not miss), miss


def report(rc):
    names = list(G.keys())
    red = [n for n in names if not G[n]]
    path = os.path.join(WORK, "web_gate.json")
    out = {"gate": "web_gate", "rc": rc, "checks": len(names),
           "green": len(names) - len(red), "red": red, "why": WHY}
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
    for n in names:
        line = "  [%s] %s" % ("绿" if G[n] else "红", n)
        if not G[n]:
            line += "  <- " + str(WHY.get(n, ""))
        print(line)
    print("  门判据 %d 项，红 %d 项 -> %s" % (len(names), len(red), path))
    tail = {0: "== D88 web_gate OK ==",
            1: "== D88 web_gate FAIL ==",
            2: "== D88 web_gate ENV-UNAVAILABLE =="}[rc]
    print(tail)
    return rc


def main():
    ok, miss = env_check()
    if not ok:
        print("  环境不可用：%s" % miss)
        return report(2)
    os.makedirs(WORK, exist_ok=True)
    cleanup_tmp()                       # 上一轮崩在中途留下的副本先清干净
    sha_b = {nm: stat_fp(p) for nm, p in LIVE.items()}
    srv = fixsrv = None
    try:
        fixsrv = start_fixture()
        DEAD[0] = free_port()           # 端口已释放 → 拒连，不占端口
        PORT[0] = free_port()
        srv = start_server(PORT[0], load_mod(SERVE_PY, "_d88_web_serve_live"))
        base_backend = {}
        for name, fn in BACKEND:
            try:
                cok, d = fn()
            except Exception as e:                            # noqa: BLE001
                cok, d = False, "EXC %r" % (e,)
            base_backend[name] = cok
            add("正例 " + name, cok, d)
        base_static = {}
        for name, fn in STATIC:
            try:
                sok, d = fn()
            except Exception as e:                            # noqa: BLE001
                sok, d = False, "EXC %r" % (e,)
            base_static[name] = sok
            add("正例 " + name, sok, d)
        res = run_browser(PORT[0])
        base_browser = {}
        for name, v, d in check_browser(res):
            base_browser[name] = v
            add("正例 " + name, v, d)
        srv.shutdown()
        srv = None

        for tag, why, which, anchor, repl, kind in MUTANTS:
            if kind == "backend":
                mpath, werr = build_backend_mutant(tag, which, anchor, repl)
                add("上牙 %s 锚点唯一（%s）" % (tag, why), bool(mpath), werr)
                if not mpath:
                    continue
                msrv = None
                caught = []
                try:
                    msrv = start_server(free_port(),
                                        load_mod(mpath, "_d88_web_mut_%s" % tag))
                    PORT[0] = msrv.server_address[1]
                    for name, fn in BACKEND:
                        try:
                            v, d = fn()
                        except Exception as e:                # noqa: BLE001
                            v, d = False, "EXC %r" % (e,)
                        if base_backend.get(name) and not v:
                            caught.append("%s <- %s" % (name, str(d)[:120]))
                    for name, fn in STATIC:
                        try:
                            v, d = fn()
                        except Exception as e:                # noqa: BLE001
                            v, d = False, "EXC %r" % (e,)
                        # 静态判据读的是活文件，变异副本不影响它 —— 只在变异体确实
                        # 改了对应文件时才参与比对，否则会把静态判据的红算成被抓。
                        if which in ("webface", "serve") and base_static.get(name) and not v:
                            caught.append("%s <- %s" % (name, str(d)[:120]))
                    add("上牙 %s 被抓住（%s）" % (tag, why), bool(caught),
                        "变异后相关判据仍全绿 = 门失能")
                    for c in caught:
                        print("       %s 抓住于 %s" % (tag, c))
                except Exception as e:                        # noqa: BLE001
                    add("上牙 %s 被抓住（%s）" % (tag, why), False,
                        "变异体直接崩了：%r" % (e,))
                finally:
                    if msrv is not None:
                        msrv.shutdown()
            else:
                PORT[0] = free_port()
                msrv = start_server(PORT[0], load_mod(SERVE_PY, "_d88_web_mut_%s" % tag))
                try:
                    mres = run_browser(PORT[0], anchor=anchor, repl=repl)
                    caught = []
                    for name, v, d in check_browser(mres):
                        if base_browser.get(name) and not v:
                            caught.append("%s <- %s" % (name, str(d)[:120]))
                    add("上牙 %s 锚点真被替换（%s）" % (tag, why),
                        bool(mres.get("anchorHit")), "变异字节没进 served 响应")
                    add("上牙 %s 被抓住（%s）" % (tag, why), bool(caught),
                        "变异后前端判据仍全绿 = 门失能")
                    for c in caught:
                        print("       %s 抓住于 %s" % (tag, c))
                finally:
                    msrv.shutdown()
    finally:
        PORT[0] = 0
        for s in (srv, fixsrv):
            if s is not None:
                s.shutdown()
        cleanup_tmp()
        for nm, path in LIVE.items():
            fp = stat_fp(path)
            add("零写入 %s 本轮字节未变" % nm, fp[0] == sha_b[nm][0],
                drift_why(nm, sha_b[nm], fp, "_webmut_"))
        leftover = [f for f in os.listdir(HERE) if f.startswith("_webmut_")]
        add("零写入 tools/ 下无残留变异副本", not leftover, repr(leftover))
    return report(1 if [n for n in G if not G[n]] else 0)


if __name__ == "__main__":
    sys.exit(main())
