#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""neg_agent_core —— 反向门：证明 agent_core_gate 与 agent_core 的判据有牙。

一门只会返回 0 不算有牙。本门每条判据都跑两边：
  BASE —— 生产代码 + 真实坏输入 → 判据必须说「有问题」；
  掏空 —— 把同一份结果换成被掏空的版本 → 判据必须说「没问题」。
前者抓「能力被删」（真把 git push 放进白名单，这里立刻红）；后者抓
「判据空转」（判据写成 `if True: return 1` 之类always-red 的假牙，或
反过来永远判 OK 的死门）。

再加四条**源码级变异体**：把 agent_core.py 拷到 work/ 下、逐行改坏一处
（摘硬阻断 / plan 不再拒写 / 沙箱不比边界 / 截断不标注），真跑那份的
`--check`，要求正向门 verdict() 判红、且红的正是目标用例。活文件全程
只读，变异副本结尾 unlink，一个字节都不留在项目里。

纯 stdlib、不碰浏览器、不占端口，约 15-20s。
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import agent_core as ac                      # noqa: E402
import agent_core_gate as gate               # noqa: E402

PY = sys.executable
WORK = os.path.join(ROOT, "work")
CASES = []


def case(name, ok, detail=""):
    ok = bool(ok)
    CASES.append((name, ok))
    print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                       ("  -- " + detail) if detail else ""))
    return ok


def tooth(name, detect, base, dead):
    """一条判据的牙：生产行为判 OK，掏空版本判 NOT OK，两者都要成立。"""
    try:
        b = bool(detect(base))
    except Exception as e:                     # noqa: BLE001
        b = False
        base = "detect(BASE) 抛异常：%r" % e
    try:
        d = bool(detect(dead))
    except Exception as e:                     # noqa: BLE001
        d = False                          # 掏空样本让判据崩了 → 视为判红
        dead = "detect(掏空) 抛异常：%r" % e
    detail = "BASE=OK 掏空=caught" if (b and not d) else \
             "BASE=%s 掏空=%s" % (b, d)
    return case("%s（BASE 判 OK + 掏空判 NOT OK）" % name, b and not d,
                detail)

# 掏空样本：一个「看起来成功」的工具结果。判据对它必须说不。
DEAD_TOOL = {"ok": True, "executed": True, "rc": 0, "tool": "x"}


def jc(tool=None, content="", cid="c1"):
    m = {"role": "assistant", "content": content}
    fin = "stop"
    if tool is not None:
        fn, args = tool
        m["tool_calls"] = [{"id": cid, "type": "function",
                            "function": {"name": fn,
                                         "arguments": json.dumps(
                                             args, ensure_ascii=False)}}]
        fin = "tool_calls"
    return {"json": {"choices": [{"index": 0, "message": m,
                                  "finish_reason": fin}]}}


def _norm_tooth(normfn, entry):
    """拿指定 _norm_completion 去跑一次桩请求，看是否归 provider。"""
    stub = ac.Stub([entry])
    old = ac._norm_completion
    ac._norm_completion = normfn
    try:
        try:
            stub.provider().chat([{"role": "user", "content": "x"}])
            return False
        except ac.AgentError as e:
            return e.kind == "provider"
        except Exception:                      # noqa: BLE001
            return False
    finally:
        ac._norm_completion = old
        stub.close()


def _no_provider_tooth(resolver, home):
    """在空 HOME 下解析端点：正确实现必须抛 no-provider。"""
    old = os.environ.get("HOME")
    os.environ["HOME"] = home
    try:
        try:
            resolver({})
            return False
        except ac.AgentError as e:
            return e.kind == "no-provider"
    finally:
        if old is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old


def _run_copy(path):
    return subprocess.run([PY, path, "--check"], capture_output=True,
                          text=True, timeout=900)


MUTANTS = [
 ("摘掉硬阻断（git push 不再被拦）",
  'if head == "git" and len(low) > 1',
  "    if False and head == \"git\" and len(low) > 1 \\",
  "git push 在 full"),
 ("plan 模式不再拒写",
  'if mode == "plan":', "    if False:", "plan 模式拒写"),
 ("沙箱不再比边界（越界放行）",
  "if real != rroot and not real.startswith(rroot + os.sep):",
  "    if False:", "越界"),
 ("截断不再标注 truncated",
  'tag = "\\n...[truncated', '    tag = "\\n...[cropped %d/%d bytes]"',
  "_cap_output 截断"),
]


