# -*- coding: utf-8 -*-
"""dsp —— 决策·监督·计划模块（Decision / Supervision / Plan，只用标准库）。

为什么要有这一层
----------------
`agent_core.run_agent` 是「需求 → 模型 → 工具 → 模型 → 终答」的单循环：
它能把一步意图变成真实执行，但它没有**项目级**概念 —— 没有任务分解、
没有「做偏了就喊停」的整轮监督、没有「先出计划、人审后再动手」的闸。
plan 模式只是只读闸，不是计划产物。

本文件补的正是那一环，并且**不新开一套真相**：
  · 权限复用 agent_core 的 RISK_READ/WRITE + plan/accept-edits/full +
    硬阻断清单（本文件只做「计划锁」与「任务白名单」两层加法）；
  · 证据口径复用 company 引擎 verdict 思想：任务完成必须有含
    rc=/PASS/FAIL/OK 的真实证据行，否则 BLOCK；
  · 审计复用 gh_audit 哈希链思想：plan 每次变迁 append 一行 sha，
    断链即 fail-closed；
  · 失败信号沿用 signals.jsonl（带 plan_id/task_id），中断记
    interrupted 不进失败规则（沿用自进化 v2 语义）。

三个职责：
  Plan        计划器：Plan JSON schema 校验 + 落盘 + 渲染 + repo-map。
  Supervision 监督器：任务白名单 + doom 归一化三连 ask + 偏离 BLOCK。
  Decision    决策器：实现类任务先 gh 参考纪律 + 单任务 8 步预算。

退出语义：本文件函数不抛业务异常 —— 判据不满足返回
{"ok": False, "error": ...}，调用方（agent_core）据此转退出码。
"""

import hashlib
import json
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLANS_DIR = os.path.join(ROOT, "work", "plans")

PLAN_VERSION = 1
TASK_BUDGET = 8              # 单任务最多几步，超即分片或 BLOCK
DOOM_COUNT = 3               # 同任务同工具同参（归一化）连做几次转 ask
DRIFT_LIMIT = 2              # 偏离 Plan 无进展几步即 BLOCK
REPO_MAP_CAP = 4096          # repo-map 字节封顶
PLAN_JSON_CAP = 4096         # Plan 摘要注入 system prompt 的字节封顶

# plan 模式下做侦察只许用这些只读工具（与 agent_core TOOLS 名同源，
# 这里只记名、不 import —— 判据是「名在表里」，执行仍走 exec_tool）。
PLAN_RECON_TOOLS = ("list_dir", "read_file", "search", "gh_search",
                    "gh_repo_view", "gh_file_read", "gh_read", "health")

# 任务 tools 白名单的合法全集（超出即 schema 非法，防止计划写个假工具名）。
KNOWN_TOOLS = ("list_dir", "read_file", "search", "run_cmd", "write_file",
               "edit_file", "git_status", "git_diff", "git_log", "gh_read",
               "health", "gh_search", "gh_repo_view", "gh_file_read",
               "gh_deploy", "web_fetch")


def _sha_hex(b):
    return hashlib.sha256(b).hexdigest()


def _norm_args(args):
    """参数归一化：键序/空格不算不同（opencode doom_loop 升级点）。

    agent_core 的 MAX_REPEAT 用 json.dumps(sort_keys=True) 比整串，
    但那是跨步全局比；这里是**同任务内**比，且先过一遍类型清洗：
    非 dict/list/标量一律按 repr 收敛，保证可比。
    """
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(args)


