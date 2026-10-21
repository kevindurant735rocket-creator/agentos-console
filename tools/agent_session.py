# -*- coding: utf-8 -*-
"""会话侧内核：自进化信号/rules、记忆层、IDEAS 扫描、会话 transcript、上下文压缩、/rewind 重放、@ 引用展开、状态行。

从 agent_core.py 整块搬出（2026-10-21，TASK2 障碍 2：主文件超 read_file
上限 256KB，agent 读不了自己的主文件）。函数体逐字复制，逻辑未改一行。
依赖方向：本模块 → agent_core（单向，段内裸名从内核导入）。
"""

import json
import os
import re
import sys
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid

import agent_provider

ROOT = agent_provider.ROOT


def _kernel():
    """取内核模块（agent_core；`python3 tools/agent_core.py` 时它是 __main__）。

    2026-10-21（TASK2 障碍 2）：本模块刻意**不**在顶层 import agent_core ——
    内核以 __main__ 身份跑时，顶层 import 会把 agent_core.py 再执行一遍，
    在 sink 尚未定义处撞成循环导入。所以按需取。
    """
    for _n in ("agent_core", "__main__"):
        _m = sys.modules.get(_n)
        _f = getattr(_m, "__file__", "") or ""
        if _f and os.path.basename(_f) == "agent_core.py":
            return _m
    import agent_core as _m
    return _m


def _tools():
    """内核的工具表（TOOLS 在内核里是可变 dict，现取现用）。"""
    return getattr(_kernel(), "TOOLS", None) or {}

SESSION_DIR = os.path.join(ROOT, "work", "agent_sessions")
TRANSCRIPT_PREFIX = "agent-"
COMPACT_KEEP = 20  # /compact 与自动裁剪保留的最近消息条数

# ── 自进化（R2）：信号 → 规则 → 注入 → 可回退 ────────────────────────
# 设计红线：这一层是**增益层**，任何一步坏掉都不许打断对话主循环（fail-open），
# 但账坏了必须如实说（/evo 报 err、不假装有规则）；账好时全部现取现算。
EVO_SIGNALS_PATH = os.path.join(SESSION_DIR, "signals.jsonl")
EVO_RULES_PATH = os.path.join(ROOT, "work", "agent_evo_rules.json")
EVO_MIN_COUNT = 3  # 同签名失败达到几条才聚成规则（R2 判据=3）

_EVO_ADVICE = {
    "no-provider": "配一个端点再聊：agentos config set llm.api_key <key>，"
    "或 AGENTOS_LLM_LOCAL=ollama 走本机离线后端",
    "provider": "端点失败：先 GET /v1/models 探活；HTTP 403/400 多为 key 或"
    "网关路由问题，不可达为网络/代理问题；/model 可现场换模型",
    "usage": "参数口径不对：先 /help 看命令形状",
}
_EVO_ADVICE_GENERIC = (
    "查 stderr 里 [失败 kind] 的原文；同类失败连续出现会在此聚成经验规则"
)
_EVO_ADVICE.update(
    {
        "tool-fail:read_file": "read_file 失败多为路径不存在或越界：先 list_dir "
        "确认结构再读，路径以项目根为基准",
        "tool-fail:edit_file": "edit_file 失败多为 old_string 不唯一或未找到："
        "先 read_file 取精确原文再改",
        "tool-fail:write_file": "write_file 失败：核对相对路径与权限模式",
        "tool-fail:run_cmd": "run_cmd 失败：命令是否在只读白名单内，长输出先缩小范围",
        "tool-fail:gh_search": "gh_search 失败：缩小 query 或检查 gh 认证",
        # 收敛类签名：来自引擎自己的打转统计（不是模型报错，是行为形状）
        "converge:stall": "本机历史：连续多步没有产出（只读/只查不动手）。"
        "先动手改一处再继续，别把时间花在读完再读上",
        "converge:loop": "本机历史：同一调用方式重复多次无进展。换个工具、"
        "换文件、换思路，别在同一条路上打转",
        "converge:explore-only": "本机历史：只探查没落地。探查够信息就动手，"
        "本轮必须有写盘或改动",
        "converge:no-verify": "本机历史：改了但没核对。改完必须读回或跑一条"
        "命令拿到真实 rc，再交付",
        "converge:gate-reject": "本机历史：终答缺证据被交付闸退回过。"
        "终答必须附真实 rc / 文件行号，别只说「已完成」",
        # 契约类签名：按任务类型统计的失败形状
        "contract:bugfix-blind": "本机历史：缺陷修复类任务多次是**盲改**"
        "（动手改了却从没看到原 bug 失败的样子）。先跑一次能触发该 bug 的"
        "命令看到失败，再改；改完再跑一次看到通过。",
        "contract:bugfix": "本机历史：缺陷修复类任务在这里出过错。"
        "按「先复现 → 再改 → 再验」的顺序走，别直接改。",
        "contract:docs": "本机历史：文档类任务出过「写了但没读回」的错。"
        "写完读回一遍确认落盘且内容对。",
        "contract:ops": "本机历史：操作/部署类任务出过「做完没确认状态」的错。"
        "做完后跑一次确认命令把状态打出来。",
        "contract:analysis": "本机历史：分析类任务出过「凭印象报数」的错。"
        "结论必须来自跑出来的命令 + rc。",
        "contract:research": "本机历史：调研类任务出过「结论没来源」的错。"
        "每条结论都要有能核对的来源（路径:行号 / URL）。",
    }
)

