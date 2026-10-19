#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent_core_gate —— 正向门：终端 agent 内核真的能跑，且只会跑它该跑的。

为什么单独立这道门（而不是让 agent_core 自己的 --check 顶掉）
----------------------------------------------------------------
`--check` 是**内核自证**：它证明 EXPECT_N 条用例当下是绿的。但「用例数变少」
「退出码被吞」「FAIL 行被 grep 掉」这类把自证本身掏空的手法，自证看不
见自己。这道门站在外面，只认三件事：
   1. 真实跑 `agent_core.py --check`，rc 必须为 0，且 stdout 里必须出现
      逐字的 `AGENT_CORE OK N/N`（EXPECT_N 写死在下方，少一条就红）；
   2. 编译必须过（py_compile）；
   3. 不依赖任何模型/网络/浏览器：只读本机真实存在的白名单命令表。
另外做两条独立复核（不复用 --check 的措辞）：
   4. 硬阻断清单独立打一次：git push / rm / find -delete 必须被拒；
   5. 自检留下的临时目录必须已被清掉（否则就是把活文件写进项目里了）。

纯 stdlib、亚秒级、不碰浏览器、不占端口、只绑回环临时端口（那也在
agent_core 自己的 Stub 里），所以可以插在门链末尾而不拖时间。

