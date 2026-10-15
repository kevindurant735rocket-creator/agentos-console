#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真渲染无障碍门 D66：把「机检 aria」升级为「浏览器真渲染后实测」。

已有 a11y_walk.py 的 19 道判据全是结构/语义层（可访问名、Tab 链、地标、
标题层级、AX 树里有这个控件）。读屏与键盘用户真正在意的下面四层，此前全仓
零判据：
  1) 焦点可见性 WCAG 2.4.7 —— a11y_walk 只验「能不能 Tab 到」，从不验
     「聚焦后有没有人眼可见的指示」。注入 :focus{outline:none} 它照样全绿。
  2) 真渲染对比度 WCAG 1.4.3 —— ux_audit 那道门是**静态解析 <style> 里的
     CSS 变量对**（--tx vs --bg），不是渲染后实测。任何元素级 color/background
     覆盖、opacity、渐变叠加都能绕开它。
  3) prefers-reduced-motion WCAG 2.3.3 —— 前庭敏感用户开「减弱动态效果」后
     页面仍无限循环动画就是缺陷。全仓零判据。
  4) 200% 缩放 / 窄视口回流 WCAG 1.4.10 —— 横向滚动 + overflow:hidden 裁字。

第五件事不是判据而是诚实的边界声明：真读屏（macOS AX / VoiceOver）需要辅助
功能权限，本机 TCC 未授予 —— 实测 System Events 的 AppleScript 返回 rc=0 但
count of UI elements of window 1 = 0、AXFocusedApplication 报 -1728。这是典型
的**静默空**：naive 脚本会把「读不到」当成「树是空的」。本门把 AT 状态显式探明
并打印 AT-VERIFIED / AT-UNVERIFIED，不可能再被误读成「读屏也验过了」。

产出 work/a11y_human.json，rc=0 全过 / 2 有红项。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
HTML = os.path.join(ROOT, "index.html")
REPORT = os.path.join(ROOT, "work", "a11y_human.json")
VIEWS = ["总览", "轮次", "阶段矩阵", "证据账本", "引擎", "Agent 台", "目标台",
         "复盘台", "能力地图", "工作区", "终端", "文件", "设置"]
# R7（2026-10-10）与 VIEWS 一一对应的 tab 键序。第 11-13 屏没有数字键，
# _goto_view 必须用真鼠标点 .nav 上的 tab 把它们切出来 —— 下面 _digit_key
# 对 idx>=10 直接抛错，就是逼着每次新增视图的人在这里补一行、并想清楚
# 「这一屏用户到底怎么进去」。
VIEW_KEYS = ["over", "round", "mx", "led", "eng", "work", "goal", "retro",
             "cap", "ws", "term", "file", "set"]
assert len(VIEW_KEYS) == len(VIEWS), "VIEW_KEYS 与 VIEWS 必须逐屏对应"
# D95：这行曾经只列 8 项，漏掉 D88 才加进来的「工作区」。漏的真实后果不是数字
# 好看 —— 是**那一屏的键盘走查、200% 重排、320px 重排、焦点可见性全都没测**，
# 而工作区恰好是唯一一面装着真终端输入框（wsq）与真文件树的屏。按数字键 1..N
# 遍历，所以补进来后它是被真走到的第 9 屏，不是多写一个字符串。
_PW_PY = os.path.join(ROOT, ".venv-pw", "bin", "python")


def _reexec_with_playwright():
    """v118 老账：同一个门脚本三种跑法三种结果（缺 playwright 时整门假红）。
    引擎的「执行面接线」走的正是 python3 tools/xxx.py 这一种。"""
    try:
        import playwright  # noqa: F401
        return
    except Exception:
        pass
    if os.environ.get("AGENTOS_GATE_REEXEC") == "1":
        return
    if os.path.isfile(_PW_PY):
        os.environ["AGENTOS_GATE_REEXEC"] = "1"
        os.execv(_PW_PY, [_PW_PY, os.path.abspath(__file__)] + sys.argv[1:])


# ── 页面内量测（真渲染后取值，不是静态解析 CSS 文本） ──────────────────────

CONTRAST_JS = r"""() => {
const L=([r,g,b])=>{const f=v=>{v/=255;return v<=.03928?v/12.92:Math.pow((v+.055)/1.055,2.4)};
return .2126*f(r)+.7152*f(g)+.0722*f(b)};
const P=s=>{const m=String(s).match(/rgba?\(([^)]+)\)/);if(!m)return null;
const p=m[1].split(/[,\/]/).map(parseFloat);
return{r:p[0],g:p[1],b:p[2],a:p.length>3&&!isNaN(p[3])?p[3]:1}};
const O=(f,b)=>{const a=f.a;return{r:f.r*a+b.r*(1-a),g:f.g*a+b.g*(1-a),b:f.b*a+b.b*(1-a),a:1}};
const R=(a,b)=>{const x=L([a.r,a.g,a.b]),y=L([b.r,b.g,b.b]);
return(Math.max(x,y)+.05)/(Math.min(x,y)+.05)};
const W={r:255,g:255,b:255,a:1},out=[];
for(const el of document.querySelectorAll('body *')){
const d=Array.from(el.childNodes).filter(n=>n.nodeType===3)
.map(n=>n.textContent).join('').trim();
if(!d)continue;
const cs=getComputedStyle(el);
if(cs.visibility==='hidden'||cs.display==='none'||parseFloat(cs.opacity)===0)continue;
const r=el.getBoundingClientRect();
if(r.width<1||r.height<1)continue;
const fg=P(cs.color);
if(!fg)continue;
const st=[];let n=el;
while(n&&n.nodeType===1){const c=P(getComputedStyle(n).backgroundColor);
if(c&&c.a>0){st.push(c);if(c.a===1)break}n=n.parentElement}
let bg=W;
for(let i=st.length-1;i>=0;i--)bg=O(st[i],bg);
const e=O(fg,bg),fs=parseFloat(cs.fontSize),fw=parseInt(cs.fontWeight,10)||400;
const lg=fs>=24||(fs>=18.66&&fw>=700),nd=lg?3:4.5,cr=R(e,bg);
out.push({t:d.slice(0,24),cr:Math.round(cr*100)/100,need:nd,ok:cr>=nd})}
return out}"""

FOCUSABLE_JS = r"""() => {
const sel='a[href],button,input,select,textarea,[tabindex],'
+'[role="button"],[role="tab"],[role="option"]';
function P(e){if(e.id)return '#'+e.id;
const parts=[];let n=e,d=0;
while(n&&n.nodeType===1&&d<4){let s=n.tagName.toLowerCase();
if(n.className&&typeof n.className==='string')
s+='.'+n.className.trim().split(/\s+/).slice(0,2).join('.');
parts.unshift(s);n=n.parentElement;d++}
return parts.join(' > ')}
return Array.from(document.querySelectorAll(sel)).filter(e=>{
const cs=getComputedStyle(e);
if(cs.display==='none'||cs.visibility==='hidden')return false;
if(e.tabIndex<0)return false;
const r=e.getBoundingClientRect();return r.width>0&&r.height>0})
.map((e,i)=>({i:i,sel:P(e),tag:e.tagName.toLowerCase(),
label:(e.getAttribute('aria-label')||e.textContent||'').trim().slice(0,20)}))}"""

