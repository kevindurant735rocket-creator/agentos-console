#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""replay_bench —— 真实会话回放基准：让 agent 的**行为改进可量化**。

为什么需要它
------------
自进化闭环目前只能证明「规则被合入了、门是绿的」，**证明不了行为真的变好了**。
「自进化」如果没有可测量的指标，就只是自我感觉。

做法
----
把真实跑过的会话录成**语料**（prompt + 工具序列 + 终答），再用**桩 provider**
原样重放。桩保证确定性：同一份语料每次回放结果完全一样。
然后做 **A/B 对照**：

- **引擎开**（默认）：现在的 agent
- **引擎关**（ctx["converge"]=False）：同一个 agent，但收敛引擎被摘掉

差值就是引擎的**真实贡献**。我们在真实事故里抓到的三种形状各自成一条语料：

1. `stall-read-loop`：连读十几个文件不落地（真实跑过 4 轮，stall_hits=1~2）
2. `deliver-with-evidence`：写盘→读回→带 rc 交付（真实跑过，成功）
3. `false-claim`：全程只读，终答说「已经完成了」——**900 秒那次事故的形状**

第 3 条是关键：引擎关掉时它会「成功交付」一个**零证据的完成宣言**；
引擎开着时交付闸会把它退回去，逼出真证据。**指标 difference 就是这个。**

指标（模式无关，从 result 里的真实工具记录算）
----------------------------------------------
- `delivered`     ：有没有拿到终答
- `false_claim`   ：终答成功交付，但全程既没写盘也没核对（**空口完成**）
- `interventions` ：引擎为了纠偏插了几次指令（停滞/循环）
- `steps`         ：用了几步（多 = 绕路）

用法
----
    python3 tools/replay_bench.py            # 跑基准，打印 A/B 报告
    python3 tools/replay_bench.py --check    # 门模式：有回归就非零退出