def validate_plan(d):
    """Plan JSON schema 校验。返回 (ok, error)；ok 时 error 为 ""。

    必备：goal 非空串；tasks 非空数组，每项有唯一 id、非空 title、
    非空 accept（可验证判据）、tools 非空且全在 KNOWN_TOOLS、
    depends 全指 tasks 内已存在的 id（含自依赖拒绝）。
    """
    if not isinstance(d, dict):
        return False, "计划必须是 JSON object"
    goal = d.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        return False, "缺 goal（非空字符串）：计划不知道要干什么"
    tasks = d.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return False, "缺 tasks（非空数组）：计划至少要有一个任务"
    ids = set()
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            return False, "tasks[%d] 不是 object" % i
        tid = t.get("id")
        if not isinstance(tid, str) or not tid.strip():
            return False, "tasks[%d] 缺 id（非空字符串）" % i
        if tid in ids:
            return False, "任务 id 重复：%r" % tid
        ids.add(tid)
        if not isinstance(t.get("title"), str) or not t.get("title").strip():
            return False, "任务 %r 缺 title（非空字符串）" % tid
        if not isinstance(t.get("accept"), str) or not t.get("accept").strip():
            return False, ("任务 %r 缺 accept（可验证判据，如"
                            "「pytest 全绿 rc=0」）" % tid)
        tools = t.get("tools")
        if not isinstance(tools, list) or not tools:
            return False, "任务 %r 缺 tools（非空数组）" % tid
        for tn in tools:
            if tn not in KNOWN_TOOLS:
                return False, ("任务 %r 的工具 %r 不在合法全集里"
                                % (tid, tn))
        deps = t.get("depends") or []
        if not isinstance(deps, list):
            return False, "任务 %r 的 depends 必须是数组" % tid
        for dep in deps:
            if dep == tid:
                return False, "任务 %r 不能依赖自己" % tid
    # depends 必须指向已存在的 id（第二遍：前向引用也允许）。
    for t in tasks:
        for dep in (t.get("depends") or []):
            if dep not in ids:
                return False, ("任务 %r 的 depends 指向不存在的任务 %r"
                                % (t.get("id"), dep))
    # 环检查（根源补强：直调 save_plan 也不可落环计划；此前只靠 leader.has_cycle）。
    adj = {t.get("id"): list(t.get("depends") or []) for t in tasks}
    color = {}
    def _dfs(u, stack):
        color[u] = 1
        for v in adj.get(u, []):
            if color.get(v, 0) == 1:
                return True
            if color.get(v, 0) == 0 and _dfs(v, stack + [v]):
                return True
        color[u] = 2
        return False
    for tid in adj:
        if color.get(tid, 0) == 0 and _dfs(tid, [tid]):
            return False, "计划有环：依赖成环，autonomy 永无可做任务（先解环）"
    return True, ""


def _bind_sha(goal, tasks):
    payload = json.dumps({"goal": goal, "tasks": tasks}, ensure_ascii=False,
                         sort_keys=True).encode("utf-8")
    return _sha_hex(payload)


def new_plan(goal, tasks):
    """组装一个 Plan dict（含 bind_sha，未落盘、未审批）。"""
    plan = {"version": PLAN_VERSION, "goal": goal.strip(),
            "tasks": tasks, "approved": False,
            "created": round(time.time(), 3)}
    for t in plan["tasks"]:
        t.setdefault("depends", [])
        t.setdefault("status", "todo")
        t.setdefault("steps_used", 0)
        t.setdefault("evidence", [])
    plan["bind_sha"] = _bind_sha(plan["goal"], plan["tasks"])
    return plan


def plan_path(sid):
    return os.path.join(PLANS_DIR, "%s-plan.json" % sid)


def chain_path(sid):
    return os.path.join(PLANS_DIR, "%s-chain.jsonl" % sid)


def _chain_append(sid, event, bind_sha):
    """审计链 append-only：一行 {ts,event,sha,prev}。写失败静默——
    审计是增益层，但读链断时判红（fail-closed 在 verify_chain）。"""
    p = chain_path(sid)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        prev = ""
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        prev = json.loads(ln).get("sha", prev)
                    except ValueError:
                        pass
        row = {"ts": round(time.time(), 3), "event": event,
               "sha": bind_sha, "prev": prev}
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True)
                    + "\n")
    except OSError:
        pass


def verify_chain(sid):
    """验审计链：每行的 prev 必须等于上一行的 sha。返回 (ok, error)。"""
    p = chain_path(sid)
    if not os.path.exists(p):
        return True, ""
    try:
        with open(p, encoding="utf-8") as f:
            rows = [json.loads(x) for x in f if x.strip()]
    except (OSError, ValueError) as e:
        return False, "审计链不可读：%s" % str(e)[:120]
    prev = ""
    for i, r in enumerate(rows):
        if not isinstance(r, dict) or r.get("prev") != prev:
            return False, ("审计链在第 %d 行断裂（prev 对不上）："
                            "可信度归零" % (i + 1))
        prev = r.get("sha", "")
    return True, ""


