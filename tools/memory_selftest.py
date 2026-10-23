#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memory_selftest —— 跨会话记忆检索（memory_retrieval）的证伪集。

为什么要有这一层（2026-10-22，已知缺口第 4 条）
------------------------------------------------
`docs/ARCHITECTURE.md` 的已知缺口写着：
  「跨会话记忆的检索：记忆是整段注入，没有按相关性检索。」

AgentOS 之前**完全不能**从过去的会话里学：会话记录（work/agent_sessions/*.jsonl）
只是落盘，下一轮会话里谁也不读它。唯一一点「跨会话」的信息是 evo 规则账
（ACTIVE 的那些），而且它们是**整段注入**——不管当前任务是什么，全塞进去。

本模块把「按相关性挑」做成确定性的（tokenize → 加权重叠 + 长度归一化），
并提供 `memory_search` 工具按需深挖。判据钉五件事：

  1. 相关命中排在不相关前面（相关性不是随机）；
  2. 空 query / 无命中 → 空（不造假注入）；
  3. 摘要封顶 + 不整段注入；
  4. 工具层：memory_search 能跑、返回 hits；
  5. **变异体自证**：把 score 改成常数 → 相关性排序必须翻红（判据有牙）。

离线跑（用临时 fixture，不联网、不碰真会话）。

裁决：MEMORY_SELFTEST n/n + MEMORY_SELFTEST_OK，exit 0/1
"""

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

CASES = []


def case(name, ok, detail=""):
    CASES.append((name, bool(ok), str(detail)))
    print(("PASS " if ok else "FAIL ") + name + ("  -- " + str(detail)[:160] if detail else ""))


def _fixture(root):
    """造一段像样的记忆源（会话记录 + 规则 + 项目记忆）。"""
    os.makedirs(os.path.join(root, "work", "agent_sessions"), exist_ok=True)
    # 两条会话：一条关于 token 节约，一条关于 UI 设计
    with open(
        os.path.join(root, "work", "agent_sessions", "s1.jsonl"),
        "w",
        encoding="utf-8",
    ) as f:
        f.write(
            json.dumps({"ts": 1, "kind": "user", "text": "怎么把每轮 prompt token 压下去"})
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "ts": 2,
                    "kind": "assistant",
                    "text": "把工具 schema 瘦身，系统提示去重，记忆改成摘要。",
                }
            )
            + "\n"
        )
    with open(
        os.path.join(root, "work", "agent_sessions", "s2.jsonl"),
        "w",
        encoding="utf-8",
    ) as f:
        f.write(json.dumps({"ts": 3, "kind": "user", "text": "UI 配色怎么选"}) + "\n")
        f.write(
            json.dumps({"ts": 4, "kind": "assistant", "text": "用 tailwind 的 slate 色板"})
            + "\n"
        )
    # 规则
    with open(
        os.path.join(root, "work", "agent_evo_rules.json"), "w", encoding="utf-8"
    ) as f:
        json.dump(
            {
                "version": 1,
                "rules": [
                    {
                        "id": "evo-token",
                        "state": "ACTIVE",
                        "advice": "token 节约：schema 瘦身 + 摘要注入",
                    },
                    {"id": "evo-ui", "state": "CANDIDATE", "advice": "UI 配色用 slate"},
                ],
            },
            f,
            ensure_ascii=False,
        )
    # 项目记忆
    with open(os.path.join(root, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write("# 仓库纪律\n## 1. token\n**铁律**：不推自有内容到 GitHub。\n")


def main():
    import memory_retrieval as MR
    import agent_core as ac

    print("MEMORY_SELFTEST 跨会话记忆检索证伪")

    root = tempfile.mkdtemp(prefix="mem_")
    _fixture(root)

    # ── 1. 相关性排序（不是随机、不是按时间）──────────────────
    hits = MR.search("token 节约", root=root, limit=5)
    case(
        "相关命中排在不相关前面",
        len(hits) >= 2
        and hits[0]["source"] in ("s1.jsonl", "evo-token")
        and hits[0]["score"] > hits[-1]["score"],
        repr([(h["source"], h["score"]) for h in hits]),
    )

    # ── 2. 空 query / 无命中 → 空（不造假注入）──────────────
    case("空 query → 不查（避免噪声）", MR.search("", root=root) == [], "")
    case(" nonsense query → 无相关命中", MR.search("zzzzzzzzzzz", root=root) == [], "")

    # ── 3. 封顶：摘要不会把整个 transcript 塞进去 ────────────
    blk = MR.build_memory_block("token", root=root, cap=200)
    case(
        "摘要封顶（不超过 cap）",
        blk != "" and len(blk) <= 260,
        "len=%d" % len(blk),
    )
    case(
        "摘要不整段注入 transcript（只给 top-k）",
        "agent_sessions" not in blk and "→" in blk,
        blk[:120].replace("\n", " | "),
    )

    # ── 4. 工具层：memory_search 能跑、返回 hits ──────────────
    r = ac._t_memory_search({"query": "token 节约", "limit": 3}, {"root": root, "mode": "full", "sid": "ms"})
    case(
        "工具层：memory_search 能跑、返回 hits",
        r.get("ok") is True and r.get("n", 0) >= 1,
        "n=%s err=%r" % (r.get("n"), r.get("error")),
    )
    r2 = ac._t_memory_search({"query": ""}, {"root": root, "mode": "full", "sid": "ms"})
    case(
        "工具层：空 query → 结构化拒绝",
        r2.get("ok") is False and "缺 query" in str(r2.get("error") or ""),
        repr(r2.get("error"))[:80],
    )
    r3 = ac._t_memory_search({"query": "token", "limit": 1}, {"root": root, "mode": "full", "sid": "ms"})
    case(
        "工具层：limit 生效（只返回 1 条）",
        r3.get("n") == 1,
        "n=%s" % r3.get("n"),
    )

    # ── 5. 变异体自证：把 score 改成常数 → 相关性排序必须翻红 ──
    _mut_dir = tempfile.mkdtemp(prefix="memmut_")
    _mut_path = os.path.join(_mut_dir, "memory_retrieval_mut.py")
    with open(os.path.join(HERE, "memory_retrieval.py"), encoding="utf-8") as f:
        _src = f.read()
    _anchor = "    qset = set(query_tokens)\n    dset = set(doc_tokens)"
    case(
        "变异体：score 的锚点恰 1 处（变异体才有效）",
        _src.count(_anchor) == 1,
        "命中 %d 处" % _src.count(_anchor),
    )
    with open(_mut_path, "w", encoding="utf-8") as f:
        f.write(
            _src.replace(
                _anchor,
                "    qset = set(query_tokens)\n    dset = set(doc_tokens)\n"
                "    return 1.0  # MUT：把分数改成常数（相关性 disappears）",
                1,
            )
        )
    import importlib.util as ilu

    _spec = ilu.spec_from_file_location("memory_retrieval_mut", _mut_path)
    _mod = ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _mut_hits = _mod.search("token 节约", root=root, limit=5)
    # 真实实现里 top1 是 token 相关；变异版分数全相等 → 排序退化为按 source 字母序
    _real_top = MR.search("token 节约", root=root, limit=1)[0]["source"]
    _mut_top = _mut_hits[0]["source"] if _mut_hits else None
    case(
        "变异体：把 score 改常数 → top1 不再是最相关的那条（判据有牙）",
        _mut_top != _real_top or not _mut_hits,
        "真实 top=%r 变异 top=%r" % (_real_top, _mut_top),
    )
    shutil.rmtree(_mut_dir, ignore_errors=True)

    bad = [c for c in CASES if not c[1]]
    print("\nMEMORY_SELFTEST %d/%d" % (len(CASES) - len(bad), len(CASES)))
    print("MEMORY_SELFTEST_OK" if not bad else "MEMORY_SELFTEST_RED")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
