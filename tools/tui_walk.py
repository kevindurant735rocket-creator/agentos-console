#!/usr/bin/env python3
"""tui_walk —— 真 PTY、真按键、真回显，逐键走查 AgentOS TUI。

为什么要有这一层
----------------
本项目的 a11y 证据（tools/a11y_walk.py、tools/a11y_human.py）全在浏览器侧：
真键盘事件打 file:// 页面、真 AX 树比对。但产品还有一整个交互面从没被任何
工具用真按键走过 —— `python3 tools/agentos.py tui`。屏幕阅读器在终端里念到的
就是这些字节，所以这一层的等价物是：起真 PTY、发真键、按「人眼看到的文本」
（剥掉 ANSI 之后）逐键判定，而不是 import 模块数函数。

判据一律可判真假，不搞「收到了字节就算过」：
  按数字 3     → footer 必须写「视图 3/13」且 tab 栏把对应面方括号括住
  按 Tab / →   → footer 视图号必须恰好 +1；Shift-Tab / ← 必须恰好 -1
  按 ↑ 到顶    → 光标行索引不许变（否则就是越界或串行）
  按 Enter     → 必须出现「明细 ·」；再按一次必须消失
  按 r         → 首行时钟必须真的往前走（先睡 1.2s 再按）
  总览按 /     → 必须出红字「这一面没有输入行」（红 = 帧里带 \\x1b[31m 前缀）
  Agent 台 /   → 必须出现 composer 行，打字 / 退格 / Esc 三样都要真的动
  按 q         → 子进程必须 rc=0 退出
任何一键不达标，`ok` 就不许是 true，退出码 2 —— 不为了凑绿把判据降级。

读屏口径（诚实声明）
------------------
本沙箱是 macOS，VoiceOver 的「辅助功能」权限（TCC）不能由进程自己授予，
所以 **OS 层读屏列为 AT-UNVERIFIED**：这里只证据到「剥掉 ANSI 之后的纯文本
长度、首行标题、每面 body 行数与字符数、最大行宽」这一层，证明每个视图都
不是「只有转义序列的空壳」。照 tools/a11y_human.py 已有的诚实写法，不装。

用法
----
    python3 tools/tui_walk.py                      # 走查真 CLI
    python3 tools/tui_walk.py --cmd '<命令>'        # 覆写被走查的 CLI（门跑变异体）
    python3 tools/tui_walk.py --out P --cols 100 --lines 30
"""

import argparse
import ast
import fcntl
import json
import os
import re
import select
import shlex
import struct
import subprocess
import sys
import termios
import time
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(ROOT, "tools", "agentos.py")
DEFAULT_CMD = "python3 tools/agentos.py tui"

FOOT_RE = re.compile(r"视图 (\d+)/(\d+)")
CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
CHARSET_RE = re.compile(r"\x1b[()][A-Za-z0-9]")
COMPOSER_RE = re.compile(r"^composer› (.*)█$")
TIME_RE = re.compile(r"(\d\d:\d\d:\d\d)")
CLEAR = "\x1b[2J\x1b[H"
DASH = "\u2500"
CLOSED_SRC = "8790 数据不可达"
READER_AT = "AT-UNVERIFIED"
READER_WHY = ("macOS VoiceOver 的辅助功能权限（TCC）不能由本沙箱进程授予，"
              "OS 层读屏未实测；本文件只证据到「剥 ANSI 后的纯文本」这一层。")
VIEWS_C = None


# ── 帧解析：全部基于「人眼看到的文本」，不基于模块内部状态 ──────────────
def strip_ansi(s):
    s = OSC_RE.sub("", s)
    s = CSI_RE.sub("", s)
    s = CHARSET_RE.sub("", s)
    return s.replace("\x1b", "")


def lines_of(raw):
    return [ln.rstrip("\r") for ln in strip_ansi(raw).split("\n")]


