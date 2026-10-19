#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mcp_gate —— W3 生态互操作门：MCP stdio 客户端真握手、降级如实、fail-closed。

为什么必须有这道门（W3 波首件交付）：
  MCP 客户端的每一层都能「看起来在工作」：握手可以假装握过、id 错配可以
  被硬吃、死服务器可以返回空结果装成功、密钥可以顺手带进第三方进程。
  本门站在外面认这些事实（纯离线：夹具是本地微型 stdio 服务器）：
    D1  mcp_client 可导入 + 纯面（encode/decode 往返、名字净化、
        servers_from_config 非法配置 fail-closed）
    D2  `--check` 末行逐字 `AGENT_CORE OK 155/155`（数漂移即红）
    D3  源码锚点：协议版本 / id 错配 / 降级如实记录 / EOF 区分 各恰 1；
        agent_core 侧 `_mcp_sync` 定义 1 处 + 调用点 ≥2
    D4  良好路径（行为）：握手 ok + list 2 工具 + call 回显
    D5  降级（行为）：initialize 被拒 → handshake="stateless" 且仍可用
    D6  id 错配（行为）：报错含「错配」（fail-closed 不误配）
    D7  死服务器（行为）：报 EOF/已退出 且不 hang（有界时间）
    D8  注册（行为）：sync_tools 注入 mcp_<server>_<tool> + 逐工具风险映射
    D9  密钥不外泄（行为）：构造 Server 时 env 已剔除本机 key
    D10 零污染：work/ 只多 --out；夹具自起自杀

