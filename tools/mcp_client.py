#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mcp_client —— MCP（Model Context Protocol）stdio 客户端：行分隔 JSON-RPC 2.0。

为什么是它（W3 波，生态互操作）：
    2026 年的 agent 生态里 MCP 是工具接入口的既成事实。AgentOS 的 TOOLS 是
    静态注册表，MCP 服务器是外部进程 —— 中间必须有一层把「tools/list 发现 +
    tools/call 调用」翻译成我们的工具层形状，且**失败一律 fail-closed**
    （死服务器 / 超时 / ID 错配都不许装成成功）。

口径（V1，全标准库）：
  * 传输：stdio（子进程 stdin/stdout），每行一条 JSON-RPC 2.0 消息。
  * 握手：先 initialize（协议版本 2026-07-28）；服务器报错/超时 → 自动降级为
    **无状态直连**（跳过 initialized 通知，直接 tools/list）—— 降级要如实记录
    （handshake="stateless"），绝不把「握不上手」装成握手成功。
  * 工具面：tools/list 的每个工具注入为 mcp_<server>_<tool>；调用走
    tools/call；isError 原样映射成 ok=False。
  * 风险档：注册时按配置给档（默认 read；标 write 的逐条走外层审批矩阵）。
  * fail-closed：读一行超时 / EOF / 匹配不上的 id → 报错，绝不当成功。
  * 密钥不外泄：服务器子进程 env 剔除本机 LLM key。
  * 配置：~/.agentos/config.toml 的 [mcp.servers.<名>]（本模块只接受显式
    dict，读盘的事由 agent_core._mcp_sync 负责 —— 本模块不碰任何配置路径）。

