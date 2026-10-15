#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""能力地图门：四桶分账的**诚实性**验收。

为什么必须单独一门：这一面是整个驾驶舱里唯一会主动显示「我有多少能力」的面，
也是最容易假绿的一面。四桶的判据全是负向的（不能把自报当验过、不能把无名
并进没跑过、不能在对不齐时凑数），而这些判据**从屏幕上看不出来** —— 一份
假账和一份真账长得一样，都是四张卡片几个整数。所以必须用夹具打进去。

本门全部用例把账本重定向到 work/ 下的临时目录（monkeypatch serve._cap_dir），
**绝不碰生产账本**，收尾再核一次生产账本 sha256 未变。

产出 work/capmap_gate.json，rc=0 全过 / 2 有红项。
"""
import ast
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import importlib.util as _ilu

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
_sp = _ilu.spec_from_file_location("_d95_serve", os.path.join(HERE, "serve.py"))
serve = _ilu.module_from_spec(_sp)
_sp.loader.exec_module(serve)

COMPANY = serve.COMPANY
PROD_DIR = os.path.join(COMPANY, "COMPANY_MEMORY", "capabilities")
PROD_FILES = ("ledger.jsonl", "regression_verdicts.jsonl",
              "autopilot_verdicts.jsonl", "events.jsonl")
BUCKETS = ("ab-verified", "ab-unverifiable", "no-verdict", "anonymous")

GATES = []


def gate(name):
    def deco(fn):
        GATES.append((name, fn))
        return fn
    return deco


def prod_hash():
    out = {}
    for fn in PROD_FILES:
        p = os.path.join(PROD_DIR, fn)
        try:
            with open(p, "rb") as f:
                out[fn] = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            out[fn] = "ABSENT"
    return out


def fresh(tag):
    return tempfile.mkdtemp(prefix="capmap_gate_%s_" % tag,
                            dir=os.path.join(ROOT, "work"))


def wl(d, name, rows):
    with io.open(os.path.join(d, name), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def wl_raw(d, name, lines):
    with io.open(os.path.join(d, name), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def with_cap_dir(d, fn):
    """把 serve._cap_dir 临时指到夹具目录，跑完还原（含异常路径）。"""
    old = serve._cap_dir
    serve._cap_dir = lambda: d
    try:
        return fn()
    finally:
        serve._cap_dir = old


def counts(j):
    return {"ab-verified": j["n_verified"],
            "ab-unverifiable": j["n_unverifiable"],
            "no-verdict": j["n_no_verdict"],
            "anonymous": j["n_anonymous"]}


def mk(cid_, status, **kw):
    r = {"capability_id": cid_, "status": status}
    r.update(kw)
    return r


def vv(cid_, verdict, ts="2026-10-01T00:00:00Z", **kw):
    r = {"id": cid_, "verdict": verdict, "ts": ts}
    r.update(kw)
    return r


@gate("正例：四类账各一条 -> 四桶互斥且穷尽（合计 == 账本总数）且每条落在正确桶")
def g_positive(_):
    d = fresh("pos")
    wl(d, "ledger.jsonl", [
        mk("cap-v", "PROMOTED", problem="A/B 验过"),
        mk("cap-u", "PROMOTED", problem="判据不足"),
        mk("cap-n", "EXPERIMENT", problem="没跑 A/B"),
        {"status": "PROMOTED", "problem": None},
    ])
    wl(d, "regression_verdicts.jsonl", [
        vv("cap-v", "HOLD-AB"), vv("cap-u", "UNVERIFIABLE")])
    wl(d, "autopilot_verdicts.jsonl", [])
    wl(d, "events.jsonl", [])
    os.makedirs(os.path.join(d, "ab_snapshots"), exist_ok=True)
    j = with_cap_dir(d, lambda: serve.capmap_payload())
    shutil.rmtree(d, ignore_errors=True)
    if not j.get("ok"):
        return False, "夹具应当可读却 ok=false: %s" % j.get("error")
    c = counts(j)
    bucket_of = dict((r["id"], r["proof"]) for r in j["rows"])
    want = {"cap-v": "ab-verified", "cap-u": "ab-unverifiable",
            "cap-n": "no-verdict"}
    wrong = dict((k, x) for k, x in bucket_of.items()
                 if k in want and x != want[k])
    anon = [r for r in j["rows"] if r["proof"] == "anonymous"]
    s = sum(c.values())
    ok = (c == {"ab-verified": 1, "ab-unverifiable": 1,
                "no-verdict": 1, "anonymous": 1}
          and s == j["n_ledger"] == 4 and not wrong and len(anon) == 1)
    return ok, "counts=%s sum=%d ledger=%d 错桶=%s anon_id=%r" % (
        c, s, j["n_ledger"], wrong or "无", anon[0]["id"] if anon else "?")


@gate("反向：3 条 status=PROMOTED 但一条 A/B 裁决都没有 -> n_verified 必须为 0")
def g_promoted_is_not_verified(_):
    """这条是整个门最承重的一条。真实盘上 PROMOTED 有 26 条、A/B 裁决 PASS=0。
    若判定写成 status == "PROMOTED" 而不是看独立裁决，屏上就会出现「已验过 26」，
    把 26 条未验证的账一次包装成 26 份能力 —— 而那个数字本身看不出任何破绽，
    只会被当成产品在变强。"""
    d = fresh("promoted")
    wl(d, "ledger.jsonl", [mk("p1", "PROMOTED"), mk("p2", "PROMOTED"),
                           mk("p3", "PROMOTED")])
    wl(d, "regression_verdicts.jsonl", [])
    wl(d, "autopilot_verdicts.jsonl", [])
    wl(d, "events.jsonl", [])
    j = with_cap_dir(d, lambda: serve.capmap_payload())
    shutil.rmtree(d, ignore_errors=True)
    ok = (j["n_verified"] == 0 and j["n_no_verdict"] == 3
          and all(r["proof"] == "no-verdict" for r in j["rows"]))
    return ok, ("PROMOTED=3 n_verified=%d n_no_verdict=%d -> %s"
                % (j["n_verified"], j["n_no_verdict"],
                   "未把自报当验过" if ok else "★ 假绿：自报被当成了验过"))


@gate("反向：6 条连 capability_id 都没有 -> 必须全落 anonymous，不得并进 no-verdict")
def g_anonymous_not_merged(_):
    d = fresh("anon")
    wl(d, "ledger.jsonl", [{"status": "PROMOTED", "problem": None}
                           for _i in range(6)])
    wl(d, "regression_verdicts.jsonl", [])
    wl(d, "autopilot_verdicts.jsonl", [])
    wl(d, "events.jsonl", [])
    j = with_cap_dir(d, lambda: serve.capmap_payload())
    shutil.rmtree(d, ignore_errors=True)
    c = counts(j)
    ok = (c["anonymous"] == 6 and c["no-verdict"] == 0 and c["ab-verified"] == 0)
    return ok, ("counts=%s -> %s" % (c, "无名账自己一桶" if ok
                                     else "★ 无名账被并进 no-verdict/已验过"))


@gate("反向：UNVERIFIABLE / PASS / FAIL 都不进「已验过」，只有 HOLD-AB/HOLD-EXIST 进")
def g_unverifiable_and_pass(_):
    d = fresh("unver")
    wl(d, "ledger.jsonl", [mk("u1", "PROMOTED"), mk("u2", "PROMOTED"),
                           mk("u3", "PROMOTED")])
    wl(d, "regression_verdicts.jsonl", [
        vv("u1", "UNVERIFIABLE"), vv("u2", "PASS"), vv("u3", "FAIL")])
    wl(d, "autopilot_verdicts.jsonl", [])
    wl(d, "events.jsonl", [])
    j = with_cap_dir(d, lambda: serve.capmap_payload())
    shutil.rmtree(d, ignore_errors=True)
    kinds = dict((r["id"], r["proof"]) for r in j["rows"])
    ok = (j["n_verified"] == 0 and j["n_unverifiable"] == 3
          and all(x == "ab-unverifiable" for x in kinds.values()))
    return ok, ("kinds=%s n_verified=%d -> %s"
                % (sorted(kinds.items()), j["n_verified"],
                   "已验过判据收在 HOLD-AB/HOLD-EXIST" if ok
                   else "★ 非 HOLD 裁决被算成了已验过"))


@gate("反向：账本目录不存在 -> ok=false（不许给「没有能力 / 一切正常」）")
def g_missing_dir_block(_):
    d = os.path.join(ROOT, "work", "capmap_gate_absent_zzz")
    shutil.rmtree(d, ignore_errors=True)
    j = with_cap_dir(d, lambda: serve.capmap_payload())
    ok = (j.get("ok") is False and bool(j.get("error"))
          and counts(j) == dict((k, 0) for k in BUCKETS))
    return ok, "ok=%s error=%r" % (j.get("ok"), (j.get("error") or "")[:44])


@gate("反向：ledger.jsonl 缺失但目录在 -> ok=false，不许静默返回空账本")
def g_missing_ledger_block(_):
    d = fresh("noledger")
    wl(d, "regression_verdicts.jsonl", [])
    j = with_cap_dir(d, lambda: serve.capmap_payload())
    shutil.rmtree(d, ignore_errors=True)
    ok = j.get("ok") is False and bool(j.get("error"))
    return ok, "ok=%s error=%r" % (j.get("ok"), (j.get("error") or "")[:48])


@gate("坏行计数不静默吞：3 合法 + 2 坏 -> 仍 ok，且 bad_lines==2")
def g_bad_lines_counted(_):
    d = fresh("bad")
    wl_raw(d, "ledger.jsonl", [
        json.dumps(mk("b1", "PROMOTED")), "{not json",
        json.dumps(mk("b2", "PROMOTED")), "",
        json.dumps(mk("b3", "PROMOTED")), "}}}",
    ])
    wl(d, "regression_verdicts.jsonl", [vv("b1", "HOLD-AB")])
    wl(d, "autopilot_verdicts.jsonl", [])
    wl(d, "events.jsonl", [])
    j = with_cap_dir(d, lambda: serve.capmap_payload())
    shutil.rmtree(d, ignore_errors=True)
    ok = (j.get("ok") is True and j["n_ledger"] == 3
          and j["bad_lines"] == 2 and j["n_verified"] == 1)
    return ok, "ledger=%s bad_lines=%s n_verified=%s" % (
        j.get("n_ledger", -1), j.get("bad_lines", -1), j.get("n_verified", -1))


@gate("取最新裁决按 ts 比而非「最后一行」：追加顺序与时间相反时旧裁决不得覆盖")
def g_latest_by_ts(_):
    d = fresh("ts")
    wl(d, "ledger.jsonl", [mk("t1", "PROMOTED")])
    wl(d, "regression_verdicts.jsonl", [
        vv("t1", "HOLD-AB", ts="2026-10-09T00:00:00Z"),
        vv("t1", "UNVERIFIABLE", ts="2026-10-01T00:00:00Z"),
    ])
    wl(d, "autopilot_verdicts.jsonl", [])
    wl(d, "events.jsonl", [])
    j = with_cap_dir(d, lambda: serve.capmap_payload())
    shutil.rmtree(d, ignore_errors=True)
    ok = j["n_verified"] == 1 and j["rows"][0]["verdict"] == "HOLD-AB"
    return ok, "verdict=%s n_verified=%d" % (j["rows"][0]["verdict"],
                                              j["n_verified"])


@gate("排序：已验过最上、anonymous 沉底（最不可追溯的账不该被埋在中间一眼扫过去）")
def g_order(_):
    d = fresh("order")
    wl(d, "ledger.jsonl", [
        {"status": "PROMOTED"},
        mk("z-nv", "EXPERIMENT"),
        mk("a-v", "PROMOTED"),
        mk("m-u", "PROMOTED"),
    ])
    wl(d, "regression_verdicts.jsonl", [vv("a-v", "HOLD-AB"),
                                         vv("m-u", "UNVERIFIABLE")])
    wl(d, "autopilot_verdicts.jsonl", [])
    wl(d, "events.jsonl", [])
    j = with_cap_dir(d, lambda: serve.capmap_payload())
    shutil.rmtree(d, ignore_errors=True)
    order = [r["proof"] for r in j["rows"]]
    want = ["ab-verified", "ab-unverifiable", "no-verdict", "anonymous"]
    return order == want, "order=%s" % order


def _rd(rel):
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


def _py_list(rel, name):
    """从某个 gate 的源码里 literal_eval 出顶层名单，读不到返回 None。

    用 ast 而不是 import：本门已经把 serve.py 当模块加载过了，再 import 四个 gate
    会引出 playwright / CDP 等副作用与启动开销。与 view_dod_selftest 同口径。
    view_dod.VIEWS 是 [(键, 中文名), ...] 的元组表，其余是纯字符串表，两种都要认。"""
    try:
        tree = ast.parse(_rd(rel))
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id == name:
            try:
                val = ast.literal_eval(node.value)
            except ValueError:
                return None
            if val and isinstance(val[0], tuple):
                return [k for k, _ in val]
            return list(val)
    return None


@gate("全量裁决记录数 >= 当前（每能力取最新一条）分布合计，且两者一致自洽")
def g_verdict_record_counts(_):
    """verdict_counts 与 verdict_record_counts 不是同一个数，屏上必须都能看见。
    生产实测：当前分布 20 条全HOLD-AB，而文件里共 73 条记录
    （UNVERIFIABLE 16 / HOLD-EXIST 31 / HOLD-AB 26）。只显示前者会把
    「26 条里 6 条从没判过、33 条历史裁决」全部藏起来。"""
    d = fresh("vdist")
    wl(d, "ledger.jsonl", [mk("cap-v", "PROMOTED"), mk("cap-n", "EXPERIMENT")])
    wl(d, "regression_verdicts.jsonl", [
        vv("cap-v", "HOLD-AB", ts="2026-01-01T00:00:00Z"),
        vv("cap-v", "UNVERIFIABLE", ts="2026-02-01T00:00:00Z"),
        vv("cap-v", "HOLD-AB", ts="2026-03-01T00:00:00Z"),
        vv("cap-x", "HOLD-EXIST", ts="2026-03-02T00:00:00Z"),
    ])
    wl(d, "autopilot_verdicts.jsonl", [])
    wl(d, "events.jsonl", [])
    j = with_cap_dir(d, lambda: serve.capmap_payload())
    shutil.rmtree(d, ignore_errors=True)
    if not j.get("ok"):
        return False, "夹具应当可读却 ok=false: %s" % j.get("error")
    cur = j.get("verdict_counts") or {}
    rec = j.get("verdict_record_counts") or {}
    cs, rs = sum(cur.values()), sum(rec.values())
    bad = []
    if j.get("n_verdict_records") != 4:
        bad.append("n_verdict_records=%r，应为 4" % j.get("n_verdict_records"))
    if rs != 4 or rec != {"UNVERIFIABLE": 1, "HOLD-EXIST": 1, "HOLD-AB": 2}:
        bad.append("verdict_record_counts=%r" % rec)
    if cur != {"HOLD-AB": 1}:
        bad.append("verdict_counts=%r，应只取 cap-v 最新一条 HOLD-AB" % cur)
    if cs > rs:
        bad.append("当前分布合计 %d > 全量记录 %d，逻辑不可能" % (cs, rs))
    if j.get("n_verdict_ids") != 2:
        bad.append("n_verdict_ids=%r，应为 2" % j.get("n_verdict_ids"))
    for k in ("n_verdict_records", "verdict_record_counts", "n_verdict_ids",
              "n_verdict_dangling", "dangling_ids"):
        if k not in j:
            bad.append("缺字段 " + k)
    return (not bad), ("current=%d(%s) all=%d(%s) ids=%s" % (
        cs, cur, rs, rec, j.get("n_verdict_ids"))
        if not bad else "★ " + "; ".join(bad))


@gate("悬空裁决 id：单独报数 + 报名单，且不得被并进任何一桶")
def g_dangling_not_bucketized(_):
    """裁决里有、能力账里查不到的 id 是**账与裁决脱节**的证据，必须自己占位。
    危险写法：把它悄悄算进 no-verdict（「没跑过 A/B」），于是屏上读成
    「这个能力还没验证」—— 但事实是「引擎判过它，账上却查不到它」。"""
    d = fresh("dangling")
    wl(d, "ledger.jsonl", [mk("cap-v", "PROMOTED"), mk("cap-n", "EXPERIMENT")])
    wl(d, "regression_verdicts.jsonl", [
        vv("cap-v", "HOLD-AB"),
        vv("ghost-1", "HOLD-EXIST"), vv("ghost-2", "UNVERIFIABLE"),
    ])
    wl(d, "autopilot_verdicts.jsonl", [])
    wl(d, "events.jsonl", [])
    j = with_cap_dir(d, lambda: serve.capmap_payload())
    shutil.rmtree(d, ignore_errors=True)
    if not j.get("ok"):
        return False, "夹具应当可读却 ok=false: %s" % j.get("error")
    c = counts(j)
    ids = [r["id"] for r in j["rows"]]
    bad = []
    if j.get("n_verdict_dangling") != 2:
        bad.append("n_verdict_dangling=%r，应为 2" % j.get("n_verdict_dangling"))
    if j.get("dangling_ids") != ["ghost-1", "ghost-2"]:
        bad.append("dangling_ids=%r" % (j.get("dangling_ids"),))
    if c != {"ab-verified": 1, "ab-unverifiable": 0,
             "no-verdict": 1, "anonymous": 0}:
        bad.append("四桶被悬空 id 污染：%s" % c)
    if sum(c.values()) != j["n_ledger"] != len(ids):
        bad.append("穷尽性被破坏 sum=%d ledger=%d rows=%d"
                   % (sum(c.values()), j["n_ledger"], len(ids)))
    for g in ("ghost-1", "ghost-2"):
        if g in ids:
            bad.append("悬空 id %s 被当成了能力行" % g)
    return (not bad), ("悬空=2 (%s) 未进桶，四桶仍穷尽 %d/%d"
                       % (j.get("dangling_ids"), sum(c.values()), j["n_ledger"])
                       if not bad else "★ " + "; ".join(bad))


@gate("反假绿：前端必须同时展示「当前分布」与「全量记录」两块，不得只剩一块")
def g_front_shows_both(_):
    """前端最容易犯的错不是算错，而是**只画一块**。判据：源码里必须同时出现
    当前分布说明（取最新一条）、全量记录数 + 悬空数/名单三个转发点。"""
    tpl = _rd("index.template.html")
    bad = []
    need = {
        "当前分布标题带「取最新一条」": "每条能力只取<strong>最新一条</strong>",
        "全量记录分布转发": "j.verdict_record_counts",
        "裁决记录总数转发": "j.n_verdict_records",
        "悬空数转发": "j.n_verdict_dangling",
        "悬空名单转发": "j.dangling_ids",
    }
    for k, v in need.items():
        if v not in tpl:
            bad.append(k)
    try:
        seg = tpl[tpl.index("function capAview(j){"):tpl.index("function capDraw(j){")]
    except ValueError:
        return False, "★ 找不到 capAview 边界"
    if "verdict_record_counts" not in seg:
        bad.append("capAview 内未渲染全量记录分布")
    if "dangling_ids" not in seg:
        bad.append("capAview 内未渲染悬空名单")
    if "不是裁决总量" not in seg:
        bad.append("未声明当前分布≠裁决总量")
    return (not bad), ("当前分布 / 全量记录 / 悬空名单三块都在屏上"
                       if not bad else "★ " + "; ".join(bad))


@gate("接线：tablist 里每个 tab 都接进 9 处名单（模板 / 构建产物 / 4 个门）+ cap 专用 5 处")
def g_wiring(_):
    """漏任何一处就是「半接线」：按钮在屏上能点，但键盘走查、200%/320px 重排、
    截图 DoD、cmd-K 搜索全都覆盖不到它 —— 而这些门**不会报红**，只会让新屏悄悄
    脱离所有回归保护。D95 已经在 a11y_human / layout_gate / JS 三处各漏过一次；
    R7 的 term/file/set 三面上线时同理，所以本门改成**从 tablist 真读键名**。

    为什么不再是写死 cap：原来这门只核「cap 在不在 5 处」，第 11-13 屏上线后
    它一句话都不说地放行了 —— 三个新 tab 有没有接过线，全靠人肉记得。现在键名
    直接正则抽 tablist 的 data-v，谁加了 tab 却没进任何一张名单，当场逐键报出。

    cap 专用那几条（capLoad/capReset/capgo 绑定/view() 复位）只有能力地图有，
    且**只查模板真源**：它们不是名单，是这一面自己的加载逻辑，别的视图没有对应物。
    """
    tpl = _rd("index.template.html")
    built = _rd("index.html")
    # a11y_human 按**中文标签**在册，与其余各处按键盘在册不是一套口径 —— 每个键都
    # 得给它自己的标签。这些字面与 a11y_human.VIEWS 逐项对应：eng 是「引擎」而
    # 不是「引擎与规则」（模板 tab 上写「引擎」），work 是「Agent 台」带一个空格。
    labels = {"over": "总览", "round": "轮次", "mx": "阶段矩阵",
              "led": "证据账本", "eng": "引擎", "work": "Agent 台",
              "goal": "目标台", "retro": "复盘台", "cap": "能力地图",
              "ws": "工作区", "term": "终端", "file": "文件", "set": "设置"}
    keys = re.findall(r'<button role="tab"[^>]*\bdata-v="([^"]+)"', tpl)
    bad, n = [], 0

    def want(desc, cond):
        nonlocal n
        n += 1
        if not cond:
            bad.append(desc)

    for k in keys:
        if k not in labels:
            # 新 tab 上线却没人给它定 a11y_human 标签：必须显式红，不能默认放过。
            want("%s: 未知视图键（labels 表里没有它，先补中文标签再上线）" % k, False)
            continue
        want("%s: tab 按钮 t-%s" % (k, k), 'id="t-%s"' % k in tpl)
        want("%s: 面板 v-%s" % (k, k), 'id="v-%s"' % k in tpl)
        want("%s: CL 面板映射" % k, "%s:'v-%s'" % (k, k) in tpl)
        want("%s: TAB 按钮映射" % k, "%s:'t-%s'" % (k, k) in tpl)
        want("%s: JS VIEWS 名单（⌘K 搜得到）" % k, "['%s'," % k in tpl)
        want("%s: 构建产物 index.html 已含 v-%s" % (k, k), 'id="v-%s"' % k in built)
        want("%s: view_dod.VIEWS" % k, '("%s",' % k in _rd("tools/view_dod.py"))
        want("%s: a11y_walk.TAB_KEYS" % k, '"%s"' % k in _rd("tools/a11y_walk.py"))
        want("%s: a11y_human.VIEWS 标签「%s」" % (k, labels[k]),
             '"%s"' % labels[k] in _rd("tools/a11y_human.py"))
        want("%s: layout_gate.VIEWS" % k, '"%s"' % k in _rd("tools/layout_gate.py"))

    # 逐键在册之外，还要**一张名单都不许多出键**：门里留着一个 tablist 已经没有的
    # 键，意味着那道门在给一个不存在的屏跑回归，绿的是死代码。
    for rel, name in (("tools/view_dod.py", "VIEWS"),
                      ("tools/a11y_walk.py", "TAB_KEYS"),
                      ("tools/layout_gate.py", "VIEWS")):
        got = _py_list(rel, name)
        if got is None:
            want("%s %s 读不到（改名了？）" % (rel, name), False)
        else:
            want("%s %s 与 tablist 逐键一致（缺 %s/多 %s）"
                 % (rel, name,
                    "/".join(k for k in keys if k not in got) or "无",
                    "/".join(k for k in got if k not in keys) or "无"),
                 sorted(got) == sorted(set(keys)))

    for desc, cond in (("template:capLoad 已定义", "async function capLoad()" in tpl),
                       ("template:capReset 已定义", "function capReset()" in tpl),
                       ("template:capgo 已绑定",
                        "$('#capgo').addEventListener('click',capLoad)" in tpl),
                       ("template:view() 走 capReset",
                        "if(key==='cap'){ capReset(); }" in tpl),
                       ("layout_gate.SETTLE 里 cap 落到真数据态",
                        '"cap": ("#capgo", "#capbuckets .capb")'
                        in _rd("tools/layout_gate.py"))):
        want(desc, cond)

    return (not bad), ("tablist 抽到 %d 个键，%d/%d 处齐%s"
                       % (len(keys), n - len(bad), n,
                          "；缺 " + " / ".join(bad) if bad else ""))



@gate("反假绿：渲染层不许有第二套判定（不得用 status 推导证明等级）")
def g_no_fake_green_path(_):
    """反向的**前端**一半。后端分桶再对，屏上照样可能假绿 —— 危险写法长这样：
    if(r.status==='PROMOTED') 打绿标。所以渲染层只允许引用后端给的 r.proof，
    不允许重算。这里直接在源码里找这种路径，而不是靠肉眼看渲染结果。"""
    tpl = _rd("index.template.html")
    segs = {}
    for name, a, b in (("capDraw", "function capDraw(j){", "function capRedraw()"),
                       ("capRow", "function capRow(r){", "function capBuckets(j){")):
        try:
            segs[name] = tpl[tpl.index(a):tpl.index(b)]
        except ValueError:
            return False, "★ 找不到 %s 的函数边界" % name
    bad = []
    for name, seg in segs.items():
        if "r.proof" not in seg:
            bad.append(name + ": 未引用后端 r.proof")
        if re.search(r"status\s*===?\s*['\"]PROMOTED['\"]", seg):
            bad.append(name + ": 用 status 推导了证明等级")
    head = tpl[tpl.index("let CAPLAST"):tpl.index("function capDraw(j){")]
    flat = head.replace("'", "").replace('"', "")
    missing = [k for k in BUCKETS if ("k:" + k) not in flat]
    if missing:
        bad.append("CAP_BUCKETS 少桶 " + "/".join(missing))
    return (not bad), ("渲染层只引用后端 r.proof，四桶定义集中在 CAP_BUCKETS"
                       if not bad else "★ " + "; ".join(bad))


def main():
    os.makedirs(os.path.join(ROOT, "work"), exist_ok=True)
    before = prod_hash()
    report = {"tool": "capmap_gate.py", "gates": [], "prod_sha_before": before}
    npass = 0
    for name, fn in GATES:
        try:
            ok, detail = fn({})
        except Exception as e:
            ok, detail = False, "EXC %s: %s" % (type(e).__name__, e)
        report["gates"].append({"name": name, "ok": bool(ok), "detail": detail})
        print("  %s %s  %s" % ("[ok  ]" if ok else "[FAIL]", name, detail))
        npass += bool(ok)
    after = prod_hash()
    clean = before == after
    nm = "生产账本零污染：全程 sha256 未变"
    detail = " ".join("%s=%s" % (k[:6], v[:8]) for k, v in sorted(before.items()))
    report["gates"].append({"name": nm, "ok": clean, "detail": detail})
    print("  %s %s  %s" % ("[ok  ]" if clean else "[FAIL]", nm, detail))
    npass += bool(clean)
    total = len(report["gates"])
    report["passed"], report["total"] = npass, total
    with open(os.path.join(ROOT, "work", "capmap_gate.json"), "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print("== CAPMAP_GATE %s  %d/%d 门通过（账本全隔离夹具）=="
          % ("OK " if npass == total else "FAIL", npass, total))
    return 0 if npass == total else 2


if __name__ == "__main__":
    sys.exit(main())
