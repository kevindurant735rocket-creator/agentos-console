#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AgentOS 驾驶舱本地服务：静态托管 + SSE 热重载 + 真树变更自动重抽重构建。

只用标准库（http.server），零外部依赖。
  GET /            静态页（no-store，禁缓存冒充旧构建）
  GET /__health    存活 + 指纹（供外部核验）
  GET /__watch     SSE 长连接，index.html/data.json 变更即推 reload
  POST /__act      执行面：跑引擎动作（严格 allowlist + 固定 argv + 单飞 + CSRF 头）
  POST /__act/abort 中止面：按 run id 真杀该 run 的整个进程组（rc 记 130，不记成功）
  GET /__act/recommend  决策面：把非绿项翻成可执行推荐（纯函数推导，动作仍走 allowlist）
  GET /__act/resolve    路由面：人话 -> allowlist 动作名（纯函数，只读，不执行不授权）
  GET /__act/stream     执行进度 SSE：动作跑的过程中逐行推，不是跑完才给
  GET /__act/status     执行面只读状态：谁在跑/已跑多久/预算多久/上次结果
  GET /__recurrence     学习面：跨 run 复发簇（引擎 evolution_intake 真数据，fail-closed）
  GET /__promote        晋升面：能力就绪度（引擎 capability.py 真判定，只读转发）
  GET /__distill        蒸馏面：单账本 star+license+commit（引擎 distill_ledger.py 真判定）
  --watch-tree     轮询真树 /Users/zhangfengrui/Desktop/项目/future agent，
                   有变动就跑 tools/extract.py + tools/build.py，再推 reload
