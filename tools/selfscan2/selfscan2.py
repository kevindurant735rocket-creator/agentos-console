#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selfscan2 —— 多候选方案隔离试跑 + 可复算打分 + fail-closed 择优。

来源（star≥1000，真核过）：
  vstorm-co/pydantic-deepagents  ★ 1103  MIT  Python
  https://github.com/vstorm-co/pydantic-deepagents

借鉴的是它的 **Live Run Forking**：
  * 每条分支完全隔离 —— copy-on-write 覆盖层，读穿透父层、写只留本地，
    各自带 budget（`budget_usd`、`test_timeout_s`）；
  * 收敛由**可插拔接受模式**裁决 —— manual / auto / auto_with_fallback(默认) / vote；
  * 打分可复算 —— confidence = quality_spread·0.4 + test_pass_ratio·0.4
    + internal_consistency·0.2，其中 test_pass_ratio 只认真实测试命令的 exit code。

移植到本仓的三条诚实做法：
  1. **隔离**：候选目录先逐份复制到临时副本，验证命令在副本里跑；原树只读，
     候选之间零污染（selftest 用 sha256 证明原树没变）。
  2. **预算**：每条命令独立超时；超时记 rc=124，既不挂死也不算通过。
  3. **fail-closed 默认档**：默认 auto_with_fallback —— 分数过线且领先次名够多
     才采纳，否则明确 NO-WINNER（rc=2）；manual 永远不替人拍板（rc=4，winner 为空）。

打分（权重是常量，没有一项来自模型自述）：
  score = 0.4·pass_ratio + 0.4·lead + 0.2·agreement
    pass_ratio = 通过命令数 / 命令总数                        （只看真实 rc）
    lead       = clamp01(0.5 + (本候选 pass_ratio − 其余候选均值))（相对领先）
    agreement  = 同一命令上与多数派 rc 一致的比例（单候选时=1.0）

用法:
  python3 tools/selfscan2/selfscan2.py --cand A B [--cand C] --cmd "pytest -q" ...
  python3 tools/selfscan2/selfscan2.py --cand A B --cmd "python3 -m py_compile x.py" \
          --accept auto_with_fallback --json work/selfscan2.json
  python3 tools/selfscan2/selfscan2.py --selftest     # 变异体验牙（红→绿）

裁决 rc：
  0 = 采纳了某个候选（winner 非空）
  2 = NO-WINNER（并列/分不够/全挂）—— 不装成功
  3 = 用法/输入错误
  4 = manual 档：已给出打分，等人拍板
