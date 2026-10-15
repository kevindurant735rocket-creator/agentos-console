#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真 served 页面无障碍门：把键盘/读屏判定从 file:// 搬到用户真正看到的 http://。

为什么这道门必须存在（D60，2026-10-03）：
  a11y_walk.py 原本只打 file://index.html。用户双击「启动 AgentOS.command」
  之后看到的是 http://127.0.0.1:8790/ —— 这不是同一件事：
    * fetch/XHR 在 file:// 下受 origin 限制，失败常常静默退化；
    * 相对路径解析基准不同（file:// 的基准是文件所在目录，不是站点根）；
    * CSP / service worker / history API 在两种 scheme 下行为不同。
  所以「file:// 16 门全绿」与「用户实际打开的页面键盘是坏的」可以同时为真。
  这道门起一份**独立端口**的 serve.py，把 a11y_walk --url 指过去，
  16 道判据一判不复制（唯一真相仍在 a11y_walk.py），变的只有导航目标。

四道判据：
  1) served == disk：HTTP 取回的 index.html 字节 sha256 必须等于盘上那份，
     且 serve.py 自报的 X-AgentOS-Build 头必须在场。
     门禁意义：证的是「测的是当前 build」，不是某个起不来的陈旧副本。
  2) 委托实跑：真跑 a11y_walk.py --url，rc==0 且输出含 A11Y_WALK OK，
     且它写下的 report 里 mode 必须是 served-http / target 以 http:// 开头。
     这条专治「--url 没被认出来、悄悄退回 file:// 恒绿」。
  3) 咬合牙：用一个改过字节的**隔离副本**当 served 源，同一段比较逻辑
     必须判红 —— 证明门 1 有牙，不是任何 HTTP 源都绿。
  4) 活的 index.html 零写入：跑完 sha256 必须与跑前一致
     （沿用 neg_a11y 的纪律，绝不就地改活页面）。