用法（被 agent_core 挂载）：sync_tools(tools_dict, cfg, risk_read, risk_write)
裁决：本模块不做自证；mcp_gate 拿微型 stdio 服务器夹具证伪它。
"""
import atexit
import json
import os
import select
import subprocess

PROTOCOL_VERSION = "2026-07-28"
INIT_TIMEOUT = 5.0            # 握手单步超时
RPC_TIMEOUT = 15.0            # 常规请求超时
CALL_TIMEOUT = 60.0           # 工具调用超时（外部工具可能慢）
MAX_RESULT_CHARS = 16000      # 单次工具结果回灌上限
_SECRET_KEYS = ("AGENTOS_LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                "GH_TOKEN", "GITHUB_TOKEN")

_LIVE = []                    # 活着的服务器（atexit 统一收尸）


class MCPError(Exception):
    """连接 / 协议层错误（fail-closed 统一出口）。"""


def encode(msg):
    """一条消息 → 一行 bytes（行分隔 JSON-RPC 2.0）。"""
    return (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")


def decode(line):
    """一行 → dict；空行 / 坏 JSON 直接抛（不许静默跳过）。"""
    try:
        obj = json.loads(line)
    except ValueError as e:
        raise MCPError("响应不是合法 JSON：%r（%s）" % (str(line)[:80], e))
    if not isinstance(obj, dict):
        raise MCPError("响应不是 JSON object：%r" % (str(line)[:80],))
    return obj


def tool_name(server, tool):
    """mcp_<server>_<tool>；非 [A-Za-z0-9_-] 一律换 _（工具名要能进 schema）。"""
    def clean(s):
        return "".join(c if (c.isalnum() or c in "_-") else "_"
                       for c in str(s))
    return "mcp_%s_%s" % (clean(server), clean(tool))


class Server(object):
    """一个 MCP stdio 服务器进程 + 请求/响应配对（严格的 id 对账）。"""

    def __init__(self, name, argv, env=None, cwd=None):
        self.name = str(name)
        self.argv = [str(x) for x in (argv or [])]
        if not self.argv:
            raise MCPError("服务器 %s 的 cmd 为空" % self.name)
        self.proc = None
        self.handshake = "none"
        self._rid = 0
        self._env = dict(os.environ)
        for k in _SECRET_KEYS:
            self._env.pop(k, None)      # 本机密钥不进第三方服务器进程
        if env:
            self._env.update({str(k): str(v) for k, v in env.items()})
        self.cwd = cwd

    # ── 进程与帧 ───────────────────────────────────────────────
    def start(self):
        try:
            self.proc = subprocess.Popen(
                self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=self._env, cwd=self.cwd,
                text=True, bufsize=1)
        except OSError as e:
            raise MCPError("服务器 %s 起不来：%s" % (self.name, e))
        _LIVE.append(self)
        return self

    def _readline(self, timeout):
        """带超时读一行。超时 / EOF 都抛 —— 死服务器不许被当成空响应。"""
        r, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not r:
            raise MCPError("服务器 %s 响应超时（%ss）" % (self.name, timeout))
        line = self.proc.stdout.readline()
        if not line:
            raise MCPError("服务器 %s 已退出/EOF（rc=%s）"
                           % (self.name, self.proc.poll()))
        return line.strip()

    def _request(self, method, params=None, timeout=RPC_TIMEOUT):
        self._rid += 1
        rid = self._rid
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise MCPError("写请求失败（%s 可能已死）：%s" % (self.name, e))
        while True:
            obj = decode(self._readline(timeout))
            if obj.get("id") != rid:
                raise MCPError(
                    "响应 id 错配：等 %s 得 %r（fail-closed，不把发错的"
                    "响应当成自己的）" % (rid, obj.get("id")))
            if "error" in obj and obj["error"] is not None:
                raise MCPError("服务器 %s 报错：%s"
                               % (self.name, str(obj["error"])[:160]))
            return obj.get("result")

    def _notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            pass

    # ── 协议面 ────────────────────────────────────────────────
    def initialize(self):
        """握手；失败**降级**为无状态直连（handshake='stateless'）。

        降级条件 = 服务器对 initialize 报错 / 超时 / 已死 —— 2026-07-28 起的
        无状态规范允许跳过握手直连；但降级只改握手，不改 id 对账与 fail-closed，
        且必须如实记录，绝不装成握过手。
        """
        try:
            res = self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agentos", "version": "1"},
            }, timeout=INIT_TIMEOUT)
        except MCPError:
            self.handshake = "stateless"
            return None
        self.handshake = "ok"
        self._notify("notifications/initialized")
        return res

    def list_tools(self):
        res = self._request("tools/list", {}, timeout=RPC_TIMEOUT)
        tools = (res or {}).get("tools")
        if not isinstance(tools, list):
            raise MCPError("tools/list 没给出 tools 数组：%r"
                           % (str(res)[:120],))
        return tools

    def call_tool(self, tool, args):
        res = self._request("tools/call",
                            {"name": str(tool),
                             "arguments": args if isinstance(args, dict)
                             else {}},
                            timeout=CALL_TIMEOUT)
        res = res if isinstance(res, dict) else {}
        text = []
        for c in res.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "text":
                text.append(str(c.get("text") or ""))
        out = "\n".join(text)[:MAX_RESULT_CHARS]
        return {"ok": not bool(res.get("isError")), "result": out,
                "tool": str(tool), "handshake": self.handshake}

    def close(self):
        if self not in (None,):
            try:
                _LIVE.remove(self)
            except ValueError:
                pass
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:                                   # noqa: BLE001
            try:
                self.proc.kill()
            except Exception:                               # noqa: BLE001
                pass


@atexit.register
def _close_all():
    for srv in list(_LIVE):
        try:
            srv.close()
        except Exception:                                   # noqa: BLE001
            pass


def servers_from_config(cfg):
    """[mcp.servers] 段 → {名: {argv, risk, tools}}；非法条目 fail-closed 报错。"""
    out = {}
    for name, ent in ((cfg or {}).get("servers") or {}).items():
        if not isinstance(ent, dict) or not ent.get("cmd"):
            raise MCPError("mcp.servers.%s 缺少 cmd 数组" % name)
        argv = ent.get("cmd")
        if not isinstance(argv, list) or not argv \
                or not all(isinstance(x, str) for x in argv):
            raise MCPError("mcp.servers.%s.cmd 必须是非空字符串数组" % name)
        risk = str(ent.get("risk") or "read").lower()
        if risk not in ("read", "write"):
            raise MCPError("mcp.servers.%s.risk 只认 read/write（得 %r）"
                           % (name, risk))
        tmap = ent.get("tools") if isinstance(ent.get("tools"), dict) else {}
        tmap2 = {}
        for k, v in tmap.items():
            v = str(v).lower()
            if v not in ("read", "write"):
                raise MCPError("mcp.servers.%s.tools.%s 只认 read/write"
                               % (name, k))
            tmap2[str(k)] = v
        out[str(name)] = {"argv": argv, "risk": risk, "tools": tmap2}
    return out


def sync_tools(tools_dict, cfg, risk_read="read", risk_write="write"):
    """发现并把 MCP 工具注册进 tools_dict（形状与 @_tool 一致）。

    返回 {"added": [...], "servers": {名: {handshake, tools}}, "errors": [...]}。
    挂载失败是**如实记一行错误**（调用方落 stderr），不是静默跳过。
    """
    added, servers, errors = [], {}, []
    for name, ent in sorted(servers_from_config(cfg).items()):
        srv = Server(name, ent["argv"])
        try:
            srv.start()
            srv.initialize()
            listed = srv.list_tools()
        except MCPError as e:
            errors.append("%s: %s" % (name, str(e)[:140]))
            srv.close()
            continue
        ok_n = 0
        for t in listed:
            tname = str((t or {}).get("name") or "")
            if not tname:
                errors.append("%s: tools/list 有缺名条目" % name)
                continue
            risk = ent["tools"].get(tname, ent["risk"])
            full = tool_name(name, tname)
            desc = str((t or {}).get("description") or "")[:200]
            schema = (t or {}).get("inputSchema")
            if not isinstance(schema, dict):
                schema = {"type": "object", "properties": {}}
            tools_dict[full] = {
                "name": full,
                "risk": risk_read if risk == "read" else risk_write,
                "description": "[MCP %s] %s" % (name, desc or tname),
                "schema": schema,
                "fn": _make_fn(srv, tname),
            }
            added.append(full)
            ok_n += 1
        servers[name] = {"handshake": srv.handshake, "tools": ok_n}
    return {"added": added, "servers": servers, "errors": errors}


def _make_fn(srv, tname):
    """工具闭包：闭在 Server 实例上 —— 服务器进程随本进程存活（atexit 收尸）。"""
    def _fn(a, ctx):
        return srv.call_tool(tname, a)
    return _fn
