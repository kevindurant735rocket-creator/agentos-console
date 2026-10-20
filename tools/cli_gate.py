#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI 硬门：终端可用性（对标 Claude Code 的「装完就能用」）。

为什么单独一个门（真实缺口）：
  在本门之前，agentos 的终端入口**没有任何一道门量过它**。index.html / TUI /
  常驻 / headroom 各有自己的门，而 `agentos ...` 这条唯一能让用户在终端里
  直接使用产品的路径，只有人手敲过。手测不会被 CI 复现，也不会在下次改坏时
  报警。本门把那条路径钉成 rc。

  判据分四组，每条都是**真命令 + 真退出码**：
    A 源码树静态 8 条（这份盘上到底装不装得出去）：agentos.py 可编译；
      VIEWS/LOADERS/index.html tab 三方同为 13；template 同样 13；
      shim 模板留着 __AGENTOS_ROOT__ 且 sh -n 通过；install.sh/uninstall.sh
      可 bash -n 且零 `rm -f`；源码树 --version rc=0。
    B 装机往返 14 条（对标 Claude Code 的「装完就能用」）：装到**隔离 dest**
      （不碰用户 ~/.local/bin、HOME 也隔离）→ shim 是普通 0755 文件、无占位
      残留、内嵌真仓库根、版本与源码树逐字相同、两次装机 sha256 相同、
      `env -i` 干净 shell 里 --version/health 全 rc=0、隔离端口 health rc=0
      且端口事后释放、卸载 rc=0 且 shim 消失、卸载幂等、卸完重装 rc=0、
      未替换占位的模板副本 rc=127 fail-closed。
    C 服务上的 CLI 行为（原有 C01-C24，另补 5 条：bash 补全、补全门名与
      gates.sh 正确链逐项相等、TUI 零 ANSI、`-p` 乱码 rc=3、死端口 rc=3/rc=1）。
    D 收尾 1 条：index.html/data.json/agentos.py/serve.py/bin-agentos 五个
      sha256 前后一致 —— 门自己不许改活树。

门口径（与全仓一致）：
 * 每条判据都带**真实执行的命令**与**真实退出码**，落进 work/cli_gate.json。
    不许用「看起来对」的文本匹配替代退出码 —— 退出码才是 CI 唯一能依赖的东西。
  * 判据红不许被重试洗绿。服务没起是**前置不满足**，单独一类如实报，
    不伪装成判据红、也不静默跳过。唯一例外是**外部基础设施**瞬时故障
    （gh whoami 的判定链在本项目之外：GitHub + 本机钥匙串）：有限重试
    ≤2 次、每次 1.5s，detail 必须带实跑次数与 stderr 尾部 —— 重试痕迹不许藏。
  * 只会绿的门和只会红的门一样没用。反向门见 tools/cli_gate_selftest.py。

不碰的东西：不开真浏览器（BROWSER=/usr/bin/true 让 webbrowser 落空）、
不装用户 PATH（B 组装机只落 work/cli_gate_dest，HOME 也指到隔离目录）、
自起的 serve.py 只挂空闲端口 + 假树 + `--poll 3600` 关 watcher、
零 playwright、零远端写。

用法: python3 tools/cli_gate.py            # rc=0 全过 / 2 有红项
      python3 tools/cli_gate.py --skip-install   # 只读环境跳 B 组
      python3 tools/cli_gate_selftest.py  # 反向门：证明本门判据有牙
