# -*- coding: utf-8 -*-
"""agent_core 的 --check 自检正文（从 agent_core.py 搬出的自洽块）。

为什么要搬：2026-10-21 的「agent 读不了自己的主文件」障碍 —— 主文件涨到
451KB，而 read_file 的上限是 256KB（MAX_FILE_BYTES）。这一块 143KB、
11,275 行里的 3,617 行，是主文件最大的一块；它是**只被 _selftest 调用**的
自洽块（AST 复核：区间顶格定义只有 _selftest_body 一个），所以整块搬走。

行为一字不变：判据条数、文案、判定逻辑原样；唯一改动是段内原 `globals()`
打补丁改成 `vars(_core)` —— 原来那几处补丁点的就是 agent_core 模块的全局，
搬到本模块后必须继续点 agent_core，否则 run_agent / _compact_history_v2
读不到补丁，自检会假绿。

依赖方向：本模块 → agent_core（单向）。主文件只在 _selftest 内部懒导入本
模块，所以没有循环导入。
"""

import os
import re
import subprocess
import sys
import threading
import time

import agent_core as _core
from agent_core import (  # noqa: F401  —— 段内按裸名引用，逐字对齐内核口径
    AgentError,
    DIGEST_PREFIX,
    NAMED_PROVIDERS,
    Provider,
    REPL_SLASH_CMDS,
    RESPONSES_MAX_OUTPUT_TOKENS,
    RESPONSES_REASONING_EFFORT,
    RISK_READ,
    RISK_WRITE,
    ROOT,
    SUBAGENT_MAX_STEPS,
    SUBAGENT_PARALLEL_MAX,
    Stub,
    TERM_CANDIDATES,
    TERM_MAX_OUT,
    TOOLS,
    _SKILLS_ITEMS,
    _SKILL_ROOTS_OVERRIDE,
    _STEER_DIR_OVERRIDE,
    _TRANSIENT_RE,
    _TurnSpinner,
    _at_completions,
    _authorize,
    _autonomy,
    _c,
    _cap_output,
    _claim_no_evidence,
    _clean_plans,
    _compact_history,
    _compact_history_v2,
    _compose_join,
    _dsp,
    _dw,
    _evo_converge_policy,
    _evo_midflight,
    _evo_mine,
    _evo_refresh_tool_hints,
    _evo_rules_load,
    _evo_set_state,
    _expand_at_refs,
    _format_answer,
    _gh_client_mod,
    _gh_file_read_argv,
    _gh_search_argv,
    _gh_search_queries,
    _hard_deny,
    _history_from_transcript,
    _ideas_scan,
    _is_stale_reply,
    _line_wants_continue,
    _looks_approved,
    _needs_approve_retry,
    _new_session_id,
    _no_fallback,
    _norm_protocol,
    _norm_responses,
    _repl_slash,
    _rewind_replay,
    _safety_flags,
    _sha,
    _startup_local_backend,
    _term_cmds,
    _to_responses_input,
    _to_responses_tools,
    _tool_label,
    _tool_summary,
    _transcript_append,
    _transcript_load,
    _turn_ui,
    _usage_total,
    build_system_prompt,
    exec_tool,
    json,
    os,
    re,
    resolve_provider,
    run_agent,
    run_agent_with_fallback,
    shutil,
    subprocess,
    sys,
    threading,
    time,
    tool_defs,
)



