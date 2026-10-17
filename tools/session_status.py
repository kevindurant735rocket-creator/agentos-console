#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""t3 session_status —— 参考 agent-deck sessionstatus.go / claude-squad instance.

Derive(tool,prior,hook,ack,now) 纯函数, 单点拥有状态派生, 不轮询猜.
规则: Stopped 最高压制一切; dead->Error; waiting+Ack+非codex->Idle;
HookFastPathWindow=120s 内 hook 优先; codex running 20s / waiting 5s 新鲜度.
纯 stdlib, 不碰现有门链.
"""
import time

FAST_WINDOW = 120
CODEX_RUNNING_TTL = 20
CODEX_WAITING_TTL = 5


def derive(tool="codex", prior="Idle", hook=None, ack=False, now=None):
    now = float(now if now is not None else time.time())
    hook = hook or {}
    kind = str(hook.get("kind") or "")
    state = str(hook.get("state") or "")
    ts = float(hook.get("ts") or 0)
    # Stopped 最高优先级
    if state == "Stopped" or prior == "Stopped":
        return "Stopped"
    if state == "dead" or prior == "Error":
        if state == "dead":
            return "Error"
    fresh = (now - ts) <= FAST_WINDOW if ts else False
    if fresh:
        if state == "dead":
            return "Error"
        if state in ("running", "Running"):
            return "Running"
        if state in ("waiting", "Waiting"):
            # waiting + 已确认 + 非 codex -> Idle
            if ack and tool != "codex":
                return "Idle"
            return "Waiting"
    # 无新鲜 hook: codex 按 TTL 衰减, 其余保持 prior
    if tool == "codex" and ts is not None:
        age = now - ts
        if prior == "Running" and age > CODEX_RUNNING_TTL:
            return "Idle"
        if prior == "Waiting" and age > CODEX_WAITING_TTL:
            return "Waiting"  # 等待保持, 由 ack 推进
    return prior


if __name__ == "__main__":
    n = 1000.0
    assert derive("codex", "Running", {"state": "Stopped", "ts": n}, now=n) == "Stopped"
    assert derive("codex", "Running", {"state": "dead", "ts": n}, now=n) == "Error"
    assert derive("claude", "Waiting", {"state": "waiting", "ts": n}, ack=True, now=n + 1) == "Idle"
    assert derive("codex", "Waiting", {"state": "waiting", "ts": n}, ack=True, now=n + 1) == "Waiting"
    assert derive("codex", "Running", {"state": "running", "ts": n - 1000}, now=n) == "Idle"
    print("SESSION_STATUS_OK 5/5")
