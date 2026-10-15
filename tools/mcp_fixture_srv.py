#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mcp_fixture_srv —— MCP stdio 服务器的微缩夹具（供 --check / mcp_gate 用）。

模式（argv[1]）：
  good        正常服务器（initialize / tools/list / tools/call 全通）
  refuse-init 对 initialize 回 error（专测无状态降级）
  bad-id      所有响应 id+100（专测 id 错配 fail-closed）
  die         进程立即退出（专测死服务器 fail-closed）

echo 工具的回显里带 key=PRESENT/ABSENT —— 断言「密钥不进第三方服务器 env」。
本文件不是产品：不进 TOOLS、不被 import，只在测试夹具里被子进程真跑。
"""
import json
import os
import sys

MODE = sys.argv[1] if len(sys.argv) > 1 else "good"


def wid(rid):
    """bad-id 模式把回执 id 全打偏 100 —— 对 id 对账 fail-closed 的实证前提。"""
    return rid + 100 if MODE == "bad-id" else rid


def w(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


if MODE == "die":
    sys.exit(3)

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        m = json.loads(line)
    except ValueError:
        continue
    rid = m.get("id")
    meth = m.get("method")
    if meth == "initialize":
        if MODE == "refuse-init":
            w({"jsonrpc": "2.0", "id": wid(rid),
               "error": {"code": -32601, "message": "no init"}})
        else:
            w({"jsonrpc": "2.0", "id": wid(rid), "result": {
                "protocolVersion": "2026-07-28", "capabilities": {},
                "serverInfo": {"name": "fixture", "version": "0"}}})
    elif meth == "tools/list":
        w({"jsonrpc": "2.0", "id": wid(rid), "result": {"tools": [
            {"name": "echo", "description": "回显",
             "inputSchema": {"type": "object",
                             "properties": {"q": {"type": "string"}}}},
            {"name": "set_thing", "description": "写入夹具",
             "inputSchema": {"type": "object",
                             "properties": {"v": {"type": "string"}}}}]}})
    elif meth == "tools/call":
        p = m.get("params") or {}
        if p.get("name") == "set_thing":
            w({"jsonrpc": "2.0", "id": wid(rid), "result": {
                "isError": True,
                "content": [{"type": "text", "text": "read-only fixture"}]}})
        else:
            sec = "PRESENT" if "AGENTOS_LLM_API_KEY" in os.environ else "ABSENT"
            a = p.get("arguments") or {}
            w({"jsonrpc": "2.0", "id": wid(rid), "result": {"content": [
                {"type": "text",
                 "text": "echo: %s key=%s"
                         % (json.dumps(a, ensure_ascii=False), sec)}]}})
    elif meth == "notifications/initialized":
        pass
    else:
        if rid is None:
            continue
        w({"jsonrpc": "2.0", "id": wid(rid), "result": {}})
