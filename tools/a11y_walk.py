#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""键盘 / 读屏实机走查门：真浏览器真按键事件 + 真无障碍树（CDP AX tree）。

与 tools/ux_audit.py 第 5 条的区别（那条是机检，本条是实测）：
  机检 = 在源码里 grep 有没有 "keydown" / "aria-label" 字样 —— 有字样不等于能用。
  实测 = 真按 Tab / Enter / ArrowDown / Escape，逐条断言焦点真落在哪、视图真切没切，
         再用 Chrome 自己算的 accessibility tree 读出每个可交互节点的可访问名。
         aria 覆盖因此是「浏览器读出来的」，不是「源码里写的」。

2026-10-02 补 (0) 可信输入前提门：
  原来 15 门全部建立在「按下去的是浏览器可信输入」这个**从未被断言**的前提上。
  pg.keyboard.press 与 dispatchEvent(new KeyboardEvent(...)) 在 handler 里长得一模一样
  （都是 keydown + 同一个 key 值），后者照样能驱动同一段切视图代码 —— 于是
  「15 门全绿 = 键盘可用」可以和「页面其实只认合成事件」同时为真，门是瞎的。
  isTrusted 是浏览器给的、JS 改不了的唯一判据，故补此门；
  并自带合成负控：故意派发一个合成事件，若它也被报成 trusted，说明探测器没牙。

产出 work/a11y_walk.json，rc=0 全过 / 2 有红项。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
HTML = os.path.join(ROOT, "index.html")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
TAB_KEYS = ["over", "round", "mx", "led", "eng", "work", "goal", "retro",
            "cap", "ws", "term", "file", "set"]
# 前端数字键 1..9 对第 1..9 个视图，**第 10 个视图用 0**（屏上 kbd 标签同）。
# D95 实测教训：这层以前只列 8 个视图，于是第 10 个视图上线后前端那句
# k<=String(TABMAX) 变成字符串比较、'2'<='10' 为 false，门只报「键 2 没反应」，
# 根因（字符串比较）却完全看不见 —— 门连第 9/10 个视图都没按过。
# 2026-10-10 R7（tablist 已有 13 个 tab）：数字键只到第 10 屏，到此为止。
# tablist 第 11-13 屏（终端 term / 文件 file / 设置 set）屏上**没有** kbd 数字标签，
# 进屏通道是 Tab 进 tablist、方向键移动、回车确认，或 Cmd+K 命令面板直达（老板裁定：
# 不给凑不出来的数字键）。为什么必须截断：DIGIT_KEYS 若跟着 len(TAB_KEYS)=13 长下去，
# '11'/'12'/'13' 不是合法按键名，playwright 直接抛 Unknown key，整门 0 命中假红 ——
# 这正是 D95「字符串比较」那一课的第二次复发，截断是拦住它的唯一办法。
DIGIT_KEYS = [str(i) for i in range(1, min(10, len(TAB_KEYS)) + 1)]
DIGIT_KEYS[9] = "0"
DIGITLESS = TAB_KEYS[len(DIGIT_KEYS):]   # 没有数字键、靠 Tab/方向键/Cmd+K 进的尾部视图

ACTIVE_JS = """() => {
  const a = document.activeElement;
  if (!a || a === document.body) return {tag: 'BODY', id: '', name: '', visible: true};
  const r = a.getBoundingClientRect();
  const cs = getComputedStyle(a);
  return {
    tag: a.tagName, id: a.id || '',
    name: (a.getAttribute('aria-label') || a.innerText || a.title || '').trim().slice(0, 40),
    visible: r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none',
  };
}"""

READY_JS = """() => document.readyState === 'complete'
  && [...document.querySelectorAll('.nav button')]
       .filter(b => b.getAttribute('tabindex') === '0').length === 1"""

SELECTED_JS = """() => [...document.querySelectorAll('[role=tab]')]
    .filter(t => t.getAttribute('aria-selected') === 'true').map(t => t.id)"""

