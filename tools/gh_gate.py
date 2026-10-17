#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gh_gate —— GitHub 原生链接面硬门：BASE 必须绿，3 个变异体必须红，零远端写自证。

为什么必须有这个门
------------------
AgentOS 的 GitHub 面（`agentos gh` → tools/gh_client.py）自称只读：白名单收口
run_gh()、直连兜底只发 HTTP GET。但「自称只读」和「结构上发不出写」之间还差一层
可执行的证据；而且「gh 在不在 / 认证没认证 / 调用通不通」这三件事任何一件漂移，
只读面都会悄悄变成空壳或假绿。本门站在外面认这些事：

  BASE  零参数生产调用必须 rc=0 且末行打印 GH_GATE OK <n>/<n>：
         · shutil.which("gh") 非空；
         · 真跑 gh api rate_limit，resources.core.limit 必须 >0；
         · 真跑项目只读面 python3 tools/agentos.py gh repos，rc=0 且
           「仓库数: N」的 N>0；
         · gh auth status rc=0 且 stdout 含 "Logged in"；
         · 零远端写：AST 扫描本门 + agentos.py + gh_client.py 里所有进程派生
           调用点（subprocess.* / run_gh / api_get），argv 字面量里出现写动词
           （gh repo create / git push / gh api -X POST|PATCH|PUT|DELETE 等）即红。
  M1    PATH 里没有 gh（剥掉所有含 gh 可执行文件的 PATH 目录）→ 门必须判红。
  M2    gh 未认证（隔离 HOME + GH_CONFIG_DIR 指向空目录 + GH_TOKEN 清空 +
        GH_HOST=github.com）→ 门必须判红。注意匿名 rate_limit 仍 rc=0
        （GitHub 允许匿名查限流，limit=60），所以 M2 的红必须靠 auth status。
  M3    只读调用必失败（临时目录放一个凡调用一律 exit 1 的假 gh 脚本，顶到
        PATH 最前；桩故意打印 `net/http: TLS handshake timeout` 这种**传输层
        瞬时错误字样**）→ 门必须判红。它同时证明 rate_limit 探针的传输层重试
        不会把真红洗成绿。

传输层抖动 vs 真红（2026-10-10 事故）：41 门并发跑批时 `gh api rate_limit`
偶发 `net/http: TLS handshake timeout`，一次网络抖动把整条链判红。探针
probe_rate_limit() 因此只对**传输层**字样做有界重试（RL_TRIES=3，1.5s 退避），
rc=0 但 JSON 不对 / 假 gh / 认证问题一律不重试，重试耗尽仍如实判红。
判据自证：谓词必须认得出事故字样、认不出真失败字样（见 main() 的自证门）。

变异体判红的双重口径（量具自己崩掉也给非 0，那是量具死了不是判据红）：
每个变异体要求子进程 rc≠0 且子进程自己落的 JSON 里 rc≠0 且 gates 里至少一条
FAIL —— 子进程必须活到给出红的结论，而不是半路死掉。

判据自证：M1/M2/M3 三条「必须红」写进 JSON 的 gates 数组（各带 ok/detail）。
零污染双自查：tools/ 下无 _ghmut_* 残留；生产 JSON 在 3 个变异体跑动前后字节
不变（变异体的桩与输出只落临时目录，跑完即清）。