"""
import hashlib
import importlib.util
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.parse
import signal
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TREE = "/Users/zhangfengrui/Desktop/项目/future agent"
COMPANY = os.path.expanduser("~/.codex/global-background/company-delivery")
PY = sys.executable
WATCH_FILES = [os.path.join(ROOT, "index.html"), os.path.join(ROOT, "data.json")]
# D77 目标台：编译请求的暂存文件。路径**固定**不是缺陷 —— 编译走 _act_lock 单飞，
# 同一时刻只可能有一个编译在跑，固定路径因此不会串味。
# 关于「需求文本进 argv」这件事，把真实约束写清楚，免得下一个人按错误的假设加固：
# 引擎 goal_loop.set-goal 的 argparse 只认 `--demand`（无 stdin、无 --demand-file），
# 所以 argv 就是它的**唯一**输入通道。这里能做的是把注入面关掉 —— Popen 传 list、
# 全程不过 shell，因此需求里的空格/引号/换行/`$(...)` 都不会被解释。
# 暂存 request.json 的作用是**审计与可复现**（盘上留一份「这份计划是按什么编出来的」），
# 不是为了替代 argv。任务目录名只带时间戳 + 需求 sha 前 6 位，需求正文不进路径。
GOAL_TASKS = os.path.join(ROOT, "work", "tasks")
GOAL_REQ = os.path.join(GOAL_TASKS, "_staging", "request.json")
_clients = []           # 活跃 SSE 队列
_lock = threading.Lock()
STATS = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "rebuilds": 0,
         "tree_rebuilds": 0, "last_build": None, "last_tree_sig": None}

def _livecheck_n():
    """上线自证的项数**从引擎源码推导**，不许在手写标签里写死。

    教训（v118 实测）：这个标签原本硬编码成「上线自证 N 项」（N 为手写数字），而引擎每加一项
    livecheck（本轮 20→21）门就红一次 —— 红的是项目自己的反漂移门，判得没错，
    但它挡的是「每次引擎升版都要手动改两个文件」。同一口径取自 goal_loop.cmd_livecheck
    区间里的 ck() 计数，与 doc_drift / act_gate 的 g_label_drift 完全一致（都减掉 def ck）。

    取不到就返回零：此时标签会变成「上线自证 N 项」且 N=0，反漂移门必然判红 ——
    这是我们要的 fail-closed（宁可门红，也不要一个看起来正常的假数字）。
    """
    try:
        g = open(os.path.join(COMPANY, "goal_loop.py"), encoding="utf-8").read()
        body = g[g.index("def cmd_livecheck"):g.index("def _render_table")]
        import re as _re
        return len(_re.findall(r"\bck\(", body)) - 1
    except Exception:
        return 0


LIVECHECK_N = _livecheck_n()
try:
    _code = open(os.path.abspath(__file__), "rb").read()
    CODE_SHA = hashlib.sha256(_code).hexdigest()[:16]
    CODE_MTIME = int(os.path.getmtime(os.path.abspath(__file__)))
except Exception:
    CODE_SHA, CODE_MTIME = "(不可读)", 0
COMPANY_SH = os.path.join(COMPANY, "company.sh")
ACTIONS = {
    "rebuild": ("重抽真树 + 重建驾驶舱", [PY, os.path.join("tools", "extract.py")], 300),
    "snapshot": ("刷闭环三面快照（直开兜底）",
                 [PY, os.path.join("tools", "snap_live.py")], 300),
    "verify": ("跑视觉/无障碍/UX 三门", ["bash", os.path.join(ROOT, "tools", "gates.sh"),
               "view_dod", "a11y_walk", "ux_audit"], 900),
    "egate": ("企业门（真树口径）", ["bash", COMPANY_SH, "egate", "--dir", TREE], 1200),
    # 超时值不是拍的，是**实测时长 + 余量**：2026-10-10 实测一次
    # `company.sh goal livecheck` 跑了 11m28s 仍未结束，而这里写的是 180s ——
    # 于是 flagship 动作在 headless 模式（agentos -p "跑一下 livecheck"）下
    # **永远** rc=124 被杀，用户看到的是「FAIL: livecheck 没跑成」。这不是门变严，
    # 是把唯一的上线自证动作做成了不可能成功。引擎侧另有自己的超时，这里是本服务
    # 侧进程组的硬上限。
    "livecheck": ("上线自证 %d 项" % LIVECHECK_N, ["bash", COMPANY_SH, "goal", "livecheck"], 2400),
    "desktop": ("交付物三件套门", ["bash", COMPANY_SH, "desktop"], 900),
    "evolution": ("跨 run 复发晋升门", ["bash", COMPANY_SH, "evolution"], 900),
    "promote": ("可执行晋升门", ["bash", COMPANY_SH, "promote"], 600),
    "doc-drift": ("文档数字零漂移", ["bash", COMPANY_SH, "doc-drift"], 600),
}
_act_lock = threading.Lock()      # 单飞：同一时刻只允许一个动作在跑
_act_state = {"running": None, "started": None, "last": None}
_act_cur = {"name": None, "lines": [], "done": True, "rc": None,
            "run": None, "aborted": False,
            "lock": threading.Lock()}   # 当前动作的流式现场，供 SSE 订阅者读
_act_proc = {"p": None, "lock": threading.Lock()}   # 当前动作的活进程，供中止端点杀进程组


def _act_busy():
    """409 的统一形状：不只说「忙」，还把谁在忙、已跑多久、它的预算多久交出去。

    客户端要靠这三个字段决定「等还是撤」—— 只给 error:busy 的话，headless
    模式除了立刻失败没有第二个选择，而 `-p` 的一次性语义本来就应该能等。
    """
    name = _act_state.get("running")
    started = _act_state.get("started") or 0.0
    tmo = ACTIONS.get(name, (None, None, None))[2] if name else None
    return {"ok": False, "error": "busy", "running": name,
            "elapsed": round(time.time() - started, 1) if started else None,
            "tmo": tmo}


def new_run_id():
    """每次执行一个不可猜的 run id —— 中止必须指名道姓，不能靠「当前正在跑的那个」。
    没有 run id（或 id 不对）就不许中止，见 abort_act。"""
    return "r" + uuid.uuid4().hex[:10]


def feed(line):
    """把一行输出追加进当前动作现场（run_act 的唯一写入口）。"""
    with _act_cur["lock"]:
        _act_cur["lines"].append(line)


_chain_buf = []      # 轮次链自己的行缓冲（不混进编译 transcript）


def chain_say(line):
    _chain_buf.append(line)


def stream_start(name):
    run_id = new_run_id()
    with _act_cur["lock"]:
        _act_cur["name"] = name
        _act_cur["lines"] = []
        _act_cur["done"] = False
        _act_cur["rc"] = None
        _act_cur["run"] = run_id
        _act_cur["aborted"] = False
    return run_id


def stream_end(rc):
    with _act_cur["lock"]:
        _act_cur["done"] = True
        _act_cur["rc"] = rc
    with _act_proc["lock"]:
        _act_proc["p"] = None


def abort_act(run_id):
    """中止**指名道姓**的那个 run。返回 (code, payload)。

    三条硬语义，abort_gate 逐条钉：
    - 没有 run id / id 不匹配 / 那个 run 已不在跑 -> 一律不杀任何进程（400/409）。
      「中止」不能退化成「把当前随便哪个进程杀掉」。
    - 真杀**整个进程组**（Popen 用 start_new_session=True 起的；只杀父进程不够，
      bash 门会把 python / 浏览器孙子留在后台继续跑）。
    - 中止后 rc 语义必须诚实：记 130（SIGINT 惯例），绝不能记成 rc=0 成功。
    """
    if not isinstance(run_id, str) or not run_id:
        return 400, {"ok": False, "error": "missing run"}
    with _act_cur["lock"]:
        cur = _act_cur["run"]
        done = _act_cur["done"]
        aborted = _act_cur["aborted"]
    running = _act_state["running"]
    if running is None or done:
        return 409, {"ok": False, "error": "no action running"}
    if cur != run_id:
        return 409, {"ok": False, "error": "run id mismatch",
                     "running": running, "run": cur}
    if aborted:
        return 409, {"ok": False, "error": "already aborted"}
    with _act_proc["lock"]:
        p = _act_proc["p"]
    if p is None or p.poll() is not None:
        return 409, {"ok": False, "error": "process already gone"}
    with _act_cur["lock"]:
        _act_cur["aborted"] = True
    feed("⏹ 用户中止 run=%s（杀进程组）" % run_id)
    try:
        pgid = os.getpgid(p.pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError) as e:
        return 500, {"ok": False, "error": "killpg failed: %s" % e}
    # 给 3s 优雅退出；不退就升 SIGKILL（bash 门常忽略 SIGTERM）。
    t0 = time.time()
    while time.time() - t0 < 3.0 and p.poll() is None:
        time.sleep(0.05)
    hard = False
    if p.poll() is None:
        hard = True
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        feed("⏹ run=%s 3s 未退 -> SIGKILL 进程组" % run_id)
    return 200, {"ok": True, "run": run_id, "action": running,
                 "sigkill": hard, "pgid": p.pid}


def recommend(data):
    """自我进化的「决策」一环：把数据里的非绿项翻成**可执行**的推荐动作。

    纯函数（不碰网络、不跑命令），所以门能拿构造数据做反证。
    每条都带真实证据串，且 action 必须是 ACTIONS 里的名字——
    推荐不许凭空编一个执行面不存在的动作。
    一切皆绿时返回空列表：会喊「没事」的告警等于没有告警。
    """
    out = []

    def add(act, sev, why, ev):
        if act in ACTIONS:
            out.append({"action": act, "severity": sev, "why": why,
                        "evidence": json.dumps(ev, ensure_ascii=False)[:300]})

    agg = data.get("aggregate") or {}
    hard = {k: agg.get(k) for k in ("FAIL", "HONESTY_VIOLATION", "TIMEOUT",
                                    "ORPHAN_SHELL", "MANIFEST_INVALID",
                                    "SELF_REPORT_PASS_BUT_RUN_FAILED")
            if agg.get(k)}
    if hard:
        add("egate", "P0", "真树口径存在硬红分类，先跑企业门定位", hard)

    dv = data.get("divergence") or []
    if dv:
        add("egate", "P1", "%d 处自报与实测不一致，需人工裁决" % len(dv),
            [{"dir": x.get("dir"), "declared": x.get("declared"),
              "gate": x.get("gate"), "adjudication": x.get("adjudication")}
             for x in dv])

    eg = data.get("env_gated") or []
    if eg:
        add("livecheck", "P2", "%d 处被环境闸挡住，非绿但不算错" % len(eg),
            [{"dir": x.get("dir"), "declared_verdict": x.get("declared_verdict")}
             for x in eg])

    ind = data.get("independent") or {}
    if (ind.get("n_mismatches") or 0) > 0:
        add("desktop", "P1", "独立复核有 %d 处不一致" % ind.get("n_mismatches"), ind)

    mf = data.get("manifest") or {}
    if mf.get("missing"):
        add("rebuild", "P1", "证据清单缺 %d 项，重抽真树" % len(mf["missing"]),
            mf.get("missing"))
    return out


# ---------------------------------------------------------------- D75 意图路由
# composer 的 label 与 placeholder 都写着「或自然语言，Enter 提交」，
# 但 2026-10-04 真浏览器实测：「帮我重建一下」「请跑一次视觉门」
# 「验证一下无障碍」三句全被「不在服务端 allowlist 里」拒掉 ——
# 因为前端 wkParse 只拿首 token 精确匹配。**UI 承诺了它没有的能力**，
# 这正是「操作反馈要好」的反面。
#
# 唯一不变量（intent_gate 逐条证伪，破了就得改判据而不是改门）：
#   本函数**只会在 ACTIONS 里挑名字，永远造不出 allowlist 之外的动作**。
#   自然语言只负责「路由」，不负责「授权」；授权仍由审批卡单独管，
#   所以加意图路由不会削弱执行面安全边界。
# 听不懂就诚实说听不懂，并把最像的几个候选摆出来；**绝不猜**。
#
# 权重只表达「多像」，不表达「该多信」：命中短语越多分越高，
# 同分就是真的分不出来 —— 那就如实报歧义，让用户自己选。
INTENT_LEXICON = {
    "rebuild":   (("重建", 3), ("重抽", 3), ("重新构建", 3), ("抽真树", 3),
                 ("rebuild", 3), ("build", 2), ("刷新页面", 2)),
    "snapshot":  (("快照", 3), ("snapshot", 3), ("刷快照", 3)),
    "verify":    (("视觉门", 3), ("无障碍", 3), ("可用性", 3), ("跑门", 3),
                 ("三门", 3), ("校验", 2), ("验证", 2), ("检查界面", 2),
                 ("verify", 3), ("ux", 2), ("a11y", 2)),
    "egate":     (("企业门", 3), ("企业级", 3), ("egate", 3)),
    "livecheck": (("上线自证", 3), ("自证", 3), ("上线检查", 3), ("上线", 2),
                 ("livecheck", 3)),
    "desktop":   (("交付物", 3), ("三件套", 3), ("桌面化", 3), ("desktop", 3)),
    "evolution": (("复发", 3), ("跨run", 3), ("跨 run", 3), ("evolution", 3)),
    "promote":   (("晋升", 3), ("晋级", 3), ("promote", 3)),
    "doc-drift": (("文档", 3), ("漂移", 3), ("doc-drift", 3), ("docdrift", 3)),
}


def resolve_intent(q):
    """把一句人话解析成 allowlist 内的动作名。纯函数：好让门拿构造输入证伪。

    返回 {"ok": True, "action": 名字, "how": "exact"/"intent",
          "matched": [命中的短语], "score": 权重和}
    或    {"ok": False, "reason": "empty"/"no-match"/"ambiguous",
           "candidates": [(动作, 分数), ...]}   —— 候选按分从高到低，供 UI 提示。

    纯函数（不碰网络、不跑命令、不读盘），所以 tools/intent_gate.py 能秒级证伪它。
    """
    raw = q if isinstance(q, str) else ""
    s = raw.strip()
    if not s:
        return {"ok": False, "reason": "empty", "candidates": []}
    low = s.lower().lstrip("/")

    # 1) 精确：动作名本身（或斜杠命令）。这是最没有歧义的一路。
    parts = low.split()
    head = parts[0] if parts else ""
    if head in ACTIONS:
        return {"ok": True, "action": head, "how": "exact",
                "matched": [head], "score": 0}

    # 2) 意图：词典打分。只认子串命中，不猜语义。
    scores = []
    for act in ACTIONS:                      # 遍历顺序 = ACTIONS 声明顺序，结果稳定
        ph = INTENT_LEXICON.get(act)
        if not ph:
            continue
        hit = [(w, wt) for w, wt in ph if w.lower() in low]
        if hit:
            scores.append((act, sum(wt for _, wt in hit), hit))
    scores.sort(key=lambda t: (-t[1], t[0]))
    if not scores:
        return {"ok": False, "reason": "no-match", "candidates": []}
    top = scores[0]
    tied = [a for a, sc, _ in scores if sc == top[1]]
    if len(tied) > 1:
        # 同分就是分不出来。如实报歧义并把候选摆出来，不替用户拍板。
        return {"ok": False, "reason": "ambiguous",
                "candidates": [(a, sc) for a, sc, _ in scores if sc == top[1]]}
    return {"ok": True, "action": top[0], "how": "intent",
            "matched": [w for w, _ in top[2]], "score": top[1]}


_intake = None


def _load_intake():
    """按路径加载引擎 evolution_intake（不靠 sys.path 猴补丁）。失败返回 None。"""
    global _intake
    if _intake is not None:
        return _intake
    path = os.path.join(COMPANY, "evolution_intake.py")
    spec = importlib.util.spec_from_file_location("_cd_intake", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _intake = mod
    return _intake


def recurrence_payload(limit=8):
    """自我进化的「学习」一环：跨 run 复发簇 = 反复犯的同一类错。

    不自实现聚类逻辑——直接复用引擎 evolution_intake.build_clusters，
    免得驾驶舱和引擎两套真相。晋升阈值也取引擎的 RECURRENCE_MIN，
    驾驶舱不许自己定一个更松的数。
    任何异常 -> ok=False + 空簇（fail-closed：读不到证据就不谎称有复发）。
    """
    try:
        m = _load_intake()
        if m is None:
            return {"ok": False, "error": "evolution_intake.py 不可加载",
                    "clusters": [], "threshold": None, "n_events": 0}
        ev = m.load_events()
        cl = m.build_clusters(ev)
        thr = int(m.RECURRENCE_MIN)
    except Exception as e:  # fail-closed：不许拿空数据冒充「一切正常」
        return {"ok": False, "error": ("intake error: %s" % e)[:120],
                "clusters": [], "threshold": None, "n_events": 0}
    rows = []
    for key, c in cl.items():
        n = int(c.get("run_count", 0))
        rows.append({
            "cluster": key,
            "family": c.get("family", ""),
            "runs": n,
            "events": int(c.get("events", 0)),
            "last_ts": c.get("last_ts", ""),
            "plan": c.get("plan", ""),
            "recurring": n >= thr,
            "ratio": round(n / float(thr), 2),
        })
    rows.sort(key=lambda r: (-r["runs"], r["cluster"]))
    led = _retro_ledger()
    # fail-closed：账本读不到就不能给「没有复发 / 已经变强」这种结论 ——
    # 复发簇来自 improvement_queue/postmortem，账本来自 postmortem，同源同命。
    return {"ok": bool(led["ok"]), "error": led["error"], "threshold": thr,
            "n_events": len(ev), "n_clusters": len(rows),
            "n_recurring": sum(1 for r in rows if r["recurring"]),
            "clusters": rows[:limit],
            "self": led["self"], "ledger": led["ledger"],
            "trend": led["trend"], "memory": led["memory"]}


def _retro_ledger(limit=14, buckets=None):
    """复盘账本 + 自我能力：闭环最后一环「哪里做错了/为什么错/下次改什么」。

    数据真源是引擎 COMPANY_MEMORY/postmortem.jsonl（postmortem.py 的产出），
    驾驶舱只读转发：verdict、根因、建议能力都逐字取引擎写下的值，
    台面不自己判定失败、不自己编根因、不自己算「变强了多少」。

    关于趋势轴的一条诚实纪律：human_interventions 在真实数据里长期为 0，
    所以**不画「人类干预在下降」这种趋势** —— 常量 0 画成下降线就是造假。
    趋势只取真有信号的 agent 侧计数（自验证/自修复/自恢复），
    人类侧计数照实给出原值，让屏上标「全程 0」而不是伪造一条斜线。
    """
    bad = {"ok": False, "error": "postmortem.jsonl 读不到",
           "self": {}, "ledger": [], "trend": [], "memory": None}
    m = _load_intake()
    if m is None:
        return dict(bad, error="evolution_intake.py 不可加载")
    path = os.path.join(getattr(m, "MEM", ""), "postmortem.jsonl")
    try:
        with open(path, encoding="utf-8") as f:
            raw = [json.loads(x) for x in f if x.strip()]
    except Exception as e:
        return dict(bad, error=("postmortem read error: %s" % e)[:120])
    if not raw:
        # 空账本：不是「零失败」，是「没数据」。fail-closed 不给结论。
        return dict(bad, error="postmortem.jsonl 为空（无数据 ≠ 无失败）")

    def num(r, k):
        v = r.get(k)
        return int(v) if isinstance(v, (int, float)) else 0

    cnt = {k: sum(num(r, k) for r in raw) for k in
           ("human_interventions", "human_takeovers", "agent_self_verifications",
            "agent_self_repairs", "agent_self_recoveries")}
    verdicts = {}
    for r in raw:
        v = r.get("verdict") or "UNKNOWN"
        verdicts[v] = verdicts.get(v, 0) + 1
    # 趋势按天分桶：只画真有分化的轴，且把每桶的 run 数一起给（防止
    # 「桶里只有 1 个 run」被读成趋势）。
    bk = {}
    for r in raw:
        d = (r.get("ts") or "")[:10]
        if len(d) != 10:
            continue
        b = bk.setdefault(d, {"day": d, "n": 0, "ver": 0, "rep": 0, "rec": 0})
        b["n"] += 1
        b["ver"] += num(r, "agent_self_verifications")
        b["rep"] += num(r, "agent_self_repairs")
        b["rec"] += num(r, "agent_self_recoveries")
    trend = sorted(bk.values(), key=lambda x: x["day"])
    if buckets:
        trend = trend[-buckets:]
    ledger = []
    for r in reversed(raw):          # 新的在前
        ledger.append({
            "ts": r.get("ts") or "",
            "dir": r.get("dir") or "",
            "verdict": r.get("verdict") or "UNKNOWN",
            "gap": r.get("capability_gap") or r.get("missing_capability") or "",
            "root_cause": r.get("root_cause") or "",
            "family": r.get("root_cause_family") or "",
            "plan": r.get("suggested_capability") or "",
            "generalizable": bool(r.get("generalizable")),
            "recoverable": r.get("recoverable"),
            "preventable": r.get("preventable"),
        })
    return {"ok": True, "error": None,
            "self": dict(cnt, runs=len(raw),
                         verdicts=verdicts,
                         n_generalizable=sum(1 for r in raw if r.get("generalizable")),
                         n_with_plan=sum(1 for r in raw if r.get("suggested_capability")),
                         n_family_known=sum(
                             1 for r in raw
                             if (r.get("root_cause_family") or "UNKNOWN_SEMANTIC")
                             != "UNKNOWN_SEMANTIC")),
            "ledger": ledger[:limit], "trend": trend, "memory": path}


# ---- D95 能力地图（目标里点名的「能力地图」，此前屏上一处未接）----
# 为什么单开一个面而不并进晋升面：晋升面回答「谁现在能晋升」，能力地图回答
# 「Agent 到底攒下了什么、其中有多少是**被独立证据验过**的」。后者是
# 「越用越强」这个断言唯一能站得住的地方，也是最容易被自吹自擂污染的地方。
#
# 三条诚实性纪律，全部来自 2026-10-05 对盘上真实数据的实测，不是假想：
#   1. `ledger.status == "PROMOTED"` **不等于**能力有效。实测 36 条账里 26 条
#      PROMOTED，但只有 5 条填了 current_status。所以自报状态与独立验证必须
#      分两列，不能合成一个「已晋升 N」的大数字。
#   2. 实测 73 条 A/B 回归裁决里没有一条 PASS（UNVERIFIABLE 16 / HOLD-AB 26 /
#      HOLD-EXIST 31）。所以「独立验证过」只能定义成「拿得出 A/B 裁决且该裁决
#      不是 UNVERIFIABLE」，屏上如实显示这个数，不把 HOLD 说成「跑过了实验」。
#   3. 实测 36 条账里 20 条有 A/B 裁决、10 条一条都没有、还有 6 条**连
#      capability_id 都没有**。后两者都不并进「已验证」，且彼此也分开：
#      「没跑过 A/B」和「连身份都无法追溯」是两种不同的账，混桶就看不出后者。
def _cap_dir():
    """能力账目录：优先走引擎自己的 MEM 路径，取不到才回退到约定布局。
    回退不是「猜个路径碰运气」—— 两条路径都实测存在，MEM 优先是因为那是
    引擎自己认的账本位置，将来引擎搬目录时驾驶舱跟着走。"""
    m = _load_intake()
    mem = getattr(m, "MEM", "") if m else ""
    if mem:
        d = os.path.join(mem, "capabilities")
        if os.path.isdir(d):
            return d
    return os.path.join(COMPANY, "COMPANY_MEMORY", "capabilities")


def _read_jsonl(path, limit=None):
    """读 jsonl。坏行跳过但**计数并如实报出** —— 静默吞掉坏行会让账本看起来
    比实际干净，正是「证据优先」要防的那种失真。"""
    rows, bad = [], 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    bad += 1
    except OSError:
        return None, bad
    if limit:
        rows = rows[-limit:]
    return rows, bad


# 「独立验证过」的唯一定义：拿得出 A/B 裁决，且裁决不是 UNVERIFIABLE。
# 写成常量而不是散在代码里的字面量，是为了门能直接断言这个定义不被悄悄改松。
CAP_VERIFIED_VERDICTS = ("HOLD-AB", "HOLD-EXIST")


def capmap_payload(limit=60):
    """能力地图：Agent 攒下了什么、其中多少被独立证据验过、哪些卡住、为什么。

    全部**只读**：不 fork 引擎、不写盘、不重算任何阈值。判定权在引擎
    capability.py 与它的三个账本文件手里，本函数只做连接与如实转发。
    读不到任何一本账 → ok=False（fail-closed，与 /__recurrence /__promote 同口径）：
    「拿不到判据」绝不等于「能力地图是空的」，更不等于「一切正常」。
    """
    bad = {"ok": False, "error": "", "rows": [], "total": 0,
           "n_ledger": 0, "n_verified": 0, "n_unverifiable": 0, "n_no_verdict": 0,
           "n_anonymous": 0,
           "n_degraded": 0, "status_counts": {}, "verdict_counts": {},
           "verdict_record_counts": {}, "n_verdict_records": 0,
           "n_verdict_ids": 0, "n_verdict_dangling": 0, "dangling_ids": [],
           "autopilot": [], "ab_snapshot_capabilities": 0, "bad_lines": 0,
           "ledger_path": "", "regression_path": "", "autopilot_path": "",
           "events_path": ""}
    d = _cap_dir()
    if not d or not os.path.isdir(d):
        return dict(bad, error="能力账目录不存在：%s" % (d or "<解析不出>"))
    lp = os.path.join(d, "ledger.jsonl")
    rp = os.path.join(d, "regression_verdicts.jsonl")
    ap = os.path.join(d, "autopilot_verdicts.jsonl")
    ep = os.path.join(d, "events.jsonl")
    led, b1 = _read_jsonl(lp)
    reg, b2 = _read_jsonl(rp)
    if led is None:
        return dict(bad, error="ledger.jsonl 读不到：%s" % lp)
    if reg is None:
        return dict(bad, error="regression_verdicts.jsonl 读不到：%s" % rp)
    auto, b3 = _read_jsonl(ap)
    auto = auto or []
    ev, b4 = _read_jsonl(ep)
    ev = ev or []

    # 每条能力取**时间上最新**的一条 A/B 裁决。文件是追加写的，末条通常即最新，
    # 但仍按 ts 比较取值：ts 一旦不规整，「最后一行」就不再等于「最新」。
    latest = {}
    for r in reg:
        rid = r.get("id") or ""
        if not rid:
            continue
        ts = r.get("ts") or ""
        if rid not in latest or ts >= (latest[rid].get("ts") or ""):
            latest[rid] = r
    # **全量**裁决记录分布，与上面「每条能力取最新一条」的 vd_cnt 是两件不同的事。
    # 2026-10-05 生产实测：regression_verdicts.jsonl 有 73 条记录 / 26 个 distinct
    # id，其中 55 条挂在账上（UNVERIFIABLE 10 / HOLD-EXIST 25 / HOLD-AB 20）、还有
    # 6 个 id 在 ledger 里**根本不存在**（悬空，如 resume-fastforward）。而面板上
    # 只显示「每条能力最新一条」= 20 条全 HOLD-AB —— 单看这块会读成「总共只有
    # 20 条裁决，且 26 条里 6 条从没被判过」，这是**不诚实**：历史裁决与悬空 id
    # 同样是真实存在的事实，必须自己占位，不能靠标签措辞掩盖。
    vr_cnt = {}
    for r in reg:
        v = r.get("verdict") or ""
        if v:
            vr_cnt[v] = vr_cnt.get(v, 0) + 1
    led_ids = set()
    for c in led:
        cid = c.get("capability_id") or c.get("name") or ""
        if cid:
            led_ids.add(cid)
    rids = set(rid for rid in latest)
    dangling = sorted(rids - led_ids)
    auto_by_id = {r.get("id") or "": r for r in auto if r.get("id")}
    ev_by_id = {}
    for e in ev:
        eid = e.get("id") or ""
        if eid:
            ev_by_id.setdefault(eid, []).append(e)

    rows, st_cnt, vd_cnt = [], {}, {}
    n_verified = n_unverifiable = n_no_verdict = n_degraded = n_anon = 0
    for c in led:
        cid = c.get("capability_id") or c.get("name") or ""
        st = c.get("status") or "UNKNOWN"
        st_cnt[st] = st_cnt.get(st, 0) + 1
        v = latest.get(cid)
        verdict = (v or {}).get("verdict") or ""
        if verdict:
            vd_cnt[verdict] = vd_cnt.get(verdict, 0) + 1
        if not cid:
            # 实测 36 条账里有 6 条**连 capability_id 与 name 都没有**（status
            # 全是 PROMOTED、problem 全是 None）。这类行不能塞进 no-verdict ——
            # 「有名字但没跑 A/B」和「连名字都没有」是两件完全不同的事，混在
            # 一起会让屏上读成「20 验过 / 16 没验」，而实际是「20 验过 /
            # 10 没跑过 / 6 连身份都没有」。后者是无法追溯的账，必须自己一桶。
            proof = "anonymous"
            n_anon += 1
        elif not verdict:
            proof = "no-verdict"          # 一条 A/B 都没有
            n_no_verdict += 1
        elif verdict in CAP_VERIFIED_VERDICTS:
            proof = "ab-verified"         # 拿得出独立 A/B 裁决
            n_verified += 1
        else:
            proof = "ab-unverifiable"     # 引擎自己说这条 A/B 验不了
            n_unverifiable += 1
        m = (v or {}).get("metrics") or {}
        was, now = m.get("was_pass"), m.get("now_pass")
        degraded = (isinstance(was, int) and isinstance(now, int) and now < was)
        if degraded:
            n_degraded += 1
        evs = ev_by_id.get(cid) or []
        hist = c.get("history") or []
        rows.append({
            "id": cid,
            "status": st,
            "current_status": c.get("current_status") or "",
            "problem": c.get("problem") or "",
            "source": c.get("source") or "",
            "mechanism": c.get("mechanism") or "",
            "implementation": c.get("implementation") or "",
            "promotion_reason": c.get("promotion_reason") or c.get("reason") or "",
            "rejection_reason": c.get("rejection_reason") or "",
            "linked_failures": c.get("linked_failures") or [],
            "regression_tests": c.get("regression_tests") or [],
            "confidence": c.get("confidence") or "",
            "history_n": len(hist),
            "last_history": hist[-1] if hist else "",
            "verdict": verdict,
            "verdict_reason": (v or {}).get("reason") or "",
            "verdict_ts": (v or {}).get("ts") or "",
            "proof": proof,
            "ab_was": was, "ab_now": now, "degraded": degraded,
            "ab_n_tasks": m.get("n_tasks"),
            "autopilot": (auto_by_id.get(cid) or {}).get("verdict") or "",
            "autopilot_reason": (auto_by_id.get(cid) or {}).get("reason") or "",
            "n_events": len(evs),
            "last_event": ("%s %s" % (evs[-1].get("ts"), evs[-1].get("action"))
                           if evs else ""),
        })
    # 排序 = 「最该被看见的排最前」。anonymous（连身份都没有）排最后：它不是
    # 最重要的能力，是最该被清理的账。混在中间会被当成普通条目一眼扫过去。
    _PR_ORDER = {"ab-verified": 0, "ab-unverifiable": 1, "no-verdict": 2,
                 "anonymous": 3}
    rows.sort(key=lambda r: (_PR_ORDER.get(r["proof"], 9),
                              r["status"] != "PROMOTED", r["id"]))
    try:
        n_snap = len([x for x in os.listdir(os.path.join(d, "ab_snapshots"))
                      if not x.startswith(".")])
    except OSError:
        n_snap = 0
    return {"ok": True, "error": None,
            "total": len(rows), "rows": rows[:limit],
            "n_ledger": len(led), "n_verified": n_verified,
            "n_unverifiable": n_unverifiable, "n_no_verdict": n_no_verdict,
            "n_anonymous": n_anon,
            "n_degraded": n_degraded,
            "status_counts": st_cnt, "verdict_counts": vd_cnt,
            "verdict_record_counts": vr_cnt,
            "n_verdict_records": len(reg),
            "n_verdict_ids": len(rids),
            "n_verdict_dangling": len(dangling),
            "dangling_ids": dangling[:40],
            "autopilot": [{"id": r.get("id") or "", "verdict": r.get("verdict") or "",
                           "reason": r.get("reason") or "", "ts": r.get("ts") or ""}
                          for r in auto],
            "ab_snapshot_capabilities": n_snap,
            "bad_lines": b1 + b2 + b3 + b4,
            "verified_def": list(CAP_VERIFIED_VERDICTS),
            "ledger_path": lp, "regression_path": rp,
            "autopilot_path": ap, "events_path": ep}


def _promote_compute(limit=40):
    """晋升面：谁能真晋升、为什么被拦。判定权全在引擎 capability.py，
    驾驶舱只做**只读转发**——不重写阈值、不重算闸门、不碰账本。
    读不到就 ok=False，不谎称"一切正常"（与 /__recurrence 同一 fail-closed 口径）。
    """
    try:
        sh = os.path.join(COMPANY, "company.sh")   # 调用时解析：COMPANY 缝隙必须真的生效
        p = subprocess.run(["bash", sh, "promote", "--json"],
                           capture_output=True, text=True, timeout=180)
        d = json.loads(p.stdout)
    except Exception as e:
        return {"ok": False, "error": ("promote probe error: %s" % e)[:120],
                "rows": [], "total": 0, "n_ready": 0, "n_hold": 0, "recurrence": None}
    if p.returncode != 0 or not d.get("ok"):
        return {"ok": False, "error": d.get("error") or "promote rc=%d" % p.returncode,
                "rows": [], "total": 0, "n_ready": 0, "n_hold": 0, "recurrence": None}
    return {"ok": True, "error": None, "total": d["total"],
            "n_ready": d["n_ready"], "n_hold": d["n_hold"],
            "recurrence": d.get("recurrence"), "rows": d["rows"][:limit]}


# v118 实测：/__promote 每次都 shell 出去跑 `company.sh promote --json`，
# 实测 9.5s（冷）/ 2.2s（热）。而 act_gate 的真浏览器门在点完 tab 后只等 1.6s 就断言，
# 于是「面板还没渲染完」被判成「面板是空壳」—— 门红了，但红的是时序，不是缺陷。
# 这里加一层短 TTL 缓存：判定权仍在引擎 capability.py，驾驶舱仍然只读转发，
# 缓存只避免**同一份只读快照**被反复重算；面板上会显示快照年龄，不假装实时。
PROMO_TTL = 20.0
_PROMO = {"ts": 0.0, "val": None}


def _promote_warm(limit=40):
    """后台重算晋升快照。异常吞掉：预热/重算失败不该把正在服务的进程带崩。"""
    try:
        val = _promote_compute(limit)
    except Exception:  # noqa: BLE001
        return
    with _lock:
        _PROMO["ts"] = time.time()
        _PROMO["val"] = val


def promote_payload(limit=40):
    with _lock:
        if _PROMO["val"] is not None and (time.time() - _PROMO["ts"]) < PROMO_TTL:
            hit = dict(_PROMO["val"])
            hit["age_s"] = round(time.time() - _PROMO["ts"], 1)
            hit["cached"] = True
            return hit
        # stale-while-revalidate：已有快照但超龄时，立刻交旧快照（面板已如实显示
        # age_s，不假装实时），后台线程去重算。2026-10-03 实测：冷路径 9.5s，
        # 门/用户的超时是 12s，机器一有负载就贴边超时 → 偶发假红。让「有旧账可给」
        # 时永远不必等 9.5s，同时判定权仍在引擎 capability.py。
        if _PROMO["val"] is not None:
            hit = dict(_PROMO["val"])
            hit["age_s"] = round(time.time() - _PROMO["ts"], 1)
            hit["cached"] = True
            hit["stale"] = True
            threading.Thread(target=_promote_warm, args=(limit,), daemon=True).start()
            return hit
    val = _promote_compute(limit)
    with _lock:
        _PROMO["ts"] = time.time()
        _PROMO["val"] = val
    out = dict(val)
    out["age_s"] = 0.0
    out["cached"] = False
    return out

_DL = None


def _load_ledger():
    """按路径加载引擎 distill_ledger.py（不靠 sys.path 猴补丁）。"""
    global _DL
    if _DL is not None:
        return _DL
    path = os.path.join(COMPANY, "distill_ledger.py")
    spec = importlib.util.spec_from_file_location("_cd_distill_ledger", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _DL = mod
    return mod


def _distill_compute(limit=12):
    """蒸馏面：引擎蒸馏单账本（star+license+commit 同一行双证据）。

    判定权与阈值全在引擎 distill_ledger（TARGET_BOTH），驾驶舱只读转发，
    不自己数、不自己定门槛。与 /__promote 同一 fail-closed 口径：
    账本读不到或 verify 红 -> ok=False，不谎称"双证据齐全"。
    """
    empty = {"ok": False, "error": None, "rows": [], "n_rows": 0,
             "n_both": 0, "target": None, "verify_ok": False, "detail": None}
    try:
        m = _load_ledger()
        if m is None:
            return dict(empty, error="distill_ledger.py 不可加载")
        path = os.path.join(COMPANY, "DISTILL_LEDGER.md")
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        rows = []
        for ln in lines:
            if not ln.startswith("|") or "---" in ln or ln.startswith("| repo"):
                continue
            c = [x.strip() for x in ln.strip("|").split("|")]
            if len(c) < 5:
                continue
            rows.append({"repo": c[0], "stars": c[1], "license": c[2],
                         "commit": c[3], "state": c[4]})
        ok, detail = m.verify(path)
        target = int(m.TARGET_BOTH)
    except Exception as e:
        return dict(empty, error=("distill probe error: %s" % e)[:120])
    both = sum(1 for r in rows if r["state"] == "双证据")
    rows.sort(key=lambda r: -(int(r["stars"]) if r["stars"].isdigit() else 0))
    return {"ok": True, "error": None, "rows": rows[:limit],
            "n_rows": len(rows), "n_both": both, "target": target,
            "verify_ok": bool(ok), "detail": detail}

# 上面三条铁律，改动不许破：
#   1) argv 是固定列表，用户输入永不参与拼接，不用 shell=True。
#   2) 名字不在表里 -> 404。
#   3) 跨站页面能对 127.0.0.1 发 POST，故 do_POST 要求自定义头 X-AgentOS-Action；
#      浏览器跨源发自定义头会触发预检，而本服务永不回 CORS 头，预检必失败。


def fp(path):
    try:
        return "%s:%d" % (hashlib.sha256(open(path, "rb").read()).hexdigest()[:12],
                          int(os.path.getmtime(path)))
    except OSError:
        return "missing"


def push(event):
    with _lock:
        dead = []
        for q in _clients:
            try:
                q.put_nowait(event)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _clients.remove(q)


def tree_sig():
    """真树指纹：阶段目录的 (路径, mtime, 大小) 三元摘要，避开 report/ 等噪声。"""
    acc = hashlib.sha256()
    n = 0
    for dirpath, dirnames, filenames in os.walk(TREE):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "__pycache__", "node_modules", ".venv")]
        if "verify_all.sh" not in filenames and not dirpath.endswith("report"):
            continue
        for fn in sorted(filenames):
            p = os.path.join(dirpath, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            acc.update(("%s|%d|%d\n" % (os.path.relpath(p, TREE), st.st_mtime, st.st_size)).encode())
            n += 1
    return "%s:%d" % (acc.hexdigest()[:12], n)


def run(step, args):
    r = subprocess.run([PY] + args, cwd=ROOT, capture_output=True, text=True)
    tail = (r.stdout or r.stderr).strip().splitlines()[-2:]
    print("[%s] rc=%d %s" % (step, r.returncode, " | ".join(tail)), flush=True)
    return r.returncode


def run_act(name):
    """跑一个 allowlist 动作。输出**逐行**进 _act_cur.lines 并推给订阅者，
    所以长达数分钟的 egate 也能边跑边看见进度，而不是干等一个「运行中…���。
    返回 (rc, tail_lines, dur)。单飞由调用方保证。
    """
    desc, argv, tmo = ACTIONS[name]
    return _run_argv(argv, name, tmo)


def _run_argv(argv, name, tmo, keep=12, sink=None):
    """跑一条**固定 argv** 并把输出逐行喂给 SSE。返回 (rc, tail_lines, dur)。

    抽出它是为了让 run_act（allowlist 静态 argv）与 compile_goal（需求是运行时
    输入，argv 不能写死在 ACTIONS 里）共用同一套进程组/中止/超时/rc 语义 ——
    复制第二份的话，那份迟早与真实现漂移，而漂移的方向恰好是「中止杀不掉」。
    """
    t0 = time.time()
    # keep=None = 回传全部行。默认 12 行是给「给人看的 tail」用的（D77 的 tail 面板
    # 只显示末尾几行），但 D78 的轮次链要从引擎回执里 json.loads 出一整块 JSON ——
    # next-round 实测输出 17 行，截到 12 行会把 JSON 拦腰砍断，解析必失败。
    # 所以「要解析」和「给人看」两种需求在这里分开，而不是让调用方各自想办法。
    # 静态动作打印前 3 段就够认；goal-plan 的 argv 里带真实 --dir（是哪一格
    # 任务空间），必须打全 —— 截断了就等于让人以为编译写的是活项目根。
    say = feed if sink is None else sink
    say("$ " + (" ".join(argv) if name == "goal-plan"
                 else " ".join(argv[:3]) + " …"))
    try:
        p = subprocess.Popen(argv, cwd=ROOT, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1,
                             start_new_session=True)   # 独立进程组 -> 中止能杀整组
    except OSError as e:
        say("SPAWN_FAIL %s" % e)
        return 127, ["SPAWN_FAIL %s" % e], time.time() - t0
    with _act_proc["lock"]:
        _act_proc["p"] = p

    def pump():
        for line in p.stdout:                    # 逐行读，逐行推
            s = line.rstrip("\n")
            if s.strip():
                say(s)
    rd = threading.Thread(target=pump, daemon=True)
    rd.start()
    try:
        rc = p.wait(timeout=tmo)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            p.kill()
        say("TIMEOUT after %ds -> killed" % tmo)
        rc = 124
    rd.join(timeout=2)
    dur = time.time() - t0
    if _act_cur["aborted"]:      # 中止不是成功：rc 归一到 130（SIGINT 惯例）
        rc = 130
        say("ABORTED rc=130（用户中止，不记成功）")
    lines = list(_chain_buf) if sink is not None else None
    if lines is None:
        with _act_cur["lock"]:
            lines = list(_act_cur["lines"])
    return rc, (lines if keep is None else lines[-keep:]), dur


def _goal_taskdir(demand):
    """给这次目标开一个**独立任务空间**，返回其路径（不存在则建）。

    为什么绝不写活项目根：v136 台账已把 R1..R5 的 GOAL.json / ROUND-BIND sha 绑死，
    而引擎 v122 明确「set-goal 是改目标不是重开项目」—— 它会保留并覆写同目录的
    goal_chain。所以在根上跑一次 set-goal 就等于改写已交付的历史目标链。独立目录
    让每次编译都是可丢弃的草稿，编译失败也不留痕在活项目上。

    目录名只含时间戳 + 需求 sha 前 6 位：需求正文不进路径（既是注入面，也会把用户
    的目标留在磁盘目录名里到处可见）。

    末尾那 4 位随机后缀不是可有可无的洁癖：实测（不加它）同一秒内两次编译**同文本**
    需求会落进同一个目录。而引擎 v122 的 set-goal 是「改目标」—— 它会从同目录的
    既有 GOAL.json 继承 rounds / goal_chain。于是第二次编译拿到的不是干净草稿，
    而是上一次的状态，「每次编译都是可丢弃的草稿」这句话就是假的。撞名还会让
    work/tasks 下的产物与 _staging/request.json 的审计对不上号。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    h = hashlib.sha256(demand.encode("utf-8")).hexdigest()[:6]
    d = os.path.join(GOAL_TASKS, "g%s-%s-%s" % (stamp, h, uuid.uuid4().hex[:4]))
    os.makedirs(os.path.join(d, "work"), exist_ok=True)
    return d


