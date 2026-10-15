#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D47 view_dod 变异体自测：证明 DoD 门的 6 道判据每道都能变红。

为什么必须有这个门（真实缺口，不是预防性焦虑）：
  view_dod.py 是「视图数 × 尺寸数」全屏真视口走查的唯一 DoD 门（交接任务 ② 的交付物）。
  改造前它**没有任何反向门** —— 全仓 11 个门里只有 freshness_gate / headroom_gate
  自带变异体。一个从没红过的门，它的绿只是「它会返回 0」，不是「它判得对」。

  而且构造上它**无法**自证：view_dod.py 硬编码 HTML=index.html 与写死的 SHOTS
  目录，想在副本上注入缺陷就只能就地改活的 index.html —— 正是 tools/neg_a11y.py
  记录的 D39 事故（注入窗口内常驻 8790 实例与 act_gate 的真点按读到缺陷版，
  表现为「门随机红」）。所以 D47 的第一步是给 view_dod.py 补 --html/--shots/--out
  三条隔离缝（默认值一字未改，老调用方零影响），本门才得以在副本上注入。

判据（双向，缺一条即 rc=2）：
  BASE   生产调用（零参数）必须 rc=0 —— 有牙 ≠ 只会红，只会红的门同样没用。
  M1-M6  每个变异体必须 rc=2 **且指定的那道门**出现在 [FAIL] 行里。
         只断言 rc!=0 是不够的：量具自己崩掉也给非 0，那是量具死了不是判据红。
零污染 跑完活 index.html 的 sha 与基线一致（mutants 只碰临时目录）。
         承重判据是「隔离缝真隔离」—— 6 个变异体各自只往自己的临时目录落图。
         为什么承重判据从共享目录搬走，见下面「共享目录为什么不能独自承重」一节
         （2026-10-03 实测的并发假红）。

M3 打靶打歪过一次，留档免得下一个人再犯：最初把 2000px 定宽块塞进视图面板，
门不红。实测不是门有洞 —— `#main` 是 overflow:auto、`body` 是 overflow:hidden，
面板内的溢出被这个**合法滚动容器**吸收，docScrollW 纹丝不动（1440=1440），
本就不该判红。改到 body 层注入后 docScrollW=2000 > clientW=1440，门如期变红。
顺带一条如实记录：PROBE_JS 里 offenders 那条子路径在本页布局下几乎不会触发
（所有越界元素都被 #main 吸收），真正承重的是 docScrollW>docClientW 这一条。