STYLE_JS = r"""(sel)=>{const e=document.querySelector(sel);
if(!e)return null;const cs=getComputedStyle(e);
return{outlineStyle:cs.outlineStyle,outlineWidth:cs.outlineWidth,
outlineColor:cs.outlineColor,boxShadow:cs.boxShadow,
backgroundColor:cs.backgroundColor,borderColor:cs.borderColor,
borderTopWidth:cs.borderTopWidth,color:cs.color,
textDecorationLine:cs.textDecorationLine}}"""

BLUR_JS = r"(sel)=>{const e=document.querySelector(sel);if(e&&e.blur)e.blur()}"
FOCUS_JS = r"(sel)=>{const e=document.querySelector(sel);if(e&&e.focus)e.focus()}"

ANIM_JS = r"""()=>{
const reduced=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const anims=(document.getAnimations&&document.getAnimations())||[];
const running=anims.filter(a=>{try{return a.playState==='running'}catch(e){return false}});
return{reduced:reduced,total:anims.length,running:running.length,
kinds:running.slice(0,6).map(a=>(a.constructor&&a.constructor.name)||'?')}}"""

REFLOW_JS = r"""()=>{
const de=document.documentElement;
const hOverflow=de.scrollWidth-de.clientWidth;
const cut=[];
for(const el of document.querySelectorAll('body *')){
const cs=getComputedStyle(el);
if(cs.overflowX!=='hidden'&&cs.overflowY!=='hidden')continue;
if(cs.textOverflow==='ellipsis')continue;
/* sr-only 是「故意藏起来给读屏」的官方写法：1px + clip 0 矩形。
   它必然 scrollWidth>>clientWidth，计进来就是**误报**——全站每个表格
   caption 都用它。但放行条件必须窄到无法被当成万能豁免：
   仅当 position:absolute 且 宽高都 ≤2px 且 clip 是零矩形 才跳过。
   真正的裁字缺陷（正文被 overflow:hidden 切掉）不满足任何一条。 */
const zeroClip=(cs.clip==='rect(0px, 0px, 0px, 0px)'||cs.clip==='rect(0, 0, 0, 0)')
||cs.clipPath==='inset(50%)';
if(cs.position==='absolute'&&el.clientWidth<=2&&el.clientHeight<=2&&zeroClip)continue;
const dx=el.scrollWidth-el.clientWidth,dy=el.scrollHeight-el.clientHeight;
if(el.clientWidth>0&&(dx>2||dy>2)){
const d=Array.from(el.childNodes).filter(n=>n.nodeType===3)
.map(n=>n.textContent).join('').trim();
cut.push({t:d.slice(0,20)||el.tagName.toLowerCase(),dx:dx,dy:dy,
sel:el.tagName.toLowerCase()+(el.className?'.'+String(el.className).split(/\s+/)[0]:'')})}}
return{hOverflow:hOverflow,cutCount:cut.length,cut:cut.slice(0,5),
cw:de.clientWidth,sw:de.scrollWidth}}"""


def _px(v):
    try:
        return float(str(v).replace("px", "").strip() or 0)
    except Exception:
        return 0.0


def _alpha(color):
    try:
        parts = str(color).split(",")
        return float(parts[3]) if len(parts) >= 4 else 1.0
    except Exception:
        return 1.0


def _visible_indicator(before, after):
    """聚焦后是否产生了**人眼可见**的变化。逐项列证据，不靠猜。
    这是本门与 a11y_walk 的分界：那边只问「焦点在不在」，这边问「看不看得见」。"""
    if before is None or after is None:
        return [], "元素在聚焦前后拿不到样式"
    why = []
    if after["outlineStyle"] != "none" and _px(after["outlineWidth"]) >= 1 \
            and _alpha(after["outlineColor"]) > 0.05:
        why.append("outline %s %s" % (after["outlineStyle"], after["outlineWidth"]))
    if after["boxShadow"] not in ("none", "") and after["boxShadow"] != before["boxShadow"]:
        why.append("box-shadow")
    if after["backgroundColor"] != before["backgroundColor"]:
        why.append("背景色变化")
    if after["borderColor"] != before["borderColor"] and _px(after["borderTopWidth"]) > 0:
        why.append("边框色变化")
    if after["color"] != before["color"] \
            and after["textDecorationLine"] != before["textDecorationLine"]:
        why.append("文字色+装饰线")
    return why, "; ".join(why) if why else "聚焦前后计算样式无可见差异"


def focus_visible_ok(measured):
    """measured: [{sel, why:[...], detail}] -> (ok, detail)
    纯函数：变异体直接拿它当判据对象，不碰浏览器。"""
    total = len(measured)
    bad = [m for m in measured if not m["why"]]
    if total == 0:
        return False, "0 个可聚焦控件被测（门自己瞎了，fail-closed）"
    if bad:
        return False, ("%d/%d 无可见焦点指示 · 例：%s" % (
            len(bad), total,
            "; ".join("%s(%s)" % (b["sel"], b["label"] or b["tag"]) for b in bad[:3])))
    return True, "%d/%d 控件聚焦后均有可见指示 · 例：%s" % (
        total, total, measured[0]["detail"])


def rendered_contrast_ok(samples):
    """samples: CONTRAST_JS 的返回 -> (ok, detail)
    阈值按 WCAG：普通文字 4.5、大字（>=24px 或 >=18.66px 粗）3.0。"""
    if not samples:
        return False, "0 个带文字节点被测（门自己瞎了，fail-closed）"
    bad = [s for s in samples if not s["ok"]]
    worst = min(samples, key=lambda s: s["cr"])
    detail = "实测 %d 个文字节点 最低 %.2f（需 %.1f）· 样本 %r" % (
        len(samples), worst["cr"], worst["need"], worst["t"])
    if bad:
        return False, "%d/%d 低于阈值（最低 %s=%.2f < %.1f）· 样本 %r" % (
            len(bad), len(samples), worst["t"], worst["cr"], worst["need"], worst["t"])
    return True, detail


def reduced_motion_ok(info):
    if not info.get("reduced"):
        return False, "prefers-reduced-motion 未生效（探测本身失败，fail-closed）"
    if info.get("running", 0) > 0:
        return False, ("减弱动态效果开启后仍有 %d 个动画在跑（%s）· WCAG 2.3.3"
                       % (info["running"], ",".join(info.get("kinds") or []) or "?"))
    return True, "reduce 已生效且 0 个运行中动画（共 %d 个动画对象）" % info.get("total", 0)


def reflow_ok(info):
    ho = info.get("hOverflow", 0)
    cut = info.get("cutCount", 0)
    if ho > 2:
        return False, "文档级横向溢出 %dpx（cw=%d sw=%d）· WCAG 1.4.10" % (
            ho, info.get("cw", 0), info.get("sw", 0))
    if cut > 0:
        return False, "%d 处文字被 overflow:hidden 裁掉（无省略号降级）· 例 %s" % (
            cut, info.get("cut"))
    return True, "无横向溢出、无静默裁字（cw=%d）" % info.get("cw", 0)
    return True, "无横向溢出、无静默裁字（cw=%d）" % info.get("cw", 0)


