# -*- coding: utf-8 -*-
"""agent_core —— AgentOS 终端 agent 的工具循环内核（只用标准库）。

为什么要有这一层
----------------
在本文件之前，「agent」这个词在本项目里指的是**固定 argv 路由器**：
serve.py 的 ACTIONS 只有 11 条写死的命令，resolve_intent() 把一句人话映射到
其中之一。它能自证、能流式、能被中止，但它做不了「读这个文件、改这两行、
跑一下测试」这类真实工程动作 —— 因为压根没有一个循环把「模型的下一步意图」
变成「一次真实执行、结果回到上下文里」。

本文件补的正是那一环，并且**不新开一套真相**：
  · 工具层尽量复用 serve.py 已有的边界（_fs_sandbox 的 realpath 沙箱、
    TERM_CMDS 的只读白名单、gh_client.run_gh 的只读白名单）；
  · 口试用 OpenAI chat/completions 的 tool_calls，因为本机 CC Switch 的
    127.0.0.1:15799/v1 端点**实测**支持（2026-10-10 实测见 tools/neg_agent_core.py 的
    正例，以及本文件 `--check` 自检）；
  · 每一步都有真实 rc / 真实字节，失败分类显式，绝不 fail-open。

权限模型（对齐 claude code 的思路，但边界比它严）
----------------------------------------------
每个工具有一个 risk 类：read / write。plan 模式下 write 一律拒绝；
accept-edits / full 模式下 write 需要审批者点头；run_cmd 与 gh_read 的
「只读白名单内向量」自动放行，白名单外一律要走审批。

另有**硬阻断清单**，任何模式、任何审批者都不能放行：git push、rm、rmdir、
git reset --hard、dd、mkfs、shutdown、sudo、launchctl…… 理由是本机两条
既存铁律：①「需用户明确说『发』才执行」的对外操作；②`rm -f` 会被 exec
policy 拒绝且拒绝发生在 shell 启动之前，所以 agent 也不该去撞它。

失败分类（上层据此给不同退出码，绝不含混成 rc=0）
-----------------------------------------------
no-provider  解析不到模型端点（env / codex 配置都没有）
provider      端点不可达 / 非 200 / 返回体形状不对
usage        调用方参数不合法（空需求、越界 step 数）
limit        达到 step / 时间 / 输出预算
tool         工具执行层错误（路径越界、命令不在白名单、写盘失败）
approval     审批被拒（这不是失败，是既定行为；回答里如实写「未执行」）

退出码口径（全项目同一套）：0 = agent 给出了最终回答；2 = 用法/预算；
3 = 基础设施（provider / HTTP /  harness）。
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import termios
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import urllib.parse
import shutil
import uuid

# 2026-10-21（TASK2 障碍 2）：provider 层已搬到 tools/agent_provider.py。
# 这里显式 re-export，保持 `from agent_core import resolve_provider` 等
# 既有调用点、以及 vars(agent_core)[名字] 打补丁的自检写法不变。
from agent_provider import (  # noqa: F401
    ROOT,
    VERSION,
    MAX_STEPS,
    CALL_TIMEOUT,
    MAX_TOOL_OUT,
    MAX_FILE_BYTES,
    MAX_LINES,
    NAMED_PROVIDERS,
    _SESSION_BREAK_RE,
    RESPONSES_MAX_OUTPUT_TOKENS,
    RESPONSES_REASONING_EFFORT,
    _DEFAULT_UA,
    AgentError,
    _read_json_file,
    _codex_provider,
    _agentos_config_path,
    _read_agentos_config,
    _write_agentos_config,
    _toml_kv,
    _provider_hint,
    _UNREACHABLE_HINT,
    _bearer_headers,
    _norm_protocol,
    resolve_provider,
    _probe_ollama,
    _as_text,
    _norm_responses_usage,
    _to_responses_input,
    _to_responses_tools,
    _norm_responses,
    _norm_completion,
    Provider,
    Stub,
)

def _sandbox(rel, root=None):
    """把相对路径解析成 root 内的真实路径；越界返回 (None, 理由)。

    为什么必须 realpath 之后再比前缀：os.path.join + startswith 挡不住
    `work/../../etc/passwd`，也挡不住指向 root 外的符号链接。先 realpath
    再比 `root + os.sep` 才是唯一可靠的边界 —— 与 serve.py._fs_sandbox 同款。
    """
    rroot = os.path.realpath(root or ROOT)
    p = (rel or "").strip()
    if not p or p == ".":
        return rroot, None
    real = os.path.realpath(os.path.join(rroot, p))
    if real != rroot and not real.startswith(rroot + os.sep):
        return None, (
            "路径越界：只能动当前项目根内的路径（项目根=%s，收到 %r）" % (rroot, p[:80])
        )
    return real, None


def _sha(path):
    if not path or not os.path.isfile(path):
        return None
    import hashlib

    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for blk in iter(lambda: f.read(65536), b""):
                h.update(blk)
    except OSError:
        return None
    return h.hexdigest()


def _is_loopback(host):
    h = (host or "").strip().lower().rstrip(".")
    return h in ("127.0.0.1", "localhost", "::1", "[::1]") or h.startswith("127.")


def _host_of(url):
    try:
        return urllib.parse.urlparse(str(url or "")).hostname or ""
    except Exception:  # noqa: BLE001
        return ""


# ── 权限模型 ──────────────────────────────────────────────────
RISK_READ = "read"
RISK_WRITE = "write"
MODES = ("plan", "accept-edits", "full")

# 任何模式、任何审批者都不能放行的命令。理由见模块 docstring：
# ① 对外动作要用户本人说「发」；② 破坏性删除不在 agent 的能力面里。
HARD_DENY_HEAD = {
    "rm",
    "rmdir",
    "dd",
    "mkfs",
    "mkfs.ext4",
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "sudo",
    "launchctl",
    "chown",
    "visudo",
    "fdisk",
    "diskutil",
    "csrutil",
    "pkill",
    "killall",
    "shred",
    "truncate",
}
HARD_DENY_GIT_SUB = {"push", "reset", "clean", "filter-branch", "update-ref"}

# 记账类工具：写的是**运行态**（账本/快照），不是项目内容 → 豁免计划锁。
# 理由：计划锁要管的是「不许擅自改项目」，不是「不许记账」；连记账都锁，
# agent 就没法列清单求批准（死锁）。
_BOOKKEEPING_TOOLS = frozenset(("todo",))
# 白名单里唯一能写盘的一条（与 serve.py term_run 的判断同源）
_WRITEISH_CMDS = {"python3"}
# 自由命令面下要拆 -c 载荷再扫的 shell 包装（bash -c "rm -rf x" 一类）
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}


def _payload_deny(payload):
    """shell -c 载荷拆词后按同一张硬阻断清单再扫一遍（尽力而为）。

    拆词扫单词，不做语法分析：`grep rm file` 这种「rm 出现在参数位」会被
    误拦 —— 换个写法即可，方向要的是 fail-closed。这是防手滑的兜底，
    不防蓄意对抗（真对抗的防线是审批与快照）。
    """
    words = [w for w in re.split(r"[^A-Za-z0-9_./\-]+", str(payload).lower()) if w]
    for i, w in enumerate(words):
        b = os.path.basename(w)
        if b in HARD_DENY_HEAD:
            return "硬阻断：shell 载荷里的 %s（破坏性命令不外包执行）" % b
        if b == "git" and i + 1 < len(words) and words[i + 1] in HARD_DENY_GIT_SUB:
            return (
                "硬阻断：shell 载荷里的 git %s（远端写/历史重写，"
                "需用户明确说「发」）" % words[i + 1]
            )
    bases = [os.path.basename(w) for w in words]
    if "find" in bases and "-delete" in words:
        return "硬阻断：shell 载荷里的 find -delete（删文件一律人工确认）"
    return None


def _hard_deny(argv):
    """命中硬阻断清单就回一句**指名道姓**的拒绝理由，否则 None。

    头词按 basename 判（/bin/rm 与 rm 同罪——自由面下绝对路径可达）；
    前缀赋值（FOO=1 rm…）剥掉再判；env 递归剥一层；bash -c 的载荷进
    _payload_deny。任何模式、任何审批、任何开关都不放行这一清单。
    """
    if not isinstance(argv, list) or not argv:
        return None
    low = [str(x).lower() for x in argv]
    while low and re.match(r"^[a-z_][a-z0-9_]*=", low[0]):
        low.pop(0)
    if not low:
        return None
    head = os.path.basename(low[0])
    if head in HARD_DENY_HEAD:
        return (
            "硬阻断：%s 不在任何模式、任何审批下放行"
            "（对外/破坏性动作由用户本人执行）" % head
        )
    if head == "git" and len(low) > 1 and os.path.basename(low[1]) in HARD_DENY_GIT_SUB:
        return "硬阻断：git %s（远端写/历史重写，需用户明确说「发」）" % low[1]
    if head == "find" and "-delete" in low:
        return "硬阻断：find -delete（删文件一律人工确认）"
    if head == "env" and len(low) > 1:
        return _hard_deny(low[1:])
    if head in _SHELLS and len(low) > 2 and low[1] == "-c":
        return _payload_deny(low[2])
    return None


def _gate_write(name, args, mode, appr, why, auto=False):
    """写动作的裁决。返回 None = 放行，字符串 = 拒绝理由。"""
    if mode == "plan":
        return (
            "plan 模式下不执行任何写操作（%s）；要执行请切 "
            "--mode accept-edits 或 full" % why
        )
    if mode == "full":
        return None  # 用户已一次性授权；硬阻断清单仍拦在前面
    if auto:
        # accept-edits 的本义：文件编辑自动放行，其余仍要审批
        return None
    if callable(appr):
        try:
            yes = bool(appr(name, args, why))
        except Exception as e:  # noqa: BLE001
            return "审批器异常，按拒绝处理：%s" % str(e)[:120]
        if not yes:
            return (
                "审批未通过：%s 未执行（这不是失败，是既定行为；"
                "回答里请如实写「未执行」）" % why
            )
        return None
    return (
        "accept-edits 模式下这一步需要审批，但没有审批者"
        "（headless 无 TTY）→ 按拒绝处理：%s" % why
    )


def _safety_flags(acfg):
    """~/.agentos/config.toml 的 [agent] 段 → 安全闸开关（纯函数，W10 自由面）。

    默认**自由面**：以用户要求为中心，全流程不摆无谓的闸。三道**监督闸**
    只在老板显式上锁时生效（他的机器他做主——往哪个方向做主都是他做主）：
      agent.plan_lock = true  → 计划锁开（未审批写锁 + 任务白名单）
      agent.mode_gate = true  → 权限模式开（plan 禁写 / accept-edits 的
                                审批询问 / 写命令与出网裁决）
      agent.cmd_gate  = true  → 命令白名单开（run_cmd 收窄为只读 13 条）
    只有**字面布尔 True** 才上锁（"true"/"1"/"yes" 字符串一律不认——配置里
    一个字符串就能悄悄上锁或解锁，是 SEC E1 堵过的同类洞）；缺键/其它值
    = 关。**硬阻断清单（rm / git push / git reset / find -delete / dd）不在
    开关范围**：那防的是模型的手滑（含路径形态与 shell 载荷），不是老板的
    决定。
    """
    a = (acfg or {}).get("agent") or {}
    return {
        "plan_lock_off": a.get("plan_lock") is not True,
        "mode_gate_off": a.get("mode_gate") is not True,
        "cmd_gate_off": a.get("cmd_gate") is not True,
    }


def _full_access(acfg):
    """~/.agentos/config.toml [agent] full_access=true → 破坏性清单放行。
    缺省关。只有字面布尔 True 才开（字符串不认，与三闸同口径）。"""
    a = (acfg or {}).get("agent") or {}
    return a.get("full_access") is True


def _audit_destructive(ctx, argv):
    """破坏性放行留痕（增益层，写失败静默）。"""
    try:
        d = os.path.join(ROOT, "work")
        os.makedirs(d, exist_ok=True)
        with open(
            os.path.join(d, "destructive_audit.jsonl"), "a", encoding="utf-8"
        ) as f:
            f.write(
                json.dumps(
                    {
                        "ts": round(time.time(), 3),
                        "sid": str(ctx.get("sid") or ""),
                        "mode": str(ctx.get("mode") or ""),
                        "argv": [str(x)[:120] for x in (argv or [])],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
    except OSError:
        pass


def _authorize(spec, args, ctx):
    """None = 放行；字符串 = 拒绝理由（话说全，别让模型去猜）。"""
    name = spec["name"]
    # 子代理工具子集硬闸（W2，fail-closed）：子集写在子 ctx 上；schema 层
    # 过滤只是给模型看的提示，这里才是结构性强制 —— 手搓工具名也出不去。
    subset = ctx.get("tool_subset")
    if isinstance(subset, list) and name not in subset:
        return "工具 %s 不在本次子代理的工具子集里（允许：%s）" % (
            name,
            " ".join(str(s) for s in subset),
        )
    mode = ctx.get("mode") or "full"
    appr = ctx.get("approver")
    if name == "run_cmd":
        argv = args.get("argv")
        if isinstance(argv, str) and argv.strip():
            try:
                argv = shlex.split(argv)
                args["argv"] = argv  # 模型偶发传字符串：按 shell 词法接纳
            except ValueError:
                pass
        why = _hard_deny(argv)
        if why and ctx.get("full_access"):
            _audit_destructive(ctx, argv)  # 本机显式全权：放行但留痕
            why = None
        if why:
            return why
        if not isinstance(argv, list) or not argv:
            return "run_cmd 的 argv 必须是非空数组（第一项是命令名）"
        if not ctx.get("cmd_gate_off"):
            # 显式命令白名单（agent.cmd_gate=true）：只读 13 条，其余拒。
            cmds = _term_cmds()
            if str(argv[0]) not in cmds:
                return "命令不在只读白名单里：%s（可跑：%s）" % (
                    str(argv[0])[:40],
                    " ".join(sorted(cmds)),
                )
            if str(argv[0]) not in _WRITEISH_CMDS:
                return None
        # 自由命令面（默认）或白名单里的写命令（python3）：按写审批裁决 ——
        # 显式 mode_gate 时问审批（headless 无审批者=拒），默认自由面放行。
        if ctx.get("mode_gate_off"):
            return None
        return _gate_write(name, args, mode, appr, "命令（%s）" % argv[0])
    if name == "web_fetch":
        host = _host_of(args.get("url"))
        if not host or _is_loopback(host):
            return None
        if ctx.get("mode_gate_off"):
            return None
        return _gate_write(name, args, mode, appr, "访问项目之外的地址 %s" % host)
    if spec["risk"] == RISK_WRITE:
        # DSP 计划锁：有计划且未审批时，写工具一律锁死（理由带 /approve 指路）。
        # try 包住：dsp.py 缺失/计划文件坏都不许阻断主循环（fail-open），
        # 只在「计划明确存在且未审批」时才锁（fail-closed 只对真计划）。
        # 老板显式关了计划锁（agent.plan_lock=false）则整段跳过。
        if not ctx.get("plan_lock_off"):
            try:
                _dsp_mod = _dsp()
                _sid = str(ctx.get("sid") or "")
                if _sid:
                    _plan, _err = _dsp_mod.load_plan(_sid)
                    if _plan is not None and not _plan.get("approved"):
                        # 记账类工具豁免计划锁：todo 写的是**运行态账本**
                        # （work/todos/<sid>.json），不是项目内容。
                        # 计划锁要管的是「不许擅自改项目」，不是「不许记账」——
                        # 连记账都锁住，agent 就没法列清单求批准（死锁）。
                        if name not in _BOOKKEEPING_TOOLS:
                            return (
                                "计划未审批：%s 未执行（/tasks 看计划，"
                                "/approve 通过后才放行写操作）" % name
                            )
            except Exception:  # noqa: BLE001
                pass
        if ctx.get("mode_gate_off"):
            return None
        return _gate_write(name, args, mode, appr, "写操作", auto=True)
    # DSP 任务白名单：有计划且已审批时，工具必须在当前任务白名单里。
    # 读工具不受白名单约束（侦察自由）；写工具在上一步已过计划锁。
    # 老板显式关了计划锁则白名单同样跳过（两道都是计划面的监督闸）。
    if not ctx.get("plan_lock_off"):
        try:
            _dsp_mod = _dsp()
            _sid = str(ctx.get("sid") or "")
            _tid = str(ctx.get("dsp_task") or "")
            if _sid and _tid:
                _plan, _err = _dsp_mod.load_plan(_sid)
                if _plan is not None and _plan.get("approved"):
                    _why = _dsp_mod.check_tool(_plan, _tid, name, args)
                    if _why:
                        return _why
        except Exception:  # noqa: BLE001
            pass
    return None


# ── 工具层 ────────────────────────────────────────────────────
TOOLS = {}


def _tool(name, risk, description, schema):
    def deco(fn):
        TOOLS[name] = {
            "name": name,
            "risk": risk,
            "fn": fn,
            "description": description,
            "schema": schema,
        }
        return fn

    return deco


def tool_defs():
    """给模型的 tools 数组（OpenAI function calling 形状）。"""
    out = []
    for name in sorted(TOOLS):
        s = TOOLS[name]
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": s["description"],
                    "parameters": s["schema"],
                },
            }
        )
    return out


_S_LISTDIR = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "项目根内的相对路径，默认 '.'"},
        "depth": {
            "type": "integer",
            "description": "1 或 2，默认 1",
            "minimum": 1,
            "maximum": 2,
        },
    },
}


@_tool(
    "list_dir",
    RISK_READ,
    "列目录：名字/是否目录/字节数。只看项目根内，隐藏文件跳过。",
    _S_LISTDIR,
)
def _t_list_dir(a, ctx):
    real, err = _sandbox(a.get("path") or ".", ctx["root"])
    if err:
        return {"ok": False, "error": err}
    if not os.path.isdir(real):
        return {"ok": False, "error": "不是目录：%s" % (a.get("path") or ".")}
    try:
        depth = max(1, min(2, int(a.get("depth", 1) or 1)))
    except Exception:  # noqa: BLE001
        depth = 1
    out = []
    try:
        for name in sorted(os.listdir(real)):
            if name.startswith("."):
                continue
            full = os.path.join(real, name)
            isd = os.path.isdir(full)
            try:
                size = 0 if isd else os.path.getsize(full)
            except OSError:
                size = 0
            row = {"name": name, "dir": isd, "size": size}
            if depth > 1 and isd:
                try:
                    row["children"] = [
                        {"name": x, "dir": os.path.isdir(os.path.join(full, x))}
                        for x in sorted(os.listdir(full))
                        if not x.startswith(".")
                    ][:50]
                except OSError:
                    row["children"] = []
            out.append(row)
    except OSError as e:
        return {"ok": False, "error": "读目录失败：%s" % str(e)[:120]}
    return {
        "ok": True,
        "path": a.get("path") or ".",
        "n": len(out),
        "entries": out[:200],
        "note": "只列前 200 条" if len(out) > 200 else "",
    }


_S_READ = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "项目根内的相对路径"},
        "offset": {
            "type": "integer",
            "description": "起始行号（1 起），默认 1",
            "minimum": 1,
        },
        "limit": {
            "type": "integer",
            "description": "最多读多少行，默认 2000",
            "minimum": 1,
            "maximum": 2000,
        },
    },
    "required": ["path"],
}


@_tool(
    "read_file",
    RISK_READ,
    "按行读一个文本文件，带回显行号。超 256 KiB 拒读并要求先 search 定位。",
    _S_READ,
)
def _t_read_file(a, ctx):
    real, err = _sandbox(a.get("path"), ctx["root"])
    if err:
        return {"ok": False, "error": err}
    if not os.path.isfile(real):
        return {"ok": False, "error": "不是文件：%s" % a.get("path")}
    try:
        sz = os.path.getsize(real)
    except OSError as e:
        return {"ok": False, "error": "取大小失败：%s" % str(e)[:120]}
    if sz > MAX_FILE_BYTES:
        return {
            "ok": False,
            "error": "文件 %d 字节 > 上限 %d；请先用 search 定位，"
            "再带 offset/limit 小范围读" % (sz, MAX_FILE_BYTES),
        }
    try:
        with open(real, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError as e:
        return {"ok": False, "error": "读失败：%s" % str(e)[:120]}
    try:
        off = max(1, int(a.get("offset", 1) or 1))
    except Exception:  # noqa: BLE001
        off = 1
    try:
        lim = max(1, min(MAX_LINES, int(a.get("limit", MAX_LINES) or MAX_LINES)))
    except Exception:  # noqa: BLE001
        lim = MAX_LINES
    seg = lines[off - 1 : off - 1 + lim]
    return {
        "ok": True,
        "path": a.get("path"),
        "n_lines": len(lines),
        "offset": off,
        "showing": len(seg),
        "text": "\n".join("%6d\t%s" % (off + i, s) for i, s in enumerate(seg)),
        "more": (off - 1 + lim) < len(lines),
    }


_S_SEARCH = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "正则（rg 语法）"},
        "path": {"type": "string", "description": "搜索起点，默认项目根"},
        "glob": {"type": "string", "description": "文件过滤，如 *.py"},
        "max_results": {
            "type": "integer",
            "minimum": 1,
            "maximum": 200,
            "description": "默认 50",
        },
    },
    "required": ["pattern"],
}


def _find_rg():
    for c in (shutil.which("rg"), "/opt/homebrew/bin/rg", "/usr/bin/rg"):
        if c and os.path.exists(c):
            return c
    return None


@_tool(
    "search",
    RISK_READ,
    "正则搜索（rg 优先，grep -E 兜底），回 文件:行号:内容。",
    _S_SEARCH,
)
def _t_search(a, ctx):
    pat = a.get("pattern")
    if not pat or not isinstance(pat, str):
        return {"ok": False, "error": "缺 pattern（正则字符串）"}
    real, err = _sandbox(a.get("path") or ".", ctx["root"])
    if err:
        return {"ok": False, "error": err}
    try:
        mx = max(1, min(200, int(a.get("max_results", 50) or 50)))
    except Exception:  # noqa: BLE001
        mx = 50
    rg = _find_rg()
    if rg:
        argv = [rg, "-n", "--no-heading", "--max-count", str(mx)]
        if a.get("glob"):
            argv += ["-g", str(a["glob"])]
        argv += ["--", pat, real]
    else:
        argv = ["grep", "-rnE", "--binary-files=without-match", "--", pat, real]
    try:
        p = subprocess.run(
            argv, capture_output=True, cwd=ctx["root"], timeout=25, env=_CMD_ENV
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "搜索超时（>25s）已杀掉", "rc": None}
    except OSError as e:
        return {"ok": False, "error": "搜索命令起不来：%s" % str(e)[:120]}
    out = ((p.stdout or b"") + (p.stderr or b"")).decode("utf-8", "replace")
    lines = [ln for ln in out.splitlines() if ln.strip()][:mx]
    rel = os.path.relpath(real, ctx["root"])
    return {
        "ok": True,
        "pattern": pat,
        "path": rel,
        "engine": "rg" if rg else "grep",
        "rc": p.returncode,
        "n": len(lines),
        "hits": lines,
        "note": "rc=1 = 没命中（这不是失败）" if p.returncode == 1 else "",
    }


_S_RUNCMD = {
    "type": "object",
    "properties": {
        "argv": {
            "type": "array",
            "items": {"type": "string"},
            "description": '如 ["ls","-1","tools"]；第一项是命令名，'
            "没有 shell（默认自由面任意命令；显式 "
            "cmd_gate 时收窄为只读白名单）",
        },
        "timeout": {
            "type": "integer",
            "minimum": 1,
            "maximum": 30,
            "description": "秒，默认 20",
        },
    },
    "required": ["argv"],
}


@_tool(
    "run_cmd",
    RISK_WRITE,
    "跑一条命令（默认自由面任意命令；显式 cmd_gate 收窄为只读白名单），"
    "回真实 rc 与真实输出。",
    _S_RUNCMD,
)
def _t_run_cmd(a, ctx):
    argv = a.get("argv")
    if isinstance(argv, str) and argv.strip():
        try:
            argv = shlex.split(argv)
            a["argv"] = argv
        except ValueError:
            pass
    if not isinstance(argv, list) or not argv:
        return {"ok": False, "error": "argv 必须是非空数组（第一项是命令名）"}
    argv = [str(x) for x in argv]
    if len(argv) > 24:
        return {"ok": False, "error": "参数过多（>24）"}
    if ctx.get("cmd_gate_off"):
        # 自由命令面（默认）：任意命令，绝对路径或 PATH 解析；硬阻断在
        # 审批层已拦，这里只负责如实起不来就说起不来。
        exe = (
            argv[0]
            if (os.path.isabs(argv[0]) and os.path.exists(argv[0]))
            else shutil.which(argv[0])
        )
        if not exe:
            return {
                "ok": False,
                "error": "命令找不到：%s（PATH 与绝对路径都没命中）" % argv[0][:40],
            }
        real_argv = [exe] + argv[1:]
    else:
        base = _term_cmds().get(argv[0])
        if not base:
            return {
                "ok": False,
                "error": "命令不在只读白名单里：%s（可跑：%s）"
                % (argv[0][:40], " ".join(sorted(_term_cmds()))),
            }
        real_argv = (
            ([base[0], argv[1]] + argv[2:])
            if len(base) == 2
            else (list(base) + argv[1:])
        )
    try:
        tmo = max(1, min(30, int(a.get("timeout", 20) or 20)))
    except Exception:  # noqa: BLE001
        tmo = 20
    t0 = time.time()
    try:
        p = subprocess.run(
            real_argv, capture_output=True, cwd=ctx["root"], timeout=tmo, env=_CMD_ENV
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": "命令超时（>%ds）已杀掉" % tmo,
            "rc": None,
            "dur": round(time.time() - t0, 2),
        }
    except OSError as e:
        return {"ok": False, "error": "命令起不来：%s" % str(e)[:120]}
    text = ((p.stdout or b"") + (p.stderr or b"")).decode("utf-8", "replace")
    return {
        "ok": True,
        "argv": real_argv,
        "rc": p.returncode,
        "out": text[:TERM_MAX_OUT],
        "truncated": len(text) > TERM_MAX_OUT,
        "dur": round(time.time() - t0, 2),
    }


_S_WRITE = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "项目根内的相对路径"},
        "content": {"type": "string", "description": "整文件内容（UTF-8）"},
    },
    "required": ["path", "content"],
}


# ── 写前快照（蒸馏自 cline checkpoints / aider git-undo）──────────────
# write_file 成功落盘前，把原文件复制进 work/agent_snapshots/<sid>/，
# manifest.jsonl 追加一行；/undo 按行逆序恢复、消费即标记。快照失败不阻断
# 主写入（fail-open），但 /undo 会如实报「无快照」。
_SNAP_ROOT = os.path.join(ROOT, "work", "agent_snapshots")


def _snap_before_write(real, ctx):
    try:
        sd = os.path.join(
            ctx.get("snap_dir") or _SNAP_ROOT, str(ctx.get("sid") or "nosid")
        )
        os.makedirs(sd, exist_ok=True)
        existed = os.path.exists(real)
        bk = ""
        if existed:
            bk = "%d__%s" % (int(time.time() * 1000), os.path.basename(real))
            n = 0
            while os.path.exists(os.path.join(sd, bk)):
                n += 1
                bk = "%d_%d__%s" % (int(time.time() * 1000), n, os.path.basename(real))
            shutil.copy2(real, os.path.join(sd, bk))
        with open(os.path.join(sd, "manifest.jsonl"), "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "ts": time.time(),
                        "target": real,
                        "backup": bk,
                        "existed": existed,
                        "consumed": False,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
        return True
    except Exception:  # noqa: BLE001
        return False


@_tool(
    "write_file",
    RISK_WRITE,
    "写一个文件（整文件覆盖或新建）。只限项目根内，回 sha256 前后值。",
    _S_WRITE,
)
def _t_write_file(a, ctx):
    rel, content = a.get("path"), a.get("content")
    if not rel:
        return {"ok": False, "error": "缺 path"}
    if not isinstance(content, str):
        return {"ok": False, "error": "content 必须是字符串"}
    real, err = _sandbox(rel, ctx["root"])
    if err:
        return {"ok": False, "error": err}
    if os.path.isdir(real):
        return {"ok": False, "error": "目标是目录：%s" % rel}
    _snap_before_write(real, ctx)
    before = _sha(real)
    try:
        d = os.path.dirname(real)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(real, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return {"ok": False, "error": "写盘失败：%s" % str(e)[:160]}
    return {
        "ok": True,
        "path": rel,
        "bytes": len(content.encode("utf-8")),
        "sha256": _sha(real),
        "before_sha256": before,
        "created": before is None,
    }


_S_EDIT = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "项目根内的相对路径"},
        "old_string": {
            "type": "string",
            "description": "要被替换掉的原文（必须在文件里唯一）",
        },
        "new_string": {"type": "string", "description": "替换成什么"},
        "replace_all": {
            "type": "boolean",
            "description": "出现多次时是否全部替换，默认 false",
        },
    },
    "required": ["path", "old_string", "new_string"],
}


@_tool(
    "edit_file",
    RISK_WRITE,
    "把文件里唯一出现的 old_string 换成 new_string；不唯一就拒改。",
    _S_EDIT,
)
def _t_edit_file(a, ctx):
    rel, old, new = a.get("path"), a.get("old_string"), a.get("new_string")
    if not rel or not isinstance(old, str) or not isinstance(new, str):
        return {
            "ok": False,
            "error": "path/old_string/new_string 缺一不可，且都必须是字符串",
        }
    if old == "":
        return {"ok": False, "error": "old_string 不能为空（空串会匹配无数处）"}
    real, err = _sandbox(rel, ctx["root"])
    if err:
        return {"ok": False, "error": err}
    if not os.path.isfile(real):
        return {"ok": False, "error": "不是文件：%s" % rel}
    try:
        with open(real, encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return {"ok": False, "error": "读失败：%s" % str(e)[:160]}
    before = _sha(real)
    n = text.count(old)
    if n == 0:
        return {"ok": False, "error": "old_string 一次都没出现（未改动任何字节）"}
    allrep = bool(a.get("replace_all"))
    if n > 1 and not allrep:
        return {
            "ok": False,
            "error": "old_string 出现 %d 次；要么带更多上下文让它唯一，"
            "要么 replace_all=true" % n,
        }
    try:
        with open(real, "w", encoding="utf-8") as f:
            f.write(text.replace(old, new) if allrep else text.replace(old, new, 1))
    except OSError as e:
        return {"ok": False, "error": "写盘失败：%s" % str(e)[:160]}
    return {
        "ok": True,
        "path": rel,
        "replacements": n if allrep else 1,
        "sha256": _sha(real),
        "before_sha256": before,
    }


def _git(a, ctx, argv, timeout=25):
    real = ["git", "-C", ctx["root"]] + [str(x) for x in argv]
    try:
        p = subprocess.run(real, capture_output=True, timeout=timeout, env=_CMD_ENV)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "git 超时（>%ds）" % timeout, "rc": None}
    except OSError as e:
        return {"ok": False, "error": "git 起不来：%s" % str(e)[:120]}
    out = (p.stdout or b"").decode("utf-8", "replace")
    err = (p.stderr or b"").decode("utf-8", "replace")
    if p.returncode != 0:
        return {
            "ok": False,
            "rc": p.returncode,
            "error": (err or out).strip()[:200] or ("git rc=%s" % p.returncode),
            "note": "不是 git 仓库也算失败，如实报",
        }
    return {
        "ok": True,
        "rc": 0,
        "out": out[:TERM_MAX_OUT],
        "truncated": len(out) > TERM_MAX_OUT,
    }


_S_NONE = {"type": "object", "properties": {}}


@_tool("git_status", RISK_READ, "git status --porcelain（真实 rc）", _S_NONE)
def _t_git_status(a, ctx):
    return _git(a, ctx, ["status", "--porcelain=v1", "--branch"])


_S_DIFF = {
    "type": "object",
    "properties": {
        "target": {
            "type": "string",
            "description": "如 HEAD / origin/main ...，默认工作区",
        }
    },
}


@_tool("git_diff", RISK_READ, "git diff（真实 rc）", _S_DIFF)
def _t_git_diff(a, ctx):
    argv = ["diff", "--no-color"]
    if a.get("target"):
        argv.append(str(a["target"]))
    return _git(a, ctx, argv)


_S_LOG = {
    "type": "object",
    "properties": {
        "n": {"type": "integer", "minimum": 1, "maximum": 50, "description": "默认 10"}
    },
}


@_tool("git_log", RISK_READ, "git log --oneline（真实 rc）", _S_LOG)
def _t_git_log(a, ctx):
    try:
        n = max(1, min(50, int(a.get("n", 10) or 10)))
    except Exception:  # noqa: BLE001
        n = 10
    return _git(a, ctx, ["log", "--oneline", "-n", str(n)])


_S_GH = {
    "type": "object",
    "properties": {
        "argv": {
            "type": "array",
            "items": {"type": "string"},
            "description": '不含 gh 本身，如 ["issue","list","--repo","o/r"]',
        },
    },
    "required": ["argv"],
}


def _gh_client():
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import gh_client  # noqa: PLC0415

        return gh_client
    except Exception:  # noqa: BLE001
        return None


@_tool(
    "gh_read", RISK_READ, "跑一条只读 gh 命令；写向量在 gh_client 里结构上被拒。", _S_GH
)
def _t_gh_read(a, ctx):
    argv = a.get("argv")
    if not isinstance(argv, list) or not argv:
        return {"ok": False, "error": 'argv 必须是非空数组，如 ["issue","list"]'}
    gc = _gh_client()
    if gc is None:
        return {"ok": False, "error": "gh_client 导不进来（ tools/ 不在 path？）"}
    try:
        r = gc.run_gh([str(x) for x in argv], timeout=30.0)
    except ValueError as e:
        return {"ok": False, "error": str(e), "denied": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "gh 调用异常：%s" % str(e)[:160]}
    out = {"ok": bool(r.get("ok")), "rc": r.get("rc"), "cmd": r.get("disp")}
    if r.get("ok"):
        out["stdout"] = (r.get("stdout") or "")[:16000]
    else:
        out["error"] = r.get("error")
        out["kind"] = r.get("kind")
    return out


_S_HEALTH = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "要探的地址（必填）：如 http://127.0.0.1:8787/health"}
    },
}


@_tool(
    "health", RISK_READ, "GET 一个回环 /__health，判 ok:true 与真实 HTTP 码", _S_HEALTH
)
def _t_health(a, ctx):
    url = (a.get("url") or "").strip()
    if not url:
        return {
            "ok": False,
            "error": "health 工具要显式给 url（常驻驾驶舱已下线，没有默认地址了）",
        }
    host = _host_of(url)
    if not _is_loopback(host):
        return {"ok": False, "error": "只允许回环地址：%r" % host}
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            st = r.getcode()
            body = r.read(4096).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "服务不可达：%s" % str(e)[:160]}
    try:
        obj = json.loads(body)
    except ValueError:
        obj = None
    healthy = st == 200 and isinstance(obj, dict) and obj.get("ok") is True
    return {
        "ok": healthy,
        "http": st,
        "url": url,
        "body": obj if isinstance(obj, dict) else body[:400],
        "note": "ok=false 表示服务自己说不健康，不是工具失败" if not healthy else "",
    }


_S_FETCH = {
    "type": "object",
    "properties": {"url": {"type": "string", "description": "http/https"}},
    "required": ["url"],
}


# ── GitHub 只读工具（R1，蒸馏自「任务优先参考 GitHub」的全局授权纪律）──
# 全部走 gh_client 只读收口 run_gh（白名单内向量），零远端写。
_S_GH_SEARCH = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "搜索词，如 terminal agent"},
        "kind": {
            "type": "string",
            "description": "repos|issues|code|commits|prs，默认 repos",
        },
        "limit": {"type": "integer", "description": "1-100，默认 10"},
    },
    "required": ["query"],
}
_S_GH_VIEW = {
    "type": "object",
    "properties": {"repo": {"type": "string", "description": "owner/repo"}},
    "required": ["repo"],
}
_S_GH_FILE = {
    "type": "object",
    "properties": {
        "repo": {"type": "string", "description": "owner/repo"},
        "path": {"type": "string", "description": "仓库内文件路径"},
        "ref": {"type": "string", "description": "分支/tag，默认默认分支"},
    },
    "required": ["repo", "path"],
}


def _gh_client_mod():
    # 用 ROOT 定位而不是 __file__：neg_agent_core 会把本文件拷到 work/ 跑，
    # 拷贝件的 __file__ 指向 work/，按 __file__ 找 gh_client 必炸（实测）。
    here = os.path.join(ROOT, "tools")
    if here not in sys.path:
        sys.path.insert(0, here)
    import gh_client

    return gh_client


def _dsp():
    """懒加载 tools/dsp.py（与 _gh_client_mod 同式：拷贝件 __file__ 不可用，
    用 ROOT 定位）。dsp.py 缺失时抛 ImportError，调用方降级走原单循环。"""
    here = os.path.join(ROOT, "tools")
    if here not in sys.path:
        sys.path.insert(0, here)
    import dsp

    return dsp


def _leader_mod():
    """懒加载 tools/leader.py（/goal 的 DAG 编译器；缺失时调用方降级）。"""
    here = os.path.join(ROOT, "tools")
    if here not in sys.path:
        sys.path.insert(0, here)
    import leader

    return leader


def _autonomy():
    """懒加载 tools/autonomy.py（自主执行引擎；缺失时调用方降级）。"""
    here = os.path.join(ROOT, "tools")
    if here not in sys.path:
        sys.path.insert(0, here)
    import autonomy

    return autonomy


def _mcp():
    """懒加载 tools/mcp_client.py（同 _dsp 式）。"""
    here = os.path.join(ROOT, "tools")
    if here not in sys.path:
        sys.path.insert(0, here)
    import mcp_client

    return mcp_client


_MCP_SYNCED = []


def _mcp_sync():
    """把 ~/.agentos/config.toml [mcp] 的服务器挂进 TOOLS（进程内只做一次）。

    只在**用户入口**（REPL / headless）调用 —— --check 与门链不走这里，
    门链的确定性不受用户配置影响。挂载失败如实打 stderr，不阻断主循环。
    """
    if _MCP_SYNCED:
        return
    _MCP_SYNCED.append(True)
    try:
        cfg = _read_agentos_config().get("mcp") or {}
        if not cfg.get("servers"):
            return
        rep = _mcp().sync_tools(TOOLS, cfg, risk_read=RISK_READ, risk_write=RISK_WRITE)
        for line in rep.get("errors") or []:
            sys.stderr.write("[mcp] 挂载失败：%s\n" % line)
        for name, st in sorted((rep.get("servers") or {}).items()):
            sys.stderr.write(
                "[mcp] %s: handshake=%s tools=%d\n"
                % (name, st.get("handshake"), st.get("tools") or 0)
            )
        if rep.get("added"):
            sys.stderr.write(
                "[mcp] 已挂载 %d 个工具：%s\n"
                % (len(rep["added"]), " ".join(sorted(rep["added"])))
            )
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("[mcp] 同步异常（忽略，主循环不受影响）：%s\n" % str(e)[:140])


def _hooks_mw():
    """懒加载 tools/hooks_mw.py（同 _dsp 式）。"""
    here = os.path.join(ROOT, "tools")
    if here not in sys.path:
        sys.path.insert(0, here)
    import hooks_mw

    return hooks_mw


def _skills_mod():
    """懒加载 tools/skills_load.py（同 _dsp 式）。"""
    here = os.path.join(ROOT, "tools")
    if here not in sys.path:
        sys.path.insert(0, here)
    import skills_load

    return skills_load


def _branches_mod():
    """懒加载 tools/branches.py（同 _dsp 式）：会话分支 + 计划自验收。"""
    here = os.path.join(ROOT, "tools")
    if here not in sys.path:
        sys.path.insert(0, here)
    import branches

    return branches


def _steer_mod():
    """懒加载 tools/steer.py（同 _dsp 式）：老板监督指令通道。"""
    here = os.path.join(ROOT, "tools")
    if here not in sys.path:
        sys.path.insert(0, here)
    import steer

    return steer


def _watch_mod():
    """懒加载 tools/watch.py（同 _dsp 式）：老板监督面。"""
    here = os.path.join(ROOT, "tools")
    if here not in sys.path:
        sys.path.insert(0, here)
    import watch

    return watch


_STEER_DIR_OVERRIDE = []


def _steer_dir():
    """指令箱目录（测试注入；空则真目录 work/steer）。"""
    return (
        _STEER_DIR_OVERRIDE[0]
        if _STEER_DIR_OVERRIDE
        else os.path.join(ROOT, "work", "steer")
    )


def _steer_take(ctx):
    """消费老板指令（每个步骤边界调一次）。返回注入文本列表。

    只配送不裁决：注入的是 [老板指令] 消息，执不执行由模型与审批矩阵定。
    任何异常只记 stderr，绝不许监督通道断主循环。
    """
    sid = str(ctx.get("sid") or "")
    if not sid:
        return []
    try:
        lines, total = _steer_mod().pending(_steer_dir(), sid)
        if not lines:
            return []
        texts = [str(x.get("text") or "")[:500] for x in lines]
        _steer_mod().commit(_steer_dir(), sid, total)
        return texts
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("[steer] 指令读取异常（忽略）：%s\n" % str(e)[:120])
        return []


_HOOKS_CFG = []
_SKILL_ROOTS_OVERRIDE = []  # 测试注入；空则用默认两处


def _skill_roots():
    return _SKILL_ROOTS_OVERRIDE or [
        os.path.join(os.path.expanduser("~"), ".agentos", "skills"),
        os.path.join(ROOT, ".agentos", "skills"),
    ]


def _hooks_cfg():
    """读 ~/.agentos/hooks.json（进程内缓存；缺文件/坏了 = None = 全关）。

    为什么缓存：钩子配置是「开机契约」，REPL 长跑期间不重读 —— 改配置重启
    生效（V1 口径，写进 /help）。
    """
    if not _HOOKS_CFG:
        _HOOKS_CFG.append(
            _hooks_mw().load_cfg(
                os.path.join(os.path.expanduser("~"), ".agentos", "hooks.json")
            )
        )
    return _HOOKS_CFG[0]


def _hooks_session(ctx):
    """SessionStart：收集注入上下文。失败只记不拦（开机不许被中间件卡死）。"""
    try:
        cfg = _hooks_cfg()
        if not cfg:
            return
        rep = _hooks_mw().run_event("SessionStart", {"root": ctx.get("root")}, cfg)
        for line in rep.get("context") or []:
            ctx.setdefault("hook_context", []).append(line)
        for e in rep.get("errors") or []:
            sys.stderr.write("[hooks] %s\n" % e)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("[hooks] SessionStart 忽略：%s\n" % str(e)[:120])


_SKILLS_ITEMS = []


def _skills_items():
    """技能清单（进程内缓存；发现失败 = 空表）。"""
    if not _SKILLS_ITEMS:
        try:
            _SKILLS_ITEMS.append(_skills_mod().discover(_skill_roots()))
        except Exception:  # noqa: BLE001
            _SKILLS_ITEMS.append([])
    return _SKILLS_ITEMS[0]


def _skills_auto(text):
    """按 triggers 命中把技能正文注入本轮系统提示（预算 4000、最多 2 个）。

    自动触发是「显式 > 猜」：只认 frontmatter 里写明的 triggers 词做子串
    命中，命中才注入正文；不命中/解析失败一律零改动。
    """
    try:
        hits = _skills_mod().auto_trigger(text, _skills_items())
    except Exception:  # noqa: BLE001
        return ""
    if not hits:
        return ""
    parts = ["本次需求命中的技能（triggers 自动注入）："]
    for name, body in hits:
        parts.append("【技能 %s】\n%s" % (name, body))
    return "\n\n".join(parts)[:4000]


_SKILLS_CACHE = []


def _skills_section():
    """skills 段（进程内缓存；无技能目录 = 空串 = 零改动）。"""
    if not _SKILLS_CACHE:
        try:
            _SKILLS_CACHE.append(
                _skills_mod().render_section(_skills_mod().discover(_skill_roots()))
            )
        except Exception:  # noqa: BLE001
            _SKILLS_CACHE.append("")
    return _SKILLS_CACHE[0]


def _gh_search_argv(query, kind, limit, sort_stars=True):
    """gh search 的 argv。

    两个实测出来的关键点（2026-10-20）：
    1. **多词是 AND**：`context compaction agent conversation summarization`
       这种自然语言长查询，要求 6 个词在 name/description 里**同时**出现 →
       必然 0 命中。GitHub 的 `OR` 语法在这条命令上**不生效**（实测
       `compaction OR summarization` → 0 命中，而 `compaction` → 3 命中）。
       → 真正的解法在调用方做**阶梯放宽**（见 _gh_search_queries）。
    2. **默认排序不是按星**：而这条链路的意图就是「找 1k+ 星标的成熟实现」，
       所以 repos 一律 `--sort stars --order desc`。
    """
    k = (
        kind
        if kind in ("repos", "issues", "code", "commits", "prs", "pulls")
        else "repos"
    )
    argv = [
        "search",
        k,
        str(query),
        "--limit",
        str(int(limit)),
        "--json",
        (
            "fullName,description,stargazersCount,license"
            if k == "repos"
            else "number,title,repository,state"
        ),
    ]
    if sort_stars and k == "repos":
        argv += ["--sort", "stars", "--order", "desc"]
    return argv


def _gh_search_queries(query):
    """把一句自然语言查询拆成**阶梯**：从原样一路放宽到单词。

    为什么要有阶梯（实测）：`gh search` 多词是 AND，agent 拿任务词组去搜
    「成熟实现」几乎必然 0 命中，而 0 命中会被误读成「网络/索引不通」。
    阶梯让同一句查询先按原样试，再逐级丢词，直到有命中为止。

    返回 [(查询, 放宽级别)]：级别 0 = 原样，1 = 前 3 词，2 = 前 2 词，
    3 = 单词（逐个试）。
    """
    q = " ".join(str(query or "").split())
    if not q:
        return []
    words = [w for w in q.split(" ") if w]
    out = [(q, 0)]
    if len(words) > 3:
        out.append((" ".join(words[:3]), 1))
    if len(words) > 2:
        out.append((" ".join(words[:2]), 2))
    for w in words[:3]:
        if w != q:
            out.append((w, 3))
    # 去重（保持顺序）
    seen = set()
    uniq = []
    for qy, lvl in out:
        if qy in seen:
            continue
        seen.add(qy)
        uniq.append((qy, lvl))
    return uniq


def _gh_file_read_argv(repo, path, ref):
    q = "?ref=%s" % ref if ref else ""
    return ["api", "repos/%s/contents/%s%s" % (repo, str(path).lstrip("/"), q)]


@_tool(
    "gh_search",
    RISK_READ,
    "搜 GitHub 成熟实现（repos/issues/code/commits/prs）——任务先看别人"
    "怎么做的，能参考绝不从零写。",
    _S_GH_SEARCH,
)
def _t_gh_search(a, ctx):
    q = (a.get("query") or "").strip()
    if not q:
        return {"ok": False, "error": "缺 query"}
    gc = _gh_client_mod()
    kind = a.get("kind") or "repos"
    limit = int(a.get("limit") or 10)
    parsed = None
    used_q = q
    relaxed = 0
    last_err = ""
    # 阶梯试：原样 → 前 3 词 → 前 2 词 → 单词。
    # 「放宽」这件事必须**说出口** —— 0 命中时 agent 会误判成「网络/索引不通」，
    # 那是错的结论（实测真因是多词 AND）。
    # 跨层级**择优**：某一层命中了 0 星的小仓不算数（这条链路的意图是找
    # 成熟实现），继续往下放宽，直到有一层命中带星的仓库。
    _best_stars = -1
    for qy, lvl in _gh_search_queries(q):
        argv = _gh_search_argv(qy, kind, limit)
        try:
            _p = gc._need_json(gc.run_gh(argv, timeout=30))
        except Exception as e:  # noqa: BLE001
            last_err = str(e)[:160]
            continue
        if not isinstance(_p, list):
            continue
        if not _p:
            continue
        _stars = 0
        for _it in _p:
            if isinstance(_it, dict):
                try:
                    _stars = max(_stars, int(_it.get("stargazersCount") or 0))
                except (TypeError, ValueError):
                    pass
        if _stars > _best_stars:
            _best_stars = _stars
            parsed = _p
            used_q = qy
            relaxed = lvl
        # 收工门槛：**1k+ 星**（这条链路的既定意图就是找成熟实现）。
        # 只命中几百星的小仓不算数 → 继续往下放宽，直到捞到 1k+ 的。
        if _stars >= 1000:
            break
    if parsed is None:
        if last_err:
            return {"ok": False, "error": "gh 搜索失败：%s" % last_err}
        return {
            "ok": False,
            "error": "0 命中（已放宽到单词仍无结果）：%r" % q,
            "hint": "换个词组再搜（词越少越容易命中）；"
            "或直接 gh_repo_view 已知的高星仓名",
        }
    rows = []
    for it in (parsed if isinstance(parsed, list) else [])[:12]:
        if not isinstance(it, dict):
            continue
        if a.get("kind", "repos") == "repos":
            rows.append(
                "%s ★%s · %s"
                % (
                    it.get("fullName", "?"),
                    it.get("stargazersCount", "-"),
                    (it.get("description") or "")[:80],
                )
            )
        else:
            rp = it.get("repository") or {}
            rows.append(
                "#%s %s [%s] · %s"
                % (
                    it.get("number"),
                    it.get("title", "?"),
                    it.get("state", "?"),
                    rp.get("nameWithOwner", "?") if isinstance(rp, dict) else "?",
                )
            )
    return {
        "ok": True,
        "query": q,
        # 命中的是**放宽后**的查询时必须说出口：否则 agent 会以为原词组
        # 精确命中了（那是在骗它 —— 命中的其实是子集词）。
        "used_query": used_q,
        "relaxed": relaxed,
        "hits": len(rows),
        "results": rows or ["（无命中）"],
        "sources": [
            "gh search %s %s%s"
            % (
                kind,
                used_q,
                ("  ← 原查询 %r 0 命中，已放宽" % q) if relaxed else "",
            )
        ],
    }


_S_TODO = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "description": "任务清单（整体覆盖式写：把当前完整清单给出来）。"
            "每项 {id?, text, state}，state ∈ pending/doing/done/dropped",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "text": {"type": "string"},
                    "state": {
                        "type": "string",
                        "enum": ["pending", "doing", "done", "dropped"],
                    },
                },
                "required": ["text"],
            },
        },
        "note": {"type": "string", "description": "给用户看的一行说明（可选）"},
        "action": {
            "type": "string",
            "enum": ["set", "update", "add", "list"],
            "description": "set=整表覆盖（默认）· update=只改点名的项（**推荐**："
            "不会抹掉别的项的进度）· add=追加一项 · list=读回当前清单",
        },
        "changes": {
            "type": "array",
            "description": "action=update/add 时用：[{id?, text?, state?}]",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "text": {"type": "string"},
                    "state": {
                        "type": "string",
                        "enum": ["pending", "doing", "done", "dropped"],
                    },
                },
            },
        },
    },
}


@_tool(
    "todo",
    RISK_WRITE,
    "任务账本：把要做的事列成清单并逐项更新（pending/doing/done/dropped）。"
    "任务超过三步就先列清单，做完一项改一项；"
    "**改单项用 action=update（只改点名的项）**，别用整表覆盖（会抹掉别的项进度）；"
    "清单里还有没结的项时，说「做完了」会被交付闸挡回。",
    _S_TODO,
)
def _t_todo(a, ctx):
    """写/读任务账本。落盘在 work/todos/<sid>.json → 跨压缩、跨重启存活。"""
    try:
        import todo as _todo_mod
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "账本模块不可用：%s" % str(e)[:120]}
    sid = str(ctx.get("sid") or "default")
    _items = a.get("items")
    _changes = a.get("changes")
    _action = str(a.get("action") or "").strip().lower()

    def _read():
        _cur = _todo_mod.load(sid, ctx.get("root"))
        return {
            "ok": True,
            "items": _cur,
            "counts": _todo_mod.counts(_cur),
            "next": _todo_mod.next_item(_cur),
            "panel": _todo_mod.render(_cur),
        }

    # 分派顺序：**先看 action，再看有没有 items**。
    # 2026-10-22 实测踩过的坑：早先写成「items 缺了就当读」，
    # 于是 `todo(action="update", changes=[...])`（**不带 items**，
    # 正是增量写该有的形状）被当成读请求 → 工具返回旧清单、更新被吞，
    # 模型连试三次都看不到效果，只好退回整表覆盖（并把缺陷报了出来）。
    # 现在：update/add 不要求 items；只有「没 action 且没 items/changes」才是读。
    if _action == "list":
        return _read()
    if _action in ("update", "add"):
        _cur = _todo_mod.load(sid, ctx.get("root"))
        _chg = _changes if _changes is not None else _items
        _norm = _todo_mod.update(_cur, _chg or [])
    elif _items is None and not _changes:
        return _read()
    else:
        _norm = _todo_mod._norm(_items or [])
    _p = _todo_mod.save(_norm, sid, ctx.get("root"))
    if not _p:
        return {"ok": False, "error": "账本落盘失败（work/todos 不可写？）"}
    _c = _todo_mod.counts(_norm)
    return {
        "ok": True,
        "written": _p,
        "counts": _c,
        "next": _todo_mod.next_item(_norm),
        "panel": _todo_mod.render(_norm),
    }


@_tool(
    "gh_repo_view",
    RISK_READ,
    "看一个 GitHub 仓库的概况（描述/星/语言/更新时间）。",
    _S_GH_VIEW,
)
def _t_gh_repo_view(a, ctx):
    repo = (a.get("repo") or "").strip()
    if repo.count("/") != 1 or not all(repo.split("/")):
        return {"ok": False, "error": "repo 需要 owner/repo 形式：%r" % repo}
    gc = _gh_client_mod()
    argv = [
        "repo",
        "view",
        repo,
        "--json",
        "nameWithOwner,description,stargazerCount,forkCount,"
        "primaryLanguage,updatedAt,licenseInfo",
    ]
    try:
        parsed = gc._need_json(gc.run_gh(argv, timeout=30))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "gh 读取失败：%s" % str(e)[:160]}
    lic = parsed.get("licenseInfo") or {}
    return {
        "ok": True,
        "repo": parsed.get("nameWithOwner", repo),
        "star": parsed.get("stargazerCount"),
        "license": lic.get("name") if isinstance(lic, dict) else None,
        "language": (parsed.get("primaryLanguage") or {}).get("name")
        if isinstance(parsed.get("primaryLanguage"), dict)
        else None,
        "description": (parsed.get("description") or "")[:160],
        "updatedAt": parsed.get("updatedAt"),
    }


@_tool(
    "gh_file_read",
    RISK_READ,
    "读一个 GitHub 仓库里的文件内容（经 contents API，只读）——参考实现细节用。",
    _S_GH_FILE,
)
def _t_gh_file_read(a, ctx):
    repo = (a.get("repo") or "").strip()
    path = (a.get("path") or "").strip()
    if repo.count("/") != 1 or not all(repo.split("/")):
        return {"ok": False, "error": "repo 需要 owner/repo 形式：%r" % repo}
    if not path or ".." in path.split("/"):
        return {"ok": False, "error": "path 非法：%r" % path}
    gc = _gh_client_mod()
    argv = _gh_file_read_argv(repo, path, (a.get("ref") or "").strip())
    try:
        parsed = gc._need_json(gc.run_gh(argv, timeout=30))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "gh 读取失败：%s" % str(e)[:160]}
    body = parsed.get("content") if isinstance(parsed, dict) else None
    if parsed.get("encoding") == "base64" and body:
        import base64

        try:
            body = base64.b64decode(body).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = "(base64 解码失败)"
    return {"ok": True, "repo": repo, "path": path, "content": str(body or "")[:8000]}


# ── GitHub 自主部署工具（R2，用户明确授权；审计+限速一条不少）──────────
_S_GH_DEPLOY = {
    "type": "object",
    "properties": {
        "dir": {"type": "string", "description": "项目根内要部署的目录"},
        "repo": {
            "type": "string",
            "description": "owner/name（缺 owner 自动拼登录名）",
        },
        "public": {"type": "boolean", "description": "默认私有库"},
        "dry_run": {"type": "boolean", "description": "预演零副作用"},
    },
    "required": ["dir", "repo"],
}


@_tool(
    "web_fetch",
    RISK_READ,
    "GET 一个 URL 的文本。回环直接放行；非回环按当前闸门裁决"
    "（默认自由面直连；显式 mode_gate 时需审批）。",
    _S_FETCH,
)
def _t_web_fetch(a, ctx):
    url = (a.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "缺 url"}
    try:
        scheme = urllib.parse.urlparse(url).scheme
    except Exception:  # noqa: BLE001
        scheme = ""
    if scheme not in ("http", "https"):
        return {"ok": False, "error": "只支持 http/https：%r" % scheme}
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "agentos-agent/%s" % VERSION, "Accept": "*/*"}
        )
        with urllib.request.urlopen(req, timeout=25) as r:
            st = r.getcode()
            body = r.read(65536).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "抓取失败：%s" % str(e)[:160]}
    return {
        "ok": st == 200,
        "http": st,
        "url": url,
        "bytes": len(body),
        "text": body[:MAX_TOOL_OUT],
    }


# ── subagent-as-tool（W2）：子代理做独立子任务 —— 禁嵌套、子集强制、权限继承 ──
# W6 加深：prompts 批量 ≤4 个并行（线程池；每任务独立 Provider 克隆与 ctx，
# 失败隔离、按输入序返回）。并行性由 repl 类门的 barrier 夹具确定性证伪
# （串行化 ⇒ Barrier 超时 ⇒ 判红），不靠摸时钟。
SUBAGENT_MAX_STEPS = 12  # 子代理默认单轮步数；显式 max_steps 照收不钳
SUBAGENT_PARALLEL_MAX = 4  # 一次批量并行的子代理数上限


def _clone_provider(p):
    """并行子代理各拿一份独立 Provider（会话 id/计数不共享互踩）。

    非 Provider 鸭子对象（夹具注入）原样透传 —— 门的 barrier 夹具靠共享
    同一对象跨线程同步，克隆语义不该把它切碎。
    """
    if isinstance(p, Provider):
        return Provider(
            {
                "base_url": p.base_url,
                "model": p.model,
                "headers": p.headers,
                "provider_name": p.provider_name,
                "source": p.source,
                "protocol": p.protocol,
            }
        )
    return p


def _subagent_batch(prompts, ctx, provider, subset, cap, depth, merge=False):
    """并行批量子代理（≤SUBAGENT_PARALLEL_MAX）。单任务失败只记在自己那份
    报告里（fail-isolated），其余照跑；报告按输入序返回。

    merge=True：全部完成后额外一次 provider 调用把各结论汇总成 digest ——
    协作的最后一跳（map → reduce），失败如实留空 digest。
    """
    import concurrent.futures as _cf

    reports = [None] * len(prompts)

    def _one(i, pr):
        sub_ctx = {
            "root": ctx.get("root"),
            "mode": ctx.get("mode"),
            "approver": ctx.get("approver"),
            "sid": ctx.get("sid"),
            "env": ctx.get("env"),
            "snap_dir": ctx.get("snap_dir"),
            "history": [],
            "subagent_depth": depth + 1,
            "tool_subset": subset,
            "plan_lock_off": ctx.get("plan_lock_off"),
            "full_access": ctx.get("full_access"),
            "mode_gate_off": ctx.get("mode_gate_off"),
            "cmd_gate_off": ctx.get("cmd_gate_off"),
        }
        try:
            res = run_agent(
                pr, ctx=sub_ctx, provider=_clone_provider(provider), max_steps=cap
            )
        except AgentError as e:
            reports[i] = {
                "prompt": pr[:40],
                "ok": False,
                "summary": "子代理失败 [%s] %s" % (e.kind, str(e)[:120]),
                "steps": None,
                "rc": None,
                "limit": False,
            }
            return
        except Exception as e:  # noqa: BLE001
            reports[i] = {
                "prompt": pr[:40],
                "ok": False,
                "summary": "子代理异常：%s" % str(e)[:120],
                "steps": None,
                "rc": None,
                "limit": False,
            }
            return
        summary = str(res.get("answer") or "").strip()
        if not summary and res.get("kind") == "limit":
            summary = "（预算用尽：%s）" % str(res.get("error"))[:100]
        reports[i] = {
            "prompt": pr[:40],
            "ok": bool(res.get("ok")),
            "summary": summary or "（无终答）",
            "steps": res.get("steps"),
            "rc": res.get("rc"),
            "limit": res.get("kind") == "limit",
        }

    with _cf.ThreadPoolExecutor(
        max_workers=min(SUBAGENT_PARALLEL_MAX, len(prompts))
    ) as _ex:
        _futs = [_ex.submit(_one, i, pr) for i, pr in enumerate(prompts)]
        for _f in _futs:
            _f.result()  # 异常已在 _one 内兜住；这里只为汇合
    out = {
        "ok": True,
        "parallel": True,
        "count": len(prompts),
        "results": reports,
        "summary": "并行 %d 个子代理全部返回（%d 成功）"
        % (len(prompts), sum(1 for r in reports if r and r.get("ok"))),
    }
    if merge:
        ok_rs = [r for r in reports if r and r.get("ok") and r.get("summary")]
        digest = ""
        if ok_rs:
            try:
                mp = (
                    "把下面 %d 个子代理的结论汇总成一段事实摘要（保留关键结论"
                    "与未决项；不寒暄、不编造）：\n\n" % len(ok_rs)
                ) + "\n\n".join("- %s" % r["summary"] for r in ok_rs)
                comp = _clone_provider(provider).chat(
                    [{"role": "user", "content": mp}], tools=None, stream=False
                )
                digest = str((comp or {}).get("content") or "").strip()
            except Exception:  # noqa: BLE001
                digest = ""
        out["digest"] = digest
    return out


_SCRATCH_CAP = 8192
_SCRATCH_TAIL = 1500


def _scratch_path(root, sid):
    return os.path.join(str(root or ""), "work", "scratch", "%s.md" % str(sid or ""))


def _scratch_append(root, sid, text):
    if not sid or not root:
        return
    try:
        p = _scratch_path(root, sid)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        cur = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
        cur += "\n## SEG\n%s\n" % str(text or "")[:2000]
        b = cur.encode("utf-8")
        if len(b) > _SCRATCH_CAP:
            cur = b[-_SCRATCH_CAP:].decode("utf-8", "ignore")
        with open(p, "w", encoding="utf-8") as f:
            f.write(cur)
    except OSError:
        pass


def _subagent_chain(prompts, ctx, provider, subset, cap, depth):
    """顺序交接链：第 i 段 prompt 追加「上一阶段结论」；任一段失败即停
    （fail-closed，如实报 stopped_at 与已完成段）——交接内容来自上一段的
    真终答，不是编排层编的。"""
    reports = []
    prev = ""
    _sid = str(ctx.get("sid") or "")
    _bb = []
    for i, pr in enumerate(prompts):
        _needs = pr[1] if isinstance(pr, tuple) else []
        pr = pr[0] if isinstance(pr, tuple) else pr
        _tail = "\n".join(_bb)[-_SCRATCH_TAIL:]
        if _tail:
            full = "%s\n\n上一阶段结论：%s\n\n[共享黑板·前面段结论]\n%s" % (
                pr,
                prev,
                _tail,
            )
        else:
            full = pr if not prev else ("%s\n\n上一阶段结论：%s" % (pr, prev))
        _missing = [k for k in _needs if k not in _tail]
        if _missing:
            reports.append(
                {
                    "prompt": pr[:40],
                    "ok": False,
                    "summary": "缺交接钥匙：%s" % ",".join(_missing),
                    "steps": None,
                    "rc": None,
                }
            )
            return {
                "ok": False,
                "chain": True,
                "stopped_at": i,
                "results": reports,
                "summary": "链在第 %d 段缺交接钥匙（缺：%s），不开工"
                % (i + 1, ",".join(_missing)[:60]),
            }
        sub_ctx = {
            "root": ctx.get("root"),
            "mode": ctx.get("mode"),
            "approver": ctx.get("approver"),
            "sid": ctx.get("sid"),
            "env": ctx.get("env"),
            "snap_dir": ctx.get("snap_dir"),
            "history": [],
            "subagent_depth": depth + 1,
            "tool_subset": subset,
            "plan_lock_off": ctx.get("plan_lock_off"),
            "full_access": ctx.get("full_access"),
            "mode_gate_off": ctx.get("mode_gate_off"),
            "cmd_gate_off": ctx.get("cmd_gate_off"),
        }
        try:
            res = run_agent(
                full, ctx=sub_ctx, provider=_clone_provider(provider), max_steps=cap
            )
        except AgentError as e:
            reports.append(
                {
                    "prompt": pr[:40],
                    "ok": False,
                    "summary": "子代理失败 [%s] %s" % (e.kind, str(e)[:120]),
                    "steps": None,
                    "rc": None,
                }
            )
            return {
                "ok": False,
                "chain": True,
                "stopped_at": i,
                "results": reports,
                "summary": "链在第 %d 段失败（%s）" % (i + 1, str(e)[:80]),
            }
        except Exception as e:  # noqa: BLE001
            reports.append(
                {
                    "prompt": pr[:40],
                    "ok": False,
                    "summary": "子代理异常：%s" % str(e)[:120],
                    "steps": None,
                    "rc": None,
                }
            )
            return {
                "ok": False,
                "chain": True,
                "stopped_at": i,
                "results": reports,
                "summary": "链在第 %d 段异常（%s）" % (i + 1, str(e)[:80]),
            }
        summary = str(res.get("answer") or "").strip()
        if not summary and res.get("kind") == "limit":
            summary = "（预算用尽：%s）" % str(res.get("error"))[:100]
        reports.append(
            {
                "prompt": pr[:40],
                "ok": bool(res.get("ok")),
                "summary": summary or "（无终答）",
                "steps": res.get("steps"),
                "rc": res.get("rc"),
            }
        )
        if not res.get("ok"):
            return {
                "ok": False,
                "chain": True,
                "stopped_at": i,
                "results": reports,
                "summary": "链在第 %d 段停（%s）" % (i + 1, (summary or "")[:60]),
            }
        prev = summary or ""
        _bb.append(prev)
        _scratch_append(ctx.get("root"), _sid, prev)
    return {
        "ok": True,
        "chain": True,
        "stopped_at": None,
        "results": reports,
        "summary": "链 %d 段全部完成" % len(reports),
    }


_S_SUBAGENT = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "description": "子任务：一句话说清目标与验收；子代理没有本会话"
            "历史，需要的信息都要写进 prompt",
        },
        "max_steps": {
            "type": "integer",
            "description": "步数预算 1..%d，默认 8" % SUBAGENT_MAX_STEPS,
        },
        "tools": {
            "type": "array",
            "items": {"type": "string"},
            "description": "允许子代理使用的工具名子集；缺省=除 subagent "
            "外全部。写类工具仍逐条走外层同款审批矩阵",
        },
        "prompts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "并行批量（与 prompt 二选一）：一次派最多 "
            "%d 个子任务同时跑，按输入序返回 results" % SUBAGENT_PARALLEL_MAX,
        },
        "chain": {
            "type": "array",
            "description": "顺序交接链（≤%d 段）：段可是字符串，或"
            "{prompt, needs}（needs 是本段开工前必须已在黑板里的钥匙，"
            "缺钥匙不开工）；段结论进共享黑板，自动带进后面各段；"
            "任一段失败即停并如实报 stopped_at" % SUBAGENT_PARALLEL_MAX,
        },
        "merge": {
            "type": "boolean",
            "description": "prompts 批量时置 true：全部完成后额外一次汇总"
            "调用产出 digest（失败如实留空）",
        },
    },
    "required": ["prompt"],
}


@_tool(
    "subagent",
    RISK_READ,
    "派子代理做独立研究子任务（禁再嵌套；无会话历史；工具子集可收窄"
    "不可放宽；写操作照走外层审批矩阵）。单发 prompt 或 prompts 批量"
    "并行（≤4）。返回 {summary, steps, rc} 或批量 {results}。",
    _S_SUBAGENT,
)
def _t_subagent(a, ctx):
    depth = int(ctx.get("subagent_depth") or 0)
    if depth > 0:
        return {
            "ok": False,
            "executed": False,
            "error": "禁止嵌套：子代理里不能再派子代理（V1 一层，fail-closed）",
        }
    prompt = str(a.get("prompt") or "").strip()
    if not prompt and a.get("prompts") is None and a.get("chain") is None:
        return {
            "ok": False,
            "executed": False,
            "error": "subagent 需要非空 prompt（或 prompts 批量 / chain 链）",
        }
    provider = ctx.get("_provider")
    if provider is None:
        return {
            "ok": False,
            "executed": False,
            "error": "subagent 拿不到 provider（只有主循环内可用）",
        }
    try:
        want = int(a.get("max_steps") or 8)
    except (TypeError, ValueError):
        want = 8
    cap = max(1, want or SUBAGENT_MAX_STEPS)
    raw = a.get("tools")
    if raw is None:
        subset = sorted(n for n in TOOLS if n != "subagent")
    else:
        if not isinstance(raw, list) or not raw:
            return {
                "ok": False,
                "executed": False,
                "error": "tools 必须是非空数组或省略（子代理不能空手）",
            }
        names = set(map(str, raw))
        bad = sorted(names - set(TOOLS))
        if bad:
            return {
                "ok": False,
                "executed": False,
                "error": "未知工具名：%s（可用：%s）"
                % (" ".join(bad), " ".join(sorted(TOOLS))),
            }
        subset = sorted(names - {"subagent"})
    _plist = a.get("prompts")
    if _plist is not None:
        if not isinstance(_plist, list) or not _plist:
            return {
                "ok": False,
                "executed": False,
                "error": "prompts 必须是非空数组（≤%d 个）" % SUBAGENT_PARALLEL_MAX,
            }
        if len(_plist) > SUBAGENT_PARALLEL_MAX:
            return {
                "ok": False,
                "executed": False,
                "error": "并行子代理一次最多 %d 个（得 %d）"
                % (SUBAGENT_PARALLEL_MAX, len(_plist)),
            }
        _plist = [str(x).strip() for x in _plist]
        if not all(_plist):
            return {"ok": False, "executed": False, "error": "prompts 里有空白项"}
        return _subagent_batch(
            _plist, ctx, provider, subset, cap, depth, merge=bool(a.get("merge"))
        )
    _chain = a.get("chain")
    if _chain is not None:
        if not isinstance(_chain, list) or not _chain:
            return {
                "ok": False,
                "executed": False,
                "error": "chain 必须是非空数组（≤%d 段）" % SUBAGENT_PARALLEL_MAX,
            }
        if len(_chain) > SUBAGENT_PARALLEL_MAX:
            return {
                "ok": False,
                "executed": False,
                "error": "chain 一次最多 %d 段（得 %d）"
                % (SUBAGENT_PARALLEL_MAX, len(_chain)),
            }
        _norm = []
        for _x in _chain:
            if isinstance(_x, str):
                _x = _x.strip()
                if not _x:
                    return {
                        "ok": False,
                        "executed": False,
                        "error": "chain 里有空白段",
                    }
                _norm.append((_x, []))
            elif isinstance(_x, dict):
                _pr = str(_x.get("prompt") or "").strip()
                _nd = _x.get("needs") or []
                if (
                    not _pr
                    or not isinstance(_nd, list)
                    or len(_nd) > 8
                    or not all(isinstance(_k, str) and _k.strip() for _k in _nd)
                ):
                    return {
                        "ok": False,
                        "executed": False,
                        "error": "chain 段须是字符串或{prompt, needs[≤8非空串]}",
                    }
                _norm.append((_pr, [str(_k).strip() for _k in _nd]))
            else:
                return {
                    "ok": False,
                    "executed": False,
                    "error": "chain 段须是字符串或{prompt, needs}",
                }
        return _subagent_chain(_norm, ctx, provider, subset, cap, depth)
    sub_ctx = {
        "root": ctx.get("root"),
        "mode": ctx.get("mode"),
        "approver": ctx.get("approver"),
        "sid": ctx.get("sid"),
        "env": ctx.get("env"),
        "snap_dir": ctx.get("snap_dir"),
        "history": [],
        "subagent_depth": depth + 1,
        "tool_subset": subset,
        "plan_lock_off": ctx.get("plan_lock_off"),
        "full_access": ctx.get("full_access"),
        "mode_gate_off": ctx.get("mode_gate_off"),
        "cmd_gate_off": ctx.get("cmd_gate_off"),
    }
    try:
        res = run_agent(prompt, ctx=sub_ctx, provider=provider, max_steps=cap)
    except AgentError as e:
        return {
            "ok": False,
            "executed": False,
            "error": "子代理失败 [%s] %s" % (e.kind, str(e)[:140]),
        }
    summary = str(res.get("answer") or "").strip()
    if not summary and res.get("kind") == "limit":
        summary = "（子代理步数预算用尽：%s）" % str(res.get("error"))[:120]
    return {
        "ok": bool(res.get("ok")),
        "summary": summary or "（子代理没有产出终答）",
        "steps": res.get("steps"),
        "rc": res.get("rc"),
        "limit": res.get("kind") == "limit",
        "tools_used": [u.get("tool") for u in res.get("tools_used") or []],
    }


# ── 终端白名单与运行环境（与 serve.py 同源，不另起一套真相） ────
TERM_CANDIDATES = {
    "pwd": ["/bin/pwd", "/usr/bin/pwd"],
    "ls": ["/bin/ls", "/usr/bin/ls"],
    "cat": ["/bin/cat", "/usr/bin/cat"],
    "wc": ["/usr/bin/wc", "/bin/wc"],
    "head": ["/usr/bin/head", "/bin/head"],
    "tail": ["/usr/bin/tail", "/bin/tail"],
    "grep": ["/usr/bin/grep", "/bin/grep"],
    "find": ["/usr/bin/find", "/bin/find"],
    "stat": ["/usr/bin/stat", "/bin/stat"],
    "date": ["/bin/date", "/usr/bin/date"],
    "whoami": ["/usr/bin/whoami", "/bin/whoami"],
    "git": ["/usr/bin/git", "/bin/git"],
    "python3": ["/opt/miniconda3/bin/python3", "/usr/bin/python3"],
}
TERM_MAX_OUT = 32768  # 单次工具回灌上限
TERM_TIMEOUT = 20  # run_cmd 默认超时
MAX_TOTAL_SECONDS = int(os.environ.get("AGENTOS_MAX_TOTAL_SECONDS", "0"))  # 0=无上限（用户要求不设工作时间上线）
MAX_REPEAT = 3  # 同一步原样连做这么多次还没进展 → 判 limit
TEXT_REPEAT = 3  # 同一句话（非空终稿内容）连说这么多次还没进展 → 判 limit
TOOL_FAIL_STREAK = 6  # 同一工具连续失败/被拒这么多次还没进展 → 判 limit

_TERM_CACHE = None
_TERM_UNRESOLVED = []


def _term_cmds():
    """命令名 → 真实 argv 前缀。绝对路径优先，PATH 兜底，结果缓存。

    与 serve.py 的 TERM_CMDS 逐字同源：那边是静态模块体，这边是库、要可
    重入，做成懒加载 + 缓存。两边口径一旦漂移，_selftest 的 term_cmds
    用例会红 —— 所以别只改一处。
    """
    global _TERM_CACHE
    if _TERM_CACHE is not None:
        return _TERM_CACHE
    out, bad = {}, []
    for name, cands in TERM_CANDIDATES.items():
        hit = next((c for c in cands if os.path.exists(c)), None)
        if not hit:
            hit = shutil.which(name)
        if not hit:
            bad.append(name)
            continue
        out[name] = [hit, "-c"] if name == "python3" else [hit]
    _TERM_UNRESOLVED = bad
    _TERM_CACHE = out
    return out


# 子进程环境：只留必要变量，**一律不带 token**。run_cmd / git 都走这里，
# 免得哪天工具把 Authorization 打进 stdout，再被回灌进模型上下文。
_CMD_ENV_KEYS = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "USER",
    "LOGNAME",
    "SHELL",
    "TZ",
)
_CMD_ENV = {k: os.environ[k] for k in _CMD_ENV_KEYS if k in os.environ}
_CMD_ENV.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
_CMD_ENV["GIT_PAGER"] = "cat"  # 别让 git log 卡在分页器上
_CMD_ENV["GIT_TERMINAL_PROMPT"] = "0"  # 无人值守时绝不弹凭据
_CMD_ENV["NO_COLOR"] = "1"


def _cap_output(text, limit=MAX_TOOL_OUT):
    """超长截断，但**必须留下「被截断了」这句话**，不静默丢字节。"""
    if text is None:
        text = ""
    text = str(text)
    if len(text) <= limit:
        return text
    tag = "\n...[truncated %d/%d bytes：输出超限，已截断；要全文请缩小范围]"
    keep = max(1, limit - (len(tag) + 24))
    return text[:keep] + (tag % (len(text), limit))


def _cap_result(res):
    """工具结果回灌前的统一瘦身：超长字段截断并显式标注。"""
    if not isinstance(res, dict):
        return res
    out = dict(res)
    for k in ("out", "text"):
        if isinstance(out.get(k), str) and len(out[k]) > MAX_TOOL_OUT:
            out[k] = _cap_output(out[k])
            out["truncated"] = True
    hits = out.get("hits")
    if isinstance(hits, list) and len(hits) > 200:
        out["hits"] = hits[:200]
        out["truncated"] = True
        out["note"] = "hits 只回前 200 条（原 %d 条）" % len(hits)
    return out


# ── 工具执行收口：找表 → 权限裁决 → 执行 → 瘦身 ───────────────
def exec_tool(name, args, ctx):
    """执行一个工具，返回 (result, executed)。

    executed=False 表示这一步**什么都没发生**：未知工具、参数不是对象、
    权限拒绝。故意不抛异常 —— 循环要接着跑，把「为什么没做」原样回给
    模型，让它换路子，而不是让整个任务崩掉。
    注意 executed=True + ok=False 是另一种情况：工具**跑了**，它自己在
    沙箱里说了不（越界路径、old_string 没找到）。这一步有返回、无副作用，
    如实回灌即可 —— 别把它和「压根没跑」混为一谈。
    """
    if not isinstance(name, str) or not name:
        return {"ok": False, "error": "工具名不能为空", "executed": False}, False
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return {
            "ok": False,
            "error": "工具参数必须是 JSON object：%r" % str(args)[:120],
            "executed": False,
        }, False
    spec = TOOLS.get(name)
    if spec is None:
        return {
            "ok": False,
            "error": "未知工具 %r（可用：%s）" % (name, " ".join(sorted(TOOLS))),
            "executed": False,
        }, False
    why = _authorize(spec, args, ctx)
    if why:
        kind = "approval" if ("审批" in why or "plan 模式" in why) else "tool"
        return {
            "ok": False,
            "error": why,
            "tool": name,
            "kind": kind,
            "executed": False,
        }, False
    # hooks 中间件（W4）：PreToolUse 拦截 / 上下文收集；无配置即全关。
    try:
        _hcfg = _hooks_cfg()
    except Exception as e:  # noqa: BLE001
        _hcfg = None
        sys.stderr.write("[hooks] 中间件不可用（忽略）：%s\n" % str(e)[:120])
    if _hcfg:
        try:
            _pre = _hooks_mw().run_event(
                "PreToolUse",
                {"tool": name, "args": args, "root": ctx.get("root")},
                _hcfg,
            )
        except Exception as e:  # noqa: BLE001
            return {
                "ok": False,
                "tool": name,
                "kind": "hook",
                "executed": False,
                "error": "PreToolUse 中间件异常（fail-closed）：%s" % str(e)[:140],
            }, False
        if _pre.get("block"):
            return {
                "ok": False,
                "tool": name,
                "kind": "hook",
                "executed": False,
                "error": "被 PreToolUse 钩子拦截：%s"
                % str(_pre.get("reason") or "")[:200],
            }, False
        for _line in _pre.get("context") or []:
            ctx.setdefault("hook_context", []).append(_line)
        for _e in _pre.get("errors") or []:
            sys.stderr.write("[hooks] %s\n" % _e)
    try:
        res = spec["fn"](args, ctx)
    except AgentError as e:
        return {
            "ok": False,
            "error": "%s：%s" % (e.kind, e.msg),
            "tool": name,
            "executed": False,
        }, False
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "error": "工具内部异常：%s: %s" % (type(e).__name__, str(e)[:200]),
            "tool": name,
            "executed": False,
        }, False
    if not isinstance(res, dict):
        res = {"ok": True, "result": str(res)[:2000]}
    res.setdefault("tool", name)
    res.setdefault("executed", True)
    # hooks PostToolUse（W4）：只记不拦 —— 已发生的事不许被中间件改写成没发生。
    if _hcfg:
        try:
            _post = _hooks_mw().run_event(
                "PostToolUse",
                {
                    "tool": name,
                    "args": args,
                    "ok": bool(res.get("ok")),
                    "root": ctx.get("root"),
                },
                _hcfg,
            )
            for _line in _post.get("context") or []:
                ctx.setdefault("hook_context", []).append(_line)
            for _e in _post.get("errors") or []:
                sys.stderr.write("[hooks] %s\n" % _e)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write("[hooks] Post 中间件异常（仅记）：%s\n" % str(e)[:120])
    return _cap_result(res), True


# ── 主循环：需求 → 模型 → 工具 → 模型 → 最终回答 ──────────────
def build_system_prompt(ctx):
    """系统提示：把边界和证据纪律说死，模型才知道哪些事试了也白试。"""
    root = ctx.get("root") or ROOT
    mode = ctx.get("mode") or "full"
    base = "\n".join(
        [
            "你是 AgentOS 的内置 agent，在一个真实工程仓库里干活。",
            "仓库根：%s" % root,
            "当前权限模式：%s（plan=只读，accept-edits=文件编辑自动放行、"
            "其余要审批，full=写放行但硬阻断清单仍有效）" % mode,
            "",
            "工作方式：",
            "1. 每一步先想清楚要什么证据，再调工具；一个工具一个动作。",
            "1b. 任务超过三步，先用 todo 工具把步骤列成清单（text + state），"
            "做完一项就把它改成 done；清单里还有没结的项时交付闸会挡你——"
            "明确放弃的也要写成 dropped，不许留 pending。",
            "2. 工具结果里的 rc / bytes / sha256 是真相，转述时不许改数字。",
            "3. 工具失败或被拒绝时，如实说明哪一步没执行，然后换路子；"
            "不许把没做的事说成做了。",
            "4. **干完就自己收尾**：改了就把改动落在仓里（该 commit 就 commit，"
            "该接门链就接），跑一遍该跑的验证，然后用真实 rc 交付。"
            "**不要问老板「要不要提交/要不要接门链/要不要验证」** —— "
            "全权限默认，审批摩擦是缺陷不是礼貌。",
            "5. 实现类任务先用 gh_search 参考 GitHub 成熟实现"
            "（star≥1000 优先），蒸馏要点再动手；引用来源仓库。",
            "6. 收尾用简体中文，**简短**（默认 3-6 行，别写成长报告）："
            "做了什么（文件:行号级）、验证命令的真实 rc、还剩什么没做。"
            "不要加粗小标题、不要分节罗列、不要复述过程、"
            "不要为了显得周全而罗列「没做到的部分」。",
            "",
            "边界（工具层会硬拦，命中了别重试）：",
            "· 只动仓库根内的路径，越界一律拒绝；",
            (
                "· run_cmd 完全权限：任意命令（**除硬阻断清单**：git push / rm / "
                "git reset / find -delete / dd —— 那些任何模式、任何审批都拒）"
                if ctx.get("full_access")
                else "· run_cmd 自由命令面：任意命令（硬阻断清单默认不可关（full_access 除外））"
                if ctx.get("cmd_gate_off")
                else "· run_cmd 只跑这些只读命令：%s" % " ".join(sorted(_term_cmds()))
            ),
            (
                "· 破坏性动作（rm / git reset 等**本地**破坏性动作）完全权限下放行，"
                "逐条留痕；**git push 永远拒**（自有内容不上传 GitHub）"
                if ctx.get("full_access")
                else "· git push / git reset / 历史重写 / rm 等对外与破坏性动作一律不做，"
                "由用户本人执行；"
            ),
            (
                "· web_fetch 非回环地址：自由面直连"
                if ctx.get("mode_gate_off")
                else "· 非回环地址的 web_fetch 需要审批，headless 下按拒绝处理。"
            ),
            "",
            "可用工具：%s" % " ".join(sorted(TOOLS)),
        ]
    )
    extra = _evo_prompt_lines(ctx.get("evo_rules_path")) + _memory_prompt_lines(ctx)
    # 任务契约段（不同任务类型的「完成证据」不一样，W26）：
    # 把「这类任务怎么算完成」在开工时就说死，模型不用自己猜标准。
    _tc = ctx.get("task_contract")
    if isinstance(_tc, dict) and _tc.get("opening"):
        extra = extra + [
            "任务契约（%s）：%s" % (_tc.get("name") or _tc.get("type") or "", _tc["opening"])
        ]
    # DSP 计划摘要行：有计划才注入（无则零改动）；超长截断，绝不撑爆上下文。
    # 自启指令行：本轮刚自启（ctx["dsp_auto"] 有值）才追加一次，防历史回灌叠加。
    try:
        _dsp_mod = _dsp()
        _plan = (
            _dsp_mod.load_plan(str(ctx.get("sid") or ""))[0] if ctx.get("sid") else None
        )
        if _plan is not None:
            _locked = not ctx.get("plan_lock_off")
            extra = extra + [_dsp_mod.plan_summary(_plan, locked=_locked)]
            if ctx.get("dsp_auto"):
                extra = extra + [
                    _dsp_mod.auto_directive(
                        _plan, {"reasons": ctx.get("dsp_auto")}, locked=_locked
                    )
                ]
    except Exception:  # noqa: BLE001
        pass
    # W4 hooks/skills 注入：钩子 additionalContext + skills 目录段（有才注入）。
    _hk = [str(x) for x in (ctx.get("hook_context") or []) if str(x).strip()]
    if _hk:
        extra = extra + ["钩子注入的上下文：\n" + "\n".join(_hk)[:4000]]
    _sk = _skills_section()
    if _sk:
        extra = extra + [_sk]
    if extra:
        return base + "\n" + "\n".join(extra)
    return base


# 「端点连不上」词表：传输层不通 / 网关过载 / 限流窗口。
# 这一族与 _TRANSIENT_RE 的区别 —— **端点自己会回来**（网络抖、网关重启、
# 限流窗口），所以在「不许用本地模型」的口径下要**一直等**。
# 401/403/400 **不在**这里：那些是 key/协议问题，等一辈子也不会好，
# 而且会把真病因（key 过期、网关路由错）藏起来 —— 那种要立刻报错 + 给处方。
_ENDPOINT_DOWN_RE = re.compile(
    r"connection refused|connection reset|connection aborted|no route to host|"
    r"name or service not known|nodename nor servname|temporary failure in name|"
    r"could not resolve host|dns|"
    r"tls handshake|ssl|handshake failure|"
    r"i/o timeout|timed? ?out|timeout expired|deadline exceeded|"
    r"unexpected[ _-]?eof|eof occurred|remote disconnected|server disconnected|"
    r"without response|empty reply|broken pipe|aborted|dial tcp|"
    r"temporar|overloaded|rate.?limit|\b429\b|\b50[2345]\b|"
    r"service unavailable|bad gateway|gateway timeout|"
    r"connection closed|closed connection|apex|disconnected",
    re.I,
)

_TRANSIENT_RE = re.compile(
    r"tls handshake|connection reset|connection refused|i/o timeout|"
    r"unexpected[ _-]?eof|eof occurred|dial tcp|temporar|timed? ?out|overloaded|"
    r"rate.?limit|50[234] |429|remote disconnected|broken pipe|closed connection|"
    r"incomplete ?read|content length|chunked error|decoding error|"
    r"without response|empty reply|aborted",
    re.I,
)

# ── W13 批准即执行 / 诚实收尾（2026-10-14 事故驱动）─────────────────
# 事故：老板说「拍板/全部同意」后，模型因系统提示谎报「写工具锁死」而空转
# 等一个不会来的 /approve，并逐字复读「做了什么/还剩什么」模板三轮。
# 三根钉：①批准词识别（负向环视防「不同意/没批准」误判）；②零证据完成声明
# 打脸（0 次工具调用却说「已落实」= 本系统铁律禁止）；③跨轮复读检测。
_APPR_RE = re.compile(
    r"(?<![不未没])(拍板|同意|批准|开干|动工|开工|"
    r"照此执行|执行吧|干吧)"
)
_APPR_NEG_RE = re.compile(
    r"(没有|并未|还没|未曾|拒绝|不同意|不批准|"
    r"未批准|没批准|先不|暂不|不予)"
)


def _looks_approved(text):
    """是不是「老板批准」：命中批准词且**整句没有否定前缀**（防误批）。
    事故样本负例：「没有批准」「我不同意」「暂不同意」都不许触发。"""
    t = str(text or "")
    return bool(_APPR_RE.search(t)) and not _APPR_NEG_RE.search(t)


_APPR_DIRECTIVE = (
    "[老板已批准] 立即开工执行（不要等审批、不要复述清单）："
    "从上一轮承诺或计划任务的第一项开始，真实调用工具把活"
    "干出来，每步给真实 rc 证据；本轮一个工具都不调用=失败。\n"
    "原话："
)
_APPR_RETRY = (
    "\n\n[再次强调] 上一轮你没有任何工具调用。现在就调用工具"
    "开工（读目标文件/跑验证命令都算），不要再写「做了什么/"
    "还剩什么」的空清单。"
)
_CLAIM_RE = re.compile(
    r"(已全面落实|全面落实|已落实|已全部完成|已部署|"
    r"部署完成|无需部署|已修复完成|已完成提升|全部执行完毕)"
)


def _claim_no_evidence(ans, n_tools):
    """零证据完成声明：0 次工具调用却声称「已落实」类动作 → True。"""
    return bool(int(n_tools) == 0 and ans and _CLAIM_RE.search(str(ans)))


def _is_stale_reply(prev, ans):
    """跨轮复读：本轮终答与上一轮近乎逐字相同（相似度 ≥0.9）→ True。"""
    if not prev or len(str(ans)) < 40:
        return False
    import difflib

    return difflib.SequenceMatcher(None, str(prev), str(ans)).ratio() >= 0.9


def _needs_approve_retry(approved_turn, tools_used, interrupted):
    """批准轮零工具调用 = 没真开工 → 需要自动重试一轮（有界一次）。
    被打断（Esc）不重试：用户主动停的，重试等于无视用户意图。"""
    return bool(approved_turn) and not tools_used and not interrupted


def run_agent(
    prompt,
    ctx=None,
    provider=None,
    stream=False,
    on_event=None,
    max_steps=None,
    cancel=None,
):
    """跑一次「需求 → 若干工具 → 最终回答」的循环，返回结果 dict。

    成功的形状：{"ok": True, "kind": "ok", "answer": ..., "steps": n,
                 "messages": [...], "usage": {...}, "elapsed": ...}
    预算耗尽的形状：{"ok": False, "kind": "limit", ...}（终答为空，如实说）
    端点/参数问题抛 AgentError，由调用方按 kind 转退出码。

    ctx 认这几个键：root / mode / approver / env / history / max_steps。
    history 是上一轮的 messages 列表，用来在交互 REPL 里保持上下文；
    跑完会被写回 ctx["history"]，超长时只保留 system + 最近 40 条。
    """
    _outer = ctx if isinstance(ctx, dict) else None  # history 写回要送到调用方
    ctx = dict(ctx or {})
    ctx["root"] = os.path.realpath(ctx.get("root") or ROOT)
    ctx.setdefault("mode", "full")
    if "full_access" not in ctx:
        try:
            ctx["full_access"] = _full_access(_read_agentos_config())
        except Exception:
            ctx["full_access"] = False
    text = (prompt or "").strip()
    if not text:
        raise AgentError("usage", "空需求：run_agent 需要一句非空的任务描述")
    # DSP 大任务自启：BIG 需求 + 本 sid 无计划 → 自动建骨架计划（未审批）。
    # fail-open 包住：分析器/落盘任何异常都不许阻断主循环；自启只是「先立账」，
    # 写锁与白名单仍由 _authorize 硬执行。自启事件经 on_event 上报（过程可见）。
    try:
        _dsp_mod = _dsp()
        _sid0 = str(ctx.get("sid") or "")
        if _sid0 and not ctx.get("dsp_noauto"):
            _plan0, _ana0 = _dsp_mod.auto_plan(text, _sid0, ctx["root"])
            if _plan0 is not None:
                ctx["dsp_auto"] = _ana0.get("reasons", [])
                # 自启即指派首个可做任务：监督（白名单/doom/记账）本轮就生效，
                # 不等用户先 /tasks。
                try:
                    _nx0 = _dsp_mod.next_task(_plan0)
                    if _nx0:
                        ctx["dsp_task"] = _nx0
                except Exception:  # noqa: BLE001
                    pass
                if callable(on_event):
                    on_event(
                        {
                            "type": "note",
                            "text": "大任务自动建计划（%s），先精修 accept"
                            " 再 /approve" % "；".join(_ana0.get("reasons", []))[:120],
                        }
                    )
    except Exception:  # noqa: BLE001
        pass
    try:
        cap = int(max_steps or ctx.get("max_steps") or MAX_STEPS)
    except (TypeError, ValueError):
        cap = MAX_STEPS
    cap = max(1, min(MAX_STEPS, cap))
    # 交付闸的退回轮**不烧用户 step 预算**：那是内核要求补证据，不是
    # 模型在同一个任务上多试一次。给它独立的 _GATE_BUDGET 额度。
    _GATE_BUDGET = 2
    if provider is None:
        # resolve_provider 给的是「端点描述」（纯 dict，便于 env 注入与门做
        # 证伪）；这里必须再包一层 Provider：run_agent 后面调的是
        # provider.chat(...)。少了这一层，dict 上没有 chat，一条真需求会在
        # 第 261 行抛 AttributeError —— 而那偏偏是 selftest 用桩对象绕开
        # 的那条路，所以 23/23 全绿也照样看不出来。
        provider = Provider(resolve_provider(ctx.get("env")))
    ctx["_provider"] = provider  # subagent 工具复用它派子代理（在副本内，不外泄）
    _sauto = _skills_auto(text)
    # 任务契约：分类 → 契约 → 写进 ctx。
    # 位置很讲究：**在 DSP 自启之后**（计划未审批会锁写，契约必须知道）、
    # **在 system prompt 之前**（契约要写进 prompt，模型开工前就该知道
    # 「这类任务怎么算完成」）。
    _vinfo = None
    try:
        import verify_cmd as _vc_mod

        _vinfo = _vc_mod.detect_verify_cmd(ctx.get("root"))
    except Exception:  # noqa: BLE001  探测失败不许拖垮主链路
        _vinfo = None
    _contract = _build_task_contract(prompt, ctx, _vinfo)
    if _contract:
        ctx["task_contract"] = _contract
    if _sauto:
        sysmsg = {
            "role": "system",
            "content": build_system_prompt(ctx) + "\n\n" + _sauto,
        }
    else:
        sysmsg = {"role": "system", "content": build_system_prompt(ctx)}
    base = ctx.get("history")
    messages = list(base) if isinstance(base, list) and base else [sysmsg]
    if not messages or messages[0].get("role") != "system":
        messages.insert(0, sysmsg)
    messages = messages + [{"role": "user", "content": text}]
    tools = tool_defs()
    t0 = time.time()
    usage = {}
    used = []
    last_sig = None
    repeat = 0
    _last_text = ""
    _text_rep = 0
    _ok_sigs = set()
    _fail_tool = ""
    _fail_n = 0
    _transient_n = 0  # 本轮累计的「端点瞬时错重试」次数
    _TRANSIENT_MAX = 6  # 上限：网络抖动可以骑，死循环不行
    # 会话中毒轮换走独立预算：与 SSL 同池会互相烧光（实测第六圈死法——
    # SSL 六次把池子吃完，中毒轮换没得花，裸死）。
    _sess_n = 0
    _SESS_MAX = 3
    _down_n = 0  # 端点重连次数（0 = 无上限时一直涨）
    step = 0
    _total_steps = 0
    _gate_retries = 0  # 交付闸退回次数（上限 2 次，防无限打转）
    # 收敛引擎（W23）：把「探查→动手→验证→交付」做成可判定状态机。
    # 为什么必须有：实测 2026-10-19 真实终端里，agent 跑 23 步 / 900 秒
    # / 86 次工具调用原地打转、零交付（read_file×10、run_cmd×7）——不是
    # 模型笨，是内核没有「该动了」「该验了」「该交了」的强制信号。
    try:
        import converge as _cv_mod

        # 收敛引擎可显式关（ctx["converge"] is False）：回放基准要 A/B 对照，
        # 没有「关」的一侧就没法证明引擎到底带来了什么。
        if ctx.get("converge") is False:
            raise ImportError("converge disabled by ctx")
        # 验证命令探测与任务契约已在上面建好（必须早于 system prompt：
        # 契约要写进 prompt，模型开工前就得知道这类任务怎么算完成）。
        _conv = _cv_mod.Converge(
            task_text=str(prompt or "")[:400],
            mode=str(ctx.get("mode") or ""),
            verify_info=_vinfo,
            contract=_contract,
            # 只有 git 仓库才要求「自审 diff」（非 git 仓库没有 diff 可看，
            # 硬要它就是永远红 —— 假红比漏红更坏）。
            require_diff_review=os.path.isdir(
                os.path.join(str(ctx.get("root") or ""), ".git")
            ),
        )
        # 自进化调参：ACTIVE 规则真拧旋钮（只允许收紧，由 apply_policy 钳制）
        try:
            _pol = _evo_converge_policy(ctx.get("evo_rules_path"))
            if _pol:
                _conv.apply_policy(_pol)
        except Exception:  # noqa: BLE001  调参坏不许拖垮主链路
            pass
    except Exception:  # noqa: BLE001  收敛层坏不许拖垮主链路
        _cv_mod = None
        _conv = None
    while True:
        step += 1
        _total_steps += 1
        if step > cap:
            # 独立续跑：有真实进展（新的成功动作）且没在打转 → 压缩上下文
            # 后自动接力（至多 2 轮），而不是直接判死刑。没进展才停。
            _chain = 0
            try:
                _chain = int(ctx.get("_chain") or 0)
            except (TypeError, ValueError):
                _chain = 0
            if _ok_sigs and _text_rep < TEXT_REPEAT:
                try:
                    _nh, _nd, _dg = _compact_history_v2(
                        messages, keep=40, provider=provider
                    )
                except Exception:  # noqa: BLE001
                    _nh, _nd, _dg = messages[-40:], 0, False
                messages = _nh + [
                    {
                        "role": "user",
                        "content": "（独立续跑第 %d 轮：前面已成功"
                        "执行 %d 个不同动作，接着干，"
                        "不要重复已做过的动作）" % (_chain + 1, len(_ok_sigs)),
                    }
                ]
                ctx["_chain"] = _chain + 1
                if _outer is not None:
                    _outer["_chain"] = _chain + 1
                step = 0
                if callable(on_event):
                    on_event(
                        {
                            "type": "note",
                            "text": "预算续跑第 %d 轮（摘要=%s）"
                            % (_chain + 1, "有" if _dg else "旧口径"),
                        }
                    )
                continue
            return {
                "ok": False,
                "kind": "limit",
                "answer": None,
                "error": "达到 step 预算 %d%s：需求没在预算内收敛；"
                "把它拆小，或显式调大 --max-steps（上限 %d）"
                % (
                    cap,
                    ("（已独立续跑 %d 轮、共 %d 步）" % (_chain, _total_steps - 1))
                    if _chain
                    else "",
                    MAX_STEPS,
                ),
                "last_text": (_last_text or "")[:200],
                "steps": step - 1,
                "messages": messages,
                "tools_used": used,
                "converge": (_conv.snapshot() if _conv is not None else {}),
                "elapsed": round(time.time() - t0, 2),
            }
        # 老板监督通道：步骤边界收指令（有则注入本轮、注明来源、只进一次）。
        _stq = _steer_take(ctx)
        if _stq:
            for _s in _stq:
                messages.append(
                    {"role": "user", "content": "[老板指令·监督纠正] %s" % _s}
                )
            if callable(on_event):
                on_event(
                    {
                        "type": "note",
                        "text": "收到老板指令 %d 条（已注入本步骤边界）" % len(_stq),
                    }
                )
        if MAX_TOTAL_SECONDS > 0 and time.time() - t0 > MAX_TOTAL_SECONDS:
            return {
                "ok": False,
                "kind": "limit",
                "answer": None,
                "error": "达到墙上时钟预算 %ds（已跑 %ds）：拆小需求，"
                "或下轮再来" % (MAX_TOTAL_SECONDS, int(time.time() - t0)),
                "steps": step - 1,
                "messages": messages,
                "tools_used": used,
                "converge": (_conv.snapshot() if _conv is not None else {}),
                "elapsed": round(time.time() - t0, 2),
            }
        if callable(on_event):
            on_event({"type": "step", "step": step})
        if cancel is not None and cancel():
            raise AgentError("interrupted", "已打断（Esc），上下文保留")
        try:
            comp = provider.chat(
                messages, tools, stream=stream, on_event=on_event, cancel=cancel
            )
        except AgentError as e:
            if e.kind == "interrupted":
                raise
            # 有界瞬时重试（蒸馏自 gemini-cli 的 retry 思想）：只认传输层
            # 瞬时词表、只非流式——流式中途重放文本是假丑。
            #
            # 顺序纪律（实测第六圈死法）：**中毒分支必须排在词表门前面**。
            # 会话脱钩的 400（No tool call found）不含传输词，排在后面就被
            # `not _TRANSIENT_RE.search → raise` 一刀毙命，rotate 永远轮不
            # 到。且走**独立预算**：与 SSL 同池会互相烧光。
            if _SESSION_BREAK_RE.search(str(e)) and not stream:
                # 循环 reroll：每次轮换后再捕一次（预算 _SESS_MAX 内），
                # 命中断流/打断/非中毒错都原样抛出，不吞。
                while True:
                    if _sess_n >= _SESS_MAX:
                        raise
                    _sess_n += 1
                    # 先留证再自救：完整净荷 dump（黑盒 reroll 治标不治本）
                    try:
                        _tr = os.path.join(
                            os.path.dirname(
                                os.path.dirname(os.path.abspath(__file__))
                            ),
                            "work",
                            "llm_400_trace.json",
                        )
                        with open(_tr, "w", encoding="utf-8") as _tf:
                            json.dump(
                                {"error": str(e)[:500], "messages": messages},
                                _tf,
                                ensure_ascii=False,
                                indent=1,
                            )
                    except Exception:  # noqa: BLE001  留证失败不挡自救
                        pass
                    provider.rotate_session()
                    if callable(on_event):
                        on_event(
                            {
                                "type": "note",
                                "text": "网关会话脱钩，轮换会话 id 重试"
                                "（%d/%d）" % (_sess_n, _SESS_MAX),
                            }
                        )
                    time.sleep(1.0)
                    # 会话中毒的真正死因：轮换会话 id 只换头，净荷里还带着上一轮
                    # 的 tool_call_id 链 → 新会话里找不到那些 call_id → 又 400。
                    # 所以每次轮换后把消息裁到「system + 最近 8 条」，切掉最老的
                    # 工具交换对（它们大概率就是中毒源）；保留最近上下文。
                    if len(messages) > 9:
                        _sys = [m for m in messages if m.get("role") == "system"][:1]
                        _tail = messages[-8:]
                        messages = _sys + _tail
                    try:
                        comp = provider.chat(
                            messages, tools, stream=stream, on_event=on_event,
                            cancel=cancel,
                        )
                        break
                    except AgentError as e2:
                        if e2.kind == "interrupted" or not _SESSION_BREAK_RE.search(
                            str(e2)
                        ):
                            raise
                        e = e2  # 还是中毒：预算 -1，继续轮换
            else:
                # ── 端点连不上 → **一直等**（用户要求：不放弃、不换本地）──
                # 两种失败要分开：
                # · **连不上**（传输层不通/网关过载/限流）→ 端点自己会回来，
                #   所以一直等；预算 0 = 无上限（_endpoint_retry_max）。
                #   只有在「不许用本地模型」（no_fallback=true）时无上限 ——
                #   允许本地兜底的用户等 6 次就切本地（那是他们自己选的策略）。
                # · 其它瞬时错 → 照旧有界（_TRANSIENT_MAX），别无限刷。
                # 401/403/400 两族都不在内：key/协议问题等一辈子也不会好。
                _down_max = (
                    _endpoint_retry_max() if _no_fallback() else _TRANSIENT_MAX
                )
                while True:
                    if stream:
                        raise  # 流式中途重放文本是假丑
                    _down = bool(_ENDPOINT_DOWN_RE.search(str(e)))
                    if _down:
                        if _down_max and _down_n >= _down_max:
                            raise
                    else:
                        if (
                            not _TRANSIENT_RE.search(str(e))
                            or _transient_n >= _TRANSIENT_MAX
                        ):
                            raise
                    if cancel is not None and cancel():
                        raise AgentError(
                            "interrupted", "已打断（Esc），端点等待中止；上下文保留"
                        )
                    if _down:
                        _down_n += 1
                        _tw = min(2.0 * (2 ** min(_down_n - 1, 5)), 30.0)
                        _msg = (
                            "端点连不上：%s\n"
                            "  → 第 %d 次重试，%.0fs 后再试；**一直等到端点恢复**"
                            "（不会改用本地模型；Ctrl-C 中断）"
                            % (str(e)[:120], _down_n, _tw)
                        )
                    else:
                        _transient_n += 1
                        _tw = min(2.0 * (2 ** (_transient_n - 1)), 12.0)
                        _msg = "瞬时错误，%.0fs 后重试（第 %d/%d 次）：%s" % (
                            _tw,
                            _transient_n,
                            _TRANSIENT_MAX,
                            str(e)[:80],
                        )
                    # 直写 stderr：note 事件在 REPL/headless 都不上屏，
                    # 「一直在等」而没有一行提示 = 看起来像卡死。
                    try:
                        sys.stderr.write(_msg + "\n")
                        sys.stderr.flush()
                    except Exception:  # noqa: BLE001
                        pass
                    if callable(on_event):
                        on_event({"type": "note", "text": _msg})
                    time.sleep(_tw)
                    try:
                        comp = provider.chat(
                            messages, tools, stream=stream, on_event=on_event,
                            cancel=cancel,
                        )
                        break
                    except AgentError as e2:
                        if e2.kind == "interrupted":
                            raise
                        if not (
                            _ENDPOINT_DOWN_RE.search(str(e2))
                            or _TRANSIENT_RE.search(str(e2))
                        ):
                            raise
                        e = e2
        if isinstance(comp.get("usage"), dict) and comp["usage"]:
            usage = comp["usage"]
        calls = comp.get("tool_calls") or []
        msg = {"role": "assistant", "content": comp.get("content") or ""}
        if calls:
            msg["tool_calls"] = calls
        messages.append(msg)
        # 打转守卫①（W11.1，实测事故）：同一句话（非空终稿内容）连续重复
        # ≥TEXT_REPEAT 步还没进展 —— 先于烧预算停。事故复现：模型嘴上重复
        # 同一句、手上换着参数连调 run_cmd，旧守卫（只查同工具同参）全漏过。
        _ct = msg["content"].strip()
        if _ct:
            if _ct == _last_text:
                _text_rep += 1
            else:
                _text_rep, _last_text = 1, _ct
            if _text_rep >= TEXT_REPEAT:
                return {
                    "ok": False,
                    "kind": "limit",
                    "answer": None,
                    "error": "同一句话连续重复 %d 步没进展（%r…）："
                    "判为原地打转，停（多为端点抖动或模型卡壳，"
                    "可重试或换端点）" % (_text_rep, _ct[:60]),
                    "steps": step,
                    "messages": messages,
                    "tools_used": used,
                    "converge": (_conv.snapshot() if _conv is not None else {}),
                    "elapsed": round(time.time() - t0, 2),
                    # 收敛状态**任何结局都要带**：限速/闸失败也是账本素材
                    "converge": (
                        _conv.snapshot() if _conv is not None else {}
                    ),
                }
        if not calls:
            if len(messages) <= 41:
                ctx["history"] = messages[-40:]
            else:
                # v2（auto-compact 的下一档）：丢弃前先让 provider 生成
                # 事实摘要 digest 塞回头部；失败自动回退旧口径（逐字等价）。
                ctx["history"], _, _dg = _compact_history_v2(
                    messages, keep=40, provider=provider
                )
                # 自压缩提示：压缩是既定行为，让它肉眼可见（含摘要有无）；
                # note 类型 headless printer 忽略。
                if callable(on_event):
                    on_event(
                        {
                            "type": "note",
                            "text": "上下文自动压缩 %d→%d 条（摘要=%s）"
                            % (
                                len(messages),
                                len(ctx["history"]),
                                "有" if _dg else "旧口径回退",
                            ),
                        }
                    )
            # 契约写回：run_agent 内部对 ctx 做了浅拷（防 root/mode 归一化
            # 回写调用方），但 docstring 承诺「跑完写回 ctx["history"]」——
            # 不回写 = REPL 多轮上下文与 compact v2 摘要全部蒸发（W1 门首跑
            # 实测抓出：digest 真发了请求，history 却停在空表）。只回写
            # history 这一个键，其余照旧留在副本里。
            if _outer is not None:
                _outer["history"] = ctx["history"]
            _ans = msg["content"] or ""
            _empty = not _ans.strip()
            # 交付闸（W23）：终答自称完成却零验证证据 → 退回补证据，别放行。
            # 形状来自真实事故：900 秒那次它说了「已完成」，全程没跑出一条 rc。
            # 任务账本（W31）：清单里还有没结的项时，**不许**说「做完了」。
            # 这是把「清单」从装饰变成真约束 —— Claude Code 的 TodoWrite /
            # Aider 的 todo 之所以有用，就因为它能拦住「只做了三分之二」。
            _todo_open = []
            try:
                import todo as _tdm

                _sid_t = str(ctx.get("sid") or "default")
                _todo_open = _tdm.unfinished(_tdm.load(_sid_t, ctx.get("root")))
            except Exception:  # noqa: BLE001
                _todo_open = []
            if _conv is not None and not _empty:
                # 交付闸的判定：账本未结项 **或** 证据不足，任一成立都不放行。
                # 账本未结是**硬事实**（不是可重试的提示），所以它并进同一个
                # 闸门判定里 —— 走既定的「重试预算用尽 → fail-closed」路径，
                # 不给它留「预算用尽就放行」的出口（那等于清单形同虚设）。
                _gok, _gwhy = True, ""
                if _todo_open:
                    _gok, _gwhy = False, (
                        "任务账本里还有 %d 项没结：%s"
                        % (
                            len(_todo_open),
                            "、".join(
                                "%s(%s)"
                                % (it.get("text", "")[:40], it.get("state", "?"))
                                for it in _todo_open[:3]
                            ),
                        )
                    )
                else:
                    try:
                        _gok, _gwhy = _conv.gate(_ans)
                    except Exception:  # noqa: BLE001
                        _gok, _gwhy = True, ""
                if not _gok:
                    if _gate_retries < _GATE_BUDGET:
                        _gate_retries += 1
                        try:
                            _gmsg = _conv.require_evidence()
                        except Exception:  # noqa: BLE001
                            _gmsg = "[交付闸] 终答缺证据：%s" % _gwhy
                        messages.append({"role": "user", "content": _gmsg})
                        if callable(on_event):
                            on_event({"type": "note", "text": _gmsg})
                        # 退回主循环，但**不消耗用户的 step 预算**：
                        # 这是内核要求补证据，不是任务本身多走一步
                        # （step -= 1 把那一步还回去；cap 判据照旧生效）。
                        # 模型这一轮可以直接调工具补证据（跑 pytest / 写文件），
                        # 走的是和正常轮完全一样的执行路径，不另开一条支线。
                        step -= 1
                        _total_steps -= 1
                        continue
                    # 退到底了还是没证据 → **不按完成交付**。
                    # 放行等于把「空口完成」洗成「已完成」，那正是
                    # 900 秒事故的根：说了做了，其实没做。这里如实报未交付。
                    return {
                        "ok": False,
                        "kind": "gate",
                        "steps": step,
                        "answer": _ans,
                        "verified": False,
                        "error": "交付闸：终答缺证据（%s），补证据轮用尽仍不通过——"
                        "本轮不按完成交付" % _gwhy,
                        "messages": messages,
                        "tools_used": used,
                        "usage": usage,
                        "elapsed": round(time.time() - t0, 2),
                        "converge": _conv.snapshot(),
                    }
            return {
                "ok": True,
                "kind": "ok",
                "steps": step,
                "answer": _ans,
                "empty_answer": _empty,
                "converge": _conv.snapshot() if _conv is not None else {},
                "error": (
                    "模型返回空内容（端点抖动/模型卡壳）：本轮没有"
                    "可交付答案，可重试或换端点"
                    if _empty
                    else None
                ),
                "finish_reason": comp.get("finish_reason"),
                "messages": messages,
                "usage": usage,
                "tools_used": used,
                "elapsed": round(time.time() - t0, 2),
                "rc": 0,
            }
        for tc in calls:
            fn = tc.get("function") or {}
            name = str(fn.get("name") or "").strip()
            raw = fn.get("arguments")
            if raw in (None, ""):
                raw = "{}"
            try:
                args = json.loads(raw)
            except ValueError:
                res = {
                    "ok": False,
                    "executed": False,
                    "tool": name,
                    "error": "工具参数不是合法 JSON（这一步未执行）：%r"
                    % str(raw)[:200],
                }
            else:
                sig = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
                if sig == last_sig:
                    repeat += 1
                else:
                    repeat, last_sig = 0, sig
                if repeat >= MAX_REPEAT:
                    return {
                        "ok": False,
                        "kind": "limit",
                        "answer": None,
                        "error": "同一步原样连做 %d 次还没进展（%s）："
                        "判为原地打转，停" % (repeat + 1, name),
                        "steps": step,
                        "messages": messages,
                        "tools_used": used,
                        "converge": (_conv.snapshot() if _conv is not None else {}),
                        "elapsed": round(time.time() - t0, 2),
                    }
                # DSP doom 升级：有计划+当前任务时，归一化同参第 3 次不判
                # limit，而是把「已打转、需人看一眼」如实回灌，让模型转 ask。
                # 全局 MAX_REPEAT 照旧（无计划时行为零改动）。
                try:
                    _dsp_dw = ctx.get("dsp_doom")
                    if _dsp_dw is None:
                        _dsp_dw = _dsp().DoomWatch()
                        ctx["dsp_doom"] = _dsp_dw
                    _dsp_verdict = None
                    if ctx.get("sid") and ctx.get("dsp_task"):
                        _dsp_verdict = _dsp_dw.note(ctx.get("dsp_task"), name, args)
                    if _dsp_verdict == "ask" and callable(on_event):
                        on_event(
                            {
                                "type": "note",
                                "text": "同参第 3 次（%s）：已打转，请确认"
                                "后再试，别原样重发" % name,
                            }
                        )
                except Exception:  # noqa: BLE001
                    pass
                if callable(on_event):
                    # 过程可见性：工具被调用的瞬间就上报（不等结果）。
                    on_event({"type": "tool_call", "tool": name, "args": args})
                res, _ex = exec_tool(name, args, ctx)
                if (
                    bool(res.get("executed"))
                    and bool(res.get("ok"))
                    and sig not in _ok_sigs
                ):
                    _text_rep = 0  # 新的成功动作=在进展，嘴上重复不算打转
                    _ok_sigs.add(sig)
            # 打转守卫②：同一工具连续失败/被拒 ≥TOOL_FAIL_STREAK 次 → 停。
            # 「执行了但失败」与「被拒/未执行」都算不进步；换工具或任一次
            # 成功即清零（正常"试错→改进"的连败间通常夹着别的动作，不受影响）。
            _good = bool(res.get("executed")) and bool(res.get("ok"))
            if _good:
                _fail_tool, _fail_n = "", 0
            else:
                if name == _fail_tool:
                    _fail_n += 1
                else:
                    _fail_tool, _fail_n = name, 1
            used.append(
                {
                    "tool": name,
                    "ok": bool(res.get("ok")),
                    "executed": bool(res.get("executed")),
                    "error": str(res.get("error") or "")[:160],
                }
            )
            if _fail_n >= TOOL_FAIL_STREAK:
                return {
                    "ok": False,
                    "kind": "limit",
                    "answer": None,
                    "error": "同一工具 %r 连续失败/被拒 %d 次（最后：%s）："
                    "判为原地打转，停"
                    % (name, _fail_n, str(res.get("error") or "")[:120]),
                    "steps": step,
                    "messages": messages,
                    "tools_used": used,
                    "converge": (_conv.snapshot() if _conv is not None else {}),
                    "elapsed": round(time.time() - t0, 2),
                }
            # DSP 任务记账：有计划+当前任务时，每步写回 steps_used/evidence，
            # ok+证据行 → done，超 TASK_BUDGET → stuck。落盘失败静默（增益层），
            # 但状态流转如实经 on_event 上报。
            try:
                if ctx.get("sid") and ctx.get("dsp_task"):
                    _dsp_m = _dsp()
                    _pl, _e = _dsp_m.load_plan(str(ctx.get("sid")))
                    if _pl is not None:
                        _ev = "rc=%s %s" % (
                            "0" if res.get("ok") else "1",
                            str(res.get("error") or res.get("result") or "")[:120],
                        )
                        _st = _dsp_m.task_record_step(
                            _pl, ctx.get("dsp_task"), bool(res.get("ok")), _ev
                        )
                        _dsp_m.save_plan(_pl, str(ctx.get("sid")))
                        if callable(on_event) and _st in ("done", "stuck"):
                            on_event(
                                {
                                    "type": "note",
                                    "text": "任务 %s → %s（%s）"
                                    % (ctx.get("dsp_task"), _st, _ev[:80]),
                                }
                            )
                            if _st == "done":
                                _nx = _dsp_m.next_task(_pl)
                                if _nx:
                                    ctx["dsp_task"] = _nx
            except Exception:  # noqa: BLE001
                pass
            if callable(on_event):
                on_event(
                    {
                        "type": "tool",
                        "tool": name,
                        "ok": bool(res.get("ok")),
                        "executed": bool(res.get("executed")),
                        "summary": _tool_summary(name, args, res),
                        # todo 工具的结果带一份面板文本 → 终端里实时显示进度
                        "todo_panel": (
                            str(res.get("panel") or "")
                            if name == "todo" and res.get("ok")
                            else ""
                        ),
                        # 失败原因随事件走：交互面要在 ⎿ 一行里说清「为什么没成」，
                        # 不必再另起一行（headless 的行形状不变）。
                        "error": str(res.get("error") or "")[:120],
                    }
                )
            # 收敛观测：喂一次（工具, 参数, 成败, rc）。产出与否决定停滞计数。
            if _conv is not None:
                try:
                    _conv.observe(
                        name,
                        args if isinstance(args, dict) else {},
                        ok=bool(res.get("ok")) and bool(res.get("executed", True)),
                        rc=res.get("rc"),
                        # 输出尾部：核对类命令 rc≠0 时，修复指令要能说清
                        # 「失败长什么样」（否则模型只能猜 —— 已知缺口第 1 条）。
                        out=res.get("out"),
                    )
                except Exception:  # noqa: BLE001
                    pass
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(
                        tc.get("id") or ("call_%s_%d" % (name or "x", step))
                    ),
                    "content": json.dumps(_cap_result(res), ensure_ascii=False),
                }
            )

        # 收敛指令注入（步骤边界）：停滞/循环/阶段越界时给模型一条明确方向。
        # 注入点是 user 角色 —— 模型能看见但不会当成工具结果，语义更干净。
        if _conv is not None and step < cap - 1:
            try:
                _dir = _conv.directive()
            except Exception:  # noqa: BLE001
                _dir = None
            if _dir:
                messages.append({"role": "user", "content": _dir})
                if callable(on_event):
                    on_event({"type": "note", "text": _dir})


def _writes_blocked(ctx):
    """当前上下文里写操作是不是被环境锁住了。

    两种锁：
    ① plan 模式（只读）—— 写工具本来就拒；
    ② DSP 计划存在但**未审批**—— 写锁死（见 dsp_gate D7）。

    契约要知道这件事：要求一份「够不着的证据」（写盘）会让任务永远
    交不了付 —— 闸门会一直退到预算耗尽。契约必须服从环境现实。
    """
    if str((ctx or {}).get("mode") or "") == "plan":
        return True, "plan 模式（只读）"
    sid = str((ctx or {}).get("sid") or "")
    if not sid:
        return False, ""
    try:
        _d = _dsp()
        _pl, _e = _d.load_plan(sid)
        if _pl is not None and not _pl.get("approved"):
            return True, "DSP 计划未审批（写锁死）"
    except Exception:  # noqa: BLE001  查不到就当没锁
        pass
    return False, ""


def _build_task_contract(prompt, ctx, verify_info=None):
    """按任务字面判定任务类型 → 生成完成契约。

    为什么要有：不同任务类型的「完成证据」根本不一样（缺陷修复要红→绿，
    文档要写下并读回，调研要可核对来源）。一份通用要求对每种都太松或太紧。

    判不出来就返回 None（不硬套）：宁可没契约，也不要给一份错的契约 ——
    错的契约会逼模型去做它不该做的事。
    """
    try:
        import task_contract as tcm
    except Exception:  # noqa: BLE001
        return None
    try:
        _kind, _why = tcm.classify(str(prompt or "")[:400])
        _c = tcm.contract_for(_kind, verify_info, str(prompt or "")[:400])
        _c["reasons"] = _why
        _blk, _blk_why = _writes_blocked(ctx)
        if _blk:
            _c = tcm.relax_for_writes(_c, True, _blk_why)
        return _c
    except Exception:  # noqa: BLE001  契约坏不许拖垮主链路
        return None


def _endpoint_retry_max():
    """端点重连预算：0 = **一直等**（默认），>0 = 试 N 次后放弃。

    只认字面 "0" 与可解析的整数；配置读不到就是 0（一直等）——
    「端点连不上就一直试」是用户明确的要求，不该因为配置缺失就退回放弃。
    """
    v = os.environ.get("AGENTOS_ENDPOINT_RETRY_MAX")
    if v is None or str(v).strip() == "":
        try:
            v = (_read_agentos_config().get("llm") or {}).get("endpoint_retry_max")
        except Exception:  # noqa: BLE001
            v = None
    if v is None or str(v).strip() == "":
        return 0
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return 0
    return max(0, n)


def _no_fallback():
    """~/.agentos/config.toml [llm] no_fallback=true → 主端点失败不换本地。

    只认字面 "true"（llm 表存字符串，与 gh.write_enabled 同口径）；
    缺键/其它值 = 默认行为（交互 REPL 回退本地一次）。
    """
    try:
        v = (_read_agentos_config().get("llm") or {}).get("no_fallback")
    except Exception:  # noqa: BLE001  配置读坏了不提权也不锁死，默认行为
        return False
    return str(v) == "true"


def _startup_local_backend():
    """REPL 启动期的本机离线后端兜底。返回 (cfg, blocked)。

    no_fallback=true 时 blocked=True 且 **probe 零调用** —— 「不能用本地
    模型」要连启动期兜底一起堵：只堵中途回退、启动时照样换本地，等于
    「云端挂了就静默降级」，正是用户不许的那件事。
    """
    if _no_fallback():
        return None, True
    return _probe_ollama(), False


def run_agent_with_fallback(
    prompt,
    ctx=None,
    provider=None,
    stream=False,
    on_event=None,
    allow_fallback=True,
    probe=None,
    cancel=None,
    retry_main=False,
    sleep=None,
):
    """run_agent 的一层包装：主端点抛 provider 错时，若本机离线后端可用，
    换过去重试一次。返回 (结果 dict, 最终用的 Provider)。

    为什么只做一层、只做一次：回退是给**交互 REPL** 的「开箱即用」兜底，
    不是给 CI 的假绿。headless 调用方传 allow_fallback=False（默认就是
    False 在 _run_once 里）—— 没端点就该 rc=3，不许悄悄换本地模型把红门
    洗绿。换不了就原样抛，绝不吞错。

    retry_main=True（配 `agentos config set llm.no_fallback true`）时反过来：
    主端点失败**不换本地**，而是指数退避（1/2/4…封顶 30s）一直重试主端点，
    只认 cancel（REPL 里 Esc）或 KeyboardInterrupt 为出口。probe（本地后端）
    全程不碰 —— 「主端点失败就一直等主端点」是字面保证，不是重试几次后
    悄悄降级。sleep 可注入，只为让自检不真睡。

    probe 可注入（默认 _probe_ollama），只为让自检能拿桩端点证伪回退路径，
    不碰真 11434。
    """
    _sleep = sleep or time.sleep
    try:
        res = run_agent(
            prompt,
            ctx=ctx,
            provider=provider,
            stream=stream,
            on_event=on_event,
            cancel=cancel,
        )
        return res, provider
    except AgentError as e:
        if e.kind != "provider" or (not allow_fallback and not retry_main):
            raise
        if retry_main and not allow_fallback:
            n = 0
            while True:
                if callable(cancel) and cancel():
                    raise
                wait = min(1 * (2**n), 30)
                sys.stderr.write(
                    "[主端点重试] 第 %d 次失败（%s），%.0fs 后重试主端点"
                    "（no_fallback：不换本地，Esc 可停）\n"
                    % (n + 1, str(e)[:100], wait)
                )
                try:
                    _sleep(wait)
                except KeyboardInterrupt:
                    raise
                if callable(cancel) and cancel():
                    raise
                try:
                    res = run_agent(
                        prompt,
                        ctx=ctx,
                        provider=provider,
                        stream=stream,
                        on_event=on_event,
                        cancel=cancel,
                    )
                    return res, provider
                except AgentError as e2:
                    if e2.kind != "provider":
                        raise
                    e, n = e2, n + 1
            # 不可达：循环只从 cancel / 非 provider 错 / 成功三处出口
        cfg = (probe or _probe_ollama)()
        if not cfg:
            raise
        if provider is not None and provider.source == str(cfg.get("source")):
            raise  # 已经在用这个后端了，别自转
        sys.stderr.write(
            "[回退] 主端点失败（%s），改用本机离线后端 %s\n"
            % (str(e)[:140], cfg.get("model") or "?")
        )
        if ctx is not None:
            # 自进化信号：被兜底掩盖≠没发生——主端点这次失败是真实事件，
            # 如实挂到 ctx 上让 REPL 记一条 ok=False 信号（挖掘原料）。
            ctx["last_fallback"] = {"kind": e.kind, "err": str(e)[:140]}
        p2 = Provider(cfg)
        res = run_agent(prompt, ctx=ctx, provider=p2, stream=stream, on_event=on_event)
        return res, p2


# ── 命令行 ────────────────────────────────────────────────────
EPILOG = """\
常用例子：
  agentos agent                          # 交互 REPL（默认 full）
  agentos agent --mode plan              # 只读规划，任何写操作一律拒绝
  agentos agent -p '跑一遍门链并汇报真实 rc'    # headless，适合脚本
  agentos agent --root /path/to/repo -p '...'   # 换仓库
  agentos agent --check                  # 自检（本地桩端点，不碰真模型）
  agentos agent --continue               # 接着上一次 agent 会话聊
  agentos agent --resume agent-2026...   # 续跑指定会话（/sessions 列 id）
  agentos agent --provider opencode -p '...'   # OpenCode zen 端点（需 AGENTOS_LLM_API_KEY）

