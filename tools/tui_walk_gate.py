#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tui_walk_gate —— 关于 tools/tui_walk.py 的门：BASE 必须绿，两个假 CLI 桩必须红。

为什么必须有这个门
------------------
tui_walk.py 是终端侧唯一的真走查：真 PTY、真按键、按剥 ANSI 后的文本逐键判定
13 视图。它自己返回 0/2，但「会返回 0」不等于「判得对」—— 一个从没被反向验过
的走查工具，它的绿只是自报。本门补上这一层：把两个**装成 TUI 的假 CLI 桩**
从 --cmd 这条唯一的覆写缝喂进去（tui_walk.py:main 的 --cmd 就是为门留的），
要求它们必须被判红。桩只落 /tmp 临时目录，不落仓库。

判据（双向，缺一条即 rc=2）：
  BASE  生产 CLI（`python3 tools/agentos.py tui --url <本门自起的新服务>`）
         必须 rc=0，
         且 JSON: ok=true、键全绿且 ≥14 键、13 视图全访并与 VIEWS 常量一致、
         无 fail-closed、无薄壳、reader_at 诚实等于 AT-UNVERIFIED。
  M1    首帧后立即退出的桩（任何键都不响应）必须 rc≠0。
  M2    恒一屏的桩（按键不换页，永远重绘第 1 面）必须 rc≠0。
         只断言 rc≠0 是不够的：量具自己崩掉也给非 0，那是量具死了不是判据红。
         所以同时要求 doc.ok=false、views_visited<views_expected、至少一键 FAIL。
零污染 活 work/tui_walk.json 本轮新鲜（BASE 写的）+ tools/ 下无 _tuiwalkmut_ 残留。
         桩与变异输出只写各自的 --out 临时目录，BASE 的 --out 是生产路径，
         两者结构上不共享 —— 承重判据是「临时目录之外的东西没被碰」。

AT-UNVERIFIED 是这一层的诚实口径：macOS VoiceOver 的 TCC 权限沙箱进程授予不了。
BASE 判据要求 reader_at **等于** AT-UNVERIFIED —— 走查工具哪天改成自称验过
OS 读屏，本门立刻红。这是「不许假装验过」的可执行版本。