# ── 真读屏（macOS AX）能力探针 ────────────────────────────────────────────
# 这段不是判据，是**诚实的边界声明**。它要做到两件事：
#   1) 真的去问 macOS 辅助功能 API（不是「我假定读屏能读」）；
#   2) 把「读不到」与「树是空的」严格分开 —— AppleScript 在 TCC 未授权时
#      返回 rc=0 但结果为空，naive 检查会把这当成「无障碍树为空」。
# 本门把它单独标成 AT-UNVERIFIED，绝不让它混进绿门计数。

AT_STATUS = {"verified": False, "token": "AT-UNVERIFIED", "reason": ""}
_BROWSER_TOKENS = ("safari", "chrome", "chromium", "firefox", "edge", "arc",
                   "brave", "opera", "vivaldi")


def _osa(script):
    # 反向门专用后门：让 neg_a11y_human.py 在**不依赖本机 TCC 授权**的前提下
    # 也能复现「rc=0 但计数=0」的静默空场景。安全边界写死：它只会返回
    # 「空」，即永远只能逼出 AT-UNVERIFIED，**不可能**伪造 AT-VERIFIED，
    # 并且 detail 里带 FAKE 字样，任何人读证据位都不会误当成真读屏通过。
    if os.environ.get("AGENTOS_AT_FAKE_EMPTY") == "1":
        return 0, "", "0"
    try:
        p = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=20)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as e:
        return -1, "", str(e)


_OSA_VISIBLE = ('tell application "System Events" to get name of every '
                'process whose background only is false')
_OSA_WINDOWS = ('on run argv' + '\n'
                '  set out to ""' + '\n'
                '  tell application "System Events"' + '\n'
                '    repeat with pr in (every process whose name is (item 1 of argv))' + '\n'
                '      repeat with w in (every window of pr)' + '\n'
                '        set out to out & (name of w) & " || "' + '\n'
                '      end repeat' + '\n'
                '    end repeat' + '\n'
                '  end tell' + '\n'
                '  return out' + '\n'
                'end run')
_OSA_ACTIVATE = ('on run argv' + '\n'
                 '  tell application "System Events" to set frontmost of '
                 '(first process whose name is (item 1 of argv)) to true' + '\n'
                 'end run')

# ── D84：AX 层能力预探针 ──────────────────────────────────────────────────
# 实测（2026-10-04，本机）：System Events 的进程枚举是好的（能列出 11 个可见
# 进程），但所有窗口化进程的 every window 一律返回空，且
# value of attribute "AXFocusedApplication" 报 -1728。
# 这不是「用户没打开被测页面」，而是 macOS TCC 辅助功能权限未授予时
# AX 读到的典型静默空（rc=0 + 空结果）。
# 原实现只看「浏览器窗口标题有没有命中 <title>」，把这两件完全不同的事混成
# 一条消息，输出的补救指引（去把页面打开）在这个状态下无效 —— 实测开着
# 页面窗口数照样是 0。拿着这条消息去修的人会一直修不到点上。
# 所以先问「AX 层本身能不能读」，再问「页面在不在前台」，两者分开报。
# ── D94 实测纠偏：osascript -1728 不是 TCC，Finder 0 窗也不是 TCC 证据 ──
# 旧实现问 System Events 两件事并据此判「AX 层死了 / TCC 辅助功能没开」：
#   ① AXFocusedApplication 解析不出来 → 判 TCC；② 对照进程 0 窗口 → 判 TCC。
# 2026-10-05 在本机把这两条都实测推翻了，证据如下（rc 全部读出）：
#   1) PyObjC ApplicationServices.AXIsProcessTrusted() → **True**（rc=0）
#      ⇒ TCC 辅助功能权限本来就是好的，**不需要**用户再去系统设置里开任何东西。
#      旧文案让人「去给 Terminal/iTerm 开辅助功能」是把人引去改一个已经对的开关，
#      而且门实际跑在宿主 App（ChatGPT.app）里，点名终端本就是错的。
#   2) osascript ... attribute "AXFocusedApplication" → rc=1 **-1728**，
#      即 errAEEventFailed，属 **Apple Events / 自动化** 通道，与 TCC 无关。
#      把 Apple Events 错误读成 TCC，是把两个不同 subsystem 的错误码混成一个。
#   3) 「对照进程 Finder 0 窗口」不成立：实测 Finder count of windows = 0，
#      这是 Finder 自己的事实（它就是 0 窗），与权限无关。拿它当 AX 层死的
#      证据 ⇒ 一个本来正常的机器会被误报成权限问题。
# 所以判「AX 层活着」改成读一棵**真实 AX 树**（PyObjC，节点数 + 角色多样性）。
# 而 Chromium 侧读不到 web content 的 AX 树是**平台事实**，不是缺陷：
# macOS 只对辅助技术（AT）客户端暴露 Chromium 的 web content AX 子树，
# 非 AT 进程读到的是 AXApplication 自链（实测 31 节点、角色全同）。
# ctypes 直调 AX 符号会 segfault（实测 rc=139），**禁用**；
# 顶层 `import Quartz` 不导出 AX 函数，必须 `import ApplicationServices`。
# ── D105 实测更正（2026-10-05，rc=0）：上面三条 Chromium 结论对交付物已失效 ──
# 1) 「AXWebArea=0 / 只读到 AXApplication 自链」是**量具**坏，不是产品坏：
#    旧量具无环检测、无深度上限，遍历 AXMenuBar 菜单把 600 节点预算吃光；
#    且采样在 App 启动后 7s 内，窗口尚未注册 kAXWindows。修正版（环检测 +
#    深度上限 40 + 菜单不进递归）实测：AXWebArea=1、原生树 nodes=305
#    cycles=0 texts=160，文本含「项目与任务空间」「图例」，并在
#    native_gate 里由**外部进程**持续守着（ax-window-exposed / ax-content-tree）。
# 2) 「AppleScript 恒空」不准确：实测 _OSA_WINDOWS 对 AgentOS 返回 'AgentOS ||'，
#    Chrome 返回 '' 是因为它在后台（后台 app 本就 0 窗）——「0 窗口」不能
#    推断「页面没开」。
# 3) 交付物是**原生 AgentOS.app**，不是浏览器页面 ⇒ 任何
#    「可见浏览器里有没有标题含 <title> 的窗口」的分支对真实产品永远为假，
#    拿它当归因就是把用户引去修一个不存在的问题（D105 已删该分支）。
_AX_CONTROL_PROCS = ("Finder",)   # 只作对照，不作判据（Finder 本就 0 窗）
_AX_CONTROL_PID = "Finder"