端点三选一：`--provider <名字>`（内置命名端点，当前 opencode）/
`AGENTOS_LLM_BASE_URL` 直指任意 OpenAI 兼容口 / `~/.codex/config.toml` 复用。
REPL 内命令：/help 帮助            /exit 退出（Ctrl-D 同效）
            /mode <plan|accept-edits|full>  切换权限模式
            /status                        会话/模式/仓库/模型/上下文
            /model [名]                    看或换模型（下一轮生效）
            /cost                          累计 token 用量（端点自报）
            /compact [聚焦]                压缩上下文（保留 system + 最近 20；有焦点词
                                           先让模型做事实摘要塞回头部，失败回退旧口径）
            /rewind [--list|<N>]           列轮次 / history 回到第 N 轮末（文件层
                                           不动；写操作回退用 /undo）
            /skill [名]                    列技能 / 把某技能正文注入下一轮上下文
                                           （技能目录：~/.agentos/skills 与本仓
                                           .agentos/skills；hooks 同理配
                                           ~/.agentos/hooks.json，重启生效）
            /branch [注记]                 另开会话分支（原会话不动、可回去）
            /verify                        验本会话计划的真完成（不提前 done）
            /sessions                      列会话（标出当前）；可 branch 进出
            /steer "指令"                  投一条老板监督指令（步骤边界注入，
                                           只配送不裁决）；agentos steer 同效
            /watch                         一屏监督面（计划/指令/会话尾）
            安全闸（默认自由面）            agentos config set agent.plan_lock
                                           true 才开计划锁；agent.mode_gate
                                           true 才开权限模式；agent.cmd_gate
                                           true 才收窄命令白名单（硬阻断清单
                                           不可关；重启生效）
            /clear  /reset                 清空上下文（保留模式）
            /tools                         列出工具
            /diff                          本仓库当下的改动（git）
            /init                          在仓库根放一份起步 AGENTS.md
            /sessions                      列本机 agent 会话 id
            /plan [<目标>]                 无参看计划；有参建新计划（任务树+判据）
            /approve                       人审通过计划（解锁写工具执行）
            /tasks [id]                    看任务树 / 切换当前任务
            /block [原因]                  偏离即 BLOCK 落盘（原因+已做+建议）
            /goal [<目标>|run|resume]      自主执行：有参出菱形 DAG 计划（里程碑+
                                           判据）；/approve 后 /goal run 全流程
                                           无人干预跑到收口（判据真跑、失败自救
                                           ≤2 次、断点可续、假绿不留存）
            !<命令>                        跑一条 shell（真实 rc；默认自由面，
                                           硬阻断清单默认不可关（full_access 除外））
            @<路径>                        把文件内容内联进这一轮需求
                                           （@ 开头的路径 Tab 可补全）
            行尾 \\ 回车                    续行多行输入（连续 \\ 继续续行）
