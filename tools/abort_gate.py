#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D76 中止能力上牙：POST /__act/abort 必须按 run id 真杀**整个进程组**，rc 记 130 不记成功。

痛点（改动前实测物证）：`grep -c "abort|AbortController|/stop|cancel|取消|中止|停止"`
在 tools/serve.py 与 index.template.html 上都是 0 —— 页面**没有任何停止入口**；
真键盘 Esc 与 ^C 之后 /__health.act_running 仍是 livecheck，用户最长干等 180s。

三组判据：
  A 契约组（纯 HTTP，不起进程）：缺 / 空 / None / 非字符串 run -> 400；
    不存在的 run -> 409；无 CSRF 头 -> 403；全拒完无残留状态。
  B 真进程组：门在**自己进程里** import serve.py 并起一个内存动作
    （父 bash 忽略 SIGTERM + 3s 后才落标记的孙 bash），因此断言的是
    错 run id 不杀、真 abort 200 且 sigkill=True、rc=130 且 ok=False、
    父进程死、**孙进程也没落标记**（整组被杀，不是只杀父）、重复 abort 409、
    中止后单飞锁释放、run id 每次不同。
    生产 allowlist 一个字都不动 —— 探针动作只活在门自己的内存里。
  C 上牙组：把三处判据改坏（只杀父进程 / 中止记成功 / 无视 run id 盲杀）
    门必须**抓住**（对应 guard 变 False）。

另有一条对照场景（control）：不中止地跑同一个探针，父/孙两个 pid 都必须活着，
且对照组自己收场的 killpg 也必须把孙打死 ——
否则 B 组的「孙进程死了」可能是探针本身写错路径的空绿。

用法：
  python3 tools/abort_gate.py                                  # 父进程，跑 C0/A+B/C
  python3 tools/abort_gate.py --child S.py PORT MARK PIDF       # 子进程，跑一次场景
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SERVE_PY = os.path.join(HERE, "serve.py")
SELF = os.path.abspath(__file__)
RC = 0

# 门用探针动作：父 bash `trap "" TERM` 忽略 SIGTERM（逼出 3s 宽限后的 SIGKILL 整组路径），
# 它再起一个孙 bash —— 孙也忽略 SIGTERM，父子各把自己的 pid 写进 pid 文件。
# 判据是**孙的 pid 活不活**，不是「孙有没有跑完某件事」：
# 后者会被 3s 宽限期骗（孙的活如果在宽限期内干完，真杀整组也会留下痕迹），
# 这类「靠时序的空绿」是本门第一版真踩过的坑，2026-10-04 已改掉。
PROBE_SCRIPT = ('trap "" TERM; echo $$ > "$D76_PIDF"; '
                'bash -c \'trap "" TERM; echo $$ > "$D76_PIDF2"; sleep 60\' & '
                'sleep 60')
FAST_SCRIPT = "echo d76-fast-ok"


def check(name, cond, detail=""):
    global RC
    if not cond:
        RC = 1
    tail = ("  <- " + str(detail)) if detail else ""
    print("  [%s] %s%s" % ("ok  " if cond else "FAIL", name, tail))


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True


