# -*- coding: utf-8 -*-
"""GitHub 只读客户端（只用标准库）。

定位：把「读 GitHub」做成 AgentOS 的一等公民。本模块是 `agentos gh` 子命令与
`-p` 自然语言 github 路由的共同数据源；两者读的是同一份客户端，不存在第二套真相。

只读铁律（结构性保证，不是口头承诺）：
  · 所有对 gh 的调用都经过唯一收口 run_gh()，它先过 _assert_readonly() 白名单，
    只有「清单内的只读命令向量」才被放行。清单之外的任何写法在这里就抛错，
    因此本文件结构上发不出远端变更。
  · 直连兜底 api_get() 只发 HTTP GET；限流（403 + X-RateLimit-Remaining:0）
    会被单独识别成 kind="ratelimit"，不伪装成成功。
  · 本客户端不提供任何「本地草稿外发」之外的能力，更不碰远端写入面。

失败分类（上层据此给不同退出码，绝不含混成 rc=0）：
  no-binary  找不到 gh 可执行文件
  rc         gh 非 0 退出
  json       输出不是合法 JSON（或响应缺关键字段）
  empty      gh 成功但 stdout 为空（空输出不当成功）
  timeout    调用超时
  network    直连兜底的网络层错误
  ratelimit  GitHub API 限流
  usage      调用方参数不合法（如缺 owner/repo）
"""
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request

# gh 真身常驻 Homebrew；shutil.which 优先，找不到再回退硬路径，逻辑里不写死账号。
_GH_HOME = "/opt/homebrew/bin/gh"


class GhError(Exception):
    """只读调用失败。kind 见模块 docstring 的失败分类。"""

    def __init__(self, kind, msg, cmd=None):
        super(GhError, self).__init__(msg)
        self.kind = kind
        self.cmd = list(cmd or [])


# 允许的只读命令向量前缀（小写）。run_gh 会做最长前缀匹配：argv 的前 n 个 token
# 必须整体等于表里某一条，才放行；否则抛错。写操作天然不在表内。
_READ_PREFIXES = {
    ("auth", "status"),
    ("auth", "token"),
    ("api", "user"),
    ("repo", "list"),
    ("repo", "view"),
    ("issue", "list"),
    ("issue", "view"),
    ("issue", "status"),
    ("pr", "list"),
    ("pr", "view"),
    ("pr", "status"),
    ("pr", "checks"),
    ("run", "list"),
    ("run", "view"),
    ("search", "repos"),
    ("search", "issues"),
    ("search", "code"),
    ("search", "commits"),
    ("search", "prs"),
    ("search", "pulls"),
}

# gh api 里用于指定方法/请求体的旗标：出现即意味着非 GET，一律拒绝。
_API_WRITE_FLAGS = {"-x", "--method", "-f", "--field", "--raw-field", "--input"}


def resolve_gh():
    """返回 gh 可执行路径；找不到返回 None。which 优先，回退 Homebrew 固定路径。"""
    p = shutil.which("gh")
    if p:
        return p
    if os.path.isfile(_GH_HOME) and os.access(_GH_HOME, os.X_OK):
        return _GH_HOME
    return None


def is_readonly(argv):
    """argv（不含 gh 本身）是否落在只读白名单内。真值判定，供门自证。"""
    low = [str(a).lower() for a in argv]
    if low[:1] == ["api"]:
        if any(t in _API_WRITE_FLAGS for t in low):
            return False
        return len(low) >= 2 and not str(low[1]).startswith("-")
    for n in range(min(len(low), 3), 0, -1):
        if tuple(low[:n]) in _READ_PREFIXES:
            return True
    return False


def _assert_readonly(argv):
    if not is_readonly(argv):
        raise ValueError("gh 命令不在只读白名单内（结构上拒绝）：%s"
                         % " ".join(str(a) for a in argv))


