#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mcp_selftest —— 「关于 MCP 门的那道门」：证明 mcp_gate 判得红也判得绿。

做法（纯 stdlib、夹具=微型 stdio 服务器，自起自杀）：
  把 tools/mcp_client.py 拷到临时目录、单点改坏一处（3 个变异体），跑同一道
  mcp_gate（--client 指变异副本）：
    M1 id 错配不报   → D6 必红（发错的响应被硬吃）
    M2 降级被绕      → D5 必红（initialize 被拒时直接炸，而不是如实降级）
    M3 死服务器不报  → D7 必红（EOF 被包装成"空结果"装成功）
  变异体跑完后双自查：生产 work/mcp_gate.json 字节不变 + 临时目录清空
  （先清再断言，断言不许测「还没清」的中间态）。

裁决：MCP_GATE_SELFTEST n/n + MCP_GATE_SELFTEST_OK / _RED，exit 0/1
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CLIENT = os.path.join(HERE, "mcp_client.py")
CORE = os.path.join(HERE, "agent_core.py")
GATE = os.path.join(HERE, "mcp_gate.py")
PROD_JSON = os.path.join(ROOT, "work", "mcp_gate.json")

CASES = []


def case(name, ok, detail=""):
    CASES.append({"name": name, "ok": bool(ok), "detail": str(detail)[:240]})
    print(("  PASS " if ok else "  FAIL ") + name + " :: " + str(detail)[:180])


MUTANTS = [
    # (说明, 锚点原文, 替换, 期望点名的判据前缀)
    ("M1 id 错配不报（发错的响应被硬吃）",
     "            if obj.get(\"id\") != rid:",
     "            if False:  # MUT id 错配不报",
     "D6"),
    ("M2 降级被绕（initialize 被拒即炸）",
     "        except MCPError:\n            self.handshake = \"stateless\"",
     "        except MCPError:\n            raise  # MUT 降级被绕",
     "D5"),
    ("M3 死服务器不报（EOF 装成空结果）",
     "        if not line:\n"
     "            raise MCPError(\"服务器 %s 已退出/EOF（rc=%s）\"\n"
     "                           % (self.name, self.proc.poll()))",
     "        if not line:\n"
     "            return '{\"jsonrpc\": \"2.0\", \"id\": %d, "
     "\"result\": {}}' % self._rid  # MUT 死服务器不报",
     "D7"),
]


def run_gate(client_path, out_path, timeout=900):
    return subprocess.run([sys.executable, GATE, "--client", client_path,
                           "--core", CORE, "--out", out_path],
                          capture_output=True, text=True, timeout=timeout)


def main():
    print("MCP_GATE 变异体自证")
    prod_before = b""
    if os.path.exists(PROD_JSON):
        with open(PROD_JSON, "rb") as f:
            prod_before = f.read()
    tmp = tempfile.mkdtemp(prefix="mcpmut_")
    try:
        p0 = run_gate(CLIENT, os.path.join(tmp, "base.json"))
        case("BASE 真源全绿（MCP_GATE OK + rc=0）",
             p0.returncode == 0 and "MCP_GATE_OK" in p0.stdout,
             "rc=%s ok=%s" % (p0.returncode,
                              "MCP_GATE_OK" in p0.stdout))

        src = open(CLIENT, encoding="utf-8").read()
        for name, anchor, repl, want in MUTANTS:
            mut_path = os.path.join(tmp, "mcp_client_mut.py")
            n = src.count(anchor)
            if n != 1:
                case(name, False,
                     "锚点命中 %d 处（须恰 1）——变异体无效" % n)
                continue
            with open(mut_path, "w", encoding="utf-8") as f:
                f.write(src.replace(anchor, repl, 1))
            try:
                r = run_gate(mut_path, os.path.join(tmp, "mut.json"))
            except subprocess.TimeoutExpired:
                case(name, False, "门超时")
                continue
            hit = ("FAIL %s" % want) in r.stdout
            case(name, r.returncode != 0 and hit,
                 "rc=%d 期望红「%s」→%s"
                 % (r.returncode, want, "命中" if hit else "未命中"))

        prod_after = b""
        if os.path.exists(PROD_JSON):
            with open(PROD_JSON, "rb") as f:
                prod_after = f.read()
        case("生产 work/mcp_gate.json 字节不变",
             prod_before == prod_after,
             "before=%dB after=%dB" % (len(prod_before), len(prod_after)))

        shutil.rmtree(tmp, ignore_errors=True)
        case("临时目录已清理（变异副本不残留）", not os.path.exists(tmp),
             "仍存在=%s" % os.path.exists(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [c for c in CASES if not c["ok"]]
    print("\nMCP_GATE_SELFTEST %d/%d" % (len(CASES) - len(bad), len(CASES)))
    print("MCP_GATE_SELFTEST_OK" if not bad else "MCP_GATE_SELFTEST_RED")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
