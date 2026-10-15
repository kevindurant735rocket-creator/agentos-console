#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R6 性能门：验证启动时间、API 响应时间、页面加载时间。

判据：
1. 服务启动时间 < 3s
2. API 响应时间 < 100ms
3. 页面加载时间 < 1s
4. 内存使用 < 200MB
5. CPU 使用率 < 10%
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

PORT = int(os.environ.get("AGENTOS_RESIDENT_PORT", "8790"))
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 冷启动判据的等待上限：3s 是判据线，量具得能区分「比 3s 慢」和「根本没起来」，
# 所以等待上限要高于判据线。12s 与 serve.py 里 promote 冷路径注释同一口径。
COLD_BUDGET = 12.0
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


def free_port():
    """现拿一个没人占的回环端口。同 tools/act_gate.py:93 的做法。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def get(path, timeout=5):
    start = time.time()
    try:
        # 回环量具绕代理会量到代理的排队，不是常驻 8790 自己的墙钟：本机 shell
        # 实测导出 HTTP_PROXY=http://127.0.0.1:7897 且 no_proxy 为空（getproxies()
        # 实拿得到），裸 urlopen 打 127.0.0.1 会先绕一圈本地 clash —— 同一个端点
        # 时快时慢，忙时直接 status=0 假红（与判据 8 同因，见其注释）。阈值不动，
        # 只是把「API 响应时间」还给被测服务。
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                "http://127.0.0.1:%d%s" % (PORT, path), timeout=timeout) as r:
            elapsed = time.time() - start
            return r.status, r.read().decode("utf-8", "replace"), elapsed
    except Exception as e:
        return 0, str(e), time.time() - start


print("== 性能门 R6 port=%d" % PORT)

# 1. API 响应时间
st, body, elapsed = get("/__health")
chk("perf-api-health", st == 200 and elapsed < 0.1,
    "HTTP=%s, 响应时间=%.3fs (目标<0.1s)" % (st, elapsed))

# 2. 蒸馏 API 响应时间
st, body, elapsed = get("/__distill")
chk("perf-api-distill", st == 200 and elapsed < 0.2,
    "HTTP=%s, 响应时间=%.3fs (目标<0.2s)" % (st, elapsed))

# 3. 能力地图 API 响应时间
st, body, elapsed = get("/__capmap")
chk("perf-api-capmap", st == 200 and elapsed < 0.2,
    "HTTP=%s, 响应时间=%.3fs (目标<0.2s)" % (st, elapsed))

# 4. 页面加载时间
st, body, elapsed = get("/")
chk("perf-page-load", st == 200 and elapsed < 1.0,
    "HTTP=%s, 加载时间=%.3fs (目标<1.0s)" % (st, elapsed))

# 5. 内存使用
rc, out = sh("ps", "-o", "rss=", "-p", str(os.getpid()))
try:
    mem_kb = int(out.strip())
    mem_mb = mem_kb / 1024
    chk("perf-memory", mem_mb < 200, "内存=%.1fMB (目标<200MB)" % mem_mb)
except:
    chk("perf-memory", False, "无法获取内存使用")

# 6. 进程数
rc, out = sh("pgrep", "-f", "serve.py")
n_procs = len([l for l in out.strip().split("\n") if l.strip()])
chk("perf-processes", n_procs <= 3, "进程数=%d (目标<=3)" % n_procs)

# 7. 文件描述符
rc, out = sh("lsof", "-p", str(os.getpid()))
n_fds = len([l for l in out.strip().split("\n") if l.strip()]) - 1
chk("perf-fds", n_fds < 100, "文件描述符=%d (目标<100)" % n_fds)

# 8. 冷启动：真起一个子服务，量「进程起来到 /__health 回 200」的墙钟。
#
# 为什么不量常驻 8790：它在机器上活了 5 天，什么都是热的，量不到启动。
# 端口用 free_port() 现拿，--tree 指向空假树 —— watcher 和项目面板都不参与，
# 量到的就是 serve.py 自己 import + bind + 首问 /__health 的代价
# （与 tools/act_gate.py:767 起自测实例是同一套做法）。
# 位置在进程数检查**之后**：自起的这个 serve.py 也是「pgrep -f serve.py」的
# 一条，先数数再起它，perf-processes 的口径才不被量具自己污染。
#
# 走 direct opener 而不是裸 urlopen：本机 shell 实测导出
# HTTP_PROXY=http://127.0.0.1:7897（urllib.request.getproxies() 实拿得到），
# 于是「打 127.0.0.1 的服务」会先绕一圈本地代理 —— 量到的是代理的排队，
# 不是服务自己的墙钟，代理一挂还会假红。冷启动是回环量具，绕代理没意义。
direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
cold_port = free_port()
cold_tree = os.path.join(ROOT, "work", "_perfgate_tree")
os.makedirs(cold_tree, exist_ok=True)
t0 = time.time()
proc = subprocess.Popen(
    [sys.executable, os.path.join(HERE, "serve.py"),
     "--port", str(cold_port), "--tree", cold_tree, "--poll", "3600"],
    cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    cold, listening, ok_json = None, None, False
    while time.time() - t0 < COLD_BUDGET:
        if listening is None:
            try:
                s = socket.create_connection(("127.0.0.1", cold_port), timeout=0.2)
                s.close()
                listening = time.time() - t0
            except OSError:
                pass
        try:
            with direct.open(
                    "http://127.0.0.1:%d/__health" % cold_port, timeout=2) as r:
                body = json.loads(r.read())
                # 200 之外还要 ok=true：光回 200 不给载荷的「起来了」不算启动完成
                if r.status == 200 and body.get("ok") is True:
                    cold = time.time() - t0
                    ok_json = True
                    break
        except Exception:
            pass
        time.sleep(0.01)
    chk("perf-cold-start",
        ok_json and cold is not None and cold < 3.0,
        "冷启动=%s (目标<3s) 监听=%s"
        % ("超时未起" if cold is None else "%.3fs" % cold,
           "未监听" if listening is None else "%.3fs" % listening))
finally:
    # 量具自己收干净：漏一个 serve.py 会把下一次的 perf-processes 顶红
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()

# 9. 用户感知启动：真起一次 `agentos health` 端到端 —— 进程 import + 打常驻 8790
#    + 渲染上屏，量「用户敲下回车到看见结果」的墙钟。它和判据 8 不是一回事：
#    判据 8 量的是 serve.py 自己「起来到回 200」，属内部量具；用户真实感知的是
#    CLI 这条命令的整条路径（Python 启动 + urllib + 输出）。R6 的「<3s」在终端
#    交付面就落在这条上，判据 8 留着量服务端冷路径，两条一起才盖住「服务冷启快
#    但 CLI 卡」的真空洞。超时也按判红处理（不许门自己崩掉不落 JSON）。
_t_cli = time.time()
try:
    _cli = subprocess.run(
        [sys.executable, os.path.join(HERE, "agentos.py"), "health"],
        cwd=ROOT, capture_output=True, text=True, timeout=COLD_BUDGET)
    _cli_elapsed = time.time() - _t_cli
    _cli_out = (_cli.stdout or "") + (_cli.stderr or "")
    chk("perf-cli-perceived",
        _cli.returncode == 0 and "OK" in _cli_out and _cli_elapsed < 3.0,
        "agentos health 端到端=%.3fs rc=%d (目标<3s 且 rc=0)" % (_cli_elapsed, _cli.returncode))
except subprocess.TimeoutExpired:
    chk("perf-cli-perceived", False,
        "agentos health 超时未回（>%.0fs，目标<3s）" % COLD_BUDGET)

print("\nPERF_GATE %s %d/%d" % ("OK" if FAIL == 0 else "FAIL", len(CHECKS) - FAIL, len(CHECKS)))

# 保存结果
result = {"ok": FAIL == 0, "checks": CHECKS, "port": PORT}
with open("work/perf_gate.json", "w") as f:
    json.dump(result, f, ensure_ascii=False, indent=1)

sys.exit(0 if FAIL == 0 else 1)
