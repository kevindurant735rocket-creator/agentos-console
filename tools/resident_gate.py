#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D30 常驻真值门：核 launchd 常驻的那个实例，不是门自己起的实例。

D29 实测真缺陷：serve.py 加了 /__promote 路由后全部门都绿，
但 launchd 常驻实例起于旧代码之前，真机打开 8790 时 /__act /__promote 全 404。
根因是门走自起实例（永远新代码），而 KeepAlive=SuccessfulExit:false
意味着进程健康就不重启 -> 代码更新在常驻面上永远不生效。
"""
import json
import hashlib
import os
import re
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVE = os.path.join(ROOT, "tools", "serve.py")
PORT = int(os.environ.get("AGENTOS_RESIDENT_PORT", "8790"))
PLIST = os.path.expanduser("~/Library/LaunchAgents/com.zhangfengrui.agentos-console.plist")
LABEL = "com.zhangfengrui.agentos-console"
KICK = "launchctl kickstart -k gui/%d/%s" % (os.getuid(), LABEL)

CHECKS = []
FAIL = 0


def chk(name, ok, detail):
    global FAIL
    CHECKS.append({"name": name, "ok": bool(ok), "detail": str(detail)[:200]})
    if not ok:
        FAIL += 1
    print(("  PASS " if ok else "  FAIL ") + name + " :: " + str(detail)[:150])


def sh(*cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def get(path, timeout=12):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (PORT, path), timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def pid_on(port):
    rc, out = sh("lsof", "-tiTCP:%d" % port, "-sTCP:LISTEN")
    ids = [int(x) for x in out.split() if x.isdigit()]
    return ids[0] if ids else 0


def proc_start_ts(pid):
    rc, out = sh("ps", "-p", str(pid), "-o", "lstart=")
    if rc != 0 or not out.strip():
        return 0
    s = out.strip()
    # 支持中文 locale（如 "二 10月/ 6 16:34:27 2026"）和英文 locale（如 "Mon Oct  6 16:34:27 2026"）
    # 提取：星期 月 日 时:分:秒 年
    parts = s.split()
    if len(parts) >= 5:
        # 中文格式：星期 月/ 日 时:分:秒 年
        # 英文格式：星期 月 日 时:分:秒 年
        try:
            # 尝试从 parts[1] 提取月份（中文 "10月/" -> "10"）
            month_str = parts[1].replace('月', '').replace('/', '').strip()
            month = int(month_str)
            day = int(parts[2])
            time_part = parts[-2]  # 时:分:秒
            year = int(parts[-1])  # 年
            # 构造标准格式：年-月-日 时:分:秒
            dt_str = f"{year}-{month:02d}-{day:02d} {time_part}"
            return int(time.mktime(time.strptime(dt_str, "%Y-%m-%d %H:%M:%S")))
        except Exception:
            pass
    # 回退：尝试原始英文格式
    try:
        return int(time.mktime(time.strptime(s, "%a %b %d %H:%M:%S %Y")))
    except Exception:
        return 0


def hhmm(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "?"


print("== 常驻真值门 D30 port=%d" % PORT)
src = open(SERVE, encoding="utf-8").read()

rc, out = sh("launchctl", "list")
chk("launchd-job-loaded", LABEL in out, LABEL if LABEL in out else "job 未加载=开机自启失效")
# v118 踩坑留证：上面这条 + plist-runatload + plist-keepalive + resident-listening
# **四条同时绿**，而开机自启其实从来没生效过 —— plist 的 ProgramArguments 写成
# `bash -lc -lc "<命令>"`，双 `-lc` 让真正的命令串被 bash 当成 $0 丢掉，
# launchd 起的是一个空转 bash；端口上那个服务是**手动**起的孤儿进程。
# 也就是说：那四条验的全是「结构在不在」，没有一个验「跑起来的那个进程是谁」。
# 下面补的就是这个洞：命令串不许被当 $0 吃掉；监听者必须**就是** job 自己的 pid。
try:
    import plistlib
    _pd = plistlib.load(open(PLIST, "rb")) if os.path.exists(PLIST) else {}
    _pa = _pd.get("ProgramArguments") or []
    _cmd = _pa[-1] if _pa else ""
    # 正确形式 `[bash, -c, "<命令>"]`：命令标志恰一个，且紧邻命令串。
    # 坏形式 `[bash, -lc, -lc, "<命令>"]` → 多出的标志让命令串被当 $0 丢掉（v118 真实故障）。
    _fi = [i for i, a in enumerate(_pa[:-1]) if a.strip() in ("-c", "-lc")]
    _bad = (len(_fi) != 1) or (_fi[0] != len(_pa) - 2)
    chk("plist-cmd-not-eaten", (not _bad) and ("serve.py" in _cmd),
        "argv=%s · 命令标志 %d 次(应1) · 紧邻=%s"
        % (" ".join(_pa)[:52], len(_fi), bool(_fi) and _fi[0] == len(_pa) - 2))
except Exception as e:
    chk("plist-cmd-not-eaten", False, str(e)[:100])

_jpid = 0
for _line in out.splitlines():
    _p = _line.split("\t")
    if len(_p) >= 3 and _p[2].strip() == LABEL:
        _jpid = int(_p[0]) if _p[0].strip().isdigit() else 0
plist = open(PLIST, encoding="utf-8").read() if os.path.exists(PLIST) else ""
chk("plist-runatload", "<key>RunAtLoad</key>" in plist, "RunAtLoad=True=开机自启")
chk("plist-keepalive", "<key>KeepAlive</key>" in plist, "KeepAlive=常驻")

pid = pid_on(PORT)
chk("resident-listening", pid > 0, "pid=%s" % (pid or "无监听"))
chk("resident-pid-is-launchd", pid > 0 and pid == _jpid,
    "监听 pid=%s vs launchd job pid=%s（不等=手动孤儿进程，自启没生效）"
    % (pid or "无", _jpid or "无"))
if pid:
    started = proc_start_ts(pid)
    mtime = int(os.path.getmtime(SERVE))
    chk("resident-code-fresh", started >= mtime,
        "进程起 %s vs serve.py 改 %s" % (hhmm(started), hhmm(mtime)))

    # 内容级对码：mtime 只是启发式（touch 一下就骗过它，换台机器时钟也不对齐）。
    # 真正的判据是**常驻进程加载的那份源码的 sha** == 磁盘上这份的 sha。
    # serve.py 在导入时把 CODE_SHA 算进 /__health，所以这条能精确回答
    # 「我现在在浏览器里点的，到底是不是我刚改的那份代码」。
    try:
        _h = json.loads(get("/__health")[1])
        _live = _h.get("code_sha")
        _disk = hashlib.sha256(open(SERVE, "rb").read()).hexdigest()[:16]
        chk("resident-code-sha", _live == _disk,
            "常驻 %s vs 磁盘 %s" % (_live, _disk))
    except Exception as e:
        chk("resident-code-sha", False, "取不到 code_sha: %s" % str(e)[:80])

# D95：能力地图端点进常驻实测名单。这份名单是硬编码的，而 code_sha 对码只能
# 证明「跑的是这份代码」，证不了「这份代码的每条路由都真能应答」—— 一条
# 写错前缀的 startswith 会同时骗过两者：code_sha 一致、路由 404。
# /__capmap 是 D95 新加的端点，不实测就等于没接。
# D96：/__projects 同样进名单。左栏「项目与任务空间」的整个数据面挂在它上面，
# 而这条名单的性质是「新增只读端点必须同时补一行实测」—— 只加代码不加实测，
# 就是把「接了」当成了「能用」。
for route in ["/__health", "/__recurrence", "/__promote", "/__capmap",
              "/__projects", "/__act/recommend", "/index.html"]:
    try:
        st, body = get(route)
    except Exception as e:
        st, body = 0, str(e)
    chk("resident-route" + route, st == 200, "HTTP=%s %s" % (st, body[:50].replace("\n", " ")))

try:
    h = json.loads(get("/__health")[1])
    acts = h.get("actions") or []
    declared = set(re.findall(r'startswith\("(/__[a-z_]+)"\)', src))
    chk("resident-health-ok", h.get("ok") is True, "actions=" + ",".join(acts))
    chk("resident-promote-in-allowlist",
        "promote" in acts and "/__promote" in declared,
        "ACTIONS 含 promote 且源码声明 /__promote")
except Exception as e:
    chk("resident-health-ok", False, str(e)[:120])

try:
    p = json.loads(get("/__promote")[1])
    chk("resident-promote-ok", p.get("ok") is True and p.get("total", 0) > 0,
        "total=%s ready=%s hold=%s" % (p.get("total"), p.get("n_ready"), p.get("n_hold")))
except Exception as e:
    chk("resident-promote-ok", False, str(e)[:120])

p8787 = pid_on(8787)
args = ""
if p8787:
    rc2, args = sh("ps", "-p", str(p8787), "-o", "args=")
chk("headroom-on-8787", bool(p8787) and "headroom" in args,
    (args or "8787 无监听").strip()[:110])
chk("serve-avoids-8787", "PORT = 8787" not in src and ", 8787)" not in src,
    "serve.py 默认 8790 且占用顺延，不与 headroom 抢 8787")

print("\nRESIDENT_GATE %s %d/%d" % ("OK" if FAIL == 0 else "FAIL", len(CHECKS) - FAIL, len(CHECKS)))
if FAIL:
    print("  修复: %s" % KICK)
with open(os.path.join(ROOT, "work", "resident_gate.json"), "w", encoding="utf-8") as f:
    json.dump({"ok": FAIL == 0, "checks": CHECKS, "port": PORT}, f, ensure_ascii=False, indent=1)
sys.exit(0 if FAIL == 0 else 1)
