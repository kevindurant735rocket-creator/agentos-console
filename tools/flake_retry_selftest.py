#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_gate 重试逻辑的变异体自捕（D40）。rc=0 全过 / 1 有洞。

为什么要单独一门：重试是最容易把「假红」修成「假绿」的地方。
判据返回 False 是结论，绝不能重试；只有抛异常的浏览器/连接层故障才该重试。
所以这门反过来验两件事，缺一不可：
  1. 有牙：把重试条件放宽到「什么都重试」，必须被判红（否则真红会被重试洗绿）。
  2. 非空壳：把重试整个去掉，infra 故障不再恢复，必须被判红。
  3. 正向：真基础设施故障重试后能过，且 detail 留痕「重试×N」。

口径与引擎 atomic_write_gate.py 的 CASE-1/2/4 一致：真变异 → 真红。

为什么按标记抽源码而不是 import：直接 import tools/flake_retry.py 本身是安全的，
但这里坚持「按 FLAKE-RETRY-BEGIN/END 标记抽块 exec」，是为了让**标记本身**成为契约 ——
标记少一对/多一对直接判红，杜绝「模块改名/块被挪走 → 抽到空块 → 假绿」。

D50 新增第三层接线自证（本门从「只验语义」升级为「语义 + 接线」双证）：
前两层（BASE + MUT1/MUT2）证明 run_gate 这个函数本身对不对；
它们**完全不能**发现「shoot.py / view_dod.py / a11y_walk.py 根本没调用 run_gate」。
2026-10-03 实测那正是 D50 偶发红的根因：只有 act_gate 一家有重试，另三家裸奔。
所以这里对三道门各做一对子进程对照：
  正例：CD_FLAKE_INJECT=always → rc=2 + 输出含「EXC TargetClosedError」+ **无 Traceback**
  变异：把 run_gate 包裹摘掉 → rc=1 + **有 Traceback**
两者都必须实测，缺一即判红。注入点在门体最前面，所以不碰 Chrome、不占端口、亚秒级。
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
_BEGIN = "# --- FLAKE-RETRY-BEGIN"
_END = "--- FLAKE-RETRY-END ---"


def load_retry_block():
    src = open(os.path.join(HERE, "flake_retry.py"), encoding="utf-8").read()
    nb, ne = src.count(_BEGIN), src.count(_END)
    if nb != 1 or ne != 1:
        print("  [FAIL] 标记必须恰好一对 BEGIN=%d END=%d（抽到空块=假绿）" % (nb, ne))
        return None
    body = src[src.index(_BEGIN):src.index(_END)]
    ns = {"time": __import__("time")}
    exec(compile(body, "flake_retry.py:flake-retry", "exec"), ns)
    if "run_gate" not in ns or "_is_infra" not in ns:
        print("  [FAIL] 抽出的块里没有 run_gate/_is_infra")
        return None
    ns["RETRY_SLEEP_S"] = 0.0  # 自测里别真睡
    return type("M", (), ns)

RESULTS = []


