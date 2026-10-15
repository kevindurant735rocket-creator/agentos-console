import os,shutil,subprocess,hashlib,sys,tempfile,time,json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from variant_pool import map_cases
INSTR_RETRY_N=2   # 量具无结论时的重试次数（重试量具，不是重试判据）
INSTR_RETRY_S=2.0
SRC="index.html"
_t_start=time.time()   # D64：起跑时刻（自检已改内容判据，这行只留作排障时序参考）
base=open(SRC,encoding="utf-8").read(); BASE=hashlib.sha256(base.encode()).hexdigest()[:12]
# D64：证据位是否被变异体污染的**内容判据**（提到模块级，好单独上牙）。
# 抽出来的理由和判据取舍见文件末尾 finally 段的注释：mtime 版会被并发
# 门链的正常写入误红，内容版只认「target 指向 neg-a11y-* 变异体目录」。
def ev_is_variant(ev_path):
    """读证据位 report 的 target；指向 neg-a11y-* 变异体目录 -> True（该判红）。"""
    try:
        with open(ev_path, encoding="utf-8") as f:
            tgt = str(json.load(f).get("target", ""))
    except Exception as e:
        return "<unreadable:%s>" % e
    return "neg-a11y-" in tgt
# 隔离副本：v122 改造前这个门**就地改活的 index.html** 注入缺陷（备份->改->跑->移回）。
# 危害不在「测试不干净」，在**并发窗口**：常驻 8790 实例、真浏览器门、act_gate 的
# 真点按读的是同一份文件，于是它们会在注入期读到缺陷版，表现为「门随机红」。
# 2026-10-02 实测（D39 落盘原子性门 AST 反扫）才把这条漏网写盘点挖出来。
# 现在改为：副本上注入，a11y_walk.py --html 指过去；活的 index.html 全程零写入。
_TD=tempfile.mkdtemp(prefix="neg-a11y-"); ISO=os.path.join(_TD,"index.html")
shutil.copyfile(SRC,ISO)
CASES=[
 # 注入点必须逐个在 index.html 里实测存在；命中不了就 [warn] 跳过，
 # 不能留一个「一直在空跑」的历史 case（case3 曾长期命中不了，见 2026-10-01 修复）。
 ("\u7834\u574f roving tabindex\uff1a5 \u4e2a tab \u5168 tabindex=0",
  [("setAttribute('tabindex',k===key?'0':'-1')","setAttribute('tabindex','0')")]),
 ("\u62b9\u6389 \u2318K \u6309\u94ae\u53ef\u8bbf\u95ee\u540d\uff08\u5220 aria-label + \u6e05\u7a7a\u6587\u672c\uff09",
  [('<button class="btn" id="cmdk" aria-label="\u6253\u5f00\u547d\u4ee4\u9762\u677f\uff08\u5feb\u6377\u952e Command \u52a0 K\uff09">\u2318K</button>',
    '<button class="btn" id="cmdk"></button>')]),
 ("\u62c6\u6389\u65b9\u5411\u952e\u5bfc\u822a\uff1atablist \u53ea\u5269\u70b9\u51fb",
  [("if(e.key==='ArrowRight') to=ks[(cur+1)%ks.length];",
    "if(e.key==='__never__') to=ks[(cur+1)%ks.length];")]),
 ("\u53bb\u6389 cmdOpen \u91cc\u7684 focus\uff08\u6253\u4e0d\u4e86\u5b57 / \u9996\u9879\u65e0\u9ad8\u4eae / \u7126\u70b9\u4e0d\u5f52\u8fd8\uff09",
  [("$('#cmdq').focus();","/*neg_no_focus*/")]),
 ("\u53bb\u6389\u6a21\u6001 Tab \u7126\u70b9\u9677\u9631\uff08aria-modal \u65e0\u9632\u62a4\u5c31\u8dd1\u51fa\u80cc\u666f\uff09",
  [("else if(k==='Tab'){ e.preventDefault(); }","else if(false){ e.preventDefault(); }")]),
 ("\u53bb\u6389 aria-activedescendant \u540c\u6b65\uff08\u8bfb\u5c4f\u4e0d\u64ad\u62a5\u5f53\u524d\u9879\uff09",
  [("q.setAttribute('aria-activedescendant',bs[cf].id)","q.setAttribute('data-neg-ad',bs[cf].id)")]),
 # ── D50 读屏结构门的三条变异体（2026-10-03）─────────────────────────────
 # 为什么必须有：结构三门是纯判据 + AX 树读取，纯判据自己会红不代表
 # 「读到的确实是页面真结构」。下面三条分别把 AX 树里的地标名、表格名、标题层级打坏，
 # 若门仍绿，说明它读的不是结构、或者压根没读。
 ("抽掉 main 地标的可访问名（rotor 只念「主区域」）",
  [('<main class="mn" id="main" aria-label="主内容区，数字键 1 起顺序切换视图">','<main class="mn" id="main">')],
  "地标都有可访问名"),
 ("清空表格 caption（表格在 AX 树里失去可访问名）",
  [('<caption class="sr">每个阶段的实测口径、自报口径、退出码与失败条目分类</caption>','<caption class="sr"></caption>')],
  "表格有可访问名"),
 ("给视图塞一个 H6 造成标题层级跳级（H2 之后直接 H6）",
  [('id="v-over" role="tabpanel" aria-labelledby="t-over" tabindex="0">',
    'id="v-over" role="tabpanel" aria-labelledby="t-over" tabindex="0"><h6>跳级探针</h6>')],
  "标题层级不跳级"),
]
def _run_case(idx, case):
    """跑一个变异体，**返回行列表**（不 print —— 并发下 print 会互相插行）。
    D72：原来整个循环体在主线程边跑边 print，199s 全串行。改为每 case 一份
    独立副本 ISO（iso-<tag>.html，替代原来 9 个 case 共用一份 ISO）+ 并发。
    共用 ISO 是串行时代才成立的隐含前提：串行下「写副本→立刻起浏览器读它」
    不会串味；并发下必须一份一档，否则 9 个 case 会互相覆盖注入内容。
    """
    # 第三元是「必须变红的那道门」（D50 起可选）：rc≠0 只是「门红了」，
    # 不说明红的是**我们注入的那一处**。指定了 expect 却红在别的门上，
    # 同样是空门（判据错位），故逐条核对 RED 行里是否含它。
    name,subs=case[0],case[1]
    expect=case[2] if len(case)>2 else ""
    tag="c%d"%idx
    iso=os.path.join(_TD,"iso-%s.html"%tag)
    lines=[]
    r=None; reds=[]; summ=[]; instr_killed=0
    try:
        h=base; ok=True
        for a,b in subs:
            if a in h: h=h.replace(a,b,1)
            else:
                lines.append("  [FAIL] 注入点未命中（反向门有洞，不是被证伪）: "+a[:60])
                ok=False
        if not ok: return lines+["  !! 注入点未命中 = 反向门有洞", "VRC=1"]
        open(iso,"w",encoding="utf-8").write(h)
        # 量具自身被杀的容错（D40 实测）：2026-10-02 实测「模态 Tab 焦点陷阱」这一 case
        # 拿到 rc=1 且已打出 RED 行，但**没打出门汇总行** —— stderr 是 Chrome 的
        # `GPU process exited unexpectedly: exit_code=15`，即整个 a11y_walk 被 SIGTERM。
        # 量具中途死掉 = 这次没结论，不是「门没红」。原来的处置是直接 rc_all=2 判红，
        # 于是基础设施抖动会伪装成 a11y 结论红（跟 act_gate 那次同源）。
        # 这里只对「无汇总行」重试（重试的是量具，不是判据）：
        #   有汇总行 → 量具给结论了，rc/RED 行照单收下，一概不重试（判据红是真红）。
        #   无汇总行 → 量具没结论，最多试 INSTR_RETRY_N+1 次；全都没结论才判红，
        #              即 fail-closed 语义**不变**，只是不再被一次抖动骗红。
        for _try in range(INSTR_RETRY_N+1):
            # --out 指向本门自己的临时目录：变异体的 report 不许落到
            # work/a11y_walk.json（那是真走查的证据位，见 a11y_walk.py --out 注释）。
            r=subprocess.run([sys.executable,"tools/a11y_walk.py","--html",iso,
                              "--out",os.path.join(_TD,"neg-report-%s-%d.json"%(tag,_try))],
                             capture_output=True,text=True)
            reds=[l.strip()[:120] for l in r.stdout.splitlines() if "[FAIL]" in l]
            summ=[l.strip() for l in r.stdout.splitlines() if "门通过" in l]
            if summ: break
            instr_killed+=1
            if _try<INSTR_RETRY_N: time.sleep(INSTR_RETRY_S)
        lines.append("== %s -> rc=%d"%(name,r.returncode))
        if instr_killed>1:
            lines.append("    (量具被 SIGTERM 无结论，重试 %d 次才拿到结论)"%(instr_killed-1))
        for l in reds: lines.append("    RED: "+l)
        for l in summ: lines.append("    "+l)
        # 顺序要紧：**先**看有没有结论，再谈结论内容。
        # 没汇总行 = 量具没给结论，这时 rc 是什么都不能当证据
        # （含 rc=0：那不是「门没红」，那是没测到）。
        if not summ:
            lines.append("    !! 量具自身异常(无门汇总行)，不能算门变红: %s"
                         % r.stderr.strip().splitlines()[-1:])
            return lines+["VRC=2"]
        if r.returncode==0:
            lines.append("    !! 门未变红 = 空门"); return lines+["VRC=1"]
        if expect and not any(expect in l for l in reds):
            lines.append("    !! 门红了但红在别处（期望红项=%s）= 判据错位空门" % expect)
            return lines+["VRC=1"]
        if expect: lines.append("    [ok  ] 预期红项命中: %s" % expect)
        return lines+["VRC=0"]
    except Exception as e:
        return lines+["    !! 变异体执行异常（并发下的真故障要显形，不能静默算过）: %r"%(e,),"VRC=2"]

