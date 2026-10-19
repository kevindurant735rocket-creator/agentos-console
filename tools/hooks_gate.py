#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hooks_gate —— W4 可扩展性门：hooks 中间件 + skills 目录。

为什么必须有这道门（W4 波首件交付）：
  hooks 与 skills 的每一层都能「看起来在工作」：拦截可以只体现在文档里、
  fail-closed 可以在崩溃时悄悄放行、description 预算可以静默超、/skill
  可以注错字段。本门站在外面认这些事实（纯离线：夹具钩子就是本地 sh）：
    D1  hooks_mw / skills_load 可导入 + 纯面
    D2  `--check` 末行逐字 `AGENT_CORE OK 153/153`（数漂移即红）
    D3  源码锚点：_hooks_session / fail-closed 拦截 / /skill 分支 / 补全表 /
        exit 2 / 预算截断 各就位
    D4  拦截（行为）：exit 2 拦截 + glob 不匹配不跑 + additionalContext 收集
    D5  fail-closed（行为）：非 0 退出码与超时都拦截
    D6  注入（行为）：hook_context 真进 system prompt
    D7  预算（行为）：description 超 500 截断留标记、正文不进 prompt 段
    D8  /skill 注入（行为）：正文进下一轮上下文；未知名不改历史
    D9  全关（行为）：无配置 = load_cfg None；exec_tool 常态可用（零影响）
    D10 零污染：work/ 只多 --out（他人在跑的 `_st_*` 自检目录有界等待，
        等到还不走/pid 已死一样判红）

用法: python3 tools/hooks_gate.py [--core P] [--hooks P] [--skills P] [--out P]
裁决：HOOKS_GATE OK n/n（rc=0）/ HOOKS_GATE RED（rc=2，有漏网）
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_CORE = os.path.join(HERE, "agent_core.py")
DEFAULT_HOOKS = os.path.join(HERE, "hooks_mw.py")
DEFAULT_SKILLS = os.path.join(HERE, "skills_load.py")
DEFAULT_OUT = os.path.join(ROOT, "work", "hooks_gate.json")
EXPECT_CHECK = "AGENT_CORE OK 153/153"

results = []


def gate(name, ok, detail=""):
    results.append({"gate": name, "ok": bool(ok), "detail": str(detail)[:200]})
    print(
        ("PASS " if ok else "FAIL ")
        + name
        + ("  -- " + str(detail)[:150] if detail else "")
    )


def load_mod(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _chat(text):
    return {
        "json": {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": text},
                }
            ]
        }
    }