def _goal_validate(req):
    """校验请求的**形状**（类型/条数/长度/前缀），返回 ((demand, acs, ngs, rounds), None)
    或 (None, 错误串)。

    这里刻意**不复刻**引擎 _quality 的语义规则（动词/数字/占位/单 AC…）。复刻一份
    就是两套真相：引擎升版改了判据，台面会静默按旧规则说另一套话，然后用户看到
    「台面说合格、引擎判红」。所以分工是死的 —— 「需求够不够详细具体」只由
    goal_loop._quality 判并原样透出；这里只挡「形状都不对」的输入（那类连引擎都
    接不住，且前端静默吞掉比报错更坏）。
    """
    def strlist(key, hi, cap):
        v = req.get(key) or []
        if isinstance(v, str):
            return None, "%s 必须是数组（收到字符串）" % key
        if not isinstance(v, list):
            return None, "%s 必须是数组" % key
        if len(v) > hi:
            return None, "%s 最多 %d 条（收到 %d）" % (key, hi, len(v))
        out = []
        for x in v:
            if not isinstance(x, str):
                return None, "%s 元素必须是字符串" % key
            x = x.strip()
            if not x:
                continue
            if len(x) > cap:
                return None, "%s 元素过长（%d > %d 字）" % (key, len(x), cap)
            if x.startswith("-"):
                # argparse 会把它当成下一个 flag 吃掉，值直接串位 ——
                # 挡在门外比让引擎报一个看不懂的错好。
                return None, "%s 元素不能以 - 开头：%r" % (key, x[:24])
            out.append(x)
        return out, None

    demand = req.get("demand")
    if not isinstance(demand, str) or not demand.strip():
        return None, "缺 demand（一句话需求）"
    demand = demand.strip()
    if len(demand) > 600:
        return None, "demand 过长（%d > 600 字）" % len(demand)
    if demand.startswith("-"):
        return None, "demand 不能以 - 开头（会被解析成参数）"
    acs, err = strlist("acs", 8, 200)
    if err:
        return None, err
    ngs, err = strlist("non_goals", 8, 200)
    if err:
        return None, err
    rounds = req.get("rounds", 3)
    if isinstance(rounds, bool) or not isinstance(rounds, int) or not (1 <= rounds <= 12):
        return None, "rounds 须 1..12 的整数"
    return (demand, acs, ngs, rounds), None