开箱即用：REPL 主端点连不通（403/400/不可达）时自动回退到本机 Ollama
（若在跑），stderr 会如实打印 [回退]。headless（-p）不回退：没端点就 rc=3。
退出码：0=给出了最终回答；2=用法/预算；3=基础设施（provider/HTTP/harness）
"""


def _build_parser():
    p = argparse.ArgumentParser(
        prog="agent_core",
        description="AgentOS 内置 agent 的内核：模型与真实工具之间的循环"
        "（只用标准库，不新开一套真相）",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "prompt", nargs="*", metavar="PROMPT", help="需求；给了一句就是 headless 模式"
    )
    p.add_argument(
        "-p",
        "--print",
        dest="print_mode",
        action="store_true",
        help="非交互：跑完把最终回答打到 stdout 后退出",
    )
    p.add_argument(
        "--mode", default="full", choices=list(MODES), help="权限模式，默认 full"
    )
    p.add_argument("--root", default=ROOT, help="仓库根，默认本文件上一级")
    p.add_argument(
        "--max-steps",
        type=int,
        default=MAX_STEPS,
        help="单任务最多几步，默认 %d，硬上限 %d" % (MAX_STEPS, MAX_STEPS),
    )
    p.add_argument(
        "--stream",
        action="store_true",
        help="边收边打（端点不支持流式时自动退化为整包）",
    )
    p.add_argument(
        "--no-stream",
        dest="no_stream",
        action="store_true",
        help="关掉 REPL 的流式输出（REPL 默认就开流式）",
    )
    p.add_argument(
        "--yes", action="store_true", help="审批一律点头（硬阻断清单仍然有效）"
    )
    p.add_argument(
        "--continue",
        dest="cont",
        action="store_true",
        help="续跑最近一份 agent 会话（work/agent_sessions/agent-*）",
    )
    p.add_argument(
        "--resume",
        default="",
        metavar="ID",
        help="续跑指定 agent 会话（/sessions 列 id）",
    )
    p.add_argument(
        "--provider",
        default="",
        metavar="NAME",
        choices=sorted(NAMED_PROVIDERS),
        help="命名 provider：%s；给了就以它为准" % " / ".join(sorted(NAMED_PROVIDERS)),
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="跑自检：本地桩端点 + 真实小文件，不碰真模型真网",
    )
    p.add_argument("--version", action="version", version="agent_core %s" % VERSION)
    return p


_RC_OF = {"usage": 2, "limit": 2, "provider": 3, "no-provider": 3}


def _rc_of_error(kind):
    """失败 kind → 退出码。全项目同一套口径，见模块 docstring。"""
    return _RC_OF.get(kind, 3)


def _make_approver(args):
    """审批者。没有 TTY 就返回 None —— _gate_write 把 None 当拒绝，
    这正是 headless 想要的行为：宁可拒绝，不能偷偷放行。"""
    if args.mode in ("full", "plan"):
        return None
    if args.yes:

        def _yes(name, a, why):
            sys.stderr.write("[自动批准 --yes] %s\n" % why)
            return True

        return _yes
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        return None

    def _ask(name, a, why):
        sys.stderr.write(
            "\n需要批准：%s\n  工具=%s  参数=%s\n  批准？[y/N] "
            % (why, name, json.dumps(a, ensure_ascii=False)[:300])
        )
        sys.stderr.flush()
        try:
            line = sys.stdin.readline()
        except Exception:  # noqa: BLE001
            return False
        return line.strip().lower() in ("y", "yes", "是")

    return _ask


def _event_printer(stream, seen=None):
    if not stream:
        return None

    def _ev(ev):
        t = ev.get("type")
        if t == "delta":
            if isinstance(seen, dict):
                seen["delta"] = True
            sys.stdout.write(ev.get("text") or "")
            sys.stdout.flush()
        elif t == "tool":
            # 文本逐字不动（README/量具引的就是这行原话），皮只包色码。
            sys.stdout.write(
                _c(
                    "dim",
                    "\n[工具 %s → %s%s]\n"
                    % (
                        ev.get("tool"),
                        "ok" if ev.get("ok") else "失败/被拒",
                        "" if ev.get("executed") else "（未执行）",
                    ),
                )
            )
            sys.stdout.flush()

    return _ev


# ── 过程可见性（Claude Code/Codex 式）：转轮 + 工具即调即显 ─────────────
# 用户诉求：干活时不要什么都不显示。模型思考/跑工具的静默段每 0.12s 重画
# 「✻ 状态… Ns · Ctrl-C 打断」；工具被调用的瞬间就上屏「⏺ 工具(参数)」，
# 结果随后 ⎿ 形态给出。只在交互 TTY 起线程；headless/管道零改动零输出。
class _TurnSpinner(object):
    """回合转轮。生命周期 = 一个 REPL 回合：start() … stop()。"""

    SHIMMER = "✻✽✶✳✢·"

    def __init__(self, enabled, colorize):
        self.enabled = bool(enabled)
        self._c = colorize
        self._stop = threading.Event()
        self._halt = threading.Event()
        self._state = "思考中"
        self._t0 = time.time()
        self._th = None

    def start(self):
        if self.enabled:
            self._th = threading.Thread(target=self._loop, daemon=True)
            self._th.start()
        return self

    def set(self, state):
        self._state = state or self._state

    def wake(self):
        self._halt.clear()

    def halt(self):
        self._halt.set()

    def pause(self):
        """清掉转轮行（只在它正显示时），给真行让出干净画布。"""
        if self.enabled and not self._halt.is_set():
            sys.stderr.write("\r\x1b[K")
            sys.stderr.flush()

    def stop(self):
        self._stop.set()
        if self.enabled:
            sys.stderr.write("\r\x1b[K")
            sys.stderr.flush()

    def _loop(self):
        i = 0
        while not self._stop.wait(0.12):
            if self._halt.is_set():
                continue
            s = self.SHIMMER[i % len(self.SHIMMER)]
            i += 1
            el = int(time.time() - self._t0)
            sys.stderr.write(
                "\r\x1b[K"
                + self._c("brand", "%s %s… %ds · Ctrl-C 打断" % (s, self._state, el))
            )
            sys.stderr.flush()


class _EscWatcher(object):
    """回合期 stdin 监听（蒸馏自 Claude Code 的 Esc 打断）。

    只在交互 TTY 启用；用 cbreak（关 ICANON/ECHO、留 ISIG）而非全 raw：
    Ctrl-C 仍走 SIGINT 原路，\n 仍由终端补 \r，箭头等 CSI 序列按噪声吞掉。
    Esc 单独按下（80ms 无后续）→ cancel Event 置位，SSE 行间/步间即中断。
    """

    _CSI_FINAL = range(0x40, 0x7F)

    def __init__(self, enabled, cancel):
        self.enabled = bool(enabled)
        self.cancel = cancel
        self._stop = threading.Event()
        self._old = None
        self._th = None

    def start(self):
        if not self.enabled:
            return self
        fd = sys.stdin.fileno()
        try:
            self._old = termios.tcgetattr(fd)
            new = list(self._old)
            new[3] &= ~(termios.ICANON | termios.ECHO)
            new[6][termios.VMIN] = 1
            new[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, new)
        except (termios.error, OSError):
            self._old = None
            return self
        import select as _sel

        self._th = threading.Thread(target=self._loop, args=(fd, _sel), daemon=True)
        self._th.start()
        return self

    def _loop(self, fd, sel):
        while not self._stop.is_set():
            try:
                r, _, _ = sel.select([fd], [], [], 0.1)
            except OSError:
                return
            if not r:
                continue
            try:
                b = os.read(fd, 1)
            except OSError:
                return
            if b == b"\x1b":
                rr, _, _ = sel.select([fd], [], [], 0.08)
                if not rr:
                    self.cancel.set()
                    continue
                try:
                    os.read(fd, 1)  # '[' 或 'O'
                    while True:
                        rr, _, _ = sel.select([fd], [], [], 0.05)
                        if not rr:
                            break
                        c = os.read(fd, 1)
                        if 0x40 <= c[0] < 0x7F:
                            break
                except OSError:
                    return
            # 其余字符丢弃：回合期不回显不打字（打断是唯一诉求）

    def stop(self):
        self._stop.set()
        if self._old is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old)
            except (termios.error, OSError):
                pass


# 工具的「CC 式短名」：Claude Code 那一行是 `⏺ Write(src/app.ts)` ——
# 工具名 + 一个关键参数。我们原来打的是**完整 JSON**
# （`write_file({"path": …, "content": "整段正文"})`）：一行几百字符，
# 又长又泄内容。现在只留**一个**关键参数。
_TOOL_SHORT = {
    "read_file": "Read",
    "write_file": "Write",
    "edit_file": "Edit",
    "run_cmd": "Bash",
    "list_dir": "List",
    "search": "Grep",
    "gh_search": "Search",
    "gh_repo_view": "Repo",
    "gh_file_read": "View",
    "git_status": "Status",
    "git_log": "Log",
    "git_diff": "Diff",
    "web_fetch": "Fetch",
    "subagent": "Agent",
    "skills": "Skill",
}


def _clip_tail(s, n):
    """太长的参数：留**尾巴**（文件名比前缀更有用）。"""
    s = " ".join(str(s or "").split())
    if len(s) <= n:
        return s
    return "…" + s[-(n - 1):]


def _tool_label(tool, args=None, limit=44):
    """工具调用的一行短标签：Write(hello.txt) / Bash(cat x) / Grep(pattern)。

    纯函数（可被门钉住）。规则：
    · 文件类工具只留**路径**；命令类只留**命令**；搜索类只留**模式**；
    · 参数太长就截断（留尾巴）；
    · 认不出的工具：第一个字符串参数，截断；连参数都没有就只留工具名。
    """
    name = str(tool or "")
    short = _TOOL_SHORT.get(name, name)
    a = args if isinstance(args, dict) else {}

    def _s(k):
        v = a.get(k)
        return "" if v is None else str(v)

    if name in ("read_file", "write_file", "edit_file", "list_dir"):
        arg = _s("path")
    elif name == "run_cmd":
        argv = a.get("argv")
        if isinstance(argv, str):
            arg = argv
        elif isinstance(argv, (list, tuple)):
            arg = " ".join(str(x) for x in argv)
        else:
            arg = _s("cmd")
    elif name == "search":
        arg = _s("pattern") or _s("query")
    elif name == "gh_search":
        arg = _s("query") or _s("q")
    elif name in ("gh_repo_view", "gh_file_read"):
        arg = _s("slug") or _s("repo") or _s("path")
    elif name == "subagent":
        arg = _s("prompt")
    elif name == "web_fetch":
        arg = _s("url")
    elif name == "todo":
        # 账本工具的参数是**清单**，一行 JSON 化会碎成渣（实测打成
        # "…': 3, 'state': 'doing'…"）。这里只报「几项」。
        _its = a.get("items")
        if isinstance(_its, list):
            arg = "%d 项" % len(_its)
        else:
            arg = "看清单"
    else:
        arg = ""
        for v in a.values():
            if isinstance(v, str) and v.strip():
                arg = v
                break
        if not arg:
            for v in a.values():
                if isinstance(v, (list, tuple)) and v:
                    arg = " ".join(str(x) for x in v)
                    break
    arg = _clip_tail(arg, limit)
    return "%s(%s)" % (short, arg) if arg else short


def _args_one_line(args):
    try:
        s = json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(args)
    return " ".join(str(s).split())[:72]


# ── W5 终端 UX 纯函数层（Claude Code 手感）：续行 / @补全 / ⎿详情 / 缩进 ──
_REPL_ROOT = [None]  # 交互补全用：当前会话 root（_repl 启动时写入）


def _line_wants_continue(line):
    """行尾奇数个反斜杠 = 续行（`\\\\` 是字面反斜杠，不续）。纯函数。"""
    s = str(line or "").rstrip()
    n = 0
    while n < len(s) and s[len(s) - 1 - n] == "\\":
        n += 1
    return n % 2 == 1


def _compose_join(acc, nxt):
    """把续行段拼进已收文本：去掉一个续行标记、以换行连接。纯函数。"""
    s = str(acc or "")
    if s.rstrip().endswith("\\"):
        s = s.rstrip()[:-1]
    return s + "\n" + str(nxt or "")


def _at_completions(tok, root, limit=20):
    """`@` 开头的 Tab 补全：root 内相对路径列候选（目录带 /、隐藏项跳过）。"""
    rel = str(tok or "")[1:]
    base = os.path.dirname(rel)
    frag = os.path.basename(rel)
    d = os.path.join(root or ".", base) if base else (root or ".")
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    out = []
    for n in names:
        if n.startswith(".") or not n.startswith(frag):
            continue
        p = os.path.join(base, n) if base else n
        out.append("@%s%s" % (p, "/" if os.path.isdir(os.path.join(d, n)) else ""))
        if len(out) >= limit:
            break
    return out


def _tool_summary(name, args, res):
    """编辑类工具的 ⎿ 详情行（Claude Code 风格）；无详情返回 ""。

    只对**真的发生且成功**的写体现详情：被拒/未执行/失败一律空 ——
    详情行是「发生了什么」的如实描述，不是装饰。
    """
    if not isinstance(res, dict) or not res.get("ok"):
        return ""
    a = args if isinstance(args, dict) else {}
    if name == "edit_file":
        old = str(a.get("old_string") or "")
        new = str(a.get("new_string") or "")
        if not old and not new:
            return ""
        o, n = old.splitlines(), new.splitlines()
        i = 0
        while i < len(o) and i < len(n) and o[i] == n[i]:
            i += 1
        j = 0
        while (
            j < len(o) - i and j < len(n) - i and o[len(o) - 1 - j] == n[len(n) - 1 - j]
        ):
            j += 1
        added, removed = len(n) - i - j, len(o) - i - j
        seg = "%s · +%d −%d" % (str(a.get("path") or "?")[:48], added, removed)
        if i < len(n) - j:
            seg += " · %s" % n[i].strip()[:48]
        return seg
    if name == "write_file":
        body = str(a.get("content") or "")
        return "%s · 写入 %d 行 / %d 字节" % (
            str(a.get("path") or "?")[:48],
            body.count("\n") + (1 if body else 0),
            len(body.encode("utf-8")),
        )
    return ""


def _format_answer(ans):
    """终答渲染：续行缩进两格（Claude Code 的 ⏺ 块手感）。纯函数。"""
    s = str(ans or "").strip()
    if not s:
        return "（无终答）"
    return s.replace("\n", "\n  ")


def _turn_ui(sp, printer, seen):
    """REPL 回合 UI 编排（过程可见性的心脏）。

    delta：转轮让位（halt+清行），首个增量前打 ⏺，正文直打——printer 不经
    手 delta（防双打），但 seen["delta"] 由这里置位。tool_call：整行打
    「⏺ 工具(参数)」，转轮改「执行 X」继续转，且不再转发 printer（headless
    -p --stream 的旧输出形状一字不动）。tool/step：转轮回「思考中」。
    非交互（sp 未启用）时 tool_call 不上屏——管道里保持旧行数。
    """
    first = {"d": False}

    def _ev(ev):
        t = ev.get("type")
        if t == "delta":
            if sp is not None:
                sp.halt()
                sp.pause()
            if not first["d"]:
                first["d"] = True
                sys.stdout.write(_c("brand", "⏺ "))
            sys.stdout.write(ev.get("text") or "")
            sys.stdout.flush()
            seen["delta"] = True
            return
        if sp is not None and sp.enabled:
            sp.pause()
            if t == "tool_call":
                sp.set("执行 " + str(ev.get("tool") or "?"))
                sys.stdout.write(
                    _c("brand", "⏺ ")
                    + _c("cyan", _tool_label(ev.get("tool"), ev.get("args")))
                    + "\n"
                )
                sys.stdout.flush()
            else:
                sp.set("思考中")
            sp.wake()
        if t == "tool_call":
            return
        if t == "tool" and sp is not None and sp.enabled:
            # 交互 REPL：**一次工具 = 一行**（CC 手感）。
            # 原来这里会多打一行「[工具 X → ok]」，加上 ⎿ 详情就是两行 ——
            # 一次工具两行、三个工具六行，工作过程被撑得很长。
            # 现在：⏺ Write(hello.txt) 一行 + ⎿ 结果一行（失败时说明原因）。
            # headless / -p --stream 的既有输出形状**一字不动**（那里仍打
            # 「[工具 X → ok]」，那是给 CI 解析用的稳定形状）。
            _ts = str(ev.get("summary") or "")
            if not _ts and not ev.get("ok"):
                _ts = "失败：%s" % str(ev.get("error") or "未执行/被拒")[:60]
            if _ts:
                sys.stdout.write(_c("dim", "  ⎿ %s\n" % _ts))
                sys.stdout.flush()
            # 任务账本面板（Claude Code 的 TodoWrite 那一层）：
            # 多行小面板，让「做到第几步」在终端里一眼可见。
            _tp = str(ev.get("todo_panel") or "")
            if _tp:
                for _ln in _tp.splitlines():
                    sys.stdout.write(_c("dim", "    %s\n" % _ln))
                sys.stdout.flush()
            return
        if printer is not None:
            printer(ev)

    return _ev


def _report(res, stream, seen=None):
    streamed = bool(stream and isinstance(seen, dict) and seen.get("delta"))
    if streamed:
        sys.stdout.write("\n")  # 增量已经逐字打过了，别把终答再抄一遍
    else:
        sys.stdout.write("%s\n" % (res.get("answer") or "").strip())
    denied = [u for u in res.get("tools_used") or [] if not u.get("executed")]
    sys.stdout.write(
        "\n--- agent_core：%d 步，%.1fs，工具调用 %d 次"
        "（其中 %d 次未执行：被拒/参数不对）\n"
        % (
            res.get("steps") or 0,
            res.get("elapsed") or 0.0,
            len(res.get("tools_used") or []),
            len(denied),
        )
    )
    if res.get("kind") == "limit":
        sys.stderr.write("预算用尽：%s\n" % res.get("error"))
        if res.get("last_text"):
            sys.stderr.write("  （卡在末句：%r）\n" % res.get("last_text"))
    if res.get("empty_answer"):
        sys.stderr.write("（模型返回空内容：多为端点抖动或模型卡壳——可重试或换端点）\n")
    return 0


def _run_once(args):
    prompt = " ".join(args.prompt).strip()
    if not prompt:
        raise AgentError("usage", "headless 模式需要一句非空需求：agent_core -p '...'")
    ctx = {
        "root": args.root,
        "mode": args.mode,
        "approver": _make_approver(args),
        "max_steps": args.max_steps,
        # headless 也配 sid：DSP 大任务自启的计划要落盘（work/plans/），
        # 无 sid 自启无处落。这是 headless 与 REPL 唯一不同的接线。
        "sid": "cli-%s" % time.strftime("%Y%m%d-%H%M%S"),
    }
    _sf9 = _safety_flags(_read_agentos_config())
    ctx.update(_sf9)
    try:
        ctx["full_access"] = _full_access(_read_agentos_config())
    except Exception:
        ctx["full_access"] = False
    if ctx.get("full_access"):
        sys.stderr.write(
            "[权限] 破坏性命令放行（逐条留痕 work/destructive_audit.jsonl）\n"
        )
    _on9 = [
        n
        for n, k in (
            ("计划锁", "plan_lock_off"),
            ("权限模式", "mode_gate_off"),
            ("命令白名单", "cmd_gate_off"),
        )
        if not _sf9[k]
    ]
    if _on9:
        sys.stderr.write(
            "[安全闸] 上锁：%s（~/.agentos/config.toml [agent] "
            "字面 true 才上锁；硬阻断清单默认不可关（full_access 除外））\n"
            % "、".join(_on9)
        )
    else:
        sys.stderr.write(
            "[安全闸] 自由面：计划锁/权限/白名单均关"
            "（硬阻断清单始终在）\n"
        )
    seen = {"delta": False}
    _mcp_sync()  # headless 也挂 MCP 服务器工具（配置里有 [mcp] 才动）
    _hooks_session(ctx)  # hooks SessionStart（有 hooks.json 才动）
    res = run_agent(
        prompt, ctx=ctx, stream=args.stream, on_event=_event_printer(args.stream, seen)
    )
    return _report(res, args.stream, seen)


# ── REPL 交互皮：Claude Code 手感（只在交互 TTY 生效）────────────────
# 总闸是 stdout.isatty()：headless（-p / --check / 管道喂 stdin）永远零
# ANSI，cli_gate / agent_core_gate 的管道量具吃不到一个色码；交互终端
# （TERM 带 256color 或 COLORTERM=truecolor/24bit）才上 Claude 橙皮。
_SKIN_SGR = {
    "brand": "\x1b[38;2;217;119;87m",  # Claude 橙 #D97757（与 TUI 同源）
    "cyan": "\x1b[38;2;126;168;188m",
    "dim": "\x1b[38;2;146;139;132m",
    "bold": "\x1b[1m",
    "r": "\x1b[0m",
}


def _skin():
    t = os.environ.get("TERM", "")
    if os.environ.get("NO_COLOR") or t in ("", "dumb"):
        return {}
    if not sys.stdout.isatty():
        return {}
    if "256color" not in t and os.environ.get("COLORTERM", "") not in (
        "truecolor",
        "24bit",
    ):
        return {}
    return _SKIN_SGR


def _c(*ks):
    """_c("brand", "bold", 文本)：前面的键取 SGR，末参是正文；无皮原样。"""
    s = ks[-1]
    S = _skin()
    if not S:
        return s
    return "".join(S[k] for k in ks[:-1]) + s + S["r"]


def _dw(s):
    """终端显示列宽：CJK/全角 2 列，其余 1 列（欢迎框对齐用）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _clip72(s, w):
    """按显示宽截断补省略号（欢迎框内自裁，不依赖终端宽度探测）。"""
    s = str(s)
    if _dw(s) <= w:
        return s
    out, used = [], 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used + cw > w - 1:
            break
        out.append(ch)
        used += cw
    return "".join(out) + "…"


