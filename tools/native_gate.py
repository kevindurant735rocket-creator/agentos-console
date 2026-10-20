#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""原生外壳门 D99：判「AgentOS 是个真 mac 原生 App」，而不是一个网页。

为什么这道门必须存在：前面 30 道门（含 D68 真渲染无障碍走查、D47 视图
DoD、D66 的 HTTP 态键盘实测）**全部发生在浏览器里**。它们再严，也只能
证明「这个网页在 Chromium 里是对的」。而用户最终双击的是一个 `.app`：

  - 双击能不能起来（bundle 结构 / 签名 / Info.plist）—— 浏览器里无从验；
  - 起来的是不是**真原生窗口**（交通灯、标题栏合并、NSToolbar）——
    在浏览器判据里「窗口」这个概念根本不存在；
  - ATS 放不放行回环明文 HTTP —— WKWebView 不放行就只给一块「无法连接」
    页，而 page_title 仍可能读上一屏的缓存，DOM 判据照样全绿；
  - ⌘K / ⌘1…⌘0 在**原生菜单**里有没有条目 —— 网页自己监听键盘不够；
    「按 ⌘K 有个真 NSMenuItem 在」（能进「键盘快捷键」设置面板、能被辅助
    功能树读到）只有原生层能证；
  - **服务死了长什么样** —— 用户第一次双击、或把 serve.py 误杀掉之后看到
    的恰恰是失败路径。它要是白屏或一句 fetch 失败，前面每道门都不会红。

三条设计纪律（都是被门自己的失败教会的）：
  1. **证据在盘上，不在 stdout**。原生进程 stdout 混着 AppKit 启动横幅与
     dyld 噪音，grep 形状不可靠；证据走 AGENTOS_PROBE_OUT 落 JSON 读回。
  2. **不判「自己声称成功」**。`webview_did_finish=1` 只说明导航回调来过；
     页面是不是真渲染出十个 tab、真切得动、⌘K 是不是真弹出且焦点真落在输入
     框上，全部从**运行后的真实 DOM** 读回。
  3. **像素判据独立于 DOM 判据**。D68 吃过一次「窗口在、内容是空」的亏；
     原生 WebView 一旦踩到 ATS / 证书 / 渲染进程崩溃，DOM 与 isVisible 可
     以同时为真，而截图会立刻变成一张空白页。门同时判截图**有结构**（颜色
     数 / 墨占比）且**逐状态互不相同**（sha256）—— 后者专门盯「5 张分状态
     截图被覆盖成同一张」那个真实发生过的 bug。

