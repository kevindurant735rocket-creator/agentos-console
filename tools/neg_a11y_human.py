import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from variant_pool import map_cases

# D68：a11y_human.py 的反向门。为什么必须有：
# a11y_human 的四道判据全部是「读真渲染结果再判」，而真渲染门最典型的失效
# 方式不是判据写错，而是**量具根本没量到东西却照样判绿**（门自己瞎了）。
# 所以逐条注入一个真缺陷，要求门**红在指定的那一道**上：
#   红在别处 = 判据错位空门；rc=0 = 空门；无汇总行 = 量具被杀，不是结论。
SRC = "index.html"
HTML = os.path.abspath(SRC)
REPORT = os.path.join("work", "a11y_human.json")
RETRY_N = 2
RETRY_S = 2.0

base = open(SRC, encoding="utf-8").read()
BASE = hashlib.sha256(base.encode()).hexdigest()[:12]
if "</head>" not in base:
    print("FAIL: index.html 里没有 </head>，注入点不存在")
    sys.exit(2)

_TD = tempfile.mkdtemp(prefix="neg-a11y-human-")
ISO = os.path.join(_TD, "index.html")


def _style(css):
    return "<style>%s</style></head>" % css


CASES = [
    ("M1 抽掉焦点指示（:focus 无 outline 也无 box-shadow）",
     _style(":focus,:focus-visible{outline:none!important;"
            "box-shadow:none!important}"),
     "焦点可见性"),
    ("M2 全站正文压成 3.4:1 灰字（低于 4.5）",
     _style("body,.dv,.dv *{color:#949494!important;background:#fff!important}"),
    "真渲染对比度"),
    # M3 原先是 *{animation:a11yhzspin 1s linear infinite}：整页连 .nav 一起转
    # 360 度，tab 按钮被转到视口外，playwright 的 click 直接抛
    # "Element is outside of the viewport" —— 那一屏连真人用户都点不到，_body 抛
    # 异常、整门退化成「0/6 实测层失败」。那是量具诚实地说「量不了」，可本门要看
    # 的是「减弱动态效果」这道判据有没有牙：量具一死就什么都证明不了，只能判
    # 「红了但红在别处」。R7 把第 11-13 屏改成真鼠标点 tab 之后才踩到这个坑
    # （此前全程数字键、一次 click 都没有）。
    # 缺陷类别一个字节都没换：还是无穷动画不理会 prefers-reduced-motion。只把落点
    # 从整页收窄到页面本来就有 9 个的 .kbd 小标签 —— 页面照样量得出数，判据必须
    # 自己红，不许靠量具崩掉蒙过去。
    ("M3 在 prefers-reduced-motion 下仍强制无限动画",
     _style("@keyframes a11yhzspin{to{transform:rotate(360deg)}}"
            ".kbd{animation:a11yhzspin 1s linear infinite}"),
     "减弱动态效果"),
    ("M4 固定 900px 宽（320px 窄视口必横向溢出）",
     _style("body{width:900px;min-width:900px}"),
     "缩放回流"),
]


def _read_at(path):
    try:
        with open(path, encoding="utf-8") as f:
            return str(json.load(f).get("at", {}).get("token", ""))
    except Exception:
        return ""


def _read_at_field(path, field):
    try:
        with open(path, encoding="utf-8") as f:
            return str(json.load(f).get("at", {}).get(field, ""))
    except Exception:
        return ""


# D84：判别力变异体。修了「AX 层不可读」与「页面没打开」的混报之后，
# 最怕的就是新分类器**只会说 TCC** —— 那样它对两种死法一视同仁，
# 等于把误导消息换了个词，判别力为零。所以两条都必须钉死：
#   M6 强制 AX 可读  -> 消息**不得**出现 TCC/辅助功能字样；
#   M7 强制 AX 不可读 -> 消息**必须**点名 TCC，并带上可执行的补救路径。
_TCC_MARKERS = ("TCC", "辅助功能", "AX 层不可读")


def _case_fake_ax(mnum, tag, mode, must_mention_tcc):
    iso = os.path.join(_TD, "iso-%s.html" % tag)
    shutil.copyfile(SRC, iso)
    r, _reds, summ, at = _run(tag, iso, {"AGENTOS_AT_FAKE_AX": mode})
    reason = _read_at_field(os.path.join(_TD, "neg-report-%s.json" % tag),
                            "reason")
    lines = ["== M%d %s（后门 mode=%s） -> rc=%d token=%s"
             % (mnum, "强制 AX 可读" if mode == "alive" else "强制 AX 不可读",
                mode, r, at)]
    for ln in summ:
        lines.append("    " + ln)
    hit = [m for m in _TCC_MARKERS if m in reason]
    if not reason:
        lines.append("    !! 报告里没有 at.reason = 分类器没留证据，无法判别")
        return lines + ["VRC=1"]
    if must_mention_tcc and not hit:
        lines.append("    !! AX 不可读却没点名 TCC（reason=%r）= 静默空被吞了"
                     % reason[:120])
        return lines + ["VRC=1"]
    if not must_mention_tcc and hit:
        lines.append("    !! AX 可读却仍报 TCC（命中 %r）= 分类器不会分辨，"
                     "换个词继续误导" % hit)
        return lines + ["VRC=1"]
    lines.append("    [ok  ] 判别正确：reason=%r" % reason[:150])
    return lines + ["VRC=0"]


