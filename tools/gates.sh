#!/bin/bash
# 解析一个带 playwright 的解释器。Homebrew 升级会带走旧 minor 的 user-site，
# 2026-10-01 就因此丢过一次 playwright（3.13 -> 3.14），门脚本直接 ImportError。
# 顺序：项目 venv -> capvenv -> 系统 python3。
# 门的执行顺序也有讲究：neg_a11y 会还原 index.html（mtime 变新），
# 所以它必须排在 shoot 之前，否则 ux_audit 会以「shots.json 早于 index.html」判 FAIL。
# replay_gate 收尾（2026-10-19 第二十四波，73→74）：兜的是「自进化**真的**改了行为吗」
# 这层真空洞。此前自进化只能证明「规则合入了、门是绿的」——**证明不了行为变好**。
# replay_gate 把真实跑过的会话语料（work/replay_corpus.jsonl，每条带 note 标注
# 源自哪次真实会话）用桩原样重放，引擎开/关做 A/B，用**交付闸自己的判据**量
# 「空口完成」（说完成了但全程零证据 = 2026-10-19 那次 23 步 900 秒事故的形状）。
# 硬指标：引擎开时零空口完成；引擎关时**必须**出现空口完成（否则语料分辨不出
# 两侧，基准就是空门）；外加确定性判据与「两侧都关→分辨力归零」变异体。
# 纯离线、亚秒级、不抢端口不碰浏览器，放链尾不扰动任何一门的顺序理由。
# 正确链（2026-10-20 起只剩**终端面**：网页驾驶舱与原生 macOS app 已下线，
# 那些门连同它们量的产物一起删了）：bash tools/gates.sh agent_core_gate neg_agent_core converge_gate converge_selftest task_contract_selftest verify_cmd_selftest gate_pollution_selftest todo_selftest replay_gate dsp_gate dsp_gate_selftest evo_merge_gate evo_merge_selftest subagent_gate subagent_selftest mcp_gate mcp_selftest hooks_gate hooks_selftest repl_ux_gate repl_ux_selftest branches_gate branches_selftest supervise_gate supervise_selftest safety_gate safety_selftest autonomy_gate autonomy_selftest boss_gate selfscan_gate cli_gate cli_gate_selftest gh_gate flake_retry_selftest rewind_gate rewind_selftest leader_gate leader_selftest verify_gate ctx_compact_selftest token_gate
# selfscan_gate 收尾（2026-10-19 第四波，71→72）：兜的是「源码里有没有埋雷」这层
# 真空洞 —— 安全此前只有 safety_gate 的 9 条**行为**判据（运行时有没有放行），
# 没有一条量过源码本身的静态缺陷（硬编码凭据、危险的 urlopen/反序列化/exec）。
# 它部署真工具 PyCQA/bandit ★8326 到 .vendor/（.gitignore 掉，不进运行时，主链路
# 仍 100% 纯 stdlib），只判**新增** MEDIUM/HIGH：存量 44 条是逐条审过的误报
# （B310 urlopen 前置已有 scheme 白名单、B105 是门令牌字面量），进基线
# work/selfscan_baseline.json；基线外新增才红，HIGH 出现即红。三条反向判据
# （喂 HIGH / 喂超基线 MEDIUM / 喂 loc=0 空扫）证明它判得红，以及未部署时必须
# 返回 3 而不是 0（「没扫」不许当「没缺陷」）。纯离线、亚秒级，放链尾不扰动
# 任何一门的顺序理由。
# boss_gate 收尾（2026-10-15 第三波，70→71）：人不审、机器审 —— 坏工具/环计划
# 必拒且说清理由，好计划才 approve；含 run_cmd 无全权拒、有全权放；monitor
# 报告形状齐。纯离线（无模型调用），放链尾 autonomy_selftest 之后，
# 不抢端口不碰浏览器，不扰动任何一门的顺序理由。
# perf_gate 收尾（2026-10-11 补链，44→45）：它此前只手工跑、不在链上，而 README
# 又引它的 OK 令牌 —— 属「文档引了门、链里却没有门」的自洽缺口。补链后 R6 性能面
# （8790 的 API/页面响应、内存/进程/fd、serve.py 冷启动，外加 `agentos health`
# 端到端「用户敲回车到看见结果」的感知启动）随每次全链复验。它全程用 free_port()
# 现拿端口、不抢 8790/8787、不碰浏览器，放最末也不扰动任何一门的顺序理由。
# gh_gate + tui_walk_gate（2026-10-10 补链，39→41）：
# gh_gate 验的是「原生链接 GitHub」这条用户可见面 —— gh CLI 真在 PATH、
# 令牌真有效、`agentos gh repos` 真吐出仓库数，四件 BASE 事实 + 3 个变异体
# （which 指假路径 / rate_limit 被改 / repos 空表）逐条证伪自己有牙。
# 它自带零污染双自查（生产 JSON 字节不变、tools/ 无 _ghmut_* 残留）。
# tui_walk_gate 验的是终端交付面在**真视口逐屏**都活 —— 13 个视图逐屏走查，
# 判据 12 条，BASE 绿 + 2 个变异体（空屏 / 数据假）被咬住；补的是 cli_gate
# 只判「tui 能退 rc=0」而看不见「屏里是不是真有数据」的真空洞。
# 两者都不抢端口/不抢浏览器（gh_gate 纯 gh CLI+网络，tui_walk_gate 走终端），
# 所以放在 cli_gate 之前、minimize_check 之后，不扰动原有顺序理由。
# cli_gate + cli_gate_selftest 排最末（2026-10-10 补链，终端化交付面）：
# 前 35 门全是「web/桌面」面 —— 页面、截图、无障碍、UX、原生壳、装机三件套，
# 没有一道门证明「agentos 这条命令本身在终端里真能用」。而本轮的交付目标是
# **对标 Claude Code 的终端版 AgentOS**：一条命令装进 PATH、TUI 打 13 视图、
# -p headless 给 CI 退出码、会话能 resume。这些能力在补链前**零门覆盖** ——
# 把 tools/agentos.py 的 tui 改成 raise SystemExit(0) 空转，35 门照样全绿。
# cli_gate 因此是自起点：4 组判据（A 源码契约 / B 隔离装机往返 / C CLI 行为面
# / D 零写入自证），共 59 条；装机只落 work/cli_gate_dest 隔离 dest 与隔离 HOME，
# **绝不碰用户 ~/.local/bin 与 ~/.zfunc**。
# cli_gate_selftest 紧跟其后（D 式反向门）：4 个变异体桩（恒 rc=0 的假 CLI /
# TUI 挂住不退 / 写向量放行 / 自相矛盾的 headless JSON）喂给同一个门，证明它
# 判得红也判得绿，而不是「它会返回 0」。它自己会重跑一遍 BASE（零参数生产调用），
# 耗时约 5 分钟，放最末不挡任何一门。
# 位置理由：cli_gate 会起一份 serve 实例（空闲端口）、开一次 `agentos open`
# （真浏览器标签），这两样都该等 web 面 35 门全部收工后再做；反过来它只读
# index.html/data.json 的字节数，不依赖前面任何一门的产物，所以没有前向依赖。
# at_selftest / neg_at_selftest 排最末（D85）：a11y_human 的 OS 层在本机必然是
# AT-UNVERIFIED（macOS TCC 辅助功能权限未授予），而反向门只能证明「不可读时
# 不撒谎」。AT-VERIFIED 这条**正向**路径当时从没有任何一道门证明过能走通 ——
# 量具永久瞎着却没人报警，正是 doctrine 要堵的「量具没量到东西却照样判绿」的
# 镜像形态。at_selftest 用桩注入证明正例能绿，neg_at_selftest 把四条判据各放松
# 一次证明它不是空门。两门纯 stdlib、亚秒级、不碰浏览器不占端口，故放末尾。
# resident_gate 排最前：它只读常驻 8790（不碰 Chrome/不抢端口，成本最低），
# 且必须在 act_gate 之前 —— act_gate 自起 50360 实例，若常驻实例落后于代码，
# act_gate 仍会全绿，正是 D30 要堵的「门绿但用户看到 404」缺口。
# headroom_gate 紧跟其后（D43）：只读 8787 的 lsof/readyz/manifest + 两条 hook
# 注册 + 两个客户端配置的 base_url，纯 stdlib、不碰 Chrome、不占端口。
# 它补的是 resident_gate 只判「8787 有监听」的那一层：参数漂移、误杀后无恢复
# 路径、客户端其实没走 8787 的静默降级 —— 这三种在 resident_gate 下全绿。
# flake_retry_selftest 紧跟其后：它验的是 act_gate 自己那套重试语义
# （判据红不许被重试洗绿 / 非空壳）。它零浏览器零端口、纯 stdlib、亚秒级，
# 但必须排在 act_gate 之前 —— act_gate 的浏览器门正依赖这套重试，
# 语义被掏空的话 act_gate 的绿是假绿。
# freshness_gate 排 neg_a11y 之后：它只读 index.html + data.json，不写盘不占端口，
# 自带 4 个变异体（永远新鲜/永远陈旧/fail-open/横幅死）反向证明自己有牙。
# 它断言「时效逻辑在盘上」，所以必须排在任何会重建 index.html 的门之后；
# 而 neg_a11y 自 v122 起只改隔离副本（活的 index.html 零写入），两者不冲突。
# view_dod_selftest 紧跟 view_dod（D47）：它是「关于门的那道门」——
# 证明 5 视图 DoD 的 6 道判据每道都能变红，而不只是「它会返回 0」。
# 排在 view_dod 之后即可，它自己会重跑一遍 view_dod 做 BASE 正例；
# 不进 serve.py 的「跑视觉/无障碍/UX 三门」按钮（那是用户点的三门，
# 塞一个 90 秒的门-about-门进去只会让按钮越等越久）。
# snap_gate 紧跟其后（D65）：它补的是 view_dod **判据漏掉**的那一层 ——
# 闭环三面（决策/晋升/蒸馏）在 file:// 直开时曾一起塌成「Failed to fetch」，
# 而 view_dod 只验「屏里有真内容」，那三处的降级文案本身就是非空文本 → 全绿。
# 即「最核心的自我进化闭环在交付态是死的，机械门看不见」。
# 它自带 3 个反向变异体（抽掉快照 / 改老到过期 / 引擎判红）逐条证伪自己；
# 变异体全在 work/ 隔离副本上跑，活的 index.html + data.json 零写入（门 5 自证）。
# a11y_served 紧跟其后（D60）：a11y_walk 只打 file://，而用户双击启动器后看到的是
# http://127.0.0.1:8790/。file:// 与 http:// 在 fetch/origin/CSP/service-worker 上
# 不是一回事，「file:// 全绿」可以和「用户实际页面键盘是坏的」同时为真。
# 它自起一份独立端口的 serve.py 把 a11y_walk --url 指过去，判据不复制第二套；
# 自带咬合牙（改字节的 served 源必须判红），排它前后都不影响别的门 ——
# 它对活的 index.html 零写入（门 4 自证 sha256 前后一致）。
# a11y_human + neg_a11y_human（D68，2026-10-03）：a11y_walk 是**机检**（DOM/ARIA
# 静态与半实测量），本门是**实机走查** —— 真浏览器真渲染量焦点/对比度/动效/回流，
# 并逐视图读 VoiceOver 实际消费的 CDP AX 树（地标齐备有可访问名、tab 有名、
# 无名交互控件 0），再拉有头浏览器置前台读 OS 层辅助功能树，
# 得出 AT-VERIFIED / AT-UNVERIFIED。neg_a11y_human 排在它**之后**：变异体跑在
# 隔离副本上（活 index.html 零写入），但它的证据位自检认的是
# 「work/a11y_human.json 指向的不是变异体」，所以真走查那份必须先落盘，
# 自检才有牙（否则只会打 [warn] 跳过）。
# projspace_gate 排在 a11y_served 之后、act_gate 之前（D96，第 10 视图要素
# 「左栏项目与任务空间」）：a11y_served 是 D60 立的规矩 —— 用户双击启动器后
# 看到的是 http://127.0.0.1:8790/，file:// 全绿可以和真实页面键盘是坏的
# 同时为真，所以凡「屏上真有的东西」都必须有 http:// 下那道。项目空间正是
# 这种东西：它的数据全部来自 /__projects 的真目录事实，而 a11y_walk 走
# file:// 时那个 fetch 必然失败，于是屏上只会出现一句诚实报错 —— 换句话说，
# a11y_walk 在这个面上量的是降级形态，正例一次都没量过。
# 它自带 6 个变异体（前端不带 proj / 未知 key 退化成 ROOT / 换掉 nav id /
# 把 n_top 写死 1 / 摘掉点击后的重拉 / root 写死假路径）逐条证伪自己；
# fixture 只造在 work/ 下三个小文件，活的 serve.py 与 index.html 零写入
# （门尾自证 sha256 前后一致 + 变异副本已清空）。它自起临时端口的 serve.py，
# 所以排在 a11y_served 之后 —— 两者都占端口，串起来比并行稳。
# act_gate 自己起一份 serve.py（--tree 指假树），跟 promo_gate 一起放最后跑，
# 避免和别的门抢 Chrome/端口。promo_gate 不起浏览器、只 fork company.sh，成本最低。
# capmap_gate 紧跟 promo_gate（D95，第 9 视图「能力地图」）：两者验的是同一批账本、
# 互补的两个方向 —— promo_gate 问「引擎准不准许晋升」，capmap_gate 问「屏上有没有
# 把没验过的账说成验过的」。后者是四桶分账的**诚实性**门：PROMOTED 26 条而独立
# A/B 裁决 PASS=0，若判定写成 status=='PROMOTED'，屏上就会出现「已验过 26」
# ——那个数字从屏幕上完全看不出破绽。它自带 5 个变异体（自报当验过 / 无名账并桶 /
# 失败返回 ok=true / 前端二套判定 / 漏接线）逐条证伪自己，账本全走 work/ 隔离夹具，
# 收尾核生产账本 sha256 未变；纯 stdlib、不碰浏览器、不占端口，亚秒级。
# abort_gate / abort_e2e 排在最后（D76）：两道中止能力的牙 ——
# 前者纯 stdlib 打 HTTP 证服务端语义（契约组 + 真进程组 + 三变异体，约 8s），
# 后者真 Chromium 点按证**用户那一侧**（停止按钮出现/可点、点完真停、
# hint 与 transcript 如实写「被用户中止」而不是记成 rc=0 成功，约 30s），
# 并且自带 route 变异体：删掉停止按钮的点击监听后必须观测到「点了不生效」。
# 两者都不写活的 index.html（探针动作注入内存 ACTIONS，变异体拦 served 字节）。
# goal_gate 排在最后（D77，第 7 视图「目标台」）：它验的是闭环第一环 ——
# 一句自然语言经 POST /__goal/plan **真 fork 公司引擎 set-goal** 后，引擎落盘的
# AC 逐条画进屏上，且与 taskdir/work/GOAL.json 对得上。它必须排最后，因为它自己
# 也起一份 serve.py + 真 Chromium（约 8s），抢端口抢浏览器都该由前面的门先做完。
# 它不可替代前面任何一道：目标台在未编译时是**故意空屏**的（诚实口径不预填假计划），
# 而 view_dod 判「屏里有真内容」—— 所以「目标台根本编译不出计划」这类最严重的缺陷
# 在屏级判据下天然全绿，只有这道门能红。它自带两个 route 变异体
# （抽掉 gplanBox(j) / 把 verdict 写死 true）逐条证伪自己，活的 index.html 零写入。
# round_gate 接在 goal_gate 之后（D78，第 8 视图「轮次台」）：它验的是闭环第二环 ——
# 引擎 status/next-round/round-goal 三条命令，屏上 #gobj 逐字等于引擎 create_goal_arg，
# 以及 goal_table 与盘上 work/GOAL.json 逐行一致（含占位符）。它自带三个诚实负例
# （编译 BLOCK / 目录越界 / 没跑过 set-goal）与三个 route 变异体（抽掉 cdraw、
# 把 #gobj 换成自拼文本、把 if(!j.ok) 改 false 画出假台账），自己起 serve.py +
# 真 Chromium 排最后，活的 index.html 零写入（变异体只拦 served 字节）。
# close_gate 接在 round_gate 之后（D79，第 9 视图「收轮台」）：它验的是闭环第三环 ——
# 收轮**前置条件**在收轮发生之前就被算清并诚实摆上屏：delta 实指某条 AC、证据含
# rc=0 且非口头、verdict 显式（不默认 CONTINUE）、SHIP 的 delta 表真的收完、
# AC 越界与未指名都判红。这道门存在的理由是 round_gate 的盲区：round_gate 判的是
# 「屏上文字 == 引擎 create_goal_arg」，可一个**永远就绪**的收轮台同样逐字等于引擎
# —— 台账照样对得上，只是每次都邀请用户跑注定 die(2) 的 close-round。
# 它的判据真源是引擎本体（import goal_loop.py 用它自己的 _load/_round_file/_trow/
# _bind_check/_giga_evidence/_ac_ids 与 DELTA_EMPTY/EVIDENCE_JUNK/EV_RESULT/EV_HEDGE/
# TBL_TODO/TBL_DONE_STATES），台面不自己复刻一套判据 —— 否则两边一起漂就一起绿。
# 取不到引擎就 fail-closed（engine 门红），绝不 fail-open 出「可以收了」。
# 它自带 8 个诚实负例（占位 delta / 口头证据 / 无 verdict / SHIP 未收 / AC 越界 /
# 未指名 AC / 活根已 CLOSED 谎称可收 / 没跑过 set-goal）+ 1 个 BLOCK 零 delta 正例
# （零 delta 在 verdict=BLOCK 时合法，不能一律判红）+ 3 个 route 变异体 +
# 3 道只读自证（活 index.html / 夹具 GOAL.json / 活根 GOAL.json 三个 sha 前后一致）。
# 每个变异体都先自证 wired（屏上字节含替换串），否则无效变异让门看起来有牙实际没牙。
# 注意：不带参数跑本脚本是**空跑 0 门 rc=0** 的假绿，必须把上面那串名字全传进去。
# intent_gate 接在 abort_e2e 之后、goal_gate 之前（D81，2026-10-04 补链）：
# 它验的是 composer 那句「或自然语言，Enter 提交」不是空头承诺 —— 一句中文经
# resolve_intent() 真落到正确动作、exact 优先、空/乱码→no-match、歧义必须报
# ambiguous 而不替用户猜、危险句绝不路由，且 ok=True 的 action 必在 ACTIONS
# allowlist 内（路由不许收窄也不许扩大授权面）。
# 补链理由是实测出的**覆盖真空洞**：查 7 份 gates.sh 历史备份，正确链从最早的
# 9 门到 22 门从来就没含过它，但登记册里一直有它，于是 D50 按登记册单跑过它、
# 看着像「覆盖了」，而项目自己的正确链一道都不跑它。更糟的是 grep 全链 22 个
# 门脚本，命中 resolve_intent/intent 的门数 = 0 —— NL 意图路由是**无门覆盖**的
# 用户可见能力：把 resolve_intent 改成 return None，22 门全绿而用户输
# 「帮我重建一下」静默无反应。D80 那道「登记册≠正确链判红」能抓清单漂移，
# 抓不到「清单整体漏了一道门」，这才是补链的理由。
# 位置理由同 goal_gate/round_gate/close_gate：它 import serve.py 并自起一份 8791
# 真实例 + 真 Chromium（约 7s），抢端口抢浏览器都该由前面的门先做完。门自带上牙
# （B 组把 action 换到 allowlist 外、把歧义强行取第一个，两条都必须被判红 + 一条
# 对照正例），失败模式 fail-closed：取不到引擎/浏览器就红。
# retro_gate 接在 intent_gate 之后、goal_gate 之前（D82，第 10 视图「复盘台」）：
# 它验的是闭环最后一环「沉淀经验→下一次自动变强」确实上了屏，不是只在 engine 里
# 有 jsonl。屏上三张卡（复发簇 / 自我诊断计数 / 复盘账本）逐字对引擎 ground truth，
# 真源是 evolution_intake.py 自己的 load_events/summarize，判据不复刻。
# 两段式无竞态：浏览器侧正例 + 4 个 route 变异体全跑在 work/d82frozen 的**冻结快照**
# 上（数据完全确定，不会因并发 run 追加 jsonl 而假红）；另起一份不改 COMPANY 的
# 实例 + 两次 truth 签名比对，证明 serve.py 真指着真目录而不是被副本盖住。
# 诚实口径锁在门里：human_interventions 真实数据全程 0，就不许画「人类干预下降」；
# top21 复发簇就是能力缺口还没修好，屏上如实这么讲。
# 失败模式 fail-closed：空账本时端点必须 503、屏上必须 fail-closed 单卡，
# 绝不出「已变强」。活的 index.html / serve.py 零写入（变异体只拦 served 字节）。
# 位置理由同 intent_gate/goal_gate：它自起 3 份临时 serve + 真 Chromium，
# 抢端口抢浏览器都该排在最后一批。
# layout_gate 接在 close_gate 之后（D83，新增第 11 视图无关的**跨视图布局硬门**）：
# 它是唯一在真视口逐屏抓「元素压字 / 文字被裁」两道缺陷的门。view_dod 判「屏里有
# 真内容」，但完全看不见 flex 内部溢出（文档 scrollWidth 不变）与无空格长 token
# 无换行点 —— 这两类在 24 门全绿时**真实存在**于 375px 顶栏与复盘台。
# 自带三段判据：D83 定性后 CTRL 0 项才放行（闭合 details 的 checkVisibility 幻盒
# 与 inline 并集盒子都不得再假红）；EXPANDED 轮把所有 details 展开再量一遍，
# 堵住「靠排除蒙混过关」；MA/MB 两个反向变异体必须各自被抓住，才证明门有牙。
# 变异体只拦 served 字节，活 index.html 零写入（理由同 retro_gate/neg_a11y 的 D39）。
# 位置理由同 close_gate：它自起临时 serve + 真 Chromium，抢端口抢浏览器排最后一批。
# env_gate 接在 layout_gate 之后、at_selftest 之前（D86，真实环境联动三面）：
# 它验的是「终端 / 文件 / 代码」这三个新面板**真的接到了后端真执行**，
# 而不是画出来的三张空壳 —— 后端真起临时 serve 打真 HTTP，前端真 Chromium 点按。
# 存在的理由是一处实测过的**假绿**：view_dod 判「屏里有真内容」，而这三面在
# 接上线之前屏上全是占位与常量，`zcode` 显示的是硬编码文本、`run` 的 rc 是写死的
# 0、`truncated` 恒为 False —— view_dod 全绿而用户点开就是一个假的编辑器。
# 它同时补住 layout_gate 看不见的一面：layout_gate 只在真视口量「压字/裁切」，
# 而「文件树列出来的目录是不是真的、越界读取有没有被挡」必须打真 HTTP 才验得到。
# 自带 6 个反向变异体（关沙箱 / 放宽白名单 / rc 写死 0 / 两个 truncated 写死 False /
# 摘掉常用命令监听）逐条证伪自己；白名单探针用 touch 不用 rm（M2 会真执行）。
# 位置理由同 layout_gate/retro_gate：自起临时 serve + 真 Chromium，抢端口抢浏览器
# 排最后一批；排在 at_selftest（纯 stdlib 亚秒级）之前，保持「末尾两道轻门」不变。
# web_gate 接在 env_gate 之后、at_selftest 之前（D88，真实环境联动第五、六面）：
# 它验的是「浏览器 / 预览」这两面**真的在取字节**，而不是画一个 iframe 框 ——
# 输入框、「取字节」按钮、iframe 三样在完全没接后端时也是齐的，而 env_gate 覆盖的是
# 前三面，前面 28 门里没有任何一道会去请求一个 URL。所以「这一面是死的」天然全绿。
# 它同时是唯一会碰安全边界（只接回环 / 不带凭据 / 只 http(s) / 不跟随重定向）
# 的门：边界写松一点，现有门一个都不会红 —— 没人请求外网 URL。
# 上牙七个变异体（host 判据改写放行 / 关重定向判据 / truncated 写死 / 关文本类型判据 /
# webface 路径指到不存在文件 / 摘掉 srcdoc 赋值 / 右栏降级分支写死假值）逐条证伪自己。
# W1 刻意写成「放行 + 把 host 改写成本机」而不是「去掉判据真去连 evil.example.com」：
# 一个为了证明自己抓得住变异体而发真实外网请求的门，本身就是缺陷。
# 位置理由同 env_gate：自起临时 serve + 上游夹具 + 真 Chromium，排最后一批。
# native_gate 接在 web_gate 之后、两道纯 stdlib 轻门之前（D99，mac 原生外壳）：
# 前面 32 道门（含 D68 真渲染无障碍、D47 视图 DoD、D66 的 HTTP 态键盘实测）
# **全部发生在浏览器里** —— 它们再严，也只证明「这个网页在 Chromium 里是对的」。
# 而用户最终双击的是一个 .app：双击能不能起来（bundle/签名/Info.plist）、
# 起来的是不是真原生窗口（交通灯/合并标题栏/NSToolbar）、ATS 放不放行回环明文
# HTTP、⌘1…⌘0 与 ⌘K 在**原生菜单**里有没有条目、服务死了长什么样 —— 这五类
# 判据在浏览器里根本不存在，且 ATS 一旦不放行，page_title 仍可能读上一屏缓存，
# 于是 DOM 判据照样全绿而用户看到的是一块「无法连接」页。
# 位置理由（必须串行、不能并行）：它**真起一个 .app**，占前台窗口并借死端口做
# 降级面，所以排在所有抢 Chrome/抢端口的门之后；它不碰浏览器、不改活的
# index.html/data.json（门尾自证 sha256 前后一致），排在哪都不污染别的门。
# native_gate_selftest 紧跟其后：它是「关于门的那道门」—— 证明 D99 的判据
# 每道都会红，而不只是「它会返回 0」。真判据与恒真判据在 rc=0 时长得一模一样，
# 这是所有门共有的盲区（view_dod 必须配 view_dod_selftest 同理）。
# 它拿 work/native/probe.json 当正例（base 全绿 = 判据非恒红），再注入 16 个
# 对应真实故障的变异体（窗口不可见/页面空白/导航失败/点菜单屏没变/⌘K 焦点没进
# 输入框/截图纯色/5 张被覆盖成同一张/菜单转发接错线/降级面不解释自愈/…）+
# 2 个 fixture bundle 的 ATS 变异体（撤掉回环放行 / 整体放开），逐条要求指定
# 判据翻红；不起 GUI、不占端口、不碰真 bundle（重绑模块全局后立刻还原），
# 纯 stdlib、亚秒级 —— 所以跟两道轻门一起放末尾。
# agent_core_gate 紧跟其后（D113）：它是「终端 agent 内核」这道交付面的
# 正向门 —— 真跑 `agent_core.py --check`（33 条用例，逐字令牌
# AGENT_CORE OK 33/33，少一条就红）+ py_compile + 独立复核硬阻断 5 项
# （git push / rm / find -delete / git reset / dd，full+审批者全点头也要拦）
# + 只读白名单无 rm + ROOT 指向本项目 + 自检临时目录清零。它不碰浏览器、
# 不抢端口（只绑回环临时端口给 Stub 端点）、不起 GUI，排在 native 家族之后
# 不占前台窗口；真端点冒烟默认 SKIP（门不许依赖外部模型在不在）。
# neg_agent_core 紧跟正向门之后：它是「关于门的那道门」—— 每条判据同时证
# 「生产行为判 OK」与「掏空样本判 NOT OK」（后者专抓恒真/恒红的假牙），
# 另拷 4 个源码级变异体（摘硬阻断 / plan 不拒写 / 沙箱不比边界 / 截断不
# 标注）到 work/ 隔离副本真跑 --check，要求红的正是目标用例而非崩在
# traceback 上；收尾 unlink 全部副本，断言 work/ 无残留。约 20s（4 次
# --check），放中段会推迟前面红项的暴露，所以压在 native 家族与两个轻门
# 之间。两门的顺序固定：先正向（证明能跑）后反向（证明判据有牙）。
# minimize_check 排在**整条链最末**（两道轻门之后）：它是 D105 mini=True
# 红项的根因门，会用 AX 把真窗口按进 Dock，再看两条恢复路径（正常启动的
# deminiaturize / 单实例接管的 AX 解除）能不能拉回来。位置理由跟
# native_gate 同源 —— 真起 .app、占前台窗口，所以必须排在所有抢窗口、
# 抢端口的门之后；排更末是因为它一次要起 2~3 个实例、约 80s，是全链最慢的
# 门，放中段会让「前面红了」这件事要等很久才看得到。它自己写独立的
# probe_minchk_*.json，不碰 probe.json，不与 native_gate 抢证据。
# evolution_gate + capability_gate + evolution_gate_selftest（2026-10-10 补链，41→44）：
# 前 41 门里没有任何一道证明「自我进化闭环在**活服务**上真跑通」——
# 进化面（复发簇追踪 / 能力自动晋升 / 能力自动回退 / 蒸馏双证据）在补链前
# 零门覆盖：把 /__capmap 的 rows 全删、/__recurrence 恒返空簇，41 门照样全绿。
# evolution_gate 打 5 个只读端点（/__recurrence /__capmap /__promote
# /__distill /__health），10 条判据证复发簇非空且有复发性簇、能力账每条有状态、
# 有 A/B 验证记录、有自动晋升触发、有退化检测；capability_gate 同源但偏向
# 账本一致性（36 条 / 状态分布 / ab_was-ab_now 成对 / HOLD-AB 判定 / 回退能力 /
# 引擎账本 == 能力地图 / 复发簇与能力关联），10 条判据。两门都只读常驻 8790
# （`AGENTOS_RESIDENT_PORT` 可覆盖）、纯 stdlib、不碰浏览器、不占端口。
# evolution_gate_selftest 是「关于门的那道门」（D 式反向门）：起一个夹具 HTTP
# 服务按五端点真 schema 返回自洽绿夹具，BASE 两门必绿，M01–M10 十个变异体
# 每个只改夹具一处、点名判据必须变 FAIL 且 rc≠0，M11 服务死两门同红，
# 收尾双自查（生产 work/evolution_gate.json 与 work/capability_gate.json
# 字节不变、隔离目录真被用）。产出目录靠 `AGENTOS_GATE_OUT_DIR` 指隔离，
# 绝不把夹具结果写进生产账。三门排最末：它们只读服务、不抢端口，
# 但必须等 cli_gate 起停 serve 实例的动作收工之后再跑，免得读到半启动的服务。
# dsp_gate + dsp_gate_selftest（2026-10-12 补链，45→47；同日二轮加固 16→18 判据 /
# 4→5 变异体：新增 D13a/D13b「KNOWN_TOOLS ⊆ exec_tool 真可解析名集」交叉断言，
# 堵工具名静默漂移——auto_plan 曾产出永久不可解析的工具名）：DSP 决策监督计划
# 模块的结构硬门。正向 dsp_gate 18 判据（dsp.py 可导入 / --check 逐字 73/73 /
# 四 REPL 命令锚点 / 计划锁锚点 / 自启双锚点 / 分析器行为 / 锁白名单链行为 /
# doom 归一化 / 证据行 / 零污染 / 工具名交叉断言双条），纯离线不碰 8790；
# dsp_gate_selftest 是「关于 DSP 门的那道门」（D 式反向门）：5 个源码级变异体
# （摘锁/摘白名单/改令牌/改 doom 期望/翻转工具名交叉断言）逐条证伪门有牙 +
# 生产 work/dsp_gate.json 字节不变。
# kbd_gate_selftest 压轴（2026-10-12 补链，47→48）：kbd_gate（D51f 原生真键盘门）
# 的「关于门的那道门」。为什么上链的是 selftest 而不是 kbd_gate 本体 —— 三条实测：
#   1) 门本体产出绿需要「pyobjc 装好 + 系统全键盘访问 FKA=on」双前置；三个系统级
#      解释器（homebrew python3 / /usr/bin/python3 / /usr/local/bin/python3）全无
#      pyobjc（项目 .venv-pw 例外——带 pyobjc，手工真跑复验用
#      `./.venv-pw/bin/python tools/kbd_gate.py`），FKA 键缺失=off（作者已用
#      纯原生 osascript display dialog 对照实验证过是机器级开关）。缺任一条，门本体在
#      链上就是常驻红（env, rc=2）；而它的文件内 doctrine 又明文「绝不并进 35 门链把
#      环境红算成产品红，也绝不 exit 0 假装量到了」。所以本体按设计语义手工跑：
#      `python3 tools/kbd_gate.py`（env → rc=2 + KBD_GATE_ENV_UNVERIFIED；补链后缺
#      pyobjc 也不再崩栈）。2026-10-06 真跑记录在案：18 拍全可读、treat 臂 3 个
#      对照臂没有的停靠点（RadioButton/Group/DisclosureTriangle）、verdict=env ——
#      量到了，但按 FKA=off 口径不报绿。
#   2) 判据本体（summarize/judge/decide/fka_state）是纯 stdlib 且与量具解耦，
#      selftest 拿那次真跑 work/native/kbd_gate.json（18 拍）当 BASE；读不到/不合格
#      才降级合成正例并打印降级原因。9 个变异体（真键没生效/窗没起来/置前失败/
#      量具不开口/全无名/非法 role/焦点外流/反向臂空/纯补读假象）+ 5 条裁决分支
#      （FKA=on→ok / on+Tab不动→red / off→env / unknown→env / off+ALWAYS红→red）
#      逐条证伪判据有牙，并证明正向路径真能产出绿（FKA=on 分支 = 正向桩证）。
#   3) W0-3 加装护栏回归：pyobjc 不可用时真门本体必须 exit 2 + ENV 令牌且不崩栈
#      （子进程跑 kbd_gate.py 本体；链上 $PY=.venv-pw 带 pyobjc 时该检查按设计打印
#      跳过说明，不计入判据数）—— 缺量具 ≠ 产品红，也 ≠ 假绿；FKA=on 的机器上
#      本门自动升级为全强度真跑。
# 纯 stdlib、无端口、无浏览器、不起 GUI（护栏检查只在缺 pyobjc 时跑，不起 .app），
# 亚秒级，放最末不扰动任何顺序理由。
# rewind_gate + rewind_selftest 收尾（2026-10-12 W1 波补链，48→50）：
# W1「全面建设和提升」波的上下文双件门 —— compact v2（丢弃前 provider 事实摘要
# digest 塞回头部，失败逐字回退旧口径）与 /rewind（会话 jsonl 重放回第 N 轮末、
# 越界 fail-closed、note 落盘、文件层不动）。正向 rewind_gate 10 判据：可 import
# + --check 逐字 140/140 + 源码锚点六元 + 重放对齐/越界行为 + digest 真调用（回环桩）
# + 回退逐字相等 + 聚焦透传 + run_agent 自动压缩集成真跑 22 步 + /rewind 端到端
# + 零污染。rewind_selftest 是「关于门的那道门」：4 个源码级变异体（跳 digest /
# 回退被绕过 / 越界不拒 / 重放错位）逐条点名判红 + 生产 JSON 字节不变。
# 两门纯 stdlib、回环桩不起真模型、不过 8790、约 1 分钟，放最末不扰动顺序理由。
# subagent_gate + subagent_selftest 压轴（2026-10-12 W2 波补链，50→52）：
# W2「全面建设和提升」波的结构性最大件 —— subagent-as-tool（子代理）。
# 正向 subagent_gate 15 判据（W6 加深：并行批次 barrier 确定性证伪 / 批量边界
# 5 拒 4 / 失败隔离按输入序；W8 加深：chain 顺序交接 / 中途段失败即停 /
# merge 汇总 digest）：可 import + 符号齐（TOOLS 含 subagent /
# SUBAGENT_MAX_STEPS=12）+ --check 逐字 140/140 + 源码锚点四元（工具本体 /
# 嵌套闸 / 步数钳 / 子集闸 各恰 1）+ 可重入真跑（外层停→内层终答→外层继续，
# 序贯桩 3 请求）+ 禁嵌套零请求 + 步数封顶（99→12，请求数=钳值）+ 子集强制
# （拒 + 不落盘）+ 权限继承（外层 plan 内层写拒、文件未落）+ 零污染。
# subagent_selftest 同款：4 个源码级变异体（步数钳被摘 / 嵌套闸被摘 /
# 子集闸被摘 / 摘要伪造）逐条点名判红 + 生产 JSON 字节不变。
# 两门纯 stdlib、回环桩不起真模型、不过 8790、约 1 分钟，放最末不扰动顺序理由。
# mcp_gate + mcp_selftest 收尾（2026-10-13 W3 波补链，52→54）：MCP（Model
# Context Protocol）stdio 客户端门 —— tools/mcp_client.py 行分隔 JSON-RPC 2.0、
# initialize 握手 / 2026-07-28 无状态降级（降级如实记 handshake="stateless"）、
# tools/list 注入 mcp_<server>_<tool>、tools/call、密钥不进第三方服务器 env、
# fail-closed（id 错配 / 死服务器 / 超时都不许装成成功）。正向 mcp_gate 10 判据：
# 纯面 / --check 逐字 140/140 / 源码锚点 + 夹具真握手 / 降级 / id 错配 / 死服务器
# 不 hang / 注册与风险映射 / 密钥不外泄 / 零污染。mcp_selftest 是「关于门的那道
# 门」：3 个源码级变异体（id 错配不报 / 降级被绕 / 死服务器不报）逐条点名判红 +
# 生产 JSON 字节不变。夹具=微型 stdio 服务器 tools/mcp_fixture_srv.py
# （good/refuse-init/bad-id/die 四模式，自起自杀，不动 8790/8787）。
# 两门纯 stdlib、不过 8790、约 1 分钟，放最末不扰动顺序理由。
# hooks_gate + hooks_selftest 压轴（2026-10-13 W4 波补链，54→56）：
# W4 可扩展性双件门 —— hooks 中间件（~/.agentos/hooks.json，契约形状与
# ~/.codex/hooks.json 同款、headroom_gate 已验过可被外部工具读写：
# {"hooks":{"PreToolUse":[{"matcher":"glob","hooks":[{"command":...}]}]}}；
# exit 2 拦截、崩溃/超时/其他非 0 退出码 fail-closed 拦截、additionalContext
# 注入、PostToolUse 只记不拦、SessionStart 注入进 system prompt、无配置全关）
# 与 skills 目录（~/.agentos/skills 与本仓 .agentos/skills 的 SKILL.md 极简
# frontmatter，description 单条 500/总量 4000 预算截断留标记，/skill <名>
# 注入正文进下一轮上下文；W6 加深：triggers 显式词命中自动注入正文——只认
# 显式词不猜、预算 4000 截断留标记）。正向 hooks_gate 12 判据；hooks_selftest 是
# 「关于门的那道门」：3 个源码级变异体（钩子崩溃放行 / description 超预算不截 /
# 正文注入错位）逐条点名判红 + 生产 JSON 字节不变。夹具钩子=本地 sh，
# 自起自杀，不动 8790/8787；两门纯 stdlib、约 1 分钟，放最末不扰动顺序理由。
# repl_ux_gate + repl_ux_selftest 压轴（2026-10-13 W5 波补链，56→58）：
# W5 终端 UX 对齐 Claude Code 的门 —— 行尾反斜杠多行续行（「… 」次级提示符）、
# `@` 路径 Tab 补全（目录带斜杠/隐藏跳过/限量）、编辑类工具结果 ⎿ 详情行
# （edit_file 行级 ±对账、write_file 行/字节数；失败/被拒一律空）、终答续行
# 缩进两格。正向 repl_ux_gate 9 判据：纯面 / --check 逐字 140/140 / 源码锚点
# （续行循环/@路由/摘要集成/缩进两站点/⎿）× 行为四条 + 事件集成（run_agent
# 真跑 edit_file）+ 零污染。repl_ux_selftest：4 个源码级变异体（续行被摘 /
# @补全被摘 / 摘要集成被摘 / 缩进被摘）逐条点名判红 + 生产 JSON 字节不变。
# 两门纯 stdlib、回环桩不起真模型、不过 8790，放最末不扰动顺序理由。
# branches_gate + branches_selftest 压轴（2026-10-13 W7 波补链，58→60）：
# W7 创新波的门 —— 会话分支 /branch（显式 fork、原文件不动、新文件带注记、
# 回去/回来都靠会话 id，永不断链）与计划自验收 /verify（逐任务五态判定：
# done/pending/evidence-missing/evidence-mismatch，真完成才叫完成——状态机
# 不许提前 done）。正向 branches_gate 9 判据：纯面 / --check 逐字 140/140 /
# 源码锚点 + fork 对账 / verify 五态 / 端到端 pending / 跑了也不提前 done /
# 缺记录 fail-closed / 零污染。branches_selftest：4 个源码级变异体（fork 不注记 /
# done 不查证据 / pending 也算完成 / 分支源拷反了）逐条点名判红 + 生产 JSON
# 字节不变。两门纯 stdlib、不过 8790，放最末不扰动顺序理由。
# supervise_gate + supervise_selftest 收尾（2026-10-13 W8 波补链，60→62）：
# W8 老板监督框架的门 —— **steer 监督指令通道**（work/steer/<sid>.jsonl
# append-only 收件箱 + <sid>.cursor 游标；跑着的 agent 在**每个步骤边界**
# 消费新增指令、注入为 [老板指令] user 消息 + note，只进一次；只配送不裁决，
# 不做绕过写锁的后门；CLI `agentos steer <sid|--last> "指令"` + REPL /steer）
# 与 **watch 监督面**（计划任务/未消费指令数/会话尾一屏；`agentos watch`
# TTY 循环刷新、管道打一份；REPL /watch）。正向 supervise_gate 9 判据：
# steer 纯面（往返/游标只进/坏 sid 防穿越）/ --check 逐字 140/140 / 源码锚点
# 七元（边界消费//steer//watch/补全表/CLI 两命令/bash 字面）/ 注入行为 /
# 幂等 / 监督面渲染 / 会话隔离 / CLI 双冒烟（watch --once rc=0；坏 sid
# steer 拒且零写入）/ 零污染。supervise_selftest：4 个变异体（游标不推进/
# 注入被摘/监督面丢任务行//steer 不接线）双通道（--core 门跑 + 纯面复核）
# 逐条证伪 + 生产 JSON 字节不变。两门纯 stdlib、不过 8790，放最末。
# safety_gate + safety_selftest 压轴（2026-10-13 W9 波补链，62→64；2026-10-14
# W10 改写成自由面契约）：自由面门 —— **默认不摆闸**（以用户要求为中心）：
# plan_lock / mode_gate / cmd_gate 三键**只有字面布尔 True 才上锁**（字符串
# "true"/"1"/"yes" 一律不认，缺键=自由）；cmd_gate 控制命令面（自由面任意
# 命令 / 显式 true 收窄回只读 13 条白名单）；**硬阻断清单（git push/rm/find
# -delete/git reset/dd）不在开关范围**（含路径形态 /bin/rm、shell -c 载荷、
# env 前缀，防的是模型手滑，不是老板）；子代理继承三键；REPL/headless 启动
# 时如实标注「安全闸=自由面 / 上锁：…」。正向 safety_gate 9 判据：flags 纯
# 转换 / --check 逐字 140/140 / 源码锚点三键 / 默认自由（入口旗标放行、裸
# ctx 保守仍锁）/ 模式开关行为（含硬阻断三形态不可关）/ 计划锁行为 / 命令面
# 开关（自由面真跑 mkdir、显式 cmd_gate 拒）/ CLI 隔离 HOME 四态（set true/
# false 落布尔 rc=0；yes 与越权键 rc=2）/ 零污染。
# safety_selftest：5 个源码级变异体（计划锁被摘/模式开关被摘/硬阻断被摘/
# flags 恒自由/命令白名单被摘）逐条点名判红 + 生产 JSON 字节不变。两门纯
# stdlib、不过 8790。
# leader_gate + leader_selftest（2026-10-14 W11 补链，64→66）：体检报告
# P0-1 的落点 —— 领导层内化进仓（tools/leader.py：目标→里程碑→任务DAG→
# 派发→回收→复盘，与 dsp 计划锁 / steer 监督入口 / watch 监督面三方串联），
# 此前指挥官只活在仓外 company.sh、仓内**零门覆盖**。leader_gate 拿桩
# worker 离线证伪全链路（编译面/落盘上链/**未审批拒跑锁面**/拓扑派发与
# 证据回收/失败阻塞下游的 DAG 面/steer 边界消费/复盘落盘/CLI 转发/坏计划
# 负控/零污染）；leader_selftest 6 变异体逐条点名判红，其中两条正是验收
# 口径「无 Leader 必红」（模块面 compile_goal 被摘 + CLI 面注册被摘）。
# 两门纯 stdlib、桩 worker 零模型零网络、不起端口不碰浏览器，与 dsp 纪律
# 层相邻，不扰动任何一门的顺序理由。
# evo_merge_gate + evo_merge_selftest（2026-10-14 W12 补链，66→68）：体检
# 报告 P0-2 的落点 —— 受控进化合入：机器可执行提案（find 锚点恰一次 /
# replace 或新文件 write）→ 路径闸（../、绝对路径、work//native//.venv-pw/
# AgentOS.app 禁改区）→ **复制式沙箱**（本仓非 git；tools/ 全量 + 与真仓
# 同构的 work/plans 基线——沙箱基线形状不同构时门自己的零污染判据会误红，
# 首跑实测踩过）→ 沙箱全绿才合入真树（先字节快照 → 应用 → **真树复验**）；
# 任一环红 → 不碰真树或自动回退；账本 work/evo_merges.jsonl 逐行落
# merged/rejected/rolled_back。正向 evo_merge_gate 7 判据（纯面闸/拒绝面/
# 合入面含「好提案合入后 capability_gate 仍 10/10」/回退原语进程内直调/
# 账本/CLI/还原与零污染）；evo_merge_selftest 8 例（4 变异体：拒绝分支/
# 锚点唯一性/路径闸/回退 各被摘必红 + BASE + 生产 JSON 字节不变 + 清理）。
# 两门纯 stdlib、以 leader_gate+capability_gate 作判卷器、不起端口不碰
# 浏览器，紧随 leader 双门，不扰动任何一门的顺序理由。
# autonomy_gate + autonomy_selftest（2026-10-15 W13 补链，68→70）：用户
# 直接指示「全流程自己完成事情」的落点 —— 自主执行引擎 tools/autonomy.py
# 把 leader「派发之后」的空白补满：一次审批后全流程无人干预收口（判据
# verify 命令组真跑、双闸 done 语义反假绿、失败确定性分类带修法自救 ≤2 次、
# 封顶 blocked 且下游按 DAG 不派发、断点检查点强杀可续、终局 REPORT 诚实
# 收口 rc=0/1）。正向 autonomy_gate 13 判据（纯面/环负控/**未审批拒跑锁面**/
# 一次调用 5/5 全流程含 verify marker 物证/自救先红后绿/**封顶+DAG 传播**/
# SIGKILL 强杀续跑零重跑/预算边界干净停/反假绿双闸/steer 监督/CLI/零污染）；
# autonomy_selftest 10 例（6 变异体：绕锁/无自救/封顶差一/续跑重跑/verify
# 被跳过/环守卫被摘 各必红 + BASE + 生产 JSON 字节不变 + 清理）。两门纯
# stdlib、桩 worker+桩判据命令零模型零网络、不起端口，紧随 evo_merge 双门。
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# D102：零参数不许判绿。
# 实测踩实（2026-10-05）：`bash tools/gates.sh` 不带门名时 for 循环体一次
# 都不执行，n_total=0、rc=0，于是打出
#   "== GATES_SUMMARY 共跑 0 门 / RC=0 计数 0 / 红门=无 ==" 并 exit 0。
# 这正是本项目反复在堵的那一类假绿 —— **量具没量到东西却照样判绿**：
# 一个从没跑过任何一道门的全链汇总，输出形状和「34 门全绿」一模一样。
# 引擎 D50 因为另有一条「RC=0 计数 == 清单条数」的兜底才没被骗到，但人
# 直接敲 `bash tools/gates.sh`（最自然的用法）就被骗了。
# 所以：无参数 = 用法错误 = 非 0 退出，并把这套门公认的规范链打出来。
BASH_SRC="${BASH_SOURCE[0]}"
CHAIN_MARK="正确链"
CANONICAL_CHAIN="$(awk -v m="$CHAIN_MARK" 'index($0, m) && /gates\.sh/ {sub(/.*gates\.sh[ \t]+/, ""); print; exit}' "$BASH_SRC")"
if [ -z "$CANONICAL_CHAIN" ]; then
  echo "GATES_FAIL cannot-derive-canonical-chain" >&2
  exit 4