REPL_SLASH_CMDS = (
    "/help",
    "/tools",
    "/mode",
    "/status",
    "/model",
    "/cost",
    "/undo",
    "/export",
    "/stats",
    "/compact",
    "/clear",
    "/reset",
    "/diff",
    "/init",
    "/memory",
    "/ideas",
    "/sessions",
    "/rewind",
    "/skill",
    "/branch",
    "/verify",
    "/steer",
    "/watch",
    "/evo",
    "/evo-apply",
    "/evo-off",
    "/plan",
    "/approve",
    "/tasks",
    "/block",
    "/goal",
    "/exit",
    "/quit",
    "/q",
)


def _repl_welcome(provider, ctx, sid, resumed):
    """Claude Code 式欢迎行：开 REPL 先看见「在哪、用什么模型、怎么使」。

    走 stderr（REPL 的 UI 全在 stderr，stdout 只留答案/数据 —— 管道喂
    stdin 的量具在 stdout 上拿到的仍是纯数据）。

    **形状从 13 行的框压到 3 行**（2026-10-20 用户诉求：「工作过程不要显示
    那么长，要和 cc 一样」）。框里 5 行信息塞进 2 行：模型与模式一行、
    会话与工具数一行、提示一行。信息一条没少（还是现取真值），只是不占屏。
    """
    tip = _clip72(
        "/help · /goal 自主执行 · /evo 自进化 · /exit 或 Ctrl-D 退出 · !cmd · @文件",
        76,
    )
    line1 = _clip72(
        "%s @ %s · %s · v%s · %s"
        % (
            provider.model or "-",
            provider.base_url or "-",
            ctx["mode"],
            VERSION,
            ctx["root"],
        ),
        76,
    )
    line2 = _clip72(
        "会话 %s（%s）· 工具 %d 个 · /tools 看清单与风险级"
        % (
            sid,
            "续跑 %d 条" % len(ctx.get("history") or []) if resumed else "新开",
            len(TOOLS),
        ),
        76,
    )
    out = [
        _c("brand", "bold", "✻ Welcome to AgentOS!") + "  " + _c("dim", line1),
        "  " + _c("cyan", line2),
        "  " + _c("dim", tip),
    ]
    sys.stderr.write("\n".join(out) + "\n")


