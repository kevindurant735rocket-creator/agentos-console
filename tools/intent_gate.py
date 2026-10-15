#!/usr/bin/env python3
# D75 意图路由上牙：composer 的 label/placeholder 写着「或自然语言，Enter 提交」，
# 这条门负责证明这句话不是空头承诺。
#
# A. 纯函数组：import serve 打真 resolve_intent()，断言 NL→正确动作、exact 优先、
#    空/乱码→no-match、歧义必须报 ambiguous、危险句绝不路由，并逐条断言
#    ok=True 的 action 一定在 ACTIONS 内（路由不能收窄也不能扩大 allowlist）。
# B. 上牙组：把判据故意改坏（action 出 allowlist、歧义强行取第一个）必须被判红。
# C. 浏览器组：起真端口真键盘，输「帮我重建一下」，断言不再被 allowlist 拒绝。
import json
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import serve  # serve.py 只在 __main__ 里起 server，import 安全

RESOLVE = serve.resolve_intent
ACTIONS = list(serve.ACTIONS)
RC = 0


def free_port():
    """问操作系统要一个当前空闲的回环端口（与 act_gate/abort_gate 同款）。

    为什么不能写死 8791（D104 实测）：那是**跨项目共享**的端口。实测
    2026-10-05 17:22 有另一个项目（new-chat/worldcraft 的
    outputs/bundle/app.py）正监听 8791，于是本门起不来 serve.py，判红成
    「serve 真端口 8791 起得来」—— 一条把「别人占了端口」说成「本项目
    serve.py 坏了」的假修复建议。
    """
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p

PROBE_SCRIPT = '''import json, sys
from playwright.sync_api import sync_playwright
base = sys.argv[1]
out = {}
NL = {"rebuild": "帮我重建一下",
      "verify": "请跑一次视觉门",
      "snapshot": "刷新快照",
      "doc-drift": "看看文档有没有漂移",
      "egate": "把企业门跑一遍",
      "livecheck": "上线自证",
      "desktop": "桌面化三件套"}
with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page(viewport={"width": 1440, "height": 900})
    pg.goto(base, wait_until="domcontentloaded")
    pg.wait_for_selector("#t-work", timeout=15000)
    pg.click("#t-work")
    pg.wait_for_selector("#wkq", state="visible", timeout=15000)
    pg.click("#wkq")
    pg.keyboard.type("帮我重建一下", delay=8)
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(3000)
    out["hint"] = pg.inner_text("#wkhint")
    try:
        out["tr"] = pg.inner_text("#wktrbox")
    except Exception:
        out["tr"] = ""
    ap = pg.query_selector("button[data-wkap=rebuild]")
    out["had_ap"] = bool(ap)
    if ap:
        ap.click()
        pg.wait_for_timeout(900)
        pg.click("#wkq")
        pg.keyboard.type("帮我重建一下", delay=8)
        pg.keyboard.press("Enter")
        pg.wait_for_timeout(9000)
        out["hint_run"] = pg.inner_text("#wkhint")
    # 通用回路：对审批卡上**真存在**的第一个动作，用它的人话句走授权→执行，
    # 证明 NL 路由贯通到执行面（不写死 rebuild，因为推荐位是动态的）。
    names = pg.eval_on_selector_all(
        "button[data-wkap]", "els=>els.map(e=>e.dataset.wkap)")
    out["cards"] = names
    out["nl_act"] = ""
    for n in names:
        if n in NL:
            out["nl_act"] = n
            break
    if out["nl_act"]:
        btn = pg.query_selector("button[data-wkap='%s']" % out["nl_act"])
        if btn:
            btn.click()
            pg.wait_for_timeout(900)
            pg.click("#wkq")
            pg.keyboard.type(NL[out["nl_act"]], delay=8)
            pg.keyboard.press("Enter")
            got = ""
            for _ in range(40):
                pg.wait_for_timeout(1000)
                got = pg.inner_text("#wkhint")
                if "rc=" in got:
                    break
            out["hint_nl"] = got
    pg.click("#wkq")
    pg.keyboard.type("复发晋升", delay=8)
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(1500)
    out["hint_amb"] = pg.inner_text("#wkhint")
    b.close()
print("@@JSON@@" + json.dumps(out, ensure_ascii=False))
'''


def check(name, cond, detail=""):
    global RC
    if not cond:
        RC = 1
    tail = ("  <- " + str(detail)) if detail else ""
    print("  [%s] %s%s" % ("ok  " if cond else "FAIL", name, tail))


print("== A. 纯函数组 resolve_intent ==")

POS = [("帮我重建一下", "rebuild"),
       ("请跑一次视觉门", "verify"),
       ("验证一下无障碍", "verify"),
       ("看看文档有没有漂移", "doc-drift"),
       ("上线自证", "livecheck"),
       ("把企业门跑一遍", "egate"),
       ("桌面化三件套", "desktop"),
       ("刷新快照", "snapshot")]
for q, want in POS:
    r = RESOLVE(q)
    ok = (r.get("ok") is True and r.get("action") == want
          and r.get("how") == "intent")
    check("NL「%s」→ %s" % (q, want), ok,
          json.dumps(r, ensure_ascii=False))

EX = [("rebuild", "rebuild"), ("/rebuild", "rebuild"),
      ("promote", "promote"), ("/verify", "verify")]
for q, want in EX:
    r = RESOLVE(q)
    ok = (r.get("ok") is True and r.get("action") == want
          and r.get("how") == "exact")
    check("exact「%s」→ %s" % (q, want), ok,
          json.dumps(r, ensure_ascii=False))

NEG = [("   ", "empty"), ("", "empty"),
       ("随便说点什么", "no-match"),
       ("把这个文件删掉", "no-match"),
       ("你好呀", "no-match")]
