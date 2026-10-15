#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""原生门 D99 的自证：证明每道判据**有牙**。

为什么必须单独做这件事：native_gate 全绿只能说明「当前这盘证据通过
了 104 道判据」，不能说明这 104 道判据会红。一道永远为真的判据和一个
真判据在 rc=0 时长得一模一样 —— 这是所有门共有的最大盲区，所以 D47 的
view_dod 必须配一个 view_dod_selftest。

做法：对真跑出来的证据（work/native/probe.json）注入一个**对应真实故障
或极易发生故障的变异体**，再要求指定那条判据翻红：

  窗口不可见            -> window-on-screen
  没请求过焦点          -> window-requested-activation
  key 与前台互相说谎    -> window-key-matches-foreground
  自称前台却没拿到 key  -> window-became-key-when-foreground
  窗口没开合并标题栏      -> window-full-size-content
  页面空白              -> page-title-real / page-body-text
  导航没完成            -> webview-nav-finished
  点了菜单但屏没变        -> switch-mx-tab
  ⌘K 打开但焦点没进输入框  -> cmdk-focus-in-input
  截图变纯色            -> shot-*-distinct-colors
  5 张截图被覆盖成同一张   -> shot-hashes-distinct
  截图被截断成 64 字节    -> shot-*-bytes
  菜单转发接错线          -> menu-forward-targets-match
  菜单没有 ⌘K          -> menu-has-cmdk
  降级面按钮没接线        -> degraded-start-button-wired
  App 没真把服务拉起来    -> degraded-service-started
  降级面不解释「会自愈」   -> degraded-panel-explains-selfheal
  降级面板压根没出现      -> degraded-panel-not-missing
ATS 撤掉回环放行        -> bundle-ats-loopback
ATS 整体放开           -> bundle-ats-not-arbitrary
面板树压根没加载       -> panel-tree-loaded
面板树里没有真文件     -> panel-tree-has-real-files
面板目录没展开         -> panel-tree-expanded
终端命令失败           -> panel-term-rc-zero
终端静默空转(D100 分词) -> panel-term-computed-42
终端 argv 引号没剥掉    -> panel-term-argv-parsed
面板截图纯色(D100 像素) -> panel-shot-distinct-colors
面板截图满屏涂黑       -> panel-shot-ink-ratio
面板选中了全黑通道     -> panel-shot-method-not-blank
原生工具栏少了面板按钮 -> panel-toolbar-wired
原生菜单没有 ⌘T       -> panel-menu-has-cmdt
D103 五窗格：0段分段器假绿 -> panel-pane-segments-declared
D103 浏览器白页(didFinish) -> panel-browser-rendered-chars
D103 预览与浏览器互相覆盖   -> panel-preview-not-confused-with-browser
D103 绕过文件树直调 loadCode -> panel-code-via-tree-selection
D103 回环限制被摘           -> panel-browser-rejects-external
D105 门读错键(读外层而非内层)-> panel-window-composition
D105 左侧栏没进窗口构成      -> projects-window-composition
D105 项目数据压根没接上      -> projects-loaded
D105 项目根变成相对路径      -> projects-roots-absolute
D105 少一行原生视图         -> projects-row-views-match
D105 AX 树读不到项目行       -> projects-ax-rows-named
D105 没走 NSMenu 按键路径     -> keyeq-real-menu-path
D105 ⌘7 快捷键被摘          -> keyeq-all-views-consumed
D105 菜单消费了但页面没切     -> keyeq-all-views-reached
D105 ⌘P 没把焦点交给左栏     -> keyeq-cmdp-focuses-projects
D105 AX 读不到窗口          -> ax-window-exposed
D105 只剩 AXApplication 自链 -> ax-content-tree
D105 树被菜单项吃光          -> ax-content-tree

base 全绿本身就是「判据非恒红」的前半段证明；变异体翻红是后半段。
本文件不起 GUI、不占端口、不改活文件，纯 stdlib。