def compile_goal(demand, acs, non_goals, rounds):
    """把一句自然语言需求**真编译**成结构化计划。返回 (code, obj)。

    「真编译」= 真的 fork 公司引擎的 `goal_loop.py set-goal` 并读回它落的 GOAL.json，
    不是在台面里拿正则拼一张看起来像计划的东西。所以「需求不够详细具体」是引擎判的
    （goal_loop._quality → die 2），台面只负责如实显示，不自己猜行不行。
    进程组/中止/超时/rc 语义全部复用 _run_argv —— 与 run_act 同一份实现，
    复制第二份那份迟早与真实现漂移，而漂移的方向恰好是「中止杀不掉」。
    """
    taskdir = _goal_taskdir(demand)
    try:
        os.makedirs(os.path.dirname(GOAL_REQ), exist_ok=True)
        with open(GOAL_REQ, "w", encoding="utf-8") as f:
            json.dump({"demand": demand, "acs": acs, "non_goals": non_goals,
                       "rounds": rounds, "taskdir": taskdir,
                       "created": time.strftime("%F %T")}, f,
                      ensure_ascii=False, indent=1)
    except OSError as e:
        return 500, {"ok": False, "rc": -1, "verdict": "BLOCK",
                     "error": "暂存请求落盘失败 %s" % str(e)[:80]}
    argv = ["bash", COMPANY_SH, "goal", "set-goal",
            "--dir", taskdir, "--demand", demand, "--rounds", str(rounds)]
    for a in acs:
        argv += ["--ac", a]
    for g in non_goals:
        argv += ["--non-goal", g]
    rc, tail, dur = _run_argv(argv, "goal-plan", 120)
    plan = None
    if rc == 0:
        # rc=0 但读不回 GOAL.json = 假绿（引擎说成功、产物不在）。按未完成处理，
        # 绝不能把这种当成功返回给前端 —— 前端会把「空计划」渲染成一张空卡片，
        # 看起来像「这个目标没有 AC」，与事实相反。
        try:
            with open(os.path.join(taskdir, "work", "GOAL.json"), encoding="utf-8") as f:
                g = json.load(f)
            plan = {k: g.get(k) for k in
                    ("demand", "goal", "acs", "non_goals", "rounds_plan",
                     "goal_table", "depts", "state", "goal_id", "created")}
        except (OSError, ValueError) as e:
            rc = 1
            tail = list(tail) + ["GOAL.json 读取失败 %s" % str(e)[:80]]
    if rc != 0:
        return 200, {"ok": False, "rc": rc, "verdict": "BLOCK", "taskdir": taskdir,
                     "dur": round(dur, 2), "tail": tail,
                     "error": "引擎目标门判红：需求不够详细具体（见上方 tail 的具体理由）"}
    return 200, {"ok": True, "rc": 0, "verdict": "PASS", "taskdir": taskdir,
                 "dur": round(dur, 2), "plan": plan, "tail": tail,
                 "artifacts": ["work/GOAL.json", "work/GOAL.md", "work/GOALS.md"]}