def run_gh(argv, timeout=30.0):
    """唯一收口：跑一条只读 gh 命令，返回结构化结果 dict。

    先过只读白名单（写向量直接抛 ValueError，是程序错误而非运行失败），
    再真正执行。返回体总能区分 ok 与失败 kind，见模块 docstring。
    """
    argv = [str(a) for a in argv]
    _assert_readonly(argv)
    gh = resolve_gh()
    disp = "gh " + " ".join(argv)
    if gh is None:
        return {"ok": False, "kind": "no-binary", "rc": None, "cmd": [disp],
                "disp": disp, "stdout": "", "stderr": "", "json_ok": False,
                "parsed": None,
                "error": "找不到 gh：PATH 与 %s 均无可用可执行文件" % _GH_HOME}
    try:
        p = subprocess.run([gh] + argv, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "kind": "timeout", "rc": 124, "cmd": [disp],
                "disp": disp, "stdout": "", "stderr": "", "json_ok": False,
                "parsed": None, "error": "gh 调用超时（>%ss）" % timeout}
    except OSError as e:
        return {"ok": False, "kind": "no-binary", "rc": None, "cmd": [disp],
                "disp": disp, "stdout": "", "stderr": str(e), "json_ok": False,
                "parsed": None, "error": "gh 启动失败：%s" % e}
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    base = {"cmd": [disp], "disp": disp, "rc": p.returncode,
            "stderr": err[:400]}
    if p.returncode != 0:
        base.update({"ok": False, "kind": "rc", "stdout": out,
                     "json_ok": False, "parsed": None,
                     "error": "gh 退出码 %s：%s" % (p.returncode,
                                                  (err or out)[:200])})
        return base
    if not out:
        base.update({"ok": False, "kind": "empty", "stdout": "",
                     "json_ok": False, "parsed": None,
                     "error": "gh 成功但 stdout 为空（空输出不当成功）"})
        return base
    try:
        parsed = json.loads(out)
        base.update({"ok": True, "kind": "json", "stdout": out,
                     "json_ok": True, "parsed": parsed, "error": None})
    except ValueError:
        # 并非所有只读命令都回 JSON（auth status 回文本）。标 json_ok=False，
        # 由需要 JSON 的上层转成 kind=json 失败。
        base.update({"ok": True, "kind": "text", "stdout": out,
                     "json_ok": False, "parsed": None, "error": None})
    return base


def _need(r):
    """ok 校验：任何运行失败都如实升级为 GhError（kind 原样带上）。"""
    if not r.get("ok"):
        raise GhError(r.get("kind", "rc"), r.get("error", "gh 调用失败"),
                      r.get("cmd"))
    return r


def _need_json(r):
    """在 _need 之上再要求「是合法 JSON」；否则 kind=json 失败。"""
    _need(r)
    if not r.get("json_ok"):
        raise GhError("json", "gh 输出不是合法 JSON：%s" % r["stdout"][:120],
                      r.get("cmd"))
    return r["parsed"]


def _slug(a, b=None):
    """owner/repo 或 repo 归一成 owner/repo。接受 "o/r" 或 ("o","r")。"""
    if b is not None:
        s = "%s/%s" % (a, b)
    else:
        s = str(a or "")
    s = s.strip().strip("/")
    if s.count("/") != 1 or not all(s.split("/")):
        raise GhError("usage", "需要 owner/repo 形式，收到：%r" % s)
    return s


# ── 高层只读取数：返回 (rows, sources)。行是 (label, value[, meta]) 三元组。
#   sources 记录实际执行的 gh 原文（disp），失败抛 GhError。 ────────────────


def whoami():
    r = run_gh(["api", "user"], timeout=30)
    d = _need_json(r)
    if not isinstance(d, dict) or not d.get("login"):
        raise GhError("json", "whoami 响应缺 login 字段", r["cmd"])
    rows = [
        ("登录账号", str(d.get("login"))),
        ("账号类型", str(d.get("type") or "-")),
        ("公开仓库", str(d.get("public_repos", "-"))),
        ("公开 Gist", str(d.get("public_gists", "-"))),
        ("关注者", str(d.get("followers", "-"))),
        ("主页", str(d.get("html_url") or "-")),
    ]
    return rows, [r["disp"]]