用法: python3 tools/view_dod_selftest.py      # rc=0 全过 / 2 有漏网
"""
import ast
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "index.html")
GATE = os.path.join(HERE, "view_dod.py")
REAL_SHOTS = os.path.join(ROOT, "docs", "UX-SHOTS", "views")


def _viewdod_screens():
    """走查总屏数 = view_dod 的 len(VIEWS) × len(WIDTHS)，现读现算。

    2026-10-10 R7 把视图从 9 扩到 13 之后，本门任何「27」的字面都会和真走查屏数
    打架（BASE 是零参数生产调用，屏数由 view_dod 自己推导）。用 ast 取字面而不
    import，是为了不执行 view_dod 模块体的任何副作用。
    """
    tree = ast.parse(open(os.path.join(HERE, "view_dod.py"), encoding="utf-8").read())
    got = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id in ("VIEWS", "WIDTHS"):
            got[node.targets[0].id] = ast.literal_eval(node.value)
    return len(got["VIEWS"]) * len(got["WIDTHS"])


NSCREENS = _viewdod_screens()
sys.path.insert(0, HERE)
from variant_pool import map_cases
INSTR_RETRY_N = 2      # 量具无结论时的重试次数（重试量具，不是重试判据）
INSTR_RETRY_S = 2.0
# M6 进度驱动的兜底上限：等第一张截图落盘最多等这么久（量具早崩时不留死线程）。
MIDRUN_TIMEOUT_S = 180.0

# 变异体 -> (注入名, 期望变红的那道门的名字片段, 注入方式, 注入内容)
# 注入方式 kind: "js" = 往隔离副本 </body> 前追加脚本；
# "rewrite-midrun" = 走查进行中改写副本内容（等第一张截图落盘 = 走查真的开始了，
#                   与机器快慢无关；改前按 BASE 耗时 35% 定时，见 D104）。
CASES = [
    ("M1 视图切不过去（view() 空转 -> 面板永不可见）",
     "屏截图齐",          # 稳定后缀：门名里视图/屏数是推导量，写死会在加视图当天假红
     "js", "window.view=function(){/*D47-neg:no-op*/};"),
    ("M2 真视口被冒充（innerWidth 被改写，与 headless --window-size 同类造假）",
     "真视口未被冒充",
     "js", "Object.defineProperty(window,'innerWidth',{get:()=>999,configurable:true});"),
    ("M3 文档级横向溢出（body 层塞 2000px 定宽块，撑破文档宽度）",
     "任一视图存在文档级横向溢出",
     "js", "document.body.insertAdjacentHTML('beforeend',"
           "'<div style=\"width:2000px;height:8px;background:#c00\"></div>');"),
    ("M4 视图是空壳（轮次内容被清空，aria_selected 仍命中）",
     "任一视图是空壳",
     "js", "document.getElementById('v-round').textContent='';"),
    ("M5 视图里抛未捕获异常（真 pageerror）",
     "任一视图 JS 报错",
     "js", "throw new Error('D47-neg-pageerror');"),
    # M6（2026-10-03 D50 改）：判据从 mtime 换成内容指纹，所以变异体也必须换量纲 ——
    # 原 M6 推 mtime 到未来，改判据后它不再触发任何东西（实测 selftest 8/9 报
    # 「期望门没红=空门」）。现在改成**走查进行中**改副本内容：这既命中新判据，
    # 又比推 mtime 更贴近真事故（并发 build.py 在 NSCREENS 屏之间落盘）。
    ("M6 截图冒充旧构建（走查进行中页面被改 -> %d 屏非同一版本）" % NSCREENS,
     "截图为旧版本冒充本轮",
     "rewrite-midrun", ""),
]

results = []


def gate(name, ok, detail=""):
    results.append({"gate": name, "ok": bool(ok), "detail": detail})
    print(("  [ok  ] " if ok else "  [FAIL] ") + name + (("  " + detail) if detail else ""))


def sha12(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:12]


def shots_fingerprint(d):
    """真截图目录的全量指纹：文件名 + 字节 sha。零写入核验靠它，不靠 mtime。"""
    if not os.path.isdir(d):
        return {}
    return {f: sha12(os.path.join(d, f)) for f in sorted(os.listdir(d)) if f.endswith(".png")}


def shots_diff(before, after):
    """真实 diff：新增 / 消失 / 内容变化（前后 sha 都带上）。
    存在的理由：旧诊断把 detail 写死成「%d 张 sha 全部不变」——
    红的时候也印「全部不变」，把排查方向指到反的地方去。"""
    added = [f for f in sorted(after) if f not in before]
    gone = [f for f in sorted(before) if f not in after]
    changed = ["%s %s->%s" % (f, before[f], after[f])
               for f in sorted(before) if f in after and before[f] != after[f]]
    return added, gone, changed


def png_count(d):
    """目录里 .png 的张数。-1 = 目录压根没建出来（比 0 更糟：缝连落地都没落地）。"""
    if not os.path.isdir(d):
        return -1
    return len([f for f in os.listdir(d) if f.endswith(".png")])


def seam_wrote(n):
    """隔离缝判据（纯函数，好对它单独做变异体自证）：落了 >=1 张 png 即为「缝生效」。
    阈值是 1 不是 27 —— 本来就期望变红的那几个变异体可能在第几屏就崩，
    写不满 NSCREENS 张并不等于缝没生效；缝没生效的唯一可观测信号是「一张都没落这儿」。"""
    return n >= 1


def _caught(fn, arg, expected):
    """变异谓词在 arg 上给出**错误**结论 = 它被咬住了。"""
    return (fn(arg) is expected) is False


def _restore(iso_html, pristine, shots_dir):
    """把隔离副本还原成 pristine，并清空该变异体自己的截图目录。

    每次量具尝试前都要还原：改写是**一次性**的，不还原的话重试那次 start/end
    读到的都是已改写的内容 → 判据不红 → 变异体被自己的重试洗白（D104）。
    """
    with open(iso_html, "w", encoding="utf-8") as f:
        f.write(pristine)
    if os.path.isdir(shots_dir):
        for fn in os.listdir(shots_dir):
            if fn.endswith(".png"):
                try:
                    os.unlink(os.path.join(shots_dir, fn))
                except OSError:
                    pass


def _arm_midrun(iso_html, mutated, shots_dir, timeout_s):
    """等到「走查真的开始了」（第一张截图落盘）再改写副本，返回该线程。

    D104（2026-10-11 实测，不是预防性改动）：原实现按 BASE 耗时的 35% sleep 定时。
    45 门全链里 6 个变异体并发抢 CPU，M6 判成「期望门没红=空门」—— 定时窗口与实际
    走查窗口脱钩，改写落在 _sha_end 之后（那一刻门本来就该绿），变异体自证失效
    （证据 /tmp/fullchain45.out 第 259 行，variant M6 155.1s 却无红行）。
    进度驱动把「改写落在 start/end 之间」变成结构性事实：第一张 png 落盘必然晚于
    起始 sha、早于结束 sha。timeout_s 是兜底，量具早崩时不留死线程。
    """
    def _go():
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            if png_count(shots_dir) >= 1:
                break
            time.sleep(0.05)
        try:
            with open(iso_html, "w", encoding="utf-8") as f:
                f.write(mutated)
        except OSError:
            pass

    th = threading.Thread(target=_go, daemon=True)
    th.start()
    return th


def run_gate(argv, midrun=None):
    """midrun=(iso_html, pristine, mutated, shots_dir)：走查途中把副本改写成 mutated。
    用来制造「页面在走查途中被改」——这是新判据要抓的真事故。"""
    killer = None
    if midrun:
        iso_html, pristine, mutated, shots_dir = midrun
        _restore(iso_html, pristine, shots_dir)
        killer = _arm_midrun(iso_html, mutated, shots_dir, MIDRUN_TIMEOUT_S)
    r = subprocess.run([sys.executable, GATE] + argv,
                       cwd=ROOT, capture_output=True, text=True)
    if killer:
        killer.join(timeout=5)
    reds = [l.strip()[:160] for l in r.stdout.splitlines() if "[FAIL]" in l]
    summ = [l.strip() for l in r.stdout.splitlines() if "门通过" in l]
    return r.returncode, reds, (summ[-1] if summ else ""), r


def run_with_retry(argv, midrun=None):
    """量具无结论（汇总行缺失，通常是 Chrome 被 SIGTERM）时重试量具，绝不重试判据。"""
    rc, reds, summ, raw = run_gate(argv, midrun)
    killed = 0
    for _ in range(INSTR_RETRY_N):
        if summ:
            break
        killed += 1
        time.sleep(INSTR_RETRY_S)
        rc, reds, summ, raw = run_gate(argv, midrun)
    return rc, reds, summ, raw, killed


def build_isolated(dst, script=""):
    """把活 index.html 复制到隔离副本，可选注入脚本。注入点未命中返回 None。"""
    h = open(SRC, encoding="utf-8").read()
    if script:
        marker = "</body>"
        if marker not in h:
            return None
        h = h.replace(marker, "<script>%s</script>\n%s" % (script, marker), 1)
    with open(dst, "w", encoding="utf-8") as f:
        f.write(h)
    return dst


def run_mutant(case, td):
    """跑一个变异体，返回 (name, ok, detail, shots_png_count)。

    D72：原来 M1..M6 在 main 里逐个跑，149s 全串行。并发安全的依据与
    variant_pool 的 docstring 同源：每个变异体持有自己的 iso_html / iso_shots /
    iso_out 三份独立路径，零写活的 index.html 与共享截图目录。
    BASE 仍留在主线程串行跑在最前面 —— 它是**零参数生产调用**，要按设计写共享
    目录 docs/UX-SHOTS/views（否则验的就不是生产路径了）。
    M6 的触发不再是 wall-clock 定时（D104），所以这里不再需要 BASE 耗时。
    """
    name, target, kind, script = case
    tag = name.split()[0]
    iso_html = os.path.join(td, "mut-%s.html" % tag)
    iso_shots = os.path.join(td, "shots-%s" % tag)
    iso_out = os.path.join(td, "out-%s.json" % tag)
    os.makedirs(iso_shots, exist_ok=True)
    if build_isolated(iso_html, script if kind == "js" else "") is None:
        return name, False, "注入点 </body> 未命中（反向门有洞）", -1
    midrun = None
    if kind == "rewrite-midrun":
        h2 = io.open(iso_html, encoding="utf-8").read()
        midrun = (iso_html, h2,
                  h2.replace("</body>", "<!--midrun-->\n</body>", 1),
                  iso_shots)
    rc, reds, summ, raw, killed = run_with_retry(
        ["--html", iso_html, "--shots", iso_shots, "--out", iso_out],
        midrun=midrun)
    note = "量具无结论重试 %d 次；" % killed if killed else ""
    if not summ:
        return name, False, note + "量具自身异常（无门汇总行）: %s" \
            % raw.stderr.strip().splitlines()[-1:], png_count(iso_shots)
    hit = [r for r in reds if target in r]
    ok = rc != 0 and bool(hit)
    detail = "%s期望门「%s」%s" % (note, target, "已红" if hit else "**没红=空门**")
    return name, ok, detail, png_count(iso_shots)


def main():
    if not os.path.exists(SRC):
        print("FAIL: index.html 不存在")
        return 2
    base_sha = sha12(SRC)
    td = tempfile.mkdtemp(prefix="vdod-selftest-")
    try:
        # ---------- BASE：生产调用（零参数）必须绿 ----------
        rc, reds, summ, raw, _ = run_with_retry([])
        if not summ:
            print("!! 量具自身异常（无门汇总行），不能算结论: %s"
                  % raw.stderr.strip().splitlines()[-1:])
            return 2
        gate("BASE 生产调用零参数 rc=0（默认值未被隔离缝改坏）",
             rc == 0 and not reds, summ)
        real_shots = shots_fingerprint(REAL_SHOTS)

        # ---------- M1..M6 ----------
        seam_counts = {}
        # D72：6 个变异体并发跑，输出仍按 CASES 原始顺序回放（见 variant_pool）。
        # worker 把结构化结论写进 verdicts[i]（每线程一格，互不覆盖），
        # 同时返回该变异体的打印行块 —— 主线程按序打印后统一 gate()，
        # 这样「输出顺序」和「results 顺序」都与改前逐字节一致。
        verdicts = {}

        def _worker(i, case):
            name, ok, detail, n = run_mutant(case, td)
            verdicts[i] = (name, ok, detail, n)
            return ["[MUT %s] %s · %s" % (name.split()[0], "OK" if ok else "FAIL", detail)]

        for line in map_cases(CASES, _worker, header=lambda i, c: c[0].split()[0]):
            print(line)
        for i, case in enumerate(CASES):
            name, ok, detail, n = verdicts[i]
            gate(name, ok, detail)
            seam_counts[name.split()[0]] = n

        # ---------- 零污染 ----------
        now_sha = sha12(SRC)
        gate("活 index.html 零写入（mutants 只碰临时目录）",
             now_sha == base_sha, "sha %s -> %s" % (base_sha, now_sha))

        # --- 承重判据：隔离缝真隔离 -------------------------------------------
        # 共享目录为什么不能独自承重（2026-10-03 实测，不是推演）：
        #   BASE 是**零参数生产调用**，按设计就往共享目录 docs/UX-SHOTS/views
        #   写那 NSCREENS 张 —— 这正是它要验的东西，不能改成隔离目录，否则验的就
        #   不是生产路径了。而引擎 D50 的 desktop_gates 13 项里也有
        #   view_dod_selftest：两条链并发时两个进程同时重写同一批 NSCREENS 张，
        #   对方落的盘被算成本门的漂移。实测台账 /tmp/chain_runs/130550/rc.txt
        #   14 门里唯一红项就是 view_dod_selftest rc=2，红的正是共享目录对账。
        # 锁也救不了：锁只串行化写者，挡不住「本门 BASE 刚拍完快照、对方随后
        # 开始重写」这个窗口，漂移照样进账。彻底的修法是**不共享**。
        # 而「缝有没有生效」只看本门自己写过的临时目录就能判，结构上碰不到
        # 别人的进程。它比旧判据**更敏感**：旧的靠 sha 变化间接推断，新的直接
        # 看「图有没有落进该落的目录」，并多抓一类旧判据抓不到的回归 ——
        # 除截图外的**额外写入**（谁新加一条硬编码路径就会露出来）。
        gap_note = "；0/缺失=缝没生效"
        seam_bad = ["%s=%d" % (t, n) for t, n in sorted(seam_counts.items())
                    if not seam_wrote(n)]
        counts_note = " ".join("%s=%d" % kv for kv in sorted(seam_counts.items()))
        gate("隔离缝真隔离（6 个变异体只往各自临时目录落图）",
             not seam_bad and len(seam_counts) == len(CASES),
             ("PNG 张数 " + counts_note + gap_note)
             if not seam_bad else "缝失效=" + ",".join(seam_bad))

        # 承重判据自己也得有牙：对 seam_wrote 跑正例 + 3 个变异谓词。
        seam_pos = [("空目录=0 判假", seam_wrote(0) is False),
                    ("1 张判真", seam_wrote(1) is True),
                    ("%d 张判真" % NSCREENS, seam_wrote(NSCREENS) is True)]
        seam_muts = [("M1 恒真 n>=0（漏放空目录）", lambda n: n >= 0, 0, False),
                     ("M2 过严 n>=99（误杀正常批次）", lambda n: n >= 99, 1, True),
                     ("M3 差一 n>1（漏放 1 张）", lambda n: n > 1, 1, True)]
        seam_caught = [nm for nm, fn, a, exp in seam_muts if _caught(fn, a, exp)]
        gate("判据自证：隔离缝判据 3 正例 + 3 变异谓词双向咬合",
             all(ok for _n, ok in seam_pos)
             and len(seam_caught) == len(seam_muts),
             "正例 %d/%d · 变异体咬住 %d/%d"
             % (sum(ok for _n, ok in seam_pos), len(seam_pos),
                len(seam_caught), len(seam_muts)))

        # --- 共享目录对账：保留，但降级为报告型（三类分开判，不许一刀切） ---
        cur = shots_fingerprint(REAL_SHOTS)
        added, gone, changed = shots_diff(real_shots, cur)
        why = []
        if added:
            why.append("新增 %s" % ",".join(added[:4]))
        if gone:
            why.append("消失 %s" % ",".join(gone[:4]))
        if changed:
            why.append("改写 %d 张（例 %s）" % (len(changed), changed[0]))
        drift = "; ".join(why)
        if not real_shots:
            gate("真截图目录零污染（BASE 后逐字节一致）", False,
                 "基线为空：BASE 跑完共享目录里一张图都没有，无法对账（fail-closed）")
        elif not why:
            gate("真截图目录零污染（BASE 后逐字节一致）", True,
                 "%d 张 sha 逐字节不变（真无漂移）" % len(real_shots))
        elif added or gone:
            # 别人跑生产 view_dod 只可能覆写同样那批名字（实测 view_dod.py 既不删
            # 也不另建文件），所以**增删文件不可能是外部重写** —— 那是目录形状
            # 被改了，属真污染，不能拿「并发」两个字糊过去。
            gate("真截图目录零污染（BASE 后逐字节一致）", False,
                 "**增删文件=非外部重写，真污染**：" + drift)
        elif seam_bad:
            gate("真截图目录零污染（BASE 后逐字节一致）", False,
                 "隔离缝已失效，漂移就可能是变异体写的：" + drift)
        else:
            # 同名内容漂移 + 缝已证生效 ⇒ 结构上本门写不到这个目录 ⇒ 归因并发。
            # 这不是放水，是把归因判据从「不可归因的共享 sha」换成「可归因的本门
            # 隔离目录」；承重判据本身是上面那条门。
            gate("真截图目录零污染（BASE 后逐字节一致）", True,
                 "同名内容漂移 %d 张，归因=并发重写生产截图"
                 "（隔离缝判据已排除本门）；例 %s" % (len(changed), changed[0]))

        with open(os.path.join(ROOT, "work", "view_dod_selftest.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"tool": "view_dod_selftest.py", "n_mutants": len(CASES),
                       "gates": results}, f, ensure_ascii=False, indent=1)
    finally:
        shutil.rmtree(td, ignore_errors=True)

    nfail = sum(1 for r in results if not r["ok"])
    print("== VIEW_DOD_SELFTEST %s  %d/%d 门通过（%d 个变异体，1:1 覆盖 6 道 DoD 判据）=="
          % ("OK" if nfail == 0 else "FAIL", len(results) - nfail, len(results), len(CASES)))
    if nfail == 0:
        print("VIEW_DOD_SELFTEST-OK %d/%d 例" % (len(CASES), len(CASES)))
    return 0 if nfail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
