#!/usr/bin/env python3
"""exitcap —— 退出码分类学与扫描器。

derived from: gptme/gptme (MIT, ★4456, https://github.com/gptme/gptme)
借鉴自 gptme v0.33.0 的 "non-interactive exit taxonomy"：
`gptme/cli/main.py::_classify_fatal_error` 把致命错误拆成
rate_limit(75) / auth_error(76) / model_unavailable(77) / generic(1)，
并落一条机器可读错误事件（error_class + exit_code）。

本工具做两件事：
  1) classify_fatal(exc_name, message)  —— 纯函数分类器（gptme 同款矩阵，可单测）
  2) scan_source() / scan_dir()        —— 扫本仓脚本里 sys.exit(N) 用到的码，
                                          没登记进 TAXONOMY 的码算「未分类」

退出码（本身就是被检验的那套分类学）：
  0  ok：全部码已登记
  2  usage：参数错
  3  verify-fail：发现未登记（未分类）的退出码
  4  need-human：--strict 下泛化码 rc=1 占比超阈值（该给人看）
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys

# ---------------------------------------------------------------------------
# 分类学（D1）：code -> (名字, 含义)
# 75/76/77/1 取自 gptme；2/3/4/124/130 是本仓既有约定，一并登记以形成闭环。
# ---------------------------------------------------------------------------
EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_USAGE = 2
EXIT_VERIFY = 3
EXIT_NEED_HUMAN = 4
EXIT_RATE_LIMIT = 75
EXIT_AUTH_ERROR = 76
EXIT_MODEL_UNAVAIL = 77
EXIT_TIMEOUT = 124
EXIT_INTERRUPTED = 130

TAXONOMY = {
    EXIT_OK: ("ok", "成功"),
    EXIT_GENERIC: ("generic", "未分类的通用失败（分类不出就该是它，绝不硬猜具体类）"),
    EXIT_USAGE: ("usage", "调用方参数/用法错"),
    EXIT_VERIFY: ("verify-fail", "判据没过：跑了但结论是不通过"),
    EXIT_NEED_HUMAN: ("need-human", "需要人或上游拍板，工具不替人决定"),
    EXIT_RATE_LIMIT: ("rate_limit", "限额/配额（gptme: 429、usage_limit_reached）"),
    EXIT_AUTH_ERROR: ("auth_error", "鉴权失败（gptme: AuthenticationError、401）"),
    EXIT_MODEL_UNAVAIL: ("model_unavailable", "模型不可用（gptme: 503、NotFoundError）"),
    EXIT_TIMEOUT: ("timeout", "超时（命令没跑完，不算通过）"),
    EXIT_INTERRUPTED: ("interrupted", "被用户/信号打断"),
}

# 泛化码：能用但不是「信息」的码（本仓大量脚本 rc=1 一把梭）
VAGUE_CODES = frozenset({EXIT_GENERIC})


# ---------------------------------------------------------------------------
# 分类器（D2：纯函数，无 IO，判不准就 generic）
# ---------------------------------------------------------------------------
def _is_rate_limit(exc_name: str, message: str) -> bool:
    n, m = exc_name.lower(), message.lower()
    return (
        "ratelimit" in n
        or "rate_limit" in n
        or "usage_limit_reached" in m
        or "429" in m
        or "too many requests" in m
    )


def _is_auth_error(exc_name: str, message: str) -> bool:
    n, m = exc_name.lower(), message.lower()
    return (
        "authentication" in n
        or "permission" in n
        or "invalid key" in m
        or "unauthor" in m
        or "401" in m
        or "403" in m  # 本仓扩展：gptme 只认 401，403 同样是「key 权限」类
    )


def _is_model_unavail(exc_name: str, message: str) -> bool:
    n, m = exc_name.lower(), message.lower()
    return (
        n in ("notfounderror", "serviceunavailableerror")
        or "model not found" in m
        or "service unavailable" in m
        or "503" in m
    )


# 顺序即优先级；改顺序会让 classify 的结果变，--selftest 的变异体专门验这一点。
RULES = (
    ("rate_limit", EXIT_RATE_LIMIT, _is_rate_limit),
    ("auth_error", EXIT_AUTH_ERROR, _is_auth_error),
    ("model_unavailable", EXIT_MODEL_UNAVAIL, _is_model_unavail),
)


def classify_fatal(
    exc_name: str, message: str, rules=RULES
) -> "tuple[str, int]":
    """(异常类型名, 消息) -> (错误类, 退出码)。纯函数：同入同出，无副作用。"""
    name = exc_name or ""
    msg = message or ""
    for cls, code, pred in rules:
        try:
            hit = pred(name, msg)
        except Exception:  # 规则本身炸了也不能中断分类（旁路容错，D3）
            hit = False
        if hit:
            return cls, code
    return "generic", EXIT_GENERIC


# ---------------------------------------------------------------------------
# 扫描器（D1 的下半场：仓库里真实用到的码，是否都在分类学里）
# ---------------------------------------------------------------------------
_EXIT_CALL = re.compile(
    r"(?:sys\.exit|os\._exit|raise\s+SystemExit|SystemExit)\s*\(\s*(\d+)\s*\)"
)


def _literal_int(node):
    """取 ast 常量里的 int；UnaryOp(负数)/Name(变量) 一律不认。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return node.value
    return None