def footer(lines):
    """末行「视图 N/M · 数据源: X」→ (N, M, X)。"""
    for ln in lines:
        m = FOOT_RE.search(ln)
        if m:
            rest = ln[m.end():]
            src = ""
            k = rest.find("数据源")
            if k >= 0:
                src = rest[k + 3:].lstrip(":： ").strip()
            return int(m.group(1)), int(m.group(2)), src
    return None, None, ""


def cursor_line(lines):
    """body 里那行带 `> ` 光标标的行号。"""
    for i, ln in enumerate(lines):
        if ln.startswith("> "):
            return i
    return None


def body_block(lines):
    """body 行 = 第一条分隔线之后、footer 之前的非空行。

    不数「─」行的条数来切：带 meta.head 的行自己也会画一条 ─ 线，按条数切会
    把 body 切短，于是「空壳面板」也能凑出几行来。
    """
    fi = None
    for i, ln in enumerate(lines):
        if FOOT_RE.search(ln):
            fi = i
            break
    seps = [i for i, ln in enumerate(lines)
            if ln.strip() and set(ln.strip()) == {DASH}]
    if fi is None or not seps or seps[0] >= fi:
        return []
    return [ln for ln in lines[seps[0] + 1:fi] if ln.strip()]


def active_tab(lines):
    """tab 栏里被方括号括住的那一项（激活面）。"""
    for ln in prelude(lines)[1]:
        for tok in re.findall(r"\[([^\]]*)\]", ln):
            return tok.strip()
    return ""


def has_detail(lines):
    return any(ln.startswith("明细 ·") for ln in lines)


def composer_buf(lines):
    for ln in lines:
        m = COMPOSER_RE.match(ln)
        if m:
            return m.group(1)
    return None


def header_time(lines):
    m = TIME_RE.search(prelude(lines)[0])
    return m.group(1) if m else None


def prelude(lines):
    """(表头行, tab 两行)。

    每帧开头是清屏序列 \\x1b[2J\\x1b[H，剥掉 ANSI 后它留下一个**空行**，
    所以表头不在 lines[0]、tab 栏也不在 lines[1:3]。按「前几条非空行」取，
    不写死下标：清屏实现换成 \\x1b[H\\x1b[J 或帧尾多一个换行时，写死
    下标的判据会集体静默失准（tab 永远读不到、时钟永远 None）。
    """
    idx = [i for i, ln in enumerate(lines[:8]) if ln.strip()]
    head = lines[idx[0]] if idx else ""
    rows = [lines[i] for i in idx[1:3]]
    return head, rows


def red_hint(raw, needle):
    """红字判据要看**没剥 ANSI** 的帧：颜色就藏在 \\x1b[31m 里。"""
    return bool(re.search(r"\x1b\[31m[^\n]*" + re.escape(needle), raw))


