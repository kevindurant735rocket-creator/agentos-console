#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D43 headroom 8787 契约门：把「误杀过一次」的死法变成可复验的门。

为什么单独一门（resident_gate 只查了第一层）：
resident_gate 的 `headroom-on-8787` 只判「8787 有监听且命令行含 headroom」，
这一条在下面三种真实死法下**照样绿**：
  1. 参数漂移：manifest 改了 port/backend/mode，进程还跑旧参数
     （与 D30「常驻实例跑旧代码」同一类病：门看的是活着，不是对的）；
  2. 无恢复路径：代理被误杀后没人拉起来 —— 这是**实测**结论，不是读 manifest
     的 supervisor_kind 声明。那个字段实测恒为 none，与实况不符：
     2026-10-03 实测代理的 PPID 链指向已 daemon 化（PPID=1）的
     `headroom.cli install agent run`，且实测把代理从 pid 71872 重生为 77738。
     所以本门走**进程树实测**守护者，一路到 init 都没人管才判红；
     hooks 的 ensure 也因此从「唯一恢复路径」降级为「第二道兜底」。
  3. 静默降级：实测 codex/claude 的 base_url 都指 cc-switch:15721，
     **没人走 8787**。所以 8787 死了不会有人喊疼 —— 越是这样越需要门，
     否则「哪天把开关切过去」的那天才会暴雷。

  4. 自发 unready（2026-10-03 重算 `~/.headroom/logs/proxy.log` 当日全部 readyz
     事件，原文）：当日 **418 个 503、20 段连片**，不是偶发。
     08:14 那段最完整：503 连片 08:14:07.618 -> 08:14:26.345 = **18.73s** ->
     `proxy_shutdown reason=signal pid=77738` 08:14:26.457 -> 间隔 **4.101s** ->
     `Proxy started 08:14:30.558` -> 首个 readyz 200 08:14:34.782
     （duration_ms=1373.89）；**首 503 -> 首 200 = 27.16s**。
     当日 shutdown/restart 共 5 次（03:31 pid 28692、04:01 pid 58907、
     07:37 pid 72573、07:46 pid 71872、08:14 pid 77738）；06:02 那段
     503 连片 29.23s **没有** shutdown 也会自愈 —— 抖动不依赖重生。
     按「503 连片 + 到下一个 200 的 gap」量，最坏一段 01:11:30 起
     （503 run 29.91s + gap 1.63s = 31.5s）。注意这是**当日某刻的快照**：
     日志还会继续长，门每次跑都按当天真日志**现场重算**并把数字打在
     `readyz-settle-covers-log` 那一行（09:12 那次跑出 31.8s），以门输出为准。
     同轮实测否掉了「压力大所以抖」：11 门跑批 239s/467 采样全 200、整轮 livecheck
     695 采样全 200、引擎侧对 8787 零引用、`install agent ensure` 是健康时空操作。
     所以这不是本项目负载，但它**落在 D50 必跑路径上**，D56 的 3 次 x 1.5s ≈ 3.3s
     预算必然整段落在窗口里 -> fail-closed 报假红。修法见 READY_SETTLE_S 上方注释：
     预算由**当天真日志量出来的最坏窗口**顶出，且**只在实测到守护者时给**。
     诚实边界：D58 首版写的「≈9.4s 窗口 / 恢复≈8s / 预算 30s」是拿单门自己几次
     探测量的，把窗口**低估了约 3 倍**；D58b 按全日日志重算修正，原 READY_SETTLE_S=30
     距最坏 31.5s 只差 1.5s 就已经会假红。