def _run(tag, iso, env_extra=None):
    """在隔离副本上跑 a11y_human。--out 指本门临时目录：变异体报告绝不许落到
    work/a11y_human.json（真走查的证据位，neg_a11y 已在同类处踩过）。
    返回 (rc, reds, summ, at_token)。"""
    out = os.path.join(_TD, "neg-report-%s.json" % tag)
    env = dict(os.environ)
    env.update(env_extra or {})
    rc, reds, summ = 9, [], []
    for i in range(RETRY_N + 1):
        p = subprocess.run(
            [sys.executable, os.path.join("tools", "a11y_human.py"),
             "--html", iso, "--out", out],
            capture_output=True, text=True, env=env, timeout=900)
        rc = p.returncode
        reds = [ln.strip()[:130] for ln in p.stdout.splitlines()
                if "[FAIL]" in ln]
        summ = [ln.strip() for ln in p.stdout.splitlines() if "门通过" in ln]
        if summ:
            break
        time.sleep(RETRY_S)
    return rc, reds, summ, _read_at(out)


def _judge(name, expect, res):
    """返回 (rc, 行列表) —— 并发下不许在 worker 里 print。"""
    rc, reds, summ = res
    lines = ["== %s -> rc=%d" % (name, rc)]
    for ln in reds:
        lines.append("    RED: " + ln)
    for ln in summ:
        lines.append("    " + ln)
    if not summ:
        lines.append("    !! 量具无门汇总行 = 没测到，不算门变红")
        return 2, lines
    if rc == 0:
        lines.append("    !! 门未变红 = 空门")
        return 1, lines
    if expect and not any(expect in ln for ln in reds):
        lines.append("    !! 红了但红在别处（期望 %s）= 判据错位空门" % expect)
        return 1, lines
    lines.append("    [ok  ] 预期红项命中: %s" % (expect or "(未指定)"))
    return 0, lines


def _run_case(idx, case):
    """一个 CSS 变异体：自己一份副本 + 自己一份 --out，返回行块。
    D72：原来 M1..M4 共用同一份 ISO，串行下「写→立刻读」才安全；
    并发必须一 case 一档（iso-m<i>.html），否则互相覆盖注入样式。"""
    # map_cases 传进来的是 0 基下标；本门的编号习惯是 M1..M5，故 +1。
    i, (name, inject, expect) = idx + 1, case
    iso = os.path.join(_TD, "iso-m%d.html" % i)
    with open(iso, "w", encoding="utf-8") as f:
        f.write(base.replace("</head>", inject, 1))
    one, lines = _judge(name, expect, _run("m%d" % i, iso)[:3])
    return lines + ["VRC=%d" % one]


def _case_m5():
    """M5：把 OS 层读屏证据伪造成「静默空」——门必须退回 AT-UNVERIFIED。"""
    iso = os.path.join(_TD, "iso-m5.html")
    shutil.copyfile(SRC, iso)
    r5, _reds5, summ5, at5 = _run("m5", iso, {"AGENTOS_AT_FAKE_EMPTY": "1"})
    lines = ["== M5 辅助功能静默空（后门） -> rc=%d token=%s" % (r5, at5)]
    for ln in summ5:
        lines.append("    " + ln)
    if at5 != "AT-UNVERIFIED":
        lines.append("    !! 拿不到证据却打 %r = 撒谎复发" % (at5 or "(空)"))
        return lines + ["VRC=1"]
    lines.append("    [ok  ] 拿不到证据时诚实退回 AT-UNVERIFIED")
    return lines + ["VRC=0"]


rc_all = 0
try:
    # M1..M4 走 _run_case；后面三个是环境后门变异体（不注入 CSS）。
    # D84 新增 M6/M7：判别力变异体，见 _case_fake_ax 的注释。
    # 用 dict 而不是 tuple 做标记：CASES 本身就是三元组，
    # 用 tuple 标记会被 isinstance(c, tuple) 连 M1..M4 一起劫持。
    items = list(CASES) + [None, {"tag": "m6", "mode": "alive"},
                           {"tag": "m7", "mode": "dead"}]

    def _one(i, c):
        if c is None:
            return _case_m5()
        if isinstance(c, dict):
            tag, mode = c["tag"], c["mode"]
            return _case_fake_ax(6 if mode == "alive" else 7, tag, mode,
                                 must_mention_tcc=(mode == "dead"))
        return _run_case(i, c)

    for ln in map_cases(items, _one,
                        header=lambda i, _c: "m%d" % (i + 1)):
        print(ln)
        if ln.startswith("VRC="):
            rc_all |= int(ln[4:])
finally:
    # 证据位零写入自检（内容判据，不用 mtime：并发门链会正常改写它）。
    if os.path.exists(REPORT):
        try:
            with open(REPORT, encoding="utf-8") as f:
                tgt = str(json.load(f).get("html", ""))
        except Exception as e:
            tgt = "<unreadable:%s>" % e
        if "neg-a11y-human-" in tgt:
            print("    !! 证据位 %s 被变异体污染（html=%s）" % (REPORT, tgt))
            rc_all = 2
        else:
            print("    [ok  ] 证据位零写入：%s 的 html=%s 非变异体" % (REPORT, tgt))
    else:
        print("    [warn] %s 不存在（a11y_human 还没跑过？）跳过" % REPORT)
    shutil.rmtree(_TD, ignore_errors=True)

now = hashlib.sha256(open(SRC, "rb").read()).hexdigest()[:12]
print("活产物零写入核验 sha=%s 基线=%s %s"
      % (now, BASE, "一致" if now == BASE else "不一致!!"))
if now != BASE:
    rc_all = rc_all or 1
print("== NEG_A11Y_HUMAN %s ==" % ("OK" if rc_all == 0 else "FAIL"))
sys.exit(rc_all)