def save_plan(plan, sid):
    """校验 → 落盘 → 上链。返回 (ok, error)。"""
    ok, err = validate_plan(plan)
    if not ok:
        return False, err
    plan["bind_sha"] = _bind_sha(plan["goal"], plan["tasks"])
    p = plan_path(sid)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=1, sort_keys=True)
    except OSError as e:
        return False, "计划落盘失败：%s" % str(e)[:120]
    _chain_append(sid, "save", plan["bind_sha"])
    return True, ""


def load_plan(sid):
    """读计划。返回 (plan_or_None, error)。文件不存在 error 为 "missing"。"""
    p = plan_path(sid)
    if not os.path.exists(p):
        return None, "missing"
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        return None, "计划文件坏了：%s" % str(e)[:120]
    ok, err = validate_plan(d)
    if not ok:
        return None, "计划 schema 非法：%s" % err
    if d.get("bind_sha") != _bind_sha(d["goal"], d["tasks"]):
        return None, ("bind_sha 对不上（计划被手改）：重算 bind 或重做计划，"
                       "不许带着改过的计划跑")
    okc, errc = verify_chain(sid)
    if not okc:
        return None, errc
    return d, ""


def approve_plan(sid):
    """人审通过：approved=true → 落盘 → 上链。返回 (ok, error)。"""
    plan, err = load_plan(sid)
    if plan is None:
        if err == "missing":
            return False, "还没有计划（/plan 先出计划）"
        return False, err
    plan["approved"] = True
    plan["approved_at"] = round(time.time(), 3)
    ok, err2 = save_plan(plan, sid)
    if not ok:
        return False, err2
    _chain_append(sid, "approve", plan["bind_sha"])
    return True, ""


def unapprove_plan(sid):
    """撤销审批：approved=false → 落盘 → 上链。老板中途叫停用。"""
    plan, err = load_plan(sid)
    if plan is None:
        if err == "missing":
            return False, "还没有计划"
        return False, err
    plan["approved"] = False
    plan.pop("approved_at", None)
    ok, err2 = save_plan(plan, sid)
    if not ok:
        return False, err2
    _chain_append(sid, "unapprove", plan["bind_sha"])
    return True, ""


def render_plan(plan):
    """计划渲染成任务树文本（屏上给人审）。超长截断并注明。"""
    lines = ["目标：%s" % plan.get("goal", ""),
             "bind=%s approved=%s"
             % (str(plan.get("bind_sha", ""))[:12],
                "yes" if plan.get("approved") else "no")]
    for t in plan.get("tasks", []):
        deps = ",".join(t.get("depends") or []) or "-"
        lines.append("[%s] %s：%s" % (t.get("status", "todo"),
                                      t.get("id"), t.get("title")))
        lines.append("    判据=%s · 工具=%s · 依赖=%s"
                     % (t.get("accept"), ",".join(t.get("tools") or []),
                        deps))
    out = "\n".join(lines)
    if len(out.encode("utf-8")) > PLAN_JSON_CAP:
        raw = out.encode("utf-8")[:PLAN_JSON_CAP].decode("utf-8",
                                                         "ignore")
        out = raw + "\n…（已截断，全文见 plan.json）"
    return out


# ── 监督器 ────────────────────────────────────────────────────

_EVIDENCE_RE = None


def _evidence_re():
    global _EVIDENCE_RE
    if _EVIDENCE_RE is None:
        import re
        _EVIDENCE_RE = re.compile(r"rc\s*=\s*\d|PASS|FAIL|OK\b|SHIP_OK")
    return _EVIDENCE_RE