# D105 实测重定标的遍历预算与判据（D105-实测9，2026-10-05，rc=0）：
#   AgentOS.app  nodes=305 cycles=0 menu=0 content=305 texts=160 AXWebArea=1
#   Finder       nodes=145 cycles=3 menu=1 content=144 texts=35  AXWebArea=0
# 旧预算 600 是按「无环检测、无菜单分离」那版定的，而那版遍历 AXMenuBar 的
# 菜单项就把预算吃光（实测 nodes=600 而角色只有 AXApplication/AXGroup/
# AXMenu/AXMenuBar），于是「树很大」被当成了「有内容」。现在把菜单子树
# 单独计数并**不进**递归，阈值按上表实测定。
_AX_TREE_MAX_NODES = 20000        # 有界遍历，避免失控树把门拖死
_AX_TREE_MAX_DEPTH = 40
_AX_MENU_ROLES = ("AXMenuBar", "AXMenu", "AXMenuItem")
_AX_MIN_CONTENT_NODES = 8         # 实测最少的一棵（Finder）也有 144
_AX_MIN_TEXTS = 3                 # 实测最少的也有 35
_AX_DELIVERABLE = "AgentOS"       # 交付物自己，不是浏览器

AT_BLOCK = {"blocked": None, "fix": ""}
# 拿不到 OS 层读屏时的补救。**不再**让用户去开辅助功能权限——它已经开着
# （AXIsProcessTrusted=True 实测）。真正的卡点是：AT 权限按进程授予，而本门
# 进程不是 AT 客户端。VoiceOver 本机**已安装**（旧注释写「本机无 VoiceOver」
# 是错的，D94 已实测推翻），所以人工验证可行、只是没法自动断。
AT_FIX = ("OS 层读屏在本机无法自动验证，原因是平台限制而非本门缺陷："
          "辅助功能权限按**进程**授予，本门进程不是 AT 客户端，所以拿不到"
          "VoiceOver 的实际焦点序列（旧文案说的是 Chromium web 子树，"
          "D105 已实测推翻：AgentOS.app 原生树的 AXWebArea=1、内容节点 305、"
          "文本 160，AX 层对交付物本身是**可读**的）。"
          "本机 **VoiceOver 已安装**（/System/Library/CoreServices/VoiceOver.app，"
          "open rc=0 可拉起），所以人工验证是可行的。"
          "辅助功能权限本身是好的（AXIsProcessTrusted=True）。"
          "要真确认请：① 按 Cmd+F5 打开 VoiceOver；"
          "② 启动 AgentOS.app 并置于前台；③ 人工用 VO 键（Ctrl+F3）"
          "逐控件走一遍焦点顺序与朗读；"
          "④ 系统设置→键盘→「全键盘访问」设为「所有控件」后重跑"
          "kbd（.venv-pw/bin/python tools/a11y_walk.py --url <地址>），"
          "确认 Tab 能走通全部控件（全键盘访问关着时 Tab 只停"
          "文本框与列表，漏掉按钮是系统行为不是页面缺陷）。"
          "AX 树物证由 native_gate 外部进程"
          "侧的两条判据（ax-window-exposed / ax-content-tree）持续守着。"
          "门不会为此虚报 AT-VERIFIED。")
# 「AX 层可读、只缺焦点序列」分支的补救：与 AT_FIX 分开，因为 neg_a11y_human
# 的 M6/M7 变异体要求可读分支**不得**出现 TCC/权限字样（AX 都读到了还说
# 权限问题就是误导）。措辞按实测事实走，不去迎合关键词。
AT_FIX_FOCUS = ("要真确认请：① 按 Cmd+F5 打开 VoiceOver；"
                "② 启动 AgentOS.app 并置于前台；③ 人工用 VO 键（Ctrl+F3）"
                "逐控件走一遍焦点顺序与朗读；"
                "④ 系统设置→键盘→「全键盘访问」设为「所有控件」后重跑"
                "kbd（.venv-pw/bin/python tools/a11y_walk.py --url <地址>），"
                "确认 Tab 能走通全部控件。")


def _ax_capability(visible=()):
    """AX 层本身能不能读 -> (alive, why)

    判「活着」的证据是一棵**真实 AX 树**：PyObjC ApplicationServices 读对照
    进程（Finder）的 kAXWindowsAttribute，再有界遍历 kAXChildrenAttribute，
    要求节点数 > 1 且角色不只有一种。角色多样性是关键 —— Chromium 对非 AT
    进程只暴露 AXApplication 自链（实测 31 节点、角色全同），那种树虽然
    「读得到」，但读不到任何内容，拿它当阳性对照就是在自己骗自己。

    判「死」只有一种：AX 层真的读不出任何一棵内容树。旧实现判死的两条依据
    已被实测推翻（见上方 D94 注释：-1728 是 Apple Events 错误、Finder 本就
    0 窗），所以这里不再用它们。
    """
    fake = "[FAKE-AT] " if os.environ.get("AGENTOS_AT_FAKE_EMPTY") == "1" else ""
    mode = os.environ.get("AGENTOS_AT_FAKE_AX", "")
    if mode == "alive":
        return True, fake + "[FAKE] 强制 AX 可读"
    elif mode == "dead":
        AT_BLOCK.update({"blocked": "TCC-AX-UNREADABLE", "fix": AT_FIX})
        return False, fake + "[FAKE] 强制 AX 不可读 => 读不到任何 AX 树"
    trusted = _ax_trusted()
    if trusted is False:
        AT_BLOCK.update({"blocked": "TCC-AX-UNREADABLE", "fix": AT_FIX})
        return False, (fake + "AXIsProcessTrusted()=False => 本进程真的没被授予"
                       "辅助功能权限，请给运行本门的宿主 App 授权后重跑")
    tree, why = _ax_probe_tree(_AX_CONTROL_PID, _AX_TREE_MAX_NODES)
    if tree:
        return True, fake + why
    if trusted is None:
        return False, (fake + why + "（PyObjC ApplicationServices 不可用，"
                       "本门无法判定 AX 层，未据此推断 TCC）")
    # trusted=True 但读不出内容树：这才是真的 AX 层不可读。
    AT_BLOCK.update({"blocked": "TCC-AX-UNREADABLE", "fix": AT_FIX})
    return False, fake + "AXIsProcessTrusted()=True 但 %s" % why


def _ax_trusted():
    """AXIsProcessTrusted() -> True / False / None（PyObjC 不可用）。
    顶层 Quartz 不导出 AX 函数，必须 import ApplicationServices。
    ctypes 直调这条路径实测 segfault（rc=139），已禁用。"""
    try:
        import ApplicationServices as AS
    except ImportError:
        return None
    try:
        return bool(AS.AXIsProcessTrusted())
    except Exception:  # noqa: BLE001
        return None


