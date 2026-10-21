#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""token_gate —— 「每轮固定开销」的门：把 token 账钉住，不许悄悄涨回去。

为什么要有这道门（2026-10-22 的真机测量）
--------------------------------------
一次真实测量（端点 opencode.ai/zen，模型 deepseek-v4.1-flash）：

    裸消息（无系统无工具）        prompt_tokens =    33
    加系统提示                    prompt_tokens =   907   → 系统提示   874
    加系统提示 + 全部工具         prompt_tokens =  3627   → 工具面    2720

**工具面每轮都要付 2,720 token**（首轮提示的 75%）。端点**不报前缀缓存**
（usage 里没有 cache 字段），所以这笔钱每轮都付真金白银。20 步的会话 =
5 万多 token 纯 schema。

于是有了这道门：量「系统提示 + 工具数组」这笔**固定开销**，钉住预算。
本轮已经砍掉的（都是实测，不猜）：

    固定开销 13,084 → 8,497 字符（≈ 4,882 → 3,170 token，-35%）
      · 工具描述与 schema 瘦身：散文归系统提示与 runtime 错误，
        schema 只留参数名/类型/枚举/必填
      · 去掉系统提示里重复的「可用工具：…」（tools 数组里已经有了）
      · 技能段从「全量描述」改成「目录式」（名字 + 4 词提示），
        正文靠新增的 skills 工具按需拉
      · 项目记忆（AGENTS.md）从「整段注入」改成「确定性摘要」
        （标题 + 每节首条硬规则 + read_file 指针）

用法：python3 tools/token_gate.py [--live] [--json OUT]
裁决：TOKEN_GATE n/n + TOKEN_GATE_OK，exit 0/1
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

# 实测校准（2026-10-22）：9,634 字符的「系统提示 + 工具数组」= 3,594 token
# → 2.68 字符/token。中文 JSON 负载的经验值；预算用**字符**钉（精确可复跑），
# token 只给人看。
CHARS_PER_TOKEN = 2.68

# 预算（ratchet：这是**上限**，涨预算必须是一次有意识的决定）
BUDGET_FIXED = 9000     # 固定开销合计（当前 8,497）
BUDGET_TOOLS = 7000     # 工具面（当前 6,595）
BUDGET_SYSTEM = 2200    # 系统提示（当前 1,902）
BUDGET_ONE_TOOL = 800   # 单个工具 desc+schema（当前 todo 790）

CASES = []


def case(name, ok, detail=""):
    CASES.append({"name": name, "ok": bool(ok), "detail": str(detail)[:240]})
    print(("  PASS " if ok else "  FAIL ") + name + ("  -- " + str(detail)[:160] if detail else ""))


# ── 判据（写成函数，好让「判据有牙」那条能拿变异输入去过）────────────
def has_dup_tool_list(sys_text):
    """系统提示里重复列工具名？（tools 数组里已有 → 纯浪费）"""
    return "可用工具：" in (sys_text or "")


def skills_is_index(sys_text):
    """技能段是目录式（含「skills 工具可读正文」的指针）？"""
    return "skills 工具可读正文" in (sys_text or "")


def memory_is_digest(sys_text):
    """项目记忆按摘要注入（有 read_file 指针，不整段注入）？"""
    return ("项目记忆" in (sys_text or "")) and ("read_file 读" in (sys_text or ""))


def measure(mode="full"):
    """量一个 mode 下的固定开销。"""
    import agent_core as ac

    ctx = {"root": ROOT, "mode": mode, "sid": "gate"}
    sp = ac.build_system_prompt(ctx)
    sys_txt = sp if isinstance(sp, str) else json.dumps(sp, ensure_ascii=False)
    defs = ac.tool_defs()
    tool_txt = json.dumps(defs, ensure_ascii=False)
    per_tool = []
    for d in defs:
        fn = d.get("function", d)
        per_tool.append(
            {
                "name": fn.get("name"),
                "desc": len(fn.get("description") or ""),
                "schema": len(json.dumps(fn.get("parameters") or {}, ensure_ascii=False)),
            }
        )
    per_tool.sort(key=lambda x: -(x["desc"] + x["schema"]))
    return {
        "mode": mode,
        "system_chars": len(sys_txt),
        "tools_chars": len(tool_txt),
        "fixed_chars": len(sys_txt) + len(tool_txt),
        "tool_count": len(defs),
        "per_tool": per_tool,
        "system_text": sys_txt,
    }