def _setup_readline():
    """TTY 才装：readline 编辑/历史（~/.agentos/agent_history）+ 斜杠命令
    Tab 补全。管道/CI 不装 —— 量具喂行不能带编辑态副作用。"""
    if not sys.stdin.isatty():
        return
    try:
        import readline
    except ImportError:
        return

    def _comp(text, state):
        if text.startswith("@"):
            opts = _at_completions(text, _REPL_ROOT[0])
            if state < len(opts):
                return opts[state] + ("" if opts[state].endswith("/") else " ")
            return None
        opts = [c for c in REPL_SLASH_CMDS if c.startswith(text)]
        return (opts[state] + " ") if state < len(opts) else None

    readline.set_completer(_comp)
    readline.set_completer_delims(" \t\n")
    readline.parse_and_bind("tab: complete")
    hpath = os.path.expanduser("~/.agentos/agent_history")

    def _save():
        try:
            readline.set_history_length(500)
            readline.write_history_file(hpath)
        except Exception:
            pass

    try:
        os.makedirs(os.path.dirname(hpath), exist_ok=True)
        readline.read_history_file(hpath)
    except Exception:
        pass
    import atexit

    atexit.register(_save)


def _readline(tag):
    """读一行输入。TTY 走 input()+readline（历史/编辑/Tab 补全）；管道/CI
    走裸 sys.stdin.readline（无编辑态）。两者 EOF 都归 None（Ctrl-D 退出）；
    TTY 下 Ctrl-C 只清行不退出（对齐 Claude Code：/exit 或 Ctrl-D 才走）。"""
    if sys.stdin.isatty():
        try:
            return input(tag)
        except EOFError:
            return None
        except KeyboardInterrupt:
            return ""
    try:
        sys.stdout.write(tag)
        sys.stdout.flush()
        line = sys.stdin.readline()
        # stdin 关闭时 readline() 返回空串，而不是抛 EOFError。
        # 必须在此转成 None，REPL 才能像 Claude Code 的 Ctrl-D 一样退出。
        return line if line else None
    except (EOFError, KeyboardInterrupt):
        return None


