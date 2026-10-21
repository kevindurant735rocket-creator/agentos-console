#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""commit_selftest —— 「commit 工具」的证伪集（收尾闭环那一层）。

为什么要有这个工具（2026-10-22 实测）
------------------------------------
agent 干活干得不错，但**自己不收尾**：两轮真实开发任务（w32 修复回路、w33
主文件拆分）的改动都躺在工作区，是**人替它 commit 的**。「干完就自己收尾」
写在系统提示里没用（模型会忘、会偷懒）—— 得有工具 + 交付闸会查。

这个工具最关键的一条纪律：**只提交本会话动过的路径**。
`git add -A` 会把**别人**（用户自己改到一半的东西）的未提交改动一起带走 ——
那是事故。所以这里逐条钉死：

  1. 只 stage 会话写过的路径（用户的其它未提交文件必须原地不动）；
  2. 路径越出仓库 → 拒绝；
  3. 没有可提交的改动 → 如实说「没有可提交」，不假装成功；
  4. 源码里**不许**出现 `add -A`（那是「一把带走」的写法）；
  5. 提交成功 → 回执里有 commit sha 与 stat（可核对的证据）。

裁决：COMMIT_SELFTEST n/n + COMMIT_SELFTEST_OK，exit 0/1
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

CASES = []


def case(name, ok, detail=""):
    CASES.append((name, bool(ok), str(detail)))
    print(("PASS " if ok else "FAIL ") + name + ("  -- " + str(detail)[:160] if detail else ""))


def _mk_repo():
    d = tempfile.mkdtemp(prefix="commit_repo_")
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", "-C", d] + list(a),
        capture_output=True,
        text=True,
        env=dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null"),
    )
    run("init", "-q", ".")
    run("config", "user.email", "t@t.local")
    run("config", "user.name", "t")
    with open(os.path.join(d, "README.md"), "w", encoding="utf-8") as f:
        f.write("base\n")
    run("add", "README.md")
    run("commit", "-qm", "init")
    # 用户自己的未提交改动（agent 不许碰）
    with open(os.path.join(d, "user_wip.md"), "w", encoding="utf-8") as f:
        f.write("用户改到一半\n")
    return d, run


def main():
    import agent_core as ac

    print("COMMIT_SELFTEST 收尾提交工具证伪")

    # ── 1. 只提交会话写过的路径（用户的未提交改动原地不动）──────
    repo, run = _mk_repo()
    ctx = {"root": repo, "mode": "full", "sid": "ct"}
    with open(os.path.join(repo, "agent_new.py"), "w", encoding="utf-8") as f:
        f.write("print('x')\n")
    ctx["session_write_paths"] = ["agent_new.py"]
    r = ac._t_commit({"message": "加个脚本"}, ctx)
    st = run("status", "--porcelain").stdout
    case(
        "只提交会话写过的路径：用户的未提交改动原地不动",
        r.get("ok")
        and r.get("committed") is True
        and "user_wip.md" in st
        and "agent_new.py" not in st,
        "status=%r result=%r" % (st.strip(), {k: r.get(k) for k in ("ok", "committed", "files")}),
    )
    case(
        "提交回执带 commit sha 与 stat（可核对的证据）",
        bool(r.get("commit"))
        and len(str(r.get("commit"))) >= 7
        and "agent_new.py" in str(r.get("stat") or ""),
        "sha=%r stat=%r" % (r.get("commit"), str(r.get("stat") or "")[:60]),
    )
    names = run("show", "--name-only", "--format=", "HEAD").stdout.split()
    case(
        "commit 里**只有**会话动过的文件（没夹带用户的东西）",
        names == ["agent_new.py"],
        "committed files=%r" % (names,),
    )

    # ── 2. 越界路径拒绝 ───────────────────────────────────────
    r2 = ac._t_commit({"message": "越界", "paths": ["../outside.py"]}, ctx)
    case(
        "路径越出仓库 → 拒绝（收窄检查先拦也是拒）",
        r2.get("ok") is False
        and (
            "不在仓库内" in str(r2.get("error") or "")
            or "本会话写过的文件" in str(r2.get("error") or "")
        ),
        repr(r2.get("error"))[:120],
    )

    # ── 2b. paths 只能收窄会话写过的集合（不许夹带用户的文件）────
    r2b = ac._t_commit(
        {"message": "夹带", "paths": ["user_wip.md"]}, ctx
    )
    case(
        "paths 里塞没写过的文件 → 拒绝（不许夹带用户的未提交改动）",
        r2b.get("ok") is False and "本会话写过的文件" in str(r2b.get("error") or ""),
        repr(r2b.get("error"))[:120],
    )

    # ── 3. 没有改动 → 如实说没有，不假装成功 ───────────────────
    ctx2 = {"root": repo, "mode": "full", "sid": "ct2", "session_write_paths": []}
    r3 = ac._t_commit({"message": "没东西可提"}, ctx2)
    case(
        "本会话没有改动 → 如实说没东西可提交",
        r3.get("ok") is False and "没有改动可提交" in str(r3.get("error") or ""),
        repr(r3.get("error"))[:120],
    )

    # ── 4. 不是 git 仓库 → 如实说 ─────────────────────────────
    plain = tempfile.mkdtemp(prefix="commit_plain_")
    r4 = ac._t_commit(
        {"message": "x"},
        {"root": plain, "mode": "full", "sid": "ct3", "session_write_paths": ["a.py"]},
    )
    case(
        "不是 git 仓库 → 如实说（不假装提交了）",
        r4.get("ok") is False and "不是 git 仓库" in str(r4.get("error") or ""),
        repr(r4.get("error"))[:120],
    )

    # ── 5. 源码级：commit 工具不许用 `add -A` ──────────────────
    import re as _re

    def _code_of(text):
        """去掉 docstring 与注释，只留代码（别把文档里的说明当违规）。"""
        t = _re.sub(r'"""[\s\S]*?"""', "", text)
        return "\n".join(
            ln for ln in t.splitlines() if not ln.strip().startswith("#")
        )

    def _has_add_A(code):
        return (
            '"add", "-A"' in code
            or '"add","-A"' in code
            or "'add', '-A'" in code
            or "add -A" in code
        )

    with open(os.path.join(HERE, "agent_core.py"), encoding="utf-8") as f:
        _src = f.read()
    _i = _src.find('def _t_commit(')
    _seg = _src[_i:_i + 4000] if _i >= 0 else ""
    _code = _code_of(_seg)
    case(
        "源码级：commit 工具的**代码**里不许出现 `add -A`（会夹带别人的改动）",
        not _has_add_A(_code),
        "命中" if _has_add_A(_code) else "无",
    )
    case(
        "判据有牙：把 `add -A` 塞进代码 → 判据必须抓到",
        _has_add_A(_code_of('    argv = ["add", "-A", "--"]\n')),
        "注入后命中=True",
    )

    for d in (repo, plain):
        shutil.rmtree(d, ignore_errors=True)

    bad = [c for c in CASES if not c[1]]
    print("\nCOMMIT_SELFTEST %d/%d" % (len(CASES) - len(bad), len(CASES)))
    print("COMMIT_SELFTEST_OK" if not bad else "COMMIT_SELFTEST_RED")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