def wait_dead(pid, timeout=6.0):
    """轮询等 pid 真死（孤儿被 init 收尸前可能短暂是僵尸，所以要轮询不能一次判）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    return not pid_alive(pid)


def read_pid(path):
    try:
        return int(open(path).read().strip())
    except (OSError, ValueError):
        return None


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def load_serve(path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("d76_serve_child", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def inject(mod):
    """把两个门用动作注入**内存**的 ACTIONS：生产 allowlist 一个字都不动。"""
    mod.ACTIONS["__d76_probe__"] = ("门用慢动作（父忽略 TERM）",
                                    ["bash", "-c", PROBE_SCRIPT], 120)
    mod.ACTIONS["__d76_fast__"] = ("门用快动作", ["bash", "-c", FAST_SCRIPT], 30)
    return mod


class Client:
    """门内最小 HTTP 客户端：只补 CSRF 头，其余语义与浏览器一致。"""

    def __init__(self, port):
        self.base = "http://127.0.0.1:%d" % port

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=15) as r:
                return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as e:
            return e.code, {}

    def post(self, path, obj, csrf=True):
        body = json.dumps(obj).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if csrf:
            req.add_header("X-AgentOS-Action", "1")
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw or "{}")
            except ValueError:
                return e.code, {"raw": raw}

    def health(self):
        return self.get("/__health")[1]


class Gate:
    """guard 累积器：每条判据都记 True/False，失败时留 why，父进程据此判红。"""

    def __init__(self):
        self.g = {}
        self.why = {}

    def add(self, name, cond, detail=""):
        try:
            v = bool(cond)
        except Exception as e:                       # noqa: BLE001 门要报失败，不是崩
            v = False
            detail = "%s | EXC %r" % (detail, e)
        self.g[name] = v
        if not v:
            self.why[name] = str(detail)[:400]


def run_child(serve_path, port, mark, pidf, do_abort=True):
    """在门自己的进程里 import serve.py、起内存探针动作、跑一遍 A/B 两组。"""
    pidf2 = pidf + "2"            # 孙进程的 pid 文件：判「孙死没死」用它，不再等时序标记
    os.environ["D76_MARK"] = mark
    os.environ["D76_PIDF"] = pidf
    os.environ["D76_PIDF2"] = pidf2
    mod = inject(load_serve(serve_path))
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", port), mod.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever,
                     kwargs={"poll_interval": 0.2}, daemon=True).start()
    cl = Client(port)
    gt = Gate()

    # --- A 契约组：无进程可杀，纯拒 ---------------------------------
    c, d = cl.post("/__act/abort", {})
    gt.add("a1_missing_run_400", c == 400, "%s %s" % (c, d))
    c, d = cl.post("/__act/abort", {"run": ""})
    gt.add("a2_empty_run_400", c == 400, "%s %s" % (c, d))
    c, d = cl.post("/__act/abort", {"run": None})
    gt.add("a3_null_run_400", c == 400, "%s %s" % (c, d))
    c, d = cl.post("/__act/abort", {"run": 12345})
    gt.add("a4_nonstr_run_400", c == 400, "%s %s" % (c, d))
    c, d = cl.post("/__act/abort", {"run": "rdeadbeefcafe"})
    gt.add("a5_unknown_run_409", c == 409, "%s %s" % (c, d))
    c, d = cl.post("/__act/abort", {"run": "rdeadbeefcafe"}, csrf=False)
    gt.add("a6_no_csrf_403", c == 403, "%s %s" % (c, d))
    ha = cl.health()
    ok = ha.get("act_running") is None and not ha.get("act_aborted")
    gt.add("a7_no_residue", ok, ha)

    if not do_abort:
        # 对照场景：真起同一个探针，但**不中止** —— 父与孙两个 pid 都必须活着。
        # 没有这一条，B 组的「孙进程死了」可能是探针本身没跑起来/路径写错的空绿。
        act = {}

        def runner_ctl():
            act["res"] = cl.post("/__act", {"action": "__d76_probe__"})

        thc = threading.Thread(target=runner_ctl, daemon=True)
        thc.start()
        seen_pid = False
        t0 = time.time()
        while time.time() - t0 < 20:
            if os.path.exists(pidf):
                seen_pid = True
                break
            time.sleep(0.2)
        gt.add("control_parent_started", seen_pid, pidf)
        ctl_pid = read_pid(pidf)
        t2 = time.time()
        while time.time() - t2 < 15 and not os.path.exists(pidf2):
            time.sleep(0.2)
        gt.add("control_grandchild_started", os.path.exists(pidf2), pidf2)
        gc_pid = read_pid(pidf2)
        gt.add("control_grandchild_alive", gc_pid and pid_alive(gc_pid),
               "gc_pid=%r" % gc_pid)
        # 收场：探针会睡 60s，不杀就白留后台进程。
        try:
            with mod._act_proc["lock"]:
                p_ctl = mod._act_proc["p"]
            if p_ctl is not None and p_ctl.poll() is None:
                os.killpg(os.getpgid(p_ctl.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError, AttributeError):
            pass
        # 收场本身也要上牙：killpg 之后孙必须真死，否则对照组在漏后台进程。
        gt.add("control_reaped", gc_pid and wait_dead(gc_pid, 6.0),
               "gc_pid=%r" % gc_pid)
        srv.shutdown()
        srv.server_close()
        return gt.g, gt.why

    # --- B 真进程组：真起慢动作、真杀 ---------------------------------
    act = {}

    def runner():
        act["res"] = cl.post("/__act", {"action": "__d76_probe__"})

    th = threading.Thread(target=runner, daemon=True)
    th.start()
    run_id = None
    t0 = time.time()
    while time.time() - t0 < 30:
        h = cl.health()
        # 必须等探针 bash **真把 pid 写下来**才允许发中止（2026-10-04 修的真 bug）：
        # /__health 的 act_run 在 stream_start 就有了，比 Popen 早，而 bash 装
        # trap "" TERM 还要几十毫秒。只等 act_run 的话，中止会打死一个还没装
        # trap 的 bash —— 它吃默认 SIGTERM 直接退，sigkill=False、pid 文件还没写，
        # 于是 b5/b9/b10 假红；m3 变异体那边则撞上「process already gone」的 409，
        # 错 id 也返回 409 -> b2 仍 True -> 门误报自己没牙。
        if h.get("act_running") == "__d76_probe__" and h.get("act_run") \
                and os.path.exists(pidf) and os.path.exists(pidf2):
            run_id = h["act_run"]
            break
        time.sleep(0.25)
    gt.add("b1_runid_acquired", run_id, "run_id=%r 探针就绪=%s/%s" % (
        run_id, os.path.exists(pidf), os.path.exists(pidf2)))

    c, d = cl.post("/__act/abort", {"run": "rdeadbeefcafe"})
    gt.add("b2_wrong_run_rejected", c == 409, "%s %s" % (c, d))
    hb = cl.health()
    gt.add("b3_wrong_run_no_kill", hb.get("act_running") == "__d76_probe__",
           "act_running=%r" % hb.get("act_running"))

    t1 = time.time()
    if run_id:
        c, d = cl.post("/__act/abort", {"run": run_id})
    else:
        c, d = 0, {"error": "no run id"}
    dt = time.time() - t1
    gt.add("b4_abort_ok", c == 200 and d.get("ok") is True, "%s %s" % (c, d))
    gt.add("b5_sigkill", d.get("sigkill") is True, d)
    gt.add("b6_abort_fast", dt < 20.0, "%.1fs" % dt)

    th.join(timeout=40)
    res = act.get("res", (0, {}))
    body = res[1] if isinstance(res[1], dict) else {}
    gt.add("b7_rc130", body.get("rc") == 130, body)
    gt.add("b8_not_ok", body.get("ok") is False, body)

    parent_pid = read_pid(pidf)
    gt.add("b9_parent_dead", parent_pid and not pid_alive(parent_pid),
           "parent_pid=%r" % parent_pid)

    gc_pid = read_pid(pidf2)
    gt.add("b10_grandchild_dead", gc_pid and wait_dead(gc_pid, 6.0),
           "gc_pid=%r" % gc_pid)

    if run_id:
        c, d = cl.post("/__act/abort", {"run": run_id})
    else:
        c, d = 0, {}
    gt.add("b11_repeat_409", c == 409, "%s %s" % (c, d))

    c, d = cl.post("/__act", {"action": "__d76_fast__"})
    gt.add("b12_lock_released", c == 200 and d.get("rc") == 0, "%s %s" % (c, d))
    gt.add("b13_runid_unique", d.get("run") and d.get("run") != run_id,
           "%r vs %r" % (d.get("run"), run_id))

    srv.shutdown()
    srv.server_close()
    return gt.g, gt.why


def spawn_child(serve_path, tag, do_abort=True):
    """把一次场景丢进**独立进程**（端口/模块/环境全隔离），返回 (guards, why, err)。"""
    mark = "/tmp/_d76_mark_%s_%d" % (tag, os.getpid())
    pidf = "/tmp/_d76_pid_%s_%d" % (tag, os.getpid())
    for p in (mark, pidf, pidf + "2"):
        if os.path.exists(p):
            os.unlink(p)
    port = free_port()
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    argv = [sys.executable, SELF, "--child", serve_path, str(port), mark, pidf]
    if not do_abort:
        argv.append("--no-abort")
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=300,
                           env=env)
    except subprocess.TimeoutExpired:
        return {}, {}, "child timeout"
    line = ""
    for ln in (r.stdout or "").splitlines():
        if ln.startswith("@@JSON@@"):
            line = ln[len("@@JSON@@"):]
    if not line:
        return {}, {}, (r.stderr or "")[-500:] or "no @@JSON@@ rc=%d" % r.returncode
    d = json.loads(line)
    guards = d.get("g") or {}
    whys = d.get("why") or {}
    # 变异体（只杀父进程）会故意留活孙进程 —— 门自己必须收尸，
    # 否则每跑一轮门就在这台机器上漏一批 sleep 60。
    for p in (pidf, pidf + "2"):
        lp = read_pid(p)
        if lp and pid_alive(lp):
            try:
                os.kill(lp, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
    for p in (mark, pidf, pidf + "2"):
        if os.path.exists(p):
            os.unlink(p)
    return guards, whys, ""


# 上牙组：每个变异体只钉**一个**判据 —— 必须由它变 False 证明门有牙。
MUTANTS = [
    ("m1_parent_only_kill",
     [('        os.killpg(pgid, signal.SIGTERM)',
       '        p.send_signal(signal.SIGTERM)'),
      ('            os.killpg(pgid, signal.SIGKILL)',
       '            p.kill()')],
     "b10_grandchild_dead", "只杀父进程 -> 孙进程还活着（整组杀才算真中止）"),
    ("m2_abort_counted_green",
     [('    if _act_cur["aborted"]:      # 中止不是成功：rc 归一到 130（SIGINT 惯例）',
       '    if False:      # 中止不是成功：rc 归一到 130（SIGINT 惯例）')],
     "b7_rc130", "中止不归一到 130 -> 回执 rc 是 -9"),
    ("m3_blind_kill",
     [('    if cur != run_id:', '    if False:')],
     "b2_wrong_run_rejected", "无视 run id -> 错 id 也真杀"),
]


def main():
    src = open(SERVE_PY, encoding="utf-8").read()

    print("== C0. 对照场景：探针父子都活着、收场也收得掉（防空绿）==")
    cg, _, cerr = spawn_child(SERVE_PY, "control", do_abort=False)
    check("对照场景跑出 JSON", bool(cg), cerr)
    if cg:
        check("不中止时父/孙两个 pid 都活着（探针非空）",
              cg.get("control_parent_started") is True
              and cg.get("control_grandchild_started") is True
              and cg.get("control_grandchild_alive") is True, cg)
        check("对照组自己的 killpg 也把孙收干净",
              cg.get("control_reaped") is True, cg)

    print("== A+B. 真 serve.py：契约组 + 真进程组 ==")
    g, why, err = spawn_child(SERVE_PY, "real")
    check("场景跑出 JSON", bool(g), err)
    if not g:
        print("== D76 中止能力上牙 FAIL（场景没跑起来）==")
        return 1
    for k in sorted(g):
        check(k, g[k], why.get(k, ""))

    print("== C. 上牙组：三处判据改坏必须被抓住 ==")
    for name, edits, key, why_txt in MUTANTS:
        body = src
        bad = False
        for old, new in edits:
            n = body.count(old)
            if n != 1:
                check("%s 变异点对码（%r 命中 %d 次）" % (name, old.strip()[:36], n),
                      False, "源码漂移，门必须跟着改")
                bad = True
                break
            body = body.replace(old, new, 1)
        if bad:
            continue
        mp = "/tmp/_d76_mut_%s_%d.py" % (name, os.getpid())
        with open(mp, "w", encoding="utf-8") as f:
            f.write(body)
        try:
            mg, _, merr = spawn_child(mp, name)
        finally:
            if os.path.exists(mp):
                os.unlink(mp)
        if not mg:
            check("%s 场景跑出 JSON" % name, False, merr)
            continue
        caught = mg.get(key) is False
        check("%s 被抓住（%s=False）：%s" % (name, key, why_txt), caught,
              "" if caught else "该判据仍为 True -> 门没牙")

    print("== D76 中止能力上牙 %s ==" % ("OK" if RC == 0 else "FAIL"))
    return RC


if __name__ == "__main__":
    _a = sys.argv[1:]
    if _a and _a[0] == "--child":
        _g, _w = run_child(_a[1], int(_a[2]), _a[3], _a[4],
                           do_abort=("--no-abort" not in _a))
        print("@@JSON@@" + json.dumps({"g": _g, "why": _w}, ensure_ascii=False))
        sys.exit(0)
    sys.exit(main())