# ---- D78 轮次链（D77 只做到「编译成计划」，闭环第二环在台面上够不到）----
# D79 加了第四个只读入口 close-preflight：它算「这一轮凭什么收、缺哪一项」，
# **不 fork 引擎写面**。起进程的那三个走单飞锁 + 流式 + 可中止；预检是毫秒级
# 读盘，不该占那条路，所以这里把「谁会起进程」单列一份，源码里一眼可见。
GOAL_CHAIN_FORK_OPS = ("status", "next-round", "round-goal")
GOAL_CHAIN_OPS = GOAL_CHAIN_FORK_OPS + ("close-preflight",)


def _chain_dir(req):
    """浏览器只能读项目内已有的任务空间，读不到项目外任何路径。"""
    d = (req.get("taskdir") or ROOT).strip()
    real = os.path.realpath(os.path.join(ROOT, d) if not os.path.isabs(d) else d)
    rroot = os.path.realpath(ROOT)
    if real != rroot and not real.startswith(rroot + os.sep):
        return None, "任务空间越界：只允许项目根或项目内的 work/tasks 子目录"
    if not os.path.isdir(real):
        return None, "任务空间不存在：%s" % os.path.relpath(real, ROOT)
    return real, None


# ---- 环境面（真实环境联动）：文件 / 代码 / 终端 ----
# 为什么做只读三面而不是「一个万能 shell」：目标要的是「随时接管」，
# 而接管的前提是**用户自己就能看到真实环境**。只读三面给了这份可见性，
# 又不会把 8790 变成任意 RCE 口子 —— 写面仍然只有 ACTIONS 那张固定 argv 表。
# 边界：路径一律 realpath 收进 ROOT，符号链接指向外面照样被拒（先 realpath 再比前缀）。

FS_MAX_BYTES = 262144        # 单文件读取上限 256 KiB，超了只回不截断的前缀 + 明确告知
FS_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv-pw", "venv"}

_webface = None


def _load_webface():
    """按路径加载 webface（不靠 sys.path 猴补丁，与 _load_intake 同一口径）。

    为什么要这一层而不是文件顶 `import webface`：门（env_gate 等）是用
    importlib 按路径加载 serve.py 的，那时 sys.path[0] 是**门脚本所在目录**
    甚至是调用者的 cwd，裸 import 会 ModuleNotFoundError —— 而那个失败会表现成
    「/__web 端点不存在」，看起来像功能没做，实际是导入方式选错。
    fail-closed：加载不到就返回一个恒假的 stub，路由仍然 200 + ok=false，
    台面上显示「取字节模块加载失败」这句原话，而不是静默没有这一面。
    """
    global _webface
    if _webface is not None:
        return _webface
    path = os.path.join(HERE, "webface.py")
    try:
        spec = importlib.util.spec_from_file_location("_cd_webface", path)
        if spec is None or spec.loader is None:
            raise ImportError("spec_from_file_location 返回 None")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:                          # noqa: BLE001
        # Python 在 except 块结束时删掉 `as e` 那个名字。这里若让 _Stub.fetch
        # 闭包引用 e，真正调它的时候拿到的是 NameError —— 也就是「取字节模块
        # 加载失败」这个 fail-closed 分支自己会抛，路由 500、断连接，用户看到的是
        # 一个空响应而不是那句原话。注释写的是 fail-closed，行为必须真的 fail-closed，
        # 所以先把理由**取成字符串**，再让 stub 引用那个字符串。
        why = str(e)[:120]

        class _Stub:
            @staticmethod
            def fetch(_raw):
                return {"ok": False, "error":
                        "取字节模块 webface.py 加载失败：%s" % why}
        mod = _Stub()
    _webface = mod
    return mod


# ------------------------------------------------------- D96（R6）项目空间
# 左栏要列**真实**在跑的项目，而不是摆两个好看的按钮 —— 目标点名要「左侧项目与
# 任务空间」，而 D95 实测左栏只有轮次脊线，项目空间在屏上根本不存在。
# 为什么必须是白名单而不是收用户传的绝对路径：/__fs 的安全边界是 realpath 前缀比对，
# 一旦允许任意根，「前缀」就成了用户自己给的那个字符串，沙箱当场失效。
# 白名单每项写死在代码里，且每次调用**现读全局 ROOT/TREE**：--tree（门跑隔离假树用）
# 覆盖后端点也跟着变，生产与门不许两套行为。
PROJ_ACTIVE_DEFAULT = "console"


def project_roots():
    """项目白名单。函数而非常量是有原因的：TREE 会被 --tree 覆盖，常量会咬住旧值。"""
    return (
        {"key": "console", "label": "AgentOS 驾驶舱", "root": ROOT,
         "role": "产品根：台面、门链、账本都在这里"},
        {"key": "future", "label": "future agent", "root": TREE,
         "role": "证据与企业门真树：轮次/覆盖数据由它抽取"},
    )


def project_keys():
    return [p["key"] for p in project_roots()]


def project_root(key):
    """按 key 取白名单里的真实根。未知 key 一律 fail-closed，绝不退化成 ROOT。

    为什么退化到 ROOT 看起来无害却要禁：它仍然是沙箱内，但会让「选了 A 项目、
    面板其实在读 B」这件事永远测不出来 —— 而「测不出来」正是这个项目一直在治的
    假绿，出现在自己身上更难发现。
    """
    for p in project_roots():
        if p["key"] == (key or "").strip():
            return p["root"], None
    return None, ("未知项目 key=%r（白名单只有 %s）"
                  % (str(key)[:40], "/".join(project_keys())))


PROJ_SCAN_CAP = 2000   # 每项目最多数 2000 个文件就收手；第二棵树实测 14 万文件


def project_facts(root):
    """一个项目的真事实：能不能读、顶层多少条目、文件数、最新改动。

    n_files 必须带 truncated：数到上限就停手时，不许把「数到一半」写成「一共 2000」。
    宁可显式说「≥2000」，也不要给一个看起来精确其实是假的数字。
    """
    out = {"exists": False, "readable": False, "n_top": 0, "n_files": 0,
           "newest_mtime": None, "truncated": False, "error": None}
    if not root:
        out["error"] = "没有根"
        return out
    if not os.path.isdir(root):
        out["exists"] = False
        out["error"] = "目录不存在或不是目录"
        return out
    out["exists"] = True
    if not os.access(root, os.R_OK | os.X_OK):
        out["error"] = "目录不可读"
        return out
    out["readable"] = True
    try:
        out["n_top"] = len(os.listdir(root))
    except OSError as ex:
        out["error"] = "读目录失败：%s" % str(ex)[:60]
        return out
    n, newest, trunc = 0, 0.0, False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in FS_SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.startswith(".") or ".bak" in fn:
                continue
            n += 1
            if n >= PROJ_SCAN_CAP:
                trunc = True
                break
            try:
                newest = max(newest, os.path.getmtime(os.path.join(dirpath, fn)))
            except OSError:
                pass
        if trunc:
            break
    out["n_files"] = n
    out["truncated"] = trunc
    out["newest_mtime"] = int(newest) if newest else None
    return out


def projects_payload():
    """左栏项目空间的数据源。ok=False 时必须带 error，不许空列表冒充「没有项目」。"""
    items = []
    for p in project_roots():
        f = project_facts(p["root"])
        items.append({"key": p["key"], "label": p["label"], "role": p["role"],
                      "root": p["root"], "ok": bool(f.get("readable")), "facts": f})
    usable = [i for i in items if i["ok"]]
    active = None
    for i in usable:
        if i["key"] == PROJ_ACTIVE_DEFAULT:
            active = i["key"]
            break
    if active is None and usable:
        active = usable[0]["key"]
    return {"ok": bool(usable), "n_projects": len(items), "active": active,
            "default": PROJ_ACTIVE_DEFAULT, "scan_cap": PROJ_SCAN_CAP,
            "projects": items,
            "error": None if usable else
            "没有任何项目可读：项目空间不可用（不是「零个项目」）"}


def _fs_sandbox(rel, root=None):
    """把请求里的相对路径解析成 ROOT 内的真实路径。越界一律返回 None。

    为什么要 realpath 之后再比前缀：os.path.join + startswith 的朴素写法挡不住
    `work/../../../../etc/passwd`，也挡不住指向 ROOT 外的符号链接。先 realpath
    再比 `ROOT + os.sep` 才是唯一可靠的边界。
    """
    rroot = os.path.realpath(root or ROOT)
    p = (rel or "").strip()
    if not p or p == ".":
        return rroot, None
    real = os.path.realpath(os.path.join(rroot, p))
    if real != rroot and not real.startswith(rroot + os.sep):
        return None, ("路径越界：只能读当前项目根内的路径"
                     "（项目根=%s，收到 %r）" % (rroot, p[:80]))
    return real, None


def fs_list(rel, depth=1, root=None):
    """列目录。返回条目列表；depth 上限 2，防一次点开整棵树。"""
    rroot = os.path.realpath(root or ROOT)
    real, err = _fs_sandbox(rel, rroot)
    if err:
        return {"ok": False, "error": err}
    if not os.path.isdir(real):
        return {"ok": False, "error": "不是目录：%s" % (rel or ".")}
    entries = []
    try:
        for name in sorted(os.listdir(real)):
            if name.startswith(".") and name != ".gitignore":
                continue
            if name in FS_SKIP_DIRS or ".bak" in name:
                continue
            full = os.path.join(real, name)
            isd = os.path.isdir(full)
            try:
                size = 0 if isd else os.path.getsize(full)
            except OSError:
                size = 0
            entries.append({"name": name, "dir": isd, "size": size,
                            "path": os.path.relpath(full, rroot)})
        if depth > 1:
            for e in entries:
                if e["dir"]:
                    sub = fs_list(e["path"], depth - 1, rroot)
                    e["children"] = sub.get("entries") if sub.get("ok") else []
    except OSError as ex:
        return {"ok": False, "error": "读目录失败：%s" % str(ex)[:80]}
    return {"ok": True, "path": rel or ".", "entries": entries, "n": len(entries)}


def fs_read(rel, root=None):
    """读一个文本文件。截断必须**显式告知**，不许让用户以为读全了。"""
    real, err = _fs_sandbox(rel, root)
    if err:
        return {"ok": False, "error": err}
    if os.path.isdir(real):
        return {"ok": False, "error": "这是目录，点开看条目而不是读内容"}
    if not os.path.isfile(real):
        return {"ok": False, "error": "文件不存在：%s" % (rel or "")}
    try:
        size = os.path.getsize(real)
        with open(real, "rb") as f:
            raw = f.read(FS_MAX_BYTES + 1)
    except OSError as ex:
        return {"ok": False, "error": "读文件失败：%s" % str(ex)[:80]}
    trunc = len(raw) > FS_MAX_BYTES
    raw = raw[:FS_MAX_BYTES]
    if b"\x00" in raw[:8192]:
        return {"ok": False, "error": "二进制文件，不在代码面里渲染",
                "binary": True, "size": size}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"ok": False, "error": "非 UTF-8 文本，不在代码面里渲染", "size": size}
    return {"ok": True, "path": rel, "size": size, "truncated": trunc,
            "limit": FS_MAX_BYTES, "n_lines": text.count("\n") + 1, "text": text}


