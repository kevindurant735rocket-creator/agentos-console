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
· **失败修复回路**（fix）：核对类命令 rc≠0 时，把「哪条命令失败 / rc 多少 /
  输出尾部」记进账本，下一步给模型一条**可定位**的修复指令（蒸馏自 SWE-agent
  的 ACI：把工具失败转成可行动的观察，而不是让模型猜）。有界：同一轮最多
  `max_fix_hints`（默认 3）次，超过就不再催 —— 不许变成「改一次试一次」的死循环。
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

# 「像核对」的单词与形态（整词匹配，避免 latest/greatest 之类误中）
# 「核对类」命令：要么是测试/静态检查，要么是**看一眼文件真实内容/大小/哈希**。
# 第二类不能漏：2026-10-20 真机实测，agent 写完文件后跑的是
# `cat -n notes.md` / `wc -c notes.md` / `sha256sum notes.md`（三条 rc=0，
# 内容、字节数、哈希都核过了）—— 交付闸却因为词表里没有 cat/sha256sum
# 而判「没有跑命令也没有读回任何证据」，把**真核对过**的交付误拒了。
# 假红比漏红更坏：它教会 agent「核对了也不算」，于是下一次就干脆不核了。
_VERIFY_WORDS = frozenset(
    (
        # 测试 / 静态检查
        "pytest",
        "unittest",
        "tox",
        "nox",
        "mypy",
        "ruff",
        "flake8",
        "pylint",
        "jest",
        "vitest",
        "eslint",
        # 文件核对（内容 / 大小 / 哈希 / 存在性）
        "cat",
        "head",
        "tail",
        "wc",
        "sha256sum",
        "shasum",
        "md5",
        "stat",
        "ls",
        "grep",
        "diff",
        "cmp",
        "file",
        "du",
    )
)
_VERIFY_PAIRS = frozenset(
    (
        ("npm", "test"),
        ("yarn", "test"),
        ("pnpm", "test"),
        ("go", "test"),
        ("cargo", "test"),
        ("make", "test"),
        ("make", "check"),
        ("git", "diff"),
        ("git", "status"),
        ("git", "log"),
        ("git", "show"),
    )
)

# 「可核对的来源」：URL 或 带行号的文件路径。
# 调研类任务的完成证据就是它 —— 核对不上的结论不算结论。
_CITATION = re.compile(
    r"https?://\S+|[\w./-]+\.[A-Za-z]{1,5}:\d+|`[^`]+/[^`]+`|`[^`]+\.[A-Za-z]{1,5}`"
)


def _has_citation(answer):
    return bool(_CITATION.search(str(answer or "")))


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
    # usedforsecurity=False：这是**指纹**（判「是不是同一次调用」），
    # 不是口令/签名，不需要抗碰撞。显式标出来，静态安全扫描也就不会
    # 把它当成「用了弱哈希做安全用途」而报警。
    h = hashlib.sha1(
        ("%s|%s" % (tool, blob)).encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:12]
    return "%s:%s" % (tool, h)


def _out_tail(text, max_chars=600, max_lines=8):
    """取输出的**尾部**（失败根因通常在最后几行）。

    截断是硬要求：把整屏输出刷进上下文，等于用噪声淹掉真正的报错
    （SWE-agent 的 ACI 也是只回可行动的片段）。
    """
    s = str(text or "")
    if not s.strip():
        return ""
    lines = [ln.rstrip() for ln in s.splitlines() if ln.strip()]
    tail_lines = lines[-max_lines:]
    out = "\n".join(tail_lines)
    if len(out) > max_chars:
        out = "…" + out[-max_chars:]
    return out


def _cmdline(args):
    """把 args 里的 argv 拼成一条人可读命令行（失败指令要指名道姓）。"""
    if not isinstance(args, dict):
        return ""
    av = args.get("argv")
    if isinstance(av, str):
        return av
    if isinstance(av, (list, tuple)):
        return " ".join(str(x) for x in av)
    return str(args.get("cmd") or "")