def check_tool(plan, task_id, name, args):
    """监督：任务白名单。返回 None=放行；字符串=拒绝理由。

    计划锁由调用方（agent_core._authorize）在本函数之前判：
    未 approve 时写工具一律拒（理由里带 /approve），这里只看白名单。
    """
    task = None
    for t in plan.get("tasks", []):
        if t.get("id") == task_id:
            task = t
            break
    if task is None:
        return "不在计划里：任务 %r 不存在（/tasks 看清单）" % task_id
    if task.get("status") == "done":
        return "任务 %r 已完成：别再动它" % task_id
    if name not in (task.get("tools") or []):
        return ("任务 %r 的工具白名单是 [%s]，%r 不在里面"
                % (task_id, ",".join(task.get("tools") or []), name))
    return None


class DoomWatch:
    """同任务同工具同参（归一化）连做 DOOM_COUNT 次 → 建议 ask。

    与 agent_core.MAX_REPEAT 的区别：①归一化比对（键序/空格不算不同）；
    ②作用域是任务内；③到阈值不是判 limit，而是返回 "ask" 让调用方
    转人工确认（opencode doom_loop 语义）。
    """

    def __init__(self):
        self.last_sig = None
        self.repeat = 0

    def note(self, task_id, name, args):
        sig = (task_id, name, _norm_args(args))
        if sig == self.last_sig:
            self.repeat += 1
        else:
            self.repeat, self.last_sig = 0, sig
        if self.repeat + 1 >= DOOM_COUNT:
            return "ask"
        return None


def task_record_step(plan, task_id, ok, evidence=""):
    """记一步：steps_used+1；ok 且 evidence 含 rc 行 → done。返回 status。"""
    for t in plan.get("tasks", []):
        if t.get("id") == task_id:
            t["steps_used"] = int(t.get("steps_used") or 0) + 1
            if evidence:
                t.setdefault("evidence", []).append(evidence[:300])
            if ok and evidence and _evidence_re().search(evidence):
                t["status"] = "done"
            elif t["steps_used"] >= TASK_BUDGET and t.get("status") != "done":
                t["status"] = "stuck"
            else:
                if t.get("status") == "todo":
                    t["status"] = "doing"
            return t["status"]
    return ""


def check_evidence(evidence):
    """任务完成证据行必须含 rc=/PASS/FAIL/OK（复用 close-round 思想）。

    返回 (ok, error)。"""
    if not isinstance(evidence, str) or not evidence.strip():
        return False, "完成任务必须给证据行（含 rc=数字 或 PASS/FAIL/OK）"
    if not _evidence_re().search(evidence):
        return False, ("证据行里没有 rc=/PASS/FAIL/OK：%r —— "
                        "没证据的「做完了」不算数" % evidence[:80])
    return True, ""


def next_task(plan):
    """下一个可做任务：todo/doing 中 depends 全 done 的第一个。无则 ""。"""
    done = {t.get("id") for t in plan.get("tasks", [])
            if t.get("status") == "done"}
    for t in plan.get("tasks", []):
        if t.get("status") in ("todo", "doing") and all(
                d in done for d in (t.get("depends") or [])):
            return t.get("id")
    return ""


def plan_summary(plan, locked=True):
    """Plan 摘要行（注入 system prompt，超长截断）。无计划返回 ""。

    locked=写面是否真被计划锁锁死（agent_core 按 plan_lock_off 传）：
    自由面下绝不能对模型说「写工具锁死」—— 实测事故（2026-10-14）：自由面
    + 未审批计划 + 摘要硬编码「未审批：写工具锁死」→ 模型每轮读到「被锁死、
    等 /approve」→ 永远空转等一个不会到来的审批事件。口径必须随闸走。"""
    ids = ["%s:%s" % (t.get("id"), t.get("status", "todo"))
           for t in plan.get("tasks", [])]
    _tail = ("（已审批）" if plan.get("approved") else
             ("（未审批：写工具锁死）" if locked else
              "（写面自由：直接按任务开工，做完给 rc 证据）"))
    s = "计划 %s [%s]%s" % (str(plan.get("goal", ""))[:60],
                            ",".join(ids), _tail)
    b = s.encode("utf-8")
    if len(b) > 400:
        s = b[:400].decode("utf-8", "ignore") + "…"
    return s


