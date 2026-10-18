# -*- coding: utf-8 -*-
"""生成 evo-evo-err-provider 提案 JSON（只在 work/ 下写，不碰真树源码）。"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "work", "evo_proposals", "evo-evo-err-provider.json")

FIND1 = '''def _key_hint(code, raw):
    """认证/额度类 HTTP 错误的「下一步怎么办」提示。

    为什么不进 _TRANSIENT_RE 也不自愈：401/403 用户必须自己去发新 key，
    机器无权替他造凭证。能做的是把求医三件套（现象、病灶、药方）一句话
    说全——实测 2026-10-18「无法正常使用」半天，就是因为裸 401 不带指引。
    """
    if int(code) not in (401, 402, 403):
        return ""
    if re.search(
        r"api[_ ]?key|auth\\b|expires|unauthorized", raw, re.I
    ):
        return (
            "\\n  → API key 缺失/失效：`agentos config set llm.api_key <新key>`"
            "（或 AGENTOS_LLM_API_KEY）后重试"
        )
    return "\\n  → 端点拒绝本请求：检查 key 权限/额度（agentos config get llm）"'''

REPL1 = '''def _key_hint(code, raw, base_url=""):
    """端点失败的「下一步怎么办」：现象（状态码 + body）已在报错里，这里补病灶与药方。

    为什么不进 _TRANSIENT_RE 也不自愈：400/401/403/404/422/429/5xx 都不是传输
    层瞬时错（_TRANSIENT_RE 只管 TLS/重置/超时那一档），机器也无权替用户造
    key、改网关路由、猜协议。能做的只有一件 —— 把下一步该敲哪条命令说全。
    同一条 err:provider 反复出现 5 次，形态全是「裸状态码 + body」不带指引，
    每次都要从头重推诊断；根因就是这里原先只认 401/402/403，其余状态码与
    不可达分支全落空。分档口径对齐 openai-python `_exceptions.py`（400/401/
    403/404/429/5xx 各自成类、连接失败单列），不把「网关拒了请求」和「根本
    连不上」混成同一句「端点失败」。
    每个分档都给同一条探活命令 `GET <base>/models`：它把「端点活着但上游/
    请求体有问题」与「链路根本不通」当场分开，是本教训里唯一真正省时间的判据。
    """
    body = raw or ""
    base = (base_url or "<base_url>").rstrip("/")
    probe = (
        "\\n  → 探活：`curl -sS %s/models`（200=端点活着，问题在上游/请求体；"
        "连不上=网络/代理）" % base
    )
    if code in (401, 402) or re.search(
        r"api[_ ]?key|auth\\b|expires|unauthorized|invalid.*key", body, re.I
    ):
        return (
            "\\n  → API key 缺失/失效：`agentos config set llm.api_key <新key>`"
            "（或 AGENTOS_LLM_API_KEY）后重试" + probe
        )
    if code == 403:
        if re.search(
            r"cloudflare|error code:? ?10\\d\\d|just a moment|waf", body, re.I
        ):
            return (
                "\\n  → 403 是 WAF/网关按客户端特征拦的，不是 key：用"
                " AGENTOS_LLM_HEADERS 换 UA 再试" + probe
            )
        return (
            "\\n  → 端点拒绝本请求：查 key 权限/额度与网关路由"
            "（agentos config get llm）" + probe
        )
    if code in (400, 422):
        if re.search(r"missing[ _]?session|session.*(required|missing)", body, re.I):
            return (
                "\\n  → 网关要会话头而这份请求没带：用 `--provider opencode`"
                "（内核自动注入 x-opencode-session）或 AGENTOS_LLM_HEADERS 补"
                + probe
            )
        if re.search(r"call_id|no tool call found|tool output", body, re.I):
            return (
                "\\n  → 网关会话脱钩：净荷自洽、坏的是服务端会话 —— 换会话再发"
                "（重启 REPL 或 /branch 出新会话）" + probe
            )
        if re.search(r"protocol|not support", body, re.I):
            return (
                "\\n  → 协议不对：该端点可能只认 responses，"
                "`AGENTOS_LLM_PROTOCOL=responses` 再试" + probe
            )
        return (
            "\\n  → key 多半没问题，是网关路由/请求体：查上游路由与会话头，"
            "或用 /model 现场换模型" + probe
        )
    if code == 404:
        return (
            "\\n  → 路径或模型名不对：`%s/models` 列可用模型，"
            "再用 /model 换模型" % base + probe
        )
    if code == 429:
        return "\\n  → 限流/额度：退避后重试，或 /model 换模型 / 换 key" + probe
    if 500 <= code <= 599:
        return (
            "\\n  → 上游 5xx：网关/模型侧过载，退避重试；持续报就用 /model 换模型"
            "或换 key" + probe
        )
    return ""


_UNREACHABLE_HINT = (
    "\\n  → 不可达=网络/代理，不是 key：先 `curl -sS <上面那个端点>/models` "
    "确认链路，再查代理变量与 AGENTOS_LLM_VIA_PROXY 该不该开；本机后端就查"
    "进程在不在（agentos health）"
)'''

FIND2 = '''                    _key_hint(e.code, raw.decode("utf-8", "replace")),'''

REPL2 = '''                    _key_hint(
                        e.code, raw.decode("utf-8", "replace"), self.base_url
                    ),'''

FIND3 = '''        except urllib.error.URLError as e:
            raise AgentError(
                "provider", "模型端点不可达：%s" % (getattr(e, "reason", e),)
            )'''

REPL3 = '''        except urllib.error.URLError as e:
            raise AgentError(
                "provider",
                "模型端点不可达：%s（端点 %s）%s"
                % (
                    getattr(e, "reason", e),
                    self.base_url or "<未配置 base_url>",
                    _UNREACHABLE_HINT,
                ),
            )'''

rationale = (
    "根因（tools/agent_core.py:242 `_key_hint`）：端点失败提示只覆盖 "
    "401/402/403，其余 HTTP 码（400/404/422/429/5xx）与 `URLError` 不可达分支"
    "（:816）只抛裸消息；403 也不区分「key 无权限」与「Cloudflare/WAF 按客户端"
    "特征拦」。于是同一条 err:provider 反复出现 5 次，每次都要从头重推"
    "「探活→分档→换模型」。修法：同一函数内按状态码分档，每档都给 "
    "`curl -sS <base>/models` 探活命令与 /model 换模型出路；不可达分支补端点名 + "
    "网络/代理判定。分档口径参照 openai/openai-python "
    "src/openai/_exceptions.py（400 BadRequestError / 401 AuthenticationError / "
    "403 PermissionDeniedError / 404 NotFoundError / 429 RateLimitError / 5xx "
    "InternalServerError，连接失败单列 APIConnectionError）。不改控制流、不加"
    "自愈、不绕门：仍是 raise AgentError('provider', ...) 原路抛出。"
)

proposal = {
    "id": "evo-evo-err-provider",
    "edits": [
        {"path": "tools/agent_core.py", "find": FIND1, "replace": REPL1},
        {"path": "tools/agent_core.py", "find": FIND2, "replace": REPL2},
        {"path": "tools/agent_core.py", "find": FIND3, "replace": REPL3},
    ],
    "rationale": rationale,
}

# 自检：三个锚点在真文件里必须各恰出现一次
src = open(os.path.join(ROOT, "tools", "agent_core.py"), encoding="utf-8").read()
for i, f in enumerate((FIND1, FIND2, FIND3), 1):
    n = src.count(f)
    print("anchor%d count=%d" % (i, n))
    assert n == 1, "anchor%d 不唯一" % i

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(proposal, f, ensure_ascii=False, indent=2)
print("WROTE", OUT, os.path.getsize(OUT), "bytes")