def dw(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def read_views(path):
    """从 tools/agentos.py 抽 VIEWS 常量，不 import（import 有副作用）。"""
    try:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except Exception:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "VIEWS":
                    try:
                        return [str(x) for x in ast.literal_eval(node.value)]
                    except Exception:
                        return None
    return None


def tab_name(tok):
    """`3 阶段矩阵` → `阶段矩阵`；无数字标的面原样返回。"""
    return re.sub(r"^\d\s+", "", tok or "").strip()


class St(object):
    """一帧的状态快照（含上一帧，供「变没变」类判据用）。"""

    def __init__(self, raw, prev_raw):
        self.raw = raw
        self.lines = lines_of(raw)
        self.vi, self.nviews, self.source = footer(self.lines)
        self.act = active_tab(self.lines)
        self.cur = cursor_line(self.lines)
        self.body = body_block(self.lines)
        self.plain_len = len(strip_ansi(raw))
        self.max_dw = max([dw(x) for x in self.lines] or [0])
        self.prev_raw = prev_raw
        self.prev_lines = lines_of(prev_raw)
        self.prev_vi, _, _ = footer(self.prev_lines)
        self.prev_act = active_tab(self.prev_lines)
        self.prev_cur = cursor_line(self.prev_lines)


# ── 判据函数：每个都返回 (ok, detail) ─────────────────────────────────
def mk_digit(key, vi0):
    def chk(st):
        want = vi0 + 1
        okn = st.vi == want
        okb = bool(st.act) and st.act.startswith(key + " ")
        return (bool(okn and okb),
                "footer=视图 %s（期望 %d）；tab 激活=%r（期望以 %r 开头）"
                % (st.vi, want, st.act, key + " "))
    return chk


def mk_nav(vi0, how):
    def chk(st):
        want = vi0 + 1
        okn = st.vi == want
        moved = bool(st.act) and st.act != st.prev_act
        return (bool(okn and moved),
                "%s 后期望视图 %d，实得 %s；激活 tab %r → %r"
                % (how, want, st.vi, st.prev_act, st.act))
    return chk


def mk_cur(d):
    def chk(st):
        if st.cur is None or st.prev_cur is None:
            return False, "找不到光标行（%r → %r）" % (st.prev_cur, st.cur)
        return (st.cur - st.prev_cur == d,
                "光标行 %s → %s（期望 %+d）" % (st.prev_cur, st.cur, d))
    return chk


def chk_detail_on(st):
    ok = has_detail(st.lines)
    return ok, "应出现「明细 ·」行，实得 %s" % ok


def chk_detail_off(st):
    ok = not has_detail(st.lines)
    return ok, "「明细 ·」应消失，实得 %s" % (not ok)


def chk_composer_on(st):
    b = composer_buf(st.lines)
    return (b is not None and b == ""), "composer 行缓冲=%r（期望空串）" % (b,)


def mk_composer(want):
    def chk(st):
        b = composer_buf(st.lines)
        return (b == want, "composer 缓冲=%r（期望 %r）" % (b, want))
    return chk


def chk_composer_off(st):
    b = composer_buf(st.lines)
    return (b is None, "composer 行应消失，实得 %r" % (b,))


def chk_red_hint(st):
    ok = red_hint(st.raw, "这一面没有输入行")
    noc = composer_buf(st.lines) is None
    return (ok and noc,
            "红字「这一面没有输入行」命中=%s；总览不该有 composer 行=%s"
            % (ok, noc))


def chk_time_moved(st):
    a, b = header_time(st.prev_lines), header_time(st.lines)
    return (bool(a) and bool(b) and a != b,
            "首行时钟 %r → %r" % (a, b))


def build_steps(nviews):
    """键序。视图名一律从屏上/VIEWS 常量取，不写死。"""
    S = []

    def add(name, data, chk, expect, pre=0.0):
        S.append({"name": name, "data": data, "check": chk,
                  "expect": expect, "pre": pre})

    for key, vi0 in (("1", 0), ("3", 2), ("5", 4), ("9", 8), ("0", 9),
                     ("7", 6), ("2", 1), ("4", 3)):
        add("digit-%s" % key, key, mk_digit(key, vi0),
            "按 %s 后 footer=视图 %d 且 tab 括住该面" % (key, vi0 + 1))
    add("tab", "\t", mk_nav(4, "Tab"), "Tab 后视图 5")
    add("right", "\x1b[C", mk_nav(5, "→"), "右方向后视图 6")
    add("shift-tab", "\x1b[Z", mk_nav(4, "Shift-Tab"), "Shift-Tab 后视图 5")
    add("left", "\x1b[D", mk_nav(3, "←"), "左方向后视图 4")
    add("up-clamped", "\x1b[A", mk_cur(0), "↑ 到顶光标行不动")
    add("down-1", "\x1b[B", mk_cur(1), "↓ 光标行 +1")
    add("down-2", "\x1b[B", mk_cur(1), "↓ 光标行 +1")
    add("up-1", "\x1b[A", mk_cur(-1), "↑ 光标行 -1")
    add("enter-detail-on", "\r", chk_detail_on, "回车出现「明细 ·」")
    add("enter-detail-off", "\r", chk_detail_off, "再回车「明细 ·」消失")
    add("digit-1-again", "1", mk_digit("1", 0), "回总览")
    add("refresh-r", "r", chk_time_moved, "按 r 首行时钟前进", pre=1.2)
    add("slash-overview", "/", chk_red_hint,
        "总览按 / 出红字「这一面没有输入行」")
    add("digit-6-agent", "6", mk_digit("6", 5), "进 Agent 台")
    add("slash-agent", "/", chk_composer_on, "Agent 台按 / 进 composer")
    add("type-l", "l", mk_composer("l"), "打字 l")
    add("type-s", "s", mk_composer("ls"), "再打 s → ls")
    add("backspace", "\x7f", mk_composer("l"), "退格删掉 s")
    add("esc-cancel", "\x1b", chk_composer_off, "Esc 取消 composer 且不退出")
    for k in range(7, nviews + 1):
        add("tab-to-%d" % k, "\t", mk_nav(k - 1, "Tab"),
            "Tab 到第 %d 面" % k)
    add("quit-q", "q", None, "按 q 退出且 rc=0")
    return S


class Walk(object):
    """真 PTY 驱动器：起子进程、发字节、收到安静为止。"""

    def __init__(self, cmd, cols, rows, quiet=0.4, hard=40.0,
                 latency=2.5):
        self.cmd = cmd
        self.cols, self.rows = cols, rows
        self.quiet, self.hard = quiet, hard
        # latency：发键后「屏必须换帧」的显式时延预算。旧逻辑在没等到换帧时
        # 只等 quiet(0.4s) 就把旧帧当答案 —— 慢重绘的面（切过去要回 8790
        # 取数）于是全被判红，量具测的是自己的耐心而不是产品。判据没松：
        # 预算内不换帧仍算坏键，假 CLI 照样红。
        self.latency = latency
        self.buf = b""
        self.last_raw = ""
        self.exited = False
        self.proc = None
        self.master = None

    def start(self):
        self.master, slave = os.openpty()
        os.set_blocking(self.master, False)
        fcntl.ioctl(slave, termios.TIOCSWINSZ,
                    struct.pack("HHHH", self.rows, self.cols, 0, 0))
        env = dict(os.environ)
        env["COLUMNS"] = str(self.cols)
        env["LINES"] = str(self.rows)
        env.setdefault("TERM", "xterm-256color")
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        self.proc = subprocess.Popen(
            shlex.split(self.cmd), stdin=slave, stdout=slave, stderr=slave,
            cwd=ROOT, env=env, start_new_session=True)
        os.close(slave)
        return self

    def frame(self):
        return self.buf.decode("utf-8", "replace").rsplit(CLEAR, 1)[-1]

    def drain(self, until_change=None):
        # until_change 是发键前的同一帧：换了帧就不再等固定 quiet，只等一小段
        # settle 就返回。慢重绘的面（切过去要先回 8790 取数，中间几百毫秒一个
        # 字节都不吐）以前会被 quiet=0.4 秒判成「画完了」，量具读到旧帧，把好
        # 按键误判成坏按键。判据一个字没动，变的只是「等到真的换了帧」。
        q = self.quiet
        settle = min(0.15, q)
        t0 = time.time()
        last = t0
        changed = until_change is None
        while not self.exited:
            try:
                r, _, _ = select.select([self.master], [], [], 0.05)
            except OSError:
                break
            if r:
                try:
                    chunk = os.read(self.master, 65536)
                except BlockingIOError:
                    continue
                except OSError:
                    self.exited = True
                    break
                if chunk:
                    self.buf += chunk
                    last = time.time()
                    if not changed and self.frame() != until_change:
                        changed = True
                        last = time.time()
                    continue
                self.exited = True
                break
            now = time.time()
            if changed:
                if now - last >= settle:
                    break
            elif now - last >= min(self.latency, self.hard):
                break
            if now - t0 >= self.hard:
                break
        return self.frame()

    def send(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        os.write(self.master, data)

    def close(self):
        try:
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except Exception:
                    self.proc.kill()
        except Exception:
            pass
        if self.master is not None:
            try:
                os.close(self.master)
            except Exception:
                pass


def first_frame(w, limit=20.0):
    """首帧必须解析出「视图 N/M」才算起屏完成，否则继续等。

    为什么不能只靠 quiet：首屏要先回 8790 取数，取数期间 PTY 一个字节都
    不吐，超过 settle(0.15s) 后 drain 就把**半张**首帧当完成帧返回。半张
    首帧让 until_change 的比对天然成立，于是后面每一键都在对旧帧 —— 量具
    自己造出一串假红（实测连环假红：first-frame、digit-1、digit-3、digit-0
    全红，而真 TUI 没问题）。这里只多等「首屏画完」，判据一个字没松。
    """
    t0 = time.time()
    raw = w.drain()
    while (footer(lines_of(raw))[0] is None
           and not w.exited
           and time.time() - t0 < limit):
        time.sleep(0.05)
        raw = w.drain()
    return raw


def run(w):
    keys = []
    views = {}
    all_ok = True
    t0 = time.time()
    first = first_frame(w)
    w.last_raw = first
    st0 = St(first, "")
    if st0.vi is None or not st0.nviews:
        keys.append({"i": 0, "name": "first-frame", "ok": False,
                     "sent": "", "detail":
                         "首帧没有「视图 N/M」footer：子进程可能没起来、"
                         "或第一屏就崩了"})
        all_ok = False
        nviews = 13
    else:
        nviews = st0.nviews
    if st0.vi is not None and st0.vi not in views:
        views[st0.vi] = {"stage": "first-frame", "vi": st0.vi}
    steps = build_steps(nviews)
    for i, sp in enumerate(steps, 1):
        data = sp["data"]
        shown = data if isinstance(data, str) else \
            data.decode("utf-8", "replace")
        if w.exited and footer(lines_of(w.last_raw))[0] is None:
            keys.append({"i": i, "name": sp["name"], "ok": False,
                         "sent": shown,
                         "detail": "子进程已退出且屏上无 footer，后续键无从判定"})
            all_ok = False
            continue
        if sp["pre"]:
            time.sleep(sp["pre"])
        prev_raw = w.last_raw
        ts = time.time()
        try:
            w.send(data)
        except OSError as e:
            keys.append({"i": i, "name": sp["name"], "ok": False,
                         "sent": shown, "detail": "写 PTY 失败 %r" % (e,)})
            all_ok = False
            continue
        raw = w.drain(until_change=prev_raw)
        w.last_raw = raw
        waited = round(time.time() - ts, 3)
        st = St(raw, prev_raw)
        if sp["check"] is None:
            ok, detail = True, "退出码单独判（见 child_rc）"
        else:
            ok, detail = sp["check"](st)
        keys.append({"i": i, "name": sp["name"], "sent": shown,
                     "bytes": len(raw.encode("utf-8", "replace")),
                     "waited": waited, "expect": sp["expect"],
                     "vi": st.vi, "ok": bool(ok), "detail": detail})
        if not ok:
            all_ok = False
        if st.vi is not None and st.vi not in views:
            views[st.vi] = {}
        if st.vi in views:
            v = views[st.vi]
            v.setdefault("vi", st.vi)
            v.setdefault("name",
                         (VIEWS_C[st.vi - 1] if VIEWS_C and
                          st.vi - 1 < len(VIEWS_C) else ""))
            v["active_tab"] = st.act
            v["tab_name"] = tab_name(st.act)
            v["tab_matches_views"] = (
                None if not VIEWS_C else
                (st.vi - 1 < len(VIEWS_C) and
                 tab_name(st.act) == VIEWS_C[st.vi - 1]))
            v["source"] = st.source
            v["plain_len"] = st.plain_len
            v["body_lines"] = len(st.body)
            v["body_chars"] = sum(len(x) for x in st.body)
            v["max_line_dw"] = st.max_dw
            v["title"] = st.lines[0] if st.lines else ""
            v["first_body"] = st.body[0] if st.body else ""
    rc_child = None
    try:
        if w.proc.poll() is None:
            rc_child = w.proc.wait(timeout=10)
        else:
            rc_child = w.proc.returncode
    except Exception:
        rc_child = None
    elapsed = round(time.time() - t0, 2)
    return {"keys": keys, "views": views, "nviews": nviews,
            "child_rc": rc_child, "elapsed": elapsed, "keys_ok": all_ok}


def main(argv=None):
    global VIEWS_C
    ap = argparse.ArgumentParser(
        description="真 PTY 真按键走查 AgentOS TUI")
    ap.add_argument("--cmd", default=DEFAULT_CMD,
                    help="被走查的 CLI（默认真 CLI；门跑变异体时覆写）")
    ap.add_argument("--out",
                    default=os.path.join(ROOT, "work", "tui_walk.json"))
    ap.add_argument("--cols", type=int, default=100)
    ap.add_argument("--lines", type=int, default=30)
    ap.add_argument("--quiet", type=float, default=0.4,
                    help="多久没有新字节算一屏画完")
    ap.add_argument("--latency", type=float, default=2.5,
                    help="发键后等换帧的时延预算秒（超了判这键没生效）")
    ap.add_argument("--hard", type=float, default=40.0,
                    help="单键等待硬上限")
    a = ap.parse_args(argv)
    VIEWS_C = read_views(ENTRY)

    w = Walk(a.cmd, a.cols, a.lines, a.quiet, a.hard, a.latency)
    try:
        w.start()
        res = run(w)
    finally:
        w.close()

    views = [res["views"][k] for k in sorted(res["views"])]
    nkeys = len(res["keys"])
    nok = sum(1 for k in res["keys"] if k["ok"])
    nviews_seen = len(views)
    closed = [v["vi"] for v in views if CLOSED_SRC in (v.get("source") or "")]
    tab_bad = [v["vi"] for v in views if v.get("tab_matches_views") is False]
    thin = [v["vi"] for v in views
            if v.get("body_lines", 0) < 1 or v.get("body_chars", 0) < 30]
    ok = (res["keys_ok"] and res["child_rc"] == 0 and
          nviews_seen == res["nviews"] and
          nviews_seen == len(VIEWS_C or views) and
          not closed and not tab_bad and not thin)

    doc = {
        "tool": "tui_walk",
        "cmd": a.cmd,
        "cols": a.cols,
        "lines": a.lines,
        "started": time.strftime("%F %T"),
        "elapsed": res["elapsed"],
        "views_const": VIEWS_C,
        "keys": res["keys"],
        "views": views,
        "child_rc": res["child_rc"],
        "ok": bool(ok),
        "gates": {
            "keys_matched": nok,
            "keys_total": nkeys,
            "views_visited": nviews_seen,
            "views_expected": res["nviews"],
            "views_fail_closed": closed,
            "views_tab_mismatch": tab_bad,
            "views_thin_body": thin,
            "reader_at": READER_AT,
        },
        "reader_at": READER_AT,
        "reader_at_why": READER_WHY,
    }
    try:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print("tui_walk: 落盘失败 %r" % (e,), file=sys.stderr)
        return 3

    print("tui_walk: cmd=%s %dx%d elapsed=%ss"
          % (a.cmd, a.cols, a.lines, res["elapsed"]))
    print("tui_walk: keys %d/%d · views %d/%d · child_rc=%s · reader_at=%s"
          % (nok, nkeys, nviews_seen, res["nviews"],
             res["child_rc"], READER_AT))
    if closed:
        print("tui_walk: 取数失败的面（fail-closed）= %s" % closed)
    if tab_bad:
        print("tui_walk: tab 名与 VIEWS 常量不符的面 = %s" % tab_bad)
    if thin:
        print("tui_walk: body 过薄的面 = %s" % thin)
    for k in res["keys"]:
        if not k["ok"]:
            print("tui_walk: FAIL %02d %s · %s" % (k["i"], k["name"],
                                                   k["detail"]))
    print("TUI_WALK %s %d/%d"
          % ("OK" if ok else "FAIL", nok, nkeys))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
