#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D96 项目空间门：左栏「项目与任务空间」不是壳，是真目录事实 + 真换根。

为什么必须单独一道门（D95 已收口的四条都判不到的那个形状）：
    * D95 交接明写「5 个视图只有总览做过真视口逐屏走查」，而这一轮补的左栏
      项目空间**在它自己的回合里没被任何门覆盖**：view_dod 判不出「项目列表
      是真的还是占位」，a11y_walk 只要求地标有可访问名、连名字都不锁，
      env_gate 判的是 ROOT 单一根的文件/代码/终端三面（根本没有 proj 参数），
      resident_gate 只判常驻进程活着。换句话说，D96 之前把 /__projects
      整个删掉、把 __fs 的 proj 参数整个删掉，全链依然 30/30 全绿 ——
      这正是这个项目一路在治的「少了一整个面却不自己变红」；
    * 更坏的一种形状是「看起来切了项目、其实还在读同一个根」：左栏两个项目
      按钮、点了 aria-current 换、路径文案也变了，但 fetch 没带 proj，
      于是面板安静地读产品根。这类装饰性切换只有**真换根**判据能抓：
      「同一个文件在 A 项目读得到、在 B 项目必须读不到」。

判据分层（刻意不复刻被测对象）：
    * 沙箱/白名单/失败模式全走**真 HTTP**，打到 serve.py 起在临时端口上的真实例，
      import 后只覆盖 TREE（第二棵树换成门自己造的 fixture），ROOT 保持真产品根 ——
      这样「console 项目能列到真 index.html」「future 项目只有 fixture 里那两个文件」
      两侧都不是编出来的；
    * 目录事实（exists / readable / n_top / n_files / newest_mtime / truncated）
      不只断言字段存在，还要**和门自己 os.listdir/os.walk 出来的真值对账** ——
      上一条 D88 的教训是「后端全绿、屏上点 wc 才发现命令起不来」，这一条是
      「字段全绿、屏上数字全是编的」。P4 变异体就是把 n_top 写死 1 那种形状；
    * 真 Chromium 点一遍：首屏两个项目节点都渲染、默认项目在激活位、
      默认树含 index.html、点 future 真换成 fixture 独有文件并读到 fixture 内容、
      点回 console 真换回来且不残留。纯文本 grep 判不出「按钮在、点了没反应」；
    * 静态只证「接线存在且唯一」，不冒充行为证明 —— 名字里都写着「静态」。

上牙（六个变异体；P2/P4/P6 改活的 serve.py 副本，P1/P3/P5 改 served 的
index.html 字节；三种副本都只写临时文件，活文件零写入，跑完 sha256 自证）：
    P1 前端取目录时不带 proj（点了也不换根）      -> 真换根判据必须红；
    P2 未知 proj 退化成 ROOT（fail-closed 失效）  -> 400 判据必须红；
    P3 换掉项目空间的 nav id（接线整个断）        -> 模板静态 + 真浏览器必须红；
    P4 把 n_top 写死 1（目录事实编造）            -> 与真值对账判据必须红；
    P5 摘掉 projPick 里的重拉（点了不刷新）        -> 真浏览器必须红；
    P6 projects_payload 里 root 写死假路径        -> roots realpath 对账必须红。
    锚点在被测文件里必须**恰好命中 1 次**，命中 0 次说明源码漂移、门自己失效了 ——
    那比红更坏，所以直接判红而不是静默跳过。

安全红线（刻意的设计约束，不是遗漏）：
    * **终端 /__term 刻意不做 proj 切换**。白名单里 python3 -c 可以写盘，
      一旦跟着左栏切根，「读文件面板」就变成了「换个项目名就能在别的树上执行」，
      而 UI 上还标着「固定在产品根」。所以门里有一条**正向**判据：带
      X-AgentOS-Action 头的 pwd 必须回产品根，不是门偷懒不管，是设计如此；
    * fixture 只在 work/ 下造三个小文件，删门用 os.unlink，不碰 ROOT 外的任何路径。

用法：python3 tools/projspace_gate.py   # rc=0 全过 / 1 有红项 / 2 环境不可用
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
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVE_PY = os.path.join(HERE, "serve.py")
INDEX_HTML = os.path.join(ROOT, "index.html")
WORK = os.path.join(ROOT, "work")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

