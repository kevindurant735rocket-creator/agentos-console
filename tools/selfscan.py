#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selfscan —— 把 GitHub 高星安全扫描器 bandit 部署成本仓的自检量具。

来源（star≥1000，真核过）：
  PyCQA/bandit  ★ 8326  Apache-2.0  Python
  https://github.com/PyCQA/bandit
  它是 Python 生态最成熟的「静态安全缺陷」扫描器：B1xx 硬编码口令、B3xx
  不安全网络/反序列化、B4xx import 级风险、B6xx subprocess 面，规则是社区
  十几年踩坑攒出来的。本仓 48k 行纯 stdlib 代码此前**没有任何静态安全量具**——
  安全只靠 safety_gate 的 9 条行为判据，它量的是「运行时有没有放行」，量不到
  「源码里有没有埋雷」。

为什么是「部署」而不是「蒸馏几条规则」：
  规则表是活的（CVE/攻击面随生态变），抄一份就僵。部署真工具 + 一条基线门，
  才能让每次改码都重新落地同一套社区规则。依赖**不进运行时**：装进
  `.vendor/bandit/`（.gitignore 掉），主链路仍 100% 纯 stdlib。

三道诚实地线（抄本仓既有 doctrine）：
  1. 未部署 ≠ 绿。`.vendor/bandit` 不在时 rc=3 并明说「未部署」，不拿
     「没扫」冒充「没缺陷」。
  2. 量具没量到东西 ≠ 绿。loc==0 或扫描器 rc 崩 → RED。
  3. 只认「新增」。存量 44 条 MEDIUM 是逐条审过的误报（B310 urlopen 前置已有
     scheme 白名单、B105 是门令牌字面量），进基线；基线外新增 MEDIUM 或任意
     HIGH 才判红。

用法:
  python3 tools/selfscan.py --install          # 部署 bandit 到 .vendor/（幂等）
  python3 tools/selfscan.py                    # 跑扫描并按基线判红绿
  python3 tools/selfscan.py --rebuild-baseline # 人工审过误报后重刷基线
  python3 tools/selfscan.py --json work/x.json

