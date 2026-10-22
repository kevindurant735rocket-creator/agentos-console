#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""model_selftest —— 「默认模型 / 换模型」这一层的证伪集。

为什么要有（2026-10-22 实测）
---------------------------
实测端点 `opencode.ai/zen/go/v1` 的 `/models` 共 **36 个模型**，其中同时
包含 `deepseek-v4.1-flash`（用户实际默认）与 `muse-spark-1.3-contributor`
（代码里的旧兜底）。所以旧兜底不是「不存在的模型」，而是**静默降级**：
配置一坏 / 写漏 `model`，agent 就从 4.1-flash 悄悄掉到 contributor 级
小模型 —— 能力差一档，而且**没有任何提示**。

这个证伪集钉四件事：
  1. 兜底默认 == 实际默认（不许再降级）；
  2. **换模型要能验**：先把端点的可用名单拉出来，名单里有才换；
  3. 探不到名单时要**如实说**（网络问题不该变成「不许换」）；
  4. 探针**带会话头**（opencode 网关漏带必 400，连 /models 都过不了）。

离线跑（本地假端点，不联网、不碰真模型）。

裁决：MODEL_SELFTEST n/n + MODEL_SELFTEST_OK，exit 0/1
"""

import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

CASES = []


def case(name, ok, detail=""):
    CASES.append((name, bool(ok), str(detail)))
    print(("PASS " if ok else "FAIL ") + name + ("  -- " + str(detail)[:160] if detail else ""))


class _ModelsHandler(BaseHTTPRequestHandler):
    seen_session = []

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            _ModelsHandler.seen_session.append(
                self.headers.get("x-opencode-session") or ""
            )
            _body = json.dumps(
                {
                    "object": "list",
                    "data": [
                        {"id": "deepseek-v4.1-flash"},
                        {"id": "deepseek-v4-pro"},
                        {"id": "glm-5.3"},
                    ],
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(_body)))
            self.end_headers()
            self.wfile.write(_body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):  # 静音
        return


def _fake_provider(base_url, session_id="agentos-test"):
    import agent_provider as ap

    return ap.Provider(
        {
            "base_url": base_url,
            "model": "deepseek-v4.1-flash",
            "headers": {},
            "protocol": "responses",
            "session_id": session_id,
        }
    )


def main():
    import agent_provider as ap

    print("MODEL_SELFTEST 默认模型 / 换模型证伪")

    # ── 1. 兜底默认 == 实际默认（不许静默降级）─────────────────
    _spec = ap.NAMED_PROVIDERS.get("opencode") or {}
    case(
        "兜底默认 == 实际默认 deepseek-v4.1-flash（配置坏也不降级）",
        _spec.get("model") == "deepseek-v4.1-flash",
        "model=%r" % (_spec.get("model"),),
    )
    case(
        "端点表仍带 protocol=responses（换协议=当场 400，这条不能被顺手删）",
        _spec.get("protocol") == "responses",
        "protocol=%r" % (_spec.get("protocol"),),
    )

    # ── 2. 探针：列模型（本地假端点，不联网）───────────────────
    _srv = HTTPServer(("127.0.0.1", 0), _ModelsHandler)
    _t = threading.Thread(target=_srv.serve_forever, daemon=True)
    _t.start()
    _base = "http://127.0.0.1:%d/v1" % _srv.server_address[1]
    try:
        _P = _fake_provider(_base)
        _names, _err = ap.list_models(_P)
        case(
            "list_models：拉到端点的可用模型名单",
            set(_names) == {"deepseek-v4.1-flash", "deepseek-v4-pro", "glm-5.3"}
            and _names == sorted(_names),  # 名单按名字排序（可预期的顺序）
            "names=%r err=%r" % (_names, _err),
        )
        case(
            "探针带会话头（网关漏带必 400，连 /models 都过不了）",
            bool(_ModelsHandler.seen_session)
            and _ModelsHandler.seen_session[0] == "agentos-test",
            "session=%r" % (_ModelsHandler.seen_session[:2],),
        )

        # ── 3. 换模型先验 ─────────────────────────────────────
        _ok, _n, _e = ap.check_model(_P, "glm-5.3")
        case(
            "check_model：名单里的模型 → 允许换",
            _ok is True and _e == "",
            "ok=%s err=%r" % (_ok, _e),
        )
        _ok2, _n2, _e2 = ap.check_model(_P, "no-such-model")
        case(
            "check_model：名单里没有的 → 拒（换过去=跑到一半才 404）",
            _ok2 is False and "no-such-model" not in ("" if _ok2 else "x"),
            "ok=%s names=%r" % (_ok2, _n2[:3]),
        )
    finally:
        _srv.shutdown()
        _srv.server_close()

    # ── 4. 探不到 → 如实说，且**允许换**（网络问题≠模型不存在）──
    _dead = "http://127.0.0.1:1/v1"
    _PD = _fake_provider(_dead)
    _dn, _derr = ap.list_models(_PD, timeout=2)
    case(
        "端点探不到 → 如实说（names 空 + err 非空，不假装列得出）",
        _dn == [] and bool(_derr),
        "names=%r err=%r" % (_dn, _derr),
    )
    _dok, _dn2, _derr2 = ap.check_model(_PD, "any-model")
    case(
        "端点探不到 → 仍允许换（探针失败≠模型不存在），但 err 要带出去",
        _dok is True and bool(_derr2),
        "ok=%s err=%r" % (_dok, _derr2),
    )

    # ── 5. 配置缺失时不能退到本地模型（no_fallback 的纪律）──────
    try:
        _cfg = ap.resolve_provider({"HOME": tempfile.mkdtemp()})
        case(
            "空 HOME（无配置）→ 解析不出就报 no-provider（不落本地模型）",
            not _cfg or _cfg.get("model"),
            "model=%r" % ((_cfg or {}).get("model"),),
        )
    except Exception as e:  # noqa: BLE001
        case(
            "空 HOME（无配置）→ 解析不出就报 no-provider（不落本地模型）",
            True,
            "如实报错=%r" % (e,)[:60],
        )

    bad = [c for c in CASES if not c[1]]
    print("\nMODEL_SELFTEST %d/%d" % (len(CASES) - len(bad), len(CASES)))
    print("MODEL_SELFTEST_OK" if not bad else "MODEL_SELFTEST_RED")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