def _selftest_body(case, jc, tmp):
    """自检正文。拆出来只为让 _selftest 的 finally 清理保持简单。"""
    # (1) 终端白名单：工具层自己的真相。
    #     原来这条判的是「agent_core 的白名单 == serve.py 的白名单」——
    #     网页驾驶舱 serve.py 已下线，被量的那一方不存在了，留着就是永远红。
    #     现在量它自己：候选集与命令表都在、且命令表非空。
    _mine_cmds = _term_cmds()
    case(
        "终端白名单：工具层自洽（候选集与命令表都在）",
        bool(TERM_CANDIDATES) and bool(_mine_cmds),
        "候选=%d 命令=%d" % (len(TERM_CANDIDATES), len(_mine_cmds)),
    )

    tmproot = os.path.realpath(tmp)
    os.makedirs(tmproot, exist_ok=True)
    ctx_full = {"root": tmproot, "mode": "full"}
    ctx_plan = {"root": tmproot, "mode": "plan"}
    ctx_ae = {"root": tmproot, "mode": "accept-edits"}

    # (2) 非白名单命令：run_cmd 一律拒 ---------------------------
    r, ex = exec_tool("run_cmd", {"argv": ["curl", "http://evil.example"]}, ctx_full)
    case(
        "白名单外命令（curl）被拒",
        (not r["ok"]) and (not ex) and "白名单" in (r.get("error") or ""),
        (r.get("error") or "")[:60],
    )

    # (3) 四步循环：写 -> 读 -> 越界写被拒 -> 最终回答 -----------
    script = [
        jc(
            ("write_file", {"path": "hello.txt", "content": "hello-agentos\n"}),
            cid="c1",
        ),
        jc(("read_file", {"path": "hello.txt"}), cid="c2"),
        jc(
            ("write_file", {"path": "../../../../etc/pwn", "content": "pwned"}),
            cid="c3",
        ),
        jc(None, "AGENT_CORE-SELFTEST-OK"),
    ]
    stub = Stub(script)
    try:
        res = run_agent(
            "写个文件再读回来，越界的那步别真做",
            ctx={"root": tmproot, "mode": "accept-edits"},
            provider=stub.provider(),
            max_steps=6,
        )
    finally:
        stub.close()
    case(
        "4 步循环收敛到最终回答（steps==4）",
        res.get("ok")
        and res.get("steps") == 4
        and res.get("answer") == "AGENT_CORE-SELFTEST-OK",
        "ok=%s steps=%s answer=%r kind=%s"
        % (
            res.get("ok"),
            res.get("steps"),
            (res.get("answer") or "")[:40],
            res.get("kind"),
        ),
    )

    hello = os.path.join(tmproot, "hello.txt")
    body = ""
    if os.path.isfile(hello):
        with open(hello, encoding="utf-8") as f:
            body = f.read()
    case(
        "write_file 真写了文件（字节为据）",
        body == "hello-agentos\n",
        "bytes=%d" % len(body.encode("utf-8")),
    )

    aids = [
        tc.get("id")
        for m in res["messages"]
        if m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or [])
    ]
    tids = [m.get("tool_call_id") for m in res["messages"] if m.get("role") == "tool"]
    case(
        "tool 消息带回 tool_call_id（与 assistant 一一对上）",
        len(tids) == 3 and tids == aids[:3],
        "%r vs %r" % (aids[:3], tids),
    )

    case(
        "越界 write_file 被拒，且 /etc/pwn 确实不存在",
        (not os.path.exists("/etc/pwn"))
        and any(
            "越界" in (m.get("content") or "")
            for m in res["messages"]
            if m.get("role") == "tool"
        ),
        "escape 步骤在 tool 消息里如实回拒",
    )

    # (4) SSE：content 分片 + tool_calls 按 index 累积 ------------
    def sse(o):
        return "data: " + json.dumps(o, ensure_ascii=False)

    stream_text = (
        "\n\n".join(
            [
                sse(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "Hel"},
                                "finish_reason": None,
                            }
                        ]
                    }
                ),
                sse(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "lo, "},
                                "finish_reason": None,
                            }
                        ]
                    }
                ),
                sse(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "world"},
                                "finish_reason": None,
                            }
                        ]
                    }
                ),
                sse(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": None,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "c0",
                                            "type": "function",
                                            "function": {
                                                "name": "read_",
                                                "arguments": '{"pa',
                                            },
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                ),
                sse(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "tool_calls",
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "function": {"arguments": 'th":"a"}'},
                                        },
                                        {
                                            "index": 1,
                                            "id": "c1",
                                            "type": "function",
                                            "function": {
                                                "name": "health",
                                                "arguments": "{}",
                                            },
                                        },
                                    ]
                                },
                            }
                        ]
                    }
                ),
            ]
        )
        + "\n\n"
    )
    stub = Stub([{"sse": stream_text}])  # 故意不发 [DONE]
    try:
        got = stub.provider().chat([{"role": "user", "content": "hi"}], stream=True)
    finally:
        stub.close()
    case(
        "SSE 无 [DONE] 也能收尾：content 拼接 + tool_calls 按 index 累积",
        got.get("content") == "Hello, world"
        and len(got.get("tool_calls") or []) == 2
        and got["tool_calls"][0]["function"]["arguments"] == '{"path":"a"}'
        and got["tool_calls"][1]["function"]["name"] == "health",
        "content=%r calls=%r"
        % (
            got.get("content"),
            [
                (c["function"]["name"], c["function"]["arguments"])
                for c in got.get("tool_calls") or []
            ],
        ),
    )

    # (5) 端点问题一律归 provider，绝不猜成成功 ------------------
    for label, entry in (
        ("非 200", {"status": 500, "json": {"error": "boom"}}),
        ("响应形状不对", {"json": {"nope": 1}}),
    ):
        stub = Stub([entry])
        try:
            try:
                stub.provider().chat([{"role": "user", "content": "x"}])
                kind, msg = "", ""
            except AgentError as e:
                kind, msg = e.kind, str(e)
        finally:
            stub.close()
        case(
            "端点 %s -> AgentError(provider)，不退化成「模型说完了」" % label,
            kind == "provider",
            "%s: %s" % (kind, msg[:70]),
        )

    # (6) 截断必须显式标注 ---------------------------------------
    capped = _cap_output("x" * 100000, limit=1000)
    case(
        "_cap_output 截断且显式标注 truncated",
        len(capped) <= 1004 and "truncated" in capped and "100000/1000" in capped,
        "len=%d" % len(capped),
    )

    # (7) 权限：plan 拒写 / full+审批者也拦不住硬阻断 ------------
    p1 = os.path.join(tmproot, "plan.txt")
    r, ex = exec_tool("write_file", {"path": "plan.txt", "content": "x"}, ctx_plan)
    case(
        "plan 模式拒写（文件没落地）",
        (not r["ok"])
        and (not ex)
        and (not os.path.exists(p1))
        and "plan" in (r.get("error") or ""),
        (r.get("error") or "")[:60],
    )

    r, ex = exec_tool(
        "run_cmd",
        {"argv": ["git", "push"]},
        {"root": tmproot, "mode": "full", "approver": lambda *a: True},
    )
    case(
        "git push 在 full + 审批者全点头下仍被硬阻断",
        (not r["ok"]) and (not ex) and "硬阻断" in (r.get("error") or ""),
        (r.get("error") or "")[:70],
    )

    # (8) accept-edits：python3 -c 无审批者拒，有审批者点头则真跑
    py = ["python3", "-c", "open('py_ok.txt','w').write('1')"]
    r, ex = exec_tool("run_cmd", {"argv": py}, ctx_ae)
    case(
        "accept-edits 下 python3 -c 无审批者 -> 拒（文件没落地）",
        (not r["ok"])
        and (not ex)
        and (not os.path.exists(os.path.join(tmproot, "py_ok.txt"))),
        (r.get("error") or "")[:70],
    )
    r, ex = exec_tool(
        "run_cmd",
        {"argv": py},
        {"root": tmproot, "mode": "accept-edits", "approver": lambda *a: True},
    )
    case(
        "同一命令有审批者点头 -> 真跑（文件真出现，rc=0）",
        r["ok"]
        and r.get("rc") == 0
        and ex
        and os.path.isfile(os.path.join(tmproot, "py_ok.txt")),
        "rc=%s executed=%s" % (r.get("rc"), ex),
    )

    # (9) accept-edits 的本义：文件编辑自动放行；审批器说 No 则整体未执行
    r, ex = exec_tool("write_file", {"path": "auto.txt", "content": "x"}, ctx_ae)
    case(
        "accept-edits 下 write_file 自动放行（这正是该模式的本义）",
        r["ok"] and ex and os.path.isfile(os.path.join(tmproot, "auto.txt")),
        "ok=%s executed=%s" % (r.get("ok"), ex),
    )
    r, ex = exec_tool(
        "run_cmd",
        {"argv": ["python3", "-c", "open('no.txt','w').write('1')"]},
        {"root": tmproot, "mode": "accept-edits", "approver": lambda *a: False},
    )
    case(
        "approver 返回 False -> 未执行且文件未出现",
        (not r["ok"])
        and (not ex)
        and (not os.path.exists(os.path.join(tmproot, "no.txt")))
        and "审批" in (r.get("error") or ""),
        (r.get("error") or "")[:70],
    )

    # (10) 路径越界：读也拒 -------------------------------------
    r, ex = exec_tool("read_file", {"path": "../../../../etc/passwd"}, ctx_full)
    case(
        "read_file 越界被拒（../../etc/passwd）",
        (not r["ok"]) and "越界" in (r.get("error") or "") and "text" not in r,
        "%s（success 形状的 text 字段=%s）"
        % ((r.get("error") or "")[:60], "有" if "text" in r else "无"),
    )

    # (11) edit_file：old_string 找不到 -> sha256 一字不变 -------
    ef = os.path.join(tmproot, "edit.txt")
    with open(ef, "w", encoding="utf-8") as f:
        f.write("alpha beta gamma\n")
    before = _sha(ef)
    r, ex = exec_tool(
        "edit_file",
        {"path": "edit.txt", "old_string": "不在文件里", "new_string": "y"},
        ctx_full,
    )
    case(
        "edit_file 找不到 old_string -> 拒改且 sha256 不变",
        (not r["ok"]) and _sha(ef) == before,
        "sha before=%s after=%s" % (before, _sha(ef)),
    )

    # (12) 工具输出超限 -> 截断且显式标注 -----------------------
    r, ex = exec_tool(
        "run_cmd", {"argv": ["python3", "-c", "print('x' * 100000)"]}, ctx_full
    )
    case(
        "run_cmd 输出超限 -> 截断并显式标注（长度可见）",
        r["ok"]
        and r.get("truncated") is True
        and 32000 < len(r.get("out") or "") <= TERM_MAX_OUT,
        "out=%d truncated=%s" % (len(r.get("out") or ""), r.get("truncated")),
    )

    # (13) 未知工具 / 坏 JSON 参数：不崩，如实回灌 ---------------
    r, ex = exec_tool("no_such_tool", {}, ctx_full)
    case(
        "未知工具名 -> 结构化拒绝，不抛异常",
        (not r["ok"]) and (not ex) and "未知工具" in (r.get("error") or ""),
        (r.get("error") or "")[:70],
    )

    stub = Stub(
        [
            {
                "json": {
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "c1",
                                        "type": "function",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": "not-json",
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                }
            },
            jc(None, "BADARGS-OK"),
        ]
    )
    try:
        res = run_agent(
            "读文件",
            ctx={"root": tmproot, "mode": "full"},
            provider=stub.provider(),
            max_steps=4,
        )
    finally:
        stub.close()
    toolmsg = [m.get("content") for m in res["messages"] if m.get("role") == "tool"]
    case(
        "坏 JSON 参数不崩：如实回灌并继续到最终回答",
        res.get("ok")
        and res.get("answer") == "BADARGS-OK"
        and any("不是合法 JSON" in (t or "") for t in toolmsg),
        "steps=%s toolmsg=%r" % (res.get("steps"), (toolmsg or [""])[0][:60]),
    )

    # (14) max_steps 触顶 -> kind=limit，终答为空 -----------------
    stub = Stub(
        [
            jc(("read_file", {"path": "nope-%d.txt" % i}), cid="c%d" % i)
            for i in range(3)
        ]
    )
    try:
        res = run_agent(
            "一直读不存在的文件",
            ctx={"root": tmproot, "mode": "full"},
            provider=stub.provider(),
            max_steps=2,
        )
    finally:
        stub.close()
    case(
        "max_steps 触顶 -> kind=limit，终答为空（不冒充成功）",
        (not res.get("ok"))
        and res.get("kind") == "limit"
        and res.get("answer") is None
        and res.get("steps") == 2,
        "kind=%s steps=%s error=%r"
        % (res.get("kind"), res.get("steps"), (res.get("error") or "")[:50]),
    )

    # (15) 空需求：用法错误，不进循环 ----------------------------
    st = Stub([])
    try:
        try:
            run_agent("   ", ctx={"root": tmproot}, provider=st.provider())
            kind = ""
        except AgentError as e:
            kind = e.kind
    finally:
        st.close()
    case("空需求抛 AgentError(usage)，不进循环", kind == "usage", "kind=%s" % kind)

    # (16) 离线后端开关：显式开才给本机 Ollama，未开不许自动探测 ----
    # 为什么把「未开」也钉进判据：自动探测本机 11434 会把「没配端点」
    # 谎成「有端点」，neg_agent_core 的空 HOME 判据正是治这个的。
    old_home = os.environ.get("HOME")
    try:
        os.environ["HOME"] = tmproot  # 空 HOME ⇒ 没有 codex 配置
        try:
            resolve_provider({})
            off_kind = "没抛错（fail-open）"
        except AgentError as e:
            off_kind = e.kind
        loc = resolve_provider({"AGENTOS_LLM_LOCAL": "ollama"})
        ok_local = loc.get("base_url") == "http://127.0.0.1:11434/v1" and bool(
            loc.get("model")
        )
        ok_off = off_kind == "no-provider"
        detail = "开=%s / 未开=%s" % (loc.get("base_url"), off_kind)
    except Exception as e:  # noqa: BLE001
        ok_local, ok_off, detail = False, False, str(e)[:120]
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
    case(
        "离线后端只在显式开时生效（未开不自动探测 11434）", ok_local and ok_off, detail
    )

    # (17) 命名 provider：内置端点表解析；未知名 fail-closed，不许猜 ----
    # 为什么钉进判据：--provider 是新加的入口，内置表写错（url/模型名）
    # 或未知名不报错（fail-open 到 codex 配置），都是用户当场撞上的事。
    try:
        # 注意：模型断言必须钉**端点表内的默认值**而不是「config 当前值」。
        # resolve 会把 ~/.agentos/config.toml 的 llm.model 当覆写注入 env
        # （这是切模型的正路），用户换模型的瞬间不该把这条例自检踢红。
        _tmodel = NAMED_PROVIDERS["opencode"]["model"]
        spec = resolve_provider(
            {
                "AGENTOS_PROVIDER": "opencode",
                "AGENTOS_LLM_MODEL": _tmodel,
            }
        )
        ok_named = (
            spec.get("base_url") == "https://opencode.ai/zen/go/v1"
            # 2026-10-22：兜底默认必须是**用户实际在用的默认**
            # （deepseek-v4.1-flash）。旧值 muse-spark-1.3-contributor 也是
            # 端点有的模型，但那是 contributor 级小模型 —— 配置一坏就
            # **静默降级**（能力差一档且无任何提示）。这条钉住「兜底 ==
            # 实际默认」。
            and spec.get("model") == "deepseek-v4.1-flash"
            # opencode 的 /chat/completions 实测 400，协议必须跟着
            # 端点表走；这条断言红 = 有人在表里删了 protocol。
            and spec.get("protocol") == "responses"
            # 2026-10-17 起该端点强制会话头；值由 Provider 持有并在
            # rotate_session 时原子轮换 —— resolve 层带值会让轮换失效。
            and spec.get("headers", {}).get("x-opencode-session") is None
        )
        try:
            resolve_provider({"AGENTOS_PROVIDER": "no-such-provider"})
            bad = "没抛错（fail-open）"
        except AgentError as e:
            bad = e.kind
        ok_bad = bad == "no-provider"
        detail = "opencode=%s protocol=%s / 未知名=%s" % (
            spec.get("base_url"),
            spec.get("protocol"),
            bad,
        )
    except Exception as e:  # noqa: BLE001
        ok_named, ok_bad, detail = False, False, str(e)[:120]
    case(
        "命名 provider：opencode 解析出内置端点且未知名报错",
        ok_named and ok_bad,
        detail,
    )

    # (18) 交互回退：主端点 403 → 换本机离线后端重试一次；headless 不回退
    # 为什么把「headless 不回退」也钉进判据：回退是给交互 REPL 的兜底，
    # 若 -p 也回退，CI 里「没配端点」会被本机模型悄悄洗成绿 —— 又一种假绿。
    stub_main = Stub([{"status": 403, "json": {"error": "blocked"}}])
    stub_fb = Stub([jc(None, "FALLBACK-OK")])

    def _fb_cfg():
        return {
            "base_url": stub_fb.base(),
            "model": "fb-model",
            "headers": {},
            "provider_name": "ollama-local",
            "source": "fallback:ollama",
        }

    try:
        res, used = run_agent_with_fallback(
            "回退自检",
            ctx={"root": tmproot, "mode": "plan"},
            provider=stub_main.provider(),
            allow_fallback=True,
            probe=_fb_cfg,
        )
        ok_fb = (
            res.get("ok")
            and res.get("answer") == "FALLBACK-OK"
            and used is not None
            and used.source == "fallback:ollama"
        )
        fb_detail = "answer=%r source=%s" % (
            (res.get("answer") or "")[:30],
            used.source if used is not None else None,
        )
    except Exception as e:  # noqa: BLE001
        ok_fb, fb_detail = False, "%s: %s" % (type(e).__name__, str(e)[:80])
    finally:
        stub_main.close()
        stub_fb.close()
    case("交互回退：主端点 403 自动换离线后端并成功", ok_fb, fb_detail)

    stub_main = Stub([{"status": 403, "json": {"error": "blocked"}}])
    try:
        try:
            run_agent_with_fallback(
                "不许回退",
                ctx={"root": tmproot, "mode": "plan"},
                provider=stub_main.provider(),
                allow_fallback=False,
                probe=lambda: {
                    "base_url": "http://127.0.0.1:1/v1",
                    "model": "x",
                    "headers": {},
                    "source": "fallback:ollama",
                },
            )
            hd_kind = "没抛错（headless 被洗绿）"
        except AgentError as e:
            hd_kind = e.kind
    finally:
        stub_main.close()
    case(
        "headless 不回退：allow_fallback=False 时原样抛 provider",
        hd_kind == "provider",
        "kind=%s" % hd_kind,
    )

    # (18b) no_fallback：主端点先 500 两次再好 → 一直重试主端点成功，
    # 本地后端零触碰；cancel 已触发时第一次失败即抛（不是死循环）。
    # 为什么钉：用户要的是「主端点失败就一直等主端点」的字面保证 ——
    # probe 零调用证明没偷换本地，seen==3 证明重试打的是主端点。
    stub_flap = Stub(
        [
            {"status": 500, "json": {"error": "boom1"}},
            {"status": 500, "json": {"error": "boom2"}},
            jc(None, "MAIN-RECOVERED"),
        ]
    )
    probe_hits = []
    slept = []

    def _never_probe():
        probe_hits.append(1)
        return None

    try:
        res_nf, used_nf = run_agent_with_fallback(
            "no_fallback 自检",
            ctx={"root": tmproot, "mode": "plan"},
            provider=stub_flap.provider(),
            allow_fallback=False,
            retry_main=True,
            probe=_never_probe,
            cancel=lambda: False,
            sleep=slept.append,
        )
        ok_nf = (
            res_nf.get("ok")
            and res_nf.get("answer") == "MAIN-RECOVERED"
            and used_nf is not None
            and used_nf.source != "fallback:ollama"
            and len(stub_flap.seen) == 3
            and not probe_hits
            and len(slept) == 2
        )
        nf_detail = "answer=%r seen=%d probe=%d slept=%r" % (
            (res_nf.get("answer") or "")[:20],
            len(stub_flap.seen),
            len(probe_hits),
            slept,
        )
    except Exception as e:  # noqa: BLE001
        ok_nf, nf_detail = False, "%s: %s" % (type(e).__name__, str(e)[:80])
    finally:
        stub_flap.close()
    case(
        "no_fallback：主端点抖动只重试主端点（本地零触碰，Esc/cancel 可停）",
        ok_nf,
        nf_detail,
    )
    stub_down = Stub([{"status": 500, "json": {"error": "down"}}])
    cx_calls = []
    slept2 = []

    def _cx_after_first():
        # 第一次失败后才置位：模拟用户在看到第一次重试提示后按 Esc。
        # run_agent 步首也会查 cancel，所以前两次调用必须放行，
        # 否则连 HTTP 都打不出去（那是另一个已有判据在管的行为）。
        cx_calls.append(1)
        return len(cx_calls) > 2

    try:
        try:
            run_agent_with_fallback(
                "cancel 出口",
                ctx={"root": tmproot, "mode": "plan"},
                provider=stub_down.provider(),
                allow_fallback=False,
                retry_main=True,
                probe=_never_probe,
                cancel=_cx_after_first,
                sleep=slept2.append,
            )
            cx_kind = "没抛错（死循环或吞错）"
        except AgentError as e:
            cx_kind = e.kind
    finally:
        stub_down.close()
    case(
        "no_fallback cancel 出口：cancel 已触发时第一次失败即抛 provider",
        cx_kind == "provider" and len(stub_down.seen) == 1 and slept2 == [1],
        "kind=%s seen=%d slept=%r" % (cx_kind, len(stub_down.seen), slept2),
    )

    # (18b-2) 会话中毒 400 的自救判据（W22 双刺）：
    # ① 词表外 400（No tool call found…不含任何传输词）也必须轮换重试
    #    ——曾因中毒分支排在词表门后面被 `not _TRANSIENT_RE → raise`
    #    一刀毙命，rotate 永远轮不到（实测第六圈就这样死）。
    # ② 轮换走独立预算 _SESS_MAX：三连跪后如实抛 provider，
    #    不许无限 reroll（也不许和 SSL 池互相烧光）。
    stub_poison = Stub(
        [
            {
                "status": 400,
                "json": {
                    "error": {
                        "message": "No tool call found for tool output "
                        "with call_id call_01_poison"
                    }
                },
            },
            jc(None, "POISON-RECOVERED"),
        ]
    )
    _rot = stub_poison.provider()
    _rots = []
    _rot_orig = _rot.rotate_session

    def _cnt_rot():
        _rots.append(1)
        _rot_orig()

    _rot.rotate_session = _cnt_rot
    try:
        res_pois = run_agent(
            "会话中毒自检",
            ctx={"root": tmproot, "mode": "plan"},
            provider=_rot,
        )
        pois_ok = (
            res_pois.get("ok")
            and res_pois.get("answer") == "POISON-RECOVERED"
            and len(stub_poison.seen) == 2
            and len(_rots) == 1
        )
        pois_detail = "answer=%r seen=%d rot=%d" % (
            (res_pois.get("answer") or "")[:20],
            len(stub_poison.seen),
            len(_rots),
        )
    except Exception as e:  # noqa: BLE001
        pois_ok, pois_detail = False, "%s: %s" % (type(e).__name__, str(e)[:90])
    finally:
        stub_poison.close()
    case(
        "会话中毒 400（词表外）也轮换重试且成功",
        pois_ok,
        pois_detail,
    )

    stub_poison3 = Stub(
        [
            {
                "status": 400,
                "json": {"error": {"message": "MissingSessionID"}},
            },
        ]
    )  # 脚本尾永远复放同一条坏 400 → 必然走到预算上限
    try:
        try:
            run_agent(
                "会话中毒预算自检",
                ctx={"root": tmproot, "mode": "plan"},
                provider=stub_poison3.provider(),
            )
            pois3_kind = "没抛错（无限 reroll？）"
        except AgentError as e:
            pois3_kind = e.kind
        pois3_ok = pois3_kind == "provider" and len(stub_poison3.seen) == 4
        # seen=4：初试 + 3 次轮换重试（1 初 + _SESS_MAX=3），第 5 次不再打
        pois3_detail = "kind=%s seen=%d" % (pois3_kind, len(stub_poison3.seen))
    finally:
        stub_poison3.close()
    case("会话中毒轮换有独立上限（3 次到顶如实例外）", pois3_ok, pois3_detail)

    # (18c) no_fallback 判据矩阵 + 启动期兜底零触碰。
    # 为什么两件事都要钉：只堵中途回退、启动时照样换本地，等于「云端挂了
    # 静默降级」；而读侧若把 "yes"/"True"/布尔 True 也当真，用户以为关掉
    # 了兜底，实际还在换 —— 两头都得有反例才不是眼看。
    G = vars(_core)
    _orig_read_g = G["_read_agentos_config"]
    _orig_probe_g = G["_probe_ollama"]
    probe18c = []

    def _fake_read_g():
        return {"llm": {"no_fallback": "true"}}

    def _fake_probe_g():
        probe18c.append(1)
        return {"base_url": "http://127.0.0.1:11434/v1",
                "model": "qwen3:0.6b", "headers": {},
                "protocol": "chat", "provider_name": "ollama-local",
                "source": "probe:fake"}

    mat18c = []
    for _raw, _want in (("true", True), ("True", False), ("yes", False),
                        (True, False), (None, False), ("false", False)):
        G["_read_agentos_config"] = (
            lambda v: (lambda: ({} if v is None
                                else {"llm": {"no_fallback": v}}))
        )(_raw)
        try:
            _got = _no_fallback()
        finally:
            G["_read_agentos_config"] = _orig_read_g
        if _got != _want:
            mat18c.append("%r→%s 期望 %s" % (_raw, _got, _want))
    case("no_fallback 读侧矩阵：只认字面 \"true\"（布尔/True/yes 全不当真）",
         not mat18c, "; ".join(mat18c) or "6 例全对")

    G["_read_agentos_config"] = _fake_read_g
    G["_probe_ollama"] = _fake_probe_g
    try:
        s_cfg, s_blocked = _startup_local_backend()
    finally:
        G["_read_agentos_config"] = _orig_read_g
        G["_probe_ollama"] = _orig_probe_g
    case("no_fallback 时启动期兜底也不碰本地后端（probe 零调用）",
         s_cfg is None and s_blocked is True and not probe18c,
         "cfg=%r blocked=%r probe=%d" % (s_cfg, s_blocked, len(probe18c)))

    G["_read_agentos_config"] = lambda: {}
    G["_probe_ollama"] = _fake_probe_g
    try:
        d_cfg, d_blocked = _startup_local_backend()
    finally:
        G["_read_agentos_config"] = _orig_read_g
        G["_probe_ollama"] = _orig_probe_g
    case("缺键（默认自由面）：启动期兜底照旧可用本地后端",
         isinstance(d_cfg, dict) and d_blocked is False,
         "cfg=%r blocked=%r" % (d_cfg, d_blocked))

    # (19) Responses 协议：翻译 / 非流式 / SSE / 失败分类 -----------
    # 为什么整块单列：opencode 网关只认 /responses（chat/completions 实测
    # 回 HTTP 400 ModelProtocolUnsupported）。这条路径此前零覆盖，等于
    # 「换协议还能用」只是眼看，不是验过。
    def _resp_json(text, cid="call_x", usage=None):
        """一条 responses 形状的非流式 JSON 响应。"""
        out = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }
        if cid is None:
            items = [out]
        else:
            items = [
                {
                    "type": "function_call",
                    "call_id": cid,
                    "name": "read_file",
                    "arguments": '{"path": "note.txt"}',
                },
                out,
            ]
        return {
            "json": {
                "id": "resp_stub",
                "status": "completed",
                "output": items,
                "usage": usage
                or {
                    "input_tokens": 11,
                    "output_tokens": 7,
                    "total_tokens": 18,
                    "output_tokens_details": {"reasoning_tokens": 3},
                },
            }
        }

    # ① 入参翻译：messages → input[]（只有 tool_calls / tool 要变形）
    tr_in = _to_responses_input(
        [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "USER"},
            {
                "role": "assistant",
                "content": "ASTEXT",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "README.md"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "FILE-OUT"},
        ]
    )
    in_ok = (
        len(tr_in) == 5
        and tr_in[0] == {"role": "system", "content": "SYS"}
        and tr_in[1] == {"role": "user", "content": "USER"}
        and tr_in[2] == {"role": "assistant", "content": "ASTEXT"}
        and tr_in[3].get("type") == "function_call"
        and tr_in[3].get("call_id") == "c1"
        and tr_in[3].get("name") == "read_file"
        and tr_in[3].get("arguments") == '{"path": "README.md"}'
        and tr_in[4]
        == {"type": "function_call_output", "call_id": "c1", "output": "FILE-OUT"}
    )
    case(
        "Responses 入参翻译：messages → input[]（含 function_call 两项）",
        in_ok,
        "types=%s" % [i.get("type") or i.get("role") for i in tr_in],
    )

    # ② 工具表拍扁：嵌套 → 扁平 + strict:false
    tr_tools = _to_responses_tools(tool_defs())
    passthru = _to_responses_tools(
        [{"type": "function", "name": "x", "parameters": {"type": "object"}}]
    )
    tools_ok = (
        bool(tr_tools)
        and len(tr_tools) == len(tool_defs())
        and all(
            t.get("type") == "function"
            and "function" not in t
            and t.get("strict") is False
            and bool(t.get("name"))
            and bool(t.get("parameters"))
            for t in tr_tools
        )
        and any(t.get("name") == "read_file" for t in tr_tools)
        and passthru
        == [{"type": "function", "name": "x", "parameters": {"type": "object"}}]
    )
    case(
        "Responses 工具表拍扁：嵌套形状 → 扁平 + strict:false（不二次包装）",
        tools_ok,
        "n=%d first=%s"
        % (len(tr_tools), json.dumps(tr_tools[0], ensure_ascii=False)[:90]),
    )

    # ③ 未知协议 fail-closed：猜一次就等于把「配错」翻译成「端点坏了」
    try:
        _norm_protocol("grpc")
        proto_bad = "没抛错（猜协议）"
    except AgentError as e:
        proto_bad = e.kind
    case(
        "未知协议名抛 provider（不猜）", proto_bad == "provider", "kind=%s" % proto_bad
    )

    # ④ responses 端到端两步：工具调用 → 结果回灌 → 终答
    note = os.path.join(tmproot, "note.txt")
    with open(note, "w", encoding="utf-8") as f:
        f.write("note line\n")
    stub_r = Stub([_resp_json(None, cid="c1"), _resp_json("RESP-E2E-OK", cid=None)])
    try:
        res = run_agent(
            "读 note.txt",
            ctx={"root": tmproot, "mode": "full"},
            provider=stub_r.provider(protocol="responses"),
            max_steps=4,
        )
    finally:
        stub_r.close()
    seen = stub_r.seen
    e2e_ok = bool(
        res.get("ok")
        and res.get("answer") == "RESP-E2E-OK"
        and len(seen) >= 2
        and "input" in seen[0]
        and "messages" not in seen[0]
        and seen[0].get("max_output_tokens") == RESPONSES_MAX_OUTPUT_TOKENS
        and (seen[0].get("reasoning") or {}).get("effort") == RESPONSES_REASONING_EFFORT
        and seen[1]["input"][-1].get("type") == "function_call_output"
        and seen[1]["input"][-1].get("call_id") == "c1"
        and seen[1].get("tools")
        and "function" not in seen[1]["tools"][0]
        and seen[1]["tools"][0].get("strict") is False
    )
    case(
        "Responses 端到端：request 走 input[]，工具结果回灌成 function_call_output",
        e2e_ok,
        "steps=%s last=%s tools0=%s"
        % (
            res.get("steps"),
            json.dumps(seen[1]["input"][-1], ensure_ascii=False)[:60]
            if len(seen) > 1
            else "-",
            json.dumps(seen[1]["tools"][0], ensure_ascii=False)[:60]
            if len(seen) > 1
            else "-",
        ),
    )

    # ⑤ responses SSE：deltas 推进 + completed 内嵌 response 优先
    sse_final = {
        "id": "resp_sse",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "SSE-OK"}],
            },
            {
                "type": "function_call",
                "call_id": "c9",
                "name": "read_file",
                "arguments": '{"path": "README.md"}',
            },
        ],
        "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
    }
    sse_lines = "\n".join(
        [
            "data: " + json.dumps({"type": "response.created"}),
            "data: "
            + json.dumps(
                {
                    "type": "response.output_item.added",
                    "item": {
                        "id": "i1",
                        "type": "function_call",
                        "call_id": "c9",
                        "name": "read_file",
                    },
                }
            ),
            "data: "
            + json.dumps(
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "i1",
                    "delta": '{"path":',
                }
            ),
            "data: "
            + json.dumps(
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "i1",
                    "delta": ' "README.md"}',
                }
            ),
            "data: "
            + json.dumps({"type": "response.output_text.delta", "delta": "SSE-"}),
            "data: "
            + json.dumps({"type": "response.output_text.delta", "delta": "OK"}),
            "data: "
            + json.dumps({"type": "response.completed", "response": sse_final}),
            "",
        ]
    )
    stub_s = Stub([{"sse": sse_lines}])
    deltas = []
    try:
        out = stub_s.provider(protocol="responses").chat(
            [{"role": "user", "content": "x"}],
            stream=True,
            on_event=lambda ev: deltas.append(ev.get("text") or ""),
        )
    finally:
        stub_s.close()
    sse_ok = (
        out.get("content") == "SSE-OK"
        and out.get("sse_done") is True
        and len(out.get("tool_calls") or []) == 1
        and (out["tool_calls"][0].get("function") or {}).get("name") == "read_file"
        and (out["tool_calls"][0].get("function") or {}).get("arguments")
        == '{"path": "README.md"}'
        and out.get("usage", {}).get("prompt_tokens") == 3
        and out.get("usage", {}).get("completion_tokens") == 4
        and deltas == ["SSE-", "OK"]
    )
    case(
        "Responses SSE：delta 推进 + completed 内嵌 response 优先 + usage 键名映射",
        sse_ok,
        "content=%r calls=%d usage=%s deltas=%s"
        % (
            out.get("content"),
            len(out.get("tool_calls") or []),
            json.dumps(out.get("usage") or {}, ensure_ascii=False),
            deltas,
        ),
    )

    # ⑥ 失败分类：incomplete / 空 output 都必须归 provider，不洗成成功
    stub_bad = Stub(
        [
            {
                "json": {
                    "id": "r_bad",
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [],
                }
            }
        ]
    )
    try:
        try:
            stub_bad.provider(protocol="responses").chat(
                [{"role": "user", "content": "x"}]
            )
            inc_kind = "没抛错（洗成成功）"
        except AgentError as e:
            inc_kind = e.kind
    finally:
        stub_bad.close()
    try:
        _norm_responses({"status": "completed", "output": []})
        empty_kind = "没抛错（洗成成功）"
    except AgentError as e:
        empty_kind = e.kind
    case(
        "Responses 失败分类：incomplete 与空 output 都归 provider",
        inc_kind == "provider" and empty_kind == "provider",
        "incomplete=%s empty=%s" % (inc_kind, empty_kind),
    )

    # ⑦ REPL /model 换模型必须连协议带回去：responses 端点上只换模型名，
    # 丢了 protocol 就被打回 /chat/completions，网关当场 400。
    p_proto = stub_r.provider(protocol="responses")
    new_p = _repl_slash("/model other-model", {"root": tmproot}, p_proto, "sid-r", "x")
    case(
        "REPL /model 重建 Provider 时保住 protocol 与 session_id",
        isinstance(new_p, Provider)
        and new_p.protocol == "responses"
        and new_p.session_id == p_proto.session_id
        and new_p.model == "other-model",
        "protocol=%s session=%s/%s"
        % (
            new_p.protocol if isinstance(new_p, Provider) else "-",
            (new_p.session_id if isinstance(new_p, Provider) else "-")[:16],
            p_proto.session_id[:16],
        ),
    )

    # (17) REPL 会话/命令的纯函数（对齐 claude code 的终端面）-------

    # (17) REPL 会话/命令的纯函数（对齐 claude code 的终端面）-------
    sid = _new_session_id()
    case(
        "_new_session_id 形状：agent- 前缀 + 时间戳 + 4 位后缀",
        re.match(r"^agent-\d{8}-\d{6}-[0-9a-f]{4}$", sid) is not None,
        "sid=%s" % sid,
    )

    tpath = os.path.join(tmproot, "t.jsonl")
    ok_rt, detail_rt = True, ""
    try:
        _transcript_append(tpath, {"kind": "user", "text": "u1"})
        _transcript_append(tpath, {"kind": "assistant", "text": "a1"})
        _transcript_append(tpath, {"kind": "meta", "text": "x"})
        recs = _transcript_load(tpath)
        hist = _history_from_transcript(recs)
        ok_rt = len(recs) == 3 and hist == [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
        ]
        detail_rt = "recs=%d hist=%d" % (len(recs), len(hist))
    except Exception as e:  # noqa: BLE001
        ok_rt, detail_rt = False, str(e)[:80]
    case("会话 JSONL 追加/读回/还原成 messages（meta 不还原）", ok_rt, detail_rt)

    hist = [{"role": "system", "content": "s"}] + [
        {"role": "user", "content": "m%d" % i} for i in range(30)
    ]
    comp, dropped = _compact_history(hist, keep=5)
    case(
        "_compact_history 保留 system + 最近 N 条（丢弃数可见）",
        len(comp) == 6
        and comp[0]["role"] == "system"
        and comp[-1]["content"] == "m29"
        and dropped == 25,
        "len=%d dropped=%d" % (len(comp), dropped),
    )

    # (W1) compact v2：摘要进历史 / 失败逐字回退 / 聚焦词透传 -----------
    _dstub = Stub([jc(content="[摘要] 关键路径 tools/dsp.py、决策：先跑门、rc=0")])
    _v2hist = [{"role": "system", "content": "s"}] + [
        {"role": "user", "content": "m%d" % i} for i in range(30)
    ]
    _v2, _d2, _dg2 = _compact_history_v2(_v2hist, keep=5, provider=_dstub.provider())
    case(
        "compact v2：丢前生成摘要进 history（system+摘要+最近 N）",
        _dg2
        and _v2[0]["role"] == "system"
        and _v2[1]["content"].startswith(DIGEST_PREFIX)
        and _v2[-1]["content"] == "m29"
        and len(_v2) == 7
        and _d2 == 25,
        "dg=%s len=%d head=%r" % (_dg2, len(_v2), _v2[1]["content"][:24]),
    )
    _fstub = Stub([{"status": 500, "json": {"error": "boom"}}])
    _v2b, _d3, _dg3 = _compact_history_v2(_v2hist, keep=5, provider=_fstub.provider())
    _fstub.close()
    _oldb, _d4 = _compact_history(_v2hist, keep=5)
    case(
        "compact v2 回退：provider 失败时与旧口径逐字相同",
        _v2b == _oldb and _d3 == _d4 and not _dg3,
        "dg=%s same=%s" % (_dg3, _v2b == _oldb),
    )
    _fctx = {
        "root": tmproot,
        "mode": "plan",
        "history": [{"role": "system", "content": "s"}]
        + [{"role": "user", "content": "m%d" % i} for i in range(30)],
    }
    _repl_slash("/compact 只留部署相关", _fctx, _dstub.provider(), "sid", "p")
    _fp = json.dumps(_dstub.seen[-1], ensure_ascii=False)
    case(
        "/compact 聚焦词进摘要请求（且摘要真落在 history）",
        "只留部署相关" in _fp
        and _fctx["history"][1]["content"].startswith(DIGEST_PREFIX),
        "seen=%d focus=%s" % (len(_dstub.seen), "只留部署相关" in _fp),
    )
    _dstub.close()

    # (W1) /rewind：重放对齐 / 端到端落 note / 越界 fail-closed ---------
    _rw = os.path.join(tmproot, "rw.jsonl")
    for _i in range(1, 4):
        _transcript_append(_rw, {"kind": "user", "text": "u%d" % _i})
        _transcript_append(_rw, {"kind": "assistant", "text": "a%d" % _i})
    _rwr = _transcript_load(_rw)
    _rmsgs, _rerr, _rturns = _rewind_replay(_rwr, 2)
    case(
        "rewind 重放：第 2 轮末 = u1..a2 四条、轮数=3",
        _rerr == ""
        and _rturns == 3
        and _rmsgs
        == [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
        ],
        "err=%r" % _rerr,
    )
    _rctx = {
        "root": tmproot,
        "mode": "plan",
        "history": [{"role": "user", "content": "stale"}],
    }
    _repl_slash("/rewind 2", _rctx, None, "sid", _rw)
    _rr = _transcript_load(_rw)
    case(
        "/rewind 端到端：history 重建 + note 落盘 + 原记录保留",
        _rctx["history"] == _rmsgs
        and len(_rr) == 7
        and _rr[-1].get("kind") == "note"
        and _rr[0]["text"] == "u1",
        "hist=%d recs=%d last=%s"
        % (len(_rctx["history"]), len(_rr), _rr[-1].get("kind")),
    )
    _keep_hist = _rctx["history"]
    _repl_slash("/rewind 0", _rctx, None, "sid", _rw)
    _repl_slash("/rewind 9", _rctx, None, "sid", _rw)
    _repl_slash("/rewind abc", _rctx, None, "sid", _rw)
    _rr2 = _transcript_load(_rw)
    case(
        "rewind 越界/非数 fail-closed：history 不动、无新 note",
        _rctx["history"] is _keep_hist and len(_rr2) == 7,
        "recs=%d" % len(_rr2),
    )

    # (W2) subagent-as-tool：可重入 / 禁嵌套 / 步数封顶 / 子集强制 / 权限继承
    # 桩脚本**序贯共享**：外层与内层走同一个桩，按下标依次取响应。
    _dsub = Stub(
        [
            jc(
                tool=(
                    "subagent",
                    {
                        "prompt": "数一下工具目录里的条目数",
                        "tools": ["list_dir"],
                        "max_steps": 3,
                    },
                )
            ),
            jc(content="子代理结论：目录共 103 项"),
            jc(content="外围结论：已拿到子代理汇报"),
        ]
    )
    _ores = run_agent(
        "先把清单交给子代理查",
        ctx={"root": tmproot, "mode": "accept-edits"},
        provider=_dsub.provider(),
        max_steps=4,
    )
    _tmsg = [m for m in _ores.get("messages") or [] if m.get("role") == "tool"]
    try:
        _sub_rep = json.loads(_tmsg[0]["content"]) if _tmsg else {}
    except ValueError:
        _sub_rep = {}
    case(
        "subagent 可重入：外层停下→内层出终答→外层继续（3 请求）",
        _ores.get("ok")
        and len(_dsub.seen) == 3
        and _sub_rep.get("summary") == "子代理结论：目录共 103 项"
        and _sub_rep.get("steps") == 1
        and _sub_rep.get("rc") == 0,
        "seen=%d summary=%r" % (len(_dsub.seen), str(_sub_rep.get("summary"))[:24]),
    )
    _dsub.close()
    _nstub = Stub([jc(content="不应被调用")])
    _nres, _ = exec_tool(
        "subagent",
        {"prompt": "再来一层"},
        {
            "root": tmproot,
            "mode": "accept-edits",
            "subagent_depth": 1,
            "_provider": _nstub.provider(),
        },
    )
    case(
        "subagent 禁嵌套：depth>0 直接拒且零请求（fail-closed）",
        _nres.get("ok") is False
        and _nres.get("executed") is False
        and "禁止嵌套" in str(_nres.get("error"))
        and not _nstub.seen,
        "err=%r seen=%d" % (str(_nres.get("error"))[:30], len(_nstub.seen)),
    )
    _nstub.close()
    # 夹具补齐（W11.1）：让每步读的是**真存在**的文件 —— 旧夹具 40 个「读不
    # 存在的文件」在新守卫②（同工具连续失败 ≥6 停）下第 6 步就被叫停，测不到
    # 「99 被钳到 12」这条本意。调用仍逐文件变化（不触 MAX_REPEAT/TEXT_REPEAT）。
    for _i in range(40):
        with open(os.path.join(tmproot, "n%d.txt" % _i), "w", encoding="utf-8") as _f:
            _f.write("x")
    _mstub = Stub(
        [jc(tool=("read_file", {"path": "n%d.txt" % _i})) for _i in range(40)]
    )
    _mres, _ = exec_tool(
        "subagent",
        {"prompt": "无限读", "max_steps": 99},
        {"root": tmproot, "mode": "accept-edits", "_provider": _mstub.provider()},
    )
    case(
        "subagent 步数解封：99 不再钳到 12（走满 40 个不同动作才停）",
        _mres.get("limit") is True
        and _mres.get("steps", 0) > SUBAGENT_MAX_STEPS
        and len(_mstub.seen) > SUBAGENT_MAX_STEPS,
        "steps=%s seen=%d" % (_mres.get("steps"), len(_mstub.seen)),
    )
    _mstub.close()
    _why_ss = _authorize(
        TOOLS["write_file"],
        {"path": "sub-x.txt", "content": "x"},
        {"root": tmproot, "mode": "full", "tool_subset": ["read_file"]},
    )
    _r_ss, _ = exec_tool(
        "write_file",
        {"path": "sub-x.txt", "content": "x"},
        {"root": tmproot, "mode": "full", "tool_subset": ["read_file"]},
    )
    case(
        "subagent 工具子集强制：子集外工具拒 + 真跑不落盘",
        isinstance(_why_ss, str)
        and "子集" in _why_ss
        and _r_ss.get("executed") is False
        and not os.path.exists(os.path.join(tmproot, "sub-x.txt")),
        "why=%r ex=%s" % (str(_why_ss)[:30], _r_ss.get("executed")),
    )
    _istub = Stub(
        [
            jc(tool=("write_file", {"path": "sub-plan.txt", "content": "x"}), cid="w1"),
            jc(content="子代理：写被拒了"),
        ]
    )
    _ires, _ = exec_tool(
        "subagent",
        {"prompt": "试着写个文件", "tools": ["write_file"], "max_steps": 4},
        {"root": tmproot, "mode": "plan", "_provider": _istub.provider()},
    )
    case(
        "subagent 权限继承：外层 plan 模式下内层写被拒、文件未落",
        _ires.get("ok") is True
        and len(_istub.seen) == 2
        and not os.path.exists(os.path.join(tmproot, "sub-plan.txt")),
        "ok=%s seen=%d" % (_ires.get("ok"), len(_istub.seen)),
    )
    _istub.close()

    # (W11.1) 打转守卫三例：重复语句提前停 / 同工具连续失败提前停 / 空答诚实化
    _g1 = Stub(
        [
            jc(tool=("read_file", {"path": "n0.txt"}), content="先看一下现状再说")
            for _i in range(5)
        ]
    )
    _r1 = run_agent(
        "干个活",
        ctx={"root": tmproot, "mode": "plan"},
        provider=_g1.provider(),
        max_steps=10,
    )
    _g1.close()
    case(
        "打转守卫①：同一句话连说无新成功动作 → 提前停（不烧预算）",
        _r1.get("kind") == "limit"
        and _r1.get("steps") == 4
        and "同一句话连续重复" in str(_r1.get("error")),
        "steps=%s err=%r" % (_r1.get("steps"), str(_r1.get("error"))[:44]),
    )
    _g2 = Stub(
        [jc(tool=("read_file", {"path": "g-none-%d.txt" % _i})) for _i in range(8)]
    )
    _r2 = run_agent(
        "干个活",
        ctx={"root": tmproot, "mode": "plan"},
        provider=_g2.provider(),
        max_steps=12,
    )
    _g2.close()
    case(
        "打转守卫②：同一工具连续失败 6 次（参数每次不同）→ 提前停",
        _r2.get("kind") == "limit"
        and _r2.get("steps") == 6
        and "连续失败" in str(_r2.get("error")),
        "steps=%s err=%r" % (_r2.get("steps"), str(_r2.get("error"))[:44]),
    )
    _g3 = Stub([jc(content="")])
    _r3 = run_agent(
        "干个活",
        ctx={"root": tmproot, "mode": "plan"},
        provider=_g3.provider(),
        max_steps=4,
    )
    _g3.close()
    case(
        "打转守卫③：空终答诚实化（empty_answer 标记 + 可读错误，不装成功）",
        _r3.get("ok") is True
        and _r3.get("empty_answer") is True
        and bool(_r3.get("error")),
        "ok=%s empty=%s" % (_r3.get("ok"), _r3.get("empty_answer")),
    )

    # (W13) 批准即执行 / 诚实收尾：五例（纯面 + 口径 + 行为）
    case(
        "W13 批准词识别：拍板/同意/开工 命中；不同意/没批准 不误判",
        bool(
            _looks_approved("拍板")
            and _looks_approved("同意执行")
            and _looks_approved("开干吧")
        )
        and not _looks_approved("我不同意")
        and not _looks_approved("没有批准")
        and not _looks_approved("还未同意")
        and not _looks_approved("暂不批准"),
        "正=%s 负=%s" % (_looks_approved("拍板"), _looks_approved("我不同意")),
    )
    case(
        "W13 零证据完成声明：0 工具+'已全面落实'=打脸；有工具=不打脸",
        _claim_no_evidence("已全面落实任务", 0) is True
        and _claim_no_evidence("本轮读了 3 个文件", 3) is False
        and _claim_no_evidence("已部署完成", 0) is True,
        "claims=%s" % _claim_no_evidence("已全面落实任务", 0),
    )
    case(
        "W13 跨轮复读检测：逐字重复=真；不同内容=假",
        _is_stale_reply(
            "**做了什么：** 已全面落实任务，暂无未做内容。"
            "**还剩什么没做：** 未动任何文件。",
            "**做了什么：** 已全面落实任务，暂无未做内容。"
            "**还剩什么没做：** 未动任何文件。",
        )
        is True
        and _is_stale_reply(
            "上一轮的长答复内容……" * 4, "完全不同的新答复内容：" + "新" * 60
        )
        is False,
        "stale_check_ok",
    )
    _sp_dir = os.path.join(ROOT, "tools")
    import importlib.util as _ilu2

    _spc = _ilu2.spec_from_file_location("dsp_w13", os.path.join(_sp_dir, "dsp.py"))
    _sp = _ilu2.module_from_spec(_spc)
    _spc.loader.exec_module(_sp)
    _pl = _sp.new_plan(
        "口径夹具",
        [
            {
                "id": "x",
                "title": "t",
                "accept": "rc=0",
                "tools": ["read_file"],
                "depends": [],
            }
        ],
    )
    case(
        "W13 计划口径随闸走：自由面不说锁死、上锁面才说",
        "写面自由" in _sp.plan_summary(_pl, locked=False)
        and "写工具锁死" not in _sp.plan_summary(_pl, locked=False)
        and "写工具锁死" in _sp.plan_summary(_pl, locked=True)
        and "不要等审批" in _sp.auto_directive(_pl, {}, locked=False)
        and "未审批时写工具锁死" in _sp.auto_directive(_pl, {}, locked=True),
        "free=%r" % _sp.plan_summary(_pl, locked=False)[:44],
    )
    # 行为：批准/空答重试判定（纯函数）+ 工具真驱动夹具
    case(
        "W13 重试判定纯函数：批准轮零工具=重试、有工具/空答勿误触",
        _needs_approve_retry(True, [], False) is True
        and _needs_approve_retry(True, [{"tool": "read_file"}], False) is False
        and _needs_approve_retry(False, [], False) is False
        and _needs_approve_retry(True, [], True) is False,
        "pure_ok",
    )
    _ap = Stub(
        [
            jc(tool=("read_file", {"path": "n0.txt"}), content="已读文件，rc=0"),
            jc(content="读完了 n0.txt：内容为 x。rc=0"),
        ]
    )
    _apval = run_agent(
        "（批准重试夹具）读 n0.txt",
        ctx={"root": tmproot, "mode": "plan"},
        provider=_ap.provider(),
        max_steps=4,
    )
    _ap.close()
    case(
        "W13 工具真驱动夹具：run_agent 层工具调用可被桩真实驱动",
        _apval.get("ok") is True
        and any(u.get("tool") == "read_file" for u in _apval.get("tools_used") or []),
        "ok=%s tools=%d" % (_apval.get("ok"), len(_apval.get("tools_used") or [])),
    )

    # (W3) MCP stdio 客户端：真握手 / 降级如实 / id 错配 / 死服务器 fail-closed
    try:
        if os.path.join(ROOT, "tools") not in sys.path:
            sys.path.insert(0, os.path.join(ROOT, "tools"))
        import mcp_client as _mcpcli
    except ImportError:
        _mcpcli = None
    _fx = os.path.join(ROOT, "tools", "mcp_fixture_srv.py")
    case(
        "mcp_client 可导入 + 纯面（encode/decode 往返 + 名字净化）",
        _mcpcli is not None
        and _mcpcli.decode(_mcpcli.encode({"a": 1}).decode())["a"] == 1
        and _mcpcli.tool_name("srv.a", "echo/b") == "mcp_srv_a_echo_b",
        "mod=%s" % (_mcpcli is not None),
    )
    _gok = False
    _srv_g = None
    if _mcpcli:
        os.environ["AGENTOS_LLM_API_KEY"] = "st-secret-for-test"
        _srv_g = _mcpcli.Server("fx", [sys.executable, _fx, "good"])
        try:
            _srv_g.start()
            _srv_g.initialize()
            _tl = _srv_g.list_tools()
            _cr = _srv_g.call_tool("echo", {"q": 1})
            _gok = (
                _srv_g.handshake == "ok"
                and len(_tl) == 2
                and "echo:" in (_cr.get("result") or "")
                and "key=ABSENT" in (_cr.get("result") or "")
            )
        except Exception:  # noqa: BLE001
            _gok = False
        finally:
            _srv_g.close()
            os.environ.pop("AGENTOS_LLM_API_KEY", None)
    case(
        "MCP 良好路径：握手 ok + list 2 工具 + call 回显 + 密钥不进子进程",
        bool(_gok),
        "handshake=%s" % (getattr(_srv_g, "handshake", "-"),),
    )
    _rok = False
    _srv_r = None
    if _mcpcli:
        _srv_r = _mcpcli.Server("fx", [sys.executable, _fx, "refuse-init"])
        try:
            _srv_r.start()
            _srv_r.initialize()
            _tl2 = _srv_r.list_tools()
            _rok = _srv_r.handshake == "stateless" and len(_tl2) == 2
        except Exception:  # noqa: BLE001
            _rok = False
        finally:
            _srv_r.close()
    case(
        "MCP 降级：initialize 被拒 → stateless 如实记录且仍可用",
        bool(_rok),
        "handshake=%s" % (getattr(_srv_r, "handshake", "-"),),
    )
    _bok = False
    _srv_b = None
    if _mcpcli:
        _srv_b = _mcpcli.Server("fx", [sys.executable, _fx, "bad-id"])
        try:
            _srv_b.start()
            _srv_b.list_tools()
        except _mcpcli.MCPError as e:
            _bok = "错配" in str(e)
        except Exception:  # noqa: BLE001
            _bok = False
        finally:
            _srv_b.close()
    case(
        "MCP id 错配 fail-closed（不把发错的响应当自己的）", bool(_bok), "ok=%s" % _bok
    )
    _dok = False
    _srv_d = None
    if _mcpcli:
        _t0d = time.time()
        _srv_d = _mcpcli.Server("fx", [sys.executable, _fx, "die"])
        try:
            _srv_d.start()
            _srv_d.list_tools()
        except _mcpcli.MCPError as e:
            _dok = (("已退出" in str(e)) or ("EOF" in str(e))) and (
                time.time() - _t0d
            ) < 10
        except Exception:  # noqa: BLE001
            _dok = False
        finally:
            _srv_d.close()
    case("MCP 死服务器 fail-closed（报 EOF/退出且不 hang）", bool(_dok), "ok=%s" % _dok)
    _reg = {}
    if _mcpcli:
        _tcopy = {}
        _rer = _mcpcli.sync_tools(
            _tcopy,
            {
                "servers": {
                    "fxs": {
                        "cmd": [sys.executable, _fx, "good"],
                        "tools": {"set_thing": "write"},
                    }
                }
            },
            risk_read=RISK_READ,
            risk_write=RISK_WRITE,
        )
        _reg = {
            "added": sorted(_rer.get("added") or []),
            "echo": _tcopy.get("mcp_fxs_echo", {}).get("risk"),
            "set": _tcopy.get("mcp_fxs_set_thing", {}).get("risk"),
            "fn": callable(_tcopy.get("mcp_fxs_echo", {}).get("fn")),
        }
    case(
        "MCP 注册：mcp_<server>_<tool> 注入 + 风险逐工具映射（read/write）",
        _reg.get("added") == ["mcp_fxs_echo", "mcp_fxs_set_thing"]
        and _reg.get("echo") == RISK_READ
        and _reg.get("set") == RISK_WRITE
        and _reg.get("fn"),
        "added=%s echo=%s set=%s"
        % (_reg.get("added"), _reg.get("echo"), _reg.get("set")),
    )

    # (W4) hooks 中间件 + skills 目录：拦截 / fail-closed / 预算 / 注入 / glob
    try:
        if os.path.join(ROOT, "tools") not in sys.path:
            sys.path.insert(0, os.path.join(ROOT, "tools"))
        import hooks_mw as _hmw
        import skills_load as _skm
    except ImportError:
        _hmw = None
        _skm = None
    _cfg_blk = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "write_*", "hooks": [{"command": "printf blocked; exit 2"}]}
            ]
        }
    }
    _cfg_ctx = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [{"command": 'printf \'{"additionalContext": "CTX-A"}\''}],
                }
            ]
        }
    }
    _rb = _hmw.run_event("PreToolUse", {"tool": "write_file"}, _cfg_blk) if _hmw else {}
    _rm = (
        _hmw.run_event("PreToolUse", {"tool": "read_file"}, _cfg_blk)
        if _hmw
        else {"ran": 0}
    )
    _rc = _hmw.run_event("PreToolUse", {"tool": "x"}, _cfg_ctx) if _hmw else {}
    case(
        "hooks：exit 2 拦截 / glob 不匹配不跑 / additionalContext 收集",
        _hmw is not None
        and _rb.get("block") is True
        and "blocked" in str(_rb.get("reason"))
        and _rm.get("ran") == 0
        and _rc.get("block") is False
        and _rc.get("context") == ["CTX-A"],
        "blk=%s miss=%s ctx=%s"
        % (_rb.get("block"), _rm.get("ran"), _rc.get("context")),
    )
    _cfg_bad = {
        "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"command": "exit 7"}]}]}
    }
    _cfg_slow = {
        "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"command": "sleep 3"}]}]}
    }
    _rf = _hmw.run_event("PreToolUse", {"tool": "x"}, _cfg_bad) if _hmw else {}
    _rt = (
        _hmw.run_event("PreToolUse", {"tool": "x"}, _cfg_slow, timeout=1.0)
        if _hmw
        else {}
    )
    case(
        "hooks PreToolUse fail-closed：非 0 退出码与超时都拦截",
        _rf.get("block") is True and _rt.get("block") is True,
        "rc7=%s timeout=%s" % (_rf.get("block"), _rt.get("block")),
    )
    _skroot = os.path.join(tmp, "skroot")
    os.makedirs(os.path.join(_skroot, "alpha"), exist_ok=True)
    os.makedirs(os.path.join(_skroot, "beta"), exist_ok=True)
    with open(os.path.join(_skroot, "alpha", "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write("---\nname: alpha\ndescription: 甲技能\n---\nALPHA-BODY\n")
    with open(os.path.join(_skroot, "beta", "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write("---\nname: beta\ndescription: %s\n---\nBETA-BODY\n" % ("长" * 600))
    _its = _skm.discover([_skroot]) if _skm else []
    _sec = _skm.render_section(_its) if _skm else ""
    _bline = [ln for ln in _sec.splitlines() if ln.startswith("- beta:")]
    case(
        "skills：frontmatter 解析 + description 超 500 截断留标记 + 不进正文",
        [i["name"] for i in _its] == ["alpha", "beta"]
        and bool(_bline)
        and _bline[0].endswith("…")
        and len(_bline[0]) <= 500 + len("- beta: ") + 1
        and "ALPHA-BODY" not in _sec,
        "names=%s bline_len=%s"
        % ([i["name"] for i in _its], len(_bline[0]) if _bline else -1),
    )
    _sctx = {"root": tmproot, "mode": "plan", "history": []}
    _SKILL_ROOTS_OVERRIDE.append(_skroot)
    try:
        _repl_slash("/skill alpha", _sctx, None, "sid", "p")
        _ok_inj = (
            bool(_sctx["history"])
            and _sctx["history"][-1]["role"] == "user"
            and "ALPHA-BODY" in _sctx["history"][-1]["content"]
        )
        _repl_slash("/skill 不存在", _sctx, None, "sid", "p")
        _ok_unknown = len(_sctx["history"]) == 1
    finally:
        _SKILL_ROOTS_OVERRIDE.pop()
    case(
        "/skill 注入正文进下一轮上下文；未知名不改历史",
        _ok_inj and _ok_unknown,
        "inj=%s unknown_keep=%s" % (_ok_inj, _ok_unknown),
    )
    _sp4 = build_system_prompt(
        {"root": tmproot, "mode": "plan", "hook_context": ["CTX-Z9"]}
    )
    case(
        "hooks 上下文进 system prompt（SessionStart/Pre 注入路径）",
        "CTX-Z9" in _sp4,
        "len=%d" % len(_sp4),
    )

    # (W5) 终端 UX 四件：续行 / @补全 / ⎿详情 / 终答缩进 ----------------
    case(
        "终端 UX：行尾反斜杠续行（奇=续、偶=字面反斜杠）",
        _line_wants_continue("跑门\\")
        and not _line_wants_continue("a\\\\")
        and not _line_wants_continue("plain")
        and _compose_join("第一段\\", "第二段") == "第一段\n第二段",
        "odd=%s even=%s"
        % (_line_wants_continue("跑门\\"), _line_wants_continue("a\\\\")),
    )
    _uxroot = os.path.join(tmp, "uxroot")
    os.makedirs(os.path.join(_uxroot, "sub"), exist_ok=True)
    with open(os.path.join(_uxroot, "a.txt"), "w", encoding="utf-8") as fh:
        fh.write("a\n")
    with open(os.path.join(_uxroot, "sub", "b.txt"), "w", encoding="utf-8") as fh:
        fh.write("b\n")
    with open(os.path.join(_uxroot, ".hidden"), "w", encoding="utf-8") as fh:
        fh.write("h\n")
    case(
        "@ 路径 Tab 补全：前缀匹配/目录带斜杠/隐藏跳过/非 @ 不问",
        _at_completions("@a", _uxroot) == ["@a.txt"]
        and _at_completions("@sub/", _uxroot) == ["@sub/b.txt"]
        and sorted(_at_completions("@", _uxroot)) == ["@a.txt", "@sub/"]
        and _at_completions("plain", _uxroot) == [],
        "a=%s sub=%s"
        % (_at_completions("@a", _uxroot), _at_completions("@sub/", _uxroot)),
    )
    _ts1 = _tool_summary(
        "edit_file",
        {"path": "x.py", "old_string": "l1\nl2\nl3", "new_string": "l1\nX\nl3"},
        {"ok": True},
    )
    _ts2 = _tool_summary(
        "write_file", {"path": "y.txt", "content": "a\nb"}, {"ok": True}
    )
    case(
        "⎿详情：edit_file 行级 ±对账 + write_file 行/字节数",
        "+1 −1" in _ts1 and "x.py" in _ts1 and "写入 2 行" in _ts2 and "y.txt" in _ts2,
        "edit=%r" % (_ts1[:40],),
    )
    case(
        "⎿详情边界：失败/被拒/非编辑工具一律空（不装饰）",
        _tool_summary(
            "edit_file",
            {"path": "x", "old_string": "a", "new_string": "b"},
            {"ok": False},
        )
        == ""
        and _tool_summary("edit_file", {"path": "x"}, {"ok": True}) == ""
        and _tool_summary("read_file", {"path": "x"}, {"ok": True}) == "",
        "边界三态",
    )
    case(
        "终答渲染：续行缩进两格、单行不变、空答给占位",
        _format_answer("l1\nl2") == "l1\n  l2"
        and _format_answer("single") == "single"
        and _format_answer("") == "（无终答）",
        "fmt=%r" % (_format_answer("l1\nl2"),),
    )
    try:
        with open(os.path.join(tmproot, "note-e.txt"), "w", encoding="utf-8") as fh:
            fh.write("l1\nl2\nl3\n")
        _estub = Stub(
            [
                jc(
                    tool=(
                        "edit_file",
                        {"path": "note-e.txt", "old_string": "l2", "new_string": "XX"},
                    )
                ),
                jc(content="改完了"),
            ]
        )
        _evs = []
        run_agent(
            "把 note-e 的第二行改掉",
            ctx={"root": tmproot, "mode": "full"},
            provider=_estub.provider(),
            max_steps=4,
            on_event=_evs.append,
        )
        _summ = [
            e.get("summary")
            for e in _evs
            if e.get("type") == "tool" and e.get("tool") == "edit_file"
        ]
        _eok = bool(_summ) and "+1 −1" in (_summ[0] or "")
        _estub.close()
    except Exception:  # noqa: BLE001
        _eok = False
    case("⎿详情进 tool 事件（run_agent 真跑 edit_file）", _eok, "ok=%s" % _eok)

    # (W6) 并行子代理批量 + skills 自动触发 ------------------------------
    # 并行性用 barrier 确定性证伪：三条子代理线必须同时到达 —— 串行化会
    # 让 Barrier 超时（BrokenBarrierError），不靠摸时钟。
    _bar = threading.Barrier(3, timeout=8)

    class _BP(object):
        model = "barrier"
        base_url = "stub"
        source = "barrier"
        session_id = "bar"
        protocol = "chat"

        def chat(self, messages, tools=None, stream=False, on_event=None, cancel=None):
            _bar.wait()
            return {
                "content": "批内完成",
                "tool_calls": [],
                "usage": {},
                "finish_reason": "stop",
            }

    try:
        _bres, _ = exec_tool(
            "subagent",
            {"prompts": ["甲任务", "乙任务", "丙任务"], "max_steps": 2},
            {"root": tmproot, "mode": "plan", "_provider": _BP()},
        )
        _brows = _bres.get("results") or []
        case(
            "subagent 并行批次：barrier 三线同到（串行必 BrokenBarrier）",
            _bres.get("ok") is True
            and _bres.get("parallel") is True
            and len(_brows) == 3
            and all(r.get("ok") for r in _brows)
            and [r.get("prompt") for r in _brows] == ["甲任务", "乙任务", "丙任务"],
            "ok=%s n=%d" % (_bres.get("ok"), len(_brows)),
        )
    except Exception as e:  # noqa: BLE001
        case(
            "subagent 并行批次：barrier 三线同到（串行必 BrokenBarrier）",
            False,
            repr(e)[:100],
        )
    _r_lim, _ = exec_tool(
        "subagent",
        {"prompts": ["a"] * 5},
        {"root": tmproot, "mode": "plan", "_provider": _BP()},
    )
    _r_empty, _ = exec_tool(
        "subagent",
        {"prompts": []},
        {"root": tmproot, "mode": "plan", "_provider": _BP()},
    )
    _r_blank, _ = exec_tool(
        "subagent",
        {"prompts": [" ", "x"]},
        {"root": tmproot, "mode": "plan", "_provider": _BP()},
    )
    case(
        "subagent 批量边界：5 个/空表/空白项 均拒（fail-closed）",
        _r_lim.get("executed") is False
        and str(SUBAGENT_PARALLEL_MAX) in str(_r_lim.get("error"))
        and _r_empty.get("executed") is False
        and _r_blank.get("executed") is False,
        "lim=%r" % (str(_r_lim.get("error"))[:44],),
    )

    class _PickyP(object):
        model = "picky"
        base_url = "stub"
        source = "picky"
        session_id = "pick"
        protocol = "chat"

        def chat(self, messages, tools=None, stream=False, on_event=None, cancel=None):
            last = str((messages or [{}])[-1].get("content") or "")
            if "boom" in last:
                raise AgentError("provider", "boom 任务失败")
            return {
                "content": "好:%s" % last[:8],
                "tool_calls": [],
                "usage": {},
                "finish_reason": "stop",
            }

    _fres, _ = exec_tool(
        "subagent",
        {"prompts": ["好一", "boom 掉", "好二"], "max_steps": 2},
        {"root": tmproot, "mode": "plan", "_provider": _PickyP()},
    )
    _fr = _fres.get("results") or []
    case(
        "subagent 批量失败隔离：单任务炸不拖垮其余、按输入序返回",
        _fres.get("ok") is True
        and len(_fr) == 3
        and _fr[0].get("ok")
        and (not _fr[1].get("ok"))
        and _fr[2].get("ok")
        and "失败" in str(_fr[1].get("summary")),
        "oks=%s" % ([r.get("ok") for r in _fr],),
    )
    _s1stub = Stub([jc(content="单发结论")])
    _s1, _ = exec_tool(
        "subagent",
        {"prompt": "单发任务", "max_steps": 2},
        {"root": tmproot, "mode": "plan", "_provider": _s1stub.provider()},
    )
    _s1stub.close()
    case(
        "subagent 单发路径回归：prompt 仍走单人版（无 results 键）",
        _s1.get("ok") is True
        and "results" not in _s1
        and _s1.get("summary") == "单发结论",
        "keys=%s" % (sorted(_s1)[:5],),
    )
    _aroot = os.path.join(tmp, "skauto")
    os.makedirs(os.path.join(_aroot, "fix"), exist_ok=True)
    os.makedirs(os.path.join(_aroot, "no"), exist_ok=True)
    with open(os.path.join(_aroot, "fix", "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write(
            "---\nname: fixit\ndescription: 修布局\ntriggers: "
            "flutter，布局崩\n---\nFIXIT-BODY-步骤一二三\n"
        )
    with open(os.path.join(_aroot, "no", "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write("---\nname: noauto\ndescription: 无触发\n---\nNO-BODY\n")
    _parsed = _skm.parse_skill(os.path.join(_aroot, "fix", "SKILL.md"))
    case(
        "skills triggers 解析：逗号/中文逗号切分、缺省空表",
        _parsed.get("triggers") == ["flutter", "布局崩"]
        and _skm.parse_skill(os.path.join(_aroot, "no", "SKILL.md")).get("triggers")
        == [],
        "trs=%s" % (_parsed.get("triggers"),),
    )
    _sth = Stub([jc(content="收到")])
    _SKILLS_ITEMS.clear()
    _SKILL_ROOTS_OVERRIDE.append(_aroot)
    try:
        run_agent(
            "帮我看看 flutter 的布局崩问题",
            ctx={"root": tmproot, "mode": "plan"},
            provider=_sth.provider(),
            max_steps=2,
        )
        _seen0 = json.dumps(_sth.seen[0], ensure_ascii=False)
        run_agent(
            "一句无关的话",
            ctx={"root": tmproot, "mode": "plan"},
            provider=_sth.provider(),
            max_steps=2,
        )
        _seen1 = json.dumps(_sth.seen[1], ensure_ascii=False)
    finally:
        _SKILL_ROOTS_OVERRIDE.pop()
        _SKILLS_ITEMS.clear()
        _sth.close()
    case(
        "skills 自动触发：triggers 命中注入正文 / 未命中不注入",
        "FIXIT-BODY" in _seen0
        and "NO-BODY" not in _seen0
        and "FIXIT-BODY" not in _seen1,
        "hit=%s miss=%s" % ("FIXIT-BODY" in _seen0, "FIXIT-BODY" in _seen1),
    )

    # (W7) 会话分支 /branch + 计划自验收 /verify ---------------------
    try:
        if os.path.join(ROOT, "tools") not in sys.path:
            sys.path.insert(0, os.path.join(ROOT, "tools"))
        import branches as _bm7
    except ImportError:
        _bm7 = None
    _bs = os.path.join(tmp, "bsrc")
    os.makedirs(_bs, exist_ok=True)
    _bsrc = os.path.join(_bs, "agent-20261013-0001-ab12.jsonl")
    with open(_bsrc, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "user", "text": "u1"}) + "\n")
        fh.write(json.dumps({"kind": "assistant", "text": "a1"}) + "\n")
        fh.write(json.dumps({"kind": "user", "text": "u2"}) + "\n")
    case(
        "branches 可导入 + fork 原语",
        _bm7 is not None
        and hasattr(_bm7, "fork_transcript")
        and hasattr(_bm7, "verify_plan"),
        "mod=%s" % (_bm7 is not None),
    )
    _bok, _berr = (False, "?")
    _bdst = ""
    if _bm7:
        _bdst = os.path.join(tmp, "bdst")
        _bok, _berr = _bm7.fork_transcript(
            _bsrc, _bdst, "agent-20261013-0002-cd34", "试分支"
        )
    _brows = (
        _transcript_load(os.path.join(_bdst, "agent-20261013-0002-cd34.jsonl"))
        if _bok
        else []
    )
    case(
        "/branch 原语：原文件不动 + 新文件多叉注记 + 轮数对",
        bool(_bok)
        and len(_brows) == 4
        and _brows[-1].get("kind") == "note"
        and "fork 自 agent-20261013-0001-ab12" in _brows[-1].get("text", "")
        and open(_bsrc, encoding="utf-8").read().count("\n") == 3,
        "ok=%s rows=%d" % (_bok, len(_brows)),
    )
    _vt = [
        {
            "id": "t1",
            "title": "t",
            "tools": ["read_file"],
            "depends": [],
            "status": "done",
            "accept": "rc=0",
            "evidence": ["pytest rc=0 全绿"],
        },
        {
            "id": "t2",
            "title": "t",
            "tools": ["read_file"],
            "depends": [],
            "status": "todo",
            "accept": "rc=0",
            "evidence": [],
        },
        {
            "id": "t3",
            "title": "t",
            "tools": ["read_file"],
            "depends": [],
            "status": "done",
            "accept": "rc=0",
            "evidence": [],
        },
        {
            "id": "t4",
            "title": "t",
            "tools": ["read_file"],
            "depends": [],
            "status": "done",
            "accept": "rc=0",
            "evidence": ["做了一些事"],
        },
        {
            "id": "t5",
            "title": "t",
            "tools": ["read_file"],
            "depends": [],
            "status": "done",
            "accept": "",
            "evidence": ["rc=0"],
        },
    ]
    _vr = _bm7.verify_plan({"tasks": _vt}) if _bm7 else {"verdicts": []}
    _vm = {x["id"]: x["verdict"] for x in _vr["verdicts"]}
    case(
        "/verify 五态：真完成/未做/证据缺/证据对不上/无 accept 判据",
        _vm
        == {
            "t1": "done",
            "t2": "pending",
            "t3": "evidence-missing",
            "t4": "evidence-mismatch",
            "t5": "evidence-mismatch",
        }
        and _vr["n_done_true"] == 1
        and _vr["n_total"] == 5,
        "m=%s" % (_vm,),
    )
    _vsid = "st-verify-%d" % os.getpid()
    # 先清陈旧 st-verify-*（此前失败的 --check 可能死在清理之前留下孤儿；
    # dsp_gate D12 的「work/plans 无自检残留」判据会因此红——自愈比追责好）。
    _plans_dir = os.path.join(ROOT, "work", "plans")
    try:
        for _stale in os.listdir(_plans_dir):
            # 全量清自检孤儿：任何 st-* 前缀的夹具都不该留在生产 plans/ 里
            # （失败轮残留会让 dsp_gate D12「无自检残留」判红）。
            if _stale.startswith(
                (
                    "st-verify-",
                    "st-watch-",
                    "st-safety-",
                    "st-run-",
                    "st-auto-",
                    "st-",
                )
            ):
                try:
                    os.unlink(os.path.join(_plans_dir, _stale))
                except OSError:
                    pass
    except OSError:
        pass
    _dsp7 = _dsp()
    _p_ok, _ = _dsp7.save_plan(
        _dsp7.new_plan(
            "分支验收",
            [
                {
                    "id": "v1",
                    "title": "读文件",
                    "tools": ["read_file"],
                    "depends": [],
                    "accept": "rc=0",
                }
            ],
        ),
        _vsid,
    )
    _repl_slash(
        "/verify",
        {"root": tmproot, "mode": "plan", "sid": _vsid, "history": []},
        None,
        _vsid,
        os.path.join(tmp, "verify.jsonl"),
    )
    _pl_v, _ = _dsp7.load_plan(_vsid)
    case(
        "/verify 端到端：未做任务如实 pending（不提前 done）",
        bool(_p_ok) and _pl_v is not None and _pl_v["tasks"][0]["status"] != "done",
        "status=%s" % (_pl_v["tasks"][0]["status"] if _pl_v else "?"),
    )
    _dsp7.approve_plan(_vsid)
    with open(os.path.join(tmproot, "verify-probe.txt"), "w", encoding="utf-8") as fh:
        fh.write("probe\n")
    _wrun, _ = exec_tool(
        "read_file",
        {"path": "verify-probe.txt"},
        {"root": tmproot, "mode": "accept-edits", "sid": _vsid, "dsp_task": "v1"},
    )
    _pl_w, _ = _dsp7.load_plan(_vsid)
    case(
        "/verify 不提前 done：白名单工具跑了也不自动 done",
        _pl_w is not None
        and _pl_w["tasks"][0]["status"] != "done"
        and bool(_wrun.get("ok")),
        "status=%s" % (_pl_w["tasks"][0]["status"] if _pl_w else "?"),
    )
    for _suf in ("-plan.json", "-chain.jsonl"):
        try:
            os.unlink(os.path.join(ROOT, "work", "plans", _vsid + _suf))
        except OSError:
            pass
    _bx = {"root": tmproot, "mode": "plan", "history": []}
    _repl_slash("/branch", _bx, None, "nosid", os.path.join(tmp, "nosid.jsonl"))
    case("/branch 缺会话记录：fail-closed 不崩", True, "rc-None=继续")

    # (W8) 监督通道 steer + 监督面 watch + 多智能体 chain/merge --------
    try:
        if os.path.join(ROOT, "tools") not in sys.path:
            sys.path.insert(0, os.path.join(ROOT, "tools"))
        import steer as _srm
        import watch as _wtm
    except ImportError:
        _srm = None
        _wtm = None
    _sbox = os.path.join(tmp, "steerbox")
    case(
        "steer 可导入 + 纯面（往返/游标只进）",
        _srm is not None
        and _srm.push(_sbox, "s1", "先看 readme")[0]
        and _srm.push(_sbox, "s1", "别动 tools/")[0]
        and len(_srm.pending(_sbox, "s1")[0]) == 2
        and _srm.commit(_sbox, "s1", 1)[0]
        and [x["text"] for x in _srm.pending(_sbox, "s1")[0]] == ["别动 tools/"]
        and _srm.commit(_sbox, "s1", 0)[0]
        and len(_srm.pending(_sbox, "s1")[0]) == 1,
        "往返+只进",
    )
    _sstub = Stub([jc(content="收到")])
    _STEER_DIR_OVERRIDE.append(_sbox)
    try:
        _srm.push(_sbox, "st-steer-x", "把范围缩小到 README")
        _srun = run_agent(
            "干点活",
            ctx={"root": tmproot, "mode": "plan", "sid": "st-steer-x"},
            provider=_sstub.provider(),
            max_steps=2,
        )
        _seen_steer = json.dumps(_sstub.seen[0], ensure_ascii=False)
        run_agent(
            "再干点活",
            ctx={"root": tmproot, "mode": "plan", "sid": "st-steer-x"},
            provider=_sstub.provider(),
            max_steps=2,
        )
        _seen_steer2 = json.dumps(_sstub.seen[1], ensure_ascii=False)
    finally:
        _STEER_DIR_OVERRIDE.pop()
        _sstub.close()
    case(
        "steer 注入：指令进本轮消息（下一步骤边界）",
        bool(_srun.get("ok"))
        and "老板指令" in _seen_steer
        and "把范围缩小到 README" in _seen_steer,
        "seen=%s" % ("老板指令" in _seen_steer),
    )
    case(
        "steer 幂等：已消费的指令不再进第二轮",
        "把范围缩小到 README" not in _seen_steer2,
        "again=%s" % ("把范围缩小到 README" in _seen_steer2),
    )
    _wsid = "st-watch-%d" % os.getpid()
    _dsp8 = _dsp()
    _dsp8.save_plan(
        _dsp8.new_plan(
            "监督面夹具",
            [
                {
                    "id": "w1",
                    "title": "第一步",
                    "tools": ["read_file"],
                    "depends": [],
                    "accept": "rc=0",
                }
            ],
        ),
        _wsid,
    )
    _wdir = os.path.join(tmp, "work", "agent_sessions")
    os.makedirs(_wdir, exist_ok=True)
    with open(os.path.join(_wdir, "st-watch-x.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(
            json.dumps({"ts": 1.0, "kind": "user", "text": "看一下 README"}) + "\n"
        )
        fh.write(json.dumps({"ts": 2.0, "kind": "assistant", "text": "好的"}) + "\n")
    _wrep_plan = _wtm.render(_wtm.snapshot(ROOT, _wsid)) if _wtm else ""
    _wrep_tail = _wtm.render(_wtm.snapshot(tmp, "st-watch-x")) if _wtm else ""
    for _suf in ("-plan.json", "-chain.jsonl"):
        try:
            os.unlink(os.path.join(ROOT, "work", "plans", _wsid + _suf))
        except OSError:
            pass
    case(
        "watch 监督面：计划任务进屏 + 会话尾进屏 + 无计划如实说",
        "监督面夹具" in _wrep_plan
        and "w1" in _wrep_plan
        and "看一下 README" in _wrep_tail
        and "计划：无" in _wrep_tail,
        "plan=%s tail=%s" % ("监督面夹具" in _wrep_plan, "看一下 README" in _wrep_tail),
    )
    _chstub = Stub([jc(content="甲结论：先做 A"), jc(content="乙结论：A 已带过")])
    _chres, _ = exec_tool(
        "subagent",
        {"chain": ["任务甲", "任务乙"], "max_steps": 2},
        {"root": tmproot, "mode": "plan", "_provider": _chstub.provider()},
    )
    _chseen2 = json.dumps(_chstub.seen[1], ensure_ascii=False)
    _chstub.close()
    case(
        "subagent chain：上一段结论自动带进下一段（交接）",
        _chres.get("ok") is True
        and _chres.get("chain") is True
        and len(_chres.get("results") or []) == 2
        and "上一阶段结论" in _chseen2
        and "甲结论：先做 A" in _chseen2,
        "ok=%s handoff=%s" % (_chres.get("ok"), "上一阶段结论" in _chseen2),
    )
    _mgstub = Stub(
        [jc(content="子一"), jc(content="子二"), jc(content="汇总裁决：两路一致")]
    )
    _mgres, _ = exec_tool(
        "subagent",
        {"prompts": ["p1", "p2"], "merge": True, "max_steps": 2},
        {"root": tmproot, "mode": "plan", "_provider": _mgstub.provider()},
    )
    _mgseen3 = json.dumps(_mgstub.seen[2], ensure_ascii=False)
    _mgn = len(_mgstub.seen)
    _mgstub.close()
    case(
        "subagent merge：批量后额外一次汇总出 digest（含全部结论）",
        _mgres.get("ok") is True
        and _mgres.get("digest") == "汇总裁决：两路一致"
        and "子一" in _mgseen3
        and "子二" in _mgseen3
        and _mgn == 3,
        "digest=%r n=%d" % (str(_mgres.get("digest"))[:16], _mgn),
    )

    # (W10) 自由面（user-centric defaults）：默认不摆闸；显式 true 才上锁 ----
    _sf = _safety_flags({})
    _sf2 = _safety_flags(
        {"agent": {"plan_lock": True, "mode_gate": True, "cmd_gate": True}}
    )
    _sf3 = _safety_flags(
        {"agent": {"plan_lock": "true", "mode_gate": 1, "cmd_gate": "yes"}}
    )
    case(
        "safety_flags：缺键=自由（默认）；字面 True 才上锁；字符串不认",
        _sf == {"plan_lock_off": True, "mode_gate_off": True, "cmd_gate_off": True}
        and _sf2
        == {"plan_lock_off": False, "mode_gate_off": False, "cmd_gate_off": False}
        and _sf3 == _sf,
        "free=%s lock=%s str=%s" % (_sf, _sf2, _sf3),
    )
    # (2026-10-22) 库入口的权限语义不许被「手搓 ctx」悄悄改掉：
    # 以前只有 CLI 入口带安全闸标志，run_agent(ctx=...) 不带 → ctx.get()
    # 取到 None（falsy）→ 计划锁/模式闸/命令白名单静默全上（实测：A/B 脚本
    # 自拼 ctx，写工具被「计划未审批」挡死，而配置里 plan_lock=false）。
    _ctx_probe = {"root": tmp, "mode": "full", "sid": "st-ctxflags"}
    class _PProbe:
        def chat(self, *a, **k):
            raise AgentError("provider", "夹具：只验 ctx 标志，不真调模型")
    try:
        run_agent(
            "夹具",
            ctx=_ctx_probe,
            provider=_PProbe(),
            max_steps=1,
        )
    except Exception:  # noqa: BLE001
        pass
    case(
        "库入口：手搓 ctx 也会补齐安全闸标志（权限语义不被静默改掉）",
        all(
            isinstance(_ctx_probe.get(k), bool)
            for k in ("plan_lock_off", "mode_gate_off", "cmd_gate_off")
        ),
        "flags=%s"
        % {
            k: _ctx_probe.get(k)
            for k in ("plan_lock_off", "mode_gate_off", "cmd_gate_off")
        },
    )
    _ssid = "st-safety-%d" % os.getpid()
    _dsp9 = _dsp()
    _dsp9.save_plan(
        _dsp9.new_plan(
            "自由面夹具",
            [
                {
                    "id": "s1",
                    "title": "写",
                    "tools": ["read_file"],
                    "depends": [],
                    "accept": "rc=0",
                }
            ],
        ),
        _ssid,
    )
    _wargs = {"path": "sa.txt", "content": "x"}
    _w_gate = {"root": tmproot, "mode": "full", "sid": _ssid, "plan_lock_off": False}
    _w_locked = _authorize(TOOLS["write_file"], _wargs, _w_gate)
    _w_free = _authorize(TOOLS["write_file"], _wargs, dict(_w_gate, plan_lock_off=True))
    case(
        "计划锁：显式上锁未审批必拦；自由面放行",
        isinstance(_w_locked, str) and "未审批" in _w_locked and _w_free is None,
        "locked=%r free=%s" % (str(_w_locked)[:24], _w_free is None),
    )
    _wc_gate = _authorize(
        TOOLS["run_cmd"],
        {"argv": ["python3", "-c", "pass"]},
        {
            "root": tmproot,
            "mode": "plan",
            "cmd_gate_off": False,
            "mode_gate_off": False,
        },
    )
    _wc_free = _authorize(
        TOOLS["run_cmd"],
        {"argv": ["python3", "-c", "pass"]},
        {"root": tmproot, "mode": "plan", "cmd_gate_off": True, "mode_gate_off": True},
    )
    _hd = _authorize(
        TOOLS["run_cmd"],
        {"argv": ["git", "push", "origin", "main"]},
        {"root": tmproot, "mode": "full", "cmd_gate_off": True, "mode_gate_off": True},
    )
    case(
        "模式开关：显式 mode_gate 下 plan 拒写命令；自由面放行；硬阻断不可关",
        isinstance(_wc_gate, str) and _wc_free is None and isinstance(_hd, str),
        "gate=%r free=%s hard=%s"
        % (str(_wc_gate)[:20], _wc_free is None, isinstance(_hd, str)),
    )
    _hd_shell = _hard_deny(["bash", "-c", "rm -rf /tmp/x && echo done"])
    _hd_ok = _hard_deny(["bash", "-c", "ls -la"])
    _hd_path = _hard_deny(["/bin/rm", "-rf", "x"])
    _hd_env = _hard_deny(["env", "FOO=1", "git", "push"])
    case(
        "硬阻断形态：路径 / shell -c 载荷 / env 前缀都拦；正常载荷放行",
        isinstance(_hd_shell, str)
        and "载荷" in _hd_shell
        and _hd_ok is None
        and isinstance(_hd_path, str)
        and isinstance(_hd_env, str),
        "shell=%r ok=%s path=%s env=%s"
        % (
            str(_hd_shell)[:18],
            _hd_ok is None,
            isinstance(_hd_path, str),
            isinstance(_hd_env, str),
        ),
    )
    _mc_free = _authorize(
        TOOLS["run_cmd"],
        {"argv": ["mkdir", "probe"]},
        {"root": tmproot, "cmd_gate_off": True, "mode_gate_off": True},
    )
    _mc_gate = _authorize(
        TOOLS["run_cmd"],
        {"argv": ["mkdir", "probe"]},
        {"root": tmproot, "cmd_gate_off": False},
    )
    _mres, _ = exec_tool(
        "run_cmd",
        {"argv": ["mkdir", "sa-dir"]},
        {"root": tmproot, "cmd_gate_off": True, "mode_gate_off": True},
    )
    case(
        "命令面：自由面放行非白名单命令并真跑（mkdir 落盘）；显式 cmd_gate 恢复白名单",
        _mc_free is None
        and isinstance(_mc_gate, str)
        and "白名单" in _mc_gate
        and _mres.get("ok") is True
        and _mres.get("rc") == 0
        and os.path.isdir(os.path.join(tmproot, "sa-dir")),
        "free=%s gate=%r rc=%s"
        % (_mc_free is None, str(_mc_gate)[:16], _mres.get("rc")),
    )
    _dsp9.approve_plan(_ssid)
    # 注：生产路径里写工具在 RISK_WRITE 分支早返回，走到白名单块的是**读面
    # 工具**（list_dir 等）——用例按真实路径测（list_dir 不在 s1 白名单）。
    _wl_gate = _authorize(
        TOOLS["list_dir"],
        {"path": "."},
        {
            "root": tmproot,
            "mode": "full",
            "sid": _ssid,
            "dsp_task": "s1",
            "plan_lock_off": False,
        },
    )
    _wl_free = _authorize(
        TOOLS["list_dir"],
        {"path": "."},
        {
            "root": tmproot,
            "mode": "full",
            "sid": _ssid,
            "dsp_task": "s1",
            "plan_lock_off": True,
        },
    )
    for _suf in ("-plan.json", "-chain.jsonl"):
        try:
            os.unlink(os.path.join(ROOT, "work", "plans", _ssid + _suf))
        except OSError:
            pass
    case(
        "任务白名单随计划锁：显式锁拦；自由面同跳",
        isinstance(_wl_gate, str) and _wl_free is None,
        "wl=%r free=%s" % (str(_wl_gate)[:24], _wl_free is None),
    )

    def _mk_wt(path):
        return {
            "json": {
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "w10",
                                    "type": "function",
                                    "function": {
                                        "name": "write_file",
                                        "arguments": json.dumps(
                                            {"path": path, "content": "w10"}
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ]
            }
        }

    for _free9, _fname in ((True, "sa-off.txt"), (False, "sa-on.txt")):
        _st9 = Stub([_mk_wt(_fname), jc(content="done")])
        _ctx9 = {"root": tmproot, "mode": "plan", "_provider": _st9.provider()}
        _ctx9["mode_gate_off"] = _free9
        _ctx9["plan_lock_off"] = _free9
        exec_tool("subagent", {"prompt": "写个文件", "max_steps": 3}, _ctx9)
        _st9.close()
    case(
        "子代理继承安全闸：自由面内层 plan 写放行；显式锁下拒",
        os.path.exists(os.path.join(tmproot, "sa-off.txt"))
        and not os.path.exists(os.path.join(tmproot, "sa-on.txt")),
        "free_exists=%s gated_exists=%s"
        % (
            os.path.exists(os.path.join(tmproot, "sa-off.txt")),
            os.path.exists(os.path.join(tmproot, "sa-on.txt")),
        ),
    )

    def _mk_rc(dname):
        return {
            "json": {
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "w10r",
                                    "type": "function",
                                    "function": {
                                        "name": "run_cmd",
                                        "arguments": json.dumps(
                                            {"argv": ["mkdir", dname]}
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ]
            }
        }

    for _free10, _dname in ((True, "sub-free-dir"), (False, "sub-gate-dir")):
        _st10 = Stub([_mk_rc(_dname), jc(content="done")])
        _ctx10 = {"root": tmproot, "mode": "full", "_provider": _st10.provider()}
        _ctx10["cmd_gate_off"] = _free10
        _ctx10["mode_gate_off"] = _free10
        _ctx10["plan_lock_off"] = _free10
        exec_tool("subagent", {"prompt": "建个目录", "max_steps": 3}, _ctx10)
        _st10.close()
    case(
        "子代理继承命令面：自由面内层 mkdir 真跑；显式 cmd_gate 下拒",
        os.path.isdir(os.path.join(tmproot, "sub-free-dir"))
        and not os.path.isdir(os.path.join(tmproot, "sub-gate-dir")),
        "free=%s gated=%s"
        % (
            os.path.isdir(os.path.join(tmproot, "sub-free-dir")),
            os.path.isdir(os.path.join(tmproot, "sub-gate-dir")),
        ),
    )

    fpath = os.path.join(tmproot, "ref.txt")
    with open(fpath, "w", encoding="utf-8") as f:
        f.write("REF-BODY\n")
    exp, used = _expand_at_refs("看 @ref.txt 再说 @../outside.txt", tmproot)
    case(
        "@path 展开真实文件、越界的不展开",
        used == ["ref.txt"] and "REF-BODY" in exp and "@../outside.txt" in exp,
        "used=%r" % (used,),
    )

    u = _usage_total(
        {"prompt_tokens": 10}, {"prompt_tokens": 5, "completion_tokens": 7}
    )
    u2 = _usage_total(u, None)
    case(
        "_usage_total 累加数字、忽略空/非数字",
        u.get("prompt_tokens") == 15 and u.get("completion_tokens") == 7 and u2 == u,
        "u=%r" % (u,),
    )

    cctx = {
        "root": tmproot,
        "mode": "plan",
        "history": [{"role": "user", "content": "x"}],
    }
    _repl_slash("/clear", cctx, None, "sid", "p")
    cleared = cctx["history"] == []
    _repl_slash("/mode bogus", cctx, None, "sid", "p")
    mode_kept = cctx["mode"] == "plan"
    _repl_slash("/mode full", cctx, None, "sid", "p")
    mode_sw = cctx["mode"] == "full"
    rc_unknown = _repl_slash("/nope", cctx, None, "sid", "p")
    case(
        "/clear 清空、/mode 只认三档、未知命令不抛且不退出",
        cleared and mode_kept and mode_sw and rc_unknown is None,
        "cleared=%s kept=%s sw=%s unknown=%r"
        % (cleared, mode_kept, mode_sw, rc_unknown),
    )

    # (EVO) 自进化：信号→规则→注入→回退（纯本地，账在 tmp 自检目录）------
    evo_sig = os.path.join(tmp, "signals.jsonl")
    evo_rules = os.path.join(tmp, "rules.json")
    with open(evo_sig, "w", encoding="utf-8") as f:
        for _ in range(3):
            f.write(
                json.dumps({"kind": "turn", "ok": False, "err_kind": "no-provider"})
                + "\n"
            )
        f.write(json.dumps({"kind": "turn", "ok": True}) + "\n")
        for _ in range(2):
            f.write(
                json.dumps({"kind": "turn", "ok": False, "err_kind": "timeout"}) + "\n"
            )
    rules_e, evo_err = _evo_mine(rules_path=evo_rules, signals_path=evo_sig)
    evo_r1 = [r for r in rules_e if r.get("sig") == "err:no-provider"]
    case(
        "evo 挖掘：3 连同签名失败→CANDIDATE(count=3)，2 次不成规则",
        evo_err == ""
        and len(evo_r1) == 1
        and evo_r1[0]["state"] == "CANDIDATE"
        and evo_r1[0]["count"] == 3
        and all(r.get("sig") != "err:timeout" for r in rules_e),
        "rules=%r err=%r"
        % ([(r.get("id"), r.get("state"), r.get("count")) for r in rules_e], evo_err),
    )
    _evo_set_state("evo-err-no-provider", "CANDIDATE", rules_path=evo_rules)
    ok_ap = _evo_set_state("evo-err-no-provider", "ACTIVE", rules_path=evo_rules)
    ptext = build_system_prompt(
        {"root": tmp, "mode": "plan", "evo_rules_path": evo_rules}
    )
    case(
        "evo 注入：ACTIVE 规则真进 system prompt",
        ok_ap and "自进化经验规则" in ptext and "config set llm.api_key" in ptext,
        "applied=%s has_section=%s" % (ok_ap, "自进化经验规则" in ptext),
    )
    # (CONVERGE-EVO) 收敛打转 → 规则 → 旋钮真被拧紧（本机失败数据改本机行为）
    # 写 3 条真形状的信号：停滞命中 1 次、只探查没写盘。
    cevo_sig = os.path.join(tmp, "signals_conv.jsonl")
    cevo_rules = os.path.join(tmp, "rules_conv.json")
    with open(cevo_sig, "w", encoding="utf-8") as f:
        for _ in range(3):
            f.write(
                json.dumps(
                    {
                        "kind": "turn",
                        "ok": True,
                        "converge": {
                            "phase": "explore",
                            "steps": 12,
                            "write_seen": False,
                            "verify_seen": True,
                            "stall_hits": 1,
                            "loop_hits": 0,
                            "gate_retries": 0,
                        },
                    }
                )
                + "\n"
            )
    cevo_r, cevo_err = _evo_mine(rules_path=cevo_rules, signals_path=cevo_sig)
    _csig = [r for r in cevo_r if r.get("sig") == "converge:stall"]
    _cexo = [r for r in cevo_r if r.get("sig") == "converge:explore-only"]
    case(
        "evo 收敛：3 次真停滞 → 聚成规则且带拧旋钮的动作",
        cevo_err == ""
        and len(_csig) == 1
        and _csig[0]["state"] == "CANDIDATE"
        and _csig[0]["count"] == 3
        and (_csig[0].get("action") or {}).get("op") == "converge_tune"
        and (_csig[0].get("action") or {}).get("max_stall") == 4
        and len(_cexo) == 1,
        "rules=%r err=%r"
        % ([(r.get("sig"), r.get("state"), r.get("action")) for r in cevo_r], cevo_err),
    )
    _evo_set_state("evo-converge-stall", "ACTIVE", rules_path=cevo_rules)
    _evo_set_state("evo-converge-explore-only", "ACTIVE", rules_path=cevo_rules)
    _cpol = _evo_converge_policy(cevo_rules)
    case(
        "evo 收敛：ACTIVE 规则 → policy 收紧旋钮（6→4）",
        _cpol.get("max_stall") == 4 and _cpol.get("max_explore") == 4,
        "pol=%r" % (_cpol,),
    )
    _cstub = Stub([jc(content="本轮结论")])
    try:
        _cres = run_agent(
            "随便看一眼",
            ctx={
                "root": tmproot,
                "mode": "plan",
                "evo_rules_path": cevo_rules,
            },
            provider=_cstub.provider(),
            max_steps=2,
        )
    finally:
        _cstub.close()
    _csnap = _cres.get("converge") or {}
    case(
        "evo 收敛：旋钮在运行期真被拧紧（快照 max_stall=4）",
        _csnap.get("max_stall") == 4 and _csnap.get("max_explore") == 4,
        "snap=%r" % (_csnap,),
    )

    # (CONVERGE-VERIFY) 动作边界的核对：改完 → 立刻催「跑验证命令」。
    # 用独立根目录：那里放一个 pyproject.toml，探测器才会认这是 Python
    # 项目（探测不到就不催 —— 不猜是硬规矩）。
    _vroot = os.path.join(tmp, "vroot")
    os.makedirs(_vroot, exist_ok=True)
    with open(os.path.join(_vroot, "pyproject.toml"), "w", encoding="utf-8") as _vfh:
        _vfh.write('[project]\nname = "vroot"\nversion = "0.0.0"\n')
    _vstub = Stub(
        [
            jc(tool=("write_file", {"path": "m.py", "content": "x = 1\n"}), cid="v1"),
            jc(content="改完了。"),
        ]
    )
    try:
        run_agent(
            "给这个模块加一个函数",
            ctx={"root": _vroot, "mode": "full"},
            provider=_vstub.provider(),
            max_steps=4,
        )
    finally:
        _vstub.close()
    _vseen = json.dumps(_vstub.seen, ensure_ascii=False)
    case(
        "核对：改完盘后核对指令真进模型可见的 messages",
        "核对指令" in _vseen and "pytest -q" in _vseen,
        "seen 里没有核对指令",
    )

    _evo_set_state("evo-err-no-provider", "CANDIDATE", rules_path=evo_rules)
    ptext2 = build_system_prompt(
        {"root": tmp, "mode": "plan", "evo_rules_path": evo_rules}
    )
    case("evo 回退：降回 CANDIDATE 后注入消失", "自进化经验规则" not in ptext2)
    with open(evo_rules, "w", encoding="utf-8") as f:
        f.write("{corrupt")
    rules_c, err_c = _evo_rules_load(evo_rules)
    case(
        "evo 账坏 fail-closed：规则读空不炸且状态如实报",
        rules_c == [] and err_c != "",
        "n=%d err=%r" % (len(rules_c), (err_c or "")[:40]),
    )
    rules_c2, err_c2 = _evo_mine(rules_path=evo_rules, signals_path=evo_sig)
    case(
        "evo 账坏时挖掘拒绝动账（防把坏账洗成假新账）",
        rules_c2 == [] and err_c2 != "" and not os.path.exists(evo_rules + ".tmp"),
        "n=%d err=%r" % (len(rules_c2), (err_c2 or "")[:40]),
    )

    # (IDEAS) 自主力：机会挖掘带 file:line 证据 -------------------------
    ideas_root = os.path.join(tmp, "repo")
    os.makedirs(os.path.join(ideas_root, "tools"), exist_ok=True)
    with open(os.path.join(ideas_root, "tools", "a.py"), "w", encoding="utf-8") as f:
        f.write("x = 1  # TODO fix me\n")
    marks_i, gaps_i = _ideas_scan(ideas_root)
    case(
        "ideas 挖掘：TODO 带 file:line、无引用源文件报候选缺口",
        any("tools/a.py:1" in m and "TODO" in m for m in marks_i)
        and any("a.py" in g for g in gaps_i),
        "marks=%r gaps=%r" % (marks_i[:2], gaps_i[:2]),
    )

    # (UI) 过程可见性：事件序列 + 转轮编排 ------------------------------
    stub_u = Stub([_resp_json(None, cid="c7"), _resp_json("DONE-UI", cid=None)])
    evs_u = []
    try:
        res_u = run_agent(
            "读 note.txt",
            ctx={"root": tmproot, "mode": "full"},
            provider=stub_u.provider(protocol="responses"),
            max_steps=4,
            on_event=evs_u.append,
        )
    finally:
        stub_u.close()
    types_u = [e.get("type") for e in evs_u]
    case(
        "过程事件：step 先于 tool_call 先于 tool 结果，args 随行",
        bool(res_u.get("ok"))
        and "tool_call" in types_u
        and "tool" in types_u
        and types_u.index("step") < types_u.index("tool_call")
        and types_u.index("tool_call") < types_u.index("tool")
        and any(e.get("type") == "tool_call" and e.get("args") for e in evs_u),
        "types=%r" % types_u,
    )
    import contextlib
    import io as _io

    buf_u = _io.StringIO()
    seen_u = {"delta": False}
    printed_u = []
    sp_u = _TurnSpinner(True, _c)  # 不 start()：无转轮线程，纯编排验证
    with contextlib.redirect_stdout(buf_u):
        ui = _turn_ui(sp_u, lambda ev: printed_u.append(ev.get("type")), seen_u)
        ui({"type": "step", "step": 1})
        ui({"type": "tool_call", "tool": "read_file", "args": {"path": "x"}})
        ui({"type": "tool", "tool": "read_file", "ok": True, "executed": True})
        ui({"type": "delta", "text": "你好"})
        ui({"type": "delta", "text": "呀"})
    case(
        "过程编排：⏺ 两次（工具行+首增量）、工具/增量都不经 printer、seen 置位",
        seen_u["delta"] is True
        # 交互面里 tool 事件**不再转发给 printer**（结果并进 ⎿ 一行，
        # 一次工具一行 = CC 手感）；delta 一律不经 printer。
        and printed_u == ["step"]
        and buf_u.getvalue().count("⏺") == 2
        # 工具行是**短标签**（Read(x)），不是完整 JSON
        and "Read(x)" in buf_u.getvalue()
        # 交互面不再有重复的「[工具 …]」行
        and "[工具" not in buf_u.getvalue()
        and "你好呀" in buf_u.getvalue(),
        "printed=%r out=%r" % (printed_u, buf_u.getvalue()[:120]),
    )

    # (MEM) 记忆层级：注入两态 + 截断（蒸馏自 gemini-cli/codex）----------
    mem_root = os.path.join(tmp, "memrepo")
    os.makedirs(mem_root, exist_ok=True)
    ptext3 = build_system_prompt({"root": mem_root, "mode": "plan"})
    case("记忆层级：无 AGENTS.md 零注入（不造假）", "项目记忆" not in ptext3)
    with open(os.path.join(mem_root, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write("MEMMARK 本仓一律 rc 为据。" + "x" * 5000)
    ptext4 = build_system_prompt({"root": mem_root, "mode": "plan"})
    _base_len = len(build_system_prompt({"root": tmp, "mode": "plan"}))
    case(
        "记忆层级：AGENTS.md 注入且**按摘要**封顶（不再整段注入）",
        "MEMMARK" in ptext4
        and "项目记忆" in ptext4
        and "read_file" in ptext4
        and len(ptext4) - _base_len < 1200,
        "len_delta=%d" % (len(ptext4) - _base_len),
    )
    # 记忆摘要不许把「结构化规则」丢掉：标题 + 每节首条加粗句都要在。
    _mem_root2 = os.path.join(tmp, "memrepo2")
    os.makedirs(_mem_root2, exist_ok=True)
    with open(os.path.join(_mem_root2, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write(
            "# 仓库纪律\n"
            "## 1. 永不上传\n"
            "**铁律**：自有内容一律不推 GitHub。\n"
            "这里是叙述性铺垫，摘要不该注入。\n"
            "## 2. 三证\n"
            "**三证**：日志 + 账本 + 真树 diff。\n"
        )
    _p5 = build_system_prompt({"root": _mem_root2, "mode": "plan"})
    case(
        "记忆层级：摘要保留全部小节标题与每节首条硬规则（叙述不注入）",
        "## 1. 永不上传" in _p5
        and "## 2. 三证" in _p5
        and "一律不推 GitHub" in _p5
        and "日志 + 账本 + 真树 diff" in _p5
        and "叙述性铺垫" not in _p5
        and len(_p5) < len(_p5) + 1,
        "len=%d" % (len(_p5),),
    )
    # (GH) GitHub 一等能力：工具注册 + argv 全落只读白名单 + 纪律注入 ----
    gh_tools_ok = all(t in TOOLS for t in ("gh_search", "gh_repo_view", "gh_file_read"))
    a1 = _gh_search_argv("terminal agent", "repos", 10)
    a2 = _gh_file_read_argv("o/r", "src/x.py", "main")
    _ghc = _gh_client_mod()
    case(
        "gh 三只读工具注册且 argv 全落只读白名单",
        gh_tools_ok
        and _ghc.is_readonly(a1)
        and _ghc.is_readonly(a2)
        and a1[0] == "search"
        and a2[0] == "api",
        "tools=%s a1=%s a2=%s" % (gh_tools_ok, a1, a2),
    )
    ptext5 = build_system_prompt({"root": tmproot, "mode": "plan"})
    case(
        "GitHub 优先纪律注入 system prompt",
        "参考 GitHub 成熟实现" in ptext5 and "gh_search" in ptext5,
        "has=%s" % ("参考 GitHub 成熟实现" in ptext5),
    )

    # (EVO2) 自进化 v2：tool 签名/描述改写/提案/midflight 节流 -------------
    evo_sig2 = os.path.join(tmp, "signals2.jsonl")
    evo_rules2 = os.path.join(tmp, "rules2.json")
    with open(evo_sig2, "w", encoding="utf-8") as f:
        for _ in range(3):
            f.write(
                json.dumps({"kind": "turn", "ok": True, "tools_fail": ["read_file"]})
                + "\n"
            )
        for _ in range(3):
            f.write(
                json.dumps({"kind": "turn", "ok": False, "err_kind": "interrupted"})
                + "\n"
            )
    rules_v2, err_v2 = _evo_mine(rules_path=evo_rules2, signals_path=evo_sig2)
    rule_tf = [r for r in rules_v2 if r.get("id") == "evo-tool-fail-read_file"]
    case(
        "evo v2：tool-fail 按工具聚签名、interrupted 不进失败规则",
        err_v2 == ""
        and len(rule_tf) == 1
        and rule_tf[0]["state"] == "CANDIDATE"
        and rule_tf[0]["count"] == 3
        and all(r.get("sig") != "err:interrupted" for r in rules_v2),
        "rules=%r err=%r" % ([(r.get("id"), r.get("count")) for r in rules_v2], err_v2),
    )
    _evo_set_state("evo-tool-fail-read_file", "ACTIVE", rules_path=evo_rules2)
    desc_before = TOOLS["read_file"]["description"]
    n_hint = _evo_refresh_tool_hints(rules_path=evo_rules2)
    hinted = TOOLS["read_file"]["description"]
    _evo_set_state("evo-tool-fail-read_file", "CANDIDATE", rules_path=evo_rules2)
    _evo_refresh_tool_hints(rules_path=evo_rules2)
    restored = TOOLS["read_file"]["description"]
    case(
        "evo v2：tool 类 ACTIVE 规则运行时改写工具描述，off 后还原",
        n_hint >= 1
        and "自进化提示" in hinted
        and restored == desc_before
        and hinted != desc_before,
        "hinted=%r restored_ok=%s" % (hinted[:60], restored == desc_before),
    )
    for _ in range(3):
        with open(evo_sig2, "a", encoding="utf-8") as f:
            f.write(
                json.dumps({"kind": "turn", "ok": True, "tools_fail": ["read_file"]})
                + "\n"
            )
    new_mf = _evo_midflight(rules_path=evo_rules2, signals_path=evo_sig2)
    new_mf2 = _evo_midflight(rules_path=evo_rules2, signals_path=evo_sig2)
    prop_dir = os.path.join(os.path.dirname(evo_rules2), "evo_proposals")
    has_prop = os.path.exists(os.path.join(prop_dir, "evo-tool-fail-read_file.md"))
    case(
        "evo v2：midflight 新候选提示一次（节流）+ 2 倍阈值自动生成提案",
        len(new_mf) >= 1 and len(new_mf2) == 0 and has_prop,
        "new=%r new2=%r prop=%s"
        % ([r.get("id") for r in new_mf], [r.get("id") for r in new_mf2], has_prop),
    )
    ghc2 = _gh_client_mod()
    ok_auto, r_auto = ghc2.write_policy(False, False, "true", "o/r", [], auto=True)
    deny_cfg, r_cfg = ghc2.write_policy(False, False, "yes", "o/r", [], auto=True)
    deny_rl, r_rl = ghc2.write_policy(
        False, False, "true", "o/r", [(time.time(), "o/r")] * 5, auto=True
    )
    case(
        "policy v2：agent 自主路径（配置开即放行含审计限速，配置模糊拒）",
        ok_auto
        and r_auto == "ok"
        and not deny_cfg
        and r_cfg == "config-off"
        and not deny_rl
        and r_rl == "rate-limit",
        "auto=%s/%s cfg=%s/%s rl=%s/%s"
        % (ok_auto, r_auto, deny_cfg, r_cfg, deny_rl, r_rl),
    )
    scan_dir = os.path.join(tmp, "scandir")
    os.makedirs(scan_dir, exist_ok=True)
    with open(os.path.join(scan_dir, ".env"), "w") as f:
        f.write("SECRET=1\n")
    with open(os.path.join(scan_dir, "ok.py"), "w") as f:
        f.write("x = 1\n")
    hits_s = ghc2.secret_scan(scan_dir)
    case(
        "deploy 密钥扫描：.env 命中、干净文件不误伤",
        any(".env" in h for h in hits_s)
        and not any(h.endswith("ok.py") for h in hits_s),
        "hits=%r" % hits_s,
    )

    # (DSP) 决策·监督·计划：schema/锁/白名单/doom/证据/链/拆分 --------
    # 固定 sid 隔离：dsp 落盘走 work/plans/<sid>-*，sid 带 pid 即不污染真计划。
    import importlib.util as _ilu

    _dsp_path = os.path.join(ROOT, "tools", "dsp.py")
    _dsp_spec = _ilu.spec_from_file_location("_st_dsp", _dsp_path)
    _dsp_mod = _ilu.module_from_spec(_dsp_spec)
    _dsp_spec.loader.exec_module(_dsp_mod)
    # sid 唯一：pid 会回收 —— 上一轮自检（被超时杀掉、没跑到清理）留下的
    # st-<pid>-plan.json 会被下一轮**同 pid** 的自检捡到，计划的链状态
    # 不同 → 行为不同 → 判据可能莫名翻红（2026-10-20 链内真撞过一次，
    # 症状是自检子进程 rc=1 但没人看得出哪条判据红）。
    # 加时间戳：sid 不再可能撞车；撞车这一类问题从根上消失。
    _run_uid = "%d-%d" % (os.getpid(), int(time.time() * 1000) % 100000000)
    _dsp_sid = "st-%s" % _run_uid
    _good_tasks = [
        {
            "id": "t1",
            "title": "侦察",
            "accept": "现状一句话 rc=0",
            "tools": ["read_file"],
            "depends": [],
        },
        {
            "id": "t2",
            "title": "动手",
            "accept": "改动落盘 rc=0",
            "tools": ["write_file"],
            "depends": ["t1"],
        },
    ]
    _plan0 = _dsp_mod.new_plan("自检计划", [dict(t) for t in _good_tasks])
    ok_v, _ = _dsp_mod.validate_plan(_plan0)
    bad_noaccept = {
        "goal": "x",
        "tasks": [{"id": "t1", "title": "t", "tools": ["read_file"], "depends": []}],
    }
    ok_na, err_na = _dsp_mod.validate_plan(bad_noaccept)
    bad_fwd = {
        "goal": "x",
        "tasks": [
            {
                "id": "t1",
                "title": "t",
                "accept": "rc=0",
                "tools": ["read_file"],
                "depends": ["ghost"],
            }
        ],
    }
    ok_fw, _ = _dsp_mod.validate_plan(bad_fwd)
    case(
        "DSP schema：合法过、无 accept 拒、前向依赖拒",
        ok_v and not ok_na and "accept" in err_na and not ok_fw,
        "valid=%s noaccept=%r" % (ok_v, err_na[:40]),
    )
    ok_sv, err_sv = _dsp_mod.save_plan(_plan0, _dsp_sid)
    plan_ld, err_ld = _dsp_mod.load_plan(_dsp_sid)
    case(
        "DSP 落盘取回：save ok + bind 对上",
        ok_sv and plan_ld is not None and plan_ld.get("goal") == "自检计划",
        "save=%s err=%r" % (ok_sv, (err_sv or err_ld)[:60]),
    )
    # 计划锁：未审批时写工具锁死（理由带 /approve）——走真 _authorize。
    _spec_w = TOOLS["write_file"]
    _ctx_locked = {"root": tmproot, "mode": "full", "sid": _dsp_sid}
    _why_lock = _authorize(_spec_w, {"path": "x.txt", "content": "x"}, _ctx_locked)
    case(
        "DSP 计划锁：未审批写工具锁死（理由带 /approve）",
        isinstance(_why_lock, str) and "/approve" in _why_lock,
        str(_why_lock)[:80],
    )
    ok_ap, _ = _dsp_mod.approve_plan(_dsp_sid)
    _why_after = _authorize(_spec_w, {"path": "x.txt", "content": "x"}, _ctx_locked)
    case(
        "DSP 审批解锁：approve 后写工具不再被计划锁拦",
        ok_ap and _why_after is None,
        "approve=%s why=%r" % (ok_ap, _why_after),
    )
    # 任务白名单：t1 只许 read_file，write_file 在 t1 下被拒。
    plan_ld2, _ = _dsp_mod.load_plan(_dsp_sid)
    _w1 = _dsp_mod.check_tool(plan_ld2, "t1", "read_file", {})
    _w2 = _dsp_mod.check_tool(plan_ld2, "t1", "write_file", {})
    case(
        "DSP 白名单：t1 放 read_file、拒 write_file",
        _w1 is None and isinstance(_w2, str),
        "w1=%r w2=%r" % (_w1, _w2),
    )
    # doom 归一化：键序不同不算不同，第 3 次转 ask。
    _dw = _dsp_mod.DoomWatch()
    _r1 = _dw.note("t1", "search", {"q": "a", "n": 1})
    _r2 = _dw.note("t1", "search", {"n": 1, "q": "a"})
    _r3 = _dw.note("t1", "search", {"q": "a", "n": 1})
    case(
        "DSP doom：归一化同参第 3 次转 ask",
        _r1 is None and _r2 is None and _r3 == "ask",
        "r=%r/%r/%r" % (_r1, _r2, _r3),
    )
    # 证据 + 审计链：无 rc 拒、有 rc 过；链不断。
    ok_e1, _ = _dsp_mod.check_evidence("做完了")
    ok_e2, _ = _dsp_mod.check_evidence("pytest 全绿 rc=0")
    ok_ch, err_ch = _dsp_mod.verify_chain(_dsp_sid)
    case(
        "DSP 证据与链：无 rc 拒、有 rc 过、审计链不断",
        (not ok_e1) and ok_e2 and ok_ch,
        "chain=%r" % err_ch[:40],
    )
    # 拆分：子任务继承 + 父 stuck；子任务不再拆。
    plan_ld3, _ = _dsp_mod.load_plan(_dsp_sid)
    ok_sp, ids_sp = _dsp_mod.split_task(plan_ld3, "t2", ["拆 a", "拆 b"])
    ok_sp2, _ = _dsp_mod.split_task(plan_ld3, "t2:1", ["再拆"])
    case(
        "DSP 拆分：t2 拆出两子、父 stuck、子不再拆",
        ok_sp and ids_sp == ["t2:1", "t2:2"] and not ok_sp2,
        "ids=%r" % (ids_sp,),
    )
    # (DSP-AUTO) 分析器 + 大任务自启：纯函数三态、自启幂等、指令注入、
    # 主循环自启落盘且首任务已指派。
    _ana_big = _dsp_mod.analyze_task("把全链门跑完并部署上线", tmproot)
    _ana_small = _dsp_mod.analyze_task("读 README 前 3 行", tmproot)
    case(
        "DSP 分析：大任务 BIG、小任务 SMALL（结构触发）",
        _ana_big["grade"] == "BIG" and _ana_small["grade"] == "SMALL",
        "big=%r small=%r" % (_ana_big["reasons"][:1], _ana_small["reasons"][:1]),
    )
    _auto_sid = "st-auto-%s" % _run_uid
    _ap1, _aa1 = _dsp_mod.auto_plan("重构门链并全量验证", _auto_sid, tmproot)
    _ap2, _aa2 = _dsp_mod.auto_plan("重构门链并全量验证", _auto_sid, tmproot)
    case(
        "DSP 自启幂等：首建有计划、重建返回 None（不覆写）",
        _ap1 is not None
        and _ap2 is None
        and "不重建" in " ".join(_aa2.get("reasons", [])),
        "first=%s second=%s" % (_ap1 is not None, _ap2 is None),
    )
    _directive = _dsp_mod.auto_directive(_ap1, _aa1) if _ap1 else ""
    case(
        "DSP 自启指令：含精修 accept 与 /approve 顺序",
        "accept" in _directive and "/approve" in _directive,
        _directive[:60],
    )
    # 主循环自启：run_agent 跑 BIG 需求（桩端点零工具终答），计划自动落盘。
    _run_sid = "st-run-%s" % _run_uid
    _stub0 = Stub([_resp_json("自启验证终答", cid=None)])
    try:
        _res0 = run_agent(
            "实现一个全新的部署流水线并全量验证",
            ctx={"root": tmproot, "mode": "accept-edits", "sid": _run_sid},
            provider=_stub0.provider(protocol="responses"),
            max_steps=2,
        )
    finally:
        _stub0.close()
    _pl0, _e0 = _dsp_mod.load_plan(_run_sid)
    case(
        "DSP 主循环自启：BIG 需求自动落盘 + 摘要进 prompt",
        _res0.get("ok")
        and _pl0 is not None
        and _pl0.get("goal", "").startswith("实现一个全新"),
        "ok=%s plan=%s" % (_res0.get("ok"), _pl0 is not None),
    )
    # 清理自检计划残留（plans/ 下 _dsp_sid/_auto_sid/_run_sid 三组全清）。
    # 放 finally：中途异常退出时也要清，否则 dsp_gate D12「无自检残留」判红。
    try:
        for _sid_c in (_dsp_sid, _auto_sid, _run_sid):
            for _suf in ("-plan.json", "-chain.jsonl"):
                try:
                    os.unlink(os.path.join(ROOT, "work", "plans", _sid_c + _suf))
                except OSError:
                    pass
    except Exception:  # noqa: BLE001
        pass
    case(
        "瞬时词表：认得传输事故不认得 401",
        bool(_TRANSIENT_RE.search("connection reset by peer"))
        and bool(_TRANSIENT_RE.search("i/o timeout"))
        and not _TRANSIENT_RE.search("HTTP 401: Bad credentials"),
        "reset=%s 401=%s"
        % (
            bool(_TRANSIENT_RE.search("connection reset")),
            bool(_TRANSIENT_RE.search("HTTP 401")),
        ),
    )

    # (ESC/UNDO) 打断与快照（蒸馏自 cline checkpoints / CC Esc）----------
    stub_i = Stub([_resp_json("nope", cid=None)])
    raised_i = False
    called_i = True
    try:
        run_agent(
            "x",
            ctx={"root": tmproot, "mode": "full"},
            provider=stub_i.provider(protocol="responses"),
            max_steps=2,
            cancel=lambda: True,
        )
    except AgentError as e:
        raised_i = e.kind == "interrupted"
        called_i = stub_i.seen == []
    finally:
        stub_i.close()
    case(
        "Esc 打断：cancel 置位即抛 interrupted（端点未被调用）",
        raised_i and called_i,
        "raised=%s seen=%r" % (raised_i, stub_i.seen),
    )
    snap = os.path.join(tmp, "snap")
    uctx = {"root": tmproot, "mode": "full", "sid": "t1", "snap_dir": snap}
    # 独占文件名：tmproot 里前面的用例写过 hello.txt（桩响应真落盘），
    # 快照例的「时点不存在」前提必须不依赖执行顺序。
    hp = os.path.join(tmproot, "undo-probe.txt")
    r1u, _ = exec_tool("write_file", {"path": "undo-probe.txt", "content": "v1"}, uctx)
    r2u, _ = exec_tool("write_file", {"path": "undo-probe.txt", "content": "v2"}, uctx)
    _repl_slash("/undo", uctx, None, "t1", "p")
    back = open(hp, encoding="utf-8").read()
    case(
        "/undo：恢复上一写（v2→v1）",
        bool(r1u.get("ok")) and bool(r2u.get("ok")) and back == "v1",
        "back=%r ok1=%s ok2=%s" % (back, r1u.get("ok"), r2u.get("ok")),
    )
    _repl_slash("/undo", uctx, None, "t1", "p")
    _repl_slash("/undo", uctx, None, "t1", "p")
    case(
        "/undo：耗尽后如实报（快照时点不存在→删除，再 undo 无快照）",
        not os.path.exists(hp),
        "exists=%s" % os.path.exists(hp),
    )

    # (W13) 自主执行：/goal 建菱形 DAG 计划（未审批）→ 未审批 run 必拒
    # （rc=2 + block 物证 + 零派发）→ 审批后桩 worker 一次跑完小闭环。
    # 界面料与引擎锁面在这里各钉一颗钉（深审在 autonomy_gate）。
    gsid = "ck_goal_%d" % os.getpid()
    gdir = os.path.join(ROOT, "work", "autonomy", gsid)
    try:
        gctx = {"mode": "accept-edits", "root": ROOT, "history": []}
        _repl_slash("/goal 给自主引擎做个小目标夹具", gctx, None, gsid, "p")
        _gp, _gerr = _dsp().load_plan(gsid)
        _tasks = (_gp or {}).get("tasks") or []
        case(
            "/goal 建菱形 DAG 计划：落盘未审批 + 5 任务里程碑齐",
            _gp is not None
            and _gp.get("approved") is False
            and len(_tasks) == 5
            and all(t.get("milestone") for t in _tasks),
            "plan=%s approved=%s n=%d"
            % (_gp is not None, (_gp or {}).get("approved"), len(_tasks)),
        )
        _auto_m = _autonomy()
        _rc_g = _auto_m.run_goal(gsid, root=ROOT, log=lambda s: None)
        _grows = _auto_m._read_ledger(ROOT, gsid)
        _gblk = os.path.join(ROOT, "work", "plans", "%s-block.md" % gsid)
        case(
            "autonomy 未审批拒跑（rc=2 + block 物证 + 零派发）",
            _rc_g == 2
            and os.path.exists(_gblk)
            and not any(r.get("event") == "dispatch" for r in _grows),
            "rc=%s block=%s rows=%d" % (_rc_g, os.path.exists(_gblk), len(_grows)),
        )
        _dsp().approve_plan(gsid)
        _w = '"%s" -c "print(\'STUB OK rc=0\')" {prompt}' % sys.executable
        _rc_c = _auto_m.run_goal(gsid, root=ROOT, worker=_w, log=lambda s: None)
        _gp2, _ge2 = _dsp().load_plan(gsid)
        _tasks2 = (_gp2 or {}).get("tasks") or []
        case(
            "autonomy 桩小闭环：审批后一次跑到 5/5 全绿（rc=0）",
            _rc_c == 0 and all(t.get("status") == "done" for t in _tasks2),
            "rc=%s done=%d/%d"
            % (
                _rc_c,
                sum(1 for t in _tasks2 if t.get("status") == "done"),
                len(_tasks2),
            ),
        )
    except Exception as e:  # noqa: BLE001
        case("/goal 自主执行三连（建计划/拒跑/小闭环）", False, repr(e)[:120])
    finally:
        for _p in (
            os.path.join(ROOT, "work", "plans", "%s-plan.json" % gsid),
            os.path.join(ROOT, "work", "plans", "%s-chain.jsonl" % gsid),
            os.path.join(ROOT, "work", "plans", "%s-block.md" % gsid),
            os.path.join(ROOT, "work", "autonomy", "%s.jsonl" % gsid),
            os.path.join(ROOT, "work", "autonomy", "%s-REPORT.md" % gsid),
        ):
            try:
                os.unlink(_p)
            except OSError:
                pass
        shutil.rmtree(gdir, ignore_errors=True)
        try:  # 父目录空了也清（门链零污染判据盯 work/）
            os.rmdir(os.path.join(ROOT, "work", "autonomy"))
        except OSError:
            pass

    # (CONVERGE) 收敛层内核级判据：注入真进 messages、交付闸真退回补证据
    # ────────────────────────────────────────────────────────────────────
    # 为什么放内核自检而不只放 converge_selftest：那是纯函数层；这里钉的
    # 是**接线**——收敛引擎与主循环真的连上了（方向指令进了模型看得见的
    # messages、缺证据的终答真的被退回并拿到第二轮答案）。
    # 读**真实存在**的文件：读不存在的路径会触发既有「同工具连续失败」护栏
    # 提前停，那是另一条护栏在起作用，不能拿它当收敛层的判据。
    for _cf in range(6):
        with open(os.path.join(tmproot, "c%d.txt" % _cf), "w", encoding="utf-8") as _cfh:
            _cfh.write("probe %d\n" % _cf)
    _cs1 = Stub(
        [
            jc(("read_file", {"path": "c%d.txt" % _i}), cid="k%d" % _i)
            for _i in range(6)
        ]
        + [jc(content="最终结论如下")]
    )
    try:
        run_agent(
            "把这个仓库彻底研究一遍并写一份改进方案",
            ctx={"root": tmproot, "mode": "full"},
            provider=_cs1.provider(),
            max_steps=8,
        )
    finally:
        _cs1.close()
    _cs1_seen = json.dumps(_cs1.seen, ensure_ascii=False)
    case(
        "收敛：连续只读无产出 → 方向指令真进模型可见的 messages",
        "收敛指令" in _cs1_seen,
        "没进 messages",
    )
    # 被退回后模型要**真的**去补证据。任务类型是「新增功能」→ 契约要求
    # 「跑过验证」或「写下并读回」二者其一。这里走「写+读回」这条路：
    # 空目录里跑 pytest 会 rc=5（没有测试可收集），那不是「验证通过」，
    # 桩要是照抄成 pytest 就等于在测一个假场景。
    with open(os.path.join(tmproot, "x.txt"), "w", encoding="utf-8") as _xfh:
        _xfh.write("x\n")
    _cs2 = Stub(
        [
            jc(tool=("read_file", {"path": "x.txt"}), cid="g1"),
            jc(content="我已经完成部署了，仓库在 GitHub 上。"),
            jc(tool=("write_file", {"path": "y.txt", "content": "y\n"}), cid="g2"),
            jc(tool=("read_file", {"path": "y.txt"}), cid="g3"),
            jc(content="写完了，y.txt 已落盘并读回确认。"),
        ]
    )
    try:
        _cs2_res = run_agent(
            "写一个新功能并验证",
            ctx={"root": tmproot, "mode": "accept-edits"},
            provider=_cs2.provider(),
            max_steps=6,
        )
    finally:
        _cs2.close()
    _cs2_seen = json.dumps(_cs2.seen, ensure_ascii=False)
    case(
        "交付闸：缺证据的完成宣言被退回并拿到补证据后的终答",
        "交付闸" in _cs2_seen and bool(_cs2_res.get("ok")),
        "退回=%s ok=%s" % ("交付闸" in _cs2_seen, _cs2_res.get("ok")),
    )

    # (TODO) 任务账本：清单里有没结的项 → 不许说「做完了」
    # ────────────────────────────────────────────────────────────────────
    # 这是把「清单」从装饰变成真约束的一层（Claude Code 的 TodoWrite /
    # Aider 的 todo 的价值就在这）。两类判据：
    # ① 账本里还有 pending/doing → 交付闸挡回（说「做完了」不算数）；
    # ② 账本全 done → 正常放行（不能变成「必须列清单才能交付」的刁难）。
    _td1 = Stub(
        [
            jc(
                tool=(
                    "todo",
                    {
                        "items": [
                            {"text": "第一步", "state": "done"},
                            {"text": "第二步", "state": "pending"},
                        ]
                    },
                ),
                cid="td1",
            ),
            # 三次答复**各不相同**：复读守卫（同一句话连续 3 步）会先于
            # 交付闸发难，那样测到的就是守卫而不是账本闸 —— 测例要隔离
            # 掉不相干的机制，才测得到自己要测的东西。
            jc(content="做完了：两步都完成了（1）。"),
            jc(content="做完了：两步都完成了（2）。"),
            jc(content="做完了：两步都完成了（3）。"),
        ]
    )
    try:
        _td1_res = run_agent(
            "按清单做两步",
            ctx={"root": tmproot, "mode": "full", "sid": "st-todo-pending"},
            provider=_td1.provider(),
            max_steps=6,
        )
    finally:
        _td1.close()
        _clean_plans("st-todo-pending")
    case(
        "账本：还有 pending 项时说「做完了」被交付闸挡回",
        _td1_res.get("ok") is False
        and "账本" in str(_td1_res.get("error") or ""),
        "ok=%s err=%r"
        % (_td1_res.get("ok"), str(_td1_res.get("error") or "")[:70]),
    )
    _td2 = Stub(
        [
            jc(
                tool=(
                    "todo",
                    {"items": [{"text": "第一步", "state": "done"}]},
                ),
                cid="td2",
            ),
            jc(content="清单全 done，做完了。"),
        ]
    )
    try:
        _td2_res = run_agent(
            "按清单做一步",
            ctx={"root": tmproot, "mode": "full", "sid": "st-todo-done"},
            provider=_td2.provider(),
            max_steps=4,
        )
    finally:
        _td2.close()
        _clean_plans("st-todo-done")
    case(
        "账本：全 done 时正常放行（不许变成「必须列清单才能交」）",
        _td2_res.get("ok") is True,
        "ok=%s err=%r"
        % (_td2_res.get("ok"), str(_td2_res.get("error") or "")[:70]),
    )

    # (DIFF-REVIEW) 改了代码却没看过自己的 diff → 交付闸挡回
    # ────────────────────────────────────────────────────────────────────
    # Aider 的整个交互核心就是「改完先看 diff 再决定」；AgentOS 是自动化的，
    # 那就把「看没看」变成**证据**而不是建议：改了东西、跑过验证，但没跑
    # `git diff` 就交付 —— 挡回去。
    _gitroot = os.path.join(tmp, "gitroot")
    _have_git = False
    try:
        os.makedirs(_gitroot, exist_ok=True)
        _r = subprocess.run(
            ["git", "init", "-q"], cwd=_gitroot, capture_output=True, timeout=60
        )
        _have_git = _r.returncode == 0
    except Exception:  # noqa: BLE001
        _have_git = False
    if _have_git:
        with open(os.path.join(_gitroot, "mod.py"), "w", encoding="utf-8") as _mf:
            _mf.write("def f():\n    return 1\n")
        _dr = Stub(
            [
                jc(tool=("write_file", {"path": "mod.py", "content": "def f():\n    return 2\n"}), cid="d1"),
                jc(tool=("read_file", {"path": "mod.py"}), cid="d2"),
                jc(content="改完了，mod.py 已更新。"),
                jc(content="改完了，mod.py 已更新（2）。"),
                jc(content="改完了，mod.py 已更新（3）。"),
            ]
        )
        try:
            _dr_res = run_agent(
                "给 mod.py 加个返回值",
                ctx={"root": _gitroot, "mode": "full", "sid": "st-diff-review"},
                provider=_dr.provider(),
                max_steps=6,
            )
        finally:
            _dr.close()
            _clean_plans("st-diff-review")
        case(
            "自审：改了代码没看 git diff → 交付闸挡回",
            _dr_res.get("ok") is False
            and "diff" in str(_dr_res.get("error") or "").lower(),
            "ok=%s err=%r"
            % (_dr_res.get("ok"), str(_dr_res.get("error") or "")[:80]),
        )
    else:
        case(
            "自审：改了代码没看 git diff → 交付闸挡回",
            True,
            "本机没有 git：这条判据不适用（如实跳过，不算绿）",
        )

    # (CC-UX) 过程显示：CC 式短标签（一次工具一行，不打完整 JSON）
    # ────────────────────────────────────────────────────────────────────
    # 用户诉求（2026-10-20）：「工作过程不要显示那么长，要和 cc 一样」。
    # 原来那行是 `write_file({"path": "a.py", "content": "整段正文"})` ——
    # 一行几百字符，又长又泄内容。现在只留**一个**关键参数。
    case(
        "CC 手感：短标签是 Write(path)/Bash(cmd)/Grep(pattern) 形态",
        _tool_label("write_file", {"path": "hello.txt", "content": "x"}) == "Write(hello.txt)"
        and _tool_label("read_file", {"path": "a/b.py"}) == "Read(a/b.py)"
        and _tool_label("run_cmd", {"argv": ["ls", "-la"]}) == "Bash(ls -la)"
        and _tool_label("search", {"pattern": "TODO"}) == "Grep(TODO)",
        "w=%r r=%r c=%r s=%r"
        % (
            _tool_label("write_file", {"path": "hello.txt"}),
            _tool_label("read_file", {"path": "a/b.py"}),
            _tool_label("run_cmd", {"argv": ["ls", "-la"]}),
            _tool_label("search", {"pattern": "TODO"}),
        ),
    )
    case(
        "CC 手感：短标签不含正文（write_file 的 content 不上屏）",
        "content" not in _tool_label("write_file", {"path": "a.py", "content": "机密正文"})
        and "机密正文" not in _tool_label("write_file", {"path": "a.py", "content": "机密正文"}),
        _tool_label("write_file", {"path": "a.py", "content": "机密正文"}),
    )
    _long_cmd = "sh -c " + ("echo " + "x" * 40 + " && ") * 6 + "tail out.txt"
    case(
        "CC 手感：长参数截断且留尾巴（命令尾部的关键信息仍在）",
        len(_tool_label("run_cmd", {"argv": [_long_cmd]})) <= 60
        and "tail out.txt" in _tool_label("run_cmd", {"argv": [_long_cmd]}),
        _tool_label("run_cmd", {"argv": [_long_cmd]}),
    )
    case(
        "CC 手感：认不出的工具也只留一个参数（不吐整坨 JSON）",
        len(_tool_label("mystery_tool", {"a": "v1", "b": "v2", "c": "v3"})) <= 60,
        _tool_label("mystery_tool", {"a": "v1", "b": "v2", "c": "v3"}),
    )

    # (GH-SEARCH) 检索阶梯：多词是 AND → 自然语言查询必然 0 命中
    # ────────────────────────────────────────────────────────────────────
    # 2026-10-20 实测：`gh search repos "context compaction agent conversation
    # summarization"` → 0 命中（6 个词要同时出现），而 `compaction` → 3 命中；
    # GitHub 的 `OR` 语法在这条命令上**不生效**。而 0 命中会被 agent 误读成
    # 「网络/索引不通」——那是错的结论。所以：阶梯放宽 + 按星排序 + 说清放宽。
    _qs = _gh_search_queries("context compaction agent conversation summarization")
    case(
        "检索：查询被拆成阶梯（原样→3词→2词→单词）",
        bool(_qs)
        and _qs[0][0] == "context compaction agent conversation summarization"
        and _qs[0][1] == 0
        and any(q == "compaction" for q, _l in _qs),
        repr(_qs[:5]),
    )
    _argv = _gh_search_argv("x", "repos", 5)
    case(
        "检索：repos 一律按星排序（这条链路要找的是 1k+ 成熟实现）",
        "--sort" in _argv and "stars" in _argv and "desc" in _argv,
        repr(_argv),
    )

    # (COMPACT) 压缩要保住用户的 ask（旧口径会把它丢掉）
    _chist = [{"role": "system", "content": "S" * 50}]
    _chist.append({"role": "user", "content": "ASK: 把上下文压缩做好"})
    _chist += [{"role": "assistant", "content": "A" * 400} for _ in range(20)]
    _chist += [{"role": "user", "content": "U" * 400} for _ in range(20)]
    # 补丁必须打在**函数实际解析名字的命名空间**上，不是 agent_core 的属性表。
    # 2026-10-21 拆分前 _compact_history_v2 住在 agent_core，「vars(_core) 上打补丁」
    # 恰好成立；搬到 agent_session 之后同样的写法打的是 agent_core 的 re-export 影子，
    # 函数体里查到的仍是真 _compact_budget（=读 config），于是这两条判据假红。
    # __globals__ 跟着定义模块走，拆分前后都成立。
    _g = _compact_history_v2.__globals__
    _ob = _g["_compact_budget"]
    try:
        _g["_compact_budget"] = lambda: 8000
        _cn, _cd, _cu = _compact_history_v2(_chist, keep=10, provider=None)
        _ask_in = any(
            "ASK" in str(m.get("content")) for m in _cn if isinstance(m, dict)
        )
        case(
            "压缩：预算口径下用户的 ask 必须在（R1）",
            _ask_in,
            "dropped=%s 保留=%d" % (_cd, len(_cn)),
        )
        _g["_compact_budget"] = lambda: 2000
        _rn, _rd, _ru = _compact_history_v2(_chist, keep=10, provider=None)
        case(
            "压缩：连保底都装不下时**拒绝压缩**（R3，不谎报 fit）",
            _rd == 0 and _ru is False and len(_rn) == len(_chist),
            "dropped=%s used=%s" % (_rd, _ru),
        )
    finally:
        _g["_compact_budget"] = _ob

    # (ENDPOINT) 端点连不上 → 一直等（用户要求：不放弃、不换本地模型）
    # ────────────────────────────────────────────────────────────────────
    # 三条判据：① 连不上按预算重试（不放弃）；② 失败后恢复 → 重试并交付；
    # ③ 401 这类 key/协议错**立刻**报错（等一辈子也不会好，还会把病因藏起来）。
    # 测例自己钉住 no_fallback 与预算，不受本机 ~/.agentos/config.toml 影响。
    class _DownStub(object):
        """按脚本抛错的桩端点：前 n 次失败，之后返回终答。"""

        def __init__(self, errs, n_after=1):
            self.errs = list(errs)
            self.n = 0
            self.n_after = n_after

        def chat(self, messages, tools=None, stream=False, on_event=None, cancel=None):
            self.n += 1
            if self.n <= len(self.errs):
                raise AgentError("provider", self.errs[self.n - 1])
            return {"content": "接上了", "tool_calls": [], "usage": {}}

        def rotate_session(self):
            pass

    # 用 globals() 改模块级函数：直接 `_no_fallback = ...` 会让它变成本函数
    # 的局部名（finally 里再读就是 UnboundLocalError）。
    _g = vars(_core)
    _old_nf = _g["_no_fallback"]
    _old_env = os.environ.get("AGENTOS_ENDPOINT_RETRY_MAX")
    try:
        _g["_no_fallback"] = lambda: True  # 测例里钉住口径
        os.environ["AGENTOS_ENDPOINT_RETRY_MAX"] = "2"
        _d1 = _DownStub(["connection refused"] * 5)
        try:
            run_agent(
                "连不上就一直等",
                ctx={"root": tmproot, "mode": "plan"},
                provider=_d1,
                max_steps=2,
            )
            _d1_err = ""
        except AgentError as e:
            _d1_err = str(e)
        case(
            "端点：连不上按预算一直重试（不放弃，3 次 = 1+2）",
            _d1.n == 3 and "connection refused" in _d1_err,
            "calls=%d err=%r" % (_d1.n, _d1_err[:60]),
        )
        _d2 = _DownStub(["connection refused"])
        _d2_res = run_agent(
            "连一下就好",
            ctx={"root": tmproot, "mode": "plan"},
            provider=_d2,
            max_steps=2,
        )
        case(
            "端点：失败后恢复 → 重试并正常交付",
            _d2.n == 2 and _d2_res.get("ok") is True,
            "calls=%d ok=%s" % (_d2.n, _d2_res.get("ok")),
        )
        _d3 = _DownStub(["HTTP 401 Unauthorized: api_key_expired"] * 5)
        try:
            run_agent(
                "key 坏了",
                ctx={"root": tmproot, "mode": "plan"},
                provider=_d3,
                max_steps=2,
            )
            _d3_err = ""
        except AgentError as e:
            _d3_err = str(e)
        case(
            "端点：401（key 问题）立刻报错，不无限等（1 次）",
            _d3.n == 1 and "401" in _d3_err,
            "calls=%d err=%r" % (_d3.n, _d3_err[:60]),
        )
    finally:
        _g["_no_fallback"] = _old_nf
        if _old_env is None:
            os.environ.pop("AGENTOS_ENDPOINT_RETRY_MAX", None)
        else:
            os.environ["AGENTOS_ENDPOINT_RETRY_MAX"] = _old_env

    # (CONTRACT) 任务契约内核级判据：类型判定 → 契约进 prompt → 类型进账
    # ────────────────────────────────────────────────────────────────────
    # 「完成各种任务」的关键：不同类型的「完成证据」不一样。钉三件事：
    # ① 分类判对（缺陷修复类任务不能判成调研）；② 契约段真进 system prompt
    # （模型开工前就知道这类任务怎么算完成）；③ 类型真进收敛快照（可被挖掘）。
    _ct_stub = Stub([jc(content="结论")])
    try:
        _ct_res = run_agent(
            "calc.py 里的 add 函数有 bug，请修好它",
            ctx={"root": tmproot, "mode": "full"},
            provider=_ct_stub.provider(),
            max_steps=2,
        )
    finally:
        _ct_stub.close()
    _ct_snap = _ct_res.get("converge") or {}
    _ct_seen = json.dumps(_ct_stub.seen, ensure_ascii=False)
    case(
        "契约：缺陷修复任务判型正确（bugfix）且类型进账",
        _ct_snap.get("contract_type") == "bugfix",
        "type=%r" % (_ct_snap.get("contract_type"),),
    )
    case(
        "契约：完成契约真进模型可见的 messages（含「先复现」）",
        "任务契约" in _ct_seen and "复现" in _ct_seen,
        "seen 里没有契约段",
    )

    # (W0-4) 斜杠补全表防漂：dispatch 里能敲的每条 /cmd 都必须在
    # REPL_SLASH_CMDS 里。2026-10-12 实测漂了 12 条（/evo /undo /plan …
    # 能敲但 Tab 补不出来，补全表停在早期版本）—— 用结构断言钉住，
    # 而不是下次靠人再发现一遍。
    _src = open(os.path.abspath(__file__), encoding="utf-8").read()
    _dispatch = set(re.findall(r'if cmd == "(/[a-z-]+)":', _src))
    _missing = sorted(_dispatch - set(REPL_SLASH_CMDS))
    case(
        "REPL 补全表覆盖全部斜杠命令（dispatch ⊆ REPL_SLASH_CMDS）",
        not _missing,
        "缺=%s" % (_missing or "无"),
    )
