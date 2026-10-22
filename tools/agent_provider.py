# -*- coding: utf-8 -*-
"""provider 层：端点解析 / 配置读写 / 模型协议归一（从 agent_core.py 搬出）。

为什么要搬：2026-10-21 的第二个障碍 —— 主文件 308KB、`read_file` 上限
256KB，agent 读不了自己的主文件。这一块 52KB，AST 复核后**零个**内核私有
全局引用（段内所有裸名都在这段里自己定义，或来自 stdlib），是天然自洽的块。

依赖方向：本模块只依赖 stdlib；agent_core 单向 `from agent_provider import *`
式的显式导入，并且**同名绑定留在 agent_core 的命名空间里**——自检用
`vars(_core)["_read_agentos_config"] = fake` 打补丁依旧生效（打的是导入后
的绑定，函数体按裸名查 globals）。

行为一字不变：函数体逐字复制，未改一行逻辑。
"""

import json
import os
import re
import threading
import tomllib
import urllib.error
import urllib.parse
import urllib.request
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
        # 2026-10-22：兜底默认从 muse-spark-1.3-contributor 改成
        # **deepseek-v4.1-flash**（= 用户实际在用的默认）。实测端点确有 36
        # 个模型，两个名字都在列里 —— 所以旧兜底不是「不存在的模型」，
        # 而是**静默降级**：配置一坏/写漏 model，agent 就从 4.1-flash
        # 悄悄掉到 contributor 级小模型（能力差一档，而且没有任何提示）。
        # 兜底必须与实际默认一致。
        "model": "deepseek-v4.1-flash",
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


def list_models(provider, timeout=20):
    """列端点可用模型（GET <base>/models，带同一套会话头）。

    为什么（2026-10-22）：**换模型要能验**。换一个端点没有的名字 →
    任务跑到一半才 404/400，报错还常常被当成「网络问题」。先把可选名单
    拉出来，换之前对一眼；换不了就**如实说探不到**（不猜、不假装）。

    返回 (names, err)：names 为空且 err 非空 = 探不到。
    """
    base = str(getattr(provider, "base_url", "") or "").rstrip("/")
    if not base:
        return [], "没有 base_url"
    req = urllib.request.Request(base + "/models", method="GET")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", _DEFAULT_UA)
    _sid = str(getattr(provider, "session_id", "") or "")
    if _sid:
        # opencode 网关强制要会话头（漏带连 /models 都过不了）
        req.add_header("x-opencode-session", _sid)
    for k, v in (getattr(provider, "headers", {}) or {}).items():
        if str(k).lower() in ("content-length", "host", "accept-encoding"):
            continue
        req.add_header(str(k), str(v))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(1 << 20)
    except Exception as e:  # noqa: BLE001
        return [], "探不到模型列表：%s" % str(e)[:120]
    try:
        obj = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as e:
        return [], "模型列表不是合法 JSON：%s" % str(e)[:80]
    names = []
    _data = obj.get("data") if isinstance(obj, dict) else obj
    if isinstance(_data, list):
        for it in _data:
            if isinstance(it, dict):
                n = str(it.get("id") or it.get("name") or "").strip()
                if n and n not in names:
                    names.append(n)
            elif isinstance(it, str) and it.strip() and it.strip() not in names:
                names.append(it.strip())
    if not names:
        return [], "模型列表为空或形状不认识"
    return sorted(names), ""


def check_model(provider, name):
    """换模型前的验证：端点有没有这个模型。返回 (ok, names, err)。

    口径（2026-10-22）：探不到名单时 **ok=True**（允许换，但调用方必须
    如实说「没能验证」）—— 探针失败不等于模型不存在，不该把网络问题
    变成「不许换」。名单拿到了就**对得上才算**。
    """
    names, err = list_models(provider)
    if not names:
        return True, [], err
    return (str(name) in names), names, ""


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