# ── REPL 会话记录 + 纯函数工具（对齐 claude code 的终端手感）──────────
# 为什么把会话落在 work/agent_sessions/agent-*.jsonl：CLI 自己的 headless
# 历史是同目录的 cli-*.jsonl（形状是「动作 + rc + 输出行」），两者语义不同
# —— agent 会话是「用户/助手逐轮对话」。混用一个前缀会让 `agentos sessions`
# 把两类东西读成一种。所以另起 agent- 前缀，互不接管对方的命名空间。
# 2026-10-21（TASK2 障碍 2）：本块已搬到 tools/agent_session.py，这里 re-export。
from agent_session import (  # noqa: F401
    SESSION_DIR,
    TRANSCRIPT_PREFIX,
    COMPACT_KEEP,
    EVO_SIGNALS_PATH,
    EVO_RULES_PATH,
    EVO_MIN_COUNT,
    _EVO_ADVICE,
    _EVO_ADVICE_GENERIC,
    _EVO_CONVERGE_TUNE,
    _evo_signal,
    _evo_rules_load,
    _evo_rules_save,
    _evo_mine,
    _evo_set_state,
    _TOOL_DESC_ORIG,
    _evo_refresh_tool_hints,
    EVO_PROPOSAL_KINDS,
    validate_proposal,
    _evo_propose,
    _evo_midflight,
    _evo_converge_policy,
    _evo_prompt_lines,
    _MEMORY_CAP,
    _memory_files,
    _memory_prompt_lines,
    _IDEA_MARK_RE,
    _ideas_scan,
    _repl_ideas,
    _session_dir,
    _new_session_id,
    _transcript_path,
    _transcript_list,
    _transcript_append,
    _transcript_load,
    _history_from_transcript,
    _safe_cut,
    _compact_history,
    DIGEST_PREFIX,
    DIGEST_PROMPT,
    DIGEST_MAX_CHARS,
    DIGEST_MSG_CHARS,
    _digest_input,
    _compact_budget,
    _compact_history_v2,
    _rewind_replay,
    _usage_total,
    _expand_at_refs,
    _status_line,
)