def auth():
    r = run_gh(["auth", "status"], timeout=30)
    _need(r)
    lines = [ln.strip() for ln in r["stdout"].splitlines() if ln.strip()]
    rows = [("认证状态", "ok（gh auth status rc=0）")]
    for ln in lines[:6]:
        rows.append(("·", ln))
    return rows, [r["disp"]]


def repos(limit=20):
    r = run_gh(["repo", "list", "--limit", str(int(limit)),
                "--json", "nameWithOwner,visibility,isFork,updatedAt"], 30)
    arr = _need_json(r)
    if not isinstance(arr, list):
        raise GhError("json", "repos 响应不是数组", r["cmd"])
    rows = [("仓库数", str(len(arr)))]
    for it in arr:
        rows.append((it.get("nameWithOwner", "?"),
                     "%s · 更新 %s%s" % (it.get("visibility", "?"),
                                        it.get("updatedAt", "?"),
                                        " · fork" if it.get("isFork") else "")))
    return rows, [r["disp"]]


def repo(slug):
    s = _slug(slug)
    r = run_gh(["repo", "view", s, "--json",
                "nameWithOwner,description,visibility,stargazerCount,"
                "forkCount,primaryLanguage,updatedAt"], 30)
    d = _need_json(r)
    pl = d.get("primaryLanguage") or {}
    rows = [
        ("仓库", str(d.get("nameWithOwner", s))),
        ("可见性", str(d.get("visibility", "?"))),
        ("描述", str(d.get("description") or "-")),
        ("Star", str(d.get("stargazerCount", "-"))),
        ("Fork", str(d.get("forkCount", "-"))),
        ("主语言", str(pl.get("name", "-") if isinstance(pl, dict) else pl)),
        ("更新", str(d.get("updatedAt", "-"))),
    ]
    return rows, [r["disp"]]


def issues(slug, state="open", limit=20):
    s = _slug(slug)
    st = state if state in ("open", "closed", "all") else "open"
    r = run_gh(["issue", "list", "--repo", s, "--state", st,
                "--limit", str(int(limit)),
                "--json", "number,title,state,author"], 30)
    arr = _need_json(r)
    if not isinstance(arr, list):
        raise GhError("json", "issues 响应不是数组", r["cmd"])
    rows = [("仓库", s), ("状态", st), ("条数", str(len(arr)))]
    for it in arr:
        au = it.get("author") or {}
        rows.append(("#%s" % it.get("number"),
                     "%s [%s] by %s" % (it.get("title", "?"),
                                        it.get("state", "?"),
                                        au.get("login", "?")
                                        if isinstance(au, dict) else "?")))
    return rows, [r["disp"]]


def prs(slug, state="open", limit=20):
    s = _slug(slug)
    st = state if state in ("open", "closed", "all") else "open"
    r = run_gh(["pr", "list", "--repo", s, "--state", st,
                "--limit", str(int(limit)),
                "--json", "number,title,state,author,headRefName"], 30)
    arr = _need_json(r)
    if not isinstance(arr, list):
        raise GhError("json", "prs 响应不是数组", r["cmd"])
    rows = [("仓库", s), ("状态", st), ("条数", str(len(arr)))]
    for it in arr:
        au = it.get("author") or {}
        rows.append(("#%s" % it.get("number"),
                     "%s [%s] %s by %s" % (it.get("title", "?"),
                                           it.get("state", "?"),
                                           it.get("headRefName", ""),
                                           au.get("login", "?")
                                           if isinstance(au, dict) else "?")))
    return rows, [r["disp"]]