def main():
    print("== neg_agent_core：每条判据都要同时抓住「能力被删」和「判据空转」")
    tmpdir = os.path.join(WORK, "_neg_agent_core")
    os.makedirs(tmpdir, exist_ok=True)
    made = []

    # ── A. 正向门的令牌判据自己有没有牙 -----------------------
    token_ok = "".join("PASS 用例 %d\n" % i for i in range(gate.EXPECT_N)) \
        + gate.TOKEN + "\n"
    short = "".join("PASS 用例 %d\n" % i for i in range(gate.EXPECT_N - 1)) \
        + "PASS 少了一条\n" \
        + "AGENT_CORE OK %d/%d\n" % (gate.EXPECT_N - 1, gate.EXPECT_N)
    laundered = "PASS 用例 0\nFAIL 越界 write_file 被拒\n" \
        + "PASS a\nPASS b\n" * 12 + gate.TOKEN + "\n"
    case("verdict：正常输出判 0", gate.verdict(token_ok) == 0,
         "%d 条 PASS + %s" % (gate.EXPECT_N, gate.TOKEN))
    case("verdict：少一条用例（22/23）判非 0",
         gate.verdict(short) != 0, "少一条也算红")
    case("verdict：FAIL 行被混进来判非 0",
         gate.verdict(laundered) != 0, "有 FAIL 就不许绿")
    case("verdict：令牌被改字判非 0",
         gate.verdict(token_ok.replace("AGENT_CORE OK", "AGENT_CORE  OK")) != 0,
         "逐字比对，不玩模糊匹配")

    # ── B. 硬阻断 -------------------------------------------------
    tooth("_hard_deny 拦 git push", lambda argv: bool(ac._hard_deny(argv)),
         ["git", "push"], ["git", "status"])
    tooth("_hard_deny 拦 rm -rf", lambda argv: bool(ac._hard_deny(argv)),
         ["rm", "-rf", "/tmp/x"], ["ls", "-1"])
    cmds = set(ac._term_cmds())
    case("只读白名单里没有 rm / dd / curl",
         not ({"rm", "dd", "curl", "sudo"} & cmds),
         "白名单 %d 条" % len(cmds))

    # ── C. 沙箱 ---------------------------------------------------
    root = os.path.realpath(tmpdir)
    case("_sandbox 拦住 ../../etc/passwd",
         bool(ac._sandbox("../../../../etc/passwd", root)[1]),
         ac._sandbox("../../../../etc/passwd", root)[1] or "")

    esc = os.path.join(root, "escape_link")
    try:
        if os.path.islink(esc) or os.path.exists(esc):
            os.unlink(esc)
    except OSError:
        pass
    os.symlink("/etc", esc)
    r1, e1 = ac._sandbox("escape_link/passwd", root)
    case("_sandbox 拦住指向 /etc 的符号链接（realpath 之后再比）",
         e1 is not None and r1 is None, e1 or "没拦住！")
    r2, e2 = ac._sandbox("ok.txt", root)
    case("_sandbox 放行项目根内的正常路径",
         e2 is None and r2 == os.path.join(root, "ok.txt"),
         "e2=%r" % e2)

    # ── D. provider 解析不 fail-open -----------------------------
    empty_home = os.path.join(tmpdir, "no_codex_home")
    os.makedirs(empty_home, exist_ok=True)
    tooth("resolve_provider 解析不到端点时抛 no-provider（不 fail-open）",
          lambda fn: _no_provider_tooth(fn, empty_home),
          ac.resolve_provider,
          lambda env: {"base_url": "http://127.0.0.1:1/v1", "model": "x",
                       "headers": {}})

    # ── E. 端点坏结果归 provider，不猜成成功 ----------------------
    tooth("响应形状不对时归 provider",
          lambda fn: _norm_tooth(fn, {"json": {"nope": 1}}),
          ac._norm_completion,
          lambda obj: {"content": "", "tool_calls": [],
                       "finish_reason": "stop", "usage": {}})
    stub = ac.Stub([{"status": 503, "json": {"error": "down"}}])
    try:
        try:
            stub.provider().chat([{"role": "user", "content": "x"}])
            kind = ""
        except ac.AgentError as e:
            kind = e.kind
    finally:
        stub.close()
    case("非 200 归 provider（503 直接冒烟）", kind == "provider", "kind=%s" % kind)

    # ── F. SSE 按 index 累积 -------------------------------------
    def sse(o):
        return "data: " + json.dumps(o, ensure_ascii=False)
    stream_text = "\n\n".join([
        sse({"choices": [{"index": 0, "delta": {"content": "Hel"}}]}),
        sse({"choices": [{"index": 0, "delta": {"content": "lo, "}}]}),
        sse({"choices": [{"index": 0, "delta": {"content": "world"}}]}),
        sse({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "c0", "function": {"name": "read_",
                                                  "arguments": "{\"pa"}}]}}]}),
        sse({"choices": [{"index": 0, "finish_reason": "tool_calls",
                          "delta": {"tool_calls": [
                              {"index": 0, "function": {
                                  "arguments": "th\":\"a\"}"}},
                              {"index": 1, "id": "c1", "function": {
                                  "name": "health", "arguments": "{}"}}]}}]}),
    ]) + "\n\n"
    stub = ac.Stub([{"sse": stream_text}])
    try:
        got = stub.provider().chat([{"role": "user", "content": "hi"}],
                                   stream=True)
    finally:
        stub.close()
    last_wins = {"content": "world", "tool_calls": [
        {"id": "c0", "function": {"name": "read_", "arguments": "{}"}}]}
    tooth("SSE content 拼接 + tool_calls 按 index 累积",
          lambda c: c.get("content") == "Hello, world"
          and len(c.get("tool_calls") or []) == 2
          and c["tool_calls"][0]["function"]["arguments"] == '{"path":"a"}'
          and c["tool_calls"][1]["function"]["name"] == "health",
          got, last_wins)

    # ── G. 主循环：预算 / tool_call_id / 权限 ---------------------
    stub = ac.Stub([jc(("read_file", {"path": "nope-%d.txt" % i}),
                       cid="c%d" % i) for i in range(3)])
    try:
        lim = ac.run_agent("一直读不存在的文件",
                           ctx={"root": root, "mode": "full"},
                           provider=stub.provider(), max_steps=2)
    finally:
        stub.close()
    tooth("step 预算触顶 -> kind=limit 且终答为空",
          lambda r: r.get("kind") == "limit" and r.get("answer") is None
          and r.get("steps") == 2,
          lim, {"ok": True, "kind": "ok", "answer": "蒙一个答案", "steps": 2})

    stub = ac.Stub([jc(("write_file", {"path": "neg_ok.txt",
                                       "content": "x"}), cid="c9"),
                    jc(None, "NEG-DONE")])
    try:
        two = ac.run_agent("写一个文件",
                           ctx={"root": root, "mode": "accept-edits"},
                           provider=stub.provider(), max_steps=4)
    finally:
        stub.close()
    ids_a = [tc.get("id") for m in two["messages"]
             if m.get("role") == "assistant"
             for tc in (m.get("tool_calls") or [])]
    ids_t = [m.get("tool_call_id") for m in two["messages"]
             if m.get("role") == "tool"]
    broken = [dict(m, tool_call_id="") if m.get("role") == "tool" else m
              for m in two["messages"]]
    tooth("tool 消息的 tool_call_id 与 assistant 对得上",
          lambda ms: [m.get("tool_call_id") for m in ms
                      if m.get("role") == "tool"] == ids_a[:len(ids_a)],
          two["messages"], broken)
    case("run_agent 真写了文件（neg_ok.txt 在盘上）",
         os.path.isfile(os.path.join(root, "neg_ok.txt")),
         "bytes=%s" % os.path.getsize(os.path.join(root, "neg_ok.txt"))
         if os.path.isfile(os.path.join(root, "neg_ok.txt")) else "没有文件")

    planf = os.path.join(root, "neg_plan.txt")
    r, ex = ac.exec_tool("write_file", {"path": "neg_plan.txt",
                                        "content": "x"},
                         {"root": root, "mode": "plan"})
    tooth("plan 模式拒写（executed=False）",
          lambda res: res.get("ok") is False and res.get("executed") is False,
          r, DEAD_TOOL)
    case("plan 模式那个文件确实没落地", not os.path.exists(planf),
         "exists=%s" % os.path.exists(planf))

    r, ex = ac.exec_tool("run_cmd", {"argv": ["python3", "-c",
                                             "open(1,'w').write('')"]},
                         {"root": root, "mode": "accept-edits"})
    tooth("accept-edits 下 python3 -c 无审批者 -> 拒",
          lambda res: res.get("ok") is False and "审批" in res.get("error", ""),
          r, DEAD_TOOL)

    r, ex = ac.exec_tool("run_cmd", {"argv": ["python3", "-c",
                                             "open('neg_no.txt','w').write('1')"]},
                         {"root": root, "mode": "accept-edits",
                          "approver": lambda *a: False})
    tooth("approver 返回 False -> 未执行",
          lambda res: res.get("ok") is False and res.get("executed") is False,
          r, DEAD_TOOL)
    case("approver 说 No 时文件没出现",
         not os.path.exists(os.path.join(root, "neg_no.txt")),
         "exists=%s" % os.path.exists(os.path.join(root, "neg_no.txt")))

    r, ex = ac.exec_tool("run_cmd", {"argv": ["git", "push"]},
                         {"root": root, "mode": "full",
                          "approver": lambda *a: True})
    tooth("full + 审批者全点头，git push 仍被硬阻断",
          lambda res: res.get("ok") is False and "硬阻断" in res.get("error", ""),
          r, DEAD_TOOL)

    # ── H. 输出截断必须标注 --------------------------------------
    tooth("_cap_output 超限时显式标注 truncated",
          lambda s: "truncated" in s and len(s) <= 1004,
          ac._cap_output("x" * 100000, limit=1000), "x" * 990)

    # ── I. 工具的坏输入不崩 --------------------------------------
    r, ex = ac.exec_tool("no_such_tool", {}, {"root": root, "mode": "full"})
    tooth("未知工具 -> 结构化拒绝且未执行",
          lambda res: res.get("ok") is False and res.get("executed") is False
          and "未知工具" in res.get("error", ""),
          r, DEAD_TOOL)
    r, ex = ac.exec_tool("read_file", "我不是对象", {"root": root,
                                                  "mode": "full"})
    tooth("工具参数不是对象 -> 结构化拒绝",
          lambda res: res.get("ok") is False and res.get("executed") is False,
          r, DEAD_TOOL)

    # ── J. 源码级变异体：正向门必须对它们判红 --------------------
    src = open(ac.__file__, encoding="utf-8").read()
    ctl = os.path.join(WORK, "_neg_ctl_agent_core.py")
    with open(ctl, "w", encoding="utf-8") as f:
        f.write(src)
    made.append(ctl)
    cp = _run_copy(ctl)
    case("对照：原样拷贝也能 --check 绿（证明变异红不是搬运造成的）",
         cp.returncode == 0 and gate.verdict(cp.stdout) == 0
         and gate.TOKEN in cp.stdout,
         "rc=%d" % cp.returncode)

    for i, (label, anchor, repl, want_fail) in enumerate(MUTANTS, 1):
        mp = os.path.join(WORK, "_neg_mut%d_agent_core.py" % i)
        n_hit = src.count(anchor)
        if n_hit != 1:
            case("变异体 %d：%s —— 锚点命中 %d 次（要求恰好 1），跳过"
                 % (i, label, n_hit), False,
                 "源码变了，先把锚点改准再跑")
            continue
        lines = src.splitlines(True)
        out = []
        for ln in lines:
            if anchor in ln:
                out.append(repl + "\n")
            else:
                out.append(ln)
        with open(mp, "w", encoding="utf-8") as f:
            f.write("".join(out))
        made.append(mp)
        try:
            r = _run_copy(mp)
        except subprocess.TimeoutExpired:
            case("变异体 %d：%s --check 超时（不算红）" % (i, label), False,
                 ">900s")
            continue
        red_lines = [ln for ln in r.stdout.splitlines()
                     if ln.startswith("FAIL ")]
        hit = any(want_fail in ln for ln in red_lines)
        case("变异体 %d：%s -> --check 判红，且红的正是目标用例"
             % (i, label),
             r.returncode != 0 and gate.verdict(r.stdout) != 0 and hit,
             "rc=%d FAIL 行=%r 命中=%s"
             % (r.returncode, [ln[:44] for ln in red_lines[:3]], hit))

    # ── 收尾：一个字节都不留在项目里 ----------------------------
    def _wipe(path):
        if os.path.isdir(path) and not os.path.islink(path):
            for n in os.listdir(path):
                _wipe(os.path.join(path, n))
            os.rmdir(path)
        else:
            os.unlink(path)

    for p in made:
        try:
            os.unlink(p)
        except OSError:
            pass
    try:
        _wipe(tmpdir)
    except OSError:
        pass
    left = [n for n in os.listdir(WORK)
            # 自检临时目录带 pid，故用前缀匹配而不是全等。
            if n.startswith("_neg_") or n.startswith("_st_agent_core")]
    case("变异副本与夹具全部清空（work/ 无残留）", not left,
         "残留=%r" % left)

    n_ok = sum(1 for _, ok in CASES if ok)
    bad = [n for n, ok in CASES if not ok]
    if bad:
        print("NEG_AGENT_CORE RED %d/%d：%s"
              % (len(bad), len(CASES), "; ".join(bad)))
        return 1
    print("NEG_AGENT_CORE OK %d/%d" % (n_ok, len(CASES)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