# Tab 序基准：给可见可聚焦元素打稳定序号，作为 Tab 链的身份标识。
# 不用 tag+id —— 矩阵里 60+ 个 button 都没有 id，会被折叠成同一个键
# （实测 mx 有 68 个可见可聚焦元素，按 tag+id 只算出 3 个「不同元素」：量具坏了，不是页面坏了）。
STAMP_JS = """() => {
  const sel = 'a[href],button,input,select,textarea,summary,[tabindex]:not([tabindex="-1"])';
  const vis = e => { const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  // 必须同时满足可见 AND 真正可 tab。只判可见会把 tabindex="-1" 的元素也算进去
  // —— 而 roving tabindex 恰恰靠 tabindex=-1 把 4 个非活动 tab 移出 Tab 序，
  // 实测那样会把 nav 的 5 个 tab 全算成可聚焦，期望序列就永远对不上（量具的错，不是页面的错）。
  const tabbable = e => {
    if (e.disabled) return false;
    const ti = e.getAttribute('tabindex');
    if (ti !== null && +ti < 0) return false;
    if (e.closest('[inert]')) return false;
    if (e.tagName === 'A' && !e.hasAttribute('href')) return false;
    return vis(e);
  };
  document.querySelectorAll('[data-a11y-i]').forEach(e => e.removeAttribute('data-a11y-i'));
  const live = [...document.querySelectorAll(sel)].filter(tabbable);
  live.forEach((e, i) => e.setAttribute('data-a11y-i', String(i)));
  return live.map((e, i) => ({ i: i, tag: e.tagName,
    name: (e.getAttribute('aria-label') || e.innerText || e.title || '').trim().slice(0, 24) }));
}"""

CUR_JS = """() => {
  const a = document.activeElement;
  if (!a || a === document.body) return { i: -1, tag: 'BODY', name: '', visible: true };
  const r = a.getBoundingClientRect(); const cs = getComputedStyle(a);
  return { i: a.hasAttribute('data-a11y-i') ? +a.getAttribute('data-a11y-i') : -2,
    tag: a.tagName,
    name: (a.getAttribute('aria-label') || a.innerText || a.title || '').trim().slice(0, 24),
    visible: r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' };
}"""

results = []


# (0) 可信输入探针：在 window 上装捕获阶段记录器（capture=true，
#     保证页面自己的 stopPropagation 也拦不住它 —— 否则「页面吞掉了事件」
#     和「事件从没发生」在报告里长得一样）。
TRUST_INIT_JS = """() => {
  if (window.__trustHooked) return;
  window.__trustHooked = true;
  window.__keylog = [];
  const rec = (e) => window.__keylog.push({key: e.key, isTrusted: e.isTrusted});
  window.addEventListener('keydown', rec, true);
  window.addEventListener('keyup', rec, true);
}"""

TRUST_DRAIN_JS = """() => {
  const l = (window.__keylog || []).slice();
  window.__keylog.length = 0;
  return l;
}"""

SYNTH_JS = """() => {
  window.dispatchEvent(new KeyboardEvent('keydown',
    {key: 'Tab', bubbles: true, cancelable: true}));
  window.dispatchEvent(new KeyboardEvent('keyup',
    {key: 'Tab', bubbles: true, cancelable: true}));
}"""


def gate(name, ok, detail=""):
    results.append({"gate": name, "ok": bool(ok), "detail": detail})
    print(("  [ok  ] " if ok else "  [FAIL] ") + name + (("  " + detail) if detail else ""))


# ── 纯判据（读屏结构；变异测试的对象；不碰浏览器） ──────────────────────────
# 为什么要补这三门（2026-10-03 实测缺口）：
#   原有 16 门覆盖「可交互节点有没有可访问名 / Tab 链能不能走 / 有没有 live region」。
#   但读屏用户真正的第二条导航通道是**地标（landmark）与标题（heading）**：
#   VoiceOver/ NVDA 的 rotor 先列地标、再列标题，用户靠它从 5 个视图里定位。
#   实测本台面此前只有「节点有名字」这一层 —— 把地标名字全删、标题层级跳成 H2→H6，
#   16 门**照样全绿**（因为节点仍各自有名字）。这三条是那一层的补门。
LANDMARK_ROLES = ("banner", "main", "navigation", "complementary",
                  "contentinfo", "region", "form", "search")


def struct_headings_ok(levels):
    """标题层级：首个必须是 2（视图自己的名字），且任何一级不得跳级。"""
    if not levels:
        return False, "无标题节点（读屏 rotor 里这个视图会整个消失）"
    if levels[0] != 2:
        return False, "首个标题层级=%d（期望 2 = 视图名）" % levels[0]
    for a, b in zip(levels, levels[1:]):
        if b > a + 1:
            return False, "层级跳级 %d→%d" % (a, b)
    return True, "层级序列=%s" % (levels[:8],)


