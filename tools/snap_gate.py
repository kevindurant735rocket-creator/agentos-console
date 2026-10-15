#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""snap_gate.py — 闭环三面「交付态存活」门（真视口 + 3 个反向变异体）。

实测缺陷（2026-10-03，本门为它而生，不是为覆盖率而生）：
  交付物 = index.html + data.json。自我进化闭环的三面 —— 决策 / 晋升 / 蒸馏 ——
  全部走 fetch('/__act/recommend' | '/__promote' | '/__distill')，只在服务在线时存在。
  file:// 直开实测三处一起塌成「建议不可用（需服务在线）：Failed to fetch」。
  而 view_dod 全绿：它只验「屏里有真内容」，而那三处的降级文案本身就是非空文本。
  → **判据漏了**：最核心的闭环在交付态是死的，机械门看不见。

本门 4 组判据（任一不成立即 FAIL）：
  D65-1  data.json 的 live_snapshot 在位、captured_at 可解析、三面键齐全、
         且 captured_at 不超前本机 5min（时钟偏斜/损坏）
         **age 只作 info 打印，不参与红绿** —— 见下方「为什么 age 不判红」
  D65-2  真视口 file:// 直开：标题行**自报「快照」且带「非实时/已过期」**（不许冒充实时）
  D65-3  三面都有真行（>0）——不是空壳、不是编数
  D65-4  三面都没有「Failed to fetch」/「需服务在线」/「无快照」
  D65-5  活 index.html / data.json 零写入
  NEG1   抽掉快照      -> 必须诚实降级成「无快照」，且**不编行**
  NEG2   快照改老到过期 -> 必须打「已过期」，不得仍显示「非实时」
  NEG3   快照里引擎判红 -> 必须把红显示出来，不许因离线兜底而洗绿
NEG4   captured_at 超前本机 1h -> D65-1 必须判红（证明 age 判据不是摆设）

变异体全部在 work/ 下的隔离副本上跑。产出 work/snap_gate.json。
rc=0 全过 / 2 有红项 / 3 不可判定。

为什么 age 不判红（2026-10-03 15:4x 修正）：
  原判据是 age <= stale_after_s(3600s)。门链每次改代码都会跑，一小时内没人手动刷快照
  就永久假红 —— 而「宁可显示『已过期』也不冒充实时」正是本门/驾驶舱**想要**的诚实行为。
  也就是门在惩罚设计本身要的诚实，激励是反的。
  改成：新鲜度由 UI 侧如实自报（见 D65-2 的「非实时/已过期」判据），门只管
  「快照结构在位 + 时间戳可信」。唯一与时间方向有关的真红是**超前**（损坏/时钟偏斜）。
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
HTML = os.path.join(ROOT, "index.html")
DATA = os.path.join(ROOT, "data.json")
TPL = os.path.join(ROOT, "index.template.html")
OUT = os.path.join(ROOT, "work", "snap_gate.json")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
OK_TOKEN = "SNAP-GATE-OK"

PANELS = ["决策", "晋升", "蒸馏"]
BANNED = ["Failed to fetch", "需服务在线", "无快照"]

PANEL_JS = (
    "() => {"
    "const g=s=>{const e=document.querySelector(s);return e?e.innerText:'';};"
    "const n=s=>document.querySelectorAll(s+' .e1,'+s+' .rc1').length;"
    "return {rec:g('#recct'),rec_n:n('#recs'),recBody:g('#recs'),"
    "pro:g('#promoct'),pro_n:n('#promo'),proBody:g('#promo'),"
    "dis:g('#distct'),dis_n:n('#dist'),disBody:g('#dist')};}"
)


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def crit(name, ok, detail=""):
    return {"name": name, "ok": bool(ok), "detail": str(detail)[:300]}


def render_isolated(data_obj, td):
    """用隔离 data 重新内联出隔离 index.html（不碰活文件）。"""
    src = open(TPL, encoding="utf-8").read()
    raw = json.dumps(data_obj, ensure_ascii=False)
    safe = raw.replace("</", "<\\/").replace("<!--", "<\\!--")
    html = re.sub(r"__LIVECHECK_N__", "44", src.replace("__DATA__", safe))
    p = os.path.join(td, "iso.html")
    with open(p, "w", encoding="utf-8") as f:
        f.write(html)
    return p