def _repl_session(args):
    """决定这次 REPL 的 session id 与要回灌的历史 -> (sid, resumed|None)。

    --resume <id> 优先；否则 --continue 取最近一份；都没有就是新会话。
    续不上就如实报一句再起新会话 —— 不让「找不到历史」把整个 REPL 卡死。
    """
    want = (getattr(args, "resume", "") or "").strip()
    cont = bool(getattr(args, "cont", False))
    files = _transcript_list()
    hit = None
    if want:
        for p in files:
            if os.path.basename(p)[: -len(".jsonl")] == want:
                hit = p
                break
        if hit is None:
            sys.stderr.write(
                "warn: 找不到会话 %s，起新会话（/sessions 列全部）\n" % want
            )
    elif cont:
        hit = files[-1] if files else None
        if hit is None:
            sys.stderr.write("warn: 没有可续的 agent 会话，起新会话\n")
    if hit is None:
        return _new_session_id(), None
    try:
        recs = _transcript_load(hit)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("warn: 会话读不动（%s），起新会话\n" % str(e)[:80])
        return _new_session_id(), None
    sid = os.path.basename(hit)[: -len(".jsonl")]
    return sid, {"id": sid, "history": _history_from_transcript(recs)}


def _repl_diff(ctx):
    """`/diff`：这个仓库当下的改动（git status --short + diff --stat）。"""
    st = _git({}, ctx, ["status", "--short"])
    if not st.get("ok"):
        sys.stderr.write(
            "不是 git 仓库 / git 不可用：%s\n" % (st.get("error") or "?")[:160]
        )
        return
    body = (st.get("out") or "").rstrip("\n")
    sys.stdout.write(body + "\n" if body else "(工作区干净)\n")
    df = _git({}, ctx, ["diff", "--stat"])
    if df.get("ok") and (df.get("out") or "").strip():
        sys.stdout.write((df.get("out") or "").rstrip("\n") + "\n")


def _repl_init(ctx):
    """`/init`：在仓库根放一份起步 AGENTS.md（已存在就不动）。"""
    path = os.path.join(ctx["root"], "AGENTS.md")
    if os.path.exists(path):
        sys.stderr.write("已存在，未改动：%s\n" % path)
        return
    body = (
        "# AGENTS.md\n\n"
        "本仓库的操作约定。agentos agent 每轮都会读它。\n\n"
        "- 验收以命令退出码为据，禁未验证宣称。\n"
        "- 对外操作（push/tag/发布）须用户明确说「发」。\n"
        "- 删文件用 unlink，不要 rm -f。\n"
    )
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
    except OSError as e:
        sys.stderr.write("写不动 %s：%s\n" % (path, str(e)[:120]))
        return
    sys.stdout.write("已写 %s（%d 字节）\n" % (path, len(body.encode("utf-8"))))


def _repl_bang(cmd, ctx):
    """`!cmd`：把一行 shell 交给工具层同一条路径，真实 rc 打到屏幕。

    走 exec_tool 而不是自己起 subprocess —— 命令面（默认自由 / 显式
    cmd_gate 收窄为只读白名单）、硬阻断、越界、输出截断全在工具层那一条
    路径上，REPL 不另开一套真相。
    """
    argv = cmd.strip().split()
    if not argv:
        sys.stderr.write("用法：!<命令>，如 !git status\n")
        return
    res, _ex = exec_tool("run_cmd", {"argv": argv, "timeout": 20}, ctx)
    if not res.get("ok"):
        sys.stderr.write("拒绝/失败：%s\n" % (res.get("error") or "?"))
        return
    out = (res.get("out") or "").rstrip("\n")
    if out:
        sys.stdout.write(out + "\n")
    sys.stdout.write(
        "rc=%s · %.1fs%s\n"
        % (
            res.get("rc"),
            res.get("dur") or 0.0,
            " · 输出已截断" if res.get("truncated") else "",
        )
    )


