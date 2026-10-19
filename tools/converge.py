#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""converge —— 收敛引擎：让 agent 从「探查很多」走向「交付一个结果」。

为什么需要它（2026-10-19 实测事故）
------------------------------------
真实终端里给 agent 派「去 GitHub 部署 1k+ 星标项目提升自己」，它跑了
**23 步 / 900 秒 / 86 次工具调用 / 0 次交付**：read_file×10、run_cmd×7、
list_dir×4 在原地打转，最后撞墙钟预算报错退出。病根不是模型笨，是**内核
没有收敛契约**：

1. 没有阶段意识 —— 探查、动手、验证、交付混在一起，没有「现在该动了」的信号；
2. 没有循环熔断 —— 同一调用重复 N 次没人喊停；
3. 没有交付闸 —— 只要模型说了「完成」，内核就当完成，不问「证据呢」。

所以本模块把「收敛」做成**可判定的状态机**，而不是寄望模型自觉：

    EXPLORE ──(该动手了)──▶ ACT ──(有改动)──▶ VERIFY ──(有 rc 证据)──▶ DELIVER

三个机制，各自有判据：
· **阶段机**（phase）：由工具语义推进阶段，到 ACT/VERIFY 边界时向模型注入
  「不要再读了，去做 / 去验」的方向指令（不打断，只在边界注入）。
· **循环熔断**（loop）：同一 (工具, 参数指纹) 重复 ≥3 次即注入「换个做法」指令。
· **停滞探测**（stall）：连续 N 步没有任何**产出性**调用（写盘成功 / 命令 rc=0）
  即注入「必须二选一：动手改，或立刻交付现状并说清卡点」。
· **交付闸**（gate）：最终回答前检查是否真有验证证据；没有就把「去跑一次验证」
  当作一条待办退回给模型，逼它补证据而不是空口交付。