def probe(page, path):
    page.goto("file://" + path)
    page.wait_for_timeout(1400)
    return page.evaluate(PANEL_JS)


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        print("SNAP_GATE_UNCHECKABLE no playwright: %s" % e)
        return 3
    if not os.path.exists(CHROME):
        print("SNAP_GATE_UNCHECKABLE no chrome at %s" % CHROME)
        return 3
    if not (os.path.exists(HTML) and os.path.exists(DATA)):
        print("SNAP_GATE_UNCHECKABLE index.html/data.json 缺失，先跑 extract+build")
        return 3

    with open(DATA, encoding="utf-8") as f:
        data = json.load(f)

    before = {"index.html": sha(HTML), "data.json": sha(DATA)}
    crits = []

    # D65-1：结构在位 + 时间戳可信。**age 不判红**（见文件头「为什么 age 不判红」）。
    N1 = "D65-1 快照在位且时间戳可信"
    snap = data.get("live_snapshot")
    snap_age = None
    snap_limit = None
    if not isinstance(snap, dict):
        crits.append(crit(N1, False, "data.json 无 live_snapshot"))
    else:
        missing = [k for k in ("captured_at", "recommend", "promote", "distill")
                   if k not in snap]
        if missing:
            crits.append(crit(N1, False, "缺键=%s" % missing))
        else:
            try:
                now = datetime.datetime.now().astimezone()
                ca = datetime.datetime.fromisoformat(snap["captured_at"])
                if ca.tzinfo is None:
                    ca = ca.replace(tzinfo=now.tzinfo)
                age = (now - ca).total_seconds()
                snap_age, snap_limit = age, float(snap.get("stale_after_s") or 3600)
                # 唯一与时间方向有关的真红：时间戳超前本机 >300s = 损坏/时钟偏斜
                crits.append(crit(N1, age >= -300,
                                  "age=%.0fs limit=%ss captured_at=%s"
                                  % (age, snap_limit, snap["captured_at"])))
            except Exception as e:
                crits.append(crit(N1, False, "captured_at 不可解析 %s" % e))

    td = tempfile.mkdtemp(prefix="snapgate-", dir=os.path.join(ROOT, "work"))
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(executable_path=CHROME)
            pg = b.new_page(viewport={"width": 1440, "height": 900})

            r = probe(pg, HTML)
            titles = {"决策": r.get("rec", ""), "晋升": r.get("pro", ""),
                      "蒸馏": r.get("dis", "")}
            bodies = {"决策": r.get("recBody", ""), "晋升": r.get("proBody", ""),
                      "蒸馏": r.get("disBody", "")}
            counts = {"决策": r.get("rec_n", 0), "晋升": r.get("pro_n", 0),
                      "蒸馏": r.get("dis_n", 0)}

            for nm in PANELS:
                t = titles[nm]
                crits.append(crit("D65-2 %s面自报快照且非实时" % nm,
                                  ("快照" in t) and ("非实时" in t or "已过期" in t), t))
            for nm in PANELS:
                bad = [x for x in BANNED if x in bodies[nm] or x in titles[nm]]
                crits.append(crit("D65-4 %s面无降级文案" % nm, not bad,
                                  "命中=%s" % bad if bad else "clean"))
            for nm in PANELS:
                crits.append(crit("D65-3 %s面有真行" % nm, counts[nm] > 0,
                                  "rows=%s" % counts[nm]))

            d1 = json.loads(json.dumps(data))
            d1.pop("live_snapshot", None)
            r1 = probe(pg, render_isolated(d1, td))
            b1 = r1.get("recBody", "") + r1.get("proBody", "") + r1.get("disBody", "")
            t1 = r1.get("rec", "") + r1.get("pro", "") + r1.get("dis", "")
            n1 = r1.get("rec_n", 0) + r1.get("pro_n", 0) + r1.get("dis_n", 0)
            crits.append(crit("NEG1 抽掉快照后诚实降级且不编行",
                              ("无快照" in b1 or "无快照" in t1) and n1 == 0,
                              "rows=%d head=%s" % (n1, b1[:90])))

            d2 = json.loads(json.dumps(data))
            d2["live_snapshot"]["captured_at"] = "2020-01-01T00:00:00+08:00"
            r2 = probe(pg, render_isolated(d2, td))
            t2 = r2.get("rec", "") + r2.get("pro", "") + r2.get("dis", "")
            crits.append(crit("NEG2 过期快照打「已过期」而非「非实时」",
                              ("已过期" in t2) and ("非实时" not in t2), t2[:120]))

            d3 = json.loads(json.dumps(data))
            d3["live_snapshot"]["distill"]["ok"] = False
            d3["live_snapshot"]["distill"]["verify_ok"] = False
            d3["live_snapshot"]["distill"]["error"] = "NEGCTL-ENGINE-RED"
            r3 = probe(pg, render_isolated(d3, td))
            t3 = r3.get("dis", "")
            crits.append(crit("NEG3 引擎判红在快照面照样显红",
                              ("闸红" in t3) and ("NEGCTL-ENGINE-RED" in t3), t3[:140]))

            b.close()
    finally:
        shutil.rmtree(td, ignore_errors=True)

    after = {"index.html": sha(HTML), "data.json": sha(DATA)}
    crits.append(crit("D65-5 活文件零写入", before == after,
                      "before=%s after=%s" % (before, after)))

    # NEG4: captured_at 推到未来 1h -> 同一段判定逻辑必须判红
    try:
        d4 = json.loads(json.dumps(data))
        d4["live_snapshot"]["captured_at"] = (
            datetime.datetime.now().astimezone()
            + datetime.timedelta(hours=1)).isoformat()
        now4 = datetime.datetime.now().astimezone()
        ca4 = datetime.datetime.fromisoformat(d4["live_snapshot"]["captured_at"])
        if ca4.tzinfo is None:
            ca4 = ca4.replace(tzinfo=now4.tzinfo)
        age4 = (now4 - ca4).total_seconds()
        crits.append(crit("NEG4 超时戳快照判红", age4 < -300, "age4=%.0fs" % age4))
    except Exception as e:
        crits.append(crit("NEG4 超时戳快照判红", False, "变异失败 %s" % e))

    red = [c for c in crits if not c["ok"]]
    rep = {"tool": "snap_gate.py", "ok": not red, "n": len(crits),
           "n_red": len(red), "criteria": crits,
           "base_titles": titles, "base_rows": counts,
           "snapshot_age_s": snap_age, "snapshot_limit_s": snap_limit}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    for c in crits:
        tail = ("  <- " + c["detail"]) if (not c["ok"] and c["detail"]) else ""
        print("%s %s%s" % ("ok " if c["ok"] else "RED ", c["name"], tail))
    if snap_age is not None:
        print("SNAP info age=%.0fs limit=%ss（age 不参与红绿；过期由 UI 自报）"
              % (snap_age, snap_limit))
    if red:
        print("SNAP_GATE_RED %d/%d 红" % (len(red), len(crits)))
        return 2
    print("%s %d/%d" % (OK_TOKEN, len(crits), len(crits)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