rc_all=0
try:
    for ln in map_cases(CASES, _run_case,
                        header=lambda i, _c: "c%d" % i):
        print(ln)
        if ln.startswith("VRC="):
            one=int(ln[4:])
            if one: rc_all=one
finally:
    # D64（2026-10-03）：本门对**读屏证据位**也必须零写入。
    # 为什么这条不能省：本门 9 个变异体都调 a11y_walk.py，而 a11y_walk.py
    # 原来把 report 硬写 work/a11y_walk.json。于是链跑完后（gates.sh 与
    # act_projects.json 的 desktop_gates 都把 neg_a11y 排在 a11y_walk **之后**），
    # 盘上留下的不是真走查那份，而是「故意抽掉 main 地标可访问名」那份红报告
    # ——2026-10-03 13:23 实测：target 指向 neg-a11y 临时目录、19 门带 FAIL。
    # 修法是给 a11y_walk.py 加 --out（见该文件同名注释），本门指到自己的临时目录。
    # 这条自检的作用是**给该修法上牙**：谁把 --out 去掉，本门当场判红，
    # 而不是等到某天有人读证据时才发现读到的是变异体。
    # 判据用**内容**不用 mtime：2026-10-03 13:35 实测 mtime 版会**误红**——
    # 判据是「证据位被本门改写」，但 mtime 只说明**有人**改写（本机允许多条
    # 门链并发跑同一批截图目录，view_dod_selftest 的注释已记这个坑），
    # 于是并发的另一条链的正版报告被当成本门污染，neg_a11y rc=2。
    # 内容判据是精确对症：只有当证据位的 target 指向 neg-a11y-* 临时目录，
    # 才可能是本门（或另一个变异体调用方）写进去的。免疫外部并发写者。
    _ev = os.path.join("work", "a11y_walk.json")
    if os.path.exists(_ev):
        _bad = ev_is_variant(_ev)
        if _bad is True:
            print("    !! 读屏证据位被变异体污染（work/a11y_walk.json 的 target 指向"
                  " 变异体临时目录）")
            rc_all = 2
        elif _bad is not False:
            print("    !! 读屏证据位读不出来（%s）= 无法排除污染，判红" % _bad)
            rc_all = 2
        else:
            print("    [ok  ] 读屏证据位零写入：证据位 target 非变异体")
    else:
        print("    [warn] work/a11y_walk.json 不存在（a11y_walk 还没跑过？）跳过证据位自检")
    shutil.rmtree(_TD,ignore_errors=True)
now=hashlib.sha256(open(SRC,'rb').read()).hexdigest()[:12]
print("活产物零写入核验 sha=%s 基线=%s %s"%(now,BASE,"一致" if now==BASE else "不一致!!"))
if now!=BASE: rc_all=rc_all or 1
sys.exit(rc_all)