"""

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import agent_core as ac  # noqa: E402

CORPUS_PATH = os.path.join(ROOT, "work", "replay_corpus.jsonl")
BASELINE_PATH = os.path.join(ROOT, "work", "replay_baseline.json")

WRITE_TOOLS = ("write_file", "edit_file")
VERIFY_TOOLS = ("run_cmd", "git_diff", "read_file")


# ── 语料 ────────────────────────────────────────────────────────────
def load_corpus(path=None):
    p = path or CORPUS_PATH
    rows = []
    try:
        with open(p, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rows.append(json.loads(ln))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows


def _stub_entry(item):
    """把语料里的一步变成桩 provider 的一条响应。"""
    if "tool" in item and item["tool"]:
        msg = {
            "role": "assistant",
            "content": item.get("content") or "",
            "tool_calls": [
                {
                    "id": item.get("cid") or "c1",
                    "type": "function",
                    "function": {
                        "name": item["tool"],
                        "arguments": json.dumps(item.get("args") or {}),
                    },
                }
            ],
        }
        fin = "tool_calls"
    else:
        msg = {"role": "assistant", "content": item.get("content") or ""}
        fin = "stop"
    return {
        "json": {
            "choices": [
                {"index": 0, "finish_reason": fin, "message": msg}
            ]
        }
    }


def _seed_files(root, files):
    for rel, content in (files or {}).items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)


def _score_with_gate(used, answer, rec, args_seq=None):
    """用交付闸的判据复算这一轮终答：返回 (通过?, 原因)。

    args_seq：与 used 一一对应的**真参数**（来自语料的 script）。
    必须传真参数：引擎判「跑过 pytest」算核对、判「跑过 ls」不算 ——
    评分若拿不到参数就只能按「rc=0 就算核对」兜底，两侧尺子就不一样了，
    A/B 的差值就失去意义。
    """
    try:
        import converge as cv
    except Exception:  # noqa: BLE001
        return True, ""
    c = cv.Converge(task_text=str(rec.get("prompt") or "")[:400])
    args_seq = list(args_seq or [])
    for i, u in enumerate(used or []):
        a = args_seq[i] if i < len(args_seq) else {}
        try:
            c.observe(
                u.get("tool") or "",
                a if isinstance(a, dict) else {},
                ok=bool(u.get("ok")),
                rc=u.get("rc"),
            )
        except Exception:  # noqa: BLE001
            continue
    try:
        return c.gate(answer or "")
    except Exception:  # noqa: BLE001
        return True, ""


def run_record(rec, engine_on=True):
    """回放一条语料，返回指标字典。"""
    tmp = tempfile.mkdtemp(prefix="replay_")
    _seed_files(tmp, rec.get("files"))
    script = [_stub_entry(it) for it in rec.get("script") or []]
    stub = ac.Stub(script)
    ctx = {
        "root": tmp,
        "mode": rec.get("mode") or "full",
    }
    if not engine_on:
        ctx["converge"] = False
    evs = []
    try:
        res = ac.run_agent(
            rec.get("prompt") or "",
            ctx=ctx,
            provider=stub.provider(),
            max_steps=int(rec.get("max_steps") or 12),
            on_event=evs.append,
        )
    finally:
        stub.close()
    used = res.get("tools_used") or []
    n_write = sum(
        1
        for u in used
        if u.get("tool") in WRITE_TOOLS and u.get("ok")
    )
    n_verify = sum(
        1
        for u in used
        if u.get("tool") in VERIFY_TOOLS and u.get("ok")
    )
    snap = res.get("converge") or {}
    delivered = bool(res.get("ok"))
    # 「空口完成」的判据 = **交付闸自己的判据**（模式无关）：
    # 把这一轮真实发生的工具调用喂给一份评分用引擎，再问它同不同意
    # 这条终答。同意 = 有证据；不同意 = 空口完成。
    # 用引擎自己的尺子量，两侧（A/B）才可比。
    # 语料里的工具项与实际执行顺序一一对应 → 取出真参数给评分用
    _script_tools = [
        (it.get("args") or {}) for it in (rec.get("script") or []) if it.get("tool")
    ]
    _gate_ok, _gate_why = _score_with_gate(
        used, res.get("answer") or "", rec, _script_tools
    )
    return {
        "id": rec.get("id"),
        "delivered": delivered,
        "false_claim": bool(delivered and not _gate_ok),
        "steps": int(res.get("steps") or 0),
        "n_write": n_write,
        "n_verify": n_verify,
        "interventions": int(snap.get("stall_hits") or 0)
        + int(snap.get("loop_hits") or 0),
        "gate_retries": int(snap.get("gate_retries") or 0),
        "answer": (res.get("answer") or "")[:120],
        "error": str(res.get("error") or "")[:120],
    }


def run_all(corpus=None, engine_on=True):
    rows = corpus if corpus is not None else load_corpus()
    return [run_record(r, engine_on=engine_on) for r in rows]


def summarize(results):
    n = len(results)
    return {
        "records": n,
        "delivered": sum(1 for r in results if r["delivered"]),
        "false_claims": sum(1 for r in results if r["false_claim"]),
        "interventions": sum(r["interventions"] for r in results),
        "gate_retries": sum(r["gate_retries"] for r in results),
        "steps": sum(r["steps"] for r in results),
    }


def compare(corpus=None):
    """A/B：引擎开 vs 引擎关。返回 (on, off, diff)。"""
    c = corpus if corpus is not None else load_corpus()
    on = summarize(run_all(c, engine_on=True))
    off = summarize(run_all(c, engine_on=False))
    diff = {
        "false_claims_avoided": off["false_claims"] - on["false_claims"],
        "delivered_delta": on["delivered"] - off["delivered"],
        "steps_delta": on["steps"] - off["steps"],
    }
    return on, off, diff


def _fmt(tag, s):
    return (
        "%-8s 交付=%d/%d 空口完成=%d 干预=%d 闸退回=%d 总步数=%d"
        % (
            tag,
            s["delivered"],
            s["records"],
            s["false_claims"],
            s["interventions"],
            s["gate_retries"],
            s["steps"],
        )
    )


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    check = "--check" in argv
    corpus = load_corpus()
    if not corpus:
        sys.stderr.write("语料为空：%s\n" % CORPUS_PATH)
        return 1
    on, off, diff = compare(corpus)

    if not check:
        print("== 回放基准：真实会话语料 A/B ==")
        print(_fmt("引擎开", on))
        print(_fmt("引擎关", off))
        print(
            "差值：空口完成避免=%+d 交付差=%+d 步数差=%+d"
            % (
                diff["false_claims_avoided"],
                diff["delivered_delta"],
                diff["steps_delta"],
            )
        )
        for r in run_all(corpus, engine_on=True):
            print(
                "  - %-24s 交付=%s 空口=%s 步=%d 干预=%d 闸退回=%d"
                % (
                    r["id"],
                    r["delivered"],
                    r["false_claim"],
                    r["steps"],
                    r["interventions"],
                    r["gate_retries"],
                )
            )
        return 0

    # ── 门模式 ──────────────────────────────────────────────────────
    fails = []
    # 1) 语料够不够（少于 3 条就没有对照价值）
    if len(corpus) < 3:
        fails.append("语料不足 3 条（当前 %d）" % len(corpus))
    # 2) 引擎必须真的更安全：空口完成必须被压住
    if on["false_claims"] > off["false_claims"]:
        fails.append(
            "回归：引擎开时空口完成 %d > 引擎关 %d（引擎没起到纠偏作用）"
            % (on["false_claims"], off["false_claims"])
        )
    # 3) 引擎开着时不允许有空口完成（硬指标）
    if on["false_claims"] > 0:
        fails.append("引擎开时仍有 %d 条空口完成" % on["false_claims"])
    # 4) 确定性：同一份语料两次回放结果必须一致
    again = summarize(run_all(corpus, engine_on=True))
    if again != on:
        fails.append("回放不确定：两次结果不同 %r vs %r" % (on, again))
    # 5) 基线不漂移：与钉住的基线比，交付数不许掉
    if os.path.exists(BASELINE_PATH):
        try:
            with open(BASELINE_PATH, encoding="utf-8") as f:
                base = json.load(f)
            if base.get("on", {}).get("delivered") is not None:
                if on["delivered"] < base["on"]["delivered"]:
                    fails.append(
                        "回归：交付数 %d < 基线 %d"
                        % (on["delivered"], base["on"]["delivered"])
                    )
                if on["false_claims"] > base["on"].get("false_claims", 0):
                    fails.append(
                        "回归：空口完成 %d > 基线 %d"
                        % (on["false_claims"], base["on"].get("false_claims", 0))
                    )
        except (OSError, ValueError) as e:
            fails.append("基线读不了：%s" % e)
    else:
        # 第一次跑：把当前成绩钉成基线
        try:
            os.makedirs(os.path.dirname(BASELINE_PATH), exist_ok=True)
            with open(BASELINE_PATH, "w", encoding="utf-8") as f:
                json.dump(
                    {"on": on, "off": off, "diff": diff},
                    f,
                    ensure_ascii=False,
                    indent=1,
                    sort_keys=True,
                )
        except OSError as e:
            fails.append("基线写不了：%s" % e)

    for f in fails:
        print("FAIL %s" % f)
    print(
        "REPLAY_BENCH %d/%d  引擎开 %s"
        % (
            5 - len(fails),
            5,
            json.dumps(on, ensure_ascii=False),
        )
    )
    if fails:
        print("REPLAY_BENCH_RED")
        return 1
    print("REPLAY_BENCH_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
