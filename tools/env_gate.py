#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D88 真实环境联动门：工作区三面（文件 / 代码 / 终端）不是壳，是真端点。

为什么必须单独一道门（view_dod / resident_gate / layout_gate 都判不到的缺陷）：
    * D87 之前，这个项目的真实形态是「一屏漂亮数据 + 死端点」：/__act、/__goal
      都能跑，但 serve.py 里没有任何文件 / 代码 / 终端端点，左侧也没有工作区，
      用户想「自己看一眼项目里到底有什么」只能去开 Finder。目标原文写的是
      「终端/浏览器/文件/代码/预览等真实环境联动」，这条在那时是**未实现**，
      而且它不会自己变红 —— 后端全绿、屏上全绿，只是少了一整个面；
    * 更坏的一种形状是「端点活着但接不上」：后端有 /__fs，index.html 里 0 命中。
      retro_gate 当初就是这个形状（数据管线通了、只缺渲染），所以本门**同时**
      断言后端行为与前端接线，两头缺一头都判红。

判据分层（刻意不复刻被测对象）：
    * 沙箱边界 / 白名单 / 截断 / rc 全部走**真 HTTP**，打到 serve.py 起在
      临时端口上的真实例，不 import 内部函数走后门 —— 走函数会漏掉路由层
      自己的解析与状态码（例如 CSRF 头漏在 Handler 而不在 term_run）；
    * 前端接线另起真 Chromium，点 tab、点文件、点命令按钮，看屏上是否真有反应。
      纯文本 grep 判不出「按钮在但点了没反应」，那正是 wsInit 漏接线的形状。

上牙（六个变异体；前五个改活的 serve.py 副本，第六个改 served 的 index.html 字节）：
   M1 关掉 realpath 前缀判据      -> 越界读与越界符号链接都必须被读到，门必须红；
   M2 白名单放宽成任意命令(env)   -> 白名单外命令被放行，门必须红；
   M3 把 returncode 写死 0        -> 非零命令被谎报成功，门必须红；
   M4 终端 truncated 写死 False   -> 超长输出被假装完整，门必须红；
   M5 代码面 truncated 写死 False -> 大文件被假装读全，门必须红；
   M6 摘掉常用命令按钮的监听      -> 按钮在、点了没反应，门必须红。
   变异体只改 tools/_envmut_<tag>.py 这类临时副本与 served 字节，
   活的 serve.py / index.html 零写入，跑完 sha256 自证。

这一版的第一稿自己漏了一条判据，被 D88 门当场抓出来：白名单里 wc/head/tail
  三条指向 macOS 上**根本不存在**的 /bin/wc、/bin/head、/bin/tail —— 后端全绿、
  路由全对，屏上点「wc -l index.html」却回「命令起不来」。所以现在判据里有一条是
  「白名单里每一条的可执行文件都必须真实存在」，并且真 HTTP 跑一遍 wc/head/tail。
  门自己漏判据这件事也是门的一部分：它必须能抓住下一条同类缺陷，而不是只覆盖
  自己想到的那几种。

安全红线（刻意的设计约束，不是遗漏）：
    * 白名单外命令的**活体探针用 touch，不是 rm / mv / cp / curl**。M2 会让
      白名单彻底失效，若探针是 `rm -rf /`，变异体就会真的把它跑起来 ——
      门不能为了证明自己抓得住变异体而去执行破坏性命令。
    * `rm` 的拒绝另有静态判据：TERM_CMDS 里没有写类命令，且错误文案把白名单
      原样列出来。两头都判，且都不需要真的跑 rm。

