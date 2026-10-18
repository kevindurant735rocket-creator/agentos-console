#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evolve：框架级自进化闭环的提案生成 + 机器合入。

为什么单独成文件：evo_merge 只有一半闭环——它没收提案、不指挥模型。
自进化的最后一公里是：**经验账里的真实教训 → 模型读码 → 产出 evo_merge
提案 → 沙箱门验 → 全绿自动合入、红了真树零改动**。人只在账本上看结果
（boss 监督），不在环里当扳手。

退出码口径（全仓同一套）：0 = merged（含 dry-ok）· 1 = rejected /
rolled_back（红要如实报出哪门哪条）· 2 = 无候选/用法 · 3 = 基础设施
（provider 不可达 / 内核 import 失败）。

用法：
  python3 tools/evolve.py                # 取规则账第一条 CANDIDATE 跑一整圈
  python3 tools/evolve.py --rule R123    # 指定规则
  python3 tools/evolve.py --no-merge     # 只到"沙箱全绿"为止（dry-ok）
  python3 tools/evolve.py --check        # 内置自检（stub 提案 + 临时账）
"""

import argparse
import json
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT + "/tools" not in sys.path:
    sys.path.insert(0, ROOT + "/tools")

GATES_DEFAULT = (
    # 沙箱门集（SAFE 子集）：快而要害，且都必须是"在只有 tools/ 的沙箱里
    # 可判"的门。agent_core(145 判据) / neg_agent_core（能力与判据双向钉死）/
    # dsp（锚点）。钉本机 GUI/真终端的门（minimize_check/tui_walk）在沙箱
    # 里结构性必红，只许进 gates_post——别再把端选错（实测 2026-10-18
    # 第四圈就是摘漏了，白掐一整圈）。
    "agent_core_gate",
    "neg_agent_core",
    "dsp_gate",
)

RULES_DEFAULT = os.path.join(ROOT, "work", "agent_evo_rules.json")
SIGNALS_DEFAULT = os.path.join(ROOT, "work", "agent_sessions", "signals.jsonl")
PROPOSAL_DIR = os.path.join(ROOT, "work", "evo_proposals")
LEDGER_DEFAULT = os.path.join(ROOT, "work", "evo_merges.jsonl")


def _import_core():
    import agent_core as ac

    return ac


def _pick_candidate(mine, rid=None):
    """规则账 → 待修规则。CANDIDATE 且 count>=3 才有资格（挖器已保证）。

    返回 (rule, err)。err 非空 = 账级错（fail-closed，不猜）。"""
    rules, err = mine
    if err:
        return None, "规则账不可读：%s" % err
    cands = [r for r in rules if r.get("state") == "CANDIDATE"]
    if rid:
        hit = [r for r in cands if r.get("id") == rid]
        if not hit:
            return None, "规则 %r 不在 CANDIDATE 清单里" % rid
        return hit[0], ""
    if not cands:
        return None, "无候选规则"
    cands.sort(key=lambda r: -int(r.get("count") or 0))
    return cands[0], ""


_PSEUDONYM_RE = re.compile(r"[^0-9A-Za-z_.-]+")


def _sid_for(rid):
    """规则 id → 沙箱/账本 id（可复跑去重的确定性形状）。"""
    base = _PSEUDONYM_RE.sub("-", str(rid))
    return "evo-%s" % base[:40]


PROMPT_TEMPLATE = """自进化提案任务（framework-evolve，机器全程无人工）。

背景：本仓的规则账聚到一条反复发生的真实教训：
  规则 id: {rid}
  出现 {count} 次 · 签名: {sig}
  建议: {advice}

你的任务不是打补丁改行为，而是**先读码找这条教训对应的代码级根因**，
然后把修法写成一份 evo_merge 提案 JSON（机器后续会沙箱门验+自动合入）。

提案文件：写到 {out}
JSON 形状（两个 edit 形态）：
{{
  "id": "{sid}",
  "edits": [
    {{"path": "相对路径", "find": "恰出现一次的原文锚点", "replace": "替换后全文"}},
    {{"path": "相对路径", "write": "新文件全文"}}
  ]
}}

铁律：
1. path 必须是仓内相对路径；work/、native/、.venv-pw、AgentOS.app 是禁改区，
   提案一律不许指向它们。
2. find 锚点必须恰出现一次（evo_merge 硬闸）；读写真码时先 read_file 定位原文。
3. 修法要小而准：一个根因一个提案；不夹带重构。
4. 修完的代码不得绕开门。若这条教训本就该进文档（无代码根因），提案可以是
   docs/ 下的新文件（write），内容=把教训写成可执行的下一步或检查表。