用法: python3 tools/tui_walk_gate.py      # rc=0 全过 / 2 有漏网
"""
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WALK = os.path.join(HERE, "tui_walk.py")
ENTRY = os.path.join(HERE, "agentos.py")
OUT_JSON = os.path.join(ROOT, "work", "tui_walk.json")
GATE_JSON = os.path.join(ROOT, "work", "tui_walk_gate.json")
AT = "AT-UNVERIFIED"
MIN_KEYS = 14                 # ACCEPT T2/T3：键数下限，低于它走查没覆盖到契约
MUT_PREFIX = "_tuiwalkmut_"   # 与 _envmut_/_webmut_/_psmut_ 同一套命名约定
INSTR_RETRY_N = 2             # 量具无结论时的重试次数（重试量具，不是重试判据）
INSTR_RETRY_S = 3.0

results = []


def gate(name, ok, detail=""):
    results.append({"gate": name, "ok": bool(ok), "detail": detail})
    print(("  [ok  ] " if ok else "  [FAIL] ") + name +
          (("  " + detail) if detail else ""))


def notes(doc):
    """一行可判真假的量纲：键数、视图数、子进程 rc。红的时候靠它对账。"""
    g = (doc or {}).get("gates") or {}
    return "keys=%s/%s views=%s/%s child_rc=%s" % (
        g.get("keys_matched"), g.get("keys_total"),
        g.get("views_visited"), g.get("views_expected"),
        (doc or {}).get("child_rc"))


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def start_fresh_server():
    """本门自己起一个**新** serve（free_port 现拿端口），等 health 起来。

    为什么不用零参数打常驻 8790：常驻那个进程可能已经跑了几天、状态退化
    （实测同一套 CLI：打常驻 8790 十三视图 75s，打新起的 3s —— 慢了 25 倍，
    每个面 5.8s）。那样量具测的是「某个跑了七天的进程有多脏」，不是产品
    面板本身。服务端健康由 serve/perf 门负责，这里只管把 UI 测准。
    返回 (proc, url)；起不来返回 (None, "")。
    """
    import socket
    import urllib.request
    srv = os.path.join(HERE, "serve.py")
    sk = socket.socket()
    sk.bind(("127.0.0.1", 0))
    port = sk.getsockname()[1]
    sk.close()
    proc = subprocess.Popen(
        [sys.executable, srv, "--port", str(port)],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = "http://127.0.0.1:%d" % port
    t0 = time.time()
    while time.time() - t0 < 25:
        if proc.poll() is not None:
            return None, ""
        try:
            with urllib.request.urlopen(url + "/__health", timeout=2) as r:
                if r.status == 200:
                    return proc, url
        except Exception:  # noqa: BLE001  还没起来，继续等
            time.sleep(0.2)
    return None, ""


def walk_with_retry(argv, out_path):
    """跑一次 tui_walk，返回 (rc, stdout, stderr, doc, 重试次数)。

    「量具无结论」的判据是 stdout 里没有 TUI_WALK 汇总行（子进程被 SIGTERM /
    起不来时会发生），这时重试的是量具本身；判据一次都没被放水。
    """
    def _once():
        p = subprocess.run([sys.executable, WALK] + argv, cwd=ROOT,
                           capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr

    rc, out, err = _once()
    n = 0
    while "TUI_WALK" not in out and n < INSTR_RETRY_N:
        n += 1
        time.sleep(INSTR_RETRY_S)
        rc, out, err = _once()
    return rc, out, err, load_json(out_path), n


# ── 假 CLI 桩：装成 TUI，但各有一处真 TUI 不会有的行为 ────────────────────
# 帧按 tui_walk.py 的解析器构造（footer「视图 1/13」+ tab 栏 [1 总览] + ─ 分隔线
# + body 一行 + 首行时钟）。首帧必须能被解析出来，否则红的不是「桩装得不像」而是
# 「量具读不到帧」，那证明力差一截：本门要的是「桩看起来像 TUI 却仍然被抓住」。
FRAME = (
    "\x1b[2J\x1b[H"
    "AgentOS 驾驶舱  12:00:00\n"
    "[1 总览] [2 轮次] [3 阶段矩阵] [4 证据账本] [5 引擎]\n"
    "[6 Agent 台] [7 目标台] [8 复盘台] [9 能力地图]\n"
    "\n"
    + ("\u2500" * 40) + "\n"
    "stub 面板：这一行是给 body_block 凑字符数用的假数据，长度够三十字符以上\n"
    "视图 1/13 · 数据源: stub（不是真数据）\n"
)

STUB_QUIT = (
    "import sys\n"
    "F = %s\n"
    "sys.stdout.write(F)\n"
    "sys.stdout.flush()\n"
) % (repr(FRAME),)

STUB_FREEZE = (
    "import sys\n"
    "F = %s\n"
    "sys.stdout.write(F)\n"
    "sys.stdout.flush()\n"
    "while True:\n"
    "    b = sys.stdin.read(1)\n"
    "    if not b:\n"
    "        break\n"
    "    sys.stdout.write(F)\n"
    "    sys.stdout.flush()\n"
) % (repr(FRAME),)


def write_stub(path, src):
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    return path


def mutant_caught(doc):
    """变异体被抓到的谓词（纯函数，方便单独自证，见 main 里的双向咬合）。

    假阳性（把 BASE 判成抓到）= 门乱红；假阴性（放过装成的桩）= 空门。
    """
    if not isinstance(doc, dict) or doc.get("ok") is not False:
        return False
    g = doc.get("gates") or {}
    keys_fail = (g.get("keys_matched") or 0) < (g.get("keys_total") or 0)
    views_fail = (g.get("views_visited") or 0) < (g.get("views_expected") or 0)
    return bool(keys_fail or views_fail)


def _caught(fn, arg, expected):
    """变异谓词在 arg 上给出**错误**结论 = 它被咬住了。"""
    return (fn(arg) is expected) is False


def main():
    t0 = time.time()
    if not os.path.exists(WALK):
        print("FAIL: tools/tui_walk.py 不存在（本门没有可验的对象）")
        return 2
    if not os.path.exists(ENTRY):
        print("FAIL: tools/agentos.py 不存在")
        return 2
    try:
        tree = ast.parse(open(ENTRY, encoding="utf-8").read())
    except Exception as e:                                  # noqa: BLE001
        print("FAIL: tools/agentos.py 解析失败 %r" % (e,))
        return 2
    views_const = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "VIEWS":
                    views_const = [str(x)
                                   for x in ast.literal_eval(node.value)]
    if not views_const:
        print("FAIL: 从 tools/agentos.py 取不到 VIEWS 常量")
        return 2
    nviews_const = len(views_const)

    td = tempfile.mkdtemp(prefix="tuiwalk-gate-")
    stubs = {"M1": os.path.join(td, "stub_quit.py"),
             "M2": os.path.join(td, "stub_freeze.py")}
    srv_proc = None
    try:
        write_stub(stubs["M1"], STUB_QUIT)
        write_stub(stubs["M2"], STUB_FREEZE)

        # ---------- BASE：生产 CLI 打在本门自起的新服务上，必须绿 ----------
        srv_proc, srv_url = start_fresh_server()
        base_argv = (
            ["--cmd", "%s %s tui --url %s" % (sys.executable, ENTRY, srv_url)]
            if srv_url else [])
        rc, out, err, doc, retried = walk_with_retry(base_argv, OUT_JSON)
        note = "量具无结论重试 %d 次；" % retried if retried else ""
        if "TUI_WALK" not in out:
            gate("BASE 量具有结论（stdout 有 TUI_WALK 汇总行）", False,
                 note + "stderr=%r" % (err.strip()[-160:],))
            return _finish(nviews_const)
        g = doc.get("gates") or {}
        gate("BASE 生产调用（本门新起服务 %s）rc=0 且汇总行 OK"
             % (srv_url or "起不来→退回默认 8790"),
             rc == 0 and "TUI_WALK OK" in out and doc.get("ok") is True,
             note + "rc=%s %s" % (rc, notes(doc)))
        keys_total = g.get("keys_total") or 0
        keys_matched = g.get("keys_matched") or 0
        gate("BASE 键全绿（%d/%d，契约下限 %d 键）"
             % (keys_matched, keys_total, MIN_KEYS),
             keys_total >= MIN_KEYS and keys_matched == keys_total,
             "缺的键=%s" % ([k.get("name") for k in doc.get("keys", [])
                            if not k.get("ok")][:6] or "无"))
        gate("BASE 13 视图全访且 tab 名与 VIEWS 常量逐项一致",
             (g.get("views_visited") == g.get("views_expected") == nviews_const
              and not g.get("views_tab_mismatch")),
             "visited=%s expected=%s VIEWS 常量=%d；tab 不符=%s"
             % (g.get("views_visited"), g.get("views_expected"),
                nviews_const, g.get("views_tab_mismatch") or "无"))
        gate("BASE 无 fail-closed 视图（数据源全是真的）",
             not g.get("views_fail_closed"),
             "fail-closed=%s" % (g.get("views_fail_closed") or "无"))
        gate("BASE 无薄壳视图（body 行数/字符达标）",
             not g.get("views_thin_body"),
             "薄壳=%s" % (g.get("views_thin_body") or "无"))
        gate("BASE reader_at 诚实（=AT-UNVERIFIED，不冒充 OS 读屏）",
             doc.get("reader_at") == AT and g.get("reader_at") == AT,
             "reader_at=%r" % (doc.get("reader_at"),))
        ev_bad = [k.get("name") for k in doc.get("keys", [])
                  if not (k.get("expect") and k.get("detail")
                          and k.get("sent") is not None)]
        gate("BASE 每键都有 expect+detail+sent 证据串（不是收到字节就算过）",
             not ev_bad and keys_total >= MIN_KEYS,
             "缺证据的键=%s" % (ev_bad[:6] or "无"))

        # ---------- M1 / M2：装成 TUI 的桩必须红 ----------
        for tag, why in (("M1", "首帧后立即退出、任何键都不响应的假 CLI"),
                         ("M2", "永远重绘同一屏、按键不换页的假 CLI")):
            mout = os.path.join(td, "out-%s.json" % tag)
            mrc, mout_txt, merr, mdoc, mretried = walk_with_retry(
                ["--cmd", "%s %s" % (sys.executable, stubs[tag]),
                 "--out", mout], mout)
            mnote = "量具无结论重试 %d 次；" % mretried if mretried else ""
            mg = (mdoc or {}).get("gates") or {}
            first_bad = next((k for k in (mdoc or {}).get("keys", [])
                              if not k.get("ok")), None)
            ok_mut = (mrc != 0 and (mdoc or {}).get("ok") is False
                      and ((mg.get("keys_matched") or 0)
                           < (mg.get("keys_total") or 0)
                           or (mg.get("views_visited") or 0)
                           < (mg.get("views_expected") or 0)))
            gate("%s %s必须红（rc≠0 且键或视图数不足）" % (tag, why),
                 ok_mut,
                 mnote + "rc=%s %s；首个 FAIL 键=%s"
                 % (mrc, notes(mdoc),
                    ("%s · %s" % (first_bad.get("name"),
                                  first_bad.get("detail"))
                     if first_bad else "无")))

        # ---------- 零污染 ----------
        fresh = (os.path.exists(OUT_JSON)
                 and os.path.getmtime(OUT_JSON) >= t0 - 1.0)
        gate("活 work/tui_walk.json 本轮由 BASE 写过（mtime 新鲜）", fresh,
             "mtime=%s 门起始=%s"
             % (time.strftime("%T", time.localtime(
                 os.path.getmtime(OUT_JSON)))
                if os.path.exists(OUT_JSON) else "缺失",
                time.strftime("%T", time.localtime(t0))))
        leftover = [f for f in sorted(os.listdir(HERE))
                    if f.startswith(MUT_PREFIX)]
        gate("tools/ 下无 %s* 残留（桩只落临时目录）" % MUT_PREFIX,
             not leftover, repr(leftover))

        # ---------- 判据自证：mutant_caught 正例 + 变异谓词双向咬合 ----------
        base_like = {"ok": True, "gates": {"keys_matched": 35,
                                           "keys_total": 35,
                                           "views_visited": nviews_const,
                                           "views_expected": nviews_const}}
        both_doc = {"ok": False, "gates": {"keys_matched": 12,
                                           "keys_total": 35,
                                           "views_visited": 1,
                                           "views_expected": nviews_const}}
        views_only = {"ok": False, "gates": {"keys_matched": 35,
                                             "keys_total": 35,
                                             "views_visited": 1,
                                             "views_expected": nviews_const}}
        pos = [("BASE 形 doc 判「没抓到」", mutant_caught(base_like) is False),
               ("键+视图双缺 doc 判「抓到」", mutant_caught(both_doc) is True),
               ("只缺视图 doc 判「抓到」", mutant_caught(views_only) is True)]
        muts = [("M1 恒真（把 BASE 也判成抓到）", lambda d: True,
                 base_like, False),
                ("M2 恒假（什么都抓不到）", lambda d: False,
                 both_doc, True),
                ("M3 只数键不数视图", lambda d: (
                    ((d.get("gates") or {}).get("keys_matched") or 0)
                    < ((d.get("gates") or {}).get("keys_total") or 0)),
                 views_only, True)]
        caught = [nm for nm, fn, a, exp in muts if _caught(fn, a, exp)]
        gate("判据自证：mutant_caught 3 正例 + 3 变异谓词双向咬合",
             all(o for _n, o in pos) and len(caught) == len(muts),
             "正例 %d/%d · 变异体咬住 %d/%d"
             % (sum(1 for _n, o in pos if o), len(pos),
                len(caught), len(muts)))
    finally:
        if srv_proc is not None and srv_proc.poll() is None:
            srv_proc.terminate()
            try:
                srv_proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                srv_proc.kill()
        shutil.rmtree(td, ignore_errors=True)
    return _finish(nviews_const)


def _finish(nviews_const):
    nfail = sum(1 for r in results if not r["ok"])
    try:
        with open(GATE_JSON, "w", encoding="utf-8") as f:
            json.dump({"tool": "tui_walk_gate.py",
                       "views_const": nviews_const,
                       "gates": results}, f, ensure_ascii=False, indent=1)
    except Exception as e:                                  # noqa: BLE001
        print("tui_walk_gate: 落盘失败 %r" % (e,), file=sys.stderr)
        return 3
    print("== TUI_WALK_GATE %s  %d/%d 门通过（BASE 真 PTY 走查 + 2 个变异体）=="
          % ("OK" if nfail == 0 else "FAIL", len(results) - nfail,
             len(results)))
    if nfail == 0:
        print("TUI_WALK_GATE-OK 2/2 例")
    return 0 if nfail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