def _ax_probe_tree(proc_name, max_nodes):
    """读 proc_name 的一棵 AX 树 -> (内容树, why)
    判「内容树」要求**非菜单**内容节点数 ≥ _AX_MIN_CONTENT_NODES 且读出
    ≥ _AX_MIN_TEXTS 条真实文本；AXApplication 自链不算。

    D105 之前这版没有环检测、没有菜单分离，于是遍历 AXMenuBar 的菜单项就把
    节点预算吃光，`nodes=600` 配四种角色被判成「真树，非自链」—— 实测那是
    纯菜单树（Finder 去掉菜单后 content=144/texts=35，而旧量具读到的是
    nodes=600 roles=AXApplication,AXGroup,AXMenu,AXMenuBar）。
    """
    try:
        import ApplicationServices as AS
    except ImportError as e:
        return False, "PyObjC ApplicationServices 不可用（%s）" % e
    p = subprocess.run(["pgrep", "-x", proc_name], capture_output=True, text=True)
    pids = [int(x) for x in p.stdout.split() if x.strip().isdigit()]
    if not pids:
        return False, "对照进程 %r 不在运行，无法取对照 AX 树" % proc_name
    best = ""
    for pid in pids[:3]:
        try:
            el = AS.AXUIElementCreateApplication(pid)
            e0, wins = AS.AXUIElementCopyAttributeValue(
                el, AS.kAXWindowsAttribute, None)
            if e0 != 0:
                best = "%s pid=%d kAXWindowsAttribute err=%d" % (proc_name, pid, e0)
                continue
            roles, stack, n = {}, [(w, 0) for w in wins], 0
            seen, cyc, menu, texts = set(), 0, 0, []
            while stack and n < max_nodes:
                node, depth = stack.pop()
                key = hash(node)
                if key in seen:
                    cyc += 1
                    continue
                seen.add(key)
                n += 1
                e1, role = AS.AXUIElementCopyAttributeValue(
                    node, AS.kAXRoleAttribute, None)
                role = role if e1 == 0 and isinstance(role, str) else None
                if role:
                    roles[role] = roles.get(role, 0) + 1
                    if role in _AX_MENU_ROLES:
                        menu += 1
                    else:
                        e3, txt = AS.AXUIElementCopyAttributeValue(
                            node, AS.kAXValueAttribute, None)
                        if e3 != 0 or not txt:
                            e3, txt = AS.AXUIElementCopyAttributeValue(
                                node, AS.kAXTitleAttribute, None)
                        if e3 == 0 and txt:
                            texts.append(str(txt))
                # 菜单子树不进递归：菜单项对「界面有没有内容」零信息量，
                # 却能把节点预算吃光（D105 实测的旧假象）。
                if role in _AX_MENU_ROLES or depth >= _AX_TREE_MAX_DEPTH:
                    continue
                e2, kids = AS.AXUIElementCopyAttributeValue(
                    node, AS.kAXChildrenAttribute, None)
                if e2 == 0 and kids is not None:
                    stack.extend((k, depth + 1) for k in list(kids))
            content = sum(v for k, v in roles.items() if k not in _AX_MENU_ROLES)
            tag = ("%s pid=%d 树 nodes=%d 环=%d 菜单节点=%d 非菜单=%d 文本=%d"
                   % (proc_name, pid, n, cyc, menu, content, len(texts)))
            if (content >= _AX_MIN_CONTENT_NODES
                    and len(texts) >= _AX_MIN_TEXTS):
                return True, _fake_txt() + tag + " => AX 层可读（非菜单内容树）"
            best = tag + "（非菜单内容不足 %d 节点 / %d 文本 ⇒ 读不到界面内容）" % (
                _AX_MIN_CONTENT_NODES, _AX_MIN_TEXTS)
        except Exception as e:  # noqa: BLE001
            best = "%s pid=%d 遍历异常 %s" % (proc_name, pid, e)
    return False, best or "AX 树遍历未产出任何节点"


def _fake_txt():
    return "[FAKE-AT] " if os.environ.get("AGENTOS_AT_FAKE_EMPTY") == "1" else ""


def _ax_deliverable_tree():
    """读**交付物自己**（AgentOS.app）的 AX 树 -> (content_tree, why)

    D105 实测（2026-10-05，rc=0）：进程名 AgentOS，kAXWindows err=0 n=1，
    非菜单内容节点 305、文本 160、AXWebArea=1。所以交付物对 AX 层是**可读**
    的，旧的「只暴露 AXApplication 自链」结论只对旧量具的错误读数成立。
    窗口需 App 启动 >7s 才在 kAXWindows 注册，早采样会读到 n=0（时序，非缺陷）。
    """
    return _ax_probe_tree(_AX_DELIVERABLE, _AX_TREE_MAX_NODES)


def _osa_args(script, *args):
    try:
        p = subprocess.run(["osascript", "-e", script] + list(args),
                           capture_output=True, text=True, timeout=20)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as e:  # noqa: BLE001
        return -1, "", str(e)


def probe_at(page_title=""):
    """真读屏可达性探针 -> (verified, token, reason)
    只回答「这台机器上能不能真读屏」，不抄第二套 a11y 逻辑。
    判红条件是「拿不到证据」，不是「页面有问题」——它不进绿门计数。"""
    rc0, out0, err0 = _osa('tell application "System Events" to get name of '
                           'first process whose frontmost is true')
    fake = "[FAKE-AT] " if os.environ.get("AGENTOS_AT_FAKE_EMPTY") == "1" else ""
    if rc0 != 0 or not out0:
        return False, "AT-UNVERIFIED", fake + "System Events 不可用（rc=%s %s）" % (rc0, err0[:60])
    app = out0.strip()
    # D66 自身修出的假阳性（2026-10-03 实测）：第一版只问「前台 App 的 AX 元素
    # 有几个」，于是前台换成 QuickTime Player 时读到 30 个 → 打印 AT-VERIFIED。
    # **读到的是别的 App 的树，与被测页面毫无关系** —— 这比 AT-UNVERIFIED 更坏，
    # 是一种撒谎。故判 VERIFIED 必须同时成立三条：
    #   ① 前台是浏览器  ② 它的窗口标题含**被测页面自己的 <title>**
    #   ③ 该窗口 AX 元素数 > 0
    # 缺一即 AT-UNVERIFIED，并说清缺哪条。
    if not any(t in app.lower() for t in _BROWSER_TOKENS):
        return False, "AT-UNVERIFIED", (
            "%sfrontmost=%s 不是浏览器 ⇒ 它的 AX 树与被测页面无关，不得据此"
            "声称读屏已验证（真验证需用浏览器打开本页并置于前台）" % (fake, app))
    token = (page_title or "").strip() or "AgentOS"
    rc2, out2, err2 = _osa('tell application "System Events" to tell '
                           '(first process whose frontmost is true) to get name of '
                           'every window')
    if rc2 != 0:
        return False, "AT-UNVERIFIED", "%sfrontmost=%s 窗口标题查询 rc=%s %s" % (
            fake, app, rc2, (err2 or "")[:60])
    titles = [t.strip() for t in out2.split(",") if t.strip()]
    if not any(token in t for t in titles):
        return False, "AT-UNVERIFIED", (
            "%sfrontmost=%s 窗口标题 %r 中没有 %r ⇒ 未证明这棵树属于被测页面"
            % (fake, app, titles[:3], token))
    rc1, out1, err1 = _osa('tell application "System Events" to tell '
                           '(first process whose frontmost is true) to count of '
                           'UI elements of window 1')
    detail = fake + "frontmost=%s 窗口命中 %r 计数 rc=%s out=%r" % (
        app, token, rc1, out1[:40])
    if rc1 != 0:
        return False, "AT-UNVERIFIED", "%s · %s" % (detail, (err1 or "")[:70])
    try:
        n = int(out1)
    except Exception:
        return False, "AT-UNVERIFIED", "%s · 计数不可解析为整数" % detail
    if n == 0:
        return False, "AT-UNVERIFIED", (
            "%s · 返回 0 个 AX 元素（静默空）⇒ 不得判为读屏已验证。"
            "**不要**据此断定是辅助功能权限问题：对 Chromium 前台窗口，"
            "0 元素是 macOS 不向非 AT 进程暴露 web content AX 树的正常表现"
            "（D94 实测）；真正的权限判据见 _ax_capability 的 AXIsProcessTrusted"
            % detail)
    return True, "AT-VERIFIED", "%s · 读到 %d 个 AX 元素" % (detail, n)


