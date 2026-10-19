#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""t2 verify_gate —— 参考 obra/superpowers verification-before-completion.

任何"完成/修好/全绿"断言必须附本消息内 fresh 命令 rc+输出尾行,
否则 BLOCK. 口头断言即红. 纯函数, 不碰现有门链.
"""
import re

DONE_RE = re.compile(r"(完成|修好|全绿|DONE|PASS|已收工|已交付)")
RC_RE = re.compile(r"\brc\s*=\s*0\b")
EVID_PAT = re.compile(r"(AGENT_|GATE.*OK|OK\s+\d+/\d+|rc=0)")


def check_claim(text):
    t = str(text or "")
    if not DONE_RE.search(t):
        return {"verdict": "PASS", "reason": "no-completion-claim"}
    if RC_RE.search(t) and EVID_PAT.search(t):
        return {"verdict": "PASS", "reason": "fresh-evidence"}
    return {"verdict": "BLOCK", "reason": "no-fresh-evidence"}


if __name__ == "__main__":
    assert check_claim("随便聊聊").get("verdict") == "PASS"
    assert check_claim("已完成，无证据").get("verdict") == "BLOCK"
    assert check_claim("已完成 AGENT_CORE OK 153/153 rc=0").get("verdict") == "PASS"
    # M2 变异: verdict 写死 true 必红 -> 自检拒绝
    print("VERIFY_GATE_OK 3/3")