rc=0 全过 / 非 0 有无效变异体或 base 带红。
"""
import copy
import json
import os
import plistlib
import shutil
import stat
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import native_gate as NG                                    # noqa: E402
import single_instance_probe as SIP                         # noqa: E402

BASE_MAIN = os.path.join(NG.WORK, "probe.json")
BASE_DEG = os.path.join(NG.WORK, "probe_degraded.json")
BASE_SI = os.path.join(NG.WORK, "single_instance_facts.json")
FIX = os.path.join(ROOT, "work", "native_selftest")
REPORT = os.path.join(ROOT, "work", "native_gate_selftest.json")

FAILS = []
RESULTS = []


def note(ok, msg):
    RESULTS.append({"ok": bool(ok), "msg": str(msg)})
    if not ok:
        FAILS.append(str(msg))
    print(("  ok   " if ok else "  RED  ") + str(msg))
    return bool(ok)


def load(path):
    if not os.path.isfile(path):
        sys.stderr.write("missing-base:%s\n" % path)
        sys.exit(2)
    with open(path) as f:
        return json.load(f)


def verdict(rows, name):
    """从 judge 返回的 (name, ok, detail) 里取出指定判据。"""
    for n, ok, _d in rows:
        if n == name:
            return bool(ok)
    return None


def join(*parts):
    return " ".join(str(p) for p in parts)


def judge_all(judge_fn, base):
    rows = judge_fn(base)
    red = [n for n, ok, _d in rows if not ok]
    return rows, red


def mutate(label, base, patch, judge_fn, ck, why):
    """注入变异体 -> 要求指定判据翻红。
    不红 = 这道判据没牙（恒真），这是本文件唯一要抓的病。
    W11 补护栏：变异函数自己崩溃（典型：基线探针没采全、补丁引用的键
    缺席）曾经整门 Traceback —— 那是量具坏，不是被测物红。现在如实
    报 MUTFN-CRASH 并继续，别的变异体照跑。"""
    d = copy.deepcopy(base)
    try:
        patch(d)
    except Exception as e:                                     # noqa: BLE001
        note(False, join("MUTFN-CRASH", label, repr(e)[:80]))
        return
    rows, _red = judge_all(judge_fn, d)
    v = verdict(rows, ck)
    if v is None:
        note(False, join("NOCHECK", label, ck))
        return
    if v:
        note(False, join("NO-TEETH", label, ck))
        return
    note(True, join("RED-OK", label, ck, why))


def stays_green(label, base, patch, judge_fn, ck, why):
    """反向变异体 -> 要求指定判据保持绿。
    守住「新增的宽容分支」：状态其实到位了，判据不许假红。"""
    d = copy.deepcopy(base)
    try:
        patch(d)
    except Exception as e:                                     # noqa: BLE001
        note(False, join("MUTFN-CRASH", label, repr(e)[:80]))
        return
    rows, _red = judge_all(judge_fn, d)
    v = verdict(rows, ck)
    if v is None:
        note(False, join("NOCHECK", label, ck))
        return
    note(v, join("GREEN-OK" if v else "FALSE-RED", label, ck, why))


def mut_window(d):
    d["window_is_visible"] = False


def mut_minimized(d):
    d["window_is_miniaturized"] = True


def mut_window_offscreen(d):
    """窗口帧完全落在所有屏幕之外（D105 新增的那条腿）。

    不能靠 mut_window（visible=False）顺带覆盖：那是另一个字段，
    而「visible=True 但帧在屏外」正是 isOnActiveSpace 判据当初
    想抓、却因为量的是前台而抓不到的情形。
    """
    d["window_frame_on_screen"] = False

def mut_mini_not_restored(d):
    """起时窗口是最小化的，产品却没把它拉回来（D105 的真实复发形态）。

    注意末态 mini=True 会**顺带**让 window-on-screen 也变红。那不算
    变异体没打中：window-minimized-restored 必须**自己**也红，
    否则它就是条恒真判据 —— 只靠邻居判红也能让整套门看起来是绿的。
    """
    d["window_minimized_before_restore"] = 1
    d["window_deminaturized_on_launch"] = 0
    d["window_is_miniaturized"] = True

def mut_restore_keys_removed(d):
    """有人把 AppDelegate.showWindow 里的 deminiaturize 删了。

    模拟方式是**直接抹掉两个键**（回退后 Swift 就不再 put 它们）。
    这条比 mut_mini_not_restored 更要紧：它证明判据能抓住
    「修复被回退」，而不只是「修复失效时末态恰好还是好的」。
    """
    d.pop("window_minimized_before_restore", None)
    d.pop("window_deminaturized_on_launch", None)

def mut_no_activation(d):
    d["window_requested_activation"] = 0


def mut_key_lies(d):
    # 窗口自称 key、App 自称非前台 —— 两者说谎。
    # 必须**相对 base 恒说谎**：写成固定的 True 会在 base 本来就
    # is_key=True 时变成无操作，判据就测不出牙了（实测踩实：
    # 同一条链跑两遍，一遍 App 没抢到前台判红、一遍抢到了判 NO-TEETH）。
    d["window_is_key"] = not bool(d.get("nsapp_is_active"))


def mut_active_never_key(d):
    # 自称已是前台，却始终没拿到 key 窗口。这条专门证明
    # window-became-key-when-foreground 的条件分支不是恒真。
    d["nsapp_is_active"] = True
    d["window_did_became_key"] = 0


def mut_not_full_size(d):
    d["window_style_has_full_size_content"] = 0


def mut_blank_page(d):
    d["page_title"] = "about:blank"
    d["page_body_len"] = 0
    d["page_tab_count"] = 0


def mut_nav_failed(d):
    d["webview_did_finish"] = 0
    d["webview_nav_error"] = -1200


def mut_switch_noop(d):
    d["active_tab_after_mx"] = "t-over"
    d["visible_panel_after_mx"] = "v-over"


def mut_cmkd_not_focusable(d):
    d["cmdk_focus_after_open"] = "BODY"


def mut_shot_blank(d):
    for t in NG.SHOT_TAGS:
        d["shot_distinct_colors_%s" % t] = 1
        d["shot_ink_ratio_%s" % t] = 0.0


def mut_shot_same_file(d):
    one = d["shot_path_over"]
    for t in NG.SHOT_TAGS:
        d["shot_path_%s" % t] = one


def mut_shot_truncated(d):
    import tempfile
    p = os.path.join(tempfile.gettempdir(), "agentos_trunc.png")
    with open(p, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 56)
    for t in NG.SHOT_TAGS:
        d["shot_path_%s" % t] = p


def mut_menu_miswired(d):
    v = list(d["native_menu_forward_targets"])
    v.reverse()
    d["native_menu_forward_targets"] = v


def mut_menu_no_cmdk(d):
    d["native_menu_has_cmdk"] = 0


def mut_deg_dead_button(d):
    d["start_wired"] = False


def mut_deg_service_not_started(d):
    d["service_started_by_app"] = 0
    d["degraded_health_after_tap"] = 0


def mut_deg_never_explained(d):
    d["recovery_detail"] = "connection refused"


def mut_deg_no_panel(d):
    d["degraded_panel_missing"] = 1


def mut_deg_recovery_fakes_dead(d):
    """D108：服务真恢复了，但终值快照落在恢复前最后一枪 —— 采样里有 ok=1。
    恢复判据必须靠采样认账，否则「晚一步起稳」被卡成假红。"""
    d["degraded_health_after_tap"] = 0
    d["degraded_health_samples"] = [
        {"t": 0.0, "ok": 0, "detail": "starting"},
        {"t": 4.2, "ok": 0, "detail": "boot"},
        {"t": 8.6, "ok": 1, "detail": "ok"},
    ]


def mut_deg_never_recovered(d):
    """D108：服务确实起不来 —— 终值 0，采样整轮 25s 全 ok=0。
    这是真红；判据不许因为「有采样字段」就放过。"""
    d["degraded_health_after_tap"] = 0
    d["degraded_health_samples"] = [
        {"t": 0.0, "ok": 0, "detail": "boot"},
        {"t": 12.0, "ok": 0, "detail": "boot"},
        {"t": 25.0, "ok": 0, "detail": "deadline"},
    ]


# ---- D100c 面板变异体 ----
# 每条都相对 base **恒说谎**（真值写反），不能写死 True/False ——
# 否则判据和变异体一起恒定，测不出「这道判据到底有没有牙」。

def mut_panel_tree_never_loaded(d):
    d["panel_tree_loaded"] = 0
    d["outline_rows"] = 0
    d["tree_names_head"] = []


def mut_panel_tree_no_real_files(d):
    d["tree_names_head"] = ["x"]
    d["outline_root_children"] = 1


def mut_panel_never_expanded(d):
    d["panel_expanded"] = 0
    d["panel_expand_index"] = 0


def mut_panel_term_failed(d):
    d["term_last_ok"] = 0
    d["term_last_rc"] = 1


def mut_panel_term_silent_empty(d):
    """D100 假绿 A 的精确复现：引号没剥，rc=0 但什么都没算。"""
    d["term_last_out"] = ""
    d["term_last_out_len"] = 0


def mut_panel_argv_quoted(d):
    """D100 假绿 A 的根因复现：argv 里留着裸引号。"""
    d["term_last_argv"] = ["python3", "-c", '"print(6*7)"']
    d["term_argv_of_placeholder"] = ["python3", "-c", '"print(6*7)"']


def mut_panel_shot_flat_color(d):
    d["panel_shot_distinct_colors_panel"] = 1


def mut_panel_shot_all_black(d):
    """平涂黑图的真实签名是 ink 高**且** distinct 极低（实测 11）。

    D105 起判据改成「ink 不高 或 distinct 够多」（深色主题下真内容
    ink 本来就 0.87~0.95，只卡 ink 会把真内容判红）。若这里只改 ink，
    distinct 仍是真内容的 186，新判据照样绿 —— 那不是判据有牙，
    是变异体在空转。两个字段一起打才是真故障。
    """
    d["panel_shot_ink_ratio_panel"] = 0.99
    d["panel_shot_distinct_colors_panel"] = 11


def mut_panel_shot_picked_blank_channel(d):
    """模拟「截图选了被判为全黑的那个通道」。

    这里**不能写死通道名**。哪条通道被判 blank 取决于那一盘的真实像素：
    D100 首跑 layer 是全黑（distinct=11 ink=0.987），而 D103 这一盘
    layer 画出了真内容（distinct=233 ink=0.057），于是 blanks=[]。
    写死 "layer" 的旧变异体在 blanks=[] 时前提失效，判据照样绿 ——
    那不是判据有牙，那是变异体在空转。所以先从证据里取 blanks，
    为空时补一个假的，保证模拟的仍是真会发生的故障。
    """
    blanks = d.get("panel_shot_blank_channels") or []
    ch = blanks[0] if blanks else "layer"
    if not blanks:
        d["panel_shot_blank_channels"] = [ch]
    d["panel_shot_method_panel"] = ch


def mut_toolbar_lost_panel(d):
    d["native_toolbar_items"] = ["health", "reload"]


def mut_menu_no_cmdt(d):
    d["native_menu_has_cmdt"] = 0


# --- D103 五窗格：四个真实缺陷的变异体 ---
def mut_panel_pane_segments_zero(d):
    """缺陷 A 的精确复现：NSSegmentedControl 0 段，tab 一个都没画。"""
    d["pane_segment_count"] = 0
    d["panel_ax_pane_segment_count"] = 0
    d["panel_ax_pane_has_all_five"] = False


def mut_panel_pane_titles_short(d):
    d["pane_titles"] = ["文件", "终端"]


def mut_panel_pane_ax_missing(d):
    """AX 树里读不到五个 tab —— 辅助功能用户找不到窗格。"""
    d["panel_ax_pane_segment_count"] = 0


def mut_panel_pane_no_switch(d):
    d["pane_switches"] = 1


def mut_panel_pane_two_visible(d):
    """两个窗格同时可见 = 布局塌了，内容互相压。"""
    d["pane_visible_count"] = 2


def mut_panel_pane_missed_visit(d):
    d["pane_visits"] = ["文件"]


def mut_panel_code_never_loaded(d):
    d["code_loaded"] = 0
    d["code_chars"] = 0
    d["code_error"] = "boom"


def mut_panel_code_direct_call(d):
    """缺陷 D：绕过文件树直接 loadCode，真实链路没接上。"""
    d["code_source"] = "direct"


def mut_panel_code_editable(d):
    d["code_textview_editable"] = True


def mut_panel_browser_blank_page(d):
    """缺陷 B：didFinish 回调了，但画出来是白页。"""
    d["browser_rendered_chars"] = 0


def mut_panel_browser_no_title(d):
    d["browser_dom_title"] = ""


def mut_panel_browser_let_external(d):
    """回环限制被摘掉：example.com 也能导航出去。"""
    d["browser_rejected"] = ""


def mut_panel_browser_external_nav(d):
    d["browser_last_requested"] = "http://example.com/steal"


def mut_panel_preview_blank(d):
    d["preview_rendered_chars"] = 0


def mut_panel_preview_confused(d):
    """缺陷 C：预览格被浏览器格的回调盖掉了。"""
    d["preview_path"] = d["browser_url"]


def mut_panel_shot_code_flat(d):
    d["panel_shot_distinct_colors_code"] = 1


def mut_panel_shot_code_black(d):
    d["panel_shot_ink_ratio_code"] = 0.99
    d["panel_shot_distinct_colors_code"] = 11


def mut_panel_shot_code_picked_blank(d):
    """同上：代码格截图选了被判全黑的通道。"""
    blanks = d.get("panel_shot_blank_channels") or []
    ch = blanks[0] if blanks else "layer"
    if not blanks:
        d["panel_shot_blank_channels"] = [ch]
    d["panel_shot_method_code"] = ch


def mut_panel_ax_no_run_button(d):
    d["panel_ax_has_run_button"] = False


def mut_panel_ax_no_browser_go(d):
    d["panel_ax_has_browser_go"] = False


def mut_proj_composition_outer_key(d):
    """把内层键写成外层键的值 —— 模拟「实现读错键，改成读外层」。
    D105 之前 window_content_subviews 就是驾驶舱|工作区；改嵌套后它变
    成 ProjectsPanel|内层split。若门还在读外键，这道判据必须翻红。"""
    d["window_content_subviews_inner"] = ["ProjectsPanel", "NSSplitView"]


def mut_proj_composition_missing(d):
    d["window_content_subviews"] = ["WKWebView", "WorkspacePanel"]


def mut_proj_kind_flat(d):
    d["window_content_kind"] = "NSView"


def mut_proj_hidden(d):
    d["projects_default_visible"] = 0


def mut_proj_never_loaded(d):
    d["proj_loaded"] = 0


def mut_proj_count_zero(d):
    d["proj_count"] = 0


def mut_proj_relative_roots(d):
    d["proj_roots_absolute"] = False


def mut_proj_rows_short(d):
    n = int(d.get("proj_count") or 0)
    d["proj_row_view_count"] = max(0, n - 1)


def mut_proj_no_active(d):
    d["proj_active_flag"] = []


def mut_proj_selection_wrong(d):
    d["proj_selected_is_active"] = False


def mut_proj_nfiles_zero(d):
    d["proj_nfiles"] = [0 for _ in (d.get("proj_nfiles") or [0])]


def mut_proj_ax_no_header(d):
    d["proj_ax_has_header"] = False
    d["proj_ax_header"] = ""


def mut_proj_ax_rows_unnamed(d):
    d["proj_ax_row_count"] = 0
    d["proj_ax_rows_named"] = 0


def mut_proj_labels_blank(d):
    """项目名渲染成空串：行还在、计数也对，只有名字是空的。"""
    d["proj_labels"] = ["   " for _ in (d.get("proj_labels") or [""])]


def mut_proj_status_empty(d):
    """底部状态栏空着 —— 数据接上了但没人把结论说出来。"""
    d["proj_status_text"] = ""


def mut_keyeq_path_faked(d):
    """路径被摘：只调 switchView 不走菜单 —— keyeq_path 会暴露。"""
    d["keyeq_path"] = "direct-switchView"


def mut_keyeq_incomplete(d):
    d["keyeq_sequence_complete"] = 0


def mut_keyeq_one_shortcut_dead(d):
    """⌘7 的 keyEquivalent 被摘 —— 只有逐键记 consumed 才量得到。"""
    d["keyeq_consumed_goal"] = 0


def mut_keyeq_tab_not_reached(d):
    """菜单消费了但页面没切过去（action 指错 selector）。"""
    d["keyeq_tab_goal"] = "over"
    d["keyeq_views_matched"] = 9


def mut_keyeq_cmdk_no(d):
    d["keyeq_cmdk_on"] = 0


def mut_keyeq_cmdt_missing(d):
    d["keyeq_cmdt_visible"] = -1


def mut_keyeq_cmdp_no_focus(d):
    d["keyeq_projects_focused"] = 0


def mut_keyeq_arrow_dead(d):
    d["proj_arrow_moves"] = 0


def mut_menu_no_cmdp(d):
    d["native_menu_has_cmdp"] = 0


def mut_menu_cmdp_no_focus(d):
    d["native_menu_cmdp_result_focused"] = 0


# D105 用**合成 base**证明这两道判据有牙，与「这台机器此刻能不能采到 AX」
# 解耦：系统 python3 没有 PyObjC 时真样本可能缺席，而判据恒真是更重的病。
# 合成 base 取 D105-实测9 的 AgentOS.app 真读数（rc=0）。
AX_BASE_SYNTH = {
    "proc": "AgentOS", "ok": True, "pid": 46184, "target_pid": 46184,
    "windows_err": 0, "windows": 1, "window_titles": ["AgentOS"],
    "nodes": 305, "cycles": 0, "menu_nodes": 0,
    "content_nodes": 305, "texts": 215,
    "web_area": 1,
    "sustained": True, "samples": 8, "samples_exposed": 7,
    "samples_need": 5, "samples_pct": 88,
    "roles": {"AXWindow": 1, "AXWebArea": 1, "AXStaticText": 149,
              "AXGroup": 99, "AXButton": 10, "AXHeading": 4},
    "text_samples": ["AgentOS", "legend", "heading"],
}


def mut_ax_windows_zero(d):
    d["windows"] = 0


def mut_ax_windows_err(d):
    d["windows_err"] = -25204


def mut_ax_not_ok(d):
    d["ok"] = False


def mut_ax_self_link(d):
    d["content_nodes"] = 31
    d["texts"] = 0


def mut_ax_menu_saturated(d):
    d["nodes"] = 600
    d["menu_nodes"] = 600
    d["content_nodes"] = 0
    d["texts"] = 600


def mut_ax_texts_thin(d):
    d["texts"] = 2


def mut_ax_foreign_pid(d):
    """树完美无缺，但采的是用户自己开着那个实例（D105 实测踩实过）。"""
    d["target_pid"] = 99999


def mut_ax_unpinned(d):
    """调用方忘了钉 pid —— 必须当成事故，而不是默认可信。"""
    d.pop("target_pid", None)


def mut_ax_bare_shell(d):
    """D105b 实测踩实：加载前那棵 23 节点/7 文本的窗口外壳树，
    节点数和文本数都过线，只有 AXWebArea=0。它必须判红。"""
    d["nodes"] = 23
    d["content_nodes"] = 23
    d["texts"] = 7
    d["web_area"] = 0


def mut_ax_loaded_texts_thin(d):
    """内容区在，但读不出足够的字（实测加载后文本下限是 7，门取 5）。"""
    d["texts"] = 4


def mut_ax_series_too_thin(d):
    """只采了 2 个样本 —— 单次采样判据这时 need=ceil(2*60/100)=2，
    两条好就 100% 通过，等于把「间歇性」这个实测事实又放过去了。"""
    d["samples"] = 2
    d["samples_exposed"] = 2
    d["samples_need"] = 2
    d["samples_pct"] = 100


def mut_ax_series_intermittent(d):
    """D105b 实测踩实的形态：8 次采样只暴露 1 次（tree 完美但断续）。"""
    d["samples_exposed"] = 1
    d["samples_pct"] = 13
    d["sustained"] = False


def mut_si_a_not_in_table(d):
    """A 进程探完就没了 —— 采样时它压根不在进程表里，这条判据是空转的。"""
    d["a_in_process_table"] = False


def mut_si_second_rc_nonzero(d):
    """第二个实例以非 0 退出（崩溃/被拒），却仍被判绿 = 并存或静默失败。"""
    d["second_rc"] = 1


def mut_si_no_takeover_msg(d):
    """第二个实例既没接管也没激活已有窗口，直接静默退出 —— 用户双击没反应。"""
    d["second_takeover_msg"] = False


def mut_si_second_slow(d):
    """第二个实例没有快速让路，而是又跑起一整套（耗时超阈值）。"""
    d["second_elapsed"] = 12.0


def mut_si_second_wrote_probe(d):
    """第二个实例写了 probe.json —— 说明它真的起了完整服务，不是接管分支。"""
    d["second_probe_exists"] = True


def mut_si_extra_process(d):
    """进程数变多：两个实例并存了（probes after > before）。"""
    d["after_pids"] = list(d.get("after_pids") or []) + [99999]


def mut_si_multi_two(d):
    """首个实例自己报 multi=2 —— 单实例探针读到了互相说谎的字段。"""
    d["first_multi_instance"] = 2


def mut_si_sib_missing(d):
    """兄弟实例数字段缺失（读数被截断/字段改名），证据不完整。"""
    d["first_sibling_instances"] = None


def main():
    os.makedirs(FIX, exist_ok=True)
    base = load(BASE_MAIN)
    axb = dict(AX_BASE_SYNTH)
    ax_live = os.path.join(NG.WORK, "ax.json")
    _rows, red = judge_all(NG.judge_ax, axb)
    note(not red, join("base-green judge_ax(synth)", ",".join(red)))
    if os.path.isfile(ax_live):
        _rows, red = judge_all(NG.judge_ax, load(ax_live))
        note(not red, join("base-green judge_ax(live)", ",".join(red)))
    deg = load(BASE_DEG)

    print("[1/3] base 必须全绿（判据非恒红的下限）")
    groups = (NG.judge_window, NG.judge_menu, NG.judge_webview,
              NG.judge_view_switch, NG.judge_cmdk, NG.judge_pixels,
              NG.judge_panel, NG.judge_projects)
    for fn in groups:
        _rows, red = judge_all(fn, base)
        note(not red, join("base-green", fn.__name__, ",".join(red)))
    _rows, red = judge_all(NG.judge_degraded, deg)
    note(not red, join("base-green judge_degraded", ",".join(red)))
    _rows, red = judge_all(lambda _d: NG.judge_bundle(), {})
    note(not red, join("base-green judge_bundle", ",".join(red)))

    print("[2/3] 注入变异体，要求指定判据翻红")
    mutate("m_window_invisible", base, mut_window,
           NG.judge_window, "window-on-screen", "窗口不可见")
    mutate("m_window_minimized", base, mut_minimized,
           NG.judge_window, "window-on-screen", "窗口被最小化")
    mutate("m_window_offscreen", base, mut_window_offscreen,
           NG.judge_window, "window-on-screen", "窗口帧全在屏幕外")
    mutate("m_mini_not_restored", base, mut_mini_not_restored,
           NG.judge_window, "window-minimized-restored",
           "起时最小化却没被拉回来")
    mutate("m_restore_keys_removed", base, mut_restore_keys_removed,
           NG.judge_window, "window-minimized-restored",
           "恢复动作的两个键不见了（修复被回退）")
    mutate("m_no_activation", base, mut_no_activation,
           NG.judge_window, "window-requested-activation", "没请求焦点")
    mutate("m_key_lies", base, mut_key_lies,
           NG.judge_window, "window-key-matches-foreground", "key说谎")
    mutate("m_active_never_key", base, mut_active_never_key,
           NG.judge_window, "window-became-key-when-foreground",
           "自称前台却没拿到key")
    mutate("m_not_full_size", base, mut_not_full_size,
           NG.judge_window, "window-full-size-content", "没开合并标题栏")
    mutate("m_blank_page/title", base, mut_blank_page,
           NG.judge_webview, "page-title-real", "页面空白")
    mutate("m_blank_page/body", base, mut_blank_page,
           NG.judge_webview, "page-body-text", "页面空白")
    mutate("m_nav_failed", base, mut_nav_failed,
           NG.judge_webview, "webview-nav-finished", "导航失败")
    mutate("m_switch_noop", base, mut_switch_noop,
           NG.judge_view_switch, "switch-mx-tab", "点菜单屏没变")
    mutate("m_cmkd_not_focusable", base, mut_cmkd_not_focusable,
           NG.judge_cmdk, "cmdk-focus-in-input", "焦点没进输入框")
    mutate("m_shot_blank", base, mut_shot_blank,
           NG.judge_pixels, "shot-mx-distinct-colors", "截图纯色")
    mutate("m_shot_same_file", base, mut_shot_same_file,
           NG.judge_pixels, "shot-hashes-distinct", "5张同一张")
    mutate("m_shot_truncated", base, mut_shot_truncated,
           NG.judge_pixels, "shot-cmdk-bytes", "截图被截断")
    mutate("m_menu_miswired", base, mut_menu_miswired,
           NG.judge_menu, "menu-forward-targets-match", "转发接错线")
    mutate("m_menu_no_cmdk", base, mut_menu_no_cmdk,
           NG.judge_menu, "menu-has-cmdk", "菜单没有cmdK")
    mutate("m_deg_dead_button", deg, mut_deg_dead_button,
           NG.judge_degraded, "degraded-start-button-wired", "按钮没接线")
    mutate("m_deg_service_not_started", deg, mut_deg_service_not_started,
           NG.judge_degraded, "degraded-service-started", "没拉起服务")
    mutate("m_deg_never_explained", deg, mut_deg_never_explained,
           NG.judge_degraded, "degraded-panel-explains-selfheal", "不解释")
    mutate("m_deg_no_panel", deg, mut_deg_no_panel,
           NG.judge_degraded, "degraded-panel-not-missing", "面板没出现")
    # D108 恢复判据双向：采样出现过 ok=1 = 真恢复过（终值快照早一枪也不能假红）；
    # 采样整轮全 ok=0 = 服务确实起不来（有采样字段也不许放过）。
    stays_green("m_deg_recovery_fakes_dead", deg, mut_deg_recovery_fakes_dead,
                NG.judge_degraded, "degraded-health-recovered",
                "采样出现过 ok=1，终值快照早一枪也不能假红")
    mutate("m_deg_never_recovered", deg, mut_deg_never_recovered,
           NG.judge_degraded, "degraded-health-recovered",
           "采样整轮 25s 全 ok=0，服务确实起不来")
    mutate("m_panel_tree_never_loaded", base, mut_panel_tree_never_loaded,
           NG.judge_panel, "panel-tree-loaded", "面板树压根没加载")
    mutate("m_panel_tree_no_real_files", base, mut_panel_tree_no_real_files,
           NG.judge_panel, "panel-tree-has-real-files", "树里没有真文件")
    mutate("m_panel_never_expanded", base, mut_panel_never_expanded,
           NG.judge_panel, "panel-tree-expanded", "目录没展开")
    mutate("m_panel_term_failed", base, mut_panel_term_failed,
           NG.judge_panel, "panel-term-rc-zero", "终端命令失败")
    mutate("m_panel_term_silent_empty", base, mut_panel_term_silent_empty,
           NG.judge_panel, "panel-term-computed-42", "rc=0但静默空转")
    mutate("m_panel_argv_quoted", base, mut_panel_argv_quoted,
           NG.judge_panel, "panel-term-argv-parsed", "argv里留着裸引号")
    mutate("m_panel_shot_flat_color", base, mut_panel_shot_flat_color,
           NG.judge_panel, "panel-shot-distinct-colors", "截图纯色")
    mutate("m_panel_shot_all_black", base, mut_panel_shot_all_black,
           NG.judge_panel, "panel-shot-ink-ratio", "截图满屏涂黑")
    mutate("m_panel_shot_picked_blank", base,
           mut_panel_shot_picked_blank_channel,
           NG.judge_panel, "panel-shot-method-not-blank", "选中了全黑通道")
    mutate("m_toolbar_lost_panel", base, mut_toolbar_lost_panel,
           NG.judge_panel, "panel-toolbar-wired", "工具栏少了面板按钮")
    mutate("m_menu_no_cmdt", base, mut_menu_no_cmdt,
           NG.judge_panel, "panel-menu-has-cmdt", "菜单没有cmdT")

    print("[2b/3] D103 五窗格变异体（四个真实缺陷）")
    mutate("m_panel_pane_segments_zero", base, mut_panel_pane_segments_zero,
           NG.judge_panel, "panel-pane-segments-declared", "0段分段器假绿")
    mutate("m_panel_pane_titles_short", base, mut_panel_pane_titles_short,
           NG.judge_panel, "panel-pane-titles-complete", "窗格标题不全")
    mutate("m_panel_pane_ax_missing", base, mut_panel_pane_ax_missing,
           NG.judge_panel, "panel-pane-tabs-in-ax-tree", "AX树读不到tab")
    mutate("m_panel_pane_no_switch", base, mut_panel_pane_no_switch,
           NG.judge_panel, "panel-pane-switch-count", "几乎没切过窗格")
    mutate("m_panel_pane_two_visible", base, mut_panel_pane_two_visible,
           NG.judge_panel, "panel-pane-only-one-visible", "两格同时可见")
    mutate("m_panel_pane_missed_visit", base, mut_panel_pane_missed_visit,
           NG.judge_panel, "panel-pane-visited-all-five", "只逛过文件格")
    mutate("m_panel_code_never_loaded", base, mut_panel_code_never_loaded,
           NG.judge_panel, "panel-code-loaded", "代码格没加载")
    mutate("m_panel_code_direct_call", base, mut_panel_code_direct_call,
           NG.judge_panel, "panel-code-via-tree-selection", "绕过文件树直调")
    mutate("m_panel_code_editable", base, mut_panel_code_editable,
           NG.judge_panel, "panel-code-readonly", "代码格可被编辑")
    mutate("m_panel_browser_blank_page", base,
           mut_panel_browser_blank_page,
           NG.judge_panel, "panel-browser-rendered-chars", "浏览器白页")
    mutate("m_panel_browser_no_title", base, mut_panel_browser_no_title,
           NG.judge_panel, "panel-browser-dom-title", "浏览器无标题")
    mutate("m_panel_browser_let_external", base,
           mut_panel_browser_let_external,
           NG.judge_panel, "panel-browser-rejects-external", "回环限制被摘")
    mutate("m_panel_browser_external_nav", base,
           mut_panel_browser_external_nav,
           NG.judge_panel, "panel-browser-rejects-external", "真导航到外网")
    mutate("m_panel_preview_blank", base, mut_panel_preview_blank,
           NG.judge_panel, "panel-preview-rendered-chars", "预览白页")
    mutate("m_panel_preview_confused", base, mut_panel_preview_confused,
           NG.judge_panel, "panel-preview-not-confused-with-browser",
           "预览与浏览器互相覆盖")
    mutate("m_panel_shot_code_flat", base, mut_panel_shot_code_flat,
           NG.judge_panel, "panel-shot-code-distinct-colors", "代码格纯色")
    mutate("m_panel_shot_code_black", base, mut_panel_shot_code_black,
           NG.judge_panel, "panel-shot-code-ink-ratio", "代码格满屏涂黑")
    mutate("m_panel_shot_code_picked_blank", base,
           mut_panel_shot_code_picked_blank,
           NG.judge_panel, "panel-shot-code-method-not-blank",
           "代码格选了全黑通道")
    mutate("m_panel_ax_no_run_button", base, mut_panel_ax_no_run_button,
           NG.judge_panel, "panel-ax-has-run-button", "AX里没有运行按钮")
    mutate("m_panel_ax_no_browser_go", base, mut_panel_ax_no_browser_go,
           NG.judge_panel, "panel-ax-has-browser-go", "AX里没有打开按钮")

    print("[2c/3] D105 左侧项目栏 + 键盘走查变异体")
    mutate("m_panel_composition_reads_outer", base,
           mut_proj_composition_outer_key,
           NG.judge_panel, "panel-window-composition", "门读错键(读外层)")
    mutate("m_proj_composition_missing", base, mut_proj_composition_missing,
           NG.judge_projects, "projects-window-composition",
           "左侧栏不在窗口构成里")
    mutate("m_proj_kind_flat", base, mut_proj_kind_flat,
           NG.judge_projects, "projects-window-composition",
           "外层不是横向split")
    mutate("m_proj_hidden", base, mut_proj_hidden,
           NG.judge_projects, "projects-default-visible", "左侧栏默认不可见")
    mutate("m_proj_never_loaded", base, mut_proj_never_loaded,
           NG.judge_projects, "projects-loaded", "项目数据压根没接上")
    mutate("m_proj_count_zero", base, mut_proj_count_zero,
           NG.judge_projects, "projects-loaded", "项目列表是空的")
    mutate("m_proj_relative_roots", base, mut_proj_relative_roots,
           NG.judge_projects, "projects-roots-absolute", "项目根是相对路径")
    mutate("m_proj_rows_short", base, mut_proj_rows_short,
           NG.judge_projects, "projects-row-views-match", "少一行原生视图")
    mutate("m_proj_no_active", base, mut_proj_no_active,
           NG.judge_projects, "projects-active-flagged", "没标出当前项目")
    mutate("m_proj_selection_wrong", base, mut_proj_selection_wrong,
           NG.judge_projects, "projects-active-flagged", "选中行不是当前项目")
    mutate("m_proj_nfiles_zero", base, mut_proj_nfiles_zero,
           NG.judge_projects, "projects-file-counts", "文件数全是0")
    mutate("m_proj_ax_no_header", base, mut_proj_ax_no_header,
           NG.judge_projects, "projects-ax-has-header", "AX里没有标题")
    mutate("m_proj_ax_rows_unnamed", base, mut_proj_ax_rows_unnamed,
           NG.judge_projects, "projects-ax-rows-named", "AX里读不到项目行")
    mutate("m_proj_labels_blank", base, mut_proj_labels_blank,
           NG.judge_projects, "projects-labels-real", "项目名渲染成空白")
    mutate("m_proj_status_empty", base, mut_proj_status_empty,
           NG.judge_projects, "projects-status-text", "状态栏没写结论")
    mutate("m_keyeq_path_faked", base, mut_keyeq_path_faked,
           NG.judge_projects, "keyeq-real-menu-path", "没走菜单按键路径")
    mutate("m_keyeq_incomplete", base, mut_keyeq_incomplete,
           NG.judge_projects, "keyeq-sequence-complete", "序列没走完")
    mutate("m_keyeq_one_shortcut_dead", base, mut_keyeq_one_shortcut_dead,
           NG.judge_projects, "keyeq-all-views-consumed", "⌘7快捷键被摘")
    mutate("m_keyeq_tab_not_reached", base, mut_keyeq_tab_not_reached,
           NG.judge_projects, "keyeq-all-views-reached", "消费了但页面没切")
    mutate("m_keyeq_cmdk_no", base, mut_keyeq_cmdk_no,
           NG.judge_projects, "keyeq-cmdk-opens", "⌘K没打开命令板")
    mutate("m_keyeq_cmdt_missing", base, mut_keyeq_cmdt_missing,
           NG.judge_projects, "keyeq-cmdt-toggled", "⌘T状态读不出来")
    mutate("m_keyeq_cmdp_no_focus", base, mut_keyeq_cmdp_no_focus,
           NG.judge_projects, "keyeq-cmdp-focuses-projects", "⌘P没聚焦左栏")
    mutate("m_keyeq_arrow_dead", base, mut_keyeq_arrow_dead,
           NG.judge_projects, "projects-arrow-key-moves", "↓键选不动行")
    mutate("m_menu_no_cmdp", base, mut_menu_no_cmdp,
           NG.judge_projects, "native-menu-has-cmdp", "菜单没有⌘P")
    mutate("m_menu_cmdp_no_focus", base, mut_menu_cmdp_no_focus,
           NG.judge_projects, "native-menu-cmdp-focuses", "⌘P点了没聚焦")

    print("[3/3] ATS 变异体（fixture bundle，真 bundle 只读）")
    bundle_mutate("m_ats_no_local", "A", {}, "bundle-ats-loopback",
                  "撤掉回环放行")
    bundle_mutate("m_ats_blanket", "B",
                  {"NSAllowsLocalNetworking": True,
                   "NSAllowsArbitraryLoads": True},
                  "bundle-ats-not-arbitrary", "整体放开")

    print("[3b/3] D105 AX 判据变异体（屏幕阅读器层物证）")
    mutate("m_ax_windows_zero", axb, mut_ax_windows_zero,
           NG.judge_ax, "ax-window-exposed", "AX 读不到窗口")
    mutate("m_ax_windows_err", axb, mut_ax_windows_err,
           NG.judge_ax, "ax-window-exposed", "kAXWindows 报错")
    mutate("m_ax_not_ok", axb, mut_ax_not_ok,
           NG.judge_ax, "ax-window-exposed", "AX 采样压根没成")
    mutate("m_ax_self_link", axb, mut_ax_self_link,
           NG.judge_ax, "ax-content-tree", "只剩 AXApplication 自链")
    mutate("m_ax_menu_saturated", axb, mut_ax_menu_saturated,
           NG.judge_ax, "ax-content-tree", "树被菜单项吃光")
    mutate("m_ax_texts_thin", axb, mut_ax_texts_thin,
           NG.judge_ax, "ax-content-tree", "内容节点够但没读出文本")
    mutate("m_ax_foreign_pid", axb, mut_ax_foreign_pid,
           NG.judge_ax, "ax-target-is-under-test", "AX 树采自别人家的实例")
    mutate("m_ax_unpinned", axb, mut_ax_unpinned,
           NG.judge_ax, "ax-target-is-under-test", "AX 采样忘了钉 pid")
    mutate("m_ax_bare_shell", axb, mut_ax_bare_shell,
           NG.judge_ax, "ax-content-tree", "只有窗口外壳，网页内容区没进 AX 树")
    mutate("m_ax_loaded_texts_thin", axb, mut_ax_loaded_texts_thin,
           NG.judge_ax, "ax-content-tree", "内容区在但读不出足够文本")
    mutate("m_ax_series_too_thin", axb, mut_ax_series_too_thin,
           NG.judge_ax, "ax-sustained", "序列只采了 2 个样本，判据等于没判")
    mutate("m_ax_series_intermittent", axb, mut_ax_series_intermittent,
           NG.judge_ax, "ax-sustained", "8 次采样只暴露 1 次（AX 间歇暴露）")

    print("[3c/3] D107 单实例判据变异体（base = 实跑落盘的真 facts）")
    if os.path.isfile(BASE_SI):
        sib = load(BASE_SI)
        _rows, red = judge_all(SIP.judge, sib)
        note(not red, join("base-green", "judge_single_instance(live)", ",".join(red)))
        mutate("m_si_a_not_in_table", sib, mut_si_a_not_in_table,
               SIP.judge, "single-instance-a-in-process-table",
               "A 压根不在进程表里")
        mutate("m_si_second_rc_nonzero", sib, mut_si_second_rc_nonzero,
               SIP.judge, "single-instance-second-rc",
               "第二个实例非 0 退出")
        mutate("m_si_no_takeover_msg", sib, mut_si_no_takeover_msg,
               SIP.judge, "single-instance-second-activated-existing",
               "第二个实例静默退出，没接管也没激活")
        mutate("m_si_second_slow", sib, mut_si_second_slow,
               SIP.judge, "single-instance-second-exited-fast",
               "第二个实例没快速让路")
        mutate("m_si_second_wrote_probe", sib, mut_si_second_wrote_probe,
               SIP.judge, "single-instance-second-wrote-no-probe",
               "第二个实例写了 probe.json（真起了服务）")
        mutate("m_si_extra_process", sib, mut_si_extra_process,
               SIP.judge, "single-instance-no-extra-process",
               "两个实例并存，进程数变多")
        mutate("m_si_multi_two", sib, mut_si_multi_two,
               SIP.judge, "single-instance-evidence-fields",
               "首个实例自报 multi=2")
        mutate("m_si_sib_missing", sib, mut_si_sib_missing,
               SIP.judge, "single-instance-evidence-fields",
               "兄弟实例字段缺失")
    else:
        note(False, join("NO-BASE", "single_instance_facts.json 缺失，D107 无牙可验"))

    n_ok = sum(1 for r in RESULTS if r["ok"])
    rc = 0 if not FAILS else 1
    rep = {"gate": "native_gate_selftest", "rc": rc,
           "checks": len(RESULTS), "ok": n_ok, "red": FAILS,
           "base_main": BASE_MAIN,
           "base_degraded": BASE_DEG,
           "results": RESULTS}
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    tmp = REPORT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, REPORT)
    print("")
    if rc == 0:
        print("NATIVE_GATE_SELFTEST_OK %d/%d" % (n_ok, len(RESULTS)))
    else:
        print("NATIVE_GATE_SELFTEST_RED %d/%d  %s"
              % (n_ok, len(RESULTS), " | ".join(FAILS)))
    print("证据 work/native_gate_selftest.json")
    return rc


def write_fixture_bundle(tag, ats):
    """造一个 fixture bundle，plist 里 ATS 按参数写。"""
    app = os.path.join(FIX, "Fixture%s.app" % tag)
    macos = os.path.join(app, "Contents", "MacOS")
    if os.path.isdir(app):
        shutil.rmtree(app)
    os.makedirs(macos)
    pl = {"CFBundleIdentifier": "com.zhangfengrui.agentos-console",
          "CFBundlePackageType": "APPL",
          "CFBundleExecutable": "AgentOS",
          "NSAppTransportSecurity": ats}
    p = os.path.join(app, "Contents", "Info.plist")
    with open(p, "wb") as f:
        plistlib.dump(pl, f)
    exe = os.path.join(macos, "AgentOS")
    with open(exe, "wb") as f:
        f.write(b"#!/bin/sh\n")
    os.chmod(exe, os.stat(exe).st_mode | stat.S_IXUSR)
    return app


def bundle_mutate(label, tag, ats, ck, why):
    """judge_bundle 读模块级全局 APP/BIN/PLIST，先重绑到 fixture，
    跑完立刻还原 —— 绝不让自证写到真 bundle 上。
    fixture 没签名，所以 bundle-adhoc-signed 也会红；这里只断言
    指定判据翻红，不要求「只有它红」。"""
    app = write_fixture_bundle(tag, ats)
    save = (NG.APP, NG.BIN, NG.PLIST)
    NG.APP = app
    NG.BIN = os.path.join(app, "Contents", "MacOS", "AgentOS")
    NG.PLIST = os.path.join(app, "Contents", "Info.plist")
    try:
        rows, _red = judge_all(lambda _d: NG.judge_bundle(), {})
    finally:
        NG.APP, NG.BIN, NG.PLIST = save
    v = verdict(rows, ck)
    if v is None:
        note(False, join("NOCHECK", label, ck))
        return
    if v:
        note(False, join("NO-TEETH", label, ck))
        return
    note(True, join("RED-OK", label, ck, why))


if __name__ == "__main__":
    sys.exit(main())