产出 work/a11y_served.json，rc=0 全过 / 2 有红项。
"""
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
HTML = os.path.join(ROOT, "index.html")
STALE_TREE = os.path.join(ROOT, "work", "_a11y_stale_tree")
WALK_REPORT = os.path.join(ROOT, "work", "a11y_walk.json")

# v118 老账：同一个门脚本有三种跑法就三种结果（缺 playwright 时 5 条门假红）。
# 引擎的「执行面接线」走的正是 `python3 tools/xxx.py` 这一种，所以门自己
# 负责换解释器（os.execv，不留第二套逻辑）。缺依赖的假红比没有门更糟。
_PW_PY = os.path.join(ROOT, ".venv-pw", "bin", "python")


def _reexec_with_playwright():
    try:
        import playwright  # noqa: F401
        return
    except Exception:
        pass
    if os.environ.get("AGENTOS_GATE_REEXEC") == "1":
        return                        # 已在正确解释器里仍缺依赖：真缺，如实红
    # 必须比**未解析软链的路径**：.venv-pw/bin/python 是软链到 homebrew python3，
    # 用 realpath 比会把「venv」和「系统」判成同一个，于是 shim 自我禁用。
    if os.path.exists(_PW_PY) and os.path.abspath(_PW_PY) != os.path.abspath(sys.executable):
        os.environ["AGENTOS_GATE_REEXEC"] = "1"
        os.execv(_PW_PY, [_PW_PY, os.path.abspath(__file__)] + sys.argv[1:])


_reexec_with_playwright()

if HERE not in sys.path:
    sys.path.insert(0, HERE)
from flake_retry import run_gate  # noqa: E402

GATES = []


def gate(name):
    def deco(fn):
        GATES.append((name, fn))
        return fn
    return deco


def sha_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha_file(path):
    with open(path, "rb") as f:
        return sha_bytes(f.read())


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def fetch(url, timeout=30):
    # HTTP 头名大小写不敏感（RFC 7230）：Python 3.14 的 http.server
    # 会把头名 canonical 化（X-AgentOS-Build -> X-Agentos-Build），
    # 逐字 dict 查找会漏掉真在的构建头 -> 门 1 假红。统一归一成
    # 小写键，门 1 按小写取，判据本身（serve.py 必须自报构建头）不变。
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
        return r.read(), {k.lower(): v for k, v in r.headers.items()}


def start_serve(port):
    """起一份独立端口的 serve.py（--tree 指假树，绝不碰真树）。"""
    fake = os.path.join(ROOT, "work", "_a11y_served_tree")
    os.makedirs(fake, exist_ok=True)
    srv = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "serve.py"),
         "--port", str(port), "--tree", fake, "--poll", "3600"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        if srv.poll() is not None:
            raise RuntimeError("serve.py 提前退出 rc=%s" % srv.returncode)
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/__health" % port, timeout=2).read()
            return srv
        except Exception:
            time.sleep(0.25)
    srv.terminate()
    raise RuntimeError("服务 20s 内没起来")


def same_build(served_bytes, disk_path):
    """门 1 的唯一真相：门与咬合牙共用这一个函数，免得两套判定各自漂移。"""
    got = sha_bytes(served_bytes)
    want = sha_file(disk_path)
    return got == want, got, want


@gate("真 served 页面：HTTP 取回的 index.html == 盘上那份（不是陈旧副本）")
def g_served_is_disk(ctx):
    body, hdrs = fetch(ctx["url"])
    ok, got, want = same_build(body, HTML)
    build_hdr = (hdrs.get("x-agentos-build") or "").strip()
    detail = "served=%s disk=%s build头=%s len=%d" % (
        got[:12], want[:12], build_hdr[:12] or "无", len(body))
    if not ok:
        return False, detail + " → served 与盘上不是同一 build"
    if not build_hdr:
        return False, detail + " → serve.py 未自报 X-AgentOS-Build"
    return True, detail


@gate("a11y_walk 走 --url 真跑绿，且确实落在 http://（不许悄悄退回 file://）")
def g_walk_over_http(ctx):
    keep = None
    if os.path.exists(WALK_REPORT):
        with open(WALK_REPORT, "rb") as f:
            keep = f.read()
        os.unlink(WALK_REPORT)     # 先删旧件，读不到新写的那份就判红
    try:
        r = subprocess.run(
            [sys.executable, os.path.join(HERE, "a11y_walk.py"), "--url", ctx["url"]],
            cwd=ROOT, capture_output=True, text=True, timeout=900)
        out = (r.stdout or "") + (r.stderr or "")
        tok = "A11Y_WALK OK" in out
        mode_ok = target_ok = False
        npass = ntot = -1
        if os.path.exists(WALK_REPORT):
            with open(WALK_REPORT, encoding="utf-8") as f:
                rep = json.load(f)
            mode_ok = rep.get("mode") == "served-http"
            target_ok = str(rep.get("target", "")).startswith("http://")
            g = rep.get("gates") or []
            ntot = len(g)
            npass = sum(1 for x in g if x.get("ok"))
        ok = (r.returncode == 0) and tok and mode_ok and target_ok
        detail = "rc=%d token=%s mode=%s target_http=%s %d/%d" % (
            r.returncode, tok, mode_ok, target_ok, npass, ntot)
        if not ok:
            detail += " | " + " / ".join(out.strip().splitlines()[-3:])
        return ok, detail
    finally:
        # 还原 file:// 那次跑的 report：本门只追加 served 侧的证据，
        # 不许把 a11y_walk.json 的 mode 改成 served-http 影响后续门的判读。
        if keep is not None:
            with open(WALK_REPORT, "wb") as f:
                f.write(keep)


@gate("咬合牙：改过字节的 served 源必须被同一段判据判红（证明门 1 不是恒绿）")
def g_teeth_stale_source(ctx):
    shutil.rmtree(STALE_TREE, ignore_errors=True)
    os.makedirs(STALE_TREE)
    with open(HTML, encoding="utf-8") as f:
        src = f.read()
    # 只动一个字节、且位置与语义无关：首个 </title> 的 o 换成 0
    mutated = src.replace("</title>", "</titl0>", 1)
    if mutated == src:
        shutil.rmtree(STALE_TREE, ignore_errors=True)
        return False, "变异体没造出来（index.html 里找不到 </title>）"
    with open(os.path.join(STALE_TREE, "index.html"), "w", encoding="utf-8") as f:
        f.write(mutated)
    port = free_port()
    try:
        handler = lambda *a, **k: SimpleHTTPRequestHandler(  # noqa: E731
            *a, directory=STALE_TREE, **k)
        srv = HTTPServer(("127.0.0.1", port), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            body, _ = fetch("http://127.0.0.1:%d/index.html" % port)
        finally:
            srv.shutdown()
            srv.server_close()
    finally:
        shutil.rmtree(STALE_TREE, ignore_errors=True)
    ok, got, want = same_build(body, HTML)
    if ok:
        return False, "变异体被判成同一 build（门 1 没牙）served=disk=%s" % got[:12]
    return True, "变异体被判红：served=%s disk=%s（门 1 有牙）" % (got[:12], want[:12])


@gate("活的 index.html 零写入：跑完 sha256 与跑前一致（本门不碰活页面）")
def g_live_untouched(ctx):
    after = sha_file(HTML)
    return after == ctx["sha_before"], "前=%s 后=%s" % (ctx["sha_before"][:12],
                                                      after[:12])


def main():
    ctx = {"url": None, "sha_before": sha_file(HTML)}
    port = free_port()
    ctx["port"] = port
    ctx["url"] = "http://127.0.0.1:%d/index.html" % port
    report = {"tool": "a11y_served.py", "port": port,
              "method": "real-http-served + playwright-real-keyboard + cdp-ax-tree",
              "gates": []}
    try:
        srv = start_serve(port)
    except Exception as e:
        print("FAIL: 服务没起来 %s" % e)
        return 2
    try:
        npass = 0
        for name, fn in GATES:
            ok, detail = run_gate(fn, ctx, name)
            report["gates"].append({"name": name, "ok": bool(ok), "detail": detail})
            print("  %s %s  %s" % ("[ok  ]" if ok else "[FAIL]", name, detail))
            npass += bool(ok)
        total = len(GATES)
        report["passed"], report["total"] = npass, total
        with open(os.path.join(ROOT, "work", "a11y_served.json"), "w") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        print("== A11Y_SERVED %s  %d/%d 门通过（真 http:// 服务页，非 file://）=="
              % ("OK " if npass == total else "FAIL", npass, total))
        return 0 if npass == total else 2
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()


if __name__ == "__main__":
    sys.exit(main())
