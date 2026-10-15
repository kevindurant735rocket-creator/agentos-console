#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""snap_live.py — 把「服务在线才有」的三个闭环面板快照进 data.json。

为什么要有这个工具（2026-10-03 实测缺陷，不是推演）：
  交付物是一份 `index.html` + `data.json`，用户双击 / 归档 / 传阅的就是它。
  但自我进化闭环的三个面板 —— 决策面（/__act/recommend）、晋升台（/__promote）、
  蒸馏台（/__distill）—— 全部走 fetch('/__xxx')，**只在服务在线时存在**。
  实测 file:// 直开 index.html：三处一起塌成「建议不可用（需服务在线）：Failed to fetch」。
  也就是说，交付物里最重要的三个闭环环节在交付状态下是死的，而机械门全绿
  （view_dod 只验「屏里有真内容」，那三处恰恰也有非空文本 → 判据漏了）。

修法（不造假绿）：构建期把三份**引擎真判定**的结果快照进 data.json.live_snapshot，
  直开时用它兜底，并在面板标题上**显式标注「快照 · 时间 · 非实时」**。
  快照里的 ok=False 照原样带走 —— 引擎判红就是红，绝不因为"离线兜底"洗绿。

判定权仍在引擎（promote 走 company.sh promote --json，distill 走 distill_ledger.verify），
  本工具只做只读转发 + 一次原子写，与 serve.py 的 /__promote 同源同口径。

CLI: snap_live.py [--check]   rc=0 快照已就位且新鲜 / 3 快照缺失或过期
"""
import argparse
import datetime
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
DATA = os.path.join(ROOT, "data.json")
SERVE = os.path.join(HERE, "serve.py")

# 快照比它自己诚实：超过这个年龄，前端标题必须打「已过期」。
STALE_AFTER_S = 3600


def atomic_write_json(path, obj):
    """同目录临时文件 + os.replace。
    与 build.py 同一理由：直接 open(path,'w') 会先截断到 0，
    常驻 8790 实例 / 真浏览器门并发读时正好读到半截 JSON → 门随机红。"""
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".snap-", suffix=".swap")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_serve():
    """按路径加载 serve.py（不靠 sys.path 猴补丁，与 serve._load_ledger 同口径）。
    serve.py 有 __main__ 护栏，import 不会起服务。"""
    spec = importlib.util.spec_from_file_location("_agentos_serve", SERVE)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _verdict_of(data):
    gv = data.get("global_verdict")
    if isinstance(gv, dict):
        return gv.get("verdict")
    return gv


def build_snapshot(data):
    """三份闭环面板的引擎真判定 + 捕获时刻。任一面 fail-closed 就照实带 ok=False。"""
    sv = load_serve()
    if sv is None:
        return None
    try:
        recommend = {"ok": True, "verdict": _verdict_of(data),
                     "recommend": sv.recommend(data)}
    except Exception as e:
        recommend = {"ok": False, "verdict": None, "recommend": [],
                     "error": ("recommend error: %s" % e)[:120]}
    try:
        promote = sv._promote_compute(40)
    except Exception as e:
        promote = {"ok": False, "error": ("promote error: %s" % e)[:120], "rows": [],
                   "total": 0, "n_ready": 0, "n_hold": 0, "recurrence": None}
    try:
        distill = sv._distill_compute(12)
    except Exception as e:
        distill = {"ok": False, "error": ("distill error: %s" % e)[:120], "rows": [],
                   "n_rows": 0, "n_both": 0, "target": None, "verify_ok": False}
    return {
        "captured_at": datetime.datetime.now().astimezone().isoformat(),
        "stale_after_s": STALE_AFTER_S,
        "source": "构建期快照 · 非实时 · 判定权在引擎",
        "recommend": recommend,
        "promote": promote,
        "distill": distill,
    }


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只判快照在位且新鲜，不写盘")
    a = ap.parse_args(argv)

    try:
        with open(DATA, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print("SNAP_LIVE_FAIL data.json 读不到: %s" % e)
        return 3

    if a.check:
        s = data.get("live_snapshot")
        if not isinstance(s, dict):
            print("SNAP_LIVE_FAIL 快照缺失（直开 index.html 时闭环三面会塌）")
            return 3
        try:
            age = (datetime.datetime.now().astimezone()
                   - datetime.datetime.fromisoformat(s["captured_at"])).total_seconds()
        except Exception:
            print("SNAP_LIVE_FAIL captured_at 不可解析")
            return 3
        if age > float(s.get("stale_after_s") or STALE_AFTER_S):
            print("SNAP_LIVE_FAIL 快照已过期 %.0fs > %ss" % (age, s.get("stale_after_s")))
            return 3
        print("SNAP_LIVE-OK age=%.0fs captured_at=%s" % (age, s["captured_at"]))
        return 0

    snap = build_snapshot(data)
    if snap is None:
        print("SNAP_LIVE_FAIL serve.py 不可加载")
        return 3
    # 快照是 data.json 的一部分：整体原子重写，不做增量补丁。
    data["live_snapshot"] = snap
    atomic_write_json(DATA, data)
    p, d, r = snap["promote"], snap["distill"], snap["recommend"]
    print("SNAP_LIVE-OK captured_at=%s recommend=%d promote_ok=%s total=%s "
          "distill_ok=%s rows=%s verify_ok=%s" % (
              snap["captured_at"], len(r.get("recommend") or []),
              p.get("ok"), p.get("total"), d.get("ok"), d.get("n_rows"),
              d.get("verify_ok")))
    # 快照里出现 ok=False 不是本工具的失败，是引擎判红 —— 但必须让人看见。
    if not (p.get("ok") and d.get("ok")):
        print("SNAP_LIVE_WARN 引擎判红已如实带走: promote=%s distill=%s"
              % (p.get("error"), d.get("error")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
