#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""todo —— 任务账本：让 agent 把「要做的事」显式写下来，并被监督。

为什么需要（对照成熟 agent 的结构）
----------------------------------
- **Claude Code** 有 TodoWrite：任务一多，就把步骤写成清单，逐项勾。
  没有它，模型会把 5 件事揉成一段话，做完 3 件就宣布「已完成」。
- **Aider** 有 todo 机制：每步编辑前先报当前在做哪一件。
- **OpenHands** 有 plan/里程碑：长任务靠它推进，不靠模型脑内记。

AgentOS 之前**没有这一层**：DSP 的计划是内核内部结构（而且是「需求拆任务」，
不是「agent 自己正在做的步骤」），agent 手上没有一份**用户看得见**的清单。
后果：长任务的进度不可见、不可核、不可恢复；交付闸也**无法判断**「你说的
做完了」是不是只做了三分之二。

本模块提供三件事：
1. **账本**：会话级、落盘（`work/todos/<sid>.json`）→ 跨压缩、跨重启存活；
2. **监督**：账本里还有 pending 项时，交付闸**不许**判「完成」
   （这是把「清单」变成真约束，而不是装饰）；
3. **可读**：终端里一屏看清「做到第几步、哪一步卡着」。

纯标准库、无副作用（只写自己的账本文件）、可单测。

学自 ruvnet/ruflo 的地方（2026-10-22）
--------------------------------------
ruflo v3 的任务域模型 `v3/src/task-execution/domain/Task.ts` 有两个设计，
本模块直接照搬（照搬的是**纪律**，不是代码）：

1. **带守卫的状态迁移**（那边 `start()` 只在 pending 时生效、`complete()`
   只在 in-progress 时生效、`cancel()` 不许动已完成的任务）。
   AgentOS 的账本原先**任意状态可跳** —— 于是「一次陈旧的整表写入」能把
   7 项 done 静默打回 pending（2026-10-21 实测），账本从此不可信；而账本是
   交付闸的判据来源 → **不可信的账本 = 不可信的交付闸**。
   现在账本也有迁移守卫：回退（done → pending/doing）默认**拒绝**，确实要
   重开就显式 `reopen=true`（并且会被记下来）。
2. **依赖 + 就绪判定 + 环检测**（那边 `Task.dependencies` /
   `areDependenciesResolved()` / `resolveExecutionOrder()` 拓扑排序并检测环）。
   AgentOS 的账本原先是**扁平列表**：没有依赖、没有「哪件该做」、面板也不说
   「这件在等谁」。现在有 `depends_on`、就绪判定、成环拒绝、优先级排序。