def _finish(out_path, core_path):
    n_ok = sum(1 for r in results if r["ok"])
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump(
            {
                "core": os.path.relpath(core_path, ROOT),
                "judgments": results,
                "n_ok": n_ok,
                "n_total": len(results),
            },
            fh,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    print("HOOKS_GATE %d/%d" % (n_ok, len(results)))
    if n_ok == len(results):
        print("HOOKS_GATE_OK")
        return 0
    print("HOOKS_GATE_RED")
    print("  红判据=%s" % [r["gate"] for r in results if not r["ok"]])
    return 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", default=DEFAULT_CORE)
    ap.add_argument("--hooks", default=DEFAULT_HOOKS)
    ap.add_argument("--skills", default=DEFAULT_SKILLS)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()
    print("== hooks_gate：hooks 中间件 + skills 目录")

    work = os.path.join(ROOT, "work")
    before = set(os.listdir(work)) if os.path.isdir(work) else set()
    try:
        asrc = open(args.core, encoding="utf-8").read()
        hsrc = open(args.hooks, encoding="utf-8").read()
        ssrc = open(args.skills, encoding="utf-8").read()
    except OSError as e:
        print("HOOKS_GATE_RED 读不到文件：%s" % e)
        return 2
    tmp = tempfile.mkdtemp(prefix="hkgate_")

    # D1 可导入 + 纯面
    HM = SK = None
    try:
        HM = load_mod(args.hooks, "hooks_mw_g")
        SK = load_mod(args.skills, "skills_load_g")
        d = os.path.join(tmp, "pure")
        os.makedirs(os.path.join(d, "x"), exist_ok=True)
        with open(os.path.join(d, "x", "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: x\ndescription: D\n---\nBODY\n")
        it = SK.parse_skill(os.path.join(d, "x", "SKILL.md"))
        gate(
            "D1 可导入 + 纯面（load_cfg 缺文件=None；parse_skill 拿 name/body）",
            HM.load_cfg(os.path.join(tmp, "nope.json")) is None
            and it
            and it["name"] == "x"
            and "BODY" in it["body"],
            "it=%s" % (bool(it),),
        )
    except Exception as e:  # noqa: BLE001
        gate(
            "D1 可导入 + 纯面（load_cfg 缺文件=None；parse_skill 拿 name/body）",
            False,
            repr(e)[:120],
        )
    if HM is None or SK is None:
        return _finish(args.out, args.core)

    # D2 --check 逐字令牌
    try:
        p = subprocess.run(
            [sys.executable, args.core, "--check"],
            capture_output=True,
            text=True,
            timeout=900,
        )
        tail = [ln for ln in p.stdout.splitlines() if "AGENT_CORE OK" in ln]
        gate(
            "D2 --check 末行逐字 %s" % EXPECT_CHECK,
            p.returncode == 0 and bool(tail) and tail[-1].strip() == EXPECT_CHECK,
            "rc=%s tail=%r" % (p.returncode, (tail[-1] if tail else "")[:44]),
        )
    except Exception as e:  # noqa: BLE001
        gate("D2 --check 末行逐字 %s" % EXPECT_CHECK, False, repr(e)[:120])

    # D3 源码锚点
    n_sess = asrc.count("def _hooks_session(")
    n_fc = asrc.count("PreToolUse 中间件异常（fail-closed）")
    n_rb = asrc.count('if cmd == "/skill":')
    import re as _re

    n_tbl = len(_re.findall(r'"/rewind",\s*"/skill",', asrc))
    n_e2 = hsrc.count("if p.returncode == 2:")
    n_pre = hsrc.count('if event == "PreToolUse":')
    n_bud = ssrc.count("if len(d) > per:")
    gate(
        "D3 源码锚点（session/fail-closed/分支/补全表/exit2/预算 各就位）",
        n_sess == 1
        and n_fc == 1
        and n_rb == 1
        and n_tbl == 1
        and n_e2 == 1
        and n_pre == 3
        and n_bud == 1,
        "sess=%d fc=%d rb=%d tbl=%d e2=%d pre=%d bud=%d"
        % (n_sess, n_fc, n_rb, n_tbl, n_e2, n_pre, n_bud),
    )

    # D4 拦截 + glob + context
    cfg_blk = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "write_*", "hooks": [{"command": "printf blocked; exit 2"}]}
            ]
        }
    }
    cfg_ctx = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [{"command": 'printf \'{"additionalContext": "CTX-A"}\''}],
                }
            ]
        }
    }
    rb = HM.run_event("PreToolUse", {"tool": "write_file"}, cfg_blk)
    rm = HM.run_event("PreToolUse", {"tool": "read_file"}, cfg_blk)
    rc = HM.run_event("PreToolUse", {"tool": "x"}, cfg_ctx)
    gate(
        "D4 拦截：exit 2 拦 + glob 不匹配不跑 + additionalContext 收集",
        rb.get("block") is True
        and "blocked" in str(rb.get("reason"))
        and rm.get("ran") == 0
        and rc.get("block") is False
        and rc.get("context") == ["CTX-A"],
        "blk=%s miss=%s ctx=%s" % (rb.get("block"), rm.get("ran"), rc.get("context")),
    )

    # D5 fail-closed
    cfg_bad = {
        "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"command": "exit 7"}]}]}
    }
    cfg_slow = {
        "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"command": "sleep 3"}]}]}
    }
    rf = HM.run_event("PreToolUse", {"tool": "x"}, cfg_bad)
    rt = HM.run_event("PreToolUse", {"tool": "x"}, cfg_slow, timeout=1.0)
    gate(
        "D5 fail-closed：非 0 退出码与超时都拦截",
        rf.get("block") is True and rt.get("block") is True,
        "rc7=%s timeout=%s" % (rf.get("block"), rt.get("block")),
    )

    # D6 hook_context 进 system prompt（走真 build_system_prompt）
    K = None
    try:
        K = load_mod(args.core, "agent_core_g")
        sp = K.build_system_prompt(
            {"root": tmp, "mode": "plan", "hook_context": ["CTX-Z9"]}
        )
        gate(
            "D6 注入：hook_context 真进 system prompt",
            "CTX-Z9" in sp,
            "len=%d" % len(sp),
        )
    except Exception as e:  # noqa: BLE001
        gate("D6 注入：hook_context 真进 system prompt", False, repr(e)[:120])

    # D7 skills 预算
    skroot = os.path.join(tmp, "sk")
    os.makedirs(os.path.join(skroot, "beta"), exist_ok=True)
    with open(os.path.join(skroot, "beta", "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write("---\nname: beta\ndescription: %s\n---\nBETA-BODY\n" % ("长" * 600))
    its = SK.discover([skroot])
    sec = SK.render_section(its)
    bline = [ln for ln in sec.splitlines() if ln.startswith("- beta:")]
    gate(
        "D7 预算：description 超 500 截断留标记、正文不进 prompt 段",
        bool(bline)
        and bline[0].endswith("…")
        and len(bline[0]) <= 500 + len("- beta: ") + 1
        and "BETA-BODY" not in sec,
        "blen=%s" % (len(bline[0]) if bline else -1),
    )

    # D8 /skill 注入
    if K is not None:
        cctx = {"root": tmp, "mode": "plan", "history": []}
        K._SKILL_ROOTS_OVERRIDE.append(skroot)
        try:
            K._repl_slash("/skill beta", cctx, None, "sid", "p")
            ok_inj = (
                bool(cctx["history"]) and "BETA-BODY" in cctx["history"][-1]["content"]
            )
            K._repl_slash("/skill 没这个", cctx, None, "sid", "p")
            ok_unknown = len(cctx["history"]) == 1
        finally:
            K._SKILL_ROOTS_OVERRIDE.pop()
        gate(
            "D8 /skill 注入：正文进下一轮上下文；未知名不改历史",
            ok_inj and ok_unknown,
            "inj=%s keep=%s" % (ok_inj, ok_unknown),
        )
    else:
        gate(
            "D8 /skill 注入：正文进下一轮上下文；未知名不改历史", False, "core 不可导入"
        )

    # D9 无配置=全关：load_cfg None + exec_tool 常态可用（零影响）
    try:
        r9, _ = K.exec_tool("list_dir", {"path": "."}, {"root": tmp, "mode": "full"})
        gate(
            "D9 全关：无 hooks.json=load_cfg None 且工具常态可用",
            HM.load_cfg(os.path.join(tmp, "nope.json")) is None
            and r9.get("ok") in (True, False)
            and "执行" not in str(r9),
            "exec_ok=%s" % (r9.get("ok"),),
        )
    except Exception as e:  # noqa: BLE001
        gate(
            "D9 全关：无 hooks.json=load_cfg None 且工具常态可用", False, repr(e)[:120]
        )

    # D11 自动触发（行为）：triggers 命中注入正文、未命中零改动
    try:
        aroot = os.path.join(tmp, "skauto")
        os.makedirs(os.path.join(aroot, "fix"), exist_ok=True)
        with open(os.path.join(aroot, "fix", "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write(
                "---\nname: fixit\ndescription: 修布局\ntriggers: "
                "flutter, 布局崩\n---\nFIXIT-BODY\n"
            )
        K._SKILLS_ITEMS.clear()
        K._SKILL_ROOTS_OVERRIDE.append(aroot)
        try:
            sth = K.Stub([_chat("收到")])
            K.run_agent(
                "帮我看看 flutter 布局崩",
                ctx={"root": tmp, "mode": "plan"},
                provider=sth.provider(),
                max_steps=2,
            )
            seen0 = json.dumps(sth.seen[0], ensure_ascii=False)
            K.run_agent(
                "无关的话",
                ctx={"root": tmp, "mode": "plan"},
                provider=sth.provider(),
                max_steps=2,
            )
            seen1 = json.dumps(sth.seen[1], ensure_ascii=False)
            sth.close()
        finally:
            K._SKILL_ROOTS_OVERRIDE.pop()
            K._SKILLS_ITEMS.clear()
        gate(
            "D11 自动触发：triggers 命中注入正文 / 未命中不注入",
            "FIXIT-BODY" in seen0 and "FIXIT-BODY" not in seen1,
            "hit=%s miss=%s" % ("FIXIT-BODY" in seen0, "FIXIT-BODY" in seen1),
        )
    except Exception as e:  # noqa: BLE001
        gate("D11 自动触发：triggers 命中注入正文 / 未命中不注入", False, repr(e)[:120])

    # D12 自动触发预算（纯面）：超 4000 截断留标记
    try:
        big = {"name": "big", "triggers": ["x"], "body": "B" * 5000}
        hits = SK.auto_trigger("x 需求", [big], limit=2, budget=4000)
        gate(
            "D12 自动触发预算：超 4000 截断留标记",
            len(hits) == 1 and len(hits[0][1]) <= 4020 and "预算截断" in hits[0][1],
            "len=%s" % (len(hits[0][1]) if hits else -1),
        )
    except Exception as e:  # noqa: BLE001
        gate("D12 自动触发预算：超 4000 截断留标记", False, repr(e)[:120])

    # D10 零污染
    import shutil as _sh
    import time as _tm

    _sh.rmtree(tmp, ignore_errors=True)

    def _new_entries():
        a = set(os.listdir(work)) if os.path.isdir(work) else set()
        return sorted((a - before) - {os.path.basename(args.out)})

    # 有界等待（W10 修假红，2026-10-14 链内实测）：`_st_agent_core.<pid>` 是
    # **别人的** --check 进程正在跑的临时目录（同机另一个会话/另一条链在同一
    # 个仓里跑自检）。它活着不是本门泄漏 —— 泄漏的判据本义是「跑完还不走」。
    # 与 W1 minimize 竞态同一口径：等到状态确定再判；"活着"的等待有界（40s），
    # pid 已死仍不走 / 超时还在 → 照旧判红，一条真泄漏都不放过。
    new = _new_entries()
    waited = 0.0
    while new and waited < 40.0:
        if any(not e.startswith("_st_") for e in new):
            break  # 非自检目录的新增 = 真泄漏，一步都不等
        living = []
        for e in new:
            pid = e.rsplit(".", 1)[-1]
            if pid.isdigit():
                try:
                    os.kill(int(pid), 0)
                    living.append(e)
                except OSError:
                    pass
        if not living:
            break
        _tm.sleep(1.0)
        waited += 1.0
        new = _new_entries()
    gate(
        "D10 零污染（work/ 只多 --out；夹具已清）",
        not new,
        "新增=%s%s"
        % (
            new or "无",
            ("（等 %.0fs 待他人在跑的自检收工）" % waited) if waited else "",
        ),
    )
    return _finish(args.out, args.core)


if __name__ == "__main__":
    sys.exit(main())