def _scan_by_ast(text: str) -> "list[int]":
    """只认真正的 exit 调用；字符串里的样例代码不算（否则自检会自咬）。"""
    tree = ast.parse(text)
    codes: "list[int]" = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        fn = node.func
        is_exit = False
        if isinstance(fn, ast.Attribute) and fn.attr in ("exit", "_exit"):
            is_exit = True
        elif isinstance(fn, ast.Name) and fn.id == "SystemExit":
            is_exit = True
        if not is_exit:
            continue
        code = _literal_int(node.args[0])
        if code is not None:
            codes.append(code)
    return codes


def scan_source(text: str) -> "tuple[list[int], list[int]]":
    """返回 (已登记码, 未登记码)，各自去重升序。

    优先 AST（精确）；源码语法坏掉时退回正则（宁可多报也不错漏）。
    """
    text = text or ""
    try:
        raw_codes = _scan_by_ast(text)
    except SyntaxError:
        raw_codes = [int(r) for r in _EXIT_CALL.findall(text)]
    known: "set[int]" = set()
    unknown: "set[int]" = set()
    for code in raw_codes:
        (known if code in TAXONOMY else unknown).add(code)
    return sorted(known), sorted(unknown)


SKIP_DIRS = {"__pycache__", ".git", "node_modules", "_superseded"}


def scan_dir(root: str, max_files: int = 4000) -> dict:
    files = []
    files_seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            if fn.endswith(".bak") or ".bak-" in fn or ".bak." in fn:
                continue
            path = os.path.join(dirpath, fn)
            files_seen += 1
            if files_seen > max_files:
                break
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            known, unknown = scan_source(text)
            if not known and not unknown:
                continue
            files.append(
                {
                    "path": os.path.relpath(path, root),
                    "known": known,
                    "unknown": unknown,
                    "vague_only": bool(known) and not unknown and set(known) <= VAGUE_CODES,
                }
            )
    return {
        "root": os.path.abspath(root),
        "scanned": min(files_seen, max_files),
        "files": files,
        "unknown_codes": sorted({c for f in files for c in f["unknown"]}),
    }


# ---------------------------------------------------------------------------
# 自检（含变异体验牙）：变异体必须让结论翻转，否则说明用例没牙
# ---------------------------------------------------------------------------
SELFTEST_MATRIX = [
    ("ValueError", "Codex API error 429: usage_limit_reached", "rate_limit", EXIT_RATE_LIMIT),
    ("RateLimitError", "too many requests", "rate_limit", EXIT_RATE_LIMIT),
    ("ValueError", "429 Too Many Requests", "rate_limit", EXIT_RATE_LIMIT),
    ("AuthenticationError", "invalid key", "auth_error", EXIT_AUTH_ERROR),
    ("ValueError", "Codex API error 401: unauthorized", "auth_error", EXIT_AUTH_ERROR),
    ("ValueError", "401 Client Error: Unauthorized for url: https://api.example.com",
     "auth_error", EXIT_AUTH_ERROR),
    ("ValueError", "Codex API error 503: service unavailable", "model_unavailable",
     EXIT_MODEL_UNAVAIL),
    ("NotFoundError", "model not found", "model_unavailable", EXIT_MODEL_UNAVAIL),
    ("ServiceUnavailableError", "service down", "model_unavailable", EXIT_MODEL_UNAVAIL),
    ("InternalServerError", "upstream 500", "generic", EXIT_GENERIC),
    ("RuntimeError", "something unexpected", "generic", EXIT_GENERIC),
]