def live_probe(m):
    """打真端点，量真实 token（可选；离线门不依赖它）。"""
    import agent_core as ac

    cfg = ac.resolve_provider()
    P = ac.Provider(cfg)
    defs = ac.tool_defs()
    msgs = [{"role": "system", "content": m["system_text"]}]
    r0 = P.chat([{"role": "user", "content": "说：ok"}], tools=[])
    r1 = P.chat(msgs, tools=[])
    r2 = P.chat(msgs, tools=defs)
    g = lambda r: (r.get("usage") or {}).get("prompt_tokens")  # noqa: E731
    out = {
        "bare": g(r0),
        "system": g(r1),
        "system_plus_tools": g(r2),
    }
    out["system_tokens"] = (out["system"] or 0) - (out["bare"] or 0)
    out["tools_tokens"] = (out["system_plus_tools"] or 0) - (out["system"] or 0)
    out["fixed_tokens"] = (out["system_plus_tools"] or 0) - (out["bare"] or 0)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="额外打一次真端点量真实 token")
    ap.add_argument("--json", default=os.path.join(ROOT, "work", "token_bench.json"))
    args = ap.parse_args(argv)

    print("TOKEN_GATE 每轮固定开销（每轮都付的那笔）")
    m = measure("full")
    est = int(m["fixed_chars"] / CHARS_PER_TOKEN)
    print(
        "  系统提示 %d 字符 ｜ 工具面 %d 字符（%d 个工具）｜ 合计 %d 字符 ≈ %d token"
        % (m["system_chars"], m["tools_chars"], m["tool_count"],
           m["fixed_chars"], est)
    )
    print("  最肥的工具：")
    for it in m["per_tool"][:5]:
        print("    %-14s desc=%-5d schema=%-5d" % (it["name"], it["desc"], it["schema"]))

    live = {}
    if args.live:
        try:
            live = live_probe(m)
            print(
                "  真端点：系统 %s token ｜ 工具面 %s token ｜ 合计 %s token"
                % (live.get("system_tokens"), live.get("tools_tokens"),
                   live.get("fixed_tokens"))
            )
        except Exception as e:  # noqa: BLE001
            print("  真端点探测失败（不影响离线门）：%r" % (e,))

    # ── 预算（ratchet）──────────────────────────────────────────
    case(
        "固定开销 ≤ %d 字符（基线 13,084 → 本轮 8,497）" % BUDGET_FIXED,
        m["fixed_chars"] <= BUDGET_FIXED,
        "%d 字符" % m["fixed_chars"],
    )
    case(
        "工具面 ≤ %d 字符（基线 7,301）" % BUDGET_TOOLS,
        m["tools_chars"] <= BUDGET_TOOLS,
        "%d 字符" % m["tools_chars"],
    )
    case(
        "系统提示 ≤ %d 字符（基线 5,783 → 本轮 1,902）" % BUDGET_SYSTEM,
        m["system_chars"] <= BUDGET_SYSTEM,
        "%d 字符" % m["system_chars"],
    )
    case(
        "单个工具 desc+schema ≤ %d 字符（基线 todo 1,424）" % BUDGET_ONE_TOOL,
        all((it["desc"] + it["schema"]) <= BUDGET_ONE_TOOL for it in m["per_tool"]),
        "最肥=%s(%d)"
        % (m["per_tool"][0]["name"],
           m["per_tool"][0]["desc"] + m["per_tool"][0]["schema"]),
    )

    # ── 结构性判据（不是「省了多少」，是「不许退回老写法」）──────
    case(
        "系统提示里不重复列工具名（工具数组里已有，重复=白花钱）",
        not has_dup_tool_list(m["system_text"]),
        "命中" if has_dup_tool_list(m["system_text"]) else "无",
    )
    case(
        "技能段是目录式（正文靠 skills 工具按需拉）",
        skills_is_index(m["system_text"]),
        "有" if skills_is_index(m["system_text"]) else "无",
    )
    case(
        "项目记忆按摘要注入（带 read_file 指针，不整段注入）",
        memory_is_digest(m["system_text"]),
        "有" if memory_is_digest(m["system_text"]) else "无",
    )

    # ── 判据有牙：把老写法塞回去，判据必须报红 ──────────────────
    case(
        "判据有牙：把「可用工具：」塞回系统提示 → 判据必须抓到",
        has_dup_tool_list(m["system_text"] + "\n可用工具：a b c"),
        "注入后命中=%s" % has_dup_tool_list(m["system_text"] + "\n可用工具：a b c"),
    )
    case(
        "判据有牙：把技能段退回全量描述 → 目录式判据必须报红",
        not skills_is_index(m["system_text"].replace("skills 工具可读正文", "（/skill <名> 注入正文）")),
        "退回后命中=%s"
        % skills_is_index(m["system_text"].replace("skills 工具可读正文", "（/skill <名> 注入正文）")),
    )

    out = {
        "tool": "token_gate",
        "chars_per_token": CHARS_PER_TOKEN,
        "budget": {
            "fixed": BUDGET_FIXED,
            "tools": BUDGET_TOOLS,
            "system": BUDGET_SYSTEM,
            "one_tool": BUDGET_ONE_TOOL,
        },
        "measured": {k: v for k, v in m.items() if k != "system_text"},
        "estimated_fixed_tokens": est,
        "live": live,
    }
    try:
        os.makedirs(os.path.dirname(args.json), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
    except OSError as e:
        print("  账本写不了（不致命）：%r" % (e,))

    bad = [c for c in CASES if not c["ok"]]
    print("\nTOKEN_GATE %d/%d" % (len(CASES) - len(bad), len(CASES)))
    print("TOKEN_GATE_OK" if not bad else "TOKEN_GATE_RED")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