裁决：SELFSCAN OK n/n（rc=0）/ SELFSCAN RED …（rc=2）/ SELFSCAN UNDEPLOYED（rc=3）
"""

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
VENDOR = os.path.join(ROOT, ".vendor", "bandit")
BASELINE = os.path.join(ROOT, "work", "selfscan_baseline.json")
DEFAULT_OUT = os.path.join(ROOT, "work", "selfscan.json")
DEFAULT_RAW = os.path.join(ROOT, "work", "selfscan_raw.json")

# 钉死版本：供应链漂移会让「同一份代码昨天绿今天红」，那不是代码的问题。
PINNED = "1.9.4"
# 只判 HIGH/MEDIUM。LOW 里有 341 条（subprocess 调用点是本仓的主要工作方式），
# 判红只会把门训成「红着也没人看」，与 doctrine 反着来。LOW 仍计数并落盘。
BLOCKING = ("HIGH", "MEDIUM")


def _rel(p):
    try:
        return os.path.relpath(p, ROOT).replace(os.sep, "/")
    except ValueError:
        return p


def deployed_version():
    """读 .vendor 里的 dist-info 版本；不在就返回 None。"""
    if not os.path.isdir(VENDOR):
        return None
    for name in os.listdir(VENDOR):
        if name.startswith("bandit-") and name.endswith(".dist-info"):
            return name[len("bandit-") : -len(".dist-info")]
    return None


def install():
    """幂等部署到 .vendor/bandit（--target，不动 site-packages）。"""
    have = deployed_version()
    if have == PINNED:
        print("已部署 bandit %s（%s）" % (have, _rel(VENDOR)))
        return 0
    print("部署 bandit==%s → %s ..." % (PINNED, _rel(VENDOR)))
    rc = subprocess.call(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--target",
            VENDOR,
            "bandit==" + PINNED,
        ]
    )
    if rc != 0:
        print("SELFSCAN INSTALL-FAIL: pip rc=%d" % rc, file=sys.stderr)
        return rc
    print("部署完成：bandit %s" % deployed_version())
    return 0


def run_bandit(targets, raw_out):
    """跑扫描器，返回 (results, metrics, err)。env 里只有 PYTHONPATH 被改。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = VENDOR + (
        (os.pathsep + env["PYTHONPATH"]) if env.get("PYTHONPATH") else ""
    )
    argv = [
        sys.executable,
        "-m",
        "bandit",
        "-r",
    ] + targets + ["-f", "json", "-q", "-o", raw_out]
    p = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True)
    if not os.path.exists(raw_out):
        return None, None, "扫描器无输出（rc=%d）：%s" % (
            p.returncode,
            (p.stderr or "")[:200],
        )
    try:
        with open(raw_out, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:  # noqa: BLE001
        return None, None, "结果不可解析：%s" % str(e)[:160]
    return d.get("results") or [], d.get("metrics") or {}, None


def key_of(r):
    """基线键：文件 + 规则 id。**不带行号** —— 改码会让行号整片漂移，
    带行号的基线第二天就全废，人就会去无条件重刷基线（等于关掉门）。"""
    return "%s|%s" % (_rel(r.get("filename") or ""), r.get("test_id") or "")


def load_baseline():
    if not os.path.exists(BASELINE):
        return None
    try:
        with open(BASELINE, "r", encoding="utf-8") as f:
            b = json.load(f)
        return b.get("allowed") or {}
    except Exception:  # noqa: BLE001
        return None


def build_baseline(results, targets):
    allowed = {}
    for r in results:
        if (r.get("issue_severity") or "").upper() in BLOCKING:
            k = key_of(r)
            allowed[k] = allowed.get(k, 0) + 1
    doc = {
        "_doc": "selfscan 基线：只收人工审过的 MEDIUM 误报，键=文件路径|规则id，值=允许条数。"
        "HIGH 永不入基线（出现即红）。改码后若不新增条数，基线不需要动。",
        "tool": {"name": "bandit", "version": deployed_version()},
        "targets": targets,
        "allowed": allowed,
    }
    os.makedirs(os.path.dirname(BASELINE), exist_ok=True)
    with open(BASELINE, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    return allowed


def verdict(results, metrics, allowed):
    """纯判定：返回 (ok, checks)。不碰 IO，好让门用桩喂变异体。"""
    checks = []
    totals = (metrics or {}).get("_totals") or {}
    loc = int(totals.get("loc") or 0)

    checks.append(("D1-loc>0", loc > 0, "loc=%d" % loc))

    highs = [r for r in results if (r.get("issue_severity") or "").upper() == "HIGH"]
    checks.append(
        (
            "D2-no-HIGH",
            not highs,
            "%d 条 HIGH%s"
            % (
                len(highs),
                ("：" + highs[0].get("test_id", "") + "@" + _rel(highs[0].get("filename") or ""))
                if highs
                else "",
            ),
        )
    )

    seen = {}
    for r in results:
        if (r.get("issue_severity") or "").upper() in BLOCKING:
            seen[key_of(r)] = seen.get(key_of(r), 0) + 1
    fresh = []
    for k, n in sorted(seen.items()):
        cap = int(allowed.get(k, 0))
        if n > cap:
            fresh.append("%s +%d（基线 %d）" % (k, n - cap, cap))
    checks.append(
        (
            "D3-no-new-MEDIUM",
            not fresh,
            ("新增：" + "；".join(fresh[:3]) + ("…" if len(fresh) > 3 else ""))
            if fresh
            else "%d 个基线键全在额度内" % len(seen),
        )
    )
    return all(c[1] for c in checks), checks


def main():
    ap = argparse.ArgumentParser(add_help=True, description="bandit 自检（部署 + 基线门）")
    ap.add_argument("--install", action="store_true", help="部署 bandit 到 .vendor/")
    ap.add_argument(
        "--targets", nargs="*", default=["tools"], help="扫描目标，默认 tools"
    )
    ap.add_argument("--rebuild-baseline", action="store_true", help="重刷基线")
    ap.add_argument("--json", dest="out", default=DEFAULT_OUT, help="判定结果落盘路径")
    ap.add_argument("--raw", dest="raw", default=DEFAULT_RAW, help="扫描器原始 JSON")
    args = ap.parse_args()

    if args.install:
        return install()

    ver = deployed_version()
    if ver is None:
        print(
            "SELFSCAN UNDEPLOYED: %s 不存在。先跑 python3 tools/selfscan.py --install"
            % _rel(VENDOR),
            file=sys.stderr,
        )
        return 3
    if ver != PINNED:
        print(
            "SELFSCAN RED: 版本漂移 期望 %s 实得 %s（供应链变了，先人工确认再改 PINNED）"
            % (PINNED, ver),
            file=sys.stderr,
        )
        return 2

    os.makedirs(os.path.dirname(args.raw), exist_ok=True)
    results, metrics, err = run_bandit(args.targets, args.raw)
    if err:
        print("SELFSCAN RED: %s" % err, file=sys.stderr)
        return 2

    if args.rebuild_baseline:
        allowed = build_baseline(results, args.targets)
        print("基线已重刷：%d 个键 → %s" % (len(allowed), _rel(BASELINE)))
        return 0

    allowed = load_baseline()
    if allowed is None:
        print(
            "SELFSCAN RED: 基线缺失或不可解析（%s）。先人工审误报再 --rebuild-baseline"
            % _rel(BASELINE),
            file=sys.stderr,
        )
        return 2

    ok, checks = verdict(results, metrics, allowed)
    for name, good, detail in checks:
        print(("PASS " if good else "FAIL ") + name + "  -- " + str(detail)[:160])
    totals = (metrics or {}).get("_totals") or {}
    print(
        "统计：loc=%s HIGH=%s MEDIUM=%s LOW=%s"
        % (
            totals.get("loc", 0),
            totals.get("SEVERITY.HIGH", 0),
            totals.get("SEVERITY.MEDIUM", 0),
            totals.get("SEVERITY.LOW", 0),
        )
    )

    doc = {
        "tool": {"name": "bandit", "version": ver},
        "targets": args.targets,
        "ok": bool(ok),
        "checks": [{"name": n, "ok": g, "detail": d} for n, g, d in checks],
        "totals": totals,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")

    if ok:
        print("SELFSCAN OK %d/%d" % (len(checks), len(checks)))
        return 0
    bad = [n for n, g, _ in checks if not g]
    print("SELFSCAN RED: %s" % " ".join(bad), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
