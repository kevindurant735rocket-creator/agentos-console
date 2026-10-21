#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""todo_selftest —— 任务账本的证伪集。

账本是「把清单变成真约束」的那一层，所以它的判据不能只看格式，
还要看**语义**：pending 项必须被认出来、done 不算、面板要能让人一眼看懂。
"""

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import todo as T  # noqa: E402

CASES = []


def case(name, ok, detail=""):
    CASES.append((name, bool(ok), str(detail)))
    return bool(ok)


# ── 1. 规整：坏输入不许把账本弄脏 ───────────────────────────────
_n = T._norm([
    {"text": "  做事一  "},
    {"text": "做事二", "state": "doing"},
    {"text": "做事三", "state": "done"},
    {"text": "做事四", "state": "不存在的状态"},
    {"text": ""},
    "裸字符串",
    123,
])
case(
    "规整：去空白、补默认状态、丢掉空项与坏项（裸字符串算一项）",
    len(_n) == 5
    and _n[0]["text"] == "做事一"
    and _n[1]["state"] == "doing"
    and _n[3]["state"] == "pending"
    and _n[4]["text"] == "裸字符串"
    and _n[4]["state"] == "pending",
    repr(_n),
)

# ── 2. 计数与「没结的项」 ───────────────────────────────────────
_items = T._norm([
    {"text": "a", "state": "done"},
    {"text": "b", "state": "doing"},
    {"text": "c", "state": "pending"},
    {"text": "d", "state": "dropped"},
])
_c = T.counts(_items)
case(
    "计数：done/dropped/pending/doing 各计各的",
    _c["done"] == 1
    and _c["dropped"] == 1
    and _c["pending"] == 1
    and _c["doing"] == 1
    and _c["total"] == 4
    and _c["open"] == 2,
    repr(_c),
)
case(
    "没结：pending + doing 算没结，done/dropped 不算",
    [i["text"] for i in T.unfinished(_items)] == ["b", "c"],
    repr(T.unfinished(_items)),
)
case(
    "下一件：先 doing，没有 doing 才第一件 pending",
    T.next_item(_items)["text"] == "b",
    repr(T.next_item(_items)),
)

# ── 3. 落盘往返 + 坏账本不炸 ───────────────────────────────────
_root = tempfile.mkdtemp(prefix="todo_st_")
_p = T.save(_items, "sid-x", _root)
case("落盘：写得出、路径在 todos/ 目录下", bool(_p) and "todos" in _p, _p)
_back = T.load("sid-x", _root)
case("落盘：读回来逐项相等", _back == _items, repr(_back))
case(
    "落盘：不在 todos/ 下不许写（曾写成 <root>/<sid>.json，像会话文件）",
    os.path.dirname(_p).endswith(os.path.join("work", "todos")),
    _p,
)
case("落盘：没有的账本读成空表（不炸）", T.load("不存在的会话", _root) == [], "")
_bad = os.path.join(T.todo_dir(_root), "broken.json")
with open(_bad, "w", encoding="utf-8") as f:
    f.write("{ 这不是 json")
case("落盘：坏账本文件读成空表（不许让一次写坏搞崩 agent）",
     T.load("broken", _root) == [], "")

# ── 3b. 增量更新：只改点名的项，不抹掉别的进度 ────────────────────
_base = T._norm([
    {"id": 1, "text": "第一步", "state": "done"},
    {"id": 2, "text": "第二步", "state": "pending"},
])
_upd = T.update(_base, [{"id": 2, "state": "done"}])
case(
    "增量：只改点名的项，其余原样（不抹掉已完成的进度）",
    len(_upd) == 2
    and _upd[0]["state"] == "done"
    and _upd[1]["state"] == "done",
    repr(_upd),
)
_upd2 = T.update(_upd, [{"text": "新的一项"}])
case(
    "增量：没给 id 的变更算追加（新项落在末尾、默认 pending）",
    len(_upd2) == 3
    and _upd2[2]["text"] == "新的一项"
    and _upd2[2]["state"] == "pending",
    repr(_upd2),
)
_upd3 = T.update(_base, [{"id": 99, "state": "done"}])
case(
    "增量：id 不存在且没给 text → 不产生垃圾项",
    len(_upd3) == 2,
    repr(_upd3),
)

# ── 3c. 工具层：action=update 不带 items 也必须回写 ──────────────
# 2026-10-22：agent 真机跑里**自己报出来**的缺陷 ——
# `todo(action="update", changes=[...])`（**不带 items**，正是增量写该有
# 的形状）被工具当成「读清单」→ 返回旧清单、更新被吞，模型连试三次都
# 看不到效果，只好退回整表覆盖。这条判据钉住「action 分派在 items 判断之前」。
import agent_core as _ac  # noqa: E402

_troot = tempfile.mkdtemp()
_tctx = {"sid": "toolselftest", "root": _troot}
_ac._t_todo(
    {"items": [{"text": "甲", "state": "pending"},
               {"text": "乙", "state": "pending"}]},
    _tctx,
)
_ac._t_todo({"action": "update", "changes": [{"id": 2, "state": "done"}]}, _tctx)
_tgot = _ac._t_todo({"action": "list"}, _tctx)["items"]
case(
    "工具层：action=update 只带 changes（不带 items）也必须回写",
    len(_tgot) == 2
    and _tgot[0]["state"] == "pending"
    and _tgot[1]["state"] == "done",
    repr(_tgot),
)
_ac._t_todo({"action": "add", "changes": [{"text": "丙"}]}, _tctx)
_tgot2 = _ac._t_todo({"action": "list"}, _tctx)["items"]
case(
    "工具层：action=add 追加一项且不动别的项",
    len(_tgot2) == 3
    and _tgot2[0]["state"] == "pending"
    and _tgot2[1]["state"] == "done"
    and _tgot2[2]["state"] == "pending"
    and _tgot2[2]["text"] == "丙",
    repr(_tgot2),
)

# ── 4. 面板可读 ─────────────────────────────────────────────────
_panel = T.render(_items)
case(
    "面板：有标题、有计数、每项一个符号",
    "计划" in _panel
    and "2/4" in _panel
    and "▸" in _panel
    and "✓" in _panel
    and "○" in _panel,
    _panel.replace("\n", " | "),
)
case("面板：空账本渲染成空串（不画空面板）", T.render([]) == "", "")
case(
    "面板：doing 排在最前（正在做的那件要一眼看到）",
    T.summary_lines(_items)[0].startswith("▸"),
    repr(T.summary_lines(_items)),
)
case(
    "面板：还有没结的项时标题要说清还剩几件",
    "还有 2 项没结" in T.render(_items),
    T.render(_items).splitlines()[0],
)

# ── 5. 状态迁移守卫（学自 ruvnet/ruflo 的 Task 实体）───────────────
# ruflo v3 的 Task 每个状态迁移都带守卫：start() 只在 pending 生效、
# complete() 只在 in-progress 生效。AgentOS 的账本原先任意状态可跳 ——
# 于是 2026-10-21 实测：一次陈旧的整表写入把 7 项 done 打回 pending，
# 账本从此不可信（账本是交付闸的判据来源 → 不可信的账本 = 不可信的交付闸）。
_g_base = T._norm([
    {"id": 1, "text": "甲", "state": "done"},
    {"id": 2, "text": "乙", "state": "pending"},
])
_g_new, _g_rej = T.apply_changes(_g_base, [{"id": 1, "state": "pending"}])
case(
    "守卫：done→pending 被拒（已完成的进度不许被抹掉）",
    _g_new[0]["state"] == "done" and len(_g_rej) == 1,
    "state=%r rejected=%r" % (_g_new[0]["state"], _g_rej),
)
_g_new2, _g_rej2 = T.apply_changes(
    _g_base, [{"id": 1, "state": "pending"}], reopen="发现做得不对，重开"
)
case(
    "守卫：写了 reopen 理由才放行回退（真要重开有路可走）",
    _g_new2[0]["state"] == "pending" and not _g_rej2,
    "state=%r rejected=%r" % (_g_new2[0]["state"], _g_rej2),
)
case(
    "守卫：reopen 留痕（重开过的项记得住，不许悄悄回退）",
    _g_new2[0].get("reopened") == 1
    and "重开" in str(_g_new2[0].get("reopen_reason") or ""),
    repr(_g_new2[0]),
)
_g_new2b, _g_rej2b = T.apply_changes(
    _g_base, [{"id": 1, "state": "pending"}], reopen=True
)
case(
    "守卫：reopen 只认理由（布尔 true 形同没给 → 拒绝）",
    _g_new2b[0]["state"] == "done" and len(_g_rej2b) == 1,
    "state=%r rejected=%r" % (_g_new2b[0]["state"], _g_rej2b),
)
_g_new3, _g_rej3 = T.apply_set(
    _g_base,
    [{"id": 1, "text": "甲", "state": "pending"},
     {"id": 2, "text": "乙", "state": "pending"}],
)
case(
    "守卫：整表覆盖也不许把 done 打回 pending（陈旧整表写入的老坑）",
    _g_new3[0]["state"] == "done" and len(_g_rej3) == 1,
    "state=%r rejected=%r" % (_g_new3[0]["state"], _g_rej3),
)
_g_new4, _g_rej4 = T.apply_changes(
    _g_base, [{"id": 1, "state": "dropped"}]
)
case(
    "守卫：done→dropped 允许（明确放弃是合法动作，不是回退）",
    _g_new4[0]["state"] == "dropped" and not _g_rej4,
    "state=%r" % (_g_new4[0]["state"],),
)
case(
    "守卫：非法状态名被规整（不许账本里出现第四种状态）",
    T._norm([{"text": "x", "state": "wat"}])[0]["state"] == "pending",
    repr(T._norm([{"text": "x", "state": "wat"}])),
)

# ── 6. 依赖 / 就绪 / 环 / 优先级（学自 ruflo 的 Task.dependencies）──
_d_open = T._norm([
    {"id": 1, "text": "基础", "state": "pending"},
    {"id": 2, "text": "上层", "state": "pending", "depends_on": [1]},
])
case(
    "依赖：依赖没结的项不算就绪（next 不给它）",
    T.next_item(_d_open)["id"] == 1
    and [it["id"] for it in T.blocked_items(_d_open)] == [2],
    "next=%r blocked=%r"
    % (T.next_item(_d_open)["id"], [it["id"] for it in T.blocked_items(_d_open)]),
)
_d_done = T._norm([
    {"id": 1, "text": "基础", "state": "done"},
    {"id": 2, "text": "上层", "state": "pending", "depends_on": [1]},
])
case(
    "依赖：依赖结了 → 依赖项就绪（next 给它）",
    T.next_item(_d_done)["id"] == 2
    and not T.blocked_items(_d_done),
    "next=%r" % (T.next_item(_d_done)["id"],),
)
_d_dangle = T._norm([
    {"id": 1, "text": "甲", "state": "pending", "depends_on": [99]},
])
case(
    "依赖：悬空依赖（指向不存在的 id）被忽略，不卡死账本",
    T.next_item(_d_dangle)["id"] == 1
    and not T.blocked_items(_d_dangle),
    "next=%r" % (T.next_item(_d_dangle)["id"],),
)
_c_base = T._norm([{"id": 1, "text": "X", "state": "pending", "depends_on": [2]}])
_c_new, _c_rej = T.apply_changes(
    _c_base, [{"id": 2, "text": "Y", "depends_on": [1]}]
)
case(
    "依赖：写成环的变更被拒（成环会让就绪判定永远为空）",
    len(_c_rej) == 1 and len(_c_new) == 1,
    "rejected=%r new=%r" % (_c_rej, _c_new),
)
_p_items = T._norm([
    {"id": 1, "text": "低", "state": "pending", "priority": "low"},
    {"id": 2, "text": "高", "state": "pending", "priority": "high"},
])
case(
    "优先级：就绪项按 high 先做",
    T.next_item(_p_items)["id"] == 2,
    "next=%r" % (T.next_item(_p_items)["id"],),
)
case(
    "面板：被依赖卡住的项要说清在等谁",
    "等 #1" in T.render(_d_open),
    T.render(_d_open).replace("\n", " | "),
)

# ── 7. 变异体自证：守卫/环检测真的有牙（不是碰巧）────────────────
# 把 todo.py 拷一份，摘掉守卫 → 回退应当被放行；摘掉环检测 → 成环应当被放行。
# 两次都放行 = 证明上面那些判据拦的确实是这两个函数。
import importlib.util as _ilu  # noqa: E402

_mut_dir = tempfile.mkdtemp(prefix="todomut_")
_mut_path = os.path.join(_mut_dir, "todo_mut.py")
with open(os.path.join(HERE, "todo.py"), encoding="utf-8") as _f:
    _mut_src = _f.read()
_guard_anchor = (
    "    if (old, new) in BACKWARD:\n        return bool(_reopen_reason(reopen))"
)
_cycle_anchor = "    return found"
case(
    "变异体：守卫与环检测的锚点各恰 1 处（变异体才有效）",
    _mut_src.count(_guard_anchor) == 1 and _mut_src.count(_cycle_anchor) == 1,
    "guard=%d cycle=%d" % (_mut_src.count(_guard_anchor), _mut_src.count(_cycle_anchor)),
)
with open(_mut_path, "w", encoding="utf-8") as _f:
    _f.write(
        _mut_src.replace(
            _guard_anchor,
            "    if (old, new) in BACKWARD:\n        return True  # MUT 摘掉守卫",
        )
    )
_spec = _ilu.spec_from_file_location("todo_mut_guard", _mut_path)
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_m_new, _m_rej = _mod.apply_changes(
    _g_base, [{"id": 1, "state": "pending"}]
)
case(
    "变异体：摘掉守卫 → 回退被放行（证明判据咬的是守卫本身）",
    _m_new[0]["state"] == "pending" and not _m_rej,
    "state=%r rejected=%r" % (_m_new[0]["state"], _m_rej),
)
with open(_mut_path, "w", encoding="utf-8") as _f:
    _f.write(_mut_src.replace(_cycle_anchor, "    return []  # MUT 摘掉环检测"))
_spec2 = _ilu.spec_from_file_location("todo_mut_cycle", _mut_path)
_mod2 = _ilu.module_from_spec(_spec2)
_spec2.loader.exec_module(_mod2)
_m2_new, _m2_rej = _mod2.apply_changes(
    _c_base, [{"id": 2, "text": "Y", "depends_on": [1]}]
)
case(
    "变异体：摘掉环检测 → 成环被放行（证明判据咬的是环检测本身）",
    len(_m2_rej) == 0 and len(_m2_new) == 2,
    "rejected=%r new=%r" % (_m2_rej, _m2_new),
)
shutil.rmtree(_mut_dir, ignore_errors=True)

bad = [c for c in CASES if not c[1]]
for name, ok, detail in CASES:
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, ("  -- " + detail) if detail else ""))
print("\nTODO_SELFTEST %d/%d" % (len(CASES) - len(bad), len(CASES)))
if bad:
    print("TODO_SELFTEST_RED")
    sys.exit(1)
print("TODO_SELFTEST_OK")