# 收敛类签名 → 该拧哪个旋钮（自进化真的改行为，不只是加提示）
# 旋钮方向是「更早介入」：本机反复打转 = 现在的阈值太宽。
# 每条只拧一个旋钮，且只允许收紧（apply_policy 里钳制）。
_EVO_CONVERGE_TUNE = {
    "converge:stall": {"op": "converge_tune", "max_stall": 4},
    "converge:loop": {"op": "converge_tune", "loop_repeat": 2},
    "converge:explore-only": {"op": "converge_tune", "max_explore": 4},
}


def _evo_signal(
    mode,
    ok,
    err_kind,
    elapsed,
    tools,
    note="",
    tools_fail=None,
    denied=None,
    denied_reasons=None,
    converge=None,
):
    """每轮一条信号进 signals.jsonl（挖掘的原料）。落盘失败静默——
    进化是增益层，绝不允许它打断对话主循环。被兜底掩盖的主端点失败也记
    （ok=False + note=fallback-masked）：对挖掘而言它是真失败事件。
    v2：tools_fail（执行了但失败的工具）/denied（被拒未执行的工具）——
    框架层不足信号，挖掘器按工具聚签名。"""
    row = {
        "ts": round(time.time(), 3),
        "kind": "turn",
        "mode": mode,
        "ok": bool(ok),
        "err_kind": err_kind or "",
        "elapsed": elapsed,
        "tools": list(tools or []),
        "note": note,
        "tools_fail": list(tools_fail or []),
        "denied": list(denied or []),
        "denied_reasons": list(denied_reasons or []),
        # 收敛干预痕迹：注入了几次方向指令、交付闸退回了几次。
        # 这些是「agent 差点原地打转」的**量化证据**——挖掘器据此生成
        # 「该收敛」类规则，让进化层能自动调阈值/改提示，而不是只靠人拍。
        "converge": dict(converge or {}),
    }
    try:
        os.makedirs(os.path.dirname(EVO_SIGNALS_PATH), exist_ok=True)
        with open(EVO_SIGNALS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass


def _evo_rules_load(rules_path=None):
    """读规则账。返回 (rules, err)：err 非空 = 账坏（fail-open 的诚实版：
    调用方拿空规则继续跑，但 /evo 必须把 err 说出口）。"""
    p = rules_path or EVO_RULES_PATH
    if not os.path.exists(p):
        return [], ""
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except (ValueError, OSError) as e:
        return [], str(e)
    if not isinstance(d, dict) or not isinstance(d.get("rules"), list):
        return [], "规则账形状不对（缺 rules 数组）"
    return d["rules"], ""


def _evo_rules_save(rules, rules_path=None):
    p = rules_path or EVO_RULES_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(
            {"version": 1, "rules": rules},
            f,
            ensure_ascii=False,
            indent=1,
            sort_keys=True,
        )


def _evo_mine(rules_path=None, signals_path=None, min_count=None):
    """信号 → 规则（幂等）。同签名失败 ≥ min_count 条聚成 CANDIDATE；
    count 一律按信号全量重算（append-only 语义）。账坏时不动账（防挖一半
    把坏账洗成假新账），如实把 err 带回去。"""
    min_count = EVO_MIN_COUNT if min_count is None else min_count
    rp = rules_path or EVO_RULES_PATH
    sp = signals_path or EVO_SIGNALS_PATH
    rules, err = _evo_rules_load(rp)
    if err:
        return [], err
    counts = {}
    try:
        with open(sp, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    s = json.loads(ln)
                except ValueError:
                    continue
                if (
                    s.get("ok") is False
                    and s.get("err_kind")
                    and s.get("err_kind") != "interrupted"
                ):
                    counts["err:%s" % s["err_kind"]] = (
                        counts.get("err:%s" % s["err_kind"], 0) + 1
                    )
                for t in s.get("tools_fail") or []:
                    k = "tool-fail:%s" % t
                    counts[k] = counts.get(k, 0) + 1
                for t in s.get("denied") or []:
                    k = "denied:%s" % t
                    counts[k] = counts.get(k, 0) + 1
                # 收敛打转统计 → 签名。这是**行为形状**的失败（不是工具报错），
                # 只有引擎自己数得出来；数出来才能自动拧阈值。
                _cv = s.get("converge") or {}
                if isinstance(_cv, dict):
                    if _cv.get("stall_hits"):
                        counts["converge:stall"] = (
                            counts.get("converge:stall", 0) + 1
                        )
                    if _cv.get("loop_hits"):
                        counts["converge:loop"] = (
                            counts.get("converge:loop", 0) + 1
                        )
                    if (
                        _cv.get("phase") == "explore"
                        and _cv.get("steps")
                        and not _cv.get("write_seen")
                    ):
                        counts["converge:explore-only"] = (
                            counts.get("converge:explore-only", 0) + 1
                        )
                    if _cv.get("write_seen") and not _cv.get("verify_seen"):
                        counts["converge:no-verify"] = (
                            counts.get("converge:no-verify", 0) + 1
                        )
                    if _cv.get("gate_retries"):
                        counts["converge:gate-reject"] = (
                            counts.get("converge:gate-reject", 0) + 1
                        )
                    # 契约类：按任务类型统计失败形状。
                    # 最有价值的一条是 bugfix-blind：动手改了却从没见��
                    # 原 bug 失败的样子 —— 这正是「盲改」，改对改错都说不清。
                    _ctype = _cv.get("contract_type") or ""
                    if _ctype:
                        counts["contract:%s" % _ctype] = (
                            counts.get("contract:%s" % _ctype, 0) + 1
                        )
                    if (
                        _ctype == "bugfix"
                        and _cv.get("write_seen")
                        and not _cv.get("repro_fail_seen")
                    ):
                        counts["contract:bugfix-blind"] = (
                            counts.get("contract:bugfix-blind", 0) + 1
                        )
    except OSError:
        counts = {}
    by_sig = {r.get("sig"): r for r in rules}
    changed = False
    for sig in sorted(counts):
        n = counts[sig]
        if n < min_count:
            continue
        kind = sig.split(":", 1)[1] if ":" in sig else sig
        r = by_sig.get(sig)
        if r is None:
            rules.append(
                {
                    "id": "evo-%s" % sig.replace(":", "-"),
                    "sig": sig,
                    "count": n,
                    "advice": _EVO_ADVICE.get(
                        sig, _EVO_ADVICE.get(kind, _EVO_ADVICE_GENERIC)
                    ),
                    "state": "CANDIDATE",
                    "created": time.strftime("%F %T"),
                }
            )
            if sig in _EVO_CONVERGE_TUNE:
                rules[-1]["action"] = dict(_EVO_CONVERGE_TUNE[sig])
            changed = True
        elif r.get("count") != n:
            r["count"] = n
            changed = True
    if changed:
        _evo_rules_save(rules, rp)
    return rules, ""


def _evo_set_state(rid, state, rules_path=None):
    """晋升/回退一条规则。返回是否命中。"""
    rules, err = _evo_rules_load(rules_path)
    if err or not rid:
        return False
    hit = False
    for r in rules:
        if r.get("id") == rid:
            r["state"] = state
            hit = True
    if hit:
        _evo_rules_save(rules, rules_path)
    return hit


# ── 自进化 v2：框架级自提升（运行时工具提示 + 提案 + midflight）────────
# 诚实边界：规则只改「运行时工具描述」与「system prompt」，绝不自改磁盘
# 代码与门判据；真正的框架级改动走 work/evo_proposals/ 提案人审。
_TOOL_DESC_ORIG = {}


def _evo_refresh_tool_hints(rules_path=None):
    """tool 类 ACTIVE 规则 → 运行时改写该工具的 description（带
    「自进化提示」标记，/tools 可见）；/evo-off 后还原原文。返回生效数。"""
    if not _tools():
        return 0
    for name, spec in _tools().items():
        if name not in _TOOL_DESC_ORIG:
            _TOOL_DESC_ORIG[name] = spec.get("description") or ""
    rules, _ = _evo_rules_load(rules_path)
    hints = {}
    for r in rules:
        if (
            r.get("state") == "ACTIVE"
            and str(r.get("sig") or "").startswith(("tool-fail:", "denied:"))
            and r.get("advice")
        ):
            hints[r["sig"].split(":", 1)[1]] = r["advice"]
    for name, spec in _tools().items():
        orig = _TOOL_DESC_ORIG.get(name) or ""
        if name in hints:
            spec["description"] = "%s（自进化提示：%s）" % (orig, hints[name])
        else:
            spec["description"] = orig
    return len(hints)


EVO_PROPOSAL_KINDS = ("tool-hint", "memory", "gate-proposal")


def validate_proposal(d):
    """intake 提案 JSON schema 校验（纯面）。返回 (ok, error)。"""
    if not isinstance(d, dict):
        return False, "提案必须是 JSON object"
    pid = d.get("id")
    if (
        not isinstance(pid, str)
        or not pid.strip()
        or "/" in pid
        or "\\" in pid
        or pid.startswith(".")
    ):
        return False, "缺合法 id（仓内相对名，不许路径穿越）"
    if not isinstance(d.get("sig"), str) or not d.get("sig").strip():
        return False, "缺 sig（失败签名）"
    if not isinstance(d.get("count"), int) or d.get("count") < 1:
        return False, "缺 count（正整数）"
    if d.get("kind") not in EVO_PROPOSAL_KINDS:
        return False, "kind 须为 %s" % "/".join(EVO_PROPOSAL_KINDS)
    if not isinstance(d.get("advice"), str) or not d.get("advice").strip():
        return False, "缺 advice（非空字符串）"
    if d.get("status") not in ("pending-human", "accepted", "rejected"):
        return False, "status 非法"
    return True, ""


def _evo_propose(rules, rules_path=None):
    """失败达 2 倍阈值的签名 → 框架级提升提案文件（人审入口）。
    .md 人读 + 同名 .json 机器 intake（validate_proposal 可验）。
    返回新生成提案数。"""
    rp = rules_path or EVO_RULES_PATH
    pdir = os.path.join(os.path.dirname(rp), "evo_proposals")
    made = 0
    for r in rules:
        n = r.get("count") or 0
        if n < EVO_MIN_COUNT * 2 or r.get("proposed"):
            continue
        os.makedirs(pdir, exist_ok=True)
        pid = r.get("id") or "rule"
        sig = str(r.get("sig") or "")
        kind = "tool-hint" if sig.startswith(("tool-fail:", "denied:")) else "memory"
        with open(os.path.join(pdir, "%s.json" % pid), "w", encoding="utf-8") as _jf:
            json.dump(
                {
                    "id": pid,
                    "sig": sig,
                    "count": n,
                    "advice": r.get("advice") or "",
                    "kind": kind,
                    "created": time.strftime("%F %T"),
                    "status": "pending-human",
                },
                _jf,
                ensure_ascii=False,
                indent=1,
                sort_keys=True,
            )
        with open(os.path.join(pdir, "%s.md" % pid), "w", encoding="utf-8") as f:
            f.write("# 框架级提升提案：%s\n\n" % pid)
            f.write(
                "- 签名：%s（×%d，触发阈值 %d）\n" % (r.get("sig"), n, EVO_MIN_COUNT)
            )
            f.write("- 现有建议：%s\n" % r.get("advice"))
            f.write("- 提议方向（人审采纳；学习只建议，永不擅改门与判据）：\n")
            f.write("  1. 工具缺陷 → 修工具实现并补 --check 用例；\n")
            f.write("  2. 使用方式 → 建议升级进 AGENTS.md 项目记忆；\n")
            f.write("  3. 量具空白 → 提议新判据（先出 --check 断言草案）。\n")
            f.write("- 生成时间：%s\n" % time.strftime("%F %T"))
            f.write("- 机器 intake：同名 .json（validate_proposal 可验）\n")
        r["proposed"] = True
        made += 1
    if made:
        _evo_rules_save(rules, rp)
    return made


def _evo_midflight(rules_path=None, signals_path=None):
    """每轮末静默挖掘：新 CANDIDATE 返回给调用方提示一次（notified 节流），
    达 2 倍阈值自动生成提案。fail-open 全程。"""
    rules, err = _evo_mine(rules_path=rules_path, signals_path=signals_path)
    if err:
        return []
    new = [r for r in rules if r.get("state") == "CANDIDATE" and not r.get("notified")]
    _evo_propose(rules, rules_path)
    for r in new:
        r["notified"] = True
    if new:
        _evo_rules_save(rules, rules_path)
    return new


def _evo_converge_policy(rules_path=None):
    """ACTIVE 规则里的收敛调参 → 合并后的 policy 字典。

    这是「根源级自进化」的落点：规则不再只是往 system prompt 里塞一段
    提醒（**文字不改变行为**），而是能拧收敛引擎的行为旋钮——本机历史
    反复停滞/打转/只探查，就让引擎**更早**介入。

    冲突处理：多条规则命中同一旋钮时取**最紧**的值。规则是「本机老是
    在这里打转」的证据，方向一致（更早介入）；取最松等于让一条弱证据
    规则抵消掉强证据规则。
    """
    rules, _ = _evo_rules_load(rules_path)
    pol = {}
    for r in rules:
        if r.get("state") != "ACTIVE":
            continue
        act = r.get("action") or {}
        if not isinstance(act, dict) or act.get("op") != "converge_tune":
            continue
        for k in ("max_stall", "max_explore", "loop_repeat"):
            if k not in act:
                continue
            try:
                v = int(act[k])
            except (TypeError, ValueError):
                continue
            cur = pol.get(k)
            pol[k] = v if cur is None else min(cur, v)
    return pol


def _evo_prompt_lines(rules_path=None):
    """ACTIVE 规则 → system prompt 追加段；没有就空（不加空标题）。"""
    rules, _ = _evo_rules_load(rules_path)
    act = [r for r in rules if r.get("state") == "ACTIVE" and r.get("advice")]
    if not act:
        return []
    return ["自进化经验规则（本机历史失败挖掘，用户已晋升 ACTIVE，照此避坑）："] + [
        "- %s" % r["advice"] for r in act
    ]


# ── 记忆层级（蒸馏自 gemini-cli GEMINI.md / openai-codex AGENTS.md）────
# 约定：仓库根的 AGENTS.md / AGENTOS.md 是项目级记忆，每轮注入 system
# prompt；单文件封顶 4KB 防失控。没有这两类文件 = 零注入（不造假）。
_MEMORY_CAP = 4096


def _memory_files(root):
    out = []
    for name in ("AGENTS.md", "AGENTOS.md"):
        p = os.path.join(root or ROOT, name)
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                body = f.read(_MEMORY_CAP + 1)
        except OSError:
            continue
        if body.strip():
            if len(body) > _MEMORY_CAP:
                body = body[:_MEMORY_CAP] + "\n…（截断，单文件上限 4KB）"
            out.append((name, body.strip()))
    return out


def fold_old_tool_results(messages, keep_rounds=2, min_chars=600):
    """把**旧的**工具结果折叠成紧凑摘要（context editing 思路）。

    为什么（2026-10-22 的 token 账）：长会话的成本大头不是工具面（那是固定
    的），而是**历史里的工具结果** —— read_file 一次 2,000 行、run_cmd 一次
    几百行输出，都在**每一轮重发**一遍。第 20 轮的 prompt 里可能躺着 3 份
    同样的文件内容。

    折叠规则（每条都有理由，不靠猜）：
      · 只折叠**工具消息**（role=tool）—— user/assistant 原文一律不动；
      · 只折叠**倒数第 keep_rounds 轮之前**的（最近两轮是模型正在用的，
        折了模型就得回头重读，反而更贵）；
      · 只折叠**超过 min_chars** 的（小结果折了没意义还添乱）；
      · 摘要**保留事实**（ok / rc / 错误首行 / 输出首行），并明说「需要原文
        请重新调用该工具」—— 折叠不是「信息消失」，是「变成可重取」。

    返回**新列表**：原 messages 不动（会话记录要完整，折叠只发生在发请求时）。
    """
    if not isinstance(messages, (list, tuple)) or not messages:
        return list(messages or [])
    # 轮次 = 带 tool_calls 的 assistant 消息；它后面的 tool 消息属于同一轮
    round_of = []
    rnd = -1
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls"):
            rnd += 1
        round_of.append(rnd)
    total = rnd + 1
    if total <= keep_rounds:
        return list(messages)
    out = []
    for i, m in enumerate(messages):
        if not (isinstance(m, dict) and m.get("role") == "tool"):
            out.append(m)
            continue
        if round_of[i] < 0 or round_of[i] >= total - keep_rounds:
            out.append(m)
            continue
        txt = m.get("content")
        if not isinstance(txt, str) or len(txt) < min_chars:
            out.append(m)
            continue
        out.append(
            {
                "role": "tool",
                "tool_call_id": m.get("tool_call_id"),
                "content": _folded_stub(txt),
            }
        )
    return out


def _folded_stub(text):
    """旧工具结果的紧凑摘要：事实 + 「需要原文请重新调用该工具」。"""
    facts = []
    obj = None
    try:
        obj = json.loads(text)
    except Exception:  # noqa: BLE001
        obj = None
    if isinstance(obj, dict):
        if "ok" in obj:
            facts.append("ok=%s" % obj.get("ok"))
        if obj.get("rc") is not None:
            facts.append("rc=%s" % obj.get("rc"))
        err = str(obj.get("error") or "").strip()
        if err:
            facts.append("err=%s" % err.splitlines()[0][:80])
        for k in ("out", "text", "content", "summary"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                facts.append("首行=%s" % v.strip().splitlines()[0][:100])
                break
    else:
        s = str(text).strip()
        if s:
            facts.append("首行=%s" % s.splitlines()[0][:100])
    return json.dumps(
        {
            "folded": True,
            "chars": len(text),
            "summary": " ｜ ".join(facts)[:200],
            "hint": "旧工具结果已折叠（省 token）；需要原文请重新调用该工具",
        },
        ensure_ascii=False,
    )


def _memory_digest(body, cap=1200):
    """把项目记忆压成**确定性摘要**：每个小节留标题 + 该节第一条粗体规则。

    为什么（2026-10-22 的 token 账）：项目记忆原来**整段**注入 system prompt
    —— 本仓 AGENTS.md 3,634 字符 ≈ 1,355 token，**每一轮**都要付一遍，而
    里面大部分是叙述性铺垫，不是「常驻指令」。现在只注入：
      · 每个小节的标题（规则的全貌，一眼看到有哪些章）
      · 每节的第一条粗体规则（真正的约束句）
      · 一句指针：全文用 read_file 读原文（工具面本来就有 read_file）

    摘要**确定性**（不调模型、不猜）→ 可测、可复跑，不会「这次给下次不给」。
    丢的是叙述性解释，不是规则；真需要细节的模型可以 read_file 读回来。
    """
    out = []
    has_heading = False
    seen_bold = False
    in_code = False
    for raw in str(body or "").splitlines():
        ln = raw.strip()
        if ln.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if not ln:
            continue
        if ln.startswith("#"):
            # 代码块里的注释行（bash 步骤）已被上面的 in_code 挡掉，
            # 这里剩下的都是真标题（含「## 1.」这种编号标题）。
            out.append(ln)
            has_heading = True
            seen_bold = False
            continue
        # 每节第一条**加粗句**才算「常驻指令」（**铁律**：… / **铁律**…），
        # 散文与代码块不注入。粗体句常常不以 ** 结尾，所以只判开头。
        if not seen_bold and ln.startswith("**"):
            out.append(ln[:160])
            seen_bold = True
    if not has_heading:
        # 兜底：**没有标题结构的文件不许被摘要吃掉**。有些项目的 AGENTS.md
        # 就是一段散文（无 # 标题、无加粗句），摘要会把它整段丢掉 ——
        # 那是「项目记忆丢失」的行为回归（判据：记忆层级那条自检曾因此变红）。
        # 这种文件退回首段散文（截断），宁可多付点 token 也不能丢规则。
        _flat = " ".join(x.strip() for x in str(body or "").splitlines() if x.strip())
        out = [_flat[:400]] if _flat else []
    txt = "\n".join(out)
    if len(txt) > cap:
        txt = txt[:cap].rsplit("\n", 1)[0] + "\n…（摘要截断，全文用 read_file 读）"
    return txt


def _memory_prompt_lines(ctx):
    files = _memory_files(ctx.get("root"))
    if not files:
        return []
    lines = ["项目记忆（自 AGENTS.md / AGENTOS.md 层级读入，其中的约束照办）："]
    for name, body in files:
        lines.append("<<<%s 摘要>>>" % name)
        lines.append(_memory_digest(body))
        lines.append(
            "（要细则自己 read_file 读 %s 的全文；上面是结构与硬规则）" % name
        )
        lines.append("<<<END %s>>>" % name)
    return lines


# ── 自主力面（R3）：/ideas 机会挖掘（创造力）+ 回合回执自检行 ────────────
_IDEA_MARK_RE = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")


def _ideas_scan(root):
    """只读扫仓库出提案原料：返回 (marks, gaps)。

    marks = TODO/FIXME/XXX/HACK 留痕，每条带 file:line（AC5 的证据形态）；
    gaps = tools/ 源文件里从未被任何其他 .py 提到 stem 的「候选测试缺口」
    （advisory：假阳性可接受，产出时明说是候选）。跳过 work/、venv、备份。
    """
    marks, stems = [], {}
    skip_dirs = {"work", ".venv-pw", "__pycache__", "node_modules", ".git", "outputs"}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            if (
                not fn.endswith(".py")
                or ".bak" in fn
                or fn.startswith("_st")
                or fn == "agent_core.py"
            ):
                continue
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root)
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    body = f.read()
            except OSError:
                continue
            stems.setdefault(os.path.splitext(fn)[0], rel)
            for i, ln in enumerate(body.splitlines(), 1):
                if _IDEA_MARK_RE.search(ln):
                    marks.append("%s:%d %s" % (rel, i, ln.strip()[:90]))
    gaps = []
    for stem, rel in sorted(stems.items()):
        mentioned = False
        for other_stem, other_rel in stems.items():
            if other_stem == stem:
                continue
            try:
                with open(
                    os.path.join(root, other_rel), encoding="utf-8", errors="replace"
                ) as f:
                    if stem in f.read():
                        mentioned = True
                        break
            except OSError:
                continue
        if not mentioned:
            gaps.append(rel)
    return marks, gaps


def _repl_ideas(ctx):
    """/ideas：把扫描结果渲染成提案清单进 stderr（只读，零写入）。"""
    root = ctx.get("root") or ROOT
    t0 = time.time()
    marks, gaps = _ideas_scan(root)
    sys.stderr.write("机会挖掘（%.1fs，只读；根 %s）：\n" % (time.time() - t0, root))
    if marks:
        sys.stderr.write(
            "· 留痕（TODO/FIXME/XXX/HACK）%d 条，前 20 条：\n" % len(marks)
        )
        for x in marks[:20]:
            sys.stderr.write("  %s\n" % x)
    else:
        sys.stderr.write("· 留痕：0 条（源码无 TODO/FIXME 挂账）\n")
    if gaps:
        sys.stderr.write(
            "· 候选测试缺口 %d 个（stem 从未被其他 .py 引用，"
            "建议补自检或门）：\n" % len(gaps)
        )
        for g in gaps[:15]:
            sys.stderr.write("  %s\n" % g)
    sys.stderr.write(
        "提案：留痕逐条转 issue（agentos gh issue-create "
        "--dry-run 预演，--yes+配置+确认才真写）；缺口逐个补 "
        "--check 用例。\n"
    )


def _session_dir():
    return SESSION_DIR


def _new_session_id():
    """agent-YYYYMMDD-HHMMSS-xxxx。前缀与 cli- 区分，后缀便于人肉认。"""
    return "%s%s-%s" % (
        TRANSCRIPT_PREFIX,
        time.strftime("%Y%m%d-%H%M%S"),
        uuid.uuid4().hex[:4],
    )


def _transcript_path(sid):
    return os.path.join(_session_dir(), "%s.jsonl" % sid)


def _transcript_list():
    """只认 agent-*.jsonl，按文件名排序（时间前缀天然有序）。"""
    d = _session_dir()
    if not os.path.isdir(d):
        return []
    return [
        os.path.join(d, n)
        for n in sorted(os.listdir(d))
        if n.startswith(TRANSCRIPT_PREFIX) and n.endswith(".jsonl")
    ]


def _transcript_append(path, rec):
    """追加一条 JSONL。目录不存在就建；失败不抛，返回 False 让调用方如实说。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def _transcript_load(path):
    recs = []
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if raw:
                recs.append(json.loads(raw))
    return recs


def _history_from_transcript(recs, keep=COMPACT_KEEP):
    """transcript 记录 -> messages（system 由 run_agent 自己补，这里不放）。"""
    msgs = []
    for r in recs:
        if not isinstance(r, dict):
            continue
        kind, text = r.get("kind"), r.get("text")
        if kind == "user" and text:
            msgs.append({"role": "user", "content": str(text)})
        elif kind == "assistant" and text:
            msgs.append({"role": "assistant", "content": str(text)})
    return msgs[-keep:] if keep > 0 else msgs


def _safe_cut(rest, start):
    """把切往前挪到「工具配对不破」的边界，返回修正后的 start。

    为什么必须有：把 start 直接钉在 keep 上，切点会落在 assistant
    (tool_calls) 与其 **tool 结果**的中间 —— 开头第一条是孤 tool 消息，
    下一次请求直接被网关 400（实测 2026-10-18：markdown-it-py 真任务
    2:36 后报 "No tool call found for tool output with call_id …"，一步
    黑掉整轮）。修法不是改阈值，是不许切进配对里：切点之前如果还挂着
    assistant(tools_calls)，它的 tool 结果也一起带走。
    """
    n = len(rest)
    start = max(0, min(start, n))
    while start < n and rest[start].get("role") == "tool":
        start -= 1
    start = max(0, start)
    # 切点左边是带 tool_calls 的 assistant：其 tool 结果在切点之后 → 破配对
    if (
        start > 0
        and (rest[start - 1].get("tool_calls") or rest[start - 1].get("tool"))
    ):
        # 把 assistant 一起留下（连同它前面更早的同环成员，放心往前扫）
        while start > 0 and rest[start - 1].get("role") == "tool":
            start -= 1
        start -= 1
    return start


def _compact_history(history, keep=COMPACT_KEEP):
    """保留 system + 最近 keep 条，返回 (新历史, 丢掉条数)。

    为什么单独成函数：`/compact` 与 run_agent 的自动裁剪必须同一套口径，
    否则「手动压过」和「自动压过」保留的东西不一样，用户看到的上下文
    和实际喂给模型的对不上。切点走 _safe_cut：宁可多留一条，不留孤儿。
    """
    if not isinstance(history, list):
        return [], 0
    head = [history[0]] if (history and history[0].get("role") == "system") else []
    rest = history[1:] if head else list(history)
    st = _safe_cut(rest, len(rest) - keep)
    dropped = st  # 从头上切掉的就是 st 条（切点只会往前挪，不会往后）
    return head + (rest[st:] if keep > 0 else rest), dropped


DIGEST_PREFIX = "【上下文摘要·自动压缩】"
DIGEST_PROMPT = (
    "把下面被压缩掉的对话历史提炼成事实摘要：保留文件路径、做出的决策、"
    "已跑命令的关键 rc、未完成的 TODO；不要寒暄、不要编造，直接输出摘要正文。"
)
DIGEST_MAX_CHARS = 6000  # 摘要请求体上限（防压缩本身超大）
DIGEST_MSG_CHARS = 400  # 每条被压消息进请求体的截断上限


def _digest_input(msgs):
    """把被丢弃的消息渲成摘要请求体（每条截断，总量封顶）。"""
    lines = []
    for m in msgs:
        role = m.get("role", "?")
        body = m.get("content")
        if not body and m.get("tool_calls"):
            body = (
                "tool_calls=%s"
                % json.dumps(m.get("tool_calls"), ensure_ascii=False)[:DIGEST_MSG_CHARS]
            )
        body = str(body or "")
        lines.append("%s: %s" % (role, body[:DIGEST_MSG_CHARS]))
    return "\n".join(lines)[:DIGEST_MAX_CHARS]


def _compact_budget():
    """本轮压缩的 token 预算。0 = 没配 context_window → 走旧的「按条数」口径。

    为什么要有预算口径：旧口径是固定保留最近 N 条消息（N=40），
    与实际 token 量无关 —— 上下文窗口小的时候它压不进去（模型直接 400），
    窗口大的时候它又压得太狠（能留的没留）。
    预算口径是蒸馏自 claude-code 的双层预算与 OpenHands 的 head+tail+摘要。
    """
    try:
        cw = int(
            (_kernel()._read_agentos_config().get("llm") or {}).get("context_window") or 0
        )
    except Exception:  # noqa: BLE001
        cw = 0
    return cw if cw > 0 else 0


def _compact_history_v2(history, keep=COMPACT_KEEP, provider=None, focus=""):
    """compact v2：丢弃前先用 provider 生成事实摘要 digest，失败回退旧行为。

    返回 (新历史, 丢掉条数, digest_used)。为什么保留旧 _compact_history：
    provider 缺席/失败时口径必须与旧版**逐字相同**（回退不是"差不多"）——
    --check 与 rewind_gate 都拿旧函数输出当回退基准做逐字对账。
    """
    if not isinstance(history, list):
        return [], 0, False
    # 预算感知（配了 context_window 才有）：保住**用户的 ask**（R1）+
    # 连续的尾巴（R2）；连这两样都装不下就**拒绝压缩**（R3，如实说，
    # 不谎报 fit —— 悄悄截断历史是 agent 开始幻觉连续性的起点）。
    # 没配 context_window 时走旧口径（逐字不变，--check/rewind_gate 拿它当基准）。
    _plan = None
    _budget = _compact_budget()
    if _budget > 0:
        try:
            import ctx_compact as _cc

            _plan = _cc.plan(list(history), _budget)
        except Exception:  # noqa: BLE001  规划失败就退回旧口径，不阻断
            _plan = None
    if _plan is not None and not _plan.fits:
        return list(history), 0, False
    if _plan is not None:
        _keep_idx = set(_plan.keep_idx)
        dropped_msgs = [m for i, m in enumerate(history) if i not in _keep_idx]
        if not dropped_msgs:
            return list(history), 0, False
        _sys_head = [
            m for i, m in enumerate(history) if i in _keep_idx and m.get("role") == "system"
        ]
        _rest_kept = [
            m for i, m in enumerate(history) if i in _keep_idx and m.get("role") != "system"
        ]
    else:
        _sys_head = []
        _rest_kept = []
    head = [history[0]] if (history and history[0].get("role") == "system") else []
    rest = history[1:] if head else list(history)
    if _plan is None and len(rest) <= keep:
        return list(history), 0, False
    if _plan is None:
        dropped_msgs = rest[: len(rest) - keep]
    digest = ""
    if provider is not None:
        try:
            prompt = DIGEST_PROMPT
            if focus:
                prompt += "\n聚焦要求：%s" % focus
            comp = provider.chat(
                [
                    {
                        "role": "user",
                        "content": prompt + "\n\n" + _digest_input(dropped_msgs),
                    }
                ],
                tools=None,
                stream=False,
            )
            digest = str((comp or {}).get("content") or "").strip()
        except Exception:  # noqa: BLE001
            digest = ""
    if not digest:
        if _plan is not None:
            # 计划已定（保住 ask + 连续尾巴）→ 回退也**按计划**来，
            # 不许退回旧口径（旧口径会把用户的 ask 丢掉 —— 那正是
            # ctx_compact 的 R1 要防的事）。摘要用「省略了多少」的自述。
            try:
                import ctx_compact as _cc

                _new = _cc.compact(list(history), _plan)
                return _new, len(dropped_msgs), False
            except Exception:  # noqa: BLE001
                pass
        new_hist, dropped = _compact_history(history, keep)
        return new_hist, dropped, False
    digest_msg = {"role": "user", "content": DIGEST_PREFIX + "\n" + digest}
    if _plan is not None:
        # 形状：系统头 + digest + （用户的 ask + 连续尾巴）
        return _sys_head + [digest_msg] + _rest_kept, len(dropped_msgs), True
    return head + [digest_msg] + rest[-keep:], len(dropped_msgs), True


def _rewind_replay(recs, n):
    """把 transcript 记录重放成「前 n 轮末」的 messages。

    一轮 = 一条 user 记录（含其后到下一轮 user 前的全部记录）。返回
    (msgs, err, turns)；越界（n<1 或 n>轮数）fail-closed：err 非空、msgs=None。
    纯函数：不碰盘、不碰 ctx —— rewind_gate 直接拿夹具喂它证伪。
    """
    turns = [
        i
        for i, r in enumerate(recs)
        if isinstance(r, dict) and r.get("kind") == "user" and r.get("text")
    ]
    if n < 1 or n > len(turns):
        return None, "越界：本会话共 %d 轮，请求第 %d 轮" % (len(turns), n), len(turns)
    end = turns[n] if n < len(turns) else len(recs)
    return _history_from_transcript(recs[:end], keep=0), "", len(turns)


def _usage_total(acc, usage):
    """累加 token 用量。端点没报 usage 就原样返回（不编数字）。"""
    out = dict(acc or {})
    if not isinstance(usage, dict):
        return out
    for k, v in usage.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        out[k] = out.get(k, 0) + v
    return out


def _expand_at_refs(text, root, limit=3, max_bytes=4000):
    """把 `@相对路径` 展开成 <file path=...>内容</file>，返回 (新文本, 用到
    的路径列表)。只读 root 内的普通文件，越界/不存在/超量的原样保留。

    claude code 的 `@file` 是终端里最常用的一个手势：模型不用先调一次
    read_file 就能看见文件。这里复用 _sandbox 的 realpath 边界，不新开
    一条绕过沙箱的读盘路径。
    """
    out, used = [], []
    for tok in str(text or "").split():
        if not (tok.startswith("@") and len(tok) > 1):
            out.append(tok)
            continue
        rel = tok[1:]
        real, err = _kernel()._sandbox(rel, root)
        if err or not real or not os.path.isfile(real) or len(used) >= limit:
            out.append(tok)
            continue
        try:
            with open(real, "r", encoding="utf-8", errors="replace") as f:
                body = f.read(max_bytes + 1)
        except OSError:
            out.append(tok)
            continue
        if len(body) > max_bytes:
            body = body[:max_bytes] + "\n...[已截断]"
        out.append("<file path=%s>\n%s\n</file>" % (rel, body))
        used.append(rel)
    return " ".join(out), used


def _status_line(ctx, sid):
    """`/status` 的一行读数。所有值都从 ctx 现取，不缓存、不估算。"""
    return "session=%s · 模式=%s · 仓库=%s · 模型=%s · 上下文=%d 条 · 累计用量=%s" % (
        sid or "-",
        ctx.get("mode"),
        ctx.get("root"),
        ctx.get("model") or "-",
        len(ctx.get("history") or []),
        json.dumps(ctx.get("usage_total") or {}, ensure_ascii=False),
    )
