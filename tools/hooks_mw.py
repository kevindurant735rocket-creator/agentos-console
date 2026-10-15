#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hooks_mw —— hooks 中间件：PreToolUse / PostToolUse / SessionStart。

契约形状**零自创**：与 ~/.codex/hooks.json 同一形状（headroom_gate 已验证过
该形状能被外部工具读写，用户心智模型只有一份）：
    {"hooks": {"PreToolUse": [{"matcher": "write_*",
                               "hooks": [{"command": "..."}]}],
               "PostToolUse": [...], "SessionStart": [...]}}
matcher = fnmatch glob，作用于工具名（Pre/Post）；缺省 "*"。钩子进程从 stdin
收 JSON payload，stdout 可回 JSON {"additionalContext": "..."}。

语义（fail-closed 只立在 PreToolUse 上）：
  * exit 2                         → 拦截（reason = stdout）
  * 崩溃 / 超时 / 其他非 0 退出码   → PreToolUse 拦截（fail-closed）；
                                     PostToolUse / SessionStart 只记 errors
  * exit 0 + stdout JSON           → 收集 additionalContext
  * 无配置 = 全关（零开销零影响）
裁决：本模块不做自证；hooks_gate 拿夹具钩子证伪它。
"""
import fnmatch
import json
import subprocess

HOOK_EVENTS = ("PreToolUse", "PostToolUse", "SessionStart")


def load_cfg(path):
    """读 hooks.json；不存在/解析失败/形状不对 → None（= 全关，不是空配置）。"""
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict) or not isinstance(cfg.get("hooks"), dict):
        return None
    return cfg


def match_commands(cfg, event, tool=None):
    """该事件 + 工具名下要跑的 command 列表（按配置顺序；glob 不匹配即跳）。"""
    out = []
    for group in (cfg.get("hooks") or {}).get(event) or []:
        if not isinstance(group, dict):
            continue
        matcher = str(group.get("matcher") or "*")
        if tool is not None and not fnmatch.fnmatchcase(str(tool), matcher):
            continue
        for h in group.get("hooks") or []:
            if isinstance(h, dict) and h.get("command"):
                out.append(str(h["command"]))
    return out


def run_event(event, payload, cfg, timeout=5.0):
    """跑一个事件的全部钩子。返回 {"block","reason","context","errors","ran"}。

    PreToolUse 的失败语义 = fail-closed（拦截）；PostToolUse / SessionStart
    的失败只入 errors（已经发生的事不许被中间件改写成"没发生"）。
    """
    tool = (payload or {}).get("tool") \
        if event in ("PreToolUse", "PostToolUse") else None
    res = {"block": False, "reason": "", "context": [], "errors": [],
           "ran": 0}
    for cmd in match_commands(cfg, event, tool=tool):
        res["ran"] += 1
        try:
            p = subprocess.run(["/bin/sh", "-c", cmd],
                               input=json.dumps(payload or {},
                                                ensure_ascii=False),
                               capture_output=True, text=True,
                               timeout=timeout)
        except (subprocess.TimeoutExpired, OSError) as e:
            if event == "PreToolUse":
                res["block"] = True
                res["reason"] = ("钩子崩溃/超时（fail-closed）：%s"
                                 % str(e)[:120])
                return res
            res["errors"].append("%s 钩子异常（仅记）：%s"
                                 % (event, str(e)[:100]))
            continue
        out = (p.stdout or "").strip()
        if p.returncode == 2:
            if event == "PreToolUse":
                res["block"] = True
                res["reason"] = out or "PreToolUse 钩子以 exit 2 拦截"
                return res
            res["errors"].append("%s 钩子 exit 2（仅记）：%s"
                                 % (event, out[:100]))
            continue
        if p.returncode != 0:
            if event == "PreToolUse":
                res["block"] = True
                res["reason"] = ("PreToolUse 钩子退出码 %d（fail-closed）：%s"
                                 % (p.returncode, out[:120]))
                return res
            res["errors"].append("%s 钩子退出码 %d（仅记）：%s"
                                 % (event, p.returncode, out[:100]))
            continue
        if out.startswith("{"):
            try:
                o = json.loads(out)
            except ValueError:
                o = None
            if isinstance(o, dict):
                ac = o.get("additionalContext")
                if isinstance(ac, str) and ac.strip():
                    res["context"].append(ac.strip())
    return res
