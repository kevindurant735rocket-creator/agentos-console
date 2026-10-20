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

# ── health：判 __health ok:true ───────────────────────────────────
def cmd_doctor(_args):
    """一键体检（对标 CC /doctor）：配置→key→端点→门锁，红给处方。

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
    # 端点连不上的口径（用户明确要求：连不上就一直试，不换本地模型）
    _nf = str(llm.get("no_fallback") or "").strip().lower() == "true"
    _erm = llm.get("endpoint_retry_max")
    try:
        _erm_n = int(str(_erm).strip()) if str(_erm or "").strip() else 0
    except (TypeError, ValueError):
        _erm_n = 0
    ck(
        "端点重连口径",
        True,
        "no_fallback=%s（%s）· 端点连不上时%s"
        % (
            "true" if _nf else "false",
            "绝不换本地模型" if _nf else "允许本地兜底",
            "一直重试到端点恢复" if (_nf and _erm_n == 0)
            else ("最多重试 %d 次" % _erm_n if _nf else "等 %d 次后切本地" % 6),
        ),
        "想要「连不上就一直等」：agentos config set llm.no_fallback true",
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

    # ④ 本地服务：网页驾驶舱已下线，这项**不再体检**
    #    （原来量的是 8790 常驻服务在不在；它已经不存在了，
    #      留一条永远红的体检项只会让 doctor 天天误报）。

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




def cmd_evolve(args):
    """转 tools/evolve.py：经验账 → 提案 → 沙箱门验 → 自动合入。"""
    argv = ["python3", os.path.join(HERE, "evolve.py")]
    if getattr(args, "rule", None):
        argv += ["--rule", str(args.rule)]
    if getattr(args, "no_merge", False):
        argv += ["--no-merge"]
    return subprocess.run(argv, cwd=ROOT).returncode



def cmd_replay(args):
    """真实会话回放基准：把录下的真实会话用桩重放，引擎开/关做 A/B。

    这条命令回答的是自进化最容易被糊弄过去的问题：**行为真的变好了吗**。
    「规则合入了、门是绿的」证明不了这个；回放同一份真实语料、
    两次跑出同一份成绩单才能。

    --check 走门模式：有回归就非零退出。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import replay_bench
    except ImportError as e:
        sys.stderr.write("回放基准不可用：%s\n" % e)
        return 3
    argv = list(getattr(args, "argv", None) or [])
    return int(replay_bench.main(argv) or 0)


def cmd_selfscan(args):
    """转 tools/selfscan.py：bandit 静态安全自检（`--install` 幂等部署）。"""
    argv = [sys.executable, os.path.join(HERE, "selfscan.py")] + list(
        getattr(args, "argv", None) or []
    )
    return subprocess.run(argv, cwd=ROOT).returncode


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