用法：python3 tools/env_gate.py   # rc=0 全过 / 1 有红项 / 2 环境不可用
"""
import hashlib
import importlib.util
import io
import json
import os
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
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
WORK = os.path.join(ROOT, "work")

BIG = os.path.join(WORK, "env_gate_big.txt")           # 300KB 文本，用来逼出截断
LINK = os.path.join(WORK, "env_gate_link")            # 指向 ROOT 外，验 realpath 判据
NEVER = os.path.join(WORK, "env_gate_never_created")  # 必须始终不存在

G = {}
WHY = {}
PORT = [0]


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

    为什么不能只比 sha：只比 sha 时，「本轮中途有人从门外改了活文件」会被
    本门归因成「门自己写了活文件」。2026-10-10 14:42 实测踩实：外部管线在
    同一窗口改 tools/serve.py，本门判红并自认写了活文件，而同一条链单独重跑
    35/35 全绿。判据不放松（字节变了就是红），但归因必须能自证清白。
    """
    st = os.stat(path)
    return (sha256_file(path), st.st_size, int(st.st_mtime))


def drift_why(nm, base, now, prefix):
    """零写入判红时的归因细节；判绿返回空串（不进 WHY）。

    带上基线与现测的 size/mtime/sha 前缀，让「谁在本轮中途写了活文件」可以
    拿去和外部写入者的落地时间对账；同时点名本门变异副本只落在
    tools/<prefix>* 命名空间，配合残留自证证明写入者在门外。
    """
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


