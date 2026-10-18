#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AgentOS 终端 CLI —— 驾驶舱统一入口（只用标准库）。

子命令
  serve     起本地服务：默认 8790、占用顺延；显式拒绝 8787
            （8787 是 headroom proxy 的常驻端口，serve 不许抢）。
  health    判 __health 的 ok:true（rc=0 才是真健康）。
  gates     转发 tools/gates.sh（门链入口；零参数拒绝，与 gates.sh 同口径）。
  open      浏览器打开驾驶舱首页。
  resident  常驻真值门 tools/resident_gate.py：核 launchd 常驻实例。
  headroom  headroom 8787 契约门 tools/headroom_gate.py。
  gh        GitHub 只读面：auth/whoami/repos/repo/issues/prs/runs/search
            （数据源只有 tools/gh_client.py，结构上发不出远端写入）。
  term      跑一条只读命令（转发 POST /__term，回服务端原话与真实 rc）。
  tui       终端交互主界面：↑↓ 移动、回车明细/进入、Tab 切面、
            i 或 / 进输入行，
            十三视图全部走真 8790 数据 —— 总览 / 轮次 / 矩阵 / 账本 /
            引擎 / Agent 台（工具卡·审批卡·流式 transcript·指纹
            diff·composer）／目标台 / 复盘台 / 能力地图 / 工作区 /
            终端（真可打字跑命令）/ 文件（树+代码）/ 设置（逐行标来源）。
            视图名、顺序、数量与 web 端 13 个 tab 一一对应，不另立一套。

设计纪律（与 serve.py 同源）：
  · serve 不复刻 serve.py 的实现，exec 到它 —— 两份实现迟早漂移。
  · tui 的每个视图失败都如实报出（fail-closed），绝不拿占位数据冒充面板。
  · 轮次/矩阵先走引擎只读入口 POST /__goal/chain，409/超时再退到
    GET /__fs 只读面取同一份 work/GOAL.json —— 两条路读的是同一个真源。
"""

import argparse
import json
import os
import queue
import select
import shutil
import subprocess
import sys
import termios
import textwrap
import unicodedata
import threading
import time
import tty
import uuid
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVE = os.path.join(HERE, "serve.py")
DEFAULT_PORT = 8790
FORBIDDEN_PORT = 8787  # headroom proxy 常驻端口，serve 不得抢

# 十三视图：名字、顺序、条数都与 web 端 index.template.html 的 13 个 tab
# （data-v = over/round/mx/led/eng/work/goal/retro/cap/ws/term/file/set）
# 逐项对应。CLI 不另立一套视图表 —— 两份表迟早漂移，而漂移的那天
# 「终端里能看到」和「浏览器里能看到」就不再是同一个产品。
VIEWS = (
    "总览",
    "轮次",
    "阶段矩阵",
    "证据账本",
    "引擎",
    "Agent 台",
    "目标台",
    "复盘台",
    "能力地图",
    "工作区",
    "终端",
    "文件",
    "设置",
)
AGENT_NAME = "Agent 台"
# 靠**名字**定位 Agent 台，不靠 len(VIEWS)-1：下标写法在只有六个视图时
# 恰好等于末位，一旦中间插视图就静默指错面，而 _FACES/face is not None/
# 回车运行授权四处都依赖它 —— 那会是「在错误的面板上按回车却执行了动作」。
AGENT_VI = VIEWS.index(AGENT_NAME)
TERM_NAME = "终端"
TERM_VI = VIEWS.index(TERM_NAME)
# 数字键位随视图数长：1-9 是单位数键，"0" 是第 10 面（与 web 端
# index.template.html 的 k==='0'?10:+k 同一口径），第 11 面起没有键，
# 必须 Tab。footer 写死 "1-6" 会在视图涨到 13 个后变成假说明；
# 反过来，十三个视图时还把 "0" 说成无操作，就是界面上撒谎。
DIGIT_HINT = (
    "1-9,0=10"
    if len(VIEWS) >= 10
    else ("1-%d" % len(VIEWS) if len(VIEWS) > 1 else "无")
)
LOADERS = []  # 十三个视图的取数函数，main 前填充（见下）
FS_PEEK = 262144  # 与 serve.py FS_MAX_BYTES 同口径（只读上限）
# CLI 自身版本。它和 index.html 的构建指纹是两件事：指纹说「屏上这份产物是哪次
# 构建」，版本说「命令行契约是哪一版」。混成一个数，就没法在 --version 里回答
# 「你的 CLI 支不支持 -p」这种问题。
VERSION = "0.2.0"


def _dw(s):
    """终端显示列宽：CJK/全角算 2 列，其余 1 列。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _clip(s, w):
    """终端列宽硬上限：按**显示列宽**截断并补省略号，
    让人看得出「还有内容被截」。按字符数截会在中英混排上
    超出真实列宽（实测 80 列终端渲染出 83 列，右侧折行糊屏）。"""
    s = str(s)
    if _dw(s) <= w:
        return s
    out, used = [], 0
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in "WF" else 1
        if used + cw > w - 1:
            break
        out.append(c)
        used += cw
    return "".join(out) + "…"


def _pad(s, n):
    """按显示列宽右补空格到 n 列，中文标签也能和英文标签对齐。"""
    s = str(s)
    return s + " " * max(0, n - _dw(s))


def _short(s, n):
    """值列预裁：超过 n 列截断并补省略号。
    之前是裸 s[:n] —— 尾部被无声切断，用户看到的「不完整」和「本来就短」
    长得一模一样（W4 PTY 走查实测：项目根路径被切到 agentos-co，无任何提示）。
    _clip 管整行宽度，_short 管单值，两层都补省略号。"""
    s = str(s)
    if len(s) <= n:
        return s
    return s[: max(0, n - 1)] + "…"


# ── Claude Code 皮：TUI 交互面的配色 ────────────────────────────────
# 只作用于 TUI 帧；数据命令（health / -p / tui </dev/null）保持全量零
# ANSI —— cli_gate 的 tui-nontty-zero-ansi 判据吃的就是那条非 TTY 路径。
# 探测：NO_COLOR 在场或 TERM 空/dumb → 无皮（帧与旧版逐字同构）；
# TERM 带 256color 或 COLORTERM=truecolor/24bit → 真 24bit 皮。
# tui_walk / cli_gate 的 PTY 都固定 TERM=xterm-256color，两具量具的解析
# 全在「剥 ANSI 后的文本」层；唯一被钉死的色码是红字 \x1b[31m（见 paint）。
_SKIN_SGR = {
    "brand": "\x1b[38;2;217;119;87m",  # Claude 橙 #D97757
    "cyan": "\x1b[38;2;126;168;188m",  # 标签青
    "dim": "\x1b[38;2;146;139;132m",  # 次要灰
    "bold": "\x1b[1m",
    "r": "\x1b[0m",
}


def _skin_ok():
    t = os.environ.get("TERM", "")
    if os.environ.get("NO_COLOR") or t in ("", "dumb"):
        return False
    return "256color" in t or os.environ.get("COLORTERM", "") in ("truecolor", "24bit")


def base_url(args):
    if getattr(args, "url", None):
        return args.url.rstrip("/")
    return "http://127.0.0.1:%d" % getattr(args, "port", DEFAULT_PORT)


# 本机自连必须绕开代理。用户终端里 http_proxy/https_proxy 常年是开着的
# （本机实测就是 127.0.0.1:7897），而 urllib.getproxies() 优先吃环境变量、
# 不看 macOS 系统设置里的 127.0.0.1 例外名单，proxy_bypass("127.0.0.1")
# 返回 False —— 于是"检查一个本机服务"要先绕一趟本地代理：端口没人听时代理
# 回 502，CLI 把代理的话当成服务端的话报出去（实测 --port 1 报
# HTTP=502 而不是连接被拒），排查的人照着错的方向查；代理一挂整个 CLI
# 全红，而服务明明是同一个机器上的进程。所以凡是 base_url 的自连都走
# 无代理 opener。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(url, payload=None, timeout=20):
    """GET（payload=None）或 POST（dict，自动带 X-AgentOS-Action:1）。

    返回 (status, obj)。HTTP 错误码不抛 —— 让调用方按状态码如实判，
    与 serve.py 的执行面语义一致；网络异常才抛 RuntimeError。
    """
    if payload is None:
        req = urllib.request.Request(url, method="GET")
    else:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "X-AgentOS-Action": "1"},
        )
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        # 错误响应体也要读：404 的 /__act/resolve 把 candidates/allowlist
        # 全放在 body 里，丢体就等于把「这句需求没匹配上」谎报成「白名单是
        # 空的 = 判红」，排查的人会照着错的方向查半天。
        try:
            raw = e.read().decode("utf-8", "replace")
            return e.code, json.loads(raw) if raw.strip() else {}
        except Exception:  # noqa: BLE001
            return e.code, {}
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("HTTP %s 失败: %s" % (url, str(e)[:120]))


# ── serve：exec 到 serve.py，单一真相源 ───────────────────────────
def cmd_serve(args):
    if args.port == FORBIDDEN_PORT:
        print(
            "FAIL: %d 是 headroom proxy 的常驻端口，serve 不得抢占"
            "（默认 %d，占用顺延）" % (FORBIDDEN_PORT, DEFAULT_PORT),
            file=sys.stderr,
        )
        return 2
    argv = [sys.executable, SERVE]
    if args.port != DEFAULT_PORT:
        argv += ["--port", str(args.port)]
    if args.watch_tree:
        argv += ["--watch-tree"]
    if args.poll is not None:
        argv += ["--poll", str(args.poll)]
    if args.tree:
        argv += ["--tree", args.tree]
    if args.open_browser:
        argv += ["--open"]
    # execv 取代本进程：信号（Ctrl+C）与退出码都直接属于 serve.py
    os.execv(sys.executable, argv)
    return 0  # 不可达


# ── health：判 __health ok:true ───────────────────────────────────
def cmd_doctor(_args):
    """一键体检（对标 CC /doctor）：配置→key→端点→服务→门锁，红给处方。

    为什么并到 agentos 而不进 agent_core：doctor 是**壳层运维面**（它的
    读者是用户，产出是处方，不是引擎能力）；但它要借 resolve_provider
    （真正会跑的解析路径），所以 import agent_core。rc：0 全绿 / 1 有红。
    """
    checks = []

    def ck(name, ok, detail, fix=""):
        checks.append((name, bool(ok), detail, fix))

    # ① 配置文件
    acfg_path = os.path.expanduser("~/.agentos/config.toml")
    cfg, cfg_err = {}, ""
    if os.path.exists(acfg_path):
        try:
            import tomllib

            with open(acfg_path, "rb") as f:
                cfg = tomllib.load(f)
        except Exception as e:  # noqa: BLE001  坏 tom 不藏
            cfg_err = str(e)[:120]
        ck(
            "config",
            not cfg_err,
            ("~/.agentos/config.toml 可解析" if not cfg_err else "坏账：" + cfg_err),
            "重写：agentos config set llm.provider <名>",
        )
    else:
        ck(
            "config",
            False,
            "~/.agentos/config.toml 不存在",
            "python3 tools/agentos.py config set llm.provider opencode",
        )

    llm = (cfg.get("llm") or {}) if isinstance(cfg, dict) else {}
    has_key = bool(str(llm.get("api_key") or "").strip())
    ck(
        "llm.api_key",
        has_key,
        "key 挂着（长度=%d）" % len(str(llm.get("api_key") or ""))
        if has_key
        else "没配 key",
        "agentos config set llm.api_key <新key>",
    )
    ck(
        "llm.provider/model",
        bool(str(llm.get("provider") or "").strip()),
        "provider=%s model=%s"
        % (llm.get("provider") or "-", llm.get("model") or "-"),
        "provider 缺：agentos config set llm.provider opencode",
    )

    # ③ 端点解析 + 探活（借 resolve_provider —— 用户实际会跑的路径）
    try:
        import agent_core as _ac

        spec = _ac.resolve_provider({})
        base = spec["base_url"]
        req = urllib.request.Request(base + "/models", method="GET")
        for k, v in (spec.get("headers") or {}).items():
            req.add_header(k, str(v))
        # 探活必须与 Provider 实跑**同一条传输路径**：同一 UA（裸
        # Python-urllib 会被 WAF 403——实测 2026-10-18 doctor 误红）、
        # 同一直连（经本机 clash 抖）。否则 doctor 与真机不同命。
        req.add_header("User-Agent", getattr(_ac, "_DEFAULT_UA", "agentos"))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=10) as resp:
            st = resp.status
            body = resp.read(4096)
        live = st == 200 and b'"' in body
        ck(
            "endpoint",
            live,
            "%s GET /models HTTP=%s" % (base, st),
            "裸 401=api_key_expired：agentos config set llm.api_key <新key>",
        )
        ck(
            "model-on-endpoint",
            bool(
                llm.get("model") is None
                or any(
                    str(m.get("id") or "") == str(llm.get("model"))
                    for m in json.loads(body.decode("utf-8", "replace")).get(
                        "data", []
                    )
                    if isinstance(m, dict)
                )
                or not json.loads(body.decode("utf-8", "replace")).get("data")
            ),
            "config 模型 %s 在端点清单里" % (llm.get("model") or "-"),
            "agentos config set llm.model <端点清单里的 id>",
        )
    except urllib.error.HTTPError as e:
        ck("endpoint", False, "HTTP %s" % e.code, "401/403=换 key；4xx 稳定=config")
    except Exception as e:  # noqa: BLE001  网络/解析都归基础设施
        ck("endpoint", False, str(e)[:120], "SSL EOF 类=网络抖动（双出口自愈会兜）")

    # ④ 本地服务
    hb = base_url(_args)
    try:
        st, h = http_json(hb + "/__health")
        ck("serve", st == 200 and h.get("ok") is True,
           "%s/__health HTTP=%s" % (hb, st),
           "agentos serve --watch-tree 起常驻")
    except Exception as e:  # noqa: BLE001
        ck("serve", False, str(e)[:80], "agentos serve --watch-tree")

    # ⑤ 门链锁（陈旧锁会冤枉下一次链）
    lockd = os.path.join(ROOT, "work", "gates.lockd")
    if os.path.exists(lockd):
        holder = ""
        holder_p = os.path.join(lockd, "holder")
        try:
            holder = open(holder_p, encoding="utf-8").read()[:60]
        except OSError:
            pass
        ck("gates-lock", True, "持有者=%s（链在跑，正常态）" % holder)
    else:
        ck("gates-lock", True, "无锁（空闲，正常态）")

    fails = [c for c in checks if not c[1]]
    for name, ok, detail, fix in checks:
        print("%s %s  %s%s" % ("PASS" if ok else "FAIL", name, detail,
                               ("  → %s" % fix if (fix and not ok) else "")))
    if fails:
        print("DOCTOR FAIL %d/%d" % (len(fails), len(checks)))
        return 1
    print("DOCTOR OK %d/%d" % (len(checks), len(checks)))
    return 0


def cmd_health(args):
    base = base_url(args)
    try:
        st, h = http_json(base + "/__health")
    except Exception as e:  # noqa: BLE001
        print("health FAIL: %s" % e)
        return 1
    if st != 200 or h.get("ok") is not True:
        print("health FAIL: HTTP=%s ok=%s" % (st, h.get("ok")))
        return 1
    s = h.get("stats") or {}
    print("health OK   %s/__health" % base)
    print("  root      %s" % h.get("root"))
    print("  tree      %s" % h.get("tree"))
    print("  index     %s" % h.get("index"))
    print("  data      %s" % h.get("data"))
    print("  clients   %s" % h.get("clients"))
    print("  started   %s" % s.get("started"))
    print("  rebuilds  %s (tree %s)" % (s.get("rebuilds"), s.get("tree_rebuilds")))
    print("  actions   %s" % " ".join(h.get("actions") or []))
    print("  code_sha  %s" % h.get("code_sha"))
    return 0


# ── gates / open / resident / headroom：转发到真门 ────────────────
def cmd_gates(args):
    if not args.gates:
        # 与 gates.sh 同口径：零参数 = 一道门都没跑 = 假绿，必须拒。
        print(
            "FAIL: 零参数 = 假绿。用法: agentos.py gates <门名> [门名…]"
            "（如 resident_gate headroom_gate）",
            file=sys.stderr,
        )
        return 2
    return subprocess.run(
        ["bash", os.path.join(ROOT, "tools", "gates.sh")] + args.gates, cwd=ROOT
    ).returncode


def cmd_open(args):
    url = base_url(args) + "/index.html"
    print("打开 %s" % url)
    webbrowser.open(url)
    return 0


def cmd_resident(args):
    return subprocess.run(
        [sys.executable, os.path.join(HERE, "resident_gate.py")], cwd=ROOT
    ).returncode


def cmd_evolve(args):
    """转 tools/evolve.py：经验账 → 提案 → 沙箱门验 → 自动合入。"""
    argv = ["python3", os.path.join(HERE, "evolve.py")]
    if getattr(args, "rule", None):
        argv += ["--rule", str(args.rule)]
    if getattr(args, "no_merge", False):
        argv += ["--no-merge"]
    return subprocess.run(argv, cwd=ROOT).returncode


def cmd_headroom(args):
    return subprocess.run(
        [sys.executable, os.path.join(HERE, "headroom_gate.py")], cwd=ROOT
    ).returncode


# ── gh：GitHub 只读面 ────────────────────────────────────────────
# 数据源只有一个：tools/gh_client.py（标准库，唯一收口 run_gh 先过只读白名单，
# 写向量在结构上发不出去）。这里不另写一套 HTTP 调用 —— 两套真相迟早漂移，
# 而漂移的那天「CLI 说认证正常」和「gh 其实没认证」会同时成立。
# 退出码口径与 -p 面一致：0 绿 / 1 远端或解析失败 / 2 用法错 / 3 基础设施错
# （找不到 gh、超时、限流、网络）。绝不在失败上给 0。
_GH_USAGE = """用法: agentos gh <子功能> [参数...]
  auth                      读 gh auth status（认证态可读即可）
  whoami                    读 gh api user（登录账号一张表）
  repos [--limit N]         我的仓库列表
  repo <owner/repo>         单个仓库概况
  issues <owner/repo> [--state open|closed|all] [--limit N]
  prs    <owner/repo> [--state open|closed|all] [--limit N]
  runs   <owner/repo> [--limit N]              工作流运行
  search <repos|issues|code|commits|prs> <查询> [--limit N]
全局: --output-format json 给单行可机读 JSON（含 ok 与 sources）。
读面铁律: 读向量只走 gh_client 的只读白名单，调用前就拦。
写面三闸（R1）: 默认拒绝；--yes + gh.write_enabled=true + 交互终端逐次确认
  y/N 三者齐才真写。旗标白名单只认 --title/--body/--head（-F/--body-file
  结构性拒绝）；审计 work/gh_audit.jsonl 哈希链 append-only，只记 SHA+长度，
  fail-closed（审计落不了盘就绝不写远端）。写子命令:
  issue-create <owner/repo> --title T --body B
  issue-comment <owner/repo> <issue号> --body B
  pr-create <owner/repo> --head 分支 --title T --body B   # 仅草稿
  任一写子命令加 --dry-run = 预演（零网络零进程），无需任何授权
  agentos gh audit [--limit N]     # 复核审计哈希链，断链 rc=2
"""

# 失败 kind → CLI 退出码。写清楚而不是笼统给 1：使用者要能分清
# 「gh 没装/限流」和「这个仓库读不到」不是一回事。
_GH_KIND_RC = {
    "no-binary": 3,
    "timeout": 3,
    "network": 3,
    "ratelimit": 3,
    "usage": 2,
    "rc": 1,
    "json": 1,
    "empty": 1,
}


def _gh_load():
    """加载 gh_client。缺失/语法坏都是基础设施错，不静默降级。"""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import gh_client

    return gh_client


def _gh_argv_split(argv):
    """手解 gh 子功能参数：位置量 + --limit/--state（容 --k=v）。

    不交给 argparse：子功能名本身就是位置量，再套一层子解析器会把
    `gh whoami --limit 3` 的 --limit 吞成顶层选项。term 面同一思路。
    """
    pos, opts = [], {}
    i = 0
    while i < len(argv):
        a = str(argv[i])
        if a in ("--limit", "--state") and i + 1 < len(argv):
            opts[a[2:]] = argv[i + 1]
            i += 2
            continue
        if a.startswith("--limit=") or a.startswith("--state="):
            k, _, v = a[2:].partition("=")
            opts[k] = v
            i += 1
            continue
        pos.append(a)
        i += 1
    return pos, opts


def _gh_int(v, what, lo, hi):
    try:
        n = int(str(v).strip())
    except ValueError:
        raise ValueError("%s 不是整数: %r" % (what, v))
    if n < lo or n > hi:
        raise ValueError("%s 越界(%s-%s): %s" % (what, lo, hi, n))
    return n


# ── gh 写面（R1）：严格解析 + fail-closed 审计（哈希链）+ 三闸执行流 ────
# 设计由两路只读部门报告钉死：ARCH 给接缝（收口在 run_gh_write、CLI 剥旗标、
# 审计走 out()），SEC 给闸（S1 --yes 必须 TTY 逐次确认；T1 旗标白名单，
# -F/--body-file 结构性进不来；R1 审计 fail-closed；R2 哈希链防篡改；
# I2 审计只记 SHA+长度不记原文；D1 每 repo 限速）。
_GH_AUDIT = os.path.join(ROOT, "work", "gh_audit.jsonl")
_GH_WRITE_REASONS = {
    "no-yes": "缺 --yes（写面默认拒绝）",
    "non-tty": "--yes 只在交互终端生效（防脚本批量滥用）",
    "config-off": "配置 gh.write_enabled 未开（agentos config set gh.write_enabled true）",
    "rate-limit": "该仓库 1 小时内真写已达上限",
}


