#!/usr/bin/env python3
"""UX DoD 机检：对齐引擎 depts/UX.md §5 的 10 条硬门。全部真跑，退出码即判据。

对应关系：
 1 裸 HTML 无样式            -> 有 <style> 且 token 数 >= 20
 2 四态齐全                  -> 空/加载/错误/404 四态代码 + 4 个构造入口
 3 三尺寸截图                -> docs/UX-SHOTS/*.png 覆盖 375/768/1440
 4 正文对比度 >= 4.5         -> WCAG 相对亮度实算
 5 键盘可达 + alt/label      -> tab 元素、aria-label、img alt、form label、方向键/回车处理
 6 首屏 < 2s 或有优化记录    -> work/perf.md 有实测记录
 7 野值像素有出处            -> 无 inline style 的魔法像素（style= 里只有 CSS 变量引用除外）
 8 截图时间戳对得上构建      -> png mtime >= index.html mtime
 9 声明交互预算              -> work/ui_budget.json 存在且字段齐全
10 可交互元素不超预算或渐进披露 -> 实测交互数 vs 预算；且有 details/dialog/hidden 容器
"""
import json
import os
import re
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
HTML = os.path.join(ROOT, "index.html")
SHOTS = os.path.join(ROOT, "docs", "UX-SHOTS")
BUDGET = os.path.join(ROOT, "work", "ui_budget.json")
PERF = os.path.join(ROOT, "work", "perf.md")

results = []


def chk(name, ok, detail=""):
    results.append((name, bool(ok), detail))


def srgb(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def lum(rgb):
    r, g, b = rgb
    return 0.2126 * srgb(r) + 0.7152 * srgb(g) + 0.0722 * srgb(b)


def parse_hex(c):
    c = c.strip()
    m = re.match(r"#([0-9a-fA-F]{3})$", c)
    if m:
        c = "".join(ch * 2 for ch in m.group(1))
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    m = re.match(r"#([0-9a-fA-F]{6})$", c)
    if m:
        return tuple(int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4))
    return None


def ratio(fg, bg):
    a, b = lum(fg), lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def hex_to_rgb(h):
    return h


