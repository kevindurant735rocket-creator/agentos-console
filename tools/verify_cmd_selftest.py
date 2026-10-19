#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_cmd_selftest —— 验证命令探测器的证伪集。

每个用例都按「坏样本必须被拒 / 好样本必须被认出」写：
探测器的错法有两种——**猜**（仓库里没有却建议）和**建议跑不动的**。
两条都判红。
"""

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import verify_cmd as vc  # noqa: E402

CASES = []


def case(name, ok, detail=""):
    CASES.append((name, bool(ok), str(detail)))
    return bool(ok)


def _proj(files):
    d = tempfile.mkdtemp(prefix="vcmd_")
    for name, content in files.items():
        p = os.path.join(d, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
    return d


# 1) 认得出各类仓库
_d = _proj({"Makefile": "test:\n\tpytest -q\n"})
case("Makefile 有 test 目标 → make test", (vc.detect_verify_cmd(_d) or {}).get("cmdline") == "make test",
     json.dumps(vc.detect_verify_cmd(_d), ensure_ascii=False))

# 2) Makefile 没有 test 目标 → 不猜
_d = _proj({"Makefile": "build:\n\techo hi\n"})
case("Makefile 无 test 目标 → 不建议 make test", vc.detect_verify_cmd(_d) is None,
     json.dumps(vc.detect_verify_cmd(_d), ensure_ascii=False))

# 3) package.json 没有 test 脚本 → 不猜
_d = _proj({"package.json": '{"name":"x"}'})
case("package.json 无 test 脚本 → 不建议 npm test", vc.detect_verify_cmd(_d) is None,
     json.dumps(vc.detect_verify_cmd(_d), ensure_ascii=False))

# 4) 空仓库 → 没有
case("空仓库 → 没有验证命令", vc.detect_verify_cmd(_proj({})) is None, "")

# 5) None / 空 root → 没有
case("root 为空 → 没有验证命令", vc.detect_verify_cmd(None) is None, "")
case("root 为空串 → 没有验证命令", vc.detect_verify_cmd("") is None, "")

# 6) 建议出来的命令本机真能跑（跑不动的建议比没建议更坏）
_d = _proj({"Makefile": "test:\n\techo ok\n"})
_info = vc.detect_verify_cmd(_d) or {}
case(
    "可用性：建议的命令本机真能跑（shutil.which 判）",
    bool(_info.get("cmd")) and vc._cmd_available(_info["cmd"]),
    json.dumps(_info, ensure_ascii=False),
)
case(
    "可用性：不存在的命令判为不可用",
    not vc._cmd_available(["definitely-not-a-real-cmd-xyz", "--x"]),
    "",
)

# 7) 逃生阀
_os_env = os.environ.get("AGENTOS_NO_VERIFY")
try:
    os.environ["AGENTOS_NO_VERIFY"] = "1"
    _d = _proj({"Makefile": "test:\n\techo ok\n"})
    case("逃生阀：AGENTOS_NO_VERIFY=1 → 不给建议", vc.detect_verify_cmd(_d) is None, "")
finally:
    if _os_env is None:
        os.environ.pop("AGENTOS_NO_VERIFY", None)
    else:
        os.environ["AGENTOS_NO_VERIFY"] = _os_env

# 8) describe 说人话
case(
    "describe：有信息时给出命令与依据",
    "make test" in vc.describe({"cmdline": "make test", "why": "Makefile 存在"}),
    vc.describe({"cmdline": "make test", "why": "Makefile 存在"}),
)
case("describe：无信息时是空串", vc.describe(None) == "", "")

bad = [c for c in CASES if not c[1]]
for name, ok, detail in CASES:
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, ("  -- " + detail) if detail else ""))
print("\nVERIFY_CMD_SELFTEST %d/%d" % (len(CASES) - len(bad), len(CASES)))
if bad:
    print("VERIFY_CMD_SELFTEST_RED")
    sys.exit(1)
print("VERIFY_CMD_SELFTEST_OK")
