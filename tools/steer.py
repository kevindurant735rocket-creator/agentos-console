#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""steer —— 老板监督指令通道：时时刻刻能插手，每条只生效一次。

语义：
  * 收件箱 work/steer/<sid>.jsonl —— 老板（另一终端 `agentos steer`、或
    REPL `/steer`）追加指令行；append-only，多写方安全、不覆盖。
  * 游标 work/steer/<sid>.cursor —— 跑着的 agent 在**每个步骤边界**读增量、
    注入本轮、推进游标（只进不退）。重启安全（游标在盘上）、幂等（同一条
    指令只进一次）。
  * 只配送不裁决：指令注入为 [老板指令] 的 user 消息 + note 事件；怎么执行
    由模型与既有审批矩阵决定 —— 监督通道**不许变成绕过写锁的后门**。
裁决：本模块不做自证；supervise_gate 拿夹具证伪它。
"""
import json
import os
import time


def _sid_ok(sid):
    """会话 id 形状闸（防路径穿越：sid 会进文件名，不许 / . 开头等）。"""
    s = str(sid or "")
    return (bool(s) and len(s) <= 100 and not s.startswith(".")
            and "/" not in s and "\\" not in s
            and all(c.isalnum() or c in "._-" for c in s))


def _p(d, sid):
    return (os.path.join(d, "%s.jsonl" % sid),
            os.path.join(d, "%s.cursor" % sid))


def push(d, sid, text):
    """老板投一条指令。返回 (ok, err)。"""
    if not _sid_ok(sid):
        return False, "会话 id 形状不对：%r" % str(sid)[:40]
    t = str(text or "").strip()
    if not t:
        return False, "指令为空"
    try:
        os.makedirs(d, exist_ok=True)
        p, _ = _p(d, sid)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": round(time.time(), 3), "text": t},
                               ensure_ascii=False) + "\n")
        return True, ""
    except OSError as e:
        return False, "投递失败：%s" % str(e)[:120]


def read_all(d, sid):
    """箱内全部指令（坏行跳过——坏的是那一行，不是整箱）。"""
    if not _sid_ok(sid):
        return []
    p, _ = _p(d, sid)
    out = []
    try:
        with open(p, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    r = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("text"):
                    out.append(r)
    except OSError:
        return []
    return out


def _cursor(d, sid):
    _, c = _p(d, sid)
    try:
        with open(c, encoding="utf-8") as f:
            return max(0, int(f.read().strip() or "0"))
    except (OSError, ValueError):
        return 0


def pending(d, sid):
    """未消费的指令 → (items, total_n)。"""
    items = read_all(d, sid)
    cur = min(_cursor(d, sid), len(items))
    return items[cur:], len(items)


def commit(d, sid, n):
    """推进游标到 n（只进不退：乱序/并发写方都不会让指令回放）。"""
    if not _sid_ok(sid):
        return False, "会话 id 形状不对：%r" % str(sid)[:40]
    _, c = _p(d, sid)
    try:
        os.makedirs(d, exist_ok=True)
        cur = _cursor(d, sid)
        with open(c, "w", encoding="utf-8") as f:
            f.write("%d\n" % max(int(n), cur))
        return True, ""
    except OSError as e:
        return False, "游标写失败：%s" % str(e)[:120]