# ── 浏览器实测 ────────────────────────────────────────────────────────────

_AX_INTERACTIVE = ("button", "link", "textbox", "combobox", "checkbox",
                   "radio", "menuitem", "tab", "switch", "spinbutton",
                   "slider", "option", "searchbox")
_AX_LANDMARKS = ("banner", "main", "complementary", "contentinfo", "navigation")


def _ax_name(node):
    """AX 节点的可访问名。CDP 给的是 name/description/supported 三个来源，
    读屏实际消费 name，缺失即读屏念不出这控件。"""
    for k in ("name", "description"):
        v = node.get(k)
        if isinstance(v, dict) and (v.get("value") or "").strip():
            return v["value"].strip()
    return ""


def _ax_collect(nodes):
    """从 CDP Accessibility.getFullAXTree 的扁平节点表抽出与读屏相关的一层：
    地标 / 交互控件 / tabpanel，逐个带可访问名。这棵树就是 VoiceOver 实际
    消费的那棵，不是我们自己再算一遍 DOM。"""
    out = {"roles": {}, "landmarks": [], "controls": [], "tabpanels": [],
           "tabs": []}
    for nd in nodes:
        role = ((nd.get("role") or {}).get("value") or "").strip()
        if not role or role in ("none", "InlineTextBox", "generic", "StaticText"):
            continue
        nm = _ax_name(nd)
        out["roles"][role] = out["roles"].get(role, 0) + 1
        rec = {"role": role, "name": nm}
        if role in _AX_LANDMARKS:
            out["landmarks"].append(rec)
        if role == "tabpanel":
            out["tabpanels"].append(rec)
        if role == "tab":
            out["tabs"].append(rec)
        if role in _AX_INTERACTIVE:
            out["controls"].append(rec)
    return out


def _at_os_probe(page_title=""):
    """把浏览器窗口拉到前台后读 OS 辅助功能层。System Events 读的 window 1
    只含浏览器工具栏（网页内容在 WebArea 之下，OS 层读不到），所以网页内容
    的可访问性由 CDP AX 树负责；本函数只回答「OS 层能不能读到前台窗口」。"""
    rc0, vis, _e = _osa(_OSA_VISIBLE)
    names = [n.strip() for n in vis.split(",") if n.strip()]
    token = (page_title or "").strip() or "AgentOS"
    ax_ok, ax_why = _ax_capability(names)
    if not ax_ok:
        return False, "AT-UNVERIFIED", (
            "AX 层不可读 ⇒ OS 读屏层无法验证（与页面开没开无关）：%s；"
            "要真验证需：%s" % (ax_why, AT_FIX))
    # D105 更正：交付物是原生 AgentOS.app，不是浏览器页面。旧实现在这里问
    # 「可见浏览器里有没有标题含 <title> 的窗口」，而 AgentOS 永远不在
    # _BROWSER_TOKENS 里 ⇒ 这条分支对真实产品**恒假**，输出的归因
    # 「请打开它再重跑」指向一个根本不存在的问题。改为直接读交付物自己的
    # AX 树（AgentOS 未运行时如实报 not running）。
    dl_ok, dl_why = _ax_deliverable_tree()
    if not dl_ok:
        return False, "AT-UNVERIFIED", (
            "AX 层可读（%s）但读不到交付物 %r 的内容树：%s ⇒ "
            "先启动 AgentOS.app 并等窗口注册（实测需 >7s）再重跑；"
            "注意「0 窗口」也可能只是它在后台，不能据此断定没开。"
            "人工项同 AT-UNVERIFIED 常规口径：① Cmd+F5 开 VoiceOver "
            "实机走焦点顺序与朗读；② 系统设置→键盘→「全键盘访问」"
            "设为「所有控件」后重跑 kbd（.venv-pw/bin/python "
            "tools/a11y_walk.py --url <地址>）确认 Tab 走通全部控件"
            % (ax_why, _AX_DELIVERABLE, dl_why))
    _rc, wl, _e2 = _osa_args(_OSA_WINDOWS, _AX_DELIVERABLE)
    hits = [w.strip() for w in wl.split("||") if w.strip()]
    target = _AX_DELIVERABLE
    act_rc, _o, act_err = _osa_args(_OSA_ACTIVATE, target)
    front = ""
    for _i in range(3):
        time.sleep(0.9)
        _fr, front, _fe = _osa('tell application "System Events" to get name '
                               'of first process whose frontmost is true')
        if front == target:
            break
        _osa_args(_OSA_ACTIVATE, target)
    if front != target:
        return False, "AT-UNVERIFIED", (
            "已请求置前台 %r（activate rc=%s %s）但 frontmost=%r ⇒ OS 层读到的"
            "树仍不属于被测页面" % (target, act_rc, act_err[:40], front))
    # 到这里已拿到交付物真实的 AX 树物证，并且它是前台 App。剩下的唯一
    # 未自动化环节是「VoiceOver 实际的焦点移动顺序与朗读」，本门进程不是
    # AT 客户端，拿不到也**不虚报**。旧实现在此处转手调 probe_at，而
    # probe_at 认的是浏览器前台 ⇒ 对原生 AgentOS 必然吐「不是浏览器」，
    # 把一个已经验过的原生 App 说成「与被测页面无关」。改为如实报缺口。
    return False, "AT-UNVERIFIED", (
        "%r 已在前台（窗口 %r）且 AX 内容树可读（%s）⇒ AX 层物证成立，"
        "剩下的缺口只是 VoiceOver 的焦点顺序/朗读，本门进程非 AT 客户端拿不到"
        "（AXIsProcessTrusted=True 只保证能读树、不保证能拿焦点序列）→ %s"
        % (target, hits[:1], dl_why, AT_FIX_FOCUS))