用法: python3 tools/mcp_gate.py [--client PATH] [--core PATH] [--out PATH]
裁决：MCP_GATE OK n/n（rc=0）/ MCP_GATE RED（rc=2，有漏网）
"""
import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_CLIENT = os.path.join(HERE, "mcp_client.py")
DEFAULT_CORE = os.path.join(HERE, "agent_core.py")
DEFAULT_OUT = os.path.join(ROOT, "work", "mcp_gate.json")
FIXTURE = os.path.join(HERE, "mcp_fixture_srv.py")
EXPECT_CHECK = "AGENT_CORE OK 155/155"

results = []


def gate(name, ok, detail=""):
    results.append({"gate": name, "ok": bool(ok), "detail": str(detail)[:200]})
    print(("PASS " if ok else "FAIL ") + name
          + ("  -- " + str(detail)[:150] if detail else ""))


def load_mod(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _finish(out_path, client_path):
    n_ok = sum(1 for r in results if r["ok"])
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump({"client": os.path.relpath(client_path, ROOT),
                   "judgments": results, "n_ok": n_ok,
                   "n_total": len(results)}, fh,
                  ensure_ascii=False, indent=2, sort_keys=True)
    print("MCP_GATE %d/%d" % (n_ok, len(results)))
    if n_ok == len(results):
        print("MCP_GATE_OK")
        return 0
    print("MCP_GATE_RED")
    print("  红判据=%s" % [r["gate"] for r in results if not r["ok"]])
    return 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", default=DEFAULT_CLIENT)
    ap.add_argument("--core", default=DEFAULT_CORE)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()
    print("== mcp_gate：MCP stdio 客户端（client=%s）"
          % os.path.relpath(args.client, ROOT))

    work = os.path.join(ROOT, "work")
    before = set(os.listdir(work)) if os.path.isdir(work) else set()
    try:
        csrc = open(args.client, encoding="utf-8").read()
        asrc = open(args.core, encoding="utf-8").read()
    except OSError as e:
        print("MCP_GATE_RED 读不到文件：%s" % e)
        return 2

    # D1 可导入 + 纯面
    MC = None
    try:
        MC = load_mod(args.client, "mcp_client_g")
        ok1 = (MC.decode(MC.encode({"a": 1}).decode())["a"] == 1
               and MC.tool_name("srv.a", "echo/b") == "mcp_srv_a_echo_b")
        try:
            MC.servers_from_config({"servers": {"x": {"risk": "rw"}}})
            ok1 = False
            d1 = "非法 risk 未被拒"
        except MC.MCPError as e:
            d1 = "非法条目拒=%s" % ("cmd" in str(e) or "risk" in str(e))
        try:
            MC.servers_from_config({"servers": {"y": {"risk": "read"}}})
            ok1 = False
            d1 += " 缺 cmd 未被拒"
        except MC.MCPError:
            pass
        gate("D1 可导入 + 纯面（往返/净化/非法配置 fail-closed）", ok1, d1)
    except Exception as e:                                     # noqa: BLE001
        gate("D1 可导入 + 纯面（往返/净化/非法配置 fail-closed）", False,
             repr(e)[:120])
    if MC is None:
        return _finish(args.out, args.client)

    # D2 --check 逐字令牌
    try:
        p = subprocess.run([sys.executable, args.core, "--check"],
                           capture_output=True, text=True, timeout=900)
        tail = [ln for ln in p.stdout.splitlines() if "AGENT_CORE OK" in ln]
        gate("D2 --check 末行逐字 %s" % EXPECT_CHECK,
             p.returncode == 0 and bool(tail)
             and tail[-1].strip() == EXPECT_CHECK,
             "rc=%s tail=%r" % (p.returncode,
                                (tail[-1] if tail else "")[:44]))
    except Exception as e:                                     # noqa: BLE001
        gate("D2 --check 末行逐字 %s" % EXPECT_CHECK, False, repr(e)[:120])

    # D3 源码锚点
    n_ver = csrc.count('PROTOCOL_VERSION = "2026-07-28"')
    n_mis = csrc.count("响应 id 错配")
    n_deg = csrc.count('self.handshake = "stateless"')
    n_eof = csrc.count("已退出/EOF")
    n_def = asrc.count("def _mcp_sync()")
    n_call = asrc.count("_mcp_sync()")
    gate("D3 源码锚点（协议版本/id 错配/降级/EOF + _mcp_sync 定义 1 处、调用 ≥2）",
         (n_ver, n_mis, n_deg, n_eof, n_def) == (1, 1, 1, 1, 1)
         and n_call >= 3,  # 定义 1 + REPL 1 + headless 1
         "ver=%d mis=%d deg=%d eof=%d def=%d call=%d"
         % (n_ver, n_mis, n_deg, n_eof, n_def, n_call))

    # D4 良好路径
    ok4 = False
    srv4 = None
    if os.path.isfile(FIXTURE):
        srv4 = MC.Server("fx", [sys.executable, FIXTURE, "good"])
        try:
            srv4.start()
            srv4.initialize()
            tl = srv4.list_tools()
            cr = srv4.call_tool("echo", {"q": 1})
            we = srv4.call_tool("set_thing", {"v": "x"})
            ok4 = (srv4.handshake == "ok" and len(tl) == 2
                   and "echo:" in (cr.get("result") or "")
                   and cr.get("ok") is True and we.get("ok") is False)
        except Exception:                                      # noqa: BLE001
            ok4 = False
        finally:
            srv4.close()
    gate("D4 良好路径：握手 ok + list 2 + call 回显 + isError 映射",
         ok4, "handshake=%s" % (getattr(srv4, "handshake", "-"),))

    # D5 降级
    ok5 = False
    srv5 = None
    if os.path.isfile(FIXTURE):
        srv5 = MC.Server("fx", [sys.executable, FIXTURE, "refuse-init"])
        try:
            srv5.start()
            srv5.initialize()
            tl5 = srv5.list_tools()
            ok5 = (srv5.handshake == "stateless" and len(tl5) == 2)
        except Exception:                                      # noqa: BLE001
            ok5 = False
        finally:
            srv5.close()
    gate("D5 降级：initialize 被拒 → stateless 如实记录且仍可用",
         ok5, "handshake=%s" % (getattr(srv5, "handshake", "-"),))

    # D6 id 错配
    ok6 = False
    srv6 = None
    if os.path.isfile(FIXTURE):
        srv6 = MC.Server("fx", [sys.executable, FIXTURE, "bad-id"])
        try:
            srv6.start()
            srv6.list_tools()
        except MC.MCPError as e:
            ok6 = "错配" in str(e)
        except Exception:                                      # noqa: BLE001
            ok6 = False
        finally:
            srv6.close()
    gate("D6 id 错配 fail-closed（不把发错的响应当自己的）", ok6,
         "ok=%s" % ok6)

    # D7 死服务器
    ok7 = False
    srv7 = None
    if os.path.isfile(FIXTURE):
        import time as _t
        t0 = _t.time()
        srv7 = MC.Server("fx", [sys.executable, FIXTURE, "die"])
        try:
            srv7.start()
            srv7.list_tools()
        except MC.MCPError as e:
            ok7 = (("已退出" in str(e)) or ("EOF" in str(e))) \
                and (_t.time() - t0) < 10
        except Exception:                                      # noqa: BLE001
            ok7 = False
        finally:
            srv7.close()
    gate("D7 死服务器 fail-closed（报 EOF/退出且不 hang）", ok7,
         "ok=%s" % ok7)

    # D8 注册 + 风险映射
    ok8 = False
    det8 = ""
    if os.path.isfile(FIXTURE):
        tcopy = {}
        try:
            rep = MC.sync_tools(
                tcopy, {"servers": {"fxs": {
                    "cmd": [sys.executable, FIXTURE, "good"],
                    "tools": {"set_thing": "write"}}}},
                risk_read="read", risk_write="write")
            ok8 = (sorted(rep.get("added") or []) ==
                   ["mcp_fxs_echo", "mcp_fxs_set_thing"]
                   and tcopy["mcp_fxs_echo"]["risk"] == "read"
                   and tcopy["mcp_fxs_set_thing"]["risk"] == "write"
                   and callable(tcopy["mcp_fxs_echo"]["fn"]))
            det8 = "added=%s" % (rep.get("added"),)
        except Exception as e:                                 # noqa: BLE001
            ok8, det8 = False, repr(e)[:100]
    gate("D8 注册：mcp_<server>_<tool> 注入 + 逐工具风险映射", ok8, det8)

    # D9 密钥不外泄（构造即剔除，无需起进程）
    try:
        os.environ["AGENTOS_LLM_API_KEY"] = "st-secret-for-gate"
        srv9 = MC.Server("x", ["/bin/true"])
        bor = [k for k in ("AGENTOS_LLM_API_KEY", "OPENAI_API_KEY",
                           "GH_TOKEN") if k in srv9._env]
    finally:
        os.environ.pop("AGENTOS_LLM_API_KEY", None)
    gate("D9 密钥不外泄：Server env 已剔除本机 key", not bor,
         "残=%s" % (bor or "无"))

    # D10 零污染
    after = set(os.listdir(work)) if os.path.isdir(work) else set()
    new = sorted((after - before) - {os.path.basename(args.out)})
    gate("D10 零污染（work/ 只多 --out）", not new, "新增=%s" % (new or "无"))
    return _finish(args.out, args.client)


if __name__ == "__main__":
    sys.exit(main())
