#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gate_pollution —— 零污染判据的共享口径。

**为什么需要共享**
零污染判据（「门跑完 work/ 里不许多出东西」）在十几个门里各写了一遍，
于是同一个坑被踩了十几遍：2026-10-20 一次全链复验里，
`repl_ux_gate` / `branches_gate` / `supervise_gate` / `safety_gate`
**同时**判红，红的全是同一条 —— 「work/ 多出 `_st_agent_core.<pid>`」。

那不是污染，是**并发**：门在跑的时候，别的进程（另一条门链、一次手动
自检）正在跑 `agent_core --check`，它的临时目录带 pid，恰好落在这个
窗口里。判据把「活着的并发自检」当成了「本门留下的垃圾」。

**口径**
- `_st_agent_core.<pid>` 且 **pid 进程还活着** → 并发，不是污染，不算。
- 其它新增项 → 一律算污染（真残留要判红）。
- pid 已经不在了 → 那才是真残留（上一轮被杀的自检留下的），要算。

抽成共享模块：口径只写一份，将来再有一道门就不会再各踩一遍。
"""

import os
import re

_LIVE_SELFTEST = re.compile(r"^_st_agent_core\.(\d+)$")


def live_selftest_dirs(entries):
    """从「新增项」里挑出**属于活着的并发自检**的那些。

    entries：work/ 的条目名列表（通常是 after - before）。
    返回其中的并发自检目录名集合（该从污染清单里排除）。
    """
    live = set()
    for name in entries or []:
        m = _LIVE_SELFTEST.match(str(name))
        if not m:
            continue
        try:
            pid = int(m.group(1))
        except (TypeError, ValueError):
            continue
        try:
            os.kill(pid, 0)
        except OSError:
            continue  # 进程没了 = 真残留，不排除
        except Exception:  # noqa: BLE001  判不准就不排除（从严）
            continue
        live.add(name)
    return live


def pollution_diff(after, before, allowed=()):
    """返回 (污染清单, 并发自检清单)。

    after / before：work/ 的条目集合。allowed：本门允许新增的（如 --out）。
    """
    after = set(after or [])
    before = set(before or [])
    new = sorted((after - before) - set(allowed or ()))
    live = live_selftest_dirs(new)
    return sorted(set(new) - live), sorted(live)
