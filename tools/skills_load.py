#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""skills_load —— skills 目录加载（~/.agentos/skills/*/SKILL.md + 项目 .agentos/skills/）。

极简 frontmatter（纯 stdlib，不引 YAML 库）：文件头部的
    ---
    name: 技能名
    description: 一句话
    triggers: 词一, 词二
    ---
    <正文 …>
只认 name / description / triggers 三个键（其余行原样忽略）；name 缺省取
目录名；triggers = 逗号分隔的触发词（自动触发**只认显式词**做子串命中，
不做模糊猜测）。description 单条 ≤500 字符、整段 ≤4000 字符进 system
prompt —— 超了**截断并留标记**（不许静默超预算，也不许悄悄丢技能）；项目根
.agentos/skills 的同名技能覆盖用户目录（近者优先）。正文经 /skill <名> 手动
注入，或 triggers 命中时自动注入（预算 4000）。
裁决：本模块不做自证；hooks_gate 拿夹具目录证伪它。
"""
import os

PER_ITEM = 500
TOTAL_BUDGET = 4000


def parse_skill(path):
    """→ {"name","description","body","path"} 或 None（读不动即 None）。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    name, desc, body, triggers = "", "", text, []
    if text.startswith("---"):
        lines = text.split("\n")
        end = None
        for i in range(1, min(len(lines), 60)):
            if lines[i].strip() == "---":
                end = i
                break
        if end is not None:
            for ln in lines[1:end]:
                k, _, v = ln.partition(":")
                k = k.strip().lower()
                if k == "name":
                    name = v.strip()
                elif k == "description":
                    desc = v.strip()
                elif k == "triggers":
                    triggers = [x.strip() for x in
                                v.replace("，", ",").split(",") if x.strip()]
            body = "\n".join(lines[end + 1:]).lstrip("\n")
    if not name:
        name = os.path.basename(os.path.dirname(path))
    return {"name": name, "description": desc, "body": body,
            "triggers": triggers, "path": path}


def discover(roots):
    """所有技能（按 root 顺序，后者覆盖同名；结果按 name 排序）。"""
    by_name = {}
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for d in sorted(os.listdir(root)):
            sk = os.path.join(root, d, "SKILL.md")
            if os.path.isfile(sk):
                it = parse_skill(sk)
                if it:
                    by_name[it["name"]] = it
    return [by_name[k] for k in sorted(by_name)]


def render_section(items, per=PER_ITEM, total=TOTAL_BUDGET):
    """system prompt 段：技能名 + 截断后的 description；带总量预算与省略标记。"""
    if not items:
        return ""
    out = ["可用技能（/skill <名> 注入正文）："]
    used = len(out[0])
    omitted = 0
    for it in items:
        d = str(it.get("description") or "").strip() or "(无 description)"
        if len(d) > per:
            d = d[:per] + "…"
        line = "- %s: %s" % (it["name"], d)
        if used + len(line) + 1 > total:
            omitted += 1
            continue
        out.append(line)
        used += len(line) + 1
    if omitted:
        out.append("（其余 %d 个技能因预算未列出）" % omitted)
    return "\n".join(out)


def render_index(items, total=1200):
    """**目录式**技能段：只给名字 + 4 词提示；正文靠 skills 工具按需取。

    为什么（2026-10-22 的 token 账）：技能段原来把每条 description 全量注入
    system prompt（本机 6 个技能 = 1,027 字符 ≈ 380 token，**每轮**都付），
    而正文本来就是「按需注入」的（/skill <名> 或 triggers 命中）。
    真正要补的是**索引**：名字在、描述短，具体做法让模型自己拉。

    摘要**确定性**（截断 description，不调模型）。
    """
    if not items:
        return ""
    out = ["可用技能（skills 工具可读正文）："]
    used = len(out[0])
    omitted = 0
    for it in items:
        d = str(it.get("description") or "").strip().split("—")[0].strip()
        words = d.split()
        hint = " ".join(words[:4])
        line = "- %s%s" % (it["name"], (": " + hint) if hint else "")
        if used + len(line) + 1 > total:
            omitted += 1
            continue
        out.append(line)
        used += len(line) + 1
    if omitted:
        out.append("（其余 %d 个技能未列出，用 skills 工具查）" % omitted)
    return "\n".join(out)


def auto_trigger(text, items, limit=2, budget=4000):
    """按 triggers 词命中自动挑技能。返回 [(name, body)]（≤limit 个、总长
    ≤budget）。没有 triggers 的技能不参与自动触发 —— 只认显式词，不猜。"""
    t = str(text or "")
    out = []
    used = 0
    for it in items or []:
        trs = it.get("triggers") or []
        if not trs or not any(str(k) in t for k in trs):
            continue
        body = str(it.get("body") or "").strip()
        if not body:
            continue
        if used + len(body) > budget:
            body = body[:max(0, budget - used)] + "\n…（预算截断）"
        out.append((it.get("name") or "?", body))
        used += len(body)
        if len(out) >= limit:
            break
    return out