def runs(slug, limit=10):
    s = _slug(slug)
    r = run_gh(["run", "list", "--repo", s, "--limit", str(int(limit)),
                "--json", "name,status,conclusion,headBranch,event"], 30)
    arr = _need_json(r)
    if not isinstance(arr, list):
        raise GhError("json", "runs 响应不是数组", r["cmd"])
    rows = [("仓库", s), ("工作流运行", str(len(arr)))]
    for it in arr:
        rows.append((it.get("name", "?"),
                     "%s · %s · %s（%s）" % (it.get("status", "?"),
                                           it.get("conclusion") or "进行中",
                                           it.get("headBranch", "?"),
                                           it.get("event", "?"))))
    return rows, [r["disp"]]


def search(kind, query, limit=20):
    k = kind if kind in ("repos", "issues", "code", "commits", "prs",
                         "pulls") else "repos"
    q = (query or "").strip()
    if not q:
        raise GhError("usage", "search 需要查询词")
    fields = ("fullName,description,stargazersCount" if k == "repos"
              else "number,title,repository,state")
    r = run_gh(["search", k, q, "--limit", str(int(limit)),
                "--json", fields], 30)
    arr = _need_json(r)
    if not isinstance(arr, list):
        raise GhError("json", "search 响应不是数组", r["cmd"])
    rows = [("类型", k), ("命中", str(len(arr)))]
    for it in arr:
        if k == "repos":
            rows.append((it.get("fullName", "?"),
                         "★%s · %s" % (it.get("stargazersCount", "-"),
                                       (it.get("description") or "")[:60])))
        else:
            rp = it.get("repository") or {}
            name = rp.get("nameWithOwner", "?") if isinstance(rp, dict) else "?"
            rows.append(("#%s" % it.get("number"),
                         "%s [%s] · %s" % (it.get("title", "?"),
                                           it.get("state", "?"),
                                           name)))
    return rows, [r["disp"]]


def _gh_token():
    """取 gh 令牌用于直连兜底提限额（只读用途）。取不到返回 None，不报错。"""
    r = run_gh(["auth", "token"], timeout=20)
    tok = r["stdout"].strip() if r.get("ok") else ""
    return tok or None


def api_get(path, timeout=30.0):
    """直连 api.github.com 的只读 GET 兜底（未认证也能读公开端点）。

    走系统/环境代理（正是为了能到 github.com）；带标准 Accept 头；带 gh 令牌时
    把限额从 60/h 提到认证档。返回 (parsed, meta)；失败抛 GhError。
    """
    url = path if str(path).startswith("http") else \
        "https://api.github.com/" + str(path).lstrip("/")
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "agentos-gh-client",
                 "X-GitHub-Api-Version": "2022-11-28"})
    tok = _gh_token()
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(body)
            except ValueError:
                raise GhError("json", "api 响应不是合法 JSON", [url])
            meta = {"sources": ["GET " + url],
                    "ratelimit_remaining": resp.headers.get(
                        "X-RateLimit-Remaining")}
            return parsed, meta
    except urllib.error.HTTPError as e:
        rem = None
        try:
            rem = e.headers.get("X-RateLimit-Remaining")
        except Exception:
            rem = None
        if e.code == 403 and rem == "0":
            raise GhError("ratelimit",
                          "GitHub API 限流（403，X-RateLimit-Remaining=0；"
                          "未认证公开端点约 60 次/时）", [url])
        try:
            detail = e.read()[:200].decode("utf-8", "replace")
        except Exception:
            detail = ""
        raise GhError("http", "GitHub API HTTP %s：%s" % (e.code, detail),
                      [url])
    except urllib.error.URLError as e:
        raise GhError("network", "GitHub API 网络错：%s" % e.reason, [url])


