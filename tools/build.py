#!/usr/bin/env python3
"""index.template.html + data.json -> index.html（数据内联，零外部依赖）。

2026-10-08 口径收口（任务 B）：主界面真源从 agent_desktop.html（桌面）
**改回** index.template.html（驾驶舱）。理由：GOAL v136 冻结的口径是
「驾驶舱 5 视图 + 第 6 Agent 台」——总览/轮次/矩阵/账本/引擎/Agent台，
而桌面版的 9 个英文视图（dashboard/chat/…）与 GOAL 对不上；且桌面版
的闭环三面在 file:// 直开时 fetch 全灭、只剩英文空标题（snap_gate
D65-2/D65-3 实测红），驾驶舱则有 snap_live 快照兜底，交付态三面是真
行。统一口径二选一里 GOAL 不可动，故落代码改回业务视图。
管线骨架不变：JS 语法焊死门 -> __DATA__ 内联 -> __LIVECHECK_N__ 构建期
注入 -> stamp 注入 html_sha -> build.json 双指纹。
焊死标记换成驾驶舱的视图切换总入口 view() 与首屏面板 id="v-over"。
"""
import json, os, sys, hashlib, datetime, subprocess, tempfile


def atomic_write(path, text):
    """同目录临时文件 + os.replace 落盘。
    为什么要原子写：直接 open(path,'w') 会先把文件**截断到 0** 再逐块写，
    于是并发的读者（常驻 8790 实例、真浏览器门、extract.py 的下一轮读）
    可能正好读到半截 index.html —— 页面壳还在、内容全空，看起来像「门随机红」。
    2026-10-02 实测修的就是这个：build 与真浏览器门并发时随机红。"""
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.tmp-', suffix='.swap')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
TPL = os.path.join(ROOT, "index.template.html")
DATA = os.path.join(ROOT, "data.json")
OUT = os.path.join(ROOT, "index.html")

if not os.path.isfile(TPL):
    print("FAIL: 主界面真源 agent_desktop.html 不存在")
    sys.exit(2)
src = open(TPL, encoding="utf-8").read()

# 焊死门：JS 语法错一律 rc=2，不产出 index.html
# 教训：本轮真实踩过 readout() 括号错位 -> 页面壳渲染但内容全空，截图才发现。
import re as _re
def _pick_js(html):
    """取最大的非 JSON <script> 块。
    为什么不用 findall(r'<script>(.*?)</script>')[-1]：本模板第一个块是
    <script id="payload" type="application/json">，正则若带属性匹配就会把
    8 字节的 __DATA__ 当成 JS，node --check 恒过 = 假绿（D79 实测踩到）。
    为什么不用最后一个 plain 块：将来再加一段 <script> 就会静默挑错块。"""
    best = ""
    for m in _re.finditer(r"<script([^>]*)>(.*?)</script>", html, _re.S):
        if "application/json" in m.group(1):
            continue
        if len(m.group(2)) > len(best):
            best = m.group(2)
    return best
_js = _pick_js(src)
# 驾驶舱的焊死标记：视图切换总入口 view() + 首屏面板 id="v-over"。
# 命中不到 = 提取错块或源没跟上（同名函数全局唯一，grep 验证过）。
if "function view(" not in _js or 'id="v-over"' not in src:
    print("FAIL: 真 JS 块里找不到 view() 或缺 v-over 面板 —— 提取错块或代码落到 </html> 之后")
    sys.exit(2)
_tmp = os.path.join(ROOT, "work", "_syntax_check.js")
os.makedirs(os.path.dirname(_tmp), exist_ok=True)
with open(_tmp, "w", encoding="utf-8") as _f:
    _f.write(_js)
_chk = subprocess.run(["node", "--check", _tmp], capture_output=True, text=True)
if _chk.returncode != 0:
    print("FAIL: JS 语法错，未产出 index.html")
    print(_chk.stderr[:1200])
    sys.exit(2)
print("SYNTAX_OK node --check rc=0")
if "__DATA__" not in src:
    print("FAIL: 模板缺 __DATA__ 占位"); sys.exit(2)
raw = open(DATA, encoding="utf-8").read()
# 防注入：内联进 <script type="application/json">，必须转义 </ 与 <\/
safe = raw.replace("</", "<\\/").replace("<!--", "<\\!--")
html = src.replace("__DATA__", safe)
if "__LIVECHECK_N__" not in src:
    print("FAIL: 模板缺 __LIVECHECK_N__ 占位"); sys.exit(2)
# livecheck 项数从**引擎源码**推导（serve.py._livecheck_n，与 doc_drift / act_gate 同口径）。
# 教训（v118 实测）：这个数曾硬编码在模板里，引擎每加一项门就红一次 —— 红的判得没错，
# 但挡的是「每次升版都手改两个文件」。现在它是构建期注入，模板不再写死。
import importlib.util as _ilu
_sp = _ilu.spec_from_file_location("_serve_probe", os.path.join(HERE, "serve.py"))
_sv = _ilu.module_from_spec(_sp); _sp.loader.exec_module(_sv)
html = html.replace("__LIVECHECK_N__", str(_sv.LIVECHECK_N))
digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
# 把构建指纹写进页面，让截图时间戳可对账（UX DoD：截图为旧版本冒充本轮 -> FAIL）
build = {"built_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
         "data_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
         "html_sha256": digest[:16]}
html = html.replace('<span class="stamp mono" id="stamp"></span>',
                    '<span class="stamp mono" id="stamp" title="build %s / html %s"></span>'
                    % (build["data_sha256"], build["html_sha256"]), 1)
# D50（2026-10-03）：补 file_sha256 = **落盘后**文件的真实指纹。
# 为什么必须补：上面那个 digest 是在注入 stamp **之前**算的，而 stamp 注入本身改了内容，
# 于是 work/build.json 的 html_sha256 与 index.html 的真实 sha256 **永远不相等**。
# 后果：ux_audit/view_dod 的「内容指纹对账」恒红 —— 一个 100% 假红的判据比没有判据更糟。
# file_sha256 是可直接与盘上文件对账的那一个；html_sha256 保持原语义（页面内嵌指纹）。
build["file_sha256"] = hashlib.sha256(html.encode("utf-8")).hexdigest()[:16]
atomic_write(OUT, html)
atomic_write(os.path.join(ROOT, "work", "build.json"),
             json.dumps(build, ensure_ascii=False, indent=1))
print("BUILD_OK bytes=%d html_sha=%s file_sha=%s data_sha=%s"
      % (len(html.encode()), build["html_sha256"], build["file_sha256"],
         build["data_sha256"]))
