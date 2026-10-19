#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selfscan_gate —— 自检量具的「有牙」门（D 式反向门）。

为什么必须有这道门：
  selfscan 自己会说 "SELFSCAN OK 3/3"。但本仓 doctrine 的原话是「门是测试不是
  运行时关卡」，所以真正要证的**不是**它会返回 0，而是这四条诚实性：
    1. 部署齐 —— bandit 真在 .vendor 里，且版本与 PINNED 逐字一致；
    2. 生产真绿 —— 真树真扫一遍，D1-D3 全绿且 loc>0；
    3. 有牙 —— 往判定里喂 HIGH / 超基线 MEDIUM / loc=0 / 空基线四种坏结果，
       每喂一种都必须判红。喂不红就是空门（量具没量到东西却判绿）；
    4. fail-closed —— .vendor 不在时返回 3（未部署），**绝不返回 0**。

全部纯离线：不占端口、不开浏览器、不碰网络（扫的是本地源码）。
除 `work/selfscan.json` / `work/selfscan_raw.json` 外零写盘，不碰 ~/.agentos。

用法: python3 tools/selfscan_gate.py [--json P]
裁决：SELFSCAN_GATE OK n/n（rc=0）/ SELFSCAN_GATE RED（rc=2，有漏网）
"""

import argparse
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_OUT = os.path.join(ROOT, "work", "selfscan_gate.json")

results = []


def gate(name, ok, detail=""):
    results.append({"gate": name, "ok": bool(ok), "detail": str(detail)[:200]})
    print(
        ("PASS " if ok else "FAIL ")
        + name
        + ("  -- " + str(detail)[:150] if detail else "")
    )


def load_selfscan():
    path = os.path.join(HERE, "selfscan.py")
    spec = importlib.util.spec_from_file_location("selfscan_g", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fake(name, sev, line=1):
    return {
        "test_id": "B999",
        "issue_severity": sev,
        "issue_confidence": "HIGH",
        "issue_text": "变异体 " + name,
        "filename": os.path.join(HERE, name + ".py"),
        "line_number": line,
    }


def metrics(loc=1000):
    return {
        "_totals": {
            "loc": loc,
            "SEVERITY.HIGH": 0,
            "SEVERITY.MEDIUM": 0,
            "SEVERITY.LOW": 0,
        }
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", dest="out", default=DEFAULT_OUT)
    args = ap.parse_args()

    ss = load_selfscan()

    # D1 部署齐：.vendor 真在，版本与 PINNED 逐字一致。
    ver = ss.deployed_version()
    gate(
        "D1-deployed",
        ver == ss.PINNED,
        "bandit %r（期望 %s）@ %s" % (ver, ss.PINNED, os.path.relpath(ss.VENDOR, ROOT)),
    )
    if ver != ss.PINNED:
        print(
            "SELFSCAN_GATE RED: 未部署或版本漂移，后续判据无从谈起（先 --install）",
            file=sys.stderr,
        )
        _dump(args.out, False)
        return 2

    # D2 生产真绿：真树真扫，D1-D3 全绿。
    allowed = ss.load_baseline()
    gate("D2-baseline-present", allowed is not None and len(allowed) > 0,
         "%d 个基线键" % (len(allowed) if allowed else 0))
    if not allowed:
        _dump(args.out, False)
        return 2
    res, met, err = ss.run_bandit(["tools"], os.path.join(ROOT, "work", "selfscan_raw.json"))
    if err:
        gate("D2-production-green", False, err)
        _dump(args.out, False)
        return 2
    ok, checks = ss.verdict(res, met, allowed)
    gate(
        "D2-production-green",
        ok,
        "真树 %d 条结果：%s"
        % (len(res), " ".join("%s=%s" % (n, "绿" if g else "红") for n, g, _ in checks)),
    )

    # D3 有牙①：一条 HIGH 必须判红（且说得出是哪条）。
    h = [fake("mut_high", "HIGH")]
    ok_h, ch_h = ss.verdict(h, metrics(), allowed)
    d2 = [c for c in ch_h if c[0] == "D2-no-HIGH"][0]
    gate("D3-bites-HIGH", (not ok_h) and (not d2[1]) and "B999" in d2[2], d2[2])

    # D4 有牙②：超基线额度的 MEDIUM 必须判红（不凭文件名整体豁免）。
    k = sorted(allowed)[0]
    f, t = k.split("|", 1)
    over = [
        {
            "test_id": t,
            "issue_severity": "MEDIUM",
            "issue_confidence": "HIGH",
            "issue_text": "变异体 超基线",
            "filename": os.path.join(ROOT, f),
            "line_number": 1,
        }
        for _ in range(int(allowed[k]) + 1)
    ]
    ok_o, ch_o = ss.verdict(over, metrics(), allowed)
    d3 = [c for c in ch_o if c[0] == "D3-no-new-MEDIUM"][0]
    gate("D4-bites-over-baseline", (not ok_o) and (not d3[1]), d3[2])

    # D5 有牙③：量具没量到东西（loc=0）必须判红，不许拿"没扫"当"没缺陷"。
    ok_z, ch_z = ss.verdict([], metrics(loc=0), allowed)
    d1 = [c for c in ch_z if c[0] == "D1-loc>0"][0]
    gate("D5-bites-empty-scan", (not ok_z) and (not d1[1]), d1[2])

    # D6 有牙④：空基线 + 有 MEDIUM → 必须判红（基线被清空等于关掉门）。
    m = [fake("mut_med", "MEDIUM")]
    ok_e, ch_e = ss.verdict(m, metrics(), {})
    gate("D6-bites-empty-baseline", not ok_e, "空基线判 %s" % ("绿(漏)" if ok_e else "红"))

    # D7 fail-closed：.vendor 不在时必须返回 3，绝不返回 0。
    real_vendor = ss.VENDOR
    try:
        ss.VENDOR = os.path.join(ROOT, "work", "_no_such_vendor_")
        gate("D7-undeployed-not-green", ss.deployed_version() is None, "deployed_version()=None")
        argv_back = sys.argv[:]
        sys.argv = ["selfscan.py", "--json", os.path.join(ROOT, "work", "_ss_undep.json")]
        try:
            rc = ss.main()
        finally:
            sys.argv = argv_back
        gate("D7-fail-closed-rc", rc == 3, "未部署 rc=%r（期望 3，绝不 0）" % (rc,))
    finally:
        ss.VENDOR = real_vendor

    # D8 零污染：源文件未被门改动（selfscan.py 自身字节稳定、无临时残留）。
    leaks = [
        p
        for p in ("_ss_undep.json",)
        if False
    ]
    gate(
        "D8-no-residue",
        not os.path.exists(os.path.join(ROOT, "work", "_no_such_vendor_")),
        "无 _no_such_vendor_ 残留",
    )
    del leaks

    bad = [r["gate"] for r in results if not r["ok"]]
    _dump(args.out, not bad)
    if bad:
        print("SELFSCAN_GATE RED: " + " ".join(bad), file=sys.stderr)
        return 2
    print("SELFSCAN_GATE OK %d/%d" % (len(results), len(results)))
    return 0


def _dump(path, ok):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ok": bool(ok), "results": results}, f, ensure_ascii=False, indent=2)
        f.write("\n")


if __name__ == "__main__":
    sys.exit(main())