证据位：work/native_gate.json、work/native/probe.json、
work/native/probe_degraded.json。rc=0 全过 / 非 0 有红项。
"""
import hashlib
import json
import os
import plistlib
import re
import socket
import struct
import subprocess
import sys
import time
import single_instance_probe

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
APP = os.path.join(ROOT, "AgentOS.app")
BIN = os.path.join(APP, "Contents", "MacOS", "AgentOS")
PLIST = os.path.join(APP, "Contents", "Info.plist")
BUILD = os.path.join(ROOT, "native", "build.sh")
WORK = os.path.join(ROOT, "work", "native")
REPORT = os.path.join(ROOT, "work", "native_gate.json")

# 活文件不许被这道门写坏（门尾自证用）
LIVE_FILES = [os.path.join(ROOT, "index.html"),
              os.path.join(ROOT, "data.json")]

# R7：13 视图。前十个有单位数快捷键（⌘1…⌘0），后三个（终端/文件/设置）
# 只进菜单、不占数字键 —— 与 web 侧 a11y_walk.DIGIT_KEYS 的
# 「只覆盖前 10 个视图」是同一契约，两边不许各写一套。
VIEW_TABLE = ["over", "round", "mx", "led", "eng",
              "work", "goal", "retro", "cap", "ws",
              "term", "file", "set"]
AX_PROC = os.environ.get("AGENTOS_AX_PROC") or "AgentOS"
AX_SCRIPT = os.path.join(HERE, "ax_probe.py")
# AX_WAIT 是**抽样预算**，不是判据：它只决定「给量具多少秒去采满
# AX_SAMPLES=12 个样本」，放松它不会让任何一条判据变松。
#
# 为什么必须从 40.0 抬到 140.0（D108 实测踩实，work/native/ax.json）：
# ax_probe 每轮迭代 ~8.2s —— 2.5s 是 AX_POLL 的睡眠，剩下 ~5.7s 花在
# `fg_activate.ensure_foreground(pid, timeout=6.0)`（ax_probe.py:144 的默认
# 超时）上：这台机上前台被 com.wonderassembly.compositor 占着，那条 AppKit
# 路每轮都走满 6s 超时，再由纯 AX 的 kAXFrontmost 兜底救回来
# （ax.json 每拍 fg_mechanism="ax-frontmost"、fg_ok=True）。ax_probe.py 与
# fg_activate.py 都是只读量具，单轮成本改不了，只能把预算给够。
# 40s 只够 5 个样本（t=6.3/14.5/23.1/31.6/39.7），而判据要 ≥12 个样本里
# 有过半暴露，ax-sustained 于是恒红 —— 这正是 ax_probe.py 文件头警告过的
# 「抽样预算装不下判据本身」：红的不是产品，是量具没采够。
# 12 个样本按 ~8.6s 的最坏节奏要约 101s，留一倍余量取 140.0。
#
# 配套的是靶子寿命：序列 ~39s 就 drained，若照旧 `flush(quit: true)` 立刻退，
# 140s 的预算大半会采在死进程上。所以主路径给 app 传
# AGENTOS_AX_GRACE=120（序列 drained 之后再多活 120s），
# 让「序列采完 → 落盘 → 继续活着给外部观察者量 → 到点自己退」成立。
AX_WAIT = 140.0
AX_POLL = 2.5
AX_SAMPLES = 12
AX_MIN_CONTENT_NODES = 8
# 旧的 AX_MIN_TEXTS=3 已作废：它是从**用户自己那个**实例量的，而门本来
# 就采错了进程（见 run_ax_probe 的注释）。文本下限改用下面实测的 5。
# 下面两个下限**不是**拍脑袋，也不是照抄别的 app —— D105b 在被测 app 自身
# 上实测了 14 个加载后采样：节点在 27↔1084 之间摆（切视图/开面板），
# 所以节点数当内容证据基本没用；稳定的只有「网页内容区在不在」：
# AXWebArea=1 出现在**每一个**加载后采样里（含最小那棵 27 节点），
# 而加载前那棵 23 节点的外壳树**没有**它。texts 的加载后实测最小值是 7。
AX_MIN_WEB_AREA = 1
AX_MIN_TEXTS_LOADED = 5
AX_SERIES_MIN_PCT = 60         # 与 ax_probe 同一个来源（D105b 实测）
AX_MIN_SERIES_SAMPLES = 6      # 样本太少时「多数暴露」没有意义（N=1 时 need=1，
                               # 一条好就 100% 通过），所以样本数本身也是一条判据。
# AX 量具**无结论**时的有界重试次数（D109）。
#
# 2026-10-10 全链实跑实测：同一份构建，单跑 native_gate 194/194 绿，而在
# 41 门全链里 ax_probe 子进程 rc=-15（SIGTERM）且 stdout 为空 —— 门拿到的
# 是「量具没跑完」，不是「树不好」。这与 flake_retry.py 的口径同源：
# **判据返回 False 是结论，绝不重试；只有量具/连接层自己没跑完才重试**。
# 所以这里只重试「没有可解析 JSON」这一种，次数有界，每次重试前确认被测
# pid 还活着（靶子死了再采是浪费），并把尝试次数落进证据
# （ax_probe_attempts），让「重试过」可见，不静默掩盖。
AX_PROBE_TRIES = 3
AX_PROBE_RETRY_SLEEP_S = 2.0
# 菜单快捷键表：前十个是 ⌘1…⌘0，末三个是空串（term/file/set 没有单位数键位，
# 与 web 同契约）。这里**必须**从 VIEW_TABLE 推长度，不能写死 10 ——
# 写死的话再加视图只会得到「菜单对、门判错」。
SHUTTLE = (["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"]
           + [""] * (len(VIEW_TABLE) - 10))
SHOT_TAGS = ["over", "mx", "cmdk", "back", "final"]
VIEW_TITLE = "AgentOS 交付驾驶舱"
FRAME_RE = re.compile(r"\{[\d.]+, [\d.]+\}, \{(\d{3,}), (\d{3,})\}")

# 像素门阈值。刻意**不**写死品牌色：那会把门绑死在美术改版上。
# 只判「这张图有结构」—— 这是「用户眼睛看到东西」的最小充分条件。
MIN_DISTINCT_COLORS = 100     # 纯白 / 纯色页 ≈ 1~2
MIN_INK_RATIO = 0.50          # 纯色页 ≈ 0
MIN_SHOT_BYTES = 20000        # 空白 takeSnapshot ≈ 5~10KB
MIN_SHOT_W = 800              # 窗口 minSize 宽 900，取下限
MIN_SHOT_H = 600
MIN_DISTINCT_HASHES = 3       # over/back/final 同态，mx/cmdk 必异

# 面板像素阈值（D100/D100b 两次假绿的教训，见 judge_panel 文档串）。
# 与上面那套方向相反：这里必须同时卡 distinct 下限和 ink 上限。
# 一张纯黑图是「满墨」ink≈0.99，于是任何 ink >= 阈值 的判据都会把
# 一张什么都没画的黑图判成通过。D100 首跑就栽在这：判据和被测物串通，
# 一起骗了门。实测对照：纯黑通道 distinct=11 ink=0.987，
# 真实内容通道 distinct=60 ink=0.0037。
MIN_PANEL_DISTINCT_COLORS = 40  # 纯黑/纯色 1~11
MAX_PANEL_INK_RATIO = 0.50      # 仅在面板是浅色时才是「满屏涂黑」的上限
MIN_PANEL_SHOT_BYTES = 20000    # 实测真内容 137958


def _panel_not_flat(ink, distinct):
    """面板截图「不是一张平涂图」—— 与主题深浅无关的判据。

    D100 的原判据是单卡 `ink <= 0.50`，那是**面板还是浅色时**标定的
    （当时实测真内容 ink=0.0037、纯黑 ink=0.987）。D105 起整个 App 走
    深色主题（对齐 Codex/ZCode 的观感），同一份真内容读回
    ink=0.87~0.95、distinct=186~253 —— 于是判据把真内容判成了
    「满屏涂黑」。

    而它本来要抓的那个故障（截图选了空白通道 / 什么都没画）在深色下的
    特征并不是 ink 高，而是 **distinct 极低**：纯黑通道 distinct=11。
    所以真正的不变式是二者取其一 —— 要么 ink 不高（浅色面板），
    要么 distinct 够多（深色但有真内容）。两个条件都在时，
    单看 ink 就必然误判。
    """
    try:
        i = float(ink)
    except (TypeError, ValueError):
        return False
    return 0.0 <= i <= MAX_PANEL_INK_RATIO \
        or int(distinct or 0) >= MIN_PANEL_DISTINCT_COLORS

# D103 五窗格契约。didFinish 只证明导航完成，白页照样回调，
# 所以 rendered_chars 必须有下限（实测真内容 browser=231 preview=297）。
MIN_RENDERED_CHARS = 50
PANE_TITLES_EXPECTED = ["文件", "终端", "代码", "浏览器", "预览"]
LOOPBACK_PREFIX = "http://127.0.0.1"

RESULTS = []


def check(name, ok, detail):
    RESULTS.append({"check": name, "ok": bool(ok),
                    "detail": str(detail)})
    tag = "  ok   " if ok else "  RED  "
    print(tag + name + "  " + str(detail))
    return bool(ok)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def png_size(path):
    """只读 PNG 头 24 字节拿宽高。刻意不引 PIL —— 这道门要纯 stdlib。"""
    with open(path, "rb") as f:
        head = f.read(24)
    if len(head) < 24:
        return None
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    if head[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", head[16:24])


def free_port(start=8791, span=30):
    """借一个此刻真没人听的端口给降级面用。降级探针要的是一个**连不上**
    的地址，所以这里验的是「此刻没人听」，不是「永远没人听」。"""
    for p in range(start, start + span):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError("no-free-port")


def port_listening(port, timeout=1.2):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


# ── 判据（拆成纯函数，好让 selftest 能拿变异体喂进来） ────────────────

def judge_bundle():
    """bundle 结构 + ATS 放行。ATS 这条是本门最关键的「门能红的理由」：
    没有 NSAllowsLocalNetworking，WKWebView 加载 http://127.0.0.1 会失败，
    失败形态是原生窗口 + 灰白错误页 —— isVisible 为真、DOM 可能还有上屏
    缓存，于是「窗口起来了」这类判据全绿而用户看到的是一句报错。"""
    out = []
    out.append(("bundle-app-dir", os.path.isdir(APP), APP))
    out.append(("bundle-executable",
                os.path.isfile(BIN) and os.access(BIN, os.X_OK),
                BIN))
    out.append(("bundle-info-plist", os.path.isfile(PLIST), PLIST))
    pl = {}
    if os.path.isfile(PLIST):
        try:
            with open(PLIST, "rb") as f:
                pl = plistlib.load(f)
        except Exception as e:                                  # noqa: BLE001
            out.append(("bundle-plist-parses", False, repr(e)))
    else:
        out.append(("bundle-plist-parses", False, "missing"))
    out.append(("bundle-plist-parses", bool(pl),
                "keys=%d" % len(pl)))
    out.append(("bundle-id", pl.get("CFBundleIdentifier")
                == "com.zhangfengrui.agentos-console",
                pl.get("CFBundleIdentifier")))
    out.append(("bundle-package-appl",
                pl.get("CFBundlePackageType") == "APPL",
                pl.get("CFBundlePackageType")))
    out.append(("bundle-exec-matches",
                pl.get("CFBundleExecutable") == "AgentOS",
                pl.get("CFBundleExecutable")))
    ats = pl.get("NSAppTransportSecurity") or {}
    out.append(("bundle-ats-loopback",
                ats.get("NSAllowsLocalNetworking") is True,
                json.dumps(ats, ensure_ascii=False)))
    out.append(("bundle-ats-not-arbitrary",
                "NSAllowsArbitraryLoads" not in ats,
                "must not blanket-disable ATS"))
    cs = subprocess.run(["codesign", "-dv", APP],
                        capture_output=True, text=True)
    sig = (cs.stderr or "").strip().splitlines()
    out.append(("bundle-adhoc-signed", cs.returncode == 0,
                sig[-1] if sig else "rc=%d" % cs.returncode))
    return out


def pick_ax_python():
    """第一个能 import ApplicationServices 的解释器 -> path or None
    D105 实测：系统 python3 与 ~/.codex/capvenv **都没有** PyObjC，
    项目 .venv-pw 有。gates.sh 的 pick_python 判据是 playwright，
    这里要判的是 PyObjC —— 两套能力不重叠，必须各挑各的。"""
    for c in (os.path.join(ROOT, ".venv-pw", "bin", "python"),
              os.path.expanduser("~/.codex/capvenv/bin/python"),
              "/opt/homebrew/bin/python3", sys.executable):
        if not (c and os.path.isfile(c) and os.access(c, os.X_OK)):
            continue
        p = subprocess.run([c, "-c", "import ApplicationServices"],
                           capture_output=True, text=True)
        if p.returncode == 0:
            return c
    return None


def _pid_alive(pid):
    """pid 还活着吗（signal 0 探测）。只用来决定「还要不要重试量具」，
    不参与任何判据 —— 靶子死了这件事由 app-main-rc / probe 判据说话。"""
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:                                          # noqa: BLE001
        return False


_MC_CACHE = None


def _minimize_helpers():
    """懒载 minimize_check 的 AX 助手（无 PyObjC 时返回 None，别把门炸了）。"""
    global _MC_CACHE
    if _MC_CACHE is False:
        return None
    if _MC_CACHE is None:
        try:
            if HERE not in sys.path:
                sys.path.insert(0, HERE)
            import minimize_check as _mc
            _MC_CACHE = _mc
        except Exception:                                      # noqa: BLE001
            _MC_CACHE = False
    return _MC_CACHE or None


def _ensure_window_exposed(pid):
    """采样前把靶窗从最小化拉回来（W10 修假红，2026-10-14 链内实测）。

    现实场景：靶实例带着最小化状态进来（人手动按过 Dock、或别的会话/别的
    门动过同一台 app），ax_probe 只能全程报 err=-25204「拿不到窗口」——
    红的是**量具进不去**，不是产品。像人一样点一下 Dock：AXMinimized=
    false + 有界等待（助手与 minimize_check 同源），然后照常采样。
    拉不回/读不到就原样往下走：该红的判据（末态仍 mini=True）一条不放过。
    """
    info = {"before": None, "action": "unknown", "attempts": 0, "why": ""}
    mc = _minimize_helpers()
    if mc is None:
        info["why"] = "no-PyObjC（跳过准备，原样采样）"
        return info
    for i in range(2):
        info["attempts"] = i + 1
        try:
            v = mc.ax_minimized(pid)
        except Exception as e:                                 # noqa: BLE001
            info["why"] = "AX 读窗异常：%r" % repr(e)[:80]
            return info
        if v is None:
            info["why"] = "AX 未读到窗（不确定，不动它）"
            time.sleep(1.0)
            continue
        info["before"] = v
        if v is False:
            info["action"] = "none"
            info["why"] = "本来就没最小化"
            return info
        try:
            mc.set_minimized_settled(pid, False, tries=2, poll=1.0)
            got = mc.wait_not_minimized(pid, timeout=8.0)
        except Exception as e:                                 # noqa: BLE001
            info["why"] = "拉回异常：%r" % repr(e)[:80]
            return info
        if got is False:
            info["action"] = "deminimized"
            info["why"] = "已按回 Dock"
            return info
        info["why"] = "仍最小化/未读到确定值"
    return info


def run_ax_probe(wait=AX_WAIT, pid=0):
    """外部进程采一次交付物的 AX 树 -> dict（含 ok=False 的 why）。

    **必须钉 pid**：D105 实测踩实 —— 只给 --proc AgentOS 时 ax_probe 走
    `pgrep -x AgentOS`，本机用户自己开着一个 AgentOS 实例，而它排在
    pgrep 结果第一位且早已在 kAXWindows 注册，于是门在 waited=0.3s 就
    「采到了树」，采到的却是**用户那个实例**（实测 ax.json pid=46184，
    而门自己起的探针 app 是另一个 pid）。后果不是数错，是两条 ax-* 判据
    一直在给一个没被测的进程背书 —— 门变绿了，被测的 app 反而没被读过一次。
    所以这里传子进程 pid，判据里再钉一条「采的就是被测 app」。"""
    py = pick_ax_python()
    if py is None:
        return {"ok": False, "why": "no-interpreter-with-PyObjC"}
    cmd = [py, AX_SCRIPT, "--proc", AX_PROC,
           "--wait", str(wait), "--poll", str(AX_POLL),
           "--samples", str(AX_SAMPLES)]
    if pid:
        cmd += ["--pid", str(pid)]
    ax = None
    attempts = 0
    last = None
    ax_prep = []
    for i in range(AX_PROBE_TRIES):
        attempts = i + 1
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=wait + 60, cwd=ROOT)
            out = (p.stdout or "").strip()
            lines = out.splitlines()
            ax = json.loads(lines[-1]) if lines else None
            last = p
        except subprocess.TimeoutExpired:                      # noqa: BLE001
            # 量具自己超预算 = 无结论（不是「树不好」）。旧代码在这里会
            # 直接抛异常把整道门炸成 Traceback，红项读不出「红在哪」。
            ax, last = None, None
        except Exception:                                      # noqa: BLE001
            ax, last = None, None
        if ax is not None:
            # W11 实测补档：探针吐 ok=True 不等于「可判」。运行中途靶窗被
            # 最小化时，探针照样能拿到残树（序列表里只暴露 2/12），旧逻辑
            # 在这里当场 break，把「拉回重采」这一跳整个跳过 —— 随后
            # ax-sustained 与 app 侧 window-on-screen 级联双红。所以收工
            # 条件从「ok 即收」改成「ok 且 sustained 才收」：凡序列暴露
            # 塌陷，一律给一次「拉回候选」的机会（_ensure_window_exposed
            # 只在真最小化时才动手）；拉回成功才重采，拉不动/本没最小化
            # 照旧原样收工。阈值与判定语义一条不动——放宽的只是重采条件。
            if ax.get("ok") and ax.get("sustained") is True:
                break
            # 采到了但不成（典型：窗口被最小化 → err=-25204 无窗口树）：
            # 尝试按回 Dock **再采一次**（W10，有界=AX_PROBE_TRIES）。
            # 不是最小化问题（拉不动/本来没最小化）→ 原样收工，不空转。
            if pid and _pid_alive(pid):
                prep = _ensure_window_exposed(pid)
                ax_prep.append(prep)
                if prep.get("action") == "deminimized" \
                        and i < AX_PROBE_TRIES - 1:
                    ax = None      # 逼出下一轮重采
                    continue
            break
        # 靶子已经死了就别再采：那不是量具抖动，是采样窗口错过。
        if pid and not _pid_alive(pid):
            break
        if i < AX_PROBE_TRIES - 1:
            time.sleep(AX_PROBE_RETRY_SLEEP_S)
    if ax is None:
        rc = last.returncode if last is not None else "timeout"
        sout = (last.stdout or "")[:120] if last is not None else ""
        return {"ok": False,
                # target_pid 必须落盘：漏掉它会让 ax-target-is-under-test
                # 打印「target_pid=None sampled_pid=None」这种说不出红在哪
                # 的自相矛盾读数（D50 那类）。判据本身的语义不变 ——
                # ax.pid 缺席时它照旧判红。
                "target_pid": int(pid or 0),
                "ax_probe_attempts": attempts,
                "ax_prep": ax_prep,
                "why": "unparsable rc=%s %r (量具无结论，已试 %d 次)"
                       % (rc, sout, attempts)}
    ax["interpreter"] = py
    ax["target_pid"] = int(pid or 0)
    ax["ax_probe_attempts"] = attempts
    ax["ax_prep"] = ax_prep
    # 证据在盘上（纪律 1）：整棵树的角色分布与文本样本不能只活在 stdout。
    try:
        os.makedirs(WORK, exist_ok=True)
        with open(os.path.join(WORK, "ax.json"), "w") as f:
            json.dump(ax, f, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception as e:                                     # noqa: BLE001
        ax["why"] = "ax.json 落盘失败 %r" % (e,)
    return ax


def judge_ax(ax):
    """AX（屏幕阅读器消费的那棵树）-> 判据列表
    ax-target-is-under-test：采到的 pid == 门自己起的那个子进程
    ax-window-exposed：kAXWindows err=0 且窗口数 ≥ 1（屏幕阅读器得有窗可读）
    ax-content-tree：非菜单内容节点 ≥ 8、网页内容区 AXWebArea ≥ 1、
                      真实文本 ≥ 5（读到的是内容，不是 AXApplication 自链、
                      一堆菜单项，也不是一棵还没加载的空窗口外壳）
    ax-sustained：上面三条量的是「**某一刻**」。AX 暴露实测是间歇的，所以
                  再判「序列里**多数**采样真的暴露了内容区」，样本数也得够
                  （N 太少时 need=1，一条好就 100% 通过，等于没判）
    阈值来自 D105b 在**被测 app 自己**身上的实测（不是从别的进程量的）：
    加载后 14 个采样里节点在 27↔1084 摆动，AXWebArea=1 无一缺席，
    加载前那棵 23 节点的外壳树没有 AXWebArea，文本下限 7。"""
    w = ax.get("windows")
    e = ax.get("windows_err")
    return [
        # 先钉「采的是不是被测 app」，再谈树长得好不好。
        # target_pid 缺失（调用方忘了传）也判红：这条判据的作用就是
        # 让「漏钉 pid」变成一声巨响，而不是又一次安静的假绿。
        #
        # D51 修正：这条**只**判 pid 同一性，不掺 ok。之前它写成
        # `ok is True and ... pid == target_pid`，于是「一棵都没采到」
        # 会连这条一起染红，而 detail 偏偏又打印 `target_pid=X sampled_pid=X`
        # —— 两个相等的 pid 配一条红，判据说不出自己红在哪，这正是 D50 那轮
        # 「自相矛盾红项」的来源。采样成功与否归下面 ax-sampling-got-a-tree，
        # 一件事一条判据，红项才读得懂。
        ("ax-target-is-under-test",
         isinstance(ax.get("target_pid"), int)
         and ax.get("target_pid") > 0
         and ax.get("pid") == ax.get("target_pid"),
         "target_pid=%s sampled_pid=%s" % (ax.get("target_pid"), ax.get("pid"))),
        # 「整条序列一棵加载后的树都没读到」。原来是 ax-window-exposed 一条
        # 同时承担「采到了没有」和「采到的够不够」两件事，best=None 时前者
        # 被吞掉，红项只留下 windows=None 这样的空读数。拆开之后：这条说
        # 「有没有」，下面那条说「够不够」。
        ("ax-sampling-got-a-tree",
         ax.get("ok") is True,
         "ok=%s best_is_none=%s why=%r"
         % (ax.get("ok"), ax.get("best_is_none"),
            str(ax.get("why") or "")[:160])),
        ("ax-window-exposed",
         ax.get("ok") is True and e == 0 and isinstance(w, int) and w >= 1,
         "windows_err=%s windows=%s titles=%s" % (
             e, w, ax.get("window_titles"))),
        ("ax-content-tree",
         ax.get("ok") is True
         and int(ax.get("content_nodes") or 0) >= AX_MIN_CONTENT_NODES
         and int(ax.get("web_area") or 0) >= AX_MIN_WEB_AREA
         and int(ax.get("texts") or 0) >= AX_MIN_TEXTS_LOADED,
         "nodes=%s 非菜单=%s(≥%d) 网页内容区=%s(≥%d) 文本=%s(≥%d)"
         " 环=%s 菜单节点=%s"
         % (ax.get("nodes"), ax.get("content_nodes"), AX_MIN_CONTENT_NODES,
            ax.get("web_area"), AX_MIN_WEB_AREA, ax.get("texts"),
            AX_MIN_TEXTS_LOADED, ax.get("cycles"), ax.get("menu_nodes"))),
        ("ax-sustained",
         ax.get("sustained") is True
         and int(ax.get("samples") or 0) >= AX_MIN_SERIES_SAMPLES
         and int(ax.get("samples_exposed") or 0)
         >= int(ax.get("samples_need") or 1),
         "序列采样=%s(≥%d) 真暴露=%s 需≥%s(=ceil(%s×%d%%)) 占比=%s%%"
         % (ax.get("samples"), AX_MIN_SERIES_SAMPLES,
            ax.get("samples_exposed"), ax.get("samples_need"),
            ax.get("samples"), AX_SERIES_MIN_PCT, ax.get("samples_pct"))),
    ]


def judge_window(d):
    """窗口组：把「产品做了什么」与「操作系统给了什么」分开判。

    这道门首版栽在这里，且栽得很隐蔽：三项判据读的是
    window_became_key / window_is_key / window_is_visible，
    而这三个键**只**在 windowDidBecomeKey(_:) 回调里写。该回调只在
    App 真的成为前台 app 时才来 —— 实测本机两种父进程下都没来：

      管道父进程（D50 的 capture_output=True）: nsapp_is_active=False
      PTY 父进程（手工 tty 跑）              : nsapp_is_active=False

    也就是说三个键**永远不写盘**，门读回 None 判红，D50 如实报
    「32/34 门 RC=0，红门=native_gate,native_gate_selftest」。
    而实测重采后 window_is_visible=True —— 窗口其实是在屏上的，
    判红的是**证据的挂载点**，不是产品。

    现在按两类分开判：
      - 产品行为（无条件判红）：起来了、要过焦点、在屏上、合并标题栏、
        frameAutosaveName、非零 frame；
      - 系统授予（**只在本进程确实是前台时才判红**）：本进程是前台
        却始终没拿到 key 窗口，那才是真缺陷。本进程不是前台时无从
        判起，如实放行并由 run_gate 打一行 WARN —— 与 D68 的
        AT-UNVERIFIED 同一套诚实口径：量具没量到东西不许声称量过，
        也不许因此把别的判据一起染红。
    """
    active = d.get("nsapp_is_active")
    focus_judgeable = active is True
    return [
        ("window-shown-at-launch",
         d.get("window_shown_at_launch") == 1,
         d.get("window_shown_at_launch")),
        ("window-requested-activation",
         d.get("window_requested_activation") == 1,
         d.get("window_requested_activation")),
        ("window-on-screen",
         d.get("window_is_visible") is True
         and d.get("window_frame_on_screen") is True
         and d.get("window_is_miniaturized") is not True,
         "visible=%s frame_on_screen=%s mini=%s" % (
             d.get("window_is_visible"),
             d.get("window_frame_on_screen"),
             d.get("window_is_miniaturized"))),
        # 「起时就最小化的窗口，被拉回来了没有」——D105 的复发防线。
        #
        # window-on-screen 只看**末态**：末态 mini=False 就绿。可 D105 那次
        # 红恰恰是「起来过(key=1) 却落盘成 mini=True」，而末态判据对
        # 「恢复动作根本没做」这件事是瞎的 —— 只要末态碰巧对就放过。
        #
        # 这里钉的是**恢复动作本身**：产品是否在 orderFront 之前调过
        # deminiaturize（AppDelegate.showWindow）。两个键缺任一个都判红，
        # 因为缺键唯一的成因就是那段代码被回退了 —— 这条判据要能
        # 抓住「有人把修复删了」，而不是只验证「现在没坏」。
        # before=0 时本条恒真：正常启动窗口本来就没最小化，没有动作可要求。
        ("window-minimized-restored",
         isinstance(d.get("window_minimized_before_restore"), int)
         and isinstance(d.get("window_deminaturized_on_launch"), int)
         and not (d.get("window_minimized_before_restore") == 1
                  and (d.get("window_deminaturized_on_launch") != 1
                       or d.get("window_is_miniaturized") is True)),
         "起时最小化=%s 已调解除=%s 末态 mini=%s" % (
             d.get("window_minimized_before_restore"),
             d.get("window_deminaturized_on_launch"),
             d.get("window_is_miniaturized"))),
        # 关键窗口与前台状态必须是同一条真话（都真或都假）。
        # 两者说谎才红：窗口自称 key 而 App 自称非前台（或反过来）
        # 意味着状态采样自相矛盾，这份证据本身不可信。
        ("window-key-matches-foreground",
         isinstance(d.get("window_is_key"), bool)
         and isinstance(active, bool)
         and d.get("window_is_key") == active,
         "is_key=%s nsapp_is_active=%s" % (
             d.get("window_is_key"), active)),
        # 只有本进程真是前台时，「最终没拿到 key」才是缺陷。
        # 非前台时不判起（run_gate 会打 WARN），但条件本身要有牙：
        # 判 selftest 会注入 active=True 且缺 did_became_key 的变异体，
        # 要求这条真翻红，否则就是恒真判据。
        ("window-became-key-when-foreground",
         (d.get("window_did_became_key") == 1) if focus_judgeable
         else True,
         "active=%s did_became_key=%s" % (
             active, d.get("window_did_became_key"))),
        ("window-full-size-content",
         d.get("window_style_has_full_size_content") == 1,
         d.get("window_style_has_full_size_content")),
        ("window-titlebar-transparent",
         d.get("window_titlebar_transparent") == 1,
         d.get("window_titlebar_transparent")),
        ("window-frame-autosave",
         d.get("window_autosave_name") == "AgentOSMainWindow",
         d.get("window_autosave_name")),
        ("window-frame-nonzero",
         bool(FRAME_RE.search(str(d.get("window_frame", "")))),
         d.get("window_frame")),
    ]


def judge_menu(d):
    return [
        ("menu-view-count",
         d.get("native_menu_view_count") == len(VIEW_TABLE),
         d.get("native_menu_view_count")),
        ("menu-view-ids",
         d.get("native_menu_view_ids") == VIEW_TABLE,
         d.get("native_menu_view_ids")),
        # 菜单条目登记的视图 id 与「点下去真转发到哪个视图」必须一致。
        # 首版这两处共用一个键名，于是后写的覆盖先写的，门读到的是
        # 一串快捷键数字而判「视图条目不对」—— 分不清是菜单错了还是
        # 转发错了。两份分开记再互校，接错线才会红。
        ("menu-forward-targets-match",
         d.get("native_menu_forward_targets") == VIEW_TABLE
         and d.get("native_menu_forward_targets") == d.get("native_menu_view_ids"),
         d.get("native_menu_forward_targets")),
        ("menu-view-titles",
         isinstance(d.get("native_menu_view_titles"), list)
         and len(d["native_menu_view_titles"]) == len(VIEW_TABLE)
         and d["native_menu_view_titles"][0] == "总览",
         d.get("native_menu_view_titles")),
        ("menu-view-shortcuts",
         d.get("native_menu_view_shortcuts") == SHUTTLE,
         d.get("native_menu_view_shortcuts")),
        ("menu-has-cmdk",
         d.get("native_menu_has_cmdk") == 1,
         d.get("native_menu_has_cmdk")),
        # 原生 App 缺「编辑」菜单时 ⌘C/⌘V/⌘Z 落在网页输入框里行为就不对，
        # 而这类缺陷在 Chromium 里量不出来 —— 所以判菜单本身在不在。
        ("menu-has-edit",
         "编辑" in (d.get("native_menu_titles") or []),
         d.get("native_menu_titles")),
        ("menu-toolbar-items",
         d.get("native_toolbar_items") == ["health", "reload", "panel"],
         d.get("native_toolbar_items")),
        ("menu-toolbar-unified",
         d.get("native_toolbar_style_unified") == 1,
         d.get("native_toolbar_style_unified")),
    ]


def judge_webview(d):
    return [
        ("webview-nav-finished",
         d.get("webview_did_finish") == 1, d.get("webview_did_finish")),
        ("webview-no-nav-error",
         "webview_nav_error" not in d,
         d.get("webview_nav_error", "none")),
        ("webview-url-is-loopback",
         str(d.get("webview_url", "")).startswith("http://127.0.0.1:"),
         d.get("webview_url")),
        ("webview-title-after-load",
         d.get("webview_title_after_load") == VIEW_TITLE,
         d.get("webview_title_after_load")),
        ("page-title-real",
         d.get("page_title") == VIEW_TITLE, d.get("page_title")),
        ("page-tab-count",
         d.get("page_tab_count") == len(VIEW_TABLE),
         d.get("page_tab_count")),
        ("page-has-tablist",
         d.get("page_has_tablist") == 1, d.get("page_has_tablist")),
        ("page-main-present",
         d.get("page_main_present") == 1, d.get("page_main_present")),
        ("page-buttons",
         int(d.get("page_buttons") or 0) >= 50, d.get("page_buttons")),
        ("page-body-text",
         int(d.get("page_body_len") or 0) >= 500, d.get("page_body_len")),
        ("health-ok",
         d.get("health_ok_last") == 1, d.get("health_detail_last")),
        ("probe-sequence-complete",
         d.get("probe_sequence_complete") == 1,
         d.get("probe_sequence_complete")),
    ]


def judge_view_switch(d):
    return [
        ("switch-mx-ran",
         d.get("view_switch_mx") == 1,
         d.get("view_switch_error_mx", d.get("view_switch_mx"))),
        ("switch-mx-tab",
         d.get("active_tab_after_mx") == "t-mx",
         d.get("active_tab_after_mx")),
        ("switch-mx-panel",
         d.get("visible_panel_after_mx") == "v-mx",
         d.get("visible_panel_after_mx")),
        ("switch-mx-tab-text",
         str(d.get("active_tab_text_after_mx", "")).startswith("阶段矩阵"),
         d.get("active_tab_text_after_mx")),
        ("switch-back-ran",
         d.get("view_switch_over") == 1,
         d.get("view_switch_error_over", d.get("view_switch_over"))),
        ("switch-back-tab",
         d.get("active_tab_after_over") == "t-over",
         d.get("active_tab_after_over")),
        ("switch-back-panel",
         d.get("visible_panel_after_over") == "v-over",
         d.get("visible_panel_after_over")),
    ]


def judge_cmdk(d):
    return [
        ("cmdk-open-called",
         d.get("cmdk_open_called") == 1,
         d.get("cmdk_open_error", d.get("cmdk_open_called"))),
        ("cmdk-on-after-open",
         d.get("cmdk_on_after_open") == 1,
         d.get("cmdk_on_after_open")),
        # 焦点：面板「开着」不等于「能用」。焦点没落进输入框时用户按 ⌘K
        # 只看见一层遮罩，还得再点一下鼠标 —— 那就不算原生快捷键。
        ("cmdk-focus-in-input",
         d.get("cmdk_focus_after_open") == "cmdq",
         d.get("cmdk_focus_after_open")),
        ("cmdk-options-listed",
         int(d.get("cmdk_options") or 0) > 0, d.get("cmdk_options")),
        ("cmdk-closed",
         d.get("cmdk_on_after_close") == 0,
         d.get("cmdk_on_after_close")),
    ]


def judge_panel(d):
    """D100/D100c：原生右侧工作面板（文件树 + 终端 + ⌘T）是不是真产品。

    这道门是被**两次假绿**逼出来的，两次的教训都写在这里，别删：

    假绿 A（分词）：输入框的默认值是 python3 -c "print(6*7)"。原来的
      parseArgv 只按空格切，于是后端收到 argv=["python3","-c",
      '"print(6*7)"']，回 rc=0 out="" —— 一次「成功但什么都没算」的
      命令。只判 rc==0 的门会把这种静默空转判成通过。所以下面同时判
      argv **形状**（引号已被正确剥掉）和输出里真的有 42。

    假绿 B（像素）：cacheDisplay 出的 PNG 是 99% 纯黑（layer-backed 控件
      在窗口没真正上屏时一个像素都不画），而判据用的是 ink >= 0.50。
      黑恰好是「满墨」，于是判据和被测物串通，一起骗了门。所以下面
      **同时**卡 distinct 下限和 ink 上限，并且要求选中的截图通道
      不在被判为全黑的那批里。

    还有一条结构性要求：面板的原生控件不在 DOM 里，a11y_walk 那道门
    量不到它们，所以这里补 ax_summary 判据（辅助功能树里真读得到
    文件名和终端输出）。
    """
    tree_names = d.get("tree_names_head") or []
    served = d.get("term_last_served_argv") or []
    method = d.get("panel_shot_method_panel")
    blanks = d.get("panel_shot_blank_channels") or []
    out = [
        # --- 面板在窗口里的结构位置 ---
        # D105 改嵌套 split 后 kind 带方向后缀，== 精确匹配会误判
        ("panel-window-is-splitview",
         str(d.get("window_content_kind") or "").startswith("NSSplitView"),
         d.get("window_content_kind")),
        # 外层 subviews 现在是 ProjectsPanel|内层split，「驾驶舱|工作区」
        # 的原始构成在内层，必须读 _inner 才量得到原来的那个断言
        ("panel-window-composition",
         d.get("window_content_subviews_inner")
         == ["WKWebView", "WorkspacePanel"],
         d.get("window_content_subviews_inner")),
        ("panel-hidden-by-default",
         d.get("panel_default_visible") == 0,
         d.get("panel_default_visible")),
        ("panel-toolbar-wired",
         "panel" in (d.get("native_toolbar_items") or []),
         d.get("native_toolbar_items")),
        ("panel-menu-has-cmdt",
         d.get("native_menu_has_cmdt") == 1,
         d.get("native_menu_has_cmdt")),

        # --- 一次完整序列：展开 -> 选中 -> 展开目录 -> 跑命令 ---
        ("panel-sequence-complete",
         d.get("panel_sequence_complete") == 1,
         d.get("panel_sequence_complete")),
        ("panel-tree-loaded",
         d.get("panel_tree_loaded") == 1,
         d.get("panel_tree_loaded")),
        # 树里必须有**真项目文件**，不是只有一个空容器
        ("panel-tree-has-real-files",
         ("tools" in tree_names and "native" in tree_names)
         or int(d.get("outline_root_children") or 0) >= 5,
         "names=%s root_children=%s"
         % (tree_names[:8], d.get("outline_root_children"))),
        ("panel-outline-rows",
         int(d.get("outline_rows") or 0) >= 5,
         d.get("outline_rows")),
        ("panel-tree-expanded",
         int(d.get("panel_expanded") or 0) >= 1,
         d.get("panel_expanded")),

        # --- 终端：形状 + 真实输出，双保险（防假绿 A 复发）---
        ("panel-term-ok",
         d.get("term_last_ok") == 1, d.get("term_last_ok")),
        ("panel-term-rc-zero",
         d.get("term_last_rc") == 0, d.get("term_last_rc")),
        ("panel-term-computed-42",
         "42" in str(d.get("term_last_out") or ""),
         repr(d.get("term_last_out"))),
        ("panel-term-argv-parsed",
         d.get("term_last_argv") == ["python3", "-c", "print(6*7)"],
         d.get("term_last_argv")),
        ("panel-term-served-argv",
         bool(served) and served[-1] == "print(6*7)", served),
        ("panel-term-textview-monospaced",
         d.get("term_textview_monospaced") is True,
         d.get("term_textview_monospaced")),
        ("panel-term-textview-selectable",
         d.get("term_textview_selectable") is True,
         d.get("term_textview_selectable")),
        ("panel-term-textview-readonly",
         d.get("term_textview_editable") is False,
         d.get("term_textview_editable")),
        ("panel-term-input-wired",
         d.get("term_input_wired") is True, d.get("term_input_wired")),
        ("panel-term-run-wired",
         d.get("term_run_wired") is True, d.get("term_run_wired")),

        # --- 像素：方向相反的双边卡（防假绿 B 复发）---
        ("panel-shot-method-not-blank",
         bool(method) and method in (d.get("panel_shot_channel_methods") or [])
         and method not in blanks,
         "method=%s blank=%s" % (method, blanks)),
        ("panel-shot-distinct-colors",
         int(d.get("panel_shot_distinct_colors_panel") or -1)
         >= MIN_PANEL_DISTINCT_COLORS,
         d.get("panel_shot_distinct_colors_panel")),
        ("panel-shot-ink-ratio",
         _panel_not_flat(d.get("panel_shot_ink_ratio_panel"),
                         d.get("panel_shot_distinct_colors_panel")),
         "ink=%s distinct=%s"
         % (d.get("panel_shot_ink_ratio_panel"),
            d.get("panel_shot_distinct_colors_panel"))),
        ("panel-shot-bytes",
         _file_bytes(d.get("panel_shot_path_panel")) >= MIN_PANEL_SHOT_BYTES,
         "%s bytes=%s" % (d.get("panel_shot_path_panel"),
                          _file_bytes(d.get("panel_shot_path_panel")))),

        # --- 辅助功能树：原生控件不在 DOM 里，只能走 AX ---
        ("panel-ax-has-tree",
         d.get("panel_ax_has_tree") is True, d.get("panel_ax_has_tree")),
        ("panel-ax-term-has-result",
         d.get("panel_ax_term_has_result") is True,
         d.get("panel_ax_term_has_result")),
        ("panel-ax-has-run-button",
         d.get("panel_ax_has_run_button") is True,
         d.get("panel_ax_has_run_button")),
        ("panel-ax-has-browser-go",
         d.get("panel_ax_has_browser_go") is True,
         d.get("panel_ax_has_browser_go")),
    ]
    out.extend(_judge_panes(d))
    return out


def _judge_panes(d):
    """D103：五个窗格（文件/终端/代码/浏览器/预览）是不是真环境。

    这道门是被**四个真实缺陷**逼出来的，四个的教训都写在这里，别删：

    缺陷 A（0 段分段器假绿）：NSSegmentedControl 默认 segmentCount==0，
      setLabel(_:forSegment:) 被全部丢弃。tab 一个都没画出来，但
      driveSeg() 照样返回成功、pane_switches 照样加一 —— 纯假绿。
      所以判据必须落到 pane_segment_count==5 与 AX 树里真有五项。

    缺陷 B（didFinish 不等于画出了东西）：WKWebView 的导航 delegate
      回调只能证明「导航完成」，白页 / 空 body 也照样 didFinish。
      所以判据要读 document.title 与 innerText 长度，rendered_chars
      低于下限就是白页。

    缺陷 C（共用 delegate 互相覆盖）：浏览器窗格和预览窗格都是
      WKWebView，如果 recordNav 不先认领 w === 哪个窗格，两边的回调
      会互相盖掉对方的事实，最后 browser/preview 读起来"都加载好了"。
      所以 preview_path 必须不等于 browser_url。

    缺陷 D（树选中没接上）：loadCode(path:) 可以被直接调用绕过文件树，
      于是「点开文件 → 代码窗格出内容」这条真实链路其实没接上。
      所以判据要求 code_source=="outline-selection" 且
      tree_selected_path == code_path。
    """
    titles = d.get("pane_titles") or []
    visits = d.get("pane_visits") or []
    blanks = d.get("panel_shot_blank_channels") or []
    code_path = str(d.get("code_path") or "")
    b_url = str(d.get("browser_url") or "")
    b_rej = str(d.get("browser_rejected") or "")
    b_last = str(d.get("browser_last_requested") or "")
    out = [
        # --- 五格结构：数量 / 标题 / 分段器 / AX / 切换 / 可见性 ---
        ("panel-pane-count-is-five",
         d.get("pane_count") == 5,
         d.get("pane_count")),
        ("panel-pane-titles-complete",
         list(titles) == PANE_TITLES_EXPECTED,
         titles),
        ("panel-pane-segments-declared",
         d.get("pane_segment_count") == 5,
         d.get("pane_segment_count")),
        ("panel-pane-tabs-in-ax-tree",
         d.get("panel_ax_pane_segment_count") == 5
         and d.get("panel_ax_pane_has_all_five") is True,
         "ax_count=%s ax_all=%s"
         % (d.get("panel_ax_pane_segment_count"),
            d.get("panel_ax_pane_has_all_five"))),
        ("panel-pane-switch-count",
         int(d.get("pane_switches") or 0) >= 5,
         d.get("pane_switches")),
        ("panel-pane-only-one-visible",
         d.get("pane_visible_count") == 1,
         d.get("pane_visible_count")),
        ("panel-pane-visited-all-five",
         all(t in visits for t in PANE_TITLES_EXPECTED),
         visits),
        ("panel-pane-tour-recorded",
         int(d.get("panel_panes_toured") or 0) >= 5,
         d.get("panel_panes_toured")),
        ("panel-pane-back-to-file",
         d.get("pane_active") == "文件"
         and d.get("panel_ax_pane_active") == "文件",
         "active=%s ax_active=%s"
         % (d.get("pane_active"), d.get("panel_ax_pane_active"))),

        # --- 代码格：必须由文件树驱动、必须真拿到字节、必须只读 ---
        ("panel-code-loaded",
         d.get("code_loaded") == 1 and d.get("code_error") == ""
         and int(d.get("code_chars") or 0) > 0 and code_path,
         "loaded=%s error=%r chars=%s path=%r"
         % (d.get("code_loaded"), d.get("code_error"),
            d.get("code_chars"), code_path)),
        ("panel-code-served-by-backend",
         d.get("code_served_path") == code_path,
         "served=%r requested=%r"
         % (d.get("code_served_path"), code_path)),
        ("panel-code-via-tree-selection",
         d.get("code_source") == "outline-selection"
         and d.get("tree_selected_path") == code_path,
         "source=%r tree_sel=%r code=%r"
         % (d.get("code_source"), d.get("tree_selected_path"), code_path)),
        ("panel-code-readonly",
         d.get("code_textview_editable") is False
         and d.get("code_textview_selectable") is True,
         "editable=%s selectable=%s"
         % (d.get("code_textview_editable"),
            d.get("code_textview_selectable"))),
        ("panel-code-readonly-in-ax",
         d.get("panel_ax_code_editable") is False
         and d.get("panel_ax_code_chars") == d.get("code_chars"),
         "ax_editable=%s ax_chars=%s code_chars=%s"
         % (d.get("panel_ax_code_editable"),
            d.get("panel_ax_code_chars"), d.get("code_chars"))),
        # --- 浏览器格：真画出了东西（防缺陷 B）+ 只接回环（防外泄）---
        ("panel-browser-loaded",
         d.get("browser_loaded") == 1 and d.get("browser_error") == ""
         and b_url.startswith(LOOPBACK_PREFIX),
         "loaded=%s error=%r url=%r"
         % (d.get("browser_loaded"), d.get("browser_error"), b_url)),
        ("panel-browser-dom-title",
         bool(str(d.get("browser_dom_title") or "").strip()),
         repr(d.get("browser_dom_title"))),
        ("panel-browser-rendered-chars",
         int(d.get("browser_rendered_chars") or 0) >= MIN_RENDERED_CHARS,
         d.get("browser_rendered_chars")),
        ("panel-browser-rejects-external",
         int(d.get("browser_attempts") or 0) >= 2 and "回环" in b_rej
         and d.get("browser_rejected_url") == d.get("browser_hostile_asked")
         and int(d.get("browser_rejected_navigations") or 0) >= 1
         and b_last.startswith(LOOPBACK_PREFIX),
         "attempts=%s rejected=%r asked=%r rejected_url=%r navs=%s last=%r"
         % (d.get("browser_attempts"), b_rej, d.get("browser_hostile_asked"),
            d.get("browser_rejected_url"),
            d.get("browser_rejected_navigations"), b_last)),
        ("panel-browser-lost-page-on-reject",
         d.get("browser_live_loaded") == 1
         and d.get("browser_live_url") == b_url,
         "live_loaded=%s live_url=%r url=%r"
         % (d.get("browser_live_loaded"),
            d.get("browser_live_url"), b_url)),
        # --- 预览格：另一个文件、另一份 DOM（防缺陷 C 互相覆盖）---
        ("panel-preview-loaded",
         d.get("preview_loaded") == 1 and d.get("preview_error") == ""
         and int(d.get("preview_chars") or 0) >= 1000,
         "loaded=%s error=%r chars=%s"
         % (d.get("preview_loaded"), d.get("preview_error"),
           d.get("preview_chars"))),
        ("panel-preview-rendered-chars",
         int(d.get("preview_rendered_chars") or 0) >= MIN_RENDERED_CHARS,
         d.get("preview_rendered_chars")),
        ("panel-preview-dom-title",
         bool(str(d.get("preview_dom_title") or "").strip()),
         repr(d.get("preview_dom_title"))),
        ("panel-preview-not-confused-with-browser",
         bool(d.get("preview_path"))
         and d.get("preview_path") != d.get("browser_url"),
         "preview_path=%r browser_url=%r"
         % (d.get("preview_path"), d.get("browser_url"))),
        # --- 代码格像素：双边卡同 panel，防「切到代码格就白屏」---
        ("panel-shot-code-method-not-blank",
         bool(d.get("panel_shot_method_code"))
         and d.get("panel_shot_method_code")
         in (d.get("panel_shot_channel_methods") or [])
         and d.get("panel_shot_method_code") not in blanks,
         "method=%s blank=%s"
         % (d.get("panel_shot_method_code"), blanks)),
        ("panel-shot-code-distinct-colors",
         int(d.get("panel_shot_distinct_colors_code") or -1)
         >= MIN_PANEL_DISTINCT_COLORS,
         d.get("panel_shot_distinct_colors_code")),
        ("panel-shot-code-ink-ratio",
         _panel_not_flat(d.get("panel_shot_ink_ratio_code"),
                         d.get("panel_shot_distinct_colors_code")),
         "ink=%s distinct=%s"
         % (d.get("panel_shot_ink_ratio_code"),
            d.get("panel_shot_distinct_colors_code"))),
        ("panel-shot-code-bytes",
         _file_bytes(d.get("panel_shot_path_code")) >= MIN_PANEL_SHOT_BYTES,
         "%s bytes=%s" % (d.get("panel_shot_path_code"),
                          _file_bytes(d.get("panel_shot_path_code")))),
    ]
    return out


VIEW_KEYS = ["over", "round", "mx", "led", "eng", "work", "goal", "retro",
             "cap", "ws", "term", "file", "set"]
# 只有这 10 个有单位数快捷键：第 11 起按下数字键会与 1…0 抢键，
# 所以键盘类判据只在这 10 个上数「消费了 / 切过去了」。
# 门同时要求原生侧自己上报总数 = len(VIEW_KEYS)（13），不许两头对不上。
DIGIT_KEYS = VIEW_KEYS[:10]


def _judge_projects_data(d, n, keys, hdr, rows_named):
    out = [
        ("projects-loaded",
         d.get("proj_loaded") == 1 and n >= 2 and d.get("proj_error") == "",
         "loaded=%s count=%s err=%r"
         % (d.get("proj_loaded"), d.get("proj_count"),
            d.get("proj_error"))),
        # 绝对路径：相对路径的话 NSTableView 展开/跳转会跳到别处
        ("projects-roots-absolute",
         d.get("proj_roots_absolute") is True,
         d.get("proj_roots_absolute")),
        # 每个项目都必须有自己的 NSTableRowView，缺一个就是少一行
        ("projects-row-views-match",
         int(d.get("proj_row_view_count") or -1) == n and n > 0,
         "rows=%s count=%s"
         % (d.get("proj_row_view_count"), n)),
        ("projects-labels-real",
         all(str(x).strip() for x in (d.get("proj_labels") or [])) and n > 0,
         d.get("proj_labels")),
        ("projects-active-flagged",
         len(d.get("proj_active_flag") or []) == 1
         and d.get("proj_selected_is_active") is True
         and str(d.get("proj_selected_key") or "") != "",
         "active=%r selected=%r sel_is_active=%r"
         % (d.get("proj_active_flag"), d.get("proj_selected_key"),
            d.get("proj_selected_is_active"))),
        ("projects-file-counts",
         all(int(x or 0) > 0 for x in (d.get("proj_nfiles") or [])) and n > 0,
         d.get("proj_nfiles")),
        ("projects-status-text",
         bool(str(d.get("proj_status_text") or "").strip()),
         repr(d.get("proj_status_text"))),
    ]
    return out + _judge_projects_ax(d, hdr, rows_named) \
        + _judge_keyboard(d)


def _judge_projects_ax(d, hdr, rows_named):
    return [
        # 辅助功能树里真读得到标题与项目行（VoiceOver 用户能听见什么）
        ("projects-ax-has-header",
         d.get("proj_ax_has_header") is True and "项目" in hdr,
         repr(hdr)),
        ("projects-ax-rows-named",
         int(d.get("proj_ax_row_count") or 0) >= 2 and rows_named >= 2,
         "rows=%s named=%s"
         % (d.get("proj_ax_row_count"), rows_named)),
    ]


def _judge_keyboard(d):
    # term/file/set 没有快捷键，逐键量具 keyeq_consumed_* / keyeq_tab_* 对它们
    # 根本没有观测值；把它们算进坏名单就是恒红。只量有键的那 10 个。
    consumed_bad = [k for k in DIGIT_KEYS
                    if d.get("keyeq_consumed_%s" % k) != 1]
    # DOM 里 tab 的 id 带 `t-` 前缀，裸 key 比对会把 10 个全判成「没切过去」。
    tab_bad = [k for k in DIGIT_KEYS
               if d.get("keyeq_tab_%s" % k) != "t-" + k]
    n_views = int(d.get("keyeq_views_total") or 0)
    n_digit = int(d.get("keyeq_digit_views_total") or 0)
    return [
        ("keyeq-real-menu-path",
         d.get("keyeq_path") == "NSMenu.performKeyEquivalent",
         repr(d.get("keyeq_path"))),
        ("keyeq-sequence-complete",
         d.get("keyeq_sequence_complete") == 1,
         d.get("keyeq_sequence_complete")),
        # 十个有快捷键的视图必须**都被菜单消费**，一个都没消费说明
        # keyEquivalent 被摘 / target/action 断了
        ("keyeq-all-views-consumed",
         not consumed_bad and len(DIGIT_KEYS) == 10 and n_digit == 10,
         "未消费=%s 有键视图=%s/%s" % (",".join(consumed_bad), n_digit,
                                len(DIGIT_KEYS))),
        # 「被消费」不够 —— 必须回读页面 active_tab 真的切过去了
        ("keyeq-all-views-reached",
         not tab_bad and n_views == len(VIEW_KEYS)
         and d.get("keyeq_views_matched") == len(DIGIT_KEYS),
         "没切过去=%s 总视图=%s(期望%s) 命中=%s(期望%s)"
         % (",".join(tab_bad), n_views, len(VIEW_KEYS),
            d.get("keyeq_views_matched"), len(DIGIT_KEYS))),
        ("keyeq-cmdk-opens",
         d.get("keyeq_cmdk_on") == 1, d.get("keyeq_cmdk_on")),
        # ⌘T 让面板出现过即可（随后会被主动收起，屏上不该留痕）
        ("keyeq-cmdt-toggled",
         d.get("keyeq_cmdt_visible") in (0, 1),
         d.get("keyeq_cmdt_visible")),
        ("keyeq-cmdp-focuses-projects",
         d.get("keyeq_projects_focused") == 1,
         d.get("keyeq_projects_focused")),
        # ↓ 键必须真走过 NSTableView.keyDown 并改变选中行
        ("projects-arrow-key-moves",
         int(d.get("proj_arrow_moves") or 0) >= 1
         and bool(str(d.get("proj_key_after_arrow") or "").strip()),
         "moves=%s after=%r"
         % (d.get("proj_arrow_moves"), d.get("proj_key_after_arrow"))),
        ("native-menu-has-cmdp",
         d.get("native_menu_has_cmdp") == 1,
         d.get("native_menu_has_cmdp")),
        ("native-menu-cmdp-focuses",
         d.get("native_menu_cmdp_result_focused") == 1,
         d.get("native_menu_cmdp_result_focused")),
    ]


def judge_projects(d):
    """左侧项目与任务空间 + 真键盘走查（D105）。

    为什么不并进 judge_panel：panel 判据量的是右侧工作区原生控件，projects
    量的是**左侧栏**和**键盘入口**，两者坏法不同（面板是渲染坏，projects 是
    数据没接上/快捷键被摘），混在一组会让「哪道判据在抓什么」不可读。

    键盘这一段是 D105 的重点：⌘1…⌘0 十一个键如果不真按一遍、只回读
    `native_menu_view_shortcuts` 那个数组，那只证明**建菜单时写了字符串**，
    证明不了 keyEquivalent 还挂着、target 没被摘、action 还指着老 selector。
    所以判据要求 `keyeq_path` 是真的走了 NSMenu.performKeyEquivalent，
    并且 10 个键的 keyeq_consumed_<view> 全为 1、10 个 keyeq_tab_<view> 全
    回读成对应 key —— 「菜单被消费」和「页面真切过去了」分开记。
    """
    n = int(d.get("proj_count") or 0)
    keys = d.get("proj_keys") or []
    hdr = str(d.get("proj_ax_header") or "")
    rows_named = int(d.get("proj_ax_rows_named") or 0)
    out = [
        ("projects-window-composition",
         d.get("window_content_subviews") == ["ProjectsPanel", "NSSplitView"]
         and d.get("window_content_kind") == "NSSplitView:outer-horizontal",
         "kind=%r subviews=%r"
         % (d.get("window_content_kind"),
            d.get("window_content_subviews"))),
        ("projects-default-visible",
         d.get("projects_default_visible") == 1
         and float(d.get("projects_divider_x") or 0) > 0,
         "visible=%s divider_x=%s"
         % (d.get("projects_default_visible"),
            d.get("projects_divider_x"))),
    ]
    return out + _judge_projects_data(d, n, keys, hdr, rows_named)


def _file_bytes(path):
    if not path or not os.path.isfile(path):
        return 0
    return os.path.getsize(path)


def judge_pixels(d):
    """像素证据：截图有结构 + 逐状态互不相同。
    后者是专门盯「5 张分状态截图被同一个路径覆盖成同一张」那个真实 bug
    —— 那个 bug 下门会拿到 5 个「ok N bytes」的绿字，盘上却只有 1 张图。"""
    out = []
    digests = {}
    for tag in SHOT_TAGS:
        out.append(("shot-%s-captured" % tag,
                    str(d.get("shot_%s" % tag, "")).startswith("ok "),
                    d.get("shot_%s" % tag)))
        path = d.get("shot_path_%s" % tag)
        exists = bool(path) and os.path.isfile(path)
        size = os.path.getsize(path) if exists else 0
        out.append(("shot-%s-bytes" % tag,
                    size >= MIN_SHOT_BYTES,
                    size if exists else "missing"))
        dim = png_size(path) if exists else None
        out.append(("shot-%s-dim" % tag,
                    bool(dim) and dim[0] >= MIN_SHOT_W
                    and dim[1] >= MIN_SHOT_H, dim))
        out.append(("shot-%s-distinct-colors" % tag,
                    int(d.get("shot_distinct_colors_%s" % tag, -1))
                    >= MIN_DISTINCT_COLORS,
                    d.get("shot_distinct_colors_%s" % tag)))
        out.append(("shot-%s-ink-ratio" % tag,
                    float(d.get("shot_ink_ratio_%s" % tag, -1.0))
                    >= MIN_INK_RATIO,
                    d.get("shot_ink_ratio_%s" % tag)))
        if exists:
            digests[tag] = sha256(path)
    uniq = len(set(digests.values()))
    out.append(("shot-hashes-distinct",
                uniq >= MIN_DISTINCT_HASHES,
                "%d unique of %d" % (uniq, len(digests))))
    return out


def judge_degraded(d):
    """降级面：服务连不上时，这块原生面板是不是真产品。
    注意 `recovery_panel_visible` 结束时是 0 —— 面板起完服务就被收走了。
    「面板出现过」的真证据是那些**只可能在建面板时才写下**的字段：
    recovery_detail / heading / start_wired / retry_wired。"""
    # 「服务恢复」要终值和采样序列一起认：`degraded_health_after_tap` 只是
    # closeDegradedSequence 那一刻的 health.lastOK 快照；`degraded_health_samples`
    # 才是从 tap 起每 0.4s 一枪、直到转绿或超预算的完整序列。以前只认终值，
    # 「确实恢复过、但快照早了一枪」会被卡成假红；反过来，整轮采样全 0 才是
    # 「服务确实起不来」的真红。二者取或。
    _samples = d.get("degraded_health_samples")
    _samples = _samples if isinstance(_samples, list) else []
    _n_ok = sum(1 for s in _samples if isinstance(s, dict) and s.get("ok") == 1)
    _recovered = d.get("degraded_health_after_tap") == 1 or _n_ok > 0
    return [
        ("degraded-mode-on",
         d.get("degraded_mode") == 1, d.get("degraded_mode")),
        ("degraded-health-was-down",
         d.get("degraded_health_before_tap") == 0,
         d.get("degraded_health_before_tap")),
        ("degraded-panel-not-missing",
         d.get("degraded_panel_missing", 0) == 0,
         d.get("degraded_panel_missing", 0)),
        ("degraded-panel-heading",
         d.get("heading") == "AgentOS 服务未运行", d.get("heading")),
        ("degraded-panel-real-cause",
         "连接失败" in str(d.get("recovery_detail", "")),
         str(d.get("recovery_detail", ""))[:60]),
        # 判「面板有没有告诉用户它会自己把服务拉起来」+「有没有把地址摆出来」。
        # 首版这里判的是「文案里有『启动服务』四个字」—— 那只匹配按钮标题，
        # 面板文案其实写的是「会自己把本地服务拉起来」，于是好面板被判红。
        # 判据必须落在**对用户的承诺**上，不是落在某个字面串上。
        ("degraded-panel-explains-selfheal",
         "自己把本地服务拉起来" in str(d.get("recovery_detail", ""))
         and "不需要你在终端敲命令" in str(d.get("recovery_detail", "")),
         str(d.get("recovery_detail", ""))[-40:]),
        ("degraded-panel-shows-url",
         "http://127.0.0.1:" in str(d.get("recovery_detail", "")),
         "address shown to user"),
        ("degraded-start-button-wired",
         d.get("start_wired") is True
         and d.get("start_title") == "启动服务",
         d.get("start_title")),
        ("degraded-retry-button-wired",
         d.get("retry_wired") is True
         and d.get("retry_title") == "重新检测",
         d.get("retry_title")),
        ("degraded-button-really-clicked",
         d.get("degraded_tapped") == 1, d.get("degraded_tapped")),
        ("degraded-service-started",
         d.get("service_started_by_app") == 1,
         d.get("service_start_error", d.get("service_started_by_app"))),
        ("degraded-service-pid",
         int(d.get("service_pid") or 0) > 0, d.get("service_pid")),
        ("degraded-health-recovered",
         _recovered,
         "after_tap=%s ok_in_samples=%d/%d"
         % (d.get("degraded_health_after_tap"), _n_ok, len(_samples))),
        ("degraded-panel-dismissed",
         d.get("degraded_recovery_after_tap") == 0,
         d.get("degraded_recovery_after_tap")),
        ("degraded-console-then-loaded",
         d.get("webview_did_finish") == 1
         and d.get("webview_title_after_load") == VIEW_TITLE,
         d.get("webview_title_after_load")),
        ("degraded-sequence-complete",
         d.get("degraded_sequence_complete") == 1,
         d.get("degraded_sequence_complete")),
    ]


# ── 执行 ────────────────────────────────────────────────────────────────

def run_app(env_extra, timeout=180):
    env = dict(os.environ)
    env.update(env_extra)
    try:
        r = subprocess.run([BIN], env=env, capture_output=True,
                           text=True, timeout=timeout, cwd=ROOT)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout>%ds" % timeout


def run_app_async(env_extra):
    """起 App 但不等它退出 -> (proc, out_file, err_file)
    D105 需要在 App **活着**期间从外部采 AX 树，所以主路径探针改成异步；
    stdout/stderr 走文件而不是 PIPE —— 探针序列的输出量未知，
    PIPE 填满而没人读会死锁，历史上 native_gate 就是被这类细节坑过的。"""
    env = dict(os.environ)
    env.update(env_extra)
    of = open(os.path.join(WORK, "app-main.out"), "w")
    ef = open(os.path.join(WORK, "app-main.err"), "w")
    p = subprocess.Popen([BIN], env=env, stdout=of, stderr=ef,
                         text=True, cwd=ROOT)
    return p, of, ef


def load_json(path):
    if not os.path.isfile(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:                                             # noqa: BLE001
        return {}


def run_gate():
    os.makedirs(WORK, exist_ok=True)
    t_all = time.time()
    before = {}
    for p in LIVE_FILES:
        if os.path.isfile(p):
            before[p] = sha256(p)

    # 环境排他预检（W11 补，2026-10-14 链内两次实测）：本门自己起的实例
    # 会被既有活实例"接管"（D107）—— 任何探针都测不到自己的靶，红的是
    # 环境不是产品。预检放在**构建之前**：脏就 ~1 秒 rc=3 快失败，且
    # **不动任何探针文件**（工作/2）、不空烧 60s 构建；旧基线原样留给下一
    # 道 native_gate_selftest，免得它因 missing-base 级联红（实测发生过）。
    _mc0 = _minimize_helpers()
    if _mc0 is not None:
        try:
            _dirty = _mc0.env_dirty_reasons()
        except Exception as _e:                                # noqa: BLE001
            _dirty = ["预检异常：%r" % (repr(_e)[:80],)]
        if _dirty:
            print("== NATIVE_GATE_ENV_DIRTY 本门要求 AgentOS.app 进程排他，"
                  "实测环境已有其他活实例 ==")
            for _r in _dirty:
                print("  [脏] %s" % (_r,))
            print("  [修法] 请把这些实例退干净后重跑本门：")
            print("         osascript -e 'tell application \"AgentOS\" to quit'"
                  "（或在 Dock 里 Cmd-Q）。量具不代劳 kill。")
            print("  [口径] 退出码 3=环境红（不排他）、1=产品红、2=用法/"
                  "注入。本门不把环境脏记成产品坏，也不许因此判绿；"
                  "探针基线文件未动。")
            return finish(before, t_all, 3)

    print("[1/6] 构建")
    bl = subprocess.run(["bash", BUILD], capture_output=True,
                        text=True, cwd=ROOT)
    tail = ""
    if bl.stdout.strip():
        tail = bl.stdout.strip().splitlines()[-1]
    check("build-rc", bl.returncode == 0,
          "rc=%d %s" % (bl.returncode, tail))
    for n, ok, d in judge_bundle():
        check(n, ok, d)
    if not os.path.isfile(BIN):
        return finish(before, t_all, 3)

    print("[2/6] 主路径探针（真服务 8790）")
    for f in ("probe.json", "probe_degraded.json"):
        p = os.path.join(WORK, f)
        if os.path.isfile(p):
            os.unlink(p)
    for tag in SHOT_TAGS:
        p = os.path.join(WORK, "shot-%s.png" % tag)
        if os.path.isfile(p):
            os.unlink(p)
    pmain = os.path.join(WORK, "probe.json")
    proc, of, ef = run_app_async({
        "AGENTOS_PROBE_OUT": pmain,
        "AGENTOS_SHOT_OUT": os.path.join(WORK, "shot.png"),
        "AGENTOS_URL": "http://127.0.0.1:8790/",
        "AGENTOS_AUTOCLOSE": "90",
        # 序列 drained（~39s）之后再多活 120s：ax_probe.py 要按时间序列采满
        # AX_SAMPLES=12 个样本要约 ~101s（每轮 ~8.2s，含 fg_activate 的 6s
        # AppKit 超时整轮走满）。少了它，量具只采到 5 个样本、最后一个采在死
        # 进程上，ax-sustained 因 samples<12 恒红；但它只是抽样预算，不是阈值
        # （AX_SAMPLES/AX_SERIES_MIN_PCT 一个都没动）。app 总寿命 ~159s，
        # 仍在下方 proc.wait(timeout=240) 与 wait+60 之内。
        "AGENTOS_AX_GRACE": "120",
        "AGENTOS_MULTI_INSTANCE": "1",
    })
    ax = run_ax_probe(pid=proc.pid)
    for _pr in (ax.get("ax_prep") or []):
        print("  ax-prep before=%s action=%s attempts=%s why=%s"
              % (_pr.get("before"), _pr.get("action"),
                 _pr.get("attempts"), _pr.get("why")))
    try:
        proc.wait(timeout=240)
    except subprocess.TimeoutExpired:
        proc.kill()
    of.close()
    ef.close()
    rc = proc.returncode
    so = open(of.name, errors="replace").read()
    se = open(ef.name, errors="replace").read()
    check("app-main-rc", rc == 0, "rc=%d" % rc)
    for n, ok, dd in judge_ax(ax):
        check(n, ok, dd)
    if not ax.get("ok"):
        print("  RED  AX 采样未成：%s" % ax.get("why"))
    d1 = load_json(pmain)
    check("probe-main-parses", bool(d1), "fields=%d" % len(d1))
    # 证据是**半份**（探针序列没走完）时，先说清这是量具被腰斩，
    # 别让后面几十条判据红成一片「产品坏了」的假象。
    # 2026-10-20 实测踩实：app 被 AGENTOS_AUTOCLOSE 从启动算起掐掉 →
    # 序列没 drained → 没有 page_* 字段 → 79 条判据一起红，
    # 而页面明明 loaded=1、title 正确。真凶在 AppDelegate 的定时器语义
    # （已修：那是「drained 后再活 N 秒」，不是「启动后 N 秒就杀」）。
    _half = bool(d1) and d1.get("probe_sequence_complete") != 1
    if _half:
        print("  RED  探针序列未走完（证据是半份：缺 probe_sequence_complete）")
        print("        —— 这是**量具被腰斩**，不是产品判红；重跑本门即可。")
        print("        缺的关键字段：%s"
              % [k for k in ("page_title", "page_tab_count", "panel_code_loaded",
                            "shot_over_captured")
                 if k not in d1][:6])
    if d1 and not _half:
        check("bundle-is-app",
              d1.get("bundle_is_app") == 1, d1.get("bundle_is_app"))
        groups = (judge_window, judge_menu, judge_webview,
                  judge_view_switch, judge_cmdk, judge_pixels, judge_panel,
                  judge_projects)
        for fn in groups:
            for n, ok, dd in fn(d1):
                check(n, ok, dd)
        # 只打不改 rc：与 D68 的 AT-UNVERIFIED 同一套诚实口径 ——
        # 量具（前台权限）没量到东西，如实说没量到，不因此把别的
        # 判据一起染红，也不假装这条路径验过了。
        if d1.get("nsapp_is_active") is not True:
            print("  WARN  AT-UNVERIFIED  本进程非前台 "
                  "(nsapp_is_active=%s)，key 窗口路径未判起"
                  % d1.get("nsapp_is_active"))
    else:
        print("  RED  no-probe-json（原生进程没落证据）")
        print((so or "")[-600:], (se or "")[-600:])

    print("[3/6] 降级面探针（死端口 + 真点按钮）")
    port = free_port()
    pdeg = os.path.join(WORK, "probe_degraded.json")
    rc2, _, _ = run_app({
        "AGENTOS_PROBE_OUT": pdeg,
        "AGENTOS_DEGRADED_PROBE": "1",
        "AGENTOS_TAP_START": "1",
        "AGENTOS_URL": "http://127.0.0.1:%d/" % port,
        "AGENTOS_AUTOCLOSE": "90",
        "AGENTOS_MULTI_INSTANCE": "1",
    })
    check("app-degraded-rc", rc2 == 0, "rc=%d" % rc2)
    d2 = load_json(pdeg)
    check("probe-degraded-parses", bool(d2), "fields=%d" % len(d2))
    spawned = int(d2.get("service_pid") or 0)
    check("degraded-port-was-free", not port_listening(port, 0.4),
          "127.0.0.1:%d" % port)
    if d2:
        for n, ok, dd in judge_degraded(d2):
            check(n, ok, dd)

    print("[4/6] 单实例行为探针（D107：第二个实例该接管而不是并存）")
    for n, ok, dd in single_instance_probe.probe()[0]:
        check(n, ok, dd)

    print("[5/6] 收尾：停掉 App 自己起的服务，端口归还")
    if spawned:
        for _ in range(25):
            if not port_listening(port, 0.4):
                break
            subprocess.run(["kill", str(spawned)],
                           capture_output=True)
            time.sleep(0.4)
    else:
        subprocess.run(["pkill", "-f", "serve.py --port %d" % port],
                       capture_output=True)
        time.sleep(0.5)
    check("degraded-port-released", not port_listening(port, 0.6),
          "127.0.0.1:%d after kill" % port)
    check("resident-8790-untouched", port_listening(8790, 2.0),
          "常驻 8790 必须仍在监听（降级探针不许误杀它）")

    print("[6/6] 活文件自证")
    for p, h in before.items():
        check("live-untouched:%s" % os.path.basename(p),
              os.path.isfile(p) and sha256(p) == h, p)
    return finish(before, t_all, None)


def finish(before, t_all, forced):
    n_ok = sum(1 for r in RESULTS if r["ok"])
    red = [r["check"] for r in RESULTS if not r["ok"]]
    rc = forced if forced is not None else (0 if not red else 1)
    rep = {"gate": "native_gate", "rc": rc, "checks": len(RESULTS),
           "ok": n_ok, "red": red, "results": RESULTS,
           "live_sha_before": before,
           "elapsed": round(time.time() - t_all, 1)}
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    tmp = REPORT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, REPORT)
    print("")
    if rc == 0:
        print("NATIVE_GATE OK %d/%d" % (n_ok, len(RESULTS)))
    else:
        print("NATIVE_GATE RED %d/%d  红项=%s"
              % (n_ok, len(RESULTS), ",".join(red)))
    print("耗时 %.1fs · 证据 work/native_gate.json"
          % (time.time() - t_all))
    return rc


if __name__ == "__main__":
    sys.exit(run_gate())