def _selftest() -> "tuple[bool, list[str]]":
    lines: "list[str]" = []
    ok = True

    # ① 分类矩阵逐条钉死
    for exc, msg, want_cls, want_code in SELFTEST_MATRIX:
        got = classify_fatal(exc, msg)
        good = got == (want_cls, want_code)
        ok = ok and good
        lines.append(
            "%s classify(%s, %r) -> %s %s"
            % ("PASS" if good else "FAIL", exc, msg[:34], got,
               "" if good else "（期望 %s）" % ((want_cls, want_code),))
        )

    # ② 变异体：删掉 rate_limit 规则后，429 用例必须落到 generic
    mutated = RULES[1:]
    got = classify_fatal("ValueError", "429 Too Many Requests", rules=mutated)
    good = got == ("generic", EXIT_GENERIC)
    ok = ok and good
    lines.append(
        "%s 变异体①：去掉 rate_limit 规则 → 429 降级 generic %s"
        % ("PASS" if good else "FAIL", got)
    )

    # ③ 变异体：规则顺序颠倒后，503+401 混合消息的归类必须变
    base = classify_fatal("ValueError", "401 unauthorized / 503 down")
    flipped = classify_fatal("ValueError", "401 unauthorized / 503 down", rules=tuple(reversed(RULES)))
    good = base[0] == "auth_error" and flipped[0] == "model_unavailable"
    ok = ok and good
    lines.append(
        "%s 变异体②：顺序颠倒后归类翻转 %s -> %s"
        % ("PASS" if good else "FAIL", base[0], flipped[0])
    )

    # ④ 扫描器：已登记 / 未登记 / 去重
    known, unknown = scan_source(
        "import sys\n"
        "if x:\n    sys.exit(75)\nelse:\n    sys.exit(99)\n"
        "raise SystemExit(75)\n"
    )
    good = known == [75] and unknown == [99]
    ok = ok and good
    lines.append("%s scan_source 分类正确 known=%s unknown=%s" % ("PASS" if good else "FAIL", known, unknown))

    # ⑤ 边界：空文本、负号/变量参数不被误抓
    known2, unknown2 = scan_source("sys.exit(0)\nsys.exit(code)\nsys.exit(-1)\n")
    good = known2 == [0] and unknown2 == []
    ok = ok and good
    lines.append("%s 扫描边界：变量/负数参数不误抓 known=%s" % ("PASS" if good else "FAIL", known2))

    # ⑥ 分类器不抛：极端输入
    try:
        got = classify_fatal(None, None)  # type: ignore[arg-type]
        good = got == ("generic", EXIT_GENERIC)
    except Exception as exc:  # noqa: BLE001
        got, good = repr(exc), False
    ok = ok and good
    lines.append("%s 分类器对 None 输入不抛 -> %s" % ("PASS" if good else "FAIL", got))

    return ok, lines


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="exitcap",
        description="退出码分类学扫描器（derived from gptme/gptme）",
    )
    ap.add_argument("--selftest", action="store_true", help="跑内置用例 + 变异体验牙")
    ap.add_argument("--scan", metavar="DIR", help="扫描目录下 .py 的 sys.exit(N)")
    ap.add_argument("--classify", nargs=2, metavar=("EXC_NAME", "MESSAGE"),
                    help="用分类器判一条异常")
    ap.add_argument("--json", metavar="PATH", help="把结果落盘为 JSON（落盘失败不影响 rc）")
    ap.add_argument("--strict", action="store_true",
                    help="存在未登记码或只有泛化 rc=1 的文件过多时给 rc=4")
    ap.add_argument("--vague-ratio", type=float, default=0.9,
                    help="--strict 下「只用泛化码」的文件占比阈值（默认 0.9）")
    args = ap.parse_args(argv)

    payload = {"tool": "exitcap", "taxonomy": {str(k): v[0] for k, v in TAXONOMY.items()}}

    if args.selftest:
        ok, lines = _selftest()
        for ln in lines:
            print(ln)
        payload["selftest"] = {"ok": ok, "passed": sum(1 for l in lines if l.startswith("PASS")),
                               "total": len(lines)}
        _dump(args.json, payload)
        if not ok:
            print("EXITCAP SELFTEST FAIL")
            return EXIT_VERIFY
        print("EXITCAP SELFTEST OK %d/%d" % (payload["selftest"]["passed"], len(lines)))
        return EXIT_OK

    if args.classify:
        cls, code = classify_fatal(args.classify[0], args.classify[1])
        print("%s -> %s (%d: %s)" % (args.classify[0], cls, code, TAXONOMY.get(code, ("?", ""))[1]))
        payload["classify"] = {"class": cls, "code": code}
        _dump(args.json, payload)
        return EXIT_OK

    if args.scan:
        if not os.path.isdir(args.scan):
            print("exitcap：不是目录：%s" % args.scan)
            return EXIT_USAGE
        rep = scan_dir(args.scan)
        payload["scan"] = rep
        for f in rep["files"]:
            mark = "UNCLASSIFIED" if f["unknown"] else ("VAGUE" if f["vague_only"] else "ok")
            print("[%-12s] %s known=%s unknown=%s" % (mark, f["path"], f["known"], f["unknown"]))
        n = len(rep["files"])
        vague = sum(1 for f in rep["files"] if f["vague_only"])
        print(
            "扫了 %d 个文件，命中退出码的 %d 个；未登记码=%s；只用泛化 rc=1 的 %d 个（%.0f%%）"
            % (rep["scanned"], n, rep["unknown_codes"] or "无", vague,
               100.0 * vague / n if n else 0.0)
        )
        _dump(args.json, payload)
        if rep["unknown_codes"]:
            return EXIT_VERIFY
        if args.strict and n and vague / n > args.vague_ratio:
            print("exitcap：泛化码占比过高，语义信息不足，建议人看一眼（rc=4）")
            return EXIT_NEED_HUMAN
        return EXIT_OK

    ap.print_help()
    return EXIT_USAGE


def _dump(path, payload) -> None:
    """旁路落盘（D3）：失败只 warn，绝不改变 rc 语义。"""
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
    except OSError as exc:  # noqa: BLE001
        print("exitcap：warn——结果没落盘（%s），但主结论不受影响" % exc, file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