# 终端只放行只读观察类命令，且 argv 前缀固定。
# 为什么不给自由 shell：8790 是常驻在本机 loopback 的长期进程，任意 shell
# 口子等于把整台开发机交给任何能打开 localhost 的页面。这里只保留「看一眼
# 项目状态」的能力，写与删一律不给。
# 白名单里每条命令写的是**候选绝对路径**，导入时按 os.path.exists 解析成真路径。
# 为什么不一条写死（D88 的真缺陷就是这么来的）：macOS 上 /bin/wc、/bin/head、
# /bin/tail 三个根本不存在，写死之后终端里 `wc -l index.html` 只会回一句含糊的
# 「命令起不来」—— 白名单里混着一条永远跑不起来的命令，还在台面上冒充能力。
# 解析不出来的那一条直接不进白名单：用户看到的是「命令不在只读白名单里」这句
# 明确的话，而不是一个假能力。fail-closed 优先于凑齐表长。
TERM_CANDIDATES = {
    "pwd": ["/bin/pwd", "/usr/bin/pwd"],
    "ls": ["/bin/ls", "/usr/bin/ls"],
    "cat": ["/bin/cat", "/usr/bin/cat"],
    "wc": ["/usr/bin/wc", "/bin/wc"],
    "head": ["/usr/bin/head", "/bin/head"],
    "tail": ["/usr/bin/tail", "/bin/tail"],
    "grep": ["/usr/bin/grep", "/bin/grep"],
    "find": ["/usr/bin/find", "/bin/find"],
    "stat": ["/usr/bin/stat", "/bin/stat"],
    "date": ["/bin/date", "/usr/bin/date"],
    "whoami": ["/usr/bin/whoami", "/bin/whoami"],
    "git": ["/usr/bin/git", "/bin/git"],
    "python3": ["/opt/miniconda3/bin/python3", "/usr/bin/python3"],
}
TERM_CMDS = {}
TERM_UNRESOLVED = []
for _name, _cands in TERM_CANDIDATES.items():
    _hit = next((c for c in _cands if os.path.exists(c)), None)
    if _hit is None:
        TERM_UNRESOLVED.append(_name)
        continue
    TERM_CMDS[_name] = [_hit, "-c"] if _name == "python3" else [_hit]
TERM_MAX_OUT = 32768
TERM_TIMEOUT = 20


def term_run(argv):
    """跑一条只读命令。返回真实 rc 与真实输出 —— 终端不替命令编造成功。

    python3 -c 是本表里唯一能写文件的一条，放行它是有意的：让终端真能算数
    （算 rc、算哈希、算行数），否则终端只是个假的复读机。限制是 cwd 收在
    ROOT、超时硬杀、PATH 固定，且落盘结果一律不回显，所以它无法被用来伪造
    「交付已完成」的证据 —— 证据只能来自 ACTIONS 那张固定 argv 表。
    """
    if not isinstance(argv, list) or not argv:
        return {"ok": False, "error": "argv 必须是数组且非空"}
    base = TERM_CMDS.get(str(argv[0]))
    if not base:
        return {"ok": False, "error": "命令不在只读白名单里：%s（可跑：%s）"
                % (str(argv[0])[:40], " ".join(sorted(TERM_CMDS)))}
    if len(argv) > 24:
        return {"ok": False, "error": "参数过多"}
    if len(base) == 2 and base[1] == "-c":
        if len(argv) < 2:
            return {"ok": False, "error": "python3 需要一段代码"}
        real_argv = [base[0], str(argv[1])] + [str(a) for a in argv[2:]]
    else:
        real_argv = list(base) + [str(a) for a in argv[1:]]
    t0 = time.time()
    try:
        p = subprocess.run(real_argv, capture_output=True, cwd=ROOT,
                           timeout=TERM_TIMEOUT,
                           env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                                "HOME": ROOT, "LANG": "en_US.UTF-8"})
        text = ((p.stdout or b"") + (p.stderr or b"")).decode("utf-8", "replace")
        return {"ok": True, "argv": real_argv, "rc": p.returncode,
                "out": text[:TERM_MAX_OUT],
                "truncated": len(text) > TERM_MAX_OUT,
                "dur": round(time.time() - t0, 2)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "命令超时（>%ds）已杀掉" % TERM_TIMEOUT,
                "rc": None, "dur": round(time.time() - t0, 2)}
    except OSError as ex:
        return {"ok": False, "error": "命令起不来：%s" % str(ex)[:80]}


def read_ledger(d):
    """读引擎落盘的 GOAL.json 整理成轮次表。读不到就报错，不返回空表。"""
    p = os.path.join(d, "work", "GOAL.json")
    try:
        with open(p, encoding="utf-8") as f:
            g = json.load(f)
    except (OSError, ValueError) as e:
        return None, "读不到 %s：%s（这个任务空间还没跑过 set-goal）" % (os.path.relpath(p, ROOT), str(e)[:80])
    if not isinstance(g, dict):
        return None, "GOAL.json 结构不是对象"
    tbl = g.get("goal_table") or []
    rounds = g.get("rounds") or []
    return {
        "demand": g.get("demand"), "goal": g.get("goal"),
        "state": g.get("state"), "rounds_plan": g.get("rounds_plan"),
        "goal_id": g.get("goal_id"),
        "chain": [{"n": c.get("n"), "goal_id": c.get("goal_id"),
                   "objective": c.get("objective")} for c in (g.get("goal_chain") or [])],
        "table": [{"n": r.get("n"), "state": r.get("state"),
                   "giga_run": r.get("giga_run"), "goal": r.get("goal"),
                   "ac": r.get("ac"), "delta": r.get("delta"),
                   "goal_id": r.get("goal_id")} for r in tbl],
        "rounds": [{"n": r.get("n"), "state": r.get("state"),
                    "giga_run": r.get("giga_run"), "delta": r.get("delta"),
                    "verdict": r.get("verdict")} for r in rounds],
        "acs": g.get("acs") or [], "non_goals": g.get("non_goals") or [],
    }, None


def run_chain(op, d, n):
    """跑一个只读引擎入口并解析它的结构化回执。返回 (rc, obj)。

    解析方式：引擎 _ok 打的是一整块 JSON，从第一个 '{' 起 json.loads。
    不用正则捞字段 —— 捞出来的 next_round 与引擎真说的不是一回事，
    而台面一旦自己拼一句人话，就是在替引擎发言。
    """
    argv = ["bash", COMPANY_SH, "goal", op, "--dir", d]
    if op == "round-goal":
        argv += ["--round", str(n)]
    del _chain_buf[:]
    rc, tail, dur = _run_argv(argv, "goal-chain", 60, keep=None, sink=chain_say)
    blob = "\n".join(tail)
    i = blob.find("{")
    payload = None
    if i >= 0:
        try:
            payload = json.loads(blob[i:])
        except ValueError:
            payload = None
    return rc, {"rc": rc, "dur": round(dur, 2), "tail": tail, "payload": payload}


_GOAL_LOOP = None


def _goal_loop():
    """引擎 goal_loop 本体 —— 收轮判据的唯一真源，不许在台面复刻一份。

    复刻就等于新开一道漂移口：引擎改一条硬门、台面还按旧规则说「能收」，
    而 close-round 真跑 rc=2 —— 说谎的是台面。所以这里 import 引擎本体，
    直接用它的 _bind_check / _giga_evidence / DELTA_EMPTY / EV_RESULT /
    _ac_ids。导入零副作用（模块顶层只有赋值，main 在 __main__ 之后，
    已用 ast 核过没有别的顶层可执行语句）。取不到就 fail-closed。
    """
    global _GOAL_LOOP
    if _GOAL_LOOP is None:
        import importlib.util
        import sys as _sys
        try:
            p = os.path.join(COMPANY, "goal_loop.py")
            spec = importlib.util.spec_from_file_location("_cd_goal_loop", p)
            m = importlib.util.module_from_spec(spec)
            # company.sh 是 `exec python3 $HERE/goal_loop.py` —— 引擎靠 sys.path[0]
            # 找到自己的 skill_home.py。spec_from_file_location **不**把 $HERE 放进
            # sys.path，直接 exec 会 ModuleNotFoundError: skill_home，判据源就取不到。
            # 这里临时补一条再原样撤掉，别污染服务端进程的 import 表。
            _added = COMPANY not in _sys.path
            if _added:
                _sys.path.insert(0, COMPANY)
            try:
                spec.loader.exec_module(m)
            finally:
                if _added:
                    try:
                        _sys.path.remove(COMPANY)
                    except ValueError:
                        pass
            _GOAL_LOOP = m
        except Exception:
            _GOAL_LOOP = False
    return _GOAL_LOOP or None