def load_serve(path, tag):
    spec = importlib.util.spec_from_file_location(tag, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def start_server(port, mod):
    from http.server import ThreadingHTTPServer
    # serve.py 的 Handler 每条请求都往 stderr 打一行；门里几十个请求会把
    # ConnectionReset 之类噪声刷满输出，淹掉真正的红项。这里只在门自己的
    # 实例上静音，被测行为一字未改。
    quiet = type("QuietHandler", (mod.Handler,),
                 {"log_message": lambda self, *a, **k: None})

    class QuietSrv(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            # 浏览器关页面时连接被 reset 是正常收尾，不是被测缺陷；默认实现
            # 会把整条 traceback 打到 stderr，把真正的红项淹掉。
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
    """返回 (status, dict)。HTTP 错误也是结果，不是异常 —— 403 是判据之一。"""
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


def fs_q(path, op="list", **kw):
    q = {"op": op, "path": path}
    q.update({k: str(v) for k, v in kw.items()})
    return http("GET", "http://127.0.0.1:%d/__fs?%s"
                % (PORT[0], urllib.parse.urlencode(q)))


def term_q(argv):
    return http("POST", "http://127.0.0.1:%d/__term" % PORT[0], {"argv": argv})


# ---------------------------------------------------------------- 后端判据
def c_root_list():
    st, j = fs_q("")
    names = [e.get("name") for e in (j.get("entries") or [])] if j.get("ok") else []
    return (st == 200 and j.get("ok") and "index.html" in names and "tools" in names), \
        "HTTP=%r ok=%r n=%d" % (st, j.get("ok"), len(names))


def c_subdir():
    st, j = fs_q("tools")
    names = [e.get("name") for e in (j.get("entries") or [])] if j.get("ok") else []
    return (st == 200 and "serve.py" in names and "env_gate.py" in names), \
        "HTTP=%r names=%s" % (st, names[:6])


def c_escape_rel():
    st, j = fs_q("../../../../etc/passwd", op="read")
    err = str(j.get("error") or "")
    leaked = "root:" in json.dumps(j, ensure_ascii=False)
    return (not j.get("ok") and "越界" in err and not leaked), \
        "HTTP=%r err=%r 泄漏=%s" % (st, err[:90], leaked)


def c_escape_symlink():
    st, j = fs_q(os.path.relpath(LINK, ROOT), op="read")
    err = str(j.get("error") or "")
    leaked = "root:" in json.dumps(j, ensure_ascii=False)
    return (not j.get("ok") and "越界" in err and not leaked), \
        "HTTP=%r err=%r 泄漏=%s" % (st, err[:90], leaked)


def c_truncated():
    st, j = fs_q(os.path.relpath(BIG, ROOT), op="read")
    if not j.get("ok"):
        return False, "HTTP=%r err=%r" % (st, str(j.get("error"))[:90])
    limit = j.get("limit")
    return (j.get("truncated") is True and isinstance(limit, int) and limit > 0
            and len(j.get("text") or "") == limit
            and j.get("size", 0) > limit), \
        "truncated=%r limit=%r len=%r size=%r" % (
            j.get("truncated"), limit, len(j.get("text") or ""), j.get("size"))


def c_binary():
    shots = os.path.join(ROOT, "docs", "UX-SHOTS", "views")
    try:
        png = sorted(f for f in os.listdir(shots) if f.endswith(".png"))[0]
    except (OSError, IndexError):
        return False, "找不到 shots 里的 PNG，二进制判据没法验"
    st, j = fs_q(os.path.relpath(os.path.join(shots, png), ROOT), op="read")
    return (not j.get("ok") and j.get("binary") is True), \
        "HTTP=%r ok=%r binary=%r" % (st, j.get("ok"), j.get("binary"))


def c_read_dir():
    st, j = fs_q("tools", op="read")
    err = str(j.get("error") or "")
    return (not j.get("ok") and "目录" in err), "HTTP=%r err=%r" % (st, err[:90])


def c_term_needs_csrf():
    url = "http://127.0.0.1:%d/__term" % PORT[0]
    req = urllib.request.Request(
        url, data=json.dumps({"argv": ["pwd"]}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            st, j = r.status, _dec(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        st, j = e.code, _dec(e.read().decode("utf-8", "replace"))
    return (st == 403 and "X-AgentOS-Action" in str(j.get("error") or "")), \
        "HTTP=%r err=%r" % (st, str(j.get("error"))[:90])


def c_term_whitelist():
    st, j = term_q(["touch", os.path.relpath(NEVER, ROOT)])
    err = str(j.get("error") or "")
    made = os.path.exists(NEVER)
    return (not j.get("ok") and "白名单" in err and not made), \
        "HTTP=%r err=%r touch 真被执行=%s" % (st, err[:70], made)


def c_term_nonzero_rc():
    st, j = term_q(["python3", "-c", "import sys;sys.exit(3)"])
    return (j.get("ok") is True and j.get("rc") == 3), \
        "HTTP=%r ok=%r rc=%r（终端不许替命令编造成功）" % (st, j.get("ok"), j.get("rc"))


def c_term_real_out():
    st, j = term_q(["ls", "-1", "tools"])
    out = str(j.get("out") or "")
    return (j.get("ok") is True and j.get("rc") == 0 and "serve.py" in out), \
        "HTTP=%r rc=%r out=%r" % (st, j.get("rc"), out[:60])


def c_term_cwd():
    st, j = term_q(["pwd"])
    out = str(j.get("out") or "").strip()
    return (j.get("ok") is True and out == os.path.realpath(ROOT)), \
        "out=%r 期望=%r" % (out[:80], os.path.realpath(ROOT))


def c_term_textcmds():
    """wc / head / tail 三条必须真能跑。

    这是 D88 真缺陷的现场判据：它们原先指向 macOS 上不存在的 /bin/wc，
    路由与白名单逻辑都正确，只有真跑一次才看得到「命令起不来」。
    """
    bad = []
    for argv, want in ((["wc", "-l", "index.html"], "index.html"),
                       (["head", "-3", "index.html"], "<"),
                       (["tail", "-2", "index.html"], "</")):
        st, j = term_q(argv)
        out = str(j.get("out") or "")
        if not (j.get("ok") is True and j.get("rc") == 0 and want in out):
            bad.append("%s -> HTTP=%r rc=%r out=%r"
                       % (" ".join(argv), st, j.get("rc"), out[:50]))
    return (not bad), "; ".join(bad) or "wc/head/tail 三条都真跑通"


def c_term_truncated():
    """终端输出超上限必须显式告知 truncated，不许静默截断。"""
    st, j = term_q(["python3", "-c", "print('y' * 40000)"])
    out = str(j.get("out") or "")
    return (j.get("ok") is True and j.get("rc") == 0 and j.get("truncated") is True
            and len(out) < 40000), \
        "HTTP=%r rc=%r truncated=%r len=%d" % (st, j.get("rc"), j.get("truncated"), len(out))


def c_whitelist_static(mod):
    cmds = getattr(mod, "TERM_CMDS", {}) or {}
    danger = {"rm", "mv", "cp", "chmod", "chown", "curl", "wget", "ssh", "scp",
              "dd", "mkfifo", "shutdown", "reboot", "kill", "killall", "sudo",
              "open", "sh", "bash", "zsh", "eval", "xargs"}
    bad = sorted(danger & set(cmds))
    gone = sorted(k for k, v in cmds.items() if not os.path.exists(v[0]))
    return (not bad and not gone and len(cmds) >= 8), \
        "TERM_CMDS=%s 命中危险=%s 路径不存在=%s" % (sorted(cmds), bad, gone)


BACKEND = [
    ("文件面 根目录列出条目", c_root_list),
    ("文件面 子目录列出条目", c_subdir),
    ("沙箱 相对路径越界被拒", c_escape_rel),
    ("沙箱 越界符号链接被拒", c_escape_symlink),
    ("代码面 超限显式告知 truncated", c_truncated),
    ("代码面 二进制明确拒", c_binary),
    ("代码面 读目录被拒", c_read_dir),
    ("终端 无 CSRF 头 403", c_term_needs_csrf),
    ("终端 白名单外命令被拒且没被执行", c_term_whitelist),
    ("终端 回真实非零 rc", c_term_nonzero_rc),
    ("终端 真实输出", c_term_real_out),
    ("终端 cwd 收在项目根", c_term_cwd),
    ("终端 wc/head/tail 真能跑", c_term_textcmds),
    ("终端 输出超限显式告知 truncated", c_term_truncated),
]


# ------------------------------------------------------------ 前端接线判据
def c_frontend_wired():
    """静态接线：tab / panel / 两个映射 / wsInit 调用各存在且唯一。

    这一项只证「接上了」，证不了「点了有反应」—— 后者靠真浏览器那几条。
    """
    try:
        src = io.open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
    except OSError as e:                                    # noqa: BLE001
        return False, "读不到 index.html：%r" % (e,)
    need = ['id="t-ws"', 'id="v-ws"', "ws:'v-ws'", "ws:'t-ws'",
            "function wsInit()", "wsInit();"]
    miss = [n for n in need if src.count(n) != 1]
    # 端点串可以多处命中（文件树与代码面各发一次 __fs），只要求存在。
    miss += [e for e in ("'/__fs", "'/__term") if e not in src]
    # wsInit(); 必须落在 boot() 里，而不是只定义不调 —— 「定义了没接线」
    # 正是 retro_gate 那次复发面的形状。
    boot_at = src.find("function boot()")
    called_at = src.find("wsInit();")
    in_boot = -1 <= boot_at <= called_at < src.find("</script>", boot_at)
    return (not miss and in_boot), \
        "缺/多次=%s wsInit 调用在 boot 内=%s" % (miss, in_boot)


READ_WS_JS = (
    "() => { const q = s => document.querySelector(s);"
    " const t = e => (e ? (e.innerText || e.textContent || '') : '');"
    " const vis = id => { const e = q(id); if (!e) return false;"
    " const r = e.getBoundingClientRect(); return r.width > 4 && r.height > 4; };"
    " return { treeN: document.querySelectorAll('#wstree button').length,"
    " treeTxt: t(q('#wstree')).slice(0, 400), path: t(q('#wspath')),"
    " code: t(q('#wscode')).slice(0, 400), term: t(q('#wsterm')).slice(-8000),"
    " panels: [vis('#v-ws .wsp:nth-child(1)'), vis('#v-ws .wsp:nth-child(2)'),"
    " vis('#v-ws .wsp:nth-child(3)')] }; }"
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


def _blank(err):
    blank = {"errs": [err], "panels": [False, False, False], "treeN": 0,
             "treeTxt": "", "path": "", "code": "", "term": ""}
    return {"errs": [err], "anchorHit": 0, "home": blank, "picked": blank,
            "quick": blank, "typed": blank, "pickedName": None}


def run_browser(port, anchor=None, repl=None):
    """真 Chromium 走一遍三面；anchor/repl 非空时拦 served 的 index.html 字节。"""
    from playwright.sync_api import sync_playwright
    out = {"errs": []}
    with sync_playwright() as pw:
        br = pw.chromium.launch(executable_path=CHROME)
        try:
            ctx = br.new_context(viewport={"width": 1440, "height": 900})
            pg = ctx.new_page()
            if anchor is not None:
                pg.route("**/index.html", _killer(anchor, repl, out))
            pg.on("pageerror", lambda e: out["errs"].append(str(e)))
            pg.goto("http://127.0.0.1:%d/index.html" % port, wait_until="load")
            pg.click("#t-ws")
            pg.wait_for_timeout(700)
            out["home"] = pg.evaluate(READ_WS_JS)
            btns = pg.query_selector_all('#wstree button[data-d="0"]')
            if btns:
                btns[0].click()
                pg.wait_for_timeout(500)
            out["picked"] = pg.evaluate(READ_WS_JS)
            out["pickedName"] = btns[0].get_attribute("data-p") if btns else None
            qb = pg.query_selector("#wsquick button[data-cmd]")
            if qb:
                qb.click()
                pg.wait_for_timeout(1200)
            out["quick"] = pg.evaluate(READ_WS_JS)
            pg.fill("#wsq", "wc -l index.html")
            pg.click("#wsrun")
            pg.wait_for_timeout(1500)
            out["typed"] = pg.evaluate(READ_WS_JS)
        except Exception as e:                                # noqa: BLE001
            return _blank("浏览器跑不起来：%r" % (e,))
        finally:
            try:
                br.close()
            except Exception:                                 # noqa: BLE001
                pass
    out["anchorHit"] = out.get("anchorHit", 0)
    return out


def check_browser(res):
    """返回 [(name, ok, detail)]；真浏览器判据。"""
    out = []
    errs = (res.get("home") or {}).get("errs") or []
    out.append(("浏览器 无 JS 报错", not errs, repr(errs[:2])))
    h = res.get("home") or {}
    out.append(("前端 三面都真渲染（非零尺寸）", bool(h.get("panels"))
                and all(h.get("panels") or []), "panels=%r" % (h.get("panels"),)))
    out.append(("前端 文件树有真条目",
                (h.get("treeN") or 0) >= 3 and "index.html" in (h.get("treeTxt") or ""),
                "treeN=%r txt=%r" % (h.get("treeN"), (h.get("treeTxt") or "")[:90])))
    # 首屏没选文件时，代码面必须**老实说没选**（占位），不许拿假内容糊上 ——
    # 这跟「点开之后必须换成真内容」是一对：前者防编造，后者防死端点。
    out.append(("前端 未选文件时代码面是诚实占位",
                "选一个文件看内容" in (h.get("code") or ""),
                "code=%r" % ((h.get("code") or "")[:90],)))
    p = res.get("picked") or {}
    out.append(("前端 点文件后代码面换成真内容",
                bool(res.get("pickedName"))
                and "选一个文件看内容" not in (p.get("code") or "")
                and len(p.get("code") or "") > 20,
                "picked=%r code=%r" % (res.get("pickedName"), (p.get("code") or "")[:90])))
    q = res.get("quick") or {}
    out.append(("前端 常用命令按钮回真 rc",
                "rc=0" in (q.get("term") or "") and "serve.py" in (q.get("term") or ""),
                "term 尾部=%r" % ((q.get("term") or "")[-110:],)))
    t = res.get("typed") or {}
    out.append(("前端 运行按钮回真 rc",
                "rc=0" in (t.get("term") or "") and "index.html" in (t.get("term") or ""),
                "term=%r" % ((t.get("term") or "")[:110],)))
    return out


def run_backend_suite():
    res = []
    for name, fn in BACKEND:
        try:
            ok, d = fn()
        except Exception as e:                                # noqa: BLE001
            ok, d = False, "EXC %r" % (e,)
        res.append((name, ok, d))
    return res


# ---------------------------------------------------------------- 变异体
# (tag, 说明, 锚点, 替换, kind)：锚点必须在被测文件里**恰好命中一次**，
# 命中 0 次说明源码漂移、门自己失效了 —— 那比红更坏，所以直接判红而不是跳过。
M1 = ("M1", "关掉 realpath 前缀判据（沙箱失效）",
      "    real = os.path.realpath(os.path.join(rroot, p))\n"
      "    if real != rroot and not real.startswith(rroot + os.sep):",
      "    real = os.path.realpath(os.path.join(rroot, p))\n    if False:", "backend")
M2 = ("M2", "白名单放宽成任意命令（env 兜底）",
      "base = TERM_CMDS.get(str(argv[0]))",
      "base = TERM_CMDS.get(str(argv[0])) or ['/usr/bin/env']", "backend")
M3 = ("M3", "把 returncode 写死 0（终端谎报成功）",
      '"rc": p.returncode,', '"rc": 0,', "backend")
M4 = ("M4", "把终端 truncated 写死 False（假装输出完整）",
      '"truncated": len(text) > TERM_MAX_OUT,', '"truncated": False,', "backend")
M5 = ("M5", "把代码面 truncated 写死 False（假装文件读全了）",
      '"path": rel, "size": size, "truncated": trunc,',
      '"path": rel, "size": size, "truncated": False,', "backend")
M6 = ("M6", "摘掉常用命令按钮的监听（按钮在、点了没反应）",
      "wsTerm(b.dataset.cmd);", "void b.dataset.cmd;", "browser")
MUTANTS = [M1, M2, M3, M4, M5, M6]


def write_mutant(tag, anchor, repl):
    src = io.open(SERVE_PY, encoding="utf-8").read()
    if src.count(anchor) != 1:
        return None, "锚点在 serve.py 里命中 %d 次（必须恰好 1 次）" % src.count(anchor)
    path = os.path.join(HERE, "_envmut_%s.py" % tag)
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(src.replace(anchor, repl, 1))
    return path, ""


def cleanup_tmp():
    for m in MUTANTS:
        p = os.path.join(HERE, "_envmut_%s.py" % m[0])
        if os.path.exists(p):
            assert p.startswith(HERE + os.sep) and p.endswith(".py"), p
            os.unlink(p)
    for p in (BIG, LINK, NEVER):
        if os.path.exists(p) or os.path.islink(p):
            assert p.startswith(WORK + os.sep), p
            os.unlink(p)


def make_fixtures():
    os.makedirs(WORK, exist_ok=True)
    for p in (LINK, NEVER):
        if os.path.exists(p) or os.path.islink(p):
            os.unlink(p)
    os.symlink("/etc/passwd", LINK)
    with io.open(BIG, "w", encoding="utf-8") as f:
        f.write("x" * 300000)


def env_check():
    miss = [n for n, p in (("Chromium", CHROME), ("serve.py", SERVE_PY),
                           ("index.html", os.path.join(ROOT, "index.html")))
            if not os.path.exists(p)]
    try:
        import playwright.sync_api  # noqa: F401
    except Exception as e:                                    # noqa: BLE001
        miss.append("playwright(%r)" % (e,))
    return (not miss), miss


def report(rc):
    names = list(G.keys())
    red = [n for n in names if not G[n]]
    path = os.path.join(WORK, "env_gate.json")
    out = {"gate": "env_gate", "rc": rc, "checks": len(names),
           "green": len(names) - len(red), "red": red, "why": WHY}
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
    for n in names:
        line = "  [%s] %s" % ("绿" if G[n] else "红", n)
        if not G[n]:
            line += "  <- " + str(WHY.get(n, ""))
        print(line)
    print("  门判据 %d 项，红 %d 项 -> %s" % (len(names), len(red), path))
    tail = {0: "== D88 env_gate OK ==",
            1: "== D88 env_gate FAIL ==",
            2: "== D88 env_gate ENV-UNAVAILABLE =="}[rc]
    print(tail)
    return rc


def main():
    ok, miss = env_check()
    if not ok:
        print("  环境不可用：%s" % miss)
        return report(2)
    make_fixtures()
    sha_b = {nm: stat_fp(p) for nm, p in
             (("index.html", os.path.join(ROOT, "index.html")),
              ("serve.py", SERVE_PY))}
    srv = None
    try:
        # (1) 后端正例：真 serve.py 起在临时端口上。
        PORT[0] = free_port()
        srv = start_server(PORT[0], load_serve(SERVE_PY, "_d88_serve_live"))
        base_backend = {}
        for name, cok, d in run_backend_suite():
            base_backend[name] = cok
            add("正例 " + name, cok, d)
        okt, dt = c_whitelist_static(load_serve(SERVE_PY, "_d88_serve_probe"))
        base_backend["白名单不含写/删类命令"] = okt
        add("正例 白名单不含写/删类命令", okt, dt)
        okw, dw = c_frontend_wired()
        base_backend["前端接线"] = okw
        add("正例 前端接线（tab/panel/映射/wsInit 在 boot 内各唯一）", okw, dw)
        # (2) 前端正例：真 Chromium 点一遍三面。
        res = run_browser(PORT[0])
        base_browser = {}
        for name, cok, d in check_browser(res):
            base_browser[name] = cok
            add("正例 " + name, cok, d)
        srv.shutdown()
        srv = None

        # (3) 上牙：每个变异体都必须被至少一条判据抓住；一个都抓不住 = 门失能。
        for tag, why, anchor, repl, kind in MUTANTS:
            if kind == "backend":
                mpath, werr = write_mutant(tag, anchor, repl)
                add("上牙 %s 锚点唯一（%s）" % (tag, why), bool(mpath), werr)
                if not mpath:
                    continue
                msrv = None
                caught = []
                try:
                    mmod = load_serve(mpath, "_d88_mut_%s" % tag)
                    msrv = start_server(free_port(), mmod)
                    PORT[0] = msrv.server_address[1]
                    for name, v, d in run_backend_suite():
                        if base_backend.get(name) and not v:
                            caught.append("%s <- %s" % (name, str(d)[:110]))
                    okt2, dt2 = c_whitelist_static(mmod)
                    if base_backend.get("白名单不含写/删类命令") and not okt2:
                        caught.append("白名单不含写/删类命令 <- %s" % str(dt2)[:110])
                except Exception as e:                        # noqa: BLE001
                    caught.append("变异体直接崩了：%r" % (e,))
                finally:
                    if msrv is not None:
                        msrv.shutdown()
                add("上牙 %s 被抓住（%s）" % (tag, why), bool(caught),
                    "变异后相关判据仍全绿 = 门失能")
                for c in caught:
                    print("       %s 抓住于 %s" % (tag, c))
                try:
                    os.unlink(mpath)
                except OSError:
                    pass
            else:
                PORT[0] = free_port()
                msrv = start_server(PORT[0], load_serve(SERVE_PY, "_d88_mut_%s" % tag))
                try:
                    mres = run_browser(PORT[0], anchor=anchor, repl=repl)
                    caught = []
                    for name, v, d in check_browser(mres):
                        if base_browser.get(name) and not v:
                            caught.append("%s <- %s" % (name, str(d)[:110]))
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
        if srv is not None:
            srv.shutdown()
        cleanup_tmp()
        for nm, path in (("index.html", os.path.join(ROOT, "index.html")),
                         ("serve.py", SERVE_PY)):
            fp = stat_fp(path)
            add("零写入 %s 本轮字节未变" % nm, fp[0] == sha_b[nm][0],
                drift_why(nm, sha_b[nm], fp, "_envmut_"))
        leftover = [f for f in os.listdir(HERE) if f.startswith("_envmut_")]
        add("零写入 tools/ 下无残留变异副本", not leftover, repr(leftover))
    return report(1 if [n for n in G if not G[n]] else 0)


if __name__ == "__main__":
    sys.exit(main())