def struct_landmarks_ok(pairs):
    """地标：全部地标角色都要有可访问名，否则读屏只念「主区域」不念是干什么的。"""
    bad = ["%s<无名>" % r for r, n in pairs if not (n or "").strip()]
    return (not bad), ("%d 个地标，无名 %d 个%s"
                       % (len(pairs), len(bad), ("：" + ",".join(bad)) if bad else ""))


def struct_tables_ok(tables):
    """表格：自身有可访问名（念得出这是哪张表），列头全部有名字（念得出第几列是什么）。"""
    bad = []
    for i, t in enumerate(tables):
        cols = t.get("cols") or []
        if not (t.get("name") or "").strip():
            bad.append("表%d 无可访问名" % i)
        empty = [j for j, c in enumerate(cols) if not (c or "").strip()]
        if empty:
            bad.append("表%d 第%s列列头无名" % (i, ",".join(str(j + 1) for j in empty)))
        if len(cols) < 2:
            bad.append("表%d 只暴露 %d 个列头（表格语义没生效）" % (i, len(cols)))
    return (not bad), ("表格 %d 个，问题 %d%s"
                       % (len(tables), len(bad), ("：" + "；".join(bad[:4])) if bad else ""))


# D50（2026-10-03）：真浏览器基础设施故障重试，与 act_gate 共用 tools/flake_retry.py。
# flake_inject() 必须在**门体最前面**：CD_FLAKE_INJECT=always 时它让本门亚秒级假红，
# 用来证明 run_gate 包裹真的接上了（删掉包裹 → 裸 traceback + rc=1 → 自测判红）。
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from flake_retry import flake_inject, run_gate  # noqa: E402,F401