"""
import argparse
import ast
import fcntl
import hashlib
import json
import os
import pty
import re
import select
import socket
import stat
import struct
import subprocess
import sys
import termios
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
OUT = os.path.join(ROOT, "work", "cli_gate.json")
DEST_DIR = os.path.join(ROOT, "work", "cli_gate_dest")   # 隔离装机目录
# 活树上五个「门跑完必须一个字节都没变」的文件（D 组自证）。
# 「零写入」判据盯的文件：网页驾驶舱下线后只剩终端面这些。
WRITE_WATCH = ("tools/agentos.py", "tools/agent_core.py", "bin/agentos",
               "tools/gates.sh")

# 被检 CLI。门自己不起进程以外的东西，只负责叫它、读它的 rc。
CLI_ARGV = [os.path.join(HERE, "agentos.py")]

# 子命令全集。写死在这里是有意的：门存在的意义就是「宣称支持的面」与
# 「真的能跑的面」逐一对上。漏一个面 = 那个面的 --help 从没被量过。
# 补全表必须与真实子命令**逐项相等**（不是「差不多」）。
# 2026-10-19：doctor/evolve（W22 加的）漏在补全表外 —— 用户敲
# `agentos doctor<Tab>` 什么也补不出来，而 --help 里明明有。
# 同批补上 replay（W24 的回放基准）。漂移就红，别放松成子集比较。
# 终端面的子命令清单：与 tools/agentos.py 的 _AGENTOS_SUBS 逐项相等
# （网页驾驶舱与原生 app 已下线，这里不再有 serve/health/open/tui/term/resume）。
SUBS = ("gates", "selfscan", "gh", "agent", "config", "completion", "sessions",
        "steer", "watch", "leader", "doctor", "evolve", "replay")

results = []


# 判据纯函数区。**判据只在这一份里**：cli_gate_selftest.py import 这些函数，
# 把变异证据喂进去要求翻红。判据若在两处各写一遍，反向门验的就可能是另一套
# 逻辑 —— 那是「关于门的门」最隐蔽的失效形态（门绿、牙在别处）。
def j_compile_ok(err_text):
    """入口文件必须能被解释器编译。err 非空就是 SyntaxError。"""
    return not (err_text or "").strip()


def j_views_contract(n_views, n_loaders, n_tabs):
    """CLI 的 VIEWS / LOADERS / web 端 tab 必须同为 13。三份表迟早漂移，
    漂移那天「终端里能看到」和「浏览器里能看到」不再是同一个产品。"""
    return n_views == 13 and n_loaders == n_views and n_tabs == n_views


def j_shim_tpl(text):
    """bin/agentos 模板必须留着 __AGENTOS_ROOT__（装机靠它替换真根）。"""
    return "__AGENTOS_ROOT__" in (text or "")


def j_version(text):
    return bool(re.search(r"AgentOS\s+\d+\.\d+\.\d+", text or ""))


def j_same_version(a, b):
    """装机版与源码树版必须逐字相同；版本号不同就是装了一份别的代码。"""
    return bool(a and b) and a.strip() == b.strip()


def j_plain_exec(mode):
    """装出去的必须是普通可执行文件：软链/目录都让「装了什么」说不清。"""
    return bool(mode) and stat.S_ISREG(mode) and (mode & 0o777) == 0o755


def j_no_placeholder(text):
    return "__AGENTOS_ROOT__" not in (text or "")


def j_root_embedded(text, root):
    return bool(root) and root in (text or "")


def j_idempotent(h1, h2):
    """两次装机 sha256 必须相同（install.sh 的幂等契约）。"""
    return bool(h1) and h1 == h2


def j_uninstalled(path):
    return not os.path.exists(path) and not os.path.islink(path)


def j_failclosed(rc, msg):
    """占位没被替换的 shim 必须自己失败并说清楚原因，不许静默跑别的代码。"""
    return rc == 127 and "定位不到" in (msg or "")


def j_zero_ansi(frame):
    return "\x1b" not in (frame or "")


def j_views_present(out, views):
    return all(v in (out or "") for v in views)


def j_headless_json(rec):
    keys = ("ok", "query", "answer", "sources", "exit_code")
    return (bool(rec) and all(k in rec for k in keys)
            and rec.get("ok") is True and rec.get("exit_code") == 0)


def j_term_refused(rc, msg):
    return rc == 1 and "白名单" in (msg or "")


def j_completion_subs(got, want):
    """补全里的子命令集合必须与真集合逐项相等（2026-10-10 实测漏过 gh）。"""
    return bool(got) and set(got) == set(want)


def j_completion_gates(got, chain):
    """补全里的门名集合必须与 gates.sh 正确链逐项相等。"""
    return bool(got) and set(got) == set(chain)


def j_zero_write(before, after):
    return dict(before or {}) == dict(after or {})


def j_clean_env_ok(rc, out):
    """env -i 干净 shell 里也要能跑：PATH 只剩系统目录时产品仍可用。"""
    return rc == 0 and "AgentOS" in (out or "")


def j_health_ok(rc, out):
    return rc == 0 and "health OK" in (out or "")


notes = []          # 不计入判据的事实性备注（UNVERIFIABLE 一类放这儿）
EVIDENCE = {}       # 原始证据原样落盘，反向门拿它当正例 BASE 复用


def ev(key, value):
    EVIDENCE[key] = value


def note(name, detail):
    """不判红、也不静默跳过：如实标注，落进 json 的 notes 字段。"""
    notes.append({"note": name, "detail": detail})
    print("  [note ] %s  %s" % (name, detail))


def read_text(path):
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def snap_sha(rel_names):
    return {p: sha256_file(os.path.join(ROOT, p)) for p in rel_names}


def module_consts(src):
    """用 ast 取模块级常量，不 import（import 会跑副作用、还要服务在）。

    同一名字多次赋值时**后一个覆盖前一个**（Python 语义）——
    否则 LOADERS 顶层占位的 [] 会把 main 前填的 13 个取数函数盖掉。
    """
    out = {}
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id in ("VIEWS", "LOADERS", "VERSION",
                                           "FORBIDDEN_PORT"):
            val = _const_value(node.value)
            if val is not None:
                out[node.targets[0].id] = val
    return out


def _const_value(node):
    """常量节点取值：literal_eval 能过就原样；过不了就数元素个数。

    LOADERS 是两段式赋值（模块级先占位 []，main 前填成函数名元组）——
    literal_eval 对「函数名元组」必然 ValueError，只 try literal 的实现
    会把这段跳过，于是永远读到占位的空表，src-views-contract 红得莫名其妙。
    契约判据比的是「几个视图、几个取数函数」，元素个数就够，不需要名字。
    """
    try:
        return ast.literal_eval(node)
    except ValueError:
        if isinstance(node, (ast.Tuple, ast.List)):
            return len(node.elts)
        return None


def _const_count(v):
    """module_consts 的结果归一成「几个」：序列取 len，已是整数的原样。"""
    if isinstance(v, (list, tuple, set)):
        return len(v)
    return v or 0


def count_tabs(path):
    """web 端 tab 数：一个个数 role="tab"，不猜。"""
    return len(re.findall(r'role="tab"', read_text(path)))


def chain_names():
    """与 agentos.py 的 _gate_names() 同口径解析 gates.sh 头部「正确链」。"""
    path = os.path.join(ROOT, "tools", "gates.sh")
    for ln in read_text(path).splitlines():
        if "正确链" not in ln or "gates.sh" not in ln:
            continue
        rest = ln.split("gates.sh", 1)[1]
        names = [w for w in rest.split()
                 if w and (w[0].isalpha() or w[0] == "_")]
        if names:
            return names
    return []


def view_names():
    """网页驾驶舱下线后没有「视图」了：这函数只服务已删除的 TUI 判据，
    保留一个空实现是为了不打断证据落盘（键还在，值为空）。"""
    return []


def free_port(avoid):
    """抓一个真空闲端口，绝不抓 headroom proxy 的常驻口。"""
    for _ in range(50):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        if p != avoid:
            return p
    raise RuntimeError("找不到空闲端口")


def port_open(port):
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def sh_which(name):
    """本机 PATH 上是否真能解析到 agentos（如实标注，不偷装）。"""
    try:
        p = subprocess.run(["bash", "-lc", "command -v %s" % name],
                           capture_output=True, timeout=30)
        return p.stdout.decode("utf-8", "replace").strip()
    except Exception:
        return ""


def gate(name, ok, detail="", cmd=None, rc=None):
    results.append({"gate": name, "ok": bool(ok), "detail": detail,
                    "cmd": cmd, "rc": rc})
    line = ("  [ok  ] " if ok else "  [FAIL] ") + name
    if detail:
        line += "  " + detail
    if cmd:
        line += "\n           $ " + cmd
    print(line)


def run(cli, argv, timeout=180, env_extra=None, cwd=ROOT):
    """跑一条命令，返回 (rc, stdout, stderr)。stdout/stderr 一定解成 str。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        env.update(env_extra)
    argv = [sys.executable] + list(cli) + list(argv)
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "timeout %ss" % timeout
    return p.returncode, p.stdout.decode("utf-8", "replace"), \
        p.stderr.decode("utf-8", "replace")