口径：正向 16 项 + 变异体 33 例，rc=0 才算绿。
纯判据抽出来做变异，是本门唯一的防「空壳」手段；实况探测只跑一遍系统。
其中 `readyz-settle-realpath` 与变异体 M29 会真起一个本地 HTTP server
（端口由内核分配，跑完即关）发真 503 —— 夹具只能证明逻辑接上了，
证明不了 urllib 真拿到 HTTPError、真 sleep、真在窗口过去后转 200。
D58b 的 `readyz-settle-covers-log` 读的是当天**真日志**，与上面两条互为对照：
一条证「夹具路径真跑得通」，一条证「预算没脱离真日志量出来的窗口」。
变异体 M30/M31/M32 盯的正是这条推导链：连片切断、gap 封顶、边界锁死。
"""
import io
import json
import os
import re
import shutil as _shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settle_budget as SB

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.expanduser("~")
HOOKS = os.path.join(HOME, ".codex", "hooks.json")
DEPLOY_DIR = os.path.join(HOME, ".headroom", "deploy")
CODEX_CFG = os.path.join(HOME, ".codex", "config.toml")
CLAUDE_CFG = os.path.join(HOME, ".claude", "settings.json")
HR_BIN = "/opt/miniconda3/bin/headroom"
DEFAULT_PORT = 8787
STALE_HOP = 8799          # ~/.headroom/sse_strip_proxy.py 的剥 SSE 前置层
RESULTS = []
FAIL = 0
MUT = []
PROFILE = ""


def chk(name, ok, detail):
    global FAIL
    RESULTS.append({"name": name, "ok": bool(ok), "detail": str(detail)[:220]})
    if not ok:
        FAIL += 1
    print(("  PASS " if ok else "  FAIL ") + name + " :: " + str(detail)[:150])


def mchk(name, ok, detail=""):
    MUT.append({"name": name, "ok": bool(ok), "detail": str(detail)[:120]})
    print(("  PASS " if ok else "  FAIL ") + "[mut] " + name
          + (" :: " + str(detail)[:90] if detail else ""))


def sh(*cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


# ── 纯判据（变异测试的对象；不得读系统） ────────────────────────────────
def parse_argv_params(argv):
    """解析 `proxy --host H --port P --mode M --backend B --telemetry`。"""
    out = {"host": None, "port": None, "mode": None, "backend": None,
           "telemetry": False}
    toks = (argv or "").split()
    for i, t in enumerate(toks):
        if t in ("--host", "--port", "--mode", "--backend") and i + 1 < len(toks):
            key = t[2:]
            val = toks[i + 1]
            out[key] = int(val) if key == "port" and val.isdigit() else val
        elif t == "--telemetry":
            out["telemetry"] = True
    return out


def params_match(mp, ap):
    """manifest 声明的 5 个参数 vs 活进程命令行。任一不等即漂移。"""
    diffs = []
    for k in ("host", "port", "mode", "backend"):
        if mp.get(k) != ap.get(k):
            diffs.append("%s 声明=%s 实跑=%s" % (k, mp.get(k), ap.get(k)))
    if bool(mp.get("telemetry")) != bool(ap.get("telemetry")):
        diffs.append("telemetry 声明=%s 实跑=%s"
                     % (bool(mp.get("telemetry")), bool(ap.get("telemetry"))))
    if not diffs:
        return True, "一致 " + json.dumps(ap, ensure_ascii=False)
    return False, "; ".join(diffs)


def upstream_ports_ok(ports, listening):
    """客户端真实上游里凡指向本机的，每个都得真在监听。"""
    dead = sorted(p for p in ports if not listening.get(p))
    if dead:
        return False, "上游在监听=缺 %s" % dead
    return True, "本机上游 %s 全在监听" % sorted(ports)


def hook_profile_ok(hooks, profile):
    """恢复路径：PreToolUse 与 SessionStart 都得注册同 profile 的
    `headroom init hook ensure`。少一条 = 误杀后只有一半入口能自愈。"""
    found = {}
    for evt, group in ((hooks or {}).get("hooks") or {}).items():
        n = 0
        for entry in group or []:
            for h in (entry or {}).get("hooks") or []:
                cmd = str(h.get("command", ""))
                if "headroom" in cmd and "ensure" in cmd:
                    m = re.search(r"--profile\s+(\S+)", cmd)
                    if m and m.group(1) == profile:
                        n += 1
        found[evt] = n
    need = ("PreToolUse", "SessionStart")
    miss = [e for e in need if not found.get(e)]
    if miss:
        return False, "缺注册入口 %s 实况=%s" % (miss, found)
    return True, "注册齐 %s" % {e: found.get(e, 0) for e in need}


def stale_hop_ok(live_text, listening):
    """8799 是上一代剥 SSE 前置层。没人引用时它死了无害（不判红），
    一旦有活配置指着它就必须真在监听。"""
    if str(STALE_HOP) not in str(live_text or ""):
        return True, "活配置未引用 %d（残留层，死了无害）" % STALE_HOP
    return bool(listening.get(STALE_HOP)), \
        "活配置引用 %d，监听=%s" % (STALE_HOP, bool(listening.get(STALE_HOP)))


def autostart_verdict(kind, load_bearing_on):
    """三态自启 x 关键路径。判据只认「真自启」，别名/日志 tail 不算通道。

    返回 (ok, why)：
      autostart + 任意        -> 绿（重启后自己回来）
      shell-unsupervised + 非关键 -> 绿，但属潜伏风险（有人引用就炸）
      shell-unsupervised + 关键   -> 红
      none + 关键             -> 红（重启后静默失效）
      none + 非关键           -> 绿（当前实况：没人用 8787）
    """
    if kind == "autostart":
        return True, "真自启，重启后自己回来"
    if not load_bearing_on:
        return True, "无自启但实测非关键路径，风险潜伏（当前实况）"
    return False, "既无自启又是关键路径 -> 重启后静默失效"


def launchd_channel_ok(plist_on_disk, label_loaded):
    """8787 的开机通道要「plist 真在盘」且「launchd 真认领」两件都在才算数。

    为什么单开一道（D70，2026-10-04 实测逼出来的）：8787 死掉时 live-listen
    会红，但「通道被删 / 被 unload 而进程还活着」是另一种死法，现有判据
    全绿 —— 实测 09:48 机器重启后 8787 死了 9.5h，没有任何开机通道拉它
    回来（只有 hook 兜底，且 hook 只在 tool call 时才跑）。这类回归只有
    「通道在不在」这一层能看见，所以单独钉住，不靠 8787 恰好死没死来间接推断。
    """
    if not plist_on_disk:
        return False, "plist 不在盘 = 没有开机通道"
    if not label_loaded:
        return False, "plist 在盘但 launchd 未认领 = load 过就再没人管"
    return True, "plist 在盘且 launchd 已认领"


# 只起进程、不带守护的写法（fallback 里就是这种）
_SHELL_START_RE = re.compile(
    r"(headroom\s+(proxy|install\s+agent|agent\s+run|start|up)\b|launchctl\s+load)")
# 只是提到 headroom 但不启动 —— 别名 / tail 日志 / curl 统计，都不算自启通道
_SHELL_MENTION_RE = re.compile(r"^\s*(alias\b|#|echo\b|curl\b|tail\b)")
# D57/M24 教训：`#` 是非单词字符，`#\b` 在「# 空格…」上永不成立（无边界），
# 导致注释行 `…` 漏过白名单、被 _SHELL_START_RE 误判成真启动动作。
# 所以只有单词前缀才带 \b，`#` 分支不带。


def classify_shell_autostart(text):
    """shell profile 里到底有没有「真启动动作」，返回 (has_start, supervised, refs_plist)。

    实测教训（2026-10-03）：.zshrc 既有 `headroom-logs='tail -f ...'` 这种
    纯别名，也有 `launchctl load ~/Library/LaunchAgents/com.headroom.proxy.plist
    || headroom proxy ... &` 这种真启动。只 grep 关键词会把别名误判成通道。
    """
    has_start = False
    supervised = False
    refs_plist = False
    for line in (text or "").splitlines():
        if _SHELL_MENTION_RE.match(line):
            continue
        if _SHELL_START_RE.search(line):
            has_start = True
            if "install agent" in line or "agent run" in line or "launchctl load" in line:
                supervised = True
            if ".plist" in line:
                refs_plist = True
    return has_start, supervised, refs_plist


def measure_autostart(port=DEFAULT_PORT):
    """实测这台机器上 8787 到底有没有开机自启通道，返回 (kind, detail)。

    kind ∈ {autostart, shell-unsupervised, none}。查不到的一律写「未知」，
    不当成「有」也不当成「无」。
    """
    found = []
    rc, out = sh("launchctl", "list")
    if any("headroom" in l.lower() for l in out.splitlines()):
        found.append("launchd")
    for d in (os.path.join(HOME, "Library", "LaunchAgents"),
              "/Library/LaunchAgents", "/Library/LaunchDaemons"):
        if os.path.isdir(d) and any("headroom" in f.lower() for f in os.listdir(d)):
            found.append(d)
    # shell profile：要求「真启动动作」，且引用的 plist 必须真在盘上
    for f in (".zshrc", ".zprofile", ".bash_profile", ".profile"):
        fp = os.path.join(HOME, f)
        try:
            if not os.path.exists(fp):
                continue
            txt = open(fp, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        has_start, supervised, refs_plist = classify_shell_autostart(txt)
        if not has_start:
            continue
        # 引用了 plist 但盘上没有（实测：.zshrc:268 引 com.headroom.proxy.plist，
        # 该文件不存在）-> launchctl load 必然失败，实际只有下面那行裸
        # `headroom proxy … &` 兜底，没有守护，所以归 shell-unsupervised。
        if supervised and not refs_plist:
            found.append("shell:" + f)
        elif supervised and refs_plist and any("LaunchAgents" in x for x in found):
            found.append("shell+plist:" + f)
        else:
            found.append("shell-unsupervised:" + f)
    li = "未知"
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get the name of every login item'],
            capture_output=True, text=True, timeout=8)
        if r.returncode == 0:
            li = "有" if "headroom" in (r.stdout or "").lower() else "无"
    except Exception as e:
        li = "未知(%s)" % type(e).__name__
    real = list(found)
    kind = ("autostart" if any(not x.startswith("shell-unsupervised") for x in real)
            else "shell-unsupervised") if real else "none"
    return kind, "通道=%s · loginitem=%s" % (real or "无", li)


def load_bearing(ports, hr_port=DEFAULT_PORT):
    """客户端是否真走 headroom。走 = 8787 一死就真会影响用户，必须盯紧。"""
    return hr_port in set(ports)


# --- D56：活体 readyz 探测的传输层重试 -------------------------------------
# 为什么加（2026-10-03 实测）：D50 连跑两轮，第二轮本门红在
#   readyz-200 HTTP=503 + install-status Healthy=no
# 而同一时刻单独跑本门 12/12 全绿、curl readyz=200。根因是单发 urlopen
# 在整机负载下（livecheck 同时拉起 11 门 + 3 门真浏览器走查）会撞上代理的
# 瞬时窗口，于是 D50 的 fail-closed 把一次抖动放大成整轮假红。
#
# 铁律（D40，与 tools/flake_retry.py 同源但不共用）：**只重试传输层异常**。
# HTTP 5xx / 连接被拒都走 urllib 的异常路径，落在重试范围内；
# 一旦真的拿到响应，判据 `code == 200` 仍然**只判一次**，绝不因为多试了几次
# 就把 400/403 这类确定性失败判绿。所以重试只可能把「瞬时红」变成绿，
# 不可能把「代理真的死了」变成绿 —— 变异体 M16 专门锁死这一条。
READY_ATTEMPTS = int(os.environ.get("CD_READY_ATTEMPTS", "3"))
READY_SLEEP_S = float(os.environ.get("CD_READY_SLEEP", "1.5"))

# --- D58：settle 窗口按「实测窗口 x 余量」给，且只在实测到守护者时才给 ------
# D58b 重算依据（2026-10-03，`~/.headroom/logs/proxy.log` 当日 readyz 全量事件，
# 不是抽样几笔的观测；口径写在 readyz_events / unready_windows docstring 里）：
#   当日 418 个 503 / 20 段连片 —— 「偶发」这个说法被数据否掉了。
#   最坏一段 01:11:30 起（503 连片 29.91s + 到下个 200 的 gap 1.63s = 31.5s）。
#   这是**当日某刻快照**，日志会继续长 —— 所以预算不是手写常数，而是每次跑门
#   现场重算 + SETTLE_MARGIN_S 顶出，实测值打在 `readyz-settle-covers-log` 行。
#   08:14 段（唯一能把完整闭环读全的一段）：
#     503 连片 08:14:07.618 -> 08:14:26.345  = 18.73s
#     proxy_shutdown reason=signal pid=77738  = 08:14:26.457
#     Headroom Proxy started                  = 08:14:30.558（间隔 4.101s）
#     首个 readyz 200                         = 08:14:34.782（duration_ms=1373.89）
#     首 503 -> 首 200                        = 27.16s
#   06:02 段 503 连片 29.23s、**无** shutdown，仍自愈 -> 抖动不是重生副作用。
#   shutdown/restart 当日 5 次：03:31 pid 28692、04:01 pid 58907、07:37 pid 72573、
#     07:46 pid 71872、08:14 pid 77738。
# D58 首版按单门自己几次探测写成「≈9.4s 窗口 / 恢复≈8s / 预算 30s」，
# 低估约 3 倍；按上面的 31.5s 顶，预算 30s **差 1.5s 就假红**。所以：
#   · 预算不再手写，改由当天真日志量出最坏窗口再 + SETTLE_MARGIN_S 顶出；
#   · 正向检查 `readyz-settle-covers-log` 直接判「预算 >= 实测最坏 + 余量」，
#     真日志无当日事件时显式 [SKIP] 退回静态地板 36.0s，**不许静默绿**。
# D56 的 3 次 x 1.5s ≈ 3.3s 预算必然整段落在窗口里 -> D50 fail-closed 报假红。
#
# 但实测同时否掉了「压力大所以抖」这个解释（不能猜，只能量）：
#   · 11 门跑批 239s / 并发 0.5s 轮询 467 采样 -> 全程 200，0 坏窗口
#   · 整轮 livecheck 695 采样 -> 全程 200，0 坏窗口，PID 单一 95261
#   · launchd.err.log mtime 停在 Aug 17，2026-10-03 行数 = 0（历史留证，非现场）
#   · company-delivery 全树 grep "8787"/"headroom" -> 引擎侧零引用，端口冲突排除
#   · `install agent ensure` 实测是**健康时的空操作**（rc=0 "already healthy"，
#     PID 全程不变，0 坏窗口）-> 元凶排除
# 结论：这是代理自己的周期性抖动，与本项目负载无关，但**落在 D50 必跑的路径上**。
#
# 为什么预算只给「实测到守护者」的：M20 已实测「链到 init = 无人管」，
# 那种情况下等再久也不会自己好，等待只是把红推迟成更慢的红，不改变判定。
PROXY_LOG = os.path.expanduser("~/.headroom/logs/proxy.log")
PROXY_LOG_DIR = os.path.dirname(PROXY_LOG)
TS_FMT = "%Y-%m-%d %H:%M:%S,%f"
# 「没人探活的那段时间」不算 unready 窗口：代理可能早就好了，只是没人问。
# 实测今日 gap 双峰 —— 活跃探活时 1.4~9s，长空窗 20~154s。按 153s 取预算
# 会把「稀疏」误读成「慢自愈」，所以给一个物理上限把空窗掐掉。
PROBE_GAP_CAP_S = 10.0
# 余量 = 实测重生间隔 4.1s + 首个 readyz 200 耗时 1.4s，取整 6s
SETTLE_MARGIN_S = 6.0
# 生命周期锚点的贴邻容差：503 连片必须**紧邻** shutdown/start 才算「重生窗口」，
# 否则就是网络出口抖动（口径与实测依据见 unready_spans 的 docstring）。
RESTART_ADJACENT_S = 15.0
# 2026-10-03 10:5x 实测：当日真日志最坏 unready 窗口已从 D58 记录的 31.5s 涨到
# 39.4s（6084 事件 / 24 段），39.4 + 余量 6 = 45.4s > 原默认 45s，门自己报红并
# 写明「需 45.4s」。这里把默认抬到 60s —— 判据（预算 >= 实测最坏 + 余量）一个字没动，
# 动的只是**预算常数**，且按门给出的实测值顶，不是拍脑袋放大。
# 注意：日志会继续长，最坏窗口再涨到 54s 以上时本门会再次如实报红，那时要重新量。
# D59（2026-10-03 18:0x 实测）修掉**两个**真缺陷，预算常数**一个字没动**：
# ① 假绿（fail-open）：`readyz_events` 只读 live `proxy.log`，而日志按 10MB 轮转。
#    实测 17:53 跑出 rc=1（最坏 286.9s），17:54 轮转后同日跑出 rc=0（0 段 /
#    0.0s）—— 历史搬进 `.1` 就看不见，可见性决定结论。现在读全部 `proxy.log*`
#    并按时间排序（M33/M34 锁死）。
# ② 用错药（判据混了两类窗口）：当日 35 段 503 连片里只有 5 段是生命周期窗口
#    （03:31 21.1s / 04:01 20.2s / 07:37 21.2s / 07:46 27.8s / 08:14 27.2s），
#    其余 30 段是 `_check_upstream()` 对 `https://api.anthropic.com` 的 HEAD
#    探活连续失败（`_UPSTREAM_CHECK_TTL=30s` 缓存 + 5s 超时，每个 503 都带
#    `duration_ms=0.31~44.75` 秒回）—— **网络出口问题，等再久也不会被 settle
#    预算治好**。最坏非生命周期段 286.9s（17:02:24 -> 17:07:01，27 个 503，
#    无 shutdown，17:07:17 livez 200）。不分类的话预算得从 60s 顶到 292.9s，
#    而那个数对网络抖动毫无意义。现在 `unready_spans` 按 start 锚点贴邻分类
#    （M35/M36/M37），实测最坏生命周期窗口 **27.8s** + 余量 6 = 33.8s < 60s。
READY_SETTLE_S = float(os.environ.get("CD_READY_SETTLE", "60"))


def _ps_argv_of(pid):
    rc, out = sh("ps", "-o", "ppid=", "-o", "args=", "-p", str(pid))
    parts = out.strip().split(None, 1)
    ppid = int(parts[0]) if parts and parts[0].isdigit() else 0
    return ppid, (parts[1] if len(parts) > 1 else "")


def measure_supervisor(pid, argv_of=None, depth=6):
    """沿 PPID 链**实测**守护者是谁，不读 manifest 的声明。

    返回 (kind, detail)，kind ∈ {headroom-agent, launchd, none}。
    `none` = 从代理一路走到 init 都没人管 —— 这才是「误杀后没人拉起来」的真死法。
    2026-10-03 实测：manifest 声明 supervisor_kind=none（自报），
    但实测 kind=headroom-agent（实测），两者不一致正是本门要暴露的东西。
    """
    argv_of = argv_of or _ps_argv_of
    start = int(pid or 0)
    if start <= 1:
        return "none", "pid=%s 无进程可查" % start
    ppid, _ = argv_of(start)
    cur, seen = int(ppid or 0), [start]
    for _ in range(depth):
        if cur <= 1:
            break
        ppid, argv = argv_of(cur)
        argv = (argv or "").strip()
        seen.append(cur)
        if "install agent run" in argv and "headroom" in argv:
            return "headroom-agent", "祖先 pid=%d argv=%s" % (cur, argv[:64])
        if "launchd" in argv:
            return "launchd", "祖先 pid=%d argv=%s" % (cur, argv[:64])
        if ppid <= 1:
            break
        cur = ppid
    return "none", "链=%s 一路到 init 无人管" % "->".join(str(x) for x in seen)


def _real_readyz_fetch(url):
    with urllib.request.urlopen(url, timeout=6) as r:
        return r.status, r.read()[:60].decode("utf-8", "replace")


def probe_readyz(port, attempts=None, sleep_s=None, fetch=None, pid_probe=None,
                 settle_s=None):
    """探测 readyz。只重试传输层异常，返回 (code, body, detail)。

    detail 带尝试痕迹（`attempt 1/3 HTTP=503 …`），重试过就在门输出里留证，
    免得「重试把红洗成绿」变成看不见的黑洞。

    pid_probe 给了就顺带记 PID：守护者在探测窗口里把代理重生时，痕迹里会出现
    `pid 71872->77738`，下一次真红时不用再从头猜是不是抖动。
    """
    n = READY_ATTEMPTS if attempts is None else int(attempts)
    nap = READY_SLEEP_S if sleep_s is None else float(sleep_s)
    # D58：settle 预算反推总次数，trace 的 `attempt i/n` 格式保持不变，
    # 所以 M15 的 `attempt 2/3` 断言不受影响（它不传 settle_s，n 仍=3）。
    if settle_s is not None and float(settle_s) > 0:
        # 上限 60：nap=0 的测试夹具不该把次数除成天文数字，
        # 而生产 nap=1.5/settle=45 -> int(30)+1=31，覆盖当天最坏窗口（现场量，约 31.5s+）。
        n = max(n, min(60, int(float(settle_s) / max(nap, 0.5)) + 1))
    fetch = fetch or _real_readyz_fetch
    url = "http://127.0.0.1:%d/readyz" % port
    trace = []
    code, body = 0, ""
    pid_first = pid_probe() if pid_probe else 0
    for i in range(1, n + 1):
        try:
            code, body = fetch(url)
            trace.append("attempt %d/%d HTTP=%s" % (i, n, code))
            break            # 拿到响应即停：判据只判这一次（D40）
        except Exception as e:
            body = str(e)[:80]
            trace.append("attempt %d/%d %s" % (i, n, type(e).__name__))
            if i < n:
                time.sleep(nap)
    if pid_probe:
        pid_last = pid_probe()
        trace.append("pid %s->%s%s" % (pid_first, pid_last,
                                       "（守护者重生过）" if pid_first and
                                       pid_last and pid_first != pid_last else ""))
    return code, body, " ".join(trace)


def real_503_settle_probe(window_s=4.5, budget=None):
    """真 socket 真 HTTP 503 窗口 -> 验 settle 预算真能盖住（D58）。

    为什么必须真起一个 server：M15/M26 用的是 `mk_fetch` 夹具，能证明
    「重试逻辑接上了」，但证明不了「urllib 真拿到 HTTPError 503、真 sleep、
    真在窗口过去后转 200」。这条走完整传输层，且**端口由内核分配**
    （bind 到 0），避免固定端口撞车。跑完必关，不留监听。
    """
    import http.server
    import socketserver
    import threading

    t0 = [time.time()]

    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                                  # noqa: N802
            if time.time() - t0[0] < window_s:
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            b = b'{"ready":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *a):
            return

    class _S(socketserver.TCPServer):
        allow_reuse_address = True

    srv = _S(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        time.sleep(0.3)
        t0[0] = time.time()
        b = READY_SETTLE_S if budget is None else float(budget)
        code, body, trace = probe_readyz(port, settle_s=b)
        el = time.time() - t0[0]
        return code, trace, el, window_s
    finally:
        srv.shutdown()
        srv.server_close()


def settle_budget(sup_kind, budget=None):
    """守护者实测结果 -> settle 等待预算（秒）。D58。

    抽成独立函数是因为「等待预算怎么来」本身就是判据，必须能被变异体戳：
    无人守护时给 0（等再久也不会自愈，只把红推迟成更慢的红）；
    实测到 headroom-agent / launchd 时才给预算。
    """
    b = READY_SETTLE_S if budget is None else float(budget)
    return 0.0 if sup_kind == "none" else float(b)


def recovered_after_retry(trace):
    """trace 里 attempt ≥2 次且最终 HTTP=200 -> 判「抖动后自愈」。D58。

    单发就 200（只有 1 条 attempt）必须判 False —— 否则「本来就好」会被
    记成「自愈」，自愈计数就成了永远为真的安慰话。变异体 M28 锁这一条。
    """
    t = trace or ""
    return t.count("attempt ") >= 2 and "HTTP=200" in t


def _paths_arg(paths):
    """None -> 全部轮转文件；str -> 单文件；list -> 按给定顺序。"""
    if paths is None:
        return proxy_log_paths()
    if isinstance(paths, str):
        return [paths]
    return list(paths)


def readyz_events(path=None, day_prefix=None):
    """从真实 proxy.log 解析 readyz 事件序列 [(epoch秒, 是否503), ...]。

    只认 `path=/readyz` 的 inbound_response —— 请求行没有 status，
    混进来会把「谁在问」当成「答了什么」。
    `day_prefix` 限定日期前缀（实测窗口要按天算，跨天连片没有物理意义）。
    抽成独立函数是因为「日志长什么样」也必须能被变异体戳：
    解析器一改宽/一改窄，实测最坏窗口跟着漂，预算就失去依据。

    D59：读**全部**轮转文件。真实现测 `~/.headroom/logs/proxy.log` 按 10MB 轮转
    （`proxy.log` + `.1`…`.5`），当日历史在 17:54 轮转后整段搬进 `proxy.log.1`。
    只读 live 单文件的后果是**实测的假绿**：同一台机、同一天、同一份日志，
    17:53 跑出 rc=1（最坏 286.9s，真红），17:54 轮转后跑出 rc=0（69 事件 /
    0 段 / 最坏 0.0s）—— 只因文件转了一圈，历史不可见就变绿。
    显式传 `path` 仍是单文件（变异体夹具要的就是确定性）。
    """
    return _read_readyz(_paths_arg(path), day_prefix)


def _iter_log_lines(fp, day_prefix, needles=("path=/readyz", "inbound_response"),
                    any_needles=()):
    if not os.path.isfile(fp):
        return
    with io.open(fp, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if any(n not in line for n in needles):
                continue
            if any_needles and not any(n in line for n in any_needles):
                continue
            if day_prefix and not line.startswith(day_prefix):
                continue
            yield line


def _read_readyz(paths, day_prefix):
    out = []
    import datetime as _dt
    for fp in paths:
        for line in _iter_log_lines(fp, day_prefix):
            mm = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+).*status=(\d+)", line)
            if not mm:
                continue
            try:
                t = _dt.datetime.strptime(mm.group(1), "%Y-%m-%d %H:%M:%S,%f")
            except Exception:
                continue
            out.append((t.timestamp(), mm.group(2) == "503"))
    out.sort(key=lambda x: x[0])
    return out


def proxy_log_paths():
    """Rotated headroom proxy logs, chronological order (oldest -> newest)."""
    if not os.path.isdir(PROXY_LOG_DIR):
        return [PROXY_LOG]
    rot = []
    for name in os.listdir(PROXY_LOG_DIR):
        m = re.match(r"^proxy\.log\.(\d+)$", name)
        if m:
            rot.append((int(m.group(1)), name))
    rot.sort(reverse=True)
    return [os.path.join(PROXY_LOG_DIR, n) for _, n in rot] + [PROXY_LOG]
def restart_marks(path=None, day_prefix=None):
    """Today (epoch, kind) marks; kind is start|shutdown. D59."""
    import datetime as _dt
    out = []
    for fp in _paths_arg(path):
        for line in _iter_log_lines(fp, day_prefix,
                                    needles=(),
                                    any_needles=("Headroom Proxy started",
                                                 "event=proxy_shutdown")):
            kind = "start" if "Headroom Proxy started" in line else "shutdown"
            mm = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+)", line)
            if not mm:
                continue
            try:
                t = _dt.datetime.strptime(mm.group(1), TS_FMT)
            except Exception:
                continue
            out.append((t.timestamp(), kind))
    out.sort(key=lambda x: x[0])
    return out
def unready_spans(events, marks=None, gap_cap=None, adjacent=RESTART_ADJACENT_S):
    """503 runs as (duration, is_lifecycle) pairs. D59 classification.

    lifecycle = the unready state BEGAN at a restart: a start mark within
    `adjacent` of the run's first 503 (the documented "adjacent to a
    start/shutdown mark"), and the run's beginning was observed (the event
    before t0 is within gap_cap -- a run that starts after an unprobed gap
    began at an unknown time; the egress failure it suffers may well have
    started during the gap, so pinning it on a restart mark that only shows
    up after probing resumed would be a guess, and the first event of the
    merged list counts as observed -- conservative, it counts against the
    budget rather than exempting).
    non-lifecycle = process alive, upstream HEAD probe failing (network
    exit); waiting longer never fixes it, so it must not inflate the budget.
    Refined 2026-10-09 against a real all-day egress outage: the serving
    process (pid 61263, up since 10-05) never restarted -- every one of
    the day's 14 "Headroom Proxy started" lines was a supervisor child that
    failed to bind 8787 (runner.log: 413x errno 48) and was killed seconds
    later -- yet the old "any start mark inside the span" rule counted the
    multi-hour 503 runs as settle windows. The old rule also misread the
    docstring: "adjacent" implemented as "contained". Both fixes keep the
    M35/M36/M37 fixtures' verdicts (M35's mark sits 3s after a first-event
    t0; M36/M37's marks are minutes away or absent) and keep real slow
    settles red: 503s observed to begin at a restart and lasting past the
    budget still count, gap or no gap.
    """
    cap = PROBE_GAP_CAP_S if gap_cap is None else float(gap_cap)
    mk = restart_marks() if marks is None else list(marks)
    spans, i, n = [], 0, len(events)
    while i < n:
        if not events[i][1]:
            i += 1
            continue
        t0 = events[i][0]
        j = i
        while j + 1 < n and events[j + 1][1]:
            j += 1
        gap = min(cap, (events[j + 1][0] - events[j][0]) if j + 1 < n else cap)
        t1 = events[j][0] + gap
        observed = (i == 0) or (t0 - events[i - 1][0]) <= cap
        life = observed and any(
            t0 - float(adjacent) <= m[0] <= t0 + float(adjacent)
            for m in mk if m[1] == "start")
        spans.append((max(0.0, t1 - t0), life))
        i = j + 1
    return spans
def unready_windows(events, gap_cap=None):
    """把 readyz 事件序列压成「每段 unready 的可观测时长」（纯判据，可变异）。

    口径（两条都是实测逼出来的）：
      · 起点 = 该段第一个 503；终点 = **之后第一个 ready**；
      · 但若那个 ready 隔太久才来（`gap_cap`，默认 PROBE_GAP_CAP_S），
        说明中间根本没人探活，这段时间**不能算进窗口** ——
        实测今日 gap 双峰：活跃探活 1.4~9s、长空窗 20~154s。
        不封顶就会把「稀疏」读成「慢自愈」，预算被顶到 170s 这种废数。
    收尾没有 ready（代理到现在还没好）就按 gap_cap 封顶，理由同上：
    只能证明「最后那个 503 时刻仍不可用」，不能证明它一直不可用到写日志。
    """
    cap = PROBE_GAP_CAP_S if gap_cap is None else float(gap_cap)
    wins, i, n = [], 0, len(events)
    while i < n:
        if not events[i][1]:
            i += 1
            continue
        t0 = events[i][0]
        j = i
        while j + 1 < n and events[j + 1][1]:
            j += 1
        gap = min(cap, (events[j + 1][0] - events[j][0]) if j + 1 < n else cap)
        wins.append(max(0.0, (events[j][0] - t0) + gap))
        i = j + 1
    return wins


def settle_covers(worst_s, budget=None, margin=SETTLE_MARGIN_S):
    """settle 预算必须 >= 实测最坏可观测窗口 + 重生余量。D58b。

    这一条是本轮真正的牙：预算不再是「拍一个够大的数」，
    而是每次跑门都对着当天的真日志重算最坏窗口，够不到就报红并说清该调到多少。
    """
    b = READY_SETTLE_S if budget is None else float(budget)
    need = float(worst_s) + float(margin)
    return (b >= need), ("预算 %.0fs vs 实测最坏 %.1fs + 余量 %.0fs = 需 %.1fs"
                         % (b, float(worst_s), float(margin), need))


def mutants():
    print("== 变异体自捕（D43 防空壳）")
    ap = {"host": "127.0.0.1", "port": 8787, "mode": "token",
          "backend": "anthropic", "telemetry": True}
    mp = dict(ap)
    # 注意：纯判据一律返回 (ok, detail) 二元组，取反必须下标 [0]。
    # 反面教材（本门 v1 实测）：写 `not params_match(...)` → not 作用在非空
    # 二元组上恒为 False → 8 个变异体全「漏网」。二元组当返回值就得按位取。
    mchk("M1 params_match 原样必须绿", *params_match(mp, ap))
    mchk("M2 params_match 抓 port 漂移",
         not params_match(dict(mp, port=9999), ap)[0], "port 9999 vs 8787")
    mchk("M3 params_match 抓 backend 漂移",
         not params_match(dict(mp, backend="openai"), ap)[0], "openai vs anthropic")
    mchk("M4 params_match 抓 telemetry 漂移",
         not params_match(dict(mp, telemetry=False), ap)[0], "关 vs 开")
    mchk("M5 params_match 抓缺失参数（--port 没传）",
         not params_match(mp, dict(ap, port=None))[0], "port=None")

    mchk("M6 upstream_ports_ok 原样必须绿",
         *upstream_ports_ok([15721], {15721: True}))
    mchk("M7 upstream_ports_ok 抓死上游",
         not upstream_ports_ok([15721], {15721: False})[0], "15721 未监听")
    mchk("M8 upstream_ports_ok 空集合判绿（无本机上游不误红）",
         *upstream_ports_ok([], {}))

    _cmd = {"command": "/opt/miniconda3/bin/headroom init hook ensure --profile P1"}
    good = {"hooks": {"PreToolUse": [{"hooks": [_cmd]}],
                      "SessionStart": [{"hooks": [dict(_cmd)]}]}}
    mchk("M9 hook_profile_ok 原样必须绿", *hook_profile_ok(good, "P1"))
    mchk("M10 hook_profile_ok 抓缺 SessionStart",
         not hook_profile_ok({"hooks": {"PreToolUse": [{"hooks": [_cmd]}]}}, "P1")[0])
    mchk("M11 hook_profile_ok 抓 profile 写错",
         not hook_profile_ok(good, "P2")[0])

    mchk("M12 stale_hop_ok 无人引用判绿",
         *stale_hop_ok("base_url=http://x/v1", {}))
    mchk("M13 stale_hop_ok 被引用但没监听必须红",
         not stale_hop_ok("upstream=http://127.0.0.1:8799", {})[0])
    mchk("M14 load_bearing 抓「以为在用其实没走」",
         (not load_bearing([15721])) and load_bearing([8790, 8787]))
    # D56：重试语义的三个变异体，锁死「重试只洗瞬时红、不造假绿」。
    class _Boom(Exception):
        pass

    def mk_fetch(script):
        """script=[200|Exception,...]，逐次返回/抛出，耗尽则重复最后一个。"""
        it = list(script)

        def f(_url):
            v = it.pop(0) if len(it) > 1 else it[0]
            if isinstance(v, Exception):
                raise v
            return v[0], v[1]
        return f

    tr = probe_readyz(8787, attempts=3, sleep_s=0, fetch=mk_fetch(
        [_Boom("HTTP Error 503"), (200, b'{"ready":true}')]))
    mchk("M15 传输层瞬时失败重试后判绿（503 -> 200）",
         tr[0] == 200 and "attempt 2/3" in tr[2], "code=%s trace=%s" % (tr[0], tr[2]))
    dead = probe_readyz(8787, attempts=3, sleep_s=0,
                        fetch=mk_fetch([_Boom("HTTP Error 503")]))
    mchk("M16 代理真死时重试不得造出假绿（3 次全败仍判红）",
         dead[0] != 200, "code=%s" % dead[0])
    no_retry = probe_readyz(8787, attempts=1, sleep_s=0,
                            fetch=mk_fetch([_Boom("HTTP Error 503")]))
    mchk("M17 去掉重试（attempts=1）则瞬时失败判红（M15 的绿确来自重试）",
         no_retry[0] != 200, "code=%s" % no_retry[0])

    # D57：守护者实测三态。判据必须能分辨，且「一路到 init 无人管」必须判得出 none。
    _tree = {
        100: (99, "/opt/miniconda3/bin/python3.13 -m headroom.cli proxy --port 8787"),
        99: (1, "/opt/miniconda3/bin/python3.13 -m headroom.cli install agent run "
                "--profile init-zhangfengrui-3ba60337"),
    }
    mchk("M18 实测守护者=headroom-agent（agent 祖先）",
         measure_supervisor(100, lambda q: _tree[q])[0] == "headroom-agent",
         measure_supervisor(100, lambda q: _tree[q])[1][:70])
    _tree_ld = {
        100: (99, "-m headroom.cli proxy --port 8787"),
        99: (77, "/bin/launchd"),
    }
    mchk("M19 实测守护者=launchd（launchd 祖先，与 M18 可分辨）",
         measure_supervisor(100, lambda q: _tree_ld[q])[0] == "launchd",
         measure_supervisor(100, lambda q: _tree_ld[q])[1][:70])
    _tree_none = {100: (1, "-m headroom.cli proxy --port 8787")}
    k_none, d_none = measure_supervisor(100, lambda q: _tree_none[q])
    mchk("M20 误杀后无人拉起来：链到 init 判 none（supervisor-measured 会红）",
         k_none == "none", d_none[:70])

    # D57：开机自启 x 关键路径，只红「既无真自启又关键」那一格。
    mchk("M21 真自启 + 关键路径 -> 绿",
         autostart_verdict("autostart", True)[0], "autostart 关键=True")
    mchk("M22 无自启 + 关键路径 -> 红（重启后静默失效的真风险）",
         not autostart_verdict("none", True)[0], "none 关键=True")
    mchk("M23 无自启 + 非关键 -> 绿（当前实况，不为没人用的端口误红）",
         autostart_verdict("none", False)[0], "none 关键=False")
    mchk("M23b 无守护的 shell fallback + 关键路径 -> 红",
         not autostart_verdict("shell-unsupervised", True)[0],
         "shell-unsupervised 关键=True")

    # D57b：只 grep 关键词会把别名误判成自启通道（本轮实测真踩到）
    _alias_only = ("alias headroom-logs='tail -f /tmp/headroom-proxy.log'\n"
                   "alias headroom-stats='curl -s http://127.0.0.1:8787/stats'\n"
                   "# headroom proxy 的说明注释\n")
    mchk("M24 只有别名/tail/注释提到 headroom -> 不算自启通道（本轮实测的真 bug）",
         classify_shell_autostart(_alias_only)[0] is False,
         "别名文件 has_start=%s" % classify_shell_autostart(_alias_only)[0])
    _real_start = ("if curl -s http://127.0.0.1:8787/livez; then\n"
                   "  :\n"
                   "else\n"
                   "  launchctl load ~/Library/LaunchAgents/com.headroom.proxy.plist || \\\n"
                   "  headroom proxy --port 8787 &\n"
                   "fi\n")
    mchk("M25 launchctl load + 裸 proxy fallback -> 认出是启动动作",
         classify_shell_autostart(_real_start)[0] is True
         and classify_shell_autostart(_real_start)[2] is True,
         "has_start=True refs_plist=True")

    # D58：settle 窗口的三条牙。核心是「等待不得造出假绿」。
    _flaky = [_Boom("HTTP Error 503"), (200, b'{"ready":true}')]
    # 有守护者 -> 允许在 settle 预算内等到 200（此处 9.4s / 30s 是**夹具参数**，
    # 生产真口径是当天日志量出的最坏窗口，见 READY_SETTLE_S 上方注释）
    s_supp = probe_readyz(8787, attempts=3, sleep_s=0, settle_s=30,
                          fetch=mk_fetch(_flaky))
    mchk("M26 实测到守护者 -> settle 预算内等到 200 判绿（等预算靠的就是它）",
         s_supp[0] == 200 and recovered_after_retry(s_supp[2]),
         "code=%s trace=%s" % (s_supp[0], s_supp[2][:90]))
    # 无守护者 -> 预算必须归 0，且真的只试 3 次就红：
    # 否则「等久一点」就成了一台万能橡皮擦，把真死也洗成绿、还慢。
    # 注意别拿「窗口内可恢复」的抖动夹具来测这条 —— settle=0 只是不给
    # *额外*预算，D56 的 3 次基础重试仍在，那时判绿是对的（见 M15）。
    s_unsup = probe_readyz(8787, attempts=3, sleep_s=0,
                           settle_s=settle_budget("none"),
                           fetch=mk_fetch([_Boom("HTTP Error 503")]))
    mchk("M27 无守护者 -> 预算归 0 且只试 3 次就红，不许靠等造绿",
         (settle_budget("none") == 0.0
          and settle_budget("headroom-agent") > 0
          and s_unsup[0] != 200
          and s_unsup[2].count("attempt ") == 3),
         "budget_none=%s tries=%s code=%s"
         % (settle_budget("none"), s_unsup[2].count("attempt "), s_unsup[0]))
    # 自愈判据本身：单发即 200 不算自愈；重试后才 200 才算
    mchk("M28 recovered_after_retry 不把「本来就好」记成自愈",
         (not recovered_after_retry("attempt 1/3 HTTP=200"))
         and recovered_after_retry("attempt 1/21 HTTP=503 attempt 2/21 HTTP=200")
         and (not recovered_after_retry("")),
         "单发=%s 重试后=%s 空=%s"
         % (recovered_after_retry("attempt 1/3 HTTP=200"),
            recovered_after_retry("attempt 1/21 HTTP=503 attempt 2/21 HTTP=200"),
            recovered_after_retry("")))
    # D58：把「不许靠等造绿」放到**真传输层**上再戳一次。
    # 同一个真 server、复刻一段 9.4s 的 503 窗口（夹具时长，非实测口径），
    # 预算归 0 就必须红 ——
    # 否则上面那条正向检查只是「永远绿的表演」。
    _r_rc, _r_tr, _r_el, _r_win = real_503_settle_probe(window_s=9.4,
                                                     budget=settle_budget("none"))
    mchk("M29 真 server 9.4s 夹具窗口 + 无守护者预算 -> 必须红（真路径不许靠等造绿）",
         _r_rc != 200 and _r_tr.count("attempt ") == 3,
         "code=%s tries=%s 耗时=%.1fs" % (_r_rc, _r_tr.count("attempt "), _r_el))

    # D58b 的三条牙，盯的是「实测窗口 → 预算」这条推导链本身。
    # ① 中间的 200 必须切断连片：合成一段会把窗口算成跨整段，预算失去依据。
    _seq = [(0.0, True), (5.0, False), (10.0, True), (15.0, False)]
    _w_seq = unready_windows(_seq, gap_cap=10.0)
    mchk("M30 unready_windows 被中间的 200 切断（不得合成一整段）",
         len(_w_seq) == 2 and all(abs(x - 5.0) < 1e-6 for x in _w_seq),
         "seq 503/200/503/200 -> %s" % [round(x, 2) for x in _w_seq])
    # ② 长空窗必须封顶：稀疏探活不等于慢自愈，不封顶会把预算顶成废数。
    _w_sparse = unready_windows([(0.0, True), (200.0, False)], gap_cap=10.0)
    mchk("M31 稀疏探活的长空窗按 gap_cap 封顶（不得读成 200s 的慢自愈）",
         len(_w_sparse) == 1 and abs(_w_sparse[0] - 10.0) < 1e-6,
         "1 个 503 + 200s 后才来 200 -> %.1fs（cap=10）" % _w_sparse[0])
    # ③ 预算边界：差 1s 就红、刚好够就绿 —— 锁死「>= need」而不是「> need」。
    mchk("M32 settle_covers 边界：预算差 1s 判红、刚好够判绿",
         (not settle_covers(31.5, budget=37.4)[0])
         and settle_covers(31.5, budget=37.6)[0],
         "need=37.5 -> 预算37.4 红=%s / 37.6 绿=%s"
         % (not settle_covers(31.5, budget=37.4)[0],
            settle_covers(31.5, budget=37.6)[0]))

    # D59-1：轮转合并。锁死本轮实测的 P0 假绿 —— 2026-10-03 17:53 门跑出
    # rc=1（最坏 286.9s），17:54 日志轮转后跑出 rc=0（69 事件 / 0 段 / 0.0s）。
    # 同一台机同一天同一份日志，只因当日历史搬进 proxy.log.1 就变绿。
    _d59 = tempfile.mkdtemp(prefix="d59-")

    def _w59(fp, lines):
        with io.open(fp, "w", encoding="utf-8") as fh:
            fh.write("".join(lines))
    try:
        _rot, _live = os.path.join(_d59, "proxy.log.1"), \
            os.path.join(_d59, "proxy.log")
        _w59(_rot, [
            "2026-10-03 10:00:00,000 - headroom.proxy - INFO - "
            "event=proxy_inbound_response id=a path=/readyz status=503 duration_ms=9.9\n",
            "2026-10-03 10:00:03,000 - headroom.proxy - INFO - "
            "event=proxy_inbound_response id=b path=/readyz status=503 duration_ms=9.9\n",
            "2026-10-03 10:00:05,000 - headroom.proxy - INFO - "
            "event=proxy_inbound_response id=c path=/readyz status=200 duration_ms=1.0\n",
        ])
        _w59(_live, [
            "2026-10-03 10:05:00,000 - headroom.proxy - INFO - "
            "event=proxy_inbound_response id=d path=/readyz status=200 duration_ms=1.0\n",
        ])
        _mrg = readyz_events(path=[_rot, _live], day_prefix="2026-10-03")
        _only = readyz_events(path=_live, day_prefix="2026-10-03")
        mchk("M33 轮转合并：.1 早事件 + live 晚事件必须合并且按时间排序",
             len(_mrg) == 4 and _mrg[0][0] < _mrg[-1][0] and len(_only) == 1,
             "合并 %d 条 / 单读 live 仅 %d 条 = 17:54 那次假绿的形状"
             % (len(_mrg), len(_only)))
        _rev = readyz_events(path=[_live, _rot], day_prefix="2026-10-03")
        mchk("M34 轮转顺序给反也必须仍按时间排序（不许靠调用方给对顺序）",
             [round(x[0], 3) for x in _rev] == [round(x[0], 3) for x in _mrg],
             "给反顺序后仍时间升序=%s"
             % ([round(x[0], 3) for x in _rev] == [round(x[0], 3) for x in _mrg]))
    finally:
        _shutil.rmtree(_d59, ignore_errors=True)
    # D59-2：生命周期 vs 其他。预算只管「重生后打通」；网络出口抖动
    # （进程全程活着、每个 503 秒回）算进去就是用错药。
    _ev_l = [(0.0, True), (4.0, True), (8.0, False)]
    _mk_l = [(2.0, "shutdown"), (3.0, "start")]
    _sp_l = unready_spans(_ev_l, marks=_mk_l, gap_cap=10.0)
    mchk("M35 紧邻 start 锚点的 503 连片 -> lifecycle（预算该管它）",
         len(_sp_l) == 1 and _sp_l[0][1] is True,
         "shut@2 start@3 + 503 连片 0..4 -> %s"
         % [(round(d, 1), l) for d, l in _sp_l])
    _ev_n = [(100.0, True), (104.0, True), (108.0, False)]
    _sp_n = unready_spans(_ev_n, marks=_mk_l, gap_cap=10.0)
    mchk("M36 无锚点的 503 连片 -> other（网络抖动，不许顶预算）",
         len(_sp_n) == 1 and _sp_n[0][1] is False,
         "锚点在 2/3s、连片在 100..104s -> %s"
         % [(round(d, 1), l) for d, l in _sp_n])
    mchk("M37 无锚点时全部归 other（不得凭空造出 lifecycle 窗口）",
         all(not l for _, l in unready_spans(_ev_n, marks=[], gap_cap=10.0)),
         "marks=[] -> %s" % [(round(d, 1), l) for d, l
                             in unready_spans(_ev_n, marks=[], gap_cap=10.0)])
    mchk("M38 预算 < 最坏 lifecycle + 余量必须红（分类后仍 fail-closed）",
         (not settle_covers(27.8, budget=33.7)[0])
         and settle_covers(27.8, budget=33.8)[0],
         "worst_life=27.8 need=33.8 -> 33.7 红=%s / 33.8 绿=%s"
         % (not settle_covers(27.8, budget=33.7)[0],
            settle_covers(27.8, budget=33.8)[0]))
    mchk("M39 开机通道两件都在才绿",
         *launchd_channel_ok(True, True))
    mchk("M40 plist 被删 -> 红（8787 还活着也不许放行）",
         (not launchd_channel_ok(False, True)[0]),
         launchd_channel_ok(False, True)[1])
    mchk("M41 plist 在盘但 launchd 没认领 -> 红（重启即静默失效）",
         (not launchd_channel_ok(True, False)[0]),
         launchd_channel_ok(True, False)[1])
    # --- D59b 重启循环 vs 合法重启 ---
    # 夹具：一段 66s 的 503 连片，里面挤了 5 次 start（实测 21:23:16->21:24:22
    # 的形状）。这是重启循环，不是「一次重启慢了 66s」。
    _ev_flap = [(float(i), True) for i in range(0, 66, 2)]
    _mk_flap = [(2.0, "shutdown"), (5.0, "start"), (20.0, "start"),
                (40.0, "start"), (60.0, "start")]
    _r_flap = SB.worst_settle_window(_ev_flap, _mk_flap, 10.0, 15.0)
    mchk("M42 >=3 次 start 挤一段 -> 判为重启循环（不再污染重启预算）",
         _r_flap["n_flap"] == 1 and _r_flap["worst"] == 0.0,
         "5 starts/66s -> flap=%d 计入预算的最坏=%.1fs"
         % (_r_flap["n_flap"], _r_flap["worst"]))
    mchk("M43 一次 start 的长窗口仍算合法重启（不许把真重启误判成循环）",
         (lambda r: r["n_flap"] == 0 and r["worst"] > 60.0)
         (SB.worst_settle_window(_ev_flap, [(2.0, "shutdown"), (5.0, "start")],
                                 10.0, 15.0)),
         "1 start/66s -> 仍是合法重启，最坏=%.1fs"
         % SB.worst_settle_window(_ev_flap, [(2.0, "shutdown"), (5.0, "start")],
                                  10.0, 15.0)["worst"])
    mchk("M44 最近窗口内的循环 -> 门自己判红（不许把在抖的循环静默放行）",
         bool(SB.recent_flaps(_r_flap["flaps"], _r_flap["flaps"][0]["t0"] + 60.0)),
         "flap t0+60s 仍在 %ds 窗口内 -> 判红" % SB.FLAP_RECENT_S)
    mchk("M45 早已停止的循环 -> 不再判红（历史事实不是当前缺陷）",
         not SB.recent_flaps(_r_flap["flaps"],
                             _r_flap["flaps"][0]["t0"] + SB.FLAP_RECENT_S + 600.0),
         "flap t0+%ds 之后再看 -> 已出窗口"
         % (SB.FLAP_RECENT_S + 600))
    mchk("M46 循环仍计入预算的话本门就是空门（反向锁：排除才是有意的）",
         (not settle_covers(66.2, budget=60.0)[0])
         and settle_covers(32.2, budget=60.0)[0],
         "66.2s 预算 60 -> 红（所以必须排除）；32.2s -> 绿（真重启够用）")
    bad = [m for m in MUT if not m["ok"]]
    print("  变异体 %d/%d 抓住" % (len(MUT) - len(bad), len(MUT)))
    return len(bad)


def manifest_view(m):
    return {"host": m.get("host"), "port": m.get("port"),
            "mode": m.get("proxy_mode"), "backend": m.get("backend"),
            "telemetry": bool(m.get("telemetry_enabled"))}


def local_ports(text):
    return set(int(p) for p in re.findall(r"127\.0\.0\.1:(\d{2,5})", text or ""))


def listening_ports(ports):
    out = {}
    for p in sorted(set(ports) | {DEFAULT_PORT, STALE_HOP}):
        rc, txt = sh("lsof", "-tiTCP:%d" % p, "-sTCP:LISTEN")
        out[p] = bool([x for x in txt.split() if x.isdigit()])
    return out


def probe():
    global PROFILE
    print("== headroom 8787 契约门 D43")
    hooks = {}
    if os.path.exists(HOOKS):
        try:
            hooks = json.load(open(HOOKS, encoding="utf-8"))
        except Exception as e:
            chk("hooks-parse", False, str(e)[:100])
    hook_txt = json.dumps(hooks, ensure_ascii=False)
    pm = re.search(r"headroom[^\"]*ensure[^\"]*--profile\s+([\w.-]+)", hook_txt)
    PROFILE = pm.group(1) if pm else ""
    if not PROFILE and os.path.isdir(DEPLOY_DIR):
        subs = [d for d in sorted(os.listdir(DEPLOY_DIR))
                if os.path.isdir(os.path.join(DEPLOY_DIR, d))]
        if len(subs) == 1:
            PROFILE = subs[0]
    chk("profile-resolved", bool(PROFILE),
        "profile=%s manifest在盘=%s"
        % (PROFILE or "无",
           os.path.exists(os.path.join(DEPLOY_DIR, PROFILE or "-",
                                        "manifest.json"))))

    mpath = os.path.join(DEPLOY_DIR, PROFILE or "-", "manifest.json")
    man = {}
    if os.path.exists(mpath):
        try:
            man = json.load(open(mpath, encoding="utf-8"))
        except Exception as e:
            chk("manifest-parse", False, str(e)[:100])
    chk("manifest-in-sync", bool(man) and man.get("port") == DEFAULT_PORT,
        "port=%s health=%s preset=%s 自报supervisor=%s"
        % (man.get("port"), man.get("health_url"), man.get("preset"),
           man.get("supervisor_kind")))

    port = int(man.get("port") or DEFAULT_PORT)
    rc, txt = sh("lsof", "-tiTCP:%d" % port, "-sTCP:LISTEN")
    pid = next((int(x) for x in txt.split() if x.isdigit()), 0)
    argv = ""
    if pid:
        rc, argv = sh("ps", "-p", str(pid), "-o", "args=")
        argv = (argv or "").strip()
    chk("live-listen", bool(pid) and "headroom" in argv,
        ("pid=%d %s" % (pid, argv[:110])) if pid else "%d 无监听" % port)

    sup_kind, sup_detail = measure_supervisor(pid)
    chk("supervisor-measured", sup_kind != "none",
        "实测=%s vs 自报=%s · %s"
        % (sup_kind, man.get("supervisor_kind"), sup_detail))

    def _pid_now():
        _rc, _t = sh("lsof", "-tiTCP:%d" % port, "-sTCP:LISTEN")
        return next((int(x) for x in _t.split() if x.isdigit()), 0)

    # D58：settle 窗口只给「实测到守护者」的实例；sup_kind=none 时 settle=0，
    # 即回到 D56 的 3 次快判（M16/M20 的红必须照样红得快）。
    settle = settle_budget(sup_kind)
    code, body, trace = probe_readyz(port, pid_probe=_pid_now, settle_s=settle)
    chk("readyz-200", code == 200,
        "HTTP=%s %s [%s]" % (code, body.replace("\n", " "), trace))
    selfhealed = recovered_after_retry(trace)

    ap = parse_argv_params(argv)
    ok, detail = params_match(manifest_view(man), ap)
    chk("params-no-drift", ok, detail)

    if PROFILE:
        rc, out = sh(HR_BIN, "install", "status", "--profile", PROFILE)
        seg = out.split("Healthy:")[1].split("\n")[0].strip() if "Healthy:" in out else "?"
        chk("install-status-healthy", rc == 0 and seg == "yes",
            "rc=%s Healthy=%s" % (rc, seg))
        rc, out = sh(HR_BIN, "install", "agent", "ensure", "--profile", PROFILE)
        chk("recovery-cmd-works", rc == 0, "ensure rc=%s %s" % (rc, out.strip()[:60]))
    else:
        chk("install-status-healthy", False, "无 profile，无法核")
        chk("recovery-cmd-works", False, "无 profile，无法核")

    ok, detail = hook_profile_ok(hooks, PROFILE) if PROFILE else (False, "无 profile")
    chk("recovery-hook-registered", ok, detail)

    cfiles = {"codex config.toml": CODEX_CFG, "claude settings.json": CLAUDE_CFG}
    texts = {}
    cports = {}
    for k, p in cfiles.items():
        t = ""
        if os.path.exists(p):
            t = open(p, encoding="utf-8", errors="replace").read()
        texts[k] = t
        cports[k] = sorted(local_ports(t))
    allp = set()
    for v in cports.values():
        allp |= set(v)
    listen = listening_ports(allp)
    ok, detail = upstream_ports_ok(allp, listen)
    chk("client-upstream-alive", ok,
        (detail + " " + json.dumps(cports, ensure_ascii=False))[:200])

    ok, detail = stale_hop_ok(texts["codex config.toml"] + texts["claude settings.json"],
                              listen)
    chk("stale-sse-hop", ok, detail)

    lb = load_bearing(allp, port)
    chk("headroom-load-bearing", True,
        ("客户端真走 %d -> 它一死就会影响用户，门必须盯紧" % port) if lb
        else "客户端走 %s，未走 %d -> 8787 死寂无声（实测常态）"
             % (sorted(allp) or "无本机上游", port))

    # D58：抖动自愈要留证，但**只在非关键路径上**允许「抖完自己好」判绿。
    # 真在用的端口（lb=True）抖完自愈也要红 —— 那说明用户在用的服务真的不可用过。
    chk("readyz-selfheal-noted", (not selfhealed) or (not lb),
        ("本轮 readyz 抖动后自愈（settle 预算 %.0fs，守护者=%s）"
         % (settle, sup_kind)) if selfhealed
        else ("首探即 200，无抖动" if not lb else "首探即 200，无抖动"))

    # D58：真 HTTP 503 窗口 -> settle 预算必须真能盖住（内核分配端口，跑完即关）
    _rc, _tr, _el, _win = real_503_settle_probe()
    chk("readyz-settle-realpath", _rc == 200 and recovered_after_retry(_tr),
        "真 server 503 窗口 %.1fs -> %.1fs 后转 200 · 预算 %.0fs · %s"
        % (_win, _el, settle_budget("headroom-agent"), _tr[:70]))

    # D58b：settle 预算必须盖住**当天真日志量出来的最坏可观测窗口**。
    # 首版把预算拍成 30s，依据只是 08:14 那一段（27.2s）—— 重算全天才发现
    # 503 不是偶发：当日 418 个 503 / 20 段连片，最坏一段 31.5s，
    # 30s 预算只剩 -1.5s 余量，下一次稍长的抖动就又变回假红。
    # 所以这条判据每次跑门都重算，够不到就报红并写清该调到多少。
    _today = time.strftime("%Y-%m-%d")
    _ev = readyz_events(day_prefix=_today)
    _spans = unready_spans(_ev)
    _marks_today = restart_marks(day_prefix=_today)
    _split = SB.worst_settle_window(_ev, _marks_today,
                                    gap_cap=PROBE_GAP_CAP_S,
                                    adjacent=RESTART_ADJACENT_S)
    _life = [s["dur"] for s in _split["spans"]
             if s["life"] and not SB.is_flap(s, _marks_today,
                                            RESTART_ADJACENT_S)]
    _other = [d for d, l in _spans if not l]
    _worst = _split["worst"]
    _cov_ok, _cov_why = settle_covers(_worst)
    if not _ev:
        # 真日志读不到 -> 显式记 SKIP 并退回静态地板，不许「读不到」变静默绿。
        _floor = SETTLE_MARGIN_S + 30.0
        _cov_ok = READY_SETTLE_S >= _floor
        _cov_why = ("[SKIP] 真日志无当日事件，退回静态地板 %.0fs（实测最坏按 30s 记）"
                    % _floor) + " · " + _cov_why
    chk("readyz-settle-covers-log", _cov_ok,
        "当日 readyz %d 事件（读 %d 个轮转文件）/ 生命周期 %d 段 最坏 %.1fs"
        " / 非生命周期 %d 段 最坏 %.1fs（网络出口抖动，不计入预算） · %s"
        % (len(_ev), len(proxy_log_paths()), len(_life), _worst,
           len(_other), max(_other) if _other else 0.0, _cov_why))

    at_kind, at_detail = measure_autostart(port)
    _flap_now = SB.recent_flaps(_split["flaps"], time.time())
    chk("restart-loop-no-flap",
        not _flap_now,
        ("当日 %d 段重启循环（>=%d 次 start 挤在一段里），最长 %.1fs；"
         "最近 %ds 内 %d 段 -> %s"
         % (len(_split["flaps"]), SB.FLAP_MIN_STARTS, _split["worst_flap"],
            SB.FLAP_RECENT_S, len(_flap_now),
            "还在抖 = 真缺陷，等再久也不会好（supervisor 抢占 8787 导致 "
            "EADDRINUSE，需要修 runner 的 kill-then-rebind 竞态）"
            if _flap_now else "已停止，不再抖"))
        + " · 重启循环不计入 settle 预算（那会把门调宽去容忍第三方缺陷）")
    at_ok, at_why = autostart_verdict(at_kind, lb)
    chk("autostart-or-unused", at_ok,
        "%s · 关键路径=%s · %s" % (at_kind, lb, at_why) + " · " + at_detail)

    _hr_plist = os.path.join(HOME, "Library", "LaunchAgents",
                              "com.headroom.proxy.plist")
    _rc2, _ls = sh("launchctl", "list")
    chk("launchd-channel-real", *launchd_channel_ok(
        os.path.exists(_hr_plist),
        any(l.split() and l.split()[-1] == "com.headroom.proxy"
            for l in _ls.splitlines() if l.strip())))


if __name__ == "__main__":
    probe()
    missed = mutants()
    total = len(RESULTS)
    # 计数一律现算，不写死 —— 与 D42 台面标签反漂移同一个教训：
    # 写死「正向 9 + 变异体 14」会在增删判据后变成假绿。
    print("\nHEADROOM_GATE %s 正向 %d/%d · 变异体 %d/%d 抓住 · 漏网 %d"
          % ("OK" if FAIL == 0 and missed == 0 else "FAIL",
             total - FAIL, total,
             len(MUT) - missed, len(MUT), missed))
    if FAIL:
        print("  修复: %s install agent ensure --profile %s"
              % (HR_BIN, PROFILE or "<profile>"))
    out = os.path.join(ROOT, "work", "headroom_gate.json")
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"ok": FAIL == 0 and missed == 0, "port": DEFAULT_PORT,
                       "profile": PROFILE, "checks": RESULTS,
                       "mutants": MUT}, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print("  [WARN] 回执写盘失败: %s" % str(e)[:80])
    sys.exit(0 if FAIL == 0 and missed == 0 else 1)
