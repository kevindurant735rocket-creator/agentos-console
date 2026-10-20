#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evo_merge_gate —— W12 受控进化合入门：提案→沙箱→合入/回退全链可证伪。

为什么必须有这道门（体检报告 P0-2 验收口径）：进化面此前终点是"建议改
system prompt"，缺「安全地把自己改强」这一跳。本门站在外面认这些事实
（纯离线、桩门 leader_gate 做判卷器、不连模型）：

    D1  纯面闸：好提案过；坏路径（../ 绝对 禁改区）拒；锚点 0/多处命中拒；
        write 已存在拒、write 新文件过
    D2  拒绝面（核心）：坏提案 → rejected（**不碰真树**：真树字节零改动，
        且沙箱里确有坏编辑证据）
    D3  合入面（核心）：好提案 → merged → 真树含标记 → 真树 leader_gate
        复验绿 → verify_gate 绿（终端面的验收口径）
    D4  回退面（核心原语）：合入中途失败 → rolled_back + 已落盘的第一个
        文件**逐字节复原**（rc=2）
    D5  账本面：rejected/merged 两行齐、字段对（id/verdict/gates）
    D6  CLI 面：--help rc=0；提案不可读 rc=3
    D7  还原与零污染：门结束时真树 leader.py 与门前逐字同位；work/ 顶层
        只多 --out；evo_sandbox 清理干净；tools/ 无残留