真端点冒烟默认**不跑**（门不许依赖外部模型在不在）：要跑就
`AGENT_CORE_GATE_REAL=1 bash tools/gates.sh agent_core_gate`。
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(ROOT, "tools", "agent_core.py")
# 33 → 40：新增 7 条 Responses 协议用例（入参翻译 / 工具拍扁 / 未知协议
# fail-closed / 端到端两步 / SSE+usage 映射 / 失败分类 / REPL 保协议）。
# 40 → 46：R2 自进化 5 例（挖掘/注入/回退/账坏 fail-closed/坏账拒动账）
# + R3 ideas 挖掘 1 例（2026-10-11 v0.2.0）。
# 46 → 48：过程可见性 2 例（tool_call 事件序列 + 转轮编排器）。
# 48 → 51：蒸馏轮 3 例（AGENTS.md 记忆层级两态/4KB 截断 + 瞬时词表）。
# 51 → 54：CC 差距收敛 3 例（Esc cancel 即抛 / undo 恢复 / undo 耗尽如实报）。
# 54 → 56：GitHub 一等能力 2 例（三只读工具注册+argv 白名单 / 纪律注入）。
# 56 → 58：R2 2 例（policy v2 agent 自主路径矩阵 / deploy 密钥扫描）。
# 58 → 61：自进化 v2 3 例（tool-fail 签名+interrupted 不入账 / 描述改写两态 /
# midflight 节流+2 倍阈值提案）。
# 61 → 69：DSP 决策监督计划 8 例（schema 三态 / 落盘取回 / 计划锁 /
# 审批解锁 / 白名单两态 / doom 归一化三连 / 证据与链 / 拆分与防膨胀）。
# 69 → 73：DSP 结构强化 4 例（分析器 BIG/SMALL / 自启幂等 / 自启指令 /
# 主循环自启落盘）。
# 73 → 74：W0-4 REPL 补全表防漂 1 例（dispatch 斜杠命令 ⊆ REPL_SLASH_CMDS）。
# 74 → 80：W1 compact v2 + rewind 6 例（摘要进历史 / 失败逐字回退 / 聚焦透传 /
# 重放对齐 / 端到端 note / 越界 fail-closed）。
# 80 → 85：W2 subagent-as-tool 5 例（可重入 / 禁嵌套 / 步数封顶 / 子集强制 /
# 权限继承）。
# 85 → 91：W3 MCP stdio 客户端 6 例（纯面 / 握手 / 降级 / id 错配 / 死服务器 /
# 注册映射）。
# 91 → 96：W4 hooks + skills 5 例（拦截与 glob / fail-closed / 预算截断 /
# /skill 注入 / hook_context 进 prompt）。
# 96 → 102：W5 终端 UX 6 例（续行 / @补全 / ⎿详情两条 / 终答缩进 / 事件集成）。
# 102 → 108：W6 并行子代理 + skills 自动触发 6 例（batch barrier / 批量边界 /
# 失败隔离 / 单发回归 / triggers 解析 / 自动注入两态）。
# 108 → 114：W7 会话分支 + 计划自验收 6 例（branches 原语 / fork 对账 /
# verify 五态 / 端到端 pending / 不提前 done / 缺记录 fail-closed）。
# 114 → 120：W8 监督通道 + 监督面 + 多智能体协作 6 例（steer 往返 / 注入 /
# 幂等 / watch 渲染 / chain 交接 / merge 汇总）。
# 120 → 125：W9 安全闸开关 5 例（flags 纯转换 / 计划锁开关 / 模式开关+硬阻断
# 不可关 / 白名单随锁 / 子代理继承）。
# 125 → 128：W10 自由面 3 例（flag 默认翻转 / 硬阻断形态三件套 / 命令面自由
# 真跑 + 子代理继承命令面；W9 五例改写成「显式 true 才上锁」两向断言）。
# 计数钉同步清单：agent_core_gate.py(EXPECT_N) · dsp_gate.py(文档 +
# EXPECT_CHECK) · dsp_gate_selftest.py(2 锚点) · rewind_gate.py(2) ·
# subagent_gate.py(2) · mcp_gate.py(2) · hooks_gate.py(2) · repl_ux_gate.py(2) ·
# branches_gate.py(2) · supervise_gate.py(2) · safety_gate.py(2) ·
# gates.sh(相关注释)。
# 计数钉同步清单（W2 首轮链曾因漏 rewind_gate 一处而红门）：
#   agent_core_gate.py(EXPECT_N) · dsp_gate.py(文档 + EXPECT_CHECK) ·
#   dsp_gate_selftest.py(2 锚点) · rewind_gate.py(2) · subagent_gate.py(2) ·
#   mcp_gate.py(2) · hooks_gate.py(2) · repl_ux_gate.py(2) · gates.sh(相关注释)。
# 改这个数必须与 agent_core._selftest 的真实用例数同时改，否则门形同虚设。
# 131 → 134：W13 自主执行 3 例（/goal 建计划 / 未审批拒跑 / 桩小闭环）
# 140 → 142：W18 no_fallback 2 例（主端点抖动只重试主端点 / cancel 出口）。
# 142 → 147：W22 会话中毒双预算 5 例（RemoteDisconnected/429/IncompleteRead
# 词表补齐在词表本身；中毒 400 词表外也轮换重试 / 轮换独立预算 3 次到顶 /
# 分层退避循环 reroll / 提案复用幂等 / 真圆闭环四圈教训五例——见 progress.md）。
EXPECT_N = 152
TOKEN = "AGENT_CORE OK %d/%d" % (EXPECT_N, EXPECT_N)
# 自检临时目录带 pid（并发安全），所以残留判据用 glob，别去猜具体名字。
STRAY_GLOB = os.path.join(ROOT, "work", "_st_agent_core*")
STRAY_NAME = "_st_agent_core"


def verdict(out):
    """把 `agent_core.py --check` 的 stdout 判成 0/1。

    暴露成函数是为了让 neg_agent_core 能拿它证伪：
    喂一份被掏空的输出（少了用例、FAIL 被洗掉）必须判非零，
    否则这道门自己就是空门。
    """
    if TOKEN not in out:
        return 1
    passes = [ln for ln in out.splitlines() if ln.startswith("PASS ")]
    fails = [ln for ln in out.splitlines() if ln.startswith("FAIL ")]
    if fails or len(passes) < EXPECT_N:
        return 1
    return 0