for q, want in NEG:
    r = RESOLVE(q)
    ok = (r.get("ok") is False and r.get("reason") == want)
    check("空/无关「%s」→ %s（ok=False）" % (q, want), ok,
          json.dumps(r, ensure_ascii=False))

r = RESOLVE("复发晋升")
ok = (r.get("ok") is False and r.get("reason") == "ambiguous"
      and "action" not in r and len(r.get("candidates") or []) >= 2)
check("歧义句「复发晋升」报 ambiguous 而非乱选", ok,
      json.dumps(r, ensure_ascii=False))

for q in ["run rm -rf /", "把这个文件删掉", "删掉所有门", "force push main"]:
    r = RESOLVE(q)
    check("危险句「%s」不路由（ok=False）" % q, r.get("ok") is False,
          json.dumps(r, ensure_ascii=False))

PROBE = [q for q, _ in POS] + [q for q, _ in EX] + [q for q, _ in NEG]
PROBE += ["复发晋升", "rm -rf /", "看看门有没有问题", "跑一下门",
          "把状态推到 promoted", "自证上线", "文档漂移"]
leaked = []
for q in PROBE:
    r = RESOLVE(q)
    if r.get("ok") is True and r.get("action") not in ACTIONS:
        leaked.append((q, r.get("action")))
check("不变量：%d 条探针的 ok=True action 全在 allowlist(%d) 内"
      % (len(PROBE), len(ACTIONS)), not leaked, leaked)

print("== B. 上牙组：判据必须能被判红 ==")

r = dict(RESOLVE("帮我重建一下"))
r["action"] = "rm-rf-everything"
check("上牙：伪造 allowlist 外动作被不变量抓住",
      r.get("ok") is True and r.get("action") not in ACTIONS, r)

amb = RESOLVE("复发晋升")
guess = {"ok": True, "action": amb["candidates"][0][0], "how": "guess"}
check("上牙：强行取第一个候选被歧义断言抓住",
      not (guess.get("ok") is False
           and guess.get("reason") == "ambiguous"), guess)

r = RESOLVE("帮我重建一下")
check("对照组：真 resolve_intent 判定为绿（否则上牙没意义）",
      r.get("ok") is True and r.get("action") in ACTIONS, r)

print("== C. 浏览器组：真端口 + 真键盘 ==")

PW = os.path.join(ROOT, ".venv-pw", "bin", "python")
if not os.path.exists(PW):
    print("  [skip] 没有 .venv-pw，跳过浏览器组")
else:
    PORT = free_port()
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "serve.py"),
         "--port", str(PORT)],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    up = False
    ours = False
    seen_root = None
    for _ in range(60):
        o = subprocess.run(
            ["curl", "-fsS",
             "http://127.0.0.1:%d/__health" % PORT],
            capture_output=True, timeout=2)
        if o.returncode == 0:
            up = True
            try:
                _h = json.loads((o.stdout or b"").decode("utf-8"))
                seen_root = _h.get("root")
                ours = (seen_root == ROOT)
            except Exception:  # noqa: BLE001
                ours = False
            break
        time.sleep(0.25)
    check("serve 真端口 %d 起得来" % PORT, up)
    # 临时端口仍有极小的 TOCTOU 窗口（挑完到 serve 抢占之间被别人抢走）。
    # 光判 HTTP 200 是不够的：那会把**别人的** /__health 当成我们的，
    # 接着去验别人的 index.html，再拿别人的路由结果报本门绿 —— 量具说谎。
    # serve.py 的 /__health 带 root 字段，用它验明正主。
    check("对面确实是本项目的 serve.py（/__health.root 匹配）",
          ours, "root=%r 期望=%r" % (seen_root, ROOT))
    if up and ours:
        sp = os.path.join(HERE, "_d75_probe.py")
        with open(sp, "w", encoding="utf-8") as f:
            f.write(PROBE_SCRIPT)
        try:
            url = "http://127.0.0.1:%d/index.html" % PORT
            r = subprocess.run([PW, sp, url], capture_output=True,
                               text=True, timeout=240)
            line = ""
            for ln in (r.stdout or "").splitlines():
                if ln.startswith("@@JSON@@"):
                    line = ln[len("@@JSON@@"):]
            check("浏览器探针跑通（rc=0 且有 JSON）",
                  r.returncode == 0 and bool(line),
                  (r.stderr or "")[-400:])
            if line:
                d = json.loads(line)
                h = d.get("hint", "")
                tr = d.get("tr", "")
                check("真键盘输「帮我重建一下」不再被 allowlist 拒绝",
                      "执行面拒绝" not in h and "不在服务端" not in h
                      and "不在任何动作词典" not in h, h)
                check("路由映射如实回显（transcript 出现 → rebuild）",
                      "帮我重建一下" in tr and "rebuild" in tr, tr[:200])
                check("路由≠授权：未授权时仍被审批卡挡住",
                      "未授权" in h or "授权" in h, h)
                check("审批卡上存在可授权动作（授权面没被路由绕掉）",
                      bool(d.get("cards")), d.get("cards"))
                hr = d.get("hint_nl", "")
                check("授权后同一句人话真跑通到 rc（动作 %s）"
                      % (d.get("nl_act") or "-"), "rc=" in hr,
                      hr or ("cards=%s" % d.get("cards")))
                ha = d.get("hint_amb", "")
                check("歧义句在浏览器里也摆候选、不替用户选",
                      "歧义" in ha and "evolution" in ha
                      and "promote" in ha, ha)
        finally:
            if os.path.exists(sp):
                os.unlink(sp)
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()

print("== D75 意图路由上牙 %s ==" % ("OK" if RC == 0 else "FAIL"))
sys.exit(RC)
