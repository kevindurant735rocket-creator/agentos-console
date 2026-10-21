#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""todo_selftest —— 任务账本的证伪集。

账本是「把清单变成真约束」的那一层，所以它的判据不能只看格式，
还要看**语义**：pending 项必须被认出来、done 不算、面板要能让人一眼看懂。
"""

import json
import os
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

bad = [c for c in CASES if not c[1]]
for name, ok, detail in CASES:
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, ("  -- " + detail) if detail else ""))
print("\nTODO_SELFTEST %d/%d" % (len(CASES) - len(bad), len(CASES)))
if bad:
    print("TODO_SELFTEST_RED")
    sys.exit(1)
print("TODO_SELFTEST_OK")