def _repl_slash(text, ctx, provider, sid, tpath):
    """处理一条 /命令。返回 "exit" / 新 Provider / None（继续）。

    命令集对齐 claude code 的终端手感：/help /tools /mode /status /model
    /cost /compact /clear(/reset) /diff /init /sessions /exit。所有读数都
    现取，不缓存、不估算；不认识的命令只报一句，不退出。
    """
    parts = text.split(None, 1)
    cmd = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""
    if cmd in ("/exit", "/quit", "/q"):
        return "exit"
    if cmd == "/help":
        sys.stderr.write(EPILOG)
        return None
    if cmd == "/tools":
        for n in sorted(TOOLS):
            sys.stderr.write(
                "  %-12s %-6s %s\n" % (n, TOOLS[n]["risk"], TOOLS[n]["description"])
            )
        return None
    if cmd == "/evo":
        rules, err = _evo_mine()
        if err:
            sys.stderr.write("规则账不可读（fail-open，本轮无规则注入）：%s\n" % err)
        sys.stderr.write(
            "信号账: %s\n规则账: %s\n" % (EVO_SIGNALS_PATH, EVO_RULES_PATH)
        )
        stats = {"n": 0, "ok": 0, "itp": 0, "tf": {}, "den": {}}
        try:
            with open(EVO_SIGNALS_PATH, encoding="utf-8") as f:
                _rows = [json.loads(x) for x in f if x.strip()][-50:]
        except (OSError, ValueError):
            _rows = []
        for r0 in _rows:
            stats["n"] += 1
            stats["ok"] += 1 if r0.get("ok") else 0
            stats["itp"] += 1 if r0.get("err_kind") == "interrupted" else 0
            for t in r0.get("tools_fail") or []:
                stats["tf"][t] = stats["tf"].get(t, 0) + 1
            for t in r0.get("denied") or []:
                stats["den"][t] = stats["den"].get(t, 0) + 1
        if stats["n"]:
            sys.stderr.write(
                "自评（近 %d 轮）：成功 %d/%d · 被打断 %d · 工具失败 top %s "
                "· 被拒 top %s\n"
                % (
                    stats["n"],
                    stats["ok"],
                    stats["n"],
                    stats["itp"],
                    sorted(stats["tf"].items(), key=lambda x: -x[1])[:3],
                    sorted(stats["den"].items(), key=lambda x: -x[1])[:3],
                )
            )
        if not rules:
            sys.stderr.write(
                "（还没有规则：同签名失败满 %d 条自动聚成 CANDIDATE）\n" % EVO_MIN_COUNT
            )
        for r in rules:
            sys.stderr.write(
                "%s [%s] ×%d  %s\n"
                % (r.get("id"), r.get("state"), r.get("count"), r.get("advice"))
            )
        sys.stderr.write(
            "晋升: /evo-apply <id> · 回退: /evo-off <id>"
            "（ACTIVE 下一轮起注入 system prompt）\n"
        )
        return None
    if cmd == "/evo-apply":
        if not rest.strip():
            sys.stderr.write("用法: /evo-apply <规则id>（/evo 先看清单）\n")
        elif _evo_set_state(rest.strip(), "ACTIVE"):
            sys.stderr.write(
                "%s → ACTIVE（下一轮起注入 system prompt；"
                "/evo-off 可回退）\n" % rest.strip()
            )
        else:
            sys.stderr.write("没这条规则：%r（/evo 先看清单）\n" % rest.strip())
        return None
    if cmd == "/evo-off":
        if not rest.strip():
            sys.stderr.write("用法: /evo-off <规则id>\n")
        elif _evo_set_state(rest.strip(), "CANDIDATE"):
            sys.stderr.write("%s → CANDIDATE（已回退，不再注入）\n" % rest.strip())
        else:
            sys.stderr.write("没这条规则：%r\n" % rest.strip())
        return None
    if cmd == "/export":
        src = _transcript_path(sid)
        rows = []
        try:
            with open(src, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        rows.append(json.loads(ln))
                    except ValueError:
                        pass
        except OSError:
            rows = []
        out_dir = os.path.join(_session_dir(), "exports")
        os.makedirs(out_dir, exist_ok=True)
        dst = os.path.join(out_dir, "%s.md" % sid)
        with open(dst, "w", encoding="utf-8") as f:
            f.write("# AgentOS 会话 %s\n\n" % sid)
            for r in rows:
                who = (
                    "用户"
                    if r.get("kind") == "user"
                    else ("助手" if r.get("kind") == "assistant" else "…")
                )
                f.write(
                    "**%s**：%s\n\n" % (who, " ".join(str(r.get("text") or "").split()))
                )
        sys.stderr.write("已导出 %s（%d 条）\n" % (dst, len(rows)))
        return None
    if cmd == "/stats":
        sys.stderr.write(_status_line(ctx, sid) + "\n")
        sys.stderr.write(
            "用量：%s\n" % json.dumps(ctx.get("usage_total") or {}, ensure_ascii=False)
        )
        return None
    if cmd == "/undo":
        sd = os.path.join(
            ctx.get("snap_dir") or _SNAP_ROOT, str(ctx.get("sid") or "nosid")
        )
        man = os.path.join(sd, "manifest.jsonl")
        rows = []
        try:
            with open(man, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if ln:
                        try:
                            rows.append(json.loads(ln))
                        except ValueError:
                            pass
        except OSError:
            rows = []
        tgt = None
        for r in reversed(rows):
            if not r.get("consumed"):
                tgt = r
                break
        if tgt is None:
            sys.stderr.write("（没有可撤销的写文件快照）\n")
            return None
        try:
            if tgt.get("existed"):
                shutil.copy2(os.path.join(sd, tgt.get("backup") or ""), tgt["target"])
                act = "已还原 %s（自快照 %s）" % (tgt["target"], tgt.get("backup"))
            else:
                if os.path.exists(tgt["target"]):
                    os.unlink(tgt["target"])
                act = "已删除 %s（快照时点该文件尚不存在）" % tgt["target"]
            tgt["consumed"] = True
            with open(man, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
            sys.stderr.write("%s\n" % act)
        except OSError as e:
            sys.stderr.write("FAIL: 撤销失败：%s\n" % str(e)[:160])
        return None
    if cmd == "/memory":
        root = ctx.get("root") or ROOT
        files = _memory_files(root)
        if files:
            for name, body in files:
                sys.stderr.write(
                    "记忆 %s（%d 字节，单文件封顶 %d）：\n"
                    % (name, len(body), _MEMORY_CAP)
                )
                for ln in body.splitlines()[:8]:
                    sys.stderr.write("  %s\n" % ln)
        else:
            sys.stderr.write(
                "（%s 下没有 AGENTS.md / AGENTOS.md——"
                "建一个，每轮 system prompt 就带上项目记忆）\n" % root
            )
        rules, _ = _evo_rules_load()
        n_act = sum(1 for r in rules if r.get("state") == "ACTIVE")
        sys.stderr.write("自进化 ACTIVE 规则 %d 条（/evo 看清单）\n" % n_act)
        return None
    if cmd == "/ideas":
        _repl_ideas(ctx)
        return None
    if cmd == "/plan":
        # DSP 计划：rest 为空 = 看当前计划；否则 rest 当 goal 建新计划。
        # 新计划含 repo-map 侦察注脚 + 未审批锁（写工具锁死，/approve 解锁）。
        try:
            dsp_mod = _dsp()
        except ImportError:
            sys.stderr.write(
                "DSP 模块缺失（tools/dsp.py 不在）:计划能力不可用，主循环不受影响\n"
            )
            return None
        if not rest.strip():
            plan, err = dsp_mod.load_plan(sid)
            if plan is None:
                sys.stderr.write(
                    "还没有计划：/plan <一句话目标> 建一个"
                    "（先出任务树，人审后再动手）\n"
                )
            else:
                sys.stderr.write(dsp_mod.render_plan(plan) + "\n")
            return None
        # 建新计划：goal 一句 + repo-map 自动侦察 + 默认三任务骨架。
        # 骨架只是起点——模型/人随后用旧 string 换新 string 精修每项 accept。
        rmap = dsp_mod.repo_map(ctx.get("root") or ROOT)
        tasks = [
            {
                "id": "t1",
                "title": "侦察：读关键文件、确认现状",
                "accept": "关键文件已读 + 现状一句话 + rc=0",
                "tools": [
                    "list_dir",
                    "read_file",
                    "search",
                    "gh_search",
                    "gh_repo_view",
                    "gh_file_read",
                ],
                "depends": [],
            },
            {
                "id": "t2",
                "title": "动手：按判据改代码",
                "accept": "改动落盘 + 验证命令 rc=0（证据行）",
                "tools": [
                    "read_file",
                    "search",
                    "write_file",
                    "edit_file",
                    "run_cmd",
                    "git_diff",
                ],
                "depends": ["t1"],
            },
            {
                "id": "t3",
                "title": "收尾：自检 + 回执",
                "accept": "自检 rc=0 + 回执含真实 rc",
                "tools": ["run_cmd", "git_diff", "git_status", "git_log"],
                "depends": ["t2"],
            },
        ]
        plan = dsp_mod.new_plan(rest.strip(), tasks)
        ok, err = dsp_mod.save_plan(plan, sid)
        if not ok:
            sys.stderr.write("计划 schema 非法：%s\n" % err)
            return None
        sys.stderr.write(dsp_mod.render_plan(plan) + "\n")
        if rmap:
            sys.stderr.write(
                "— 仓库结构（repo-map，截断 %dB 内）—\n%s\n"
                % (dsp_mod.REPO_MAP_CAP, rmap)
            )
        sys.stderr.write(
            "下一步：检查每项判据（accept），没问题就 /approve"
            " 解锁执行；判据不对先改 plan.json 再 /approve。\n"
        )
        return None
    if cmd == "/approve":
        try:
            dsp_mod = _dsp()
        except ImportError:
            sys.stderr.write("DSP 模块缺失：无计划可批\n")
            return None
        ok, err = dsp_mod.approve_plan(sid)
        if ok:
            nxt = dsp_mod.next_task(dsp_mod.load_plan(sid)[0])
            sys.stderr.write(
                "计划已审批（写工具解锁）%s\n"
                % ("· 先做 %s" % nxt if nxt else "· 无待办")
            )
            ctx["dsp_task"] = nxt
        else:
            sys.stderr.write("批不了：%s\n" % err)
        return None
    if cmd == "/tasks":
        try:
            dsp_mod = _dsp()
        except ImportError:
            sys.stderr.write("DSP 模块缺失\n")
            return None
        plan, err = dsp_mod.load_plan(sid)
        if plan is None:
            sys.stderr.write("还没有计划（/plan 先出计划）\n")
            return None
        sys.stderr.write(dsp_mod.render_plan(plan) + "\n")
        nxt = dsp_mod.next_task(plan)
        if nxt:
            if (rest.strip() or nxt) != (
                ctx.get("dsp_task") or ""
            ) and rest.strip() in [t.get("id") for t in plan.get("tasks", [])]:
                ctx["dsp_task"] = rest.strip()
                sys.stderr.write("当前任务 → %s\n" % rest.strip())
            elif not ctx.get("dsp_task"):
                ctx["dsp_task"] = nxt
                sys.stderr.write("当前任务 → %s（下一个可做）\n" % nxt)
            else:
                sys.stderr.write(
                    "当前任务 %s（/tasks <id> 切换）\n" % ctx.get("dsp_task")
                )
        else:
            sys.stderr.write("（无待办：全 done 或卡住——/block 看偏离）\n")
        return None
    if cmd == "/block":
        try:
            dsp_mod = _dsp()
        except ImportError:
            sys.stderr.write("DSP 模块缺失\n")
            return None
        why = rest.strip() or "偏离计划、无进展（手动 BLOCK）"
        done = (
            [t.get("id") for t in (dsp_mod.load_plan(sid)[0] or {}).get("tasks", [])]
            if dsp_mod.load_plan(sid)[0]
            else []
        )
        p = dsp_mod.write_block(sid, why, done, "把当前任务拆小，或重出计划")
        sys.stderr.write("已 BLOCK：%s\n原因：%s\n" % (p or "（落盘失败）", why))
        return None
    if cmd == "/goal":
        # 自主执行：/goal <目标> 出菱形 DAG 计划；/goal run 一次审批后全
        # 流程无人干预跑到收口（派发/验证/自救≤2 次/断点可续/诚实报告）；
        # /goal resume 同 run（已完成任务零重跑）；空参看计划状态。
        try:
            dsp_mod = _dsp()
            leader_mod = _leader_mod()
            autonomy_mod = _autonomy()
        except ImportError:
            sys.stderr.write(
                "DSP/leader/autonomy 模块缺失：自主执行不可用，主循环不受影响\n"
            )
            return None
        arg = rest.strip()
        if not arg:
            plan, err = dsp_mod.load_plan(sid)
            if plan is None:
                sys.stderr.write(
                    "还没有目标计划：/goal <一句话目标> 出 "
                    "DAG 计划（/approve 后 /goal run 全流程"
                    "自跑）\n"
                )
            else:
                sys.stderr.write(dsp_mod.render_plan(plan) + "\n")
            return None
        if arg in ("run", "resume"):
            plan, err = dsp_mod.load_plan(sid)
            if plan is None:
                sys.stderr.write("还没有计划：/goal <目标> 先出计划\n")
                return None
            if not plan.get("approved"):
                sys.stderr.write(
                    "计划未审批：先 /approve（自主引擎不绕计划锁），再 /goal run\n"
                )
                return None
            rc = autonomy_mod.run_goal(
                sid,
                root=ctx.get("root") or ROOT,
                mode=arg,
                log=lambda s: sys.stderr.write(s + "\n"),
            )
            sys.stderr.write(
                "autonomy 收口 rc=%d（报告 work/autonomy/"
                "%s-REPORT.md；断点续跑 /goal resume）\n" % (rc, sid)
            )
            return None
        plan = leader_mod.compile_goal(arg, ctx.get("root") or ROOT)
        ok, err = dsp_mod.save_plan(plan, sid)
        if not ok:
            sys.stderr.write("计划 schema 非法：%s\n" % err)
            return None
        sys.stderr.write(dsp_mod.render_plan(plan) + "\n")
        sys.stderr.write(
            "下一步：检查每项判据（可给任务补 verify 命令组，"
            "引擎会真跑），/approve 审批后 /goal run 全流程"
            "无人干预跑到收口。\n"
        )
        return None
    if cmd in ("/reset", "/clear"):
        ctx["history"] = []
        sys.stderr.write("上下文已清空（模式仍是 %s）\n" % ctx["mode"])
        return None
    if cmd == "/compact":
        before = len(ctx.get("history") or [])
        ctx["history"], dropped, _dg = _compact_history_v2(
            ctx.get("history"), COMPACT_KEEP, provider=provider, focus=rest
        )
        sys.stderr.write(
            "上下文压缩：%d -> %d 条（丢 %d，保留最近 %d；摘要=%s）\n"
            % (
                before,
                len(ctx["history"]),
                dropped,
                COMPACT_KEEP,
                "有" if _dg else "无",
            )
        )
        return None
    if cmd == "/rewind":
        try:
            recs = _transcript_load(tpath) if os.path.isfile(tpath) else []
        except (OSError, ValueError) as e:  # noqa: BLE001
            sys.stderr.write(
                "/rewind：会话文件读不动（%s）——未改上下文\n" % str(e)[:60]
            )
            return None
        if rest in ("", "--list"):
            turns = [r for r in recs if r.get("kind") == "user" and r.get("text")]
            if not turns:
                sys.stderr.write(
                    "/rewind：本会话还没有轮次记录（%s）\n"
                    % os.path.basename(str(tpath))
                )
                return None
            for i, r in enumerate(turns, 1):
                sys.stderr.write("  第 %d 轮: %s\n" % (i, str(r.get("text"))[:60]))
            sys.stderr.write(
                "/rewind <N> 回到第 N 轮末（共 %d 轮；文件层不动，"
                "写操作回退用 /undo）\n" % len(turns)
            )
            return None
        try:
            n = int(rest)
        except ValueError:
            sys.stderr.write("/rewind：N 必须是整数（/rewind --list 看轮次）\n")
            return None
        msgs, err, turns_n = _rewind_replay(recs, n)
        if err:
            sys.stderr.write("/rewind 拒绝：%s —— 上下文未动\n" % err)
            return None
        ctx["history"] = msgs
        _transcript_append(
            tpath,
            {
                "ts": time.time(),
                "kind": "note",
                "text": "rewind → 第 %d/%d 轮"
                "（history 重建 %d 条）" % (n, turns_n, len(msgs)),
            },
        )
        sys.stderr.write(
            "已回退到第 %d/%d 轮：history 重建 %d 条"
            "（文件层未动；写操作回退用 /undo）\n" % (n, turns_n, len(msgs))
        )
        return None
    if cmd == "/skill":
        try:
            items = _skills_mod().discover(_skill_roots())
        except Exception as e:  # noqa: BLE001
            sys.stderr.write("/skill：技能目录读不动（%s）\n" % str(e)[:80])
            return None
        if not items:
            sys.stderr.write(
                "/skill：没有发现技能（~/.agentos/skills/*/SKILL.md"
                " 或 本仓 .agentos/skills/*/SKILL.md）\n"
            )
            return None
        if not rest:
            for it in items:
                sys.stderr.write(
                    "  %-24s %s\n" % (it["name"], str(it.get("description"))[:60])
                )
            sys.stderr.write("/skill <名> 注入正文（共 %d 个技能）\n" % len(items))
            return None
        it = next((x for x in items if x["name"] == rest), None)
        if it is None:
            sys.stderr.write("/skill：没有名为 %r 的技能（/skill 列表）\n" % rest[:60])
            return None
        body = str(it.get("body") or "").strip()[:20000]
        ctx.setdefault("history", []).append(
            {"role": "user", "content": "[skill %s]\n%s" % (it["name"], body)}
        )
        sys.stderr.write(
            "已注入技能 %s（%d 字符）到下一轮上下文\n" % (it["name"], len(body))
        )
        return None
    if cmd == "/cost":
        sys.stderr.write(
            "累计用量（端点自报，没报就是空）：%s\n"
            % json.dumps(ctx.get("usage_total") or {}, ensure_ascii=False)
        )
        return None
    if cmd == "/status":
        sys.stderr.write(_status_line(ctx, sid) + "\n")
        return None
    if cmd == "/model":
        if not rest:
            sys.stderr.write(
                "模型=%s · 端点=%s · 来源=%s\n"
                % (provider.model or "-", provider.base_url, provider.source)
            )
            return None
        ctx["model"] = rest
        sys.stderr.write("模型 → %s（下一轮生效）\n" % rest)
        # 换模型不等于换端点：protocol / session_id 必须原样带过去，
        # 否则刚从 responses 端点起跑的会话会被打回 chat/completions，
        # 网关当场 400 ModelProtocolUnsupported（本会话就踩过）。
        return Provider(
            {
                "base_url": provider.base_url,
                "model": rest,
                "headers": provider.headers,
                "protocol": provider.protocol,
                "session_id": provider.session_id,
                "provider_name": provider.provider_name,
                "source": provider.source,
            }
        )
    if cmd == "/diff":
        _repl_diff(ctx)
        return None
    if cmd == "/init":
        _repl_init(ctx)
        return None
    if cmd == "/sessions":
        files = _transcript_list()
        if not files:
            sys.stderr.write("(空) %s 下还没有 agent-*.jsonl 会话\n" % _session_dir())
        for p in files[-20:]:
            base = os.path.basename(p)
            mark = " ← 当前" if base == "%s.jsonl" % sid else ""
            sys.stderr.write("  %s%s\n" % (base[: -len(".jsonl")], mark))
        sys.stderr.write(
            "/branch [注记] 另开分支（当前会话不动）；"
            "/verify 验本会话关联计划的真完成\n"
        )
        return None
    if cmd == "/branch":
        note = rest.strip()
        nsrc = _transcript_path(sid)
        if not os.path.isfile(nsrc):
            sys.stderr.write("/branch：当前会话还没有落盘记录（至少聊一轮）\n")
            return None
        nsid = (
            "agent-%s" % time.strftime("%Y%m%d-%H%M%S") + "-%s" % uuid.uuid4().hex[:4]
        )
        try:
            bm = _branches_mod()
            ok_b, err_b = bm.fork_transcript(nsrc, _session_dir(), nsid, note)
        except ImportError:
            sys.stderr.write("/branch：branches 模块缺失\n")
            return None
        if not ok_b:
            sys.stderr.write("/branch 失败：%s\n" % err_b)
            return None
        sys.stderr.write(
            "已分支：%s（原会话 %s 不动；/sessions 回去，"
            "--resume %s 回来）\n" % (nsid, sid, nsid)
        )
        return None
    if cmd == "/verify":
        try:
            plan, err = _dsp().load_plan(sid)
        except ImportError:
            sys.stderr.write("/verify：DSP 模块缺失\n")
            return None
        if plan is None:
            sys.stderr.write("/verify：本会话无计划（%s）\n" % (err or "？"))
            return None
        try:
            rep = _branches_mod().verify_plan(plan)
        except ImportError:
            sys.stderr.write("/verify：branches 模块缺失\n")
            return None
        for v in rep["verdicts"]:
            sys.stderr.write(
                "  [%-16s] %s：%s\n" % (v["verdict"], v["id"], v["detail"])
            )
        sys.stderr.write(
            "真完成 %d/%d（其余未提前 done）\n" % (rep["n_done_true"], rep["n_total"])
        )
        _transcript_append(
            tpath,
            {
                "ts": time.time(),
                "kind": "note",
                "text": "/verify 真完成 %d/%d" % (rep["n_done_true"], rep["n_total"]),
            },
        )
        return None
    if cmd == "/steer":
        if not rest:
            try:
                _pl, _tot = _steer_mod().pending(_steer_dir(), sid)
                _pn = len(_pl)
            except Exception:  # noqa: BLE001
                _pn = 0
            sys.stderr.write(
                '/steer "指令"：给本会话投一条监督指令'
                "（下一步骤边界注入；只配送不裁决）；"
                "待消费 %d 条\n" % _pn
            )
            return None
        try:
            ok_s, err_s = _steer_mod().push(_steer_dir(), sid, rest)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write("/steer 失败：%s\n" % str(e)[:80])
            return None
        if ok_s:
            sys.stderr.write(
                "已投递老板指令：下一步骤边界注入本轮"
                "（跑着的 agent 会在一个步骤内看到）\n"
            )
        else:
            sys.stderr.write("/steer 失败：%s\n" % err_s)
        return None
    if cmd == "/watch":
        try:
            _wm = _watch_mod()
            sys.stdout.write(_wm.render(_wm.snapshot(ctx["root"], sid)) + "\n")
        except Exception as e:  # noqa: BLE001
            sys.stderr.write("/watch 失败：%s\n" % str(e)[:80])
        return None
    if cmd == "/mode":
        if rest in MODES:
            ctx["mode"] = rest
            sys.stderr.write("权限模式 → %s\n" % rest)
        else:
            sys.stderr.write("只支持：%s\n" % " ".join(MODES))
        return None
    sys.stderr.write("不认识的 REPL 命令：%s（/help）\n" % cmd)
    return None


def _repl(args):
    """交互式 REPL。上下文跨轮保留（history），权限模式可现场切。

    开箱即用：默认走 ~/.codex/config.toml 的端点；主端点连不通（403/400/
    不可达）时自动回退到本机 Ollama（若在跑），并在 stderr 如实打印回退。
    端点错误不再让 REPL 退出 —— 用户还在，换条路继续；只有 /exit 或 Ctrl-D
    才结束。默认开流式，让工具调用/增量可见（--no-stream 关掉）。

    会话落在 work/agent_sessions/agent-*.jsonl：每轮 user/assistant 各一条，
    退出时打印 session id；`agentos agent --continue` 或 `--resume <id>`
    接着聊。`!cmd` 走只读白名单（与工具层同一张表），`@path` 把文件内容
    内联进这一轮的需求。
    """
    stream = not getattr(args, "no_stream", False)
    try:
        provider = Provider(
            resolve_provider(args.env if hasattr(args, "env") else None)
        )
    except AgentError as e:
        if e.kind != "no-provider":
            sys.stderr.write("失败 [%s] %s\n" % (e.kind, e))
            return _rc_of_error(e.kind)
        cfg, blocked = _startup_local_backend()
        if not cfg:
            sys.stderr.write(
                "失败 [no-provider] %s%s\n"
                % (
                    e,
                    "（no_fallback=true：不换本地模型，请修好端点后重试）"
                    if blocked
                    else "",
                )
            )
            return _rc_of_error(e.kind)
        sys.stderr.write(
            "[回退] 没配端点，改用本机离线后端 %s\n" % (cfg.get("model") or "?")
        )
        provider = Provider(cfg)
    sid, resumed = _repl_session(args)
    ctx = {
        "root": args.root,
        "mode": args.mode,
        "approver": _make_approver(args),
        "max_steps": args.max_steps,
        "history": [],
        "usage_total": {},
    }
    _sf9 = _safety_flags(_read_agentos_config())
    ctx.update(_sf9)
    try:
        ctx["full_access"] = _full_access(_read_agentos_config())
    except Exception:
        ctx["full_access"] = False
    if ctx.get("full_access"):
        sys.stderr.write(
            "[权限] 破坏性命令放行（逐条留痕 work/destructive_audit.jsonl）\n"
        )
    _on9 = [
        n
        for n, k in (
            ("计划锁", "plan_lock_off"),
            ("权限模式", "mode_gate_off"),
            ("命令白名单", "cmd_gate_off"),
        )
        if not _sf9[k]
    ]
    if _on9:
        sys.stderr.write(
            "[安全闸] 上锁：%s（~/.agentos/config.toml [agent] "
            "字面 true 才上锁；硬阻断清单默认不可关（full_access 除外））\n"
            % "、".join(_on9)
        )
    else:
        sys.stderr.write(
            "[安全闸] 自由面：计划锁/权限/白名单均关"
            "（硬阻断清单始终在）\n"
        )
    if resumed:
        ctx["history"] = resumed["history"]
        sys.stderr.write(
            "[续跑] 会话 %s：回灌 %d 条上下文\n"
            % (resumed["id"], len(resumed["history"]))
        )
    tpath = _transcript_path(sid)
    ctx["sid"] = sid
    _repl_welcome(provider, ctx, sid, resumed)
    _REPL_ROOT[0] = ctx["root"]
    _setup_readline()
    _evo_refresh_tool_hints()
    _mcp_sync()  # 挂 MCP 服务器工具（配置里有 [mcp] 才动）
    _hooks_session(ctx)  # hooks SessionStart（有 hooks.json 才动）
    while True:
        line = _readline("> ")
        if line is None:
            sys.stderr.write("\n会话 %s（%s）\n" % (sid, tpath))
            return 0
        text = line.strip()
        if not text:
            continue
        # 多行输入（Claude Code 手感）：行尾奇数个反斜杠 = 续行，继续用
        # 「… 」次级提示符收行，逐段拼到不含续行标记为止。
        while _line_wants_continue(text):
            nxt = _readline("… ")
            if nxt is None:
                break
            text = _compose_join(text, nxt)
            if not text.strip():
                break
        if not text:
            continue
        if text.startswith("/"):
            rc = _repl_slash(text, ctx, provider, sid, tpath)
            if rc == "exit":
                sys.stderr.write("会话 %s（%s）\n" % (sid, tpath))
                return 0
            if isinstance(rc, Provider):
                provider = rc
            continue
        if text.startswith("!"):
            _repl_bang(text[1:], ctx)
            continue
        prompt, refs = _expand_at_refs(text, ctx["root"])
        if refs:
            sys.stderr.write("[引用] %s\n" % ", ".join(refs))
        # [老板已批准]：批准词 → 硬指令 + 未审批计划按口谕自动批准（上链）
        # + 若本轮模型零工具调用则自动重试一轮（2026-10-14 空转事故）。
        _approved_turn = _looks_approved(text)
        if _approved_turn:
            prompt = _APPR_DIRECTIVE + text + "\n" + prompt
            try:
                _sidA = str(ctx.get("sid") or "")
                if _sidA:
                    _pA, _eA = _dsp().load_plan(_sidA)
                    if _pA is not None and not _pA.get("approved"):
                        _okA, _erA = _dsp().approve_plan(_sidA)
                        if _okA:
                            sys.stderr.write(
                                "[批准] 检测到老板批准用语："
                                "计划按口谕自动批准（已上哈希链）"
                                "—本轮直接开工\n"
                            )
            except Exception:  # noqa: BLE001
                pass
        t_turn = time.time()
        seen = {"delta": False}
        # 过程可见性：只在交互 TTY 起转轮线程；headless/管道零改动。
        sp = _TurnSpinner(sys.stderr.isatty(), _c).start()
        interrupt = threading.Event()
        ew = _EscWatcher(sys.stdin.isatty(), interrupt).start()
        try:
            res, provider = run_agent_with_fallback(
                prompt,
                ctx=ctx,
                provider=provider,
                stream=stream,
                on_event=_turn_ui(sp, _event_printer(stream, seen), seen),
                cancel=interrupt.is_set,
                allow_fallback=not _no_fallback(),
                retry_main=_no_fallback(),
            )
            # 批准轮零工具调用 = 没开工 → 自动重试一轮（有界一次）
            if _needs_approve_retry(
                _approved_turn, res.get("tools_used"), interrupt.is_set()
            ):
                sys.stderr.write("[批准] 本轮零工具调用（没真开工）——自动重试一轮\n")
                res, provider = run_agent_with_fallback(
                    _APPR_RETRY,
                    ctx=ctx,
                    provider=provider,
                    stream=stream,
                    on_event=_turn_ui(sp, _event_printer(stream, seen), seen),
                    cancel=interrupt.is_set,
                    allow_fallback=not _no_fallback(),
                    retry_main=_no_fallback(),
                )
            # 空答（端点抖动的典型形态）→ 自动重试一轮（有界一次）
            if res.get("empty_answer") and not interrupt.is_set():
                sys.stderr.write("[重试] 本轮模型空答——自动重试一轮\n")
                res, provider = run_agent_with_fallback(
                    "（上一轮你的回复为空）请重新给出实质回答；若任务需要"
                    "读文件/跑命令，现在就调用工具。",
                    ctx=ctx,
                    provider=provider,
                    stream=stream,
                    on_event=_turn_ui(sp, _event_printer(stream, seen), seen),
                    cancel=interrupt.is_set,
                    allow_fallback=not _no_fallback(),
                    retry_main=_no_fallback(),
                )
        except AgentError as e:
            if e.kind == "interrupted":
                # 用户按 Esc：不进失败规则，但如实入账（自评的体验信号）
                _evo_signal(
                    ctx.get("mode", ""),
                    False,
                    "interrupted",
                    round(time.time() - t_turn, 2),
                    [],
                )
                sys.stderr.write("\n已打断（Esc）：本轮终止，上下文保留\n")
                continue
            _evo_signal(
                ctx.get("mode", ""), False, e.kind, round(time.time() - t_turn, 2), []
            )
            sys.stderr.write("失败 [%s] %s（上下文保留，可继续输入）\n" % (e.kind, e))
            continue
        except KeyboardInterrupt:
            sys.stderr.write("\n已中止当前任务（上下文保留）\n")
            continue
        finally:
            sp.stop()
            ew.stop()
        fb = ctx.pop("last_fallback", None)
        if fb:
            _evo_signal(
                ctx.get("mode", ""),
                False,
                fb.get("kind", "provider"),
                round(time.time() - t_turn, 2),
                [],
                note="fallback-masked",
            )
        _empty_ans = bool(res.get("empty_answer"))
        if _empty_ans:
            sys.stderr.write(
                "（本轮模型返回空内容：多为端点抖动或模型"
                "卡壳——可重试，或用 /model 换一个端点）\n"
            )
        _evo_signal(
            ctx.get("mode", ""),
            (not _empty_ans),
            ("empty-answer" if _empty_ans else ""),
            res.get("elapsed")
            if res.get("elapsed") is not None
            else round(time.time() - t_turn, 2),
            [u.get("tool") for u in res.get("tools_used") or []],
            tools_fail=[
                u.get("tool")
                for u in res.get("tools_used") or []
                if u.get("executed") and not u.get("ok")
            ],
            denied=[
                u.get("tool")
                for u in res.get("tools_used") or []
                if not u.get("executed")
            ],
            denied_reasons=[
                "%s：%s" % (u.get("tool"), u.get("error"))
                for u in res.get("tools_used") or []
                if not u.get("executed")
            ],
            # 收敛快照：run_agent 把快照挂在结果里（REPL 与 headless 通用，
            # 不用在这里重建引擎——REPL 侧没有 _conv 这个局部量）。
            converge=(res.get("converge") or {}),
        )
        # W13 诚实收尾：零证据完成声明打脸 + 跨轮复读检测（信号入账）
        _n_tools_turn = len(res.get("tools_used") or [])
        _ans0 = (res.get("answer") or "").strip()
        _claim_bad = _claim_no_evidence(_ans0, _n_tools_turn)
        _prev_ans = getattr(_repl, "_prev_ans", "")
        _stale = _is_stale_reply(_prev_ans, _ans0)
        if _claim_bad:
            sys.stderr.write(
                "（注：本轮 0 次工具调用，上面「已落实/已部署」"
                "类声明未附任何 rc/命令证据——本系统铁律：无证据"
                "的「完成」不算完成）\n"
            )
            try:
                _evo_signal(
                    ctx.get("mode", ""),
                    False,
                    "no-evidence-claim",
                    round(time.time() - t_turn, 2),
                    [],
                )
            except Exception:  # noqa: BLE001
                pass
        if _stale:
            sys.stderr.write(
                "（注：本轮终答与上一轮近乎逐字相同（模型"
                "复读）——可换端点：agentos config set llm.model "
                "<其他模型>）\n"
            )
            try:
                _evo_signal(
                    ctx.get("mode", ""),
                    False,
                    "stale-reply",
                    round(time.time() - t_turn, 2),
                    [],
                )
            except Exception:  # noqa: BLE001
                pass
        if _ans0:
            _repl._prev_ans = _ans0[:2000]
        ctx["usage_total"] = _usage_total(ctx.get("usage_total"), res.get("usage"))
        u = res.get("usage") or {}
        tot = ctx.get("usage_total") or {}
        _tu = res.get("tools_used") or []
        _tx = sum(1 for _x in _tu if _x.get("executed"))
        # ctx% 只在用户配置了 llm.context_window 才显示（不知道窗口大小就
        # 不装知道——CC 显示 context left 是因为它知道自己家的窗口）。
        _cw = 0
        try:
            _cw = int(
                (_read_agentos_config().get("llm") or {}).get("context_window") or 0
            )
        except Exception:  # noqa: BLE001
            _cw = 0
        _ctxseg = ""
        if _cw > 0 and tot.get("prompt_tokens"):
            _ctxseg = " · ctx %.0f%%" % (100.0 * tot["prompt_tokens"] / _cw)
        # 累计段：只有「累计 ≠ 本轮」时才出现（第一轮显示两遍同样的数是噪音）
        _totseg = ""
        if (
            tot.get("prompt_tokens") is not None
            and tot.get("prompt_tokens") != u.get("prompt_tokens")
        ):
            _totseg = " · 累计 ↑%s ↓%s" % (
                tot.get("prompt_tokens"),
                tot.get("completion_tokens")
                if tot.get("completion_tokens") is not None
                else "-",
            )
        _totseg += _ctxseg
        sys.stderr.write(
            _c(
                "dim",
                "  %d 步 %.1fs · 工具 %d/%d · 本轮 ↑%s ↓%s tok%s\n"
                % (
                    res.get("steps") or 0,
                    res.get("elapsed") or 0.0,
                    _tx,          # 拿到回执的工具数
                    len(_tu),     # 调用的工具总数
                    u.get("prompt_tokens")
                    if u.get("prompt_tokens") is not None
                    else "-",
                    u.get("completion_tokens")
                    if u.get("completion_tokens") is not None
                    else "-",
                    # 累计只在与本轮不同时出现（第一轮重复显示是噪音）；
                    # ctx% 只在配了 context_window 时才有。
                    _totseg,
                ),
            )
        )
        try:
            for r in _evo_midflight():
                sys.stderr.write(
                    _c(
                        "dim",
                        "[evo] 新规则候选 %s ×%s（/evo 查看，"
                        "/evo-apply 晋升）\n" % (r.get("id"), r.get("count")),
                    )
                )
        except Exception:  # noqa: BLE001
            pass
        _transcript_append(tpath, {"ts": time.time(), "kind": "user", "text": text})
        ans = (res.get("answer") or "").strip()
        _transcript_append(tpath, {"ts": time.time(), "kind": "assistant", "text": ans})
        if res.get("kind") == "limit":
            sys.stderr.write("预算用尽：%s\n" % res.get("error"))
        elif res.get("kind") == "gate":
            sys.stderr.write(
                "交付闸未通过（本轮不算完成）：%s\n" % (res.get("error") or "")
            )
        if stream and seen.get("delta"):
            sys.stdout.write("\n")  # 增量已逐字打过，别把终答再抄一遍
        elif not stream:
            sys.stdout.write(_c("brand", "⏺ ") + "%s\n\n" % _format_answer(ans))
        else:
            # 流式开着但一条增量都没来：⏺ 由这里补上再出终答
            sys.stdout.write(_c("brand", "⏺ ") + "%s\n\n" % _format_answer(ans))
        denied = [u for u in res.get("tools_used") or [] if not u.get("executed")]
        if denied:
            sys.stderr.write(
                "本轮 %d 次工具未执行（被拒/参数不对）：%s\n"
                % (len(denied), ", ".join(u["tool"] for u in denied))
            )


def _selftest():
    """自检：本地桩端点 + work/ 下的真实小文件，不碰真模型、不碰真网。

    每条用例的布尔都由**真实观察**判定：返回路径里的 kind、磁盘上文件在
    不在、sha256 变没变。有 FAIL 就 rc=1 —— 这里糊一次，上层全绿都是假的。
    """
    cases = []

    def case(name, ok, detail=""):
        cases.append([name, bool(ok), str(detail)])

    def jc(tool=None, content="", cid="call_1"):
        """一条 chat/completions 响应；tool=(工具名, 参数dict)。"""
        m = {"role": "assistant", "content": content}
        fin = "stop"
        if tool is not None:
            fn, args = tool
            m["tool_calls"] = [
                {
                    "id": cid,
                    "type": "function",
                    "function": {
                        "name": fn,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                }
            ]
            fin = "tool_calls"
        return {"json": {"choices": [{"index": 0, "message": m, "finish_reason": fin}]}}

    # 目录名带 pid：固定在用一个路径时，两个自检并发跑会互相看见对方留下
    # 的 py_ok.txt / auto.txt，把「文件没落地」这类判据莫名判红（2026-10-11
    # 实测被并发门打出一条 FAIL，单独重跑就绿 —— 典型的假红）。
    tmp = os.path.join(ROOT, "work", "_st_agent_core.%d" % os.getpid())
    # 先清「上轮崩溃留下的孤儿」：进程被杀（超时/被中断）时 finally 不会跑，
    # 目录就留在 work/ 里，零污染判据当场抓红。
    # 只清 **pid 已不在** 的目录：并发自检各自有目录（同名会互相看见对方
    # 的产物），活着的进程那份不能动。
    try:
        _wdir = os.path.join(ROOT, "work")
        for _e in os.listdir(_wdir):
            if not _e.startswith("_st_agent_core."):
                continue
            try:
                _opid = int(_e.rsplit(".", 1)[1])
            except (ValueError, IndexError):
                continue
            if _opid == os.getpid():
                continue
            try:
                os.kill(_opid, 0)
            except OSError:
                shutil.rmtree(os.path.join(_wdir, _e), ignore_errors=True)
    except OSError:
        pass
    try:
        _selftest_body(case, jc, tmp)
    finally:
        # rmtree 而不是 unlink+rmdir：自检正文可能建嵌套目录（如 ideas 例的
        # repo/tools/），旧清法只清得动平文件，嵌套目录一留就是 work/ 下的
        # _st_agent_core.* 残留（agent_core_gate 的 stray 判据当场抓红，
        # 2026-10-11 实测）。
        shutil.rmtree(tmp, ignore_errors=True)
    bad = [c for c in cases if not c[1]]
    for name, ok, detail in cases:
        sys.stdout.write(
            "%s %s%s\n"
            % ("PASS" if ok else "FAIL", name, ("  -- " + detail) if detail else "")
        )
    if bad:
        sys.stdout.write(
            "AGENT_CORE FAIL %d/%d\n" % (len(cases) - len(bad), len(cases))
        )
        return 1
    sys.stdout.write("AGENT_CORE OK %d/%d\n" % (len(cases), len(cases)))
    return 0


def _clean_plans(*sids):
    """清掉自检用例的 DSP 计划残留。

    自检里带 sid 的用例可能触发 DSP 自启（建计划落 work/plans/<sid>-*），
    用完必须清 —— dsp_gate 的 D12「work/plans 无自检残留」会当场抓红
    （2026-10-21 实测踩到：不是产品有残留，是自检自己留下的）。
    """
    for _sid in sids:
        for _suf in ("-plan.json", "-chain.jsonl"):
            try:
                os.unlink(
                    os.path.join(ROOT, "work", "plans", _sid + _suf)
                )
            except OSError:
                pass


def _selftest_body(case, jc, tmp):
    """自检正文（2026-10-21 搬到 tools/agent_core_selftest.py）。

    为什么搬：主文件 451KB > read_file 上限 256KB，agent 读不了自己的
    主文件。这一块 143KB 且只被 _selftest 调用，整块搬走、行为一字不变。
    懒导入：本模块在 --check 之外不该为自检付加载成本，也避免循环导入。
    """
    # 懒导入的自检模块要按**本文件所在目录**找，不能钉死 ROOT/tools：
    # 负向门把 agent_core.py 拷成变异副本再跑 --check（tools/neg_agent_core.py），
    # 钉死 ROOT/tools 就会导入**原版**自检 → 变异咬不到 → 门假绿。
    # 用 append 而不是 insert(0)：sys.path[0] 是脚本自身目录，副本优先用自己的。
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.append(here)
    import agent_core_selftest

    return agent_core_selftest._selftest_body(case, jc, tmp)

def main(argv=None):
    args = _build_parser().parse_args(argv)
    if args.provider:
        # --provider 落进 env：resolve_provider 的每个调用点
        # （REPL / headless / run_agent / 自检）都能看见，不必逐处穿参。
        os.environ["AGENTOS_PROVIDER"] = args.provider
    if args.check:
        return _selftest()
    try:
        if args.prompt or args.print_mode:
            return _run_once(args)
        return _repl(args)
    except AgentError as e:
        sys.stderr.write("agent_core 失败 [%s] %s\n" % (e.kind, e))
        return _rc_of_error(e.kind)
    except KeyboardInterrupt:
        sys.stderr.write("\n已中止\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