def _sha_hex(s):
    import hashlib

    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _gh_write_parse(rest):
    """写子命令严格解析（SEC T1）：值旗标只认 --title/--body/--head，布尔只认
    --yes/--dry-run；其余任何 - 开头 token 一律拒绝 —— -F/--body-file 这类
    文件旗标结构上进不来，body 无法借道本机文件外带。"""
    pos, vals, yes, dry = [], {}, False, False
    toks = [str(t) for t in (rest or [])]
    i = 0
    while i < len(toks):
        t = toks[i]
        if t == "--yes":
            yes = True
            i += 1
            continue
        if t == "--dry-run":
            dry = True
            i += 1
            continue
        if t.startswith("-"):
            k, eq, v = t.lstrip("-").partition("=")
            if k in ("title", "body", "head"):
                if eq:
                    vals[k] = v
                    i += 1
                else:
                    if i + 1 >= len(toks):
                        raise ValueError("--%s 需要值" % k)
                    vals[k] = toks[i + 1]
                    i += 2
                continue
            raise ValueError("写面旗标白名单外（结构性拒绝）：%s" % t)
        pos.append(t)
        i += 1
    return pos, vals, yes, dry


def _gh_audit_last_sha():
    """审计文件最后一条的 sha；文件不存在/为空返回创世串。"""
    if not os.path.exists(_GH_AUDIT):
        return "0" * 64
    last = ""
    with open(_GH_AUDIT, "r", encoding="utf-8") as f:
        for ln in f:
            if ln.strip():
                last = ln
    if not last:
        return "0" * 64
    try:
        return json.loads(last).get("sha") or ("0" * 64)
    except ValueError:
        raise ValueError("审计文件尾行损坏（先 gh audit 查，勿继续真写）")