"""

import json
import os
import time

# 状态机：pending（没做）→ doing（在做）→ done（做完）/ dropped（明确放弃）
STATES = ("pending", "doing", "done", "dropped")
STATE_GLYPH = {
    "done": "✓",
    "doing": "▸",
    "dropped": "–",
    "pending": "○",
}
# 面板里 doing 排最前（正在做的那件），其次 pending，最后 done/dropped。
ORDER = {"doing": 0, "pending": 1, "dropped": 2, "done": 3}

# ── 状态迁移守卫（学自 ruflo 的 Task 实体）──────────────────────────
# 合法迁移：pending→doing/done/dropped；doing→pending/done/dropped；
# done→dropped（做完就完了）；dropped→pending/doing（捡回来做）。
TRANSITIONS = {
    "pending": {"pending", "doing", "done", "dropped"},
    "doing": {"doing", "pending", "done", "dropped"},
    "done": {"done", "dropped"},
    "dropped": {"dropped", "pending", "doing", "done"},
}
# **回退**：已完成的项被拉回未完成 —— 默认拒绝，要显式 reopen。
BACKWARD = {("done", "pending"), ("done", "doing")}
# 优先级（学自 ruflo 的 Task.getPriorityValue：high=3/medium=2/low=1）
PRIORITIES = ("high", "medium", "low")
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _reopen_reason(reopen):
    """reopen 只认**非空理由字符串**。

    为什么不是布尔开关：真机实测（2026-10-22）—— 模型看到工具描述里有
    reopen=true，就**直接带上它**把已完成的项打回 pending，守卫形同虚设。
    布尔开关只问「允不允许」，不问「为什么」；要让它有牙齿，就得留下理由
    —— 理由进账本（reopened 计数 + 理由文本），面板上明写「重开过」，
    人和交付闸都看得见这次回退。
    """
    if isinstance(reopen, str) and reopen.strip():
        return reopen.strip()[:200]
    return ""


def can_transition(old, new, reopen=None):
    """这次状态迁移允许吗？

    - 同态：永远允许；
    - 回退（done→pending/doing）：必须给 reopen **理由**（默认拒绝）；
    - 其余看迁移表。
    """
    old = str(old or "pending").strip().lower()
    new = str(new or "").strip().lower()
    if new not in STATES:
        return False
    if old == new:
        return True
    if (old, new) in BACKWARD:
        return bool(_reopen_reason(reopen))
    return new in TRANSITIONS.get(old, set())


def todo_dir(root=None):
    """账本目录：<root>/work/todos/<sid>.json。

    为什么要专门的目录：之前它直接写 <root>/<sid>.json —— 那个名字**长得
    像会话记录文件**（agent-<时间戳>-<id>.json），放进会话根里既容易误认，
    也容易和别的同名文件撞车。专门的 todos/ 目录，边界才清楚。
    """
    if root:
        return os.path.join(str(root), "work", "todos")
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "work", "todos"
    )


def todo_path(sid, root=None):
    sid = str(sid or "default").replace("/", "_")
    return os.path.join(todo_dir(root), "%s.json" % sid)


def _norm_int(v):
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def _norm_deps(v):
    """规整 depends_on：一串 item id（去重、保序、丢掉非整数）。"""
    if v is None:
        return []
    if isinstance(v, (int, str)):
        v = [v]
    out = []
    for x in v if isinstance(v, (list, tuple)) else []:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if i not in out:
            out.append(i)
    return out


def _norm_prio(v):
    p = str(v or "").strip().lower()
    return p if p in PRIORITIES else "medium"


def _norm(items):
    """规整成 [{'id','text','state','depends_on','priority'}]（id 按出现顺序稳定分配）。"""
    out = []
    if not isinstance(items, (list, tuple)):
        return out
    for i, it in enumerate(items):
        if isinstance(it, str):
            it = {"text": it}
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or "").strip()
        if not text:
            continue
        state = str(it.get("state") or "pending").strip().lower()
        if state not in STATES:
            state = "pending"
        try:
            ident = int(it.get("id"))
        except (TypeError, ValueError):
            ident = i + 1
        out.append(
            {
                "id": ident,
                "text": text[:200],
                "state": state,
                "depends_on": _norm_deps(it.get("depends_on")),
                "priority": _norm_prio(it.get("priority")),
                "reopened": _norm_int(it.get("reopened")),
                "reopen_reason": str(it.get("reopen_reason") or "")[:200],
            }
        )
    return out


def load(sid, root=None):
    """读账本。文件坏了就当空账本（不因为一次写坏就崩掉整个 agent）。"""
    p = todo_path(sid, root)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return []
    return _norm(d.get("items") or [])


def save(items, sid, root=None):
    """写账本。返回落盘路径（便于自检/审计引用）。"""
    d = todo_dir(root)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return ""
    p = todo_path(sid, root)
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "version": 1,
                    "updated": time.strftime("%F %T"),
                    "items": _norm(items),
                },
                f,
                ensure_ascii=False,
                indent=1,
            )
        os.replace(tmp, p)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return ""
    return p


def counts(items):
    items = _norm(items)
    c = {s: 0 for s in STATES}
    for it in items:
        c[it["state"]] = c.get(it["state"], 0) + 1
    c["total"] = len(items)
    c["open"] = c["pending"] + c["doing"]
    return c


def next_item(items):
    """当前该做的那一件。

    只从**就绪**的项里挑（依赖没结的不算 —— 那是 blocked，不是 next）：
    先 doing，没有 doing 就第一件 pending；同级按优先级 high>medium>low，
    再同级按 id 先后（学自 ruflo 的 `Task.sortByPriority`）。
    """
    items = _norm(items)
    ready = ready_items(items)
    for it in ready:
        if it["state"] == "doing":
            return it
    pend = [it for it in ready if it["state"] == "pending"]
    pend.sort(key=lambda it: (PRIORITY_RANK.get(it.get("priority"), 1), it["id"]))
    return pend[0] if pend else None


def detect_cycle(items):
    """找出 depends_on 构成的环（返回环上的 id 列表；空 = 无环）。

    学自 ruflo 的 `Task.resolveExecutionOrder()`：它拓扑排序时一旦发现
    「没有 ready 的了但还有剩的」就抛「循环依赖」。这里不排序，只**检测**，
    因为账本是给人看的清单（顺序由人/模型决定），但**成环必须拒绝** ——
    环会让「就绪判定」永远为空，账本从此卡死且看不出为什么。
    """
    items = _norm(items)
    ids = {it["id"] for it in items}
    dep = {
        it["id"]: [d for d in (it.get("depends_on") or []) if d in ids]
        for it in items
    }
    state = {}
    stack = []
    found = []

    def visit(n):
        state[n] = 1
        stack.append(n)
        for m in dep.get(n, []):
            if state.get(m) == 1:
                i = stack.index(m)
                found.extend(stack[i:])
                return True
            if state.get(m, 0) == 0 and visit(m):
                return True
        stack.pop()
        state[n] = 2
        return False

    for n in sorted(dep):
        if state.get(n, 0) == 0 and visit(n):
            break
    return found


def ready_items(items):
    """就绪的项：依赖都已结（done/dropped）的 pending/doing 项。"""
    items = _norm(items)
    ids = {it["id"] for it in items}
    closed = {it["id"] for it in items if it["state"] in ("done", "dropped")}
    out = []
    for it in items:
        if it["state"] not in ("pending", "doing"):
            continue
        # 悬空依赖（指向不存在的 id）**忽略**，不当成永远等不到 ——
        # 否则模型写错一个 id，这项就永远 blocked，账本看不出为什么卡住。
        deps = [d for d in (it.get("depends_on") or []) if d in ids]
        if all(d in closed for d in deps):
            out.append(it)
    return out


def blocked_items(items):
    """被依赖卡住的项（依赖还没结）。"""
    ready_ids = {it["id"] for it in ready_items(items)}
    return [
        it
        for it in _norm(items)
        if it["state"] in ("pending", "doing") and it["id"] not in ready_ids
    ]


def summary_lines(items, max_rows=12):
    """终端面板用的几行文本（按 doing → pending → dropped → done 排序）。"""
    items = _norm(items)
    if not items:
        return []
    closed = {it["id"] for it in items if it["state"] in ("done", "dropped")}
    order = sorted(
        range(len(items)),
        key=lambda i: (
            ORDER.get(items[i]["state"], 9),
            PRIORITY_RANK.get(items[i].get("priority"), 1),
            i,
        ),
    )
    lines = []
    for i in order[:max_rows]:
        it = items[i]
        wait = []
        if it["state"] in ("pending", "doing"):
            # 只有**没结**的项才谈得上「在等谁」；已完成的项显示等待标记会误导。
            wait = [d for d in (it.get("depends_on") or []) if d not in closed]
        mark = ""
        if wait:
            mark = "（等 %s）" % ",".join("#%d" % d for d in wait)
        elif it.get("priority") == "high":
            mark = "（高优）"
        if it.get("reopened"):
            mark += "（重开过 %d 次）" % _norm_int(it.get("reopened"))
        lines.append(
            "%s %s%s" % (STATE_GLYPH.get(it["state"], "○"), it["text"], mark)
        )
    if len(order) > max_rows:
        lines.append("… 另有 %d 项" % (len(order) - max_rows))
    return lines


def render(items, max_rows=12):
    """完整面板文本（多行）。空账本返回 ''。"""
    items = _norm(items)
    if not items:
        return ""
    c = counts(items)
    head = "计划 %d/%d 完成" % (c["done"] + c["dropped"], c["total"])
    if c["open"]:
        head += "（还有 %d 项没结）" % c["open"]
    return "\n".join([head] + summary_lines(items, max_rows))


def _reject(rid, frm, to, why):
    return {"id": rid, "from": frm, "to": to, "why": why}


BACKWARD_WHY = (
    "不许把已完成的项打回未完成（%s→%s）；真要重开就写 reopen=\"<理由>\"（"
    "要留痕，不写就拒）"
)


def apply_changes(current, changes, reopen=None):
    """**增量**更新账本，返回 (新账本, 被拒的变更)。

    为什么要有增量形式：整体覆盖式写（items=[…]）看着简单，但 agent 一次
    陈旧的整表写入就会**抹掉已完成的进度** —— 2026-10-21 实测：agent 第一轮
    把 7 项全标 done 之后，又一次整表写入把状态打回 pending，账本从此不可信
    （而账本是交付闸的判据来源 → 不可信的账本 = 不可信的交付闸）。
    增量写让「只改我刚做完的那一项」成为可能。

    **拒绝是明说的**（进 rejected 列表返回给模型），不是静默忽略 ——
    静默忽略会让模型以为改成了，然后基于错误认知继续干。
    """
    items = _norm(current)
    out = list(items)
    by_id = {it["id"]: i for i, it in enumerate(out)}
    rejected = []
    if not isinstance(changes, (list, tuple)):
        changes = [changes]
    for ch in changes:
        if not isinstance(ch, dict):
            continue
        try:
            cid = int(ch.get("id"))
        except (TypeError, ValueError):
            cid = None
        st = str(ch.get("state") or "").strip().lower()
        if cid is not None and cid in by_id:
            i = by_id[cid]
            it = dict(out[i])
            old = it["state"]
            if "text" in ch and str(ch.get("text") or "").strip():
                it["text"] = str(ch["text"]).strip()[:200]
            if st in STATES and st != old:
                if not can_transition(old, st, reopen=reopen):
                    rejected.append(
                        _reject(cid, old, st, BACKWARD_WHY % (old, st))
                    )
                else:
                    it["state"] = st
                    if (old, st) in BACKWARD:
                        it["reopened"] = _norm_int(it.get("reopened")) + 1
                        it["reopen_reason"] = _reopen_reason(reopen)
            if "depends_on" in ch:
                it["depends_on"] = _norm_deps(ch.get("depends_on"))
            if "priority" in ch:
                it["priority"] = _norm_prio(ch.get("priority"))
            out[i] = it
            continue
        # 新项（没给 id 或 id 不存在）→ 追加
        txt = str(ch.get("text") or "").strip()
        if txt:
            nid = cid if cid is not None else (max(by_id) + 1 if by_id else 1)
            out.append(
                {
                    "id": nid,
                    "text": txt[:200],
                    "state": st if st in STATES else "pending",
                    "depends_on": _norm_deps(ch.get("depends_on")),
                    "priority": _norm_prio(ch.get("priority")),
                }
            )
            by_id[nid] = len(out) - 1
    cyc = detect_cycle(out)
    if cyc:
        rejected.append(
            {
                "ids": cyc,
                "why": "依赖成环（%s）：成环会让就绪判定永远为空，账本卡死"
                % ",".join("#%d" % i for i in cyc),
            }
        )
        return items, rejected
    return _norm(out), rejected


def apply_set(current, new_items, reopen=None):
    """整表覆盖，但**不许静默回退**已完成的项。

    2026-10-21 真实踩到的坑：agent 把 7 项标 done 后，一次陈旧的整表写入
    把状态打回 pending，账本从此不可信。存量账本是判据来源 → 覆盖写也不能
    把 done 打回 pending（除非显式 reopen=true）。被拒的项保持旧状态。
    """
    cur = _norm(current)
    old_by_id = {it["id"]: it for it in cur}
    new = _norm(new_items)
    rejected = []
    out = []
    for it in new:
        old = old_by_id.get(it["id"])
        if old is not None and old["state"] == "done" and it["state"] != "done":
            if not can_transition(old["state"], it["state"], reopen=reopen):
                rejected.append(
                    _reject(
                        it["id"],
                        old["state"],
                        it["state"],
                        BACKWARD_WHY % (old["state"], it["state"]),
                    )
                )
                it = dict(old)
            else:
                it = dict(it)
                it["reopened"] = _norm_int(old.get("reopened")) + 1
                it["reopen_reason"] = _reopen_reason(reopen)
        out.append(it)
    cyc = detect_cycle(out)
    if cyc:
        rejected.append(
            {
                "ids": cyc,
                "why": "依赖成环（%s）：成环会让就绪判定永远为空，账本卡死"
                % ",".join("#%d" % i for i in cyc),
            }
        )
        return cur, rejected
    return _norm(out), rejected


def update(items, changes):
    """兼容旧调用：只要新账本（被拒的变更被忽略）。"""
    new, _rejected = apply_changes(items, changes)
    return new


def unfinished(items):
    """还没结的项（pending/doing）——交付闸问的就是它。"""
    return [it for it in _norm(items) if it["state"] in ("pending", "doing")]


if __name__ == "__main__":  # 手动看一眼
    import sys

    sid = sys.argv[1] if len(sys.argv) > 1 else "default"
    print(render(load(sid)) or "（空账本）")