# ── 分析器（大任务判定：结构触发，不猜意图）─────────────────────
# 分析器是纯函数：输入一句话需求 + 仓库根，输出 (grade, reasons)。
# grade = "BIG"（进计划闭环）/ "SMALL"（走原单循环）。
# 触发是结构性的——关键词/长度/仓库规模三路有一路命中即 BIG：
# 大任务漏进单循环是 silently 弱（步数烧光、上下文撑爆），宁可误报 BIG
# （多一道人审）也不漏报。误报成本 = 一次 /approve；漏报成本 = 整轮烧光。

BIG_WORDS = ("实现", "重构", "全链", "部署", "蒸馏", "迁移", "升级", "全局",
             "implement", "refactor", "deploy", "migrate", "upgrade",
             "scaffold", "build", "feature", "audit", "全量", "批量")
BIG_LEN = 60                 # 需求正文超这么多字，几乎不可能是单步活
BIG_FILES = 80               # 仓库顶层条目超这么多，侦察面大到必须先画图


def analyze_task(prompt, root=None):
    """分析需求复杂度。返回 {"grade": "BIG"|"SMALL", "reasons": [...]}。

    纯函数、不碰网、不写盘——门可直接拿它做判据。"""
    text = (prompt or "").strip()
    reasons = []
    low = text.lower()
    for w in BIG_WORDS:
        if w.lower() in low:
            reasons.append("关键词命中：%s" % w)
            break
    if len(text) >= BIG_LEN:
        reasons.append("需求长 %d 字（≥%d）" % (len(text), BIG_LEN))
    if root:
        try:
            n = len([nm for nm in os.listdir(root)
                     if not nm.startswith(".")])
            if n >= BIG_FILES:
                reasons.append("仓库顶层 %d 项（≥%d），侦察面大" % (n,
                                                                   BIG_FILES))
        except OSError:
            pass
    if reasons:
        return {"grade": "BIG", "reasons": reasons}
    return {"grade": "SMALL", "reasons": ["无大任务信号，走原单循环"]}


def should_auto_plan(prompt, sid, root=None):
    """大任务自启条件：分析 BIG + 本 sid 还没有计划。返回 (bool, analysis)。

    有计划就不重复建（幂等）；分析 SMALL 直接放行。调用方 fail-open 包住。"""
    analysis = analyze_task(prompt, root)
    if analysis["grade"] != "BIG":
        return False, analysis
    if sid and os.path.exists(plan_path(sid)):
        return False, {"grade": "BIG",
                       "reasons": analysis["reasons"] + ["已有计划，不重建"]}
    return True, analysis


def auto_plan(prompt, sid, root=None):
    """大任务自启：建默认三任务骨架（未审批）+ 落盘上链。

    返回 (plan_or_None, analysis)。骨架的 accept 是占位判据——模型首轮
    必须先精修 accept 再求 /approve（system prompt 指令钉死顺序）。"""
    want, analysis = should_auto_plan(prompt, sid, root)
    if not want:
        return None, analysis
    tasks = [
        {"id": "t1", "title": "侦察：读关键文件、确认现状",
         "accept": "关键文件已读 + 现状一句话 + rc=0",
         "tools": ["list_dir", "read_file", "search", "gh_search",
                   "gh_repo_view", "gh_file_read"],
         "depends": []},
        {"id": "t2", "title": "动手：按判据改代码",
         "accept": "改动落盘 + 验证命令 rc=0（证据行）",
         "tools": ["read_file", "search", "write_file", "edit_file",
                   "run_cmd", "git_diff"],
         "depends": ["t1"]},
        {"id": "t3", "title": "收尾：自检 + 回执",
         "accept": "自检 rc=0 + 回执含真实 rc",
         "tools": ["run_cmd", "git_diff", "git_status", "git_log"],
         "depends": ["t2"]},
    ]
    plan = new_plan((prompt or "")[:120], tasks)
    ok, _ = save_plan(plan, sid)
    if not ok:
        return None, analysis
    return plan, analysis