def _fix_detail(f):
    """把一条失败记录渲染成**可行动**的修复指令尾部（失败点/rc/输出尾部）。

    三样东西缺一不可：哪条命令失败、rc 多少、输出长什么样。
    少任何一样，模型就只能猜 —— 那正是「已知缺口第 1 条」。
    """
    f = f or {}
    parts = [
        "[修复指令] 上一步核对**没通过**（rc≠0）：命令 `%s` 退出码 rc=%s。"
        % (f.get("cmd") or "(未记录)", f.get("rc"))
    ]
    tail = f.get("tail") or ""
    if tail:
        parts.append("输出尾部（已截断，只贴最后几行）：\n%s" % tail)
    else:
        parts.append(
            "这条命令没有输出可看 —— 先想办法让失败**说出原因**"
            "（加 -v/--traceback，或分成小步跑）。"
        )
    parts.append(
        "先**定位**失败点再改：读上面那段报错指向的文件与行号，改那一处，"
        "然后重跑 `%s` 直到 rc=0。别整片重写，也别换个命令绕开它。"
        % (f.get("cmd") or "同一条命令")
    )
    return "\n".join(parts)


def _is_verify_cmd(args):
    """这条命令像不像「核对」。

    判据是**整词 + runner/verb 形态**，不是子串匹配。踩过的坑：
    子串匹配 `"test" in joined` 会把 `cat latest.txt`、`grep greatest` 判成
    验证；`ls -la`、`cat x`、`sh -c "curl …"` 也都曾被算成核对 ——
    「跑过命令」不等于「核对过改动」。宁可严：判不准就当没验。

    形态一：测试/检查类命令整词出现（pytest / ruff / mypy / jest …）
    形态二：<runner> <verb>（npm test / go test / make test / git diff …）
    形态三：python -m pytest（`-m` 后必须跟 pytest/unittest）
    """
    argv = (args or {}).get("argv")
    if isinstance(argv, str):
        argv = argv.split()
    if not isinstance(argv, list) or not argv:
        return False
    toks = [str(x).lower() for x in argv]
    # shell 包装：`bash -c "pytest -q"` 里真正的命令在第三个参数里。
    # 不拆开就会让「包了一层 shell 的核对」被误判成没核对（误伤好行为）。
    if (
        len(toks) >= 3
        and toks[0] in ("sh", "bash", "zsh", "dash")
        and toks[1] in ("-c", "-lc")
    ):
        inner = toks[2].split()
        if inner and _is_verify_cmd({"argv": inner}):
            return True
    for t in toks:
        if t in _VERIFY_WORDS:
            return True
    for i in range(len(toks) - 1):
        pair = (toks[i], toks[i + 1])
        if pair not in _VERIFY_PAIRS:
            continue
        if toks[i + 1] == "-m":
            # `python -m <东西>`：只有 -m pytest / -m unittest 算核对
            if len(toks) > i + 2 and toks[i + 2] in ("pytest", "unittest"):
                return True
            continue
        return True
    return False