def rows_from_api(path, timeout=30.0):
    """api_get 的一层包装：直连兜底也吐 (rows, sources)，失败如实抛 GhError。

    rows 形状：数组响应压成前若干条 + 计数行；对象响应摊成键值行。
    """
    parsed, meta = api_get(path, timeout=timeout)
    rows = []
    if isinstance(parsed, list):
        rows.append(("条目数", str(len(parsed))))
        for it in parsed[:20]:
            if isinstance(it, dict):
                key = it.get("full_name") or it.get("name") or \
                    it.get("login") or it.get("title") or "item"
                rows.append((str(key), str(it.get("html_url")
                                          or it.get("description") or "-")[:80]))
            else:
                rows.append(("·", str(it)[:80]))
    elif isinstance(parsed, dict):
        for k in ("login", "name", "full_name", "description", "html_url",
                  "stargazers_count", "visibility", "type"):
            if k in parsed:
                rows.append((k, str(parsed[k])[:80]))
    else:
        rows.append(("·", str(parsed)[:120]))
    return rows, meta["sources"]


# ── 写面（R1）：唯一写收口 + 三闸策略纯函数 ─────────────────────────────
# 读铁律不动：run_gh 仍只放行只读白名单。写走独立的 run_gh_write，它只认
# 下面三张固定写模板的前缀（SEC T1：flag 集合封闭，-F/--body-file 类文件
# 旗标结构性进不来——cmd_gh 的解析器同样只认白名单旗标）。
_WRITE_PREFIXES = {
    ("issue", "comment"),
    ("issue", "create"),
    ("pr", "create"),
    ("repo", "create"),
}

# 审计核心（R2 下沉到本层：agent 工具与 CLI 共用一条哈希链）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIT_PATH = os.path.join(ROOT, "work", "gh_audit.jsonl")


def _sha_hex(s):
    import hashlib
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def audit_last_sha(path=None):
    path = path or AUDIT_PATH
    if not os.path.exists(path):
        return "0" * 64
    last = ""
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            if ln.strip():
                last = ln
    if not last:
        return "0" * 64
    try:
        return json.loads(last).get("sha") or ("0" * 64)
    except ValueError:
        raise ValueError("审计文件尾行损坏（先 gh audit 查，勿继续真写）")


def audit_append(kind, sub, repo, num, vals, rc, note, path=None):
    """append-only 哈希链审计（fail-closed 的证据层）。标题/正文只记
    SHA 前缀+长度，原文永不落审计。返回 sha；抛异常 = 调用方不得继续。"""
    path = path or AUDIT_PATH
    prev = audit_last_sha(path)
    payload = {"ts": round(time.time(), 3), "kind": kind, "sub": sub,
               "repo": repo, "num": num, "rc": rc, "note": note,
               "title_sha": _sha_hex(vals.get("title") or "")[:16],
               "title_len": len(vals.get("title") or ""),
               "body_sha": _sha_hex(vals.get("body") or "")[:16],
               "body_len": len(vals.get("body") or ""),
               "prev": prev}
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    payload["sha"] = _sha_hex(prev + line)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return payload["sha"]


def audit_exec_rows(path=None):
    rows = []
    path = path or AUDIT_PATH
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if r.get("kind") == "EXEC":
                rows.append((r.get("ts", 0), r.get("repo") or ""))
    return rows


def secret_scan(root):
    """部署前密钥扫描：命中文件名即拒发（SEC 纪律：.env/私钥/凭证文件）。"""
    hits = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs
                   if d not in (".git", "__pycache__", "node_modules")]
        for fn in files:
            low = fn.lower()
            if (low == ".env" or low.startswith(".env.")
                    or low.startswith("id_rsa") or low.startswith("id_ed25519")
                    or low.endswith(".pem") or low.endswith(".key")
                    or "credential" in low):
                hits.append(os.path.relpath(os.path.join(dirpath, fn), root))
    return hits

# SEC D1：每仓库每小时真写上限（dry-run 不计数，审计 EXEC 行才算）。
WRITE_RATE_LIMIT = 5