def close_preflight(d, n, delta, evidence, verdict, ac_moved):
    """收轮就绪度：只读盘 + 只读草案，**一个字节都不写**。

    判据顺序与引擎 cmd_close_round 一致，逐条用引擎自己的函数算。分两族：
      disk:*  与输入无关 —— 轮账在不在、轮开没开、绑 sha 对不对、G0 是不是
             真产物、还剩几轮没收。
      draft:* 用户在台面输入框打的字 —— 按引擎的 delta/evidence/AC 判据
             预判会不会被判红。台面不代写，只预判。
    返回 (rc, obj)。rc 恒 0 = 「读到了就 0」，能不能收看 obj['ready']；
    读不到盘才是真失败（ok=False），且此时绝不给 ready。
    """
    import re as _re
    delta = (delta or "").strip()
    ev = (evidence or "").strip()
    # 刻意**不**给默认 CONTINUE：引擎 _real_main 里 `a.verdict not in (SHIP,BLOCK,CONTINUE)`
    # 就 die(2)，省略 --verdict 同样 rc=2。这里默认空串，让「忘了填裁决」也判红，
    # 否则台面说就绪、引擎真跑 rc=2 —— 说谎的是台面。
    vd = (verdict or "").strip().upper()
    am = (ac_moved or "").strip()
    gates = []

    def gate(gid, kind, label, ok, why):
        gates.append({"id": gid, "kind": kind, "label": label,
                      "ok": bool(ok), "why": str(why or "")[:400]})
        return bool(ok)

    gl = _goal_loop()
    base = {"round": n, "taskdir": os.path.relpath(d, ROOT), "readonly": True,
            "draft": {"delta": delta, "evidence": ev, "verdict": vd, "ac_moved": am},
            "gates": gates, "ok": True, "ready": False, "verdict": "BLOCK"}
    if gl is not None:
        # 引擎 GF_RUNS = os.environ['GF_OUT'] or ~/.codex/gf-runs/artifacts，
        # 在**导入那一刻**定死。台面进程与用户终端的 env 不同时会读到另一个账目录，
        # 于是 bind/giga 两条门给出与「用户真跑 close-round」不同的答案。
        # 与其悄悄按某个目录判红/判绿，不如把**这次判的是哪个目录**摆到屏上：
        # 判红时用户第一眼就能确认「是不是我看错目录了」。
        base["gf_runs"] = getattr(gl, "GF_RUNS", None)
        base["gf_runs_from_env"] = bool(os.environ.get("GF_OUT"))
    if gl is None:
        gate("engine", "disk", "引擎 goal_loop.py 可读（判据真源）", False,
             "取不到引擎判据本体（%s）：拿不到判据就不给就绪度（fail-closed）" % COMPANY)
        base["ok"] = False
        base["error"] = "引擎 goal_loop.py 不可读，无法自证就绪度（fail-closed）"
        return 1, base
    gp = os.path.join(os.path.relpath(d, ROOT), "work", "GOAL.json")
    G = gl._load(d, "GOAL.json")
    if not isinstance(G, dict):
        gate("goal_json", "disk", "读得到 work/GOAL.json", False,
             "读不到 %s（这个任务空间还没跑过 set-goal）" % gp)
        base["ok"] = False
        base["error"] = "读不到 %s：这个任务空间还没跑过 set-goal" % gp
        return 1, base
    gate("goal_json", "disk", "读得到 work/GOAL.json", True,
         "state=%s · AC %d 条 · 轮次计划 R1..R%s"
         % (G.get("state") or "—", len(G.get("acs") or []), G.get("rounds_plan") or "?"))
    rf = gl._round_file(d, n)
    gate("round-file", "disk", "本轮账 work/ROUNDS/R%d.md 在" % n,
         os.path.isfile(rf), "无 %s，先 plan-round 开轮" % os.path.relpath(rf, ROOT))
    rec = next((r for r in (G.get("rounds") or []) if r.get("n") == n), None)
    gate("opened", "disk", "R%d 已开台账" % n, bool(rec),
         "R%d 未开台账（先 plan-round --round %d 开轮才有本轮专属账）" % (n, n))
    rstate = (rec or {}).get("state") or "—"
    gate("not-closed", "disk", "R%d 还没收过（现 %s）" % (n, rstate),
         rstate != "CLOSED", "R%d 已关闭，禁止重复收轮" % n)
    row = gl._trow(G, n)
    gate("row", "disk", "目标表有 R%d 行" % n, row is not None,
         "目标表缺 R%d 行：没有目标行就没有「这轮提升什么」，不许收轮" % n)
    giga = (rec or {}).get("giga_run") or ""
    if rec is not None and row is not None:
        bok, bmsg = gl._bind_check(giga, n, row, rec)
        gate("bind", "disk", "本轮账↔目标表绑定 sha 对得上", bok, bmsg)
        eok, emsg = gl._giga_evidence(giga)
        gate("giga", "disk", "本轮账真跑过 G0（不是 new-run 空账）", eok, emsg)
    tbl = G.get("goal_table") or []
    pend = [r for r in tbl if r.get("n") != n
            and (r.get("state") or gl.TBL_TODO) not in gl.TBL_DONE_STATES]
    ps = "、".join("R%s(%s)" % (r.get("n"), r.get("state") or gl.TBL_TODO)
                   for r in pend) or "无"
    gate("ship-pending", "disk", "SHIP 时不许有未收轮（现未收：%s）" % ps,
         vd != "SHIP" or not pend,
         "目标表还有 %d 轮未收（%s）：每轮都要有提升才叫完成，不许提前报 SHIP"
         % (len(pend), ps))
    gate("verdict-field", "draft", "裁决 ∈ SHIP|BLOCK|CONTINUE",
         vd in ("SHIP", "BLOCK", "CONTINUE"),
         "--verdict 须 SHIP|BLOCK|CONTINUE（收到 %r）" % vd)
    zero = (not delta) or bool(gl.DELTA_EMPTY.match(delta))
    if zero and vd != "BLOCK":
        gate("delta", "draft", "delta 不是空/占位", False,
             "delta 为空/占位 → 这一轮不算数：R%d 不予收轮（保持 OPEN），"
             "close-round 不记账、目标表该行也不进「已收轮」" % n)
    else:
        gate("delta", "draft", "delta 不是空/占位", True, "%d 字" % len(delta))
    if not ev or gl.EVIDENCE_JUNK.match(ev):
        gate("ev-junk", "draft", "evidence 不是空话术", False,
             "evidence 是空话术：必须写真命令 + 退出码（禁「应该/大概/相信」）")
    else:
        gate("ev-junk", "draft", "evidence 不是空话术", True, "%d 字" % len(ev))
    rm = gl.EV_RESULT.search(ev)
    gate("ev-result", "draft", "evidence 带结果标记（rc=/exit=/PASS…）",
         bool(rm), "evidence 无结果标记：必须带 rc=/exit=/退出码/PASS/FAIL 之一，"
         "否则不算证据" if not rm else "命中 %r" % rm.group(0))
    hm = gl.EV_HEDGE.search(ev)
    gate("ev-hedge", "draft", "evidence 无口头断言", not hm,
         "evidence 含口头断言（%s）：禁未验证宣称，写真命令与退出码"
         % (hm.group(1) if hm else ""))
    if vd == "SHIP" and not _re.search(r"(rc=0|exit=0|退出码\s*0|SHIP_OK|PASS)", ev, _re.I):
        gate("ship-rc0", "draft", "SHIP 的 evidence 里有 rc=0/exit=0", False,
             "verdict=SHIP 但证据里没有 rc=0/exit=0/SHIP_OK —— 禁未验证宣称")
    else:
        gate("ship-rc0", "draft", "SHIP 的 evidence 里有 rc=0/exit=0", True, "—")
    ids, aerr = gl._ac_ids(delta, am, len(G.get("acs") or []))
    if aerr:
        gate("ac", "draft", "指名了动了哪条 AC（且合法）", False,
             "AC 指名不合法：%s" % "；".join(aerr))
    elif not ids and vd != "BLOCK":
        gate("ac", "draft", "指名了动了哪条 AC（且合法）", False,
             "delta 没指名动了哪条 AC：这轮提升无法归因（「把一堆功能都改了」不算"
             "一轮提升）。本项目 AC 范围 AC1..AC%d" % len(G.get("acs") or []))
    else:
        gate("ac", "draft", "指名了动了哪条 AC（且合法）", True,
             "ac_moved=%s" % (",".join(ids) or "（主动叫停，未动 AC）"))
    ready = all(x["ok"] for x in gates)
    base.update({"ready": ready, "verdict": "PASS" if ready else "BLOCK",
                 "state": G.get("state"), "rstate": rstate, "giga_run": giga or None,
                 "row_state": (row or {}).get("state") or gl.TBL_TODO,
                 "acs": G.get("acs") or [], "ac_moved": ids,
                 "pending": [{"n": r.get("n"), "state": r.get("state") or gl.TBL_TODO}
                             for r in pend]})
    argv = None
    # 只在 ready 时给命令：不就绪还摆一行 close-round，是在邀请用户去跑一条
    # 注定 rc=2 的命令。没就绪度的时候不给命令，才是真的「缺哪项补哪项」。
    if ready and delta and ev:
        argv = ["bash", COMPANY_SH, "goal", "close-round", "--dir", d,
                "--round", str(n), "--delta", delta, "--evidence", ev,
                "--verdict", vd]
        if am:
            argv += ["--ac-moved", am]
    base["close_argv"] = argv
    return 0, base


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, fmt, *a):  # 静音访问日志，只留事件
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("X-AgentOS-Build", fp(os.path.join(ROOT, "index.html")))
        super().end_headers()

    def do_GET(self):
        # 环境面三个只读端点。放在最前面是因为它们最便宜，也最常被面板问。
        # 它们不需要 CSRF 头：全是只读的，读不到 ROOT 外的任何东西，也不 fork 进程。
        if self.path.startswith("/__projects"):
            # D96：左栏「项目空间」的唯一数据源。只读、不 fork、不写盘，与另外三个
            # 环境端点同口径。它报的是**真实**目录事实（可读/条目数/文件数/最新改动），
            # 拿不到就 ok=false 带 error —— 左栏不许拿占位数据假装有项目。
            self._json(200, projects_payload())
            return
        if self.path.startswith("/__fs"):
            qs = urllib.parse.urlparse(self.path).query
            q = urllib.parse.parse_qs(qs)
            rel = (q.get("path") or [""])[0]
            # 未知 proj 一律 400 fail-closed，绝不「忽略这个参数、照旧读产品根」：
            # 那样切错项目时面板会安静地读另一个根，而这类静默错位正是本项目
            # 一路在治的假绿。宁可报错让人看见。
            rroot, perr = project_root((q.get("proj") or [""])[0] or PROJ_ACTIVE_DEFAULT)
            if perr:
                self._json(400, {"ok": False, "error": perr,
                                 "code": "unknown_project",
                                 "projects": project_keys()})
                return
            if (q.get("op") or ["list"])[0] == "read":
                self._json(200, fs_read(rel, rroot))
            else:
                try:
                    depth = max(1, min(2, int((q.get("depth") or ["1"])[0])))
                except ValueError:
                    depth = 1
                self._json(200, fs_list(rel, depth, rroot))
            return
        # D88 浏览器/预览面的取字节端点。只读、不 fork、不写盘、不需要 CSRF 头，
        # 与上面三个环境面同口径。安全边界（scheme / 凭据 / 回环 host / 不跟随重定向）
        # 全在 webface 模块里，注释写了为什么必须是这四条 —— 别为了「加个站点看看」
        # 把 host 校验放松，那一行就是本机所有端口的敞开口。
        if self.path.startswith("/__web"):
            qs = urllib.parse.urlparse(self.path).query
            q = urllib.parse.parse_qs(qs)
            self._json(200, _load_webface().fetch((q.get("url") or [""])[0]))
            return
        if self.path.startswith("/__health"):
            body = json.dumps({
                "ok": True, "root": ROOT, "tree": TREE,
                "index": fp(os.path.join(ROOT, "index.html")),
                "data": fp(os.path.join(ROOT, "data.json")),
                "clients": len(_clients), "stats": STATS,
                "actions": sorted(ACTIONS), "act_running": _act_state["running"],
                "act_last": _act_state["last"],
                "act_run": _act_cur["run"], "act_aborted": _act_cur["aborted"],
                # 常驻实例跑的是哪一份代码？launchd 配了 KeepAlive，进程**从不**因为
                # 你改了 serve.py 而重载 —— 于是 /__health 报 ok=true、8 个动作齐全，
                # 台面看着一切正常，实际跑的是昨天那份代码。
                # v118 实测：常驻进程报的项数落后源码整整一代（差 N 项）。
                # 门走自起实例（永远新代码），所以门绿着，盲区在这儿。
                # 这个字段 + act_gate 的对码门把盲区焊死：常驻实例的指纹 == 本地源码指纹。
                "code_sha": CODE_SHA, "code_mtime": CODE_MTIME,
            }, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/__act/status"):
            # 只读状态面：执行面此刻在忙什么、已跑多久、预算多久、上一次结果是什么。
            # 三个消费者都指它：`agentos -p` 决定要不要等（409 只有一瞬，转个头就没了，
            # 而客户端需要的是「现在还忙不忙」这个可持续查询的事实）；`agentos sessions`
            # 复盘；以及用户在终端里 `curl /__act/status` 自查。不 fork、不写盘、无 CSRF。
            st = _act_state
            self._json(200, {
                "ok": True,
                "running": st.get("running"),
                "elapsed": round(time.time() - st["started"], 1)
                            if st.get("started") else None,
                "tmo": ACTIONS.get(st.get("running"), (None, None, None))[2]
                       if st.get("running") else None,
                "last": st.get("last"),
                "actions": sorted(ACTIONS),
            })
            return
        if self.path.startswith("/__act/resolve"):
            # 只读路由面：把一句人话翻成 allowlist 内的动作名。
            # **不执行任何东西** —— 授权与执行仍在 POST /__act 那条路上。
            qs = urllib.parse.urlparse(self.path).query
            q = urllib.parse.parse_qs(qs).get("q", [""])[0]
            r = resolve_intent(q)
            r["allowlist"] = sorted(ACTIONS)
            self._json(200, r)
            return
        if self.path.startswith("/__act/recommend"):
            try:
                with open(os.path.join(ROOT, "data.json"), encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError) as e:
                self._json(503, {"ok": False, "error": "data.json 不可读: %s" % e})
                return
            recs = recommend(data)
            self._json(200, {"ok": True, "verdict": data.get("global_verdict"),
                             "n": len(recs), "recommend": recs})
            return
        if self.path.startswith("/__recurrence"):
            p = recurrence_payload()
            self._json(200 if p.get("ok") else 503, p)
            return
        if self.path.startswith("/__promote"):
            p = promote_payload()
            self._json(200 if p.get("ok") else 503, p)
            return
        if self.path.startswith("/__capmap"):
            # 纯只读：GET 就够，不需要 CSRF 头，也不需要进 POST 的 allowlist。
            # 读不到账 → 503，屏上按 BLOCK 渲染，绝不返回 ok=true 的空地图。
            p = capmap_payload()
            self._json(200 if p.get("ok") else 503, p)
            return
        if self.path.startswith("/__distill"):
            p = _distill_compute()
            self._json(200 if p.get("ok") else 503, p)
            return
        if self.path.startswith("/__act/stream"):
            try:
                qs = self.path.split("?", 1)[1] if "?" in self.path else ""
                cursor = int(dict(p.split("=", 1) for p in qs.split("&") if "=" in p).get("cursor", "0"))
            except ValueError:
                cursor = 0
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                self.wfile.write(b"event: hello\ndata: stream\n\n")
                self.wfile.flush()
                idle = 0
                run_sent = None
                while True:
                    with _act_cur["lock"]:
                        lines = list(_act_cur["lines"])
                        done, rc = _act_cur["done"], _act_cur["rc"]
                        run_now = _act_cur["run"]
                    if run_now and run_now != run_sent:
                        # 中止面要靠 run id 指名道姓，所以 run id 必须**先**推给前端。
                        self.wfile.write(("data: __RUN__ %s\n\n" % run_now).encode())
                        run_sent = run_now
                        self.wfile.flush()
                    while cursor < len(lines):
                        self.wfile.write(("data: %s\n\n" % lines[cursor]).encode())
                        cursor += 1
                    if cursor < len(lines):
                        self.wfile.flush()
                        continue
                    if done:
                        self.wfile.write(("data: __RC__ %s\n\n" % rc).encode())
                        self.wfile.flush()
                        break
                    try:
                        q = queue.Queue(maxsize=8)
                        with _lock:
                            _clients.append(q)
                        q.get(timeout=1.5)
                        with _lock:
                            if q in _clients:
                                _clients.remove(q)
                        idle = 0
                    except queue.Empty:
                        idle += 1
                        if idle % 6 == 0:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ValueError):
                pass
            return
        if self.path.startswith("/__watch"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q = queue.Queue(maxsize=8)
            with _lock:
                _clients.append(q)
            try:
                self.wfile.write(b"event: hello\ndata: connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        ev = q.get(timeout=5)
                        self.wfile.write(("event: %s\ndata: %s\n\n" % (ev, time.time())).encode())
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ValueError):
                pass
            finally:
                with _lock:
                    if q in _clients:
                        _clients.remove(q)
            return
        super().do_GET()

    def _goal_chain(self, req):
        """轮次链：三个 fork 引擎的只读入口 + 盘上轮次表 + 收轮就绪度预检。"""
        op = req.get("op")
        if op not in GOAL_CHAIN_OPS:
            # 只放行三个**只读**入口。bind-goal / plan-round / close-round 是写操作，
            # 且各自要外部真凭据（对话 goal id、本轮专属账、真 G0 产物）——
            # 在浏览器里替用户编一个 goal id 就能收轮，等于把闭环第二环演成假的。
            self._json(400, {"ok": False, "verdict": "BLOCK", "error":
                             "op 只允许 %s（四个只读入口）；写操作不在台面上开放"
                             % "/".join(GOAL_CHAIN_OPS)})
            return
        d, derr = _chain_dir(req)
        if derr:
            self._json(400, {"ok": False, "verdict": "BLOCK", "error": derr})
            return
        try:
            n = int(req.get("round") or 1)
        except (TypeError, ValueError):
            n = 1
        if not 1 <= n <= 24:
            self._json(400, {"ok": False, "verdict": "BLOCK",
                             "error": "round 越界（1..24）"})
            return
        if op == "close-preflight":
            self._close_preflight(req, d, n)
            return
        if not _act_lock.acquire(blocking=False):
            self._json(409, _act_busy())
            return
        try:
            _act_state["running"] = "goal-chain"
            _act_state["started"] = time.time()
            run_id = stream_start("goal-chain")
            rc = -1
            try:
                rc, out = run_chain(op, d, n)
                obj = {"op": op, "taskdir": os.path.relpath(d, ROOT), "round": n,
                       "run": run_id}
                obj.update(out)
                obj["ok"] = rc == 0
                if op == "status":
                    led, lerr = read_ledger(d)
                    if lerr:
                        # 引擎 status rc=0 但盘上读不出轮次表 = 读不到就不给表，
                        # 绝不能返回一张空表让用户以为「这个项目没有轮次」。
                        obj.update({"ok": False, "ledger_error": lerr})
                    else:
                        obj["ledger"] = led
                if rc != 0:
                    obj["error"] = "引擎 %s rc=%d（见 tail 原话）" % (op, rc)
                obj["verdict"] = "PASS" if obj.get("ok") else "BLOCK"
            except Exception as e:                    # noqa: BLE001
                rc = 1
                obj = {"ok": False, "rc": 1, "verdict": "BLOCK", "op": op,
                       "error": "读轮次链异常 %s" % str(e)[:120], "tail": []}
            finally:
                stream_end(rc)      # 订阅者绝不能因为异常被吊在半空
            obj["aborted"] = _act_cur["aborted"]
            _act_state["last"] = {"action": "goal-chain", "rc": rc,
                                  "dur": obj.get("dur"), "tail": obj.get("tail"),
                                  "run": run_id, "aborted": _act_cur["aborted"]}
            self._json(200, obj)
        finally:
            _act_state["running"] = None
            # started 必须一起清：只清 running 的话，空闲后 /__act/status 的
            # elapsed 会从动作开始那一刻一直往上累，读起来像「还有个动作在跑」。
            _act_state["started"] = None
            _act_lock.release()

    def _close_preflight(self, req, d, n):
        """收轮就绪度（D79）：只读，一个字节都不写。

        刻意**不进单飞锁、不 stream_start**：它不 fork 任何引擎进程，是毫秒级
        读盘。占着执行面的锁会让用户跑长门时连「这轮能不能收」都问不了；
        而 CSRF 头、body 上限、路径越界、round 越界这四道硬门在
        do_POST 与 _chain_dir 里已经给到了 —— 预检不比任何写面更松。
        """
        t0 = time.time()
        try:
            rc, obj = close_preflight(d, n, req.get("delta"), req.get("evidence"),
                                      req.get("verdict"), req.get("ac_moved"))
        except Exception as e:                            # noqa: BLE001
            rc, obj = 1, {"ok": False, "ready": False, "verdict": "BLOCK",
                          "gates": [], "error": "预检异常 %s" % str(e)[:120]}
        obj["op"] = "close-preflight"
        obj["rc"] = rc
        obj["dur"] = round(time.time() - t0, 2)
        self._json(200, obj)

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # /__goal/plan 与 /__act/* 共用同一套执行面硬门：CSRF 头、body 上限、单飞锁、
        # 可中止、可流式。目标台不是「另一个更松的口子」。
        # /__term 同享 CSRF 头与 body 上限，但**不进单飞锁**：终端要能在一个长门
        # 跑着的时候照样能查状态，占着执行面锁会让「随时接管」变成假话。
        if not (self.path.startswith("/__act") or self.path.startswith("/__goal")
                or self.path.startswith("/__term")):
            self._json(404, {"ok": False, "error": "no-such-endpoint"})
            return
        if self.headers.get("X-AgentOS-Action") != "1":
            self._json(403, {"ok": False, "error": "missing X-AgentOS-Action header"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 4096:
                self._json(413, {"ok": False, "error": "body too large"})
                return
            req = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, OSError):
            self._json(400, {"ok": False, "error": "bad json"})
            return
        if self.path.startswith("/__term"):
            self._json(200, term_run(req.get("argv")))
            return
        if self.path.startswith("/__goal/plan"):
            got, err = _goal_validate(req)
            if got is None:
                self._json(400, {"ok": False, "rc": -1, "verdict": "BLOCK", "error": err})
                return
            if not _act_lock.acquire(blocking=False):
                self._json(409, _act_busy())
                return
            try:
                _act_state["running"] = "goal-plan"
                _act_state["started"] = time.time()
                run_id = stream_start("goal-plan")
                rc = -1
                try:
                    code, obj = compile_goal(*got)
                    rc = obj.get("rc", -1) if isinstance(obj, dict) else -1
                except Exception as e:                    # noqa: BLE001
                    code = 200
                    obj = {"ok": False, "rc": 1, "verdict": "BLOCK",
                           "error": "编译异常 %s" % str(e)[:120], "tail": []}
                    rc = 1
                finally:
                    stream_end(rc)      # 订阅者绝不能因为异常被吊在半空
                obj["run"] = run_id
                obj["aborted"] = _act_cur["aborted"]
                _act_state["last"] = {"action": "goal-plan", "rc": rc,
                                      "dur": obj.get("dur"), "tail": obj.get("tail"),
                                      "run": run_id, "aborted": _act_cur["aborted"]}
                self._json(code, obj)
            finally:
                _act_state["running"] = None
                _act_state["started"] = None
                _act_lock.release()
            return
        if self.path.startswith("/__goal/chain"):
            self._goal_chain(req)
            return
        name = req.get("action")
        if self.path.startswith("/__act/abort"):
            # 中止面：仍走 POST + X-AgentOS-Action:1（它会杀进程，属于执行面写操作）。
            # 只认 run id —— 不认「当前正在跑的那个」。
            code, obj = abort_act(req.get("run"))
            self._json(code, obj)
            return
        if not isinstance(name, str):      # list/dict 会让 `in ACTIONS` 抛 TypeError 掐连接
            self._json(404, {"ok": False, "error": "unknown action"})
            return
        if name == "__list":
            # tmo 一起给出去：客户端要靠它定自己的等待预算。只给名字和描述的话，
            # 客户端只能拿一个固定 --timeout 去撞，而 livecheck 的真实时长
            # （2026-10-10 实测 >11min）远大于任何合理的固定值。
            self._json(200, {"ok": True, "actions": [
                {"name": k, "desc": v[0], "tmo": v[2]} for k, v in ACTIONS.items()]})
            return
        if name not in ACTIONS:
            self._json(404, {"ok": False, "error": "unknown action"})
            return
        if not _act_lock.acquire(blocking=False):
            self._json(409, _act_busy())
            return
        try:
            _act_state["running"] = name
            _act_state["started"] = time.time()
            run_id = stream_start(name)
            rc = -1
            try:
                rc, tail, dur = run_act(name)
            finally:
                stream_end(rc)          # 订阅者绝不能因为异常被吊在半空
            _act_state["last"] = {"action": name, "rc": rc, "dur": round(dur, 2),
                                  "tail": tail, "run": run_id,
                                  "aborted": _act_cur["aborted"]}
            if rc == 0 and name in ("rebuild", "verify"):
                push("reload")
            self._json(200, {"ok": rc == 0, "action": name, "rc": rc,
                             "dur": round(dur, 2), "tail": tail,
                             "run": run_id, "aborted": _act_cur["aborted"]})
        finally:
            _act_state["running"] = None
            _act_state["started"] = None
            _act_lock.release()


def watcher(stop, poll=1.0):
    global TREE
    last = [fp(p) for p in WATCH_FILES]
    last_tree = [None]
    while not stop.is_set():
        time.sleep(poll)
        now = [fp(p) for p in WATCH_FILES]
        if now != last:
            last[:] = now
            STATS["rebuilds"] += 1
            print("[hot] 构建产物变化 -> reload", flush=True)
            push("reload")
        if last_tree[0] is None:
            try:
                last_tree[0] = tree_sig()
                STATS["last_tree_sig"] = last_tree[0]
            except OSError:
                last_tree[0] = "unreadable"
            continue
        try:
            sig = tree_sig()
        except OSError:
            continue
        if sig != last_tree[0]:
            last_tree[0] = sig
            STATS["last_tree_sig"] = sig
            STATS["tree_rebuilds"] += 1
            STATS["last_build"] = time.strftime("%H:%M:%S")
            print("[tree] 真树变动 %s -> extract+build" % sig, flush=True)
            if run("extract", [os.path.join("tools", "extract.py")]) == 0:
                if run("build", [os.path.join("tools", "build.py")]) == 0:
                    last[:] = [fp(p) for p in WATCH_FILES]
                    STATS["last_build"] = time.strftime("%H:%M:%S")
                    push("reload")
                else:
                    push("build-failed")
            else:
                push("extract-failed")


def main():
    args = sys.argv[1:]
    # 8787 已被 headroom.cli proxy 常驻占用（实测），本服务默认让开
    port = 8790
    poll = 1.0
    if "--port" in args:
        port = int(args[args.index("--port") + 1])
    # 常驻模式必须拉长轮询：tree_sig 要 walk 真树 14 万文件（实测 0.52s/次），
    # 每秒轮询 = 常年 ~52% CPU。默认 15s → ~3.5%。
    if "--poll" in args:
        poll = float(args[args.index("--poll") + 1])
    global TREE
    if "--tree" in args:          # 可指向假树，用于不碰真树的自测
        TREE = args[args.index("--tree") + 1]
    # 端口占用自愈：8787 被占就顺延，避免双击启动直接死掉（实测 8787 已被旧进程占）
    srv = None
    for cand in range(port, port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", cand), Handler)
            port = cand
            break
        except OSError as e:
            if e.errno not in (48, 98):     # 48=EADDRINUSE 98=EADDRINUSE(mac)
                raise
            print("[port] %d 被占用，顺延下一个" % cand, flush=True)
    if srv is None:
        print("FAIL: %d~%d 全被占用" % (port, port + 19))
        return 2
    srv.daemon_threads = True
    url = "http://127.0.0.1:%d/index.html" % port
    print("AGENTOS_CONSOLE_UP %s  (pid=%d)" % (url, os.getpid()), flush=True)
    if "--open" in args:
        threading.Thread(target=lambda: __import__("webbrowser").open(url),
                         daemon=True).start()
    stop = threading.Event()
    threading.Thread(target=watcher, args=(stop, poll), daemon=True).start()
    # 启动即预热晋升快照：首访 /__promote 不必等 9.5s 冷路径。
    threading.Thread(target=_promote_warm, daemon=True).start()
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        print("AGENTOS_CONSOLE_DOWN", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
