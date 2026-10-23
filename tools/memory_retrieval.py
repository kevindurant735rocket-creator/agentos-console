#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memory_retrieval —— 跨会话记忆的**按相关性检索**（零依赖版）。

为什么要有这一层（2026-10-22，已知缺口第 4 条）
------------------------------------------------
`docs/ARCHITECTURE.md` 的「已知缺口」里写着：
  「跨会话记忆的检索：记忆是整段注入，没有按相关性检索。」

AgentOS 之前**完全不能**从过去的会话里学：会话记录（work/agent_sessions/*.jsonl）
只是落盘，下一轮会话里谁也不读它。唯一一点「跨会话」的信息是 evo 规则账
（agent_evo_rules.json 里 ACTIVE 的那些），而且它们是**整段注入**——
不管当前任务是什么，把所有 ACTIVE 规则都塞进 system prompt。

成熟 agent（Claude Code 的 CLAUDE.md + memory tool、Aider 的 conventions）
的做法是**检索**：给定当前任务，挑出最相关的过去经验，只注入那几条。
本模块做 exactly 这件事，**不用 embedding / 向量库**（零依赖、纯 stdlib、
离线可跑）：

  1. **tokenize**：CJK 单字 + 英文词 + CJK bigram（2 字窗口）—— 中文没有
     分词器也能有可接受的相关性信号；
  2. **score**：加权重叠 + 长度归一化（idf-ish，长文档不白占优势）；
  3. **search**：跨三类记忆源（会话记录 / ACTIVE 规则 / 项目记忆）；
  4. **build_memory_block**：确定性摘要（top-k，每条 ≤ 200 字符），不调模型。

确定性是这个模块的纪律：**摘要不能每次不同**（那会让人分不清是"模型这次
没用"还是"这次没注入"）。所有排序 tie-break 用 source 名字 + ts，可复跑。

纯标准库、无副作用（只读记忆源）、可单测。
"""

import json
import os
import re

CASES = []

# ── 分词（CJK-aware，零依赖）──────────────────────────────────────
_CJK_PUNCT = set("，。！？、；：（）《》【】「」『』—…·")


def tokenize(text):
    """把文本切成一组 token（去重，确定性顺序）。

    CJK 单字 + 英文/数字词 + CJK bigram。为什么加 bigram：中文没有空格，
    单字匹配噪声太大；bigram（2 字窗口）给相关性一个**具体度**信号，
    「记忆检索」能同时命中「记忆」「检索」「记忆检」「忆检索」。
    """
    out = []
    buf = []
    for ch in str(text or ""):
        if ch.isascii() and (ch.isalnum() or ch in "_-"):
            buf.append(ch)
        else:
            if buf:
                out.append("".join(buf).lower())
                buf = []
            if ch.strip() and ch not in _CJK_PUNCT:
                out.append(ch)
    if buf:
        out.append("".join(buf).lower())
    # bigram
    cjk = [ch for ch in str(text or "") if ch.strip() and not ch.isascii() and ch not in _CJK_PUNCT]
    for i in range(len(cjk) - 1):
        out.append(cjk[i] + cjk[i + 1])
    # 去重保序
    seen = set()
    uniq = []
    for t in out:
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def _idfish(tokens):
    # 占位：idf 需要语料级 df；这里用长度惩罚代替（长 token 更具体）
    return [max(1, len(t)) for t in tokens]


def score(query_tokens, doc_tokens):
    """query 对 doc 的相关性分数（0..1 量级）。

    加权重叠 + 长度归一化：长文档不白占优势（ otherwise 一篇 10,000 字的
    会话记录永远压过一篇 100 字的规则）。
    """
    if not query_tokens or not doc_tokens:
        return 0.0
    qset = set(query_tokens)
    dset = set(doc_tokens)
    w = _idfish(query_tokens)
    hit = 0.0
    for t, weight in zip(query_tokens, w):
        if t in dset:
            hit += float(weight)
    # 长度归一化
    denom = (len(qset) ** 0.5) * (len(dset) ** 0.5) + 1.0
    return hit / denom


# ── 记忆源 ────────────────────────────────────────────────────────
def _session_entries(root):
    """work/agent_sessions/*.jsonl → 一个会话一条（user 问 + assistant 答）。"""
    out = []
    if not root:
        return out
    base = os.path.join(str(root), "work", "agent_sessions")
    try:
        files = sorted(
            (f for f in os.listdir(base) if f.endswith(".jsonl")),
            reverse=True,
        )
    except OSError:
        return out
    for fn in files[:50]:  # 不扫爆：最近 50 个会话
        p = os.path.join(base, fn)
        try:
            rows = [
                json.loads(ln)
                for ln in open(p, encoding="utf-8", errors="replace")
                if ln.strip()
            ]
        except (OSError, ValueError):
            continue
        # 拼成「问 → 答」文本（user + assistant 的最终回答）
        users = [str(r.get("text") or "") for r in rows if r.get("kind") == "user"]
        assts = [str(r.get("text") or "") for r in rows if r.get("kind") == "assistant"]
        if not users:
            continue
        q = " ".join(users)
        a = assts[-1] if assts else ""
        text = "%s  →  %s" % (q, a)
        out.append(
            {
                "kind": "session",
                "source": fn,
                "ts": rows[0].get("ts") if rows else 0,
                "text": text[:500],
                "tokens": tokenize(text),
            }
        )
    return out


def _rule_entries(root):
    """ACTIVE 规则 → 一条一条的条目。"""
    out = []
    p = os.path.join(str(root), "work", "agent_evo_rules.json")
    try:
        d = json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        return out
    for r in (d.get("rules") or []):
        if str(r.get("state") or "").upper() != "ACTIVE":
            continue
        adv = str(r.get("advice") or "").strip()
        if not adv:
            continue
        out.append(
            {
                "kind": "rule",
                "source": str(r.get("id") or "rule"),
                "ts": r.get("updated") or 0,
                "text": adv,
                "tokens": tokenize(adv),
            }
        )
    return out


def _memory_entries(root):
    """项目记忆（AGENTS.md / AGENTOS.md）→ 每节标题 + 首条硬规则。"""
    out = []
    for name in ("AGENTS.md", "AGENTOS.md"):
        p = os.path.join(str(root or ""), name)
        try:
            body = open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if not body.strip():
            continue
        # 每节一个条目：标题 + 首条加粗句
        cur = None
        for ln in body.splitlines():
            s = ln.strip()
            if s.startswith("#"):
                cur = s
                continue
            if cur and s.startswith("**"):
                out.append(
                    {
                        "kind": "memory",
                        "source": name,
                        "ts": 0,
                        "text": "%s · %s" % (cur, s[:160]),
                        "tokens": tokenize(cur + " " + s),
                    }
                )
                cur = None  # 一节只取首条
    return out


def search(query, root=None, limit=5, kinds=None):
    """按相关性检索跨会话记忆。返回 [{kind,source,score,text}]。"""
    qtok = tokenize(query)
    if not qtok:
        return []
    kinds = set(kinds) if kinds else None
    cands = []
    cands += _session_entries(root)
    cands += _rule_entries(root)
    cands += _memory_entries(root)
    if kinds:
        cands = [c for c in cands if c["kind"] in kinds]
    scored = []
    for c in cands:
        sc = score(qtok, c["tokens"])
        if sc <= 0:
            continue
        scored.append(
            {
                "kind": c["kind"],
                "source": c["source"],
                "score": round(sc, 4),
                "text": c["text"],
            }
        )
    # 稳定排序：分数降序 → source 名 → ts 降序
    scored.sort(key=lambda x: (-x["score"], x["source"]))
    return scored[: max(1, int(limit or 5))]


def build_memory_block(query, root=None, cap=800, limit=4, min_score=0.05):
    """确定性摘要块（注入 system prompt 用）。无相关命中 → ""。"""
    hits = search(query, root=root, limit=limit)
    hits = [h for h in hits if h["score"] >= min_score]
    if not hits:
        return ""
    out = ["相关经验（按相关性从过往会话/规则里挑的，不是整段注入）："]
    used = len(out[0])
    kept = 0
    for h in hits:
        line = "- [%s] %s（%.2f）" % (h["kind"], h["text"].replace("\n", " ")[:180], h["score"])
        if used + len(line) + 1 > cap:
            break
        out.append(line)
        used += len(line) + 1
        kept += 1
    if kept == 0:
        return ""
    out.append("（更多用 memory_search 工具查；不要把上面当铁律，只是线索）")
    return "\n".join(out)


def main():
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", default="")
    ap.add_argument("--root", default=".")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()
    if not args.query:
        print("用法：memory_retrieval.py <query> [--root DIR] [--limit N]")
        return 0
    hits = search(args.query, root=args.root, limit=args.limit)
    for h in hits:
        print("%.3f  %-8s %-32s %s" % (h["score"], h["kind"], h["source"][:32], h["text"][:80]))
    if not hits:
        print("（无相关命中）")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