用法: python3 tools/evo_merge_gate.py [--evo P] [--out P]
裁决：EVO_MERGE_GATE OK n/n（rc=0）/ EVO_MERGE_GATE RED（rc=2，有漏网）
"""
import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_EVO = os.path.join(HERE, "evo_merge.py")
DEFAULT_OUT = os.path.join(ROOT, "work", "evo_merge_gate.json")

EVO = DEFAULT_EVO
LEADER = os.path.join(HERE, "leader.py")
LEADER_ANCHOR = 'ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))'

results = []


def gate(name, ok, detail=""):
    results.append({"gate": name, "ok": bool(ok), "detail": str(detail)[:200]})
    print(("PASS " if ok else "FAIL ") + name
          + ("  -- " + str(detail)[:150] if detail else ""))


def load_mod(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sha(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return ""


def run_evo(proposal, gates, ledger, root="", extra=()):
    argv = [sys.executable, EVO, "apply", proposal,
            "--gates", gates, "--ledger", ledger]
    if root:
        argv += ["--root", root]
    argv += list(extra)
    return subprocess.run(argv, capture_output=True, text=True, timeout=600)


def run_gate_file(gate_name, cwd):
    p = subprocess.run([sys.executable, os.path.join(cwd, "tools",
                                                     "%s.py" % gate_name)],
                       capture_output=True, text=True, timeout=600, cwd=cwd)
    return p


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def _rows(ledger):
    rows = []
    try:
        with open(ledger, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    rows.append(json.loads(ln))
    except (OSError, ValueError):
        pass
    return rows


def _finish(out_path):
    n_ok = sum(1 for r in results if r["ok"])
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump({"judgments": results, "n_ok": n_ok,
                   "n_total": len(results)}, fh,
                  ensure_ascii=False, indent=2, sort_keys=True)
    print("EVO_MERGE_GATE %d/%d" % (n_ok, len(results)))
    if n_ok == len(results):
        print("EVO_MERGE_GATE_OK")
        return 0
    print("EVO_MERGE_GATE_RED")
    print("  红判据=%s" % [r["gate"] for r in results if not r["ok"]])
    return 2


def main():
    global EVO
    ap = argparse.ArgumentParser()
    ap.add_argument("--evo", default=DEFAULT_EVO)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()
    EVO = os.path.abspath(args.evo)
    print("== eve_merge_gate：受控进化合入（沙箱→合入/回退）")

    work = os.path.join(ROOT, "work")
    before_w = set(os.listdir(work)) if os.path.isdir(work) else set()
    tools = os.path.join(ROOT, "tools")
    before_t = set(os.listdir(tools))
    leader_before = b""
    with open(LEADER, "rb") as f:
        leader_before = f.read()
    leader_sha0 = _sha(LEADER)
    tmp = tempfile.mkdtemp(prefix="evomrg_")
    tag = "eg%d" % os.getpid()
    good_id, bad_id = tag + "-good", tag + "-bad"
    ledger = os.path.join(tmp, "ledger.jsonl")
    marker = "# evo-merge-g%s" % tag

    try:
        E = load_mod(EVO, "evo_g")

        # D1 纯面闸
        try:
            good = {"id": tag, "rationale": "t",
                    "edits": [{"path": "tools/leader.py",
                               "find": LEADER_ANCHOR,
                               "replace": LEADER_ANCHOR + "  " + marker}]}
            ok_good, _ = E.validate(good, ROOT)
            bad_paths = ["../x", "/etc/hosts", "native/x", "work/x",
                         ".venv-pw/x", "AgentOS.app/Contents/x", ".codex/x"]
            rej_paths = all(
                not E.validate({"id": "t", "edits": [{"path": bp,
                                                      "find": "a",
                                                      "replace": "b"}]},
                               ROOT)[0] for bp in bad_paths)
            rej_zero = not E.validate(
                {"id": "t", "edits": [{"path": "tools/leader.py",
                                       "find": "zzz_never_" + tag,
                                       "replace": "x"}]}, ROOT)[0]
            rej_multi = not E.validate(
                {"id": "t", "edits": [{"path": "tools/leader.py",
                                       "find": "import ",
                                       "replace": "x"}]}, ROOT)[0]
            rej_wr_exist = not E.validate(
                {"id": "t", "edits": [{"path": "tools/leader.py",
                                       "write": "x"}]}, ROOT)[0]
            # 路径闸专属探针：work/ 下**真实存在且锚点唯一**的文件 ——
            # 只有路径闸能拦；摘掉路径闸时它会一路过关（M3 变异体因此必红）
            wf = os.path.join(work, "%s-probe.txt" % tag)
            with open(wf, "w", encoding="utf-8") as f:
                f.write("PATHGATE_MARK\n")
            try:
                rej_work_file = not E.validate(
                    {"id": "t", "edits": [
                        {"path": "work/%s-probe.txt" % tag,
                         "find": "PATHGATE_MARK", "replace": "x"}]},
                    ROOT)[0]
            finally:
                try:
                    os.unlink(wf)
                except OSError:
                    pass
            ok_wr_new = E.validate(
                {"id": "t", "edits": [{"path": "tools/%s-new.py" % tag,
                                       "write": "x\n"}]}, ROOT)[0]
            gate("D1 纯面闸：好过 / 坏路径全拒（含 work/ 实存文件专属探针）"
                 " / 锚点 0与2处拒 / write 闸",
                 ok_good and rej_paths and rej_zero and rej_multi
                 and rej_wr_exist and rej_work_file and ok_wr_new,
                 "good=%s paths=%s zero=%s multi=%s wr=%s/%s wf=%s"
                 % (ok_good, rej_paths, rej_zero, rej_multi,
                    rej_wr_exist, ok_wr_new, rej_work_file))
        except Exception as e:                                 # noqa: BLE001
            gate("D1 纯面闸：好过 / 坏路径全拒 / 锚点 0与2处拒 / write 闸",
                 False, repr(e)[:120])

        # D2 拒绝面：坏提案 → rejected 且真树零改动
        try:
            bad = {"id": bad_id, "rationale": "break d1",
                   "edits": [{"path": "tools/leader.py",
                              "find": "    return dsp.new_plan(g, tasks)",
                              "replace": '    return {"goal": g}'}]}
            bp = os.path.join(tmp, "bad.json")
            _write_json(bp, bad)
            p = run_evo(bp, "leader_gate", ledger)
            sha_now = _sha(LEADER)
            sb_leader = os.path.join(ROOT, "work", "evo_sandbox", bad_id,
                                     "tools", "leader.py")
            sb_has_edit = False
            try:
                with open(sb_leader, encoding="utf-8") as f:
                    sb_has_edit = 'return {"goal": g}' in f.read()
            except OSError:
                pass
            verdicts = [r.get("verdict") for r in _rows(ledger)
                        if r.get("id") == bad_id]
            gate("D2 拒绝面：坏提案 rejected + 真树零改动 + 沙箱留坏编辑证据",
                 p.returncode == 2 and "EVO_MERGE rejected" in p.stdout
                 and sha_now == leader_sha0 and sb_has_edit
                 and verdicts == ["rejected"],
                 "rc=%s sha同=%s sandbox=%s verdicts=%s"
                 % (p.returncode, sha_now == leader_sha0, sb_has_edit,
                    verdicts))
        except Exception as e:                                 # noqa: BLE001
            gate("D2 拒绝面：坏提案 rejected + 真树零改动 + 沙箱留坏编辑证据",
                 False, repr(e)[:120])

        # D3 合入面：好提案 merged → 真树标记 → 复验绿 → capability 10/10
        try:
            gp = os.path.join(tmp, "good.json")
            _write_json(gp, {"id": good_id, "rationale": "marker",
                             "edits": [{"path": "tools/leader.py",
                                        "find": LEADER_ANCHOR,
                                        "replace": LEADER_ANCHOR + "  "
                                                   + marker}]})
            p = run_evo(gp, "leader_gate", ledger)
            merged_in_tree = marker in open(LEADER, encoding="utf-8").read()
            lr = run_gate_file("leader_gate", ROOT)
            # capability_gate 随网页驾驶舱一起下线了（它量的是网页 /__capmap
            # 能力账）→ 改用终端面仍然有的 verify_gate 复验。
            cg = run_gate_file("verify_gate", ROOT)
            verdicts = [r.get("verdict") for r in _rows(ledger)
                        if r.get("id") == good_id]
            gate("D3 合入面：merged + 真树含标记 + leader_gate 复验绿 + "
                 "verify_gate 绿",
                 p.returncode == 0 and "EVO_MERGE merged" in p.stdout
                 and merged_in_tree and lr.returncode == 0
                 and cg.returncode == 0 and verdicts == ["merged"],
                 "rc=%s tree=%s lg=%s cg=%s verdicts=%s"
                 % (p.returncode, merged_in_tree, lr.returncode,
                    cg.returncode, verdicts))
        except Exception as e:                                 # noqa: BLE001
            gate("D3 合入面：merged + 真树含标记 + leader_gate 复验绿 + "
                 "verify_gate 绿", False, repr(e)[:120])

        # 立即还原真树（D3 的合入是演示；本门结束时必须逐字同位）
        with open(LEADER, "wb") as f:
            f.write(leader_before)

        # D4 回退面（原语，进程内直调）：闸后目标消失 → 合入半途失败 → 回退
        try:
            ftr = os.path.join(tmp, "tree")
            os.makedirs(os.path.join(ftr, "tools"))
            shutil.copy2(LEADER, os.path.join(ftr, "tools", "leader.py"))
            with open(os.path.join(ftr, "tools", "second.py"), "w",
                      encoding="utf-8") as f:
                f.write("A\nSECOND_MARK\nB\n")
            l2 = os.path.join(ftr, "tools", "leader.py")
            with open(l2, "rb") as f:
                l2_before = f.read()
            edits2 = [
                {"path": "tools/leader.py", "find": LEADER_ANCHOR,
                 "replace": LEADER_ANCHOR + "  " + marker},
                {"path": "tools/second.py", "find": "SECOND_MARK",
                 "replace": "THIRD_MARK"}]
            prop2 = {"id": tag + "-roll", "edits": edits2}
            ok2, _e2 = E.validate(prop2, ftr)   # 夹具树下先过闸
            os.unlink(os.path.join(ftr, "tools", "second.py"))  # 闸后目标消失
            rc4 = E.merge_and_verify(prop2, ftr, [], ledger)
            with open(l2, "rb") as f:
                l2_after = f.read()
            rolls = [r for r in _rows(ledger)
                     if r.get("id") == tag + "-roll"
                     and r.get("verdict") == "rolled_back"]
            gate("D4 回退面：合入中途失败 → rolled_back + 第一文件逐字复原",
                 ok2 and rc4 == 2 and l2_after == l2_before
                 and len(rolls) == 1,
                 "闸=%s rc=%s 复原=%s roll账=%d"
                 % (ok2, rc4, l2_after == l2_before, len(rolls)))
        except Exception as e:                                 # noqa: BLE001
            gate("D4 回退面：合入中途失败 → rolled_back + 第一文件逐字复原",
                 False, repr(e)[:120])

        # D5 账本面
        try:
            rows = _rows(ledger)
            b = [r for r in rows if r.get("id") == bad_id]
            g = [r for r in rows if r.get("id") == good_id]
            gate("D5 账本面：rejected/merged 两行齐、字段对（含 gates 证据）",
                 len(b) == 1 and len(g) == 1
                 and b[0].get("verdict") == "rejected"
                 and isinstance(b[0].get("gates"), list)
                 and g[0].get("verdict") == "merged",
                 "rows=%d" % len(rows))
        except Exception as e:                                 # noqa: BLE001
            gate("D5 账本面：rejected/merged 两行齐、字段对（含 gates 证据）",
                 False, repr(e)[:120])

        # D6 CLI 面
        try:
            ph = subprocess.run([sys.executable, EVO, "--help"],
                                capture_output=True, text=True, timeout=60)
            pm = subprocess.run([sys.executable, EVO, "apply",
                                 os.path.join(tmp, "missing.json"),
                                 "--ledger", ledger],
                                capture_output=True, text=True, timeout=60)
            gate("D6 CLI 面：--help rc=0；提案不可读 rc=3",
                 ph.returncode == 0 and pm.returncode == 3,
                 "help=%s missing=%s" % (ph.returncode, pm.returncode))
        except Exception as e:                                 # noqa: BLE001
            gate("D6 CLI 面：--help rc=0；提案不可读 rc=3", False,
                 repr(e)[:120])
    finally:
        # 无论成败：真树 leader.py 还原 + 沙箱清理
        with open(LEADER, "wb") as f:
            f.write(leader_before)
        shutil.rmtree(os.path.join(ROOT, "work", "evo_sandbox"),
                      ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)

    # D7 还原与零污染
    restored = _sha(LEADER) == leader_sha0
    after_w = set(os.listdir(work)) if os.path.isdir(work) else set()
    after_t = set(os.listdir(tools))
    new_w = sorted((after_w - before_w)
                   - {os.path.basename(os.path.abspath(args.out))})
    new_t = sorted(after_t - before_t)
    gate("D7 还原与零污染：leader.py 逐字同位；work/ 只多 --out；"
         "沙箱清、tools/ 零残留",
         restored and not new_w and not new_t,
         "还原=%s 新增work=%s 新增tools=%s" % (restored,
                                              new_w or "无", new_t or "无"))

    return _finish(args.out)


if __name__ == "__main__":
    sys.exit(main())
