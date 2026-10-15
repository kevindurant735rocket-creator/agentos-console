#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""at_selftest.py 的反向门 D85：证明这套 AT 自证不是「怎么都绿」。

at_selftest 自己的性质是「桩注入」——它证明 probe_at 的正向路径**能**走通。
但一个只会发绿的门同样可能是空门：真跑时 osascript 一挂、桩注入的分支没人
碰、条件写错了 —— 它照样全绿。所以本门把 probe_at 的四条判据各放松一次
（源码文本级变异，跑在 sys.modules 预注册的变异副本上，不落盘、不碰真文件），
要求 at_selftest 必须**红在对应那条反例上**：

  M1 放松「前台必须是浏览器」        => 必须红在 N2
  M2 放松「窗口标题必须命中被测页」  => 必须红在 N3
  M3 放松「AX 元素数不得为 0」       => 必须红在 N4
  M4 抽掉「计数不可解析」兜底        => 必须红在 N5

红在别处 = 判据错位（那道变异其实没破坏语义，或破坏的是别的判据）；
rc 不等于 1 = 变异体没跑起来（量具被杀，不是结论）；无 FAIL 行 = 空门。
产出 work/neg_at_selftest.json。
"""
import hashlib
import io
import json
import os
import sys
import types
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
REPORT = os.path.join(ROOT, "work", "neg_at_selftest.json")
sys.path.insert(0, HERE)

SRC_PATH = os.path.join(HERE, "a11y_human.py")
_SRC = open(SRC_PATH, encoding="utf-8").read()

# D85b 自纠：变异体的报告必须与真跑证据位**分路径**。
# 第一版让变异体写 at_selftest.REPORT 再 os.unlink 掉，理由是「别让假报告盖掉
# 真证据」—— 但两者同路径，删掉的就是真跑证据位本身。实测后果：正向门刚跑出
# work/at_selftest.json，反向门一跑就没了，27 门全绿的当轮连物证都不剩。
# 这恰恰是本仓 doctrine 要堵的「无物证判绿」，只不过这次是我自己造的。
MUT_REPORT = os.path.join(ROOT, "work", "neg_at_selftest.mutant.json")
REAL_REPORT = os.path.join(ROOT, "work", "at_selftest.json")

MUTANTS = [
    ("M1 放松「前台必须是浏览器」",
     "if not any(t in app.lower() for t in _BROWSER_TOKENS):",
     "if False:", "N2"),
    ("M2 放松「窗口标题必须命中被测页」",
     "if not any(token in t for t in titles):",
     "if False:", "N3"),
    ("M3 放松「AX 元素数不得为 0」",
     "    if n == 0:", "    if False:", "N4"),
    ("M4 抽掉「计数不可解析」兜底（脏输出直接当通过）",
     '    except Exception:\n        return False, "AT-UNVERIFIED", '
     '"%s · 计数不可解析为整数" % detail',
     "    except Exception:\n        n = 1", "N5"),
]

_ERR_NO_POINT = "源码里找不到变异点：a11y_human.py 已改版，本变异体失效，" \
                "必须重写而不是跳过"
_ERR_NOT_RED = "期望 at_selftest rc=1（判红），实得 rc=%s"
_ERR_NO_FAIL = "at_selftest 输出里没有 FAIL 行 —— 它在变异体下仍全绿，" \
               "即判据被掏空"
_ERR_MISPLACE = "红了但红在 %s 上，不在指定的 %s 上 => 判据错位"


def _run_mutant(name, old, new, case):
    if old not in _SRC:
        return {"tag": name, "mutates_case": case, "ok": False,
                "rc": "NO-MUTATION-POINT",
                "errs": [_ERR_NO_POINT + " (%r)" % old[:60]]}
    code = compile(_SRC.replace(old, new, 1), "a11y_human_mut.py", "exec")
    mod = types.ModuleType("a11y_human")
    mod.__file__ = SRC_PATH
    sys.modules["a11y_human"] = mod
    buf = io.StringIO()
    errs = []
    try:
        with redirect_stdout(buf):
            exec(code, mod.__dict__)
        sys.modules.pop("at_selftest", None)
        import at_selftest
        with redirect_stdout(buf):
            rc = at_selftest.main(report_path=MUT_REPORT)
        if os.path.isfile(MUT_REPORT):
            os.unlink(MUT_REPORT)
    except Exception as e:  # noqa: BLE001
        rc = "EXC:%s" % e
        errs.append("变异体执行期异常，量具被杀: %r" % (e,))
    out = buf.getvalue()
    reds = [l.strip() for l in out.splitlines()
            if l.strip().startswith("FAIL")]
    hit = any(case in l for l in reds)
    if rc != 1:
        errs.append(_ERR_NOT_RED % rc)
    if not reds:
        errs.append(_ERR_NO_FAIL)
    if reds and not hit:
        errs.append(_ERR_MISPLACE % (reds, case))
    return {"tag": name, "mutates_case": case, "ok": not errs, "rc": rc,
            "reds": reds, "hits_expected_case": hit, "errs": errs}


def main():
    if os.path.isfile(MUT_REPORT):
        os.unlink(MUT_REPORT)
    # 真跑证据位的指纹：反向门跑完必须一模一样（存在 + sha 不变），
    # 否则本门自己就在毁物证，必须判红而不是照绿。
    before = (hashlib.sha256(open(REAL_REPORT, "rb").read()).hexdigest()
              if os.path.isfile(REAL_REPORT) else None)
    items = [_run_mutant(*m) for m in MUTANTS]
    after = (hashlib.sha256(open(REAL_REPORT, "rb").read()).hexdigest()
             if os.path.isfile(REAL_REPORT) else None)
    ev = {"tag": "E0 反向门不得毁掉正向门的真跑证据位",
          "mutates_case": "work/at_selftest.json",
          "ok": before is not None and before == after,
          "rc": "sha=%s" % (after[:12] if after else "MISSING"),
          "reds": [], "hits_expected_case": True,
          "errs": ([]
                   if before is not None and before == after else
                   ["work/at_selftest.json 跑前 %s 跑后 %s —— 反向门动了真证据位"
                    % (before[:12] if before else "MISSING",
                       after[:12] if after else "MISSING")])}
    items.insert(0, ev)
    if os.path.isfile(MUT_REPORT):
        os.unlink(MUT_REPORT)
    red = [i for i in items if not i["ok"]]
    report = {"title": "at_selftest 反向门", "mutants": len(items),
              "passed": len(items) - len(red), "items": items,
              "how": "sys.modules 预注册变异副本，源码文本级替换，不落盘"}
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("=" * 70)
    print("NEG_AT_SELFTEST  at_selftest 反向门（放松判据必须被抓）")
    print("=" * 70)
    for i in items:
        print("  %s %-40s -> rc=%s 红在%s"
              % ("PASS" if i["ok"] else "FAIL", i["tag"], i["rc"],
                 i.get("mutates_case", "?")))
        for e in i["errs"]:
            print("       !! %s" % e)
    print("-" * 70)
    print("evidence: work/neg_at_selftest.json")
    if red:
        print("NEG_AT_SELFTEST FAIL %d/%d" % (len(red), len(items)))
        return 1
    print("NEG_AT_SELFTEST OK %d/%d 变异体 %d/%d 全部抓住"
          % (len(items), len(items), len(items), len(items)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