# ── shell 补全（自包含：脚本文本内嵌在本文件里）───────────────────
# 早先这里读 completions/agentos.<shell>，于是「补全能不能用」变成了装机
# 流程的副作用：没跑过 install.sh 的人敲 `agentos completion zsh` 只拿到
# 一句「脚本缺失」，而 CLI 契约是一个子命令自己就能吐出可用补全。
# 所以脚本文本内嵌 —— 任何人任何时候拿到的都是同一份。
# 门名不在这儿另抄一份：现读 tools/gates.sh 头部的「正确链」，链上加门、
# 改序，补全下一次打印就跟着变，不会出现补全里有、链里没有的幻觉项。
# 终端 AgentOS 的子命令清单（网页驾驶舱与原生 app 已下线，这里只留终端面）。
_AGENTOS_SUBS = (
    ("gates", "转发 tools/gates.sh（门链入口）"),
    ("selfscan", "静态安全自检：bandit 扫 tools/，只判新增 MEDIUM/HIGH（--install 部署）"),
    ("gh", "GitHub 读写面（读直连；写默认拒绝：--yes+配置+TTY 三闸+审计链）"),
    ("agent", "内置终端 agent（模型+真实工具循环）"),
    ("config", "读写 ~/.agentos/config.toml（API key / provider / model）"),
    ("completion", "打印 shell 补全脚本"),
    ("sessions", "列 headless 会话历史"),
    ("steer", "老板监督指令：投给一个会话（步骤边界注入，只配送不裁决）"),
    ("watch", "老板监督面：计划/指令/会话尾一屏"),
    ("leader", "仓内指挥官：目标→里程碑→任务DAG→派发→回收→复盘"),
    ("doctor", "一键体检：配置/key/端点/门锁，红给处方"),
    ("evolve", "框架级自进化一圈：规则账→模型提案→沙箱门验→自动合入"),
    ("replay", "真实会话回放基准：引擎开/关 A/B，量「空口完成」被压住多少"),
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

  # 这个集合必须与 _AGENTOS_SUBS / argparse 的 21 个子命令逐项相等：
  # 2026-10-10 实测这里漏了 gh（用户敲 agentos gh <Tab> 什么都补不出来），
  # 而 --help 里明明有 —— cli_gate 的「补全与真实子命令不漂移」就是抓这个的。
  # 2026-10-19 复测：doctor/evolve（W22 加的子命令）也漏在这串里，
  # 同样「--help 里有、Tab 补不出来」—— 一起补上。
  local subs="gates selfscan gh agent config completion sessions steer watch leader doctor evolve replay"
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
        "  agentos agent                   进终端 agent（真模型 + 真工具循环）\n"
        "  agentos agent -p '把门链跑完'   headless 一次跑完，退出码可接 CI\n"
        "  agentos agent --provider opencode -p '...'  "
        "# OpenCode zen 端点（需 AGENTOS_LLM_API_KEY）\n"
        "  agentos gh repos                 GitHub 只读面（零远端写）\n"
        "  agentos doctor                   一键体检（配置/key/端点/门锁）\n"
        "  agentos evolve                   框架级自进化一圈\n"
        "  agentos replay                   真实会话回放基准（A/B）\n"
        "  agentos gates <门名…>            跑门链\n"
        "  agentos sessions                 历史会话\n"
        "  agentos completion zsh           打印 shell 补全脚本\n",
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
    # 注：顶层 -p/--print（原「人话 → 网页驾驶舱动作」）已随网页面下线。
    # 终端 agent 的 headless 走 `agentos agent -p '需求'`，那条路还在。
    sub = p.add_subparsers(dest="cmd", metavar="子命令")

    def add(name, help_, fn, **kw):
        sp = sub.add_parser(name, parents=[common], help=help_, **kw)
        sp.set_defaults(func=fn)
        return sp



    add(
        "doctor",
        "一键体检：配置/key/端点/门锁，红给处方（对标 CC /doctor）",
        cmd_doctor,
    )
    sp = add("evolve", "框架级自进化一圈：规则账→模型提案→沙箱门验→自动合入", cmd_evolve)
    sp.add_argument("--rule", default=None, help="指定规则 id（默认取最热 CANDIDATE）")
    sp.add_argument("--no-merge", action="store_true", help="只到沙箱全绿（dry-ok）")


    sp = add(
        "selfscan",
        "静态安全自检：bandit 扫 tools/，只判新增 MEDIUM/HIGH（--install 部署）",
        cmd_selfscan,
    )
    sp.add_argument(
        "argv",
        nargs=argparse.REMAINDER,
        help="原样转发给 selfscan.py：--install / --rebuild-baseline / --json P",
    )

    sp = add(
        "replay",
        "真实会话回放基准：引擎开/关 A/B，量「空口完成」被压住多少",
        cmd_replay,
    )
    sp.add_argument(
        "argv",
        nargs=argparse.REMAINDER,
        help="原样转发给 replay_bench.py：--check（门模式）",
    )

    sp = add("gates", "转发 tools/gates.sh（门链入口）", cmd_gates)
    sp.add_argument("gates", nargs="*", help="门名，如 resident_gate headroom_gate")

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


    args = p.parse_args(argv)
    # SUPPRESS 补账：没显式给过的字段压根不存在，这里统一落默认值，
    # 下游 base_url()/cmd_print()/cmd_serve() 一律能直接取。
    args.url = getattr(args, "url", None)
    args.port = getattr(args, "port", DEFAULT_PORT)
    args.timeout = getattr(args, "timeout", None)
    args.no_color = getattr(args, "no_color", False)
    args.output_format = getattr(args, "output_format", "text")

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
