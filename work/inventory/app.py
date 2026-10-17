#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""t2 落盘：本地库存后台 MVP（stdlib only）
借鉴：
- frappe/erpnext ★40266：Bin + Stock Ledger 台账（每次出入库记一条 ledger，不直接改数，库存=汇总）
- GreaterWMS ★4411：入库/出库/调拨/盘点闭环 + 库位字段
- nocobase ★24606：无代码后台思路 → 单文件 + JSON 数据 + 极简 HTTP 管理页
"""
import json, os, time

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data.json")

def _load():
    if not os.path.exists(DATA):
        return {"items": {}, "ledger": []}
    with open(DATA, encoding="utf-8") as f:
        return json.load(f)

def _save(d):
    tmp = DATA + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DATA)

def add(sku, name, qty, loc="A-01"):
    d = _load()
    it = d["items"].get(sku, {"sku": sku, "name": name, "qty": 0, "loc": loc})
    it["name"] = name or it["name"]; it["loc"] = loc or it["loc"]
    it["qty"] += int(qty)
    d["items"][sku] = it
    d["ledger"].append({"ts": time.time(), "op": "in", "sku": sku, "qty": int(qty)})
    _save(d)
    return it

def deduct(sku, qty):
    d = _load()
    it = d["items"].get(sku)
    if not it: return {"ok": False, "error": "no-sku"}
    if it["qty"] < int(qty): return {"ok": False, "error": "insufficient"}
    it["qty"] -= int(qty)
    d["ledger"].append({"ts": time.time(), "op": "out", "sku": sku, "qty": int(qty)})
    _save(d)
    return {"ok": True, "item": it}

def lst():
    d = _load()
    return sorted(d["items"].values(), key=lambda x: x["sku"])

def rollback(n=1):
    """回滚：撤销最近 n 条 ledger（含台账+结存），可用 = 可回滚判据"""
    d = _load()
    for _ in range(int(n)):
        if not d["ledger"]: break
        e = d["ledger"].pop()
        it = d["items"].get(e["sku"])
        if not it: continue
        if e["op"] == "in": it["qty"] -= e["qty"]
        else: it["qty"] += e["qty"]
    _save(d)
    return {"ok": True, "ledger_len": len(d["ledger"])}

if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        for it in lst(): print(f'{it["sku"]} {it["name"]} qty={it["qty"]} loc={it["loc"]}')
    elif cmd == "add":
        print(add(sys.argv[2], sys.argv[3], int(sys.argv[4])))
    elif cmd == "deduct":
        print(deduct(sys.argv[2], int(sys.argv[3])))
    elif cmd == "rollback":
        print(rollback(int(sys.argv[2]) if len(sys.argv) > 2 else 1))