fi
if [ "$#" -eq 0 ]; then
  echo "GATES_FAIL no-gate-named（零参数 = 一道门都没跑，不能判绿）" >&2
  echo "  用法: bash tools/gates.sh <门名> [门名...]" >&2
  echo "  规范全链（$(echo $CANONICAL_CHAIN | wc -w | tr -d ' ') 门，顺序有讲究，见本文件头部「正确链」注释）:" >&2
  echo "  bash tools/gates.sh $CANONICAL_CHAIN" >&2
  exit 2
fi

# 2026-10-21（TASK2 障碍 1）：本仓只剩终端面，链上 41 道门**没有一道需要浏览器**。
# 原来这里强制「必须找到带 playwright 的解释器」，找不到就整条链 exit 3
# （no-interpreter-with-playwright）—— 结果一条干净 clone（没有 .venv-pw）连自己的
# 门都跑不了：为一套用不上的浏览器栈把整条链开不了工。
# 现在的口径（两层）：
#   1) 优先选带 playwright 的解释器（真需要浏览器的门/手工复验仍拿到它）；
#   2) 没有就退回任意可用 python3，并打一行如实的兜底说明（不是硬失败）。
# 真需要 playwright 的门自己报缺依赖（点名是哪个模块），不再让整条链陪葬。
pick_python() {
  local c
  for c in "$ROOT/.venv-pw/bin/python" "$HOME/.codex/capvenv/bin/python" \
           /opt/homebrew/bin/python3 python3 /usr/bin/python3; do
    [ -x "$c" ] || command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c "import playwright" >/dev/null 2>&1; then
      echo "$c"; return 0
    fi
  done
  return 1
}