def auto_directive(plan, analysis, locked=True):
    """大任务自启后注入 system prompt 的指令段（模型路由纪律）。

    locked=写面是否真被锁死（与 plan_summary 同源）：上锁面按原顺序钉死
    （精修 accept → /approve → 按任务做）；自由面直接说「开工」——绝不再
    让模型等一个不会来的审批事件（2026-10-14 实测事故：自由面 + 未审批
    计划 + 指令硬编码「未审批写工具锁死」→ 空转等审批 + 复读模板）。"""
    _why = "；".join(analysis.get("reasons", []))[:160]
    if not locked:
        return ("大任务已自动建计划（%s）；写面自由（计划锁已关）：不要等"
                "审批，直接按任务顺序开工，每步给真实 rc 证据，做完收尾。"
                % _why)
    return ("大任务已自动建计划（%s），先读计划、把每项 accept 精修成"
            "可验证判据，经人 /approve 后再动手；未审批时写工具锁死，"
            "撞锁别重试。" % _why)


# ── 决策器 ────────────────────────────────────────────────────

def repo_map(root, cap=REPO_MAP_CAP):
    """轻量 repo-map：os.walk 两层，隐藏目录/超大目录跳过，cap 封顶。

    蒸馏自 aider repo-map：给模型全仓结构图，但只用标准库、
    只读、失败返回 ""（fail-open，不阻断主循环）。"""
    rows = []
    try:
        top = sorted(os.listdir(root))
    except OSError:
        return ""
    for name in top:
        if name.startswith("."):
            continue
        full = os.path.join(root, name)
        if os.path.isdir(full):
            try:
                kids = sorted(n for n in os.listdir(full)
                              if not n.startswith("."))
            except OSError:
                kids = []
            rows.append("%s/ (%d)" % (name, len(kids)))
            for k in kids[:30]:
                rows.append("  %s" % k)
            if len(kids) > 30:
                rows.append("  …（还有 %d 项）" % (len(kids) - 30))
        else:
            try:
                sz = os.path.getsize(full)
            except OSError:
                sz = 0
            rows.append("%s (%dB)" % (name, sz))
        if len("\n".join(rows).encode("utf-8")) > cap:
            break
    out = "\n".join(rows)
    b = out.encode("utf-8")
    if len(b) > cap:
        out = b[:cap].decode("utf-8", "ignore") + "\n…（已截断）"
    return out


def split_task(plan, task_id, sub_titles):
    """任务失败自动拆分子任务（最多 1 层：已有冒号 id 的不再拆）。

    返回 (ok, new_ids_or_error)。子任务继承父的 tools/accept，
    父状态置 stuck，子 depends 链式串起。"""
    if task_id and ":" in task_id:
        return False, "子任务不再拆（只拆一层，防止膨胀）"
    task = None
    for t in plan.get("tasks", []):
        if t.get("id") == task_id:
            task = t
            break
    if task is None:
        return False, "任务 %r 不存在" % task_id
    if not sub_titles or len(sub_titles) > 4:
        return False, "拆分 1-4 个子任务（多了就是没想清楚）"
    ids = {t.get("id") for t in plan.get("tasks", [])}
    new_ids = []
    prev = None
    for i, title in enumerate(sub_titles, 1):
        nid = "%s:%d" % (task_id, i)
        if nid in ids:
            return False, "子任务 %r 已存在" % nid
        sub = {"id": nid, "title": title.strip() or nid,
               "accept": task.get("accept"),
               "tools": list(task.get("tools") or []),
               "depends": ([prev] if prev else list(task.get("depends")
                                                     or [])),
               "status": "todo", "steps_used": 0, "evidence": []}
        plan["tasks"].append(sub)
        new_ids.append(nid)
        prev = nid
    task["status"] = "stuck"
    return True, new_ids


def write_block(sid, reason, done_list, suggest):
    """偏离 BLOCK 落盘 work/plans/<sid>-block.md。返回路径。"""
    p = os.path.join(PLANS_DIR, "%s-block.md" % sid)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("# BLOCK %s\n\n" % sid)
            f.write("原因：%s\n\n" % reason)
            f.write("已做：\n")
            for d in done_list or []:
                f.write("- %s\n" % d)
            f.write("\n建议拆分：\n%s\n" % (suggest or "（无）"))
    except OSError:
        return ""
    return p
