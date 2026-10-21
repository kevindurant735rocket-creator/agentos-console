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


def _norm(items):
    """规整成 [{'id': int, 'text': str, 'state': str}]（id 按出现顺序稳定分配）。"""
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
        out.append({"id": ident, "text": text[:200], "state": state})
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
    """当前该做的那一件：先 doing，没有 doing 就第一件 pending。"""
    items = _norm(items)
    for it in items:
        if it["state"] == "doing":
            return it
    for it in items:
        if it["state"] == "pending":
            return it
    return None


def summary_lines(items, max_rows=12):
    """终端面板用的几行文本（按 doing → pending → dropped → done 排序）。"""
    items = _norm(items)
    if not items:
        return []
    order = sorted(
        range(len(items)),
        key=lambda i: (ORDER.get(items[i]["state"], 9), i),
    )
    lines = []
    for i in order[:max_rows]:
        it = items[i]
        lines.append(
            "%s %s" % (STATE_GLYPH.get(it["state"], "○"), it["text"])
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


def unfinished(items):
    """还没结的项（pending/doing）——交付闸问的就是它。"""
    return [it for it in _norm(items) if it["state"] in ("pending", "doing")]


if __name__ == "__main__":  # 手动看一眼
    import sys

    sid = sys.argv[1] if len(sys.argv) > 1 else "default"
    print(render(load(sid)) or "（空账本）")