纯标准库、无副作用、可单测；所有判据都能在 tools/converge_selftest.py 里证伪。
"""

import hashlib
import json
import os
import re

# ── 阶段 ────────────────────────────────────────────────────────────
P_EXPLORE = "explore"
P_ACT = "act"
P_VERIFY = "verify"
P_DELIVER = "deliver"

# 探查类工具：只读、只找信息
_EXPLORE_TOOLS = frozenset(
    (
        "read_file",
        "list_dir",
        "search",
        "gh_search",
        "gh_repo_view",
        "gh_file_read",
        "git_log",
        "git_status",
        "health",
    )
)

# 产出类工具：写盘
_WRITE_TOOLS = frozenset(("write_file", "edit_file"))

# 验证类工具：跑命令看真实 rc
_VERIFY_TOOLS = frozenset(("run_cmd", "git_diff"))
# 读回也算核对（write 后 read 是最常见的验证形态，不该被判「无证据」）
_VERIFY_READBACK = frozenset(("read_file",))

# 交付前的「至少要有一条真证据」判据（按任务字面推断）
_DELIVER_HINT = (
    "现在必须收敛到交付：给出最终结论 + 支撑它的真实 rc/文件行号。"
    "不要再探查。"
)

# 终答自称「做完了」——规则 C（零工具却宣称完成）用它。
_CLAIMS_DONE = re.compile(
    r"已完成|已经完成|完成了|已部署|部署完成|已实现|已修复|已新增|已加上|"
    r"完成了部署|搞定|(?<![\w-])(?:deployed|finished|done)(?![\w-])",
    re.I,
)

# 终答诚实报告「没做成」——这类回答**不算**未交付，不该被闸退回。
_REPORTS_FAILURE = re.compile(
    r"被拒|未执行|没有做|没能|无法|不能|被拦|被拒了|失败|未完成|没有改动|未改动",
    re.I,
)

def _fingerprint(tool, args):
    """(工具, 参数) 指纹：同参同工具才算重复；换参数不算。

    为什么参数也要进指纹：agent 换参数重试是正常探路（推进了），同参
    重复才是真打转。只按工具名判会把「连读 10 个不同文件」误判成循环。
    """
    try:
        blob = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        blob = repr(args)
    h = hashlib.sha1(("%s|%s" % (tool, blob)).encode("utf-8")).hexdigest()[:12]
    return "%s:%s" % (tool, h)


def _is_verify_cmd(args):
    """run_cmd 的参数像不像「验证」。

    判据是**命令名关键词**，不是靠模型自述：跑 pytest / ls / cat / git diff
    这类命令会产出可核对的 rc 事实；跑 ls 也能证明「东西在那儿」。
    """
    argv = (args or {}).get("argv")
    if isinstance(argv, str):
        argv = argv.split()
    if not isinstance(argv, list) or not argv:
        return False
    joined = " ".join(str(x) for x in argv).lower()
    return any(
        k in joined
        for k in (
            "pytest",
            "unittest",
            "npm test",
            "test",
            "make ",
            "ls ",
            "cat ",
            "git diff",
            "git status",
            "git log",
            "python3 -c",
            "python -c",
            "node -e",
            "sh -c",
            "bash -c",
        )
    )


class Converge:
    """一轮任务的收敛状态机。调用方（run_agent）每个工具调用后喂一次。

    参数
    ----
    max_explore：探查阶段最多容忍几步（到边界就催「去动手」）。
    max_stall  ：连续多少步没有产出性调用即判停滞。
    loop_repeat：同一指纹重复几次即熔断。
    """

    def __init__(
        self,
        max_explore=6,
        max_stall=6,
        loop_repeat=3,
        task_text="",
        mode="",
    ):
        self.max_explore = int(max_explore)
        self.max_stall = int(max_stall)
        self.loop_repeat = int(loop_repeat)
        self.task_text = str(task_text or "")
        self.mode = str(mode or "")
        self.phase = P_EXPLORE
        self.steps = 0
        self.fps = {}  # 指纹 -> 次数
        self.since_productive = 0
        self.write_seen = False
        self.verify_seen = False
        self.productive_seen = False
        self.last_fps = None
        self.repeat_fps = None
        self.repeat_count = 0
        self.dirty = False  # 有没有产出性调用
        self.notes = []  # 注入过的指令（供自检/审计）
        self.hints = []  # 交付闸退回的待办
        # 任务字面像不像「要做出东西」——决定要不要强推 VERIFY
        # 需求点名要验证/测试 → 交付前必须拿出核对证据。
        self.demands_verify = any(
            k in self.task_text.lower()
            for k in ("验证", "校验", "测试", "验一下", "verify", "test", "check ")
        )
        # 关键词刻意保守：「做」太宽泛（"做一次泛化测试"也会命中），
        # 只有明确指向「产出/改动」的词才算「像要构建」。
        self.implies_build = any(
            k in self.task_text.lower()
            for k in (
                "实现",
                "写",
                "改",
                "部署",
                "创建",
                "修",
                "新增",
                "生成",
                "build",
                "create",
                "implement",
                "add ",
                "fix",
            )
        )

    # ── 观测 ────────────────────────────────────────────────────────
    def observe(self, tool, args, ok=True, rc=None):
        """喂一次工具调用结果。返回本步是否算「产出性」。

        ok=False（工具被拒/失败）不算产出 —— 停滞探测要区分「在动但没推动」
        和「压根没动」。
        """
        self.steps += 1
        tool = str(tool or "")
        fp = _fingerprint(tool, args)
        self.fps[fp] = self.fps.get(fp, 0) + 1
        if fp == self.last_fps:
            self.repeat_count += 1
        else:
            self.repeat_count = 1
            self.last_fps = fp
        if self.fps[fp] >= self.repeat_fps_repeat():
            self.repeat_fps = fp

        productive = False
        if tool in _WRITE_TOOLS and ok:
            self.write_seen = True
            self.dirty = True
            productive = True
        elif tool in _VERIFY_TOOLS and ok:
            self.verify_seen = True
            if tool == "run_cmd" and rc in (0, None):
                productive = True
            elif tool == "git_diff":
                productive = True
        elif tool in _VERIFY_READBACK and ok:
            self.verify_seen = True
        if productive:
            self.productive_seen = True
            self.since_productive = 0
        else:
            self.since_productive += 1

        # 阶段推进（单向：ACT 之后不回 EXPLORE，避免「改一点又去读」的抖动）
        if self.phase == P_EXPLORE:
            if tool in _WRITE_TOOLS or (tool == "run_cmd" and rc == 0 and self.implies_build):
                self.phase = P_ACT
        elif self.phase == P_ACT:
            if self.verify_seen:
                self.phase = P_VERIFY
        elif self.phase == P_VERIFY:
            if self.verify_seen and self.write_seen:
                self.phase = P_DELIVER
        return productive

    def repeat_fps_repeat(self):
        return self.loop_repeat

    # ── 指令注入 ────────────────────────────────────────────────────
    def directive(self):
        """给模型的一句话方向指令（没有就返回 None）。

        优先级：停滞 > 循环 > 阶段边界。三者互斥，一次只给一条，避免
        指令噪声把模型淹掉。
        """
        if self.since_productive >= self.max_stall and self.steps > 2:
            msg = (
                "[收敛指令] 连续 %d 步没有产出（没有写盘成功，也没有跑出 rc=0）"
                "，现在二选一：① 立刻动手改一处；② 立刻交付当前结论并写清卡点。"
                "不要再重复读同样的文件。" % self.since_productive
            )
            self.since_productive = 0  # 注入过就重置计数，别每步都喊
            self.notes.append(msg)
            return msg
        if self.repeat_count >= self.loop_repeat and self.fps.get(self.repeat_fps, 0) >= self.loop_repeat:
            msg = (
                "[收敛指令] 你已用同样的方式（%s）重复 %d 次无进展；"
                "换一条完全不同的路径（换工具、换文件、换思路），别再重复它。"
                % (str(self.repeat_fps).split(":")[0], self.fps.get(self.repeat_fps, 0))
            )
            self.repeat_count = 0
            self.notes.append(msg)
            return msg
        if self.phase == P_EXPLORE and self.steps >= self.max_explore:
            msg = (
                "[收敛指令] 已探查 %d 步，信息够了。现在去**动手**"
                "（写盘/改文件/跑命令），不要继续读。"
                % self.steps
            )
            self.notes.append(msg)
            return msg
        if self.phase == P_ACT and self.verify_seen is False and self.steps > self.max_explore:
            msg = (
                "[收敛指令] 改动已落地，但还没验证过。现在跑一次真命令"
                "（pytest / ls / git diff）拿到 rc，再交付。"
            )
            self.notes.append(msg)
            return msg
        return None

    # ── 交付闸 ──────────────────────────────────────────────────────
    def gate(self, answer):
        """交付前判定。返回 (ok, 原因)。

        ok=False 时，调用方应把「去补证据」当待办退回给模型再走一轮，
        而不是把没有证据的「完成」当完成放行。

        判据不只看任务字面（那会漏掉「模型自己以为很简单」的任务），
        还看**终答自称完成**却有没有验证证据——这正是 900 秒那次事故的
        形状：说了一堆「已完成」，全程没跑出一条真 rc。
        """
        text = (answer or "").strip()
        if not text:
            return False, "最终回答为空"
        # 诚实报告失败是**真交付**（说清了没做成 + 原因），不退回。
        if _REPORTS_FAILURE.search(text):
            return True, ""
        # 规则 A：像是要产出、动过工具、却什么都没写 → 只探查不动手，退回。
        # 这正是 900 秒那次事故的形状（探查 86 次、一步没落地）。
        if self.implies_build and self.steps > 0:
            if not self.write_seen and not self.dirty:
                return False, "任务像是要做东西，但全程只有探查没有写盘/改动证据"
        # 规则 B：需求**点名要验证**却没验 → 退回。
        # 判据来自需求字面（「验证/测试/跑一次」），不是「所有改动都得验」——
        # 否则「把第二行改掉」这种小事会被无端卡住。
        if self.demands_verify and self.steps > 0 and not self.verify_seen:
            return False, "需求要求验证，但没有跑命令也没有读回任何证据"
        # 规则 C：一次工具都没调，却宣称做完了 → 空口交付，退回。
        if self.steps == 0 and _CLAIMS_DONE.search(text):
            return False, "零工具调用却宣称完成（没有任何事实依据）"
        return True, ""

    def require_evidence(self):
        """交付闸退回时给模型的一句话（逼它补证据）。"""
        msg = (
            "[交付闸] 上一版终答缺证据被退回：%s。"
            "现在必须给出可核对的真实 rc 或文件行号，然后重写终答。"
            % (self.hints[-1] if self.hints else "缺少验证证据")
        )
        self.hints.append(self.hints[-1] if self.hints else "缺少验证证据")
        return msg

    # ── 审计 ────────────────────────────────────────────────────────
    def snapshot(self):
        return {
            "phase": self.phase,
            "steps": self.steps,
            "since_productive": self.since_productive,
            "write_seen": self.write_seen,
            "verify_seen": self.verify_seen,
            "dirty": self.dirty,
            "repeat_fp": self.repeat_fps,
            "repeat_count": self.repeat_count,
            "notes": len(self.notes),
        }


def convergence_report(path=None):
    """读真实信号账，出一份「收敛画像」——给门和自测看趋势用。"""
    if path is None:
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..",
            "work",
            "agent_sessions",
            "signals.jsonl",
        )
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rows.append(json.loads(ln))
                except ValueError:
                    continue
    except OSError:
        return {"turns": 0, "avg_tools": 0.0, "long_turns": 0, "no_tool_turns": 0}
    n = len(rows)
    if not n:
        return {"turns": 0, "avg_tools": 0.0, "long_turns": 0, "no_tool_turns": 0}
    tools_total = sum(len(r.get("tools") or []) for r in rows)
    long_turns = sum(1 for r in rows if len(r.get("tools") or []) >= 15)
    no_tool = sum(1 for r in rows if not (r.get("tools") or []))
    return {
        "turns": n,
        "avg_tools": round(tools_total / float(n), 2),
        "long_turns": long_turns,
        "no_tool_turns": no_tool,
    }


if __name__ == "__main__":  # 手动看一眼真实信号画像
    print(json.dumps(convergence_report(), ensure_ascii=False, indent=1))
