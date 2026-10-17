#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""t1 子代理文件化 —— 参考 qwen-code SubagentConfig / opencode TaskAgent.

.agentos/agents/*.md 前matter 子集:
    name / description / tools(逗号) / disallowedTools(逗号)
    approvalMode(plan|auto-edit|yolo|bubble) / maxTurns(int) / model(str)
只读约束: bubble=后台确认上浮父会话排队(默认); 探索型默认只读工具集.
纯 stdlib, 无外部依赖. 新文件不碰现有门链.
"""
import os

DEFAULT_READONLY = {"Glob", "Grep", "LS", "View", "Read", "GlobTool", "GrepTool"}
VALID_MODES = {"plan", "auto-edit", "yolo", "bubble"}


def parse_agent_file(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    meta, body = {}, text
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
                k = k.strip()
                if k:
                    meta[k] = v.strip()
            body = "\n".join(lines[end + 1:]).lstrip("\n")
    name = meta.get("name") or os.path.splitext(os.path.basename(path))[0]
    def _lst(k):
        return [x.strip() for x in meta.get(k, "").replace("，", ",").split(",") if x.strip()]
    mode = meta.get("approvalMode", "bubble")
    if mode not in VALID_MODES:
        mode = "bubble"
    try:
        max_turns = int(meta.get("maxTurns", "12"))
    except ValueError:
        max_turns = 12
    return {"name": name, "description": meta.get("description", ""),
            "tools": _lst("tools"), "disallowedTools": _lst("disallowedTools"),
            "approvalMode": mode, "maxTurns": max_turns,
            "model": meta.get("model", "inherit"), "systemPrompt": body, "path": path}


def discover(root):
    out = {}
    if not root or not os.path.isdir(root):
        return []
    for fn in sorted(os.listdir(root)):
        if fn.endswith(".md"):
            it = parse_agent_file(os.path.join(root, fn))
            if it:
                out[it["name"]] = it
    return [out[k] for k in sorted(out)]


def effective_tools(cfg):
    """探索型默认只读: tools为空则用只读集; 再减 disallowedTools."""
    base = set(cfg.get("tools") or list(DEFAULT_READONLY))
    return sorted(base - set(cfg.get("disallowedTools") or []))


def needs_escalation(cfg):
    """bubble 模式后台确认上浮父会话, 其余按模式字面."""
    return (cfg.get("approvalMode") or "bubble") in ("bubble", "plan")


if __name__ == "__main__":
    import tempfile
    d = tempfile.mkdtemp(prefix="agents_")
    with open(os.path.join(d, "explorer.md"), "w", encoding="utf-8") as f:
        f.write("---\ndescription: 探索只读\napprovalMode: bubble\nmaxTurns: 20\n---\n你是探索子代理。\n")
    items = discover(d)
    assert len(items) == 1 and items[0]["name"] == "explorer", items
    assert items[0]["approvalMode"] == "bubble" and items[0]["maxTurns"] == 20
    assert set(effective_tools(items[0])) <= DEFAULT_READONLY
    assert needs_escalation(items[0]) is True
    print("AGENT_FILES_OK 1/1")
