# -*- coding: utf-8 -*-
"""agent_core —— AgentOS 终端 agent 的工具循环内核（只用标准库）。

为什么要有这一层
----------------
在本文件之前，「agent」这个词在本项目里指的是**固定 argv 路由器**：
serve.py 的 ACTIONS 只有 11 条写死的命令，resolve_intent() 把一句人话映射到
其中之一。它能自证、能流式、能被中止，但它做不了「读这个文件、改这两行、
跑一下测试」这类真实工程动作 —— 因为压根没有一个循环把「模型的下一步意图」
变成「一次真实执行、结果回到上下文里」。

本文件补的正是那一环，并且**不新开一套真相**：
  · 工具层尽量复用 serve.py 已有的边界（_fs_sandbox 的 realpath 沙箱、
    TERM_CMDS 的只读白名单、gh_client.run_gh 的只读白名单）；
  · 口试用 OpenAI chat/completions 的 tool_calls，因为本机 CC Switch 的
    127.0.0.1:15799/v1 端点**实测**支持（2026-10-10 实测见 tools/neg_agent_core.py 的
    正例，以及本文件 `--check` 自检）；
  · 每一步都有真实 rc / 真实字节，失败分类显式，绝不 fail-open。

权限模型（对齐 claude code 的思路，但边界比它严）
----------------------------------------------
每个工具有一个 risk 类：read / write。plan 模式下 write 一律拒绝；
accept-edits / full 模式下 write 需要审批者点头；run_cmd 与 gh_read 的
「只读白名单内向量」自动放行，白名单外一律要走审批。

另有**硬阻断清单**，任何模式、任何审批者都不能放行：git push、rm、rmdir、
git reset --hard、dd、mkfs、shutdown、sudo、launchctl…… 理由是本机两条
既存铁律：①「需用户明确说『发』才执行」的对外操作；②`rm -f` 会被 exec
policy 拒绝且拒绝发生在 shell 启动之前，所以 agent 也不该去撞它。

失败分类（上层据此给不同退出码，绝不含混成 rc=0）
-----------------------------------------------
no-provider  解析不到模型端点（env / codex 配置都没有）
provider      端点不可达 / 非 200 / 返回体形状不对
usage        调用方参数不合法（空需求、越界 step 数）
limit        达到 step / 时间 / 输出预算
tool         工具执行层错误（路径越界、命令不在白名单、写盘失败）
approval     审批被拒（这不是失败，是既定行为；回答里如实写「未执行」）

退出码口径（全项目同一套）：0 = agent 给出了最终回答；2 = 用法/预算；
3 = 基础设施（provider / HTTP /  harness）。
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import termios
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import urllib.parse
import shutil
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION = "0.1.0"

MAX_STEPS = 24  # 一次任务里最多多少轮「模型→工具→模型」
CALL_TIMEOUT = 120  # 单次模型调用上限秒
MAX_TOOL_OUT = 32768  # 单次工具回灌给模型的字节上限
MAX_FILE_BYTES = 262144  # 单文件读上限，与 serve.py FS_MAX_BYTES 同口径
MAX_LINES = 2000  # read_file 默认行数上限

# 内置命名 provider：`--provider <名字>` 或 `AGENTOS_PROVIDER=<名字>` 选取。
# 加新端点只改这张表，别在调用点再抄一遍 base_url。
NAMED_PROVIDERS = {
    "opencode": {
        "base_url": "https://opencode.ai/zen/go/v1",
        "model": "muse-spark-1.3-contributor",
        "note": "OpenCode zen 网关；需 AGENTOS_LLM_API_KEY（Bearer）",
        # opencode 网关只认 Responses 协议：实测 POST /chat/completions
        # 必回 HTTP 400 ModelProtocolUnsupported「Model does not support
        # this protocol.」，改 /responses 才 200。所以这里写死协议，
        # 不给调用点猜的机会。
        "protocol": "responses",
        # 2026-10-17 实测：本端点现在强制要会话头（不带必回 400
        # MissingSessionID，连 /models 都过不了）。取 uuid4 每次 resolve
        # 注入一个；同一 Provider 实例生命周期内稳定，够做会话亲和。
        "session_header": "x-opencode-session",
    },
}

# 会话中毒词表：opencode 网关有状态路由在 503/断流后，对后续请求
# 回 400「No tool call found for tool output with call_id …」。命中即
# rotate_session 换干净 worker。别把 "call_id" 收窄到具体报错文案之外的
# 形状——网关文案会变，抓稳定词根就够。
_SESSION_BREAK_RE = re.compile(
    r"no tool call found|tool output with call_id|missingsessionid",
    re.I,
)

# Responses 协议的两个固定参数。为什么不塞进 Provider._body_responses 的
# 字面量里：两个值都是**实测**出来的，抽成常量才能在自检里一起断言，也
# 不至于下次调网关时有人随手改坏。
#   max_output_tokens 太小 → status=incomplete 且 output 为空（实测 20 即如此）；
#   4096 足够跑完工具循环；reasoning effort=low 实测把 output 从 659 压到
#   344，正文仍然完整，工具调用不受影响。
# 2026-10-18 真任务实测纠正：4096 被 reasoning 模型烧穿 —— 步内截断成
# 中断句的「伪终稿」，任务没做完还自报 ok。上限提到 16384（responses API
# 上限内），截断本身就是伪终稿的根因，不是模型的错。
RESPONSES_MAX_OUTPUT_TOKENS = 16384
RESPONSES_REASONING_EFFORT = "low"

# 默认 User-Agent。为什么不是 urllib 自带的 "Python-urllib/3.x"：
# Cloudflare 会按 UA 拦（opencode 网关实测 403 error 1010「按浏览器签名
# 封锁」）。要用别的 UA 就经 AGENTOS_LLM_HEADERS 覆盖。
_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class AgentError(Exception):
    """内核级失败。kind 见模块 docstring 的失败分类。"""

    def __init__(self, kind, msg):
        super(AgentError, self).__init__(msg)
        self.kind = kind


def _read_json_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _codex_provider():
    """从 ~/.codex/config.toml 读本机已配好的模型端点。

    为什么不自己发明一套 key 管理：本机已经有 CC Switch 托管的一份配置，
    里面 base_url / http_headers / model 都是真的、正在被 codex 本人使用的。
    agent 复用它 = 用户不必为第二个工具再签一份 key。解析失败就返回 None，
    由调用方决定 fail-closed，不猜、不写死账号。
    """
    p = os.path.join(os.path.expanduser("~"), ".codex", "config.toml")
    try:
        import tomllib

        with open(p, "rb") as f:
            cfg = tomllib.load(f)
    except Exception:
        return None
    name = cfg.get("model_provider")
    prov = (cfg.get("model_providers") or {}).get(name) or {}
    base = (prov.get("base_url") or "").strip()
    if not base:
        return None
    hdrs = {}
    for k, v in (prov.get("http_headers") or {}).items():
        if isinstance(v, str):
            hdrs[str(k)] = v
    return {
        "base_url": base.rstrip("/"),
        "model": cfg.get("model") or "",
        "headers": hdrs,
        "provider_name": str(name or ""),
        "source": "codex-config:%s" % p,
    }


def _agentos_config_path():
    """AgentOS 自己的配置文件路径：~/.agentos/config.toml。

    为什么不复用 ~/.codex/config.toml：那份文件由 CC Switch 托管，用户
    要求不擅自改它。AgentOS 需要自己的配置空间存 API key / 默认 provider
    等设置，互不干扰。
    """
    return os.path.join(os.path.expanduser("~"), ".agentos", "config.toml")


def _read_agentos_config():
    """读 ~/.agentos/config.toml，返回 dict。文件不存在或解析失败返回 {}。

    与 _codex_provider 不同，这里不做 fail-closed 语义 —— 调用方（resolve_provider
    / config 命令）自行决定缺字段时怎么处理。
    """
    p = _agentos_config_path()
    try:
        import tomllib

        with open(p, "rb") as f:
            cfg = tomllib.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _write_agentos_config(cfg):
    """把 dict 写入 ~/.agentos/config.toml（TOML 格式）。

    支持嵌套 dict（写成 [table] 段）和 str/int/float/bool 值。
    写前自动建 ~/.agentos/ 目录。返回 (ok, err_msg)。
    手写简单 TOML writer，不依赖 tomli_w（项目风格：只用标准库）。
    """
    p = _agentos_config_path()
    d = os.path.dirname(p)
    try:
        os.makedirs(d, exist_ok=True)
        lines = []
        tables = {}
        for k, v in cfg.items():
            if isinstance(v, dict):
                tables[k] = v
                continue
            lines.append(_toml_kv(k, v))
        for tname, tval in tables.items():
            if lines:
                lines.append("")
            lines.append("[%s]" % tname)
            for k, v in tval.items():
                if isinstance(v, dict):
                    raise ValueError("不支持嵌套两层以上的表：%s.%s" % (tname, k))
                lines.append(_toml_kv(k, v))
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return True, ""
    except Exception as ex:
        return False, str(ex)


def _toml_kv(k, v):
    """把单个键值对写成 TOML 行。不支持的类型抛 ValueError。"""
    if isinstance(v, bool):
        return "%s = %s" % (k, "true" if v else "false")
    if isinstance(v, (int, float)):
        return "%s = %s" % (k, v)
    if isinstance(v, str):
        escaped = v.replace("\\", "\\\\").replace('"', '\\"')
        return '%s = "%s"' % (k, escaped)
    raise ValueError("不支持的类型：%s=%r" % (k, v))


def _provider_hint(code, raw, base_url=""):
    """端点失败的「求医三件套」：现象（HTTP 码 + body）已在报错里，这里补病灶与药方。

    为什么不在码上自愈：400/401/403/404/429 都不是传输层瞬时错（_TRANSIENT_RE
    只管 TLS/重置/超时那一档），机器也无权替用户造 key、改网关路由、猜协议。
    能做的只有一件事 —— 把下一步该敲哪条命令说全。同一条 err:provider 反复
    出现 5 次，形态全是「裸 400/403/不可达」不带指引，每次都要从头重推诊断。
    分档对齐 openai-python 的 _exceptions.py：状态码各自成类、连接失败另立
    APIConnectionError，不把「网关拒了请求」和「根本连不上」混成一句「端点失败」。
    探活一律给 /models：它把「端点活着但上游/请求体有问题」与「链路根本不通」
    当场分开，是这条教训里唯一真正省时间的判据。
    """
    try:
        code = int(code)
    except (TypeError, ValueError):  # 提示层永不抛错（它本身在错误路径上）
        return ""
    body = raw or ""
    base = (base_url or "<base_url>").rstrip("/")
    probe = (
        "\n  → 先探活：`curl -sS %s/models`（HTTP 200=端点活着，问题在上游/请求体；"
        "连不上=网络/代理）" % base
    )
    if code in (401, 402) or re.search(
        r"api[_ ]?key|auth\b|expires|unauthorized|invalid.*key", body, re.I
    ):
        return (
            "\n  → API key 缺失/失效：`agentos config set llm.api_key <新key>`"
            "（或 AGENTOS_LLM_API_KEY）后重试" + probe
        )
    if code == 403:
        if re.search(
            r"cloudflare|error code:? ?10\d\d|just a moment|attention required|waf",
            body,
            re.I,
        ):
            return (
                "\n  → 403 是 WAF/网关按客户端特征拦的，不是 key：换 UA 或"
                "`AGENTOS_LLM_VIA_PROXY=1` 换路再试" + probe
            )
        return (
            "\n  → 端点拒绝本请求：检查 key 权限/额度（agentos config get llm）"
            + probe
        )
    if code in (400, 422):
        if re.search(r"protocol|not support", body, re.I):
            return (
                "\n  → 协议不对：该端点可能只认 responses，"
                "`AGENTOS_LLM_PROTOCOL=responses` 再试" + probe
            )
        if re.search(r"missing[ _]?session|session.*(required|missing)", body, re.I):
            return (
                "\n  → 网关要会话头而这份请求没带：用 `--provider opencode`"
                "（内核自动注入 x-opencode-session），或 AGENTOS_LLM_HEADERS 手动补"
                + probe
            )
        if re.search(r"call_id|no tool call found|tool output", body, re.I):
            return (
                "\n  → 网关会话脱钩：净荷自洽、坏的是服务端会话 —— 换会话再发"
                "（重启 REPL 或 /branch 出新会话），别拿同一份会话连跪" + probe
            )
        return (
            "\n  → key 多半没问题，是网关路由/请求体：查上游路由与会话头，"
            "或用 AGENTOS_LLM_MODEL 换模型" + probe
        )
    if code == 404:
        return (
            "\n  → 路径或模型名不对：`%s/models` 列可用模型，"
            "再用 AGENTOS_LLM_MODEL=<名> 换模型" % base + probe
        )
    if code == 429:
        return "\n  → 限流/额度：退避后重试，或换 key / 换模型" + probe
    if 500 <= code <= 599:
        return (
            "\n  → 上游 5xx：网关/模型侧过载，退避重试；持续报就用"
            " AGENTOS_LLM_MODEL 换个模型或换 key" + probe
        )
    return ""


_UNREACHABLE_HINT = (
    "\n  → 不可达=网络/代理，不是 key：先 `curl -sS <上面那个端点>/models` 确认链路，"
    "再查代理变量与 AGENTOS_LLM_VIA_PROXY 该不该开；本机后端就查进程在不在"
)


def _bearer_headers(e):
    """AGENTOS_LLM_HEADERS（JSON 对象）+ AGENTOS_LLM_API_KEY → 请求头。

    抽成函数是因为 env 直配与命名 provider 两条路都要用同一套解析；
    抄两遍的那天一定会改一处忘一处。
    """
    hdrs = {}
    raw = e.get("AGENTOS_LLM_HEADERS")
    if raw:
        try:
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError("headers 不是对象")
            hdrs = {str(k): str(v) for k, v in obj.items()}
        except Exception as ex:
            raise AgentError(
                "provider", "AGENTOS_LLM_HEADERS 不是合法 JSON 对象：%s" % ex
            )
    key = (e.get("AGENTOS_LLM_API_KEY") or "").strip()
    if key:
        hdrs.setdefault("Authorization", "Bearer " + key)
    return hdrs


def _norm_protocol(v):
    """把协议名归一成 chat / responses，别的一律抛错，不猜。

    为什么不让未知值静默退回 chat：chat/completions 与 responses 的请求体
    形状完全不同，猜错一次就会把「配错协议」翻译成「模型端点坏了」，两边
    都查不到真因。宁可当场报 provider 错，与 AGENTOS_LLM_HEADERS 收到坏
    JSON 时的处理同风格。
    """
    if v is None or not str(v).strip():
        return "chat"
    p = str(v).strip().lower()
    if p not in ("chat", "responses"):
        raise AgentError("provider", "未知协议：%r（只支持 chat / responses）" % (v,))
    return p


def resolve_provider(env=None):
    """按 命名 provider → env 直配 → 本机离线 → codex 配置的顺序解析端点。
    解析不到抛 no-provider。

    env 六个键（都可由调用方注入，便于门做证伪）：
      AGENTOS_PROVIDER       命名 provider 名（NAMED_PROVIDERS 的键），给了就以它为准
      AGENTOS_LLM_BASE_URL  形如 http://127.0.0.1:15799/v1
      AGENTOS_LLM_MODEL
      AGENTOS_LLM_API_KEY   可选
      AGENTOS_LLM_HEADERS   可选，JSON 对象字符串
      AGENTOS_LLM_PROTOCOL  可选，chat / responses；命名 provider 自己声明过就
                            以命名 provider 为准（opencode 声明 responses）
    （命名 provider 优先于 env 直配：点名要哪个端点，哪个说了算。）

    env 未设 key 时，自动从 ~/.agentos/config.toml 的 [llm] 段读 api_key /
    provider / model，补进 env 再解析。
    """
    e = os.environ if env is None else env
    # 从 ~/.agentos/config.toml 补全 env 未设的字段（仅当 env 未显式设时）
    acfg = _read_agentos_config()
    llm_cfg = acfg.get("llm") or {}
    if not isinstance(llm_cfg, dict):
        llm_cfg = {}
    if env is None:
        if not (e.get("AGENTOS_LLM_API_KEY") or "").strip():
            k = llm_cfg.get("api_key")
            if isinstance(k, str) and k.strip():
                os.environ["AGENTOS_LLM_API_KEY"] = k.strip()
        if not (e.get("AGENTOS_PROVIDER") or "").strip():
            p = llm_cfg.get("provider")
            if isinstance(p, str) and p.strip():
                os.environ["AGENTOS_PROVIDER"] = p.strip()
        if not (e.get("AGENTOS_LLM_MODEL") or "").strip():
            m = llm_cfg.get("model")
            if isinstance(m, str) and m.strip():
                os.environ["AGENTOS_LLM_MODEL"] = m.strip()
    else:
        env = dict(env)
        if not (env.get("AGENTOS_LLM_API_KEY") or "").strip():
            k = llm_cfg.get("api_key")
            if isinstance(k, str) and k.strip():
                env["AGENTOS_LLM_API_KEY"] = k.strip()
        if not (env.get("AGENTOS_PROVIDER") or "").strip():
            p = llm_cfg.get("provider")
            if isinstance(p, str) and p.strip():
                env["AGENTOS_PROVIDER"] = p.strip()
        if not (env.get("AGENTOS_LLM_MODEL") or "").strip():
            m = llm_cfg.get("model")
            if isinstance(m, str) and m.strip():
                env["AGENTOS_LLM_MODEL"] = m.strip()
        e = env
    named = (e.get("AGENTOS_PROVIDER") or "").strip()
    if named:
        spec = NAMED_PROVIDERS.get(named)
        if spec is None:
            raise AgentError(
                "no-provider",
                "未知命名 provider：%s（可选：%s）；或用 "
                "AGENTOS_LLM_BASE_URL 直指端点"
                % (named, " / ".join(sorted(NAMED_PROVIDERS))),
            )
        # 注意：这里**不**注入会话头值。会话 id 必须只归 Provider 一个来源
        # （self.session_id），否则 rotate_session() 换了 id 但真正发出的
        # 头还是 resolve 时钉死的旧 uuid —— 实测 2026-10-18 attrs 真任务
        # 6:25 里自愈全弹在同一个旧会话上，等于没换。表里的 session_header
        # 字段只作声明/自检用。
        _hdr = _bearer_headers(e)
        return {
            "base_url": spec["base_url"].rstrip("/"),
            "model": (e.get("AGENTOS_LLM_MODEL") or spec["model"]).strip(),
            "headers": _hdr,
            # 命名 provider 自己声明的协议优先于 env：opencode 的
            # /chat/completions 实测 400，只能走 responses。
            "protocol": _norm_protocol(
                spec.get("protocol") or e.get("AGENTOS_LLM_PROTOCOL")
            ),
            "provider_name": "named:%s" % named,
            "source": "env:AGENTOS_PROVIDER",
        }
    base = (e.get("AGENTOS_LLM_BASE_URL") or "").strip()
    if base:
        return {
            "base_url": base.rstrip("/"),
            "model": (e.get("AGENTOS_LLM_MODEL") or "").strip(),
            "headers": _bearer_headers(e),
            "protocol": _norm_protocol(e.get("AGENTOS_LLM_PROTOCOL")),
            "provider_name": "env",
            "source": "env:AGENTOS_LLM_BASE_URL",
        }
    # 本机离线后端：显式开 AGENTOS_LLM_LOCAL=ollama 才认本机 Ollama 的
    # OpenAI 兼容口。为什么不自动探测：本机常年跑着 Ollama，自动探测会把
    # neg_agent_core 的「空 HOME 必须抛 no-provider」判据变红 —— 那等于把
    # 「没配端点」谎成「有端点」。要离线后端就自己开这个开关。
    if (e.get("AGENTOS_LLM_LOCAL") or "").strip().lower() in (
        "ollama",
        "1",
        "yes",
        "true",
    ):
        return {
            "base_url": "http://127.0.0.1:11434/v1",
            "model": (e.get("AGENTOS_LLM_MODEL") or "qwen3:0.6b").strip(),
            "headers": {},
            "protocol": "chat",
            "provider_name": "ollama-local",
            "source": "env:AGENTOS_LLM_LOCAL",
        }
    cp = _codex_provider()
    if cp:
        return cp
    raise AgentError(
        "no-provider",
        "解析不到模型端点：AGENTOS_LLM_BASE_URL 未设，且 ~/.codex/config.toml "
        "里的 model_providers 没有可用 base_url。设一个再跑，别让 agent "
        "假装有模型。",
    )


def _probe_ollama(timeout=1.5):
    """探测本机 Ollama 的 OpenAI 兼容口是否真能用。

    只在**主端点已经失败**的回退路径里调，绝不进 resolve_provider 的默认
    顺序 —— 本机常年跑着 Ollama，自动探测会把「没配端点」谎成「有端点」，
    正是 neg_agent_core 的空 HOME 判据在治的那种假绿。
    可用返回一份 cfg dict（形同 resolve_provider 的返回），否则 None。
    只读 /v1/models，不落盘、不联网（仅回环）。
    """
    base = "http://127.0.0.1:11434/v1"
    try:
        req = urllib.request.Request(base + "/models", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(65536)
        obj = json.loads(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001  没跑/没模型都算不可用
        return None
    data = obj.get("data") if isinstance(obj, dict) else None
    names = (
        [str(m.get("id") or "").strip() for m in data if isinstance(m, dict)]
        if isinstance(data, list)
        else []
    )
    names = [n for n in names if n]
    if not names:
        return None
    model = "qwen3:0.6b" if "qwen3:0.6b" in names else names[0]
    return {
        "base_url": base,
        "model": model,
        "headers": {},
        "protocol": "chat",
        "provider_name": "ollama-local",
        "source": "fallback:ollama",
    }


# ── Responses 协议：请求体 / 响应归一化 ────────────────────────
def _as_text(v):
    """把任意 content / 参数统一成字符串（Responses 边的段值形状不固定）。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, dict):
        for k in ("text", "content", "output"):
            t = v.get(k)
            if isinstance(t, str):
                return t
    return json.dumps(v, ensure_ascii=False)


def _norm_responses_usage(u):
    """Responses 的 usage 键名（input/output_tokens）映射回
    prompt/completion_tokens —— _usage_total 与自检认后者这套名字。

    实测键名：{"input_tokens":..,"output_tokens":..,"total_tokens":..,
    "output_tokens_details":{"reasoning_tokens":..}}。映射不上就返回 {}，
    让调用方按「端点没报」处理，不拿 0 冒充真数字。
    """
    if not isinstance(u, dict):
        return {}
    out = {}
    for src, dst in (
        ("input_tokens", "prompt_tokens"),
        ("output_tokens", "completion_tokens"),
        ("total_tokens", "total_tokens"),
    ):
        v = u.get(src)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[dst] = int(v)
    return out


def _to_responses_input(messages):
    """chat 形状的 messages → Responses 的 input[] 数组。

    只译「chat 有、responses 没有」的那几样，其余原样透传 —— 实测
    （tools/probe_body.json，HTTP 200 completed）证明
    {"role":"system|user|assistant","content":"字符串"} 网关照收，不必包成
    content[] 段数组。真正要译的是：
      assistant.tool_calls → {"type":"function_call", call_id, name, arguments}
      role=tool            → {"type":"function_call_output", call_id, output}
    论据：不译的话多步工具循环第二步就会被网关判形状不对，那是本协议下
    最容易踩的坑。
    """
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "").strip()
        if role == "tool":
            out.append(
                {
                    "type": "function_call_output",
                    "call_id": str(m.get("tool_call_id") or ""),
                    "output": _as_text(m.get("content")),
                }
            )
            continue
        if role == "assistant":
            # 空正文就不出 message item：纯工具调用的那一轮没有可说的，
            # 出个空 message 只会让网关多一个空转轮次。
            body = _as_text(m.get("content"))
            if body:
                out.append({"role": "assistant", "content": body})
            for tc in m.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                if not isinstance(args, str):
                    args = json.dumps(
                        args if args is not None else {}, ensure_ascii=False
                    )
                out.append(
                    {
                        "type": "function_call",
                        "call_id": str(tc.get("id") or ""),
                        "name": str(fn.get("name") or "").strip(),
                        "arguments": args,
                    }
                )
            continue
        out.append({"role": role or "user", "content": _as_text(m.get("content"))})
    return out


def _to_responses_tools(tools):
    """chat 的嵌套 tools 形状 → Responses 的扁平形状。

    chat:  {"type":"function","function":{"name":..,"parameters":{..}}}
    responses: {"type":"function","name":..,"parameters":{..},"strict":false}
    strict 显式写 false：本项目的工具参数不带全量 JSON Schema 约束，写 true
     会让网关以严格模式校验并拒掉合法调用（shape 依据见 probe_body.json）。
    """
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if "name" in t and "function" not in t:
            out.append(t)  # 已经是扁平形状，别二次包装
            continue
        fn = t.get("function") or {}
        out.append(
            {
                "type": "function",
                "name": str(fn.get("name") or "").strip(),
                "description": str(fn.get("description") or ""),
                "parameters": fn.get("parameters")
                or {"type": "object", "properties": {}},
                "strict": False,
            }
        )
    return out


def _norm_responses(obj):
    """把一次 responses 响应归一成与 _norm_completion 相同的形状。

    形状不对、或者既无正文又无工具调用，就抛 provider 错 —— 实测
    max_output_tokens 给太小会拿到 status=incomplete、output:[]，那种
    响应必须当失败，绝不能洗成「模型说完了」。
    """
    if not isinstance(obj, dict):
        raise AgentError("provider", "responses 响应不是 JSON 对象")
    status = str(obj.get("status") or "").strip()
    if status == "incomplete" or obj.get("incomplete_details"):
        raise AgentError(
            "provider",
            "responses 响应不完整（status=%s，"
            "incomplete_details=%s）：max_output_tokens"
            " 可能太小，不猜内容"
            % (
                status or "-",
                json.dumps(obj.get("incomplete_details"), ensure_ascii=False),
            ),
        )
    if status == "failed" or obj.get("error"):
        err = obj.get("error")
        raise AgentError(
            "provider",
            "responses 响应失败：%s"
            % (
                json.dumps(err, ensure_ascii=False)[:300]
                if err is not None
                else status or "-"
            ),
        )
    output = obj.get("output")
    if not isinstance(output, list):
        raise AgentError(
            "provider", "responses 响应里没有 output 数组（形状不对，不猜）"
        )
    content = []
    calls = []
    for i, item in enumerate(output):
        if not isinstance(item, dict):
            continue
        it = str(item.get("type") or "").strip()
        if it == "message":
            for seg in item.get("content") or []:
                if isinstance(seg, dict) and str(seg.get("type") or "") in (
                    "output_text",
                    "text",
                    "refusal",
                    "",
                ):
                    t = seg.get("text")
                    if isinstance(t, str) and t:
                        content.append(t)
                elif isinstance(seg, str) and seg:
                    content.append(seg)
        elif it == "function_call":
            args = item.get("arguments")
            if not isinstance(args, str):
                args = json.dumps(args if args is not None else {}, ensure_ascii=False)
            calls.append(
                {
                    "id": str(item.get("call_id") or ("call_%d" % i)),
                    "type": "function",
                    "function": {
                        "name": str(item.get("name") or "").strip(),
                        "arguments": args,
                    },
                }
            )
        # reasoning 项刻意忽略：推理过程不是给用户看的正文，也不是工具调用。
    if not content and not calls:
        raise AgentError(
            "provider",
            "responses 响应里既无正文也无工具调用（status=%s）：不猜" % (status or "-"),
        )
    return {
        "content": "".join(content),
        "tool_calls": calls,
        "finish_reason": "tool_calls" if calls else "stop",
        "usage": _norm_responses_usage(obj.get("usage")),
    }


# ── 响应归一化 / 调用层 ───────────────────────────────────────
def _norm_completion(obj):
    """把一次 chat/completions 响应归一成 {content, tool_calls, ...}。

    形状不对就抛 provider 错，绝不猜：猜一次就等于把「端点坏了」翻译成
    「模型说完了」，正是本项目治了一路的假绿。
    """
    if not isinstance(obj, dict):
        raise AgentError("provider", "响应不是 JSON 对象")
    choices = obj.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AgentError("provider", "响应里没有 choices（形状不对，不猜）")
    first = choices[0] if isinstance(choices[0], dict) else {}
    msg = first.get("message")
    if not isinstance(msg, dict):
        raise AgentError("provider", "choices[0].message 不是对象")
    calls = []
    raw = msg.get("tool_calls") or []
    for i, tc in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function")
        fn = fn if isinstance(fn, dict) else {}
        args = fn.get("arguments")
        if not isinstance(args, str):
            # 有的实现直接给 dict；统一成字符串，主循环按字符串解析。
            args = json.dumps(args if args is not None else {}, ensure_ascii=False)
        calls.append(
            {
                "id": str(tc.get("id") or ("call_%d" % i)),
                "type": "function",
                "function": {
                    "name": str(fn.get("name") or "").strip(),
                    "arguments": args,
                },
            }
        )
    content = msg.get("content")
    usage = obj.get("usage")
    return {
        "content": content if isinstance(content, str) else "",
        "tool_calls": calls,
        "finish_reason": first.get("finish_reason"),
        "usage": usage if isinstance(usage, dict) else {},
    }


class Provider(object):
    """一个模型端点。只管发请求、拿结果、分类失败。

    支持两种协议（self.protocol）：chat = OpenAI chat/completions，
    responses = OpenAI Responses。两者的请求体与响应形状都不同，但
    chat() 对外的返回形状统一，所以上层 run_agent 不必知道协议。
    """

    def __init__(self, cfg):
        self.base_url = str(cfg.get("base_url") or "").rstrip("/")
        self.model = str(cfg.get("model") or "").strip()
        self.headers = dict(cfg.get("headers") or {})
        self.provider_name = str(cfg.get("provider_name") or "")
        self.source = str(cfg.get("source") or "")
        # 协议决定 URL / 请求体 / 响应解析三条路，故而在构造时就定死，
        # 别让每次调用再判一次。
        self.protocol = _norm_protocol(cfg.get("protocol"))
        # 网关侧会话 id：opencode 实测漏带 x-opencode-session 就 HTTP 400
        # MissingSessionID。一个 Provider 一个 id，多步工具循环的多次请求
        # 落在同一会话里，网关路由/缓存才对得上。
        self.session_id = str(
            cfg.get("session_id") or ("agentos-%s" % uuid.uuid4().hex[:16])
        )
        self.n_calls = 0
        # 传输层双出口状态：direct（默认，不走系统代理）与 via-proxy（clash
        # 稳定 mixed 口）。SSL EOF 连续 2 次自动降路到 proxy —— 同主端点同
        # 净荷，只换出口，不是模型/端点降级。
        self._proxy_mode = False
        self._ssl_fail_n = 0

    def rotate_session(self):
        """换一个网关会话 id（原子性：一次调用换一次）。

        为什么需要：opencode 网关按 x-opencode-session 做有状态路由，
        服务端 503/断流后 worker 会话日志会与客户端净荷脱钩，之后每一发
        都回 400「No tool call found for tool output with call_id …」——
        净荷本身是自洽的（我们整条历史全量重发），坏的是服务端会话。
        实测 2026-10-18 markdown-it-py 真任务两连跪都是这个形状。轮换
        会话 = 换到干净 worker，按净荷重新校验，是唯一的自救出口。
        """
        self.session_id = "agentos-%s" % uuid.uuid4().hex[:16]
        return self.session_id

    def _send(self, req, timeout):
        """发请求（双出口择路）。

        direct 连续 2 次 SSL EOF → 全面降路 via-proxy；成功即清计数。经
        proxy 后仍 EOF 不回荡（proxy 是最后一条路），如实抛给上层重试。
        """
        if (os.environ.get("AGENTOS_LLM_VIA_PROXY") or "").strip():
            return urllib.request.urlopen(req, timeout=timeout)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            resp = opener.open(req, timeout=timeout)
            self._ssl_fail_n = 0
            return resp
        except urllib.error.URLError as _ue:
            _why = str(getattr(_ue, "reason", _ue))
            if "SSL" in _why and "EOF" in _why and self._proxy_mode is False:
                # 第一次 TLS 抖就换路：链路断窗里赌第二次=必然死（实测）。
                self._ssl_fail_n += 1
                self._proxy_mode = True
                return urllib.request.urlopen(req, timeout=timeout)
            raise

    # -- 底层 HTTP -----------------------------------------------------
    def _open(self, body, stream):
        # 端点路径按协议分叉：Responses 走 /responses，chat 走
        # /chat/completions。写死两条而不是拼接后缀，是为了让「这个端点
        # 到底连哪」在代码里一眼可见。
        url = self.base_url + (
            "/responses" if self.protocol == "responses" else "/chat/completions"
        )
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "text/event-stream" if stream else "application/json")
        # 默认浏览器 UA（见 _DEFAULT_UA 的注释）；下面循环里的显式
        # headers 后加，仍然盖过它。
        req.add_header("User-Agent", _DEFAULT_UA)
        # 会话头放在用户 headers **之前**加：这样 AGENTOS_LLM_HEADERS 里
        # 显式写的 x-opencode-session 仍然能盖过它（后加者胜）。
        if self.session_id:
            req.add_header("x-opencode-session", self.session_id)
        for k, v in self.headers.items():
            if str(k).lower() in ("content-length", "host", "accept-encoding"):
                continue  # 这三个由 urllib 自己管
            req.add_header(str(k), str(v))
        try:
            return self._send(req, CALL_TIMEOUT)
        except urllib.error.HTTPError as e:
            raw = b""
            try:
                raw = e.read(1024)
            except Exception:  # noqa: BLE001
                pass
            raise AgentError(
                "provider",
                "模型端点 HTTP %s：%s%s"
                % (
                    e.code,
                    raw.decode("utf-8", "replace")[:300],
                    _provider_hint(
                        e.code, raw.decode("utf-8", "replace"), self.base_url
                    ),
                ),
            )
        except urllib.error.URLError as e:
            raise AgentError(
                "provider",
                "模型端点不可达：%s（端点 %s）%s"
                % (
                    getattr(e, "reason", e),
                    self.base_url or "<未配置 base_url>",
                    _UNREACHABLE_HINT,
                ),
            )
        except Exception as e:  # noqa: BLE001  socket.timeout 等
            raise AgentError(
                "provider", "模型调用失败：%s: %s" % (type(e).__name__, str(e)[:160])
            )

    # -- 对外入口 -----------------------------------------------------
    def chat(self, messages, tools=None, stream=False, on_event=None, cancel=None):
        """跑一次对话。返回 {content, tool_calls, finish_reason, usage}。

        键与协议无关：chat 与 responses 各自在 _body_* / _norm_* 里折算成
        同一套名字，所以上层 run_agent 不知道也不需要知道协议。

        stream=True 时按 SSE 逐行解析，边收边通过 on_event 抛
        {"type": "delta", "text": ...}；tool_calls 的 delta 按 index（chat）
        或 item_id（responses）累积 —— arguments 常常被切成好几段，不累积
        就等于丢参数。
        """
        if not self.model:
            raise AgentError("usage", "provider 没给 model 名（%s）" % self.source)
        is_resp = self.protocol == "responses"
        if is_resp:
            body = self._body_responses(messages, tools, stream)
        else:
            body = {"model": self.model, "messages": messages, "stream": bool(stream)}
            if tools:
                body["tools"] = tools
                body["tool_choice"] = "auto"
        self.n_calls += 1
        if stream:
            resp = self._open(body, True)
            try:
                return (
                    self._read_sse_responses(resp, on_event, cancel)
                    if is_resp
                    else self._read_sse(resp, on_event, cancel)
                )
            except AgentError as e:
                # 端点不支持流式（把整包 JSON 当非 data: 行吐回来）时，如实
                # 退化成整包请求重发一次 —— 这正是 --stream 帮助里写死的
                # 语义。真·空流也走这条；重试非流式仍空就照原样抛，不洗绿。
                if "流式响应为空：" not in str(e):
                    raise
                self.n_calls += 1
                resp2 = self._open(dict(body, stream=False), False)
                try:
                    raw2 = resp2.read()
                finally:
                    try:
                        resp2.close()
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    obj2 = json.loads(raw2.decode("utf-8", "replace"))
                except ValueError:
                    raise e
                out = _norm_responses(obj2) if is_resp else _norm_completion(obj2)
                out["degraded_from_stream"] = True
                return out
        resp = self._open(body, False)
        try:
            raw = resp.read()
        except Exception as e:  # noqa: BLE001
            raise AgentError("provider", "读响应失败：%s" % str(e)[:160])
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise AgentError("provider", "响应不是 JSON：%r" % raw[:200])
        return _norm_responses(obj) if is_resp else _norm_completion(obj)

    def _read_sse(self, resp, on_event, cancel=None):
        content = []
        slots = {}  # index -> {"id","name","args":[]}
        finish = None
        got_done = False
        try:
            for raw in resp:
                if cancel is not None and cancel():
                    raise AgentError("interrupted", "已打断（Esc）")
                line = raw.decode("utf-8", "replace").strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    got_done = True
                    break
                try:
                    obj = json.loads(payload)
                except ValueError:
                    continue  # 心跳/注释行，不猜
                if not isinstance(obj, dict):
                    continue
                ch = obj.get("choices")
                if not isinstance(ch, list) or not ch:
                    continue
                first = ch[0] if isinstance(ch[0], dict) else {}
                delta = first.get("delta")
                delta = delta if isinstance(delta, dict) else {}
                piece = delta.get("content")
                if isinstance(piece, str) and piece:
                    content.append(piece)
                    if callable(on_event):
                        on_event({"type": "delta", "text": piece})
                for tc in delta.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    idx = tc.get("index")
                    try:
                        idx = int(idx)
                    except Exception:  # noqa: BLE001
                        idx = len(slots)
                    slot = slots.setdefault(idx, {"id": "", "name": "", "args": []})
                    if tc.get("id"):
                        slot["id"] = str(tc["id"])
                    fn = tc.get("function")
                    fn = fn if isinstance(fn, dict) else {}
                    nm = fn.get("name")
                    if isinstance(nm, str) and nm:
                        slot["name"] += nm
                    arg = fn.get("arguments")
                    if isinstance(arg, str) and arg:
                        slot["args"].append(arg)
                finish = first.get("finish_reason") or finish
        except AgentError:
            raise  # interrupted 等语义错误原样穿透，
            # 不许被包装成 provider 失败触发回退
        except Exception as e:  # noqa: BLE001
            raise AgentError(
                "provider", "流式读取中断：%s: %s" % (type(e).__name__, str(e)[:160])
            )
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
        if not content and not slots:
            # 没收到 [DONE] 且一个字节内容都没有：端点吐了空流，不当成功。
            # 前缀「流式响应为空：」必须与 responses 侧逐字一致：chat() 靠
            # 这个子串判「要不要退化成整包重发」，两侧消息不一致就会有一侧
            # 永远拿不到回退机会。
            raise AgentError(
                "provider",
                "流式响应为空：既无 content 也无 tool_calls（done=%s）" % got_done,
            )
        calls = []
        for idx in sorted(slots):
            s = slots[idx]
            calls.append(
                {
                    "id": s["id"] or ("call_%d" % idx),
                    "type": "function",
                    "function": {
                        "name": s["name"],
                        "arguments": "".join(s["args"]) or "{}",
                    },
                }
            )
        return {
            "content": "".join(content),
            "tool_calls": calls,
            "finish_reason": finish,
            "usage": {},
            "sse": True,
            "sse_done": got_done,
        }

    # -- Responses 协议 -------------------------------------------------
    def _body_responses(self, messages, tools, stream):
        """chat 形状的入参 → responses 请求体。

        max_output_tokens / reasoning 取模块常量而不是字面量：两个值都是
        实测出来的（见 RESPONSES_MAX_OUTPUT_TOKENS 上的注释），写在这里
        自检才能一起断言。
        """
        body = {
            "model": self.model,
            "input": _to_responses_input(messages),
            "stream": bool(stream),
            "max_output_tokens": RESPONSES_MAX_OUTPUT_TOKENS,
            "reasoning": {"effort": RESPONSES_REASONING_EFFORT},
        }
        if tools:
            body["tools"] = _to_responses_tools(tools)
            body["tool_choice"] = "auto"
        return body

    def _read_sse_responses(self, resp, on_event, cancel=None):
        """解析 Responses 的 SSE 事件流，返回与 _norm_responses 同一形状。

        实测事件序列：response.created → response.output_item.added（工具项
        已带 name/call_id）→ response.output_text.delta（正文增量）→
        response.function_call_arguments.delta（按 item_id 累积的参数增量）→
        response.function_call_arguments.done → response.completed（内嵌完整
        response 对象）。

        优先用 completed 里内嵌的 response 走 _norm_responses —— 那是端点
        自己汇总的权威结果；只有流被掐断、没等到终态事件时，才拿累积的
        slot 拼一份，并据实标记 sse_done=False。
        """
        content = []
        slots = {}  # item_id -> {"id","name","args":[]}
        saw_items = False
        final = None
        try:
            for raw in resp:
                if cancel is not None and cancel():
                    raise AgentError("interrupted", "已打断（Esc）")
                line = raw.decode("utf-8", "replace").strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except ValueError:
                    continue  # 心跳/注释行，不猜
                if not isinstance(obj, dict):
                    continue
                etype = str(obj.get("type") or "").strip()
                if etype == "response.output_text.delta":
                    piece = obj.get("delta")
                    if isinstance(piece, str) and piece:
                        content.append(piece)
                        if callable(on_event):
                            on_event({"type": "delta", "text": piece})
                elif etype == "response.output_item.added":
                    item = obj.get("item")
                    if (
                        isinstance(item, dict)
                        and str(item.get("type") or "") == "function_call"
                    ):
                        iid = str(item.get("id") or "")
                        slot = slots.setdefault(
                            iid,
                            {
                                "id": str(item.get("call_id") or ""),
                                "name": str(item.get("name") or ""),
                                "args": [],
                            },
                        )
                        saw_items = True
                elif etype == "response.function_call_arguments.delta":
                    iid = str(obj.get("item_id") or "")
                    piece = obj.get("delta")
                    slot = slots.setdefault(iid, {"id": "", "name": "", "args": []})
                    if isinstance(piece, str) and piece:
                        slot["args"].append(piece)
                elif etype in (
                    "response.completed",
                    "response.incomplete",
                    "response.failed",
                ):
                    r = obj.get("response")
                    if isinstance(r, dict):
                        final = r
                    break
        except AgentError:
            raise  # interrupted 等语义错误原样穿透，
            # 不许被包装成 provider 失败触发回退
        except Exception as e:  # noqa: BLE001
            raise AgentError(
                "provider", "流式读取中断：%s: %s" % (type(e).__name__, str(e)[:160])
            )
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
        if final is not None:
            out = _norm_responses(final)
            out["sse"] = True
            out["sse_done"] = True
            return out
        if not content and not slots:
            # 连终态事件都没等到、一个字节内容也没有：空流，不当成功。
            raise AgentError(
                "provider",
                "流式响应为空：responses 事件流里"
                "既无正文也无函数调用"
                "（done=%s）" % saw_items,
            )
        calls = []
        for iid in sorted(slots):
            s = slots[iid]
            calls.append(
                {
                    "id": s["id"] or ("call_%s" % iid),
                    "type": "function",
                    "function": {
                        "name": s["name"],
                        "arguments": "".join(s["args"]) or "{}",
                    },
                }
            )
        return {
            "content": "".join(content),
            "tool_calls": calls,
            "finish_reason": "tool_calls" if calls else "stop",
            "usage": {},
            "sse": True,
            "sse_done": False,
        }


# ── 本地桩端点（自检 / 反向门用；不碰真网、不碰真模型） ─────────
class Stub(object):
    """只在 127.0.0.1 临时端口听的桩服务，按请求次序回脚本。

    为什么要一个真 HTTP 桩而不是内存假对象：Provider 的 URL 拼装、请求体
    序列化、SSE 分行解析、非 200 归类全是真代码路径，用假对象替掉它们等于
    没验。桩只绑回环、随端口随机、server_close 即消失，不落盘不联网。
    """

    def __init__(self, script):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.script = list(script)
        self.seen = []
        self._lock = threading.Lock()
        stub = self

        class _H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # noqa: N802  桩不说话
                return

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8", "replace") or "{}")
                except ValueError:
                    body = {}
                with stub._lock:
                    stub.seen.append(body)
                    idx = len(stub.seen) - 1
                entry = stub.script[idx] if idx < len(stub.script) else stub.script[-1]
                status = int(entry.get("status", 200))
                if "sse" in entry:
                    payload = str(entry["sse"]).encode("utf-8")
                    ctype = "text/event-stream"
                else:
                    payload = json.dumps(entry.get("json"), ensure_ascii=False).encode(
                        "utf-8"
                    )
                    ctype = "application/json"
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except Exception:  # noqa: BLE001  客户端已断
                    pass

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self._srv.daemon_threads = True
        self.port = self._srv.server_address[1]
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._t.start()

    def base(self):
        return "http://127.0.0.1:%d/v1" % self.port

    def provider(self, model="stub-model", protocol="chat", session_id=None):
        """造一个指向本桩的 Provider。

        protocol 默认 chat 是**刻意**的：绝大多数自检用例钉的是
        chat/completions 形状，把默认值改成 responses 会让它们全部悄悄换个
        协议，测的就不是原来那条路径了。要测 responses 就显式传。
        """
        cfg = {
            "base_url": self.base(),
            "model": model,
            "headers": {},
            "provider_name": "stub",
            "source": "stub:%d" % self.port,
        }
        if protocol:
            cfg["protocol"] = protocol
        if session_id:
            cfg["session_id"] = session_id
        return Provider(cfg)

    def close(self):
        for fn in (self._srv.shutdown, self._srv.server_close):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass


# ── 路径沙箱（与 serve.py 同一套边界，不新开一套真相） ──────────
def _sandbox(rel, root=None):
    """把相对路径解析成 root 内的真实路径；越界返回 (None, 理由)。

    为什么必须 realpath 之后再比前缀：os.path.join + startswith 挡不住
    `work/../../etc/passwd`，也挡不住指向 root 外的符号链接。先 realpath
    再比 `root + os.sep` 才是唯一可靠的边界 —— 与 serve.py._fs_sandbox 同款。
    """
    rroot = os.path.realpath(root or ROOT)
    p = (rel or "").strip()
    if not p or p == ".":
        return rroot, None
    real = os.path.realpath(os.path.join(rroot, p))
    if real != rroot and not real.startswith(rroot + os.sep):
        return None, (
            "路径越界：只能动当前项目根内的路径（项目根=%s，收到 %r）" % (rroot, p[:80])
        )
    return real, None


def _sha(path):
    if not path or not os.path.isfile(path):
        return None
    import hashlib

    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for blk in iter(lambda: f.read(65536), b""):
                h.update(blk)
    except OSError:
        return None
    return h.hexdigest()


def _is_loopback(host):
    h = (host or "").strip().lower().rstrip(".")
    return h in ("127.0.0.1", "localhost", "::1", "[::1]") or h.startswith("127.")


def _host_of(url):
    try:
        return urllib.parse.urlparse(str(url or "")).hostname or ""
    except Exception:  # noqa: BLE001
        return ""


# ── 权限模型 ──────────────────────────────────────────────────
RISK_READ = "read"
RISK_WRITE = "write"
MODES = ("plan", "accept-edits", "full")

# 任何模式、任何审批者都不能放行的命令。理由见模块 docstring：
# ① 对外动作要用户本人说「发」；② 破坏性删除不在 agent 的能力面里。
HARD_DENY_HEAD = {
    "rm",
    "rmdir",
    "dd",
    "mkfs",
    "mkfs.ext4",
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "sudo",
    "launchctl",
    "chown",
    "visudo",
    "fdisk",
    "diskutil",
    "csrutil",
    "pkill",
    "killall",
    "shred",
    "truncate",
}
HARD_DENY_GIT_SUB = {"push", "reset", "clean", "filter-branch", "update-ref"}
# 白名单里唯一能写盘的一条（与 serve.py term_run 的判断同源）
_WRITEISH_CMDS = {"python3"}
# 自由命令面下要拆 -c 载荷再扫的 shell 包装（bash -c "rm -rf x" 一类）
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}


def _payload_deny(payload):
    """shell -c 载荷拆词后按同一张硬阻断清单再扫一遍（尽力而为）。

    拆词扫单词，不做语法分析：`grep rm file` 这种「rm 出现在参数位」会被
    误拦 —— 换个写法即可，方向要的是 fail-closed。这是防手滑的兜底，
    不防蓄意对抗（真对抗的防线是审批与快照）。
    """
    words = [w for w in re.split(r"[^A-Za-z0-9_./\-]+", str(payload).lower()) if w]
    for i, w in enumerate(words):
        b = os.path.basename(w)
        if b in HARD_DENY_HEAD:
            return "硬阻断：shell 载荷里的 %s（破坏性命令不外包执行）" % b
        if b == "git" and i + 1 < len(words) and words[i + 1] in HARD_DENY_GIT_SUB:
            return (
                "硬阻断：shell 载荷里的 git %s（远端写/历史重写，"
                "需用户明确说「发」）" % words[i + 1]
            )
    bases = [os.path.basename(w) for w in words]
    if "find" in bases and "-delete" in words:
        return "硬阻断：shell 载荷里的 find -delete（删文件一律人工确认）"
    return None


def _hard_deny(argv):
    """命中硬阻断清单就回一句**指名道姓**的拒绝理由，否则 None。

    头词按 basename 判（/bin/rm 与 rm 同罪——自由面下绝对路径可达）；
    前缀赋值（FOO=1 rm…）剥掉再判；env 递归剥一层；bash -c 的载荷进
    _payload_deny。任何模式、任何审批、任何开关都不放行这一清单。
    """
    if not isinstance(argv, list) or not argv:
        return None
    low = [str(x).lower() for x in argv]
    while low and re.match(r"^[a-z_][a-z0-9_]*=", low[0]):
        low.pop(0)
    if not low:
        return None
    head = os.path.basename(low[0])
    if head in HARD_DENY_HEAD:
        return (
            "硬阻断：%s 不在任何模式、任何审批下放行"
            "（对外/破坏性动作由用户本人执行）" % head
        )
    if head == "git" and len(low) > 1 and os.path.basename(low[1]) in HARD_DENY_GIT_SUB:
        return "硬阻断：git %s（远端写/历史重写，需用户明确说「发」）" % low[1]
    if head == "find" and "-delete" in low:
        return "硬阻断：find -delete（删文件一律人工确认）"
    if head == "env" and len(low) > 1:
        return _hard_deny(low[1:])
    if head in _SHELLS and len(low) > 2 and low[1] == "-c":
        return _payload_deny(low[2])
    return None


def _gate_write(name, args, mode, appr, why, auto=False):
    """写动作的裁决。返回 None = 放行，字符串 = 拒绝理由。"""
    if mode == "plan":
        return (
            "plan 模式下不执行任何写操作（%s）；要执行请切 "
            "--mode accept-edits 或 full" % why
        )
    if mode == "full":
        return None  # 用户已一次性授权；硬阻断清单仍拦在前面
    if auto:
        # accept-edits 的本义：文件编辑自动放行，其余仍要审批
        return None
    if callable(appr):
        try:
            yes = bool(appr(name, args, why))
        except Exception as e:  # noqa: BLE001
            return "审批器异常，按拒绝处理：%s" % str(e)[:120]
        if not yes:
            return (
                "审批未通过：%s 未执行（这不是失败，是既定行为；"
                "回答里请如实写「未执行」）" % why
            )
        return None
    return (
        "accept-edits 模式下这一步需要审批，但没有审批者"
        "（headless 无 TTY）→ 按拒绝处理：%s" % why
    )


def _safety_flags(acfg):
    """~/.agentos/config.toml 的 [agent] 段 → 安全闸开关（纯函数，W10 自由面）。

    默认**自由面**：以用户要求为中心，全流程不摆无谓的闸。三道**监督闸**
    只在老板显式上锁时生效（他的机器他做主——往哪个方向做主都是他做主）：
      agent.plan_lock = true  → 计划锁开（未审批写锁 + 任务白名单）
      agent.mode_gate = true  → 权限模式开（plan 禁写 / accept-edits 的
                                审批询问 / 写命令与出网裁决）
      agent.cmd_gate  = true  → 命令白名单开（run_cmd 收窄为只读 13 条）
    只有**字面布尔 True** 才上锁（"true"/"1"/"yes" 字符串一律不认——配置里
    一个字符串就能悄悄上锁或解锁，是 SEC E1 堵过的同类洞）；缺键/其它值
    = 关。**硬阻断清单（rm / git push / git reset / find -delete / dd）不在
    开关范围**：那防的是模型的手滑（含路径形态与 shell 载荷），不是老板的
    决定。
    """
    a = (acfg or {}).get("agent") or {}
    return {
        "plan_lock_off": a.get("plan_lock") is not True,
        "mode_gate_off": a.get("mode_gate") is not True,
        "cmd_gate_off": a.get("cmd_gate") is not True,
    }


def _full_access(acfg):
    """~/.agentos/config.toml [agent] full_access=true → 破坏性清单放行。
    缺省关。只有字面布尔 True 才开（字符串不认，与三闸同口径）。"""
    a = (acfg or {}).get("agent") or {}
    return a.get("full_access") is True


def _audit_destructive(ctx, argv):
    """破坏性放行留痕（增益层，写失败静默）。"""
    try:
        d = os.path.join(ROOT, "work")
        os.makedirs(d, exist_ok=True)
        with open(
            os.path.join(d, "destructive_audit.jsonl"), "a", encoding="utf-8"
        ) as f:
            f.write(
                json.dumps(
                    {
                        "ts": round(time.time(), 3),
                        "sid": str(ctx.get("sid") or ""),
                        "mode": str(ctx.get("mode") or ""),
                        "argv": [str(x)[:120] for x in (argv or [])],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
    except OSError:
        pass


def _authorize(spec, args, ctx):
    """None = 放行；字符串 = 拒绝理由（话说全，别让模型去猜）。"""
    name = spec["name"]
    # 子代理工具子集硬闸（W2，fail-closed）：子集写在子 ctx 上；schema 层
    # 过滤只是给模型看的提示，这里才是结构性强制 —— 手搓工具名也出不去。
    subset = ctx.get("tool_subset")
    if isinstance(subset, list) and name not in subset:
        return "工具 %s 不在本次子代理的工具子集里（允许：%s）" % (
            name,
            " ".join(str(s) for s in subset),
        )
    mode = ctx.get("mode") or "full"
    appr = ctx.get("approver")
    if name == "run_cmd":
        argv = args.get("argv")
        if isinstance(argv, str) and argv.strip():
            try:
                argv = shlex.split(argv)
                args["argv"] = argv  # 模型偶发传字符串：按 shell 词法接纳
            except ValueError:
                pass
        why = _hard_deny(argv)
        if why and ctx.get("full_access"):
            _audit_destructive(ctx, argv)  # 本机显式全权：放行但留痕
            why = None
        if why:
            return why
        if not isinstance(argv, list) or not argv:
            return "run_cmd 的 argv 必须是非空数组（第一项是命令名）"
        if not ctx.get("cmd_gate_off"):
            # 显式命令白名单（agent.cmd_gate=true）：只读 13 条，其余拒。
            cmds = _term_cmds()
            if str(argv[0]) not in cmds:
                return "命令不在只读白名单里：%s（可跑：%s）" % (
                    str(argv[0])[:40],
                    " ".join(sorted(cmds)),
                )
            if str(argv[0]) not in _WRITEISH_CMDS:
                return None
        # 自由命令面（默认）或白名单里的写命令（python3）：按写审批裁决 ——
        # 显式 mode_gate 时问审批（headless 无审批者=拒），默认自由面放行。
        if ctx.get("mode_gate_off"):
            return None
        return _gate_write(name, args, mode, appr, "命令（%s）" % argv[0])
    if name == "web_fetch":
        host = _host_of(args.get("url"))
        if not host or _is_loopback(host):
            return None
        if ctx.get("mode_gate_off"):
            return None
        return _gate_write(name, args, mode, appr, "访问项目之外的地址 %s" % host)
    if spec["risk"] == RISK_WRITE:
        # DSP 计划锁：有计划且未审批时，写工具一律锁死（理由带 /approve 指路）。
        # try 包住：dsp.py 缺失/计划文件坏都不许阻断主循环（fail-open），
        # 只在「计划明确存在且未审批」时才锁（fail-closed 只对真计划）。
        # 老板显式关了计划锁（agent.plan_lock=false）则整段跳过。
        if not ctx.get("plan_lock_off"):
            try:
                _dsp_mod = _dsp()
                _sid = str(ctx.get("sid") or "")
                if _sid:
                    _plan, _err = _dsp_mod.load_plan(_sid)
                    if _plan is not None and not _plan.get("approved"):
                        return (
                            "计划未审批：%s 未执行（/tasks 看计划，"
                            "/approve 通过后才放行写操作）" % name
                        )
            except Exception:  # noqa: BLE001
                pass
        if ctx.get("mode_gate_off"):
            return None
        return _gate_write(name, args, mode, appr, "写操作", auto=True)
    # DSP 任务白名单：有计划且已审批时，工具必须在当前任务白名单里。
    # 读工具不受白名单约束（侦察自由）；写工具在上一步已过计划锁。
    # 老板显式关了计划锁则白名单同样跳过（两道都是计划面的监督闸）。
    if not ctx.get("plan_lock_off"):
        try:
            _dsp_mod = _dsp()
            _sid = str(ctx.get("sid") or "")
            _tid = str(ctx.get("dsp_task") or "")
            if _sid and _tid:
                _plan, _err = _dsp_mod.load_plan(_sid)
                if _plan is not None and _plan.get("approved"):
                    _why = _dsp_mod.check_tool(_plan, _tid, name, args)
                    if _why:
                        return _why
        except Exception:  # noqa: BLE001
            pass
    return None


# ── 工具层 ────────────────────────────────────────────────────
TOOLS = {}


def _tool(name, risk, description, schema):
    def deco(fn):
        TOOLS[name] = {
            "name": name,
            "risk": risk,
            "fn": fn,
            "description": description,
            "schema": schema,
        }
        return fn

    return deco


def tool_defs():
    """给模型的 tools 数组（OpenAI function calling 形状）。"""
    out = []
    for name in sorted(TOOLS):
        s = TOOLS[name]
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": s["description"],
                    "parameters": s["schema"],
                },
            }
        )
    return out


_S_LISTDIR = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "项目根内的相对路径，默认 '.'"},
        "depth": {
            "type": "integer",
            "description": "1 或 2，默认 1",
            "minimum": 1,
            "maximum": 2,
        },
    },
}


@_tool(
    "list_dir",
    RISK_READ,
    "列目录：名字/是否目录/字节数。只看项目根内，隐藏文件跳过。",
    _S_LISTDIR,
)
def _t_list_dir(a, ctx):
    real, err = _sandbox(a.get("path") or ".", ctx["root"])
    if err:
        return {"ok": False, "error": err}
    if not os.path.isdir(real):
        return {"ok": False, "error": "不是目录：%s" % (a.get("path") or ".")}
    try:
        depth = max(1, min(2, int(a.get("depth", 1) or 1)))
    except Exception:  # noqa: BLE001
        depth = 1
    out = []
    try:
        for name in sorted(os.listdir(real)):
            if name.startswith("."):
                continue
            full = os.path.join(real, name)
            isd = os.path.isdir(full)
            try:
                size = 0 if isd else os.path.getsize(full)
            except OSError:
                size = 0
            row = {"name": name, "dir": isd, "size": size}
            if depth > 1 and isd:
                try:
                    row["children"] = [
                        {"name": x, "dir": os.path.isdir(os.path.join(full, x))}
                        for x in sorted(os.listdir(full))
                        if not x.startswith(".")
                    ][:50]
                except OSError:
                    row["children"] = []
            out.append(row)
    except OSError as e:
        return {"ok": False, "error": "读目录失败：%s" % str(e)[:120]}
    return {
        "ok": True,
        "path": a.get("path") or ".",
        "n": len(out),
        "entries": out[:200],
        "note": "只列前 200 条" if len(out) > 200 else "",
    }


_S_READ = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "项目根内的相对路径"},
        "offset": {
            "type": "integer",
            "description": "起始行号（1 起），默认 1",
            "minimum": 1,
        },
        "limit": {
            "type": "integer",
            "description": "最多读多少行，默认 2000",
            "minimum": 1,
            "maximum": 2000,
        },
    },
    "required": ["path"],
}


@_tool(
    "read_file",
    RISK_READ,
    "按行读一个文本文件，带回显行号。超 256 KiB 拒读并要求先 search 定位。",
    _S_READ,
)
def _t_read_file(a, ctx):
    real, err = _sandbox(a.get("path"), ctx["root"])
    if err:
        return {"ok": False, "error": err}
    if not os.path.isfile(real):
        return {"ok": False, "error": "不是文件：%s" % a.get("path")}
    try:
        sz = os.path.getsize(real)
    except OSError as e:
        return {"ok": False, "error": "取大小失败：%s" % str(e)[:120]}
    if sz > MAX_FILE_BYTES:
        return {
            "ok": False,
            "error": "文件 %d 字节 > 上限 %d；请先用 search 定位，"
            "再带 offset/limit 小范围读" % (sz, MAX_FILE_BYTES),
        }
    try:
        with open(real, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError as e:
        return {"ok": False, "error": "读失败：%s" % str(e)[:120]}
    try:
        off = max(1, int(a.get("offset", 1) or 1))
    except Exception:  # noqa: BLE001
        off = 1
    try:
        lim = max(1, min(MAX_LINES, int(a.get("limit", MAX_LINES) or MAX_LINES)))
    except Exception:  # noqa: BLE001
        lim = MAX_LINES
    seg = lines[off - 1 : off - 1 + lim]
    return {
        "ok": True,
        "path": a.get("path"),
        "n_lines": len(lines),
        "offset": off,
        "showing": len(seg),
        "text": "\n".join("%6d\t%s" % (off + i, s) for i, s in enumerate(seg)),
        "more": (off - 1 + lim) < len(lines),
    }


_S_SEARCH = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "正则（rg 语法）"},
        "path": {"type": "string", "description": "搜索起点，默认项目根"},
        "glob": {"type": "string", "description": "文件过滤，如 *.py"},
        "max_results": {
            "type": "integer",
            "minimum": 1,
            "maximum": 200,
            "description": "默认 50",
        },
    },
    "required": ["pattern"],
}


def _find_rg():
    for c in (shutil.which("rg"), "/opt/homebrew/bin/rg", "/usr/bin/rg"):
        if c and os.path.exists(c):
            return c
    return None


@_tool(
    "search",
    RISK_READ,
    "正则搜索（rg 优先，grep -E 兜底），回 文件:行号:内容。",
    _S_SEARCH,
)
def _t_search(a, ctx):
    pat = a.get("pattern")
    if not pat or not isinstance(pat, str):
        return {"ok": False, "error": "缺 pattern（正则字符串）"}
    real, err = _sandbox(a.get("path") or ".", ctx["root"])
    if err:
        return {"ok": False, "error": err}
    try:
        mx = max(1, min(200, int(a.get("max_results", 50) or 50)))
    except Exception:  # noqa: BLE001
        mx = 50
    rg = _find_rg()
    if rg:
        argv = [rg, "-n", "--no-heading", "--max-count", str(mx)]
        if a.get("glob"):
            argv += ["-g", str(a["glob"])]
        argv += ["--", pat, real]
    else:
        argv = ["grep", "-rnE", "--binary-files=without-match", "--", pat, real]
    try:
        p = subprocess.run(
            argv, capture_output=True, cwd=ctx["root"], timeout=25, env=_CMD_ENV
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "搜索超时（>25s）已杀掉", "rc": None}
    except OSError as e:
        return {"ok": False, "error": "搜索命令起不来：%s" % str(e)[:120]}
    out = ((p.stdout or b"") + (p.stderr or b"")).decode("utf-8", "replace")
    lines = [ln for ln in out.splitlines() if ln.strip()][:mx]
    rel = os.path.relpath(real, ctx["root"])
    return {
        "ok": True,
        "pattern": pat,
        "path": rel,
        "engine": "rg" if rg else "grep",
        "rc": p.returncode,
        "n": len(lines),
        "hits": lines,
        "note": "rc=1 = 没命中（这不是失败）" if p.returncode == 1 else "",
    }


_S_RUNCMD = {
    "type": "object",
    "properties": {
        "argv": {
            "type": "array",
            "items": {"type": "string"},
            "description": '如 ["ls","-1","tools"]；第一项是命令名，'
            "没有 shell（默认自由面任意命令；显式 "
            "cmd_gate 时收窄为只读白名单）",
        },
        "timeout": {
            "type": "integer",
            "minimum": 1,
            "maximum": 30,
            "description": "秒，默认 20",
        },
    },
    "required": ["argv"],
}


@_tool(
    "run_cmd",
    RISK_WRITE,
    "跑一条命令（默认自由面任意命令；显式 cmd_gate 收窄为只读白名单），"
    "回真实 rc 与真实输出。",
    _S_RUNCMD,
)
def _t_run_cmd(a, ctx):
    argv = a.get("argv")
    if isinstance(argv, str) and argv.strip():
        try:
            argv = shlex.split(argv)
            a["argv"] = argv
        except ValueError:
            pass
    if not isinstance(argv, list) or not argv:
        return {"ok": False, "error": "argv 必须是非空数组（第一项是命令名）"}
    argv = [str(x) for x in argv]
    if len(argv) > 24:
        return {"ok": False, "error": "参数过多（>24）"}
    if ctx.get("cmd_gate_off"):
        # 自由命令面（默认）：任意命令，绝对路径或 PATH 解析；硬阻断在
        # 审批层已拦，这里只负责如实起不来就说起不来。
        exe = (
            argv[0]
            if (os.path.isabs(argv[0]) and os.path.exists(argv[0]))
            else shutil.which(argv[0])
        )
        if not exe:
            return {
                "ok": False,
                "error": "命令找不到：%s（PATH 与绝对路径都没命中）" % argv[0][:40],
            }
        real_argv = [exe] + argv[1:]
    else:
        base = _term_cmds().get(argv[0])
        if not base:
            return {
                "ok": False,
                "error": "命令不在只读白名单里：%s（可跑：%s）"
                % (argv[0][:40], " ".join(sorted(_term_cmds()))),
            }
        real_argv = (
            ([base[0], argv[1]] + argv[2:])
            if len(base) == 2
            else (list(base) + argv[1:])
        )
    try:
        tmo = max(1, min(30, int(a.get("timeout", 20) or 20)))
    except Exception:  # noqa: BLE001
        tmo = 20
    t0 = time.time()
    try:
        p = subprocess.run(
            real_argv, capture_output=True, cwd=ctx["root"], timeout=tmo, env=_CMD_ENV
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": "命令超时（>%ds）已杀掉" % tmo,
            "rc": None,
            "dur": round(time.time() - t0, 2),
        }
    except OSError as e:
        return {"ok": False, "error": "命令起不来：%s" % str(e)[:120]}
    text = ((p.stdout or b"") + (p.stderr or b"")).decode("utf-8", "replace")
    return {
        "ok": True,
        "argv": real_argv,
        "rc": p.returncode,
        "out": text[:TERM_MAX_OUT],
        "truncated": len(text) > TERM_MAX_OUT,
        "dur": round(time.time() - t0, 2),
    }


_S_WRITE = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "项目根内的相对路径"},
        "content": {"type": "string", "description": "整文件内容（UTF-8）"},
    },
    "required": ["path", "content"],
}


# ── 写前快照（蒸馏自 cline checkpoints / aider git-undo）──────────────
# write_file 成功落盘前，把原文件复制进 work/agent_snapshots/<sid>/，
# manifest.jsonl 追加一行；/undo 按行逆序恢复、消费即标记。快照失败不阻断
# 主写入（fail-open），但 /undo 会如实报「无快照」。
_SNAP_ROOT = os.path.join(ROOT, "work", "agent_snapshots")


def _snap_before_write(real, ctx):
    try:
        sd = os.path.join(
            ctx.get("snap_dir") or _SNAP_ROOT, str(ctx.get("sid") or "nosid")
        )
        os.makedirs(sd, exist_ok=True)
        existed = os.path.exists(real)
        bk = ""
        if existed:
            bk = "%d__%s" % (int(time.time() * 1000), os.path.basename(real))
            n = 0
            while os.path.exists(os.path.join(sd, bk)):
                n += 1
                bk = "%d_%d__%s" % (int(time.time() * 1000), n, os.path.basename(real))
            shutil.copy2(real, os.path.join(sd, bk))
        with open(os.path.join(sd, "manifest.jsonl"), "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "ts": time.time(),
                        "target": real,
                        "backup": bk,
                        "existed": existed,
                        "consumed": False,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
        return True
    except Exception:  # noqa: BLE001
        return False


@_tool(
    "write_file",
    RISK_WRITE,
    "写一个文件（整文件覆盖或新建）。只限项目根内，回 sha256 前后值。",
    _S_WRITE,
)
def _t_write_file(a, ctx):
    rel, content = a.get("path"), a.get("content")
    if not rel:
        return {"ok": False, "error": "缺 path"}
    if not isinstance(content, str):
        return {"ok": False, "error": "content 必须是字符串"}
    real, err = _sandbox(rel, ctx["root"])
    if err:
        return {"ok": False, "error": err}
    if os.path.isdir(real):
        return {"ok": False, "error": "目标是目录：%s" % rel}
    _snap_before_write(real, ctx)
    before = _sha(real)
    try:
        d = os.path.dirname(real)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(real, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return {"ok": False, "error": "写盘失败：%s" % str(e)[:160]}
    return {
        "ok": True,
        "path": rel,
        "bytes": len(content.encode("utf-8")),
        "sha256": _sha(real),
        "before_sha256": before,
        "created": before is None,
    }


_S_EDIT = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "项目根内的相对路径"},
        "old_string": {
            "type": "string",
            "description": "要被替换掉的原文（必须在文件里唯一）",
        },
        "new_string": {"type": "string", "description": "替换成什么"},
        "replace_all": {
            "type": "boolean",
            "description": "出现多次时是否全部替换，默认 false",
        },
    },
    "required": ["path", "old_string", "new_string"],
}


@_tool(
    "edit_file",
    RISK_WRITE,
    "把文件里唯一出现的 old_string 换成 new_string；不唯一就拒改。",
    _S_EDIT,
)
def _t_edit_file(a, ctx):
    rel, old, new = a.get("path"), a.get("old_string"), a.get("new_string")
    if not rel or not isinstance(old, str) or not isinstance(new, str):
        return {
            "ok": False,
            "error": "path/old_string/new_string 缺一不可，且都必须是字符串",
        }
    if old == "":
        return {"ok": False, "error": "old_string 不能为空（空串会匹配无数处）"}
    real, err = _sandbox(rel, ctx["root"])
    if err:
        return {"ok": False, "error": err}
    if not os.path.isfile(real):
        return {"ok": False, "error": "不是文件：%s" % rel}
    try:
        with open(real, encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return {"ok": False, "error": "读失败：%s" % str(e)[:160]}
    before = _sha(real)
    n = text.count(old)
    if n == 0:
        return {"ok": False, "error": "old_string 一次都没出现（未改动任何字节）"}
    allrep = bool(a.get("replace_all"))
    if n > 1 and not allrep:
        return {
            "ok": False,
            "error": "old_string 出现 %d 次；要么带更多上下文让它唯一，"
            "要么 replace_all=true" % n,
        }
    try:
        with open(real, "w", encoding="utf-8") as f:
            f.write(text.replace(old, new) if allrep else text.replace(old, new, 1))
    except OSError as e:
        return {"ok": False, "error": "写盘失败：%s" % str(e)[:160]}
    return {
        "ok": True,
        "path": rel,
        "replacements": n if allrep else 1,
        "sha256": _sha(real),
        "before_sha256": before,
    }


def _git(a, ctx, argv, timeout=25):
    real = ["git", "-C", ctx["root"]] + [str(x) for x in argv]
    try:
        p = subprocess.run(real, capture_output=True, timeout=timeout, env=_CMD_ENV)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "git 超时（>%ds）" % timeout, "rc": None}
    except OSError as e:
        return {"ok": False, "error": "git 起不来：%s" % str(e)[:120]}
    out = (p.stdout or b"").decode("utf-8", "replace")
    err = (p.stderr or b"").decode("utf-8", "replace")
    if p.returncode != 0:
        return {
            "ok": False,
            "rc": p.returncode,
            "error": (err or out).strip()[:200] or ("git rc=%s" % p.returncode),
            "note": "不是 git 仓库也算失败，如实报",
        }
    return {
        "ok": True,
        "rc": 0,
        "out": out[:TERM_MAX_OUT],
        "truncated": len(out) > TERM_MAX_OUT,
    }


_S_NONE = {"type": "object", "properties": {}}


@_tool("git_status", RISK_READ, "git status --porcelain（真实 rc）", _S_NONE)
def _t_git_status(a, ctx):
    return _git(a, ctx, ["status", "--porcelain=v1", "--branch"])


_S_DIFF = {
    "type": "object",
    "properties": {
        "target": {
            "type": "string",
            "description": "如 HEAD / origin/main ...，默认工作区",
        }
    },
}


@_tool("git_diff", RISK_READ, "git diff（真实 rc）", _S_DIFF)
def _t_git_diff(a, ctx):
    argv = ["diff", "--no-color"]
    if a.get("target"):
        argv.append(str(a["target"]))
    return _git(a, ctx, argv)


_S_LOG = {
    "type": "object",
    "properties": {
        "n": {"type": "integer", "minimum": 1, "maximum": 50, "description": "默认 10"}
    },
}


@_tool("git_log", RISK_READ, "git log --oneline（真实 rc）", _S_LOG)
def _t_git_log(a, ctx):
    try:
        n = max(1, min(50, int(a.get("n", 10) or 10)))
    except Exception:  # noqa: BLE001
        n = 10
    return _git(a, ctx, ["log", "--oneline", "-n", str(n)])


_S_GH = {
    "type": "object",
    "properties": {
        "argv": {
            "type": "array",
            "items": {"type": "string"},
            "description": '不含 gh 本身，如 ["issue","list","--repo","o/r"]',
        },
    },
    "required": ["argv"],
}


def _gh_client():
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import gh_client  # noqa: PLC0415

        return gh_client
    except Exception:  # noqa: BLE001
        return None


@_tool(
    "gh_read", RISK_READ, "跑一条只读 gh 命令；写向量在 gh_client 里结构上被拒。", _S_GH
)
def _t_gh_read(a, ctx):
    argv = a.get("argv")
    if not isinstance(argv, list) or not argv:
        return {"ok": False, "error": 'argv 必须是非空数组，如 ["issue","list"]'}
    gc = _gh_client()
    if gc is None:
        return {"ok": False, "error": "gh_client 导不进来（ tools/ 不在 path？）"}
    try:
        r = gc.run_gh([str(x) for x in argv], timeout=30.0)
    except ValueError as e:
        return {"ok": False, "error": str(e), "denied": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "gh 调用异常：%s" % str(e)[:160]}
    out = {"ok": bool(r.get("ok")), "rc": r.get("rc"), "cmd": r.get("disp")}
    if r.get("ok"):
        out["stdout"] = (r.get("stdout") or "")[:16000]
    else:
        out["error"] = r.get("error")
        out["kind"] = r.get("kind")
    return out


_S_HEALTH = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "默认 http://127.0.0.1:8790/__health"}
    },
}


@_tool(
    "health", RISK_READ, "GET 一个回环 /__health，判 ok:true 与真实 HTTP 码", _S_HEALTH
)
def _t_health(a, ctx):
    url = (a.get("url") or "http://127.0.0.1:8790/__health").strip()
    host = _host_of(url)
    if not _is_loopback(host):
        return {"ok": False, "error": "只允许回环地址：%r" % host}
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            st = r.getcode()
            body = r.read(4096).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "服务不可达：%s" % str(e)[:160]}
    try:
        obj = json.loads(body)
    except ValueError:
        obj = None
    healthy = st == 200 and isinstance(obj, dict) and obj.get("ok") is True
    return {
        "ok": healthy,
        "http": st,
        "url": url,
        "body": obj if isinstance(obj, dict) else body[:400],
        "note": "ok=false 表示服务自己说不健康，不是工具失败" if not healthy else "",
    }


_S_FETCH = {
    "type": "object",
    "properties": {"url": {"type": "string", "description": "http/https"}},
    "required": ["url"],
}


# ── GitHub 只读工具（R1，蒸馏自「任务优先参考 GitHub」的全局授权纪律）──
# 全部走 gh_client 只读收口 run_gh（白名单内向量），零远端写。
_S_GH_SEARCH = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "搜索词，如 terminal agent"},
        "kind": {
            "type": "string",
            "description": "repos|issues|code|commits|prs，默认 repos",
        },
        "limit": {"type": "integer", "description": "1-100，默认 10"},
    },
    "required": ["query"],
}
_S_GH_VIEW = {
    "type": "object",
    "properties": {"repo": {"type": "string", "description": "owner/repo"}},
    "required": ["repo"],
}
_S_GH_FILE = {
    "type": "object",
    "properties": {
        "repo": {"type": "string", "description": "owner/repo"},
        "path": {"type": "string", "description": "仓库内文件路径"},
        "ref": {"type": "string", "description": "分支/tag，默认默认分支"},
    },
    "required": ["repo", "path"],
}


def _gh_client_mod():
    # 用 ROOT 定位而不是 __file__：neg_agent_core 会把本文件拷到 work/ 跑，
    # 拷贝件的 __file__ 指向 work/，按 __file__ 找 gh_client 必炸（实测）。
    here = os.path.join(ROOT, "tools")
    if here not in sys.path:
        sys.path.insert(0, here)
    import gh_client

    return gh_client


def _dsp():
    """懒加载 tools/dsp.py（与 _gh_client_mod 同式：拷贝件 __file__ 不可用，
    用 ROOT 定位）。dsp.py 缺失时抛 ImportError，调用方降级走原单循环。"""
    here = os.path.join(ROOT, "tools")
    if here not in sys.path:
        sys.path.insert(0, here)
    import dsp

    return dsp


def _leader_mod():
    """懒加载 tools/leader.py（/goal 的 DAG 编译器；缺失时调用方降级）。"""
    here = os.path.join(ROOT, "tools")
    if here not in sys.path:
        sys.path.insert(0, here)
    import leader

    return leader


def _autonomy():
    """懒加载 tools/autonomy.py（自主执行引擎；缺失时调用方降级）。"""
    here = os.path.join(ROOT, "tools")
    if here not in sys.path:
        sys.path.insert(0, here)
    import autonomy

    return autonomy


def _mcp():
    """懒加载 tools/mcp_client.py（同 _dsp 式）。"""
    here = os.path.join(ROOT, "tools")
    if here not in sys.path:
        sys.path.insert(0, here)
    import mcp_client

    return mcp_client


_MCP_SYNCED = []


def _mcp_sync():
    """把 ~/.agentos/config.toml [mcp] 的服务器挂进 TOOLS（进程内只做一次）。

    只在**用户入口**（REPL / headless）调用 —— --check 与门链不走这里，
    门链的确定性不受用户配置影响。挂载失败如实打 stderr，不阻断主循环。
    """
    if _MCP_SYNCED:
        return
    _MCP_SYNCED.append(True)
    try:
        cfg = _read_agentos_config().get("mcp") or {}
        if not cfg.get("servers"):
            return
        rep = _mcp().sync_tools(TOOLS, cfg, risk_read=RISK_READ, risk_write=RISK_WRITE)
        for line in rep.get("errors") or []:
            sys.stderr.write("[mcp] 挂载失败：%s\n" % line)
        for name, st in sorted((rep.get("servers") or {}).items()):
            sys.stderr.write(
                "[mcp] %s: handshake=%s tools=%d\n"
                % (name, st.get("handshake"), st.get("tools") or 0)
            )
        if rep.get("added"):
            sys.stderr.write(
                "[mcp] 已挂载 %d 个工具：%s\n"
                % (len(rep["added"]), " ".join(sorted(rep["added"])))
            )
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("[mcp] 同步异常（忽略，主循环不受影响）：%s\n" % str(e)[:140])


def _hooks_mw():
    """懒加载 tools/hooks_mw.py（同 _dsp 式）。"""
    here = os.path.join(ROOT, "tools")
    if here not in sys.path:
        sys.path.insert(0, here)
    import hooks_mw

    return hooks_mw


def _skills_mod():
    """懒加载 tools/skills_load.py（同 _dsp 式）。"""
    here = os.path.join(ROOT, "tools")
    if here not in sys.path:
        sys.path.insert(0, here)
    import skills_load

    return skills_load


def _branches_mod():
    """懒加载 tools/branches.py（同 _dsp 式）：会话分支 + 计划自验收。"""
    here = os.path.join(ROOT, "tools")
    if here not in sys.path:
        sys.path.insert(0, here)
    import branches

    return branches


def _steer_mod():
    """懒加载 tools/steer.py（同 _dsp 式）：老板监督指令通道。"""
    here = os.path.join(ROOT, "tools")
    if here not in sys.path:
        sys.path.insert(0, here)
    import steer

    return steer


def _watch_mod():
    """懒加载 tools/watch.py（同 _dsp 式）：老板监督面。"""
    here = os.path.join(ROOT, "tools")
    if here not in sys.path:
        sys.path.insert(0, here)
    import watch

    return watch


_STEER_DIR_OVERRIDE = []


def _steer_dir():
    """指令箱目录（测试注入；空则真目录 work/steer）。"""
    return (
        _STEER_DIR_OVERRIDE[0]
        if _STEER_DIR_OVERRIDE
        else os.path.join(ROOT, "work", "steer")
    )


def _steer_take(ctx):
    """消费老板指令（每个步骤边界调一次）。返回注入文本列表。

    只配送不裁决：注入的是 [老板指令] 消息，执不执行由模型与审批矩阵定。
    任何异常只记 stderr，绝不许监督通道断主循环。
    """
    sid = str(ctx.get("sid") or "")
    if not sid:
        return []
    try:
        lines, total = _steer_mod().pending(_steer_dir(), sid)
        if not lines:
            return []
        texts = [str(x.get("text") or "")[:500] for x in lines]
        _steer_mod().commit(_steer_dir(), sid, total)
        return texts
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("[steer] 指令读取异常（忽略）：%s\n" % str(e)[:120])
        return []


_HOOKS_CFG = []
_SKILL_ROOTS_OVERRIDE = []  # 测试注入；空则用默认两处


def _skill_roots():
    return _SKILL_ROOTS_OVERRIDE or [
        os.path.join(os.path.expanduser("~"), ".agentos", "skills"),
        os.path.join(ROOT, ".agentos", "skills"),
    ]


def _hooks_cfg():
    """读 ~/.agentos/hooks.json（进程内缓存；缺文件/坏了 = None = 全关）。

    为什么缓存：钩子配置是「开机契约」，REPL 长跑期间不重读 —— 改配置重启
    生效（V1 口径，写进 /help）。
    """
    if not _HOOKS_CFG:
        _HOOKS_CFG.append(
            _hooks_mw().load_cfg(
                os.path.join(os.path.expanduser("~"), ".agentos", "hooks.json")
            )
        )
    return _HOOKS_CFG[0]


def _hooks_session(ctx):
    """SessionStart：收集注入上下文。失败只记不拦（开机不许被中间件卡死）。"""
    try:
        cfg = _hooks_cfg()
        if not cfg:
            return
        rep = _hooks_mw().run_event("SessionStart", {"root": ctx.get("root")}, cfg)
        for line in rep.get("context") or []:
            ctx.setdefault("hook_context", []).append(line)
        for e in rep.get("errors") or []:
            sys.stderr.write("[hooks] %s\n" % e)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("[hooks] SessionStart 忽略：%s\n" % str(e)[:120])


_SKILLS_ITEMS = []


def _skills_items():
    """技能清单（进程内缓存；发现失败 = 空表）。"""
    if not _SKILLS_ITEMS:
        try:
            _SKILLS_ITEMS.append(_skills_mod().discover(_skill_roots()))
        except Exception:  # noqa: BLE001
            _SKILLS_ITEMS.append([])
    return _SKILLS_ITEMS[0]


def _skills_auto(text):
    """按 triggers 命中把技能正文注入本轮系统提示（预算 4000、最多 2 个）。

    自动触发是「显式 > 猜」：只认 frontmatter 里写明的 triggers 词做子串
    命中，命中才注入正文；不命中/解析失败一律零改动。
    """
    try:
        hits = _skills_mod().auto_trigger(text, _skills_items())
    except Exception:  # noqa: BLE001
        return ""
    if not hits:
        return ""
    parts = ["本次需求命中的技能（triggers 自动注入）："]
    for name, body in hits:
        parts.append("【技能 %s】\n%s" % (name, body))
    return "\n\n".join(parts)[:4000]


_SKILLS_CACHE = []


def _skills_section():
    """skills 段（进程内缓存；无技能目录 = 空串 = 零改动）。"""
    if not _SKILLS_CACHE:
        try:
            _SKILLS_CACHE.append(
                _skills_mod().render_section(_skills_mod().discover(_skill_roots()))
            )
        except Exception:  # noqa: BLE001
            _SKILLS_CACHE.append("")
    return _SKILLS_CACHE[0]


def _gh_search_argv(query, kind, limit):
    k = (
        kind
        if kind in ("repos", "issues", "code", "commits", "prs", "pulls")
        else "repos"
    )
    return [
        "search",
        k,
        str(query),
        "--limit",
        str(int(limit)),
        "--json",
        (
            "fullName,description,stargazersCount,license"
            if k == "repos"
            else "number,title,repository,state"
        ),
    ]


def _gh_file_read_argv(repo, path, ref):
    q = "?ref=%s" % ref if ref else ""
    return ["api", "repos/%s/contents/%s%s" % (repo, str(path).lstrip("/"), q)]


@_tool(
    "gh_search",
    RISK_READ,
    "搜 GitHub 成熟实现（repos/issues/code/commits/prs）——任务先看别人"
    "怎么做的，能参考绝不从零写。",
    _S_GH_SEARCH,
)
def _t_gh_search(a, ctx):
    q = (a.get("query") or "").strip()
    if not q:
        return {"ok": False, "error": "缺 query"}
    gc = _gh_client_mod()
    argv = _gh_search_argv(q, a.get("kind") or "repos", a.get("limit") or 10)
    try:
        parsed = gc._need_json(gc.run_gh(argv, timeout=30))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "gh 搜索失败：%s" % str(e)[:160]}
    rows = []
    for it in (parsed if isinstance(parsed, list) else [])[:12]:
        if not isinstance(it, dict):
            continue
        if a.get("kind", "repos") == "repos":
            rows.append(
                "%s ★%s · %s"
                % (
                    it.get("fullName", "?"),
                    it.get("stargazersCount", "-"),
                    (it.get("description") or "")[:80],
                )
            )
        else:
            rp = it.get("repository") or {}
            rows.append(
                "#%s %s [%s] · %s"
                % (
                    it.get("number"),
                    it.get("title", "?"),
                    it.get("state", "?"),
                    rp.get("nameWithOwner", "?") if isinstance(rp, dict) else "?",
                )
            )
    return {
        "ok": True,
        "query": q,
        "hits": len(rows),
        "results": rows or ["（无命中）"],
    }


@_tool(
    "gh_repo_view",
    RISK_READ,
    "看一个 GitHub 仓库的概况（描述/星/语言/更新时间）。",
    _S_GH_VIEW,
)
def _t_gh_repo_view(a, ctx):
    repo = (a.get("repo") or "").strip()
    if repo.count("/") != 1 or not all(repo.split("/")):
        return {"ok": False, "error": "repo 需要 owner/repo 形式：%r" % repo}
    gc = _gh_client_mod()
    argv = [
        "repo",
        "view",
        repo,
        "--json",
        "nameWithOwner,description,stargazerCount,forkCount,"
        "primaryLanguage,updatedAt,licenseInfo",
    ]
    try:
        parsed = gc._need_json(gc.run_gh(argv, timeout=30))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "gh 读取失败：%s" % str(e)[:160]}
    lic = parsed.get("licenseInfo") or {}
    return {
        "ok": True,
        "repo": parsed.get("nameWithOwner", repo),
        "star": parsed.get("stargazerCount"),
        "license": lic.get("name") if isinstance(lic, dict) else None,
        "language": (parsed.get("primaryLanguage") or {}).get("name")
        if isinstance(parsed.get("primaryLanguage"), dict)
        else None,
        "description": (parsed.get("description") or "")[:160],
        "updatedAt": parsed.get("updatedAt"),
    }


@_tool(
    "gh_file_read",
    RISK_READ,
    "读一个 GitHub 仓库里的文件内容（经 contents API，只读）——参考实现细节用。",
    _S_GH_FILE,
)
def _t_gh_file_read(a, ctx):
    repo = (a.get("repo") or "").strip()
    path = (a.get("path") or "").strip()
    if repo.count("/") != 1 or not all(repo.split("/")):
        return {"ok": False, "error": "repo 需要 owner/repo 形式：%r" % repo}
    if not path or ".." in path.split("/"):
        return {"ok": False, "error": "path 非法：%r" % path}
    gc = _gh_client_mod()
    argv = _gh_file_read_argv(repo, path, (a.get("ref") or "").strip())
    try:
        parsed = gc._need_json(gc.run_gh(argv, timeout=30))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "gh 读取失败：%s" % str(e)[:160]}
    body = parsed.get("content") if isinstance(parsed, dict) else None
    if parsed.get("encoding") == "base64" and body:
        import base64

        try:
            body = base64.b64decode(body).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = "(base64 解码失败)"
    return {"ok": True, "repo": repo, "path": path, "content": str(body or "")[:8000]}


# ── GitHub 自主部署工具（R2，用户明确授权；审计+限速一条不少）──────────
_S_GH_DEPLOY = {
    "type": "object",
    "properties": {
        "dir": {"type": "string", "description": "项目根内要部署的目录"},
        "repo": {
            "type": "string",
            "description": "owner/name（缺 owner 自动拼登录名）",
        },
        "public": {"type": "boolean", "description": "默认私有库"},
        "dry_run": {"type": "boolean", "description": "预演零副作用"},
    },
    "required": ["dir", "repo"],
}


@_tool(
    "gh_deploy",
    RISK_WRITE,
    "把项目根内的目录部署成 GitHub 仓库（缺库则建+push）。仅 mode=full "
    "且 gh.write_enabled=true；密钥扫描拒发、审计+限速全程留痕。",
    _S_GH_DEPLOY,
)
def _t_gh_deploy(a, ctx):
    if ctx.get("mode") != "full":
        return {
            "ok": False,
            "error": "gh_deploy 仅 mode=full 可用（当前 %r）；"
            "/mode full 后再试" % ctx.get("mode"),
        }
    wen = (_read_agentos_config().get("gh") or {}).get("write_enabled")
    if str(wen) != "true":
        return {
            "ok": False,
            "error": "gh.write_enabled 未开：agentos config set gh.write_enabled true",
        }
    gc = _gh_client_mod()
    d = (a.get("dir") or "").strip()
    root_abs = os.path.abspath(ctx.get("root") or os.getcwd())
    dirpath = os.path.abspath(os.path.join(root_abs, d))
    if dirpath != root_abs and not dirpath.startswith(root_abs + os.sep):
        return {"ok": False, "error": "dir 越界（只允许项目根内）：%s" % d}
    if not os.path.isdir(dirpath):
        return {"ok": False, "error": "目录不存在：%s" % d}
    repo = (a.get("repo") or "").strip()
    if repo.count("/") != 1:
        try:
            parsed = gc._need_json(gc.run_gh(["api", "user"], timeout=20))
            login = str((parsed or {}).get("login") or "")
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": "取登录名失败：%s" % str(e)[:120]}
        if not login:
            return {"ok": False, "error": "gh 未认证，无法确定 owner"}
        repo = "%s/%s" % (login, repo)
    rows = gc.audit_exec_rows()
    okp, reason = gc.write_policy(True, False, wen, repo, rows, auto=True)
    if not okp:
        return {
            "ok": False,
            "error": "部署被拒（%s）：%s"
            % (
                reason,
                {
                    "config-off": "gh.write_enabled 未开",
                    "rate-limit": "该仓库 1 小时真写已达上限",
                    "no-yes": "-",
                    "non-tty": "-",
                }.get(reason, reason),
            ),
        }
    public = bool(a.get("public"))

    def _aud(kind, sub, rp, num, v):
        try:
            return (
                gc.audit_append(kind, sub, rp, num, v, 0, "agent 自主部署（mode=full）")
                is not None
            )
        except Exception:  # noqa: BLE001
            return False

    r = gc.deploy_dir(
        dirpath, repo, public=public, dry_run=bool(a.get("dry_run")), audit=_aud
    )
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error")}
    if r.get("dry_run"):
        return {
            "ok": True,
            "dry_run": True,
            "steps": r.get("steps") or [],
            "secret_hits": r.get("secret_hits") or [],
        }
    return {
        "ok": True,
        "repo": r.get("repo"),
        "created": r.get("created"),
        "pushed": r.get("pushed"),
    }


@_tool(
    "web_fetch",
    RISK_READ,
    "GET 一个 URL 的文本。回环直接放行；非回环按当前闸门裁决"
    "（默认自由面直连；显式 mode_gate 时需审批）。",
    _S_FETCH,
)
def _t_web_fetch(a, ctx):
    url = (a.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "缺 url"}
    try:
        scheme = urllib.parse.urlparse(url).scheme
    except Exception:  # noqa: BLE001
        scheme = ""
    if scheme not in ("http", "https"):
        return {"ok": False, "error": "只支持 http/https：%r" % scheme}
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "agentos-agent/%s" % VERSION, "Accept": "*/*"}
        )
        with urllib.request.urlopen(req, timeout=25) as r:
            st = r.getcode()
            body = r.read(65536).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "抓取失败：%s" % str(e)[:160]}
    return {
        "ok": st == 200,
        "http": st,
        "url": url,
        "bytes": len(body),
        "text": body[:MAX_TOOL_OUT],
    }


# ── subagent-as-tool（W2）：子代理做独立子任务 —— 禁嵌套、子集强制、权限继承 ──
# W6 加深：prompts 批量 ≤4 个并行（线程池；每任务独立 Provider 克隆与 ctx，
# 失败隔离、按输入序返回）。并行性由 repl 类门的 barrier 夹具确定性证伪
# （串行化 ⇒ Barrier 超时 ⇒ 判红），不靠摸时钟。
SUBAGENT_MAX_STEPS = 12  # 子代理默认单轮步数；显式 max_steps 照收不钳
SUBAGENT_PARALLEL_MAX = 4  # 一次批量并行的子代理数上限


def _clone_provider(p):
    """并行子代理各拿一份独立 Provider（会话 id/计数不共享互踩）。

    非 Provider 鸭子对象（夹具注入）原样透传 —— 门的 barrier 夹具靠共享
    同一对象跨线程同步，克隆语义不该把它切碎。
    """
    if isinstance(p, Provider):
        return Provider(
            {
                "base_url": p.base_url,
                "model": p.model,
                "headers": p.headers,
                "provider_name": p.provider_name,
                "source": p.source,
                "protocol": p.protocol,
            }
        )
    return p


def _subagent_batch(prompts, ctx, provider, subset, cap, depth, merge=False):
    """并行批量子代理（≤SUBAGENT_PARALLEL_MAX）。单任务失败只记在自己那份
    报告里（fail-isolated），其余照跑；报告按输入序返回。

    merge=True：全部完成后额外一次 provider 调用把各结论汇总成 digest ——
    协作的最后一跳（map → reduce），失败如实留空 digest。
    """
    import concurrent.futures as _cf

    reports = [None] * len(prompts)

    def _one(i, pr):
        sub_ctx = {
            "root": ctx.get("root"),
            "mode": ctx.get("mode"),
            "approver": ctx.get("approver"),
            "sid": ctx.get("sid"),
            "env": ctx.get("env"),
            "snap_dir": ctx.get("snap_dir"),
            "history": [],
            "subagent_depth": depth + 1,
            "tool_subset": subset,
            "plan_lock_off": ctx.get("plan_lock_off"),
            "full_access": ctx.get("full_access"),
            "mode_gate_off": ctx.get("mode_gate_off"),
            "cmd_gate_off": ctx.get("cmd_gate_off"),
        }
        try:
            res = run_agent(
                pr, ctx=sub_ctx, provider=_clone_provider(provider), max_steps=cap
            )
        except AgentError as e:
            reports[i] = {
                "prompt": pr[:40],
                "ok": False,
                "summary": "子代理失败 [%s] %s" % (e.kind, str(e)[:120]),
                "steps": None,
                "rc": None,
                "limit": False,
            }
            return
        except Exception as e:  # noqa: BLE001
            reports[i] = {
                "prompt": pr[:40],
                "ok": False,
                "summary": "子代理异常：%s" % str(e)[:120],
                "steps": None,
                "rc": None,
                "limit": False,
            }
            return
        summary = str(res.get("answer") or "").strip()
        if not summary and res.get("kind") == "limit":
            summary = "（预算用尽：%s）" % str(res.get("error"))[:100]
        reports[i] = {
            "prompt": pr[:40],
            "ok": bool(res.get("ok")),
            "summary": summary or "（无终答）",
            "steps": res.get("steps"),
            "rc": res.get("rc"),
            "limit": res.get("kind") == "limit",
        }

    with _cf.ThreadPoolExecutor(
        max_workers=min(SUBAGENT_PARALLEL_MAX, len(prompts))
    ) as _ex:
        _futs = [_ex.submit(_one, i, pr) for i, pr in enumerate(prompts)]
        for _f in _futs:
            _f.result()  # 异常已在 _one 内兜住；这里只为汇合
    out = {
        "ok": True,
        "parallel": True,
        "count": len(prompts),
        "results": reports,
        "summary": "并行 %d 个子代理全部返回（%d 成功）"
        % (len(prompts), sum(1 for r in reports if r and r.get("ok"))),
    }
    if merge:
        ok_rs = [r for r in reports if r and r.get("ok") and r.get("summary")]
        digest = ""
        if ok_rs:
            try:
                mp = (
                    "把下面 %d 个子代理的结论汇总成一段事实摘要（保留关键结论"
                    "与未决项；不寒暄、不编造）：\n\n" % len(ok_rs)
                ) + "\n\n".join("- %s" % r["summary"] for r in ok_rs)
                comp = _clone_provider(provider).chat(
                    [{"role": "user", "content": mp}], tools=None, stream=False
                )
                digest = str((comp or {}).get("content") or "").strip()
            except Exception:  # noqa: BLE001
                digest = ""
        out["digest"] = digest
    return out


_SCRATCH_CAP = 8192
_SCRATCH_TAIL = 1500


def _scratch_path(root, sid):
    return os.path.join(str(root or ""), "work", "scratch", "%s.md" % str(sid or ""))


def _scratch_append(root, sid, text):
    if not sid or not root:
        return
    try:
        p = _scratch_path(root, sid)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        cur = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
        cur += "\n## SEG\n%s\n" % str(text or "")[:2000]
        b = cur.encode("utf-8")
        if len(b) > _SCRATCH_CAP:
            cur = b[-_SCRATCH_CAP:].decode("utf-8", "ignore")
        with open(p, "w", encoding="utf-8") as f:
            f.write(cur)
    except OSError:
        pass


def _subagent_chain(prompts, ctx, provider, subset, cap, depth):
    """顺序交接链：第 i 段 prompt 追加「上一阶段结论」；任一段失败即停
    （fail-closed，如实报 stopped_at 与已完成段）——交接内容来自上一段的
    真终答，不是编排层编的。"""
    reports = []
    prev = ""
    _sid = str(ctx.get("sid") or "")
    _bb = []
    for i, pr in enumerate(prompts):
        _needs = pr[1] if isinstance(pr, tuple) else []
        pr = pr[0] if isinstance(pr, tuple) else pr
        _tail = "\n".join(_bb)[-_SCRATCH_TAIL:]
        if _tail:
            full = "%s\n\n上一阶段结论：%s\n\n[共享黑板·前面段结论]\n%s" % (
                pr,
                prev,
                _tail,
            )
        else:
            full = pr if not prev else ("%s\n\n上一阶段结论：%s" % (pr, prev))
        _missing = [k for k in _needs if k not in _tail]
        if _missing:
            reports.append(
                {
                    "prompt": pr[:40],
                    "ok": False,
                    "summary": "缺交接钥匙：%s" % ",".join(_missing),
                    "steps": None,
                    "rc": None,
                }
            )
            return {
                "ok": False,
                "chain": True,
                "stopped_at": i,
                "results": reports,
                "summary": "链在第 %d 段缺交接钥匙（缺：%s），不开工"
                % (i + 1, ",".join(_missing)[:60]),
            }
        sub_ctx = {
            "root": ctx.get("root"),
            "mode": ctx.get("mode"),
            "approver": ctx.get("approver"),
            "sid": ctx.get("sid"),
            "env": ctx.get("env"),
            "snap_dir": ctx.get("snap_dir"),
            "history": [],
            "subagent_depth": depth + 1,
            "tool_subset": subset,
            "plan_lock_off": ctx.get("plan_lock_off"),
            "full_access": ctx.get("full_access"),
            "mode_gate_off": ctx.get("mode_gate_off"),
            "cmd_gate_off": ctx.get("cmd_gate_off"),
        }
        try:
            res = run_agent(
                full, ctx=sub_ctx, provider=_clone_provider(provider), max_steps=cap
            )
        except AgentError as e:
            reports.append(
                {
                    "prompt": pr[:40],
                    "ok": False,
                    "summary": "子代理失败 [%s] %s" % (e.kind, str(e)[:120]),
                    "steps": None,
                    "rc": None,
                }
            )
            return {
                "ok": False,
                "chain": True,
                "stopped_at": i,
                "results": reports,
                "summary": "链在第 %d 段失败（%s）" % (i + 1, str(e)[:80]),
            }
        except Exception as e:  # noqa: BLE001
            reports.append(
                {
                    "prompt": pr[:40],
                    "ok": False,
                    "summary": "子代理异常：%s" % str(e)[:120],
                    "steps": None,
                    "rc": None,
                }
            )
            return {
                "ok": False,
                "chain": True,
                "stopped_at": i,
                "results": reports,
                "summary": "链在第 %d 段异常（%s）" % (i + 1, str(e)[:80]),
            }
        summary = str(res.get("answer") or "").strip()
        if not summary and res.get("kind") == "limit":
            summary = "（预算用尽：%s）" % str(res.get("error"))[:100]
        reports.append(
            {
                "prompt": pr[:40],
                "ok": bool(res.get("ok")),
                "summary": summary or "（无终答）",
                "steps": res.get("steps"),
                "rc": res.get("rc"),
            }
        )
        if not res.get("ok"):
            return {
                "ok": False,
                "chain": True,
                "stopped_at": i,
                "results": reports,
                "summary": "链在第 %d 段停（%s）" % (i + 1, (summary or "")[:60]),
            }
        prev = summary or ""
        _bb.append(prev)
        _scratch_append(ctx.get("root"), _sid, prev)
    return {
        "ok": True,
        "chain": True,
        "stopped_at": None,
        "results": reports,
        "summary": "链 %d 段全部完成" % len(reports),
    }


_S_SUBAGENT = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "description": "子任务：一句话说清目标与验收；子代理没有本会话"
            "历史，需要的信息都要写进 prompt",
        },
        "max_steps": {
            "type": "integer",
            "description": "步数预算 1..%d，默认 8" % SUBAGENT_MAX_STEPS,
        },
        "tools": {
            "type": "array",
            "items": {"type": "string"},
            "description": "允许子代理使用的工具名子集；缺省=除 subagent "
            "外全部。写类工具仍逐条走外层同款审批矩阵",
        },
        "prompts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "并行批量（与 prompt 二选一）：一次派最多 "
            "%d 个子任务同时跑，按输入序返回 results" % SUBAGENT_PARALLEL_MAX,
        },
        "chain": {
            "type": "array",
            "description": "顺序交接链（≤%d 段）：段可是字符串，或"
            "{prompt, needs}（needs 是本段开工前必须已在黑板里的钥匙，"
            "缺钥匙不开工）；段结论进共享黑板，自动带进后面各段；"
            "任一段失败即停并如实报 stopped_at" % SUBAGENT_PARALLEL_MAX,
        },
        "merge": {
            "type": "boolean",
            "description": "prompts 批量时置 true：全部完成后额外一次汇总"
            "调用产出 digest（失败如实留空）",
        },
    },
    "required": ["prompt"],
}


@_tool(
    "subagent",
    RISK_READ,
    "派子代理做独立研究子任务（禁再嵌套；无会话历史；工具子集可收窄"
    "不可放宽；写操作照走外层审批矩阵）。单发 prompt 或 prompts 批量"
    "并行（≤4）。返回 {summary, steps, rc} 或批量 {results}。",
    _S_SUBAGENT,
)
def _t_subagent(a, ctx):
    depth = int(ctx.get("subagent_depth") or 0)
    if depth > 0:
        return {
            "ok": False,
            "executed": False,
            "error": "禁止嵌套：子代理里不能再派子代理（V1 一层，fail-closed）",
        }
    prompt = str(a.get("prompt") or "").strip()
    if not prompt and a.get("prompts") is None and a.get("chain") is None:
        return {
            "ok": False,
            "executed": False,
            "error": "subagent 需要非空 prompt（或 prompts 批量 / chain 链）",
        }
    provider = ctx.get("_provider")
    if provider is None:
        return {
            "ok": False,
            "executed": False,
            "error": "subagent 拿不到 provider（只有主循环内可用）",
        }
    try:
        want = int(a.get("max_steps") or 8)
    except (TypeError, ValueError):
        want = 8
    cap = max(1, want or SUBAGENT_MAX_STEPS)
    raw = a.get("tools")
    if raw is None:
        subset = sorted(n for n in TOOLS if n != "subagent")
    else:
        if not isinstance(raw, list) or not raw:
            return {
                "ok": False,
                "executed": False,
                "error": "tools 必须是非空数组或省略（子代理不能空手）",
            }
        names = set(map(str, raw))
        bad = sorted(names - set(TOOLS))
        if bad:
            return {
                "ok": False,
                "executed": False,
                "error": "未知工具名：%s（可用：%s）"
                % (" ".join(bad), " ".join(sorted(TOOLS))),
            }
        subset = sorted(names - {"subagent"})
    _plist = a.get("prompts")
    if _plist is not None:
        if not isinstance(_plist, list) or not _plist:
            return {
                "ok": False,
                "executed": False,
                "error": "prompts 必须是非空数组（≤%d 个）" % SUBAGENT_PARALLEL_MAX,
            }
        if len(_plist) > SUBAGENT_PARALLEL_MAX:
            return {
                "ok": False,
                "executed": False,
                "error": "并行子代理一次最多 %d 个（得 %d）"
                % (SUBAGENT_PARALLEL_MAX, len(_plist)),
            }
        _plist = [str(x).strip() for x in _plist]
        if not all(_plist):
            return {"ok": False, "executed": False, "error": "prompts 里有空白项"}
        return _subagent_batch(
            _plist, ctx, provider, subset, cap, depth, merge=bool(a.get("merge"))
        )
    _chain = a.get("chain")
    if _chain is not None:
        if not isinstance(_chain, list) or not _chain:
            return {
                "ok": False,
                "executed": False,
                "error": "chain 必须是非空数组（≤%d 段）" % SUBAGENT_PARALLEL_MAX,
            }
        if len(_chain) > SUBAGENT_PARALLEL_MAX:
            return {
                "ok": False,
                "executed": False,
                "error": "chain 一次最多 %d 段（得 %d）"
                % (SUBAGENT_PARALLEL_MAX, len(_chain)),
            }
        _norm = []
        for _x in _chain:
            if isinstance(_x, str):
                _x = _x.strip()
                if not _x:
                    return {
                        "ok": False,
                        "executed": False,
                        "error": "chain 里有空白段",
                    }
                _norm.append((_x, []))
            elif isinstance(_x, dict):
                _pr = str(_x.get("prompt") or "").strip()
                _nd = _x.get("needs") or []
                if (
                    not _pr
                    or not isinstance(_nd, list)
                    or len(_nd) > 8
                    or not all(isinstance(_k, str) and _k.strip() for _k in _nd)
                ):
                    return {
                        "ok": False,
                        "executed": False,
                        "error": "chain 段须是字符串或{prompt, needs[≤8非空串]}",
                    }
                _norm.append((_pr, [str(_k).strip() for _k in _nd]))
            else:
                return {
                    "ok": False,
                    "executed": False,
                    "error": "chain 段须是字符串或{prompt, needs}",
                }
        return _subagent_chain(_norm, ctx, provider, subset, cap, depth)
    sub_ctx = {
        "root": ctx.get("root"),
        "mode": ctx.get("mode"),
        "approver": ctx.get("approver"),
        "sid": ctx.get("sid"),
        "env": ctx.get("env"),
        "snap_dir": ctx.get("snap_dir"),
        "history": [],
        "subagent_depth": depth + 1,
        "tool_subset": subset,
        "plan_lock_off": ctx.get("plan_lock_off"),
        "full_access": ctx.get("full_access"),
        "mode_gate_off": ctx.get("mode_gate_off"),
        "cmd_gate_off": ctx.get("cmd_gate_off"),
    }
    try:
        res = run_agent(prompt, ctx=sub_ctx, provider=provider, max_steps=cap)
    except AgentError as e:
        return {
            "ok": False,
            "executed": False,
            "error": "子代理失败 [%s] %s" % (e.kind, str(e)[:140]),
        }
    summary = str(res.get("answer") or "").strip()
    if not summary and res.get("kind") == "limit":
        summary = "（子代理步数预算用尽：%s）" % str(res.get("error"))[:120]
    return {
        "ok": bool(res.get("ok")),
        "summary": summary or "（子代理没有产出终答）",
        "steps": res.get("steps"),
        "rc": res.get("rc"),
        "limit": res.get("kind") == "limit",
        "tools_used": [u.get("tool") for u in res.get("tools_used") or []],
    }


# ── 终端白名单与运行环境（与 serve.py 同源，不另起一套真相） ────
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
TERM_MAX_OUT = 32768  # 单次工具回灌上限
TERM_TIMEOUT = 20  # run_cmd 默认超时
MAX_TOTAL_SECONDS = int(os.environ.get("AGENTOS_MAX_TOTAL_SECONDS", "0"))  # 0=无上限（用户要求不设工作时间上线）
MAX_REPEAT = 3  # 同一步原样连做这么多次还没进展 → 判 limit
TEXT_REPEAT = 3  # 同一句话（非空终稿内容）连说这么多次还没进展 → 判 limit
TOOL_FAIL_STREAK = 6  # 同一工具连续失败/被拒这么多次还没进展 → 判 limit

_TERM_CACHE = None
_TERM_UNRESOLVED = []


def _term_cmds():
    """命令名 → 真实 argv 前缀。绝对路径优先，PATH 兜底，结果缓存。

    与 serve.py 的 TERM_CMDS 逐字同源：那边是静态模块体，这边是库、要可
    重入，做成懒加载 + 缓存。两边口径一旦漂移，_selftest 的 term_cmds
    用例会红 —— 所以别只改一处。
    """
    global _TERM_CACHE
    if _TERM_CACHE is not None:
        return _TERM_CACHE
    out, bad = {}, []
    for name, cands in TERM_CANDIDATES.items():
        hit = next((c for c in cands if os.path.exists(c)), None)
        if not hit:
            hit = shutil.which(name)
        if not hit:
            bad.append(name)
            continue
        out[name] = [hit, "-c"] if name == "python3" else [hit]
    _TERM_UNRESOLVED = bad
    _TERM_CACHE = out
    return out


# 子进程环境：只留必要变量，**一律不带 token**。run_cmd / git 都走这里，
# 免得哪天工具把 Authorization 打进 stdout，再被回灌进模型上下文。
_CMD_ENV_KEYS = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "USER",
    "LOGNAME",
    "SHELL",
    "TZ",
)
_CMD_ENV = {k: os.environ[k] for k in _CMD_ENV_KEYS if k in os.environ}
_CMD_ENV.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
_CMD_ENV["GIT_PAGER"] = "cat"  # 别让 git log 卡在分页器上
_CMD_ENV["GIT_TERMINAL_PROMPT"] = "0"  # 无人值守时绝不弹凭据
_CMD_ENV["NO_COLOR"] = "1"


def _cap_output(text, limit=MAX_TOOL_OUT):
    """超长截断，但**必须留下「被截断了」这句话**，不静默丢字节。"""
    if text is None:
        text = ""
    text = str(text)
    if len(text) <= limit:
        return text
    tag = "\n...[truncated %d/%d bytes：输出超限，已截断；要全文请缩小范围]"
    keep = max(1, limit - (len(tag) + 24))
    return text[:keep] + (tag % (len(text), limit))


def _cap_result(res):
    """工具结果回灌前的统一瘦身：超长字段截断并显式标注。"""
    if not isinstance(res, dict):
        return res
    out = dict(res)
    for k in ("out", "text"):
        if isinstance(out.get(k), str) and len(out[k]) > MAX_TOOL_OUT:
            out[k] = _cap_output(out[k])
            out["truncated"] = True
    hits = out.get("hits")
    if isinstance(hits, list) and len(hits) > 200:
        out["hits"] = hits[:200]
        out["truncated"] = True
        out["note"] = "hits 只回前 200 条（原 %d 条）" % len(hits)
    return out


# ── 工具执行收口：找表 → 权限裁决 → 执行 → 瘦身 ───────────────
def exec_tool(name, args, ctx):
    """执行一个工具，返回 (result, executed)。

    executed=False 表示这一步**什么都没发生**：未知工具、参数不是对象、
    权限拒绝。故意不抛异常 —— 循环要接着跑，把「为什么没做」原样回给
    模型，让它换路子，而不是让整个任务崩掉。
    注意 executed=True + ok=False 是另一种情况：工具**跑了**，它自己在
    沙箱里说了不（越界路径、old_string 没找到）。这一步有返回、无副作用，
    如实回灌即可 —— 别把它和「压根没跑」混为一谈。
    """
    if not isinstance(name, str) or not name:
        return {"ok": False, "error": "工具名不能为空", "executed": False}, False
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return {
            "ok": False,
            "error": "工具参数必须是 JSON object：%r" % str(args)[:120],
            "executed": False,
        }, False
    spec = TOOLS.get(name)
    if spec is None:
        return {
            "ok": False,
            "error": "未知工具 %r（可用：%s）" % (name, " ".join(sorted(TOOLS))),
            "executed": False,
        }, False
    why = _authorize(spec, args, ctx)
    if why:
        kind = "approval" if ("审批" in why or "plan 模式" in why) else "tool"
        return {
            "ok": False,
            "error": why,
            "tool": name,
            "kind": kind,
            "executed": False,
        }, False
    # hooks 中间件（W4）：PreToolUse 拦截 / 上下文收集；无配置即全关。
    try:
        _hcfg = _hooks_cfg()
    except Exception as e:  # noqa: BLE001
        _hcfg = None
        sys.stderr.write("[hooks] 中间件不可用（忽略）：%s\n" % str(e)[:120])
    if _hcfg:
        try:
            _pre = _hooks_mw().run_event(
                "PreToolUse",
                {"tool": name, "args": args, "root": ctx.get("root")},
                _hcfg,
            )
        except Exception as e:  # noqa: BLE001
            return {
                "ok": False,
                "tool": name,
                "kind": "hook",
                "executed": False,
                "error": "PreToolUse 中间件异常（fail-closed）：%s" % str(e)[:140],
            }, False
        if _pre.get("block"):
            return {
                "ok": False,
                "tool": name,
                "kind": "hook",
                "executed": False,
                "error": "被 PreToolUse 钩子拦截：%s"
                % str(_pre.get("reason") or "")[:200],
            }, False
        for _line in _pre.get("context") or []:
            ctx.setdefault("hook_context", []).append(_line)
        for _e in _pre.get("errors") or []:
            sys.stderr.write("[hooks] %s\n" % _e)
    try:
        res = spec["fn"](args, ctx)
    except AgentError as e:
        return {
            "ok": False,
            "error": "%s：%s" % (e.kind, e.msg),
            "tool": name,
            "executed": False,
        }, False
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "error": "工具内部异常：%s: %s" % (type(e).__name__, str(e)[:200]),
            "tool": name,
            "executed": False,
        }, False
    if not isinstance(res, dict):
        res = {"ok": True, "result": str(res)[:2000]}
    res.setdefault("tool", name)
    res.setdefault("executed", True)
    # hooks PostToolUse（W4）：只记不拦 —— 已发生的事不许被中间件改写成没发生。
    if _hcfg:
        try:
            _post = _hooks_mw().run_event(
                "PostToolUse",
                {
                    "tool": name,
                    "args": args,
                    "ok": bool(res.get("ok")),
                    "root": ctx.get("root"),
                },
                _hcfg,
            )
            for _line in _post.get("context") or []:
                ctx.setdefault("hook_context", []).append(_line)
            for _e in _post.get("errors") or []:
                sys.stderr.write("[hooks] %s\n" % _e)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write("[hooks] Post 中间件异常（仅记）：%s\n" % str(e)[:120])
    return _cap_result(res), True


# ── 主循环：需求 → 模型 → 工具 → 模型 → 最终回答 ──────────────
def build_system_prompt(ctx):
    """系统提示：把边界和证据纪律说死，模型才知道哪些事试了也白试。"""
    root = ctx.get("root") or ROOT
    mode = ctx.get("mode") or "full"
    base = "\n".join(
        [
            "你是 AgentOS 的内置 agent，在一个真实工程仓库里干活。",
            "仓库根：%s" % root,
            "当前权限模式：%s（plan=只读，accept-edits=文件编辑自动放行、"
            "其余要审批，full=写放行但硬阻断清单仍有效）" % mode,
            "",
            "工作方式：",
            "1. 每一步先想清楚要什么证据，再调工具；一个工具一个动作。",
            "2. 工具结果里的 rc / bytes / sha256 是真相，转述时不许改数字。",
            "3. 工具失败或被拒绝时，如实说明哪一步没执行，然后换路子；"
            "不许把没做的事说成做了。",
            "4. 收尾用简体中文写：做了什么（文件:行号级）、验证命令的真实 rc、"
            "还剩什么没做。",
            "",
            "边界（工具层会硬拦，命中了别重试）：",
            "· 只动仓库根内的路径，越界一律拒绝；",
            (
                "· run_cmd 完全权限：任意命令（含 rm/git push，逐条留痕）"
                if ctx.get("full_access")
                else "· run_cmd 自由命令面：任意命令（硬阻断清单默认不可关（full_access 除外））"
                if ctx.get("cmd_gate_off")
                else "· run_cmd 只跑这些只读命令：%s" % " ".join(sorted(_term_cmds()))
            ),
            (
                "· 破坏性动作（rm/git push 等）完全权限下放行，逐条留痕审计；"
                if ctx.get("full_access")
                else "· git push / git reset / 历史重写 / rm 等对外与破坏性动作一律不做，"
                "由用户本人执行；"
            ),
            (
                "· web_fetch 非回环地址：自由面直连"
                if ctx.get("mode_gate_off")
                else "· 非回环地址的 web_fetch 需要审批，headless 下按拒绝处理。"
            ),
            "",
            "可用工具：%s" % " ".join(sorted(TOOLS)),
            "5. 实现类任务先用 gh_search 参考 GitHub 成熟实现"
            "（star≥1000 优先），蒸馏要点再动手；引用来源仓库。",
        ]
    )
    extra = _evo_prompt_lines(ctx.get("evo_rules_path")) + _memory_prompt_lines(ctx)
    # DSP 计划摘要行：有计划才注入（无则零改动）；超长截断，绝不撑爆上下文。
    # 自启指令行：本轮刚自启（ctx["dsp_auto"] 有值）才追加一次，防历史回灌叠加。
    try:
        _dsp_mod = _dsp()
        _plan = (
            _dsp_mod.load_plan(str(ctx.get("sid") or ""))[0] if ctx.get("sid") else None
        )
        if _plan is not None:
            _locked = not ctx.get("plan_lock_off")
            extra = extra + [_dsp_mod.plan_summary(_plan, locked=_locked)]
            if ctx.get("dsp_auto"):
                extra = extra + [
                    _dsp_mod.auto_directive(
                        _plan, {"reasons": ctx.get("dsp_auto")}, locked=_locked
                    )
                ]
    except Exception:  # noqa: BLE001
        pass
    # W4 hooks/skills 注入：钩子 additionalContext + skills 目录段（有才注入）。
    _hk = [str(x) for x in (ctx.get("hook_context") or []) if str(x).strip()]
    if _hk:
        extra = extra + ["钩子注入的上下文：\n" + "\n".join(_hk)[:4000]]
    _sk = _skills_section()
    if _sk:
        extra = extra + [_sk]
    if extra:
        return base + "\n" + "\n".join(extra)
    return base


_TRANSIENT_RE = re.compile(
    r"tls handshake|connection reset|connection refused|i/o timeout|"
    r"unexpected[ _-]?eof|eof occurred|dial tcp|temporar|timed? ?out|overloaded|"
    r"rate.?limit|50[234] |429|remote disconnected|broken pipe|closed connection|"
    r"incomplete ?read|content length|chunked error|decoding error|"
    r"without response|empty reply|aborted",
    re.I,
)

# ── W13 批准即执行 / 诚实收尾（2026-10-14 事故驱动）─────────────────
# 事故：老板说「拍板/全部同意」后，模型因系统提示谎报「写工具锁死」而空转
# 等一个不会来的 /approve，并逐字复读「做了什么/还剩什么」模板三轮。
# 三根钉：①批准词识别（负向环视防「不同意/没批准」误判）；②零证据完成声明
# 打脸（0 次工具调用却说「已落实」= 本系统铁律禁止）；③跨轮复读检测。
_APPR_RE = re.compile(
    r"(?<![不未没])(拍板|同意|批准|开干|动工|开工|"
    r"照此执行|执行吧|干吧)"
)
_APPR_NEG_RE = re.compile(
    r"(没有|并未|还没|未曾|拒绝|不同意|不批准|"
    r"未批准|没批准|先不|暂不|不予)"
)


def _looks_approved(text):
    """是不是「老板批准」：命中批准词且**整句没有否定前缀**（防误批）。
    事故样本负例：「没有批准」「我不同意」「暂不同意」都不许触发。"""
    t = str(text or "")
    return bool(_APPR_RE.search(t)) and not _APPR_NEG_RE.search(t)


_APPR_DIRECTIVE = (
    "[老板已批准] 立即开工执行（不要等审批、不要复述清单）："
    "从上一轮承诺或计划任务的第一项开始，真实调用工具把活"
    "干出来，每步给真实 rc 证据；本轮一个工具都不调用=失败。\n"
    "原话："
)
_APPR_RETRY = (
    "\n\n[再次强调] 上一轮你没有任何工具调用。现在就调用工具"
    "开工（读目标文件/跑验证命令都算），不要再写「做了什么/"
    "还剩什么」的空清单。"
)
_CLAIM_RE = re.compile(
    r"(已全面落实|全面落实|已落实|已全部完成|已部署|"
    r"部署完成|无需部署|已修复完成|已完成提升|全部执行完毕)"
)


def _claim_no_evidence(ans, n_tools):
    """零证据完成声明：0 次工具调用却声称「已落实」类动作 → True。"""
    return bool(int(n_tools) == 0 and ans and _CLAIM_RE.search(str(ans)))


def _is_stale_reply(prev, ans):
    """跨轮复读：本轮终答与上一轮近乎逐字相同（相似度 ≥0.9）→ True。"""
    if not prev or len(str(ans)) < 40:
        return False
    import difflib

    return difflib.SequenceMatcher(None, str(prev), str(ans)).ratio() >= 0.9


def _needs_approve_retry(approved_turn, tools_used, interrupted):
    """批准轮零工具调用 = 没真开工 → 需要自动重试一轮（有界一次）。
    被打断（Esc）不重试：用户主动停的，重试等于无视用户意图。"""
    return bool(approved_turn) and not tools_used and not interrupted


def run_agent(
    prompt,
    ctx=None,
    provider=None,
    stream=False,
    on_event=None,
    max_steps=None,
    cancel=None,
):
    """跑一次「需求 → 若干工具 → 最终回答」的循环，返回结果 dict。

    成功的形状：{"ok": True, "kind": "ok", "answer": ..., "steps": n,
                 "messages": [...], "usage": {...}, "elapsed": ...}
    预算耗尽的形状：{"ok": False, "kind": "limit", ...}（终答为空，如实说）
    端点/参数问题抛 AgentError，由调用方按 kind 转退出码。

    ctx 认这几个键：root / mode / approver / env / history / max_steps。
    history 是上一轮的 messages 列表，用来在交互 REPL 里保持上下文；
    跑完会被写回 ctx["history"]，超长时只保留 system + 最近 40 条。
    """
    _outer = ctx if isinstance(ctx, dict) else None  # history 写回要送到调用方
    ctx = dict(ctx or {})
    ctx["root"] = os.path.realpath(ctx.get("root") or ROOT)
    ctx.setdefault("mode", "full")
    if "full_access" not in ctx:
        try:
            ctx["full_access"] = _full_access(_read_agentos_config())
        except Exception:
            ctx["full_access"] = False
    text = (prompt or "").strip()
    if not text:
        raise AgentError("usage", "空需求：run_agent 需要一句非空的任务描述")
    # DSP 大任务自启：BIG 需求 + 本 sid 无计划 → 自动建骨架计划（未审批）。
    # fail-open 包住：分析器/落盘任何异常都不许阻断主循环；自启只是「先立账」，
    # 写锁与白名单仍由 _authorize 硬执行。自启事件经 on_event 上报（过程可见）。
    try:
        _dsp_mod = _dsp()
        _sid0 = str(ctx.get("sid") or "")
        if _sid0 and not ctx.get("dsp_noauto"):
            _plan0, _ana0 = _dsp_mod.auto_plan(text, _sid0, ctx["root"])
            if _plan0 is not None:
                ctx["dsp_auto"] = _ana0.get("reasons", [])
                # 自启即指派首个可做任务：监督（白名单/doom/记账）本轮就生效，
                # 不等用户先 /tasks。
                try:
                    _nx0 = _dsp_mod.next_task(_plan0)
                    if _nx0:
                        ctx["dsp_task"] = _nx0
                except Exception:  # noqa: BLE001
                    pass
                if callable(on_event):
                    on_event(
                        {
                            "type": "note",
                            "text": "大任务自动建计划（%s），先精修 accept"
                            " 再 /approve" % "；".join(_ana0.get("reasons", []))[:120],
                        }
                    )
    except Exception:  # noqa: BLE001
        pass
    try:
        cap = int(max_steps or ctx.get("max_steps") or MAX_STEPS)
    except (TypeError, ValueError):
        cap = MAX_STEPS
    cap = max(1, min(MAX_STEPS, cap))
    if provider is None:
        # resolve_provider 给的是「端点描述」（纯 dict，便于 env 注入与门做
        # 证伪）；这里必须再包一层 Provider：run_agent 后面调的是
        # provider.chat(...)。少了这一层，dict 上没有 chat，一条真需求会在
        # 第 261 行抛 AttributeError —— 而那偏偏是 selftest 用桩对象绕开
        # 的那条路，所以 23/23 全绿也照样看不出来。
        provider = Provider(resolve_provider(ctx.get("env")))
    ctx["_provider"] = provider  # subagent 工具复用它派子代理（在副本内，不外泄）
    _sauto = _skills_auto(text)
    if _sauto:
        sysmsg = {
            "role": "system",
            "content": build_system_prompt(ctx) + "\n\n" + _sauto,
        }
    else:
        sysmsg = {"role": "system", "content": build_system_prompt(ctx)}
    base = ctx.get("history")
    messages = list(base) if isinstance(base, list) and base else [sysmsg]
    if not messages or messages[0].get("role") != "system":
        messages.insert(0, sysmsg)
    messages = messages + [{"role": "user", "content": text}]
    tools = tool_defs()
    t0 = time.time()
    usage = {}
    used = []
    last_sig = None
    repeat = 0
    _last_text = ""
    _text_rep = 0
    _ok_sigs = set()
    _fail_tool = ""
    _fail_n = 0
    _transient_n = 0  # 本轮累计的「端点瞬时错重试」次数
    _TRANSIENT_MAX = 6  # 上限：网络抖动可以骑，死循环不行
    # 会话中毒轮换走独立预算：与 SSL 同池会互相烧光（实测第六圈死法——
    # SSL 六次把池子吃完，中毒轮换没得花，裸死）。
    _sess_n = 0
    _SESS_MAX = 3
    step = 0
    _total_steps = 0
    while True:
        step += 1
        _total_steps += 1
        if step > cap:
            # 独立续跑：有真实进展（新的成功动作）且没在打转 → 压缩上下文
            # 后自动接力（至多 2 轮），而不是直接判死刑。没进展才停。
            _chain = 0
            try:
                _chain = int(ctx.get("_chain") or 0)
            except (TypeError, ValueError):
                _chain = 0
            if _ok_sigs and _text_rep < TEXT_REPEAT:
                try:
                    _nh, _nd, _dg = _compact_history_v2(
                        messages, keep=40, provider=provider
                    )
                except Exception:  # noqa: BLE001
                    _nh, _nd, _dg = messages[-40:], 0, False
                messages = _nh + [
                    {
                        "role": "user",
                        "content": "（独立续跑第 %d 轮：前面已成功"
                        "执行 %d 个不同动作，接着干，"
                        "不要重复已做过的动作）" % (_chain + 1, len(_ok_sigs)),
                    }
                ]
                ctx["_chain"] = _chain + 1
                if _outer is not None:
                    _outer["_chain"] = _chain + 1
                step = 0
                if callable(on_event):
                    on_event(
                        {
                            "type": "note",
                            "text": "预算续跑第 %d 轮（摘要=%s）"
                            % (_chain + 1, "有" if _dg else "旧口径"),
                        }
                    )
                continue
            return {
                "ok": False,
                "kind": "limit",
                "answer": None,
                "error": "达到 step 预算 %d%s：需求没在预算内收敛；"
                "把它拆小，或显式调大 --max-steps（上限 %d）"
                % (
                    cap,
                    ("（已独立续跑 %d 轮、共 %d 步）" % (_chain, _total_steps - 1))
                    if _chain
                    else "",
                    MAX_STEPS,
                ),
                "last_text": (_last_text or "")[:200],
                "steps": step - 1,
                "messages": messages,
                "tools_used": used,
                "elapsed": round(time.time() - t0, 2),
            }
        # 老板监督通道：步骤边界收指令（有则注入本轮、注明来源、只进一次）。
        _stq = _steer_take(ctx)
        if _stq:
            for _s in _stq:
                messages.append(
                    {"role": "user", "content": "[老板指令·监督纠正] %s" % _s}
                )
            if callable(on_event):
                on_event(
                    {
                        "type": "note",
                        "text": "收到老板指令 %d 条（已注入本步骤边界）" % len(_stq),
                    }
                )
        if MAX_TOTAL_SECONDS > 0 and time.time() - t0 > MAX_TOTAL_SECONDS:
            return {
                "ok": False,
                "kind": "limit",
                "answer": None,
                "error": "达到墙上时钟预算 %ds（已跑 %ds）：拆小需求，"
                "或下轮再来" % (MAX_TOTAL_SECONDS, int(time.time() - t0)),
                "steps": step - 1,
                "messages": messages,
                "tools_used": used,
                "elapsed": round(time.time() - t0, 2),
            }
        if callable(on_event):
            on_event({"type": "step", "step": step})
        if cancel is not None and cancel():
            raise AgentError("interrupted", "已打断（Esc），上下文保留")
        try:
            comp = provider.chat(
                messages, tools, stream=stream, on_event=on_event, cancel=cancel
            )
        except AgentError as e:
            if e.kind == "interrupted":
                raise
            # 有界瞬时重试（蒸馏自 gemini-cli 的 retry 思想）：只认传输层
            # 瞬时词表、只非流式——流式中途重放文本是假丑。
            #
            # 顺序纪律（实测第六圈死法）：**中毒分支必须排在词表门前面**。
            # 会话脱钩的 400（No tool call found）不含传输词，排在后面就被
            # `not _TRANSIENT_RE.search → raise` 一刀毙命，rotate 永远轮不
            # 到。且走**独立预算**：与 SSL 同池会互相烧光。
            if _SESSION_BREAK_RE.search(str(e)) and not stream:
                # 循环 reroll：每次轮换后再捕一次（预算 _SESS_MAX 内），
                # 命中断流/打断/非中毒错都原样抛出，不吞。
                while True:
                    if _sess_n >= _SESS_MAX:
                        raise
                    _sess_n += 1
                    # 先留证再自救：完整净荷 dump（黑盒 reroll 治标不治本）
                    try:
                        _tr = os.path.join(
                            os.path.dirname(
                                os.path.dirname(os.path.abspath(__file__))
                            ),
                            "work",
                            "llm_400_trace.json",
                        )
                        with open(_tr, "w", encoding="utf-8") as _tf:
                            json.dump(
                                {"error": str(e)[:500], "messages": messages},
                                _tf,
                                ensure_ascii=False,
                                indent=1,
                            )
                    except Exception:  # noqa: BLE001  留证失败不挡自救
                        pass
                    provider.rotate_session()
                    if callable(on_event):
                        on_event(
                            {
                                "type": "note",
                                "text": "网关会话脱钩，轮换会话 id 重试"
                                "（%d/%d）" % (_sess_n, _SESS_MAX),
                            }
                        )
                    time.sleep(1.0)
                    # 会话中毒的真正死因：轮换会话 id 只换头，净荷里还带着上一轮
                    # 的 tool_call_id 链 → 新会话里找不到那些 call_id → 又 400。
                    # 所以每次轮换后把消息裁到「system + 最近 8 条」，切掉最老的
                    # 工具交换对（它们大概率就是中毒源）；保留最近上下文。
                    if len(messages) > 9:
                        _sys = [m for m in messages if m.get("role") == "system"][:1]
                        _tail = messages[-8:]
                        messages = _sys + _tail
                    try:
                        comp = provider.chat(
                            messages, tools, stream=stream, on_event=on_event,
                            cancel=cancel,
                        )
                        break
                    except AgentError as e2:
                        if e2.kind == "interrupted" or not _SESSION_BREAK_RE.search(
                            str(e2)
                        ):
                            raise
                        e = e2  # 还是中毒：预算 -1，继续轮换
            else:
                while True:
                    if (
                        stream
                        or not _TRANSIENT_RE.search(str(e))
                        or _transient_n >= _TRANSIENT_MAX
                    ):
                        raise
                    _transient_n += 1
                    _tw = min(2.0 * (2 ** (_transient_n - 1)), 12.0)
                    if callable(on_event):
                        on_event(
                            {
                                "type": "note",
                                "text": "瞬时错误，%.0fs 后重试（第 %d/%d 次）：%s"
                                % (_tw, _transient_n, _TRANSIENT_MAX, str(e)[:80]),
                            }
                        )
                    time.sleep(_tw)
                    try:
                        comp = provider.chat(
                            messages, tools, stream=stream, on_event=on_event,
                            cancel=cancel,
                        )
                        break
                    except AgentError as e2:
                        if e2.kind == "interrupted":
                            raise
                        if not _TRANSIENT_RE.search(str(e2)):
                            raise
                        e = e2
        if isinstance(comp.get("usage"), dict) and comp["usage"]:
            usage = comp["usage"]
        calls = comp.get("tool_calls") or []
        msg = {"role": "assistant", "content": comp.get("content") or ""}
        if calls:
            msg["tool_calls"] = calls
        messages.append(msg)
        # 打转守卫①（W11.1，实测事故）：同一句话（非空终稿内容）连续重复
        # ≥TEXT_REPEAT 步还没进展 —— 先于烧预算停。事故复现：模型嘴上重复
        # 同一句、手上换着参数连调 run_cmd，旧守卫（只查同工具同参）全漏过。
        _ct = msg["content"].strip()
        if _ct:
            if _ct == _last_text:
                _text_rep += 1
            else:
                _text_rep, _last_text = 1, _ct
            if _text_rep >= TEXT_REPEAT:
                return {
                    "ok": False,
                    "kind": "limit",
                    "answer": None,
                    "error": "同一句话连续重复 %d 步没进展（%r…）："
                    "判为原地打转，停（多为端点抖动或模型卡壳，"
                    "可重试或换端点）" % (_text_rep, _ct[:60]),
                    "steps": step,
                    "messages": messages,
                    "tools_used": used,
                    "elapsed": round(time.time() - t0, 2),
                }
        if not calls:
            if len(messages) <= 41:
                ctx["history"] = messages[-40:]
            else:
                # v2（auto-compact 的下一档）：丢弃前先让 provider 生成
                # 事实摘要 digest 塞回头部；失败自动回退旧口径（逐字等价）。
                ctx["history"], _, _dg = _compact_history_v2(
                    messages, keep=40, provider=provider
                )
                # 自压缩提示：压缩是既定行为，让它肉眼可见（含摘要有无）；
                # note 类型 headless printer 忽略。
                if callable(on_event):
                    on_event(
                        {
                            "type": "note",
                            "text": "上下文自动压缩 %d→%d 条（摘要=%s）"
                            % (
                                len(messages),
                                len(ctx["history"]),
                                "有" if _dg else "旧口径回退",
                            ),
                        }
                    )
            # 契约写回：run_agent 内部对 ctx 做了浅拷（防 root/mode 归一化
            # 回写调用方），但 docstring 承诺「跑完写回 ctx["history"]」——
            # 不回写 = REPL 多轮上下文与 compact v2 摘要全部蒸发（W1 门首跑
            # 实测抓出：digest 真发了请求，history 却停在空表）。只回写
            # history 这一个键，其余照旧留在副本里。
            if _outer is not None:
                _outer["history"] = ctx["history"]
            _ans = msg["content"] or ""
            _empty = not _ans.strip()
            return {
                "ok": True,
                "kind": "ok",
                "steps": step,
                "answer": _ans,
                "empty_answer": _empty,
                "error": (
                    "模型返回空内容（端点抖动/模型卡壳）：本轮没有"
                    "可交付答案，可重试或换端点"
                    if _empty
                    else None
                ),
                "finish_reason": comp.get("finish_reason"),
                "messages": messages,
                "usage": usage,
                "tools_used": used,
                "elapsed": round(time.time() - t0, 2),
                "rc": 0,
            }
        for tc in calls:
            fn = tc.get("function") or {}
            name = str(fn.get("name") or "").strip()
            raw = fn.get("arguments")
            if raw in (None, ""):
                raw = "{}"
            try:
                args = json.loads(raw)
            except ValueError:
                res = {
                    "ok": False,
                    "executed": False,
                    "tool": name,
                    "error": "工具参数不是合法 JSON（这一步未执行）：%r"
                    % str(raw)[:200],
                }
            else:
                sig = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
                if sig == last_sig:
                    repeat += 1
                else:
                    repeat, last_sig = 0, sig
                if repeat >= MAX_REPEAT:
                    return {
                        "ok": False,
                        "kind": "limit",
                        "answer": None,
                        "error": "同一步原样连做 %d 次还没进展（%s）："
                        "判为原地打转，停" % (repeat + 1, name),
                        "steps": step,
                        "messages": messages,
                        "tools_used": used,
                        "elapsed": round(time.time() - t0, 2),
                    }
                # DSP doom 升级：有计划+当前任务时，归一化同参第 3 次不判
                # limit，而是把「已打转、需人看一眼」如实回灌，让模型转 ask。
                # 全局 MAX_REPEAT 照旧（无计划时行为零改动）。
                try:
                    _dsp_dw = ctx.get("dsp_doom")
                    if _dsp_dw is None:
                        _dsp_dw = _dsp().DoomWatch()
                        ctx["dsp_doom"] = _dsp_dw
                    _dsp_verdict = None
                    if ctx.get("sid") and ctx.get("dsp_task"):
                        _dsp_verdict = _dsp_dw.note(ctx.get("dsp_task"), name, args)
                    if _dsp_verdict == "ask" and callable(on_event):
                        on_event(
                            {
                                "type": "note",
                                "text": "同参第 3 次（%s）：已打转，请确认"
                                "后再试，别原样重发" % name,
                            }
                        )
                except Exception:  # noqa: BLE001
                    pass
                if callable(on_event):
                    # 过程可见性：工具被调用的瞬间就上报（不等结果）。
                    on_event({"type": "tool_call", "tool": name, "args": args})
                res, _ex = exec_tool(name, args, ctx)
                if (
                    bool(res.get("executed"))
                    and bool(res.get("ok"))
                    and sig not in _ok_sigs
                ):
                    _text_rep = 0  # 新的成功动作=在进展，嘴上重复不算打转
                    _ok_sigs.add(sig)
            # 打转守卫②：同一工具连续失败/被拒 ≥TOOL_FAIL_STREAK 次 → 停。
            # 「执行了但失败」与「被拒/未执行」都算不进步；换工具或任一次
            # 成功即清零（正常"试错→改进"的连败间通常夹着别的动作，不受影响）。
            _good = bool(res.get("executed")) and bool(res.get("ok"))
            if _good:
                _fail_tool, _fail_n = "", 0
            else:
                if name == _fail_tool:
                    _fail_n += 1
                else:
                    _fail_tool, _fail_n = name, 1
            used.append(
                {
                    "tool": name,
                    "ok": bool(res.get("ok")),
                    "executed": bool(res.get("executed")),
                    "error": str(res.get("error") or "")[:160],
                }
            )
            if _fail_n >= TOOL_FAIL_STREAK:
                return {
                    "ok": False,
                    "kind": "limit",
                    "answer": None,
                    "error": "同一工具 %r 连续失败/被拒 %d 次（最后：%s）："
                    "判为原地打转，停"
                    % (name, _fail_n, str(res.get("error") or "")[:120]),
                    "steps": step,
                    "messages": messages,
                    "tools_used": used,
                    "elapsed": round(time.time() - t0, 2),
                }
            # DSP 任务记账：有计划+当前任务时，每步写回 steps_used/evidence，
            # ok+证据行 → done，超 TASK_BUDGET → stuck。落盘失败静默（增益层），
            # 但状态流转如实经 on_event 上报。
            try:
                if ctx.get("sid") and ctx.get("dsp_task"):
                    _dsp_m = _dsp()
                    _pl, _e = _dsp_m.load_plan(str(ctx.get("sid")))
                    if _pl is not None:
                        _ev = "rc=%s %s" % (
                            "0" if res.get("ok") else "1",
                            str(res.get("error") or res.get("result") or "")[:120],
                        )
                        _st = _dsp_m.task_record_step(
                            _pl, ctx.get("dsp_task"), bool(res.get("ok")), _ev
                        )
                        _dsp_m.save_plan(_pl, str(ctx.get("sid")))
                        if callable(on_event) and _st in ("done", "stuck"):
                            on_event(
                                {
                                    "type": "note",
                                    "text": "任务 %s → %s（%s）"
                                    % (ctx.get("dsp_task"), _st, _ev[:80]),
                                }
                            )
                            if _st == "done":
                                _nx = _dsp_m.next_task(_pl)
                                if _nx:
                                    ctx["dsp_task"] = _nx
            except Exception:  # noqa: BLE001
                pass
            if callable(on_event):
                on_event(
                    {
                        "type": "tool",
                        "tool": name,
                        "ok": bool(res.get("ok")),
                        "executed": bool(res.get("executed")),
                        "summary": _tool_summary(name, args, res),
                    }
                )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(
                        tc.get("id") or ("call_%s_%d" % (name or "x", step))
                    ),
                    "content": json.dumps(_cap_result(res), ensure_ascii=False),
                }
            )


def _no_fallback():
    """~/.agentos/config.toml [llm] no_fallback=true → 主端点失败不换本地。

    只认字面 "true"（llm 表存字符串，与 gh.write_enabled 同口径）；
    缺键/其它值 = 默认行为（交互 REPL 回退本地一次）。
    """
    try:
        v = (_read_agentos_config().get("llm") or {}).get("no_fallback")
    except Exception:  # noqa: BLE001  配置读坏了不提权也不锁死，默认行为
        return False
    return str(v) == "true"


def _startup_local_backend():
    """REPL 启动期的本机离线后端兜底。返回 (cfg, blocked)。

    no_fallback=true 时 blocked=True 且 **probe 零调用** —— 「不能用本地
    模型」要连启动期兜底一起堵：只堵中途回退、启动时照样换本地，等于
    「云端挂了就静默降级」，正是用户不许的那件事。
    """
    if _no_fallback():
        return None, True
    return _probe_ollama(), False


def run_agent_with_fallback(
    prompt,
    ctx=None,
    provider=None,
    stream=False,
    on_event=None,
    allow_fallback=True,
    probe=None,
    cancel=None,
    retry_main=False,
    sleep=None,
):
    """run_agent 的一层包装：主端点抛 provider 错时，若本机离线后端可用，
    换过去重试一次。返回 (结果 dict, 最终用的 Provider)。

    为什么只做一层、只做一次：回退是给**交互 REPL** 的「开箱即用」兜底，
    不是给 CI 的假绿。headless 调用方传 allow_fallback=False（默认就是
    False 在 _run_once 里）—— 没端点就该 rc=3，不许悄悄换本地模型把红门
    洗绿。换不了就原样抛，绝不吞错。

    retry_main=True（配 `agentos config set llm.no_fallback true`）时反过来：
    主端点失败**不换本地**，而是指数退避（1/2/4…封顶 30s）一直重试主端点，
    只认 cancel（REPL 里 Esc）或 KeyboardInterrupt 为出口。probe（本地后端）
    全程不碰 —— 「主端点失败就一直等主端点」是字面保证，不是重试几次后
    悄悄降级。sleep 可注入，只为让自检不真睡。

    probe 可注入（默认 _probe_ollama），只为让自检能拿桩端点证伪回退路径，
    不碰真 11434。
    """
    _sleep = sleep or time.sleep
    try:
        res = run_agent(
            prompt,
            ctx=ctx,
            provider=provider,
            stream=stream,
            on_event=on_event,
            cancel=cancel,
        )
        return res, provider
    except AgentError as e:
        if e.kind != "provider" or (not allow_fallback and not retry_main):
            raise
        if retry_main and not allow_fallback:
            n = 0
            while True:
                if callable(cancel) and cancel():
                    raise
                wait = min(1 * (2**n), 30)
                sys.stderr.write(
                    "[主端点重试] 第 %d 次失败（%s），%.0fs 后重试主端点"
                    "（no_fallback：不换本地，Esc 可停）\n"
                    % (n + 1, str(e)[:100], wait)
                )
                try:
                    _sleep(wait)
                except KeyboardInterrupt:
                    raise
                if callable(cancel) and cancel():
                    raise
                try:
                    res = run_agent(
                        prompt,
                        ctx=ctx,
                        provider=provider,
                        stream=stream,
                        on_event=on_event,
                        cancel=cancel,
                    )
                    return res, provider
                except AgentError as e2:
                    if e2.kind != "provider":
                        raise
                    e, n = e2, n + 1
            # 不可达：循环只从 cancel / 非 provider 错 / 成功三处出口
        cfg = (probe or _probe_ollama)()
        if not cfg:
            raise
        if provider is not None and provider.source == str(cfg.get("source")):
            raise  # 已经在用这个后端了，别自转
        sys.stderr.write(
            "[回退] 主端点失败（%s），改用本机离线后端 %s\n"
            % (str(e)[:140], cfg.get("model") or "?")
        )
        if ctx is not None:
            # 自进化信号：被兜底掩盖≠没发生——主端点这次失败是真实事件，
            # 如实挂到 ctx 上让 REPL 记一条 ok=False 信号（挖掘原料）。
            ctx["last_fallback"] = {"kind": e.kind, "err": str(e)[:140]}
        p2 = Provider(cfg)
        res = run_agent(prompt, ctx=ctx, provider=p2, stream=stream, on_event=on_event)
        return res, p2


# ── 命令行 ────────────────────────────────────────────────────
EPILOG = """\
常用例子：
  agentos agent                          # 交互 REPL（默认 full）
  agentos agent --mode plan              # 只读规划，任何写操作一律拒绝
  agentos agent -p '跑一遍门链并汇报真实 rc'    # headless，适合脚本
  agentos agent --root /path/to/repo -p '...'   # 换仓库
  agentos agent --check                  # 自检（本地桩端点，不碰真模型）
  agentos agent --continue               # 接着上一次 agent 会话聊
  agentos agent --resume agent-2026...   # 续跑指定会话（/sessions 列 id）
  agentos agent --provider opencode -p '...'   # OpenCode zen 端点（需 AGENTOS_LLM_API_KEY）

端点三选一：`--provider <名字>`（内置命名端点，当前 opencode）/
`AGENTOS_LLM_BASE_URL` 直指任意 OpenAI 兼容口 / `~/.codex/config.toml` 复用。
REPL 内命令：/help 帮助            /exit 退出（Ctrl-D 同效）
            /mode <plan|accept-edits|full>  切换权限模式
            /status                        会话/模式/仓库/模型/上下文
            /model [名]                    看或换模型（下一轮生效）
            /cost                          累计 token 用量（端点自报）
            /compact [聚焦]                压缩上下文（保留 system + 最近 20；有焦点词
                                           先让模型做事实摘要塞回头部，失败回退旧口径）
            /rewind [--list|<N>]           列轮次 / history 回到第 N 轮末（文件层
                                           不动；写操作回退用 /undo）
            /skill [名]                    列技能 / 把某技能正文注入下一轮上下文
                                           （技能目录：~/.agentos/skills 与本仓
                                           .agentos/skills；hooks 同理配
                                           ~/.agentos/hooks.json，重启生效）
            /branch [注记]                 另开会话分支（原会话不动、可回去）
            /verify                        验本会话计划的真完成（不提前 done）
            /sessions                      列会话（标出当前）；可 branch 进出
            /steer "指令"                  投一条老板监督指令（步骤边界注入，
                                           只配送不裁决）；agentos steer 同效
            /watch                         一屏监督面（计划/指令/会话尾）
            安全闸（默认自由面）            agentos config set agent.plan_lock
                                           true 才开计划锁；agent.mode_gate
                                           true 才开权限模式；agent.cmd_gate
                                           true 才收窄命令白名单（硬阻断清单
                                           不可关；重启生效）
            /clear  /reset                 清空上下文（保留模式）
            /tools                         列出工具
            /diff                          本仓库当下的改动（git）
            /init                          在仓库根放一份起步 AGENTS.md
            /sessions                      列本机 agent 会话 id
            /plan [<目标>]                 无参看计划；有参建新计划（任务树+判据）
            /approve                       人审通过计划（解锁写工具执行）
            /tasks [id]                    看任务树 / 切换当前任务
            /block [原因]                  偏离即 BLOCK 落盘（原因+已做+建议）
            /goal [<目标>|run|resume]      自主执行：有参出菱形 DAG 计划（里程碑+
                                           判据）；/approve 后 /goal run 全流程
                                           无人干预跑到收口（判据真跑、失败自救
                                           ≤2 次、断点可续、假绿不留存）
            !<命令>                        跑一条 shell（真实 rc；默认自由面，
                                           硬阻断清单默认不可关（full_access 除外））
            @<路径>                        把文件内容内联进这一轮需求
                                           （@ 开头的路径 Tab 可补全）
            行尾 \\ 回车                    续行多行输入（连续 \\ 继续续行）
开箱即用：REPL 主端点连不通（403/400/不可达）时自动回退到本机 Ollama
（若在跑），stderr 会如实打印 [回退]。headless（-p）不回退：没端点就 rc=3。
退出码：0=给出了最终回答；2=用法/预算；3=基础设施（provider/HTTP/harness）
"""


def _build_parser():
    p = argparse.ArgumentParser(
        prog="agent_core",
        description="AgentOS 内置 agent 的内核：模型与真实工具之间的循环"
        "（只用标准库，不新开一套真相）",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "prompt", nargs="*", metavar="PROMPT", help="需求；给了一句就是 headless 模式"
    )
    p.add_argument(
        "-p",
        "--print",
        dest="print_mode",
        action="store_true",
        help="非交互：跑完把最终回答打到 stdout 后退出",
    )
    p.add_argument(
        "--mode", default="full", choices=list(MODES), help="权限模式，默认 full"
    )
    p.add_argument("--root", default=ROOT, help="仓库根，默认本文件上一级")
    p.add_argument(
        "--max-steps",
        type=int,
        default=MAX_STEPS,
        help="单任务最多几步，默认 %d，硬上限 %d" % (MAX_STEPS, MAX_STEPS),
    )
    p.add_argument(
        "--stream",
        action="store_true",
        help="边收边打（端点不支持流式时自动退化为整包）",
    )
    p.add_argument(
        "--no-stream",
        dest="no_stream",
        action="store_true",
        help="关掉 REPL 的流式输出（REPL 默认就开流式）",
    )
    p.add_argument(
        "--yes", action="store_true", help="审批一律点头（硬阻断清单仍然有效）"
    )
    p.add_argument(
        "--continue",
        dest="cont",
        action="store_true",
        help="续跑最近一份 agent 会话（work/agent_sessions/agent-*）",
    )
    p.add_argument(
        "--resume",
        default="",
        metavar="ID",
        help="续跑指定 agent 会话（/sessions 列 id）",
    )
    p.add_argument(
        "--provider",
        default="",
        metavar="NAME",
        choices=sorted(NAMED_PROVIDERS),
        help="命名 provider：%s；给了就以它为准" % " / ".join(sorted(NAMED_PROVIDERS)),
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="跑自检：本地桩端点 + 真实小文件，不碰真模型真网",
    )
    p.add_argument("--version", action="version", version="agent_core %s" % VERSION)
    return p


_RC_OF = {"usage": 2, "limit": 2, "provider": 3, "no-provider": 3}


def _rc_of_error(kind):
    """失败 kind → 退出码。全项目同一套口径，见模块 docstring。"""
    return _RC_OF.get(kind, 3)


def _make_approver(args):
    """审批者。没有 TTY 就返回 None —— _gate_write 把 None 当拒绝，
    这正是 headless 想要的行为：宁可拒绝，不能偷偷放行。"""
    if args.mode in ("full", "plan"):
        return None
    if args.yes:

        def _yes(name, a, why):
            sys.stderr.write("[自动批准 --yes] %s\n" % why)
            return True

        return _yes
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        return None

    def _ask(name, a, why):
        sys.stderr.write(
            "\n需要批准：%s\n  工具=%s  参数=%s\n  批准？[y/N] "
            % (why, name, json.dumps(a, ensure_ascii=False)[:300])
        )
        sys.stderr.flush()
        try:
            line = sys.stdin.readline()
        except Exception:  # noqa: BLE001
            return False
        return line.strip().lower() in ("y", "yes", "是")

    return _ask


def _event_printer(stream, seen=None):
    if not stream:
        return None

    def _ev(ev):
        t = ev.get("type")
        if t == "delta":
            if isinstance(seen, dict):
                seen["delta"] = True
            sys.stdout.write(ev.get("text") or "")
            sys.stdout.flush()
        elif t == "tool":
            # 文本逐字不动（README/量具引的就是这行原话），皮只包色码。
            sys.stdout.write(
                _c(
                    "dim",
                    "\n[工具 %s → %s%s]\n"
                    % (
                        ev.get("tool"),
                        "ok" if ev.get("ok") else "失败/被拒",
                        "" if ev.get("executed") else "（未执行）",
                    ),
                )
            )
            sys.stdout.flush()

    return _ev


# ── 过程可见性（Claude Code/Codex 式）：转轮 + 工具即调即显 ─────────────
# 用户诉求：干活时不要什么都不显示。模型思考/跑工具的静默段每 0.12s 重画
# 「✻ 状态… Ns · Ctrl-C 打断」；工具被调用的瞬间就上屏「⏺ 工具(参数)」，
# 结果随后 ⎿ 形态给出。只在交互 TTY 起线程；headless/管道零改动零输出。
class _TurnSpinner(object):
    """回合转轮。生命周期 = 一个 REPL 回合：start() … stop()。"""

    SHIMMER = "✻✽✶✳✢·"

    def __init__(self, enabled, colorize):
        self.enabled = bool(enabled)
        self._c = colorize
        self._stop = threading.Event()
        self._halt = threading.Event()
        self._state = "思考中"
        self._t0 = time.time()
        self._th = None

    def start(self):
        if self.enabled:
            self._th = threading.Thread(target=self._loop, daemon=True)
            self._th.start()
        return self

    def set(self, state):
        self._state = state or self._state

    def wake(self):
        self._halt.clear()

    def halt(self):
        self._halt.set()

    def pause(self):
        """清掉转轮行（只在它正显示时），给真行让出干净画布。"""
        if self.enabled and not self._halt.is_set():
            sys.stderr.write("\r\x1b[K")
            sys.stderr.flush()

    def stop(self):
        self._stop.set()
        if self.enabled:
            sys.stderr.write("\r\x1b[K")
            sys.stderr.flush()

    def _loop(self):
        i = 0
        while not self._stop.wait(0.12):
            if self._halt.is_set():
                continue
            s = self.SHIMMER[i % len(self.SHIMMER)]
            i += 1
            el = int(time.time() - self._t0)
            sys.stderr.write(
                "\r\x1b[K"
                + self._c("brand", "%s %s… %ds · Ctrl-C 打断" % (s, self._state, el))
            )
            sys.stderr.flush()


class _EscWatcher(object):
    """回合期 stdin 监听（蒸馏自 Claude Code 的 Esc 打断）。

    只在交互 TTY 启用；用 cbreak（关 ICANON/ECHO、留 ISIG）而非全 raw：
    Ctrl-C 仍走 SIGINT 原路，\n 仍由终端补 \r，箭头等 CSI 序列按噪声吞掉。
    Esc 单独按下（80ms 无后续）→ cancel Event 置位，SSE 行间/步间即中断。
    """

    _CSI_FINAL = range(0x40, 0x7F)

    def __init__(self, enabled, cancel):
        self.enabled = bool(enabled)
        self.cancel = cancel
        self._stop = threading.Event()
        self._old = None
        self._th = None

    def start(self):
        if not self.enabled:
            return self
        fd = sys.stdin.fileno()
        try:
            self._old = termios.tcgetattr(fd)
            new = list(self._old)
            new[3] &= ~(termios.ICANON | termios.ECHO)
            new[6][termios.VMIN] = 1
            new[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, new)
        except (termios.error, OSError):
            self._old = None
            return self
        import select as _sel

        self._th = threading.Thread(target=self._loop, args=(fd, _sel), daemon=True)
        self._th.start()
        return self

    def _loop(self, fd, sel):
        while not self._stop.is_set():
            try:
                r, _, _ = sel.select([fd], [], [], 0.1)
            except OSError:
                return
            if not r:
                continue
            try:
                b = os.read(fd, 1)
            except OSError:
                return
            if b == b"\x1b":
                rr, _, _ = sel.select([fd], [], [], 0.08)
                if not rr:
                    self.cancel.set()
                    continue
                try:
                    os.read(fd, 1)  # '[' 或 'O'
                    while True:
                        rr, _, _ = sel.select([fd], [], [], 0.05)
                        if not rr:
                            break
                        c = os.read(fd, 1)
                        if 0x40 <= c[0] < 0x7F:
                            break
                except OSError:
                    return
            # 其余字符丢弃：回合期不回显不打字（打断是唯一诉求）

    def stop(self):
        self._stop.set()
        if self._old is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old)
            except (termios.error, OSError):
                pass


def _args_one_line(args):
    try:
        s = json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(args)
    return " ".join(str(s).split())[:72]


# ── W5 终端 UX 纯函数层（Claude Code 手感）：续行 / @补全 / ⎿详情 / 缩进 ──
_REPL_ROOT = [None]  # 交互补全用：当前会话 root（_repl 启动时写入）


def _line_wants_continue(line):
    """行尾奇数个反斜杠 = 续行（`\\\\` 是字面反斜杠，不续）。纯函数。"""
    s = str(line or "").rstrip()
    n = 0
    while n < len(s) and s[len(s) - 1 - n] == "\\":
        n += 1
    return n % 2 == 1


def _compose_join(acc, nxt):
    """把续行段拼进已收文本：去掉一个续行标记、以换行连接。纯函数。"""
    s = str(acc or "")
    if s.rstrip().endswith("\\"):
        s = s.rstrip()[:-1]
    return s + "\n" + str(nxt or "")


def _at_completions(tok, root, limit=20):
    """`@` 开头的 Tab 补全：root 内相对路径列候选（目录带 /、隐藏项跳过）。"""
    rel = str(tok or "")[1:]
    base = os.path.dirname(rel)
    frag = os.path.basename(rel)
    d = os.path.join(root or ".", base) if base else (root or ".")
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    out = []
    for n in names:
        if n.startswith(".") or not n.startswith(frag):
            continue
        p = os.path.join(base, n) if base else n
        out.append("@%s%s" % (p, "/" if os.path.isdir(os.path.join(d, n)) else ""))
        if len(out) >= limit:
            break
    return out


def _tool_summary(name, args, res):
    """编辑类工具的 ⎿ 详情行（Claude Code 风格）；无详情返回 ""。

    只对**真的发生且成功**的写体现详情：被拒/未执行/失败一律空 ——
    详情行是「发生了什么」的如实描述，不是装饰。
    """
    if not isinstance(res, dict) or not res.get("ok"):
        return ""
    a = args if isinstance(args, dict) else {}
    if name == "edit_file":
        old = str(a.get("old_string") or "")
        new = str(a.get("new_string") or "")
        if not old and not new:
            return ""
        o, n = old.splitlines(), new.splitlines()
        i = 0
        while i < len(o) and i < len(n) and o[i] == n[i]:
            i += 1
        j = 0
        while (
            j < len(o) - i and j < len(n) - i and o[len(o) - 1 - j] == n[len(n) - 1 - j]
        ):
            j += 1
        added, removed = len(n) - i - j, len(o) - i - j
        seg = "%s · +%d −%d" % (str(a.get("path") or "?")[:48], added, removed)
        if i < len(n) - j:
            seg += " · %s" % n[i].strip()[:48]
        return seg
    if name == "write_file":
        body = str(a.get("content") or "")
        return "%s · 写入 %d 行 / %d 字节" % (
            str(a.get("path") or "?")[:48],
            body.count("\n") + (1 if body else 0),
            len(body.encode("utf-8")),
        )
    return ""


def _format_answer(ans):
    """终答渲染：续行缩进两格（Claude Code 的 ⏺ 块手感）。纯函数。"""
    s = str(ans or "").strip()
    if not s:
        return "（无终答）"
    return s.replace("\n", "\n  ")


def _turn_ui(sp, printer, seen):
    """REPL 回合 UI 编排（过程可见性的心脏）。

    delta：转轮让位（halt+清行），首个增量前打 ⏺，正文直打——printer 不经
    手 delta（防双打），但 seen["delta"] 由这里置位。tool_call：整行打
    「⏺ 工具(参数)」，转轮改「执行 X」继续转，且不再转发 printer（headless
    -p --stream 的旧输出形状一字不动）。tool/step：转轮回「思考中」。
    非交互（sp 未启用）时 tool_call 不上屏——管道里保持旧行数。
    """
    first = {"d": False}

    def _ev(ev):
        t = ev.get("type")
        if t == "delta":
            if sp is not None:
                sp.halt()
                sp.pause()
            if not first["d"]:
                first["d"] = True
                sys.stdout.write(_c("brand", "⏺ "))
            sys.stdout.write(ev.get("text") or "")
            sys.stdout.flush()
            seen["delta"] = True
            return
        if sp is not None and sp.enabled:
            sp.pause()
            if t == "tool_call":
                sp.set("执行 " + str(ev.get("tool") or "?"))
                sys.stdout.write(
                    _c("brand", "⏺ ")
                    + _c("cyan", str(ev.get("tool") or "?"))
                    + _c("dim", "(%s)" % _args_one_line(ev.get("args")))
                    + "\n"
                )
                sys.stdout.flush()
            else:
                sp.set("思考中")
            sp.wake()
        if t == "tool_call":
            return
        if printer is not None:
            printer(ev)
        # ⎿ 详情行（Claude Code 手感）：只有交互 TTY 且工具自报有详情才画
        # （headless / -p --stream 的既有输出形状一字不动）。
        if t == "tool" and sp is not None and sp.enabled:
            _ts = str(ev.get("summary") or "")
            if _ts:
                sys.stdout.write(_c("dim", "  ⎿ %s\n" % _ts))
                sys.stdout.flush()

    return _ev


def _report(res, stream, seen=None):
    streamed = bool(stream and isinstance(seen, dict) and seen.get("delta"))
    if streamed:
        sys.stdout.write("\n")  # 增量已经逐字打过了，别把终答再抄一遍
    else:
        sys.stdout.write("%s\n" % (res.get("answer") or "").strip())
    denied = [u for u in res.get("tools_used") or [] if not u.get("executed")]
    sys.stdout.write(
        "\n--- agent_core：%d 步，%.1fs，工具调用 %d 次"
        "（其中 %d 次未执行：被拒/参数不对）\n"
        % (
            res.get("steps") or 0,
            res.get("elapsed") or 0.0,
            len(res.get("tools_used") or []),
            len(denied),
        )
    )
    if res.get("kind") == "limit":
        sys.stderr.write("预算用尽：%s\n" % res.get("error"))
        if res.get("last_text"):
            sys.stderr.write("  （卡在末句：%r）\n" % res.get("last_text"))
    if res.get("empty_answer"):
        sys.stderr.write("（模型返回空内容：多为端点抖动或模型卡壳——可重试或换端点）\n")
    return 0


def _run_once(args):
    prompt = " ".join(args.prompt).strip()
    if not prompt:
        raise AgentError("usage", "headless 模式需要一句非空需求：agent_core -p '...'")
    ctx = {
        "root": args.root,
        "mode": args.mode,
        "approver": _make_approver(args),
        "max_steps": args.max_steps,
        # headless 也配 sid：DSP 大任务自启的计划要落盘（work/plans/），
        # 无 sid 自启无处落。这是 headless 与 REPL 唯一不同的接线。
        "sid": "cli-%s" % time.strftime("%Y%m%d-%H%M%S"),
    }
    _sf9 = _safety_flags(_read_agentos_config())
    ctx.update(_sf9)
    try:
        ctx["full_access"] = _full_access(_read_agentos_config())
    except Exception:
        ctx["full_access"] = False
    if ctx.get("full_access"):
        sys.stderr.write(
            "[完全权限] 破坏性命令放行（rm/git push 等，逐条留痕"
            " work/destructive_audit.jsonl）\n"
        )
    _on9 = [
        n
        for n, k in (
            ("计划锁", "plan_lock_off"),
            ("权限模式", "mode_gate_off"),
            ("命令白名单", "cmd_gate_off"),
        )
        if not _sf9[k]
    ]
    if _on9:
        sys.stderr.write(
            "[安全闸] 上锁：%s（~/.agentos/config.toml [agent] "
            "字面 true 才上锁；硬阻断清单默认不可关（full_access 除外））\n"
            % "、".join(_on9)
        )
    else:
        sys.stderr.write(
            "[安全闸] 自由面：计划锁/权限模式/命令白名单均关"
            "（默认以用户要求为中心；字面 true 才上锁；"
            "硬阻断清单默认不可关（full_access 除外））\n"
        )
    seen = {"delta": False}
    _mcp_sync()  # headless 也挂 MCP 服务器工具（配置里有 [mcp] 才动）
    _hooks_session(ctx)  # hooks SessionStart（有 hooks.json 才动）
    res = run_agent(
        prompt, ctx=ctx, stream=args.stream, on_event=_event_printer(args.stream, seen)
    )
    return _report(res, args.stream, seen)


# ── REPL 交互皮：Claude Code 手感（只在交互 TTY 生效）────────────────
# 总闸是 stdout.isatty()：headless（-p / --check / 管道喂 stdin）永远零
# ANSI，cli_gate / agent_core_gate 的管道量具吃不到一个色码；交互终端
# （TERM 带 256color 或 COLORTERM=truecolor/24bit）才上 Claude 橙皮。
_SKIN_SGR = {
    "brand": "\x1b[38;2;217;119;87m",  # Claude 橙 #D97757（与 TUI 同源）
    "cyan": "\x1b[38;2;126;168;188m",
    "dim": "\x1b[38;2;146;139;132m",
    "bold": "\x1b[1m",
    "r": "\x1b[0m",
}


def _skin():
    t = os.environ.get("TERM", "")
    if os.environ.get("NO_COLOR") or t in ("", "dumb"):
        return {}
    if not sys.stdout.isatty():
        return {}
    if "256color" not in t and os.environ.get("COLORTERM", "") not in (
        "truecolor",
        "24bit",
    ):
        return {}
    return _SKIN_SGR


def _c(*ks):
    """_c("brand", "bold", 文本)：前面的键取 SGR，末参是正文；无皮原样。"""
    s = ks[-1]
    S = _skin()
    if not S:
        return s
    return "".join(S[k] for k in ks[:-1]) + s + S["r"]


def _dw(s):
    """终端显示列宽：CJK/全角 2 列，其余 1 列（欢迎框对齐用）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _clip72(s, w):
    """按显示宽截断补省略号（欢迎框内自裁，不依赖终端宽度探测）。"""
    s = str(s)
    if _dw(s) <= w:
        return s
    out, used = [], 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used + cw > w - 1:
            break
        out.append(ch)
        used += cw
    return "".join(out) + "…"


REPL_SLASH_CMDS = (
    "/help",
    "/tools",
    "/mode",
    "/status",
    "/model",
    "/cost",
    "/undo",
    "/export",
    "/stats",
    "/compact",
    "/clear",
    "/reset",
    "/diff",
    "/init",
    "/memory",
    "/ideas",
    "/sessions",
    "/rewind",
    "/skill",
    "/branch",
    "/verify",
    "/steer",
    "/watch",
    "/evo",
    "/evo-apply",
    "/evo-off",
    "/plan",
    "/approve",
    "/tasks",
    "/block",
    "/goal",
    "/exit",
    "/quit",
    "/q",
)


def _repl_welcome(provider, ctx, sid, resumed):
    """Claude Code 式欢迎框：开 REPL 先看见「在哪、用什么模型、怎么使」。

    走 stderr（REPL 的 UI 全在 stderr，stdout 只留答案/数据 —— 管道喂
    stdin 的量具在 stdout 上拿到的仍是纯数据）。框内全是现取真值：
    模型/端点/来源、权限模式、仓库根、会话与续跑条数、工具数。
    """
    W = 72
    bar = _c("brand", "│  ")
    end = _c("brand", "  │")
    blank = bar + " " * (W - 4) + end
    tip = _clip72(
        "/help · /goal 自主执行 · /evo 自进化 · /exit 或 Ctrl-D 退出 · !cmd · @文件",
        W - 4,
    )
    items = [
        (
            "模型",
            "%s @ %s（%s）"
            % (provider.model or "-", provider.base_url, provider.source),
        ),
        ("模式", "%s · v%s · 仓库 %s" % (ctx["mode"], VERSION, ctx["root"])),
        (
            "会话",
            "%s（%s）"
            % (
                sid,
                "续跑 %d 条" % len(ctx.get("history") or []) if resumed else "新开",
            ),
        ),
        ("工具", "%d 个 · /tools 看清单与风险级" % len(TOOLS)),
    ]
    out = [
        _c("brand", "bold", "✻ Welcome to AgentOS!"),
        "",
        _c("brand", "╭" + "─" * (W - 2) + "╮"),
    ]
    for lab, val in items:
        body = _clip72("* %s  %s" % (lab, val), W - 4)
        fill = " " * max(0, (W - 4) - _dw(body))
        ci = 2 + len(lab)
        out.append(
            bar
            + _c("brand", body[:2])
            + _c("cyan", body[2:ci])
            + body[ci:]
            + fill
            + end
        )
    out += [
        blank,
        bar + _c("dim", tip) + " " * max(0, (W - 4) - _dw(tip)) + end,
        _c("brand", "╰" + "─" * (W - 2) + "╯"),
    ]
    sys.stderr.write("\n".join(out) + "\n")


def _setup_readline():
    """TTY 才装：readline 编辑/历史（~/.agentos/agent_history）+ 斜杠命令
    Tab 补全。管道/CI 不装 —— 量具喂行不能带编辑态副作用。"""
    if not sys.stdin.isatty():
        return
    try:
        import readline
    except ImportError:
        return

    def _comp(text, state):
        if text.startswith("@"):
            opts = _at_completions(text, _REPL_ROOT[0])
            if state < len(opts):
                return opts[state] + ("" if opts[state].endswith("/") else " ")
            return None
        opts = [c for c in REPL_SLASH_CMDS if c.startswith(text)]
        return (opts[state] + " ") if state < len(opts) else None

    readline.set_completer(_comp)
    readline.set_completer_delims(" \t\n")
    readline.parse_and_bind("tab: complete")
    hpath = os.path.expanduser("~/.agentos/agent_history")

    def _save():
        try:
            readline.set_history_length(500)
            readline.write_history_file(hpath)
        except Exception:
            pass

    try:
        os.makedirs(os.path.dirname(hpath), exist_ok=True)
        readline.read_history_file(hpath)
    except Exception:
        pass
    import atexit

    atexit.register(_save)


def _readline(tag):
    """读一行输入。TTY 走 input()+readline（历史/编辑/Tab 补全）；管道/CI
    走裸 sys.stdin.readline（无编辑态）。两者 EOF 都归 None（Ctrl-D 退出）；
    TTY 下 Ctrl-C 只清行不退出（对齐 Claude Code：/exit 或 Ctrl-D 才走）。"""
    if sys.stdin.isatty():
        try:
            return input(tag)
        except EOFError:
            return None
        except KeyboardInterrupt:
            return ""
    try:
        sys.stdout.write(tag)
        sys.stdout.flush()
        line = sys.stdin.readline()
        # stdin 关闭时 readline() 返回空串，而不是抛 EOFError。
        # 必须在此转成 None，REPL 才能像 Claude Code 的 Ctrl-D 一样退出。
        return line if line else None
    except (EOFError, KeyboardInterrupt):
        return None


# ── REPL 会话记录 + 纯函数工具（对齐 claude code 的终端手感）──────────
# 为什么把会话落在 work/agent_sessions/agent-*.jsonl：CLI 自己的 headless
# 历史是同目录的 cli-*.jsonl（形状是「动作 + rc + 输出行」），两者语义不同
# —— agent 会话是「用户/助手逐轮对话」。混用一个前缀会让 `agentos sessions`
# 把两类东西读成一种。所以另起 agent- 前缀，互不接管对方的命名空间。
SESSION_DIR = os.path.join(ROOT, "work", "agent_sessions")
TRANSCRIPT_PREFIX = "agent-"
COMPACT_KEEP = 20  # /compact 与自动裁剪保留的最近消息条数

# ── 自进化（R2）：信号 → 规则 → 注入 → 可回退 ────────────────────────
# 设计红线：这一层是**增益层**，任何一步坏掉都不许打断对话主循环（fail-open），
# 但账坏了必须如实说（/evo 报 err、不假装有规则）；账好时全部现取现算。
EVO_SIGNALS_PATH = os.path.join(SESSION_DIR, "signals.jsonl")
EVO_RULES_PATH = os.path.join(ROOT, "work", "agent_evo_rules.json")
EVO_MIN_COUNT = 3  # 同签名失败达到几条才聚成规则（R2 判据=3）

_EVO_ADVICE = {
    "no-provider": "配一个端点再聊：agentos config set llm.api_key <key>，"
    "或 AGENTOS_LLM_LOCAL=ollama 走本机离线后端",
    "provider": "端点失败：先 GET /v1/models 探活；HTTP 403/400 多为 key 或"
    "网关路由问题，不可达为网络/代理问题；/model 可现场换模型",
    "usage": "参数口径不对：先 /help 看命令形状",
}
_EVO_ADVICE_GENERIC = (
    "查 stderr 里 [失败 kind] 的原文；同类失败连续出现会在此聚成经验规则"
)
_EVO_ADVICE.update(
    {
        "tool-fail:read_file": "read_file 失败多为路径不存在或越界：先 list_dir "
        "确认结构再读，路径以项目根为基准",
        "tool-fail:edit_file": "edit_file 失败多为 old_string 不唯一或未找到："
        "先 read_file 取精确原文再改",
        "tool-fail:write_file": "write_file 失败：核对相对路径与权限模式",
        "tool-fail:run_cmd": "run_cmd 失败：命令是否在只读白名单内，长输出先缩小范围",
        "tool-fail:gh_search": "gh_search 失败：缩小 query 或检查 gh 认证",
    }
)


def _evo_signal(
    mode,
    ok,
    err_kind,
    elapsed,
    tools,
    note="",
    tools_fail=None,
    denied=None,
    denied_reasons=None,
):
    """每轮一条信号进 signals.jsonl（挖掘的原料）。落盘失败静默——
    进化是增益层，绝不允许它打断对话主循环。被兜底掩盖的主端点失败也记
    （ok=False + note=fallback-masked）：对挖掘而言它是真失败事件。
    v2：tools_fail（执行了但失败的工具）/denied（被拒未执行的工具）——
    框架层不足信号，挖掘器按工具聚签名。"""
    row = {
        "ts": round(time.time(), 3),
        "kind": "turn",
        "mode": mode,
        "ok": bool(ok),
        "err_kind": err_kind or "",
        "elapsed": elapsed,
        "tools": list(tools or []),
        "note": note,
        "tools_fail": list(tools_fail or []),
        "denied": list(denied or []),
        "denied_reasons": list(denied_reasons or []),
    }
    try:
        os.makedirs(os.path.dirname(EVO_SIGNALS_PATH), exist_ok=True)
        with open(EVO_SIGNALS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass


def _evo_rules_load(rules_path=None):
    """读规则账。返回 (rules, err)：err 非空 = 账坏（fail-open 的诚实版：
    调用方拿空规则继续跑，但 /evo 必须把 err 说出口）。"""
    p = rules_path or EVO_RULES_PATH
    if not os.path.exists(p):
        return [], ""
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except (ValueError, OSError) as e:
        return [], str(e)
    if not isinstance(d, dict) or not isinstance(d.get("rules"), list):
        return [], "规则账形状不对（缺 rules 数组）"
    return d["rules"], ""


def _evo_rules_save(rules, rules_path=None):
    p = rules_path or EVO_RULES_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(
            {"version": 1, "rules": rules},
            f,
            ensure_ascii=False,
            indent=1,
            sort_keys=True,
        )


def _evo_mine(rules_path=None, signals_path=None, min_count=None):
    """信号 → 规则（幂等）。同签名失败 ≥ min_count 条聚成 CANDIDATE；
    count 一律按信号全量重算（append-only 语义）。账坏时不动账（防挖一半
    把坏账洗成假新账），如实把 err 带回去。"""
    min_count = EVO_MIN_COUNT if min_count is None else min_count
    rp = rules_path or EVO_RULES_PATH
    sp = signals_path or EVO_SIGNALS_PATH
    rules, err = _evo_rules_load(rp)
    if err:
        return [], err
    counts = {}
    try:
        with open(sp, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    s = json.loads(ln)
                except ValueError:
                    continue
                if (
                    s.get("ok") is False
                    and s.get("err_kind")
                    and s.get("err_kind") != "interrupted"
                ):
                    counts["err:%s" % s["err_kind"]] = (
                        counts.get("err:%s" % s["err_kind"], 0) + 1
                    )
                for t in s.get("tools_fail") or []:
                    k = "tool-fail:%s" % t
                    counts[k] = counts.get(k, 0) + 1
                for t in s.get("denied") or []:
                    k = "denied:%s" % t
                    counts[k] = counts.get(k, 0) + 1
    except OSError:
        counts = {}
    by_sig = {r.get("sig"): r for r in rules}
    changed = False
    for sig in sorted(counts):
        n = counts[sig]
        if n < min_count:
            continue
        kind = sig.split(":", 1)[1] if ":" in sig else sig
        r = by_sig.get(sig)
        if r is None:
            rules.append(
                {
                    "id": "evo-%s" % sig.replace(":", "-"),
                    "sig": sig,
                    "count": n,
                    "advice": _EVO_ADVICE.get(
                        sig, _EVO_ADVICE.get(kind, _EVO_ADVICE_GENERIC)
                    ),
                    "state": "CANDIDATE",
                    "created": time.strftime("%F %T"),
                }
            )
            changed = True
        elif r.get("count") != n:
            r["count"] = n
            changed = True
    if changed:
        _evo_rules_save(rules, rp)
    return rules, ""


def _evo_set_state(rid, state, rules_path=None):
    """晋升/回退一条规则。返回是否命中。"""
    rules, err = _evo_rules_load(rules_path)
    if err or not rid:
        return False
    hit = False
    for r in rules:
        if r.get("id") == rid:
            r["state"] = state
            hit = True
    if hit:
        _evo_rules_save(rules, rules_path)
    return hit


# ── 自进化 v2：框架级自提升（运行时工具提示 + 提案 + midflight）────────
# 诚实边界：规则只改「运行时工具描述」与「system prompt」，绝不自改磁盘
# 代码与门判据；真正的框架级改动走 work/evo_proposals/ 提案人审。
_TOOL_DESC_ORIG = {}


def _evo_refresh_tool_hints(rules_path=None):
    """tool 类 ACTIVE 规则 → 运行时改写该工具的 description（带
    「自进化提示」标记，/tools 可见）；/evo-off 后还原原文。返回生效数。"""
    if not TOOLS:
        return 0
    for name, spec in TOOLS.items():
        if name not in _TOOL_DESC_ORIG:
            _TOOL_DESC_ORIG[name] = spec.get("description") or ""
    rules, _ = _evo_rules_load(rules_path)
    hints = {}
    for r in rules:
        if (
            r.get("state") == "ACTIVE"
            and str(r.get("sig") or "").startswith(("tool-fail:", "denied:"))
            and r.get("advice")
        ):
            hints[r["sig"].split(":", 1)[1]] = r["advice"]
    for name, spec in TOOLS.items():
        orig = _TOOL_DESC_ORIG.get(name) or ""
        if name in hints:
            spec["description"] = "%s（自进化提示：%s）" % (orig, hints[name])
        else:
            spec["description"] = orig
    return len(hints)


EVO_PROPOSAL_KINDS = ("tool-hint", "memory", "gate-proposal")


def validate_proposal(d):
    """intake 提案 JSON schema 校验（纯面）。返回 (ok, error)。"""
    if not isinstance(d, dict):
        return False, "提案必须是 JSON object"
    pid = d.get("id")
    if (
        not isinstance(pid, str)
        or not pid.strip()
        or "/" in pid
        or "\\" in pid
        or pid.startswith(".")
    ):
        return False, "缺合法 id（仓内相对名，不许路径穿越）"
    if not isinstance(d.get("sig"), str) or not d.get("sig").strip():
        return False, "缺 sig（失败签名）"
    if not isinstance(d.get("count"), int) or d.get("count") < 1:
        return False, "缺 count（正整数）"
    if d.get("kind") not in EVO_PROPOSAL_KINDS:
        return False, "kind 须为 %s" % "/".join(EVO_PROPOSAL_KINDS)
    if not isinstance(d.get("advice"), str) or not d.get("advice").strip():
        return False, "缺 advice（非空字符串）"
    if d.get("status") not in ("pending-human", "accepted", "rejected"):
        return False, "status 非法"
    return True, ""


def _evo_propose(rules, rules_path=None):
    """失败达 2 倍阈值的签名 → 框架级提升提案文件（人审入口）。
    .md 人读 + 同名 .json 机器 intake（validate_proposal 可验）。
    返回新生成提案数。"""
    rp = rules_path or EVO_RULES_PATH
    pdir = os.path.join(os.path.dirname(rp), "evo_proposals")
    made = 0
    for r in rules:
        n = r.get("count") or 0
        if n < EVO_MIN_COUNT * 2 or r.get("proposed"):
            continue
        os.makedirs(pdir, exist_ok=True)
        pid = r.get("id") or "rule"
        sig = str(r.get("sig") or "")
        kind = "tool-hint" if sig.startswith(("tool-fail:", "denied:")) else "memory"
        with open(os.path.join(pdir, "%s.json" % pid), "w", encoding="utf-8") as _jf:
            json.dump(
                {
                    "id": pid,
                    "sig": sig,
                    "count": n,
                    "advice": r.get("advice") or "",
                    "kind": kind,
                    "created": time.strftime("%F %T"),
                    "status": "pending-human",
                },
                _jf,
                ensure_ascii=False,
                indent=1,
                sort_keys=True,
            )
        with open(os.path.join(pdir, "%s.md" % pid), "w", encoding="utf-8") as f:
            f.write("# 框架级提升提案：%s\n\n" % pid)
            f.write(
                "- 签名：%s（×%d，触发阈值 %d）\n" % (r.get("sig"), n, EVO_MIN_COUNT)
            )
            f.write("- 现有建议：%s\n" % r.get("advice"))
            f.write("- 提议方向（人审采纳；学习只建议，永不擅改门与判据）：\n")
            f.write("  1. 工具缺陷 → 修工具实现并补 --check 用例；\n")
            f.write("  2. 使用方式 → 建议升级进 AGENTS.md 项目记忆；\n")
            f.write("  3. 量具空白 → 提议新判据（先出 --check 断言草案）。\n")
            f.write("- 生成时间：%s\n" % time.strftime("%F %T"))
            f.write("- 机器 intake：同名 .json（validate_proposal 可验）\n")
        r["proposed"] = True
        made += 1
    if made:
        _evo_rules_save(rules, rp)
    return made


def _evo_midflight(rules_path=None, signals_path=None):
    """每轮末静默挖掘：新 CANDIDATE 返回给调用方提示一次（notified 节流），
    达 2 倍阈值自动生成提案。fail-open 全程。"""
    rules, err = _evo_mine(rules_path=rules_path, signals_path=signals_path)
    if err:
        return []
    new = [r for r in rules if r.get("state") == "CANDIDATE" and not r.get("notified")]
    _evo_propose(rules, rules_path)
    for r in new:
        r["notified"] = True
    if new:
        _evo_rules_save(rules, rules_path)
    return new


def _evo_prompt_lines(rules_path=None):
    """ACTIVE 规则 → system prompt 追加段；没有就空（不加空标题）。"""
    rules, _ = _evo_rules_load(rules_path)
    act = [r for r in rules if r.get("state") == "ACTIVE" and r.get("advice")]
    if not act:
        return []
    return ["自进化经验规则（本机历史失败挖掘，用户已晋升 ACTIVE，照此避坑）："] + [
        "- %s" % r["advice"] for r in act
    ]


# ── 记忆层级（蒸馏自 gemini-cli GEMINI.md / openai-codex AGENTS.md）────
# 约定：仓库根的 AGENTS.md / AGENTOS.md 是项目级记忆，每轮注入 system
# prompt；单文件封顶 4KB 防失控。没有这两类文件 = 零注入（不造假）。
_MEMORY_CAP = 4096


def _memory_files(root):
    out = []
    for name in ("AGENTS.md", "AGENTOS.md"):
        p = os.path.join(root or ROOT, name)
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                body = f.read(_MEMORY_CAP + 1)
        except OSError:
            continue
        if body.strip():
            if len(body) > _MEMORY_CAP:
                body = body[:_MEMORY_CAP] + "\n…（截断，单文件上限 4KB）"
            out.append((name, body.strip()))
    return out


def _memory_prompt_lines(ctx):
    files = _memory_files(ctx.get("root"))
    if not files:
        return []
    lines = ["项目记忆（自 AGENTS.md / AGENTOS.md 层级读入，其中的约束照办）："]
    for name, body in files:
        lines.append("<<<%s>>>" % name)
        lines.append(body)
        lines.append("<<<END %s>>>" % name)
    return lines


# ── 自主力面（R3）：/ideas 机会挖掘（创造力）+ 回合回执自检行 ────────────
_IDEA_MARK_RE = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")


def _ideas_scan(root):
    """只读扫仓库出提案原料：返回 (marks, gaps)。

    marks = TODO/FIXME/XXX/HACK 留痕，每条带 file:line（AC5 的证据形态）；
    gaps = tools/ 源文件里从未被任何其他 .py 提到 stem 的「候选测试缺口」
    （advisory：假阳性可接受，产出时明说是候选）。跳过 work/、venv、备份。
    """
    marks, stems = [], {}
    skip_dirs = {"work", ".venv-pw", "__pycache__", "node_modules", ".git", "outputs"}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            if (
                not fn.endswith(".py")
                or ".bak" in fn
                or fn.startswith("_st")
                or fn == "agent_core.py"
            ):
                continue
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root)
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    body = f.read()
            except OSError:
                continue
            stems.setdefault(os.path.splitext(fn)[0], rel)
            for i, ln in enumerate(body.splitlines(), 1):
                if _IDEA_MARK_RE.search(ln):
                    marks.append("%s:%d %s" % (rel, i, ln.strip()[:90]))
    gaps = []
    for stem, rel in sorted(stems.items()):
        mentioned = False
        for other_stem, other_rel in stems.items():
            if other_stem == stem:
                continue
            try:
                with open(
                    os.path.join(root, other_rel), encoding="utf-8", errors="replace"
                ) as f:
                    if stem in f.read():
                        mentioned = True
                        break
            except OSError:
                continue
        if not mentioned:
            gaps.append(rel)
    return marks, gaps


def _repl_ideas(ctx):
    """/ideas：把扫描结果渲染成提案清单进 stderr（只读，零写入）。"""
    root = ctx.get("root") or ROOT
    t0 = time.time()
    marks, gaps = _ideas_scan(root)
    sys.stderr.write("机会挖掘（%.1fs，只读；根 %s）：\n" % (time.time() - t0, root))
    if marks:
        sys.stderr.write(
            "· 留痕（TODO/FIXME/XXX/HACK）%d 条，前 20 条：\n" % len(marks)
        )
        for x in marks[:20]:
            sys.stderr.write("  %s\n" % x)
    else:
        sys.stderr.write("· 留痕：0 条（源码无 TODO/FIXME 挂账）\n")
    if gaps:
        sys.stderr.write(
            "· 候选测试缺口 %d 个（stem 从未被其他 .py 引用，"
            "建议补自检或门）：\n" % len(gaps)
        )
        for g in gaps[:15]:
            sys.stderr.write("  %s\n" % g)
    sys.stderr.write(
        "提案：留痕逐条转 issue（agentos gh issue-create "
        "--dry-run 预演，--yes+配置+确认才真写）；缺口逐个补 "
        "--check 用例。\n"
    )


def _session_dir():
    return SESSION_DIR


def _new_session_id():
    """agent-YYYYMMDD-HHMMSS-xxxx。前缀与 cli- 区分，后缀便于人肉认。"""
    return "%s%s-%s" % (
        TRANSCRIPT_PREFIX,
        time.strftime("%Y%m%d-%H%M%S"),
        uuid.uuid4().hex[:4],
    )


def _transcript_path(sid):
    return os.path.join(_session_dir(), "%s.jsonl" % sid)


def _transcript_list():
    """只认 agent-*.jsonl，按文件名排序（时间前缀天然有序）。"""
    d = _session_dir()
    if not os.path.isdir(d):
        return []
    return [
        os.path.join(d, n)
        for n in sorted(os.listdir(d))
        if n.startswith(TRANSCRIPT_PREFIX) and n.endswith(".jsonl")
    ]


def _transcript_append(path, rec):
    """追加一条 JSONL。目录不存在就建；失败不抛，返回 False 让调用方如实说。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def _transcript_load(path):
    recs = []
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if raw:
                recs.append(json.loads(raw))
    return recs


def _history_from_transcript(recs, keep=COMPACT_KEEP):
    """transcript 记录 -> messages（system 由 run_agent 自己补，这里不放）。"""
    msgs = []
    for r in recs:
        if not isinstance(r, dict):
            continue
        kind, text = r.get("kind"), r.get("text")
        if kind == "user" and text:
            msgs.append({"role": "user", "content": str(text)})
        elif kind == "assistant" and text:
            msgs.append({"role": "assistant", "content": str(text)})
    return msgs[-keep:] if keep > 0 else msgs


def _safe_cut(rest, start):
    """把切往前挪到「工具配对不破」的边界，返回修正后的 start。

    为什么必须有：把 start 直接钉在 keep 上，切点会落在 assistant
    (tool_calls) 与其 **tool 结果**的中间 —— 开头第一条是孤 tool 消息，
    下一次请求直接被网关 400（实测 2026-10-18：markdown-it-py 真任务
    2:36 后报 "No tool call found for tool output with call_id …"，一步
    黑掉整轮）。修法不是改阈值，是不许切进配对里：切点之前如果还挂着
    assistant(tools_calls)，它的 tool 结果也一起带走。
    """
    n = len(rest)
    start = max(0, min(start, n))
    while start < n and rest[start].get("role") == "tool":
        start -= 1
    start = max(0, start)
    # 切点左边是带 tool_calls 的 assistant：其 tool 结果在切点之后 → 破配对
    if (
        start > 0
        and (rest[start - 1].get("tool_calls") or rest[start - 1].get("tool"))
    ):
        # 把 assistant 一起留下（连同它前面更早的同环成员，放心往前扫）
        while start > 0 and rest[start - 1].get("role") == "tool":
            start -= 1
        start -= 1
    return start


def _compact_history(history, keep=COMPACT_KEEP):
    """保留 system + 最近 keep 条，返回 (新历史, 丢掉条数)。

    为什么单独成函数：`/compact` 与 run_agent 的自动裁剪必须同一套口径，
    否则「手动压过」和「自动压过」保留的东西不一样，用户看到的上下文
    和实际喂给模型的对不上。切点走 _safe_cut：宁可多留一条，不留孤儿。
    """
    if not isinstance(history, list):
        return [], 0
    head = [history[0]] if (history and history[0].get("role") == "system") else []
    rest = history[1:] if head else list(history)
    st = _safe_cut(rest, len(rest) - keep)
    dropped = st  # 从头上切掉的就是 st 条（切点只会往前挪，不会往后）
    return head + (rest[st:] if keep > 0 else rest), dropped


DIGEST_PREFIX = "【上下文摘要·自动压缩】"
DIGEST_PROMPT = (
    "把下面被压缩掉的对话历史提炼成事实摘要：保留文件路径、做出的决策、"
    "已跑命令的关键 rc、未完成的 TODO；不要寒暄、不要编造，直接输出摘要正文。"
)
DIGEST_MAX_CHARS = 6000  # 摘要请求体上限（防压缩本身超大）
DIGEST_MSG_CHARS = 400  # 每条被压消息进请求体的截断上限


def _digest_input(msgs):
    """把被丢弃的消息渲成摘要请求体（每条截断，总量封顶）。"""
    lines = []
    for m in msgs:
        role = m.get("role", "?")
        body = m.get("content")
        if not body and m.get("tool_calls"):
            body = (
                "tool_calls=%s"
                % json.dumps(m.get("tool_calls"), ensure_ascii=False)[:DIGEST_MSG_CHARS]
            )
        body = str(body or "")
        lines.append("%s: %s" % (role, body[:DIGEST_MSG_CHARS]))
    return "\n".join(lines)[:DIGEST_MAX_CHARS]


def _compact_history_v2(history, keep=COMPACT_KEEP, provider=None, focus=""):
    """compact v2：丢弃前先用 provider 生成事实摘要 digest，失败回退旧行为。

    返回 (新历史, 丢掉条数, digest_used)。为什么保留旧 _compact_history：
    provider 缺席/失败时口径必须与旧版**逐字相同**（回退不是"差不多"）——
    --check 与 rewind_gate 都拿旧函数输出当回退基准做逐字对账。
    """
    if not isinstance(history, list):
        return [], 0, False
    head = [history[0]] if (history and history[0].get("role") == "system") else []
    rest = history[1:] if head else list(history)
    if len(rest) <= keep:
        return list(history), 0, False
    dropped_msgs = rest[: len(rest) - keep]
    digest = ""
    if provider is not None:
        try:
            prompt = DIGEST_PROMPT
            if focus:
                prompt += "\n聚焦要求：%s" % focus
            comp = provider.chat(
                [
                    {
                        "role": "user",
                        "content": prompt + "\n\n" + _digest_input(dropped_msgs),
                    }
                ],
                tools=None,
                stream=False,
            )
            digest = str((comp or {}).get("content") or "").strip()
        except Exception:  # noqa: BLE001
            digest = ""
    if not digest:
        new_hist, dropped = _compact_history(history, keep)
        return new_hist, dropped, False
    digest_msg = {"role": "user", "content": DIGEST_PREFIX + "\n" + digest}
    return head + [digest_msg] + rest[-keep:], len(dropped_msgs), True


def _rewind_replay(recs, n):
    """把 transcript 记录重放成「前 n 轮末」的 messages。

    一轮 = 一条 user 记录（含其后到下一轮 user 前的全部记录）。返回
    (msgs, err, turns)；越界（n<1 或 n>轮数）fail-closed：err 非空、msgs=None。
    纯函数：不碰盘、不碰 ctx —— rewind_gate 直接拿夹具喂它证伪。
    """
    turns = [
        i
        for i, r in enumerate(recs)
        if isinstance(r, dict) and r.get("kind") == "user" and r.get("text")
    ]
    if n < 1 or n > len(turns):
        return None, "越界：本会话共 %d 轮，请求第 %d 轮" % (len(turns), n), len(turns)
    end = turns[n] if n < len(turns) else len(recs)
    return _history_from_transcript(recs[:end], keep=0), "", len(turns)


def _usage_total(acc, usage):
    """累加 token 用量。端点没报 usage 就原样返回（不编数字）。"""
    out = dict(acc or {})
    if not isinstance(usage, dict):
        return out
    for k, v in usage.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        out[k] = out.get(k, 0) + v
    return out


def _expand_at_refs(text, root, limit=3, max_bytes=4000):
    """把 `@相对路径` 展开成 <file path=...>内容</file>，返回 (新文本, 用到
    的路径列表)。只读 root 内的普通文件，越界/不存在/超量的原样保留。

    claude code 的 `@file` 是终端里最常用的一个手势：模型不用先调一次
    read_file 就能看见文件。这里复用 _sandbox 的 realpath 边界，不新开
    一条绕过沙箱的读盘路径。
    """
    out, used = [], []
    for tok in str(text or "").split():
        if not (tok.startswith("@") and len(tok) > 1):
            out.append(tok)
            continue
        rel = tok[1:]
        real, err = _sandbox(rel, root)
        if err or not real or not os.path.isfile(real) or len(used) >= limit:
            out.append(tok)
            continue
        try:
            with open(real, "r", encoding="utf-8", errors="replace") as f:
                body = f.read(max_bytes + 1)
        except OSError:
            out.append(tok)
            continue
        if len(body) > max_bytes:
            body = body[:max_bytes] + "\n...[已截断]"
        out.append("<file path=%s>\n%s\n</file>" % (rel, body))
        used.append(rel)
    return " ".join(out), used


def _status_line(ctx, sid):
    """`/status` 的一行读数。所有值都从 ctx 现取，不缓存、不估算。"""
    return "session=%s · 模式=%s · 仓库=%s · 模型=%s · 上下文=%d 条 · 累计用量=%s" % (
        sid or "-",
        ctx.get("mode"),
        ctx.get("root"),
        ctx.get("model") or "-",
        len(ctx.get("history") or []),
        json.dumps(ctx.get("usage_total") or {}, ensure_ascii=False),
    )


def _repl_session(args):
    """决定这次 REPL 的 session id 与要回灌的历史 -> (sid, resumed|None)。

    --resume <id> 优先；否则 --continue 取最近一份；都没有就是新会话。
    续不上就如实报一句再起新会话 —— 不让「找不到历史」把整个 REPL 卡死。
    """
    want = (getattr(args, "resume", "") or "").strip()
    cont = bool(getattr(args, "cont", False))
    files = _transcript_list()
    hit = None
    if want:
        for p in files:
            if os.path.basename(p)[: -len(".jsonl")] == want:
                hit = p
                break
        if hit is None:
            sys.stderr.write(
                "warn: 找不到会话 %s，起新会话（/sessions 列全部）\n" % want
            )
    elif cont:
        hit = files[-1] if files else None
        if hit is None:
            sys.stderr.write("warn: 没有可续的 agent 会话，起新会话\n")
    if hit is None:
        return _new_session_id(), None
    try:
        recs = _transcript_load(hit)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("warn: 会话读不动（%s），起新会话\n" % str(e)[:80])
        return _new_session_id(), None
    sid = os.path.basename(hit)[: -len(".jsonl")]
    return sid, {"id": sid, "history": _history_from_transcript(recs)}


def _repl_diff(ctx):
    """`/diff`：这个仓库当下的改动（git status --short + diff --stat）。"""
    st = _git({}, ctx, ["status", "--short"])
    if not st.get("ok"):
        sys.stderr.write(
            "不是 git 仓库 / git 不可用：%s\n" % (st.get("error") or "?")[:160]
        )
        return
    body = (st.get("out") or "").rstrip("\n")
    sys.stdout.write(body + "\n" if body else "(工作区干净)\n")
    df = _git({}, ctx, ["diff", "--stat"])
    if df.get("ok") and (df.get("out") or "").strip():
        sys.stdout.write((df.get("out") or "").rstrip("\n") + "\n")


def _repl_init(ctx):
    """`/init`：在仓库根放一份起步 AGENTS.md（已存在就不动）。"""
    path = os.path.join(ctx["root"], "AGENTS.md")
    if os.path.exists(path):
        sys.stderr.write("已存在，未改动：%s\n" % path)
        return
    body = (
        "# AGENTS.md\n\n"
        "本仓库的操作约定。agentos agent 每轮都会读它。\n\n"
        "- 验收以命令退出码为据，禁未验证宣称。\n"
        "- 对外操作（push/tag/发布）须用户明确说「发」。\n"
        "- 删文件用 unlink，不要 rm -f。\n"
    )
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
    except OSError as e:
        sys.stderr.write("写不动 %s：%s\n" % (path, str(e)[:120]))
        return
    sys.stdout.write("已写 %s（%d 字节）\n" % (path, len(body.encode("utf-8"))))


def _repl_bang(cmd, ctx):
    """`!cmd`：把一行 shell 交给工具层同一条路径，真实 rc 打到屏幕。

    走 exec_tool 而不是自己起 subprocess —— 命令面（默认自由 / 显式
    cmd_gate 收窄为只读白名单）、硬阻断、越界、输出截断全在工具层那一条
    路径上，REPL 不另开一套真相。
    """
    argv = cmd.strip().split()
    if not argv:
        sys.stderr.write("用法：!<命令>，如 !git status\n")
        return
    res, _ex = exec_tool("run_cmd", {"argv": argv, "timeout": 20}, ctx)
    if not res.get("ok"):
        sys.stderr.write("拒绝/失败：%s\n" % (res.get("error") or "?"))
        return
    out = (res.get("out") or "").rstrip("\n")
    if out:
        sys.stdout.write(out + "\n")
    sys.stdout.write(
        "rc=%s · %.1fs%s\n"
        % (
            res.get("rc"),
            res.get("dur") or 0.0,
            " · 输出已截断" if res.get("truncated") else "",
        )
    )


def _repl_slash(text, ctx, provider, sid, tpath):
    """处理一条 /命令。返回 "exit" / 新 Provider / None（继续）。

    命令集对齐 claude code 的终端手感：/help /tools /mode /status /model
    /cost /compact /clear(/reset) /diff /init /sessions /exit。所有读数都
    现取，不缓存、不估算；不认识的命令只报一句，不退出。
    """
    parts = text.split(None, 1)
    cmd = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""
    if cmd in ("/exit", "/quit", "/q"):
        return "exit"
    if cmd == "/help":
        sys.stderr.write(EPILOG)
        return None
    if cmd == "/tools":
        for n in sorted(TOOLS):
            sys.stderr.write(
                "  %-12s %-6s %s\n" % (n, TOOLS[n]["risk"], TOOLS[n]["description"])
            )
        return None
    if cmd == "/evo":
        rules, err = _evo_mine()
        if err:
            sys.stderr.write("规则账不可读（fail-open，本轮无规则注入）：%s\n" % err)
        sys.stderr.write(
            "信号账: %s\n规则账: %s\n" % (EVO_SIGNALS_PATH, EVO_RULES_PATH)
        )
        stats = {"n": 0, "ok": 0, "itp": 0, "tf": {}, "den": {}}
        try:
            with open(EVO_SIGNALS_PATH, encoding="utf-8") as f:
                _rows = [json.loads(x) for x in f if x.strip()][-50:]
        except (OSError, ValueError):
            _rows = []
        for r0 in _rows:
            stats["n"] += 1
            stats["ok"] += 1 if r0.get("ok") else 0
            stats["itp"] += 1 if r0.get("err_kind") == "interrupted" else 0
            for t in r0.get("tools_fail") or []:
                stats["tf"][t] = stats["tf"].get(t, 0) + 1
            for t in r0.get("denied") or []:
                stats["den"][t] = stats["den"].get(t, 0) + 1
        if stats["n"]:
            sys.stderr.write(
                "自评（近 %d 轮）：成功 %d/%d · 被打断 %d · 工具失败 top %s "
                "· 被拒 top %s\n"
                % (
                    stats["n"],
                    stats["ok"],
                    stats["n"],
                    stats["itp"],
                    sorted(stats["tf"].items(), key=lambda x: -x[1])[:3],
                    sorted(stats["den"].items(), key=lambda x: -x[1])[:3],
                )
            )
        if not rules:
            sys.stderr.write(
                "（还没有规则：同签名失败满 %d 条自动聚成 CANDIDATE）\n" % EVO_MIN_COUNT
            )
        for r in rules:
            sys.stderr.write(
                "%s [%s] ×%d  %s\n"
                % (r.get("id"), r.get("state"), r.get("count"), r.get("advice"))
            )
        sys.stderr.write(
            "晋升: /evo-apply <id> · 回退: /evo-off <id>"
            "（ACTIVE 下一轮起注入 system prompt）\n"
        )
        return None
    if cmd == "/evo-apply":
        if not rest.strip():
            sys.stderr.write("用法: /evo-apply <规则id>（/evo 先看清单）\n")
        elif _evo_set_state(rest.strip(), "ACTIVE"):
            sys.stderr.write(
                "%s → ACTIVE（下一轮起注入 system prompt；"
                "/evo-off 可回退）\n" % rest.strip()
            )
        else:
            sys.stderr.write("没这条规则：%r（/evo 先看清单）\n" % rest.strip())
        return None
    if cmd == "/evo-off":
        if not rest.strip():
            sys.stderr.write("用法: /evo-off <规则id>\n")
        elif _evo_set_state(rest.strip(), "CANDIDATE"):
            sys.stderr.write("%s → CANDIDATE（已回退，不再注入）\n" % rest.strip())
        else:
            sys.stderr.write("没这条规则：%r\n" % rest.strip())
        return None
    if cmd == "/export":
        src = _transcript_path(sid)
        rows = []
        try:
            with open(src, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        rows.append(json.loads(ln))
                    except ValueError:
                        pass
        except OSError:
            rows = []
        out_dir = os.path.join(_session_dir(), "exports")
        os.makedirs(out_dir, exist_ok=True)
        dst = os.path.join(out_dir, "%s.md" % sid)
        with open(dst, "w", encoding="utf-8") as f:
            f.write("# AgentOS 会话 %s\n\n" % sid)
            for r in rows:
                who = (
                    "用户"
                    if r.get("kind") == "user"
                    else ("助手" if r.get("kind") == "assistant" else "…")
                )
                f.write(
                    "**%s**：%s\n\n" % (who, " ".join(str(r.get("text") or "").split()))
                )
        sys.stderr.write("已导出 %s（%d 条）\n" % (dst, len(rows)))
        return None
    if cmd == "/stats":
        sys.stderr.write(_status_line(ctx, sid) + "\n")
        sys.stderr.write(
            "用量：%s\n" % json.dumps(ctx.get("usage_total") or {}, ensure_ascii=False)
        )
        return None
    if cmd == "/undo":
        sd = os.path.join(
            ctx.get("snap_dir") or _SNAP_ROOT, str(ctx.get("sid") or "nosid")
        )
        man = os.path.join(sd, "manifest.jsonl")
        rows = []
        try:
            with open(man, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if ln:
                        try:
                            rows.append(json.loads(ln))
                        except ValueError:
                            pass
        except OSError:
            rows = []
        tgt = None
        for r in reversed(rows):
            if not r.get("consumed"):
                tgt = r
                break
        if tgt is None:
            sys.stderr.write("（没有可撤销的写文件快照）\n")
            return None
        try:
            if tgt.get("existed"):
                shutil.copy2(os.path.join(sd, tgt.get("backup") or ""), tgt["target"])
                act = "已还原 %s（自快照 %s）" % (tgt["target"], tgt.get("backup"))
            else:
                if os.path.exists(tgt["target"]):
                    os.unlink(tgt["target"])
                act = "已删除 %s（快照时点该文件尚不存在）" % tgt["target"]
            tgt["consumed"] = True
            with open(man, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
            sys.stderr.write("%s\n" % act)
        except OSError as e:
            sys.stderr.write("FAIL: 撤销失败：%s\n" % str(e)[:160])
        return None
    if cmd == "/memory":
        root = ctx.get("root") or ROOT
        files = _memory_files(root)
        if files:
            for name, body in files:
                sys.stderr.write(
                    "记忆 %s（%d 字节，单文件封顶 %d）：\n"
                    % (name, len(body), _MEMORY_CAP)
                )
                for ln in body.splitlines()[:8]:
                    sys.stderr.write("  %s\n" % ln)
        else:
            sys.stderr.write(
                "（%s 下没有 AGENTS.md / AGENTOS.md——"
                "建一个，每轮 system prompt 就带上项目记忆）\n" % root
            )
        rules, _ = _evo_rules_load()
        n_act = sum(1 for r in rules if r.get("state") == "ACTIVE")
        sys.stderr.write("自进化 ACTIVE 规则 %d 条（/evo 看清单）\n" % n_act)
        return None
    if cmd == "/ideas":
        _repl_ideas(ctx)
        return None
    if cmd == "/plan":
        # DSP 计划：rest 为空 = 看当前计划；否则 rest 当 goal 建新计划。
        # 新计划含 repo-map 侦察注脚 + 未审批锁（写工具锁死，/approve 解锁）。
        try:
            dsp_mod = _dsp()
        except ImportError:
            sys.stderr.write(
                "DSP 模块缺失（tools/dsp.py 不在）:计划能力不可用，主循环不受影响\n"
            )
            return None
        if not rest.strip():
            plan, err = dsp_mod.load_plan(sid)
            if plan is None:
                sys.stderr.write(
                    "还没有计划：/plan <一句话目标> 建一个"
                    "（先出任务树，人审后再动手）\n"
                )
            else:
                sys.stderr.write(dsp_mod.render_plan(plan) + "\n")
            return None
        # 建新计划：goal 一句 + repo-map 自动侦察 + 默认三任务骨架。
        # 骨架只是起点——模型/人随后用旧 string 换新 string 精修每项 accept。
        rmap = dsp_mod.repo_map(ctx.get("root") or ROOT)
        tasks = [
            {
                "id": "t1",
                "title": "侦察：读关键文件、确认现状",
                "accept": "关键文件已读 + 现状一句话 + rc=0",
                "tools": [
                    "list_dir",
                    "read_file",
                    "search",
                    "gh_search",
                    "gh_repo_view",
                    "gh_file_read",
                ],
                "depends": [],
            },
            {
                "id": "t2",
                "title": "动手：按判据改代码",
                "accept": "改动落盘 + 验证命令 rc=0（证据行）",
                "tools": [
                    "read_file",
                    "search",
                    "write_file",
                    "edit_file",
                    "run_cmd",
                    "git_diff",
                ],
                "depends": ["t1"],
            },
            {
                "id": "t3",
                "title": "收尾：自检 + 回执",
                "accept": "自检 rc=0 + 回执含真实 rc",
                "tools": ["run_cmd", "git_diff", "git_status", "git_log"],
                "depends": ["t2"],
            },
        ]
        plan = dsp_mod.new_plan(rest.strip(), tasks)
        ok, err = dsp_mod.save_plan(plan, sid)
        if not ok:
            sys.stderr.write("计划 schema 非法：%s\n" % err)
            return None
        sys.stderr.write(dsp_mod.render_plan(plan) + "\n")
        if rmap:
            sys.stderr.write(
                "— 仓库结构（repo-map，截断 %dB 内）—\n%s\n"
                % (dsp_mod.REPO_MAP_CAP, rmap)
            )
        sys.stderr.write(
            "下一步：检查每项判据（accept），没问题就 /approve"
            " 解锁执行；判据不对先改 plan.json 再 /approve。\n"
        )
        return None
    if cmd == "/approve":
        try:
            dsp_mod = _dsp()
        except ImportError:
            sys.stderr.write("DSP 模块缺失：无计划可批\n")
            return None
        ok, err = dsp_mod.approve_plan(sid)
        if ok:
            nxt = dsp_mod.next_task(dsp_mod.load_plan(sid)[0])
            sys.stderr.write(
                "计划已审批（写工具解锁）%s\n"
                % ("· 先做 %s" % nxt if nxt else "· 无待办")
            )
            ctx["dsp_task"] = nxt
        else:
            sys.stderr.write("批不了：%s\n" % err)
        return None
    if cmd == "/tasks":
        try:
            dsp_mod = _dsp()
        except ImportError:
            sys.stderr.write("DSP 模块缺失\n")
            return None
        plan, err = dsp_mod.load_plan(sid)
        if plan is None:
            sys.stderr.write("还没有计划（/plan 先出计划）\n")
            return None
        sys.stderr.write(dsp_mod.render_plan(plan) + "\n")
        nxt = dsp_mod.next_task(plan)
        if nxt:
            if (rest.strip() or nxt) != (
                ctx.get("dsp_task") or ""
            ) and rest.strip() in [t.get("id") for t in plan.get("tasks", [])]:
                ctx["dsp_task"] = rest.strip()
                sys.stderr.write("当前任务 → %s\n" % rest.strip())
            elif not ctx.get("dsp_task"):
                ctx["dsp_task"] = nxt
                sys.stderr.write("当前任务 → %s（下一个可做）\n" % nxt)
            else:
                sys.stderr.write(
                    "当前任务 %s（/tasks <id> 切换）\n" % ctx.get("dsp_task")
                )
        else:
            sys.stderr.write("（无待办：全 done 或卡住——/block 看偏离）\n")
        return None
    if cmd == "/block":
        try:
            dsp_mod = _dsp()
        except ImportError:
            sys.stderr.write("DSP 模块缺失\n")
            return None
        why = rest.strip() or "偏离计划、无进展（手动 BLOCK）"
        done = (
            [t.get("id") for t in (dsp_mod.load_plan(sid)[0] or {}).get("tasks", [])]
            if dsp_mod.load_plan(sid)[0]
            else []
        )
        p = dsp_mod.write_block(sid, why, done, "把当前任务拆小，或重出计划")
        sys.stderr.write("已 BLOCK：%s\n原因：%s\n" % (p or "（落盘失败）", why))
        return None
    if cmd == "/goal":
        # 自主执行：/goal <目标> 出菱形 DAG 计划；/goal run 一次审批后全
        # 流程无人干预跑到收口（派发/验证/自救≤2 次/断点可续/诚实报告）；
        # /goal resume 同 run（已完成任务零重跑）；空参看计划状态。
        try:
            dsp_mod = _dsp()
            leader_mod = _leader_mod()
            autonomy_mod = _autonomy()
        except ImportError:
            sys.stderr.write(
                "DSP/leader/autonomy 模块缺失：自主执行不可用，主循环不受影响\n"
            )
            return None
        arg = rest.strip()
        if not arg:
            plan, err = dsp_mod.load_plan(sid)
            if plan is None:
                sys.stderr.write(
                    "还没有目标计划：/goal <一句话目标> 出 "
                    "DAG 计划（/approve 后 /goal run 全流程"
                    "自跑）\n"
                )
            else:
                sys.stderr.write(dsp_mod.render_plan(plan) + "\n")
            return None
        if arg in ("run", "resume"):
            plan, err = dsp_mod.load_plan(sid)
            if plan is None:
                sys.stderr.write("还没有计划：/goal <目标> 先出计划\n")
                return None
            if not plan.get("approved"):
                sys.stderr.write(
                    "计划未审批：先 /approve（自主引擎不绕计划锁），再 /goal run\n"
                )
                return None
            rc = autonomy_mod.run_goal(
                sid,
                root=ctx.get("root") or ROOT,
                mode=arg,
                log=lambda s: sys.stderr.write(s + "\n"),
            )
            sys.stderr.write(
                "autonomy 收口 rc=%d（报告 work/autonomy/"
                "%s-REPORT.md；断点续跑 /goal resume）\n" % (rc, sid)
            )
            return None
        plan = leader_mod.compile_goal(arg, ctx.get("root") or ROOT)
        ok, err = dsp_mod.save_plan(plan, sid)
        if not ok:
            sys.stderr.write("计划 schema 非法：%s\n" % err)
            return None
        sys.stderr.write(dsp_mod.render_plan(plan) + "\n")
        sys.stderr.write(
            "下一步：检查每项判据（可给任务补 verify 命令组，"
            "引擎会真跑），/approve 审批后 /goal run 全流程"
            "无人干预跑到收口。\n"
        )
        return None
    if cmd in ("/reset", "/clear"):
        ctx["history"] = []
        sys.stderr.write("上下文已清空（模式仍是 %s）\n" % ctx["mode"])
        return None
    if cmd == "/compact":
        before = len(ctx.get("history") or [])
        ctx["history"], dropped, _dg = _compact_history_v2(
            ctx.get("history"), COMPACT_KEEP, provider=provider, focus=rest
        )
        sys.stderr.write(
            "上下文压缩：%d -> %d 条（丢 %d，保留最近 %d；摘要=%s）\n"
            % (
                before,
                len(ctx["history"]),
                dropped,
                COMPACT_KEEP,
                "有" if _dg else "无",
            )
        )
        return None
    if cmd == "/rewind":
        try:
            recs = _transcript_load(tpath) if os.path.isfile(tpath) else []
        except (OSError, ValueError) as e:  # noqa: BLE001
            sys.stderr.write(
                "/rewind：会话文件读不动（%s）——未改上下文\n" % str(e)[:60]
            )
            return None
        if rest in ("", "--list"):
            turns = [r for r in recs if r.get("kind") == "user" and r.get("text")]
            if not turns:
                sys.stderr.write(
                    "/rewind：本会话还没有轮次记录（%s）\n"
                    % os.path.basename(str(tpath))
                )
                return None
            for i, r in enumerate(turns, 1):
                sys.stderr.write("  第 %d 轮: %s\n" % (i, str(r.get("text"))[:60]))
            sys.stderr.write(
                "/rewind <N> 回到第 N 轮末（共 %d 轮；文件层不动，"
                "写操作回退用 /undo）\n" % len(turns)
            )
            return None
        try:
            n = int(rest)
        except ValueError:
            sys.stderr.write("/rewind：N 必须是整数（/rewind --list 看轮次）\n")
            return None
        msgs, err, turns_n = _rewind_replay(recs, n)
        if err:
            sys.stderr.write("/rewind 拒绝：%s —— 上下文未动\n" % err)
            return None
        ctx["history"] = msgs
        _transcript_append(
            tpath,
            {
                "ts": time.time(),
                "kind": "note",
                "text": "rewind → 第 %d/%d 轮"
                "（history 重建 %d 条）" % (n, turns_n, len(msgs)),
            },
        )
        sys.stderr.write(
            "已回退到第 %d/%d 轮：history 重建 %d 条"
            "（文件层未动；写操作回退用 /undo）\n" % (n, turns_n, len(msgs))
        )
        return None
    if cmd == "/skill":
        try:
            items = _skills_mod().discover(_skill_roots())
        except Exception as e:  # noqa: BLE001
            sys.stderr.write("/skill：技能目录读不动（%s）\n" % str(e)[:80])
            return None
        if not items:
            sys.stderr.write(
                "/skill：没有发现技能（~/.agentos/skills/*/SKILL.md"
                " 或 本仓 .agentos/skills/*/SKILL.md）\n"
            )
            return None
        if not rest:
            for it in items:
                sys.stderr.write(
                    "  %-24s %s\n" % (it["name"], str(it.get("description"))[:60])
                )
            sys.stderr.write("/skill <名> 注入正文（共 %d 个技能）\n" % len(items))
            return None
        it = next((x for x in items if x["name"] == rest), None)
        if it is None:
            sys.stderr.write("/skill：没有名为 %r 的技能（/skill 列表）\n" % rest[:60])
            return None
        body = str(it.get("body") or "").strip()[:20000]
        ctx.setdefault("history", []).append(
            {"role": "user", "content": "[skill %s]\n%s" % (it["name"], body)}
        )
        sys.stderr.write(
            "已注入技能 %s（%d 字符）到下一轮上下文\n" % (it["name"], len(body))
        )
        return None
    if cmd == "/cost":
        sys.stderr.write(
            "累计用量（端点自报，没报就是空）：%s\n"
            % json.dumps(ctx.get("usage_total") or {}, ensure_ascii=False)
        )
        return None
    if cmd == "/status":
        sys.stderr.write(_status_line(ctx, sid) + "\n")
        return None
    if cmd == "/model":
        if not rest:
            sys.stderr.write(
                "模型=%s · 端点=%s · 来源=%s\n"
                % (provider.model or "-", provider.base_url, provider.source)
            )
            return None
        ctx["model"] = rest
        sys.stderr.write("模型 → %s（下一轮生效）\n" % rest)
        # 换模型不等于换端点：protocol / session_id 必须原样带过去，
        # 否则刚从 responses 端点起跑的会话会被打回 chat/completions，
        # 网关当场 400 ModelProtocolUnsupported（本会话就踩过）。
        return Provider(
            {
                "base_url": provider.base_url,
                "model": rest,
                "headers": provider.headers,
                "protocol": provider.protocol,
                "session_id": provider.session_id,
                "provider_name": provider.provider_name,
                "source": provider.source,
            }
        )
    if cmd == "/diff":
        _repl_diff(ctx)
        return None
    if cmd == "/init":
        _repl_init(ctx)
        return None
    if cmd == "/sessions":
        files = _transcript_list()
        if not files:
            sys.stderr.write("(空) %s 下还没有 agent-*.jsonl 会话\n" % _session_dir())
        for p in files[-20:]:
            base = os.path.basename(p)
            mark = " ← 当前" if base == "%s.jsonl" % sid else ""
            sys.stderr.write("  %s%s\n" % (base[: -len(".jsonl")], mark))
        sys.stderr.write(
            "/branch [注记] 另开分支（当前会话不动）；"
            "/verify 验本会话关联计划的真完成\n"
        )
        return None
    if cmd == "/branch":
        note = rest.strip()
        nsrc = _transcript_path(sid)
        if not os.path.isfile(nsrc):
            sys.stderr.write("/branch：当前会话还没有落盘记录（至少聊一轮）\n")
            return None
        nsid = (
            "agent-%s" % time.strftime("%Y%m%d-%H%M%S") + "-%s" % uuid.uuid4().hex[:4]
        )
        try:
            bm = _branches_mod()
            ok_b, err_b = bm.fork_transcript(nsrc, _session_dir(), nsid, note)
        except ImportError:
            sys.stderr.write("/branch：branches 模块缺失\n")
            return None
        if not ok_b:
            sys.stderr.write("/branch 失败：%s\n" % err_b)
            return None
        sys.stderr.write(
            "已分支：%s（原会话 %s 不动；/sessions 回去，"
            "--resume %s 回来）\n" % (nsid, sid, nsid)
        )
        return None
    if cmd == "/verify":
        try:
            plan, err = _dsp().load_plan(sid)
        except ImportError:
            sys.stderr.write("/verify：DSP 模块缺失\n")
            return None
        if plan is None:
            sys.stderr.write("/verify：本会话无计划（%s）\n" % (err or "？"))
            return None
        try:
            rep = _branches_mod().verify_plan(plan)
        except ImportError:
            sys.stderr.write("/verify：branches 模块缺失\n")
            return None
        for v in rep["verdicts"]:
            sys.stderr.write(
                "  [%-16s] %s：%s\n" % (v["verdict"], v["id"], v["detail"])
            )
        sys.stderr.write(
            "真完成 %d/%d（其余未提前 done）\n" % (rep["n_done_true"], rep["n_total"])
        )
        _transcript_append(
            tpath,
            {
                "ts": time.time(),
                "kind": "note",
                "text": "/verify 真完成 %d/%d" % (rep["n_done_true"], rep["n_total"]),
            },
        )
        return None
    if cmd == "/steer":
        if not rest:
            try:
                _pl, _tot = _steer_mod().pending(_steer_dir(), sid)
                _pn = len(_pl)
            except Exception:  # noqa: BLE001
                _pn = 0
            sys.stderr.write(
                '/steer "指令"：给本会话投一条监督指令'
                "（下一步骤边界注入；只配送不裁决）；"
                "待消费 %d 条\n" % _pn
            )
            return None
        try:
            ok_s, err_s = _steer_mod().push(_steer_dir(), sid, rest)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write("/steer 失败：%s\n" % str(e)[:80])
            return None
        if ok_s:
            sys.stderr.write(
                "已投递老板指令：下一步骤边界注入本轮"
                "（跑着的 agent 会在一个步骤内看到）\n"
            )
        else:
            sys.stderr.write("/steer 失败：%s\n" % err_s)
        return None
    if cmd == "/watch":
        try:
            _wm = _watch_mod()
            sys.stdout.write(_wm.render(_wm.snapshot(ctx["root"], sid)) + "\n")
        except Exception as e:  # noqa: BLE001
            sys.stderr.write("/watch 失败：%s\n" % str(e)[:80])
        return None
    if cmd == "/mode":
        if rest in MODES:
            ctx["mode"] = rest
            sys.stderr.write("权限模式 → %s\n" % rest)
        else:
            sys.stderr.write("只支持：%s\n" % " ".join(MODES))
        return None
    sys.stderr.write("不认识的 REPL 命令：%s（/help）\n" % cmd)
    return None


def _repl(args):
    """交互式 REPL。上下文跨轮保留（history），权限模式可现场切。

    开箱即用：默认走 ~/.codex/config.toml 的端点；主端点连不通（403/400/
    不可达）时自动回退到本机 Ollama（若在跑），并在 stderr 如实打印回退。
    端点错误不再让 REPL 退出 —— 用户还在，换条路继续；只有 /exit 或 Ctrl-D
    才结束。默认开流式，让工具调用/增量可见（--no-stream 关掉）。

    会话落在 work/agent_sessions/agent-*.jsonl：每轮 user/assistant 各一条，
    退出时打印 session id；`agentos agent --continue` 或 `--resume <id>`
    接着聊。`!cmd` 走只读白名单（与工具层同一张表），`@path` 把文件内容
    内联进这一轮的需求。
    """
    stream = not getattr(args, "no_stream", False)
    try:
        provider = Provider(
            resolve_provider(args.env if hasattr(args, "env") else None)
        )
    except AgentError as e:
        if e.kind != "no-provider":
            sys.stderr.write("失败 [%s] %s\n" % (e.kind, e))
            return _rc_of_error(e.kind)
        cfg, blocked = _startup_local_backend()
        if not cfg:
            sys.stderr.write(
                "失败 [no-provider] %s%s\n"
                % (
                    e,
                    "（no_fallback=true：不换本地模型，请修好端点后重试）"
                    if blocked
                    else "",
                )
            )
            return _rc_of_error(e.kind)
        sys.stderr.write(
            "[回退] 没配端点，改用本机离线后端 %s\n" % (cfg.get("model") or "?")
        )
        provider = Provider(cfg)
    sid, resumed = _repl_session(args)
    ctx = {
        "root": args.root,
        "mode": args.mode,
        "approver": _make_approver(args),
        "max_steps": args.max_steps,
        "history": [],
        "usage_total": {},
    }
    _sf9 = _safety_flags(_read_agentos_config())
    ctx.update(_sf9)
    try:
        ctx["full_access"] = _full_access(_read_agentos_config())
    except Exception:
        ctx["full_access"] = False
    if ctx.get("full_access"):
        sys.stderr.write(
            "[完全权限] 破坏性命令放行（rm/git push 等，逐条留痕"
            " work/destructive_audit.jsonl）\n"
        )
    _on9 = [
        n
        for n, k in (
            ("计划锁", "plan_lock_off"),
            ("权限模式", "mode_gate_off"),
            ("命令白名单", "cmd_gate_off"),
        )
        if not _sf9[k]
    ]
    if _on9:
        sys.stderr.write(
            "[安全闸] 上锁：%s（~/.agentos/config.toml [agent] "
            "字面 true 才上锁；硬阻断清单默认不可关（full_access 除外））\n"
            % "、".join(_on9)
        )
    else:
        sys.stderr.write(
            "[安全闸] 自由面：计划锁/权限模式/命令白名单均关"
            "（默认以用户要求为中心；字面 true 才上锁；"
            "硬阻断清单默认不可关（full_access 除外））\n"
        )
    if resumed:
        ctx["history"] = resumed["history"]
        sys.stderr.write(
            "[续跑] 会话 %s：回灌 %d 条上下文\n"
            % (resumed["id"], len(resumed["history"]))
        )
    tpath = _transcript_path(sid)
    ctx["sid"] = sid
    _repl_welcome(provider, ctx, sid, resumed)
    _REPL_ROOT[0] = ctx["root"]
    _setup_readline()
    _evo_refresh_tool_hints()
    _mcp_sync()  # 挂 MCP 服务器工具（配置里有 [mcp] 才动）
    _hooks_session(ctx)  # hooks SessionStart（有 hooks.json 才动）
    while True:
        line = _readline("> ")
        if line is None:
            sys.stderr.write("\n会话 %s（%s）\n" % (sid, tpath))
            return 0
        text = line.strip()
        if not text:
            continue
        # 多行输入（Claude Code 手感）：行尾奇数个反斜杠 = 续行，继续用
        # 「… 」次级提示符收行，逐段拼到不含续行标记为止。
        while _line_wants_continue(text):
            nxt = _readline("… ")
            if nxt is None:
                break
            text = _compose_join(text, nxt)
            if not text.strip():
                break
        if not text:
            continue
        if text.startswith("/"):
            rc = _repl_slash(text, ctx, provider, sid, tpath)
            if rc == "exit":
                sys.stderr.write("会话 %s（%s）\n" % (sid, tpath))
                return 0
            if isinstance(rc, Provider):
                provider = rc
            continue
        if text.startswith("!"):
            _repl_bang(text[1:], ctx)
            continue
        prompt, refs = _expand_at_refs(text, ctx["root"])
        if refs:
            sys.stderr.write("[引用] %s\n" % ", ".join(refs))
        # [老板已批准]：批准词 → 硬指令 + 未审批计划按口谕自动批准（上链）
        # + 若本轮模型零工具调用则自动重试一轮（2026-10-14 空转事故）。
        _approved_turn = _looks_approved(text)
        if _approved_turn:
            prompt = _APPR_DIRECTIVE + text + "\n" + prompt
            try:
                _sidA = str(ctx.get("sid") or "")
                if _sidA:
                    _pA, _eA = _dsp().load_plan(_sidA)
                    if _pA is not None and not _pA.get("approved"):
                        _okA, _erA = _dsp().approve_plan(_sidA)
                        if _okA:
                            sys.stderr.write(
                                "[批准] 检测到老板批准用语："
                                "计划按口谕自动批准（已上哈希链）"
                                "—本轮直接开工\n"
                            )
            except Exception:  # noqa: BLE001
                pass
        t_turn = time.time()
        seen = {"delta": False}
        # 过程可见性：只在交互 TTY 起转轮线程；headless/管道零改动。
        sp = _TurnSpinner(sys.stderr.isatty(), _c).start()
        interrupt = threading.Event()
        ew = _EscWatcher(sys.stdin.isatty(), interrupt).start()
        try:
            res, provider = run_agent_with_fallback(
                prompt,
                ctx=ctx,
                provider=provider,
                stream=stream,
                on_event=_turn_ui(sp, _event_printer(stream, seen), seen),
                cancel=interrupt.is_set,
                allow_fallback=not _no_fallback(),
                retry_main=_no_fallback(),
            )
            # 批准轮零工具调用 = 没开工 → 自动重试一轮（有界一次）
            if _needs_approve_retry(
                _approved_turn, res.get("tools_used"), interrupt.is_set()
            ):
                sys.stderr.write("[批准] 本轮零工具调用（没真开工）——自动重试一轮\n")
                res, provider = run_agent_with_fallback(
                    _APPR_RETRY,
                    ctx=ctx,
                    provider=provider,
                    stream=stream,
                    on_event=_turn_ui(sp, _event_printer(stream, seen), seen),
                    cancel=interrupt.is_set,
                    allow_fallback=not _no_fallback(),
                    retry_main=_no_fallback(),
                )
            # 空答（端点抖动的典型形态）→ 自动重试一轮（有界一次）
            if res.get("empty_answer") and not interrupt.is_set():
                sys.stderr.write("[重试] 本轮模型空答——自动重试一轮\n")
                res, provider = run_agent_with_fallback(
                    "（上一轮你的回复为空）请重新给出实质回答；若任务需要"
                    "读文件/跑命令，现在就调用工具。",
                    ctx=ctx,
                    provider=provider,
                    stream=stream,
                    on_event=_turn_ui(sp, _event_printer(stream, seen), seen),
                    cancel=interrupt.is_set,
                    allow_fallback=not _no_fallback(),
                    retry_main=_no_fallback(),
                )
        except AgentError as e:
            if e.kind == "interrupted":
                # 用户按 Esc：不进失败规则，但如实入账（自评的体验信号）
                _evo_signal(
                    ctx.get("mode", ""),
                    False,
                    "interrupted",
                    round(time.time() - t_turn, 2),
                    [],
                )
                sys.stderr.write("\n已打断（Esc）：本轮终止，上下文保留\n")
                continue
            _evo_signal(
                ctx.get("mode", ""), False, e.kind, round(time.time() - t_turn, 2), []
            )
            sys.stderr.write("失败 [%s] %s（上下文保留，可继续输入）\n" % (e.kind, e))
            continue
        except KeyboardInterrupt:
            sys.stderr.write("\n已中止当前任务（上下文保留）\n")
            continue
        finally:
            sp.stop()
            ew.stop()
        fb = ctx.pop("last_fallback", None)
        if fb:
            _evo_signal(
                ctx.get("mode", ""),
                False,
                fb.get("kind", "provider"),
                round(time.time() - t_turn, 2),
                [],
                note="fallback-masked",
            )
        _empty_ans = bool(res.get("empty_answer"))
        if _empty_ans:
            sys.stderr.write(
                "（本轮模型返回空内容：多为端点抖动或模型"
                "卡壳——可重试，或用 /model 换一个端点）\n"
            )
        _evo_signal(
            ctx.get("mode", ""),
            (not _empty_ans),
            ("empty-answer" if _empty_ans else ""),
            res.get("elapsed")
            if res.get("elapsed") is not None
            else round(time.time() - t_turn, 2),
            [u.get("tool") for u in res.get("tools_used") or []],
            tools_fail=[
                u.get("tool")
                for u in res.get("tools_used") or []
                if u.get("executed") and not u.get("ok")
            ],
            denied=[
                u.get("tool")
                for u in res.get("tools_used") or []
                if not u.get("executed")
            ],
            denied_reasons=[
                "%s：%s" % (u.get("tool"), u.get("error"))
                for u in res.get("tools_used") or []
                if not u.get("executed")
            ],
        )
        # W13 诚实收尾：零证据完成声明打脸 + 跨轮复读检测（信号入账）
        _n_tools_turn = len(res.get("tools_used") or [])
        _ans0 = (res.get("answer") or "").strip()
        _claim_bad = _claim_no_evidence(_ans0, _n_tools_turn)
        _prev_ans = getattr(_repl, "_prev_ans", "")
        _stale = _is_stale_reply(_prev_ans, _ans0)
        if _claim_bad:
            sys.stderr.write(
                "（注：本轮 0 次工具调用，上面「已落实/已部署」"
                "类声明未附任何 rc/命令证据——本系统铁律：无证据"
                "的「完成」不算完成）\n"
            )
            try:
                _evo_signal(
                    ctx.get("mode", ""),
                    False,
                    "no-evidence-claim",
                    round(time.time() - t_turn, 2),
                    [],
                )
            except Exception:  # noqa: BLE001
                pass
        if _stale:
            sys.stderr.write(
                "（注：本轮终答与上一轮近乎逐字相同（模型"
                "复读）——可换端点：agentos config set llm.model "
                "<其他模型>）\n"
            )
            try:
                _evo_signal(
                    ctx.get("mode", ""),
                    False,
                    "stale-reply",
                    round(time.time() - t_turn, 2),
                    [],
                )
            except Exception:  # noqa: BLE001
                pass
        if _ans0:
            _repl._prev_ans = _ans0[:2000]
        ctx["usage_total"] = _usage_total(ctx.get("usage_total"), res.get("usage"))
        u = res.get("usage") or {}
        tot = ctx.get("usage_total") or {}
        _tu = res.get("tools_used") or []
        _tx = sum(1 for _x in _tu if _x.get("executed"))
        # ctx% 只在用户配置了 llm.context_window 才显示（不知道窗口大小就
        # 不装知道——CC 显示 context left 是因为它知道自己家的窗口）。
        _cw = 0
        try:
            _cw = int(
                (_read_agentos_config().get("llm") or {}).get("context_window") or 0
            )
        except Exception:  # noqa: BLE001
            _cw = 0
        _ctxseg = ""
        if _cw > 0 and tot.get("prompt_tokens"):
            _ctxseg = " · ctx %.0f%%" % (100.0 * tot["prompt_tokens"] / _cw)
        sys.stderr.write(
            _c(
                "dim",
                "  %d 步 %.1fs · 工具 %d 次 · 回执 %d/%d · "
                "本轮 ↑%s ↓%s tok · 累计 ↑%s ↓%s%s\n"
                % (
                    res.get("steps") or 0,
                    res.get("elapsed") or 0.0,
                    len(_tu),
                    _tx,
                    len(_tu),
                    u.get("prompt_tokens")
                    if u.get("prompt_tokens") is not None
                    else "-",
                    u.get("completion_tokens")
                    if u.get("completion_tokens") is not None
                    else "-",
                    tot.get("prompt_tokens")
                    if tot.get("prompt_tokens") is not None
                    else "-",
                    tot.get("completion_tokens")
                    if tot.get("completion_tokens") is not None
                    else "-",
                    _ctxseg,
                ),
            )
        )
        try:
            for r in _evo_midflight():
                sys.stderr.write(
                    _c(
                        "dim",
                        "[evo] 新规则候选 %s ×%s（/evo 查看，"
                        "/evo-apply 晋升）\n" % (r.get("id"), r.get("count")),
                    )
                )
        except Exception:  # noqa: BLE001
            pass
        _transcript_append(tpath, {"ts": time.time(), "kind": "user", "text": text})
        ans = (res.get("answer") or "").strip()
        _transcript_append(tpath, {"ts": time.time(), "kind": "assistant", "text": ans})
        if res.get("kind") == "limit":
            sys.stderr.write("预算用尽：%s\n" % res.get("error"))
        if stream and seen.get("delta"):
            sys.stdout.write("\n")  # 增量已逐字打过，别把终答再抄一遍
        elif not stream:
            sys.stdout.write(_c("brand", "⏺ ") + "%s\n\n" % _format_answer(ans))
        else:
            # 流式开着但一条增量都没来：⏺ 由这里补上再出终答
            sys.stdout.write(_c("brand", "⏺ ") + "%s\n\n" % _format_answer(ans))
        denied = [u for u in res.get("tools_used") or [] if not u.get("executed")]
        if denied:
            sys.stderr.write(
                "本轮 %d 次工具未执行（被拒/参数不对）：%s\n"
                % (len(denied), ", ".join(u["tool"] for u in denied))
            )


def _selftest():
    """自检：本地桩端点 + work/ 下的真实小文件，不碰真模型、不碰真网。

    每条用例的布尔都由**真实观察**判定：返回路径里的 kind、磁盘上文件在
    不在、sha256 变没变。有 FAIL 就 rc=1 —— 这里糊一次，上层全绿都是假的。
    """
    cases = []

    def case(name, ok, detail=""):
        cases.append([name, bool(ok), str(detail)])

    def jc(tool=None, content="", cid="call_1"):
        """一条 chat/completions 响应；tool=(工具名, 参数dict)。"""
        m = {"role": "assistant", "content": content}
        fin = "stop"
        if tool is not None:
            fn, args = tool
            m["tool_calls"] = [
                {
                    "id": cid,
                    "type": "function",
                    "function": {
                        "name": fn,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                }
            ]
            fin = "tool_calls"
        return {"json": {"choices": [{"index": 0, "message": m, "finish_reason": fin}]}}

    # 目录名带 pid：固定在用一个路径时，两个自检并发跑会互相看见对方留下
    # 的 py_ok.txt / auto.txt，把「文件没落地」这类判据莫名判红（2026-10-11
    # 实测被并发门打出一条 FAIL，单独重跑就绿 —— 典型的假红）。
    tmp = os.path.join(ROOT, "work", "_st_agent_core.%d" % os.getpid())
    try:
        _selftest_body(case, jc, tmp)
    finally:
        # rmtree 而不是 unlink+rmdir：自检正文可能建嵌套目录（如 ideas 例的
        # repo/tools/），旧清法只清得动平文件，嵌套目录一留就是 work/ 下的
        # _st_agent_core.* 残留（agent_core_gate 的 stray 判据当场抓红，
        # 2026-10-11 实测）。
        shutil.rmtree(tmp, ignore_errors=True)
    bad = [c for c in cases if not c[1]]
    for name, ok, detail in cases:
        sys.stdout.write(
            "%s %s%s\n"
            % ("PASS" if ok else "FAIL", name, ("  -- " + detail) if detail else "")
        )
    if bad:
        sys.stdout.write(
            "AGENT_CORE FAIL %d/%d\n" % (len(cases) - len(bad), len(cases))
        )
        return 1
    sys.stdout.write("AGENT_CORE OK %d/%d\n" % (len(cases), len(cases)))
    return 0


def _selftest_body(case, jc, tmp):
    """自检正文。拆出来只为让 _selftest 的 finally 清理保持简单。"""
    # (1) 终端白名单与 serve.py 逐项同源 -------------------------
    try:
        import importlib.util

        sp = os.path.join(ROOT, "tools", "serve.py")
        spec_ = importlib.util.spec_from_file_location("_st_serve", sp)
        mod = importlib.util.module_from_spec(spec_)
        spec_.loader.exec_module(mod)
        mine = _term_cmds()
        same = sorted(mod.TERM_CANDIDATES) == sorted(TERM_CANDIDATES) and sorted(
            mod.TERM_CMDS.items()
        ) == sorted(mine.items())
        detail = "serve.py %d 条 / agent_core %d 条%s" % (
            len(mod.TERM_CMDS),
            len(mine),
            "" if same else "（口径漂移）",
        )
    except Exception as e:  # noqa: BLE001
        same, detail = False, "import serve.py 失败：%s" % str(e)[:120]
    case("终端白名单与 serve.py 同源（逐项相等）", same, detail)

    tmproot = os.path.realpath(tmp)
    os.makedirs(tmproot, exist_ok=True)
    ctx_full = {"root": tmproot, "mode": "full"}
    ctx_plan = {"root": tmproot, "mode": "plan"}
    ctx_ae = {"root": tmproot, "mode": "accept-edits"}

    # (2) 非白名单命令：run_cmd 一律拒 ---------------------------
    r, ex = exec_tool("run_cmd", {"argv": ["curl", "http://evil.example"]}, ctx_full)
    case(
        "白名单外命令（curl）被拒",
        (not r["ok"]) and (not ex) and "白名单" in (r.get("error") or ""),
        (r.get("error") or "")[:60],
    )

    # (3) 四步循环：写 -> 读 -> 越界写被拒 -> 最终回答 -----------
    script = [
        jc(
            ("write_file", {"path": "hello.txt", "content": "hello-agentos\n"}),
            cid="c1",
        ),
        jc(("read_file", {"path": "hello.txt"}), cid="c2"),
        jc(
            ("write_file", {"path": "../../../../etc/pwn", "content": "pwned"}),
            cid="c3",
        ),
        jc(None, "AGENT_CORE-SELFTEST-OK"),
    ]
    stub = Stub(script)
    try:
        res = run_agent(
            "写个文件再读回来，越界的那步别真做",
            ctx={"root": tmproot, "mode": "accept-edits"},
            provider=stub.provider(),
            max_steps=6,
        )
    finally:
        stub.close()
    case(
        "4 步循环收敛到最终回答（steps==4）",
        res.get("ok")
        and res.get("steps") == 4
        and res.get("answer") == "AGENT_CORE-SELFTEST-OK",
        "ok=%s steps=%s answer=%r kind=%s"
        % (
            res.get("ok"),
            res.get("steps"),
            (res.get("answer") or "")[:40],
            res.get("kind"),
        ),
    )

    hello = os.path.join(tmproot, "hello.txt")
    body = ""
    if os.path.isfile(hello):
        with open(hello, encoding="utf-8") as f:
            body = f.read()
    case(
        "write_file 真写了文件（字节为据）",
        body == "hello-agentos\n",
        "bytes=%d" % len(body.encode("utf-8")),
    )

    aids = [
        tc.get("id")
        for m in res["messages"]
        if m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or [])
    ]
    tids = [m.get("tool_call_id") for m in res["messages"] if m.get("role") == "tool"]
    case(
        "tool 消息带回 tool_call_id（与 assistant 一一对上）",
        len(tids) == 3 and tids == aids[:3],
        "%r vs %r" % (aids[:3], tids),
    )

    case(
        "越界 write_file 被拒，且 /etc/pwn 确实不存在",
        (not os.path.exists("/etc/pwn"))
        and any(
            "越界" in (m.get("content") or "")
            for m in res["messages"]
            if m.get("role") == "tool"
        ),
        "escape 步骤在 tool 消息里如实回拒",
    )

    # (4) SSE：content 分片 + tool_calls 按 index 累积 ------------
    def sse(o):
        return "data: " + json.dumps(o, ensure_ascii=False)

    stream_text = (
        "\n\n".join(
            [
                sse(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "Hel"},
                                "finish_reason": None,
                            }
                        ]
                    }
                ),
                sse(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "lo, "},
                                "finish_reason": None,
                            }
                        ]
                    }
                ),
                sse(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "world"},
                                "finish_reason": None,
                            }
                        ]
                    }
                ),
                sse(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": None,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "c0",
                                            "type": "function",
                                            "function": {
                                                "name": "read_",
                                                "arguments": '{"pa',
                                            },
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                ),
                sse(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "tool_calls",
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "function": {"arguments": 'th":"a"}'},
                                        },
                                        {
                                            "index": 1,
                                            "id": "c1",
                                            "type": "function",
                                            "function": {
                                                "name": "health",
                                                "arguments": "{}",
                                            },
                                        },
                                    ]
                                },
                            }
                        ]
                    }
                ),
            ]
        )
        + "\n\n"
    )
    stub = Stub([{"sse": stream_text}])  # 故意不发 [DONE]
    try:
        got = stub.provider().chat([{"role": "user", "content": "hi"}], stream=True)
    finally:
        stub.close()
    case(
        "SSE 无 [DONE] 也能收尾：content 拼接 + tool_calls 按 index 累积",
        got.get("content") == "Hello, world"
        and len(got.get("tool_calls") or []) == 2
        and got["tool_calls"][0]["function"]["arguments"] == '{"path":"a"}'
        and got["tool_calls"][1]["function"]["name"] == "health",
        "content=%r calls=%r"
        % (
            got.get("content"),
            [
                (c["function"]["name"], c["function"]["arguments"])
                for c in got.get("tool_calls") or []
            ],
        ),
    )

    # (5) 端点问题一律归 provider，绝不猜成成功 ------------------
    for label, entry in (
        ("非 200", {"status": 500, "json": {"error": "boom"}}),
        ("响应形状不对", {"json": {"nope": 1}}),
    ):
        stub = Stub([entry])
        try:
            try:
                stub.provider().chat([{"role": "user", "content": "x"}])
                kind, msg = "", ""
            except AgentError as e:
                kind, msg = e.kind, str(e)
        finally:
            stub.close()
        case(
            "端点 %s -> AgentError(provider)，不退化成「模型说完了」" % label,
            kind == "provider",
            "%s: %s" % (kind, msg[:70]),
        )

    # (6) 截断必须显式标注 ---------------------------------------
    capped = _cap_output("x" * 100000, limit=1000)
    case(
        "_cap_output 截断且显式标注 truncated",
        len(capped) <= 1004 and "truncated" in capped and "100000/1000" in capped,
        "len=%d" % len(capped),
    )

    # (7) 权限：plan 拒写 / full+审批者也拦不住硬阻断 ------------
    p1 = os.path.join(tmproot, "plan.txt")
    r, ex = exec_tool("write_file", {"path": "plan.txt", "content": "x"}, ctx_plan)
    case(
        "plan 模式拒写（文件没落地）",
        (not r["ok"])
        and (not ex)
        and (not os.path.exists(p1))
        and "plan" in (r.get("error") or ""),
        (r.get("error") or "")[:60],
    )

    r, ex = exec_tool(
        "run_cmd",
        {"argv": ["git", "push"]},
        {"root": tmproot, "mode": "full", "approver": lambda *a: True},
    )
    case(
        "git push 在 full + 审批者全点头下仍被硬阻断",
        (not r["ok"]) and (not ex) and "硬阻断" in (r.get("error") or ""),
        (r.get("error") or "")[:70],
    )

    # (8) accept-edits：python3 -c 无审批者拒，有审批者点头则真跑
    py = ["python3", "-c", "open('py_ok.txt','w').write('1')"]
    r, ex = exec_tool("run_cmd", {"argv": py}, ctx_ae)
    case(
        "accept-edits 下 python3 -c 无审批者 -> 拒（文件没落地）",
        (not r["ok"])
        and (not ex)
        and (not os.path.exists(os.path.join(tmproot, "py_ok.txt"))),
        (r.get("error") or "")[:70],
    )
    r, ex = exec_tool(
        "run_cmd",
        {"argv": py},
        {"root": tmproot, "mode": "accept-edits", "approver": lambda *a: True},
    )
    case(
        "同一命令有审批者点头 -> 真跑（文件真出现，rc=0）",
        r["ok"]
        and r.get("rc") == 0
        and ex
        and os.path.isfile(os.path.join(tmproot, "py_ok.txt")),
        "rc=%s executed=%s" % (r.get("rc"), ex),
    )

    # (9) accept-edits 的本义：文件编辑自动放行；审批器说 No 则整体未执行
    r, ex = exec_tool("write_file", {"path": "auto.txt", "content": "x"}, ctx_ae)
    case(
        "accept-edits 下 write_file 自动放行（这正是该模式的本义）",
        r["ok"] and ex and os.path.isfile(os.path.join(tmproot, "auto.txt")),
        "ok=%s executed=%s" % (r.get("ok"), ex),
    )
    r, ex = exec_tool(
        "run_cmd",
        {"argv": ["python3", "-c", "open('no.txt','w').write('1')"]},
        {"root": tmproot, "mode": "accept-edits", "approver": lambda *a: False},
    )
    case(
        "approver 返回 False -> 未执行且文件未出现",
        (not r["ok"])
        and (not ex)
        and (not os.path.exists(os.path.join(tmproot, "no.txt")))
        and "审批" in (r.get("error") or ""),
        (r.get("error") or "")[:70],
    )

    # (10) 路径越界：读也拒 -------------------------------------
    r, ex = exec_tool("read_file", {"path": "../../../../etc/passwd"}, ctx_full)
    case(
        "read_file 越界被拒（../../etc/passwd）",
        (not r["ok"]) and "越界" in (r.get("error") or "") and "text" not in r,
        "%s（success 形状的 text 字段=%s）"
        % ((r.get("error") or "")[:60], "有" if "text" in r else "无"),
    )

    # (11) edit_file：old_string 找不到 -> sha256 一字不变 -------
    ef = os.path.join(tmproot, "edit.txt")
    with open(ef, "w", encoding="utf-8") as f:
        f.write("alpha beta gamma\n")
    before = _sha(ef)
    r, ex = exec_tool(
        "edit_file",
        {"path": "edit.txt", "old_string": "不在文件里", "new_string": "y"},
        ctx_full,
    )
    case(
        "edit_file 找不到 old_string -> 拒改且 sha256 不变",
        (not r["ok"]) and _sha(ef) == before,
        "sha before=%s after=%s" % (before, _sha(ef)),
    )

    # (12) 工具输出超限 -> 截断且显式标注 -----------------------
    r, ex = exec_tool(
        "run_cmd", {"argv": ["python3", "-c", "print('x' * 100000)"]}, ctx_full
    )
    case(
        "run_cmd 输出超限 -> 截断并显式标注（长度可见）",
        r["ok"]
        and r.get("truncated") is True
        and 32000 < len(r.get("out") or "") <= TERM_MAX_OUT,
        "out=%d truncated=%s" % (len(r.get("out") or ""), r.get("truncated")),
    )

    # (13) 未知工具 / 坏 JSON 参数：不崩，如实回灌 ---------------
    r, ex = exec_tool("no_such_tool", {}, ctx_full)
    case(
        "未知工具名 -> 结构化拒绝，不抛异常",
        (not r["ok"]) and (not ex) and "未知工具" in (r.get("error") or ""),
        (r.get("error") or "")[:70],
    )

    stub = Stub(
        [
            {
                "json": {
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "c1",
                                        "type": "function",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": "not-json",
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                }
            },
            jc(None, "BADARGS-OK"),
        ]
    )
    try:
        res = run_agent(
            "读文件",
            ctx={"root": tmproot, "mode": "full"},
            provider=stub.provider(),
            max_steps=4,
        )
    finally:
        stub.close()
    toolmsg = [m.get("content") for m in res["messages"] if m.get("role") == "tool"]
    case(
        "坏 JSON 参数不崩：如实回灌并继续到最终回答",
        res.get("ok")
        and res.get("answer") == "BADARGS-OK"
        and any("不是合法 JSON" in (t or "") for t in toolmsg),
        "steps=%s toolmsg=%r" % (res.get("steps"), (toolmsg or [""])[0][:60]),
    )

    # (14) max_steps 触顶 -> kind=limit，终答为空 -----------------
    stub = Stub(
        [
            jc(("read_file", {"path": "nope-%d.txt" % i}), cid="c%d" % i)
            for i in range(3)
        ]
    )
    try:
        res = run_agent(
            "一直读不存在的文件",
            ctx={"root": tmproot, "mode": "full"},
            provider=stub.provider(),
            max_steps=2,
        )
    finally:
        stub.close()
    case(
        "max_steps 触顶 -> kind=limit，终答为空（不冒充成功）",
        (not res.get("ok"))
        and res.get("kind") == "limit"
        and res.get("answer") is None
        and res.get("steps") == 2,
        "kind=%s steps=%s error=%r"
        % (res.get("kind"), res.get("steps"), (res.get("error") or "")[:50]),
    )

    # (15) 空需求：用法错误，不进循环 ----------------------------
    st = Stub([])
    try:
        try:
            run_agent("   ", ctx={"root": tmproot}, provider=st.provider())
            kind = ""
        except AgentError as e:
            kind = e.kind
    finally:
        st.close()
    case("空需求抛 AgentError(usage)，不进循环", kind == "usage", "kind=%s" % kind)

    # (16) 离线后端开关：显式开才给本机 Ollama，未开不许自动探测 ----
    # 为什么把「未开」也钉进判据：自动探测本机 11434 会把「没配端点」
    # 谎成「有端点」，neg_agent_core 的空 HOME 判据正是治这个的。
    old_home = os.environ.get("HOME")
    try:
        os.environ["HOME"] = tmproot  # 空 HOME ⇒ 没有 codex 配置
        try:
            resolve_provider({})
            off_kind = "没抛错（fail-open）"
        except AgentError as e:
            off_kind = e.kind
        loc = resolve_provider({"AGENTOS_LLM_LOCAL": "ollama"})
        ok_local = loc.get("base_url") == "http://127.0.0.1:11434/v1" and bool(
            loc.get("model")
        )
        ok_off = off_kind == "no-provider"
        detail = "开=%s / 未开=%s" % (loc.get("base_url"), off_kind)
    except Exception as e:  # noqa: BLE001
        ok_local, ok_off, detail = False, False, str(e)[:120]
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
    case(
        "离线后端只在显式开时生效（未开不自动探测 11434）", ok_local and ok_off, detail
    )

    # (17) 命名 provider：内置端点表解析；未知名 fail-closed，不许猜 ----
    # 为什么钉进判据：--provider 是新加的入口，内置表写错（url/模型名）
    # 或未知名不报错（fail-open 到 codex 配置），都是用户当场撞上的事。
    try:
        # 注意：模型断言必须钉**端点表内的默认值**而不是「config 当前值」。
        # resolve 会把 ~/.agentos/config.toml 的 llm.model 当覆写注入 env
        # （这是切模型的正路），用户换模型的瞬间不该把这条例自检踢红。
        _tmodel = NAMED_PROVIDERS["opencode"]["model"]
        spec = resolve_provider(
            {
                "AGENTOS_PROVIDER": "opencode",
                "AGENTOS_LLM_MODEL": _tmodel,
            }
        )
        ok_named = (
            spec.get("base_url") == "https://opencode.ai/zen/go/v1"
            and spec.get("model") == "muse-spark-1.3-contributor"
            # opencode 的 /chat/completions 实测 400，协议必须跟着
            # 端点表走；这条断言红 = 有人在表里删了 protocol。
            and spec.get("protocol") == "responses"
            # 2026-10-17 起该端点强制会话头；值由 Provider 持有并在
            # rotate_session 时原子轮换 —— resolve 层带值会让轮换失效。
            and spec.get("headers", {}).get("x-opencode-session") is None
        )
        try:
            resolve_provider({"AGENTOS_PROVIDER": "no-such-provider"})
            bad = "没抛错（fail-open）"
        except AgentError as e:
            bad = e.kind
        ok_bad = bad == "no-provider"
        detail = "opencode=%s protocol=%s / 未知名=%s" % (
            spec.get("base_url"),
            spec.get("protocol"),
            bad,
        )
    except Exception as e:  # noqa: BLE001
        ok_named, ok_bad, detail = False, False, str(e)[:120]
    case(
        "命名 provider：opencode 解析出内置端点且未知名报错",
        ok_named and ok_bad,
        detail,
    )

    # (18) 交互回退：主端点 403 → 换本机离线后端重试一次；headless 不回退
    # 为什么把「headless 不回退」也钉进判据：回退是给交互 REPL 的兜底，
    # 若 -p 也回退，CI 里「没配端点」会被本机模型悄悄洗成绿 —— 又一种假绿。
    stub_main = Stub([{"status": 403, "json": {"error": "blocked"}}])
    stub_fb = Stub([jc(None, "FALLBACK-OK")])

    def _fb_cfg():
        return {
            "base_url": stub_fb.base(),
            "model": "fb-model",
            "headers": {},
            "provider_name": "ollama-local",
            "source": "fallback:ollama",
        }

    try:
        res, used = run_agent_with_fallback(
            "回退自检",
            ctx={"root": tmproot, "mode": "plan"},
            provider=stub_main.provider(),
            allow_fallback=True,
            probe=_fb_cfg,
        )
        ok_fb = (
            res.get("ok")
            and res.get("answer") == "FALLBACK-OK"
            and used is not None
            and used.source == "fallback:ollama"
        )
        fb_detail = "answer=%r source=%s" % (
            (res.get("answer") or "")[:30],
            used.source if used is not None else None,
        )
    except Exception as e:  # noqa: BLE001
        ok_fb, fb_detail = False, "%s: %s" % (type(e).__name__, str(e)[:80])
    finally:
        stub_main.close()
        stub_fb.close()
    case("交互回退：主端点 403 自动换离线后端并成功", ok_fb, fb_detail)

    stub_main = Stub([{"status": 403, "json": {"error": "blocked"}}])
    try:
        try:
            run_agent_with_fallback(
                "不许回退",
                ctx={"root": tmproot, "mode": "plan"},
                provider=stub_main.provider(),
                allow_fallback=False,
                probe=lambda: {
                    "base_url": "http://127.0.0.1:1/v1",
                    "model": "x",
                    "headers": {},
                    "source": "fallback:ollama",
                },
            )
            hd_kind = "没抛错（headless 被洗绿）"
        except AgentError as e:
            hd_kind = e.kind
    finally:
        stub_main.close()
    case(
        "headless 不回退：allow_fallback=False 时原样抛 provider",
        hd_kind == "provider",
        "kind=%s" % hd_kind,
    )

    # (18b) no_fallback：主端点先 500 两次再好 → 一直重试主端点成功，
    # 本地后端零触碰；cancel 已触发时第一次失败即抛（不是死循环）。
    # 为什么钉：用户要的是「主端点失败就一直等主端点」的字面保证 ——
    # probe 零调用证明没偷换本地，seen==3 证明重试打的是主端点。
    stub_flap = Stub(
        [
            {"status": 500, "json": {"error": "boom1"}},
            {"status": 500, "json": {"error": "boom2"}},
            jc(None, "MAIN-RECOVERED"),
        ]
    )
    probe_hits = []
    slept = []

    def _never_probe():
        probe_hits.append(1)
        return None

    try:
        res_nf, used_nf = run_agent_with_fallback(
            "no_fallback 自检",
            ctx={"root": tmproot, "mode": "plan"},
            provider=stub_flap.provider(),
            allow_fallback=False,
            retry_main=True,
            probe=_never_probe,
            cancel=lambda: False,
            sleep=slept.append,
        )
        ok_nf = (
            res_nf.get("ok")
            and res_nf.get("answer") == "MAIN-RECOVERED"
            and used_nf is not None
            and used_nf.source != "fallback:ollama"
            and len(stub_flap.seen) == 3
            and not probe_hits
            and len(slept) == 2
        )
        nf_detail = "answer=%r seen=%d probe=%d slept=%r" % (
            (res_nf.get("answer") or "")[:20],
            len(stub_flap.seen),
            len(probe_hits),
            slept,
        )
    except Exception as e:  # noqa: BLE001
        ok_nf, nf_detail = False, "%s: %s" % (type(e).__name__, str(e)[:80])
    finally:
        stub_flap.close()
    case(
        "no_fallback：主端点抖动只重试主端点（本地零触碰，Esc/cancel 可停）",
        ok_nf,
        nf_detail,
    )
    stub_down = Stub([{"status": 500, "json": {"error": "down"}}])
    cx_calls = []
    slept2 = []

    def _cx_after_first():
        # 第一次失败后才置位：模拟用户在看到第一次重试提示后按 Esc。
        # run_agent 步首也会查 cancel，所以前两次调用必须放行，
        # 否则连 HTTP 都打不出去（那是另一个已有判据在管的行为）。
        cx_calls.append(1)
        return len(cx_calls) > 2

    try:
        try:
            run_agent_with_fallback(
                "cancel 出口",
                ctx={"root": tmproot, "mode": "plan"},
                provider=stub_down.provider(),
                allow_fallback=False,
                retry_main=True,
                probe=_never_probe,
                cancel=_cx_after_first,
                sleep=slept2.append,
            )
            cx_kind = "没抛错（死循环或吞错）"
        except AgentError as e:
            cx_kind = e.kind
    finally:
        stub_down.close()
    case(
        "no_fallback cancel 出口：cancel 已触发时第一次失败即抛 provider",
        cx_kind == "provider" and len(stub_down.seen) == 1 and slept2 == [1],
        "kind=%s seen=%d slept=%r" % (cx_kind, len(stub_down.seen), slept2),
    )

    # (18b-2) 会话中毒 400 的自救判据（W22 双刺）：
    # ① 词表外 400（No tool call found…不含任何传输词）也必须轮换重试
    #    ——曾因中毒分支排在词表门后面被 `not _TRANSIENT_RE → raise`
    #    一刀毙命，rotate 永远轮不到（实测第六圈就这样死）。
    # ② 轮换走独立预算 _SESS_MAX：三连跪后如实抛 provider，
    #    不许无限 reroll（也不许和 SSL 池互相烧光）。
    stub_poison = Stub(
        [
            {
                "status": 400,
                "json": {
                    "error": {
                        "message": "No tool call found for tool output "
                        "with call_id call_01_poison"
                    }
                },
            },
            jc(None, "POISON-RECOVERED"),
        ]
    )
    _rot = stub_poison.provider()
    _rots = []
    _rot_orig = _rot.rotate_session

    def _cnt_rot():
        _rots.append(1)
        _rot_orig()

    _rot.rotate_session = _cnt_rot
    try:
        res_pois = run_agent(
            "会话中毒自检",
            ctx={"root": tmproot, "mode": "plan"},
            provider=_rot,
        )
        pois_ok = (
            res_pois.get("ok")
            and res_pois.get("answer") == "POISON-RECOVERED"
            and len(stub_poison.seen) == 2
            and len(_rots) == 1
        )
        pois_detail = "answer=%r seen=%d rot=%d" % (
            (res_pois.get("answer") or "")[:20],
            len(stub_poison.seen),
            len(_rots),
        )
    except Exception as e:  # noqa: BLE001
        pois_ok, pois_detail = False, "%s: %s" % (type(e).__name__, str(e)[:90])
    finally:
        stub_poison.close()
    case(
        "会话中毒 400（词表外）也轮换重试且成功",
        pois_ok,
        pois_detail,
    )

    stub_poison3 = Stub(
        [
            {
                "status": 400,
                "json": {"error": {"message": "MissingSessionID"}},
            },
        ]
    )  # 脚本尾永远复放同一条坏 400 → 必然走到预算上限
    try:
        try:
            run_agent(
                "会话中毒预算自检",
                ctx={"root": tmproot, "mode": "plan"},
                provider=stub_poison3.provider(),
            )
            pois3_kind = "没抛错（无限 reroll？）"
        except AgentError as e:
            pois3_kind = e.kind
        pois3_ok = pois3_kind == "provider" and len(stub_poison3.seen) == 4
        # seen=4：初试 + 3 次轮换重试（1 初 + _SESS_MAX=3），第 5 次不再打
        pois3_detail = "kind=%s seen=%d" % (pois3_kind, len(stub_poison3.seen))
    finally:
        stub_poison3.close()
    case("会话中毒轮换有独立上限（3 次到顶如实例外）", pois3_ok, pois3_detail)

    # (18c) no_fallback 判据矩阵 + 启动期兜底零触碰。
    # 为什么两件事都要钉：只堵中途回退、启动时照样换本地，等于「云端挂了
    # 静默降级」；而读侧若把 "yes"/"True"/布尔 True 也当真，用户以为关掉
    # 了兜底，实际还在换 —— 两头都得有反例才不是眼看。
    G = globals()
    _orig_read_g = G["_read_agentos_config"]
    _orig_probe_g = G["_probe_ollama"]
    probe18c = []

    def _fake_read_g():
        return {"llm": {"no_fallback": "true"}}

    def _fake_probe_g():
        probe18c.append(1)
        return {"base_url": "http://127.0.0.1:11434/v1",
                "model": "qwen3:0.6b", "headers": {},
                "protocol": "chat", "provider_name": "ollama-local",
                "source": "probe:fake"}

    mat18c = []
    for _raw, _want in (("true", True), ("True", False), ("yes", False),
                        (True, False), (None, False), ("false", False)):
        G["_read_agentos_config"] = (
            lambda v: (lambda: ({} if v is None
                                else {"llm": {"no_fallback": v}}))
        )(_raw)
        try:
            _got = _no_fallback()
        finally:
            G["_read_agentos_config"] = _orig_read_g
        if _got != _want:
            mat18c.append("%r→%s 期望 %s" % (_raw, _got, _want))
    case("no_fallback 读侧矩阵：只认字面 \"true\"（布尔/True/yes 全不当真）",
         not mat18c, "; ".join(mat18c) or "6 例全对")

    G["_read_agentos_config"] = _fake_read_g
    G["_probe_ollama"] = _fake_probe_g
    try:
        s_cfg, s_blocked = _startup_local_backend()
    finally:
        G["_read_agentos_config"] = _orig_read_g
        G["_probe_ollama"] = _orig_probe_g
    case("no_fallback 时启动期兜底也不碰本地后端（probe 零调用）",
         s_cfg is None and s_blocked is True and not probe18c,
         "cfg=%r blocked=%r probe=%d" % (s_cfg, s_blocked, len(probe18c)))

    G["_read_agentos_config"] = lambda: {}
    G["_probe_ollama"] = _fake_probe_g
    try:
        d_cfg, d_blocked = _startup_local_backend()
    finally:
        G["_read_agentos_config"] = _orig_read_g
        G["_probe_ollama"] = _orig_probe_g
    case("缺键（默认自由面）：启动期兜底照旧可用本地后端",
         isinstance(d_cfg, dict) and d_blocked is False,
         "cfg=%r blocked=%r" % (d_cfg, d_blocked))

    # (19) Responses 协议：翻译 / 非流式 / SSE / 失败分类 -----------
    # 为什么整块单列：opencode 网关只认 /responses（chat/completions 实测
    # 回 HTTP 400 ModelProtocolUnsupported）。这条路径此前零覆盖，等于
    # 「换协议还能用」只是眼看，不是验过。
    def _resp_json(text, cid="call_x", usage=None):
        """一条 responses 形状的非流式 JSON 响应。"""
        out = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }
        if cid is None:
            items = [out]
        else:
            items = [
                {
                    "type": "function_call",
                    "call_id": cid,
                    "name": "read_file",
                    "arguments": '{"path": "note.txt"}',
                },
                out,
            ]
        return {
            "json": {
                "id": "resp_stub",
                "status": "completed",
                "output": items,
                "usage": usage
                or {
                    "input_tokens": 11,
                    "output_tokens": 7,
                    "total_tokens": 18,
                    "output_tokens_details": {"reasoning_tokens": 3},
                },
            }
        }

    # ① 入参翻译：messages → input[]（只有 tool_calls / tool 要变形）
    tr_in = _to_responses_input(
        [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "USER"},
            {
                "role": "assistant",
                "content": "ASTEXT",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "README.md"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "FILE-OUT"},
        ]
    )
    in_ok = (
        len(tr_in) == 5
        and tr_in[0] == {"role": "system", "content": "SYS"}
        and tr_in[1] == {"role": "user", "content": "USER"}
        and tr_in[2] == {"role": "assistant", "content": "ASTEXT"}
        and tr_in[3].get("type") == "function_call"
        and tr_in[3].get("call_id") == "c1"
        and tr_in[3].get("name") == "read_file"
        and tr_in[3].get("arguments") == '{"path": "README.md"}'
        and tr_in[4]
        == {"type": "function_call_output", "call_id": "c1", "output": "FILE-OUT"}
    )
    case(
        "Responses 入参翻译：messages → input[]（含 function_call 两项）",
        in_ok,
        "types=%s" % [i.get("type") or i.get("role") for i in tr_in],
    )

    # ② 工具表拍扁：嵌套 → 扁平 + strict:false
    tr_tools = _to_responses_tools(tool_defs())
    passthru = _to_responses_tools(
        [{"type": "function", "name": "x", "parameters": {"type": "object"}}]
    )
    tools_ok = (
        bool(tr_tools)
        and len(tr_tools) == len(tool_defs())
        and all(
            t.get("type") == "function"
            and "function" not in t
            and t.get("strict") is False
            and bool(t.get("name"))
            and bool(t.get("parameters"))
            for t in tr_tools
        )
        and any(t.get("name") == "read_file" for t in tr_tools)
        and passthru
        == [{"type": "function", "name": "x", "parameters": {"type": "object"}}]
    )
    case(
        "Responses 工具表拍扁：嵌套形状 → 扁平 + strict:false（不二次包装）",
        tools_ok,
        "n=%d first=%s"
        % (len(tr_tools), json.dumps(tr_tools[0], ensure_ascii=False)[:90]),
    )

    # ③ 未知协议 fail-closed：猜一次就等于把「配错」翻译成「端点坏了」
    try:
        _norm_protocol("grpc")
        proto_bad = "没抛错（猜协议）"
    except AgentError as e:
        proto_bad = e.kind
    case(
        "未知协议名抛 provider（不猜）", proto_bad == "provider", "kind=%s" % proto_bad
    )

    # ④ responses 端到端两步：工具调用 → 结果回灌 → 终答
    note = os.path.join(tmproot, "note.txt")
    with open(note, "w", encoding="utf-8") as f:
        f.write("note line\n")
    stub_r = Stub([_resp_json(None, cid="c1"), _resp_json("RESP-E2E-OK", cid=None)])
    try:
        res = run_agent(
            "读 note.txt",
            ctx={"root": tmproot, "mode": "full"},
            provider=stub_r.provider(protocol="responses"),
            max_steps=4,
        )
    finally:
        stub_r.close()
    seen = stub_r.seen
    e2e_ok = bool(
        res.get("ok")
        and res.get("answer") == "RESP-E2E-OK"
        and len(seen) >= 2
        and "input" in seen[0]
        and "messages" not in seen[0]
        and seen[0].get("max_output_tokens") == RESPONSES_MAX_OUTPUT_TOKENS
        and (seen[0].get("reasoning") or {}).get("effort") == RESPONSES_REASONING_EFFORT
        and seen[1]["input"][-1].get("type") == "function_call_output"
        and seen[1]["input"][-1].get("call_id") == "c1"
        and seen[1].get("tools")
        and "function" not in seen[1]["tools"][0]
        and seen[1]["tools"][0].get("strict") is False
    )
    case(
        "Responses 端到端：request 走 input[]，工具结果回灌成 function_call_output",
        e2e_ok,
        "steps=%s last=%s tools0=%s"
        % (
            res.get("steps"),
            json.dumps(seen[1]["input"][-1], ensure_ascii=False)[:60]
            if len(seen) > 1
            else "-",
            json.dumps(seen[1]["tools"][0], ensure_ascii=False)[:60]
            if len(seen) > 1
            else "-",
        ),
    )

    # ⑤ responses SSE：deltas 推进 + completed 内嵌 response 优先
    sse_final = {
        "id": "resp_sse",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "SSE-OK"}],
            },
            {
                "type": "function_call",
                "call_id": "c9",
                "name": "read_file",
                "arguments": '{"path": "README.md"}',
            },
        ],
        "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
    }
    sse_lines = "\n".join(
        [
            "data: " + json.dumps({"type": "response.created"}),
            "data: "
            + json.dumps(
                {
                    "type": "response.output_item.added",
                    "item": {
                        "id": "i1",
                        "type": "function_call",
                        "call_id": "c9",
                        "name": "read_file",
                    },
                }
            ),
            "data: "
            + json.dumps(
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "i1",
                    "delta": '{"path":',
                }
            ),
            "data: "
            + json.dumps(
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "i1",
                    "delta": ' "README.md"}',
                }
            ),
            "data: "
            + json.dumps({"type": "response.output_text.delta", "delta": "SSE-"}),
            "data: "
            + json.dumps({"type": "response.output_text.delta", "delta": "OK"}),
            "data: "
            + json.dumps({"type": "response.completed", "response": sse_final}),
            "",
        ]
    )
    stub_s = Stub([{"sse": sse_lines}])
    deltas = []
    try:
        out = stub_s.provider(protocol="responses").chat(
            [{"role": "user", "content": "x"}],
            stream=True,
            on_event=lambda ev: deltas.append(ev.get("text") or ""),
        )
    finally:
        stub_s.close()
    sse_ok = (
        out.get("content") == "SSE-OK"
        and out.get("sse_done") is True
        and len(out.get("tool_calls") or []) == 1
        and (out["tool_calls"][0].get("function") or {}).get("name") == "read_file"
        and (out["tool_calls"][0].get("function") or {}).get("arguments")
        == '{"path": "README.md"}'
        and out.get("usage", {}).get("prompt_tokens") == 3
        and out.get("usage", {}).get("completion_tokens") == 4
        and deltas == ["SSE-", "OK"]
    )
    case(
        "Responses SSE：delta 推进 + completed 内嵌 response 优先 + usage 键名映射",
        sse_ok,
        "content=%r calls=%d usage=%s deltas=%s"
        % (
            out.get("content"),
            len(out.get("tool_calls") or []),
            json.dumps(out.get("usage") or {}, ensure_ascii=False),
            deltas,
        ),
    )

    # ⑥ 失败分类：incomplete / 空 output 都必须归 provider，不洗成成功
    stub_bad = Stub(
        [
            {
                "json": {
                    "id": "r_bad",
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [],
                }
            }
        ]
    )
    try:
        try:
            stub_bad.provider(protocol="responses").chat(
                [{"role": "user", "content": "x"}]
            )
            inc_kind = "没抛错（洗成成功）"
        except AgentError as e:
            inc_kind = e.kind
    finally:
        stub_bad.close()
    try:
        _norm_responses({"status": "completed", "output": []})
        empty_kind = "没抛错（洗成成功）"
    except AgentError as e:
        empty_kind = e.kind
    case(
        "Responses 失败分类：incomplete 与空 output 都归 provider",
        inc_kind == "provider" and empty_kind == "provider",
        "incomplete=%s empty=%s" % (inc_kind, empty_kind),
    )

    # ⑦ REPL /model 换模型必须连协议带回去：responses 端点上只换模型名，
    # 丢了 protocol 就被打回 /chat/completions，网关当场 400。
    p_proto = stub_r.provider(protocol="responses")
    new_p = _repl_slash("/model other-model", {"root": tmproot}, p_proto, "sid-r", "x")
    case(
        "REPL /model 重建 Provider 时保住 protocol 与 session_id",
        isinstance(new_p, Provider)
        and new_p.protocol == "responses"
        and new_p.session_id == p_proto.session_id
        and new_p.model == "other-model",
        "protocol=%s session=%s/%s"
        % (
            new_p.protocol if isinstance(new_p, Provider) else "-",
            (new_p.session_id if isinstance(new_p, Provider) else "-")[:16],
            p_proto.session_id[:16],
        ),
    )

    # (17) REPL 会话/命令的纯函数（对齐 claude code 的终端面）-------

    # (17) REPL 会话/命令的纯函数（对齐 claude code 的终端面）-------
    sid = _new_session_id()
    case(
        "_new_session_id 形状：agent- 前缀 + 时间戳 + 4 位后缀",
        re.match(r"^agent-\d{8}-\d{6}-[0-9a-f]{4}$", sid) is not None,
        "sid=%s" % sid,
    )

    tpath = os.path.join(tmproot, "t.jsonl")
    ok_rt, detail_rt = True, ""
    try:
        _transcript_append(tpath, {"kind": "user", "text": "u1"})
        _transcript_append(tpath, {"kind": "assistant", "text": "a1"})
        _transcript_append(tpath, {"kind": "meta", "text": "x"})
        recs = _transcript_load(tpath)
        hist = _history_from_transcript(recs)
        ok_rt = len(recs) == 3 and hist == [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
        ]
        detail_rt = "recs=%d hist=%d" % (len(recs), len(hist))
    except Exception as e:  # noqa: BLE001
        ok_rt, detail_rt = False, str(e)[:80]
    case("会话 JSONL 追加/读回/还原成 messages（meta 不还原）", ok_rt, detail_rt)

    hist = [{"role": "system", "content": "s"}] + [
        {"role": "user", "content": "m%d" % i} for i in range(30)
    ]
    comp, dropped = _compact_history(hist, keep=5)
    case(
        "_compact_history 保留 system + 最近 N 条（丢弃数可见）",
        len(comp) == 6
        and comp[0]["role"] == "system"
        and comp[-1]["content"] == "m29"
        and dropped == 25,
        "len=%d dropped=%d" % (len(comp), dropped),
    )

    # (W1) compact v2：摘要进历史 / 失败逐字回退 / 聚焦词透传 -----------
    _dstub = Stub([jc(content="[摘要] 关键路径 tools/dsp.py、决策：先跑门、rc=0")])
    _v2hist = [{"role": "system", "content": "s"}] + [
        {"role": "user", "content": "m%d" % i} for i in range(30)
    ]
    _v2, _d2, _dg2 = _compact_history_v2(_v2hist, keep=5, provider=_dstub.provider())
    case(
        "compact v2：丢前生成摘要进 history（system+摘要+最近 N）",
        _dg2
        and _v2[0]["role"] == "system"
        and _v2[1]["content"].startswith(DIGEST_PREFIX)
        and _v2[-1]["content"] == "m29"
        and len(_v2) == 7
        and _d2 == 25,
        "dg=%s len=%d head=%r" % (_dg2, len(_v2), _v2[1]["content"][:24]),
    )
    _fstub = Stub([{"status": 500, "json": {"error": "boom"}}])
    _v2b, _d3, _dg3 = _compact_history_v2(_v2hist, keep=5, provider=_fstub.provider())
    _fstub.close()
    _oldb, _d4 = _compact_history(_v2hist, keep=5)
    case(
        "compact v2 回退：provider 失败时与旧口径逐字相同",
        _v2b == _oldb and _d3 == _d4 and not _dg3,
        "dg=%s same=%s" % (_dg3, _v2b == _oldb),
    )
    _fctx = {
        "root": tmproot,
        "mode": "plan",
        "history": [{"role": "system", "content": "s"}]
        + [{"role": "user", "content": "m%d" % i} for i in range(30)],
    }
    _repl_slash("/compact 只留部署相关", _fctx, _dstub.provider(), "sid", "p")
    _fp = json.dumps(_dstub.seen[-1], ensure_ascii=False)
    case(
        "/compact 聚焦词进摘要请求（且摘要真落在 history）",
        "只留部署相关" in _fp
        and _fctx["history"][1]["content"].startswith(DIGEST_PREFIX),
        "seen=%d focus=%s" % (len(_dstub.seen), "只留部署相关" in _fp),
    )
    _dstub.close()

    # (W1) /rewind：重放对齐 / 端到端落 note / 越界 fail-closed ---------
    _rw = os.path.join(tmproot, "rw.jsonl")
    for _i in range(1, 4):
        _transcript_append(_rw, {"kind": "user", "text": "u%d" % _i})
        _transcript_append(_rw, {"kind": "assistant", "text": "a%d" % _i})
    _rwr = _transcript_load(_rw)
    _rmsgs, _rerr, _rturns = _rewind_replay(_rwr, 2)
    case(
        "rewind 重放：第 2 轮末 = u1..a2 四条、轮数=3",
        _rerr == ""
        and _rturns == 3
        and _rmsgs
        == [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
        ],
        "err=%r" % _rerr,
    )
    _rctx = {
        "root": tmproot,
        "mode": "plan",
        "history": [{"role": "user", "content": "stale"}],
    }
    _repl_slash("/rewind 2", _rctx, None, "sid", _rw)
    _rr = _transcript_load(_rw)
    case(
        "/rewind 端到端：history 重建 + note 落盘 + 原记录保留",
        _rctx["history"] == _rmsgs
        and len(_rr) == 7
        and _rr[-1].get("kind") == "note"
        and _rr[0]["text"] == "u1",
        "hist=%d recs=%d last=%s"
        % (len(_rctx["history"]), len(_rr), _rr[-1].get("kind")),
    )
    _keep_hist = _rctx["history"]
    _repl_slash("/rewind 0", _rctx, None, "sid", _rw)
    _repl_slash("/rewind 9", _rctx, None, "sid", _rw)
    _repl_slash("/rewind abc", _rctx, None, "sid", _rw)
    _rr2 = _transcript_load(_rw)
    case(
        "rewind 越界/非数 fail-closed：history 不动、无新 note",
        _rctx["history"] is _keep_hist and len(_rr2) == 7,
        "recs=%d" % len(_rr2),
    )

    # (W2) subagent-as-tool：可重入 / 禁嵌套 / 步数封顶 / 子集强制 / 权限继承
    # 桩脚本**序贯共享**：外层与内层走同一个桩，按下标依次取响应。
    _dsub = Stub(
        [
            jc(
                tool=(
                    "subagent",
                    {
                        "prompt": "数一下工具目录里的条目数",
                        "tools": ["list_dir"],
                        "max_steps": 3,
                    },
                )
            ),
            jc(content="子代理结论：目录共 103 项"),
            jc(content="外围结论：已拿到子代理汇报"),
        ]
    )
    _ores = run_agent(
        "先把清单交给子代理查",
        ctx={"root": tmproot, "mode": "accept-edits"},
        provider=_dsub.provider(),
        max_steps=4,
    )
    _tmsg = [m for m in _ores.get("messages") or [] if m.get("role") == "tool"]
    try:
        _sub_rep = json.loads(_tmsg[0]["content"]) if _tmsg else {}
    except ValueError:
        _sub_rep = {}
    case(
        "subagent 可重入：外层停下→内层出终答→外层继续（3 请求）",
        _ores.get("ok")
        and len(_dsub.seen) == 3
        and _sub_rep.get("summary") == "子代理结论：目录共 103 项"
        and _sub_rep.get("steps") == 1
        and _sub_rep.get("rc") == 0,
        "seen=%d summary=%r" % (len(_dsub.seen), str(_sub_rep.get("summary"))[:24]),
    )
    _dsub.close()
    _nstub = Stub([jc(content="不应被调用")])
    _nres, _ = exec_tool(
        "subagent",
        {"prompt": "再来一层"},
        {
            "root": tmproot,
            "mode": "accept-edits",
            "subagent_depth": 1,
            "_provider": _nstub.provider(),
        },
    )
    case(
        "subagent 禁嵌套：depth>0 直接拒且零请求（fail-closed）",
        _nres.get("ok") is False
        and _nres.get("executed") is False
        and "禁止嵌套" in str(_nres.get("error"))
        and not _nstub.seen,
        "err=%r seen=%d" % (str(_nres.get("error"))[:30], len(_nstub.seen)),
    )
    _nstub.close()
    # 夹具补齐（W11.1）：让每步读的是**真存在**的文件 —— 旧夹具 40 个「读不
    # 存在的文件」在新守卫②（同工具连续失败 ≥6 停）下第 6 步就被叫停，测不到
    # 「99 被钳到 12」这条本意。调用仍逐文件变化（不触 MAX_REPEAT/TEXT_REPEAT）。
    for _i in range(40):
        with open(os.path.join(tmproot, "n%d.txt" % _i), "w", encoding="utf-8") as _f:
            _f.write("x")
    _mstub = Stub(
        [jc(tool=("read_file", {"path": "n%d.txt" % _i})) for _i in range(40)]
    )
    _mres, _ = exec_tool(
        "subagent",
        {"prompt": "无限读", "max_steps": 99},
        {"root": tmproot, "mode": "accept-edits", "_provider": _mstub.provider()},
    )
    case(
        "subagent 步数解封：99 不再钳到 12（走满 40 个不同动作才停）",
        _mres.get("limit") is True
        and _mres.get("steps", 0) > SUBAGENT_MAX_STEPS
        and len(_mstub.seen) > SUBAGENT_MAX_STEPS,
        "steps=%s seen=%d" % (_mres.get("steps"), len(_mstub.seen)),
    )
    _mstub.close()
    _why_ss = _authorize(
        TOOLS["write_file"],
        {"path": "sub-x.txt", "content": "x"},
        {"root": tmproot, "mode": "full", "tool_subset": ["read_file"]},
    )
    _r_ss, _ = exec_tool(
        "write_file",
        {"path": "sub-x.txt", "content": "x"},
        {"root": tmproot, "mode": "full", "tool_subset": ["read_file"]},
    )
    case(
        "subagent 工具子集强制：子集外工具拒 + 真跑不落盘",
        isinstance(_why_ss, str)
        and "子集" in _why_ss
        and _r_ss.get("executed") is False
        and not os.path.exists(os.path.join(tmproot, "sub-x.txt")),
        "why=%r ex=%s" % (str(_why_ss)[:30], _r_ss.get("executed")),
    )
    _istub = Stub(
        [
            jc(tool=("write_file", {"path": "sub-plan.txt", "content": "x"}), cid="w1"),
            jc(content="子代理：写被拒了"),
        ]
    )
    _ires, _ = exec_tool(
        "subagent",
        {"prompt": "试着写个文件", "tools": ["write_file"], "max_steps": 4},
        {"root": tmproot, "mode": "plan", "_provider": _istub.provider()},
    )
    case(
        "subagent 权限继承：外层 plan 模式下内层写被拒、文件未落",
        _ires.get("ok") is True
        and len(_istub.seen) == 2
        and not os.path.exists(os.path.join(tmproot, "sub-plan.txt")),
        "ok=%s seen=%d" % (_ires.get("ok"), len(_istub.seen)),
    )
    _istub.close()

    # (W11.1) 打转守卫三例：重复语句提前停 / 同工具连续失败提前停 / 空答诚实化
    _g1 = Stub(
        [
            jc(tool=("read_file", {"path": "n0.txt"}), content="先看一下现状再说")
            for _i in range(5)
        ]
    )
    _r1 = run_agent(
        "干个活",
        ctx={"root": tmproot, "mode": "plan"},
        provider=_g1.provider(),
        max_steps=10,
    )
    _g1.close()
    case(
        "打转守卫①：同一句话连说无新成功动作 → 提前停（不烧预算）",
        _r1.get("kind") == "limit"
        and _r1.get("steps") == 4
        and "同一句话连续重复" in str(_r1.get("error")),
        "steps=%s err=%r" % (_r1.get("steps"), str(_r1.get("error"))[:44]),
    )
    _g2 = Stub(
        [jc(tool=("read_file", {"path": "g-none-%d.txt" % _i})) for _i in range(8)]
    )
    _r2 = run_agent(
        "干个活",
        ctx={"root": tmproot, "mode": "plan"},
        provider=_g2.provider(),
        max_steps=12,
    )
    _g2.close()
    case(
        "打转守卫②：同一工具连续失败 6 次（参数每次不同）→ 提前停",
        _r2.get("kind") == "limit"
        and _r2.get("steps") == 6
        and "连续失败" in str(_r2.get("error")),
        "steps=%s err=%r" % (_r2.get("steps"), str(_r2.get("error"))[:44]),
    )
    _g3 = Stub([jc(content="")])
    _r3 = run_agent(
        "干个活",
        ctx={"root": tmproot, "mode": "plan"},
        provider=_g3.provider(),
        max_steps=4,
    )
    _g3.close()
    case(
        "打转守卫③：空终答诚实化（empty_answer 标记 + 可读错误，不装成功）",
        _r3.get("ok") is True
        and _r3.get("empty_answer") is True
        and bool(_r3.get("error")),
        "ok=%s empty=%s" % (_r3.get("ok"), _r3.get("empty_answer")),
    )

    # (W13) 批准即执行 / 诚实收尾：五例（纯面 + 口径 + 行为）
    case(
        "W13 批准词识别：拍板/同意/开工 命中；不同意/没批准 不误判",
        bool(
            _looks_approved("拍板")
            and _looks_approved("同意执行")
            and _looks_approved("开干吧")
        )
        and not _looks_approved("我不同意")
        and not _looks_approved("没有批准")
        and not _looks_approved("还未同意")
        and not _looks_approved("暂不批准"),
        "正=%s 负=%s" % (_looks_approved("拍板"), _looks_approved("我不同意")),
    )
    case(
        "W13 零证据完成声明：0 工具+'已全面落实'=打脸；有工具=不打脸",
        _claim_no_evidence("已全面落实任务", 0) is True
        and _claim_no_evidence("本轮读了 3 个文件", 3) is False
        and _claim_no_evidence("已部署完成", 0) is True,
        "claims=%s" % _claim_no_evidence("已全面落实任务", 0),
    )
    case(
        "W13 跨轮复读检测：逐字重复=真；不同内容=假",
        _is_stale_reply(
            "**做了什么：** 已全面落实任务，暂无未做内容。"
            "**还剩什么没做：** 未动任何文件。",
            "**做了什么：** 已全面落实任务，暂无未做内容。"
            "**还剩什么没做：** 未动任何文件。",
        )
        is True
        and _is_stale_reply(
            "上一轮的长答复内容……" * 4, "完全不同的新答复内容：" + "新" * 60
        )
        is False,
        "stale_check_ok",
    )
    _sp_dir = os.path.join(ROOT, "tools")
    import importlib.util as _ilu2

    _spc = _ilu2.spec_from_file_location("dsp_w13", os.path.join(_sp_dir, "dsp.py"))
    _sp = _ilu2.module_from_spec(_spc)
    _spc.loader.exec_module(_sp)
    _pl = _sp.new_plan(
        "口径夹具",
        [
            {
                "id": "x",
                "title": "t",
                "accept": "rc=0",
                "tools": ["read_file"],
                "depends": [],
            }
        ],
    )
    case(
        "W13 计划口径随闸走：自由面不说锁死、上锁面才说",
        "写面自由" in _sp.plan_summary(_pl, locked=False)
        and "写工具锁死" not in _sp.plan_summary(_pl, locked=False)
        and "写工具锁死" in _sp.plan_summary(_pl, locked=True)
        and "不要等审批" in _sp.auto_directive(_pl, {}, locked=False)
        and "未审批时写工具锁死" in _sp.auto_directive(_pl, {}, locked=True),
        "free=%r" % _sp.plan_summary(_pl, locked=False)[:44],
    )
    # 行为：批准/空答重试判定（纯函数）+ 工具真驱动夹具
    case(
        "W13 重试判定纯函数：批准轮零工具=重试、有工具/空答勿误触",
        _needs_approve_retry(True, [], False) is True
        and _needs_approve_retry(True, [{"tool": "read_file"}], False) is False
        and _needs_approve_retry(False, [], False) is False
        and _needs_approve_retry(True, [], True) is False,
        "pure_ok",
    )
    _ap = Stub(
        [
            jc(tool=("read_file", {"path": "n0.txt"}), content="已读文件，rc=0"),
            jc(content="读完了 n0.txt：内容为 x。rc=0"),
        ]
    )
    _apval = run_agent(
        "（批准重试夹具）读 n0.txt",
        ctx={"root": tmproot, "mode": "plan"},
        provider=_ap.provider(),
        max_steps=4,
    )
    _ap.close()
    case(
        "W13 工具真驱动夹具：run_agent 层工具调用可被桩真实驱动",
        _apval.get("ok") is True
        and any(u.get("tool") == "read_file" for u in _apval.get("tools_used") or []),
        "ok=%s tools=%d" % (_apval.get("ok"), len(_apval.get("tools_used") or [])),
    )

    # (W3) MCP stdio 客户端：真握手 / 降级如实 / id 错配 / 死服务器 fail-closed
    try:
        if os.path.join(ROOT, "tools") not in sys.path:
            sys.path.insert(0, os.path.join(ROOT, "tools"))
        import mcp_client as _mcpcli
    except ImportError:
        _mcpcli = None
    _fx = os.path.join(ROOT, "tools", "mcp_fixture_srv.py")
    case(
        "mcp_client 可导入 + 纯面（encode/decode 往返 + 名字净化）",
        _mcpcli is not None
        and _mcpcli.decode(_mcpcli.encode({"a": 1}).decode())["a"] == 1
        and _mcpcli.tool_name("srv.a", "echo/b") == "mcp_srv_a_echo_b",
        "mod=%s" % (_mcpcli is not None),
    )
    _gok = False
    _srv_g = None
    if _mcpcli:
        os.environ["AGENTOS_LLM_API_KEY"] = "st-secret-for-test"
        _srv_g = _mcpcli.Server("fx", [sys.executable, _fx, "good"])
        try:
            _srv_g.start()
            _srv_g.initialize()
            _tl = _srv_g.list_tools()
            _cr = _srv_g.call_tool("echo", {"q": 1})
            _gok = (
                _srv_g.handshake == "ok"
                and len(_tl) == 2
                and "echo:" in (_cr.get("result") or "")
                and "key=ABSENT" in (_cr.get("result") or "")
            )
        except Exception:  # noqa: BLE001
            _gok = False
        finally:
            _srv_g.close()
            os.environ.pop("AGENTOS_LLM_API_KEY", None)
    case(
        "MCP 良好路径：握手 ok + list 2 工具 + call 回显 + 密钥不进子进程",
        bool(_gok),
        "handshake=%s" % (getattr(_srv_g, "handshake", "-"),),
    )
    _rok = False
    _srv_r = None
    if _mcpcli:
        _srv_r = _mcpcli.Server("fx", [sys.executable, _fx, "refuse-init"])
        try:
            _srv_r.start()
            _srv_r.initialize()
            _tl2 = _srv_r.list_tools()
            _rok = _srv_r.handshake == "stateless" and len(_tl2) == 2
        except Exception:  # noqa: BLE001
            _rok = False
        finally:
            _srv_r.close()
    case(
        "MCP 降级：initialize 被拒 → stateless 如实记录且仍可用",
        bool(_rok),
        "handshake=%s" % (getattr(_srv_r, "handshake", "-"),),
    )
    _bok = False
    _srv_b = None
    if _mcpcli:
        _srv_b = _mcpcli.Server("fx", [sys.executable, _fx, "bad-id"])
        try:
            _srv_b.start()
            _srv_b.list_tools()
        except _mcpcli.MCPError as e:
            _bok = "错配" in str(e)
        except Exception:  # noqa: BLE001
            _bok = False
        finally:
            _srv_b.close()
    case(
        "MCP id 错配 fail-closed（不把发错的响应当自己的）", bool(_bok), "ok=%s" % _bok
    )
    _dok = False
    _srv_d = None
    if _mcpcli:
        _t0d = time.time()
        _srv_d = _mcpcli.Server("fx", [sys.executable, _fx, "die"])
        try:
            _srv_d.start()
            _srv_d.list_tools()
        except _mcpcli.MCPError as e:
            _dok = (("已退出" in str(e)) or ("EOF" in str(e))) and (
                time.time() - _t0d
            ) < 10
        except Exception:  # noqa: BLE001
            _dok = False
        finally:
            _srv_d.close()
    case("MCP 死服务器 fail-closed（报 EOF/退出且不 hang）", bool(_dok), "ok=%s" % _dok)
    _reg = {}
    if _mcpcli:
        _tcopy = {}
        _rer = _mcpcli.sync_tools(
            _tcopy,
            {
                "servers": {
                    "fxs": {
                        "cmd": [sys.executable, _fx, "good"],
                        "tools": {"set_thing": "write"},
                    }
                }
            },
            risk_read=RISK_READ,
            risk_write=RISK_WRITE,
        )
        _reg = {
            "added": sorted(_rer.get("added") or []),
            "echo": _tcopy.get("mcp_fxs_echo", {}).get("risk"),
            "set": _tcopy.get("mcp_fxs_set_thing", {}).get("risk"),
            "fn": callable(_tcopy.get("mcp_fxs_echo", {}).get("fn")),
        }
    case(
        "MCP 注册：mcp_<server>_<tool> 注入 + 风险逐工具映射（read/write）",
        _reg.get("added") == ["mcp_fxs_echo", "mcp_fxs_set_thing"]
        and _reg.get("echo") == RISK_READ
        and _reg.get("set") == RISK_WRITE
        and _reg.get("fn"),
        "added=%s echo=%s set=%s"
        % (_reg.get("added"), _reg.get("echo"), _reg.get("set")),
    )

    # (W4) hooks 中间件 + skills 目录：拦截 / fail-closed / 预算 / 注入 / glob
    try:
        if os.path.join(ROOT, "tools") not in sys.path:
            sys.path.insert(0, os.path.join(ROOT, "tools"))
        import hooks_mw as _hmw
        import skills_load as _skm
    except ImportError:
        _hmw = None
        _skm = None
    _cfg_blk = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "write_*", "hooks": [{"command": "printf blocked; exit 2"}]}
            ]
        }
    }
    _cfg_ctx = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [{"command": 'printf \'{"additionalContext": "CTX-A"}\''}],
                }
            ]
        }
    }
    _rb = _hmw.run_event("PreToolUse", {"tool": "write_file"}, _cfg_blk) if _hmw else {}
    _rm = (
        _hmw.run_event("PreToolUse", {"tool": "read_file"}, _cfg_blk)
        if _hmw
        else {"ran": 0}
    )
    _rc = _hmw.run_event("PreToolUse", {"tool": "x"}, _cfg_ctx) if _hmw else {}
    case(
        "hooks：exit 2 拦截 / glob 不匹配不跑 / additionalContext 收集",
        _hmw is not None
        and _rb.get("block") is True
        and "blocked" in str(_rb.get("reason"))
        and _rm.get("ran") == 0
        and _rc.get("block") is False
        and _rc.get("context") == ["CTX-A"],
        "blk=%s miss=%s ctx=%s"
        % (_rb.get("block"), _rm.get("ran"), _rc.get("context")),
    )
    _cfg_bad = {
        "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"command": "exit 7"}]}]}
    }
    _cfg_slow = {
        "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"command": "sleep 3"}]}]}
    }
    _rf = _hmw.run_event("PreToolUse", {"tool": "x"}, _cfg_bad) if _hmw else {}
    _rt = (
        _hmw.run_event("PreToolUse", {"tool": "x"}, _cfg_slow, timeout=1.0)
        if _hmw
        else {}
    )
    case(
        "hooks PreToolUse fail-closed：非 0 退出码与超时都拦截",
        _rf.get("block") is True and _rt.get("block") is True,
        "rc7=%s timeout=%s" % (_rf.get("block"), _rt.get("block")),
    )
    _skroot = os.path.join(tmp, "skroot")
    os.makedirs(os.path.join(_skroot, "alpha"), exist_ok=True)
    os.makedirs(os.path.join(_skroot, "beta"), exist_ok=True)
    with open(os.path.join(_skroot, "alpha", "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write("---\nname: alpha\ndescription: 甲技能\n---\nALPHA-BODY\n")
    with open(os.path.join(_skroot, "beta", "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write("---\nname: beta\ndescription: %s\n---\nBETA-BODY\n" % ("长" * 600))
    _its = _skm.discover([_skroot]) if _skm else []
    _sec = _skm.render_section(_its) if _skm else ""
    _bline = [ln for ln in _sec.splitlines() if ln.startswith("- beta:")]
    case(
        "skills：frontmatter 解析 + description 超 500 截断留标记 + 不进正文",
        [i["name"] for i in _its] == ["alpha", "beta"]
        and bool(_bline)
        and _bline[0].endswith("…")
        and len(_bline[0]) <= 500 + len("- beta: ") + 1
        and "ALPHA-BODY" not in _sec,
        "names=%s bline_len=%s"
        % ([i["name"] for i in _its], len(_bline[0]) if _bline else -1),
    )
    _sctx = {"root": tmproot, "mode": "plan", "history": []}
    _SKILL_ROOTS_OVERRIDE.append(_skroot)
    try:
        _repl_slash("/skill alpha", _sctx, None, "sid", "p")
        _ok_inj = (
            bool(_sctx["history"])
            and _sctx["history"][-1]["role"] == "user"
            and "ALPHA-BODY" in _sctx["history"][-1]["content"]
        )
        _repl_slash("/skill 不存在", _sctx, None, "sid", "p")
        _ok_unknown = len(_sctx["history"]) == 1
    finally:
        _SKILL_ROOTS_OVERRIDE.pop()
    case(
        "/skill 注入正文进下一轮上下文；未知名不改历史",
        _ok_inj and _ok_unknown,
        "inj=%s unknown_keep=%s" % (_ok_inj, _ok_unknown),
    )
    _sp4 = build_system_prompt(
        {"root": tmproot, "mode": "plan", "hook_context": ["CTX-Z9"]}
    )
    case(
        "hooks 上下文进 system prompt（SessionStart/Pre 注入路径）",
        "CTX-Z9" in _sp4,
        "len=%d" % len(_sp4),
    )

    # (W5) 终端 UX 四件：续行 / @补全 / ⎿详情 / 终答缩进 ----------------
    case(
        "终端 UX：行尾反斜杠续行（奇=续、偶=字面反斜杠）",
        _line_wants_continue("跑门\\")
        and not _line_wants_continue("a\\\\")
        and not _line_wants_continue("plain")
        and _compose_join("第一段\\", "第二段") == "第一段\n第二段",
        "odd=%s even=%s"
        % (_line_wants_continue("跑门\\"), _line_wants_continue("a\\\\")),
    )
    _uxroot = os.path.join(tmp, "uxroot")
    os.makedirs(os.path.join(_uxroot, "sub"), exist_ok=True)
    with open(os.path.join(_uxroot, "a.txt"), "w", encoding="utf-8") as fh:
        fh.write("a\n")
    with open(os.path.join(_uxroot, "sub", "b.txt"), "w", encoding="utf-8") as fh:
        fh.write("b\n")
    with open(os.path.join(_uxroot, ".hidden"), "w", encoding="utf-8") as fh:
        fh.write("h\n")
    case(
        "@ 路径 Tab 补全：前缀匹配/目录带斜杠/隐藏跳过/非 @ 不问",
        _at_completions("@a", _uxroot) == ["@a.txt"]
        and _at_completions("@sub/", _uxroot) == ["@sub/b.txt"]
        and sorted(_at_completions("@", _uxroot)) == ["@a.txt", "@sub/"]
        and _at_completions("plain", _uxroot) == [],
        "a=%s sub=%s"
        % (_at_completions("@a", _uxroot), _at_completions("@sub/", _uxroot)),
    )
    _ts1 = _tool_summary(
        "edit_file",
        {"path": "x.py", "old_string": "l1\nl2\nl3", "new_string": "l1\nX\nl3"},
        {"ok": True},
    )
    _ts2 = _tool_summary(
        "write_file", {"path": "y.txt", "content": "a\nb"}, {"ok": True}
    )
    case(
        "⎿详情：edit_file 行级 ±对账 + write_file 行/字节数",
        "+1 −1" in _ts1 and "x.py" in _ts1 and "写入 2 行" in _ts2 and "y.txt" in _ts2,
        "edit=%r" % (_ts1[:40],),
    )
    case(
        "⎿详情边界：失败/被拒/非编辑工具一律空（不装饰）",
        _tool_summary(
            "edit_file",
            {"path": "x", "old_string": "a", "new_string": "b"},
            {"ok": False},
        )
        == ""
        and _tool_summary("edit_file", {"path": "x"}, {"ok": True}) == ""
        and _tool_summary("read_file", {"path": "x"}, {"ok": True}) == "",
        "边界三态",
    )
    case(
        "终答渲染：续行缩进两格、单行不变、空答给占位",
        _format_answer("l1\nl2") == "l1\n  l2"
        and _format_answer("single") == "single"
        and _format_answer("") == "（无终答）",
        "fmt=%r" % (_format_answer("l1\nl2"),),
    )
    try:
        with open(os.path.join(tmproot, "note-e.txt"), "w", encoding="utf-8") as fh:
            fh.write("l1\nl2\nl3\n")
        _estub = Stub(
            [
                jc(
                    tool=(
                        "edit_file",
                        {"path": "note-e.txt", "old_string": "l2", "new_string": "XX"},
                    )
                ),
                jc(content="改完了"),
            ]
        )
        _evs = []
        run_agent(
            "把 note-e 的第二行改掉",
            ctx={"root": tmproot, "mode": "full"},
            provider=_estub.provider(),
            max_steps=4,
            on_event=_evs.append,
        )
        _summ = [
            e.get("summary")
            for e in _evs
            if e.get("type") == "tool" and e.get("tool") == "edit_file"
        ]
        _eok = bool(_summ) and "+1 −1" in (_summ[0] or "")
        _estub.close()
    except Exception:  # noqa: BLE001
        _eok = False
    case("⎿详情进 tool 事件（run_agent 真跑 edit_file）", _eok, "ok=%s" % _eok)

    # (W6) 并行子代理批量 + skills 自动触发 ------------------------------
    # 并行性用 barrier 确定性证伪：三条子代理线必须同时到达 —— 串行化会
    # 让 Barrier 超时（BrokenBarrierError），不靠摸时钟。
    _bar = threading.Barrier(3, timeout=8)

    class _BP(object):
        model = "barrier"
        base_url = "stub"
        source = "barrier"
        session_id = "bar"
        protocol = "chat"

        def chat(self, messages, tools=None, stream=False, on_event=None, cancel=None):
            _bar.wait()
            return {
                "content": "批内完成",
                "tool_calls": [],
                "usage": {},
                "finish_reason": "stop",
            }

    try:
        _bres, _ = exec_tool(
            "subagent",
            {"prompts": ["甲任务", "乙任务", "丙任务"], "max_steps": 2},
            {"root": tmproot, "mode": "plan", "_provider": _BP()},
        )
        _brows = _bres.get("results") or []
        case(
            "subagent 并行批次：barrier 三线同到（串行必 BrokenBarrier）",
            _bres.get("ok") is True
            and _bres.get("parallel") is True
            and len(_brows) == 3
            and all(r.get("ok") for r in _brows)
            and [r.get("prompt") for r in _brows] == ["甲任务", "乙任务", "丙任务"],
            "ok=%s n=%d" % (_bres.get("ok"), len(_brows)),
        )
    except Exception as e:  # noqa: BLE001
        case(
            "subagent 并行批次：barrier 三线同到（串行必 BrokenBarrier）",
            False,
            repr(e)[:100],
        )
    _r_lim, _ = exec_tool(
        "subagent",
        {"prompts": ["a"] * 5},
        {"root": tmproot, "mode": "plan", "_provider": _BP()},
    )
    _r_empty, _ = exec_tool(
        "subagent",
        {"prompts": []},
        {"root": tmproot, "mode": "plan", "_provider": _BP()},
    )
    _r_blank, _ = exec_tool(
        "subagent",
        {"prompts": [" ", "x"]},
        {"root": tmproot, "mode": "plan", "_provider": _BP()},
    )
    case(
        "subagent 批量边界：5 个/空表/空白项 均拒（fail-closed）",
        _r_lim.get("executed") is False
        and str(SUBAGENT_PARALLEL_MAX) in str(_r_lim.get("error"))
        and _r_empty.get("executed") is False
        and _r_blank.get("executed") is False,
        "lim=%r" % (str(_r_lim.get("error"))[:44],),
    )

    class _PickyP(object):
        model = "picky"
        base_url = "stub"
        source = "picky"
        session_id = "pick"
        protocol = "chat"

        def chat(self, messages, tools=None, stream=False, on_event=None, cancel=None):
            last = str((messages or [{}])[-1].get("content") or "")
            if "boom" in last:
                raise AgentError("provider", "boom 任务失败")
            return {
                "content": "好:%s" % last[:8],
                "tool_calls": [],
                "usage": {},
                "finish_reason": "stop",
            }

    _fres, _ = exec_tool(
        "subagent",
        {"prompts": ["好一", "boom 掉", "好二"], "max_steps": 2},
        {"root": tmproot, "mode": "plan", "_provider": _PickyP()},
    )
    _fr = _fres.get("results") or []
    case(
        "subagent 批量失败隔离：单任务炸不拖垮其余、按输入序返回",
        _fres.get("ok") is True
        and len(_fr) == 3
        and _fr[0].get("ok")
        and (not _fr[1].get("ok"))
        and _fr[2].get("ok")
        and "失败" in str(_fr[1].get("summary")),
        "oks=%s" % ([r.get("ok") for r in _fr],),
    )
    _s1stub = Stub([jc(content="单发结论")])
    _s1, _ = exec_tool(
        "subagent",
        {"prompt": "单发任务", "max_steps": 2},
        {"root": tmproot, "mode": "plan", "_provider": _s1stub.provider()},
    )
    _s1stub.close()
    case(
        "subagent 单发路径回归：prompt 仍走单人版（无 results 键）",
        _s1.get("ok") is True
        and "results" not in _s1
        and _s1.get("summary") == "单发结论",
        "keys=%s" % (sorted(_s1)[:5],),
    )
    _aroot = os.path.join(tmp, "skauto")
    os.makedirs(os.path.join(_aroot, "fix"), exist_ok=True)
    os.makedirs(os.path.join(_aroot, "no"), exist_ok=True)
    with open(os.path.join(_aroot, "fix", "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write(
            "---\nname: fixit\ndescription: 修布局\ntriggers: "
            "flutter，布局崩\n---\nFIXIT-BODY-步骤一二三\n"
        )
    with open(os.path.join(_aroot, "no", "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write("---\nname: noauto\ndescription: 无触发\n---\nNO-BODY\n")
    _parsed = _skm.parse_skill(os.path.join(_aroot, "fix", "SKILL.md"))
    case(
        "skills triggers 解析：逗号/中文逗号切分、缺省空表",
        _parsed.get("triggers") == ["flutter", "布局崩"]
        and _skm.parse_skill(os.path.join(_aroot, "no", "SKILL.md")).get("triggers")
        == [],
        "trs=%s" % (_parsed.get("triggers"),),
    )
    _sth = Stub([jc(content="收到")])
    _SKILLS_ITEMS.clear()
    _SKILL_ROOTS_OVERRIDE.append(_aroot)
    try:
        run_agent(
            "帮我看看 flutter 的布局崩问题",
            ctx={"root": tmproot, "mode": "plan"},
            provider=_sth.provider(),
            max_steps=2,
        )
        _seen0 = json.dumps(_sth.seen[0], ensure_ascii=False)
        run_agent(
            "一句无关的话",
            ctx={"root": tmproot, "mode": "plan"},
            provider=_sth.provider(),
            max_steps=2,
        )
        _seen1 = json.dumps(_sth.seen[1], ensure_ascii=False)
    finally:
        _SKILL_ROOTS_OVERRIDE.pop()
        _SKILLS_ITEMS.clear()
        _sth.close()
    case(
        "skills 自动触发：triggers 命中注入正文 / 未命中不注入",
        "FIXIT-BODY" in _seen0
        and "NO-BODY" not in _seen0
        and "FIXIT-BODY" not in _seen1,
        "hit=%s miss=%s" % ("FIXIT-BODY" in _seen0, "FIXIT-BODY" in _seen1),
    )

    # (W7) 会话分支 /branch + 计划自验收 /verify ---------------------
    try:
        if os.path.join(ROOT, "tools") not in sys.path:
            sys.path.insert(0, os.path.join(ROOT, "tools"))
        import branches as _bm7
    except ImportError:
        _bm7 = None
    _bs = os.path.join(tmp, "bsrc")
    os.makedirs(_bs, exist_ok=True)
    _bsrc = os.path.join(_bs, "agent-20261013-0001-ab12.jsonl")
    with open(_bsrc, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "user", "text": "u1"}) + "\n")
        fh.write(json.dumps({"kind": "assistant", "text": "a1"}) + "\n")
        fh.write(json.dumps({"kind": "user", "text": "u2"}) + "\n")
    case(
        "branches 可导入 + fork 原语",
        _bm7 is not None
        and hasattr(_bm7, "fork_transcript")
        and hasattr(_bm7, "verify_plan"),
        "mod=%s" % (_bm7 is not None),
    )
    _bok, _berr = (False, "?")
    _bdst = ""
    if _bm7:
        _bdst = os.path.join(tmp, "bdst")
        _bok, _berr = _bm7.fork_transcript(
            _bsrc, _bdst, "agent-20261013-0002-cd34", "试分支"
        )
    _brows = (
        _transcript_load(os.path.join(_bdst, "agent-20261013-0002-cd34.jsonl"))
        if _bok
        else []
    )
    case(
        "/branch 原语：原文件不动 + 新文件多叉注记 + 轮数对",
        bool(_bok)
        and len(_brows) == 4
        and _brows[-1].get("kind") == "note"
        and "fork 自 agent-20261013-0001-ab12" in _brows[-1].get("text", "")
        and open(_bsrc, encoding="utf-8").read().count("\n") == 3,
        "ok=%s rows=%d" % (_bok, len(_brows)),
    )
    _vt = [
        {
            "id": "t1",
            "title": "t",
            "tools": ["read_file"],
            "depends": [],
            "status": "done",
            "accept": "rc=0",
            "evidence": ["pytest rc=0 全绿"],
        },
        {
            "id": "t2",
            "title": "t",
            "tools": ["read_file"],
            "depends": [],
            "status": "todo",
            "accept": "rc=0",
            "evidence": [],
        },
        {
            "id": "t3",
            "title": "t",
            "tools": ["read_file"],
            "depends": [],
            "status": "done",
            "accept": "rc=0",
            "evidence": [],
        },
        {
            "id": "t4",
            "title": "t",
            "tools": ["read_file"],
            "depends": [],
            "status": "done",
            "accept": "rc=0",
            "evidence": ["做了一些事"],
        },
        {
            "id": "t5",
            "title": "t",
            "tools": ["read_file"],
            "depends": [],
            "status": "done",
            "accept": "",
            "evidence": ["rc=0"],
        },
    ]
    _vr = _bm7.verify_plan({"tasks": _vt}) if _bm7 else {"verdicts": []}
    _vm = {x["id"]: x["verdict"] for x in _vr["verdicts"]}
    case(
        "/verify 五态：真完成/未做/证据缺/证据对不上/无 accept 判据",
        _vm
        == {
            "t1": "done",
            "t2": "pending",
            "t3": "evidence-missing",
            "t4": "evidence-mismatch",
            "t5": "evidence-mismatch",
        }
        and _vr["n_done_true"] == 1
        and _vr["n_total"] == 5,
        "m=%s" % (_vm,),
    )
    _vsid = "st-verify-%d" % os.getpid()
    # 先清陈旧 st-verify-*（此前失败的 --check 可能死在清理之前留下孤儿；
    # dsp_gate D12 的「work/plans 无自检残留」判据会因此红——自愈比追责好）。
    _plans_dir = os.path.join(ROOT, "work", "plans")
    try:
        for _stale in os.listdir(_plans_dir):
            if _stale.startswith(("st-verify-", "st-watch-", "st-safety-")):
                try:
                    os.unlink(os.path.join(_plans_dir, _stale))
                except OSError:
                    pass
    except OSError:
        pass
    _dsp7 = _dsp()
    _p_ok, _ = _dsp7.save_plan(
        _dsp7.new_plan(
            "分支验收",
            [
                {
                    "id": "v1",
                    "title": "读文件",
                    "tools": ["read_file"],
                    "depends": [],
                    "accept": "rc=0",
                }
            ],
        ),
        _vsid,
    )
    _repl_slash(
        "/verify",
        {"root": tmproot, "mode": "plan", "sid": _vsid, "history": []},
        None,
        _vsid,
        os.path.join(tmp, "verify.jsonl"),
    )
    _pl_v, _ = _dsp7.load_plan(_vsid)
    case(
        "/verify 端到端：未做任务如实 pending（不提前 done）",
        bool(_p_ok) and _pl_v is not None and _pl_v["tasks"][0]["status"] != "done",
        "status=%s" % (_pl_v["tasks"][0]["status"] if _pl_v else "?"),
    )
    _dsp7.approve_plan(_vsid)
    with open(os.path.join(tmproot, "verify-probe.txt"), "w", encoding="utf-8") as fh:
        fh.write("probe\n")
    _wrun, _ = exec_tool(
        "read_file",
        {"path": "verify-probe.txt"},
        {"root": tmproot, "mode": "accept-edits", "sid": _vsid, "dsp_task": "v1"},
    )
    _pl_w, _ = _dsp7.load_plan(_vsid)
    case(
        "/verify 不提前 done：白名单工具跑了也不自动 done",
        _pl_w is not None
        and _pl_w["tasks"][0]["status"] != "done"
        and bool(_wrun.get("ok")),
        "status=%s" % (_pl_w["tasks"][0]["status"] if _pl_w else "?"),
    )
    for _suf in ("-plan.json", "-chain.jsonl"):
        try:
            os.unlink(os.path.join(ROOT, "work", "plans", _vsid + _suf))
        except OSError:
            pass
    _bx = {"root": tmproot, "mode": "plan", "history": []}
    _repl_slash("/branch", _bx, None, "nosid", os.path.join(tmp, "nosid.jsonl"))
    case("/branch 缺会话记录：fail-closed 不崩", True, "rc-None=继续")

    # (W8) 监督通道 steer + 监督面 watch + 多智能体 chain/merge --------
    try:
        if os.path.join(ROOT, "tools") not in sys.path:
            sys.path.insert(0, os.path.join(ROOT, "tools"))
        import steer as _srm
        import watch as _wtm
    except ImportError:
        _srm = None
        _wtm = None
    _sbox = os.path.join(tmp, "steerbox")
    case(
        "steer 可导入 + 纯面（往返/游标只进）",
        _srm is not None
        and _srm.push(_sbox, "s1", "先看 readme")[0]
        and _srm.push(_sbox, "s1", "别动 tools/")[0]
        and len(_srm.pending(_sbox, "s1")[0]) == 2
        and _srm.commit(_sbox, "s1", 1)[0]
        and [x["text"] for x in _srm.pending(_sbox, "s1")[0]] == ["别动 tools/"]
        and _srm.commit(_sbox, "s1", 0)[0]
        and len(_srm.pending(_sbox, "s1")[0]) == 1,
        "往返+只进",
    )
    _sstub = Stub([jc(content="收到")])
    _STEER_DIR_OVERRIDE.append(_sbox)
    try:
        _srm.push(_sbox, "st-steer-x", "把范围缩小到 README")
        _srun = run_agent(
            "干点活",
            ctx={"root": tmproot, "mode": "plan", "sid": "st-steer-x"},
            provider=_sstub.provider(),
            max_steps=2,
        )
        _seen_steer = json.dumps(_sstub.seen[0], ensure_ascii=False)
        run_agent(
            "再干点活",
            ctx={"root": tmproot, "mode": "plan", "sid": "st-steer-x"},
            provider=_sstub.provider(),
            max_steps=2,
        )
        _seen_steer2 = json.dumps(_sstub.seen[1], ensure_ascii=False)
    finally:
        _STEER_DIR_OVERRIDE.pop()
        _sstub.close()
    case(
        "steer 注入：指令进本轮消息（下一步骤边界）",
        bool(_srun.get("ok"))
        and "老板指令" in _seen_steer
        and "把范围缩小到 README" in _seen_steer,
        "seen=%s" % ("老板指令" in _seen_steer),
    )
    case(
        "steer 幂等：已消费的指令不再进第二轮",
        "把范围缩小到 README" not in _seen_steer2,
        "again=%s" % ("把范围缩小到 README" in _seen_steer2),
    )
    _wsid = "st-watch-%d" % os.getpid()
    _dsp8 = _dsp()
    _dsp8.save_plan(
        _dsp8.new_plan(
            "监督面夹具",
            [
                {
                    "id": "w1",
                    "title": "第一步",
                    "tools": ["read_file"],
                    "depends": [],
                    "accept": "rc=0",
                }
            ],
        ),
        _wsid,
    )
    _wdir = os.path.join(tmp, "work", "agent_sessions")
    os.makedirs(_wdir, exist_ok=True)
    with open(os.path.join(_wdir, "st-watch-x.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(
            json.dumps({"ts": 1.0, "kind": "user", "text": "看一下 README"}) + "\n"
        )
        fh.write(json.dumps({"ts": 2.0, "kind": "assistant", "text": "好的"}) + "\n")
    _wrep_plan = _wtm.render(_wtm.snapshot(ROOT, _wsid)) if _wtm else ""
    _wrep_tail = _wtm.render(_wtm.snapshot(tmp, "st-watch-x")) if _wtm else ""
    for _suf in ("-plan.json", "-chain.jsonl"):
        try:
            os.unlink(os.path.join(ROOT, "work", "plans", _wsid + _suf))
        except OSError:
            pass
    case(
        "watch 监督面：计划任务进屏 + 会话尾进屏 + 无计划如实说",
        "监督面夹具" in _wrep_plan
        and "w1" in _wrep_plan
        and "看一下 README" in _wrep_tail
        and "计划：无" in _wrep_tail,
        "plan=%s tail=%s" % ("监督面夹具" in _wrep_plan, "看一下 README" in _wrep_tail),
    )
    _chstub = Stub([jc(content="甲结论：先做 A"), jc(content="乙结论：A 已带过")])
    _chres, _ = exec_tool(
        "subagent",
        {"chain": ["任务甲", "任务乙"], "max_steps": 2},
        {"root": tmproot, "mode": "plan", "_provider": _chstub.provider()},
    )
    _chseen2 = json.dumps(_chstub.seen[1], ensure_ascii=False)
    _chstub.close()
    case(
        "subagent chain：上一段结论自动带进下一段（交接）",
        _chres.get("ok") is True
        and _chres.get("chain") is True
        and len(_chres.get("results") or []) == 2
        and "上一阶段结论" in _chseen2
        and "甲结论：先做 A" in _chseen2,
        "ok=%s handoff=%s" % (_chres.get("ok"), "上一阶段结论" in _chseen2),
    )
    _mgstub = Stub(
        [jc(content="子一"), jc(content="子二"), jc(content="汇总裁决：两路一致")]
    )
    _mgres, _ = exec_tool(
        "subagent",
        {"prompts": ["p1", "p2"], "merge": True, "max_steps": 2},
        {"root": tmproot, "mode": "plan", "_provider": _mgstub.provider()},
    )
    _mgseen3 = json.dumps(_mgstub.seen[2], ensure_ascii=False)
    _mgn = len(_mgstub.seen)
    _mgstub.close()
    case(
        "subagent merge：批量后额外一次汇总出 digest（含全部结论）",
        _mgres.get("ok") is True
        and _mgres.get("digest") == "汇总裁决：两路一致"
        and "子一" in _mgseen3
        and "子二" in _mgseen3
        and _mgn == 3,
        "digest=%r n=%d" % (str(_mgres.get("digest"))[:16], _mgn),
    )

    # (W10) 自由面（user-centric defaults）：默认不摆闸；显式 true 才上锁 ----
    _sf = _safety_flags({})
    _sf2 = _safety_flags(
        {"agent": {"plan_lock": True, "mode_gate": True, "cmd_gate": True}}
    )
    _sf3 = _safety_flags(
        {"agent": {"plan_lock": "true", "mode_gate": 1, "cmd_gate": "yes"}}
    )
    case(
        "safety_flags：缺键=自由（默认）；字面 True 才上锁；字符串不认",
        _sf == {"plan_lock_off": True, "mode_gate_off": True, "cmd_gate_off": True}
        and _sf2
        == {"plan_lock_off": False, "mode_gate_off": False, "cmd_gate_off": False}
        and _sf3 == _sf,
        "free=%s lock=%s str=%s" % (_sf, _sf2, _sf3),
    )
    _ssid = "st-safety-%d" % os.getpid()
    _dsp9 = _dsp()
    _dsp9.save_plan(
        _dsp9.new_plan(
            "自由面夹具",
            [
                {
                    "id": "s1",
                    "title": "写",
                    "tools": ["read_file"],
                    "depends": [],
                    "accept": "rc=0",
                }
            ],
        ),
        _ssid,
    )
    _wargs = {"path": "sa.txt", "content": "x"}
    _w_gate = {"root": tmproot, "mode": "full", "sid": _ssid, "plan_lock_off": False}
    _w_locked = _authorize(TOOLS["write_file"], _wargs, _w_gate)
    _w_free = _authorize(TOOLS["write_file"], _wargs, dict(_w_gate, plan_lock_off=True))
    case(
        "计划锁：显式上锁未审批必拦；自由面放行",
        isinstance(_w_locked, str) and "未审批" in _w_locked and _w_free is None,
        "locked=%r free=%s" % (str(_w_locked)[:24], _w_free is None),
    )
    _wc_gate = _authorize(
        TOOLS["run_cmd"],
        {"argv": ["python3", "-c", "pass"]},
        {
            "root": tmproot,
            "mode": "plan",
            "cmd_gate_off": False,
            "mode_gate_off": False,
        },
    )
    _wc_free = _authorize(
        TOOLS["run_cmd"],
        {"argv": ["python3", "-c", "pass"]},
        {"root": tmproot, "mode": "plan", "cmd_gate_off": True, "mode_gate_off": True},
    )
    _hd = _authorize(
        TOOLS["run_cmd"],
        {"argv": ["git", "push", "origin", "main"]},
        {"root": tmproot, "mode": "full", "cmd_gate_off": True, "mode_gate_off": True},
    )
    case(
        "模式开关：显式 mode_gate 下 plan 拒写命令；自由面放行；硬阻断不可关",
        isinstance(_wc_gate, str) and _wc_free is None and isinstance(_hd, str),
        "gate=%r free=%s hard=%s"
        % (str(_wc_gate)[:20], _wc_free is None, isinstance(_hd, str)),
    )
    _hd_shell = _hard_deny(["bash", "-c", "rm -rf /tmp/x && echo done"])
    _hd_ok = _hard_deny(["bash", "-c", "ls -la"])
    _hd_path = _hard_deny(["/bin/rm", "-rf", "x"])
    _hd_env = _hard_deny(["env", "FOO=1", "git", "push"])
    case(
        "硬阻断形态：路径 / shell -c 载荷 / env 前缀都拦；正常载荷放行",
        isinstance(_hd_shell, str)
        and "载荷" in _hd_shell
        and _hd_ok is None
        and isinstance(_hd_path, str)
        and isinstance(_hd_env, str),
        "shell=%r ok=%s path=%s env=%s"
        % (
            str(_hd_shell)[:18],
            _hd_ok is None,
            isinstance(_hd_path, str),
            isinstance(_hd_env, str),
        ),
    )
    _mc_free = _authorize(
        TOOLS["run_cmd"],
        {"argv": ["mkdir", "probe"]},
        {"root": tmproot, "cmd_gate_off": True, "mode_gate_off": True},
    )
    _mc_gate = _authorize(
        TOOLS["run_cmd"],
        {"argv": ["mkdir", "probe"]},
        {"root": tmproot, "cmd_gate_off": False},
    )
    _mres, _ = exec_tool(
        "run_cmd",
        {"argv": ["mkdir", "sa-dir"]},
        {"root": tmproot, "cmd_gate_off": True, "mode_gate_off": True},
    )
    case(
        "命令面：自由面放行非白名单命令并真跑（mkdir 落盘）；显式 cmd_gate 恢复白名单",
        _mc_free is None
        and isinstance(_mc_gate, str)
        and "白名单" in _mc_gate
        and _mres.get("ok") is True
        and _mres.get("rc") == 0
        and os.path.isdir(os.path.join(tmproot, "sa-dir")),
        "free=%s gate=%r rc=%s"
        % (_mc_free is None, str(_mc_gate)[:16], _mres.get("rc")),
    )
    _dsp9.approve_plan(_ssid)
    # 注：生产路径里写工具在 RISK_WRITE 分支早返回，走到白名单块的是**读面
    # 工具**（list_dir 等）——用例按真实路径测（list_dir 不在 s1 白名单）。
    _wl_gate = _authorize(
        TOOLS["list_dir"],
        {"path": "."},
        {
            "root": tmproot,
            "mode": "full",
            "sid": _ssid,
            "dsp_task": "s1",
            "plan_lock_off": False,
        },
    )
    _wl_free = _authorize(
        TOOLS["list_dir"],
        {"path": "."},
        {
            "root": tmproot,
            "mode": "full",
            "sid": _ssid,
            "dsp_task": "s1",
            "plan_lock_off": True,
        },
    )
    for _suf in ("-plan.json", "-chain.jsonl"):
        try:
            os.unlink(os.path.join(ROOT, "work", "plans", _ssid + _suf))
        except OSError:
            pass
    case(
        "任务白名单随计划锁：显式锁拦；自由面同跳",
        isinstance(_wl_gate, str) and _wl_free is None,
        "wl=%r free=%s" % (str(_wl_gate)[:24], _wl_free is None),
    )

    def _mk_wt(path):
        return {
            "json": {
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "w10",
                                    "type": "function",
                                    "function": {
                                        "name": "write_file",
                                        "arguments": json.dumps(
                                            {"path": path, "content": "w10"}
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ]
            }
        }

    for _free9, _fname in ((True, "sa-off.txt"), (False, "sa-on.txt")):
        _st9 = Stub([_mk_wt(_fname), jc(content="done")])
        _ctx9 = {"root": tmproot, "mode": "plan", "_provider": _st9.provider()}
        _ctx9["mode_gate_off"] = _free9
        _ctx9["plan_lock_off"] = _free9
        exec_tool("subagent", {"prompt": "写个文件", "max_steps": 3}, _ctx9)
        _st9.close()
    case(
        "子代理继承安全闸：自由面内层 plan 写放行；显式锁下拒",
        os.path.exists(os.path.join(tmproot, "sa-off.txt"))
        and not os.path.exists(os.path.join(tmproot, "sa-on.txt")),
        "free_exists=%s gated_exists=%s"
        % (
            os.path.exists(os.path.join(tmproot, "sa-off.txt")),
            os.path.exists(os.path.join(tmproot, "sa-on.txt")),
        ),
    )

    def _mk_rc(dname):
        return {
            "json": {
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "w10r",
                                    "type": "function",
                                    "function": {
                                        "name": "run_cmd",
                                        "arguments": json.dumps(
                                            {"argv": ["mkdir", dname]}
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ]
            }
        }

    for _free10, _dname in ((True, "sub-free-dir"), (False, "sub-gate-dir")):
        _st10 = Stub([_mk_rc(_dname), jc(content="done")])
        _ctx10 = {"root": tmproot, "mode": "full", "_provider": _st10.provider()}
        _ctx10["cmd_gate_off"] = _free10
        _ctx10["mode_gate_off"] = _free10
        _ctx10["plan_lock_off"] = _free10
        exec_tool("subagent", {"prompt": "建个目录", "max_steps": 3}, _ctx10)
        _st10.close()
    case(
        "子代理继承命令面：自由面内层 mkdir 真跑；显式 cmd_gate 下拒",
        os.path.isdir(os.path.join(tmproot, "sub-free-dir"))
        and not os.path.isdir(os.path.join(tmproot, "sub-gate-dir")),
        "free=%s gated=%s"
        % (
            os.path.isdir(os.path.join(tmproot, "sub-free-dir")),
            os.path.isdir(os.path.join(tmproot, "sub-gate-dir")),
        ),
    )

    fpath = os.path.join(tmproot, "ref.txt")
    with open(fpath, "w", encoding="utf-8") as f:
        f.write("REF-BODY\n")
    exp, used = _expand_at_refs("看 @ref.txt 再说 @../outside.txt", tmproot)
    case(
        "@path 展开真实文件、越界的不展开",
        used == ["ref.txt"] and "REF-BODY" in exp and "@../outside.txt" in exp,
        "used=%r" % (used,),
    )

    u = _usage_total(
        {"prompt_tokens": 10}, {"prompt_tokens": 5, "completion_tokens": 7}
    )
    u2 = _usage_total(u, None)
    case(
        "_usage_total 累加数字、忽略空/非数字",
        u.get("prompt_tokens") == 15 and u.get("completion_tokens") == 7 and u2 == u,
        "u=%r" % (u,),
    )

    cctx = {
        "root": tmproot,
        "mode": "plan",
        "history": [{"role": "user", "content": "x"}],
    }
    _repl_slash("/clear", cctx, None, "sid", "p")
    cleared = cctx["history"] == []
    _repl_slash("/mode bogus", cctx, None, "sid", "p")
    mode_kept = cctx["mode"] == "plan"
    _repl_slash("/mode full", cctx, None, "sid", "p")
    mode_sw = cctx["mode"] == "full"
    rc_unknown = _repl_slash("/nope", cctx, None, "sid", "p")
    case(
        "/clear 清空、/mode 只认三档、未知命令不抛且不退出",
        cleared and mode_kept and mode_sw and rc_unknown is None,
        "cleared=%s kept=%s sw=%s unknown=%r"
        % (cleared, mode_kept, mode_sw, rc_unknown),
    )

    # (EVO) 自进化：信号→规则→注入→回退（纯本地，账在 tmp 自检目录）------
    evo_sig = os.path.join(tmp, "signals.jsonl")
    evo_rules = os.path.join(tmp, "rules.json")
    with open(evo_sig, "w", encoding="utf-8") as f:
        for _ in range(3):
            f.write(
                json.dumps({"kind": "turn", "ok": False, "err_kind": "no-provider"})
                + "\n"
            )
        f.write(json.dumps({"kind": "turn", "ok": True}) + "\n")
        for _ in range(2):
            f.write(
                json.dumps({"kind": "turn", "ok": False, "err_kind": "timeout"}) + "\n"
            )
    rules_e, evo_err = _evo_mine(rules_path=evo_rules, signals_path=evo_sig)
    evo_r1 = [r for r in rules_e if r.get("sig") == "err:no-provider"]
    case(
        "evo 挖掘：3 连同签名失败→CANDIDATE(count=3)，2 次不成规则",
        evo_err == ""
        and len(evo_r1) == 1
        and evo_r1[0]["state"] == "CANDIDATE"
        and evo_r1[0]["count"] == 3
        and all(r.get("sig") != "err:timeout" for r in rules_e),
        "rules=%r err=%r"
        % ([(r.get("id"), r.get("state"), r.get("count")) for r in rules_e], evo_err),
    )
    _evo_set_state("evo-err-no-provider", "CANDIDATE", rules_path=evo_rules)
    ok_ap = _evo_set_state("evo-err-no-provider", "ACTIVE", rules_path=evo_rules)
    ptext = build_system_prompt(
        {"root": tmp, "mode": "plan", "evo_rules_path": evo_rules}
    )
    case(
        "evo 注入：ACTIVE 规则真进 system prompt",
        ok_ap and "自进化经验规则" in ptext and "config set llm.api_key" in ptext,
        "applied=%s has_section=%s" % (ok_ap, "自进化经验规则" in ptext),
    )
    _evo_set_state("evo-err-no-provider", "CANDIDATE", rules_path=evo_rules)
    ptext2 = build_system_prompt(
        {"root": tmp, "mode": "plan", "evo_rules_path": evo_rules}
    )
    case("evo 回退：降回 CANDIDATE 后注入消失", "自进化经验规则" not in ptext2)
    with open(evo_rules, "w", encoding="utf-8") as f:
        f.write("{corrupt")
    rules_c, err_c = _evo_rules_load(evo_rules)
    case(
        "evo 账坏 fail-closed：规则读空不炸且状态如实报",
        rules_c == [] and err_c != "",
        "n=%d err=%r" % (len(rules_c), (err_c or "")[:40]),
    )
    rules_c2, err_c2 = _evo_mine(rules_path=evo_rules, signals_path=evo_sig)
    case(
        "evo 账坏时挖掘拒绝动账（防把坏账洗成假新账）",
        rules_c2 == [] and err_c2 != "" and not os.path.exists(evo_rules + ".tmp"),
        "n=%d err=%r" % (len(rules_c2), (err_c2 or "")[:40]),
    )

    # (IDEAS) 自主力：机会挖掘带 file:line 证据 -------------------------
    ideas_root = os.path.join(tmp, "repo")
    os.makedirs(os.path.join(ideas_root, "tools"), exist_ok=True)
    with open(os.path.join(ideas_root, "tools", "a.py"), "w", encoding="utf-8") as f:
        f.write("x = 1  # TODO fix me\n")
    marks_i, gaps_i = _ideas_scan(ideas_root)
    case(
        "ideas 挖掘：TODO 带 file:line、无引用源文件报候选缺口",
        any("tools/a.py:1" in m and "TODO" in m for m in marks_i)
        and any("a.py" in g for g in gaps_i),
        "marks=%r gaps=%r" % (marks_i[:2], gaps_i[:2]),
    )

    # (UI) 过程可见性：事件序列 + 转轮编排 ------------------------------
    stub_u = Stub([_resp_json(None, cid="c7"), _resp_json("DONE-UI", cid=None)])
    evs_u = []
    try:
        res_u = run_agent(
            "读 note.txt",
            ctx={"root": tmproot, "mode": "full"},
            provider=stub_u.provider(protocol="responses"),
            max_steps=4,
            on_event=evs_u.append,
        )
    finally:
        stub_u.close()
    types_u = [e.get("type") for e in evs_u]
    case(
        "过程事件：step 先于 tool_call 先于 tool 结果，args 随行",
        bool(res_u.get("ok"))
        and "tool_call" in types_u
        and "tool" in types_u
        and types_u.index("step") < types_u.index("tool_call")
        and types_u.index("tool_call") < types_u.index("tool")
        and any(e.get("type") == "tool_call" and e.get("args") for e in evs_u),
        "types=%r" % types_u,
    )
    import contextlib
    import io as _io

    buf_u = _io.StringIO()
    seen_u = {"delta": False}
    printed_u = []
    sp_u = _TurnSpinner(True, _c)  # 不 start()：无转轮线程，纯编排验证
    with contextlib.redirect_stdout(buf_u):
        ui = _turn_ui(sp_u, lambda ev: printed_u.append(ev.get("type")), seen_u)
        ui({"type": "step", "step": 1})
        ui({"type": "tool_call", "tool": "read_file", "args": {"path": "x"}})
        ui({"type": "tool", "tool": "read_file", "ok": True, "executed": True})
        ui({"type": "delta", "text": "你好"})
        ui({"type": "delta", "text": "呀"})
    case(
        "过程编排：⏺ 两次（工具行+首增量）、delta 不经 printer、seen 置位",
        seen_u["delta"] is True
        and printed_u == ["step", "tool"]
        and buf_u.getvalue().count("⏺") == 2
        and "read_file" in buf_u.getvalue()
        and "你好呀" in buf_u.getvalue(),
        "printed=%r out=%r" % (printed_u, buf_u.getvalue()[:120]),
    )

    # (MEM) 记忆层级：注入两态 + 截断（蒸馏自 gemini-cli/codex）----------
    mem_root = os.path.join(tmp, "memrepo")
    os.makedirs(mem_root, exist_ok=True)
    ptext3 = build_system_prompt({"root": mem_root, "mode": "plan"})
    case("记忆层级：无 AGENTS.md 零注入（不造假）", "项目记忆" not in ptext3)
    with open(os.path.join(mem_root, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write("MEMMARK 本仓一律 rc 为据。" + "x" * 5000)
    ptext4 = build_system_prompt({"root": mem_root, "mode": "plan"})
    _base_len = len(build_system_prompt({"root": tmp, "mode": "plan"}))
    case(
        "记忆层级：AGENTS.md 注入且超 4KB 截断封顶",
        "MEMMARK" in ptext4
        and "项目记忆" in ptext4
        and "（截断，单文件上限 4KB）" in ptext4
        and len(ptext4) - _base_len < 4096 + 600,
        "len_delta=%d" % (len(ptext4) - _base_len),
    )
    # (GH) GitHub 一等能力：工具注册 + argv 全落只读白名单 + 纪律注入 ----
    gh_tools_ok = all(t in TOOLS for t in ("gh_search", "gh_repo_view", "gh_file_read"))
    a1 = _gh_search_argv("terminal agent", "repos", 10)
    a2 = _gh_file_read_argv("o/r", "src/x.py", "main")
    _ghc = _gh_client_mod()
    case(
        "gh 三只读工具注册且 argv 全落只读白名单",
        gh_tools_ok
        and _ghc.is_readonly(a1)
        and _ghc.is_readonly(a2)
        and a1[0] == "search"
        and a2[0] == "api",
        "tools=%s a1=%s a2=%s" % (gh_tools_ok, a1, a2),
    )
    ptext5 = build_system_prompt({"root": tmproot, "mode": "plan"})
    case(
        "GitHub 优先纪律注入 system prompt",
        "参考 GitHub 成熟实现" in ptext5 and "gh_search" in ptext5,
        "has=%s" % ("参考 GitHub 成熟实现" in ptext5),
    )

    # (EVO2) 自进化 v2：tool 签名/描述改写/提案/midflight 节流 -------------
    evo_sig2 = os.path.join(tmp, "signals2.jsonl")
    evo_rules2 = os.path.join(tmp, "rules2.json")
    with open(evo_sig2, "w", encoding="utf-8") as f:
        for _ in range(3):
            f.write(
                json.dumps({"kind": "turn", "ok": True, "tools_fail": ["read_file"]})
                + "\n"
            )
        for _ in range(3):
            f.write(
                json.dumps({"kind": "turn", "ok": False, "err_kind": "interrupted"})
                + "\n"
            )
    rules_v2, err_v2 = _evo_mine(rules_path=evo_rules2, signals_path=evo_sig2)
    rule_tf = [r for r in rules_v2 if r.get("id") == "evo-tool-fail-read_file"]
    case(
        "evo v2：tool-fail 按工具聚签名、interrupted 不进失败规则",
        err_v2 == ""
        and len(rule_tf) == 1
        and rule_tf[0]["state"] == "CANDIDATE"
        and rule_tf[0]["count"] == 3
        and all(r.get("sig") != "err:interrupted" for r in rules_v2),
        "rules=%r err=%r" % ([(r.get("id"), r.get("count")) for r in rules_v2], err_v2),
    )
    _evo_set_state("evo-tool-fail-read_file", "ACTIVE", rules_path=evo_rules2)
    desc_before = TOOLS["read_file"]["description"]
    n_hint = _evo_refresh_tool_hints(rules_path=evo_rules2)
    hinted = TOOLS["read_file"]["description"]
    _evo_set_state("evo-tool-fail-read_file", "CANDIDATE", rules_path=evo_rules2)
    _evo_refresh_tool_hints(rules_path=evo_rules2)
    restored = TOOLS["read_file"]["description"]
    case(
        "evo v2：tool 类 ACTIVE 规则运行时改写工具描述，off 后还原",
        n_hint >= 1
        and "自进化提示" in hinted
        and restored == desc_before
        and hinted != desc_before,
        "hinted=%r restored_ok=%s" % (hinted[:60], restored == desc_before),
    )
    for _ in range(3):
        with open(evo_sig2, "a", encoding="utf-8") as f:
            f.write(
                json.dumps({"kind": "turn", "ok": True, "tools_fail": ["read_file"]})
                + "\n"
            )
    new_mf = _evo_midflight(rules_path=evo_rules2, signals_path=evo_sig2)
    new_mf2 = _evo_midflight(rules_path=evo_rules2, signals_path=evo_sig2)
    prop_dir = os.path.join(os.path.dirname(evo_rules2), "evo_proposals")
    has_prop = os.path.exists(os.path.join(prop_dir, "evo-tool-fail-read_file.md"))
    case(
        "evo v2：midflight 新候选提示一次（节流）+ 2 倍阈值自动生成提案",
        len(new_mf) >= 1 and len(new_mf2) == 0 and has_prop,
        "new=%r new2=%r prop=%s"
        % ([r.get("id") for r in new_mf], [r.get("id") for r in new_mf2], has_prop),
    )
    ghc2 = _gh_client_mod()
    ok_auto, r_auto = ghc2.write_policy(False, False, "true", "o/r", [], auto=True)
    deny_cfg, r_cfg = ghc2.write_policy(False, False, "yes", "o/r", [], auto=True)
    deny_rl, r_rl = ghc2.write_policy(
        False, False, "true", "o/r", [(time.time(), "o/r")] * 5, auto=True
    )
    case(
        "policy v2：agent 自主路径（配置开即放行含审计限速，配置模糊拒）",
        ok_auto
        and r_auto == "ok"
        and not deny_cfg
        and r_cfg == "config-off"
        and not deny_rl
        and r_rl == "rate-limit",
        "auto=%s/%s cfg=%s/%s rl=%s/%s"
        % (ok_auto, r_auto, deny_cfg, r_cfg, deny_rl, r_rl),
    )
    scan_dir = os.path.join(tmp, "scandir")
    os.makedirs(scan_dir, exist_ok=True)
    with open(os.path.join(scan_dir, ".env"), "w") as f:
        f.write("SECRET=1\n")
    with open(os.path.join(scan_dir, "ok.py"), "w") as f:
        f.write("x = 1\n")
    hits_s = ghc2.secret_scan(scan_dir)
    case(
        "deploy 密钥扫描：.env 命中、干净文件不误伤",
        any(".env" in h for h in hits_s)
        and not any(h.endswith("ok.py") for h in hits_s),
        "hits=%r" % hits_s,
    )

    # (DSP) 决策·监督·计划：schema/锁/白名单/doom/证据/链/拆分 --------
    # 固定 sid 隔离：dsp 落盘走 work/plans/<sid>-*，sid 带 pid 即不污染真计划。
    import importlib.util as _ilu

    _dsp_path = os.path.join(ROOT, "tools", "dsp.py")
    _dsp_spec = _ilu.spec_from_file_location("_st_dsp", _dsp_path)
    _dsp_mod = _ilu.module_from_spec(_dsp_spec)
    _dsp_spec.loader.exec_module(_dsp_mod)
    _dsp_sid = "st-%d" % os.getpid()
    _good_tasks = [
        {
            "id": "t1",
            "title": "侦察",
            "accept": "现状一句话 rc=0",
            "tools": ["read_file"],
            "depends": [],
        },
        {
            "id": "t2",
            "title": "动手",
            "accept": "改动落盘 rc=0",
            "tools": ["write_file"],
            "depends": ["t1"],
        },
    ]
    _plan0 = _dsp_mod.new_plan("自检计划", [dict(t) for t in _good_tasks])
    ok_v, _ = _dsp_mod.validate_plan(_plan0)
    bad_noaccept = {
        "goal": "x",
        "tasks": [{"id": "t1", "title": "t", "tools": ["read_file"], "depends": []}],
    }
    ok_na, err_na = _dsp_mod.validate_plan(bad_noaccept)
    bad_fwd = {
        "goal": "x",
        "tasks": [
            {
                "id": "t1",
                "title": "t",
                "accept": "rc=0",
                "tools": ["read_file"],
                "depends": ["ghost"],
            }
        ],
    }
    ok_fw, _ = _dsp_mod.validate_plan(bad_fwd)
    case(
        "DSP schema：合法过、无 accept 拒、前向依赖拒",
        ok_v and not ok_na and "accept" in err_na and not ok_fw,
        "valid=%s noaccept=%r" % (ok_v, err_na[:40]),
    )
    ok_sv, err_sv = _dsp_mod.save_plan(_plan0, _dsp_sid)
    plan_ld, err_ld = _dsp_mod.load_plan(_dsp_sid)
    case(
        "DSP 落盘取回：save ok + bind 对上",
        ok_sv and plan_ld is not None and plan_ld.get("goal") == "自检计划",
        "save=%s err=%r" % (ok_sv, (err_sv or err_ld)[:60]),
    )
    # 计划锁：未审批时写工具锁死（理由带 /approve）——走真 _authorize。
    _spec_w = TOOLS["write_file"]
    _ctx_locked = {"root": tmproot, "mode": "full", "sid": _dsp_sid}
    _why_lock = _authorize(_spec_w, {"path": "x.txt", "content": "x"}, _ctx_locked)
    case(
        "DSP 计划锁：未审批写工具锁死（理由带 /approve）",
        isinstance(_why_lock, str) and "/approve" in _why_lock,
        str(_why_lock)[:80],
    )
    ok_ap, _ = _dsp_mod.approve_plan(_dsp_sid)
    _why_after = _authorize(_spec_w, {"path": "x.txt", "content": "x"}, _ctx_locked)
    case(
        "DSP 审批解锁：approve 后写工具不再被计划锁拦",
        ok_ap and _why_after is None,
        "approve=%s why=%r" % (ok_ap, _why_after),
    )
    # 任务白名单：t1 只许 read_file，write_file 在 t1 下被拒。
    plan_ld2, _ = _dsp_mod.load_plan(_dsp_sid)
    _w1 = _dsp_mod.check_tool(plan_ld2, "t1", "read_file", {})
    _w2 = _dsp_mod.check_tool(plan_ld2, "t1", "write_file", {})
    case(
        "DSP 白名单：t1 放 read_file、拒 write_file",
        _w1 is None and isinstance(_w2, str),
        "w1=%r w2=%r" % (_w1, _w2),
    )
    # doom 归一化：键序不同不算不同，第 3 次转 ask。
    _dw = _dsp_mod.DoomWatch()
    _r1 = _dw.note("t1", "search", {"q": "a", "n": 1})
    _r2 = _dw.note("t1", "search", {"n": 1, "q": "a"})
    _r3 = _dw.note("t1", "search", {"q": "a", "n": 1})
    case(
        "DSP doom：归一化同参第 3 次转 ask",
        _r1 is None and _r2 is None and _r3 == "ask",
        "r=%r/%r/%r" % (_r1, _r2, _r3),
    )
    # 证据 + 审计链：无 rc 拒、有 rc 过；链不断。
    ok_e1, _ = _dsp_mod.check_evidence("做完了")
    ok_e2, _ = _dsp_mod.check_evidence("pytest 全绿 rc=0")
    ok_ch, err_ch = _dsp_mod.verify_chain(_dsp_sid)
    case(
        "DSP 证据与链：无 rc 拒、有 rc 过、审计链不断",
        (not ok_e1) and ok_e2 and ok_ch,
        "chain=%r" % err_ch[:40],
    )
    # 拆分：子任务继承 + 父 stuck；子任务不再拆。
    plan_ld3, _ = _dsp_mod.load_plan(_dsp_sid)
    ok_sp, ids_sp = _dsp_mod.split_task(plan_ld3, "t2", ["拆 a", "拆 b"])
    ok_sp2, _ = _dsp_mod.split_task(plan_ld3, "t2:1", ["再拆"])
    case(
        "DSP 拆分：t2 拆出两子、父 stuck、子不再拆",
        ok_sp and ids_sp == ["t2:1", "t2:2"] and not ok_sp2,
        "ids=%r" % (ids_sp,),
    )
    # (DSP-AUTO) 分析器 + 大任务自启：纯函数三态、自启幂等、指令注入、
    # 主循环自启落盘且首任务已指派。
    _ana_big = _dsp_mod.analyze_task("把全链门跑完并部署上线", tmproot)
    _ana_small = _dsp_mod.analyze_task("读 README 前 3 行", tmproot)
    case(
        "DSP 分析：大任务 BIG、小任务 SMALL（结构触发）",
        _ana_big["grade"] == "BIG" and _ana_small["grade"] == "SMALL",
        "big=%r small=%r" % (_ana_big["reasons"][:1], _ana_small["reasons"][:1]),
    )
    _auto_sid = "st-auto-%d" % os.getpid()
    _ap1, _aa1 = _dsp_mod.auto_plan("重构门链并全量验证", _auto_sid, tmproot)
    _ap2, _aa2 = _dsp_mod.auto_plan("重构门链并全量验证", _auto_sid, tmproot)
    case(
        "DSP 自启幂等：首建有计划、重建返回 None（不覆写）",
        _ap1 is not None
        and _ap2 is None
        and "不重建" in " ".join(_aa2.get("reasons", [])),
        "first=%s second=%s" % (_ap1 is not None, _ap2 is None),
    )
    _directive = _dsp_mod.auto_directive(_ap1, _aa1) if _ap1 else ""
    case(
        "DSP 自启指令：含精修 accept 与 /approve 顺序",
        "accept" in _directive and "/approve" in _directive,
        _directive[:60],
    )
    # 主循环自启：run_agent 跑 BIG 需求（桩端点零工具终答），计划自动落盘。
    _run_sid = "st-run-%d" % os.getpid()
    _stub0 = Stub([_resp_json("自启验证终答", cid=None)])
    try:
        _res0 = run_agent(
            "实现一个全新的部署流水线并全量验证",
            ctx={"root": tmproot, "mode": "accept-edits", "sid": _run_sid},
            provider=_stub0.provider(protocol="responses"),
            max_steps=2,
        )
    finally:
        _stub0.close()
    _pl0, _e0 = _dsp_mod.load_plan(_run_sid)
    case(
        "DSP 主循环自启：BIG 需求自动落盘 + 摘要进 prompt",
        _res0.get("ok")
        and _pl0 is not None
        and _pl0.get("goal", "").startswith("实现一个全新"),
        "ok=%s plan=%s" % (_res0.get("ok"), _pl0 is not None),
    )
    # 清理自检计划残留（plans/ 下 _dsp_sid/_auto_sid/_run_sid 三组全清）。
    for _sid_c in (_dsp_sid, _auto_sid, _run_sid):
        for _suf in ("-plan.json", "-chain.jsonl"):
            try:
                os.unlink(os.path.join(ROOT, "work", "plans", _sid_c + _suf))
            except OSError:
                pass
    case(
        "瞬时词表：认得传输事故不认得 401",
        bool(_TRANSIENT_RE.search("connection reset by peer"))
        and bool(_TRANSIENT_RE.search("i/o timeout"))
        and not _TRANSIENT_RE.search("HTTP 401: Bad credentials"),
        "reset=%s 401=%s"
        % (
            bool(_TRANSIENT_RE.search("connection reset")),
            bool(_TRANSIENT_RE.search("HTTP 401")),
        ),
    )

    # (ESC/UNDO) 打断与快照（蒸馏自 cline checkpoints / CC Esc）----------
    stub_i = Stub([_resp_json("nope", cid=None)])
    raised_i = False
    called_i = True
    try:
        run_agent(
            "x",
            ctx={"root": tmproot, "mode": "full"},
            provider=stub_i.provider(protocol="responses"),
            max_steps=2,
            cancel=lambda: True,
        )
    except AgentError as e:
        raised_i = e.kind == "interrupted"
        called_i = stub_i.seen == []
    finally:
        stub_i.close()
    case(
        "Esc 打断：cancel 置位即抛 interrupted（端点未被调用）",
        raised_i and called_i,
        "raised=%s seen=%r" % (raised_i, stub_i.seen),
    )
    snap = os.path.join(tmp, "snap")
    uctx = {"root": tmproot, "mode": "full", "sid": "t1", "snap_dir": snap}
    # 独占文件名：tmproot 里前面的用例写过 hello.txt（桩响应真落盘），
    # 快照例的「时点不存在」前提必须不依赖执行顺序。
    hp = os.path.join(tmproot, "undo-probe.txt")
    r1u, _ = exec_tool("write_file", {"path": "undo-probe.txt", "content": "v1"}, uctx)
    r2u, _ = exec_tool("write_file", {"path": "undo-probe.txt", "content": "v2"}, uctx)
    _repl_slash("/undo", uctx, None, "t1", "p")
    back = open(hp, encoding="utf-8").read()
    case(
        "/undo：恢复上一写（v2→v1）",
        bool(r1u.get("ok")) and bool(r2u.get("ok")) and back == "v1",
        "back=%r ok1=%s ok2=%s" % (back, r1u.get("ok"), r2u.get("ok")),
    )
    _repl_slash("/undo", uctx, None, "t1", "p")
    _repl_slash("/undo", uctx, None, "t1", "p")
    case(
        "/undo：耗尽后如实报（快照时点不存在→删除，再 undo 无快照）",
        not os.path.exists(hp),
        "exists=%s" % os.path.exists(hp),
    )

    # (W13) 自主执行：/goal 建菱形 DAG 计划（未审批）→ 未审批 run 必拒
    # （rc=2 + block 物证 + 零派发）→ 审批后桩 worker 一次跑完小闭环。
    # 界面料与引擎锁面在这里各钉一颗钉（深审在 autonomy_gate）。
    gsid = "ck_goal_%d" % os.getpid()
    gdir = os.path.join(ROOT, "work", "autonomy", gsid)
    try:
        gctx = {"mode": "accept-edits", "root": ROOT, "history": []}
        _repl_slash("/goal 给自主引擎做个小目标夹具", gctx, None, gsid, "p")
        _gp, _gerr = _dsp().load_plan(gsid)
        _tasks = (_gp or {}).get("tasks") or []
        case(
            "/goal 建菱形 DAG 计划：落盘未审批 + 5 任务里程碑齐",
            _gp is not None
            and _gp.get("approved") is False
            and len(_tasks) == 5
            and all(t.get("milestone") for t in _tasks),
            "plan=%s approved=%s n=%d"
            % (_gp is not None, (_gp or {}).get("approved"), len(_tasks)),
        )
        _auto_m = _autonomy()
        _rc_g = _auto_m.run_goal(gsid, root=ROOT, log=lambda s: None)
        _grows = _auto_m._read_ledger(ROOT, gsid)
        _gblk = os.path.join(ROOT, "work", "plans", "%s-block.md" % gsid)
        case(
            "autonomy 未审批拒跑（rc=2 + block 物证 + 零派发）",
            _rc_g == 2
            and os.path.exists(_gblk)
            and not any(r.get("event") == "dispatch" for r in _grows),
            "rc=%s block=%s rows=%d" % (_rc_g, os.path.exists(_gblk), len(_grows)),
        )
        _dsp().approve_plan(gsid)
        _w = '"%s" -c "print(\'STUB OK rc=0\')" {prompt}' % sys.executable
        _rc_c = _auto_m.run_goal(gsid, root=ROOT, worker=_w, log=lambda s: None)
        _gp2, _ge2 = _dsp().load_plan(gsid)
        _tasks2 = (_gp2 or {}).get("tasks") or []
        case(
            "autonomy 桩小闭环：审批后一次跑到 5/5 全绿（rc=0）",
            _rc_c == 0 and all(t.get("status") == "done" for t in _tasks2),
            "rc=%s done=%d/%d"
            % (
                _rc_c,
                sum(1 for t in _tasks2 if t.get("status") == "done"),
                len(_tasks2),
            ),
        )
    except Exception as e:  # noqa: BLE001
        case("/goal 自主执行三连（建计划/拒跑/小闭环）", False, repr(e)[:120])
    finally:
        for _p in (
            os.path.join(ROOT, "work", "plans", "%s-plan.json" % gsid),
            os.path.join(ROOT, "work", "plans", "%s-chain.jsonl" % gsid),
            os.path.join(ROOT, "work", "plans", "%s-block.md" % gsid),
            os.path.join(ROOT, "work", "autonomy", "%s.jsonl" % gsid),
            os.path.join(ROOT, "work", "autonomy", "%s-REPORT.md" % gsid),
        ):
            try:
                os.unlink(_p)
            except OSError:
                pass
        shutil.rmtree(gdir, ignore_errors=True)
        try:  # 父目录空了也清（门链零污染判据盯 work/）
            os.rmdir(os.path.join(ROOT, "work", "autonomy"))
        except OSError:
            pass

    # (W0-4) 斜杠补全表防漂：dispatch 里能敲的每条 /cmd 都必须在
    # REPL_SLASH_CMDS 里。2026-10-12 实测漂了 12 条（/evo /undo /plan …
    # 能敲但 Tab 补不出来，补全表停在早期版本）—— 用结构断言钉住，
    # 而不是下次靠人再发现一遍。
    _src = open(os.path.abspath(__file__), encoding="utf-8").read()
    _dispatch = set(re.findall(r'if cmd == "(/[a-z-]+)":', _src))
    _missing = sorted(_dispatch - set(REPL_SLASH_CMDS))
    case(
        "REPL 补全表覆盖全部斜杠命令（dispatch ⊆ REPL_SLASH_CMDS）",
        not _missing,
        "缺=%s" % (_missing or "无"),
    )


def main(argv=None):
    args = _build_parser().parse_args(argv)
    if args.provider:
        # --provider 落进 env：resolve_provider 的每个调用点
        # （REPL / headless / run_agent / 自检）都能看见，不必逐处穿参。
        os.environ["AGENTOS_PROVIDER"] = args.provider
    if args.check:
        return _selftest()
    try:
        if args.prompt or args.print_mode:
            return _run_once(args)
        return _repl(args)
    except AgentError as e:
        sys.stderr.write("agent_core 失败 [%s] %s\n" % (e.kind, e))
        return _rc_of_error(e.kind)
    except KeyboardInterrupt:
        sys.stderr.write("\n已中止\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
