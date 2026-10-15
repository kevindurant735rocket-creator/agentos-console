#!/usr/bin/env python3
# D64 谓词上牙（谓词 teeth test）：给 neg_a11y.py 末尾「读屏证据位零写入」
# 自检的**判据函数** ev_is_variant() 装三颗牙。
#
# 为什么单独成文件而不是跑整条 neg_a11y：整条门要注入 9 个变异体、每个都调
# a11y_walk.py 跑真键盘事件+真 AX 树，代价是分钟级；而这次要验的判据是个
# 纯函数「读一份 report 看 target 是不是指向变异体目录」，秒级就该能证伪。
#
# 不 import neg_a11y（它顶层就把 9 个变异体跑起来了），改为**从源文件里
# 抽出那一个函数定义**再 exec —— 验的是盘上真实代码，不是抄一遍的副本。
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(HERE, "neg_a11y.py")

src = open(SRC, encoding="utf-8").read()
m = re.search(r"^def ev_is_variant\(.*?(?=^\S)", src, re.S | re.M)
if not m:
    print("FAIL: neg_a11y.py 里找不到 ev_is_variant 定义（判据被删/改名？）")
    sys.exit(2)
ns = {"json": json}
exec(compile(m.group(0), SRC, "exec"), ns)
pred = ns["ev_is_variant"]

td = tempfile.mkdtemp(prefix="d64-teeth-")
CASES = [
    ("变异体报告必须判红",
     {"target": "file:///var/folders/x/neg-a11y-abc123/index.html"}, True),
    ("正版 file:// 报告必须放行",
     {"target": "file://" + os.path.join(ROOT, "index.html")}, False),
    ("正版 served-http 报告必须放行",
     {"target": "http://127.0.0.1:8790/index.html", "mode": "served-http"}, False),
]
rc_all = 0
try:
    for i, (name, rep, want) in enumerate(CASES):
        p = os.path.join(td, "neg-report-%d.json" % i)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False)
        got = pred(p)
        ok = (got is want)
        print("  [%s] %s -> %r（期望 %r）"
              % ("ok  " if ok else "FAIL", name, got, want))
        if not ok:
            rc_all = 1
    # 第四颗牙：证据位根本读不出来时，判据必须**非 False**（让调用方能判红），
    # 而不是静悄悄当成「没污染」放行。
    bad = os.path.join(td, "not-json.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("{ this is not json")
    got = pred(bad)
    ok = (got is not False)
    print("  [%s] 读不出来的证据位必须可判红 -> %r" % ("ok  " if ok else "FAIL", got))
    if not ok:
        rc_all = 1
finally:
    shutil.rmtree(td, ignore_errors=True)
print("== D64 谓词上牙 %s  %d/4 颗牙咬住 =="
      % ("OK" if rc_all == 0 else "FAIL", 4 - rc_all))
sys.exit(rc_all)