def _body(_ctx):
    flake_inject()
    del results[:]  # 重试时别把上一轮的判据累加进来
    from playwright.sync_api import sync_playwright
    # --html 覆盖：负控门要在**隔离副本**上注入缺陷再验，不能就地改活的 index.html。
    # 就地改的后果不是「测试不干净」，是**并发窗口内任何读者都看到注入的缺陷版** ——
    # 常驻 8790 实例、真浏览器门、act_gate 的真点按都读同一份文件，于是「门随机红」。
    # 默认值不变，老调用方零影响。
    html = HTML
    if "--html" in sys.argv:
        html = sys.argv[sys.argv.index("--html") + 1]
    # D60（2026-10-03）：--url 走**真 served 页面**。
    # 为什么必须多这一路：用户双击「启动 AgentOS.command」后看到的是
    # http://127.0.0.1:8790/，不是 file://。两者不是同一件事 ——
    # file:// 下 fetch/XHR 受 origin 限制、相对路径解析基准不同、
    # CSP 与 service worker 行为也不同。也就是说「file:// 全绿」
    # 与「用户实际打开的页面键盘/读屏是坏的」可以同时为真。
    # 只加参数不复制判据：16 门判据原样跑，变的只有导航目标，
    # 免得两套真相各自漂移（这正是 neg_a11y 注释里记的老病）。
    url = None
    if "--url" in sys.argv:
        url = sys.argv[sys.argv.index("--url") + 1]
    # D64（2026-10-03）：--out 让「谁跑的」和「证据写哪」解耦。
    # 为什么必须加：neg_a11y.py 把缺陷注入隔离副本后照样调本脚本，而本脚本
    # 原来把 report 硬写 work/a11y_walk.json —— 于是**故意打坏的那一次**
    # 会把真走查的证据覆盖掉。实测链跑完后盘上留着的是 neg-a11y 临时目录、
    # target=file:///var/folders/.../neg-a11y-*/index.html、19 门里带 FAIL 的
    # 变异体报告（2026-10-03 13:23 实测）。谁在链跑完后读这份证据，
    # 读到的都不是用户真正打开的那个页面的读屏结果。
    # 默认值不变，老调用方（serve.py 的按钮、a11y_served.py）零影响。
    out_path = os.path.join(ROOT, "work", "a11y_walk.json")
    if "--out" in sys.argv:
        out_path = sys.argv[sys.argv.index("--out") + 1]
    if url is None and not os.path.exists(html):
        print("FAIL: index.html 不存在")
        return 2
    target = url if url else ("file://" + html)
    report = {"tool": "a11y_walk.py", "method": "playwright-real-keyboard+cdp-ax-tree",
              "target": target,
              "mode": "served-http" if url else "file"}
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME)
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        pg.goto(target)
        # 2026-10-03: 固定 sleep(1200) 是 flake 温床 —— livecheck 里 D50 跑本门时
        # CPU 被前几十道门压着，页面 JS 偶尔没跑完就被读数（roving tabindex 全 None）。
        # 改成等**页面自己的就绪信号**（readyState complete + roving 已初始化 = 恰好
        # 1 个 tabindex=0）；等不到就退回原固定等待，让后面的判据照常判红（牙不丢）。
        try:
            pg.wait_for_function(READY_JS, timeout=8000)
        except Exception:  # noqa: BLE001
            pg.wait_for_timeout(1200)
        # (0) 可信输入前提门（2026-10-02 新增）。必须排在所有按键之前：
        #     它是后面每一门的前提，先按完键再回头问「刚才那些键可信吗」就晚了。
        pg.evaluate(TRUST_INIT_JS)
        for k in ("Tab", "Shift+Tab", "ArrowRight", "Escape", "Enter"):
            pg.keyboard.press(k)
            pg.wait_for_timeout(40)
        real_ev = pg.evaluate(TRUST_DRAIN_JS)
        pg.evaluate(SYNTH_JS)
        synth_ev = pg.evaluate(TRUST_DRAIN_JS)
        n_real = len(real_ev)
        n_real_trusted = sum(1 for e in real_ev if e["isTrusted"])
        n_synth_trusted = sum(1 for e in synth_ev if e["isTrusted"])
        report["trusted_input"] = {
            "real_events": n_real,
            "real_trusted": n_real_trusted,
            "synth_events": len(synth_ev),
            "synth_trusted": n_synth_trusted,
        }
        # 判据是「有事件 + 全可信 + 合成负控不被误信」三条同时成立。
        # 少任何一条都判红：n_real==0 是探针没装上（空壳），n_synth_trusted>0
        # 是探测器没牙（什么都信），两者都比「页面坏了」更该先修门。
        trust_ok = (n_real > 0) and (n_real_trusted == n_real) and (n_synth_trusted == 0)
        gate("按键确为浏览器可信输入（isTrusted，非 JS 合成）", trust_ok,
             "真按 %d/%d 事件 isTrusted=true · 合成负控 %d 个事件 isTrusted=false"
             % (n_real_trusted, n_real, len(synth_ev) - n_synth_trusted))
        # (1) tablist 方向键：ARIA APG 要求左右方向键在 tablist 内移动
        pg.evaluate("() => document.getElementById('t-over').focus()")
        before = pg.evaluate(ACTIVE_JS)
        moved = False
        # 2026-10-01 修：原实现「任一方向键能动」就算过，于是只杀掉 ArrowRight 时
        # ArrowLeft 仍能动 -> 门放行，反向门判它是空门。APG 要求双向可换页，故改为双向断言。
        moved_dir = {}
        for k in ("ArrowRight", "ArrowLeft"):
            pg.evaluate("() => document.getElementById('t-over').focus()")
            pg.wait_for_timeout(60)
            pre = pg.evaluate(ACTIVE_JS)["id"]
            pg.keyboard.press(k)
            pg.wait_for_timeout(90)
            moved_dir[k] = pg.evaluate(ACTIVE_JS)["id"] != pre
        moved = all(moved_dir.values())
        report["tablist_arrow"] = {"start": before["id"], "moved": moved,
                                   "per_dir": moved_dir}
        gate("tablist 双向方向键都能换页（ARIA APG 要求）", moved,
             "焦点 %s → 右=%s 左=%s"
             % (before["id"], moved_dir.get("ArrowRight"), moved_dir.get("ArrowLeft")))
        # (1b) roving tabindex：tablist 在 Tab 序列里只占一个停靠点（APG 硬要求）
        ti = pg.evaluate("""() => [...document.querySelectorAll('.nav button')]
            .map(b => ({id: b.id, ti: b.getAttribute('tabindex')}))""")
        roving = sum(1 for t in ti if t["ti"] == "0") == 1
        report["roving_tabindex"] = ti
        gate("tablist 用 roving tabindex（Tab 只停一次，组内交给方向键）", roving,
             " ".join("%s=%s" % (t["id"], t["ti"]) for t in ti)
             + " readyState=" + str(pg.evaluate("() => document.readyState")))
        # (3) 数字键 1..N 直达视图，且 aria-selected 同步
        digit_bad = []
        for i, key in enumerate(TAB_KEYS[:len(DIGIT_KEYS)], start=1):
            pg.keyboard.press(DIGIT_KEYS[i - 1])
            pg.wait_for_timeout(160)
            sel = pg.evaluate(SELECTED_JS)
            if sel != ["t-" + key]:
                digit_bad.append("键%s→期望t-%s 实得%s" % (DIGIT_KEYS[i - 1], key, sel))
        report["digit_switch"] = digit_bad
        # 键位与屏上 kbd 标签必须一致：门按 DIGIT_KEYS 按，标签是用户看到的契约。
        kbds = pg.evaluate("""() => [...document.querySelectorAll('.nav button')]
            .map(b => ({id: b.id, kbd: (b.querySelector('.kbd')||{}).textContent||''}))""")
        # 第 1 个 tab（总览）是默认视图，屏上**本就没有** kbd 数字标签，
        # 其余 tab 必须与实际键位逐字一致（用户看标签按键，不是看代码）。
        kb_expect = [""] + DIGIT_KEYS[1:]
        kb_bad = [x["id"] for x, k in zip(kbds, kb_expect) if x["kbd"] != k]
        # 尾部视图必须**没有**数字标签：给它们标 11/12/13 等于许诺一个按不出来的键。
        tail_bad = [x["id"] for x in kbds[len(DIGIT_KEYS):] if x["kbd"]]
        if kb_bad:
            digit_bad.append("屏上 kbd 标签与实际键位不符: %s" % "/".join(kb_bad))
        gate("数字键 1..%d 直达前 %d 视图且 aria-selected 同步（第 10 视图用 0）"
             % (len(DIGIT_KEYS), len(DIGIT_KEYS)),
             not digit_bad,
             "%d/%d 命中" % (len(DIGIT_KEYS), len(DIGIT_KEYS)) if not digit_bad
             else "; ".join(digit_bad))
        if DIGITLESS:
            gate("第 %d-%d 屏无数字键也不冒充（屏上无 kbd 标签，走 Tab/方向键/Cmd+K）"
                 % (len(DIGIT_KEYS) + 1, len(TAB_KEYS)),
                 not tail_bad,
                 "无标签的 tab=" + ",".join(x["id"] for x in kbds[len(DIGIT_KEYS):]))
        # (3b) R7 尾部屏没有数字键，键盘唯一通道就是 tablist 的方向键。把方向键从第 1 屏
        # 一条条按到底并逐个核对焦点 id —— 少走通一屏，那一屏就等于键盘不可达：上面那道
        # 数字键门只覆盖前 10 屏，尾部屏（term/file/set）正是靠这一条兜住。
        pg.evaluate("() => document.getElementById('t-over').focus()")
        pg.wait_for_timeout(60)
        seq = [pg.evaluate(ACTIVE_JS)["id"]]
        for _ in range(len(TAB_KEYS) - 1):
            pg.keyboard.press("ArrowRight")
            pg.wait_for_timeout(80)
            seq.append(pg.evaluate(ACTIVE_JS)["id"])
        want_seq = ["t-" + k for k in TAB_KEYS]
        seq_bad = ["%s!=%s" % (g, w) for g, w in zip(seq, want_seq) if g != w]
        report["tablist_arrow_seq"] = seq
        gate("方向键能从第 1 屏依次走到第 %d 屏（无数字键的 %s 靠这条可达）"
             % (len(TAB_KEYS), "/".join(DIGITLESS)),
             seq == want_seq,
             "焦点序=" + ">".join(seq) + ("  不一致=" + ";".join(seq_bad) if seq_bad else ""))
        # (4) 回车走通主流程：Tab 到视图 tab → Enter → 视图真切换
        pg.evaluate("() => document.getElementById('t-over').focus()")
        pg.keyboard.press("Enter")
        pg.wait_for_timeout(150)
        pg.keyboard.press("Tab")
        pg.wait_for_timeout(80)
        pg.keyboard.press("Enter")
        pg.wait_for_timeout(200)
        enter_sel = pg.evaluate(SELECTED_JS)
        gate("回车能走通主流程（Tab 落到控件 → Enter 生效）",
             len(enter_sel) == 1, "Enter 后选中态=%s" % (enter_sel or "无"))
        # (5) 命令面板：Cmd+K 开 → ArrowDown 选 → Enter 执行 → 再开 → Escape 关
        # 注意：⌘K 是 toggle，不是「开」。上一步 Enter 落在 ⌘K 按钮上时已经打开过一次，
        # 这里若不先归零，第二次 ⌘K 会把它关掉，于是读到 opened=False —— 那是量具状态脏了。
        pg.evaluate("() => window.__agentos && document.getElementById('cmd').classList.remove('on')")
        pg.evaluate("() => document.getElementById('cmdk').focus()")
        pg.keyboard.press("Meta+k")
        pg.wait_for_timeout(250)
        opened = pg.evaluate("() => document.getElementById('cmd').classList.contains('on')")
        pg.keyboard.press("ArrowDown")
        pg.wait_for_timeout(120)
        sel_i = pg.evaluate("() => { const b=document.querySelector('#cmdlist button.sel');"
                            " return b ? b.innerText.slice(0,20) : ''; }")
        pg.keyboard.press("Enter")
        pg.wait_for_timeout(250)
        closed_enter = not pg.evaluate("() => document.getElementById('cmd').classList.contains('on')")
        pg.keyboard.press("Meta+k")
        pg.wait_for_timeout(200)
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(200)
        closed_esc = not pg.evaluate("() => document.getElementById('cmd').classList.contains('on')")
        report["cmd_palette"] = {"opened": opened, "arrow_sel": sel_i,
                                 "enter_closes": closed_enter, "escape_closes": closed_esc}
        gate("命令面板 Cmd+K → 方向键 → Enter 执行",
             opened and bool(sel_i) and closed_enter,
             "打开=%s 方向键选中=%r Enter后关闭=%s" % (opened, sel_i, closed_enter))
        gate("命令面板 Escape 可关闭（无键盘陷阱）", closed_esc,
             "Escape 关闭=%s" % closed_esc)
        # (5b) 2026-10-01 真机（in-app browser，非 headless）走查补的 5 条。
        # 原先 10 条门全绿却漏了这 5 条：它们只断言「面板开/关、方向键有高亮」，
        # 从不断言「焦点落在哪」。真实浏览器里这 5 条逐条变红。
        # 教训：门必须断言焦点位置，只断言状态变化会漏掉整类可用性缺陷。
        pg.evaluate("() => document.getElementById('cmd').classList.remove('on')")
        pg.evaluate("() => document.getElementById('cmdk').focus()")
        opener = pg.evaluate("() => document.activeElement ? document.activeElement.id : ''")
        pg.keyboard.press("Meta+k")
        pg.wait_for_timeout(250)
        focus_in = pg.evaluate("() => document.activeElement ? document.activeElement.id : ''")
        hi0 = pg.evaluate("() => { const b=document.querySelector('#cmdlist button.sel');"
                          " return b ? b.getAttribute('aria-selected') : ''; }")
        ad0 = pg.evaluate("() => { const q=document.getElementById('cmdq');"
                          " const b=document.querySelector('#cmdlist button.sel');"
                          " const t = b ? b.id : '';"
                          " return q.getAttribute('aria-activedescendant') === t ? 'match' : 'mismatch'; }")
        gate("命令面板打开后焦点落在搜索框（键盘用户能直接打字）",
             focus_in == "cmdq", "打开后焦点 id=%r（期望 cmdq）" % focus_in)
        gate("命令面板打开即有高亮项（不用先按一次方向键）",
             hi0 == "true" and ad0 == "match",
             "首项 aria-selected=%r activedescendant=%s" % (hi0, ad0))
        before_n = pg.evaluate("() => document.querySelectorAll('#cmdlist button').length")
        pg.keyboard.type("\u5f15\u64ce", delay=30)
        pg.wait_for_timeout(250)
        typed = pg.evaluate("() => document.getElementById('cmdq').value")
        after_n = pg.evaluate("() => document.querySelectorAll('#cmdlist button').length")
        gate("命令面板可直接打字收窄候选（焦点在输入框的实效）",
             bool(typed) and after_n < before_n,
             "输入=%r 候选 %d->%d" % (typed, before_n, after_n))
        pg.keyboard.press("Tab")
        pg.wait_for_timeout(120)
        trapped = pg.evaluate("() => document.getElementById('cmd').contains(document.activeElement)")
        gate("模态对话框内 Tab 不逃逸到背景内容（aria-modal 必配焦点陷阱）",
             trapped, "Tab 后焦点仍在面板内=%s" % trapped)
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(250)
        restored = pg.evaluate("() => document.activeElement ? document.activeElement.id : ''")
        gate("命令面板关闭后焦点归还触发元素（不把人丢回 body）",
             restored == opener and bool(opener),
             "触发元素=%r 关闭后焦点=%r" % (opener, restored))
        report["cmd_palette_focus"] = {"opener": opener, "focus_in": focus_in,
                                       "first_highlight": hi0, "activedescendant": ad0,
                                       "typed": typed, "candidates": [before_n, after_n],
                                       "tab_trapped": trapped, "focus_restored": restored}
        # (6) 读屏视角，逐视图采 AX 树
        # 为什么要逐视图：非活动 tabpanel 是 display:none（这是对的，不是缺陷），
        # 只在总览采一次会漏掉另外 4 个视图的全部控件 —— 实测同一页 DOM 有 67 个 button，
        # 只采总览时 AX 树只暴露 5 个，覆盖率被低估 6 倍。aria 覆盖必须是逐视图真读出来的。
        # 新版 playwright 移除了 page.accessibility，改走 CDP Accessibility.getFullAXTree
        # —— 这才是 Chrome 自己给读屏软件（VoiceOver/NVDA）算的那棵树，不是我们自己解析 DOM。
        cdp = pg.context.new_cdp_session(pg)
        cdp.send("Accessibility.enable")
        INTERACTIVE = {"button", "link", "textbox", "combobox", "listbox",
                       "checkbox", "radio", "slider", "switch", "menuitem", "tab"}
        ax_by_view, nameless_all, thin_views, tab_by_view = {}, [], [], {}
        heads_by_view, marks, tables = {}, [], []

        def ax_roles(nodes, by_role, nameless):
            for n in nodes or []:
                role = ((n.get("role") or {}).get("value")) or ""
                if role in INTERACTIVE:
                    by_role[role] = by_role.get(role, 0) + 1
                    if not (((n.get("name") or {}).get("value")) or "").strip():
                        nameless.append(role)
                ax_roles(n.get("childNodes"), by_role, nameless)

        def ax_level(n):
            for p in n.get("properties") or []:
                if p.get("name") == "level":
                    return ((p.get("value") or {}).get("value"))
            return None

        def ax_text(nodes, want):
            """抽 AX 子树里某 role 的全部节点名（列头/地标/标题共用一条路径）。"""
            out = []
            for n in nodes or []:
                if ((n.get("role") or {}).get("value")) == want:
                    out.append((((n.get("name") or {}).get("value")) or "").strip())
                out += ax_text(n.get("childNodes"), want)
            return out

        for vk in TAB_KEYS:
            pg.evaluate("k => view(k)", vk)
            pg.wait_for_timeout(300)
            full = cdp.send("Accessibility.getFullAXTree")
            by_role, nameless = {}, []
            ax_roles(full.get("nodes"), by_role, nameless)
            n_int = sum(by_role.values())
            ax_by_view[vk] = {"ax_nodes": len(full.get("nodes") or []),
                              "interactive": n_int, "by_role": by_role,
                              "nameless": nameless[:10]}
            nodes = full.get("nodes") or []
            heads_by_view[vk] = [ax_level(n) for n in nodes
                                 if ((n.get("role") or {}).get("value")) == "heading"
                                 and ax_level(n) is not None]
            marks = [(r, (ax_text(nodes, r) or [""])[0])
                     for r in LANDMARK_ROLES if ax_text(nodes, r)]
            tnames = ax_text(nodes, "table")
            if tnames:
                tables.append({"name": tnames[0],
                               "cols": ax_text(nodes, "columnheader")})
            if nameless:
                nameless_all.append("%s(%d)" % (vk, len(nameless)))
            if n_int < 5:
                thin_views.append("%s=%d" % (vk, n_int))
            # 该视图内的 Tab 链：先给可见可聚焦元素打序号，再从第一个开始连按 Tab，
            # 必须按序号顺序走完全部，且中途不出现不可见落点。
            # 两个实测坑：
            #  (1) document.body.focus() 不重置 Chromium 的顺序焦点导航起点，Tab 会从上次位置续走
            #      —— 实测 mx 视图 68 个可见可聚焦元素，从 body 起步只采到 3 个。
            #  (2) 走到最后一个元素后再按一次 Tab 会进入浏览器 chrome（地址栏），
            #      document.activeElement 变 BODY —— 这是正常环绕，不是「卡死」。
            stamps = pg.evaluate(STAMP_JS)
            n_live = len(stamps)
            # 只走 n_live-1 步，全部落在页面内。再多按一次会走出最后一个元素、
            # 焦点进浏览器 chrome（activeElement 变 BODY）或落到枚举集合之外 ——
            # 那是浏览器环绕，不是页面的可达性，故不纳入判据。
            n_steps = max(0, min(n_live, 24) - 1)
            if n_live:
                pg.evaluate("() => document.querySelector('[data-a11y-i=\"0\"]').focus()")
            ch = []
            for _ in range(n_steps):
                pg.keyboard.press("Tab")
                pg.wait_for_timeout(40)
                ch.append(pg.evaluate(CUR_JS))
            order = [c["i"] for c in ch]
            expect = list(range(1, n_live))[:n_steps]
            tab_by_view[vk] = {
                "live_focusable": n_live,
                "distinct": len(set(order)),
                "ordered_ok": order == expect,
                "body_stuck": sum(1 for c in ch if c["tag"] == "BODY"),
                "invisible": sum(1 for c in ch if not c["visible"]),
                "order": order, "expect": expect,
            }

        report["ax_by_view"] = ax_by_view
        report["tab_by_view"] = tab_by_view
        gate("读屏视角：逐视图每个可交互节点都有可访问名", not nameless_all,
             "%d 视图共 %d 个可交互节点，无名 %d 个"
             % (len(TAB_KEYS), sum(v["interactive"] for v in ax_by_view.values()),
                len(nameless_all))
             + ("  无名视图=" + ",".join(nameless_all) if nameless_all else ""))
        gate("读屏视角：%d 个视图的控件都真的进了无障碍树（无空壳视图）" % len(TAB_KEYS),
             not thin_views,
             " ".join("%s=%d" % (k, v["interactive"]) for k, v in ax_by_view.items()))
        stuck_v = ["%s(不可见%d)" % (k, v["invisible"])
                   for k, v in tab_by_view.items() if v["invisible"]]
        # 门判据：Tab 序号必须严格递增覆盖全部可见可聚焦元素，且顺序正确。
        # 不再用「命中不同元素数」当判据 —— 该数只反映采样步数，不反映可达性。
        stuck_v += ["%s(序乱 %s≠%s)" % (k, v["order"], v["expect"])
                    for k, v in tab_by_view.items() if not v["ordered_ok"]]
        reach = ["%s=%d/%d" % (k, v["distinct"], v["live_focusable"])
                 for k, v in tab_by_view.items()]
        gate("逐视图 Tab 链可达（按序走完所有可见可聚焦元素、无不可见落点）",
             not stuck_v,
             "命中/可见可聚焦 " + " ".join(reach)
             + ("  问题=" + ",".join(stuck_v) if stuck_v else ""))
        # (7) 读屏视角：存在 live region 播报状态变化
        live = pg.evaluate("""() => {
          const out = [];
          for (const e of document.querySelectorAll('[aria-live]'))
            out.push({id: e.id || e.tagName, live: e.getAttribute('aria-live')});
          return out;
        }""")
        report["live_regions"] = live
        gate("存在 aria-live 区域供读屏播报状态变化", len(live) > 0,
             "%d 个：%s" % (len(live), " ".join("%s(%s)" % (x["id"], x["live"]) for x in live[:4])))
        # (8) 读屏结构门 D50：地标与标题 —— rotor 的第二条导航通道
        report["headings_by_view"] = heads_by_view
        report["landmarks"] = marks
        report["tables_ax"] = tables
        hbad = []
        for vk in TAB_KEYS:
            ok, why = struct_headings_ok(heads_by_view.get(vk) or [])
            if not ok:
                hbad.append("%s:%s" % (vk, why))
        gate("读屏结构：逐视图标题层级不跳级且以视图名为首（H2 起）", not hbad,
             " ".join("%s=%s" % (k, v[:6]) for k, v in heads_by_view.items())
             + ("  问题=" + ",".join(hbad) if hbad else ""))
        ok, why = struct_landmarks_ok(marks)
        gate("读屏结构：全部地标都有可访问名（rotor 念得出是干什么的）", ok, why)
        ok, why = struct_tables_ok(tables)
        gate("读屏结构：表格有可访问名且列头全部进 AX 树", ok, why)
        pg.close()
        b.close()
    _od = os.path.dirname(os.path.abspath(out_path))
    if _od and not os.path.isdir(_od):
        os.makedirs(_od, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(dict(report, gates=results), f, ensure_ascii=False, indent=1)
    nfail = sum(1 for r in results if not r["ok"])
    print("== A11Y_WALK %s  %d/%d 门通过（真键盘事件 + 真 AX 树）=="
          % ("OK" if nfail == 0 else "FAIL", len(results) - nfail, len(results)))
    return nfail == 0, ("%d/%d 门通过" % (len(results) - nfail, len(results)))


def main():
    """唯一入口。判据红 = 结论，一次都不重试；基础设施故障才重试（D40 铁律）。"""
    ok, detail = run_gate(_body, None)
    if detail.startswith("EXC "):
        print("  [EXC] " + detail)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