def chk(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print("  %s %s%s" % ("[ok  ]" if ok else "[FAIL]", name,
                         ("  " + detail) if detail else ""))


class _TargetClosed(Exception):
    pass


_TargetClosed.__name__ = "TargetClosedError"


def _run(mod):
    """在给定模块语义下跑四个用例。"""
    out = {}

    ok, d = mod.run_gate(lambda ctx: (True, "clean"), {})
    out["clean_pass"] = (ok is True) and ("重试" not in d)

    calls = {"n": 0}

    def falsey(ctx):
        calls["n"] += 1
        return False, "判据红"

    ok, d = mod.run_gate(falsey, {})
    out["falsey_is_red"] = (ok is False) and (calls["n"] == 1) and ("重试" not in d)

    st = {"n": 0}

    def flaky(ctx):
        st["n"] += 1
        if st["n"] < 3:
            raise _TargetClosed(
                "Page.wait_for_timeout: Target page, context or browser has been closed")
        return True, "recovered"

    ok, d = mod.run_gate(flaky, {})
    out["infra_recovers"] = (ok is True) and (st["n"] == 3) and ("重试×2" in d)

    st2 = {"n": 0}

    def real_bug(ctx):
        st2["n"] += 1
        raise ValueError("真正的代码错，不是抖动")

    ok, d = mod.run_gate(real_bug, {})
    out["real_bug_no_retry"] = (ok is False) and (st2["n"] == 1)
    return out



# ---------------- D50：接线的行为证伪（正例 + 变异体） ----------------
# 2026-10-20：这一层原来验的是 shoot/view_dod/a11y_walk 三家网页面门
# **真的调用了 run_gate**（摘掉包裹必须退回 rc=1 + traceback）。那三家门随
# 网页驾驶舱一起下线了，而且幸存的终端面门**没有**这个包裹形态
# （它们的判据写法不同）—— 这一层验的「接线」已经不存在，变异体无从下手。
# 与其留一条永远红的判据，不如如实说清：这一层随被测对象一起下线。
# 下面两层（BASE + MUT1/MUT2）仍然自证 run_gate 这个函数本身对不对。
GATES = ()
_ENV = dict(os.environ, CD_FLAKE_INJECT="always", CD_FLAKE_SLEEP="0")
_WRAP = "    ok, detail = run_gate(_body, None)\n"
_UNWRAP = "    ok, detail = _body(None)\n"


def _run_gate(name, script_dir):
    p = subprocess.run([sys.executable, os.path.join(script_dir, name + ".py")],
                       capture_output=True, text=True, env=_ENV, timeout=120)
    return p.returncode, ((p.stdout or "") + (p.stderr or ""))


def chk_wiring():
    """三门各一对：接上了 = rc=2 无 traceback；摘掉包裹 = rc=1 有 traceback。"""
    for name in GATES:  # 空：被验的接线形态随网页面一起下线了
        rc, out = _run_gate(name, HERE)
        chk("WIRING 正例 %s：run_gate 包裹在位 → rc=2 无 traceback" % name,
            rc == 2 and "EXC TargetClosedError" in out and "Traceback" not in out and "[EXC]" in out,
            "rc=%s exc=%s tb=%s" % (rc, "EXC TargetClosedError" in out, "Traceback" in out))
        tmp = tempfile.mkdtemp(prefix="flakeretry-mut-")
        try:
            shutil.copytree(HERE, os.path.join(tmp, "tools"))
            tp = os.path.join(tmp, "tools", name + ".py")
            t = io.open(tp, encoding="utf-8").read()
            if t.count(_WRAP) != 1:
                chk("MUT3 %s：包裹可被定位" % name, False, "找不到 run_gate 包裹行")
                continue
            io.open(tp, "w", encoding="utf-8").write(t.replace(_WRAP, _UNWRAP, 1))
            mrc, mout = _run_gate(name, os.path.join(tmp, "tools"))
            chk("MUT3 %s：摘掉包裹必须退回 rc=1 + traceback" % name,
                mrc == 1 and "Traceback" in mout, "rc=%s tb=%s" % (mrc, "Traceback" in mout))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("== run_gate 重试语义自捕（D40） ==")
    ag = load_retry_block()
    if ag is None:
        print("== FLAKE_RETRY FAIL 0/1 ==")
        print("FLAKE_RETRY_SELFTEST-FAIL 0/1 例")
        return 1
    base = _run(ag)
    for k in ("clean_pass", "falsey_is_red", "infra_recovers", "real_bug_no_retry"):
        chk("BASE " + k, base[k])

    class M1:
        """变异体 1：什么都重试 —— 判据 False 也重试到过为止。"""

        @staticmethod
        def _is_infra(exc):
            return True

        @staticmethod
        def run_gate(fn, ctx, name=""):
            last = (False, "EXC")
            for _a in range(3):
                try:
                    ok, d = fn(ctx)
                    if ok:                      # 唯一的收敛条件：判据说「过」
                        return True, d
                    last = (False, d)           # 判据说「红」→ 继续重试（这正是要抓的洞）
                except Exception:
                    pass
            return last

    m1 = _run(M1)
    chk("MUT1 判据红不得被重试洗绿", m1["falsey_is_red"] is False,
        "变异体下 falsey_is_red=%s（应 False：变异体更宽松=有洞）" % m1["falsey_is_red"])

    class M2:
        """变异体 2：重试整个空壳 —— 从不重试，infra 故障直接红。"""

        @staticmethod
        def _is_infra(exc):
            return True

        @staticmethod
        def run_gate(fn, ctx, name=""):
            try:
                ok, d = fn(ctx)
                return bool(ok), d
            except Exception as e:
                return False, "EXC %s: %s" % (type(e).__name__, e)

    m2 = _run(M2)
    chk("MUT2 非空壳：infra 故障必须能重试恢复", m2["infra_recovers"] is False,
        "变异体下 infra_recovers=%s（应 False：变异体不重试=空壳）" % m2["infra_recovers"])

    chk_wiring()

    npass = sum(1 for _n, v in RESULTS if v)
    print("== FLAKE_RETRY %s %d/%d ==" % ("OK" if npass == len(RESULTS) else "FAIL",
                                         npass, len(RESULTS)))
    if npass == len(RESULTS):
        print("FLAKE_RETRY_SELFTEST-OK %d/%d 例" % (npass, len(RESULTS)))
        return 0
    print("FLAKE_RETRY_SELFTEST-FAIL %d/%d 例" % (npass, len(RESULTS)))
    return 1


if __name__ == "__main__":
    sys.exit(main())