def _at_live(url, page_title):
    """真读屏走查：拉起**有头** Chromium 打开本页 → 逐视图读 CDP AX 树 →
    置前台读 OS 层。返回 (trees, os_ok, os_token, os_detail, err)。"""
    from playwright.sync_api import sync_playwright
    trees = []
    err = ""
    try:
        with sync_playwright() as p:
            br = p.chromium.launch(headless=False,
                                  args=["--force-color-profile=srgb"])
            pg = br.new_page(viewport={"width": 1440, "height": 900})
            pg.goto(url, wait_until="load")
            pg.wait_for_timeout(900)
            for i in range(len(VIEWS)):
                _goto_view(pg, i)
                cdp = pg.context.new_cdp_session(pg)
                cdp.send("Accessibility.enable")
                raw = cdp.send("Accessibility.getFullAXTree")
                trees.append(dict(_ax_collect(raw.get("nodes") or []),
                                  _view=VIEWS[i]))
                cdp.detach()
            br.close()
    except Exception as e:  # noqa: BLE001 - 基础设施故障，非页面缺陷
        err = "有头浏览器不可用：%s" % e
    # OS 层单独一拍取判定，不再塞进浏览器 try：D105 起它读的是原生交付物
    # AgentOS.app 的 AX 树，与浏览器开不开无关。旧写法让 Chromium 一崩
    # （实测 M7：Target page, context or browser has been closed）就把整条
    # OS 判定连锅端走，只报「浏览器不可用」——OS 层的真答案被基础设施故障
    # 吞了。拆开之后：浏览器故障进 err 原样上报，OS 层判定自己说话。
    try:
        ok, token, detail = _at_os_probe(page_title)
    except Exception as e:  # noqa: BLE001 - 基础设施故障，非页面缺陷
        ok, token, detail = False, "AT-UNVERIFIED", (
            "OS 层读屏探测自身异常（AppleScript/AX 基础设施）：%s" % e)
    return trees, ok, token, detail, err

def _digit_key(idx):
    """第 idx 个视图（0 基）对应的数字键。1..9 走 1..9，**第 10 个视图走 0** ——
    与 index.template.html 的 keydown 处理和屏上 kbd 标签同口径。D95 实测：
    这行原来无条件 str(idx+1)，第 10 屏上线后 playwright 直接抛
    Unknown key: "10"，整门 0/6 假红 —— 门连「切到第 10 屏」都没做到，
    却报成「实测层失败」。

    2026-10-10 R7：tablist 到了 13 个 tab，但数字键只到第 10 屏（第 11-13 屏
    屏上没有 kbd 标签，老板裁定不给凑不出来的数字键）。所以 idx>=10 必须是
    **显式抛错**而不是 str(idx+1)：抛出来的话，调用点一句话就露馅；str(idx+1)
    会造出 '11'/'12'/'13' 这种不存在的按键名，playwright 抛的是
    Unknown key: "11"，看上去像环境故障，真正的原因（这一屏没有数字键）
    反而被埋掉 —— D95 同一课，换个马甲再来一次。"""
    if idx >= 10:
        raise ValueError("第 %d 屏没有数字键，_goto_view 会走点击分支" % (idx + 1))
    return "0" if idx == 9 else str(idx + 1)


def _goto_view(page, idx):
    """切到第 idx 个视图（0 基）。前 10 屏用真键盘数字键（与 a11y_walk 的数字键
    1..N 同口径），第 11 屏起改走真鼠标点击 .nav 上的 tab。

    为什么后半段不用 page.evaluate("k => view(k)")：那是一条 JS 后门，绕开了
    用户真实可用的每条输入通道。第 11-13 屏恰好是**只靠方向键 / Tab / Cmd+K
    可达**的屏，用 evaluate 切过去等于把「这一屏到底进不进得去」这个问题
    整个跳过 —— 而它正是 R7 裁定的核心。真鼠标点 tab 按钮，量的才是用户的路。"""
    if idx < 10:
        page.keyboard.press(_digit_key(idx))
    else:
        page.click('.nav button[data-v="%s"]' % VIEW_KEYS[idx])
    page.wait_for_timeout(260)


def _measure_focus(page):
    """逐控件：先 blur 量基线样式，再 focus 量聚焦样式，比出可见差异。
    先按一次真 Tab 激活键盘模态，否则 :focus-visible 不生效 —— 这是 Chrome
    的实际规则，不按就等于在测「鼠标焦点」。"""
    page.keyboard.press("Tab")
    page.wait_for_timeout(120)
    els = page.evaluate(FOCUSABLE_JS)
    measured = []
    for e in els[:60]:
        page.evaluate(BLUR_JS, e["sel"])
        before = page.evaluate(STYLE_JS, e["sel"])
        page.evaluate(FOCUS_JS, e["sel"])
        page.wait_for_timeout(25)
        after = page.evaluate(STYLE_JS, e["sel"])
        why, detail = _visible_indicator(before, after)
        measured.append({"sel": e["sel"], "tag": e["tag"], "label": e["label"],
                        "why": why, "detail": detail})
    return measured


_BODY_CTX = {}


def _body(_ctx):
    flake_inject()
    from playwright.sync_api import sync_playwright

    url, html = _target()
    focus_all, contrast_all, reflow_all = [], [], []

    with sync_playwright() as p:
        br = p.chromium.launch(args=["--force-color-profile=srgb"])
        pg = br.new_page(viewport={"width": 1440, "height": 900})
        pg.goto(url, wait_until="load")
        pg.wait_for_timeout(700)
        page_title = pg.title()
        for i in range(len(VIEWS)):
            _goto_view(pg, i)
            focus_all += _measure_focus(pg)
            contrast_all += pg.evaluate(CONTRAST_JS)
            reflow_all.append(dict(pg.evaluate(REFLOW_JS), _view=VIEWS[i]))
        for w, tag in ((640, "200pct"), (320, "320px")):
            pg.set_viewport_size({"width": w, "height": 800})
            pg.wait_for_timeout(320)
            for i in range(len(VIEWS)):
                _goto_view(pg, i)
                info = pg.evaluate(REFLOW_JS)
                info["_view"] = "%s/%s" % (tag, VIEWS[i])
                reflow_all.append(info)
        pg.set_viewport_size({"width": 1440, "height": 900})
        pg.emulate_media(reduced_motion="reduce")
        pg.wait_for_timeout(420)
        motion = pg.evaluate(ANIM_JS)
        br.close()

    ctx = {"focus": focus_all, "contrast": contrast_all, "motion": motion,
           "reflow": reflow_all, "html": html, "url": url,
           "page_title": page_title, "gates": []}
    trees, at_ok, at_token, at_detail, at_err = _at_live(url, page_title)
    ctx["at_trees"] = trees
    ctx["at_os_ok"] = at_ok
    ctx["at_os_token"] = at_token
    ctx["at_os_detail"] = at_detail
    ctx["at_err"] = at_err
    AT_STATUS.update({"verified": at_ok, "token": at_token, "reason": at_detail,
                      "blocked": AT_BLOCK.get("blocked"),
                      "fix": AT_BLOCK.get("fix", "")})
    # D111：ctx 另行留一份，main() 从这份取，不再看 run_gate 的返回值类型。
    # 踩实经过（2026-10-10 16:29）：浏览器启动抖了一次，run_gate 第二次把
    # _body 全量跑通，按 flake_retry.py:71 的语义把 detail 串成
    # "重试×1 后过 | {dict}" —— 这本该是成功路径。可 main() 早先写的是
    # `not isinstance(detail, dict)`，于是拿到一个 str，把一次实测通过
    # 整门报成 A11Y_HUMAN FAIL 0/6。同一条链 16:44 干净复跑是
    # A11Y_HUMAN OK 6/6 rc=0，判据本身没红，红的是这条取值路径。
    # 后果不是「多跑一遍」：红门让整条 35 门链 rc≠0，D50 上线自证跟着红，
    # 而排查的人会从「哪条 a11y 判据塌了」这个错误前提出发。
    _BODY_CTX.clear()
    _BODY_CTX.update(ctx)
    return True, ctx