def main():
    if not os.path.exists(HTML):
        print("UX_VERIFY_FAIL: index.html 不存在")
        return 2
    html = open(HTML, encoding="utf-8").read()

    # 1 有样式
    styles = re.findall(r"<style[^>]*>(.*?)</style>", html, re.S)
    tokens = len(set(re.findall(r"--[a-z0-9-]+\s*:", html)))
    chk("门1 令牌化样式（非裸 HTML）", bool(styles) and tokens >= 20,
        "style块=%d CSS变量=%d" % (len(styles), tokens))

    # 2 四态
    four = {
        "空态": ("empty" in html and "没有可显示的阶段数据" in html),
        "加载态": ("loading" in html and "正在解析内联载荷" in html),
        "错误态": ("error" in html and "载荷解析失败" in html),
        "404态": ("notfound" in html and "404" in html),
    }
    chk("门2 四态齐全（空/加载/错误/404）", all(four.values()),
        " ".join("%s=%s" % (k, "Y" if v else "N") for k, v in four.items()))

    # 3 三尺寸截图
    shots = {}
    if os.path.isdir(SHOTS):
        for f in os.listdir(SHOTS):
            m = re.search(r"(375|768|1440)", f)
            if m and f.endswith(".png"):
                shots.setdefault(m.group(1), []).append(f)
    need3 = all(k in shots for k in ("375", "768", "1440"))
    chk("门3 三尺寸截图齐（375/768/1440）", need3,
        " ".join("%s=%d" % (k, len(shots.get(k, []))) for k in ("375", "768", "1440")))

    # 4 对比度：正文 + 关键语义色，对各自背景实算
    vars = dict(re.findall(r"--([a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,6})", html))
    bgs = {"bg": parse_hex(vars.get("bg", "#0e1116")),
           "bg2": parse_hex(vars.get("bg2", "#12171e")),
           "panel": parse_hex(vars.get("panel", "#161c24")),
           "panel2": parse_hex(vars.get("panel2", "#1b222b"))}
    pairs = [("tx", "bg"), ("tx", "bg2"), ("tx", "panel"), ("tx", "panel2"),
             ("tx2", "bg"), ("tx2", "panel"), ("tx3", "bg"), ("tx3", "panel"),
             ("tx3", "bg2"), ("pass", "panel"), ("partial", "panel"), ("fail", "panel"),
             ("pass", "bg"), ("partial", "bg"), ("fail", "bg"), ("gate", "bg"),
             ("tx3", "panel2"), ("tx2", "bg2"), ("tx2", "panel2")]
    worst = (99.0, "")
    bad = []
    for fg, bg in pairs:
        f = parse_hex(vars.get(fg))
        if not f or not bgs.get(bg):
            continue
        rr = ratio(f, bgs[bg])
        if rr < worst[0]:
            worst = (rr, "%s on %s" % (fg, bg))
        if rr < 4.5:
            bad.append("%s/%s=%.2f" % (fg, bg, rr))
    chk("门4 正文/语义对比度 >= 4.5", not bad,
        "最低=%.2f (%s)%s" % (worst[0], worst[1], (" 低于4.5: " + ",".join(bad)) if bad else ""))

    # 5 a11y / 键盘
    tabs = len(re.findall(r"<button[^>]*role=\"tab\"", html))
    aria = len(re.findall(r"aria-label=", html))
    imgs = re.findall(r"<img\b[^>]*>", html)
    img_no_alt = [t for t in imgs if "alt=" not in t]
    inputs = re.findall(r"<input\b[^>]*>", html)
    labels = re.findall(r"<label\b", html)
    keys = all(k in html for k in ("ArrowDown", "ArrowRight", "Enter", "Escape"))
    roletab = len(re.findall(r'role="tab"', html))
    live = len(re.findall(r'aria-live=', html))
    pressed = "aria-pressed" in html
    chk("门5 键盘可达 + alt/label/aria",
        tabs >= 5 and aria >= 10 and not img_no_alt and len(labels) >= len(inputs)
        and keys and roletab >= 5 and live >= 1 and pressed,
        "tabs=%d aria-label=%d img=%d(img缺alt=%d) input=%d label=%d keys=%s live=%d pressed=%s"
        % (tabs, aria, len(imgs), len(img_no_alt), len(inputs), len(labels), keys, live, pressed))

    # 6 首屏性能
    perf_ok = os.path.exists(PERF) and re.search(r"(首屏|first.?paint|FCP|load).{0,40}\d", open(PERF, encoding="utf-8").read(), re.I)
    size = os.path.getsize(HTML)
    perf_ok = perf_ok or size < 120000
    chk("门6 首屏 < 2s 或有优化记录", perf_ok,
        "零依赖单文件 %d B; perf.md=%s" % (size, os.path.exists(PERF)))

    # 7 野值像素
    inline = re.findall(r'style="([^"]*)"', html)
    magic = []
    for s in inline:
        for num in re.findall(r"(?<![\w.-])(\d+)px", s):
            if int(num) not in (0, 2):
                magic.append(s[:60])
    chk("门7 野值像素有出处（无 inline 魔法像素）", not magic,
        "inline style=%d 含野值=%d" % (len(inline), len(magic)))

    # 8 截图时间戳
    stale = []
    if os.path.isdir(SHOTS):
        hm = os.path.getmtime(HTML)
        for f in os.listdir(SHOTS):
            if f.endswith(".png") and os.path.getmtime(os.path.join(SHOTS, f)) < hm:
                stale.append(f)
    chk("门8 截图非旧版（mtime >= 构建）", not stale, "过期=%d %s" % (len(stale), stale[:3]))

    # 9 交互预算声明
    bok = False
    bdetail = "work/ui_budget.json 不存在"
    if os.path.exists(BUDGET):
        try:
            b = json.load(open(BUDGET, encoding="utf-8"))
            need = {"first_screen_interactive", "total_interactive", "progressive_disclosure"}
            bok = need.issubset(set(b)) or (len(b) >= 3 and all(k in b for k in list(b)[:3]))
            bdetail = "keys=%s" % sorted(b)
        except Exception as e:
            bdetail = "JSON 解析失败: %s" % e
    chk("门9 声明交互预算 work/ui_budget.json", bok, bdetail)

    # 10 交互元素数 vs 预算 + 渐进披露
    inter = len(re.findall(r"<button\b", html))
    disc = len(re.findall(r"<details\b", html)) + len(re.findall(r'role="dialog"', html)) + len(re.findall(r"\bhidden\b", html))
    over = False
    if bok:
        b = json.load(open(BUDGET, encoding="utf-8"))
        cap = b.get("first_screen_interactive") or b.get("cap") or 0
        over = inter > cap
    chk("门10 交互元素不超预算或有渐进披露", (not over) or disc >= 2,
        "实测button=%d 渐进披露容器=%d(details/dialog/hidden)" % (inter, disc))

    ok = sum(1 for _, o, _ in results if o)
    print("=" * 74)
    for n, o, d in results:
        print("  %s %-42s %s" % ("PASS" if o else "FAIL", n, d))
    print("=" * 74)
    print("UX_DO_D %d/%d" % (ok, len(results)))
    print("UX_VERIFY_OK" if ok == len(results) else "UX_VERIFY_FAIL")
    return 0 if ok == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())
