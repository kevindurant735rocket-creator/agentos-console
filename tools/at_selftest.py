#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AT-VERIFIED 正向路径自证门 D85。

为什么必须有这道门（与 D84 的 neg_a11y_human 互补，不是重复）：
neg_a11y_human 证明的是「AX 层不可读时不会撒谎」。但它有一个它自己无法覆盖
的盲区 —— **AT-VERIFIED 这条正向路径从来没被证明过能走通**。

后果是：a11y_human.py 现在在本机必然输出 AT-UNVERIFIED（macOS TCC 辅助功能
权限未授予，实测 AXFocusedApplication 报 -1728、所有窗口 every window 返空）。
一个只能永远输出 UNVERIFIED 的仪器，等用户哪天真去授权了 TCC 也不会翻绿，
而且**没有任何人会知道它坏了** —— 这正是本项目 doctrine 明令要堵的
「量具没量到东西却照样判绿」的镜像形态：量具永久瞎着却没人报警。

所以这里在**进程内** monkeypatch `_osa` 注入桩（不新增、不修改任何真实
env 后门，因此不破坏 D84 写死的安全铁律：真跑时的后门只能返回空，永远无法
伪造 AT-VERIFIED），然后直接调用 a11y_human.probe_at：

  正例 1 条 —— 前台是浏览器 + 窗口标题命中被测 <title> + AX 元素数 > 0
            ⇒ 必须返回 (True, "AT-VERIFIED")；
  反例 5 条 —— System Events 挂掉 / 前台不是浏览器 / 标题没命中 /
            计数为 0（静默空）/ 计数不可解析 ⇒ 必须 AT-UNVERIFIED，
            且理由必须点到对应的真实成因，不许含糊。

一条正例红 = 正向路径坏了（有人把三条件之一改松了）；
一条反例绿 = 判据被掏空。两头都对才 rc=0。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
REPORT = os.path.join(ROOT, "work", "at_selftest.json")
sys.path.insert(0, HERE)

import a11y_human as A  # noqa: E402

TITLE = "AgentOS 交付驾驶舱"


def _stub(app, titles, count, rc_app=0, rc_win=0, rc_cnt=0):
    """造一个假的 _osa：按脚本内容分派 probe_at 的三条查询。"""
    seen = []

    def _fake(script, *a, **kw):
        seen.append(script)
        if "count of UI elements" in script:
            return rc_cnt, count, "" if rc_cnt == 0 else "err-count"
        if "every window" in script:
            return rc_win, titles, "" if rc_win == 0 else "err-win"
        return rc_app, app, "" if rc_app == 0 else "err-app"

    return _fake, seen


def _run(fake, title=TITLE):
    old = A._osa
    A._osa = fake
    try:
        return A.probe_at(title)
    finally:
        A._osa = old


# (tag, 桩参数, 期望 verified, 理由必须命中的关键词, 说明)
CASES = [
    ("P1 正例 浏览器前台+标题命中+AX 元素数 42",
     dict(app="Google Chrome", titles="a, " + TITLE, count="42"),
     True, [], "三条件齐全时必须判 AT-VERIFIED"),
    ("N1 反例 System Events 不可用 rc=1",
     dict(app="", titles="", count="", rc_app=1),
     False, ["System Events"], "AX 查询本身就失败 ⇒ 不得声称已验证"),
    ("N2 反例 前台是 Finder 不是浏览器",
     dict(app="Finder", titles=TITLE, count="42"),
     False, ["不是浏览器"], "读到别的 App 的树 ⇒ 必须退回"),
    ("N3 反例 窗口标题未命中被测 title",
     dict(app="Safari", titles="其他页面, 无关窗口", count="17"),
     False, ["没有"], "未证明这棵树属于被测页面 ⇒ 必须退回"),
    ("N4 反例 AX 元素计数为 0（TCC 静默空）",
     dict(app="Safari", titles=TITLE, count="0"),
     False, ["0 个 AX 元素"], "rc=0 但空树 ⇒ 必须点破静默空"),
    ("N5 反例 计数不可解析为整数",
     dict(app="Safari", titles=TITLE, count="42 个"),
     False, ["不可解析"], "脏输出不得当成通过"),
]


def main(report_path=REPORT):
    # 后门必须关着：本门证明的是「没有后门时的判据本身」，不是后门。
    for k in ("AGENTOS_AT_FAKE_EMPTY", "AGENTOS_AT_FAKE_AX"):
        os.environ.pop(k, None)
    items = []
    for tag, kw, want_ok, must, why in CASES:
        fake, seen = _stub(**kw)
        got_ok, token, reason = _run(fake)
        errs = []
        if bool(got_ok) is not want_ok:
            errs.append("verified 期望 %s 实得 %s" % (want_ok, got_ok))
        want_tok = "AT-VERIFIED" if want_ok else "AT-UNVERIFIED"
        if token != want_tok:
            errs.append("token 期望 %s 实得 %s" % (want_tok, token))
        for word in must:
            if word not in reason:
                errs.append("理由未命中 %r" % word)
        if want_ok and "42 个 AX 元素" not in reason:
            errs.append("正例理由未报出实测计数")
        if not want_ok and "AT-VERIFIED" in reason:
            errs.append("反例理由里出现了 AT-VERIFIED")
        if not seen:
            errs.append("probe_at 一次 osa 查询都没发（判据被短路）")
        items.append({"tag": tag, "ok": not errs, "token": token,
                      "verified": bool(got_ok), "calls": len(seen),
                      "why": why, "reason": reason, "errs": errs})

    red = [i for i in items if not i["ok"]]
    pos = [i for i in items if i["verified"]]
    neg = [i for i in items if not i["verified"]]
    report = {"title": "AT 正向路径自证", "total": len(items),
              "passed": len(items) - len(red), "items": items,
              "backdoor_env": ["AGENTOS_AT_FAKE_EMPTY", "AGENTOS_AT_FAKE_AX"],
              "note": "进程内 monkeypatch a11y_human._osa，未新增任何可伪造 "
                      "AT-VERIFIED 的真实后门"}
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("=" * 70)
    print("AT_SELFTEST  AT-VERIFIED 正向路径自证（桩注入，非真机）")
    print("=" * 70)
    for i in items:
        print("  %s %-50s -> %s" % ("PASS" if i["ok"] else "FAIL",
                                     i["tag"], i["token"]))
        print("       %s" % i["reason"][:150])
        for e in i["errs"]:
            print("       !! %s" % e)
    print("-" * 70)
    print("正例 %d/%d 判 AT-VERIFIED · 反例 %d/%d 正确退回 AT-UNVERIFIED"
          % (len(pos), sum(1 for i in items if i["ok"] and i["verified"]),
             len(neg), sum(1 for i in items if i["ok"] and not i["verified"])))
    print("evidence: %s" % os.path.relpath(report_path, ROOT))
    if red:
        print("AT_SELFTEST FAIL %d/%d" % (len(red), len(items)))
        return 1
    print("AT_SELFTEST OK %d/%d（正例有牙 + 反例不漏）" % (len(items), len(items)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