def write_policy(has_yes, tty, write_enabled, repo, exec_rows, now=None,
                 auto=False):
    """写面三闸 + 限速的纯函数裁决。返回 (ok, reason)。

    has_yes        命令行给了 --yes
    tty            stdin 是 TTY（SEC S1：--yes 必须人手在场逐次确认，
                   非交互一律拒 —— 脚本/agent 批量灌写的路在这里断）
    write_enabled  配置 gh.write_enabled 的原值；只有字面 "true" 算开
                   （SEC E1："yes"/"1"/"True" 一律不算，配置误写不提权）
    repo           本次要写的 owner/repo
    exec_rows      审计里的真写行 [(ts, repo)]（EXEC-DRY 不在内）
    auto           agent 自主路径（用户明确授权）：mode=full 且配置开时
                   免逐次 TTY 确认，但审计与限速一条不少（R2）
    """
    if auto:
        if str(write_enabled) != "true":
            return False, "config-off"
        now = time.time() if now is None else float(now)
        n = sum(1 for (ts, rp) in (exec_rows or [])
                if rp == repo and now - float(ts) < 3600)
        if n >= WRITE_RATE_LIMIT:
            return False, "rate-limit"
        return True, "ok"
    if not has_yes:
        return False, "no-yes"
    if not tty:
        return False, "non-tty"
    if str(write_enabled) != "true":
        return False, "config-off"
    now = time.time() if now is None else float(now)
    n = sum(1 for (ts, rp) in (exec_rows or [])
            if rp == repo and now - float(ts) < 3600)
    if n >= WRITE_RATE_LIMIT:
        return False, "rate-limit"
    return True, "ok"


def deploy_dir(dirpath, repo, public=False, dry_run=False, audit=None,
               timeout=180):
    """自主部署：目录 → GitHub 仓库（不存在则建）→ init/commit/push。

    dirpath 必须是已存在目录且已过密钥扫描（本函数再扫一次，双保险）；
    repo=owner/name；audit=可调用（返回 falsy = 审计失败，fail-closed 不
    执行写阶段）。dry_run=True 零副作用只出计划。
    """
    dirpath = os.path.abspath(dirpath)
    if not os.path.isdir(dirpath):
        return {"ok": False, "error": "目录不存在：%s" % dirpath}
    hits = secret_scan(dirpath)
    if hits:
        return {"ok": False,
                "error": "密钥扫描命中（拒发）：%s" % ", ".join(hits[:5])}
    if repo.count("/") != 1 or not all(repo.split("/")):
        return {"ok": False, "error": "repo 需要 owner/repo：%r" % repo}
    git_steps = ["git init（若无 .git）", "git add -A",
                 "git commit -m 'agentos deploy <ts>'（若有暂存）",
                 "git branch -M main", "git push -u origin main → %s" % repo]
    gh = resolve_gh()
    ex = run_gh(["repo", "view", repo, "--json", "nameWithOwner"], timeout=30)
    exists = bool(ex.get("ok"))
    plan = []
    if not exists:
        plan.append("gh repo create %s --%s"
                    % (repo, "public" if public else "private"))
    plan += git_steps
    if dry_run:
        return {"ok": True, "dry_run": True, "steps": plan,
                "secret_hits": hits}
    if gh is None:
        return {"ok": False, "error": "找不到 gh 可执行文件"}
    if not exists:
        if audit is not None and not audit("EXEC", "repo-create", repo,
                                           None, {}):
            return {"ok": False, "error": "审计落盘失败（fail-closed）"}
        rc = subprocess.run([gh, "repo", "create", repo,
                             "--" + ("public" if public else "private")],
                            capture_output=True, text=True, timeout=timeout)
        if rc.returncode != 0:
            return {"ok": False,
                    "error": "repo create 失败：%s"
                             % ((rc.stderr or rc.stdout) or "")[:160]}
    if audit is not None and not audit("EXEC", "deploy-push", repo, None,
                                       {}):
        return {"ok": False, "error": "审计落盘失败（fail-closed）"}

    def g(*argv):
        return subprocess.run(["git", "-C", dirpath] + list(argv),
                              capture_output=True, text=True,
                              timeout=timeout)

    if not os.path.isdir(os.path.join(dirpath, ".git")):
        g("init")
    g("add", "-A")
    g("commit", "-m", "agentos deploy %s" % time.strftime("%F %T"))
    br = (g("branch", "--show-current").stdout or "").strip()
    if br != "main":
        g("branch", "-M", "main")
    if g("remote", "get-url", "origin").returncode != 0:
        g("remote", "add", "origin",
          "https://github.com/%s.git" % repo)
    else:
        g("remote", "set-url", "origin",
          "https://github.com/%s.git" % repo)
    subprocess.run([gh, "auth", "setup-git"], capture_output=True,
                   text=True, timeout=60)
    push = subprocess.run(["git", "-C", dirpath, "push", "-u", "origin",
                           "main"], capture_output=True, text=True,
                          timeout=timeout)
    if push.returncode != 0:
        return {"ok": False, "created": exists,
                "error": "push 失败：%s"
                         % ((push.stderr or push.stdout) or "")[:200]}
    return {"ok": True, "repo": repo, "created": not exists, "pushed": True}


