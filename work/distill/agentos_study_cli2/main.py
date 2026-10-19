#!/usr/bin/env python3
"""agentos-study-cli2 — 最小终端 agent CLI 骨架。

设计点复用自 Aider-AI/aider (Apache-2.0, ★49630) 的 README 观察：
  D1 改动可回滚：snapshot / undo
  D2 验证闭环：verify（真实 rc，失败即整体失败）
  D3 模型可插拔 + 项目地图：--model / AGENTOS_MODEL, map
纯标准库，无网络。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

SNAPSHOT_DIR = ".agentos_snapshots"
SKIP_DIRS = {".git", "__pycache__", ".agentos_snapshots", ".venv", "node_modules"}
DEFAULT_MODEL = "sonnet"


# ---------------------------------------------------------------- D3: 模型配置
def resolve_model(cli_value: str | None) -> str:
    """模型只是配置，不是硬编码分支：CLI 优先，其次环境变量，最后默认。"""
    return cli_value or os.environ.get("AGENTOS_MODEL") or DEFAULT_MODEL


def _iter_files(root: Path, limit: int):
    """按路径序遍历项目文件，跳过无关目录；达到 limit 即停（控制上下文体积）。"""
    count = 0
    for path in sorted(root.rglob("*")):
        if count >= limit:
            return
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        yield path
        count += 1


def _first_line(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.readline().strip()[:100]
    except OSError:
        return ""


def cmd_map(args: argparse.Namespace) -> int:
    """D3: 项目地图——给模型/人一个受限规模的仓库锚点，替代全量上下文。"""
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2
    rows = []
    for path in _iter_files(root, args.limit):
        try:
            nlines = sum(1 for _ in path.open("rb"))
        except OSError:
            nlines = -1
        rows.append(
            {
                "path": str(path.relative_to(root)),
                "lines": nlines,
                "head": _first_line(path),
            }
        )
    print(json.dumps({"root": str(root), "model": resolve_model(args.model),
                      "count": len(rows), "files": rows}, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------- D1: 可回滚写面
def _snapshot_dir(root: Path) -> Path:
    return root / SNAPSHOT_DIR


def cmd_snapshot(args: argparse.Namespace) -> int:
    """D1: 写面操作前打快照，供 undo 使用。"""
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2
    sdir = _snapshot_dir(root)
    sdir.mkdir(parents=True, exist_ok=True)

    label = args.label or "snap"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive = sdir / f"{stamp}-{label}.tar.gz"

    with tarfile.open(archive, "w:gz") as tar:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if any(part in SKIP_DIRS for part in rel.parts):
                continue
            tar.add(path, arcname=str(rel))

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = sdir / "manifest.json"
    entries = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else []
    entries.append({"archive": archive.name, "sha256": digest, "label": label,
                    "created": stamp})
    manifest.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"snapshot: {archive.name}\nsha256: {digest}\nfiles_tarred: {len(entries)} snapshot(s)")
    return 0


def cmd_undo(args: argparse.Namespace) -> int:
    """D1: 回滚到最近一个快照（先给当前状态补一个 pre-undo 快照，回滚本身也可回滚）。"""
    root = Path(args.root).resolve()
    sdir = _snapshot_dir(root)
    manifest = sdir / "manifest.json"
    if not manifest.exists():
        print("error: no snapshots found", file=sys.stderr)
        return 2
    entries = json.loads(manifest.read_text(encoding="utf-8"))
    if not entries:
        print("error: empty manifest", file=sys.stderr)
        return 2

    target = entries[-1]
    archive = sdir / target["archive"]
    if not archive.exists():
        print(f"error: archive missing: {archive}", file=sys.stderr)
        return 2
    if hashlib.sha256(archive.read_bytes()).hexdigest() != target["sha256"]:
        print("error: snapshot checksum mismatch", file=sys.stderr)
        return 3

    if not args.dry_run:
        cmd_snapshot(argparse.Namespace(root=str(root), label="pre-undo", model=None))
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                dest = (root / member.name).resolve()
                if not str(dest).startswith(str(root)):
                    print(f"error: unsafe path in archive: {member.name}", file=sys.stderr)
                    return 4
            tar.extractall(root)

    print(f"undo -> {target['archive']} (dry_run={args.dry_run})")
    return 0


# ---------------------------------------------------------------- D2: 验证闭环
def cmd_verify(args: argparse.Namespace) -> int:
    """D2: 顺序执行验证命令；收集真实 rc，任一非零即整体失败。"""
    if not args.cmd:
        print("error: at least one --cmd required", file=sys.stderr)
        return 2
    results = []
    failed = False
    for command in args.cmd:
        proc = subprocess.run(command, shell=True, capture_output=True, text=True)
        ok = proc.returncode == 0
        results.append({"cmd": command, "rc": proc.returncode, "ok": ok,
                        "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-2000:]})
        print(f"[{'PASS' if ok else 'FAIL'}] rc={proc.returncode} :: {command}")
        if not ok:
            failed = True
            if not args.keep_going:
                break
    if args.json:
        print(json.dumps({"results": results, "ok": not failed}, ensure_ascii=False, indent=2))
    return 1 if failed else 0


def cmd_info(args: argparse.Namespace) -> int:
    model = resolve_model(args.model)
    print(json.dumps({"tool": "agentos-study-cli2", "model": model,
                      "derived_from": "Aider-AI/aider"}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentos-study-cli2",
        description="Minimal terminal agent CLI skeleton (derived from Aider-AI/aider).",
    )
    parser.add_argument("--model", default=None,
                        help="model name; falls back to $AGENTOS_MODEL")
    sub = parser.add_subparsers(dest="command", required=True)

    p_map = sub.add_parser("map", help="D3: print a bounded project map")
    p_map.add_argument("--root", default=".")
    p_map.add_argument("--limit", type=int, default=40)
    p_map.add_argument("--model", default=None)
    p_map.set_defaults(func=cmd_map)

    p_snap = sub.add_parser("snapshot", help="D1: create a snapshot before writes")
    p_snap.add_argument("--root", default=".")
    p_snap.add_argument("--label", default=None)
    p_snap.set_defaults(func=cmd_snapshot)

    p_undo = sub.add_parser("undo", help="D1: restore the latest snapshot")
    p_undo.add_argument("--root", default=".")
    p_undo.add_argument("--dry-run", action="store_true")
    p_undo.set_defaults(func=cmd_undo)

    p_verify = sub.add_parser("verify", help="D2: run verification commands, fail fast")
    p_verify.add_argument("--cmd", action="append", default=[])
    p_verify.add_argument("--keep-going", action="store_true")
    p_verify.add_argument("--json", action="store_true")
    p_verify.set_defaults(func=cmd_verify)

    p_info = sub.add_parser("info", help="show resolved model / provenance")
    p_info.add_argument("--model", default=None)
    p_info.set_defaults(func=cmd_info)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