用法: python3 tools/gh_gate.py [--out PATH]   # rc=0 全过 / 2 有漏网 / 3 落盘失败
"""

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SELF = os.path.abspath(__file__)
ENTRY = os.path.join(HERE, "agentos.py")
GH_CLIENT = os.path.join(HERE, "gh_client.py")
SCAN_FILES = (SELF, ENTRY, GH_CLIENT)
DEFAULT_OUT = os.path.join(ROOT, "work", "gh_gate.json")
MUT_PREFIX = "_ghmut_"
MUT_ENV = "GH_GATE_MUTANT"
TIMEOUT = 120

# 传输层瞬时错误特征（2026-10-10 实测事故）：全链 41 门并发跑批时，
# `gh api rate_limit` 偶发 `net/http: TLS handshake timeout`（rc=1），
# 一次网络抖动就把整条链判红 —— 而同一秒单跑本门是 RC=0。这类抖动不是
# 「GitHub 只读面坏了」的证据，所以只对**传输层**错误做有界重试。
# 重试**不允许洗绿**：rc=0 但 JSON 不对、认证问题、假 gh 一律不重试，
# 重试耗尽仍按原口径如实判红（M3 变异体就是打印这种字样仍必须红的自证）。
TRANSIENT_RE = re.compile(
    r"tls handshake timeout|tls handshake|connection reset|connection refused|"
    r"i/o timeout|dial tcp|no such host|unexpected eof|server misbehaving|"
    r"temporary failure in name resolution|proxyconnect|"
    r"50[234] (bad gateway|service unavailable|gateway timeout)",
    re.I,
)
RL_TRIES = 3

# 零远端写判据：这些模式出现在任何进程派生调用的 argv 字面量里 = 有写向量。
# AST 扫描只读调用点的字面量 argv，本表自身的正则串不会成为扫描对象。
WRITE_RES = [
    re.compile(p)
    for p in (
        r"gh\s+repo\s+create",
        r"gh\s+repo\s+delete",
        r"gh\s+pr\s+create",
        r"gh\s+issue\s+create",
        r"gh\s+release\s+create",
        r"gh\s+api\s+\S+\s+-X\s*(POST|PATCH|PUT|DELETE)",
        r"gh\s+api\s+\S+\s+--method[=\s]\s*(POST|PATCH|PUT|DELETE)",
        r"git\s+push",
        r"git\s+commit",
        r"git\s+reset\s+--hard",
    )
]

# 进程派生收口：标准库 spawn 函数 + gh_client 的唯一收口 run_gh/api_get。
SPAWN_FUNCS = {
    "run",
    "call",
    "check_call",
    "check_output",
    "Popen",
    "system",
    "getoutput",
    "getstatusoutput",
    "run_gh",
    "api_get",
}

results = []


def gate(name, ok, detail=""):
    results.append({"gate": name, "ok": bool(ok), "detail": one_line(detail)})
    print(
        ("  [ok  ] " if ok else "  [FAIL] ")
        + name
        + (("  " + one_line(detail)) if detail else "")
    )


def one_line(s):
    return " ".join(str(s).split())


def run(argv, env=None, timeout=TIMEOUT, cwd=None):
    """跑子进程；超时/起不来一律转成非 0 的伪结果，绝不静默成功。

    stdin 一律 DEVNULL：让被检 CLI 的「--yes 必须 TTY」判据在门里走非交互
    分支（否则从交互终端直接跑本门时，写面确认提示会吃 stdin 把门挂住）。
    """
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as e:
        out = e.stdout or b""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return subprocess.CompletedProcess(argv, 124, out, "TIMEOUT")
    except Exception as e:  # noqa: BLE001
        return subprocess.CompletedProcess(argv, 125, "", repr(e))


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def scan_write_hits(path):
    """AST 扫描：所有进程派生调用点的字面量 argv，命中写动词模式即记一条。"""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = (
            fn.attr
            if isinstance(fn, ast.Attribute)
            else (fn.id if isinstance(fn, ast.Name) else "")
        )
        if name not in SPAWN_FUNCS or not node.args:
            continue
        a0 = node.args[0]
        toks = []
        if isinstance(a0, (ast.List, ast.Tuple)):
            for el in a0.elts:
                if isinstance(el, ast.Constant) and isinstance(el.value, str):
                    toks.append(el.value)
        elif isinstance(a0, ast.Constant) and isinstance(a0.value, str):
            toks.append(a0.value)
        blob = " ".join(toks)
        for rx in WRITE_RES:
            m = rx.search(blob)
            if m:
                hits.append(
                    "%s:%d %s() argv 命中 %r"
                    % (os.path.basename(path), node.lineno, name, m.group(0))
                )
    return hits


def probe_rate_limit(gh):
    """真跑 gh api rate_limit；只对传输层瞬时错误做有界重试。返回 (ok, detail)。

    重试边界必须窄：只有 stderr/stdout 命中 TRANSIENT_RE 才重试，且最多
    RL_TRIES 次。rc=0 但 JSON 结构不对、假 gh（exit 1 无传输层字样）、
    认证问题一律**不重试**，直接判红 —— 重试只吃网络抖动，不会把
    「GitHub 只读面真坏了」洗成绿（M3 变异体就是打印传输层字样仍必须红）。
    """
    note = ""
    for attempt in range(1, RL_TRIES + 1):
        p = run([gh, "api", "rate_limit"])
        if p.returncode == 0:
            try:
                limit = json.loads(p.stdout)["resources"]["core"]["limit"]
                ok = (
                    isinstance(limit, int) and not isinstance(limit, bool) and limit > 0
                )
                return ok, "rc=0 limit=%r%s" % (limit, note)
            except Exception as e:  # noqa: BLE001
                return False, "JSON 解析失败 %r" % (e,)
        err = "%s %s" % (p.stderr or "", p.stdout or "")
        if attempt < RL_TRIES and TRANSIENT_RE.search(err):
            note = "（传输层瞬时错误，已重试 %d 次）" % attempt
            time.sleep(1.5 * attempt)
            continue
        return False, "rc=%s stderr=%r%s" % (
            p.returncode,
            p.stderr.strip()[-160:],
            note,
        )
    return False, "重试耗尽%s" % note


def probe_repos():
    """真跑项目只读面 `agentos.py gh repos`；只对传输层瞬时错误做有界重试。

    2026-10-10 第二次同源踩实：44 门长链跑到本门时，唯一红的判据就是这条 ——
    `gh api rate_limit` 有传输层重试所以活着，`gh repo list` 没有，一次
    `net/http: TLS handshake timeout` 就把 BASE 判红；而同一分钟单跑本门是
    GH_GATE OK 11/11、rc=0。这与上面 probe_rate_limit 的注释记的是同一类事故，
    只是当时只给 rate_limit 装了重试，漏了这条。

    重试边界与 probe_rate_limit 完全同款：只有 stdout/stderr 命中 TRANSIENT_RE
    才重试，最多 RL_TRIES 次；rc=0 但「仓库数」行不对、认证问题（M2）、
    假 gh（M3，打印传输层字样但仍是真红）一律不洗绿，重试耗尽仍如实判红。
    """
    note = ""
    for attempt in range(1, RL_TRIES + 1):
        p = run([sys.executable, ENTRY, "gh", "repos"], cwd=ROOT)
        if p.returncode == 0:
            m = re.search(r"仓库数:\s*(\d+)", p.stdout)
            ok = bool(m) and int(m.group(1)) > 0
            return ok, "rc=0 %s%s" % (m.group(0) if m else "无「仓库数」行", note)
        err = "%s %s" % (p.stderr or "", p.stdout or "")
        if attempt < RL_TRIES and TRANSIENT_RE.search(err):
            note = "（传输层瞬时错误，已重试 %d 次）" % attempt
            time.sleep(1.5 * attempt)
            continue
        return False, "rc=%s%s" % (p.returncode, note)
    return False, "重试耗尽%s" % note


def check_base():
    """BASE 四件事实，各自带 ok + detail。"""
    gh = shutil.which("gh")
    which = (gh is not None, gh or "None")

    rl_ok, rl_detail = False, "未测（which 失败）"
    if gh:
        rl_ok, rl_detail = probe_rate_limit(gh)

    repos_ok, repos_detail = probe_repos()

    auth_ok, auth_detail = False, "未测"
    if gh:
        p = run([gh, "auth", "status"])
        auth_ok = p.returncode == 0 and "Logged in" in p.stdout
        auth_detail = "rc=%s %s" % (
            p.returncode,
            "含 'Logged in'" if "Logged in" in p.stdout else "stdout 无 'Logged in'",
        )
    return {
        "which": which,
        "rate_limit": (rl_ok, rl_detail),
        "repos": (repos_ok, repos_detail),
        "auth": (auth_ok, auth_detail),
    }


def check_zero_write():
    hits = []
    for path in SCAN_FILES:
        if os.path.exists(path):
            hits.extend(scan_write_hits(path))
    return hits


def env_without_gh():
    """M1：构造一个不含 gh 的 PATH（剥掉所有存在 gh 可执行文件的目录）。"""
    env = dict(os.environ)
    keep = [
        d
        for d in env.get("PATH", "").split(os.pathsep)
        if d and not os.path.exists(os.path.join(d, "gh"))
    ]
    env["PATH"] = os.pathsep.join(keep)
    return env


def env_unauthenticated(td):
    """M2：隔离 HOME + GH_CONFIG_DIR 指向空目录 + GH_TOKEN 清空 + GH_HOST 固定。"""
    env = dict(os.environ)
    home = os.path.join(td, "m2-home")
    cfg = os.path.join(td, "m2-ghcfg")
    os.makedirs(home, exist_ok=True)
    os.makedirs(cfg, exist_ok=True)
    env["HOME"] = home
    env["GH_CONFIG_DIR"] = cfg
    env["GH_TOKEN"] = ""
    env["GH_ENTERPRISE_TOKEN"] = ""
    env["GH_HOST"] = "github.com"
    return env


def env_fake_gh(td):
    """M3：假 gh 脚本（凡调用一律 exit 1）顶到 PATH 最前。

    桩故意打印**传输层瞬时错误字样**（`net/http: TLS handshake timeout`）：
    这样 M3 同时证明两件事 —— 假 gh 必须判红，且 probe_rate_limit 的传输层
    重试**不会把真红洗成绿**（重试 3 次耗尽仍红）。若有人把重试放宽成
    「任何失败都重试/最终放行」，这条会当场红。
    """
    bindir = os.path.join(td, "m3-bin")
    os.makedirs(bindir, exist_ok=True)
    fake = os.path.join(bindir, "gh")
    with open(fake, "w", encoding="utf-8") as f:
        f.write(
            "#!/bin/sh\n"
            'echo \'Get "https://api.github.com/rate_limit": '
            "net/http: TLS handshake timeout' >&2\n"
            "exit 1\n"
        )
    os.chmod(fake, 0o755)
    env = dict(os.environ)
    env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    return env


def run_mutant(tag, env, td):
    """把本门当子进程在变异环境里真跑一遍，要求它活着给出红的结论。

    返回 (caught, detail, child_rc, child_json_rc)。只认 rc≠0 不够：量具崩了
    也是非 0。所以同时要求子进程自己落的 JSON 里 rc≠0 且 gates 有 FAIL。
    """
    out_path = os.path.join(td, "mutant-%s.json" % tag)
    child_env = dict(env)
    child_env[MUT_ENV] = tag
    p = run([sys.executable, SELF, "--out", out_path], env=child_env)
    mdoc = load_json(out_path)
    failed = [
        g.get("gate") for g in ((mdoc or {}).get("gates") or []) if not g.get("ok")
    ]
    caught = (
        p.returncode != 0
        and isinstance(mdoc, dict)
        and mdoc.get("rc") != 0
        and bool(failed)
    )
    detail = "child_rc=%s json_rc=%s 失败门=%s" % (
        p.returncode,
        (mdoc or {}).get("rc"),
        failed[:3] or "无",
    )
    return caught, detail, p.returncode, (mdoc or {}).get("rc")


def parse_out():
    args = sys.argv[1:]
    if "--out" in args:
        i = args.index("--out")
        if i + 1 < len(args):
            return args[i + 1]
    return DEFAULT_OUT


def write_json(out_path, rc):
    doc = {"tool": "gh_gate.py", "rc": rc, "gates": results}
    d = os.path.dirname(os.path.abspath(out_path))
    try:
        os.makedirs(d, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
    except Exception as e:  # noqa: BLE001
        print("gh_gate: 落盘失败 %r" % (e,), file=sys.stderr)
        return 3
    return rc


def finish():
    nfail = sum(1 for r in results if not r["ok"])
    ok = nfail == 0
    print(
        "== GH_GATE %s  %d/%d 门通过（BASE 四件事实 + 零写扫描 + 重试谓词自证 + 3 变异体 + 零污染）=="
        % ("OK" if ok else "FAIL", len(results) - nfail, len(results))
    )
    if ok:
        print("GH_GATE OK %d/%d" % (len(results) - nfail, len(results)))
    else:
        print("GH_GATE FAIL %d/%d" % (len(results) - nfail, len(results)))
    return 0 if ok else 2


def main():
    out_path = parse_out()
    is_child = bool(os.environ.get(MUT_ENV))

    base = check_base()
    gate("BASE gh 可解析（shutil.which('gh') 非空）", *base["which"])
    gate("BASE gh api rate_limit rc=0 且 resources.core.limit>0", *base["rate_limit"])
    gate("BASE agentos.py gh repos rc=0 且「仓库数: N」N>0", *base["repos"])
    gate("BASE gh auth status rc=0 且 stdout 含 'Logged in'", *base["auth"])

    hits = check_zero_write()
    gate(
        "BASE 读面零写向量（AST 扫描 %d 个文件，写只许走 run_gh_write 收口）"
        % len(SCAN_FILES),
        not hits,
        "命中=%s" % (hits[:3] or "无"),
    )

    # ── W 组（R1 写面）：策略矩阵 + CLI 拒绝路径 + dry-run 预演 + 审计链 ──
    # 写面三闸的「有牙」证据：四类未授权写必须在策略层被拒（W1 矩阵 / W3
    # 无 --yes / W4 非交互 --yes / W5 白名单外旗标），预演零网络可达（W6），
    # 审计链自洽且篡改必断（W7/W8）。这组在 M1/M2/M3 变异体子进程里同样
    # 要绿 —— 写面闸不依赖 gh 可执行/认证（拒在策略层），BASE 红不连坐。
    sys.path.insert(0, HERE)
    try:
        import gh_client as _gc

        w_cases = [
            (
                dict(
                    has_yes=False,
                    tty=True,
                    write_enabled="true",
                    repo="o/r",
                    exec_rows=[],
                ),
                False,
                "no-yes",
            ),
            (
                dict(
                    has_yes=True,
                    tty=False,
                    write_enabled="true",
                    repo="o/r",
                    exec_rows=[],
                ),
                False,
                "non-tty",
            ),
            (
                dict(
                    has_yes=True,
                    tty=True,
                    write_enabled="yes",
                    repo="o/r",
                    exec_rows=[],
                ),
                False,
                "config-off",
            ),
            (
                dict(
                    has_yes=True,
                    tty=True,
                    write_enabled="True",
                    repo="o/r",
                    exec_rows=[],
                ),
                False,
                "config-off",
            ),
            (
                dict(
                    has_yes=True,
                    tty=True,
                    write_enabled="true",
                    repo="o/r",
                    exec_rows=[(time.time(), "o/r")] * 5,
                ),
                False,
                "rate-limit",
            ),
            (
                dict(
                    has_yes=True,
                    tty=True,
                    write_enabled="true",
                    repo="o/r",
                    exec_rows=[(time.time() - 7200, "o/r")] * 9,
                ),
                True,
                "ok",
            ),
            (
                dict(
                    has_yes=True,
                    tty=True,
                    write_enabled="true",
                    repo="o/other",
                    exec_rows=[(time.time(), "o/r")] * 9,
                ),
                True,
                "ok",
            ),
        ]
        w_bad = []
        for kw, want, why in w_cases:
            got, rsn = _gc.write_policy(**kw)
            if got != want or rsn != why:
                w_bad.append(
                    "%s→(%s,%s) 期望(%s,%s)"
                    % (list(kw.values())[0], got, rsn, want, why)
                )
        gate(
            "W1 写面策略矩阵：无--yes/非TTY/配置关(含大小写陷阱)/限速 四拒、"
            "陈旧写与他仓写不误限",
            not w_bad,
            "; ".join(w_bad) or "7 例全对",
        )
        try:
            _gc.run_gh_write(["repo", "delete", "o/r"])
            gate("W2 写收口模板复核：模板外向量必须 ValueError", False, "未抛")
        except ValueError:
            gate("W2 写收口模板复核：模板外向量必须 ValueError", True)
    except Exception as e:  # noqa: BLE001
        gate("W1/W2 gh_client 可导入", False, repr(e))

    w_cli = [sys.executable, ENTRY, "gh"]

    def w_audit_tail(kinds):
        p = os.path.join(ROOT, "work", "gh_audit.jsonl")
        if not os.path.exists(p):
            return []
        rows = []
        with open(p, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        rows.append(json.loads(ln))
                    except ValueError:
                        pass
        return [r for r in rows if r.get("kind") in kinds]

    r_den = run(w_cli + ["issue-comment", "octo/demo", "1", "--body", "gate-probe"])
    den = w_audit_tail(("DENY",))
    gate(
        "W3 写面 CLI：无 --yes rc≠0 且审计 DENY(no-yes)",
        r_den.returncode != 0 and den and den[-1].get("note") == "no-yes",
        "rc=%s tail=%s" % (r_den.returncode, den[-1].get("note") if den else None),
    )
    r_yes = run(
        w_cli + ["issue-comment", "octo/demo", "1", "--body", "gate-probe", "--yes"]
    )
    yesr = w_audit_tail(("DENY",))
    gate(
        "W4 写面 CLI：--yes 非交互 rc≠0 且审计 DENY(non-tty)（SEC S1）",
        r_yes.returncode != 0 and yesr and yesr[-1].get("note") == "non-tty",
        "rc=%s tail=%s" % (r_yes.returncode, yesr[-1].get("note") if yesr else None),
    )
    r_flag = run(
        w_cli
        + [
            "issue-create",
            "octo/demo",
            "--title",
            "t",
            "--body",
            "b",
            "-F",
            "/etc/hosts",
        ]
    )
    gate(
        "W5 写面 CLI：白名单外旗标 -F 结构性拒绝（SEC T1）",
        r_flag.returncode != 0,
        "rc=%s" % r_flag.returncode,
    )
    r_dry = run(
        w_cli + ["issue-comment", "octo/demo", "1", "--body", "gate-probe", "--dry-run"]
    )
    dryr = w_audit_tail(("EXEC-DRY",))
    gate(
        "W6 写面 CLI：--dry-run rc=0 且「将执行」且审计 EXEC-DRY",
        r_dry.returncode == 0 and "将执行" in (r_dry.stdout or "") and bool(dryr),
        "rc=%s" % r_dry.returncode,
    )
    r_av = run(w_cli + ["audit", "--limit", "3"])
    gate(
        "W7 审计哈希链自洽（gh audit rc=0 且「自洽」）",
        r_av.returncode == 0 and "自洽" in (r_av.stdout or ""),
        "rc=%s" % r_av.returncode,
    )
    try:
        import agentos as _ag

        w_src = os.path.join(ROOT, "work", "gh_audit.jsonl")
        if os.path.exists(w_src):
            with open(w_src, encoding="utf-8") as f:
                w_body = f.read()
            w_tam = (
                w_body.replace("DENY", "EXEC", 1)
                if "DENY" in w_body
                else w_body[:-2] + "x\n"
            )
            w_tf = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
            w_tf.write(w_tam)
            w_tf.close()
            w_ok, _, _, w_msg = _ag._gh_audit_verify(w_tf.name)
            os.unlink(w_tf.name)
            gate("W8 审计防篡改：副本改一字即断链", not w_ok, w_msg)
        else:
            gate("W8 审计防篡改", False, "审计文件不存在（W3 应已落 DENY）")
    except Exception as e:  # noqa: BLE001
        gate("W8 审计防篡改可执行", False, repr(e))

    # W9-W11（R2 能力矩阵揭出缺口的负向门）：config 字面量校验 + 写面 TTY
    # 确认拒绝。全部用 temp HOME 隔离（_agentos_config_path 走 expanduser，
    # HOME 可注入），绝不读写真实 ~/.agentos/config.toml；W9 用真 PTY——
    # 管道里 --yes 会先被 non-tty 拒（W4），只有真 TTY 才走得到确认问句。
    try:
        import fcntl as _fcntl
        import pty as _pty
        import struct as _struct
        import termios as _termios

        w9_home = tempfile.mkdtemp(prefix="ghgate-w9-")
        os.makedirs(os.path.join(w9_home, ".agentos"), exist_ok=True)
        with open(
            os.path.join(w9_home, ".agentos", "config.toml"), "w", encoding="utf-8"
        ) as f:
            f.write('[gh]\nwrite_enabled = "true"\n')
        env9 = dict(os.environ)
        env9["HOME"] = w9_home
        r_y = run(
            [sys.executable, ENTRY, "config", "set", "gh.write_enabled", "yes"],
            env=env9,
        )
        r_t = run(
            [sys.executable, ENTRY, "config", "set", "gh.write_enabled", "true"],
            env=env9,
        )
        gate(
            "W10 config set gh.write_enabled：模糊值 yes 拒 rc≠0、"
            "字面 true 过 rc=0（SEC E1）",
            r_y.returncode != 0 and r_t.returncode == 0,
            "yes_rc=%s true_rc=%s" % (r_y.returncode, r_t.returncode),
        )
        r_b = run([sys.executable, ENTRY, "config", "set", "bogus.key", "v"], env=env9)
        gate(
            "W11 config set 任意表拒绝（只放行 llm.* / gh.*）",
            r_b.returncode != 0,
            "rc=%s" % r_b.returncode,
        )
        m9, s9 = _pty.openpty()
        _fcntl.ioctl(s9, _termios.TIOCSWINSZ, _struct.pack("HHHH", 24, 100, 0, 0))
        p9 = subprocess.Popen(
            [
                sys.executable,
                ENTRY,
                "gh",
                "issue-comment",
                "octo/demo",
                "1",
                "--body",
                "probe",
                "--yes",
            ],
            stdin=s9,
            stdout=s9,
            stderr=s9,
            env=env9,
            cwd=ROOT,
            start_new_session=True,
        )
        os.close(s9)
        time.sleep(1.5)  # 等确认问句落到 pty
        os.write(m9, b"n\n")  # 人答 n —— 必须 DENY 且不写远端
        t9 = time.time()
        while p9.poll() is None and time.time() - t9 < 20:
            time.sleep(0.2)
        rc9 = p9.poll()
        if rc9 is None:
            p9.terminate()
            rc9 = p9.wait(timeout=5)
        os.close(m9)
        den9 = w_audit_tail(("DENY",))
        gate(
            "W9 写面 TTY 确认拒绝：--yes+配置开+答 n → rc≠0 且审计 "
            "DENY(confirm-no)（SEC S1 末段，真 PTY）",
            rc9 not in (0, None) and den9 and den9[-1].get("note") == "confirm-no",
            "rc=%s tail=%s" % (rc9, den9[-1].get("note") if den9 else None),
        )
        shutil.rmtree(w9_home, ignore_errors=True)
    except Exception as e:  # noqa: BLE001
        gate("W9-W11 可执行", False, repr(e))

    # W12-W14（R2 自主部署）：agent 路径 policy 矩阵 / 密钥扫描 / 建库预演
    try:
        import gh_client as _gc2

        w12 = [
            (
                dict(
                    has_yes=False,
                    tty=False,
                    write_enabled="true",
                    repo="o/r",
                    exec_rows=[],
                    auto=True,
                ),
                True,
                "ok",
            ),
            (
                dict(
                    has_yes=False,
                    tty=False,
                    write_enabled="yes",
                    repo="o/r",
                    exec_rows=[],
                    auto=True,
                ),
                False,
                "config-off",
            ),
            (
                dict(
                    has_yes=False,
                    tty=False,
                    write_enabled="true",
                    repo="o/r",
                    exec_rows=[(time.time(), "o/r")] * 5,
                    auto=True,
                ),
                False,
                "rate-limit",
            ),
            (
                dict(
                    has_yes=False,
                    tty=False,
                    write_enabled="true",
                    repo="o/r",
                    exec_rows=[],
                ),
                False,
                "no-yes",
            ),
        ]
        w12_bad = []
        for kw, want, why in w12:
            got, rsn = _gc2.write_policy(**kw)
            if got != want or rsn != why:
                w12_bad.append(
                    "%s→(%s,%s) 期望(%s,%s)" % (kw.get("auto"), got, rsn, want, why)
                )
        gate(
            "W12 policy v2：agent 自主路径（配置开+审计限速即放行、"
            "配置模糊拒、CLI 无 --yes 仍拒）",
            not w12_bad,
            "; ".join(w12_bad) or "4 例全对",
        )
        w13_dir = tempfile.mkdtemp(prefix="ghgate-w13-")
        with open(os.path.join(w13_dir, ".env"), "w") as f:
            f.write("X=1\n")
        with open(os.path.join(w13_dir, "ok.py"), "w") as f:
            f.write("x=1\n")
        h13 = _gc2.secret_scan(w13_dir)
        c13 = _gc2.secret_scan(tempfile.mkdtemp(prefix="ghgate-w13b-"))
        gate(
            "W13 deploy 密钥扫描：.env 命中拒发、干净目录零误伤",
            any(".env" in h for h in h13) and not c13,
            "hits=%r clean=%r" % (h13, c13),
        )
        # W13b（W18 部署纠错）：venv 误报 + .env.example 模板豁免。
        # 为什么钉：项目根自带 .venv-pw（playwright/pip 的 vendored 文件，
        # 名含 credential/.pem）和带默认值的 .env.example，旧规则下根目录
        # 永远发不出去 —— 扫描器把「文件名像密钥」和「模板里有默认端口」
        # 都当成了「真有密钥」。豁免只认「值不像凭证」，不认「值非空」。
        w13b_dir = tempfile.mkdtemp(prefix="ghgate-w13b-")
        os.makedirs(os.path.join(w13b_dir, ".venv-pw", "lib"))
        with open(os.path.join(w13b_dir, ".venv-pw", "lib", "cacert.pem"), "w") as f:
            f.write("fake-pem\n")
        with open(os.path.join(w13b_dir, ".env.example"), "w") as f:
            f.write("AGENTOS_PORT=8790\nAGENTOS_ROOT=/app\n"
                    "AGENTOS_LLM_API_KEY=\nGH_TOKEN=\n")
        h13b = _gc2.secret_scan(w13_dir)  # 复用 W13 目录：.env 仍须命中
        c13b = _gc2.secret_scan(w13b_dir)
        w13c_dir = tempfile.mkdtemp(prefix="ghgate-w13c-")
        with open(os.path.join(w13c_dir, ".env.example"), "w") as f:
            f.write('GH_TOKEN="ghp_realkeyfilled"\n')
        h13c = _gc2.secret_scan(w13c_dir)
        w13d_dir = tempfile.mkdtemp(prefix="ghgate-w13d-")
        with open(os.path.join(w13d_dir, ".env.example"), "w") as f:
            f.write("AGENTOS_LLM_API_KEY=sk-RealLookingKey0123456789\n")
        h13d = _gc2.secret_scan(w13d_dir)
        gate(
            "W13b 扫描器不误伤：venv 内文件 + 带默认值的 .env.example 放行，"
            "填了真 key 的 .env.example 照拒（ghp_ / sk- 各一），.env 仍命中",
            not c13b and bool(h13c) and bool(h13d)
            and any(".env" in h for h in h13b),
            "venv_tpl=%r filled=%r sk=%r env=%r"
            % (c13b, h13c, h13d, h13b),
        )
        r14 = run(w_cli + ["repo-create", "ghgate-demo-repo", "--dry-run"])
        d14 = w_audit_tail(("EXEC-DRY",))
        gate(
            "W14 repo-create dry-run rc=0 且审计 EXEC-DRY 留痕",
            r14.returncode == 0 and d14 and (d14[-1].get("sub") == "repo-create"),
            "rc=%s sub=%s" % (r14.returncode, d14[-1].get("sub") if d14 else None),
        )
    except Exception as e:  # noqa: BLE001
        gate("W12-W14 可执行", False, repr(e))

    # 判据自证：传输层重试谓词必须双向有牙 —— 认得出真事故字样（否则重试
    # 永远不触发，抖动照旧判红），也认不出「真失败」字样（否则真红会被重试
    # 洗成绿）。两个方向都断，才配当「只吃抖动」的证据。
    incident = (
        'Get "https://api.github.com/rate_limit": net/http: TLS handshake timeout'
    )
    real_fail = "HTTP 401: Bad credentials (https://api.github.com/rate_limit)"
    gate(
        "判据自证：重试谓词认传输层事故字样、不认真失败字样",
        bool(TRANSIENT_RE.search(incident)) and not TRANSIENT_RE.search(real_fail),
        "incident=%s real_fail=%s"
        % (bool(TRANSIENT_RE.search(incident)), bool(TRANSIENT_RE.search(real_fail))),
    )

    prod_before = None
    if os.path.exists(out_path):
        with open(out_path, "rb") as f:
            prod_before = f.read()

    if not is_child:
        td = tempfile.mkdtemp(prefix="ghgate-")
        try:
            muts = (
                ("M1", "PATH 无 gh（剥掉含 gh 的 PATH 目录）", env_without_gh()),
                (
                    "M2",
                    "gh 未认证（隔离 HOME/GH_CONFIG_DIR/GH_TOKEN）",
                    env_unauthenticated(td),
                ),
                (
                    "M3",
                    "假 gh（exit 1 且打印传输层 TLS 字样）顶 PATH 最前",
                    env_fake_gh(td),
                ),
            )
            for tag, why, env in muts:
                caught, detail, crc, jrc = run_mutant(tag, env, td)
                gate(
                    "%s %s 必须红（子进程活着判红：rc≠0 且 JSON rc≠0 且有 FAIL 门）"
                    % (tag, why),
                    caught,
                    detail,
                )
        finally:
            shutil.rmtree(td, ignore_errors=True)

    prod_untouched = (
        prod_before is None
        and not os.path.exists(out_path)
        or prod_before is not None
        and os.path.exists(out_path)
        and open(out_path, "rb").read() == prod_before
    )
    gate(
        "零污染：生产 JSON 在 3 个变异体跑动前后字节不变",
        prod_untouched,
        "before=%s after=%s"
        % (
            "None" if prod_before is None else "有",
            "None" if not os.path.exists(out_path) else "有",
        ),
    )

    leftover = [f for f in sorted(os.listdir(HERE)) if f.startswith(MUT_PREFIX)]
    gate(
        "零污染：tools/ 下无 %s* 残留（桩只落临时目录）" % MUT_PREFIX,
        not leftover,
        repr(leftover),
    )

    rc = 0 if all(r["ok"] for r in results) else 2
    if write_json(out_path, rc) == 3:
        return 3
    return finish()


if __name__ == "__main__":
    sys.exit(main())