pick_any_python() {
  local c
  for c in "$ROOT/.venv-pw/bin/python" /opt/homebrew/bin/python3 python3 \
           /usr/local/bin/python3 /usr/bin/python3; do
    [ -x "$c" ] || command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c "import sys" >/dev/null 2>&1; then
      echo "$c"; return 0
    fi
  done
  return 1
}

PY="$(pick_python)" || {
  PY="$(pick_any_python)" || {
    echo "GATES_FAIL no-python-interpreter" >&2
    echo "  fix: 装一个 python3（本链所有门都只要 stdlib）" >&2
    exit 3
  }
  echo "GATES-NOTE 没找到带 playwright 的解释器，退回 $PY（纯 stdlib 门照跑；" >&2
  echo "  若某道门真的需要浏览器，它会自己报缺依赖并点名模块）" >&2
  echo "  opt-in: $ROOT/.venv-pw/bin/python -m pip install playwright" >&2
}

# D50（2026-10-03）：**不再首红即 break**，全链跑完再汇总。
# 原来 `[ "$one" -ne 0 ] && rc=$one && break` 的问题有二，且都会误导排查：
#   1) 引擎 D50 门按「RC=0 计数 == 清单条数」裁决，短路让计数偏低 → 掩盖真实全貌；
#   2) 排在后面的门根本没跑，其中若有红项，下一次只能整套重跑才看得见（且分不清是谁红）。
# 现在：rc 取**首个非零**（保持「有一条红就整体红」的语义），但所有门都跑完，
# 并额外打一行「共跑 N 门 / RC=0 计数」供引擎与人都能一眼对账。
# D97：http_shot_gate 为什么排在 view_dod_selftest 之后、snap_gate 之前
# （顺序不是随便挑的，三条都是实测出来的）：
#   1) 它必须在 view_dod_selftest **之后**：http_shot_gate 自己起服务器、读
#      docs/UX-SHOTS 并量截图产物。排在 selftest 前会与 6 个变异体的并发
#      浏览器互相抢 CPU，实测过 6 路并发下 wait_for_function 15s 超时 → 假红。
#   2) 它必须在 shoot / view_dod **之后**：它量的就是这两个门拍出来的东西。
#      反过来排就是「先验产物、后跑生产者」，出现红项时无法区分是生产者坏了
#      还是产物坏了 —— 正是 D50 修 mtime 假红时清掉的同一类混淆。
#   3) 它必须在 snap_gate **之前**：snap_gate 抓的是最终视觉快照。若 http_shot_gate
#      判红说明真 HTTP 页面本身不健康，此时快照再好看也是残废页的快照，
#      先让更上游的门说话。
rc=0
n_total=0
n_ok=0
red=""
slow=""
# D102（2026-10-10）：门链互斥锁。为什么必须有它：两条门链同时打同一个项目时，
# 「零写入」判据会把**另一条链**的落盘当成「本门自己写了活文件」——
# 2026-10-10 14:42 实测踩实：外部管线在同一窗口改 tools/serve.py + tools/agentos.py，
# 于是 env_gate 与 web_gate 双双判红（各自 38/44 项判据里只红这一条），
# 而把同一条链单独跑一遍 35/35 全绿。污染不报成污染，反而嫁祸给门自己 =
# 下一次排查从错误的前提出发，且两份链的结果都不再可作上线凭证。
# 语义（fail-closed，不放松任何判据）：
#   · 等最多 GATES_WAIT 秒；等不到就判红退出，并点名持有者 pid/起始时间/链。
#   · 不许「大概没人在跑」就放行 —— 那正是可被静默污染的洞。
#   · 锁里记 pid：持有者进程死了（kill -9 / 崩溃）就算陈旧并抢回，不死等。
# 为什么不_fsync/不FIFO：mkdir 原子建锁已够互斥；公平性靠等待重试，最坏是多等一轮。
GATES_LOCK="$ROOT/work/gates.lockd"
GATES_WAIT="${GATES_WAIT:-1500}"
g_lock_held=""
g_t0=$SECONDS
while :; do
  if mkdir "$GATES_LOCK" 2>/dev/null; then
    printf 'pid=%s\nstart=%s\nchain=%s\n' "$$" "$(date -u +%FT%TZ)" "$*" \
      > "$GATES_LOCK/holder" 2>/dev/null
    g_lock_held=1
    break
  fi
  g_hp=$(sed -n 's/^pid=//p' "$GATES_LOCK/holder" 2>/dev/null | head -1)
  if [ -n "$g_hp" ] && ! kill -0 "$g_hp" 2>/dev/null; then
    echo "GATES-LOCK 持有者 pid=$g_hp 已死，抢回陈旧锁"
    find "$GATES_LOCK" -depth -delete 2>/dev/null
    continue
  fi
  if [ $((SECONDS - g_t0)) -ge "$GATES_WAIT" ]; then
    echo "== GATES_LOCK_TIMEOUT 等 ${GATES_WAIT}s 未拿到门链互斥锁；持有者=$(tr '\n' ' ' < "$GATES_LOCK/holder" 2>/dev/null)拒绝与并发链混跑，判红 =="
    exit 2
  fi
  sleep 5
