#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""浏览器基础设施重试语义 —— act_gate / shoot / view_dod / a11y_walk 四门共用（D50）。

为什么抽成共享模块（2026-10-03 实测）：D50 偶发红的根因之一是**只有 act_gate 一家有重试**。
shoot.py / view_dod.py / a11y_walk.py 三门都真拉 Chrome TargetClosedError 直接判红，
于是同一条代码同一台机器能跑出「22/22 全绿」与「shoot 红」两种结果。
四家各抄一份 = 四份迟早漂移；所以这里做单一真源，其余三家 `from flake_retry import run_gate`。

铁律不变（D40）：判据返回 False 是**结论**，一次都不重试；只有抛异常的
浏览器/连接层故障才重试，且重试痕迹写进 detail 留痕。放宽成「什么都重试」= 门有洞。

按 FLAKE-RETRY-BEGIN/END 标记整段包起来，是为了让 flake_retry_selftest.py
按标记抽块 exec —— 免得本模块 import 时把四道真浏览器门一起拖起来。
标记必须**恰好一对**，少/多直接判红（抽到空块 = 假绿）。
"""
import os
import time

# 重试间隔可被 CD_FLAKE_SLEEP 覆盖（自测里要亚秒级，不真睡）。
# 这是**加快**自测速度，不是放宽判据；语义与默认 2.0s 完全一致。
RETRY_SLEEP_S = float(os.environ.get("CD_FLAKE_SLEEP", "2.0"))


class TargetClosedError(Exception):
    """与 Playwright 同名的异常类型，供 CD_FLAKE_INJECT 自测注入用。"""


def flake_inject():
    """注入点：CD_FLAKE_INJECT=always 时抛基础设施故障。放在门体最前面，
    于是自测**不碰 Chrome、不占端口、亚秒级**，却能证明 run_gate 包裹真的接上了。
    删掉包裹就会变成裸 traceback + rc=1 → 门必红，这就是它的牙。"""
    if os.environ.get("CD_FLAKE_INJECT") == "always":
        raise TargetClosedError(
            "Page.wait_for_timeout: Target page, context or browser has been closed"
            "  [CD_FLAKE_INJECT=always 故意注入]")


# --- FLAKE-RETRY-BEGIN 浏览器基础设施重试（D40 实测修的门自身的抖动） -------
# 2026-10-02 实测：同一条代码、同一台机器，连续两次 act_gate
# 一次 21/22（红在「建议按钮」，EXC TargetClosedError），一次 22/22 全绿。
# 真因不是判据错，而是本机常态 40+ codex 进程 / 10 核，chromium 拉起后
# driver 侧 target 偶发被回收（页还没等到 aria-live 的 rc 就没了）。
# 这类红是假红：会让引擎 livecheck 的 D27 随机变红，把真信号淹在噪声里。
# 但修法有红线：只重试基础设施故障，绝不重试判据失败。
# 判据返回 False（ok=False）是结论不是故障，那种一律直接判红，一次都不重试。
# 只有抛异常的「浏览器/连接层」故障才重试，且重试痕迹写进 detail 留痕，不静默掩盖。
_INFRA_MARKS = (
    "TargetClosedError", "Browser has been closed", "has been closed",
    "Target page, context or browser", "Connection refused",
    "WebSocket error", "browserType.launch: Executable",
    "net::ERR_CONNECTION_REFUSED", "net::ERR_ABORTED", "Protocol error",
)
# （原先这里硬写 2.0；现由文件头的 CD_FLAKE_SLEEP 提供，默认仍是 2.0）
RETRY_N = 2  # 首次 + 2 次重试 = 最多 3 次尝试


def _is_infra(exc):
    """只认基础设施层故障：判定口径固定为「异常类型 + 消息」，不做更宽的模糊匹配。"""
    txt = "%s: %s" % (type(exc).__name__, exc)
    return any(m in txt for m in _INFRA_MARKS)


def run_gate(fn, ctx, name=""):
    """跑一条门；基础设施故障才重试，判据红一律原样判红。"""
    last = None
    for attempt in range(RETRY_N + 1):
        try:
            ok, detail = fn(ctx)
            if attempt:
                detail = "重试×%d 后过 | %s" % (attempt, detail)
            return bool(ok), detail
        except Exception as e:
            if not _is_infra(e) or attempt == RETRY_N:
                return False, "EXC %s: %s" % (type(e).__name__, e)
            last = e
            time.sleep(RETRY_SLEEP_S * (attempt + 1))
    return False, "EXC %s: %s" % (type(last).__name__, last) if last else "EXC 未知"
# --- FLAKE-RETRY-END ---