class Converge:
    """一轮任务的收敛状态机。调用方（run_agent）每个工具调用后喂一次。

    参数
    ----
    max_explore：探查阶段最多容忍几步（到边界就催「去动手」）。
    max_stall  ：连续多少步没有产出性调用即判停滞。
    loop_repeat：同一指纹重复几次即熔断。
    max_fix_hints：同一轮里「核对失败 → 修复提示」最多发几次（有界，防死循环）。
    """

    def __init__(
        self,
        max_explore=6,
        max_stall=6,
        loop_repeat=3,
        max_fix_hints=3,
        task_text="",
        mode="",
        verify_info=None,
        contract=None,
        require_diff_review=False,
    ):
        self.max_explore = int(max_explore)
        self.max_stall = int(max_stall)
        self.loop_repeat = int(loop_repeat)
        # 有界失败修复回路：上限只许小（默认 3），大到失去「有界」意义就钳住。
        self.max_fix_hints = max(1, min(int(max_fix_hints), 5))
        self.task_text = str(task_text or "")
        self.mode = str(mode or "")
        # 「这个仓库跑什么命令能证明改动是对的」——由 tools/verify_cmd.py
        # 探测（本地蒸馏 aider 的 edit→auto-lint/test 设计）。
        # 探测不到就是 None：宁可不说，也不猜一条必然失败的命令。
        self.verify_info = dict(verify_info) if verify_info else None
        # 改了还没核对（verify_pending）与本轮是否已经催过（verify_nagged）。
        # 催一次就够：连催会把噪声灌进模型，反而不看。
        self.verify_pending = False
        self.verify_nagged = False
        # 最近一次改动的核对结论：""=还没验 / "pass" / "fail"。
        # 对应上游 aider 的 `self.test_outcome = not test_errors`
        # ——「这次改动过没过检查」必须是**可查的结论**，不是印象。
        self.verify_outcome = ""
        # ── 任务契约（W26）：不同任务类型的「完成证据」不一样 ──────────
        # 契约只做两件事：① 决定闸门要哪几类证据；② 在动作边界提醒
        # 「这类任务该怎么干」（比如缺陷修复要先复现）。
        self.contract = dict(contract) if contract else None
        # 证据账：写过/读过的路径、复现红绿、跑过的命令
        self.write_paths = set()
        self.read_paths = set()
        self.repro_fail_seen = False      # 见过「红」：某次验证命令失败
        self.fix_after_repro = False      # 红之后动过手（改过）
        self.repro_green = False          # 红→改→绿，闭环成立
        self.cmd_ran = False              # 跑过成功的命令（可复算证据）
        # 自审 diff（Aider 式的「改完先看改动」）：改了东西却没看过自己的
        # diff 就交付，等于闭眼提交。记在引擎里，闸门据此要证据。
        self.diff_reviewed = False
        # 需要自审 diff（只有 git 仓库才要：非 git 仓库没有 diff 可看）
        self.require_diff_review = bool(require_diff_review)
        self.n_reads = 0                  # 读过的文件数（查证类证据）
        self.write_blocked = False        # 写过但没落地（被环境锁住/写失败）
        self._repro_nagged = False        # 复现提醒只发一次（不轰炸模型）
        self.phase = P_EXPLORE
        self.steps = 0
        self.fps = {}  # 指纹 -> 次数
        self.since_productive = 0
        self.write_seen = False
        self.verify_seen = False
        # 收尾证据（2026-10-22）：提交过一次（commit 工具成功）→ 树是干净的
        self.commit_seen = False
        self.commit_files = []
        self.productive_seen = False
        self.last_fps = None
        self.repeat_fps = None
        self.repeat_count = 0
        self.dirty = False  # 有没有产出性调用
        self.gate_retries = 0  # 交付闸退回次数（证据不足被退了几回）
        # 分类型命中计数：自进化账靠它分辨「是停滞还是循环还是只探查」，
        # 再决定拧哪个旋钮（收紧错了旋钮 = 治错了病）。
        self.stall_hits = 0
        self.loop_hits = 0
        self.phase_hits = 0
        self.notes = []  # 注入过的指令（供自检/审计）
        self.hints = []  # 交付闸退回的待办
        # ── 有界失败修复回路（已知缺口第 1 条）────────────────────────
        # 核对类命令 rc≠0 时记一条 {cmd, rc, tail}，下一步给模型一条**可定位**
        # 的修复指令（蒸馏自 SWE-agent 的 ACI：把工具失败转成可行动的观察）。
        # 有界：fix_hints 是已发条数，到 max_fix_hints 就不再催 ——
        # 「改一次试一次」的死循环比不催更坏。
        self.fix_failures = 0     # 本轮核对失败的次数（账本要能看见）
        self.fix_hints = 0        # 已发过的修复指令条数
        self.fix_pending = None   # 还没告诉模型的失败（dict），None=没有
        self.fix_suppressed = False  # 配额用完，已停止催促
        self.last_fail = None     # 最近一次失败记录（供 snapshot 审计）
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
    def observe(self, tool, args, ok=True, rc=None, out=None):
        """喂一次工具调用结果。返回本步是否算「产出性」。

        ok=False（工具被拒/失败）不算产出 —— 停滞探测要区分「在动但没推动」
        和「压根没动」。

        out：命令的原始输出（可选）。核对类命令 rc≠0 时用它取**输出尾部**，
        让下一步的修复指令能带「失败长什么样」，而不是只说「失败了」。
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
        verified_now = False
        _path = ""
        if isinstance(args, dict):
            _path = str(args.get("path") or "")
        if tool in _WRITE_TOOLS and not ok:
            # 写没落地（被拒/越界/参数错）：环境不让写或写不成。
            # 记下来 —— 契约不能要求一份**够不着**的证据。
            self.write_blocked = True
        if tool in _WRITE_TOOLS and ok:
            self.write_seen = True
            self.dirty = True
            productive = True
            # 改了 → 进入「待核对」态。验证要贴着动作发生：
            # 连改十个文件只在末尾验一次，前九个坏了也不知道
            # （蒸馏自 aider 的改完自动跑 lint/test）。
            self.verify_pending = True
            self.verify_nagged = False
            self.verify_outcome = ""  # 新改动 → 上一次的结论作废
            if _path:
                self.write_paths.add(_path)
            if self.repro_fail_seen:
                # 「红」之后动过手 —— 这是红→绿链的中间一环
                self.fix_after_repro = True
        elif tool in _VERIFY_TOOLS and ok:
            if tool == "run_cmd":
                # 「跑过命令」≠「核对过」：curl/wget 这类只是取回东西，
                # 不构成对本轮改动的核对。有参数就按命令名判；没参数
                # （离线评分路径只喂工具名）就按 rc=0 兜底。
                # 跑起来了就是「可复算」的证据（分析/运维类任务的硬要求）：
                # 结论引用的 rc 与输出，别人能照着再跑一遍。
                self.cmd_ran = True
                # 「看过自己的 diff」也算核对（git diff / git diff --stat）
                if isinstance(args, dict):
                    _av = args.get("argv")
                    if isinstance(_av, str):
                        _av = _av.split()
                    if isinstance(_av, (list, tuple)) and any(
                        "diff" in str(_x) for _x in _av
                    ):
                        self.diff_reviewed = True
                _vc = (rc in (0, None))
                if _vc and (not args or _is_verify_cmd(args)):
                    self.verify_seen = True
                    productive = True
                    verified_now = True
                    self.verify_outcome = "pass"
                    # 修好了：旧失败记录作废，别拿它继续催（催过期失败=噪声）
                    self.fix_pending = None
                    if self.fix_after_repro:
                        self.repro_green = True  # 红→改→绿，闭环成立
                elif not _vc and (not args or _is_verify_cmd(args)):
                    # 跑了检查但没过：这是**结论**（fail），不是「没验」。
                    # 记下来，别让账本只留下「验过」两个字。
                    self.verify_outcome = "fail"
                    self.repro_fail_seen = True  # 这就是「红」
                    self._record_failure(args, rc, out)
                # rc≠0 的检查**不算**核对通过：它证明的是「还没好」，
                # 所以既不记证据也不算产出 —— 停滞探测器会因此催它去修。
            else:  # git_diff
                self.verify_seen = True
                productive = True
                verified_now = True
        elif tool == "git_diff" and ok:
            self.verify_seen = True
            verified_now = True
            self.diff_reviewed = True
        elif tool == "commit" and ok:
            # 收尾证据：commit 工具成功 → 改动进了真树（不是只躺在工作区）。
            # 交付闸据此要求「改了就得提交」。
            self.commit_seen = True
            if isinstance(args, dict):
                self.commit_files = list(
                    args.get("files") or args.get("paths") or []
                )
        elif tool in _VERIFY_READBACK and ok:
            self.verify_seen = True
            verified_now = True
            self.n_reads += 1
            if _path:
                self.read_paths.add(_path)
        if productive:
            self.productive_seen = True
            self.since_productive = 0
        else:
            self.since_productive += 1
        if verified_now:
            self.verify_pending = False

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

    # ── 有界失败修复回路 ───────────────────────────────────────────
    def _record_failure(self, args, rc, out):
        """记一次核对失败：命令 / rc / 输出尾部（截断）。

        为什么只记尾部：失败根因几乎总在最后几行；整屏输出灌进上下文等于用
        噪声淹掉报错（SWE-agent 的 ACI 也只回可行动的片段）。
        """
        cmd = _cmdline(args) or (self.verify_info or {}).get("cmdline") or ""
        try:
            _rc = int(rc) if rc is not None else -1
        except (TypeError, ValueError):
            _rc = -1
        self.fix_failures += 1
        self.last_fail = {
            "cmd": cmd,
            "rc": _rc,
            "tail": _out_tail(out if out is not None else (args or {}).get("out")),
            "n": self.fix_failures,
        }
        # 配额用完就不再排队：不再催 = 不再给模型「再试一次」的暗示。
        if self.fix_hints >= self.max_fix_hints:
            self.fix_suppressed = True
            self.fix_pending = None
            return self.last_fail
        self.fix_pending = dict(self.last_fail)
        return self.last_fail

    def fix_directive(self):
        """把最近一次失败转成一条**可定位**的修复指令（没有就返回 None）。

        只发一次、有界（max_fix_hints）：超过上限就返回 None，不再催 ——
        否则「改一次试一次」会变成新的死循环。
        """
        f = self._take_failure()
        if f is None:
            return None
        msg = _fix_detail(f)
        if self.fix_hints >= self.max_fix_hints:
            msg += (
                "\n（这是本轮第 %d 次也是最后一次自动提示；再用同一方式重试也不会"
                "再被提示 —— 要么换一条真不一样的做法，要么如实报告卡在哪。）"
                % self.fix_hints
            )
        self.notes.append(msg)
        return msg

    def _take_failure(self):
        """取走「还没告诉模型」的失败记录（配额内）。

        有界在这里落地：配额用完就返回 None 并置 fix_suppressed —— 不再催。
        """
        if not self.fix_pending:
            return None
        if self.fix_hints >= self.max_fix_hints:
            self.fix_suppressed = True
            self.fix_pending = None
            return None
        f = self.fix_pending
        self.fix_pending = None
        self.fix_hints += 1
        return f

    # ── 自进化调参（根源级：规则账能改引擎行为，不只是加提示）──────
    def apply_policy(self, policy):
        """把一条自进化规则算出的参数落进引擎。返回实际生效值。

        为什么要有这条：ACTIVE 规则以前只会往 system prompt 里塞一段
        「照此避坑」的文字——**文字不改变行为**。收敛引擎的阈值是真正
        的行为旋钮：本机历史反复停滞，就该更早催它动手；反复同参打转，
        就该更早熔断。所以规则必须能拧旋钮。

        安全钳制：下限 2（比 2 还小会变成"每步都催"，模型被噪声淹没，
        反而更差）；上限不超过构造时的初值（只允许收紧，不允许放宽
        ——放宽等于把刚修好的护栏拆掉）。
        """
        if not isinstance(policy, dict):
            return {}
        applied = {}
        for key, attr in (
            ("max_stall", "max_stall"),
            ("max_explore", "max_explore"),
            ("loop_repeat", "loop_repeat"),
        ):
            if key not in policy:
                continue
            try:
                want = int(policy[key])
            except (TypeError, ValueError):
                continue
            cur = int(getattr(self, attr))
            newv = max(2, min(want, cur))  # 只允许收紧，且不低于 2
            if newv != cur:
                setattr(self, attr, newv)
            applied[attr] = int(getattr(self, attr))
        return applied

    # ── 指令注入 ────────────────────────────────────────────────────
    def directive(self):
        """给模型的一句话方向指令（没有就返回 None）。

        优先级：失败修复 > 核对 > 复现 > 停滞 > 循环 > 阶段边界。互斥，一次
        只给一条，避免指令噪声把模型淹掉。

        失败修复排最前：核对刚判红时，「上一条命令为什么红、报错长什么样」是
        信息量最大的那一句；排在「还没核对」之后就会被它盖掉，模型仍然只能猜。
        """
        # 失败修复回路（有界）：只对**还没告诉模型**的失败发一次，配额用完
        # 就自然返回 None，后面的判据照常接管 —— 不会变成无限催促。
        _fix = self.fix_directive()
        if _fix:
            return _fix
        # 核对指令优先级次高：刚改完还没核对，是所有错误里最值钱的一个
        # ——其余错误（绕路、停滞）只是慢，错误改动是错。
        if self.verify_pending and not self.verify_nagged and self.verify_info:
            self.verify_nagged = True
            if self.verify_outcome == "fail":
                msg = (
                    "[核对指令] 上一次核对**没通过**（rc≠0）。别再改一遍就交差："
                    "先把刚才那条命令的报错读懂，定位到真正失败的地方再改，"
                    "改完重新跑 `%s`（%s）直到 rc=0。"
                    % (
                        self.verify_info.get("cmdline") or "",
                        self.verify_info.get("why") or "",
                    )
                )
            else:
                msg = (
                    "[核对指令] 你刚改了东西，但还没核对过。这个仓库的验证命令是"
                    " `%s`（%s）：现在就跑它，rc 决定真假；失败就把报错读懂再改，"
                    "别把没验过的改动说成做完了。"
                    % (
                        self.verify_info.get("cmdline") or "",
                        self.verify_info.get("why") or "",
                    )
                )
            self.notes.append(msg)
            return msg
        # 缺陷修复的中途提醒：已经动手改了，但全程没看到过原 bug 失败的样子。
        # 这时最该做的是**回退到复现**——盲改没法定向。软提醒（不硬拦），
        # 因为有些 bug 确实复现不了，硬拦只会逼模型编一个假复现。
        if (
            (self.contract or {}).get("type") == "bugfix"
            and self.write_seen
            and not self.repro_fail_seen
            and not self._repro_nagged
        ):
            self._repro_nagged = True
            msg = (
                "[复现指令] 你已经动手改了，但**从头到尾没看到过原 bug 失败的样子**。"
                "先停一下：跑一次能触发这个 bug 的命令/测试，把失败的样子留好，"
                "再确认你的改动确实让那个失败消失了。没复现过的修改，没法证明修对了。"
            )
            self.notes.append(msg)
            return msg
        if self.since_productive >= self.max_stall and self.steps > 2:
            msg = (
                "[收敛指令] 连续 %d 步没有产出（没有写盘成功，也没有跑出 rc=0）"
                "，现在二选一：① 立刻动手改一处；② 立刻交付当前结论并写清卡点。"
                "不要再重复读同样的文件。" % self.since_productive
            )
            self.since_productive = 0  # 注入过就重置计数，别每步都喊
            self.stall_hits += 1
            self.notes.append(msg)
            return msg
        if self.repeat_count >= self.loop_repeat and self.fps.get(self.repeat_fps, 0) >= self.loop_repeat:
            msg = (
                "[收敛指令] 你已用同样的方式（%s）重复 %d 次无进展；"
                "换一条完全不同的路径（换工具、换文件、换思路），别再重复它。"
                % (str(self.repeat_fps).split(":")[0], self.fps.get(self.repeat_fps, 0))
            )
            self.repeat_count = 0
            self.loop_hits += 1
            self.notes.append(msg)
            return msg
        if self.phase == P_EXPLORE and self.steps >= self.max_explore:
            msg = (
                "[收敛指令] 已探查 %d 步，信息够了。现在去**动手**"
                "（写盘/改文件/跑命令），不要继续读。"
                % self.steps
            )
            self.phase_hits += 1
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
    def evidence_ok(self, answer=""):
        """按契约的**硬要求**判证据齐不齐。返回 (齐?, 缺什么, 缺的原因)。

        硬要求只认「真的发生过的事」：写盘记录、读回记录、命令 rc、
        红→绿链。**不认终答里的措辞** —— 措辞会说「已修复」，事实不会。
        """
        req = (self.contract or {}).get("hard") or []
        missing = []
        why = {
            "verify_pass": "改了但没有一次通过的核对命令",
            "write_and_readback": "没有把写下的内容读回来核对过",
            "write_seen": "没有任何一次成功的写盘（改动没落地）",
            "source_cited": "结论里没有可核对的来源（URL 或 文件:行号）",
            "cited": "结论里没有可核对的来源（URL / 文件:行号 / 命令）",
            "command_ran": "没有跑过任何命令（结论不可复算）",
            "any_read": "没有读过任何东西（结论无从谈起）",
            "repro_red_then_green": "没有「先复现失败 → 再改 → 再通过」的完整链",
            "diff_reviewed": "改了东西但没看过自己的 diff（git diff）",
            "committed": "改了代码但没提交（真树还脏）——用 commit 工具提交"
            "你动过的文件（绝不 push）",
            "no_failed_check": "上一次核对没通过（rc≠0），先修好再交付",
        }

        def _has(kind):
            if kind == "verify_pass":
                return bool(self.write_seen and self.verify_seen)
            if kind == "committed":
                # 改了 → 必须真提交过；**没改** → 无需提交（vacuously 满足）。
                # 否则纯只读/纯分析类任务会被一条用不上的要求卡死。
                return bool(self.commit_seen) or (not self.write_seen)
            if kind == "write_and_readback":
                return bool(self.write_paths & self.read_paths)
            if kind == "write_seen":
                # 写成功本身就是落盘证据（工具回执里有 rc/bytes/diff）。
                # 读回是**更强**的确认（内容对不对），但对「改一行」这种
                # 小改动是过度要求 —— 过度约束会把小任务也卡死。
                return bool(self.write_seen)
            if kind in ("source_cited", "cited"):
                return _has_citation(answer)
            if kind == "command_ran":
                return bool(self.cmd_ran)
            if kind == "any_read":
                return bool(self.read_paths or self.n_reads)
            if kind == "repro_red_then_green":
                return bool(self.repro_green)
            if kind == "diff_reviewed":
                return bool(self.diff_reviewed)
            if kind == "no_failed_check":
                # 已知有一次核对没过（rc≠0）→ 不许当没发生过就交付
                return self.verify_outcome != "fail"
            return False

        # 写够不着（被环境锁住）时，撤掉「必须写盘」的要求 ——
        # 否则任务永远交不了付（闸门一直退到预算耗尽）。
        _relaxed = []
        if self.write_blocked and not self.write_seen:
            _ws = ("write_and_readback", "verify_pass", "write_seen")
            _kept = []
            for r in req:
                if isinstance(r, (tuple, list)):
                    _rest = [k for k in r if k not in _ws]
                    if _rest:
                        _kept.append(tuple(_rest) if len(_rest) > 1 else _rest[0])
                    continue
                if r in _ws:
                    continue
                _kept.append(r)
            req = _kept
            _relaxed = ["写操作被环境锁住，契约的写类要求已撤"]
        # 全局规则（不分任务类型）：**已知有一次核对没通过**（rc≠0）→
        # 不许当没发生过就交付。这不是「重试提示」，是事实：
        # 刚跑过的检查是红的。
        if self.verify_outcome == "fail":
            missing.append("no_failed_check")
        for r in req:
            # 「自审 diff」只在 git 仓库里有意义：非 git 仓库根本没有 diff
            # 可看，硬要它就是永远红（假红比漏红更坏）。
            if r == "diff_reviewed" and not self.require_diff_review:
                continue
            # 元组 = any-of（这一类证据拿到其中一种就算齐）
            if isinstance(r, (tuple, list)):
                if not any(_has(k) for k in r):
                    missing.append("/".join(r))
                continue
            if not _has(r):
                missing.append(r)
        return (
            (not missing),
            missing,
            [why.get(m, m) for m in missing] + _relaxed,
        )

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
            self.gate_retries += 1
            return False, "最终回答为空"
        # 诚实报告失败是**真交付**（说清了没做成 + 原因），不退回。
        if _REPORTS_FAILURE.search(text):
            return True, ""
        # 任务契约的硬要求（W26）：不同类型的「完成证据」不一样。
        _ev_ok, _ev_missing, _ev_why = self.evidence_ok(text)
        if not _ev_ok:
            return False, "任务契约（%s）要求的证据缺：%s" % (
                (self.contract or {}).get("name") or "通用",
                "；".join(_ev_why),
            )
        # 规则 A：像是要产出、动过工具、却什么都没写 → 只探查不动手，退回。
        # 这正是 900 秒那次事故的形状（探查 86 次、一步没落地）。
        if self.implies_build and self.steps > 0:
            if not self.write_seen and not self.dirty:
                self.gate_retries += 1
                return False, "任务像是要做东西，但全程只有探查没有写盘/改动证据"
        # 规则 B：需求**点名要验证**却没验 → 退回。
        # 判据来自需求字面（「验证/测试/跑一次」），不是「所有改动都得验」——
        # 否则「把第二行改掉」这种小事会被无端卡住。
        if self.demands_verify and self.steps > 0 and not self.verify_seen:
            self.gate_retries += 1
            return False, (
                "需求要求验证，但本轮没有**核对类**命令"
                "（cat/wc/sha256sum/pytest/git diff 这类才算核对；"
                "只跑了别的命令不算）"
            )
        # 规则 C：一次工具都没调，却宣称做完了 → 空口交付，退回。
        if self.steps == 0 and _CLAIMS_DONE.search(text):
            self.gate_retries += 1
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
            "gate_retries": self.gate_retries,
            "stall_hits": self.stall_hits,
            "verify_pending": bool(self.verify_pending),
            "verify_nagged": bool(self.verify_nagged),
            "verify_cmd": (self.verify_info or {}).get("cmdline") or "",
            "verify_outcome": self.verify_outcome,
            "contract_type": (self.contract or {}).get("type") or "",
            "contract_name": (self.contract or {}).get("name") or "",
            "repro_fail_seen": bool(self.repro_fail_seen),
            "repro_green": bool(self.repro_green),
            "cmd_ran": bool(self.cmd_ran),
            "write_paths": len(self.write_paths),
            "read_paths": len(self.read_paths),
            "write_blocked": bool(self.write_blocked),
            "loop_hits": self.loop_hits,
            "phase_hits": self.phase_hits,
            "max_stall": self.max_stall,
            "max_explore": self.max_explore,
            "loop_repeat": self.loop_repeat,
            # 失败修复回路：账本要能看见「这一轮失败过几次 / 提示了几次 /
            # 是不是已经催到上限停催」—— 否则自进化无法据此判「该拧哪个旋钮」。
            "fix_failures": self.fix_failures,
            "fix_hints": self.fix_hints,
            "max_fix_hints": self.max_fix_hints,
            "fix_suppressed": bool(self.fix_suppressed),
            "fix_pending": bool(self.fix_pending),
            "last_fail_cmd": (self.last_fail or {}).get("cmd") or "",
            "last_fail_rc": (self.last_fail or {}).get("rc"),
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