done
if [ -n "$g_lock_held" ]; then
  trap 'find "$GATES_LOCK" -depth -delete 2>/dev/null' EXIT
fi
# D101：每门一份自己的完整输出落盘 work/gates/<门名>.log（stdout+stderr）。
# 为什么加这道：引擎 D50 只把 gates.sh 的 stdout 截成 200 字摘要塞进
# detail，红门**自己的输出整段丢掉**，而它的「红判据」是拿「全 stdout 里
# 第一个含 FAIL/RED 的行」凑的 —— 于是别家门的变异体自证输出会被挂到红门
# 名下（实测踩实：native_gate 红，却报出 a11y_walk 的 tablist 判据）。
# 没有这份落盘，红一次就只能整套重跑再猜。
GLOG_DIR="$ROOT/work/gates"
mkdir -p "$GLOG_DIR"
t_all=$SECONDS
for g in "$@"; do
  one=0
  n_total=$((n_total + 1))
  echo "== $g (py=$PY)"
  t0=$SECONDS
  "$PY" "$ROOT/tools/$g.py" 2>"$GLOG_DIR/$g.err" | tee "$GLOG_DIR/$g.log"
  one=${PIPESTATUS[0]}
  if [ "$one" -ne 0 ] && [ -s "$GLOG_DIR/$g.log" ]; then
    echo "  REDGATE  以下是 $g 自己的红行（完整日志 work/gates/$g.log）"
    grep -aE "FAIL|RED |NATIVE_GATE_SELFTEST_RED" "$GLOG_DIR/$g.log" \
      | head -8 | sed 's/^/  REDGATE  /'
  fi
  echo "RC=$one"
  # D71：每门挂自己的墙钟。**行首不许出现 "== " 或 "RC="** ——
  # 引擎 D50 靠这两形状认门名和裁决（goal_loop.py 只收 `RC=0` 与 `== 名 (py=`），
  # 这里多打一个 "== 12s" 就会污染它的红门归属。
  d=$((SECONDS - t0))
  echo "TIME: $g ${d}s"
  slow="${slow}${g}:${d} "
  if [ "$one" -eq 0 ]; then
    n_ok=$((n_ok + 1))
  else
    [ "$rc" -eq 0 ] && rc=$one          # 取首个非零，不覆盖
    red="${red}${red:+,}${g}(RC=${one})"
  fi
done
echo "TIME: TOTAL $((SECONDS - t_all))s"
# 旧摘要行整行保留在前（外部 grep 靠它），新信息只往后追加 —— 不改老形状。
echo "== GATES_SUMMARY 共跑 ${n_total} 门 / RC=0 计数 ${n_ok} / 红门=${red:-无} == | 墙钟 ${slow}"
exit $rc