def _gh_audit_append(kind, sub, repo, num, vals, rc, note):
    """append-only 审计（SEC R2 哈希链）。EXEC/EXEC-DRY 行由调用方 fail-closed：
    本函数抛异常 = 不得继续执行远端写。标题/正文只记 SHA 前缀+长度（SEC I2），
    原文永不落审计。"""
    prev = _gh_audit_last_sha()
    payload = {
        "ts": round(time.time(), 3),
        "kind": kind,
        "sub": sub,
        "repo": repo,
        "num": num,
        "rc": rc,
        "note": note,
        "title_sha": _sha_hex(vals.get("title") or "")[:16],
        "title_len": len(vals.get("title") or ""),
        "body_sha": _sha_hex(vals.get("body") or "")[:16],
        "body_len": len(vals.get("body") or ""),
        "prev": prev,
    }
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    payload["sha"] = _sha_hex(prev + line)
    with open(_GH_AUDIT, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return payload["sha"]


def _gh_audit_rows():
    if not os.path.exists(_GH_AUDIT):
        return []
    rows = []
    with open(_GH_AUDIT, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rows.append(json.loads(ln))
            except ValueError:
                rows.append({"kind": "CORRUPT", "note": ln[:80]})
    return rows


def _gh_audit_exec_rows():
    """真写行 [(ts, repo)]（EXEC 不含 EXEC-DRY）—— 限速的输入。"""
    return [
        (r.get("ts", 0), r.get("repo") or "")
        for r in _gh_audit_rows()
        if r.get("kind") == "EXEC"
    ]


def _gh_audit_verify(path=None):
    """复核哈希链，返回 (ok, n, bad_i, msg)。篡改/删行/改一字都会断链。"""
    path = path or _GH_AUDIT
    if not os.path.exists(path):
        return True, 0, -1, "空链（尚无审计文件）"
    prev = "0" * 64
    n = 0
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
            except ValueError:
                return False, n, n, "第 %d 行不是合法 JSON" % (n + 1)
            got = d.pop("sha", None)
            want = _sha_hex(prev + json.dumps(d, ensure_ascii=False, sort_keys=True))
            if got != want:
                return False, n, n, "第 %d 行 sha 断链（被改过或 prev 错）" % (n + 1)
            prev = got
            n += 1
    return True, n, -1, "自洽 %d 条" % n


def _gh_cfg_flag():
    """gh.write_enabled 原值。读不到/配置层坏 → None（策略按 config-off 拒）。"""
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import agent_core

        cfg = agent_core._read_agentos_config()
        return (cfg.get("gh") or {}).get("write_enabled")
    except Exception:  # noqa: BLE001
        return None


def _gh_write_argv(sub, repo, num, vals):
    """固定模板拼装：用户值一律 --k= 赋值形（gh 的 flag 解析不可能把值当
    flag）；slug 已过 owner/repo 形状校验，num 已过整数界校验。"""
    t, b = vals.get("title"), vals.get("body")
    if sub == "issue-create":
        return ["issue", "create", "-R", repo, "--title=%s" % t, "--body=%s" % b]
    if sub == "issue-comment":
        return ["issue", "comment", str(num), "-R", repo, "--body=%s" % b]
    return [
        "pr",
        "create",
        "-R",
        repo,
        "--draft",
        "--head=%s" % vals.get("head"),
        "--title=%s" % t,
        "--body=%s" % b,
    ]


def _gh_write_cmd(sub, rest, out):
    """写子命令全流程：严格解析 → 策略三闸 → (预演 | TTY 确认+审计先行+执行)。

    审计 fail-closed（SEC R1）：EXEC/EXEC-DRY 行落盘失败 = 不碰 gh。
    真写只发生在：--yes 且 gh.write_enabled=true 且 TTY 逐次确认 y。
    """
    gc = _gh_load()
    try:
        pos, vals, yes, dry = _gh_write_parse(rest)
        if sub == "issue-create":
            if len(pos) != 1:
                raise ValueError("issue-create 需要 <owner/repo>")
            repo, num = _gh_slug(pos, "repo"), None
            if not vals.get("title") or vals.get("body") is None:
                raise ValueError("issue-create 需要 --title 与 --body")
        elif sub == "issue-comment":
            if len(pos) != 2:
                raise ValueError("issue-comment 需要 <owner/repo> <issue号>")
            repo = _gh_slug(pos[:1], "repo")
            num = _gh_int(pos[1], "issue号", 1, 10**7)
            if vals.get("body") is None:
                raise ValueError("issue-comment 需要 --body")
        else:
            if len(pos) != 1:
                raise ValueError("pr-create 需要 <owner/repo>")
            repo, num = _gh_slug(pos, "repo"), None
            if (
                not vals.get("head")
                or not vals.get("title")
                or vals.get("body") is None
            ):
                raise ValueError("pr-create 需要 --head --title --body")
        argv = _gh_write_argv(sub, repo, num, vals)
    except ValueError as e:
        return out(1, sub, [], [], "写面参数不合法：%s" % e)
    disp = "gh " + " ".join(argv)
    if dry:
        try:
            _gh_audit_append(
                "EXEC-DRY", sub, repo, num, vals, 0, "dry-run 预演（未联网未起进程）"
            )
        except Exception as e:  # noqa: BLE001
            return out(1, sub, [], [], "审计落盘失败（fail-closed）：%s" % e)
        return out(
            0,
            sub,
            [
                ("将执行", disp),
                ("模式", "dry-run：零网络零进程，只预演"),
                ("审计", _GH_AUDIT),
            ],
            [disp],
        )
    okp, reason = gc.write_policy(
        yes, sys.stdin.isatty(), _gh_cfg_flag(), repo, _gh_audit_exec_rows()
    )
    if not okp:
        try:
            _gh_audit_append("DENY", sub, repo, num, vals, 1, reason)
        except Exception:  # noqa: BLE001
            pass  # 拒绝方向本就安全
        return out(
            1,
            sub,
            [],
            [],
            "写被拒（%s）：%s" % (reason, _GH_WRITE_REASONS.get(reason, reason)),
        )
    sys.stderr.write("即将执行远端写（%s）：\n  %s\n确认？[y/N] " % (sub, disp))
    sys.stderr.flush()
    ans = (sys.stdin.readline() or "").strip().lower()
    if ans != "y":
        try:
            _gh_audit_append("DENY", sub, repo, num, vals, 1, "confirm-no")
        except Exception:  # noqa: BLE001
            pass
        return out(1, sub, [], [], "已取消（未确认 y，未执行）")
    try:
        _gh_audit_append("EXEC", sub, repo, num, vals, 0, "已确认，审计先行")
    except Exception as e:  # noqa: BLE001
        return out(1, sub, [], [], "审计落盘失败（fail-closed，不执行远端写）：%s" % e)
    r = gc.run_gh_write(argv)
    if not r.get("ok"):
        k = r.get("kind", "rc")
        return out(_GH_KIND_RC.get(k, 1), sub, [], [], "gh 写失败：%s" % r.get("error"))
    lines = (r.get("stdout") or "").strip().splitlines()
    return out(
        0,
        sub,
        [
            ("已执行", disp),
            ("结果", lines[-1] if lines else "（gh rc=0，无 stdout）"),
            ("审计", _GH_AUDIT),
        ],
        [disp],
    )


def _gh_repo_parse(rest, bools, valflags):
    """repo-create/deploy 的严格解析（同 _gh_write_parse 纪律：白名单外
    一律拒）。返回 (pos, vals, boolset)。"""
    pos, vals, bset = [], {}, set()
    toks = [str(t) for t in (rest or [])]
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in bools:
            bset.add(t)
            i += 1
            continue
        if t.startswith("-"):
            k, eq, v = t.lstrip("-").partition("=")
            if k in valflags:
                if eq:
                    vals[k] = v
                    i += 1
                else:
                    if i + 1 >= len(toks):
                        raise ValueError("--%s 需要值" % k)
                    vals[k] = toks[i + 1]
                    i += 2
                continue
            raise ValueError("旗标白名单外（结构性拒绝）：%s" % t)
        pos.append(t)
        i += 1
    return pos, vals, bset


def _gh_repo_create_cmd(rest, out):
    gc = _gh_load()
    try:
        pos, vals, bset = _gh_repo_parse(
            rest, ("--yes", "--dry-run"), ("public", "private")
        )
        name = pos[0] if pos else ""
        if not name or "/" in name and name.count("/") != 1:
            raise ValueError("repo-create 需要 <name> 或 <owner/name>：%r" % name)
        vis = (
            "private"
            if "--private" in bset
            else ("public" if "--public" in bset else "private")
        )
    except ValueError as e:
        return out(1, "repo-create", [], [], "参数不合法：%s" % e)
    argv = ["repo", "create", name, "--" + vis]
    disp = "gh " + " ".join(argv)
    if "--dry-run" in bset:
        try:
            gc.audit_append(
                "EXEC-DRY",
                "repo-create",
                name,
                None,
                {},
                0,
                "dry-run 预演（未联网未起进程）",
            )
        except Exception as e:  # noqa: BLE001
            return out(1, "repo-create", [], [], "审计落盘失败（fail-closed）：%s" % e)
        return out(
            0,
            "repo-create",
            [("将执行", disp), ("模式", "dry-run：零网络零进程")],
            [disp],
        )
    okp, reason = gc.write_policy(
        "--yes" in bset, sys.stdin.isatty(), _gh_cfg_flag(), name, _gh_audit_exec_rows()
    )
    if not okp:
        try:
            _gh_audit_append("DENY", "repo-create", name, None, {}, 1, reason)
        except Exception:  # noqa: BLE001
            pass
        return out(
            1,
            "repo-create",
            [],
            [],
            "写被拒（%s）：%s" % (reason, _GH_WRITE_REASONS.get(reason, reason)),
        )
    sys.stderr.write("即将创建远端仓库：\n  %s\n确认？[y/N] " % disp)
    sys.stderr.flush()
    if (sys.stdin.readline() or "").strip().lower() != "y":
        return out(1, "repo-create", [], [], "已取消（未确认 y）")
    try:
        _gh_audit_append("EXEC", "repo-create", name, None, {}, 0, "已确认，审计先行")
    except Exception as e:  # noqa: BLE001
        return out(
            1, "repo-create", [], [], "审计落盘失败（fail-closed，不执行）：%s" % e
        )
    r = gc.run_gh_write(argv)
    if not r.get("ok"):
        return out(
            _GH_KIND_RC.get(r.get("kind", "rc"), 1),
            "repo-create",
            [],
            [],
            "gh 失败：%s" % r.get("error"),
        )
    return out(
        0,
        "repo-create",
        [("已执行", disp), ("结果", (r.get("stdout") or "-")[:120])],
        [disp],
    )


def _gh_deploy_cmd(rest, out):
    gc = _gh_load()
    try:
        pos, vals, bset = _gh_repo_parse(
            rest, ("--yes", "--dry-run", "--public", "--private"), ("repo",)
        )
        d = pos[0] if pos else ""
        repo = (vals.get("repo") or "").strip()
        if not d:
            raise ValueError("deploy 需要 <目录>")
        if repo.count("/") != 1 or not all(repo.split("/")):
            raise ValueError("deploy 需要 --repo owner/name：%r" % repo)
        dirpath = os.path.abspath(d)
        if not os.path.isdir(dirpath):
            raise ValueError("目录不存在：%s" % dirpath)
    except ValueError as e:
        return out(1, "deploy", [], [], "参数不合法：%s" % e)
    vis = (
        "private"
        if "--private" in bset
        else ("public" if "--public" in bset else "private")
    )

    def _aud(kind, sub, rp, num, v):
        try:
            return (
                _gh_audit_append(kind, sub, rp, num, v, 0, "CLI deploy（%s）" % vis)
                is not None
            )
        except Exception:  # noqa: BLE001
            return False

    if "--dry-run" in bset:
        r = gc.deploy_dir(dirpath, repo, public=vis == "public", dry_run=True)
        if not r.get("ok"):
            return out(1, "deploy", [], [], r.get("error"))
        return out(
            0,
            "deploy",
            [
                ("计划", "；".join(r.get("steps") or [])),
                ("密钥扫描", "命中 %d" % len(r.get("secret_hits") or [])),
            ],
            [],
        )
    okp, reason = gc.write_policy(
        "--yes" in bset, sys.stdin.isatty(), _gh_cfg_flag(), repo, _gh_audit_exec_rows()
    )
    if not okp:
        return out(
            1,
            "deploy",
            [],
            [],
            "写被拒（%s）：%s" % (reason, _GH_WRITE_REASONS.get(reason, reason)),
        )
    sys.stderr.write(
        "即将部署目录 %s → %s（%s）：\n  建库(若缺)+init+commit"
        "+push\n确认？[y/N] " % (dirpath, repo, vis)
    )
    sys.stderr.flush()
    if (sys.stdin.readline() or "").strip().lower() != "y":
        return out(1, "deploy", [], [], "已取消（未确认 y）")
    r = gc.deploy_dir(dirpath, repo, public=vis == "public", audit=_aud)
    if not r.get("ok"):
        return out(1, "deploy", [], [], "部署失败：%s" % r.get("error"))
    return out(
        0,
        "deploy",
        [("已完成", "%s（建库=%s，push=OK）" % (repo, r.get("created")))],
        [],
    )


def _gh_audit_cmd(rest, out):
    """复核审计哈希链并列最近 N 条。断链 rc=2 —— 写面可信度归零，先查人。"""
    try:
        pos, opts = _gh_argv_split(list(rest or []))
        lim = _gh_int(opts.get("limit", "10"), "--limit", 1, 100)
    except ValueError as e:
        return out(1, "audit", [], [], str(e))
    ok, n, bad_i, msg = _gh_audit_verify()
    rows = [("审计文件", _GH_AUDIT), ("哈希链", msg)]
    if ok:
        for r in _gh_audit_rows()[-lim:]:
            rows.append(
                (
                    "%s %s" % (r.get("kind"), r.get("sub")),
                    "%s repo=%s num=%s rc=%s %s"
                    % (
                        time.strftime("%m-%d %H:%M:%S", time.localtime(r.get("ts", 0))),
                        r.get("repo"),
                        r.get("num"),
                        r.get("rc"),
                        r.get("note", ""),
                    ),
                )
            )
        return out(0, "audit", rows, [])
    return out(2, "audit", rows, [], "审计链断（%s）——写面可信度归零，先查人" % msg)


def _gh_exc(gc):
    """gh_client 的失败异常类。没加载成功就返回空元组。

    except 子句是**运行期求值**的：`except _gh_exc(gc)` 里 gc 没绑定时，
    抛异常现场会先炸一个 NameError，把真正的错因盖掉。空元组等于「不接」，
    至少错因能原样出去。
    """
    e = getattr(gc, "GhError", None)
    return (e,) if isinstance(e, type) and issubclass(e, BaseException) else ()


def _gh_slug(pos, what):
    """owner/repo 形状校验（手写，不引 re）。"""
    s = (pos[0] if pos else "").strip()
    if "/" not in s:
        raise ValueError("%s 需要 owner/repo（收到 %r）" % (what, s))
    o, _, r = s.partition("/")
    if not o or not r or "/" in r:
        raise ValueError("%s 不是 owner/repo 形状: %r" % (what, s))
    return s


def cmd_gh(args):
    """GitHub 只读面。rc 口径见 _GH_KIND_RC；sources 里是真实执行的 gh 原文。"""
    argv = [str(a) for a in (getattr(args, "argv", None) or [])]
    fmt = getattr(args, "output_format", "text")
    t0 = time.time()

    def out(code, sub, rows, sources, err=None):
        if fmt == "json":
            rec = {
                "ok": int(code) == 0,
                "exit_code": int(code),
                "action": "gh" + ((" " + sub) if sub else ""),
                "rows": [[l, v] for l, v in rows],
                "sources": list(sources or []),
                "dur": round(time.time() - t0, 2),
                "error": err,
            }
            sys.stdout.write(json.dumps(rec, ensure_ascii=False) + "\n")
        else:
            for l, v in rows:
                sys.stdout.write("%s: %s\n" % (l, v))
            if sources:
                sys.stdout.write("sources: %s\n" % " + ".join(sources))
            if err:
                sys.stderr.write("FAIL: %s\n" % err)
        sys.stdout.flush()
        return int(code)

    if argv and argv[0] in ("-h", "--help", "help"):
        sys.stdout.write(_GH_USAGE)
        return 0
    if not argv:
        sys.stderr.write(_GH_USAGE)
        return 2
    sub, rest = argv[0], argv[1:]
    # gc 先绑 None：except 子句在运行期求值，try 里第一行就炸的话
    # `except _gh_exc(gc)` 会先抛 NameError，把真错因盖掉。
    gc = None
    try:
        gc = _gh_load()
    except Exception as e:  # noqa: BLE001
        # 基础设施错（模块缺失/语法坏）：rc=3，且绝不静默降级成空列表。
        return out(3, sub, [], [], "gh_client 加载失败：%s" % e)
    try:
        pos, opts = _gh_argv_split(rest)
        lim = _gh_int(opts.get("limit", "20"), "--limit", 1, 100)
        if sub == "auth":
            rows, srcs = gc.auth()
        elif sub == "whoami":
            rows, srcs = gc.whoami()
        elif sub == "repos":
            rows, srcs = gc.repos(limit=lim)
        elif sub == "repo":
            rows, srcs = gc.repo(_gh_slug(pos, "repo"))
        elif sub == "issues":
            rows, srcs = gc.issues(
                _gh_slug(pos, "issues"), state=opts.get("state", "open"), limit=lim
            )
        elif sub == "prs":
            rows, srcs = gc.prs(
                _gh_slug(pos, "prs"), state=opts.get("state", "open"), limit=lim
            )
        elif sub == "runs":
            rows, srcs = gc.runs(_gh_slug(pos, "runs"), limit=lim)
        elif sub == "search":
            if not pos:
                raise ValueError("search 需要 <类型> <查询>")
            q = " ".join(pos[1:]).strip()
            if not q:
                raise ValueError("search 需要查询词")
            rows, srcs = gc.search(pos[0], q, limit=lim)
        elif sub in ("issue-create", "issue-comment", "pr-create"):
            return _gh_write_cmd(sub, rest, out)
        elif sub == "audit":
            return _gh_audit_cmd(rest, out)
        elif sub == "repo-create":
            return _gh_repo_create_cmd(rest, out)
        elif sub == "deploy":
            return _gh_deploy_cmd(rest, out)
        else:
            return out(2, sub, [], [], "未知 gh 子功能 %r\n%s" % (sub, _GH_USAGE))
    except _gh_exc(gc) as e:
        # GhError 的 kind 就是 rc 的真源；不认 kind 一律按远端失败 1。
        kind = getattr(e, "kind", None) or "rc"
        return out(_GH_KIND_RC.get(kind, 1), sub, [], [], "%s: %s" % (kind, e))
    except ValueError as e:
        return out(2, sub, [], [], "%s" % e)
    return out(0, sub, rows, srcs)


def cmd_term(args):
    """非交互跑一条只读命令：转发 POST /__term，回服务端原话与真实 rc。

    与 tui 的终端面共用同一个 /__term 白名单路由 —— 唯一的区别是这一条
    不进 raw 模式，可以直接被管道、CI、脚本调用。rc 一律取服务端回执里的
    那个值，不用本地 subprocess 的退出码冒充（跑的都不是同一个进程）。
    """
    argv = list(args.argv or [])
    if not argv:
        print(
            "FAIL: 零参数 = 什么都没跑。用法: agentos.py term <命令> [参数…]"
            "（如 agentos.py term ls -1 tools）",
            file=sys.stderr,
        )
        return 2
    base = base_url(args)
    try:
        st, j = http_json(base + "/__term", {"argv": argv}, timeout=45)
    except Exception as e:  # noqa: BLE001
        print("FAIL: %s 不可达：%s" % (base, e), file=sys.stderr)
        return 1
    if st != 200:
        print(
            "FAIL: HTTP=%s %s" % (st, _short(j.get("error") or "", 160)),
            file=sys.stderr,
        )
        return 1
    out = j.get("out") or ""
    if out:
        sys.stdout.write(out if out.endswith("\n") else out + "\n")
    if j.get("truncated"):
        print("… 输出被服务端截断（上限 32 KiB），不是全量", file=sys.stderr)
    rc = j.get("rc")
    if rc is None:
        # 命令压根没跑起来（超时/起不来）：如实说，不给 0。
        print(
            "FAIL: 命令未执行：%s" % (j.get("error") or "服务端未回 rc"),
            file=sys.stderr,
        )
        return 1
    print("rc=%s · %ss · argv=%s" % (rc, j.get("dur"), " ".join(j.get("argv") or argv)))
    return 0 if rc == 0 else 1


# ── -p/--print：headless 一次性模式（CI / 脚本 / 管道）────────────
# 退出码契约（写死；CI 直接依赖，改语义必须先改门）：
#   0  动作真跑了，且服务端回执 rc == 0
#   1  动作真跑了，但服务端回执 rc != 0（动作自己红）
#   2  用法错：空需求 / 无子命令且非 TTY / -p 与子命令混用
#   3  基础设施错：服务不可达、__health 不健康、路由不成、
#      ok:false（单飞冲突/被中止）、服务端没回 rc
# headless 永不进 raw 模式、永不着色（本 CLI 全量零 ANSI）：stdout 只承载
# 结果，告警全走 stderr，--output-format json 时 stdout 恰好一行可 json.loads。
PRINT_TIMEOUT = 300.0
SESSION_DIR = os.path.join(ROOT, "work", "agent_sessions")


def _act_tmo(base, action, budget):
    """问服务端：这个动作的进程组硬上限是几秒（ACTIONS 里的 tmo）。

    取不到就返回 None，调用方退回用户给的 budget。为什么必须问而不是写死：
    2026-10-10 实测 livecheck 跑了 11m28s，而 --timeout 默认 300s ——
    客户端比服务端先撤，服务端的进度还在流，客户端已经报「执行失败」退出。
    """
    try:
        st, j = http_json(
            base + "/__act", {"action": "__list"}, timeout=min(budget, 15.0)
        )
    except Exception:  # noqa: BLE001
        return None
    if st != 200 or j.get("ok") is not True:
        return None
    for it in j.get("actions") or []:
        if isinstance(it, dict) and it.get("name") == action:
            t = it.get("tmo")
            if isinstance(t, (int, float)) and t > 0:
                return float(t)
    return None


class _PrintSink(object):
    """一次性模式的输出面。

    text：边跑边吐、每行带相对秒戳 —— egate 要跑 600s，跑完才给等于没给。
    json：只攒不吐，末行一次性给完整对象，否则 CI 没法解析 stdout。
    """

    def __init__(self, fmt, t0):
        self.fmt = fmt or "text"
        self.t0 = t0
        self.lines = []

    def line(self, s):
        s = str(s).rstrip("\r\n")
        self.lines.append(s)
        if self.fmt != "json":
            sys.stdout.write("[%6.1fs] %s\n" % (time.time() - self.t0, s))
            sys.stdout.flush()

    def warn(self, s):
        sys.stderr.write(str(s).rstrip("\r\n") + "\n")
        sys.stderr.flush()

    def finish(self, rec):
        if self.fmt == "json":
            sys.stdout.write(json.dumps(rec, ensure_ascii=False) + "\n")
        else:
            if rec.get("error"):
                self.warn("FAIL: %s" % rec["error"])
            if rec.get("action"):
                sys.stdout.write(
                    "rc=%s · exit=%s · %.1fs · action=%s · how=%s · run=%s\n"
                    % (
                        rec.get("action_rc"),
                        rec.get("exit_code"),
                        rec.get("dur", 0.0),
                        rec.get("action"),
                        rec.get("how") or "-",
                        rec.get("run") or "-",
                    )
                )
        sys.stdout.flush()
        return int(rec.get("exit_code", 3))


def _new_session_id():
    return "cli-%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:4])


def _session_files():
    """只认 cli-*.jsonl —— 本 CLI 写的历史，不接管别人的命名空间。"""
    if not os.path.isdir(SESSION_DIR):
        return []
    return [
        os.path.join(SESSION_DIR, n)
        for n in sorted(os.listdir(SESSION_DIR))
        if n.startswith("cli-") and n.endswith(".jsonl")
    ]


def _session_load(path):
    recs = []
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if raw:
                recs.append(json.loads(raw))
    return recs


def _session_write(rec):
    """把一次 headless 执行落成历史。写不进去只警告、不改 rc ——
    历史是附加价值，不是裁决依据，不许因为它把绿的说成红的。"""
    try:
        os.makedirs(SESSION_DIR, exist_ok=True)
        path = os.path.join(SESSION_DIR, rec["id"] + ".jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return path
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("warn: 会话历史没写下（%s）\n" % str(e)[:120])
        return None


# ── -p 只读路由面：人话 → 本项目已知只读能力 ─────────────────────
# 为什么必须有这一层：/__act/resolve 只认 8 个**执行面**动作。用户问
# 「现在什么状态」「看一眼能力地图」时它只会 no-match，而这句话要的根本不是
# 执行，是读。以前这里直接 rc=3，用户看到的是「没有这个动作」——问句被回答说
# 成了另一件事。
# 铁律：命中后一律复用 TUI 的取数函数（LOADERS / panel），CLI 与终端读同一份
# 数据；命不中就退回执行面，绝不猜一个面凑答案。
_Q_ROUTES = (
    ("projects", "项目列表", ("project", "proj", "项目", "空间")),
    ("goal", "目标台账", ("goal", "目标", "台账")),
    ("capmap", "能力地图", ("capmap", "capability", "能力")),
    ("rounds", "轮次", ("round", "轮次", "回合", "历史")),
    ("matrix", "阶段矩阵", ("matrix", "阶段", "矩阵", "进度")),
    ("ledger", "证据账本", ("ledger", "证据", "账本")),
    ("engine", "引擎", ("engine", "引擎", "门链")),
    ("agent", "Agent 台", ("agent", "智能体")),
    ("retro", "复盘台", ("retro", "复盘", "回顾")),
    ("ws", "工作区", ("ws", "工作区", "目录树", "文件树")),
    ("file", "文件", ("file", "文件", "代码")),
    (
        "term",
        "终端命令",
        ("term", "terminal", "shell", "终端", "命令", "跑一下", "运行", "执行"),
    ),
    # gh 必须排在 status 之前：status 的兜底词里有「总览/概览」，而
    # 「看一下 github 仓库」不是要总览。gh 放这里还因为它只读、零参数依赖，
    # 命不中时不参加兜底竞争。
    (
        "gh",
        "GitHub",
        ("gh", "github", "仓库", "repo", "issue", "pr", "pull request", "star"),
    ),
    # status 放最后：它带 总览/概览/怎么样 这类兜底词，排前面会把
    # 「看一眼能力地图」也抢走。兜底面只该在更具体的一个都没命中时接。
    (
        "status",
        "状态/健康",
        (
            "status",
            "health",
            "健康",
            "状态",
            "怎么样",
            "咋样",
            "正常",
            "在线",
            "alive",
            "总览",
            "概览",
            "驾驶舱",
        ),
    ),
)


def _q_route(q):
    """人话 → (route_id, 路由名)。命不中返回 (None, None)。

    计分：关键词命中数，平局按声明序（越具体的越靠前）。零命中就返回 None，
    不许「猜一个最接近的」——猜对那次像智能，猜错那次就是把别的面数据
    当成答案，比 rc=3 难看得多。
    """
    low = q.lower()
    best, best_n, best_name = None, 0, None
    for rid, name, kws in _Q_ROUTES:
        n = 0
        for k in kws:
            if k.lower() in low:
                n += 1
        if n > best_n:
            best, best_n, best_name = rid, n, name
    return best, best_name


def _q_quoted(q):
    """抽出引号/反引号里的片段。不引 re：这点活儿手写更快也更可控。"""
    out = []
    for a, b in (("`", "`"), ('"', '"'), ("'", "'")):
        i = q.find(a)
        while i >= 0:
            j = q.find(b, i + 1)
            if j < 0:
                break
            out.append(q[i + 1 : j].strip())
            i = q.find(a, j + 1)
    return out


def _q_row(row):
    """取数行 → (label, value, meta)。

    TUI 的取数函数里两种行都有：`("健康", "ok")` 和 `("上级", "…", {...})`。
    这里统一成三元组，免得每个调用点都要 try/except 解包 —— 那种 try 一多，
    真实的数据错误就会被一起吞掉。
    """
    row = list(row) + [None, None]
    return row[0], row[1], row[2]


def _q_term_argv(q, allow):
    """从问句里取出真要跑的只读命令 argv，取不到返回 None（不许猜）。

    三条路都由严到宽：引号里的整条 → 问句本身就是命令 → 动词后面那段。
    每条都以「首 token 在服务端白名单里」收口，所以猜不出白名单外的命令。
    """
    allow = set(allow or [])
    if not allow:
        return None
    for frag in _q_quoted(q):
        toks = frag.split()
        if toks and toks[0] in allow:
            return toks
    toks = q.strip().split()
    if toks and toks[0] in allow:
        return toks
    # "跑一下 ls -1 tools" / "term ls" / "执行一下 ./x"：前几个词里找动词，
    # 动词之后第一个在白名单里的 token 起，整段当命令。
    for i in range(1, min(len(toks), 4)):
        if toks[i] in allow:
            return toks[i:]
    return None


def _q_projects(base, timeout):
    """项目列表没有对应 TUI 取数函数，这里自己读 GET /__projects。"""
    st, j = http_json(base + "/__projects", timeout=min(timeout, 20.0))
    if st != 200 or j.get("ok") is not True:
        raise RuntimeError("/__projects 不健康（HTTP=%s）" % st)
    rows = [
        ("项目数", str(j.get("n_projects")), {"sec": 1}),
        ("当前", "%s / 默认 %s" % (j.get("active"), j.get("default"))),
    ]
    for p in j.get("projects") or []:
        f = p.get("facts") or {}
        rows.append(
            (
                p.get("key") or "?",
                "%s · %s · 顶层 %s 项 · 文件 %s 项%s"
                % (
                    p.get("label") or "-",
                    p.get("root") or "-",
                    f.get("n_top"),
                    f.get("n_files"),
                    "（已截断）" if f.get("truncated") else "",
                ),
                {"kind": "no"} if p.get("ok") is not True else None,
            )
        )
    return rows, "GET /__projects（只读）", {}


# 只读路由 id → 取数函数。除 projects 外全部复用 TUI 的 loader。
_Q_VIEW_ROUTE = {
    "status": "总览",
    "rounds": "轮次",
    "matrix": "阶段矩阵",
    "ledger": "证据账本",
    "engine": "引擎",
    "agent": "Agent 台",
    "goal": "目标台",
    "retro": "复盘台",
    "capmap": "能力地图",
    "ws": "工作区",
    "file": "文件",
}


def _q_fetch(rid, base, timeout):
    if rid == "projects":
        return _q_projects(base, timeout)
    if rid == "gh":
        return _q_gh()
    name = _Q_VIEW_ROUTE.get(rid)
    if not name or name not in VIEWS:
        raise RuntimeError("只读路由 %s 没有对应视图" % rid)
    return LOADERS[VIEWS.index(name)](base)


def _q_gh():
    """GitHub 只读面：whoami + 仓库列表。取不到就抛，不许空列表冒充有数据。"""
    gc = _gh_load()
    rows, srcs = gc.whoami()
    src = " + ".join(srcs)
    try:
        rrows, rsrc = gc.repos(limit=15)
    except Exception as e:  # noqa: BLE001
        # 账号能认证而仓库列不出来（token Scope 不够）≠ 整个 gh 面不可用：
        # 这一条如实做成一行红，而不是让整句问话 rc=3。
        return (list(rows) + [("仓库列表", "取不到：%s" % e)], src, {})
    return (list(rows) + list(rrows), " + ".join(list(srcs) + list(rsrc)), {})


def cmd_agent(args):
    """`agentos agent`：argv 原样转发给 tools/agent_core.py 的内核。

    为什么是转发而不是在这里把内核的参数重新声明一遍：内核的 CLI
    （-p/--mode/--root/--max-steps/--stream/--yes/--check）是随内核演进
    的，agentos 里抄第二份解析器，那天内核加了参数而这边没跟上，用户敲
    出来的是「用法错」—— 把「能力还在」谎成「你不会用」。所以这里用
    REMAINDER 收原始 argv，调 agent_core.main() 并把内核真实的退出码交
    回去（0=给出了最终回答；2=用法/预算；3=基础设施）。`agentos agent
    --help` 打的是内核自己维护的那份帮助，永远不会和内核对不上。
    """
    tools = os.path.join(ROOT, "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    try:
        import agent_core
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("FAIL: 载入 agent 内核失败：%s\n" % e)
        return 3
    argv = list(args.argv or [])
    try:
        return int(agent_core.main(argv) or 0)
    except KeyboardInterrupt:
        sys.stderr.write("\n已中止\n")
        return 130


def cmd_config(args):
    """`agentos config`：读写 ~/.agentos/config.toml。

    子命令：
      set <key> <value>   写配置（key 如 llm.api_key / llm.provider / llm.model）
      get <key>           读配置（不存在返回 rc=1）
      show                打印完整配置（脱敏 api_key）

    为什么需要这个命令：用户不想每次 export 环境变量，也不想手动编辑
    TOML。`agentos config set llm.api_key <key>` 一条命令搞定，与
    `claude config set` 手感一致。
    """
    tools = os.path.join(ROOT, "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    try:
        import agent_core
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("FAIL: 载入 agent 内核失败：%s\n" % e)
        return 3

    sub = getattr(args, "config_cmd", None)
    if sub == "set":
        key = getattr(args, "key", "")
        val = getattr(args, "value", "")
        if not key or not val:
            sys.stderr.write(
                "FAIL: config set 需要 key 和 value\n"
                "  用法: agentos config set llm.api_key <key>\n"
            )
            return 2
        parts = key.split(".")
        if len(parts) != 2:
            sys.stderr.write("FAIL: key 格式应为 table.field，如 llm.api_key\n")
            return 2
        table, field = parts
        # 校验（R2 G1/G1b，能力矩阵验证揭出）：①写面开关只认字面 true/false
        # —— "yes"/"1"/"True" 一律拒，一次手滑不许悄悄提权（SEC E1）；
        # ②表只放行已知集合，任意键会把用户配置当垃圾场。
        if table not in ("llm", "gh", "agent"):
            sys.stderr.write(
                "FAIL: config set 只放行 llm.* / gh.* / agent.* 表"
                "（收到 %s.*）；新表请先进代码白名单\n" % table
            )
            return 2
        if table == "gh" and field == "write_enabled" and val not in ("true", "false"):
            sys.stderr.write(
                "FAIL: gh.write_enabled 只认字面 true / false"
                "（收到 %r）——写面开关不接受模糊值\n" % val
            )
            return 2
        if table == "llm" and field == "no_fallback" and val not in ("true", "false"):
            sys.stderr.write(
                "FAIL: llm.no_fallback 只认字面 true / false"
                "（收到 %r）——回退开关不接受模糊值\n" % val
            )
            return 2
        if table == "agent":
            # 安全闸开关（W10 自由面）：只放行 plan_lock / mode_gate / cmd_gate
            # 三键，值只认字面 true/false，且**存成布尔**——TOML 落盘为
            # true/false 真值，读侧只认字面布尔 True 才上锁（字符串 "true"
            # 不上锁、缺键=自由）。默认自由面，显式 true 才把闸摆回去。
            if field not in ("plan_lock", "mode_gate", "cmd_gate"):
                sys.stderr.write(
                    "FAIL: agent 表只放行 plan_lock / mode_gate "
                    "/ cmd_gate（收到 agent.%s）\n" % field
                )
                return 2
            if val not in ("true", "false"):
                sys.stderr.write(
                    "FAIL: agent.%s 只认字面 true / false（收到 %r）\n" % (field, val)
                )
                return 2
        cfg = agent_core._read_agentos_config()
        if table not in cfg or not isinstance(cfg[table], dict):
            cfg[table] = {}
        if table == "agent":
            cfg[table][field] = val == "true"  # 存布尔，不存字符串
        else:
            cfg[table][field] = val
        # 用户说「你直接把 opencode 给我接入」—— 设 api_key 时如果 provider
        # 未设，自动补 opencode，这样 `agentos config set llm.api_key <key>`
        # 一条命令就能用 opencode，不必再单独设 provider。
        if table == "llm" and field == "api_key":
            llm = cfg.get("llm") or {}
            if not (llm.get("provider") or "").strip():
                llm["provider"] = "opencode"
        ok, err = agent_core._write_agentos_config(cfg)
        if not ok:
            sys.stderr.write("FAIL: 写配置失败：%s\n" % err)
            return 3
        sys.stderr.write(
            "OK: %s.%s 已写入 %s\n" % (table, field, agent_core._agentos_config_path())
        )
        return 0
    if sub == "get":
        key = getattr(args, "key", "")
        if not key:
            sys.stderr.write(
                "FAIL: config get 需要 key\n  用法: agentos config get llm.api_key\n"
            )
            return 2
        parts = key.split(".")
        if len(parts) != 2:
            sys.stderr.write("FAIL: key 格式应为 table.field，如 llm.api_key\n")
            return 2
        table, field = parts
        cfg = agent_core._read_agentos_config()
        val = (cfg.get(table) or {}).get(field)
        if val is None:
            sys.stderr.write("未配置：%s\n" % key)
            return 1
        sys.stdout.write("%s\n" % val)
        return 0
    if sub == "show":
        cfg = agent_core._read_agentos_config()
        if not cfg:
            sys.stderr.write(
                "(空) %s 下还没有配置\n" % agent_core._agentos_config_path()
            )
            return 0
        for table, fields in sorted(cfg.items()):
            if not isinstance(fields, dict):
                continue
            sys.stdout.write("[%s]\n" % table)
            for k, v in sorted(fields.items()):
                if "key" in k.lower() or "token" in k.lower():
                    sys.stdout.write('%s = "<已配置，用 config get 查看>"\n' % k)
                else:
                    sys.stdout.write("%s = %s\n" % (k, v))
            sys.stdout.write("\n")
        return 0
    sys.stderr.write(
        "FAIL: config 需要子命令 set/get/show\n"
        "  用法: agentos config set llm.api_key <key>\n"
        "        agentos config get llm.api_key\n"
        "        agentos config show\n"
    )
    return 2


def cmd_print(args):
    """headless 一次性模式：人话 → allowlist 动作 → 流式执行 → 结构化结果。


    与 tui 的 Agent 台同源：路由走 GET /__act/resolve，执行走 POST /__act，
    进度走 GET /__act/stream SSE。白名单 / 单飞 / 中止全在服务端，
    CLI 不另立一套，也不会因为自己起进程而绕开它们。
    """
    q = (getattr(args, "request", None) or "").strip()
    if not q:
        sys.stderr.write(
            "FAIL: -p/--print 需要一句需求。空需求 = 什么都没干，不许当成成功。\n"
            '  用法: agentos -p "跑一下 livecheck" [--output-format json]\n'
        )
        return 2
    base = base_url(args)
    # argparse 用 SUPPRESS 当 --timeout 的默认值：没显式给时 args 上根本没有这个
    # 属性。所以「用户显式收窄了预算」与「用户没说话」在这里是可分的 —— 而这两件
    # 事的等待策略必须不同（见下面 eff 的计算）。
    given_to = getattr(args, "timeout", None)
    timeout = float(given_to if given_to is not None else PRINT_TIMEOUT)
    t0 = time.time()
    sink = _PrintSink(getattr(args, "output_format", "text"), t0)
    sid = _new_session_id()

    def emit(
        code,
        msg,
        action=None,
        how=None,
        run=None,
        action_rc=None,
        ok=None,
        route=None,
        answer=None,
        sources=None,
    ):
        rec = {
            "id": sid,
            "started": time.strftime("%Y-%m-%d %H:%M:%S"),
            "request": q,
            "action": action,
            "how": how,
            "run": run,
            "action_rc": action_rc,
            "exit_code": int(code),
            "dur": round(time.time() - t0, 2),
            "lines": sink.lines,
            "error": msg,
        }
        # query 是 request 的契约别名：json 消费者（CI / 脚本）按契约 4 找
        # query，老会话记录找 request。两个键写同一份值，比改键名安全。
        rec["query"] = q
        # 契约 4 要求 json 至少含 ok/query/answer/sources。ok 不写死在 dict
        # 字面量里：它必须由退出码推，写死就会出现「exit_code=3 而 ok=true」
        # 这种自相矛盾的记录——那正是本项目一路在治的假绿。
        if ok is None:
            ok = int(code) == 0
        rec["ok"] = bool(ok)
        rec["route"] = route
        rec["answer"] = answer if answer is not None else (" ".join(sink.lines)[:600])
        rec["sources"] = list(sources or [])
        _session_write(rec)
        return sink.finish(rec)

    # 1) 服务健康：不健康就没有"真机"可言，直接基础设施错
    try:
        st, h = http_json(base + "/__health", timeout=min(timeout, 20.0))
    except Exception as e:  # noqa: BLE001
        # 这一条也在执行面：不标 route 的话，事后翻会话记录时分不清
        # 「执行面在健康检查就倒了」和「只读面在健康检查就倒了」。
        return emit(
            3, "%s 不可达：%s" % (base, e), route="action", sources=["GET /__health"]
        )
    if st != 200 or h.get("ok") is not True:
        return emit(
            3,
            "%s/__health 不健康（HTTP=%s ok=%s）" % (base, st, h.get("ok")),
            route="action",
            sources=["GET /__health"],
        )

    # 1.5) 只读路由面：先问「这句是不是在要一份现成的读结果」。
    # /__act/resolve 只认 8 个**执行面**动作，用户问「现在什么状态」「看一眼
    # 能力地图」时它只会 no-match —— 但这类问题根本不该落到执行面，它们要的是
    # 读。命中就直接复用 TUI 那十三个取数函数：CLI 里读到的与终端里看到的是
    # 同一份数据，不存在第二份真相。命不中再退执行面，原行为一字不变。
    # 终端命令是特例，它不看关键词看命令名："ls -1 tools" 里一个路由关键词
    # 都没有，但它确实是本 CLI 能答的一句。所以先按白名单抽一次命令。
    rid, rname = _q_route(q)
    argv = None
    if rid in (None, "term"):
        argv = _q_term_argv(q, _panel(base).allowlist() or [])
        if argv is not None:
            rid, rname = "term", "终端命令"
    if rid == "term" and argv is None:
        # 关键词看着像终端面、却抽不出命令名 —— 典型是「跑一下 livecheck」，
        # 那句话要的是**执行面**。这里绝不能判死，否则一句话被回答说成
        # 「没有这个动作」。清掉 rid，让它继续往下走执行面。
        rid, rname = None, None
    if rid == "term":
        pnl = _panel(base)
        pnl.run_cmd(" ".join(argv))
        last = pnl.hist[-1] if pnl.hist else None
        if not last or last[2] is None:
            return emit(
                3,
                "命令未执行（服务端未回 rc，不给 0）",
                route="term",
                sources=["POST /__term"],
            )
        _t, _cmd, rc, dur, out, note = last
        body = [ln for ln in (out or "").splitlines() if ln.strip()]
        shown = body[-40:]
        for ln in shown:
            sink.line(ln)
        if len(body) > len(shown):
            sink.line("…（共 %d 行，只显示最后 %d 行）" % (len(body), len(shown)))
        if note:
            sink.warn(note)
        return emit(
            0 if rc == 0 else 1,
            None,
            route="term",
            action_rc=rc,
            sources=["POST /__term"],
            answer="rc=%s · %ss · %s" % (rc, dur, " ".join(argv)),
        )
    if rid:
        try:
            rows, src, _extra = _q_fetch(rid, base, timeout)
        except Exception as e:  # noqa: BLE001
            return emit(3, "只读路由 %s 取数失败：%s" % (rid, e), route=rid)
        if not rows:
            return emit(
                3, "只读路由 %s 一行数据都没取到（不拿空屏当答案）" % rid, route=rid
            )
        norm = [_q_row(r) for r in rows]
        for label, val, _meta in norm:
            sink.line("%s: %s" % (label, val))
        return emit(
            0,
            None,
            route=rid,
            sources=[src],
            answer=" ".join("%s=%s" % (l, v) for l, v, _ in norm)[:600],
        )
    # 2) 路由：人话 → allowlist 动作名。路由不成要把候选摊开，不许静默猜
    try:
        st, rj = http_json(
            base + "/__act/resolve?q=" + urllib.parse.quote(q),
            timeout=min(timeout, 20.0),
        )
    except Exception as e:  # noqa: BLE001
        return emit(3, "路由面不可达：%s" % e)
    if st != 200 or rj.get("ok") is not True:
        cands = " ".join(rj.get("candidates") or []) or "(无)"
        allow = " ".join(rj.get("allowlist") or []) or "(空册 = 判红)"
        # 两侧候选都摊开：只读面有哪些、执行面有哪些。以前只报执行面，
        # 用户看着「我明明问了状态」却被告知没有这个动作。
        rc_ids = [r[0] for r in _Q_ROUTES]
        return emit(
            3,
            "「%s」路由不成（%s）\n  candidates: %s\n  allowlist : %s"
            "\n  只读面可问：%s"
            % (q, rj.get("reason") or "no-match", cands, allow, " ".join(rc_ids)),
        )
    action = rj.get("action")
    how = rj.get("how")
    sink.line("路由 → %s（%s）" % (action, how))

    box = {}

    tmo = _act_tmo(base, action, timeout)
    # 等待预算分两种情况。以前只写 max() 一条，注释却声称「用户显式给更小的值时
    # 不放大」—— 代码和注释互相打脸，而 --timeout 在生产上等于没生效。
    #   没显式给 --timeout：按服务端这个动作的硬上限 + 120s 余量。
    #     2026-10-10 实测 livecheck 跑了 11m28s，默认 300s 会先于服务端撤。
    #   显式给了 --timeout N：听用户的，N 秒后撤并回 124。
    if given_to is not None:
        eff = max(float(given_to), 1.0)
    else:
        eff = (tmo + 120.0) if tmo else float(timeout)
    deadline = time.time() + eff

    # 3) 先起 SSE pump 再起执行：反过来会丢动作最前几行进度
    evq = queue.Queue()
    stop = threading.Event()

    def pump():
        try:
            req = urllib.request.Request(base + "/__act/stream", method="GET")
            with _OPENER.open(req, timeout=eff) as r:
                for raw in r:
                    if stop.is_set():
                        break
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    if line.startswith("data: "):
                        body = line[6:].strip()
                        if body and not body.startswith("__RUN__"):
                            evq.put(("line", body))
        except Exception as e:  # noqa: BLE001
            evq.put(("warn", "SSE 中断：%s" % str(e)[:120]))
        finally:
            evq.put(("eof", None))

    threading.Thread(target=pump, daemon=True).start()

    def run_act():
        # 409「执行面忙」不是失败，是「此刻有人在跑」。一次性语义下本该等：
        # 立刻退 3 的话，用户看到的是「livecheck 没跑成：busy」——而 livecheck
        # 压根没被跑过。等锁期间持续打进度行，不让人以为卡死了。
        while True:
            try:
                st, j = http_json(base + "/__act", {"action": action}, timeout=eff)
            except Exception as e:  # noqa: BLE001
                box["err"] = str(e)[:160]
                return
            if st == 409 and j.get("error") == "busy" and time.time() < deadline:
                el = j.get("elapsed")
                sink.line(
                    "[等锁] 执行面正忙：%s 已跑 %s（预算 %ss）· 本端再等 %ds"
                    % (
                        j.get("running") or "-",
                        ("%.0fs" % el) if isinstance(el, (int, float)) else "?",
                        j.get("tmo") or "?",
                        max(1, int(deadline - time.time())),
                    )
                )
                time.sleep(3.0)
                continue
            box["st"], box["j"] = st, j
            return

    rt = threading.Thread(target=run_act, daemon=True)
    rt.start()
    while rt.is_alive() and time.time() < deadline:
        try:
            kind, val = evq.get(timeout=0.2)
        except queue.Empty:
            continue
        if kind == "line":
            sink.line(val)
        elif kind == "warn":
            sink.warn(val)
    stop.set()
    while True:  # 排空残余进度行，不丢尾帧
        try:
            kind, val = evq.get_nowait()
        except queue.Empty:
            break
        if kind == "line":
            sink.line(val)
        elif kind == "warn":
            sink.warn(val)
    rt.join(timeout=5)

    if "err" in box:
        # route/sources 必须贯穿执行面的每一条出口：json 记录里
        # route:null 的失败行看不出它是走执行面还是走只读面，事后复盘
        # 就没法把「路由不成」和「跑成了但 rc≠0」分开。
        return emit(
            3,
            "POST /__act 失败：%s" % box["err"],
            action=action,
            how=how,
            route="action",
            sources=["POST /__act"],
        )
    if "st" not in box:
        # 124 = 超时，与 GNU timeout 同口径（契约第 5 条）。原来这里和
        # "POST 失败"共用 3：CI 拿不到「该重试」与「该报错」的区分，
        # 而且 --timeout 这条参数在生产上等于没生效。
        if rt.is_alive():
            return emit(
                124,
                "执行超时（--timeout %.0fs）未回执，CLI 已撤离"
                "（服务端可能仍在跑，用 agentos sessions 查状态）" % timeout,
                action=action,
                how=how,
            )
        # 124 与 3 分开：CI 要靠退出码判断该不该重试。
        return emit(
            3,
            "执行未回执（既没超时也没结果，不许猜 0）",
            action=action,
            how=how,
            route="action",
            sources=["POST /__act", "GET /__act/stream"],
        )
    st, j = box["st"], box["j"]
    # tail 在服务端一直是**行表**（serve.py:_run_argv 返回 tail_lines，
    # /__act 原样回传，keep=12），从来不是字符串。这里当成字符串的第一个
    # 后果是每个真动作都在最后一步 AttributeError 崩掉：跑完了、该报 rc 了，
    # 用户看到的是一段 traceback 和 rc=1 —— 而 CLI 的全部分价值就在这个 rc 上。
    # 两种形状都接：字符串按行拆，行表逐行打，别的形状当作没有。
    _t = j.get("tail")
    if isinstance(_t, str):
        tail = [x for x in _t.splitlines() if x.strip()]
    elif isinstance(_t, (list, tuple)):
        tail = [str(x) for x in _t if str(x).strip()]
    else:
        tail = []
    if st != 200 or j.get("ok") is not True:
        for ln in tail:
            sink.line(ln)
        # 服务端 rc=124 是「进程组被超时杀掉」，与 GNU timeout 同口径。它必须
        # 用 124 出去：CI 与人都要靠这一个数字区分「该重试/该加预算」和
        # 「这个动作本身判红」。混在 3 里，超时会被当成永久失败。
        if j.get("rc") == 124:
            return emit(
                124,
                "%s 在服务端超时被杀（预算 %ss）：%s"
                % (action, j.get("tmo") or "?", (tail[-1] if tail else "no tail")),
                action=action,
                how=how,
                run=j.get("run"),
                route="action",
                sources=["POST /__act"],
            )
        return emit(
            3,
            "%s 没跑成：%s" % (action, j.get("error") or ("HTTP=%s" % st)),
            action=action,
            how=how,
            run=j.get("run"),
            route="action",
            sources=["POST /__act"],
        )
    arc = j.get("rc")
    if arc is None:
        # 服务端没给 rc：按"没跑成"算。猜 0 就是拿未验证当通过。
        for ln in tail:
            sink.line(ln)
        return emit(
            3,
            "服务端没回 rc，不许猜 0",
            action=action,
            how=how,
            run=j.get("run"),
            route="action",
            sources=["POST /__act"],
        )
    for ln in tail:
        sink.line(ln)
    if arc == 124:
        return emit(
            124,
            "%s 在服务端超时被杀（预算 %ss）" % (action, j.get("tmo") or "?"),
            action=action,
            how=how,
            run=j.get("run"),
            action_rc=arc,
            route="action",
            sources=["POST /__act"],
        )
    return emit(
        0 if arc == 0 else 1,
        None,
        action=action,
        how=how,
        run=j.get("run"),
        action_rc=arc,
        route="action",
        sources=["POST /__act", "GET /__act/resolve", "GET /__act/stream"],
    )


def _latest_agent_sid(root):
    """最近一个 agent-* 会话 id（无则空串）。"""
    sess = os.path.join(root, "work", "agent_sessions")
    try:
        cands = sorted(
            n
            for n in os.listdir(sess)
            if n.startswith("agent-") and n.endswith(".jsonl")
        )
    except OSError:
        return ""
    return cands[-1][: -len(".jsonl")] if cands else ""


def cmd_steer(args):
    """老板监督指令：投给一个会话（跑着的 agent 在步骤边界消费）。

    只配送不裁决：agent 把它注入为 [老板指令] 消息；执行与否仍走模型与
    该会话的审批矩阵 —— 监督通道不是绕过写锁的后门。--last 选最近会话。
    """
    av = [str(x) for x in (args.argv or [])]
    if not av:
        sys.stderr.write("用法: agentos steer <会话id|--last> <指令文本>\n")
        return 2
    sid = av[0]
    text = " ".join(av[1:]).strip()
    if sid == "--last":
        sid = _latest_agent_sid(ROOT)
        if not sid:
            sys.stderr.write("没有 agent-* 会话可投\n")
            return 1
    if not text:
        sys.stderr.write("指令文本为空\n")
        return 2
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import steer as _steer

    ok, err = _steer.push(os.path.join(ROOT, "work", "steer"), sid, text)
    if not ok:
        sys.stderr.write("投递失败：%s\n" % err)
        return 1
    sys.stdout.write("已投递 → %s（下一步骤边界注入）\n" % sid)
    return 0


def cmd_leader(args):
    """`agentos leader`：argv 原样转发给 tools/leader.py（仓内指挥官）。

    与 cmd_agent 同式：动作面（plan/approve/run/retro）随 leader 演进，
    在这里抄第二份解析器迟早漂移；REMAINDER 转发把「宣称支持的面」与
    「真能跑的面」绑成同一份代码。
    """
    tools = os.path.join(ROOT, "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    try:
        import leader
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("FAIL: 载入 leader 失败：%s\n" % e)
        return 3
    try:
        return int(leader.main(list(args.argv or [])) or 0)
    except KeyboardInterrupt:
        sys.stderr.write("已中断\n")
        return 130


def cmd_watch(args):
    """老板监督面：一屏看清计划/指令/会话尾（TTY 循环刷新，管道打一份）。"""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import watch as _watch

    sid = getattr(args, "sid", "") or _latest_agent_sid(ROOT)
    if not sid:
        sys.stderr.write("没有 agent-* 会话；也可 --sid 显式指定\n")
        return 1
    if getattr(args, "once", False) or not sys.stdout.isatty():
        sys.stdout.write(_watch.render(_watch.snapshot(ROOT, sid)) + "\n")
        return 0
    try:
        while True:
            sys.stdout.write(
                "\x1b[H\x1b[2J" + _watch.render(_watch.snapshot(ROOT, sid)) + "\n"
            )
            sys.stdout.flush()
            time.sleep(1.0)
    except KeyboardInterrupt:
        return 0


def cmd_sessions(args):
    files = _session_files()
    if not files:
        print("(空) %s 下还没有 cli-*.jsonl 会话历史" % SESSION_DIR)
        return 0
    print(
        "%-28s %-8s %-16s %4s %5s %4s  %s"
        % ("id", "时刻", "动作", "rc", "exit", "行", "首条请求")
    )
    for path in files:
        stem = os.path.basename(path)[: -len(".jsonl")]
        try:
            recs = _session_load(path)
        except Exception as e:  # noqa: BLE001
            print("%-28s (读不动: %s)" % (stem[:28], str(e)[:60]))
            continue
        rec = recs[-1] if recs else {}
        # 首条用户请求预览（蒸馏自 opencode /sessions 的可读列表）：
        # 让「挑会话恢复」不用逐个 open。只取 40 列，纯读。
        prev = ""
        for ln in rec.get("lines") or []:
            if not isinstance(ln, dict):
                continue  # 历史账里混有非 dict 行（事故会话实证）
            txt = str(ln.get("text") or ln.get("out") or "").strip()
            if txt:
                prev = " ".join(txt.split())[:40]
                break
        print(
            "%-28s %-8s %-16s %4s %5s %4s  %s"
            % (
                str(rec.get("id") or stem)[:28],
                str(rec.get("started") or "-")[-8:],
                str(rec.get("action") or "-")[:16],
                rec.get("action_rc"),
                rec.get("exit_code"),
                len(rec.get("lines") or []),
                prev,
            )
        )
    return 0


def cmd_resume(args):
    sid = (getattr(args, "session", "") or "").strip()
    if not sid:
        sys.stderr.write("FAIL: resume 需要会话 id（agentos sessions 列全部历史）\n")
        return 2
    hit = None
    for p in _session_files():
        if os.path.basename(p)[: -len(".jsonl")] == sid:
            hit = p
            break
    if hit is None:
        sys.stderr.write("FAIL: 找不到会话 %s（agentos sessions 看可用 id）\n" % sid)
        return 2
    try:
        recs = _session_load(hit)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("FAIL: 会话 %s 读不动：%s\n" % (sid, e))
        return 1
    rec = recs[-1] if recs else {}
    lines = [str(x) for x in (rec.get("lines") or [])]
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        # 非终端：可核对的回放，退出码沿用当时那次执行，不重新判一次
        for ln in lines:
            sys.stdout.write("%s\n" % ln)
        sys.stdout.write(
            "resume %s · action=%s · action_rc=%s · exit=%s\n"
            % (sid, rec.get("action"), rec.get("action_rc"), rec.get("exit_code"))
        )
        return int(rec.get("exit_code") or 0)
    base = base_url(args)
    try:
        face = load_agent(base)[2]["face"]
    except Exception as e:  # noqa: BLE001
        # 取不到 Agent 台就照常进 TUI，只如实说明没回灌成功
        sys.stderr.write("warn: Agent 台取不到，没回灌历史（%s）\n" % str(e)[:120])
        return cmd_tui(args)
    seed = [
        (str(rec.get("started") or "")[-8:] or "--:--:--", "< %s" % ln)
        for ln in lines[-40:]
    ]
    face.transcript = seed + list(face.transcript)
    face.hint = "已回灌会话 %s（%d 行）；重跑仍走授权闸，不会因为回放就自动执行" % (
        sid,
        len(seed),
    )
    return cmd_tui(args)


# ── shell 补全（自包含：脚本文本内嵌在本文件里）───────────────────
# 早先这里读 completions/agentos.<shell>，于是「补全能不能用」变成了装机
# 流程的副作用：没跑过 install.sh 的人敲 `agentos completion zsh` 只拿到
# 一句「脚本缺失」，而 CLI 契约是一个子命令自己就能吐出可用补全。
# 所以脚本文本内嵌 —— 任何人任何时候拿到的都是同一份。
# 门名不在这儿另抄一份：现读 tools/gates.sh 头部的「正确链」，链上加门、
# 改序，补全下一次打印就跟着变，不会出现补全里有、链里没有的幻觉项。
_AGENTOS_SUBS = (
    ("serve", "起本地服务（默认 8790，占用顺延，不抢 8787）"),
    ("health", "判 __health 的 ok:true（rc=0 才是真健康）"),
    ("open", "浏览器打开驾驶舱首页"),
    ("tui", "终端交互主界面（13 视图走真数据）"),
    ("gates", "转发 tools/gates.sh（门链入口）"),
    ("resident", "常驻真值门：核 launchd 常驻实例"),
    ("headroom", "headroom 8787 契约门"),
    ("term", "服务端只读跑一条命令（转发 POST /__term）"),
    ("gh", "GitHub 读写面（读直连；写默认拒绝：--yes+配置+TTY 三闸+审计链）"),
    ("agent", "内置终端 agent（模型+真实工具循环）"),
    ("config", "读写 ~/.agentos/config.toml（API key / provider / model）"),
    ("completion", "打印 shell 补全脚本"),
    ("sessions", "列 headless 会话历史"),
    ("resume", "回放一次会话"),
    ("steer", "老板监督指令：投给一个会话（步骤边界注入，只配送不裁决）"),
    ("watch", "老板监督面：计划/指令/会话尾一屏"),
    ("leader", "仓内指挥官：目标→里程碑→任务DAG→派发→回收→复盘"),
)


def _gate_names():
    """现读 tools/gates.sh 头部「正确链」那一行，取真门名与真顺序。

    解析失败就返回空表：空表仍是一份语法正确的补全脚本，只是门名少几个，
    比编一份猜出来的门名表诚实。
    """
    path = os.path.join(ROOT, "tools", "gates.sh")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                if "正确链" not in ln or "gates.sh" not in ln:
                    continue
                rest = ln.split("gates.sh", 1)[1]
                names = [
                    w for w in rest.split() if w and (w[0].isalpha() or w[0] == "_")
                ]
                if names:
                    return tuple(names)
    except OSError:
        pass
    return ()


def _term_cmd_names():
    """现读 tools/serve.py 的 TERM_CANDIDATES 键名，与 /__term 白名单同源。"""
    path = os.path.join(ROOT, "tools", "serve.py")
    names = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            inside = False
            for ln in f:
                if not inside:
                    if ln.startswith("TERM_CANDIDATES"):
                        inside = True
                    continue
                if ln.startswith("}"):
                    break
                key = ln.split(":", 1)[0].strip().strip('"').strip("'")
                if key:
                    names.append(key)
    except OSError:
        return ()
    return tuple(sorted(set(names)))


_ZSH_COMPLETION = r"""#compdef agentos
# AgentOS 终端 CLI 的 zsh 补全。由 `agentos completion zsh` 自包含打印。
#
# 常驻生效（推荐）：
#   mkdir -p ~/.zfunc && agentos completion zsh > ~/.zfunc/_agentos
#   print 'fpath=(~/.zfunc $fpath); autoload -Uz compinit; compinit' >> ~/.zshrc
#   exec zsh
# 只对当前 shell 生效：
#   eval "$(agentos completion zsh)"

typeset -ga _agentos_subs _agentos_gates _agentos_shells _agentos_terms
_agentos_subs=(
%(subs)s
)
_agentos_gates=( %(gates)s )
_agentos_shells=( zsh bash )
_agentos_terms=( %(terms)s )

_agentos() {
  local curcontext="$curcontext" state line ret=1
  typeset -A opt_args

  _arguments -C \
    '(-V --version)'{-V,--version}'[打印 CLI 自身版本（不连服务）]' \
    '(-h --help)'{-h,--help}'[显示帮助并退出]' \
    '(-p --print)'{-p,--print}'[headless 一次性模式]:需求:' \
    '--url=[服务地址]:url:_urls' \
    '--port=[服务端口]:端口:' \
    '--timeout=[单次执行上限秒]:秒:' \
    '--no-color[零 ANSI 输出]' \
    '--output-format=[输出格式]:格式:(text json)' \
    '1: :->sub' \
    '*:: :->args' && ret=0

  case $state in
    sub)
      _describe -t subcommands 'agentos 子命令' _agentos_subs && ret=0
      ;;
    args)
      case ${words[1]} in
        gates)
          _describe -t gates '门名（顺序见 tools/gates.sh 头部正确链）' \
            _agentos_gates && ret=0
          ;;
        completion)
          _describe -t shells 'shell' _agentos_shells && ret=0
          ;;
        term)
          _describe -t terms '只读命令' _agentos_terms && ret=0
          ;;
      esac
      ;;
  esac
  return ret
}

if [ "${funcstack[1]}" != "_agentos" ]; then
  compdef _agentos agentos
fi
"""

_BASH_COMPLETION = r"""# AgentOS 终端 CLI 的 bash 补全。由 `agentos completion bash` 自包含打印。
#
# 生效方式：source <(agentos completion bash)
# 常驻：把上一行追加进 ~/.bashrc（install.sh 装完后会打印同样一行）。

_agentos() {
  local cur prev cmd i
  COMPREPLY=()
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD-1]}"

  # 这个集合必须与 _AGENTOS_SUBS / argparse 的 17 个子命令逐项相等：
  # 2026-10-10 实测这里漏了 gh（用户敲 agentos gh <Tab> 什么都补不出来），
  # 而 --help 里明明有 —— cli_gate 的「补全与真实子命令不漂移」就是抓这个的。
  local subs="serve health open tui gates resident headroom term gh agent config completion sessions resume steer watch leader"
  local globals="-V --version -h --help -p --print --url --port --timeout --no-color --output-format"
  local gates="%(gates)s"
  local shells="zsh bash"
  local terms="%(terms)s"

  cmd=""
  for ((i = 1; i < COMP_CWORD; i++)); do
    case "${COMP_WORDS[i]}" in
      -*) continue ;;
      *) cmd="${COMP_WORDS[i]}"; break ;;
    esac
  done

  if [ -z "$cmd" ]; then
    case "$cur" in
      -*) COMPREPLY=( $(compgen -W "$globals" -- "$cur") ); return 0 ;;
    esac
    COMPREPLY=( $(compgen -W "$subs" -- "$cur") )
    return 0
  fi

  case "$cmd" in
    gates) COMPREPLY=( $(compgen -W "$gates" -- "$cur") ) ;;
    completion) COMPREPLY=( $(compgen -W "$shells" -- "$cur") ) ;;
    term) COMPREPLY=( $(compgen -W "$terms" -- "$cur") ) ;;
    *) COMPREPLY=( $(compgen -W "$globals" -- "$cur") ) ;;
  esac
  return 0
}

complete -F _agentos agentos
"""


def cmd_completion(args):
    """打印自包含补全脚本：脚本文本内嵌在本文件里，没装过机的人也拿得到。"""
    tpl = _ZSH_COMPLETION if args.shell == "zsh" else _BASH_COMPLETION
    sys.stdout.write(
        tpl
        % {
            "subs": "\n".join("    '%s:%s'" % (n, d) for n, d in _AGENTOS_SUBS),
            "gates": " ".join(_gate_names()),
            "terms": " ".join(_term_cmd_names()),
        }
    )
    return 0


# ── tui 十三视图的取数函数（全部走真 8790）───────────────────────
def load_overview(base):
    st, h = http_json(base + "/__health")
    if st != 200 or h.get("ok") is not True:
        raise RuntimeError(
            "/__health 未返回 ok:true（HTTP=%s ok=%s）" % (st, h.get("ok"))
        )
    s = h.get("stats") or {}
    rows = [
        ("健康", "ok"),
        ("项目根", h.get("root") or "?"),
        ("证据真树", h.get("tree") or "?"),
        ("index 指纹", h.get("index") or "?"),
        ("data 指纹", h.get("data") or "?"),
        ("SSE 客户端", str(h.get("clients"))),
        ("启动时间", s.get("started") or "?"),
        ("热重载", "%s 次" % s.get("rebuilds")),
        ("真树重抽", "%s 次" % s.get("tree_rebuilds")),
        ("代码 sha", h.get("code_sha") or "?"),
        ("执行中", h.get("act_running") or "无"),
        ("动作 allowlist", " ".join(h.get("actions") or [])),
    ]
    return rows, "GET /__health", h


def fetch_goal_json(base):
    """轮次账真源：先走引擎只读入口 POST /__goal/chain op=status，
    409（执行面忙）/超时/失败时退到 GET /__fs 只读面取 work/GOAL.json。
    两条路读的是同一个文件，不存在第二份真相。"""
    first_err = None
    try:
        st, o = http_json(base + "/__goal/chain", {"op": "status"}, timeout=45)
        if st == 200 and isinstance(o.get("ledger"), dict):
            return o["ledger"], "POST /__goal/chain op=status（引擎只读入口）"
        first_err = "HTTP=%s" % st
    except Exception as e:  # noqa: BLE001
        first_err = _short(e, 80)
    qs = urllib.parse.urlencode(
        {"op": "read", "path": "work/GOAL.json", "proj": "console"}
    )
    st, o = http_json(base + "/__fs?" + qs, timeout=20)
    if st == 200 and o.get("ok") and o.get("text"):
        return json.loads(o["text"]), "GET /__fs work/GOAL.json（只读面降级）"
    raise RuntimeError("轮次账双路皆败（chain: %s；fs: HTTP=%s）" % (first_err, st))


def load_rounds(base):
    g, src = fetch_goal_json(base)
    rows = [
        ("目标", str(g.get("goal") or "?")[:72]),
        ("状态", g.get("state") or "?"),
        ("轮次计划", "R1..R%s" % (g.get("rounds_plan") or "?")),
        ("已收轮次", "%d" % len(g.get("rounds") or [])),
    ]
    for r in g.get("rounds") or []:
        rows.append(
            (
                "R%s %s" % (r.get("n"), r.get("state") or "?"),
                "verdict=%s · delta=%s"
                % (r.get("verdict") or "—", str(r.get("delta") or "—")[:56]),
            )
        )
    if not g.get("rounds"):
        rows.append(("(空)", "还没有轮次记录（没跑过 set-goal）"))
    return rows, src, g


def load_matrix(base):
    g, src = fetch_goal_json(base)
    # 引擎 status 回执的轮次账把目标表放在 "table"，
    # /__fs 降级路径的 GOAL.json 原键名是 "goal_table" —— 两者都认。
    tbl = g.get("goal_table") or g.get("table") or []
    rows = [("矩阵规模", "%d 行 × 轮次目标" % len(tbl))]
    for t in tbl:
        rows.append(
            (
                "R%s %s" % (t.get("n"), _short(t.get("state") or "?", 8)),
                "%s · AC%s · %s"
                % (
                    _short(t.get("goal") or "—", 44),
                    _short(t.get("ac") or "—", 18),
                    _short(t.get("delta") or "—", 38),
                ),
            )
        )
    if not tbl:
        rows.append(("(空)", "目标表还没有行"))
    return rows, src, g


def load_ledger(base):
    """证据账本：门链**实测口径 vs 自报口径**的逐行对照。

    数据是 data.json 的 dirs（与 web 端 v-led 同一张表），经 /__fs 只读面取。
    以前这一面读的是 /__recurrence —— 那是**复盘台**的数据（同一类错误复发
    几次），两个判据被塞进同一屏，于是「实测和自报不一致」这个最要紧的分歧
    在 CLI 里根本看不见。现在两面分开，各读各的真源。
    """
    qs = urllib.parse.urlencode({"op": "read", "path": "data.json"})
    st, o = http_json(base + "/__fs?" + qs, timeout=25)
    if st != 200 or o.get("ok") is not True or not o.get("text"):
        raise RuntimeError(
            "data.json 读不出来（HTTP=%s %s）" % (st, _short(o.get("error") or "", 70))
        )
    try:
        d = json.loads(o["text"])
    except ValueError as e:
        raise RuntimeError("data.json 不是合法 JSON：%s" % _short(e, 60))
    dirs = d.get("dirs") or []
    div = set(x.get("dir") for x in (d.get("divergence") or []))
    rows = [
        ("载荷时间", d.get("generated_at") or "?"),
        ("全局结论", d.get("global_verdict") or "?"),
        ("口径分歧", "%d 行（◆ 自报与实测不一致）" % len(div)),
        ("证据来源", _short(d.get("evidence_source") or "?", 60)),
    ]
    rows.append(
        (
            "逐行对照",
            "%d 行 · 阶段 | 实测 | 自报 | exit | 失败条目" % len(dirs),
            {"sec": 1},
        )
    )
    for r in dirs[:24]:
        flag = " ◆" if r.get("dir") in div else ""
        rows.append(
            (
                str(r.get("dir") or "?"),
                "实测 %s · 自报 %s%s · exit %s · 失败 %s"
                "（负 %s 红 %s）"
                % (
                    r.get("gate") or "—",
                    r.get("declared") or "—",
                    flag,
                    "null" if r.get("exit") is None else r.get("exit"),
                    r.get("fail_total"),
                    r.get("fail_negctrl"),
                    r.get("fail_real"),
                ),
                {"kind": "no"} if r.get("dir") in div else None,
            )
        )
    if len(dirs) > 24:
        rows.append(("…", "共 %d 行，只显示前 24 行" % len(dirs), None))
    if not dirs:
        rows.append(
            (
                "(空)",
                "data.json 里没有 dirs —— 空表不等于没有分歧，只说明还没抽过",
                None,
            )
        )
    return rows, "GET /__fs?op=read&path=data.json（dirs + divergence）", d
    rows = [
        (
            "复发簇",
            "%d 个 · 阈值 %d run · 已复发 %d"
            % (
                o.get("n_clusters") or 0,
                o.get("threshold") or 0,
                o.get("n_recurring") or 0,
            ),
        ),
        ("事件总数", str(o.get("n_events"))),
        (
            "自我计数",
            "验证 %d · 修复 %d · 恢复 %d · 人工介入 %d"
            % (
                self_.get("agent_self_verifications") or 0,
                self_.get("agent_self_repairs") or 0,
                self_.get("agent_self_recoveries") or 0,
                self_.get("human_interventions") or 0,
            ),
        ),
        ("账本路径", o.get("memory") or "?"),
    ]
    for c in (o.get("clusters") or [])[:8]:
        rows.append(
            (
                "簇 %d run" % (c.get("runs") or 0),
                "%s · %s · 事件 %d"
                % (
                    _short(c.get("cluster"), 38),
                    _short(c.get("family"), 22),
                    c.get("events") or 0,
                ),
            )
        )
    led = (o.get("ledger") or [])[:10]
    rows.append(("复盘账本", "最近 %d 条" % len(led)))
    for e in led:
        rows.append(
            (
                e.get("verdict") or "?",
                "%s · %s"
                % (
                    _short(e.get("root_cause") or "—", 50),
                    _short(e.get("plan") or "—", 38),
                ),
            )
        )
    return rows, "GET /__recurrence", o


def load_engine(base):
    st, h = http_json(base + "/__health")
    if st != 200:
        raise RuntimeError("/__health HTTP=%s" % st)
    actions = h.get("actions") or []
    st, r = http_json(base + "/__act/recommend")
    if st != 200 or r.get("ok") is not True:
        raise RuntimeError("/__act/recommend 未返回 ok:true（HTTP=%s）" % st)
    recs = r.get("recommend") or []
    rows = [
        ("全局裁决", r.get("verdict") or "?"),
        ("可执行推荐", "%d 条" % (r.get("n") if r.get("n") is not None else len(recs))),
    ]
    for rec in recs:
        rows.append(
            (
                "[%s] %s" % (rec.get("severity") or "?", rec.get("action") or "?"),
                _short(rec.get("why") or "", 62),
            )
        )
    if not recs:
        rows.append(("(无事)", "一切皆绿，没有可执行推荐"))
    rows.append(("动作 allowlist", " ".join(actions)))
    return rows, "GET /__act/recommend + /__health", {"health": h, "recommend": r}


# ── 目标台 / 复盘台 / 能力地图 / 工作区 / 终端 / 文件 / 设置 ──────────
# 七个新视图全部走与 web 端**同一个**只读端点，不另起一套取数逻辑：
#   目标台  POST /__goal/chain op=status（409/失败降级 GET /__fs）
#   复盘台  GET  /__recurrence
#   能力地图 GET  /__capmap
#   工作区  GET  /__projects + GET /__fs?op=list
#   终端    POST /__term（只读白名单，rc 为唯一裁决）
#   文件    GET  /__fs?op=list / ?op=read
#   设置    GET  /__fs work/build.json + /__projects + /__health（三路只读源）
def load_goal(base):
    g, src = fetch_goal_json(base)
    rows = [
        ("目标", str(g.get("goal") or "?")[:78]),
        ("需求原文", str(g.get("demand") or "—")[:78]),
        ("状态", g.get("state") or "?"),
        (
            "轮次计划",
            "R1..R%s · 已收 %d 轮"
            % (g.get("rounds_plan") or "?", len(g.get("rounds") or [])),
        ),
    ]
    acs = g.get("acs") or []
    rows.append(("验收判据 AC", "%d 条（单条不足以判交付）" % len(acs), {"sec": 1}))
    # acs 的形态引擎改过：早期是 {"id","text"} 字典，现在 work/GOAL.json 里是
    # 字符串列表（["AC1","AC2",…]）。只认字典的那种写法当场
    # 'str' object has no attribute 'get' 崩掉整个目标台（2026-10-10 实测 rc=1）。
    # 两种都接，宁可少打一列 metadata，也不许因为 schema 变了就整面白屏。
    for i, a in enumerate(acs, 1):
        if isinstance(a, dict):
            rows.append((str(a.get("id") or i), _short(a.get("text") or "—", 60)))
        else:
            rows.append((str(i), _short(str(a), 60)))
    if not acs:
        rows.append(("(空)", "引擎没编译出 AC —— 这一条本身就是拒收理由", None))
    ng = g.get("non_goals") or []
    rows.append(
        ("非目标", "%d 条（空非目标 = 范围不可判定 → 引擎判红）" % len(ng), {"sec": 1})
    )
    for x in ng[:8]:
        rows.append(("·", _short(x.get("text") or x if isinstance(x, dict) else x, 68)))
    tbl = g.get("table") or g.get("goal_table") or []
    rows.append(("目标表", "%d 行 × 每轮一个专属目标与 AC" % len(tbl), {"sec": 1}))
    for t in tbl:
        if isinstance(t, dict):
            rows.append(
                (
                    "R%s %s" % (t.get("n"), _short(t.get("state") or "?", 8)),
                    _short(t.get("goal") or "—", 58),
                )
            )
        else:
            rows.append(("·", _short(str(t), 66)))
    ch = g.get("chain") or g.get("goal_chain") or []
    if ch:
        rows.append(("轮次链", "%d 环（闭环第二环的引擎回执）" % len(ch), {"sec": 1}))
        for c in ch[:8]:
            if isinstance(c, dict):
                rows.append(
                    (
                        str(c.get("round") or c.get("n") or "?"),
                        _short(c.get("next") or c.get("summary") or "—", 62),
                    )
                )
            else:
                rows.append(("·", _short(str(c), 66)))
    return rows, src, g


def load_retro(base):
    """复盘台：复发簇 + 自我能力计数 + 复盘账本 + 趋势。

    与旧的「证据账本」是同源数据（都读 /__recurrence）。拆成两面是因为
    web 端本来就是两面：证据账本量的是**门链实测 vs 自报**的分歧，复盘台
    量的是**同一类错误复发了多少次**。合成一面会把两个判据混成一个数字。
    """
    st, o = http_json(base + "/__recurrence")
    if st != 200 or o.get("ok") is not True:
        raise RuntimeError(
            "/__recurrence 未返回 ok:true（HTTP=%s %s）"
            % (st, _short(o.get("error") or "", 60))
        )
    self_ = o.get("self") or {}
    rows = [
        (
            "复发簇",
            "%d 个 · 阈值 %d run · 已达阈值复发 %d"
            % (
                o.get("n_clusters") or 0,
                o.get("threshold") or 0,
                o.get("n_recurring") or 0,
            ),
        ),
        ("事件总数", str(o.get("n_events"))),
        (
            "自我计数",
            "验证 %d · 修复 %d · 恢复 %d · 人工介入 %d"
            % (
                self_.get("agent_self_verifications") or 0,
                self_.get("agent_self_repairs") or 0,
                self_.get("agent_self_recoveries") or 0,
                self_.get("human_interventions") or 0,
            ),
        ),
    ]
    trend = o.get("trend") or []
    if trend:
        rows.append(
            (
                "趋势",
                " → ".join(
                    "%s:%s"
                    % (
                        t.get("day") or t.get("d"),
                        t.get("n") if t.get("n") is not None else t.get("v"),
                    )
                    for t in trend[-10:]
                ),
            )
        )
    rows.append(("复发簇明细", "%d 簇" % len(o.get("clusters") or []), {"sec": 1}))
    for c in (o.get("clusters") or [])[:10]:
        rows.append(
            (
                "%d run · %s"
                % (c.get("runs") or 0, _short(c.get("family") or "?", 16)),
                _short(c.get("cluster"), 60),
            )
        )
    led = (o.get("ledger") or [])[:12]
    rows.append(
        ("复盘账本", "最近 %d 条 · %s" % (len(led), o.get("memory") or "?"), {"sec": 1})
    )
    for e in led:
        rows.append(
            (
                str(e.get("verdict") or "?"),
                "%s · 下次改：%s"
                % (
                    _short(e.get("root_cause") or "—", 44),
                    _short(e.get("plan") or "—", 34),
                ),
            )
        )
    if not led:
        rows.append(("(空)", "复盘账本还没有条目 —— 空账不等于没有复发"))
    return rows, "GET /__recurrence", o


def load_cap(base):
    """能力地图：自报与「被独立证据验过」永远并排，不合成单一数字。"""
    st, o = http_json(base + "/__capmap")
    if st != 200 or o.get("ok") is not True:
        raise RuntimeError(
            "/__capmap 未返回 ok:true（HTTP=%s %s）"
            % (st, _short(o.get("error") or "", 60))
        )
    sc = o.get("status_counts") or {}
    vc = o.get("verdict_counts") or {}
    rows = [
        ("能力总数", str(o.get("total") or len(o.get("rows") or []))),
        (
            "状态分布",
            " · ".join("%s=%s" % (k, v) for k, v in sorted(sc.items())) or "—",
        ),
        (
            "独立裁决",
            " · ".join("%s=%s" % (k, v) for k, v in sorted(vc.items())) or "—",
        ),
        (
            "已验过",
            "%d · 验不了 %d · 无裁决 %d · 未署名 %d"
            % (
                o.get("n_verified") or 0,
                o.get("n_unverifiable") or 0,
                o.get("n_no_verdict") or 0,
                o.get("n_anonymous") or 0,
            ),
        ),
        (
            "账本一致性",
            "记录 %d · 裁决 id %d · 悬空裁决 %d · 坏行 %d"
            % (
                o.get("n_ledger") or 0,
                o.get("n_verdict_ids") or 0,
                o.get("n_verdict_dangling") or 0,
                o.get("n_bad_lines") or 0,
            ),
        ),
        ("降级项", str(o.get("n_degraded") or 0)),
        ("账本路径", o.get("ledger_path") or "?"),
    ]
    rows.append(
        (
            "逐条能力",
            "%d 条（自报 status ≠ 独立证据）" % len(o.get("rows") or []),
            {"sec": 1},
        )
    )
    for r in (o.get("rows") or [])[:14]:
        cur = r.get("current_status") or ""
        tail = " ⇒ %s" % cur if cur and cur != r.get("status") else ""
        rows.append(
            (
                _short(r.get("id"), 26),
                "%s%s · %s"
                % (
                    _short(r.get("status") or "?", 12),
                    tail,
                    _short(r.get("problem") or r.get("source") or "—", 34),
                ),
            )
        )
    return rows, "GET /__capmap", o


def _ws_dirs(base):
    st, o = http_json(base + "/__fs?op=list&depth=1")
    if st != 200 or o.get("ok") is not True:
        raise RuntimeError(
            "/__fs?op=list 未返回 ok:true（HTTP=%s %s）"
            % (st, _short(o.get("error") or "", 60))
        )
    return o.get("entries") or []


def load_ws(base):
    """工作区：真目录条目 + 项目空间（切换项目 = 真换根，不是换文案）。"""
    st, p = http_json(base + "/__projects")
    if st != 200 or p.get("ok") is not True:
        raise RuntimeError(
            "/__projects 未返回 ok:true（HTTP=%s %s）"
            % (st, _short(p.get("error") or "", 60))
        )
    rows = [
        ("当前项目", "%s（%s）" % (p.get("active"), p.get("default"))),
        ("项目数", str(p.get("n_projects") or len(p.get("projects") or []))),
    ]
    rows.append(("项目空间", "换根 = /__fs?proj= 真换，不是换一段文案", {"sec": 1}))
    for pr in p.get("projects") or []:
        f = pr.get("facts") or {}
        rows.append(
            (
                "%s%s"
                % (
                    "▸ " if pr.get("key") == p.get("active") else "  ",
                    pr.get("label") or pr.get("key"),
                ),
                "%s · 顶层 %s 项 / %s 文件 · 最新 %s"
                % (
                    _short(pr.get("root") or "?", 46),
                    f.get("n_top"),
                    f.get("n_files"),
                    _short(f.get("newest_mtime") or "?", 16),
                ),
            )
        )
    entries = _ws_dirs(base)
    rows.append(
        ("当前根条目", "%d 条（/__fs?op=list，只读）" % len(entries), {"sec": 1})
    )
    for e in entries[:20]:
        rows.append(
            (
                "D" if e.get("dir") else "F",
                "%-28s %8s"
                % (
                    _short(e.get("name"), 28),
                    (str(e.get("size")) + "B") if not e.get("dir") else "<dir>",
                ),
                {"kind": "fsdir" if e.get("dir") else "fsfile", "path": e.get("path")},
            )
        )
    return rows, "GET /__projects + GET /__fs?op=list&depth=1", {"p": p}


# 终端 / 文件两面带自己的输入态与光标态，且要「切走再切回不丢」——
# 与 AgentFace 同构：按 base 缓存，不用全局单例（同一台机器可能连着两个口）。
_PANELS = {}


TERM_TAIL = 6  # 终端面每条历史只回显输出的最后这么多行


class Panel(object):
    """终端与文件两面的本地状态：输入行、历史、当前目录、选中文件。

    终端这一面的纪律与 web 端 v-term 完全同源：
      · 命令只走 POST /__term 的只读白名单，服务端回什么就显示什么；
      · rc 用服务端回执里的那个值，绝不本地猜一个 0；
      · 命令没跑起来（超时/起不来）时 rc 是 null —— 屏上必须写成
        「未执行」而不是「rc=0」，这是这条面唯一不能省的诚实。
    """

    def __init__(self, base):
        self.base = base
        self.buf = ""  # 输入行
        self.typing = False
        self.dir = "."  # 文件面当前目录
        self.file = None  # 正在看的文件相对路径
        self.code = []  # 文件面代码行
        self.hist = []  # 终端面 [(时刻, argv, rc, dur, out, note)]
        self.hint = ""
        self.hint_kind = ""  # "no" = 红字
        self.busy = False

    # ── 终端面 ────────────────────────────────────────
    def allowlist(self):
        """终端只读白名单，从**服务端自己**的回话里取，不在本地抄一份。

        为什么不抄：TERM_CMDS 是 serve.py 里一张会随平台变化的表（命令找不到
        就整个不上表，实测 TERM_UNRESOLVED），本地抄一份的结果是「屏上列着
        python3、一跑就说不认识」—— 正是本项目一路在治的假绿。而 /__health 的
        actions 是**执行面** allowlist（rebuild/verify…），跟终端白名单不是
        一张表，拿它充数是第二种假绿。

        于是用服务端对非法命令的原话取真值：term_run 的报错自带「可跑：…」。
        解析不到就明说取不到，绝不用执行面的表顶替。
        """
        try:
            st, j = http_json(
                self.base + "/__term", {"argv": ["__agentos_list_probe__"]}, timeout=20
            )
        except Exception:  # noqa: BLE001
            return None
        msg = str(j.get("error") or "")
        i = msg.find("可跑：")
        if i < 0:
            return None
        tail = msg[i + len("可跑：") :].strip().strip("（）()")
        got = [x for x in tail.split() if x]
        return sorted(got) if got else None

    def run_cmd(self, line):
        line = (line or "").strip()
        if not line:
            return
        t = time.strftime("%H:%M:%S")
        argv = line.split()
        try:
            st, j = http_json(self.base + "/__term", {"argv": argv}, timeout=45)
        except Exception as e:  # noqa: BLE001
            self.hist.append((t, line, None, None, "", "NETWORK %s" % _short(e, 80)))
            self.hint = "网络失败，命令没有跑"
            self.hint_kind = "no"
            return
        if st != 200:
            err = _short(j.get("error") or ("HTTP=%s" % st), 120)
            self.hist.append((t, line, None, None, "", err))
            self.hint = err
            self.hint_kind = "no"
            return
        rc = j.get("rc")
        note = []
        if rc is None:
            note.append("未执行：%s" % (j.get("error") or "服务端未回 rc"))
        if j.get("truncated"):
            note.append("输出被服务端截断（32 KiB 上限），不是全量")
        self.hist.append(
            (t, line, rc, j.get("dur"), j.get("out") or "", " · ".join(note))
        )
        if rc is None:
            self.hint = "命令未执行 —— 不给 0"
            self.hint_kind = "no"
        elif rc == 0:
            self.hint = "rc=0 · %ss" % j.get("dur")
            self.hint_kind = ""
        else:
            self.hint = "rc=%s · %ss（非零就是非零）" % (rc, j.get("dur"))
            self.hint_kind = "no"
        del self.hist[:-40]

    # ── 文件面 ────────────────────────────────────────
    def ls(self, rel):
        qs = urllib.parse.urlencode({"op": "list", "depth": "1", "path": rel or "."})
        st, o = http_json(self.base + "/__fs?" + qs)
        if st != 200 or o.get("ok") is not True:
            raise RuntimeError(
                "/__fs 列目录失败（HTTP=%s %s）"
                % (st, _short(o.get("error") or "", 70))
            )
        return o

    def open_file(self, rel):
        qs = urllib.parse.urlencode({"op": "read", "path": rel})
        st, o = http_json(self.base + "/__fs?" + qs)
        if st != 200 or o.get("ok") is not True:
            raise RuntimeError(
                "/__fs 读文件失败（HTTP=%s %s）"
                % (st, _short(o.get("error") or "", 70))
            )
        self.file = rel
        self.code = (o.get("text") or "").splitlines()
        if o.get("truncated"):
            self.code.append("… 服务端只回了前 %d 字节，不是全量" % FS_PEEK)
        return o

    def goto(self, rel):
        """输入行给一个路径：是目录就切过去，是文件就读取。

        先 ls 判真假，不靠结尾斜杠猜 —— 猜错的后果是「看着进了目录，
        其实在读一个同名文件」，而这种静默错位本项目一路在治。
        """
        rel = (rel or "").strip()
        if not rel:
            return
        try:
            self.ls(rel)
        except Exception:  # noqa: BLE001
            # 列不出来，就按文件试；再不行把服务端原话抛给调用方。
            self.open_file(rel)
            self.hint = "已读入 %s" % rel
            self.hint_kind = ""
            return
        self.dir = rel
        self.file = None
        self.code = []
        self.hint = "已进入 %s" % rel
        self.hint_kind = ""

    def up_dir(self):
        """文件面回上一层。

        先在服务端真列一遍再切：跟着就 ls 一次，失败了屏上出现的是服务端的
        原话（越界/不存在），不是一个进得去却是空的目录 —— 本项目把
        「看着成功了其实没有」当作最贵的缺陷，此处同一纪律。
        根目录下不再往上：把项目根之外的目录列给用户看，本身就是越权。
        """
        rel = (self.dir or ".").strip().rstrip("/")
        if rel in ("", ".", "/"):
            self.hint = "已在产品根（.），不再往外走"
            self.hint_kind = "no"
            return False
        head = rel.rpartition("/")[0]
        if rel.startswith("/") and not head:
            head = "/"
        elif not head:
            head = "."
        try:
            self.ls(head)
        except Exception as e:  # noqa: BLE001
            self.hint = _short(e, 90)
            self.hint_kind = "no"
            return False
        self.dir = head
        self.file = None
        self.code = []
        self.hint = "已进入 %s" % head
        self.hint_kind = ""
        return True


def _panel(base):
    p = _PANELS.get(base)
    if p is None:
        p = Panel(base)
        _PANELS[base] = p
    return p


def load_term(base):
    """终端面：输入行 + 历史。第一屏先给白名单，免得用户瞎猜能跑什么。"""
    p = _panel(base)
    al = p.allowlist()
    if al:
        rows = [("只读白名单", " ".join(al), {"sec": 1})]
    else:
        # 取不到就说取不到。这里最危险的捷径是把 /__health 的 actions
        # （执行面 allowlist）贴上来充数 —— 屏上立刻像模像样，但那是另一张表。
        rows = [("只读白名单", "服务端未回（不拿执行面 actions 顶替）", None)]
    if p.hint:
        rows.append(("上一条", p.hint, {"kind": "no"} if p.hint_kind == "no" else None))
    rows.append(("历史", "%d 条（rc 全部取自服务端回执）" % len(p.hist), {"sec": 1}))
    for t, cmd, rc, dur, out, note in reversed(p.hist):
        if rc is None:
            tag = "未执行"
        else:
            tag = "rc=%s · %ss" % (rc, dur)
        rows.append(
            (
                "$ %s" % _short(cmd, 46),
                tag,
                {"kind": "no"} if (rc is None or rc != 0) else None,
            )
        )
        if note:
            rows.append(("  !", _short(note, 66), {"kind": "no"}))
        ol = (out or "").splitlines()
        if len(ol) > TERM_TAIL:
            # 只回显尾巴，就必须说清「前面还有 N 行没显示」。不说，用户会把
            # 这 6 行当成命令的全部输出 —— 本项目把「看着像全量、其实是尾巴」
            # 列为最贵的假象的一种，这里按同一条纪律办。
            rows.append(
                (
                    "  !",
                    "… 前 %d 行未显示，只回显最后 %d 行"
                    % (len(ol) - TERM_TAIL, TERM_TAIL),
                    {"kind": "no"},
                )
            )
        for ln in ol[-TERM_TAIL:]:
            rows.append(("  ", _short(ln, 74), None))
    if not p.hist:
        rows.append(
            ("·", "还没有跑过命令。按 i 或 / 进输入行，回车即跑（真 rc，不猜）", None)
        )
    rows.append(
        (
            "输入",
            "i 或 / 进输入行 · Enter 跑 · Esc 取消 · cwd=产品根 · 超时 20s 硬杀",
            {"sec": 1},
        )
    )
    return rows, "POST /__term（只读白名单）", {"panel": p}


def load_file(base):
    """文件面：目录树 + 代码。回车进目录 / 看代码，← 或 .. 回上一层。"""
    p = _panel(base)
    try:
        o = p.ls(p.dir)
    except Exception as e:  # noqa: BLE001
        return ([("取数失败", str(e)[:110])], "错误（/__fs 不可达）", {"panel": p})
    rows = [
        ("目录", p.dir or ".", {"sec": 1}),
        ("上级", "… 回车回到上一层（根目录下不可用）", {"kind": "up"}),
    ]
    for e in o.get("entries") or []:
        nm = e.get("name") or "?"
        rows.append(
            (
                "D " if e.get("dir") else "F ",
                "%-30s %s"
                % (
                    _short(nm, 30),
                    "<dir>" if e.get("dir") else "%dB" % (e.get("size") or 0),
                ),
                {"kind": "dir" if e.get("dir") else "file", "path": e.get("path")},
            )
        )
    if p.file:
        rows.append(
            ("代码", "%s · %d 行（回车重新读取）" % (p.file, len(p.code)), {"sec": 1})
        )
        first = max(0, len(p.code) - 30)
        for i in range(first, len(p.code)):
            rows.append(("%4d" % (i + 1), _short(p.code[i], 76), None))
        if len(p.code) > 30:
            rows.append(("…", "共 %d 行，只显示最后 30 行" % len(p.code), None))
    return rows, "GET /__fs?op=list / op=read（只读，越界服务端拒）", {"panel": p}


def load_set(base):
    """设置面：逐行标来源。三路只读源，一行一个出处，不写「页面内联」冒充。"""
    rows = []
    qs = urllib.parse.urlencode({"op": "read", "path": "work/build.json"})
    st, b = http_json(base + "/__fs?" + qs)
    if st == 200 and b.get("ok") and b.get("text"):
        try:
            bj = json.loads(b["text"])
        except ValueError:
            bj = None
        if isinstance(bj, dict):
            for k in ("html_sha256", "data_sha256", "file_sha256", "built_at"):
                rows.append((k, str(bj.get(k) or "—")))
        else:
            rows.append(("构建指纹", "work/build.json 不是合法 JSON", None))
    else:
        rows.append(
            (
                "构建指纹",
                "读不到：%s" % _short(b.get("error") or "HTTP=%s" % st, 70),
                None,
            )
        )
    st, p = http_json(base + "/__projects")
    if st == 200 and p.get("ok"):
        rows.append(
            (
                "项目",
                "%s / %s（共 %s）"
                % (p.get("active"), p.get("default"), p.get("n_projects")),
            )
        )
        for pr in p.get("projects") or []:
            rows.append(
                (
                    "· %s" % _short(pr.get("label") or pr.get("key"), 20),
                    _short(pr.get("root") or "?", 56),
                )
            )
    else:
        rows.append(
            ("项目", "读不到：%s" % _short(p.get("error") or "HTTP=%s" % st, 70), None)
        )
    st, h = http_json(base + "/__health")
    if st == 200 and h.get("ok") is True:
        s = h.get("stats") or {}
        rows += [
            ("服务端口", base),
            ("index 指纹", h.get("index") or "?"),
            ("data 指纹", h.get("data") or "?"),
            ("代码 sha", h.get("code_sha") or "?"),
            (
                "热重载",
                "%s 次 · 真树重抽 %s 次" % (s.get("rebuilds"), s.get("tree_rebuilds")),
            ),
            ("SSE 客户端", str(h.get("clients"))),
        ]
    else:
        rows.append(("服务", "读不到：HTTP=%s" % st, None))
    rows.append(
        (
            "来源",
            "work/build.json ← /__fs · 项目 ← /__projects · "
            "服务 ← /__health · 全部只读",
            {"sec": 1},
        )
    )
    return rows, "GET /__fs + GET /__projects + GET /__health（三路只读）", {}


# ── Agent 台：web 端 v-work 五面的终端复刻 ──────────────────
AGENT_FPK = [
    ("code_sha", "serve.py 代码指纹"),
    ("index", "index.html 指纹"),
    ("data", "data.json 指纹"),
]
_FACES = {}  # base -> AgentFace（切走再切回不丢状态）


class AgentFace(object):
    """Agent 台六面（工具卡/审批卡/流式 transcript/真表 diff/事件/composer）。

    纪律与 web 端 v-work 同源：
      · 工具执行只认服务端 allowlist，rc 为唯一裁决；
      /__act/recommend 有非绿项时，未授权动作一律不发请求
      （审批卡就是那道闸，不做先斩后奏）；
      · 中止只认 SSE 先推的 run id，不盲杀；
      · 自然语言路由走 /__act/resolve 只读面，路由 ≠ 授权。
    """

    def __init__(self, base):
        self.base = base
        self.acts = []  # [(name, desc)] 来自 POST /__act __list
        self.act_last = None  # /__health 的 act_last（最近一次真回执）
        self.recs = []  # /__act/recommend 的非绿推荐
        self.pend = set()  # 显式授权集合（授权是显式状态）
        self.transcript = []  # [(时刻, 行)] 来自 /__act/stream SSE
        self.events = []  # [(时刻, 事件名)] 来自 /__watch SSE
        self.prev_fp = None
        self.cur_fp = None
        self.hint = ""
        self.hint_kind = ""  # "no" = 红字
        self.run_state = None
        self._watch_started = False

    # ── 取数（全走真 8790）──────────────────────────────
    def refresh(self):
        st, h = http_json(self.base + "/__health")
        if st != 200 or h.get("ok") is not True:
            raise RuntimeError(
                "/__health 未返回 ok:true（HTTP=%s ok=%s）" % (st, h.get("ok"))
            )
        st, lj = http_json(self.base + "/__act", {"action": "__list"})
        if st != 200 or not isinstance(lj.get("actions"), list):
            raise RuntimeError("/__act __list 失败（HTTP=%s）" % st)
        self.acts = [(a.get("name", "?"), a.get("desc", "")) for a in lj["actions"]]
        self.act_last = h.get("act_last")
        st, rj = http_json(self.base + "/__act/recommend")
        if st != 200 or rj.get("ok") is not True:
            raise RuntimeError("/__act/recommend 未返回 ok:true（HTTP=%s）" % st)
        self.recs = rj.get("recommend") or []
        self.prev_fp = self.cur_fp
        self.cur_fp = {
            "code_sha": h.get("code_sha"),
            "index": h.get("index"),
            "data": h.get("data"),
        }
        self._start_watch()

    def _start_watch(self):
        if self._watch_started:
            return
        self._watch_started = True
        threading.Thread(target=self._watch_loop, daemon=True).start()

    def _watch_loop(self):
        """/__watch SSE 常连：事件名落到 events（本会话）。"""
        try:
            req = urllib.request.Request(self.base + "/__watch")
            with _OPENER.open(req, timeout=None) as r:
                pend = None
                for raw in r:
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    if line.startswith("event: "):
                        pend = line[7:].strip()
                    elif line.startswith("data: ") and pend:
                        self.events.append((time.strftime("%H:%M:%S"), pend))
                        del self.events[:-40]
                        pend = None
        except Exception:  # noqa: BLE001
            pass

    # ── 行（sec 小节标题 / head 表头 / tool 工具卡 /
    #       approval 审批卡 / 普通日志行）─────────────────
    def rows(self):
        rows = []
        rows.append(
            ("工具调用卡片", "动作只认服务端 allowlist · rc 为唯一裁决", {"sec": 1})
        )
        for name, desc in self.acts:
            tag = ""
            al = self.act_last
            if al and al.get("action") == name:
                tag = "rc=%s · %ss" % (al.get("rc"), al.get("dur"))
            val = "%s   [%s]" % (desc, tag) if tag else desc
            rows.append((name, _short(val, 64), {"kind": "tool", "name": name}))
        rows.append(("审批卡", "授权是显式状态，不是隐含默认值", {"sec": 1}))
        if not self.recs:
            rows.append(
                (
                    "(无事)",
                    "当前无非绿项，无需授权任何动作（来自 /__act/recommend 真判定）",
                    None,
                )
            )
        for r in self.recs:
            act = r.get("action")
            on = act in self.pend
            rows.append(
                (
                    act,
                    "%s · %s" % ("已授权" if on else "待授权", str(r.get("why"))[:52]),
                    {"kind": "approval", "action": act},
                )
            )
        rows.append(
            ("流式 transcript", "接 /__act/stream SSE · 逐行推不是跑完才给", {"sec": 1})
        )
        if not self.transcript:
            rows.append(
                (
                    "·",
                    "未运行。执行只认 allowlist，rc 为唯一裁决；"
                    "回车工具卡或用 composer 发起",
                    None,
                )
            )
        for t, line in self.transcript[-8:]:
            rows.append((t, _short(line, 68), None))
        rows.append(
            ("指纹 diff", "/__health 三指纹逐次比对 · 静止的指纹不算 diff", {"sec": 1})
        )
        rows.append(("指标", "当前值                较上一轮", {"head": 1}))
        for key, label in AGENT_FPK:
            cur = (self.cur_fp or {}).get(key)
            prev = (self.prev_fp or {}).get(key) if self.prev_fp else None
            chg = bool(self.prev_fp and cur and prev and cur != prev)
            tail = "变了（原 %s）" % prev if chg else "未变"
            rows.append((label, "%-20s %s" % (str(cur or "不可判定")[:20], tail), None))
        rows.append(
            ("/__watch 事件", "最近 %d 条（本会话）" % len(self.events), {"sec": 1})
        )
        if not self.events:
            rows.append(("·", "本会话内尚无 __watch 事件（静止不等于有问题）", None))
        for t, nm in self.events[-4:]:
            rows.append((t, nm, None))
        rows.append(
            (
                "composer",
                "c 输入 · /动作名 或自然语言 · Enter 提交 · s 中止运行",
                {"sec": 1},
            )
        )
        # ── 计划面（DSP）：读本地 work/plans/*-plan.json 真数据 ──
        # 为什么读本地不走 8790：计划是 CLI 侧（agent_core）产物，服务端
        # 不存计划真相；走 /__fs 绕一圈反而多一层可坏。fail-closed：目录
        # 不可读如实报，不拿占位冒充。
        rows.append(("决策监督计划", "work/plans 真数据 · 未审批写锁死", {"sec": 1}))
        try:
            _pdir = os.path.join(ROOT, "work", "plans")
            _pfiles = sorted(n for n in os.listdir(_pdir) if n.endswith("-plan.json"))
        except OSError as e:
            rows.append(("·", "计划目录不可读：%s" % str(e)[:60], None))
            return rows
        if not _pfiles:
            rows.append(("·", "暂无计划（REPL 里 /plan <目标> 建一个）", None))
            return rows
        for _pf in _pfiles[-3:]:
            try:
                with open(os.path.join(_pdir, _pf), encoding="utf-8") as _f:
                    _pd = json.load(_f)
                _done = sum(
                    1 for _t in _pd.get("tasks", []) if _t.get("status") == "done"
                )
                _tot = len(_pd.get("tasks", []))
                rows.append(
                    (
                        _pf[: -len("-plan.json")],
                        "%s [%d/%d done] %s"
                        % (
                            str(_pd.get("goal", "?"))[:40],
                            _done,
                            _tot,
                            "已审批" if _pd.get("approved") else "未审批·写锁死",
                        ),
                        None,
                    )
                )
            except (OSError, ValueError) as e:
                rows.append((_pf, "读失败：%s" % str(e)[:60], None))
        return rows

    # ── 执行（授权闸 → SSE 流 → POST，rc 唯一裁决）──────
    def run_tool(self, name):
        if self.run_state is not None:
            return
        if self.recs and name not in self.pend:
            self.hint = "%s 未授权 —— 授权是显式状态：回车审批卡先授权，再执行" % name
            self.hint_kind = "no"
            return
        sink = {"lines": [], "run": None, "resp": None, "post_done": False}
        st = threading.Thread(target=self._stream_loop, args=(sink,), daemon=True)
        st.start()
        t0 = time.time()
        while sink["run"] is None and time.time() - t0 < 3 and st.is_alive():
            time.sleep(0.05)
        run_id = sink["run"]
        self.transcript.append(
            (
                time.strftime("%H:%M:%S"),
                "▶ %s%s" % (name, "（run=%s）" % run_id if run_id else ""),
            )
        )
        self.hint = "%s 运行中…" % name
        self.hint_kind = ""
        pt = threading.Thread(target=self._post_loop, args=(name, sink), daemon=True)
        pt.start()
        self.run_state = {"name": name, "sink": sink, "stream": st, "post": pt}

    def _stream_loop(self, sink):
        """/__act/stream：先推 __RUN__ run id（中止靠它指名道姓），
        逐行推输出，__RC__ 后连接即关。"""
        try:
            req = urllib.request.Request(self.base + "/__act/stream?cursor=0")
            with _OPENER.open(req, timeout=None) as r:
                for raw in r:
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    if not line.startswith("data: "):
                        continue
                    d = line[6:].strip()
                    if d == "stream":
                        continue
                    if d.startswith("__RUN__ "):
                        sink["run"] = d[8:].strip()
                    elif d.startswith("__RC__ "):
                        break
                    else:
                        sink["lines"].append((time.strftime("%H:%M:%S"), d))
        except Exception:  # noqa: BLE001
            pass

    def _post_loop(self, name, sink):
        try:
            st, j = http_json(self.base + "/__act", {"action": name}, timeout=900)
        except Exception as e:  # noqa: BLE001
            st, j = 0, {"ok": False, "rc": -1, "tail": ["NETWORK %s" % _short(e, 80)]}
        sink["resp"] = j
        sink["post_done"] = True

    def poll_run(self):
        """TUI 主循环每帧调用：run 落账（回执 + 流式行）后刷新取数。"""
        rs = self.run_state
        if rs is None:
            return
        sink = rs["sink"]
        if not sink.get("post_done"):
            return
        rs["stream"].join(timeout=8)
        self.transcript.extend(sink["lines"])
        j = sink.get("resp") or {}
        name = rs["name"]
        t = time.strftime("%H:%M:%S")
        if j.get("aborted"):
            self.transcript.append(
                (
                    t,
                    "⏹ %s 被用户中止（run=%s）—— "
                    "不记成功" % (name, j.get("run") or "?"),
                )
            )
            self.hint = "%s 已中止 rc=%s（不算成功）" % (name, j.get("rc"))
            self.hint_kind = "no"
        else:
            self.transcript.append(
                (
                    t,
                    "◀ %s rc=%s 用时 %ss（流式 %d 行）"
                    % (name, j.get("rc"), j.get("dur"), len(sink["lines"])),
                )
            )
            self.hint = "%s rc=%s · %s" % (
                name,
                j.get("rc"),
                "成功" if j.get("ok") else "失败",
            )
            self.hint_kind = "" if j.get("ok") else "no"
        self.run_state = None
        try:
            self.refresh()
        except Exception as e:  # noqa: BLE001
            self.hint = "run 已落账，但刷新取数失败: %s" % str(e)[:60]
            self.hint_kind = "no"

    def abort(self):
        rs = self.run_state
        if rs is None:
            self.hint = "还没有正在运行的 run（无 run id 可中止）"
            self.hint_kind = "no"
            return
        run = rs["sink"].get("run")
        if not run:
            self.hint = "run id 未到手，不盲杀"
            self.hint_kind = "no"
            return
        try:
            st, j = http_json(self.base + "/__act/abort", {"run": run})
        except Exception as e:  # noqa: BLE001
            st, j = 0, {"ok": False, "error": str(e)[:60]}
        self.hint = "中止 run=%s → HTTP=%s ok=%s" % (run, st, j.get("ok"))
        self.hint_kind = "" if j.get("ok") else "no"

    # ── 审批卡：授权是显式状态，开关都留痕 ───────────────
    def toggle_auth(self, action):
        if action in self.pend:
            self.pend.discard(action)
            self.hint = "%s 已撤销授权" % action
        else:
            self.pend.add(action)
            self.hint = "%s 已授权，可执行" % action
        self.hint_kind = ""

    # ── composer：/help、/动作名、自然语言路由（只读面）──
    def submit(self, q):
        q = (q or "").strip()
        if not q:
            return
        t = time.strftime("%H:%M:%S")
        if q in ("/help", "?"):
            self.transcript.append(
                (t, "/help → 列出 %d 个 allowlist 动作" % len(self.acts))
            )
            self.hint = "可用动作 " + " · ".join("/" + n for n, _ in self.acts)
            self.hint_kind = ""
            return
        names = dict(self.acts)
        act = None
        if q.startswith("/"):
            head = q[1:].split()[0]
            if head in names:
                act = head
            else:
                self.transcript.append((t, "no-match（/%s 不在 allowlist）" % head))
                self.hint = "/%s 不在 allowlist —— /help 列全部动作" % head
                self.hint_kind = "no"
                return
        else:
            head = q.split()[0]
            if head in names:
                act = head
        if act:
            self.run_tool(act)
            return
        # 自然语言兜底：/__act/resolve 是纯函数只读面，不执行不授权。
        # 路由 ≠ 授权：命中后仍会被审批卡挡住。
        try:
            st, j = http_json(self.base + "/__act/resolve?q=" + urllib.parse.quote(q))
        except Exception as e:  # noqa: BLE001
            self.hint = "/__act/resolve 不可达：%s" % str(e)[:60]
            self.hint_kind = "no"
            return
        if j.get("ok") and j.get("action"):
            via = ""
            if j.get("how") == "intent":
                via = "（按「%s」路由）" % "+".join(j.get("matched") or [])
            self.transcript.append((t, "「%s」→ %s%s" % (q, j["action"], via)))
            self.hint = "「%s」理解为 %s%s —— 仍需审批卡授权" % (q, j["action"], via)
            self.hint_kind = ""
            self.run_tool(j["action"])
        elif j.get("reason") == "ambiguous":
            c = " · ".join("%s(%s)" % (a, s) for a, s in (j.get("candidates") or []))
            self.transcript.append((t, "「%s」歧义 → %s" % (q, c)))
            self.hint = "「%s」有歧义，不替你猜：%s —— 请用 /动作名 明确" % (q, c)
            self.hint_kind = "no"
        else:
            why = "是空的" if j.get("reason") == "empty" else "不在任何动作词典里"
            self.transcript.append((t, "「%s」no-match（%s）" % (q, why)))
            self.hint = "「%s」%s —— 听不懂，不乱猜；用 /动作名 或 /help" % (q, why)
            self.hint_kind = "no"


def load_agent(base):
    face = _FACES.get(base)
    if face is None:
        face = AgentFace(base)
        _FACES[base] = face
    face.refresh()
    return (
        face.rows(),
        ("GET /__health + /__act(__list) + /__act/recommend"),
        {"face": face},
    )


LOADERS = (
    load_overview,
    load_rounds,
    load_matrix,
    load_ledger,
    load_engine,
    load_agent,
    load_goal,
    load_retro,
    load_cap,
    load_ws,
    load_term,
    load_file,
    load_set,
)
FILE_VI = VIEWS.index("文件")
PANEL_VI = (TERM_VI, FILE_VI)
INPUT_VI = (AGENT_VI, TERM_VI, FILE_VI)  # 有输入行的三面
VIEW_SRC = {TERM_VI: "POST /__term", FILE_VI: "GET /__fs"}
assert len(LOADERS) == len(VIEWS), "LOADERS 与 VIEWS 必须逐面对应（现在 %d vs %d）" % (
    len(LOADERS),
    len(VIEWS),
)


# ── tui：终端交互主界面 ───────────────────────────────────────────
def _map_csi(seq):
    """CSI 序列 → 逻辑键名。

    终结字节决定是哪一键，前缀数字决定是哪一个（`1;5C` = Ctrl+→，
    `3~` = Delete）。查不到的序列一律返回 "esc" 而不是"忽略"——
    宁可让调用方当 Esc 处理（输入态下取消、别的态下退出），
    也不要把一串没认出来的字节静默吞掉，那会让用户以为键盘坏了。
    """
    if not seq:
        return "esc"
    letters = {
        "A": "up",
        "B": "down",
        "C": "right",
        "D": "left",
        "Z": "shift-tab",
        "H": "home",
        "F": "end",
    }
    if seq[-1] in letters:
        return letters[seq[-1]]
    if seq[-1] == "~":
        num = "".join(c for c in seq[:-1] if c.isdigit())
        tilde = {
            "1": "home",
            "4": "end",
            "3": "del",
            "5": "pgup",
            "6": "pgdn",
            "7": "home",
            "8": "end",
        }
        return tilde.get(num, "esc")
    return "esc"


def _tab_digit(n):
    """第 n 面（1 起）的直达键标；没有键的面返回空串。

    与 web 端 kbd 同一口径：1-9 用数字本身，"0" 代表第 10 面，第 11 面起
    不标。n 越界（视图数变了而这里没跟着变）也返回空串 —— 宁可少一个标，
    不能标一个按了不动的键。
    """
    if n < 1 or n > 10 or n > len(VIEWS):
        return ""
    return "%d" % (n if n < 10 else 0)


class AgentOS_TUI(object):
    """十三视图 TUI，与 web 端 13 个 tab 同名同序。

    键盘
      ↑↓ / PgUp PgDn   移动光标
     Tab / →          下一面        Shift-Tab  上一面
                      （文件面例外：← 是回上一层目录 —— 文件管理器三十年的
                       肌肉记忆；那一面想切回上一屏用 Shift-Tab）
     1..9、0          直达前 10 面（0 = 第 10 面，与 web 端 kbd 一致）
                      第 11-13 面（终端/文件/设置）**没有数字键**，
                      用 Tab/←→ 或下方的输入行切过去 —— 界面上不许
                      标一个按了不动的键。
    回车             当前行明细；Agent 台工具卡运行/审批卡授权；
                     终端面跑输入行里的命令；文件面进目录/读代码
    i 或 /           进输入行（Esc 取消，Enter 提交）
                      —— 仅 Agent 台/终端/文件三面有输入行，别的面
                      按 i 会明说「这一面没有输入行」
     a                 Agent 台授权开关
     s                 Agent 台中止运行
     r                 刷新本面
     q / Ctrl-C / Esc  退出（输入态下 q 与 Esc 都是普通字符）
    """

    def __init__(self, base, color=None, start_vi=0):
        self.base = base
        # 配色：None=按环境自动探测（NO_COLOR / TERM dumb → 无皮）；
        # 显式 True 仍要过环境探测；False=强制旧版无色帧。
        if color is None:
            self.color = _skin_ok()
        else:
            self.color = bool(color) and _skin_ok()
        self.welcomed = False  # 欢迎卡只画第一帧，任意键后退场
        # start_vi：打开落在哪一面。裸 agentos / agentos tui / resume 都从
        # Agent 台起 —— 打开就能按 / 给 Agent 派活（Claude Code 的
        # 「打开→打字」手感），数据面板 Tab 一步可达。
        self.vi = start_vi if 0 <= start_vi < len(VIEWS) else 0
        self.cursor = 0
        self.rows = []
        self.source = ""
        self.detail = None  # (label, value) 或 None
        self.rc = 0
        self.composing = False  # 输入行输入态（所有面共用）
        self.cbuf = ""
        self.cbuf_sel = False  # 回填内容处于「全选」态：首个可见键整体替换
        self.hint = ""
        self.hint_kind = ""  # "no" = 红字
        # 取数函数的第三個返回值：终端/文件两面把自己的 Panel 挂在这里带
        # 回来。写 {} 而不是 None，是为了让「没有 extra」和「extra 是空」在
        # .get() 上同形，少一条会静默走错的分支。
        self.extra = {}

    def load(self):
        try:
            self.rows, self.source, extra = LOADERS[self.vi](self.base)
            self.extra = extra if isinstance(extra, dict) else {}
        except Exception as e:  # noqa: BLE001
            # fail-closed：取数失败如实报，不拿占位数据冒充面板
            self.rows = [("取数失败", str(e)[:110])]
            self.source = "错误（8790 数据不可达）"
            self.extra = {}
        self.cursor = 0
        self.detail = None
        # 切面就清 TUI 层的 hint：把上一面那句红字留在这面，读起来像
        # 这句是冲本面说的。
        self.hint = ""
        self.hint_kind = ""
        # 文件面的 Panel 自己有一份 hint（已进入 / 已在产品根 / 越界原话），
        # 而这一面在渲染时 face 是 None，Panel.hint 一行都不显示 —— 于是
        # 「已在产品根（.），不再往外走」这句关键拒绝写进了代码却永远看不见。
        # 终端面那份 hint 已由「上一条」行自带，这里只补文件面这个哑洞。
        if self.vi == FILE_VI:
            p0 = self.extra.get("panel")
            if p0 is not None and p0.hint:
                self.hint = p0.hint
                self.hint_kind = p0.hint_kind

    def toggle_detail(self):
        if self.detail is not None:
            self.detail = None
        elif 0 <= self.cursor < len(self.rows):
            self.detail = self.rows[self.cursor]

    def read_key(self, timeout=None):
        # 全程 os.read 直读 fd：sys.stdin.read 是缓冲读取，会把
        # \x1b[B 三个字节一次吞进 Python 缓冲，只返回 \x1b，剩下
        # [B 留在 Python 缓冲里，select 监听的是 OS fd，看不见
        # 它们，80ms 超时就把箭头序列误判成 Esc（实测踩坑）。
        # timeout=None 阻塞等键；给出秒数则返回 None（供 busy
        # 时刷屏：动作跑着也要让 transcript 逐行流动起来）。
        fd = sys.stdin.fileno()
        if timeout is not None:
            r, _, _ = select.select([fd], [], [], timeout)
            if not r:
                return None
        b = os.read(fd, 1)
        if not b:
            return None
        ch = b.decode("utf-8", "replace")
        if ch == "\x1b":
            # Esc 单独按与 \x1b[A 箭头序列的区分：等 80ms，没有后续就是 Esc
            r, _, _ = select.select([fd], [], [], 0.08)
            if not r:
                return "esc"
            a = os.read(fd, 1).decode("utf-8", "replace")
            if a == "[":
                # 收整个 CSI：参数字节（0x30-0x3F）→ 中间字节（0x20-0x2F）
                # → 终结字节（0x40-0x7E）。旧写法一个个 os.read 找字母，
                # 遇到 \x1b[5~（PgUp）这类数字参数就把 5 当成终结字符吐掉、
                # 循环继续吃到 ~ 才停，最后查表落在默认值 "esc" 上 ——
                # 于是**按 Page Up 等于退出程序**。这里按 CSI 语法收完再查表。
                seq = ""
                while True:
                    r, _, _ = select.select([fd], [], [], 0.08)
                    if not r:
                        break
                    c = os.read(fd, 1).decode("utf-8", "replace")
                    if not c:
                        break
                    seq += c
                    if "\x40" <= c <= "\x7e":
                        break
                return _map_csi(seq)
            if a == "O":
                bb = os.read(fd, 1).decode("utf-8", "replace")
                return {"A": "up", "B": "down", "C": "right", "D": "left"}.get(
                    bb, "esc"
                )
            return "esc"
        if ch == "\t":
            return "tab"
        if ch in ("\r", "\n"):
            return "enter"
        return ch

    def _hero(self, w):
        """Claude Code 式欢迎卡：只画进 TUI 的第一帧，任意键后退场。

        卡画在 body 区（首条 ─ 分隔线之后），表头/tab 栏/footer 的位置与
        文字一字不动 —— tui_walk 与 cli_gate 的首帧判据（首行含
        「AgentOS TUI」+ 时钟、tab 方括号、footer「视图 N/13」）照旧命中。
        卡内文案不许出现「视图 N/M」形态的字样：footer() 是全帧扫
        FOOT_RE 取第一个命中，早一行命中就把整个解析带偏。
        """
        S = _SKIN_SGR if self.color else None

        def C(*ks):
            # C("brand", "bold", text)：前面的键取 SGR，末参是正文。
            s = ks[-1]
            if not S:
                return s
            return "".join(S[k] for k in ks[:-1]) + s + S["r"]

        W = min(w - 2, 76)
        if W < 44:
            return []
        bar = C("brand", "│  ")
        end = C("brand", "  │")
        blank = bar + " " * (W - 4) + end
        head_t = _clip("✻ Welcome to AgentOS!", W - 4)
        fill_t = " " * max(0, (W - 4) - _dw(head_t))
        tip = _clip("/ 直接给 Agent 派活 · Tab 切数据面板 · 1-9/0 直达 · q 退出", W - 4)
        fill_p = " " * max(0, (W - 4) - _dw(tip))
        rows = []
        for lab, val in (
            ("数据源", self.base),
            ("工作目录", ROOT),
            ("面板", "13 面全走 8790 真数据 · 与 web 端同名同序"),
        ):
            pre = "* " + _pad(lab, 8) + "  "  # 按显示宽对齐三行标签
            plain = _clip(pre + val, W - 4)
            fill = " " * max(0, (W - 4) - _dw(plain))
            ci = len(pre)  # 字符数下标，不是显示宽（CJK 一个字 2 列）
            rows.append(
                bar
                + C("brand", plain[:2])
                + C("cyan", plain[2:ci])
                + plain[ci:]
                + fill
                + end
            )
        return (
            [
                C("brand", "╭" + "─" * (W - 2) + "╮"),
                bar + C("brand", "bold", head_t) + fill_t + end,
                blank,
            ]
            + rows
            + [
                blank,
                bar + C("dim", tip) + fill_p + end,
                C("brand", "╰" + "─" * (W - 2) + "╯"),
            ]
        )

    def paint(self):
        size = shutil.get_terminal_size()
        w = size.columns or 80
        h = size.lines or 24
        S = _SKIN_SGR if self.color else None

        def C(*ks):
            # 同 _hero.C：前面的键取 SGR，末参是正文。
            s = ks[-1]
            if not S:
                return s
            return "".join(S[k] for k in ks[:-1]) + s + S["r"]

        out = ["\x1b[2J\x1b[H"]
        # 首行：cli_gate 的 tty-noarg-enters-tui 钉死「首行含 AgentOS TUI」，
        # tui_walk 的 header_time 吃首行时钟。品牌符与配色随便加，这两样
        # 字面量不能动。先裁后上色（_clip 不认 ANSI，只能裁完再包色码）。
        head = _clip(
            "✻ AgentOS TUI · %s · %s" % (time.strftime("%H:%M:%S"), self.base), w
        )
        ha, hs1, hrest = head.partition(" · ")
        hb, hs2, hc = hrest.partition(" · ")
        if S and hs1 and hs2:
            out.append(C("brand", "bold", ha) + S["dim"] + hs1 + hb + hs2 + hc + S["r"])
        else:
            out.append(head)
        # tab 栏分两行画，每行最多 7 面，面名前那一位是直达键标（1-9、
        # 0=10；第 11 面起无标）。单行十三面是 86 列 —— 80 列终端下
        # _clip 会把最后三面（终端/文件/设置）整段切掉，而那三面恰是本轮
        # 新实装、最该被看见的。两行 58/37 列，80 列放得下，也不挤。
        toks = []
        for i, n in enumerate(VIEWS):
            d = _tab_digit(i + 1)
            label = ("%s %s" % (d, n)) if d else ("  %s" % n)
            toks.append("[%s]" % label if i == self.vi else " %s " % label)
        act_tok = toks[self.vi]
        for r in range(0, len(toks), 7):
            line = _clip(" ".join(toks[r : r + 7]), w)
            if S and act_tok in line:
                line = line.replace(act_tok, C("brand", "bold", act_tok))
            out.append(line)
        hero = self._hero(w) if not self.welcomed and h >= 30 else []
        out.append(C("dim", "─" * min(w, 64)))
        # 欢迎卡占的行数从 body 里扣，footer 必须留在屏内。
        body_h = max(4, h - 12 - len(hero))
        out.extend(hero)
        for i, row in enumerate(self.rows[:body_h]):
            label, value = row[0], row[1]
            meta = row[2] if len(row) > 2 else None
            mark = ">" if i == self.cursor else " "
            if meta and meta.get("sec"):
                line = _clip("■ %s  %s" % (label, value), w)
                out.append(C("brand", "bold", line))
                continue
            if meta and meta.get("head"):
                hl = _clip("  %s %s" % (_pad(label, 20), value), w)
                if S and len(hl) > 22:
                    out.append("  " + C("cyan", hl[2:22]) + hl[22:])
                else:
                    out.append(hl)
                out.append("  " + C("dim", "─" * min(w - 2, 60)))
                continue
            extra = ""
            if meta and meta.get("kind") == "tool":
                extra = "  [回车 运行]"
            elif meta and meta.get("kind") == "approval":
                extra = "  [回车/a 授权开关]"
            line = _clip(
                "%s %s %s%s" % (mark, _pad(_clip(label, 20), 20), str(value), extra), w
            )
            if S:
                for mk in ("  [回车 运行]", "  [回车/a 授权开关]"):
                    if mk in line:
                        line = line.replace(mk, S["dim"] + mk + S["r"])
                if i == self.cursor and line:
                    # 光标行：Claude 橙的「>」，剥 ANSI 后仍是「> 」行首
                    # （tui_walk 的 cursor_line 判据）。
                    line = S["brand"] + S["bold"] + ">" + S["r"] + line[1:]
            out.append(line)
        if len(self.rows) > body_h:
            out.append(C("dim", "  … 共 %d 行" % len(self.rows)))
        out.append(C("dim", "─" * min(w, 64)))
        if self.detail:
            dl, dv = self.detail[0], self.detail[1]
            dline = _clip("明细 · %s" % dl, w)
            # 「明细 ·」必须行首（tui_walk has_detail 的 startswith 判据），
            # 颜色只能上在前缀之后。
            if S and dline.startswith("明细 · "):
                dline = "明细 · " + C("cyan", dline[len("明细 · ") :])
            out.append(dline)
            for chunk in textwrap.wrap(str(dv), max(20, w - 4)):
                out.append(_clip("  " + chunk, w))
            out.append(C("dim", "回车返回"))
        else:
            face = _FACES.get(self.base) if self.vi == AGENT_VI else None
            if self.composing:
                cplain = _clip("composer› " + self.cbuf + "█", w)
                if S:
                    # composer 行剥 ANSI 后必须逐字等于
                    # 「composer› <缓冲>█」（tui_walk COMPOSER_RE 钉死），
                    # 所以色码只能包在两段既有文字外面。
                    out.append(
                        C("brand", "composer› ") + cplain[len("composer› ") : -1] + "█"
                    )
                else:
                    out.append(cplain)
            # hint 有两个来源：Agent 台自己的运行态，和 TUI 自己这层
            # （比如「这一面没有输入行」）。只画 face 的那份，TUI 层
            # 的提示就等于写了永远不会显示的代码。
            hint = face.hint if face is not None else self.hint
            hk = face.hint_kind if face is not None else self.hint_kind
            # 回填全选态必须说出来：行里已经有内容，用户看不出第一个键是
            # 替换还是追加。这句话跟着 composing+cbuf_sel 每帧现算，不落
            # self.hint —— load() 会清 hint，落下去一刷新就没了。
            if self.composing and self.cbuf_sel:
                hint = "输入行已全选：键入整体替换 · Enter 原样重跑 · Esc 取消"
                hk = ""
            if hint:
                line = _clip("hint: " + hint, w)
                if hk == "no":
                    # 红字编码 \x1b[31m 被 tui_walk 的 red_hint 判据钉死，
                    # 不能换成真彩色红。
                    out.append("\x1b[31m" + line + "\x1b[0m")
                else:
                    out.append(C("dim", line))
            if self.vi == AGENT_VI:
                # 数字键位随视图数长：1-9 有单位数键，第 10 面起必须 Tab
                out.append(
                    C(
                        "dim",
                        "↑↓ 移动 · 回车 运行/授权 · a 授权开关 · "
                        "c composer · s 中止 · Tab 切面 · %s 直达 · q 退出"
                        % DIGIT_HINT,
                    )
                )
            elif self.vi == TERM_VI:
                # 终端面的 footer 只讲终端面的事：命令从输入行进，rc 由服务端
                # 回执说了算。不把「r 刷新」抄在这儿 —— 跑完一条命令历史自己
                # 会长，r 在这面是多余动作。
                out.append(
                    C(
                        "dim",
                        "↑↓ 移动 · i 或 / 输命令（Esc 取消）· "
                        "Enter 提交即跑 · Tab 切面 · %s 直达 · q 退出" % DIGIT_HINT,
                    )
                )
            elif self.vi == FILE_VI:
                out.append(
                    C(
                        "dim",
                        "↑↓ 选择 · Enter 进目录/读代码 · ← 回上层 · "
                        "i 或 / 输路径 · Tab 切面 · %s 直达 · q 退出" % DIGIT_HINT,
                    )
                )
            else:
                out.append(
                    C(
                        "dim",
                        "↑↓ 移动 · Tab/→ 切面 · ← 上面 · "
                        "回车 明细 · r 刷新 · %s 直达 · q 退出" % DIGIT_HINT,
                    )
                )
        fplain = _clip(
            "视图 %d/%d · 数据源: %s" % (self.vi + 1, len(VIEWS), self.source), w
        )
        k = fplain.find(" · 数据源: ")
        if S and k > 0:
            sep = " · 数据源: "
            out.append(
                C("brand", "bold", fplain[:k])
                + S["dim"]
                + sep
                + S["r"]
                + C("cyan", fplain[k + len(sep) :])
            )
        else:
            out.append(fplain)
        # out[0] 是清屏序列，不是一行内容。直接 "\n".join(out) 会在
        # \x1b[H 后面多出一个换行 —— 实测 30 行终端的第 1 行永远空白，
        # 所有内容整体下移一行，24 行终端下 footer 被顶到屏外。
        # 前缀单独写，剩下的才 join。
        sys.stdout.write(out[0] + "\n".join(out[1:]) + "\n")
        sys.stdout.flush()

    def _compose_key(self, key):
        # composer 输入态：可见字符打字、Backspace 删、
        # Enter 提交、Esc 取消。q 在输入态是普通字符。
        # 提交去向按所在面分发：Agent 台给 face，终端面经 POST /__term 跑
        # 只读白名单，文件面按路径 ls/read，别的面如实说「这一面没有
        # 输入行」。原先不分面的后果是「在总览按 / 敲一行字，回车没反应」。
        face = _FACES.get(self.base) if self.vi == AGENT_VI else None
        # 终端/文件两面的本地状态由取数函数挂在 extra 里带回来，TUI 不另存
        # 一份 —— 存两份就会漂移，而漂移那天「输入行里敲的路径」和「屏上
        # 正在看的目录」不是同一个。
        panel = self.extra.get("panel") if self.vi in PANEL_VI else None
        if key is None:
            return
        if key == "enter":
            q = self.cbuf
            self.cbuf = ""
            self.cbuf_sel = False
            self.composing = False
            if panel is not None:
                # 输入行的内容落回 Panel：下次再按 i，接着上一次写。
                panel.buf = q
            if not q.strip():
                return
            if face is not None:
                face.submit(q)
            elif panel is not None and self.vi == TERM_VI:
                panel.run_cmd(q)
                self.load()
            elif panel is not None and self.vi == FILE_VI:
                # ".." 是文件管理器三十年的肌肉记忆，必须等于「回上一层」，
                # 不是去访问一个名叫 ".." 的项 —— 后者在 /__fs 上只会报错。
                if q.strip() in ("..", "../"):
                    panel.up_dir()
                else:
                    try:
                        panel.goto(q)
                    except Exception as e:  # noqa: BLE001
                        panel.hint = _short(e, 90)
                        panel.hint_kind = "no"
                self.load()
            else:
                self.hint = "这一面没有输入行"
                self.hint_kind = "no"
        elif key == "esc":
            self.cbuf = ""
            self.cbuf_sel = False
            self.composing = False
            if panel is not None:
                panel.buf = self.cbuf
        elif key in ("\x7f", "\b"):
            # 回填内容默认处于全选态：第一下退格整行清空，与文件管理器 /
            # 浏览器地址栏三十年的肌肉记忆一致；此后才是一格一格删。
            if self.cbuf_sel:
                self.cbuf = ""
                self.cbuf_sel = False
            else:
                self.cbuf = self.cbuf[:-1]
            if panel is not None:
                panel.buf = self.cbuf
        elif isinstance(key, str) and len(key) == 1 and key.isprintable():
            # 全选态下第一个可见键整体替换回填内容，不是追加到末尾。
            # 少了这条，回填 "tools" 后敲 ".." 会拼成 "tools.."，
            # goto("tools..") 必然失败 —— 用户看到的却是「上级按了没反应」。
            if self.cbuf_sel:
                self.cbuf = ""
                self.cbuf_sel = False
            self.cbuf += key
            if panel is not None:
                panel.buf = self.cbuf

    def run(self):
        self.load()
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            sys.stdout.write("\x1b[?25l")  # 隐藏光标
            sys.stdout.flush()
            while True:
                self.paint()
                # 终端/文件两面的 Panel 每帧从 extra 取一次：extra 是取数
                # 函数返回的真身，不另存副本。
                panel = self.extra.get("panel") if self.vi in PANEL_VI else None
                # Agent 台：每帧 poll 运行落账（流式 transcript
                # 逐行流动），再按需短超时等键
                face = _FACES.get(self.base) if self.vi == AGENT_VI else None
                if face is not None:
                    face.poll_run()
                busy = bool(face is not None and face.run_state)
                key = self.read_key(0.25 if (busy or self.composing) else None)
                if self.composing:
                    self._compose_key(key)
                    continue
                if key is None:
                    continue
                self.welcomed = True
                if key in ("q", "\x03", "esc"):
                    break
                elif key == "up":
                    self.cursor = max(0, self.cursor - 1)
                    self.detail = None
                elif key == "down":
                    self.cursor = min(len(self.rows) - 1, self.cursor + 1)
                    self.detail = None
                elif key in ("tab", "right"):
                    self.vi = (self.vi + 1) % len(VIEWS)
                    self.load()
                elif key == "left" and self.vi == FILE_VI and panel is not None:
                    # 文件面：← 回上一层目录（文件管理器三十年的肌肉记忆）。
                    # 这一面切到上一屏用 Shift-Tab —— 两者都是「往左」，
                    # 但一个动目录树、一个动视图，混用一次就够用户骂一次。
                    panel.up_dir()
                    self.load()
                elif key in ("shift-tab", "left"):
                    self.vi = (self.vi - 1) % len(VIEWS)
                    self.load()
                elif key == "enter":
                    meta = None
                    if self.detail is None and 0 <= self.cursor < len(self.rows):
                        row = self.rows[self.cursor]
                        meta = row[2] if len(row) > 2 else None
                    if self.vi == AGENT_VI and meta and meta.get("kind") == "tool":
                        f = _FACES.get(self.base)
                        if f is not None:
                            f.run_tool(meta["name"])
                    elif (
                        self.vi == AGENT_VI and meta and meta.get("kind") == "approval"
                    ):
                        f = _FACES.get(self.base)
                        if f is not None:
                            f.toggle_auth(meta["action"])
                    elif self.vi == FILE_VI and panel is not None and meta is not None:
                        # 文件面的行带 meta.kind：dir/file 进下一层，
                        # up 回上一层。meta 里没有 path 的行（比如代码行）
                        # 落到 toggle_detail，让用户看全文。
                        k = meta.get("kind")
                        if k == "up":
                            panel.up_dir()
                            self.load()
                        elif k in ("dir", "file"):
                            try:
                                panel.goto(meta.get("path"))
                            except Exception as e:  # noqa: BLE001
                                panel.hint = _short(e, 90)
                                panel.hint_kind = "no"
                            self.load()
                        else:
                            self.toggle_detail()
                    else:
                        self.toggle_detail()
                elif key == "a" and self.vi == AGENT_VI:
                    f = _FACES.get(self.base)
                    if f is not None and 0 <= self.cursor < len(self.rows):
                        row = self.rows[self.cursor]
                        meta = row[2] if len(row) > 2 else None
                        if meta and meta.get("kind") == "approval":
                            f.toggle_auth(meta["action"])
                elif key in ("i", "/"):
                    # 只有 Agent 台 / 终端 / 文件三面真有输入行。别的面按 i
                    # 以前是静默无动作 —— 用户分不清「这面没有输入行」和
                    # 「键盘坏了」，所以这里给一句红字，不装作听见了。
                    if self.vi in INPUT_VI:
                        panel = self.extra.get("panel") if self.vi in PANEL_VI else None
                        self.composing = True
                        # 终端/文件两面的输入行接着上次写（上一条命令、
                        # 上一个路径），Agent 台的 composer 从头来。
                        self.cbuf = panel.buf if panel is not None else ""
                        # 回填即全选：键入整体替换、Enter 原样重跑、Esc 取消。
                        # 没有这个态，".." 会拼成上一次的路径后面变成
                        # "tools.."，回上层就废了。
                        self.cbuf_sel = bool(panel is not None and panel.buf)
                    else:
                        self.hint = (
                            "这一面没有输入行（i / 只对 Agent 台、终端、文件有效）"
                        )
                        self.hint_kind = "no"
                elif key == "c" and self.vi == AGENT_VI:
                    self.composing = True
                    self.cbuf = ""
                    self.cbuf_sel = False
                elif key == "s" and self.vi == AGENT_VI:
                    f = _FACES.get(self.base)
                    if f is not None:
                        f.abort()
                elif key in ("1", "2", "3", "4", "5", "6", "7", "8", "9", "0"):
                    # "0" = 第 10 面，与 web 端同一口径
                    # （index.template.html 的 k==='0'?10:+k）。不能并进
                    # 上面的数字公式：int("0")-1 = -1 在 Python 里是合法
                    # 下标（末面），照抄会把第 10 面静默跳到第 13 面。
                    # 判据列 ASCII 数字表而不是 key.isdigit()：isdigit()
                    # 认全角「１」和上标「²」，后者 int() 直接抛 ValueError，
                    # 于是「敲了一个上标字符，TUI 崩了」—— 键盘走查里真实存在。
                    n = 10 if key == "0" else int(key)
                    if n <= len(VIEWS):
                        self.vi = n - 1
                        self.cursor = 0
                        self.detail = None
                        self.load()
                elif key == "r":
                    self.load()
        finally:
            sys.stdout.write("\x1b[?25h\x1b[2J\x1b[H")  # 还原光标/清屏
            sys.stdout.flush()
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        return self.rc


def cmd_tui(args):
    base = base_url(args)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        # 非终端（管道/CI/自动化）：不进 raw 模式，一次性打印十三视图真数据
        rc = 0
        for name, fn in zip(VIEWS, LOADERS):
            print("== %s ==" % name)
            try:
                rows, source, _ = fn(base)
                print("  数据源: %s" % source)
                for row in rows:
                    print("  %-22s %s" % (str(row[0])[:22], row[1]))
                face = _FACES.get(base) if name == VIEWS[AGENT_VI] else None
                if face is not None and face.hint:
                    print("  hint: %s" % face.hint)
            except Exception as e:  # noqa: BLE001
                rc = 1
                print("  取数失败: %s" % e)
            print()
        return rc
    return AgentOS_TUI(
        base, color=not getattr(args, "no_color", False), start_vi=AGENT_VI
    ).run()


# ── 入口 ──────────────────────────────────────────────────────────
def _forward_sub_argv(argv, name):
    """第一个非选项词是 name 时，返回它后面的原样 argv；否则返回 None。

    为什么赶在 argparse 之前自己认：argparse 的 REMAINDER 收 --help 会被
    顶层解析器半路拦下，实测报 `error: unrecognized arguments: --help`
    （rc=2）。那条错指的是转发器自己，可用户想问的是内核/指挥官 —— 于是
    「内核明明有 -p/--mode/--check、leader 明明有 plan/run」被显示成
    「你这儿不支持」。认出来就直接转发，喂给下游的就是用户原样敲的那一串。
    """
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--url", "--port", "--timeout"):
            i += 2  # 这三个吃一个值，跳过值本身
            continue
        if a.startswith("-"):
            i += 1
            continue
        return list(argv[i + 1 :]) if a == name else None
    return None


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    fwd = _forward_sub_argv(argv, "agent")
    if fwd is not None:
        return cmd_agent(argparse.Namespace(argv=fwd))
    fwd = _forward_sub_argv(argv, "leader")
    if fwd is not None:
        return cmd_leader(argparse.Namespace(argv=fwd))
    # 顶层可选参数统一放在 common（add_help=False 的父解析器）里，默认值一律
    # argparse.SUPPRESS —— 写成 None 的话，子命令那一轮解析会把顶层已经拿到
    # 的值覆盖回默认（argparse 已知行为），于是 `agentos --port 9001 tui` 的
    # 端口在子命令解析时被悄悄抹掉、按 8790 去连，报出来的错因还是假的。
    # SUPPRESS 的代价是"没给过的字段不存在"，所以解析完统一补默认值。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--url",
        default=argparse.SUPPRESS,
        help="服务地址（默认 http://127.0.0.1:%d）" % DEFAULT_PORT,
    )
    common.add_argument(
        "--port",
        type=int,
        default=argparse.SUPPRESS,
        help="服务端口（默认 %d；8787 是 headroom 常驻口，serve 拒抢）" % DEFAULT_PORT,
    )
    common.add_argument(
        "--timeout",
        type=float,
        default=argparse.SUPPRESS,
        help="单次执行上限秒（-p 用，默认 %.0f）" % PRINT_TIMEOUT,
    )
    common.add_argument(
        "--no-color",
        dest="no_color",
        action="store_true",
        default=argparse.SUPPRESS,
        help="兼容预留：数据命令全量零 ANSI；TUI 交互面的配色可用它关闭",
    )
    common.add_argument(
        "--output-format",
        dest="output_format",
        choices=("text", "json"),
        default=argparse.SUPPRESS,
        help="-p 的输出格式（默认 text；json 时 stdout 恰好一行、可 json.loads）",
    )

    p = argparse.ArgumentParser(
        prog="agentos",
        parents=[common],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="AgentOS 终端 CLI：驾驶舱统一入口。",
        epilog="常用：\n"
        "  agentos tui                     进交互驾驶舱（TTY 时）\n"
        '  agentos -p "跑一下 livecheck"   headless 一次跑完，'
        "退出码可接 CI\n"
        "  agentos health                  判 __health 的 ok:true\n"
        "  agentos term ls -1 tools        服务端只读跑一条命令\n"
        "  agentos gh repos                 GitHub 只读面（零远端写）\n"
        "  agentos agent -p '把 livecheck 跑完'  "
        "终端 agent：真模型 + 真工具循环\n"
        "  agentos agent --provider opencode -p '...'  "
        "# OpenCode zen 端点（需 AGENTOS_LLM_API_KEY）\n"
        "  agentos sessions / resume       历史与会话恢复\n"
        "  agentos completion zsh          打印 shell 补全脚本\n",
    )
    p.add_argument(
        "-V",
        "--version",
        action="version",
        version="AgentOS %s" % VERSION,
        help="打印 CLI 自身版本（不连服务、不依赖服务活着）",
    )
    # nargs="?"：给了值是需求，没给值是空需求（rc=2）。默认 None 表示"没有
    # -p 这一项"，和"-p 是空的"是两件事 —— 混成一个 None 就没法把 -p 与
    # 子命令混用判成用法错。
    p.add_argument(
        "-p",
        "--print",
        dest="request",
        metavar="需求",
        nargs="?",
        const="",
        default=None,
        help="headless 一次性模式：人话 → allowlist 动作 → 流式执行",
    )
    sub = p.add_subparsers(dest="cmd", metavar="子命令")

    def add(name, help_, fn, **kw):
        sp = sub.add_parser(name, parents=[common], help=help_, **kw)
        sp.set_defaults(func=fn)
        return sp

    sp = add("serve", "起本地服务：默认 8790、占用顺延，不抢 8787", cmd_serve)
    sp.add_argument(
        "--watch-tree", action="store_true", help="轮询真树变动，自动重抽+重构建+热重载"
    )
    sp.add_argument(
        "--poll", type=float, default=None, help="真树轮询间隔秒（常驻建议 15）"
    )
    sp.add_argument("--tree", default=None, help="指定真树路径（自测用）")
    sp.add_argument(
        "--open", dest="open_browser", action="store_true", help="起完自动开浏览器"
    )

    add("health", "判 __health 的 ok:true（rc=0 才是真健康）", cmd_health)
    add(
        "doctor",
        "一键体检：配置/key/端点/服务/门锁，红给处方（对标 CC /doctor）",
        cmd_doctor,
    )
    sp = add("evolve", "框架级自进化一圈：规则账→模型提案→沙箱门验→自动合入", cmd_evolve)
    sp.add_argument("--rule", default=None, help="指定规则 id（默认取最热 CANDIDATE）")
    sp.add_argument("--no-merge", action="store_true", help="只到沙箱全绿（dry-ok）")
    add("open", "浏览器打开驾驶舱首页", cmd_open)
    add("tui", "终端交互主界面（%d 视图走真 8790 数据）" % len(VIEWS), cmd_tui)
    add("resident", "常驻真值门：核 launchd 常驻实例", cmd_resident)
    add("headroom", "headroom 8787 契约门", cmd_headroom)

    sp = add("gates", "转发 tools/gates.sh（门链入口）", cmd_gates)
    sp.add_argument("gates", nargs="*", help="门名，如 resident_gate headroom_gate")

    sp = add("term", "非交互跑一条只读命令（转发 POST /__term）", cmd_term)
    sp.add_argument("argv", nargs=argparse.REMAINDER, help="命令与参数，如 ls -1 tools")

    # gh 单独一个子命令而不是并进 term：term 的本机白名单只有 12 条命令，
    # 没有 gh；而 gh 的只读面在 gh_client 里自己有一道白名单。两套白名单
    # 合成一个只会让「为什么这条被拒」变得答不上来。argv 用 REMAINDER 收，
    # 子功能名（repos/issues/prs/runs/search/repo/whoami/auth）是位置量。
    sp = add(
        "gh", "GitHub 读写面（读直连；写默认拒绝：--yes+配置+TTY 三闸+审计链）", cmd_gh
    )
    sp.add_argument(
        "argv",
        nargs=argparse.REMAINDER,
        help="子功能与参数，如 repos --limit 20 / issues o/r",
    )

    # 转发而不是重声明，理由见 cmd_agent 的 docstring：内核 CLI 是随内核
    # 演进的，这边抄一份解析器，就会出现「内核有这能力而这边报用法错」。
    # add_help=False 是这一行存在的理由：argparse 若自己吞掉 -h/--help，
    # 用户拿到的是「转发器」的帮助（只有 --url/--port 这些壳参数），而内核
    # 真正的 -p/--mode/--check 一个都不在上面 —— 看着像没这个能力。关掉壳
    # 帮助后 --help 落进 REMAINDER，由内核自己答，那才是唯一的真源。
    sp = add(
        "agent",
        "内置终端 agent：模型+真实工具循环（转发 agent_core）",
        cmd_agent,
        add_help=False,
    )
    sp.add_argument(
        "argv",
        nargs=argparse.REMAINDER,
        help="原样转发给 agent_core：-p '...' / --mode plan / "
        "--provider opencode / --root DIR / --max-steps N / "
        "--stream / --yes / --check",
    )

    sp = add(
        "config",
        "读写 ~/.agentos/config.toml（API key / provider / model）",
        cmd_config,
    )
    sp.add_argument(
        "config_cmd",
        choices=("set", "get", "show"),
        help="set 写入 / get 读取 / show 全部",
    )
    sp.add_argument(
        "key",
        nargs="?",
        default="",
        help="键名，如 llm.api_key / llm.provider / llm.model",
    )
    sp.add_argument("value", nargs="?", default="", help="值（仅 set 需要）")

    sp = add("completion", "打印 shell 补全脚本", cmd_completion)
    sp.add_argument("shell", choices=("zsh", "bash"), help="目标 shell")

    add(
        "sessions",
        "列 headless 会话历史（work/agent_sessions/cli-*.jsonl）",
        cmd_sessions,
    )

    sp = add(
        "steer", "老板监督指令：投给一个会话（步骤边界注入，只配送不裁决）", cmd_steer
    )
    sp.add_argument("argv", nargs=argparse.REMAINDER, help="会话id|--last 指令文本")
    sp = add("watch", "老板监督面：计划/指令/会话尾一屏（默认最近会话）", cmd_watch)
    sp.add_argument("--sid", default="", help="会话 id（缺省取最近 agent-*）")
    sp.add_argument("--once", action="store_true", help="打一份即退")

    sp = add(
        "leader",
        "仓内指挥官：目标→里程碑→任务DAG→派发→回收→复盘",
        cmd_leader,
        add_help=False,
    )
    sp.add_argument(
        "argv",
        nargs=argparse.REMAINDER,
        help="plan '目标' [--sid S] / approve <sid> / run <sid> "
        "[--worker CMD] / retro <sid>（转发 tools/leader.py）",
    )

    sp = add("resume", "回放一次会话（TTY 时回灌 Agent 台再进 TUI）", cmd_resume)
    sp.add_argument(
        "session",
        metavar="会话id",
        help="cli-YYYYMMDD-HHMMSS-xxxx（agentos sessions 看列表）",
    )

    args = p.parse_args(argv)
    # SUPPRESS 补账：没显式给过的字段压根不存在，这里统一落默认值，
    # 下游 base_url()/cmd_print()/cmd_serve() 一律能直接取。
    args.url = getattr(args, "url", None)
    args.port = getattr(args, "port", DEFAULT_PORT)
    args.timeout = getattr(args, "timeout", None)
    args.no_color = getattr(args, "no_color", False)
    args.output_format = getattr(args, "output_format", "text")

    if args.request is not None and getattr(args, "cmd", None):
        sys.stderr.write(
            "FAIL: -p/--print 不能和子命令 %s 混用：headless 自己就是一次"
            "完整执行，不是子命令的前缀。\n"
            '  用法: agentos -p "跑一下 livecheck"\n' % args.cmd
        )
        return 2
    if args.request is not None:
        return cmd_print(args)
    if not getattr(args, "cmd", None):
        # 无子命令：TTY 直接进驾驶舱（"agentos" 就该是打开产品）；非 TTY
        # （管道/CI）进 raw 模式等于把控制字符糊进日志，只给用法并判错。
        if sys.stdin.isatty() and sys.stdout.isatty():
            return cmd_tui(args)
        p.print_help(sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