"""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

# 钉死的权重（借自 pydantic-deep 的 0.4/0.4/0.2 口径）
W_PASS = 0.4
W_LEAD = 0.4
W_AGREE = 0.2

# 默认档位的过线条件：分数至少这么高，且领先次名至少这么多
MIN_SCORE = 0.70
MIN_MARGIN = 0.05
TIMEOUT_RC = 124  # 与 GNU timeout 的约定一致

ACCEPT_MODES = ("auto", "auto_with_fallback", "vote", "manual")


def parse_cmd_args(items):
    """把 --cmd 收到的字符串列表切成 argv 列表（空串跳过）。

    抽成纯函数是为了能被 selftest 钉住：带 `-m` / `--flag` 的命令必须原样成
    argv 元素，不能被 argparse 或二次 flatten 切碎（本工具真踩过这个坑）。
    """
    return [shlex.split(c) for c in items if c.strip()]


def _clamp01(x):
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def run_checks(cand_dir, cmds, timeout=20.0, sandbox_root=None):
    """在**副本**里逐条跑命令，返回 [{cmd, rc, tail}]。

    隔离：cand_dir 先复制到 sandbox_root 下的临时目录，命令 cwd 指向副本。
    超时：记 rc=124，tail 里明写 timeout，绝不把超时当通过。
    """
    if sandbox_root is None:
        sandbox_root = tempfile.mkdtemp(prefix="selfscan2-")
    work = os.path.join(sandbox_root, os.path.basename(os.path.abspath(cand_dir)) or "cand")
    if os.path.exists(work):
        shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(cand_dir, work, symlinks=True)

    rows = []
    for cmd in cmds:
        try:
            p = subprocess.run(
                cmd,
                cwd=work,
                shell=isinstance(cmd, str),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            tail = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()
            rows.append({"cmd": cmd, "rc": p.returncode, "tail": (tail[-1] if tail else "")[:160]})
        except subprocess.TimeoutExpired:
            rows.append(
                {"cmd": cmd, "rc": TIMEOUT_RC, "tail": "timeout %ss（不当作通过）" % timeout}
            )
        except OSError as e:  # noqa: BLE001
            rows.append({"cmd": cmd, "rc": 127, "tail": "无法执行：%s" % str(e)[:120]})
    return rows


def _pass_ratio(rows):
    if not rows:
        return 0.0
    return sum(1 for r in rows if r["rc"] == 0) / float(len(rows))


def _agreement(rows, all_rows, i):
    """与多数派在每条命令上 rc 一致的比例；单候选时=1.0。"""
    if len(all_rows) <= 1:
        return 1.0
    hits = 0
    for k in range(len(rows)):
        votes = {}
        for other in all_rows:
            if k < len(other):
                votes[other[k]["rc"]] = votes.get(other[k]["rc"], 0) + 1
        top = max(votes.values())
        major = [rc for rc, n in votes.items() if n == top]
        if rows[k]["rc"] in major:
            hits += 1
    return hits / float(len(rows)) if rows else 0.0


def score_candidates(per_cand):
    """纯打分：per_cand = {name: rows}，返回 {name: {score, pass_ratio, lead, agreement}}。

    不碰 IO，好让 selftest 直接喂桩数据。
    """
    names = list(per_cand)
    ratios = {n: _pass_ratio(per_cand[n]) for n in names}
    all_rows = [per_cand[n] for n in names]
    out = {}
    for i, n in enumerate(names):
        others = [ratios[m] for m in names if m != n]
        mean_others = sum(others) / float(len(others)) if others else ratios[n]
        lead = _clamp01(0.5 + (ratios[n] - mean_others))
        agree = _agreement(per_cand[n], all_rows, i)
        s = W_PASS * ratios[n] + W_LEAD * lead + W_AGREE * agree
        out[n] = {
            "score": round(s, 4),
            "pass_ratio": round(ratios[n], 4),
            "lead": round(lead, 4),
            "agreement": round(agree, 4),
        }
    return out


def decide(scores, mode="auto_with_fallback", min_score=MIN_SCORE, min_margin=MIN_MARGIN):
    """接受模式裁决。返回 (winner_or_None, reason)。

    fail-closed 是本模块的本义：拿不准就返回 None，由调用方以 rc=2/4 如实上报。
    """
    if mode not in ACCEPT_MODES:
        return None, "未知接受模式：%s（可用：%s）" % (mode, "/".join(ACCEPT_MODES))
    if not scores:
        return None, "没有候选"
    if mode == "manual":
        return None, "manual 档：只给打分，不替人拍板（等人指定 winner）"

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1]["score"], kv[0]))
    best_name, best = ranked[0]
    runner = ranked[1][1]["score"] if len(ranked) > 1 else 0.0

    if mode == "auto":
        return best_name, "auto 档：最高分 %s=%s（并列按名字序，可能只是随手挑）" % (
            best_name,
            best["score"],
        )
    if mode == "vote":
        # 投票：每条命令的 rc 当一张票，看哪个候选最常与多数派一致
        votes = sorted(scores.items(), key=lambda kv: (-kv[1]["agreement"], kv[0]))
        if len(votes) > 1 and votes[0][1]["agreement"] == votes[1][1]["agreement"]:
            return None, "vote 档：一致度并列（%s），没有多数派" % votes[0][1]["agreement"]
        return votes[0][0], "vote 档：与多数派最一致 %s（%.4f）" % (
            votes[0][0],
            votes[0][1]["agreement"],
        )

    # auto_with_fallback（默认）
    if len(ranked) > 1 and abs(best["score"] - runner) < 1e-9:
        return None, "NO-WINNER：与次名同分（%.4f），默认档不赌" % best["score"]
    if best["score"] < min_score:
        return None, "NO-WINNER：最高分 %.4f < 过线 %.2f" % (best["score"], min_score)
    if best["score"] - runner < min_margin:
        return None, "NO-WINNER：领先次名仅 %.4f < 要求 %.2f" % (
            best["score"] - runner,
            min_margin,
        )
    return best_name, "auto_with_fallback：%s=%s，领先次名 %.4f" % (
        best_name,
        best["score"],
        best["score"] - runner,
    )


# ---------------------------------------------------------------- selftest

def _mk(d, files):
    os.makedirs(d, exist_ok=True)
    for name, body in files.items():
        p = os.path.join(d, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)


def selftest():
    import hashlib

    checks = []

    def ck(name, good, detail):
        checks.append((name, bool(good), str(detail)[:160]))

    tmp = tempfile.mkdtemp(prefix="selfscan2-st-")
    try:
        # 三个候选：good 两条全过，half 一过一挂，bad 两条全挂；
        # probe.py 会写文件，用来证明「写只落副本」。
        spec = {
            "good": ('open("out.txt","w").write("x")\n', "pass\n"),
            "half": ('open("out.txt","w").write("x")\n', "raise SystemExit(1)\n"),
            "bad": ("raise SystemExit(1)\n", "raise SystemExit(1)\n"),
        }
        cands = {}
        for name, (p1, p2) in spec.items():
            d = os.path.join(tmp, name)
            _mk(d, {"probe.py": p1, "probe2.py": p2})
            cands[name] = d
        before = {}
        for name, d in cands.items():
            with open(os.path.join(d, "probe.py"), "rb") as f:
                before[name] = hashlib.sha256(f.read()).hexdigest()

        # 两条命令：probe.py 只有 bad 挂；probe2.py 只有 good 过 -> half 恰好 1/2
        cmds = [[sys.executable, "probe.py"], [sys.executable, "probe2.py"]]
        rows = {n: run_checks(d, cmds, timeout=10, sandbox_root=os.path.join(tmp, "sb_" + n))
                for n, d in cands.items()}
        sc = score_candidates(rows)

        ck("隔离：原树 probe.py sha256 未变（写只落副本）",
           all(hashlib.sha256(open(os.path.join(d, "probe.py"), "rb").read()).hexdigest()
               == before[n] for n, d in cands.items()),
           "sha 前后一致")
        ck("隔离：副本里确实落了 out.txt（命令真跑了）",
           all(os.path.exists(os.path.join(tmp, "sb_" + n, n, "out.txt")) for n in ("good",)),
           "good 副本有 out.txt")
        ck("打分：good 排第一且 > half > bad",
           sc["good"]["score"] > sc["half"]["score"] > sc["bad"]["score"],
           "good=%.4f half=%.4f bad=%.4f" % (sc["good"]["score"], sc["half"]["score"], sc["bad"]["score"]))

        w, why = decide(sc, "auto_with_fallback")
        ck("默认档：分差够大时采纳 good", w == "good", "winner=%s why=%s" % (w, why[:60]))

        wm, whym = decide(sc, "manual")
        ck("manual 档：永不自选（winner 为空，交给 rc=4）", wm is None, "why=%s" % whym[:60])

        # 并列：两个候选分数完全相同 -> NO-WINNER
        tie = {"a": {"score": 0.9, "pass_ratio": 1.0, "lead": 0.75, "agreement": 1.0},
               "b": {"score": 0.9, "pass_ratio": 1.0, "lead": 0.75, "agreement": 1.0}}
        wt, whyt = decide(tie, "auto_with_fallback")
        ck("默认档：同分 NO-WINNER（不赌）", wt is None, "why=%s" % whyt[:60])
        wa, _ = decide(tie, "auto")
        ck("auto 档：同分也给出一个（并明说可能只是随手挑）", wa in ("a", "b"), "winner=%s" % wa)

        # 分不够：全挂 -> NO-WINNER
        dead = {"x": {"score": 0.1, "pass_ratio": 0.0, "lead": 0.5, "agreement": 1.0},
                "y": {"score": 0.05, "pass_ratio": 0.0, "lead": 0.5, "agreement": 0.0}}
        wd, whyd = decide(dead, "auto_with_fallback")
        ck("默认档：全挂时 NO-WINNER（不拿 0 分当赢家）", wd is None, "why=%s" % whyd[:60])

        # 超时：不挂死且记 124
        slow = os.path.join(tmp, "slow")
        _mk(slow, {"sleep.py": "import time; time.sleep(30)\n"})
        r = run_checks(slow, [[sys.executable, "sleep.py"]], timeout=1,
                       sandbox_root=os.path.join(tmp, "sb_slow"))
        ck("预算：超时记 rc=124 且不当作通过", r[0]["rc"] == TIMEOUT_RC, "rc=%d tail=%s" % (r[0]["rc"], r[0]["tail"][:40]))

        # vote 档：一致度并列 -> NO-WINNER
        vt = {"p": {"score": 0.5, "pass_ratio": 1.0, "lead": 0.5, "agreement": 0.5},
              "q": {"score": 0.5, "pass_ratio": 1.0, "lead": 0.5, "agreement": 0.5}}
        wv, whyv = decide(vt, "vote")
        ck("vote 档：一致度并列 -> NO-WINNER", wv is None, "why=%s" % whyv[:60])

        ck("未知接受模式 -> None（fail-closed，不猜）", decide(sc, "bogus")[0] is None, "ok")

        # 回归：带 `-m` / 引号的命令必须原样成 argv，不能被切碎（真踩过的坑）
        got = parse_cmd_args(["python3 -m py_compile selfscan2.py", "  ", 'sh -c "a b"'])
        ck("CLI 解析：短横线参数不被吞、空串跳过",
           got == [["python3", "-m", "py_compile", "selfscan2.py"], ["sh", "-c", "a b"]],
           "got=%s" % got)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    for name, good, detail in checks:
        print(("PASS " if good else "FAIL ") + name + "  -- " + detail)
    bad = [n for n, g, _ in checks if not g]
    if bad:
        print("SELFSCAN2 RED: %s" % " ".join(bad), file=sys.stderr)
        return 2
    print("SELFSCAN2 OK %d/%d" % (len(checks), len(checks)))
    return 0


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="多候选隔离试跑 + 可复算打分 + fail-closed 择优")
    ap.add_argument("--cand", nargs="+", action="append", default=[], help="候选目录（可多次）")
    ap.add_argument(
        "--cmd",
        action="append",
        default=[],
        help='验证命令，一条一个字符串（如 --cmd "python3 -m pytest -q"，可多次）',
    )
    ap.add_argument("--accept", default="auto_with_fallback", choices=list(ACCEPT_MODES))
    ap.add_argument("--min-score", type=float, default=MIN_SCORE)
    ap.add_argument("--min-margin", type=float, default=MIN_MARGIN)
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--json", dest="out", default=None)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    cands = [c for group in args.cand for c in group]
    cmds = parse_cmd_args(args.cmd)
    if not cands or not cmds:
        print("用法：--cand <目录> [...] --cmd <argv> [...]（至少各一个）", file=sys.stderr)
        return 3
    missing = [c for c in cands if not os.path.isdir(c)]
    if missing:
        print("候选目录不存在：%s" % ", ".join(missing), file=sys.stderr)
        return 3

    from hashlib import sha256

    before = {}
    for c in cands:
        h = sha256()
        for base, _dirs, files in os.walk(c):
            for fn in sorted(files):
                p = os.path.join(base, fn)
                try:
                    with open(p, "rb") as f:
                        h.update(f.read())
                except OSError:
                    continue
        before[c] = h.hexdigest()

    tmp = tempfile.mkdtemp(prefix="selfscan2-run-")
    try:
        rows = {c: run_checks(c, cmds, timeout=args.timeout,
                              sandbox_root=os.path.join(tmp, "sb%02d" % i))
                for i, c in enumerate(cands)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    after = {}
    for c in cands:
        h = sha256()
        for base, _dirs, files in os.walk(c):
            for fn in sorted(files):
                p = os.path.join(base, fn)
                try:
                    with open(p, "rb") as f:
                        h.update(f.read())
                except OSError:
                    continue
        after[c] = h.hexdigest()
    clean = all(before[c] == after[c] for c in cands)

    scores = score_candidates(rows)
    winner, reason = decide(scores, args.accept, args.min_score, args.min_margin)

    for c in cands:
        s = scores[c]
        print("候选 %s  score=%.4f pass=%.2f lead=%.2f agree=%.2f  (%s)"
              % (os.path.basename(os.path.abspath(c)), s["score"], s["pass_ratio"],
                 s["lead"], s["agreement"], c))
        for r in rows[c]:
            print("    rc=%-3d %s  %s" % (r["rc"], " ".join(r["cmd"]) if isinstance(r["cmd"], list) else r["cmd"], r["tail"]))
    print("原树未被改动：%s" % ("是" if clean else "否（隔离失效，判红）"))
    print("接受模式 %s → %s" % (args.accept, reason))

    doc = {
        "_doc": "selfscan2：候选隔离试跑的打分与裁决明细（rc 全为真跑结果）。",
        "accept": args.accept,
        "cmds": cmds,
        "scores": scores,
        "detail": rows,
        "isolation_clean": clean,
        "winner": winner,
        "reason": reason,
    }
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
            f.write("\n")

    if not clean:
        print("SELFSCAN2 RED: 隔离失效（候选被就地改动）", file=sys.stderr)
        return 2
    if args.accept == "manual":
        print("SELFSCAN2 MANUAL: 打分已给，等人工指定 winner")
        return 4
    if winner is None:
        print("SELFSCAN2 NO-WINNER: %s" % reason, file=sys.stderr)
        return 2
    print("SELFSCAN2 OK winner=%s" % winner)
    return 0


if __name__ == "__main__":
    sys.exit(main())