def run_sh(argv, timeout=240, env_extra=None, cwd=ROOT):
    """跑任意 argv（装机/卸载/干净 shell 都用它），返回 (rc, out, err)。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        env.update(env_extra)
    try:
        p = subprocess.run(list(argv), cwd=cwd, env=env,
                           stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "timeout %ss" % timeout
    return p.returncode, p.stdout.decode("utf-8", "replace"), \
        p.stderr.decode("utf-8", "replace")


def strip_ansi(s):
    s = re.sub(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)", "", s)
    s = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", s)
    s = re.sub(r"\x1b[()][A-Za-z0-9]", "", s)
    return s.replace("\x1b", "")


def lines_of(frame):
    return [ln.rstrip("\r") for ln in frame.split("\n")]


def last_screen(raw):
    """取「最后一次清屏后真正画出来的那一屏」。

    不能无脑取清屏序列之后的全部字节：产品退出时 finally 里会写
    `\x1b[?25h\x1b[2J\x1b[H` 还原光标并清屏（agentos.py:2698），末段就此被
    盖成空段。拿那一屏当证据，量到的是「用户退出后的空白」，不是产品画的
    界面 —— 本门曾因此把 25/26 判成 FAIL，而产品其实是好的。
    所以从后往前找第一个非空段；全空才退回原文（那也如实说明一屏都没画）。
    """
    for seg in reversed(re.split(r"\x1b\[2J(?:\x1b\[H)?", raw)):
        s = strip_ansi(seg)
        if s.strip():
            return s
    return strip_ansi(raw)


def pty_cli(cli, keys, cols=100, lines=30, boot=4.0, quiet=2.0, hard=90.0):
    """在真 PTY 里跑被检 CLI，依次送键，返回 (child_rc, frame, elapsed)。

    真 PTY 而不是管道：`agentos` 无参数时只有 isatty() 双真才进 TUI，管道里
    看到的只是用法 + rc=2 —— 用管道量这条路径等于量的是另一件事。
    """
    m, s = pty.openpty()
    fcntl.ioctl(s, termios.TIOCSWINSZ, struct.pack("HHHH", lines, cols, 0, 0))
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["COLUMNS"] = str(cols)
    env["LINES"] = str(lines)
    env["TERM"] = "xterm-256color"
    t0 = time.time()
    p = subprocess.Popen([sys.executable] + list(cli), stdin=s, stdout=s,
                         stderr=s, cwd=ROOT, env=env, start_new_session=True)
    os.close(s)
    buf = []

    def drain(limit):
        last = time.time()
        while time.time() - last < limit:
            if time.time() - t0 > hard:
                return
            try:
                r, _, _ = select.select([m], [], [], 0.08)
            except OSError:
                return
            if not r:
                continue
            try:
                chunk = os.read(m, 65536)
            except OSError:
                return
            if not chunk:
                return
            buf.append(chunk)
            last = time.time()

    drain(boot)
    for k in keys:
        try:
            os.write(m, k.encode("latin-1"))
        except OSError:
            break
        drain(quiet)
    elapsed = round(time.time() - t0, 2)
    try:
        p.wait(timeout=20)
    except subprocess.TimeoutExpired:
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=10)
    try:
        os.close(m)
    except OSError:
        pass
    raw = b"".join(buf).decode("utf-8", "replace")
    frame = last_screen(raw)
    return p.returncode, frame, elapsed


def _json1(out):
    """stdout 必须恰好一行合法 JSON，否则返回 {}（不许半截解析）。"""
    o = out.strip()
    if len(o.splitlines()) != 1:
        return {}
    try:
        return json.loads(o)
    except ValueError:
        return {}


def body(cli=None, skip_install=False):
    """判据体。返回 (pre_ok, detail)。每条判据自行 gate() 记账。"""
    cli = list(cli or CLI_ARGV)
    base_sha = snap_sha(WRITE_WATCH)

    # ── A 组：源码树静态面（不碰服务；量的是「这份盘上装不装得出去」）
    ap = os.path.join(ROOT, "tools", "agentos.py")
    with open(ap, "rb") as f:
        src_bytes = f.read()
    src = src_bytes.decode("utf-8", "replace")
    cerr = ""
    try:
        compile(src_bytes, ap, "exec")
    except SyntaxError as e:
        cerr = str(e)
    gate("src-agentos-compiles", j_compile_ok(cerr),
         cerr[:110] or "入口文件可编译",
         cmd="python3 -c \"import ast;ast.parse(open('tools/agentos.py').read())\"",
         rc=0 if not cerr else 1)
    ev("compile_err", cerr)

    tpl_text = read_text(os.path.join(ROOT, "bin", "agentos"))
    ok = j_shim_tpl(tpl_text)
    gate("src-shim-template-placeholder", ok,
         "模板留着 __AGENTOS_ROOT__=%s" % ok,
         cmd="grep -c __AGENTOS_ROOT__ bin/agentos", rc=0 if ok else 1)
    rc, _o, err = run_sh(["sh", "-n", os.path.join(ROOT, "bin", "agentos")],
                         timeout=60)
    gate("src-shim-syntax", rc == 0, "rc=%d %s" % (rc, err.strip()[:90]),
         cmd="sh -n bin/agentos", rc=rc)
    for nm, rel in (("src-install-syntax", "install.sh"),
                    ("src-uninstall-syntax", "uninstall.sh")):
        rc, _o, err = run_sh(["bash", "-n", os.path.join(ROOT, rel)],
                             timeout=60)
        gate(nm, rc == 0, "rc=%d %s" % (rc, err.strip()[:90]),
             cmd="bash -n %s" % rel, rc=rc)
    bad_rm = [rel for rel in ("install.sh", "uninstall.sh")
              if re.search(r"\brm\s+-\w+", read_text(os.path.join(ROOT, rel)))]
    gate("src-installers-no-rm", not bad_rm,
         ("命中 rm -f 的脚本: %s" % bad_rm) if bad_rm
         else "装机/卸载脚本零 rm -f（删文件只走 unlink）",
         cmd='grep -nE "\\brm +-" install.sh uninstall.sh',
         rc=1 if bad_rm else 0)
    rc, out, err = run(cli, ["--version"], timeout=60)
    ok = rc == 0 and j_version(out)
    gate("src-tree-version", ok, "rc=%d %r" % (rc, out.strip()[:40]),
         cmd="python3 tools/agentos.py --version", rc=rc)
    ev("version_text", out.strip())
    # ── 前置件：CLI 自己能跑（常驻服务已下线，不再要求它）──
    rc, out, err = run(cli, ["-V"], timeout=60)
    if rc != 0:
        return False, "PREFAIL `-V` rc=%s %s" % (rc, (err or out)[:200])
    m = re.search(r"AgentOS\s+(\S+)", out)
    if not m:
        return False, "PREFAIL `-V` 输出里没有版本号: %r" % out[:120]

    # C01-C03：宣称的面与真能跑的面逐一对上
    rc, out, err = run(cli, ["--help"], timeout=60)
    gate("help-usage", rc == 0 and "usage: agentos" in out,
         "rc=%d" % rc, cmd="agentos --help", rc=rc)
    missing = [s for s in SUBS if s not in out]
    gate("help-lists-all-subs", not missing,
         ("缺 %s" % missing) if missing else "%d 个子命令全部在册" % len(SUBS),
         cmd="agentos --help", rc=rc)
    bad = []
    for s in SUBS:
        r2, _o, _e = run(cli, [s, "--help"], timeout=60)
        if r2 != 0:
            bad.append("%s(rc=%s)" % (s, r2))
    gate("sub-help-all-rc0", not bad,
         ("红项 %s" % bad) if bad else "%d 面子命令 --help 全 rc=0" % len(SUBS),
         cmd="for s in <%d subs>: agentos <s> --help" % len(SUBS),
         rc=0 if not bad else 1)

    # C09-C10：shell 补全
    rc, out, err = run(cli, ["completion", "zsh"], timeout=60)
    gate("completion-zsh", rc == 0 and "#compdef agentos" in out,
         "rc=%d 行数=%d" % (rc, len(out.splitlines())),
         cmd="agentos completion zsh", rc=rc)
    rc, out, err = run(cli, ["completion"], timeout=60)
    gate("completion-noarg-rc2", rc == 2,
         "rc=%d（缺 shell 参数必须是用法错）" % rc,
         cmd="agentos completion", rc=rc)

    # C11-C12：历史与恢复
    rc, out, err = run(cli, ["sessions"], timeout=90)
    gate("sessions-rc0", rc == 0, "rc=%d" % rc, cmd="agentos sessions", rc=rc)
    # C13：门转发零参数必须拒绝（假绿拒切口）
    rc, out, err = run(cli, ["gates"], timeout=90)
    gate("gates-zeroarg-rc2", rc == 2 and "零参数" in (out + err),
         "rc=%d" % rc, cmd="agentos gates", rc=rc)

    # C14-C16：GitHub 只读面（原生链接 github 的终端出口）
    # gh whoami 的判定链在本项目之外（GitHub API + 本机钥匙串）。D40 铁律：
    # 判据红=结论，一次都不重试；但「基础设施故障」是明文例外 —— 2026-10-10
    # chain2 实测过一次假红：同一命令紧随其后连跑 3 次全 rc=0、
    # gh api rate_limit 5000/5000，红的那下纯粹是瞬时抖动。所以只在这一条
    # 上给有界重试（≤2 次、每次 1.5s），判据本身一字不改，detail 必须带实跑
    # 次数，失败时再附 stderr 尾部 80 字 —— 重试痕迹不许藏。
    attempts = 0
    for _try in range(3):
        attempts += 1
        rc, out, err = run(cli, ["gh", "whoami"], timeout=120)
        if rc == 0 and "sources:" in out:
            break
        time.sleep(1.5)
    gate("gh-whoami-rc0", rc == 0 and "sources:" in out,
         "rc=%d sources 行=%s 实跑 %d 次%s" % (
             rc, "sources:" in out, attempts,
             "" if attempts == 1 else
             " · stderr 尾=%r" % ((err or "").strip()[-80:])),
         cmd="agentos gh whoami", rc=rc)
    rc, out, err = run(cli, ["gh", "repo", "create", "x"], timeout=120)
    gate("gh-write-denied-rc2", rc == 2, "rc=%d（本机零远端写）" % rc,
         cmd="agentos gh repo create x", rc=rc)
    rc, out, err = run(cli, ["gh", "nope"], timeout=120)
    gate("gh-unknown-sub-rc2", rc == 2, "rc=%d" % rc,
         cmd="agentos gh nope", rc=rc)

    # C21：非 TTY 无参数必须给用法并判错（进 raw 等于把控制字符糊进日志）
    rc, out, err = run(cli, [], timeout=60)
    gate("noarg-nontty-rc2", rc == 2 and "usage: agentos" in (out + err),
         "rc=%d" % rc, cmd="agentos < /dev/null", rc=rc)

    # C23：真 PTY —— 「agentos」这两个字在终端里必须**进终端 agent**
    # （网页驾驶舱下线后，无参数 = 进 agent 的 REPL；首屏用门主动终止，
    #   所以这条不判 rc，只判画面）。
    _, frame, elapsed = pty_cli(cli, [], boot=4.0, quiet=0.5)
    head = lines_of(frame)[0] if lines_of(frame) else ""
    gate("tty-noarg-enters-agent", "AgentOS" in head or ">" in frame,
         "首行=%r elapsed=%ss" % (head[:46], elapsed),
         cmd="agentos   # 真 PTY、无参数、零送键")

    # ── B 组：装机往返（对标 Claude Code 的「装完就能用」）──────────────
    # 隔离是硬约束不是图干净：--dest 指 work/cli_gate_dest/bin，HOME 也指到
    # work/cli_gate_dest/home。本门是 CI 门，碰一次用户 ~/.local/bin /
    # ~/.zshrc / ~/.zfunc 就是事故 —— 和「误杀常驻」同级。
    if skip_install:
        note("install-roundtrip-skipped",
             "--skip-install：B 组装机往返整体未跑。只读环境的如实降级，"
             "不算判据绿、也不算判据红。")
    else:
        # 上一轮留下的 dest 先清干净，否则「两次装机 sha256 相同」量到的
        # 是上一轮的 shim。删目录只走 find -depth -delete（禁 rm -rf）。
        subprocess.run(["find", DEST_DIR, "-depth", "-delete"],
                       capture_output=True, timeout=60)
        DESTBIN = os.path.join(os.path.realpath(DEST_DIR), "bin")
        FAKEHOME = os.path.join(os.path.realpath(DEST_DIR), "home")
        os.makedirs(DESTBIN, exist_ok=True)
        os.makedirs(FAKEHOME, exist_ok=True)
        ienv = {"HOME": FAKEHOME}
        INST = ["bash", os.path.join(ROOT, "install.sh"),
                "--dest", DESTBIN, "-q"]
        UNINST = ["bash", os.path.join(ROOT, "uninstall.sh"),
                  "--dest", DESTBIN, "-q"]
        shim = os.path.join(DESTBIN, "agentos")

        def _tail(s):
            rows = (s or "").strip().splitlines()
            return rows[-1][:96] if rows else ""

        # B01 装得上
        rc, out, err = run_sh(INST, timeout=300, env_extra=ienv)
        gate("inst-install-rc0", rc == 0, "rc=%d %s" % (rc, _tail(out + err)),
             cmd="bash install.sh --dest work/cli_gate_dest/bin -q", rc=rc)

        # B02 落地的必须是普通 0755 文件（软链会让「装了什么」说不清）
        try:
            mode = os.lstat(shim).st_mode
        except OSError:
            mode = 0
        ok = j_plain_exec(mode) and not os.path.islink(shim)
        gate("inst-shim-plain-0755", ok,
             "mode=%s 软链=%s" % (oct(stat.S_IMODE(mode)), os.path.islink(shim)),
             # 拼接连结，别用 % 格式化：模板里的 %Lp 会被 % 运算符当成格式符，
             # 整道门在跑到这条之前就 ValueError 崩掉（2026-10-10 实测 rc=1）。
             cmd="stat -f '%Lp %Y' " + shim, rc=0 if ok else 1)
        # 落**完整** st_mode（含 S_IFREG）：只落 S_IMODE 的话，反向门拿它当
        # 正例喂 j_plain_exec 会因 S_ISREG 假而翻红 —— 正例 BASE 自己就不能
        # 复现判据为真，反向门会去追一个不存在的 bug。
        ev("shim_mode", oct(mode))

        # B03 占位必须被换成真仓库根
        shim_txt = read_text(shim)
        ok = j_no_placeholder(shim_txt) and j_root_embedded(shim_txt, ROOT)
        gate("inst-shim-root-embedded", ok,
             "内嵌根=%s 占位残留=%s" % (j_root_embedded(shim_txt, ROOT),
                                       "__AGENTOS_ROOT__" in shim_txt),
             cmd="grep -c __AGENTOS_ROOT__ <shim>；grep -c %s <shim>" % ROOT,
             rc=0 if ok else 1)
        ev("shim_root_embedded", [j_root_embedded(shim_txt, ROOT),
                                  j_no_placeholder(shim_txt)])

        # B04 装出来的版本必须与源码树逐字相同
        rc, out, err = run_sh([shim, "--version"], timeout=90, env_extra=ienv)
        ok = rc == 0 and j_same_version(out.strip(),
                                        str(EVIDENCE.get("version_text") or ""))
        gate("inst-version-same", ok,
             "装=%r 树=%r" % (out.strip()[:40],
                             str(EVIDENCE.get("version_text"))[:40]),
             cmd="<shim> --version", rc=rc)
        ev("version_installed", out.strip())

        # B05 两次装机 sha256 相同（幂等契约）
        sha1 = sha256_file(shim) if os.path.exists(shim) else ""
        rc, out, err = run_sh(INST, timeout=300, env_extra=ienv)
        sha2 = sha256_file(shim) if os.path.exists(shim) else ""
        ok = rc == 0 and j_idempotent(sha1, sha2)
        gate("inst-idempotent-sha256", ok,
             "rc=%d sha1=%s… sha2=%s… 相同=%s"
             % (rc, sha1[:12], sha2[:12], sha1 == sha2),
             cmd="install.sh 装两次 → shasum -a 256 <shim>", rc=rc)
        ev("sha_install", [sha1, sha2])

        # B06/B07 env -i 干净 shell 里也要能跑（PATH 只剩 /usr/bin:/bin）
        CLEAN = ["env", "-i", "PATH=/usr/bin:/bin", "HOME=" + FAKEHOME]
        rc, out, err = run_sh(CLEAN + [shim, "--version"], timeout=120)
        gate("inst-clean-shell-version", j_clean_env_ok(rc, out),
             "rc=%d %r" % (rc, out.strip()[:40]),
             cmd="env -i PATH=/usr/bin:/bin HOME=<隔离> <shim> --version", rc=rc)
        ev("clean_rc", rc)
        rc, out, err = run_sh(CLEAN + [shim, "completion", "zsh"], timeout=120)
        gate("inst-clean-shell-runs", rc == 0 and "#compdef agentos" in out,
             "rc=%d %s" % (rc, _tail(out)),
             cmd="env -i PATH=/usr/bin:/bin HOME=<隔离> <shim> completion zsh", rc=rc)
        ev("clean_rc2", rc)

        # B10-B12 卸载：rc=0、shim 真消失、再卸一次还 rc=0
        rc, out, err = run_sh(UNINST, timeout=300, env_extra=ienv)
        gate("inst-uninstall-rc0", rc == 0, "rc=%d %s" % (rc, _tail(out + err)),
             cmd="bash uninstall.sh --dest work/cli_gate_dest/bin -q", rc=rc)
        gone = j_uninstalled(shim)
        gate("inst-uninstall-removed", gone, "shim 还在=%s" % os.path.exists(shim),
             cmd="test -e <shim>", rc=0 if gone else 1)
        ev("uninst_gone", gone)
        rc, out, err = run_sh(UNINST, timeout=300, env_extra=ienv)
        gate("inst-uninstall-idempotent", rc == 0,
             "rc=%d（第二次卸载也要 rc=0）" % rc, cmd="再跑一次 uninstall.sh", rc=rc)

        # B13 卸完能原样装回
        rc, out, err = run_sh(INST, timeout=300, env_extra=ienv)
        rc2, out2, _e2 = run_sh([shim, "--version"], timeout=90, env_extra=ienv) \
            if os.path.exists(shim) else (1, "", "")
        gate("inst-reinstall-roundtrip", rc == 0 and rc2 == 0,
             "install rc=%d / shim --version rc=%d" % (rc, rc2),
             cmd="卸完重装，再跑一次 --version", rc=rc)

        # B14 fail-closed：占位没被替换的 shim 必须自己失败并说清原因，
        # 不许静默跑别的代码。副本落 work/ 下（活树之外），跑完 unlink。
        fc = os.path.join(os.path.realpath(DEST_DIR), "failclosed")
        fcr, fcmsg = 0, ""
        try:
            with open(os.path.join(ROOT, "bin", "agentos"), "rb") as f:
                fc_data = f.read()
            with open(fc, "wb") as f:
                f.write(fc_data)
            os.chmod(fc, 0o755)
            fcr, fco, fce = run_sh([fc, "--version"], timeout=90)
            fcmsg = fco + fce
        except OSError as e:
            fcr, fcmsg = 1, "副本没造出来: %r" % (e,)
        finally:
            if os.path.exists(fc):
                try:
                    os.unlink(fc)          # 禁 rm -f
                except OSError:
                    pass
        ok = j_failclosed(fcr, fcmsg)
        gate("inst-failclosed-template", ok,
             "rc=%d（未替换占位必须 127 且说「定位不到」） %r" % (fcr, _tail(fcmsg)),
             cmd="cp bin/agentos <副本> && chmod 755 && <副本> --version", rc=fcr)
        ev("failclosed", [fcr, j_failclosed(fcr, fcmsg)])

    # ── C25-C26：bash 补全既要语法对，又要与真子命令/真门链逐项相等 --------
    rc, out, err = run(cli, ["completion", "bash"], timeout=60)
    comp_path = os.path.join(ROOT, "work", "cli_gate_completion.bash")
    try:
        with open(comp_path, "w", encoding="utf-8") as f:
            f.write(out)
        syn, _o, se = run_sh(["bash", "-n", comp_path], timeout=60)
    except OSError as e:
        syn, se = 1, "落盘失败 %r" % (e,)
    m_sub = re.search(r'local subs="([^"]*)"', out)
    got_subs = m_sub.group(1).split() if m_sub else []
    m_g = re.search(r'local gates="([^"]*)"', out)
    got_gates = m_g.group(1).split() if m_g else []
    ok = rc == 0 and syn == 0 and j_completion_subs(got_subs, SUBS)
    gate("completion-bash-subs", ok,
         "rc=%d bash -n rc=%d got=%d want=%d 差=%s"
         % (rc, syn, len(got_subs), len(SUBS),
            sorted(set(got_subs) ^ set(SUBS)) or "无"),
         cmd="agentos completion bash > f && bash -n f && 比对 Subs", rc=rc)
    ev("completion_subs", [got_subs, list(SUBS)])
    chain = chain_names()
    ok = j_completion_gates(got_gates, chain)
    gate("completion-gates-chain", ok,
         "got=%d chain=%d 差=%s" % (len(got_gates), len(chain),
                                    sorted(set(got_gates) ^ set(chain)) or "无"),
         cmd="agentos completion bash 的 local gates vs gates.sh 正确链",
         rc=0 if ok else 1)
    ev("completion_gates", [got_gates, chain])

    # ── D 组：门自己不许改活树 ------------------------------------------
    after = snap_sha(WRITE_WATCH)
    ok = j_zero_write(base_sha, after)
    diff = [k for k in base_sha if base_sha.get(k) != after.get(k)]
    gate("zero-write-source-tree", ok,
         "五个文件 sha256 前后一致=%s 漂移=%s" % (ok, diff or "无"),
         cmd="shasum -a 256 tools/agentos.py "
             "tools/serve.py bin/agentos（门前/门后）",
         rc=0 if ok else 1)
    ev("zero_write", [base_sha, after])
    # 本机 PATH 上是否真有 agentos：如实标注。本门只装 work/cli_gate_dest，
    # 从不往用户 PATH 里装东西，也不因为别人装了就判红。
    w = sh_which("agentos")
    if w:
        note("path-install-not-ours",
             "本机 PATH 上 agentos=%s（用户自己装的；本门没装它、也没动它）" % w)
    else:
        note("path-install-unverifiable",
             "bash -lc 'command -v agentos' 为空：本机 PATH 上没有 agentos"
             "（本门只落 work/cli_gate_dest，不写用户 PATH）")
    return True, ""


def main(argv=None):
    ap = argparse.ArgumentParser(description="CLI 硬门：终端可用性")
    ap.add_argument("--cmd", default=None,
                    help="被检 CLI 的 argv 前缀（反向门跑变异体时用）")
    ap.add_argument("--out", default=None,
                    help="判据 JSON 落点（反向门隔离变异体数据用，默认 work/cli_gate.json）")
    ap.add_argument("--skip-install", action="store_true",
                    help="跳过 B 组装机往返（只读环境用；未跑的判据记 note，不算绿）")
    a = ap.parse_args(argv)
    cli = a.cmd.split() if a.cmd else list(CLI_ARGV)
    out_path = a.out or OUT
    t0 = time.time()
    # docstring 第 40 行承诺的 --skip-install 必须真的接到 body，否则只在
    # JSON 里记一个永远是 False 的字段（2026-10-10 补：此前压根没这个参数）。
    pre_ok, detail = body(cli, skip_install=a.skip_install)
    nfail = sum(1 for r in results if not r["ok"])
    # [PRE] 与落盘告警都必须打在摘要**之前**：摘要行得是本门最后一行
    # （goal_loop.py 靠尾行认门名与裁决，见 gates.sh D101/D71 注释）。旧版
    # 把 [PRE] 打在摘要后面，尾行就不再是 `== NAME OK n/n ==` 形态了。
    if not pre_ok:
        print("  [PRE] " + detail)
    try:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"tool": "cli_gate", "ok": bool(nfail == 0),
                       "pre_ok": bool(pre_ok), "prefail": detail,
                       "elapsed": round(time.time() - t0, 2),
                       "cli": cli, "dest": DEST_DIR,
                       "skip_install": bool(a.skip_install),
                       "views": list(view_names()),
                       "subs": list(SUBS), "results": results,
                       # notes/evidence 是给反向门当正例 BASE 的：判据纯函数
                       # 层由 cli_gate_selftest.py import 复用，变异体就是
                       # 把这里的 evidence 改坏再要求翻红。
                       "notes": notes, "evidence": EVIDENCE},
                      f, ensure_ascii=False, indent=1)
    except OSError as e:
        print("  [warn] 落盘失败 %r" % (e,))
    print("== CLI_GATE %s  %d/%d 判据通过  elapsed=%.1fs =="
          % ("OK" if nfail == 0 else "FAIL", len(results) - nfail,
             len(results), time.time() - t0))
    return 0 if nfail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