def _sh(argv, timeout=900):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def main():
    bad = []
    print("== agent_core_gate：内核自检 %d 条 + 编译 + 硬阻断独立复核" % EXPECT_N)

    # 1+2. 真跑 --check，读真实 rc 与逐字令牌 -------------------
    p = _sh([sys.executable, CORE, "--check"])
    sys.stdout.write(p.stdout)
    if p.stderr.strip():
        sys.stderr.write(p.stderr)
    if p.returncode != 0:
        bad.append(
            "agent_core.py --check rc=%d（真实退出码，不是看输出猜的）" % p.returncode
        )
    if verdict(p.stdout) != 0:
        bad.append("stdout 里没有逐字 %s，或 PASS 行不足 %d 条" % (TOKEN, EXPECT_N))

    # 3. 编译必须过 -------------------------------------------
    c = _sh([sys.executable, "-m", "py_compile", CORE])
    if c.returncode != 0:
        bad.append("py_compile rc=%d：%s" % (c.returncode, (c.stderr or "")[-200:]))
    else:
        print("PASS py_compile rc=0")

    # 4. 硬阻断独立复核：直接 import，不吃 --check 的任何措辞 -----
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    try:
        import agent_core as ac
    except Exception as e:  # noqa: BLE001
        bad.append("import agent_core 失败：%s" % e)
        ac = None
    if ac is not None:
        denials = [
            ("git push", ["git", "push"]),
            ("rm -rf /", ["rm", "-rf", "/"]),
            ("find -delete", ["find", ".", "-delete"]),
            ("git reset --hard", ["git", "reset", "--hard"]),
            ("dd", ["dd", "if=/dev/zero", "of=/dev/sda"]),
        ]
        for label, argv in denials:
            why = ac._hard_deny(argv)
            if not why:
                bad.append("硬阻断漏了 %s：_hard_deny 返回 None" % label)
            else:
                print("PASS 硬阻断 %-16s -> %s" % (label, why[:52]))
        ok_cmds = sorted(ac._term_cmds())
        if "rm" in ok_cmds:
            bad.append("只读白名单里竟然有 rm")
        else:
            print("PASS 只读白名单 %d 条，rm 不在其中" % len(ok_cmds))
        if os.path.realpath(ac.ROOT) != os.path.realpath(ROOT):
            bad.append("agent_core.ROOT 指到别处去了：%s" % ac.ROOT)

    # 5. 自检不许把临时文件留在项目里 -------------------------
    import glob

    stray = sorted(os.path.basename(p) for p in glob.glob(STRAY_GLOB))
    if stray:
        bad.append(
            "work/%s* 没被清掉：自检在项目里留了活文件（%s）"
            % (STRAY_NAME, ", ".join(stray[:5]))
        )
    else:
        print("PASS 自检临时目录已清空（work/%s* 无残留）" % STRAY_NAME)

    # 6. 真端点冒烟：默认不跑 ---------------------------------
    if os.environ.get("AGENT_CORE_GATE_REAL") == "1":
        try:
            import time

            t0 = time.time()
            r = _sh(
                [sys.executable, CORE, "-p", "reply with exactly: PONG"], timeout=600
            )
            took = time.time() - t0
            if r.returncode != 0 or "PONG" not in r.stdout:
                bad.append(
                    "真端点冒烟 rc=%d（%.1fs）：%s"
                    % (r.returncode, took, (r.stdout or "")[-200:])
                )
            else:
                print("PASS 真端点冒烟 rc=0，%.1fs，回答里有 PONG" % took)
        except subprocess.TimeoutExpired:
            bad.append("真端点冒烟超时（>600s）")
    else:
        print("SKIP 真端点冒烟（未设 AGENT_CORE_GATE_REAL=1；门不依赖外部模型）")

    if bad:
        print("AGENT_CORE_GATE RED %d 条：" % len(bad))
        for b in bad:
            print("FAIL %s" % b)
        return 1
    print(
        "AGENT_CORE_GATE OK 自检 %d 条全绿 / 编译 rc=0 / 硬阻断 5 项独立复核通过"
        % EXPECT_N
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