# ── 四道判据 + AT 状态探针 ────────────────────────────────────────────────

def g_focus(ctx):
    return focus_visible_ok(ctx["focus"])


def g_contrast(ctx):
    return rendered_contrast_ok(ctx["contrast"])


def g_motion(ctx):
    return reduced_motion_ok(ctx["motion"])


def g_reflow(ctx):
    bad = [r for r in ctx["reflow"] if not reflow_ok(r)[0]]
    if not ctx["reflow"]:
        return False, "0 个视口/视图组合被测（门自己瞎了，fail-closed）"
    if bad:
        first = bad[0]
        return False, ("%d/%d 组合回流失败 · 首例 %s：%s" % (
            len(bad), len(ctx["reflow"]), first.get("_view"), reflow_ok(first)[1]))
    return True, "%d 个视口×视图组合均无横向溢出、无静默裁字" % len(ctx["reflow"])


def g_at_tree(ctx):
    """真读屏走查（网页内容层）：逐视图读 VoiceOver 实际消费的 CDP AX 树。
    判红 = 地标缺名 / tab 无名 / 存在无名交互控件 —— 这三类是读屏实灾。"""
    trees = ctx.get("at_trees") or []
    if not trees:
        return False, "0 个视图的 AX 树被测（门自己瞎了，fail-closed）"
    if len(trees) != len(VIEWS):
        return False, ("只量到 %d/%d 视图的 AX 树（少采任何一屏都算门自己瞎了，"
                       "fail-closed）" % (len(trees), len(VIEWS)))
    bad = []
    for t in trees:
        have = {r["role"] for r in t["landmarks"]}
        miss = [r for r in _AX_LANDMARKS if r not in have]
        unnamed_lm = [r["role"] for r in t["landmarks"] if not r["name"]]
        unnamed_tb = [r["name"] or "(无名)" for r in t["tabs"] if not r["name"]]
        unnamed = ["%s/%s" % (c["role"], c["name"] or "(无名)")
                   for c in t["controls"] if not c["name"]]
        if miss or unnamed_lm or unnamed_tb or unnamed:
            bad.append("%s 地标缺=%s 地标无名=%s tab无名=%s 控件无名=%s"
                       % (t["_view"], miss, unnamed_lm, unnamed_tb, unnamed[:3]))
    nt = sum(len(t["tabs"]) for t in trees)
    if bad:
        return False, "%d/%d 视图不合格 · 首例 %s" % (len(bad), len(trees), bad[0])
    return True, ("%d 视图 × 地标%d/类齐备 · tab %d 个全有名 · 无名交互控件 0"
                  % (len(trees), len(_AX_LANDMARKS), nt))


def g_at_os(ctx):
    """真读屏走查（OS 层）：前台浏览器窗口 AX 可读且标题命中本页。判绿不靠
    「读屏可用」这个环境结论 —— 环境不达标时判绿并显式说 AT-UNVERIFIED，
    绝不因此把页面判红，也绝不因此假装读屏已验证。"""
    token = ctx.get("at_os_token", "AT-UNVERIFIED")
    detail = ctx.get("at_os_detail", "")
    AT_STATUS.update({"verified": bool(ctx.get("at_os_ok")), "token": token,
                      "reason": detail,
                      "blocked": AT_BLOCK.get("blocked"),
                      "fix": AT_BLOCK.get("fix", "")})
    return True, "%s · %s" % (token, detail)


GATES = [
    ("焦点可见性：每个可聚焦控件聚焦后有人眼可见指示（WCAG 2.4.7）", g_focus),
    ("真渲染对比度：逐视图实测 ≥4.5 / 大字 ≥3.0（WCAG 1.4.3）", g_contrast),
    ("减弱动态效果：prefers-reduced-motion 下 0 个运行中动画（WCAG 2.3.3）", g_motion),
    ("缩放回流：200% 缩放与 320px 下无横向溢出、无静默裁字（WCAG 1.4.10）", g_reflow),
    ("真读屏 AX 树走查：逐视图地标齐备且有可访问名、tab 有名、无名交互控件 0",
     g_at_tree),
    ("真读屏 OS 层状态显式记录（AT-VERIFIED / AT-UNVERIFIED，不静默）", g_at_os),
]


sys.path.insert(0, HERE)
from flake_retry import flake_inject, run_gate  # noqa: E402


def _target():
    """导航目标：默认 file://index.html；--url 指 http（served 态）；
    --html 指隔离副本（变异体用，绝不就地改活的 index.html）。"""
    url = None
    html = HTML
    if "--url" in sys.argv:
        url = sys.argv[sys.argv.index("--url") + 1]
    if "--html" in sys.argv:
        html = os.path.abspath(sys.argv[sys.argv.index("--html") + 1])
    if not os.path.isfile(html):
        print("FAIL: index.html 不存在 %s" % html)
        sys.exit(2)
    return (url or ("file://" + html)), html


def _out_path():
    """证据位路径。--out 让「谁跑的」与「证据写哪」解耦：反向门在隔离副本上
    注入缺陷后照样调本门，若证据位写死 work/a11y_human.json，那链跑完盘上
    留下的就是故意打坏的那一份（与 a11y_walk --out 同一个老病）。"""
    if "--out" in sys.argv:
        return os.path.abspath(sys.argv[sys.argv.index("--out") + 1])
    return REPORT


def _write_report(ctx, path):
    od = os.path.dirname(path)
    if od and not os.path.isdir(od):
        os.makedirs(od, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ctx, f, ensure_ascii=False, indent=1)


def main():
    """唯一入口。判据红 = 结论，一次都不重试；基础设施故障才重试（D40 铁律）。"""
    _reexec_with_playwright()
    ok, detail = run_gate(_body, None)
    ctx = dict(_BODY_CTX)
    if not ok or not ctx:
        print("  [EXC] " + str(detail))
        print("== A11Y_HUMAN FAIL  0/%d 门通过（实测层失败）==" % len(GATES))
        return 2
    results = []
    for name, fn in GATES:
        g_ok, g_detail = run_gate(fn, ctx, name)
        results.append({"name": name, "ok": bool(g_ok), "detail": g_detail})
        print(("  [ok  ] " if g_ok else "  [FAIL] ") + name + ("  " + g_detail if g_detail else ""))
    nfail = sum(1 for r in results if not r["ok"])
    ctx["gates"], ctx["passed"], ctx["total"] = results, len(results) - nfail, len(results)
    ctx["at"] = dict(AT_STATUS)
    _write_report(ctx, _out_path())
    print("== A11Y_HUMAN %s  %d/%d 门通过（真渲染实测 · AT=%s）=="
          % ("OK" if nfail == 0 else "FAIL", len(results) - nfail, len(results),
             AT_STATUS["token"]))
    return 0 if nfail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