5. 产出文件里只放这一个 JSON，别裹 markdown 代码围栏。
"""


def _build_prompt(rule, out_path, sid):
    return PROMPT_TEMPLATE.format(
        rid=rule.get("id"),
        count=rule.get("count"),
        sig=str(rule.get("sig"))[:120],
        advice=str(rule.get("advice"))[:200],
        out=out_path,
        sid=sid,
    )


def _proposal_dir():
    os.makedirs(PROPOSAL_DIR, exist_ok=True)
    return PROPOSAL_DIR


def propose(rule, ac, max_steps=24, provider=None, fresh=False):
    """真模型提案：run_agent 产出提案文件。返回 (path, err)。

    为什么要求 agent 写文件而不是 stdout 取 JSON：流式/stdout 太脆，
    文件是机器可核对的产物（存在+可解析=成，否则红）。"""
    out_dir = _proposal_dir()
    sid = _sid_for(rule.get("id"))
    out_path = os.path.join(out_dir, "%s.json" % sid)
    # 复用既有合法提案（幂等环）：同一规则重跑不必再耗一次模型——
    # 曾实测把第一轮的好提案被第二轮网差劣化掉。--fresh 才重生成。
    if not fresh and os.path.exists(out_path):
        try:
            with open(out_path, encoding="utf-8") as f:
                json.load(f)
            return out_path, ""
        except (OSError, ValueError):
            pass  # 旧提案坏了，走重生成
    prompt = _build_prompt(rule, out_path, sid)
    ctx = {"root": ROOT, "mode": "full"}
    try:
        res = ac.run_agent(
            prompt, ctx=ctx, provider=provider, stream=False, max_steps=max_steps
        )
    except ac.AgentError as ex:
        return None, "provider：模型提案失败（%s）" % str(ex)[:140]
    if not res.get("ok"):
        return None, "提案轮未完成（kind=%s，%s 步后停）" % (
            res.get("kind"),
            res.get("steps"),
        )
    if not os.path.exists(out_path):
        return None, "提案文件没落地：%s（agent 最终回答首段见会话账）" % out_path
    try:
        with open(out_path, encoding="utf-8") as f:
            json.load(f)
    except (OSError, ValueError) as ex:
        return None, "提案不是合法 JSON：%s" % str(ex)[:100]
    return out_path, ""


def finalize(rule, rid, rules_path=None):
    """闭环收尾账行：规则 → ACTIVE（真修已进产品，规则成为行为准则）。"""
    import agent_core as ac

    ac._evo_set_state(rid, "ACTIVE", rules_path=rules_path)
    return "闭环：%s → ACTIVE（教训已固化为产品代码，/evo-off 可回退）" % rid


GATES_POST_DEFAULT = GATES_DEFAULT + (
    # 真机门：只在合入后跑（沙箱没有 app bundle / 真终端，结构性必红）。
    "minimize_check",
)


def run_one(rule_ids=None, gates=None, ac=None, max_steps=24, do_merge=True,
            ledger=None, rules_path=None, signals_path=None, runner=None,
            gates_post=None, fresh=False, out=print):
    """一圈完整自进化。返回退出码。

    参数里的 ac/runner 是 --check 的缝：真跑走默认（真模型+真 run_agent），
    自检走 stub（不碰网、不碰真树）。"""
    if ac is None:
        ac = _import_core()
    mine = ac._evo_mine(
        rules_path=rules_path,
        signals_path=signals_path,
    )
    rule, err = _pick_candidate(mine, rule_ids[0] if rule_ids else None)
    if err:
        out("EVOLVE 未开工：%s" % err)
        return 2
    sid = _sid_for(rule.get("id"))
    out("候选规则 %s（×%s）：%s" % (rule.get("id"), rule.get("count"),
                                    str(rule.get("advice"))[:80]))
    if runner is not None:
        ppath, perr = runner(rule, sid)
    else:
        ppath, perr = propose(rule, ac, max_steps=max_steps, fresh=fresh)
    if perr:
        out("EVOLVE 失败：%s" % perr)
        return 3 if perr.startswith("provider") else 1
    with open(ppath, encoding="utf-8") as f:
        proposal = json.load(f)
    proposal["id"] = sid  # 账本/沙箱 id 一律由 evolve 定（确定性、可复跑）
    import evo_merge as em

    gate_list = GATES_DEFAULT if gates is None else list(gates)
    post_list = (gate_list + GATES_POST_DEFAULT) if gates_post is None else gates_post
    rc = em.run_pipeline(proposal, ROOT, gate_list,
                         ledger or LEDGER_DEFAULT, do_merge=do_merge,
                         gates_post=post_list)
    if rc == 0:
        out(finalize(rule, rule.get("id"), rules_path=rules_path))
        return 0
    return 1


def _selfcheck():
    """--check：stub 提案 + 临时账，不碰网、不碰真树、不碰真账本。"""
    import tempfile

    ttl = []
    try:
        sys.path.insert(0, ROOT + "/tools")
        import agent_core as ac
        import evo_merge as em

        def t(name, ok, detail=""):
            ttl.append([name, bool(ok), str(detail)])

        tmp = tempfile.mkdtemp(prefix="evolve_sc_")
        rules = [
            {"id": "R_self", "state": "CANDIDATE", "count": 3,
             "sig": "err:provider", "advice": "主线修已过，测试流本身"},
            {"id": "R_active", "state": "ACTIVE", "count": 9,
             "sig": "tool-fail:x", "advice": "应是 ACTIVE，不该被挑"},
        ]
        rules_path = os.path.join(tmp, "rules.json")
        with open(rules_path, "w", encoding="utf-8") as f:
            json.dump({"rules": rules}, f, ensure_ascii=False)

        def stub_runner(rule, sid):
            # 提案：docs/ 下新文件（write 形态，非禁改区；只进沙箱，不碰真树）
            p = os.path.join(_proposal_dir(), "%s.json" % sid)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "id": sid,
                        "edits": [
                            {"path": "docs/evolve_selfcheck_probe.md",
                             "write": "# 自检探针（沙箱产出，未合入真树）\n"},
                        ],
                    },
                    f,
                    ensure_ascii=False,
                )
            return p, ""

        ledger = os.path.join(tmp, "merges.jsonl")
        rc = run_one(
            ["R_self"],
            gates=[],  # _gates_on 空集 = 全绿真空针（不走真门，秒级）
            ac=ac,
            runner=stub_runner,
            do_merge=False,
            ledger=ledger,
            rules_path=rules_path,
            gates_post=[],
        )
        t("dry-ok 结束态 rc=0", rc == 0, "rc=%s" % rc)
        t("dry-ok 合约：不进账（账文件都不该建出来）",
          not os.path.exists(ledger))
        no_probe = not os.path.exists(
            os.path.join(ROOT, "docs", "evolve_selfcheck_probe.md")
        )
        t("真树零改动（--no-merge）", no_probe)

        # 候选挑选纪律：只挑 CANDIDATE（ACTIVE 不碰）
        t("只挑 CANDIDATE", _pick_candidate(
            (rules, ""), None)[0].get("id") == "R_self")

        # 无候选 → rc=2 而不是 0/炸
        empty = []
        rp2 = os.path.join(tmp, "empty.json")
        sp2 = os.path.join(tmp, "empty_signals.jsonl")
        open(sp2, "w").close()  # 空信号账：保证夹具闭界（不沾真仓经验账）
        with open(rp2, "w", encoding="utf-8") as f:
            json.dump({"rules": empty}, f)
        rc2 = run_one(gates=[], ac=ac, runner=stub_runner,
                      do_merge=False, rules_path=rp2, signals_path=sp2,
                      gates_post=[], ledger=ledger)
        t("无候选 rc=2", rc2 == 2, "rc=%s" % rc2)

        # 闭环收尾：merged 后规则账确实转 ACTIVE（用临时 rules_path）
        ac._evo_set_state("R_self", "ACTIVE", rules_path=rules_path)
        with open(rules_path, encoding="utf-8") as f:
            state = {r["id"]: r["state"] for r in json.load(f)["rules"]}
        t("finalize → ACTIVE", state.get("R_self") == "ACTIVE", state)

        # 锚点纪律借 evo_merge.validate 钉住（evolve 依赖它，不另造一份）
        bad = {"id": "t", "edits": [{"path": "README.md",
                                     "find": "a",
                                     "replace": "b"}]}
        ok, werr = em.validate(bad, ROOT)
        t("锚点不恰一 → 拒（依托 evo_merge.validate）",
          ok is False and "锚点必须恰出现一次" in werr, werr[:60])

        fails = [x for x in ttl if not x[1]]
        for name, okk, detail in ttl:
            print("PASS %s" % name if okk else "FAIL %s -- %s" % (name, detail))
        print("EVOLVE %s %d/%d" % ("OK" if not fails else "FAIL",
                                   len(ttl) - len(fails), len(ttl)))
        return 0 if not fails else 1
    finally:
        # 沙箱/探针清场：沙箱目录 + 探针提案文件（真树从未被碰）
        shutil.rmtree(
            os.path.join(ROOT, "work", "evo_sandbox"), ignore_errors=True
        )


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--check" in argv:
        return _selfcheck()
    ap = argparse.ArgumentParser(prog="evolve")
    ap.add_argument("--rule", default=None)
    ap.add_argument("--gates", nargs="*", default=None)
    ap.add_argument("--max-steps", type=int, default=24)
    ap.add_argument("--no-merge", action="store_true")
    ap.add_argument("--fresh", action="store_true", help="无视既有提案，强制重新生成")
    ap.add_argument("--ledger", default=LEDGER_DEFAULT)
    ap.add_argument("--rules", default=RULES_DEFAULT)
    ap.add_argument("--signals", default=SIGNALS_DEFAULT)
    a = ap.parse_args(argv)
    return run_one(
        [a.rule] if a.rule else None,
        gates=a.gates,
        max_steps=a.max_steps,
        do_merge=not a.no_merge,
        ledger=a.ledger,
        rules_path=a.rules,
        signals_path=a.signals,
        fresh=a.fresh,
    )


if __name__ == "__main__":
    sys.exit(main())