FIXTURE = os.path.join(WORK, "psgate_tree")
FIX_FILE = os.path.join(FIXTURE, "only_here.txt")
FIX_SUB = os.path.join(FIXTURE, "sub", "deep.txt")
FIX_BODY = "PSGATE-ONLY-IN-FIXTURE\nline2\n"
FIX_REAL = os.path.realpath(FIXTURE)
ROOT_REAL = os.path.realpath(ROOT)

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


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def load_serve(path, tag, tree=None):
    spec = importlib.util.spec_from_file_location(tag, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    if tree is not None:
        # TREE 每次调用现读（project_roots 是函数而非常量），所以这里覆盖后端点跟着变。
        # ROOT 一律不动：console 项目必须指回真产品根，否则「真换根」就退化成
        # 「两个假的树互相对换」，那种测试永远绿，也永远测不到东西。
        m.TREE = tree
    return m


def start_server(port, mod):
    from http.server import ThreadingHTTPServer
    # serve.py 的 Handler 每条请求都往 stderr 打一行；门里几十个请求会把噪声
    # 刷满输出、淹掉真正的红项。只在门自己的实例上静音，被测行为一字未改。
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
    """返回 (status, dict)。HTTP 错误也是结果，不是异常 —— 400 是判据之一。"""
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


def fs_q(path, op="list", proj=None, **kw):
    q = {"op": op, "path": path}
    if proj is not None:
        q["proj"] = proj
    q.update({k: str(v) for k, v in kw.items()})
    return http("GET", "http://127.0.0.1:%d/__fs?%s"
                % (PORT[0], urllib.parse.urlencode(q)))


def term_q(argv):
    return http("POST", "http://127.0.0.1:%d/__term" % PORT[0], {"argv": argv})


def projects_q():
    return http("GET", "http://127.0.0.1:%d/__projects" % PORT[0])


# ------------------------------------------------------------- 真值对账用
def truth_n_files(root, cap):
    """门自己走一遍 os.walk 数文件 —— 不复用被测代码，也不碰它的常量。"""
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn.startswith(".") or ".bak" in fn:
                continue
            n += 1
            if n >= cap:
                return n
    return n


# ---------------------------------------------------------------- 后端判据
def c_projects_list():
    st, j = projects_q()
    items = j.get("projects") or []
    keys = [i.get("key") for i in items]
    return (st == 200 and j.get("ok") is True and keys == ["console", "future"]
            and j.get("n_projects") == len(items)
            and j.get("active") and j.get("active") == j.get("default")
            and j.get("active") in keys
            and isinstance(j.get("scan_cap"), int) and j.get("scan_cap") > 0), \
        "HTTP=%r ok=%r keys=%r n=%r active=%r default=%r cap=%r" % (
            st, j.get("ok"), keys, j.get("n_projects"), j.get("active"),
            j.get("default"), j.get("scan_cap"))


def c_roots_realpath():
    """白名单里的 root 必须是真实目录的真实路径，且两个项目真的不是同一个根。

    「两个项目卡指向同一个根」是这一面最阴的一种假绿：UI 上是两个按钮、
    事实都对，切过去读到的还是同一个目录，只有这条能抓。
    """
    st, j = projects_q()
    roots = [os.path.realpath(str(i.get("root") or "")) for i in (j.get("projects") or [])]
    want = [ROOT_REAL, FIX_REAL]
    return (st == 200 and roots == want and len(set(roots)) == 2), \
        "roots=%r 期望=%r" % (roots, want)


def c_facts_real():
    """目录事实要和门自己 os.listdir/os.walk 出来的真值一致，不许编。"""
    st, j = projects_q()
    bad = []
    for i in (j.get("projects") or []):
        root = os.path.realpath(str(i.get("root") or ""))
        f = i.get("facts") or {}
        cap = int(j.get("scan_cap") or 0) or 2000
        want_top = len(os.listdir(root))
        want_files = truth_n_files(root, cap)
        if f.get("exists") is not os.path.isdir(root):
            bad.append("%s exists=%r" % (i.get("key"), f.get("exists")))
        if f.get("readable") is not True:
            bad.append("%s readable=%r" % (i.get("key"), f.get("readable")))
        if f.get("n_top") != want_top:
            bad.append("%s n_top=%r 真值=%d" % (i.get("key"), f.get("n_top"), want_top))
        if not isinstance(f.get("n_files"), int) or f["n_files"] < min(1, want_files):
            bad.append("%s n_files=%r 真值=%d" % (i.get("key"), f.get("n_files"),
                                                  want_files))
        nm = f.get("newest_mtime")
        if not isinstance(nm, int) or nm <= 0 or nm > int(__import__("time").time()) + 60:
            bad.append("%s newest_mtime=%r" % (i.get("key"), nm))
        if f.get("truncated") not in (True, False):
            bad.append("%s truncated=%r" % (i.get("key"), f.get("truncated")))
        if i.get("ok") is not bool(f.get("readable")):
            bad.append("%s ok=%r readable=%r" % (i.get("key"), i.get("ok"),
                                                 f.get("readable")))
    return (not bad), "; ".join(bad[:4]) or "两项目六项事实与真值一致"


def c_unknown_proj():
    st, j = fs_q("", proj="bogus")
    err = str(j.get("error") or "")
    return (st == 400 and j.get("ok") is False and j.get("code") == "unknown_project"
            and "未知项目" in err and sorted(j.get("projects") or []) == ["console", "future"]), \
        "HTTP=%r ok=%r code=%r err=%r projects=%r" % (
            st, j.get("ok"), j.get("code"), err[:70], j.get("projects"))


def c_empty_proj_default():
    st, j = fs_q("", proj="")
    names = [e.get("name") for e in (j.get("entries") or [])] if j.get("ok") else []
    st2, j2 = fs_q("", proj=None)
    return (st == 200 and j.get("ok") and "index.html" in names
            and st2 == 200 and j2.get("ok")), \
        "proj='' HTTP=%r n=%d 含index.html=%s；不带 proj 参数 HTTP=%r" % (
            st, len(names), "index.html" in names, st2)


def c_switch_read():
    """真换根：同一个文件名在 future 读得到、在 console 必须读不到。"""
    stf, jf = fs_q("only_here.txt", op="read", proj="future")
    stc, jc = fs_q("only_here.txt", op="read", proj="console")
    return (stf == 200 and jf.get("ok") is True and FIX_BODY in str(jf.get("text") or "")
            and jc.get("ok") is False and "文件不存在" in str(jc.get("error") or "")), \
        "future HTTP=%r ok=%r 正文命中=%s；console HTTP=%r ok=%r err=%r" % (
            stf, jf.get("ok"), FIX_BODY.strip() in str(jf.get("text") or ""),
            stc, jc.get("ok"), str(jc.get("error"))[:60])


def c_switch_list():
    stf, jf = fs_q("", proj="future")
    stc, jc = fs_q("", proj="console")
    fnames = [e.get("name") for e in (jf.get("entries") or [])]
    cnames = [e.get("name") for e in (jc.get("entries") or [])]
    return (stf == 200 and jf.get("ok") and "only_here.txt" in fnames
            and "index.html" not in fnames
            and stc == 200 and jc.get("ok") and "index.html" in cnames
            and "only_here.txt" not in cnames), \
        "future=%r；console 含 index.html=%s 含 only_here=%s" % (
            fnames[:6], "index.html" in cnames, "only_here.txt" in cnames)


def c_switch_subdir():
    """换根后子目录必须按新根解析，且目录不能被当文件读。

    depth=2 时 fs_list 只给**目录**条目挂 children，所以要验的是「根里的目录
    sub，其 children 里含 deep.txt」，而不是去 sub 里再列一层。这不是写法偏好：
    门的第一稿数的是 sub 的 children，而文件本来就没有 children，判据恒假 ——
    一条永远为假的判据比没有判据更坏，它给人「子目录这条已经验过」的错觉。
    """
    st, j = fs_q("", proj="future", depth=2)
    entries = (j.get("entries") or []) if j.get("ok") else []
    names = [e.get("name") for e in entries]
    subs = [e for e in entries if e.get("name") == "sub"]
    kids = [c2.get("name") for e in subs for c2 in (e.get("children") or [])]
    stc, jc = fs_q("sub", proj="future", op="read")
    return (st == 200 and "only_here.txt" in names and "index.html" not in names
            and bool(subs) and "deep.txt" in kids
            and stc == 200 and jc.get("ok") is False and "目录" in str(jc.get("error") or "")), \
        "future 根=%r；sub 是目录=%s 其 children=%r；点目录当文件读 ok=%r err=%r" % (
            names[:5], bool(subs), kids, jc.get("ok"), str(jc.get("error"))[:50])


def c_escape_after_switch():
    """换到 fixture 之后，越界读必须仍被拒，而且报错要点名 fixture 的真根。"""
    st, j = fs_q("../../../../../../etc/passwd", op="read", proj="future")
    err = str(j.get("error") or "")
    leaked = "root:" in json.dumps(j, ensure_ascii=False)
    return (j.get("ok") is False and "越界" in err and FIX_REAL in err and not leaked), \
        "HTTP=%r err=%r 泄漏=%s" % (st, err[:110], leaked)


def c_term_stays_root():
    """终端刻意不随左栏切根：pwd 必须回产品根。这是设计约束，正向断言。"""
    st, j = term_q(["pwd"])
    out = str(j.get("out") or "").strip()
    return (j.get("ok") is True and out == ROOT_REAL), \
        "HTTP=%r out=%r 期望=%r" % (st, out[:80], ROOT_REAL)


def c_scan_cap_truncated(mod):
    """数到上限必须显式 truncated=True，不许把「数到一半」写成精确值。

    手法是改模块常量而不是造 2000 个文件：造文件只证明「多的时候怎样」，
    而这里要证明的是**上限本身被当真**——上限是多少就得数到多少并自认截断。
    """
    old = getattr(mod, "PROJ_SCAN_CAP", None)
    mod.PROJ_SCAN_CAP = 1
    try:
        f = mod.project_facts(FIXTURE)
    finally:
        mod.PROJ_SCAN_CAP = old
    f2 = mod.project_facts(FIXTURE)
    return (f.get("truncated") is True and f.get("n_files") == 1
            and f2.get("truncated") is False and (f2.get("n_files") or 0) >= 2), \
        "cap=1 -> truncated=%r n_files=%r；恢复后 truncated=%r n_files=%r" % (
            f.get("truncated"), f.get("n_files"), f2.get("truncated"), f2.get("n_files"))


def c_all_unreadable(mod):
    """一个项目都读不到时必须 ok=False + error + active 为空，不许空列表充数。"""
    old = mod.project_roots
    mod.project_roots = lambda: [
        {"key": "ghost-a", "label": "A", "root": "/nonexistent-psgate-a", "role": "x"},
        {"key": "ghost-b", "label": "B", "root": "/nonexistent-psgate-b", "role": "x"},
    ]
    try:
        p = mod.projects_payload()
    finally:
        mod.project_roots = old
    p2 = mod.projects_payload()
    bad = []
    if p.get("ok") is not False or not p.get("error") or p.get("active") is not None:
        bad.append("全不可读时 ok=%r error=%r active=%r"
                   % (p.get("ok"), str(p.get("error"))[:40], p.get("active")))
    if any(i.get("ok") for i in (p.get("projects") or [])):
        bad.append("全不可读时仍有 ok=True 的项")
    if p2.get("ok") is not True or p2.get("active") != p2.get("default"):
        bad.append("恢复后 ok=%r active=%r" % (p2.get("ok"), p2.get("active")))
    return (not bad), "; ".join(bad) or "全不可读诚实报错，恢复后回到默认项目"


def c_static_failclosed():
    """静态：未知 key 必须 fail-closed，源码里不许有「退化到 ROOT」的后门。"""
    try:
        src = io.open(SERVE_PY, encoding="utf-8").read()
    except OSError as e:                                    # noqa: BLE001
        return False, "读不到 serve.py：%r" % (e,)
    import ast
    body = ""
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == "project_root":
            body = ast.get_source_segment(src, node) or ""
            break
    if not body:
        return False, "定位不到 project_root 函数体（源码漂移或函数被改名）"
    silent = re.findall(r"project_root\s*\([^)]*\)\s*or\s+ROOT|return\s+ROOT\s*,\s*None", body)
    return (not silent and "未知项目 key=" in body
            and body.count("return None,") >= 1), \
        "静默退化 %d 处；'未知项目 key=' %d 次；fail-closed return %d 次" % (
            len(silent), body.count("未知项目 key="), body.count("return None,"))


BACKEND = [
    ("项目清单 /__projects 报真清单", c_projects_list),
    ("项目根 两根 realpath 且互不相同", c_roots_realpath),
    ("项目根 目录事实与 os.walk 真值对账", c_facts_real),
    ("未知 proj 一律 400 fail-closed", c_unknown_proj),
    ("空 proj 落默认项目", c_empty_proj_default),
    ("真换根 同名文件换根后读不到", c_switch_read),
    ("真换根 目录清单换根后不同", c_switch_list),
    ("真换根 子目录按新根解析", c_switch_subdir),
    ("真换根 越界读仍拒且点名当前根", c_escape_after_switch),
    ("终端 刻意不随左栏切根（pwd 回产品根）", c_term_stays_root),
]


# ------------------------------------------------------- 前端接线（静态）
def c_static_template():
    """静态接线：地标新名唯一、旧名零残留、两个 nav 都在、每处取数调用都带 proj。

    只证「接上了且唯一」。它冒充不了「点了有反应」—— 那靠真浏览器那几条。

    proj 参数的期望数从 2 改成 4，是因为 W2 轮一次上了「工作区」「文件」两个视图，
    每个视图各有 list + read 两条真调用（wsList/wsRead/fileList/fileRead），四条都
    必须带 proj：少带任一条，那一面就会静默改显驾驶舱根的目录，而用户以为自己还在看
    当前项目 —— 正是本判据要堵的失败模式。所以期望数跟着**真调用数**走，判据本身
    一字未松：现在少任何一处都照样判红。"""
    try:
        src = io.open(INDEX_HTML, encoding="utf-8").read()
    except OSError as e:                                    # noqa: BLE001
        return False, "读不到 index.html：%r" % (e,)
    counts = {
        'aside aria-label="项目与任务空间"': src.count('<aside class="sp" aria-label="项目与任务空间">'),
        'aside aria-label="导航"（旧名残留）': src.count('<aside class="sp" aria-label="导航">'),
        'nav id="projspace"': src.count('id="projspace"'),
        'aria-label="项目空间导航"': src.count('aria-label="项目空间导航"'),
        'nav id="rail"': src.count('id="rail"'),
        'div id="wsproj"': src.count('id="wsproj"'),
        "fetch('/__fs?proj=（真调用）": src.count("fetch('/__fs?proj='"),
        "function projInit()": src.count("function projInit()"),
        "function projPick(": src.count("function projPick("),
        "function projPath()": src.count("function projPath()"),
        "终端诚实标注": src.count("固定在产品根，不随左侧项目切"),
        "读不到时诚实报错分支": src.count("项目空间读不到："),
        "truncated 显示成 ≥N": src.count("'≥'+f.n_files"),
    }
    want = {"aside aria-label=\"项目与任务空间\"": 1,
            'aside aria-label="导航"（旧名残留）': 0,
            'nav id="projspace"': 1,
            'aria-label="项目空间导航"': 1,
            'nav id="rail"': 1,
            'div id="wsproj"': 1,
            "fetch('/__fs?proj=（真调用）": 4,
            "function projInit()": 1,
            "function projPick(": 1,
            "function projPath()": 1,
            "终端诚实标注": 1,
            "读不到时诚实报错分支": 2,
            "truncated 显示成 ≥N": 1}
    miss = ["%s=%d 期望%d" % (k, counts[k], want[k])
            for k in want if counts[k] != want[k]]
    # projInit(); 必须落在 boot() 里 —— 「定义了没接线」正是 retro_gate 那次的形状。
    boot_at = src.find("function boot()")
    called_at = src.find("projInit();")
    end_at = src.find("</script>", boot_at)
    in_boot = -1 <= boot_at <= called_at < end_at
    if not in_boot:
        miss.append("projInit(); 不在 boot() 内（boot@%d 调用@%d script末@%d）"
                    % (boot_at, called_at, end_at))
    return (not miss), "; ".join(miss) or "%d 项静态接线计数全对，projInit 在 boot 内" % len(want)


STATIC = [("静态 模板接线（地标/双 nav/proj 参数四处/projInit 在 boot）",
           c_static_template)]


def c_static_serve(src):
    """同一批接线判据，跑在**被 served 的那份字节**上，供 P3 变异体使用。"""
    counts = [src.count('<nav class="pjs" id="projspace" aria-label="项目空间导航">'),
              src.count('id="rail"'), src.count("fetch('/__fs?proj=')")]
    return (counts == [1, 1, 4]), "served 字节里 projspace nav=%d rail=%d fetch(/__fs?proj==%d" % (
        counts[0], counts[1], counts[2])


# ------------------------------------------------------------ 真浏览器判据
READ_JS = (
    "() => { const q=s=>document.querySelector(s);"
    " const t=e=>(e?(e.innerText||e.textContent||''):'');"
    " const nodes=[...document.querySelectorAll('#projspace .pjnode')];"
    " return {"
    " nodeN: nodes.length,"
    " nodeNames: nodes.map(n=>t(n)),"
    " nodeTitles: nodes.map(n=>n.getAttribute('title')||''),"
    " current: nodes.map(n=>n.getAttribute('aria-current')),"
    " hasSpace: !!q('nav[aria-label=\"项目空间导航\"]'),"
    " hasAside: !!q('aside[aria-label=\"项目与任务空间\"]'),"
    " hasRail: !!q('nav#rail'),"
    " hasErr: !!q('#projspace .pjerr'),"
    " hasLoad: !!q('#projspace .pjload'),"
    " wsproj: t(q('#wsproj')),"
    " tree: t(q('#wstree')).slice(0,600),"
    " treeN: document.querySelectorAll('#wstree button').length,"
    " code: t(q('#wscode')).slice(0,400),"
    " dct: t(q('#wsdct')).slice(0,300),"
    " wsOn: (q('#v-ws')||{className:''}).className.includes('on')"
    " }; }"
)


def _blank(err):
    b = {"nodeN": 0, "nodeNames": [], "nodeTitles": [], "current": [],
         "hasSpace": False, "hasAside": False, "hasRail": False,
         "hasErr": False, "hasLoad": False, "wsproj": "", "tree": "",
         "treeN": 0, "code": "", "dct": "", "wsOn": False}
    return {"errs": [err], "anchorHit": 0, "served": "", "home": b, "fut": b,
            "futFile": b, "back": b, "clicked": None, "futKey": None}


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


def launch(pw):
    """优先用系统 Chrome（版本与用户日常一致），没有就退回 playwright 自带的。"""
    try:
        return pw.chromium.launch(executable_path=CHROME)
    except Exception:                                       # noqa: BLE001
        return pw.chromium.launch()


def run_browser(port, anchor=None, repl=None):
    """真 Chromium 点一遍项目空间；anchor/repl 非空时拦 served 的 index.html 字节。"""
    from playwright.sync_api import sync_playwright
    out = {"errs": []}
    with sync_playwright() as pw:
        br = launch(pw)
        try:
            ctx = br.new_context(viewport={"width": 1440, "height": 900})
            pg = ctx.new_page()
            if anchor is not None:
                pg.route("**/index.html", _killer(anchor, repl, out))
                try:
                    out["served"] = pg.evaluate(
                        "async u => await (await fetch(u)).text()",
                        "http://127.0.0.1:%d/index.html" % port) or ""
                except Exception:                            # noqa: BLE001
                    out["served"] = ""
                # fetch 走的是 route 里那条管线，这里再抓一次原始字节以确认变异生效
                try:
                    resp = pg.request.get("http://127.0.0.1:%d/index.html" % port)
                    out["servedApi"] = resp.text()
                except Exception:                            # noqa: BLE001
                    out["servedApi"] = ""
            pg.on("pageerror", lambda e: out["errs"].append(str(e)))
            pg.goto("http://127.0.0.1:%d/index.html" % port, wait_until="load")
            pg.wait_for_timeout(1200)
            out["home"] = pg.evaluate(READ_JS)
            pg.click("#t-ws")
            pg.wait_for_timeout(700)
            out["homeWs"] = pg.evaluate(READ_JS)
            nodes = pg.query_selector_all("#projspace .pjnode")
            out["futKey"] = nodes[1].get_attribute("data-proj") if len(nodes) > 1 else None
            if len(nodes) > 1:
                nodes[1].click()
                pg.wait_for_timeout(800)
                out["fut"] = pg.evaluate(READ_JS)
                btns = pg.query_selector_all('#wstree button[data-d="0"]')
                out["clicked"] = btns[0].get_attribute("data-p") if btns else None
                if btns:
                    btns[0].click()
                    pg.wait_for_timeout(700)
                out["futFile"] = pg.evaluate(READ_JS)
                nodes = pg.query_selector_all('#projspace .pjnode')
                if nodes:
                    nodes[0].click()
                    pg.wait_for_timeout(800)
                out["back"] = pg.evaluate(READ_JS)
        except Exception as e:                                # noqa: BLE001
            return _blank("浏览器跑不起来：%r" % (e,))
        finally:
            try:
                br.close()
            except Exception:                                 # noqa: BLE001
                pass
    out["anchorHit"] = out.get("anchorHit", 0)
    out.setdefault("served", out.get("servedApi", ""))
    out.setdefault("servedApi", "")
    return out


def check_browser(res):
    """返回 [(name, ok, detail)]。"""
    out = []
    errs = res.get("errs") or []
    out.append(("浏览器 无 JS 报错", not errs, repr(errs[:2])))
    h = res.get("home") or {}
    out.append(("浏览器 两个项目节点都渲染",
                (h.get("nodeN") or 0) == 2 and not h.get("hasErr")
                and not h.get("hasLoad"),
                "nodeN=%r err=%r load残留=%r" % (h.get("nodeN"), h.get("hasErr"),
                                                h.get("hasLoad"))))
    names = h.get("nodeNames") or []
    out.append(("浏览器 节点有可访问名（label + key + 路径 + 计数）",
                len(names) == 2 and all(len(x or "") > 8 for x in names)
                and any("console" in (x or "") for x in names)
                and any("future" in (x or "") for x in names)
                and all((t or "").count("\n") >= 1 for t in (h.get("nodeTitles") or [])),
                "names=%r titles=%r" % (names, (h.get("nodeTitles") or [])[:2])))
    out.append(("浏览器 地标齐（项目与任务空间 / 项目空间导航 / 轮次脊线）",
                bool(h.get("hasAside")) and bool(h.get("hasSpace")) and bool(h.get("hasRail")),
                "aside=%r space=%r rail=%r" % (h.get("hasAside"), h.get("hasSpace"),
                                               h.get("hasRail"))))
    out.append(("浏览器 默认项目在激活位且只有一个 aria-current=true",
                h.get("current") == ["true", "false"], "current=%r" % (h.get("current"),)))
    hw = res.get("homeWs") or {}
    out.append(("浏览器 默认项目树是产品根（含 index.html）+ #wsproj 显示真根",
                "index.html" in (hw.get("tree") or "") and os.path.realpath(
                    str(hw.get("wsproj") or "")) == ROOT_REAL,
                "treeN=%r wsproj=%r" % (hw.get("treeN"), (hw.get("wsproj") or "")[:70])))
    fu = res.get("fut") or {}
    out.append(("浏览器 点 future 真换根（树换 fixture 独有文件）",
                "only_here.txt" in (fu.get("tree") or "")
                and "index.html" not in (fu.get("tree") or "")
                and os.path.realpath(str(fu.get("wsproj") or "")) == FIX_REAL,
                "tree=%r wsproj=%r" % ((fu.get("tree") or "")[:80],
                                       (fu.get("wsproj") or "")[:70])))
    out.append(("浏览器 点 future 后 aria-current 真的换到它",
                fu.get("current") == ["false", "true"], "current=%r" % (fu.get("current"),)))
    ff = res.get("futFile") or {}
    dct = (ff.get("dct") or "").casefold()
    out.append(("浏览器 点 fixture 文件读到 fixture 内容 + 面包屑写明项目",
                "PSGATE-ONLY-IN-FIXTURE" in (ff.get("code") or "")
                and "future agent" in dct and "驾驶舱" not in dct,
                "clicked=%r dct=%r（#wsdct 有 text-transform:uppercase，故比大小写无关）"
                % (res.get("clicked"), (ff.get("dct") or "")[:80])))
    bk = res.get("back") or {}
    out.append(("浏览器 切回 console 真换回且不残留 fixture 文件",
                "index.html" in (bk.get("tree") or "")
                and "only_here.txt" not in (bk.get("tree") or "")
                and "PSGATE-ONLY-IN-FIXTURE" not in (bk.get("code") or "")
                and bk.get("current") == ["true", "false"]
                and os.path.realpath(str(bk.get("wsproj") or "")) == ROOT_REAL,
                "tree=%r current=%r wsproj=%r" % ((bk.get("tree") or "")[:80],
                                                  bk.get("current"),
                                                  (bk.get("wsproj") or "")[:70])))
    return out


# ---------------------------------------------------------------- 变异体
# (tag, 说明, 锚点, 替换, kind)：kind=backend 改 serve.py 副本，kind=browser 改
# served 的 index.html 字节。锚点必须在被测文件里**恰好命中一次**；命中 0 次
# 说明源码漂移、门自己失效了 —— 那比红更坏，所以直接判红而不是跳过。
P1 = ("P1", "前端取目录时不带 proj（点了也不换根）",
      "    const r=await fetch('/__fs?proj='+encodeURIComponent(PROJ)\n"
      "      +'&op=list&depth=1&path='+encodeURIComponent(path||''));",
      "    const r=await fetch('/__fs'\n"
      "      +'?op=list&depth=1&path='+encodeURIComponent(path||''));", "browser")
P2 = ("P2", "未知 proj 退化成 ROOT（fail-closed 失效）",
      '    return None, ("未知项目 key=%r（白名单只有 %s）"\n'
      '                  % (str(key)[:40], "/".join(project_keys())))',
      "    return ROOT, None", "backend")
P3 = ("P3", "换掉项目空间的 nav id（接线整个断）",
      '<nav class="pjs" id="projspace" aria-label="项目空间导航">',
      '<nav class="pjs" id="projspaceX" aria-label="项目空间导航">', "browser")
P4 = ("P4", "把 n_top 写死 1（目录事实编造）",
      '        out["n_top"] = len(os.listdir(root))',
      '        out["n_top"] = 1', "backend")
P5 = ("P5", "摘掉 projPick 里的重拉（点了不刷新）",
      "  if($('#main')&&$('#v-ws').classList.contains('on')) wsList('');",
      "  void 0;", "browser")
P6 = ("P6", "projects_payload 里 root 写死假路径",
      '        items.append({"key": p["key"], "label": p["label"], "role": p["role"],\n'
      '                      "root": p["root"], "ok": bool(f.get("readable")), "facts": f})',
      '        items.append({"key": p["key"], "label": p["label"], "role": p["role"],\n'
      '                      "root": "/tmp/not-a-real-root",'
      ' "ok": bool(f.get("readable")), "facts": f})', "backend")
MUTANTS = [P1, P2, P3, P4, P5, P6]


def write_mutant(tag, anchor, repl):
    src = io.open(SERVE_PY, encoding="utf-8").read()
    if src.count(anchor) != 1:
        return None, "锚点在 serve.py 里命中 %d 次（必须恰好 1 次）" % src.count(anchor)
    path = os.path.join(HERE, "_psmut_%s.py" % tag)
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(src.replace(anchor, repl, 1))
    return path, ""


def cleanup_tmp():
    for m in MUTANTS:
        p = os.path.join(HERE, "_psmut_%s.py" % m[0])
        if os.path.exists(p):
            assert p.startswith(HERE + os.sep) and p.endswith(".py"), p
            os.unlink(p)
    if os.path.isdir(FIXTURE):
        for dp, dn, fn in os.walk(FIXTURE, topdown=False):
            for name in fn:
                os.unlink(os.path.join(dp, name))
            for name in dn:
                os.rmdir(os.path.join(dp, name))
        os.rmdir(FIXTURE)


def make_fixtures():
    """第二棵「项目树」造在 work/ 下：跟产品根毫无关系，正因如此换根才可证。"""
    if os.path.isdir(FIXTURE):
        cleanup_tmp()
    os.makedirs(os.path.join(FIXTURE, "sub"))
    with io.open(FIX_FILE, "w", encoding="utf-8") as f:
        f.write(FIX_BODY)
    with io.open(FIX_SUB, "w", encoding="utf-8") as f:
        f.write("PSGATE-DEEP-FIXTURE\n")
    return os.path.isfile(FIX_FILE) and os.path.isfile(FIX_SUB)


def env_check():
    miss = [n for n, p in (("Chromium", CHROME), ("serve.py", SERVE_PY),
                           ("index.html", INDEX_HTML), ("work/", WORK))
            if not os.path.exists(p)]
    try:
        import playwright.sync_api  # noqa: F401
    except Exception as e:                                    # noqa: BLE001
        miss.append("playwright(%r)" % (e,))
    if not os.access(WORK, os.W_OK):
        miss.append("work/ 不可写（门要在这下面造 fixture）")
    return (not miss), miss


def report(rc):
    names = list(G.keys())
    red = [n for n in names if not G[n]]
    path = os.path.join(WORK, "projspace_gate.json")
    out = {"gate": "projspace_gate", "d": "D96", "rc": rc, "checks": len(names),
           "green": len(names) - len(red), "red": red, "why": WHY}
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
    for n in names:
        line = "  [%s] %s" % ("绿" if G[n] else "红", n)
        if not G[n]:
            line += "  <- " + str(WHY.get(n, ""))
        print(line)
    print("  门判据 %d 项，红 %d 项 -> %s" % (len(names), len(red), path))
    tail = {0: "== D96 projspace_gate OK ==",
            1: "== D96 projspace_gate FAIL ==",
            2: "== D96 projspace_gate ENV-UNAVAILABLE =="}[rc]
    print(tail)
    return rc


def run_suite(mod):
    """后端判据在给定的 serve.py 模块上跑一遍，返回 {name: (ok, detail)}。"""
    res = {}
    for name, fn in BACKEND:
        try:
            res[name] = fn()
        except Exception as e:                                # noqa: BLE001
            res[name] = (False, "EXC %r" % (e,))
    for name, fn in (("项目事实 数到扫描上限必须自认截断", c_scan_cap_truncated),
                     ("项目空间 全不可读必须诚实报错", c_all_unreadable)):
        try:
            res[name] = fn(mod)
        except Exception as e:                                # noqa: BLE001
            res[name] = (False, "EXC %r" % (e,))
    return res


def main():
    ok, miss = env_check()
    if not ok:
        print("  环境不可用：%s" % miss)
        return report(2)
    if not make_fixtures():
        print("  fixture 没造出来：%s" % FIXTURE)
        return report(2)
    sha_b = {nm: sha256_file(p) for nm, p in
             (("index.html", INDEX_HTML), ("serve.py", SERVE_PY))}
    srv = None
    base_backend = {}
    base_browser = {}
    try:
        # (1) 后端正例：真 serve.py 起在临时端口上，TREE 指向门自己造的 fixture。
        PORT[0] = free_port()
        live = load_serve(SERVE_PY, "_d96_serve_live", tree=FIXTURE)
        srv = start_server(PORT[0], live)
        for name, (cok, d) in run_suite(live).items():
            base_backend[name] = cok
            add("正例 " + name, cok, d)
        okf, df = c_static_failclosed()
        base_backend["静态 未知 key 必须 fail-closed"] = okf
        add("正例 静态 未知 key 必须 fail-closed", okf, df)
        for name, fn in STATIC:
            cok, d = fn()
            base_backend[name] = cok
            add("正例 " + name, cok, d)
        # (2) 前端正例：真 Chromium 点一遍项目空间。
        res = run_browser(PORT[0])
        for name, cok, d in check_browser(res):
            base_browser[name] = cok
            add("正例 " + name, cok, d)
        srv.shutdown()
        srv = None

        # (3) 上牙：每个变异体都必须被至少一条正例判据抓住。
        for tag, why, anchor, repl, kind in MUTANTS:
            if kind == "backend":
                mpath, werr = write_mutant(tag, anchor, repl)
                add("上牙 %s 锚点唯一（%s）" % (tag, why), bool(mpath), werr)
                if not mpath:
                    continue
                msrv = None
                caught = []
                try:
                    mmod = load_serve(mpath, "_d96_mut_%s" % tag, tree=FIXTURE)
                    msrv = start_server(free_port(), mmod)
                    PORT[0] = msrv.server_address[1]
                    for name, v in run_suite(mmod).items():
                        if base_backend.get(name) and not v[0]:
                            caught.append("%s <- %s" % (name, str(v[1])[:110]))
                    okt, dt = c_static_failclosed()
                    if base_backend.get("静态 未知 key 必须 fail-closed") and not okt:
                        caught.append("静态 fail-closed <- %s" % str(dt)[:110])
                except Exception as e:                        # noqa: BLE001
                    caught.append("变异体直接崩了：%r" % (e,))
                finally:
                    if msrv is not None:
                        msrv.shutdown()
                    PORT[0] = 0
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
                msrv = start_server(PORT[0], load_serve(SERVE_PY, "_d96_mut_%s" % tag,
                                                        tree=FIXTURE))
                try:
                    mres = run_browser(PORT[0], anchor=anchor, repl=repl)
                    caught = []
                    for name, v, d in check_browser(mres):
                        if base_browser.get(name) and not v:
                            caught.append("%s <- %s" % (name, str(d)[:110]))
                    add("上牙 %s 锚点真被替换（%s）" % (tag, why),
                        bool(mres.get("anchorHit")), "变异字节没进 served 响应")
                    # P3 改的是模板结构，静态接线判据也必须跟着红 —— 这条跑在
                    # **served 的字节**上，不是磁盘上那份，否则等于没验。
                    sok, sd = c_static_serve(mres.get("served") or "")
                    if sok is not True:
                        caught.append("静态 served 接线 <- %s" % str(sd)[:110])
                    add("上牙 %s 被抓住（%s）" % (tag, why), bool(caught),
                        "变异后前端判据仍全绿 = 门失能")
                    for c in caught:
                        print("       %s 抓住于 %s" % (tag, c))
                finally:
                    msrv.shutdown()
                    PORT[0] = 0
    finally:
        PORT[0] = 0
        if srv is not None:
            srv.shutdown()
        cleanup_tmp()
        for nm, path in (("index.html", INDEX_HTML), ("serve.py", SERVE_PY)):
            add("零写入 %s 本轮字节未变" % nm, sha256_file(path) == sha_b[nm],
                "门自己写了活文件")
        leftover = [f for f in os.listdir(HERE) if f.startswith("_psmut_")]
        add("零写入 tools/ 下无残留变异副本", not leftover, repr(leftover))
        add("零写入 fixture 已清空", not os.path.isdir(FIXTURE), FIXTURE)
    return report(1 if [n for n in G if not G[n]] else 0)


if __name__ == "__main__":
    sys.exit(main())