def run_gh_write(argv, dry_run=False, timeout=60.0):
    """唯一写收口：只接受固定写模板前缀；dry_run 永不落网。

    与 run_gh(只读) 双收口并列。调用契约（gh_gate 有判据盯）：
      · 调用方必须先落审计（fail-closed），审计失败不得走到这里；
      · argv 由 cmd_gh 的固定模板拼装（用户值走 --title=/--body= 赋值形，
        结构上进不了 flag 位），本函数再做一次前缀复核 —— 双保险；
      · dry_run=True 在解析 gh 可执行之前就返回，零进程零网络。
    返回体形状与 run_gh 同族（ok/kind/disp/stdout/stderr/parsed）。
    """
    argv = [str(a) for a in argv]
    if tuple(a.lower() for a in argv[:2]) not in _WRITE_PREFIXES:
        raise ValueError("gh 写向量不在写模板内（结构性拒绝）：%s"
                         % " ".join(argv[:3]))
    disp = "gh " + " ".join(argv)
    if dry_run:
        return {"ok": True, "kind": "dry-run", "rc": 0, "cmd": [disp],
                "disp": disp, "stdout": "", "stderr": "", "json_ok": False,
                "parsed": None, "error": None, "would_run": disp}
    gh = resolve_gh()
    if gh is None:
        return {"ok": False, "kind": "no-binary", "rc": None, "cmd": [disp],
                "disp": disp, "stdout": "", "stderr": "", "json_ok": False,
                "parsed": None,
                "error": "找不到 gh：PATH 与 %s 均无可用可执行文件" % _GH_HOME}
    try:
        p = subprocess.run([gh] + argv, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "kind": "timeout", "rc": 124, "cmd": [disp],
                "disp": disp, "stdout": "", "stderr": "", "json_ok": False,
                "parsed": None, "error": "gh 写调用超时（>%ss）" % timeout}
    except OSError as e:
        return {"ok": False, "kind": "no-binary", "rc": None, "cmd": [disp],
                "disp": disp, "stdout": "", "stderr": str(e), "json_ok": False,
                "parsed": None, "error": "gh 启动失败：%s" % e}
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    base = {"cmd": [disp], "disp": disp, "rc": p.returncode,
            "stderr": err[:400]}
    if p.returncode != 0:
        base.update({"ok": False, "kind": "rc", "stdout": out,
                     "json_ok": False, "parsed": None,
                     "error": "gh 写退出码 %s：%s" % (p.returncode,
                                                     (err or out)[:200])})
        return base
    base.update({"ok": True, "kind": "text", "stdout": out,
                 "json_ok": False, "parsed": None, "error": None})
    return base
